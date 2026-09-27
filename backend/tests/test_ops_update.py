"""Linux simulation of the staged update state machine and its trust boundaries."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from meyar.ops import deployment_ready, update
from meyar.ops.alembic_static_metadata import prove_static_linear_upgrade
from meyar.ops.offline_host import InstallFailure
from meyar.ops.result import FindingStatus, OpsResultBuilder
from meyar.ops.service_plist import ServiceSpec

FROM = "meyar-0.1.0+" + "a" * 12
TO = "meyar-0.1.1+" + "b" * 12
LABEL = "com.bank.meyar"
PASSWORD = "synthetic-db-password-never-output"
CV = "private-candidate-cv.pdf"


def _migration(revision: str, parent: str | None) -> bytes:
    return f"revision = {revision!r}\ndown_revision = {parent!r}\n".encode()


def _plan(kind: str = "APP_ONLY") -> dict[str, object]:
    return {
        "format_version": 1,
        "update_id": "update-1",
        "actor_uid": 1000,
        "created_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        "from_release_id": FROM,
        "from_source_sha": "a" * 40,
        "from_alembic_head": "source",
        "to_release_id": TO,
        "to_source_sha": "b" * 40,
        "to_alembic_head": "source" if kind == "APP_ONLY" else "target",
        "rollback_compatibility": kind,
        "from_rollback_compatibility": "APP_ONLY",
        "model_manifest_reference": "model-ref",
        "model_approval_status": "DEVELOPMENT_INTEGRATION",
        "app_service_label": LABEL,
        "config_identity": [1, 2, 3, 4],
    }


def test_static_graph_proves_only_one_linear_forward_path() -> None:
    files = {
        "backend/alembic/versions/base.py": _migration("base", None),
        "backend/alembic/versions/source.py": _migration("source", "base"),
        "backend/alembic/versions/target.py": _migration("target", "source"),
    }
    assert prove_static_linear_upgrade(files, "source", "target")
    assert not prove_static_linear_upgrade(files, "unrelated", "target")
    assert not prove_static_linear_upgrade(files, "target", "source")
    files["backend/alembic/versions/branch.py"] = _migration("branch", "base")
    assert not prove_static_linear_upgrade(files, "source", "target")


@pytest.mark.parametrize(
    "kind,target_head,model_reference,graph,expected",
    [
        ("APP_ONLY", "target", "model-ref", True, "UPDATE_SCHEMA_PATH_INVALID"),
        ("FORWARD_COMPATIBLE_SCHEMA", "target", "model-ref", False, "UPDATE_SCHEMA_PATH_INVALID"),
        (
            "BACKUP_RESTORE_REQUIRED",
            "target",
            "model-ref",
            True,
            "UPDATE_REQUIRES_RESTORE_PROCEDURE",
        ),
        (
            "PROHIBITED_PENDING_PROCEDURE",
            "target",
            "model-ref",
            True,
            "UPDATE_PROHIBITED_PENDING_PROCEDURE",
        ),
        ("APP_ONLY", "source", "other-model", True, "UPDATE_MODEL_CHANGE_UNSUPPORTED"),
    ],
)
def test_prepare_policy_rejects_unsupported_release_relationships(
    ops_host_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    target_head: str,
    model_reference: str,
    graph: bool,
    expected: str,
) -> None:
    monkeypatch.setattr(update, "_active_config", lambda *_a, **_k: (None, "source"))
    monkeypatch.setattr(update, "verify_active_release", lambda _root: FROM)
    monkeypatch.setattr(update, "_graph_proven", lambda *_: graph)
    monkeypatch.setattr(update, "load_host_settings", lambda _root: object())
    monkeypatch.setattr(update, "verify_installed_models", lambda *_a, **_k: None)
    monkeypatch.setattr(update, "_config_identity", lambda _root: [1, 2, 3, 4])
    source = {
        "source_sha": "a" * 40,
        "alembic_heads": ["source"],
        "rollback_compatibility": "APP_ONLY",
        "model_manifest": {"reference": "model-ref", "status": "DEVELOPMENT_INTEGRATION"},
    }
    target = {
        **source,
        "source_sha": "b" * 40,
        "alembic_heads": [target_head],
        "rollback_compatibility": kind,
        "model_manifest": {"reference": model_reference, "status": "DEVELOPMENT_INTEGRATION"},
    }
    monkeypatch.setattr(
        update, "_release", lambda _root, release: source if release == FROM else target
    )
    with pytest.raises(update.UpdateFailure) as failure:
        update._plan_fields(ops_host_root, LABEL, "update-1", TO)
    assert failure.value.code == expected


def test_prepare_rejects_same_or_missing_target(
    ops_host_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(update, "_active_config", lambda *_a, **_k: (None, "source"))
    monkeypatch.setattr(update, "verify_active_release", lambda _root: FROM)
    with pytest.raises(update.UpdateFailure, match="UPDATE_TARGET_INVALID"):
        update._plan_fields(ops_host_root, LABEL, "update-1", FROM)

    def missing(*_args: object) -> object:
        raise InstallFailure("INSTALLED_CONTENT_MISMATCH")

    monkeypatch.setattr(update, "_release", missing)
    with pytest.raises(InstallFailure, match="INSTALLED_CONTENT_MISMATCH"):
        update._plan_fields(ops_host_root, LABEL, "update-1", TO)


def test_changed_protected_config_makes_existing_plan_stale(
    ops_host_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(update, "_active_config", lambda *_a, **_k: (None, "source"))
    monkeypatch.setattr(update, "_config_identity", lambda _root: [1, 2, 3, 5])
    plan = _plan()
    plan["actor_uid"] = update.os.geteuid()
    with pytest.raises(update.UpdateFailure, match="UPDATE_PLAN_STALE"):
        update._validate_plan(ops_host_root, LABEL, "update-1", plan)


def test_new_update_blocked_until_prior_update_is_finalized(
    ops_host_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan()
    plan["actor_uid"] = update.os.geteuid()
    directory = update._transaction(ops_host_root, "update-1", create=True)
    update._publish_phase(directory, "plan", plan)
    update._publish_phase(
        directory,
        "apply",
        update._phase_record(plan, backup_id="backup-1", activation_generation="g-update"),
    )
    generation = {
        "activation_reason": "update",
        "update_id": "update-1",
        "plan_sha256": update._plan_digest(plan),
        "backup_id": "backup-1",
        "database_schema_head": "source",
        "release_id": TO,
        "previous_release_id": FROM,
        "rollback_compatibility": "APP_ONLY",
    }
    monkeypatch.setattr(update, "_active_generation", lambda _root: ("g-update", generation))
    assert not update._previous_transaction_complete(ops_host_root)
    update._publish_phase(directory, "finalize", update._phase_record(plan))
    assert update._previous_transaction_complete(ops_host_root)
    generation["update_id"] = "foreign"
    assert not update._previous_transaction_complete(ops_host_root)


def test_prepare_rechecks_identity_after_readiness_and_is_idempotent(
    ops_host_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(update.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(update, "_previous_transaction_complete", lambda _root: True)
    current = _plan()
    current.pop("created_at")
    monkeypatch.setattr(update, "_plan_fields", lambda *_: dict(current))
    monkeypatch.setattr(
        deployment_ready,
        "run_deployment_ready",
        lambda *_: update._result("deployment-ready", "READY", ok=True),
    )
    first = update.run_update_prepare(ops_host_root, LABEL, "update-1", TO)
    assert first.findings[0].code == "UPDATE_PREPARED"
    directory = ops_host_root / "shared/updates/update-1"
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / "plan.json").stat().st_mode & 0o777 == 0o600
    assert update.run_update_prepare(ops_host_root, LABEL, "update-1", TO).findings[0].code == (
        "UPDATE_ALREADY_PREPARED"
    )
    current["to_release_id"] = FROM
    assert update.run_update_prepare(ops_host_root, LABEL, "update-1", FROM).findings[0].code == (
        "UPDATE_ID_CONFLICT"
    )
    assert PASSWORD not in (directory / "plan.json").read_text()


def test_prepare_refuses_readiness_failure_and_stale_recheck(
    ops_host_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(update.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(update, "_previous_transaction_complete", lambda _root: True)
    current = _plan()
    current.pop("created_at")
    monkeypatch.setattr(update, "_plan_fields", lambda *_: dict(current))
    monkeypatch.setattr(
        deployment_ready,
        "run_deployment_ready",
        lambda *_: update._result("deployment-ready", "SECRET_ERROR"),
    )
    assert update.run_update_prepare(ops_host_root, LABEL, "update-1", TO).findings[0].code == (
        "UPDATE_READINESS_FAILED"
    )
    assert not (ops_host_root / "shared/updates/update-1/plan.json").exists()

    def changed(*_: object) -> object:
        current["config_identity"] = [1, 2, 3, 5]
        return update._result("deployment-ready", "READY", ok=True)

    monkeypatch.setattr(deployment_ready, "run_deployment_ready", changed)
    assert update.run_update_prepare(ops_host_root, LABEL, "update-1", TO).findings[0].code == (
        "UPDATE_PLAN_STALE"
    )


@pytest.mark.parametrize(
    "returncode,expected", [(0, None), (113, None), (1, "UPDATE_SERVICE_STATE_UNCERTAIN")]
)
def test_service_absence_requires_exact_113(returncode: int, expected: str | None) -> None:
    runner = lambda _argv: subprocess.CompletedProcess(  # noqa: E731
        ["/bin/launchctl"], returncode, stdout=PASSWORD, stderr=CV
    )
    if returncode == 113:
        update._service_absent(LABEL, runner)
    else:
        with pytest.raises(update.UpdateFailure) as failure:
            update._service_absent(LABEL, runner)
        assert failure.value.code == "UPDATE_SERVICE_STATE_UNCERTAIN"
        assert PASSWORD not in str(failure.value)
        assert CV not in str(failure.value)


@pytest.mark.parametrize("kind", ["APP_ONLY", "FORWARD_COMPATIBLE_SCHEMA"])
def test_apply_uses_verified_backup_and_target_runtime_without_shell(
    ops_host_root: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.setattr(update.platform, "system", lambda: "Darwin")
    plan = _plan(kind)
    plan["actor_uid"] = update.os.geteuid()
    directory = update._transaction(ops_host_root, "update-1", create=True)
    update._publish_phase(directory, "plan", plan)
    monkeypatch.setattr(update, "_validate_plan", lambda *_: plan)
    monkeypatch.setattr(update, "_service_absent", lambda *_: None)
    backup_calls: list[str] = []
    monkeypatch.setattr(update, "_backup_bound", lambda _r, _p, bid, _d: backup_calls.append(bid))
    state = {"release": FROM, "head": "source", "generation": "g-source"}
    monkeypatch.setattr(update, "verify_active_release", lambda _r: state["release"])
    monkeypatch.setattr(update, "_db_head", lambda _r: state["head"])

    def activation(_root: Path, release: str, *, metadata: dict[str, object]) -> None:
        state["release"] = release
        state["generation"] = "g-update"
        state["metadata"] = {
            **metadata,
            "release_id": release,
            "previous_release_id": FROM,
            "rollback_compatibility": kind,
        }

    monkeypatch.setattr(update, "_activate_release_locked", activation)
    monkeypatch.setattr(
        update, "_active_generation", lambda _root: (state["generation"], state["metadata"])
    )
    calls: list[tuple[list[str], dict[str, object]]] = []

    def migrate(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        state["head"] = "target"
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(update.subprocess, "run", migrate)
    result = update.run_update_apply(
        ops_host_root, LABEL, "update-1", "backup-1", Path("/trusted/pg")
    )
    assert result.findings[0].code == "UPDATE_APPLIED_SERVICE_STOPPED"
    assert backup_calls == ["backup-1"]
    if kind == "APP_ONLY":
        assert not calls
    else:
        assert len(calls) == 1
        argv, kwargs = calls[0]
        assert argv[:3] == [
            str(ops_host_root / "releases" / TO / ".venv/bin/python"),
            "-m",
            "meyar.ops.update_worker",
        ]
        assert argv[-4:] == ["--from-head", "source", "--to-head", "target"]
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert "shell" not in kwargs
        assert PASSWORD not in json.dumps(argv)
    receipt = (directory / "apply.json").read_text()
    assert PASSWORD not in receipt and CV not in receipt
    (directory / "apply.json").unlink()
    assert (
        update.run_update_apply(ops_host_root, LABEL, "update-1", "backup-1", Path("/trusted/pg"))
        .findings[0]
        .code
        == "UPDATE_APPLIED_SERVICE_STOPPED"
    )
    state["metadata"]["update_id"] = "foreign"
    assert (
        update.run_update_apply(ops_host_root, LABEL, "update-1", "backup-1", Path("/trusted/pg"))
        .findings[0]
        .code
        == "UPDATE_STATE_UNCERTAIN"
    )


def test_backup_binding_rejects_old_or_wrong_source(
    ops_host_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan()
    backup = {
        "release_id": FROM,
        "source_sha": "a" * 40,
        "alembic_head": "source",
        "created_at": datetime.now(UTC).isoformat(),
    }
    monkeypatch.setattr(update, "_backups_root", lambda *_: ops_host_root / "shared/backups")
    monkeypatch.setattr(update, "_pg_tools", lambda *_: (Path("/pg_dump"), Path("/pg_restore")))
    monkeypatch.setattr(update, "_verify_artifact", lambda *_: None)
    monkeypatch.setattr(update, "_load_json", lambda *_: backup)
    update._backup_bound(ops_host_root, plan, "backup-1", Path("/trusted/pg"))
    backup["release_id"] = TO
    with pytest.raises(update.UpdateFailure, match="UPDATE_BACKUP_INVALID"):
        update._backup_bound(ops_host_root, plan, "backup-1", Path("/trusted/pg"))
    backup["release_id"] = FROM
    backup["created_at"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    with pytest.raises(update.UpdateFailure, match="UPDATE_BACKUP_INVALID"):
        update._backup_bound(ops_host_root, plan, "backup-1", Path("/trusted/pg"))


def test_forward_rollback_evidence_is_required(
    ops_host_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan("FORWARD_COMPATIBLE_SCHEMA")
    plan["actor_uid"] = update.os.geteuid()
    directory = update._transaction(ops_host_root, "update-1", create=True)
    update._publish_phase(directory, "plan", plan)
    apply = update._phase_record(plan, backup_id="backup-1", activation_generation="g-update")
    update._publish_phase(directory, "apply", apply)
    generation = {
        "activation_reason": "rollback",
        "update_id": "update-1",
        "plan_sha256": update._plan_digest(plan),
        "backup_id": "backup-1",
        "database_schema_head": "target",
        "compatibility_source_release_id": TO,
        "update_rollback_compatibility": "FORWARD_COMPATIBLE_SCHEMA",
        "release_id": FROM,
        "previous_release_id": TO,
        "rollback_compatibility": "APP_ONLY",
    }
    monkeypatch.setattr(update, "_active_generation", lambda _root: ("g-rollback", generation))
    monkeypatch.setattr(update, "_validate_plan", lambda _r, _l, _u, plan: plan)
    monkeypatch.setattr(
        update,
        "_release",
        lambda _root, release: {
            "rollback_compatibility": "FORWARD_COMPATIBLE_SCHEMA" if release == TO else "APP_ONLY",
            "alembic_heads": ["target" if release == TO else "source"],
            "model_manifest": {"reference": "model-ref", "status": "DEVELOPMENT_INTEGRATION"},
        },
    )
    assert update.forward_rollback_head(ops_host_root, FROM) is None
    update._publish_phase(
        directory,
        "rollback",
        update._phase_record(plan, backup_id="backup-1", activation_generation="g-rollback"),
    )
    assert update.forward_rollback_head(ops_host_root, FROM) == "target"
    generation["compatibility_source_release_id"] = FROM
    assert update.forward_rollback_head(ops_host_root, FROM) is None


@pytest.mark.parametrize("kind", ["APP_ONLY", "FORWARD_COMPATIBLE_SCHEMA"])
def test_rollback_returns_only_to_transaction_source_and_never_migrates(
    ops_host_root: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.setattr(update.platform, "system", lambda: "Darwin")
    plan = _plan(kind)
    plan["actor_uid"] = update.os.geteuid()
    directory = update._transaction(ops_host_root, "update-1", create=True)
    update._publish_phase(directory, "plan", plan)
    update._publish_phase(
        directory,
        "apply",
        update._phase_record(plan, backup_id="backup-1", activation_generation="g-update"),
    )
    monkeypatch.setattr(update, "_validate_plan", lambda *_: plan)
    monkeypatch.setattr(update, "_service_absent", lambda *_: None)
    monkeypatch.setattr(update, "_db_head", lambda _root: plan["to_alembic_head"])
    monkeypatch.setattr(update, "_release", lambda *_: {"rollback_compatibility": "APP_ONLY"})
    generation = {
        "activation_reason": "update",
        "update_id": "update-1",
        "plan_sha256": update._plan_digest(plan),
        "backup_id": "backup-1",
        "database_schema_head": plan["to_alembic_head"],
        "release_id": TO,
        "previous_release_id": FROM,
        "rollback_compatibility": kind,
    }
    state = {"generation_id": "g-update"}
    monkeypatch.setattr(
        update, "_active_generation", lambda _root: (state["generation_id"], generation)
    )
    activations: list[str] = []

    def activate(
        _root: Path,
        release: str,
        *,
        metadata: dict[str, object],
        returning_to_prior_release: bool,
    ) -> None:
        assert returning_to_prior_release
        activations.append(release)
        state["generation_id"] = "g-rollback"
        generation.clear()
        generation.update(
            {
                **metadata,
                "release_id": release,
                "previous_release_id": TO,
                "rollback_compatibility": "APP_ONLY",
            }
        )

    monkeypatch.setattr(update, "_activate_release_locked", activate)
    monkeypatch.setattr(
        update.subprocess, "run", lambda *_a, **_k: pytest.fail("downgrade invoked")
    )
    first = update.run_rollback_apply(ops_host_root, LABEL, "update-1")
    assert first.findings[0].code == "ROLLBACK_APPLIED_SERVICE_STOPPED"
    assert activations == [FROM]
    assert generation["database_schema_head"] == plan["to_alembic_head"]
    (directory / "rollback.json").unlink()
    assert update.run_rollback_apply(ops_host_root, LABEL, "update-1").findings[0].code == (
        "ROLLBACK_APPLIED_SERVICE_STOPPED"
    )
    generation["update_id"] = "foreign"
    assert update.run_rollback_apply(ops_host_root, LABEL, "update-1").findings[0].code == (
        "ROLLBACK_STATE_INVALID"
    )


def test_migration_failure_never_activates_or_leaks_stderr(
    ops_host_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(update.platform, "system", lambda: "Darwin")
    plan = _plan("FORWARD_COMPATIBLE_SCHEMA")
    plan["actor_uid"] = update.os.geteuid()
    directory = update._transaction(ops_host_root, "update-1", create=True)
    update._publish_phase(directory, "plan", plan)
    monkeypatch.setattr(update, "_validate_plan", lambda *_: plan)
    monkeypatch.setattr(update, "_service_absent", lambda *_: None)
    monkeypatch.setattr(update, "_backup_bound", lambda *_: None)
    monkeypatch.setattr(update, "verify_active_release", lambda _root: FROM)
    monkeypatch.setattr(update, "_db_head", lambda _root: "source")
    monkeypatch.setattr(
        update,
        "_activate_release_locked",
        lambda *_a, **_k: pytest.fail("activation attempted after migration failure"),
    )
    monkeypatch.setattr(
        update.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1, stdout=PASSWORD, stderr=CV),
    )
    result = update.run_update_apply(
        ops_host_root, LABEL, "update-1", "backup-1", Path("/trusted/pg")
    )
    assert result.findings[0].code == "UPDATE_SCHEMA_FAILED"
    assert PASSWORD not in result.model_dump_json()
    assert CV not in result.model_dump_json()
    assert not (directory / "apply.json").exists()


def test_receipt_fsync_failure_never_reports_completion(
    ops_host_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = update._transaction(ops_host_root, "update-1", create=True)
    real_fsync = update.os.fsync

    def fail_fsync(descriptor: int) -> None:
        if update.os.fstat(descriptor).st_mode & 0o170000 == 0o100000:
            raise OSError("synthetic secret " + PASSWORD)
        real_fsync(descriptor)

    monkeypatch.setattr(update.os, "fsync", fail_fsync)
    with pytest.raises(update.UpdateFailure) as failure:
        update._publish_phase(directory, "plan", _plan())
    assert failure.value.code == "UPDATE_PUBLICATION_STATE_UNCERTAIN"
    assert PASSWORD not in str(failure.value)


def _final_service_probes(
    root: Path, monkeypatch: pytest.MonkeyPatch, state: dict[str, str]
) -> None:
    monkeypatch.setattr(
        deployment_ready,
        "_installed_spec",
        lambda *_args: (ServiceSpec(LABEL, "meyar", root, 8000), b"canonical-plist"),
    )
    monkeypatch.setattr(
        update,
        "run_service_status",
        lambda **_kwargs: update._result("service-status", state["launchd"], ok=True),
    )
    monkeypatch.setattr(deployment_ready, "_probe_application", lambda _port: state["health"])


def test_finalize_publishes_only_after_full_readiness(
    ops_host_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(update.platform, "system", lambda: "Darwin")
    directory = update._transaction(ops_host_root, "update-1", create=True)
    plan = _plan()
    plan["actor_uid"] = update.os.geteuid()
    receipt = update._phase_record(plan, backup_id="backup-1", activation_generation="g-update")
    monkeypatch.setattr(update, "_finalize_snapshot", lambda *_a, **_k: (directory, plan, receipt))
    state = {"launchd": "SERVICE_VISIBLE", "health": "APPLICATION_LIVE"}
    _final_service_probes(ops_host_root, monkeypatch, state)
    monkeypatch.setattr(
        deployment_ready,
        "run_deployment_ready",
        lambda *_: update._result("deployment-ready", "SECRET_FAILURE"),
    )
    assert update.run_update_finalize(ops_host_root, LABEL, "update-1").findings[0].code == (
        "UPDATE_READINESS_FAILED"
    )
    assert not (directory / "finalize.json").exists()
    monkeypatch.setattr(
        deployment_ready,
        "run_deployment_ready",
        lambda *_: update._result("deployment-ready", "READY", ok=True),
    )
    assert update.run_update_finalize(ops_host_root, LABEL, "update-1").findings[0].code == (
        "UPDATE_COMPLETED"
    )
    assert (directory / "finalize.json").exists()
    state["launchd"] = "SERVICE_NOT_VISIBLE"
    assert update.run_update_finalize(ops_host_root, LABEL, "update-1").findings[0].code == (
        "UPDATE_COMPLETED"
    )


def test_forward_rollback_finalize_requires_distinct_schema_finding(
    ops_host_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(update.platform, "system", lambda: "Darwin")
    directory = update._transaction(ops_host_root, "update-1", create=True)
    plan = _plan("FORWARD_COMPATIBLE_SCHEMA")
    plan["actor_uid"] = update.os.geteuid()
    receipt = update._phase_record(plan, backup_id="backup-1", activation_generation="g-rollback")
    monkeypatch.setattr(update, "_finalize_snapshot", lambda *_a, **_k: (directory, plan, receipt))
    state = {"launchd": "SERVICE_VISIBLE", "health": "APPLICATION_LIVE"}
    _final_service_probes(ops_host_root, monkeypatch, state)
    monkeypatch.setattr(
        deployment_ready,
        "run_deployment_ready",
        lambda *_: update._result("deployment-ready", "DB_REVISION_CURRENT", ok=True),
    )
    assert update.run_rollback_finalize(ops_host_root, LABEL, "update-1").findings[0].code == (
        "ROLLBACK_READINESS_FAILED"
    )
    builder = OpsResultBuilder(action="deployment-ready")
    builder.add(
        component="db_schema",
        status=FindingStatus.OK,
        code="DB_REVISION_FORWARD_COMPATIBLE_ROLLBACK",
        message="verified",
    )
    monkeypatch.setattr(deployment_ready, "run_deployment_ready", lambda *_: builder.build())
    assert update.run_rollback_finalize(ops_host_root, LABEL, "update-1").findings[0].code == (
        "ROLLBACK_COMPLETED"
    )
    assert (directory / "rollback_finalize.json").exists()
    state["launchd"] = "SERVICE_NOT_VISIBLE"
    assert update.run_rollback_finalize(ops_host_root, LABEL, "update-1").findings[0].code == (
        "ROLLBACK_COMPLETED"
    )


@pytest.mark.parametrize(
    "rollback,phase,completed,changed",
    [
        (False, "finalize", "UPDATE_COMPLETED", "UPDATE_READINESS_CHANGED"),
        (True, "rollback_finalize", "ROLLBACK_COMPLETED", "ROLLBACK_READINESS_CHANGED"),
    ],
)
@pytest.mark.parametrize(
    "launchd",
    ["SERVICE_NOT_VISIBLE", "SERVICE_PROBE_FAILED", "LAUNCHCTL_TIMEOUT", "LAUNCHCTL_UNAVAILABLE"],
)
def test_finalize_rechecks_service_after_green_readiness(
    ops_host_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    rollback: bool,
    phase: str,
    completed: str,
    changed: str,
    launchd: str,
) -> None:
    monkeypatch.setattr(update.platform, "system", lambda: "Darwin")
    directory = update._transaction(ops_host_root, "update-1", create=True)
    plan = _plan()
    plan["actor_uid"] = update.os.geteuid()
    receipt = update._phase_record(plan, backup_id="backup-1", activation_generation="g-1")
    monkeypatch.setattr(update, "_finalize_snapshot", lambda *_a, **_k: (directory, plan, receipt))
    state = {"launchd": "SERVICE_VISIBLE", "health": "APPLICATION_LIVE"}
    _final_service_probes(ops_host_root, monkeypatch, state)

    def green_then_stop(*_args: object) -> object:
        state["launchd"] = launchd
        return update._result("deployment-ready", "READY", ok=True)

    monkeypatch.setattr(deployment_ready, "run_deployment_ready", green_then_stop)
    finalize = update.run_rollback_finalize if rollback else update.run_update_finalize
    result = finalize(ops_host_root, LABEL, "update-1")
    assert result.findings[0].code == changed
    assert result.findings[0].code != completed
    assert not (directory / f"{phase}.json").exists()


@pytest.mark.parametrize("health", ["APPLICATION_UNREACHABLE", "APPLICATION_HEALTH_INVALID"])
@pytest.mark.parametrize("rollback", [False, True])
def test_finalize_rejects_liveness_drift_after_green_readiness(
    ops_host_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    rollback: bool,
    health: str,
) -> None:
    monkeypatch.setattr(update.platform, "system", lambda: "Darwin")
    directory = update._transaction(ops_host_root, "update-1", create=True)
    plan = _plan()
    plan["actor_uid"] = update.os.geteuid()
    receipt = update._phase_record(plan, backup_id="backup-1", activation_generation="g-1")
    monkeypatch.setattr(update, "_finalize_snapshot", lambda *_a, **_k: (directory, plan, receipt))
    state = {"launchd": "SERVICE_VISIBLE", "health": "APPLICATION_LIVE"}
    _final_service_probes(ops_host_root, monkeypatch, state)

    def green_then_drift(*_args: object) -> object:
        state["health"] = health
        return update._result("deployment-ready", "READY", ok=True)

    monkeypatch.setattr(deployment_ready, "run_deployment_ready", green_then_drift)
    finalize = update.run_rollback_finalize if rollback else update.run_update_finalize
    result = finalize(ops_host_root, LABEL, "update-1")
    assert result.findings[0].code == (
        "ROLLBACK_READINESS_CHANGED" if rollback else "UPDATE_READINESS_CHANGED"
    )
    assert not (directory / ("rollback_finalize.json" if rollback else "finalize.json")).exists()
