"""Release-artifact archive safety inspection. Reading member metadata
via `tarfile.getmembers()` never extracts anything to disk — this module
never calls `TarFile.extract`/`extractall`, in this PR or any future one
that reuses it; extraction itself remains out of scope for #35 PR1.

Every unsafe archive shape called out by the issue is rejected here:
absolute paths, `..` traversal, symlinks, hard links, device/special
files, and paths escaping the expected single top-level artifact root.
"""

from __future__ import annotations

import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class ArchiveSafetyViolation:
    member_name: str
    reason: str


class UnsafeArchiveError(ValueError):
    def __init__(self, violations: list[ArchiveSafetyViolation]) -> None:
        self.violations = violations
        summary = "; ".join(f"{v.member_name}: {v.reason}" for v in violations[:10])
        super().__init__(f"unsafe archive ({len(violations)} violation(s)): {summary}")


def inspect_archive_members(
    archive_path: Path, *, expected_root: str
) -> list[ArchiveSafetyViolation]:
    """Never raises for an unsafe archive — returns the full list of
    violations found so callers can report every offending member, not
    just the first. Raises only for a genuinely unreadable/corrupt
    archive file (a distinct, infrastructure-level failure)."""
    violations: list[ArchiveSafetyViolation] = []
    with tarfile.open(archive_path, mode="r:*") as archive:
        for member in archive.getmembers():
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
