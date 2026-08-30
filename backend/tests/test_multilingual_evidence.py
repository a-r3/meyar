"""Slice 13 — multilingual (Azerbaijani / Russian / English) evidence.

docs/SECURITY_PRIVACY.md lists Azerbaijani, Russian, and English in its
test-data minimum coverage. This is a targeted evidence pass proving the
*already-existing* local parsing + extraction pipeline handles all three
languages under the currently accepted local-AI architecture — it does
not add any new multilingual subsystem, language detection, or
translation. Two layers of proof, matching the two layers where language
could plausibly break something:

1. Parsing (`LocalTextParser`) — `pypdf`/`python-docx` `.extract_text()`
   is Unicode-based text extraction with no language-specific logic, so a
   DOCX proof generalizes to PDF by construction (neither parser
   inspects, filters, or transforms text by language).
2. Extraction (`extract_candidate_profile` / `extract_candidate_identity`)
   — Pydantic v2 schema validation, evidence-quote verification, and
   Postgres JSON persistence must round-trip non-Latin/Cyrillic text
   unchanged. Uses `FakeLLMProvider` (no live Ollama dependency), so this
   proves the *pipeline* handles these scripts — it makes no claim about
   real-model extraction quality/accuracy in any language, which is a
   target-Mac/production-model concern, not a Slice 13 evidence gap.
"""

import io
import uuid

import docx
import pytest
from fakes import FakeLLMProvider
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.extraction.identity_service import extract_candidate_identity
from meyar.extraction.service import extract_candidate_profile
from meyar.schemas.candidate_identity import CandidateIdentityExtraction, IdentityFieldItem
from meyar.schemas.candidate_profile import (
    CandidateProfileExtraction,
    EmploymentItem,
    EvidenceRef,
    SkillItem,
)
from meyar.services.candidate_document_repo import get_candidate_document

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Synthetic, non-attributable sample sentences — not real candidate content.
_SAMPLES = {
    "az": "Bacarıqlar: Python, SQL, verilənlər bazası idarəetməsi. "
    "Təcrübə: Baş proqramçı, 2021-2025.",
    "ru": "Навыки: Python, SQL, управление базами данных. Опыт: Ведущий разработчик, 2021-2025.",
    "en": "Skills: Python, SQL, database administration. Experience: Lead Developer, 2021-2025.",
}
_NAMES = {
    "az": "Aygün Məmmədova",
    "ru": "Екатерина Смирнова",
    "en": "Jane Synthetic Doe",
}


def _auth(plaintext: str) -> dict:
    return {"Authorization": f"Bearer {plaintext}"}


