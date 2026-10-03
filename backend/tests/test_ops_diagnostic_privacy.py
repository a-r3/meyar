"""Synthetic acceptance-blocker proofs; no operator data or real artifacts."""

from pathlib import Path

import pytest
from test_ops_build_release import (
    _commit_all,
    _default_request,
    _init_repo,
    _write_fixture_repo_files,
)
from test_ops_verify_release import _build_artifact, _write_manifest, _write_sha256sums

from meyar.config import Settings
from meyar.ops import archive_safety, preflight, readiness, status, verify_release
from meyar.ops.build_release import build_release
from meyar.ops.result import FindingStatus, OpsResult, OpsResultBuilder

SENTINEL = "SYNTHETIC_PRIVATE_OPERATOR_S2_REJECTED"


def assert_private_failure(result: OpsResult, code: str) -> None:
    assert not result.ok
    assert any(f.code == code and f.status is FindingStatus.FAIL for f in result.findings)
    assert SENTINEL not in result.model_dump_json()


@pytest.mark.parametrize("failure", ["invalid_directory", "existing_target", "long_member"])
def test_build_failure_paths_and_members_are_private(tmp_path, monkeypatch, failure):
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo)
    if failure == "long_member":
        (repo / "backend/src/meyar" / f"{SENTINEL}.py").write_text("# synthetic\n")
        monkeypatch.setattr(archive_safety, "MAX_MEMBER_NAME_LENGTH", 70)
    sha = _commit_all(repo)
    output = tmp_path / SENTINEL
    if failure != "invalid_directory":
        output.mkdir()
    if failure == "existing_target":
        (output / "SHA256SUMS").write_text("synthetic existing target")
    result = build_release(_default_request(source_sha=sha, output_dir=output), invocation_cwd=repo)
    code = {
        "invalid_directory": "OUTPUT_DIR_INVALID",
        "existing_target": "OUTPUT_TARGET_EXISTS",
        "long_member": "MEMBER_NAME_LENGTH_EXCEEDS_BOUND",
    }[failure]
    assert_private_failure(result, code)
    assert str(output) not in result.model_dump_json()
    if failure == "long_member":
        finding = next(f for f in result.findings if f.code == code)
        assert "1" in finding.message and "70" in finding.message


@pytest.mark.parametrize("kind", ["artifact", "manifest"])
@pytest.mark.parametrize("ambiguity", [False, True])
def test_checksum_filename_failures_are_private(tmp_path, kind, ambiguity):
    manifest = tmp_path / f"{SENTINEL}.json"
    _write_manifest(manifest)
    artifact = _build_artifact(tmp_path, archive_name=f"{SENTINEL}.tar.gz")
    sums = _write_sha256sums(tmp_path, artifact, manifest)
    target = artifact if kind == "artifact" else manifest
    lines = sums.read_text().splitlines()
    selected = next(line for line in lines if line.endswith(target.name))
    lines = [line for line in lines if not line.endswith(target.name)]
    if ambiguity:
        lines.extend([selected, selected])
    sums.write_text("\n".join(lines) + "\n")
    result = verify_release.verify_release(
        manifest_path=manifest, artifact_path=artifact, sha256sums_path=sums
    )
    prefix = "MANIFEST_" if kind == "manifest" else ""
    suffix = "AMBIGUOUS" if ambiguity else "MISSING"
    assert_private_failure(result, f"{prefix}CHECKSUM_ENTRY_{suffix}")


def test_unsafe_archive_member_failure_is_private(tmp_path):
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest)
    artifact = _build_artifact(tmp_path, extra_members={f"../{SENTINEL}": b"synthetic"})
    sums = _write_sha256sums(tmp_path, artifact, manifest)
    result = verify_release.verify_release(
        manifest_path=manifest, artifact_path=artifact, sha256sums_path=sums
    )
    assert_private_failure(result, "UNSAFE_ARCHIVE_MEMBER")
    assert "2" in next(f.message for f in result.findings if f.code == "UNSAFE_ARCHIVE_MEMBER")


