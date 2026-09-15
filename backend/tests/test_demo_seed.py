"""Presentation-readiness synthetic demo bootstrap (issue #25 — chore, not
a product Slice). Verifies meyar.services.demo_seed_service produces real,
tenant-isolated, deterministic-evaluator-backed demo state without ever
touching the production LLM/embedding provider factories. All data is
synthetic — see fixtures/synthetic_cvs/README.md and
.claude/rules/testing.md."""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.core.auth import authenticate_raw_api_key
from meyar.core.password import verify_password
from meyar.ingestion.parsers.local_text_parser import LocalTextParser
from meyar.models.candidate import Candidate
from meyar.models.evaluation import Evaluation
from meyar.models.job import Job
from meyar.models.tenant import Tenant
from meyar.search.schemas import CandidateSearchRequest, RequiredFilters, SearchMode
from meyar.search.service import search_candidates
from meyar.services.api_key_repo import create_api_key
from meyar.services.audit_repo import record_event
from meyar.services.candidate_document_repo import get_candidate_document
from meyar.services.candidate_repo import create_candidate
from meyar.services.demo_seed_service import (
    DEMO_TENANT_MARKER_EVENT,
    DEMO_TENANT_NAME,
    DEMO_USER_USERNAME,
    DemoTenantAmbiguousError,
    _demo_candidates,
    reset_demo,
    seed_demo,
)
from meyar.services.job_criteria_repo import get_current_criteria_version
from meyar.services.tenant_membership_repo import get_membership_for_user_and_tenant
from meyar.services.tenant_repo import create_tenant
from meyar.services.user_repo import create_user, get_user_by_username
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
        evaluation_as_of_date=date(2026, 1, 1),
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
    assert second.api_key_plaintext is not None
    assert second.api_key_plaintext != first.api_key_plaintext

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
    idempotent no-op path activates as soon as any candidate exists. The
    marker event is written first in a genuine seed_demo call (see
    DEMO_TENANT_MARKER_EVENT), so a faithful "interrupted mid-run"
    simulation must include it — an unmarked same-named tenant is a
    different, deliberately-refused scenario (see the ambiguity tests
    below)."""
    tenant = await create_tenant(db_session, name=DEMO_TENANT_NAME)
    await record_event(db_session, tenant_id=tenant.id, event_type=DEMO_TENANT_MARKER_EVENT)
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


async def test_reseed_rotates_demo_key_and_revokes_the_previous_one(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """Section 20 gap: an idempotent reseed must always hand the operator
    a usable plaintext credential, and must never leave key sprawl behind
    — exactly one demo API key is active after a reseed."""
    from meyar.models.api_key import ApiKey

    first = await _seed(db_session, tmp_path)
    await db_session.commit()

    second = await _seed(db_session, tmp_path)
    await db_session.commit()

    assert second.api_key_plaintext is not None
    login_with_old = await authenticate_raw_api_key(db_session, first.api_key_plaintext)
    assert login_with_old is None  # revoked, no longer usable
    login_with_new = await authenticate_raw_api_key(db_session, second.api_key_plaintext)
    assert login_with_new is not None

    keys = await db_session.execute(
        select(ApiKey).where(ApiKey.tenant_id == second.tenant_id)
    )
    active_keys = [key for key in keys.scalars().all() if key.revoked_at is None]
    assert len(active_keys) == 1
    assert active_keys[0].id == login_with_new.id


async def test_reseed_key_rotation_never_touches_another_tenant(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    other_tenant = await create_tenant(db_session, name="Unrelated-Tenant")
    _other_key, other_plaintext = await create_api_key(
        db_session, tenant_id=other_tenant.id, env="test"
    )
    await db_session.commit()

    await _seed(db_session, tmp_path)
    await db_session.commit()
    await _seed(db_session, tmp_path)  # triggers the rotation path
    await db_session.commit()

    still_active = await authenticate_raw_api_key(db_session, other_plaintext)
    assert still_active is not None


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


# ---------------------------------------------------------------------------
# Reset-safety hardening (independent-audit P0 follow-up): display name
# alone must never be sufficient proof of demo-tenant ownership. See
# demo_seed_service._find_demo_tenant and docs/DECISIONS.md D-022.
# ---------------------------------------------------------------------------


async def test_reset_refuses_unmarked_same_name_tenant_and_leaves_it_untouched(
    db_session: AsyncSession,
) -> None:
    """Requirement 1: an ordinary tenant that merely happens to be named
    exactly DEMO_TENANT_NAME, with no real demo tenant anywhere, must
    cause reset to refuse outright — never adopted, never deleted."""
    ordinary = await create_tenant(db_session, name=DEMO_TENANT_NAME)
    await create_candidate(db_session, tenant_id=ordinary.id)
    await db_session.commit()

    with pytest.raises(DemoTenantAmbiguousError):
        await reset_demo(db_session)

    # Completely untouched: same id, same name, candidate still present.
    survivor = await db_session.get(Tenant, ordinary.id)
    assert survivor is not None
    assert survivor.name == DEMO_TENANT_NAME
    candidates = await db_session.execute(
        select(Candidate).where(Candidate.tenant_id == ordinary.id)
    )
    assert len(candidates.scalars().all()) == 1


async def test_seed_refuses_unmarked_same_name_tenant_and_leaves_it_untouched(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """Same scenario as above, through seed_demo (no --reset) — must also
    refuse rather than silently creating a second same-named tenant or
    adopting the unmarked one."""
    ordinary = await create_tenant(db_session, name=DEMO_TENANT_NAME)
    await create_candidate(db_session, tenant_id=ordinary.id)
    await db_session.commit()

    with pytest.raises(DemoTenantAmbiguousError):
        await _seed(db_session, tmp_path)

    tenants = await db_session.execute(select(Tenant).where(Tenant.name == DEMO_TENANT_NAME))
    rows = tenants.scalars().all()
    assert len(rows) == 1  # no second same-named tenant was created
    assert rows[0].id == ordinary.id
    candidates = await db_session.execute(
        select(Candidate).where(Candidate.tenant_id == ordinary.id)
    )
    assert len(candidates.scalars().all()) == 1  # untouched, not reseeded


async def test_reset_refuses_when_marked_and_unmarked_tenants_share_name(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """Requirement 2: a real, properly-marked demo tenant AND an ordinary
    tenant sharing the exact same display name — the ambiguity itself
    must block the operation. Neither tenant may be deleted, even though
    one of them is legitimately the demo tenant."""
    real_summary = await _seed(db_session, tmp_path)
    await db_session.commit()

    colliding = await create_tenant(db_session, name=DEMO_TENANT_NAME)
    await create_candidate(db_session, tenant_id=colliding.id)
    await db_session.commit()

    with pytest.raises(DemoTenantAmbiguousError):
        await reset_demo(db_session)

    # Both tenants survive, completely unmodified.
    real_tenant = await db_session.get(Tenant, real_summary.tenant_id)
    assert real_tenant is not None
    real_candidates = await db_session.execute(
        select(Candidate).where(Candidate.tenant_id == real_summary.tenant_id)
    )
    assert len(real_candidates.scalars().all()) == len(_demo_candidates())

    colliding_tenant = await db_session.get(Tenant, colliding.id)
    assert colliding_tenant is not None
    colliding_candidates = await db_session.execute(
        select(Candidate).where(Candidate.tenant_id == colliding.id)
    )
    assert len(colliding_candidates.scalars().all()) == 1


async def test_seed_with_one_valid_marked_demo_tenant_works_normally(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """Requirement 3: exactly one tenant named DEMO_TENANT_NAME, and it
    carries the bootstrap marker — seed/reset may operate on it as
    designed (the ordinary, non-collision case)."""
    summary = await _seed(db_session, tmp_path)
    await db_session.commit()

    marker = await db_session.execute(
        select(Job).where(Job.tenant_id == summary.tenant_id)  # sanity: real data exists
    )
    assert len(marker.scalars().all()) == 2

    deleted = await reset_demo(db_session)
    await db_session.commit()
    assert deleted is True

    survivor = await db_session.execute(select(Tenant).where(Tenant.name == DEMO_TENANT_NAME))
    assert survivor.scalars().first() is None


async def test_repeated_seed_reset_cycle_remains_idempotent(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """Requirement 5: cycling seed -> reset -> seed -> reset -> seed
    several times never accumulates extra tenants/candidates and never
    raises — each cycle is a clean, positively-identified operation."""
    for _ in range(3):
        summary = await _seed(db_session, tmp_path)
        await db_session.commit()
        assert summary.candidates_created in (0, len(_demo_candidates()))

        tenants = await db_session.execute(
            select(Tenant).where(Tenant.name == DEMO_TENANT_NAME)
        )
        assert len(tenants.scalars().all()) == 1

        deleted = await reset_demo(db_session)
        await db_session.commit()
        assert deleted is True

    tenants_after = await db_session.execute(
        select(Tenant).where(Tenant.name == DEMO_TENANT_NAME)
    )
    assert tenants_after.scalars().first() is None


# ---------------------------------------------------------------------------
# Slice 1 — synthetic demo HUMAN login (issue #30). Distinct from, and in
# addition to, the machine API-key credential exercised above.
# ---------------------------------------------------------------------------


async def test_seed_demo_creates_a_working_human_login(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    summary = await _seed(db_session, tmp_path)
    await db_session.commit()

    assert summary.human_username == DEMO_USER_USERNAME
    assert summary.human_temp_password

    user = await get_user_by_username(db_session, DEMO_USER_USERNAME)
    assert user is not None
    assert user.is_active
    assert verify_password(user.password_hash, summary.human_temp_password)
    assert user.password_hash != summary.human_temp_password  # never stored in plaintext

    membership = await get_membership_for_user_and_tenant(
        db_session, user_id=user.id, tenant_id=summary.tenant_id
    )
    assert membership is not None
    assert membership.is_active
    assert membership.role == "HR_USER"


async def test_reseed_rotates_demo_human_password_and_invalidates_the_old_one(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    first = await _seed(db_session, tmp_path)
    await db_session.commit()

    second = await _seed(db_session, tmp_path)
    await db_session.commit()

    assert second.human_username == first.human_username
    assert second.human_temp_password != first.human_temp_password

    user = await get_user_by_username(db_session, DEMO_USER_USERNAME)
    assert user is not None
    assert not verify_password(user.password_hash, first.human_temp_password)
    assert verify_password(user.password_hash, second.human_temp_password)


async def test_demo_human_login_never_touches_an_unrelated_same_named_user(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """A real operator account that happens to share DEMO_USER_USERNAME
    (e.g. a name collision on a shared dev DB) must never have its
    password silently rotated by seed-demo — the same positive-
    identification discipline as the tenant-name-collision protections
    above, applied to the human login."""
    unrelated = await create_user(
        db_session, username=DEMO_USER_USERNAME, plaintext_password="unrelated-real-password-1"
    )
    await db_session.commit()

    with pytest.raises(DemoTenantAmbiguousError):
        await _seed(db_session, tmp_path)

    survivor = await get_user_by_username(db_session, DEMO_USER_USERNAME)
    assert survivor is not None
    assert survivor.id == unrelated.id
    assert verify_password(survivor.password_hash, "unrelated-real-password-1")
