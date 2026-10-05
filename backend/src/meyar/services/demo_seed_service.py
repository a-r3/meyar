"""Presentation-readiness synthetic demo bootstrap (issue #25, chore —
not a product Slice). Populates one clearly-marked, isolated demo tenant
with realistic-but-obviously-synthetic candidates, documents, jobs, and
deterministic evaluations, entirely through the real repository/service
layers `meyar.services.*`/`meyar.evaluation.service` — never a parallel
data path.

This module is the ONLY place a stand-in LLM/embedding provider is ever
constructed outside `tests/`. `_DemoLLMProvider`/`_DemoEmbeddingProvider`
below are never wired into `meyar.llm.dependency.get_llm_provider` or
`meyar.embedding.dependency.get_embedding_provider` — the real,
Ollama-backed factories used by every request-serving code path (API, UI,
CLI extract-profile/embed-candidate) are completely untouched. Seeding
only ever runs from an explicit operator command (`meyar seed-demo`),
never at application startup, never automatically. All extraction output
here is pre-written, evidence-matched synthetic data (the same technique
`tests/fakes.py` uses), not a live model's output — the CLI must never
claim otherwise. See docs/LOCAL_DEMO.md."""

import io
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import date

from docx import Document
from docx.shared import Inches
from PIL import Image, ImageDraw
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.agent.capabilities.contracts import AgentPlanContext, AgentPlanProposal
from meyar.agent.clarification_schemas import ClarificationAnswerProposal
from meyar.agent.schemas import (
    GroundedFact,
    GroundedSelection,
    JDCriteriaDraft,
    RequirementSpan,
)
from meyar.core.roles import ROLE_HR_USER
from meyar.embedding.provider import EmbeddingResult
from meyar.embedding.serializer import SERIALIZER_VERSION
from meyar.evaluation.service import evaluate_and_score_candidate
from meyar.extraction.identity_service import (
    infer_candidate_identity,
    load_identity_view,
    persist_identity_outcome,
)
from meyar.extraction.service import (
    infer_candidate_profile,
    load_profile_view,
    persist_profile_outcome,
)
from meyar.extraction.view import ProfessionalDocumentView
from meyar.ingestion.parser import DocumentParser
from meyar.llm.provider import LLMResultProvenance
from meyar.models.audit_event import AuditEvent
from meyar.models.candidate_document import CandidateDocument
from meyar.models.candidate_photo_version import CandidatePhotoVersion
from meyar.models.tenant import Tenant
from meyar.models.tenant_membership import TenantMembership
from meyar.models.user import User
from meyar.schemas.candidate_identity import CandidateIdentityExtraction, IdentityFieldItem
from meyar.schemas.candidate_profile import (
    CandidateProfileExtraction,
    CertificationItem,
    DomainExperienceItem,
    EducationItem,
    EmploymentItem,
    EvidenceRef,
    LanguageItem,
    SkillExperienceItem,
    SkillItem,
)
from meyar.schemas.criteria import CriterionIn, CriterionKind, CriterionType
from meyar.search.planner_schemas import PlannerDraft
from meyar.services.api_key_repo import create_api_key, revoke_active_api_keys_for_tenant
from meyar.services.audit_repo import record_event
from meyar.services.browser_session_repo import revoke_sessions_for_membership
from meyar.services.candidate_document_service import ingest_candidate_document
from meyar.services.candidate_embedding_repo import EmbeddingCompatibility
from meyar.services.candidate_embedding_service import (
    persist_embedding,
    prepare_embedding,
    record_embedding_reused,
    validate_embedding_result,
)
from meyar.services.candidate_repo import count_candidates_for_tenant, create_candidate
from meyar.services.job_criteria_repo import create_criteria_version
from meyar.services.job_repo import create_job
from meyar.services.profile_authority import get_current_authorized_profile
from meyar.services.storage_authority import storage_writer
from meyar.services.storage_recovery import track_staged
from meyar.services.tenant_membership_repo import create_membership
from meyar.services.tenant_repo import create_tenant
from meyar.services.user_repo import create_user, get_user_by_id, get_user_by_username, set_password
from meyar.storage.base import DocumentStorage
from meyar.storage.photo import LocalPhotoStorage

# The display name every demo tenant is created with. NEVER sufficient
# proof of demo ownership by itself — an ordinary tenant could share this
# exact name by accident or by another operator's choice. Positive
# identification always additionally requires DEMO_TENANT_MARKER_EVENT
# (see _find_demo_tenant) before seed-demo/reset-demo will touch a
# tenant. See docs/DECISIONS.md D-022.
DEMO_TENANT_NAME = "MEYAR Demo (Synthetic)"

# Written once, as an AuditEvent, at the moment seed_demo creates a new
# demo tenant — reusing the existing tenant-scoped audit_events table as
# the durable positive-identification marker, deliberately avoiding a
# schema migration for this. Never written anywhere else, and audit
# events are only ever created by trusted application code (no route
# lets a client write an arbitrary event_type for a tenant it doesn't
# own), so this is a reliable proof-of-origin marker in this operator/
# dev-tool context.
DEMO_TENANT_MARKER_EVENT = "DEMO_TENANT_BOOTSTRAPPED"

# The synthetic demo HUMAN login (Slice 1, issue #30) — distinct from,
# and in addition to, the machine API key summary.api_key_plaintext below.
# Username and membership are never proof of User ownership. Only an exact
# trusted creation marker plus exclusive membership permits User mutation.
DEMO_USER_USERNAME = "demo.hr"
DEMO_HUMAN_MARKER_EVENT = "DEMO_HUMAN_BOOTSTRAPPED"


