import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from meyar.models.audit_event import AuditEvent

# Slice 13 — behavioral privacy guard (docs/SECURITY_PRIVACY.md). Audit
# metadata may only carry ids/enums/counts/durations/hashes/version
# strings — never raw CV text, raw NL/search query text, PII, secrets,
# raw LLM output, embedding vectors, or filesystem/storage paths. Keys
# are matched exactly (not by substring) so legitimate ids like
# "api_key_id" or hashed values like "query_sha256" are never flagged.
_FORBIDDEN_METADATA_KEYS = frozenset(
    {
        "email",
        "phone",
        "full_name",
        "name",
        "raw_text",
        "raw_query",
        "query",
        "natural_language_request",
        "prompt",
        "content",
        "vector",
        "embedding",
        "embedding_vector",
        "storage_key",
        "path",
        "filesystem_path",
        "csrf_token",
        "session_token",
        "api_key",
        "api_key_secret",
        "secret",
        "password",
        "raw_output",
        "llm_output",
        "cv_text",
    }
)

# ids/hashes/version strings observed in existing metadata are well under
# this bound (UUIDs are 36 chars, sha256 hex digests are 64); a longer
# string value is treated as accidental free-text leakage.
_MAX_METADATA_STRING_LENGTH = 300


def _assert_metadata_is_privacy_safe(metadata: dict) -> None:
    for key, value in metadata.items():
        if key.lower() in _FORBIDDEN_METADATA_KEYS:
            raise ValueError(f"Audit metadata key {key!r} is not allowed (privacy guard).")
        if isinstance(value, str) and len(value) > _MAX_METADATA_STRING_LENGTH:
            raise ValueError(
                f"Audit metadata value for {key!r} exceeds "
                f"{_MAX_METADATA_STRING_LENGTH} chars — looks like leaked free text."
            )


async def record_event(
    db: AsyncSession, *, tenant_id: uuid.UUID, event_type: str, metadata: dict | None = None
) -> AuditEvent:
    if metadata:
        _assert_metadata_is_privacy_safe(metadata)
    event = AuditEvent(tenant_id=tenant_id, event_type=event_type, event_metadata=metadata or {})
    db.add(event)
    await db.flush()
    return event
