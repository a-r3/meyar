# Issue #46 S2 — Logging / Privacy Hardening

S2 Diagnostic Privacy is independently **ACCEPTED + MERGED** through PR #117.
Accepted head `4ee99cbcd68faffbb820c8726ca0631f9f58e06b`; exact-head CI
[37115861862](https://github.com/a-r3/meyar/actions/runs/37115861862) **SUCCESS**.
Owner squash/main `a470305df37bfc952f74446834bfae6615b9bc00`; accepted and
merged full tree `0d4b8271af3db366f605495b35de83764ec315b4`.
The earlier rejected S2 head and its remediation are historical.
#46 remains OPEN; S2 only under M9 milestone 10.

## Verified starting state and scope authority

Local main, origin/main and live GitHub main matched
`95b65313920463af6ee03d4eb0072244e25ff089`; fast-forward-only pull was already
up to date. PR #116 is MERGED. Accepted S1 head
`436e01589d86cc6db2d7926d2801525e9555b702`, CI `37106111151` SUCCESS, and
owner squash have identical full tree `8c017e6eeae367fbb411e88e64a2faf4af2d9b32`.
#46/#35/#36/#45/#50 were verified OPEN; M9 remains OPEN. Hooks `.githooks`.
The completed **MEYAR — Issue #46 remaining-scope audit** (2026-10-03,
audited main `41833a91aaa1a57643a5020737d5574a80114cd5`) is the scope authority:
B17/B18, R04, and S2 — Diagnostic privacy boundary. Every selected finding
was rechecked on current post-S1 main, rather than inferred from the old audit.

## Reproduction and contract

Starting-main source was exported read-only to a temporary directory for
synthetic before-fix tests without resetting/replacing the working branch.
The initial 13 regressions failed (1.26s); eight additional extraction/evidence/
CLI-input cases failed (2.28s), the CLI argument case failed (1.11s), and the
archive failure finding failed (2.88s). These demonstrate raw canaries in SQL,
UI/ops/library/access/CLI/provider diagnostics and failure records, plus missing
safe structural API diagnostics. The same combined 23-case starting-main proof
was repeated sequentially after resuming: **23 failed in 2.76s**, with the source
export pinned to the verified starting SHA. The actual SQL engine had hide_parameters=False;
SQL/driver error detail and raw UI traceback exposed the synthetic values.
URL credential stripping alone left arbitrary candidate/secret text intact.
No real PII, real documents or external candidate-content requests were used.

Allowed diagnostic values: closed event/component/reason codes, bounded
exception class, safe method/status/count, and opaque UUID only where useful.
Forbidden: candidate identity/CV/query/JD text, prompt/output, tokens/cookies/CSRF/
passwords/auth headers, database URLs/passwords, SQL bind/literal/result values,
private original paths, exception messages/args/causes/tracebacks. There is no
content-sanitizer or debug bypass. Authorized product data presentation and
explicit one-time credential provisioning are not operational diagnostic logs.

## Sink review and changes

