"""Opt-in structural evidence only; never changes resource ownership or outcomes.

Writes to the original stderr so pytest capture cannot hide a live hang.
No SQL text, parameters, frame locals, exception messages or task-name payloads.
"""

import asyncio
import hashlib
import json
import os
import queue
import re
import stat
import threading
import time
import traceback
import weakref
from collections import deque
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import Pool

_lock = threading.RLock()
_stop = threading.Event()
_phase = ("", "IDLE", 0.0)
_loops = weakref.WeakSet()
_pools = weakref.WeakSet()
_checked_out = {}
_recent_events = deque(maxlen=256)
_thread = None
# Ownership: _output_fd (direct pipe/terminal output) is owned by this module
# and closed only after being detached under _lock; emitters write to their
# own dup of it. A regular-file fd is owned solely by its writer thread.
_output_fd = None
_output_queue = None
_writer_thread = None
_dropped_records = 0
_repo_root = Path(__file__).resolve().parents[2]
THRESHOLD_SECONDS = 120
MAX_RECORD_BYTES = 4096
WATCHDOG_JOIN_SECONDS = 21
WRITER_JOIN_SECONDS = 1


def _safe_nodeid(nodeid):
    """Keep only code-owned collection identity; hash arbitrary parameter IDs."""
    base, bracket, parameter = nodeid.partition("[")
    if (
        len(base) > 180
        or not re.fullmatch(r"tests/[A-Za-z0-9_./-]+\.py(?:::[A-Za-z_][A-Za-z0-9_]*)+", base)
        or ".." in Path(base.split("::", 1)[0]).parts
    ):
        return "nodeid-sha256=" + hashlib.sha256(nodeid.encode()).hexdigest()[:12]
    if not bracket:
        return base
    return base + "[param-sha256=" + hashlib.sha256(parameter.encode()).hexdigest()[:12] + "]"


def _safe_path(filename):
    if filename.startswith("<") and filename.endswith(">"):
        return "generated"
    path = Path(filename)
    if path.is_absolute():
        try:
            relative = path.relative_to(_repo_root)
        except ValueError:
            relative = None
    else:
        relative = None
    if (relative is not None and ".." not in relative.parts
            and re.fullmatch(r"[A-Za-z0-9_./-]{1,180}", str(relative))):
        return str(relative)
    if "/site-packages/" in filename:
        package = filename.split("/site-packages/", 1)[1].split("/", 1)[0]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", package):
            return "site-packages/" + package
    if filename.startswith(("/usr/lib/python", "/usr/local/lib/python")):
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", path.name):
            return "stdlib/" + path.name
        return "stdlib"
    return "external-sha256=" + hashlib.sha256(filename.encode()).hexdigest()[:12]


def _drop():
    global _dropped_records
    with _lock:
        _dropped_records += 1


def _emit(kind, **fields):
    try:
        line = ("MEYAR_HANG " + json.dumps({"kind": kind, **fields}) + "\n").encode()
    except (TypeError, ValueError):
        _drop()
        return
    if len(line) > MAX_RECORD_BYTES:
        _drop()
        return
    owned_fd = None
    with _lock:
        output_queue = _output_queue
        if output_queue is None and _output_fd is not None:
            # The dup shares the open file description (and its O_NONBLOCK)
            # but is ours alone: closing or reusing the module fd number
            # cannot redirect this write. dup never waits on the pipe.
            try:
                owned_fd = os.dup(_output_fd)
            except OSError:
                pass
    if output_queue is not None:
        try:
            output_queue.put_nowait(line)
        except queue.Full:
            _drop()
        return
    if owned_fd is None:
        _drop()
        return
    try:
        if os.write(owned_fd, line) != len(line):
            _drop()
    except OSError:
        _drop()
    finally:
        try:
            os.close(owned_fd)
        except OSError:
            pass


