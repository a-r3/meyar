"""Fixed capability codes required for installed synthetic smoke evidence."""

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
