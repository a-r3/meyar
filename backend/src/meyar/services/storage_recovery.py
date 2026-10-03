"""Bounded compensation between PostgreSQL authority and the storage filesystem
(issue #46 S4).

Filesystem and database cannot be one atomic transaction. Instead every
filesystem mutation that is tied to a caller-owned DB transaction is recorded
in a per-session ledger:

* created originals (``storage.save``) are removed again if the DB side did
  not become durable;
* deletes are *staged* (reversible, constant memory) and then purged only after
  the DB deletion is durable, or restored to the exact key if it is not.

The database is always the arbiter: before any compensating action the ledger
re-checks, in a fresh transaction, whether a durable row still references the
key. A COMMIT whose outcome is ambiguous therefore never deletes a referenced
original and never leaves a deleted candidate's bytes behind.
If rollback fails before its event, PENDING effects become UNKNOWN. The failed
Session connections must be invalidated before a fresh transaction can resolve
them; failed reset or verification retains the ledger and fails closed.

Residual window (documented in docs/DECISIONS.md D-S4): a hard process kill
between a filesystem mutation and its compensation cannot be recovered here
(no durable journal by design). Created-but-unreferenced originals and
staged-but-unpurged ``.trash`` objects are then left for the later #46
orphan-reconciliation slice. Failures of compensation itself raise
``StorageCompensationError`` and log only closed structural fields.
"""

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from meyar.services.candidate_document_repo import document_storage_key_referenced
from meyar.services.candidate_photo_repo import photo_storage_key_referenced
from meyar.storage.staging import StagedObject, StorageStagingError

logger = logging.getLogger(__name__)
_LEDGER_KEY = "meyar.storage_recovery"


class StorageCompensationError(Exception):
    """Compensation failed; primary retains the original without emitting its payload.

    The message is a closed constant — never a key, path or exception payload.
    """

    def __init__(self, unresolved: int) -> None:
        super().__init__("Storage compensation incomplete.")
        self.unresolved = unresolved
        self.primary: BaseException | None = None


class _PrimaryStorageFailure(RuntimeError):
    """Closed traceback cause for a primary exception with no safe message contract."""

    def __init__(self) -> None:
        super().__init__("Primary storage operation failed.")


def _raise_compensation(error: StorageCompensationError, primary: BaseException | None) -> NoReturn:
    error.primary = primary
    # The staging error has a fixed, zero-argument message and suppresses raw
    # cleanup context. Arbitrary primary exceptions cannot safely be formatted.
    cause = primary if primary is None or type(primary) is StorageStagingError else (
        _PrimaryStorageFailure()
    )
    raise error from cause


class _State(StrEnum):
    PENDING = "PENDING"
    COMMITTED = "COMMITTED"
    ROLLED_BACK = "ROLLED_BACK"
    UNKNOWN = "UNKNOWN"  # outcome must be established by fresh DB authority


@dataclass
class _Created:
    storage: Any
    tenant_id: uuid.UUID
    storage_key: str
    state: _State = _State.PENDING
    namespace: str = "document"


@dataclass
class _Staged:
    storage: Any
    staged: StagedObject
    state: _State = _State.PENDING


class _Ledger:
    def __init__(self) -> None:
        self.created: list[_Created] = []
        self.staged: list[_Staged] = []
        self.needs_invalidation = False

    def mark(self, state: _State) -> None:
        if state is _State.COMMITTED:
            # Durable authority now owns every created original: nothing to undo.
            self.created = [c for c in self.created if c.state is not _State.PENDING]
        entries: list[_Created | _Staged] = [*self.created, *self.staged]
        for entry in entries:
            if entry.state is _State.PENDING:
                entry.state = state


def _ledger(db: AsyncSession) -> _Ledger:
    ledger = db.sync_session.info.get(_LEDGER_KEY)
    if ledger is None:
        ledger = db.sync_session.info[_LEDGER_KEY] = _Ledger()
    return ledger


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    ledger = session.info.get(_LEDGER_KEY)
    # after_commit also fires when a SAVEPOINT is released; only the outermost
    # commit (not a nested one) makes DB authority durable.
    if ledger is not None and not session.in_nested_transaction():
        ledger.mark(_State.COMMITTED)


@event.listens_for(Session, "after_rollback")
def _after_rollback(session: Session) -> None:
    ledger = session.info.get(_LEDGER_KEY)
    # A SAVEPOINT rollback (persist's own failure path) must not mark the
    # still-live outer transaction's other originals as rolled back.
    if ledger is not None and not session.in_nested_transaction():
        ledger.mark(_State.ROLLED_BACK)


def track_created(
    db: AsyncSession,
    storage: Any,
    *,
    tenant_id: uuid.UUID,
    storage_key: str,
    namespace: str = "document",
) -> _Created:
    entry = _Created(storage, tenant_id, storage_key, namespace=namespace)
    _ledger(db).created.append(entry)
    return entry


def track_staged(db: AsyncSession, storage: Any, staged: StagedObject | None) -> None:
    if staged is not None:
        _ledger(db).staged.append(_Staged(storage, staged))


async def _referenced(db: AsyncSession, namespace: str, tenant_id: uuid.UUID, key: str) -> bool:
    if namespace == "photo":
        return await photo_storage_key_referenced(db, tenant_id=tenant_id, storage_key=key)
    return await document_storage_key_referenced(db, tenant_id=tenant_id, storage_key=key)


async def _resolve_created(db: AsyncSession, entry: _Created) -> None:
    if await _referenced(db, entry.namespace, entry.tenant_id, entry.storage_key):
        return  # the commit did become durable: the asset is legitimate authority
    await entry.storage.delete_owned(tenant_id=entry.tenant_id, storage_key=entry.storage_key)


