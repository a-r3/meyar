"""Disposable synthetic HTTP smoke, executed by the installed release for evidence."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from meyar.ops.diagnostics import _identity
from meyar.ops.host_config import load_host_settings
from meyar.ops.offline_host import _operation_lock, verify_active_release
from meyar.ops.private_evidence import area, publish_bundle
from meyar.ops.smoke_contract import REQUIRED_CHECKS, postgres_image_digest
from meyar.smoke_fixture import SYNTHETIC_CV_PDF

CONTAINER = f"meyar-smoke-{os.getpid()}"
PORT = 55741
APP_PORT = 58080
PG_USER = "meyar"
PG_PASSWORD = secrets.token_urlsafe(24)
PG_DB = f"meyar_smoke_{secrets.token_hex(6)}"
DEV_IMAGE = "pgvector/pgvector:pg16"
DATABASE_URL = f"postgresql+asyncpg://{PG_USER}:{PG_PASSWORD}@127.0.0.1:{PORT}/{PG_DB}"
BASE_URL = f"http://127.0.0.1:{APP_PORT}"

results: list[dict] = []
checks: set[str] = set()
installed_python: Path | None = None
installed_backend: Path | None = None
installed_ini: Path | None = None
docker_command: list[str] = ["docker"]
docker_env: dict[str, str] = {}


class SmokeIncomplete(Exception):
    """A required local capability did not complete; never publish evidence."""


def _passed(code: str, ok: bool) -> None:
    if ok:
        checks.add(code)


def _app_command(module: str, *args: str) -> list[str]:
    if installed_python is None:
        executable = {"alembic": "alembic", "meyar.cli": "meyar", "uvicorn": "uvicorn"}[module]
        return ["uv", "run", executable, *args]
    return [str(installed_python), "-I", "-m", module, *args]


def _record(step: str, ok: bool, detail: str = "") -> None:
    results.append({"step": step, "ok": ok, "detail": detail})
    print(f"[{'OK' if ok else 'FAIL/SKIP'}] {step}{': ' + detail if detail else ''}")


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd, check=True, **kwargs)


def _created_tenant(stdout: str) -> tuple[str, str]:
    """Parse the current CLI's one-time key without printing it."""
    lines = stdout.splitlines()
    try:
        tenant_line = next(line for line in lines if line.startswith("Created tenant: "))
        tenant_id = tenant_line.split(": ", 1)[1].split(" (", 1)[0].strip()
        marker = lines.index("API key (shown once, store it now):")
        key = lines[marker + 1].strip()
        if not tenant_id or not key.startswith("meyar_test_"):
            raise ValueError
        return tenant_id, key
    except (StopIteration, IndexError, ValueError) as exc:
        raise RuntimeError("tenant provisioning output invalid") from exc


