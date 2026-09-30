# Issue #87 implementation evidence

Status: implementation evidence for owner/independent review, not acceptance.
Accepted source baseline: `ac9a249359c9a5dcf417b4e3f269a2c0b92b3ab0`.
Issues #88 and #50 are outside this change.

## Current-state audit and root causes

Login verifies the password, then either creates a BrowserSession or signs a
stateless pending tenant-selection claim. Tenant selection reloads the User and
membership before session creation. Each authenticated request resolves the
cookie through BrowserSession, User and TenantMembership. Previously security
mutators changed hashes/active flags without revoking sessions: re-enable could
therefore revive the old cookie. Demo rotation reaches the same `set_password`
service after positive synthetic-demo identification. CLI password, user-active
and membership-active commands also use these repository mutators. No other
supported production writer bypasses them.

Agent GET resolves an owned durable conversation and renders its composer. POST
checks CSRF, resolves the conversation, reserves a #85 turn, releases the DB
before inference, revalidates in Phase B, then atomically commits transcript,
ResultSet/pending-draft pointers and clears the reservation. Before #87 it had
no durable request identity. A completed form could run again. Form length
validation also ran before CRLF normalization. The global UI validation handler
rendered without resolving the authenticated principal. Failed login had no
tenant-independent durable security trail.

## Accepted-main reproductions

The initial pre-edit reproduction pass covered password, user/membership
disable/re-enable and CRLF. The expanded eight-case harness was subsequently
run against an immutable accepted-main archive in a separate disposable
`meyar87_baseline_test` database. **8 passed** means all vulnerable assertions
were observed; it is not a passing security gate.

| Class | Observed accepted-main behavior |
|---|---|
| A | Password rotation: old cookie still returned 200. |
| B | Membership disable returned signed-out; re-enable revived the old cookie. |
| C | User disable returned signed-out; re-enable revived the old cookie. |
| D | Seed-demo rotation changed the temporary password; old cookie still worked. |
| E | Wrong-password 401 produced no durable audit event. |
| F | 4000 canonical LF characters transported as CRLF returned 422. |
| G | Exact completed ordinary POST replay appended a second turn pair and called the model again. |
| H | Exact completed JD POST replay appended a second pair and superseded D1 with D2. |

The retained harness is
`backend/tests/reproductions/issue87_accepted_main.py`. It is deliberately outside
normal `test_*.py` discovery: its assertions describe the vulnerable baseline.
To reproduce, export the accepted commit into a temporary directory, copy the
harness into its `backend/tests/`, replace `meyar_test` with a separate disposable
database name in that archive's `tests/conftest.py`, and run:

```bash
PYTHONPATH=src:tests /path/to/installed/backend/.venv/bin/python -m pytest -q tests/issue87_accepted_main.py
```

## Local browser checks — synthetic data only

The real local UI was exercised through the workspace browser, with a temporary
test-provider dependency override used only for JD integrity checks. Production
provider routing was not changed.

- Exactly 4000 multiline canonical characters submitted successfully (HTTP 200).
- 4001 characters returned 422 with the canonical text, Azerbaijani error and
  authenticated navigation preserved.
- An ordinary deterministic turn completed. After successful history replacement,
  reload/back/forward stayed on the canonical conversation GET with no new turn.
- A deterministic JD produced D1. Reload retained its exact confirmation URL/D1.
- A double-click attempt on a fresh composer committed exactly one additional
  pair (transcript length 6 to 8); the progressive disabled button blocked the
  second browser click. Explicit same-token concurrency/replay is proven by
  route tests, independently of this UI guard.
- Seed-demo rotation invalidated the existing browser cookie: reload landed on
  login. The fresh temporary password logged in. Historical conversation text
  remained visible, with no inherited pending-draft authority in the new session.
- A real local `qwen3:0.6b` Ollama smoke turn completed with HTTP 200. Its model
  proposal did not yield an executable search plan; the normal safe HR outcome
  was shown. This is infrastructure/integrity evidence, not model-quality or
  Target-Mac acceptance.

The first browser connection stalled during navigation, but server logs and a
fresh tab confirmed the completed request. The earlier POST forward cache miss
was resolved by progressive canonical GET history replacement. Durable server
idempotency remains authoritative with JavaScript absent.

## Verification authority

Final local gates on the implementation:

- `uv run ruff check .`: All checks passed.
- `uv run mypy src`: no issues in 204 source files.
- `uv run pytest -q`: **2820 passed in 224.80s**; no local ownership failures.
- `uv run alembic heads`: single `a87d4c6e2b19` head.
- Focused integrity, migration, pending-login and inference-boundary run:
  **62 passed in 15.54s**, before the final full-suite run.
- JavaScript syntax, tracked-tree privacy scan and Git whitespace checks pass.

Final command output, the PR's exact-head CI, and independent acceptance review
are the gates. D-091 documents the proposed lifecycle, retention/crash and
migration/downgrade policies. This file does not declare #87 accepted.
