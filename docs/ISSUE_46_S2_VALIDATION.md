# Issue #46 S2 — Logging / Privacy Hardening

Status: proposed for independent acceptance; #46 remains OPEN. Only S2 under
existing M9 milestone 10. Branch `fix/46-s2-diagnostic-privacy`.

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

Final local gates, on the frozen S2 implementation:

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
physical deployed-host/Target-Mac log behavior is not claimed. Existing explicit
operator artifact/location inventory output remains governed by #35; this slice
does not claim arbitrary operator-supplied release manifests or artifact names
are a safe logging input. Existing accepted
folder transaction lifetime and unrelated DOCX ZIP serialization timestamp-test
flakiness remain deferred. Stored legacy failure rows are not rewritten.

## Delivery / HUMAN ACTION REQUIRED

Exact PR number, delivered head and exact-head CI are published in the PR and
operational delivery report, without a self-referential source commit.
Independent S2 acceptance remains pending; no merge or issue closure is claimed.

Independently review the new exact S2 head. Do not merge, enable auto-merge,
close #46, start S3+ or advance other issues. Reply `S2 PASS` or provide corrections.
