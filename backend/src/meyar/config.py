from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MEYAR_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://meyar:meyar_dev_password@localhost:5432/meyar"
    env: str = "development"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5-coder:3b"
    max_upload_bytes: int = 10 * 1024 * 1024
    rate_limit_per_minute: int = 60
    inference_concurrency: int = 1

    @property
    def api_key_env(self) -> str:
        return "live" if self.env == "production" else "test"


@lru_cache
def get_settings() -> Settings:
    return Settings()
