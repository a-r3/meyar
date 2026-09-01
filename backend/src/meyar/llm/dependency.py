from meyar.config import get_settings
from meyar.llm.ollama_provider import OllamaLLMProvider


def get_llm_provider() -> OllamaLLMProvider:
    settings = get_settings()
    return OllamaLLMProvider(
        base_url=settings.ollama_base_url,
        model=settings.ollama_model,
        timeout_seconds=settings.llm_timeout_seconds,
        max_concurrency=settings.inference_concurrency,
    )
