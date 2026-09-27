"""Read-only matrix over canonical installed-host receipts; no lifecycle mutation."""

from __future__ import annotations

import asyncio
import hashlib
import os
import platform
import stat
import sys
from pathlib import Path
from typing import Any

from sqlalchemy.engine import make_url

from meyar.ops.backup import MAX_MEMBERS, MAX_STORAGE_BYTES, SAFE_ID, run_backup_verify
from meyar.ops.deployment_ready import run_deployment_ready
from meyar.ops.diagnostics import _identity, verify_diagnostics
from meyar.ops.host_config import load_host_settings
from meyar.ops.offline_host import InstallFailure, _load_json, _operation_lock
from meyar.ops.private_evidence import (
    EvidenceFailure,
    area,
    publish_bundle,
    read_json,
    verify_bundle,
)
from meyar.ops.reboot import _receipt as reboot_receipt
from meyar.ops.restore import DATABASE_IDENTIFIER, _inspect_target, _tree_hash
from meyar.ops.result import FindingStatus, OpsResult, build_single_finding_result
from meyar.ops.service_plist import validate_label
from meyar.ops.service_status import run_service_status
from meyar.ops.update import (
    UpdateFailure,
    _active_generation,
    _matches_generation,
    _read_phase,
    _receipt_matches,
    _transaction,
    _validate_plan,
    forward_rollback_head,
)

_CHECKS = (
    "platform",
    "installation",
    "services",
    "readiness",
    "reboot",
    "backup",
    "restore",
    "update_rollback",
    "https_edge",
    "diagnostics",
    "synthetic_smoke",
)


def _result(code: str, ok: bool = False) -> OpsResult:
    return build_single_finding_result(
        action="lifecycle-acceptance",
        component="acceptance",
        status=FindingStatus.OK if ok else FindingStatus.FAIL,
        code=code,
        message="lifecycle evidence recorded" if ok else "lifecycle evidence unavailable",
    )


def _check(component: str, status: str, code: str) -> dict[str, str]:
    return {"component": component, "status": status, "code": code}


def _restore(root: Path, restore_id: str, backup_id: str, identity: tuple[Any, ...]) -> bool:
    path = root / "shared/restores" / restore_id
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        return False
    receipt = read_json(path / "restore_manifest.json")
    if not bool(
        set(receipt)
        == {
            "format_version",
            "restore_id",
            "backup_id",
            "completed_at",
            "restored_by_uid",
            "source_release_id",
            "source_sha",
            "alembic_head",
            "target_database",
            "storage_file_count",
            "storage_tree_sha256",
        }
        and type(receipt.get("storage_file_count")) is int
        and 0 <= receipt["storage_file_count"] <= MAX_MEMBERS
        and type(receipt.get("restored_by_uid")) is int
        and receipt["restored_by_uid"] == os.geteuid()
        and isinstance(receipt.get("storage_tree_sha256"), str)
        and len(receipt["storage_tree_sha256"]) == 64
        and receipt.get("format_version") == 1
        and receipt.get("restore_id") == restore_id
        and receipt.get("backup_id") == backup_id
        and receipt.get("source_release_id") == identity[0]
        and receipt.get("source_sha") == identity[1]
        and receipt.get("alembic_head") == identity[2]
        and isinstance(receipt.get("target_database"), str)
        and DATABASE_IDENTIFIER.fullmatch(receipt["target_database"]) is not None
        and receipt.get("completed_at")
    ):
        return False
    storage = path / "storage"
    if not storage.is_dir() or storage.is_symlink():
        return False
    items: dict[str, str] = {}
    total = 0
    for directory, directories, files in os.walk(storage, followlinks=False):
        for name in directories + files:
            member = Path(directory) / name
            metadata = member.lstat()
            if stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != os.geteuid():
                return False
            if stat.S_ISREG(metadata.st_mode):
                if metadata.st_nlink != 1:
                    return False
                total += metadata.st_size
                if total > MAX_STORAGE_BYTES or len(items) >= MAX_MEMBERS:
                    return False
                digest = hashlib.sha256()
                with member.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                items[member.relative_to(storage).as_posix()] = digest.hexdigest()
            elif not stat.S_ISDIR(metadata.st_mode):
                return False
    if (
        len(items) != receipt["storage_file_count"]
        or _tree_hash(items) != receipt["storage_tree_sha256"]
    ):
        return False
    settings = load_host_settings(root)
    url = make_url(settings.database_url)
    if url.database == receipt["target_database"]:
        return False
    asyncio.run(
        _inspect_target(url.set(database=receipt["target_database"]), expected_head=identity[2])
    )
    return True


