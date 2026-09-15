from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MEYAR_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://meyar:meyar_dev_password@localhost:5432/meyar"
    env: str = "development"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:0.6b"
    max_upload_bytes: int = 10 * 1024 * 1024
    rate_limit_per_minute: int = 60
    inference_concurrency: int = 1
    storage_root: str = "./var/storage"
    llm_provider: str = "ollama"
    llm_timeout_seconds: float = 60.0
    llm_max_input_chars: int = 20000
    embedding_provider: str = "ollama"
    # DEV_INTEGRATION_MODEL default — not an approved final production
    # embedding model (see meyar.embedding.ollama_provider,
    # docs/DECISIONS.md). Final selection is blocked on the target Mac
    # Mini benchmark and multilingual quality validation (Slice 13).
    ollama_embedding_model: str = "nomic-embed-text"
    # Trusted runtime provenance for the DEV_INTEGRATION_MODEL. This is
    # configuration, never LLM-controlled planner output, and remains
    # replaceable when the target-Mac production model is approved.
    embedding_dimensions: int = 768
    embedding_timeout_seconds: float = 60.0
    embedding_max_input_chars: int = 20000
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
    def _production_ui_cookie_must_be_secure(self) -> "Settings":
        if self.env == "production" and not self.ui_cookie_secure:
            raise ValueError("Production UI cookies must be Secure.")
        return self

    @model_validator(mode="after")
    def _production_needs_real_pending_login_secret(self) -> "Settings":
        if (
            self.env == "production"
            and self.pending_login_secret == "dev-insecure-pending-login-secret-change-me"
        ):
            raise ValueError(
                "Production requires MEYAR_PENDING_LOGIN_SECRET to be set explicitly."
            )
        return self

    @property
    def api_key_env(self) -> str:
        return "live" if self.env == "production" else "test"


@lru_cache
def get_settings() -> Settings:
    return Settings()
