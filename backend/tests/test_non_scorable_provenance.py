"""Issue #45 (C) — durable, source-bound provenance for requirements that did
NOT become scoring authority. Synthetic data only."""

import uuid

import pytest
from api_contract_support import human_surface
from conftest import BrowserTestClient as AsyncClient
from fakes import FakeLLMProvider
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_ui_agent_routes import local_ui_settings  # noqa: F401 - pytest fixture re-export

from meyar.agent.schemas import (
    JDCriteriaDraft,
    JDDraftCriterionItem,
    RequirementSpanState,
)
from meyar.agent.semantic_provenance import (
    AgentSemanticProvenance,
    NonScorableRequirementProvenance,
    SemanticProvenanceError,
    build_agent_semantic_provenance,
    parse_agent_semantic_provenance,
)
from meyar.agent.service import _dispatch_draft_job_criteria
from meyar.config import Settings
from meyar.llm.dependency import get_llm_provider
from meyar.main import app
from meyar.models.job_criteria_version import JobCriteriaVersion

UNSUPPORTED_LINE = "Candidate must be willing to travel"


async def _draft(source: str, *items: JDDraftCriterionItem):  # noqa: ANN202
    llm = FakeLLMProvider(jd_draft=JDCriteriaDraft(title="Role", must_have=list(items)))
    result = await _dispatch_draft_job_criteria(llm, jd_text=source)
    assert result is not None and result.job_draft is not None
    return result.job_draft


def _python_item(span: str = "req-0001", source_text: str = "Python required"):  # noqa: ANN202
    return JDDraftCriterionItem(
        span_id=span, kind="SKILL", requirement="Python", source_text=source_text
    )


async def test_unsupported_occurrence_is_recorded_with_exact_source_slice_and_no_authority() -> (
    None
):
    source = f"Python required\n{UNSUPPORTED_LINE}"
    draft = await _draft(source, _python_item())
    provenance = build_agent_semantic_provenance(draft, [*draft.must_have, *draft.preferred])
    assert provenance.non_scorable_requirements is not None
    [record] = provenance.non_scorable_requirements
    assert record.state == "UNSUPPORTED"
    assert record.reason_code == "UNSUPPORTED_BY_SCORING_POLICY"
    assert record.source_text == UNSUPPORTED_LINE
    assert source[record.start_offset : record.end_offset] == UNSUPPORTED_LINE
    assert record.scoring_authority is False and record.review_decision is None
    assert [c.criterion_id for c in provenance.criteria] == [draft.must_have[0].id]
    # The scored set is untouched by the non-scorable record.
    assert record.span_id not in {
        span.span_id for criterion in provenance.criteria for span in criterion.source_spans
    }
    assert [item.state for item in draft.requirements].count(RequirementSpanState.SCORABLE) == 1


async def test_prohibited_occurrence_records_location_and_reason_but_never_its_text() -> None:
    source = "Rol üçün namizəd kişi olmalıdır. Python bilməlidir."
    draft = await _draft(
        source,
        JDDraftCriterionItem(
            span_id="req-0002", kind="SKILL", requirement="Python", source_text="Python bilməlidir"
        ),
        JDDraftCriterionItem(
            span_id="req-0001", kind="SKILL", requirement="kişi", source_text="kişi olmalıdır"
        ),
    )
    provenance = build_agent_semantic_provenance(draft, [*draft.must_have, *draft.preferred])
    assert provenance.non_scorable_requirements is not None
    prohibited = [r for r in provenance.non_scorable_requirements if r.state == "PROHIBITED"]
    assert len(prohibited) == 1
    assert prohibited[0].source_text is None
    assert prohibited[0].reason_code == "PROHIBITED_ATTRIBUTE"
    assert "kişi" not in provenance.model_dump_json()


def test_non_scorable_record_cannot_claim_authority_or_hold_prohibited_text() -> None:
    base = {"span_id": "req-0001", "start_offset": 0, "end_offset": 4}
    with pytest.raises(ValidationError):
        NonScorableRequirementProvenance(
            **base,
            state="UNSUPPORTED",
            reason_code="UNSUPPORTED_BY_SCORING_POLICY",
            source_text="abcd",
            scoring_authority=True,  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError):  # prohibited text must not be kept
        NonScorableRequirementProvenance(
            **base, state="PROHIBITED", reason_code="PROHIBITED_ATTRIBUTE", source_text="abcd"
        )
    with pytest.raises(ValidationError):  # text length must match the offsets
        NonScorableRequirementProvenance(
            **base,
            state="UNSUPPORTED",
            reason_code="UNSUPPORTED_BY_SCORING_POLICY",
            source_text="abc",
        )
    with pytest.raises(ValidationError):  # exclusion needs both reason and decision
        NonScorableRequirementProvenance(
            **base,
            state="NEEDS_HUMAN_REVIEW",
            reason_code="EXCLUDED_BY_REVIEWER",
            source_text="abcd",
        )


