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
  output. Claim-specific validation then requires one attributable quote
  to contain the material values of that fact: accepted skill alias (and
  no supported explicit contradiction), language plus claimed
  proficiency, certification identity and other populated fields,
  education's populated institution/degree/field/date, or employment's
  populated role/employer/date/current relationship. Project descriptions
  are likewise attributable to their own quote. A reference to a
  nonexistent page/block, a fabricated or unrelated quote, or an
  unsupported material value fails validation and the extraction is
  persisted as `FAILED`, never as a silently-accepted success. Existing
  deterministic interval-grounding and duration rules are separate and
  unchanged.
- Claim-specific support is deliberately deterministic lexical
  attribution, not general natural-language entailment. Matching is
  case/whitespace/Azerbaijani-diacritic normalized; skills additionally
  use the same curated aliases as deterministic evaluation. Positive
  every material professional term fails closed for enumerated obvious
  English `no`, `without`, and nearby `not` constructions, including
  `does/do/did/has/have/had not` with the bounded supported verb list.
  This applies to skill, language/proficiency, certification, education,
  employment, project, domain, skill-experience and domain-experience
  claims. Skill/domain intervals still require subject plus interval in
  one accepted quote; when an experience item references an employment
  row, that same quote must also support the referenced role/employer.
  Paraphrases, implicit claims, complex negation scope, distant terms,
  and unenumerated multilingual negation are not inferred. This is not a
  natural-language entailment guarantee. Where literal support cannot be
  established, the whole extraction remains unverified rather than
  becoming positive evidence or a deterministic `NOT_MATCHED` fact.
- Identity extraction has the same location/verbatim check plus
  value-specific attribution before HR presentation: normalized email
  value, normalized phone digits, or all material normalized name tokens
  must occur in one of that field's own accepted quotes. Identity remains
  presentation-only and is not introduced into matching, scoring,
  ranking, embeddings, or suitability logic.
- Current consumers re-run these validators against the immutable
  canonical document before treating a persisted `COMPLETED` professional
  or identity version as authority. Unsupported legacy facts are
  unavailable to search, evaluation, agent profile/evidence flows, and HR
  profile presentation; cached positive evaluations tied to such a
  profile are not reused and are presented without score/fit as
  unavailable. Ordinary reads never rewrite historical rows.
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

D-050 strengthens attribution: contradiction checks use a bounded canonical
context (200 normalized characters around each attributed quote occurrence),
with local token/clause scope. Current state must be explicit and positive;
linked periods must fit the referenced employment occurrence. Domain intervals
cannot borrow contradictory evidence. Email tokens and coherent phone
occurrences replace substring/digit-concatenation attribution. Embedding
generation/reuse and folder/demo readiness also enforce shared current
authority. Historical rows remain immutable. This is bounded lexical
validation, not general entailment or multilingual NLI.

D-052 completes the bounded English coordination rule: an explicit negative
governor remains active across `and`/`or`/`nor` members until a sentence,
semicolon, independent newline, `but`, or `however` boundary. Local `not` does
not spread into a later positive member, and `not only ... but also ...` remains
positive. Phone authority additionally rejects two-fragment numeric strings
without a phone-like prefix/shape, even when the supplied quote is cropped from
canonical reference/code context.

D-053 removes bare digits as self-authenticating phone evidence. Canonical
source context must provide conventional phone syntax or a directly adjacent
bounded phone/contact label, while explicit reference, invoice, employee-ID,
account, ID, and code labels reject. Cropped quotes cannot hide that canonical
meaning. Unlabeled uninterrupted digits fail closed, which may suppress a valid
unlabeled number but never guesses that an arbitrary identifier is a phone.

## Agent candidate-factual authority

- `AgentDecision` has no free-text answer field. `FINAL_ANSWER` and
  `CLARIFY` carry only a closed non-factual response code which the server
  maps to fixed copy. Candidate facts can reach HR only through typed,
  tenant-scoped tool results or a server-built rendering of validated
  `GroundedFact` values; the model may only select those fact ids.
- Numeric candidate evaluation remains exclusively the deterministic
  evaluation service's output. The agent does not calculate or author a
  score. Hiring recommendations are not an agent output: the server-owned
  response explicitly reserves the decision for an authorized human.
