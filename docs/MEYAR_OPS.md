# MEYAR Ops — `meyar-ops`

Operator tooling for agentless deployment readiness (issue #35 — Slice 6,
M9). This document covers PR1 (`preflight`/`status`/`readiness`/
`verify-release` — `docs/DECISIONS.md` D-066) and PR2 (`service-render`/
`service-verify`/`service-status` — D-067). See §11 below for the exact
#35/#46 boundary.

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

Local operator `preflight`, `status`, `readiness`, and release
*verification* (`verify-release`).

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
`db_migration` (DB current revision(s) vs. exactly one code Alembic
head — a genuine revision-query/permission failure is reported as its
own truthful `ALEMBIC_REVISION_QUERY_FAILED`, never folded into "no
revision"; more than one DB revision row is its own truthful
`MULTIPLE_DB_REVISIONS`, never silently collapsed to the first row),
`storage_write` (a bounded, self-cleaning write probe under the
*already-provisioned* storage root — never creates the root or any
parent directory, never overwrites an existing file, always cleans up,
fails safely — truthfully "not writable" — on a missing or unwritable
root; a probe-path UUID collision with a pre-existing file is reported as
not writable and that pre-existing file is never deleted, since this
invocation never created it), `ollama` (daemon reachability), `llm_model`,
and `embedding_model` (configured-model availability). Every component's
`Finding` is preserved even when another component fails — including when
LLM/embedding provider construction or `.health()` raises a
non-normalized exception (a malformed local Ollama response, an
unexpected provider-internal failure): that is isolated per provider and
reported as its own truthful `OLLAMA_HEALTH_CHECK_ERROR` /
`EMBEDDING_HEALTH_CHECK_ERROR` finding, never allowed to escape
`run_readiness()` and discard findings already collected. `status` applies
the same per-provider isolation to `ollama_reachability` /
`configured_llm_identity` / `configured_embedding_identity`. No candidate
data is read to determine readiness.

### `verify-release`

Verification only — never builds, installs, or extracts a release.
Verifies: the release manifest's schema; the full 40-character
`source_sha` format; the manifest's declared `release_id` against the
deterministic `(release_version, source_sha)` identity (and, if
`--expected-release-id` is given, against that too); the external
`SHA256SUMS` file's format (a filename with more than one well-formed
digest entry is rejected as ambiguous, never resolved last-write-wins);
the artifact's actual SHA-256 against that external checksum (a **hard
failure** on mismatch); **the external release manifest's own SHA-256
against a `SHA256SUMS` entry keyed by the manifest's filename** (a
missing, ambiguous, or mismatched manifest entry is its own hard
failure) — the release bundle is artifact + manifest + `SHA256SUMS`
together, and a single `release_bundle_integrity` finding is `OK` only
when both the artifact and the manifest checksums matched (`SHA256SUMS`
is deliberately never included in its own checksum set — this stays
integrity verification, not code signing/publisher identity); archive
member safety (rejects absolute paths, `..` traversal, symlinks, hard
links, device/special files, and any path escaping the expected
`release_id/` root — `extractall`/`extract` are never called anywhere in
this command); that the archive's `release_id/backend/uv.lock` member is
present exactly once, is a regular file, and its SHA-256 matches
`ReleaseManifest.uv_lock_sha256` (binding the manifest's declared
lockfile digest to the artifact's actual contents, not just to
hex-string syntax); and, if the archive carries an embedded
`release_id/release_manifest.json`, that it is present **at most once**
(an ambiguous duplicate — never silently resolved by
`TarFile.getmember()`'s last-write-wins behavior — is its own hard
failure, `INTERNAL_MANIFEST_AMBIGUOUS`), is a regular file (a
directory/non-regular member is rejected as `INTERNAL_MANIFEST_NOT_REGULAR_FILE`),
and is **fully** consistent with the external manifest — every field, via
typed model equality, not only `release_id`/`release_version`/
`source_sha`. Archive inspection is bounded (member count, member-name
length, aggregate declared uncompressed size — see
`meyar.ops.archive_safety`), and every sidecar this module reads is
bounded: the two archive members read by name (embedded manifest,
`uv.lock`) are bounded by their declared tar-member size, and the two
external sidecar files (release manifest, `SHA256SUMS`) are bounded by
the read call itself — `meyar.ops.verify_release._read_bounded_text`
opens the file and calls `_read_bounded_from_stream`, which never
requests more than `limit + 1` bytes from the open binary handle
(`fh.read(limit + 1)`), rejecting the file as `MANIFEST_TOO_LARGE`/
`SHA256SUMS_TOO_LARGE` the instant the observed byte count exceeds the
configured limit, before any UTF-8 decoding is attempted. This bound is
enforced by the read call itself, not by a `stat()` taken beforehand: a
`stat()` size is a TOCTOU-vulnerable pre-check (the file can grow between
the `stat()` and the read) and is never used as the bound for these two
sidecars; an I/O failure while reading is reported as
`MANIFEST_UNREADABLE`/`SHA256SUMS_UNREADABLE`. Archive-member inspection
stops the instant its own declared-size bound is exceeded, reported as its
own `ARCHIVE_RESOURCE_BOUND_EXCEEDED`/`*_TOO_LARGE` finding. Release-artifact
*building* is deferred to a later #35 PR — this PR's tests use synthetic
fixtures built on the fly, never real repository binaries.

