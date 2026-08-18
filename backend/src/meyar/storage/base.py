import uuid
from typing import Protocol


class DocumentStorage(Protocol):
    """Storage boundary for raw candidate-document bytes. Swappable —
    domain/API code must depend only on this Protocol, never on a concrete
    backend, so local disk can later be replaced by encrypted object
    storage without touching callers. See docs/SECURITY_PRIVACY.md."""

    async def save(self, *, tenant_id: uuid.UUID, content: bytes) -> str:
        """Persist content and return an opaque storage_key. Never derives
        the key from user-controlled input (e.g. a filename)."""
        ...

    async def read(self, *, storage_key: str) -> bytes: ...

    async def delete(self, *, storage_key: str) -> None:
        """Must not raise if the key is already gone (idempotent)."""
        ...