def _write_regular_file(stop, output_queue, fd):
    """Only this daemon worker may wait on regular-file I/O; tests never do.

    It owns ``fd`` and is the only code that closes it, so teardown can never
    close a descriptor this thread may still write to.
    """
    try:
        while not stop.is_set() or not output_queue.empty():
            try:
                line = output_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                os.write(fd, line)
            except OSError:
                pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _frames():
    return [{"file": _safe_path(f.filename), "line": f.lineno, "function": f.name}
            for f in traceback.extract_stack(limit=16)[:-1]]


def _await_stack(coro):
    frames = []
    seen = set()
    while coro is not None and id(coro) not in seen and len(frames) < 40:
        seen.add(id(coro))
        frame = getattr(coro, "cr_frame", getattr(coro, "gi_frame", None))
        if frame is not None:
            frames.append({"file": _safe_path(frame.f_code.co_filename), "line": frame.f_lineno,
                           "function": frame.f_code.co_qualname})
        coro = getattr(coro, "cr_await", getattr(coro, "gi_yieldfrom", None))
    return frames


def _connect(connection, record):
    driver = getattr(connection, "driver_connection", None)
    get_pid = getattr(driver, "get_server_pid", None)
    record.info["hang_pid"] = get_pid() if callable(get_pid) else None


def _checkout(connection, record, proxy):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    with _lock:
        if loop is not None:
            _loops.add(loop)
        task = asyncio.current_task() if loop is not None else None
        owner = {"connection": id(record), "pid": record.info.get("hang_pid"),
                 "nodeid": _phase[0], "phase": _phase[1], "task": id(task) if task else None,
                 "stack": _await_stack(task.get_coro()) if task else _frames()}
        _checked_out[id(record)] = owner
        _recent_events.append({"kind": "CHECKOUT", "connection": id(record),
                               "pid": owner["pid"], "nodeid": owner["nodeid"]})


def _checkin(connection, record):
    with _lock:
        owner = _checked_out.pop(id(record), None)
        _recent_events.append({"kind": "CHECKIN", "connection": id(record),
                               "pid": record.info.get("hang_pid"),
                               "checkout_nodeid": owner["nodeid"] if owner else None})


def _engine_connect(connection):
    with _lock:
        _pools.add(connection.engine.pool)


def _session_begin(session, transaction, connection):
    driver = connection.connection.driver_connection
    get_pid = getattr(driver, "get_server_pid", None)
    pid = get_pid() if callable(get_pid) else None
    with _lock:
        _recent_events.append({"kind": "SESSION_BEGIN", "session": id(session),
                               "transaction": id(transaction), "pid": pid,
                               "engine": id(connection.engine), "pool": id(connection.engine.pool),
                               "nodeid": _phase[0], "phase": _phase[1]})


def _pool_snapshot():
    with _lock:
        pools = list(_pools)
        owners = list(_checked_out.values())
    for pool in pools[:100]:
        _emit("POOL", pool=id(pool), status=pool.status())
    for owner in owners[:100]:
        _emit("CHECKED_OUT", **owner)
    _emit("POOL_SNAPSHOT_END", pools=len(pools), checked_out=len(owners),
          truncated=len(pools) > 100 or len(owners) > 100)


def _tasks(loop, source="event_loop"):
    # Runs on the observed loop, never touches task locals or repr(coroutine).
    tasks = sorted(asyncio.all_tasks(loop), key=id)
    for task in tasks[:100]:
        name = task.get_name()
        if not re.fullmatch(r"Task-\d+", name):
            name = "<custom-name-redacted>"
        _emit("TASK", loop=id(loop), source=source, task=id(task), name=name,
              coroutine=getattr(task.get_coro(), "__qualname__", type(task.get_coro()).__name__),
              done=task.done(), cancelled=task.cancelled(), stack=_await_stack(task.get_coro()))
    _emit("TASK_SNAPSHOT_END", loop=id(loop), source=source,
          count=len(tasks), truncated=len(tasks) > 100)