| Sink | Current-main finding and S2 treatment |
|---|---|
| SQL engine/pool | All seven app/operator/Alembic constructors now hide parameters. Real production Settings plus a real disposable DB query proves success-row/DEBUG and failed bind/driver diagnostics do not emit sentinels. A structural record projection also removes literal SQL, result rows and pool exception detail; hide_parameters alone cannot do that. |
| App logging config | No existing central app configuration. Idempotent `diagnostics.py` projects only SQLAlchemy, Uvicorn, HTTPX/HTTPCore record families before every handler, including handlers installed afterward; application log sites were inspected and use fixed structural messages. |
| Uvicorn access/error | Raw target/query/client and traceback data replaced by private targets plus closed method/status or component/code/type. Actual Uvicorn default access/error formatters and the real ASGI app are exercised. |
| Global API/UI errors | Shared closed handler logs component=http/code=UNEXPECTED_ERROR/type. ASGI middleware consumes unexpected HTTP exceptions before server traceback reporting, keeps UI headers and never sends a second response. Generic API 500 and validation 422 omit Pydantic raw input/context. Existing typed/auth/tenant handlers remain. |
| Parser/worker stderr | Already safe: real supervisor uses DEVNULL, worker logging disabled, strict closed IPC. Real malformed document and a real child writing unsafe stderr/error payload regressions prove no escape. No parser production change. |
| Local providers | Closed transport/schema errors with unchanged typed codes; unsafe exception chains removed. Invalid JSON/envelope/provenance returns typed safe failure. Existing local-only transport, prompts, admission, retry behavior and inference DB release unchanged. |
| Profile/identity failures | Provider/evidence exceptions may contain model labels/PII. New failure messages carry bounded type only; codes/status/retries remain. Legacy rows not rewritten; CLI no longer prints their raw error_message. |
| Folder/reconciliation | Existing structural audit failure metadata retained; unexpected processing logs closed component/code/type only. Fixed photo warnings remain. No filename/content/exception payload or transaction/concurrency changes. |
| Storage/auth | Existing structural storage/photo warnings and generic auth refusals preserved. Sentinel storage, invalid API/password/cookie and CSRF paths prove safe response/log output. No storage compensation or auth policy change. |
| Ops/CLI | Exception helper never calls str/repr/args. All existing Finding codes/components retain context; final raw archive-message sink uses the helper. CLI error output drops raw exceptions, paths, validation input, legacy failure text and argv echo; planner output retains structural presence/status rather than filter content. |
| Agent/search/planner | No raw operational logger identified; typed errors have closed transports/codes, provider/schema failures use the same boundary and generic global handling. Existing structural AuditEvent metadata untouched; shared no-exfiltration/auth/tenant/inference regressions included. |

The existing planner CLI test now checks the actual returned Java/5-year plan
and zero model calls, while asserting filter text is absent from stdout. It does
not weaken deterministic planner validation to accommodate privacy-safe output.
Nine existing photo recovery cases in `backend/tests/test_candidate_photo_service.py`
now assert the safe HTTP 500 body and the same OSError/ValueError type in the
structural log. Their original byte/hash/row/tenant isolation and retry assertions
are retained; no storage/recovery production code changed.

## New exact regression manifest

43 parameterized cases across two new files:
`backend/tests/test_logging_privacy.py`, `backend/tests/test_logging_privacy_runtime.py`.

```text
test_real_sql_logs_and_driver_exception_do_not_emit_bound_candidate_values
test_global_failure_has_safe_response_and_structural_log
test_ops_uncaught_failure_does_not_render_exception_payload
test_provider_failure_diagnostics_exclude_prompt_response_and_cause
test_library_diagnostics_cannot_emit_raw_payloads_even_at_debug
test_access_log_does_not_emit_request_target_or_client_identity
test_authentication_failures_do_not_log_api_password_cookie_or_csrf
test_api_validation_does_not_report_input_or_exception_context
test_csrf_refusal_never_logs_form_or_session_content
test_processing_failure_records_and_cli_do_not_carry_provider_payload
test_candidate_evidence_failure_does_not_store_model_authored_label
test_storage_failure_has_generic_response_and_safe_error_log
test_exception_projection_never_evaluates_str_repr_or_args
test_release_failure_finding_does_not_echo_unsafe_archive_member
test_malformed_model_output_has_closed_error_without_validation_input
test_invalid_provider_http_json_has_safe_typed_error
test_parser_unsafe_stderr_and_failure_payload_are_discarded
test_real_malformed_parser_document_never_logs_candidate_text
test_folder_unexpected_failure_logs_only_structural_diagnostic
test_cli_invalid_search_request_reports_no_input_or_path
test_cli_unexpected_exception_has_closed_diagnostic
test_cli_argument_errors_do_not_echo_tokens_or_query
test_all_production_engine_constructors_hide_bound_parameters
test_real_uvicorn_failure_and_access_logs_are_private
```

Sentinels separately represent synthetic name/email/phone, CV text, HR query/JD,
API/cookie/CSRF/password, DB password, model output and private path. Assertions
capture actual SQL/application logs, HTTP responses, stderr/CLI JSON and safe
failure records; they require canary absence and structural diagnostic presence.
The engine-constructor inventory protects all seven production call sites.

## Quality gates

Historical gates for the acceptance-REJECTED `5e40f9f...` implementation
(they do not establish acceptance):