class _DemoLLMProvider:
    """See module docstring. Not the production LLMProvider — a
    per-candidate deterministic stand-in used only inside seed_demo.
    Structurally satisfies the meyar.llm.provider.LLMProvider Protocol
    (provider_name/model_name/model_revision + the two extraction
    methods actually used here)."""

    provider_name = "demo-synthetic"
    model_name = "demo-synthetic-v1"
    model_revision = ""

    def __init__(
        self,
        extraction: CandidateProfileExtraction,
        identity_extraction: CandidateIdentityExtraction,
    ) -> None:
        self._extraction = extraction
        self._identity_extraction = identity_extraction

    async def extract_candidate_profile(
        self, view: ProfessionalDocumentView
    ) -> tuple[CandidateProfileExtraction, str]:
        return self._extraction, self.model_name

    async def extract_candidate_identity(
        self, view: ProfessionalDocumentView
    ) -> tuple[CandidateIdentityExtraction, str]:
        return self._identity_extraction, self.model_name

    async def plan_candidate_search(
        self, natural_language_request: str, *, repair: bool = False
    ) -> tuple[PlannerDraft, LLMResultProvenance]:
        raise NotImplementedError("The demo seed provider never plans searches.")

    async def propose_agent_plan(
        self, *, context: AgentPlanContext, repair: bool = False
    ) -> tuple[AgentPlanProposal, LLMResultProvenance]:
        raise NotImplementedError("The demo seed provider never runs the agent planner.")

    async def select_grounded_facts(
        self,
        *,
        question: str,
        facts: list[GroundedFact],
        repair: bool = False,
    ) -> tuple[GroundedSelection, LLMResultProvenance]:
        raise NotImplementedError("The demo seed provider never runs the agent loop.")

    async def draft_job_criteria(
        self,
        jd_text: str,
        *,
        requirement_spans: list[RequirementSpan],
        span_hints: dict[str, dict[str, str]] | None = None,
        repair: bool = False,
    ) -> tuple[JDCriteriaDraft, LLMResultProvenance]:
        raise NotImplementedError("The demo seed provider never drafts job criteria.")

    async def resolve_clarification_answer(
        self,
        *,
        clarification_type: str,
        allowed_answers: list[str],
        answer_text: str,
        repair: bool = False,
    ) -> tuple[ClarificationAnswerProposal, LLMResultProvenance]:
        raise NotImplementedError("The demo seed provider never runs the agent loop.")

    async def health(self) -> dict:
        return {"reachable": True, "model": self.model_name, "model_available": True}


class _DemoEmbeddingProvider:
    """See module docstring. Not the production EmbeddingProvider."""

    provider_name = "demo-synthetic-embedding"
    model_name = "demo-synthetic-embedding-v1"
    model_revision = ""

    def __init__(self, vector: list[float]) -> None:
        self._vector = vector

    async def embed(self, text: str) -> EmbeddingResult:
        return EmbeddingResult(
            vector=self._vector,
            dimensions=len(self._vector),
            provider=self.provider_name,
            model_name=self.model_name,
            model_revision=self.model_revision,
        )

    async def health(self) -> dict:
        return {"reachable": True, "model": self.model_name, "model_available": True}


@dataclass(frozen=True)
class _DemoCandidate:
    """One synthetic candidate. `lines` becomes the real DOCX document
    (one real paragraph per line, so LocalTextParser's block_index equals
    the list index exactly — no separate index bookkeeping). Every
    evidence quote below is a verbatim substring of one of these lines,
    verified the same way a live extraction's evidence is verified."""

    doc_filename: str
    lines: list[str]
    full_name: str
    email: str
    phone: str
    name_line: int
    contact_line: int
    skills: list[tuple[str, int]]  # (skill_name, line_index)
    employment: list[dict]  # title, organization, start_date, end_date, is_current, line_index
    education: list[dict]  # institution, degree, field_of_study, date, line_index
    certifications: list[dict] = field(default_factory=list)  # name, issuer, date, line_index
    languages: list[dict] = field(default_factory=list)  # language, proficiency, line_index
    skill_experience: list[dict] = field(default_factory=list)
    domain_experience: list[dict] = field(default_factory=list)
    embedding_vector: list[float] = field(default_factory=lambda: [0.1, 0.2, 0.3, 0.4])


