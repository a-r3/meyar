# Issue #46 PR-3 — ordered DOCX tables and safe provenance

Implementation proposal, awaiting independent acceptance review. Refs #46 under
M9 — Deployment, Benchmark & Integration Readiness. Do not merge or close #46
on this record alone. No Target-Mac execution or model-acceptance claim.

## Baseline and scope

Live origin/main was fetched and verified as
`9cc2cf9623b263ba4a2dd92217fce23c566ba0a7`, including the accepted PR-2 and its
PR #109 status correction. Tracked worktree was clean; hooksPath `.githooks`
was verified. Implementation branch: `feat/46-docx-ordered-tables`.

Only the approved M-4/PR-3 table subset is implemented. Full Word fidelity,
OCR, recovery and backfill remain deferred. General remaining #46 M-5/M-9
work is outside PR-3. #35 remains OPEN with substantial existing `meyar-ops`
implementation; PR-3 does not advance or modify #35. #36 and #50 remain OPEN
and are not advanced by PR-3. The real Target-Mac benchmark has not been
completed; no Target-Mac acceptance is claimed.
No migration, SQLAlchemy model, dependency, or uv.lock change.

## Ordered extraction and source coordinates

`meyar.ingestion.parsers.docx_source` contains all private python-docx/OOXML
parsing dependence. Tests assert the installed locked python-docx is 1.2.0.
Document and source-cell `iter_inner_content()` preserve direct paragraph/table
order. Tables traverse actual `w:tr` / `w:tc` once in source order. No
`row.cells`, layout-grid expansion or vMerge-origin resolution is used.

One global zero-based source-paragraph index includes empty paragraphs, nested
paragraphs and suppressed ambiguous merge paragraphs. Empty paragraphs emit
nothing but consume an index. Each emitted block contains exactly one source
paragraph. DOCX has one **logical** canonical page (page=1); no Word physical
pagination is inferred. Paragraph text follows the existing library behavior;
page-break text fusion is deliberately unchanged.

Optional closed source provenance in canonical JSON is either `{kind: BODY}`
or `{kind: TABLE, path: [{table, row, cell}, ...], paragraph, row_context_complete}`. Path steps record
ancestor table/source-row/source-cell ordinals; table ordinal is local to its
body or parent cell, and paragraph ordinal is local to its immediate cell,
including blanks. All coordinates are strict bounded integers, not visual-grid
coordinates or XML paths. Body and historical blocks need no table metadata.
No archive names, raw XML or free-form paths are persisted.

Horizontal and rectangular merged origin cells are extracted once. Empty
vertical continuations emit nothing. Nonempty continuation source text, including text inside unsupported wrappers,
is ambiguous: it is omitted, its paragraph consumes an index, and a closed warning
is recorded. No guessed rendering authority is assigned. Where such omission
exists, supported TABLE blocks in that immediate physical row cannot grant
context-dependent professional or identity authority. Other complete rows
remain eligible under existing same-block rules. BODY rules remain unchanged.

## Table evidence authority

Server-only provenance survives professional redaction and identity-view
construction. Original table context is retained solely for conservative vetoes
so redaction cannot erase a negative governor; both fields are excluded from
model prompt serialization and public responses. Positive evidence continues
to use the redacted cited block. The same shared
validators apply at initial extraction and current-authority reconstruction.
Positive values must still occur in one cited block/quote under existing rules;
sibling text never supplies a missing name, company, title, date, proficiency,
domain or any other positive material value.

For TABLE citations, immediate row membership uses the exact ancestor path and
last table/row coordinates, excluding nested rows and other tables. At most
64 supported blocks and 8192 source characters are inspected. Above either
bound, authority fails closed instead of truncating potential contradictions.
This is a conservative lexical veto, not NLP/model entailment. Any enumerated
negative governor (`no`, `without`, `neither`, `not` except `not only`) or
explicit `absent`/`unavailable` in that row vetoes positive professional
interpretation. In particular `No experience with | Python` cannot prove a
Python skill, regardless of source-cell direction or cropped quote.

All TABLE identity fields additionally reject the explicit English whole-word
labels `reference`, `referee`, `recommender`, and `emergency contact` in their
immediate supported row context, using existing case/whitespace normalization.
This applies equally to full_name, email and phone; no multilingual or semantic
classifier is introduced. Persisted identity reads reuse the same verifier via
`identity_authority.py`, returning no authorized values for rejected versions.

