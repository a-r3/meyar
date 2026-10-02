# Issue #46 PR-2 — bounded parsing and truthful failure authority

Implementation proposal from exact accepted main
`d5a06a2342abe4acfb9d072c22bf8f956497801e`. This is PR-2 only; it requires
independent review and owner acceptance. Issue #46 remains open under M9.

## Architecture and acceptance

The shared upload/folder ingestion service awaits bounded thread admission
before running the unchanged PR-1 validator. Validation still precedes hash,
storage and CandidateDocument creation. There is one admitted validation task
and at most four waiting callers per application event loop; cancellation
returns to the caller but the finite validation work retains its slot until
completion. This prevents abandoned requests from admitting unlimited threads.
PR-1 member/directory/declared and actual expansion/ratio checks are preserved.
Validation overload returns safe HTTP 503 before storage/document creation;
folder ingestion preserves its existing import-failure behavior (including the
separate, unresolved M-5 candidate-creation concern).

`DocumentParser.parse` stays async. `LocalTextParser` now supervises a fresh
Python process for each admitted parse, adapting the photo-worker module/stdin
pattern. It uses bounded streaming stdin/stdout, suppresses child diagnostics,
and replaces the inherited environment with a single fixed locale. Only bytes,
document type, input size and numeric output policy are sent. No filename,
storage path/handle, tenant, database session, credential or candidate identity
is passed. Libraries and canonical extraction run locally inside the child;
there is no external service, AI call or OCR.

The supervisor shields process creation so cancellation cannot lose a child
handle, cancels and joins IPC tasks, drains stdout without accumulating it,
terminates, escalates to kill after 0.5 seconds, and reaps before releasing
admission. Repeat cancellation cannot interrupt cleanup. The worker deadline
covers startup, input transfer, parsing and response transfer; cleanup occurs
outside that deadline so it cannot overwrite an earlier typed failure. Parent
IPC accumulates at most the result cap plus one byte, with a 64 KiB reader
high-water limit (asyncio transport buffering can overshoot by a finite read).
The parent rejects duplicate keys, unknown fields/codes, coercion, wrong parser
identity, invalid page/index/text semantics and all output limits before
canonical persistence. Nested model construction is count-bounded as well.

Successful PDF page numbering and one block per nonempty page are unchanged.
DOCX remains one logical page with body paragraphs only; empty paragraphs
continue to leave gaps in original paragraph indexes. Entirely blank,
image-only or zero-page PDFs and DOCX with no supported body text fail with
`INSUFFICIENT_EXTRACTABLE_TEXT`, without a CanonicalDocument. Mixed blank/text
PDFs retain empty pages. A short valid text such as `Hi` succeeds. Table-only
DOCX intentionally fails in PR-2. No claim that a no-text document is certainly
scanned is made. M-4 table/header/footer/text-box extraction is PR-3.

## Safety limits and development evidence

These are module-level safety policies, not throughput promises or Target-Mac
benchmark results. Limits are inclusive; exceeding one rejects the parse and
never silently truncates evidence.

| Boundary | Policy | Reason |
| --- | --- | --- |
| Active parser children | 1 per application loop | Serializes the potential 768 MiB allocation alongside the local model on the 7.5 GiB development host; aggregate multiplies with application processes. |
| Parser waiters | 4 | A short bounded burst, at default 10 MiB intake at most 40 MiB of waiting document bytes, independent of request count. |
| Admission wait | 3 seconds | Finite, short overload response; deliberately does not promise to serve an entire queued burst behind a worst-case document. |
| Worker elapsed time | 20 seconds | More than 30x the synthetic large-DOCX library time below; includes fresh interpreter startup/IPC and is independent of parser progress. |
| Cleanup grace | 0.5 seconds | Allows ordinary termination, then kills an unresponsive child and awaits OS reaping. |
| Worker address space | 768 MiB | About 100 MiB measured loaded baseline + 128 MiB accepted ZIP expansion with several-fold XML/library overhead and margin; worst-case documents may still be rejected safely. |
| PDF pages | 300 | Retains the accepted prior policy; checked before per-page extraction. |
| Canonical blocks | 10,000 | Matches a deliberately excessive 10,000-paragraph synthetic CV, bounding object/JSON amplification. |
| Unicode characters | 1,000,000 | Large margin over a normal CV, bounds a giant page and accumulated paragraphs. |
| UTF-8 text bytes | 4 MiB | Separate encoding bound, covers at most four bytes per accepted Unicode scalar with rounding headroom; also rechecked by the parent. |
| Serialized result | 8 MiB | Allows JSON escapes (up to six bytes per control character), 10,000 block/index records and page envelopes, with a separately enforced hard IPC boundary. |
| Validation active/waiting/wait | 1 / 4 / 3 seconds | Same bounded burst policy, separate from parser slots; PR-1 caps make verification finite. |

