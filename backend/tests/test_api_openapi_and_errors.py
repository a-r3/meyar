"""Issue #45 (G) — OpenAPI truthfulness, error contract and the planner /
transport separation. Synthetic data only."""

import re
from pathlib import Path

import pytest
from agent_plans import search_plan
from api_contract_support import human_surface
from conftest import BrowserTestClient as AsyncClient
from fakes import FakeLLMProvider
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession
from test_ui_agent_routes import local_ui_settings  # noqa: F401 - pytest fixture re-export

from meyar.config import Settings
from meyar.llm.dependency import get_llm_provider
from meyar.main import app, create_app
from meyar.search.planner_schemas import PlannerDraft
from meyar.search.schemas import RequiredFilters

SRC = Path(__file__).resolve().parent.parent / "src" / "meyar"
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


@pytest.fixture(scope="module")
def schema() -> dict:
    return create_app(Settings(ui_cookie_secure=False)).openapi()


def _operations(schema: dict, prefix: str):  # noqa: ANN202
    for path, operations in schema["paths"].items():
        if path.startswith(prefix):
            for method, operation in operations.items():
                if method in HTTP_METHODS:
                    yield path, method, operation


def test_security_schemes_describe_the_real_mechanisms(schema: dict) -> None:
    schemes = schema["components"]["securitySchemes"]
    assert schemes["ApiKeyBearer"]["type"] == "http"
    assert schemes["ApiKeyBearer"]["scheme"] == "bearer"
    assert schemes["HumanSessionCookie"] == {
        "type": "apiKey",
        "in": "cookie",
        "name": "meyar_ui_session",
        "description": schemes["HumanSessionCookie"]["description"],
    }
    assert schemes["HumanCsrfToken"]["in"] == "header"
    assert schemes["HumanCsrfToken"]["name"] == "X-CSRF-Token"


def test_human_operations_require_cookie_and_csrf_together_and_never_an_api_key(
    schema: dict,
) -> None:
    seen = 0
    for path, method, operation in _operations(schema, "/ui/api/v1"):
        seen += 1
        (requirement,) = operation["security"]  # ONE requirement object == AND
        assert "ApiKeyBearer" not in requirement, (method, path)
        assert "HumanSessionCookie" in requirement, (method, path)
        assert ("HumanCsrfToken" in requirement) == (method != "get"), (method, path)
    assert seen >= 15


def test_machine_operations_use_only_the_api_key_scheme(schema: dict) -> None:
    protected = 0
    for path, method, operation in _operations(schema, "/api/v1"):
        if path.startswith("/api/v1/health"):
            assert "security" not in operation
            continue
        protected += 1
        schemes = {name for requirement in operation["security"] for name in requirement}
        assert schemes == {"ApiKeyBearer"}, (method, path)
    assert protected >= 20


def test_every_issue_45_error_response_is_the_documented_envelope(schema: dict) -> None:
    contract_paths = (
        "/ui/api/v1",
        "/api/v1/candidates",
        "/api/v1/jobs",
        "/api/v1/evaluations",
    )
    checked = 0
    for path, method, operation in _operations(schema, "/"):
        if not path.startswith(contract_paths):
            continue
        for code, response in operation["responses"].items():
            if code in {"401", "403", "404", "409", "413", "503"} and "content" in response:
                ref = response["content"]["application/json"]["schema"]["$ref"]
                assert ref.endswith("/ErrorEnvelope"), (method, path, code)
                checked += 1
    assert checked > 50
    envelope = schema["components"]["schemas"]["ErrorEnvelope"]
    assert set(envelope["properties"]) == {"detail", "error"}
    error_codes = schema["components"]["schemas"]["ErrorCode"]["enum"]
    assert {"AUTHENTICATION_REQUIRED", "CSRF_REJECTED", "NOT_FOUND", "RETRYABLE_BUSY"} <= set(
        error_codes
    )


def test_schema_exposes_no_internal_authority_or_storage_fields(schema: dict) -> None:
    blob = str(schema).lower()
    for forbidden in ("storage_key", "storage_path", "password_hash", "key_hash", "csrf_secret"):
        assert forbidden not in blob


def test_public_request_models_cannot_carry_authority_identifiers(schema: dict) -> None:
    for name in (
        "TurnRequest",
        "ResolveDraftRequest",
        "ConfirmDraftRequest",
        "RetryRequest",
        "ApiScoreCandidateRequest",
        "ApiBatchRankRequest",
    ):
        model = schema["components"]["schemas"][name]
        assert model.get("additionalProperties") is False, name
        assert not {"tenant_id", "user_id", "session_id", "browser_session_id"} & set(
            model["properties"]
        ), name


