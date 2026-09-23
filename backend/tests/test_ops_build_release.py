"""`meyar-ops build-release` (issue #35 PR3). Every fixture is a real,
throwaway Git repository created under `tmp_path` — the point of this
module is proving the builder reads from Git commit objects, never the
mutable working tree, so a synthetic in-memory fixture would defeat the
purpose. No real MEYAR repository history is ever touched."""

from __future__ import annotations

import os
import subprocess
import tarfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from meyar.ops.build_release import (
    ALLOWED_PATHSPECS,
    BuildReleaseRequest,
    _cleanup_created_file,
    _create_exclusive,
    build_release,
    validate_source_sha,
)
from meyar.ops.model_manifest import ModelApprovalStatus
from meyar.ops.release_manifest import ReleaseManifest, RollbackCompatibility, compute_release_id
from meyar.ops.result import FindingStatus
from meyar.ops.verify_release import verify_release

# --- fixture repo construction -------------------------------------------------

ALEMBIC_INI = "[alembic]\nscript_location = %(here)s/alembic\n"
UV_LOCK_CONTENT = b"# fixture uv.lock\nversion = 1\n"


def _run_git(argv: list[str], *, cwd: Path) -> str:
    result = subprocess.run(["git", *argv], cwd=cwd, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _init_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _run_git(["init", "-q"], cwd=repo)
    _run_git(["config", "user.email", "test@example.com"], cwd=repo)
    _run_git(["config", "user.name", "Test"], cwd=repo)
    return repo


def _migration_script(revision: str, down_revision: str | None) -> str:
    return (
        '"""fixture migration"""\n'
        f"revision = {revision!r}\n"
        f"down_revision = {down_revision!r}\n"
        "branch_labels = None\ndepends_on = None\n"
    )


def _write_fixture_repo_files(
    repo: Path,
    *,
    version: str = "1.0.0",
    requires_python: str = ">=3.12",
    include_disallowed: bool = True,
) -> None:
    meyar_dir = repo / "backend" / "src" / "meyar"
    meyar_dir.mkdir(parents=True, exist_ok=True)
    (meyar_dir / "main.py").write_text("APP = 'meyar'\n")
    (repo / "backend" / "pyproject.toml").write_text(
        f'[project]\nname = "meyar"\nversion = "{version}"\nrequires-python = "{requires_python}"\n'
    )
    (repo / "backend" / "uv.lock").write_bytes(UV_LOCK_CONTENT)
    (repo / "backend" / "alembic.ini").write_text(ALEMBIC_INI)
    (repo / "backend" / "alembic" / "versions").mkdir(parents=True, exist_ok=True)
    (repo / "backend" / "alembic" / "env.py").write_text("# minimal fixture env\n")
    (repo / "backend" / "alembic" / "versions" / "0001_initial.py").write_text(
        _migration_script("0001initial", None)
    )
    if include_disallowed:
        (repo / "backend" / "tests").mkdir(parents=True, exist_ok=True)
        (repo / "backend" / "tests" / "test_x.py").write_text("def test_x(): pass\n")
        (repo / "backend" / "scripts").mkdir(parents=True, exist_ok=True)
        (repo / "backend" / "scripts" / "run.sh").write_text("#!/bin/sh\necho hi\n")
        (repo / "backend" / ".env").write_text("SECRET=hunter2\n")


def _commit_all(repo: Path, message: str = "fixture commit") -> str:
    _run_git(["add", "-A"], cwd=repo)
    _run_git(["commit", "-q", "-m", message], cwd=repo)
    return _run_git(["rev-parse", "HEAD"], cwd=repo).strip()


def _fixed_clock(dt: datetime) -> Callable[[], datetime]:
    return lambda: dt


FIXED_BUILT_AT = datetime(2026, 1, 1, tzinfo=UTC)


def _default_request(
    *, source_sha: str, output_dir: Path, **overrides: object
) -> BuildReleaseRequest:
    base: dict[str, object] = dict(
        source_sha=source_sha,
        output_dir=output_dir,
        rollback_compatibility=RollbackCompatibility.BACKUP_RESTORE_REQUIRED,
        model_manifest_reference="docs/DECISIONS.md#D-068",
        model_approval_status=ModelApprovalStatus.DEVELOPMENT_INTEGRATION,
    )
    base.update(overrides)
    return BuildReleaseRequest(**base)  # type: ignore[arg-type]


def _finding(result, component: str):
    return next(f for f in result.findings if f.component == component)


def _tar_members(archive_path: Path) -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    with tarfile.open(archive_path, mode="r:gz") as tar:
        for info in tar.getmembers():
            extracted = tar.extractfile(info)
            members[info.name] = extracted.read() if extracted is not None else b""
    return members


# --- A. provenance --------------------------------------------------------------


@pytest.mark.parametrize("bad_sha", ["short", "g" * 40, "A" * 40, "a" * 41, ""])
def test_validate_source_sha_rejects_non_full_hex(bad_sha: str) -> None:
    with pytest.raises(ValueError, match="source_sha"):
        validate_source_sha(bad_sha)


def test_build_release_rejects_short_sha(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)
    _commit_all(repo)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    result = build_release(
        _default_request(source_sha="deadbeef", output_dir=output_dir), invocation_cwd=repo
    )
    assert result.ok is False
    assert _finding(result, "source_sha_format").code == "SOURCE_SHA_INVALID_FORMAT"
    assert list(output_dir.iterdir()) == []


def test_build_release_rejects_nonexistent_commit(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)
    _commit_all(repo)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    result = build_release(
        _default_request(source_sha="a" * 40, output_dir=output_dir), invocation_cwd=repo
    )
    assert result.ok is False
    assert _finding(result, "commit_exists").code == "SOURCE_COMMIT_NOT_FOUND"
    assert list(output_dir.iterdir()) == []


def test_build_release_rejects_non_commit_object(tmp_path: Path) -> None:
    """A well-formed, existing blob SHA is not a commit — must be rejected,
    not silently treated as one."""
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)
    _commit_all(repo)
    blob_sha = _run_git(["rev-parse", "HEAD:backend/pyproject.toml"], cwd=repo).strip()
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    result = build_release(
        _default_request(source_sha=blob_sha, output_dir=output_dir), invocation_cwd=repo
    )
    assert result.ok is False
    assert _finding(result, "commit_exists").code == "SOURCE_COMMIT_NOT_FOUND"


