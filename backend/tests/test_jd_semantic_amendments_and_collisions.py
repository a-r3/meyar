"""Issue #84 — human-amendment provenance and canonical collision authority.

Blocker 1: a bounded HR follow-up that changes min_years / required_level /
modality creates an ordered, immutable, server-owned amendment; the durable
provenance must explain the whole transition from the ORIGINAL source span
to the confirmed criterion, and confirmation fails closed otherwise.

Blocker 2: the same canonical requirement never carries two weights. Exact
duplicates collapse to one criterion supported by several spans; conflicting
modality / duration / level becomes a blocking review that only an explicit
server-declared HR choice can resolve.

Synthetic text only; the local model is a fake provider (unavailable unless
stated), so every expectation is deterministic server authority.
"""

import hashlib
import re
import uuid
from datetime import date
from decimal import Decimal

import pytest
from fakes import FakeLLMProvider
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.agent.canonical_requirements import semantic_identity
from meyar.agent.schemas import (
    AgentJobDraftToolResult,
    JDDraftCriterionKind,
    RequirementSpanState,
    SemanticCriterionAmendment,
    SemanticCriterionAmendmentField,
    SemanticReviewReason,
)
from meyar.agent.semantic_provenance import (
    SemanticProvenanceError,
    build_agent_semantic_provenance,
    parse_agent_semantic_provenance,
)
from meyar.agent.service import (
    _apply_pending_draft_followup,
    _dispatch_draft_job_criteria,
    exclude_blocking_review_requirement,
    resolve_job_draft_review_modality,
    resolve_job_draft_semantic_conflict,
)
from meyar.config import Settings, get_settings
from meyar.evaluation.evaluators import evaluate_criterion
from meyar.evaluation.policy import compute_overall_result
from meyar.llm.dependency import get_llm_provider
from meyar.llm.provider import ModelUnavailableError
from meyar.main import app
from meyar.schemas.candidate_profile import CandidateProfileExtraction
from meyar.schemas.criteria import CriterionIn, CriterionKind, CriterionType
from meyar.scoring.policy import score_results
from meyar.ui.service import agent_draft_requires_resolution

_UNAVAILABLE = FakeLLMProvider(jd_draft_error=ModelUnavailableError("synthetic outage"))
AS_OF = date(2026, 9, 30)
PYTHON_3Y = "Python üzrə minimum 3 il təcrübə tələb olunur."
ENGLISH_B2 = "English B2 tələb olunur."


async def _draft(text: str) -> AgentJobDraftToolResult:
    result = await _dispatch_draft_job_criteria(_UNAVAILABLE, jd_text=text)
    assert result is not None and result.job_draft is not None
    return result.job_draft


def _follow(draft: AgentJobDraftToolResult, message: str) -> AgentJobDraftToolResult:
    updated = _apply_pending_draft_followup(draft, message)
    assert updated is not None, message
    return updated


def _criteria(draft: AgentJobDraftToolResult) -> list[CriterionIn]:
    return [*draft.must_have, *draft.preferred]


def _amendment(
    draft: AgentJobDraftToolResult,
    *,
    sequence: int,
    criterion_id: str,
    span_id: str,
    field: SemanticCriterionAmendmentField,
    previous: str,
    new: str,
    source: str = "synthetic HR follow-up",
) -> SemanticCriterionAmendment:
    return SemanticCriterionAmendment(
        sequence=sequence,
        criterion_id=criterion_id,
        span_id=span_id,
        field=field,
        previous_value=previous,
        new_value=new,
        source_text=source,
        source_sha256=hashlib.sha256(source.encode()).hexdigest(),
    )


def _replace_criterion(
    draft: AgentJobDraftToolResult, criterion: CriterionIn, **update
) -> AgentJobDraftToolResult:
    replacement = CriterionIn.model_validate({**criterion.model_dump(), **update})
    return draft.model_copy(
        update={
            "must_have": [replacement if c.id == criterion.id else c for c in draft.must_have],
            "preferred": [replacement if c.id == criterion.id else c for c in draft.preferred],
        }
    )


