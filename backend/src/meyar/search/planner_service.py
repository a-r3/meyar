"""Slice 9 natural-language planner and thin Slice 8 orchestration."""

import hashlib
import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from meyar.embedding.provider import EmbeddingProvider
from meyar.llm.provider import (
    LLMProvider,
    LLMProviderError,
    LLMResultProvenance,
    ModelSchemaInvalidError,
    ModelTimeoutError,
    ModelUnavailableError,
)
from meyar.search.planner_policy import (
    PLANNER_POLICY_VERSION,
    PlannerPolicyError,
    build_interpretation_summary,
    convert_planner_draft,
    precheck_natural_language_request,
    try_deterministic_intent_parse,
)
from meyar.search.planner_prompts import SEARCH_PLANNER_PROMPT_VERSION
from meyar.search.planner_schemas import (
    PLANNER_SCHEMA_VERSION,
    PlannedCandidateSearchResponse,
    PlannerDraft,
    PlannerOutcome,
    PlannerReasonCode,
    SearchPlanResult,
)
from meyar.search.schemas import EmbeddingSearchConfig
from meyar.search.service import search_candidates
from meyar.services.audit_repo import record_event

MAX_PLANNER_ATTEMPTS = 2

# Provenance recorded for a plan produced by the deterministic fast path
# (D-026) — clearly distinguishable from any configured LLM provider in
# audit metadata, since no model call happened. Fixed, not read from
# config: this identifies the parser code path itself, not a model.
DETERMINISTIC_PLANNER_PROVENANCE = LLMResultProvenance(
    provider="meyar-deterministic",
    model_name="meyar-deterministic-parser-v1",
    model_revision="",
)


def _request_sha256(natural_language_request: str) -> str:
    return hashlib.sha256(natural_language_request.encode("utf-8")).hexdigest()


def _configured_provenance(llm: LLMProvider) -> LLMResultProvenance:
    return LLMResultProvenance(
        provider=llm.provider_name,
        model_name=llm.model_name,
        model_revision=llm.model_revision,
    )


def _provenance_matches(
    configured: LLMResultProvenance, actual: LLMResultProvenance
) -> bool:
    return (
        configured.provider == actual.provider
        and configured.model_name == actual.model_name
        and configured.model_revision == actual.model_revision
    )


def _result(
    *,
    outcome: PlannerOutcome,
    request_sha256: str,
    provenance: LLMResultProvenance,
    attempt_count: int,
    draft: PlannerDraft | None = None,
    search_request=None,
    reason_codes: list[PlannerReasonCode] | None = None,
) -> SearchPlanResult:
    return SearchPlanResult(
        executable=outcome == PlannerOutcome.EXECUTABLE,
        outcome=outcome,
        search_request=search_request,
        planner_policy_version=PLANNER_POLICY_VERSION,
        prompt_version=SEARCH_PLANNER_PROMPT_VERSION,
        schema_version=PLANNER_SCHEMA_VERSION,
        model_provider=provenance.provider,
        model_name=provenance.model_name,
        model_revision=provenance.model_revision,
        attempt_count=attempt_count,
        request_sha256=request_sha256,
        interpretation=build_interpretation_summary(draft, search_request),
        reason_codes=reason_codes or [],
    )


async def _audit_plan_result(
    db: AsyncSession, *, tenant_id: uuid.UUID, result: SearchPlanResult
) -> None:
    if result.outcome == PlannerOutcome.EXECUTABLE:
        event_type = "SEARCH_PLAN_CREATED"
    elif result.outcome in (
        PlannerOutcome.MALFORMED_MODEL_OUTPUT,
        PlannerOutcome.PLANNER_PROVIDER_FAILURE,
    ):
        event_type = "SEARCH_PLAN_FAILED"
    else:
        event_type = "SEARCH_PLAN_REJECTED"

    metadata: dict = {
        "executable": result.executable,
        "outcome": result.outcome.value,
        "planner_policy_version": result.planner_policy_version,
        "prompt_version": result.prompt_version,
        "schema_version": result.schema_version,
        "provider": result.model_provider,
        "model_name": result.model_name,
        "model_revision": result.model_revision,
        "attempt_count": result.attempt_count,
        "request_sha256": result.request_sha256,
        "reason_codes": [code.value for code in result.reason_codes],
    }
    if result.search_request is not None:
        metadata.update(
            {
                "mode": result.search_request.mode.value,
                "result_limit": result.search_request.limit,
            }
        )
    await record_event(db, tenant_id=tenant_id, event_type=event_type, metadata=metadata)


