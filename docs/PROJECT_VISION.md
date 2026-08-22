# MEYAR — Project Vision

**MEYAR — Internal AI Candidate Intelligence & CV Search Platform.**

This document is the canonical product-vision reference. Requirement
authority (highest to lowest): the official task **"CV Screening API —
Layihə Task Bölgüsü"** (`AI-PROJ-CV-01`, v1.0, 18.08.2026) → owner
clarifications recorded in `docs/DECISIONS.md` D-011 → this document →
`docs/MASTER_SPEC.md` (technical detail) → `docs/MVP_PLAN.md` (slice
sequencing).

## Vision

Rabitabank OJSC HR needs to stop manually reading CVs. MEYAR ingests the
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

Authorized internal Rabitabank HR/recruiting staff, and approved internal
systems that call the REST API on their behalf. No candidate-facing or
public-facing surface exists or is planned.

## Product surfaces

1. **Internal chat/search UI** — natural-language requests ("Show me 10
   candidates with at least 5 years of banking experience who know
   Python, SQL, Russian and English") return the requested number of
   matching candidates (when enough exist) with evidence/reasons per
   candidate, not just an ID list.
2. **CV Library** — browse/search all indexed candidates; open a
   candidate profile and the original CV, authorized-access only.
3. **Internal REST API** — documented (OpenAPI/Swagger), authenticated,
   powers the two UI surfaces above and any other approved internal
   system. Not a commercial product API.

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
`CandidateIdentity` is documented here as the target shape; it is **not
implemented yet** (belongs to Slice 7).

## Local CV folder ingestion

MEYAR scans a configured local folder and builds/maintains its own
persistent indexed candidate database. Processing is incremental and
idempotent: file → content hash → new/changed/already-indexed detection
→ parse → extract → persist/index. Unchanged files are never
reprocessed. Not implemented yet — this is Slice 6, the next
implementation slice.

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
embedding API. Not implemented yet (Slices 7–9).

## JD matching, 0–100 score, batch ranking

The existing deterministic evaluation engine (`meyar.evaluation`, Slice
5, D-010) is the foundation. It already computes per-criterion status and
a fit band (`STRONG_MATCH`/`POTENTIAL_MATCH`/`INSUFFICIENT_EVIDENCE`/
`MANUAL_REVIEW_REQUIRED`) with evidence, fully deterministic, no LLM
invention. The official task requires an additional **0–100 numeric
score + explanation** on top of this — the previous "numeric score
deferred" decision (D-010 point 4) is superseded by D-011. The exact
scoring formula is a near-term implementation decision, not finalized
here. Batch ranking (one JD → score every candidate → sorted list with
reasons) reuses the same engine per-candidate. Not implemented yet
(Slice 10).

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
mapping to Rabitabank's organizational boundaries is an open
implementation decision, not a commercial multi-tenant SaaS model. See
`docs/SECURITY_PRIVACY.md` for full detail.

## Delivery process

`main` ← Pull Request ← task branch (`feat/*`, `fix/*`, `chore/*`,
`docs/*`). Quality gate before merge: `ruff`, `mypy`, `pytest` (frontend
checks once a frontend exists). Repository hosting/remote is bank-owned
infrastructure, configured separately from this documentation pass.

## MVP definition

See the official Definition-of-Done acceptance matrix in `docs/STATUS.md`
and the slice sequencing in `docs/MVP_PLAN.md`. In short: CVs are
searchable (structured + semantic), scoreable against a JD (0–100 +
explanation), rankable in batch, all through a chat UI, a CV Library UI,
and a documented internal API — entirely on local infrastructure.

## Roadmap

R0 (this documentation re-baseline) → Git infrastructure → Slice 6 (Local
CV Library & Folder Indexer) → Slice 7 (CandidateIdentity + local
embeddings/pgvector) → Slice 8 (Hybrid search) → Slice 9 (NL search
planner) → Slice 10 (JD 0–100 scoring + batch ranking) → Slice 11 (Chat
UI + CV Library UI) → Slice 12 (API/Swagger/README completion) → Slice 13
(Security + official DoD acceptance). Full detail in `docs/MVP_PLAN.md`.
