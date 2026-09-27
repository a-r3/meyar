"""Staged, install-owner application update and explicit application rollback.

No command in this module mutates launchd, restores a production database,
or performs an Alembic downgrade. All externally visible failures are fixed
codes; candidate data and protected configuration never enter receipts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import create_async_engine

from meyar.ops.ai_provision import verify_installed_models
from meyar.ops.alembic_introspect import get_db_alembic_revision
from meyar.ops.alembic_static_metadata import (
    AlembicStaticMetadataError,
    prove_static_linear_upgrade,
)
from meyar.ops.backup import (
    SAFE_ID,
    BackupFailure,
    _backups_root,
    _pg_tools,
    _publish,
    _verify_artifact,
)
from meyar.ops.host_config import load_host_settings
from meyar.ops.offline_host import (
    SAFE_RELEASE,
    InstallFailure,
    _activate_release_locked,
    _current_release,
    _load_json,
    _operation_lock,
    _real_directory,
    _verify_install,
    verify_active_release,
)
from meyar.ops.result import FindingStatus, OpsResult, build_single_finding_result
from meyar.ops.schema_init import SchemaInitFailure, _active_config
from meyar.ops.service_plist import validate_label
from meyar.ops.service_status import LaunchctlRunner, run_service_status

_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEAD = re.compile(r"[A-Za-z0-9_]{1,128}\Z")
_PHASES = frozenset({"plan", "apply", "finalize", "rollback", "rollback_finalize"})
_PLAN_KEYS = frozenset(
    {
        "format_version",
        "update_id",
        "actor_uid",
        "created_at",
        "from_release_id",
        "from_source_sha",
        "from_alembic_head",
        "to_release_id",
        "to_source_sha",
        "to_alembic_head",
        "rollback_compatibility",
        "model_manifest_reference",
        "model_approval_status",
        "app_service_label",
        "config_identity",
        "from_rollback_compatibility",
    }
)


class UpdateFailure(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _result(action: str, code: str, *, ok: bool = False) -> OpsResult:
    return build_single_finding_result(
        action=action,
        component="update" if action.startswith("update") else "rollback",
        status=FindingStatus.OK if ok else FindingStatus.FAIL,
        code=code,
        message="operation completed" if ok else "operation refused or failed",
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _check_inputs(root: Path, label: str, update_id: str) -> None:
    if platform.system() != "Darwin" or os.geteuid() <= 0:
        raise UpdateFailure("UPDATE_HOST_INVALID")
    if not root.is_absolute() or ".." in root.parts:
        raise UpdateFailure("UPDATE_HOST_INVALID")
    if SAFE_ID.fullmatch(update_id) is None:
        raise UpdateFailure("UPDATE_ID_INVALID")
    try:
        validate_label(label)
        _real_directory(root)
    except (ValueError, InstallFailure, OSError) as exc:
        raise UpdateFailure("UPDATE_HOST_INVALID") from exc


def _updates_root(root: Path, *, create: bool = False) -> Path:
    shared = root / "shared"
    _real_directory(shared)
    if shared.stat().st_uid != os.geteuid() or shared.stat().st_mode & 0o022:
        raise UpdateFailure("UPDATE_LAYOUT_UNSAFE")
    updates = shared / "updates"
    if create:
        try:
            updates.mkdir(mode=0o700)
            _sync_dir(shared)
        except FileExistsError:
            pass
    _real_directory(updates)
    metadata = updates.stat()
    if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o7777 != 0o700:
        raise UpdateFailure("UPDATE_LAYOUT_UNSAFE")
    return updates


def _transaction(root: Path, update_id: str, *, create: bool = False) -> Path:
    updates = _updates_root(root, create=create)
    target = updates / update_id
    if create:
        try:
            target.mkdir(mode=0o700)
            _sync_dir(updates)
        except FileExistsError:
            pass
    _real_directory(target)
    metadata = target.stat()
    if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o7777 != 0o700:
        raise UpdateFailure("UPDATE_LAYOUT_UNSAFE")
    return target


def _sync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_phase(directory: Path, phase: str) -> dict[str, Any] | None:
    if phase not in _PHASES:
        raise UpdateFailure("UPDATE_STATE_UNCERTAIN")
    path = directory / f"{phase}.json"
    if not os.path.lexists(path):
        return None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            metadata = os.fstat(descriptor)
            raw = os.read(descriptor, 4097)
        finally:
            os.close(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o7777 != 0o600
            or len(raw) > 4096
            or len(raw) != metadata.st_size
        ):
            raise UpdateFailure("UPDATE_STATE_UNCERTAIN")

        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate receipt field")
                value[key] = item
            return value

        payload = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(payload, dict):
            raise UpdateFailure("UPDATE_STATE_UNCERTAIN")
        if payload.get("format_version") != 1:
            raise UpdateFailure("UPDATE_STATE_UNCERTAIN")
        return payload
    except (InstallFailure, OSError, ValueError) as exc:
        raise UpdateFailure("UPDATE_STATE_UNCERTAIN") from exc


def _publish_phase(directory: Path, phase: str, payload: dict[str, Any]) -> None:
    """Exclusive publication. An uncertain fsync leaves evidence for inspection."""
    if phase not in _PHASES or _read_phase(directory, phase) is not None:
        raise UpdateFailure("UPDATE_STATE_UNCERTAIN")
    data = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(data) > 4096:
        raise UpdateFailure("UPDATE_STATE_UNCERTAIN")
    path = directory / f"{phase}.json"
    stage = directory / f".{phase}-{uuid.uuid4().hex}"
    try:
        descriptor = os.open(stage, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _publish(stage, path)
        _sync_dir(directory)
    except (OSError, BackupFailure) as exc:
        raise UpdateFailure("UPDATE_PUBLICATION_STATE_UNCERTAIN") from exc


def _sync_existing_phase(directory: Path, phase: str) -> None:
    path = directory / f"{phase}.json"
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _sync_dir(directory)


def _config_identity(root: Path) -> list[int]:
    path = root / "shared/config/.env"
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise UpdateFailure("UPDATE_PLAN_STALE")
    return [metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns]


def _release(root: Path, release_id: str) -> dict[str, Any]:
    if SAFE_RELEASE.fullmatch(release_id) is None:
        raise UpdateFailure("UPDATE_TARGET_INVALID")
    state = _verify_install(root, release_id)
    manifest = _load_json(root / "releases" / release_id / "release_manifest.json")
    heads = manifest.get("alembic_heads")
    model = manifest.get("model_manifest")
    if (
        manifest.get("release_id") != release_id
        or not isinstance(manifest.get("source_sha"), str)
        or _HEX40.fullmatch(str(manifest["source_sha"])) is None
        or not isinstance(heads, list)
        or len(heads) != 1
        or not isinstance(heads[0], str)
        or _HEAD.fullmatch(heads[0]) is None
        or not isinstance(model, dict)
        or not isinstance(model.get("reference"), str)
        or not isinstance(model.get("status"), str)
        or manifest.get("rollback_compatibility") != state.get("rollback_compatibility")
    ):
        raise UpdateFailure("UPDATE_TARGET_INVALID")
    return manifest


def _graph_proven(root: Path, release_id: str, source: str, target: str) -> bool:
    try:
        versions = root / "releases" / release_id / "backend/alembic/versions"
        _real_directory(versions)
        files: dict[str, bytes] = {}
        for path in versions.iterdir():
            if path.suffix != ".py":
                continue
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1024 * 1024:
                return False
            files[f"backend/alembic/versions/{path.name}"] = path.read_bytes()
        if len(files) > 1000:
            return False
        return prove_static_linear_upgrade(files, source, target)
    except (AlembicStaticMetadataError, InstallFailure, OSError):
        return False


def _plan_fields(root: Path, label: str, update_id: str, target_id: str) -> dict[str, Any]:
    try:
        _, installed_head = _active_config(root, implementation_file=Path(__file__))
    except SchemaInitFailure as exc:
        raise UpdateFailure("UPDATE_TARGET_INVALID") from exc
    current_id = verify_active_release(root)
    if current_id == target_id:
        raise UpdateFailure("UPDATE_TARGET_INVALID")
    current = _release(root, current_id)
    target = _release(root, target_id)
    kind = target["rollback_compatibility"]
    if kind == "BACKUP_RESTORE_REQUIRED":
        raise UpdateFailure("UPDATE_REQUIRES_RESTORE_PROCEDURE")
    if kind == "PROHIBITED_PENDING_PROCEDURE":
        raise UpdateFailure("UPDATE_PROHIBITED_PENDING_PROCEDURE")
    if kind not in {"APP_ONLY", "FORWARD_COMPATIBLE_SCHEMA"}:
        raise UpdateFailure("UPDATE_TARGET_INVALID")
    from_head, to_head = current["alembic_heads"][0], target["alembic_heads"][0]
    if from_head != installed_head:
        raise UpdateFailure("UPDATE_TARGET_INVALID")
    if kind == "APP_ONLY" and from_head != to_head:
        raise UpdateFailure("UPDATE_SCHEMA_PATH_INVALID")
    if not _graph_proven(root, target_id, from_head, to_head):
        raise UpdateFailure("UPDATE_SCHEMA_PATH_INVALID")
    if current["model_manifest"] != target["model_manifest"]:
        raise UpdateFailure("UPDATE_MODEL_CHANGE_UNSUPPORTED")
    settings = load_host_settings(root)
    verify_installed_models(root, settings, probe=False)
    return {
        "format_version": 1,
        "update_id": update_id,
        "actor_uid": os.geteuid(),
        "from_release_id": current_id,
        "from_source_sha": current["source_sha"],
        "from_alembic_head": from_head,
        "to_release_id": target_id,
        "to_source_sha": target["source_sha"],
        "to_alembic_head": to_head,
        "rollback_compatibility": kind,
        "from_rollback_compatibility": current["rollback_compatibility"],
        "model_manifest_reference": current["model_manifest"]["reference"],
        "model_approval_status": current["model_manifest"]["status"],
        "app_service_label": label,
        "config_identity": _config_identity(root),
    }


def _active_generation(root: Path) -> tuple[str, dict[str, Any]]:
    _current_release(root)
    link = os.readlink(root / "current")
    generation = Path(link).parts[1]
    return generation, _load_json(root / "activations" / generation / "state.json")


def _plan_digest(plan: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _validate_plan(
    root: Path, label: str, update_id: str, plan: dict[str, Any] | None
) -> dict[str, Any]:
    try:
        _active_config(root, implementation_file=Path(__file__))
    except SchemaInitFailure as exc:
        raise UpdateFailure("UPDATE_STATE_UNCERTAIN") from exc
    if (
        plan is None
        or set(plan) != _PLAN_KEYS
        or plan.get("update_id") != update_id
        or plan.get("actor_uid") != os.geteuid()
        or plan.get("app_service_label") != label
        or plan.get("from_release_id") == plan.get("to_release_id")
        or plan.get("rollback_compatibility") not in {"APP_ONLY", "FORWARD_COMPATIBLE_SCHEMA"}
        or plan.get("from_rollback_compatibility")
        not in {
            "APP_ONLY",
            "FORWARD_COMPATIBLE_SCHEMA",
            "BACKUP_RESTORE_REQUIRED",
            "PROHIBITED_PENDING_PROCEDURE",
        }
        or type(plan.get("config_identity")) is not list
        or len(plan["config_identity"]) != 4
        or any(type(item) is not int or item < 0 for item in plan["config_identity"])
        or not isinstance(plan.get("created_at"), str)
        or any(
            not isinstance(plan.get(key), str) or _HEX40.fullmatch(plan[key]) is None
            for key in ("from_source_sha", "to_source_sha")
        )
        or any(
            not isinstance(plan.get(key), str) or _HEAD.fullmatch(plan[key]) is None
            for key in ("from_alembic_head", "to_alembic_head")
        )
        or not isinstance(plan.get("model_manifest_reference"), str)
        or not isinstance(plan.get("model_approval_status"), str)
        or any(
            SAFE_RELEASE.fullmatch(str(plan.get(key))) is None
            for key in ("from_release_id", "to_release_id")
        )
    ):
        raise UpdateFailure("UPDATE_STATE_UNCERTAIN")
    try:
        created = datetime.fromisoformat(plan["created_at"])
        if created.tzinfo is None or created.utcoffset() is None:
            raise ValueError("naive timestamp")
    except ValueError as exc:
        raise UpdateFailure("UPDATE_STATE_UNCERTAIN") from exc
    if _config_identity(root) != plan["config_identity"]:
        raise UpdateFailure("UPDATE_PLAN_STALE")
    verify_installed_models(root, load_host_settings(root), probe=False)
    for prefix in ("from", "to"):
        manifest = _release(root, plan[f"{prefix}_release_id"])
        if manifest["source_sha"] != plan[f"{prefix}_source_sha"] or manifest["alembic_heads"] != [
            plan[f"{prefix}_alembic_head"]
        ]:
            raise UpdateFailure("UPDATE_PLAN_STALE")
    target = _release(root, plan["to_release_id"])
    source = _release(root, plan["from_release_id"])
    if (
        target["rollback_compatibility"] != plan["rollback_compatibility"]
        or source["rollback_compatibility"] != plan["from_rollback_compatibility"]
        or source["model_manifest"] != target["model_manifest"]
        or target["model_manifest"]
        != {
            "reference": plan["model_manifest_reference"],
            "status": plan["model_approval_status"],
        }
    ):
        raise UpdateFailure("UPDATE_PLAN_STALE")
    return plan


def _previous_transaction_complete(root: Path) -> bool:
    generation_id, generation = _active_generation(root)
    reason = generation.get("activation_reason")
    if reason not in {"update", "rollback"}:
        return True
    update_id = generation.get("update_id")
    if not isinstance(update_id, str) or SAFE_ID.fullmatch(update_id) is None:
        return False
    try:
        directory = _transaction(root, update_id)
        plan = _read_phase(directory, "plan")
        if plan is None or plan.get("update_id") != update_id:
            return False
        apply = _read_phase(directory, "apply")
        if apply is None or not _receipt_matches(apply, plan):
            return False
        backup_id = apply.get("backup_id")
        if not isinstance(backup_id, str) or SAFE_ID.fullmatch(backup_id) is None:
            return False
        if reason == "rollback":
            # A forward-compatible rollback needs explicit reconciliation;
            # a later update must never guess its starting schema.
            rollback = _read_phase(directory, "rollback")
            return (
                plan.get("rollback_compatibility") == "APP_ONLY"
                and rollback is not None
                and _receipt_matches(rollback, plan)
                and rollback.get("activation_generation") == generation_id
                and _matches_generation(generation, plan, "rollback", backup_id)
                and _receipt_matches(_read_phase(directory, "rollback_finalize"), plan)
            )
        return (
            apply.get("activation_generation") == generation_id
            and _matches_generation(generation, plan, "update", backup_id)
            and _receipt_matches(_read_phase(directory, "finalize"), plan)
        )
    except (UpdateFailure, InstallFailure, OSError, KeyError, TypeError, ValueError):
        return False


def _service_absent(label: str, runner: LaunchctlRunner | None) -> None:
    result = run_service_status(label=label, platform_system="Darwin", runner=runner)
    if result.findings[0].code != "SERVICE_NOT_VISIBLE":
        raise UpdateFailure("UPDATE_SERVICE_STATE_UNCERTAIN")


async def _query_db(database_url: str) -> str | None:
    engine = create_async_engine(database_url, connect_args={"timeout": 5.0})
    try:
        async with asyncio.timeout(10):
            result = await get_db_alembic_revision(engine)
        return result.revisions[0] if result.table_exists and len(result.revisions) == 1 else None
    finally:
        await engine.dispose()


def _db_head(root: Path) -> str | None:
    settings = load_host_settings(root)
    try:
        return asyncio.run(_query_db(settings.database_url))
    except Exception:  # noqa: BLE001 - driver text may contain credentials
        return None


def _backup_bound(root: Path, plan: dict[str, Any], backup_id: str, pg_bin_dir: Path) -> None:
    try:
        if SAFE_ID.fullmatch(backup_id) is None:
            raise BackupFailure("BACKUP_ID_INVALID")
        backups = _backups_root(root, os.geteuid())
        _, pg_restore = _pg_tools(pg_bin_dir, os.geteuid())
        directory = backups / backup_id
        _verify_artifact(directory, backup_id, pg_restore)
        backup = _load_json(directory / "backup_manifest.json")
        if (
            backup.get("release_id") != plan["from_release_id"]
            or backup.get("source_sha") != plan["from_source_sha"]
            or backup.get("alembic_head") != plan["from_alembic_head"]
            or datetime.fromisoformat(str(backup["created_at"]))
            < datetime.fromisoformat(plan["created_at"])
        ):
            raise BackupFailure("BACKUP_IDENTITY_MISMATCH")
    except (BackupFailure, InstallFailure, OSError, KeyError, ValueError, TypeError) as exc:
        raise UpdateFailure("UPDATE_BACKUP_INVALID") from exc


def _phase_record(plan: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {
        "format_version": 1,
        "update_id": plan["update_id"],
        "actor_uid": os.geteuid(),
        "completed_at": _now(),
        "plan_sha256": _plan_digest(plan),
        **fields,
    }


def _receipt_matches(receipt: dict[str, Any] | None, plan: dict[str, Any]) -> bool:
    return bool(
        receipt is not None
        and receipt.get("format_version") == 1
        and receipt.get("update_id") == plan["update_id"]
        and receipt.get("actor_uid") == plan["actor_uid"]
        and receipt.get("plan_sha256") == _plan_digest(plan)
        and isinstance(receipt.get("completed_at"), str)
    )


def _matches_generation(
    generation: dict[str, Any], plan: dict[str, Any], reason: str, backup_id: str
) -> bool:
    expected = {
        "activation_reason": reason,
        "update_id": plan["update_id"],
        "plan_sha256": _plan_digest(plan),
        "backup_id": backup_id,
        "database_schema_head": plan["to_alembic_head"],
        "compatibility_source_release_id": plan["to_release_id"] if reason == "rollback" else None,
        "update_rollback_compatibility": plan["rollback_compatibility"]
        if reason == "rollback"
        else None,
    }
    return all(generation.get(key) == value for key, value in expected.items()) and (
        generation.get("release_id")
        == (plan["to_release_id"] if reason == "update" else plan["from_release_id"])
        and generation.get("previous_release_id")
        == (plan["from_release_id"] if reason == "update" else plan["to_release_id"])
        and generation.get("rollback_compatibility")
        == (plan["rollback_compatibility"] if reason == "update" else _release_class(plan))
    )


def _release_class(plan: dict[str, Any]) -> str:
    # Generation's classification is the destination release's own class;
    # rollback's exceptional permission is separately bound in metadata.
    return str(plan["from_rollback_compatibility"])


def run_update_prepare(root: Path, label: str, update_id: str, target_id: str) -> OpsResult:
    action = "update-prepare"
    try:
        _check_inputs(root, label, update_id)
        with _operation_lock(root):
            if not _previous_transaction_complete(root):
                raise UpdateFailure("UPDATE_PREVIOUS_TRANSACTION_INCOMPLETE")
            fields = _plan_fields(root, label, update_id, target_id)
            directory = _transaction(root, update_id, create=True)
            existing = _read_phase(directory, "plan")
            if existing is not None:
                if {key: existing.get(key) for key in fields} != fields:
                    raise UpdateFailure("UPDATE_ID_CONFLICT")
                _sync_existing_phase(directory, "plan")
                return _result(action, "UPDATE_ALREADY_PREPARED", ok=True)
        # Readiness performs long external probes and takes the lock itself.
        from meyar.ops.deployment_ready import run_deployment_ready

        if not run_deployment_ready(root, label).ok:
            raise UpdateFailure("UPDATE_READINESS_FAILED")
        with _operation_lock(root):
            if not _previous_transaction_complete(root):
                raise UpdateFailure("UPDATE_PREVIOUS_TRANSACTION_INCOMPLETE")
            if _plan_fields(root, label, update_id, target_id) != fields:
                raise UpdateFailure("UPDATE_PLAN_STALE")
            if _read_phase(directory, "plan") is not None:
                raise UpdateFailure("UPDATE_ID_CONFLICT")
            _publish_phase(directory, "plan", {**fields, "created_at": _now()})
        return _result(action, "UPDATE_PREPARED", ok=True)
    except (UpdateFailure, InstallFailure) as exc:
        return _result(
            action, exc.code if isinstance(exc, UpdateFailure) else "UPDATE_TARGET_INVALID"
        )
    except Exception:  # noqa: BLE001 - never expose config/model/probe text
        return _result(action, "UPDATE_PREPARE_FAILED")


def run_update_apply(
    root: Path,
    label: str,
    update_id: str,
    backup_id: str,
    pg_bin_dir: Path,
    *,
    runner: LaunchctlRunner | None = None,
) -> OpsResult:
    action = "update-apply"
    try:
        _check_inputs(root, label, update_id)
        with _operation_lock(root):
            directory = _transaction(root, update_id)
            plan = _validate_plan(root, label, update_id, _read_phase(directory, "plan"))
            _service_absent(label, runner)
            _backup_bound(root, plan, backup_id, pg_bin_dir)
            existing = _read_phase(directory, "apply")
            active = verify_active_release(root)
            if active == plan["to_release_id"]:
                generation_id, generation = _active_generation(root)
                if not _matches_generation(generation, plan, "update", backup_id):
                    raise UpdateFailure("UPDATE_STATE_UNCERTAIN")
                if _db_head(root) != plan["to_alembic_head"]:
                    raise UpdateFailure("UPDATE_SCHEMA_STATE_UNCERTAIN")
                if existing is None:
                    _publish_phase(
                        directory,
                        "apply",
                        _phase_record(
                            plan, backup_id=backup_id, activation_generation=generation_id
                        ),
                    )
                elif (
                    not _receipt_matches(existing, plan)
                    or existing.get("backup_id") != backup_id
                    or existing.get("activation_generation") != generation_id
                ):
                    raise UpdateFailure("UPDATE_STATE_UNCERTAIN")
                else:
                    _sync_existing_phase(directory, "apply")
                return _result(action, "UPDATE_APPLIED_SERVICE_STOPPED", ok=True)
            if active != plan["from_release_id"] or existing is not None:
                raise UpdateFailure("UPDATE_STATE_UNCERTAIN")
            state = _db_head(root)
            source, target = plan["from_alembic_head"], plan["to_alembic_head"]
            if plan["rollback_compatibility"] == "APP_ONLY":
                if state != source or source != target:
                    raise UpdateFailure("UPDATE_SCHEMA_STATE_UNCERTAIN")
            elif state == source and source != target:
                python = root / "releases" / plan["to_release_id"] / ".venv/bin/python"
                argv = [
                    str(python),
                    "-m",
                    "meyar.ops.update_worker",
                    "--install-root",
                    str(root),
                    "--target-release-id",
                    plan["to_release_id"],
                    "--from-head",
                    source,
                    "--to-head",
                    target,
                ]
                try:
                    completed = subprocess.run(
                        argv,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                        timeout=600,
                        env={
                            "PATH": "/usr/bin:/bin",
                            "HOME": "/var/empty",
                            "PYTHONNOUSERSITE": "1",
                        },
                    )
                except (OSError, subprocess.TimeoutExpired):
                    completed = None
                after = _db_head(root)
                if completed is None or completed.returncode != 0:
                    raise UpdateFailure(
                        "UPDATE_SCHEMA_FAILED"
                        if after == source
                        else "UPDATE_SCHEMA_STATE_UNCERTAIN"
                    )
                if after != target:
                    raise UpdateFailure("UPDATE_SCHEMA_POSTCHECK_FAILED")
            elif state != target or plan["rollback_compatibility"] != "FORWARD_COMPATIBLE_SCHEMA":
                raise UpdateFailure("UPDATE_SCHEMA_STATE_UNCERTAIN")
            # The target schema with the old pointer is an explicitly supported
            # recovery state for this exact plan; migration is not rerun.
            if _db_head(root) != target:
                raise UpdateFailure("UPDATE_SCHEMA_POSTCHECK_FAILED")
            _activate_release_locked(
                root,
                plan["to_release_id"],
                metadata={
                    "activation_reason": "update",
                    "update_id": update_id,
                    "plan_sha256": _plan_digest(plan),
                    "backup_id": backup_id,
                    "database_schema_head": target,
                },
            )
            generation_id, generation = _active_generation(root)
            if not _matches_generation(generation, plan, "update", backup_id):
                raise UpdateFailure("UPDATE_STATE_UNCERTAIN")
            _publish_phase(
                directory,
                "apply",
                _phase_record(plan, backup_id=backup_id, activation_generation=generation_id),
            )
        return _result(action, "UPDATE_APPLIED_SERVICE_STOPPED", ok=True)
    except UpdateFailure as exc:
        return _result(action, exc.code)
    except Exception:  # noqa: BLE001 - migration/backup/driver text may contain secrets
        return _result(action, "UPDATE_STATE_UNCERTAIN")


def _finalize_snapshot(
    root: Path, label: str, update_id: str, *, rollback: bool
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    directory = _transaction(root, update_id)
    plan = _validate_plan(root, label, update_id, _read_phase(directory, "plan"))
    apply = _read_phase(directory, "apply")
    if apply is None or not _receipt_matches(apply, plan):
        raise UpdateFailure("ROLLBACK_STATE_INVALID" if rollback else "UPDATE_STATE_UNCERTAIN")
    phase = "rollback" if rollback else "apply"
    receipt = _read_phase(directory, phase)
    generation_id, generation = _active_generation(root)
    if (
        receipt is None
        or not _receipt_matches(receipt, plan)
        or receipt.get("activation_generation") != generation_id
        or not _matches_generation(
            generation, plan, "rollback" if rollback else "update", str(apply["backup_id"])
        )
        or _db_head(root) != plan["to_alembic_head"]
    ):
        raise UpdateFailure("ROLLBACK_STATE_INVALID" if rollback else "UPDATE_STATE_UNCERTAIN")
    settings = load_host_settings(root)
    verify_installed_models(root, settings, probe=True)
    return directory, plan, receipt


def _finalize(root: Path, label: str, update_id: str, *, rollback: bool) -> OpsResult:
    action = "rollback-finalize" if rollback else "update-finalize"
    phase = "rollback_finalize" if rollback else "finalize"
    try:
        _check_inputs(root, label, update_id)
        with _operation_lock(root):
            directory, plan, receipt = _finalize_snapshot(root, label, update_id, rollback=rollback)
            existing = _read_phase(directory, phase)
            if existing is not None:
                if not _receipt_matches(existing, plan):
                    raise UpdateFailure(
                        "ROLLBACK_STATE_INVALID" if rollback else "UPDATE_STATE_UNCERTAIN"
                    )
                _sync_existing_phase(directory, phase)
                return _result(
                    action, "ROLLBACK_COMPLETED" if rollback else "UPDATE_COMPLETED", ok=True
                )
        from meyar.ops.deployment_ready import run_deployment_ready

        ready = run_deployment_ready(root, label)
        if not ready.ok:
            raise UpdateFailure(
                "ROLLBACK_READINESS_FAILED" if rollback else "UPDATE_READINESS_FAILED"
            )
        if rollback and plan["rollback_compatibility"] == "FORWARD_COMPATIBLE_SCHEMA":
            if not any(
                finding.code == "DB_REVISION_FORWARD_COMPATIBLE_ROLLBACK"
                for finding in ready.findings
            ):
                raise UpdateFailure("ROLLBACK_READINESS_FAILED")
        with _operation_lock(root):
            if _finalize_snapshot(root, label, update_id, rollback=rollback) != (
                directory,
                plan,
                receipt,
            ):
                raise UpdateFailure(
                    "ROLLBACK_STATE_INVALID" if rollback else "UPDATE_STATE_UNCERTAIN"
                )
            _publish_phase(directory, phase, _phase_record(plan))
        return _result(action, "ROLLBACK_COMPLETED" if rollback else "UPDATE_COMPLETED", ok=True)
    except UpdateFailure as exc:
        return _result(action, exc.code)
    except Exception:  # noqa: BLE001 - protected settings/probes may include secrets
        return _result(action, "ROLLBACK_STATE_INVALID" if rollback else "UPDATE_STATE_UNCERTAIN")


def run_update_finalize(root: Path, label: str, update_id: str) -> OpsResult:
    return _finalize(root, label, update_id, rollback=False)


def run_rollback_apply(
    root: Path, label: str, update_id: str, *, runner: LaunchctlRunner | None = None
) -> OpsResult:
    action = "rollback-apply"
    try:
        _check_inputs(root, label, update_id)
        with _operation_lock(root):
            directory = _transaction(root, update_id)
            plan = _validate_plan(root, label, update_id, _read_phase(directory, "plan"))
            apply = _read_phase(directory, "apply")
            if apply is None or not _receipt_matches(apply, plan):
                raise UpdateFailure("ROLLBACK_NOT_ALLOWED")
            backup_id = apply.get("backup_id")
            if not isinstance(backup_id, str) or SAFE_ID.fullmatch(backup_id) is None:
                raise UpdateFailure("ROLLBACK_STATE_INVALID")
            _service_absent(label, runner)
            if _db_head(root) != plan["to_alembic_head"]:
                raise UpdateFailure("ROLLBACK_STATE_INVALID")
            generation_id, generation = _active_generation(root)
            existing = _read_phase(directory, "rollback")
            if generation.get("release_id") == plan["from_release_id"]:
                if not _matches_generation(generation, plan, "rollback", backup_id):
                    raise UpdateFailure("ROLLBACK_STATE_INVALID")
                if existing is None:
                    _publish_phase(
                        directory,
                        "rollback",
                        _phase_record(
                            plan, backup_id=backup_id, activation_generation=generation_id
                        ),
                    )
                elif (
                    not _receipt_matches(existing, plan)
                    or existing.get("activation_generation") != generation_id
                ):
                    raise UpdateFailure("ROLLBACK_STATE_INVALID")
                else:
                    _sync_existing_phase(directory, "rollback")
                return _result(action, "ROLLBACK_APPLIED_SERVICE_STOPPED", ok=True)
            if (
                existing is not None
                or generation.get("release_id") != plan["to_release_id"]
                or generation_id != apply.get("activation_generation")
                or not _matches_generation(generation, plan, "update", backup_id)
            ):
                raise UpdateFailure("ROLLBACK_STATE_INVALID")
            source_class = _release(root, plan["from_release_id"])["rollback_compatibility"]
            _activate_release_locked(
                root,
                plan["from_release_id"],
                returning_to_prior_release=True,
                metadata={
                    "activation_reason": "rollback",
                    "update_id": update_id,
                    "plan_sha256": _plan_digest(plan),
                    "backup_id": backup_id,
                    "database_schema_head": plan["to_alembic_head"],
                    "compatibility_source_release_id": plan["to_release_id"],
                    "update_rollback_compatibility": plan["rollback_compatibility"],
                },
            )
            generation_id, generation = _active_generation(root)
            if generation.get("rollback_compatibility") != source_class or not _matches_generation(
                generation, plan, "rollback", backup_id
            ):
                raise UpdateFailure("ROLLBACK_STATE_INVALID")
            _publish_phase(
                directory,
                "rollback",
                _phase_record(plan, backup_id=backup_id, activation_generation=generation_id),
            )
        return _result(action, "ROLLBACK_APPLIED_SERVICE_STOPPED", ok=True)
    except UpdateFailure as exc:
        return _result(action, exc.code)
    except Exception:  # noqa: BLE001 - never expose protected state
        return _result(action, "ROLLBACK_STATE_INVALID")


def run_rollback_finalize(root: Path, label: str, update_id: str) -> OpsResult:
    return _finalize(root, label, update_id, rollback=True)


def forward_rollback_head(root: Path, active_id: str) -> str | None:
    """Return the one permitted newer DB head, only with complete evidence."""
    try:
        generation_id, generation = _active_generation(root)
        update_id = generation.get("update_id")
        if (
            generation.get("activation_reason") != "rollback"
            or not isinstance(update_id, str)
            or SAFE_ID.fullmatch(update_id) is None
        ):
            return None
        directory = _transaction(root, update_id)
        plan = _read_phase(directory, "plan")
        receipt = _read_phase(directory, "rollback")
        apply = _read_phase(directory, "apply")
        if (
            plan is None
            or receipt is None
            or apply is None
            or set(plan) != _PLAN_KEYS
            or plan["rollback_compatibility"] != "FORWARD_COMPATIBLE_SCHEMA"
            or active_id != plan["from_release_id"]
            or receipt.get("activation_generation") != generation_id
            or not _receipt_matches(receipt, plan)
            or not _receipt_matches(apply, plan)
            or not _matches_generation(generation, plan, "rollback", str(apply.get("backup_id")))
        ):
            return None
        _validate_plan(root, str(plan["app_service_label"]), update_id, plan)
        source = _release(root, plan["to_release_id"])
        previous = _release(root, active_id)
        if (
            source["rollback_compatibility"] != "FORWARD_COMPATIBLE_SCHEMA"
            or source["alembic_heads"] != [plan["to_alembic_head"]]
            or previous["alembic_heads"] != [plan["from_alembic_head"]]
            or source["model_manifest"] != previous["model_manifest"]
        ):
            return None
        return str(plan["to_alembic_head"])
    except (UpdateFailure, InstallFailure, OSError, ValueError, KeyError, TypeError):
        return None
