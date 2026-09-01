"""Human-user password hashing. Argon2id (OWASP's first-recommendation
password hash) via `argon2-cffi` — a pure local computation library, no
network dependency, consistent with the local-only boundary the rest of
the platform enforces. See docs/DECISIONS.md for the slice decision
record.

Only this module and meyar.services.user_repo touch a plaintext password.
Never log a plaintext password, a hash, or pass either through
meyar.services.audit_repo metadata."""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerifyMismatchError

# Default argon2-cffi parameters (time_cost=3, memory_cost=64MB,
# parallelism=4) are OWASP-baseline-appropriate for an interactively
# submitted login form. The encoded hash string itself carries its
# algorithm/version/parameters, so a future tuning change never
# invalidates already-stored hashes — verify() reads the params back out
# of the hash, and needs_rehash() flags rows that should be upgraded on
# next successful login.
_hasher = PasswordHasher()


def hash_password(plaintext: str) -> str:
    """Return a salted, versioned Argon2id hash. Never raises on the
    caller's input; the hash string alone is safe to persist."""
    return _hasher.hash(plaintext)


def verify_password(password_hash: str, plaintext: str) -> bool:
    """Constant-time-verified match, provided by argon2-cffi itself. Any
    malformed/foreign hash is treated as a non-match, never an error."""
    try:
        return _hasher.verify(password_hash, plaintext)
    except (VerifyMismatchError, InvalidHash):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when a stored hash was produced with weaker-than-current
    parameters and should be silently upgraded on next successful login."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHash:
        return True