def _python_profile(start: str, end: str) -> CandidateProfileExtraction:
    evidence = [{"page": 1, "block_index": 0, "quote": "Synthetic: Python engineer"}]
    return CandidateProfileExtraction.model_validate(
        {
            "skills": [{"name": "Python", "category": None, "evidence": evidence}],
            "employment_history": [
                {
                    "title": "Engineer",
                    "organization": "Synthetic Org",
                    "start_date": start,
                    "end_date": end,
                    "is_current": False,
                    "evidence": evidence,
                }
            ],
            "skill_experience": [
                {
                    "skill_name": "Python",
                    "employment_index": 0,
                    "start_date": start,
                    "end_date": end,
                    "is_current": False,
                    "evidence": evidence,
                }
            ],
        }
    )


# ============================================================================
# Blocker 1 — human amendments
# ============================================================================


async def test_a_min_years_amendment_is_durable_truthful_and_scored() -> None:
    original = await _draft(PYTHON_3Y)
    (source_criterion,) = _criteria(original)
    assert (source_criterion.kind, source_criterion.min_years) == (
        CriterionKind.SKILL_EXPERIENCE,
        3.0,
    )
    amended = _follow(original, "3 ili 5 et")
    (criterion,) = _criteria(amended)
    assert criterion.min_years == 5.0

    provenance = build_agent_semantic_provenance(amended, [criterion])
    (record,) = provenance.criteria
    (span,) = record.source_spans
    assert "3 il" in span.source_text  # the source fragment still says 3 years
    assert PYTHON_3Y[span.start_offset : span.end_offset] == span.source_text
    assert record.origin.min_years == 3.0 and record.final.min_years == 5.0
    (amendment,) = provenance.amendments
    assert (amendment.field, amendment.previous_value, amendment.new_value) == (
        SemanticCriterionAmendmentField.MIN_YEARS,
        "3",
        "5",
    )
    assert amendment.source_text == "3 ili 5 et"
    assert amendment.source_sha256 == hashlib.sha256(b"3 ili 5 et").hexdigest()
    assert (amendment.criterion_id, amendment.span_id) == (criterion.id, span.span_id)
    # Durable JSON round-trips through strict read validation (replay).
    assert parse_agent_semantic_provenance(provenance.model_dump(mode="json")) == provenance

    # The scorer evaluates the amended 5-year criterion, not the source 3.
    four_years = _python_profile("2022-01", "2026-01")
    assert (
        evaluate_criterion(source_criterion, four_years, evaluation_as_of_date=AS_OF).status
        == "MATCH"
    )
    assert (
        evaluate_criterion(criterion, four_years, evaluation_as_of_date=AS_OF).status != "MATCH"
    )


async def test_b_chained_min_years_amendments_keep_the_ordered_history() -> None:
    draft = _follow(_follow(await _draft(PYTHON_3Y), "3 ili 5 et"), "5 ili 7 et")
    (criterion,) = _criteria(draft)
    assert criterion.min_years == 7.0
    provenance = build_agent_semantic_provenance(draft, [criterion])
    assert [
        (a.sequence, a.previous_value, a.new_value, a.source_text) for a in provenance.amendments
    ] == [(1, "3", "5", "3 ili 5 et"), (2, "5", "7", "5 ili 7 et")]
    (record,) = provenance.criteria
    assert (record.origin.min_years, record.final.min_years) == (3.0, 7.0)


async def test_c_language_level_amendment_chain_is_persisted() -> None:
    draft = _follow(_follow(await _draft(ENGLISH_B2), "B2-ni C1 et"), "C1-i C2 et")
    (criterion,) = _criteria(draft)
    assert criterion.required_level == "C2"
    provenance = build_agent_semantic_provenance(draft, [criterion])
    assert [(a.field, a.previous_value, a.new_value) for a in provenance.amendments] == [
        (SemanticCriterionAmendmentField.REQUIRED_LEVEL, "B2", "C1"),
        (SemanticCriterionAmendmentField.REQUIRED_LEVEL, "C1", "C2"),
    ]
    (record,) = provenance.criteria
    assert (record.origin.required_level, record.final.required_level) == ("B2", "C2")


