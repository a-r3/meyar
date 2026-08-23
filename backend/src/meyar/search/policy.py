"""meyar-search-v1 — the deterministic Slice 8 hybrid-search ranking
policy. Every constant/formula a search result depends on lives here, not
scattered across the service, so Slice 9/11/12 can consume it without
reverse-engineering ranking semantics. See docs/DECISIONS.md."""

import math

SEARCH_POLICY_VERSION = "meyar-search-v1"

DEFAULT_STRUCTURED_WEIGHT = 0.5
DEFAULT_SEMANTIC_WEIGHT = 0.5

# Tolerance for validating structured_weight + semantic_weight == 1.0.
WEIGHT_SUM_TOLERANCE = 1e-6

MIN_SEARCH_LIMIT = 1
MAX_SEARCH_LIMIT = 100
MAX_SEMANTIC_QUERY_LENGTH = 2000


def is_valid_query_vector(vector: list[float]) -> bool:
    """Defense-in-depth boundary validation for a query embedding vector,
    independent of any specific EmbeddingProvider implementation
    (OllamaEmbeddingProvider already validates this for its own HTTP
    response, but the search boundary must not blindly trust every
    current/future provider to do the same). Non-empty, every value a
    finite real number (no NaN/+-Inf) — zero-norm is checked separately
    by the caller since it is only invalid for a cosine-distance search."""
    return bool(vector) and all(isinstance(v, int | float) and math.isfinite(v) for v in vector)


def cosine_distance_to_similarity(distance: float) -> float:
    """pgvector's `<=>` operator (cosine_distance) returns 1 - cosine
    similarity. This is the one place that inverse is taken — never treat
    a raw pgvector distance as a similarity/relevance score elsewhere."""
    return 1.0 - distance


def normalize_semantic_score(cosine_similarity: float) -> float:
    """Cosine similarity is in [-1, 1]; maps it to a bounded [0, 1]
    semantic_score via (similarity + 1) / 2, clamped for floating-point
    tolerance. This is the ONLY normalization used anywhere in Slice 8 —
    see docs/DECISIONS.md for why this exact formula was chosen."""
    score = (cosine_similarity + 1.0) / 2.0
    return max(0.0, min(1.0, score))


def compute_hybrid_score(
    structured_score: float,
    semantic_score: float,
    structured_weight: float,
    semantic_weight: float,
) -> float:
    """hybrid_score = (structured_weight * structured_score) +
    (semantic_weight * semantic_score). This is a SEARCH RELEVANCE score
    (0-1), never a hiring score, JD fit score, or 0-100 score — Slice 10
    owns official JD scoring."""
    return (structured_weight * structured_score) + (semantic_weight * semantic_score)
