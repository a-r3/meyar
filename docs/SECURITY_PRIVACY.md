# MEYAR — Security & Privacy

## Privacy model

- `CandidateIdentityVersion` (name/contact) is implemented as a model separate
  from `CandidateProfile` (extracted professional facts) — see
  `docs/PROJECT_VISION.md`, MASTER_SPEC.md §5, and D-014. It may be displayed to
  authorized HR users in the UI/API but **must never be read by the
  matching, evaluation, search, or ranking engine** — those only ever
  read `CandidateProfile`. Identity data is presentation-only, never a
  scoring/ranking feature.
- Sensitive/irrelevant attributes (gender, photo, DOB/age, ethnicity,
  religion, marital status, political opinion, health info) are never
  extracted into `CandidateProfile` and never influence evaluation — the
  extraction schema simply has no fields for them, so the LLM cannot smuggle
  them in without failing Pydantic validation.
- CV content never leaves the bank's internal network to a cloud LLM.
  This applies equally to the local embedding model once it exists
  (Slice 7): local-only, never an external embedding API.
- Files discovered by the local folder scanner/indexer (Slice 6) are
  untrusted input exactly like a direct upload — same MIME sniffing, size
  cap, opaque storage id, no filename-derived paths. A local file is not
  implicitly more trusted than an uploaded one.
- Any UI/API surface that exposes original CV bytes or `CandidateIdentity`
  fields requires the same authenticated/authorized access control as the
  rest of the API — there is no anonymous or public read path anywhere in
  MEYAR (it is internal HR tooling, not a public product).

## AI extraction (Slice 4)

- `OllamaLLMProvider` refuses to construct against a non-loopback
  `MEYAR_OLLAMA_BASE_URL` — candidate document content cannot leave the
  machine via configuration mistake.
- The model is shown a `ProfessionalDocumentView`, never the raw
  `CanonicalDocument`: emails, phone-number-shaped strings, and lines
  explicitly labeled DOB/gender/sex/marital-status/religion are
  deterministically redacted first (`meyar.extraction.redaction`).
  Employment dates are preserved — the phone redaction is digit-count
  gated (9+ digits) specifically so a "2021-2025" range survives.
  **Known limitation:** candidate names are not redacted (reliable name
  detection is its own NLP problem, out of MVP scope) — the extraction
  schema simply has no `name` field, and the system prompt instructs the
  model to ignore identity, so a name cannot enter `CandidateProfile`
  even though it's visible to the model during inference. Tracked as a
  future hardening item, not a blocker.
- Every extracted fact's evidence (page, block_index, quote) is
  re-verified against a freshly rebuilt `ProfessionalDocumentView` for
  the *exact* `CanonicalDocument` referenced — never trusted from model
  output. A reference to a nonexistent page/block, a fabricated quote, or
  (structurally impossible by construction) another document's content
  fails validation and the extraction is persisted as `FAILED`, never as
  a silently-accepted success.
- The extraction schema uses `extra="forbid"` and has no field for name/
  email/phone/age/gender/religion/ethnicity/marital status/health/
  photo/nationality — the model cannot smuggle a sensitive attribute into
  `CandidateProfile` without failing Pydantic validation.
- The system prompt explicitly frames document content as untrusted data
  and instructs the model not to follow, evaluate, or act on anything
  inside it — tested against the existing prompt-injection fixture.
- One bounded retry on schema-invalid structured output (never unbounded);
  `MODEL_UNAVAILABLE`/`MODEL_TIMEOUT`/`MODEL_SCHEMA_INVALID`/
  `EVIDENCE_INVALID`/`INPUT_TOO_LARGE` all fail safely to a stored
  `FAILED`/`MANUAL_REVIEW_REQUIRED` `CandidateProfileVersion` — never a
  crash, never fabricated content.

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
| Retry-induced duplicate work/cost | `Idempotency-Key` on unsafe writes where relevant |
| Inference overload | Global concurrency semaphore around the `LLMProvider` call; per-tenant rate limit |
| Candidate content leaving bank infrastructure via embeddings | Local-only embedding provider abstraction, same boundary pattern as `LLMProvider`; no external embedding API call anywhere in code |
| Candidate content leaving the host machine via any outbound network call | Formally verified (Slice 13): static inventory confirms only two `httpx.AsyncClient` construction sites exist in the app, both loopback-gated; a deterministic runtime guard (`test_no_exfiltration.py`) proves a representative extract+embed workflow, run through the real provider classes, never attempts a non-loopback request. Validated as an application-level, tested-configuration claim — not a physical-firewall/network-layer guarantee. |
| Identity data (name/contact) leaking into scoring/ranking as a hidden signal | `CandidateIdentityVersion` is presentation-only by construction — matching/search/ranking inputs include only `CandidateProfile` fields, and Slice 11 resolves identity after backend order is fixed |
| Untrusted local files treated as more trustworthy than uploads | Folder-discovered files go through the identical MIME/size/opaque-id validation path as direct upload — no separate, weaker code path |

## Tenant isolation enforcement

Enforced in the data-access layer, not the UI/route layer: every repository
function signature requires `tenant_id` and every query filters on it.
Postgres Row-Level Security is a documented future hardening step — not
required for MVP given the repository-layer enforcement,
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
- Browser UI login posts the API key once to `/ui/login`, exchanges it for a
  256-bit opaque cookie token, and persists only the token's SHA-256 digest in
  `BrowserSession`. Every browser request reloads the live API-key row and
  derives tenant/scopes from it. Sessions expire after eight hours, support
  revocation/logout, and all authenticated POSTs require per-session CSRF.
  Cookies are `HttpOnly`, `SameSite=Lax`, `Path=/ui`, and Secure by default;
  loopback development must opt out explicitly. UI responses are no-store and
  carry restrictive same-origin browser headers (D-018).

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

Policy values (retention days, backup frequency/schedule) are configurable,
not hardcoded — open business decision, see DECISIONS.md. The backup/restore
*mechanism* itself (PostgreSQL-native `pg_dump`/`pg_restore` + document-
storage archive, both required together) is documented and has an executed
synthetic acceptance proof — see `docs/BACKUP_RESTORE.md`. `DELETE
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
- Whether Postgres RLS is added before broader internal rollout —
  deferred until real production load is observed. (No external pilot
  customer exists — MEYAR is internal-only, D-011.)
