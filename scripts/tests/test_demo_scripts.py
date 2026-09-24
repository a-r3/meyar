#!/usr/bin/env python3
"""Lightweight test harness for scripts/demo-up.sh and scripts/demo-down.sh.

Stubs `docker`, `uv`, and (selectively) `ollama` via a fake PATH entry so no
real Docker daemon, PostgreSQL, or Python backend is touched. Real `git`,
`curl`, `bash`, and coreutils are used unmodified. Not part of the backend
pytest suite (backend/pyproject.toml scopes pytest to backend/tests) — this
exercises only the two shell wrappers, which have no product/domain logic
of their own. Run directly:

    python3 scripts/tests/test_demo_scripts.py

Chore-level harness (one-command local demo convenience). See
docs/LOCAL_DEMO.md and CLAUDE.md non-negotiables (no real CVs/PII/secrets
persisted to disk by this harness either).
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import textwrap
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_UP = REPO_ROOT / "scripts" / "demo-up.sh"
DEMO_DOWN = REPO_ROOT / "scripts" / "demo-down.sh"
HOST = "127.0.0.1"
PORT = 8000


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


FAKE_DOCKER = textwrap.dedent(
    r"""
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ -n "${DEMO_TEST_CALL_LOG:-}" ]]; then
      printf 'docker %s\n' "$*" >> "$DEMO_TEST_CALL_LOG"
    fi
    if [[ "$1" == "info" ]]; then
      exit "${FAKE_DOCKER_INFO_EXIT:-0}"
    fi
    if [[ "$1" == "compose" ]]; then
      shift
      case "$1" in
        version)
          exit "${FAKE_DOCKER_COMPOSE_VERSION_EXIT:-0}"
          ;;
        up)
          exit "${FAKE_DOCKER_COMPOSE_UP_EXIT:-0}"
          ;;
        ps)
          if [[ "$*" == *"--format"* ]]; then
            cat "${FAKE_PG_HEALTH_FILE:?FAKE_PG_HEALTH_FILE not set}"
            exit 0
          fi
          echo "fake ps output (diagnostics)"
          exit 0
          ;;
        stop)
          exit "${FAKE_DOCKER_COMPOSE_STOP_EXIT:-0}"
          ;;
        down)
          echo "FORBIDDEN CALL: docker compose down $*" >&2
          exit 99
          ;;
        *)
          echo "fake docker: unhandled compose subcommand: $*" >&2
          exit 1
          ;;
      esac
    fi
    echo "fake docker: unhandled command: $*" >&2
    exit 1
    """
).strip() + "\n"

FAKE_UV = textwrap.dedent(
    r"""
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ -n "${DEMO_TEST_CALL_LOG:-}" ]]; then
      printf 'uv %s\n' "$*" >> "$DEMO_TEST_CALL_LOG"
    fi
    if [[ "$1" == "sync" ]]; then
      exit "${FAKE_UV_SYNC_EXIT:-0}"
    fi
    if [[ "$1" == "run" ]]; then
      shift
      case "$1" in
        alembic)
          shift
          if [[ "$1" == "upgrade" ]]; then
            exit "${FAKE_ALEMBIC_UPGRADE_EXIT:-0}"
          elif [[ "$1" == "heads" ]]; then
            printf '%s\n' "${FAKE_ALEMBIC_HEADS_OUTPUT:-abc123 (head)}"
            exit 0
          fi
          echo "fake uv: unhandled alembic subcommand: $*" >&2
          exit 1
          ;;
        meyar)
          shift
          if [[ "$1" == "seed-demo" ]]; then
            if [[ -n "${FAKE_SEED_OUTPUT_FILE:-}" ]]; then
              cat "$FAKE_SEED_OUTPUT_FILE"
            fi
            exit "${FAKE_SEED_EXIT:-0}"
          fi
          echo "fake uv: unhandled meyar subcommand: $*" >&2
          exit 1
          ;;
        uvicorn)
          if [[ "${FAKE_UVICORN_CRASH:-0}" == "1" ]]; then
            exit 7
          fi
          if [[ "${FAKE_UVICORN_HANG:-0}" == "1" ]]; then
            exec sleep 300
          fi
          exec python3 "${FAKE_UVICORN_SERVER_SCRIPT:?not set}"
          ;;
        *)
          echo "fake uv: unhandled run subcommand: $*" >&2
          exit 1
          ;;
      esac
    fi
    echo "fake uv: unhandled invocation: $*" >&2
    exit 1
    """
).strip() + "\n"

FAKE_OLLAMA_AVAILABLE = textwrap.dedent(
    r"""
    #!/usr/bin/env bash
    if [[ "${1:-}" == "list" ]]; then
      echo "NAME  ID  SIZE  MODIFIED"
      exit 0
    fi
    exit 1
    """
).strip() + "\n"

FAKE_UVICORN_SERVER = textwrap.dedent(
    r"""
    import http.server
    import os


    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/v1/health":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status": "ok"}')
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):
            pass


    host = os.environ.get("FAKE_UVICORN_HOST", "127.0.0.1")
    port = int(os.environ.get("FAKE_UVICORN_PORT", "8000"))
    server = http.server.HTTPServer((host, port), Handler)
    server.serve_forever()
    """
).strip() + "\n"


def _port_free(port: int = PORT, host: str = HOST) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        try:
            s.connect((host, port))
            return False
        except OSError:
            return True


def _health_ok(timeout: float = 1.0) -> bool:
    """True only once /api/v1/health actually answers 200 — a much tighter
    proxy for 'demo-up.sh's own readiness loop has declared ready' than a
    bare TCP connect, which can succeed a moment before the script's own
    curl check does and race its cleanup trap if a signal follows too
    quickly."""
    try:
        with urllib.request.urlopen(
            f"http://{HOST}:{PORT}/api/v1/health", timeout=timeout
        ) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _wait_for(predicate, timeout: float = 10.0, interval: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


READY_BANNER = "MEYAR LOCAL DEMO READY"


class _RunningDemo:
    """Streams a backgrounded demo-up.sh's combined stdout/stderr so a test
    can wait for the literal readiness banner (steady state) before sending
    Ctrl+C, instead of racing an external TCP/HTTP probe against the
    script's own internal readiness loop and its cleanup trap."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self.lines: list[str] = []
        self._event = threading.Event()
        self._marker: str | None = None
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.append(line)
            if self._marker is not None and self._marker in line:
                self._event.set()
        self.proc.stdout.close()

    def wait_for_ready(self, timeout: float = 15.0) -> bool:
        self._marker = READY_BANNER
        if any(READY_BANNER in line for line in self.lines):
            self._event.set()
        return self._event.wait(timeout)

    def stop(self, timeout: float = 10.0) -> str:
        self.proc.send_signal(signal.SIGINT)
        self.proc.wait(timeout=timeout)
        self._thread.join(timeout=5)
        return "".join(self.lines)

    def output_so_far(self) -> str:
        return "".join(self.lines)


class DemoScriptsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        if not _port_free():
            self.skipTest(f"port {PORT} is already in use on this machine; cannot safely test")

        self.tmp = Path(tempfile.mkdtemp(prefix="meyar-demo-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.fake_bin = self.tmp / "bin"
        self.fake_bin.mkdir()
        _write_executable(self.fake_bin / "docker", FAKE_DOCKER)
        _write_executable(self.fake_bin / "uv", FAKE_UV)

        self.repo = self.tmp / "repo"
        (self.repo / "backend").mkdir(parents=True)
        (self.repo / "backend" / ".env.example").write_text("MEYAR_ENV=development\n")
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"], cwd=self.repo, check=True
        )
        subprocess.run(["git", "config", "user.name", "demo-script-tests"], cwd=self.repo, check=True)
        (self.repo / "README.md").write_text("test repo\n")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=self.repo, check=True)

        self.pg_health_file = self.tmp / "pg_health"
        self.pg_health_file.write_text("healthy")

        self.call_log = self.tmp / "calls.log"
        self.call_log.write_text("")

        self.uvicorn_server_script = self.tmp / "fake_uvicorn_server.py"
        self.uvicorn_server_script.write_text(FAKE_UVICORN_SERVER)

        self.seed_output_file = self.tmp / "seed_output.txt"
        self.seed_output_file.write_text(
            "Demo tenant: 11111111-1111-1111-1111-111111111111\n"
            "Candidates created: 9\n"
            "--- Machine/API credential (for REST API testing) ---\n"
            "API key prefix (safe to log/display): mk_test\n"
            "API key (shown once, store it now):\n"
            "sk_test_FAKE_API_KEY_NOT_REAL\n"
            "--- Human/UI login (for the normal /ui/login screen) ---\n"
            "Username: demo.hr\n"
            "Temporary password (shown once, never persisted in plaintext):\n"
            "FAKE_PASSWORD_abc123XYZ\n"
        )

        # Deliberately excludes /usr/local/bin and similar: a real `ollama`
        # (or other tool) installed there on the host machine must never
        # leak into these isolated scenarios.
        real_path = "/usr/bin:/bin"
        self.base_env = {
            "PATH": f"{self.fake_bin}:{real_path}",
            "HOME": str(self.tmp),
            "DEMO_TEST_CALL_LOG": str(self.call_log),
            "FAKE_PG_HEALTH_FILE": str(self.pg_health_file),
            "FAKE_UVICORN_SERVER_SCRIPT": str(self.uvicorn_server_script),
            "FAKE_SEED_OUTPUT_FILE": str(self.seed_output_file),
            "MEYAR_DEMO_POSTGRES_TIMEOUT_SECONDS": "2",
            "MEYAR_DEMO_UVICORN_TIMEOUT_SECONDS": "5",
        }

    # -- helpers --------------------------------------------------------

    def run_up(self, extra_env=None, args=None, background=False, timeout=20):
        env = dict(self.base_env)
        env.update(extra_env or {})
        cmd = ["bash", str(DEMO_UP), *(args or [])]
        if background:
            return subprocess.Popen(
                cmd,
                cwd=self.repo,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        return subprocess.run(
            cmd,
            cwd=self.repo,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )

    def run_down(self, extra_env=None, timeout=15):
        env = dict(self.base_env)
        env.update(extra_env or {})
        return subprocess.run(
            ["bash", str(DEMO_DOWN)],
            cwd=self.repo,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )

    def calls(self) -> str:
        return self.call_log.read_text()

    def stop_background(self, proc: subprocess.Popen, timeout: float = 10) -> str:
        proc.send_signal(signal.SIGINT)
        out, _ = proc.communicate(timeout=timeout)
        return out

    def run_up_until_ready(self, extra_env=None, args=None, timeout=15) -> _RunningDemo:
        """Start demo-up.sh in the background and block until its own
        readiness banner appears (steady state), not merely until the port
        answers — see _RunningDemo."""
        proc = self.run_up(extra_env=extra_env, args=args, background=True)
        demo = _RunningDemo(proc)
        ready = demo.wait_for_ready(timeout=timeout)
        if not ready:
            demo.stop()
            self.fail(
                f"demo-up.sh never printed the ready banner within {timeout}s:\n"
                f"{demo.output_so_far()}"
            )
        return demo

    # -- env handling -----------------------------------------------------

    def test_env_created_when_missing(self):
        env_path = self.repo / "backend" / ".env"
        self.assertFalse(env_path.exists())
        demo = self.run_up_until_ready()
        demo.stop()
        self.assertIn("MEYAR_ENV=development", env_path.read_text())

    def test_env_not_overwritten_when_present(self):
        env_path = self.repo / "backend" / ".env"
        env_path.write_text("MEYAR_CUSTOM_MARKER=owner-set-value\n")
        demo = self.run_up_until_ready()
        demo.stop()
        self.assertEqual(env_path.read_text(), "MEYAR_CUSTOM_MARKER=owner-set-value\n")

    # -- prerequisites ------------------------------------------------------

    def test_missing_prerequisite_fails(self):
        (self.fake_bin / "uv").unlink()
        result = self.run_up()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Required command not found", result.stdout)

    # -- postgres / migrations / seed gating ---------------------------------

    def test_postgres_timeout_blocks_migration(self):
        self.pg_health_file.write_text("starting")
        result = self.run_up()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not become healthy", result.stdout)
        self.assertNotIn("alembic upgrade", self.calls())

    def test_migration_failure_blocks_seed_and_server(self):
        result = self.run_up(extra_env={"FAKE_ALEMBIC_UPGRADE_EXIT": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("alembic upgrade head failed", result.stdout)
        self.assertNotIn("meyar seed-demo", self.calls())
        self.assertNotIn("run uvicorn", self.calls())

    def test_multiple_alembic_heads_blocks_seed_and_server(self):
        result = self.run_up(
            extra_env={"FAKE_ALEMBIC_HEADS_OUTPUT": "aaa (head)\nbbb (head)"}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Expected exactly one Alembic head", result.stdout)
        self.assertNotIn("meyar seed-demo", self.calls())

    def test_seed_failure_blocks_server(self):
        result = self.run_up(extra_env={"FAKE_SEED_EXIT": "3"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("meyar seed-demo failed", result.stdout)
        self.assertNotIn("run uvicorn", self.calls())

    def test_seed_success_prints_credentials_exactly_once(self):
        demo = self.run_up_until_ready()
        out = demo.stop()
        self.assertEqual(out.count("FAKE_PASSWORD_abc123XYZ"), 1)
        self.assertNotIn("sk_test_FAKE_API_KEY_NOT_REAL", out)

    # -- ollama optionality ---------------------------------------------------

    def test_ollama_unavailable_is_warning_only(self):
        demo = self.run_up_until_ready()
        out = demo.stop()
        self.assertIn("Ollama unavailable", out)
        self.assertIn(READY_BANNER, out)

    def test_ollama_available_reported(self):
        _write_executable(self.fake_bin / "ollama", FAKE_OLLAMA_AVAILABLE)
        demo = self.run_up_until_ready()
        out = demo.stop()
        self.assertIn("AVAILABLE", out)
        self.assertNotIn("Ollama unavailable", out)

    # -- port conflict handling -----------------------------------------------

    def test_port_conflict_unknown_process_fails_without_killing(self):
        # A prior test's server may have been killed only moments ago;
        # give the kernel a beat to actually release the port before this
        # test's own decoy tries to bind it.
        self.assertTrue(_wait_for(_port_free, timeout=5))
        decoy = subprocess.Popen(
            [
                "python3",
                "-c",
                "import socket,time\n"
                "s=socket.socket()\n"
                "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
                "s.bind(('127.0.0.1', 8000))\n"
                "s.listen(1)\n"
                "time.sleep(30)\n",
            ],
        )
        try:
            self.assertTrue(_wait_for(lambda: not _port_free(), timeout=5))
            result = self.run_up()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Refusing to start a duplicate", result.stdout)
            self.assertIsNone(
                decoy.poll(), "demo-up.sh must not kill an unrelated process on the port"
            )
            self.assertNotIn("run uvicorn", self.calls())
        finally:
            decoy.kill()
            decoy.wait(timeout=5)

    def test_port_conflict_already_running_meyar_reuses(self):
        healthy_server = subprocess.Popen(
            ["python3", str(self.uvicorn_server_script)],
            env={**os.environ, "FAKE_UVICORN_HOST": HOST, "FAKE_UVICORN_PORT": str(PORT)},
        )
        try:
            self.assertTrue(_wait_for(_health_ok, timeout=5))
            result = self.run_up()
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("already running and healthy", result.stdout)
            self.assertIn(READY_BANNER, result.stdout)
            self.assertNotIn("run uvicorn", self.calls())
            self.assertIsNone(healthy_server.poll())
        finally:
            healthy_server.kill()
            healthy_server.wait(timeout=5)

    # -- signal / cleanup ------------------------------------------------------

    def test_cleanup_targets_only_owned_pid(self):
        decoy = subprocess.Popen(["sleep", "60"])
        try:
            demo = self.run_up_until_ready()
            demo.stop()
            self.assertTrue(
                _wait_for(_port_free, timeout=10), "owned uvicorn stub must be stopped"
            )
            self.assertIsNone(
                decoy.poll(), "an unrelated process must never be touched by cleanup"
            )
        finally:
            decoy.kill()
            decoy.wait(timeout=5)

    # -- secrets ------------------------------------------------------------

    def test_secrets_never_written_to_disk_by_wrapper(self):
        demo = self.run_up_until_ready()
        demo.stop()
        secret = "FAKE_PASSWORD_abc123XYZ"
        for path in self.repo.rglob("*"):
            if path.is_file() and path.name != "seed_output.txt" and ".git" not in path.parts:
                content = path.read_bytes()
                self.assertNotIn(secret.encode(), content, f"secret leaked into {path}")

    # -- demo-down ------------------------------------------------------------

    def test_demo_down_stops_postgres_only(self):
        result = self.run_down()
        self.assertEqual(result.returncode, 0)
        self.assertIn("compose stop postgres", self.calls())
        self.assertNotIn("compose down", self.calls())

    def test_demo_down_reports_running_server_without_killing(self):
        server = subprocess.Popen(
            ["python3", str(self.uvicorn_server_script)],
            env={**os.environ, "FAKE_UVICORN_HOST": HOST, "FAKE_UVICORN_PORT": str(PORT)},
        )
        try:
            self.assertTrue(_wait_for(lambda: not _port_free(), timeout=5))
            result = self.run_down()
            self.assertEqual(result.returncode, 0)
            self.assertIn("MEYAR process may still be running", result.stdout)
            self.assertIsNone(server.poll())
        finally:
            server.kill()
            server.wait(timeout=5)


class ScriptContentStaticChecksTestCase(unittest.TestCase):
    """Forbidden-pattern checks that don't need runtime simulation."""

    def _read(self, path: Path) -> str:
        return path.read_text()

    def test_no_destructive_docker_compose_down_v(self):
        for script in (DEMO_UP, DEMO_DOWN):
            self.assertNotIn("compose down", self._read(script))

    def test_no_broad_process_kill(self):
        for script in (DEMO_UP, DEMO_DOWN):
            content = self._read(script)
            for forbidden in ("pkill", "killall", "kill -9 0", "kill 0"):
                self.assertNotIn(forbidden, content)

    def test_no_sudo(self):
        for script in (DEMO_UP, DEMO_DOWN):
            self.assertNotIn("sudo", self._read(script))

    def test_no_eval(self):
        for script in (DEMO_UP, DEMO_DOWN):
            content = self._read(script)
            self.assertNotRegex(content, r"(^|\s)eval(\s|$)")

    def test_no_reset_flag(self):
        self.assertNotIn("--reset", self._read(DEMO_UP))

    def test_uses_set_euo_pipefail(self):
        for script in (DEMO_UP, DEMO_DOWN):
            self.assertIn("set -euo pipefail", self._read(script))

    def test_no_lan_bind(self):
        self.assertNotIn("0.0.0.0", self._read(DEMO_UP))

    def test_no_uvicorn_reload_flag(self):
        self.assertNotIn("--reload", self._read(DEMO_UP))

    def test_scripts_are_executable(self):
        for script in (DEMO_UP, DEMO_DOWN):
            self.assertTrue(os.access(script, os.X_OK), f"{script} must be executable")


if __name__ == "__main__":
    unittest.main()
