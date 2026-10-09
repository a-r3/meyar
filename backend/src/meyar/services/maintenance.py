"""Bounded, tenant-explicit inspection and recovery for #46.

No filename-derived ownership, candidate payload logging, implicit retention
period, or deletion of a currently referenced asset. The caller commits DB work.
"""

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy import String, exists, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.models.agent_conversation import AgentConversation, AgentConversationSessionContext
from meyar.models.audit_event import AuditEvent
from meyar.models.auth_security_event import AuthSecurityEvent
from meyar.models.browser_session import BrowserSession
from meyar.models.candidate import Candidate
from meyar.models.candidate_document import CandidateDocument
from meyar.models.candidate_embedding_version import CandidateEmbeddingVersion
from meyar.models.candidate_identity_version import CandidateIdentityVersion
from meyar.models.candidate_photo_version import CandidatePhotoVersion
from meyar.models.candidate_profile_version import CandidateProfileVersion
from meyar.models.tenant_membership import TenantMembership
from meyar.services.storage_authority import storage_maintenance
from meyar.storage.local import LocalFilesystemStorage
from meyar.storage.photo import LocalPhotoStorage
from meyar.storage.staging import StagedObject

_HEX = re.compile(r"[0-9a-f]{32}")
_TEMP = re.compile(r"[0-9a-f]{32}\.[0-9a-f]{32}\.tmp")
_PROTECTED_AUDIT = ("DEMO_TENANT_BOOTSTRAPPED", "DEMO_HUMAN_BOOTSTRAPPED")


class MaintenancePolicy(BaseModel):
    limit: int = Field(default=100, ge=1, le=1000)
    scan_limit: int = Field(default=10000, ge=1, le=100000)
    scan_offset: int = Field(default=0, ge=0, le=1000000)
    seconds: float = Field(default=20, gt=0, le=60)
    orphan_age_seconds: int = Field(default=3600, ge=0, le=31536000)
    session_days: int | None = Field(default=None, ge=1, le=36500)
    conversation_days: int | None = Field(default=None, ge=1, le=36500)
    audit_days: int | None = Field(default=None, ge=1, le=36500)
    empty_candidate_days: int | None = Field(default=None, ge=1, le=36500)
    # Authentication failures are intentionally tenant-independent (#87).
    # This separately named global opt-in never infers tenancy from user names.
    global_auth_event_days: int | None = Field(default=None, ge=1, le=36500)
    # Longer than the maximum admitted two-attempt model gap. Inspection only:
    # retries use the existing processing commands, never fake failed versions.
    unfinished_age_seconds: int = Field(default=7200, ge=7200, le=31536000)


@dataclass
class MaintenanceResult:
    apply: bool
    counts: dict[str, int] = field(default_factory=dict)
    complete: bool = True
    unresolved: int = 0
    busy: bool = False
    next_scan_offset: int = 0

    def count(self, code: str) -> None:
        self.counts[code] = self.counts.get(code, 0) + 1

    @property
    def exit_code(self) -> int:
        return 2 if self.unresolved else 1 if self.busy or not self.complete else 0


def _digest(path: Path) -> str:
    # Legacy trash is bounded by the same ingestion upper safety floor.
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("Asset exceeds recovery byte budget")
    with path.open("rb") as handle:
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _entries(root: Path, tenant: uuid.UUID):
    """Only fixed application namespaces. Never recurse arbitrary storage trees."""
    for folder, kind in (
        (root / str(tenant), "document"),
        (root / "photo" / tenant.hex, "photo"),
        (root / ".trash" / tenant.hex, "trash"),
    ):
        if any(
            (root.joinpath(*folder.relative_to(root).parts[:i])).is_symlink()
            for i in range(1, len(folder.relative_to(root).parts) + 1)
        ):
            raise ValueError("Unsafe storage directory")
        if not folder.exists():
            continue
        with os.scandir(folder) as entries:
            for entry in entries:
                yield Path(entry.path), kind, entry.is_file(follow_symlinks=False)


