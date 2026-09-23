"""`meyar-ops verify-release` — verification only (issue #35 PR1 §10).
Never builds, installs, or extracts a release artifact to disk. Checksum
mismatch is always a hard failure. Archive member content is only ever
read (in memory, via `TarFile.extractfile`, never written to disk) after
every member has already passed `archive_safety.inspect_archive_members`
with zero violations — an unsafe archive's content is never touched.

This module is an operational security boundary against a downloaded
release artifact, so every content read below is bounded: archive member
scanning itself is bounded in `archive_safety` (member count, name
length, aggregate declared size), and the two specific members this
module reads by name (the embedded release manifest and `backend/
uv.lock`) each have their own declared-size bound checked *before* any
byte is read, so a lying/oversized header can never force an unbounded
in-memory read.
"""

from __future__ import annotations

import hashlib
import tarfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from meyar.ops.archive_safety import ArchiveBoundExceededError, inspect_archive_members
from meyar.ops.redact import safe_exception_text
from meyar.ops.release_manifest import ReleaseManifest, compute_release_id
from meyar.ops.result import FindingStatus, OpsResult, OpsResultBuilder

INTERNAL_MANIFEST_MEMBER_NAME = "release_manifest.json"
UV_LOCK_MEMBER_RELATIVE_PATH = "backend/uv.lock"
_MAX_FINDING_TEXT = 480
_CHUNK_SIZE = 1024 * 1024

# release_manifest.json is a few KB of flat JSON; 1 MiB is generous
# headroom while still bounding the read against a header that lies
# about a much smaller file's declared size.
_MAX_EMBEDDED_MANIFEST_SIZE = 1 * 1024 * 1024

# This project's backend/uv.lock is well under 1 MiB; 16 MiB is generous
# headroom for future dependency growth while still bounding the read.
_MAX_UV_LOCK_SIZE = 16 * 1024 * 1024


