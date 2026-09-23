"""`meyar-ops verify-release` (issue #35 PR1 §10/§12). All fixtures are
built synthetically under tmp_path — no artifact/manifest is ever
extracted to disk by these tests or by the code under test."""

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from meyar.ops.archive_safety import MAX_ARCHIVE_MEMBER_COUNT
from meyar.ops.release_manifest import compute_release_id
from meyar.ops.verify_release import (
    _MAX_EMBEDDED_MANIFEST_SIZE,
    _MAX_EXTERNAL_MANIFEST_SIZE,
    _MAX_SHA256SUMS_SIZE,
    _MAX_UV_LOCK_SIZE,
    parse_sha256sums,
    verify_release,
)

VALID_SHA = "a" * 40
RELEASE_VERSION = "0.1.0"
RELEASE_ID = compute_release_id(release_version=RELEASE_VERSION, source_sha=VALID_SHA)

UV_LOCK_CONTENT = b"# synthetic backend/uv.lock fixture content\nversion = 1\n"
UV_LOCK_SHA256 = hashlib.sha256(UV_LOCK_CONTENT).hexdigest()


def _manifest_dict(**overrides: object) -> dict:
    base = {
        "manifest_schema_version": 1,
        "release_version": RELEASE_VERSION,
        "release_id": RELEASE_ID,
        "source_sha": VALID_SHA,
        "built_at": "2026-09-23T00:00:00Z",
        "required_python_version": ">=3.12",
        "uv_lock_sha256": UV_LOCK_SHA256,
        "alembic_heads": ["6f4c2a9d8e10"],
        "rollback_compatibility": "BACKUP_RESTORE_REQUIRED",
        "model_manifest": {
            "reference": "docs/DECISIONS.md#D-066",
            "status": "DEVELOPMENT_INTEGRATION",
        },
        "artifact_format": "tar.gz",
        "artifact_format_version": 1,
    }
    base.update(overrides)
    return base


def _write_manifest(path: Path, **overrides: object) -> dict:
    data = _manifest_dict(**overrides)
    path.write_text(json.dumps(data))
    return data


def _add_file_member(tf: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    tf.addfile(info, io.BytesIO(data))


def _build_artifact(
    tmp_path: Path,
    *,
    root: str = RELEASE_ID,
    embed_manifest: dict | None | bool = True,
    extra_members: dict[str, bytes] | None = None,
    uv_lock: bytes | None = UV_LOCK_CONTENT,
    duplicate_uv_lock: bool = False,
    uv_lock_as_directory: bool = False,
    archive_name: str = "meyar-release.tar.gz",
) -> Path:
    """embed_manifest: True embeds `_manifest_dict()`, a dict embeds that
    exact dict, False/None embeds nothing. uv_lock: bytes embeds
    `{root}/backend/uv.lock` with that content (defaults to the fixture
    content whose digest matches `_manifest_dict()`'s uv_lock_sha256);
    None omits it entirely."""
    artifact_path = tmp_path / archive_name
    with tarfile.open(artifact_path, mode="w:gz") as tf:
        if embed_manifest:
            payload = json.dumps(
                embed_manifest if isinstance(embed_manifest, dict) else _manifest_dict()
            ).encode()
            _add_file_member(tf, f"{root}/release_manifest.json", payload)
        if uv_lock_as_directory:
            info = tarfile.TarInfo(name=f"{root}/backend/uv.lock")
            info.type = tarfile.DIRTYPE
            tf.addfile(info)
        elif uv_lock is not None:
            _add_file_member(tf, f"{root}/backend/uv.lock", uv_lock)
            if duplicate_uv_lock:
                _add_file_member(tf, f"{root}/backend/uv.lock", uv_lock)
        for name, data in (extra_members or {f"{root}/app/main.py": b"hello"}).items():
            _add_file_member(tf, name, data)
    return artifact_path


def _write_sha256sums(
    tmp_path: Path,
    artifact_path: Path,
    manifest_path: Path | None = None,
    *,
    digest: str | None = None,
    manifest_digest: str | None = None,
    include_manifest_entry: bool = True,
) -> Path:
    """A full release bundle's SHA256SUMS covers both the artifact tarball
    and the external release manifest (Blocker 1 — corrective review): the
    manifest checksum entry is included by default whenever a
    ``manifest_path`` is given, so a "valid release" fixture passes both
    `artifact_checksum` and `manifest_checksum`, not just the former."""
    actual = digest or hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    lines = [f"{actual}  {artifact_path.name}"]
    if manifest_path is not None and include_manifest_entry:
        m_digest = manifest_digest or hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        lines.append(f"{m_digest}  {manifest_path.name}")
    sums_path = tmp_path / "SHA256SUMS"
    sums_path.write_text("\n".join(lines) + "\n")
    return sums_path


def test_fully_valid_release_verifies_ok(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    artifact_path = _build_artifact(tmp_path)
    sums_path = _write_sha256sums(tmp_path, artifact_path, manifest_path)

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is True
    codes = {f.component: f.status.value for f in result.findings}
    assert codes["manifest_schema"] == "OK"
    assert codes["release_identity"] == "OK"
    assert codes["artifact_checksum"] == "OK"
    assert codes["manifest_checksum"] == "OK"
    assert codes["release_bundle_integrity"] == "OK"
    assert codes["archive_safety"] == "OK"
    assert codes["uv_lock_binding"] == "OK"
    assert codes["internal_manifest_consistency"] == "OK"


def test_invalid_manifest_schema_fails_and_skips_downstream(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"not": "a manifest"}))
    artifact_path = _build_artifact(tmp_path)
    sums_path = _write_sha256sums(tmp_path, artifact_path)

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is False
    by_component = {f.component: f for f in result.findings}
    assert by_component["manifest_schema"].status.value == "FAIL"
    assert by_component["release_identity"].status.value == "SKIPPED"


