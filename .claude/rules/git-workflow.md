# Git workflow rules

Detailed operational authority for how MEYAR is developed in Git. See
`CLAUDE.md` for the concise summary and the canonical repo/remote
identity. This file is what governs the actual sequence of commands.

## Startup protocol — run before any material task

```bash
pwd
git rev-parse --show-toplevel
git branch --show-current
git status --short
git remote -v
git log -3 --oneline
```

Then determine, in order:

1. Is the repo path correct (`/home/oem/Documents/Job/RabitaBank/Meyar`)?
2. Is the working tree clean? If unexpectedly dirty, inspect and report —
   never `git reset --hard` / `git clean -fd` / `git checkout -- .` /
   any destructive restore to force a clean state.
3. What branch am I on?
4. Is this work appropriate for the current branch?
5. What is the latest accepted checkpoint (`git log`)?
6. What do `docs/STATUS.md` / `docs/DECISIONS.md` say about current state?
7. Does this task require a new branch?

Before material editing, verify hooks with `git config --get core.hooksPath`.
If missing or not `.githooks`, run `scripts/setup-git-governance.sh` and
re-check; if setup fails, stop. Then query live GitHub PR, issue, milestone,
remote-head, and prior-merge state. If unavailable, do not guess; stop with
`## HUMAN ACTION REQUIRED`.

If the task changes code or docs materially and the current branch is
`main`, **create the correct task branch before editing anything.** The
only exception is an explicitly owner-authorized repository-bootstrap or
urgent operation — not a default.

## Branch scope

One branch = one coherent task/slice. Naming: `feat/*`, `fix/*`,
`chore/*`, `docs/*`, `test/*`. Examples: `feat/cv-folder-indexing`,
`feat/local-embeddings`, `fix/pdf-parser-resource-limit`,
`chore/test-mypy-cleanup`. Don't combine unrelated refactors/features in
one branch; don't opportunistically rewrite unrelated working code.

## Safe staging

Never assume everything in the working tree belongs in the current
commit. Before every commit:

```bash
git status --short
git diff
```

Stage intentionally (never a blind `git add .`/`git add -A`), then
inspect what's actually staged:

```bash
git diff --cached --stat
git diff --cached --name-only
git diff --cached
```

Verify no secret, `.env`, PII, real CV, database dump, runtime file, or
model artifact is staged.

## Commit policy

Clear conventional-style messages: `feat: ...`, `fix: ...`, `docs: ...`,
`chore: ...`, `test: ...`. Commit coherent, tested states — not a noisy
checkpoint after every tiny edit.

## Task branch → PR → main

```bash
git push -u origin <branch>
gh pr create --base main --head <branch> --title "<type>: <summary>" --body-file <file>
```

PR body states: purpose, scope, material decisions, tests performed,
security/privacy impact, known gaps/deferred work. Use
`.github/pull_request_template.md`. **Do not merge the PR in the same
operation that creates it.** PR creation and green CI are not completion or
authorization to merge.

The default strategy for normal feature/fix/chore/docs/test PRs is **Squash
and merge**. Do not use merge commits or rebase-and-merge by default. After CI
is green, stop and end the operational report with:

```text
## HUMAN ACTION REQUIRED

1. Open the PR: <PR URL>
2. Confirm CI is green and review the diff.
3. Click "Squash and merge" and confirm the merge.
4. Do not start the next slice or modify main manually.
5. Reply: `merged`
```

After the owner reports the merge, never trust the message alone. Verify the
PR is actually merged and remote `main` contains the result, then synchronize:

```bash
gh pr view <number> --json state,mergedAt,mergeCommit
git fetch origin
git switch main
git pull --ff-only origin main
```

Then verify linked issue closure and approved milestone progress/state. If
inconsistent, investigate before branch deletion or next-task preparation;
never close or reorganize milestones without roadmap authorization.

