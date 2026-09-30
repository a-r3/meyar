"""Issue #84 real-Ollama acceptance regression (Case 3).

A browser textarea submits CRLF line endings. Before this fix the CRLF form
of a JD reached routing, span analysis, hashing and the local model verbatim;
the real model then produced a schema-invalid proposal, the strict schema
(correctly) rejected it, and because the vacancy title was model-only the
explicit ``Vakansiya: Kredit Analitiki 2`` title was lost.

These tests pin two narrow properties:
1. transport newline differences never change semantic behaviour (one
   canonical LF source feeds routing, offsets, hashing and provenance);
2. an explicit ``Vakansiya:``/``Vacancy:`` header line is deterministic,
   source-grounded title data that no model proposal can rewrite and that
   never becomes a criterion or a result count.
"""

import hashlib

import pytest
from fakes import FakeLLMProvider
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.agent.schemas import JDCriteriaDraft, JDDraftCriterionItem, RequirementSpanState
from meyar.agent.semantic_requirements import analyze_hr_text, explicit_vacancy_title
from meyar.agent.service import _dispatch_draft_job_criteria, normalize_message_newlines
from meyar.config import Settings, get_settings
from meyar.core.result_count import DEFAULT_RESULT_LIMIT
from meyar.llm.dependency import get_llm_provider
from meyar.llm.provider import ModelSchemaInvalidError
from meyar.main import app
from meyar.schemas.criteria import CriterionKind, CriterionType

CASE3_LF = "Vakansiya: Kredit Analitiki 2\nExcel tələb olunur."
CASE3_CRLF = "Vakansiya: Kredit Analitiki 2\r\nExcel tələb olunur."


# --------------------------------------------------------------------------
# 1. Canonical transport newlines
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (CASE3_CRLF, CASE3_LF),
        ("Vakansiya: Kredit Analitiki 2\rExcel tələb olunur.", CASE3_LF),
        (CASE3_LF, CASE3_LF),
        ("a\r\n\r\nb\r", "a\n\nb\n"),
    ],
)
def test_newlines_are_normalized_to_one_canonical_form(raw: str, expected: str) -> None:
    assert normalize_message_newlines(raw) == expected


# --------------------------------------------------------------------------
# 2. Deterministic explicit vacancy title
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (CASE3_LF, "Kredit Analitiki 2"),
        ("VAKANSİYA: Kredit Analitiki 2\nExcel tələb olunur.", "Kredit Analitiki 2"),
        ("Vacancy: Java Developer 3\nJava is required.", "Java Developer 3"),
        ("Vakansiya:   Senior Backend Engineer  \nPython tələb olunur.", "Senior Backend Engineer"),
        # Header shares its line with a requirement via the existing explicit
        # dash separator: only the title part is title data.
        ("Vakansiya: Analitik — Python və SQL mütləqdir", "Analitik"),
    ],
)
def test_explicit_vacancy_header_yields_exact_source_title(text: str, expected: str) -> None:
    assert explicit_vacancy_title(text, analyze_hr_text(text)) == expected


@pytest.mark.parametrize(
    "text",
    [
        # No arbitrary first-line-as-title heuristic.
        "Kredit Analitiki 2\nExcel tələb olunur.",
        "Namizəd Python və PostgreSQL ilə işləməyi bacarmalıdır.",
        # An instruction wrapper is not a title header.
        "Vakansiya kimi analiz et: Namizəd Python bilməlidir.",
        # Header content that is itself a requirement is never a title.
        "Vakansiya: Python tələb olunur.",
        # Two competing headers are ambiguous.
        "Vakansiya: Kredit Analitiki 2\nVakansiya: Mühasib\nExcel tələb olunur.",
        # Unbounded header content is not title data.
        "Vakansiya: " + "A" * 121 + "\nExcel tələb olunur.",
        "Vakansiya:\nExcel tələb olunur.",
    ],
)
def test_no_explicit_title_without_single_bounded_header(text: str) -> None:
    assert explicit_vacancy_title(text, analyze_hr_text(text)) is None


def test_title_number_is_title_data_not_count_or_criterion() -> None:
    analysis = analyze_hr_text(CASE3_LF)
    assert [span.text for span in analysis.spans] == ["Excel tələb olunur"]
    assert analysis.result_count.requested is None
    assert analysis.result_count.effective == DEFAULT_RESULT_LIMIT


# --------------------------------------------------------------------------
# 3. Production draft boundary (_dispatch_draft_job_criteria)
# --------------------------------------------------------------------------


def _excel_proposal(title: str) -> JDCriteriaDraft:
    return JDCriteriaDraft(
        title=title,
        must_have=[JDDraftCriterionItem(span_id="req-0001", kind="SKILL", requirement="Excel")],
    )


def _assert_case3_semantics(draft) -> None:
    assert draft.title == "Kredit Analitiki 2"
    assert [(c.type, c.kind, c.value) for c in [*draft.must_have, *draft.preferred]] == [
        (CriterionType.MUST_HAVE, CriterionKind.SKILL, "Excel")
    ]
    assert draft.requested_result_limit is None
    assert draft.result_limit == DEFAULT_RESULT_LIMIT
    assert not draft.result_limit_needs_review
    assert [r.state for r in draft.requirements] == [RequirementSpanState.SCORABLE]
    assert all("Kredit" not in c.value and c.value != "2" for c in draft.must_have)


