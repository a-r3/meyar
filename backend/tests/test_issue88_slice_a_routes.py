"""Issue #88 slice A — resumable clarification over the real ``POST
/ui/agent`` path (D-092 + Amendments A1/A2). Synthetic data only; the local
model is the deterministic FakeLLMProvider."""

import hashlib
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import BrowserTestClient as AsyncClient
from fakes import FakeLLMProvider
from httpx import ASGITransport
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from test_ui_agent_routes import (
    _draft_confirm_path,
    _hidden_value,
    _login_and_csrf,
    _python_confirmation_data,
    local_ui_settings,  # noqa: F401 - pytest fixture re-export
)

from meyar.agent.clarification_schemas import ClarificationProposalValue
from meyar.agent.schemas import (
    AgentActionType,
    AgentDecision,
    AgentResponseCode,
    JDCriteriaDraft,
    JDDraftCriterionItem,
)
from meyar.config import Settings
from meyar.core.roles import ROLE_HR_USER
from meyar.llm.dependency import get_llm_provider
from meyar.llm.provider import (
    InferenceBusyError,
    ModelTimeoutError,
    ModelUnavailableError,
)
from meyar.main import app
from meyar.models.agent_conversation import AgentConversation, AgentConversationSessionContext
from meyar.models.agent_result_set import AgentResultSet
from meyar.models.agent_task import AgentClarification, AgentTask
from meyar.models.agent_turn_submission import AgentTurnSubmission
from meyar.models.audit_event import AuditEvent
from meyar.search.planner_schemas import PlannerDraft
from meyar.search.schemas import RequiredFilters
from meyar.services.agent_submission_repo import request_hash
from meyar.services.tenant_membership_repo import create_membership
from meyar.services.tenant_repo import create_tenant
from meyar.services.user_repo import create_user

SOURCE = "Python mütləqdir."
VSR_TRIGGER = "Vakansiyanı analiz et"
STALE_COPY = "Bu sual artıq aktiv deyil. Zəhmət olmasa əvvəlki sorğunu yenidən göndərin."
REJECTED_COPY = "Bu sual artıq aktiv deyil."


def _fake(**overrides) -> FakeLLMProvider:  # noqa: ANN003
    options: dict = {
        "agent_decision": AgentDecision(
            action=AgentActionType.FINAL_ANSWER, response_code=AgentResponseCode.GREETING
        ),
        "planner_draft": PlannerDraft(required_filters=RequiredFilters(skills=["Python"])),
        "jd_draft": JDCriteriaDraft(
            title="Backend",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001", kind="SKILL", requirement="Python",
                    source_text="Python mütləqdir",
                )
            ],
        ),
    }
    options.update(overrides)
    return FakeLLMProvider(**options)


async def _open(client: AsyncClient, user, password, fake: FakeLLMProvider):  # noqa: ANN001,ANN202
    app.dependency_overrides[get_llm_provider] = lambda: fake
    csrf = await _login_and_csrf(client, user.username, password)
    page = await client.get("/ui/agent")
    return csrf, _hidden_value(page.text, "conversation_id")


async def _say(client: AsyncClient, csrf: str, conversation_id: str, message: str, **extra):  # noqa: ANN003,ANN202
    return await client.post(
        "/ui/agent",
        data={"message": message, "csrf_token": csrf, "conversation_id": conversation_id, **extra},
    )


async def _rows(db: AsyncSession, model, **where):  # noqa: ANN001,ANN003,ANN202
    stmt = select(model).execution_options(populate_existing=True)
    for key, value in where.items():
        stmt = stmt.where(getattr(model, key) == value)
    if hasattr(model, "created_at"):
        stmt = stmt.order_by(model.created_at, model.id)
    return list((await db.scalars(stmt)).all())


async def _conversation(db: AsyncSession, conversation_id: str) -> AgentConversation:
    row = await db.scalar(
        select(AgentConversation)
        .where(AgentConversation.id == uuid.UUID(conversation_id))
        .execution_options(populate_existing=True)
    )
    assert row is not None
    return row


async def _context(db: AsyncSession, conversation_id: str) -> AgentConversationSessionContext:
    rows = await _rows(
        db, AgentConversationSessionContext, conversation_id=uuid.UUID(conversation_id)
    )
    assert len(rows) == 1
    return rows[0]


async def _clarifications(db: AsyncSession, tenant_id) -> list[AgentClarification]:  # noqa: ANN001
    return await _rows(db, AgentClarification, tenant_id=tenant_id)


async def _events(db: AsyncSession, tenant_id, event_type: str) -> list[AuditEvent]:  # noqa: ANN001
    return await _rows(db, AuditEvent, tenant_id=tenant_id, event_type=event_type)


def _turn_ids(conversation: AgentConversation) -> list[str | None]:
    return [turn.get("turn_id") for turn in conversation.turns]


def _search_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- A / B -----------------------------------------------------------------