async def test_modality_amendment_is_recorded_as_a_criterion_type_change() -> None:
    draft = _follow(await _draft("Python tələb olunur."), "Python-u üstünlük et")
    (criterion,) = _criteria(draft)
    assert criterion.type == CriterionType.PREFERRED
    provenance = build_agent_semantic_provenance(draft, [criterion])
    (amendment,) = provenance.amendments
    assert (amendment.field, amendment.previous_value, amendment.new_value) == (
        SemanticCriterionAmendmentField.CRITERION_TYPE,
        "MUST_HAVE",
        "PREFERRED",
    )


async def test_followup_value_outside_criterion_bounds_is_not_applied() -> None:
    draft = await _draft(PYTHON_3Y)
    assert _apply_pending_draft_followup(draft, "3 ili 500 et") is None


async def test_d_tampered_final_value_without_amendment_fails_closed() -> None:
    draft = await _draft(PYTHON_3Y)
    (criterion,) = _criteria(draft)
    tampered = _replace_criterion(draft, criterion, min_years=5)
    with pytest.raises(SemanticProvenanceError):
        build_agent_semantic_provenance(tampered, _criteria(tampered))
    level = await _draft(ENGLISH_B2)
    (english,) = _criteria(level)
    forged_level = _replace_criterion(level, english, required_level="C1")
    with pytest.raises(SemanticProvenanceError):
        build_agent_semantic_provenance(forged_level, _criteria(forged_level))
    modality = await _draft("Python tələb olunur.")
    (python,) = _criteria(modality)
    forged_type = _replace_criterion(modality, python, type="PREFERRED")
    forged_type = forged_type.model_copy(
        update={"must_have": [], "preferred": _criteria(forged_type)}
    )
    with pytest.raises(SemanticProvenanceError):
        build_agent_semantic_provenance(forged_type, _criteria(forged_type))


async def test_e_broken_amendment_chain_fails_closed() -> None:
    draft = _follow(await _draft(PYTHON_3Y), "3 ili 5 et")
    (criterion,) = _criteria(draft)
    span_id = draft.amendments[0].span_id
    forged = _replace_criterion(draft, criterion, min_years=7).model_copy(
        update={
            "amendments": [
                *draft.amendments,
                _amendment(
                    draft,
                    sequence=2,
                    criterion_id=criterion.id,
                    span_id=span_id,
                    field=SemanticCriterionAmendmentField.MIN_YEARS,
                    previous="4",
                    new="7",
                ),
            ]
        }
    )
    with pytest.raises(SemanticProvenanceError):
        build_agent_semantic_provenance(forged, _criteria(forged))


@pytest.mark.parametrize("mutation", ["duplicate_sequence", "gap", "reordered"])
async def test_amendment_sequence_must_be_one_ordered_chain(mutation: str) -> None:
    draft = _follow(_follow(await _draft(PYTHON_3Y), "3 ili 5 et"), "5 ili 7 et")
    first, second = draft.amendments
    chains = {
        "duplicate_sequence": [first, second.model_copy(update={"sequence": 1})],
        "gap": [first, second.model_copy(update={"sequence": 3})],
        "reordered": [second.model_copy(update={"sequence": 1}), first.model_copy(
            update={"sequence": 2}
        )],
    }
    forged = draft.model_copy(update={"amendments": chains[mutation]})
    with pytest.raises(SemanticProvenanceError):
        build_agent_semantic_provenance(forged, _criteria(forged))


async def test_f_cross_criterion_or_cross_span_amendment_fails_closed() -> None:
    draft = await _draft("Python üzrə minimum 3 il təcrübə tələb olunur. SQL tələb olunur.")
    python = next(c for c in _criteria(draft) if c.value == "Python")
    sql = next(c for c in _criteria(draft) if c.value == "SQL")
    sql_span = next(r.span_id for r in draft.requirements if r.criterion_id == sql.id)
    python_span = next(r.span_id for r in draft.requirements if r.criterion_id == python.id)
    amended = _replace_criterion(draft, python, min_years=5)
    crossings = ((python.id, sql_span), (sql.id, python_span), ("kotlin", python_span))
    for criterion_id, span_id in crossings:
        forged = amended.model_copy(
            update={
                "amendments": [
                    _amendment(
                        amended,
                        sequence=1,
                        criterion_id=criterion_id,
                        span_id=span_id,
                        field=SemanticCriterionAmendmentField.MIN_YEARS,
                        previous="3",
                        new="5",
                    )
                ]
            }
        )
        with pytest.raises(SemanticProvenanceError):
            build_agent_semantic_provenance(forged, _criteria(forged))


