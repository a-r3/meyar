import argparse
import asyncio
import uuid
from datetime import date

from meyar.config import get_settings
from meyar.db import get_session_factory
from meyar.embedding.dependency import get_embedding_provider, get_embedding_search_config
from meyar.embedding.provider import EmbeddingProviderError
from meyar.evaluation.service import EvaluationInputError, evaluate_and_score_candidate
from meyar.extraction.identity_service import (
    IdentityExtractionPreconditionError,
    extract_candidate_identity,
)
from meyar.extraction.service import ExtractionPreconditionError, extract_candidate_profile
from meyar.ingestion.dependency import get_document_parser
from meyar.ingestion.folder_scanner import InvalidSourceRootError
from meyar.llm.dependency import get_llm_provider
from meyar.scoring.batch import BatchRankingError, rank_candidates_for_job
from meyar.scoring.policy import ScoringPolicyError
from meyar.search.planner_schemas import PlannerOutcome
from meyar.search.planner_service import plan_and_search_candidates, plan_candidate_search
from meyar.search.schemas import CandidateSearchRequest, SearchMode
from meyar.search.service import SearchRequestError, search_candidates
from meyar.services.api_key_repo import create_api_key
from meyar.services.audit_repo import record_event
from meyar.services.candidate_document_repo import get_candidate_document
from meyar.services.candidate_embedding_service import (
    EmbeddingPreconditionError,
    embed_candidate_profile,
)
from meyar.services.candidate_profile_repo import get_current_profile_version
from meyar.services.folder_indexer_service import index_folder
from meyar.services.folder_reconciliation_service import reconcile_folder
from meyar.services.job_criteria_repo import get_current_criteria_version
from meyar.services.tenant_repo import create_tenant
from meyar.storage.dependency import get_document_storage


async def _create_tenant(name: str) -> None:
    settings = get_settings()
    factory = get_session_factory()
    async with factory() as db:
        tenant = await create_tenant(db, name=name)
        api_key, plaintext = await create_api_key(
            db, tenant_id=tenant.id, env=settings.api_key_env
        )
        await record_event(
            db,
            tenant_id=tenant.id,
            event_type="tenant.created",
            metadata={"api_key_id": str(api_key.id)},
        )
        await db.commit()

    print(f"Created tenant: {tenant.id} ({name})")
    print(f"API key (shown once, store it now): {plaintext}")
    print(f"Key prefix (safe to log/display): {api_key.prefix}")


async def _extract_profile(tenant_id: str, candidate_id: str, document_id: str) -> None:
    settings = get_settings()
    factory = get_session_factory()
    llm = get_llm_provider()
    async with factory() as db:
        document = await get_candidate_document(
            db,
            tenant_id=uuid.UUID(tenant_id),
            candidate_id=uuid.UUID(candidate_id),
            document_id=uuid.UUID(document_id),
        )
        if document is None:
            print("Document not found for this tenant/candidate.")
            return

        try:
            version = await extract_candidate_profile(
                db,
                llm,
                tenant_id=uuid.UUID(tenant_id),
                candidate_id=uuid.UUID(candidate_id),
                candidate_document=document,
                model_provider_name=settings.llm_provider,
                max_input_chars=settings.llm_max_input_chars,
            )
        except ExtractionPreconditionError as exc:
            await db.commit()
            print(f"Extraction could not run: {exc.code} — {exc}")
            return

        await db.commit()

    print(f"Profile version: {version.id} (v{version.version_number})")
    print(f"Status: {version.status}")
    if version.status == "COMPLETED":
        content = version.profile_content or {}
        print(f"Skills extracted: {len(content.get('skills', []))}")
        print(f"Employment entries: {len(content.get('employment_history', []))}")
    else:
        print(f"Error: {version.error_code} — {version.error_message}")