async def plan_candidate_search(
    db: AsyncSession,
    llm: LLMProvider,
    *,
    tenant_id: uuid.UUID,
    natural_language_request: str,
    as_of_date: date,
    embedding_config: EmbeddingSearchConfig,
) -> SearchPlanResult:
    """Interpret a request without reading candidate/search repositories.

    The database session is used only to append a PII-safe ``AuditEvent``.
    Candidate retrieval belongs exclusively to ``search_candidates`` and
    is invoked only by ``plan_and_search_candidates`` below.
    """
    request_hash = _request_sha256(natural_language_request)
    configured_provenance = _configured_provenance(llm)

    try:
        precheck_natural_language_request(natural_language_request)
    except PlannerPolicyError as exc:
        result = _result(
            outcome=exc.outcome,
            request_sha256=request_hash,
            provenance=configured_provenance,
            attempt_count=0,
            reason_codes=exc.reason_codes,
        )
        await _audit_plan_result(db, tenant_id=tenant_id, result=result)
        return result

    # Conservative deterministic fast path (D-026): explicit, unambiguous
    # HR search intents (skills, languages, certifications, total
    # experience — singly or "və"-combined) execute without ever calling
    # the local LLM planner, so common supported queries do not depend on
    # a small local model's interpretation quality. Only ever produces a
    # result when the ENTIRE request is confidently accounted for;
    # anything else falls through to the LLM loop below unchanged.
    deterministic_draft = try_deterministic_intent_parse(natural_language_request)
    if deterministic_draft is not None:
        try:
            deterministic_request = convert_planner_draft(
                deterministic_draft,
                natural_language_request=natural_language_request,
                as_of_date=as_of_date,
                embedding_config=embedding_config,
            )
        except PlannerPolicyError:
            # The parser's own extraction did not survive the same
            # fidelity validation the LLM path is held to — decline
            # silently and fall through to the LLM rather than raise,
            # since this is an internal safety net, not a user-facing
            # rejection reason.
            pass
        else:
            result = _result(
                outcome=PlannerOutcome.EXECUTABLE,
                request_sha256=request_hash,
                provenance=DETERMINISTIC_PLANNER_PROVENANCE,
                attempt_count=0,
                draft=deterministic_draft,
                search_request=deterministic_request,
            )
            await _audit_plan_result(db, tenant_id=tenant_id, result=result)
            return result

    draft: PlannerDraft | None = None
    actual_provenance = configured_provenance
    for attempt in range(1, MAX_PLANNER_ATTEMPTS + 1):
        try:
            draft, actual_provenance = await llm.plan_candidate_search(
                natural_language_request, repair=attempt > 1
            )
        except ModelSchemaInvalidError:
            continue
        except ModelTimeoutError:
            result = _result(
                outcome=PlannerOutcome.PLANNER_PROVIDER_FAILURE,
                request_sha256=request_hash,
                provenance=configured_provenance,
                attempt_count=attempt,
                reason_codes=[PlannerReasonCode.MODEL_TIMEOUT],
            )
            await _audit_plan_result(db, tenant_id=tenant_id, result=result)
            return result
        except ModelUnavailableError:
            result = _result(
                outcome=PlannerOutcome.PLANNER_PROVIDER_FAILURE,
                request_sha256=request_hash,
                provenance=configured_provenance,
                attempt_count=attempt,
                reason_codes=[PlannerReasonCode.MODEL_UNAVAILABLE],
            )
            await _audit_plan_result(db, tenant_id=tenant_id, result=result)
            return result
        except LLMProviderError:
            result = _result(
                outcome=PlannerOutcome.PLANNER_PROVIDER_FAILURE,
                request_sha256=request_hash,
                provenance=configured_provenance,
                attempt_count=attempt,
                reason_codes=[PlannerReasonCode.MODEL_UNAVAILABLE],
            )
            await _audit_plan_result(db, tenant_id=tenant_id, result=result)
            return result

        if not _provenance_matches(configured_provenance, actual_provenance):
            result = _result(
                outcome=PlannerOutcome.VALIDATION_FAILURE,
                request_sha256=request_hash,
                provenance=actual_provenance,
                attempt_count=attempt,
                draft=draft,
                reason_codes=[PlannerReasonCode.MODEL_PROVENANCE_MISMATCH],
            )
            await _audit_plan_result(db, tenant_id=tenant_id, result=result)
            return result

        try:
            search_request = convert_planner_draft(
                draft,
                natural_language_request=natural_language_request,
                as_of_date=as_of_date,
                embedding_config=embedding_config,
            )
        except PlannerPolicyError as exc:
            result = _result(
                outcome=exc.outcome,
                request_sha256=request_hash,
                provenance=actual_provenance,
                attempt_count=attempt,
                draft=draft,
                reason_codes=exc.reason_codes,
            )
            await _audit_plan_result(db, tenant_id=tenant_id, result=result)
            return result

        result = _result(
            outcome=PlannerOutcome.EXECUTABLE,
            request_sha256=request_hash,
            provenance=actual_provenance,
            attempt_count=attempt,
            draft=draft,
            search_request=search_request,
        )
        await _audit_plan_result(db, tenant_id=tenant_id, result=result)
        return result

    result = _result(
        outcome=PlannerOutcome.MALFORMED_MODEL_OUTPUT,
        request_sha256=request_hash,
        provenance=configured_provenance,
        attempt_count=MAX_PLANNER_ATTEMPTS,
        reason_codes=[PlannerReasonCode.MODEL_SCHEMA_INVALID],
    )
    await _audit_plan_result(db, tenant_id=tenant_id, result=result)
    return result


async def plan_and_search_candidates(
    db: AsyncSession,
    llm: LLMProvider,
    *,
    tenant_id: uuid.UUID,
    natural_language_request: str,
    as_of_date: date,
    embedding_config: EmbeddingSearchConfig,
    embedding_provider: EmbeddingProvider | None = None,
) -> PlannedCandidateSearchResponse:
    """Plan, then delegate execution unchanged to the accepted Slice 8 service."""
    plan = await plan_candidate_search(
        db,
        llm,
        tenant_id=tenant_id,
        natural_language_request=natural_language_request,
        as_of_date=as_of_date,
        embedding_config=embedding_config,
    )
    if not plan.executable:
        return PlannedCandidateSearchResponse(plan=plan)

    assert plan.search_request is not None
    response = await search_candidates(
        db,
        tenant_id=tenant_id,
        request=plan.search_request,
        embedding_provider=embedding_provider,
    )
    return PlannedCandidateSearchResponse(plan=plan, search_response=response)
