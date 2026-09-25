# MEYAR Ops — `meyar-ops`

Operator tooling for agentless deployment readiness (issue #35 — Slice 6,
M9). This document covers PR1 (`preflight`/`status`/`readiness`/
`verify-release` — `docs/DECISIONS.md` D-066), PR2 (`service-render`/
`service-verify`/`service-status` — D-067), PR3 (`build-release` —
D-068), PR4 (offline bundle/activation — D-073), and PR5 (host config
and service binding — D-074). See §11 below for the #35/#46 boundary.

**PR3 scope reminder — read before assuming this means "deployable":**
`build-release` produces an immutable MEYAR APPLICATION release
artifact (source, migrations, `pyproject.toml`/`uv.lock`, and release
identity/integrity metadata) built from an exact Git commit. It is
**not** a complete offline deployment bundle — it contains no Python
runtime, no `uv` executable, no third-party wheels/offline wheelhouse, no
Ollama, no models, and no PostgreSQL, and it defines no host install
layout. See "PR3 commands" below and §14/§15.

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
external dependencies, unmanaged by these commands. PR5 binds them to
PR4's accepted immutable release and shared host layout (D-073/D-074).

### `config-verify` (PR5)

```bash
uv run meyar-ops config-verify --install-root <root>
```

Read-only verification of `<root>/shared/config/.env`: safe directory
chain; regular, non-symlink file; 64 KiB bounded read; well-formed
dotenv; no duplicate critical keys or interpolation; production values
validated through application `Settings`, without ambient shell values.
Fixed finding codes never include secrets. The config is operator-owned
and outside the immutable release. Production requires explicit safe
database credentials and pending-login secret, Secure cookies, local
Ollama providers and loopback endpoint, and exact absolute
`<root>/shared/storage`. The production model remains TBD until #36.

### `service-render`

```bash
uv run meyar-ops service-render \
  --label <launchd-label> \
  --user-name <dedicated-non-root-account> \
  --install-root <root> \
  --port <1-65535> \
  --output <path-to-write>
```

Renders a deterministic LaunchDaemon plist (`plistlib`, `sort_keys=True`)
with exactly seven keys: `Label`, `UserName`, `WorkingDirectory`,
`ProgramArguments`, `StandardOutPath`, `StandardErrorPath`, `KeepAlive`.
The root is operator-supplied; runtime, config and log paths are derived
from its verified PR4 layout.

- **Label:** rejected if empty, containing `/`, or containing any
  whitespace/control character.
- **UserName:** rejected if empty or `root`. `meyar-ops` only references
  this account in the plist; it never creates it.
- **Install root:** absolute and normalized. PR4's activation chain,
  installed tree, release-local Python and application source must verify.
- **Executable:** exactly `<root>/current/.venv/bin/python`, never a
  bare `uv`/PATH-resolved name or version-stale release path.
  `ProgramArguments` is always a real argv array: `[executable, "-m",
  "uvicorn", "meyar.main:app", "--host",
  "127.0.0.1", "--port", str(port)]`. Never a shell command string, never
  `shell=True`.
- **WorkingDirectory:** exactly `<root>/shared/config`, where `Settings`
  loads `.env`; no `.env` enters the immutable release.
- **Logs:** exactly `<root>/shared/logs/meyar.stdout.log` and
  `meyar.stderr.log`.
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
uv run meyar-ops service-verify --plist <path> --install-root <root> [--expected-label <label>]
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
folded into one generic failure. It also verifies the active PR4 release,
host production config and exact canonical executable, working directory
and log paths. Stale/external interpreters and release-internal config
working directories fail.

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

## PR3 commands

`build-release` — builds an immutable, verifiable MEYAR **application**
release artifact from an exact Git commit. See D-068.

### `build-release`

```bash
uv run meyar-ops build-release \
  --source-sha <40-lowercase-hex-git-commit-sha> \
  --output-dir <existing-directory> \
  --rollback-compatibility <RollbackCompatibility value> \
  --model-manifest-reference <non-empty reference string> \
  --model-approval-status <ModelApprovalStatus value>
```

