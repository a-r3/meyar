"""Issue #88 slice A — route-level acceptance items of the D-092 §22 test
plan over the real ``POST /ui/agent`` path:

* item 21 — M-8 language matrix (AZ / EN / mixed original x button / label /
  model answer) resumes with the ORIGINAL source;
* item 9  — replay matrix (clarification-creating and vacancy-producing
  COMPLETED submissions replayed with the same server token);
* item 11 — a new BrowserSession cannot answer another session's
  clarification, even with its exact id;
* item 15a — lane-B review-resolve of D1 rewrites the transcript in place
  and leaves the lane-A clarification answerable.

Synthetic data only; the local model is the deterministic FakeLLMProvider."""

import uuid

import pytest
from conftest import BrowserTestClient as AsyncClient
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession
from test_issue88_slice_a_routes import (
    REJECTED_COPY,
    SOURCE,
    _clarifications,
    _context,
    _conversation,
    _events,
    _fake,
    _open,
    _rows,
    _say,
    _search_hash,
)
from test_ui_agent_routes import (
    _hidden_value,
    _login_and_csrf,
    local_ui_settings,  # noqa: F401 - pytest fixture re-export
)

from meyar.agent.clarification_schemas import ClarificationProposalValue
from meyar.config import Settings
from meyar.llm.provider import ModelUnavailableError
from meyar.main import app
from meyar.models.agent_conversation import AgentConversationSessionContext
from meyar.models.agent_result_set import AgentResultSet
from meyar.models.agent_task import AgentTask
from meyar.search import planner_service

# --- item 21: M-8 language matrix ---------------------------------------------

ORIGINALS = {
    "AZ": "Python mütləqdir.",
    "EN": "Python is mandatory.",
    "MIXED": "Python mandatory-dir.",
}
LABELS = {"AZ": "namizəd axtarışı", "EN": "candidate search", "MIXED": "namizəd search"}
MODEL_ANSWERS = {"AZ": "bəli", "EN": "yes", "MIXED": "bəli, yes"}
ALLOWED = ["CANDIDATE_SEARCH", "VACANCY_ANALYSIS"]


