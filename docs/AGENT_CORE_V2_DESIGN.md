# Agent Core v2 — task, dialogue and capability architecture (issue #88)

**Status: ACCEPTED DESIGN; slice A IMPLEMENTED, technically accepted and
MERGED (PR #98); slice B IMPLEMENTED, independently technically accepted
and MERGED (PR #100, D-094); slice C independently accepted and MERGED
(PR #102, D-095); Amendment A3 ACCEPTED and MERGED (PR #103); Issue #88
CLOSED/completed (see "Current implementation status" below).**

Current roadmap: #45 accepted shared application contracts precede the future
comprehensive audit/remediation/full re-audit and #35; #50 is deferred and consumes
#45 contracts where relevant (D-115 / [ROADMAP_NORMALIZATION.md](ROADMAP_NORMALIZATION.md)).

Historical status at design acceptance (kept as recorded then):
- This design was independently reviewed and accepted, at design head
  `dfc7636b1c0b788024c28d17f2b913784b5a897c`.
- **Implementation has NOT started.** Nothing in this document exists in
  code yet.
- #88 remains OPEN. Implementation (slice A first, §23) begins only after
  the OWNER manually merges this accepted design PR.
- #50 remains out of scope.

Decision record: D-092 (`docs/DECISIONS.md`). Audited baseline: `main` @
`fb03477a4b4c3ec4698b7294f2589fbbbeafa4e7` (#84, #85, #86, #87 closed).
Alembic head at audit time: `a87d4c6e2b19`.

**Amendment A1 (ACCEPTED AMENDMENT; owner-selected "Variant 1"; implemented
by slice A — see "Current implementation status" below). Status at
acceptance (historical):**
- A1 was independently reviewed and accepted.
- Slice A implementation has NOT started. #88 remains OPEN, and #50 remains
  OPEN and out of scope.
- Slice A may begin only after the OWNER manually merges PR #94 and
  post-merge verification succeeds.

The change: the clarification liveness binding changes from
`turn_version` equality to an **append-position** binding through a new
`question_turn_id` (§6.2 rule 2, §4.3, §18). Reason: the audit of `main`
@ `3ac42a5` for slice A found that the lane-B human routes
(`/ui/agent/drafts/{id}/confirm` via `mark_pending_job_draft_confirmed`, and
`/ui/agent/drafts/{id}/resolve` via `replace_pending_job_draft`) rewrite
transcript entries **in place** and bump `turn_version` (D-089: every
transcript write advances it). Under the original rule, confirming or
reviewing D1 would stale an open lane-A clarification. That contradicts
§4.4 rule 4. No other part of the accepted architecture changes.


**Amendment A2 (ACCEPTED AMENDMENT; owner-specified exchange-chain rule;
implemented by slice A — see "Current implementation status" below). Status
at acceptance (historical):**
- A2 was independently reviewed and accepted.
- Slice A implementation remains PAUSED. #88 remains OPEN, and #50 remains
  OPEN and out of scope.
- Implementation resumes only after (1) the OWNER merges PR #96, (2)
  post-merge verification succeeds, and (3) the recurring CI pytest hang is
  investigated separately.

The change: A1's adjacency rule is correct for attempt 1 but
incomplete for the attempt-2 UNCLEAR retry (§6.4 step 5, T5). After a retry
the tail is `[U1 source, Q1, U2 unclear answer, Q2]`, so the entry before Q2
is U2, not the source. Read literally, every attempt-2 clarification would be
born stale. A2 replaces A1's adjacency clause with a **structural
exchange-chain rule** (§6.2 rule 2), validated only from persisted
clarification/task/submission relationships, for at most 2 attempts. It
applies to **both** resumable types: SEARCH_OR_VACANCY, with its source
binding, and VACANCY_SOURCE_REQUIRED, which has no source but still has the
2-attempt policy. It adds two persisted fields (`created_from_turn_id`,
`superseded_reason`, §4.3, §18) and one closed value (`SOURCE_MESSAGE`,
§6.3).

A2 also separates three cases (§6.4):
- user ambiguity (a valid UNCLEAR result) consumes an attempt;
- classifier infrastructure failure abandons the turn and consumes nothing;
- a stale or foreign button is a rejected request, not a state transition.

**Amendment A3 (ACCEPTED AMENDMENT; independently reviewed and accepted;
MERGED through PR #103, squash `d9e96c06169d9ebf25ab6f276eb63bab4c5d4828`).**
Full text: D-092 Amendment A3 in `docs/DECISIONS.md`. Summary:
- A3.1 (§15, assistant turns only): `propose_agent_plan` never receives
  persisted HR-facing assistant display text (it can carry
  CandidateIdentity); assistant turns are projected as the closed
  `AgentTurnOutcome` code. User turns, the turn limit and human-visible
  transcript storage/display are unchanged. No heuristic name redaction, no
  conversation RAG.
- A3.2 (§10.2 rule 3): a coordinated material subject analyzed as one
  occurrence ("Python və Java") needs coverage for EACH deterministic
  coordinated part. Coverage sources are SEARCH/REFINE source spans, the
  grounded reference span and the grounded topic span; a `limit_quote` is
  numeric workflow grounding, NOT requirement coverage. WHOLE_MESSAGE still
  covers everything; anti-laundering is unchanged.
- A3.3 (RANK): RANK_JOB_CANDIDATES stays a registered HUMAN_ACTION_ONLY
  capability that never executes in a turn, but it is NOT model-proposable
  or offered in chat until an explicit server-owned, unambiguous "current
  confirmed job" selector exists (own reviewed decision). No "latest
  confirmation" choice, no transcript scanning, no migration or pointer in
  A3. Direct ranking routes and deterministic scoring are unchanged.
  CREATE_JOB is unchanged.
- Slice-C PR #102 must now be corrected to accepted A3 (keep
  the outcome-code projection and coordinated-part coverage; exclude LIMIT
  from coverage; make RANK proposability consistent with A3.3; keep the
  direct ranking routes).

**Current implementation status.** A2 (with A1 and the D-092 slice A
scope) is implemented by issue #88 Slice A; implementation record D-093.
The corrected Slice-A code was technically accepted by independent review
at `195387bddaf7f1f4b6d11aa3c2dd56e070478bf6` and merged through PR #98
(accepted head `4b27e6a87cebed083fdeb0cfa4afbf8c99b5c2c6`, squash commit
`a9b8ff39746aa5767f8ad2b3b95809db5b1b9bc4` on `main`). Slice B (§23,
capability registry + validator) is implemented (implementation record
D-094), was independently technically accepted at code head
`76d3719bde0a5bfb8a8b1c8e7aab84832a8de198`, and merged through PR #100
(accepted head `4b3a2260922f1dc1c19ba753a3c990b44d0c8d2f`, squash commit
`2961365c961894cc0f6b61ac65619c9891e8cf4e` on `main`).
Slice C (implementation record D-095), corrected to the accepted Amendment
A3 (merged through PR #103), was independently accepted at PR #102 head
`117d0abbad4f012911e0cd96794604eaa9f474ba` (exact-head CI run
`36995144617` SUCCESS: 3229 pytest passed, 10 hang diagnostics passed) and
merged through PR #102 as squash commit
`82d6f7b4a6e4e51b02442b62dc7a556519b66e5c` on `main` (single parent
`32f1a0e5411f0445f3bc794451e5288561ae5cd1`); the accepted-head tree equals
the squash tree exactly (`d259e06fe73b11d33aca751bf4f444924d09bace`).
Issue #88 is CLOSED/completed. #50 remains OPEN and was out of scope for
#88. No migration (Alembic head stays `b88a2c4d6e10`); no new dependency.
The target-Mac benchmark remains separate; model quality/selection remains
#36. The design text in this document is unchanged.
---

## 0. Product contract

> Normal HR users must experience MEYAR as a context-aware AI assistant
> capable of understanding, clarifying and continuing natural-language tasks,
> while all scoring authority and consequential state transitions remain
> deterministic/server-owned or explicitly human-confirmed.

Agent Core v2 changes the orchestration from

```text
message → classify one action → answer
```

to

```text
conversation
→ resolve existing structured dialogue state (open clarification / waiting task)
→ understand/propose the user goal
→ produce a bounded capability plan
→ server validates the plan
→ execute authoritative capabilities
→ update structured state (atomically, in Phase B)
→ evidence-backed / server-safe response
```

Continuity comes from typed server state, not from copy that only sounds
like an assistant and not from the model re-reading the transcript.

---

## 1. Current architecture (audited on `fb03477`, not planned)

### 1.1 Turn flow as implemented

`POST /ui/agent` (`backend/src/meyar/ui/router.py`, `agent_turn`):

1. CSRF check. The canonical LF message is limited to 4000 characters (#87).
   Required scope is `candidates:read` (`AGENT_TURN_REQUIRED_SCOPE`).
2. **Phase A** (`reserve_agent_turn`): lock the owned `AgentConversation`,
   refuse with 409 if another live reservation exists, create or get the
   BrowserSession-bound `AgentConversationSessionContext`, and write
   `active_turn_id`. Claim the server-issued `AgentTurnSubmission` (ISSUED →
   PROCESSING). The resulting `TurnReservation` snapshots `turn_version`,
   `context_id`, `context_epoch`, `active_result_set_id` and
   `active_pending_draft_id`.
3. `execute_agent_turn` (`agent/service.py`) runs over **snapshots**
   (`ConversationSnapshot`, `TurnSessionState`):
   1. **Pending-draft follow-up branch.** If the session context has a live
      pending draft and `_FOLLOWUP_RE` matches, the server applies the
      deterministic draft amendment (`_apply_pending_draft_followup`) and the
      turn ends.
   2. `route_agent_entry(message)` (`agent/intent_routing.py`, policy
      `agent-entry-routing-v4`) returns one of FORCE_JOB_DRAFT,
      FORCE_CANDIDATE_SEARCH, FORCE_RESULT_LIMIT, MODEL_ROUTED,
      CLARIFY_AMBIGUOUS, CLARIFY_JOB_SOURCE_REQUIRED or
      CLARIFY_INPUT_STRUCTURE, and audits `agent.entry.routed`.
   3. The three CLARIFY_* routes return **fixed copy** with outcome
      `CLARIFICATION_REQUESTED`. The turn ends and nothing structured is
      stored.
   4. Otherwise a `while True` loop runs:
      - A forced route builds a server `AgentDecision`: DRAFT_JOB_CRITERIA
        over the exact source span, SEARCH_CANDIDATES with the user's own
        text, or REFINE with a server limit.
      - MODEL_ROUTED calls `llm.decide_agent_action`. The model sees the
        last `agent_max_context_turns` (default 8) transcript
        `(role, text)` pairs, the last tool summary, `active_result_context_present`
        and `available_candidate_refs`. The call gets one repair retry.
      - The model returns one strict `AgentDecision` (`agent/schemas.py`,
        `extra="forbid"`): an action enum plus the minimal typed argument.
   5. Dispatch is hard-coded `if/elif` on `decision.action`:
      - `_dispatch_search` → `plan_and_search_candidates` (frozen D-031
        planner, which uses local-model planning) → `create_result_set_from_search`.
        The new ResultSet pointer is synced into the in-turn state. The loop
        runs again, and the model may add a second step or close the turn,
        bounded by `agent_max_tool_calls` (default 3).
      - `_dispatch_refine` → `validate_active_result_set_for_refinement` +
        planner for `filter_query` → `create_result_set_from_refinement`.
        Turn-terminal.
      - `_dispatch_profile` / `_dispatch_evidence` →
        `resolve_active_candidate_ref` → `get_current_authorized_profile`.
        After that comes an optional D-038 grounded synthesis
        (`select_grounded_facts` + deterministic `render_grounded_answer`).
        Turn-terminal.
      - `_dispatch_draft_job_criteria` → local JD draft + deterministic
        canonicalization (#84), producing a review-only pending draft payload.
        Turn-terminal. A **model-proposed** DRAFT_JOB_CRITERIA is rejected
        (`agent.entry.action_rejected`) and replaced by the fixed ambiguous
        copy.
      - FINAL_ANSWER / CLARIFY carry a closed `AgentResponseCode`, rendered
        as fixed server copy.
   6. `_finish_turn` builds an `AgentTurnCommit`: the user turn, an
      assistant turn with server-authority text and an optional
      `pending_job_draft` payload, plus the proposed live pointers.
4. **Phase B** (`TurnBoundary.reenter` → `revalidate_reserved_turn`):
   - Principal rows are locked `FOR SHARE` (D-091).
   - The conversation and context rows are re-locked, and the reservation,
     `turn_version`, context and pointers are revalidated.
   - The submission is re-checked.
   - `apply_agent_turn_commit` appends the transcript, sets both pointers and
     the title, and audits `agent.turn.completed`.
   - `sync_last_turn_display_text` (D-045) runs, the submission becomes
     COMPLETED, the reservation is cleared and everything commits once.
5. No DB connection is held during any model/embedding call (#85
   `BoundaryLLM`/`BoundaryEmbedding` wrappers: `leave_db` → call →
   `reenter`).

### 1.2 Authority classification of today's system

| Concern | Where it lives today | Class |
|---|---|---|
| Tenant/user/membership ownership | `AgentConversation(tenant_id, owner_user_id, owner_membership_id)`, re-derived from live UIContext | Durable DB authority |
| Principal liveness | User/Membership/BrowserSession rows, locked FOR SHARE in re-entry (D-091) | Durable DB authority |
| Candidate ordinal authority | Session context `active_result_set_id` → `AgentResultSet` → member snapshot → current professional authority (#49/#80/#86) | BrowserSession-bound live authority |
| Pending JD draft authority | Session context `active_pending_draft_id` + payload in the owning transcript | BrowserSession-bound live authority |
| Confirmation idempotency | `AgentDraftConfirmation` unique (tenant, draft_id) | Durable DB authority |
| Request identity / replay | `AgentTurnSubmission` (#87) | Durable, BrowserSession-bound |
| Same-conversation concurrency | `active_turn_id` + `turn_version` (#85) | Durable DB authority |
| JD vs search vs clarify routing | `route_agent_entry` regexes + `analyze_hr_text` | Deterministic server code |
| Pending-draft modification intent | `_FOLLOWUP_RE` regex + deterministic amendment | Deterministic server code |
| Next action for MODEL_ROUTED text | `AgentDecision` from the local model | **Model proposal** |
| Search filters | Frozen D-031 planner (local model proposal → deterministic validation) | Model proposal, server-validated |
| Scores / ranking | Deterministic policy engine + `rank_candidates_for_job` | Deterministic server authority |
| Transcript | `AgentConversation.turns` JSON, ≤100 entries (`MAX_PERSISTED_AGENT_TURNS`), no stable per-turn id | Durable human-visible history, **not** authority |
| Clarification state | None, only fixed copy in the transcript | Does not exist |
| Task/goal state | None | Does not exist |
| Plan | One `AgentDecision` at a time, in memory; `searched_queries` guard | Transient, one turn |

Hard-coded branches in `service.py`:
- the pending-draft follow-up branch
- the clarification copy map
- forced draft, search and limit decisions
- the model-proposed-draft rejection
- the FINAL/CLARIFY handling
- the tool-limit check
- the duplicate-search guard
- separate DRAFT, REFINE and PROFILE/EVIDENCE terminal branches
- the search-terminal special case

Adding a capability today means editing `AgentActionType`, the
`AgentDecision` validator, `TOOL_ACTIONS`, the prompt, the loop and a new
`_dispatch_*`.

### 1.3 Root cause of the non-resumable clarification (audit M-8)

Probed against `route_agent_entry` on `fb03477`:

| Message | Route today |
|---|---|
| `Python mütləqdir.` | CLARIFY_AMBIGUOUS |
| `Python is mandatory.` | CLARIFY_AMBIGUOUS |
| `Python mandatory-dir.` | CLARIFY_AMBIGUOUS |
| `namizəd axtarışı` | MODEL_ROUTED |
| `candidate search` | MODEL_ROUTED |
| `namizəd search` | MODEL_ROUTED |
| `vakansiya kimi` | MODEL_ROUTED |

Why the flow breaks:
1. CLARIFY_AMBIGUOUS returns fixed copy and stores **nothing** except the
   transcript text. No record links the question to its source message, no
   allowed answers exist, and nothing expires.
2. The answer turn is routed as a brand-new, standalone message. It carries
   no material requirement, so it falls through to MODEL_ROUTED.
3. At that point the only link to `Python mütləqdir.` is the model reading
   the last 8 transcript entries. The model either invents a
   `search_query`, which may drop or rewrite the requirement, or answers
   CLARIFY again.
4. If the model proposes DRAFT_JOB_CRITERIA, that is rejected by design
   (#79), so the vacancy branch can never be reached from the answer.
5. There are no answer controls in the UI.

`CLARIFY_JOB_SOURCE_REQUIRED` ("send the vacancy text") has the same defect.
The text sent next is re-routed from scratch. A single-line requirement then
lands in CLARIFY_AMBIGUOUS again instead of being analyzed as the requested
vacancy.

---

## 2. Authority hierarchy (preserved, restated)

```text
Database / authenticated ownership
> live BrowserSession + ResultSet / mutation authority
> validated deterministic domain services
> validated capability execution
> accepted evidence / provenance
> structured task / dialogue state
> bounded human-visible transcript / model context
> model inference
```

The model **proposes**. It never grants authority. Structured agent state
sits *below* live session authority. It may **never**:
- authorize a candidate id, a ResultSet or a pending draft;
- choose a tenant or a session;
- override stale state;
- create scopes;
- change deterministic score policy;
- decide a hiring outcome.

Task state is a record of *what the user is trying to do*. It is never a
record of *what the user is allowed to touch*.

---

## 3. Semantic boundary: deterministic rules vs local model

### 3.1 Kept deterministic (authority or safety relevant)

- Prohibited/sensitive attribute detection (`find_prohibited_term`, #84
  PROTECTED_CUE roles) before any capability runs.
- Structural input safety: canonical LF, the 4000-character limit and the
  CLARIFY_INPUT_STRUCTURE overflow rule.
- Exact source offsets for JD sources (`_job_source_span`) and every
  `SourceOccurrence`.
- Count-only current-ResultSet follow-ups (`FORCE_RESULT_LIMIT`).
- Explicit vacancy-analysis wrappers and structurally strong pasted JDs.
  These keep the #79 guarantee that the JD source is exactly the user's text.
- Explicit new-search imperatives (`FORCE_CANDIDATE_SEARCH`).
- The requirement-shaped-but-ambiguous detection that **creates** the
  SEARCH_OR_VACANCY clarification.
- Confirmation boundaries: CREATE_JOB and ranking are never executed from
  a chat turn.
- The pending-draft amendment grammar (`_FOLLOWUP_RE` +
  `_apply_pending_draft_followup`). It is deterministic editing of a live
  draft and stays unchanged, but it is frozen (see §3.2).
- Closed-choice clarification answer labels (§6.5). This is a bounded,
  versioned table of the server's own button labels, not a semantic
  dictionary.

### 3.2 No longer grown

`intent_routing.py` regexes and `_FOLLOWUP_RE` are frozen as the
general-understanding layer. New phrases, technologies and languages are
**not** added there. A deterministic rule is added only when a model
ambiguity would weaken authority (the categories above), and each such rule
needs a reviewed decision entry.

### 3.3 Model proposal for general HR language

MODEL_ROUTED text (and, from slice C on, anything the deterministic pre-router
does not own) goes to one local-model call that returns a strict
`AgentPlanProposal` (§9). The server then validates it (§10). The capability
layer takes closed codes plus exact grounded quotes of the user's own text
(§10.2), so it is language-neutral.

Language claims are limited to what is tested:
- `SupportedInputLanguage` on `fb03477` covers **Azerbaijani, English and
  mixed AZ/EN**. Transliterated Azerbaijani is handled only as far as
  `fold_az_ascii` folding already covers it.
- **Russian is not claimed.** Cyrillic input is MODEL_ROUTED today, and no
  test proves Russian planning quality. Whether RU is supported is decided by
  #36 benchmark evidence, not by this design.

---

## 4. Structured state model

### 4.1 Four separate layers

| Layer | Storage | Authority |
|---|---|---|
| 1. Human-visible history | `AgentConversation.turns` (existing, ≤100 turns) | None. Display and bounded model context only |
| 2. Structured task/dialogue state | **new** `agent_tasks`, `agent_clarifications` | Continuity only. Never candidate or mutation authority |
| 3. BrowserSession live authority | `AgentConversationSessionContext` pointers (existing `active_result_set_id`, `active_pending_draft_id`; **new** `active_clarification_id`) | Live authority, not inherited across sessions |
| 4. Model context | Assembled per model call, never stored | None |

Nothing new is put on `AgentConversation` except a stable `turn_id` inside
each new transcript entry (JSON, no DDL). That id exists so a clarification
can point at its source turn.

### 4.2 Evaluation of the conceptual entities

- **AgentTask: adopted, small.** It records goal and lifecycle across turns
  (clarification → execution → confirmation → done).
  - It is persisted **only when the turn ends in a waiting state**
    (WAITING_CLARIFICATION or WAITING_CONFIRMATION).
  - A single-turn goal (search, refine, evidence) never needs a durable
    task row. Its provenance is the audit trail plus the ResultSet. This
    avoids one row per chat turn.
  - The ACTIVE state exists only in memory during a turn.
- **AgentDialogueState: not a separate table.** It is the combination of
  the session-context pointer `active_clarification_id` (live) and the
  waiting task's typed columns plus a small versioned state object. A
  separate table would duplicate the pointer semantics that already exist
  for ResultSet and pending draft.
- **AgentClarification: adopted.** It is the core of M-8 and is
  source-bound and BrowserSession-bound.
- **AgentPlan: not persisted.** A plan is proposed, validated and executed
  inside one turn. Resuming after a clarification needs no stored plan,
  because the resolved answer maps deterministically to exactly one
  capability over the bound source (§6.6). Plan provenance goes to audit
  (plan SHA-256, step count, closed capability codes, validation code).
  Persisting plans would add retention cost without adding authority.

### 4.3 Proposed fields and authority matrix

`agent_tasks`:

| Field | Durable? | Owner | Scope | Survives logout/relogin? | Survives new BrowserSession? | Live candidate/mutation authority? | Expiry / supersession | Source of truth | Migration |
|---|---|---|---|---|---|---|---|---|---|
| `id` | yes (until retention) | session context | tenant | row survives; **not actionable** | no (other context) | no | retention §13 | DB | new table |
| `tenant_id` FK tenants CASCADE | yes | — | tenant | — | — | no | — | DB | new |
| `conversation_id` FK CASCADE | yes | conversation | tenant+owner | — | — | no | — | DB | new |
| `session_context_id` FK session contexts CASCADE | yes | BrowserSession | tenant+owner+session | — | — | no (binding only) | dies with the context | DB | new |
| `owner_user_id`, `owner_membership_id` (NO ACTION, as on conversation) | yes | owner | — | — | — | no | — | DB, copied from the conversation at creation | new |
| `task_type` closed enum: `UNDETERMINED`, `CANDIDATE_SEARCH`, `RESULT_FOLLOWUP`, `VACANCY_ANALYSIS` | yes | task | — | — | — | no | UNDETERMINED only while WAITING_CLARIFICATION | server, set by the deterministic router or by a validated plan/resolution | new + CHECK |
| `status` closed enum (§7) | yes | task | — | — | — | no | §7 | server | new + CHECK |
| `phase` closed enum: `NEEDS_INTENT_CHOICE`, `NEEDS_SOURCE`, `DRAFT_REVIEW`, `DONE` | yes | task | — | — | — | no | set with status | server | new + CHECK |
| `state` JSON, `AgentTaskStateV1` (`extra=forbid`, ≤2 KB): `unresolved_slots` (closed `SlotName` ≤4), `assumptions` (closed `AssumptionCode` ≤8) | yes | task | — | — | — | **no**: carries no candidate refs, ids, ResultSet, draft id or identity | replaced as a whole | server | new + CHECK on `state_schema_version` |
| `pending_draft_id` (nullable UUID, no FK, like the pointer) | yes | task | — | — | — | **no**: a reference only, actionable only if it equals the live `active_pending_draft_id` | WAITING_CONFIRMATION only | pointer is the truth | new |
| `policy_version` (e.g. `agent-core-v2-task-v1`) | yes | — | — | — | — | no | unknown version → treated as EXPIRED (fail closed) | code | new |
| `created_by_submission_id` UUID **UNIQUE**, no FK (submissions are retired after 24h) | yes | — | — | — | — | no | — | #87 submission | new |
| `created_at`, `updated_at`, `expires_at`, `terminal_at` | yes | — | — | — | — | no | `expires_at` ≤ BrowserSession `expires_at` | server clock | new |

`agent_clarifications`:

| Field | Durable? | Owner | Scope | Survives relogin? | New BrowserSession? | Live authority? | Expiry / supersession | Source of truth | Migration |
|---|---|---|---|---|---|---|---|---|---|
| `id` | yes | task | tenant | row yes; **not answerable** | no | no | §6 | DB | new table |
| `tenant_id`, `conversation_id`, `session_context_id` FKs CASCADE; `task_id` FK agent_tasks CASCADE | yes | BrowserSession context | tenant+owner+session | — | — | no | — | DB | new |
| `context_epoch` | yes | — | — | — | — | no | mismatch → STALE | session context at creation | new |
| `clarification_type` closed: `SEARCH_OR_VACANCY`, `VACANCY_SOURCE_REQUIRED` | yes | — | — | — | — | no | — | deterministic router | new + CHECK |
| `answer_schema_version` (e.g. `clarification-answers-v1`) | yes | — | — | — | — | no | unknown → EXPIRED | code | new |
| `question_turn_id` UUID (id of the assistant transcript entry that asked the question), NOT NULL (A1) | yes | — | — | — | — | no | answerable only while this entry is the **last** transcript entry (§6.2 rule 2) → otherwise STALE | server-issued transcript entry id | new |
| `created_from_turn_id` UUID (A2), NOT NULL: the server-issued user transcript turn whose processing created this clarification attempt (appended by `created_by_submission_id`). SEARCH_OR_VACANCY attempt 1: equals `source_turn_id`. VACANCY_SOURCE_REQUIRED attempt 1: the trigger user turn U0 (`source_turn_id` stays NULL). Attempt 2 of either type: the UNCLEAR answer turn U2. **Sequencing provenance only, never source authority**; server-selected only, never client- or model-supplied | yes | — | — | — | — | no | chain check (§6.2 rule 2) | server-issued transcript entry id | new |
| `superseded_reason` closed (A2): `UNCLEAR`, `NEW_TASK`; non-null ⇔ status SUPERSEDED. Each value is a real persisted transition of the clarification itself (§6.4 steps 3–5) | yes | — | — | — | — | no | — | server | new + CHECK |
| `source_turn_id` UUID (id of the user transcript entry); SEARCH_OR_VACANCY only, NULL for VACANCY_SOURCE_REQUIRED | yes | — | — | — | — | no | missing → STALE | transcript entry id | new |
| `source_sha256` (SHA-256 of the canonical LF source span); same nullability as `source_turn_id` | yes | — | — | — | — | no | mismatch → STALE | computed | new |
| `source_start`, `source_end` (exact offsets into that user turn; CHECK `0 ≤ start < end ≤ 4000`) | yes | — | — | — | — | no | — | router (whole message today) | new |
| `semantic_policy_version`, `routing_policy_version` | yes | — | — | — | — | no | mismatch → STALE | code | new |
| `created_turn_version` (conversation's final committed `turn_version` after the creating Phase B, i.e. after the D-045 display sync) | yes | — | — | — | — | no | **provenance only** since A1; not a liveness condition | DB | new |
| `status` closed: `OPEN`, `RESOLVED`, `SUPERSEDED`, `EXPIRED` | yes | — | — | — | — | no | §6 | server | new + CHECK |
| `attempt` 1..2 | yes | — | — | — | — | no | >2 → EXPIRED | server | new + CHECK |
| `expires_at` | yes | — | — | — | — | no | TTL §6.7 | server clock | new |
| `superseded_by_id` self-FK SET NULL | yes | — | — | — | — | no | — | server | new |
| `resolved_value` closed `ClarificationAnswer` (nullable; CHECK non-null ⇔ RESOLVED) | yes | — | — | — | — | no | — | validated answer | new + CHECK |
| `resolution_source` closed: `BUTTON`, `LABEL`, `MODEL` | yes | — | — | — | — | no | — | server | new + CHECK |
| `created_by_submission_id` UNIQUE; `resolved_by_submission_id` UNIQUE nullable | yes | — | — | — | — | no | — | #87 | new |
| `created_at`, `resolved_at` | yes | — | — | — | — | no | — | — | new |

**No text is copied.** The clarification row holds no HR text, JD text or
CV text. The source text stays only in the transcript entry it points at.

`AgentConversationSessionContext.active_clarification_id` (new, FK
agent_clarifications SET NULL):
- It is the **only** live authority for "which clarification may be
  answered now in this session".
- It follows exactly the same rules as `active_pending_draft_id`: a new
  BrowserSession gets a fresh context with a NULL pointer.

Partial unique indexes (one per waiting lane, §4.4):
- one `OPEN` clarification per `session_context_id`;
- one `WAITING_CLARIFICATION` task per `session_context_id` (dialogue lane);
- one `WAITING_CONFIRMATION` task per `session_context_id`
  (mutation-confirmation lane; mirrors the single pending-draft pointer).

### 4.4 Two independent waiting lanes (v1 contract)

A session context has exactly two waiting lanes. They are independent and
may coexist.

| | A. Dialogue lane | B. Mutation-confirmation lane |
|---|---|---|
| Holds | at most one `WAITING_CLARIFICATION` task (+ its one OPEN clarification) | at most one `WAITING_CONFIRMATION` vacancy task |
| Live authority pointer | `active_clarification_id` | `active_pending_draft_id` (existing) |
| Created by | a server-typed clarification (§6.1) | ANALYZE_VACANCY producing a pending draft (direct, or via a resolved clarification) |
| Ended by | resolution, supersession, expiry, attempts (T2–T8) | HR confirmation, draft replacement, pointer loss, expiry (T9–T12) |
| Enforced by | partial unique index + pointer | partial unique index + pointer |

Lane rules:
1. **Coexistence.** For example, pending vacancy draft D1 waits for
   confirmation, then HR sends `Python mütləqdir.`. A SEARCH_OR_VACANCY
   clarification is created in lane A, and D1 stays untouched in lane B.
2. **Lane A never cancels lane B implicitly.** A new clarification, a
   clarification resolution, a normal candidate search, a refinement or a
   profile/evidence lookup never touch lane B. Resolving a clarification
   changes only its own task, **unless** the resolved action itself replaces
   the pending draft. Resolving to VACANCY_ANALYSIS produces D2, which
   replaces `active_pending_draft_id`, and that is exactly T11 for the old
   lane-B task.
3. **Only a new pending draft replaces lane B.** A new vacancy analysis
   (forced, or resumed) that moves `active_pending_draft_id` cancels the old
   WAITING_CONFIRMATION task (T11) in the same Phase B and puts the new task
   in lane B.
4. **Lane B never cancels lane A.** Confirming D1 (T10) in the separate
   confirm route leaves an OPEN clarification in lane A untouched. The next
   agent turn still resolves it by §6.4. The confirm and review-resolve
   routes rewrite pending-draft payloads **in place**. They bump
   `turn_version` but never append a transcript entry, so the append-position
   binding (§6.2 rule 2, A1) keeps the clarification answerable.
5. **Turn ordering when both lanes are live.**
   - If lane A has an OPEN clarification, §6.4 runs **first**.
   - The existing pending-draft amendment branch (`_FOLLOWUP_RE`, lane B)
     runs only if §6.4 classifies the message as a new request. The
     clarification is then superseded (T7) and the message is processed as
     today.
   - This stops a clarification answer such as `namizəd axtarışı et` from
     being misread as a draft edit because of the frozen `et` token.

---

## 5. Pipeline v2 (per turn)

```text
Phase A (unchanged #85/#87)
→ canonical message
→ [1] dialogue-state resolution (§6.4): open clarification? answer / new task / unclear
→ [2] deterministic pre-router (route_agent_entry, frozen)
      FORCE_*    → server-built single-step plan
      CLARIFY_*  → typed clarification (new state, no execution)
      MODEL      → [3]
→ [3] leave DB → local model: AgentPlanProposal → re-enter
→ [4] Layer 1 static validator (§11.1) → reject (closed code, zero execution) | executable plan
→ [5] per step: Layer 2 dynamic preconditions (§11.2) → executor (#85 boundary);
      a failed later step → PLAN_INCOMPLETE, nothing activated (§11.3)
→ [6] stage task/clarification/pointer changes in AgentTurnCommit
→ Phase B: revalidate everything, including the clarification row (§12),
  then commit once
```

There is no loop that asks the model "what next?" after a tool. The plan is
fixed before execution starts (§9, §11).

---

## 6. Resumable clarification (M-8)

### 6.1 Creation

- The deterministic router returns CLARIFY_AMBIGUOUS (requirement-shaped,
  neither search nor JD) → the server builds
  `ClarificationDraft(type=SEARCH_OR_VACANCY, source = whole canonical
  message, offsets (0, len), sha256)`.
- Before building it, the server also requires
  `analyze_hr_text(source).requirements` to be non-empty and no PROHIBITED
  span (as today).
- The model proposing `ANALYZE_VACANCY` for text the router did not force
  becomes the same SEARCH_OR_VACANCY clarification if the text is
  requirement-shaped. Otherwise it gets the fixed NEED_MORE_DETAIL copy.
  This keeps #79: the model never starts JD drafting. It can only cause a
  question.
- CLARIFY_JOB_SOURCE_REQUIRED → `VACANCY_SOURCE_REQUIRED` with no source
  binding (`source_turn_id`, hash and offsets NULL, CHECK by type).
  `created_from_turn_id` is the trigger user turn U0. U0 is sequencing
  provenance, **not** JD source authority. The slot is filled by a later
  qualifying *current* message (§6.4 step 3).
- CLARIFY_INPUT_STRUCTURE and the model's closed CLARIFY codes
  (NEED_MORE_DETAIL, CANDIDATE_REFERENCE_REQUIRED, RESULT_CONTEXT_REQUIRED,
  UNSUPPORTED_REQUEST, HIRING_DECISION_REQUIRES_HUMAN) stay **non-resumable**
  fixed copy in #88. Only server-typed clarifications are resumable.

The turn stages:
- a new task (`UNDETERMINED` or `VACANCY_ANALYSIS`, status
  WAITING_CLARIFICATION);
- a clarification (OPEN, attempt 1);
- `active_clarification_id` pointing at it;
- the user turn (`turn_id` = new server UUID) and the assistant turn
  (`turn_id` = new server UUID = the clarification's `question_turn_id`),
  which carries a display-only `clarification` payload (question code plus
  choice codes, used to render buttons).

All of it is written in Phase B (§12). `created_turn_version` records the
conversation's final committed `turn_version` after that Phase B, after the
D-045 display sync. It is provenance only (A1).

### 6.2 Source binding and verification on resume

Resume is allowed only if **all** of the following hold. Otherwise the
clarification becomes EXPIRED/stale and the turn gives safe re-clarify copy:

1. `session_context.active_clarification_id == clarification.id`. The row is
   locked FOR UPDATE, `status == OPEN`, `expires_at > now`, and
   `context_epoch` equals the context's epoch.
2. **Append-position binding (A1) with exchange chain (A2).** For the
   current clarification C at attempt n:
   1. C's `question_turn_id` must be the conversation's **last** appended
      transcript entry, with role `assistant`.
   2. C keeps the **same original source binding**: `source_turn_id`, source
      hash and offsets never move to a user's unclear answer. For
      VACANCY_SOURCE_REQUIRED they stay NULL at every attempt.
   3. Between the chain start and C's question, only the server-recognized
      retry chain may exist. The tail must be exactly:

      | Type | Attempt 1 | Attempt 2 |
      |---|---|---|
      | SEARCH_OR_VACANCY | `[source U1, Q1]` | `[source U1, Q1, unclear answer U2, Q2]` |
      | VACANCY_SOURCE_REQUIRED | `[trigger U0, Q1]` | `[trigger U0, Q1, unclear answer U2, Q2]` |

      Attempt 1 requires:
      - `C.created_from_turn_id` at `T[-2]`, role `user`;
      - `C.question_turn_id` at `T[-1]`, role `assistant`;
      - for SEARCH_OR_VACANCY, also `C.created_from_turn_id =
        C.source_turn_id`.

      Attempt 2 requires, from persisted rows only:
      - the predecessor P has `P.superseded_by_id = C.id`;
      - `P.status = SUPERSEDED`, `P.superseded_reason = UNCLEAR`;
      - `P.attempt = 1` and `C.attempt = 2`;
      - P and C share `task_id`, `session_context_id` and
        `clarification_type`;
      - `T[-4]` is `P.created_from_turn_id` (U1 or U0), role `user`;
      - `T[-3]` is `P.question_turn_id` (Q1), role `assistant`;
      - `T[-2]` is `C.created_from_turn_id` (U2), role `user`. U2 is the
        entry appended by `C.created_by_submission_id`, the exact submission
        whose answer was classified UNCLEAR;
      - `T[-1]` is `C.question_turn_id` (Q2), role `assistant`, the latest
        appended entry;
      - for SEARCH_OR_VACANCY, P and C carry the identical original source
        binding, and it is valid (rules 3–4 below), with
        `P.created_from_turn_id = P.source_turn_id`.
   4. Any foreign appended entry anywhere in that chain makes C stale. There
      is no backward transcript scanning or semantic history search: at most
      four tail entries and one predecessor row are read.
   5. Chain participation never makes a turn a source. U0 and U2 are never
      JD or search sources. For VACANCY_SOURCE_REQUIRED the JD source is
      only the qualifying **current** message (§6.4 step 3).
   - The answer is therefore the very next **appended** turn. Any turn
     appended by another tab or session on the same conversation makes the
     clarification stale.
   - `created_turn_version` stays provenance only. `turn_version` stays the
     #85/D-089 concurrency authority. `question_turn_id` stays the
     current-question append-order authority.
   - In-place rewrites that append nothing do not affect liveness, although
     they bump `turn_version`. This covers the D-045 display sync, lane-B
     confirm (`mark_pending_job_draft_confirmed`) and lane-B review
     resolution (`replace_pending_job_draft`).
   - Invariant required of every transcript writer: an in-place rewrite
     preserves each entry's `turn_id`, order and role. The 100-entry bound
     only drops entries from the front, so the last two entries are always
     retained.
   - This is evaluated under the conversation row lock in the resuming
     turn, and again in Phase B.
3. The transcript entry with `turn_id == source_turn_id` exists, has role
   `user`, and SHA-256 of `text[source_start:source_end]` equals
   `source_sha256`.
4. `semantic_policy_version` and `routing_policy_version` equal the running
   code's versions. Re-running `analyze_hr_text` on the span must still
   yield ≥1 material requirement and no PROHIBITED span.
5. Answer schema version is known.

Rule 2 makes the 100-turn transcript bound irrelevant: the question is the
last entry and the source is at most four entries back (A2). The server never rebuilds the source from model memory or
from "the latest user message that looks like a requirement".

### 6.3 Answer schema (server-owned)

```text
SEARCH_OR_VACANCY        → CANDIDATE_SEARCH | VACANCY_ANALYSIS
VACANCY_SOURCE_REQUIRED  → the next message is the source (slot fill), no choice enum
```

Resolution sources are closed: `BUTTON`, `LABEL`, `MODEL`, and (A2)
`SOURCE_MESSAGE`.
- `SOURCE_MESSAGE` is the **deterministic server-side** resolution source
  for one case only: a VACANCY_SOURCE_REQUIRED clarification satisfied by the
  user's current qualifying source message (§6.4 step 3).
- That exact current message becomes the JD source, recorded with
  `resolved_value = VACANCY_ANALYSIS`.
- It is distinct from BUTTON (explicit closed choice), LABEL (closed-label
  table) and MODEL (local classifier). It never implies model-authored
  source text.

`ClarificationAnswer` is a closed StrEnum. Allowed answers per type are a
code constant, versioned by `answer_schema_version`. The model never widens
it.

### 6.4 Resolving the next message (deterministic order)

With a live OPEN clarification in this session context:

1. **Button.** The form posts `clarification_id` + `clarification_choice`.
   Both must equal the live pointer and be an allowed value. If not, the
   turn fails closed with "this question is no longer active" copy. It
   **never** falls back to interpreting the text.
   - (A2) A stale, foreign or mismatched button is a **rejected request,
     not a clarification transition**:
     - no transcript entry is appended, so the live clarification cannot be
       staled;
     - the live clarification is not superseded and no attempt is consumed;
     - `active_clarification_id` is not cleared or replaced;
     - no capability runs;
     - the submission ends ABANDONED (#85/#87 not-run semantics), and the
       re-rendered page carries a fresh submission token;
     - bounded structural audit only: `agent.clarification.rejected` with
       a closed reason (`NOT_ACTIVE`, `INVALID_CHOICE`). The #87 request hash
   covers message + choice + clarification id, so a replay with a different
   choice fails closed.
2. **Closed-choice label match (deterministic).**
   - The text is folded (`fold_az_ascii(normalize_azerbaijani_case)`) and
     tokenized.
   - It must contain exactly one answer's label tokens, and nothing else
     beyond a bounded filler set (e.g. "kimi", "as", "please",
     "zəhmət olmasa").
   - Label table `clarification-answers-v1`:
     - CANDIDATE_SEARCH = {namizəd, namizədlər, candidate(s)} ×
       {axtarış(ı), axtar, search};
     - VACANCY_ANALYSIS = {vakansiya, vacancy, job} ×
       {kimi, tələb(i), requirement, analiz, analysis}, plus a bare
       `vakansiya` / `vacancy`.
   - This covers the reviewed AZ, EN and mixed answers. The table is bounded
     (≤24 tokens per type), reviewed and versioned.
   - The match is whole-message and strict, so it runs before new-task
     detection without ever swallowing a real new request.
3. **Clear new task (deterministic).**
   - `route_agent_entry` returns FORCE_JOB_DRAFT, FORCE_CANDIDATE_SEARCH or
     FORCE_RESULT_LIMIT, or the message itself has ≥1 material requirement.
   - Result: the old clarification becomes **SUPERSEDED**, its task becomes
     CANCELLED, and the message is handled as a normal new turn.
   - For VACANCY_SOURCE_REQUIRED, a message with material requirements or
     useful multi-line vacancy structure, or a FORCE_JOB_DRAFT route, is the
     **slot fill**, not a new task. It resolves (`SOURCE_MESSAGE`) and runs
     ANALYZE_VACANCY over that exact current message, or over the router's
     exact source span for FORCE_JOB_DRAFT.
   - For VACANCY_SOURCE_REQUIRED, the clear new tasks are only
     FORCE_CANDIDATE_SEARCH and FORCE_RESULT_LIMIT. A message that is
     neither a qualifying source nor a clear new task is handled
     deterministically as **UNCLEAR** (step 5). No model is called for this
     type.
4. **Local model classifier.**
   `LLMProvider.resolve_clarification_answer(type, allowed_answers,
   answer_text)` returns a strict `ClarificationAnswerProposal(value ∈
   allowed ∪ {NEW_REQUEST, UNCLEAR})`.
   - The model gets only the answer text and the closed options. It does
     **not** get the source text or the transcript.
   - An allowed value resolves the clarification.
   - NEW_REQUEST is handled as in step 3 (supersede, then a normal turn).
   - Only a successfully returned, valid closed `UNCLEAR` result → step 5.
   - (A2) **Classifier infrastructure or contract failure is not UNCLEAR.**
     This covers INFERENCE_BUSY, timeout, a transport or provider error,
     Ollama unavailable, malformed output after the one allowed repair, and
     any other classifier execution failure. The turn uses the existing
     #85/#87 abandon semantics:
     - no transcript entry is appended (the clarification is not staled);
     - no supersession, no attempt increment, no attempt 2;
     - no task state change;
     - the submission ends ABANDONED, and HR sees the fixed
       temporary-unavailability copy with a fresh token;
     - the live clarification stays OPEN and answerable for the retry.
   - VACANCY_SOURCE_REQUIRED does not use the classifier (step 3).
5. **Unclear.**
   - The old clarification becomes SUPERSEDED (`superseded_reason =
     UNCLEAR`, `superseded_by_id` = the new row) by a new clarification with
     the **same source binding** and `attempt + 1` (same task, still
     WAITING_CLARIFICATION).
   - The new row's `created_from_turn_id` is this unclear answer's user
     entry, and its `question_turn_id` is the re-asked question entry. Both
     are appended by the same submission, so the A2 chain (§6.2 rule 2) holds.
   - The assistant repeats the question with buttons.
   - This applies to both resumable types. For VACANCY_SOURCE_REQUIRED the
     source fields stay NULL, and the unclear answer U2 is never a source.
   - Infrastructure failures never reach this step (step 4).
   - If the next attempt would be 3, the clarification becomes EXPIRED and
     the task FAILED_SAFE, with copy asking the user to rephrase the request.
     **No capability executes** on an unclear answer.

This is one documented policy: **supersede on any appended turn that is
not an answer**. A2 adds two cases that append nothing and therefore change
no clarification state: a rejected button (step 1) and a classifier
infrastructure failure (step 4). There is no "retained in the background" clarification, and no
hidden guessing.

### 6.5 Why a label table is not "regex creep"

It matches only the server's own closed-choice labels (what the buttons
say), in the tested languages. It is scoped to one clarification type. It
never produces criteria or search text and never routes a new task. Anything
it does not match goes to the bounded model classifier with a closed output.

### 6.6 Resumption (exact transition)

Resolved `CANDIDATE_SEARCH`:
- Task becomes `CANDIDATE_SEARCH`, the server builds the single-step plan
  `SEARCH_CANDIDATES(query = source span text)` (verified per §6.2).
- This is the same path as today's FORCE_CANDIDATE_SEARCH: the user's own
  text goes to the frozen D-031 planner.

Resolved `VACANCY_ANALYSIS`:
- Task becomes `VACANCY_ANALYSIS`, the server builds the single-step plan
  `ANALYZE_VACANCY(source = source span)`.
- This is the same path as FORCE_JOB_DRAFT: a source-bound, review-only
  pending draft.

On success:
- clarification RESOLVED (`resolved_value`, `resolution_source`,
  `resolved_by_submission_id`);
- pointer cleared;
- task COMPLETED (search) or WAITING_CONFIRMATION with
  `pending_draft_id` (vacancy).

The answer text itself ("namizəd axtarışı") is stored in the transcript as
the user's turn, but it is **never** used as search or JD input.

### 6.7 Expiry, concurrency, sessions, replay

- **TTL.** `agent_clarification_ttl_seconds`, default 1800, bounds 60–3600,
  and never beyond the BrowserSession expiry. Staleness is also forced by
  rule 2 of §6.2 (next appended turn only).
- **Cardinality.** This is per session context, per waiting lane (§4.4):
  at most one OPEN clarification and one WAITING_CLARIFICATION task in the
  dialogue lane. A new dialogue-lane task supersedes the old one (T7).
  Independently, the mutation-confirmation lane may hold one
  WAITING_CONFIRMATION vacancy task. A new clarification or a new search
  never cancels it; only a replacing pending draft does (T11).
- **Logout / new BrowserSession.** The new session gets a fresh context
  with pointer NULL. The old question is visible in history, but its
  buttons render disabled (live `can_act` is computed from the pointer, as
  for drafts). Old rows are never answerable.
- **`context_epoch` change** (a new context row) → epoch mismatch → never
  answerable.
- **Expired or superseded clarifications are terminal.** No transition
  leaves SUPERSEDED or EXPIRED. Phase B re-locks the row and requires OPEN
  plus the unchanged pointer, so a late or duplicate answer can never
  execute.
- **#87 refresh / replay.** A COMPLETED submission redirects before any
  execution. A same-token concurrent duplicate gets 409. The UNIQUE
  `resolved_by_submission_id` and `created_by_submission_id` are defense in
  depth.

---

## 7. Task lifecycle (closed state machine)

Persisted statuses: `WAITING_CLARIFICATION`, `WAITING_CONFIRMATION`,
`COMPLETED`, `CANCELLED`, `EXPIRED`, `FAILED_SAFE`.
`ACTIVE` is in-memory only: a turn working on a goal that finishes in the
same turn.

Lane A = dialogue lane (T1–T8); lane B = mutation-confirmation lane
(T3's target, T9–T11); T12 applies to both (§4.4).

| # | Event | Old | New | Authority required | DB mutation point | Audit |
|---|---|---|---|---|---|---|
| T1 | Deterministic clarification created | (none) / ACTIVE | WAITING_CLARIFICATION | live principal + reservation + `candidates:read` | Phase B insert task + clarification + pointer | `agent.task.created`, `agent.clarification.created` |
| T2 | Valid answer (button/label/model), resume succeeds, search | WAITING_CLARIFICATION | COMPLETED | + live OPEN clarification (§6.2) | Phase B | `agent.clarification.resolved`, `agent.task.state_changed` |
| T3 | Valid answer, vacancy draft produced (task moves lane A → lane B; any previous lane-B task gets T11 in the same Phase B) | WAITING_CLARIFICATION | WAITING_CONFIRMATION | + draft pointer set in the same Phase B | Phase B | same |
| T4 | Valid answer, capability returns a truthful failure (e.g. JOB_DRAFT_FAILED, non-executable plan) | WAITING_CLARIFICATION | FAILED_SAFE | same | Phase B | `agent.clarification.resolved`, `agent.task.state_changed` |
| T5 | Unclear answer (valid closed UNCLEAR result, or VACANCY_SOURCE_REQUIRED neither-source-nor-new-task), attempt < 2; either resumable type (A2) | WAITING_CLARIFICATION | WAITING_CLARIFICATION (new clarification row) | same | Phase B | `agent.clarification.superseded(reason=UNCLEAR)`, `agent.clarification.created` |
| T6 | Unclear answer at attempt 2 | WAITING_CLARIFICATION | FAILED_SAFE | same | Phase B | `agent.clarification.expired(reason=ATTEMPTS)` |
| T7 | New task while a clarification is open | WAITING_CLARIFICATION | CANCELLED | same | Phase B of the new turn | `agent.clarification.superseded(reason=NEW_TASK)` |
| T8 | TTL passed, stale source, or version mismatch, observed by a turn | WAITING_CLARIFICATION | EXPIRED | same | Phase B of that turn | `agent.clarification.expired(reason=TTL\|STALE\|VERSION)` |
| T8a (A2) | Clarification classifier infrastructure/contract failure (busy, timeout, provider/transport error, unavailable, malformed after repair) | WAITING_CLARIFICATION | WAITING_CLARIFICATION (**no transition**; nothing appended; attempt unchanged) | — | none (turn abandoned, submission ABANDONED) | existing `agent.turn.busy` / not-committed audit |
| T8b (A2) | Stale/foreign/mismatched clarification button | WAITING_CLARIFICATION | WAITING_CLARIFICATION (**no transition**; request rejected; pointer kept) | — | none (submission ABANDONED) | `agent.clarification.rejected(reason=NOT_ACTIVE\|INVALID_CHOICE)` |
| T9 | Draft modified (new draft id) | WAITING_CONFIRMATION | WAITING_CONFIRMATION (`pending_draft_id` updated) | live pending-draft authority | Phase B | `agent.task.state_changed(same)` |
| T10 | HR confirms draft (`/ui/agent/drafts/{id}/confirm`, CSRF) | WAITING_CONFIRMATION | COMPLETED | existing confirm route scopes + `resolve_pending_draft_authority` | the confirm route's single commit, with `create_job`/`AgentDraftConfirmation` | existing `job.created` + `agent.task.state_changed` |
| T11 | Pending-draft pointer replaced by a new vacancy analysis (forced, or a resolved VACANCY_ANALYSIS clarification) | WAITING_CONFIRMATION | CANCELLED | live pointer | Phase B | `agent.task.state_changed(reason=DRAFT_REPLACED)` |
| T12 | Lazy expiry: task past `expires_at` or context gone | any waiting | EXPIRED | none (maintenance) | retention batch (§13) | `agent.task.state_changed(reason=EXPIRED)` |

Terminal states (COMPLETED, CANCELLED, EXPIRED, FAILED_SAFE) have **no**
outgoing transitions. Turns that end in a waiting state are the only ones
that create a task row. Busy, cancelled, stale and revoked turns commit no
transition at all (§12).

A new search, refinement, lookup or clarification while a vacancy draft
waits for confirmation does **not** cancel it. This preserves today's
behaviour where the pending pointer survives search turns. That is why
there are two independent waiting lanes (§4.4), not one active-task slot.

---

## 8. Capability registry

### 8.1 Contract

```python
class CapabilityName(StrEnum):          # closed; code change + registry entry to extend
    SEARCH_CANDIDATES = ...; REFINE_RESULTS = ...; GET_CANDIDATE_PROFILE = ...
    GET_CANDIDATE_EVIDENCE = ...; ANALYZE_VACANCY = ...; CREATE_JOB = ...
    RANK_JOB_CANDIDATES = ...

@dataclass(frozen=True)
class CapabilityDefinition:
    name: CapabilityName
    policy_version: str                   # e.g. "cap-search-v1"; audited
    input_schema: type[BaseModel]         # Pydantic v2, extra="forbid"
    output_schema: type[BaseModel]        # existing AgentToolResult member types
    required_scopes: frozenset[str]
    side_effect: SideEffect               # NONE | SESSION_WORKING_STATE | DERIVED_RECORDS | BUSINESS_MUTATION
    execution: ExecutionMode              # IN_TURN | HUMAN_ACTION_ONLY
    model_proposable: bool                # may appear in a model plan at all
    live_context: frozenset[LiveContextReq]   # ACTIVE_RESULT_SET | PENDING_DRAFT | CONFIRMED_JOB_IN_SESSION
    produces: frozenset[LiveContextReq]   # e.g. SEARCH produces ACTIVE_RESULT_SET (for step dependencies)
    confirmation: ConfirmationPolicy      # NONE | EXPLICIT_HUMAN_ROUTE
    candidate_content: ContentPolicy      # NONE | PROFESSIONAL_LOCAL_ONLY
    identity: IdentityPolicy              # NEVER_IN_INPUT_OR_MODEL; display-only in UI view layer
    allowed_task_types: frozenset[TaskType]
    executor: Callable[..., Awaitable[CapabilityOutcome]]   # server-bound at import time
    audit: AuditPolicy                    # closed metadata keys only
```

`CAPABILITY_REGISTRY: Mapping[CapabilityName, CapabilityDefinition]` is
built once at import time. A startup/test assertion checks that the registry
keys equal `set(CapabilityName)`, that every executor is a module-level
function in `meyar.agent.capabilities.*`, and that no mutating definition is
`IN_TURN`. The model only ever produces a `CapabilityName` value. It never
names a module or function.

**Enum vs bounded string.**
- The name is a **strict StrEnum** on the server.
- The JSON schema sent to Ollama for each call is generated from the
  registry and lists only the subset that is model-proposable **and**
  permitted for this principal and context (an `enum` constraint).
- An unknown value fails Pydantic → the plan is rejected.
- Extending means adding one enum member and one registry entry. The
  orchestrator is not touched.

This fails closed, and constrained decoding narrows what the small model can
emit.

### 8.2 Execution modes

- `IN_TURN`: executed inside the agent turn, and only if `side_effect` is
  NONE or SESSION_WORKING_STATE (inert ResultSet rows, activated by the
  Phase B pointer).
- `HUMAN_ACTION_ONLY`: the plan step never executes. The validator turns it
  into a **server-derived UI affordance** (an existing CSRF form) only when
  its live target exists. Executing it happens in the existing,
  separately authenticated route.

---

## 9. Initial capability mapping (current services, audited)

| Capability | Current implementation | Input schema | Output schema | Required scopes | Live context | Local model use | Candidate content exposure | Side effect (real) | Confirmation | Deterministic authority | Existing audit / provenance | Change needed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| SEARCH_CANDIDATES | `_dispatch_search` → `plan_and_search_candidates` → `create_result_set_from_search` | `SearchArgs{source: SourceSelection}` (§10.2). The planner gets the whole message or verified exact spans, never model text (replaces `AgentDecision.search_query`) | `AgentSearchToolResult` | `candidates:read` | none; **produces** ACTIVE_RESULT_SET | D-031 planner (LLM), embeddings for hybrid | ranked professional summaries in the UI; no identity to the model | SESSION_WORKING_STATE (new AgentResultSet, inert until pointer) + audit | none | frozen planner validation, prohibited-attribute and no-silent-weakening rules, snapshot | planner audit, `CANDIDATE_SEARCH_EXECUTED`, `agent.result_set.created`, `agent.tool.executed` | wrap as executor; the executor receives server-resolved source text instead of `decision.search_query` (domain service unchanged) |
| REFINE_RESULTS | `_dispatch_refine` → `validate_active_result_set_for_refinement` → `create_result_set_from_refinement` | `RefineArgs{filter_source?: SourceSelection, limit_quote?: SourceQuote}`, ≥1 required; `filter_query` becomes grounded spans, `limit` a server-parsed count (§10.2) | `AgentRefineToolResult` | `candidates:read` | ACTIVE_RESULT_SET | planner for `filter_query` only | subset of snapshot members | SESSION_WORKING_STATE + audit | none | #86 snapshot subset, server order | `agent.result_set.refined` / `refine_rejected` | wrap unchanged (`AgentActionType.REFINE_CANDIDATE_RESULTS` renamed only at the registry layer) |
| GET_CANDIDATE_PROFILE | `_dispatch_profile` → `resolve_active_candidate_ref` → `get_current_authorized_profile` | `ProfileArgs{ref_quote}`: a server-parsed ordinal 1..50 (§10.2 rule 5) | `AgentProfileToolResult` | `candidates:read` | ACTIVE_RESULT_SET | optional D-038 grounded synthesis (`select_grounded_facts`) over accepted professional facts | accepted `CandidateProfileExtraction` (professional only) | NONE (+ audit) | none | ordinal → snapshot → current profile authority | `agent.result_set.reference_resolved` / `reference_rejected` | wrap unchanged |
| GET_CANDIDATE_EVIDENCE | `_dispatch_evidence` (same resolution) | `EvidenceArgs{ref_quote, topic_quote?}`: server-parsed ordinal; topic is an exact grounded slice or absent (§10.2 rule 6) | `AgentEvidenceToolResult` | `candidates:read` | ACTIVE_RESULT_SET | same optional grounded synthesis | accepted evidence items (CV spans, professional) | NONE (+ audit) | none | same | same | wrap unchanged |
| ANALYZE_VACANCY | `_dispatch_draft_job_criteria` + #84 canonicalization | `VacancyArgs{source: SourceBinding}`, **server-built only**: offsets into the current message, or a resumed clarification source | `AgentJobDraftToolResult` (pending draft) | today `candidates:read` (route); **proposed** + `jobs:write` (offer only what HR can complete; no user impact, since both roles hold all scopes) | none; **produces** PENDING_DRAFT | `draft_job_criteria` (LLM) + deterministic canonicalization | JD text only | SESSION_WORKING_STATE (transcript payload + pointer, no Job row) | none (draft is review-only) | source offsets, CanonicalRequirement, the model never supplies source | `agent.tool.executed` + `jd_draft_audit_metadata`, `agent.tool.failed` | wrap unchanged; `model_proposable=False` (a model proposal becomes the SEARCH_OR_VACANCY clarification, §6.1) |
| CREATE_JOB | `confirm_agent_job_draft` route → `resolve_pending_draft_authority` → `create_job` + `create_criteria_version` + `AgentDraftConfirmation` + `mark_pending_job_draft_confirmed` | none from the model; the draft id comes from the live pointer | redirect/render of ranking | `jobs:write`, `jobs:read`, `candidates:read`, `evaluations:write` | PENDING_DRAFT | none | none | **BUSINESS_MUTATION** (Job, JobCriteriaVersion, confirmation link) | **EXPLICIT_HUMAN_ROUTE** (CSRF form) | pending-draft authority, duplicate-job check, unique confirmation | `job.created`, criteria-version audit, `AgentDraftConfirmation` | registry entry `HUMAN_ACTION_ONLY`; the route additionally marks the task COMPLETED (T10) |
| RANK_JOB_CANDIDATES | `/ui/jobs/{v}/rank` (CSRF) and post-confirm `_render_job_ranking` → `rank_candidates_for_job` → `evaluate_and_score_candidate` | none from the model; target `job_criteria_version_id` is server-derived (`AgentDraftConfirmation` of this BrowserSession) | ranking view | `jobs:read`, `candidates:read`, `evaluations:write` | CONFIRMED_JOB_IN_SESSION | **none**: fully deterministic | accepted current profiles, deterministic scores | **DERIVED_RECORDS**: writes `Evaluation` rows (reused by exact provenance tuple), `EVALUATION_STARTED`, `CANDIDATE_SCORE_COMPUTED`, `JOB_BATCH_RANKED` audit. It is **not read-only** | **EXPLICIT_HUMAN_ROUTE** | deterministic policy engine and scoring policy; business date from `resolve_business_date(settings.business_timezone)` at the UI boundary, never from the model | as listed | registry entry `HUMAN_ACTION_ONLY`; in chat, a proposal renders the existing rank form for the session-confirmed job only |

Transitional compatibility: `REFINE_RESULTS` is the registry name for
today's `REFINE_CANDIDATE_RESULTS`, and `ANALYZE_VACANCY` for
`DRAFT_JOB_CRITERIA`. The existing `AgentToolResult.tool_name` values and
audit `tool_name` strings stay stable until slice C (§17). Audits gain
`capability` and `capability_version` keys; existing keys are not
renamed.

---

## 10. Plan proposal schema and bounds

### 10.1 Model output contract (`agent-plan-v1`)

```python
class PlanKind(StrEnum): PLAN = "PLAN"; CLARIFY = "CLARIFY"; CONVERSE = "CONVERSE"

# Grounding primitives (§10.2). The model never writes search
# text; it only points at user-owned text by exact quotation.
class SourceQuote(BaseModel):  extra="forbid"; quote: str (1..500)   # must be an exact, unique substring
class SourceSelection(BaseModel):
    extra="forbid"
    mode: Literal["WHOLE_MESSAGE", "QUOTES"]
    quotes: list[SourceQuote] (0..4)        # QUOTES only, ≥1; WHOLE_MESSAGE ⇒ empty

class SearchArgs(BaseModel):   extra="forbid"; source: SourceSelection
class RefineArgs(BaseModel):   extra="forbid"; filter_source: SourceSelection | None
                                               limit_quote: SourceQuote | None      # ≥1 of the two
class ProfileArgs(BaseModel):  extra="forbid"; ref_quote: SourceQuote               # e.g. "birincinin"
class EvidenceArgs(BaseModel): extra="forbid"; ref_quote: SourceQuote
                                               topic_quote: SourceQuote | None      # e.g. "Python"
class NoArgs(BaseModel):       extra="forbid"   # CREATE_JOB / RANK_JOB_CANDIDATES proposals

class PlanStep(BaseModel):     # discriminated union on `capability`
    capability: CapabilityName            # per-call enum subset (§8.1)
    args: <per-capability args model>

class AgentPlanProposal(BaseModel):
    model_config = {"extra": "forbid"}
    schema_version: Literal["agent-plan-v1"]
    kind: PlanKind
    goal: TaskType | None                 # CANDIDATE_SEARCH | RESULT_FOLLOWUP | VACANCY_ANALYSIS
    steps: list[PlanStep] (max_length=MAX_PLAN_STEPS)     # PLAN only, ≥1
    clarification_code: ModelClarificationCode | None     # CLARIFY only (closed; incl. SEARCH_OR_VACANCY)
    response_code: AgentResponseCode | None               # CONVERSE only (GREETING/ACKNOWLEDGEMENT)
```

The contract has **no field for**:
- a tenant id, BrowserSession id, ResultSet id, candidate UUID, draft id or
  job id;
- a scope or permission, or a confirmation flag;
- an evaluation date, score or weight;
- a free-text answer;
- **any model-authored search, filter, topic or numeric value.**

Every value that drives execution is either a closed code, or an exact
quotation of the current user message that the server resolves to offsets
and parses itself (§10.2). Unknown keys fail `extra="forbid"`. Every string
has a hard bound; the ≤2000-character Ollama grammar limit from D-035 still
applies.

### 10.2 Source grounding of search/refine/lookup inputs (v1 contract)

Principle: **the model chooses *which* capability runs and *which part* of
the user's own words it applies to. It never writes the words.** The frozen
D-031 planner keeps its role of turning source-bound text into validated
structured filters. Faithfulness to the user's request is enforced here,
before the planner, and not by "the planner will validate the derived
query". The planner can only validate what it is given.

Server resolution (pure, in the static layer §11.1, against the canonical
LF current message `M`):
1. **WHOLE_MESSAGE** resolves to `M` exactly. This is the default and
   always safe. It is the same input today's FORCE_CANDIDATE_SEARCH gives
   the planner.
2. **QUOTES** resolve each quote to `M.find(quote)`.
   - The quote must occur **exactly once** in `M`: byte-exact after LF
     canonicalization, no case or diacritic folding.
   - Zero or multiple occurrences reject the plan with `SOURCE_NOT_GROUNDED`.
   - The server computes `(start, end)` offsets and a SHA-256 per span. These
     are server-owned and audited as hashes/offsets only.
   - Spans must be non-overlapping and non-blank. The planner input is the
     spans in source order, joined by a server-owned `"\n"`. No model
     character enters the query.
3. **Requirement coverage.** *(Clarified by accepted Amendment A3.2:
   per coordinated part; `limit_quote` is not a coverage source.)*
   - Run `analyze_hr_text(M)` (policy-versioned, deterministic).
   - Every material requirement (state SCORABLE or NEEDS_HUMAN_REVIEW)
     must have its `subject` occurrence overlap **some** grounding span of
     **some** step in the plan: search/refine source spans, a ref quote or a
     topic quote.
   - Otherwise the plan is rejected with `SOURCE_COVERAGE_INCOMPLETE`. The
     model therefore cannot silently drop a requirement (for example keep
     "Python" and drop "Java").
   - WHOLE_MESSAGE trivially covers everything.
4. **Prohibited content.** If `M` contains any PROHIBITED requirement or
   PROTECTED_CUE role, QUOTES mode is not allowed for SEARCH/REFINE
   (`SOURCE_SELECTION_FORBIDDEN`). Only WHOLE_MESSAGE is accepted, so the
   frozen planner's prohibited-attribute refusal applies to the whole
   request. A span selection can never launder a protected-attribute
   request into a "clean" query.
5. **Numeric grounding** (`limit_quote`, `ref_quote`).
   - The quote is grounded as in rule 2, then parsed **by the server** with
     the closed count parser (`count_token_value`, as already used by
     FORCE_RESULT_LIMIT) or a closed ordinal parser.
   - The ordinal parser covers digits, `#N`, the Azerbaijani ordinal suffix
     over count words (`birinci`, `ikinci`, ... with case suffixes) and
     English `first`…`fifth` / `Nth`, over the range 1..`MAX_CANDIDATE_REF`.
   - The parsed value is the argument. There is no model-supplied integer.
   - A span the parser cannot read (e.g. "sonuncu") rejects the step with
     `REFERENCE_NOT_GROUNDED`. HR gets the existing
     CANDIDATE_REFERENCE_REQUIRED / RESULT_CONTEXT_REQUIRED copy.
   - This ordinal table is a closed numeric vocabulary. It belongs to the
     §3.1 safety-relevant category, not the frozen general-intent regex
     layer.
6. **Evidence topic** (`topic_quote`).
   - It is grounded by rule 2 (exact unique slice of `M`) or absent (all
     evidence of the resolved candidate).
   - Lower risk, but still strict: the topic only narrows which stored,
     accepted evidence items of an already-authorized candidate are shown,
     by the existing case/diacritic-insensitive substring match. An
     ungrounded topic is rejected and is not ignored silently.

Server-built plans are already grounded by construction:
- deterministic FORCE_* routes use the whole message or the router's own
  exact span;
- resumed clarifications use the bound source span (§6.2);
- FORCE_RESULT_LIMIT uses the server-parsed count.

### 10.3 Bounds

| Bound | Value | Notes |
|---|---|---|
| Max plan steps | `MAX_PLAN_STEPS = 3`, effective `min(3, agent_max_tool_calls)`; max 4 quotes per SourceSelection | `agent_max_tool_calls` keeps its truthful meaning: max capability executions per turn |
| Max capability executions per turn | same | a server-built resume plan is exactly 1 step |
| Planner model calls per turn | 1 + 1 repair | replaces `decide_agent_action`; there is **no** post-tool "what next" call |
| Clarification-answer classifier calls | ≤1 + 1 repair, only when an OPEN clarification is unmatched by button/label | |
| Capability-internal model calls | unchanged service bounds (D-031 planner, JD draft `MAX_JD_DRAFT_ATTEMPTS=2`, synthesis `MAX_SYNTHESIS_ATTEMPTS=2`) | all through `BoundaryLLM` / `BoundaryEmbedding` |
| Clarification depth | 2 attempts per source binding (§6.4), then EXPIRED | no nested clarifications |
| Waiting state | per session context: dialogue lane ≤1 WAITING_CLARIFICATION task + ≤1 OPEN clarification; mutation-confirmation lane ≤1 WAITING_CONFIRMATION task (§4.4) | partial unique indexes + pointers |
| Task lifetime | WAITING_CLARIFICATION: clarification TTL (≤3600 s); WAITING_CONFIRMATION: until the pointer changes or the BrowserSession expires (≤`ui_session_ttl_hours`) | |
| Model context turns | `agent_max_context_turns` (default 8), unchanged | |
| Persisted task history | ≤10 terminal tasks per session context (§13) | |

There is no recursion, no model self-invocation, no dynamic tools, and no
open-ended loop. The `while True` loop is replaced by a `for step in
validated_plan.steps` bounded iteration.

---

## 11. Plan validation: static whole-plan layer + dynamic per-step layer

Validation has two explicit layers with different guarantees. Neither
layer mutates business state.

### 11.1 Layer 1 — static whole-plan validation (before any execution)

`validate_plan(proposal, message, ctx: ValidationContext) -> ExecutablePlan |
PlanRejection` is a **pure function**: no DB access, no mutation.

`ValidationContext` is assembled by read-only server calls in a DB phase
just before validation:
- principal scopes;
- lane A: the waiting-clarification task type/phase, if any;
- lane B: whether a WAITING_CONFIRMATION vacancy task / live pending draft
  exists;
- the **pre-existing** active ResultSet status from a read-only validation:
  none, valid with n members, STALE or EXPIRED;
- whether a confirmed job exists in this session;
- running policy/schema versions;
- `analyze_hr_text(message)` for grounding (§10.2).

Checks, in order. The first failure rejects the **whole** plan, and **zero
capabilities execute**:

| Code | Rule |
|---|---|
| `UNSUPPORTED_VERSION` | `schema_version` or a capability `policy_version` unknown |
| `SHAPE_INVALID` | kind/field combination invalid (e.g. PLAN without steps, CONVERSE with steps) |
| `UNKNOWN_CAPABILITY` | not in the registry or not in this call's offered subset (normally already a Pydantic failure) |
| `NOT_MODEL_PROPOSABLE` | e.g. ANALYZE_VACANCY from the model (converted to the SEARCH_OR_VACANCY clarification per §6.1, not executed) |
| `INVALID_ARGUMENTS` | args fail the capability `input_schema` |
| `PLAN_TOO_LONG` | steps > effective max |
| `DUPLICATE_STEP` | identical (capability, resolved grounded args) twice (generalizes today's `searched_queries` guard) |
| `SCOPE_MISSING` | `required_scopes ⊄ principal scopes` |
| `TASK_TYPE_CONFLICT` | a step's capability is not allowed for the plan's goal (lane selection below) |
| `SOURCE_NOT_GROUNDED` / `SOURCE_SELECTION_FORBIDDEN` / `SOURCE_COVERAGE_INCOMPLETE` / `REFERENCE_NOT_GROUNDED` | §10.2 rules 2–6 |
| `PROHIBITED_ATTRIBUTE` | `find_prohibited_term` / PROTECTED_CUE hit in any resolved grounded text (WHOLE_MESSAGE is then passed to the planner, whose refusal is authoritative) |
| `DEPENDENCY_INVALID` | dependency graph shape: a step that consumes ACTIVE_RESULT_SET has neither a pre-existing context nor an **earlier** step whose `produces` includes it; no step consumes a later step's output; at most one producer of ACTIVE_RESULT_SET per plan |
| `RESULT_CONTEXT_REQUIRED` / `RESULT_SET_STALE` / `RESULT_SET_EXPIRED` | a step consuming the **pre-existing** ResultSet (no earlier producer) while it is absent/stale/expired at plan start |
| `CANDIDATE_REF_OUT_OF_RANGE` | for a step consuming the **pre-existing** ResultSet, the parsed ordinal exceeds its known size n. Ordinals against a ResultSet produced by an earlier step **cannot** be range-checked here (Layer 2) |
| `CONFIRMATION_REQUIRED` | a `HUMAN_ACTION_ONLY` capability without its live target → rejected; with target → converted to an affordance, **never executed** |
| `CANDIDATE_CONTENT_POLICY` | a capability whose content policy is not satisfied (reserved for #50 capabilities) |

**Lane selection for `TASK_TYPE_CONFLICT`.**
- A plan exists only for a turn that is *not* a resolved clarification.
  Resolved clarifications get server-built plans for the lane-A task's
  resolved type (§6.6), so no model plan is validated against lane A.
  Otherwise lane A has just been superseded (§6.4 step 3/4).
- A model plan is therefore checked against **its own `goal`**:
  - CANDIDATE_SEARCH allows SEARCH, REFINE, PROFILE, EVIDENCE.
  - RESULT_FOLLOWUP allows REFINE, PROFILE, EVIDENCE.
  - VACANCY_ANALYSIS allows CREATE_JOB and RANK affordances only.
    ANALYZE_VACANCY itself is not model-proposable.
- Lane B is consulted **only** by capabilities whose `live_context` or
  `produces` includes PENDING_DRAFT or CONFIRMED_JOB_IN_SESSION:
  - CREATE_JOB needs the lane-B target to become an affordance;
  - RANK needs the session-confirmed job; *(accepted Amendment A3.3:
    RANK is not model-proposable/offered in chat until an explicit
    server-owned current-confirmed-job selector exists)*
  - a producer of PENDING_DRAFT (server-built only) triggers T11.
- Search, refine, profile and evidence neither read nor modify lane B.

### 11.2 Layer 2 — dynamic per-step precondition validation (immediately before each step)

Run in the DB phase directly before step *k* executes, after the previous
step finished:
1. If step *k* consumes a ResultSet produced by an earlier step in this plan,
   that step must have **succeeded** and its ResultSet must exist, belong to
   this tenant/session/context/epoch, and be unexpired.
2. The resolved ordinal must be ≤ the **actual** member count of the
   consumed ResultSet. With a producer, zero results means step *k* does not
   execute.
3. For a pre-existing ResultSet, authoritative re-validation of
   ResultSet/member snapshot authority (#86) is still current.
4. Any other runtime-dependent context set by prior steps.
5. Then the executor's own authoritative checks run as today, e.g.
   `resolve_active_candidate_ref` and `validate_active_result_set_for_refinement`.
   Layer 2 does not replace them.

Example: `SEARCH_CANDIDATES → GET_CANDIDATE_PROFILE(ref_quote="birincinin")`.
- Layer 1 proves that SEARCH produces ACTIVE_RESULT_SET, that step 2
  depends on it, and that the ordinal is grounded and parses to 1.
- Layer 1 cannot prove that member 1 exists.
- After the search, Layer 2 reads the actual count. If it is 0, the profile
  step does not execute.

### 11.3 Partial-execution semantics (v1: atomic plan)

v1 plans are **atomic with respect to activation**. There is no step
criticality: every step is required.
- **Success.** If every step executes successfully, Phase B activates the
  final produced pointers, commits the transcript and commits task
  transitions as successful.
- **A later step fails its Layer 2 precondition, or its executor returns a
  non-success outcome.** The plan is `PLAN_INCOMPLETE`:
  - no BUSINESS_MUTATION has happened, because IN_TURN steps are only NONE
    or SESSION_WORKING_STATE;
  - no HUMAN_ACTION_ONLY operation executed (it never does in-turn);
  - ResultSet rows created by earlier steps stay **inert**: Phase B does
    **not** activate any pointer produced by this plan, and the previous
    `active_result_set_id` stays exactly as it was. The inert rows fall
    under #86 bounded retention;
  - no task or clarification transition is committed as successful.
    Model plans never resolve lane A. Lane B is unchanged, because
    model plans cannot produce a pending draft;
  - the turn still completes normally (submission COMPLETED, reservation
    cleared). The transcript gets the user turn and an assistant turn with a
    new closed outcome `PLAN_INCOMPLETE` and fixed truthful copy, e.g.
    "Sorğunun bütün addımları icra oluna bilmədi, ona görə nəticə
    aktivləşdirilmədi. Sorğunu hissə-hissə göndərin." No result cards are
    rendered as active;
  - audit keeps bounded attempt/execution provenance: `agent.plan.validated`,
    `agent.tool.executed` for steps that ran, and
    `agent.plan.incomplete(step_index, reason_code)`.
- **Single-step plans** (all server-built plans, and most model plans):
  the step's own truthful outcome (empty search, non-executable planner
  result, JOB_DRAFT_FAILED, RESULT_SET_STALE) is the plan outcome, exactly as
  today. A zero-member search as the *final* step is a completed plan with
  an empty result, just as today.
- Keeping an earlier successful search visible when a later **optional**
  step fails would need an explicit per-step `criticality` field and its
  own decision. It is out of scope for v1.

HR sees fixed product copy per code family (e.g. "Bu sorğunu təhlükəsiz
icra edə bilmədim; zəhmət olmasa dəqiqləşdirin."). They never see raw
codes, capability names or ids. The codes go to audit
(`agent.plan.rejected`, `agent.plan.incomplete`).

---

## 12. Execution model and #85 / Phase B atomicity

### 12.1 Execution

```text
Phase A  lock+reserve, claim submission                      → commit (leave_db)
[if needed] model: clarification classifier                   (no DB)
reenter  revalidate (principal FOR SHARE, conv/context locks)
[if MODEL route] leave_db → model: plan proposal → reenter
Layer 1: validate (pure) on freshly read ValidationContext   → reject = zero execution
for step in plan:            # ≤ 3
    Layer 2: dynamic preconditions for this step               → fail = PLAN_INCOMPLETE, stop
    executor(step)           # may leave_db → local inference → reenter internally
Phase B  final reenter + clarification/task row locks + submission check
         → apply (plan-produced pointers only if the plan completed) → commit
```

- No DB connection, transaction or row lock is held across an Ollama or
  embedding wait. The existing `BoundaryLLM` / `BoundaryEmbedding` wrappers
  are reused. New provider methods (`propose_agent_plan`,
  `resolve_clarification_answer`) are called through the same wrapper and the
  same #85 admission gate.
- Same-conversation concurrency stays governed by the #85 reservation and
  the #87 submission. No queue, worker or distributed component is added.
- Each executor is responsible only for its own service call. The
  orchestrator owns ordering, bounds and the `produces` / `live_context`
  hand-off. For example, a SEARCH step's new ResultSet id is placed in the
  in-turn `TurnSessionState`, so a following PROFILE step resolves ordinals
  against it, exactly as the eager sync works today. That in-turn value is
  **staged**. It becomes the committed pointer only through Phase B, and only
  for a completed plan (§11.3).

### 12.2 Phase B atomicity

**Pattern: staged in memory, written only in Phase B.** ResultSet rows
already use the "inert row + pointer activation" variant and keep it.

`AgentTurnCommit` grows:
- `task_changes`: insert or status update, typed;
- `clarification_changes`: insert new, or transition existing
  `(id, expected_status=OPEN, new_status, resolution fields)`;
- `active_clarification_id`: the new pointer value.

`TurnReservation` additionally snapshots `active_clarification_id`.

In Phase B, after `revalidate_reserved_turn` (which checks that the pointer
is unchanged, extending the existing CONTEXT_CHANGED check), the server:

1. locks the referenced clarification row and waiting task rows
   `FOR UPDATE`, **after** the conversation/context rows (lock order:
   principal → conversation → session context → clarification → task →
   submission);
2. re-verifies each staged transition's precondition (still OPEN, still
   unexpired, `question_turn_id` still the last transcript entry per §6.2
   rule 2). For a `PLAN_INCOMPLETE` turn it
   stages **no** plan-produced pointer and no successful task transition;
3. writes transcript (with `turn_id`s), ResultSet/draft/clarification
   pointers, task/clarification rows, and task/clarification audit events;
4. marks the submission COMPLETED and clears the reservation, then
   **commits once**.

A revoked, stale, busy, cancelled or replayed turn never reaches step 3. The
existing `abandon_reserved_turn` rolls back and clears only the reservation.
It therefore leaves **no** actionable clarification, task transition or
pointer behind. Audit events for plan proposal/rejection are
attempt-provenance and may commit earlier, like today's `agent.entry.routed`.
Task and clarification events are committed only with the state they
describe.

---

## 13. #87 idempotency integration

- A COMPLETED replay of a submission redirects to the canonical GET before
  Phase A work, so there is no second planner call, capability execution or
  state change. This is the existing behaviour and it is preserved.
- A same-token concurrent duplicate gets 409 from the reservation and the
  PROCESSING claim.
- Defense in depth in the schema:
  - `agent_tasks.created_by_submission_id` UNIQUE;
  - `agent_clarifications.created_by_submission_id` UNIQUE;
  - `agent_clarifications.resolved_by_submission_id` UNIQUE.
  A hypothetical double Phase B for one submission fails on constraint
  rather than creating a second task/clarification or resolving twice.
- Pending drafts (`active_pending_draft_id`), ResultSets (pointer) and Jobs
  (`AgentDraftConfirmation` unique) keep their existing idempotency. A
  replay cannot replace a draft, add a ResultSet or create a job.
- The button choice is part of the submission request hash (§6.4), so an
  altered-choice replay fails closed like altered bytes do today.
- A new server-issued submission token is a new intentional turn. With an
  OPEN clarification, it is resolved by §6.4.

---

## 14. Session and ResultSet authority (#49 / #80 / #86)

Candidate reference authority is unchanged:

```text
BrowserSession → conversation session context → active_result_set_id
→ AgentResultSet (tenant/session/conversation/epoch/expiry)
→ member snapshot (ordinal) → current professional authority
```

- Task and clarification rows store **no** candidate UUID, ordinal
  selection, ResultSet id or draft id as authority. `pending_draft_id` on a
  task is only a reference, actionable only when it equals the live pointer.
- A new BrowserSession inherits nothing:
  - no active ResultSet or ordinals;
  - no pending draft;
  - no open clarification;
  - no waiting task.
  The durable conversation history stays visible and read-only.
- The only state that could survive a session is semantic task metadata.
  #88 deliberately keeps **none** of it across sessions, because tasks are
  session-context-bound. A future cross-session "resume my vacancy work"
  would need its own decision and would still re-establish live authority
  from scratch.

---

## 15. Memory model and model-context projection

A. **Transcript**: existing, ≤100 turns, plus `turn_id` for new entries.
B. **Structured state**: tasks and clarifications, typed and versioned,
   session-bound.
C. **Model context**: a projection built per call, never stored.

There is no infinite memory and no RAG or embeddings over conversation
history (separate future scope). The full conversation is never sent.

Projection for `propose_agent_plan`:
- the last `agent_max_context_turns` `(role, text)` pairs (unchanged);
  *(accepted Amendment A3.1: assistant turns project only the closed
  `AgentTurnOutcome` code, never persisted display text)*
- `available_capabilities`: the per-call enum subset, as names plus
  one-line server descriptions;
- `active_result_context_present: bool`,
  `available_candidate_refs: [1..n]` (unchanged semantics);
- `waiting_clarification`: `{task_type, phase}` closed codes, or null
  (lane A);
- `pending_vacancy_confirmation`: `{present: bool}` (lane B).
No task id, clarification id or draft id is ever sent. Both facts are
advisory only; the validator re-reads the lanes (§11).

Projection for `resolve_clarification_answer`:
- the answer text;
- the clarification type code;
- the allowed answer codes.

The projection **excludes**:
- CandidateIdentity (name/contact);
- every UUID (tenant, user, session, conversation, ResultSet, draft, task,
  clarification, candidate);
- security/CSRF/submission tokens;
- scopes (only the derived capability subset is sent);
- audit data;
- the clarification's source text (not needed for classification);
- chain-of-thought (never requested or stored).

"Ignore identity" is not the only protection. Identity is structurally
absent from every model input type, and the prompt-projection builders are
typed (Pydantic) with an allow-list, not a deny-list.

---

## 16. Factuality, identity and scoring safety

- **Factuality is unchanged.** Assistant text stays server-authority text
  (`ASSISTANT_TEXT_AUTHORITY_SERVER`):
  - closed response codes become fixed copy;
  - tool results are rendered deterministically;
  - D-038 grounded synthesis still selects only from accepted professional
    facts and renders deterministically.
  The plan contract has **no** free-text answer channel. Any new factual
  channel (#50) needs its own reviewed capability.
- **Identity.** No capability input, plan field, task state, clarification
  row, audit event or model projection contains CandidateIdentity. The UI
  view layer may display names to authorized HR users after execution, as
  today. Identity never feeds search, matching or ranking.
- **Scoring and hiring.**
  - The LLM never authors a numeric score, weight, fit band, winner or
    hiring decision.
  - RANK_JOB_CANDIDATES has no model arguments. Its date comes from the UI
    boundary's business date.
  - `HIRING_DECISION_REQUIRES_HUMAN` stays a closed response.
  - Agent Core may *explain* deterministic evaluation output through
    existing server rendering. It never changes policy.
- **Local-only AI.** All new model calls go through `meyar.llm.LLMProvider`
  to local Ollama under the shared #85 admission gate. There is no external
  AI or embedding API.

---

## 17. #50 extension contract (not implemented)

A future `ANSWER_CANDIDATE_QUESTION` / `COMPARE_CANDIDATES` plugs in by:
1. adding enum members and `CapabilityDefinition`s with
   `live_context={ACTIVE_RESULT_SET}`, grounded-quote args only (§10.2),
   `candidate_content=PROFESSIONAL_LOCAL_ONLY` and
   `identity=NEVER_IN_INPUT_OR_MODEL`;
2. an executor under `meyar.agent.capabilities` that resolves ordinals via
   `resolve_active_candidate_ref` and reads accepted profile/evidence only;
3. its own validated output schema and factuality review.

The orchestrator, validator and plan schema stay generic. The per-call enum
subset and `PlanStep` discriminated union grow by registration only. There
is no router edit and no bypass of ResultSet or evidence authority.

---

## 18. Migration design (shape only, no revision assigned)

Chained after `a87d4c6e2b19`, single head:

- **`agent_tasks`**:
  - columns per §4.3;
  - CHECKs on `task_type`, `status`, `phase`, `state_schema_version`, and
    status/terminal_at pairing (terminal ⇔ `terminal_at` set);
    `pending_draft_id` non-null ⇔ WAITING_CONFIRMATION;
  - FKs: tenant CASCADE, conversation CASCADE, session_context CASCADE,
    owner user/membership NO ACTION (as conversation);
  - UNIQUE(`created_by_submission_id`);
  - partial UNIQUE(`session_context_id`) WHERE status =
    'WAITING_CLARIFICATION'; partial UNIQUE(`session_context_id`) WHERE
    status = 'WAITING_CONFIRMATION';
  - index (`tenant_id`, `session_context_id`, `terminal_at`) for retention.
- **`agent_clarifications`**:
  - columns per §4.3;
  - CHECKs on type, status, attempt 1..2, resolved_value ⇔ RESOLVED,
    resolution_source ⇔ RESOLVED, `0 ≤ source_start < source_end ≤ 4000`;
  - `source_turn_id`, `source_sha256` and offsets are all required for
    SEARCH_OR_VACANCY and all NULL for VACANCY_SOURCE_REQUIRED;
  - `question_turn_id` NOT NULL (A1);
  - `created_from_turn_id` NOT NULL (A2). For SEARCH_OR_VACANCY at
    attempt 1, CHECK `created_from_turn_id = source_turn_id`. For
    VACANCY_SOURCE_REQUIRED, the source fields are NULL at every attempt;
  - `superseded_reason` IN (`UNCLEAR`, `NEW_TASK`) and non-null ⇔
    `status = SUPERSEDED` (A2);
  - `resolution_source` IN (`BUTTON`, `LABEL`, `MODEL`, `SOURCE_MESSAGE`)
    (A2);
  - FKs: tenant/conversation/session_context/task CASCADE; `superseded_by_id`
    self SET NULL;
  - UNIQUE(`created_by_submission_id`), UNIQUE(`resolved_by_submission_id`);
  - partial UNIQUE(`session_context_id`) WHERE status = 'OPEN'.
- **`agent_conversation_session_contexts.active_clarification_id`**:
  nullable FK → agent_clarifications SET NULL.

Legacy behaviour and compatibility:
- **Legacy conversations:** no rows are created. Old `CLARIFICATION_REQUESTED`
  turns stay plain history and are never resumable. Old transcript entries
  get no `turn_id`, so no fabricated task or clarification history exists.
- **Upgrade:** additive DDL only. Existing rows are valid with NULL pointers.
  Old application code ignores the new tables and column.
- **Downgrade:** representable. It drops the pointer column, then the
  clarifications, then the tasks. This is safe and fail-closed:
  - these are session-scoped working state, and audit provenance is
    retained;
  - losing an OPEN clarification only means the next answer is routed as
    today's un-resumed message;
  - losing a WAITING_CONFIRMATION task leaves the existing pending-draft
    pointer, and therefore confirmation, intact;
  - nothing on the downgraded schema can resume a dropped clarification.
  The migration logs the dropped row counts (counts only).

This fits the agentless configure → migrate → start → healthcheck →
backup/restore → update/rollback path. There is no new service, runtime or
manual edit.

---

## 19. Retention and audit

### 19.1 Retention (bounded, no daemon)

- On creation of a task in a session context, retire (DELETE, bounded batch
  ≤20) terminal tasks beyond the newest 10 for that context.
  Clarifications cascade with them.
- Expiry is marked lazily. Any turn in the context that observes an
  expired/stale OPEN clarification transitions it in its own Phase B (T8).
  GET renders compute `can_act` from `expires_at` without writing.
- BrowserSession rows are never deleted on `fb03477`, so contexts of
  expired sessions accumulate. A tenant-scoped agentless command
  `meyar retire-agent-tasks --tenant-id … --max-batches …` (the same shape
  as `meyar retire-result-sets`) retires tasks/clarifications whose
  BrowserSession has expired. It is optional in slice A and required before
  pilot.
- Durable conversation history is **never** deleted by task cleanup. Audit
  events and consequential provenance (`AgentDraftConfirmation`, Job,
  Evaluation) are never deleted by it.

### 19.2 Audit vocabulary

This extends the existing `agent.<noun>.<verb>` naming. Existing events are
kept.

| Event | When | Metadata (closed keys only) |
|---|---|---|
| `agent.task.created` | Phase B | `task_type`, `status`, `policy_version` |
| `agent.task.state_changed` | Phase B / confirm route / retention | `from_status`, `to_status`, `reason_code` |
| `agent.clarification.created` | Phase B | `clarification_type`, `attempt`, `answer_schema_version` |
| `agent.clarification.resolved` | Phase B | `clarification_type`, `resolved_value`, `resolution_source` |
| `agent.clarification.superseded` | Phase B | `reason_code` (NEW_TASK/UNCLEAR) |
| `agent.clarification.expired` | Phase B / retention | `reason_code` (TTL/STALE/VERSION/ATTEMPTS/SOURCE_MISMATCH) |
| `agent.clarification.rejected` | attempt | `reason_code` (INVALID_CHOICE/NOT_ACTIVE) |
| `agent.plan.validated` | attempt | `plan_sha256`, `step_count`, `capabilities` (closed codes), `schema_version` |
| `agent.plan.rejected` | attempt | `reason_code`, `schema_version` (Layer 1) |
| `agent.plan.incomplete` | attempt | `step_index`, `reason_code` (Layer 2 / executor non-success; nothing activated) |
| `agent.tool.executed` (existing) | as today | + `capability`, `capability_version` |
| `agent.entry.routed` / `agent.entry.action_rejected` (existing) | unchanged | unchanged |

Never logged or audited:
- raw HR text, JD body, CV text or evidence text;
- CandidateIdentity;
- raw model output or chain-of-thought;
- search query text;
- tokens.

`plan_sha256` is computed over the canonical JSON of the *validated* plan,
with every quote replaced by its server-resolved offsets and span SHA-256.
It contains no HR text, UUIDs or identity.

---

## 20. UX contract (SSR/Jinja, no SPA)

- **Clarification turn:**
  - the assistant bubble shows the fixed question;
  - under it, closed-choice buttons ("Namizəd axtarışı", "Vakansiya tələbi
    kimi") in a small CSRF form posting to `/ui/agent` with a fresh #87
    submission token, `clarification_id`, `clarification_choice` and
    `message` = the button label (so the transcript reads naturally);
  - the normal composer stays active for a typed answer;
  - buttons render only while the live pointer points at that clarification
    (`can_act`), and are otherwise shown disabled with "Bu sual artıq aktiv
    deyil".
- **Resumed turn:**
  - the assistant headline states continuity in server copy, e.g.
    "“Python mütləqdir.” tələbi üzrə namizəd axtarışı:", quoting the bound
    source span (HR's own text, escaped);
  - then the normal search/draft result.
- **Stale or expired answer:** fixed copy asking HR to resend the original
  request. No execution happens.
- **Never shown:** UUIDs, capability names, plan or task ids, reason codes,
  state-machine labels.
- **JavaScript is not required.** Button forms work without JS; progressive
  enhancement only disables double-submit (as today).

---

## 21. End-to-end scenarios (state + authority traces)

**A. `Python mütləqdir.` → clarification → `namizəd axtarışı` → search.**
- T1: the router returns CLARIFY_AMBIGUOUS. Phase B commits:
  - user turn `u1` (turn_id U1);
  - task τ (UNDETERMINED, WAITING_CLARIFICATION, NEEDS_INTENT_CHOICE);
  - clarification κ (SEARCH_OR_VACANCY, OPEN, source U1 [0,17),
    sha256(“Python mütləqdir.”), `question_turn_id = Q1`,
    `created_turn_version = v` as provenance);
  - pointer `active_clarification_id = κ`;
  - assistant question with buttons (turn_id Q1).
- T2: `namizəd axtarışı` goes through §6.4:
  - no button;
  - the step 2 whole-message label match gives CANDIDATE_SEARCH (steps 3
    and 4 are not reached).
- Checks pass:
  - pointer = κ, OPEN, unexpired, epoch equal;
  - the last transcript entry is Q1, directly preceded by U1;
  - U1's hash matches;
  - versions equal;
  - analysis still finds a requirement.
- The server plan is `SEARCH_CANDIDATES(query = "Python mütləqdir.")`, run
  through the frozen planner, producing a new ResultSet row (inert).
- Phase B:
  - κ RESOLVED(CANDIDATE_SEARCH, LABEL, resolved_by = s2);
  - pointer cleared;
  - τ CANDIDATE_SEARCH → COMPLETED;
  - `active_result_set_id` = the new set;
  - transcript appended.
- The words "namizəd axtarışı" are never search input.
- EN (`Python is mandatory.` → `candidate search`) and mixed
  (`Python mandatory-dir.` → `namizəd search`) follow the identical path.

**B. Same original → `vakansiya kimi` → vacancy.**
- The label match gives VACANCY_ANALYSIS, and the server plan is
  `ANALYZE_VACANCY(source = U1[0,17))`.
- #84 produces the source-bound draft D1.
- Phase B:
  - κ RESOLVED;
  - τ VACANCY_ANALYSIS → WAITING_CONFIRMATION(`pending_draft_id = D1`);
  - `active_pending_draft_id = D1`;
  - no Job exists.

**C. Clarification open → unrelated new task** (`Java bilən namizədləri
göstər`).
- §6.4: no label match (step 2); step 3 finds FORCE_CANDIDATE_SEARCH, a
  clear new task.
- In the same Phase B:
  - κ SUPERSEDED(NEW_TASK);
  - τ CANCELLED;
  - pointer cleared;
  - the Java search executes as a normal turn.
- κ can never be answered afterwards.

**D. Expired clarification → late answer.**
- κ `expires_at` has passed, or another tab or session appended a turn to
  the conversation (Q1 is no longer last), or the session changed. The late `namizəd axtarışı` fails §6.2.
- Phase B:
  - κ EXPIRED(TTL/STALE);
  - τ EXPIRED;
  - pointer cleared;
  - fixed "resend the requirement" copy.
- No capability executes. If κ belonged to another BrowserSession, this
  context never pointed at it: the text is simply a new turn, and the
  foreign row is untouched.

**E. Search → `bunlardan SQL bilənləri` → refine.**
- MODEL route. The plan is
  `[REFINE_RESULTS(filter_source=QUOTES["SQL bilənləri"])]`.
- Layer 1:
  - the quote is an exact unique slice of the message, and the server
    computes [10,23);
  - coverage holds: the only material requirement subject (`bunlardan SQL`,
    [0,13)) overlaps the span;
  - `candidates:read` is held;
  - the pre-existing ACTIVE_RESULT_SET is present and VALID n.
- Layer 2 re-validates the pre-existing ResultSet authority immediately
  before the step.
- The executor runs today's `_dispatch_refine` with the **server-resolved
  span text** as `filter_query`: a subset of the #86 snapshot and a new
  REFINEMENT ResultSet. Phase B switches the pointer.
- The plan cannot name a ResultSet id or candidate id, and cannot author
  filter text.

**F. `birincinin sübutunu göstər` → evidence.**
- The plan is `[GET_CANDIDATE_EVIDENCE(ref_quote="birincinin")]`.
- Layer 1:
  - the quote is grounded, and the server's ordinal parser gives 1;
  - 1 ≤ n of the pre-existing ResultSet.
- A model-supplied integer does not exist in the contract. A quote the
  parser cannot read gets REFERENCE_NOT_GROUNDED and the existing
  "which candidate?" copy.
- The executor calls `resolve_active_candidate_ref` (authoritative:
  tenant/session/epoch/expiry/member snapshot/current profile), then the
  accepted evidence, then optional grounded synthesis.
- If the ResultSet became STALE after validation, the executor returns the
  existing RESULT_SET_STALE outcome.

**G. Vacancy analyzed → model proposes CREATE_JOB.**
- A pending draft D1 is live. The plan is `[CREATE_JOB()]`: the registry
  says HUMAN_ACTION_ONLY and the live PENDING_DRAFT target exists.
- The validator converts the step into an affordance and executes nothing.
- τ stays WAITING_CONFIRMATION, and the response renders the existing
  confirm form for D1.
- Only HR's separate CSRF POST to `/ui/agent/drafts/D1/confirm` creates the
  Job (T10), in its own transaction, and marks τ COMPLETED there.

**H. Unknown capability** (e.g. `DELETE_CANDIDATE`, or a registered name
outside the per-call subset).
- Pydantic enum failure → one repair → still invalid → MALFORMED_MODEL_OUTPUT
  (today's outcome).
- If it parses but is not offered in this call, the result is
  `agent.plan.rejected(UNKNOWN_CAPABILITY/NOT_MODEL_PROPOSABLE)`.
- Zero executions and no state change except the attempt audit.

**I. CREATE_JOB without a live draft.**
- The validator returns CONFIRMATION_REQUIRED (no target) and rejects the
  plan.
- Zero mutation: no Job, no criteria version, no confirmation row.

**J. Two tabs / same submission replay.**
- Same token concurrently: the second request gets 409 (#85 reservation /
  #87 PROCESSING).
- COMPLETED replay: redirect, no execution.
- Two tabs with *different* tokens answering the same κ:
  - both share the session context, and conversation-level reservation
    serializes them;
  - the first resolves κ and appends its turn pair (Q1 is no longer last);
  - the second then fails §6.2 rule 1/2 (κ no longer OPEN or the pointer
    changed) and gets the "no longer active" copy.
- Exactly one resolution and one execution. The UNIQUE submission columns
  are the backstop.

**K. Session revoked during inference.**
- Phase B's principal FOR SHARE check (D-091) raises PRINCIPAL_REVOKED.
- `abandon_reserved_turn` rolls back.
- No transcript, task or clarification transition, and no ResultSet, draft
  or clarification pointer.
- κ stays as it was, and is unreachable anyway, since the session is dead.

**L. New BrowserSession opens the same conversation.**
- `get_or_create_session_context` creates a new context (new epoch) with
  all pointers NULL.
- History, including the old question and old results, renders read-only.
  Buttons are disabled and `available_candidate_refs = []`.
- An answer typed now is a new turn. An ordinal gets
  RESULT_CONTEXT_REQUIRED. Drafts are not confirmable.

**M. Two-step plan whose dependent step cannot run (atomic plan).**
- `Kotlin bilən namizəd tap və birincinin profilini göstər`.
- Ordinal language routes it to MODEL_ROUTED today (the result-context
  check precedes the search imperative).
- The plan is `[SEARCH_CANDIDATES(QUOTES["Kotlin bilən namizəd"]),
  GET_CANDIDATE_PROFILE(ref_quote="birincinin")]`. Every material subject
  overlaps one of the two grounded spans.
- Layer 1 passes: the dependency is SEARCH → produces → PROFILE, and the
  ordinal is grounded (1). It **cannot** check that member 1 exists.
- Step 1 runs and produces an inert ResultSet R with 0 members.
- Step 2's Layer 2 check: the actual count is 0 < 1, so the step does not
  execute and the result is `PLAN_INCOMPLETE`.
- Phase B:
  - transcript user turn + assistant turn with fixed PLAN_INCOMPLETE copy;
  - submission COMPLETED, reservation cleared;
  - `active_result_set_id` **unchanged** (the previous one, if any). R is
    never activated and is retired by #86 retention;
  - no successful task transition.
- Audit: `agent.plan.validated`, `agent.tool.executed` (step 1) and
  `agent.plan.incomplete(step_index=2, reason=CANDIDATE_REF_OUT_OF_RANGE)`.

**N. Model injects a constraint absent from the source.**
- The user writes `Python bilən namizədlər`. A wrong or injected proposal
  is `SEARCH_CANDIDATES(QUOTES["Java bilən namizədlər"])`.
- Layer 1: the quote does not occur in the message, so the plan is
  rejected with SOURCE_NOT_GROUNDED and zero executions. No Java search can
  exist.
- A proposal that quotes only part of a multi-requirement message (e.g.
  `Python` from `Python və Java bilən`) gets SOURCE_COVERAGE_INCOMPLETE.
- Nothing the model writes reaches the planner. Only WHOLE_MESSAGE or exact
  slices do.

**O. Both waiting lanes live.**
- D1 is pending confirmation (lane B), and HR sends `Python mütləqdir.`.
- A SEARCH_OR_VACANCY clarification κ is created in lane A, and D1 is
  untouched.
- `namizəd axtarışı` resolves κ and runs the search. Lane B stays D1.
- If HR had answered `vakansiya kimi`, the resumed ANALYZE_VACANCY produces
  D2 and moves `active_pending_draft_id` to D2. The D1 task is cancelled
  (T11) in the same Phase B, and the new task holds lane B.

---

## 22. Adversarial test plan (future; not written in this PR)

For slices A to C, each item is a failing-first regression:

1. **Unknown capability** in the model output: rejected, zero executor
   calls (spy), zero state.
2. **Invalid args**: extra keys, wrong types, over-length strings, and
   ordinal quotes parsing to 0 or 51.
3. **Over-long plan**: 4 steps are rejected whole; step 1 does not execute.
4. **Missing scope**: a membership role without a scope yields
   SCOPE_MISSING (synthetic role via unknown-role → zero scopes).
5. **Missing ResultSet**: REFINE/PROFILE/EVIDENCE without the pointer.
6. **Stale ResultSet**: a snapshot member's authority changes after
   validation and before execution; the executor rejects it.
7. **Forged candidate ref**: an ordinal beyond size; a UUID-shaped string in
   args is rejected by the schema.
8. **Mutation without confirmation**: CREATE_JOB/RANK in a plan creates
   zero Job/Evaluation rows. CREATE_JOB with a live draft only renders the
   affordance.
9. **Replay**: a COMPLETED replay of a clarification-creating turn, a
   resolving turn and a vacancy turn each give no second
   task/clarification/draft/ResultSet/job.
10. **Same-conversation concurrency**: two tokens answer one clarification;
    exactly one resolves.
11. **Cross-session clarification**: a new BrowserSession cannot answer, and
    a button with a foreign `clarification_id` fails closed.
12. **Cross-tenant task id**: tenant B posts tenant A's `clarification_id`;
    this is indistinguishable not-active, with no state touched.
13. **Expired clarification**: TTL passed, then the answer gives EXPIRED and
    no execution.
14. **Superseded clarification**: after a new task, a button for the old
    clarification fails closed.
15. **Source-turn / hash mismatch**: tampered transcript text, a missing
    `turn_id` or a policy version bump each give STALE and no execution.
15a. **Append-position binding (A1)**:
    - a turn appended after the question (another tab or session) gives
      STALE and no execution;
    - lane-B confirm or review-resolve of D1 between question and answer
      (turn_version bumped, nothing appended) leaves the clarification
      answerable, and it resolves correctly;
    - `created_turn_version` equals the persisted `turn_version` right
      after the creating commit, including the D-045 display sync bump;
    - every in-place transcript writer preserves `turn_id`.
15b. **Exchange-chain retry and failure semantics (A2)**:
    - A. SEARCH_OR_VACANCY attempt 1 UNCLEAR → a valid attempt-2 chain
      `[U1, Q1, U2, Q2]`. The original source binding is unchanged, and the
      resumed planner/JD drafter receives U1's text, never U2's.
    - B. VACANCY_SOURCE_REQUIRED attempt 1 UNCLEAR → a valid attempt-2 chain
      `[U0, Q1, U2, Q2]`. The source fields are still NULL, and a later
      qualifying current message is the only JD source (never U0 or U2).
    - C. Attempt 2 UNCLEAR → clarification EXPIRED, task FAILED_SAFE,
      pointer cleared, no attempt 3, no capability.
    - D. Classifier timeout or provider/transport error/Ollama unavailable →
      turn abandoned. No attempt consumed, no transcript appended,
      clarification and task unchanged and still answerable.
    - E. Malformed classifier output after the one repair → same as D.
    - F. INFERENCE_BUSY → same as D.
    - G. Foreign, stale or mismatched clarification button → request
      rejected, nothing appended, live clarification/pointer/attempt
      unchanged, `agent.clarification.rejected` audited.
    - H. An extra appended turn inside the expected retry sequence (before
      or after Q1, or after Q2) → stale, fail closed.
    - Forged chain cases → stale: predecessor with a different task,
      session context or type; `superseded_reason` ≠ UNCLEAR; attempt not
      1 → 2; SEARCH_OR_VACANCY source binding differing between P and C;
      wrong ids at `T[-4]`..`T[-1]`.
    - Bounded work: liveness reads at most four tail entries and one
      predecessor row.
16. **The model tries to add tenant/session/candidate UUID or scope fields**:
    rejected by `extra="forbid"`; parametrized over each forbidden key.
17. **The model tries a protected attribute** (AZ/EN) in search/refine args:
    PROHIBITED_ATTRIBUTE, no search.
18. **Candidate identity contamination**: synthetic identity fixtures; assert
    that no name/email/phone appears in any model projection, task state,
    clarification row or audit metadata.
19. **External AI / no-exfiltration**: the existing no-exfiltration suites
    are extended to the new provider methods.
20. **Deterministic score unchanged**: the ranking output for fixed
    fixtures is byte-identical before and after the agent path, and no
    model argument reaches `rank_candidates_for_job`.
21. **M-8 language matrix**: AZ, EN and mixed originals × button, label and
    model (fake provider) answers resume with the original source.
22. **Busy / cancel / revoke during a resumed turn**: the clarification is
    still OPEN afterwards and no pointer changes.
23. **#85**: no DB checkout during the plan or classifier model calls
    (pool-probe tests extended).
24. **Source injection** (future required test): the user source says
    Python, and a malicious or wrong plan proposal quotes `Java ...`. The
    plan is rejected (SOURCE_NOT_GROUNDED). A spy on the planner asserts it
    never receives "Java", and a whole-message search remains source-bound
    to Python.
25. **Coverage drop**: `Python və Java bilən` with a plan quoting only
    `Python` gets SOURCE_COVERAGE_INCOMPLETE and zero executions.
26. **Ambiguous/duplicate quote**: a quote occurring twice, or a
    near-match differing in case or diacritics, gets SOURCE_NOT_GROUNDED.
27. **Laundering a protected attribute**: a message with a prohibited cue
    plus a QUOTES selection that omits it gets SOURCE_SELECTION_FORBIDDEN.
    The WHOLE_MESSAGE path yields the planner's prohibited refusal.
28. **Numeric grounding**: `ilk 3` quoted but the model claims 5 is
    impossible by schema. A `limit_quote`/`ref_quote` the parser cannot read
    gets REFERENCE_NOT_GROUNDED. AZ/EN ordinal forms parse to the expected
    value.
29. **Static vs dynamic**: in SEARCH → PROFILE(ref 1) where the search
    returns 0, the profile executor is never called (spy), the result is
    PLAN_INCOMPLETE, and the active pointer is unchanged. The inert
    ResultSet exists but is not active.
30. **Layer 1 zero execution**: a plan whose step 3 fails Layer 1 (e.g.
    SCOPE_MISSING) executes neither step 1 nor step 2.
31. **Lanes**: a clarification created while D1 is pending leaves D1
    confirmable. A search while D1 is pending leaves D1. A resolved
    VACANCY_ANALYSIS replaces D1 → T11. Confirming D1 leaves an open
    clarification answerable. `namizəd axtarışı et` with both lanes live
    resolves the clarification and does not hit the draft amendment branch.

---

## 23. Implementation slices (after design acceptance)

Every slice keeps the product working, passes the full gate, and changes one
concern.

**Slice A: typed clarification/task persistence + resumable clarification.**
- Migration (§18); the `turn_id` in new transcript entries;
  `active_clarification_id` in `TurnReservation`/revalidation; Phase B
  staging (§12.2).
- SEARCH_OR_VACANCY and VACANCY_SOURCE_REQUIRED, the §6.4 resolution order
  (button + label table + `resolve_clarification_answer` provider method
  with fake/Ollama implementations), and resumption through the **existing**
  forced search/draft code paths.
- The confirm route marks WAITING_CONFIRMATION tasks COMPLETED.
- UI buttons, retention on creation, audit.
- Tests: §22 items 9–15, 21, 22, 23 and the isolation tests.
- No registry and no plan contract yet. `AgentDecision` is untouched.

**Slice B: capability registry + validator, behaviour-preserving.**
- `meyar.agent.capabilities` with the seven definitions, whose executors
  wrap the current `_dispatch_*` unchanged.
- The pure Layer 1 `validate_plan`, the Layer 2 per-step precondition hook,
  and the atomic activation rule (§11.3).
- The first grounding step: the adapter passes WHOLE_MESSAGE to the
  planner for model-routed SEARCH_CANDIDATES instead of the model's
  `search_query` (the same input FORCE_CANDIDATE_SEARCH already uses).
  Refine filter, limit and ordinals stay on today's transitional
  model-supplied `AgentDecision` fields until slice C. This is a residual
  that exists on `main` today, not a regression.
- A transitional adapter turns each current `AgentDecision` / forced route
  into a one-step `PlanStep`, so the `if/elif` dispatch becomes registry
  dispatch.
- CREATE_JOB/RANK become `HUMAN_ACTION_ONLY` entries (affordance only).
- Tests: §22 items 1–8, 16–20 at the validator/registry level, plus all
  #49/#79/#80/#84–#87 suites green unchanged.

**Slice C: model plan contract + retirement of the action loop.**
- `propose_agent_plan` (`agent-plan-v1`, per-call enum subset, repair).
- Bounded multi-step plans, with no post-tool re-decision.
- The full §10.2 grounding contract: `SourceSelection` quotes, coverage,
  prohibited-content rule, the closed count/ordinal parsers and grounded
  evidence topic. After this slice no model-authored text or number drives
  execution.
- ANALYZE_VACANCY model proposals become the clarification.
- Retire `decide_agent_action`, `AgentDecision`, `TOOL_ACTIONS` and the
  `while True` loop.
- A new `AGENT_PROMPT_VERSION` and a real-Ollama smoke test (not a
  Target-Mac benchmark).
- Tests: the model-path adversarial matrix end-to-end with fake providers
  (§22 items 24–31 included), plus a prompt-injection fixture (a plan cannot be steered to a
  non-offered capability or a UUID field).

Dependency order is A, then B, then C. A delivers the M-8 user value first
on today's orchestration. B changes plumbing without changing behaviour. C
changes the model contract last, when everything below it is proven. Each
slice gets its own PR with `Refs #88`. Only the final slice closes #88.

---

## 24. Compatibility and transition

| Component | Fate |
|---|---|
| `route_agent_entry` + `analyze_hr_text` + source spans | **retained**, frozen, as the deterministic pre-router (A, B, C) |
| `_apply_pending_draft_followup` / `_FOLLOWUP_RE` | **retained**, frozen (deterministic live-draft editing) |
| `_dispatch_search`, `_dispatch_refine`, `_dispatch_profile`, `_dispatch_evidence`, `_dispatch_draft_job_criteria` | **wrapped** as capability executors (B); domain logic unchanged |
| `plan_and_search_candidates`, ResultSet repo, `get_current_authorized_profile`, #84 canonicalization, grounded synthesis, `rank_candidates_for_job`, confirm route | **retained unchanged** (domain services) |
| `AgentDecision`, `AgentActionType`, `TOOL_ACTIONS`, `decide_agent_action`, `while True` loop, `searched_queries` | **transitional** (adapter in B), **removed** in C |
| Model-authored `search_query` / `filter_query` / `limit` / `candidate_ref` / `evidence_topic` | `search_query` ignored (WHOLE_MESSAGE) from B; all replaced by grounded quotes + server parsers in C (§10.2) |
| `AgentTurnCommit`, `apply_agent_turn_commit`, `TurnBoundary`, `revalidate_reserved_turn` | **extended** (A) |
| `AgentToolResult` / `AgentTurnResult` / outcomes / presentation | **retained**; new closed outcomes only for clarification staleness and `PLAN_INCOMPLETE` |

Agent Core v2 replaces orchestration plumbing. It does not reimplement
search, evidence, evaluation or JD canonicalization.

---

## 25. Performance (Target Mac) and deployment

- The target is a Mac mini M4 Pro with 24 GB of memory: one local Ollama
  model, shared and bounded by the #85 admission gate.
- The design adds **no** model server, no second concurrent model, no
  conversation-history vectors and no distributed infrastructure.
- Model calls per turn go **down** for search-then-close turns, because the
  post-tool "what next" call is removed.
- The only new call is the clarification classifier. It runs only when a
  clarification is open and the button/label match did not resolve it, and
  its prompt is tiny.
- Model size/selection remains #36. No benchmark is part of this PR.
- Bank deployment remains **agentless**. It needs no Claude Code, Codex,
  Node runtime, developer shell editing or external SaaS AI. New state
  arrives only through Alembic migration and the existing
  configure/migrate/start/healthcheck/backup/rollback path. The optional
  `meyar retire-agent-tasks` command follows the existing ops-CLI pattern.

---

## 26. Open design questions (none on an authority/security boundary)

Resolved in this revision and no longer open: dual waiting lanes (§4.4),
static vs dynamic validation (§11), and source grounding of search/refine
inputs (§10.2).

1. **Answer plus a new requirement in one message** (`namizəd axtarışı, SQL
   də olsun`). v1 policy: a material requirement means a new task, so the
   clarification is superseded. Merging the two is deferred.
2. **Russian input.** It is not claimed. Enabling it depends on #36
   evidence and RU planner/label tests.
3. **Ranking in chat for jobs not confirmed in this session.** This would
   need a server-owned job picker affordance, and the model would still
   never name a job id. Deferred.
4. **Ordinal vocabulary coverage.** Phrases outside the closed parser
   (e.g. `sonuncu` / "last") fail closed with the existing "which
   candidate?" copy. Extending the closed list is a quality decision, not an
   authority one.
5. **Optional plan steps.** v1 plans are atomic (§11.3). Per-step
   criticality (keep an earlier search when an optional lookup fails) would
   need its own decision.
6. **Numeric defaults.** TTL 1800 s, ≤10 terminal tasks per context, and
   2 clarification attempts are reversible settings/constants, confirmed at
   slice A review.

---

## 27. Design review questions — answers

1. **What exactly is the durable task authority?**
   - There is none over data. `agent_tasks` durably records goal type,
     lifecycle status and phase for one BrowserSession context.
   - It is authoritative only for *continuity* (what the next answer
     resumes). It is never authoritative for candidates, ResultSets, drafts,
     jobs or scopes.
2. **What is BrowserSession-bound?**
   - Every task, every clarification, and all three live pointers
     (`active_result_set_id`, `active_pending_draft_id`,
     `active_clarification_id`), through the session context.
   - Waiting state lives in two independent lanes (§4.4): the dialogue lane
     (`active_clarification_id`, ≤1 WAITING_CLARIFICATION) and the
     mutation-confirmation lane (`active_pending_draft_id`, ≤1
     WAITING_CONFIRMATION). They coexist. Only a replacing pending draft
     ends lane B (T11).
   - The transcript is conversation-bound and durable.
3. **How is a clarification source-bound?**
   - `source_turn_id` + `source_sha256` + exact offsets, plus semantic and
     routing policy versions.
   - (A1/A2) the append-position binding with the exchange chain: the
     current `question_turn_id` is the last transcript entry, and the tail
     back to the source is exactly the server-linked retry chain
     (`[U1, Q1]` or `[U1, Q1, U2, Q2]`), validated from persisted
     predecessor/submission relationships.
   - `created_turn_version` is provenance only.
   - All are re-verified in the resuming turn and again under lock in
     Phase B. Any mismatch fails closed.
4. **How does natural language resolve a closed clarification?**
   - A deterministic order: button (exact), then the strict whole-message
     label table, then clear-new-task detection, then the local classifier returning
     exactly one allowed value or NEW_REQUEST/UNCLEAR.
   - The server validates the value. UNCLEAR never executes.
5. **What makes a plan executable?**
   - Schema-valid `agent-plan-v1`, all inputs grounded in the current
     message (§10.2), and every Layer 1 static check passing on freshly-read
     context (§11.1), with only IN_TURN non-business-mutating steps. A
     Layer 1 failure means zero execution.
   - Then, before each step, the Layer 2 dynamic preconditions (§11.2) and
     the executor's own authority checks.
   - A failed later step makes the plan PLAN_INCOMPLETE: nothing it produced
     is activated (§11.3).
6. **What can the model propose but never authorize?**
   - Goals, plan steps, **which** part of the user's own message a step
     applies to (exact quotes), closed clarification answers, and
     CREATE_JOB/RANK proposals. It never writes search/filter/topic text or
     numbers: the server resolves quotes and parses counts/ordinals itself.
   - It never authorizes candidate identity, ResultSet choice, drafts,
     tenant or session, scopes, scores, weights, dates or confirmation.
7. **How are mutations confirmed?**
   - `HUMAN_ACTION_ONLY` capabilities become existing CSRF forms tied to
     live server targets.
   - Only a separate authenticated human POST to the existing route executes
     the existing deterministic service.
8. **How does #85 Phase B atomically commit task/dialogue state?**
   - Changes are staged in `AgentTurnCommit`. Phase B revalidates the
     pointer snapshot, locks clarification/task rows in a fixed order, and
     re-checks preconditions.
   - Transcript, pointers, rows, audit, submission COMPLETED and the
     reservation clear are written in one commit. Otherwise nothing is.
9. **How does #87 replay protection interact with tasks/plans?**
   - A COMPLETED replay never re-executes. PROCESSING duplicates get 409.
   - UNIQUE `created_by_submission_id` / `resolved_by_submission_id` are
     the schema backstop, and the button choice is part of the request hash.
10. **How does a new session avoid inheriting live authority?**
    - A new session context has NULL pointers and a new epoch. All
      tasks/clarifications are keyed to the old context.
    - History is read-only and buttons are disabled.
11. **How does a new capability get registered without router growth?**
    - Add one enum member plus one `CapabilityDefinition` with a server
      executor.
    - The per-call schema subset, validator and orchestrator are generic.
12. **What prevents candidate identity from entering ranking or plan
    authority?**
    - Typed allow-list projections have no identity field.
    - Plan/args schemas are `extra="forbid"`; candidate references are
      grounded ordinal quotes parsed by the server (§10.2).
    - Registry `identity` policy, and deterministic ranking with no model
      arguments.
    - Identity-contamination regression tests.
13. **What state is projected into model context?**
    - The last N transcript turns, the offered capability names, result
      context presence/refs, `waiting_clarification` {task_type, phase} or
      null (lane A), and `pending_vacancy_confirmation` {present} (lane B).
      No task, clarification or draft ids are sent.
    - For the classifier: only the answer text, type and allowed codes.
14. **What expires and what remains durable?**
    - Clarifications expire (TTL / next appended turn (A1) / attempts / versions).
      Waiting tasks end with their pointer or session.
    - Terminal tasks are pruned to 10 per context. Conversation history,
      audit, Jobs, criteria versions, confirmations and Evaluations remain.
15. **How will #50 plug in later without bypassing ResultSet or evidence
    authority?**
    - As registered capabilities with `ACTIVE_RESULT_SET` live context,
      grounded ordinal quotes (§10.2), executors that resolve through
      `resolve_active_candidate_ref` and accepted evidence, their own output
      schema and factuality review.
    - The orchestrator does not change.