async def _evaluate(
    tenant_id: str, candidate_id: str, job_id: str, as_of_date_text: str
) -> None:
    """Resolves the latest CandidateProfileVersion and latest
    JobCriteriaVersion ONCE, then evaluates against those exact concrete
    versions — never re-resolves "latest" mid-evaluation."""
    try:
        parsed_tenant_id = uuid.UUID(tenant_id)
        parsed_candidate_id = uuid.UUID(candidate_id)
        parsed_job_id = uuid.UUID(job_id)
        evaluation_as_of_date = date.fromisoformat(as_of_date_text)
    except ValueError as exc:
        print("Invalid tenant/candidate/job id or as-of date (expected UUID and YYYY-MM-DD).")
        raise SystemExit(2) from exc

    try:
        factory = get_session_factory()
        async with factory() as db:
            profile_version = await get_current_profile_version(
                db, tenant_id=parsed_tenant_id, candidate_id=parsed_candidate_id
            )
            if profile_version is None:
                print("No candidate profile version found — run extract-profile first.")
                raise SystemExit(2)

            criteria_version = await get_current_criteria_version(
                db, tenant_id=parsed_tenant_id, job_id=parsed_job_id
            )
            if criteria_version is None:
                print("No job criteria version found for this job.")
                raise SystemExit(2)

            try:
                scored = await evaluate_and_score_candidate(
                    db,
                    tenant_id=parsed_tenant_id,
                    candidate_id=parsed_candidate_id,
                    candidate_profile_version_id=profile_version.id,
                    job_id=parsed_job_id,
                    job_criteria_version_id=criteria_version.id,
                    evaluation_as_of_date=evaluation_as_of_date,
                )
            except (EvaluationInputError, ScoringPolicyError):
                await db.commit()  # preserve the safe failure audit event
                raise
            await db.commit()
    except EvaluationInputError as exc:
        print(f"Evaluation could not run: {exc.code} — {exc}")
        raise SystemExit(2) from exc
    except ScoringPolicyError as exc:
        print(f"Scoring policy failed: {exc.code} — {exc}")
        raise SystemExit(3) from exc
    except SystemExit:
        raise
    except Exception as exc:
        print(f"Evaluation infrastructure failure: {type(exc).__name__}")
        raise SystemExit(4) from exc

    evaluation = scored.evaluation
    if evaluation.status == "FAILED":
        print(f"Evaluation failed: {evaluation.error_code}")
        raise SystemExit(2)
    print(f"Evaluation: {evaluation.id}")
    print(f"Profile version: {evaluation.candidate_profile_version_id}")
    print(f"Criteria version: {evaluation.job_criteria_version_id}")
    print(f"Status: {evaluation.status}")
    print(f"Score: {evaluation.numeric_score}")
    print(f"Fit band: {evaluation.overall_result}")
    print(f"Evaluation policy: {evaluation.policy_engine_version}")
    print(f"Scoring policy: {evaluation.scoring_policy_version}")
    print(f"As-of date: {evaluation.evaluation_as_of_date}")
    print(f"Reused existing evaluation: {scored.reused}")
    explanation = evaluation.score_explanation or {}
    for result in explanation.get("criteria", []):
        print(
            f"  [{result['criterion_type']}] {result['criterion_id']} "
            f"({result['criterion_kind']}): {result['status']} "
            f"weight={result['weight']} factor={result['factor']} "
            f"points={result['weighted_points']} reason={result['reason_code']}"
        )


