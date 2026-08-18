import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from meyar.models.audit_event import AuditEvent


async def record_event(
    db: AsyncSession, *, tenant_id: uuid.UUID, event_type: str, metadata: dict | None = None
) -> AuditEvent:
    event = AuditEvent(tenant_id=tenant_id, event_type=event_type, event_metadata=metadata or {})
    db.add(event)
    await db.flush()
    return event