async def test_a_scored_span_cannot_also_be_recorded_as_non_scorable() -> None:
    source = f"Python required\n{UNSUPPORTED_LINE}"
    draft = await _draft(source, _python_item())
    provenance = build_agent_semantic_provenance(draft, [*draft.must_have, *draft.preferred])
    payload = provenance.model_dump(mode="json")
    scored_span = payload["criteria"][0]["source_spans"][0]
    payload["non_scorable_requirements"].append(
        {
            "span_id": scored_span["span_id"],
            "start_offset": scored_span["start_offset"],
            "end_offset": scored_span["end_offset"],
            "state": "UNSUPPORTED",
            "reason_code": "UNSUPPORTED_BY_SCORING_POLICY",
            "source_text": scored_span["source_text"],
            "review_decision": None,
            "scoring_authority": False,
        }
    )
    with pytest.raises(ValidationError):
        AgentSemanticProvenance.model_validate(payload)


async def test_tampered_non_scorable_source_is_refused_at_the_confirmation_boundary() -> None:
    source = f"Python required\n{UNSUPPORTED_LINE}"
    draft = await _draft(source, _python_item())
    for item in draft.requirements:
        if item.state == RequirementSpanState.UNSUPPORTED:
            item.start_offset += 1  # the draft no longer matches the re-derived source
    with pytest.raises(SemanticProvenanceError):
        build_agent_semantic_provenance(draft, [*draft.must_have, *draft.preferred])


async def test_legacy_provenance_without_the_field_stays_truthfully_unrecorded() -> None:
    source = f"Python required\n{UNSUPPORTED_LINE}"
    draft = await _draft(source, _python_item())
    modern = build_agent_semantic_provenance(draft, [*draft.must_have, *draft.preferred])
    legacy = modern.model_dump(mode="json")
    del legacy["non_scorable_requirements"]  # exactly what every pre-#45 row looks like
    parsed = parse_agent_semantic_provenance(legacy)
    assert parsed is not None
    assert parsed.non_scorable_requirements is None  # "not recorded", never reconstructed
    assert parse_agent_semantic_provenance(None) is None


async def test_confirmed_version_exposes_non_scorable_provenance_and_scores_only_scorable(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    tenant, user, password, _membership = tenant_and_user
    tenant_id, username = tenant.id, user.username
    source = f"Python required\n{UNSUPPORTED_LINE}"
    from agent_plans import vacancy_proposal

    app.dependency_overrides[get_llm_provider] = lambda: FakeLLMProvider(
        agent_plan=vacancy_proposal(),
        jd_draft=JDCriteriaDraft(title="Role", must_have=[_python_item()]),
    )
    human = await human_surface(client, username, password)
    conversation = (
        await client.post(human.url("/conversations"), headers=human.write_headers)
    ).json()["id"]
    token = await client.post(
        human.url(f"/conversations/{conversation}/submissions"), headers=human.write_headers
    )
    turn = await client.post(
        human.url(f"/conversations/{conversation}/turns"),
        headers=human.write_headers,
        json={
            "message": f"Analyze this job description:\n{source}",
            "submission_id": token.json()["submission_id"],
        },
    )
    assert turn.status_code == 200, turn.text
    draft = next(r["job_draft"] for r in turn.json()["results"] if r["job_draft"])
    assert draft["unsupported_must_have"] == [UNSUPPORTED_LINE]
    confirmed = await client.post(
        human.url(f"/drafts/{draft['draft_id']}/confirm"), headers=human.write_headers, json={}
    )
    assert confirmed.status_code == 200, confirmed.text
    job_id = await db_session.scalar(
        select(JobCriteriaVersion.job_id).where(JobCriteriaVersion.tenant_id == tenant_id)
    )
    response = await client.get(
        human.url(f"/jobs/{job_id}/criteria/1/provenance"), headers=human.read_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["authoring_channel"] == "AGENT_CONFIRMED_DRAFT"
    assert body["draft_confirmation"]["draft_id"] == draft["draft_id"]
    assert body["non_scorable_recorded"] is True
    [record] = body["non_scorable_requirements"]
    assert record["source_text"] == UNSUPPORTED_LINE
    assert record["scoring_authority"] is False
    assert body["unsupported_requirements"] == [UNSUPPORTED_LINE]
    assert [c["criterion_id"] for c in body["semantic_provenance"]["criteria"]] == ["python"]
    # The immutable criteria themselves contain only the scorable requirement.
    version = await db_session.scalar(
        select(JobCriteriaVersion)
        .where(
            JobCriteriaVersion.job_id == uuid.UUID(job_id)
            if isinstance(job_id, str)
            else JobCriteriaVersion.job_id == job_id
        )
        .execution_options(populate_existing=True)
    )
    assert [c["value"] for c in version.criteria] == ["Python"]
