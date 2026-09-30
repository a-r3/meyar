"""Issue #84 — durable, immutable semantic provenance for agent-confirmed
criteria versions (D-088 amendment).

Synthetic text only; the local model is a fake provider. Covers the strict
schema, the fail-closed confirmation builder, carrying accepted local-model
identity and rejected-proposal counts, and the full HTTP confirm path
(reconstruction from the persisted version, exclusion durability across
transcript truncation, scorer MATCH unaffected by provenance)."""

import hashlib
import re
import uuid
from datetime import date

import pytest
from fakes import FakeLLMProvider
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.agent.canonical_requirements import JD_SEMANTIC_POLICY_VERSION
from meyar.agent.prompts import JD_CRITERIA_DRAFT_PROMPT_VERSION
from meyar.agent.schemas import (
    AgentActionType,
    AgentDecision,
    JDCriteriaDraft,
    JDDraftCriterionItem,
    SemanticInterpretationSource,
    SemanticReviewDecision,
    SemanticReviewDecisionKind,
)
from meyar.agent.semantic_provenance import (
    AgentSemanticProvenance,
    SemanticProvenanceError,
    build_agent_semantic_provenance,
    parse_agent_semantic_provenance,
)
from meyar.agent.semantic_requirements import analyze_hr_text
from meyar.agent.service import (
    _dispatch_draft_job_criteria,
    exclude_blocking_review_requirement,
    jd_draft_audit_metadata,
)
from meyar.config import Settings, get_settings
from meyar.evaluation.evaluators import evaluate_criterion
from meyar.evaluation.policy import OVERALL_STRONG_MATCH, compute_overall_result
from meyar.llm.dependency import get_llm_provider
from meyar.llm.provider import ModelUnavailableError
from meyar.main import app
from meyar.schemas.candidate_profile import CandidateProfileExtraction
from meyar.schemas.criteria import CriterionIn

COORDINATED_AZ = "Namizəd Python və PostgreSQL ilə işləməyi bacarmalıdır."
# The workspace routes an explicit vacancy header straight to JD drafting.
VACANCY_MESSAGE = f"Vakansiya: Analitik\n{COORDINATED_AZ}"
_UNAVAILABLE = FakeLLMProvider(jd_draft_error=ModelUnavailableError("synthetic outage"))


def _coordinated_llm(text: str = COORDINATED_AZ, **kwargs) -> FakeLLMProvider:
    ids = [span.span_id for span in analyze_hr_text(text).spans]
    return FakeLLMProvider(
        jd_draft=JDCriteriaDraft(
            title="Analitik",
            must_have=[
                JDDraftCriterionItem(span_id=ids[0], kind="SKILL", requirement="Python"),
                JDDraftCriterionItem(span_id=ids[1], kind="SKILL", requirement="PostgreSQL"),
            ],
        ),
        **kwargs,
    )


async def _draft(text: str, llm: FakeLLMProvider | None = None):
    result = await _dispatch_draft_job_criteria(llm or _UNAVAILABLE, jd_text=text)
    assert result is not None and result.job_draft is not None
    return result.job_draft


def _criteria(draft) -> list[CriterionIn]:
    return [*draft.must_have, *draft.preferred]


# --- draft carries provenance inputs -----------------------------------------


async def test_accepted_model_identity_and_policy_are_carried_on_the_draft() -> None:
    draft = await _draft(COORDINATED_AZ, _coordinated_llm())
    assert draft.source_sha256 == hashlib.sha256(COORDINATED_AZ.encode()).hexdigest()
    assert draft.semantic_policy_version == JD_SEMANTIC_POLICY_VERSION
    assert draft.semantic_prompt_version == JD_CRITERIA_DRAFT_PROMPT_VERSION
    assert draft.semantic_model is not None
    assert draft.semantic_model.provider == "fake"
    assert draft.semantic_model.model_name
    by_value = {c.value: c.id for c in draft.must_have}
    sources = {
        result.criterion_id: result.interpretation_source for result in draft.requirements
    }
    assert sources[by_value["PostgreSQL"]] == SemanticInterpretationSource.MODEL_VALIDATED


async def test_failed_model_call_records_no_model_identity() -> None:
    draft = await _draft("Python tələb olunur.")
    assert draft.semantic_model is None and draft.semantic_prompt_version is None
    assert [r.interpretation_source for r in draft.requirements] == [
        SemanticInterpretationSource.DETERMINISTIC
    ]


