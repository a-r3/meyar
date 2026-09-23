# MEYAR Ops — `meyar-ops`

Operator tooling for agentless deployment readiness (issue #35 — Slice 6,
M9). This document covers what **this PR** ("PR1") delivers. See
`docs/DECISIONS.md` D-066 for the material decisions behind it, and §11
below for the exact #35/#46 boundary.

## What `meyar-ops` is

`meyar-ops` is a normal typed, tested, linted Python package
(`backend/src/meyar/ops/`) with a console entry point registered in
`backend/pyproject.toml` (`[project.scripts]`) — not an ad-hoc shell
script. It is operationally separate from candidate business logic: it
never reads candidate/CV/JD/query content, and nothing in it feeds
matching, scoring, or search. It reuses the same trusted boundaries the
application already has — `meyar.llm`/`meyar.embedding` for Ollama
reachability, `meyar.llm.loopback` for loopback enforcement, the
`meyar.db` engine helpers, and Alembic's own `ScriptDirectory`/DB
`alembic_version` bookkeeping — rather than re-implementing any of them.

Run it from `backend/`:

```bash
uv run meyar-ops <command> [options]
```

## PR1 commands

### `preflight`

Non-destructive host/runtime checks useful *before* deployment. Never
writes, deletes, or connects to the database. Checks: Python major/minor,
`uv` availability/version, platform/architecture facts, required
filesystem paths, free disk space against a configurable minimum
(`MEYAR_OPS_MIN_FREE_DISK_MB`, default 2048), storage-root
existence/permissions, that the configured Ollama URL is loopback-only,
PostgreSQL client tooling (`psql`/`pg_dump`/`pg_restore`) as individual
findings, Alembic config/script-directory availability, and that the
configured LLM/embedding model names are syntactically present.

Missing *optional* tooling (e.g. no `pg_dump` on this host yet) is a
`WARN`, never a hard failure and never an uncaught exception — this
command does not choose a PostgreSQL provisioning topology (no Docker
requirement, no Homebrew/Postgres.app choice).

### `status`

Read-only operational metadata: installed `meyar` package version;
release identity from an installed release manifest when `--manifest
<path>` is given (otherwise reported as skipped — running from a source
checkout); Python version; platform; the code's single Alembic head; the
database's current Alembic revision (when reachable); storage-root
accessibility; Ollama reachability; and configured LLM/embedding model
identity and availability. Never reads candidate/database *content* —
only connectivity, revision identifiers, and configured model names.

### `readiness`

Component-level local operator readiness, composed from the same
primitives as `status` plus one write probe. **Not** the HTTP `/ready`
route — that is issue #46's scope; this is a CLI-only, human/operator-run
check. Six independently reported components: `database` (connectivity),
`db_migration` (DB current revision vs. exactly one code Alembic head),
`storage_write` (a bounded, self-cleaning write probe under the
configured storage root — never overwrites an existing file, always
cleans up, fails safely on an unwritable root), `ollama` (daemon
reachability), `llm_model`, and `embedding_model` (configured-model
availability). Every component's `Finding` is preserved even when another
component fails. No candidate data is read to determine readiness.

### `verify-release`

Verification only — never builds, installs, or extracts a release.
Verifies: the release manifest's schema; the full 40-character
`source_sha` format; the manifest's declared `release_id` against the
deterministic `(release_version, source_sha)` identity (and, if
`--expected-release-id` is given, against that too); the external
`SHA256SUMS` file's format; the artifact's actual SHA-256 against that
external checksum (a **hard failure** on mismatch); archive member safety
(rejects absolute paths, `..` traversal, symlinks, hard links,
device/special files, and any path escaping the expected `release_id/`
root — `extractall`/`extract` are never called anywhere in this
command); and, if the archive carries an embedded
`release_id/release_manifest.json`, that its identity is consistent with
the external manifest. Release-artifact *building* is deferred to a
later #35 PR — this PR's tests use synthetic fixtures built on the fly,
never real repository binaries.

## JSON result contract

Every command prints exactly one JSON object to stdout:

```json
{
  "action": "readiness",
  "ok": false,
  "started_at": "2026-09-23T11:07:12.545541Z",
  "finished_at": "2026-09-23T11:07:12.685578Z",
  "findings": [
    {"component": "database", "status": "OK", "code": "DATABASE_REACHABLE", "message": "database connection succeeded"},
    {"component": "embedding_model", "status": "FAIL", "code": "EMBEDDING_MODEL_UNAVAILABLE", "message": "model=nomic-embed-text available=False"}
  ]
}
```

`ok` is defined uniformly across all four commands: `true` iff no
`Finding` has `status: FAIL`. `WARN`/`SKIPPED` findings are reported but
never flip `ok`. Every component's finding is always present in the list
— nothing is ever dropped because an earlier component failed.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | `SUCCESS` — `ok: true` |
| 1 | `CHECK_FAILURE` — the command ran to completion but `ok: false` (a preflight/readiness/verify-release check failed) |
| 2 | `INVALID_INVOCATION` — bad/missing CLI arguments (argparse's own exit code) |
| 3 | `INFRASTRUCTURE_FAILURE` — an uncaught exception at the CLI boundary; still prints a valid `OpsResult` JSON object (never a bare traceback) |

## Security / no-exfiltration guarantees

- Never prints a database password or a complete database URL — only
  host/port/database-name-safe text (`meyar.ops.redact`), verified by
  `test_ops_no_exfiltration.py` and per-command tests that force a DB
  failure with a credential-bearing DSN and assert it never appears in
  the output.
- Never prints an API key, session value, or a raw environment dump.
- Never reads or prints candidate/CV/JD/query content — no command
  touches a candidate-owned table or file.
- Reaches Ollama only through the existing `meyar.llm`/`meyar.embedding`
  provider boundary (`require_loopback_url` +
  `build_local_only_async_client`) — `meyar-ops` never constructs its own
  HTTP client; a static AST-based test asserts no `ops/` module imports
  `httpx` (or `ollama`) directly.
- `verify-release` never extracts an archive to disk in this PR —
  `TarFile.extractall`/`.extract` are never called; a monkeypatch-based
  test asserts this at runtime, and a static grep-based test asserts it
  in source.

## What is NOT implemented yet

- No HTTP `/ready` route (`/api/v1/health` remains liveness-only,
  unchanged) — issue #46.
- No launchd/service lifecycle.
- No update/rollback orchestration (the `RollbackCompatibility` enum is a
  typed classification for a future human-operated runbook, not an
  executable mechanism — no automatic Alembic downgrade).
- No release-artifact *building* (only verification of an already-built
  artifact against fixtures).
- No production model approval — `meyar.ops.model_manifest` supports
  `PRODUCTION_APPROVED` as a schema value but nothing in this PR
  instantiates it.
- No PostgreSQL/Homebrew/Docker production provisioning topology choice.
- No comprehensive production config fail-closed hardening or SQL/
  exception logging hardening beyond what already exists — that is
  issue #46.

## #35 / #46 boundary

**This PR (#35 PR1):** local operator `preflight`, local operator
`status`, local operator component `readiness`, the release/model
manifest typed contracts, and release-artifact *verification*.

**Deferred to #46:** authenticated HTTP `/ready`, in-application
degraded-state semantics, comprehensive production config fail-closed
hardening, SQL/exception logging hardening, application recovery/
concurrency semantics.

**Deferred to #36:** real Target-Mac model selection & benchmark on the
confirmed Mac mini M4 Pro reference hardware. `meyar-ops` does not choose,
benchmark, or approve a production model.

## Platform verification status

Everything in this PR — `preflight`, `status`, `readiness`,
`verify-release`, and the full test suite — has been run and verified
**on Linux only**, against the local Ollama daemon and the local
PostgreSQL/`pgvector` container described in `docker-compose.yml`.

**Apple Silicon / Mac mini M4 Pro reference-hardware rehearsal remains
UNCONFIRMED.** No claim of Apple-Silicon runtime acceptance or
bank-Mac verification is made by this PR. That verification is issue
#36's scope, and depends on this PR's tooling first being reviewed and
merged.

**Production model remains TBD** — `qwen3:0.6b` is the source/default
development/integration setting, `qwen3:1.7b` is a recent local
acceptance/browser-runtime override; neither is benchmarked or
production-approved. See `docs/TARGET_MAC_BENCHMARK.md` and issue #36.
