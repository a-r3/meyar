"""Slice 7 — CandidateIdentity extraction. Uses synthetic content only
(fixtures/synthetic_cvs/ text plus hand-seeded canonical blocks for
email/phone, since no existing fixture contains contact info) — never a
real person. See docs/MASTER_SPEC.md §5 and .claude/rules/testing.md."""

import uuid

import pytest
from fakes import FakeLLMProvider
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.extraction.identity_service import (
    IdentityExtractionPreconditionError,
    extract_candidate_identity,
)
from meyar.llm.provider import ModelTimeoutError, ModelUnavailableError
from meyar.schemas.candidate_identity import CandidateIdentityExtraction, IdentityFieldItem
from meyar.schemas.candidate_profile import EvidenceRef
from meyar.services.candidate_document_repo import (
    create_candidate_document,
    create_canonical_document,
)
from meyar.services.candidate_identity_repo import get_current_identity_version
from meyar.services.candidate_repo import create_candidate
from meyar.services.tenant_repo import create_tenant


async def _seed_candidate_with_identity_content(db_session: AsyncSession, tenant_id: uuid.UUID):
    """Seeds a Candidate + CandidateDocument + CanonicalDocument whose
    content includes a name, email, and phone block — the direct-upload
    pipeline's existing synthetic fixtures don't contain contact info
    (the professional-extraction fixtures deliberately don't need any),
    so identity extraction is exercised against hand-seeded canonical
    content instead of a new binary fixture."""
    candidate = await create_candidate(db_session, tenant_id=tenant_id)
    document = await create_candidate_document(
        db_session,
        tenant_id=tenant_id,
        candidate_id=candidate.id,
        original_filename="synthetic.pdf",
        mime_type="application/pdf",
        byte_size=100,
        sha256_hash="a" * 64,
        storage_key=f"test/{uuid.uuid4().hex}",
    )
    canonical = await create_canonical_document(
        db_session,
        tenant_id=tenant_id,
        candidate_document_id=document.id,
        parser_name="test-parser",
        parser_version="1.0.0",
        language=None,
        content={
            "pages": [
                {
                    "page": 1,
                    "blocks": [
                        {
                            "index": 0,
                            "text": (
                                "SYNTHETIC TEST DATA - NOT A REAL PERSON\n"
                                "Jane Synthetic Doe\n"
                                "Email: jane.synthetic@example.com\n"
                                "Phone: +1-555-0100"
                            ),
                        }
                    ],
                }
            ]
        },
    )
    await db_session.flush()
    return candidate, document, canonical


@pytest.fixture
async def candidate_with_contact_content(db_session: AsyncSession):
    tenant = await create_tenant(db_session, name=f"IdTenant-{uuid.uuid4().hex[:8]}")
    await db_session.commit()
    candidate, document, canonical = await _seed_candidate_with_identity_content(
        db_session, tenant.id
    )
    await db_session.commit()
    return tenant, candidate, document, canonical


def _valid_identity_extraction() -> CandidateIdentityExtraction:
    return CandidateIdentityExtraction(
        full_name=IdentityFieldItem(
            value="Jane Synthetic Doe",
            evidence=[EvidenceRef(page=1, block_index=0, quote="Jane Synthetic Doe")],
        ),
        email=IdentityFieldItem(
            value="jane.synthetic@example.com",
            evidence=[
                EvidenceRef(
                    page=1, block_index=0, quote="Email: jane.synthetic@example.com"
                )
            ],
        ),
        phone=IdentityFieldItem(
            value="+1-555-0100",
            evidence=[EvidenceRef(page=1, block_index=0, quote="Phone: +1-555-0100")],
        ),
    )


async def test_full_name_email_phone_extraction_creates_completed_version(
    db_session: AsyncSession, candidate_with_contact_content
) -> None:
    tenant, candidate, document, _canonical = candidate_with_contact_content
    llm = FakeLLMProvider(identity_extraction=_valid_identity_extraction())

    version = await extract_candidate_identity(
        db_session,
        llm,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        candidate_document=document,
        model_provider_name="fake",
        max_input_chars=20000,
    )
    await db_session.commit()

    assert version.status == "COMPLETED"
    assert version.version_number == 1
    assert version.schema_version == "candidate-identity-v1"
    assert version.identity_content["full_name"]["value"] == "Jane Synthetic Doe"
    assert version.identity_content["email"]["value"] == "jane.synthetic@example.com"
    assert version.identity_content["phone"]["value"] == "+1-555-0100"


