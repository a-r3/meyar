"""D-039/D-040 regression: hybrid-thinking models (qwen3) default to
emitting a hidden chain-of-thought before the schema-constrained JSON,
multiplying real Ollama latency roughly 5x on this project's target 8GB
dev hardware — enough to blow past the 60s request timeout on a cold model
load and turn a real agent turn into an outright AGENT_PROVIDER_FAILURE
(found via real-Ollama Slice 2 acceptance testing, PR #40). Both agent
prompts (meyar.agent.prompts) already forbid chain-of-thought output, so
thinking is pure waste there — propose_agent_plan/select_grounded_facts
disable it explicitly.

D-039 originally disabled thinking for every OllamaLLMProvider._chat call.
Owner scope correction (D-040): that is outside Slice 2 and changes
previously-accepted extraction/identity/planner AI behavior without a
dedicated quality benchmark — those call sites must keep their exact
pre-D-039 request shape (no ``think`` key at all, i.e. the Ollama/model
default), unchanged from accepted ``main``."""

import json
import uuid

import httpx
import pytest
from pydantic import ValidationError

from meyar.config import Settings
from meyar.extraction.view import ModelInputBlock, ProfessionalDocumentView
from meyar.llm.ollama_provider import OllamaLLMProvider


def _view() -> ProfessionalDocumentView:
    return ProfessionalDocumentView(
        canonical_document_id=uuid.uuid4(),
        blocks=[ModelInputBlock(page=1, block_index=0, text="Skills: Python")],
    )


def test_request_serving_settings_reject_fake_llm_provider() -> None:
    with pytest.raises(ValidationError):
        Settings(llm_provider="fake")  # type: ignore[arg-type]


async def test_agent_plan_request_disables_thinking() -> None:
    from meyar.agent.capabilities.contracts import (
        AgentPlanContext,
        CapabilityName,
        ContextTurn,
        OfferedCapability,
    )

    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        plan = {
            "schema_version": "agent-plan-v1",
            "kind": "PLAN",
            "goal": "CANDIDATE_SEARCH",
            "steps": [
                {"capability": "SEARCH_CANDIDATES", "args": {"source": {"mode": "WHOLE_MESSAGE"}}}
            ],
        }
        return httpx.Response(
            200, json={"model": "qwen3:1.7b", "message": {"content": json.dumps(plan)}}
        )

    provider = OllamaLLMProvider(
        base_url="http://127.0.0.1:11434",
        model="qwen3:1.7b",
        timeout_seconds=2,
        transport=httpx.MockTransport(handler),
    )

    proposal, _provenance = await provider.propose_agent_plan(
        context=AgentPlanContext(
            recent_turns=[ContextTurn(role="user", text="Java bilən namizədləri göstər")],
            available_capabilities=[
                OfferedCapability(name=CapabilityName.SEARCH_CANDIDATES, description="Search.")
            ],
            max_plan_steps=3,
            active_result_context_present=False,
            available_candidate_refs=[],
            pending_vacancy_confirmation=False,
        )
    )

    assert captured["think"] is False
    assert proposal.steps[0].capability == CapabilityName.SEARCH_CANDIDATES
    # Per-call subset (D-092 §8.1): the constrained schema offers ONLY the
    # offered capability.
    schema = json.dumps(captured["format"])
    assert "SEARCH_CANDIDATES" in schema
    for name in CapabilityName:
        if name != CapabilityName.SEARCH_CANDIDATES:
            assert name.value not in schema


async def test_grounded_selection_request_disables_thinking() -> None:
    from meyar.agent.schemas import GroundedFact

    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "qwen3:1.7b",
                "message": {"content": json.dumps({"used_facts": [1], "caveat": None})},
            },
        )

    provider = OllamaLLMProvider(
        base_url="http://127.0.0.1:11434",
        model="qwen3:1.7b",
        timeout_seconds=2,
        transport=httpx.MockTransport(handler),
    )

    await provider.select_grounded_facts(
        question="Onun Python təcrübəsi neçə ildir?",
        facts=[GroundedFact(id=1, category="skills", title="Python", detail=None)],
    )

    assert captured["think"] is False


async def test_jd_drafting_uses_real_ollama_json_mode_not_fake_fallback() -> None:
    from meyar.agent.schemas import RequirementSpan

    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "qwen3:1.7b",
                "message": {
                    "content": json.dumps(
                        {
                            "title": "Backend",
                            "must_have": [
                                {
                                    "span_id": "req-0001",
                                    "kind": "SKILL_EXPERIENCE",
                                    "requirement": "Python",
                                    "min_years": 5,
                                }
                            ],
                            "preferred": [],
                        }
                    )
                },
            },
        )

    provider = OllamaLLMProvider(
        base_url="http://127.0.0.1:11434",
        model="qwen3:1.7b",
        timeout_seconds=2,
        transport=httpx.MockTransport(handler),
    )
    draft, provenance = await provider.draft_job_criteria(
        "Minimum 5 il Python",
        requirement_spans=[
            RequirementSpan(
                span_id="req-0001",
                start_offset=0,
                end_offset=19,
                text="Minimum 5 il Python",
                normalized="minimum 5 il python",
            )
        ],
    )
    assert captured["format"] == "json"
    assert captured["think"] is False
    assert draft.must_have[0].kind == "SKILL_EXPERIENCE"
    assert provenance.provider == "ollama"


async def test_profile_extraction_request_omits_thinking_field_entirely() -> None:
    """D-040: extraction must keep its exact pre-D-039 request shape —
    no ``think`` key at all, never an explicit True/False either."""
    captured: dict = {}
    empty_extraction = json.dumps(
        {
            "skills": [],
            "employment_history": [],
            "education": [],
            "certifications": [],
            "languages": [],
            "projects": [],
        }
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200, json={"model": "qwen3:0.6b", "message": {"content": empty_extraction}}
        )

    provider = OllamaLLMProvider(
        base_url="http://127.0.0.1:11434",
        model="qwen3:0.6b",
        timeout_seconds=2,
        transport=httpx.MockTransport(handler),
    )

    await provider.extract_candidate_profile(_view())

    assert "think" not in captured


async def test_identity_extraction_request_omits_thinking_field_entirely() -> None:
    captured: dict = {}
    empty_identity = json.dumps({"full_name": None, "email": None, "phone": None})

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200, json={"model": "qwen3:0.6b", "message": {"content": empty_identity}}
        )

    provider = OllamaLLMProvider(
        base_url="http://127.0.0.1:11434",
        model="qwen3:0.6b",
        timeout_seconds=2,
        transport=httpx.MockTransport(handler),
    )

    await provider.extract_candidate_identity(_view())

    assert "think" not in captured


async def test_planner_request_omits_thinking_field_entirely() -> None:
    captured: dict = {}
    empty_draft = json.dumps({})

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200, json={"model": "qwen3:0.6b", "message": {"content": empty_draft}}
        )

    provider = OllamaLLMProvider(
        base_url="http://127.0.0.1:11434",
        model="qwen3:0.6b",
        timeout_seconds=2,
        transport=httpx.MockTransport(handler),
    )

    await provider.plan_candidate_search("Java bilən namizədləri göstər")

    assert "think" not in captured
