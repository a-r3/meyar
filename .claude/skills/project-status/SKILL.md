---
name: project-status
description: Update docs/STATUS.md and append concise decision records after material work. Use at the end of a slice or a material architectural change.
---

Update `docs/STATUS.md` in place (it holds only current state, not history):
current phase, completed, in progress, blockers, next slice. Keep each
bullet one line.

If a material, non-obvious decision was made during the work (an assumption
that resolved missing information, a deviation from the spec), append one
entry to `docs/DECISIONS.md` in its existing format (id, date, decision,
why, reversibility). Do not append a decision entry for routine
implementation choices already implied by `docs/MASTER_SPEC.md`.
