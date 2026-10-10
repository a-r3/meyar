# MEYAR — Project Vision

**Current delivery authority (D-115):** the bounded MEYAR AI/JD workflow and #49
foundations are delivered. Normalization → #45 shared application/HTTP contracts
→ independent acceptance/owner merge/post-merge verification → comprehensive audit,
required remediation and full re-audit → #35 deployment completion → #36 actual target
benchmark/model decision → #20 final acceptance. Generic #34 expansion, #50 Q&A/
comparison and conditional #37 integrations are deferred; optional work is not an
initial-deployment gate. See [ROADMAP_NORMALIZATION.md](ROADMAP_NORMALIZATION.md).

**MEYAR — Internal AI Candidate Intelligence & CV Search Platform.**

This document is the canonical product-vision reference. Requirement
authority (highest to lowest): the official task **"CV Screening API —
Layihə Task Bölgüsü"** (`AI-PROJ-CV-01`, v1.0, 18.08.2026) → owner
clarifications recorded in `docs/DECISIONS.md` D-011 → this document →
`docs/MASTER_SPEC.md` (technical detail) → `docs/MVP_PLAN.md` (slice
sequencing).

## Vision

Bank HR needs to stop manually reading CVs. MEYAR ingests the
bank's existing CV archive plus new candidate documents, extracts
structured professional information locally (no candidate data ever
leaves bank infrastructure), and gives HR staff a single place to search,
browse, and score candidates against real job requirements — via a
chat-style natural-language interface, a browsable CV Library, and a
documented internal REST API.

MEYAR is **internal HR tooling**, not an external/commercial B2B SaaS
product. There are no external customers, no billing, no public API
surface. The previous "privacy-first B2B API for external customers"
framing is superseded (see D-011).

## Target users

Authorized internal bank HR/recruiting staff, and approved internal
systems that call the REST API on their behalf. No candidate-facing or
public-facing surface exists or is planned.

## Product surfaces

