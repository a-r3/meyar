"""Short-lived signed password-verification claim, bound to security stamps."""

import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass

_TTL_SECONDS = 300


@dataclass(frozen=True)
class PendingLoginClaim:
    user_id: uuid.UUID
    user_security_version: uuid.UUID
    membership_versions: dict[uuid.UUID, uuid.UUID]


def issue_pending_login_token(
    *, secret: str, user_id: uuid.UUID, user_security_version: uuid.UUID,
    membership_versions: dict[uuid.UUID, uuid.UUID],
) -> str:
    expires_at = int(time.time()) + _TTL_SECONDS
    members = ",".join(
        f"{member_id}.{version}"
        for member_id, version in sorted(membership_versions.items(), key=lambda item: str(item[0]))
    )
    payload = f"{user_id}:{expires_at}:{user_security_version}:{members}"
    signature = hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{payload}:{signature}"


def verify_pending_login_token(*, secret: str, token: str) -> PendingLoginClaim | None:
    """Verify integrity only; caller must reload User and membership state."""
    if len(token) > 4096:
        return None
    parts = token.split(":")
    if len(parts) != 5:
        return None
    user_id_text, expires_text, version_text, members_text, signature = parts
    payload = ":".join(parts[:-1])
    expected_signature = hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_signature.encode("ascii"), signature.encode("utf-8")):
        return None
    try:
        expires_at = int(expires_text)
        user_id = uuid.UUID(user_id_text)
        version = uuid.UUID(version_text)
        members = {}
        for pair in members_text.split(","):
            membership_id, membership_version = pair.split(".")
            members[uuid.UUID(membership_id)] = uuid.UUID(membership_version)
    except (ValueError, TypeError):
        return None
    if expires_at < int(time.time()) or not members:
        return None
    return PendingLoginClaim(user_id, version, members)
