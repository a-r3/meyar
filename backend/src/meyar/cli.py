import argparse
import asyncio
import uuid

from meyar.config import get_settings
from meyar.db import get_session_factory
from meyar.evaluation.service import EvaluationInputError, evaluate_candidate
from meyar.extraction.service import ExtractionPreconditionError, extract_candidate_profile
from meyar.ingestion.dependency import get_document_parser
from meyar.ingestion.folder_scanner import InvalidSourceRootError
from meyar.llm.dependency import get_llm_provider
from meyar.services.api_key_repo import create_api_key
from meyar.services.audit_repo import record_event
from meyar.services.candidate_document_repo import get_candidate_document
from meyar.services.candidate_profile_repo import get_current_profile_version
from meyar.services.folder_indexer_service import index_folder
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


async def _evaluate(tenant_id: str, candidate_id: str, job_id: str) -> None:
    """Resolves the latest CandidateProfileVersion and latest
    JobCriteriaVersion ONCE, then evaluates against those exact concrete
    versions — never re-resolves "latest" mid-evaluation."""
    factory = get_session_factory()
    async with factory() as db:
        profile_version = await get_current_profile_version(
            db, tenant_id=uuid.UUID(tenant_id), candidate_id=uuid.UUID(candidate_id)
        )
        if profile_version is None:
            print("No candidate profile version found — run extract-profile first.")
            return

        criteria_version = await get_current_criteria_version(
            db, tenant_id=uuid.UUID(tenant_id), job_id=uuid.UUID(job_id)
        )
        if criteria_version is None:
            print("No job criteria version found for this job.")
            return

        try:
            evaluation = await evaluate_candidate(
                db,
                tenant_id=uuid.UUID(tenant_id),
                candidate_id=uuid.UUID(candidate_id),
                candidate_profile_version_id=profile_version.id,
                job_id=uuid.UUID(job_id),
                job_criteria_version_id=criteria_version.id,
            )
        except EvaluationInputError as exc:
            await db.commit()
            print(f"Evaluation could not run: {exc.code} — {exc}")
            return

        await db.commit()

    print(f"Evaluation: {evaluation.id}")
    print(f"Status: {evaluation.status}")
    print(f"Overall result: {evaluation.overall_result}")
    if evaluation.status == "FAILED":
        print(f"Error: {evaluation.error_code} — {evaluation.error_message}")
    else:
        for result in evaluation.criterion_results or []:
            print(
                f"  [{result['type']}] {result['criterion_id']} ({result['kind']}): "
                f"{result['status']} — {result['reason_code']}"
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
    if summary.failed > 0:
        raise SystemExit(1)


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

    index_folder_parser = sub.add_parser(
        "index-folder",
        help="Scan a local folder for PDF/DOCX CVs and ingest new/changed files.",
    )
    index_folder_parser.add_argument("--tenant-id", required=True)
    index_folder_parser.add_argument("--root", required=True, help="Local folder path to scan.")

    args = parser.parse_args()
    if args.command == "create-tenant":
        asyncio.run(_create_tenant(args.name))
    elif args.command == "extract-profile":
        asyncio.run(_extract_profile(args.tenant_id, args.candidate_id, args.document_id))
    elif args.command == "evaluate":
        asyncio.run(_evaluate(args.tenant_id, args.candidate_id, args.job_id))
    elif args.command == "index-folder":
        asyncio.run(_index_folder(args.tenant_id, args.root))


if __name__ == "__main__":
    main()