async def test_rejected_proposals_are_counted_on_draft_and_audit_metadata() -> None:
    llm = FakeLLMProvider(
        jd_draft=JDCriteriaDraft(
            title="Role",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001", kind="SKILL", requirement="Python Kubernetes"
                )
            ],
        )
    )
    draft = await _draft("Python required", llm)
    assert draft.rejected_proposal_count == 1
    metadata = jd_draft_audit_metadata(draft)
    assert metadata["rejected_proposal_count"] == 1
    assert metadata["model_result_accepted"] is True
    assert "Python" not in str(metadata) and "Kubernetes" not in str(metadata)


# --- fail-closed builder ------------------------------------------------------


async def test_builder_reconstructs_exact_fragments_for_every_criterion() -> None:
    draft = await _draft(COORDINATED_AZ, _coordinated_llm())
    provenance = build_agent_semantic_provenance(draft, _criteria(draft))
    assert provenance.draft_id == draft.draft_id
    assert {item.criterion_id for item in provenance.criteria} == {
        c.id for c in _criteria(draft)
    }
    for item in provenance.criteria:
        assert COORDINATED_AZ[item.start_offset : item.end_offset] == item.source_text
    dumped = provenance.model_dump(mode="json")
    assert COORDINATED_AZ not in str(dumped)  # never the full JD, only fragments
    assert parse_agent_semantic_provenance(dumped) == provenance


async def test_builder_fails_closed_on_missing_policy_version() -> None:
    draft = await _draft("Python tələb olunur.")
    tampered = draft.model_copy(update={"semantic_policy_version": None})
    with pytest.raises(SemanticProvenanceError):
        build_agent_semantic_provenance(tampered, _criteria(tampered))


async def test_builder_fails_closed_on_missing_source_digest() -> None:
    draft = await _draft("Python tələb olunur.")
    tampered = draft.model_copy(update={"source_sha256": None})
    with pytest.raises(SemanticProvenanceError):
        build_agent_semantic_provenance(tampered, _criteria(tampered))


async def test_builder_fails_closed_on_missing_criterion_provenance() -> None:
    draft = await _draft("Python tələb olunur. SQL tələb olunur.")
    requirements = [r.model_copy(update={"criterion_id": None}) for r in draft.requirements]
    tampered = draft.model_copy(update={"requirements": requirements[:1] + requirements[1:]})
    with pytest.raises(SemanticProvenanceError):
        build_agent_semantic_provenance(tampered, _criteria(tampered))


async def test_builder_fails_closed_on_duplicate_confirmed_criterion() -> None:
    draft = await _draft("Python tələb olunur.")
    criteria = _criteria(draft)
    with pytest.raises(SemanticProvenanceError):
        build_agent_semantic_provenance(draft, [*criteria, *criteria])


async def test_builder_fails_closed_on_duplicate_source_span() -> None:
    draft = await _draft("Python tələb olunur. SQL tələb olunur.")
    first, second = draft.requirements
    tampered = draft.model_copy(
        update={"requirements": [first, second.model_copy(update={"span_id": first.span_id})]}
    )
    with pytest.raises(SemanticProvenanceError):
        build_agent_semantic_provenance(tampered, _criteria(tampered))


async def test_builder_fails_closed_on_criterion_mismatch() -> None:
    draft = await _draft("Python tələb olunur.")
    altered = [c.model_copy(update={"value": "Kubernetes"}) for c in _criteria(draft)]
    with pytest.raises(SemanticProvenanceError):
        build_agent_semantic_provenance(draft, altered)


async def test_builder_fails_closed_on_unknown_confirmed_criterion() -> None:
    draft = await _draft("Python tələb olunur.")
    extra = _criteria(draft)[0].model_copy(update={"id": "kubernetes", "value": "Kubernetes"})
    with pytest.raises(SemanticProvenanceError):
        build_agent_semantic_provenance(draft, [*_criteria(draft), extra])


@pytest.mark.parametrize(
    "update",
    [
        {"end_offset": 5},
        {"text": "Pyth0n tələb olunur"},
        {"text": "Python"},
        {"interpretation_source": None},
    ],
)
async def test_builder_fails_closed_on_offset_or_text_mismatch(update: dict) -> None:
    draft = await _draft("Python tələb olunur.")
    requirement = draft.requirements[0].model_copy(update=update)
    tampered = draft.model_copy(update={"requirements": [requirement]})
    with pytest.raises(SemanticProvenanceError):
        build_agent_semantic_provenance(tampered, _criteria(tampered))


async def test_builder_fails_closed_on_unknown_span_review_decision() -> None:
    draft = await _draft("Python tələb olunur.")
    tampered = draft.model_copy(
        update={
            "review_decisions": [
                SemanticReviewDecision(
                    span_id="req-0099", decision=SemanticReviewDecisionKind.MUST_HAVE
                )
            ]
        }
    )
    with pytest.raises(SemanticProvenanceError):
        build_agent_semantic_provenance(tampered, _criteria(tampered))