async def test_production_keeps_documentation_and_schema_disabled_for_both_surfaces() -> None:
    settings = Settings(
        _env_file=None,
        env="production",
        pending_login_secret="synthetic-s45-only-secret",
        database_url="postgresql+asyncpg://synthetic:synthetic@127.0.0.1:5432/synthetic",
        ollama_model="synthetic-s45:v1",
        ollama_embedding_model="synthetic-s45-embed:v1",
    )
    application = create_app(settings)
    assert application.openapi_url is None
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as ac:
        for path in ("/docs", "/openapi.json", "/redoc"):
            assert (await ac.get(path)).status_code == 404
        # the API itself is unaffected: it just refuses unauthenticated callers
        assert (await ac.get("/ui/api/v1/session")).status_code == 401
        assert (await ac.get("/api/v1/jobs")).status_code == 401


# --- runtime error contract ---------------------------------------------------------


async def test_new_machine_routes_return_the_envelope_and_keep_legacy_detail(
    client: AsyncClient, tenant_and_key
) -> None:
    _tenant, _key, plaintext = tenant_and_key
    headers = {"Authorization": f"Bearer {plaintext}"}
    missing = await client.get(
        "/api/v1/evaluations/00000000-0000-0000-0000-000000000000", headers=headers
    )
    assert missing.status_code == 404
    assert (
        missing.json()["error"]
        == {
            "code": "NOT_FOUND",
            "reason": None,
            "message": "The requested resource was not found.",
            "retryable": False,
        }
        or missing.json()["error"]["code"] == "NOT_FOUND"
    )
    # legacy `detail` key is preserved for FastAPI-style clients
    assert isinstance(missing.json()["detail"], str)
    bad = await client.get("/api/v1/jobs?page_size=500", headers=headers)
    assert bad.status_code == 422
    body = bad.json()
    assert body["detail"] == "Invalid request."
    assert body["error"] == {
        "code": "VALIDATION_FAILED",
        "reason": "REQUEST_INVALID",
        "message": "The request is invalid.",
        "retryable": False,
    }


