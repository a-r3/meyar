"""Process-wide bounded local-inference admission (Slice 2 #31, issue #85).

A fresh ``OllamaLLMProvider`` instance is constructed per request (see
meyar.llm.dependency.get_llm_provider), so the bound must live outside any
one instance — otherwise every browser tab/session would get its own
independent budget and the setting would do nothing. ONE module-level
``InferenceAdmission`` is shared by every call through
``OllamaLLMProvider._chat`` (extraction, identity extraction, NL search
planning, and the agent loop alike).

Issue #85 replaced the former plain ``asyncio.Semaphore`` (unbounded
waiters, unbounded wait) with a bounded admission gate:

- at most ``max_active`` inference calls run at once (unchanged guarantee);
- at most ``max_queued`` callers may wait for a slot — one more is rejected
  immediately with ``InferenceQueueFullError``;
- a waiter that is not admitted within ``queue_timeout_seconds`` is
  rejected with ``InferenceQueueTimeoutError``;
- waiters are admitted strictly FIFO;
- a waiter that is cancelled (client gone, request task cancelled) or times
  out leaves the queue immediately, and a slot handed to a waiter that is
  simultaneously cancelled is given back — capacity can never leak.

Only this module's own counters are authority — never asyncio.Semaphore
internals. State is process-local by design (single-process deployment,
docs/DECISIONS.md D-089)."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum

# Safe single-process defaults (Settings mirror them, docs/DECISIONS.md
# D-089). With concurrency 1 and a slow local model, at most this many HR
# requests wait; anything beyond receives the truthful BUSY outcome.
DEFAULT_INFERENCE_QUEUE_MAX_WAITERS = 4
DEFAULT_INFERENCE_QUEUE_TIMEOUT_SECONDS = 30.0


class InferenceRejectionReason(StrEnum):
    QUEUE_FULL = "QUEUE_FULL"
    QUEUE_TIMEOUT = "QUEUE_TIMEOUT"


class InferenceAdmissionError(Exception):
    """The local-inference gate refused this call. Carries only a closed
    reason code — never queue contents, sizes, or caller identity."""

    reason: InferenceRejectionReason

    def __init__(self, reason: InferenceRejectionReason) -> None:
        self.reason = reason
        super().__init__(f"Local inference not admitted: {reason.value}.")


class InferenceQueueFullError(InferenceAdmissionError):
    def __init__(self) -> None:
        super().__init__(InferenceRejectionReason.QUEUE_FULL)


class InferenceQueueTimeoutError(InferenceAdmissionError):
    def __init__(self) -> None:
        super().__init__(InferenceRejectionReason.QUEUE_TIMEOUT)


@dataclass(frozen=True)
class InferenceAdmissionSnapshot:
    """Non-sensitive, process-local view used by readiness (issue #85)."""

    capacity: int
    queue_capacity: int
    active: int
    queued: int
    saturated: bool
    saturated_for_seconds: float


class InferenceAdmission:
    def __init__(
        self, *, max_active: int, max_queued: int, queue_timeout_seconds: float
    ) -> None:
        if max_active < 1:
            raise ValueError("inference_concurrency must be >= 1.")
        if max_queued < 0:
            raise ValueError("inference_queue_max_waiters must be >= 0.")
        if not queue_timeout_seconds > 0:
            raise ValueError("inference_queue_timeout_seconds must be > 0.")
        self._max_active = max_active
        self._max_queued = max_queued
        self._queue_timeout = queue_timeout_seconds
        self._active = 0
        self._waiters: deque[asyncio.Future[None]] = deque()
        self._saturated_since: float | None = None

    # -- typed read API -----------------------------------------------------

    @property
    def capacity(self) -> int:
        return self._max_active

    @property
    def queue_capacity(self) -> int:
        return self._max_queued

    @property
    def queue_timeout_seconds(self) -> float:
        return self._queue_timeout

    @property
    def active(self) -> int:
        return self._active

    @property
    def queued(self) -> int:
        return len(self._waiters)

    @property
    def saturated(self) -> bool:
        """Every slot busy AND the wait queue full: the next caller is
        rejected immediately."""
        return self._active >= self._max_active and len(self._waiters) >= self._max_queued

    def snapshot(self) -> InferenceAdmissionSnapshot:
        since = self._saturated_since
        return InferenceAdmissionSnapshot(
            capacity=self._max_active,
            queue_capacity=self._max_queued,
            active=self._active,
            queued=len(self._waiters),
            saturated=self.saturated,
            saturated_for_seconds=0.0 if since is None else time.monotonic() - since,
        )

    # -- admission ----------------------------------------------------------

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Hold one active inference slot for the body. Raises
        ``InferenceQueueFullError``/``InferenceQueueTimeoutError`` without
        ever entering the body; always releases on exit, including
        cancellation."""
        await self._acquire()
        try:
            yield
        finally:
            self._release()

    async def _acquire(self) -> None:
        if self._active < self._max_active and not self._waiters:
            self._active += 1
            self._track_saturation()
            return
        if len(self._waiters) >= self._max_queued:
            raise InferenceQueueFullError()
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        self._track_saturation()
        try:
            async with asyncio.timeout(self._queue_timeout):
                await waiter
        except BaseException as exc:
            if waiter.done() and not waiter.cancelled():
                # The slot was handed over just as we were cancelled/timed
                # out: we are leaving without using it, so give it back.
                self._release()
            else:
                waiter.cancel()
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass
                self._track_saturation()
            if isinstance(exc, TimeoutError):
                raise InferenceQueueTimeoutError() from None
            raise
        # Admitted: the releasing holder transferred its slot to us, so
        # ``_active`` already counts this call.

    def _release(self) -> None:
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)
                self._track_saturation()
                return
        self._active -= 1
        self._track_saturation()

    def _track_saturation(self) -> None:
        if self.saturated:
            if self._saturated_since is None:
                self._saturated_since = time.monotonic()
        else:
            self._saturated_since = None


_admission: InferenceAdmission | None = None


def get_inference_admission(
    *, max_active: int, max_queued: int, queue_timeout_seconds: float
) -> InferenceAdmission:
    """Lazily creates the shared gate at the first configured policy and
    reuses it thereafter. Resizing in-flight admission state is not safe, so
    a changed policy only takes effect after ``reset_inference_admission``
    (tests only) or a process restart."""
    global _admission
    if _admission is None:
        _admission = InferenceAdmission(
            max_active=max_active,
            max_queued=max_queued,
            queue_timeout_seconds=queue_timeout_seconds,
        )
    return _admission


def current_inference_admission() -> InferenceAdmission | None:
    """The live process-wide gate, or ``None`` before the first inference
    call (nothing admitted, nothing queued)."""
    return _admission


def reset_inference_admission() -> None:
    """Test-only: clears the process-wide singleton."""
    global _admission
    _admission = None
