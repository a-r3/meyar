# MEYAR

Canonical repository root: discovered via `git rev-parse --show-toplevel`
(never hard-code an absolute local path — the working copy may live at
any path, on any machine).

Canonical development remote:
`https://github.com/a-r3/meyar.git`

Product: MEYAR — Internal AI Candidate Intelligence & CV Search Platform.
This is **not** the superseded external B2B/SaaS product.

## Authority

`README.md` is the primary human onboarding entry point. It summarizes current
reality but does not replace the canonical detailed authority in `docs/`.

Before material work, read in order:

1. `docs/STATUS.md`
2. `docs/PROJECT_VISION.md`
3. Relevant sections of `docs/MASTER_SPEC.md`
4. Relevant entries in `docs/DECISIONS.md`
5. `docs/SECURITY_PRIVACY.md` when security, candidate data, or AI is affected

Before material editing, verify `git config --get core.hooksPath`. If absent
or not `.githooks`, run `scripts/setup-git-governance.sh`, re-check, and stop
if setup fails. Then query live GitHub PR, issue, milestone, remote-head, and
prior-merge state; do not infer it from memory or status docs. If GitHub is
unavailable, stop with `## HUMAN ACTION REQUIRED` and report the gap.

The shared `docs/` authority governs product and technical requirements.
This file is Codex's entry point; `CLAUDE.md` and `.claude/` provide Claude
orchestration, while `.githooks/` and `.github/` enforce agent-independent
governance.

## Non-negotiables

- Candidate AI processing is local only. Never send candidate data to an
  external AI, embedding, or cloud service.
- Real CVs and candidate PII must never enter GitHub. Only clearly synthetic
  CV fixtures may be committed.
- Never commit credentials, tokens, user-level Codex configuration, runtime
  storage, database dumps, or model artifacts.

## Git workflow

- Do not normally implement directly on `main`. Material work uses one task
  branch: `feat/*`, `fix/*`, `chore/*`, `docs/*`, or `test/*` as appropriate.
- Run the quality gate, inspect the diff, stage intentionally, commit the task
  branch, push it, open a PR to `main`, and verify CI.
- Dependabot PRs follow the same gate: CI passes, the owner reviews, then merge.
  Auto-merge is disabled. Major updates require explicit compatibility and
  migration review, and backend dependency changes include `backend/uv.lock`.
- Stop for owner review. Never merge automatically unless explicitly
  authorized. The default strategy is **Squash and merge**.
- PR creation or green CI is not completion. End the operational report with
  `## HUMAN ACTION REQUIRED`, the exact owner action/URL, what not to do, and
  the short reply expected. Wait for owner confirmation.
- After the owner reports a merge, verify the PR and remote `main`, then switch
  to `main` and pull with `git pull --ff-only origin main` before continuing.
  Verify linked issue closure and approved milestone progress/state before
  deleting the task branch or preparing the next task.
- Associate every material product task/PR with its applicable GitHub
  milestone when one exists. The canonical mapping is in `docs/MVP_PLAN.md`
  and `docs/STATUS.md`; never invent, rename, close, or reorganize milestones
  without an approved roadmap decision.
- Without explicit owner authorization, never force-push, use `reset --hard`,
  run a destructive clean, rewrite accepted history, bypass failed CI, or
  delete unmerged work.

From `backend/`, the quality gate is:

```bash
uv run ruff check .
uv run mypy src
uv run pytest -q
```

`mypy src` is currently clean. `mypy .` has known pre-existing test-only type
debt; do not claim whole-repository mypy is clean or widen CI to `mypy .` until
that debt is resolved.

## Persistent technical context

- D-005: Docker `network_mode: host` and port 55719 are a development-laptop
  workaround only, not CI or production architecture.
- `qwen3:0.6b` is integration-verified development infrastructure only. Final
  production model selection must occur on the target Mac Mini.
- The next product slice is Slice 6 — Local CV Library & Folder Indexer. It
  has not started. The old External Async Evaluation API Slice 6 is cancelled
  and superseded.
- The GitHub repository is currently a private personal development repository
  and may later migrate to an official bank-owned repository. Preserve full
  Git history during any migration.
