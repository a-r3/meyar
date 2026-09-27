"""Allowlisted, local-only installed-host diagnostics without raw logs or data."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
import sys
from pathlib import Path
from typing import Any

from meyar.ops.ai_provision import _runtime_state, _service_state, verify_installed_models
from meyar.ops.backup import SAFE_ID
from meyar.ops.deployment_ready import _installed_spec, run_deployment_ready
from meyar.ops.host_config import load_host_settings
from meyar.ops.offline_host import (
    InstallFailure,
    _load_json,
    _operation_lock,
    verify_active_release,
)
from meyar.ops.private_evidence import EvidenceFailure, area, publish_bundle, verify_bundle
from meyar.ops.result import FindingStatus, OpsResult, build_single_finding_result
from meyar.ops.schema_init import _active_config
from meyar.ops.service_plist import validate_label
from meyar.ops.service_status import run_service_status

DIAGNOSTIC_FILES = frozenset(
    {
        "manifest.json",
        "environment.json",
        "deployment.json",
        "services.json",
        "database.json",
        "ai.json",
        "operations.json",
        "filesystem.json",
    }
)
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")


def _result(action: str, code: str, ok: bool = False) -> OpsResult:
    return build_single_finding_result(
        action=action,
        component="diagnostics",
        status=FindingStatus.OK if ok else FindingStatus.FAIL,
        code=code,
        message="diagnostics verified" if ok else "diagnostics refused or failed",
    )


def _identity(
    root: Path, app_label: str
) -> tuple[str, str, str, str, str, tuple[int, ...], str, str, str, str, str, str]:
    release_id = verify_active_release(root)
    _, head = _active_config(root, implementation_file=Path(__file__))
    manifest = _load_json(root / "releases" / release_id / "release_manifest.json")
    source_sha = manifest.get("source_sha")
    if not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("source identity invalid")
    settings = load_host_settings(root)
    model_digest, receipt = verify_installed_models(root, settings, probe=False)
    # No protected config bytes or values enter the artifact. The file identity
    # catches replacement between unlocked probes and publication.
    config = root / "shared/config/.env"
    info = config.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("config identity invalid")
    config_identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    runtime = receipt.get("runtime_sha256")
    if not isinstance(runtime, str) or not re.fullmatch(r"[0-9a-f]{64}", runtime):
        raise ValueError("runtime identity invalid")
    runtime_version = _runtime_state(root)["version"]
    models = receipt["models"]
    llm_digest = models["LLM"]["ollama_digest"]
    embedding_digest = models["EMBEDDING"]["ollama_digest"]
    _, plist = _installed_spec(root, app_label, Path("/Library/LaunchDaemons"), 0, os.geteuid())
    receipt_hash = hashlib.sha256(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return (
        release_id,
        source_sha,
        head,
        model_digest,
        runtime,
        config_identity,
        hashlib.sha256(plist).hexdigest(),
        receipt_hash,
        runtime_version,
        llm_digest,
        embedding_digest,
        _service_state(root)["label"],
    )


def _safe_codes(result: OpsResult) -> list[str]:
    return [finding.code for finding in result.findings if _CODE.fullmatch(finding.code)]


def _files(
    root: Path,
    diagnostic_id: str,
    app_label: str,
    ollama_label: str,
    identity: tuple[str, str, str, str, str, tuple[int, ...], str, str, str, str, str, str],
) -> dict[str, dict[str, Any]]:
    release_id, source_sha, head, model_digest, runtime = identity[:5]
    app = run_service_status(label=app_label)
    ollama = run_service_status(label=ollama_label)
    ready = run_deployment_ready(root, app_label)
    usage = os.statvfs(root)
    codes = _safe_codes(ready)
    return {
        "manifest.json": {
            "format_version": 1,
            "diagnostic_id": diagnostic_id,
            "kind": "allowlisted-local-metadata",
        },
        "environment.json": {
            "system": platform.system(),
            "architecture": platform.machine(),
            "macos_version": platform.mac_ver()[0],
            "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        },
        "deployment.json": {
            "release_id": release_id,
            "source_sha": source_sha,
            "alembic_head": head,
            "ready": ready.ok,
            "finding_codes": codes,
        },
        "services.json": {
            "app_label": app_label,
            "app_codes": _safe_codes(app),
            "ollama_label": ollama_label,
            "ollama_codes": _safe_codes(ollama),
        },
        "database.json": {
            "revision_code": next(
                (code for code in codes if code.startswith("DB_REVISION_")), "PREREQUISITE_FAILED"
            )
        },
        "ai.json": {
            "model_manifest_sha256": model_digest,
            "runtime_sha256": runtime,
            "runtime_version": identity[8],
            "llm_digest": identity[9],
            "embedding_digest": identity[10],
            "model_code": "MODEL_MANIFEST_VERIFIED"
            if "MODEL_MANIFEST_VERIFIED" in codes
            else "MODEL_MANIFEST_UNVERIFIED",
        },
        "operations.json": {
            name: (root / "shared" / name).is_dir() for name in ("backups", "restores", "updates")
        },
        "filesystem.json": {
            "block_size": usage.f_frsize,
            "total_blocks": usage.f_blocks,
            "available_blocks": usage.f_bavail,
        },
    }


def _validate_shapes(files: dict[str, dict[str, Any]], diagnostic_id: str) -> None:
    if set(files) != DIAGNOSTIC_FILES:
        raise EvidenceFailure("DIAGNOSTICS_INVALID")
    manifest = files["manifest.json"]
    if manifest != {
        "format_version": 1,
        "diagnostic_id": diagnostic_id,
        "kind": "allowlisted-local-metadata",
    }:
        raise EvidenceFailure("DIAGNOSTICS_INVALID")
    deployment = files["deployment.json"]
    if (
        set(deployment) != {"release_id", "source_sha", "alembic_head", "ready", "finding_codes"}
        or not isinstance(deployment["release_id"], str)
        or not re.fullmatch(r"meyar-[A-Za-z0-9.+_-]{1,80}\+[0-9a-f]{12}", deployment["release_id"])
        or not isinstance(deployment["source_sha"], str)
        or not re.fullmatch(r"[0-9a-f]{40}", deployment["source_sha"])
        or not isinstance(deployment["alembic_head"], str)
        or not re.fullmatch(r"[A-Za-z0-9_]{1,128}", deployment["alembic_head"])
        or type(deployment["ready"]) is not bool
        or not isinstance(deployment["finding_codes"], list)
        or any(
            not isinstance(code, str) or not _CODE.fullmatch(code)
            for code in deployment["finding_codes"]
        )
    ):
        raise EvidenceFailure("DIAGNOSTICS_INVALID")
    expected = {
        "environment.json": {"system", "architecture", "macos_version", "python"},
        "services.json": {"app_label", "app_codes", "ollama_label", "ollama_codes"},
        "database.json": {"revision_code"},
        "ai.json": {
            "model_manifest_sha256", "runtime_sha256", "runtime_version",
            "llm_digest", "embedding_digest", "model_code",
        },
        "operations.json": {"backups", "restores", "updates"},
        "filesystem.json": {"block_size", "total_blocks", "available_blocks"},
    }
    if any(set(files[name]) != keys for name, keys in expected.items()):
        raise EvidenceFailure("DIAGNOSTICS_INVALID")
    for name in ("environment.json",):
        if any(
            not isinstance(value, str)
            or len(value) > 80
            or re.fullmatch(r"[A-Za-z0-9._ +()-]*", value) is None
            for value in files[name].values()
        ):
            raise EvidenceFailure("DIAGNOSTICS_INVALID")
    services = files["services.json"]
    for key in ("app_label", "ollama_label"):
        try:
            validate_label(services[key])
        except (ValueError, TypeError) as exc:
            raise EvidenceFailure("DIAGNOSTICS_INVALID") from exc
    for key in ("app_codes", "ollama_codes"):
        if not isinstance(services[key], list) or any(
            not isinstance(code, str) or _CODE.fullmatch(code) is None for code in services[key]
        ):
            raise EvidenceFailure("DIAGNOSTICS_INVALID")
    if not isinstance(files["database.json"]["revision_code"], str) or not _CODE.fullmatch(
        files["database.json"]["revision_code"]
    ):
        raise EvidenceFailure("DIAGNOSTICS_INVALID")
    ai = files["ai.json"]
    if any(
        not isinstance(ai[key], str) or not re.fullmatch(r"[0-9a-f]{64}", ai[key])
        for key in ("model_manifest_sha256", "runtime_sha256", "llm_digest", "embedding_digest")
    ) or ai["model_code"] not in {"MODEL_MANIFEST_VERIFIED", "MODEL_MANIFEST_UNVERIFIED"}:
        raise EvidenceFailure("DIAGNOSTICS_INVALID")
    if not isinstance(ai["runtime_version"], str) or re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][a-zA-Z0-9._-]+)?", ai["runtime_version"]
    ) is None:
        raise EvidenceFailure("DIAGNOSTICS_INVALID")
    if any(type(value) is not bool for value in files["operations.json"].values()) or any(
        type(value) is not int or value < 0 for value in files["filesystem.json"].values()
    ):
        raise EvidenceFailure("DIAGNOSTICS_INVALID")


def verify_diagnostics(root: Path, diagnostic_id: str) -> OpsResult:
    try:
        if SAFE_ID.fullmatch(diagnostic_id) is None:
            raise EvidenceFailure("DIAGNOSTICS_ID_INVALID")
        path = area(root, "diagnostics") / diagnostic_id
        files = verify_bundle(path, DIAGNOSTIC_FILES)
        _validate_shapes(files, diagnostic_id)
        return _result("diagnostics-verify", "DIAGNOSTICS_VERIFIED", True)
    except (EvidenceFailure, OSError):
        return _result("diagnostics-verify", "DIAGNOSTICS_INVALID")


def collect_diagnostics(
    root: Path, app_label: str, ollama_label: str, diagnostic_id: str
) -> OpsResult:
    try:
        if SAFE_ID.fullmatch(diagnostic_id) is None:
            raise EvidenceFailure("DIAGNOSTICS_ID_INVALID")
        validate_label(app_label)
        validate_label(ollama_label)
        with _operation_lock(root):
            first = _identity(root, app_label)
            if first[11] != ollama_label:
                raise EvidenceFailure("DIAGNOSTICS_LABEL_MISMATCH")
        files = _files(root, diagnostic_id, app_label, ollama_label, first)
        _validate_shapes(files, diagnostic_id)
        with _operation_lock(root):
            if _identity(root, app_label) != first:
                raise EvidenceFailure("DIAGNOSTICS_STATE_CHANGED")
            parent = area(root, "diagnostics", create=True)
            publish_bundle(parent, diagnostic_id, files)
        verified = verify_diagnostics(root, diagnostic_id)
        if not verified.ok:
            raise EvidenceFailure("DIAGNOSTICS_INVALID")
        return _result("collect-diagnostics", "DIAGNOSTICS_COLLECTED", True)
    except EvidenceFailure as exc:
        return _result("collect-diagnostics", exc.code)
    except InstallFailure as exc:
        return _result(
            "collect-diagnostics",
            "OPERATION_BUSY" if exc.code == "OPERATION_BUSY" else "DIAGNOSTICS_UNAVAILABLE",
        )
    except Exception:  # noqa: BLE001 - do not expose protected settings or probe text
        return _result("collect-diagnostics", "DIAGNOSTICS_UNAVAILABLE")
