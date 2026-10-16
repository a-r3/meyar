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
    assert bad.status_code == 422 and bad.json() == {"detail": "Invalid request."}


async def test_legacy_machine_error_bodies_are_unchanged(
    client: AsyncClient, tenant_and_key
) -> None:
    unauthenticated = await client.get("/api/v1/jobs")
    assert unauthenticated.status_code == 401
    assert unauthenticated.json() == {"detail": "Invalid or missing API key."}
    assert unauthenticated.headers["www-authenticate"] == "Bearer"


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
