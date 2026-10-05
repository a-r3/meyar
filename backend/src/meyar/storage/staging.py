"""Reversible, constant-memory delete for local storage namespaces (issue #46 S4).

A staged delete uses a reversible same-filesystem hardlink/unlink sequence
into an opaque trash path; no bytes are read into memory. If source unlink
fails, the partial trash link is removed before the failure propagates. Failed
partial cleanup raises a closed staging error and leaves observable trash for
later reconciliation. A completed stage is either purged (the database deletion
became durable) or restored to its exact original key (the deletion did not).
Restore never overwrites: a different object at the original key fails closed.
"""

import json
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

TRASH_DIR = ".trash"
_CHUNK = 1024 * 1024
logger = logging.getLogger(__name__)


class StorageStagingError(OSError):
    """Partial staging cleanup failed; unresolved trash requires reconciliation."""

    def __init__(self) -> None:
        super().__init__("Storage staging cleanup unresolved")


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
    if not path.exists():
        return None
    trash_ref = f"{TRASH_DIR}/{tenant_id.hex}/{uuid.uuid4().hex}"
    trash_path = root / trash_ref
    trash_path.parent.mkdir(parents=True, exist_ok=True)
    # Publish durable server-owned key authority BEFORE removing the original.
    # Both the journal and directory are fsynced before destructive unlink.
    journal = trash_path.with_suffix(".json")
    payload = {"version": 1, "namespace": namespace, "tenant_id": str(tenant_id),
               "storage_key": storage_key, "trash_ref": trash_ref}
    with journal.open("x") as handle:
        os.chmod(journal, 0o600)
        json.dump(payload, handle)
        handle.flush()
        os.fsync(handle.fileno())
    directory = os.open(trash_path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    try:
        # link+unlink instead of rename: an (impossible-by-construction)
        # collision on the random trash name can never replace a staged object.
        os.link(path, trash_path)
    except FileNotFoundError:
        journal.unlink(missing_ok=True)
        if path.exists():
            raise  # missing trash parent is a structural failure, not absent source
        return None
    except BaseException:
        journal.unlink(missing_ok=True)
        raise
    try:
        # Persist the recovery link before unlinking the only original name.
        directory = os.open(trash_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        os.unlink(path)
    except BaseException:
        try:
            os.unlink(trash_path)
        except BaseException:
            logger.error(
                "component=storage_staging code=STORAGE_STAGE_CLEANUP_UNRESOLVED "
                "unresolved_count=1"
            )
            raise StorageStagingError() from None
        journal.unlink(missing_ok=True)
        raise
    return StagedObject(namespace, tenant_id, storage_key, trash_ref)


def purge_staged(root: Path, staged: StagedObject) -> None:
    (root / staged.trash_ref).unlink(missing_ok=True)
    (root / staged.trash_ref).with_suffix(".json").unlink(missing_ok=True)


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
        trash_path.with_suffix(".json").unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(trash_path, path)  # fails if the key is occupied; never overwrites
    except FileExistsError:
        if not _same_bytes(trash_path, path):
            raise StorageRestoreConflictError("Original key contains different bytes") from None
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    trash_path.unlink(missing_ok=True)
    trash_path.with_suffix(".json").unlink(missing_ok=True)