def test_invalid_source_sha_in_manifest_fails(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    raw = _manifest_dict()
    raw["source_sha"] = "not-a-sha"
    manifest_path.write_text(json.dumps(raw))
    artifact_path = _build_artifact(tmp_path)
    sums_path = _write_sha256sums(tmp_path, artifact_path)

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is False
    by_component = {f.component: f for f in result.findings}
    assert by_component["manifest_schema"].status.value == "FAIL"


def test_release_id_mismatch_fails(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, release_id="totally-wrong-id")
    artifact_path = _build_artifact(tmp_path)
    sums_path = _write_sha256sums(tmp_path, artifact_path)

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is False
    by_component = {f.component: f for f in result.findings}
    assert by_component["release_identity"].code == "RELEASE_ID_MISMATCH"


def test_expected_release_id_mismatch_fails(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    artifact_path = _build_artifact(tmp_path)
    sums_path = _write_sha256sums(tmp_path, artifact_path)

    result = verify_release(
        manifest_path=manifest_path,
        sha256sums_path=sums_path,
        artifact_path=artifact_path,
        expected_release_id="some-other-release-id",
    )
    assert result.ok is False
    by_component = {f.component: f for f in result.findings}
    assert by_component["expected_release_id"].code == "EXPECTED_RELEASE_ID_MISMATCH"


def test_checksum_mismatch_is_a_hard_failure(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    artifact_path = _build_artifact(tmp_path)
    wrong_digest = "0" * 64
    sums_path = _write_sha256sums(tmp_path, artifact_path, digest=wrong_digest)

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is False
    by_component = {f.component: f for f in result.findings}
    assert by_component["artifact_checksum"].code == "CHECKSUM_MISMATCH"


def test_missing_sha256sums_entry_fails(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    artifact_path = _build_artifact(tmp_path)
    sums_path = tmp_path / "SHA256SUMS"
    sums_path.write_text(f"{'0' * 64}  some-other-file.tar.gz\n")

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is False
    by_component = {f.component: f for f in result.findings}
    assert by_component["artifact_checksum"].code == "CHECKSUM_ENTRY_MISSING"


def test_duplicate_sha256sums_entry_for_artifact_is_rejected_as_ambiguous(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    artifact_path = _build_artifact(tmp_path)
    digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    sums_path = tmp_path / "SHA256SUMS"
    sums_path.write_text(f"{digest}  {artifact_path.name}\n{digest}  {artifact_path.name}\n")

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is False
    by_component = {f.component: f for f in result.findings}
    assert by_component["artifact_checksum"].code == "CHECKSUM_ENTRY_AMBIGUOUS"


def test_unsafe_archive_absolute_path_rejected(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    artifact_path = _build_artifact(
        tmp_path, embed_manifest=False, uv_lock=None, extra_members={"/etc/passwd": b"evil"}
    )
    sums_path = _write_sha256sums(tmp_path, artifact_path)

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is False
    by_component = {f.component: f for f in result.findings}
    assert by_component["archive_safety"].code == "UNSAFE_ARCHIVE_MEMBER"
    assert by_component["internal_manifest_consistency"].status.value == "SKIPPED"
    assert by_component["uv_lock_binding"].status.value == "SKIPPED"


def test_unsafe_archive_traversal_rejected(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    artifact_path = _build_artifact(
        tmp_path,
        embed_manifest=False,
        uv_lock=None,
        extra_members={f"{RELEASE_ID}/../../escape.txt": b"evil"},
    )
    sums_path = _write_sha256sums(tmp_path, artifact_path)

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is False
    by_component = {f.component: f for f in result.findings}
    assert by_component["archive_safety"].code == "UNSAFE_ARCHIVE_MEMBER"


def test_no_internal_manifest_is_skipped_not_failed(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    artifact_path = _build_artifact(tmp_path, embed_manifest=False)
    sums_path = _write_sha256sums(tmp_path, artifact_path, manifest_path)

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is True
    by_component = {f.component: f for f in result.findings}
    assert by_component["internal_manifest_consistency"].code == "NO_INTERNAL_MANIFEST"
    assert by_component["internal_manifest_consistency"].status.value == "SKIPPED"
    assert by_component["uv_lock_binding"].status.value == "OK"


# ---------------------------------------------------------------------
# Blocker 1 — full internal manifest consistency (issue #35 PR1
# corrective review): metamorphic regressions proving that changing ONLY
# one critical field of the *embedded* manifest is caught, not just the
# previous release_id/release_version/source_sha identity subset.
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,override",
    [
        ("uv_lock_sha256", "c" * 64),
        ("alembic_heads", ["deadbeefcafe"]),
        ("rollback_compatibility", "APP_ONLY"),
        (
            "model_manifest",
            {"reference": "docs/DECISIONS.md#D-999", "status": "DEVELOPMENT_INTEGRATION"},
        ),
        ("required_python_version", ">=3.13"),
        ("artifact_format", "zip"),
        ("artifact_format_version", 2),
    ],
)
def test_internal_manifest_single_field_mismatch_fails(
    tmp_path: Path, field: str, override: object
) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    mismatched_internal = _manifest_dict(**{field: override})
    artifact_path = _build_artifact(tmp_path, embed_manifest=mismatched_internal)
    sums_path = _write_sha256sums(tmp_path, artifact_path)

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is False
    by_component = {f.component: f for f in result.findings}
    assert by_component["internal_manifest_consistency"].code == "INTERNAL_MANIFEST_MISMATCH", (
        f"field {field!r} mismatch was not caught"
    )


def test_internal_manifest_matching_on_only_identity_fields_still_fails(tmp_path: Path) -> None:
    """The previous (defective) behaviour only compared release_id/
    release_version/source_sha. Prove a manifest that agrees on exactly
    those three but differs elsewhere is still a hard failure."""
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    mismatched_internal = _manifest_dict(rollback_compatibility="PROHIBITED_PENDING_PROCEDURE")
    assert mismatched_internal["release_id"] == RELEASE_ID
    assert mismatched_internal["release_version"] == RELEASE_VERSION
    assert mismatched_internal["source_sha"] == VALID_SHA
    artifact_path = _build_artifact(tmp_path, embed_manifest=mismatched_internal)
    sums_path = _write_sha256sums(tmp_path, artifact_path)

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is False
    by_component = {f.component: f for f in result.findings}
    assert by_component["internal_manifest_consistency"].code == "INTERNAL_MANIFEST_MISMATCH"


def test_internal_manifest_too_large_fails(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    oversized_payload = b"{" + b" " * (_MAX_EMBEDDED_MANIFEST_SIZE + 1) + b"}"
    artifact_path = tmp_path / "meyar-release.tar.gz"
    with tarfile.open(artifact_path, mode="w:gz") as tf:
        _add_file_member(tf, f"{RELEASE_ID}/release_manifest.json", oversized_payload)
        _add_file_member(tf, f"{RELEASE_ID}/backend/uv.lock", UV_LOCK_CONTENT)
        _add_file_member(tf, f"{RELEASE_ID}/app/main.py", b"hello")
    sums_path = _write_sha256sums(tmp_path, artifact_path)

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is False
    by_component = {f.component: f for f in result.findings}
    assert by_component["internal_manifest_consistency"].code == "INTERNAL_MANIFEST_TOO_LARGE"


# ---------------------------------------------------------------------
# Blocker 2 — bind uv_lock_sha256 to the actual artifact.
# ---------------------------------------------------------------------


def test_uv_lock_wrong_digest_fails(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    artifact_path = _build_artifact(tmp_path, uv_lock=b"a completely different uv.lock body\n")
    sums_path = _write_sha256sums(tmp_path, artifact_path)

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is False
    by_component = {f.component: f for f in result.findings}
    assert by_component["uv_lock_binding"].code == "UV_LOCK_SHA256_MISMATCH"


def test_uv_lock_missing_member_fails(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    artifact_path = _build_artifact(tmp_path, uv_lock=None)
    sums_path = _write_sha256sums(tmp_path, artifact_path)

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is False
    by_component = {f.component: f for f in result.findings}
    assert by_component["uv_lock_binding"].code == "UV_LOCK_MEMBER_MISSING"


def test_uv_lock_duplicate_member_is_ambiguous_fail(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    artifact_path = _build_artifact(tmp_path, duplicate_uv_lock=True)
    sums_path = _write_sha256sums(tmp_path, artifact_path)

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is False
    by_component = {f.component: f for f in result.findings}
    assert by_component["uv_lock_binding"].code == "UV_LOCK_MEMBER_AMBIGUOUS"


def test_uv_lock_non_regular_member_fails(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    artifact_path = _build_artifact(tmp_path, uv_lock_as_directory=True)
    sums_path = _write_sha256sums(tmp_path, artifact_path)

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is False
    by_component = {f.component: f for f in result.findings}
    assert by_component["uv_lock_binding"].code == "UV_LOCK_MEMBER_NOT_REGULAR_FILE"


def test_uv_lock_correct_digest_passes(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    artifact_path = _build_artifact(tmp_path)
    sums_path = _write_sha256sums(tmp_path, artifact_path, manifest_path)

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    by_component = {f.component: f for f in result.findings}
    assert by_component["uv_lock_binding"].code == "UV_LOCK_SHA256_MATCHES"
    assert result.ok is True


def test_uv_lock_too_large_fails(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    oversized = b"x" * (_MAX_UV_LOCK_SIZE + 1)
    digest = hashlib.sha256(oversized).hexdigest()
    _write_manifest(manifest_path, uv_lock_sha256=digest)
    artifact_path = _build_artifact(tmp_path, uv_lock=oversized)
    sums_path = _write_sha256sums(tmp_path, artifact_path)

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is False
    by_component = {f.component: f for f in result.findings}
    assert by_component["uv_lock_binding"].code == "UV_LOCK_MEMBER_TOO_LARGE"


# ---------------------------------------------------------------------
# Corrective review (PR #52) — bind the external release manifest itself
# to SHA256SUMS, so it is no longer the sole unverified authority for
# source_sha/release_id/rollback_compatibility/Alembic heads/model
# reference/Python requirement/artifact format.
# ---------------------------------------------------------------------


def test_manifest_checksum_missing_entry_fails(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    artifact_path = _build_artifact(tmp_path)
    # Artifact entry only — no manifest entry at all.
    sums_path = _write_sha256sums(tmp_path, artifact_path, manifest_path=None)

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is False
    by_component = {f.component: f for f in result.findings}
    assert by_component["manifest_checksum"].code == "MANIFEST_CHECKSUM_ENTRY_MISSING"
    assert by_component["artifact_checksum"].status.value == "OK"
    assert by_component["release_bundle_integrity"].code == "RELEASE_BUNDLE_INTEGRITY_FAILED"


def test_manifest_checksum_duplicate_entry_is_ambiguous_fail(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    artifact_path = _build_artifact(tmp_path)
    artifact_digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    sums_path = tmp_path / "SHA256SUMS"
    sums_path.write_text(
        f"{artifact_digest}  {artifact_path.name}\n"
        f"{manifest_digest}  {manifest_path.name}\n"
        f"{'0' * 64}  {manifest_path.name}\n"
    )

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is False
    by_component = {f.component: f for f in result.findings}
    assert by_component["manifest_checksum"].code == "MANIFEST_CHECKSUM_ENTRY_AMBIGUOUS"
    assert by_component["release_bundle_integrity"].code == "RELEASE_BUNDLE_INTEGRITY_FAILED"


def test_manifest_checksum_mismatch_fails(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    artifact_path = _build_artifact(tmp_path)
    sums_path = _write_sha256sums(
        tmp_path, artifact_path, manifest_path, manifest_digest="0" * 64
    )

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is False
    by_component = {f.component: f for f in result.findings}
    assert by_component["manifest_checksum"].code == "MANIFEST_CHECKSUM_MISMATCH"
    assert by_component["release_bundle_integrity"].code == "RELEASE_BUNDLE_INTEGRITY_FAILED"


def test_artifact_checksum_valid_but_manifest_checksum_invalid_is_overall_fail(
    tmp_path: Path,
) -> None:
    """The release bundle is artifact + manifest + SHA256SUMS together —
    a valid artifact checksum alone must never report overall success."""
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    artifact_path = _build_artifact(tmp_path)
    sums_path = _write_sha256sums(
        tmp_path, artifact_path, manifest_path, manifest_digest="f" * 64
    )

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is False
    by_component = {f.component: f for f in result.findings}
    assert by_component["artifact_checksum"].status.value == "OK"
    assert by_component["manifest_checksum"].code == "MANIFEST_CHECKSUM_MISMATCH"
    assert by_component["release_bundle_integrity"].status.value == "FAIL"


def test_artifact_and_manifest_checksums_both_correct_passes(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    artifact_path = _build_artifact(tmp_path)
    sums_path = _write_sha256sums(tmp_path, artifact_path, manifest_path)

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is True
    by_component = {f.component: f for f in result.findings}
    assert by_component["artifact_checksum"].code == "CHECKSUM_MATCHES"
    assert by_component["manifest_checksum"].code == "MANIFEST_CHECKSUM_MATCHES"
    assert by_component["release_bundle_integrity"].code == "RELEASE_BUNDLE_INTEGRITY_OK"


def test_oversized_external_manifest_fails_before_unbounded_read(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    oversized = b"{" + b" " * (_MAX_EXTERNAL_MANIFEST_SIZE + 1) + b"}"
    manifest_path.write_bytes(oversized)
    artifact_path = _build_artifact(tmp_path)
    sums_path = _write_sha256sums(tmp_path, artifact_path, manifest_path=None)

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is False
    by_component = {f.component: f for f in result.findings}
    assert by_component["manifest_schema"].code == "MANIFEST_TOO_LARGE"


def test_oversized_sha256sums_fails_before_unbounded_read(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    artifact_path = _build_artifact(tmp_path)
    sums_path = tmp_path / "SHA256SUMS"
    sums_path.write_bytes(b"#" + b" " * (_MAX_SHA256SUMS_SIZE + 1) + b"\n")

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is False
    by_component = {f.component: f for f in result.findings}
    assert by_component["sha256sums_format"].code == "SHA256SUMS_TOO_LARGE"


# ---------------------------------------------------------------------
# Corrective review (PR #52) — embedded manifest ambiguity: a duplicate
# `release_id/release_manifest.json` member must never be silently
# resolved (TarFile.getmember() is last-write-wins); a non-regular member
# must be rejected truthfully.
# ---------------------------------------------------------------------


def test_duplicate_embedded_manifest_is_ambiguous_fail(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    payload = json.dumps(_manifest_dict()).encode()
    artifact_path = tmp_path / "meyar-release.tar.gz"
    with tarfile.open(artifact_path, mode="w:gz") as tf:
        _add_file_member(tf, f"{RELEASE_ID}/release_manifest.json", payload)
        _add_file_member(tf, f"{RELEASE_ID}/release_manifest.json", payload)
        _add_file_member(tf, f"{RELEASE_ID}/backend/uv.lock", UV_LOCK_CONTENT)
        _add_file_member(tf, f"{RELEASE_ID}/app/main.py", b"hello")
    sums_path = _write_sha256sums(tmp_path, artifact_path, manifest_path)

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is False
    by_component = {f.component: f for f in result.findings}
    assert by_component["internal_manifest_consistency"].code == "INTERNAL_MANIFEST_AMBIGUOUS"


def test_embedded_manifest_as_directory_is_rejected(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    artifact_path = tmp_path / "meyar-release.tar.gz"
    with tarfile.open(artifact_path, mode="w:gz") as tf:
        info = tarfile.TarInfo(name=f"{RELEASE_ID}/release_manifest.json")
        info.type = tarfile.DIRTYPE
        tf.addfile(info)
        _add_file_member(tf, f"{RELEASE_ID}/backend/uv.lock", UV_LOCK_CONTENT)
        _add_file_member(tf, f"{RELEASE_ID}/app/main.py", b"hello")
    sums_path = _write_sha256sums(tmp_path, artifact_path, manifest_path)

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is False
    by_component = {f.component: f for f in result.findings}
    assert (
        by_component["internal_manifest_consistency"].code
        == "INTERNAL_MANIFEST_NOT_REGULAR_FILE"
    )


def test_single_valid_embedded_manifest_still_passes(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    artifact_path = _build_artifact(tmp_path)
    sums_path = _write_sha256sums(tmp_path, artifact_path, manifest_path)

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is True
    by_component = {f.component: f for f in result.findings}
    assert by_component["internal_manifest_consistency"].code == "INTERNAL_MANIFEST_CONSISTENT"


# ---------------------------------------------------------------------
# Blocker 5 — bounded release-archive inspection.
# ---------------------------------------------------------------------


def test_excessive_member_count_fails(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    extra_members = {
        f"{RELEASE_ID}/app/file-{i}.txt": b"x" for i in range(MAX_ARCHIVE_MEMBER_COUNT + 1)
    }
    artifact_path = _build_artifact(
        tmp_path, embed_manifest=False, uv_lock=None, extra_members=extra_members
    )
    sums_path = _write_sha256sums(tmp_path, artifact_path)

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is False
    by_component = {f.component: f for f in result.findings}
    assert by_component["archive_safety"].code == "ARCHIVE_RESOURCE_BOUND_EXCEEDED"


def test_excessive_declared_aggregate_size_fails(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    # A header can declare a huge size without that many bytes actually
    # following — the bound check reads only header metadata, never
    # member content, so a raw header-only file (no real gzip stream) is
    # enough to prove it never tries to read that far.
    artifact_path = tmp_path / "meyar-release.tar"
    info = tarfile.TarInfo(name=f"{RELEASE_ID}/app/huge.bin")
    info.size = 2 * 1024 * 1024 * 1024  # 2 GiB declared, 0 bytes actually written
    artifact_path.write_bytes(info.tobuf(format=tarfile.GNU_FORMAT))
    sums_path = _write_sha256sums(tmp_path, artifact_path)

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is False
    by_component = {f.component: f for f in result.findings}
    assert by_component["archive_safety"].code == "ARCHIVE_RESOURCE_BOUND_EXCEEDED"


def test_parse_sha256sums_ignores_malformed_lines() -> None:
    text = "\n".join(
        [
            "# a comment",
            f"{'a' * 64}  good-file.tar.gz",
            "not-a-valid-line",
            f"{'b' * 10}  too-short-digest.tar.gz",
            "",
        ]
    )
    parsed = parse_sha256sums(text)
    assert parsed.digests == {"good-file.tar.gz": "a" * 64}
    assert parsed.ambiguous_filenames == frozenset()


def test_parse_sha256sums_flags_duplicate_filename_as_ambiguous() -> None:
    text = "\n".join(
        [
            f"{'a' * 64}  dup-file.tar.gz",
            f"{'b' * 64}  dup-file.tar.gz",
            f"{'c' * 64}  unique-file.tar.gz",
        ]
    )
    parsed = parse_sha256sums(text)
    assert "dup-file.tar.gz" not in parsed.digests
    assert parsed.ambiguous_filenames == {"dup-file.tar.gz"}
    assert parsed.digests == {"unique-file.tar.gz": "c" * 64}


def test_command_never_extracts_to_disk(tmp_path: Path, monkeypatch) -> None:
    """Defense-in-depth: TarFile.extractall must never be called anywhere
    in the verify-release path."""
    import tarfile as tarfile_module

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("verify-release must never call TarFile.extractall")

    monkeypatch.setattr(tarfile_module.TarFile, "extractall", _forbidden)
    monkeypatch.setattr(tarfile_module.TarFile, "extract", _forbidden)

    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    artifact_path = _build_artifact(tmp_path)
    sums_path = _write_sha256sums(tmp_path, artifact_path, manifest_path)

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is True
