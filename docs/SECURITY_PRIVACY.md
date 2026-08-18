# MEYAR — Security & Privacy

## Privacy model

- `CandidateIdentity` (name/contact/raw doc) is stored separately from
  `CandidateProfile` (extracted professional facts). Only `CandidateProfile`
  is readable by the matching engine.
- Sensitive/irrelevant attributes (gender, photo, DOB/age, ethnicity,
  religion, marital status, political opinion, health info) are never
  extracted into `CandidateProfile` and never influence evaluation — the
  extraction schema simply has no fields for them, so the LLM cannot smuggle
  them in without failing Pydantic validation.
- CV content never leaves the local network to a cloud LLM in MVP.

## Threat model (MVP-relevant)

| Threat | Mitigation |
|---|---|
| Cross-tenant data access | Mandatory `tenant_id` scoping in every repository query; no route relies on client-supplied tenant id; isolation tests (see below) |
| API key theft/leak | Only SHA-256 hash persisted; keys never logged; prefix-only in logs/UI; revocation + expiry supported |
| Prompt injection in CV content | CV text is always framed as quoted data in prompts, never as instructions; structured-output schema has no "instruction" field; dedicated fixture + test (see Test Data) |
| Malicious upload (zip bomb, path traversal, MIME spoofing, oversized file) | MIME sniffing + extension cross-check, max size enforcement, opaque storage ids, no user-controlled paths |
| Unvalidated LLM output reaching authoritative tables | All LLM output passes Pydantic v2 schema validation before persistence; validation failure → `MANUAL_REVIEW_REQUIRED`/`FAILED`, never silently coerced |
| Local inference endpoint exposure | Ollama bound to localhost/internal Docker network only, in every environment; never a public route |
| Secret leakage via logs/git | PII-safe structured logging (ids only); `.claude` hooks block obvious secret patterns and real CV files from commits |
| Retry-induced duplicate work/cost | `Idempotency-Key` on `POST /v1/evaluations` |
| Inference overload | Global concurrency semaphore around the `LLMProvider` call; per-tenant rate limit |

## Tenant isolation enforcement

Enforced in the data-access layer, not the UI/route layer: every repository
function signature requires `tenant_id` and every query filters on it.
Postgres Row-Level Security is a documented future hardening step (D-xxx in
DECISIONS.md) — not required for MVP given the repository-layer enforcement,
revisit if the app layer is ever bypassed (e.g. raw SQL tooling, admin
scripts).

## API security

- All non-health routes require `Authorization: Bearer meyar_live_...` (or
  `meyar_test_...` in non-prod).
- Key verification: look up by prefix, compare SHA-256 hash in constant
  time, check `revoked_at`/`expires_at`, update `last_used_at`.
- Scopes enforced per-route via a FastAPI dependency.
- Errors never reveal whether a resource exists in another tenant (404, not
  403-with-details, for cross-tenant reads).

## Document storage

Raw candidate CV bytes are addressed only by an opaque storage key
(`{tenant_id}/{uuid4}`), generated server-side — never derived from the
client-supplied filename, so there is no path-traversal surface. Storage
sits behind a swappable `DocumentStorage` interface
(`meyar.storage.base`); the MVP implementation is local filesystem under a
configurable root (`MEYAR_STORAGE_ROOT`, default `./var/storage`, git-
ignored). **Encryption-at-rest is not implemented at the application
layer in MVP** — local storage relies on host/disk-level protection (e.g.
full-disk encryption). This is a known, explicitly deferred gap: before
handling real production candidate data, swap in an encrypted/object
storage backend behind the same interface (see docs/DECISIONS.md D-007
for the parity reasoning — same swap-friendly boundary as the parser).
`DELETE /v1/candidates/{id}` deletes the stored bytes for every document
before removing DB rows, so a successful delete never leaves an orphaned
file on disk.

## PII-safe logging

Structured logs use ids/enums/durations only — see MASTER_SPEC.md §16.
Enforced by convention + a lightweight log-call lint in `security-review`
skill; CV text/PII must never be passed to the logger.

## Retention / deletion

Policy values (retention days, backup handling) are configurable, not
hardcoded — open business decision, see DECISIONS.md. `DELETE
/v1/candidates/{id}` hard-deletes identity + raw document content
immediately; evaluation audit rows are tombstoned per tenant policy, not
necessarily deleted (needed for audit/compliance defense), but never expose
raw CV content after candidate deletion.

## Test data

Synthetic only, under `fixtures/synthetic_cvs/`, clearly marked synthetic.
Minimum coverage: Azerbaijani, Russian, English, multilingual, missing
skills, conflicting employment dates, scanned/image CV, unusual formatting,
malicious prompt-injection content, malformed file. Never commit real CVs
(enforced by a pre-commit hook pattern check + `.gitignore`).

## Testing priorities (highest value first)

1. Tenant A cannot access tenant B resources (any resource type).
2. API key authentication required on protected routes.
3. Revoked/expired API key rejected.
4. CV upload validation (size/MIME/malformed/traversal).
5. Document parsing produces canonical representation.
6. AI structured output validated; invalid output never persisted raw.
7. Malicious CV prompt-injection content has zero effect on evaluation
   instructions/output.
8. `UNKNOWN` is never silently converted to `NOT_MATCHED`.
9. Deterministic policy engine — unit tested independent of the LLM.
10. Evaluation job lifecycle transitions.
11. Idempotent evaluation submission (no duplicate jobs).
12. No PII in normal application logs.

## Unresolved / open questions

- Final legal retention periods per jurisdiction — deferred to owner
  (business/legal decision), tracked as an open item in DECISIONS.md.
- Whether Postgres RLS is added before first external pilot customer —
  deferred until real multi-tenant load is observed.
