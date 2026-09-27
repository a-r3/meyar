# MEYAR Ops — `meyar-ops`

Operator tooling for agentless deployment readiness (issue #35 — Slice 6,
M9). This document covers PR1 (`preflight`/`status`/`readiness`/
`verify-release` — `docs/DECISIONS.md` D-066), PR2 (`service-render`/
`service-verify`/`service-status` — D-067), PR3 (`build-release` —
D-068), PR4 (offline bundle/activation — D-073), PR5 (host config
and service binding — D-074), PR6 (privileged LaunchDaemon lifecycle
foundation — D-075), PR7 (fresh database schema initialization —
D-076), PR8 (installed deployment readiness — D-077), PR9
(production backup creation/verification — D-078), and PR10
(isolated restore — D-079), PR11 (offline local AI provisioning —
D-080), PR12 (staged update and rollback — D-081), and PR13
(diagnostics and lifecycle evidence — D-082). See §11 below
for the #35/#46 boundary.

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
never interprets candidate/CV/JD/query content; backup and restore stream
opaque candidate bytes under private storage controls, and nothing in it
feeds matching, scoring, or search. It reuses the same trusted boundaries the
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

The PR4 install-root owner is the trusted operator identity. That same UID
must own `<root>`, `shared`, `shared/config`, and `.env`. These three
directories and `.env` must have the same GID, reserved for the dedicated
MEYAR service account; the account must be a member of that group. The
directories must be group-traversable and neither group- nor other-writable;
`shared/config` grants no access to other users. `.env` must be
owner-readable and group-readable, with no owner execute, group
write/execute, or other access (for example, mode `0640`). This gives the
non-root service a read path without exposing plaintext secrets to unrelated
local users. Ancestors of `<root>` must be owned by root or the install
operator and not group/other-writable, except a root-owned sticky temporary
directory. `config-verify` checks filesystem UID/GID/mode relationships;
it cannot establish that the bank has restricted group membership or that
the selected `UserName` belongs to that group. PR6 must provision and
verify those principal assignments before installing or starting a daemon.
PR4's root-owned-by-installer and non-group-writable directory rules remain
in force; do not make `shared/config` group-writable to provision `.env`.

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
  installed tree, executable release-local Python and application source
  must verify.
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

Rendering verifies the active interpreter's execute bits for owner, group,
and other (PR4 installs it as `0555`); it does not run the service.
Render/verify does not claim that PR6's separate principal and runtime
permission checks have passed.

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
ordinary Linux CI. Exit `0` means `SERVICE_VISIBLE`; exit `113` means
`SERVICE_NOT_VISIBLE`; any other exit means `SERVICE_PROBE_FAILED`.
Timeout and unavailable-binary findings remain distinct in `service-status`
and are normalized to `SERVICE_PROBE_FAILED` by `deployment-ready`.
Raw `launchctl` stdout/stderr and runner exception text never enter either
result.

**Real `launchctl`/`bootstrap`/reboot behavior on macOS remains
UNCONFIRMED** — see "Platform verification status" below.

### PR6 privileged lifecycle foundation (merged as PR #69)

The four commands have the same required arguments:

```bash
<root>/current/.venv/bin/python -m meyar.ops.cli service-install \
  --label <validated-label> --user-name <provisioned-service-user> \
  --install-root <root> --install-owner-uid <operator-uid> --port <1-65535>
```

Replace `service-install` with `service-start`, `service-stop`, or
`service-restart` for the other operations. The bank invokes this command
in an approved external root context; the tool requires Darwin and
effective UID 0, never invokes `sudo`, prompts, or creates an account.
The explicit `--install-owner-uid` is the trusted install operator's
numeric UID. It must be non-root and match the install root; the protected
`shared/config/.env` chain remains bound to it. Ordinary `config-verify`
still binds that chain to its own effective UID. The configured service
user must exist, have a nonzero UID distinct from the install operator,
and belong to the existing dedicated service GID of `shared/config`.
The command resolves the account/group through `pwd`, `grp`, and
`os.getgrouplist`; bank provisioning is a prerequisite.

Every mutation joins the existing PR4 `<root>/.meyar-ops.lock` with a
nonblocking exclusive `flock`; root opens the preexisting operator-owned
regular lock without creation, mode/owner change, or deletion. Contention
returns `OPERATION_BUSY`. Installation verifies the active PR4 release,
production Settings, executable, account, and group, then prepares this
runtime contract: operator-owned root/releases/config/control paths are
service-group traversable and never group-writable; `activations` and
`shared` are setgid for future group inheritance; `shared/storage` and
`shared/logs` are operator-owned, service-group-owned `2770` directories;
the two canonical log files are operator-owned, service-group-owned
regular `0660` files. `.env` remains unchanged at the accepted `0640`
contract, and immutable release content remains read-only. Existing
unsafe symlinks, ownership, and broad write bits fail closed. PR4's
host-layout verifier permits group write only for those two mutable
directories with the expected config service GID, setgid, and no other
write; future PR4 install/verify/activation remains compatible.