Phone evidence additionally rejects bounded sibling non-phone identifier
labels through the existing label grammar. A sibling `Phone` label cannot
make an otherwise unsupported bare numeric token into a phone. Email syntax,
same-block attribution, quote/location verification and historical BODY
behavior retain their existing rules. These choices can suppress valid table
facts where context is ambiguous; they never guess positive authority.

## Omission disclosure and terminal behavior

Five closed warnings, each at most once and in deterministic server order:

- `DOCX_HEADER_TEXT_OMITTED`
- `DOCX_FOOTER_TEXT_OMITTED`
- `DOCX_TEXTBOX_TEXT_OMITTED`
- `DOCX_AMBIGUOUS_MERGE_TEXT_OMITTED`
- `DOCX_TABLE_TEXT_OMITTED`

A bounded read-only scan checks the existing main source and existing related
header/footer parts. Default, first-page and even-page relationships are
included; shared parts are inspected once. No header/footer accessor creates
missing definitions or recursively follows section inheritance. Detection
checks nonblank `w:t` in those existing omitted parts and recognized
`w:txbxContent` containers. It never extracts shape text, collects arbitrary
`a:t`, interprets visual order, or joins AlternateContent branches. A warning
expresses detected omitted source text, not certainty about its rendered page.
The same bounded scan detects nonblank continuation-cell source text even
inside unsupported wrappers, solely to disclose omission; it never promotes
that content into canonical authority. Warning payloads contain only server
codes, never the omitted text, XML, paths
or member names. Blank/decorative shapes do not warn.

With supported body/table text, parsing succeeds and canonical JSON carries
durable warnings. Without supported text but with known omitted text, the new
terminal code is `UNSUPPORTED_DOCX_TEXT_ONLY`, with fixed public message:

> This document contains text in DOCX structures that are not yet supported.

The existing terminal ingestion path retains the authorized original and a
PARSE_FAILED document but creates no canonical authority or inference. The new
DOCX-only failure is not accepted for PDF by the parent protocol. Without
supported or detected omitted text, `INSUFFICIENT_EXTRACTABLE_TEXT` remains;
no scanned-document claim is made. Operational failure/retry contracts are
unchanged. Future explicit recovery remains possible and unimplemented.

## API and HR presentation

Authorized canonical API output adds only `partial_extraction: bool`. Its
existing block DTO strips internal source metadata; warning codes are not
returned. Detail document summaries also expose that boolean. Normal HR detail
and preview show fixed Azerbaijani copy:

> Sənədin bəzi hissələri hələ şərh edilə bilmir. Orijinal CV-ni yoxlayın.

Original access remains a separate authorized action. Evidence labels derive
from the source document's MIME type: PDF retains physical page wording;
DOCX/unknown sources use `CV-də` without a page claim. Profile, search, agent
evidence and ranking presentations use this same distinction. DOCX preview
uses `CV mətni`, not `Səhifə 1`. No table/row/cell/nesting ordinals or warning
codes appear on normal HR screens.

## Version, history and bounds

Parser version changes from 1.1.0 to **1.2.0** for changed table-only acceptance,
ordered indexes, provenance and completeness/failure semantics. Historical
canonical JSON, indexes, evidence, accepted profiles and identities are not
rewritten or invalidated just for a version change. Reads use existing
source refs. Unchanged folder files and old table-only terminal failures are
not automatically reparsed or backfilled.

Existing policy remains: one active child/four waiters, 3s admission, 20s
worker execution deadline, 0.5s cleanup grace, 768 MiB RLIMIT_AS, 300 PDF pages,
10,000 blocks, 1,000,000 characters, 4 MiB UTF-8 text and 8 MiB serialized
result. Existing ZIP/member/expansion/ratio bounds, fixed child environment,
cancellation-safe terminate/kill/reap and local-only processing remain.

Two new explicit policy limits:

- **8 nested table levels** bounds call stack and coordinate-list cardinality.
  Levels at the limit succeed; the next level fails with PARSER_OUTPUT_LIMIT.
