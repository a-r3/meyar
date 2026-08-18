---
name: implement-slice
description: Inspect relevant docs, implement the smallest vertical slice, test it, update status. Use when starting or continuing an MVP slice from docs/MVP_PLAN.md.
---

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
5. Run the `project-status` skill to update `docs/STATUS.md` with what
   changed, one line per fact — no long retrospective prose.
6. Report to the owner: implemented / tests+result / material issues /
   next slice / git status. Nothing more.