def _demo_candidates() -> list[_DemoCandidate]:
    disclaimer = "SYNTHETIC DEMO DATA — NOT A REAL PERSON — see docs/LOCAL_DEMO.md"
    return [
        _DemoCandidate(
            doc_filename="candidate-01-java-backend.docx",
            full_name="Aysel Demo-Karimova",
            email="aysel.demo@example.invalid",
            phone="+994-00-000-0001",
            lines=[
                disclaimer,
                "Aysel Demo-Karimova",
                "Email: aysel.demo@example.invalid | Phone: +994-00-000-0001",
                "Skills: Java, Spring Boot, PostgreSQL",
                "Backend Developer — Northline Software (2018 - 2025)",
                "BSc Computer Science, Baku State University (2014 - 2018)",
                "Languages: English (Fluent)",
            ],
            name_line=1,
            contact_line=2,
            skills=[("Java", 3), ("Spring Boot", 3), ("PostgreSQL", 3)],
            employment=[
                {
                    "title": "Backend Developer",
                    "organization": "Northline Software",
                    "start_date": "2018",
                    "end_date": "2025",
                    "is_current": False,
                    "line": 4,
                }
            ],
            education=[
                {
                    "institution": "Baku State University",
                    "degree": "BSc Computer Science",
                    "field_of_study": "Computer Science",
                    "date": "2014 - 2018",
                    "line": 5,
                }
            ],
            languages=[{"language": "English", "proficiency": "Fluent", "line": 6}],
            embedding_vector=[0.9, 0.1, 0.0, 0.0],
        ),
        _DemoCandidate(
            doc_filename="candidate-02-python-data.docx",
            full_name="Tural Demo-Aliyev",
            email="tural.demo@example.invalid",
            phone="+994-00-000-0002",
            lines=[
                disclaimer,
                "Tural Demo-Aliyev",
                "Email: tural.demo@example.invalid | Phone: +994-00-000-0002",
                "Skills: Python, SQL, Pandas, Machine Learning",
                "Data Analyst — Caspian Analytics (2021 - 2025)",
                "BSc Data Science, ADA University (2017 - 2021)",
            ],
            name_line=1,
            contact_line=2,
            skills=[
                ("Python", 3),
                ("SQL", 3),
                ("Pandas", 3),
                ("Machine Learning", 3),
            ],
            employment=[
                {
                    "title": "Data Analyst",
                    "organization": "Caspian Analytics",
                    "start_date": "2021",
                    "end_date": "2025",
                    "is_current": False,
                    "line": 4,
                }
            ],
            education=[
                {
                    "institution": "ADA University",
                    "degree": "BSc Data Science",
                    "field_of_study": "Data Science",
                    "date": "2017 - 2021",
                    "line": 5,
                }
            ],
            embedding_vector=[0.0, 0.9, 0.1, 0.0],
        ),
        _DemoCandidate(
            doc_filename="candidate-03-aml-compliance-senior.docx",
            full_name="Nigar Demo-Huseynova",
            email="nigar.demo@example.invalid",
            phone="+994-00-000-0003",
            lines=[
                disclaimer,
                "Nigar Demo-Huseynova",
                "Email: nigar.demo@example.invalid | Phone: +994-00-000-0003",
                "Skills: AML, KYC, Compliance Monitoring, Risk Assessment",
                "Compliance Officer — Zerafshan National Bank (2017 - 2025)",
                "BA Economics, Azerbaijan State University of Economics (2012 - 2016)",
                "Certification: ACAMS Certified Anti-Money Laundering Specialist — ACAMS (2019)",
                "Languages: Azerbaijani (Native), English (Fluent)",
            ],
            name_line=1,
            contact_line=2,
            skills=[
                ("AML", 3),
                ("KYC", 3),
                ("Compliance Monitoring", 3),
                ("Risk Assessment", 3),
            ],
            employment=[
                {
                    "title": "Compliance Officer",
                    "organization": "Zerafshan National Bank",
                    "start_date": "2017",
                    "end_date": "2025",
                    "is_current": False,
                    "line": 4,
                }
            ],
            education=[
                {
                    "institution": "Azerbaijan State University of Economics",
                    "degree": "BA Economics",
                    "field_of_study": "Economics",
                    "date": "2012 - 2016",
                    "line": 5,
                }
            ],
            certifications=[
                {
                    "name": "ACAMS Certified Anti-Money Laundering Specialist",
                    "issuer": "ACAMS",
                    "date": "2019",
                    "line": 6,
                }
            ],
            languages=[
                {"language": "Azerbaijani", "proficiency": "Native", "line": 7},
                {"language": "English", "proficiency": "Fluent", "line": 7},
            ],
            embedding_vector=[0.0, 0.0, 0.9, 0.1],
        ),
        _DemoCandidate(
            doc_filename="candidate-04-devops.docx",
            full_name="Elvin Demo-Mammadov",
            email="elvin.demo@example.invalid",
            phone="+994-00-000-0004",
            lines=[
                disclaimer,
                "Elvin Demo-Mammadov",
                "Email: elvin.demo@example.invalid | Phone: +994-00-000-0004",
                "Skills: Kubernetes, Docker, AWS, Terraform, CI/CD",
                "DevOps Engineer — CloudBridge Systems (2020 - 2025)",
                "BSc Information Technology, Sumgait State University (2016 - 2020)",
            ],
            name_line=1,
            contact_line=2,
            skills=[
                ("Kubernetes", 3),
                ("Docker", 3),
                ("AWS", 3),
                ("Terraform", 3),
                ("CI/CD", 3),
            ],
            employment=[
                {
                    "title": "DevOps Engineer",
                    "organization": "CloudBridge Systems",
                    "start_date": "2020",
                    "end_date": "2025",
                    "is_current": False,
                    "line": 4,
                }
            ],
            education=[
                {
                    "institution": "Sumgait State University",
                    "degree": "BSc Information Technology",
                    "field_of_study": "Information Technology",
                    "date": "2016 - 2020",
                    "line": 5,
                }
            ],
            embedding_vector=[0.1, 0.0, 0.0, 0.9],
        ),
        _DemoCandidate(
            doc_filename="candidate-05-frontend.docx",
            full_name="Lala Demo-Sultanova",
            email="lala.demo@example.invalid",
            phone="+994-00-000-0005",
            lines=[
                disclaimer,
                "Lala Demo-Sultanova",
                "Email: lala.demo@example.invalid | Phone: +994-00-000-0005",
                "Skills: React, TypeScript, CSS",
                "Frontend Developer — PixelForge Studio (2022 - 2025)",
                "BSc Software Engineering, Khazar University (2018 - 2022)",
            ],
            name_line=1,
            contact_line=2,
            skills=[("React", 3), ("TypeScript", 3), ("CSS", 3)],
            employment=[
                {
                    "title": "Frontend Developer",
                    "organization": "PixelForge Studio",
                    "start_date": "2022",
                    "end_date": "2025",
                    "is_current": False,
                    "line": 4,
                }
            ],
            education=[
                {
                    "institution": "Khazar University",
                    "degree": "BSc Software Engineering",
                    "field_of_study": "Software Engineering",
                    "date": "2018 - 2022",
                    "line": 5,
                }
            ],
            embedding_vector=[0.2, 0.2, 0.0, 0.2],
        ),
        _DemoCandidate(
            doc_filename="candidate-06-business-analyst.docx",
            full_name="Kamran Demo-Rzayev",
            email="kamran.demo@example.invalid",
            phone="+994-00-000-0006",
            lines=[
                disclaimer,
                "Kamran Demo-Rzayev",
                "Email: kamran.demo@example.invalid | Phone: +994-00-000-0006",
                "Skills: Business Analysis, Stakeholder Management, SQL",
                "Business Analyst — Meridian Consulting (2018 - 2025)",
                "MBA, UNEC Business School (2016 - 2018)",
            ],
            name_line=1,
            contact_line=2,
            skills=[
                ("Business Analysis", 3),
                ("Stakeholder Management", 3),
                ("SQL", 3),
            ],
            employment=[
                {
                    "title": "Business Analyst",
                    "organization": "Meridian Consulting",
                    "start_date": "2018",
                    "end_date": "2025",
                    "is_current": False,
                    "line": 4,
                }
            ],
            education=[
                {
                    "institution": "UNEC Business School",
                    "degree": "MBA",
                    "field_of_study": "Business Administration",
                    "date": "2016 - 2018",
                    "line": 5,
                }
            ],
            embedding_vector=[0.3, 0.1, 0.1, 0.1],
        ),
        _DemoCandidate(
            doc_filename="candidate-07-java-backend-senior.docx",
            full_name="Farid Demo-Nabiyev",
            email="farid.demo@example.invalid",
            phone="+994-00-000-0007",
            lines=[
                disclaimer,
                "Farid Demo-Nabiyev",
                "Email: farid.demo@example.invalid | Phone: +994-00-000-0007",
                "Skills: Java, Microservices, Kafka, AWS",
                "Senior Backend Engineer — Northbridge Software (2015 - 2025)",
                "MSc Computer Engineering, Baku Higher Oil School (2013 - 2015)",
                "Languages: English (Fluent), Azerbaijani (Native)",
            ],
            name_line=1,
            contact_line=2,
            skills=[
                ("Java", 3),
                ("Microservices", 3),
                ("Kafka", 3),
                ("AWS", 3),
            ],
            employment=[
                {
                    "title": "Senior Backend Engineer",
                    "organization": "Northbridge Software",
                    "start_date": "2015",
                    "end_date": "2025",
                    "is_current": False,
                    "line": 4,
                }
            ],
            education=[
                {
                    "institution": "Baku Higher Oil School",
                    "degree": "MSc Computer Engineering",
                    "field_of_study": "Computer Engineering",
                    "date": "2013 - 2015",
                    "line": 5,
                }
            ],
            languages=[
                {"language": "English", "proficiency": "Fluent", "line": 6},
                {"language": "Azerbaijani", "proficiency": "Native", "line": 6},
            ],
            embedding_vector=[0.85, 0.05, 0.0, 0.1],
        ),
        _DemoCandidate(
            doc_filename="candidate-08-aml-junior-insufficient.docx",
            full_name="Sabina Demo-Guliyeva",
            email="sabina.demo@example.invalid",
            phone="+994-00-000-0008",
            lines=[
                disclaimer,
                "Sabina Demo-Guliyeva",
                "Email: sabina.demo@example.invalid | Phone: +994-00-000-0008",
                "Skills: AML",
                "BA Economics, Ganja State University (2021 - 2025)",
            ],
            name_line=1,
            contact_line=2,
            skills=[("AML", 3)],
            employment=[],  # deliberately empty: demonstrates the
            # EXPERIENCE-criterion UNKNOWN/insufficient-evidence path — no
            # employment history to evaluate, never silently treated as a
            # failure (see meyar.evaluation.evaluators.evaluate_experience).
            education=[
                {
                    "institution": "Ganja State University",
                    "degree": "BA Economics",
                    "field_of_study": "Economics",
                    "date": "2021 - 2025",
                    "line": 4,
                }
            ],
            embedding_vector=[0.0, 0.1, 0.5, 0.0],
        ),
        _DemoCandidate(
            doc_filename="candidate-09-sre-banking.docx",
            full_name="Rashad Demo-Isayev",
            email="rashad.demo@example.invalid",
            phone="+994-00-000-0009",
            lines=[
                disclaimer,
                "Rashad Demo-Isayev",
                "Email: rashad.demo@example.invalid | Phone: +994-00-000-0009",
                "Skills: Python, Site Reliability, Monitoring, Banking Systems Integration",
                (
                    "Site Reliability Engineer — Zerafshan National Bank IT "
                    "(2019 - 2025); Python used for banking systems integration "
                    "throughout 2019 - 2025."
                ),
                "BSc Computer Science, Baku Engineering University (2015 - 2019)",
                "Languages: English (B2)",
            ],
            name_line=1,
            contact_line=2,
            skills=[
                ("Python", 3),
                ("Site Reliability", 3),
                ("Monitoring", 3),
                ("Banking Systems Integration", 3),
            ],
            employment=[
                {
                    "title": "Site Reliability Engineer",
                    "organization": "Zerafshan National Bank IT",
                    "start_date": "2019",
                    "end_date": "2025",
                    "is_current": False,
                    "line": 4,
                }
            ],
            education=[
                {
                    "institution": "Baku Engineering University",
                    "degree": "BSc Computer Science",
                    "field_of_study": "Computer Science",
                    "date": "2015 - 2019",
                    "line": 5,
                }
            ],
            languages=[{"language": "English", "proficiency": "B2", "line": 6}],
            skill_experience=[
                {
                    "skill_name": "Python",
                    "employment_index": 0,
                    "start_date": "2019",
                    "end_date": "2025",
                    "is_current": False,
                    "line": 4,
                }
            ],
            domain_experience=[
                {
                    "domain": "Banking",
                    "employment_index": 0,
                    "start_date": "2019",
                    "end_date": "2025",
                    "is_current": False,
                    "line": 4,
                }
            ],
            embedding_vector=[0.15, 0.0, 0.2, 0.6],
        ),
    ]


