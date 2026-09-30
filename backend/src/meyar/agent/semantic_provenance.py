"""Durable, immutable semantic provenance for an agent-confirmed
``JobCriteriaVersion`` (issue #84, D-088 amendments A-1 and A-4).

The pending JD draft is session/transcript state and is bounded/retained as
such. Once HR confirms it, this record is persisted WITH the immutable
criteria version so an auditor can later prove, per scoring criterion:

- every exact source fragment that supports it (span id + offsets + exact
  text) — one criterion may be supported by several duplicate spans, but it
  is still ONE weight;
- whether each fragment's canonical shape was ``DETERMINISTIC`` or
  ``MODEL_VALIDATED``;
- the source-derived ``origin`` parameters (modality, duration, level) and,
  when different, exactly which explicit human decision produced them: a
  review decision, a semantic-conflict choice, and/or an ordered chain of
  bounded HR follow-up amendments ending in the confirmed ``final`` values;
- which semantic policy / prompt / local model identity produced it.

Schema ``jd-semantic-provenance-v2`` supersedes v1, which was never part of
an accepted release; v1 records are not accepted on read.

It never contains the full JD (only ``source_sha256`` of it), raw model
output, chain-of-thought, candidate data or PII. Amendment ``source_text`` is
the HR follow-up instruction that authorized the change. It NEVER influences
scoring: the deterministic scorer consumes only the persisted ``CriterionIn``
rows.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from meyar.agent.canonical_requirements import (
    semantic_identity,
    semantic_parameters,
    subject_grounded_in_span,
)
from meyar.agent.schemas import (
    MAX_JD_REQUIREMENT_SPANS,
    AgentJobDraftToolResult,
    JDDraftCriterionKind,
    RequirementSpanState,
    SemanticConflictResolution,
    SemanticCriterionAmendment,
    SemanticCriterionAmendmentField,
    SemanticInterpretationSource,
    SemanticModelProvenance,
    SemanticParameters,
    SemanticReviewDecision,
    SemanticReviewDecisionKind,
    encode_amendment_value,
)
from meyar.agent.semantic_requirements import analyze_hr_text
from meyar.schemas.criteria import CriterionIn, CriterionKind, CriterionType

AGENT_SEMANTIC_PROVENANCE_SCHEMA_VERSION = "jd-semantic-provenance-v2"

# Kinds whose canonical value must be attributable to every recorded exact
# fragment (contiguous tokens or a reviewed alias) — the same grounding rule
# the canonical boundary applied when the draft was built.
_GROUNDED_KINDS = {
    CriterionKind.SKILL: JDDraftCriterionKind.SKILL,
    CriterionKind.SKILL_EXPERIENCE: JDDraftCriterionKind.SKILL_EXPERIENCE,
    CriterionKind.DOMAIN_EXPERIENCE: JDDraftCriterionKind.DOMAIN_EXPERIENCE,
    CriterionKind.LANGUAGE: JDDraftCriterionKind.LANGUAGE,
}


class SourceSpanProvenance(BaseModel):
    model_config = {"extra": "forbid", "frozen": True}

    span_id: str = Field(pattern=r"^req-\d{4}$")
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    source_text: str = Field(min_length=1, max_length=4000)
    interpretation_source: SemanticInterpretationSource

    @model_validator(mode="after")
    def _validate_offsets(self) -> SourceSpanProvenance:
        if self.end_offset <= self.start_offset:
            raise ValueError("Provenance offsets must describe a non-empty range.")
        if len(self.source_text) != self.end_offset - self.start_offset:
            raise ValueError("Provenance source text must match its exact offsets.")
        return self


class CriterionSemanticProvenance(BaseModel):
    model_config = {"extra": "forbid", "frozen": True}

    criterion_id: str = Field(pattern=r"^[a-z0-9_]{1,64}$")
    source_spans: list[SourceSpanProvenance] = Field(
        min_length=1, max_length=MAX_JD_REQUIREMENT_SPANS
    )
    # Source-derived parameters after explicit review/conflict decisions and
    # BEFORE any follow-up amendment.
    origin: SemanticParameters
    # The confirmed CriterionIn parameters.
    final: SemanticParameters

    @model_validator(mode="after")
    def _validate_spans(self) -> CriterionSemanticProvenance:
        ids = [span.span_id for span in self.source_spans]
        if len(set(ids)) != len(ids):
            raise ValueError("A source span is listed twice for one criterion.")
        return self


def _parameter_value(parameters: SemanticParameters, field: SemanticCriterionAmendmentField) -> Any:
    if field == SemanticCriterionAmendmentField.CRITERION_TYPE:
        return parameters.criterion_type
    if field == SemanticCriterionAmendmentField.MIN_YEARS:
        return parameters.min_years
    return parameters.required_level


def _with_parameter(
    parameters: SemanticParameters, field: SemanticCriterionAmendmentField, encoded: str
) -> SemanticParameters:
    if field == SemanticCriterionAmendmentField.CRITERION_TYPE:
        return parameters.model_copy(update={"criterion_type": CriterionType(encoded)})
    if field == SemanticCriterionAmendmentField.MIN_YEARS:
        return parameters.model_copy(update={"min_years": float(encoded)})
    return parameters.model_copy(update={"required_level": encoded})


def replay_amendments(
    origin: SemanticParameters,
    amendments: list[SemanticCriterionAmendment],
) -> SemanticParameters:
    """Apply one criterion's ordered amendments; every ``previous_value`` must
    equal the value it replaces (an unbroken, exact human transition chain)."""
    current = origin
    for amendment in amendments:
        value = _parameter_value(current, amendment.field)
        if value is None or encode_amendment_value(amendment.field, value) != (
            amendment.previous_value
        ):
            raise ValueError("Amendment chain is broken.")
        current = _with_parameter(current, amendment.field, amendment.new_value)
    return current


class AgentSemanticProvenance(BaseModel):
    """Strict schema for ``job_criteria_versions.agent_semantic_provenance``.

    Database JSON is never trusted: every read goes through
    ``parse_agent_semantic_provenance``, which re-validates the whole record
    including the amendment replay."""

    model_config = {"extra": "forbid", "frozen": True}

    schema_version: Literal["jd-semantic-provenance-v2"] = "jd-semantic-provenance-v2"
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
    conflict_resolutions: list[SemanticConflictResolution] = Field(
        default_factory=list, max_length=MAX_JD_REQUIREMENT_SPANS
    )
    amendments: list[SemanticCriterionAmendment] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def _validate_consistency(self) -> AgentSemanticProvenance:
        criterion_ids = [item.criterion_id for item in self.criteria]
        if len(set(criterion_ids)) != len(criterion_ids):
            raise ValueError("Duplicate criterion provenance.")
        span_ids = [span.span_id for item in self.criteria for span in item.source_spans]
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
            span.interpretation_source == SemanticInterpretationSource.MODEL_VALIDATED
            for item in self.criteria
            for span in item.source_spans
        ) and self.model is None:
            raise ValueError("A model-validated criterion requires model provenance.")

        spans_by_criterion = {
            item.criterion_id: {span.span_id for span in item.source_spans}
            for item in self.criteria
        }
        resolved_spans: set[str] = set()
        for resolution in self.conflict_resolutions:
            owners = [
                criterion_id
                for criterion_id, spans in spans_by_criterion.items()
                if set(resolution.span_ids) <= spans
            ]
            if len(owners) != 1 or resolved_spans & set(resolution.span_ids):
                raise ValueError("Conflict resolution does not match one criterion.")
            resolved_spans |= set(resolution.span_ids)
            criterion = next(item for item in self.criteria if item.criterion_id == owners[0])
            if criterion.origin != resolution.chosen:
                raise ValueError("Conflict resolution does not explain the origin values.")

        if [item.sequence for item in self.amendments] != list(
            range(1, len(self.amendments) + 1)
        ):
            raise ValueError("Amendments must be one ordered, gap-free sequence.")
        for amendment in self.amendments:
            spans = spans_by_criterion.get(amendment.criterion_id)
            if spans is None or amendment.span_id not in spans:
                raise ValueError("Amendment does not match a confirmed criterion span.")
        for item in self.criteria:
            chain = [a for a in self.amendments if a.criterion_id == item.criterion_id]
            try:
                replayed = replay_amendments(item.origin, chain)
            except ValueError as exc:
                raise ValueError("Amendment chain is broken.") from exc
            if replayed != item.final:
                raise ValueError("Amendment chain does not explain the final values.")
        return self


class SemanticProvenanceError(ValueError):
    """The confirmation boundary could not establish exact provenance."""


def _criterion_parameters(criterion: CriterionIn) -> SemanticParameters:
    return semantic_parameters(criterion.type, criterion.min_years, criterion.required_level)


def build_agent_semantic_provenance(
    draft: AgentJobDraftToolResult, criteria: list[CriterionIn]
) -> AgentSemanticProvenance:
    """Fail-closed proof, at the confirmation boundary, that every confirmed
    criterion is explained by the ORIGINAL JD plus explicit human decisions.

    The draft is never trusted on its own: the source JD is re-hashed and
    re-analysed deterministically, and every span offset/text, family,
    modality, duration and level is re-derived from that analysis. A
    confirmed value that differs from the source must be explained by a
    review decision, a conflict choice among the re-derived source options,
    or an exact ordered amendment chain. Browser form data is never an
    input here."""
    if not draft.semantic_policy_version:
        raise SemanticProvenanceError("Semantic policy version is missing.")
    if draft.source_sha256 is None or draft.source_jd_text is None:
        raise SemanticProvenanceError("Source digest is missing.")
    if (
        hashlib.sha256(draft.source_jd_text.encode("utf-8")).hexdigest()
        != draft.source_sha256
    ):
        raise SemanticProvenanceError("Source text does not match its digest.")
    analysis = analyze_hr_text(draft.source_jd_text)
    source_spans = {span.span_id: span for span in analysis.spans}
    source_semantics = {item.requirement_span_id: item for item in analysis.requirements}

    draft_criteria = {item.id: item for item in [*draft.must_have, *draft.preferred]}
    if len(draft_criteria) != len(draft.must_have) + len(draft.preferred):
        raise SemanticProvenanceError("Duplicate draft criterion ids.")
    span_ids = [item.span_id for item in draft.requirements]
    if len(set(span_ids)) != len(span_ids):
        raise SemanticProvenanceError("Duplicate source spans.")
    known_spans = set(span_ids)
    scorable = [
        item for item in draft.requirements if item.state == RequirementSpanState.SCORABLE
    ]
    confirmed_ids = [criterion.id for criterion in criteria]
    if len(set(confirmed_ids)) != len(confirmed_ids):
        raise SemanticProvenanceError("Duplicate confirmed criterion.")
    if {item.criterion_id for item in scorable} != set(confirmed_ids):
        raise SemanticProvenanceError("Confirmed criteria do not match scorable spans.")
    identities = [
        semantic_identity(JDDraftCriterionKind(criterion.kind.value), criterion.value)
        for criterion in criteria
    ]
    if len(set(identities)) != len(identities):
        raise SemanticProvenanceError("Two criteria share one canonical identity.")

    decisions: dict[str, SemanticReviewDecisionKind] = {}
    for decision in draft.review_decisions:
        if decision.span_id not in known_spans or decision.span_id in decisions:
            raise SemanticProvenanceError("Review decision references an unknown span.")
        decisions[decision.span_id] = decision.decision
    resolution_by_span: dict[str, SemanticConflictResolution] = {}
    for resolution in draft.conflict_resolutions:
        for span_id in resolution.span_ids:
            if span_id not in known_spans or span_id in resolution_by_span:
                raise SemanticProvenanceError("Conflict resolution references an unknown span.")
            resolution_by_span[span_id] = resolution
    for amendment in draft.amendments:
        if amendment.criterion_id not in draft_criteria or amendment.span_id not in known_spans:
            raise SemanticProvenanceError("Amendment references an unknown criterion or span.")
    if [item.sequence for item in draft.amendments] != list(
        range(1, len(draft.amendments) + 1)
    ):
        raise SemanticProvenanceError("Amendments are not one ordered sequence.")

    records: list[CriterionSemanticProvenance] = []
    for criterion in criteria:
        expected = draft_criteria.get(criterion.id)
        if expected is None or expected != criterion:
            raise SemanticProvenanceError("Confirmed criterion differs from the draft.")
        supporting = [item for item in scorable if item.criterion_id == criterion.id]
        grounding_kind = _GROUNDED_KINDS.get(criterion.kind)
        span_records: list[SourceSpanProvenance] = []
        origins: set[SemanticParameters] = set()
        for result in supporting:
            source_span = source_spans.get(result.span_id)
            semantic = source_semantics.get(result.span_id)
            if (
                source_span is None
                or semantic is None
                or result.text is None
                or result.interpretation_source is None
                or (source_span.start_offset, source_span.end_offset, source_span.text)
                != (result.start_offset, result.end_offset, result.text)
            ):
                raise SemanticProvenanceError("Criterion source provenance is incomplete.")
            if semantic.criterion_family is None or (
                semantic.criterion_family.value != criterion.kind.value
            ):
                raise SemanticProvenanceError("Criterion kind is not the source family.")
            if (
                grounding_kind is not None
                and criterion.value
                and not subject_grounded_in_span(grounding_kind, criterion.value, result.text)
            ):
                raise SemanticProvenanceError(
                    "Criterion is not grounded in its source fragment."
                )
            span_resolution = resolution_by_span.get(result.span_id)
            span_decision = decisions.get(result.span_id)
            if span_resolution is not None:
                # The options must be exactly the re-derived source parameter
                # sets of the conflicting spans; HR chose one of them.
                derived: list[SemanticParameters] = []
                for span_id in span_resolution.span_ids:
                    member = source_semantics.get(span_id)
                    if member is None or member.criterion_type is None:
                        raise SemanticProvenanceError("Conflict source is not interpretable.")
                    option = semantic_parameters(
                        member.criterion_type, member.min_years, member.required_level
                    )
                    if option not in derived:
                        derived.append(option)
                if set(derived) != set(span_resolution.options) or len(derived) < 2:
                    raise SemanticProvenanceError("Conflict options are not the source options.")
                origins.add(span_resolution.chosen)
            else:
                source_type = semantic.criterion_type
                if span_decision in (
                    SemanticReviewDecisionKind.MUST_HAVE,
                    SemanticReviewDecisionKind.PREFERRED,
                ):
                    if source_type is not None:
                        raise SemanticProvenanceError(
                            "A review decision cannot override source modality."
                        )
                    source_type = CriterionType(span_decision.value)
                if source_type is None:
                    raise SemanticProvenanceError("Criterion modality has no authority.")
                origins.add(
                    semantic_parameters(source_type, semantic.min_years, semantic.required_level)
                )
            try:
                span_records.append(
                    SourceSpanProvenance(
                        span_id=result.span_id,
                        start_offset=result.start_offset,
                        end_offset=result.end_offset,
                        source_text=result.text,
                        interpretation_source=result.interpretation_source,
                    )
                )
            except ValidationError as exc:
                raise SemanticProvenanceError(
                    "Criterion source offsets are inconsistent."
                ) from exc
        if not span_records:
            raise SemanticProvenanceError("Criterion has no source provenance.")
        if len(origins) != 1:
            raise SemanticProvenanceError("Supporting source spans disagree.")
        origin = next(iter(origins))
        chain = [item for item in draft.amendments if item.criterion_id == criterion.id]
        if any(item.span_id not in {r.span_id for r in span_records} for item in chain):
            raise SemanticProvenanceError("Amendment span does not support its criterion.")
        final = _criterion_parameters(criterion)
        try:
            replayed = replay_amendments(origin, chain)
        except ValueError as exc:
            raise SemanticProvenanceError("Amendment chain is broken.") from exc
        if replayed != final:
            raise SemanticProvenanceError(
                "Confirmed values are not explained by the source and human amendments."
            )
        records.append(
            CriterionSemanticProvenance(
                criterion_id=criterion.id, source_spans=span_records, origin=origin, final=final
            )
        )

    excluded_by_items = {
        item.span_id for item in draft.needs_review if item.acknowledged_excluded
    }
    excluded_by_decisions = {
        span_id
        for span_id, decision in decisions.items()
        if decision == SemanticReviewDecisionKind.EXCLUDED_BY_REVIEWER
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
            conflict_resolutions=list(draft.conflict_resolutions),
            amendments=list(draft.amendments),
        )
    except ValidationError as exc:
        raise SemanticProvenanceError("Semantic provenance is inconsistent.") from exc


def parse_agent_semantic_provenance(raw: Any) -> AgentSemanticProvenance | None:
    """Strictly validate persisted JSON. ``None`` means "not an agent
    semantic version" (manual/API/legacy rows); malformed JSON raises."""
    if raw is None:
        return None
    return AgentSemanticProvenance.model_validate(raw)