Installation publishes the exact PR5 seven-key XML bytes at the fixed
`/Library/LaunchDaemons/<label>.plist`. It writes/fsyncs a same-directory
temporary regular file as root:wheel `0644`, then hard-links it to the
final name without replacement and removes the temporary name. Existing
safe exact bytes return `SERVICE_PLIST_ALREADY_INSTALLED`; any different,
symlinked, nonregular, or unsafe existing destination returns a conflict.
`service-install` never starts the job.

After rechecking plist, host binding, principal, and runtime permissions,
the lifecycle uses only fixed argv with `/bin/launchctl` and the system
domain. `service-start` probes `print system/<label>`; absent means
`bootstrap system /Library/LaunchDaemons/<label>.plist`, then
`kickstart system/<label>`; loaded means `kickstart system/<label>` only.
`service-stop` uses `bootout system/<label>` when loaded and verifies
absence; already absent succeeds without mutation and leaves the plist,
config, logs, storage, and release untouched. `service-restart` uses
`kickstart -k system/<label>` when loaded; absent uses bootstrap plus
ordinary kickstart. Probe exit 113 is treated as absent; other probe
failures fail closed. All subprocesses have a 10-second timeout, discard
stdout/stderr, and use a fixed minimal environment. The result contains
only fixed codes and no raw launchctl output or candidate content.

**LaunchDaemon installed/loaded != MEYAR application ready != DB/schema
ready != Ollama/model ready.** These commands assert launchd visibility
only. Real Apple-Silicon launchd and reboot behavior remain unconfirmed.

The application process validates host config at FastAPI startup as the
dedicated service user. Its runtime verifier derives the install owner
from the canonical, protected `shared/config` tree only after rejecting a
service-owned root, checking the PR5 owner/GID/mode and ancestor chain,
service group membership, and traversal/read access. The operator-facing
`config-verify` still requires the caller to own the root; privileged
lifecycle still requires an explicit `--install-owner-uid`. Startup also
rejects ambient `MEYAR_*` settings that change the protected file values.

### PR7 `schema-init` — first-deployment database only

Run as the trusted non-root install operator, after installing and activating
the release and verifying the host production config, before installing or
starting the service:

```bash
<root>/current/.venv/bin/python -m meyar.ops.cli schema-init --install-root <root>
```

`<root>` must be the canonical absolute operator-owned install root. The
command acquires PR4's existing `<root>/.meyar-ops.lock` with nonblocking
exclusive `flock`; contention reports `OPERATION_BUSY`. It does not delete
the lock or change its inode, owner, or mode. It verifies the complete PR4
active release, the actual imported MEYAR package and Python executable,
the active `backend/alembic.ini` and `backend/alembic/` script directory,
and the protected PR5 `shared/config/.env`. The DB URL comes only from the
validated file, never from CLI arguments, ambient shell Settings, or the
development checkout. The sole target is the active code's single Alembic
head, which must equal the installed manifest's sole declared head.

The command reads only Alembic revision metadata and PostgreSQL table names:

| Database state | Result |
| --- | --- |
| Exactly the active head | `SCHEMA_ALREADY_CURRENT`; no mutation |
| No revision and no user tables | Upgrade to the active head; independently verify exactly that one revision; `SCHEMA_INITIALIZED` |
| No revision but any other user table | `UNVERSIONED_SCHEMA_PRESENT`; no mutation |
| Older or different revision | `SCHEMA_UPGRADE_REQUIRES_UPDATE_WORKFLOW`; no mutation |
| Multiple revisions | `MULTIPLE_DB_REVISIONS`; no mutation |
| Database unavailable | `DATABASE_UNREACHABLE`; no secret-bearing exception text |

No candidate rows are read. Alembic runs through the active release's
Python API with an explicit connection using the protected DB URL; the URL
is not put in `alembic.ini`, argv, or the result. A migration failure reports
`SCHEMA_INITIALIZATION_FAILED`; a failed independent revision postcheck
reports `SCHEMA_POSTCHECK_FAILED`. This command does not stamp, downgrade,
select an arbitrary revision, recover a failed migration, or operate the
service. PostgreSQL and pgvector must already be provisioned; migration
failure does not install or change either.

**PR7 is not the production update migration workflow.** An existing older
schema must await the later backup/update/rollback safety boundary.
`SCHEMA_INITIALIZED` or `SCHEMA_ALREADY_CURRENT` does not mean the
application is ready, the service is healthy, or Ollama/model is ready.

### PR8 `deployment-ready` — installed deployment operator gate

Run as the trusted non-root install operator after `install-release`,
`activate-release`, `config-verify`, `schema-init`, `service-install`, and
`service-start`:

```bash
<root>/current/.venv/bin/python -m meyar.ops.cli deployment-ready \
  --install-root <root> --label <validated-launchd-label>
```

