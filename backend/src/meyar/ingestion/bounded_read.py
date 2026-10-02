from typing import Protocol

_READ_CHUNK_BYTES = 64 * 1024


class _AsyncReadable(Protocol):
    async def read(self, size: int = -1, /) -> bytes: ...


async def read_bounded(source: _AsyncReadable, *, max_bytes: int) -> bytes:
    """Read at most ``max_bytes + 1`` bytes from ``source``, in chunks;
    reading stops there however large the source is. One byte past the
    limit is enough for the document validator (the final size authority)
    to reject the document as too large. Memory use is O(``max_bytes``):
    the chunks are collected and then joined into the returned bytes, so
    this does not claim an exact peak-buffer size, only that the amount
    read and retained is bounded by ``max_bytes + 1``."""
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
