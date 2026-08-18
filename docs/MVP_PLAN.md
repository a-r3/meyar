# MEYAR — MVP Plan

## Slices

| # | Slice | Proves | Depends on |
|---|-------|--------|------------|
| 0 | Bootstrap | preflight, docs, harness, repo scaffold | - |
| 1 | Tenant + API Auth | API key → authenticate → tenant context → protected endpoint | 0 |
| 2 | Job Criteria | tenant → create job → versioned criteria | 1 |
| 3 | Candidate Upload | tenant → upload CV → secure storage → parsed canonical doc | 1 |
| 4 | Local AI | canonical CV → local model → validated structured profile + evidence | 3 |
| 5 | Evaluation | candidate + criteria → queued eval → AI criterion analysis → policy engine → stored result | 2, 4 |
| 6 | External API Flow | full external client flow, end to end | 1-5 |
| 7 | Security Gate | tenant isolation, PII-safe logs, prompt-injection resistance, key protections, upload protections, idempotency, audit evidence | 1-6 |

## Acceptance criteria per slice

- **1**: cross-tenant access returns 404/403 (never leaks existence);
  revoked/expired keys rejected; unauthenticated request to a protected
  route rejected; API key secret never persisted or logged in plaintext.
- **2**: criteria versions immutable once created; job always resolves to
  exactly one current criteria version.
- **3**: rejects oversized/malformed/MIME-mismatched files; stores under
  opaque id; original filename never used as a path.
- **4**: LLM output that fails schema validation never reaches
  `CandidateProfile`; sensitive attributes never extracted into the profile.
- **5**: evaluation lifecycle transitions correctly (QUEUED→PROCESSING→
  COMPLETED/NEEDS_REVIEW/FAILED); policy engine has LLM-independent unit
  tests; duplicate `Idempotency-Key` does not create a second job.
- **6**: a scripted external client can complete the full flow against a
  running local stack.
- **7**: full test matrix in SECURITY_PRIVACY.md passes.

## Deferred (explicitly out of MVP scope)

Billing/payments, complex plans, ATS connectors, email/Drive ingestion,
cloud LLM, autonomous rejection, interview bot, RAG, vector DB,
fine-tuning, mobile app, Kubernetes, microservices, advanced dashboards,
complex RBAC, enterprise SSO, webhook delivery (interface left open, not
implemented).

## Current status

See `STATUS.md`.