def _synthetic_portrait(index: int) -> bytes:
    """Nine deterministic geometric illustrations; no person, font, or network asset."""
    backgrounds = [
        "#DCEBE4", "#E6E3F2", "#E2ECF2", "#F0E7DD", "#E2E9D8",
        "#EDE1E7", "#DFE6EF", "#E8E7D9", "#E5E0ED",
    ]
    jackets = [
        "#285B57", "#4E4678", "#355E77", "#795845", "#526E40",
        "#794D63", "#365278", "#68633B", "#594F76",
    ]
    skin = ["#D79B75", "#B77D5D", "#E1AC84", "#A66F55", "#C98968"]
    image = Image.new("RGB", (320, 420), backgrounds[index % 9])
    draw = ImageDraw.Draw(image)
    draw.ellipse((18, 245, 302, 535), fill=jackets[index % 9])
    draw.rectangle((139, 211, 181, 286), fill=skin[index % 5])
    draw.ellipse((75, 54, 245, 258), fill="#36333D")
    draw.ellipse((91, 75, 229, 245), fill=skin[index % 5])
    draw.pieslice((78, 53, 246, 172), 180, 360, fill="#36333D")
    draw.ellipse((128, 157, 134, 163), fill="#36333D")
    draw.ellipse((185, 157, 191, 163), fill="#36333D")
    draw.arc((145, 180, 177, 202), 0, 180, fill="#7B4D49", width=2)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _build_docx(lines: list[str], portrait_index: int | None = None) -> bytes:
    document = Document()
    for line in lines:
        document.add_paragraph(line)
    if portrait_index is not None:
        document.add_picture(io.BytesIO(_synthetic_portrait(portrait_index)), width=Inches(1.35))
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _profile_extraction(candidate: _DemoCandidate) -> CandidateProfileExtraction:
    return CandidateProfileExtraction(
        skills=[
            SkillItem(
                name=name,
                evidence=[
                    EvidenceRef(page=1, block_index=line, quote=candidate.lines[line])
                ],
            )
            for name, line in candidate.skills
        ],
        employment_history=[
            EmploymentItem(
                title=entry["title"],
                organization=entry["organization"],
                start_date=entry["start_date"],
                end_date=entry["end_date"],
                is_current=entry["is_current"],
                evidence=[
                    EvidenceRef(
                        page=1, block_index=entry["line"], quote=candidate.lines[entry["line"]]
                    )
                ],
            )
            for entry in candidate.employment
        ],
        education=[
            EducationItem(
                institution=entry["institution"],
                degree=entry["degree"],
                field_of_study=entry["field_of_study"],
                date=entry["date"],
                evidence=[
                    EvidenceRef(
                        page=1, block_index=entry["line"], quote=candidate.lines[entry["line"]]
                    )
                ],
            )
            for entry in candidate.education
        ],
        certifications=[
            CertificationItem(
                name=entry["name"],
                issuer=entry["issuer"],
                date=entry["date"],
                evidence=[
                    EvidenceRef(
                        page=1, block_index=entry["line"], quote=candidate.lines[entry["line"]]
                    )
                ],
            )
            for entry in candidate.certifications
        ],
        languages=[
            LanguageItem(
                language=entry["language"],
                proficiency=entry["proficiency"],
                evidence=[
                    EvidenceRef(
                        page=1, block_index=entry["line"], quote=candidate.lines[entry["line"]]
                    )
                ],
            )
            for entry in candidate.languages
        ],
        skill_experience=[
            SkillExperienceItem(
                skill_name=entry["skill_name"],
                employment_index=entry["employment_index"],
                start_date=entry["start_date"],
                end_date=entry["end_date"],
                is_current=entry["is_current"],
                evidence=[
                    EvidenceRef(
                        page=1, block_index=entry["line"], quote=candidate.lines[entry["line"]]
                    )
                ],
            )
            for entry in candidate.skill_experience
        ],
        domain_experience=[
            DomainExperienceItem(
                domain=entry["domain"],
                employment_index=entry["employment_index"],
                start_date=entry["start_date"],
                end_date=entry["end_date"],
                is_current=entry["is_current"],
                evidence=[
                    EvidenceRef(
                        page=1, block_index=entry["line"], quote=candidate.lines[entry["line"]]
                    )
                ],
            )
            for entry in candidate.domain_experience
        ],
    )