@pytest.mark.parametrize("module", [readiness, status])
async def test_unavailable_configured_model_diagnostics_are_private(monkeypatch, module):
    settings = Settings(_env_file=None, ollama_model=SENTINEL, ollama_embedding_model=SENTINEL)

    class Provider:
        async def health(self):
            return {"reachable": False, "model_available": False}

    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(module, "get_llm_provider", Provider)
    monkeypatch.setattr(module, "get_embedding_provider", Provider)
    builder = OpsResultBuilder(action="synthetic-health")
    await module._check_ollama_and_models(builder)
    result = builder.build()
    assert_private_failure(result, "OLLAMA_UNREACHABLE")
    assert {"LLM_MODEL_UNAVAILABLE", "EMBEDDING_MODEL_UNAVAILABLE"} <= {
        f.code for f in result.findings
    }


def test_preflight_missing_path_diagnostic_is_private(tmp_path, monkeypatch):
    private = tmp_path / SENTINEL
    monkeypatch.setattr(preflight, "_REQUIRED_PATHS", [private])
    builder = OpsResultBuilder(action="preflight")
    preflight._check_required_paths(builder)
    assert_private_failure(builder.build(), "PATH_MISSING")


def test_preflight_storage_failure_is_private(tmp_path, monkeypatch):
    private = tmp_path / SENTINEL
    private.write_text("synthetic non-directory")
    settings = Settings(_env_file=None, storage_root=str(private))
    monkeypatch.setattr(preflight, "get_settings", lambda: settings)
    builder = OpsResultBuilder(action="preflight")
    preflight._check_storage_root(builder)
    assert_private_failure(builder.build(), "STORAGE_ROOT_NOT_A_DIRECTORY")


def test_update_worker_invalid_argv_does_not_echo_private_input(capsys):
    from meyar.ops.update_worker import main

    assert main([f"--{SENTINEL}"]) == 1
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_host_config_failure_does_not_evaluate_exception_text(monkeypatch):
    from meyar.ops import host_config

    class PrivateError(ValueError):
        def __str__(self):
            pytest.fail("exception text evaluated")

        def __repr__(self):
            pytest.fail("exception repr evaluated")

    def fail(_root):
        raise PrivateError(SENTINEL)

    monkeypatch.setattr(host_config, "load_host_settings", fail)
    assert_private_failure(host_config.verify_host_config(Path(SENTINEL)), "CONFIG_VALUES_INVALID")


@pytest.mark.parametrize("kind", ["lock_missing", "lock_duplicate", "manifest_duplicate"])
def test_internal_member_failure_does_not_echo_private_root(tmp_path, kind):
    from test_ops_verify_release import _manifest_dict

    from meyar.ops.release_manifest import ReleaseManifest

    artifact = _build_artifact(
        tmp_path,
        root=SENTINEL,
        uv_lock=None if kind == "lock_missing" else b"synthetic",
        duplicate_uv_lock=kind == "lock_duplicate",
    )
    if kind == "manifest_duplicate":
        import tarfile

        from test_ops_verify_release import _add_file_member

        with tarfile.open(artifact, "w:gz") as archive:
            for _ in range(2):
                _add_file_member(archive, f"{SENTINEL}/release_manifest.json", b"{}")
    builder = OpsResultBuilder(action="verify-release")
    manifest = ReleaseManifest.model_validate(_manifest_dict())
    check = (
        verify_release._check_internal_manifest
        if kind == "manifest_duplicate"
        else verify_release._check_uv_lock_binding
    )
    check(builder, manifest, artifact, SENTINEL, True)
    code = {
        "lock_missing": "UV_LOCK_MEMBER_MISSING",
        "lock_duplicate": "UV_LOCK_MEMBER_AMBIGUOUS",
        "manifest_duplicate": "INTERNAL_MANIFEST_AMBIGUOUS",
    }[kind]
    assert_private_failure(builder.build(), code)


def _semantic_inventory(rows):
    """Compare expression structure; Python patch releases vary unparse quotes."""
    import ast

    return [
        {
            key: value
            if key == "file" or value is None
            else ast.dump(ast.parse(value, mode="eval"), include_attributes=False)
            for key, value in row.items()
        }
        for row in rows
    ]


