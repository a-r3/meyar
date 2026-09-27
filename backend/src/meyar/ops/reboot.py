"""Human-operated, two-boot survival proof. Never initiates a reboot."""

from __future__ import annotations

import os
import platform
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from meyar.ops.ai_provision import verify_installed_models
from meyar.ops.backup import SAFE_ID
from meyar.ops.deployment_ready import run_deployment_ready
from meyar.ops.diagnostics import _identity
from meyar.ops.host_config import load_host_settings
from meyar.ops.offline_host import InstallFailure, _operation_lock
from meyar.ops.private_evidence import EvidenceFailure, area, publish_bundle, verify_bundle
from meyar.ops.result import FindingStatus, OpsResult, build_single_finding_result
from meyar.ops.service_plist import validate_label
from meyar.ops.service_status import run_service_status


def _boot_time() -> int:
    completed = subprocess.run(
        ["/usr/sbin/sysctl", "-n", "kern.boottime"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=3,
        check=False,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
    )
    if completed.returncode != 0 or len(completed.stdout) > 256:
        raise ValueError("boot authority unavailable")
    match = re.match(rb"^\{ sec = ([0-9]{1,12}), usec = ([0-9]{1,6}) \}", completed.stdout)
    if match is None:
        raise ValueError("boot authority invalid")
    return int(match.group(1)) * 1_000_000 + int(match.group(2))


def _result(action: str, code: str, ok: bool = False) -> OpsResult:
    return build_single_finding_result(
        action=action,
        component="reboot",
        status=FindingStatus.OK if ok else FindingStatus.FAIL,
        code=code,
        message="reboot proof verified" if ok else "reboot proof refused or failed",
    )


def _state(root: Path, app_label: str, ollama_label: str) -> dict[str, object]:
    identity = _identity(root, app_label)
    if identity[11] != ollama_label:
        raise EvidenceFailure("REBOOT_LABEL_MISMATCH")
    release_id, source_sha, head, model_digest, runtime_sha = identity[:5]
    return {
        "release_id": release_id,
        "source_sha": source_sha,
        "alembic_head": head,
        "model_manifest_sha256": model_digest,
        "runtime_sha256": runtime_sha,
        "app_label": app_label,
        "ollama_label": ollama_label,
    }


def _receipt(root: Path, reboot_id: str, phase: str) -> dict[str, object]:
    files = verify_bundle(
        area(root, "reboots") / f"{reboot_id}-{phase}", frozenset({"manifest.json"})
    )
    manifest = files["manifest.json"]
    common = {
        "format_version",
        "reboot_id",
        "phase",
        "boot_time_us",
        "release_id",
        "source_sha",
        "alembic_head",
        "model_manifest_sha256",
        "runtime_sha256",
        "app_label",
        "ollama_label",
    }
    phase_fields = (
        {"prepared_at", "operator_uid"} if phase == "prepare" else {"verified_at", "code"}
    )
    if (
        phase not in {"prepare", "verify"}
        or set(manifest) != common | phase_fields
        or manifest.get("format_version") != 1
        or manifest.get("reboot_id") != reboot_id
        or manifest.get("phase") != phase
        or type(manifest.get("boot_time_us")) is not int
        or manifest["boot_time_us"] <= 0
        or not isinstance(manifest.get("release_id"), str)
        or re.fullmatch(r"meyar-[A-Za-z0-9.+_-]{1,80}\+[0-9a-f]{12}", manifest["release_id"])
        is None
        or not isinstance(manifest.get("source_sha"), str)
        or re.fullmatch(r"[0-9a-f]{40}", manifest["source_sha"]) is None
        or not isinstance(manifest.get("alembic_head"), str)
        or re.fullmatch(r"[A-Za-z0-9_]{1,128}", manifest["alembic_head"]) is None
        or any(
            not isinstance(manifest.get(key), str)
            or re.fullmatch(r"[0-9a-f]{64}", manifest[key]) is None
            for key in ("model_manifest_sha256", "runtime_sha256")
        )
        or (phase == "prepare" and type(manifest.get("operator_uid")) is not int)
        or (phase == "verify" and manifest.get("code") != "REBOOT_SURVIVAL_VERIFIED")
    ):
        raise EvidenceFailure("REBOOT_RECEIPT_INVALID")
    try:
        validate_label(str(manifest["app_label"]))
        validate_label(str(manifest["ollama_label"]))
        timestamp = manifest["prepared_at" if phase == "prepare" else "verified_at"]
        if not isinstance(timestamp, str) or len(timestamp) > 40:
            raise ValueError
        when = datetime.fromisoformat(timestamp)
        if when.tzinfo is None or when.utcoffset() is None:
            raise ValueError
    except (ValueError, TypeError, KeyError) as exc:
        raise EvidenceFailure("REBOOT_RECEIPT_INVALID") from exc
    return manifest


def run_reboot_prepare(
    root: Path,
    app_label: str,
    ollama_label: str,
    reboot_id: str,
    *,
    boot_reader: Callable[[], int] = _boot_time,
    platform_system: str | None = None,
) -> OpsResult:
    try:
        if (platform_system or platform.system()) != "Darwin" or SAFE_ID.fullmatch(
            reboot_id
        ) is None:
            raise EvidenceFailure("REBOOT_INPUT_INVALID")
        validate_label(app_label)
        validate_label(ollama_label)
        boot = boot_reader()
        if boot <= 0:
            raise ValueError("invalid boot")
        with _operation_lock(root):
            state = _state(root, app_label, ollama_label)
            parent = area(root, "reboots", create=True)
            publish_bundle(
                parent,
                f"{reboot_id}-prepare",
                {
                    "manifest.json": {
                        "format_version": 1,
                        "reboot_id": reboot_id,
                        "phase": "prepare",
                        "boot_time_us": boot,
                        "prepared_at": datetime.now(UTC).isoformat(),
                        "operator_uid": os.geteuid(),
                        **state,
                    }
                },
            )
        return _result("reboot-prepare", "REBOOT_PREPARED", True)
    except (ValueError, OSError, EvidenceFailure, InstallFailure):
        return _result("reboot-prepare", "REBOOT_PREPARE_FAILED")
    except Exception:  # noqa: BLE001 - never expose settings
        return _result("reboot-prepare", "REBOOT_PREPARE_FAILED")


def run_reboot_verify(
    root: Path,
    app_label: str,
    ollama_label: str,
    reboot_id: str,
    *,
    boot_reader: Callable[[], int] = _boot_time,
    platform_system: str | None = None,
) -> OpsResult:
    try:
        if (platform_system or platform.system()) != "Darwin" or SAFE_ID.fullmatch(
            reboot_id
        ) is None:
            raise EvidenceFailure("REBOOT_INPUT_INVALID")
        validate_label(app_label)
        validate_label(ollama_label)
        prepare = _receipt(root, reboot_id, "prepare")
        boot = boot_reader()
        prepared_boot = prepare["boot_time_us"]
        if type(prepared_boot) is not int or boot <= prepared_boot:
            raise EvidenceFailure("REBOOT_NOT_OBSERVED")
        with _operation_lock(root):
            state = _state(root, app_label, ollama_label)
        if any(prepare.get(key) != value for key, value in state.items()):
            raise EvidenceFailure("REBOOT_STATE_CHANGED")
        for label in (app_label, ollama_label):
            result = run_service_status(label=label)
            if not result.ok or result.findings[0].code != "SERVICE_VISIBLE":
                raise EvidenceFailure("REBOOT_SERVICE_UNAVAILABLE")
        ready = run_deployment_ready(root, app_label)
        if not ready.ok:
            raise EvidenceFailure("REBOOT_READINESS_FAILED")
        verify_installed_models(root, load_host_settings(root), probe=True)
        final_boot = boot_reader()
        with _operation_lock(root):
            if _state(root, app_label, ollama_label) != state or final_boot != boot:
                raise EvidenceFailure("REBOOT_STATE_CHANGED")
            # Read the prepare receipt again under the publication lock.
            if _receipt(root, reboot_id, "prepare") != prepare:
                raise EvidenceFailure("REBOOT_RECEIPT_INVALID")
            parent = area(root, "reboots", create=True)
            publish_bundle(
                parent,
                f"{reboot_id}-verify",
                {
                    "manifest.json": {
                        "format_version": 1,
                        "reboot_id": reboot_id,
                        "phase": "verify",
                        "boot_time_us": boot,
                        "verified_at": datetime.now(UTC).isoformat(),
                        "code": "REBOOT_SURVIVAL_VERIFIED",
                        **state,
                    }
                },
            )
        return _result("reboot-verify", "REBOOT_SURVIVAL_VERIFIED", True)
    except EvidenceFailure as exc:
        return _result("reboot-verify", exc.code)
    except Exception:  # noqa: BLE001 - launchctl, model, and config text is untrusted
        return _result("reboot-verify", "REBOOT_VERIFY_FAILED")
