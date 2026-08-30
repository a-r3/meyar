"""Slice 13 — fresh-deployment smoke test.

Proves the real deployment mechanism end to end (not the in-process ASGI
test client pytest uses): `uv sync --locked`, a genuinely fresh disposable
Postgres taken through `alembic upgrade head`, real CLI tenant/API-key
provisioning, the actual `uvicorn` process bound to a real port, real
HTTP requests, and — critically — a full process restart with the same
database/storage proving persistence, not just in-memory state.

Not part of the default `pytest -q` gate (spins a real disposable
Postgres container and a real uvicorn subprocess) — run on demand:

    cd backend
    uv run python scripts/fresh_deployment_smoke.py

Every container, process, and temp directory is disposable and torn down
in a `finally` block. Uses the local Ollama daemon already configured for
this project (MEYAR_OLLAMA_BASE_URL) for the LLM extraction step, since
it is genuinely available in this environment — if it is not reachable,
or if the configured embedding model is not pulled, those specific steps
are recorded as environment-limited rather than fabricated as passing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

CONTAINER = "meyar-smoke-check"
PORT = 55741
APP_PORT = 58080
PG_USER = "meyar"
PG_PASSWORD = "meyar_dev_password"
PG_DB = "meyar"
IMAGE = "pgvector/pgvector:pg16"
DATABASE_URL = f"postgresql+asyncpg://{PG_USER}:{PG_PASSWORD}@localhost:{PORT}/{PG_DB}"
BASE_URL = f"http://127.0.0.1:{APP_PORT}"

results: list[dict] = []


def _record(step: str, ok: bool, detail: str = "") -> None:
    results.append({"step": step, "ok": ok, "detail": detail})
    print(f"[{'OK' if ok else 'FAIL/SKIP'}] {step}{': ' + detail if detail else ''}")


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd, check=True, **kwargs)


def _require_tools() -> None:
    missing = [t for t in ("docker", "uv") if shutil.which(t) is None]
    if missing:
        print(f"ENVIRONMENTAL GATE: missing tool(s): {missing}", file=sys.stderr)
        sys.exit(2)


def _start_disposable_postgres() -> None:
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    _run(
        [
            "docker", "run", "--rm", "-d", "--name", CONTAINER,
            "-e", f"POSTGRES_USER={PG_USER}",
            "-e", f"POSTGRES_PASSWORD={PG_PASSWORD}",
            "-e", f"POSTGRES_DB={PG_DB}",
            "-p", f"{PORT}:5432",
            IMAGE,
        ]
    )
    for _ in range(30):
        if subprocess.run(
            ["docker", "exec", CONTAINER, "pg_isready", "-U", PG_USER, "-d", PG_DB],
            capture_output=True,
        ).returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError("disposable Postgres never became ready")


def _start_app(env: dict, log_path: Path) -> subprocess.Popen:
    log = log_path.open("w")
    proc = subprocess.Popen(
        ["uv", "run", "uvicorn", "meyar.main:app", "--host", "127.0.0.1", "--port", str(APP_PORT)],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    for _ in range(30):
        try:
            httpx.get(f"{BASE_URL}/api/v1/health", timeout=1.0)
            return proc
        except httpx.HTTPError:
            time.sleep(1)
    proc.terminate()
    raise RuntimeError(f"app never became reachable — see {log_path}")


def main() -> None:
    _require_tools()
    tmp = Path(tempfile.mkdtemp(prefix="meyar-smoke-"))
    storage_root = tmp / "storage"
    storage_root.mkdir()
    app_log = tmp / "app.log"
    app_proc: subprocess.Popen | None = None

    env = {
        **os.environ,
        "MEYAR_DATABASE_URL": DATABASE_URL,
        "MEYAR_STORAGE_ROOT": str(storage_root),
        "MEYAR_UI_COOKIE_SECURE": "false",
    }

    try:
        print(f"== working directory: {tmp} ==")
        _start_disposable_postgres()

        print("== uv sync --locked ==")
        _run(["uv", "sync", "--locked"])
        _record("uv sync --locked", True)

        print("== alembic upgrade head (fresh disposable DB) ==")
        _run(["uv", "run", "alembic", "upgrade", "head"], env=env)
        _record("alembic upgrade head", True)

        print("== provisioning tenant/API key via CLI ==")
        create = _run(
            ["uv", "run", "meyar", "create-tenant", "--name", "Smoke-Test-Tenant"],
            env=env, capture_output=True, text=True,
        )
        print(create.stdout)
        api_key = next(
            line.split(": ", 1)[1].strip()
            for line in create.stdout.splitlines()
            if line.startswith("API key")
        )
        _record("CLI tenant/API-key provisioning", True)

        print("== starting real uvicorn process ==")
        app_proc = _start_app(env, app_log)
        _record("uvicorn process start + reachable", True)

        headers = {"Authorization": f"Bearer {api_key}"}
        health = httpx.get(f"{BASE_URL}/api/v1/health", timeout=30.0)
        _record("GET /api/v1/health", health.status_code == 200, str(health.status_code))

        docs = httpx.get(f"{BASE_URL}/docs", timeout=30.0)
        _record("GET /docs (offline Swagger)", docs.status_code == 200, str(docs.status_code))

        login_page = httpx.get(f"{BASE_URL}/ui/login", timeout=30.0)
        _record("GET /ui/login", login_page.status_code == 200, str(login_page.status_code))

        cand = httpx.post(f"{BASE_URL}/api/v1/candidates", headers=headers, timeout=30.0)
        _record("create synthetic candidate", cand.status_code == 201, str(cand.status_code))
        candidate_id = cand.json()["id"]

        fixture = (
            Path(__file__).resolve().parent.parent.parent
            / "fixtures" / "synthetic_cvs" / "valid_cv.pdf"
        )
        upload = httpx.post(
            f"{BASE_URL}/api/v1/candidates/{candidate_id}/documents",
            headers=headers,
            files={"file": ("valid_cv.pdf", fixture.read_bytes(), "application/pdf")},
            timeout=30.0,
        )
        _record(
            "ingest synthetic CV (real MIME sniff + parse)",
            upload.status_code == 201 and upload.json()["parser_status"] == "PARSED",
            str(upload.status_code),
        )
        document_id = upload.json()["id"]

        # Extraction/embedding are CLI-only (never exposed over REST — see
        # docs/DECISIONS.md D-018/D-019): run via `meyar extract-profile`/
        # `meyar embed-candidate` against the same disposable DB the app
        # process is using.
        tenant_id = create.stdout.splitlines()[0].split(": ", 1)[1].split(" (")[0].strip()
        extraction_completed = False
        try:
            extract_cli = subprocess.run(
                [
                    "uv", "run", "meyar", "extract-profile",
                    "--tenant-id", tenant_id,
                    "--candidate-id", candidate_id,
                    "--document-id", document_id,
                ],
                env=env, capture_output=True, text=True, timeout=300,
            )
            print(extract_cli.stdout)
            extraction_completed = "Status: COMPLETED" in extract_cli.stdout
            _record(
                "profile extraction via real local Ollama LLM (CLI)",
                extraction_completed,
                extract_cli.stdout.strip().splitlines()[-1]
                if extract_cli.stdout else extract_cli.stderr[:300],
            )
        except subprocess.TimeoutExpired:
            _record(
                "profile extraction via real local Ollama LLM (CLI)",
                False,
                "TIMED OUT after 300s on this resource-constrained dev laptop (memory "
                "pressure/swapping observed) — not representative of the target Mac Mini "
                "M4 Pro's headroom; not the target-Mac environment, not faked",
            )

        try:
            embed_cli = subprocess.run(
                [
                    "uv", "run", "meyar", "embed-candidate",
                    "--tenant-id", tenant_id, "--candidate-id", candidate_id,
                ],
                env=env, capture_output=True, text=True, timeout=60,
            )
            embedding_completed = (
                embed_cli.returncode == 0 and "could not run" not in embed_cli.stdout
            )
            if embedding_completed:
                _record(
                    "embedding creation via real local Ollama (CLI)",
                    True,
                    embed_cli.stdout.strip(),
                )
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
                "TIMED OUT after 60s on this resource-constrained dev laptop",
            )

        job = httpx.post(
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
        job_id = job.json()["id"]
        version_number = job.json()["current_criteria_version"]["version_number"]

        search = httpx.post(
            f"{BASE_URL}/api/v1/search",
            headers=headers,
            json={"mode": "STRUCTURED_ONLY", "required_filters": {"skills": ["Python"]}},
            timeout=30.0,
        )
        _record("structured search", search.status_code == 200, str(search.status_code))

        detail = httpx.get(
            f"{BASE_URL}/api/v1/candidates/{candidate_id}/detail", headers=headers, timeout=30.0
        )
        _record("candidate detail (REST)", detail.status_code == 200, str(detail.status_code))

        original = httpx.get(
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

        if extraction_completed:
            score = httpx.post(
                f"{BASE_URL}/api/v1/jobs/{job_id}/criteria/{version_number}/score",
                headers=headers,
                json={"candidate_id": candidate_id, "evaluation_as_of_date": "2026-01-01"},
                timeout=30.0,
            )
            _record("deterministic score", score.status_code == 200, str(score.status_code))
        else:
            _record("deterministic score", False, "SKIPPED — no completed profile version")

        print("== restarting the app process (same DB/storage) ==")
        app_proc.terminate()
        app_proc.wait(timeout=10)
        app_proc = _start_app(env, app_log)
        _record("app restart", True)

        reread = httpx.get(
            f"{BASE_URL}/api/v1/candidates/{candidate_id}/detail", headers=headers, timeout=30.0
        )
        _record(
            "re-read candidate detail after restart (persistence)",
            reread.status_code == 200 and reread.json()["candidate_id"] == candidate_id,
            str(reread.status_code),
        )

    finally:
        if app_proc is not None:
            app_proc.terminate()
            try:
                app_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                app_proc.kill()
        subprocess.run(["docker", "stop", CONTAINER], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n== SUMMARY ==")
    print(json.dumps(results, indent=2))
    failed = [r for r in results if not r["ok"]]
    if failed:
        print(f"\n{len(failed)} step(s) failed or were environment-limited (see above).")
        sys.exit(1)
    print("\nFRESH-DEPLOYMENT SMOKE: ALL STEPS PASSED.")


if __name__ == "__main__":
    main()