Pre-change Linux synthetic measurements (existing libraries, no new dependency):

| Input | Bytes | Validation | In-process library parsing | Result bytes | Peak RSS / virtual size |
| --- | ---: | ---: | ---: | ---: | --- |
| Synthetic four-paragraph DOCX | 36,681 | 0.0014s | 0.0071s | 336 | 63,764 / 89,540 KiB |
| Synthetic one-page PDF | 1,094 | below 0.0001s | 0.0016s | 276 | 63,764 / 89,540 KiB |
| Generated 10,000-paragraph DOCX | 39,381 | 0.0017s | 0.589s | 709,016 | 83,988 / 110,616 KiB |

These small reproductions justify conservative rejection boundaries only. They
do not measure adversarial worst cases, production concurrency, model sizing
or Apple-Silicon performance. A single library `extract_text()` or XML parse
may allocate before its text counter runs; the memory and elapsed-time limits
cover that window. PR-1 default intake is 10 MiB, per-member expansion 32 MiB,
total expansion 128 MiB, members 1,000, expansion ratio 100 above 1 MiB and
central directory 1,000 KiB. None is weakened here.

## Linux and Darwin resource enforcement

Before document libraries/input, the worker feature-detects Python `resource`
and `RLIMIT_AS`, sets both soft and hard limits, checks their readback, and
requires an anonymous private mapping larger than the entire cap to fail with
`ENOMEM`. The probe reserves virtual address space without touching its pages;
if it succeeds it is immediately closed and parsing fails closed. Missing
capability, rejected setup, incorrect readback, unexpected probe errors or an
unenforced limit produce exit 72 / `PARSER_RESOURCE_UNAVAILABLE`. Allocation
exhaustion produces exit 73 / `PARSER_RESOURCE_LIMIT`; abnormal exits without
that typed signal are `PARSER_WORKER_FAILED` (no guessed OOM diagnosis).