def _require_tools(evidence: bool) -> None:
    global docker_command, docker_env
    docker_found = shutil.which("docker")
    missing = (["docker"] if docker_found is None else []) + (
        ["uv"] if not evidence and shutil.which("uv") is None else []
    )
    if missing:
        print(f"ENVIRONMENTAL GATE: missing tool(s): {missing}", file=sys.stderr)
        sys.exit(2)
    docker_path = Path(docker_found or "")
    if (
        not docker_path.is_absolute()
        or not docker_path.is_file()
        or not os.access(docker_path, os.X_OK)
    ):
        raise RuntimeError("smoke Docker executable is not an absolute executable file")
    docker_path = docker_path.resolve(strict=True)
    docker_env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"DOCKER_HOST", "DOCKER_CONTEXT"}
    }
    endpoint = subprocess.run(
        [
            str(docker_path),
            "context",
            "inspect",
            "--format",
            '{{(index .Endpoints "docker").Host}}',
        ],
        env=docker_env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if not endpoint.startswith("unix://"):
        raise RuntimeError("smoke requires a local Unix Docker socket")
    socket_path = Path(endpoint.removeprefix("unix://"))
    if not socket_path.is_absolute() or not stat.S_ISSOCK(socket_path.stat().st_mode):
        raise RuntimeError("smoke Docker endpoint is not a local Unix socket")
    docker_command = [str(docker_path), "--host", endpoint]


def _require_local_image(image_ref: str) -> str:
    digest = postgres_image_digest(image_ref)
    if digest is None:
        raise SmokeIncomplete("approved pgvector image must be an exact sha256 reference")
    inspected = subprocess.run(
        [*docker_command, "image", "inspect", "--format", "{{json .RepoDigests}}", image_ref],
        env=docker_env,
        capture_output=True,
        text=True,
        check=False,
    )
    if inspected.returncode != 0:
        raise SmokeIncomplete("approved pgvector image is not preloaded locally")
    try:
        digests = json.loads(inspected.stdout)
    except json.JSONDecodeError as exc:
        raise SmokeIncomplete("local pgvector image identity is unavailable") from exc
    if not isinstance(digests, list) or image_ref not in digests:
        raise SmokeIncomplete("local pgvector image identity does not match approved digest")
    return digest


def _start_disposable_postgres(image_ref: str | None = None) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", PORT))
    _run(
        [
            *docker_command,
            "run",
            "--pull=never",
            "--rm",
            "-d",
            "--name",
            CONTAINER,
            "-e",
            f"POSTGRES_USER={PG_USER}",
            "-e",
            "POSTGRES_PASSWORD",
            "-e",
            f"POSTGRES_DB={PG_DB}",
            "-p",
            f"127.0.0.1:{PORT}:5432",
            image_ref or DEV_IMAGE,
        ],
        env={**docker_env, "POSTGRES_PASSWORD": PG_PASSWORD},
    )
    for _ in range(30):
        if (
            subprocess.run(
                [*docker_command, "exec", CONTAINER, "pg_isready", "-U", PG_USER, "-d", PG_DB],
                env=docker_env,
                capture_output=True,
            ).returncode
            == 0
        ):
            return
        time.sleep(1)
    raise RuntimeError("disposable Postgres never became ready")


def _start_app(env: dict, log_path: Path) -> subprocess.Popen:
    # Evidence mode hands Uvicorn a pre-bound loopback socket. No other
    # process can win a bind race and make its HTTP responses look like this
    # installed-release child process.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", APP_PORT))
        listener.listen(128)
        if installed_python is None:
            command = _app_command(
                "uvicorn", "meyar.main:app", "--host", "127.0.0.1", "--port", str(APP_PORT)
            )
            pass_fds: tuple[int, ...] = ()
            listener.close()
        else:
            command = _app_command("uvicorn", "meyar.main:app", "--fd", str(listener.fileno()))
            pass_fds = (listener.fileno(),)
        with log_path.open("w") as log:
            proc = subprocess.Popen(
                command,
                env=env,
                cwd=installed_backend,
                stdout=log,
                stderr=subprocess.STDOUT,
                pass_fds=pass_fds,
            )
    for _ in range(30):
        if proc.poll() is not None:
            raise RuntimeError("installed application exited before serving smoke")
        try:
            response = httpx.get(f"{BASE_URL}/api/v1/health", timeout=1.0, trust_env=False)
            if response.status_code == 200 and proc.poll() is None:
                return proc
        except httpx.HTTPError:
            pass
        time.sleep(1)
    proc.terminate()
    raise RuntimeError(f"app never became reachable — see {log_path}")


def _installed_identity(root: Path, app_label: str) -> tuple[Any, ...]:
    if not root.is_absolute() or ".." in root.parts:
        raise RuntimeError("installed smoke root invalid")
    release_id = verify_active_release(root)
    release = root / "releases" / release_id
    if (
        Path(__file__).resolve() != release / "backend/src/meyar/smoke_workload.py"
        or Path(sys.executable).resolve() != release / ".venv/bin/python"
    ):
        raise RuntimeError("smoke worker is not the active installed release")
    identity = _identity(root, app_label)
    if identity[0] != release_id:
        raise RuntimeError("active release changed")
    return identity


def _smoke_env(storage_root: Path, root: Path | None = None) -> dict[str, str]:
    env = {
        **{
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("MEYAR_", "PYTHON"))
        },
        "MEYAR_DATABASE_URL": DATABASE_URL,
        "MEYAR_STORAGE_ROOT": str(storage_root),
        "MEYAR_UI_COOKIE_SECURE": "false",
        "MEYAR_ENV": "test",
    }
    if root is not None:
        settings = load_host_settings(root)
        env.update(
            {
                "MEYAR_OLLAMA_BASE_URL": settings.ollama_base_url,
                "MEYAR_OLLAMA_MODEL": settings.ollama_model,
                "MEYAR_OLLAMA_EMBEDDING_MODEL": settings.ollama_embedding_model,
                "MEYAR_EMBEDDING_DIMENSIONS": str(settings.embedding_dimensions),
                "MEYAR_LLM_PROVIDER": "ollama",
                "MEYAR_EMBEDDING_PROVIDER": "ollama",
            }
        )
    return env


