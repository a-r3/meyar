"""Issue #45 — accountable-human HTTP/JSON surface (``/ui/api/v1``).

Synthetic data only; the local model is the deterministic FakeLLMProvider."""

import uuid

import pytest
from agent_plans import converse, vacancy_proposal
from conftest import DEFAULT_TEST_PASSWORD
from conftest import BrowserTestClient as AsyncClient
from fakes import FakeLLMProvider
from httpx import ASGITransport
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_ui_agent_routes import local_ui_settings  # noqa: F401 - pytest fixture re-export

from meyar.agent.schemas import JDCriteriaDraft, JDDraftCriterionItem
from meyar.config import Settings
from meyar.core.roles import ROLE_HR_USER
from meyar.llm.dependency import get_llm_provider
from meyar.main import app
from meyar.models.audit_event import AuditEvent
from meyar.models.job import JOB_STATUS_ARCHIVED, Job
from meyar.models.tenant import Tenant
from meyar.schemas.criteria import CriterionIn, CriterionKind, CriterionType
from meyar.services.candidate_repo import create_candidate
from meyar.services.job_criteria_repo import create_criteria_version
from meyar.services.job_repo import create_job
from meyar.services.tenant_membership_repo import create_membership, set_membership_active
from meyar.services.user_repo import create_user

BASE = "/ui/api/v1"
JD = "Analyze this job description:\nBackend.\nPython required."


def _python_fake() -> FakeLLMProvider:
    return FakeLLMProvider(
        agent_plan=vacancy_proposal(),
        jd_draft=JDCriteriaDraft(
            title="Backend",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind="SKILL",
                    requirement="Python",
                    source_text="Python required",
                )
            ],
        ),
    )


