"""Trusted application principals (issue #45, D-116).

A principal is produced ONLY by server-side authentication:

- ``MachinePrincipal`` — an active API key on an active tenant
  (``meyar.core.auth.get_current_tenant``). Machine authority is tenant-scoped
  data access. It is never an accountable human and can never confirm a JD
  draft or own a conversation.
- ``HumanPrincipal`` — a live User + TenantMembership + BrowserSession on an
  active tenant, re-derived on every request
  (``meyar.ui.auth.resolve_ui_context``). The only authority for consequential
  human actions.

No operation accepts a client- or model-supplied tenant/user/session id as
authority; identifiers in request bodies are only *references* that must
resolve inside the principal's own scope."""

import uuid
from dataclasses import dataclass
from typing import Protocol

from meyar.services.audit_repo import ACTOR_API_KEY, ACTOR_HUMAN_USER


class Principal(Protocol):
    """What every shared operation may rely on, regardless of credential type."""

    @property
    def tenant_id(self) -> uuid.UUID: ...

    @property
    def actor_type(self) -> str: ...

    @property
    def actor_id(self) -> uuid.UUID: ...


@dataclass(frozen=True)
class MachinePrincipal:
    tenant_id: uuid.UUID
    api_key_id: uuid.UUID
    scopes: list[str]

    @property
    def actor_type(self) -> str:
        return ACTOR_API_KEY

    @property
    def actor_id(self) -> uuid.UUID:
        return self.api_key_id


@dataclass(frozen=True)
class HumanPrincipal:
    session_id: uuid.UUID
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    membership_id: uuid.UUID
    role: str
    scopes: frozenset[str]

    @property
    def actor_type(self) -> str:
        return ACTOR_HUMAN_USER

    @property
    def actor_id(self) -> uuid.UUID:
        return self.user_id

    def owner(self):  # noqa: ANN201 - OwnerPrincipal (lazy import, avoids a cycle)
        """The durable-conversation owner is always the live principal."""
        from meyar.services.agent_conversation_repo import OwnerPrincipal

        return OwnerPrincipal(
            tenant_id=self.tenant_id, user_id=self.user_id, membership_id=self.membership_id
        )