PostgreSQL/pgvector must already exist; PR11 provisions Ollama and verified
local model artifacts separately before this gate. The gate takes no DB URL,
Ollama URL, model name, port, service-user, or arbitrary plist path. Its only
plist authority is `/Library/LaunchDaemons/<label>.plist`, which must be a
safe, root-owned, regular, canonical-mode file with exact bytes from the
accepted renderer. Only after structural verification does the gate derive
the service user and port from that plist. It verifies the exact active
release Python/package/command and migration graph, PR5 protected `.env`,
PR6 dedicated principal and runtime permissions, and `launchctl print
system/<label>`. Application liveness probes only
`http://127.0.0.1:<validated-port>/api/v1/health`, via direct numeric-loopback
`http.client.HTTPConnection`; ambient proxies, DNS and redirects cannot
change the destination. The response must be HTTP 200 with JSON
`{"status":"ok"}`. DB reachability uses bounded `SELECT 1`; schema current
requires the sole active code/manifest head to equal the sole DB revision.
Ollama and both configured models use explicit protected Settings through the
existing local-only provider boundary. PR11 additionally requires the active
release's `sha256:<model-manifest-hash>` reference to resolve to an immutable
installed manifest and import receipt, exact configured names, local tag
digests, runtime version, Ollama LaunchDaemon, and synthetic embedding
dimensions. Name availability alone cannot pass. All findings use fixed codes without
secrets or raw response/exception text.

The result reports `active_release`, `host_config`, `model_manifest`, `service_plist`,
`service_principal`, `runtime_permissions`, `launchd`,
`application_liveness`, `database`, `db_schema`, `ollama`, `llm_model`, and
`embedding_model` separately. Their success codes are respectively
`ACTIVE_RELEASE_VERIFIED`, `HOST_CONFIG_VERIFIED`, `SERVICE_PLIST_VERIFIED`,
`SERVICE_PRINCIPAL_VERIFIED`, `RUNTIME_PERMISSIONS_VERIFIED`,
`SERVICE_VISIBLE`, `APPLICATION_LIVE`, `DATABASE_REACHABLE`,
`DB_REVISION_CURRENT`, `OLLAMA_REACHABLE`, `LLM_MODEL_AVAILABLE`, and
`EMBEDDING_MODEL_AVAILABLE`. A failed prerequisite yields a `SKIPPED`
finding; independent safe checks continue. Any `FAIL` makes `ok=false`.

The command opens the existing PR4 lock read-only and holds it during the
installed identity/config/plist/runtime snapshot. It releases the lock for
bounded HTTP/DB/model probes, then reacquires it for final installed-state,
launchd, and liveness verification before reporting `ok=true`. A concurrent mutation caught by
the lock or final check fails the gate; the result is a point-in-time operator
assessment, not a promise that the host remains ready after it exits.
It performs no filesystem or database mutation, permission repair, account
creation, service lifecycle action, migration, or model pull.