**Source provenance — the whole point of this command.** Every byte in
the produced artifact, and every source-derived `ReleaseManifest` field
(`release_version`, `required_python_version`, `uv_lock_sha256`,
`alembic_heads`), comes from the exact selected Git commit object via
fixed-argv Git plumbing (`git rev-parse --show-toplevel`, `git cat-file -e
<sha>^{commit}`, `git ls-tree -r -z --full-tree`, `git cat-file -p
<blob-sha>` — never `shell=True`, never a shell command string, never
path-interpolated into a shell). A dirty, modified, or untracked file in
the mutable working tree can never change the bytes produced for a given
`--source-sha`: every file's content is read from its Git blob object,
never from disk. `--source-sha` must be the full 40-lowercase-hex commit
id; a short SHA, a non-hex string, a SHA that doesn't exist locally, or a
well-formed SHA that names a blob/tree instead of a commit are all
rejected as truthful `FAIL` findings — never silently defaulted to
`HEAD`, and never treated as "close enough."

**Allowlist, not "tar everything and exclude bad things."** Exactly five
pathspecs are ever passed to Git (`meyar.ops.build_release
.ALLOWED_PATHSPECS`):

```
backend/src/meyar
backend/pyproject.toml
backend/uv.lock
backend/alembic.ini
backend/alembic
```

`backend/tests/`, `backend/scripts/`, `.env`, `.git/`, real CVs/customer
data, caches, and everything else are never considered, even implicitly
— they are never in the pathspec list Git is asked about, regardless of
what else exists at the selected commit. Every listed tree entry is also
independently inspected: a Git symlink entry (`mode 120000`), a submodule
entry (object type `commit`), or any entry that is not an ordinary
regular file (`mode 100644`/`100755`, object type `blob`) aborts the
whole build before a single byte is written — reported as
`UNSAFE_GIT_TREE_ENTRY`. Nothing is ever written to `--output-dir` when
provenance/allowlist validation fails.