async def test_missing_identity_field_remains_null(
    db_session: AsyncSession, candidate_with_contact_content
) -> None:
    tenant, candidate, document, _canonical = candidate_with_contact_content
    extraction = CandidateIdentityExtraction(
        full_name=IdentityFieldItem(
            value="Jane Synthetic Doe",
            evidence=[EvidenceRef(page=1, block_index=0, quote="Jane Synthetic Doe")],
        ),
        email=None,
        phone=None,
    )
    llm = FakeLLMProvider(identity_extraction=extraction)

    version = await extract_candidate_identity(
        db_session,
        llm,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        candidate_document=document,
        model_provider_name="fake",
        max_input_chars=20000,
    )
    await db_session.commit()

    assert version.status == "COMPLETED"
    assert version.identity_content["email"] is None
    assert version.identity_content["phone"] is None


def test_strict_extra_field_rejected() -> None:
    with pytest.raises(Exception):  # noqa: B017 — pydantic ValidationError
        CandidateIdentityExtraction.model_validate(
            {
                "full_name": {
                    "value": "Jane Synthetic Doe",
                    "evidence": [{"page": 1, "block_index": 0, "quote": "Jane"}],
                },
                "date_of_birth": "1990-01-01",
            }
        )


async def test_invalid_evidence_page_rejected(
    db_session: AsyncSession, candidate_with_contact_content
) -> None:
    tenant, candidate, document, _canonical = candidate_with_contact_content
    bad_extraction = CandidateIdentityExtraction(
        full_name=IdentityFieldItem(
            value="Jane Synthetic Doe",
            evidence=[EvidenceRef(page=99, block_index=0, quote="Jane Synthetic Doe")],
        )
    )
    llm = FakeLLMProvider(identity_extraction=bad_extraction)

    version = await extract_candidate_identity(
        db_session,
        llm,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        candidate_document=document,
        model_provider_name="fake",
        max_input_chars=20000,
    )
    await db_session.commit()

    assert version.status == "FAILED"
    assert version.error_code == "EVIDENCE_INVALID"
    assert version.identity_content is None


async def test_evidence_page_block_mismatch_rejected(
    db_session: AsyncSession, candidate_with_contact_content
) -> None:
    tenant, candidate, document, _canonical = candidate_with_contact_content
    bad_extraction = CandidateIdentityExtraction(
        email=IdentityFieldItem(
            value="jane.synthetic@example.com",
            evidence=[EvidenceRef(page=1, block_index=5, quote="jane.synthetic@example.com")],
        )
    )
    llm = FakeLLMProvider(identity_extraction=bad_extraction)

    version = await extract_candidate_identity(
        db_session,
        llm,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        candidate_document=document,
        model_provider_name="fake",
        max_input_chars=20000,
    )
    await db_session.commit()

    assert version.status == "FAILED"
    assert version.error_code == "EVIDENCE_INVALID"


async def test_fabricated_evidence_quote_rejected(
    db_session: AsyncSession, candidate_with_contact_content
) -> None:
    tenant, candidate, document, _canonical = candidate_with_contact_content
    bad_extraction = CandidateIdentityExtraction(
        phone=IdentityFieldItem(
            value="+9-999-9999",
            evidence=[EvidenceRef(page=1, block_index=0, quote="+9-999-9999")],
        )
    )
    llm = FakeLLMProvider(identity_extraction=bad_extraction)

    version = await extract_candidate_identity(
        db_session,
        llm,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        candidate_document=document,
        model_provider_name="fake",
        max_input_chars=20000,
    )
    await db_session.commit()

    assert version.status == "FAILED"
    assert version.error_code == "EVIDENCE_INVALID"