@pytest.mark.parametrize("answer_kind", ["BUTTON", "LABEL", "MODEL"])
@pytest.mark.parametrize("language", list(ORIGINALS))
async def test_language_matrix_resumes_with_the_original_source(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user, monkeypatch,
    local_ui_settings: Settings, language: str, answer_kind: str,  # noqa: F811
) -> None:
    tenant, user, password, _m = tenant_and_user
    tenant_id = tenant.id
    original = ORIGINALS[language]
    planned: list[str] = []
    real_plan = planner_service.plan_candidate_search

    async def spy(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        # Every request the resumed search planner/search pipeline receives.
        planned.append(kwargs["natural_language_request"])
        return await real_plan(*args, **kwargs)

    monkeypatch.setattr(planner_service, "plan_candidate_search", spy)
    fake = _fake(clarification_proposals=[ClarificationProposalValue.CANDIDATE_SEARCH])
    csrf, conversation_id = await _open(client, user, password, fake)
    created = await _say(client, csrf, conversation_id, original)
    assert created.status_code == 200
    (clarification,) = await _clarifications(db_session, tenant_id)
    # The original is the bound source: exact span and hash.
    assert clarification.clarification_type == "SEARCH_OR_VACANCY"
    assert (clarification.source_start, clarification.source_end) == (0, len(original))
    assert clarification.source_sha256 == _search_hash(original)
    source_turn = (await _conversation(db_session, conversation_id)).turns[-2]
    assert str(clarification.source_turn_id) == source_turn["turn_id"]
    assert source_turn["text"] == original
    assert planned == []  # T1 never searched

    if answer_kind == "BUTTON":
        answer_text = "Namizəd axtarışı"
        extra = {
            "clarification_id": str(clarification.id),
            "clarification_choice": "CANDIDATE_SEARCH",
        }
    else:
        answer_text = LABELS[language] if answer_kind == "LABEL" else MODEL_ANSWERS[language]
        extra = {}
    answer = await _say(client, csrf, conversation_id, answer_text, **extra)
    assert answer.status_code == 200
    await db_session.refresh(clarification)
    assert (clarification.status, clarification.resolved_value) == (
        "RESOLVED", "CANDIDATE_SEARCH",
    )
    assert clarification.resolution_source == answer_kind
    assert (await _context(db_session, conversation_id)).active_clarification_id is None
    # The resumed planner/search received the ORIGINAL — never the answer.
    assert planned == [original]
    assert all(answer_text not in request for request in fake.planner_requests)
    assert all(request == original for request in fake.planner_requests)
    (result_set,) = await _rows(db_session, AgentResultSet, tenant_id=tenant_id)
    assert result_set.request_sha256 == _search_hash(original)
    assert result_set.request_sha256 == clarification.source_sha256
    if answer_kind == "MODEL":
        # §15: ONLY the answer text, the type and the closed codes.
        assert fake.clarification_calls == [
            ("SEARCH_OR_VACANCY", ALLOWED, answer_text, False)
        ]
    else:
        assert fake.clarification_calls == []
    assert fake.agent_call_count == 0


# --- item 9: replay matrix -----------------------------------------------------


async def _state(db: AsyncSession, tenant_id, conversation_id: str) -> dict:  # noqa: ANN001
    conversation = await _conversation(db, conversation_id)
    context = await _context(db, conversation_id)
    return {
        "turns": [(turn.get("turn_id"), turn["role"]) for turn in conversation.turns],
        "turn_version": conversation.turn_version,
        "pointer": context.active_clarification_id,
        "pending_draft": context.active_pending_draft_id,
        "result_set": context.active_result_set_id,
        "tasks": [
            (row.id, row.status, row.pending_draft_id)
            for row in await _rows(db, AgentTask, tenant_id=tenant_id)
        ],
        "clarifications": [
            (row.id, row.status, row.attempt)
            for row in await _clarifications(db, tenant_id)
        ],
        "result_sets": len(await _rows(db, AgentResultSet, tenant_id=tenant_id)),
        "resolved": len(await _events(db, tenant_id, "agent.clarification.resolved")),
        "created": len(await _events(db, tenant_id, "agent.clarification.created")),
        "task_created": len(await _events(db, tenant_id, "agent.task.created")),
        "task_changed": len(await _events(db, tenant_id, "agent.task.state_changed")),
    }


async def test_replay_of_completed_clarification_creating_submission_is_inert(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    tenant, user, password, _m = tenant_and_user
    tenant_id = tenant.id
    fake = _fake()
    csrf, conversation_id = await _open(client, user, password, fake)
    page = await client.get(f"/ui/agent?conversation={conversation_id}")
    data = {
        "message": SOURCE, "csrf_token": csrf, "conversation_id": conversation_id,
        "submission_id": _hidden_value(page.text, "submission_id"),
    }
    assert (await client.post("/ui/agent", data=data)).status_code == 200
    completed = await _state(db_session, tenant_id, conversation_id)
    assert len(completed["tasks"]) == 1 and len(completed["clarifications"]) == 1
    assert completed["pointer"] == completed["clarifications"][0][0]
    assert len(completed["turns"]) == 2
    assert (completed["created"], completed["task_created"]) == (1, 1)

    replay = await client.post("/ui/agent", data=data, follow_redirects=False)
    assert replay.status_code == 303
    # No second task, clarification, transcript pair; same live pointer.
    assert await _state(db_session, tenant_id, conversation_id) == completed


async def test_replay_of_completed_vacancy_resolving_submission_is_inert(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    tenant, user, password, _m = tenant_and_user
    tenant_id = tenant.id
    fake = _fake()
    csrf, conversation_id = await _open(client, user, password, fake)
    await _say(client, csrf, conversation_id, SOURCE)
    (clarification,) = await _clarifications(db_session, tenant_id)
    page = await client.get(f"/ui/agent?conversation={conversation_id}")
    data = {
        "message": "Vakansiya tələbi kimi", "csrf_token": csrf,
        "conversation_id": conversation_id,
        "submission_id": _hidden_value(page.text, "submission_id"),
        "clarification_id": str(clarification.id),
        "clarification_choice": "VACANCY_ANALYSIS",
    }
    assert (await client.post("/ui/agent", data=data)).status_code == 200
    completed = await _state(db_session, tenant_id, conversation_id)
    assert completed["pending_draft"] is not None
    (task,) = completed["tasks"]
    assert task[1:] == ("WAITING_CONFIRMATION", completed["pending_draft"])
    assert completed["resolved"] == 1 and len(completed["turns"]) == 4
    assert fake.jd_draft_call_count == 1

    replay = await client.post("/ui/agent", data=data, follow_redirects=False)
    assert replay.status_code == 303
    # No duplicate draft, WAITING_CONFIRMATION task, resolution or pair.
    assert await _state(db_session, tenant_id, conversation_id) == completed
    assert fake.jd_draft_call_count == 1


# --- item 11: cross-session button ----------------------------------------------


async def test_new_browser_session_cannot_answer_another_sessions_clarification(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    tenant, user, password, _m = tenant_and_user
    tenant_id = tenant.id
    fake = _fake(clarification_proposals=[ClarificationProposalValue.CANDIDATE_SEARCH])
    csrf, conversation_id = await _open(client, user, password, fake)
    await _say(client, csrf, conversation_id, SOURCE)
    (foreign,) = await _clarifications(db_session, tenant_id)
    foreign_id = foreign.id
    session_a_context = await _context(db_session, conversation_id)
    session_a_context_id = session_a_context.id
    before = await _conversation(db_session, conversation_id)
    before_turns, before_version = list(before.turns), before.turn_version

    responses = {}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as other:
        other_csrf = await _login_and_csrf(other, user.username, password)
        page = await other.get(f"/ui/agent?conversation={conversation_id}")
        assert page.status_code == 200
        # B has no live pointer, so no buttons and no clarification id.
        assert 'name="clarification_choice"' not in page.text
        assert str(foreign_id) not in page.text
        for case, posted in (("foreign", foreign_id), ("random", uuid.uuid4())):
            responses[case] = await _say(
                other, other_csrf, conversation_id, "Namizəd axtarışı",
                clarification_id=str(posted), clarification_choice="CANDIDATE_SEARCH",
            )
    # Session A's exact id is indistinguishable from a random one.
    for response in responses.values():
        assert response.status_code == 409
        assert REJECTED_COPY in response.text
        assert str(foreign_id) not in response.text
        assert 'name="clarification_choice"' not in response.text
    rejected = await _events(db_session, tenant_id, "agent.clarification.rejected")
    assert [event.event_metadata for event in rejected] == [{"reason_code": "NOT_ACTIVE"}] * 2

    # Foreign clarification, A's pointer and the transcript are untouched;
    # no capability ran.
    await db_session.refresh(foreign)
    assert (foreign.status, foreign.attempt, foreign.resolved_by_submission_id) == (
        "OPEN", 1, None,
    )
    contexts = await _rows(
        db_session, AgentConversationSessionContext,
        conversation_id=uuid.UUID(conversation_id),
    )
    by_id = {context.id: context for context in contexts}
    assert by_id[session_a_context_id].active_clarification_id == foreign_id
    # B holds no live clarification pointer (its rejected turns committed
    # nothing, so it may not even own a context row yet).
    assert all(
        context.active_clarification_id is None
        for context in contexts if context.id != session_a_context_id
    )
    after = await _conversation(db_session, conversation_id)
    assert after.turns == before_turns and after.turn_version == before_version
    assert await _rows(db_session, AgentResultSet, tenant_id=tenant_id) == []
    assert fake.planner_requests == [] and fake.clarification_calls == []
    (task,) = await _rows(db_session, AgentTask, tenant_id=tenant_id)
    assert task.status == "WAITING_CLARIFICATION"
    # Session A can still answer its own question.
    assert (await _say(client, csrf, conversation_id, "namizəd axtarışı")).status_code == 200
    await db_session.refresh(foreign)
    assert foreign.status == "RESOLVED"


# --- item 15a: lane-B review-resolve coexistence ----------------------------------

REVIEW_JD = "Vakansiya: Backend\nPython tələb olunur.\nKubernetes təcrübəsi mütləqdir."


async def test_lane_b_review_resolve_rewrites_in_place_and_lane_a_stays_answerable(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    import re

    tenant, user, password, _m = tenant_and_user
    tenant_id = tenant.id
    # Drafter outage -> the deterministic fallback leaves an unresolved
    # MUST_HAVE that requires HR review before confirmation.
    fake = _fake(jd_draft_error=ModelUnavailableError("synthetic outage"))
    csrf, conversation_id = await _open(client, user, password, fake)
    draft_page = await _say(client, csrf, conversation_id, REVIEW_JD)
    assert draft_page.status_code == 200
    resolve_path = re.search(r'action="(/ui/agent/drafts/[0-9a-f-]+/resolve)"', draft_page.text)
    span = re.search(r'name="span_id" value="(req-\d{4})"', draft_page.text)
    assert resolve_path is not None and span is not None
    d1 = (await _context(db_session, conversation_id)).active_pending_draft_id
    assert d1 is not None and str(d1) in resolve_path.group(1)
    (lane_b,) = await _rows(db_session, AgentTask, tenant_id=tenant_id)
    assert lane_b.status == "WAITING_CONFIRMATION"

    # Lane A opens while D1 waits.
    assert (await _say(client, csrf, conversation_id, SOURCE)).status_code == 200
    (clarification,) = await _clarifications(db_session, tenant_id)
    before = await _conversation(db_session, conversation_id)
    before_shape = [(turn.get("turn_id"), turn["role"]) for turn in before.turns]
    before_payload = next(
        turn["pending_job_draft"] for turn in before.turns if "pending_job_draft" in turn
    )
    before_version = before.turn_version

    # The REAL review route.
    resolved = await client.post(
        resolve_path.group(1),
        data={"csrf_token": csrf, "span_id": span.group(1), "decision": "exclude"},
    )
    assert resolved.status_code == 200
    after = await _conversation(db_session, conversation_id)
    # In place: same entries, ids, order and roles; the D1 payload changed;
    # nothing appended; turn_version bumped.
    assert [(turn.get("turn_id"), turn["role"]) for turn in after.turns] == before_shape
    after_payload = next(
        turn["pending_job_draft"] for turn in after.turns if "pending_job_draft" in turn
    )
    assert after_payload != before_payload
    assert after_payload["draft_id"] == str(d1)
    assert after.turn_version > before_version
    context = await _context(db_session, conversation_id)
    assert context.active_clarification_id == clarification.id
    assert context.active_pending_draft_id == d1
    await db_session.refresh(clarification)
    assert (clarification.status, clarification.attempt) == ("OPEN", 1)
    await db_session.refresh(lane_b)
    assert lane_b.status == "WAITING_CONFIRMATION"
    assert len(await _events(db_session, tenant_id, "agent.draft.review_resolved")) == 1

    # Lane A is still answerable afterwards and resumes with the original.
    resumed = await _say(client, csrf, conversation_id, "namizəd axtarışı")
    assert resumed.status_code == 200
    await db_session.refresh(clarification)
    assert (clarification.status, clarification.resolution_source) == ("RESOLVED", "LABEL")
    (result_set,) = await _rows(db_session, AgentResultSet, tenant_id=tenant_id)
    assert result_set.request_sha256 == _search_hash(SOURCE)
    # Lane B was never cancelled by the lane-A resolution.
    await db_session.refresh(lane_b)
    assert lane_b.status == "WAITING_CONFIRMATION"
    assert (await _context(db_session, conversation_id)).active_pending_draft_id == d1