def _publish_evidence(
    root: Path, smoke_id: str, app_label: str, before: tuple[Any, ...], image_digest: str
) -> None:
    if any(not result["ok"] for result in results) or checks != REQUIRED_CHECKS:
        raise RuntimeError("required synthetic smoke checks did not all pass")
    with _operation_lock(root):
        after = _installed_identity(root, app_label)
        if after != before:
            raise RuntimeError("installed release identity changed during smoke")
        publish_bundle(
            area(root, "smoke", create=True),
            smoke_id,
            {
                "manifest.json": {
                    "format_version": 2,
                    "smoke_id": smoke_id,
                    "status": "PASS",
                    "release_id": before[0],
                    "source_sha": before[1],
                    "alembic_head": before[2],
                    "execution_mode": "installed-release",
                    "disposable_database": True,
                    "postgres_image_digest": image_digest,
                    "all_required_steps_passed": True,
                    "passed_checks": sorted(REQUIRED_CHECKS),
                }
            },
        )


def main() -> None:
    global installed_python, installed_backend, installed_ini
    results.clear()
    checks.clear()
    installed_python = None
    installed_backend = None
    installed_ini = None
    parser = argparse.ArgumentParser()
    parser.add_argument("--ops-install-root", type=Path)
    parser.add_argument("--smoke-id")
    parser.add_argument("--app-label")
    parser.add_argument("--postgres-image-ref")
    args = parser.parse_args()
    if any(
        value is not None
        for value in (args.ops_install_root, args.smoke_id, args.app_label, args.postgres_image_ref)
    ) and not all(
        value is not None
        for value in (args.ops_install_root, args.smoke_id, args.app_label, args.postgres_image_ref)
    ):
        parser.error("all smoke evidence options must be supplied together")
    evidence = args.ops_install_root is not None
    image_digest: str | None = None
    _require_tools(evidence)
    before: tuple[Any, ...] | None = None
    if evidence:
        assert args.app_label is not None
        before = _installed_identity(args.ops_install_root, args.app_label)
        release = args.ops_install_root / "releases" / before[0]
        installed_python = release / ".venv/bin/python"
        installed_backend = release / "backend"
        installed_ini = installed_backend / "alembic.ini"
        try:
            image_digest = _require_local_image(args.postgres_image_ref)
        except SmokeIncomplete as exc:
            print(f"ENVIRONMENTAL GATE: {exc}", file=sys.stderr)
            sys.exit(2)
    tmp = Path(tempfile.mkdtemp(prefix="meyar-smoke-"))
    storage_root = tmp / "storage"
    storage_root.mkdir()
    app_log = tmp / "app.log"
    app_proc: subprocess.Popen | None = None
    api: httpx.Client | None = None
    browser: httpx.Client | None = None

    env = _smoke_env(storage_root, args.ops_install_root)

    try:
        print(f"== working directory: {tmp} ==")
        _start_disposable_postgres(args.postgres_image_ref)

        if not evidence:
            print("== uv sync --locked ==")
            _run(["uv", "sync", "--locked"])
            _record("uv sync --locked", True)

        print("== alembic upgrade head (fresh disposable DB) ==")
        migration_args = (
            ("upgrade", "head")
            if installed_ini is None
            else ("-c", str(installed_ini), "upgrade", "head")
        )
        _run(_app_command("alembic", *migration_args), env=env, cwd=installed_backend)
        _record("alembic upgrade head", True)
        _passed("fresh_schema", True)

        print("== provisioning tenant/API key via CLI ==")
        create = _run(
            _app_command("meyar.cli", "create-tenant", "--name", "Smoke-Test-Tenant"),
            env=env,
            cwd=installed_backend,
            capture_output=True,
            text=True,
        )
        tenant_id, api_key = _created_tenant(create.stdout)
        _record("CLI tenant/API-key provisioning", True)

        # The interactive CLI reads twice from a detached stdin; the random
        # password is never an argv value, a log line, or receipt content.
        password = secrets.token_urlsafe(24)
        username = "synthetic-smoke-user"
        user = subprocess.run(
            _app_command("meyar.cli", "create-user", "--username", username),
            env=env,
            cwd=installed_backend,
            input=f"{password}\n{password}\n",
            capture_output=True,
            text=True,
            timeout=30,
            start_new_session=True,
        )
        if user.returncode != 0:
            raise RuntimeError("synthetic user provisioning failed")
        _run(
            _app_command(
                "meyar.cli",
                "add-membership",
                "--username",
                username,
                "--tenant-id",
                tenant_id,
                "--role",
                "HR_USER",
            ),
            env=env,
            cwd=installed_backend,
            capture_output=True,
            text=True,
        )

        print("== starting real uvicorn process ==")
        app_proc = _start_app(env, app_log)
        _record("uvicorn process start + reachable", True)

        api = httpx.Client(base_url=BASE_URL, trust_env=False, timeout=30.0)
        headers = {"Authorization": f"Bearer {api_key}"}
        health = api.get(f"{BASE_URL}/api/v1/health", timeout=30.0)
        _record("GET /api/v1/health", health.status_code == 200, str(health.status_code))

        docs = api.get(f"{BASE_URL}/docs", timeout=30.0)
        _record("GET /docs (offline Swagger)", docs.status_code == 200, str(docs.status_code))

        login_page = api.get(f"{BASE_URL}/ui/login", timeout=30.0)
        _record("GET /ui/login", login_page.status_code == 200, str(login_page.status_code))

        browser = httpx.Client(
            base_url=BASE_URL, follow_redirects=False, timeout=30.0, trust_env=False
        )
        login = browser.post("/ui/login", data={"username": username, "password": password})
        login_ok = login.status_code == 303 and bool(browser.cookies)
        _record("synthetic human login", login_ok, str(login.status_code))
        _passed("auth_login", login_ok and login_page.status_code == 200)

        cand = api.post(f"{BASE_URL}/api/v1/candidates", headers=headers, timeout=30.0)
        _record("create synthetic candidate", cand.status_code == 201, str(cand.status_code))
        candidate_id = cand.json()["id"]

        upload = api.post(
            f"{BASE_URL}/api/v1/candidates/{candidate_id}/documents",
            headers=headers,
            files={"file": ("valid_cv.pdf", SYNTHETIC_CV_PDF, "application/pdf")},
            timeout=30.0,
        )
        _record(
            "ingest synthetic CV (real MIME sniff + parse)",
            upload.status_code == 201 and upload.json()["parser_status"] == "PARSED",
            str(upload.status_code),
        )
        _passed(
            "synthetic_cv_ingestion",
            upload.status_code == 201 and upload.json()["parser_status"] == "PARSED",
        )
        document_id = upload.json()["id"]

        # Extraction/embedding are CLI-only (never exposed over REST — see
        # docs/DECISIONS.md D-018/D-019): run via `meyar extract-profile`/
        # `meyar embed-candidate` against the same disposable DB the app
        # process is using.
        extraction_completed = False
        try:
            extract_cli = subprocess.run(
                [
                    *_app_command("meyar.cli", "extract-profile"),
                    "--tenant-id",
                    tenant_id,
                    "--candidate-id",
                    candidate_id,
                    "--document-id",
                    document_id,
                ],
                env=env,
                cwd=installed_backend,
                capture_output=True,
                text=True,
                timeout=300,
            )
            print(extract_cli.stdout)
            extraction_completed = (
                extract_cli.returncode == 0 and "Status: COMPLETED" in extract_cli.stdout
            )
            _record(
                "profile extraction via real local Ollama LLM (CLI)",
                extraction_completed,
                extract_cli.stdout.strip().splitlines()[-1]
                if extract_cli.stdout
                else extract_cli.stderr[:300],
            )
            _passed("local_extraction", extraction_completed)
        except subprocess.TimeoutExpired:
            _record(
                "profile extraction via real local Ollama LLM (CLI)",
                False,
                "TIMED OUT after 300s on this host; no smoke PASS receipt",
            )

        embedding_completed = False
        try:
            embed_cli = subprocess.run(
                [
                    *_app_command("meyar.cli", "embed-candidate"),
                    "--tenant-id",
                    tenant_id,
                    "--candidate-id",
                    candidate_id,
                ],
                env=env,
                cwd=installed_backend,
                capture_output=True,
                text=True,
                timeout=60,
            )
            embedding_completed = embed_cli.returncode == 0 and any(
                line.startswith("Embedding version: ") for line in embed_cli.stdout.splitlines()
            )
            if embedding_completed:
                _record(
                    "embedding creation via real local Ollama (CLI)",
                    True,
                    embed_cli.stdout.strip(),
                )
                _passed("local_embedding", True)
            else:
                _record(
                    "embedding creation via real local Ollama (CLI)",
                    False,
                    "SKIPPED — configured embedding model is not pulled on this dev machine "
                    f"({embed_cli.stdout.strip() or embed_cli.stderr[:200]}); "
                    "not the target-Mac environment, not faked",
                )
        except subprocess.TimeoutExpired:
            _record(
                "embedding creation via real local Ollama (CLI)",
                False,
                "TIMED OUT after 60s on this host; no smoke PASS receipt",
            )

        if not extraction_completed or not embedding_completed:
            raise SmokeIncomplete

        job = api.post(
            f"{BASE_URL}/api/v1/jobs",
            headers=headers,
            json={
                "title": "Smoke Test Job",
                "criteria": [
                    {
                        "id": "python",
                        "kind": "SKILL",
                        "type": "MUST_HAVE",
                        "label": "Python",
                        "value": "Python",
                        "weight": 1,
                    }
                ],
            },
            timeout=30.0,
        )
        _record("create job + criteria", job.status_code == 201, str(job.status_code))
        _passed("vacancy_flow", job.status_code == 201)
        job_id = job.json()["id"]
        version_number = job.json()["current_criteria_version"]["version_number"]

        search = api.post(
            f"{BASE_URL}/api/v1/search",
            headers=headers,
            json={"mode": "STRUCTURED_ONLY", "required_filters": {"skills": ["Python"]}},
            timeout=30.0,
        )
        search_ok = search.status_code == 200 and any(
            item.get("candidate_id") == candidate_id for item in search.json().get("results", [])
        )
        _record("structured search", search_ok, str(search.status_code))
        _passed("structured_search", search_ok)

        detail = api.get(
            f"{BASE_URL}/api/v1/candidates/{candidate_id}/detail", headers=headers, timeout=30.0
        )
        _record("candidate detail (REST)", detail.status_code == 200, str(detail.status_code))
        library = browser.get("/ui/library")
        ui_detail = browser.get(f"/ui/candidates/{candidate_id}")
        library_ok = (
            detail.status_code == 200
            and library.status_code == 200
            and candidate_id in library.text
            and ui_detail.status_code == 200
        )
        _record("candidate library + detail (human session)", library_ok)
        _passed("candidate_library_detail", library_ok)

        original = api.get(
            f"{BASE_URL}/ui/candidates/{candidate_id}/documents/{document_id}/original",
            headers=headers,
            follow_redirects=False,
            timeout=30.0,
        )
        # UI route requires a browser session cookie, not a bare bearer token —
        # 303 (redirect to login) is the correct, safe response to an
        # unauthenticated request here, proving the auth boundary holds even
        # during a real HTTP smoke run.
        _record(
            "GET original-CV route without UI session -> safe redirect",
            original.status_code == 303,
            str(original.status_code),
        )
        authorized_original = browser.get(
            f"/ui/candidates/{candidate_id}/documents/{document_id}/original"
        )
        original_ok = (
            original.status_code == 303
            and authorized_original.status_code == 200
            and authorized_original.content == SYNTHETIC_CV_PDF
        )
        _record("original-CV authorization + exact synthetic bytes", original_ok)
        _passed("original_cv_authorization", original_ok)

        if extraction_completed:
            score = api.post(
                f"{BASE_URL}/api/v1/jobs/{job_id}/criteria/{version_number}/score",
                headers=headers,
                json={"candidate_id": candidate_id, "evaluation_as_of_date": "2026-01-01"},
                timeout=30.0,
            )
            score_ok = score.status_code == 200 and score.json().get("candidate_id") == candidate_id
            _record("deterministic score", score_ok, str(score.status_code))
            _passed("deterministic_scoring", score_ok)
            rank = api.post(
                f"{BASE_URL}/api/v1/jobs/{job_id}/criteria/{version_number}/rank",
                headers=headers,
                json={"evaluation_as_of_date": "2026-01-01"},
                timeout=30.0,
            )
            rank_ok = rank.status_code == 200 and any(
                item.get("candidate_id") == candidate_id for item in rank.json().get("results", [])
            )
            _record("deterministic ranking", rank_ok, str(rank.status_code))
            _passed("deterministic_ranking", rank_ok)
        else:
            _record("deterministic score", False, "SKIPPED — no completed profile version")
            _record("deterministic ranking", False, "SKIPPED — no completed profile version")

        print("== restarting the app process (same DB/storage) ==")
        app_proc.terminate()
        app_proc.wait(timeout=10)
        app_proc = _start_app(env, app_log)
        _record("app restart", True)

        reread = api.get(
            f"{BASE_URL}/api/v1/candidates/{candidate_id}/detail", headers=headers, timeout=30.0
        )
        _record(
            "re-read candidate detail after restart (persistence)",
            reread.status_code == 200 and reread.json()["candidate_id"] == candidate_id,
            str(reread.status_code),
        )
        _passed(
            "restart_persistence",
            reread.status_code == 200 and reread.json()["candidate_id"] == candidate_id,
        )
    except SmokeIncomplete:
        pass
    finally:
        if browser is not None:
            browser.close()
        if api is not None:
            api.close()
        if app_proc is not None:
            if app_proc.poll() is None:
                app_proc.terminate()
            try:
                app_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                app_proc.kill()
        subprocess.run([*docker_command, "stop", CONTAINER], env=docker_env, capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n== SUMMARY ==")
    print(json.dumps(results, indent=2))
    failed = [r for r in results if not r["ok"]]
    if evidence and checks != REQUIRED_CHECKS:
        failed.append({"step": "required smoke capability set", "ok": False})
    if failed:
        print(f"\n{len(failed)} step(s) failed or were environment-limited (see above).")
        sys.exit(1)
    print("\nFRESH-DEPLOYMENT SMOKE: ALL STEPS PASSED.")
    if evidence:
        assert args.ops_install_root is not None and args.app_label is not None
        assert args.smoke_id is not None and before is not None and image_digest is not None
        _publish_evidence(
            args.ops_install_root, args.smoke_id, args.app_label, before, image_digest
        )


if __name__ == "__main__":
    main()