def test_artifact_bytes_come_from_selected_commit(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)
    sha = _commit_all(repo)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    result = build_release(
        _default_request(source_sha=sha, output_dir=output_dir),
        invocation_cwd=repo,
        clock=_fixed_clock(FIXED_BUILT_AT),
    )
    assert result.ok is True, result.model_dump_json()
    artifact = next(output_dir.glob("*.tar.gz"))
    members = _tar_members(artifact)
    release_id = compute_release_id(release_version="1.0.0", source_sha=sha)
    assert members[f"{release_id}/backend/src/meyar/main.py"] == b"APP = 'meyar'\n"


def test_dirty_tracked_file_modification_does_not_affect_artifact(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)
    sha = _commit_all(repo)

    # Modify a tracked, allowlisted file WITHOUT committing.
    (repo / "backend" / "src" / "meyar" / "main.py").write_text("MODIFIED = True\n")

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    result = build_release(
        _default_request(source_sha=sha, output_dir=output_dir),
        invocation_cwd=repo,
        clock=_fixed_clock(FIXED_BUILT_AT),
    )
    assert result.ok is True, result.model_dump_json()
    artifact = next(output_dir.glob("*.tar.gz"))
    members = _tar_members(artifact)
    release_id = compute_release_id(release_version="1.0.0", source_sha=sha)
    # The committed bytes, never the dirty working-tree bytes.
    assert members[f"{release_id}/backend/src/meyar/main.py"] == b"APP = 'meyar'\n"


def test_untracked_allowlist_shaped_file_does_not_enter_artifact(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)
    sha = _commit_all(repo)

    # Untracked file living directly under an allowed-looking source dir.
    (repo / "backend" / "src" / "meyar" / "untracked_new.py").write_text("SHOULD_NOT_SHIP = 1\n")

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    result = build_release(
        _default_request(source_sha=sha, output_dir=output_dir),
        invocation_cwd=repo,
        clock=_fixed_clock(FIXED_BUILT_AT),
    )
    assert result.ok is True, result.model_dump_json()
    artifact = next(output_dir.glob("*.tar.gz"))
    members = _tar_members(artifact)
    assert not any(name.endswith("untracked_new.py") for name in members)


def test_selected_commit_pyproject_version_and_python_are_used(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo, version="2.5.1", requires_python=">=3.13")
    sha = _commit_all(repo)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    result = build_release(
        _default_request(source_sha=sha, output_dir=output_dir),
        invocation_cwd=repo,
        clock=_fixed_clock(FIXED_BUILT_AT),
    )
    assert result.ok is True, result.model_dump_json()
    manifest_path = next(output_dir.glob("*.release-manifest.json"))
    manifest = ReleaseManifest.model_validate_json(manifest_path.read_text())
    assert manifest.release_version == "2.5.1"
    assert manifest.required_python_version == ">=3.13"
    assert manifest.release_id == compute_release_id(release_version="2.5.1", source_sha=sha)