| Gate | Result |
|---|---|
| `uv run ruff check .` | PASS |
| `uv run mypy src` | PASS — 229 production source files |
| `uv run pytest -q` | **3,611 passed in 922.43s (0:15:22)** |
| `uv run alembic heads` | Single unchanged `b88a2c4d6e10 (head)` |
| `git diff --check` / staged diff check | Clean |
| `scripts/scan-tracked-tree.sh` | Clean, including intentionally staged new files; pattern-based guard |

From the repository root, the reproducible gate commands are:

```bash
cd backend
uv run ruff check .
uv run mypy src
uv run pytest -q
uv run alembic heads
uv run pytest -q tests/test_logging_privacy.py tests/test_logging_privacy_runtime.py
cd ..
git diff --check
scripts/scan-tracked-tree.sh
```

Completed focused gates: **801 passed in 246.70s**, covering the 43 new S2
cases, no-exfiltration, auth/tenant, parser/provider/folder/search and ops
compatibility. Photo recovery plus new S2 cases: **70 passed in 127.59s**.
After adding explicit structural-type assertions to the nine adjusted HTTP
cases: **9 passed in 40.89s**. No production change followed these runs. The final post-migration log-capture
isolation compatibility gate (real migration deliberately first, photo cases and
all 43 S2 regressions): **71 passed in 155.38s**.

Verification is sequential because the synthetic DB schema fixture is shared.
A provisional compatibility run overlapped a reproduction and was discarded;
its folder failure lost the tenant row during the schema reset. Its planner CLI
assertion also expected filter text, corrected by retaining actual semantic
assertions and requiring private stdout. These provisional results are not gates.
After resuming, one test attempt found the local PostgreSQL service stopped
(97 fixture setup errors, interrupted); the documented development service was
started and the suite restarted. This was an environment failure, not a gate.
The first full attempt observed nine photo tests expecting propagated raw HTTP
exceptions and was interrupted. A two-case focused reproduction confirmed
`DID NOT RAISE OSError` (2 failed in 8.46s): the new safe HTTP boundary intentionally
consumes the exception. Only the HTTP expectation was corrected as described
above; the full gate was restarted afterward. That restart exposed prior
in-process migration tests disabling application loggers through Alembic
fileConfig. A standalone configuration reproduction changed UI logger.disabled
from False to True. The opt-in `enabled_diagnostic_loggers` test fixture restores
disabled flags for log-capture cases only and restores them afterward; levels,
handlers, privacy projection and production configuration remain intact. A real
fresh-server subprocess remains separately tested. The final full gate follows
a focused run with the real migration deliberately first.
No test exclusions, CI bypass, real PII or unrelated source fixes are used.

## PR #117 acceptance blocker remediation

Rejected exact head: `5e40f9f52a0b377741274a3d4f23724c1cb84a4c`.
Green original CI did not establish acceptance. That head left raw operator
paths/member names in failure Findings, and the previous #35 deferral claim
was incorrect. Operational FAILURE/ERROR privacy is owned by S2.

Before production editing, the new synthetic tests ran against that rejected
source: **11 failed, 1 passed in 1.27s**. All 11 failures were the serialized
sentinel-absence assertion: missing output directory, existing output target,
selected overlong source member, missing/ambiguous artifact and manifest checksum
entries (four cases), unsafe archive member, unavailable configured LLM/embedding
identities in readiness/status (two cases), and missing preflight path. The
storage non-directory failure was already private and passed. An initial invalid
storage test fixture was corrected before this proof; provisional fixture errors
are not evidence of production leakage. No real operator path/data was used.

