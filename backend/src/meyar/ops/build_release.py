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
never touched.
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
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Protocol

from meyar.ops.alembic_introspect import AlembicIntrospectionError, get_code_alembic_heads
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


class OutputDirInvalidError(Exception):
    def __init__(self, output_dir: Path, reason: str) -> None:
        self.output_dir = output_dir
        self.reason = reason
        super().__init__(f"--output-dir invalid ({reason}): {output_dir}")


class OutputTargetExistsError(Exception):
    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"output target already exists: {path}")


def validate_source_sha(value: str) -> None:
    if not _FULL_SHA_RE.fullmatch(value):
        raise ValueError("source_sha must be exactly 40 lowercase hex characters")


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


def _fetch_all_blobs(
    entries: list[GitTreeEntry], *, repo_root: Path, runner: GitRunner
) -> dict[str, bytes]:
    return {
        entry.path: _read_blob(entry.blob_sha, repo_root=repo_root, runner=runner)
        for entry in entries
    }


def _compute_alembic_heads(content_by_path: dict[str, bytes]) -> list[str]:
    """Materializes only the selected commit's `backend/alembic.ini` +
    `backend/alembic/**` blobs into a throwaway temp directory so the
    existing, unmodified `get_code_alembic_heads` (real `alembic.config
    .Config` + `alembic.script.ScriptDirectory`) can compute heads from
    the SELECTED COMMIT's migration tree — never the working tree's.
    `alembic.ini`'s `script_location = %(here)s/alembic` resolves
    relative to the ini file's own directory, so this works regardless of
    where the temp directory lives."""
    with tempfile.TemporaryDirectory(prefix="meyar-build-release-alembic-") as tmp:
        tmp_backend = Path(tmp) / "backend"
        for path, content in content_by_path.items():
            if path == "backend/alembic.ini" or path.startswith("backend/alembic/"):
                dest = tmp_backend / path.removeprefix("backend/")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(content)
        ini_path = tmp_backend / "alembic.ini"
        if not ini_path.is_file():
            raise RequiredReleaseFileMissingError("backend/alembic.ini")
        return get_code_alembic_heads(ini_path)


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
    try:
        fd = os.open(basename, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644, dir_fd=dir_fd)
    except FileExistsError:
        raise OutputTargetExistsError(output_dir / basename) from None
    created_stat = os.fstat(fd)
    try:
        with os.fdopen(fd, "wb") as fh:
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

    try:
        content_by_path = _fetch_all_blobs(entries, repo_root=repo_root, runner=run_git)
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
    except AlembicIntrospectionError as exc:
        builder.add(
            component="alembic_heads",
            status=FindingStatus.FAIL,
            code="ALEMBIC_HEADS_UNAVAILABLE",
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