1. **Internal chat/search UI** — natural-language requests ("Show me 10
   candidates with at least 5 years of banking experience who know
   Python, SQL, Russian and English") return the requested number of
   matching candidates (when enough exist) with evidence/reasons per
   candidate, not just an ID list. **Delivered direction:** MEYAR AI is
   the primary bounded local-AI workflow foundation, including server-owned
   ResultSet follow-up/refinement and JD review/confirmation — see
   "Bounded local-AI HR agent direction" below.
2. **CV Library** — browse/search all indexed candidates; open a
   candidate profile and the original CV, authorized-access only. Remains
   a first-class surface alongside the delivered MEYAR AI workspace.
3. **Internal REST API** — documented (OpenAPI/Swagger), authenticated,
   exposes existing deterministic services to approved internal systems.
   Current Jinja adapters also orchestrate workflows; #45 extracts shared application
   behavior and documents client-neutral contracts without duplicating policy.
   Not a commercial product API.

## Core capability pipeline

```
Local CV folder (existing bank archive)
  → folder scanner / incremental indexer (hash-based change detection)
  → local document parsing (PDF/DOCX, OCR fallback for scanned pages)
  → local LLM structured extraction (schema-validated, evidence-backed)
  → CandidateIdentity + CandidateProfile
  → local embedding model → vector → pgvector
  → structured filters + semantic retrieval → hybrid search
  → natural-language query → validated SearchPlan → search execution
  → JD matching → deterministic policy engine → 0–100 score + fit band + evidence
  → batch ranking across many candidates for one JD
```

## CandidateIdentity vs CandidateProfile

Preserves the existing privacy boundary while meeting the official
extraction requirement (name/contact/education/experience/skills/
languages/certifications):

- **`CandidateIdentity`** — `full_name`, `email`, `phone`. Presentation
  only; may be joined in for authorized UI/API result display.
- **`CandidateProfile`** — `employment_history`, `education`, `skills`,
  `languages`, `certifications`, `projects`, `professional achievements`.
  The only thing search/matching/ranking ever reads.

Sensitive/irrelevant attributes (gender, photo, DOB, ethnicity, religion,
marital status, political opinion, health) never enter either model and
never influence matching — unchanged from the original privacy model.
`CandidateIdentityVersion` is implemented as a separate immutable,
tenant-scoped presentation model (Slice 7); Slice 11 renders its current
version only after search/ranking authority has returned.

## Local CV folder ingestion

MEYAR scans a configured local folder and builds/maintains its own
persistent indexed candidate database. Processing is incremental and
idempotent: file → content hash → new/changed/already-indexed detection
→ parse → extract → persist/index. Unchanged files are never
reprocessed. This is implemented in Slice 6; D-013 fixes the exact
changed/missing/retry semantics.

## Structured + semantic hybrid search

```
Natural-language user request
  → LLM interprets intent into a strict, schema-validated SearchPlan
  → structured filters (skills, min experience, language, certification,
    education, industry, must/preferred) + semantic vector retrieval
  → deterministic/rule-based relevance combination
  → ranked result set with per-candidate evidence/explanation
```

The LLM interprets the query into `SearchPlan` JSON; it never searches or
ranks the database directly. Local embeddings only — no external
embedding API. This is implemented in Slices 7–9 and rendered by Slice 11.

## JD matching, 0–100 score, batch ranking

The existing deterministic evaluation engine (`meyar.evaluation`, Slice
5, D-010) is the foundation. It already computes per-criterion status and
a fit band (`STRONG_MATCH`/`POTENTIAL_MATCH`/`INSUFFICIENT_EVIDENCE`/
`MANUAL_REVIEW_REQUIRED`) with evidence, fully deterministic, no LLM
invention. Slice 10 adds the required **0–100 numeric score + explanation**
on top of this — the previous "numeric score deferred" decision (D-010 point
4) is superseded by D-011. `meyar-score-v1` uses exact Decimal weighted
factors, explicit as-of provenance, and fit-tier-first batch ranking over one
current profile per active candidate. See D-017. Service and CLI are
implemented; UI and machine REST scoring/ranking are delivered (Slices 11–12).
Remaining workflow/application contract gaps belong to #45.

## Local-only AI

Every model call — extraction LLM and embedding model alike — goes
through a local-only provider abstraction (`meyar.llm.LLMProvider` today;
an equivalent boundary for embeddings). Ollama (or any local model
runtime) is never publicly exposed. No candidate content is ever sent to
an external/cloud AI service. This is unchanged and non-negotiable.

## Security & privacy

Authentication, authorization, and auditing remain mandatory — MEYAR
handles real candidate PII for an internal HR system, not anonymous
public traffic. The existing tenant/organization isolation mechanism may
remain in the codebase as a resource-isolation abstraction; its final
mapping to the bank's organizational boundaries is an open
implementation decision, not a commercial multi-tenant SaaS model. See
`docs/SECURITY_PRIVACY.md` for full detail.

## Delivery process

`main` ← Pull Request ← task branch (`feat/*`, `fix/*`, `chore/*`,
`docs/*`, `test/*`). Quality gate before merge: `ruff`, `mypy src`,
`pytest` (frontend checks once a frontend exists). Development remote:
`https://github.com/a-r3/meyar.git` (private, personal — temporary until
the bank supplies an official repository; full history preserved on
migration). See D-012, `docs/DECISIONS.md`.

## MVP definition

See the official Definition-of-Done acceptance matrix in `docs/STATUS.md`
and the slice sequencing in `docs/MVP_PLAN.md`. In short: CVs are
searchable (structured + semantic), scoreable against a JD (0–100 +
explanation), rankable in batch, all through a chat UI, a CV Library UI,
and a documented internal API — entirely on local infrastructure.

## Roadmap

Historical official-task sequence (current sequence is D-115 above):
R0 (documentation re-baseline) → Git infrastructure → Slice 6 (Local
CV Library & Folder Indexer) → Slice 7 (CandidateIdentity + local
embeddings/pgvector) → Slice 8 (Hybrid search) → Slice 9 (NL search
planner) → Slice 10 (JD 0–100 scoring + batch ranking) → Slice 11 (Chat
UI + CV Library UI) → Slice 12 (API/Swagger/README completion) → Slice 13
(Security + official DoD acceptance). Full detail in `docs/MVP_PLAN.md`.

## Bounded local-AI HR agent direction

D-030/D-031/D-032 established this direction as future work at the time of those
historical decisions. Subsequent accepted implementation delivered **MEYAR AI**
as the primary bounded local-AI HR workflow foundation, alongside Candidate
Library / Candidate Detail. The permanent principles apply to current agent
capabilities and any reviewed future expansion:

> AI understands. Database remembers. Search retrieves. Deterministic
> policy evaluates. Evidence explains. Humans decide.

The LLM/agent must never decide the final numeric score, decide a hiring
outcome, silently weaken a requirement, fabricate an unsupported fact, use
identity/PII secretly in ranking, or bypass tenant/auth/tool schemas.
Candidate-content AI remains local-only via Ollama; no external AI API may
ever receive candidate content, in the agent's tool-calling loop or
anywhere else.

### Delivered workflow foundation

MEYAR AI uses typed capability registration, bounded server-owned plans and
structured dialogue/clarification state over existing domain services. #49
server-owned ResultSet authority and conversational follow-up/refinement are
delivered. JD drafting, review/conflict resolution and amendment lead to explicit
human confirmation, then deterministic evaluation/ranking.

Human authority is the implemented live User + TenantMembership + BrowserSession
and active Tenant boundary. Job creation is HUMAN_ACTION_ONLY with CSRF and exact
server-held proposal authority; AgentDraftConfirmation preserves durable identity
and replay/idempotency. Machine API keys do not confer human confirmation authority.

`SearchPlan` and the deterministic policy/validation machinery from Slice 9 remain
internal typed tool/policy boundaries. Agent arguments retain schema validation,
prohibited-attribute policy, no-silent-weakening, tenant authorization and accepted
evidence/provenance requirements. The existing Job/criteria and deterministic
evaluation engines remain scoring/ranking authority. The deterministic language
fast-path remains frozen under D-031; its existing sunset condition is tied to
accepted functional parity, not an automatic rewrite.

### Remaining contracts and future expansion

#45 completes client-neutral shared application/API contracts for these accepted
workflows; it does not rebuild them. Human HTTP/JSON session/authentication design
and shared successful original-CV access auditing remain explicit #45 residuals.

#50 dedicated evidence-backed arbitrary candidate Q&A and deterministic
multi-candidate comparison remain unimplemented, deferred post-presentation work.
They are distinct from delivered #49 follow-ups and consume accepted #45 contracts
where relevant. No #50 capability is claimed here.

Future external or consequential capabilities require actual requirements and new
reviewed scope. Generic #34 expansion is deferred and #37 integrations conditional;
neither creates an initial-deployment requirement. Any new consequential capability
must retain accountable human confirmation, deterministic authority and privacy rules.

Historical rationale and supersession: `docs/DECISIONS.md` D-030/D-031/D-032.
Current phase sequence, bounded milestone ownership and remaining scope: D-115,
[ROADMAP_NORMALIZATION.md](ROADMAP_NORMALIZATION.md) and `docs/MVP_PLAN.md`.
