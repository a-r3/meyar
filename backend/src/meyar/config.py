from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from meyar.llm.concurrency import (
    DEFAULT_INFERENCE_QUEUE_MAX_WAITERS,
    DEFAULT_INFERENCE_QUEUE_TIMEOUT_SECONDS,
)
from meyar.llm.loopback import require_loopback_url
from meyar.llm.model_identity import is_local_model_identity


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MEYAR_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    database_url: str = "postgresql+asyncpg://meyar:meyar_dev_password@localhost:5432/meyar"
    env: Literal["development", "test", "production"] = "development"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:0.6b"
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, ge=1, le=32 * 1024 * 1024)
    rate_limit_per_minute: int = Field(default=60, ge=1, le=100000)
    # Issue #85 (docs/DECISIONS.md D-089): process-wide bounded local
    # inference admission. ``inference_concurrency`` model calls run at
    # once; at most ``inference_queue_max_waiters`` more wait, each for at
    # most ``inference_queue_timeout_seconds``. Excess AI work receives a
    # truthful HR "busy" outcome instead of piling up behind Ollama.
    inference_concurrency: int = Field(default=1, ge=1, le=16)
    inference_queue_max_waiters: int = Field(
        default=DEFAULT_INFERENCE_QUEUE_MAX_WAITERS, ge=0, le=64
    )
    inference_queue_timeout_seconds: float = Field(
        default=DEFAULT_INFERENCE_QUEUE_TIMEOUT_SECONDS, gt=0, le=300
    )
    # Readiness (NOT liveness) reports INFERENCE_SATURATED only once the
    # gate has been continuously saturated for this long — a momentary
    # full queue is normal backpressure, not an unready process.
    inference_saturation_grace_seconds: float = Field(default=10.0, ge=0, le=600)
    # Issue #85: a server-owned agent-turn reservation outlives one
    # inference gap (queue wait + one model call) and is refreshed at every
    # re-entry; it only matters when a process died mid-turn.
    agent_turn_reservation_seconds: int = Field(default=600, ge=60, le=3600)
    # Issue #88 slice A (D-092 §6.7): resumable clarification TTL. Never
    # beyond the owning BrowserSession's own expiry (capped in Phase B).
    agent_clarification_ttl_seconds: int = Field(default=1800, ge=60, le=3600)
    # Explicit single-process SQLAlchemy pool policy (issue #85, D-089).
    # These equal SQLAlchemy's own QueuePool defaults; they are explicit so
    # the operability contract is reviewable. Agent inference never holds a
    # pooled connection, so these are sized for short DB work only.
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=10, ge=0, le=50)
    db_pool_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    storage_root: str = "./var/storage"
    # Request-serving runtime has no fake/deterministic provider mode.
    # Test doubles are dependency overrides and the synthetic demo provider
    # is scoped to the explicit seed command only.
    llm_provider: Literal["ollama"] = "ollama"
    # Small local models can need more than a minute for the bounded JD
    # schema-repair turn on development hardware; still finite and explicit.
    llm_timeout_seconds: float = Field(default=120.0, gt=0, le=1800)
    llm_max_input_chars: int = Field(default=20000, ge=1, le=1000000)
    embedding_provider: Literal["ollama"] = "ollama"
    # DEV_INTEGRATION_MODEL default — not an approved final production
    # embedding model (see meyar.embedding.ollama_provider,
    # docs/DECISIONS.md). Final selection is blocked on the target Mac
    # Mini benchmark and multilingual quality validation (Slice 13).
    ollama_embedding_model: str = "nomic-embed-text"
    # Trusted runtime provenance for the DEV_INTEGRATION_MODEL. This is
    # configuration, never LLM-controlled planner output, and remains
    # replaceable when the target-Mac production model is approved.
    embedding_dimensions: int = Field(default=768, ge=1, le=16000)
    embedding_timeout_seconds: float = Field(default=60.0, gt=0, le=1800)
    embedding_max_input_chars: int = Field(default=20000, ge=1, le=1000000)
    # Browser sessions are deliberately bounded and cookies are secure by
    # default. Local loopback development must opt out explicitly.
    ui_session_ttl_hours: int = Field(default=8, ge=1, le=24)
    ui_cookie_secure: bool = True
    # Signs the short-lived (not persisted) pending-login token used only
    # between password verification and an explicit tenant choice, for a
    # user with more than one active TenantMembership (Slice 1, #30) — see
    # meyar.ui.router._issue_pending_login_token. Never a session/auth
    # token itself; a captured token still requires the real password to
    # have already been verified and expires in minutes.
    pending_login_secret: str = "dev-insecure-pending-login-secret-change-me"
    # Folder-import file-stability window (Slice 14): a discovered file
    # whose mtime is newer than (scan time - this many seconds) is
    # skipped for this scan only — never marked FAILED — so a partial/
    # in-progress copy onto the source folder is never ingested
    # mid-write. Conservative default; see docs/DECISIONS.md D-021.
    folder_stability_seconds: int = Field(default=60, ge=0)
    # Slice 2 (#31) — bounded read-only agent orchestration loop. A single
    # user turn may trigger at most this many tool calls before the loop
    # is forced to stop and return whatever was gathered so far — never an
    # unbounded/recursive agent loop. See meyar.agent.service.
    agent_max_tool_calls: int = Field(default=3, ge=1, le=10)
    # How many of the most recent (role, text) turns are replayed into the
    # agent's own prompt context each orchestration step. Bounds prompt
    # size and how much conversation state one BrowserSession accumulates.
    agent_max_context_turns: int = Field(default=8, ge=1, le=50)
    # One bank/business timezone owns the UI's effective date. Deterministic
    # services still receive the resolved date explicitly and never consult
    # the wall clock themselves.
    business_timezone: str = "Asia/Baku"

    @model_validator(mode="after")
    def _runtime_values(self) -> "Settings":
        if not self.storage_root.strip():
            raise ValueError("Storage root must be nonblank.")
        try:
            ZoneInfo(self.business_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Business timezone must be a supported timezone.") from None
        return self

    @model_validator(mode="after")
    def _turn_reservation_outlives_inference_gap(self) -> "Settings":
        # A live turn must never lose its reservation merely because one
        # admitted-and-running inference gap took its full bounded time.
        gap = self.inference_queue_timeout_seconds + self.llm_timeout_seconds
        if self.agent_turn_reservation_seconds <= gap:
            raise ValueError(
                "MEYAR_AGENT_TURN_RESERVATION_SECONDS must exceed the inference queue "
                "timeout plus the LLM timeout."
            )
        return self

    @model_validator(mode="after")
    def _production_safety(self) -> "Settings":
        if self.env != "production":
            return self
        if not self.ui_cookie_secure:
            raise ValueError("Production UI cookies must be Secure.")
        if (
            "pending_login_secret" not in self.model_fields_set
            or not self.pending_login_secret.strip()
            or self.pending_login_secret.strip() == "dev-insecure-pending-login-secret-change-me"
        ):
            raise ValueError("Production requires a non-default MEYAR_PENDING_LOGIN_SECRET.")
        if "database_url" not in self.model_fields_set or not self.database_url.strip():
            raise ValueError("Production requires an explicit nonblank MEYAR_DATABASE_URL.")
        if "meyar_dev_password" in self.database_url:
            raise ValueError("Production cannot use the development database URL.")
        try:
            database = make_url(self.database_url)
        except ArgumentError:
            raise ValueError("Production database URL is invalid.") from None
        if database.drivername != "postgresql+asyncpg" or not database.database:
            raise ValueError("Production requires a PostgreSQL asyncpg database URL.")
        if self.embedding_provider != "ollama":
            raise ValueError("Production requires the local Ollama embedding provider.")
        # The same local-only boundary used by request-serving providers.
        try:
            require_loopback_url(self.ollama_base_url, setting_name="MEYAR_OLLAMA_BASE_URL")
        except ValueError:
            raise ValueError("Production requires a loopback Ollama endpoint.") from None
        endpoint = urlsplit(self.ollama_base_url)
        try:
            port = endpoint.port
        except ValueError:
            port = None
        if (
            endpoint.scheme != "http"
            or endpoint.hostname != "127.0.0.1"
            or port is None
            or endpoint.username
            or endpoint.password
            or endpoint.path not in ("", "/")
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("Production requires a numeric loopback Ollama endpoint.")
        for name in (self.ollama_model, self.ollama_embedding_model):
            if not is_local_model_identity(name):
                raise ValueError("Production requires a local model identity.")
        return self

    @property
    def api_key_env(self) -> str:
        return "live" if self.env == "production" else "test"


@lru_cache
def get_settings() -> Settings:
    return Settings()
