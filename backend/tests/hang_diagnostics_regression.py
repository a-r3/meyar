"""Explicitly invoked regression tests; excluded from the ordinary testpaths pattern."""

import asyncio
import hashlib
import inspect
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import hang_diagnostics as diagnostics
import pytest
from conftest import TEST_DATABASE_URL
from sqlalchemy.engine import make_url


def test_nonblocking_full_and_closed_output_cannot_interrupt_pytest(monkeypatch):
    read_fd, write_fd = os.pipe()
    output_fd = os.open(f"/proc/self/fd/{write_fd}", os.O_WRONLY | os.O_NONBLOCK)
    try:
        assert os.get_blocking(write_fd)
        assert not os.get_blocking(output_fd)
        while True:
            try:
                os.write(output_fd, b"x" * 4096)
            except BlockingIOError:
                break
        before = diagnostics._dropped_records
        with monkeypatch.context() as patch:
            patch.setattr(diagnostics, "_output_fd", output_fd)
            patch.setattr(diagnostics, "_output_queue", None)
            start = time.monotonic()
            diagnostics._emit("SYNTHETIC", safe_count=1)
            assert time.monotonic() - start < 1
            assert diagnostics._dropped_records == before + 1
            os.close(output_fd)
            output_fd = -1
            diagnostics._emit("SYNTHETIC", safe_count=2)
            assert diagnostics._dropped_records == before + 2
    finally:
        if output_fd >= 0:
            os.close(output_fd)
        os.close(write_fd)
        os.close(read_fd)


def test_parameter_nodeid_is_stable_hashed_and_used_by_phase_marker(monkeypatch):
    raw = "tests/test_x.py::test_name[private JD text and a session token]"
    expected = "tests/test_x.py::test_name[param-sha256=" + hashlib.sha256(
        b"private JD text and a session token]"
    ).hexdigest()[:12] + "]"
    assert diagnostics._safe_nodeid(raw) == expected
    assert diagnostics._safe_nodeid(raw) == diagnostics._safe_nodeid(raw)
    assert diagnostics._safe_nodeid("tests/test_x.py::test_name") == (
        "tests/test_x.py::test_name"
    )
    emitted = []
    with monkeypatch.context() as patch:
        patch.setattr(diagnostics, "_emit", lambda kind, **fields: emitted.append(fields))
        diagnostics._mark(type("Item", (), {"nodeid": raw, "funcargs": {}})(), "CALL", "START")
    assert diagnostics._phase[0] == expected
    assert raw not in str(emitted)


def test_paths_are_repo_relative_or_nonreversible_labels():
    repo_file = Path(diagnostics.__file__)
    assert diagnostics._safe_path(str(repo_file)) == "backend/tests/hang_diagnostics.py"
    secret_path = "/home/private-user/candidate-jd/secret.py"
    safe = diagnostics._safe_path(secret_path)
    assert safe.startswith("external-sha256=")
    assert "/home/" not in safe and "candidate" not in safe
    assert diagnostics._safe_path("/usr/lib/python3.12/asyncio/tasks.py") == "stdlib/tasks.py"


async def test_observer_classifies_arbitrary_application_name_and_bounds_commands(monkeypatch):
    import asyncpg

    url = make_url(TEST_DATABASE_URL)
    connection = await asyncpg.connect(
        host=url.host, port=url.port, user=url.username, password=url.password,
        database=url.database, timeout=5,
        server_settings={"application_name": "meyar_SECRET_SENTINEL"},
    )
    emitted = []
    try:
        async with connection.transaction(readonly=True):
            await connection.fetchval("SELECT 1")
            with monkeypatch.context() as patch:
                patch.setattr(
                    diagnostics, "_emit", lambda kind, **fields: emitted.append((kind, fields))
                )
                await diagnostics._postgres()
        activity = [fields for kind, fields in emitted if kind == "PG_ACTIVITY"]
        assert any(row["application_class"] == "OTHER" for row in activity)
        assert "SECRET_SENTINEL" not in str(emitted)
        assert all("application_name" not in row for row in activity)
        assert all("relation" not in fields for kind, fields in emitted if kind == "PG_LOCK")
    finally:
        await connection.close(timeout=2)