## PR2 commands

macOS LaunchDaemon **foundation** for the MEYAR application process only —
render, verify, and read-only status. **Not** installation or lifecycle
orchestration: no command in this section writes to
`/Library/LaunchDaemons`, invokes `launchctl bootstrap`/`bootout`/
`kickstart`, requires/uses `sudo`, or creates a service account. The
target service model (not yet installed by any command here) is: system
LaunchDaemon -> dedicated non-root `UserName` -> MEYAR application
process -> loopback-bound Uvicorn. PostgreSQL and Ollama remain local
external dependencies, unmanaged by these commands. There is no accepted
immutable release/install filesystem layout yet, and these commands do
not invent one. See `docs/DECISIONS.md` D-067.

### `service-render`

```bash
uv run meyar-ops service-render \
  --label <launchd-label> \
  --user-name <dedicated-non-root-account> \
  --working-directory <absolute-path> \
  --executable <absolute-path-to-interpreter> \
  --port <1-65535> \
  --stdout-path <absolute-path> \
  --stderr-path <absolute-path> \
  --output <path-to-write>
```

Renders a deterministic LaunchDaemon plist (`plistlib`, `sort_keys=True`)
with exactly seven keys: `Label`, `UserName`, `WorkingDirectory`,
`ProgramArguments`, `StandardOutPath`, `StandardErrorPath`, `KeepAlive`.
Every typed input is required and operator-supplied — no bank-specific or
otherwise hardcoded default for any field.

- **Label:** rejected if empty, containing `/`, or containing any
  whitespace/control character.
- **UserName:** rejected if empty or `root`. `meyar-ops` only references
  this account in the plist; it never creates it.
- **WorkingDirectory / Executable / StandardOutPath / StandardErrorPath:**
  each must be an absolute path, free of NUL/control characters, free of
  a lexical `..` traversal component. Symlink path components are
  deliberately **not** blanket-rejected — no final immutable-release-path
  contract exists yet.
- **Executable:** never a bare `uv`/PATH-resolved name — an absolute
  interpreter path. `ProgramArguments` is always constructed as a real
  argv array: `[executable, "-m", "uvicorn", "meyar.main:app", "--host",
  "127.0.0.1", "--port", str(port)]`. Never a shell command string, never
  `shell=True`.
- **Port:** validated 1–65535.
- **EnvironmentVariables:** never emitted — secrets stay outside the
  plist in host-local configuration.
- **KeepAlive:** always the structured `{"SuccessfulExit": false}` —
  restart-after-abnormal-exit semantics. No explicit `RunAtLoad` (already
  implied) and no `ThrottleInterval` (would only restate launchd's own
  default) are emitted.

Writes only to the explicit `--output` path — never
`/Library/LaunchDaemons`, never `launchctl`, never `sudo`. The output
file is created with `O_CREAT | O_EXCL` (atomic, not a `Path.exists()`
pre-check, so no overwrite race): an existing path is refused as
`OUTPUT_PATH_EXISTS` and left untouched. `ServiceSpec` validation happens
before any filesystem write, so invalid input never creates a file; any
failure during the write removes the partial file it created.

### `service-verify`

```bash
uv run meyar-ops service-verify --plist <path> [--expected-label <label>]
```

Bounded read (1 MiB cap, enforced by the read call itself — never a
`stat()` pre-check) and `plistlib` parse of an operator-supplied plist,
then independent checks for every component of the security/shape
contract: plist validity; `Label` well-formedness (and, if
`--expected-label` is given, an exact match); `ProgramArguments` present
as a non-empty argv array of strings; the executable path absolute; the
full invocation matching the expected `meyar.main:app` uvicorn contract;
host exactly `127.0.0.1`; port in range; `UserName` present and not
`root`; `WorkingDirectory`/`StandardOutPath`/`StandardErrorPath`
absolute; no `EnvironmentVariables`; no shell wrapper (`Program` key or a
shell interpreter as the executable); `KeepAlive` exactly
`{SuccessfulExit: false}`; and the plist's top-level key set is exactly
the seven keys `service-render` emits — any other key (a tampered
`EnvironmentVariables`, a re-added `RunAtLoad`, a `Program` string, an
unrecognized key) is rejected as `UNSUPPORTED_KEY`, never silently
accepted. A malformed (non-plist) file and a well-formed-but-tampered
plist are both rejected with distinct, truthful finding codes — never
folded into one generic failure. No final install-root containment rule
is invented (no final immutable release layout exists yet), and symlinked
path components are not blanket-rejected, matching `service-render`.

### `service-status`

```bash
uv run meyar-ops service-status --label <launchd-label>
```

