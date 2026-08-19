# MEYAR — Status

## Current phase
Slice 4 (Local AI Candidate Profile Extraction) implemented and verified.
Not yet committed.

## Completed
- Preflight, fast docs pass, Claude Code harness.
- Backend scaffold: FastAPI + SQLAlchemy 2.0 async + Alembic + PostgreSQL.
- Slice 1 (Tenant + API Auth) — committed as `f5b4ec3`.
- Slice 2 (Job Criteria) — committed as `9ef3273`.
- Slice 3 (Candidate Upload) — committed as `56aca03`.
- Slice 4 (Profile Extraction): `LLMProvider` protocol + `OllamaLLMProvider`
  (loopback-only, rejects non-local base URLs at construction), strict
  `CandidateProfileExtraction` Pydantic schema (skills/employment/
  education/certifications/languages/projects, `extra="forbid"`, every
  item requires evidence), `ProfessionalDocumentView` (pre-LLM redaction
  of email/phone/labeled DOB-gender-marital-religion, employment dates
  preserved), versioned system prompt
  (`candidate-profile-extraction-v1`), deterministic evidence verifier
  (re-checks every page/block/quote against the real CanonicalDocument,
  never trusts model-supplied references), immutable
  `CandidateProfileVersion` (mirrors JobCriteriaVersion — re-extraction
  creates a new version, never mutates). One bounded retry on
  schema-invalid output. `FakeLLMProvider` for deterministic tests. No
  public API endpoint added (internal service + CLI `meyar
  extract-profile`, per "internal service first" — avoids freezing a
  contract Slice 5's evaluation queue will likely replace).

## Tests
65/65 passing (38 prior + 27 new: valid extraction, PII-field exclusion,
nonexistent evidence page/block rejection, fabricated quote rejection,
bounded-retry success/exhaustion, provider unavailable/timeout, oversized
input, missing-canonical-document precondition, re-extraction versioning
(v1 unchanged, v2 correct), loopback-only enforcement, prompt-injection
fixture passed through as inert data, PII-not-in-logs regression,
redaction unit tests (email/phone redacted, employment dates and
"2019-Present" ranges preserved, labeled DOB/gender/marital/religion
lines redacted), cross-tenant profile isolation). `ruff` and `mypy`
clean.

## Live local inference
**PASS** with `qwen3:0.6b` (see D-009 — Ollama 0.16.2 is too old for
Qwen3.5; this is the dev-integration substitute, not a production
choice). Full pipeline verified end-to-end for real: synthetic
`valid_cv.pdf` → parsed CanonicalDocument → redacted
ProfessionalDocumentView → real local Ollama call → valid structured
JSON → Pydantic validation passed → every evidence reference verified
against the real document → immutable `CandidateProfileVersion` v1,
status COMPLETED. A known fact ("Python") was extracted with a
verified-real evidence quote. ~38s end-to-end. Model quality caveat
(expected at 0.6B): category assignment was confused (some items landed
in `skills` with category-like names such as "employment_history") —
every individual evidence reference was still independently verified as
a real substring of the source document, proving the verifier works
against genuine (not just fake-provider) model output.

One escalation attempt to `qwen3:1.7b` was made (per the "try once"
guidance) using the same candidate/document: it hit `MODEL_TIMEOUT` at
60s under this host's severe memory pressure (194MB-360MB free RAM,
6+GB swap in use throughout this session). This is a host-resource
limit, not an architecture defect — it also incidentally verified the
`MODEL_TIMEOUT` failure path against a real timeout (safely created a
`FAILED` v2, no crash). Escalation stopped here per policy; `qwen3:0.6b`
reverted as the working dev default.

**INTEGRATION_VERIFIED — not MODEL_QUALITY_APPROVED.** Final production
model selection remains a target-Mac benchmark (D-001).

## In progress
Nothing in flight.

## Blockers
None blocking. Same open items: real Apple Silicon Mac not yet available
(D-001); Auto Mode script still dry-run only; document encryption-at-rest
still deferred to host/disk protection (Slice 3). New: Ollama binary on
this dev machine cannot be upgraded without root access this session
does not have (D-009) — blocks Qwen3.5 on this specific host only, not
an application constraint.

## Next slice
Slice 5 — Evaluation Engine (CandidateProfileVersion + JobCriteriaVersion
→ criterion-by-criterion evidence evaluation → deterministic policy
calculation → immutable Evaluation result). Slice 4 work is
implemented/tested but intentionally left uncommitted per current
instructions.
