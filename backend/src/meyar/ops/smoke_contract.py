"""Fixed capability codes required for installed synthetic smoke evidence."""

import re

POSTGRES_IMAGE_REF = re.compile(r"pgvector/pgvector@sha256:([0-9a-f]{64})\Z")
POSTGRES_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def postgres_image_digest(reference: str) -> str | None:
    """Accept only an exact pgvector manifest digest supplied for rehearsal."""
    match = POSTGRES_IMAGE_REF.fullmatch(reference)
    return f"sha256:{match.group(1)}" if match else None


REQUIRED_CHECKS = frozenset(
    {
        "fresh_schema",
        "auth_login",
        "synthetic_cv_ingestion",
        "local_extraction",
        "local_embedding",
        "candidate_library_detail",
        "structured_search",
        "vacancy_flow",
        "deterministic_scoring",
        "deterministic_ranking",
        "original_cv_authorization",
        "restart_persistence",
    }
)