- **100,000 source XML nodes**, aggregated across the main document and existing
  header/footer parts, bounds empty/unsupported traversal independently of
  emitted blocks. Read-only preflight visits nodes once; supported extraction
  and immediate-row omission detection use additional bounded source passes. Iterator-stack scanning avoids recursive
  detection and width-sized work queues. Exact/over tests exercise both the
  actual policy and the no-text outcome. Coordinates/indexes stay below this
  ceiling and malformed fields are rejected in the parent.

These are explicit security ceilings for oversized structural inputs, not
production-capacity or Mac benchmark measurements. Every emitted table
paragraph uses TextBudget; provenance and warnings contribute to worker/parent
serialized-result bounds. Warning cardinality is at most five; unknown,
duplicate, unsorted, malformed or over-limit metadata fails closed.

## Explicitly deferred Word structures

Header/footer/text-box **extraction** is deferred. SmartArt, footnotes/endnotes,
comments, revision wrappers, generic content controls, arbitrary AlternateContent
branches, embedded objects and generated list labels are not extracted.
Detection is limited to the closed omission mechanisms above and is not an
exhaustive Word-completeness claim. Page-break fusion is unchanged. No original
storage recovery, historical row migration or automatic retry/backfill design
is added.

## Reviewed-head validation (before independent correction)

Actual focused/full gate output is recorded below before delivery. Tests are
synthetic only. The documented local PostgreSQL container was initially stopped;
it was started for validation. Initial database-unavailable setup errors do not
count as test success. No application or production data was migrated.

Final focused command:

```bash
cd backend
uv run pytest -q tests/test_docx_tables.py tests/test_docx_table_integration.py tests/test_parser_authority_correction.py tests/test_parser_isolation.py tests/test_parser_failure_integration.py
```

**152 passed in 42.07s** against the final source. Coverage includes ordered
multilingual/nested tables, physical merged cells, giant gridSpan and long
vertical merges, unsupported-wrapper continuation disclosure, all four warnings,
strict parent rejection, exact/over text/structure/nesting/context/output limits,
initial/current evidence authority, redaction-resistant server-only negative
context, public API/HR rendering, original retention
and unchanged-file historical-failure non-backfill. Prior broader focused run:
396 passed with two old terminal-code-list expectation failures; those expectations
were corrected and the final focused rerun above passed. Preliminary full runs
were intentionally stopped before completion to add unsupported-wrapper and
redaction-safety regressions; they are not counted as completed quality gates.

| Final gate | Actual result |
|---|---|
| `uv run ruff check .` | All checks passed |
| `uv run mypy src` | Success: no issues found in 226 source files |
| `uv run pytest -q` | **3425 passed in 520.97s (0:08:40)** |
| `uv run alembic heads` | `b88a2c4d6e10 (head)` — unchanged single head |
| `git diff --check` / staged diff check | Clean |
| `scripts/scan-tracked-tree.sh` | Tracked-tree secret/real-data scan: clean |
| Migration / SQLAlchemy / dependency / lock diff | None |

Full pytest includes parser isolation/failure and memory/reap/recovery,
DOCX archive safety, folder reconciliation, extraction/evidence/current
historical authority, identity, tenant/auth, original-CV authorization,
no-exfiltration and UI rendering regressions. `mypy src` is the project gate;
no whole-repository `mypy .` cleanliness claim is made. CI is separately
verified against the published exact head and reported on the PR/delivery
record; green CI is not independent acceptance. #46 remains OPEN.

Reproducible final gates:

```bash
cd backend
uv run ruff check .
uv run mypy src
uv run pytest -q
uv run alembic heads
cd ..
git diff --check
scripts/scan-tracked-tree.sh
```

## Independent review correction

Same PR #110 / branch `feat/46-docx-ordered-tables`; reviewed head
`597a8d707a77545ef21e5f570126dfa0634b5719`. Both blockers reproduced before
production editing: **11 failed in 0.92s**, each because the required rejection
did not occur. Root causes: missing immediate-row omission authority and
phone-only sibling identity checks. Reproductions are not counted as success.

### Precise omission mechanism and historical authority