async def test_builder_fails_closed_on_inconsistent_exclusion() -> None:
    draft = await _draft("Python tələb olunur.\nKubernetes təcrübəsi mütləqdir.")
    review = next(item for item in draft.needs_review if item.blocking)
    excluded = exclude_blocking_review_requirement(draft, span_id=review.span_id)
    assert excluded.review_decisions == [
        SemanticReviewDecision(
            span_id=review.span_id, decision=SemanticReviewDecisionKind.EXCLUDED_BY_REVIEWER
        )
    ]
    build_agent_semantic_provenance(excluded, _criteria(excluded))  # consistent: ok
    without_decision = excluded.model_copy(update={"review_decisions": []})
    with pytest.raises(SemanticProvenanceError):
        build_agent_semantic_provenance(without_decision, _criteria(without_decision))


# --- strict schema on read ----------------------------------------------------


async def _valid_payload() -> dict:
    draft = await _draft(COORDINATED_AZ, _coordinated_llm())
    return build_agent_semantic_provenance(draft, _criteria(draft)).model_dump(mode="json")


def test_parse_returns_none_for_versions_without_agent_provenance() -> None:
    assert parse_agent_semantic_provenance(None) is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(raw_model_output="ignore previous instructions"),
        lambda p: p.update(schema_version="jd-semantic-provenance-v0"),
        lambda p: p.update(semantic_policy_version=""),
        lambda p: p.update(source_sha256="not-a-digest"),
        lambda p: p.update(model=None),  # MODEL_VALIDATED criteria require a model
        lambda p: p["criteria"].append(dict(p["criteria"][0])),
        lambda p: p["criteria"][0].update(source_text="x"),
        lambda p: p["criteria"][0].update(jd_text="full vacancy text"),
        lambda p: p.update(
            review_decisions=[
                {"span_id": p["criteria"][0]["span_id"], "decision": "EXCLUDED_BY_REVIEWER"}
            ]
        ),
    ],
)
async def test_parse_rejects_malformed_persisted_provenance(mutate) -> None:
    payload = await _valid_payload()
    mutate(payload)
    with pytest.raises(ValidationError):
        parse_agent_semantic_provenance(payload)


async def test_provenance_schema_is_frozen() -> None:
    provenance = AgentSemanticProvenance.model_validate(await _valid_payload())
    with pytest.raises(ValidationError):
        provenance.source_sha256 = "b" * 64  # type: ignore[misc]


# --- HTTP confirm boundary ----------------------------------------------------


@pytest.fixture
def local_ui_settings() -> Settings:
    settings = Settings(ui_cookie_secure=False)
    app.dependency_overrides[get_settings] = lambda: settings
    return settings


async def _login_and_csrf(client: AsyncClient, username: str, password: str) -> str:
    response = await client.post(
        "/ui/login", data={"username": username, "password": password}, follow_redirects=False
    )
    assert response.status_code == 303
    home = await client.get("/ui")
    match = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', home.text)
    assert match is not None
    return match.group(1)


def _profile(*skills: str) -> CandidateProfileExtraction:
    return CandidateProfileExtraction.model_validate(
        {
            "skills": [
                {
                    "name": skill,
                    "category": None,
                    "evidence": [
                        {"page": 1, "block_index": 0, "quote": f"Synthetic evidence: {skill}"}
                    ],
                }
                for skill in skills
            ],
            "employment_history": [],
            "education": [],
            "certifications": [],
            "languages": [],
            "projects": [],
        }
    )