async def _references(db, tenant: uuid.UUID, namespace: str, key: str) -> bool:
    model = CandidateDocument if namespace == "document" else CandidatePhotoVersion
    column = (
        CandidateDocument.storage_key
        if namespace == "document"
        else CandidatePhotoVersion.derived_storage_key
    )
    return bool(await db.scalar(select(exists().where(model.tenant_id == tenant, column == key))))


async def _legacy_trash(
    db,
    root: Path,
    path: Path,
    tenant: uuid.UUID,
    policy: MaintenancePolicy,
    result: MaintenanceResult,
) -> None:
    """Pre-journal trash uses DB-owned exact content hashes, never trash names.

    Restore identical content to every missing referenced key with that hash;
    no overwrite. More than the bounded match budget refuses this object.
    """
    digest = _digest(path)
    documents = (
        (
            await db.execute(
                select(CandidateDocument.storage_key)
                .where(
                    CandidateDocument.tenant_id == tenant,
                    CandidateDocument.sha256_hash == digest,
                )
                .limit(policy.limit + 1)
            )
        )
        .scalars()
        .all()
    )
    photos = (
        (
            await db.execute(
                select(CandidatePhotoVersion.derived_storage_key)
                .where(
                    CandidatePhotoVersion.tenant_id == tenant,
                    CandidatePhotoVersion.derived_sha256 == digest,
                )
                .limit(policy.limit + 1)
            )
        )
        .scalars()
        .all()
    )
    if len(documents) + len(photos) > policy.limit:
        raise ValueError("Legacy match budget exceeded")
    for kind, keys in (("document", documents), ("photo", photos)):
        storage = (
            LocalFilesystemStorage(str(root))
            if kind == "document"
            else LocalPhotoStorage(str(root))
        )
        for key in keys:
            if key is None:
                raise ValueError("Missing asset authority")
            target = (
                storage._owned_path(tenant, key)
                if isinstance(storage, LocalFilesystemStorage)
                else storage._path_for(key, tenant)
            )
            if target.is_symlink():
                raise ValueError("Unsafe target")
            if target.exists():
                if _digest(target) != digest:
                    raise ValueError("Conflicting referenced asset")
            elif result.apply:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.link(path, target)
            result.count("LEGACY_REFERENCE_RESTORED")
    result.count("LEGACY_TRASH_RECONCILED")
    if result.apply:
        path.unlink()


