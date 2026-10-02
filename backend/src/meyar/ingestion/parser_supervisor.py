"""Async, bounded IPC and lifecycle supervisor for untrusted parsing."""

import asyncio
import json
import sys
from dataclasses import asdict

from pydantic import ConfigDict, ValidationError

from meyar.ingestion import parser_policy as policy
from meyar.ingestion.admission import gate
from meyar.ingestion.parser import (
    CanonicalBlock,
    CanonicalDocumentContent,
    CanonicalPage,
    ParseError,
    ParseFailureCode,
    ParseResult,
)
from meyar.ingestion.parser_output import validate_result
from meyar.ingestion.parser_worker import RESOURCE_LIMIT_EXIT, RESOURCE_UNAVAILABLE_EXIT


class _Block(CanonicalBlock):
    model_config = ConfigDict(extra="forbid")


class _Page(CanonicalPage):
    model_config = ConfigDict(extra="forbid")
    blocks: list[_Block]  # type: ignore[assignment]


class _Content(CanonicalDocumentContent):
    model_config = ConfigDict(extra="forbid")
    pages: list[_Page]  # type: ignore[assignment]


class _Result(ParseResult):
    model_config = ConfigDict(extra="forbid")
    content: _Content


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def decode_result(output: bytes, kind: str, limits: policy.OutputLimits) -> ParseResult:
    if len(output) > limits.result_bytes:
        raise ParseError(ParseFailureCode.PARSER_OUTPUT_LIMIT)
    try:
        payload = json.loads(output, object_pairs_hook=_unique_object)
        if isinstance(payload, dict) and set(payload) == {"error"}:
            code = ParseFailureCode(payload["error"])
            raise ParseError(code)
        # Bound model construction too, before Pydantic materializes nested models.
        if isinstance(payload, dict) and isinstance(payload.get("content"), dict):
            pages = payload["content"].get("pages")
            if isinstance(pages, list):
                if len(pages) > limits.pages:
                    raise ParseError(ParseFailureCode.PARSER_OUTPUT_LIMIT)
                count = 0
                for page in pages:
                    if isinstance(page, dict) and isinstance(page.get("blocks"), list):
                        count += len(page["blocks"])
                        if count > limits.blocks:
                            raise ParseError(ParseFailureCode.PARSER_OUTPUT_LIMIT)
        result = _Result.model_validate(payload, strict=True)
        validate_result(result, kind, limits)
        return result
    except (ValueError, TypeError, RecursionError, ValidationError):
        raise ParseError(ParseFailureCode.INVALID_PARSER_OUTPUT) from None


async def _read_bounded(stream: asyncio.StreamReader, cap: int) -> bytes:
    output = bytearray()
    while chunk := await stream.read(min(64 * 1024, cap + 1 - len(output))):
        output.extend(chunk)
        if len(output) > cap:
            raise ParseError(ParseFailureCode.PARSER_OUTPUT_LIMIT)
    return bytes(output)


async def _write_input(process: asyncio.subprocess.Process, data: bytes) -> None:
    assert process.stdin is not None
    try:
        for offset in range(0, len(data), 64 * 1024):
            process.stdin.write(data[offset : offset + 64 * 1024])
            await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass  # early worker exit is classified from its exit code
    finally:
        process.stdin.close()


async def _reap(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), policy.CLEANUP_SECONDS)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
    await process.wait()


async def _finish(task: asyncio.Task) -> None:
    # Repeat cancellation must not interrupt cleanup and strand a child/thread.
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            continue
    task.result()
    if cancelled:
        raise asyncio.CancelledError


async def _run(data: bytes, kind: str, limits: policy.OutputLimits, seconds: float) -> bytes:
    """Deadline for normally resolving startup and input-controlled worker work.

    Cleanup intentionally awaits shielded OS spawn resolution to obtain/reap
    any late-created child. An indefinitely stuck OS spawn primitive can delay
    timeout delivery indefinitely; this is not a strict total wall-clock bound.
    Terminate/kill/reap cleanup is outside the worker execution deadline.
    """
    spawn = asyncio.create_task(
        asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "meyar.ingestion.parser_worker",
            kind,
            json.dumps(asdict(limits)),
            str(len(data)),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={"LANG": "C.UTF-8"},  # never inherit credentials/configuration
            limit=64 * 1024,
        )
    )
    process = None
    tasks: list[asyncio.Task] = []
    try:
        async with asyncio.timeout(seconds):
            try:
                process = await asyncio.shield(spawn)
            except Exception:
                raise ParseError(ParseFailureCode.PARSER_STARTUP_FAILED) from None
            assert process.stdout is not None
            tasks = [
                asyncio.create_task(_read_bounded(process.stdout, limits.result_bytes)),
                asyncio.create_task(_write_input(process, data)),
                asyncio.create_task(process.wait()),
            ]
            results = await asyncio.gather(*tasks)
            if process.returncode == RESOURCE_UNAVAILABLE_EXIT:
                raise ParseError(ParseFailureCode.PARSER_RESOURCE_UNAVAILABLE)
            if process.returncode == RESOURCE_LIMIT_EXIT:
                raise ParseError(ParseFailureCode.PARSER_RESOURCE_LIMIT)
            if process.returncode != 0:
                raise ParseError(ParseFailureCode.PARSER_WORKER_FAILED)
            return results[0]
    except TimeoutError:
        raise ParseError(ParseFailureCode.PARSER_TIMEOUT) from None
    except ParseError:
        raise
    except Exception:
        raise ParseError(ParseFailureCode.PARSER_WORKER_FAILED) from None
    finally:

        async def cleanup() -> None:
            nonlocal process
            if process is None:
                try:
                    process = await spawn
                except Exception:
                    return
            for task in tasks:
                if not task.done():
                    task.cancel()

            # Drain stdout during reap: an overproducing child must never stall
            # asyncio's pipe transport / wait() at its StreamReader high-water mark.
            async def discard() -> None:
                assert process is not None and process.stdout is not None
                while await process.stdout.read(64 * 1024):
                    pass

            await asyncio.gather(*tasks, return_exceptions=True)
            drain = asyncio.create_task(discard())
            try:
                await _reap(process)
            finally:
                await drain

        await _finish(asyncio.create_task(cleanup()))


async def parse_isolated(
    *,
    data: bytes,
    document_type: str,
    limits: policy.OutputLimits | None = None,
    seconds: float = policy.WORKER_SECONDS,
) -> ParseResult:
    if document_type not in ("PDF", "DOCX"):
        raise ParseError(ParseFailureCode.INVALID_DOCUMENT)
    limits = limits or policy.OutputLimits()
    async with gate().slot():
        output = await _run(data, document_type, limits, seconds)
    return decode_result(output, document_type, limits)