def _identity_extraction(candidate: _DemoCandidate) -> CandidateIdentityExtraction:
    return CandidateIdentityExtraction(
        full_name=IdentityFieldItem(
            value=candidate.full_name,
            evidence=[
                EvidenceRef(
                    page=1,
                    block_index=candidate.name_line,
                    quote=candidate.lines[candidate.name_line],
                )
            ],
        ),
        email=IdentityFieldItem(
            value=candidate.email,
            evidence=[
                EvidenceRef(
                    page=1,
                    block_index=candidate.contact_line,
                    quote=candidate.lines[candidate.contact_line],
                )
            ],
        ),
        phone=IdentityFieldItem(
            value=candidate.phone,
            evidence=[
                EvidenceRef(
                    page=1,
                    block_index=candidate.contact_line,
                    quote=candidate.lines[candidate.contact_line],
                )
            ],
        ),
    )


def _demo_jobs() -> list[dict]:
    return [
        {
            "title": "Senior Backend Engineer",
            "criteria": [
                CriterionIn(
                    id="java_skill",
                    kind=CriterionKind.SKILL,
                    type=CriterionType.MUST_HAVE,
                    label="Java",
                    value="Java",
                    weight=3,
                ),
                CriterionIn(
                    id="min_backend_experience",
                    kind=CriterionKind.EXPERIENCE,
                    type=CriterionType.MUST_HAVE,
                    label="Minimum backend engineering experience",
                    min_years=5,
                    weight=3,
                ),
                CriterionIn(
                    id="aws_cert_preferred",
                    kind=CriterionKind.CERTIFICATION,
                    type=CriterionType.PREFERRED,
                    label="AWS Certified Solutions Architect",
                    value="AWS Certified Solutions Architect",
                    weight=1,
                ),
                CriterionIn(
                    id="english_preferred",
                    kind=CriterionKind.LANGUAGE,
                    type=CriterionType.PREFERRED,
                    label="English proficiency",
                    value="English",
                    required_level="Fluent",
                    weight=1,
                ),
            ],
        },
        {
            "title": "AML / Compliance Specialist",
            "criteria": [
                CriterionIn(
                    id="aml_skill",
                    kind=CriterionKind.SKILL,
                    type=CriterionType.MUST_HAVE,
                    label="AML",
                    value="AML",
                    weight=3,
                ),
                CriterionIn(
                    id="acams_cert",
                    kind=CriterionKind.CERTIFICATION,
                    type=CriterionType.MUST_HAVE,
                    label="ACAMS certification",
                    value="ACAMS Certified Anti-Money Laundering Specialist",
                    weight=3,
                ),
                CriterionIn(
                    id="min_compliance_experience",
                    kind=CriterionKind.EXPERIENCE,
                    type=CriterionType.MUST_HAVE,
                    label="Minimum compliance/AML experience",
                    min_years=3,
                    weight=2,
                ),
                CriterionIn(
                    id="azerbaijani_preferred",
                    kind=CriterionKind.LANGUAGE,
                    type=CriterionType.PREFERRED,
                    label="Azerbaijani language",
                    value="Azerbaijani",
                    weight=1,
                ),
            ],
        },
    ]