async def _postgres():
    # A fresh driver connection on the watchdog thread's own event loop:
    # independent of every engine, pool, connection and loop under test.
    from conftest import TEST_DATABASE_URL

    url = make_url(TEST_DATABASE_URL)
    conn = await asyncpg.connect(host=url.host, port=url.port, user=url.username,
                                 password=url.password, database=url.database,
                                 timeout=5, command_timeout=5,
                                 server_settings={"application_name": "meyar-hang-observer"})
    try:
        async with conn.transaction(readonly=True):
            rows = await conn.fetch("""
                SELECT pid, state, wait_event_type, wait_event,
                       extract(epoch FROM clock_timestamp()-xact_start)::float AS transaction_age,
                       extract(epoch FROM clock_timestamp()-query_start)::float AS query_age,
                       pg_blocking_pids(pid) AS blockers,
                       CASE WHEN application_name = 'meyar-hang-observer'
                            THEN 'OBSERVER' ELSE 'OTHER' END AS application_class
                FROM pg_stat_activity
                WHERE datname=current_database() AND pid<>pg_backend_pid()
                ORDER BY pid LIMIT 100
            """)
            for row in rows:
                _emit("PG_ACTIVITY", **dict(row))
            locks = await conn.fetch("""
                SELECT l.pid, l.locktype, l.mode, l.granted,
                       l.relation::bigint AS relation_oid
                FROM pg_locks l
                WHERE l.pid IN (SELECT pid FROM pg_stat_activity
                    WHERE datname=current_database() AND pid<>pg_backend_pid())
                ORDER BY l.pid, l.granted, l.locktype LIMIT 300
            """)
            for row in locks:
                _emit("PG_LOCK", **dict(row))
            _emit("PG_SNAPSHOT_END", activities=len(rows), locks=len(locks))
    finally:
        await conn.close(timeout=2)


def _watch(stop):
    reported = None
    while not stop.wait(1):
        with _lock:
            current = _phase
            loops = list(_loops)
        if current[1] == "IDLE" or current == reported:
            continue
        if time.monotonic() - current[2] < THRESHOLD_SECONDS:
            continue
        reported = current
        _emit("WATCHDOG", nodeid=current[0], phase=current[1],
              elapsed=time.monotonic()-current[2], dropped_records=_dropped_records)
        _pool_snapshot()
        with _lock:
            recent = list(_recent_events)
        for record in recent:
            _emit("RECENT_EVENT", **record)
        for loop in loops:
            _emit("LOOP", loop=id(loop), running=loop.is_running(), closed=loop.is_closed())
            if loop.is_running() and not loop.is_closed():
                captured = threading.Event()

                def capture(target=loop, signal=captured):
                    try:
                        _tasks(target)
                    finally:
                        signal.set()

                try:
                    loop.call_soon_threadsafe(capture)
                    if not captured.wait(2):
                        # A blocked loop cannot run its own callback. Inspect
                        # read-only task metadata from the observer thread.
                        _tasks(loop, source="watchdog_thread")
                except RuntimeError:
                    _emit("LOOP_UNAVAILABLE", loop=id(loop))
                except Exception as exc:
                    _emit("TASK_SNAPSHOT_ERROR", loop=id(loop), error_type=type(exc).__name__)
        try:
            asyncio.run(asyncio.wait_for(_postgres(), timeout=20))
        except Exception as exc:
            _emit("PG_OBSERVER_ERROR", error_type=type(exc).__name__)


def _shutdown():
    """Detach output under _lock first, then clean up with bounded joins.

    No new emitter can acquire the old output once detached; one already
    writing holds its own dup. The writer fd is never closed here: a writer
    outliving its join keeps its fd until it exits (daemon/process exit
    bounds it), which is preferable to writing into a reused descriptor.
    """
    global _output_fd, _output_queue, _writer_thread
    with _lock:
        _stop.set()
        direct_fd, _output_fd = _output_fd, None
        _output_queue = None
        watchdog, writer = _thread, _writer_thread
        _writer_thread = None
    if direct_fd is not None:
        try:
            os.close(direct_fd)
        except OSError:
            pass
    if watchdog is not None:
        watchdog.join(timeout=WATCHDOG_JOIN_SECONDS)
    if writer is not None:
        writer.join(timeout=WRITER_JOIN_SECONDS)