async def test_machine_framework_errors_use_the_envelope_with_preserved_detail(
    client: AsyncClient, tenant_and_key
) -> None:
    """Every /api/v1 framework failure returns the stable error envelope.

    The historical top-level ``detail`` is preserved (where it was already
    present) and the ``error`` object is added. Auth failures keep
    ``WWW-Authenticate: Bearer``.
    """
    _tenant, _key, plaintext = tenant_and_key
    headers = {"Authorization": f"Bearer {plaintext}"}

    # 401 invalid key: legacy detail preserved + Bearer challenge header.
    bad_key = await client.get(
        "/api/v1/jobs", headers={"Authorization": "Bearer definitely-invalid-key"}
    )
    assert bad_key.status_code == 401
    assert bad_key.headers["www-authenticate"] == "Bearer"
    body = bad_key.json()
    assert body["detail"] == "Invalid or missing API key."
    assert body["error"] == {
        "code": "AUTHENTICATION_REQUIRED",
        "reason": None,
        "message": "Authentication is required.",
        "retryable": False,
    }

    # 401 missing key.
    missing_key = await client.get("/api/v1/jobs")
    assert missing_key.status_code == 401
    assert missing_key.headers["www-authenticate"] == "Bearer"
    assert missing_key.json()["detail"] == "Invalid or missing API key."

    # 422 validation: legacy detail preserved + envelope.
    bad = await client.get("/api/v1/jobs?page_size=500", headers=headers)
    assert bad.status_code == 422
    bad_body = bad.json()
    assert bad_body["detail"] == "Invalid request."
    assert bad_body["error"]["code"] == "VALIDATION_FAILED"

    # 404 from a shared contract route: enveloped, safe detail.
    missing = await client.get(
        "/api/v1/evaluations/00000000-0000-0000-0000-000000000001", headers=headers
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"
    assert "Traceback" not in missing.text
    assert "sqlalchemy" not in missing.text.lower()


async def test_planner_non_executable_outcome_is_a_200_answer_not_an_http_error(
    client: AsyncClient,
    tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    _tenant, user, password, _membership = tenant_and_user
    app.dependency_overrides[get_llm_provider] = lambda: FakeLLMProvider(
        agent_plan=search_plan(),
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["qadın"])),
    )
    human = await human_surface(client, user.username, password)
    conversation = (
        await client.post(human.url("/conversations"), headers=human.write_headers)
    ).json()["id"]
    token = await client.post(
        human.url(f"/conversations/{conversation}/submissions"), headers=human.write_headers
    )
    response = await client.post(
        human.url(f"/conversations/{conversation}/turns"),
        headers=human.write_headers,
        json={
            "message": "qadın namizədləri göstər",
            "submission_id": token.json()["submission_id"],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["results"], body
    search = next(r["search"] for r in body["results"] if r["search"])
    assert search["executable"] is False
    assert search["outcome"] == "PROHIBITED_REQUEST"
    # internal planner reason codes are NOT part of the human contract
    assert "reason_codes" not in search
    assert "PROTECTED_CRITERION" not in response.text
    assert "error" not in body


# --- no exfiltration -----------------------------------------------------------------


_NEW_MODULES = (
    "application",
    "api/human",
    "api/contracts.py",
    "api/errors.py",
    "api/openapi.py",
    "api/responses.py",
    "schemas/api_human.py",
    "schemas/api_candidates.py",
    "schemas/api_jobs.py",
    "schemas/read_models.py",
)
_NETWORK_IMPORTS = re.compile(
    r"^\s*(?:import|from)\s+(?:httpx|requests|aiohttp|urllib|http\.client|socket|"
    r"openai|anthropic|google\.generativeai|cohere|ollama)\b",
    re.M,
)


def test_new_contract_code_has_no_direct_network_or_cloud_ai_dependency() -> None:
    """Model access stays behind LLMProvider / EmbeddingProvider (loopback-only
    Ollama). The new application and API modules import no HTTP client and no
    cloud AI SDK at all, so candidate-bearing content cannot leave through them."""
    files: list[Path] = []
    for entry in _NEW_MODULES:
        target = SRC / entry
        files.extend(sorted(target.rglob("*.py")) if target.is_dir() else [target])
    assert len(files) >= 20
    offenders = [str(f.relative_to(SRC)) for f in files if _NETWORK_IMPORTS.search(f.read_text())]
    assert offenders == []


def test_deterministic_layers_never_read_the_wall_clock() -> None:
    """The evaluation date is an explicit argument: no deterministic service
    calls date.today()/datetime.now()/time.time() (issue #45 F)."""
    clock = re.compile(
        r"\b(?:date\.today|datetime\.(?:now|utcnow|today)|time\.(?:time|monotonic)|"
        r"datetime\.date\.today)\s*\("
    )
    roots = ("evaluation", "scoring", "search/structured.py", "search/policy.py")
    for root in roots:
        target = SRC / root
        for file in sorted(target.rglob("*.py")) if target.is_dir() else [target]:
            assert clock.search(file.read_text()) is None, str(file.relative_to(SRC))


async def test_clarification_is_a_200_outcome_and_its_live_state_is_readable(
    client: AsyncClient,
    tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    from agent_plans import clarify

    _tenant, user, password, _membership = tenant_and_user
    app.dependency_overrides[get_llm_provider] = lambda: FakeLLMProvider(
        agent_plan=clarify("NEED_MORE_DETAIL")
    )
    human = await human_surface(client, user.username, password)
    conversation = (
        await client.post(human.url("/conversations"), headers=human.write_headers)
    ).json()["id"]
    token = await client.post(
        human.url(f"/conversations/{conversation}/submissions"), headers=human.write_headers
    )
    response = await client.post(
        human.url(f"/conversations/{conversation}/turns"),
        headers=human.write_headers,
        json={"message": "Python", "submission_id": token.json()["submission_id"]},
    )
    assert response.status_code == 200
    assert response.json()["outcome"] == "CLARIFICATION_REQUESTED"
    assert response.json()["message"]
    detail = (
        await client.get(human.url(f"/conversations/{conversation}"), headers=human.read_headers)
    ).json()
    assert [turn["role"] for turn in detail["turns"]] == ["user", "assistant"]
    assert detail["title_kind"] in {"NEW", "GENERAL"}


# --- Machine (/api/v1) error contract regression tests -----------------------


def _assert_envelope(
    body: dict, code: str, *, detail: str, reason: str | None, retryable: bool
) -> None:
    assert set(body) == {"detail", "error"}, body
    assert body["detail"] == detail
    error = body["error"]
    assert error["code"] == code
    assert error["reason"] == reason
    assert isinstance(error["message"], str) and error["message"]
    assert error["retryable"] is retryable
    text = str(body)
    for forbidden in ("Traceback", "sqlalchemy", "storage_key", "/home/", "postgres"):
        assert forbidden not in text, forbidden


async def test_machine_401_invalid_api_key_envelope(
    client: AsyncClient, tenant_and_key
) -> None:
    _tenant, _key, _plaintext = tenant_and_key
    response = await client.get(
        "/api/v1/jobs", headers={"Authorization": "Bearer definitely-invalid-key"}
    )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    _assert_envelope(
        response.json(),
        "AUTHENTICATION_REQUIRED",
        detail="Invalid or missing API key.",
        reason=None,
        retryable=False,
    )


async def test_machine_401_missing_api_key_envelope(client: AsyncClient) -> None:
    response = await client.get("/api/v1/jobs")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    _assert_envelope(
        response.json(),
        "AUTHENTICATION_REQUIRED",
        detail="Invalid or missing API key.",
        reason=None,
        retryable=False,
    )


async def test_machine_403_insufficient_scope_envelope(
    client: AsyncClient, tenant_and_key, db_session: AsyncSession
) -> None:
    from meyar.services.api_key_repo import create_api_key

    tenant, _key, _plaintext = tenant_and_key
    _limited_key, limited_plaintext = await create_api_key(
        db_session, tenant_id=tenant.id, env="test", scopes=["candidates:read"]
    )
    await db_session.commit()
    response = await client.post(
        "/api/v1/jobs",
        headers={"Authorization": f"Bearer {limited_plaintext}"},
        json={"title": "Limited", "criteria": []},
    )
    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == "AUTHORIZATION_DENIED"
    assert isinstance(body["detail"], str) and body["detail"]
    assert "Traceback" not in response.text


async def test_machine_422_request_validation_envelope(
    client: AsyncClient, tenant_and_key
) -> None:
    _tenant, _key, plaintext = tenant_and_key
    headers = {"Authorization": f"Bearer {plaintext}"}
    response = await client.get("/api/v1/jobs?page_size=500", headers=headers)
    assert response.status_code == 422
    _assert_envelope(
        response.json(),
        "VALIDATION_FAILED",
        detail="Invalid request.",
        reason="REQUEST_INVALID",
        retryable=False,
    )


async def test_machine_shared_application_error_envelope(
    client: AsyncClient, tenant_and_key
) -> None:
    _tenant, _key, plaintext = tenant_and_key
    headers = {"Authorization": f"Bearer {plaintext}"}
    response = await client.get(
        "/api/v1/evaluations/00000000-0000-0000-0000-000000000001", headers=headers
    )
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["reason"] is None
    assert isinstance(body["detail"], str) and body["detail"]
    assert "Traceback" not in response.text


async def test_machine_413_upload_limit_envelope(
    client: AsyncClient, tenant_and_key, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tenant, _key, plaintext = tenant_and_key
    headers = {"Authorization": f"Bearer {plaintext}"}

    from meyar.config import Settings, get_settings

    app.dependency_overrides[get_settings] = lambda: Settings(max_upload_bytes=10)
    try:
        created = await client.post("/api/v1/candidates", headers=headers)
        assert created.status_code == 201
        candidate_id = created.json()["id"]
        response = await client.post(
            f"/api/v1/candidates/{candidate_id}/documents",
            headers=headers,
            files={"file": ("cv.pdf", b"x" * 100, "application/pdf")},
        )
    finally:
        app.dependency_overrides.pop(get_settings, None)
    assert response.status_code == 413
    _assert_envelope(
        response.json(),
        "PAYLOAD_TOO_LARGE",
        detail="File exceeds the maximum allowed size of 10 bytes.",
        reason=None,
        retryable=False,
    )


async def test_machine_500_unexpected_envelope(
    client: AsyncClient, tenant_and_key, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tenant, _key, plaintext = tenant_and_key
    headers = {"Authorization": f"Bearer {plaintext}"}

    async def _boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("unexpected database failure")

    monkeypatch.setattr("meyar.api.contracts.candidate_ops.list_candidate_library", _boom)
    response = await client.get("/api/v1/candidates", headers=headers)
    assert response.status_code == 500
    _assert_envelope(
        response.json(),
        "INTERNAL_ERROR",
        detail="Internal server error.",
        reason=None,
        retryable=False,
    )
