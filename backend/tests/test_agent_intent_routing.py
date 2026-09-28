"""Issue #79 — server-owned unified-composer entry routing."""

import pytest

from meyar.agent.intent_routing import AgentEntryRoute, route_agent_entry


@pytest.mark.parametrize(
    "message",
    [
        "Python bilən namizədləri göstər",
        "5 il Python təcrübəsi olan 10 namizəd tap",
        "Python və SQL bilən 5 namizəd göstər",
        "Python required, SQL preferred olan namizədləri göstər",
        "bunlardan SQL bilənlər",
        "ilk 3",
    ],
)
def test_candidate_search_and_result_context_cues_remain_model_routed(message: str) -> None:
    assert route_agent_entry(message).route == AgentEntryRoute.MODEL_ROUTED


@pytest.mark.parametrize(
    "message",
    [
        (
            "Bu vakansiya elanını analiz et:\n"
            "Senior Backend Engineer.\n"
            "Python mütləqdir.\n"
            "PostgreSQL üstünlükdür."
        ),
        (
            "Vakansiya: Senior Backend Mühəndisi.\n"
            "Python bilməlidir.\n"
            "İngilis dili B2 tələb olunur."
        ),
        "- Python required\n- SQL preferred",
        "Senior Backend Engineer\nPython required\nPostgreSQL preferred",
    ],
)
def test_explicit_or_structurally_strong_jd_is_forced(message: str) -> None:
    assert route_agent_entry(message).route == AgentEntryRoute.FORCE_JOB_DRAFT


@pytest.mark.parametrize(
    "message",
    [
        "Python required. SQL preferred.",
        "Python mütləqdir.",
    ],
)
def test_requirement_shaped_text_without_search_or_jd_structure_clarifies(message: str) -> None:
    assert route_agent_entry(message).route == AgentEntryRoute.CLARIFY_AMBIGUOUS


def test_explicit_job_analysis_beats_candidate_terms_inside_the_source() -> None:
    message = (
        "Bu vakansiya elanını analiz et: Senior Backend Engineer. "
        "Python required. Sonda 5 namizəd göstər."
    )
    assert route_agent_entry(message).route == AgentEntryRoute.FORCE_JOB_DRAFT


def test_ordinary_conversation_stays_model_routed() -> None:
    assert route_agent_entry("Salam").route == AgentEntryRoute.MODEL_ROUTED


# Issue #79 PR81 independent-review correction: the deterministic router was
# too narrow for Azerbaijani case morphology on workflow nouns (vakansiya/
# elan/vəzifə) and too aggressive in treating a bare candidate noun
# ("namizəd") as search evidence. See intent_routing.py module docstring and
# the _AZ_CASE_SUFFIX/_JOB_ANALYSIS_ACTION_RE comments for the fix strategy.
@pytest.mark.parametrize(
    "message",
    [
        # Case A — Azerbaijani accusative case suffix on the workflow noun.
        "Vakansiyanı analiz et: Python tələb olunur.",
        # Same, plain-ASCII typing variance (no diacritic keys).
        "Vakansiyani analiz et: Python teleb olunur.",
        # Genitive case suffix.
        "Vakansiyanın tələblərini analiz et: Python mütləqdir.",
        # Case B — explicit vacancy-scoped evaluate action; must not be
        # stolen by the generic candidate-noun detector even though
        # "namizədləri" is present.
        "bu vakansiyaya uyğun namizədləri qiymətləndir",
        "Bu vakansiyaya uyğun namizədləri qiymətləndir.",
        # Case C — requirement-shaped two-line paste using "Namizəd" as a
        # subject noun; no find/show/list/search request present.
        "Namizəd Python bilməlidir.\nNamizəd SQL bilməlidir.",
        "Candidate must know Python.\nCandidate must have SQL experience.",
        # Structured multi-line JD paste with an explicit vacancy header.
        "Vakansiya: Backend Mühəndisi.\nPython tələb olunur.\nSQL üstünlükdür.",
    ],
)
def test_workflow_morphology_forces_job_draft(message: str) -> None:
    assert route_agent_entry(message).route == AgentEntryRoute.FORCE_JOB_DRAFT


@pytest.mark.parametrize(
    "message",
    [
        # A bare candidate/result noun with no requirement modality and no
        # search/result action is never, by itself, search evidence.
        "Namizəd haqqında danış.",
        "Tell me about the candidate.",
        # Explicit search action wins even when requirement terminology
        # ("required"/"preferred") and a candidate noun are both present.
        "Python required, SQL preferred olan namizədləri göstər",
        "Find candidates with Python and SQL",
        # Current-ResultSet / ordinal follow-up language must never be
        # captured by any JD detector.
        "bunlardan SQL bilənlər",
        "ilk 3",
        "ikinci namizəd",
        "Python bilən namizədləri göstər",
        "5 il Python təcrübəsi olan 10 namizəd tap",
    ],
)
def test_candidate_noun_alone_is_not_search_authority(message: str) -> None:
    assert route_agent_entry(message).route == AgentEntryRoute.MODEL_ROUTED


@pytest.mark.parametrize(
    "message",
    [
        # A bare candidate noun combined with requirement modality (but no
        # search action and no JD structure) is genuinely ambiguous — it
        # must clarify, not be silently claimed by either JD or search.
        "Namizəd yaxşı təcrübəyə malikdir.",
        "Candidate has strong experience.",
    ],
)
def test_candidate_noun_with_requirement_modality_alone_clarifies(message: str) -> None:
    assert route_agent_entry(message).route == AgentEntryRoute.CLARIFY_AMBIGUOUS
