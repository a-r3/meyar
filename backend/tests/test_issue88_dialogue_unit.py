"""Issue #88 slice A — pure §6.2 liveness / A2 exchange-chain and §6.4
label-table rules, without DB or model (D-092 + Amendments A1/A2)."""

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from meyar.agent.canonical_requirements import JD_SEMANTIC_POLICY_VERSION
from meyar.agent.clarification_schemas import (
    ANSWER_SCHEMA_VERSION,
    ClarificationAnswer,
    ExpiryReason,
    ResolutionSource,
    SupersededReason,
)
from meyar.agent.dialogue import (
    ClarificationSnapshot,
    SourceSlotOutcome,
    classify_vacancy_source_message,
    liveness_failure,
    match_answer_label,
    retry_clarification,
    source_sha256,
)
from meyar.agent.intent_routing import ENTRY_ROUTING_POLICY_VERSION, route_agent_entry

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
CONTEXT = uuid.uuid4()
TASK = uuid.uuid4()
SOURCE = "Python mütləqdir."


def _turn(role: str, text: str = "x") -> dict:
    return {"role": role, "text": text, "turn_id": str(uuid.uuid4())}


def _snapshot(**overrides) -> ClarificationSnapshot:  # noqa: ANN003
    values = {
        "id": uuid.uuid4(),
        "task_id": TASK,
        "session_context_id": CONTEXT,
        "context_epoch": 1,
        "clarification_type": "SEARCH_OR_VACANCY",
        "answer_schema_version": ANSWER_SCHEMA_VERSION,
        "question_turn_id": uuid.uuid4(),
        "created_from_turn_id": uuid.uuid4(),
        "source_turn_id": None,
        "source_sha256": None,
        "source_start": None,
        "source_end": None,
        "semantic_policy_version": JD_SEMANTIC_POLICY_VERSION,
        "routing_policy_version": ENTRY_ROUTING_POLICY_VERSION,
        "status": "OPEN",
        "attempt": 1,
        "expires_at": NOW + timedelta(minutes=30),
        "superseded_by_id": None,
        "superseded_reason": None,
        "task_status": "WAITING_CLARIFICATION",
    }
    values.update(overrides)
    return ClarificationSnapshot(**values)


def _attempt1(prefix: list[dict] | None = None):  # noqa: ANN202
    u1, q1 = _turn("user", SOURCE), _turn("assistant")
    current = _snapshot(
        question_turn_id=uuid.UUID(q1["turn_id"]),
        created_from_turn_id=uuid.UUID(u1["turn_id"]),
        source_turn_id=uuid.UUID(u1["turn_id"]),
        source_sha256=source_sha256(SOURCE),
        source_start=0,
        source_end=len(SOURCE),
    )
    return [*(prefix or []), u1, q1], current


def _attempt2(clarification_type: str = "SEARCH_OR_VACANCY"):  # noqa: ANN202
    u1 = _turn("user", SOURCE if clarification_type == "SEARCH_OR_VACANCY" else "trigger")
    q1, u2, q2 = _turn("assistant"), _turn("user", "hmm"), _turn("assistant")
    binding = (
        {
            "source_turn_id": uuid.UUID(u1["turn_id"]),
            "source_sha256": source_sha256(SOURCE),
            "source_start": 0,
            "source_end": len(SOURCE),
        }
        if clarification_type == "SEARCH_OR_VACANCY"
        else {}
    )
    current = _snapshot(
        clarification_type=clarification_type, attempt=2,
        question_turn_id=uuid.UUID(q2["turn_id"]),
        created_from_turn_id=uuid.UUID(u2["turn_id"]), **binding,
    )
    predecessor = _snapshot(
        clarification_type=clarification_type, attempt=1, status="SUPERSEDED",
        superseded_reason="UNCLEAR", superseded_by_id=current.id,
        question_turn_id=uuid.UUID(q1["turn_id"]),
        created_from_turn_id=uuid.UUID(u1["turn_id"]), **binding,
    )
    return [u1, q1, u2, q2], current, predecessor