@dataclass(frozen=True)
class DemoSeedSummary:
    tenant_id: uuid.UUID
    api_key_prefix: str
    api_key_plaintext: str | None  # a fresh key is always minted and shown; see seed_demo
    already_seeded: bool
    candidates_created: int
    documents_created: int
    profiles_created: int
    identities_created: int
    embeddings_created: int
    jobs_created: int
    evaluations_created: int
    human_username: str
    human_temp_password: str  # always freshly (re)issued; see _bootstrap_demo_human_login


class DemoTenantAmbiguousError(Exception):
    """Raised whenever the demo tenant cannot be positively and
    unambiguously identified. Never caught internally — seed_demo and
    reset_demo must both abort before taking any destructive or
    tenant-creating action when this is raised. See module docstring
    and docs/DECISIONS.md D-022 (reset-safety hardening)."""


async def _find_demo_tenant(db: AsyncSession) -> Tenant | None:
    """Positively identifies the demo tenant. Display name alone is
    NEVER sufficient proof of demo ownership — a tenant is only ever
    treated as "the demo tenant" if it (a) is the sole tenant named
    exactly DEMO_TENANT_NAME, AND (b) carries the DEMO_TENANT_MARKER_EVENT
    audit-trail marker this module itself writes at creation time (see
    seed_demo). That marker lives in the existing tenant-scoped
    audit_events table — no new column, no migration.

    Returns None only when zero tenants are named DEMO_TENANT_NAME (the
    normal first-run case). Raises DemoTenantAmbiguousError — never
    silently guesses, adopts, deletes, or mutates anything — when:
    - more than one tenant is named DEMO_TENANT_NAME (even if one of
      them is legitimately marked: the ambiguity itself is unsafe), or
    - exactly one tenant has that name but does not carry the marker
      (almost certainly an unrelated tenant that merely happens to
      share the display name)."""
    result = await db.execute(select(Tenant).where(Tenant.name == DEMO_TENANT_NAME))
    candidates = result.scalars().all()

    if len(candidates) > 1:
        raise DemoTenantAmbiguousError(
            f"{len(candidates)} tenants are named exactly {DEMO_TENANT_NAME!r}. "
            "Refusing to guess which one is the demo tenant — no data was "
            "read, adopted, created, or deleted. Resolve the name collision "
            "manually (rename or remove the non-demo tenant) before running "
            "seed-demo again."
        )
    if not candidates:
        return None

    tenant = candidates[0]
    marker = await db.execute(
        select(AuditEvent.id)
        .where(
            AuditEvent.tenant_id == tenant.id,
            AuditEvent.event_type == DEMO_TENANT_MARKER_EVENT,
        )
        .limit(1)
    )
    if marker.scalar_one_or_none() is None:
        raise DemoTenantAmbiguousError(
            f"A tenant named {DEMO_TENANT_NAME!r} exists (id={tenant.id}) but "
            "was not created by seed-demo — it carries no bootstrap marker. "
            "Refusing to adopt, reset, or reseed it; nothing was changed. "
            "This is very likely an unrelated tenant that happens to share "
            "the demo display name — rename it if the collision is real."
        )
    return tenant


async def _marked_demo_human(
    db: AsyncSession, tenant_id: uuid.UUID,
) -> tuple[User | None, AuditEvent | None, list[TenantMembership]]:
    """Validate the immutable creation chain before any seed/reset mutation.

    A replacement links its predecessor explicitly; multiple roots, forks,
    duplicate User IDs, malformed/dangling links or a missing current User/
    demo membership are ambiguous. Never adopt a User by its username.
    """
    markers = list((await db.scalars(select(AuditEvent).where(
        AuditEvent.tenant_id == tenant_id, AuditEvent.event_type == DEMO_HUMAN_MARKER_EVENT,
    ))).all())
    if not markers:
        return None, None, []
    by_previous: dict[uuid.UUID | None, AuditEvent] = {}
    user_ids: dict[uuid.UUID, uuid.UUID] = {}
    try:
        for marker in markers:
            metadata = marker.event_metadata
            if set(metadata) not in ({"user_id"}, {"user_id", "previous_marker_id"}):
                raise ValueError()
            user_id = uuid.UUID(metadata["user_id"])
            previous = (
                uuid.UUID(metadata["previous_marker_id"])
                if "previous_marker_id" in metadata else None
            )
            if previous in by_previous or user_id in user_ids.values():
                raise ValueError()
            by_previous[previous] = marker
            user_ids[marker.id] = user_id
        current = by_previous[None]
        visited = {current.id}
        while current.id in by_previous:
            current = by_previous[current.id]
            if current.id in visited:
                raise ValueError()
            visited.add(current.id)
        if len(visited) != len(markers):
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError):
        raise DemoTenantAmbiguousError("Demo human creation markers are ambiguous.") from None

    # Lock the User before checking ALL memberships (including inactive ones).
    # Its FK lock also prevents a concurrent membership insert from racing a
    # successful exclusivity check. Match existing User -> Membership order.
    user = await get_user_by_id(db, user_ids[current.id], for_update=True)
    memberships = list((await db.scalars(select(TenantMembership).where(
        TenantMembership.user_id == user_ids[current.id],
    ).with_for_update().execution_options(populate_existing=True))).all())
    if user is None or not any(m.tenant_id == tenant_id for m in memberships):
        raise DemoTenantAmbiguousError("Marked demo human authority is missing.")
    return user, current, memberships