Darwin is not skipped. Apple's published XNU
[setrlimit implementation](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/kern_resource.c)
passes RLIMIT_AS into
[Mach VM size-limit enforcement](https://github.com/apple-oss-distributions/xnu/blob/main/osfmk/vm/vm_map.c).
Runtime verification is required instead of inferring support from a platform
name or a successful setter. If a Mac runtime baseline itself exceeds 768 MiB,
setup refuses and parsing remains unavailable, rather than silently raising or
removing the safety cap. Older/unsupported runtimes also fail closed.

Linux real subprocess refusal/lifecycle is tested here. Darwin feature-path
unit tests use injected primitives on Linux and are **not macOS execution**.
Actual Python/library baseline, limit enforcement, normal parsing, timeout,
cancellation/kill/reap and recovery on the owner Mac remain part of later
agentless Mac rehearsal. Neither CI nor this proposal establishes #36 or
Target-Mac acceptance. No new dependency is used to emulate memory enforcement.

## Closed failure contract

New failures use existing CandidateDocument string fields; no migration.
Raw exception/library messages, CV text, archive member names, paths and
process environment are never used as new public/persisted parser metadata.
Worker diagnostics are discarded; general logging privacy remains separate.

| Stable code | Fixed public message |
| --- | --- |
| INVALID_DOCUMENT | The document could not be parsed. |
| PARSER_BUSY | Document parsing is busy. Please try again later. |
| PARSER_TIMEOUT | Document parsing exceeded the time limit. |
| PARSER_RESOURCE_LIMIT | Document parsing exceeded a resource limit. |
| PARSER_RESOURCE_UNAVAILABLE | Safe document parsing is unavailable. |
| PARSER_WORKER_FAILED | Document parsing could not be completed. |
| PARSER_STARTUP_FAILED | Document parsing could not be started. |
| INVALID_PARSER_OUTPUT | Document parsing returned an invalid result. |
| INSUFFICIENT_EXTRACTABLE_TEXT | Usable text could not be extracted. |
| PARSER_OUTPUT_LIMIT | Document parsing exceeded an output limit. |

A parser admission failure after intake creates the ordinary failed document;
a validation-admission failure occurs before document creation. Folder files
that passed validation and produced a failed CandidateDocument deliberately
remain `INDEXED` and count as successful ingestion, per the existing contract.
The next valid file still parses. This is not a fix for general M-5 or M-9.

## Version, historical authority and deferrals

Production parser version is **1.1.0**, reflecting resource/acceptance/failure
policy changes. Normal successful layout is unchanged. Existing canonical
JSON/evidence, profiles and identity versions remain immutable; no historical
rewrite, reference renumbering, automatic invalidation, or parser-version-only
folder backfill is introduced. PR-3 completeness requires its own version.

Deferred: M-4/PR-3; general M-5 phantom-candidate repair; M-9 failed
re-extraction authority; tenant-active enforcement; API cache/security headers;
general logging privacy; readiness; Alembic/model drift; reconciliation locks;
same-content concurrency; retention/orphans; all remaining #46 items; #35
agentless deployment tooling; #36 benchmark/model selection; #50 Q&A/comparison.

## Validation results

Actual Linux results (2026-10-02), synthetic/generated documents only:

| Gate | Result |
| --- | --- |
| Focused parser/upload/folder/reconciliation/PR-1 safety/multilingual/auth/tenant/no-exfiltration/original/preview suite | **212 passed in 92.10s** |
| `uv run ruff check .` | **All checks passed** |
| `uv run mypy src` | **Success: no issues found in 225 source files** (not `mypy .`) |
| `uv run pytest -q` | **3336 passed in 718.33s** (63 new tests over the accepted 3273-test baseline) |
| `uv run alembic heads` | **b88a2c4d6e10 (head)**, unchanged single head |
| `git diff --check` | Clean |
| `scripts/scan-tracked-tree.sh` | Clean; repeated after intentional staging to include new files |
| Migration / dependency / lockfile diff | None |

The focused invocation, from `backend/`:

```bash
uv run pytest -q tests/test_parser_isolation.py tests/test_parser_failure_integration.py tests/test_candidate_documents.py tests/test_folder_indexer.py tests/test_folder_reconciliation.py tests/test_docx_archive_safety.py tests/test_upload_body_limit.py tests/test_folder_scanner_bounds.py tests/test_multilingual_evidence.py tests/test_api_key_auth.py tests/test_tenant_isolation.py tests/test_no_exfiltration.py tests/test_ui_original_cv.py tests/test_ui_candidate_preview.py
```

Full required reproduction:

```bash
cd "$(git rev-parse --show-toplevel)/backend"
uv run ruff check .
uv run mypy src
uv run pytest -q
uv run alembic heads
cd ..
git diff --check
scripts/scan-tracked-tree.sh
```

Lifecycle tests launch real Linux children, including a SIGTERM-resistant
child that requires SIGKILL, cancellation during startup, repeated cancellation,
real refused over-cap allocation, invalid and overproducing responses, startup
failure, admission saturation and normal parsing after failures. Primitive
injection tests cover missing/rejected/unverified enforcement on both platform
labels. Boundary tests cover actual 300/301 pages, 10,000/10,001 blocks and
1,000,000/1,000,001 characters, plus injected exact/over Unicode UTF-8 and
serialized budgets, parent schema/semantic rejection, blank/image-only/zero/
mixed PDFs, table-only and unchanged body-paragraph DOCX. Integration checks
prove fixed persisted/API copy, no canonical/profile/identity authority after
failure, continued original access, next-file isolation, preserved INDEXED
semantics, and absence of parser-version-only backfill. PR-1 archive safety
and validation heartbeat/cancellation/admission remain covered.

Exact-head GitHub CI and owner acceptance are separate delivery gates; these
local results do not claim either or real Mac acceptance.

## Changed files

Paths below are relative to the canonical root returned by
`git rev-parse --show-toplevel` (no deployment/local checkout path is authority).

- `backend/src/meyar/api/v1/candidates.py`
- `backend/src/meyar/ingestion/admission.py`
- `backend/src/meyar/ingestion/parser.py`
- `backend/src/meyar/ingestion/parser_output.py`
- `backend/src/meyar/ingestion/parser_policy.py`
- `backend/src/meyar/ingestion/parser_supervisor.py`
- `backend/src/meyar/ingestion/parser_worker.py`
- `backend/src/meyar/ingestion/parsers/local_text_parser.py`
- `backend/src/meyar/ingestion/parsers/local_text_sync.py`
- `backend/src/meyar/ingestion/validation.py`
- `backend/src/meyar/services/candidate_document_service.py`
- `backend/src/meyar/services/folder_indexer_service.py`
- `backend/tests/test_candidate_documents.py`
- `backend/tests/test_parser_failure_integration.py`
- `backend/tests/test_parser_isolation.py`
- `docs/DECISIONS.md`
- `docs/STATUS.md`
- `docs/ISSUE_46_PR2_VALIDATION.md`
