"""Fixed, unauthenticated HTTPS health probe for bank-managed ingress."""

from __future__ import annotations

import http.client
import json
import os
import ssl
import stat
from pathlib import Path
from urllib.parse import urlsplit

from meyar.ops.diagnostics import _identity
from meyar.ops.offline_host import InstallFailure, _operation_lock
from meyar.ops.private_evidence import EvidenceFailure, area, publish_bundle, unique_object
from meyar.ops.result import FindingStatus, OpsResult, build_single_finding_result


def _result(code: str, ok: bool = False) -> OpsResult:
    return build_single_finding_result(
        action="edge-verify",
        component="edge",
        status=FindingStatus.OK if ok else FindingStatus.FAIL,
        code=code,
        message="HTTPS edge verified" if ok else "HTTPS edge verification failed",
    )


def _origin(value: str) -> tuple[str, int]:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in value)
    ):
        raise ValueError("invalid origin")
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise ValueError("invalid port") from exc
    if not 1 <= port <= 65535:
        raise ValueError("invalid port")
    return parsed.hostname, port


def _ca_file(path: Path) -> Path:
    try:
        info = path.lstat()
        if (
            not path.is_absolute()
            or ".." in path.parts
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid not in (0, os.geteuid())
            or info.st_mode & 0o022
            or not 0 < info.st_size <= 1024 * 1024
        ):
            raise ValueError("unsafe CA file")
        return path
    except OSError as exc:
        raise ValueError("unsafe CA file") from exc


def run_edge_verify(
    origin: str,
    ca_file: Path | None = None,
    *,
    install_root: Path | None = None,
    edge_id: str | None = None,
    app_label: str | None = None,
) -> OpsResult:
    if sum(value is not None for value in (install_root, edge_id, app_label)) not in (0, 3):
        return _result("EDGE_EVIDENCE_ARGUMENTS_INVALID")
    try:
        host, port = _origin(origin)
    except ValueError:
        return _result("EDGE_ORIGIN_INVALID")
    try:
        context = ssl.create_default_context(cafile=str(_ca_file(ca_file)) if ca_file else None)
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
    except (ValueError, OSError, ssl.SSLError):
        return _result("EDGE_CA_INVALID")
    first = None
    if install_root is not None and app_label is not None:
        try:
            with _operation_lock(install_root):
                first = _identity(install_root, app_label)
        except Exception:  # noqa: BLE001 - protected state is never public
            return _result("EDGE_EVIDENCE_UNAVAILABLE")
    connection = http.client.HTTPSConnection(host, port, timeout=3.0, context=context)
    try:
        connection.request("GET", "/api/v1/health", headers={"Accept": "application/json"})
        response = connection.getresponse()
        raw = response.read(1025)
        if response.status != 200 or len(raw) > 1024:
            return _result("EDGE_HEALTH_INVALID")
        try:
            payload = json.loads(raw, object_pairs_hook=unique_object)
        except (ValueError, UnicodeError):
            return _result("EDGE_HEALTH_INVALID")
        if payload != {"status": "ok"}:
            return _result("EDGE_HEALTH_INVALID")
    except ssl.SSLCertVerificationError:
        return _result("EDGE_CERTIFICATE_INVALID")
    except (ssl.SSLError, TimeoutError, OSError, http.client.HTTPException):
        return _result("EDGE_UNREACHABLE")
    finally:
        connection.close()
    if (
        install_root is not None
        and edge_id is not None
        and app_label is not None
        and first is not None
    ):
        try:
            with _operation_lock(install_root):
                if _identity(install_root, app_label) != first:
                    return _result("EDGE_STATE_CHANGED")
                parent = area(install_root, "edges", create=True)
                publish_bundle(
                    parent,
                    edge_id,
                    {
                        "manifest.json": {
                            "format_version": 1,
                            "edge_id": edge_id,
                            "origin": origin,
                            "code": "EDGE_TLS_VERIFIED",
                            "release_id": first[0],
                            "source_sha": first[1],
                            "model_manifest_sha256": first[3],
                        }
                    },
                )
        except (InstallFailure, EvidenceFailure, OSError):
            return _result("EDGE_EVIDENCE_UNAVAILABLE")
    return _result("EDGE_TLS_VERIFIED", ok=True)
