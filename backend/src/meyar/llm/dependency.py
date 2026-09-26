from meyar.config import Settings, get_settings
from meyar.llm.ollama_provider import OllamaLLMProvider


def get_llm_provider() -> OllamaLLMProvider:
    return llm_provider_from_settings(get_settings())


def llm_provider_from_settings(settings: Settings) -> OllamaLLMProvider:
    return OllamaLLMProvider(
        base_url=settings.ollama_base_url,
        model=settings.ollama_model,
        timeout_seconds=settings.llm_timeout_seconds,
        max_concurrency=settings.inference_concurrency,
    )