def _update(
    root: Path, app_label: str, update_id: str, backup_id: str, identity: tuple[Any, ...]
) -> bool:
    directory = _transaction(root, update_id)
    plan = _validate_plan(root, app_label, update_id, _read_phase(directory, "plan"))
    phases = {
        name: _read_phase(directory, name)
        for name in ("apply", "finalize", "rollback", "rollback_finalize")
    }
    if any(not _receipt_matches(value, plan) for value in phases.values()):
        return False
    apply = phases["apply"]
    rollback = phases["rollback"]
    if (
        apply is None
        or rollback is None
        or apply.get("backup_id") != backup_id
        or rollback.get("backup_id") != backup_id
    ):
        return False
    generation_id, generation = _active_generation(root)
    return bool(
        plan["from_release_id"] == identity[0]
        and plan["from_source_sha"] == identity[1]
        and (
            plan["to_alembic_head"] == identity[2]
            or (
                plan["rollback_compatibility"] == "FORWARD_COMPATIBLE_SCHEMA"
                and forward_rollback_head(root, identity[0]) == plan["to_alembic_head"]
            )
        )
        and rollback.get("activation_generation") == generation_id
        and _matches_generation(generation, plan, "rollback", backup_id)
    )


def _reboot(
    root: Path, reboot_id: str, app_label: str, ollama_label: str, identity: tuple[Any, ...]
) -> bool:
    prepare = reboot_receipt(root, reboot_id, "prepare")
    verify = reboot_receipt(root, reboot_id, "verify")
    prepared_boot = prepare["boot_time_us"]
    verified_boot = verify["boot_time_us"]
    return bool(
        verify.get("code") == "REBOOT_SURVIVAL_VERIFIED"
        and type(prepared_boot) is int
        and type(verified_boot) is int
        and verified_boot > prepared_boot
        and all(
            verify.get(key) == prepare.get(key)
            for key in (
                "release_id",
                "source_sha",
                "alembic_head",
                "model_manifest_sha256",
                "runtime_sha256",
                "app_label",
                "ollama_label",
            )
        )
        and verify.get("release_id") == identity[0]
        and verify.get("source_sha") == identity[1]
        and verify.get("alembic_head") == identity[2]
        and verify.get("model_manifest_sha256") == identity[3]
        and verify.get("runtime_sha256") == identity[4]
        and verify.get("app_label") == app_label
        and verify.get("ollama_label") == ollama_label
    )


def _edge(root: Path, edge_id: str, identity: tuple[Any, ...]) -> bool:
    manifest = verify_bundle(area(root, "edges") / edge_id, frozenset({"manifest.json"}))[
        "manifest.json"
    ]
    return bool(
        manifest.get("format_version") == 1
        and manifest.get("edge_id") == edge_id
        and manifest.get("code") == "EDGE_TLS_VERIFIED"
        and isinstance(manifest.get("origin"), str)
        and manifest["origin"].startswith("https://")
        and manifest.get("release_id") == identity[0]
        and manifest.get("source_sha") == identity[1]
        and manifest.get("model_manifest_sha256") == identity[3]
    )


