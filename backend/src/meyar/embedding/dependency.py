from meyar.config import Settings, get_settings
from meyar.embedding.ollama_provider import OllamaEmbeddingProvider
from meyar.embedding.serializer import SERIALIZER_VERSION
from meyar.search.schemas import EmbeddingSearchConfig


def get_embedding_provider() -> OllamaEmbeddingProvider:
    return embedding_provider_from_settings(get_settings())


def embedding_provider_from_settings(settings: Settings) -> OllamaEmbeddingProvider:
    return OllamaEmbeddingProvider(
        base_url=settings.ollama_base_url,
        model=settings.ollama_embedding_model,
        timeout_seconds=settings.embedding_timeout_seconds,
    )


def get_embedding_search_config() -> EmbeddingSearchConfig:
    """Build trusted Slice 8 provenance from application configuration.

    Natural-language planner output never supplies any of these fields.
    """
    settings = get_settings()
    return EmbeddingSearchConfig(
        provider=settings.embedding_provider,
        model_name=settings.ollama_embedding_model,
        model_revision="",
        serializer_version=SERIALIZER_VERSION,
        embedding_dimensions=settings.embedding_dimensions,
    )
