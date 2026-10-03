# Issue #46 M-9 — preserve accepted facts after failed re-extraction

Status: M-9 independently **ACCEPTED + MERGED** through PR #114.
Refs #46, M9 — Deployment, Benchmark & Integration Readiness (milestone 10).
#46 itself is NOT complete and MUST remain OPEN. #35/#36/#50 remain OPEN and
unchanged. No Target-Mac work, benchmark or production model approval is claimed.
M-5 remains independently accepted and merged. No new product behavior or next
#46 hardening item is started by this documentation-only follow-up.

## Verified independent acceptance and owner merge

Owner-confirmed independent acceptance **PASS** at exact PR #114 head
`ffc59eab3447b4a6ecb07017725751a8024fa062`; its
[exact-head CI run 37097082007](https://github.com/a-r3/meyar/actions/runs/37097082007)
completed **SUCCESS**. The owner manually **Squash and merged** PR #114 to
`32f9cc9ed1a39c0a43ffe112dce2f641d6608cc4`, whose sole parent is
`22cf6a5b0b46bc61841cae2d3a646a353b9889a9`. Live GitHub and local Git
verification confirm the accepted head and squash have the identical full tree:
`6459479c913c45f42410cc646fa4ad4e68f22b02`. Accepted and merged content
are therefore identical.

PR #114 is MERGED; M-9 independent acceptance is PASS. #46 was verified OPEN
before this follow-up proceeded; #35/#36/#50 and milestone M9 remain OPEN.
The accepted latest-attempt/document-boundary, same-document preservation,
fail-closed selected evidence, ResultSet, readiness and HR-only identity
semantics below remain unchanged. Cross-document fallback remains prohibited.

The earlier rejected head `d5984e779f7ec707e04f9bf3798c6d65da341a5a` and its
successful CI run `37094854961` are historical correction evidence only;
the accepted head/run above supersede their pending/rejected delivery status.
The post-merge docs PR is separate and is not merged by the agent.

## Original implementation base and prerequisites (historical)

Original implementation main and local task base:
`22cf6a5b0b46bc61841cae2d3a646a353b9889a9`.
Implementation branch: `fix/46-m9-effective-profile`; hooksPath verified `.githooks`.
Live #46/#35/#36/#50: OPEN. M9 milestone 10: open.
PR #112 (M-5) MERGED at `d14ab1a5b4e25bd7d012bada4041ea59aaf55b61`;
its independent acceptance is durably recorded in D-099 and M-5 validation.
PR #113 (M-5 post-merge record) MERGED at the exact base above.
Unrelated dependency PRs #3/#4/#5/#67 were not changed.

## Original same-document root cause and reproduction (historical)

The single, tenant-wide and bounded-member repository helpers selected max
version_number without filtering status. Read-time evidence authorization then
rejected the selected failed/manual-review attempt. Immutable v1 remained in
storage, but selection prevented every professional consumer from reaching it.
UI's custom library SQL and identity selection had the same chronology problem.
Neither historical-row deletion nor an embedding provenance mismatch caused
this defect.

Before production edits, five synthetic tests seeded an evidence-valid Python
v1 COMPLETED, appended v2 FAILED with no facts, and verified the old selector
returned v2. The expected preserved authority failed across shared boundaries.
Executed from backend (UV_CACHE_DIR set to a writable temporary cache):

```bash
uv run --offline pytest -q tests/test_m9_effective_profile.py
```

Actual output, omitting pytest's fixture representations:

```text
FAILED tests/test_m9_effective_profile.py::test_m9_authority_survives_failed_attempt
FAILED tests/test_m9_effective_profile.py::test_m9_structured_search_survives_failed_attempt
FAILED tests/test_m9_effective_profile.py::test_m9_ranking_survives_failed_attempt
FAILED tests/test_m9_effective_profile.py::test_m9_embedding_survives_failed_attempt
FAILED tests/test_m9_effective_profile.py::test_m9_detail_survives_failed_attempt
5 failed in 0.72s
```

Specific failures: authorized profile was None; search result_count was 0
(expected 1); ranking evaluated_count was 0 (expected 1); embedding raised
PROFILE_NOT_COMPLETED with status FAILED; detail skills were [] (expected
Python). After the initial implementation: `5 passed in 0.65s`.

## Acceptance blocker and corrected-head reproduction

Reviewed head used candidate-wide newest COMPLETED. It incorrectly revived
D1 facts after a D2 failed/manual attempt. The former changed-document test
asserted that wrong policy; it has been corrected, not treated as acceptance.
Before-fix reproduction of the original same-document bug above remains history.
The original green 355-focused/3492-full results below apply to the REJECTED
reviewed head and do not validate the document-boundary correction.

Final synthetic correction tests were also run against reviewed profile/identity/
library code loaded into a separate Python process. Working production files
were not reverted. Exact reviewed source came from `git show` of that head.
The initial correction run had 13 product assertion failures and four invalid
semantic request fixtures (17 failed, 16 passed in 6.82s); semantic fixtures
were corrected before the final reviewed-code reproduction recorded below.

Final reviewed-code reproduction:

```text
19 failed, 17 passed in 6.76s
```

All 19 failures are corrected-contract assertions: retry boundary, new-document
identity, mixed query selection, all three search modes, current scoring,
ResultSet/agent and UI. The same-document/evidence/success-switch cases pass.

## Authority contract (D-100)

**Fallback is permitted only inside the latest attempt's CandidateDocument boundary. Cross-document fallback is prohibited.**

- **Latest extraction attempt:** highest immutable version_number regardless of
  COMPLETED/FAILED/MANUAL_REVIEW_REQUIRED. Used for diagnostics, history,
  operational status, and per-document readiness/retry. Explicit aliases name
  latest-attempt operations; legacy repository `get_current_*`/`list_current_*`
  functions retain attempt semantics for compatibility. Production professional
  consumers use the new effective selectors.
- **Effective accepted professional profile:** resolve latest attempt regardless
  of status, take its CandidateDocument, select newest COMPLETED within that
  SAME tenant/candidate/document, then pass unchanged `authorize_profile_version()` (or its
  identical batch form). Status selection alone never grants fact authority.
  With no COMPLETED row, professional authority is absent. If the selected
  COMPLETED fails current evidence checks, authority is absent: **fail closed,
  no scan back through older COMPLETED rows**.
- D1 COMPLETED v1 → same-D1 FAILED or MANUAL_REVIEW_REQUIRED v2 may preserve
  v1 while its current canonical evidence remains valid.
- D1/v1 → new-D2 failed/manual v2 yields no effective profile if D2 has no
  completed version. D1 is history only. D2/v3 COMPLETED restores current
  authority subject to evidence authorization.
- A newer COMPLETED v3 replaces v1 deterministically, subject to that same
  current evidence boundary. Unsupported v3 blocks facts; it cannot silently
  restore a potentially superseded older profile.
- No copying into failed rows, status rewrite, historical-row update, mutable
  current flag, migration, database schema, dependency or lockfile change.
- Identity/PII never selects professional authority or affects suitability.

The fail-closed newest-COMPLETED policy prevents an unsupported newest accepted
extraction from silently reviving older facts, and avoids an unbounded historical
verification scan. This intentionally favors security and predictable cost over
recovering older facts when the newest COMPLETED is evidence-invalid.

## Consumer behavior

**Search / embeddings.** Structured search keeps v1 only after same-document
failed/manual-review attempts. New-document failure excludes D1 from all current
structured/semantic/hybrid results and embedding preparation. For same-document
fallback, semantic and hybrid reuse only the exact v1 embedding with unchanged
profile-id, serializer source-hash, provider/model/revision/dimension matching.
A v3 switch excludes v1's embedding even when content/config could coincide.
Until v3's compatible embedding exists, existing missing-embedding exclusion
and counts apply. After generation, v3 participates. Embedding preparation uses
only the effective selected and evidence-authorized profile; latest FAILED is
never embedded. Existing absent/noncompleted error distinction is preserved.

**Ranking / evaluation.** Batch ranking, direct score and CLI score resolve the
same effective selection. Evaluation's exact profile-id and current evidence
check remain unchanged, including before cached evaluation reuse. New evaluation
with a new explicit as-of date records preserved v1; an identical provenance
request reuses its existing evaluation. No historical evaluation rewrite,
automatic rescore, arithmetic or deterministic policy change. Batch candidates
without a COMPLETED profile are counted as NO_EFFECTIVE_PROFILE (replacing the
old no-current/noncompleted distinction). Direct score with no selected COMPLETED
returns the existing profile-not-found 404; selected evidence-invalid rows remain
rejected by evaluation's evidence boundary.

**Reconciliation.** Per-document latest-attempt queries and processing orchestration
retain attempt semantics. A new document B's v2 FAILED remains not READY and is
retried; candidate-level facts from document A/v1 are unavailable as current
when the latest profile attempt is B and B has no accepted completion. Readiness
check still requires the exact document's profile and identity and that profile's
embedding. A real extraction failure followed by successful retry creates v3,
switches professional authority, and leaves v2 FAILED immutable.
M-5 ingestion/path/failure/retention semantics are unchanged.

**HR.** Detail and library facts/evidence come from the effective source, with
same-document fallback disclosure: “Son yenilənmə tamamlanmadı. Əvvəlki
təsdiqlənmiş profil göstərilir.” Normal HTML shows no error code, profile UUID, version number or
model/provider internals. Evidence locations/quotes and PDF/DOCX page wording
are resolved against the preserved profile's own source, never a failed newer
source. Evidence-invalid COMPLETED rows expose no facts and receive safe
unavailable copy. A new-document failed/manual attempt displays attention state
without old-document professional facts. Old evaluations/documents remain history.

The REST detail DTO reuses CandidateDetailView, so its schema gains additive
`latest_attempt_status`, `preserved_profile` and `preserved_identity` fields.
Existing `profile_status` continues describing latest processing/availability,
while `profile_version` identifies the selected facts' source. Consumers can now
explicitly distinguish preserved facts from latest processing; no field removal
or database change. Identity version/status describe the selected HR identity.

The former “Hazırlıq vəziyyəti” library control is explicitly named **“Son
emalın vəziyyəti”**. Its unchanged query parameter `profile_status` filters the
latest persisted extraction attempt's status, exactly the card badge's status;
COMPLETED means processing completed, not an independent evidence guarantee.
Effective fact availability is shown independently by facts/warning/unavailable
copy. With no profile attempt, the accepted M-5 parser-failure presentation
fallback is unchanged. This is not a claim that a failed refresh is ready.

**Identity.** Select the latest identity attempt's CandidateDocument, then its
newest COMPLETED and unchanged current identity evidence verification, without
historical scan. Same-document failure may preserve supported name/email/phone
for HR only. New-document failed/manual identity cannot reuse D1 contacts.
New unsupported COMPLETED identity fails
closed. Detail/library disclose preserved contacts independently. Identity remains
absent from professional extraction, embeddings, search, ranking and evaluation.
Photos deliberately retain their separate latest-document plus exact-document
identity authorization rule; M-9 does not revive an older photo.

**Agent / ResultSet.** Current professional-profile/evidence operations share
`get_current_authorized_profile`, now resolving the effective profile. Member
checks use the bounded effective selector and existing batch evidence boundary.
A snapshot recording D1/v1 remains compatible after same-D1 failed/manual v2
only while exact effective authority still matches. New-D2 failure with no
accepted completion makes the old member STALE; a new COMPLETED v3 also does
(even if v3 is subsequently evidence-invalid).
All original profile/embedding member ids, scores, ordinal and persisted policy
provenance remain immutable. Refinement still uses the original bounded member
set; it never adds unrelated new candidates. Missing/mismatched embeddings,
canonical authority loss and foreign tenants still fail closed. D-100 amends
D-090's live compatibility rule; stored member-snapshot-v1 format is unchanged.

## Performance / concurrency

Single, bounded and tenant-wide profile selectors share one SQL statement:
tenant-scoped DISTINCT ON latest-attempt provenance (all statuses), followed by
a same tenant/candidate/document join and newest-COMPLETED DISTINCT ON. Bounded
candidate IDs restrict the inner query; candidate ownership remains checked.
Only selected content is returned. Identity single/page selectors use the same
logical rule and the library now reuses the shared bounded identity selector.
No Python historical loading, per-candidate selector, or historical evidence scan was introduced. Search now
uses shared canonical batches of at most 500 instead of a query per candidate,
bounding bind parameters and canonical-content memory.
Library uses page-bounded effective selection and batch professional authority;
identity authorization retains existing page-bounded per-identity verification,
with one extra bounded latest-identity projection for truthful disclosure.
Ranking retains its existing per-evaluation validation/persistence cost; this
slice adds no historical scan or new per-candidate selection to ranking.
Synthetic query-count tests require two queries for effective-member selection
plus evidence authorization, and one canonical query for five-candidate search despite
many failed attempts; a forced two-version batch limit proves three bounded
canonical queries return the same five candidates. Existing #86 scale regressions
remain mandatory.

Authority is derived from immutable committed rows, with no new mutable pointer
or write-side synchronization. Existing unique candidate/version allocation
constraints remain the concurrent-write backstop; this slice does not claim to
resolve extraction-write races or overlapping reconcilers. A read evaluates its
selected immutable snapshot; a concurrent newer commit is reflected on a later
selection. Exact evaluation/embedding provenance cannot be relabeled by that
concurrent update. No new transaction isolation, locking or retry behavior.

## Original reviewed-head gates (historical, acceptance REJECTED)

All data and providers are synthetic; local tests require the local development
PostgreSQL test database, never a real model or candidate fixture. UV_CACHE_DIR
was set to `/tmp/meyar-m9-uv-cache` because the sandbox cannot write the default
cache. Database/socket tests ran with local service access. A sandboxed initial
attempt was cancelled and rerun with that access; a first broad command named a
nonexistent evaluation test file and ran no tests. The final commands below are
the actual corrected executions. An initial full run was interrupted before
completion when review identified the need to bound search canonical batches;
no result from that incomplete run is claimed.

Focused command, from backend:

```bash
export UV_CACHE_DIR=/tmp/meyar-m9-uv-cache
uv run --offline pytest -q tests/test_m9_effective_profile.py tests/test_candidate_profile_extraction.py tests/test_candidate_factual_authority_backstop.py tests/test_candidate_identity.py tests/test_search_structured.py tests/test_search_semantic.py tests/test_search_hybrid.py tests/test_candidate_embedding.py tests/test_scoring_persistence.py tests/test_api_evaluations.py tests/test_batch_ranking.py tests/test_folder_reconciliation.py tests/test_ui_routes.py tests/test_agent_service.py tests/test_agent_result_set.py tests/test_agent_result_set_snapshot.py tests/test_agent_result_set_scale.py tests/test_tenant_isolation.py tests/test_no_exfiltration.py
```

Actual output:

```text
........................................................................ [ 20%]
........................................................................ [ 40%]
........................................................................ [ 60%]
........................................................................ [ 81%]
...................................................................      [100%]
355 passed in 51.58s
```

Full gate, executed from backend (the existing test-only hang diagnostics were
enabled, as in CI):

```bash
export UV_CACHE_DIR=/tmp/meyar-m9-uv-cache
export MEYAR_TEST_HANG_DIAGNOSTICS=1
uv run --offline ruff check .
uv run --offline mypy src
uv run --offline pytest -q
uv run --offline alembic heads
```

Actual results (diagnostic phase/pool lines omitted):

```text
All checks passed!
Success: no issues found in 226 source files
3492 passed in 503.92s (0:08:23)
b88a2c4d6e10 (head)
```

Root checks:

```bash
git diff --check
scripts/scan-tracked-tree.sh
```

`git diff --check`: exit 0, no output. Tracked-tree secret/real-data scan: clean.
The scan is repeated after explicitly staging the new regression/validation
files so they are included; pre-existing untracked personal material is not
staged or inspected. No DB schema change, so fresh-migration/alembic-check work
is not required by this slice. Full migration regressions remain in pytest.

These original gate results belong to the rejected reviewed head. The accepted
correction, exact-head CI and owner merge are recorded above and below.

## Corrected regression matrix and gates

All fixtures/providers remain clearly synthetic. Commands use the local test
PostgreSQL and writable UV cache, without real CVs or model calls.

| Acceptance cases | Corrected regression |
|---|---|
| 1–2 same-document FAILED/manual preserves | authority/search/rank/embed/detail; manual selector parity; ResultSet FAILED/manual tests |
| 3–4 new-document FAILED/manual has no effective profile | new_document_has_no_effective_profile_in_any_selector (single/bounded/tenant-wide) |
| 5,7 current structured/semantic/hybrid and embedding exclusion | new_document_excludes_old_search_and_embedding (three modes × two statuses) |
| 6 current rank/direct score; immutable readable history | new_document_blocks_current_scoring_preserves_history |
| 8 stale D1 ResultSet/agent profile/evidence/refinement; immutable members | new_document_stales_result_set_and_agent_facts |
| 9 UI withholds old professional facts, truthful latest state | new_document_ui_withholds_old_facts (detail/library HTML, safe text) |
| 10 identity boundary | identity_failure_respects_document_boundary (same/new × FAILED/manual; detail/library and bounded query) |
| 11–12 new-document not READY, actual retry restores search/rank/readiness | changed_document_failure_then_retry_restores_authority |
| 13 same-document snapshot remains valid | agent_profile_evidence_and_snapshot_preserved_then_stale; existing snapshot FAILED/manual parameterization |
| 14 invalid selected completion, no backward scan | newest_completed_invalid_evidence_fails_closed (same/new document), factual authority suite |
| 15 existing Java D1 → Python D2 success | test_folder_reconciliation.py |
| 16 query scale/bounds | search_and_member_lookup_do_not_query_each_history (mixed document boundary × 500/2 chunk size); #86 scale suite |

Executed separately from backend:

```bash
export UV_CACHE_DIR=/tmp/meyar-m9-uv-cache
uv run --offline pytest -q tests/test_m9_effective_profile.py
```

```text
36 passed in 7.57s
```

The 19-file focused boundary command above was rerun on the correction:

```text
374 passed in 89.72s (0:01:29)
```

Static checks rerun on corrected source:

```text
All checks passed!
Success: no issues found in 226 source files
```

Corrected full gate (diagnostic lines omitted):

```text
3511 passed in 819.97s (0:13:39)
b88a2c4d6e10 (head)
```

`git diff --check`: exit 0, no output. `scripts/scan-tracked-tree.sh`:
`Tracked-tree secret/real-data scan: clean.` Both checks are repeated against
intentionally staged correction files before commit. No database migration,
dependency or lockfile changed.

The full gate uses the same commands above, with test-only hang diagnostics
redirected to a temporary log. These diagnostics do not change production
behavior. The corrected head was subsequently independently accepted and
manually merged by the owner; exact-head CI and full-tree equality are recorded
above. The rejected reviewed head's successful CI remains historical only.

## Implementation files changed (PR #114, historical)

Exact repository-relative paths (root discovered with git rev-parse):

```text
README.md
backend/src/meyar/api/v1/evaluations.py
backend/src/meyar/cli.py
backend/src/meyar/models/candidate_embedding_version.py
backend/src/meyar/scoring/batch.py
backend/src/meyar/search/service.py
backend/src/meyar/services/agent_result_set_repo.py
backend/src/meyar/services/candidate_embedding_service.py
backend/src/meyar/services/candidate_identity_repo.py
backend/src/meyar/services/candidate_profile_repo.py
backend/src/meyar/services/identity_authority.py
backend/src/meyar/services/profile_authority.py
backend/src/meyar/ui/service.py
backend/src/meyar/ui/templates/candidate_detail.html
backend/src/meyar/ui/templates/library.html
backend/src/meyar/ui/view_models.py
backend/tests/test_agent_result_set_snapshot.py
backend/tests/test_batch_ranking.py
backend/tests/test_m9_effective_profile.py
backend/tests/test_ui_routes.py
docs/DECISIONS.md
docs/ISSUE_46_M9_VALIDATION.md
docs/MASTER_SPEC.md
docs/SECURITY_PRIVACY.md
docs/STATUS.md
```

## Remaining #46 risks and deferrals

M-9 is independently ACCEPTED + MERGED. #46 itself is NOT complete and stays
OPEN; M-9 acceptance does not close other runtime/ingestion/recovery hardening.
No automatic repair/backfill of historical
FAILED rows, partial-claim extraction, parser-version reprocessing, resurrection
of evidence-invalid COMPLETED profiles, photo fallback, or automatic historical
rescoring. Newest-COMPLETED evidence failure deliberately leaves no effective
facts; an explicit repair/new extraction is required.

Unresolved concurrency/reconciliation overlap and same-content races, folder
transaction lifetime/inference separation, storage-save/DB rollback compensation,
generic retention/legacy orphan cleanup, configured embedding readiness, and
remaining ingestion/resource/runtime/config/security items stay owned by #46.
No #35/#36/#50 advancement or Target-Mac work. See #46 and accepted
M-5/PR-3 records for other deferrals; no accepted slice is reopened.
