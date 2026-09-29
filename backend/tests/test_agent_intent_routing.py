"""Issue #79 — server-owned unified-composer entry routing."""

import pytest

from meyar.agent.intent_routing import (
    ENTRY_ROUTING_POLICY_VERSION,
    AgentEntryRoute,
    AgentRoutedAction,
    AgentRoutingSource,
    route_agent_entry,
)


def test_routing_policy_version_is_v3() -> None:
    assert ENTRY_ROUTING_POLICY_VERSION == "agent-entry-routing-v3"


# Issue #79 PR81 visual-acceptance correction: an explicit, unambiguous NEW
# candidate search is decided by the server, never by the orchestration model
# (the real local model had routed the first message below to refinement).
@pytest.mark.parametrize(
    "message",
    [
        "Python bilən namizədləri göstər",
        "5 il Python təcrübəsi olan 10 namizəd tap",
        "Python və SQL bilən 5 namizəd göstər",
        "Python required, SQL preferred olan namizədləri göstər",
        "Find candidates with Python and SQL",
        "Show candidates with at least 5 years of Java",
        "Mənə Python bilən namizədləri göstərin, zəhmət olmasa.",
    ],
)
def test_explicit_new_search_is_forced(message: str) -> None:
    routing = route_agent_entry(message)
    assert routing.route == AgentEntryRoute.FORCE_CANDIDATE_SEARCH
    assert routing.routing_source == AgentRoutingSource.DETERMINISTIC_SEARCH
    assert routing.routed_action == AgentRoutedAction.SEARCH_CANDIDATES


@pytest.mark.parametrize(
    "message",
    [
        "bunlardan SQL bilənlər",
        "ikinci namizəd",
        "yalnız bunların içində Python bilənlər",
        "yalnız bunların içində Python bilənləri göstər",
        "bunlardan SQL bilənləri göstər",
        "bunlardan Python bilən ilk 3 nəfəri göstər",
        "bunlardan ilk 3",
    ],
)
def test_current_result_followups_win_over_new_search(message: str) -> None:
    assert route_agent_entry(message).route == AgentEntryRoute.MODEL_ROUTED


@pytest.mark.parametrize(
    ("message", "limit"),
    [
        ("ilk 3", 3),
        ("ilk üçü", 3),
        ("ilk üçü göstər", 3),
        ("ilk 3 namizədi göstər", 3),
        ("ilk beşi", 5),
        ("first 3", 3),
        ("top 5", 5),
        ("5 nəfərə endir", 5),
    ],
)
def test_count_only_followup_is_server_routed_limit(message: str, limit: int) -> None:
    routing = route_agent_entry(message)
    assert routing.route == AgentEntryRoute.FORCE_RESULT_LIMIT
    assert routing.routing_source == AgentRoutingSource.DETERMINISTIC_RESULT_CONTEXT
    assert routing.routed_action == AgentRoutedAction.REFINE_CANDIDATE_RESULTS
    assert routing.result_limit == limit


@pytest.mark.parametrize("message", ["ilk 99", "ilk iş yeri", "ilk 0"])
def test_out_of_range_or_non_count_ilk_is_not_forced(message: str) -> None:
    assert route_agent_entry(message).route != AgentEntryRoute.FORCE_RESULT_LIMIT


@pytest.mark.parametrize(
    "message",
    [
        # Inflected (non-imperative) verbs inside requirement sentences are
        # never a search command.
        "Namizəd liderlik bacarığı göstərməlidir.",
        "Candidates must show ownership. Python required.",
        # Bare candidate noun with no search imperative.
        "Namizəd haqqında danış",
    ],
)
def test_non_imperative_search_vocabulary_is_not_forced_search(message: str) -> None:
    assert route_agent_entry(message).route != AgentEntryRoute.FORCE_CANDIDATE_SEARCH


def test_oversized_search_text_is_not_forced() -> None:
    message = "Python bilən. " * 160 + "Python bilən namizədləri göstər"
    assert route_agent_entry(message).route != AgentEntryRoute.FORCE_CANDIDATE_SEARCH


@pytest.mark.parametrize(
    ("message", "expected_source"),
    [
        ("Vakansiyanı analiz et: Python tələb olunur.", "Python tələb olunur."),
        (
            "Bu vakansiya elanını analiz et:\n"
            "Senior Backend Engineer.\n"
            "Python tələb olunur.\n"
            "SQL üstünlükdür.",
            "Senior Backend Engineer.\nPython tələb olunur.\nSQL üstünlükdür.",
        ),
        (
            "Bu vakansiya elanını analiz et\nPython tələb olunur.\nSQL üstünlükdür.  ",
            "Python tələb olunur.\nSQL üstünlükdür.",
        ),
    ],
)
def test_job_analysis_wrapper_is_sliced_to_exact_user_source(
    message: str, expected_source: str
) -> None:
    routing = route_agent_entry(message)
    assert routing.route == AgentEntryRoute.FORCE_JOB_DRAFT
    source = routing.draft_source(message)
    assert source == expected_source
    # Exact substring by offsets, never rewritten text.
    assert routing.draft_source_start is not None
    assert message[routing.draft_source_start : routing.draft_source_end] == source
    assert "analiz et" not in source


@pytest.mark.parametrize(
    "message",
    [
        "Vakansiya: Backend Mühəndisi\nPython tələb olunur.\nSQL üstünlükdür.",
        "Vakansiya: Senior Backend Mühəndisi\nPython tələb olunur.\n"
        "PostgreSQL üstünlükdür.\nİngilis dili B2 tələb olunur.",
    ],
)
def test_structural_vacancy_header_keeps_full_source(message: str) -> None:
    routing = route_agent_entry(message)
    assert routing.route == AgentEntryRoute.FORCE_JOB_DRAFT
    assert routing.draft_source(message) == message


@pytest.mark.parametrize(
    "message",
    [
        "bu vakansiyaya uyğun namizədləri qiymətləndir",
        "Bu vakansiyaya uyğun namizədləri qiymətləndir.",
        "Vakansiyanı analiz et:",
        "Vakansiyanı analiz et:   \n  ",
    ],
)
def test_job_analysis_command_without_source_requires_source(message: str) -> None:
    routing = route_agent_entry(message)
    assert routing.route == AgentEntryRoute.CLARIFY_JOB_SOURCE_REQUIRED
    assert routing.routing_source == AgentRoutingSource.DETERMINISTIC_CLARIFICATION


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
        # Case B (explicit vacancy-scoped evaluate action with no source)
        # is now CLARIFY_JOB_SOURCE_REQUIRED — see the test above.
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
        # Current-ResultSet / ordinal follow-up language must never be
        # captured by any JD detector.
        "bunlardan SQL bilənlər",
        "ikinci namizəd",
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