**Alembic heads come from the selected commit's own migration tree,
parsed — never executed.** (PR #56 security-corrective pass.)
`backend/alembic.ini`'s presence in the selected commit is still
required, but only as a presence check. Heads themselves are computed by
`meyar.ops.alembic_static_metadata.compute_static_alembic_heads`, which
parses each selected-commit `backend/alembic/versions/*.py` blob with
Python's `ast` module and extracts only the static `revision`/
`down_revision` literal assignments (via `ast.literal_eval`, which
accepts only literal shapes and rejects a function call, an attribute
lookup, a name reference, an f-string, or any other computed
expression) — **never** Alembic's own `ScriptDirectory`, which loads
each migration file as a Python module and would execute its top-level
code as a side effect of building its revision map. A selected commit's
migration file cannot run any code during a `build-release` invocation,
by construction — proven by
`test_malicious_migration_top_level_side_effect_is_never_executed`.
`meyar.ops.alembic_introspect.get_code_alembic_heads` (real
`ScriptDirectory`) is unchanged and remains the correct mechanism for
`status`/`readiness`/`preflight`, which only ever point it at the
trusted, already-running working tree — not an arbitrary selected
commit.

The static graph is also proven acyclic (PR #56 final resource-safety
corrective pass): `compute_static_alembic_heads` runs a deterministic
white/gray/black DFS over the parsed `down_revision` edges
(`_detect_down_revision_cycle`) before computing heads. Without this, a
cyclic pair (`A.down_revision = B`, `B.down_revision = A`) is invisible
to a plain "revisions not referenced as a parent" computation — both ends
of the cycle are mutually "referenced" and simply vanish from the result,
while an entirely independent, valid branch's head is still reported as
if nothing were wrong. Real Alembic rejects a cyclic revision graph
outright; this reproduces that rejection (self-cycle, two-node cycle, or
longer, even alongside an independent valid branch) purely over
already-parsed static metadata — no migration code is executed to detect
it, exactly like head computation itself.

**Archive layout** — a single top-level root, `<release_id>/`:

```
<release_id>/
  release_manifest.json
  backend/
    pyproject.toml
    uv.lock
    alembic.ini
    alembic/...
    src/meyar/...
```

The embedded `<release_id>/release_manifest.json` is mandatory for a
`build-release`-produced artifact (unlike `verify-release`, which still
treats a missing embedded manifest as `SKIPPED` for compatibility with
other/legacy artifacts).

**Deterministic content selection, not byte-for-byte reproducibility.**
The exact same `--source-sha` always yields the exact same allowlisted
file set and bytes, and archive-internal metadata is normalized
regardless of build time: members are added in a fixed lexical order,
every member gets fixed `uid=0`/`gid=0`/empty `uname`/`gname`/mode
`0o644`/`mtime=0`, and the gzip wrapper itself uses a fixed header
`mtime` and no embedded filename. `built_at` is, deliberately, a real
build timestamp — **two builds of the same commit at different real
times will still differ** in `built_at` and therefore in the embedded
and external manifest bytes and both checksums. This module never claims
byte-identical rebuilds; it only claims deterministic *content
selection*.

**Output bundle — three files in `--output-dir`, never overwritten:**

```
<release_id>.tar.gz
<release_id>.release-manifest.json
SHA256SUMS
```

`SHA256SUMS` binds both the artifact and the external manifest, in the
exact format `verify-release`/`parse_sha256sums` already expects. An
early `Path.exists()` check against all three targets gives a fast,
clear `OUTPUT_TARGET_EXISTS` failure before any build work happens, but
that check is never trusted as the race/security boundary: every actual
file creation still goes through `O_CREAT | O_EXCL` relative to a dir-fd
anchored to `--output-dir`. If a race introduces one of the three
targets after the early check passes, `O_EXCL` still refuses the
create — so no overwrite race exists either way. A pre-existing file at
that path is never touched. If a later step fails
after this invocation has already created one or more of the three
files, only the file(s) *this invocation itself created* are removed
(identity-checked via `(st_dev, st_ino)`, mirroring
`service_plist._unlink_if_same_file`) — never a pre-existing file, and
never a file a concurrent actor has since swapped in at the same path.
`--output-dir` must already exist and be a directory; this command never
creates a parent directory tree.

**Release-derived output names are validated as path-safe (PR #56
security-corrective pass).** `release_version` is read from the selected
commit's `pyproject.toml` — attacker-controlled content for any commit
an operator selects, exactly like every other selected-commit blob.
Before either `release_version` or the `release_id` derived from it can
reach an output filename or archive member root, both are validated as
safe single filesystem path components (non-empty; not `.`/`..`; no `/`
or `\`; no ASCII control character) by a build-release-specific
validator. An unsafe value fails the build truthfully
(`RELEASE_VERSION_UNSAFE`/`RELEASE_ID_UNSAFE`) before any output file or
archive member is created — it is never silently normalized into a
different, "safe-looking" value. `compute_release_id()`'s own contract
and `ReleaseManifest`'s existing length constraints are unchanged.

**Output write ownership is failure-safe (PR #56 security-corrective
pass).** `_create_exclusive` explicitly owns the raw file descriptor
returned by `os.open()` until `os.fdopen()` succeeds; if `os.fdopen()`
itself fails — a case the original implementation did not explicitly
handle — the still-owned raw descriptor is closed directly (a redundant-
close `OSError` is suppressed) before the existing identity-checked path
cleanup runs, and the original exception always propagates unmasked.

One narrower case is deliberately fail-*closed* rather than
self-cleaning (PR #56 final resource-safety corrective pass): if
`os.fstat(fd)` itself fails immediately after the `O_CREAT | O_EXCL`
create succeeds, there is no `(st_dev, st_ino)` identity to check a later
cleanup against. Rather than guess that the on-disk path is still the
file this invocation just created — which could delete a concurrent
actor's replacement at the same basename — the raw fd is still always
closed, but the created file is left in place for operator inspection,
reported as `OUTPUT_IDENTITY_UNAVAILABLE`, and no manifest/`SHA256SUMS`
file is ever written afterward. Every other failure point already holds
a real `(st_dev, st_ino)` and continues to clean up exactly as before.

**Resource bounds are enforced before construction, not discovered after
(PR #56 final resource-safety corrective pass).** The same three bounds
`verify-release` enforces (`archive_safety.MAX_ARCHIVE_MEMBER_COUNT`,
`MAX_MEMBER_NAME_LENGTH`, `MAX_AGGREGATE_UNCOMPRESSED_SIZE`) are checked
against values already deterministically knowable *before* the artifact
is built, so an invalid artifact is never written merely so a post-build
check can discover a bound violation:

- **member count** — checked immediately after the allowlisted tree
  listing (`member_count_bound`/`MEMBER_COUNT_EXCEEDS_BOUND`), as the
  count of selected entries plus the one mandatory embedded manifest,
  before a single blob's content is fetched;
- **member name length** — checked once `release_id` is known
  (`member_name_length_bound`/`MEMBER_NAME_LENGTH_EXCEEDS_BOUND`),
  against the *final* archive member name (`<release_id>/<path>`), not
  the raw Git path alone;
- **aggregate uncompressed size** — checked once the release manifest is
  built (`aggregate_size_bound`/`AGGREGATE_SIZE_EXCEEDS_BOUND`), as the
  selected source blobs' Git-declared size (reused from the fetch step)
  plus the embedded manifest's own bytes — not source blobs alone.

**Self-check before publishing.** Immediately after writing the
artifact, `build-release` still runs the exact same
`meyar.ops.archive_safety.inspect_archive_members` bounded safety
inspection `verify-release` performs, and the full artifact is then
required to pass `verify-release` unmodified — this is the strongest
acceptance test for this command (see `test_build_release_output_passes_
verify_release`). This remains defense-in-depth for any bound the
prechecks above did not already catch (and for unsafe member *shapes*,
which the prechecks do not evaluate at all). A self-check failure removes
the artifact file already written and fails the build before the
manifest/`SHA256SUMS` files are ever created.

**Model/rollback governance stays declarative.** `--rollback-
compatibility` and `--model-approval-status` are required CLI inputs —
the builder records whichever enum value the operator supplies and
performs no rollback/backup/restore/Alembic-downgrade action and no
model benchmarking/approval inference. This PR ships no
`PRODUCTION_APPROVED` model manifest and does not change issue #36
governance.

**Not implemented by this command:** no `--release-version` override
(the release-version authority is always the selected commit's
`backend/pyproject.toml` `[project].version`); no arbitrary
archive-path/include argument; no secrets accepted; no Python runtime,
`uv` executable, wheel/wheelhouse, Ollama, model, or PostgreSQL payload;
no host install layout, extraction, or activation.

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
- `verify-release`/`build-release` never extract an archive to disk —
  `TarFile.extractall`/`.extract` are never called anywhere in either
  module; a monkeypatch-based test asserts this at runtime for
  `verify-release`, and a static grep-based test asserts it in source for
  both.
- `service-status` never includes raw `launchctl` stdout/stderr in its
  `OpsResult` — only the fixed argv, exit status, and bounded/redacted
  error text on a genuine runner failure; never `shell=True`; never
  `sudo`.
- `build-release` invokes Git only via fixed-argv `subprocess.run`
  (`meyar.ops.build_release.default_git_runner`) — never `shell=True`,
  never a shell command string, never untrusted path interpolation into a
  shell; a static AST-based test asserts no `subprocess` call in the
  module passes `shell=True`. It never accepts a secret, never performs
  an HTTP request, and never calls an external/cloud LLM.

## What is NOT implemented yet

- No HTTP `/ready` route (`/api/v1/health` remains liveness-only,
  unchanged) — issue #46.
- No launchd/service **lifecycle**: no `service-install`, `service-start`,
  `service-stop`, `service-restart`. No plist is ever written to
  `/Library/LaunchDaemons`, no `launchctl bootstrap`/`bootout`/
  `kickstart` is ever called, no `sudo`/privilege escalation, no service
  account is created. `service-render`/`service-verify`/`service-status`
  are foundation-only (D-067).
- PR4's immutable release/install layout exists; PR5 binds the service
  plist to it. Privileged installation remains pending.
- No update/rollback orchestration (the `RollbackCompatibility` enum is a
  typed classification for a future human-operated runbook, not an
  executable mechanism — no automatic Alembic downgrade).
- No offline Python runtime, `uv` executable, third-party wheel/
  wheelhouse, Ollama, model, or PostgreSQL payload in the `build-release`
  artifact — application/source release-artifact *building* now exists
  (D-068), but it is not yet a complete offline deployment bundle.
- `build-release` alone only builds an artifact; PR4 separately supplies
  offline host installation and activation.
- No production model approval — `meyar.ops.model_manifest` supports
  `PRODUCTION_APPROVED` as a schema value but nothing in this PR
  instantiates it. The schema itself requires a non-empty
  `benchmark_reference` for any entry claiming `BENCHMARKED_PENDING_APPROVAL`
  or `PRODUCTION_APPROVED` — a review claim always has to point somewhere
  — but does not invent any stronger cross-field requirement beyond that;
  further governance rules are for issue #36 to define once real
  benchmark evidence exists.
- No PostgreSQL/Homebrew/Docker production provisioning topology choice.
- PR5 covers only the deployment-blocking production config boundary;
  remaining #46 work, including SQL/exception logging, stays open.

## #35 / #46 boundary

**#35 PR1:** local operator `preflight`, local operator `status`, local
operator component `readiness`, the release/model manifest typed
contracts, and release-artifact *verification*.

**#35 PR2:** macOS LaunchDaemon **foundation** only — `service-render`
(typed plist generation), `service-verify` (bounded plist shape/security
verification), `service-status` (read-only `launchctl print
system/<label>` probe). No plist installation, no `launchctl` mutation,
no service-account creation, no PostgreSQL/Ollama lifecycle. Its original
operator path inputs were superseded by PR5 after PR4 fixed the layout.

**#35 PR3:** `build-release` — builds an immutable MEYAR
**application** release artifact (source, migrations, `pyproject.toml`/
`uv.lock`, release identity/integrity metadata) from an exact Git commit,
allowlist-driven, with an embedded + external `ReleaseManifest` and a
`SHA256SUMS`-bound output bundle that passes `verify-release` unmodified.
Not yet a complete offline deployment bundle (see the "PR3 scope
reminder" and "PR3 commands" sections above), no host install layout, no
install/update/rollback/service-lifecycle orchestration.

**#35 PR4 (D-073):** `bundle-build` and the bundled, stdlib-only
`meyar-ops.py` host commands (`install-release`, `verify-install`,
`activate-release`). This establishes an exact offline wheel payload and
an immutable install/atomic activation layout. No LaunchDaemon mutation,
database migration, model installation, or full rollback is performed.

**#35 PR5 (D-074):** `config-verify` and canonical service render/verify
binding. Direct production `Settings` rejects blank/default secrets,
development DB credentials, insecure cookies, invalid provider selection
and non-loopback Ollama. The service consumes active-release code and
shared configuration, storage and logs. No LaunchDaemon mutation or
target-Mac acceptance is performed. #35 and #46 remain open.

**Deferred to #46:** authenticated HTTP `/ready`, in-application
  degraded-state semantics, remaining production config policy,
  SQL/exception logging hardening, application recovery/
concurrency semantics.

**Deferred to #36:** real Target-Mac model selection & benchmark on the
confirmed Mac mini M4 Pro reference hardware. `meyar-ops` does not choose,
benchmark, or approve a production model.

## Platform verification status

Everything in this document — `preflight`, `status`, `readiness`,
`verify-release`, `service-render`, `service-verify`, `service-status`,
`build-release`, and the full test suite — has been run and verified **on
Linux only**, against the local Ollama daemon and the local PostgreSQL/
`pgvector`
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

## PR4 — offline dependency bundle and host install foundation

PR4 is a **contract implementation verified on Linux**. It does not claim
that downloaded macOS wheels execute on Apple Silicon or that the bank
host has been rehearsed. The reference target is macOS arm64, CPython
3.12, with a minimum wheel platform tag of `macosx_11_0_arm64`. The
target's exact 3.12 patch version and SHA-256 of its resolved interpreter
executable must be supplied by the bank operator. A different version,
OS, architecture, or executable hash fails before host mutation.

**Runtime model.** The bank provisions and approves CPython 3.12 with
working `venv` and `pip`. The bundle contains no Python runtime and
neither builder nor host installer requires `uv`. There is no Homebrew
or target-host internet fallback. The host creates a release-local venv
with `--copies`; its `pip` installs only the bundle's selected, hashed
wheels using `--isolated --no-index --no-deps --require-hashes` and
`--only-binary=:all:`. Failure to create the venv or use pip fails the
install. The development-side `bundle-build` reads the selected
application archive's `uv.lock` directly; it downloads exact wheel URLs
and SHA-256 values from that lock, checks wheel tags for CPython 3.12
macOS arm64, and never executes or installs a foreign wheel on Linux.
`greenlet` is an explicit direct dependency because SQLAlchemy's
conditional dependency marker does not select macOS `arm64`; Pillow is
also a direct, required runtime dependency. PostgreSQL's application
driver is `asyncpg`; `pgvector` is the Python integration package. This
runtime lock has no `psycopg` dependency.

**Build command** (on a trusted development/build machine, against a
previously created and verified `build-release` output):

```bash
cd backend
uv run --locked meyar-ops bundle-build \
  --artifact <release-id>.tar.gz \
  --manifest <release-id>.release-manifest.json \
  --sha256sums SHA256SUMS \
  --output-dir <existing-output-directory> \
  --runtime-version <approved-exact-3.12.x-version> \
  --runtime-executable-sha256 <approved-64-hex-sha256>
```

The builder consumes an exact application artifact, copies the three
inputs into a temporary directory, runs the existing `verify-release`
checks on those stable copies, enforces the application path allowlist,
selects the lock's target-compatible binary wheels, downloads each
locked URL once, checks its hash and ZIP member safety, and atomically
publishes `<release-id>.deployment/` without overwriting an existing
directory. The directory contains the application artifact, its two
sidecars, `requirements.txt`, `wheels/`, `meyar-ops.py` copied from that
exact application archive, and `deployment_manifest.json`. The manifest
binds format version, release id/full source SHA, artifact and sidecar
hashes, `uv.lock` SHA, runtime version/executable SHA, target OS/arch/tag,
each wheel name/version/file/SHA, aggregate dependency identity, Alembic
heads, model reference/status, rollback classification, and installer
hash. This is an integrity contract, **not** publisher signing; the
handoff channel and accepted source commit remain operator trust inputs.
No candidate/CV/storage file, tests, `.env`, credentials, model artifact,
or database dump is read or included.

**Host commands** (run the bundled script using the approved Python
interpreter; `<root>` must already exist, be owned by the invoking
operator, and reject group/other writes; normally `/opt/meyar`):

```bash
<approved-python3.12> <bundle>/meyar-ops.py install-release \
  --bundle-dir <bundle> --install-root <root>
<approved-python3.12> <bundle>/meyar-ops.py verify-install \
  --install-root <root> --release-id <release-id>
<approved-python3.12> <bundle>/meyar-ops.py activate-release \
  --install-root <root> --release-id <release-id>
```

These use the existing `OpsResult` JSON shape and exit 0 on success,
1 on a failed check, 2 for invalid invocation, and 3 for an unexpected
infrastructure failure. The script never prints
input paths, subprocess output, candidate content, or secrets. All
subprocesses use fixed argv; no shell, Git, `uv`, external AI, or target
network call is made. Any filesystem mutation is under a non-blocking
`flock` on `<root>/.meyar-ops.lock`; competing operations fail
`OPERATION_BUSY`. The OS releases a stale lock when its process dies.
The lock file contains no owner identity or secret.

**Canonical layout:**

```
<root>/
  releases/<release-id>/    # immutable source, migrations, release-local venv
  activations/g-<id>/       # exact current and previous release identities
  current                   # atomic symlink to activations/g-<id>/current
  shared/config/            # bank-owned configuration/secrets, outside release
  shared/storage/           # mutable candidate/CV data, outside release
  shared/backups/
  shared/logs/
```

Extraction is streamed into a disposable staging directory, with member
count, declared-size, path, type, duplicate, and allowlist checks; no
`extractall` call is used. A complete install is renamed into its
versioned release path only after the lock, manifest, wheel, dependency,
venv, and tree checks pass. A failed or killed pre-rename install cannot
change `current`. Reinstalling identical bytes verifies the installed
tree and succeeds idempotently; different or tampered bytes under the
same release id fail. The release-local source path is checked during
`verify-install`. No mutable data enters release directories.

Activation verifies the installed tree first, then creates a generation
record containing the exact new release id, previous release id, and
rollback classification. The sole active-release pointer is
`<root>/current`; `activations/current` is not created. An atomic
replacement of `<root>/current` publishes the complete generation.
Failure or process death before replacement leaves the previous public
state unchanged, including no `current` entry on first activation. A
process killed after replacement leaves a complete new generation.
`PROHIBITED_PENDING_PROCEDURE` and `BACKUP_RESTORE_REQUIRED` block
activation over an existing release until a later workflow supplies the
required procedure or verified backup/restore gate. `APP_ONLY` and
`FORWARD_COMPATIBLE_SCHEMA` are recorded with the previous identity.
There is no Alembic downgrade, service restart, or automatic
rollback in PR4. PR5 binds the service executable to
`<root>/current/.venv/bin/python` and loads config from
`<root>/shared/config`, with storage and logs under `shared/`.
Immutable code/runtime != mutable host configuration != mutable
candidate/document storage. Real Mac execution, native-extension import checks, and
service/reboot rehearsal remain **UNCONFIRMED**.
