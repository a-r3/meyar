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
