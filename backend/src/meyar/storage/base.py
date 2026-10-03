import uuid
from typing import Protocol

from meyar.storage.staging import StagedObject


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

    async def delete_owned(self, *, tenant_id: uuid.UUID, storage_key: str) -> None:
        """Idempotent delete that refuses (ValueError) any key outside this
        tenant's namespace. Used to compensate a save that never became
        database authority."""
        ...

    async def stage_delete(
        self, *, tenant_id: uuid.UUID, storage_key: str
    ) -> StagedObject | None:
        """Reversibly remove an object (constant memory). None if already absent."""
        ...

    async def purge_staged(self, staged: StagedObject) -> None:
        """Make a staged delete permanent. Idempotent."""
        ...

    async def restore_staged(self, staged: StagedObject) -> None:
        """Restore a staged object to its exact key. Idempotent; fails closed
        (OSError) rather than overwrite different bytes."""
        ...
