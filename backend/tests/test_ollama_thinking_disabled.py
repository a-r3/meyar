"""D-039 regression: hybrid-thinking models (qwen3) default to emitting a
hidden chain-of-thought before the schema-constrained JSON, multiplying real
Ollama latency roughly 5x on this project's target 8GB dev hardware — enough
to blow past the 60s request timeout on a cold model load and turn a real
agent turn into an outright AGENT_PROVIDER_FAILURE (found via real-Ollama
Slice 2 acceptance testing, PR #40). Every prompt in this module already
forbids chain-of-thought output, so thinking is pure waste; every
OllamaLLMProvider._chat call must disable it explicitly rather than rely on
the model's default."""

import json

import httpx

from meyar.llm.ollama_provider import OllamaLLMProvider


async def test_agent_decision_request_disables_thinking() -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "qwen3:1.7b",
                "message": {
                    "content": json.dumps({"action": "SEARCH_CANDIDATES", "search_query": "Java"})
                },
            },
        )

    provider = OllamaLLMProvider(
        base_url="http://127.0.0.1:11434",
        model="qwen3:1.7b",
        timeout_seconds=2,
        transport=httpx.MockTransport(handler),
    )

    await provider.decide_agent_action(
        recent_turns=[("user", "Java bilən namizədləri göstər")],
        last_tool_result_summary=None,
        available_candidate_refs=[],
    )

    assert captured["think"] is False


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