@pytest.mark.parametrize(
    "update",
    [
        {"field": "WEIGHT"},
        {"new_value": "5 years"},
        {"new_value": "5.0"},
        {"previous_value": "5"},  # no-op transition
        {"source_text": ""},
        {"source_sha256": "0" * 64},
        {"criterion_id": "Not Valid"},
    ],
)
def test_amendment_schema_rejects_unknown_field_malformed_value_or_missing_source(
    update: dict,
) -> None:
    payload = {
        "sequence": 1,
        "criterion_id": "python",
        "span_id": "req-0001",
        "field": "MIN_YEARS",
        "previous_value": "3",
        "new_value": "5",
        "source_text": "3 ili 5 et",
        "source_sha256": hashlib.sha256(b"3 ili 5 et").hexdigest(),
        **update,
    }
    with pytest.raises(ValidationError):
        SemanticCriterionAmendment.model_validate(payload)


async def test_forged_source_text_or_digest_fails_closed() -> None:
    draft = _follow(await _draft(PYTHON_3Y), "3 ili 5 et")
    forged_text = draft.model_copy(
        update={"source_jd_text": PYTHON_3Y.replace("3 il", "5 il")}
    )
    with pytest.raises(SemanticProvenanceError):
        build_agent_semantic_provenance(forged_text, _criteria(forged_text))
    missing = draft.model_copy(update={"source_jd_text": None})
    with pytest.raises(SemanticProvenanceError):
        build_agent_semantic_provenance(missing, _criteria(missing))


async def test_persisted_provenance_with_broken_replay_is_rejected_on_read() -> None:
    draft = _follow(_follow(await _draft(PYTHON_3Y), "3 ili 5 et"), "5 ili 7 et")
    payload = build_agent_semantic_provenance(draft, _criteria(draft)).model_dump(mode="json")
    for mutate in (
        lambda p: p["amendments"].pop(0),
        lambda p: p["amendments"][1].update(previous_value="4"),
        lambda p: p["criteria"][0]["final"].update(min_years=9),
        lambda p: p["criteria"][0]["origin"].update(min_years=5),
        lambda p: p["amendments"][0].update(criterion_id="kotlin"),
    ):
        broken = parse_agent_semantic_provenance(payload).model_dump(mode="json")  # type: ignore[union-attr]
        mutate(broken)
        with pytest.raises(ValidationError):
            parse_agent_semantic_provenance(broken)


# ============================================================================
# Blocker 2 — canonical collision authority
# ============================================================================


def test_identity_rule_is_kind_plus_normalized_subject() -> None:
    skill = JDDraftCriterionKind.SKILL
    assert semantic_identity(skill, "Postgres") == semantic_identity(skill, "PostgreSQL")
    assert semantic_identity(JDDraftCriterionKind.LANGUAGE, "ingilis dili") == semantic_identity(
        JDDraftCriterionKind.LANGUAGE, "English"
    )
    assert semantic_identity(
        JDDraftCriterionKind.DOMAIN_EXPERIENCE, "bank"
    ) == semantic_identity(JDDraftCriterionKind.DOMAIN_EXPERIENCE, "Banking")
    # Different evaluator semantics stay distinct.
    assert semantic_identity(skill, "Python") != semantic_identity(
        JDDraftCriterionKind.SKILL_EXPERIENCE, "Python"
    )


async def test_1_exact_duplicate_is_one_criterion_with_two_supporting_spans() -> None:
    draft = await _draft("Python required. Python required.")
    assert [c.value for c in _criteria(draft)] == ["Python"]
    assert [r.criterion_id for r in draft.requirements] == ["python", "python"]
    assert not agent_draft_requires_resolution(draft)
    provenance = build_agent_semantic_provenance(draft, _criteria(draft))
    (record,) = provenance.criteria
    assert [s.span_id for s in record.source_spans] == ["req-0001", "req-0002"]


