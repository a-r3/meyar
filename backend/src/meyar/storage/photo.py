"""Opaque, photo-only derived asset namespace under the backed-up storage root."""

import os
import uuid
from pathlib import Path

from meyar.storage.staging import StagedObject, purge_staged, restore_staged, stage_file


class LocalPhotoStorage:
    def __init__(self, root: str) -> None:
        self._root = Path(root)

    def _path_for(self, storage_key: str, tenant_id: uuid.UUID) -> Path:
        pieces = storage_key.split("/")
        if (
            len(pieces) != 3
            or pieces[0] != "photo"
            or pieces[1] != tenant_id.hex
            or len(pieces[2]) != 32
        ):
            raise ValueError("Invalid derived photo key")
        try:
            uuid.UUID(hex=pieces[1])
            uuid.UUID(hex=pieces[2])
        except ValueError as exc:
            raise ValueError("Invalid derived photo key") from exc
        return self._root / storage_key

    async def save(self, *, tenant_id: uuid.UUID, content: bytes) -> str:
        key = f"photo/{tenant_id.hex}/{uuid.uuid4().hex}"
        path = self._path_for(key, tenant_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(content)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return key

    async def read(self, *, tenant_id: uuid.UUID, storage_key: str) -> bytes:
        return self._path_for(storage_key, tenant_id).read_bytes()

    async def delete(self, *, tenant_id: uuid.UUID, storage_key: str) -> None:
        self._path_for(storage_key, tenant_id).unlink(missing_ok=True)

    async def delete_owned(self, *, tenant_id: uuid.UUID, storage_key: str) -> None:
        # Compensation protocol shared with DocumentStorage (storage_recovery).
        await self.delete(tenant_id=tenant_id, storage_key=storage_key)

    async def stage_delete(
        self, *, tenant_id: uuid.UUID, storage_key: str
    ) -> StagedObject | None:
        path = self._path_for(storage_key, tenant_id)
        return stage_file(
            self._root, path, namespace="photo", tenant_id=tenant_id, storage_key=storage_key
        )

    async def purge_staged(self, staged: StagedObject) -> None:
        self._path_for(staged.storage_key, staged.tenant_id)  # tenant/shape validation
        purge_staged(self._root, staged)

    async def restore_staged(self, staged: StagedObject) -> None:
        path = self._path_for(staged.storage_key, staged.tenant_id)
        restore_staged(self._root, path, staged)