async def _resolve_staged_after_rollback(db: AsyncSession, entry: _Staged) -> None:
    staged = entry.staged
    if await _referenced(db, staged.namespace, staged.tenant_id, staged.storage_key):
        await entry.storage.restore_staged(staged)
    else:
        await entry.storage.purge_staged(staged)  # deletion proved durable


def _log_unresolved(code: str, count: int, exc: BaseException) -> None:
    name = type(exc).__name__
    logger.error(
        "component=storage_recovery code=%s unresolved_count=%d error_type=%s",
        code, count, name if name.isidentifier() and len(name) <= 64 else "Exception",
    )


async def settle(db: AsyncSession, *, raise_on_unresolved: bool) -> None:
    """Resolve every non-pending ledger entry against current DB truth."""
    ledger = _ledger(db)
    if ledger.needs_invalidation:
        # Never query an uncertain writer transaction, including on a lazy retry.
        error = StorageCompensationError(len(ledger.created) + len(ledger.staged))
        _log_unresolved("STORAGE_COMPENSATION_UNRESOLVED", error.unresolved, error)
        raise error
    failures: list[BaseException] = []
    remaining_created: list[_Created] = []
    for created in ledger.created:
        if created.state is _State.PENDING:
            remaining_created.append(created)
        elif created.state in (_State.ROLLED_BACK, _State.UNKNOWN):
            try:
                await _resolve_created(db, created)
            except Exception as exc:
                failures.append(exc)
                remaining_created.append(created)  # stays observable, retryable
        # COMMITTED: durable authority, nothing to do
    ledger.created = remaining_created

    remaining_staged: list[_Staged] = []
    purge_failures: list[BaseException] = []
    for staged in ledger.staged:
        if staged.state is _State.PENDING:
            remaining_staged.append(staged)
        elif staged.state is _State.COMMITTED:
            try:
                await staged.storage.purge_staged(staged.staged)
            except Exception as exc:
                purge_failures.append(exc)
                remaining_staged.append(staged)
        else:
            try:
                await _resolve_staged_after_rollback(db, staged)
            except Exception as exc:
                failures.append(exc)
                remaining_staged.append(staged)
    ledger.staged = remaining_staged

    if purge_failures:
        # The DB deletion is already durable: the operation succeeded, but
        # un-purged bytes remain under .trash for operator reconciliation.
        _log_unresolved("STORAGE_PURGE_UNRESOLVED", len(purge_failures), purge_failures[0])
    if failures:
        _log_unresolved("STORAGE_COMPENSATION_UNRESOLVED", len(failures), failures[0])
        if raise_on_unresolved:
            raise StorageCompensationError(len(failures))


async def settle_leftovers(db: AsyncSession) -> None:
    """Best-effort pass for a caller that rolled back without this module."""
    ledger = db.sync_session.info.get(_LEDGER_KEY)
    if ledger is not None and (ledger.needs_invalidation or any(
        e.state is not _State.PENDING for e in [*ledger.created, *ledger.staged]
    )):
        await settle(db, raise_on_unresolved=False)


async def _quiet_rollback(db: AsyncSession) -> None:
    try:
        await db.rollback()
    except Exception as exc:
        ledger = _ledger(db)
        ledger.needs_invalidation = True
        _log_unresolved(
            "DB_ROLLBACK_FAILED_DURING_COMPENSATION",
            len(ledger.created) + len(ledger.staged), exc,
        )


async def rollback_with_recovery(db: AsyncSession, primary: BaseException | None = None) -> None:
    """End the writer transaction, then resolve its effects against fresh DB truth."""
    await _quiet_rollback(db)
    ledger = _ledger(db)
    # An absent rollback event is not proof of rollback or commit. Do not skip
    # these effects: UNKNOWN means query authority, never assume an outcome.
    ledger.mark(_State.UNKNOWN)
    try:
        if ledger.needs_invalidation or db.in_transaction():
            ledger.needs_invalidation = True
            try:
                # Supported SQLAlchemy reset: discard unsafe connections and
                # expunge ORM state. Session.info (the ledger) is retained.
                await db.invalidate()
                if db.in_transaction():
                    raise StorageCompensationError(len(ledger.created) + len(ledger.staged))
            except Exception as exc:
                count = len(ledger.created) + len(ledger.staged)
                _log_unresolved("STORAGE_COMPENSATION_UNRESOLVED", count, exc)
                raise StorageCompensationError(count) from None
            ledger.needs_invalidation = False
        await settle(db, raise_on_unresolved=True)
    except StorageCompensationError as exc:
        _raise_compensation(exc, primary)
    finally:
        await _quiet_rollback(db)  # release the verification read transaction


async def commit_with_recovery(db: AsyncSession) -> None:
    """Commit; on failure roll back and compensate, then re-raise the failure.

    After success, staged deletes become permanent.
    """
    try:
        await db.commit()
    except BaseException as exc:
        await rollback_with_recovery(db, exc)
        raise
    await settle(db, raise_on_unresolved=False)


@asynccontextmanager
async def recover_on_failure(db: AsyncSession) -> AsyncIterator[None]:
    """Caller-owned transaction scope: any exception rolls back and compensates."""
    try:
        yield
    except BaseException as exc:
        await rollback_with_recovery(db, exc)
        raise


async def abandon_created(db: AsyncSession, entry: _Created, primary: BaseException) -> None:
    """Compensate one save whose DB work was rolled back to a savepoint."""
    entry.state = _State.ROLLED_BACK
    try:
        await settle(db, raise_on_unresolved=True)
    except StorageCompensationError as exc:
        _raise_compensation(exc, primary)