def _smoke(root: Path, smoke_id: str, identity: tuple[Any, ...]) -> bool:
    receipt = verify_bundle(area(root, "smoke") / smoke_id, frozenset({"manifest.json"}))[
        "manifest.json"
    ]
    return bool(
        set(receipt)
        == {
            "format_version",
            "smoke_id",
            "status",
            "release_id",
            "source_sha",
            "disposable_database",
            "all_steps_passed",
            "step_count",
        }
        and receipt["format_version"] == 1
        and receipt["smoke_id"] == smoke_id
        and receipt["status"] == "PASS"
        and receipt["release_id"] == identity[0]
        and receipt["source_sha"] == identity[1]
        and receipt["disposable_database"] is True
        and receipt["all_steps_passed"] is True
        and type(receipt["step_count"]) is int
        and receipt["step_count"] >= 12
    )


def _diagnostics(root: Path, diagnostic_id: str, identity: tuple[Any, ...]) -> bool:
    if not verify_diagnostics(root, diagnostic_id).ok:
        return False
    files = verify_bundle(
        area(root, "diagnostics") / diagnostic_id,
        frozenset(
            {
                "manifest.json", "environment.json", "deployment.json", "services.json",
                "database.json", "ai.json", "operations.json", "filesystem.json",
            }
        ),
    )
    deployment = files["deployment.json"]
    ai = files["ai.json"]
    return bool(
        deployment["release_id"] == identity[0]
        and deployment["source_sha"] == identity[1]
        and deployment["alembic_head"] == identity[2]
        and ai["model_manifest_sha256"] == identity[3]
        and ai["runtime_sha256"] == identity[4]
        and ai["runtime_version"] == identity[8]
        and ai["llm_digest"] == identity[9]
        and ai["embedding_digest"] == identity[10]
    )