async def test_2_conflicting_modality_is_blocking_review_never_two_weights() -> None:
    draft = await _draft("Python required. Python preferred.")
    assert _criteria(draft) == []
    (review,) = draft.needs_review
    assert review.blocking and review.conflict_span_ids == ["req-0001", "req-0002"]
    assert [o.criterion_type for o in review.conflict_options] == [
        CriterionType.MUST_HAVE,
        CriterionType.PREFERRED,
    ]
    assert agent_draft_requires_resolution(draft)
    # Neither value is ever chosen automatically; the modality resolver and
    # an out-of-range option are refused.
    with pytest.raises(ValueError):
        resolve_job_draft_review_modality(
            draft, span_id="req-0001", criterion_type=CriterionType.MUST_HAVE
        )
    with pytest.raises(ValueError):
        resolve_job_draft_semantic_conflict(draft, span_id="req-0001", option_index=2)

    resolved = resolve_job_draft_semantic_conflict(draft, span_id="req-0001", option_index=1)
    assert [(c.value, c.type) for c in _criteria(resolved)] == [
        ("Python", CriterionType.PREFERRED)
    ]
    assert not agent_draft_requires_resolution(resolved)
    provenance = build_agent_semantic_provenance(resolved, _criteria(resolved))
    (record,) = provenance.criteria
    assert [s.span_id for s in record.source_spans] == ["req-0001", "req-0002"]
    (choice,) = provenance.conflict_resolutions
    assert choice.chosen.criterion_type == CriterionType.PREFERRED
    assert record.origin == choice.chosen


async def test_conflict_choice_outside_source_options_fails_closed() -> None:
    draft = await _draft("Python required. Python preferred.")
    resolved = resolve_job_draft_semantic_conflict(draft, span_id="req-0001", option_index=0)
    (choice,) = resolved.conflict_resolutions
    forged_option = choice.model_copy(
        update={
            "options": [
                choice.options[0],
                choice.options[1].model_copy(update={"min_years": 9.0}),
            ]
        }
    )
    forged = resolved.model_copy(update={"conflict_resolutions": [forged_option]})
    with pytest.raises(SemanticProvenanceError):
        build_agent_semantic_provenance(forged, _criteria(forged))


async def test_conflict_can_be_explicitly_excluded_without_a_criterion() -> None:
    draft = await _draft("Python required. Python preferred.")
    excluded = exclude_blocking_review_requirement(draft, span_id="req-0001")
    assert _criteria(excluded) == [] and not agent_draft_requires_resolution(excluded)


async def test_3_alias_duplicates_are_one_canonical_criterion() -> None:
    draft = await _draft("Postgres required. PostgreSQL required.")
    assert [c.value for c in _criteria(draft)] == ["PostgreSQL"]
    (record,) = build_agent_semantic_provenance(draft, _criteria(draft)).criteria
    assert len(record.source_spans) == 2


async def test_4_conflicting_duration_is_explicit_review() -> None:
    draft = await _draft("5 years Python required. 3 years Python required.")
    assert _criteria(draft) == []
    (review,) = draft.needs_review
    assert [o.min_years for o in review.conflict_options] == [5.0, 3.0]
    assert all(
        r.state == RequirementSpanState.NEEDS_HUMAN_REVIEW for r in draft.requirements
    )
    resolved = resolve_job_draft_semantic_conflict(draft, span_id="req-0001", option_index=1)
    assert [c.min_years for c in _criteria(resolved)] == [3.0]
    build_agent_semantic_provenance(resolved, _criteria(resolved))


async def test_5_conflicting_language_level_is_explicit_review() -> None:
    draft = await _draft("English B2 required. English C1 required.")
    assert _criteria(draft) == []
    (review,) = draft.needs_review
    assert [o.required_level for o in review.conflict_options] == ["B2", "C1"]


