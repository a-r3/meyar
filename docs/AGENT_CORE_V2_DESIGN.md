# Agent Core v2 — task, dialogue and capability architecture (issue #88)

**Status: PROPOSED / DESIGN REVIEW ONLY.** Nothing in this document is
implemented, accepted or merged. Decision record: D-092 (`docs/DECISIONS.md`).
Audited baseline: `main` @ `fb03477a4b4c3ec4698b7294f2589fbbbeafa4e7`
(#84, #85, #86, #87 closed). Alembic head at audit time: `a87d4c6e2b19`.
#88 stays OPEN after this design PR; implementation starts only after the
owner merges an accepted version of this design. #50 is non-scope.

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
layer takes typed arguments and ordinals only, so it is language-neutral.

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
| `source_turn_id` UUID (id of the user transcript entry) | yes | — | — | — | — | no | missing → STALE | transcript entry id | new |
| `source_sha256` (SHA-256 of the canonical LF source span) | yes | — | — | — | — | no | mismatch → STALE | computed | new |
| `source_start`, `source_end` (exact offsets into that user turn; CHECK `0 ≤ start < end ≤ 4000`) | yes | — | — | — | — | no | — | router (whole message today) | new |
| `semantic_policy_version`, `routing_policy_version` | yes | — | — | — | — | no | mismatch → STALE | code | new |
| `created_turn_version` (conversation `turn_version` after the creating Phase B) | yes | — | — | — | — | no | the answer must arrive while `turn_version` still equals this | DB | new |
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

Partial unique indexes:
- one `OPEN` clarification per `session_context_id`;
- one `WAITING_CLARIFICATION` task per `session_context_id`;
- one `WAITING_CONFIRMATION` task per `session_context_id` (mirrors the
  single pending-draft pointer).

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
→ [4] pure plan validator (§10) → reject (closed code, zero execution) | executable plan
→ [5] execute steps via registry executors, each with #85 boundary
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
  binding (offsets NULL, CHECK by type). The slot is filled by the *next*
  message.
- CLARIFY_INPUT_STRUCTURE and the model's closed CLARIFY codes
  (NEED_MORE_DETAIL, CANDIDATE_REFERENCE_REQUIRED, RESULT_CONTEXT_REQUIRED,
  UNSUPPORTED_REQUEST, HIRING_DECISION_REQUIRES_HUMAN) stay **non-resumable**
  fixed copy in #88. Only server-typed clarifications are resumable.

The turn stages:
- a new task (`UNDETERMINED` or `VACANCY_ANALYSIS`, status
  WAITING_CLARIFICATION);
- a clarification (OPEN, attempt 1);
- `active_clarification_id` pointing at it;
- the user turn (`turn_id` = new UUID) and the assistant turn, which
  carries a display-only `clarification` payload (question code plus
  choice codes, used to render buttons).

All of it is written in Phase B (§12). `created_turn_version` is the
conversation's final `turn_version` after that commit.

### 6.2 Source binding and verification on resume

Resume is allowed only if **all** of the following hold. Otherwise the
clarification becomes EXPIRED/stale and the turn gives safe re-clarify copy:

1. `session_context.active_clarification_id == clarification.id`. The row is
   locked FOR UPDATE, `status == OPEN`, `expires_at > now`, and
   `context_epoch` equals the context's epoch.
2. `conversation.turn_version == clarification.created_turn_version`. The
   answer must be the very next transcript write. A turn from another tab or
   session on the same conversation makes the clarification stale.
3. The transcript entry with `turn_id == source_turn_id` exists, has role
   `user`, and SHA-256 of `text[source_start:source_end]` equals
   `source_sha256`.
4. `semantic_policy_version` and `routing_policy_version` equal the running
   code's versions. Re-running `analyze_hr_text` on the span must still
   yield ≥1 material requirement and no PROHIBITED span.
5. Answer schema version is known.

Rule 2 makes the 100-turn transcript bound irrelevant: the source is at most
two entries back. The server never rebuilds the source from model memory or
from "the latest user message that looks like a requirement".

### 6.3 Answer schema (server-owned)

```text
SEARCH_OR_VACANCY        → CANDIDATE_SEARCH | VACANCY_ANALYSIS
VACANCY_SOURCE_REQUIRED  → the next message is the source (slot fill), no choice enum
```

`ClarificationAnswer` is a closed StrEnum. Allowed answers per type are a
code constant, versioned by `answer_schema_version`. The model never widens
it.

### 6.4 Resolving the next message (deterministic order)

With a live OPEN clarification in this session context:

1. **Button.** The form posts `clarification_id` + `clarification_choice`.
   Both must equal the live pointer and be an allowed value. If not, the
   turn fails closed with "this question is no longer active" copy. It
   **never** falls back to interpreting the text. The #87 request hash
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
     useful multi-line vacancy structure is the **slot fill**, not a new
     task. It resolves and runs ANALYZE_VACANCY over that exact message.
4. **Local model classifier.**
   `LLMProvider.resolve_clarification_answer(type, allowed_answers,
   answer_text)` returns a strict `ClarificationAnswerProposal(value ∈
   allowed ∪ {NEW_REQUEST, UNCLEAR})`.
   - The model gets only the answer text and the closed options. It does
     **not** get the source text or the transcript.
   - An allowed value resolves the clarification.
   - NEW_REQUEST is handled as in step 3 (supersede, then a normal turn).
   - UNCLEAR or a validation failure after one repair → step 5.
   - VACANCY_SOURCE_REQUIRED has no choice enum, so step 4 is skipped: any
     message that is not a step 3 slot fill is a new request (supersede).
5. **Unclear.**
   - The old clarification becomes SUPERSEDED by a new clarification with
     the **same source binding** and `attempt + 1` (same task, still
     WAITING_CLARIFICATION). The assistant repeats the question with
     buttons.
   - If the next attempt would be 3, the clarification becomes EXPIRED and
     the task FAILED_SAFE, with copy asking the user to rephrase the request.
     **No capability executes** on an unclear answer.

This is one documented policy: **supersede on any turn that is not an
answer**. There is no "retained in the background" clarification, and no
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
  and never beyond the BrowserSession expiry. Expiry is also forced by
  rule 2 of §6.2 (next write only).
- **Cardinality.** At most one OPEN clarification per session context, and
  one WAITING_CLARIFICATION task per session context (partial unique
  indexes). There is no second, parallel active task per conversation
  context: a new task supersedes.
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

| # | Event | Old | New | Authority required | DB mutation point | Audit |
|---|---|---|---|---|---|---|
| T1 | Deterministic clarification created | (none) / ACTIVE | WAITING_CLARIFICATION | live principal + reservation + `candidates:read` | Phase B insert task + clarification + pointer | `agent.task.created`, `agent.clarification.created` |
| T2 | Valid answer (button/label/model), resume succeeds, search | WAITING_CLARIFICATION | COMPLETED | + live OPEN clarification (§6.2) | Phase B | `agent.clarification.resolved`, `agent.task.state_changed` |
| T3 | Valid answer, vacancy draft produced | WAITING_CLARIFICATION | WAITING_CONFIRMATION | + draft pointer set in the same Phase B | Phase B | same |
| T4 | Valid answer, capability returns a truthful failure (e.g. JOB_DRAFT_FAILED, non-executable plan) | WAITING_CLARIFICATION | FAILED_SAFE | same | Phase B | `agent.clarification.resolved`, `agent.task.state_changed` |
| T5 | Unclear answer, attempt < 2 | WAITING_CLARIFICATION | WAITING_CLARIFICATION (new clarification row) | same | Phase B | `agent.clarification.superseded(reason=UNCLEAR)`, `agent.clarification.created` |
| T6 | Unclear answer at attempt 2 | WAITING_CLARIFICATION | FAILED_SAFE | same | Phase B | `agent.clarification.expired(reason=ATTEMPTS)` |
| T7 | New task while a clarification is open | WAITING_CLARIFICATION | CANCELLED | same | Phase B of the new turn | `agent.clarification.superseded(reason=NEW_TASK)` |
| T8 | TTL passed, stale source, or version mismatch, observed by a turn | WAITING_CLARIFICATION | EXPIRED | same | Phase B of that turn | `agent.clarification.expired(reason=TTL\|STALE\|VERSION)` |
| T9 | Draft modified (new draft id) | WAITING_CONFIRMATION | WAITING_CONFIRMATION (`pending_draft_id` updated) | live pending-draft authority | Phase B | `agent.task.state_changed(same)` |
| T10 | HR confirms draft (`/ui/agent/drafts/{id}/confirm`, CSRF) | WAITING_CONFIRMATION | COMPLETED | existing confirm route scopes + `resolve_pending_draft_authority` | the confirm route's single commit, with `create_job`/`AgentDraftConfirmation` | existing `job.created` + `agent.task.state_changed` |
| T11 | Pending-draft pointer replaced by a new vacancy analysis | WAITING_CONFIRMATION | CANCELLED | live pointer | Phase B | `agent.task.state_changed` |
| T12 | Lazy expiry: task past `expires_at` or context gone | any waiting | EXPIRED | none (maintenance) | retention batch (§13) | `agent.task.state_changed(reason=EXPIRED)` |

Terminal states (COMPLETED, CANCELLED, EXPIRED, FAILED_SAFE) have **no**
outgoing transitions. Turns that end in a waiting state are the only ones
that create a task row. Busy, cancelled, stale and revoked turns commit no
transition at all (§12).

A new search while a vacancy draft waits for confirmation does **not**
cancel it. This preserves today's behaviour where the pending pointer
survives search turns. That is why there are two independent waiting
slots, not one.

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
| SEARCH_CANDIDATES | `_dispatch_search` → `plan_and_search_candidates` → `create_result_set_from_search` | `SearchArgs{query: str 1..2000}` (= `AgentDecision.search_query`) | `AgentSearchToolResult` | `candidates:read` | none; **produces** ACTIVE_RESULT_SET | D-031 planner (LLM), embeddings for hybrid | ranked professional summaries in the UI; no identity to the model | SESSION_WORKING_STATE (new AgentResultSet, inert until pointer) + audit | none | frozen planner validation, prohibited-attribute and no-silent-weakening rules, snapshot | planner audit, `CANDIDATE_SEARCH_EXECUTED`, `agent.result_set.created`, `agent.tool.executed` | wrap as executor unchanged |
| REFINE_RESULTS | `_dispatch_refine` → `validate_active_result_set_for_refinement` → `create_result_set_from_refinement` | `RefineArgs{filter_query?: str 1..2000, limit?: 1..50}`, ≥1 required | `AgentRefineToolResult` | `candidates:read` | ACTIVE_RESULT_SET | planner for `filter_query` only | subset of snapshot members | SESSION_WORKING_STATE + audit | none | #86 snapshot subset, server order | `agent.result_set.refined` / `refine_rejected` | wrap unchanged (`AgentActionType.REFINE_CANDIDATE_RESULTS` renamed only at the registry layer) |
| GET_CANDIDATE_PROFILE | `_dispatch_profile` → `resolve_active_candidate_ref` → `get_current_authorized_profile` | `ProfileArgs{candidate_ref: 1..50}` | `AgentProfileToolResult` | `candidates:read` | ACTIVE_RESULT_SET | optional D-038 grounded synthesis (`select_grounded_facts`) over accepted professional facts | accepted `CandidateProfileExtraction` (professional only) | NONE (+ audit) | none | ordinal → snapshot → current profile authority | `agent.result_set.reference_resolved` / `reference_rejected` | wrap unchanged |
| GET_CANDIDATE_EVIDENCE | `_dispatch_evidence` (same resolution) | `EvidenceArgs{candidate_ref: 1..50, evidence_topic?: ≤200}` | `AgentEvidenceToolResult` | `candidates:read` | ACTIVE_RESULT_SET | same optional grounded synthesis | accepted evidence items (CV spans, professional) | NONE (+ audit) | none | same | same | wrap unchanged |
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

class SearchArgs(BaseModel):  extra="forbid"; query: str (1..2000)
class RefineArgs(BaseModel):  extra="forbid"; filter_query: str|None; limit: int|None (1..50)
class ProfileArgs(BaseModel): extra="forbid"; candidate_ref: int (1..50)
class EvidenceArgs(BaseModel):extra="forbid"; candidate_ref: int (1..50); evidence_topic: str|None (≤200)
class NoArgs(BaseModel):      extra="forbid"            # CREATE_JOB / RANK_JOB_CANDIDATES proposals

class PlanStep(BaseModel):    # discriminated union on `capability`
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

The contract has **no field for** a tenant id, BrowserSession id,
ResultSet id, candidate UUID, draft id, job id, scope or permission,
confirmation flag, evaluation date, score or weight, or free-text answer.
Candidate references are ordinals only, resolved at execution time through
the active ResultSet (§14). Unknown keys fail `extra="forbid"`. Every string
has a hard bound; the ≤2000-character Ollama grammar limit from D-035 still
applies.

`SearchArgs.query` and `RefineArgs.filter_query` keep today's semantics.
They are forwarded unmodified into the frozen D-031 planner, which revalidates
prohibited attributes and never silently weakens a requirement. Binding these
to exact source spans is an open question (§19). It is not an authority
boundary, because search is non-mutating and the planner re-derives filters.

### 10.2 Bounds

| Bound | Value | Notes |
|---|---|---|
| Max plan steps | `MAX_PLAN_STEPS = 3`, effective `min(3, agent_max_tool_calls)` | `agent_max_tool_calls` keeps its truthful meaning: max capability executions per turn |
| Max capability executions per turn | same | a server-built resume plan is exactly 1 step |
| Planner model calls per turn | 1 + 1 repair | replaces `decide_agent_action`; there is **no** post-tool "what next" call |
| Clarification-answer classifier calls | ≤1 + 1 repair, only when an OPEN clarification is unmatched by button/label | |
| Capability-internal model calls | unchanged service bounds (D-031 planner, JD draft `MAX_JD_DRAFT_ATTEMPTS=2`, synthesis `MAX_SYNTHESIS_ATTEMPTS=2`) | all through `BoundaryLLM` / `BoundaryEmbedding` |
| Clarification depth | 2 attempts per source binding (§6.4), then EXPIRED | no nested clarifications |
| Open clarifications / waiting tasks | 1 OPEN clarification and ≤1 per waiting status, per session context | partial unique indexes |
| Task lifetime | WAITING_CLARIFICATION: clarification TTL (≤3600 s); WAITING_CONFIRMATION: until the pointer changes or the BrowserSession expires (≤`ui_session_ttl_hours`) | |
| Model context turns | `agent_max_context_turns` (default 8), unchanged | |
| Persisted task history | ≤10 terminal tasks per session context (§13) | |

There is no recursion, no model self-invocation, no dynamic tools, and no
open-ended loop. The `while True` loop is replaced by a `for step in
validated_plan.steps` bounded iteration.

---

## 11. Plan validator (pure, server-owned)

`validate_plan(proposal, ctx: ValidationContext) -> ExecutablePlan |
PlanRejection` is a **pure function**: no DB access, no mutation.

`ValidationContext` is assembled by read-only server calls in a DB phase:
- principal scopes;
- task type if waiting;
- active ResultSet status from `active_result_set_size` / a read-only
  validation: none, valid n, STALE or EXPIRED;
- pending-draft presence;
- a confirmed job in this session;
- open clarification state;
- running policy/schema versions.

Rejections are checked in order. The first failure rejects the **whole**
plan and nothing executes:

| Code | Rule |
|---|---|
| `UNSUPPORTED_VERSION` | `schema_version` or a capability `policy_version` unknown |
| `SHAPE_INVALID` | kind/field combination invalid (e.g. PLAN without steps, CONVERSE with steps) |
| `UNKNOWN_CAPABILITY` | not in the registry (normally already a Pydantic failure) |
| `NOT_MODEL_PROPOSABLE` | e.g. ANALYZE_VACANCY from the model (converted to the SEARCH_OR_VACANCY clarification per §6.1, not executed) |
| `INVALID_ARGUMENTS` | args fail the capability `input_schema` |
| `PLAN_TOO_LONG` | steps > effective max |
| `DUPLICATE_STEP` | identical (capability, args) twice (generalizes today's `searched_queries` guard) |
| `SCOPE_MISSING` | `required_scopes ⊄ principal scopes` |
| `TASK_TYPE_CONFLICT` | step capability not allowed for the plan goal / waiting task type |
| `RESULT_CONTEXT_REQUIRED` | needs ACTIVE_RESULT_SET, and neither the context nor an earlier step (`produces`) provides it |
| `RESULT_SET_STALE` / `RESULT_SET_EXPIRED` | active ResultSet not valid at validation time (re-checked authoritatively at execution) |
| `CANDIDATE_REF_OUT_OF_RANGE` | ordinal > validated size (or > the producing step's result, checked at execution) |
| `CONFIRMATION_REQUIRED` | a `HUMAN_ACTION_ONLY` capability without its live target → rejected; with target → converted to an affordance, **never executed** |
| `PROHIBITED_ATTRIBUTE` | `find_prohibited_term` / PROTECTED_CUE hit in any string arg |
| `CANDIDATE_CONTENT_POLICY` | a capability whose content policy is not satisfied (e.g. identity field requested; reserved for #50 capabilities) |

The HR user sees fixed product copy per code family (e.g. "Bu sorğunu
təhlükəsiz icra edə bilmədim; zəhmət olmasa dəqiqləşdirin."). They never see
raw codes, capability names or ids. The codes go to audit
(`agent.plan.rejected`).

---

## 12. Execution model and #85 / Phase B atomicity

### 12.1 Execution

```text
Phase A  lock+reserve, claim submission                      → commit (leave_db)
[if needed] model: clarification classifier                   (no DB)
reenter  revalidate (principal FOR SHARE, conv/context locks)
[if MODEL route] leave_db → model: plan proposal → reenter
validate (pure) on freshly read ValidationContext
for step in plan:            # ≤ 3
    executor(step)           # may leave_db → local inference → reenter internally
Phase B  final reenter + clarification/task row locks + submission check → apply → commit
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
  against it, exactly as the eager sync works today.

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
   unexpired, same `created_turn_version`);
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
- `available_capabilities`: the per-call enum subset, as names plus
  one-line server descriptions;
- `active_result_context_present: bool`,
  `available_candidate_refs: [1..n]` (unchanged semantics);
- `pending_vacancy_draft_present: bool`;
- `waiting_task`: `{task_type, phase}` codes only, or null.

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
   `live_context={ACTIVE_RESULT_SET}`, ordinal-only args,
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
    resolution_source ⇔ RESOLVED, offsets required for SEARCH_OR_VACANCY and
    NULL for VACANCY_SOURCE_REQUIRED, `0 ≤ source_start < source_end ≤ 4000`;
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
| `agent.plan.rejected` | attempt | `reason_code`, `schema_version` |
| `agent.tool.executed` (existing) | as today | + `capability`, `capability_version` |
| `agent.entry.routed` / `agent.entry.action_rejected` (existing) | unchanged | unchanged |

Never logged or audited:
- raw HR text, JD body, CV text or evidence text;
- CandidateIdentity;
- raw model output or chain-of-thought;
- search query text;
- tokens.

`plan_sha256` is computed over the canonical JSON of the *validated* plan,
which contains no UUIDs or identity.

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
    sha256(“Python mütləqdir.”), `created_turn_version = v`);
  - pointer `active_clarification_id = κ`;
  - assistant question with buttons.
- T2: `namizəd axtarışı` goes through §6.4:
  - no button;
  - the step 2 whole-message label match gives CANDIDATE_SEARCH (steps 3
    and 4 are not reached).
- Checks pass:
  - pointer = κ, OPEN, unexpired, epoch equal;
  - `turn_version == v`;
  - U1 is present and the hash matches;
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
- κ `expires_at` has passed, or another tab wrote to the conversation, or
  the session changed. The late `namizəd axtarışı` fails §6.2.
- Phase B:
  - κ EXPIRED(TTL/STALE);
  - τ EXPIRED;
  - pointer cleared;
  - fixed "resend the requirement" copy.
- No capability executes. If κ belonged to another BrowserSession, this
  context never pointed at it: the text is simply a new turn, and the
  foreign row is untouched.

**E. Search → `bunlardan SQL bilənləri` → refine.**
- MODEL route, and the plan is `[REFINE_RESULTS(filter_query="SQL bilənlər")]`.
- The validator:
  - checks `candidates:read`;
  - requires ACTIVE_RESULT_SET, which is present;
  - requires the pre-check to pass (VALID n).
- The executor runs today's `_dispatch_refine`: a subset of the #86
  snapshot and a new REFINEMENT ResultSet. Phase B switches the pointer.
- The plan cannot name a ResultSet id or candidate id.

**F. `birincinin sübutunu göstər` → evidence.**
- The plan is `[GET_CANDIDATE_EVIDENCE(candidate_ref=1)]`.
- The validator checks that ref 1 ≤ n.
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
  - the first resolves κ and bumps `turn_version`;
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

---

## 22. Adversarial test plan (future; not written in this PR)

For slices A to C, each item is a failing-first regression:

1. **Unknown capability** in the model output: rejected, zero executor
   calls (spy), zero state.
2. **Invalid args**: extra keys, wrong types, over-length strings, ref 0 or
   51.
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
- The pure `validate_plan`.
- A transitional adapter turns each current `AgentDecision` / forced route
  into a one-step `PlanStep`, so the `if/elif` dispatch becomes registry
  dispatch.
- CREATE_JOB/RANK become `HUMAN_ACTION_ONLY` entries (affordance only).
- Tests: §22 items 1–8, 16–20 at the validator/registry level, plus all
  #49/#79/#80/#84–#87 suites green unchanged.

**Slice C: model plan contract + retirement of the action loop.**
- `propose_agent_plan` (`agent-plan-v1`, per-call enum subset, repair).
- Bounded multi-step plans, with no post-tool re-decision.
- ANALYZE_VACANCY model proposals become the clarification.
- Retire `decide_agent_action`, `AgentDecision`, `TOOL_ACTIONS` and the
  `while True` loop.
- A new `AGENT_PROMPT_VERSION` and a real-Ollama smoke test (not a
  Target-Mac benchmark).
- Tests: the model-path adversarial matrix end-to-end with fake providers,
  plus a prompt-injection fixture (a plan cannot be steered to a
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
| `AgentTurnCommit`, `apply_agent_turn_commit`, `TurnBoundary`, `revalidate_reserved_turn` | **extended** (A) |
| `AgentToolResult` / `AgentTurnResult` / outcomes / presentation | **retained**; new outcomes only for clarification staleness |

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

1. **Answer plus a new requirement in one message** (`namizəd axtarışı, SQL
   də olsun`). v1 policy: a material requirement means a new task, so the
   clarification is superseded. Merging the two is deferred.
2. **Russian input.** It is not claimed. Enabling it depends on #36
   evidence and RU planner/label tests.
3. **Ranking in chat for jobs not confirmed in this session.** This would
   need a server-owned job picker affordance, and the model would still
   never name a job id. Deferred.
4. **Binding `SearchArgs.query` to exact source spans** instead of the
   model's own text. Not an authority boundary (§10.1). To be decided with
   slice C prompt evaluation.
5. **Numeric defaults.** TTL 1800 s, ≤10 terminal tasks per context, and
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
   - The transcript is conversation-bound and durable.
3. **How is a clarification source-bound?**
   - `source_turn_id` + `source_sha256` + exact offsets, plus semantic and
     routing policy versions and `created_turn_version`.
   - All are re-verified in the resuming turn and again under lock in
     Phase B. Any mismatch fails closed.
4. **How does natural language resolve a closed clarification?**
   - A deterministic order: button (exact), then the strict whole-message
     label table, then clear-new-task detection, then the local classifier returning
     exactly one allowed value or NEW_REQUEST/UNCLEAR.
   - The server validates the value. UNCLEAR never executes.
5. **What makes a plan executable?**
   - Schema-valid `agent-plan-v1`, then every §11 check passes on
     freshly-read context, with only IN_TURN non-business-mutating steps.
   - Then each executor re-checks its own authority at execution time.
6. **What can the model propose but never authorize?**
   - Goals, plan steps, ordinals, search/filter text, closed clarification
     answers, and CREATE_JOB/RANK proposals.
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
    - Plan/args schemas are `extra="forbid"` with ordinals only.
    - Registry `identity` policy, and deterministic ranking with no model
      arguments.
    - Identity-contamination regression tests.
13. **What state is projected into model context?**
    - The last N transcript turns, the offered capability names, result
      context presence/refs, pending-draft presence, and waiting task
      type/phase codes.
    - For the classifier: only the answer text, type and allowed codes.
14. **What expires and what remains durable?**
    - Clarifications expire (TTL / next-write / attempts / versions).
      Waiting tasks end with their pointer or session.
    - Terminal tasks are pruned to 10 per context. Conversation history,
      audit, Jobs, criteria versions, confirmations and Evaluations remain.
15. **How will #50 plug in later without bypassing ResultSet or evidence
    authority?**
    - As registered capabilities with `ACTIVE_RESULT_SET` live context,
      ordinal args, executors that resolve through
      `resolve_active_candidate_ref` and accepted evidence, their own output
      schema and factuality review.
    - The orchestrator does not change.