async def _login(client: AsyncClient, user, password: str) -> str:  # noqa: ANN001
    # The app and the test share one session, so a request can expire the ORM
    # object: tests that log in more than once pass the name captured up front.
    username = user if isinstance(user, str) else user.username
    response = await client.post(
        "/ui/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303
    session = await client.get(f"{BASE}/session")
    assert session.status_code == 200
    return session.json()["csrf_token"]


def _h(csrf: str) -> dict[str, str]:
    return {"X-CSRF-Token": csrf}


async def _new_conversation(client: AsyncClient, csrf: str) -> str:
    response = await client.post(f"{BASE}/conversations", headers=_h(csrf))
    assert response.status_code == 201
    return response.json()["id"]


async def _submit(client: AsyncClient, csrf: str, conversation: str, message: str, **extra):  # noqa: ANN003,ANN202
    token = await client.post(f"{BASE}/conversations/{conversation}/submissions", headers=_h(csrf))
    assert token.status_code == 201
    return await client.post(
        f"{BASE}/conversations/{conversation}/turns",
        headers=_h(csrf),
        json={"message": message, "submission_id": token.json()["submission_id"], **extra},
    ), token.json()["submission_id"]


# --- authentication / csrf / separation -------------------------------------


async def test_unauthenticated_human_json_is_401_envelope_not_a_redirect(
    client: AsyncClient,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    for method, path in (
        ("GET", "/session"),
        ("GET", "/conversations"),
        ("POST", "/conversations"),
        ("POST", f"/drafts/{uuid.uuid4()}/confirm"),
    ):
        response = await client.request(method, f"{BASE}{path}", follow_redirects=False)
        assert response.status_code == 401, path
        body = response.json()
        assert body["error"]["code"] == "AUTHENTICATION_REQUIRED"
        assert body["error"]["retryable"] is False
        assert "location" not in response.headers
        assert "<html" not in response.text.lower()


async def test_api_key_is_never_human_authority_and_cookie_is_never_machine_authority(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_key_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    tenant, _key, plaintext_key, user, password, _membership = tenant_key_and_user
    bearer = {"Authorization": f"Bearer {plaintext_key}"}
    for method, path in (
        ("GET", "/session"),
        ("GET", "/conversations"),
        ("POST", f"/drafts/{uuid.uuid4()}/confirm"),
        ("POST", "/conversations"),
    ):
        response = await client.request(method, f"{BASE}{path}", headers=bearer)
        assert response.status_code == 401, path
        assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"

    await _login(client, user, password)
    machine = await client.get("/api/v1/usage")
    assert machine.status_code == 401  # the cookie session is not an API key


async def test_session_endpoint_returns_only_the_server_derived_principal(
    client: AsyncClient,
    tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    tenant, user, password, membership = tenant_and_user
    await _login(client, user, password)
    response = await client.get(f"{BASE}/session")
    body = response.json()
    assert body["tenant_id"] == str(tenant.id)
    assert body["user_id"] == str(user.id)
    assert body["membership_id"] == str(membership.id)
    assert len(body["csrf_token"]) == 64
    assert response.headers["cache-control"] == "no-store"
    assert "password" not in response.text


async def test_state_changing_json_requires_a_valid_csrf_header(
    client: AsyncClient,
    tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    _tenant, user, password, _membership = tenant_and_user
    csrf = await _login(client, user, password)
    for headers in ({}, _h("0" * 64), _h(csrf[:-1] + ("0" if csrf[-1] != "0" else "1"))):
        response = await client.post(f"{BASE}/conversations", headers=headers)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "CSRF_REJECTED"
    assert (await client.post(f"{BASE}/conversations", headers=_h(csrf))).status_code == 201
    # A GET never needs one.
    assert (await client.get(f"{BASE}/conversations")).status_code == 200


async def test_csrf_is_checked_after_authentication_so_it_never_leaks_resources(
    client: AsyncClient,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    response = await client.post(f"{BASE}/conversations", headers=_h("a" * 64))
    assert response.status_code == 401


async def test_client_supplied_authority_is_rejected_not_obeyed(
    client: AsyncClient,
    tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    _tenant, user, password, _membership = tenant_and_user
    csrf = await _login(client, user, password)
    conversation = await _new_conversation(client, csrf)
    forged = {
        "tenant_id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "session_id": str(uuid.uuid4()),
        "browser_session_id": str(uuid.uuid4()),
    }
    token = await client.post(f"{BASE}/conversations/{conversation}/submissions", headers=_h(csrf))
    for field, value in forged.items():
        response = await client.post(
            f"{BASE}/conversations/{conversation}/turns",
            headers=_h(csrf),
            json={"message": "salam", "submission_id": token.json()["submission_id"], field: value},
        )
        assert response.status_code == 422, field
        assert value not in response.text
        assert response.json()["error"]["code"] == "VALIDATION_FAILED"
    response = await client.post(
        f"{BASE}/drafts/{uuid.uuid4()}/confirm", headers=_h(csrf), json={"tenant_id": "x"}
    )
    assert response.status_code == 422


async def test_logged_out_revoked_session_is_refused_and_cookie_cleared(
    client: AsyncClient,
    tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    _tenant, user, password, _membership = tenant_and_user
    csrf = await _login(client, user, password)
    stolen = client.cookies["meyar_ui_session"]
    await client.post("/ui/logout", data={"csrf_token": csrf}, follow_redirects=False)
    client.cookies.clear()
    client.cookies.set("meyar_ui_session", stolen, domain="test.local", path="/ui")
    response = await client.get(f"{BASE}/session")
    assert response.status_code == 401
    assert "meyar_ui_session" in response.headers.get("set-cookie", "")


async def test_membership_and_tenant_deactivation_take_effect_immediately(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    tenant, user, password, membership = tenant_and_user
    tenant_id, membership_id = tenant.id, membership.id
    csrf = await _login(client, user, password)
    assert (await client.get(f"{BASE}/conversations")).status_code == 200
    await set_membership_active(db_session, membership_id=membership_id, is_active=False)
    await db_session.commit()
    response = await client.post(f"{BASE}/conversations", headers=_h(csrf))
    assert response.status_code == 401
    await set_membership_active(db_session, membership_id=membership_id, is_active=True)
    await db_session.execute(update(Tenant).where(Tenant.id == tenant_id).values(is_active=False))
    await db_session.commit()
    response = await client.get(f"{BASE}/conversations")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


# --- conversations -----------------------------------------------------------


async def test_conversation_lifecycle_is_bounded_and_owner_isolated(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    tenant_key_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    tenant, user, password, _membership = tenant_and_user
    csrf = await _login(client, user, password)
    first = await _new_conversation(client, csrf)
    second = await _new_conversation(client, csrf)
    listing = (await client.get(f"{BASE}/conversations?page_size=1")).json()
    assert [item["id"] for item in listing["items"]] == [second]
    assert listing["has_next"] is True and listing["page_size"] == 1
    assert (await client.get(f"{BASE}/conversations?page_size=51")).status_code == 422
    assert (await client.get(f"{BASE}/conversations?page=0")).status_code == 422
    detail = (await client.get(f"{BASE}/conversations/{first}")).json()
    assert detail["turns"] == [] and detail["title_kind"] == "NEW"

    # Another user in the SAME tenant, and a user in ANOTHER tenant: both get
    # an identical 404 for a real id and for a random id.
    other_user = await create_user(
        db_session, username=f"hr2-{uuid.uuid4().hex[:6]}", plaintext_password=DEFAULT_TEST_PASSWORD
    )
    await create_membership(
        db_session, user_id=other_user.id, tenant_id=tenant.id, role=ROLE_HR_USER
    )
    await db_session.commit()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as other:
        other_csrf = await _login(other, other_user, DEFAULT_TEST_PASSWORD)
        real = await other.get(f"{BASE}/conversations/{first}")
        fake = await other.get(f"{BASE}/conversations/{uuid.uuid4()}")
        assert real.status_code == fake.status_code == 404
        assert real.json() == fake.json()
        assert (await other.get(f"{BASE}/conversations")).json()["items"] == []
        token = await other.post(
            f"{BASE}/conversations/{first}/submissions", headers=_h(other_csrf)
        )
        assert token.status_code == 404
    _t2, _k2, _pk2, user2, password2, _m2 = tenant_key_and_user
    async with AsyncClient(transport=transport, base_url="http://test") as foreign:
        await _login(foreign, user2, password2)
        assert (await foreign.get(f"{BASE}/conversations/{first}")).status_code == 404


async def test_turn_is_idempotent_and_planner_outcome_is_not_an_error(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    _tenant, user, password, _membership = tenant_and_user
    app.dependency_overrides[get_llm_provider] = lambda: FakeLLMProvider(
        agent_plan=converse("GREETING")
    )
    csrf = await _login(client, user, password)
    conversation = await _new_conversation(client, csrf)
    first, submission = await _submit(client, csrf, conversation, "salam")
    assert first.status_code == 200
    body = first.json()
    assert body["status"] == "COMPLETED" and body["outcome"] == "ANSWERED"
    assert body["message"]
    # Replay of the SAME submission: never a second turn.
    replay = await client.post(
        f"{BASE}/conversations/{conversation}/turns",
        headers=_h(csrf),
        json={"message": "salam", "submission_id": submission},
    )
    assert replay.status_code == 200 and replay.json()["status"] == "REPLAYED"
    history = (await client.get(f"{BASE}/conversations/{conversation}")).json()
    assert [turn["role"] for turn in history["turns"]] == ["user", "assistant"]
    # A different message under the already-used identity is refused identically
    # to an unknown one (no oracle).
    tampered = await client.post(
        f"{BASE}/conversations/{conversation}/turns",
        headers=_h(csrf),
        json={"message": "başqa", "submission_id": submission},
    )
    assert tampered.status_code == 404


async def test_turn_message_is_bounded_and_unknown_submission_is_not_found(
    client: AsyncClient,
    tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    _tenant, user, password, _membership = tenant_and_user
    csrf = await _login(client, user, password)
    conversation = await _new_conversation(client, csrf)
    token = await client.post(f"{BASE}/conversations/{conversation}/submissions", headers=_h(csrf))
    too_long = await client.post(
        f"{BASE}/conversations/{conversation}/turns",
        headers=_h(csrf),
        json={"message": "x" * 4001, "submission_id": token.json()["submission_id"]},
    )
    assert too_long.status_code == 422
    assert too_long.json()["error"]["reason"] == "MESSAGE_LENGTH"
    huge = await client.post(
        f"{BASE}/conversations/{conversation}/turns",
        headers=_h(csrf),
        json={"message": "x" * 9000, "submission_id": token.json()["submission_id"]},
    )
    assert huge.status_code == 422
    unknown = await client.post(
        f"{BASE}/conversations/{conversation}/turns",
        headers=_h(csrf),
        json={"message": "salam", "submission_id": str(uuid.uuid4())},
    )
    assert unknown.status_code == 404


async def test_busy_local_inference_is_a_retryable_503_not_a_planner_outcome(
    client: AsyncClient,
    tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    from meyar.llm.provider import InferenceBusyError

    _tenant, user, password, _membership = tenant_and_user
    app.dependency_overrides[get_llm_provider] = lambda: FakeLLMProvider(
        agent_error=InferenceBusyError("QUEUE_FULL")
    )
    csrf = await _login(client, user, password)
    conversation = await _new_conversation(client, csrf)
    response, _submission = await _submit(client, csrf, conversation, "salam, necəsən?")
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "RETRYABLE_BUSY" and error["retryable"] is True
    assert error["reason"] == "BUSY"
    assert "QUEUE" not in response.text and "Traceback" not in response.text
    history = (await client.get(f"{BASE}/conversations/{conversation}")).json()
    assert history["turns"] == []  # nothing fabricated or persisted


# --- JD draft: review, accountable confirmation ------------------------------


async def _draft(client: AsyncClient, csrf: str) -> tuple[str, str, dict]:
    app.dependency_overrides[get_llm_provider] = lambda: _python_fake()
    conversation = await _new_conversation(client, csrf)
    response, _submission = await _submit(client, csrf, conversation, JD)
    assert response.status_code == 200, response.text
    results = response.json()["results"]
    draft = next(item["job_draft"] for item in results if item["job_draft"])
    assert draft["can_act"] is True
    return conversation, draft["draft_id"], draft


async def test_confirm_creates_the_job_once_and_replays_without_a_second_one(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    tenant, user, password, _membership = tenant_and_user
    csrf = await _login(client, user, password)
    conversation, draft_id, draft = await _draft(client, csrf)
    assert draft["requires_resolution"] is False
    assert [row["requirement"] for row in draft["must_have_rows"]] == ["Python"]

    assert (await client.post(f"{BASE}/drafts/{draft_id}/confirm", json={})).status_code == 403
    confirmed = await client.post(f"{BASE}/drafts/{draft_id}/confirm", headers=_h(csrf), json={})
    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()
    assert body["replayed"] is False and body["criteria_version_number"] == 1

    again = await client.post(f"{BASE}/drafts/{draft_id}/confirm", headers=_h(csrf), json={})
    assert again.status_code == 200 and again.json()["replayed"] is True
    assert again.json()["job_id"] == body["job_id"]
    assert again.json()["criteria_version_id"] == body["criteria_version_id"]
    jobs = (await db_session.scalars(select(Job).where(Job.tenant_id == tenant.id))).all()
    assert len(jobs) == 1

    events = (
        await db_session.scalars(
            select(AuditEvent).where(
                AuditEvent.tenant_id == tenant.id, AuditEvent.event_type == "agent.draft.confirmed"
            )
        )
    ).all()
    assert len(events) == 1
    event = events[0]
    assert event.actor_type == "HUMAN_USER" and event.actor_id == user.id
    assert event.event_metadata == {
        "draft_id": draft_id,
        "job_id": body["job_id"],
        "criteria_version_id": body["criteria_version_id"],
        "criteria_version": 1,
    }
    # Confirmation is independent of ranking: nothing was ranked by it.
    from meyar.models.evaluation import Evaluation

    assert (await db_session.scalars(select(Evaluation))).all() == []


async def test_only_the_exact_live_draft_in_the_callers_own_session_is_confirmable(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    tenant, user, password, _membership = tenant_and_user
    tenant_id, username = tenant.id, user.username
    csrf = await _login(client, username, password)
    _conversation, draft_id, _draft_body = await _draft(client, csrf)
    # unknown draft id
    unknown = await client.post(f"{BASE}/drafts/{uuid.uuid4()}/confirm", headers=_h(csrf), json={})
    assert unknown.status_code == 404
    assert unknown.json()["error"]["reason"] == "DRAFT_NOT_ACTIVE"

    # a second login is a different BrowserSession: the draft is not its authority
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as relogin:
        relogin_csrf = await _login(relogin, username, password)
        refused = await relogin.post(
            f"{BASE}/drafts/{draft_id}/confirm", headers=_h(relogin_csrf), json={}
        )
        assert refused.status_code == 404
    assert (await db_session.scalars(select(Job).where(Job.tenant_id == tenant_id))).all() == []


async def test_reviewed_proposal_must_equal_the_server_draft_exactly(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    tenant, user, password, _membership = tenant_and_user
    tenant_id = tenant.id
    csrf = await _login(client, user, password)
    _conversation, draft_id, draft = await _draft(client, csrf)
    row = draft["must_have_rows"][0]
    good = {
        "span_id": row["span_id"],
        "criterion": {
            "id": "python",
            "kind": "SKILL",
            "type": "MUST_HAVE",
            "label": "Python",
            "value": "Python",
            "weight": 1.0,
        },
    }

    def body(**changes):  # noqa: ANN003,ANN202
        criterion = {**good["criterion"], **changes.pop("criterion", {})}
        return {
            "reviewed": {
                "title": changes.pop("title", "Backend"),
                "criteria": [{**good, "criterion": criterion}],
            }
        }

    for tampered in (
        body(criterion={"weight": 0.1}),  # weakened weight
        body(criterion={"value": "Java", "label": "Java"}),  # replaced criterion
        body(criterion={"type": "PREFERRED"}),  # weakened modality
        body(title="Other title"),  # different title
        {"reviewed": {"title": "Backend", "criteria": []}},  # criterion removed
    ):
        refused = await client.post(
            f"{BASE}/drafts/{draft_id}/confirm", headers=_h(csrf), json=tampered
        )
        assert refused.status_code == 422, tampered
        assert refused.json()["error"]["code"] == "VALIDATION_FAILED"
    assert (await db_session.scalars(select(Job).where(Job.tenant_id == tenant_id))).all() == []
    accepted = await client.post(f"{BASE}/drafts/{draft_id}/confirm", headers=_h(csrf), json=body())
    assert accepted.status_code == 200, accepted.text


async def test_resolve_requires_exactly_one_decision_and_csrf(
    client: AsyncClient,
    tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    _tenant, user, password, _membership = tenant_and_user
    csrf = await _login(client, user, password)
    _conversation, draft_id, draft = await _draft(client, csrf)
    span = draft["must_have_rows"][0]["span_id"]
    assert (
        await client.post(f"{BASE}/drafts/{draft_id}/resolve", json={"span_id": span})
    ).status_code == 403
    nothing = await client.post(
        f"{BASE}/drafts/{draft_id}/resolve", headers=_h(csrf), json={"span_id": span}
    )
    assert nothing.status_code == 422
    assert nothing.json()["error"]["reason"] == "INVALID_RESOLUTION"
    two = await client.post(
        f"{BASE}/drafts/{draft_id}/resolve",
        headers=_h(csrf),
        json={"span_id": span, "criterion_type": "PREFERRED", "decision": "exclude"},
    )
    assert two.status_code == 422
    bad_span = await client.post(
        f"{BASE}/drafts/{draft_id}/resolve",
        headers=_h(csrf),
        json={"span_id": "'; DROP", "criterion_type": "PREFERRED"},
    )
    assert bad_span.status_code == 422


async def test_unrelated_user_cannot_use_a_draft_id_from_another_tenant(
    client: AsyncClient,
    tenant_and_user,
    tenant_key_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    _tenant, user, password, _membership = tenant_and_user
    csrf = await _login(client, user, password)
    _conversation, draft_id, _draft_body = await _draft(client, csrf)
    _t, _k, _pk, user2, password2, _m = tenant_key_and_user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as foreign:
        foreign_csrf = await _login(foreign, user2, password2)
        for action in ("confirm", "resolve"):
            payload = (
                {}
                if action == "confirm"
                else {"span_id": "req-0001", "criterion_type": "PREFERRED"}
            )
            response = await foreign.post(
                f"{BASE}/drafts/{draft_id}/{action}", headers=_h(foreign_csrf), json=payload
            )
            assert response.status_code in (404, 422)
            assert response.status_code != 200


@pytest.mark.parametrize("prefix", ["/ui/api/v1", "/api/v1"])
async def test_error_bodies_never_leak_internals(
    client: AsyncClient,
    local_ui_settings: Settings,  # noqa: F811
    prefix: str,
) -> None:
    response = await client.get(f"{prefix}/does-not-exist")
    text = response.text
    for forbidden in ("Traceback", "sqlalchemy", "/home/", "storage_key", "postgres"):
        assert forbidden not in text


async def test_human_duplicate_guard_is_preserved_for_identical_active_jobs(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    """Documented compatibility contract (D-116): the human workflow rejects an
    identical ACTIVE job (409); the machine API does not (see
    test_machine_api_job_creation_still_permits_duplicates)."""
    tenant, user, password, _membership = tenant_and_user
    tenant_id = tenant.id
    csrf = await _login(client, user, password)
    _c1, first_draft, _d1 = await _draft(client, csrf)
    first = await client.post(f"{BASE}/drafts/{first_draft}/confirm", headers=_h(csrf), json={})
    assert first.status_code == 200
    _c2, second_draft, _d2 = await _draft(client, csrf)
    second = await client.post(f"{BASE}/drafts/{second_draft}/confirm", headers=_h(csrf), json={})
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "CONFLICT"
    assert second.json()["error"]["reason"] == "DUPLICATE_ACTIVE_JOB"
    jobs = (await db_session.scalars(select(Job).where(Job.tenant_id == tenant_id))).all()
    assert len(jobs) == 1


async def test_clarification_state_is_readable_and_buttons_are_checked_against_the_live_one(
    client: AsyncClient,
    tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    from meyar.agent.clarification_schemas import ClarificationProposalValue

    _tenant, user, password, _membership = tenant_and_user
    app.dependency_overrides[get_llm_provider] = lambda: FakeLLMProvider(
        agent_plan=converse("GREETING"),
        clarification_proposals=[ClarificationProposalValue.UNCLEAR],
    )
    csrf = await _login(client, user, password)
    conversation = await _new_conversation(client, csrf)
    first, _s = await _submit(client, csrf, conversation, "Python mütləqdir.")
    assert first.status_code == 200
    detail = (await client.get(f"{BASE}/conversations/{conversation}")).json()
    live = detail["active_clarification"]
    assert live is not None and live["choices"]
    # A button naming a foreign clarification id is refused; nothing changes.
    refused, _s2 = await _submit(
        client,
        csrf,
        conversation,
        "bəli",
        clarification_id=str(uuid.uuid4()),
        clarification_choice=live["choices"][0]["value"],
    )
    assert refused.status_code == 409
    assert refused.json()["error"]["reason"] == "CLARIFICATION_NOT_ACTIVE"
    after = (await client.get(f"{BASE}/conversations/{conversation}")).json()
    assert after["active_clarification"]["id"] == live["id"]
    assert after["turns"] == detail["turns"]


# --- Human JSON legacy_detail separation (issue #45 blocker 2) --------------


def _profile(*skills: str) -> dict:
    return {
        "skills": [{"name": s, "evidence": {"source": "synthetic", "quote": s}} for s in skills],
        "employment_history": [],
        "education": [],
        "certifications": [],
        "languages": [],
        "projects": [],
    }


def _skill(criterion_id: str, value: str, weight: float = 1.0) -> CriterionIn:
    return CriterionIn(
        id=criterion_id,
        kind=CriterionKind.SKILL,
        type=CriterionType.MUST_HAVE,
        label=value,
        value=value,
        weight=weight,
    )


async def _human_job(
    db_session: AsyncSession, tenant_id: uuid.UUID, *criteria: CriterionIn
) -> tuple[uuid.UUID, int]:
    job = await create_job(db_session, tenant_id=tenant_id, title="Human Job")
    version = await create_criteria_version(
        db_session,
        tenant_id=tenant_id,
        job_id=job.id,
        criteria=[c.model_dump(mode="json") for c in criteria],
        created_by_api_key_id=None,
    )
    await db_session.commit()
    return job.id, version.version_number


async def _assert_human_envelope(
    response, *, status: int, code: str, reason: str, message_fragment: str
) -> None:
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"detail", "error"}, body
    error = body["error"]
    assert error["code"] == code
    assert error["reason"] == reason
    assert isinstance(error["message"], str) and error["message"]
    # Blocker 2: top-level detail is the fixed human message, never the raw
    # machine/internal legacy_detail sub-code.
    assert body["detail"] == error["message"]
    assert message_fragment in body["detail"]
    # Human JSON never exposes legacy_detail as a top-level field.
    assert "legacy_detail" not in body


async def test_human_score_not_found_detail_is_message_not_legacy_detail(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    tenant, user, password, _membership = tenant_and_user
    tenant_id = tenant.id
    csrf = await _login(client, user, password)
    # A candidate with NO effective profile: score must 404 with PROFILE_NOT_FOUND.
    candidate = await create_candidate(db_session, tenant_id=tenant_id)
    await db_session.commit()
    job, number = await _human_job(db_session, tenant_id, _skill("python", "Python"))
    response = await client.post(
        f"{BASE}/jobs/{job}/criteria/{number}/score",
        headers=_h(csrf),
        json={"candidate_id": str(candidate.id)},
    )
    await _assert_human_envelope(
        response,
        status=404,
        code="NOT_FOUND",
        reason="PROFILE_NOT_FOUND",
        message_fragment="profile",
    )


async def test_human_rank_archived_job_detail_is_message_not_legacy_code(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    tenant, user, password, _membership = tenant_and_user
    tenant_id = tenant.id
    csrf = await _login(client, user, password)
    job, number = await _human_job(db_session, tenant_id, _skill("python", "Python"))
    # Archive the job so ranking is refused with the closed JOB_ARCHIVED sub-code.
    job_row = await db_session.get(Job, job)
    job_row.status = JOB_STATUS_ARCHIVED
    await db_session.commit()
    response = await client.post(
        f"{BASE}/jobs/{job}/criteria/{number}/rank",
        headers=_h(csrf),
        json={},
    )
    await _assert_human_envelope(
        response,
        status=409,
        code="CONFLICT",
        reason="JOB_ARCHIVED",
        message_fragment="archived",
    )
