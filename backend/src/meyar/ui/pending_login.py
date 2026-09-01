"""Short-lived, unpersisted pending-login token bridging password
verification and an explicit tenant choice for a user with more than one
active TenantMembership (Slice 1, issue #30). Deliberately not a new auth
system or a DB row: it proves only "this user_id already had their
password verified within the last few minutes" — the tenant/membership
itself is always re-validated server-side against the live database
before a real BrowserSession is ever created (see meyar.ui.router).
"""

import hashlib
import hmac
import time
import uuid

_TTL_SECONDS = 300


def issue_pending_login_token(*, secret: str, user_id: uuid.UUID) -> str:
    expires_at = int(time.time()) + _TTL_SECONDS
    payload = f"{user_id}:{expires_at}"
    signature = hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{payload}:{signature}"


def verify_pending_login_token(*, secret: str, token: str) -> uuid.UUID | None:
    """Returns the authenticated user_id, or None on any malformed,
    expired, or mis-signed token — never raises on untrusted input."""
    parts = token.split(":")
    if len(parts) != 3:
        return None
    user_id_text, expires_text, signature = parts
    payload = f"{user_id_text}:{expires_text}"
    expected_signature = hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_signature, signature):
        return None
    try:
        expires_at = int(expires_text)
        user_id = uuid.UUID(user_id_text)
    except ValueError:
        return None
    if expires_at < int(time.time()):
        return None
    return user_id
