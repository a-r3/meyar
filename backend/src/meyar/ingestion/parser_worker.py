"""Disposable parser process: bytes on stdin, bounded closed JSON on stdout.

No inherited credentials, filenames, storage handles, database or tenant objects.
Resource setup precedes importing document libraries and reading document bytes.
"""

import errno
import json
import logging
import mmap
import sys

from meyar.ingestion.parser_policy import WORKER_MEMORY_BYTES, OutputLimits

RESOURCE_UNAVAILABLE_EXIT = 72
RESOURCE_LIMIT_EXIT = 73


def establish_memory_limit(limit: int = WORKER_MEMORY_BYTES) -> None:
    """Require both an installed RLIMIT_AS and observable allocation refusal.

    Linux and Darwin use the same feature-detected primitive. In particular,
    setrlimit success alone is insufficient on older/unsupported Darwin kernels.
    The probe reserves address space without touching/committing its pages.
    """
    import resource

    if not hasattr(resource, "RLIMIT_AS"):
        raise RuntimeError("resource unavailable")
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    if resource.getrlimit(resource.RLIMIT_AS) != (limit, limit):
        raise RuntimeError("resource unavailable")
    try:
        probe = mmap.mmap(-1, limit + mmap.PAGESIZE, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
    except OSError as exc:
        if exc.errno != errno.ENOMEM:
            raise RuntimeError("resource unavailable") from None
    else:
        probe.close()
        raise RuntimeError("resource unavailable")


def encode_bounded(result: dict, cap: int) -> bytes:
    # No arbitrarily large serialized response is accumulated even in the worker.
    output = bytearray()
    for chunk in json.JSONEncoder(ensure_ascii=False, separators=(",", ":")).iterencode(result):
        encoded = chunk.encode("utf-8")
        if len(output) + len(encoded) > cap:
            from meyar.ingestion.parser import ParseError, ParseFailureCode

            raise ParseError(ParseFailureCode.PARSER_OUTPUT_LIMIT)
        output.extend(encoded)
    return bytes(output)


def main() -> None:
    try:
        establish_memory_limit()
    except (ImportError, AttributeError, OSError, ValueError, RuntimeError):
        sys.exit(RESOURCE_UNAVAILABLE_EXIT)
    # No raw library diagnostics (including pypdf warnings) leave this process.
    logging.disable(logging.CRITICAL)
    from meyar.ingestion.parser import ParseError, ParseFailureCode

    reserve = bytearray(64 * 1024)
    try:
        from meyar.ingestion.parsers.local_text_sync import parse_sync

        kind = sys.argv[1]
        limits = OutputLimits(**json.loads(sys.argv[2])) if len(sys.argv) > 2 else OutputLimits()
        input_cap = int(sys.argv[3])
        data = sys.stdin.buffer.read(input_cap + 1)
        if len(data) > input_cap:
            raise ParseError(ParseFailureCode.PARSER_OUTPUT_LIMIT)
        result = parse_sync(data, kind, limits)
        output = encode_bounded(result.model_dump(mode="json"), limits.result_bytes)
    except MemoryError:
        del reserve
        sys.exit(RESOURCE_LIMIT_EXIT)
    except OSError as exc:
        if exc.errno == errno.ENOMEM:
            sys.exit(RESOURCE_LIMIT_EXIT)
        output = encode_bounded({"error": ParseFailureCode.INVALID_DOCUMENT}, 1024)
    except ParseError as exc:
        output = encode_bounded({"error": exc.code}, 1024)
    except Exception:
        output = encode_bounded({"error": ParseFailureCode.INVALID_DOCUMENT}, 1024)
    sys.stdout.buffer.write(output)


if __name__ == "__main__":
    main()
