"""No-exfiltration text helpers shared by every ops check. A Finding
message must never carry a secret, credential, full database URL,
session value, or environment dump (see docs/MEYAR_OPS.md and
`.claude/rules/security-privacy.md`) — these helpers are the single
place that turns an arbitrary exception/URL into safe, bounded text."""

from __future__ import annotations

import re

from meyar.diagnostics import exception_type

# Matches the credential portion of any "<scheme>://user:pass@host" URL
# (asyncpg/psycopg DSNs, and defensively any other URL-shaped secret that
# might appear inside a driver exception message).
_CREDENTIAL_IN_URL = re.compile(r"://[^/@\s]+@")

def redact_database_url(url: str) -> str:
    """Host/port/database name only — never the scheme's user:password."""
    sanitized = _CREDENTIAL_IN_URL.sub("://<redacted>@", url)
    return sanitized.split("@")[-1] if "@" in sanitized else sanitized


def safe_exception_text(exc: BaseException) -> str:
    """Exception class only. URL redaction cannot remove arbitrary PII/secrets.

    Never evaluate str/repr/args or format a cause/traceback. The calling
    Finding supplies its existing closed component and reason code.
    """
    return exception_type(exc)
