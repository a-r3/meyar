"""Opt-in structural evidence only; never changes resource ownership or outcomes.

Writes to the original stderr so pytest capture cannot hide a live hang.
No SQL text, parameters, frame locals, exception messages or task-name payloads.
"""

import asyncio
import json
import os
import re
import sys
import threading
import time
import traceback
import weakref

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
_thread = None
_output_fd = None
THRESHOLD_SECONDS = 120


def _emit(kind, **fields):
    with _lock:
        line = "MEYAR_HANG " + json.dumps({"kind": kind, **fields}) + "\n"
        if _output_fd is not None:
            os.write(_output_fd, line.encode())
        else:
            print(line, end="", file=sys.__stderr__, flush=True)


def _frames():
    return [{"file": f.filename, "line": f.lineno, "function": f.name}
            for f in traceback.extract_stack(limit=16)[:-1]]


def _await_stack(coro):
    frames = []
    seen = set()
    while coro is not None and id(coro) not in seen and len(frames) < 40:
        seen.add(id(coro))
        frame = getattr(coro, "cr_frame", getattr(coro, "gi_frame", None))
        if frame is not None:
            frames.append({"file": frame.f_code.co_filename, "line": frame.f_lineno,
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
    _emit("CHECKOUT", **owner)


def _checkin(connection, record):
    with _lock:
        owner = _checked_out.pop(id(record), None)
    _emit("CHECKIN", connection=id(record), pid=record.info.get("hang_pid"),
          checkout_owner=owner)


def _engine_connect(connection):
    with _lock:
        _pools.add(connection.engine.pool)


def _session_begin(session, transaction, connection):
    driver = connection.connection.driver_connection
    get_pid = getattr(driver, "get_server_pid", None)
    pid = get_pid() if callable(get_pid) else None
    _emit("SESSION_BEGIN", session=id(session), transaction=id(transaction), pid=pid,
          engine=id(connection.engine), pool=id(connection.engine.pool),
          nodeid=_phase[0], phase=_phase[1])


def _pool_snapshot():
    with _lock:
        pools = list(_pools)
        owners = list(_checked_out.values())
    _emit("POOLS", pools=[{"pool": id(p), "status": p.status()} for p in pools],
          checked_out=owners)


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
                       CASE WHEN application_name LIKE 'meyar%'
                            THEN application_name ELSE '<redacted>' END AS application_name
                FROM pg_stat_activity
                WHERE datname=current_database() AND pid<>pg_backend_pid()
                ORDER BY pid LIMIT 100
            """)
            for row in rows:
                _emit("PG_ACTIVITY", **dict(row))
            locks = await conn.fetch("""
                SELECT l.pid, l.locktype, l.mode, l.granted, c.relname AS relation
                FROM pg_locks l LEFT JOIN pg_class c ON c.oid=l.relation
                WHERE l.pid IN (SELECT pid FROM pg_stat_activity
                    WHERE datname=current_database() AND pid<>pg_backend_pid())
                ORDER BY l.pid, l.granted, l.locktype LIMIT 300
            """)
            for row in locks:
                _emit("PG_LOCK", **dict(row))
            _emit("PG_SNAPSHOT_END", activities=len(rows), locks=len(locks))
    finally:
        await conn.close(timeout=2)


def _watch():
    reported = None
    while not _stop.wait(1):
        with _lock:
            current = _phase
            loops = list(_loops)
        if current[1] == "IDLE" or current == reported:
            continue
        if time.monotonic() - current[2] < THRESHOLD_SECONDS:
            continue
        reported = current
        _emit("WATCHDOG", nodeid=current[0], phase=current[1],
              elapsed=time.monotonic()-current[2])
        _pool_snapshot()
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


def pytest_configure(config):
    global _thread, _output_fd
    # pytest's fd capture redirects even sys.__stderr__. Duplicate its saved
    # original stderr so markers/watchdog remain visible before a test ends.
    capture = config.pluginmanager.getplugin("capturemanager")
    global_capture = getattr(capture, "_global_capturing", None)
    stderr_capture = getattr(global_capture, "err", None)
    original_fd = getattr(stderr_capture, "targetfd_save", 2)
    _output_fd = os.dup(original_fd)
    event.listen(Pool, "connect", _connect)
    event.listen(Pool, "checkout", _checkout)
    event.listen(Pool, "checkin", _checkin)
    event.listen(Engine, "engine_connect", _engine_connect)
    event.listen(Session, "after_begin", _session_begin)
    event.listen(Pool, "reset", _reset)
    _thread = threading.Thread(target=_watch, name="meyar-hang-watchdog", daemon=True)
    _thread.start()


def _reset(connection, record, reset_state):
    _emit("RESET", connection=id(record), pid=record.info.get("hang_pid"),
          transaction_was_reset=reset_state.transaction_was_reset,
          terminate_only=reset_state.terminate_only)


def pytest_unconfigure(config):
    global _output_fd
    _stop.set()
    for name, callback in [("connect", _connect), ("checkout", _checkout),
                           ("checkin", _checkin), ("reset", _reset)]:
        event.remove(Pool, name, callback)
    event.remove(Engine, "engine_connect", _engine_connect)
    event.remove(Session, "after_begin", _session_begin)
    if _thread is not None:
        _thread.join(timeout=21)
    if _output_fd is not None and (_thread is None or not _thread.is_alive()):
        os.close(_output_fd)
        _output_fd = None


def _mark(item, phase, boundary):
    global _phase
    with _lock:
        _phase = (item.nodeid, phase if boundary == "START" else "BETWEEN", time.monotonic())
        if boundary == "START":
            for value in item.funcargs.values():
                if isinstance(value, asyncio.Runner):
                    _loops.add(value.get_loop())
    _emit("PHASE", nodeid=item.nodeid, phase=phase, boundary=boundary)
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
