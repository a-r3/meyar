import re

_WHITESPACE_RE = re.compile(r"\s+")

# Deterministic, explicitly curated aliases only — never fuzzy/embedding
# matching. Adding an alias is a one-line, reviewable, auditable change.
# "Java" must never equal "JavaScript" — no entry below conflates them.
SKILL_ALIASES: dict[str, str] = {
    "js": "javascript",
    "ts": "typescript",
    "postgres": "postgresql",
    "psql": "postgresql",
    "py": "python",
    "k8s": "kubernetes",
    "golang": "go",
}


def normalize_text(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", value).strip().lower()


def normalize_skill_name(value: str) -> str:
    normalized = normalize_text(value)
    return SKILL_ALIASES.get(normalized, normalized)


def accepted_skill_terms(value: str) -> frozenset[str]:
    """All explicitly curated spellings that resolve to ``value``.

    This is the inverse view of ``SKILL_ALIASES`` used by claim-specific
    extraction evidence validation. Keeping it here guarantees evidence
    acceptance and deterministic skill matching use the same canonical
    alias authority; no fuzzy or model-judged synonym is introduced.
    """
    canonical = normalize_skill_name(value)
    return frozenset(
        {canonical}
        | {
            alias
            for alias, alias_canonical in SKILL_ALIASES.items()
            if alias_canonical == canonical
        }
    )
