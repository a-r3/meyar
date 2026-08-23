from meyar.config import get_settings
from meyar.embedding.ollama_provider import OllamaEmbeddingProvider


def get_embedding_provider() -> OllamaEmbeddingProvider:
    settings = get_settings()
    return OllamaEmbeddingProvider(
        base_url=settings.ollama_base_url,
        model=settings.ollama_embedding_model,
        timeout_seconds=settings.embedding_timeout_seconds,
    )
