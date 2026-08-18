# Testing rules

- Every new tenant-scoped endpoint/repository function gets a cross-tenant
  isolation test (tenant A cannot read/write/delete tenant B's resource).
- Policy-engine logic must have unit tests that run without calling the LLM
  (feed it fixed `CandidateProfile`/criterion inputs).
- New upload/parsing code gets a malformed/oversized/MIME-mismatch test.
- New prompt/extraction code gets at least one prompt-injection fixture test
  asserting the injected instruction has no effect on output.
- Run `pytest` + `ruff check` + `mypy` before considering a slice done (see
  `test-gate` skill). Don't run the full suite on every single edit — batch
  at slice boundaries.