| Reviewed family / exact old sink | Correction / retained structure |
|---|---|
| `build_release`: OUTPUT_DIR_INVALID / OUTPUT_TARGET_EXISTS | Fixed copy; code/component/status unchanged, including exclusive-create race refusal. |
| `build_release`: MEMBER_NAME_LENGTH_EXCEEDS_BOUND | Remove example member/repr; preserve offending count and numeric limit. |
| Build/Git/output failure constructors and unsafe archive self-check | Fixed structural exception copy; no source/member/argv/stderr/value/path summaries. Internal attributes needed for processing remain private and are never serialized. |
| `verify_release`: CHECKSUM_ENTRY_MISSING/AMBIGUOUS; MANIFEST_CHECKSUM_ENTRY_MISSING/AMBIGUOUS | Fixed artifact/manifest role, no supplied filename; ambiguity/missing codes unchanged. |
| `verify_release`: UNSAFE_ARCHIVE_MEMBER / ARCHIVE_RESOURCE_BOUND_EXCEEDED | Count-only violations / fixed resource-bound copy; no member names, reason payload or exception rendering. |
| `verify_release`: UV_LOCK_MEMBER_MISSING/AMBIGUOUS/NOT_REGULAR_FILE/TOO_LARGE, lockfile ARCHIVE_UNREADABLE | Fixed member role; preserve duplicate count, declared size/limit and bounded exception type where applicable. No release-root/member-path echo. |
| `verify_release`: INTERNAL_MANIFEST_AMBIGUOUS/NOT_REGULAR_FILE/TOO_LARGE | Fixed role plus count/size/limit; no internal manifest path. |
| Readiness SCHEMA_MISMATCH; status stale/multiple revisions | Fixed mismatch/stale state or head count; no DB-supplied revision strings. |
| Readiness/status LLM_MODEL_UNAVAILABLE / EMBEDDING_MODEL_UNAVAILABLE | Presence/availability booleans, no arbitrary configured model text, including WARN cases. |
| Preflight PATH_MISSING; service-status PLATFORM_UNSUPPORTED | Fixed missing/platform refusal; no path name or injected platform string. Required path names came from a closed built-in tuple, but failure copy no longer depends on it. |
| Host configuration failure reasons | `HostConfigFailure(ValueError)` carries the existing closed code; allowlisted `.code` replaces forbidden `str(exc)` evaluation. Untyped/Pydantic/OS failures keep CONFIG_VALUES_INVALID. |
| Offline bundle failure; Alembic introspection/static parse; UTF-8 sidecar decoding | Bounded exception type / fixed copy; no exception message evaluation or raw parse/driver payload. |
| Internal update worker argparse | Silent structural refusal/exit 1, no argparse argv echo. Public ops/offline-install parsers already had private refusals. |
| Backup/restore/update/install/schema-init/AI/service-lifecycle/cleanup/edge/reboot/diagnostics/deployment-ready | Reviewed fixed result copy or closed-code projection; existing numeric counts and typed reason fields retained. No lifecycle, subprocess, storage or readiness-policy redesign. |
| Storage probe/status/preflight; plist failure keys/argv | Existing fixed messages or bounded exception type; interpolated plist keys/tool names are closed module constants, not supplied values. Existing structural assertions retained. |

The automated AST inventory records **106 nonliteral failure/conditional or
result-helper sinks** in `backend/tests/fixtures/ops_failure_diagnostic_sinks.json`.
It includes OpsResultBuilder/Finding/result-helper calls, storage delegates and
standalone install JSON messages. Fixed string messages and explicit OK inventory
are allowed; changing or adding a nonliteral sink requires reviewed source
classification. Numeric counts/limits, booleans, fixed module constants, safe
exception types and closed code projections are reviewed allowed expressions.
Delegated storage/lifecycle messages were traced to fixed producers. A separate
AST guard refuses str/repr/args/cause/traceback and direct exception interpolation.
This is an explicit sink manifest, not a global ban on string interpolation.
Expressions are compared by semantic AST, not ast.unparse quote style. First
remediation CI run `37114242183` on intermediate head
`909c433f8f6cc78988847b9cb0a0d12f5076a27c` had **3631 passed / 1 failed**:
Python 3.12.15 in CI and local 3.12.3 unparsed the same plist-key f-string with
different outer quotes. The source expression was unchanged and private. The
comparison now normalizes both expression ASTs; an added regression proves
equivalent quote styles compare equal while a raw private_path expression still
differs. No production correction or existing assertion was removed for this
CI repair. The failed run is not the final gate.