async def run_maintenance(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    storage_root: Path,
    policy: MaintenancePolicy,
    apply: bool = False,
    now: datetime | None = None,
) -> MaintenanceResult:
    """Shared CLI/service operator boundary. No app auth bypass or remote calls.

    The explicit tenant may have been deleted (crash during demo reset); storage
    reference authority remains tenant-scoped. An exclusive TRY advisory lock
    refuses while any live writer holds its shared transaction lock.
    """
    result = MaintenanceResult(apply)
    now = now or datetime.now(UTC)
    deadline = time.monotonic() + policy.seconds
    if not await storage_maintenance(db, tenant_id):
        result.busy = True
        return result
    await db.execute(
        text("SELECT set_config('statement_timeout', :milliseconds, true)"),
        {"milliseconds": str(max(int(policy.seconds * 1000), 1))},
    )
    processed = 0
    root = storage_root.resolve()
    try:
        if storage_root.is_symlink() or not root.is_dir():
            raise ValueError("Unsafe or absent storage root")
        for position, (path, kind, regular) in enumerate(_entries(root, tenant_id)):
            if time.monotonic() >= deadline:
                result.complete = False
                result.next_scan_offset = max(position, policy.scan_offset)
                break
            if position < policy.scan_offset:
                continue
            result.count("SCANNED")
            if result.counts["SCANNED"] > policy.scan_limit or time.monotonic() >= deadline:
                result.complete = False
                result.next_scan_offset = position
                break
            # Continue at this entry when the mutation budget, rather than the
            # enumeration budget, is exhausted. Restart at zero after EOF.
            result.next_scan_offset = position
            if not regular or path.is_symlink():
                result.unresolved += 1
                continue
            try:
                modified = path.stat().st_mtime
            except FileNotFoundError:
                continue  # a preceding journal action removed this directory entry
            if now.timestamp() - modified < policy.orphan_age_seconds:
                continue
            if kind == "trash":
                if path.suffix == ".json":
                    if not _HEX.fullmatch(path.stem) or path.stat().st_size > 4096:
                        result.unresolved += 1
                        continue
                    if processed >= policy.limit:
                        result.complete = False
                        break
                    try:
                        payload = json.loads(path.read_text())
                        ref = f".trash/{tenant_id.hex}/{path.stem}"
                        if (
                            set(payload)
                            != {"version", "namespace", "tenant_id", "storage_key", "trash_ref"}
                            or payload["version"] != 1
                            or payload["tenant_id"] != str(tenant_id)
                            or payload["trash_ref"] != ref
                            or payload["namespace"] not in ("document", "photo")
                        ):
                            raise ValueError("Invalid recovery journal")
                        staged = StagedObject(
                            payload["namespace"], tenant_id, payload["storage_key"], ref
                        )
                        storage = (
                            LocalFilesystemStorage(str(root))
                            if staged.namespace == "document"
                            else LocalPhotoStorage(str(root))
                        )
                        target = (
                            storage._owned_path(tenant_id, staged.storage_key)
                            if isinstance(storage, LocalFilesystemStorage)
                            else storage._path_for(staged.storage_key, tenant_id)
                        )
                        trash = root / ref
                        if target.is_symlink() or trash.is_symlink():
                            raise ValueError("Unsafe recovery asset")
                        referenced = await _references(
                            db, tenant_id, staged.namespace, staged.storage_key
                        )
                        if referenced and not target.exists() and not trash.exists():
                            raise ValueError("Referenced bytes unavailable")
                        code = "STAGED_RESTORED" if referenced else "STAGED_PURGED"
                        result.count(code)
                        if apply:
                            if referenced:
                                await storage.restore_staged(staged)
                            else:
                                await storage.purge_staged(staged)
                    except (OSError, ValueError, TypeError, KeyError):
                        result.unresolved += 1
                    processed += 1
                elif _HEX.fullmatch(path.name):
                    if path.with_suffix(".json").exists():
                        continue
                    if processed >= policy.limit:
                        result.complete = False
                        break
                    try:
                        await _legacy_trash(db, root, path, tenant_id, policy, result)
                    except (OSError, ValueError):
                        result.unresolved += 1
                    processed += 1
                else:
                    result.unresolved += 1
                continue
            if not (_HEX.fullmatch(path.name) or _TEMP.fullmatch(path.name)):
                result.unresolved += 1
                continue
            key = path.relative_to(root).as_posix()
            if await _references(db, tenant_id, kind, key):
                continue
            if processed >= policy.limit:
                result.complete = False
                break
            result.count("ORPHAN_REMOVED")
            if apply:
                path.unlink()
            processed += 1
        else:
            result.next_scan_offset = 0
    except (OSError, ValueError):
        result.unresolved += 1

    # Remaining budget is shared by every category. Rows use skip-locked rather
    # than waiting behind application authority. Policies are explicit opt-ins.
    async def retire(model, condition, code: str) -> None:
        nonlocal processed
        if time.monotonic() >= deadline:
            result.complete = False
            return
        budget = max(policy.limit - processed, 0)
        rows = (
            await db.scalars(
                select(model)
                .where(condition)
                .order_by(model.id)
                .limit(budget + 1)
                .with_for_update(skip_locked=True)
            )
        ).all()
        if len(rows) > budget:
            result.complete = False
        for row in rows[:budget]:
            result.count(code)
            if apply:
                await db.delete(row)
            processed += 1

    if policy.session_days is not None:
        cutoff = now - timedelta(days=policy.session_days)
        await retire(
            BrowserSession,
            (
                BrowserSession.tenant_membership_id.in_(
                    select(TenantMembership.id).where(TenantMembership.tenant_id == tenant_id)
                )
                & ((BrowserSession.expires_at < cutoff) | (BrowserSession.revoked_at < cutoff))
            ),
            "SESSION_RETIRED",
        )
    if policy.conversation_days is not None:
        cutoff = now - timedelta(days=policy.conversation_days)
        await retire(
            AgentConversation,
            (
                (AgentConversation.tenant_id == tenant_id)
                & (AgentConversation.updated_at < cutoff)
                & (
                    (AgentConversation.active_turn_id.is_(None))
                    | (AgentConversation.active_turn_expires_at < now)
                )
                & ~exists().where(
                    AgentConversationSessionContext.conversation_id == AgentConversation.id,
                    AgentConversationSessionContext.browser_session_id == BrowserSession.id,
                    BrowserSession.expires_at > now,
                    BrowserSession.revoked_at.is_(None),
                )
            ),
            "CONVERSATION_RETIRED",
        )
    if policy.audit_days is not None:
        await retire(
            AuditEvent,
            (AuditEvent.tenant_id == tenant_id)
            & (AuditEvent.created_at < now - timedelta(days=policy.audit_days))
            & AuditEvent.event_type.not_in(_PROTECTED_AUDIT),
            "AUDIT_RETIRED",
        )
    if policy.global_auth_event_days is not None:
        await retire(
            AuthSecurityEvent,
            AuthSecurityEvent.created_at < now - timedelta(days=policy.global_auth_event_days),
            "GLOBAL_AUTH_EVENT_RETIRED",
        )
    if policy.empty_candidate_days is not None:
        # No automatic ownership claim: explicit administrative age policy. Any
        # referencing table preserves the candidate, including failed folder rows.
        conditions = [
            (Candidate.tenant_id == tenant_id),
            (Candidate.created_at < now - timedelta(days=policy.empty_candidate_days)),
        ]
        for table in Candidate.metadata.tables.values():
            if table.name != "candidates" and "candidate_id" in table.c:
                conditions.append(~exists().where(table.c.candidate_id == Candidate.id))
        from sqlalchemy import and_

        await retire(Candidate, and_(*conditions), "EMPTY_CANDIDATE_RETIRED")
    for stage, model in (
        ("PROFILE", CandidateProfileVersion),
        ("IDENTITY", CandidateIdentityVersion),
        ("EMBEDDING", CandidateEmbeddingVersion),
    ):
        if time.monotonic() >= deadline:
            result.complete = False
            break
        event_type = (
            "CANDIDATE_EMBEDDING_STARTED"
            if stage == "EMBEDDING"
            else f"CANDIDATE_{stage}_EXTRACTION_STARTED"
        )
        columns = model.__table__.c
        if stage == "EMBEDDING":
            completed = exists().where(
                model.tenant_id == tenant_id,
                columns.candidate_profile_version_id.cast(String)
                == AuditEvent.event_metadata["profile_version_id"].as_string(),
            )
        else:
            completed = exists().where(
                model.tenant_id == tenant_id,
                model.candidate_id.cast(String)
                == AuditEvent.event_metadata["candidate_id"].as_string(),
                columns.candidate_document_id.cast(String)
                == AuditEvent.event_metadata["document_id"].as_string(),
                model.created_at >= AuditEvent.created_at,
            )
        unfinished = (
            await db.scalars(
                select(AuditEvent.id)
                .where(
                    AuditEvent.tenant_id == tenant_id,
                    AuditEvent.event_type == event_type,
                    AuditEvent.created_at < now - timedelta(seconds=policy.unfinished_age_seconds),
                    ~completed,
                )
                .order_by(AuditEvent.id)
                .limit(policy.limit + 1)
            )
        ).all()
        if len(unfinished) > policy.limit:
            result.complete = False
        for _ in unfinished[: policy.limit]:
            result.count(f"UNFINISHED_{stage}")
    if apply:
        await db.flush()
    return result
