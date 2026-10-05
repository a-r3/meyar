"""#46: actual cancelled/overproducing photo children must be reaped."""

import asyncio
import sys

import pytest

from meyar.services import candidate_photo_service as service


async def test_cancelled_photo_worker_is_reaped(monkeypatch):
    real_spawn = asyncio.create_subprocess_exec
    spawned = asyncio.Event()
    children = []

    async def spawn(*args, **kwargs):
        child = await real_spawn(
            sys.executable,
            "-c",
            "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(120)",
            **kwargs,
        )
        children.append(child)
        spawned.set()
        return child

    monkeypatch.setattr(service.asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(service._extract_isolated(b"SYNTHETIC", "PDF"))
    try:
        await asyncio.wait_for(spawned.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        assert children[0].returncode is not None, "cancelled request left a live child"
    finally:
        for child in children:
            if child.returncode is None:
                child.kill()
                await child.wait()


async def test_overproducing_photo_child_is_bounded_and_reaped(monkeypatch):
    real_spawn = asyncio.create_subprocess_exec
    children = []

    async def spawn(*args, **kwargs):
        child = await real_spawn(
            sys.executable,
            "-c",
            "import os,time;os.write(1,b'x'*2000000);time.sleep(120)",
            **kwargs,
        )
        children.append(child)
        return child

    monkeypatch.setattr(service.asyncio, "create_subprocess_exec", spawn)
    result = await asyncio.wait_for(service._extract_isolated(b"SYNTHETIC", "PDF"), 10)
    assert result == {"status": "EXTRACTION_FAILED", "reason_code": "WORKER_FAILED"}
    assert len(children) == 1 and children[0].returncode is not None


async def test_cancel_during_photo_spawn_waits_for_handle_and_reaps_late_child(monkeypatch):
    real_spawn = asyncio.create_subprocess_exec
    spawned, release = asyncio.Event(), asyncio.Event()
    children = []

    async def spawn(*args, **kwargs):
        child = await real_spawn(sys.executable, "-c", "import time;time.sleep(120)", **kwargs)
        children.append(child)
        spawned.set()
        await release.wait()
        return child

    monkeypatch.setattr(service.asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(service._extract_isolated(b"SYNTHETIC", "PDF"))
    try:
        await asyncio.wait_for(spawned.wait(), 10)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        assert children[0].returncode is not None
    finally:
        release.set()
        for child in children:
            if child.returncode is None:
                child.kill()
                await child.wait()