async def _rank_job(
    tenant_id: str, job_criteria_version_id: str, as_of_date_text: str
) -> None:
    try:
        parsed_tenant_id = uuid.UUID(tenant_id)
        parsed_criteria_id = uuid.UUID(job_criteria_version_id)
        evaluation_as_of_date = date.fromisoformat(as_of_date_text)
    except ValueError as exc:
        print("Invalid tenant/criteria id or as-of date (expected UUID and YYYY-MM-DD).")
        raise SystemExit(2) from exc

    try:
        factory = get_session_factory()
        async with factory() as db:
            result = await rank_candidates_for_job(
                db,
                tenant_id=parsed_tenant_id,
                job_criteria_version_id=parsed_criteria_id,
                evaluation_as_of_date=evaluation_as_of_date,
            )
            await db.commit()
    except BatchRankingError as exc:
        print(f"Batch input rejected: {exc.code} — {exc}")
        raise SystemExit(2) from exc
    except ScoringPolicyError as exc:
        print(f"Scoring policy failed: {exc.code} — {exc}")
        raise SystemExit(3) from exc
    except Exception as exc:
        print(f"Batch ranking infrastructure failure: {type(exc).__name__}")
        raise SystemExit(4) from exc

    print(f"Criteria version: {result.job_criteria_version_id}")
    print(f"As-of date: {result.evaluation_as_of_date}")
    print(f"Evaluation policy: {result.evaluation_policy_version}")
    print(f"Scoring policy: {result.scoring_policy_version}")
    print(f"Evaluated: {result.evaluated_count}")
    print(f"Reused: {result.reused_count}")
    print(f"Skipped: {result.skipped_count}")
    print(f"Skip reasons: {result.skip_reason_counts}")
    for item in result.results:
        print(
            f"  #{item.rank} candidate={item.candidate_id} "
            f"profile={item.candidate_profile_version_id} evaluation={item.evaluation_id} "
            f"fit={item.fit_band} tier={item.fit_tier} score={item.numeric_score}"
        )


async def _index_folder(tenant_id: str, root: str) -> None:
    """CLI entry point for Slice 6 — never prints CV text, filenames, or
    any candidate PII; only ids and counts. Exit codes: 0 = clean scan,
    1 = scan completed but at least one file failed ingestion, 2 =
    invalid source folder, 3 = infrastructure/database failure."""
    settings = get_settings()
    factory = get_session_factory()
    storage = get_document_storage()
    parser = get_document_parser()

    try:
        async with factory() as db:
            summary = await index_folder(
                db,
                storage,
                parser,
                tenant_id=uuid.UUID(tenant_id),
                root_path=root,
                max_bytes=settings.max_upload_bytes,
                stability_window_seconds=settings.folder_stability_seconds,
            )
            await db.commit()
    except InvalidSourceRootError as exc:
        print(f"Invalid source folder: {exc}")
        raise SystemExit(2) from exc
    except Exception as exc:  # infrastructure/database failure
        print(f"Folder indexing failed: {type(exc).__name__}")
        raise SystemExit(3) from exc

    print(f"Source: {summary.folder_source_id}")
    print(f"Discovered: {summary.discovered}")
    print(f"New: {summary.new}")
    print(f"Changed: {summary.changed}")
    print(f"Retried: {summary.retried}")
    print(f"Unchanged: {summary.unchanged}")
    print(f"Successful: {summary.successful}")
    print(f"Failed: {summary.failed}")
    print(f"Missing: {summary.missing}")
    print(f"Skipped (unstable): {summary.skipped_unstable}")
    if summary.failed > 0:
        raise SystemExit(1)