Verify the expected squash commit/content is present. Delete the local task
branch only after that verification; a squash-merged branch may require local
deletion despite not being an ancestor of `main`. The remote branch may be
deleted automatically by GitHub. Never begin new work from the old task branch;
create the next approved branch from synchronized `main`.

## Issue and milestone traceability

For material product work, use the approved milestone from
`docs/MVP_PLAN.md` / `docs/STATUS.md`. One coherent deliverable gets one
GitHub issue when useful; avoid micro-issues for every test or tiny edit.
Normal traceability is: approved milestone → issue → task branch →
implementation → tests → PR → CI → owner Squash and merge → issue closes →
milestone progress updates. Associate the PR with the milestone and reference
the issue using `Closes #<issue-number>` when the PR fully delivers it.

Create/manage milestones and issues through authenticated GitHub tooling when
available. Never invent, rename, close, or reorganize milestones without an
approved roadmap decision. If tooling or authentication cannot perform a
required operation, use the mandatory `## HUMAN ACTION REQUIRED` format.

## Human-action checkpoints

Whenever progress requires owner action, say so explicitly and end the report
with `## HUMAN ACTION REQUIRED`: state what to do, where, the exact button/
command/value when known, what not to do, and the short reply expected. This is
mandatory for PR review/merge, authentication the agent cannot complete,
repository/account UI settings, paid-plan decisions, destructive Git actions,
irreversible production/security decisions, bank infrastructure/access,
target Mac Mini access, real-data approval/access, and business-owner
requirement confirmation. Never silently wait or fabricate completion.

## Forbidden without explicit owner approval

- Force push to `main` (`git push --force` to `main`)
- `git reset --hard`, destructive `git clean`, history rewrite
- Deleting an unmerged task branch that contains work
- Bypassing a failing CI gate or `--no-verify`

## Local Git guards

Hooks live in `.githooks/` and are activated per-repo via
`scripts/setup-git-governance.sh` (`git config core.hooksPath
.githooks`) — no global Git config is touched. `pre-commit` blocks
obvious secrets/private keys/real-CV-shaped paths/runtime storage.
`pre-push` blocks a direct push of local `main` to remote `main` after
the initial bootstrap push, unless `MEYAR_ALLOW_MAIN_PUSH=1` is set
explicitly by the owner for a genuine emergency — never set this
automatically.

## Sensitive-data rule

GitHub may contain: application source, documentation, migrations,
synthetic fixtures (`fixtures/synthetic_cvs/`). GitHub must never
contain: real CVs, candidate PII, real database dumps, `.env`,
credentials, model blobs/weights, runtime storage (`backend/var/`).

## Dependency updates

Dependabot opens weekly PRs for backend `uv` dependencies and GitHub Actions.
Every dependency PR goes through the normal CI gate and owner review; there is
no automatic merge. Major updates require explicit compatibility, migration,
and security/privacy review. Commit `backend/uv.lock` whenever a backend
dependency change updates resolution. A future semver-patch-only auto-merge
policy would require a separate accepted decision; never auto-merge major or
minor dependency updates or application feature PRs by default.

## Repository identity

Canonical local repo: `/home/oem/Documents/Job/RabitaBank/Meyar`.
Canonical development remote: `https://github.com/a-r3/meyar.git`
(`a-r3/meyar`, private). This is a personal development remote and may
later be migrated to an official bank-owned repository — full Git
history must be preserved on that migration; never rewrite history
merely because the remote owner changes.

## Task completion checklist

1. Run focused tests for the change.
2. Run the project quality gate (`test-gate` skill: `ruff check .`,
   `mypy src`, `pytest -q`).
3. Inspect the diff.
4. Check secrets/PII/real-CV safety.
5. Update `docs/STATUS.md`/`docs/DECISIONS.md` if material.
6. Commit on the task branch.
7. Push the task branch.
8. Open a PR into `main`.
9. Verify CI actually ran (and is green, or report the genuine failure).
10. STOP for review — do not merge.
