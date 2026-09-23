"""No-exfiltration text helpers shared by every ops check. A Finding
message must never carry a secret, credential, full database URL,
session value, or environment dump (see docs/MEYAR_OPS.md and
`.claude/rules/security-privacy.md`) — these helpers are the single
place that turns an arbitrary exception/URL into safe, bounded text."""

from __future__ import annotations

import re

# Matches the credential portion of any "<scheme>://user:pass@host" URL
# (asyncpg/psycopg DSNs, and defensively any other URL-shaped secret that
# might appear inside a driver exception message).
_CREDENTIAL_IN_URL = re.compile(r"://[^/@\s]+@")

_MAX_MESSAGE_CHARS = 300


def redact_database_url(url: str) -> str:
    """Host/port/database name only — never the scheme's user:password."""
    sanitized = _CREDENTIAL_IN_URL.sub("://<redacted>@", url)
    return sanitized.split("@")[-1] if "@" in sanitized else sanitized


def safe_exception_text(exc: BaseException) -> str:
    """Bounded, credential-stripped text safe to place in a Finding
    message or CLI error output. Never the exception's full repr/args,
    which for a DB/HTTP driver can embed a DSN or response body."""
    text = _CREDENTIAL_IN_URL.sub("://<redacted>@", str(exc))
    text = " ".join(text.split())  # collapse newlines/whitespace
    if len(text) > _MAX_MESSAGE_CHARS:
        text = text[: _MAX_MESSAGE_CHARS - 1] + "…"
    return text or exc.__class__.__name__
