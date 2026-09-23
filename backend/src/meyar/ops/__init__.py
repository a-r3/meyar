"""meyar-ops — local operator tooling for agentless deployment readiness.

Issue #35 PR1 scope: non-destructive host/runtime preflight, read-only
status/readiness reporting, and release/model manifest contracts plus
release-artifact verification. This package is operationally separate
from candidate business logic (never reads candidate/JD/CV content or
influences matching/search/ranking) but lives inside the normal typed,
tested, linted application package surface — not an ad-hoc shell script.

Out of scope for this PR (see docs/MEYAR_OPS.md): authenticated HTTP
`/ready` (issue #46), launchd/service lifecycle, update/rollback
orchestration, release artifact building, and any production model
approval.
"""
