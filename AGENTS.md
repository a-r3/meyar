# MEYAR

Canonical repository:
`/home/oem/Documents/Job/RabitaBank/Meyar`

Canonical development remote:
`https://github.com/a-r3/meyar.git`

Product: Internal Rabitabank AI Candidate Intelligence & CV Search Platform.
This is **not** the superseded external B2B/SaaS product.

## Authority

Before material work, read in order:

1. `docs/STATUS.md`
2. `docs/PROJECT_VISION.md`
3. Relevant sections of `docs/MASTER_SPEC.md`
4. Relevant entries in `docs/DECISIONS.md`
5. `docs/SECURITY_PRIVACY.md` when security, candidate data, or AI is affected

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
- Stop for owner review. Never merge automatically unless explicitly
  authorized.
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
  and may later migrate to an official Rabitabank repository. Preserve full
  Git history during any migration.