async def test_6_coordination_plus_repeat_scores_each_subject_once() -> None:
    draft = await _draft("Python and PostgreSQL required. Python required.")
    assert sorted(c.value for c in _criteria(draft)) == ["PostgreSQL", "Python"]
    provenance = build_agent_semantic_provenance(draft, _criteria(draft))
    spans = {r.criterion_id: len(r.source_spans) for r in provenance.criteria}
    assert spans == {"python": 2, "postgresql": 1}


async def test_distinct_evaluator_semantics_stay_distinct() -> None:
    draft = await _draft("Python required. 5 years Python experience required.")
    assert sorted((c.kind.value, c.min_years or 0) for c in _criteria(draft)) == [
        ("SKILL", 0),
        ("SKILL_EXPERIENCE", 5.0),
    ]
    assert draft.needs_review == []


async def test_review_resolution_merges_into_an_existing_identical_criterion() -> None:
    draft = await _draft("Python required.\n- Python")
    (review,) = draft.needs_review
    with pytest.raises(ValueError):
        resolve_job_draft_review_modality(
            draft, span_id=review.span_id or "", criterion_type=CriterionType.PREFERRED
        )
    merged = resolve_job_draft_review_modality(
        draft, span_id=review.span_id or "", criterion_type=CriterionType.MUST_HAVE
    )
    assert [c.value for c in _criteria(merged)] == ["Python"]
    (record,) = build_agent_semantic_provenance(merged, _criteria(merged)).criteria
    assert len(record.source_spans) == 2


async def test_two_confirmed_criteria_with_one_identity_fail_closed() -> None:
    draft = await _draft("Python required. SQL required.")
    python, sql = _criteria(draft)
    forged = _replace_criterion(draft, sql, label="Python", value="Python")
    with pytest.raises(SemanticProvenanceError):
        build_agent_semantic_provenance(forged, _criteria(forged))


def _score(criteria: list[CriterionIn], profile: CandidateProfileExtraction) -> tuple:
    results = [evaluate_criterion(c, profile, evaluation_as_of_date=AS_OF) for c in criteria]
    band = compute_overall_result(results)
    score, _ = score_results(
        results,
        fit_band=band,
        evaluation_id=uuid.uuid4(),
        candidate_profile_version_id=uuid.uuid4(),
        job_criteria_version_id=uuid.uuid4(),
        evaluation_as_of_date=AS_OF,
        evaluation_policy_version="test",
    )
    return band, score


async def test_7_repetition_cannot_double_weight_or_shift_the_preferred_ratio() -> None:
    once = await _draft("Kotlin required. Python preferred. SQL preferred.")
    repeated = await _draft(
        "Kotlin required. Python preferred. Python preferred. Python preferred. SQL preferred."
    )
    assert [c.model_dump() for c in _criteria(repeated)] == [
        c.model_dump() for c in _criteria(once)
    ]
    evidence = [{"page": 1, "block_index": 0, "quote": "Synthetic: Kotlin, Python"}]
    profile = CandidateProfileExtraction.model_validate(
        {
            "skills": [
                {"name": "Kotlin", "category": None, "evidence": evidence},
                {"name": "Python", "category": None, "evidence": evidence},
            ]
        }
    )
    assert _score(_criteria(repeated), profile) == _score(_criteria(once), profile)
    band, score = _score(_criteria(repeated), profile)
    assert score < Decimal("100")  # SQL is still unproven; Python counts once


# ============================================================================
# HTTP confirm boundary
# ============================================================================


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


async def _version(db_session: AsyncSession, tenant_id: uuid.UUID):
    from meyar.models.job_criteria_version import JobCriteriaVersion
    from meyar.services.job_criteria_repo import get_agent_semantic_provenance

    version = (
        await db_session.execute(
            select(JobCriteriaVersion).where(JobCriteriaVersion.tenant_id == tenant_id)
        )
    ).scalar_one()
    provenance = await get_agent_semantic_provenance(
        db_session, tenant_id=tenant_id, criteria_version_id=version.id
    )
    return version, provenance