def test_inventory_compares_structure_and_still_rejects_private_path():
    # The two f-strings are equivalent (PEP 701), but Python 3.12 patch
    # releases choose different outer quotes when unparsing this same AST.
    first = {"file": "synthetic.py", "message": "f\"keys: {', '.join(missing)}\""}
    second = {"file": "synthetic.py", "message": "f'keys: {', '.join(missing)}'"}
    private = {"file": "synthetic.py", "message": 'f"keys: {private_path}"'}
    assert _semantic_inventory([first]) == _semantic_inventory([second])
    assert _semantic_inventory([first]) != _semantic_inventory([private])


def test_ops_failure_diagnostic_sink_inventory_requires_review():
    """Only nonliteral failure/conditional sinks need explicit source review.

    Fixed messages and explicit OK inventory are allowed. New arbitrary
    interpolations (including names, Paths and exceptions) fail this manifest;
    reviewers must classify their input as closed/numeric before updating it.
    Delegating result/storage helpers are included, never silently exempted.
    """
    import ast
    import json

    root = Path(__file__).parents[1] / "src/meyar/ops"
    observed = []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in sorted(ast.walk(tree), key=lambda n: getattr(n, "lineno", 0)):
            if not isinstance(node, ast.Call):
                continue
            fields = {item.arg: item.value for item in node.keywords}
            message = fields.get("message")
            if message is None or isinstance(message, ast.Constant):
                continue
            finding_status = fields.get("status")
            if isinstance(finding_status, ast.Attribute) and finding_status.attr == "OK":
                continue
            observed.append(
                {
                    "file": path.name,
                    **{
                        key: ast.unparse(fields[key]) if key in fields else None
                        for key in ("status", "component", "code", "message")
                    },
                }
            )
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                fields = {
                    key.value: value
                    for key, value in zip(node.keys, node.values, strict=True)
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                }
                message = fields.get("message")
                if message is not None and not isinstance(message, ast.Constant):
                    observed.append(
                        {
                            "file": path.name,
                            **{
                                key: ast.unparse(fields[key]) if key in fields else None
                                for key in ("status", "component", "code", "message")
                            },
                        }
                    )
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in {"str", "repr"}:
                    assert not any(
                        isinstance(arg, ast.Name) and arg.id == "exc" for arg in node.args
                    ), path
            if isinstance(node, ast.Attribute):
                assert not (
                    isinstance(node.value, ast.Name)
                    and node.value.id == "exc"
                    and node.attr in {"args", "__cause__", "__traceback__"}
                ), path
            if isinstance(node, ast.FormattedValue):
                assert not (isinstance(node.value, ast.Name) and node.value.id == "exc"), path
    expected = json.loads(
        (Path(__file__).parent / "fixtures/ops_failure_diagnostic_sinks.json").read_text()
    )
    assert _semantic_inventory(observed) == _semantic_inventory(expected), (
        "Review changed operational failure diagnostic sink inputs"
    )


async def test_readiness_schema_mismatch_does_not_echo_private_revision(monkeypatch):
    monkeypatch.setattr(readiness, "get_settings", lambda: Settings(_env_file=None))
    from meyar.ops.alembic_introspect import DbAlembicRevisionResult

    monkeypatch.setattr(readiness, "get_code_alembic_heads", lambda _path: ["a" * 12])

    async def private_revision(_engine):
        return DbAlembicRevisionResult(table_exists=True, revisions=[SENTINEL])

    monkeypatch.setattr(readiness, "get_db_alembic_revision", private_revision)
    builder = OpsResultBuilder(action="readiness")
    await readiness._check_db_migration(builder, database_ok=True)
    assert_private_failure(builder.build(), "SCHEMA_MISMATCH")


@pytest.mark.parametrize("field", ["release_id", "release_version"])
def test_success_inventory_does_not_echo_arbitrary_manifest_identity(tmp_path, field):
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, **{field: SENTINEL})
    builder = OpsResultBuilder(action="status")
    status._check_release_identity(builder, manifest)
    result = builder.build()
    assert result.ok
    assert result.findings[0].code == "RELEASE_IDENTITY"
    assert SENTINEL not in result.model_dump_json()