async def test_a_new_entries_get_stable_server_turn_ids_preserved_by_display_sync(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    _tenant, user, password, _m = tenant_and_user
    csrf, conversation_id = await _open(client, user, password, _fake())
    assert (await _say(client, csrf, conversation_id, "Salam")).status_code == 200
    first = await _conversation(db_session, conversation_id)
    ids = _turn_ids(first)
    assert len(ids) == 2 and all(ids) and len(set(ids)) == 2
    for value in ids:
        uuid.UUID(str(value))
    # The D-045 display sync rewrote the assistant text in place: id, order
    # and role are preserved.
    assert [turn["role"] for turn in first.turns] == ["user", "assistant"]
    await client.get(f"/ui/agent?conversation={conversation_id}")
    assert (await _say(client, csrf, conversation_id, "Salam yenə")).status_code == 200
    second = await _conversation(db_session, conversation_id)
    assert _turn_ids(second)[:2] == ids
    assert len(set(_turn_ids(second))) == 4


async def test_b_legacy_entries_without_turn_id_are_never_backfilled_or_resumed(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    _tenant, user, password, _m = tenant_and_user
    tenant_id = _tenant.id
    fake = _fake()
    csrf, conversation_id = await _open(client, user, password, fake)
    conversation = await _conversation(db_session, conversation_id)
    legacy = [
        {"role": "user", "text": SOURCE},
        {"role": "assistant", "text": "köhnə sual", "outcome": "CLARIFICATION_REQUESTED"},
    ]
    conversation.turns = legacy
    await db_session.commit()
    assert (await _say(client, csrf, conversation_id, "namizəd axtarışı")).status_code == 200
    stored = await _conversation(db_session, conversation_id)
    assert stored.turns[:2] == legacy
    assert "turn_id" not in stored.turns[0] and "turn_id" not in stored.turns[1]
    # A legacy CLARIFICATION_REQUESTED turn is plain history: nothing resumes.
    assert await _clarifications(db_session, tenant_id) == []
    assert fake.clarification_calls == []
    assert fake.agent_call_count == 1


# --- E / G: SEARCH_OR_VACANCY attempt 1 --------------------------------------


@pytest.mark.parametrize("source_kind", ["LABEL", "BUTTON", "MODEL"])
async def test_e_search_or_vacancy_attempt1_resumes_original_source(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings, source_kind: str,  # noqa: F811
) -> None:
    tenant, user, password, _m = tenant_and_user
    tenant_id = tenant.id
    fake = _fake(clarification_proposals=[ClarificationProposalValue.CANDIDATE_SEARCH])
    csrf, conversation_id = await _open(client, user, password, fake)
    created = await _say(client, csrf, conversation_id, SOURCE)
    assert created.status_code == 200
    assert 'name="clarification_choice" value="CANDIDATE_SEARCH"' in created.text
    (clarification,) = await _clarifications(db_session, tenant_id)
    conversation = await _conversation(db_session, conversation_id)
    user_turn, question = conversation.turns[-2:]
    assert clarification.status == "OPEN" and clarification.attempt == 1
    assert clarification.clarification_type == "SEARCH_OR_VACANCY"
    assert str(clarification.source_turn_id) == user_turn["turn_id"]
    assert str(clarification.created_from_turn_id) == user_turn["turn_id"]
    assert str(clarification.question_turn_id) == question["turn_id"]
    assert (clarification.source_start, clarification.source_end) == (0, len(SOURCE))
    assert clarification.source_sha256 == _search_hash(SOURCE)
    assert question["clarification"] == {
        "question_code": "SEARCH_OR_VACANCY",
        "choices": ["CANDIDATE_SEARCH", "VACANCY_ANALYSIS"],
    }
    # A1: provenance = final committed turn_version after the D-045 sync.
    assert clarification.created_turn_version == conversation.turn_version
    context = await _context(db_session, conversation_id)
    assert context.active_clarification_id == clarification.id
    (task,) = await _rows(db_session, AgentTask, tenant_id=tenant_id)
    assert (task.task_type, task.status, task.phase) == (
        "UNDETERMINED", "WAITING_CLARIFICATION", "NEEDS_INTENT_CHOICE",
    )

    if source_kind == "LABEL":
        answer = await _say(client, csrf, conversation_id, "namizəd axtarışı")
    elif source_kind == "BUTTON":
        answer = await _say(
            client, csrf, conversation_id, "Namizəd axtarışı",
            clarification_id=str(clarification.id), clarification_choice="CANDIDATE_SEARCH",
        )
    else:
        answer = await _say(client, csrf, conversation_id, "bəli")
    assert answer.status_code == 200
    await db_session.refresh(clarification)
    await db_session.refresh(task)
    assert clarification.status == "RESOLVED"
    assert clarification.resolved_value == "CANDIDATE_SEARCH"
    assert clarification.resolution_source == source_kind
    assert clarification.resolved_by_submission_id is not None
    assert (task.task_type, task.status) == ("CANDIDATE_SEARCH", "COMPLETED")
    context = await _context(db_session, conversation_id)
    assert context.active_clarification_id is None
    # G: the planner received the ORIGINAL bound source, never the answer.
    (result_set,) = await _rows(db_session, AgentResultSet, tenant_id=tenant_id)
    assert result_set.request_sha256 == _search_hash(SOURCE)
    assert context.active_result_set_id == result_set.id
    if source_kind == "MODEL":
        # §15: the classifier saw only the answer text, type and closed codes.
        assert fake.clarification_calls == [
            ("SEARCH_OR_VACANCY", ["CANDIDATE_SEARCH", "VACANCY_ANALYSIS"], "bəli", False)
        ]
    else:
        assert fake.clarification_calls == []
    assert fake.agent_call_count == 0


# --- F / G / R: SEARCH_OR_VACANCY retry chain ---------------------------------


async def test_f_g_search_or_vacancy_unclear_builds_exact_a2_chain_and_keeps_u1_source(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    tenant, user, password, _m = tenant_and_user
    tenant_id = tenant.id
    fake = _fake(clarification_proposals=[ClarificationProposalValue.UNCLEAR])
    csrf, conversation_id = await _open(client, user, password, fake)
    await _say(client, csrf, conversation_id, SOURCE)
    retry = await _say(client, csrf, conversation_id, "hmm")
    assert retry.status_code == 200
    assert 'name="clarification_choice" value="VACANCY_ANALYSIS"' in retry.text
    previous, current = await _clarifications(db_session, tenant_id)
    conversation = await _conversation(db_session, conversation_id)
    u1, q1, u2, q2 = conversation.turns[-4:]
    assert [u1["role"], q1["role"], u2["role"], q2["role"]] == [
        "user", "assistant", "user", "assistant",
    ]
    assert (previous.status, previous.superseded_reason) == ("SUPERSEDED", "UNCLEAR")
    assert previous.superseded_by_id == current.id
    assert (previous.attempt, current.attempt) == (1, 2)
    assert current.status == "OPEN"
    assert previous.task_id == current.task_id
    assert previous.session_context_id == current.session_context_id
    assert previous.clarification_type == current.clarification_type == "SEARCH_OR_VACANCY"
    assert str(previous.created_from_turn_id) == u1["turn_id"]
    assert str(previous.question_turn_id) == q1["turn_id"]
    assert str(current.created_from_turn_id) == u2["turn_id"]
    assert str(current.question_turn_id) == q2["turn_id"]
    # The original source binding never moves to the unclear answer U2.
    for field in ("source_turn_id", "source_sha256", "source_start", "source_end"):
        assert getattr(current, field) == getattr(previous, field)
    assert str(current.source_turn_id) == u1["turn_id"]
    (task,) = await _rows(db_session, AgentTask, tenant_id=tenant_id)
    assert task.status == "WAITING_CLARIFICATION"
    assert (await _context(db_session, conversation_id)).active_clarification_id == current.id

    resumed = await _say(client, csrf, conversation_id, "namizəd axtarışı")
    assert resumed.status_code == 200
    await db_session.refresh(current)
    assert (current.status, current.resolution_source) == ("RESOLVED", "LABEL")
    (result_set,) = await _rows(db_session, AgentResultSet, tenant_id=tenant_id)
    assert result_set.request_sha256 == _search_hash(SOURCE)


async def test_r_search_or_vacancy_second_unclear_expires_without_attempt_three(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    tenant, user, password, _m = tenant_and_user
    tenant_id = tenant.id
    fake = _fake(clarification_proposals=[ClarificationProposalValue.UNCLEAR])
    csrf, conversation_id = await _open(client, user, password, fake)
    await _say(client, csrf, conversation_id, SOURCE)
    await _say(client, csrf, conversation_id, "hmm")
    final = await _say(client, csrf, conversation_id, "hmm yenə")
    assert final.status_code == 200
    assert "daha aydın ifadə" in final.text
    rows = await _clarifications(db_session, tenant_id)
    assert [row.attempt for row in rows] == [1, 2]
    assert rows[1].status == "EXPIRED"
    (task,) = await _rows(db_session, AgentTask, tenant_id=tenant_id)
    assert task.status == "FAILED_SAFE" and task.terminal_at is not None
    assert (await _context(db_session, conversation_id)).active_clarification_id is None
    assert await _rows(db_session, AgentResultSet, tenant_id=tenant_id) == []
    expired = await _events(db_session, tenant_id, "agent.clarification.expired")
    assert [event.event_metadata for event in expired] == [{"reason_code": "ATTEMPTS"}]


# --- H / I / J: VACANCY_SOURCE_REQUIRED ---------------------------------------


async def test_h_i_r_vacancy_source_required_chain_is_deterministic_and_bounded(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    tenant, user, password, _m = tenant_and_user
    tenant_id = tenant.id
    fake = _fake()
    csrf, conversation_id = await _open(client, user, password, fake)
    first = await _say(client, csrf, conversation_id, VSR_TRIGGER)
    assert first.status_code == 200
    assert 'name="clarification_choice"' not in first.text
    (attempt1,) = await _clarifications(db_session, tenant_id)
    conversation = await _conversation(db_session, conversation_id)
    u0, q1 = conversation.turns[-2:]
    assert attempt1.clarification_type == "VACANCY_SOURCE_REQUIRED"
    assert (
        attempt1.source_turn_id, attempt1.source_sha256,
        attempt1.source_start, attempt1.source_end,
    ) == (None, None, None, None)
    # U0 is sequencing provenance only, never JD source.
    assert str(attempt1.created_from_turn_id) == u0["turn_id"]
    assert str(attempt1.question_turn_id) == q1["turn_id"]
    (task,) = await _rows(db_session, AgentTask, tenant_id=tenant_id)
    assert (task.task_type, task.phase) == ("VACANCY_ANALYSIS", "NEEDS_SOURCE")

    await _say(client, csrf, conversation_id, "hmm")
    previous, current = await _clarifications(db_session, tenant_id)
    conversation = await _conversation(db_session, conversation_id)
    u0b, q1b, u2, q2 = conversation.turns[-4:]
    assert (u0b["turn_id"], q1b["turn_id"]) == (u0["turn_id"], q1["turn_id"])
    assert (previous.status, previous.superseded_reason) == ("SUPERSEDED", "UNCLEAR")
    assert previous.superseded_by_id == current.id and current.attempt == 2
    assert str(current.created_from_turn_id) == u2["turn_id"]
    assert str(current.question_turn_id) == q2["turn_id"]
    assert current.source_turn_id is None and current.source_sha256 is None

    await _say(client, csrf, conversation_id, "hmm yenə")
    rows = await _clarifications(db_session, tenant_id)
    assert len(rows) == 2 and rows[1].status == "EXPIRED"
    await db_session.refresh(task)
    assert task.status == "FAILED_SAFE"
    # VACANCY_SOURCE_REQUIRED never calls the model classifier, and an
    # unclear answer never reaches the JD drafter.
    assert fake.clarification_calls == [] and fake.jd_draft_call_count == 0


async def test_j_vacancy_source_required_resolves_with_source_message(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    tenant, user, password, _m = tenant_and_user
    tenant_id = tenant.id
    fake = _fake()
    csrf, conversation_id = await _open(client, user, password, fake)
    await _say(client, csrf, conversation_id, VSR_TRIGGER)
    await _say(client, csrf, conversation_id, "hmm")
    response = await _say(client, csrf, conversation_id, SOURCE)
    assert response.status_code == 200
    previous, current = await _clarifications(db_session, tenant_id)
    assert (current.status, current.resolved_value, current.resolution_source) == (
        "RESOLVED", "VACANCY_ANALYSIS", "SOURCE_MESSAGE",
    )
    assert current.source_turn_id is None
    # Only the qualifying CURRENT message is the JD source — never U0 or U2.
    assert fake.last_jd_text == SOURCE
    context = await _context(db_session, conversation_id)
    assert context.active_clarification_id is None
    assert context.active_pending_draft_id is not None
    (task,) = await _rows(db_session, AgentTask, tenant_id=tenant_id)
    assert (task.status, task.pending_draft_id) == (
        "WAITING_CONFIRMATION", context.active_pending_draft_id,
    )


# --- K: classifier infrastructure failure -----------------------------------


@pytest.mark.parametrize(
    "fake_options",
    [
        {"clarification_error": ModelTimeoutError("t")},
        {"clarification_error": ModelUnavailableError("u")},
        {"clarification_fail_first_n_calls": 2},
        {"clarification_error": InferenceBusyError("QUEUE_FULL")},
    ],
    ids=["timeout", "unavailable", "malformed-after-repair", "busy"],
)
async def test_k_classifier_failure_consumes_no_attempt_and_appends_nothing(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings, fake_options: dict,  # noqa: F811
) -> None:
    tenant, user, password, _m = tenant_and_user
    tenant_id = tenant.id
    fake = _fake(**fake_options)
    csrf, conversation_id = await _open(client, user, password, fake)
    await _say(client, csrf, conversation_id, SOURCE)
    before = await _conversation(db_session, conversation_id)
    before_turns, before_version = list(before.turns), before.turn_version
    (clarification,) = await _clarifications(db_session, tenant_id)
    clarification_id = clarification.id
    page = await client.get(f"/ui/agent?conversation={conversation_id}")
    token = _hidden_value(page.text, "submission_id")
    response = await _say(client, csrf, conversation_id, "bəli", submission_id=token)
    assert response.status_code == 503
    after = await _conversation(db_session, conversation_id)
    assert after.turns == before_turns and after.turn_version == before_version
    assert [row.id for row in await _clarifications(db_session, tenant_id)] == [clarification_id]
    await db_session.refresh(clarification)
    assert (clarification.status, clarification.attempt) == ("OPEN", 1)
    (task,) = await _rows(db_session, AgentTask, tenant_id=tenant_id)
    assert task.status == "WAITING_CLARIFICATION"
    assert (await _context(db_session, conversation_id)).active_clarification_id == (
        clarification_id
    )
    submission = await db_session.get(
        AgentTurnSubmission, uuid.UUID(token), populate_existing=True
    )
    assert submission is not None and submission.status == "ABANDONED"
    # Still answerable on retry.
    assert (await _say(client, csrf, conversation_id, "namizəd axtarışı")).status_code == 200
    await db_session.refresh(clarification)
    assert clarification.status == "RESOLVED"


# --- L / M / N / S: rejected buttons, closed reasons, request hash ----------


@pytest.mark.parametrize("case", ["foreign-id", "invalid-choice", "superseded-id", "vsr-choice"])
async def test_l_stale_foreign_or_mismatched_button_is_rejected_without_transition(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings, case: str,  # noqa: F811
) -> None:
    tenant, user, password, _m = tenant_and_user
    tenant_id = tenant.id
    fake = _fake(clarification_proposals=[ClarificationProposalValue.UNCLEAR])
    csrf, conversation_id = await _open(client, user, password, fake)
    await _say(client, csrf, conversation_id, VSR_TRIGGER if case == "vsr-choice" else SOURCE)
    if case == "superseded-id":
        await _say(client, csrf, conversation_id, "hmm")
    rows = await _clarifications(db_session, tenant_id)
    live = rows[-1]
    live_id = live.id
    before_rows = [(row.id, row.status, row.attempt) for row in rows]
    posted_id, choice, reason = {
        "foreign-id": (uuid.uuid4(), "CANDIDATE_SEARCH", "NOT_ACTIVE"),
        "invalid-choice": (live.id, "DELETE_CANDIDATE", "INVALID_CHOICE"),
        "superseded-id": (rows[0].id, "CANDIDATE_SEARCH", "NOT_ACTIVE"),
        "vsr-choice": (live.id, "VACANCY_ANALYSIS", "INVALID_CHOICE"),
    }[case]
    before = await _conversation(db_session, conversation_id)
    before_turns = list(before.turns)
    page = await client.get(f"/ui/agent?conversation={conversation_id}")
    token = _hidden_value(page.text, "submission_id")
    response = await _say(
        client, csrf, conversation_id, "Namizəd axtarışı", submission_id=token,
        clarification_id=str(posted_id), clarification_choice=choice,
    )
    assert response.status_code == 409
    assert REJECTED_COPY in response.text
    assert (await _conversation(db_session, conversation_id)).turns == before_turns
    after_rows = await _clarifications(db_session, tenant_id)
    assert [(row.id, row.status, row.attempt) for row in after_rows] == before_rows
    assert (await _context(db_session, conversation_id)).active_clarification_id == live_id
    (event,) = await _events(db_session, tenant_id, "agent.clarification.rejected")
    assert event.event_metadata == {"reason_code": reason}
    submission = await db_session.get(
        AgentTurnSubmission, uuid.UUID(token), populate_existing=True
    )
    assert submission is not None and submission.status == "ABANDONED"
    assert await _rows(db_session, AgentResultSet, tenant_id=tenant_id) == []
    # A fresh token is rendered for the retry.
    assert _hidden_value(response.text, "submission_id") != token


async def test_m_superseded_reason_admits_only_real_transitions(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    tenant, user, password, _m = tenant_and_user
    tenant_id = tenant.id
    csrf, conversation_id = await _open(client, user, password, _fake())
    await _say(client, csrf, conversation_id, SOURCE)
    (clarification,) = await _clarifications(db_session, tenant_id)
    for status, reason in (
        ("SUPERSEDED", "NOT_ACTIVE_BUTTON"),
        ("SUPERSEDED", "NOT_ACTIVE"),
        ("SUPERSEDED", "INVALID_CHOICE"),
        ("SUPERSEDED", None),
        ("OPEN", "UNCLEAR"),
    ):
        clarification.status = status
        clarification.superseded_reason = reason
        with pytest.raises(DBAPIError):
            await db_session.flush()
        await db_session.rollback()
        clarification = (await _clarifications(db_session, tenant_id))[0]


async def test_n_request_hash_covers_clarification_fields_and_altered_replay_fails_closed(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    clarification_id = uuid.uuid4()
    base = request_hash("Namizəd axtarışı")
    with_id = request_hash("Namizəd axtarışı", clarification_id=clarification_id)
    search = request_hash(
        "Namizəd axtarışı", clarification_id=clarification_id,
        clarification_choice="CANDIDATE_SEARCH",
    )
    vacancy = request_hash(
        "Namizəd axtarışı", clarification_id=clarification_id,
        clarification_choice="VACANCY_ANALYSIS",
    )
    other_id = request_hash(
        "Namizəd axtarışı", clarification_id=uuid.uuid4(),
        clarification_choice="CANDIDATE_SEARCH",
    )
    assert len({base, with_id, search, vacancy, other_id}) == 5
    assert search == request_hash(
        "Namizəd axtarışı", clarification_id=clarification_id,
        clarification_choice="CANDIDATE_SEARCH",
    )

    tenant, user, password, _m = tenant_and_user
    tenant_id = tenant.id
    fake = _fake()
    csrf, conversation_id = await _open(client, user, password, fake)
    await _say(client, csrf, conversation_id, SOURCE)
    (clarification,) = await _clarifications(db_session, tenant_id)
    page = await client.get(f"/ui/agent?conversation={conversation_id}")
    token = _hidden_value(page.text, "submission_id")
    data = {
        "message": "Namizəd axtarışı", "csrf_token": csrf, "conversation_id": conversation_id,
        "submission_id": token, "clarification_id": str(clarification.id),
        "clarification_choice": "CANDIDATE_SEARCH",
    }
    assert (await client.post("/ui/agent", data=data)).status_code == 200
    altered = await client.post(
        "/ui/agent", data={**data, "clarification_choice": "VACANCY_ANALYSIS"},
        follow_redirects=False,
    )
    assert altered.status_code == 404
    assert fake.jd_draft_call_count == 0
    assert len(await _rows(db_session, AgentResultSet, tenant_id=tenant_id)) == 1


# --- O: replay / concurrency --------------------------------------------------


async def test_o_replayed_or_second_answer_cannot_advance_a_clarification_twice(
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
    token = _hidden_value(page.text, "submission_id")
    data = {
        "message": "Namizəd axtarışı", "csrf_token": csrf, "conversation_id": conversation_id,
        "submission_id": token, "clarification_id": str(clarification.id),
        "clarification_choice": "CANDIDATE_SEARCH",
    }
    assert (await client.post("/ui/agent", data=data)).status_code == 200
    clarification_id = clarification.id
    replay = await client.post("/ui/agent", data=data, follow_redirects=False)
    assert replay.status_code == 303
    # Another tab with a NEW token presses the same (now resolved) button.
    second = await _say(
        client, csrf, conversation_id, "Namizəd axtarışı",
        clarification_id=str(clarification_id), clarification_choice="CANDIDATE_SEARCH",
    )
    assert second.status_code == 409
    await db_session.refresh(clarification)
    assert clarification.status == "RESOLVED"
    resolved = await _events(db_session, tenant_id, "agent.clarification.resolved")
    assert len(resolved) == 1
    assert len(await _rows(db_session, AgentResultSet, tenant_id=tenant_id)) == 1
    # Schema backstop: one resolving submission resolves at most one row.
    duplicate = AgentClarification(
        **{
            column.name: getattr(clarification, column.name)
            for column in AgentClarification.__table__.columns
            if column.name not in {"id", "created_by_submission_id"}
        },
        id=uuid.uuid4(),
        created_by_submission_id=uuid.uuid4(),
    )
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_o_in_flight_turn_blocks_a_second_answer_without_transition(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    tenant, user, password, _m = tenant_and_user
    tenant_id = tenant.id
    csrf, conversation_id = await _open(client, user, password, _fake())
    await _say(client, csrf, conversation_id, SOURCE)
    (clarification,) = await _clarifications(db_session, tenant_id)
    conversation = await _conversation(db_session, conversation_id)
    conversation.active_turn_id = uuid.uuid4()
    conversation.active_turn_expires_at = datetime.now(UTC) + timedelta(minutes=1)
    await db_session.commit()
    response = await _say(client, csrf, conversation_id, "namizəd axtarışı")
    assert response.status_code == 409
    await db_session.refresh(clarification)
    assert clarification.status == "OPEN"


# --- C / D / P / Q: lanes and liveness ----------------------------------------


async def _draft_d1(client: AsyncClient, csrf: str, conversation_id: str):  # noqa: ANN202
    response = await _say(
        client, csrf, conversation_id, "Analyze this job description:\nBackend.\nPython required."
    )
    assert response.status_code == 200
    return response


def _python_required_fake(**overrides) -> FakeLLMProvider:  # noqa: ANN003
    return _fake(
        jd_draft=JDCriteriaDraft(
            title="Backend",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001", kind="SKILL", requirement="Python",
                    source_text="Python required",
                )
            ],
        ),
        **overrides,
    )


async def test_c_p_lane_b_confirm_bumps_turn_version_but_lane_a_stays_answerable(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    tenant, user, password, _m = tenant_and_user
    tenant_id = tenant.id
    fake = _python_required_fake()
    csrf, conversation_id = await _open(client, user, password, fake)
    draft_page = await _draft_d1(client, csrf, conversation_id)
    context = await _context(db_session, conversation_id)
    d1 = context.active_pending_draft_id
    assert d1 is not None
    (lane_b,) = await _rows(db_session, AgentTask, tenant_id=tenant_id)
    assert (lane_b.status, lane_b.pending_draft_id) == ("WAITING_CONFIRMATION", d1)

    # P: a lane-A clarification while D1 waits; lane B untouched.
    await _say(client, csrf, conversation_id, SOURCE)
    (clarification,) = await _clarifications(db_session, tenant_id)
    context = await _context(db_session, conversation_id)
    assert context.active_pending_draft_id == d1
    await db_session.refresh(lane_b)
    assert lane_b.status == "WAITING_CONFIRMATION"

    # C: lane-B confirm rewrites the transcript in place and bumps
    # turn_version; it never appends, so lane A stays live.
    version_before = (await _conversation(db_session, conversation_id)).turn_version
    confirm = await client.post(
        _draft_confirm_path(draft_page.text),
        data=_python_confirmation_data(draft_page.text, csrf),
        follow_redirects=False,
    )
    assert confirm.status_code in (200, 303)
    conversation = await _conversation(db_session, conversation_id)
    assert conversation.turn_version > version_before
    await db_session.refresh(lane_b)
    assert lane_b.status == "COMPLETED"
    await db_session.refresh(clarification)
    assert clarification.status == "OPEN"
    assert (await _context(db_session, conversation_id)).active_clarification_id == (
        clarification.id
    )
    # P: `et` must not route this answer to the draft-amendment branch.
    resumed = await _say(client, csrf, conversation_id, "namizəd axtarışı et")
    assert resumed.status_code == 200
    await db_session.refresh(clarification)
    assert (clarification.status, clarification.resolution_source) == ("RESOLVED", "LABEL")


async def test_q_resolved_vacancy_replaces_only_lane_b_t11(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    tenant, user, password, _m = tenant_and_user
    tenant_id = tenant.id
    fake = _python_required_fake()
    csrf, conversation_id = await _open(client, user, password, fake)
    await _draft_d1(client, csrf, conversation_id)
    d1 = (await _context(db_session, conversation_id)).active_pending_draft_id
    (lane_b,) = await _rows(db_session, AgentTask, tenant_id=tenant_id)
    fake._jd_draft = JDCriteriaDraft(
        title="Backend",
        must_have=[
            JDDraftCriterionItem(
                span_id="req-0001", kind="SKILL", requirement="Python",
                source_text="Python mütləqdir",
            )
        ],
    )
    await _say(client, csrf, conversation_id, SOURCE)
    (clarification,) = await _clarifications(db_session, tenant_id)
    response = await _say(client, csrf, conversation_id, "vakansiya kimi")
    assert response.status_code == 200
    context = await _context(db_session, conversation_id)
    d2 = context.active_pending_draft_id
    assert d2 is not None and d2 != d1
    assert fake.last_jd_text == SOURCE
    await db_session.refresh(lane_b)
    assert (lane_b.status, lane_b.pending_draft_id) == ("CANCELLED", None)
    tasks = await _rows(db_session, AgentTask, tenant_id=tenant_id)
    resumed = [task for task in tasks if task.id == clarification.task_id][0]
    assert (resumed.task_type, resumed.status, resumed.pending_draft_id) == (
        "VACANCY_ANALYSIS", "WAITING_CONFIRMATION", d2,
    )
    changed = await _events(db_session, tenant_id, "agent.task.state_changed")
    assert {"from_status": "WAITING_CONFIRMATION", "to_status": "CANCELLED",
            "reason_code": "DRAFT_REPLACED"} in [event.event_metadata for event in changed]


async def test_q_forced_new_draft_cancels_old_lane_b_task_only(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    tenant, user, password, _m = tenant_and_user
    tenant_id = tenant.id
    fake = _python_required_fake()
    csrf, conversation_id = await _open(client, user, password, fake)
    await _draft_d1(client, csrf, conversation_id)
    # A replacing JD without the frozen amendment tokens ("required", "et").
    fake._jd_draft = JDCriteriaDraft(
        title="Backend",
        must_have=[
            JDDraftCriterionItem(
                span_id="req-0001", kind="SKILL", requirement="Python",
                source_text="Python mandatory",
            )
        ],
    )
    replaced = await _say(
        client, csrf, conversation_id, "Analyze this job description:\nBackend.\nPython mandatory."
    )
    assert replaced.status_code == 200
    first, second = await _rows(db_session, AgentTask, tenant_id=tenant_id)
    context = await _context(db_session, conversation_id)
    assert first.status == "CANCELLED"
    assert (second.status, second.pending_draft_id) == (
        "WAITING_CONFIRMATION", context.active_pending_draft_id,
    )
    assert context.active_clarification_id is None


async def test_d_unrelated_appended_turn_from_another_session_stales_lane_a(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    tenant, user, password, _m = tenant_and_user
    tenant_id = tenant.id
    fake = _fake()
    csrf, conversation_id = await _open(client, user, password, fake)
    await _say(client, csrf, conversation_id, SOURCE)
    (clarification,) = await _clarifications(db_session, tenant_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as other:
        other_csrf = await _login_and_csrf(other, user.username, password)
        # S: a new BrowserSession inherits no live clarification or buttons.
        other_page = await other.get(f"/ui/agent?conversation={conversation_id}")
        assert 'name="clarification_choice"' not in other_page.text
        appended = await _say(other, other_csrf, conversation_id, "Salam")
        assert appended.status_code == 200
    await db_session.refresh(clarification)
    assert clarification.status == "OPEN"
    late = await _say(client, csrf, conversation_id, "namizəd axtarışı")
    assert late.status_code == 200
    assert STALE_COPY in late.text
    await db_session.refresh(clarification)
    assert clarification.status == "EXPIRED"
    (task,) = await _rows(db_session, AgentTask, tenant_id=tenant_id)
    assert task.status == "EXPIRED"
    first_context = await _context_for(db_session, conversation_id, clarification)
    assert first_context.active_clarification_id is None
    assert await _rows(db_session, AgentResultSet, tenant_id=tenant_id) == []
    expired = await _events(db_session, tenant_id, "agent.clarification.expired")
    assert [event.event_metadata for event in expired] == [{"reason_code": "STALE"}]


async def _context_for(
    db: AsyncSession, conversation_id: str, clarification: AgentClarification
) -> AgentConversationSessionContext:
    rows = await _rows(
        db, AgentConversationSessionContext, id=clarification.session_context_id
    )
    assert len(rows) == 1
    return rows[0]


async def test_ttl_expired_clarification_fails_closed_without_execution(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    tenant, user, password, _m = tenant_and_user
    tenant_id = tenant.id
    csrf, conversation_id = await _open(client, user, password, _fake())
    await _say(client, csrf, conversation_id, SOURCE)
    (clarification,) = await _clarifications(db_session, tenant_id)
    clarification.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    page = await client.get(f"/ui/agent?conversation={conversation_id}")
    assert 'name="clarification_choice"' not in page.text
    late = await _say(client, csrf, conversation_id, "namizəd axtarışı")
    assert STALE_COPY in late.text
    await db_session.refresh(clarification)
    assert clarification.status == "EXPIRED"
    assert await _rows(db_session, AgentResultSet, tenant_id=tenant_id) == []
    expired = await _events(db_session, tenant_id, "agent.clarification.expired")
    assert [event.event_metadata for event in expired] == [{"reason_code": "TTL"}]


async def test_clear_new_task_supersedes_with_new_task_and_runs_normally(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    tenant, user, password, _m = tenant_and_user
    tenant_id = tenant.id
    fake = _fake()
    csrf, conversation_id = await _open(client, user, password, fake)
    await _say(client, csrf, conversation_id, SOURCE)
    response = await _say(client, csrf, conversation_id, "Java bilən namizədləri göstər")
    assert response.status_code == 200
    (clarification,) = await _clarifications(db_session, tenant_id)
    assert (clarification.status, clarification.superseded_reason) == ("SUPERSEDED", "NEW_TASK")
    (task,) = await _rows(db_session, AgentTask, tenant_id=tenant_id)
    assert task.status == "CANCELLED"
    (result_set,) = await _rows(db_session, AgentResultSet, tenant_id=tenant_id)
    assert result_set.request_sha256 == _search_hash("Java bilən namizədləri göstər")
    assert fake.clarification_calls == []


# --- S: tenant / session isolation ------------------------------------------


async def test_s_cross_tenant_clarification_id_is_indistinguishable_and_untouched(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    tenant, user, password, _m = tenant_and_user
    tenant_id = tenant.id
    csrf, conversation_id = await _open(client, user, password, _fake())
    await _say(client, csrf, conversation_id, SOURCE)
    (foreign,) = await _clarifications(db_session, tenant_id)

    other_tenant = await create_tenant(db_session, name=f"T-{uuid.uuid4().hex[:8]}")
    other_user = await create_user(
        db_session, username=f"hr-{uuid.uuid4().hex[:8]}",
        plaintext_password="correct-horse-battery-staple-1",
    )
    await create_membership(
        db_session, user_id=other_user.id, tenant_id=other_tenant.id, role=ROLE_HR_USER
    )
    other_tenant_id, other_username = other_tenant.id, other_user.username
    foreign_id = foreign.id
    await db_session.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as other:
        other_csrf = await _login_and_csrf(
            other, other_username, "correct-horse-battery-staple-1"
        )
        page = await other.get("/ui/agent")
        other_conversation = _hidden_value(page.text, "conversation_id")
        response = await _say(
            other, other_csrf, other_conversation, "Namizəd axtarışı",
            clarification_id=str(foreign_id), clarification_choice="CANDIDATE_SEARCH",
        )
    assert response.status_code == 409
    assert REJECTED_COPY in response.text
    await db_session.refresh(foreign)
    assert (foreign.status, foreign.attempt) == ("OPEN", 1)
    (event,) = await _events(db_session, other_tenant_id, "agent.clarification.rejected")
    assert event.event_metadata == {"reason_code": "NOT_ACTIVE"}
    assert await _events(db_session, tenant_id, "agent.clarification.rejected") == []


async def test_s_repository_reads_are_tenant_and_context_scoped(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    from meyar.services.agent_task_repo import read_live_clarification

    tenant, user, password, _m = tenant_and_user
    tenant_id = tenant.id
    csrf, conversation_id = await _open(client, user, password, _fake())
    await _say(client, csrf, conversation_id, SOURCE)
    (row,) = await _clarifications(db_session, tenant_id)
    assert await read_live_clarification(
        db_session, tenant_id=tenant_id, context_id=row.session_context_id,
        clarification_id=row.id,
    ) is not None
    assert await read_live_clarification(
        db_session, tenant_id=uuid.uuid4(), context_id=row.session_context_id,
        clarification_id=row.id,
    ) is None
    assert await read_live_clarification(
        db_session, tenant_id=tenant_id, context_id=uuid.uuid4(), clarification_id=row.id,
    ) is None


def test_privacy_no_text_columns_on_task_or_clarification_rows() -> None:
    """§4.3: no HR/JD/CV text is copied into structured state."""
    text_like = {
        column.name
        for table in (AgentTask.__table__, AgentClarification.__table__)
        for column in table.columns
        if re.search(r"(^|_)(text|message|query|name|email|phone)($|_)", column.name)
    }
    assert text_like == set()


async def test_t9_deterministic_amendment_keeps_the_same_lane_b_task(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    tenant, user, password, _m = tenant_and_user
    tenant_id = tenant.id
    csrf, conversation_id = await _open(client, user, password, _python_required_fake())
    await _draft_d1(client, csrf, conversation_id)
    d1 = (await _context(db_session, conversation_id)).active_pending_draft_id
    (task,) = await _rows(db_session, AgentTask, tenant_id=tenant_id)
    task_id = task.id
    amended = await _say(
        client, csrf, conversation_id, "Make Python preferred instead of required"
    )
    assert amended.status_code == 200
    d2 = (await _context(db_session, conversation_id)).active_pending_draft_id
    assert d2 is not None and d2 != d1
    (task,) = await _rows(db_session, AgentTask, tenant_id=tenant_id)
    assert (task.id, task.status, task.pending_draft_id) == (task_id, "WAITING_CONFIRMATION", d2)
    changed = await _events(db_session, tenant_id, "agent.task.state_changed")
    assert [event.event_metadata["reason_code"] for event in changed] == ["DRAFT_MODIFIED"]