`launchd` visible != application live != deployment ready.
`GET /api/v1/health` remains liveness only. CLI `deployment-ready` is not
HTTP `/ready` (#46). A green CLI gate is neither real Target-Mac acceptance
nor production-model approval (#36); issues #35 and #46 remain open.

### PR11 offline Ollama and model provisioning

The bank Mac receives a reviewed local directory with only
`ai_bundle_manifest.json`, `model_manifest.json`, one macOS arm64 Ollama
Mach-O executable named by the manifest, and two single-file GGUF artifacts.
The transport manifest is version 1 and binds the runtime platform,
architecture, declared version, filename and SHA-256; the model-manifest
filename and SHA-256; and each LLM/EMBEDDING role, exact local tagged model
name, GGUF filename and SHA-256. It is separate from `ModelManifest`, which
continues to own model identity and approval status. The active release must
have been built with `--model-manifest-reference sha256:<SHA-256 of the exact
model_manifest.json>` and a status matching both entries. Older documentary
references cannot pass `model-install`, `model-verify`, or `deployment-ready`.
Use an operator-reviewed bundle and immutable release; hashes establish
integrity relative to that trusted handoff, not publisher authenticity.

From the installed release, use the trusted non-root install owner except
for the four privileged service mutations. The dedicated Ollama account
must already exist and differ from both the install owner and MEYAR app
service account. The application LaunchDaemon must already be installed.
Its full primary and supplementary group set must exclude the MEYAR service
GID recorded on `shared/config`; group lookup failure blocks installation.

```bash
<root>/current/.venv/bin/python -m meyar.ops.cli ai-bundle-verify --bundle-dir <local-ai-bundle>
<root>/current/.venv/bin/python -m meyar.ops.cli ollama-install --install-root <root> --bundle-dir <local-ai-bundle>
<root>/current/.venv/bin/python -m meyar.ops.cli ollama-service-install --install-root <root> --label <ollama-label> --app-label <app-label> --user-name <dedicated-ollama-user> --install-owner-uid <uid>
<root>/current/.venv/bin/python -m meyar.ops.cli ollama-service-start --install-root <root> --label <ollama-label> --app-label <app-label> --user-name <dedicated-ollama-user> --install-owner-uid <uid>
<root>/current/.venv/bin/python -m meyar.ops.cli model-install --install-root <root> --bundle-dir <local-ai-bundle>
<root>/current/.venv/bin/python -m meyar.ops.cli model-verify --install-root <root>
<root>/current/.venv/bin/python -m meyar.ops.cli deployment-ready --install-root <root> --label <app-label>
```

`ollama-service-stop`, `ollama-service-restart`, and read-only
`ollama-service-status --label <ollama-label>` are also available. Mutation
uses the existing PR4 lock and fixed `/bin/launchctl` system-domain argv.
The canonical plist runs exactly `<root>/shared/ollama/runtimes/<SHA-256>/ollama
serve` without a shell, as the dedicated non-root user, with fixed
`OLLAMA_HOST=127.0.0.1:<configured-port>`, `OLLAMA_MODELS`, private `HOME`,
`OLLAMA_NO_CLOUD=1`, and bounded MEYAR log paths. Runtime binaries and
manifests are owner-owned and read-only; the Ollama user owns only its
private model/state directories and dedicated logs. Root/shared/logs allow
traversal to those paths without read access to app releases, config secrets,
candidate storage, backups, or restores.
The service verifies those effective access rights against installed modes
and refuses widened paths. Local model tags reject `cloud` and any `-cloud`
suffix through the same validator used by production Settings.

`model-install` verifies every local hash, copies each GGUF into a temporary
service-readable staging directory, writes only `FROM <verified-local-GGUF>`
to a generated Modelfile, and runs fixed argv
`<immutable-ollama> create <manifest-name> -f <generated-Modelfile>` with a
fixed local-only environment. It never calls `ollama pull`, a model registry,
Git, Homebrew, `curl`, `wget`, `pip`, or `uv download`; it uses no shell and
needs no coding agent or network on the bank host. A name already present
without an exact receipt is a conflict. A failed partial import leaves no
completion receipt and never deletes unrelated models; operator inspection
is required before retry. Successful imports publish an immutable manifest
at `shared/ollama/manifests/<SHA-256>.json` plus a no-clobber receipt. The
receipt records source GGUF SHA-256 and Ollama's local tag digest separately.
The former is **not** the latter. `model-verify` checks `/api/version`,
`/api/tags` digests, service visibility, and one bounded synthetic `/api/embed`
probe against `MEYAR_EMBEDDING_DIMENSIONS`; it never sends candidate text.
Production request paths recheck local tag digests before candidate-bearing
Ollama calls. Raw Ollama bodies and process output never enter results.

**Model installed != model production-approved.** PR11 proves the
installation/verification mechanism only. All PR11 fixtures use
`DEVELOPMENT_INTEGRATION`; final production model approval remains Issue #36
and real Target-Mac behavior remains unconfirmed.

### PR9 `backup-create` and `backup-verify`

Run the installed release's Python as the trusted non-root install owner:

```bash
<root>/current/.venv/bin/python -m meyar.ops.cli backup-create \
  --install-root <root> --label <validated-launchd-label> \
  --backup-id <safe-id> --pg-bin-dir <absolute-postgresql-client-bin-dir>

<root>/current/.venv/bin/python -m meyar.ops.cli backup-verify \
  --install-root <root> --backup-id <safe-id> \
  --pg-bin-dir <absolute-postgresql-client-bin-dir>
```

The production sequence is `deployment-ready` → privileged `service-stop`
→ `backup-create` → `backup-verify` → privileged `service-start` →
`deployment-ready`. `backup-create` never controls launchd. Under the
existing PR4 lock it verifies the exact active release, code/manifest
Alembic head, protected Settings, canonical installed plist and runtime
permissions, sole current database revision, `launchctl print
system/<label>` exit **113**, and a refused direct connection to
`127.0.0.1:<canonical-port>`. Exit 0 means the service must be stopped;
unexpected exit, timeout, and ambiguous socket errors fail. A final
snapshot check repeats installed identity/config/plist and stopped-state
proof before publication. Independent external database writers must be
excluded by the maintenance procedure.

`<safe-id>` matches `[A-Za-z0-9][A-Za-z0-9_-]{0,79}`. Output is fixed at
`<root>/shared/backups/<safe-id>/`: `database.dump` (PostgreSQL `-Fc`),
`storage.tar` (relative descendants of `shared/storage`), and
`backup_manifest.json` (format version 1, backup ID/time/operator UID,
release ID/source SHA/Alembic head, fixed filenames, SHA-256 digests,
byte sizes, storage file count). The manifest contains no candidate,
tenant, path, or credential data. The private directory is `0700` and
files are `0600`. A private same-filesystem staging directory is verified
then atomically renamed with no-replace semantics; existing IDs are never
overwritten. Success requires the parent backup directory fsync after rename.
If that fsync fails, the command moves its identity-matched backup back to
private staging and fsyncs the same validated parent directory again. Only
a successful rollback fsync permits staging cleanup and
`BACKUP_PUBLICATION_DURABILITY_FAILED`. If rollback fsync fails, the private
stage is preserved and `BACKUP_PUBLICATION_STATE_UNCERTAIN` is reported.
That uncertain result also covers a changed final identity or unsafe
rollback; foreign content is preserved. Inspect `shared/backups`, resolve
any private `.backup-*` residue, and run `backup-verify` if the requested
public ID exists before retry. Do not assume that ID is durably free.

`--pg-bin-dir` must name a normalized real directory with trusted ownership
and no group/world write access; exact regular, non-symlink, executable
`pg_dump` and `pg_restore` files are required. No ambient PATH lookup is
used. Protected Settings supply the DB identity. `pg_dump` runs with fixed
argv `pg_dump -Fc --serializable-deferrable --no-password -h <host> -p
<port> -U <user> -d <database> -f <private-stage>/database.dump`. The
password is held in a private `0600` `PGPASSFILE`, escaped per libpq, and
removed before publication; it never enters argv, findings, manifest, or
logs. `backup-verify` checks exact schema/permissions/hashes, runs the
validated `pg_restore --list` without a database connection, and parses
the tar without extracting. It never reads `.env` or changes the backup.
Storage symlinks, hardlinks, special nodes, unsafe names, and duplicate
archive entries are rejected. A created and verified backup is **not**
restore-tested.

### PR10 `restore`

PR9 / PR #72 is merged and post-merge verified on accepted main
`2dcf0a8783e49008b0eb707d81b42248209b34d6`. To restore, first run
`backup-verify`, then have a PostgreSQL operator precreate an empty isolated
database on the protected host/port for the protected DB principal:

```bash
<root>/current/.venv/bin/python -m meyar.ops.cli restore \
  --install-root <root> --backup-id <safe-backup-id> \
  --restore-id <safe-restore-id> --target-database <isolated-database-name> \
  --pg-bin-dir <absolute-postgresql-client-bin-dir>
```

Only `RESTORE_COMPLETED` is success. PR10 holds the PR4 lock, reuses PR9
verification, requires exact active release ID/source SHA/Alembic head,
reads the PR5 protected Settings, and refuses the production database name
or a nonempty target. It streams verified storage into private
`shared/restores/.restore-*` staging, checks the extracted file tree, runs
trusted absolute `pg_restore` with `--exit-on-error --single-transaction
--no-owner --no-privileges --no-password`, checks exactly one restored
Alembic revision, then publishes `shared/restores/<restore-id>/` without
replacement and writes a durable non-secret completion manifest. The
password uses a private, removed `PGPASSFILE`. Storage and database cannot
be one atomic transaction: after DB success, later failure returns
`RESTORE_INCOMPLETE_ISOLATED_TARGET` and preserves isolated state. Discard
and recreate that target DB before retrying; no database is dropped by the
tool. Neither production DB nor live `shared/storage` is written. An
isolated restore completed **!=** production cutover or readiness. Update,
rollback, and cutover remain future #35 work. See `docs/BACKUP_RESTORE.md`.

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
- No service uninstall, production database restore cutover, or PostgreSQL provisioning.
  PR6's privileged lifecycle foundation
  installs and controls the MEYAR LaunchDaemon; it does not invoke
  `sudo`, escalate itself, or create a service account.
- PR12 supports only `APP_ONLY` and `FORWARD_COMPATIBLE_SCHEMA` application
  update/rollback. There is no automatic Alembic downgrade or destructive
  production restore.
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

**#35 PR6 (D-075, merged as PR #69):** explicit privileged
install-owner boundary, dedicated service principal/group resolution,
protected runtime write paths, no-clobber canonical plist publication,
PR4 lock coordination, and fixed-argv system launchctl start/stop/restart.
No application/database/model readiness, migrations, update/rollback,
or target-Mac acceptance.

**#35 PR7 (D-076, merged as PR #70):** `schema-init` binds the
running package and Alembic graph to the verified active release, then
initializes only a fresh empty database. It does not upgrade an existing
production schema or perform backup, rollback, service, or model work.

**#35 PR8 (D-077, merged as PR #71):** `deployment-ready` is the
read-only installed-host operator gate described above. No HTTP `/ready` or
Target-Mac acceptance is included.

**#35 PR9 (D-078, merged as PR #72):** quiesced production backup
creation and read-only structural verification.

**#35 PR10 (D-079, merged as PR #73):** verified backup to an
empty isolated database and private restore workspace. No production
cutover, update, or rollback is included.

**#35 PR11 (D-080, merged as PR #74):** offline local GGUF/Ollama
provisioning, dedicated LaunchDaemon, exact installed manifest/digest
verification, and readiness/runtime identity checks. No model approval.

**#35 PR12 (D-081, merged as PR #75):** staged update and explicit
application rollback with mandatory verified backup and durable receipts.
No downgrade or production restore cutover.

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
PR6's lifecycle simulation, `build-release`, and the full test suite —
has been run and verified **on
Linux only**, against the local Ollama daemon and the local PostgreSQL/
`pgvector`
container described in `docker-compose.yml`. `service-status`'s tests use
an injected fake `launchctl` runner; no test claims real `launchctl`
behavior.

**Apple Silicon / Mac mini M4 Pro reference-hardware rehearsal remains
UNCONFIRMED.** No claim of Apple-Silicon runtime acceptance, real
`launchctl print`/`bootstrap`/`bootout`/`kickstart` behavior,
service-survives-reboot behavior,
or bank-Mac verification is made by this document. That verification is
issue #35's later physical-host rehearsal plus issue #36, and
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

## PR12 — staged production update and explicit rollback (D-081)

PR11 / PR #74 is merged and post-merge verified. The accepted base for
PR12 is `38624ca3440c7dbf750d4248d8e0a9992b7192b5`. Both application
releases must already be installed and verified with PR4's offline
`install-release` and `verify-install`; this workflow uses no Git, network,
download, or rebuild on the deployment host. Run each unprivileged phase
as the trusted non-root install owner from the *then-current* installed
release. The bank invokes only `service-stop` and `service-start` in its
approved external root context, with the exact PR6 arguments above.

**Update sequence:**

```bash
<approved-python3.12> <bundle>/meyar-ops.py install-release --bundle-dir <bundle> --install-root <root>
<approved-python3.12> <bundle>/meyar-ops.py verify-install --install-root <root> --release-id <target-release-id>
<root>/current/.venv/bin/python -m meyar.ops.cli update-prepare --install-root <root> --label <app-label> --update-id <safe-id> --target-release-id <target-release-id>
sudo <root>/current/.venv/bin/python -m meyar.ops.cli service-stop --label <app-label> --user-name <app-service-user> --install-root <root> --install-owner-uid <install-owner-uid> --port <app-port>
<root>/current/.venv/bin/python -m meyar.ops.cli backup-create --install-root <root> --label <app-label> --backup-id <safe-backup-id> --pg-bin-dir <trusted-postgresql-bin-dir>
<root>/current/.venv/bin/python -m meyar.ops.cli backup-verify --install-root <root> --backup-id <safe-backup-id> --pg-bin-dir <trusted-postgresql-bin-dir>
<root>/current/.venv/bin/python -m meyar.ops.cli update-apply --install-root <root> --label <app-label> --update-id <safe-id> --backup-id <safe-backup-id> --pg-bin-dir <trusted-postgresql-bin-dir>
sudo <root>/current/.venv/bin/python -m meyar.ops.cli service-start --label <app-label> --user-name <app-service-user> --install-root <root> --install-owner-uid <install-owner-uid> --port <app-port>
<root>/current/.venv/bin/python -m meyar.ops.cli update-finalize --install-root <root> --label <app-label> --update-id <safe-id>
```

`update-prepare` requires full healthy `deployment-ready`, installed
release/model/config identity, unchanged model manifest reference and
approval status, and a supported schema path. It probes readiness without
holding the operation lock, then reacquires that lock and rechecks identity
before publishing `plan.json`. The plan binds the config file's device,
inode, size, and nanosecond mtime; it never stores config bytes or a
password-derived hash. A changed config requires a new update ID and plan.
`update-apply` independently requires the accepted launchd exit-113
absence, re-verifies the entire PR9 backup (hashes, tar, dump structure),
and binds its release ID, source SHA, head, and creation time to the plan.
The service remains stopped after `UPDATE_APPLIED_SERVICE_STOPPED`.
Only successful full readiness followed by a durable `finalize.json`
returns `UPDATE_COMPLETED`. A readiness failure leaves the new release
active; stop the service before an explicit rollback.

**Rollback sequence:**

```bash
sudo <root>/current/.venv/bin/python -m meyar.ops.cli service-stop --label <app-label> --user-name <app-service-user> --install-root <root> --install-owner-uid <install-owner-uid> --port <app-port>
<root>/current/.venv/bin/python -m meyar.ops.cli rollback-apply --install-root <root> --label <app-label> --update-id <safe-id>
sudo <root>/current/.venv/bin/python -m meyar.ops.cli service-start --label <app-label> --user-name <app-service-user> --install-root <root> --install-owner-uid <install-owner-uid> --port <app-port>
<root>/current/.venv/bin/python -m meyar.ops.cli rollback-finalize --install-root <root> --label <app-label> --update-id <safe-id>
```

The destination is only the verified transaction's prior release; no
arbitrary release argument exists. `APP_ONLY` requires identical Alembic
heads and makes no DB change. `FORWARD_COMPATIBLE_SCHEMA` requires one
statically proven target head and a single unbranched descendant path
from the source head. Only its target installed Python executes `alembic
upgrade <exact-target-head>` against protected host config, with fixed
argv, suppressed process output, and an exact before/after DB revision
check. Its rollback means **previous application release + newer,
explicitly compatible DB schema**. Rollback never means Alembic
downgrade. Normal readiness still requires exact app/DB heads; the sole
exception requires matching rollback activation metadata, plan, receipt,
source release, model identity, and target DB head, and reports
`DB_REVISION_FORWARD_COMPATIBLE_ROLLBACK`.

`BACKUP_RESTORE_REQUIRED` fails at prepare with
`UPDATE_REQUIRES_RESTORE_PROCEDURE`; `PROHIBITED_PENDING_PROCEDURE` fails
with `UPDATE_PROHIBITED_PENDING_PROCEDURE`. A model reference or approval
status change fails `UPDATE_MODEL_CHANGE_UNSUPPORTED`. PR12 neither
restores a production DB nor provisions a new model.

The private install-owner `0700` area
`<root>/shared/updates/<update-id>/` holds append-only `0600`
`plan.json`, `apply.json`, `finalize.json`, `rollback.json`, and
`rollback_finalize.json`. A phase file appears only after that phase
completes, via exclusive no-follow creation and file/directory fsync.
Records contain operator UID/time, from/to release/source/head,
compatibility, unchanged model reference/status, app label, config file
identity, backup ID, and activation generation evidence; no DB URL,
password, environment value, candidate identifier, CV path, prompt, or
model output. An uncertain publication requires inspecting and retrying
the same transaction; a matching completed phase is idempotent.

If a forward migration commits while the old app pointer remains, the
declared compatibility permits that exact app/schema pair during recovery.
Retry `update-apply` with the same plan and verified backup: it sees the
target DB head and activates without rerunning migration. If pointer
activation completed but the apply or rollback receipt was interrupted,
the exact activation generation can reconstruct only its own missing
receipt. A foreign generation is refused. A new update is blocked over
an unfinished transaction or a forward-compatible rollback. That latter
state requires a separately reviewed reconciliation procedure before any
new update; PR12 provides no generic schema-override flag or production
restore cutover. Real Apple-Silicon/launchd behavior remains unconfirmed.

## PR13 — diagnostics, HTTPS edge, reboot proof, cleanup, lifecycle matrix (D-082)

PR12 / PR #75 merged and was post-merge verified. Accepted main is
`7c911cbc850d21999eae2239e6b51bfedc570d4a`. PR13 is engineering
closure tooling for #35; the separate owner acceptance audit decides
whether #35 can close. Run the active release's Python as the trusted
non-root install owner. IDs match `[A-Za-z0-9][A-Za-z0-9_-]{0,79}`.

```bash
<root>/current/.venv/bin/python -m meyar.ops.cli collect-diagnostics --install-root <root> --app-label <app-label> --ollama-label <ollama-label> --diagnostic-id <id>
<root>/current/.venv/bin/python -m meyar.ops.cli diagnostics-verify --install-root <root> --diagnostic-id <id>
<root>/current/.venv/bin/python -m meyar.ops.cli edge-verify --origin https://<bank-hostname> --ca-file <bank-approved-ca-file> --install-root <root> --app-label <app-label> --edge-id <id>
<root>/current/.venv/bin/python -m meyar.ops.cli reboot-prepare --install-root <root> --app-label <app-label> --ollama-label <ollama-label> --reboot-id <id>
# Human/IT performs one normal machine reboot here.
<root>/current/.venv/bin/python -m meyar.ops.cli reboot-verify --install-root <root> --app-label <app-label> --ollama-label <ollama-label> --reboot-id <id>
<root>/current/.venv/bin/python -m meyar.ops.cli cleanup --install-root <root>
<root>/current/.venv/bin/python -m meyar.ops.cli cleanup --install-root <root> --apply
<root>/current/.venv/bin/python -m meyar.ops.cli lifecycle-acceptance --install-root <root> --app-label <app-label> --ollama-label <ollama-label> --run-id <id> --backup-id <backup-id> --restore-id <restore-id> --update-id <update-id> --reboot-id <reboot-id> --edge-id <edge-id> --diagnostic-id <diagnostic-id> --smoke-id <smoke-id> --pg-bin-dir <trusted-postgresql-bin-dir>
```

Omit `--ca-file` for system trust. `edge-verify` without the three evidence
arguments is a read-only probe; supply `--install-root`, `--app-label`, and
`--edge-id` together to publish a receipt for lifecycle acceptance. The
bank manages TLS/reverse proxy/ingress. Browsers reach its HTTPS origin;
the proxy reaches MEYAR on numeric `127.0.0.1:<MEYAR-port>`. Uvicorn and
Ollama remain loopback-only. The verifier makes only an unauthenticated
direct `GET /api/v1/health` to the explicit HTTPS origin: no redirect,
proxy environment, cookies, API key, request body, candidate data, or
arbitrary path. It requires system or explicit CA trust, hostname
validation, HTTP 200, and exact `{"status":"ok"}` within 1024 bytes and
three seconds. No internet dependency or proxy installer is introduced.

`collect-diagnostics` writes only the following `0600` files inside a
new `0700` `<root>/shared/diagnostics/<id>/` directory, with no overwrite:
`manifest.json`, `environment.json`, `deployment.json`, `services.json`,
`database.json`, `ai.json`, `operations.json`, `filesystem.json`, and
`checksums.sha256`. These carry safe release/source/schema/model manifest,
installed LLM and embedding digests, and Ollama runtime version/hash
identities, fixed finding codes, platform/Python, service labels/status,
and filesystem capacity. They contain no raw logs, launchctl output,
config values, database rows/dumps, candidate filename/content/identity,
query, prompt, model response, or storage enumeration. No upload exists.
The command takes the operation lock to capture deployment identity,
releases it for bounded readiness/service probes, then reacquires it to
recheck active release, config file identity, model receipt, and service
plist before no-clobber publication. A changed identity fails
`DIAGNOSTICS_STATE_CHANGED`. Collection self-verifies exact members,
private modes, JSON shape, duplicate keys, size bounds, and hashes.

`reboot-prepare` records macOS `kern.boottime` (via fixed `/usr/sbin/sysctl`
argv), active release/source/schema/model/runtime, labels, time, and
operator UID under `shared/reboots/<id>-prepare/`. It never reboots the
machine. After a human/IT reboot, `reboot-verify` requires a strictly
newer boot identity, unchanged deployment identity, both visible system
LaunchDaemons, full `deployment-ready` (including app liveness), and
live model verification before publishing a separate private
`<id>-verify` receipt. Same-boot verification fails truthfully.

`cleanup` is dry-run by default. `--apply` deletes only a proven orphan
`<root>/.next-current-<32 hex>` symlink created by PR4 activation, with
the exact canonical activation-generation target. It refuses deletion
when a similarly named, wrong-type, foreign-owner, unknown-target, or
active-target entry is present. The operation lock protects mutation.
It never deletes releases, generations, config, storage, logs, backups,
restores, updates, models, diagnostics, benchmarks, or DB data.

`lifecycle-acceptance` validates current Darwin/arm64 and active CPython,
immutable release/source/config/schema/model, services and full readiness,
then the named reboot, verified backup, isolated restore, finalized update
and explicit rollback, HTTPS edge, diagnostics, and synthetic smoke evidence.
Run the smoke worker directly on the installed host:
`<root>/current/.venv/bin/python -I -m meyar.smoke_workload --ops-install-root <root> --app-label <app-label> --smoke-id <id>`.
`backend/scripts/fresh_deployment_smoke.py` dispatches that same command
when invoked from a checkout with all three evidence options. The worker
verifies the exact active release, its installed Python/source and Alembic
head before creating a disposable PostgreSQL container and temporary
storage. Migration, CLI and Uvicorn subprocesses all use the verified
immutable release Python with
`-I`; Alembic uses that release's `backend/alembic.ini` and migration tree.
The Docker endpoint must be a local Unix socket; the disposable database has
a random name/password and a `127.0.0.1`-only port mapping. Evidence-mode
Uvicorn inherits a pre-bound loopback socket so HTTP responses can only come
from the launched installed-release process.
The worker re-verifies release, model and configuration identity before
publishing. It strips ambient `MEYAR_*` and `PYTHON*` values from the smoke
subprocess environment, sets the disposable DB/storage explicitly, and uses
only the protected host settings for the local Ollama/model identities.
Docker and local Ollama are required; `uv` is used only by the ordinary
checkout developer mode. A failed extraction, embedding, or other
required capability publishes no PASS receipt. This is not a production-
tenant seeder or destination-host Git deployment instruction.

Smoke receipt format 2 records only `smoke_id`, PASS, release/source/head,
`execution_mode: installed-release`, `disposable_database: true`,
`all_required_steps_passed: true`, and fixed `passed_checks` identifiers:
`fresh_schema`, `auth_login`, `synthetic_cv_ingestion`, `local_extraction`,
`local_embedding`, `candidate_library_detail`, `structured_search`,
`vacancy_flow`, `deterministic_scoring`, `deterministic_ranking`,
`original_cv_authorization`, and `restart_persistence`. Legacy receipts
cannot satisfy lifecycle acceptance.
Missing evidence is `INCOMPLETE`, invalid/tampered evidence is `FAIL`, and
every check must be `PASS` for overall `PASS`. The new `0700`
`shared/acceptance/<run-id>/` directory has `0600` `manifest.json`,
`checks.json`, `summary.json`, and `checksums.sha256`. `summary.json`
contains `status: PASS|FAIL|INCOMPLETE`, release/source, and fixed-code
check results. This command performs no service/update/restore mutation.

The #35 agentless sequence is: preflight → offline install → configure →
schema-init → offline Ollama/models → app/Ollama LaunchDaemons →
deployment-ready → human reboot proof → synthetic smoke → backup →
isolated restore → staged update → explicit rollback rehearsal → HTTPS
edge verification → diagnostics → lifecycle acceptance. The safe
diagnostics metadata carries release/source/schema/model manifest, installed
model digests, runtime version/hash, platform, and service-readiness identities
for #36; it does not duplicate
`backend/scripts/target_mac_benchmark.py` or select a production model.

| Stage | PR13 state |
| --- | --- |
| Tooling implemented | proposed in PR13; independent audit pending |
| Linux simulations | executed in PR13 quality gate |
| Apple-Silicon lifecycle rehearsal | **INCOMPLETE / hardware unavailable in this run** |
| Bank-Mac benchmark and production model | **unstarted; Issue #36** |

**Issue #35 tooling complete != real bank Mac benchmark complete.** Real
macOS arm64 native runtime, launchctl, and reboot behavior are not
established by Linux tests. Issue #36 remains next after #35 acceptance.
Issue #49 is post-presentation capability expansion. #35/#46 remain OPEN.