def test_selected_commit_uv_lock_digest_is_used(tmp_path: Path) -> None:
    import hashlib

    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)
    (repo / "backend" / "uv.lock").write_bytes(b"different lock content\n")
    sha = _commit_all(repo)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    result = build_release(
        _default_request(source_sha=sha, output_dir=output_dir),
        invocation_cwd=repo,
        clock=_fixed_clock(FIXED_BUILT_AT),
    )
    assert result.ok is True, result.model_dump_json()
    manifest_path = next(output_dir.glob("*.release-manifest.json"))
    manifest = ReleaseManifest.model_validate_json(manifest_path.read_text())
    assert manifest.uv_lock_sha256 == hashlib.sha256(b"different lock content\n").hexdigest()


def test_required_release_file_missing_fails_truthfully(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)
    (repo / "backend" / "pyproject.toml").unlink()
    sha = _commit_all(repo)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    result = build_release(
        _default_request(source_sha=sha, output_dir=output_dir), invocation_cwd=repo
    )
    assert result.ok is False
    assert _finding(result, "metadata_extraction").code == "REQUIRED_RELEASE_FILE_MISSING"
    assert list(output_dir.iterdir()) == []


def test_git_command_infrastructure_failure_is_reported_truthfully(tmp_path: Path) -> None:
    """A Git-runner-level failure (binary missing/timeout) is normalized to
    a truthful Finding, never an uncaught exception escaping build_release."""
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)
    _commit_all(repo)
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    def _broken_runner(argv, *, cwd):  # noqa: ANN001, ARG001
        raise FileNotFoundError("git not found")

    result = build_release(
        _default_request(source_sha="a" * 40, output_dir=output_dir),
        invocation_cwd=repo,
        runner=_broken_runner,
    )
    assert result.ok is False
    assert _finding(result, "repo_root_discovery").code == "GIT_REPO_ROOT_NOT_FOUND"


# --- B. allowlist -----------------------------------------------------------------


def test_exact_allowed_source_families_included_others_excluded(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo, include_disallowed=True)
    sha = _commit_all(repo)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    result = build_release(
        _default_request(source_sha=sha, output_dir=output_dir),
        invocation_cwd=repo,
        clock=_fixed_clock(FIXED_BUILT_AT),
    )
    assert result.ok is True, result.model_dump_json()
    artifact = next(output_dir.glob("*.tar.gz"))
    members = set(_tar_members(artifact))
    release_id = compute_release_id(release_version="1.0.0", source_sha=sha)

    assert f"{release_id}/backend/src/meyar/main.py" in members
    assert f"{release_id}/backend/pyproject.toml" in members
    assert f"{release_id}/backend/uv.lock" in members
    assert f"{release_id}/backend/alembic.ini" in members
    assert f"{release_id}/backend/alembic/versions/0001_initial.py" in members
    assert f"{release_id}/release_manifest.json" in members

    assert not any("/backend/tests/" in name for name in members)
    assert not any("/backend/scripts/" in name for name in members)
    assert not any(name.endswith("/.env") for name in members)
    assert not any(".git" in name for name in members)


def test_allowed_pathspecs_are_exactly_the_documented_five() -> None:
    assert ALLOWED_PATHSPECS == (
        "backend/src/meyar",
        "backend/pyproject.toml",
        "backend/uv.lock",
        "backend/alembic.ini",
        "backend/alembic",
    )


def test_symlink_git_entry_rejected(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)
    link_target = repo / "backend" / "src" / "meyar" / "real_target.py"
    link_target.write_text("x = 1\n")
    link_path = repo / "backend" / "src" / "meyar" / "linked.py"
    link_path.symlink_to("real_target.py")
    sha = _commit_all(repo)

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    result = build_release(
        _default_request(source_sha=sha, output_dir=output_dir), invocation_cwd=repo
    )
    assert result.ok is False
    assert _finding(result, "tree_entries_safety").code == "UNSAFE_GIT_TREE_ENTRY"
    assert list(output_dir.iterdir()) == []


def test_submodule_entry_rejected(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)
    submodule_repo = _init_repo(tmp_path, name="submodule-source")
    (submodule_repo / "f.txt").write_text("x\n")
    _commit_all(submodule_repo)
    _run_git(
        [
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(submodule_repo),
            "backend/src/meyar/vendored",
        ],
        cwd=repo,
    )
    sha = _commit_all(repo)

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    result = build_release(
        _default_request(source_sha=sha, output_dir=output_dir), invocation_cwd=repo
    )
    assert result.ok is False
    assert _finding(result, "tree_entries_safety").code == "UNSAFE_GIT_TREE_ENTRY"
    assert list(output_dir.iterdir()) == []


# --- C. archive ---------------------------------------------------------------


