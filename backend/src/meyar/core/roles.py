"""Centralized human role → permission mapping (Slice 1 — Human Identity
& Dual Access, issue #30). Reuses the exact same scope vocabulary the
machine API-key path already enforces (meyar.services.api_key_repo.
DEFAULT_SCOPES) — the human role model is a second, independent way to
grant that same permission vocabulary, not a parallel one. Routers and
templates must never compare a role-name string directly; they depend on
`permissions_for_role` (or the `require_ui_scopes` dependency built on
it) so the mapping stays in exactly one place.

Deliberately minimal per docs' non-goal on "complex enterprise RBAC":
current repository evidence shows no UI action that is actually
admin-only today (see the Slice 1 investigation notes in
docs/DECISIONS.md), so both roles currently resolve to the same full
permission set an HR user already had via any default-scoped API key.
ADMIN exists now only so CLI provisioning and the schema don't require a
breaking change the day an admin-only capability is actually added."""

from collections.abc import Mapping

ROLE_HR_USER = "HR_USER"
ROLE_ADMIN = "ADMIN"

VALID_ROLES = frozenset({ROLE_HR_USER, ROLE_ADMIN})

# Same vocabulary as meyar.services.api_key_repo.DEFAULT_SCOPES.
_FULL_HR_PERMISSIONS = frozenset(
    {
        "jobs:read",
        "jobs:write",
        "candidates:read",
        "candidates:write",
        "evaluations:read",
        "evaluations:write",
    }
)

_ROLE_PERMISSIONS: Mapping[str, frozenset[str]] = {
    ROLE_HR_USER: _FULL_HR_PERMISSIONS,
    ROLE_ADMIN: _FULL_HR_PERMISSIONS,
}


def permissions_for_role(role: str) -> frozenset[str]:
    """Unknown role names resolve to zero permissions — fail closed, never
    fail open on a role that isn't recognized."""
    return _ROLE_PERMISSIONS.get(role, frozenset())


def is_valid_role(role: str) -> bool:
    return role in VALID_ROLES
