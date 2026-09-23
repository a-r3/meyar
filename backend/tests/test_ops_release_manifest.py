"""Release-manifest schema, identity determinism, and pyproject version
reading (issue #35 PR1 §7)."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from meyar.ops.model_manifest import ModelApprovalStatus
from meyar.ops.release_manifest import (
    ModelManifestReference,
    ReleaseManifest,
    RollbackCompatibility,
    compute_release_id,
    read_project_version,
)

VALID_SHA = "a" * 40
VALID_LOCK_SHA = "b" * 64


def _valid_manifest_kwargs(**overrides: object) -> dict:
    base = dict(
        release_version="0.1.0",
        release_id=compute_release_id(release_version="0.1.0", source_sha=VALID_SHA),
        source_sha=VALID_SHA,
        built_at="2026-09-23T00:00:00Z",
        required_python_version=">=3.12",
        uv_lock_sha256=VALID_LOCK_SHA,
        alembic_heads=["6f4c2a9d8e10"],
        rollback_compatibility=RollbackCompatibility.BACKUP_RESTORE_REQUIRED,
        model_manifest=ModelManifestReference(
            reference="docs/DECISIONS.md#D-066",
            status=ModelApprovalStatus.DEVELOPMENT_INTEGRATION,
        ),
        artifact_format="tar.gz",
        artifact_format_version=1,
    )
    base.update(overrides)
    return base


def test_valid_release_manifest_round_trips() -> None:
    manifest = ReleaseManifest(**_valid_manifest_kwargs())
    reparsed = ReleaseManifest.model_validate_json(manifest.model_dump_json())
    assert reparsed == manifest


def test_release_manifest_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ReleaseManifest(**_valid_manifest_kwargs(), unexpected_field="nope")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad_sha",
    [
        "too-short",
        "g" * 40,  # not hex
        "A" * 40,  # uppercase not accepted
        VALID_SHA + "a",  # 41 chars
        "",
    ],
)
def test_invalid_source_sha_rejected(bad_sha: str) -> None:
    with pytest.raises(ValidationError):
        ReleaseManifest(**_valid_manifest_kwargs(source_sha=bad_sha))


def test_invalid_uv_lock_sha256_rejected() -> None:
    with pytest.raises(ValidationError):
        ReleaseManifest(**_valid_manifest_kwargs(uv_lock_sha256="not-a-digest"))


def test_invalid_rollback_compatibility_rejected() -> None:
    with pytest.raises(ValidationError):
        ReleaseManifest(**_valid_manifest_kwargs(rollback_compatibility="YOLO_ROLLBACK"))


def test_rollback_compatibility_supports_all_four_required_values() -> None:
    values = {c.value for c in RollbackCompatibility}
    assert values == {
        "APP_ONLY",
        "FORWARD_COMPATIBLE_SCHEMA",
        "BACKUP_RESTORE_REQUIRED",
        "PROHIBITED_PENDING_PROCEDURE",
    }


def test_alembic_heads_must_be_non_empty() -> None:
    with pytest.raises(ValidationError):
        ReleaseManifest(**_valid_manifest_kwargs(alembic_heads=[]))


def test_compute_release_id_is_deterministic() -> None:
    a = compute_release_id(release_version="0.1.0", source_sha=VALID_SHA)
    b = compute_release_id(release_version="0.1.0", source_sha=VALID_SHA)
    assert a == b
    assert a.startswith("meyar-0.1.0+")


def test_compute_release_id_changes_with_source_sha() -> None:
    a = compute_release_id(release_version="0.1.0", source_sha=VALID_SHA)
    b = compute_release_id(release_version="0.1.0", source_sha="c" * 40)
    assert a != b


def test_compute_release_id_rejects_invalid_sha() -> None:
    with pytest.raises(ValueError, match="source_sha"):
        compute_release_id(release_version="0.1.0", source_sha="short")


def test_read_project_version_reads_pyproject(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "meyar"\nversion = "9.9.9"\n')
    assert read_project_version(pyproject) == "9.9.9"


def test_read_project_version_matches_real_backend_pyproject() -> None:
    """This PR does not change pyproject.toml's version — pin that
    invariant so a future accidental bump is caught here too."""
    repo_pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    assert read_project_version(repo_pyproject) == "0.1.0"


def test_read_project_version_missing_version_raises(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "meyar"\n')
    with pytest.raises(ValueError, match="version"):
        read_project_version(pyproject)