def test_single_release_id_root_and_embedded_manifest_mandatory(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)
    sha = _commit_all(repo)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    result = build_release(
        _default_request(source_sha=sha, output_dir=output_dir),
        invocation_cwd=repo,
        clock=_fixed_clock(FIXED_BUILT_AT),
    )
    assert result.ok is True, result.model_dump_json()
    artifact = next(output_dir.glob("*.tar.gz"))
    release_id = compute_release_id(release_version="1.0.0", source_sha=sha)
    with tarfile.open(artifact, mode="r:gz") as tar:
        names = tar.getnames()
    roots = {name.split("/", 1)[0] for name in names}
    assert roots == {release_id}
    assert f"{release_id}/release_manifest.json" in names


def test_member_order_and_metadata_are_normalized(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)
    sha = _commit_all(repo)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    result = build_release(
        _default_request(source_sha=sha, output_dir=output_dir),
        invocation_cwd=repo,
        clock=_fixed_clock(FIXED_BUILT_AT),
    )
    assert result.ok is True, result.model_dump_json()
    artifact = next(output_dir.glob("*.tar.gz"))
    with tarfile.open(artifact, mode="r:gz") as tar:
        members = tar.getmembers()
    names = [m.name for m in members]
    assert names == sorted(names)
    for m in members:
        assert m.uid == 0
        assert m.gid == 0
        assert m.uname == ""
        assert m.gname == ""
        assert m.mtime == 0
        assert m.mode == 0o644
        assert m.isfile()


def test_archive_bound_exceeded_during_self_check_fails_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)
    sha = _commit_all(repo)
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    monkeypatch.setattr("meyar.ops.archive_safety.MAX_ARCHIVE_MEMBER_COUNT", 1)

    result = build_release(
        _default_request(source_sha=sha, output_dir=output_dir),
        invocation_cwd=repo,
        clock=_fixed_clock(FIXED_BUILT_AT),
    )
    assert result.ok is False
    assert _finding(result, "archive_self_check").code == "ARCHIVE_RESOURCE_BOUND_EXCEEDED"
    assert list(output_dir.iterdir()) == []


# --- D. manifest / checksums ---------------------------------------------------


def test_external_and_embedded_manifest_are_field_for_field_equal(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)
    sha = _commit_all(repo)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    result = build_release(
        _default_request(source_sha=sha, output_dir=output_dir),
        invocation_cwd=repo,
        clock=_fixed_clock(FIXED_BUILT_AT),
    )
    assert result.ok is True, result.model_dump_json()
    manifest_path = next(output_dir.glob("*.release-manifest.json"))
    artifact = next(output_dir.glob("*.tar.gz"))
    external = ReleaseManifest.model_validate_json(manifest_path.read_text())
    release_id = compute_release_id(release_version="1.0.0", source_sha=sha)
    members = _tar_members(artifact)
    embedded = ReleaseManifest.model_validate_json(members[f"{release_id}/release_manifest.json"])
    assert embedded == external


def test_sha256sums_has_exactly_the_expected_entries(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)
    sha = _commit_all(repo)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    result = build_release(
        _default_request(source_sha=sha, output_dir=output_dir),
        invocation_cwd=repo,
        clock=_fixed_clock(FIXED_BUILT_AT),
    )
    assert result.ok is True, result.model_dump_json()
    sums_text = (output_dir / "SHA256SUMS").read_text()
    lines = [line for line in sums_text.splitlines() if line.strip()]
    assert len(lines) == 2
    filenames = {line.split(maxsplit=1)[1] for line in lines}
    artifact_name = next(output_dir.glob("*.tar.gz")).name
    manifest_name = next(output_dir.glob("*.release-manifest.json")).name
    assert filenames == {artifact_name, manifest_name}


def test_alembic_heads_reflect_selected_commit_migration_tree(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)
    (repo / "backend" / "alembic" / "versions" / "0002_second.py").write_text(
        _migration_script("0002second", "0001initial")
    )
    sha = _commit_all(repo)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    result = build_release(
        _default_request(source_sha=sha, output_dir=output_dir),
        invocation_cwd=repo,
        clock=_fixed_clock(FIXED_BUILT_AT),
    )
    assert result.ok is True, result.model_dump_json()
    manifest_path = next(output_dir.glob("*.release-manifest.json"))
    manifest = ReleaseManifest.model_validate_json(manifest_path.read_text())
    assert manifest.alembic_heads == ["0002second"]


# --- E. output safety -----------------------------------------------------------


def test_no_overwrite_when_output_target_already_exists(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)
    sha = _commit_all(repo)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    release_id = compute_release_id(release_version="1.0.0", source_sha=sha)
    pre_existing = output_dir / f"{release_id}.tar.gz"
    pre_existing.write_bytes(b"pre-existing unrelated content")

    result = build_release(
        _default_request(source_sha=sha, output_dir=output_dir),
        invocation_cwd=repo,
        clock=_fixed_clock(FIXED_BUILT_AT),
    )
    assert result.ok is False
    assert _finding(result, "output_targets").code == "OUTPUT_TARGET_EXISTS"
    # Pre-existing file is completely untouched.
    assert pre_existing.read_bytes() == b"pre-existing unrelated content"
    assert list(output_dir.iterdir()) == [pre_existing]


