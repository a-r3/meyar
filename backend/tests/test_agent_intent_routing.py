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