async def test_http_followup_amendment_is_persisted_with_the_version(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    tenant, user, password, _membership = tenant_and_user
    tenant_id = tenant.id
    await db_session.commit()
    app.dependency_overrides[get_llm_provider] = lambda: _UNAVAILABLE
    csrf = await _login_and_csrf(client, user.username, password)
    first = await client.post(
        "/ui/agent", data={"message": f"Vakansiya: Backend\n{PYTHON_3Y}", "csrf_token": csrf}
    )
    assert first.status_code == 200
    amended = await client.post("/ui/agent", data={"message": "3 ili 5 et", "csrf_token": csrf})
    assert amended.status_code == 200
    confirm = re.findall(r'action="(/ui/agent/drafts/[0-9a-f-]+/confirm)"', amended.text)
    assert confirm
    confirmed = await client.post(confirm[-1], data={"csrf_token": csrf})
    assert confirmed.status_code == 200
    version, provenance = await _version(db_session, tenant_id)
    assert [(c["value"], c["min_years"]) for c in version.criteria] == [("Python", 5.0)]
    assert provenance is not None
    (amendment,) = provenance.amendments
    assert (amendment.previous_value, amendment.new_value, amendment.source_text) == (
        "3",
        "5",
        "3 ili 5 et",
    )
    (record,) = provenance.criteria
    assert "3 il" in record.source_spans[0].source_text
    assert (record.origin.min_years, record.final.min_years) == (3.0, 5.0)


async def test_http_semantic_conflict_blocks_until_explicit_choice(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    from meyar.models.audit_event import AuditEvent

    tenant, user, password, _membership = tenant_and_user
    tenant_id = tenant.id
    await db_session.commit()
    app.dependency_overrides[get_llm_provider] = lambda: _UNAVAILABLE
    csrf = await _login_and_csrf(client, user.username, password)
    page = await client.post(
        "/ui/agent",
        data={
            "message": "Vakansiya: Backend\nPython tələb olunur.\nPython üstünlükdür.",
            "csrf_token": csrf,
        },
    )
    assert page.status_code == 200
    assert "fərqli əhəmiyyət və ya parametrlərlə göstərilib" in page.text
    assert "Tələbləri təsdiqlə və namizədləri sırala" not in page.text
    resolve_path = re.search(r'action="(/ui/agent/drafts/[0-9a-f-]+/resolve)"', page.text)
    span = re.search(r'name="span_id" value="(req-\d{4})"', page.text)
    assert resolve_path is not None and span is not None
    confirm_path = resolve_path.group(1).replace("/resolve", "/confirm")
    assert (await client.post(confirm_path, data={"csrf_token": csrf})).status_code == 422
    ambiguous = await client.post(
        resolve_path.group(1),
        data={"csrf_token": csrf, "span_id": span.group(1), "option": "0", "decision": "exclude"},
    )
    assert ambiguous.status_code == 422
    chosen = await client.post(
        resolve_path.group(1),
        data={"csrf_token": csrf, "span_id": span.group(1), "option": "1"},
    )
    assert chosen.status_code == 200
    assert "Tələbləri təsdiqlə və namizədləri sırala" in chosen.text
    confirmed = await client.post(confirm_path, data={"csrf_token": csrf})
    assert confirmed.status_code == 200
    version, provenance = await _version(db_session, tenant_id)
    assert [(c["value"], c["type"]) for c in version.criteria] == [("Python", "PREFERRED")]
    assert provenance is not None
    (choice,) = provenance.conflict_resolutions
    assert choice.chosen.criterion_type == CriterionType.PREFERRED
    assert len(provenance.criteria[0].source_spans) == 2
    event = (
        await db_session.execute(
            select(AuditEvent).where(
                AuditEvent.tenant_id == tenant_id,
                AuditEvent.event_type == "agent.draft.review_resolved",
            )
        )
    ).scalar_one()
    assert event.event_metadata["criterion_type"] == "CONFLICT_RESOLVED"
    assert "Python" not in str(event.event_metadata)


def test_semantic_conflict_is_a_non_liftable_review_reason() -> None:
    from meyar.agent.canonical_requirements import LIFTABLE_REVIEW_REASONS

    assert SemanticReviewReason.SEMANTIC_CONFLICT not in LIFTABLE_REVIEW_REASONS