async def reset_demo(
    db: AsyncSession, storage: DocumentStorage, photo_storage: LocalPhotoStorage
) -> bool:
    """Delete the marked demo tenant and only a marked, exclusive current User.

    Unmarked/shared identities survive; only tenant-owned rows cascade away.
    Ambiguous human markers abort before deletion. No operator-supplied IDs.

    The tenant's original CVs and derived photos are staged (reversible) here
    and become permanent only via storage_recovery.commit_with_recovery; the
    caller owns the transaction and must finish it that way (issue #46 S4).
    """
    tenant = await _find_demo_tenant(db)
    if tenant is None:
        return False
    demo_user, _, memberships = await _marked_demo_human(db, tenant.id)
    await storage_writer(db, tenant.id)
    original_keys = await db.scalars(
        select(CandidateDocument.storage_key).where(CandidateDocument.tenant_id == tenant.id)
    )
    for key in original_keys.all():
        track_staged(
            db, storage, await storage.stage_delete(tenant_id=tenant.id, storage_key=key)
        )
    photo_keys = await db.scalars(
        select(CandidatePhotoVersion.derived_storage_key).where(
            CandidatePhotoVersion.tenant_id == tenant.id,
            CandidatePhotoVersion.derived_storage_key.is_not(None),
        )
    )
    for photo_key in photo_keys.all():
        assert photo_key is not None
        track_staged(
            db, photo_storage,
            await photo_storage.stage_delete(tenant_id=tenant.id, storage_key=photo_key),
        )
    if demo_user is not None and all(m.tenant_id == tenant.id for m in memberships):
        await db.delete(demo_user)
    await db.delete(tenant)
    await db.flush()
    return True


async def _bootstrap_demo_human_login(
    db: AsyncSession, *, tenant_id: uuid.UUID
) -> tuple[str, str]:
    """Issue a fresh credential without adopting legacy or shared identities."""
    existing, marker, memberships = await _marked_demo_human(db, tenant_id)
    temp_password = secrets.token_urlsafe(12)
    if (existing is not None and existing.is_active
            and all(m.tenant_id == tenant_id for m in memberships)
            and all(m.is_active and m.role == ROLE_HR_USER for m in memberships)):
        await set_password(db, user_id=existing.id, plaintext_password=temp_password)
        return existing.username, temp_password

    # Retire stale DEMO-tenant access only. The generic membership setter also
    # bumps User.security_version, so it is deliberately inappropriate here.
    legacy = await get_user_by_username(db, DEMO_USER_USERNAME)
    retired_ids = {u.id for u in (existing, legacy) if u is not None}
    for user_id in retired_ids:
        stale = await db.scalar(select(TenantMembership).where(
            TenantMembership.user_id == user_id, TenantMembership.tenant_id == tenant_id,
        ))
        if stale is not None:
            await db.execute(update(TenantMembership).where(
                TenantMembership.id == stale.id,
            ).values(is_active=False, security_version=uuid.uuid4()))
            await revoke_sessions_for_membership(db, membership_id=stale.id)

    username = DEMO_USER_USERNAME
    # Names are server-derived; collisions never authorize adopting an identity.
    while await get_user_by_username(db, username) is not None:
        username = f"demo.hr.{tenant_id.hex}.{uuid.uuid4().hex}"
    user = await create_user(db, username=username, plaintext_password=temp_password)
    await create_membership(db, user_id=user.id, tenant_id=tenant_id, role=ROLE_HR_USER)
    metadata = {"user_id": str(user.id)}
    if marker is not None:
        metadata["previous_marker_id"] = str(marker.id)
    await record_event(
        db, tenant_id=tenant_id, event_type=DEMO_HUMAN_MARKER_EVENT, metadata=metadata,
    )
    return user.username, temp_password