def test_failure_leaves_no_partial_owned_output(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)
    (repo / "backend" / "pyproject.toml").unlink()
    sha = _commit_all(repo)
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    result = build_release(
        _default_request(source_sha=sha, output_dir=output_dir), invocation_cwd=repo
    )
    assert result.ok is False
    assert list(output_dir.iterdir()) == []


def test_create_exclusive_cleanup_never_deletes_a_swapped_replacement(tmp_path: Path) -> None:
    """Direct unit proof of the concurrent-pathname-safety property: cleanup
    only removes the exact inode this invocation created, identity-checked
    via (st_dev, st_ino) — never a different file a concurrent actor placed
    at the same basename afterward."""
    import os

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    dir_fd = os.open(str(output_dir), os.O_RDONLY | os.O_DIRECTORY)
    try:
        created = _create_exclusive(
            dir_fd=dir_fd, basename="target.txt", data=b"original", output_dir=output_dir
        )
        # Simulate a concurrent actor replacing the file at the same path via
        # an atomic rename — a different, real inode, unlike unlink+recreate
        # which can occasionally reuse the just-freed inode number on some
        # filesystems and would make this test meaningless either way.
        replacement_src = output_dir / "replacement_src.txt"
        replacement_src.write_bytes(b"replacement, different inode")
        os.replace(replacement_src, output_dir / "target.txt")

        _cleanup_created_file(
            dir_fd=created.dir_fd, basename=created.basename, expected_stat=created.stat
        )

        assert (output_dir / "target.txt").read_bytes() == b"replacement, different inode"
    finally:
        os.close(dir_fd)


def test_create_exclusive_rejects_existing_target(tmp_path: Path) -> None:
    import os

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    (output_dir / "target.txt").write_bytes(b"already here")
    dir_fd = os.open(str(output_dir), os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(Exception, match="already exists"):
            _create_exclusive(
                dir_fd=dir_fd, basename="target.txt", data=b"new", output_dir=output_dir
            )
        assert (output_dir / "target.txt").read_bytes() == b"already here"
    finally:
        os.close(dir_fd)


# --- F. integration: build-release -> verify-release round trip ---------------


def test_build_release_output_passes_verify_release(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)
    sha = _commit_all(repo)
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    build_result = build_release(
        _default_request(source_sha=sha, output_dir=output_dir),
        invocation_cwd=repo,
        clock=_fixed_clock(FIXED_BUILT_AT),
    )
    assert build_result.ok is True, build_result.model_dump_json()

    release_id = compute_release_id(release_version="1.0.0", source_sha=sha)
    verify_result = verify_release(
        manifest_path=output_dir / f"{release_id}.release-manifest.json",
        sha256sums_path=output_dir / "SHA256SUMS",
        artifact_path=output_dir / f"{release_id}.tar.gz",
        expected_release_id=release_id,
    )
    assert verify_result.ok is True, verify_result.model_dump_json()
    assert all(f.status != FindingStatus.FAIL for f in verify_result.findings)


# --- G. reproducibility semantics -----------------------------------------------


def test_same_commit_and_fixed_clock_produce_identical_bytes(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)
    sha = _commit_all(repo)

    out1 = tmp_path / "out1"
    out1.mkdir()
    out2 = tmp_path / "out2"
    out2.mkdir()

    r1 = build_release(
        _default_request(source_sha=sha, output_dir=out1),
        invocation_cwd=repo,
        clock=_fixed_clock(FIXED_BUILT_AT),
    )
    r2 = build_release(
        _default_request(source_sha=sha, output_dir=out2),
        invocation_cwd=repo,
        clock=_fixed_clock(FIXED_BUILT_AT),
    )
    assert r1.ok is True and r2.ok is True

    artifact1 = next(out1.glob("*.tar.gz")).read_bytes()
    artifact2 = next(out2.glob("*.tar.gz")).read_bytes()
    assert artifact1 == artifact2

    manifest1 = next(out1.glob("*.release-manifest.json")).read_bytes()
    manifest2 = next(out2.glob("*.release-manifest.json")).read_bytes()
    assert manifest1 == manifest2


def test_default_clock_uses_real_current_utc_time(tmp_path: Path) -> None:
    """No clock override -> CLI/production behavior uses a real build
    timestamp, never a fixed/fake one."""
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)
    sha = _commit_all(repo)
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    before = datetime.now(UTC)
    result = build_release(
        _default_request(source_sha=sha, output_dir=output_dir), invocation_cwd=repo
    )
    after = datetime.now(UTC)

    assert result.ok is True, result.model_dump_json()
    manifest_path = next(output_dir.glob("*.release-manifest.json"))
    manifest = ReleaseManifest.model_validate_json(manifest_path.read_text())
    assert before <= manifest.built_at <= after