async def test_observer_connect_query_and_close_have_finite_timeouts(monkeypatch):
    settings = {}

    class FakeTransaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class FakeConnection:
        def transaction(self, *, readonly):
            settings["readonly"] = readonly
            return FakeTransaction()

        async def fetch(self, query):
            return []

        async def close(self, *, timeout):
            settings["close_timeout"] = timeout

    async def fake_connect(**kwargs):
        settings.update(kwargs)
        return FakeConnection()

    with monkeypatch.context() as patch:
        patch.setattr(diagnostics.asyncpg, "connect", fake_connect)
        patch.setattr(diagnostics, "_emit", lambda *_args, **_kwargs: None)
        await diagnostics._postgres()
    assert settings["timeout"] == 5
    assert settings["command_timeout"] == 5
    assert settings["close_timeout"] == 2
    assert settings["readonly"] is True
    assert "asyncio.wait_for(_postgres(), timeout=20)" in inspect.getsource(diagnostics._watch)


async def test_custom_task_names_are_redacted(monkeypatch):
    task = asyncio.create_task(asyncio.sleep(3600), name="SECRET-SENTINEL")
    emitted = []
    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                diagnostics, "_emit", lambda kind, **fields: emitted.append((kind, fields))
            )
            diagnostics._tasks(asyncio.get_running_loop())
        assert "SECRET-SENTINEL" not in str(emitted)
        assert any(fields.get("name") == "<custom-name-redacted>"
                   for kind, fields in emitted if kind == "TASK")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_exception_cleanup_and_second_pytest_session(tmp_path):
    test_file = tmp_path / "synthetic_lifecycle.py"
    test_file.write_text(
        "import pytest\n"
        "@pytest.fixture\n"
        "def setup_error():\n"
        "    raise RuntimeError('synthetic setup failure')\n"
        "@pytest.fixture\n"
        "def teardown_error():\n"
        "    yield\n"
        "    raise RuntimeError('synthetic teardown failure')\n"
        "def test_setup(setup_error): pass\n"
        "def test_call(): raise RuntimeError('synthetic call failure')\n"
        "def test_teardown(teardown_error): pass\n"
        "def test_second_session():\n"
        "    import hang_diagnostics as d\n"
        "    assert d._thread.is_alive() and not d._stop.is_set()\n"
    )
    script = (
        "import sys, pytest; sys.path.insert(0, 'tests'); "
        "import hang_diagnostics as d; from sqlalchemy import event; "
        "from sqlalchemy.pool import Pool; from sqlalchemy.engine import Engine; "
        "from sqlalchemy.orm import Session; "
        f"path={str(test_file)!r}; "
        "check=lambda: (not d._thread.is_alive() and d._output_fd is None and "
        "d._output_queue is None and d._writer_thread is None and "
        "all(not event.contains(t,n,f) for t,n,f in "
        "[(Pool,'connect',d._connect),(Pool,'checkout',d._checkout),"
        "(Pool,'checkin',d._checkin),(Pool,'reset',d._reset),"
        "(Engine,'engine_connect',d._engine_connect),(Session,'after_begin',d._session_begin)])); "
        "a=pytest.main(['-q','--tb=no','-p','no:cacheprovider','-p','hang_diagnostics',"
        "path+'::test_setup',path+'::test_call',path+'::test_teardown']); "
        "assert a==1 and check(); "
        "b=pytest.main(['-q','--tb=no','-p','no:cacheprovider','-p','hang_diagnostics',"
        "path+'::test_second_session']); "
        "assert b==0 and check(); print('LIFECYCLE_PASS')"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert "LIFECYCLE_PASS" in result.stdout


def test_redirected_regular_file_keeps_diagnostic_markers(tmp_path):
    test_file = tmp_path / "synthetic_output.py"
    test_file.write_text("def test_ok(): assert True\n")
    log = tmp_path / "pytest.log"
    with log.open("wb") as output:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "-p", "hang_diagnostics", str(test_file)],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent)},
            stdout=output, stderr=subprocess.STDOUT, timeout=15, check=False,
        )
    assert result.returncode == 0
    content = log.read_text()
    assert '"kind": "PHASE"' in content
    assert "1 passed" in content


class _GatedOs:
    """Module-local ``os`` stand-in: pauses one write on Events, records closes."""

    def __init__(self, gate_fd=None):
        self.gate_fd = gate_fd
        self.write_fds = []
        self.closed = []
        self.acquired = threading.Event()
        self.proceed = threading.Event()

    def __getattr__(self, name):
        return getattr(os, name)

    def write(self, fd, data):
        self.write_fds.append(fd)
        if self.gate_fd is None or fd == self.gate_fd:
            self.acquired.set()
            assert self.proceed.wait(10)
        return os.write(fd, data)

    def close(self, fd):
        self.closed.append((fd, threading.get_ident()))
        return os.close(fd)


