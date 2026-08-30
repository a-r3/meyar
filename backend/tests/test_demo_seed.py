"""Presentation-readiness synthetic demo bootstrap (issue #25 — chore, not
a product Slice). Verifies meyar.services.demo_seed_service produces real,
tenant-isolated, deterministic-evaluator-backed demo state without ever
touching the production LLM/embedding provider factories. All data is
synthetic — see fixtures/synthetic_cvs/README.md and
.claude/rules/testing.md."""

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.ingestion.parsers.local_text_parser import LocalTextParser
from meyar.models.candidate import Candidate
from meyar.models.evaluation import Evaluation
from meyar.models.job import Job
from meyar.models.tenant import Tenant
from meyar.search.schemas import CandidateSearchRequest, RequiredFilters, SearchMode
from meyar.search.service import search_candidates
from meyar.services.candidate_document_repo import get_candidate_document
from meyar.services.candidate_repo import create_candidate
from meyar.services.demo_seed_service import (
    DEMO_TENANT_NAME,
    _demo_candidates,
    reset_demo,
    seed_demo,
)
from meyar.services.job_criteria_repo import get_current_criteria_version
from meyar.services.tenant_repo import create_tenant
from meyar.storage.local import LocalFilesystemStorage
from meyar.ui.service import list_candidate_library

MAX_BYTES = 10 * 1024 * 1024
MAX_INPUT_CHARS = 20000


def _storage(tmp_path: Path) -> LocalFilesystemStorage:
    return LocalFilesystemStorage(root=str(tmp_path / "storage"))


def _parser() -> LocalTextParser:
    return LocalTextParser()


async def _seed(db_session: AsyncSession, tmp_path: Path):
    return await seed_demo(
        db_session,
        _storage(tmp_path),
        _parser(),
        max_bytes=MAX_BYTES,
        max_profile_input_chars=MAX_INPUT_CHARS,
        max_identity_input_chars=MAX_INPUT_CHARS,
        max_embedding_input_chars=MAX_INPUT_CHARS,
    )


async def test_seed_demo_succeeds_on_empty_db(db_session: AsyncSession, tmp_path: Path) -> None:
    summary = await _seed(db_session, tmp_path)
    await db_session.commit()

    assert summary.already_seeded is False
    assert summary.candidates_created == len(_demo_candidates())
    assert summary.documents_created == summary.candidates_created
    assert summary.profiles_created == summary.candidates_created
    assert summary.identities_created == summary.candidates_created
    assert summary.jobs_created == 2
    assert summary.evaluations_created > 0
    assert summary.api_key_plaintext is not None
    assert summary.api_key_plaintext.startswith("meyar_test_")

    tenant = await db_session.get(Tenant, summary.tenant_id)
    assert tenant is not None
    assert tenant.name == DEMO_TENANT_NAME


async def test_seed_demo_idempotent_no_duplicates(db_session: AsyncSession, tmp_path: Path) -> None:
    first = await _seed(db_session, tmp_path)
    await db_session.commit()

    second = await _seed(db_session, tmp_path)
    await db_session.commit()

    assert second.tenant_id == first.tenant_id
    assert second.already_seeded is True
    assert second.candidates_created == 0
    assert second.api_key_plaintext is None  # never re-shown, only minted fresh

    result = await db_session.execute(
        select(Candidate).where(Candidate.tenant_id == first.tenant_id)
    )
    assert len(result.scalars().all()) == len(_demo_candidates())

    jobs = await db_session.execute(select(Job).where(Job.tenant_id == first.tenant_id))
    assert len(jobs.scalars().all()) == 2


