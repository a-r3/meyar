"""Synthetic #46 runtime configuration and readiness regressions."""

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import create_async_engine

from meyar.api.v1 import health as health_routes
from meyar.config import Settings, get_settings


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_upload_bytes", 0),
        ("rate_limit_per_minute", -1),
        ("llm_timeout_seconds", 0),
        ("llm_timeout_seconds", float("inf")),
        ("embedding_timeout_seconds", -1),
        ("embedding_max_input_chars", 0),
        ("llm_max_input_chars", -1),
        ("embedding_dimensions", 0),
        ("embedding_provider", "external"),
        ("storage_root", " "),
        ("business_timezone", "Invalid/Synthetic"),
    ],
)
def test_invalid_runtime_configuration_fails_closed(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


async def test_readiness_refuses_unreachable_database_while_liveness_is_up(client, monkeypatch):
    engine = create_async_engine("postgresql+asyncpg://synthetic:synthetic@127.0.0.1:1/meyar")
    monkeypatch.setattr(health_routes, "get_engine", lambda: engine)
    from meyar.main import app

    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    try:
        assert (await client.get("/api/v1/health")).status_code == 200
        response = await client.get("/api/v1/health/ready")
        assert response.status_code == 503, response.json()
        assert "DATABASE_UNREACHABLE" in response.json()["reasons"]
    finally:
        await engine.dispose()


@pytest.mark.parametrize("fault", [None, "schema", "llm", "embedding", "storage", "daemon"])
async def test_full_readiness_components_closed_codes(db_session, tmp_path, monkeypatch, fault):
    from sqlalchemy import text

    from meyar.core import readiness

    class Health:
        def __init__(self, kind):
            self.kind = kind

        async def health(self):
            return {"reachable": fault != "daemon", "model_available": fault != self.kind}

    monkeypatch.setattr(readiness, "llm_provider_from_settings", lambda _: Health("llm"))
    monkeypatch.setattr(
        readiness, "embedding_provider_from_settings", lambda _: Health("embedding")
    )
    monkeypatch.setattr(readiness, "get_code_alembic_heads", lambda _: ["synthetic_head"])
    await db_session.execute(text("CREATE TABLE alembic_version (version_num varchar(32))"))
    await db_session.execute(
        text("INSERT INTO alembic_version VALUES (:revision)"),
        {"revision": "old" if fault == "schema" else "synthetic_head"},
    )
    await db_session.commit()
    from conftest import TEST_DATABASE_URL

    engine = create_async_engine(TEST_DATABASE_URL)
    root = tmp_path if fault != "storage" else tmp_path / "absent"
    try:
        reasons = await readiness.readiness_reasons(
            engine,
            Settings(
                _env_file=None,
                storage_root=str(root),
            ),
        )
        expected = {
            None: [],
            "schema": ["SCHEMA_MISMATCH"],
            "llm": ["LLM_MODEL_UNAVAILABLE"],
            "embedding": ["EMBEDDING_MODEL_UNAVAILABLE"],
            "storage": ["STORAGE_NOT_WRITABLE"],
            "daemon": ["OLLAMA_UNREACHABLE"],
        }
        assert reasons == expected[fault]
        assert not list(tmp_path.glob(".meyar-ops-probe-*"))
        assert not (tmp_path / "absent").exists()
    finally:
        await engine.dispose()
        await db_session.execute(text("DROP TABLE alembic_version"))
        await db_session.commit()