def test_two_real_time_builds_of_the_same_commit_differ_in_built_at(tmp_path: Path) -> None:
    """Explicit proof that this module never claims byte-identical builds
    across real wall-clock time — two builds a moment apart differ in
    built_at and therefore in manifest bytes/checksums, by design."""
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)
    sha = _commit_all(repo)

    out1 = tmp_path / "out1"
    out1.mkdir()
    out2 = tmp_path / "out2"
    out2.mkdir()

    r1 = build_release(
        _default_request(source_sha=sha, output_dir=out1),
        invocation_cwd=repo,
        clock=_fixed_clock(datetime(2026, 1, 1, tzinfo=UTC)),
    )
    r2 = build_release(
        _default_request(source_sha=sha, output_dir=out2),
        invocation_cwd=repo,
        clock=_fixed_clock(datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)),
    )
    assert r1.ok is True and r2.ok is True
    manifest1 = next(out1.glob("*.release-manifest.json")).read_bytes()
    manifest2 = next(out2.glob("*.release-manifest.json")).read_bytes()
    assert manifest1 != manifest2


# --- H. non-executing Alembic static metadata (PR #56 security-corrective pass) --


def test_malicious_migration_top_level_side_effect_is_never_executed(tmp_path: Path) -> None:
    """The whole point of the corrective fix: a selected commit's migration
    file can contain arbitrary top-level Python — build-release must never
    run it while deriving `alembic_heads`. Proven here by a migration file
    that would write a sentinel file as an import-time side effect; the
    build must still succeed (its static revision/down_revision metadata is
    valid) while the sentinel is never created."""
    sentinel_path = tmp_path / "SENTINEL_EXECUTED"
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)
    malicious_migration = (
        '"""fixture migration with a top-level side effect"""\n'
        "from pathlib import Path\n"
        f"Path({str(sentinel_path)!r}).write_text('executed')\n"
        "revision = 'sentinelrev'\n"
        "down_revision = None\n"
    )
    (repo / "backend" / "alembic" / "versions" / "0001_initial.py").write_text(malicious_migration)
    sha = _commit_all(repo)
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    result = build_release(
        _default_request(source_sha=sha, output_dir=output_dir),
        invocation_cwd=repo,
        clock=_fixed_clock(FIXED_BUILT_AT),
    )
    assert result.ok is True, result.model_dump_json()
    assert not sentinel_path.exists(), "migration module code must never be executed"
    manifest_path = next(output_dir.glob("*.release-manifest.json"))
    manifest = ReleaseManifest.model_validate_json(manifest_path.read_text())
    assert manifest.alembic_heads == ["sentinelrev"]


def test_simple_alembic_revision_chain_computes_single_head(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)  # 0001_initial: revision=0001initial, down_revision=None
    (repo / "backend" / "alembic" / "versions" / "0002_second.py").write_text(
        _migration_script("0002second", "0001initial")
    )
    (repo / "backend" / "alembic" / "versions" / "0003_third.py").write_text(
        _migration_script("0003third", "0002second")
    )
    sha = _commit_all(repo)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    result = build_release(
        _default_request(source_sha=sha, output_dir=output_dir),
        invocation_cwd=repo,
        clock=_fixed_clock(FIXED_BUILT_AT),
    )
    assert result.ok is True, result.model_dump_json()
    manifest_path = next(output_dir.glob("*.release-manifest.json"))
    manifest = ReleaseManifest.model_validate_json(manifest_path.read_text())
    assert manifest.alembic_heads == ["0003third"]


def test_multiple_alembic_heads_are_all_reported(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)  # 0001_initial: revision=0001initial, down_revision=None
    (repo / "backend" / "alembic" / "versions" / "0002_branch_a.py").write_text(
        _migration_script("branch_a", "0001initial")
    )
    (repo / "backend" / "alembic" / "versions" / "0003_branch_b.py").write_text(
        _migration_script("branch_b", "0001initial")
    )
    sha = _commit_all(repo)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    result = build_release(
        _default_request(source_sha=sha, output_dir=output_dir),
        invocation_cwd=repo,
        clock=_fixed_clock(FIXED_BUILT_AT),
    )
    assert result.ok is True, result.model_dump_json()
    manifest_path = next(output_dir.glob("*.release-manifest.json"))
    manifest = ReleaseManifest.model_validate_json(manifest_path.read_text())
    assert manifest.alembic_heads == ["branch_a", "branch_b"]


