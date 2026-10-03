"""Reversible, constant-memory delete for local storage namespaces (issue #46 S4).

A staged delete moves an object into an opaque trash path on the same
filesystem (an atomic rename — no bytes are read into memory). It is then
either purged (the database deletion became durable) or restored to its exact
original key (the database deletion did not). Restore never overwrites: an
existing different object at the original key fails closed.
"""

import os
import uuid
from dataclasses import dataclass
from pathlib import Path

TRASH_DIR = ".trash"
_CHUNK = 1024 * 1024


class StorageRestoreConflictError(OSError):
    """The original key is occupied by different bytes; nothing was overwritten."""


@dataclass(frozen=True)
class StagedObject:
    """Opaque handle for one staged delete. Carries no content and no host path."""

    namespace: str  # "document" | "photo"
    tenant_id: uuid.UUID
    storage_key: str
    trash_ref: str  # relative to the storage root


def stage_file(root: Path, path: Path, *, namespace: str, tenant_id: uuid.UUID,
               storage_key: str) -> StagedObject | None:
    """Move an existing object to trash. None means it was already absent."""
    trash_ref = f"{TRASH_DIR}/{tenant_id.hex}/{uuid.uuid4().hex}"
    trash_path = root / trash_ref
    trash_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # link+unlink instead of rename: an (impossible-by-construction)
        # collision on the random trash name can never replace a staged object.
        os.link(path, trash_path)
    except FileNotFoundError:
        return None
    os.unlink(path)
    return StagedObject(namespace, tenant_id, storage_key, trash_ref)


def purge_staged(root: Path, staged: StagedObject) -> None:
    (root / staged.trash_ref).unlink(missing_ok=True)


def _same_bytes(first: Path, second: Path) -> bool:
    if first.stat().st_size != second.stat().st_size:
        return False
    with first.open("rb") as left, second.open("rb") as right:
        while True:
            a, b = left.read(_CHUNK), right.read(_CHUNK)
            if a != b:
                return False
            if not a:
                return True


def restore_staged(root: Path, path: Path, staged: StagedObject) -> None:
    """Return the object to its exact original key without overwriting anything."""
    trash_path = root / staged.trash_ref
    if not trash_path.exists():
        # Already restored/purged by an earlier compensation attempt: idempotent.
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(trash_path, path)  # fails if the key is occupied; never overwrites
    except FileExistsError:
        if not _same_bytes(trash_path, path):
            raise StorageRestoreConflictError("Original key contains different bytes") from None
    trash_path.unlink(missing_ok=True)
