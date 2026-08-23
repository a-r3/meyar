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

    @model_validator(mode="after")
    def _production_ui_cookie_must_be_secure(self) -> "Settings":
        if self.env == "production" and not self.ui_cookie_secure:
            raise ValueError("Production UI cookies must be Secure.")
        return self

    @property
    def api_key_env(self) -> str:
        return "live" if self.env == "production" else "test"


@lru_cache
def get_settings() -> Settings:
    return Settings()
