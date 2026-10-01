"""Issue #88 slice A x #85: the clarification classifier runs outside every
DB transaction, through the real OllamaLLMProvider + shared admission gate,
and its wire prompt carries only the answer text, type and closed codes
(D-092 §12.1, §15). Gated in-memory transport; never a real Ollama."""

import asyncio
import json
import re

import httpx
from conftest import BrowserTestClient as AsyncClient
from httpx import ASGITransport
from sqlalchemy import select
from test_agent_inference_boundary import (
    UNRELATED_ROUTE_BUDGET_SECONDS,
    GatedOllama,
    _install,
    _login,
    _new_conversation,
    _spawn,
    _timed,
    small_pool,  # noqa: F401 - pytest fixture re-export
)

from meyar.config import Settings
from meyar.main import app
from meyar.models.agent_conversation import AgentConversation
from meyar.models.agent_task import AgentClarification

SOURCE = "Python mütləqdir."
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class _RecordingGate(GatedOllama):
    def __init__(self, decision: dict) -> None:
        super().__init__(decision)
        self.payloads: list[dict] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.payloads.append(json.loads(request.content))
        return await super().handler(request)


async def test_classifier_holds_no_db_connection_and_projects_only_answer(
    small_pool, tenant_and_user  # noqa: F811
) -> None:
    _engine, factory, probe = small_pool
    _tenant, user, password, _membership = tenant_and_user
    gate = _RecordingGate({"value": "UNCLEAR"})
    _install(Settings(ui_cookie_secure=False, inference_concurrency=1), gate)
    observed: dict[str, int] = {}

    def _observe() -> None:
        observed["checked_out"] = probe.checked_out

    gate.on_enter = _observe
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        csrf = await _login(client, user.username, password)
        conversation_id = await _new_conversation(client, csrf)
        form = {"csrf_token": csrf, "conversation_id": str(conversation_id)}
        created = await client.post("/ui/agent", data={**form, "message": SOURCE})
        assert created.status_code == 200
        assert gate.calls == 0  # clarification creation is deterministic
        baseline = probe.checked_out
        turn = _spawn(client.post("/ui/agent", data={**form, "message": "hmm"}))
        await asyncio.wait_for(gate.entered.wait(), timeout=10)
        assert observed["checked_out"] == baseline
        library, elapsed = await _timed(client.get("/ui/library"))
        assert library.status_code == 200 and elapsed < UNRELATED_ROUTE_BUDGET_SECONDS
        gate.release.set()
        response = await asyncio.wait_for(turn, timeout=10)
    assert response.status_code == 200
    (payload,) = gate.payloads
    prompt = json.dumps(payload["messages"], ensure_ascii=False)
    assert "hmm" in prompt and "SEARCH_OR_VACANCY" in prompt
    assert SOURCE not in prompt and "Python" not in prompt
    assert _UUID_RE.search(prompt) is None
    async with factory() as db:
        rows = list(
            (
                await db.scalars(
                    select(AgentClarification).order_by(AgentClarification.attempt)
                )
            ).all()
        )
        assert [(row.attempt, row.status) for row in rows] == [(1, "SUPERSEDED"), (2, "OPEN")]
        stored = await db.get(AgentConversation, conversation_id)
        assert stored is not None and len(stored.turns) == 4