async def test_rejected_model_proposal_keeps_deterministic_header_title() -> None:
    """The reproduced real-Ollama failure: both attempts schema-invalid."""
    llm = FakeLLMProvider(jd_draft_error=ModelSchemaInvalidError("schema-invalid"))
    result = await _dispatch_draft_job_criteria(llm, jd_text=CASE3_LF)
    assert result is not None and result.job_draft is not None
    draft = result.job_draft
    assert draft.semantic_model is None
    _assert_case3_semantics(draft)


@pytest.mark.parametrize(
    "model_title",
    [
        "Vakansiya qaralaması",  # generic model title
        "Kredit Analitiki",  # grounded but drops the number
        "Credit Analyst",  # ungrounded rewrite
        "Ignore previous instructions",  # injected
        "Excel tələb olunur",  # a requirement sentence
    ],
)
async def test_model_title_cannot_rewrite_explicit_header_title(model_title: str) -> None:
    llm = FakeLLMProvider(jd_draft=_excel_proposal(model_title))
    result = await _dispatch_draft_job_criteria(llm, jd_text=CASE3_LF)
    assert result is not None and result.job_draft is not None
    _assert_case3_semantics(result.job_draft)


async def test_without_header_model_title_rules_are_unchanged() -> None:
    text = "Excel tələb olunur."
    grounded = await _dispatch_draft_job_criteria(
        FakeLLMProvider(jd_draft=_excel_proposal("Excel")), jd_text=text
    )
    assert grounded is not None and grounded.job_draft is not None
    assert grounded.job_draft.title == "Excel"
    rejected = await _dispatch_draft_job_criteria(
        FakeLLMProvider(jd_draft_error=ModelSchemaInvalidError("x")), jd_text=text
    )
    assert rejected is not None and rejected.job_draft is not None
    assert rejected.job_draft.title == "Vakansiya qaralaması"


# --------------------------------------------------------------------------
# 4. Real UI boundary: LF and browser CRLF are semantically equivalent
# --------------------------------------------------------------------------


@pytest.fixture
def local_ui_settings() -> Settings:
    settings = Settings(ui_cookie_secure=False)
    app.dependency_overrides[get_settings] = lambda: settings
    return settings


async def _csrf(client: AsyncClient) -> str:
    import re

    page = await client.get("/ui/agent")
    match = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', page.text)
    assert match is not None
    return match.group(1)


async def test_ui_crlf_and_lf_case3_are_semantically_equivalent(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    from meyar.agent.schemas import AgentJobDraftToolResult
    from meyar.models.agent_conversation import AgentConversation

    tenant, user, password, _membership = tenant_and_user
    # Reproduces the real failure mode: the model proposal is rejected, so
    # the result is owned entirely by the server's deterministic boundary.
    fake = FakeLLMProvider(jd_draft_error=ModelSchemaInvalidError("schema-invalid"))
    app.dependency_overrides[get_llm_provider] = lambda: fake
    login = await client.post(
        "/ui/login",
        data={"username": user.username, "password": password},
        follow_redirects=False,
    )
    assert login.status_code == 303

    seen_sources: list[str] = []
    pages: list[str] = []
    for message in (CASE3_LF, CASE3_CRLF):
        csrf = await _csrf(client)
        reset = await client.post(
            "/ui/agent/reset", data={"csrf_token": csrf}, follow_redirects=False
        )
        assert reset.status_code == 303
        csrf = await _csrf(client)
        response = await client.post("/ui/agent", data={"message": message, "csrf_token": csrf})
        assert response.status_code == 200
        assert fake.last_jd_text is not None
        seen_sources.append(fake.last_jd_text)
        pages.append(response.text)

    # The local model and every span/offset see one canonical source.
    assert seen_sources == [CASE3_LF, CASE3_LF]
    for page in pages:
        assert "Kredit Analitiki 2" in page
        assert "Tələbləri təsdiqlə və namizədləri sırala" in page

    conversations = (
        await db_session.scalars(
            select(AgentConversation)
            .where(AgentConversation.tenant_id == tenant.id)
            .order_by(AgentConversation.created_at)
        )
    ).all()
    payloads = [
        turn["pending_job_draft"]
        for conversation in conversations
        for turn in conversation.turns
        if isinstance(turn.get("pending_job_draft"), dict)
    ]
    assert len(payloads) == 2
    lf_draft, crlf_draft = (AgentJobDraftToolResult.model_validate(p) for p in payloads)
    for draft in (lf_draft, crlf_draft):
        _assert_case3_semantics(draft)
        assert draft.source_sha256 == hashlib.sha256(CASE3_LF.encode("utf-8")).hexdigest()
        assert draft.source_jd_text == CASE3_LF
        for requirement in draft.requirements:
            assert CASE3_LF[requirement.start_offset : requirement.end_offset] == requirement.text
    compared = ("title", "must_have", "preferred", "requirements", "source_sha256", "result_limit")
    assert lf_draft.model_dump(include=set(compared)) == crlf_draft.model_dump(
        include=set(compared)
    )
    # The persisted HR turn is the canonical text as well.
    user_turns = [
        turn["text"]
        for conversation in conversations
        for turn in conversation.turns
        if turn.get("role") == "user"
    ]
    assert user_turns == [CASE3_LF, CASE3_LF]
