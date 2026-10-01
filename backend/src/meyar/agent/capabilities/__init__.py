"""Capability registry, Layer-1 validator with §10.2 source grounding,
Layer-2 execution, the ``agent-plan-v1`` model contract and server-built
plans (issue #88 slices B/C; D-092 §8–§12, §23; D-094 / D-095).

Every IN_TURN executor wraps today's ``meyar.agent.service._dispatch_*``
unchanged and receives only server-resolved values; CREATE_JOB and
RANK_JOB_CANDIDATES are HUMAN_ACTION_ONLY and never execute in a turn."""

from meyar.agent.capabilities.contracts import (
    AGENT_PLAN_SCHEMA_VERSION,
    CAPABILITY_PLAN_SCHEMA_VERSION,
    MAX_PLAN_STEPS,
    CapabilityName,
    CapabilityOutcome,
    ExecutablePlan,
    ExecutionContext,
    PlanExecution,
    PlanOrigin,
    PlanRejection,
    PlanRejectionCode,
    PlanStatus,
    ResultSetContext,
    ResultSetStatus,
    StepFailureReason,
    ValidatedStep,
    ValidationContext,
)
from meyar.agent.capabilities.execution import execute_plan
from meyar.agent.capabilities.registry import (
    CAPABILITY_REGISTRY,
    CapabilityDefinition,
    assert_registry_invariants,
    capability_audit_metadata,
    offered_capabilities,
)
from meyar.agent.capabilities.validator import validate_plan

__all__ = [
    "AGENT_PLAN_SCHEMA_VERSION",
    "CAPABILITY_PLAN_SCHEMA_VERSION",
    "CAPABILITY_REGISTRY",
    "MAX_PLAN_STEPS",
    "CapabilityDefinition",
    "CapabilityName",
    "CapabilityOutcome",
    "ExecutablePlan",
    "ExecutionContext",
    "PlanExecution",
    "PlanOrigin",
    "PlanRejection",
    "PlanRejectionCode",
    "PlanStatus",
    "ResultSetContext",
    "ResultSetStatus",
    "StepFailureReason",
    "ValidatedStep",
    "ValidationContext",
    "assert_registry_invariants",
    "capability_audit_metadata",
    "execute_plan",
    "offered_capabilities",
    "validate_plan",
]