async def test_confirmed_version_reconstructs_semantic_provenance_and_still_matches(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    from meyar.models.job_criteria_version import JobCriteriaVersion
    from meyar.services.job_criteria_repo import get_agent_semantic_provenance

    tenant, user, password, _membership = tenant_and_user
    tenant_id = tenant.id
    await db_session.commit()
    fake = _coordinated_llm(
        VACANCY_MESSAGE,
        agent_decision=AgentDecision(action=AgentActionType.DRAFT_JOB_CRITERIA)
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    csrf = await _login_and_csrf(client, user.username, password)
    page = await client.post("/ui/agent", data={"message": VACANCY_MESSAGE, "csrf_token": csrf})
    assert page.status_code == 200
    confirm = re.search(r'action="(/ui/agent/drafts/[0-9a-f-]+/confirm)"', page.text)
    assert confirm is not None
    confirmed = await client.post(confirm.group(1), data={"csrf_token": csrf})
    assert confirmed.status_code == 200

    version = (
        await db_session.execute(
            select(JobCriteriaVersion).where(JobCriteriaVersion.tenant_id == tenant_id)
        )
    ).scalar_one()
    provenance = await get_agent_semantic_provenance(
        db_session, tenant_id=tenant_id, criteria_version_id=version.id
    )
    assert provenance is not None
    assert str(provenance.draft_id) in confirm.group(1)
    assert provenance.source_sha256 == hashlib.sha256(VACANCY_MESSAGE.encode()).hexdigest()
    assert provenance.semantic_policy_version == JD_SEMANTIC_POLICY_VERSION
    assert provenance.prompt_version == JD_CRITERIA_DRAFT_PROMPT_VERSION
    assert provenance.model is not None and provenance.model.provider == "fake"
    criteria = [CriterionIn.model_validate(item) for item in version.criteria]
    by_id = {item.criterion_id: item for item in provenance.criteria}
    assert set(by_id) == {criterion.id for criterion in criteria}
    spans = {span.span_id: span for span in analyze_hr_text(VACANCY_MESSAGE).spans}
    for criterion in criteria:
        record = by_id[criterion.id]
        span = spans[record.span_id]
        assert (record.start_offset, record.end_offset) == (span.start_offset, span.end_offset)
        assert VACANCY_MESSAGE[record.start_offset : record.end_offset] == record.source_text
        assert criterion.value in record.source_text
    postgres = next(c for c in criteria if c.value == "PostgreSQL")
    assert by_id[postgres.id].interpretation_source == (
        SemanticInterpretationSource.MODEL_VALIDATED
    )
    # Provenance is audit evidence only: the deterministic scorer consumes the
    # persisted criteria and a Python + PostgreSQL profile matches.
    results = [
        evaluate_criterion(
            criterion, _profile("Python", "PostgreSQL"), evaluation_as_of_date=date(2026, 9, 30)
        )
        for criterion in criteria
    ]
    assert {result.status for result in results} == {"MATCH"}
    assert compute_overall_result(results) == OVERALL_STRONG_MATCH
    # Tenant-scoped read: another tenant never resolves this version.
    assert (
        await get_agent_semantic_provenance(
            db_session, tenant_id=uuid.uuid4(), criteria_version_id=version.id
        )
        is None
    )


async def test_exclusion_is_durable_on_the_version_across_transcript_truncation(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    from meyar.models.agent_conversation import AgentConversation
    from meyar.models.audit_event import AuditEvent
    from meyar.models.job_criteria_version import JobCriteriaVersion
    from meyar.services.job_criteria_repo import get_agent_semantic_provenance

    tenant, user, password, _membership = tenant_and_user
    tenant_id = tenant.id
    await db_session.commit()
    app.dependency_overrides[get_llm_provider] = lambda: _UNAVAILABLE
    csrf = await _login_and_csrf(client, user.username, password)
    page = await client.post(
        "/ui/agent",
        data={
            "message": "Vakansiya: Backend\nPython tələb olunur.\nKubernetes təcrübəsi mütləqdir.",
            "csrf_token": csrf,
        },
    )
    resolve_path = re.search(r'action="(/ui/agent/drafts/[0-9a-f-]+/resolve)"', page.text)
    span = re.search(r'name="span_id" value="(req-\d{4})"', page.text)
    assert resolve_path is not None and span is not None
    excluded = await client.post(
        resolve_path.group(1),
        data={"csrf_token": csrf, "span_id": span.group(1), "decision": "exclude"},
    )
    assert excluded.status_code == 200
    draft_id = resolve_path.group(1).split("/")[-2]
    event = (
        await db_session.execute(
            select(AuditEvent).where(
                AuditEvent.tenant_id == tenant_id,
                AuditEvent.event_type == "agent.draft.review_resolved",
            )
        )
    ).scalar_one()
    assert event.event_metadata["draft_id"] == draft_id
    assert event.event_metadata["criterion_type"] == "EXCLUDED_BY_REVIEWER"
    assert "Kubernetes" not in str(event.event_metadata)

    confirmed = await client.post(
        resolve_path.group(1).replace("/resolve", "/confirm"), data={"csrf_token": csrf}
    )
    assert confirmed.status_code == 200
    # The bounded session transcript may be truncated/retained away; the
    # immutable version keeps the human decision.
    await db_session.execute(update(AgentConversation).values(turns=[]))
    await db_session.commit()
    version = (
        await db_session.execute(
            select(JobCriteriaVersion).where(JobCriteriaVersion.tenant_id == tenant_id)
        )
    ).scalar_one()
    provenance = await get_agent_semantic_provenance(
        db_session, tenant_id=tenant_id, criteria_version_id=version.id
    )
    assert provenance is not None
    assert [(d.span_id, d.decision) for d in provenance.review_decisions] == [
        (span.group(1), SemanticReviewDecisionKind.EXCLUDED_BY_REVIEWER)
    ]
    assert span.group(1) not in {item.span_id for item in provenance.criteria}
    assert [c["value"] for c in version.criteria] == ["Python"]