def test_duplicate_alembic_revision_id_rejected(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)  # 0001_initial: revision=0001initial
    (repo / "backend" / "alembic" / "versions" / "0002_dup.py").write_text(
        _migration_script("0001initial", None)
    )
    sha = _commit_all(repo)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    result = build_release(
        _default_request(source_sha=sha, output_dir=output_dir), invocation_cwd=repo
    )
    assert result.ok is False
    assert _finding(result, "alembic_heads").code == "ALEMBIC_STATIC_METADATA_INVALID"
    assert list(output_dir.iterdir()) == []


def test_alembic_missing_parent_revision_rejected(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)
    (repo / "backend" / "alembic" / "versions" / "0002_orphan.py").write_text(
        _migration_script("orphanrev", "does-not-exist")
    )
    sha = _commit_all(repo)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    result = build_release(
        _default_request(source_sha=sha, output_dir=output_dir), invocation_cwd=repo
    )
    assert result.ok is False
    assert _finding(result, "alembic_heads").code == "ALEMBIC_STATIC_METADATA_INVALID"
    assert list(output_dir.iterdir()) == []


def test_alembic_dynamic_computed_revision_rejected(tmp_path: Path) -> None:
    """A `revision` value that is anything other than a plain string
    literal (a function call, here) must be rejected rather than silently
    evaluated or ignored — this is exactly the "never execute" boundary."""
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)
    dynamic_migration = (
        '"""fixture migration with a computed revision"""\n'
        "import hashlib\n"
        "revision = hashlib.sha1(b'x').hexdigest()[:12]\n"
        "down_revision = None\n"
    )
    (repo / "backend" / "alembic" / "versions" / "0001_initial.py").write_text(dynamic_migration)
    sha = _commit_all(repo)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    result = build_release(
        _default_request(source_sha=sha, output_dir=output_dir), invocation_cwd=repo
    )
    assert result.ok is False
    assert _finding(result, "alembic_heads").code == "ALEMBIC_STATIC_METADATA_INVALID"
    assert list(output_dir.iterdir()) == []


def test_alembic_malformed_missing_revision_assignment_rejected(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)
    malformed_migration = '"""fixture migration missing revision"""\ndown_revision = None\n'
    (repo / "backend" / "alembic" / "versions" / "0001_initial.py").write_text(malformed_migration)
    sha = _commit_all(repo)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    result = build_release(
        _default_request(source_sha=sha, output_dir=output_dir), invocation_cwd=repo
    )
    assert result.ok is False
    assert _finding(result, "alembic_heads").code == "ALEMBIC_STATIC_METADATA_INVALID"
    assert list(output_dir.iterdir()) == []


# --- I. release-identity path safety (PR #56 security-corrective pass) -----------


def _build_with_unsafe_version(tmp_path: Path, version_toml_literal: str):
    """`version_toml_literal` is the exact TOML-source text for the
    `[project].version` value (e.g. `"'../evil'"` for a TOML literal
    string), so tests can construct a `pyproject.toml` whose parsed
    `release_version` contains characters an f-string-interpolated plain
    version never could (a literal backslash, a raw control character via
    a `\\uXXXX` escape, ...)."""
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)
    (repo / "backend" / "pyproject.toml").write_text(
        f'[project]\nname = "meyar"\nversion = {version_toml_literal}\n'
        'requires-python = ">=3.12"\n'
    )
    sha = _commit_all(repo)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    result = build_release(
        _default_request(source_sha=sha, output_dir=output_dir),
        invocation_cwd=repo,
        clock=_fixed_clock(FIXED_BUILT_AT),
    )
    return result, output_dir


def test_release_version_containing_traversal_sequence_rejected(tmp_path: Path) -> None:
    result, output_dir = _build_with_unsafe_version(tmp_path, "'../evil'")
    assert result.ok is False
    assert _finding(result, "release_identity_safety").code == "RELEASE_VERSION_UNSAFE"
    assert list(output_dir.iterdir()) == []


def test_release_version_containing_forward_slash_rejected(tmp_path: Path) -> None:
    result, output_dir = _build_with_unsafe_version(tmp_path, "'1.0/beta'")
    assert result.ok is False
    assert _finding(result, "release_identity_safety").code == "RELEASE_VERSION_UNSAFE"
    assert list(output_dir.iterdir()) == []


def test_release_version_containing_backslash_rejected(tmp_path: Path) -> None:
    # TOML literal (single-quoted) string: backslash is not an escape
    # introducer, so this embeds one literal backslash character.
    result, output_dir = _build_with_unsafe_version(tmp_path, "'bad\\path'")
    assert result.ok is False
    assert _finding(result, "release_identity_safety").code == "RELEASE_VERSION_UNSAFE"
    assert list(output_dir.iterdir()) == []