A second before-fix proof confirmed **2 failed in 0.81s**: status schema-valid
manifest free strings were echoed as OK identity metadata. `release_identity_text`
now displays only a bounded numeric/closed a/b/rc version plus exact derived
hex-commit release identity; other metadata gets fixed availability copy. This
is output classification, not a secret detector or a new artifact validator.
Existing valid-release identity assertions remain unchanged. The provisional
full run was stopped for this additional correction and is not a gate.

Intentional successful release identity/checksum metadata remains product
inventory. Arbitrary output locations/plist labels and unvalidated release/model
values are omitted from success messages as well, because those values can
contain sensitive input. Release artifacts/manifests, one-time credentials,
identity/checksum fields and provisioning behavior remain unchanged; no #35
artifact-lifecycle work is advanced.

New regression functions in `backend/tests/test_ops_diagnostic_privacy.py`
(**22 cases**, alongside the unchanged existing 43 S2 cases):

- `test_build_failure_paths_and_members_are_private` (3)
- `test_checksum_filename_failures_are_private` (4)
- `test_unsafe_archive_member_failure_is_private`
- `test_unavailable_configured_model_diagnostics_are_private` (2)
- `test_preflight_missing_path_diagnostic_is_private`
- `test_preflight_storage_failure_is_private`
- `test_update_worker_invalid_argv_does_not_echo_private_input`
- `test_host_config_failure_does_not_evaluate_exception_text`
- `test_internal_member_failure_does_not_echo_private_root` (3)
- `test_ops_failure_diagnostic_sink_inventory_requires_review`
- `test_inventory_compares_structure_and_still_rejects_private_path`
- `test_readiness_schema_mismatch_does_not_echo_private_revision`
- `test_success_inventory_does_not_echo_arbitrary_manifest_identity` (2)

Before the final success-inventory correction, the focused gate passed
**714 in 25.40s**, including all 43 existing S2 cases and the initial 19
remediation cases. Final complete focused gate: **716 passed in 25.60s**, including all 43 existing
S2 regressions, all 21 remediation cases, all ops suites and both no-exfiltration
suites. Intermediate-head full gate: **3632 passed in 833.92s (13:53)**.
Final post-inventory portability gates: **717 focused passed in 30.95s**;
**3633 full passed in 866.41s (14:26)**. Ruff/mypy(src) clean; unchanged
single Alembic head, diff and tracked-tree scans clean. Exact delivered
head/CI are recorded in delivery. The prior affected-release/preflight/plist gate passed **174 in 4.72s**.
Final Ruff clean; mypy(src) clean on 229 files; unchanged single Alembic head
`b88a2c4d6e10`. Final full-gate results and exact delivered head/CI are recorded in the
operational delivery report. S1 accepted/merged history is unchanged.

## Scope and limitations

S1 accepted/merged wording is corrected minimally in STATUS, D-101,
S1 validation and SECURITY_PRIVACY, in this same S2 PR. D-102 defines this slice.
No migration, dependency or lockfile change; Alembic head unchanged. No S3+,
cache/no-store/docs policy, demo ownership, schema drift, asset compensation,
folder leases/concurrency/inference transaction refactor, readiness/model checks,
config-range hardening, retention implementation, #45/#35/#36/#50 advancement,
deployment or Target-Mac work. `.aws` was neither inspected nor modified.

The application-owned diagnostics are the tested boundary. Independently
configured Ollama/OS/DB daemon logs and host-level policy remain separate;
physical deployed-host/Target-Mac log behavior is not claimed. Failure/error
diagnostic privacy belongs to S2, including operator-supplied inputs; it is not
deferred to #35. Intentional successful release identity and checksum metadata
remain explicit product inventory. Arbitrary output locations/plist labels and
unvalidated release/model values are omitted from messages, including success
copy, while release files/manifests and one-time provisioning are unchanged.
Existing accepted
folder transaction lifetime and unrelated DOCX ZIP serialization timestamp-test
flakiness remain deferred. Stored legacy failure rows are not rewritten.

## Delivery / HUMAN ACTION REQUIRED

Exact PR number, delivered head and exact-head CI are published in the PR and
operational delivery report, without a self-referential source commit.
S2 is independently accepted and owner-merged as recorded above. #46 remains OPEN.

The former S2 re-acceptance request is historical and has been fulfilled by the
owner. This S3 PR records that post-merge state; it does not reopen S2.
