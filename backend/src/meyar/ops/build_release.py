"""`meyar-ops build-release` — builds an immutable, verifiable MEYAR
APPLICATION release artifact from an exact Git commit (issue #35 PR3).
See docs/MEYAR_OPS.md and docs/DECISIONS.md (D-068).

**Scope boundary (read this before touching this module):** this builds
an application/source release artifact only — MEYAR source, migrations,
`pyproject.toml`/`uv.lock`, and release identity/integrity metadata. It
does NOT provision a Python runtime, `uv`, third-party wheels/an offline
wheelhouse, Ollama, models, or PostgreSQL, and it does NOT define a host
install layout or perform any install/activation/service-lifecycle
action. A later #35 slice delivers offline dependency provisioning
before this artifact is sufficient for real deployment.

**Provenance is the whole point.** Every byte in the produced artifact,
and every source-derived `ReleaseManifest` field, comes from the exact
selected Git commit object (`--source-sha`) via fixed-argv Git plumbing
(`git rev-parse`, `git cat-file`, `git ls-tree` — never `shell=True`,
never a shell command string) — never from the mutable working tree. A
dirty/untracked/modified working tree can never change the bytes
produced for a given commit: every file's content is read from its Git
blob object (`git cat-file -p <blob-sha>`), never from disk.

**Allowlist, not "tar everything and exclude bad things."** Only five
pathspecs are ever passed to Git (`ALLOWED_PATHSPECS`); `backend/tests/`,
`backend/scripts/`, `.env`, `.git/`, real CVs, and everything else never
enter the picture even implicitly. Every listed tree entry is also
independently checked (`_list_allowlisted_tree_entries`) and any symlink,
submodule, or otherwise-non-regular-file entry aborts the whole build
before a single byte is written — this is defense-in-depth against a
future commit accidentally introducing one of those shapes under an
otherwise-allowed path.

**No byte-for-byte reproducibility claim.** `built_at` is a real build
timestamp and legitimately differs between two builds of the same
commit — see `ReleaseManifest.built_at`. What IS deterministic: content
selection (the exact same commit always yields the exact same allowlisted
file set/bytes) and archive-internal normalization (lexical member
order, fixed uid/gid/mode, empty uname/gname, a fixed member `mtime`, and
a normalized gzip header) — see `_build_tar_gz_bytes`. Two builds of the
same commit at different times will still differ in `built_at` and
therefore in the embedded manifest bytes, the external manifest bytes,
and the resulting checksums; this module never claims otherwise.

**Output-write safety mirrors `service_plist.py`'s pattern.** Each of the
three release-bundle files is created with `O_CREAT | O_EXCL` (never a
`Path.exists()` pre-check, so no overwrite race) relative to a dir-fd
already anchored to `--output-dir`; an existing target aborts the build
before anything is replaced, and any failure after this invocation has
created one or more of the three files removes only the files this
invocation itself created (identity-checked via `(st_dev, st_ino)`,
exactly like `service_plist._unlink_if_same_file`) — a pre-existing file
at the same path, or a file some other process swapped in afterward, is
never touched. The one exception is the rare case where `os.fstat(fd)`
itself fails immediately after this invocation's own create succeeded:
there is then no `(st_dev, st_ino)` to identity-check against, so cleanup
is deliberately skipped rather than guessed — see
`OutputIdentityUnavailableError` — and the created file is left in place
for operator inspection while the raw fd is still always closed and no
later bundle file is ever published.

**READ selected source metadata, never EXECUTE it (PR #56 corrective
pass).** Alembic heads for the manifest are derived by parsing selected-
commit `backend/alembic/versions/*.py` blobs with `ast`
(`meyar.ops.alembic_static_metadata`) — never Alembic's own
`ScriptDirectory`, which imports/executes each migration module as a
side effect of building its revision map. `release_version` (read from
the selected commit's `pyproject.toml`, itself attacker-controlled
content for any selected commit) and the `release_id` derived from it
are validated as safe single filesystem path components before either
can reach an output filename or archive member root
(`_validate_filesystem_safe_component`) — an unsafe value fails the
build truthfully rather than being silently normalized.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import io
import os
import re
import subprocess
import tarfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Protocol

from meyar.ops import archive_safety
from meyar.ops.alembic_static_metadata import (
    AlembicStaticMetadataError,
    compute_static_alembic_heads,
)
from meyar.ops.archive_safety import ArchiveBoundExceededError, inspect_archive_members
from meyar.ops.model_manifest import ModelApprovalStatus
from meyar.ops.redact import safe_exception_text
from meyar.ops.release_manifest import (
    ModelManifestReference,
    ReleaseManifest,
    RollbackCompatibility,
    compute_release_id,
    parse_project_metadata,
)
from meyar.ops.result import FindingStatus, OpsResult, OpsResultBuilder

_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_GIT_TIMEOUT_SECONDS = 30.0
_MAX_MESSAGE_CHARS = 480

# The exact, narrow application-source allowlist (issue #35 PR3 §6). Only
# these pathspecs are ever passed to `git ls-tree`/resolved into archive
# members — nothing else is ever considered, regardless of what else is
# tracked at the selected commit.
ALLOWED_PATHSPECS: tuple[str, ...] = (
    "backend/src/meyar",
    "backend/pyproject.toml",
    "backend/uv.lock",
    "backend/alembic.ini",
    "backend/alembic",
)

_ALLOWED_EXACT_FILES = frozenset(
    {"backend/pyproject.toml", "backend/uv.lock", "backend/alembic.ini"}
)
_ALLOWED_DIR_PREFIXES = ("backend/src/meyar/", "backend/alembic/")

# Git tree modes for an ordinary regular file (non-executable / executable).
# `120000` (symlink) and the `commit` object type (submodule) are rejected
# outright — see `_list_allowlisted_tree_entries`.
_ALLOWED_REGULAR_FILE_MODES = frozenset({"100644", "100755"})

ARTIFACT_FORMAT = "tar.gz"
ARTIFACT_FORMAT_VERSION = 1
INTERNAL_MANIFEST_MEMBER_NAME = "release_manifest.json"

# Fixed, deterministic tar member metadata — see module docstring
# "No byte-for-byte reproducibility claim" for what this does and does not
# guarantee.
_FIXED_MEMBER_MODE = 0o644
_FIXED_MEMBER_MTIME = 0
_FIXED_UID = 0
_FIXED_GID = 0


def _bounded(text: str, limit: int = _MAX_MESSAGE_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class GitRunner(Protocol):
    def __call__(self, argv: list[str], *, cwd: Path) -> subprocess.CompletedProcess[bytes]: ...


def default_git_runner(argv: list[str], *, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603 - fixed argv (caller always prepends "git"), no shell
        argv,
        cwd=cwd,
        capture_output=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        check=False,
    )


class GitCommandError(Exception):
    """A `git` invocation exited non-zero. `stderr_text` is already
    bounded — never the raw, unbounded stderr stream."""

    def __init__(self, argv: list[str], returncode: int, stderr_text: str) -> None:
        self.argv = argv
        self.returncode = returncode
        self.stderr_text = stderr_text
        super().__init__(
            f"git command failed (exit {returncode}): {' '.join(argv)}: {stderr_text}"
        )


class GitCommitNotFoundError(Exception):
    def __init__(self, source_sha: str) -> None:
        self.source_sha = source_sha
        super().__init__(f"commit not found in local repository: {source_sha}")


class UnsafeGitTreeEntryError(Exception):
    """One or more selected-commit tree entries under the allowlisted
    pathspecs are not an ordinary regular file — a symlink, submodule, or
    otherwise-unsupported entry shape. The whole build aborts; nothing is
    written."""

    def __init__(self, entries: list[GitTreeEntry]) -> None:
        self.entries = entries
        summary = "; ".join(f"{e.path} (mode={e.mode}, type={e.obj_type})" for e in entries[:10])
        super().__init__(f"{len(entries)} unsafe git tree entr(y/ies): {summary}")


class UnsafeBuiltArchiveError(Exception):
    """Raised when the artifact this invocation itself just built fails
    the same safety inspection `verify-release` performs. Should never
    happen in normal operation (every member is constructed by this
    module), but the check runs unconditionally — see docs/MEYAR_OPS.md
    §13 — so a latent bug here is a truthful build failure with cleanup,
    never a published bundle that would go on to fail its own
    verify-release round trip. `code` distinguishes a resource-bound
    violation from an unsafe-member-shape violation, mirroring
    `verify_release`'s own `ARCHIVE_RESOURCE_BOUND_EXCEEDED` /
    `UNSAFE_ARCHIVE_MEMBER` distinction."""

    def __init__(self, message: str, *, code: str) -> None:
        self.code = code
        super().__init__(message)


class RequiredReleaseFileMissingError(Exception):
    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(f"selected commit has no '{path}' — required for a release build")


class GitBlobTooLargeError(Exception):
    """Raised when a selected-commit blob's Git-declared size (`git
    cat-file -s`) already exceeds the release-artifact aggregate size
    bound — checked before that blob's content is ever read via `git
    cat-file -p`, so a pathologically large allowlisted blob can never be
    fully buffered into memory first and rejected only afterward (see
    module docstring "Resource-safety review", PR #56 corrective pass)."""

    def __init__(self, path: str, declared_size: int, limit: int) -> None:
        self.path = path
        self.declared_size = declared_size
        self.limit = limit
        super().__init__(
            f"blob for '{path}' has declared size {declared_size} bytes, exceeding the "
            f"release-artifact aggregate bound of {limit} bytes"
        )


class ReleaseIdentityUnsafeError(Exception):
    """Raised when a commit-derived release-identity value (`release_version`
    from the selected commit's `pyproject.toml`, or the `release_id`
    computed from it) is unsafe to use as a filesystem path component.
    `release_version` is attacker-controlled content for any commit an
    operator selects — a value containing a path separator or traversal
    sequence must never reach `os.open(..., dir_fd=output_dir_fd)`, which
    would otherwise let a malicious selected commit write outside
    `--output-dir`."""

    def __init__(self, field: str, value: str, reason: str) -> None:
        self.field = field
        self.value = value
        self.reason = reason
        super().__init__(f"{field} is unsafe for filesystem use ({reason}): {value!r}")


class OutputDirInvalidError(Exception):
    def __init__(self, output_dir: Path, reason: str) -> None:
        self.output_dir = output_dir
        self.reason = reason
        super().__init__(f"--output-dir invalid ({reason}): {output_dir}")


class OutputTargetExistsError(Exception):
    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"output target already exists: {path}")


class OutputIdentityUnavailableError(Exception):
    """Raised when `os.fstat(fd)` itself fails immediately after this
    invocation's own `O_CREAT | O_EXCL` create succeeded (PR #56 final
    resource-safety corrective pass). This is the one point in
    `_create_exclusive` where no `(st_dev, st_ino)` identity is available
    for the file this invocation just created — every later failure path
    already holds `created_stat` and can identity-check cleanup exactly
    like `_cleanup_created_file` does everywhere else.

    Deliberately fail-closed: this invocation never guesses that the
    on-disk path is still the file it just created and unlinks it anyway —
    a concurrent actor may already have replaced it at the same basename,
    and an un-identity-checked delete is exactly the race this module's
    cleanup discipline exists to prevent (see `_cleanup_created_file`). The
    raw fd is always closed regardless; only the on-disk path is left
    untouched, for operator inspection/removal. No later bundle file
    (manifest, SHA256SUMS) is ever written once this is raised."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(
            "could not verify identity of the file just created (os.fstat failed) — "
            f"left in place, un-cleaned, for operator inspection: {path}"
        )


def validate_source_sha(value: str) -> None:
    if not _FULL_SHA_RE.fullmatch(value):
        raise ValueError("source_sha must be exactly 40 lowercase hex characters")


_CONTROL_CHARS = frozenset(chr(c) for c in range(0x20)) | {chr(0x7F)}


def _validate_filesystem_safe_component(value: str, *, field: str) -> None:
    """Validates `value` is safe to use as a single filesystem path
    component — a release-bundle output basename or the basename prefix
    embedded in one. Never normalizes/coerces an unsafe value into a safe
    one; the caller must fail the build truthfully instead. Rejects:
    empty; `.`/`..`; any path separator (`/` or `\\`, so a value cannot
    itself introduce a multi-component path or a Windows-style separator
    on a system that honors it); and any ASCII control character
    (0x00-0x1F, 0x7F), which also covers embedded NUL."""
    if not value:
        raise ReleaseIdentityUnsafeError(field, value, "empty")
    if value in (".", ".."):
        raise ReleaseIdentityUnsafeError(field, value, "'.' or '..' is not a valid path component")
    if "/" in value or "\\" in value:
        raise ReleaseIdentityUnsafeError(field, value, "contains a path separator")
    if any(ch in _CONTROL_CHARS for ch in value):
        raise ReleaseIdentityUnsafeError(field, value, "contains a control character")


def _run_git(argv: list[str], *, cwd: Path, runner: GitRunner) -> bytes:
    """Runs `["git", *argv]` via the injected runner. Any infrastructure-
    level failure to even invoke Git (binary missing, subprocess timeout)
    is normalized to `GitCommandError` here, the single point where every
    `_run_git` call site's exceptions are uniform — callers only ever
    handle `GitCommandError`, never a raw `OSError`/`TimeoutExpired`."""
    full_argv = ["git", *argv]
    try:
        completed = runner(full_argv, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitCommandError(full_argv, -1, safe_exception_text(exc)) from exc
    if completed.returncode != 0:
        stderr_text = completed.stderr.decode("utf-8", errors="replace") if completed.stderr else ""
        raise GitCommandError(full_argv, completed.returncode, _bounded(stderr_text))
    return completed.stdout


def _discover_repo_root(*, invocation_cwd: Path, runner: GitRunner) -> Path:
    raw = _run_git(["rev-parse", "--show-toplevel"], cwd=invocation_cwd, runner=runner)
    return Path(raw.decode("utf-8").strip())


def _verify_commit_exists(source_sha: str, *, repo_root: Path, runner: GitRunner) -> None:
    """A non-zero exit here is an expected, meaningful outcome (the commit
    does not exist) — not a command failure — so it is reported as
    `GitCommitNotFoundError`, distinct from an infrastructure-level
    `GitCommandError` (Git binary missing, timeout)."""
    argv = ["git", "cat-file", "-e", f"{source_sha}^{{commit}}"]
    try:
        completed = runner(argv, cwd=repo_root)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitCommandError(argv, -1, safe_exception_text(exc)) from exc
    if completed.returncode != 0:
        raise GitCommitNotFoundError(source_sha)


@dataclass(frozen=True)
class GitTreeEntry:
    mode: str
    obj_type: str
    blob_sha: str
    path: str


def _path_is_allowed(path: str) -> bool:
    if path in _ALLOWED_EXACT_FILES:
        return True
    return any(path.startswith(prefix) for prefix in _ALLOWED_DIR_PREFIXES)


def _list_allowlisted_tree_entries(
    source_sha: str, *, repo_root: Path, runner: GitRunner
) -> list[GitTreeEntry]:
    """Lists every blob under `ALLOWED_PATHSPECS` at `source_sha`, using
    `--full-tree` so paths are always repo-root-relative regardless of
    `repo_root` itself (defense in depth — `repo_root` is already the
    real toplevel). Raises `UnsafeGitTreeEntryError` (nothing written yet)
    if any entry is a symlink, a submodule, or otherwise not an ordinary
    regular file, or lexically escapes the allowlist."""
    raw = _run_git(
        ["ls-tree", "-r", "-z", "--full-tree", source_sha, "--", *ALLOWED_PATHSPECS],
        cwd=repo_root,
        runner=runner,
    )
    text = raw.decode("utf-8")
    entries: list[GitTreeEntry] = []
    unsafe: list[GitTreeEntry] = []
    for record in text.split("\0"):
        if not record:
            continue
        meta, _sep, path = record.partition("\t")
        mode, obj_type, blob_sha = meta.split(" ")
        entry = GitTreeEntry(mode=mode, obj_type=obj_type, blob_sha=blob_sha, path=path)

        if ".." in PurePosixPath(path).parts or path.startswith("/"):
            unsafe.append(entry)
            continue
        if not _path_is_allowed(path):
            unsafe.append(entry)
            continue
        if obj_type == "commit":  # submodule
            unsafe.append(entry)
            continue
        if mode == "120000":  # symlink
            unsafe.append(entry)
            continue
        if obj_type != "blob" or mode not in _ALLOWED_REGULAR_FILE_MODES:
            unsafe.append(entry)
            continue
        entries.append(entry)

    if unsafe:
        raise UnsafeGitTreeEntryError(unsafe)
    return entries


def _read_blob(blob_sha: str, *, repo_root: Path, runner: GitRunner) -> bytes:
    return _run_git(["cat-file", "-p", blob_sha], cwd=repo_root, runner=runner)


def _blob_declared_size(blob_sha: str, *, repo_root: Path, runner: GitRunner) -> int:
    """Reads the blob's size from Git's own object metadata (`git
    cat-file -s`) — a small, fixed-size response — without reading any of
    the blob's actual content."""
    raw = _run_git(["cat-file", "-s", blob_sha], cwd=repo_root, runner=runner)
    return int(raw.decode("ascii").strip())


def _fetch_all_blobs(
    entries: list[GitTreeEntry], *, repo_root: Path, runner: GitRunner
) -> tuple[dict[str, bytes], int]:
    """Fetches every entry's blob content, but checks each blob's
    Git-declared size against the release artifact's own accepted
    aggregate-size bound (`archive_safety.MAX_AGGREGATE_UNCOMPRESSED_SIZE`)
    *before* reading that blob's content — so neither a single
    pathologically large allowlisted blob nor several blobs whose sizes
    sum past the bound can be fully buffered into memory first and
    rejected only afterward (the running total already includes the
    current blob's declared size before its content is ever read).

    Returns `(content_by_path, aggregate_declared_size)` — the caller
    reuses the Git-declared aggregate source size for the final
    aggregate-including-embedded-manifest bound check, rather than
    recomputing it from anything else."""
    content_by_path: dict[str, bytes] = {}
    aggregate_declared_size = 0
    for entry in entries:
        declared_size = _blob_declared_size(entry.blob_sha, repo_root=repo_root, runner=runner)
        aggregate_declared_size += declared_size
        if aggregate_declared_size > archive_safety.MAX_AGGREGATE_UNCOMPRESSED_SIZE:
            raise GitBlobTooLargeError(
                entry.path, declared_size, archive_safety.MAX_AGGREGATE_UNCOMPRESSED_SIZE
            )
        content_by_path[entry.path] = _read_blob(
            entry.blob_sha, repo_root=repo_root, runner=runner
        )
    return content_by_path, aggregate_declared_size


def _compute_alembic_heads(content_by_path: dict[str, bytes]) -> list[str]:
    """Computes Alembic heads for the SELECTED COMMIT's migration set
    without ever executing any of its Python code. `backend/alembic.ini`'s
    presence is still required (matching this command's prior observable
    contract), but only as a presence check — it is never parsed/loaded
    here. Heads themselves come from `alembic_static_metadata
    .compute_static_alembic_heads`, which parses each `backend/alembic/
    versions/*.py` blob with `ast` and extracts only the static
    `revision`/`down_revision` literals — never Alembic's own
    `ScriptDirectory`, which would import (execute) each migration module
    as a side effect of building its revision map. See
    `meyar.ops.alembic_static_metadata`'s module docstring for the full
    rationale; `get_code_alembic_heads` (real `ScriptDirectory`) remains
    unchanged and is still correctly used by `status`/`readiness`/
    `preflight` against the trusted working tree."""
    if "backend/alembic.ini" not in content_by_path:
        raise RequiredReleaseFileMissingError("backend/alembic.ini")
    return compute_static_alembic_heads(content_by_path)


def _build_tar_gz_bytes(members: list[tuple[str, bytes]]) -> bytes:
    """Deterministic tar.gz bytes: members sorted lexically by archive
    name, fixed uid/gid/mode/mtime/uname/gname on every member, a fixed
    gzip header `mtime` and no embedded filename. See module docstring
    "No byte-for-byte reproducibility claim" — this normalizes everything
    EXCEPT the manifest member's own content, which legitimately differs
    build-to-build because `built_at` is a real timestamp."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0, compresslevel=9, filename="") as gz:
        with tarfile.open(fileobj=gz, mode="w|") as tar:
            for name, content in sorted(members, key=lambda m: m[0]):
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                info.mtime = _FIXED_MEMBER_MTIME
                info.mode = _FIXED_MEMBER_MODE
                info.type = tarfile.REGTYPE
                info.uid = _FIXED_UID
                info.gid = _FIXED_GID
                info.uname = ""
                info.gname = ""
                tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


@dataclass(frozen=True)
class _CreatedFile:
    dir_fd: int
    basename: str
    stat: os.stat_result
    path: Path


def _create_exclusive(
    *, dir_fd: int, basename: str, data: bytes, output_dir: Path
) -> _CreatedFile:
    """`O_CREAT | O_EXCL` create, with explicit raw-fd ownership discipline:
    this function owns the raw `fd` returned by `os.open()` until
    ownership is explicitly transferred to the `os.fdopen()`-wrapped file
    object. `os.fdopen()` does not itself guarantee closing `fd` on its
    own failure, so a failure between `os.open()` and a successful
    `os.fdopen()` — including `os.fstat(fd)` itself raising — closes the
    still-owned raw `fd` directly here before any cleanup/re-raise, never
    leaking it. Cleanup of the just-created path is always identity-
    checked via `(st_dev, st_ino)` (`_cleanup_created_file`, which already
    suppresses its own `OSError`s), so it can never delete a pre-existing
    file or one a concurrent actor has since swapped in at the same
    basename, and it can never mask the original exception being
    propagated."""
    try:
        fd = os.open(basename, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644, dir_fd=dir_fd)
    except FileExistsError:
        raise OutputTargetExistsError(output_dir / basename) from None

    try:
        created_stat = os.fstat(fd)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.close(fd)
        raise OutputIdentityUnavailableError(output_dir / basename) from exc

    try:
        fh = os.fdopen(fd, "wb")
    except BaseException:
        # Ownership of `fd` never transferred to a file object — this
        # function still owns it and must close it directly. `fd` may or
        # may not already be closed depending on how `os.fdopen()` failed;
        # either way this close is safe, since a double-close's `OSError`
        # is suppressed rather than propagated.
        with contextlib.suppress(OSError):
            os.close(fd)
        _cleanup_created_file(dir_fd=dir_fd, basename=basename, expected_stat=created_stat)
        raise

    # Ownership of `fd` has now transferred to `fh` — from here `fd` must
    # never be closed directly; closing `fh` is the only remaining path.
    try:
        with fh:
            fh.write(data)
    except BaseException:
        _cleanup_created_file(dir_fd=dir_fd, basename=basename, expected_stat=created_stat)
        raise
    return _CreatedFile(
        dir_fd=dir_fd, basename=basename, stat=created_stat, path=output_dir / basename
    )


def _cleanup_created_file(*, dir_fd: int, basename: str, expected_stat: os.stat_result) -> None:
    """Removes exactly the file this invocation created — identity-checked
    via `(st_dev, st_ino)`, never a replacement pathname a concurrent
    actor swapped in afterward, and never a pre-existing file (this is
    only ever called for a basename this invocation itself created)."""
    with contextlib.suppress(OSError):
        current = os.lstat(basename, dir_fd=dir_fd)
        if (current.st_dev, current.st_ino) == (expected_stat.st_dev, expected_stat.st_ino):
            os.unlink(basename, dir_fd=dir_fd)


@dataclass(frozen=True)
class BuildReleaseRequest:
    source_sha: str
    output_dir: Path
    rollback_compatibility: RollbackCompatibility
    model_manifest_reference: str
    model_approval_status: ModelApprovalStatus


def build_release(
    request: BuildReleaseRequest,
    *,
    invocation_cwd: Path | None = None,
    runner: GitRunner | None = None,
    clock: Callable[[], datetime] | None = None,
) -> OpsResult:
    builder = OpsResultBuilder(action="build-release")
    run_git = runner if runner is not None else default_git_runner
    now = clock if clock is not None else lambda: datetime.now(UTC)
    cwd = invocation_cwd if invocation_cwd is not None else Path.cwd()

    try:
        validate_source_sha(request.source_sha)
    except ValueError as exc:
        builder.add(
            component="source_sha_format",
            status=FindingStatus.FAIL,
            code="SOURCE_SHA_INVALID_FORMAT",
            message=safe_exception_text(exc),
        )
        return builder.build()
    builder.add(
        component="source_sha_format",
        status=FindingStatus.OK,
        code="SOURCE_SHA_VALID_FORMAT",
        message="source_sha is 40 lowercase hex characters",
    )

    if not request.model_manifest_reference.strip():
        builder.add(
            component="model_manifest_reference",
            status=FindingStatus.FAIL,
            code="MODEL_MANIFEST_REFERENCE_EMPTY",
            message="--model-manifest-reference must not be empty",
        )
        return builder.build()

    try:
        repo_root = _discover_repo_root(invocation_cwd=cwd, runner=run_git)
    except GitCommandError as exc:
        builder.add(
            component="repo_root_discovery",
            status=FindingStatus.FAIL,
            code="GIT_REPO_ROOT_NOT_FOUND",
            message=safe_exception_text(exc),
        )
        return builder.build()
    builder.add(
        component="repo_root_discovery",
        status=FindingStatus.OK,
        code="GIT_REPO_ROOT_FOUND",
        message="resolved local Git repository root",
    )

    try:
        _verify_commit_exists(request.source_sha, repo_root=repo_root, runner=run_git)
    except GitCommitNotFoundError as exc:
        builder.add(
            component="commit_exists",
            status=FindingStatus.FAIL,
            code="SOURCE_COMMIT_NOT_FOUND",
            message=safe_exception_text(exc),
        )
        return builder.build()
    except GitCommandError as exc:
        builder.add(
            component="commit_exists",
            status=FindingStatus.FAIL,
            code="GIT_COMMAND_FAILED",
            message=safe_exception_text(exc),
        )
        return builder.build()
    builder.add(
        component="commit_exists",
        status=FindingStatus.OK,
        code="SOURCE_COMMIT_FOUND",
        message="source_sha exists as a commit in the local repository",
    )

    output_dir = request.output_dir
    if not output_dir.is_dir():
        builder.add(
            component="output_dir",
            status=FindingStatus.FAIL,
            code="OUTPUT_DIR_INVALID",
            message=_bounded(f"--output-dir does not exist or is not a directory: {output_dir}"),
        )
        return builder.build()
    builder.add(
        component="output_dir",
        status=FindingStatus.OK,
        code="OUTPUT_DIR_VALID",
        message="--output-dir exists and is a directory",
    )

    try:
        entries = _list_allowlisted_tree_entries(
            request.source_sha, repo_root=repo_root, runner=run_git
        )
    except UnsafeGitTreeEntryError as exc:
        builder.add(
            component="tree_entries_safety",
            status=FindingStatus.FAIL,
            code="UNSAFE_GIT_TREE_ENTRY",
            message=safe_exception_text(exc),
        )
        return builder.build()
    except GitCommandError as exc:
        builder.add(
            component="tree_entries_safety",
            status=FindingStatus.FAIL,
            code="GIT_LS_TREE_FAILED",
            message=safe_exception_text(exc),
        )
        return builder.build()
    if not entries:
        builder.add(
            component="tree_entries_safety",
            status=FindingStatus.FAIL,
            code="NO_ALLOWLISTED_ENTRIES_FOUND",
            message="selected commit has no tracked files under the allowlisted paths",
        )
        return builder.build()
    builder.add(
        component="tree_entries_safety",
        status=FindingStatus.OK,
        code="TREE_ENTRIES_SAFE",
        message=f"{len(entries)} allowlisted regular-file entr"
        f"{'y' if len(entries) == 1 else 'ies'} found, no unsafe shapes",
    )

    # Resource-bound precheck 1/3 (PR #56 final resource-safety corrective
    # pass): the final archive member count is already fully knowable from
    # the tree listing alone (every entry becomes one member, plus the one
    # mandatory embedded manifest) — checked here, before fetching a single
    # blob's content, so a selected commit whose allowlisted tree alone
    # already exceeds the verifier's own accepted member count can never
    # cause thousands of blob fetches for an artifact that would be
    # rejected by verify-release regardless.
    final_member_count = len(entries) + 1  # + mandatory embedded release_manifest.json
    if final_member_count > archive_safety.MAX_ARCHIVE_MEMBER_COUNT:
        builder.add(
            component="member_count_bound",
            status=FindingStatus.FAIL,
            code="MEMBER_COUNT_EXCEEDS_BOUND",
            message=_bounded(
                f"final archive member count {final_member_count} (selected source entries "
                "plus the mandatory embedded manifest) exceeds the accepted bound of "
                f"{archive_safety.MAX_ARCHIVE_MEMBER_COUNT}"
            ),
        )
        return builder.build()
    builder.add(
        component="member_count_bound",
        status=FindingStatus.OK,
        code="MEMBER_COUNT_WITHIN_BOUND",
        message=f"final archive member count {final_member_count} is within the accepted bound",
    )

    try:
        content_by_path, source_aggregate_declared_size = _fetch_all_blobs(
            entries, repo_root=repo_root, runner=run_git
        )
    except GitBlobTooLargeError as exc:
        builder.add(
            component="blob_content_fetch",
            status=FindingStatus.FAIL,
            code="GIT_BLOB_TOO_LARGE",
            message=safe_exception_text(exc),
        )
        return builder.build()
    except GitCommandError as exc:
        builder.add(
            component="blob_content_fetch",
            status=FindingStatus.FAIL,
            code="GIT_CAT_FILE_FAILED",
            message=safe_exception_text(exc),
        )
        return builder.build()
    builder.add(
        component="blob_content_fetch",
        status=FindingStatus.OK,
        code="BLOB_CONTENT_FETCHED",
        message=f"fetched {len(content_by_path)} blob(s) from the selected commit",
    )

    pyproject_bytes = content_by_path.get("backend/pyproject.toml")
    uv_lock_bytes = content_by_path.get("backend/uv.lock")
    if pyproject_bytes is None:
        builder.add(
            component="metadata_extraction",
            status=FindingStatus.FAIL,
            code="REQUIRED_RELEASE_FILE_MISSING",
            message="selected commit has no 'backend/pyproject.toml'",
        )
        return builder.build()
    if uv_lock_bytes is None:
        builder.add(
            component="metadata_extraction",
            status=FindingStatus.FAIL,
            code="REQUIRED_RELEASE_FILE_MISSING",
            message="selected commit has no 'backend/uv.lock'",
        )
        return builder.build()

    try:
        release_version, required_python_version = parse_project_metadata(
            pyproject_bytes.decode("utf-8")
        )
    except (ValueError, UnicodeDecodeError) as exc:
        builder.add(
            component="metadata_extraction",
            status=FindingStatus.FAIL,
            code="PYPROJECT_METADATA_INVALID",
            message=safe_exception_text(exc),
        )
        return builder.build()
    uv_lock_sha256 = hashlib.sha256(uv_lock_bytes).hexdigest()
    builder.add(
        component="metadata_extraction",
        status=FindingStatus.OK,
        code="METADATA_EXTRACTED",
        message=f"release_version={release_version} from the selected commit's pyproject.toml",
    )

    # `release_version` is attacker-controlled content for any commit an
    # operator selects (it comes straight from that commit's
    # pyproject.toml) — validated here as a safe filesystem path component
    # BEFORE it can reach `release_id`/output filenames/archive member
    # roots, never silently normalized into a different value.
    try:
        _validate_filesystem_safe_component(release_version, field="release_version")
    except ReleaseIdentityUnsafeError as exc:
        builder.add(
            component="release_identity_safety",
            status=FindingStatus.FAIL,
            code="RELEASE_VERSION_UNSAFE",
            message=safe_exception_text(exc),
        )
        return builder.build()

    try:
        alembic_heads = _compute_alembic_heads(content_by_path)
    except RequiredReleaseFileMissingError as exc:
        builder.add(
            component="alembic_heads",
            status=FindingStatus.FAIL,
            code="REQUIRED_RELEASE_FILE_MISSING",
            message=safe_exception_text(exc),
        )
        return builder.build()
    except AlembicStaticMetadataError as exc:
        builder.add(
            component="alembic_heads",
            status=FindingStatus.FAIL,
            code="ALEMBIC_STATIC_METADATA_INVALID",
            message=safe_exception_text(exc),
        )
        return builder.build()
    builder.add(
        component="alembic_heads",
        status=FindingStatus.OK,
        code="ALEMBIC_HEADS_COMPUTED",
        message=f"{len(alembic_heads)} Alembic head(s) from the selected commit's migration tree",
    )

    release_id = compute_release_id(release_version=release_version, source_sha=request.source_sha)
    try:
        _validate_filesystem_safe_component(release_id, field="release_id")
    except ReleaseIdentityUnsafeError as exc:
        builder.add(
            component="release_identity_safety",
            status=FindingStatus.FAIL,
            code="RELEASE_ID_UNSAFE",
            message=safe_exception_text(exc),
        )
        return builder.build()
    builder.add(
        component="release_identity_safety",
        status=FindingStatus.OK,
        code="RELEASE_IDENTITY_SAFE",
        message="release_version and release_id are safe filesystem path components",
    )

    # Resource-bound precheck 2/3: with release_id now known, every final
    # archive member name (the release-id-prefixed source paths plus the
    # mandatory embedded manifest name) is fully constructible — validated
    # here, before tar construction, using the FINAL name including the
    # release-id prefix, not just the raw Git path.
    final_member_names = [f"{release_id}/{entry.path}" for entry in entries]
    final_member_names.append(f"{release_id}/{INTERNAL_MANIFEST_MEMBER_NAME}")
    too_long_names = [
        name for name in final_member_names if len(name) > archive_safety.MAX_MEMBER_NAME_LENGTH
    ]
    if too_long_names:
        builder.add(
            component="member_name_length_bound",
            status=FindingStatus.FAIL,
            code="MEMBER_NAME_LENGTH_EXCEEDS_BOUND",
            message=_bounded(
                f"{len(too_long_names)} final archive member name(s) exceed the accepted "
                f"bound of {archive_safety.MAX_MEMBER_NAME_LENGTH} characters, e.g. "
                f"{too_long_names[0]!r} ({len(too_long_names[0])} characters)"
            ),
        )
        return builder.build()
    builder.add(
        component="member_name_length_bound",
        status=FindingStatus.OK,
        code="MEMBER_NAME_LENGTH_WITHIN_BOUND",
        message=f"all {len(final_member_names)} final archive member names are within "
        "the accepted bound",
    )

    artifact_name = f"{release_id}.tar.gz"
    manifest_name = f"{release_id}.release-manifest.json"
    sums_name = "SHA256SUMS"
    for name in (artifact_name, manifest_name, sums_name):
        if (output_dir / name).exists():
            builder.add(
                component="output_targets",
                status=FindingStatus.FAIL,
                code="OUTPUT_TARGET_EXISTS",
                message=_bounded(f"output target already exists: {output_dir / name}"),
            )
            return builder.build()
    builder.add(
        component="output_targets",
        status=FindingStatus.OK,
        code="OUTPUT_TARGETS_AVAILABLE",
        message="none of the three release-bundle output targets already exist",
    )

    try:
        manifest = ReleaseManifest(
            release_version=release_version,
            release_id=release_id,
            source_sha=request.source_sha,
            built_at=now(),
            required_python_version=required_python_version,
            uv_lock_sha256=uv_lock_sha256,
            alembic_heads=alembic_heads,
            rollback_compatibility=request.rollback_compatibility,
            model_manifest=ModelManifestReference(
                reference=request.model_manifest_reference,
                status=request.model_approval_status,
            ),
            artifact_format=ARTIFACT_FORMAT,
            artifact_format_version=ARTIFACT_FORMAT_VERSION,
        )
    except ValueError as exc:
        builder.add(
            component="release_manifest_construction",
            status=FindingStatus.FAIL,
            code="RELEASE_MANIFEST_INVALID",
            message=safe_exception_text(exc),
        )
        return builder.build()
    builder.add(
        component="release_manifest_construction",
        status=FindingStatus.OK,
        code="RELEASE_MANIFEST_BUILT",
        message=f"release_id={release_id}",
    )

    manifest_bytes = manifest.model_dump_json().encode("utf-8")

    # Resource-bound precheck 3/3: the final aggregate uncompressed size is
    # the selected source blobs (already Git-declared-size-checked as they
    # were fetched — reused here rather than recomputed) PLUS the embedded
    # manifest's own bytes, now that the manifest is fully built. Checked
    # before tar construction — never only the source blobs alone.
    aggregate_size_with_manifest = source_aggregate_declared_size + len(manifest_bytes)
    if aggregate_size_with_manifest > archive_safety.MAX_AGGREGATE_UNCOMPRESSED_SIZE:
        builder.add(
            component="aggregate_size_bound",
            status=FindingStatus.FAIL,
            code="AGGREGATE_SIZE_EXCEEDS_BOUND",
            message=_bounded(
                f"aggregate uncompressed size {aggregate_size_with_manifest} bytes (selected "
                "source blobs plus the embedded release_manifest.json) exceeds the accepted "
                f"bound of {archive_safety.MAX_AGGREGATE_UNCOMPRESSED_SIZE} bytes"
            ),
        )
        return builder.build()
    builder.add(
        component="aggregate_size_bound",
        status=FindingStatus.OK,
        code="AGGREGATE_SIZE_WITHIN_BOUND",
        message=f"aggregate uncompressed size {aggregate_size_with_manifest} bytes is within "
        "the accepted bound",
    )

    tar_members = [(f"{release_id}/{path}", content) for path, content in content_by_path.items()]
    tar_members.append((f"{release_id}/{INTERNAL_MANIFEST_MEMBER_NAME}", manifest_bytes))
    artifact_bytes = _build_tar_gz_bytes(tar_members)

    created: list[_CreatedFile] = []
    try:
        output_dir_fd = os.open(str(output_dir), os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        builder.add(
            component="output_bundle_write",
            status=FindingStatus.FAIL,
            code="OUTPUT_DIR_UNOPENABLE",
            message=safe_exception_text(exc),
        )
        return builder.build()

    def _cleanup_all() -> None:
        for cf in created:
            _cleanup_created_file(dir_fd=cf.dir_fd, basename=cf.basename, expected_stat=cf.stat)

    try:
        artifact_file = _create_exclusive(
            dir_fd=output_dir_fd,
            basename=artifact_name,
            data=artifact_bytes,
            output_dir=output_dir,
        )
        created.append(artifact_file)

        try:
            violations = inspect_archive_members(artifact_file.path, expected_root=release_id)
        except ArchiveBoundExceededError as exc:
            raise UnsafeBuiltArchiveError(
                safe_exception_text(exc), code="ARCHIVE_RESOURCE_BOUND_EXCEEDED"
            ) from exc
        if violations:
            summary = "; ".join(f"{v.member_name}: {v.reason}" for v in violations[:5])
            raise UnsafeBuiltArchiveError(
                _bounded(f"{len(violations)} unsafe archive member(s): {summary}"),
                code="UNSAFE_ARCHIVE_MEMBER",
            )
        builder.add(
            component="archive_self_check",
            status=FindingStatus.OK,
            code="ARCHIVE_SAFE",
            message="built artifact passed the same safety inspection verify-release performs",
        )

        manifest_file = _create_exclusive(
            dir_fd=output_dir_fd,
            basename=manifest_name,
            data=manifest_bytes,
            output_dir=output_dir,
        )
        created.append(manifest_file)

        artifact_checksum = hashlib.sha256(artifact_bytes).hexdigest()
        manifest_checksum = hashlib.sha256(manifest_bytes).hexdigest()
        sums_bytes = (
            f"{artifact_checksum}  {artifact_name}\n{manifest_checksum}  {manifest_name}\n"
        ).encode()
        sums_file = _create_exclusive(
            dir_fd=output_dir_fd, basename=sums_name, data=sums_bytes, output_dir=output_dir
        )
        created.append(sums_file)
    except OutputTargetExistsError as exc:
        _cleanup_all()
        builder.add(
            component="output_bundle_write",
            status=FindingStatus.FAIL,
            code="OUTPUT_TARGET_EXISTS",
            message=safe_exception_text(exc),
        )
        return builder.build()
    except UnsafeBuiltArchiveError as exc:
        _cleanup_all()
        builder.add(
            component="archive_self_check",
            status=FindingStatus.FAIL,
            code=exc.code,
            message=str(exc),
        )
        return builder.build()
    except OutputIdentityUnavailableError as exc:
        # Cleans up only files this invocation ALREADY identity-checked
        # and tracked in `created` — the file that triggered this
        # exception was never appended to `created` (its identity could
        # never be established) and is deliberately left untouched.
        _cleanup_all()
        builder.add(
            component="output_bundle_write",
            status=FindingStatus.FAIL,
            code="OUTPUT_IDENTITY_UNAVAILABLE",
            message=safe_exception_text(exc),
        )
        return builder.build()
    except OSError as exc:
        _cleanup_all()
        builder.add(
            component="output_bundle_write",
            status=FindingStatus.FAIL,
            code="OUTPUT_WRITE_FAILED",
            message=safe_exception_text(exc),
        )
        return builder.build()
    finally:
        with contextlib.suppress(OSError):
            os.close(output_dir_fd)

    builder.add(
        component="build_complete",
        status=FindingStatus.OK,
        code="RELEASE_BUILT",
        message=f"release_id={release_id} written to {output_dir}",
    )
    return builder.build()