async def _reconcile_folder(tenant_id: str, root: str, limit: int | None) -> None:
    """CLI entry point for Slice 14 — one command serves both initial
    bulk import and repeatable reconciliation: discovery/ingestion
    (reusing index_folder unchanged) followed by profile/identity/
    embedding processing for whatever isn't yet fully processed for its
    current document, so a folder-imported candidate becomes searchable
    without a separate manual per-candidate command. PII-safe output:
    only ids and counts. Exit codes: 0 = clean run (nothing failed and
    nothing was left pending by --limit), 1 = completed with at least
    one ingestion or downstream-processing failure, 2 = invalid source
    folder, 3 = infrastructure/database failure."""
    settings = get_settings()
    factory = get_session_factory()
    storage = get_document_storage()
    parser = get_document_parser()
    llm = get_llm_provider()
    embedding_provider = get_embedding_provider()

    try:
        async with factory() as db:
            scan_summary, reconciliation_summary = await reconcile_folder(
                db,
                storage,
                parser,
                llm,
                embedding_provider,
                tenant_id=uuid.UUID(tenant_id),
                root_path=root,
                max_bytes=settings.max_upload_bytes,
                stability_window_seconds=settings.folder_stability_seconds,
                model_provider_name=settings.llm_provider,
                max_profile_input_chars=settings.llm_max_input_chars,
                max_identity_input_chars=settings.llm_max_input_chars,
                max_embedding_input_chars=settings.embedding_max_input_chars,
                limit=limit,
            )
    except InvalidSourceRootError as exc:
        print(f"Invalid source folder: {exc}")
        raise SystemExit(2) from exc
    except Exception as exc:  # infrastructure/database failure
        print(f"Folder reconciliation failed: {type(exc).__name__}")
        raise SystemExit(3) from exc

    print(f"Source: {scan_summary.folder_source_id}")
    print(f"Discovered: {scan_summary.discovered}")
    print(f"New: {scan_summary.new}")
    print(f"Changed: {scan_summary.changed}")
    print(f"Retried: {scan_summary.retried}")
    print(f"Unchanged: {scan_summary.unchanged}")
    print(f"Ingestion successful: {scan_summary.successful}")
    print(f"Ingestion failed: {scan_summary.failed}")
    print(f"Missing: {scan_summary.missing}")
    print(f"Skipped (unstable): {scan_summary.skipped_unstable}")
    print(f"Candidates considered: {reconciliation_summary.candidates_considered}")
    print(f"Already ready: {reconciliation_summary.already_ready}")
    print(f"Processed this run: {reconciliation_summary.processed}")
    print(f"Ready after this run: {reconciliation_summary.ready_after}")
    print(f"Not fully ready (pending retry): {reconciliation_summary.failed}")
    print(f"Skipped due to --limit: {reconciliation_summary.skipped_due_to_limit}")
    if scan_summary.failed > 0 or reconciliation_summary.failed > 0:
        raise SystemExit(1)


async def _extract_identity(tenant_id: str, candidate_id: str, document_id: str) -> None:
    """PII-safe by design: never prints full_name/email/phone. Only ids,
    status, and a non-identifying found-field count. There is no
    authorized reveal command in this slice — see Slice 7 spec §7."""
    settings = get_settings()
    factory = get_session_factory()
    llm = get_llm_provider()
    async with factory() as db:
        document = await get_candidate_document(
            db,
            tenant_id=uuid.UUID(tenant_id),
            candidate_id=uuid.UUID(candidate_id),
            document_id=uuid.UUID(document_id),
        )
        if document is None:
            print("Document not found for this tenant/candidate.")
            return

        try:
            version = await extract_candidate_identity(
                db,
                llm,
                tenant_id=uuid.UUID(tenant_id),
                candidate_id=uuid.UUID(candidate_id),
                candidate_document=document,
                model_provider_name=settings.llm_provider,
                max_input_chars=settings.llm_max_input_chars,
            )
        except IdentityExtractionPreconditionError as exc:
            await db.commit()
            print(f"Identity extraction could not run: {exc.code} — {exc}")
            return

        await db.commit()

    print(f"Identity version: {version.id} (v{version.version_number})")
    print(f"Candidate: {version.candidate_id}")
    print(f"Source document: {version.candidate_document_id}")
    print(f"Status: {version.status}")
    if version.status != "COMPLETED":
        print(f"Error: {version.error_code} — {version.error_message}")


