import hashlib
import secrets

PREFIX_DISPLAY_LEN = 12


def generate_api_key(env: str) -> tuple[str, str, str]:
    """Generate a new API key. Returns (plaintext, prefix, key_hash).

    The plaintext is the caller's responsibility to show exactly once and
    never persist or log. Only `prefix` (safe to display/log) and
    `key_hash` (SHA-256 of the plaintext) are meant for storage."""
    secret = secrets.token_urlsafe(32)
    plaintext = f"meyar_{env}_{secret}"
    prefix = plaintext[:PREFIX_DISPLAY_LEN]
    key_hash = hash_api_key(plaintext)
    return plaintext, prefix, key_hash


def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