def test_release_version_containing_control_character_rejected(tmp_path: Path) -> None:
    # TOML basic (double-quoted) string with a \u escape: a genuine control
    # character (U+0001) in the parsed Python string, not merely its text
    # representation.
    result, output_dir = _build_with_unsafe_version(tmp_path, '"bad\\u0001char"')
    assert result.ok is False
    assert _finding(result, "release_identity_safety").code == "RELEASE_VERSION_UNSAFE"
    assert list(output_dir.iterdir()) == []


def test_release_version_normal_value_is_accepted(tmp_path: Path) -> None:
    result, output_dir = _build_with_unsafe_version(tmp_path, "'0.1.0'")
    assert result.ok is True, result.model_dump_json()
    assert _finding(result, "release_identity_safety").code == "RELEASE_IDENTITY_SAFE"
    assert next(output_dir.glob("*.tar.gz")).name.startswith("meyar-0.1.0+")


# --- J. fd ownership on os.fdopen()/setup failure (PR #56 security-corrective) ---


def test_create_exclusive_closes_raw_fd_and_removes_output_on_fdopen_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    dir_fd = os.open(str(output_dir), os.O_RDONLY | os.O_DIRECTORY)
    opened_fds: list[int] = []

    def _raising_fdopen(fd, *args, **kwargs):  # noqa: ANN001, ARG001
        opened_fds.append(fd)
        raise OSError("simulated fdopen failure")

    monkeypatch.setattr(os, "fdopen", _raising_fdopen)
    try:
        with pytest.raises(OSError, match="simulated fdopen failure"):
            _create_exclusive(
                dir_fd=dir_fd, basename="target.txt", data=b"data", output_dir=output_dir
            )
    finally:
        monkeypatch.undo()

    assert opened_fds, "os.fdopen should have been invoked exactly once"
    leaked_fd = opened_fds[0]
    # The raw fd must have been explicitly closed by _create_exclusive
    # itself: operating on it again must fail with EBADF, not succeed.
    with pytest.raises(OSError):
        os.fstat(leaked_fd)
    # The partially-created output file must have been removed.
    assert not (output_dir / "target.txt").exists()
    os.close(dir_fd)


def test_create_exclusive_fdopen_failure_never_deletes_a_swapped_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Combines the two failure-handling properties: even when `os.fdopen`
    fails AND a concurrent actor has already swapped in a different real
    file at the same basename (atomic rename) before the failure handler
    runs, cleanup must still only ever remove the exact inode this
    invocation created — never the replacement."""
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    dir_fd = os.open(str(output_dir), os.O_RDONLY | os.O_DIRECTORY)
    real_open = os.open

    def _open_then_swap(path, *args, **kwargs):  # noqa: ANN001, ARG001
        fd = real_open(path, *args, **kwargs)
        if path == "target.txt":
            replacement_src = output_dir / "replacement_src.txt"
            replacement_src.write_bytes(b"replacement, different inode")
            os.replace(replacement_src, output_dir / "target.txt")
        return fd

    def _raising_fdopen(fd, *args, **kwargs):  # noqa: ANN001, ARG001
        raise OSError("simulated fdopen failure")

    monkeypatch.setattr(os, "open", _open_then_swap)
    monkeypatch.setattr(os, "fdopen", _raising_fdopen)
    try:
        with pytest.raises(OSError, match="simulated fdopen failure"):
            _create_exclusive(
                dir_fd=dir_fd, basename="target.txt", data=b"data", output_dir=output_dir
            )
    finally:
        monkeypatch.undo()

    assert (output_dir / "target.txt").read_bytes() == b"replacement, different inode"
    os.close(dir_fd)


def test_create_exclusive_succeeds_normally_after_fd_ownership_fix(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    dir_fd = os.open(str(output_dir), os.O_RDONLY | os.O_DIRECTORY)
    try:
        created = _create_exclusive(
            dir_fd=dir_fd, basename="target.txt", data=b"ordinary content", output_dir=output_dir
        )
        assert created.path.read_bytes() == b"ordinary content"
    finally:
        os.close(dir_fd)


# --- K. resource safety: Git blob size bound (PR #56 security-corrective pass) ---


def test_git_blob_size_bound_enforced_before_content_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every allowlisted blob's Git-declared size must be checked against
    the accepted archive aggregate-size bound BEFORE its content is ever
    read via `git cat-file -p` — proven here by lowering the bound far
    below the fixture repo's real (tiny) file sizes and asserting the
    build fails truthfully instead of buffering an oversized blob first."""
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)
    sha = _commit_all(repo)
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    monkeypatch.setattr("meyar.ops.archive_safety.MAX_AGGREGATE_UNCOMPRESSED_SIZE", 4)

    result = build_release(
        _default_request(source_sha=sha, output_dir=output_dir), invocation_cwd=repo
    )
    assert result.ok is False
    assert _finding(result, "blob_content_fetch").code == "GIT_BLOB_TOO_LARGE"
    assert list(output_dir.iterdir()) == []
