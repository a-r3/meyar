"""`meyar-ops verify-release` — verification only (issue #35 PR1 §10).
Never builds, installs, or extracts a release artifact to disk. Checksum
mismatch is always a hard failure. Archive member content is only ever
read (in memory, via `TarFile.extractfile`, never written to disk) after
every member has already passed `archive_safety.inspect_archive_members`
with zero violations — an unsafe archive's content is never touched.
"""

from __future__ import annotations

import hashlib
import tarfile
from pathlib import Path

from pydantic import ValidationError

from meyar.ops.archive_safety import inspect_archive_members
from meyar.ops.redact import safe_exception_text
from meyar.ops.release_manifest import ReleaseManifest, compute_release_id
from meyar.ops.result import FindingStatus, OpsResult, OpsResultBuilder

INTERNAL_MANIFEST_MEMBER_NAME = "release_manifest.json"
_MAX_FINDING_TEXT = 480


def _bounded(text: str, limit: int = _MAX_FINDING_TEXT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_sha256sums(text: str) -> dict[str, str]:
    """Standard `sha256sum`-style output: `<hex digest>  <filename>` (or
    `*<filename>` for binary mode) per line. Malformed lines are ignored
    — callers only care whether the specific artifact filename they need
    is present with a well-formed digest."""
    entries: dict[str, str] = {}
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
        entries[filename.lstrip("*").strip()] = digest
    return entries


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
    sums = _check_sha256sums_format(builder, sha256sums_path)
    _check_artifact_checksum(builder, sums, artifact_path)
    archive_ok = _check_archive_safety(builder, artifact_path, expected_root)
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
) -> dict[str, str] | None:
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
    sums = parse_sha256sums(text)
    if not sums:
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
        message=f"parsed {len(sums)} checksum entr{'y' if len(sums) == 1 else 'ies'}",
    )
    return sums


def _check_artifact_checksum(
    builder: OpsResultBuilder, sums: dict[str, str] | None, artifact_path: Path
) -> None:
    if sums is None:
        builder.add(
            component="artifact_checksum",
            status=FindingStatus.SKIPPED,
            code="SHA256SUMS_INVALID",
            message="skipped: SHA256SUMS could not be parsed",
        )
        return

    artifact_name = artifact_path.name
    expected_digest = sums.get(artifact_name)
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


def _check_internal_manifest(
    builder: OpsResultBuilder,
    manifest: ReleaseManifest | None,
    artifact_path: Path,
    expected_root: str | None,
    archive_ok: bool,
) -> None:
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

    if (
        internal.release_id == manifest.release_id
        and internal.release_version == manifest.release_version
        and internal.source_sha == manifest.source_sha
    ):
        builder.add(
            component="internal_manifest_consistency",
            status=FindingStatus.OK,
            code="INTERNAL_MANIFEST_CONSISTENT",
            message="embedded manifest identity matches the external manifest",
        )
    else:
        builder.add(
            component="internal_manifest_consistency",
            status=FindingStatus.FAIL,
            code="INTERNAL_MANIFEST_MISMATCH",
            message="embedded manifest identity does not match the external manifest",
        )