async def _embed_candidate(tenant_id: str, candidate_id: str) -> None:
    """Operates on the candidate's current CandidateProfileVersion.
    Never prints the vector. Reports whether the result was newly
    computed or reused from an existing idempotent match."""
    settings = get_settings()
    factory = get_session_factory()
    provider = get_embedding_provider()

    try:
        async with factory() as db:
            version, was_reused = await embed_candidate_profile(
                db,
                provider,
                tenant_id=uuid.UUID(tenant_id),
                candidate_id=uuid.UUID(candidate_id),
                max_input_chars=settings.embedding_max_input_chars,
            )
            await db.commit()
    except EmbeddingPreconditionError as exc:
        print(f"Embedding could not run: {exc.code} — {exc}")
        raise SystemExit(2) from exc
    except EmbeddingProviderError as exc:
        print(f"Embedding provider failed: {exc.code}")
        raise SystemExit(3) from exc

    print(f"Embedding version: {version.id}")
    print(f"Candidate: {version.candidate_id}")
    print(f"Profile version: {version.candidate_profile_version_id}")
    print(f"Provider/model: {version.provider}/{version.model_name}")
    print(f"Dimensions: {version.embedding_dimensions}")
    print(f"Reused existing embedding: {was_reused}")


async def _search_candidates(tenant_id: str, request_file: str) -> None:
    """CLI demonstration of Slice 8 structured/semantic/hybrid search — no
    natural-language input (Slice 9). Reads a strict CandidateSearchRequest
    from a JSON file. PII-safe output only: candidate_id, rank, scores,
    professional match summary — never identity, vectors, or raw CV text.
    STRUCTURED_ONLY never constructs a real embedding-provider HTTP client.
    Exit codes: 0 = ran (possibly zero results), 2 = invalid request/config,
    3 = embedding provider failure, 4 = other infrastructure failure."""
    from pydantic import ValidationError

    try:
        with open(request_file, encoding="utf-8") as fh:
            raw = fh.read()
        request = CandidateSearchRequest.model_validate_json(raw)
    except (OSError, ValidationError) as exc:
        print(f"Invalid search request: {exc}")
        raise SystemExit(2) from exc

    embedding_provider = None
    if request.mode != SearchMode.STRUCTURED_ONLY:
        embedding_provider = get_embedding_provider()

    factory = get_session_factory()
    try:
        async with factory() as db:
            response = await search_candidates(
                db,
                tenant_id=uuid.UUID(tenant_id),
                request=request,
                embedding_provider=embedding_provider,
            )
            await db.commit()
    except SearchRequestError as exc:
        print(f"Search request rejected: {exc.code} — {exc}")
        raise SystemExit(2) from exc
    except EmbeddingProviderError as exc:
        print(f"Embedding provider failed: {exc.code}")
        raise SystemExit(3) from exc

    print(f"Mode: {response.mode.value}")
    print(f"Policy version: {response.policy_version}")
    print(f"Eligible candidates: {response.eligible_profile_count}")
    if request.mode != SearchMode.STRUCTURED_ONLY:
        print(f"Compatible embeddings: {response.compatible_embedding_count}")
        print(f"Excluded (missing embedding): {response.excluded_missing_embedding_count}")
    print(f"Results ({response.result_count}):")
    for result in response.results:
        print(
            f"  #{result.rank} candidate={result.candidate_id} "
            f"relevance={result.relevance_score:.4f} "
            f"structured={result.structured_score} semantic={result.semantic_score}"
        )


