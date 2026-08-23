from functools import lru_cache

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
    embedding_timeout_seconds: float = 60.0
    embedding_max_input_chars: int = 20000

    @property
    def api_key_env(self) -> str:
        return "live" if self.env == "production" else "test"


@lru_cache
def get_settings() -> Settings:
    return Settings()