async def test_repeat_seed_after_kill_between_stages_recovers(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """Restart safety: seeding again after the tenant already has a
    partial candidate count (simulating an interrupted first run) must
    not error and must not duplicate the existing candidates — the
    idempotent no-op path activates as soon as any candidate exists."""
    tenant = await create_tenant(db_session, name=DEMO_TENANT_NAME)
    await create_candidate(db_session, tenant_id=tenant.id)  # simulates 1 partial candidate
    await db_session.commit()

    summary = await _seed(db_session, tmp_path)
    await db_session.commit()

    assert summary.tenant_id == tenant.id
    assert summary.already_seeded is True
    result = await db_session.execute(select(Candidate).where(Candidate.tenant_id == tenant.id))
    assert len(result.scalars().all()) == 1  # untouched, no duplicate/partial reseed


async def test_demo_tenant_isolation(db_session: AsyncSession, tmp_path: Path) -> None:
    other_tenant = await create_tenant(db_session, name="Unrelated-Tenant")
    await create_candidate(db_session, tenant_id=other_tenant.id)
    await db_session.commit()

    summary = await _seed(db_session, tmp_path)
    await db_session.commit()

    assert summary.tenant_id != other_tenant.id

    other_candidates = await db_session.execute(
        select(Candidate).where(Candidate.tenant_id == other_tenant.id)
    )
    assert len(other_candidates.scalars().all()) == 1  # unaffected by seeding

    demo_candidates = await db_session.execute(
        select(Candidate).where(Candidate.tenant_id == summary.tenant_id)
    )
    assert len(demo_candidates.scalars().all()) == len(_demo_candidates())


async def test_seeded_candidates_visible_in_library(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    summary = await _seed(db_session, tmp_path)
    await db_session.commit()

    page = await list_candidate_library(db_session, tenant_id=summary.tenant_id)
    assert page.total == len(_demo_candidates())
    assert len(page.items) == len(_demo_candidates())


async def test_original_synthetic_cv_retrievable(db_session: AsyncSession, tmp_path: Path) -> None:
    summary = await _seed(db_session, tmp_path)
    await db_session.commit()

    candidates = await db_session.execute(
        select(Candidate).where(Candidate.tenant_id == summary.tenant_id)
    )
    candidate = candidates.scalars().first()
    assert candidate is not None

    from meyar.services.candidate_document_repo import list_candidate_documents

    documents = await list_candidate_documents(
        db_session, tenant_id=summary.tenant_id, candidate_id=candidate.id
    )
    assert len(documents) == 1
    document = documents[0]
    assert document.parser_status == "PARSED"

    fetched = await get_candidate_document(
        db_session,
        tenant_id=summary.tenant_id,
        candidate_id=candidate.id,
        document_id=document.id,
    )
    assert fetched is not None

    storage = _storage(tmp_path)
    data = await storage.read(storage_key=document.storage_key)
    assert len(data) > 0
    assert "/" not in document.storage_key.split("/", 1)[1]  # opaque suffix, no path segments


async def test_demo_jobs_and_criteria_present(db_session: AsyncSession, tmp_path: Path) -> None:
    summary = await _seed(db_session, tmp_path)
    await db_session.commit()

    jobs = await db_session.execute(select(Job).where(Job.tenant_id == summary.tenant_id))
    job_rows = jobs.scalars().all()
    assert {j.title for j in job_rows} == {
        "Senior Backend Engineer",
        "AML / Compliance Specialist",
    }

    for job in job_rows:
        criteria_version = await get_current_criteria_version(
            db_session, tenant_id=summary.tenant_id, job_id=job.id
        )
        assert criteria_version is not None
        assert len(criteria_version.criteria) >= 3
        kinds = {c["kind"] for c in criteria_version.criteria}
        types = {c["type"] for c in criteria_version.criteria}
        assert "MUST_HAVE" in types
        assert "PREFERRED" in types
        assert kinds  # at least one criterion kind present


async def test_deterministic_evaluation_generated_through_real_evaluator(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    summary = await _seed(db_session, tmp_path)
    await db_session.commit()

    evaluations = await db_session.execute(
        select(Evaluation).where(Evaluation.tenant_id == summary.tenant_id)
    )
    rows = evaluations.scalars().all()
    assert len(rows) == summary.evaluations_created

    completed = [e for e in rows if e.status == "COMPLETED"]
    assert completed  # at least one candidate/job pair actually scored

    for evaluation in completed:
        assert evaluation.numeric_score is not None
        assert evaluation.policy_engine_version  # proves the real deterministic
        assert evaluation.scoring_policy_version  # policy engine ran, not a hand-inserted score
        assert evaluation.overall_result in {
            "STRONG_MATCH",
            "GOOD_MATCH",
            "POTENTIAL_MATCH",
            "NOT_MATCHED",
            "INSUFFICIENT_EVIDENCE",
        }


async def test_unknown_insufficient_evidence_case_present(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """At least one seeded candidate must exercise the UNKNOWN/manual-
    review path (never silently coerced into NOT_MATCHED) — the
    empty-employment-history demo candidate."""
    summary = await _seed(db_session, tmp_path)
    await db_session.commit()

    evaluations = await db_session.execute(
        select(Evaluation).where(
            Evaluation.tenant_id == summary.tenant_id, Evaluation.status == "COMPLETED"
        )
    )
    rows = evaluations.scalars().all()

    found_unknown = False
    for evaluation in rows:
        for criterion_result in evaluation.criterion_results or []:
            if criterion_result.get("status") == "UNKNOWN":
                found_unknown = True
                break
        if found_unknown:
            break
    assert found_unknown


async def test_structured_search_against_demo_state(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """No embedding/LLM provider required for STRUCTURED_ONLY — proves
    the demo dataset is searchable through the real search service
    without Ollama."""
    summary = await _seed(db_session, tmp_path)
    await db_session.commit()

    request = CandidateSearchRequest(
        mode=SearchMode.STRUCTURED_ONLY,
        required_filters=RequiredFilters(skills=["Java"]),
    )
    result = await search_candidates(db_session, tenant_id=summary.tenant_id, request=request)

    assert result.result_count == 2  # the two Java-backend demo candidates
    for item in result.results:
        assert item.mode == SearchMode.STRUCTURED_ONLY


async def test_reset_cannot_affect_non_demo_tenant(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    other_tenant = await create_tenant(db_session, name="Unrelated-Tenant-2")
    await create_candidate(db_session, tenant_id=other_tenant.id)
    await db_session.commit()

    await _seed(db_session, tmp_path)
    await db_session.commit()

    deleted = await reset_demo(db_session)
    await db_session.commit()
    assert deleted is True

    # The demo tenant itself is gone.
    demo_tenant = await db_session.execute(select(Tenant).where(Tenant.name == DEMO_TENANT_NAME))
    assert demo_tenant.scalars().first() is None

    # The unrelated tenant and its data are completely untouched.
    other_tenant_row = await db_session.get(Tenant, other_tenant.id)
    assert other_tenant_row is not None
    other_candidates = await db_session.execute(
        select(Candidate).where(Candidate.tenant_id == other_tenant.id)
    )
    assert len(other_candidates.scalars().all()) == 1


async def test_reset_with_no_demo_tenant_is_a_safe_noop(db_session: AsyncSession) -> None:
    deleted = await reset_demo(db_session)
    assert deleted is False


async def test_no_real_pii_or_secrets_in_demo_fixtures() -> None:
    """Static check on the demo dataset itself — every synthetic identity
    is unmistakably fake and every document is tagged as such."""
    for candidate in _demo_candidates():
        assert candidate.email.endswith("@example.invalid")
        assert candidate.phone.startswith("+994-00-000-")
        assert "Demo" in candidate.full_name
        assert any("SYNTHETIC DEMO DATA" in line for line in candidate.lines)
        # No .invalid/synthetic marker leaks into the disclaimer line itself —
        # confirms the disclaimer is plain text, not accidentally duplicating PII.
        disclaimer_lines = [line for line in candidate.lines if "SYNTHETIC DEMO DATA" in line]
        assert all("@" not in line for line in disclaimer_lines)