@pytest.mark.parametrize(
    "bad_extraction",
    [
        CandidateIdentityExtraction(
            email=IdentityFieldItem(
                value="mallory@example.com",
                evidence=[EvidenceRef(page=1, block_index=0, quote="Jane Synthetic Doe")],
            )
        ),
        CandidateIdentityExtraction(
            phone=IdentityFieldItem(
                value="+1-999-9999",
                evidence=[EvidenceRef(page=1, block_index=0, quote="Jane Synthetic Doe")],
            )
        ),
        CandidateIdentityExtraction(
            full_name=IdentityFieldItem(
                value="Mallory Example",
                evidence=[EvidenceRef(page=1, block_index=0, quote="Jane Synthetic Doe")],
            )
        ),
    ],
)
async def test_identity_value_must_be_supported_by_its_own_quote(
    db_session: AsyncSession, candidate_with_contact_content, bad_extraction
) -> None:
    tenant, candidate, document, _canonical = candidate_with_contact_content
    version = await extract_candidate_identity(
        db_session,
        FakeLLMProvider(identity_extraction=bad_extraction),
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        candidate_document=document,
        model_provider_name="fake",
        max_input_chars=20000,
    )
    await db_session.commit()

    assert version.status == "FAILED"
    assert version.error_code == "CLAIM_EVIDENCE_UNSUPPORTED"
    assert version.identity_content is None


async def test_bounded_retry_then_fails(
    db_session: AsyncSession, candidate_with_contact_content
) -> None:
    tenant, candidate, document, _canonical = candidate_with_contact_content
    llm = FakeLLMProvider(identity_extraction=_valid_identity_extraction(), fail_first_n_calls=2)

    version = await extract_candidate_identity(
        db_session,
        llm,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        candidate_document=document,
        model_provider_name="fake",
        max_input_chars=20000,
    )
    await db_session.commit()

    assert llm.call_count == 2
    assert version.status == "FAILED"
    assert version.error_code == "MODEL_SCHEMA_INVALID"


async def test_bounded_retry_then_succeeds(
    db_session: AsyncSession, candidate_with_contact_content
) -> None:
    tenant, candidate, document, _canonical = candidate_with_contact_content
    llm = FakeLLMProvider(identity_extraction=_valid_identity_extraction(), fail_first_n_calls=1)

    version = await extract_candidate_identity(
        db_session,
        llm,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        candidate_document=document,
        model_provider_name="fake",
        max_input_chars=20000,
    )
    await db_session.commit()

    assert llm.call_count == 2
    assert version.status == "COMPLETED"


async def test_provider_unavailable_fails_safely(
    db_session: AsyncSession, candidate_with_contact_content
) -> None:
    tenant, candidate, document, _canonical = candidate_with_contact_content
    llm = FakeLLMProvider(error=ModelUnavailableError("connection refused"))

    version = await extract_candidate_identity(
        db_session,
        llm,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        candidate_document=document,
        model_provider_name="fake",
        max_input_chars=20000,
    )
    await db_session.commit()

    assert version.status == "FAILED"
    assert version.error_code == "MODEL_UNAVAILABLE"


async def test_provider_timeout_fails_safely(
    db_session: AsyncSession, candidate_with_contact_content
) -> None:
    tenant, candidate, document, _canonical = candidate_with_contact_content
    llm = FakeLLMProvider(error=ModelTimeoutError("timed out"))

    version = await extract_candidate_identity(
        db_session,
        llm,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        candidate_document=document,
        model_provider_name="fake",
        max_input_chars=20000,
    )
    await db_session.commit()

    assert version.status == "FAILED"
    assert version.error_code == "MODEL_TIMEOUT"


async def test_reextraction_creates_v2_preserves_v1_immutable(
    db_session: AsyncSession, candidate_with_contact_content
) -> None:
    tenant, candidate, document, _canonical = candidate_with_contact_content
    llm1 = FakeLLMProvider(identity_extraction=_valid_identity_extraction())
    v1 = await extract_candidate_identity(
        db_session,
        llm1,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        candidate_document=document,
        model_provider_name="fake",
        max_input_chars=20000,
    )
    await db_session.commit()
    assert v1.version_number == 1
    v1_snapshot = v1.identity_content

    llm2 = FakeLLMProvider(identity_extraction=_valid_identity_extraction())
    v2 = await extract_candidate_identity(
        db_session,
        llm2,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        candidate_document=document,
        model_provider_name="fake",
        max_input_chars=20000,
    )
    await db_session.commit()

    assert v2.version_number == 2
    current = await get_current_identity_version(
        db_session, tenant_id=tenant.id, candidate_id=candidate.id
    )
    assert current.version_number == 2
    assert v1.identity_content == v1_snapshot  # v1 row never mutated in place