async def seed_demo(
    db: AsyncSession,
    storage: DocumentStorage,
    parser: DocumentParser,
    *,
    max_bytes: int,
    max_profile_input_chars: int,
    max_identity_input_chars: int,
    max_embedding_input_chars: int,
    evaluation_as_of_date: date,
) -> DemoSeedSummary:
    """Idempotent: if the demo tenant already has seeded candidates, this
    is a safe no-op that rotates the demo login credential — every
    currently-active API key on the (positively-identified) demo tenant is
    revoked and exactly one fresh key is minted and returned, since a
    previous run's plaintext can never be recovered and an operator must
    always come away from `seed-demo` with a usable credential — never a
    duplicate dataset. Every
    candidate/document/profile/identity/embedding/job/criteria/evaluation
    row is created through the same real service functions the rest of
    the application uses; only the LLM/embedding *inputs* are synthetic,
    pre-written, evidence-matched data instead of a live model's output
    (see module docstring) — nothing here is a fake production AI mode.

    Raises DemoTenantAmbiguousError (creates/mutates nothing) if the
    demo tenant cannot be positively and unambiguously identified — see
    _find_demo_tenant. A same-named-but-unmarked tenant is never adopted
    or reseeded, even implicitly."""
    tenant = await _find_demo_tenant(db)
    if tenant is None:
        tenant = await create_tenant(db, name=DEMO_TENANT_NAME)
        # The positive-identification marker itself — written once, at
        # creation, through the existing tenant-scoped audit trail. This
        # is what future _find_demo_tenant calls trust; the display name
        # alone is never sufficient (see docs/DECISIONS.md D-022).
        await record_event(
            db, tenant_id=tenant.id, event_type=DEMO_TENANT_MARKER_EVENT, metadata={}
        )

    # Validate before key rotation, dataset creation or any identity mutation.
    human_username, human_temp_password = await _bootstrap_demo_human_login(db, tenant_id=tenant.id)

    existing_candidate_count = await count_candidates_for_tenant(db, tenant_id=tenant.id)
    # Exactly one active key even for a marked legacy tenant with no dataset.
    await revoke_active_api_keys_for_tenant(db, tenant_id=tenant.id)
    api_key, plaintext = await create_api_key(db, tenant_id=tenant.id, env="test")
    if existing_candidate_count > 0:
        # Rotate: revoke every currently-active key on this positively-
        # identified demo tenant before minting the replacement, so a
        # re-run never leaves an unusable orphaned key behind and never
        # accumulates indefinitely many valid demo credentials. Scoped
        # strictly to tenant.id — never a generic cross-tenant operation.
        await db.flush()
        return DemoSeedSummary(
            tenant_id=tenant.id,
            api_key_prefix=api_key.prefix,
            api_key_plaintext=plaintext,
            already_seeded=True,
            candidates_created=0,
            documents_created=0,
            profiles_created=0,
            identities_created=0,
            embeddings_created=0,
            jobs_created=0,
            evaluations_created=0,
            human_username=human_username,
            human_temp_password=human_temp_password,
        )

    documents_created = profiles_created = identities_created = embeddings_created = 0
    created_candidate_ids: list[uuid.UUID] = []

    for portrait_index, spec in enumerate(_demo_candidates()):
        candidate = await create_candidate(db, tenant_id=tenant.id)
        created_candidate_ids.append(candidate.id)

        document = await ingest_candidate_document(
            db,
            storage,
            parser,
            tenant_id=tenant.id,
            candidate_id=candidate.id,
            filename=spec.doc_filename,
            content_type=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
            data=_build_docx(spec.lines, portrait_index),
            max_bytes=max_bytes,
        )
        documents_created += 1

        llm = _DemoLLMProvider(
            extraction=_profile_extraction(spec), identity_extraction=_identity_extraction(spec)
        )
        view, canonical_id = await load_profile_view(
            db, tenant_id=tenant.id, candidate_id=candidate.id, candidate_document=document,
        )
        outcome = await infer_candidate_profile(llm, view, max_input_chars=max_profile_input_chars)
        profile_version = await persist_profile_outcome(
            db, tenant_id=tenant.id, candidate_id=candidate.id, candidate_document=document,
            canonical_document_id=canonical_id, model_provider_name="demo-synthetic",
            outcome=outcome,
        )
        profiles_created += 1
        view, canonical_id = await load_identity_view(
            db, tenant_id=tenant.id, candidate_id=candidate.id, candidate_document=document,
        )
        identity = await infer_candidate_identity(
            llm, view, max_input_chars=max_identity_input_chars,
        )
        await persist_identity_outcome(
            db, tenant_id=tenant.id, candidate_id=candidate.id, candidate_document=document,
            canonical_document_id=canonical_id, model_provider_name="demo-synthetic",
            outcome=identity,
        )
        identities_created += 1
        if profile_version.status == "COMPLETED":
            embedder = _DemoEmbeddingProvider(vector=spec.embedding_vector)
            compatibility = EmbeddingCompatibility(
                embedder.provider_name, embedder.model_name, embedder.model_revision,
                SERIALIZER_VERSION, len(spec.embedding_vector),
            )
            plan = await prepare_embedding(
                db, embedder, tenant_id=tenant.id, candidate_id=candidate.id,
                max_input_chars=max_embedding_input_chars, compatibility=compatibility,
            )
            if plan.existing is not None:
                await record_embedding_reused(db, tenant_id=tenant.id,
                                              candidate_id=candidate.id, plan=plan)
            else:
                vector = await embedder.embed(plan.text)
                validate_embedding_result(vector, compatibility)
                await persist_embedding(db, tenant_id=tenant.id, candidate_id=candidate.id,
                                        plan=plan, result=vector)
            embeddings_created += 1

    jobs_created = 0
    evaluations_created = 0
    for job_spec in _demo_jobs():
        job = await create_job(db, tenant_id=tenant.id, title=job_spec["title"])
        criteria_version = await create_criteria_version(
            db,
            tenant_id=tenant.id,
            job_id=job.id,
            criteria=[c.model_dump(mode="json") for c in job_spec["criteria"]],
            created_by_api_key_id=api_key.id,
        )
        jobs_created += 1

        for candidate_id in created_candidate_ids:
            authorized = await get_current_authorized_profile(
                db, tenant_id=tenant.id, candidate_id=candidate_id
            )
            if authorized is None:
                continue
            current_profile, _ = authorized
            scored = await evaluate_and_score_candidate(
                db,
                tenant_id=tenant.id,
                candidate_id=candidate_id,
                candidate_profile_version_id=current_profile.id,
                job_id=job.id,
                job_criteria_version_id=criteria_version.id,
                evaluation_as_of_date=evaluation_as_of_date,
                resolved_profile_version=current_profile,
                resolved_criteria_version=criteria_version,
            )
            if scored.evaluation.status == "COMPLETED":
                evaluations_created += 1

    return DemoSeedSummary(
        tenant_id=tenant.id,
        api_key_prefix=api_key.prefix,
        api_key_plaintext=plaintext,
        already_seeded=False,
        candidates_created=len(created_candidate_ids),
        documents_created=documents_created,
        profiles_created=profiles_created,
        identities_created=identities_created,
        embeddings_created=embeddings_created,
        jobs_created=jobs_created,
        evaluations_created=evaluations_created,
        human_username=human_username,
        human_temp_password=human_temp_password,
    )