def run_lifecycle_acceptance(
    root: Path,
    app_label: str,
    ollama_label: str,
    run_id: str,
    *,
    backup_id: str | None = None,
    restore_id: str | None = None,
    update_id: str | None = None,
    reboot_id: str | None = None,
    edge_id: str | None = None,
    diagnostic_id: str | None = None,
    smoke_id: str | None = None,
    pg_bin_dir: Path | None = None,
) -> OpsResult:
    try:
        if SAFE_ID.fullmatch(run_id) is None:
            raise EvidenceFailure("ACCEPTANCE_ID_INVALID")
        validate_label(app_label)
        validate_label(ollama_label)
        with _operation_lock(root):
            first = _identity(root, app_label)
            if first[11] != ollama_label:
                raise EvidenceFailure("ACCEPTANCE_LABEL_MISMATCH")
        checks: list[dict[str, str]] = []
        platform_ok = platform.system() == "Darwin" and platform.machine() == "arm64"
        checks.append(
            _check(
                "platform",
                "PASS" if platform_ok else "FAIL",
                "PLATFORM_VERIFIED" if platform_ok else "PLATFORM_UNSUPPORTED",
            )
        )
        python_ok = (
            sys.implementation.name == "cpython"
            and sys.version_info[:2] == (3, 12)
            and Path(sys.executable).samefile(root / "current/.venv/bin/python")
        )
        checks.append(
            _check(
                "installation",
                "PASS" if python_ok else "FAIL",
                "INSTALLATION_VERIFIED" if python_ok else "PYTHON_RUNTIME_INVALID",
            )
        )
        app = run_service_status(label=app_label)
        ollama = run_service_status(label=ollama_label)
        services_ok = all(
            result.ok and result.findings[0].code == "SERVICE_VISIBLE" for result in (app, ollama)
        )
        checks.append(
            _check(
                "services",
                "PASS" if services_ok else "FAIL",
                "SERVICES_VISIBLE" if services_ok else "SERVICES_UNAVAILABLE",
            )
        )
        ready = run_deployment_ready(root, app_label)
        checks.append(
            _check(
                "readiness",
                "PASS" if ready.ok else "FAIL",
                "DEPLOYMENT_READY" if ready.ok else "DEPLOYMENT_NOT_READY",
            )
        )

        def evidence(component: str, value: str | None, verifier: Any) -> None:
            if value is None:
                checks.append(
                    _check(component, "INCOMPLETE", f"{component.upper()}_EVIDENCE_MISSING")
                )
                return
            if SAFE_ID.fullmatch(value) is None:
                checks.append(_check(component, "FAIL", f"{component.upper()}_EVIDENCE_INVALID"))
                return
            try:
                passed = verifier(value)
            except Exception:  # noqa: BLE001 - all evidence failures become fixed codes
                passed = False
            checks.append(
                _check(
                    component,
                    "PASS" if passed else "FAIL",
                    f"{component.upper()}_VERIFIED"
                    if passed
                    else f"{component.upper()}_EVIDENCE_INVALID",
                )
            )

        evidence(
            "reboot", reboot_id, lambda value: _reboot(root, value, app_label, ollama_label, first)
        )
        if backup_id is None or pg_bin_dir is None:
            checks.append(_check("backup", "INCOMPLETE", "BACKUP_EVIDENCE_MISSING"))
        elif SAFE_ID.fullmatch(backup_id) is None:
            checks.append(_check("backup", "FAIL", "BACKUP_EVIDENCE_INVALID"))
        else:
            assert backup_id is not None
            backup = run_backup_verify(root, backup_id, pg_bin_dir)
            try:
                manifest = _load_json(root / "shared/backups" / backup_id / "backup_manifest.json")
                backup_ok = (
                    backup.ok
                    and manifest.get("release_id") == first[0]
                    and manifest.get("source_sha") == first[1]
                    and manifest.get("alembic_head") == first[2]
                )
            except Exception:  # noqa: BLE001
                backup_ok = False
            checks.append(
                _check(
                    "backup",
                    "PASS" if backup_ok else "FAIL",
                    "BACKUP_VERIFIED" if backup_ok else "BACKUP_EVIDENCE_INVALID",
                )
            )
        evidence(
            "restore",
            restore_id,
            lambda value: backup_id is not None and _restore(root, value, backup_id, first),
        )
        evidence(
            "update_rollback",
            update_id,
            lambda value: (
                backup_id is not None and _update(root, app_label, value, backup_id, first)
            ),
        )
        evidence("https_edge", edge_id, lambda value: _edge(root, value, first))
        evidence("diagnostics", diagnostic_id, lambda value: _diagnostics(root, value, first))
        evidence("synthetic_smoke", smoke_id, lambda value: _smoke(root, value, first))
        with _operation_lock(root):
            if _identity(root, app_label) != first:
                raise EvidenceFailure("ACCEPTANCE_STATE_CHANGED")
            status = (
                "FAIL"
                if any(check["status"] == "FAIL" for check in checks)
                else "INCOMPLETE"
                if any(check["status"] == "INCOMPLETE" for check in checks)
                else "PASS"
            )
            if {check["component"] for check in checks} != set(_CHECKS):
                raise EvidenceFailure("ACCEPTANCE_MATRIX_INVALID")
            parent = area(root, "acceptance", create=True)
            publish_bundle(
                parent,
                run_id,
                {
                    "manifest.json": {
                        "format_version": 1,
                        "run_id": run_id,
                        "release_id": first[0],
                        "source_sha": first[1],
                    },
                    "checks.json": {"checks": checks},
                    "summary.json": {
                        "status": status,
                        "release_id": first[0],
                        "source_sha": first[1],
                        "checks": checks,
                    },
                },
            )
        return _result(f"ACCEPTANCE_{status}", status == "PASS")
    except (EvidenceFailure, InstallFailure, ValueError, OSError, UpdateFailure) as exc:
        return _result(
            exc.code
            if isinstance(exc, (EvidenceFailure, InstallFailure, UpdateFailure))
            else "ACCEPTANCE_UNAVAILABLE"
        )
    except Exception:  # noqa: BLE001 - no protected input in public output
        return _result("ACCEPTANCE_UNAVAILABLE")
