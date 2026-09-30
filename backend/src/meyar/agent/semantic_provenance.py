"""Durable, immutable semantic provenance for an agent-confirmed
``JobCriteriaVersion`` (issue #84, D-088 amendment).

The pending JD draft is session/transcript state and is bounded/retained as
such. Once HR confirms it, this record is persisted WITH the immutable
criteria version so an auditor can later prove, per scoring criterion:

- which exact source fragment (span id + offsets + exact text) it came from;
- whether its canonical shape was ``DETERMINISTIC`` or ``MODEL_VALIDATED``;
- which semantic policy / prompt / local model identity produced it;
- which explicit human review decisions affected ranking
  (``MUST_HAVE`` / ``PREFERRED`` / ``EXCLUDED_BY_REVIEWER``).

It never contains the full JD (only ``source_sha256`` of it), raw model
output, chain-of-thought, candidate data or PII. It NEVER influences scoring:
the deterministic scorer consumes only the persisted ``CriterionIn`` rows.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from meyar.agent.canonical_requirements import subject_grounded_in_span
from meyar.agent.schemas import (
    MAX_JD_REQUIREMENT_SPANS,
    AgentJobDraftToolResult,
    JDDraftCriterionKind,
    RequirementSpanState,
    SemanticInterpretationSource,
    SemanticModelProvenance,
    SemanticReviewDecision,
    SemanticReviewDecisionKind,
)
from meyar.schemas.criteria import CriterionIn, CriterionKind

AGENT_SEMANTIC_PROVENANCE_SCHEMA_VERSION = "jd-semantic-provenance-v1"

# Kinds whose canonical value must be attributable to the recorded exact
# fragment (contiguous tokens or a reviewed alias) — the same grounding rule
# the canonical boundary applied when the draft was built.
_GROUNDED_KINDS = {
    CriterionKind.SKILL: JDDraftCriterionKind.SKILL,
    CriterionKind.SKILL_EXPERIENCE: JDDraftCriterionKind.SKILL_EXPERIENCE,
    CriterionKind.DOMAIN_EXPERIENCE: JDDraftCriterionKind.DOMAIN_EXPERIENCE,
    CriterionKind.LANGUAGE: JDDraftCriterionKind.LANGUAGE,
}


class CriterionSemanticProvenance(BaseModel):
    model_config = {"extra": "forbid", "frozen": True}

    criterion_id: str = Field(pattern=r"^[a-z0-9_]{1,64}$")
    span_id: str = Field(pattern=r"^req-\d{4}$")
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    source_text: str = Field(min_length=1, max_length=4000)
    interpretation_source: SemanticInterpretationSource

    @model_validator(mode="after")
    def _validate_offsets(self) -> CriterionSemanticProvenance:
        if self.end_offset <= self.start_offset:
            raise ValueError("Provenance offsets must describe a non-empty range.")
        if len(self.source_text) != self.end_offset - self.start_offset:
            raise ValueError("Provenance source text must match its exact offsets.")
        return self


class AgentSemanticProvenance(BaseModel):
    """Strict schema for ``job_criteria_versions.agent_semantic_provenance``.

    Database JSON is never trusted: every read goes through
    ``parse_agent_semantic_provenance``."""

    model_config = {"extra": "forbid", "frozen": True}

    schema_version: Literal["jd-semantic-provenance-v1"] = "jd-semantic-provenance-v1"
    draft_id: uuid.UUID
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_policy_version: str = Field(min_length=1, max_length=64)
    prompt_version: str | None = Field(default=None, min_length=1, max_length=64)
    model: SemanticModelProvenance | None = None
    rejected_proposal_count: int = Field(default=0, ge=0)
    criteria: list[CriterionSemanticProvenance] = Field(
        default_factory=list, max_length=MAX_JD_REQUIREMENT_SPANS
    )
    review_decisions: list[SemanticReviewDecision] = Field(
        default_factory=list, max_length=MAX_JD_REQUIREMENT_SPANS
    )

    @model_validator(mode="after")
    def _validate_uniqueness(self) -> AgentSemanticProvenance:
        criterion_ids = [item.criterion_id for item in self.criteria]
        span_ids = [item.span_id for item in self.criteria]
        if len(set(criterion_ids)) != len(criterion_ids):
            raise ValueError("Duplicate criterion provenance.")
        if len(set(span_ids)) != len(span_ids):
            raise ValueError("One source span cannot provenance two criteria.")
        decision_spans = [item.span_id for item in self.review_decisions]
        if len(set(decision_spans)) != len(decision_spans):
            raise ValueError("Duplicate review decision for one source span.")
        excluded = {
            item.span_id
            for item in self.review_decisions
            if item.decision == SemanticReviewDecisionKind.EXCLUDED_BY_REVIEWER
        }
        if excluded & set(span_ids):
            raise ValueError("An excluded source span cannot provenance a criterion.")
        if any(
            item.interpretation_source == SemanticInterpretationSource.MODEL_VALIDATED
            for item in self.criteria
        ) and self.model is None:
            raise ValueError("A model-validated criterion requires model provenance.")
        return self


class SemanticProvenanceError(ValueError):
    """The confirmation boundary could not establish exact provenance."""


def build_agent_semantic_provenance(
    draft: AgentJobDraftToolResult, criteria: list[CriterionIn]
) -> AgentSemanticProvenance:
    """Fail-closed: exactly one server-authorized source span per confirmed
    criterion, consistent with the immutable pending draft. Browser form
    data is never an input here."""
    if not draft.semantic_policy_version:
        raise SemanticProvenanceError("Semantic policy version is missing.")
    if draft.source_sha256 is None:
        raise SemanticProvenanceError("Source digest is missing.")
    draft_criteria = {item.id: item for item in [*draft.must_have, *draft.preferred]}
    if len(draft_criteria) != len(draft.must_have) + len(draft.preferred):
        raise SemanticProvenanceError("Duplicate draft criterion ids.")
    scorable = [
        item for item in draft.requirements if item.state == RequirementSpanState.SCORABLE
    ]
    span_ids = [item.span_id for item in draft.requirements]
    if len(set(span_ids)) != len(span_ids):
        raise SemanticProvenanceError("Duplicate source spans.")
    confirmed_ids = [criterion.id for criterion in criteria]
    if len(set(confirmed_ids)) != len(confirmed_ids):
        raise SemanticProvenanceError("Duplicate confirmed criterion.")
    if {item.criterion_id for item in scorable} != set(confirmed_ids):
        raise SemanticProvenanceError("Confirmed criteria do not match scorable spans.")

    records: list[CriterionSemanticProvenance] = []
    for criterion in criteria:
        matches = [item for item in scorable if item.criterion_id == criterion.id]
        if len(matches) != 1:
            raise SemanticProvenanceError("Criterion has no unique source provenance.")
        result = matches[0]
        expected = draft_criteria.get(criterion.id)
        if expected is None or expected != criterion:
            raise SemanticProvenanceError("Confirmed criterion differs from the draft.")
        if result.text is None or result.interpretation_source is None:
            raise SemanticProvenanceError("Criterion source provenance is incomplete.")
        grounding_kind = _GROUNDED_KINDS.get(criterion.kind)
        if (
            grounding_kind is not None
            and criterion.value
            and not subject_grounded_in_span(grounding_kind, criterion.value, result.text)
        ):
            raise SemanticProvenanceError("Criterion is not grounded in its source fragment.")
        try:
            records.append(
                CriterionSemanticProvenance(
                    criterion_id=criterion.id,
                    span_id=result.span_id,
                    start_offset=result.start_offset,
                    end_offset=result.end_offset,
                    source_text=result.text,
                    interpretation_source=result.interpretation_source,
                )
            )
        except ValidationError as exc:
            raise SemanticProvenanceError("Criterion source offsets are inconsistent.") from exc

    known_spans = set(span_ids)
    for decision in draft.review_decisions:
        if decision.span_id not in known_spans:
            raise SemanticProvenanceError("Review decision references an unknown span.")
    excluded_by_items = {
        item.span_id for item in draft.needs_review if item.acknowledged_excluded
    }
    excluded_by_decisions = {
        item.span_id
        for item in draft.review_decisions
        if item.decision == SemanticReviewDecisionKind.EXCLUDED_BY_REVIEWER
    }
    if excluded_by_items != excluded_by_decisions:
        raise SemanticProvenanceError("Review exclusions are inconsistent.")
    try:
        return AgentSemanticProvenance(
            draft_id=draft.draft_id,
            source_sha256=draft.source_sha256,
            semantic_policy_version=draft.semantic_policy_version,
            prompt_version=draft.semantic_prompt_version,
            model=draft.semantic_model,
            rejected_proposal_count=draft.rejected_proposal_count,
            criteria=records,
            review_decisions=list(draft.review_decisions),
        )
    except ValidationError as exc:
        raise SemanticProvenanceError("Semantic provenance is inconsistent.") from exc


def parse_agent_semantic_provenance(raw: Any) -> AgentSemanticProvenance | None:
    """Strictly validate persisted JSON. ``None`` means "not an agent
    semantic version" (manual/API/legacy rows); malformed JSON raises."""
    if raw is None:
        return None
    return AgentSemanticProvenance.model_validate(raw)
