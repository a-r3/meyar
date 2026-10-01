"""Capability registry, Layer-1 validator, Layer-2 execution and the
transitional adapter (issue #88 slice B; D-092 §8–§11, §23; D-094).

Behaviour-preserving plumbing: every IN_TURN executor wraps today's
``meyar.agent.service._dispatch_*`` unchanged; CREATE_JOB and
RANK_JOB_CANDIDATES are HUMAN_ACTION_ONLY and never execute in a turn."""

from meyar.agent.capabilities.contracts import (
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
)
from meyar.agent.capabilities.validator import validate_plan

__all__ = [
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
    "validate_plan",
]
