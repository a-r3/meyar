# MEYAR — Status

## Current phase

**Issue #62 (Search/Agent evidence attribution for skill experience and
language level) is under implementation on
`fix/issue-62-search-evidence-attribution`, based on accepted `main`
`35ef79e0007b6796030d1d8b0095f25f348732ce`.** PR #61 (candidate
photo extraction and identity-only presentation, issue #60) is merged.
This corrective preserves search eligibility, ranking, and scoring;
issue #63 separately tracks order-sensitive duplicate language facts.
Owner independent acceptance remains required before any #62 merge.

**PR #42 (M8 Slice 4 — Agent Product UX & JD Matching, issue #33) is
MERGED — squash SHA `584eb3584f10abf13db03046d32f85041b4df2ab` on `main`.**
Issue #33 is CLOSED. PR #42 carried the full issue #44 AZ/EN
generalization-hardening corrective series (D-042 through D-065,
including the D-061/D-062/D-063 passes narrated below plus two later
independent-audit passes, D-064 and D-065, recorded in
`docs/DECISIONS.md`) to acceptance and merge. **Issue #44 is CLOSED.**
Post-merge, `main` at that SHA carries a clean quality gate and a clean
`scripts/scan-tracked-tree.sh`. HR UI Productization & Presentation
Readiness (issue #27, PR #29, M7) is likewise merged/accepted — see the
correction below in this same section.

**Current active engineering phase: issue #35 — Agentless Mac Deployment
Readiness (M9).** Tested, executable (not merely documented)
provisioning/configuration/PostgreSQL-pgvector/migration/Ollama-and-model-
setup/service-lifecycle/healthcheck/backup-restore/update-rollback/
diagnostics tooling, with no Claude Code/Codex/AI-coding-agent dependency
on the target deployment host.

**PR1 for #35 (`meyar-ops` foundation, D-066) is MERGED — squash SHA
`91c4e7e02ac7d9851a70efe4f5a11d061ee61b28` on `main` (PR #52).** Added the
typed `meyar-ops` CLI (`preflight`/`status`/`readiness`/`verify-release`),
the release/model manifest contracts, and release-artifact verification
(no artifact building yet). See `docs/MEYAR_OPS.md`. Verified on Linux
only; Apple-Silicon/Mac-mini-M4-Pro rehearsal remains **UNCONFIRMED**
(issue #36's scope), and no production model is approved by this PR.

**PR2 for #35 (macOS LaunchDaemon service foundation, D-067) is MERGED —
squash SHA `60953e37ab41a8165a871538840eeb8a008fd383` on `main` (PR
#54).** Added three `meyar-ops` commands — `service-render`,
`service-verify`, `service-status` — that render, verify, and
read-only-probe a macOS system LaunchDaemon plist for the MEYAR
application process (system LaunchDaemon -> dedicated non-root
`UserName` -> MEYAR application process -> loopback-bound Uvicorn). This
is foundation only: no `service-install`/`start`/`stop`/`restart`, no
plist ever written to `/Library/LaunchDaemons`, no `launchctl` mutation
(`bootstrap`/`bootout`/`kickstart`), no `sudo`, no service-account
creation, no PostgreSQL/Ollama lifecycle, no HTTP `/ready` (#46), no #36
benchmark/model work, no production-model approval, no HTTPS/reverse-
proxy/PKI topology. Privileged install/start/stop/restart remain **NOT
implemented**. See `docs/MEYAR_OPS.md`. Verified on Linux only, with an
injected fake `launchctl` runner for `service-status` — real macOS/
Apple-Silicon `launchctl`/reboot behavior remains **UNCONFIRMED**. The
production model remains **TBD**.

**PR3 for #35 (immutable application release artifact builder, D-068) is
MERGED — squash SHA `500e665c8e32e2f6c9f574b70a4ba7462623de73` on `main`
(PR #56).** `meyar-ops build-release` is now accepted on `main`. It
builds an immutable, verifiable MEYAR **application** release artifact
(`backend/src/meyar/**`, migrations, `pyproject.toml`/`uv.lock`, and
release identity/integrity metadata) from an exact Git commit SHA, via
fixed-argv Git plumbing, never the mutable working tree — exact
Git-commit provenance remains the authority for every source-derived
manifest field. Allowlist-driven (`backend/tests/`, `backend/scripts/`,
`.env` never enter the picture); Alembic migration Python is statically
parsed (`ast.parse`/`ast.literal_eval`), never executed, by
`build-release`; produces an embedded + external `ReleaseManifest` and a
`SHA256SUMS`-bound output bundle that passes the existing, unmodified
`verify-release` command, whose own resource bounds are prechecked
before use. Output-identity failure semantics remain fail-closed
(`OUTPUT_IDENTITY_UNAVAILABLE`). This is **not** yet a complete offline
deployment bundle: no Python runtime, `uv` executable, third-party
wheels/offline wheelhouse, Ollama, models, or PostgreSQL are packaged,
and no host install layout/extraction/activation/service-lifecycle
orchestration exists. No production model approval is made or implied.
See `docs/MEYAR_OPS.md` and `docs/DECISIONS.md` D-068. Verified on Linux
only, against real throwaway Git repositories and this repository's own
real HEAD commit (read-only); Apple-Silicon runtime acceptance remains
**UNCONFIRMED** and the production model remains **TBD**.

Issue #35 remains OPEN as the current active engineering phase (offline
dependency provisioning, host install layout, plist
*installation*/lifecycle orchestration, and further #35 work are still to
come); issue #36 remains OPEN and not started; issue #46 remains OPEN and
separate.

**Chore (D-069, not a #35/#36/#46 slice):** `scripts/demo-up.sh`/
`scripts/demo-down.sh` — a one-command wrapper around the existing,
unmodified local-demo flow (`docs/LOCAL_DEMO.md`, issue #25, D-022) for
presentation operator UX. No backend/product/`seed-demo` behavior
changed. See D-069 for full detail.

**Next phase: issue #36 — Real Target-Mac Model Selection & Benchmark
(M9).** Executes `backend/scripts/target_mac_benchmark.py` on the actual
confirmed reference hardware (Mac mini M4 Pro, 12-core CPU/16-core
GPU/24GB unified memory/512GB SSD) once issue #35's tooling is in place.
**This benchmark has not yet been executed on real target hardware.**
Issue #20 (Slice 13 — Security + Official Definition-of-Done Acceptance)
and milestone **M5 remain OPEN** until that real-hardware benchmark and
the resulting production-model decision are complete; #35 and #36 extend
M9 and do not supersede or close M5/#20.

**Other open/backlog work:** issue #45 (API-first agent/application
contract completion, M8) and issue #46 (pre-deployment runtime/ingestion/
recovery hardening, M9) remain OPEN. Issues #49 (server-owned search
result-context and conversational follow-ups) and #50 (evidence-backed
candidate Q&A and deterministic comparison, depends on #49) are
post-presentation capability-backlog items — **planned, not
implemented.**

The detailed narrative immediately below (the D-055 through D-063 passes,
and the earlier M7 HR UI productization detail) is retained as historical
decision context. It was written while that work was still local/
pre-merge; both lines of work were subsequently accepted and merged, per
the correction above.

**Issue #44 AZ/EN generalization hardening has an independent-audit corrective
implementation locally on `feat/agent-product-ux-jd-matching` (D-061/D-062), not
pushed or merged.** D-055's
canonical `RequirementSpan` architecture and every previously accepted
candidate/JD/confirmation boundary remain intact. Confirmed agent vacancies
now round-trip and deterministically evaluate skill-specific duration,
domain experience, and language proficiency; persist safe unsupported/review
disclosures and bounded result count on the immutable criteria version; use a
configured business timezone to resolve one explicit UI-boundary evaluation
date; and return the requested top 1–100 eligible results without changing
scores or requirements. The real local browser path now adapts bounded legacy
Ollama draft shapes to those typed semantics, exposes supported modality
ambiguity for protected HR resolution, and provides a stable reloadable
ranking URL with criterion evidence. The source-bound semantic layer now
supports Azerbaijani and English (plus safe mixed professional terminology),
blocks protected attributes before and after parsing, preserves unfamiliar
professional subjects without grammar suffixes, shares skill-duration/domain/
language-level semantics with ordinary search, binds top-K separately, and
handles bounded pending-draft edits before search routing. D-061 replaces the
remaining phrase-bounded protected checks with concept/grammar policy, makes
subject identity an exact bounded occurrence rather than a clause remainder,
preserves professional families, rejects count expressions at the typed
criterion boundary, reconciles every material span to one terminal state,
supports truthful bidirectional modality follow-ups, and gives equivalent
simple-search-in-vacancy-mode guidance in AZ/EN. Russian fails closed before
inference. D-062 completes the exact-browser correction: English recruitment
wrappers and coordinating clauses retain only their attributable professional
subjects; explicit presence-only domain experience uses the evaluator's
existing optional-duration contract; context-only modality changes resolve
only a unique source bucket; duration/top-K search executes deterministically;
and wrong-mode guidance suppresses incompatible review copy.
(This pass was local/pre-merge at the time of writing; it was later
accepted and merged as part of PR #42 — see the current-status
correction at the top of this section.)

**Issue #44 final-audit parser correction (D-063) is implemented locally on
the required independent-audit base.** Passport-holder/national eligibility
proxies are protected by person-context grammar while technical passport
systems remain professional material; result-limit parsing exposes explicit
ABSENT/VALID/AMBIGUOUS/OUT_OF_RANGE state plus complete consumed source
occurrences; quantifiers cannot become typed professional identities; named
certifications survive adjacent quantity language; discourse wrappers are
excluded from exact subject offsets; and unambiguous English presence-only
domain experience uses the accepted optional-duration evaluator contract.
The accepted scoring, candidate authority, session/confirmation, ordinary
search, local-only Ollama, and presentation architectures are unchanged.

**Slice 13 — Security + Official Definition-of-Done Acceptance: implementation
pass 1 MERGED (PR #22, squash SHA `a709ce1`, `Refs #20`); Target-Mac hardware
gate PENDING.** Original-CV access, no-exfiltration formal verification,
backup/restore acceptance, audit-privacy guard, and multilingual (AZ/RU/EN)
evidence are all implemented and tested. Target reference hardware is now
owner-confirmed (Mac mini M4 Pro, 12-core CPU, 16-core GPU, 24 GB unified
memory, 512 GB SSD — a validated MVP reference configuration, not a permanent
platform lock-in; see `docs/TARGET_MAC_BENCHMARK.md`), but the benchmark has
not yet been executed on that hardware — this remains the sole mandatory
blocker to closing M5. Issue #20 and M5 remain OPEN (PR #22's merge did not
and must not close either).
Governance PR #1 merged at `16929fd` (**M0 CLOSED**); Slice 6 PR #7
merged at `55fef2d` (**M1 — CV Ingestion & Candidate Library is
CLOSED**, issue #6 closed); Slice 7 PR #9 merged at `24b1d67` (issue #8
closed); Slice 8 PR #11 Squash-merged at `412d978` (issue #10 closed).
Slice 9 PR #13 merged at `1be5d51` (issue #12 closed; **M2 CLOSED**).
Slice 10 PR #15 merged at `1c9dbbd` (issue #14 and **M3 — JD Matching &
Ranking CLOSED**). Slice 11 PR #17 squash-merged at `e182bdd` (issue #16
closed). Slice 12 PR #19 squash-merged at `93fa567` (issue #18 closed;
**M4 — Internal Product Interface & API CLOSED**). Slice 13 PR #22
squash-merged at `a709ce1` (`Refs #20`, issue #20 deliberately left open —
see above). Slice 14 PR #24 squash-merged at `f6e31ff` (`Closes #23`, issue
#23 closed; D-021). Chore PR #26 (pre-presentation readiness/local demo
bootstrap, issue #25, D-022) squash-merged at `a539e34`. Local `main` and
`origin/main` currently sit at `a539e34`.
**M5 — Security, Target-Mac Validation & MVP Acceptance is OPEN**,
containing only issue #20 (Slice 13 — Security + Official
Definition-of-Done Acceptance) pending the Target-Mac benchmark gate.
**M6 — Operational CV Intake & Reconciliation is CLOSED** (owner-approved
closure; issue #23 closed by PR #24, no remaining open issues).
**M7 — HR UI & Presentation Readiness is OPEN** (issue #27; owner-driven
HR UI productization pass following visual inspection of the running
local UI — see D-023 through D-029, and "In progress" below). PR
#29 (same branch) received a second owner visual inspection that found
seven further blockers (NL search still generically failing on ordinary
Azerbaijani phrasing, no vacancy-creation UI, raw criterion ids on the
ranking table, developer wording, technical metadata on candidate detail,
CV-preview XSS confirmation, and a mislabeled inline-vs-download original
CV action) — all fixed in the same PR; see D-024. A third owner visual
inspection found the new vacancy-creation form's "Ad"/"Dəyər" field split
had produced a malformed criterion (root-caused, not a scoring bug —
value held the criterion's TYPE instead of the requirement) and that the
NL-search "unsupported" message didn't distinguish a genuine deterministic
product-policy gap from the small local planner model simply misjudging
an ordinary request — both fixed; see D-025. The owner then required a
stronger guarantee: common explicit NL search intents must not depend on
local-model quality at all, not just get a more honest failure message —
addressed with a conservative deterministic fast-path parser (skills,
languages, certifications, total experience, simple "və" combinations)
that executes with zero LLM calls for exactly the concepts SearchPlan
already represents, falling back to the LLM unchanged for everything
else; see D-026. A dedicated semantic-correctness audit of that fast path
then found it was silently converting "N years of experience IN skill X"
into "skill + N years TOTAL experience" — a real weakening, not just an
honesty issue — fixed at the shared precheck (so no consumer can produce
that combination without an explicit HR confirmation via a new
clarification screen); also fixed a previously-unimplemented vacancy
kind-aware validation gap and reported (not yet fixed) a Job duplicate-
title/no-lifecycle gap; see D-027. That reported gap is now closed: Job
gained a persisted ACTIVE/ARCHIVED lifecycle (migration `db7e4523f491`,
existing rows deterministically backfilled ACTIVE, no hard delete
anywhere), a default-active `/ui/jobs` listing with an explicit archive
view and a CSRF/tenant-scoped "Arxivlə" action, and canonical-signature
duplicate-creation protection (titles remain non-unique; an identical
normalized title+criteria combination is rejected for a second ACTIVE
job, enforced by a real partial-unique-index DB constraint against
concurrent double-submits, not just an application-level check) — scoped
to the `/ui/jobs` form path only, `POST /api/v1/jobs` unchanged; see
D-028. A fourth owner visual check then found the "Minimum müddət (il)"
duration input still visibly enabled for non-EXPERIENCE rows even though
`_parse_criterion_row` (D-027) already rejected it server-side — fixed
with a self-hosted vanilla-JS presentation enhancement plus a matching
server-rendered initial/re-rendered state, so the control is disabled
and cleared for every kind but EXPERIENCE with or without JavaScript;
server-side validation is unchanged and remains authoritative; see
D-029. PR #29 was subsequently **MERGED** at squash SHA `1f8bd12`
(2026-09-01), closing issue #27; the HR UI productization/presentation-
readiness pass described above is accepted.

**Product-direction pivot recorded (2026-09-01).** Following an
owner-requested independent full product/architecture audit, MEYAR adopts a
bounded local-AI HR agent as the primary future UX; see D-030 (product
direction), D-031 (search architecture — `SearchPlan`/deterministic policy
become internal tool boundaries; the D-026 deterministic fast-path is now
FROZEN with an explicit parity-based sunset condition; tool-calling does
not weaken deterministic validation), and D-032 (Job/Vacancy backend
retained as-is; primary UX shifts toward agent-drafted criteria + human
confirmation; current Vacancies UI is supporting/deferred). This is a
documentation/governance-only change on the `feat/hr-ui-productization`
branch — no source code changed, no PR #29 functionality removed. Roadmap
recorded in `docs/MVP_PLAN.md`; tracked via new GitHub milestones **M8 —
Bounded Local-AI HR Agent Platform** and **M9 — Deployment, Benchmark &
Integration Readiness** (issues #30–#37), without closing M5/#20 or any
other existing milestone/issue.

**M8 Slice 1 — Human Identity & Dual Access (#30) implementation complete,
PR not yet opened (2026-09-01).** Branch `feat/human-identity-dual-access`
from synced `main` (`1f8bd12`). Adds `User`/`TenantMembership` (Argon2id
password hashing, `argon2-cffi`), a centralized minimal role model
(`meyar.core.roles`: `HR_USER`/`ADMIN`, currently identical permissions —
no admin-only UI action exists yet to differentiate them), and replaces
`/ui/login`'s API-key-paste form with username/password authentication —
generic/timing-safe failure message, session-fixation-safe fresh cookie,
a server-rendered tenant-selection screen (stateless HMAC-signed
pending-login token) for a user with more than one active membership.
`BrowserSession` now carries `(user_id, tenant_membership_id)` instead of
`api_key_id`; every request live-rechecks `User.is_active`/
`TenantMembership.is_active`. `AuditEvent` gained structured
`actor_type`/`actor_id` (`ACTOR_HUMAN_USER`/`ACTOR_API_KEY`/
`ACTOR_SYSTEM`), wired into UI login/logout/job-create/job-archive and
REST job/candidate create/delete — both a human and a machine action are
now individually attributable without ever storing a name/email/password/
API-key secret. New CLI provisioning (`create-user`, `add-membership`,
`set-password`, `disable-user`/`enable-user`,
`disable-membership`/`enable-membership`; secrets are interactive-only via
`getpass`, never a CLI argument). `seed-demo` now also bootstraps/rotates
a synthetic human login (`demo.hr`) alongside the existing API key, with
the same positive-identification collision guards as the existing tenant
logic. One Alembic migration `f4a91c2e6b7d` (`db7e4523f491` → head):
upgrade/downgrade/re-upgrade proven against the real dev DB and by a new
automated migration test that preserves pre-existing `Tenant`/`ApiKey`
rows. The machine REST path (`meyar.core.auth`, `TenantContext`,
`require_scope`) is completely untouched — dual access is real and
independent, not a shared credential bridge. Quality gates: `ruff` clean,
`mypy src` clean (127 files), `alembic heads` = one head, full `pytest`
suite 716 passed / 0 failed. See D-034 (`docs/DECISIONS.md`) for the
design decisions and two bugs found and fixed during this slice's own
testing. **Squash-merged as PR #39 (`d13ddb9`, `Closes #30`); issue #30
closed.** Local `main`/`origin/main` at `d13ddb9`.

**M8 Slice 2 — Read-Only Local AI Agent Foundation (#31) implementation
complete, PR not yet opened (2026-09-01).** Branch
`feat/read-only-ai-agent-foundation` from synced `main` (`d13ddb9`). Adds
a bounded local-AI agent: new `meyar.agent` package (`schemas.py` —
`AgentDecision`, the model's only output shape, `extra="forbid"`, mirrors
`PlannerDraft`'s discipline; `prompts.py`; `service.py` — the bounded
orchestration loop) plus a new `LLMProvider.decide_agent_action` method on
the existing `OllamaLLMProvider`. Exactly three read-only tools:
`search_candidates` forwards the model's own restated query, unmodified,
into the existing frozen `plan_and_search_candidates` pipeline (D-026/
D-027/D-031 guarantees reused as-is, not re-implemented) and loops back
for one more decision; `get_candidate_profile`/`get_candidate_evidence`
resolve a model-produced ordinal `candidate_ref` — never a raw
candidate_id — against the conversation's own server-held
`last_search_candidate_ids`, and always finalize the turn immediately
(found or not), so a small local model never gets a second, riskier
chance to freelance about an answer that's already complete. The model's
own `message` field is closing/clarifying framing text only — every
factual claim is rendered separately and deterministically from typed
tool-result data, never from model free text (D-035). New
`AgentConversation` model/table (migration `a1c5e9f2b6d3`), 1:1 with
`BrowserSession` (unique FK, cascade), so two human sessions never share
state and a fresh login always starts empty. New
`meyar.llm.concurrency` gives the previously-declared-but-unused
`Settings.inference_concurrency` its first real enforcement: one
process-wide semaphore shared by every `OllamaLLMProvider._chat` call
(extraction, identity, NL search planning, and the agent loop alike).
New `GET`/`POST /ui/agent` routes + `agent.html` template + "MEYAR AI"
nav entry (classic Search/Vacancies untouched). Candidate identity
(full name) is resolved only in the UI presentation layer, from
already-tenant-scoped tool results — never sent into the model's prompt.
A real, non-obvious bug was found and fixed during manual real-Ollama
verification — see D-035. Quality gates: `ruff` clean, `mypy src` clean
(134 files), `alembic heads` = one head, full `pytest` suite 756 passed
/ 0 failed, `scripts/scan-tracked-tree.sh` clean. **Opened as PR #40**
(CI green). Owner live inspection of PR #40 then found a contradictory
render — an empty assistant bubble, a red "AI response could not be
safely processed" error, a simultaneous green "query executed" banner,
and a misleading "Uyğunluq 0%" badge, all for one turn. Root-caused via a
real-DB repro test: the loop's follow-up "what next" decision failing
after `SEARCH_CANDIDATES` already succeeded was returned as
`MALFORMED_MODEL_OUTPUT` while still carrying the successful
`tool_results`. Fixed — see D-036: a new `ANSWERED_FROM_TOOL_RESULT`
outcome means a follow-up framing failure (or a successful
profile/evidence lookup, which never has model framing at all) is never
treated as fatal; `MALFORMED_MODEL_OUTPUT`/`AGENT_PROVIDER_FAILURE` are
now only ever returned with an empty `tool_results`; every stored
assistant turn is redisplayed through the same deterministic
outcome-\>text mapping the live turn uses, so it is never blank; the
agent's relevance-percentage pill is now shown only for
`SEMANTIC_ONLY`/`HYBRID` search modes (a plain `STRUCTURED_ONLY`
discovery query has no real score to show). 9 new regression tests (6
service-level, 3 HTTP-level rendering assertions). Quality gates
re-verified: `ruff` clean, `mypy src` clean, full `pytest` suite 765
passed / 0 failed (up from 756),
`alembic heads` unchanged (no migration — additive JSON turn shape only),
`scripts/scan-tracked-tree.sh` clean. Pushed to PR #40, CI re-verified
green. Owner conversational retest then found a product-level gap: a
follow-up like "birincinin təcrübəsini izah et" correctly resolved the
ordinal and fetched the right profile/evidence data, but the assistant
only ever repeated the generic D-036 fallback sentence while the UI
dumped the full structured profile below it — not a coherent
explanation. Fixed — see D-037: a new, narrow
`LLMProvider.synthesize_grounded_answer` call (used only after a
successful `GET_CANDIDATE_PROFILE`/`GET_CANDIDATE_EVIDENCE`) lets the
model produce natural-language prose from a small, bounded, indexed fact
list built from the candidate's own already-validated profile fields
(never raw CV text, never identity); the model's answer is independently
re-validated server-side before ever being trusted — every cited fact id
must have actually been supplied, and every number the answer states must
appear verbatim in those facts (the concrete guard against an invented
duration/skill-year count). A rejected or unavailable answer falls back
to the existing D-036 deterministic message exactly as before — never a
turn failure. `SEARCH_CANDIDATES` and all scoring/planner/navigation
behavior unchanged. 10 new regression tests (8 service-level — including
direct unit tests on the fact-builder and validator — 2 HTTP-level).
Quality gates re-verified: `ruff` clean, `mypy src` clean, full `pytest`
suite 775 passed / 0 failed (up from 765), `alembic heads` unchanged (no
migration), `scripts/scan-tracked-tree.sh` clean; the new `GroundedAnswer`
schema was independently spot-checked against a real local Ollama daemon
to rule out a repeat of D-035's `maxLength` failure mode (none found — a
slow response on this memory-constrained dev box, not a schema defect).
Pushed to PR #40. Owner factuality review then found D-037's free-text
`GroundedAnswer.answer` field could not prevent an unsupported
NON-numeric claim (e.g. "he managed a team" from a fact that only states
role/company/dates) — D-037's validation only checked cited fact ids and
numbers, never qualitative content. Fixed — see D-038:
`GroundedAnswer` is replaced by `GroundedSelection` (`used_facts` +
a single closed-enum `caveat`, no free-text field at all — `extra=
"forbid"` makes adding one a validation error); the model only selects/
orders which already-supplied facts are relevant, and
`render_grounded_answer` builds the entire displayed sentence
server-side from fixed per-category AZ templates applied to those facts'
own verbatim values — there is structurally no channel for an
unsupported claim (numeric or not) to appear, not merely a check that
usually catches one. Verified live against a real Ollama daemon (no
schema-crash regression) and rendered exactly as expected. 11 new/rewritten
regression tests (8 service-level including a structural schema test
proving the vulnerability class is closed, 3 HTTP-level). Quality gates:
`ruff` clean, `mypy src` clean, full `pytest` suite 778 passed / 0 failed
(up from 765), `alembic heads` unchanged (no migration),
`scripts/scan-tracked-tree.sh` clean. Pushed to PR #40. Owner-directed
autonomous acceptance run of the real 3-turn flow against a real local
`qwen3:1.7b` daemon then found two more real bugs neither visible to
`FakeLLMProvider`-based tests — see D-039: qwen3's default hidden-
thinking mode made every agent call ~5x slower, turning Turn 1's very
first decision into an outright `AGENT_PROVIDER_FAILURE` (fixed:
`think: false` on the agent decision/synthesis calls, ~34s cold-load
latency measured down to ~6.5s); and, once fast enough to reliably reach a second
"what next" decision, the loop had no guard against the model re-issuing
an identical `SEARCH_CANDIDATES` call, eventually co-rendering
`TOOL_CALL_LIMIT_EXCEEDED` above duplicated result blocks (fixed
structurally: an identical repeated query within one turn now finalizes
on the existing results instead of looping). A related prompt
clarification (`AGENT_PROMPT_VERSION` -> v2) fixed two routing gaps the
same real run surfaced: a general "experience" ask topic-filtering itself
to zero evidence matches, and a duration question about an
already-identified candidate being answered with `CLARIFY` echoing the
user's own question instead of using the D-038 evidence+caveat mechanism.
Re-verified end to end: all three turns of the real flow now produce a
single coherent grounded result each, and Turn 3 correctly states the
evidence does not prove a specific Python duration without ever deriving
"2021–2025 = 4 years" or substituting total experience for it. 2 new
regression tests for the thinking-disabled payload, 1 new + 1 rewritten
for the redundant-search guard. Owner scope-corrected: D-039's
`think: false` had been applied to every `OllamaLLMProvider._chat` call,
which reaches previously-accepted extraction/identity/planner AI
behavior (Slices 4/7/9) outside Slice 2. Fixed — see D-040:
`OllamaLLMProvider._chat` now takes an optional `think` parameter, sent
only by the two agent call sites (`decide_agent_action`,
`select_grounded_facts`); extraction/identity/planner calls omit the
`think` key entirely, byte-identical to their pre-D-039 request shape.
The target-hardware benchmark that would formally back a real-model
generation-parameter change like this is issue #36 (Slice 7, M9,
"Agent understanding" dimension) — not #20, whose current scope is the
broader Slice 13/M5 security + DoD acceptance issue (a target-hardware
benchmark execution is one gate inside it, not its whole purpose). 3 new
regression tests proving extraction/identity/planner requests omit
`think`. Re-verified end to end again: the real 3-turn flow is unaffected
by the narrowing (both prior fixes live only in the agent call sites).
Quality gates re-verified: `ruff` clean, `mypy src` clean, full `pytest`
suite 784 passed / 0 failed (up from 781), `alembic heads` unchanged (no
migration), `scripts/scan-tracked-tree.sh` clean. Pushed to PR #40.
**Still not merged — awaiting owner retest.**

GitHub remote established (`https://github.com/a-r3/meyar.git`, private,
temporary development remote — see D-012, `docs/DECISIONS.md`). `main`
bootstrap-pushed at `f8ac183`, then governance-merged at `16929fd`, then
Slice-6-merged at `55fef2d`. The governance PR included the required
root onboarding README and weekly low-noise Dependabot configuration for
backend `uv` and GitHub Actions dependencies; dependency auto-merge
remains disabled.

## Completed
- Preflight, fast docs pass, Claude Code harness.
- Backend scaffold: FastAPI + SQLAlchemy 2.0 async + Alembic + PostgreSQL.
- Slice 1 (Tenant + API Auth) — committed as `f5b4ec3`.
- Slice 2 (Job Criteria) — committed as `9ef3273`.
- Slice 3 (Candidate Upload) — committed as `56aca03`.
- Slice 4 (Profile Extraction) — committed as `c14a7ac`.
- Slice 5 (Evaluation Engine) — committed as `9d71cb6`. Fully
  deterministic, no LLM.
  `meyar.evaluation.evaluators` implements SKILL/CERTIFICATION/EDUCATION/
  LANGUAGE/EXPERIENCE per-criterion policy (exact normalized-string match
  + a small curated alias table — no fuzzy/embedding matching; deterministic
  year-extraction + overlap detection for EXPERIENCE, never LLM date math).
  `meyar.evaluation.policy` computes the overall fit band
  (`meyar-policy-v1` — see D-010 for the exact binding algorithm).
  Immutable `Evaluation` model (never updated in place — a new profile
  version, criteria version, or policy version always produces a new
  row) with exact provenance (candidate_profile_version_id,
  job_criteria_version_id, policy_engine_version). Tenant isolation
  enforced at the resolution step: `get_profile_version_by_id`/
  `get_criteria_version_by_id` are tenant-scoped lookups, so a
  cross-tenant id simply fails to resolve — `EvaluationInputError` before
  any row is created. Internal service only (`meyar.evaluation.service.
  evaluate_candidate`) + CLI (`meyar evaluate`) for Slice 5, per the
  "don't freeze an API contract Slice 6 will replace" guidance — no
  `POST /v1/evaluations` endpoint yet. `CriterionIn` gained an additive,
  optional `required_level` field for LANGUAGE proficiency requirements
  (backward-compatible — old stored criteria rows just default to
  `None`).
- Governance (M0) — `chore/git-governance` merged as `16929fd`.
  CI/hooks/PR-template/onboarding-README/Dependabot landed; organization
  branding removed from tracked docs; startup-protocol path checks made
  environment-neutral (`git rev-parse --show-toplevel`, no hard-coded
  absolute path). Issue #2 closed, milestone M0 closed.
- Slice 6 (Local CV Library & Folder Indexer) — merged as `55fef2d`
  (PR #7), associated with **M1 — CV Ingestion & Candidate Library**
  (now CLOSED), closed issue #6. New `FolderSource`/`FolderIndexedFile`
  models (Alembic migration `bd1b929cd874`), a symlink-safe recursive
  scanner (`meyar.ingestion.folder_scanner`), and an orchestration
  service (`meyar.services.folder_indexer_service.index_folder`) that
  reuses the existing secure ingestion pipeline unchanged — that
  pipeline itself was extracted into `meyar.services
  .candidate_document_service.ingest_candidate_document` so the
  direct-upload API route and the folder indexer share exactly one code
  path (no parallel ingestion architecture). SHA-256 content hash (never
  mtime) drives NEW/CHANGED/UNCHANGED/retry classification; re-scanning
  an unchanged folder creates zero duplicate Candidate/CandidateDocument/
  index rows (dedicated regression test); a changed file creates a new
  immutable CandidateDocument version under the same Candidate identity;
  a removed file is tombstoned (`MISSING`), never hard-deleted; a
  malformed file never aborts the rest of a scan; a previously FAILED
  file is retried on the next scan when unchanged. New CLI command
  `meyar index-folder --tenant-id --root` (distinct exit codes: 0 clean,
  1 completed-with-failures, 2 invalid source, 3 infra/DB failure),
  PII-safe output (counts/ids only). See D-013, `docs/DECISIONS.md`, for
  the exact removed/changed/retry/parse-failure semantics chosen.
- Slice 7 (Candidate Identity + Local Embeddings / Vector Index) — on
  `feat/candidate-identity-vector-index`, associated with **M2 —
  Candidate Search Intelligence**, closes issue #8. Two independent new
  flows: (1) `CandidateIdentityVersion` — an immutable, versioned,
  evidence-backed local extraction of full_name/email/phone
  (`meyar.extraction.identity_service`), wholly separate from
  `CandidateProfile` and read by no matching/evaluation/search/embedding
  code path; uses a new unredacted `build_identity_document_view`
  (Slice 4's professional view stays redacted) and its own
  `extra="forbid"` schema. (2) `CandidateEmbeddingVersion` — a local,
  pgvector-persisted embedding of deterministic `CandidateProfile`
  content only (`meyar.embedding.serializer
  .build_professional_embedding_text`, fixed field order, no evidence
  quotes, identity structurally unreachable), generated through a new
  `EmbeddingProvider` abstraction (`meyar.embedding.provider`) with a
  local-only `OllamaEmbeddingProvider` (loopback-enforced, shared
  `meyar.llm.loopback.require_loopback_url` helper) and a
  `DEV_INTEGRATION_MODEL` default (`nomic-embed-text` — not an approved
  production model). Idempotent by (profile version, provider, model,
  revision, serializer version, source hash) with a DB unique-constraint
  backstop — a serializer or source-text change never silently reuses
  an older vector; "current" is derived
  from the candidate's current `CandidateProfileVersion`, never a
  fragile flag, so a superseded profile's embedding is correctly STALE;
  a changed profile creates a new embedding without destroying prior
  history; a dimension-agnostic `vector()` column avoids locking in an
  unapproved production dimension. PostgreSQL switched to
  `pgvector/pgvector:pg16` (dev container recreated, volume preserved;
  CI image updated) — Alembic migration `7aae8da26969` enables the
  extension and creates both tables. New CLI commands `meyar
  extract-identity` (PII-safe output) and `meyar embed-candidate`
  (reused/idempotent reporting, never prints the vector). See D-014,
  `docs/DECISIONS.md`, for the exact semantics chosen. **Merged as
  PR #9 at `24b1d67`, closes issue #8.**
- Slice 8 (Hybrid Candidate Search) — on `feat/hybrid-candidate-search`,
  associated with **M2 — Candidate Search Intelligence**, closes issue
  #10. New `meyar.search` package: a strict `CandidateSearchRequest`
  (`STRUCTURED_ONLY`/`SEMANTIC_ONLY`/`HYBRID`, `extra="forbid"`) with
  required filters (hard eligibility gate) separated from preferred
  filters (soft ranking signal — `structured_score` = matched/total,
  0.0 when none configured, never a fabricated advantage). Structured
  filters (skills/certifications/languages/education/
  min_total_experience_years) evaluate the candidate's CURRENT
  `CandidateProfileVersion` only, reusing
  `meyar.evaluation.normalization`'s skill/text normalization rather
  than reimplementing matching; an explicit `as_of_date` is required
  whenever an experience-duration filter is set, so "present/ongoing"
  employment resolves deterministically instead of drifting with the
  wall clock. Semantic retrieval requires an explicit
  `EmbeddingSearchConfig` (provider/model_name/model_revision/
  serializer_version/embedding_dimensions) — only a candidate's current,
  exactly-compatible `CandidateEmbeddingVersion` participates (mirrors
  the Slice 7/D-014 provenance grouping; a stale or incompatible
  embedding is excluded, never mixed into one similarity ranking); the
  query text is embedded locally through the same `EmbeddingProvider`
  instance passed in, with an explicit provider/model/revision match
  check before use. `STRUCTURED_ONLY` never calls the embedding
  provider at all (regression-tested with a provider configured to
  raise if invoked). Hybrid ranking is deterministic (`meyar-search-v1`,
  see D-015): required filters gate eligibility before any semantic
  scoring — a failed required filter can never be overridden by high
  semantic similarity — and the full eligible+compatible set is scored
  before sort/limit, so a lower-semantic/higher-structured candidate can
  still outrank a high-semantic candidate under structured-favoring
  weights even with `limit=1` (both proved by dedicated merge-critical
  regression tests). Semantic similarity is normalized from pgvector's
  `cosine_distance` via `(cosine_similarity + 1) / 2`; results sort by
  relevance descending with candidate UUID ascending as the stable,
  non-PII tie-break. `CandidateIdentity` is never queried anywhere in
  `meyar.search` (regression-tested: identical profiles/embeddings with
  wildly different identity content produce identical rank/relevance).
  `CANDIDATE_SEARCH_EXECUTED` audit events carry only mode/counts/
  policy/embedding-config metadata plus a query SHA-256 — never raw
  query text, identity, CV text, or vector values. No new schema/
  migration — Slice 8 reads existing Slice 4/7 tables via two new
  read-only repository queries. New CLI command `meyar
  search-candidates --tenant-id --request-file <path>` (a JSON
  `CandidateSearchRequest`, no natural-language input), PII-safe output
  (candidate_id/rank/scores only). A small, explicitly documented Slice
  7 hardening: `OllamaEmbeddingProvider.embed` now also rejects an
  all-zero (zero-norm) vector, since cosine similarity is undefined for
  one and a genuine embedding of non-empty text is never all-zero. See
  D-015, `docs/DECISIONS.md`, for the exact ranking policy. **Merged as
  PR #11 at `412d978`, closes issue #10.**
- Slice 9 (Natural-Language Search Planner) — on
  `feat/natural-language-search-planner`, associated with **M2 —
  Candidate Search Intelligence**, closes issue #12. New strict
  `PlannerDraft`/`SearchPlanResult` contracts and deterministic
  `meyar-search-planner-v1` policy convert untrusted English/Azerbaijani
  HR text into the existing Slice 8 `CandidateSearchRequest`. Search mode
  is derived from validated contents; the LLM cannot supply tenant id,
  reference date, embedding provenance, ranking weights, SQL, identity,
  or scores. Existing protected-criterion policy runs before and after
  the LLM; its explicit Azerbaijani root-plus-allowed-suffix handling catches
  common protected inflections without prefix-matching unrelated words such
  as `yaşıl`. Explicit MVP fidelity guards preserve common Azerbaijani
  mandatory forms and reject skill-specific duration, language proficiency,
  identity, custom search weighting, invented numeric
  experience/result counts, and invented structured fields rather than
  weakening or partially executing meaning. Local Ollama remains
  loopback-only with strict JSON parsing and exactly one bounded repair retry.
  `plan_candidate_search`
  reads no candidate/search repository (its DB session is audit-only);
  `plan_and_search_candidates` is a thin plan→accepted Slice 8 delegate,
  with the hard-gate invariant proven end-to-end against real pgvector.
  `SEARCH_PLAN_CREATED`/`REJECTED`/`FAILED` audit events retain only the
  request SHA-256, safe versions/provenance/counts/reason codes — never
  raw request, semantic query, model output, identity, or CV data. New
  CLI: `meyar plan-search --tenant-id ... --query ... --as-of-date
  YYYY-MM-DD [--execute]`. No migration/dependency was added. See D-016.
- Slice 10 (JD 0–100 Scoring + Batch Ranking) — on
  `feat/jd-scoring-batch-ranking`, associated with **M3 — JD Matching &
  Ranking**, closes issue #14. Slice 5's wall-clock resolution of current
  employment is replaced by a required, persisted full
  `evaluation_as_of_date`; `meyar-policy-v1` meaning is retained because the
  change controls provenance rather than criterion semantics. New
  `meyar-score-v1` policy uses exhaustive status factors, textual float→
  Decimal conversion, declared weights only, final-only `ROUND_HALF_UP` to
  two decimals, and a typed zero-total failure. Immutable Evaluation rows gain
  nullable date/score/policy/explanation fields; exact scored provenance is
  reused with a PostgreSQL partial unique-index concurrency backstop, while
  historical rows remain NULL and unmodified. Structured explanations contain
  exact decimal strings and location-only evidence references—never identity,
  quotes, CV text, AI output, vectors, or semantic relevance. Batch ranking
  reads the tenant library directly, chooses exactly one current completed
  profile per active candidate, and sorts explicit fit tier → Decimal score →
  UUID integer; MUST_HAVE gating (including zero-weight MUST_HAVE) therefore
  dominates score. New CLI commands are the date-required `meyar evaluate` and
  `meyar rank-job`; no REST/UI surface or Slice 11 code was added. Migration
  `c0a4f2d8e317` (down revision `7aae8da26969`); see D-017.
- Slice 11 (Internal Chat UI + CV Library UI) — implemented on
  `feat/internal-chat-cv-library-ui`, associated with **M4 — Internal Product
  Interface & API**, closes issue #16 when owner-merged. FastAPI now exposes a
  separate server-rendered `/ui` router using explicitly auto-escaped Jinja2
  templates and repository-packaged local CSS (no Node/runtime CDN/analytics).
  Azerbaijani pages cover API-key login, natural-language candidate search,
  typed fail-closed planner outcomes, CV Library filters/pagination, tenant-safe
  candidate detail, presentation-only current identity, existing-job listing,
  and exact Slice 10 ranking/score/contribution display. Search delegates to
  `plan_and_search_candidates`; ranking delegates to
  `rank_candidates_for_job`; templates contain no policy formula or result
  sorting. New server-side `BrowserSession` stores only API-key id, SHA-256
  session-token digest, CSRF material, eight-hour expiry, and revocation state;
  every request refreshes the live API key and derives current tenant/scopes.
  Authenticated POSTs require constant-time CSRF validation. Cookie defaults are
  Secure/HttpOnly/SameSite=Lax/Path=/ui; local HTTP requires the explicit
  `MEYAR_UI_COOKIE_SECURE=false` override. All `/ui` responses carry restrictive
  CSP/no-sniff/no-referrer/frame-denial/no-store headers. Migration
  `e3b1f7a9c2d4` (down revision `c0a4f2d8e317`); see D-018.
- Slice 12 (REST API / Swagger / README Completion) — implemented on
  `feat/rest-api-openapi-completion`, associated with **M4 — Internal Product
  Interface & API**, closes issue #18 when owner-merged. `/api/v1` gained
  `POST /search`, `POST /search/natural-language`,
  `POST /jobs/{job_id}/criteria/{version_number}/score`,
  `POST /jobs/{job_id}/criteria/{version_number}/rank`, and
  `GET /candidates/{candidate_id}/detail` — all thin wrappers over the
  accepted Slice 5/8/9/10 services and the Slice 11 identity-enrichment
  helpers; no ranking/matching/policy logic duplicated in routes.
  `meyar.core.auth.get_current_tenant` now resolves the API key through
  FastAPI's `HTTPBearer`/`Security()` instead of a manual header read, so
  `/openapi.json` now declares a real `ApiKeyBearer` bearer security scheme
  with identical 401/403 observable behavior. External request/response DTOs
  (`meyar.schemas.api_search`, `api_evaluation`, `api_candidate`) are
  distinct from internal service schemas — `extra="forbid"` throughout, and
  trusted server config (`embedding_config`, structured/semantic weights)
  is injected server-side, never client-suppliable. `numeric_score` is
  always the canonical `ScoreExplanation` decimal string (e.g. `"75.00"`),
  never a bare Decimal/float; `evaluation_as_of_date` stays required with no
  wall-clock default; score/rank preserve exact idempotency/ordering from
  the underlying services. `/usage` now returns real tenant-scoped
  candidate/evaluation counts (`count_candidates_for_tenant`,
  `count_evaluations_for_tenant`) instead of hardcoded placeholder zeros.
  `/docs` is fully offline via the vendored `swagger-ui-bundle` package
  (zero CDN/runtime network dependency, verified by
  `test_openapi_contract.py`); `/ui/*` remains excluded from the OpenAPI
  schema. See D-019. Slice 13 final security/DoD acceptance was not started.

## Tests
**677/677 passing** as of the kind-aware duration-field UI pass (branch
`feat/hr-ui-productization`, still not merged): 673 prior (D-028 pass,
see below) + 4 new for D-029 — rendered-form assertions that the
"Minimum müddət (il)" control is disabled/cleared for a default SKILL
row and enabled for an EXPERIENCE row, that a kind-switched EXPERIENCE→
SKILL submission neither echoes the stale value back as editable nor
persists a `Job`, and a direct manual POST of SKILL + `min_years`
(bypassing the client script entirely, equivalent to JavaScript
disabled) is still rejected server-side with no `Job` created.

**673/673 passing** as of the Job lifecycle pass (branch
`feat/hr-ui-productization`, still not merged): 659 prior (D-027 pass,
see below) + 14 new for D-028 — the migration backfill-to-ACTIVE test
(fresh throwaway DB, pre-migration schema, `test_ui_migration_packaging.py`),
and `test_ui_job_lifecycle.py`'s full coverage: default-active/archive
listing, archive route auth/CSRF/tenant-isolation/cross-tenant-denial, no
hard delete, archived-job evaluation-history title resolution, identical-
active-duplicate rejection, same-title-different-criteria allowed,
archived duplicate not blocking a new active job, a genuine concurrent-
double-submit test against the real partial-unique-index DB constraint
(not just the application-level pre-check), and an API-path regression
proving `POST /api/v1/jobs` is unaffected.

**659/659 passing** as of the semantic-correctness audit pass (branch
`feat/hr-ui-productization`, still not merged): 639 prior (D-026 pass,
see below) + 20 new/changed for D-027 — the skill-specific-duration
never-weakens-to-total-experience matrix (locative/ablative suffix,
üzrə/ilə connector, ASCII vs diacritic, the "ən az" precheck gap),
extraction-for-clarification and Java/JavaScript-boundary tests
(`test_search_planner_policy.py`), the reversed "təcrübəsi ... N il"
word-order fast-path pattern and its tenant-isolation/audit coverage for
the confirmed clarification alternative (`test_search_deterministic_parser.py`),
the end-to-end clarification-then-confirm UI flow with zero LLM calls
(`test_ui_routes.py`), and the vacancy kind-aware min_years rejection
(`test_ui_job_creation.py`).

**639/639 passing** as of the deterministic-fast-path pass (branch
`feat/hr-ui-productization`, still not merged): 607 prior (D-025 pass,
see below) + 32 new for D-026 — the full parser regression matrix and
full-pipeline (zero-LLM-call, valid-plan, real-execution, tenant-isolation,
safe-audit) coverage in `test_search_deterministic_parser.py`, plus 6
existing LLM-failure/repair-path tests updated to use query text that
stays genuinely outside the new fast path's scope (their purpose —
testing malformed/timeout/outage LLM handling — is otherwise now
short-circuited by the fast path, which is the intended product
improvement).

**607/607 passing** as of the third-round PR #29 visual-inspection pass
(branch `feat/hr-ui-productization`, still not merged): 600 prior (D-024
pass, see below) + 7 new/changed for D-025 — the model-self-decline
marker and its distinct HR message (`test_search_planner_policy.py`,
`test_ui_routes.py`), the single-"Tələb"-field form regression
(`test_ui_job_creation.py`), and the two Blocker-A root-cause acceptance
tests: a UI-created SKILL criterion resolving against real candidate
evidence, and UI-created vs. API-created criterion structural equivalence
(`test_ui_job_creation.py`).

**600/600 passing** as of the second-round PR #29 visual-inspection pass
(branch `feat/hr-ui-productization`, still not merged): 582 prior (D-023
pass, see below) + 18 new regressions for D-024 — the diacritic-fold and
locative/ablative-suffix planner-policy fidelity fixes plus a
Java/JavaScript non-regression case (`test_search_planner_policy.py`), an
end-to-end ordinary-Azerbaijani-query executable-outcome test
(`test_ui_routes.py`), the ranking-table human-label/kind test
(`test_ui_routes.py`), the CV-preview XSS-escaping regression
(`test_ui_candidate_preview.py`), the original-CV always-attachment
disposition test (`test_ui_original_cv.py`), and 11 new vacancy-creation
tests covering auth/CSRF/tenant-isolation/validation/successful-creation/
rankability (`test_ui_job_creation.py`).

**582/582 passing** as of the M7 HR UI productization pass (branch
`feat/hr-ui-productization`, not yet merged): 537 prior (Slice 14 merge
baseline, see below) + regression coverage added for D-023 — the
control-character normalization fix (`test_search_planner_policy.py`),
the demo-key rotation fix (`test_demo_seed.py`), the new CV-preview route
(`test_ui_candidate_preview.py`), and the UI date-boundary/information-
boundary changes (`test_ui_routes.py`).

**537/537 passing** as of Slice 14 merge (PR #24, squash `f6e31ff`): 511 prior
(post-Slice-13-merge baseline) + 26 Slice 14 regressions across two passes —
`test_folder_indexer.py` (8 new: file-stability skip/later-processing,
unstable-existing-file-not-tombstoned, cross-path exact-content dedup,
cross-tenant dedup isolation, no-merge-on-different-content, folder-path
oversized/`_MAX_PAGES` limits), `test_folder_reconciliation.py` (14: fresh
PDF/DOCX reconciliation produces profile/identity/embedding, resulting
candidate is searchable end-to-end, unchanged-reconciliation idempotency
(no duplicate Candidate/downstream versions), per-candidate isolation on
extraction failure, embedding-failure-then-safe-retry, `--limit`
bounding, PII-safe audit output, full `reconcile_folder` restart-safe
flow, plus a focused post-audit hardening pass: a changed-CV end-to-end
regression proving stale search state is never used, and a
`--limit`-fairness/anti-starvation regression), `test_folder_reconciliation_cli.py`
(4: happy path, invalid root exit 2, downstream-failure exit 1, `--limit`
flag). The breakdown below predates Slice 13/14 and is retained as
historical record of Slices 1–12; it has not been backfilled for Slice 13's
own test additions
(`test_no_exfiltration.py`, `test_ui_original_cv.py`,
`test_e2e_synthetic_mvp.py`, `test_multilingual_evidence.py`,
`test_audit_privacy_guard.py`, `test_candidate_documents.py` additions —
see PR #22).

473/473 passing (430 prior + 43 Slice 12 search/NL-search/evaluation/
ranking/candidate-detail/usage/OpenAPI-contract regressions). Slice 12
focused coverage proves: `STRUCTURED_ONLY` search never invokes the
embedding provider; all 7 typed `PlannerOutcome`s are reachable and
distinguishable via `POST /api/v1/search/natural-language` with no
fallback search on a non-executable outcome; genuine embedding/DB
infrastructure failure returns 503, distinct from the typed 200
`PLANNER_PROVIDER_FAILURE` outcome; a client cannot smuggle
`embedding_config`/weight overrides into a search request (422 via
`extra="forbid"`); repeated identical score requests reuse the same
`evaluation_id` (`reused=true`); `numeric_score` serializes as a
two-decimal string; batch rank response order exactly matches backend
order with no route-level re-sort; cross-tenant 404 on score/rank/detail;
and `/openapi.json` declares the Bearer scheme, excludes `/ui/*`, and has
unique operationIds.

430/430 passing (395 prior + 35 Slice 11 browser-session/auth/CSRF/UI/
library/search/ranking/XSS/header/packaging/migration regressions).
Slice 11 focused coverage proves valid/generic-invalid login, hashed session
tokens, secure cookie policies, fixation prevention, session/key expiry and
revocation, live scope enforcement, CSRF success/failure before domain work,
actual Azerbaijani POST→Slice 9→Slice 8→escaped HTML, prohibited/unsupported/
ambiguous/malformed/provider-failure no-search behavior, planner-outage
independence for library/detail, bounded library pagination and current-profile
authority with no stale fallback, direct cross-tenant candidate/criteria 404,
presentation identity and XSS escaping, backend ranking-order preservation,
UNKNOWN/INSUFFICIENT_EVIDENCE/manual-review language, security headers, local
asset/resource packaging, and real PostgreSQL migration upgrade→downgrade→
re-upgrade.

The prior 395 include 353 through Slice 9 plus 42 Slice 10 scoring/evaluation/
persistence/batch/CLI regressions.
Slice 10 coverage includes the six exhaustive status factors; exact Decimal
boundaries, unequal weights, textual float conversion, repeating-fraction and
half-cent `ROUND_HALF_UP`; zero total and mixed zero/positive validation;
UNKNOWN vs NOT_MATCHED explanation meaning and exact recomputation; persisted
date/score/policy/explanation; historical NULL rows and partial uniqueness;
same-input reuse plus date/profile/criteria-version changes; explicit 2026 →
2027 → repeated-2026 current-employment proof; fit-tier dominance;
zero-weight MUST_HAVE gating; manual-review tier; current-profile-only batch;
UUID tie/order stability under reversed repository results; identity
invariance; tenant isolation; zero/skip candidate sets; PII-safe audits/CLI;
and static no-AI/vector/search/identity dependency checks. Migration
`c0a4f2d8e317` was also exercised independently as pre-Slice-10 upgrade →
legacy-row insert → upgrade → downgrade → re-upgrade, preserving NULL score
provenance and unchanged criterion JSON throughout.

The prior 353 include 231 pre-Slice-9 tests, 102 Slice 9 planner/policy/
service/CLI tests, and 20 shared Azerbaijani normalization/protected-policy
regressions.
Slice 9 coverage includes strict draft parsing, deterministic mode and
draft-to-request conversion, Azerbaijani/English intent, unsupported semantic
weakening and custom weighting, explicit Azerbaijani mandatory/protected
morphology without broad prefix matching, protected-criteria pre/post checks,
bounded repair, provider/result provenance, trusted date/embedding/weight
injection, audit privacy, CLI exits,
structured-only no-embedding execution, and a real-pgvector hybrid hard-gate
integration path. The prior 231 tests include 177 through Slice 7 plus 54
Slice 8 tests — 17 structured + 17 semantic +
10 hybrid + 1 zero-norm-vector hardening regression on
`OllamaEmbeddingProvider` + 9 post-acceptance-audit provenance/source-
hash-freshness regressions, `test_search_semantic_provenance.py`, see
D-015's correction note). Deterministic policy unit tests
(no DB, no LLM — `test_evaluation_policy.py`): skill match/absent-is-
unknown/case-normalization/Java-never-equals-JavaScript/alias
normalization, certification match/absent, education match/unsupported,
language explicit-level-match/present-without-level-is-partial/absent/
no-level-required, experience sufficient/insufficient/ambiguous-dates-
require-review/no-history-is-unknown/overlapping-dates-conflict/correct-
summation, configured manual-review flag forces review even on MATCH,
evidence carried through unmodified, policy deterministic on repeated
identical input, overall-result algorithm (all 4 bands + manual-review/
conflict precedence + preferred-ratio threshold), no hidden criteria.
DB-integration tests (`test_evaluation_engine.py`): exact version
provenance persisted, immutability (new profile version → new
evaluation, old one unchanged), new criteria version → new evaluation,
cross-tenant candidate+job mixing rejected (`CRITERIA_VERSION_NOT_FOUND`),
tenant B cannot retrieve tenant A's evaluation, wrong-tenant profile id
rejected even though it exists, profile belonging to a different
candidate rejected, criterion-result count matches configured criteria
exactly (no hidden criteria), repeated identical evaluation
deterministic, no identity/protected fields reach the policy engine.
`uv run ruff check .` and `uv run mypy src` are clean. Whole-repository
`uv run mypy .` retains known test-only type debt.

Slice 6 (`test_folder_indexer.py`, `test_folder_indexer_cli.py`, 22
tests): empty folder, valid PDF/DOCX discovery+import, nested-folder
relative-path normalization, unsupported extension ignored, case-
insensitive extension, unchanged-rescan idempotency (zero duplicate
Candidate/CandidateDocument/index rows), changed-file new-version-same-
candidate with prior evidence preserved, removed-file MISSING then
unchanged-reappearance reactivation, malformed-PDF (parse-stage) and
malformed-DOCX (validation-stage) isolation without aborting the scan,
retry-of-previously-FAILED-file then fixed, SHA-256 correctness, source-
root-escape/symlink prevention (file and directory symlinks), invalid/
non-directory source root, cross-tenant isolation (same folder path
scanned by two tenants never shares rows), PII-safe audit metadata, and
CLI happy-path/invalid-root/exit-code-on-failure with no filename in
output.

Slice 7 (`test_candidate_identity.py`, `test_candidate_embedding.py`,
`test_identity_embedding_cli.py`, 50 tests): identity — full_name/email/
phone extraction with real evidence verification, missing field stays
null, strict extra-field rejection, invalid/mismatched evidence
rejected, fabricated-quote rejected, bounded retry (fail-then-succeed
and fail-twice), provider unavailable/timeout handled safely,
re-extraction creates v2 and leaves v1 immutable, tenant isolation,
audit metadata and logs contain no PII, identity view proven unredacted
vs. the professional view proven redacted for the same document.
Embedding — serializer determinism (same content → same text → same
SHA-256) and identity exclusion, first-embed creates a record, identical
rerun is idempotent (provider called exactly once across two calls),
reuse/uniqueness identity independently proven to require all seven
provenance fields — a serializer-version bump, a same-hash-different-
serializer case, and a same-serializer-different-hash case each
correctly produce a distinct embedding and actually re-invoke the
provider, never silently reusing a stale vector (the exact scenario an
acceptance audit reproduced pre-merge — see D-014); DB unique-constraint
backstop independently proven for both the accept and reject sides, new
profile version makes the prior embedding provably STALE (absent from
the current-version lookup) while old history is preserved, different
model/dimension produces distinct non-mixed provenance, provider error
persists no row, tenant isolation, audit metadata contains no vector/
text/PII, plus direct
`OllamaEmbeddingProvider` HTTP-behavior tests via `httpx.MockTransport`
(no real Ollama): loopback rejection/acceptance, valid response, empty/
missing/NaN/non-numeric vector rejected, non-200 and connect-error and
timeout handled. CLI — `extract-identity` PII-safe happy path and
not-found case, `embed-candidate` happy-path-then-reused (provider
called once) and no-profile exit code 2. Plus one Slice 8 hardening
regression added to this file: `OllamaEmbeddingProvider` rejects an
all-zero (zero-norm) embedding vector.

Slice 8 (`test_search_structured.py` 17, `test_search_semantic.py` 17,
`test_search_hybrid.py` 10 — 44 tests, all against real pgvector, no
mocked vector distance): structured — no-filter bounded result, required
skill match/non-match/multiple-required-all-must-match, preferred skill
score affecting rank, certification/language/education filters,
experience-threshold filter, `as_of_date`-deterministic "present"
employment (repeated identical search same result), missing `as_of_date`
rejected when an experience filter is set, protected/sensitive term
rejected in both filter values and semantic query, current-profile-only
search (stale v1 skill no longer matches after v2 supersedes it,
result correctly references v2), `STRUCTURED_ONLY` never invokes the
embedding provider (proven with a provider configured to raise if
called), stable candidate-UUID tie-break, tenant isolation. Semantic —
cosine-similarity ranking order, score normalization bounded [0,1],
top-N limit, stale-profile embedding excluded then re-included once a
compatible v2 embedding exists, incompatible provider/model/model-
revision/serializer/dimension each independently excluded (never mixed
into one ranking), candidate with no compatible embedding excluded,
invalid query-vector dimension rejected, zero-norm query vector
rejected, provider error propagates safely, tenant isolation, vector
values never present in the response, raw query text never present in
the audit event (only a SHA-256), embedding-provider/config mismatch
rejected. Hybrid — the two merge-critical regressions:
**hard-constraint gate never bypassed by semantic similarity** (a
candidate failing a required skill with near-perfect semantic similarity
is excluded entirely; the passing, lower-similarity candidate is
returned) and **no premature semantic top-k** (a candidate with a lower
semantic score but a perfect preferred-structured score correctly
outranks a candidate with a near-perfect semantic score under
structured-favoring weights, even with `limit=1`); plus preferred-score-
affects-rank, weights-must-sum-to-1.0 validation, the exact deterministic
weighted-sum formula reproduced from returned scores, candidate lacking
a compatible embedding excluded from hybrid, stable tie-break, repeated-
identical-search determinism, **`CandidateIdentity` proven not to affect
rank/relevance** (two candidates with identical profiles/embeddings but
wildly different identity content produce identical results), and
explanation components (matched required/preferred filter labels)
proven to match the actual score calculation.

Post-acceptance-audit correction (`test_search_semantic_provenance.py`,
9 tests — see D-015's correction note): an independent acceptance audit
of the initial Slice 8 implementation reproduced two defects before
merge, both fixed on this same branch/PR. (1) The service validated
only the `EmbeddingProvider` object's declared static attributes
against `embedding_config`, never the actual `EmbeddingResult`'s own
provider/model_name/model_revision — fixed, and regression-tested with
a provider whose declared attributes match config but whose `.embed()`
result claims a different provider/model/revision (same dimensions),
each independently rejected as `EMBEDDING_RESULT_PROVENANCE_MISMATCH`;
a matching-provenance case is also tested to prove the fix isn't
over-strict. Two non-finite (NaN/±Inf) query-vector regressions prove
the search boundary itself validates the vector, not just
`OllamaEmbeddingProvider`. (2) `search_compatible_embeddings` selected
among multiple same-profile/same-config embedding rows (differing only
by `source_sha256`, a state Slice 7 explicitly allows) by arbitrary/
unordered SQL row-return order rather than by the current canonical
serialization — independently proven (during the audit) to flip between
the current and a stale embedding purely by reversing insertion order.
Fixed by recomputing each eligible candidate's current canonical
`source_sha256` at search time and requiring an exact
`(candidate_profile_version_id, source_sha256)` match; regression-tested
for insertion-order independence (both orders select only the current
hash, identical scores), the current hash being entirely absent
(candidate excluded, never falls back to a stale hash), and three
coexisting historical hashes (only the current one participates, the
candidate appears exactly once).
`uv run ruff check .` and `uv run mypy src` are clean (95 source files).

## Live synthetic smoke
**PASS.** Per Slice 5 spec §25, no live Ollama call required (Slice 4
already verified that integration) — used a synthetically constructed
CandidateProfileVersion (bypassing the LLM entirely) against a 3-criterion
JobCriteriaVersion (2 MUST_HAVE: Python skill + 3yr min experience; 1
PREFERRED: AWS certification). Result: both MUST_HAVE criteria MATCH
(skill found; experience computed as 4 years from "2021-2025",
correctly exceeding the 3-year requirement) with evidence correctly
carried through from the profile; PREFERRED certification correctly
UNKNOWN (absent, not penalized as failure); overall result
`POTENTIAL_MATCH` (all must-haves satisfied, 0% preferred coverage <
50% threshold) — exactly matching the documented D-010 algorithm.
Evaluation's persisted `candidate_profile_version_id`/
`job_criteria_version_id` verified to equal the exact input versions.

## In progress
**M8 Slice 4 — Agent Product UX & JD Matching (#33)**: implementation
complete on `feat/agent-product-ux-jd-matching` (from synced `main`
`8c1782f`), PR #42 opened against `main`, **corrected per three rounds of
owner UI review (D-043 functional, D-044 presentation, D-045 turn-render
consistency/copy/evidence-attribution/composer/criteria/ranking/
unsupported-requirement contract), then a fourth pass (D-046) closing the
two remaining acceptance blockers — a real-Ollama JD-requirement
grounding defect (an HR-facing "unsupported requirement" disclosure with
no check that the requirement was ever actually in the JD text) and an
independently-verified explanation of the 850/846/848 historical
test-count question (822 → 838 → 846 → 846 → 848 → 851, zero test
removals at any commit in this branch's lineage; no 850 anywhere in this
repository's git/reflog/PR history) — and, at the time of that pass, still
awaiting owner re-review before merge. PR #42 was subsequently, after the
further corrective passes recorded below (through D-065), accepted and
**MERGED** at squash SHA `584eb3584f10abf13db03046d32f85041b4df2ab` — see
"Current phase" above.** D-045 fixes a genuine root-cause bug (a search/tool-result
turn's live headline and its persisted history text were two
independently computed values — see D-045 item 1), a real string-
duplication bug in education-title formatting, and search evidence that
was not actually attributable to the requirement it was shown under; adds
a narrow `JDDraftCriterionKind.OTHER` escape hatch so a real, non-
sensitive, evaluator-unsupported JD requirement (e.g. relocation
willingness) is disclosed and survives confirmation onto the ranking page
as an explicitly "informational, not scored" notice instead of being
silently dropped or misclassified into a scored criterion; and de-noises
the composer/criteria-review/ranking presentation. See D-045 for the full
per-item breakdown.
MEYAR AI is now the primary post-login HR surface (`_finalize_human_login`
redirects to `/ui/agent`; top nav — and the brand/logo link, and every
page's own "home" link — is exactly `MEYAR AI | Namizədlər | Çıxış`, no
secondary nav row at all any more (D-044): **neither Vacancies (D-043)
nor classic search (D-044) is a normal HR nav/discovery destination**;
`/ui/jobs`, `/ui/search`-family routes remain fully functional as
backend/supporting capability, reachable by direct URL only). **D-044
also unifies the composer** (one mode `<select>` — "Adi söhbət" /
"Vakansiya elanını analiz et" — plus one send button, JS-guarded against
an empty submit instead of the browser's native English validation
popup), **restructures the conversation feed so each turn's user message,
assistant explanation, and result cards render as one block with the
composer strictly after it** (previously the composer sat between
history and the live turn's own results), **replaces developer-taxonomy
strings** (`"skill: Python"` → plain "Python"; evidence citations →
`"CV, səhifə N — "quote""`, deduplicated), and **gives three tool types
their own deterministic, evidence-grounded headline sentence** instead of
the generic "Nəticələr aşağıdadır." filler.
New `AgentActionType.DRAFT_JOB_CRITERIA`: HR pastes/describes a JD, a new
bounded `LLMProvider.draft_job_criteria` call drafts a structured criteria
set restricted to the manual form's five `CriterionKind`s, every item is
re-validated into a real `CriterionIn` (same denylist/schema as the
manual form and REST API). **D-043 correction: an item that fails that
check is never silently dropped** — a non-sensitive failure is disclosed
verbatim in the review (`AgentJobDraftToolResult.unsupported`); a
sensitive/prohibited match is a safe count only
(`prohibited_count`, matched text never redisplayed). **D-043 also adds a
first-class deterministic JD entry point**: the composer's "Vakansiya
elanını analiz et" mode (a `<select>` option since D-044, originally a
second submit button) submits `intent=draft_job_criteria`, which makes
`run_agent_turn(..., explicit_action=AgentActionType.DRAFT_JOB_CRITERIA)`
skip `llm.decide_agent_action` entirely for that turn — no model call, no
routing ambiguity, immune to the qwen3:1.7b misrouting limitation below.
The review form (new shared `_criteria_rows.html` macro, also now used by
`job_new.html`) posts through the existing `POST /ui/jobs` — nothing
persisted until HR confirms, and **confirming from the agent's own review
form now lands directly on the ranking result** (new shared
`meyar.ui.router._render_job_ranking`, same `rank_candidates_for_job` call
the manual "Namizədləri sırala" action already used — no new scoring
authority) instead of a bare redirect to the de-emphasized jobs list; the
unchanged manual `/ui/jobs/new` path still redirects to `/ui/jobs` as
before. Conversation UX consolidated: one deterministic server-computed
`headline` replaces the previous two-tier duplicate status-banner stack;
new `POST /ui/agent/reset` ("Yeni söhbət") clears only the caller's own
session-scoped `AgentConversation`. The semantic-similarity pill is
relabeled "Semantik yaxınlıq %" and consistently hidden outside
SEMANTIC_ONLY/HYBRID search modes on both `/ui/search` and `/ui/agent` —
the real 0–100 deterministic score stays exactly where it already lived
(`ranking_results.html` only). D-031's fast-path sunset condition is NOT
acted on this slice — that owner-evaluated judgment is left to the
accompanying UI review, per issue #33/D-031 point 3.
Real-Ollama acceptance testing (qwen3:1.7b, same model as D-039/D-040)
found and fixed a genuine defect: the model can echo
`AGENT_SYSTEM_PROMPT`'s own instruction text verbatim into a CLARIFY/
FINAL_ANSWER message — fixed structurally with
`meyar.agent.service._looks_like_prompt_leak` (exact containment check,
not a heuristic), reusing the existing bounded-retry-then-
`MALFORMED_MODEL_OUTPUT`-fallback path; confirmed live post-fix that a
repeat leak is rejected and only the safe fallback text renders. The same
testing also surfaced a known, documented model-quality limitation (not
a code defect): qwen3:1.7b sometimes still routes a raw pasted JD to
SEARCH_CANDIDATES instead of DRAFT_JOB_CRITERIA when relying on
conversational routing alone despite a sharpened disambiguation prompt
(`AGENT_PROMPT_VERSION` -> `agent-orchestrator-prompt-v3`) — **the D-043
explicit `intent` affordance is the product answer to this limitation,
not a further prompt change**; both branches of the conversational path
stay fully safe when misrouting happens (a misrouted JD's own
search-planner call fails typed/non-fabricating, never silently produces
a wrong result), and `draft_job_criteria` itself was independently
verified end-to-end against the real model, including through the full
validation/dispatch pipeline, producing a correctly structured,
denylist-clean draft; the D-044 presentation pass was independently
re-verified the same way (live browser session against real
`qwen3:1.7b`, not only `FakeLLMProvider` fixtures) — see D-044 for the
exact screenshots/assertions. Quality gates (D-044 pass): `ruff` clean,
`mypy src` clean (136 files), `alembic heads` unchanged (no migration —
purely additive schema/service/template layer), full `pytest` suite
passed, `scripts/scan-tracked-tree.sh` clean.
D-045 quality gates: `ruff` clean, `mypy src` clean (136 files),
`alembic heads` unchanged (single head, still `a1c5e9f2b6d3` — no
migration), full `pytest` suite passed (848 passed, +2 new tests vs
D-044's 846, none deleted/weakened), `scripts/scan-tracked-tree.sh`
clean.
D-046 quality gates: `ruff` clean, `mypy src` clean (136 files), `alembic
heads` unchanged (single head, still `a1c5e9f2b6d3` — no migration), full
`pytest` suite passed (851 passed, +3 new tests vs D-045's 848 — the
JD-grounding unit test plus two end-to-end fabricated-requirement
regressions — none deleted/weakened), `scripts/scan-tracked-tree.sh`
clean. A fifth pre-acceptance P0 pass (D-048) closes two reproduced
candidate-factual authority defects without changing JD grounding,
duration arithmetic, deterministic scoring, or the API: accepted
professional facts now require claim-specific support from their own
verified evidence, and unrestricted model prose has been removed from
`FINAL_ANSWER`/`CLARIFY` in favor of closed response codes plus
server-owned rendering. Legacy persisted assistant prose is ignored unless
it carries the new server-authority marker. D-048 quality gates: focused
suite 216 passed; `ruff` clean; `mypy src` clean (136 files); full `pytest`
887 passed; Alembic remains at the single `a1c5e9f2b6d3` head; tracked-tree
scan clean. See D-042, D-043, D-044, D-045, D-046, D-048.
A sixth pre-acceptance P0 pass (D-049, issue #44) closes the remaining
candidate-factuality authority escapes reproduced against `86d3e3f`:
centralized deterministic contradiction handling now covers every
professional fact type; linked skill/domain experience must attribute its
employment context in the same accepted span; all professional-profile
consumers revalidate legacy `COMPLETED` rows against current canonical
evidence; identity values are attributable to their own evidence; and JD
draft titles/evidence topics can no longer become model-authored trusted
assistant headings. Unsupported legacy facts and cached evaluations are
presented as unavailable without rewriting history. The safe-but-product-
degrading whole-profile failure behavior is unchanged. D-049 gates:
focused suite 186 passed; `ruff` clean; `mypy src` clean (138 files); full
`pytest` 910 passed; Alembic remains at `a1c5e9f2b6d3`; tracked-tree scan
clean. At the time of this pass PR #42 remained open and not accepted;
nothing had yet been pushed or merged. See D-049.

The independent re-audit at `7b748f4` disproved complete closure. D-050
records the corrective canonical-context, current-state, period-attribution,
embedding, identity-token, history-version and fixture-integrity changes.
At the time of this pass PR #42 remained not accepted, local only, with no
push or merge. (Both D-049 and D-050 predate the eventual PR #42 merge —
see "Current phase" above.)

**M8 Slice 3 — Evidence Capability Completion (#32)**: **MERGED as PR #41
(`8c1782f`, squash); issue #32 closed.** Closes the D-027-identified
`SkillItem`/`EmploymentItem` grounding gap: `CandidateProfileExtraction`
gains optional `skill_experience` (skill <-> attributable employment-period
grounding) and `domain_experience` (explicit sector/domain evidence,
never inferred from an employer name — see `meyar.core.domain_terms`)
lists, two new deterministic evaluators (`SKILL_EXPERIENCE`/
`DOMAIN_EXPERIENCE` criterion kinds) that compute duration only from
attributable, date-parseable periods (merging overlaps, never
double-counting), and agent evidence/fact surfacing for both. No
migration (`profile_content`/`criteria` are JSON columns; both new lists
default to empty, old rows validate unchanged). An owner final-review pass
caught and fixed a real bug before merge: a skill/domain claim's duration
was being computed from its LINKED EMPLOYMENT ENTRY's full period rather
than its own evidence-backed sub-interval (e.g. a 5-year job with only
6 months of attributed Java evidence was wrongly becoming "5 years
Java"). Fixed by giving `SkillExperienceItem`/`DomainExperienceItem` their
own `start_date`/`end_date`/`is_current` — `employment_index` is now
context/provenance only, never a duration source. Extraction prompt
bumped again to `candidate-profile-extraction-v3`. Also confirmed (not a
regression, pre-existing/unchanged): no automatic reprocessing exists for
already-COMPLETED profiles on a prompt-version bump; the operator-only
CLI `meyar extract-profile <tenant> <candidate> <document>` remains the
only way to retroactively backfill this grounding onto a pre-Slice-3
candidate — until then it safely reports UNKNOWN, never a fabricated/
inherited duration. A second owner review found a further real gap: a
verbatim, real evidence quote proved a skill/domain was mentioned but
never proved the SPECIFIC start_date/end_date claimed for it — a model
could cite a real quote and still claim an arbitrary broader interval
(e.g. an unrelated employment entry's own dates line). Fixed with a new
deterministic (non-LLM) `meyar.core.interval_terms.
interval_grounded_in_quotes` check at extraction-verification time: each
claimed year must literally appear in that item's own cited quotes
(skill also requires the skill name itself to co-occur). Confirmed
open/current grounding remains fully deterministic via the explicit
`evaluation_as_of_date` parameter, unaffected by this check. A third
owner review then found that check was itself still insufficient: it
joined ALL of an item's evidence quotes before checking subject/year
presence, so a subject in one real quote plus unrelated dates in a
second real quote wrongly passed (reproduced live before fixing: exactly
this shape returned `True` pre-fix). Fixed to check PER-QUOTE — one
single evidence quote must contain both the subject and every claimed
year together, never combined across separate quotes; `subject_terms`
generalized so domain reuses its existing curated-synonym set
(`meyar.core.domain_terms.accepted_terms_for_domain`) in the same
relational check. Five new regressions match the owner's five required
cases exactly, including that multiple independently-grounded periods
still aggregate correctly. See D-041.

**M8 Slice 2 — Read-Only Local AI Agent Foundation (#31)**: **MERGED as
PR #40 (`c430518`, squash); issue #31 closed.** M8 Slice 1 (#30) merged as
PR #39 (`d13ddb9`). Slice 14 remains the most recently merged non-M8
product-Slice work before the M8 pivot.

**Chore (issue #25, not a Slice):** pre-presentation readiness and local
demo bootstrap — **MERGED as PR #26 at squash SHA `a539e34`** (see D-022).

**Chore (issue #27, not a Slice, M7):** HR UI productization and
presentation readiness, on `feat/hr-ui-productization` —
**MERGED as PR #29 at squash SHA `1f8bd12`; issue #27 closed.**
Following owner visual inspection of the running local `/ui/*` surfaces,
reworked navigation/copy into HR language, removed the manual
evaluation-date inputs (current date now injected explicitly at the UI
boundary), root-caused and fixed the `REQUEST_CONTROL_CHARACTERS`
false-positive on ordinary textarea input, decluttered candidate/vacancy/
ranking screens (raw UUIDs and pipeline-status internals no longer shown
on the primary HR pages), added a truthful in-app CV preview route
alongside the original-file download, resolved job titles into evaluation
history, and fixed the `meyar seed-demo` key-rotation gap (idempotent
reseed now always returns a usable, single active credential). See D-023.

A second owner visual inspection of PR #29 found seven further blockers,
all fixed on the same branch/PR (see D-024): (1) root-caused and fixed —
without requiring live Ollama as an acceptance dependency — two remaining
deterministic-fidelity-check regex gaps (Azerbaijani ASCII/diacritic
typing variance, and agglutinative locative/ablative case suffixes) that
were still rejecting ordinary Azerbaijani skill+experience queries after
D-023's control-character fix; (2) added `/ui/jobs/new` +
`POST /ui/jobs` — HR can now create a vacancy (title, MUST_HAVE/PREFERRED
criteria, no raw id/UUID entry) from the UI for the first time, reusing
the exact `POST /api/v1/jobs` domain services; (3) ranking table now shows
the criterion's HR label instead of its raw internal id/kind enum;
(4) softened remaining developer-oriented ranking/jobs-page wording;
(5) candidate-detail "Texniki məlumat" no longer exposes parser
name/version/error code; (6) confirmed and regression-tested CV-preview
XSS escaping; (7) "Originalı yüklə" now always sends a true download
(`Content-Disposition: attachment`) instead of opening PDFs inline.
No search/matching/scoring behavior changed; no migration.

A third owner visual inspection found two further acceptance blockers,
both fixed on the same branch/PR (see D-025). Root-caused, live, before
any change — not guessed: (A) an owner-created vacancy's Python SKILL
criterion resolved to UNKNOWN against a candidate with verified Python
evidence; traced to the persisted criterion having `value: "MUST_HAVE"`
instead of `value: "Python"` — the previous two-field ("Ad"/"Dəyər") form
let an HR tester type the requirement's type into the value field, and
the deterministic scorer correctly found no matching skill for that
malformed value (not a scoring/evaluator bug). Fixed by collapsing
"Ad"/"Dəyər" into a single HR-facing "Tələb" field that becomes both the
label and the matched value, making the mistake structurally impossible;
added a critical acceptance test proving a UI-created SKILL criterion now
resolves correctly against real evidence (and that missing evidence still
correctly resolves to UNKNOWN), plus a structural-equivalence test against
an API-created criterion. (B) The natural-language search "unsupported"
message didn't distinguish a genuine, model-independent product-policy
gap from the small local planner model (`qwen3:0.6b`) simply misjudging
an ordinary request — reproduced live against real local Ollama and
confirmed via the persisted audit event that the actual outcome was the
*model itself* self-declining (`PlannerDraft.unsupported_reason_codes`),
not a policy-regex false positive and not a provider failure. Added an
internal `MODEL_DECLINED_INTERPRETATION` marker and a distinct, honest HR
message for that case only; genuine deterministic product-policy
rejections keep the original message. No search/matching/scoring
behavior changed; no migration.

The owner then required a stronger guarantee than a more honest failure
message: common, explicit, supported HR search intents must not depend
on local-model interpretation quality at all (see D-026). Added
`meyar.search.planner_policy.try_deterministic_intent_parse` — a
conservative, whole-clause-anchored deterministic parser for exactly the
concepts `CandidateSearchRequest` already represents (skills, languages,
certifications, total experience years, simple "və" combinations),
invoked between the existing security precheck and the LLM loop in
`plan_candidate_search`. A match executes with zero LLM calls, through
the *same* `convert_planner_draft` validation the LLM path uses; anything
not fully, unambiguously accounted for declines and falls through to the
LLM completely unchanged — never a partial/weaker search. Skill-specific
duration (e.g. "Python üzrə 5 il təcrübəsi") is deliberately not
reinterpreted as total experience (no SearchPlan field for it) and
continues to fail the existing precheck exactly as before. Investigated
(per the request) whether a structured clarification/confirmation state
for a partially-understood request could be represented on the existing
server-rendered architecture — concluded it's architecturally feasible
(a session-scoped pending-plan + confirm/reject route) but is a
materially new feature with its own session/CSRF/audit implications, not
folded into this pass, since this parser never actually produces a
partial state (binary: full match or decline). No search/matching/
scoring behavior changed; no migration.

A dedicated semantic-correctness audit of that fast path found the
"continues to fail exactly as before" claim above was only half true
(see D-027): the *connector* phrasing ("Python üzrə 5 il təcrübəsi") did
still correctly decline, but the *locative/ablative-suffix* phrasing
("Pythonda 5 il təcrübəsi", including the original owner-reported query)
was in fact producing `skills=["python"], min_total_experience_years=5.0`
— silently reading "5 years IN Python" as "Python skill + 5 years of
ANY experience". Inspected the evidence model directly (not assumed):
`CandidateProfileExtraction` has no field linking a `SkillItem` to an
`EmploymentItem` date range, so MEYAR genuinely cannot prove per-skill
duration — confirmed this must never be invented. Fixed at the shared
`precheck_natural_language_request` (a fifth `_SKILL_DURATION_PATTERNS`
entry for the suffix shape, plus an "ən az"-without-"ı" fold gap found
while writing the regression matrix) so every equivalent phrasing is now
rejected identically, for the deterministic fast path, the LLM path, the
REST API, and the CLI alike — one fix, one source of truth, no new
`PlannerOutcome`/API contract change. Added a genuinely honest
alternative: a `/ui/search`-only clarification screen
(`search_clarification.html`) naming the extracted skill/years, offering
one explicit confirm action that resubmits an unambiguous
explicit-separation rephrasing through the normal flow — never executed
without that click. Also fixed, on the same audit pass: vacancy-creation
kind-aware validation was previously incomplete (a stray "Təcrübə (il)"
value on a non-EXPERIENCE row was silently dropped, not rejected — now
rejected). Reported, not fixed: `Job` has no unique-title constraint and
no lifecycle/status field at all (no close/archive/soft-delete) — a
genuine open product gap for a future slice, not a scoring/tenant-
isolation issue. No search/matching/scoring behavior changed beyond the
precheck rejection scope; no migration.

That reported Job lifecycle gap is now closed (see D-028). `Job` gained
`status` (`ACTIVE`/`ARCHIVED`) and `archived_at` via migration
`db7e4523f491` — every existing row backfills to `ACTIVE` deterministically
in the same `ALTER TABLE` (verified against the real dev DB with
upgrade/downgrade/re-upgrade, and against a from-scratch throwaway
database through the entire `base`→`head` chain); no hard delete
anywhere, no `JobCriteriaVersion`/`Evaluation` row is ever touched by
archiving. `/ui/jobs` defaults to active vacancies with a new
`?status=archived` view ("Aktiv vakansiyalar"/"Arxiv" tabs); a new
CSRF-protected, tenant-scoped `POST /ui/jobs/{job_id}/archive` (existing
`jobs:write` scope) is the only transition, with no reopen/edit/delete in
this pass. Titles remain deliberately non-unique — instead, a canonical
signature (normalized title + sorted, normalized criteria — never the
free-text label or the id) is compared, and creating a second `ACTIVE`
job with an identical signature is rejected with an HR-safe message; an
`ARCHIVED` duplicate never blocks a new `ACTIVE` one. The real
concurrency guard is a partial unique index on
`(tenant_id, duplicate_signature) WHERE status='ACTIVE' AND
duplicate_signature IS NOT NULL`, declared on both the model (so the test
suite's `Base.metadata.create_all` schema gets it too — a genuine gap the
concurrency test itself caught on the first pass, when the index existed
only in the migration) and the migration; a race that slips past the
application-level pre-check hits this constraint and gets the identical
friendly rejection. `POST /api/v1/jobs` is completely unaffected — no
duplicate check, no lifecycle field required or returned; verified with a
regression test creating two identical jobs via the API successfully.
Ranking/scoring untouched.

Slice 14 (`feat/folder-reconciliation`, **MERGED as PR #24 at squash SHA
`f6e31ff`, closes issue #23**, associated with
**M6 — Operational CV Intake & Reconciliation**). Closes the gap left after Slice 6: a
folder-imported `CandidateDocument` never automatically continued through
profile extraction, identity extraction, or embedding, so a folder-imported
candidate was not searchable without a manual per-candidate command. Reuses
`meyar.services.folder_indexer_service.index_folder` unchanged for
discovery/ingestion; adds a new `meyar.services.folder_reconciliation_service`
that drives whichever of `extract_candidate_profile`/
`extract_candidate_identity`/`embed_candidate_profile` (Slice 4/7, unchanged)
each pending candidate's *current* document still needs, deriving readiness
from existing `candidate_document_id`-scoped provenance rather than a new
migration. A `stability_window_seconds` parameter on `index_folder`
(`MEYAR_FOLDER_STABILITY_SECONDS`, default 60) skips a file that was
modified too recently to trust — never marked FAILED, never tombstoned
MISSING if already tracked. `find_indexed_file_by_content_hash`
(tenant-scoped SHA-256 lookup) links a byte-identical file at a second path
to the already-ingested `candidate_id`/`candidate_document_id` instead of
minting a duplicate — document-content dedup only, never human-identity
resolution, never cross-tenant. New CLI command
`meyar reconcile-folder --tenant-id --root [--limit N]` serves both initial
bulk import and repeatable reconciliation in one invocation (same exit-code
discipline as `index-folder`: 0 clean, 1 completed-with-failures, 2 invalid
source, 3 infra/DB failure), PII-safe output. No database migration (Slice
4/7's existing `candidate_document_id` FKs are sufficient); no new runtime
dependency (periodic reconciliation, not a filesystem watcher — see D-021).
A focused hardening pass (same PR, commit `41cad87`) then closed two
findings from an independent acceptance audit: a real changed-CV
end-to-end regression proving stale search state is never used, and a
`--limit`-fairness fix so a persistently-failing candidate can never
permanently starve a candidate that has never been attempted (see D-021
items 3, 7, 9). **M6 has no remaining open issues but is deliberately
left open pending explicit owner milestone-closure approval** — see
"Current phase" above.

## Blockers
**Mac Mini benchmark execution** — the sole remaining mandatory blocker to
MVP closure, now sequenced behind issue #35 (Agentless Mac Deployment
Readiness, the current active phase): issue #35's tooling lands first, then
issue #36 executes the benchmark. Target hardware is owner-confirmed (Mac
mini M4 Pro, 12-core CPU/16-core GPU/24GB unified memory/512GB SSD —
reference, not lock-in), and the benchmark harness is built and dry-run
smoke-tested, but it has not been executed on the actual confirmed hardware
— this also blocks approving a final production LLM/embedding model
(D-014). Other previously-open items: document encryption-at-rest remains a
deployment responsibility, not an application feature, per
`docs/SECURITY_PRIVACY.md` (unchanged); D-009 Ollama upgrade needs root
(unchanged, non-blocking). Git remote is connected but is a
personal/temporary one (D-012) — official bank-owned remote still pending,
migration keeps full history when it arrives (organizational, non-blocking
for MVP).

## Next action
1. **Issue #35 — Agentless Mac Deployment Readiness (M9, current active
   phase).** Complete tested, executable provisioning/configuration/
   migration/Ollama-setup/service-lifecycle/healthcheck/backup-restore/
   update-rollback/diagnostics tooling, with no Claude Code/Codex/AI-coding-
   agent dependency on the target deployment host.
2. **Issue #36 — Real Target-Mac Model Selection & Benchmark (M9, next,
   depends on #35).** Run `backend/scripts/target_mac_benchmark.py` on the
   actual confirmed target hardware and record a production model decision.
   M5/issue #20 must not close until this moves to DONE — #35/#36 do not
   supersede or close M5/#20.
3. **M6 closure decision** — issue #23 is closed and M6 has no remaining
   open issues; closing the milestone itself requires an explicit owner
   decision (never invented automatically — see
   `.claude/rules/git-workflow.md`).
4. **Git Infrastructure** — remote connected (`a-r3/meyar`, private,
   temporary — D-012); governance merged (`16929fd`). May later migrate
   to an official bank-owned remote (history preserved).

The previously planned "Slice 6 — External Async Evaluation API" is
CANCELLED (superseded by D-011) — it is not what "Slice 6" now refers to.

## GitHub milestone status

The detailed canonical mapping is in `docs/MVP_PLAN.md`. No milestone has a
due date because the official timeline has not been supplied.

| Milestone | Slice mapping | Current status |
|---|---|---|
| M0 — Project Foundation & Governance | R0 + Git Infrastructure | CLOSED — merged `16929fd`, issue #2 closed |
| M1 — CV Ingestion & Candidate Library | Slice 6 | CLOSED — merged `55fef2d` (PR #7), issue #6 closed |
| M2 — Candidate Search Intelligence | Slices 7–9 | CLOSED — PRs #9/#11/#13 merged; issues #8/#10/#12 closed |
| M3 — JD Matching & Ranking | Slice 10 | CLOSED — PR #15 merged at `1c9dbbd`, issue #14 closed |
| M4 — Internal Product Interface & API | Slices 11–12 | CLOSED — Slice 11 merged (PR #17, issue #16 closed); Slice 12 merged (PR #19 at `93fa567`, issue #18 closed) |
| M5 — Security, Target-Mac Validation & MVP Acceptance | Slice 13 + target-Mac benchmark | OPEN — issue #20 open; PR #22 merged at `a709ce1` implementing Pass 1 (original CV, no-exfiltration, backup/restore, audit guard, multilingual evidence) with `Refs #20`; Mac Mini benchmark execution on the now owner-confirmed target hardware remains the sole open mandatory gate |
| M6 — Operational CV Intake & Reconciliation | Slice 14 | CLOSED — Slice 14 merged (PR #24 at `f6e31ff`), issue #23 closed; owner-approved closure |
| M7 — HR UI & Presentation Readiness | HR UI productization (chore, issue #27) | OPEN (milestone not yet explicitly closed by owner) — PR #29 MERGED at `1f8bd12`, closing issue #27; Job lifecycle implemented; see D-023 through D-029 |
| M8 — Bounded Local-AI HR Agent Platform | Slices 1–5 (issues #30–#34) | OPEN — Slice 1 (#30), Slice 2 (#31), Slice 3 (#32) merged (PR #41 at `8c1782f`); Slice 4 (#33) MERGED as PR #42 at `584eb35`, closing issue #33 and, via its full D-042–D-065 corrective series, issue #44; Slice 5 — Confirmed Actions Framework (issue #34) remains OPEN, not started |
| M9 — Deployment, Benchmark & Integration Readiness | Slices 6–8 (issues #35–#37) | OPEN — created 2026-09-01 per D-030/D-031/D-032; issue #35 (Agentless Mac Deployment Readiness) is the current active engineering phase, implementation in progress; issue #36 (Real Target-Mac Model Selection & Benchmark) is next, not yet started; does not supersede or close M5/#20 |

## Official requirement gap matrix

Against `AI-PROJ-CV-01` + owner clarifications. DONE = implemented and
tested; PARTIAL = foundation exists, capability incomplete; NOT STARTED =
no code yet.

| Requirement | Status | Evidence | Remaining work | Slice |
|---|---|---|---|---|
| Mac Mini / model benchmark | NOT STARTED | Target reference hardware now owner-confirmed (Mac mini M4 Pro, 12-core CPU/16-core GPU/24GB unified memory/512GB SSD — reference, not lock-in); harness built (`backend/scripts/target_mac_benchmark.py`) and dry-run smoke-tested on the dev machine only | Execute the benchmark on the actual confirmed target hardware; no numeric latency threshold is approved (functional success + recorded timings, per owner decision 2026-08-28) | 13 (blocked on physical hardware access) |
| PDF parsing | DONE | `pypdf`-based `LocalTextParser` (Slice 3) | — | 3 |
| DOCX parsing | DONE | `python-docx`-based parsing (Slice 3) | — | 3 |
| Scanned PDF detection / OCR | NOT STARTED | Digital-PDF-only parsing (D-007) | Local OCR fallback — explicitly deferred, non-MVP | future |
| Multilingual CV tests | DONE | Synthetic Azerbaijani/Russian/English fixtures + 9 regression tests proving the existing parser (Unicode-transparent) and extraction/identity pipeline (Pydantic + Postgres JSON) round-trip all three languages unchanged, via `FakeLLMProvider` (`test_multilingual_evidence.py`). Owner-approved classification (2026-08-28): pipeline/schema evidence, not a claim of real-model per-language quality | Real-model sanity sampling may occur during the actual Target-Mac run where practical — supplements, does not replace, this evidence | 13 |
| Structured extraction | DONE | `CandidateProfileExtraction` schema (Slice 4); `CandidateIdentityExtraction` (full_name/email/phone, separate schema, Slice 7) | — | 4, 7 |
| Strict JSON validation | DONE | Pydantic v2, `extra="forbid"`, bounded retry (Slice 4, Slice 7 identity, Slice 9 planner) | — | 4, 7, 9 |
| Uncertainty handling | DONE | `UNKNOWN` never auto-downgraded (D-010), Slice 5 | — | 5 |
| Candidate DB | DONE | `Candidate`, `CandidateDocument`, `CandidateProfileVersion`, `CandidateIdentityVersion` (Slice 7, D-014) | — | 3, 4, 7 |
| Original file reference | DONE | Opaque storage id + `DocumentStorage` abstraction (Slice 3) | Opaque reference is DONE; the separate mandatory "open original CV" product capability (`docs/PROJECT_VISION.md`) is not implemented — carried into Slice 13 / M5 final MVP acceptance, not Slice 11 | 13 |
| Local CV folder migration/indexing | DONE | Symlink-safe recursive scanner, SHA-256 content-hash incremental/idempotent indexing, existing ingestion pipeline reused, tombstone-not-delete on removal (Slice 6, D-013) | — | 6 |
| Local embeddings / vector storage | DONE | Local `EmbeddingProvider`/`OllamaEmbeddingProvider` (loopback-enforced), pgvector-backed `CandidateEmbeddingVersion` with version/provenance, idempotent, dimension-agnostic column (Slice 7, D-014) | — | 7 |
| Access control | DONE | API-key auth, scopes, tenant isolation (Slice 1) | Extend scopes as new endpoints ship | ongoing |
| JD matching | DONE | Deterministic per-criterion evaluation (Slice 5) | — | 5 |
| 0–100 scoring | DONE | `meyar-score-v1`: exact Decimal weighted formula, exhaustive factors, final `ROUND_HALF_UP`, persisted score and recomputable explanation (Slice 10, D-017) | — | 10 |
| Batch scoring / ranking | DONE | Tenant-library batch service, current-profile-only selection, fit-tier-first ordering, Decimal score, UUID tie-break, deterministic skips (Slice 10, D-017) | UI presentation (Slice 11) and REST presentation (`POST /api/v1/jobs/{job_id}/criteria/{version_number}/rank`, Slice 12, D-019) both live | 10 |
| Structured search | DONE | Deterministic required/preferred filters (skills/certifications/languages/education/min experience) over the current `CandidateProfileVersion`, reusing Slice 5 normalization (Slice 8, D-015) | — | 8 |
| Semantic search | DONE | Local query embedding + pgvector retrieval over current, exactly-compatible embeddings (Slice 8, D-015); strict local natural-language `SearchPlan` conversion delegates to that engine (Slice 9, D-016) | — | 8, 9 |
| Explanations | DONE | Criterion evidence (Slice 5), search components (Slice 8), planner reasons (Slice 9), and exact recomputable score contributions with location-only evidence refs (Slice 10); never chain-of-thought | — | 5, 8, 9, 10 |
| REST API | DONE | Full `/api/v1` surface: jobs/candidates/health/usage plus Slice 12 structured+NL search, single/batch scoring, and identity-enriched candidate detail — all thin wrappers over accepted Slice 5/8/9/10 services (D-019) | — | 12 |
| Swagger / OpenAPI | DONE | Bearer `securitySchemes` entry, tag metadata, unique operationIds, `/ui` excluded from schema, fully offline `/docs` (vendored `swagger-ui-bundle`, zero CDN dependency), `test_openapi_contract.py` regression (D-019) | — | 12 |
| Auth | DONE | API-key + scopes (Slice 1); OpenAPI-visible via `HTTPBearer` (Slice 12, D-019) | — | 1, 12 |
| README examples | DONE | API-key provisioning, auth header, Swagger access, synthetic curl examples for search/NL-search/score/rank, local-AI dependency map, error semantics (Slice 12) | — | 12 |
| Bad-file testing | DONE | Oversized/malformed/MIME-mismatch tests (Slice 3); malformed-PDF/DOCX isolation + path-traversal/symlink tests for the folder indexer (Slice 6) | — | 6, 13 |
| Scoring consistency | DONE | Exact input reuse, explicit historical date, Decimal boundary/rounding/recomputation, version-change, stable-tie, gate-vs-score, and identity-invariance regressions (Slice 10) | Final target acceptance remains Slice 13 | 10, 13 |
| External-network/exfiltration verification | DONE | Static inventory (only two `httpx.AsyncClient` construction sites in the whole app, both loopback-gated) plus a deterministic runtime guard (`test_no_exfiltration.py`) that patches `httpx.AsyncClient.send` to reject any non-loopback request, exercised against a representative extract+embed workflow via the real `OllamaLLMProvider`/`OllamaEmbeddingProvider` classes (`MockTransport`), plus a negative control proving the guard itself works. **D-047 (2026-09-05, PR #42 pre-merge internal audit):** closed a below-the-logical-URL-layer defect — httpx's default `trust_env=True` let process-environment `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` re-route a loopback-validated request through a proxy transport when `NO_PROXY` wasn't also set correctly. All Ollama client construction now goes through one shared `trust_env=False` boundary (`meyar.llm.loopback.build_local_only_async_client`); proven at the internal `_mounts`/`_trust_env` transport-configuration level, not just `request.url.host` (`test_ollama_transport_proxy_isolation.py`, 18 tests). | — | 13 |
| Data-protection / backup description | DONE | `docs/BACKUP_RESTORE.md` runbook; `backend/scripts/backup_restore_acceptance.py` executed successfully against synthetic, disposable data — DB (`pg_dump`/`pg_restore`) + document storage (`tar`) backed up and restored into an isolated destination, row counts/relationships verified, original-CV bytes byte-identical, repeat score request reused the exact same `Evaluation` (`reused=true`) | — | 13 |
| Git branch / PR workflow | PARTIAL | Remote connected (`a-r3/meyar`, private), CI + hooks + PR template merged (`16929fd`, D-012) | Migrate to official bank remote when supplied — organizational, owner-dependent, not a Slice 13 software gap | Git Infrastructure |

**Official numbered task matrix — 28 items.** Summary (independently
recounted during Slice 13 finalization, 2026-08-28): **25 DONE, 1 PARTIAL,
2 NOT STARTED** (28 items). Since the Slice-12 baseline (22/2/4): multilingual
CV tests, external-network/exfiltration verification, and data-protection/
backup description all moved NOT STARTED/PARTIAL → DONE (Slice 13 Pass 1,
PR #22). Original-file reference remains DONE at the storage layer; the
separate mandatory "open original CV" product capability
(`docs/PROJECT_VISION.md`) **is now implemented** (Slice 13 Pass 1 — see
`GET /ui/candidates/{candidate_id}/documents/{document_id}/original`) — this
does not add a 29th matrix row or change row 10's DONE status, it closes the
separate mandatory item tracked in issue #20. The one remaining PARTIAL row
(Git branch/PR workflow) is organizational, pending a bank-owned remote, and
is not a Slice 13 or MVP-closure blocker. The two remaining NOT STARTED rows
are Mac Mini/model benchmark (mandatory — target hardware is now
owner-confirmed, but execution on that hardware has not occurred; this is the
sole remaining mandatory MVP blocker) and OCR (explicitly deferred/non-MVP
per D-007, durable decision authority, not a blocker). **MVP cannot be
declared complete and M5/issue #20 must not close until the Mac Mini
benchmark row moves to DONE** on the actual confirmed target hardware.