def _live(turns, current, predecessor=None):  # noqa: ANN001,ANN202
    return liveness_failure(
        turns, current, predecessor, now=NOW, context_id=CONTEXT, context_epoch=1
    )


def test_attempt1_chain_is_live_and_reads_only_the_tail() -> None:
    history = [_turn("user"), _turn("assistant")] * 40
    turns, current = _attempt1(prefix=history)
    assert _live(turns, current) is None


@pytest.mark.parametrize("clarification_type", ["SEARCH_OR_VACANCY", "VACANCY_SOURCE_REQUIRED"])
def test_attempt2_exact_chain_is_live(clarification_type: str) -> None:
    turns, current, predecessor = _attempt2(clarification_type)
    assert _live(turns, current, predecessor) is None


def test_attempt2_without_predecessor_or_with_attempt1_rules_is_stale() -> None:
    turns, current, _predecessor = _attempt2()
    assert _live(turns, current, None) == ExpiryReason.STALE
    # A1's literal adjacency (source directly before Q2) never validates attempt 2.
    assert _live(turns, dataclasses.replace(current, attempt=1)) == ExpiryReason.STALE


@pytest.mark.parametrize("position", ["before-q1", "after-q1", "after-q2"])
def test_foreign_appended_entry_anywhere_in_the_chain_is_stale(position: str) -> None:
    turns, current, predecessor = _attempt2()
    u1, q1, u2, q2 = turns
    foreign = _turn("user")
    forged = {
        "before-q1": [u1, foreign, q1, u2, q2],
        "after-q1": [u1, q1, foreign, u2, q2],
        "after-q2": [u1, q1, u2, q2, foreign, _turn("assistant")],
    }[position]
    assert _live(forged, current, predecessor) == ExpiryReason.STALE


@pytest.mark.parametrize(
    "change",
    [
        {"task_id": uuid.uuid4()},
        {"session_context_id": uuid.uuid4()},
        {"clarification_type": "VACANCY_SOURCE_REQUIRED"},
        {"superseded_reason": "NEW_TASK"},
        {"status": "EXPIRED"},
        {"attempt": 2},
        {"superseded_by_id": uuid.uuid4()},
        {"source_sha256": "0" * 64},
        {"question_turn_id": uuid.uuid4()},
        {"created_from_turn_id": uuid.uuid4()},
    ],
)
def test_forged_predecessor_chain_is_stale(change: dict) -> None:
    turns, current, predecessor = _attempt2()
    assert _live(turns, current, dataclasses.replace(predecessor, **change)) is not None


def test_wrong_roles_or_missing_turn_ids_are_stale() -> None:
    turns, current = _attempt1()
    legacy = [{k: v for k, v in turn.items() if k != "turn_id"} for turn in turns]
    assert _live(legacy, current) == ExpiryReason.STALE
    swapped = [{**turns[0], "role": "assistant"}, turns[1]]
    assert _live(swapped, current) == ExpiryReason.STALE


def test_lane_b_in_place_rewrite_keeps_liveness_turn_version_is_not_consulted() -> None:
    turns, current = _attempt1(prefix=[_turn("user"), {**_turn("assistant"), "pending": 1}])
    rewritten = [dict(turn) for turn in turns]
    rewritten[1].pop("pending")
    rewritten[1]["confirmed_job_draft"] = {"draft_id": "d1"}
    assert _live(rewritten, current) is None


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"expires_at": NOW}, ExpiryReason.TTL),
        ({"context_epoch": 2}, ExpiryReason.STALE),
        ({"session_context_id": uuid.uuid4()}, ExpiryReason.STALE),
        ({"task_status": "CANCELLED"}, ExpiryReason.STALE),
        ({"answer_schema_version": "clarification-answers-v0"}, ExpiryReason.VERSION),
        ({"semantic_policy_version": "old"}, ExpiryReason.VERSION),
        ({"routing_policy_version": "old"}, ExpiryReason.VERSION),
        ({"source_sha256": "f" * 64}, ExpiryReason.SOURCE_MISMATCH),
        ({"source_end": 4}, ExpiryReason.SOURCE_MISMATCH),
    ],
)
def test_attempt1_staleness_reasons(change: dict, reason: ExpiryReason) -> None:
    turns, current = _attempt1()
    assert _live(turns, dataclasses.replace(current, **change)) == reason