async def _plan_search(
    tenant_id: str, query: str, as_of_date_text: str, *, execute: bool
) -> None:
    """PII-safe Slice 9 CLI: plan only by default, optionally execute Slice 8."""
    try:
        parsed_tenant_id = uuid.UUID(tenant_id)
        trusted_as_of_date = date.fromisoformat(as_of_date_text)
    except ValueError as exc:
        print("Invalid tenant id or as-of date (expected UUID and YYYY-MM-DD).")
        raise SystemExit(2) from exc

    llm = get_llm_provider()
    embedding_config = get_embedding_search_config()
    factory = get_session_factory()
    try:
        async with factory() as db:
            if execute:
                planned = await plan_and_search_candidates(
                    db,
                    llm,
                    tenant_id=parsed_tenant_id,
                    natural_language_request=query,
                    as_of_date=trusted_as_of_date,
                    embedding_config=embedding_config,
                    embedding_provider=get_embedding_provider(),
                )
                plan = planned.plan
            else:
                planned = None
                plan = await plan_candidate_search(
                    db,
                    llm,
                    tenant_id=parsed_tenant_id,
                    natural_language_request=query,
                    as_of_date=trusted_as_of_date,
                    embedding_config=embedding_config,
                )
            await db.commit()
    except (SearchRequestError, EmbeddingProviderError) as exc:
        print(f"Natural-language search execution failed: {exc.code}")
        raise SystemExit(4) from exc
    except Exception as exc:
        print(f"Natural-language search infrastructure failure: {type(exc).__name__}")
        raise SystemExit(4) from exc

    print(f"Executable: {plan.executable}")
    print(f"Outcome: {plan.outcome.value}")
    print(f"Planner policy: {plan.planner_policy_version}")
    print(f"Prompt version: {plan.prompt_version}")
    print(f"Schema version: {plan.schema_version}")
    print(f"Attempts: {plan.attempt_count}")
    if plan.reason_codes:
        print(f"Reasons: {','.join(code.value for code in plan.reason_codes)}")

    if not plan.executable:
        if plan.outcome == PlannerOutcome.PLANNER_PROVIDER_FAILURE:
            raise SystemExit(3)
        raise SystemExit(2)

    assert plan.search_request is not None
    request = plan.search_request
    print(f"Mode: {request.mode.value}")
    print(f"Limit: {request.limit}")
    print(f"Required filters: {request.required_filters.model_dump(mode='json')}")
    print(f"Preferred filters: {request.preferred_filters.model_dump(mode='json')}")
    print(f"Semantic intent present: {request.semantic_query is not None}")

    if execute:
        assert planned is not None and planned.search_response is not None
        response = planned.search_response
        print(f"Search policy: {response.policy_version}")
        print(f"Results ({response.result_count}):")
        for result in response.results:
            print(
                f"  #{result.rank} candidate={result.candidate_id} "
                f"relevance={result.relevance_score:.4f} "
                f"structured={result.structured_score} semantic={result.semantic_score}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(prog="meyar")
    sub = parser.add_subparsers(dest="command", required=True)

    create_tenant_parser = sub.add_parser(
        "create-tenant", help="Create a tenant and its first API key."
    )
    create_tenant_parser.add_argument("--name", required=True)

    extract_parser = sub.add_parser(
        "extract-profile", help="Run candidate profile extraction against a real local LLM."
    )
    extract_parser.add_argument("--tenant-id", required=True)
    extract_parser.add_argument("--candidate-id", required=True)
    extract_parser.add_argument("--document-id", required=True)

    evaluate_parser = sub.add_parser(
        "evaluate",
        help="Evaluate a candidate's latest profile version against a job's latest criteria.",
    )
    evaluate_parser.add_argument("--tenant-id", required=True)
    evaluate_parser.add_argument("--candidate-id", required=True)
    evaluate_parser.add_argument("--job-id", required=True)
    evaluate_parser.add_argument(
        "--as-of-date", required=True, help="Deterministic evaluation date (YYYY-MM-DD)."
    )

    rank_job_parser = sub.add_parser(
        "rank-job", help="Deterministically score and rank the tenant candidate library."
    )
    rank_job_parser.add_argument("--tenant-id", required=True)
    rank_job_parser.add_argument("--job-criteria-version-id", required=True)
    rank_job_parser.add_argument(
        "--as-of-date", required=True, help="Deterministic evaluation date (YYYY-MM-DD)."
    )

    index_folder_parser = sub.add_parser(
        "index-folder",
        help="Scan a local folder for PDF/DOCX CVs and ingest new/changed files.",
    )
    index_folder_parser.add_argument("--tenant-id", required=True)
    index_folder_parser.add_argument("--root", required=True, help="Local folder path to scan.")

    reconcile_folder_parser = sub.add_parser(
        "reconcile-folder",
        help=(
            "Scan a local folder for PDF/DOCX CVs, ingest new/changed files, and drive "
            "them through profile/identity extraction and embedding so they become "
            "searchable — one command for both initial bulk import and repeatable "
            "reconciliation."
        ),
    )
    reconcile_folder_parser.add_argument("--tenant-id", required=True)
    reconcile_folder_parser.add_argument(
        "--root", required=True, help="Local folder path to scan."
    )
    reconcile_folder_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Bound how many not-yet-ready candidates are processed this invocation "
            "(already-ready candidates are free and never count against it). "
            "Default: unlimited."
        ),
    )

    extract_identity_parser = sub.add_parser(
        "extract-identity",
        help="Run candidate identity extraction (name/email/phone) against a real local LLM.",
    )
    extract_identity_parser.add_argument("--tenant-id", required=True)
    extract_identity_parser.add_argument("--candidate-id", required=True)
    extract_identity_parser.add_argument("--document-id", required=True)

    embed_candidate_parser = sub.add_parser(
        "embed-candidate",
        help="Locally embed a candidate's current professional profile (pgvector).",
    )
    embed_candidate_parser.add_argument("--tenant-id", required=True)
    embed_candidate_parser.add_argument("--candidate-id", required=True)

    search_candidates_parser = sub.add_parser(
        "search-candidates",
        help=(
            "Structured/semantic/hybrid candidate search (Slice 8) — "
            "no natural-language input."
        ),
    )
    search_candidates_parser.add_argument("--tenant-id", required=True)
    search_candidates_parser.add_argument(
        "--request-file", required=True, help="Path to a JSON CandidateSearchRequest file."
    )

    plan_search_parser = sub.add_parser(
        "plan-search",
        help="Plan a natural-language candidate search locally; optionally execute Slice 8.",
    )
    plan_search_parser.add_argument("--tenant-id", required=True)
    plan_search_parser.add_argument("--query", required=True)
    plan_search_parser.add_argument(
        "--as-of-date",
        required=True,
        help="Trusted deterministic reference date (YYYY-MM-DD).",
    )
    plan_search_parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute the validated plan through Slice 8 (default is plan-only).",
    )

    args = parser.parse_args()
    if args.command == "create-tenant":
        asyncio.run(_create_tenant(args.name))
    elif args.command == "extract-profile":
        asyncio.run(_extract_profile(args.tenant_id, args.candidate_id, args.document_id))
    elif args.command == "evaluate":
        asyncio.run(_evaluate(args.tenant_id, args.candidate_id, args.job_id, args.as_of_date))
    elif args.command == "rank-job":
        asyncio.run(
            _rank_job(args.tenant_id, args.job_criteria_version_id, args.as_of_date)
        )
    elif args.command == "index-folder":
        asyncio.run(_index_folder(args.tenant_id, args.root))
    elif args.command == "reconcile-folder":
        asyncio.run(_reconcile_folder(args.tenant_id, args.root, args.limit))
    elif args.command == "extract-identity":
        asyncio.run(_extract_identity(args.tenant_id, args.candidate_id, args.document_id))
    elif args.command == "embed-candidate":
        asyncio.run(_embed_candidate(args.tenant_id, args.candidate_id))
    elif args.command == "search-candidates":
        asyncio.run(_search_candidates(args.tenant_id, args.request_file))
    elif args.command == "plan-search":
        asyncio.run(
            _plan_search(
                args.tenant_id,
                args.query,
                args.as_of_date,
                execute=args.execute,
            )
        )


if __name__ == "__main__":
    main()
