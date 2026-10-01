"""Privacy-safe plan provenance (issue #88 slices B/C, D-092 §19.2).

``agent.plan.validated`` carries EXACTLY the closed keys ``plan_sha256``,
``step_count``, ``capabilities`` and ``schema_version``. ``plan_sha256`` is
SHA-256 over the canonical JSON of the VALIDATED plan in which every quote /
source is replaced by its server-resolved offsets and span SHA-256 (§19.2).
The canonical form holds only closed codes (schema/policy versions, origin,
goal, capabilities, grounded-field/mode codes, affordance targets), bounded
integers (server-parsed ordinals/limits, span offsets) and span hashes: no
HR/JD/CV/evidence/quote text, no identity, no UUID, no raw model output. The
canonical form itself is never persisted.

Stable: the same validated plan (same capabilities, same grounded spans of
the same message, same parsed values) always hashes identically; a change of
capability, span, parsed value or source text changes the hash."""

from __future__ import annotations

import hashlib
import json

from meyar.agent.capabilities.contracts import ExecutablePlan, GroundingRecord, ValidatedStep


def _canonical_record(record: GroundingRecord) -> dict[str, object]:
    return {
        "field": record.field.value,
        "mode": record.mode.value,
        "spans": [
            {"start": span.start, "end": span.end, "sha256": span.sha256}
            for span in record.spans
        ],
    }


def _canonical_step(step: ValidatedStep) -> dict[str, object]:
    return {
        "capability": step.capability.value,
        "policy_version": step.policy_version,
        "candidate_ref": step.candidate_ref,
        "limit": step.limit,
        "grounding": [_canonical_record(record) for record in step.grounding],
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
