"""Bounded admission, shared by all parser instances in one application loop."""

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from weakref import WeakKeyDictionary

from meyar.ingestion import parser_policy as policy
from meyar.ingestion.parser import ParseError, ParseFailureCode


class AdmissionGate:
    def __init__(
        self,
        *,
        active: int = policy.MAX_ACTIVE,
        waiters: int = policy.MAX_WAITERS,
        seconds: float = policy.ADMISSION_SECONDS,
    ) -> None:
        self.capacity = active
        self.max_waiters = waiters
        self.seconds = seconds
        self.active = 0
        self.waiters: deque[asyncio.Future[None]] = deque()

    async def acquire(self) -> None:
        if self.active < self.capacity:
            self.active += 1
            return
        if len(self.waiters) >= self.max_waiters:
            raise ParseError(ParseFailureCode.PARSER_BUSY)
        future = asyncio.get_running_loop().create_future()
        self.waiters.append(future)
        try:
            await asyncio.wait_for(asyncio.shield(future), self.seconds)
        except BaseException as exc:
            if future in self.waiters:
                self.waiters.remove(future)
                future.cancel()
            else:
                # A slot was handed to us immediately before cancellation/timeout.
                self.release()
            if isinstance(exc, TimeoutError):
                raise ParseError(ParseFailureCode.PARSER_BUSY) from None
            raise

    def release(self) -> None:
        if self.waiters:
            self.waiters.popleft().set_result(None)
        else:
            self.active -= 1

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        await self.acquire()
        try:
            yield
        finally:
            self.release()


_parser_gates: WeakKeyDictionary[asyncio.AbstractEventLoop, AdmissionGate] = WeakKeyDictionary()
_validation_gates: WeakKeyDictionary[asyncio.AbstractEventLoop, AdmissionGate] = WeakKeyDictionary()


def gate(*, validation: bool = False) -> AdmissionGate:
    gates = _validation_gates if validation else _parser_gates
    loop = asyncio.get_running_loop()
    if loop not in gates:
        gates[loop] = AdmissionGate()
    return gates[loop]
