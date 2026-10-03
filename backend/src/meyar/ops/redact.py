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


def release_identity_text(*, release_id: str, release_version: str, source_sha: str) -> str:
    """Explicit success inventory accepts a closed version/commit identity only.

    The manifest schema's free strings are not a diagnostic allowlist. This
    display rule changes no artifact identity, manifest validation or lifecycle.
    Unknown versions remain available to their product consumer, not message copy.
    """
    version_shape = r"[0-9]{1,6}(?:\.[0-9]{1,6}){0,3}(?:(?:a|b|rc)[0-9]{1,6})?"
    if (
        re.fullmatch(version_shape, release_version)
        and re.fullmatch(r"[0-9a-f]{40}", source_sha)
        and release_id == f"meyar-{release_version}+{source_sha[:12]}"
    ):
        return f"release_id={release_id} release_version={release_version}"
    return "release identity metadata available"