Read-only, macOS-only probe of whether the given label is currently
visible in the **system** launchd domain: fixed argv (`/bin/launchctl
print system/<validated-label>`, never `shell=True`, never a bare
`launchctl` relying on `PATH`), no `sudo`, no mutation
(`bootstrap`/`bootout`/`kickstart` are never called). The launchctl
runner is injected/testable, so this command's tests never require a
real `launchctl`/macOS host. On any non-Darwin platform (all current
CI), it returns a truthful `PLATFORM_UNSUPPORTED` finding — it does not
pretend the check ran, and it does not require `launchctl` to exist on
ordinary Linux CI. Raw `launchctl` stdout/stderr is never included in the
returned `OpsResult` — only the fixed argv used, the process exit status,
and (on a genuine runner-level infrastructure failure — missing binary,
timeout) bounded/redacted error text via the existing
`meyar.ops.redact.safe_exception_text`.

**Real `launchctl`/`bootstrap`/reboot behavior on macOS remains
UNCONFIRMED** — see "Platform verification status" below.

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

`ok` is defined uniformly across all `meyar-ops` commands: `true` iff no
`Finding` has `status: FAIL`. `WARN`/`SKIPPED` findings are reported but
never flip `ok`. Every component's finding is always present in the list
— nothing is ever dropped because an earlier component failed.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | `SUCCESS` — `ok: true` |
| 1 | `CHECK_FAILURE` — the command ran to completion but `ok: false` (a preflight/readiness/verify-release/service-render/service-verify/service-status check failed) |
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
- `service-status` never includes raw `launchctl` stdout/stderr in its
  `OpsResult` — only the fixed argv, exit status, and bounded/redacted
  error text on a genuine runner failure; never `shell=True`; never
  `sudo`.

## What is NOT implemented yet

- No HTTP `/ready` route (`/api/v1/health` remains liveness-only,
  unchanged) — issue #46.
- No launchd/service **lifecycle**: no `service-install`, `service-start`,
  `service-stop`, `service-restart`. No plist is ever written to
  `/Library/LaunchDaemons`, no `launchctl bootstrap`/`bootout`/
  `kickstart` is ever called, no `sudo`/privilege escalation, no service
  account is created. `service-render`/`service-verify`/`service-status`
  are foundation-only (D-067).
- No final immutable release/install filesystem layout — path fields are
  operator-supplied and validated lexically only.
- No update/rollback orchestration (the `RollbackCompatibility` enum is a
  typed classification for a future human-operated runbook, not an
  executable mechanism — no automatic Alembic downgrade).
- No release-artifact *building* (only verification of an already-built
  artifact against fixtures).
- No production model approval — `meyar.ops.model_manifest` supports
  `PRODUCTION_APPROVED` as a schema value but nothing in this PR
  instantiates it. The schema itself requires a non-empty
  `benchmark_reference` for any entry claiming `BENCHMARKED_PENDING_APPROVAL`
  or `PRODUCTION_APPROVED` — a review claim always has to point somewhere
  — but does not invent any stronger cross-field requirement beyond that;
  further governance rules are for issue #36 to define once real
  benchmark evidence exists.
- No PostgreSQL/Homebrew/Docker production provisioning topology choice.
- No comprehensive production config fail-closed hardening or SQL/
  exception logging hardening beyond what already exists — that is
  issue #46.

## #35 / #46 boundary

**#35 PR1:** local operator `preflight`, local operator `status`, local
operator component `readiness`, the release/model manifest typed
contracts, and release-artifact *verification*.

**#35 PR2 (this PR):** macOS LaunchDaemon **foundation** only —
`service-render` (typed plist generation), `service-verify` (bounded
plist shape/security verification), `service-status` (read-only
`launchctl print system/<label>` probe). No plist installation, no
`launchctl` mutation, no service-account creation, no PostgreSQL/Ollama
lifecycle. No final immutable release/install path is chosen.

**Deferred to #46:** authenticated HTTP `/ready`, in-application
degraded-state semantics, comprehensive production config fail-closed
hardening, SQL/exception logging hardening, application recovery/
concurrency semantics.

**Deferred to #36:** real Target-Mac model selection & benchmark on the
confirmed Mac mini M4 Pro reference hardware. `meyar-ops` does not choose,
benchmark, or approve a production model.

## Platform verification status

Everything in this document — `preflight`, `status`, `readiness`,
`verify-release`, `service-render`, `service-verify`, `service-status`,
and the full test suite — has been run and verified **on Linux only**,
against the local Ollama daemon and the local PostgreSQL/`pgvector`
container described in `docker-compose.yml`. `service-status`'s tests use
an injected fake `launchctl` runner; no test claims real `launchctl`
behavior.

**Apple Silicon / Mac mini M4 Pro reference-hardware rehearsal remains
UNCONFIRMED.** No claim of Apple-Silicon runtime acceptance, real
`launchctl print`/`bootstrap` behavior, service-survives-reboot behavior,
or bank-Mac verification is made by this document. That verification is
issue #35's later (installation/lifecycle) scope plus issue #36, and
depends on this tooling first being reviewed and merged.

**Production model remains TBD** — `qwen3:0.6b` is the source/default
development/integration setting, `qwen3:1.7b` is a recent local
acceptance/browser-runtime override; neither is benchmarked or
production-approved. See `docs/TARGET_MAC_BENCHMARK.md` and issue #36.
