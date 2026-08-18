---
name: test-gate
description: Run applicable formatter/linter/type/test commands for the backend. Use at the end of a slice, not after every edit.
---

From `backend/`, run and fix failures before proceeding:

```bash
uv run ruff check .
uv run mypy src
uv run pytest -q
```

If `frontend/` exists and TS files changed, also run `npm run lint` and
`npx tsc --noEmit` there. Report pass/fail per command; do not paste full
raw output unless something failed.
