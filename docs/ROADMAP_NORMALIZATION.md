# MEYAR — Owner-approved roadmap normalization (D-115)

## Verified accepted baseline — 2026-10-09

- Live `origin/main`: `386ae9da22f2e7cbb4286cde439292d8f21e25e4`.
- PR #128: independently accepted per owner, manually Squash-MERGED; accepted head
  `254895c2b6890019c84bff90f00b88038854a097`.
- Accepted-head and squash full trees both: `96a9934ffbd997cee5d7d5c2b0f3e2ba185d2b94`.
- Accepted-head CI: [37937363994](https://github.com/a-r3/meyar/actions/runs/37937363994), SUCCESS.
- #46 CLOSED; exactly #20/#34/#35/#36/#37/#38/#45/#50 OPEN before normalization.
- Dependency-update PRs #3/#4/#5/#127/#129/#130 are separate and untouched.

The roadmap audit is OWNER-ACCEPTED WITH AMENDMENTS. This documentation/issue
normalization requires its own independent audit; it does not claim independent
acceptance of the normalization PR or authorize implementation/merge/closure.

## Mandatory phase sequence

#46 CLOSED → roadmap normalization independently accepted + owner-merged
→ #45 API-first application contract completion
→ independent acceptance → OWNER manual Squash merge → post-merge verification
→ Comprehensive Adversarial Product + Architecture Audit
→ fix all P0/P1 + owner-selected P2 → full re-audit accepted
→ #35 Agentless Mac Deployment Readiness completion
→ #36 real Target-Mac benchmark + production model selection
→ #20 final DoD / production acceptance.

#45 is the next mandatory engineering phase after normalization acceptance/merge.
#35 is QUEUED DEPLOYMENT-COMPLETION PHASE. #50 is post-presentation/deferred;
#37 conditional. Neither is a mandatory initial-deployment gate.

## Milestone normalization and remaining owner actions

Milestones represent bounded delivery goals, not indefinite optional-backlog buckets.
Unambiguous reversible metadata changes: remove #50 from M8 (GitHub milestone 9)
and #37 from M9 (GitHub milestone 10), keeping both issues OPEN and unmilestoned.
Keep #34 in M8 pending owner review/closure; its concrete residuals transfer to #45
in M8. Keep #38 in M7 pending owner review/closure; unresolved debt transfers below.
M5 retains #20; M9 retains #35/#36. This normalization PR belongs to M8.
No milestone is created, renamed, closed or otherwise reorganized.

Owner actions after independent normalization acceptance:

- [#34](https://github.com/a-r3/meyar/issues/34): close as superseded/not planned for
  generic expansion after verifying concrete residual ownership in #45.
- [#38](https://github.com/a-r3/meyar/issues/38): close the historical bundle after
  verifying item-level ownership; then consider manual closure of
  [M7](https://github.com/a-r3/meyar/milestone/8).
- [M8](https://github.com/a-r3/meyar/milestone/9): consider manual closure only after
  #34 disposition and #45 acceptance/merge/verification. Optional #50 does not block it.
- [M9](https://github.com/a-r3/meyar/milestone/10): consider manual closure after
  #35/#36 acceptance. Conditional #37 does not block it.
- [M5](https://github.com/a-r3/meyar/milestone/6): retain OPEN until #20 final sign-off.

No issue/milestone closure or #45/#35/#36/#50 implementation is authorized here.

## Future comprehensive audit / debt register

This register is explicit ownership for the audit AFTER #45 acceptance/merge/
post-merge verification, before #35. It is not execution of that audit. The audit
must examine the whole product/architecture, then remediate all P0/P1 and
owner-selected P2 and perform full re-audit. This list does not limit audit scope.

| ID | Requirement / finding | Current evidence | Required disposition |
| --- | --- | --- | --- |
| AUD-B | #38 sequential duplicate test overclaims concurrency | `backend/tests/test_ui_job_lifecycle.py::test_concurrent_double_submit_is_rejected_by_db_constraint_not_only_precheck` uses one session and prior commit | Assess true two-connection interleaved coverage or accurate relabeling; record debt/remediation, not a claimed existing race proof. |
| AUD-C1 | #38 parser/folder pagination passthrough | `backend/src/meyar/ui/templates/library.html` preserves both parameters | Determine supported deep-link filtering vs cleanup; no speculative removal. |
| AUD-C3 | #38 planner outcome enum in DOM | `backend/src/meyar/ui/templates/search_results.html` and `agent.html` | Assess actual product/privacy impact; do not invent security severity. |
| AUD-RATE | MASTER_SPEC §11 per-tenant sliding-window request limit | `backend/src/meyar/config.py` declares/validates rate_limit_per_minute; source-wide search finds no consumer. `api/v1/usage.py` explicitly defers rate limiting. `main.py` registers body-limit/response policies, not request-rate enforcement. | Genuinely unimplemented requirement; comprehensive audit must decide explicit disposition/remediation and re-audit; #20 final DoD must consume it. No severity/design assigned here. |

Process-wide bounded inference admission (D-089), queue bounds and resource caps
are implemented separate controls; they do not fulfill per-tenant request limits.
#38 C2 is resolved (no visible block_index in candidate detail), C4 is resolved by
`backend/tests/test_ui_routes.py::test_supported_professional_searches_bypass_model`.
#38 A remains real and is #45 scope E (document current API/human duplicate semantics;
any compatibility change requires a separate explicit reviewed decision).

## Normalized GitHub issue bodies

The following bodies are the exact normalization payloads, versioned here so the
PR review can assess external metadata changes alongside documentation. Issues
remain OPEN; owner closure recommendations are not closure actions.

### Issue #45

#### Status

OPEN — NEXT MANDATORY ENGINEERING PHASE, queued until roadmap normalization is independently accepted and owner-merged.

This is NOT a second implementation of existing workflows. It exposes/extracts already accepted behavior through shared application/client-neutral contracts.

#### Objective

Make the implemented MEYAR conversation, JD review/confirmation, candidate-library and deterministic evaluation workflow consumable through documented client-neutral application and HTTP/JSON contracts.

Jinja UI and API adapters must call shared application/domain behavior.

Do not rewrite FastAPI.
Do not split the modular monolith.
Do not create duplicate scoring/search/JD policy.

#### Existing foundations — DO NOT REBUILD

- durable owner-scoped conversations;
- BrowserSession-separated durable conversation history;
- submission identity/idempotency;
- inference reservations and transaction release/revalidation;
- Agent Core v2 capability registry;
- bounded server-owned plans;
- structured clarification/dialogue state;
- canonical JD source authority;
- JD review/conflict/amendment logic;
- immutable scored-criterion semantic provenance;
- human-confirmed JD creation;
- AgentDraftConfirmation;
- same-session replay/idempotency;
- deterministic search/evaluation/scoring/ranking;
- candidate library/detail;
- authorized original-CV UI download;
- #46 runtime/recovery/retention foundations.

#### Required remaining scope

##### A. Shared application/principal boundary

- Extract only orchestration/response assembly that must be shared by UI and HTTP/JSON adapters.
- Define trusted application principal/context contracts for:
  - existing machine API-key operations;
  - accountable human workflows.
- Before human JSON endpoints are implemented, record an explicit reviewed human HTTP/JSON authentication/session design.
- Never accept client/model-supplied tenant/user/session ids as authority.
- API keys alone must NOT become human confirmation authority.
- Preserve live User + membership + BrowserSession + tenant authority.
- Explicitly address cookie scope, CSRF, JSON authentication failure semantics and OpenAPI security definitions.

##### B. Conversation + JD contracts

- typed conversation create/list/history/turn/new-conversation-reset contracts;
- bounded results;
- owner/session isolation;
- preserve submission idempotency and reservation behavior;
- preserve clarification/dialogue state;
- typed JD draft/review-resolution/confirmation contracts over the EXISTING workflow;
- only exact live server-held draft can be confirmed;
- browser/API/model fields cannot weaken authoritative criteria;
- reuse AgentDraftConfirmation;
- confirmation must not depend on inline ranking success;
- create an explicit privacy-safe correlation from proposal/draft → accountable human confirmation → resulting Job/CriteriaVersion while preserving #46 retention semantics.

##### C. Non-scorable JD requirement provenance

Current JobCriteriaVersion keeps unsupported/needs-review strings while scored criteria have stronger semantic provenance.

Preserve durable, source-bound handling of non-scorable requirements sufficiently to explain:

- exact source occurrence where permitted;
- disposition/state/reason;
- human review decision where applicable.

Do NOT make unsupported/prohibited content scoring authority.

Do NOT fabricate provenance for legacy/manual/API criteria.

##### D. Candidate contracts

- bounded tenant-authorized Candidate Library REST listing;
- stable ordering and supported filters;
- client-neutral candidate detail/evidence/provenance DTO ownership;
- remove REST dependency on UI view-model aliases where shared behavior is touched;
- truthful processing/readiness state contract;
- exact relevant document/profile/version provenance;
- bounded supported retry operations only for processing stages the system actually supports;
- no fake generic background-task framework;
- original-CV HTTP retrieval using server-resolved authorization and storage authority;
- synthetic filename;
- truthful attachment semantics;
- no storage path/key exposure;
- successful original-CV access must be audited through the shared application operation for BOTH UI and API clients.

##### E. Job/evaluation contracts

- bounded Job list;
- archive/lifecycle API parity for existing ACTIVE/ARCHIVED behavior;
- preserve criteria immutability and archive restrictions;
- document current duplicate semantics:
  human/UI workflow has duplicate guard;
  existing machine API creation permits duplicates.
- Do NOT silently change this compatibility contract without a separate explicit reviewed decision.
- persisted Evaluation GET-by-ID;
- full deterministic provenance/explanation;
- historical CandidateProfileVersion selection for authorized SINGLE-candidate reproducible scoring;
- normal batch ranking continues to use effective current authority;
- batch response exposes full existing ScoreExplanation;
- no new scoring authority.

##### F. Business date

Normal HR/client requests should not have to supply evaluation date.

For relevant score/rank/search application operations:

- date may be omitted;
- resolve configured business “today” exactly ONCE at application boundary;
- pass the resolved date EXPLICITLY into deterministic services;
- return/persist effective date;
- explicit supplied date continues to work unchanged for reproducibility;
- deterministic services must never read wall-clock time implicitly.

##### G. Errors/OpenAPI

- consistent privacy-safe machine error/reason/retryability contract;
- preserve planner non-executable outcomes separately;
- OpenAPI security/schema/response tests;
- synthetic workflow contract coverage;
- production HTTP docs/schema exposure policy remains unchanged.

#### Explicit invariants

- tenant identity server-derived;
- candidate-content AI local only;
- CandidateIdentity/photo/PII never suitability input;
- deterministic scoring/ranking remains authority;
- consequential mutations require accountable human confirmation;
- unsupported evidence remains UNKNOWN/unavailable;
- no external/cloud LLM receives candidate-bearing data;
- no raw CV/JD/secrets/storage paths/raw model output in audit metadata.

#### Out of scope

- #50 Q&A/comparison;
- speculative generic #34 multi-action framework;
- #37 integrations;
- #35 deployment work;
- #36 benchmark;
- new Job states;
- autonomous hiring;
- scoring-policy redesign;
- FastAPI rewrite;
- microservices;
- SPA rewrite;
- speculative `/v2`;
- unrelated dependency upgrades.

#### Phase dependency

#45 starts only after this roadmap-normalization PR is accepted and merged.

After #45:
independent acceptance
→ owner merge
→ post-merge verification
→ comprehensive adversarial product/architecture audit.

#35 does NOT start before that audit/remediation/full re-audit sequence is accepted.

### Issue #34

#### Status

OPEN — OWNER-CLOSURE-READY after independent normalization review.
Historical delivered-versus-residual record; generic expansion is deferred.

#### Delivered on accepted current main

The existing JD workflow already has:

- server-owned pending JD draft and live BrowserSession-bound pending authority;
- accountable human confirmation and independent CSRF;
- exact persisted proposal authority, tamper rejection and no silent Job mutation;
- `CREATE_JOB` HUMAN_ACTION_ONLY capability boundary;
- AgentDraftConfirmation and same-session replay/idempotency;
- historical confirmation identity that survives expired-session retirement;
- durable Job/criteria semantic provenance.

Evidence: D-057, D-088, D-091–D-094 and D-114; `backend/src/meyar/ui/router.py`,
`services/agent_draft_confirmation_repo.py`, `models/agent_draft_confirmation.py`,
`agent/capabilities/registry.py` (source paths under `backend/src/meyar/`), plus
`docs/AGENT_CORE_V2_DESIGN.md` and `docs/ISSUE_46_FINAL_CLOSURE.md`.

#### Concrete residual ownership → #45

- Shared application confirmation operation.
- Explicit reviewed human HTTP/JSON authentication/session transport design.
- Privacy-safe proposal/draft → accountable confirmation → Job/CriteriaVersion audit correlation, preserving #46 retention semantics.
- Client-neutral JD/conversation/application contracts.

These are explicit #45 requirements, not a new duplicate JD workflow.

#### Deferred generic expansion

A GENERIC arbitrary multi-action pending-action framework is deferred and is
NOT required for initial deployment. Do not implement a speculative generic
envelope merely to keep this historical issue alive.

Future calendar/interview/ATS or another actual mutating capability must create
a NEW concrete issue based on real requirements. Reconsider generic abstraction then.

#### Recommended OWNER disposition after normalization acceptance

Close as superseded/not planned for generic expansion after concrete residual ownership moved to #45.

The normalization agent must not close this issue. Retain M8 until OWNER reviews
and closes this historical record; generic expansion must not block M8 indefinitely.

### Issue #35

#### Status

OPEN — QUEUED DEPLOYMENT-COMPLETION PHASE.
This is not the current active engineering phase and does not start from zero.

#### Objective

Complete deployment and operator readiness on the real target topology with no
Claude Code, Codex or other coding-agent dependency on the target host.

#### Existing implementation — reuse, do not rebuild

`meyar-ops` PR1–PR13 tooling is delivered (D-066–D-068, D-073–D-082;
PR13 merged as PR #76). `docs/MEYAR_OPS.md` records command contracts:

- `build-release` / `verify-release`, release and model manifest verification;
- offline `bundle-build`, install/verify/activate-release;
- `config-verify`, protected host settings and service binding;
- `schema-init`, component readiness and `deployment-ready`;
- allowlisted `collect-diagnostics` / `diagnostics-verify`, bounded cleanup;
- `backup-create` / `backup-verify` and isolated `restore`;
- staged update and explicit application rollback with compatibility boundaries;
- offline Ollama/model bundle install, digest verification and readiness;
- launchd render/verify/install/start/stop/restart/status and dedicated Ollama lifecycle;
- `reboot-prepare` / `reboot-verify`, HTTPS `edge-verify`;
- installed-release synthetic smoke and `lifecycle-acceptance` evidence commands.

#46 runtime/readiness/recovery/retention foundations are also accepted and merged.
Tooling and Linux simulations do not prove physical Apple Silicon acceptance.

#### Remaining completion scope

- Real target topology/prerequisites, PostgreSQL provisioning and operator/service-account assumptions.
- Offline artifact/dependency/model handoff and final accepted release/schema/runtime/model identity.
- Scheduling and host log lifecycle; explicit bank operational policy.
- Physical Apple Silicon rehearsal, actual launchd restart and reboot proof.
- Backup/verify/isolated restore lifecycle and approved production recovery/cutover procedure.
- Staged update/rollback rehearsal within declared compatibility; no arbitrary Alembic downgrade.
- Operator handoff, access, security/environment approvals, bank HTTPS/CA/ingress decisions.

Unknown deployment decisions remain UNKNOWN until explicitly reviewed. Existing
commands must be validated against the accepted post-audit release, not discarded.

#### Mandatory dependency

#45 independently accepted + owner-merged + post-merge verified
→ Comprehensive Adversarial Product + Architecture Audit
→ fix all P0/P1 + owner-selected P2
→ full re-audit accepted
→ THEN #35 deployment completion.

#36 follows accepted #35 lifecycle and actual bank Mac availability. It owns actual
target performance and production model selection; #35 does not approve models.

#### Security & architectural invariants (must not be weakened for deployment convenience)

This slice must preserve, not relax, every existing product security/privacy
boundary:

- Tenant isolation at the data-access layer (no route trusts a
  client-supplied tenant id).
- Auth/session/CSRF protections unchanged in the deployed configuration.
- Original-CV access remains authorized/audited — no anonymous/public read
  path introduced by deployment tooling.
- No candidate/JD content exfiltration: local Ollama only, never publicly
  exposed, no external/cloud AI endpoint reachable from the deployed service.
- Secrets (DB credentials, API keys, model config) live outside Git and are
  never written to logs, CLI history, or the support bundle in plaintext.
- No real candidate CVs/PII in repository, deployment fixtures, or synthetic
  smoke/demo data — synthetic fixtures only.
- Evidence/provenance and deterministic-scoring guarantees (policy engine,
  not the LLM, computes final status) are unaffected by packaging/runtime
  changes.
- Auditability of deployment actions (who provisioned/updated/rolled back,
  when, from which accepted release artifact).
- Migrations remain reproducible and deterministic (single expected Alembic
  head verified on fresh install).
- Backup/restore procedures preserve data integrity and do not silently
  drop or corrupt tenant data.
- Update/rollback paths are clean: no partial-state deployments, no
  undeclared schema-compatibility breaks.

#### Acceptance criteria

- [ ] Agentless deployment can be executed end-to-end from documented
      commands/scripts alone.
- [ ] No coding agent (Claude Code, Codex, or equivalent) is required on the
      destination Mac.
- [ ] No manual source editing occurs on the destination Mac.
- [ ] Fresh provisioning path is documented and rehearsed on a non-target
      Apple Silicon machine where feasible.
- [ ] Migration procedure is reproducible and verifies a single expected
      Alembic head on fresh install.
- [ ] Service survives restart/reboot per the chosen service model
      (`launchd` unless a better accepted mechanism is demonstrated).
- [ ] Local healthcheck/readiness command works.
- [ ] Synthetic application smoke passes (auth, UI opens, candidate library,
      search, vacancy review/ranking) with no real PII.
- [ ] Ollama requests remain local; no external AI path exists or is
      reachable.
- [ ] Backup/restore rehearsal passes with synthetic data only.
- [ ] Update procedure is documented and tested (staged, with backup/
      readiness gate before activation).
- [ ] Rollback procedure is documented and tested to a defensible,
      explicitly declared compatibility boundary (never an arbitrary
      Alembic downgrade).
- [ ] Support/diagnostics bundle collection is documented, allowlisted, and
      redacts secrets — no candidate-content exfiltration.
- [ ] Secrets are not committed to Git and not logged (full or partial).
- [ ] Tracked-tree scan is clean (no secrets/PII/real CVs staged).
- [ ] Relevant tests/quality gates are green.
- [ ] Final deliverables are usable by bank IT operations staff without
      Claude Code/Codex.
- [ ] Explicitly out of scope: this issue does **not** claim actual
      performance/model acceptance on the real bank Mac — that is #36's
      scope, executed after #35 lands.

### Issue #36

#### Status

OPEN — TARGET-HARDWARE EXECUTION AND PRODUCTION MODEL APPROVAL PENDING.

#### Target

Owner-confirmed Mac mini M4 Pro: 12-core CPU, 16-core GPU,
24 GB unified memory, 512 GB SSD. No coding agent may be required on this host.

#### A. Harness completion BEFORE bank visit

The current `backend/scripts/target_mac_benchmark.py` is a foundation, not
complete proof of this issue's full matrix. It measures PDF/DOCX parsing,
profile extraction and embedding; other operations are explicitly unmeasured.
Complete and verify the synthetic harness before the visit. A non-target run
verifies the harness only and must never count as target acceptance.

The benchmark harness must be complete before visiting the bank.

The machine-readable run artifact should include conceptually:

```text
manifest.json
environment.json
samples.jsonl
resources.csv
summary.json
report.html
checksums.sha256
```

The benchmark must distinguish:

- PASS
- FAIL
- INCOMPLETE
- NOT_TARGET_HARDWARE

Cover at minimum:

- environment/release/schema/model identity
- cold/warm startup
- DB/Ollama/model readiness
- PDF/DOCX canonicalization
- extraction
- identity extraction
- embeddings
- end-to-end candidate processing
- reconciliation
- duplicates
- changed-CV reprocessing
- structured/semantic/hybrid search
- agent query + follow-up
- JD drafting
- criteria confirmation
- deterministic ranking
- candidate detail/canonical preview/original authorization
- concurrency/overload
- memory/CPU/swap/disk/DB growth
- failure/recovery
- backup/restore

Correctness/security failure is a blocker regardless of latency.
Missing measurement is INCOMPLETE, not PASS.
Issue #20 / M5 stays open until actual bank-Mac execution is complete.

#### B. Actual execution ON the real target Mac

Run the complete approved matrix using the accepted immutable installed release,
record release/schema/runtime/model names and digests, real resources and cold/warm
measurements, and preserve privacy-safe machine-readable evidence. Missing measurement
is INCOMPLETE; correctness/security failure is a blocker regardless of latency.
The historical owner policy is functional success plus recorded timings, with no
invented latency threshold (D-020); impractical behavior requires an owner decision.

Production LLM/embedding model approval remains BLOCKED until actual target execution
and an explicit evidence-backed decision. Development models are not production approval.

#### Dependencies

- Accepted post-audit application release (after #45, audit/remediation/full re-audit).
- Accepted #35 agentless deployment lifecycle.
- Actual bank Mac availability.

M5/#20 consume this evidence for final acceptance; neither closes automatically.
References: `docs/TARGET_MAC_BENCHMARK.md`, `docs/MEYAR_OPS.md`, D-115.

### Issue #20

#### Status / objective

OPEN — FINAL DoD / PRODUCTION ACCEPTANCE UMBRELLA (M5).
Consume accepted evidence; do not duplicate implementation owned by #45/#35/#36.

#### Historical delivered evidence — retain and revalidate for final release

- PR #22 / D-020 delivered original-CV UI retrieval, >300-page rejection regression,
  migration-chain CI, audit-privacy guard, local-only no-exfiltration proof,
  synthetic multilingual pipeline evidence and synthetic backup/restore proof.
- Subsequent accepted UI work supplies a separate canonical in-app preview.
  Original PDF/DOCX retrieval is a truthful ATTACHMENT download with a synthetic
  filename, server-resolved authorization/storage and no storage path exposure.
- Human UI auth is User + live membership + BrowserSession + tenant authority,
  not retired API-key browser login. Machine Bearer API keys remain separate.
- Deployment release/config/readiness/backup/restore/update/rollback/diagnostics/
  launchd/reboot/HTTPS/evidence tooling exists (D-066–D-082); physical lifecycle
  acceptance remains #35.
- #46 final runtime/recovery/retention engineering is accepted and owner-merged
  through PR #128: squash `386ae9da22f2e7cbb4286cde439292d8f21e25e4`,
  accepted head `254895c2b6890019c84bff90f00b88038854a097`, identical tree
  `96a9934ffbd997cee5d7d5c2b0f3e2ba185d2b94`; #46 is CLOSED.

Historical 22/2/4 and later 25/1/2 official-matrix counts describe their dated
baselines. They are not current final-production acceptance counts.

#### Required evidence for final owner sign-off

- [ ] #45 shared application/HTTP contracts independently accepted, owner-merged and post-merge verified.
- [ ] Comprehensive Adversarial Product + Architecture Audit completed; all P0/P1 and owner-selected P2 fixed; full re-audit accepted.
- [ ] #35 real topology/prerequisites, immutable offline handoff, physical Apple Silicon lifecycle, scheduling/log policy, backup/restore/update/rollback and operator handoff accepted.
- [ ] #36 complete harness before bank visit, actual full-matrix execution on the bank Mac and production model decision.
- [ ] Final security/operational prerequisites, access, HTTPS/CA, disk/data protection, approved retention, backup/recovery and environment approvals.
- [ ] Final official requirement traceability against the accepted release, with explicit evidence/deferred-by-decision rows and regression/security/operational acceptance.
- [ ] Owner production sign-off; no automatic issue or milestone closure.

#### Explicit requirement follow-up: per-tenant rate limiting

MASTER_SPEC §11 requires per-tenant sliding-window request enforcement. Current
main has validated `Settings.rate_limit_per_minute` but no enforcement consumer;
`api/v1/usage.py` explicitly says enforcement remains later work. Process-wide
inference admission is implemented and is a different control.

Future comprehensive audit must examine this gap and record an explicit disposition,
required remediation and re-audit evidence before final DoD. Do not silently omit it,
assign severity or choose an implementation design in this normalization.

#### Target / model authority

Mac mini M4 Pro, 12-core CPU, 16-core GPU, 24 GB unified memory, 512 GB SSD
is owner-confirmed. Actual target benchmark is unexecuted; production LLM and
embedding selection remain blocked. Existing harness is partial relative to #36.
No arbitrary latency thresholds are newly approved here (historical D-020 policy applies).

#### Scope boundaries

#50 is post-presentation/deferred and #37 conditional; neither is a mandatory initial
deployment gate without an explicit new owner requirement. Generic #34 expansion
is deferred; concrete current JD/application residuals belong to #45.
OCR remains deferred per D-007; synthetic multilingual evidence is not real-model
quality approval. Bank repository migration preserves full history and remains an
organizational matter. Local-only candidate AI, deterministic authority, tenant
isolation, accountable human confirmation and privacy-safe audit are mandatory.

References: D-020, D-115; `docs/STATUS.md`, `docs/ROADMAP_NORMALIZATION.md`,
`docs/SECURITY_PRIVACY.md`, `docs/TARGET_MAC_BENCHMARK.md`.

### Issue #38

#### Status

OPEN — OWNER-CLOSURE-READY after independent normalization review.
This historical PR #29 findings bundle is disaggregated below. Closure of this
tracking issue does not claim unresolved findings are fixed.

#### Item-level disposition and explicit owners

| Item | Current-main status | Evidence / transferred ownership |
| --- | --- | --- |
| A — machine API jobs vs human duplicate semantics | STILL REAL | `test_api_job_creation_is_unaffected_by_ui_duplicate_guard` in `backend/tests/test_ui_job_lifecycle.py`: human/UI guard rejects duplicates; machine API creation permits them. #45 owns documentation/explicit contract decision. Do not silently change compatibility. |
| B — fake “concurrent” duplicate test | STILL REAL — test debt | `test_concurrent_double_submit_is_rejected_by_db_constraint_not_only_precheck` creates and commits the first Job, then inserts another on one session. It proves DB uniqueness after a prior commit, not a two-connection interleaved race. Future comprehensive audit/debt register item AUD-B. |
| C1 — parser/folder pagination passthrough | STILL PRESENT | `backend/src/meyar/ui/templates/library.html` preserves parser_status/folder_status in pagination links. Future comprehensive audit/debt register AUD-C1; determine whether supported deep-link filtering should be retained before cleanup. |
| C2 — visible block_index | RESOLVED | `backend/src/meyar/ui/templates/candidate_detail.html` no longer displays block_index; authorized evidence locations remain. |
| C3 — planner outcome enum in DOM | STILL PRESENT | `backend/src/meyar/ui/templates/search_results.html` has data-outcome; agent.html also carries a coarse outcome attribute. Future comprehensive audit/debt register AUD-C3. No security severity claimed without evidence. |
| C4 — deterministic fast path lacks request-path coverage | RESOLVED | `backend/tests/test_ui_routes.py::test_supported_professional_searches_bypass_model` posts to `/ui/search` with FakeLLMProvider raising if called and asserts zero calls. |

AUD-B / AUD-C1 / AUD-C3 are explicit entries in `docs/ROADMAP_NORMALIZATION.md`'s
future Comprehensive Adversarial Audit debt register. A is explicit #45 scope E.

#### Owner disposition

After independent review verifies those ownership transfers, OWNER may close #38.
Keep it OPEN and retain M7 until that review/closure. M7 can then become closure-ready;
no milestone closure is authorized by this task. No fixes are implemented here.

### Issue #50

#### Status

OPEN — POST-PRESENTATION / DEFERRED CAPABILITY.
NOT A MANDATORY INITIAL DEPLOYMENT GATE. Removed from mandatory M8 milestone tracking;
no replacement optional-backlog milestone is invented.

#### Delivered foundations and future dependency

#49 and #84/#85/#86/#87/#88 foundations are delivered, accepted and merged;
these issues are CLOSED. Reuse server-owned ResultSets, semantic JD authority,
transaction separation/admission, stale semantics, request/session integrity and
Agent Core v2 registry/bounded plans/dialogue state.

Dedicated candidate factual Q&A and deterministic multi-candidate comparison remain
genuinely UNIMPLEMENTED. Future #50 must consume #45's accepted application contracts
where relevant and register capabilities through Agent Core v2 rather than adding
an ad-hoc chat router. This normalization does not start #50.

#### Purpose

After a server-owned result set exists, allow HR to ask questions such as:

- "ikinci namizədin Java təcrübəsi neçə ildir?"
- "bank təcrübəsi varmı?"
- "1 və 3-ü müqayisə et"
- "bu məlumatı CV-də haradan götürdün?"

#### Required architecture: server-owned structured/evidence-aware RAG

Candidate factual Q&A must **not** be answered from conversational memory
alone. This is intentionally not vanilla "raw CV chunks → vector search →
LLM → trust answer".

Required retrieval flow:

```
authorized candidate reference (via #49's result_set)
  → tenant/session/result-set authorization
  → accepted CandidateProfile / structured facts
  → relevant accepted evidence / canonical CV spans
  → deterministic calculations where applicable
  → local LLM explanation
  → evidence-backed answer
```

Use existing MEYAR structured/vector/hybrid retrieval infrastructure where
appropriate, but server authorization and accepted evidence remain the
authority — not the LLM.

#### Required invariants

- Local Ollama only; no candidate content to external AI APIs.
- Server-owned candidate identity/reference (via #49).
- Evidence/provenance citations on every factual answer.
- `UNKNOWN` / insufficient evidence when a fact is unsupported — never a
  guess, never a silent negative assumption.
- Current-period duration questions ("how many years...") use an explicit
  evaluation date and deterministic interval math, not LLM arithmetic.
- Deterministic comparison of supported criteria — criterion-by-criterion,
  not a freeform LLM judgment.
- The existing deterministic score/evaluator remains the authority for any
  scoring-shaped question.
- The LLM must not invent numeric hiring scores.
- The LLM must not select a hiring winner or make the final hiring
  decision.
- Protected/PII data (`CandidateIdentity` fields) must not secretly
  influence suitability answers.
- No answer is produced solely from conversation memory — every factual
  claim traces to accepted `CandidateProfile`/evidence.
- Tenant/session/result-set context is enforced on every candidate
  reference.
- Replay/reload/stale context is handled the same way #49 handles it for
  result sets.
- Retrieval provenance is auditable (which candidate, which result set,
  which evidence spans, what was asked).

#### Comparison flow

```
server-authorized candidates (via #49)
  → criterion/factual retrieval
  → deterministic facts/evaluation
  → evidence
  → local LLM explanation
```

Missing evidence → `UNKNOWN` / insufficient evidence. Never "assume
negative" and never invent experience that isn't in accepted evidence.

#### Non-scope

- Hiring recommendation or winner selection by the LLM.
- Any new scoring authority outside the existing deterministic policy
  engine.

#### Acceptance criteria

- [ ] Candidate-bound Q&A always validates the candidate reference
      server-side against the active `result_set` (#49) before answering.
- [ ] Factual answers cite accepted CV evidence (provenance visible).
- [ ] Duration questions use deterministic interval logic with an explicit
      evaluation date, not LLM-computed arithmetic.
- [ ] Unsupported facts return `UNKNOWN`, never a guess or an assumed
      negative.
- [ ] Comparison output is factual and criterion-by-criterion.
- [ ] No LLM-authored final numeric score anywhere in this flow.
- [ ] No LLM hiring recommendation or winner selection anywhere in this
      flow.
- [ ] The #49 result-context dependency is enforced, not bypassed.
- [ ] No cross-tenant or cross-session candidate access.
- [ ] No external AI exfiltration of candidate content.
- [ ] Audit log contains candidate/result/evidence provenance for each
      answered question.
- [ ] Q&A/comparison are implemented as Agent Core v2 (#88) registry
      capabilities, validated by the server-owned plan boundary.

### Issue #37

#### Status

OPEN — DEFERRED / CONDITIONAL.
NOT A MANDATORY INITIAL DEPLOYMENT GATE unless actual bank environment requirements
make a specific integration concrete. Removed from mandatory M9 milestone tracking;
no replacement optional-backlog milestone is invented.

#### Objective

Track (do not implement) future target-environment-dependent integrations.

#### Scope — tracking only

- **Enterprise identity provider integration** (would extend Slice 1's
  user/role model — the design in Slice 1 must not preclude this, but must
  not hard-code a specific provider either).
- **Calendar/scheduling integration adapter** (would extend Slice 5's
  confirmed-actions extension points).
- **ATS / external HR-system integration.**

#### Explicit constraint

**Do not implement any of the above, and do not hard-code any specific
external vendor or deployment organization, until the target deployment
environment's actual infrastructure is known.** This issue exists purely so
the roadmap acknowledges these needs without inventing premature
integration surface.

#### Exit condition

This issue is re-scoped into concrete implementation slices only after the
owner confirms the target environment's actual identity-provider/
calendar/ATS-integration requirements.

#### References

- Slice 1 (Human Identity & Dual Access) — design compatibility target.
- Existing JD confirmation boundary; generic #34 expansion is deferred. Any real
  mutating integration requires a NEW concrete issue and reviewed confirmation design.

## Change boundary and verification policy

Repository changes are Markdown documentation/governance only; GitHub mutations
are the eight versioned issue bodies and two milestone-assignment removals above.
No application code, migrations, schema, templates, CSS, JavaScript, tests, dependencies,
scripts or deployment tooling are changed. Local full pytest is not necessary for
this documentation-only task; the existing PR CI quality gate remains unchanged.
Independent normalization acceptance and OWNER actions remain required.
