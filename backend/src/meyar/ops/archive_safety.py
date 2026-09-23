"""Release-artifact archive safety inspection. Reading member metadata
never extracts anything to disk — this module never calls
`TarFile.extract`/`extractall`, in this PR or any future one that reuses
it; extraction itself remains out of scope for #35 PR1.

Every unsafe archive shape called out by the issue is rejected here:
absolute paths, `..` traversal, symlinks, hard links, device/special
files, and paths escaping the expected single top-level artifact root.

`verify-release` is an operational security boundary that runs against a
downloaded artifact, so member scanning is bounded rather than trusting
the archive's own header count/sizes: members are read one at a time via
`TarFile.next()` (never the eager `TarFile.getmembers()`, which would
parse every header into memory before this function gets a chance to
stop), and scanning aborts the instant any bound below is exceeded —
never after reading further into a hostile archive."""

from __future__ import annotations

import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# Conservative bounds for a release artifact, which is expected to be a
# modest source tree (application code, migrations, a lockfile) — not a
# byte-for-byte contract, just generous headroom that still turns an
# archive-header decompression bomb (huge member count, absurd declared
# sizes) into a fast, truthful FAIL instead of unbounded memory/CPU use.
# `backend/` currently has ~330 tracked files; 5,000 is >10x headroom.
MAX_ARCHIVE_MEMBER_COUNT = 5_000
MAX_MEMBER_NAME_LENGTH = 400
MAX_AGGREGATE_UNCOMPRESSED_SIZE = 1024 * 1024 * 1024  # 1 GiB


@dataclass(frozen=True)
class ArchiveSafetyViolation:
    member_name: str
    reason: str


class UnsafeArchiveError(ValueError):
    def __init__(self, violations: list[ArchiveSafetyViolation]) -> None:
        self.violations = violations
        summary = "; ".join(f"{v.member_name}: {v.reason}" for v in violations[:10])
        super().__init__(f"unsafe archive ({len(violations)} violation(s)): {summary}")


class ArchiveBoundExceededError(ValueError):
    """Raised the instant a resource bound is exceeded while scanning —
    a distinct, truthful outcome from an unsafe-member-shape violation or
    a genuinely corrupt archive, so callers can report it as its own
    code rather than folding it into either."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"release archive exceeded a resource bound: {reason}")


def inspect_archive_members(
    archive_path: Path, *, expected_root: str
) -> list[ArchiveSafetyViolation]:
    """Never raises for an unsafe archive — returns the full list of
    violations found so callers can report every offending member, not
    just the first. Raises `ArchiveBoundExceededError` the moment a
    resource bound above is exceeded, and otherwise raises only for a
    genuinely unreadable/corrupt archive file (a distinct,
    infrastructure-level failure)."""
    violations: list[ArchiveSafetyViolation] = []
    member_count = 0
    aggregate_size = 0
    with tarfile.open(archive_path, mode="r:*") as archive:
        while True:
            member = archive.next()
            if member is None:
                break
            member_count += 1
            if member_count > MAX_ARCHIVE_MEMBER_COUNT:
                raise ArchiveBoundExceededError(
                    f"member count exceeds bound of {MAX_ARCHIVE_MEMBER_COUNT}"
                )
            if len(member.name) > MAX_MEMBER_NAME_LENGTH:
                raise ArchiveBoundExceededError(
                    f"member name length exceeds bound of {MAX_MEMBER_NAME_LENGTH} characters"
                )
            aggregate_size += max(member.size, 0)
            if aggregate_size > MAX_AGGREGATE_UNCOMPRESSED_SIZE:
                raise ArchiveBoundExceededError(
                    "aggregate declared uncompressed member size exceeds bound of "
                    f"{MAX_AGGREGATE_UNCOMPRESSED_SIZE} bytes"
                )
            violations.extend(_check_member(member, expected_root=expected_root))
    return violations


def _check_member(member: tarfile.TarInfo, *, expected_root: str) -> list[ArchiveSafetyViolation]:
    name = member.name
    found: list[ArchiveSafetyViolation] = []

    is_absolute = name.startswith("/") or PurePosixPath(name).is_absolute()
    if is_absolute:
        found.append(ArchiveSafetyViolation(name, "absolute path"))

    if ".." in PurePosixPath(name).parts:
        found.append(ArchiveSafetyViolation(name, "'..' path traversal"))

    if member.issym():
        found.append(ArchiveSafetyViolation(name, "symlink entry"))
    if member.islnk():
        found.append(ArchiveSafetyViolation(name, "hard link entry"))
    if member.isdev():  # character, block, or FIFO/special device entry
        found.append(ArchiveSafetyViolation(name, "device/special file entry"))

    # Containment check is independent of the above — always run so a
    # traversal-free but still-escaping relative path is caught too.
    normalized = PurePosixPath(name)
    prefix = PurePosixPath(expected_root)
    if not is_absolute and normalized != prefix and prefix not in normalized.parents:
        found.append(
            ArchiveSafetyViolation(name, f"path escapes expected artifact root '{expected_root}'")
        )

    return found