After bounded source preflight, an iterator scan checks each supported physical
row for nonblank `w:t` / `w:delText` outside the exact direct
cell/paragraph/run/text and paragraph/hyperlink/run/text paths used by the pinned
library. Text in content controls (`w:sdt`), skipped revision wrappers
(`w:ins`, `w:del`, `w:moveFrom`, `w:moveTo`), and recognized cell text boxes is
only detected, never extracted or assigned accepted/rejected revision semantics.
Omitted merge/text-box text retains its specific warning; other skipped table
text adds `DOCX_TABLE_TEXT_OMITTED`. The scan stores only one strict boolean
`row_context_complete` on each emitted TABLE source. No omitted text is retained.
Direct supported nested tables are skipped by the enclosing row scan and their
rows inspected independently; unsupported wrapped nested text remains an omission
in the enclosing row. This adds bounded passes with no source-node ceiling
change, recursive detection, layout-grid expansion or width-sized scan queue.

Worker and parent require the boolean on current output, consistent across
emitted blocks in the same immediate row; false requires a recognized table
omission warning. Missing/null/coerced/inconsistent metadata fails the protocol.
Evidence treats false or historical unknown/missing completeness as unknown
context and fails closed. This is row-local: unrelated rows and header/footer
stories do not invalidate complete rows. Historical TABLE blocks without the
boolean are conservatively withheld when current authority needs row context,
since their omitted text cannot be reconstructed from stored supported blocks.
Historical rows are never rewritten or automatically reparsed; this is a current
evidence rule, not parser-version-only invalidation. BODY blocks remain unaffected.
Metadata is excluded from prompts, canonical API and HR HTML.

### Correction tests and gates

New synthetic suite `backend/tests/test_docx_review_corrections.py` covers:

- `test_omitted_row_rejects_positive_and_current_profile_authority` — sdt,
  text box, insertion/deletion/move wrappers; no omitted text in canonical,
  prompt serialization or captured logs; current profile authority rejects.
- `test_reference_identity_rejected_on_extraction_and_persisted_rebuild` —
  reference name/email, emergency-contact name, referee phone, recommender email;
  unchanged persisted identity content, no authorized rebuilt values.
- `test_incomplete_context_is_immediate_row_local_with_body_unchanged`.
- `test_nested_row_is_separate_and_complete_hyperlink_text_stays_supported`.
- `test_complete_table_and_body_identity_preserved_and_incomplete_table_rejected`.
- `test_positive_identity_sibling_cannot_supply_missing_same_block_support` —
  full_name/email/phone must still be supported in their own cited block.
- `test_row_completeness_strict_worker_parent_contract` — missing/null/string/
  integer/inconsistent flags and missing warning rejected by parent.
- `test_historical_table_missing_completeness_is_unknown_without_rewrite`.
- `test_run_revision_and_content_control_omissions_are_not_extracted`.

Existing suites now cover five closed warning codes, unchanged 100,000-node/
depth/text/IPC boundaries and row-local ambiguous merge behavior. Upload/API/HR
integration adds content-control omission with friendly partial disclosure,
authorized original retention, no omitted text, raw code or completeness flag.
Header/footer and BODY text-box warnings preserve ordinary complete table facts.

Focused command:

```bash
cd backend
uv run pytest -q tests/test_docx_tables.py tests/test_docx_review_corrections.py tests/test_docx_table_integration.py tests/test_parser_authority_correction.py tests/test_parser_isolation.py tests/test_parser_failure_integration.py
```

**185 passed in 43.18s**. Ruff: all checks passed. `mypy src`: no issues in
226 source files. Full pytest: **3458 passed in 522.31s (0:08:42)**, including
tenant/auth, original-CV authorization, no-exfiltration, parser isolation/failure,
evidence/profile/identity authority and UI regressions. Alembic: unchanged single
head `b88a2c4d6e10`. Diff whitespace checks and tracked-tree secret/real-data scan
are clean. Migration/model/dependency/uv.lock diff is empty. Published exact-head
CI is verified separately in the PR/delivery record before requesting re-review.

Corrections preserve parser 1.2.0, physical traversal, all existing bounds,
process isolation, original retention, public DTOs and friendly HR copy.
No migration, dependency or lockfile change. No historical rewrite/backfill,
external AI/OCR or deferred extraction slice. PR-3 remains a proposal awaiting
independent re-review; #46 remains OPEN.