async def test_no_canonical_document_raises_precondition_error(
    db_session: AsyncSession,
) -> None:
    tenant = await create_tenant(db_session, name=f"IdTenant-{uuid.uuid4().hex[:8]}")
    await db_session.commit()
    candidate = await create_candidate(db_session, tenant_id=tenant.id)
    document = await create_candidate_document(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        original_filename="unparsed.pdf",
        mime_type="application/pdf",
        byte_size=10,
        sha256_hash="b" * 64,
        storage_key=f"test/{uuid.uuid4().hex}",
    )
    await db_session.commit()

    llm = FakeLLMProvider(identity_extraction=_valid_identity_extraction())
    with pytest.raises(IdentityExtractionPreconditionError) as exc_info:
        await extract_candidate_identity(
            db_session,
            llm,
            tenant_id=tenant.id,
            candidate_id=candidate.id,
            candidate_document=document,
            model_provider_name="fake",
            max_input_chars=20000,
        )
    assert exc_info.value.code == "UNSUPPORTED_CANONICAL_DOCUMENT"


async def test_tenant_isolation_identity_version_not_visible_cross_tenant(
    db_session: AsyncSession, candidate_with_contact_content
) -> None:
    tenant_a, candidate, document, _canonical = candidate_with_contact_content
    tenant_b = await create_tenant(db_session, name=f"IdTenantB-{uuid.uuid4().hex[:8]}")
    await db_session.commit()

    llm = FakeLLMProvider(identity_extraction=_valid_identity_extraction())
    await extract_candidate_identity(
        db_session,
        llm,
        tenant_id=tenant_a.id,
        candidate_id=candidate.id,
        candidate_document=document,
        model_provider_name="fake",
        max_input_chars=20000,
    )
    await db_session.commit()

    leaked = await get_current_identity_version(
        db_session, tenant_id=tenant_b.id, candidate_id=candidate.id
    )
    assert leaked is None

    own = await get_current_identity_version(
        db_session, tenant_id=tenant_a.id, candidate_id=candidate.id
    )
    assert own is not None


async def test_identity_audit_metadata_contains_no_pii(
    db_session: AsyncSession, candidate_with_contact_content
) -> None:
    from sqlalchemy import select

    from meyar.models.audit_event import AuditEvent

    tenant, candidate, document, _canonical = candidate_with_contact_content
    llm = FakeLLMProvider(identity_extraction=_valid_identity_extraction())
    await extract_candidate_identity(
        db_session,
        llm,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        candidate_document=document,
        model_provider_name="fake",
        max_input_chars=20000,
    )
    await db_session.commit()

    result = await db_session.execute(
        select(AuditEvent).where(AuditEvent.tenant_id == tenant.id)
    )
    for event in result.scalars().all():
        metadata_str = str(event.event_metadata)
        assert "Jane Synthetic Doe" not in metadata_str
        assert "jane.synthetic@example.com" not in metadata_str
        assert "+1-555-0100" not in metadata_str


async def test_identity_extraction_does_not_log_pii(
    db_session: AsyncSession, candidate_with_contact_content, caplog
) -> None:
    tenant, candidate, document, _canonical = candidate_with_contact_content
    llm = FakeLLMProvider(identity_extraction=_valid_identity_extraction())
    with caplog.at_level("DEBUG"):
        await extract_candidate_identity(
            db_session,
            llm,
            tenant_id=tenant.id,
            candidate_id=candidate.id,
            candidate_document=document,
            model_provider_name="fake",
            max_input_chars=20000,
        )
    await db_session.commit()

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "Jane Synthetic Doe" not in log_text
    assert "jane.synthetic@example.com" not in log_text


async def test_identity_view_is_unredacted_unlike_professional_view(
    db_session: AsyncSession, candidate_with_contact_content
) -> None:
    """The whole point of a separate identity view: email/phone must be
    visible to identity extraction even though the professional view
    redacts them (Slice 4)."""
    from meyar.extraction.view import build_identity_document_view, build_professional_document_view
    from meyar.services.candidate_document_repo import get_latest_canonical_document

    tenant, candidate, document, _canonical = candidate_with_contact_content
    canonical = await get_latest_canonical_document(
        db_session, tenant_id=tenant.id, candidate_document_id=document.id
    )
    identity_view = build_identity_document_view(canonical)
    professional_view = build_professional_document_view(canonical)

    assert "jane.synthetic@example.com" in identity_view.blocks[0].text
    assert "jane.synthetic@example.com" not in professional_view.blocks[0].text
    assert "[REDACTED_EMAIL]" in professional_view.blocks[0].text
