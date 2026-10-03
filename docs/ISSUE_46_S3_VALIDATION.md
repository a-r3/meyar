# Issue #46 S3 — Runtime security / demo ownership / HTTP policy

Implementation proposed for independent acceptance. Refs #46, existing M9
milestone 10. #46 is not complete; #46/#35/#36/#45/#50 remain OPEN.

## Starting state and S2 post-merge evidence

Local main, origin/main, live GitHub main and remote main matched
`a470305df37bfc952f74446834bfae6615b9bc00`. PR #117 is MERGED, whose
accepted head is `4ee99cbcd68faffbb820c8726ca0631f9f58e06b`. Exact-head CI
[37115861862](https://github.com/a-r3/meyar/actions/runs/37115861862) SUCCESS.
Accepted and squash/main trees both equal
`0d4b8271af3db366f605495b35de83764ec315b4`. Its sole parent is accepted S1
main `95b65313920463af6ee03d4eb0072244e25ff089`. S2 is ACCEPTED + MERGED;
its prior rejected-head evidence remains historical. Hooks: `.githooks`.
Live issues, milestones, recent PR/prior-merge state and remote head were
queried before material editing. Task branch: `fix/46-s3-runtime-security`.

## Exact pre-fix synthetic reproduction

Before any production edit, the new ownership regressions ran against starting
main's source: **14 failed, 1 passed, 1 deselected in 6.66s**. Selection:
`-k 'legacy_unmarked or reset_deletes_only or shared_demo_member or ambiguous'`.
The exact `_bootstrap_demo_human_login` path found demo.hr by username, accepted
its membership on the marked tenant, called `set_password`, changed its hash/
security_version/updated_at, and revoked user-wide sessions even when the User
also belonged to another tenant. `reset_demo` deleted the same User whenever
it had a demo membership, cascading unrelated memberships/sessions too.
Legacy unmarked demo.hr was also password-rotated. Duplicate/conflicting/invalid/
missing-user creation markers were ignored; seed/reset proceeded. Only deletion
of a marked exclusive user matched the new contract and passed. No real data,
real operator paths or external candidate-content request was used.

## Final contract

D-103 records the architecture and compatibility decision. Trusted
`DEMO_HUMAN_BOOTSTRAPPED` events carry exact `user_id`, written only when the
application creates the synthetic human. Replacement events additionally carry
`previous_marker_id`; all events must form exactly one unbranched chain with
unique User IDs. Duplicate roots, duplicate Users, forks, cycles, disconnected/
malformed links, a missing current User or missing demo membership fail closed
before any destructive mutation, including API-key revocation. Audit history is
immutable. Username/membership alone never authorizes changing/deleting a User.

The current User is locked before ALL memberships are queried/locked; inactive
other-tenant memberships still prevent exclusivity. Password rotation requires
an exclusive active human with an active HR_USER membership. Reset may delete
only the current positively marked exclusive User. Shared/unmarked identities
and unrelated memberships/sessions survive the demo tenant cascade.

Legacy seed preserves the marked tenant/dataset and the old User's password,
active flag, security_version and updated_at. It creates a new marked exclusive
human, using demo.hr if free or a server-derived tenant UUID/random UUID username
on collision. Only obsolete demo-tenant membership stamps/access and their
BrowserSessions are retired; no underlying User stamp or unrelated session is
changed. Repeats rotate only the replacement. Plaintext password/API key is
returned once and never persisted/logged. Every seed revokes active keys only
on the positively marked demo tenant and issues exactly one usable fresh key,
including a marked empty legacy dataset.

`APIResponsePolicyMiddleware` centrally overwrites `http.response.start` headers
for exact `/api/v1` and `/api/v1/*` with `Cache-Control: no-store` and
`X-Content-Type-Options: nosniff`, irrespective of endpoint/status/body kind.
It wraps the safe-error middleware and request limiter and never consumes or
buffers response/request bodies. The limiter is inside the existing safe-error
boundary, preserving bounded multipart parsing and making unexpected limiter
failures private. No unsupported HTTP/1.0 need was found; Pragma is omitted.
JSON, real detail/search/evaluation/document metadata, 204, 401/403/404/422,
safe 500, TenantInactiveError and early 413 are tested. UI CSP/frame/referrer/
no-store remain unchanged. UI static was already no-store; Swagger assets retain
ETag/non-no-store semantics. Neighboring /api/v10 is not matched.

Current REST routes expose original-document metadata only; actual CV bytes use
`/ui/candidates/{candidate_id}/documents/{document_id}/original`. The real
human-authenticated download is tested. Isolated test-only REST FileResponse and
StreamingResponse routes prove header and byte preservation. No new production
original-CV REST endpoint is introduced under excluded #45 work.

`create_app` fixes docs routing at construction from trusted MEYAR_ENV. In
production, `openapi_url=None`, no `/docs` router and no `/docs-assets` mount:
all return ordinary `404 {"detail":"Not Found"}`, identical to an absent path,
without schema/config/model/candidate/secret detail. Development/test keep
`/docs`, `/openapi.json` and local JS/CSS/favicon, with no external runtime URLs.
CDN-backed default `/redoc` is disabled in all environments. Production health
and normal API auth remain functional. No docs auth subsystem or SSR redesign.

## Production files

- `backend/src/meyar/services/demo_seed_service.py`
- `backend/src/meyar/api/response_policy.py`
- `backend/src/meyar/main.py`

The existing same-name-user regression now also requires a usable separate demo
human, preserving its original unrelated-password assertion. All other original
demo/auth/privacy semantics remain asserted. Documentation updates record S2
acceptance, S3 policy and the printed-username compatibility behavior.

## Exact new regression names

`backend/tests/test_demo_user_ownership.py`:

```text
test_new_seed_marks_exact_created_human_and_never_logs_plaintext
test_outside_username_collision_preserves_security_and_gets_usable_demo_login
test_inactive_other_membership_still_prevents_demo_user_mutation
test_marked_empty_dataset_seed_revokes_only_demo_keys
test_legacy_unmarked_demo_member_is_preserved_with_usable_replacement
test_shared_demo_member_reseed_preserves_unrelated_session_and_identity
test_reset_deletes_only_marked_exclusive_human
test_ambiguous_human_markers_fail_before_any_mutation
```

`backend/tests/test_http_response_policy.py`:

```text
test_real_rest_candidate_search_evaluation_and_204_are_private
test_real_rest_safe_errors_are_private
test_inactive_tenant_generic_401_is_private
test_unexpected_safe_500_is_private
test_api_stream_and_file_bytes_receive_policy
test_early_upload_413_receives_policy
test_unexpected_upload_limiter_failure_is_safe_and_private
test_ui_headers_and_static_cache_behavior_remain_unchanged
test_api_policy_does_not_match_neighboring_path_prefixes
```

`backend/tests/test_docs_environment_policy.py`:

```text
test_nonproduction_offline_swagger_and_schema_are_local
test_production_docs_schema_and_assets_are_ordinary_not_found
```

## Verification

All required gates completed successfully. Synthetic DB test sessions
run sequentially; a provisional docs run was interrupted during its schema
setup when an overlapping test session was noticed and is not gate evidence.
The final focused and full gates run sequentially. Earlier bounded demo run:
49 passed in 149.41s; initial HTTP/docs run: 29 passed in 8.46s. Later cases and
middleware ordering are included in the final gates, not inferred from these
intermediate runs.

Final source checks (production source only for mypy):

```text
$ uv run ruff check .
All checks passed!
$ uv run mypy src
Success: no issues found in 230 source files
$ uv run alembic heads
b88a2c4d6e10 (head)
```

Affected compatibility suites: demo seed/reset/login, API key and human/session/
tenant authority, candidate detail/documents/real original CV, REST search/NL/
evaluation, error/privacy transports, offline Swagger, and application/operator
no-exfiltration. Initial compatibility output:

```text
........................................................................ [ 26%]
........................................................................ [ 53%]
........................................................................ [ 79%]
.......................................................                  [100%]
271 passed in 207.64s (0:03:27)
```

After the final limiter middleware ordering and additional ownership cases,
the complete S3 plus existing upload-boundary regressions ran on final source:

```bash
uv run pytest -q tests/test_demo_user_ownership.py tests/test_http_response_policy.py tests/test_docs_environment_policy.py tests/test_upload_body_limit.py
```

```text
..........................................................               [100%]
58 passed in 28.67s
```

The complete final-source gate included every affected S1/S2/auth/tenant/REST/
original-CV/docs/no-exfiltration suite; no exclusions or CI bypass:

```text
$ uv run pytest -q
........................................................................ [  1%]
........................................................................ [  3%]
........................................................................ [  5%]
........................................................................ [  7%]
........................................................................ [  9%]
........................................................................ [ 11%]
........................................................................ [ 13%]
........................................................................ [ 15%]
........................................................................ [ 17%]
........................................................................ [ 19%]
........................................................................ [ 21%]
........................................................................ [ 23%]
........................................................................ [ 25%]
........................................................................ [ 27%]
........................................................................ [ 29%]
........................................................................ [ 31%]
........................................................................ [ 33%]
........................................................................ [ 35%]
........................................................................ [ 37%]
........................................................................ [ 39%]
........................................................................ [ 41%]
........................................................................ [ 43%]
........................................................................ [ 45%]
........................................................................ [ 46%]
........................................................................ [ 48%]
........................................................................ [ 50%]
........................................................................ [ 52%]
........................................................................ [ 54%]
........................................................................ [ 56%]
........................................................................ [ 58%]
........................................................................ [ 60%]
........................................................................ [ 62%]
........................................................................ [ 64%]
........................................................................ [ 66%]
........................................................................ [ 68%]
........................................................................ [ 70%]
........................................................................ [ 72%]
........................................................................ [ 74%]
........................................................................ [ 76%]
........................................................................ [ 78%]
........................................................................ [ 80%]
........................................................................ [ 82%]
........................................................................ [ 84%]
........................................................................ [ 86%]
........................................................................ [ 88%]
........................................................................ [ 90%]
........................................................................ [ 92%]
........................................................................ [ 93%]
........................................................................ [ 95%]
........................................................................ [ 97%]
........................................................................ [ 99%]
......                                                                   [100%]
3678 passed in 874.68s (0:14:34)
```

`git diff --check` is clean. The tracked-tree scan is clean; the staged-tree
check is repeated after intentionally adding the new files. This guard is
pattern-based, not a comprehensive PII classifier. All new fixtures are
synthetic. No migration/dependency/lockfile change; single head unchanged.

## Scope and delivery

No migration, dependency or lockfile change. No external candidate AI, scoring
change, auth weakening, storage recovery, folder concurrency, readiness,
retention, Alembic drift, later Issue #46 bundle, #45/#35/#36/#50 advancement,
or Target-Mac work. The existing untracked `.aws` entry was not inspected or
modified. No automatic merge or issue closure. S3 acceptance remains pending.
Exact delivered PR head and exact-head CI belong in the PR/operational delivery
report (avoiding a self-referential source commit).

## HUMAN ACTION REQUIRED

Independently review the delivered S3 PR at its exact head. Do not merge, enable
auto-merge, close #46, start a later bundle or advance the excluded issues.
Reply `S3 PASS` or provide corrections. Green gates/CI are not acceptance.