def pytest_configure(config):
    global _thread, _output_fd, _output_queue, _writer_thread
    global _stop, _phase, _loops, _pools, _dropped_records
    # A new pytest.main() in this interpreter must not inherit old state.
    _shutdown()
    with _lock:
        _stop = threading.Event()
        _phase = ("", "IDLE", 0.0)
        _loops = weakref.WeakSet()
        _pools = weakref.WeakSet()
        _checked_out.clear()
        _recent_events.clear()
        _dropped_records = 0
    # A separate open-file description keeps O_NONBLOCK off pytest's own pipe.
    # A regular file instead shares its offset with pytest via dup and writes
    # on a bounded-queue daemon: independent offsets can overwrite output.
    capture = config.pluginmanager.getplugin("capturemanager")
    global_capture = getattr(capture, "_global_capturing", None)
    stderr_capture = getattr(global_capture, "err", None)
    original_fd = getattr(stderr_capture, "targetfd_save", 2)
    try:
        duplicate = os.dup(original_fd)
    except OSError:
        duplicate = None
    if duplicate is not None:
        try:
            regular = stat.S_ISREG(os.fstat(duplicate).st_mode)
        except OSError:
            regular = False
        if regular:
            output_queue = queue.Queue(maxsize=1024)
            writer = threading.Thread(
                target=_write_regular_file, args=(_stop, output_queue, duplicate),
                name="meyar-hang-output", daemon=True,
            )
            try:
                writer.start()  # From here the writer alone owns `duplicate`.
            except RuntimeError:
                os.close(duplicate)
            else:
                with _lock:
                    _output_queue, _writer_thread = output_queue, writer
        else:
            try:
                direct_fd = os.open(f"/proc/self/fd/{duplicate}",
                                    os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            except OSError:
                direct_fd = None
            finally:
                os.close(duplicate)
            with _lock:
                _output_fd = direct_fd
    event.listen(Pool, "connect", _connect)
    event.listen(Pool, "checkout", _checkout)
    event.listen(Pool, "checkin", _checkin)
    event.listen(Engine, "engine_connect", _engine_connect)
    event.listen(Session, "after_begin", _session_begin)
    event.listen(Pool, "reset", _reset)
    _thread = threading.Thread(target=_watch, args=(_stop,),
                               name="meyar-hang-watchdog", daemon=True)
    _thread.start()


def _reset(connection, record, reset_state):
    with _lock:
        _recent_events.append({"kind": "RESET", "connection": id(record),
                               "pid": record.info.get("hang_pid"),
                               "transaction_was_reset": reset_state.transaction_was_reset,
                               "terminate_only": reset_state.terminate_only})


def pytest_unconfigure(config):
    for name, callback in [("connect", _connect), ("checkout", _checkout),
                           ("checkin", _checkin), ("reset", _reset)]:
        event.remove(Pool, name, callback)
    event.remove(Engine, "engine_connect", _engine_connect)
    event.remove(Session, "after_begin", _session_begin)
    _shutdown()


def _mark(item, phase, boundary):
    global _phase
    with _lock:
        _phase = (_safe_nodeid(item.nodeid),
                  phase if boundary == "START" else "BETWEEN", time.monotonic())
        if boundary == "START":
            for value in item.funcargs.values():
                if isinstance(value, asyncio.Runner):
                    _loops.add(value.get_loop())
    _emit("PHASE", nodeid=_phase[0], phase=phase, boundary=boundary)
    _pool_snapshot()


def pytest_sessionfinish(session, exitstatus):
    global _phase
    with _lock:
        _phase = ("", "IDLE", time.monotonic())


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_setup(item):
    _mark(item, "SETUP", "START")
    try:
        return (yield)
    finally:
        _mark(item, "SETUP", "END")


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_call(item):
    _mark(item, "CALL", "START")
    try:
        return (yield)
    finally:
        _mark(item, "CALL", "END")


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item):
    _mark(item, "TEARDOWN", "START")
    try:
        return (yield)
    finally:
        _mark(item, "TEARDOWN", "END")