def _read_available(fd):
    os.set_blocking(fd, False)
    try:
        return os.read(fd, 65536)
    except BlockingIOError:
        return b""


def test_inflight_direct_write_cannot_reach_reused_descriptor_number(monkeypatch):
    read_a, write_a = os.pipe()
    module_fd = os.open(f"/proc/self/fd/{write_a}", os.O_WRONLY | os.O_NONBLOCK)
    gated = _GatedOs()
    unrelated = []
    try:
        with monkeypatch.context() as patch:
            patch.setattr(diagnostics, "os", gated)
            patch.setattr(diagnostics, "_output_fd", module_fd)
            patch.setattr(diagnostics, "_output_queue", None)
            patch.setattr(diagnostics, "_writer_thread", None)
            patch.setattr(diagnostics, "_thread", None)
            patch.setattr(diagnostics, "_stop", threading.Event())
            emitter = threading.Thread(
                target=diagnostics._emit, args=("SYNTHETIC",), kwargs={"marker": "RACE"}
            )
            emitter.start()
            # T1: the emitter holds its output and is about to write.
            assert gated.acquired.wait(10)
            owned_fd = gated.write_fds[0]
            assert owned_fd != module_fd
            # T2: teardown detaches and closes the module descriptor.
            diagnostics._shutdown()
            assert diagnostics._output_fd is None
            assert (module_fd, threading.get_ident()) in gated.closed
            # T3: deliberately make the old number name an unrelated pipe.
            read_b, write_b = os.pipe()
            unrelated += [read_b, write_b]
            if read_b == module_fd:
                read_b = os.dup(read_b)
                unrelated.append(read_b)
            if write_b != module_fd:
                os.dup2(write_b, module_fd)
                unrelated.append(module_fd)
            assert os.fstat(module_fd).st_ino == os.fstat(write_b).st_ino
            # T1 resumes: the delayed write must not land in the unrelated pipe.
            gated.proceed.set()
            emitter.join(10)
            assert not emitter.is_alive()
        assert _read_available(read_b) == b""
        assert b'"marker": "RACE"' in _read_available(read_a)
        assert owned_fd in [fd for fd, _ident in gated.closed]
    finally:
        gated.proceed.set()
        for fd in {*unrelated, read_a, write_a}:
            try:
                os.close(fd)
            except OSError:
                pass


def test_teardown_never_closes_fd_owned_by_writer_outliving_its_join(monkeypatch, tmp_path):
    writer_fd = os.open(tmp_path / "output.log", os.O_WRONLY | os.O_CREAT, 0o600)
    gated = _GatedOs(gate_fd=writer_fd)
    stop = threading.Event()
    output_queue = diagnostics.queue.Queue(maxsize=1024)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(diagnostics, "os", gated)
            patch.setattr(diagnostics, "WRITER_JOIN_SECONDS", 0.05)
            writer = threading.Thread(
                target=diagnostics._write_regular_file, args=(stop, output_queue, writer_fd),
                daemon=True,
            )
            patch.setattr(diagnostics, "_output_fd", None)
            patch.setattr(diagnostics, "_output_queue", output_queue)
            patch.setattr(diagnostics, "_writer_thread", writer)
            patch.setattr(diagnostics, "_thread", None)
            patch.setattr(diagnostics, "_stop", stop)
            writer.start()
            diagnostics._emit("SYNTHETIC", marker="SLOW")
            assert gated.acquired.wait(10)
            diagnostics._shutdown()
            # The writer outlived its bounded join: teardown detached it but
            # neither closed nor reassigned the fd the writer still owns.
            assert writer.is_alive()
            assert stop.is_set()
            assert diagnostics._output_queue is None and diagnostics._writer_thread is None
            assert writer_fd not in [fd for fd, _ident in gated.closed]
            os.fstat(writer_fd)
            gated.proceed.set()
            writer.join(10)
            assert not writer.is_alive()
        assert [ident for fd, ident in gated.closed if fd == writer_fd] == [writer.ident]
        assert b'"marker": "SLOW"' in (tmp_path / "output.log").read_bytes()
    finally:
        gated.proceed.set()
        if writer_fd not in [fd for fd, _ident in gated.closed]:
            os.close(writer_fd)