def test_tampered_source_text_is_source_mismatch() -> None:
    turns, current = _attempt1()
    turns[-2] = {**turns[-2], "text": "Java mütləqdir."}
    assert _live(turns, current) == ExpiryReason.SOURCE_MISMATCH


def test_retry_keeps_the_original_source_binding_and_sequences_u2() -> None:
    _turns, current = _attempt1()
    u2, q2 = uuid.uuid4(), uuid.uuid4()
    retry = retry_clarification(current, user_turn_id=u2, question_turn_id=q2)
    assert (retry.attempt, retry.reuse_task_id, retry.predecessor_id) == (2, TASK, current.id)
    assert (retry.created_from_turn_id, retry.question_turn_id) == (u2, q2)
    assert retry.source_turn_id == current.source_turn_id != u2
    assert (retry.source_sha256, retry.source_start, retry.source_end) == (
        current.source_sha256, current.source_start, current.source_end,
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("namizəd axtarışı", ClarificationAnswer.CANDIDATE_SEARCH),
        ("Namizəd axtarışı et", ClarificationAnswer.CANDIDATE_SEARCH),
        ("candidate search", ClarificationAnswer.CANDIDATE_SEARCH),
        ("namizəd search", ClarificationAnswer.CANDIDATE_SEARCH),
        ("vakansiya kimi", ClarificationAnswer.VACANCY_ANALYSIS),
        ("Vakansiya tələbi kimi", ClarificationAnswer.VACANCY_ANALYSIS),
        ("vacancy", ClarificationAnswer.VACANCY_ANALYSIS),
        ("job analysis please", ClarificationAnswer.VACANCY_ANALYSIS),
        ("job", None),
        ("namizəd axtarışı, SQL də olsun", None),
        ("vakansiya kimi namizəd axtarışı", None),
        ("Python mütləqdir", None),
        ("namizəd axtarışı 5", None),
        ("", None),
    ],
)
def test_closed_label_table_is_strict_and_whole_message(
    text: str, expected: ClarificationAnswer | None
) -> None:
    assert match_answer_label(text) == expected


@pytest.mark.parametrize(
    ("message", "outcome"),
    [
        ("Java bilən namizədləri göstər", SourceSlotOutcome.NEW_TASK),
        ("ilk 3", SourceSlotOutcome.NEW_TASK),
        ("Python mütləqdir.", SourceSlotOutcome.SOURCE),
        ("Backend developer\nKomanda işi", SourceSlotOutcome.SOURCE),
        ("Vakansiyanı analiz et:\nBackend.\nPython required.", SourceSlotOutcome.SOURCE),
        ("hmm", SourceSlotOutcome.UNCLEAR),
        ("Vakansiyanı analiz et", SourceSlotOutcome.UNCLEAR),
        ("vakansiya kimi", SourceSlotOutcome.UNCLEAR),
        # Unrepresentable (CLARIFY_INPUT_STRUCTURE) text is never an
        # analyzable source, even when multi-line.
        ("Python " * 400 + "tələb olunur\nKomanda işi", SourceSlotOutcome.UNCLEAR),
    ],
)
def test_vacancy_source_required_slot_rules_are_deterministic(
    message: str, outcome: SourceSlotOutcome
) -> None:
    result, source = classify_vacancy_source_message(message, route_agent_entry(message))
    assert result == outcome
    if message.startswith("Vakansiyanı analiz et:"):
        # The router's exact source span after the wrapper, never the wrapper.
        assert source == "Backend.\nPython required."
    elif outcome == SourceSlotOutcome.SOURCE:
        assert source == message


def test_closed_vocabularies_match_the_accepted_contract() -> None:
    assert {reason.value for reason in SupersededReason} == {"UNCLEAR", "NEW_TASK"}
    assert {source.value for source in ResolutionSource} == {
        "BUTTON", "LABEL", "MODEL", "SOURCE_MESSAGE",
    }
