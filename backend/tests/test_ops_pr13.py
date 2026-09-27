"""Linux simulations of PR13 evidence and fail-closed boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from meyar.ops import cleanup, diagnostics, edge, lifecycle_acceptance, private_evidence, reboot
from meyar.ops.offline_host import _operation_lock
from meyar.ops.private_evidence import EvidenceFailure, area, publish_bundle, verify_bundle
from meyar.ops.result import FindingStatus, OpsResultBuilder

RELEASE = "meyar-test+abcdef123456"
SOURCE = "a" * 40
MODEL = "b" * 64
RUNTIME = "c" * 64
APP = "com.bank.meyar"
OLLAMA = "com.bank.ollama"
IDENTITY = (
    RELEASE,
    SOURCE,
    "head",
    MODEL,
    RUNTIME,
    (1, 2, 3, 4),
    "d" * 64,
    "e" * 64,
    "0.12.0",
    "a" * 64,
    "b" * 64,
    OLLAMA,
)


def _result(code: str = "SERVICE_VISIBLE", *, ok: bool = True, secret: str = ""):
    builder = OpsResultBuilder(action="synthetic")
    builder.add(
        component="synthetic",
        status=FindingStatus.OK if ok else FindingStatus.FAIL,
        code=code,
        message=secret or code,
    )
    return builder.build()


def test_diagnostics_actual_bytes_exclude_all_sensitive_inputs(
    ops_host_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = [
        "P@ssword-Private-111",
        "postgresql+asyncpg://s:private@localhost/db",
        "API-Secret-222",
        "Candidate-Name-333",
        "candidate444@example.test",
        "Private-CV-555.pdf",
        "Private-CV-Text-666",
        "search-query-777",
        "application-log-888",
        "ollama-log-999",
        "launchctl-stderr-000",
    ]
    root = ops_host_root
    (root / "shared/config/.env").write_text("\n".join(secrets[:3]))
    (root / "shared/storage" / secrets[5]).write_text(secrets[6])
    (root / "shared/logs/app.log").write_text("\n".join(secrets[3:9]))
    (root / "shared/logs/ollama.log").write_text("\n".join(secrets[9:]))
    monkeypatch.setattr(diagnostics, "_identity", lambda *_: IDENTITY)
    monkeypatch.setattr(diagnostics, "run_service_status", lambda **_: _result(secret=secrets[10]))
    monkeypatch.setattr(
        diagnostics,
        "run_deployment_ready",
        lambda *_: _result("DB_REVISION_CURRENT", secret=secrets[7]),
    )
    result = diagnostics.collect_diagnostics(root, APP, OLLAMA, "diag-1")
    assert result.ok, result
    bundle = root / "shared/diagnostics/diag-1"
    assert bundle.stat().st_mode & 0o777 == 0o700
    files = list(bundle.iterdir())
    assert {file.name for file in files} == diagnostics.DIAGNOSTIC_FILES | {"checksums.sha256"}
    raw = b"".join(file.read_bytes() for file in files)
    assert all(secret.encode() not in raw for secret in secrets)
    assert all(file.stat().st_mode & 0o777 == 0o600 for file in files)
    assert diagnostics.verify_diagnostics(root, "diag-1").ok
    assert not diagnostics.collect_diagnostics(root, APP, OLLAMA, "diag-1").ok


def test_diagnostics_reject_changed_snapshot_and_tampered_bundle(
    ops_host_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = iter([IDENTITY, (*IDENTITY[:2], "changed", *IDENTITY[3:])])
    monkeypatch.setattr(diagnostics, "_identity", lambda *_: next(calls))
    monkeypatch.setattr(diagnostics, "run_service_status", lambda **_: _result())
    monkeypatch.setattr(diagnostics, "run_deployment_ready", lambda *_: _result())
    result = diagnostics.collect_diagnostics(ops_host_root, APP, OLLAMA, "changed")
    assert result.findings[0].code == "DIAGNOSTICS_STATE_CHANGED"
    assert not (ops_host_root / "shared/diagnostics/changed").exists()

    monkeypatch.setattr(diagnostics, "_identity", lambda *_: IDENTITY)
    assert diagnostics.collect_diagnostics(ops_host_root, APP, OLLAMA, "diag-2").ok
    target = ops_host_root / "shared/diagnostics/diag-2/deployment.json"
    target.chmod(0o600)
    target.write_text(target.read_text().replace(SOURCE, "f" * 40))
    assert not diagnostics.verify_diagnostics(ops_host_root, "diag-2").ok


def test_lifecycle_rejects_diagnostics_from_another_deployment(
    ops_host_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(diagnostics, "_identity", lambda *_: IDENTITY)
    monkeypatch.setattr(diagnostics, "run_service_status", lambda **_: _result())
    monkeypatch.setattr(diagnostics, "run_deployment_ready", lambda *_: _result())
    assert diagnostics.collect_diagnostics(ops_host_root, APP, OLLAMA, "diag-3").ok
    assert lifecycle_acceptance._diagnostics(ops_host_root, "diag-3", IDENTITY)
    changed = (*IDENTITY[:3], "f" * 64, *IDENTITY[4:])
    assert not lifecycle_acceptance._diagnostics(ops_host_root, "diag-3", changed)


@pytest.mark.parametrize(
    "origin",
    [
        "http://bank.test",
        "https://user:pass@bank.test",
        "https://bank.test/other",
        "https://bank.test?x=1",
    ],
)
def test_edge_rejects_non_origin_inputs(origin: str) -> None:
    assert edge.run_edge_verify(origin).findings[0].code == "EDGE_ORIGIN_INVALID"


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (302, b"", "EDGE_HEALTH_INVALID"),
        (500, b"", "EDGE_HEALTH_INVALID"),
        (200, b"x" * 1025, "EDGE_HEALTH_INVALID"),
        (200, b"not-json", "EDGE_HEALTH_INVALID"),
        (200, b'{"status":"bad"}', "EDGE_HEALTH_INVALID"),
        (200, b'{"status":"ok","status":"ok"}', "EDGE_HEALTH_INVALID"),
        (200, b'{"status":"ok"}', "EDGE_TLS_VERIFIED"),
    ],
)
def test_edge_fixed_direct_request_and_bounded_response(
    monkeypatch: pytest.MonkeyPatch, status: int, body: bytes, expected: str
) -> None:
    captured: dict[str, object] = {}

    class Connection:
        def __init__(self, host: str, port: int, *, timeout: float, context: object) -> None:
            captured.update(host=host, port=port, timeout=timeout, context=context)

        def request(self, method: str, path: str, *, headers: dict[str, str]) -> None:
            captured.update(method=method, path=path, headers=headers)

        def getresponse(self):
            return self

        def read(self, count: int) -> bytes:
            captured["limit"] = count
            return body

        def close(self) -> None:
            pass

    monkeypatch.setattr(edge.http.client, "HTTPSConnection", Connection)
    monkeypatch.setenv("HTTPS_PROXY", "http://private-proxy.invalid")
    monkeypatch.setenv("HTTP_PROXY", "http://private-proxy.invalid")
    Connection.status = status
    result = edge.run_edge_verify("https://bank.test:8443")
    assert result.findings[0].code == expected
    assert captured["host"] == "bank.test"
    assert captured["port"] == 8443
    assert captured["method"] == "GET"
    assert captured["path"] == "/api/v1/health"
    assert captured["headers"] == {"Accept": "application/json"}
    assert captured["limit"] == 1025


def test_edge_invalid_certificate_and_ca_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ssl

    class BadConnection:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def request(self, *_args: object, **_kwargs: object) -> None:
            raise ssl.SSLCertVerificationError("hostname mismatch")

        def close(self) -> None:
            pass

    monkeypatch.setattr(edge.http.client, "HTTPSConnection", BadConnection)
    assert edge.run_edge_verify("https://bank.test").findings[0].code == "EDGE_CERTIFICATE_INVALID"
    ca = tmp_path / "ca.pem"
    ca.write_text("synthetic")
    link = tmp_path / "link.pem"
    link.symlink_to(ca)
    assert edge.run_edge_verify("https://bank.test", link).findings[0].code == "EDGE_CA_INVALID"


def test_edge_real_local_tls_trust_and_hostname(tmp_path: Path) -> None:
    if shutil.which("openssl") is None:
        pytest.skip("openssl unavailable for synthetic TLS fixture")
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )
    cert.chmod(0o644)
    requests: list[tuple[str, str | None, str | None]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(
                (self.path, self.headers.get("Authorization"), self.headers.get("Cookie"))
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        assert edge.run_edge_verify(f"https://127.0.0.1:{port}", cert).ok
        assert (
            edge.run_edge_verify(f"https://127.0.0.1:{port}").findings[0].code
            == "EDGE_CERTIFICATE_INVALID"
        )
        assert (
            edge.run_edge_verify(f"https://localhost:{port}", cert).findings[0].code
            == "EDGE_CERTIFICATE_INVALID"
        )
        assert requests == [("/api/v1/health", None, None)]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _reboot_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reboot, "_identity", lambda *_: IDENTITY)
    monkeypatch.setattr(reboot, "run_service_status", lambda **_: _result())
    monkeypatch.setattr(reboot, "run_deployment_ready", lambda *_: _result())
    monkeypatch.setattr(reboot, "load_host_settings", lambda *_: object())
    monkeypatch.setattr(reboot, "verify_installed_models", lambda *_a, **_k: None)


def test_reboot_requires_intervening_boot_and_full_postboot_state(
    ops_host_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reboot_setup(monkeypatch)
    root = ops_host_root
    assert reboot.run_reboot_prepare(
        root, APP, OLLAMA, "rb-1", boot_reader=lambda: 10, platform_system="Darwin"
    ).ok
    same = reboot.run_reboot_verify(
        root, APP, OLLAMA, "rb-1", boot_reader=lambda: 10, platform_system="Darwin"
    )
    assert same.findings[0].code == "REBOOT_NOT_OBSERVED"
    monkeypatch.setattr(reboot, "run_service_status", lambda **_: _result(ok=False))
    absent = reboot.run_reboot_verify(
        root, APP, OLLAMA, "rb-1", boot_reader=lambda: 11, platform_system="Darwin"
    )
    assert absent.findings[0].code == "REBOOT_SERVICE_UNAVAILABLE"
    monkeypatch.setattr(reboot, "run_service_status", lambda **_: _result())
    monkeypatch.setattr(reboot, "run_deployment_ready", lambda *_: _result(ok=False))
    assert (
        reboot.run_reboot_verify(
            root, APP, OLLAMA, "rb-1", boot_reader=lambda: 11, platform_system="Darwin"
        )
        .findings[0]
        .code
        == "REBOOT_READINESS_FAILED"
    )
    monkeypatch.setattr(reboot, "run_deployment_ready", lambda *_: _result())
    assert reboot.run_reboot_verify(
        root, APP, OLLAMA, "rb-1", boot_reader=lambda: 11, platform_system="Darwin"
    ).ok
    receipt = reboot._receipt(root, "rb-1", "verify")
    assert receipt["code"] == "REBOOT_SURVIVAL_VERIFIED"
    path = root / "shared/reboots/rb-1-verify/manifest.json"
    path.write_text(path.read_text().replace(SOURCE, "f" * 40))
    with pytest.raises(EvidenceFailure):
        reboot._receipt(root, "rb-1", "verify")


def test_reboot_model_failure_and_receipt_fsync_failure_never_succeed(
    ops_host_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reboot_setup(monkeypatch)
    root = ops_host_root
    assert reboot.run_reboot_prepare(
        root, APP, OLLAMA, "rb-2", boot_reader=lambda: 10, platform_system="Darwin"
    ).ok

    def broken_model(*_args: object, **_kwargs: object) -> None:
        raise ValueError("synthetic model mismatch")

    monkeypatch.setattr(reboot, "verify_installed_models", broken_model)
    failed_model = reboot.run_reboot_verify(
        root, APP, OLLAMA, "rb-2", boot_reader=lambda: 11, platform_system="Darwin"
    )
    assert not failed_model.ok
    assert not (root / "shared/reboots/rb-2-verify").exists()

    monkeypatch.setattr(reboot, "verify_installed_models", lambda *_a, **_k: None)
    original_sync = private_evidence.sync_dir

    def fail_stage_sync(path: Path) -> None:
        if path.name.startswith(".evidence-"):
            raise OSError("synthetic fsync failure")
        original_sync(path)

    monkeypatch.setattr(private_evidence, "sync_dir", fail_stage_sync)
    failed_sync = reboot.run_reboot_verify(
        root, APP, OLLAMA, "rb-2", boot_reader=lambda: 11, platform_system="Darwin"
    )
    assert not failed_sync.ok
    assert not (root / "shared/reboots/rb-2-verify").exists()


def test_cleanup_only_removes_proven_activation_temporary_links(ops_host_root: Path) -> None:
    root = ops_host_root
    for relative in ("releases", "shared/backups"):
        (root / relative).chmod(0o750)
    generation = root / ("activations/g-" + "a" * 32)
    generation.mkdir()
    (generation / "current").symlink_to(f"../../releases/{RELEASE}")
    candidate = root / (".next-current-" + "b" * 32)
    candidate.symlink_to(f"activations/{generation.name}/current")
    foreign = root / ".next-current-foreign"
    foreign.symlink_to("/tmp/foreign")
    assert cleanup.run_cleanup(root).findings[-1].code == "CLEANUP_DRY_RUN"
    assert cleanup.run_cleanup(root, apply=True).findings[-1].code == "CLEANUP_UNSAFE"
    assert candidate.is_symlink()
    foreign.unlink()
    assert cleanup.run_cleanup(root, apply=True).ok
    assert not candidate.exists()
    active = root / (".next-current-" + "c" * 32)
    active.symlink_to(os.readlink(root / "current"))
    assert not cleanup.run_cleanup(root, apply=True).ok
    assert active.is_symlink()
    active.unlink()
    with _operation_lock(root):
        assert not cleanup.run_cleanup(root, apply=True).ok


def test_cleanup_rejects_wrong_type_foreign_owner_and_active_pointer(
    ops_host_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = ops_host_root
    for relative in ("releases", "shared/backups"):
        (root / relative).chmod(0o750)
    generation = root / ("activations/g-" + "a" * 32)
    generation.mkdir()
    (generation / "current").symlink_to(f"../../releases/{RELEASE}")
    candidate = root / (".next-current-" + "b" * 32)
    candidate.mkdir()
    assert not cleanup.run_cleanup(root, apply=True).ok
    assert candidate.is_dir()
    candidate.rmdir()
    candidate.symlink_to(f"activations/{generation.name}/current")
    original_lstat = Path.lstat

    def foreign_lstat(path: Path):
        info = original_lstat(path)
        if path == candidate:
            return SimpleNamespace(
                st_mode=info.st_mode,
                st_uid=0,
                st_nlink=info.st_nlink,
                st_dev=info.st_dev,
                st_ino=info.st_ino,
            )
        return info

    monkeypatch.setattr(Path, "lstat", foreign_lstat)
    assert not cleanup.run_cleanup(root, apply=True).ok
    assert candidate.is_symlink()
    monkeypatch.setattr(Path, "lstat", original_lstat)
    (root / "current").unlink()
    (root / "current").symlink_to(candidate.name)
    assert not cleanup.run_cleanup(root, apply=True).ok
    assert candidate.is_symlink()


def test_private_bundle_rejects_hash_change_extra_member_and_duplicate_keys(
    ops_host_root: Path,
) -> None:
    parent = area(ops_host_root, "diagnostics", create=True)
    bundle = publish_bundle(parent, "sample", {"manifest.json": {"format_version": 1}})
    assert verify_bundle(bundle, frozenset({"manifest.json"}))
    (bundle / "extra").write_text("x")
    with pytest.raises(EvidenceFailure):
        verify_bundle(bundle, frozenset({"manifest.json"}))
    (bundle / "extra").unlink()
    raw = b'{"format_version":1,"format_version":1}\n'
    (bundle / "manifest.json").write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    (bundle / "checksums.sha256").write_text(f"{digest}  manifest.json\n")
    with pytest.raises(EvidenceFailure):
        verify_bundle(bundle, frozenset({"manifest.json"}))


def test_lifecycle_missing_is_incomplete_and_tampered_evidence_fails(
    ops_host_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = ops_host_root
    monkeypatch.setattr(lifecycle_acceptance, "_identity", lambda *_: IDENTITY)
    monkeypatch.setattr(lifecycle_acceptance.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(lifecycle_acceptance.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(lifecycle_acceptance.Path, "samefile", lambda *_: True)
    monkeypatch.setattr(lifecycle_acceptance, "run_service_status", lambda **_: _result())
    monkeypatch.setattr(lifecycle_acceptance, "run_deployment_ready", lambda *_: _result())
    missing = lifecycle_acceptance.run_lifecycle_acceptance(root, APP, OLLAMA, "run-1")
    assert missing.findings[0].code == "ACCEPTANCE_INCOMPLETE"
    summary = verify_bundle(
        root / "shared/acceptance/run-1",
        frozenset({"manifest.json", "checks.json", "summary.json"}),
    )["summary.json"]
    assert summary["status"] == "INCOMPLETE"
    assert (
        next(item for item in summary["checks"] if item["component"] == "reboot")["status"]
        == "INCOMPLETE"
    )

    monkeypatch.setattr(lifecycle_acceptance, "_reboot", lambda *_: True)
    monkeypatch.setattr(lifecycle_acceptance, "_restore", lambda *_: True)
    monkeypatch.setattr(lifecycle_acceptance, "_update", lambda *_: True)
    monkeypatch.setattr(lifecycle_acceptance, "_edge", lambda *_: True)
    monkeypatch.setattr(lifecycle_acceptance, "_smoke", lambda *_: True)
    monkeypatch.setattr(lifecycle_acceptance, "_diagnostics", lambda *_: True)
    monkeypatch.setattr(lifecycle_acceptance, "run_backup_verify", lambda *_: _result())
    monkeypatch.setattr(
        lifecycle_acceptance,
        "_load_json",
        lambda *_: {"release_id": RELEASE, "source_sha": SOURCE, "alembic_head": "head"},
    )
    no_image = lifecycle_acceptance.run_lifecycle_acceptance(
        root,
        APP,
        OLLAMA,
        "run-missing-image",
        backup_id="backup",
        restore_id="restore",
        update_id="update",
        reboot_id="reboot",
        edge_id="edge",
        diagnostic_id="diag",
        smoke_id="smoke",
        postgres_image_ref="pgvector/pgvector@sha256:" + "d" * 64,
        pg_bin_dir=root,
    )
    assert no_image.findings[0].code == "ACCEPTANCE_INCOMPLETE"
    missing_checks = json.loads(
        (root / "shared/acceptance/run-missing-image/checks.json").read_text()
    )["checks"]
    assert (
        next(check for check in missing_checks if check["component"] == "synthetic_smoke")["status"]
        == "INCOMPLETE"
    )
    (root / "shared/smoke/smoke").mkdir(parents=True)
    passed = lifecycle_acceptance.run_lifecycle_acceptance(
        root,
        APP,
        OLLAMA,
        "run-2",
        backup_id="backup",
        restore_id="restore",
        update_id="update",
        reboot_id="reboot",
        edge_id="edge",
        diagnostic_id="diag",
        smoke_id="smoke",
        postgres_image_ref="pgvector/pgvector@sha256:" + "d" * 64,
        pg_bin_dir=root,
    )
    assert passed.findings[0].code == "ACCEPTANCE_PASS"
    monkeypatch.setattr(lifecycle_acceptance, "_update", lambda *_: False)
    failed = lifecycle_acceptance.run_lifecycle_acceptance(
        root,
        APP,
        OLLAMA,
        "run-3",
        backup_id="backup",
        restore_id="restore",
        update_id="update",
        reboot_id="reboot",
        edge_id="edge",
        diagnostic_id="diag",
        smoke_id="smoke",
        postgres_image_ref="pgvector/pgvector@sha256:" + "d" * 64,
        pg_bin_dir=root,
    )
    assert failed.findings[0].code == "ACCEPTANCE_FAIL"
    failed_summary = json.loads((root / "shared/acceptance/run-3/summary.json").read_text())
    assert failed_summary["status"] == "FAIL"