def _bounded(text: str, limit: int = _MAX_FINDING_TEXT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass(frozen=True)
class ParsedChecksums:
    """`digests` holds one well-formed entry per unique filename;
    `ambiguous_filenames` holds every filename that appeared more than
    once (with well-formed digest lines) — those names are deliberately
    absent from `digests` rather than silently resolved last-write-wins,
    so a caller can report ambiguity as its own truthful outcome."""

    digests: dict[str, str]
    ambiguous_filenames: frozenset[str]


def parse_sha256sums(text: str) -> ParsedChecksums:
    """Standard `sha256sum`-style output: `<hex digest>  <filename>` (or
    `*<filename>` for binary mode) per line. Malformed lines are ignored
    — callers only care whether the specific artifact filename they need
    is present with a well-formed, unambiguous digest. A filename that
    appears more than once with a well-formed digest is never resolved
    last-write-wins — it is reported as ambiguous instead."""
    digests: dict[str, str] = {}
    ambiguous: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        digest, filename = parts
        digest = digest.lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            continue
        name = filename.lstrip("*").strip()
        if name in digests or name in ambiguous:
            ambiguous.add(name)
            digests.pop(name, None)
            continue
        digests[name] = digest
    return ParsedChecksums(digests=digests, ambiguous_filenames=frozenset(ambiguous))


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def verify_release(
    *,
    manifest_path: Path,
    sha256sums_path: Path,
    artifact_path: Path,
    expected_release_id: str | None = None,
) -> OpsResult:
    builder = OpsResultBuilder(action="verify-release")

    manifest = _check_manifest_schema(builder, manifest_path)
    expected_root = _check_release_identity(builder, manifest, expected_release_id)
    parsed_sums = _check_sha256sums_format(builder, sha256sums_path)
    _check_artifact_checksum(builder, parsed_sums, artifact_path)
    archive_ok = _check_archive_safety(builder, artifact_path, expected_root)
    _check_uv_lock_binding(builder, manifest, artifact_path, expected_root, archive_ok)
    _check_internal_manifest(builder, manifest, artifact_path, expected_root, archive_ok)

    return builder.build()


def _check_manifest_schema(
    builder: OpsResultBuilder, manifest_path: Path
) -> ReleaseManifest | None:
    try:
        raw = manifest_path.read_text()
    except OSError as exc:
        builder.add(
            component="manifest_schema",
            status=FindingStatus.FAIL,
            code="MANIFEST_UNREADABLE",
            message=_bounded(f"could not read release manifest: {safe_exception_text(exc)}"),
        )
        return None
    try:
        manifest = ReleaseManifest.model_validate_json(raw)
    except ValidationError as exc:
        error_summary = f"release manifest failed schema validation: {exc.error_count()} error(s)"
        builder.add(
            component="manifest_schema",
            status=FindingStatus.FAIL,
            code="MANIFEST_SCHEMA_INVALID",
            message=_bounded(error_summary),
        )
        return None
    builder.add(
        component="manifest_schema",
        status=FindingStatus.OK,
        code="MANIFEST_VALID",
        message="release manifest schema is valid",
    )
    return manifest


def _check_release_identity(
    builder: OpsResultBuilder,
    manifest: ReleaseManifest | None,
    expected_release_id: str | None,
) -> str | None:
    """Returns the expected archive root prefix (the manifest's
    release_id) when derivable, else None."""
    if manifest is None:
        builder.add(
            component="release_identity",
            status=FindingStatus.SKIPPED,
            code="MANIFEST_INVALID",
            message="skipped: release manifest failed schema validation",
        )
        if expected_release_id is not None:
            builder.add(
                component="expected_release_id",
                status=FindingStatus.SKIPPED,
                code="MANIFEST_INVALID",
                message="skipped: release manifest failed schema validation",
            )
        return None

    try:
        derived = compute_release_id(
            release_version=manifest.release_version, source_sha=manifest.source_sha
        )
    except ValueError as exc:
        builder.add(
            component="release_identity",
            status=FindingStatus.FAIL,
            code="SOURCE_SHA_INVALID",
            message=_bounded(safe_exception_text(exc)),
        )
        derived = None
    else:
        if derived == manifest.release_id:
            builder.add(
                component="release_identity",
                status=FindingStatus.OK,
                code="RELEASE_ID_MATCHES",
                message="release_id matches the deterministic (release_version, source_sha) id",
            )
        else:
            builder.add(
                component="release_identity",
                status=FindingStatus.FAIL,
                code="RELEASE_ID_MISMATCH",
                message="declared release_id does not match the deterministic identity",
            )

    if expected_release_id is not None:
        if manifest.release_id == expected_release_id:
            builder.add(
                component="expected_release_id",
                status=FindingStatus.OK,
                code="EXPECTED_RELEASE_ID_MATCHES",
                message="release_id matches the expected release id",
            )
        else:
            builder.add(
                component="expected_release_id",
                status=FindingStatus.FAIL,
                code="EXPECTED_RELEASE_ID_MISMATCH",
                message="release_id does not match the expected release id",
            )

    return manifest.release_id


def _check_sha256sums_format(
    builder: OpsResultBuilder, sha256sums_path: Path
) -> ParsedChecksums | None:
    try:
        text = sha256sums_path.read_text()
    except OSError as exc:
        builder.add(
            component="sha256sums_format",
            status=FindingStatus.FAIL,
            code="SHA256SUMS_UNREADABLE",
            message=_bounded(f"could not read SHA256SUMS: {safe_exception_text(exc)}"),
        )
        return None
    parsed = parse_sha256sums(text)
    if not parsed.digests and not parsed.ambiguous_filenames:
        builder.add(
            component="sha256sums_format",
            status=FindingStatus.FAIL,
            code="SHA256SUMS_EMPTY_OR_MALFORMED",
            message="SHA256SUMS contained no well-formed digest lines",
        )
        return None
    builder.add(
        component="sha256sums_format",
        status=FindingStatus.OK,
        code="SHA256SUMS_VALID",
        message=f"parsed {len(parsed.digests)} checksum entr"
        f"{'y' if len(parsed.digests) == 1 else 'ies'}",
    )
    return parsed


def _check_artifact_checksum(
    builder: OpsResultBuilder, parsed: ParsedChecksums | None, artifact_path: Path
) -> None:
    if parsed is None:
        builder.add(
            component="artifact_checksum",
            status=FindingStatus.SKIPPED,
            code="SHA256SUMS_INVALID",
            message="skipped: SHA256SUMS could not be parsed",
        )
        return

    artifact_name = artifact_path.name
    if artifact_name in parsed.ambiguous_filenames:
        builder.add(
            component="artifact_checksum",
            status=FindingStatus.FAIL,
            code="CHECKSUM_ENTRY_AMBIGUOUS",
            message=_bounded(
                f"multiple SHA256SUMS entries for '{artifact_name}' — ambiguous, not resolved"
            ),
        )
        return

    expected_digest = parsed.digests.get(artifact_name)
    if expected_digest is None:
        builder.add(
            component="artifact_checksum",
            status=FindingStatus.FAIL,
            code="CHECKSUM_ENTRY_MISSING",
            message=_bounded(f"no SHA256SUMS entry for '{artifact_name}'"),
        )
        return

    try:
        actual_digest = _sha256_file(artifact_path)
    except OSError as exc:
        builder.add(
            component="artifact_checksum",
            status=FindingStatus.FAIL,
            code="ARTIFACT_UNREADABLE",
            message=_bounded(f"could not read artifact: {safe_exception_text(exc)}"),
        )
        return

    if actual_digest == expected_digest:
        builder.add(
            component="artifact_checksum",
            status=FindingStatus.OK,
            code="CHECKSUM_MATCHES",
            message="artifact SHA-256 matches the external SHA256SUMS entry",
        )
    else:
        builder.add(
            component="artifact_checksum",
            status=FindingStatus.FAIL,
            code="CHECKSUM_MISMATCH",
            message="artifact SHA-256 does not match the external SHA256SUMS entry",
        )


def _check_archive_safety(
    builder: OpsResultBuilder, artifact_path: Path, expected_root: str | None
) -> bool:
    if expected_root is None:
        # No trustworthy release_id to bound member paths against — still
        # run the archive open/inspect so unsafe member *shapes*
        # (absolute/traversal/symlink/hardlink/device) are still caught,
        # using the artifact's own bare filename as a best-effort root.
        expected_root = artifact_path.name.split(".tar")[0]

    try:
        violations = inspect_archive_members(artifact_path, expected_root=expected_root)
    except ArchiveBoundExceededError as exc:
        builder.add(
            component="archive_safety",
            status=FindingStatus.FAIL,
            code="ARCHIVE_RESOURCE_BOUND_EXCEEDED",
            message=_bounded(f"archive exceeded a resource bound: {exc.reason}"),
        )
        return False
    except (tarfile.TarError, OSError) as exc:
        builder.add(
            component="archive_safety",
            status=FindingStatus.FAIL,
            code="ARCHIVE_UNREADABLE",
            message=_bounded(f"could not read archive: {safe_exception_text(exc)}"),
        )
        return False

    if violations:
        summary = "; ".join(f"{v.member_name}: {v.reason}" for v in violations[:5])
        builder.add(
            component="archive_safety",
            status=FindingStatus.FAIL,
            code="UNSAFE_ARCHIVE_MEMBER",
            message=_bounded(f"{len(violations)} unsafe archive member(s): {summary}"),
        )
        return False

    builder.add(
        component="archive_safety",
        status=FindingStatus.OK,
        code="ARCHIVE_SAFE",
        message="no unsafe archive member shapes found",
    )
    return True


def _check_uv_lock_binding(
    builder: OpsResultBuilder,
    manifest: ReleaseManifest | None,
    artifact_path: Path,
    expected_root: str | None,
    archive_ok: bool,
) -> None:
    """Binds `ReleaseManifest.uv_lock_sha256` to the actual dependency
    lockfile shipped *inside* the artifact — a manifest field that is
    merely well-formed hex proves nothing about what the artifact
    actually contains. Never extracts to disk: the member is located by
    exact path, its declared size is bounded before any read, and its
    content is hashed via a bounded streaming read through
    `TarFile.extractfile` (an in-memory file object), never written to
    disk."""
    if manifest is None or expected_root is None:
        builder.add(
            component="uv_lock_binding",
            status=FindingStatus.SKIPPED,
            code="EXTERNAL_MANIFEST_INVALID",
            message="skipped: no valid external release manifest to compare against",
        )
        return
    if not archive_ok:
        builder.add(
            component="uv_lock_binding",
            status=FindingStatus.SKIPPED,
            code="ARCHIVE_UNSAFE",
            message="skipped: archive failed the safety inspection",
        )
        return

    member_path = f"{expected_root}/{UV_LOCK_MEMBER_RELATIVE_PATH}"
    try:
        with tarfile.open(artifact_path, mode="r:*") as archive:
            matches = [m for m in archive.getmembers() if m.name == member_path]
            if not matches:
                builder.add(
                    component="uv_lock_binding",
                    status=FindingStatus.FAIL,
                    code="UV_LOCK_MEMBER_MISSING",
                    message=_bounded(f"no '{member_path}' member found in archive"),
                )
                return
            if len(matches) > 1:
                builder.add(
                    component="uv_lock_binding",
                    status=FindingStatus.FAIL,
                    code="UV_LOCK_MEMBER_AMBIGUOUS",
                    message=_bounded(
                        f"{len(matches)} archive members found at '{member_path}' — ambiguous"
                    ),
                )
                return
            member = matches[0]
            if not member.isfile():
                builder.add(
                    component="uv_lock_binding",
                    status=FindingStatus.FAIL,
                    code="UV_LOCK_MEMBER_NOT_REGULAR_FILE",
                    message=_bounded(f"'{member_path}' is not a regular file"),
                )
                return
            if member.size > _MAX_UV_LOCK_SIZE:
                builder.add(
                    component="uv_lock_binding",
                    status=FindingStatus.FAIL,
                    code="UV_LOCK_MEMBER_TOO_LARGE",
                    message=_bounded(
                        f"'{member_path}' declared size {member.size} exceeds bound of "
                        f"{_MAX_UV_LOCK_SIZE} bytes"
                    ),
                )
                return
            extracted = archive.extractfile(member)
            if extracted is None:
                builder.add(
                    component="uv_lock_binding",
                    status=FindingStatus.FAIL,
                    code="UV_LOCK_MEMBER_NOT_REGULAR_FILE",
                    message=_bounded(f"'{member_path}' has no readable file content"),
                )
                return
            hasher = hashlib.sha256()
            for chunk in iter(lambda: extracted.read(_CHUNK_SIZE), b""):
                hasher.update(chunk)
            actual_digest = hasher.hexdigest()
    except (tarfile.TarError, OSError) as exc:
        builder.add(
            component="uv_lock_binding",
            status=FindingStatus.FAIL,
            code="ARCHIVE_UNREADABLE",
            message=_bounded(f"could not read '{member_path}': {safe_exception_text(exc)}"),
        )
        return

    if actual_digest == manifest.uv_lock_sha256:
        builder.add(
            component="uv_lock_binding",
            status=FindingStatus.OK,
            code="UV_LOCK_SHA256_MATCHES",
            message=(
                f"'{UV_LOCK_MEMBER_RELATIVE_PATH}' content matches "
                "ReleaseManifest.uv_lock_sha256"
            ),
        )
    else:
        builder.add(
            component="uv_lock_binding",
            status=FindingStatus.FAIL,
            code="UV_LOCK_SHA256_MISMATCH",
            message=(
                f"'{UV_LOCK_MEMBER_RELATIVE_PATH}' content does not match "
                "ReleaseManifest.uv_lock_sha256"
            ),
        )


def _check_internal_manifest(
    builder: OpsResultBuilder,
    manifest: ReleaseManifest | None,
    artifact_path: Path,
    expected_root: str | None,
    archive_ok: bool,
) -> None:
    """Compares the *complete* canonical typed manifest (every field via
    `ReleaseManifest.__eq__`, i.e. structured field values — never raw
    JSON text/formatting/order), not just an identity subset. Any
    semantically relevant field mismatch (uv_lock_sha256, alembic_heads,
    rollback_compatibility, model_manifest, required_python_version,
    artifact_format/version, built_at, ...) is a hard failure — a
    manifest that agrees only on release_id/version/source_sha is not
    "consistent"."""
    if manifest is None or expected_root is None:
        builder.add(
            component="internal_manifest_consistency",
            status=FindingStatus.SKIPPED,
            code="EXTERNAL_MANIFEST_INVALID",
            message="skipped: no valid external release manifest to compare against",
        )
        return
    if not archive_ok:
        builder.add(
            component="internal_manifest_consistency",
            status=FindingStatus.SKIPPED,
            code="ARCHIVE_UNSAFE",
            message="skipped: archive failed the safety inspection",
        )
        return

    member_path = f"{expected_root}/{INTERNAL_MANIFEST_MEMBER_NAME}"
    try:
        with tarfile.open(artifact_path, mode="r:*") as archive:
            try:
                member = archive.getmember(member_path)
            except KeyError:
                builder.add(
                    component="internal_manifest_consistency",
                    status=FindingStatus.SKIPPED,
                    code="NO_INTERNAL_MANIFEST",
                    message="no embedded release manifest found inside the archive",
                )
                return
            if member.size > _MAX_EMBEDDED_MANIFEST_SIZE:
                builder.add(
                    component="internal_manifest_consistency",
                    status=FindingStatus.FAIL,
                    code="INTERNAL_MANIFEST_TOO_LARGE",
                    message=_bounded(
                        f"'{member_path}' declared size {member.size} exceeds bound of "
                        f"{_MAX_EMBEDDED_MANIFEST_SIZE} bytes"
                    ),
                )
                return
            extracted = archive.extractfile(member)
            raw = extracted.read() if extracted is not None else b""
    except (tarfile.TarError, OSError) as exc:
        builder.add(
            component="internal_manifest_consistency",
            status=FindingStatus.FAIL,
            code="ARCHIVE_UNREADABLE",
            message=_bounded(f"could not read embedded manifest: {safe_exception_text(exc)}"),
        )
        return

    try:
        internal = ReleaseManifest.model_validate_json(raw)
    except ValidationError:
        builder.add(
            component="internal_manifest_consistency",
            status=FindingStatus.FAIL,
            code="INTERNAL_MANIFEST_SCHEMA_INVALID",
            message="embedded release manifest failed schema validation",
        )
        return

    if internal == manifest:
        builder.add(
            component="internal_manifest_consistency",
            status=FindingStatus.OK,
            code="INTERNAL_MANIFEST_CONSISTENT",
            message="embedded manifest is fully consistent with the external manifest",
        )
    else:
        builder.add(
            component="internal_manifest_consistency",
            status=FindingStatus.FAIL,
            code="INTERNAL_MANIFEST_MISMATCH",
            message="embedded manifest does not fully match the external manifest",
        )
