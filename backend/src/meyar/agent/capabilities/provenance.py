"""Privacy-safe plan provenance (issue #88 slice B, D-092 §19.2).

``agent.plan.validated`` carries EXACTLY the closed keys ``plan_sha256``,
``step_count``, ``capabilities`` and ``schema_version``. ``plan_sha256`` is
SHA-256 over the canonical JSON of the VALIDATED plan in which every piece of
text — the server-resolved source (WHOLE_MESSAGE / bound resume span / JD
span) and the transitional model text fields (refine filter, evidence topic)
— is replaced by its SHA-256 and length. The canonical form holds only closed
codes (schema/policy versions, origin, goal, capabilities, the WHOLE_MESSAGE
mode, affordance targets), bounded integers (ordinals, limits, span offsets)
and those fingerprints: no HR/JD/CV/evidence text, no identity, no UUID, no
raw model output. The canonical form itself is never persisted."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from meyar.agent.capabilities.contracts import ExecutablePlan, ValidatedStep

# Closed literal values allowed to stay verbatim in the canonical args.
_CLOSED_TEXT_KEYS = frozenset({"mode"})


def _fingerprint(text: str) -> dict[str, object]:
    return {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "length": len(text)}


def _safe(value: Any, *, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {k: _safe(v, key=k) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_safe(item) for item in value]
    if isinstance(value, str):
        return value if key in _CLOSED_TEXT_KEYS else _fingerprint(value)
    if value is None or isinstance(value, bool | int):
        return value
    raise TypeError(f"Unsupported plan value type: {type(value).__name__}")


def _canonical_step(step: ValidatedStep) -> dict[str, object]:
    return {
        "capability": step.capability.value,
        "policy_version": step.policy_version,
        "args": _safe(step.args.model_dump(mode="json")),
        "resolved_source": (
            None if step.resolved_text is None else _fingerprint(step.resolved_text)
        ),
    }


def canonical_plan(plan: ExecutablePlan) -> dict[str, object]:
    return {
        "schema_version": plan.schema_version,
        "origin": plan.origin.value,
        "goal": plan.goal.value,
        "steps": [_canonical_step(step) for step in plan.steps],
        "affordances": [
            {"capability": a.capability.value, "live_target": a.live_target.value}
            for a in plan.affordances
        ],
    }


def plan_sha256(plan: ExecutablePlan) -> str:
    encoded = json.dumps(
        canonical_plan(plan), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def plan_validated_metadata(plan: ExecutablePlan) -> dict[str, object]:
    """The exact ``agent.plan.validated`` metadata (closed keys only)."""
    capabilities = [step.capability.value for step in plan.steps] + [
        affordance.capability.value for affordance in plan.affordances
    ]
    return {
        "plan_sha256": plan_sha256(plan),
        "step_count": len(capabilities),
        "capabilities": capabilities,
        "schema_version": plan.schema_version,
    }
