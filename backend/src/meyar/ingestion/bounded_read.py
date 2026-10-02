from typing import Protocol

_READ_CHUNK_BYTES = 64 * 1024


class _AsyncReadable(Protocol):
    async def read(self, size: int = -1, /) -> bytes: ...


async def read_bounded(source: _AsyncReadable, *, max_bytes: int) -> bytes:
    """Read at most ``max_bytes + 1`` bytes in chunks, never the whole
    stream. One byte past the limit is enough for the document validator
    (the final size authority) to reject it as too large, without the
    process ever holding more than ``max_bytes + 1`` bytes of it."""
    limit = max_bytes + 1
    parts: list[bytes] = []
    total = 0
    while total < limit:
        chunk = await source.read(min(_READ_CHUNK_BYTES, limit - total))
        if not chunk:
            break
        parts.append(chunk)
        total += len(chunk)
    return b"".join(parts)