def _synthetic_docx_bytes(paragraph_text: str) -> bytes:
    document = docx.Document()
    document.add_paragraph(paragraph_text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


@pytest.mark.parametrize("language", ["az", "ru", "en"])
async def test_docx_parser_extracts_text_verbatim_for_language(
    client: AsyncClient, tenant_and_key, language: str
) -> None:
    """Layer 1: LocalTextParser is Unicode-transparent — it performs no
    language detection, filtering, or transformation, so text in any of
    the three languages survives parsing byte-for-byte (modulo the
    parser's own whitespace normalization)."""
    _tenant, _key, plaintext = tenant_and_key
    candidate_resp = await client.post("/api/v1/candidates", headers=_auth(plaintext))
    assert candidate_resp.status_code == 201
    candidate_id = candidate_resp.json()["id"]

    upload = await client.post(
        f"/api/v1/candidates/{candidate_id}/documents",
        headers=_auth(plaintext),
        files={
            "file": (
                f"cv_{language}.docx",
                _synthetic_docx_bytes(_SAMPLES[language]),
                DOCX_MIME,
            )
        },
    )
    assert upload.status_code == 201
    body = upload.json()
    assert body["parser_status"] == "PARSED"
    all_text = " ".join(
        block["text"] for page in body["canonical"]["pages"] for block in page["blocks"]
    )
    assert _SAMPLES[language] in all_text


@pytest.mark.parametrize("language", ["az", "ru", "en"])
async def test_profile_extraction_round_trips_language_evidence_unchanged(
    client: AsyncClient, db_session: AsyncSession, tenant_and_key, language: str
) -> None:
    """Layer 2: a FakeLLMProvider extraction whose evidence quote is in
    the target language must pass Pydantic validation, evidence
    verification against the real parsed document, and Postgres JSON
    persistence — with the text unchanged, proving the schema/DB layer is
    not Latin-only."""
    tenant, _key, plaintext = tenant_and_key
    candidate_resp = await client.post("/api/v1/candidates", headers=_auth(plaintext))
    candidate_id = candidate_resp.json()["id"]
    upload = await client.post(
        f"/api/v1/candidates/{candidate_id}/documents",
        headers=_auth(plaintext),
        files={
            "file": (
                f"cv_{language}.docx",
                _synthetic_docx_bytes(_SAMPLES[language]),
                DOCX_MIME,
            )
        },
    )
    assert upload.json()["parser_status"] == "PARSED"
    document_id = upload.json()["id"]

    document = await get_candidate_document(
        db_session,
        tenant_id=tenant.id,
        candidate_id=uuid.UUID(candidate_id),
        document_id=uuid.UUID(document_id),
    )
    extraction = CandidateProfileExtraction(
        skills=[
            SkillItem(
                name="Python",
                evidence=[EvidenceRef(page=1, block_index=0, quote=_SAMPLES[language])],
            )
        ],
        employment_history=[
            EmploymentItem(
                title="Lead Developer",
                start_date="2021",
                end_date="2025",
                evidence=[EvidenceRef(page=1, block_index=0, quote=_SAMPLES[language])],
            )
        ],
    )
    llm = FakeLLMProvider(extraction=extraction)

    version = await extract_candidate_profile(
        db_session,
        llm,
        tenant_id=tenant.id,
        candidate_id=uuid.UUID(candidate_id),
        candidate_document=document,
        model_provider_name="fake",
        max_input_chars=20000,
    )
    await db_session.commit()

    assert version.status == "COMPLETED"
    assert version.profile_content is not None
    stored_quote = version.profile_content["skills"][0]["evidence"][0]["quote"]
    assert stored_quote == _SAMPLES[language]


@pytest.mark.parametrize("language", ["az", "ru", "en"])
async def test_identity_extraction_round_trips_non_latin_full_name(
    client: AsyncClient, db_session: AsyncSession, tenant_and_key, language: str
) -> None:
    """A non-Latin/Cyrillic full_name must round-trip through the identity
    pipeline unchanged — proving CandidateIdentity storage is not
    Latin-only either."""
    tenant, _key, plaintext = tenant_and_key
    candidate_resp = await client.post("/api/v1/candidates", headers=_auth(plaintext))
    candidate_id = candidate_resp.json()["id"]
    name = _NAMES[language]
    upload = await client.post(
        f"/api/v1/candidates/{candidate_id}/documents",
        headers=_auth(plaintext),
        files={
            "file": (f"cv_{language}.docx", _synthetic_docx_bytes(name), DOCX_MIME),
        },
    )
    assert upload.json()["parser_status"] == "PARSED"
    document_id = upload.json()["id"]

    document = await get_candidate_document(
        db_session,
        tenant_id=tenant.id,
        candidate_id=uuid.UUID(candidate_id),
        document_id=uuid.UUID(document_id),
    )
    extraction = CandidateIdentityExtraction(
        full_name=IdentityFieldItem(
            value=name, evidence=[EvidenceRef(page=1, block_index=0, quote=name)]
        )
    )
    llm = FakeLLMProvider(identity_extraction=extraction)

    version = await extract_candidate_identity(
        db_session,
        llm,
        tenant_id=tenant.id,
        candidate_id=uuid.UUID(candidate_id),
        candidate_document=document,
        model_provider_name="fake",
        max_input_chars=20000,
    )
    await db_session.commit()

    assert version.status == "COMPLETED"
    assert version.identity_content["full_name"]["value"] == name
