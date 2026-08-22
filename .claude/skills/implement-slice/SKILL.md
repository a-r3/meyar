---
name: implement-slice
description: Inspect relevant docs, implement the smallest vertical slice, test it, update status. Use when starting or continuing an MVP slice from docs/MVP_PLAN.md.
---

Follow `.claude/rules/git-workflow.md`'s startup protocol first (verify
repo/branch/tree/remote, create/confirm the task branch — never implement
on `main`). Then:

1. Read the target slice's row in `docs/MVP_PLAN.md` and its acceptance
   criteria. Read only the specific parts of `docs/MASTER_SPEC.md` relevant
   to this slice — don't reread the whole doc set every time.
2. Implement the smallest change that satisfies the acceptance criteria,
   following `.claude/rules/architecture.md` and
   `.claude/rules/security-privacy.md`.
3. Write/extend tests per `.claude/rules/testing.md`, including the
   cross-tenant isolation test if the slice touches a tenant-owned
   resource.
4. Run the `test-gate` skill.
5. Inspect the diff for secrets/PII/real-CV/runtime-file safety (`git
   status --short`, `git diff --cached`) before staging anything — never
   `git add .` blindly.
6. Run the `project-status` skill to update `docs/STATUS.md`/
   `docs/DECISIONS.md` with what changed, one line per fact — no long
   retrospective prose.
7. Commit on the task branch, push it, open a PR into `main`
   (`.claude/rules/git-workflow.md` has the exact commands). Do not
   merge, force-push, or bypass a failing test/CI.
8. Report to the owner: implemented / tests+result / material issues /
   PR link / git status. STOP for review — do not merge automatically.