- Persisted assistant text is replayed only with both `SERVER_VALIDATED`
  and current `text_authority_version=candidate-factuality-v2`. Legacy rows,
  including older server-marked rows lacking that version, are rendered
  from the fixed outcome mapping, preventing historical unrestricted model
  prose from re-entering the UI.
- `JDCriteriaDraft.title` remains untrusted draft content. It is retained
  only in the editable JD review payload when its material tokens are
  attributable to the HR-supplied JD text; otherwise a generic draft
  title is used. The assistant headline is always fixed server copy and
  never includes that title. Likewise, `evidence_topic` is only a selector:
  the visible topic is resolved from a validated profile fact title, or
  omitted in favor of generic server copy; raw model topic text is never
  rendered or persisted as assistant authority.

## Local-only Ollama operating contract

Two distinct guarantees are in play, and they must not be conflated —
closing the transport-egress defect above (D-047) only closes the first:

**APPLICATION GUARANTEE (verified in this repository's test suite):**
- Every MEYAR-constructed HTTP client used for Ollama inference,
  embeddings, or health/readiness rejects a non-loopback
  `MEYAR_OLLAMA_BASE_URL` at construction time (`require_loopback_url`).
- Every such client is built with `trust_env=False`, so process
  environment proxy configuration (`HTTP_PROXY`/`HTTPS_PROXY`/
  `ALL_PROXY`) can never redirect a candidate-content request off-machine,
  and this does not depend on `NO_PROXY` being set correctly.
- Every such client has `follow_redirects=False`, so a redirect response
  from the local Ollama daemon cannot carry a request outside the
  approved boundary.

**HOST/OLLAMA CONFIGURATION GUARANTEE (deployment-environment
responsibility — a future deployment preflight must verify these before
go-live, not this codebase):**
- The Ollama daemon itself binds only to an approved local interface,
  preferably loopback (`OLLAMA_HOST=127.0.0.1`, not `0.0.0.0`).
- Cloud-backed Ollama behavior (any "Ollama Cloud"/hosted-model routing
  the daemon supports) is disabled.
- Only approved local models are available to the daemon — no
  unapproved/unreviewed model can be pulled or invoked.
- Model identity/digest is release-managed (pinned, reviewed model
  versions — not "whatever `latest` resolves to on the day of a pull").
- Host-level outbound-network denial (OS firewall egress rule blocking
  the Ollama process, or the whole host, from reaching the public
  internet) remains defense in depth underneath the application-layer
  guarantee above — the application guarantee must not be treated as a
  substitute for it.

**NOT VERIFIED in this development environment:** daemon-level
cloud-disable status, interface binding, model pinning/digest management,
and host-level egress denial are all deployment/target-hardware concerns
(see `docs/TARGET_MAC_BENCHMARK.md`) that cannot be checked from this
repository's test suite — they require a deployment preflight against the
actual target Ollama installation, not yet implemented.

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
| Candidate content leaving the host machine via any outbound network call | Formally verified (Slice 13): static inventory confirms three call sites build a local-only HTTP client in the app (`OllamaLLMProvider.health`, `OllamaLLMProvider._chat`, `OllamaEmbeddingProvider.embed`), all loopback-gated; a deterministic runtime guard (`test_no_exfiltration.py`) proves a representative extract+embed workflow, run through the real provider classes, never attempts a non-loopback request. Validated as an application-level, tested-configuration claim — not a physical-firewall/network-layer guarantee. |
| Candidate content leaving the host machine via process-environment proxy configuration (`HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`), even when the logical request URL is loopback | D-047: all three client-construction sites above route through the single shared `meyar.llm.loopback.build_local_only_async_client` boundary, which passes `trust_env=False` — httpx's own environment-derived proxy selection (`_get_proxy_map`) is disabled outright, so the fix does not depend on `NO_PROXY` being set correctly. `follow_redirects` stays explicit `False` so a redirect response cannot carry a request outside the boundary either. Proven at the transport-configuration level (internal `_mounts`/`_trust_env` state, not just `request.url.host`) in `test_ollama_transport_proxy_isolation.py`. See "Local-only Ollama operating contract" below for the host/daemon-level guarantees this does not cover. |
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
