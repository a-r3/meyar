"""`meyar-ops verify-release` (issue #35 PR1 §10/§12). All fixtures are
built synthetically under tmp_path — no artifact/manifest is ever
extracted to disk by these tests or by the code under test."""

import hashlib
import io
import json
import tarfile
from pathlib import Path

from meyar.ops.release_manifest import compute_release_id
from meyar.ops.verify_release import parse_sha256sums, verify_release

VALID_SHA = "a" * 40
VALID_LOCK_SHA = "b" * 64
RELEASE_VERSION = "0.1.0"
RELEASE_ID = compute_release_id(release_version=RELEASE_VERSION, source_sha=VALID_SHA)


def _manifest_dict(**overrides: object) -> dict:
    base = {
        "manifest_schema_version": 1,
        "release_version": RELEASE_VERSION,
        "release_id": RELEASE_ID,
        "source_sha": VALID_SHA,
        "built_at": "2026-09-23T00:00:00Z",
        "required_python_version": ">=3.12",
        "uv_lock_sha256": VALID_LOCK_SHA,
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


def _build_artifact(
    tmp_path: Path,
    *,
    root: str = RELEASE_ID,
    embed_manifest: dict | None | bool = True,
    extra_members: dict[str, bytes] | None = None,
) -> Path:
    """embed_manifest: True embeds `_manifest_dict()`, a dict embeds that
    exact dict, False/None embeds nothing."""
    artifact_path = tmp_path / "meyar-release.tar.gz"
    with tarfile.open(artifact_path, mode="w:gz") as tf:
        if embed_manifest:
            payload = json.dumps(
                embed_manifest if isinstance(embed_manifest, dict) else _manifest_dict()
            ).encode()
            info = tarfile.TarInfo(name=f"{root}/release_manifest.json")
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
        for name, data in (extra_members or {f"{root}/app/main.py": b"hello"}).items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return artifact_path


def _write_sha256sums(tmp_path: Path, artifact_path: Path, *, digest: str | None = None) -> Path:
    actual = digest or hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    sums_path = tmp_path / "SHA256SUMS"
    sums_path.write_text(f"{actual}  {artifact_path.name}\n")
    return sums_path


def test_fully_valid_release_verifies_ok(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    artifact_path = _build_artifact(tmp_path)
    sums_path = _write_sha256sums(tmp_path, artifact_path)

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is True
    codes = {f.component: f.status.value for f in result.findings}
    assert codes["manifest_schema"] == "OK"
    assert codes["release_identity"] == "OK"
    assert codes["artifact_checksum"] == "OK"
    assert codes["archive_safety"] == "OK"
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


def test_unsafe_archive_absolute_path_rejected(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    artifact_path = _build_artifact(
        tmp_path, embed_manifest=False, extra_members={"/etc/passwd": b"evil"}
    )
    sums_path = _write_sha256sums(tmp_path, artifact_path)

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is False
    by_component = {f.component: f for f in result.findings}
    assert by_component["archive_safety"].code == "UNSAFE_ARCHIVE_MEMBER"
    assert by_component["internal_manifest_consistency"].status.value == "SKIPPED"


def test_unsafe_archive_traversal_rejected(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    artifact_path = _build_artifact(
        tmp_path,
        embed_manifest=False,
        extra_members={f"{RELEASE_ID}/../../escape.txt": b"evil"},
    )
    sums_path = _write_sha256sums(tmp_path, artifact_path)

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is False
    by_component = {f.component: f for f in result.findings}
    assert by_component["archive_safety"].code == "UNSAFE_ARCHIVE_MEMBER"


def test_internal_manifest_mismatch_fails(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    mismatched_internal = _manifest_dict(release_version="9.9.9")
    artifact_path = _build_artifact(tmp_path, embed_manifest=mismatched_internal)
    sums_path = _write_sha256sums(tmp_path, artifact_path)

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is False
    by_component = {f.component: f for f in result.findings}
    assert by_component["internal_manifest_consistency"].code == "INTERNAL_MANIFEST_MISMATCH"


def test_no_internal_manifest_is_skipped_not_failed(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    artifact_path = _build_artifact(tmp_path, embed_manifest=False)
    sums_path = _write_sha256sums(tmp_path, artifact_path)

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is True
    by_component = {f.component: f for f in result.findings}
    assert by_component["internal_manifest_consistency"].code == "NO_INTERNAL_MANIFEST"
    assert by_component["internal_manifest_consistency"].status.value == "SKIPPED"


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
    assert parsed == {"good-file.tar.gz": "a" * 64}


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
    sums_path = _write_sha256sums(tmp_path, artifact_path)

    result = verify_release(
        manifest_path=manifest_path, sha256sums_path=sums_path, artifact_path=artifact_path
    )
    assert result.ok is True
