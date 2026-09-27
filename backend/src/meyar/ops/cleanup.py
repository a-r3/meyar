"""Conservative cleanup of abandoned PR4 activation temporary symlinks only."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from meyar.ops.offline_host import InstallFailure, _current_release, _operation_lock
from meyar.ops.result import FindingStatus, OpsResult, OpsResultBuilder

_TEMP = re.compile(r"\.next-current-[0-9a-f]{32}\Z")
_TARGET = re.compile(r"activations/g-[0-9a-f]{32}/current\Z")


def run_cleanup(root: Path, *, apply: bool = False) -> OpsResult:
    builder = OpsResultBuilder(action="cleanup")
    try:
        with _operation_lock(root):
            # A malformed current pointer makes every cleanup decision unsafe.
            _current_release(root)
            active_target = (
                os.readlink(root / "current") if os.path.lexists(root / "current") else None
            )
            candidates: list[tuple[Path, int, int]] = []
            unknown = 0
            for path in root.iterdir():
                if not path.name.startswith(".next-current-"):
                    if path.name.startswith(".") and path.name != ".meyar-ops.lock":
                        unknown += 1
                    continue
                info = path.lstat()
                if (
                    _TEMP.fullmatch(path.name) is None
                    or not stat.S_ISLNK(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or info.st_nlink != 1
                ):
                    unknown += 1
                    continue
                target = os.readlink(path)
                if _TARGET.fullmatch(target) is None or target == active_target:
                    unknown += 1
                    continue
                generation = root / target
                if (
                    not generation.is_symlink()
                    or os.readlink(generation).startswith("../../releases/") is False
                ):
                    unknown += 1
                    continue
                candidates.append((path, info.st_dev, info.st_ino))
            # Inspect only known staging parents, one level deep. Never scan
            # candidate storage, logs, or arbitrary subtrees.
            for relative in (
                "releases",
                "shared/backups",
                "shared/restores",
                "shared/updates",
                "shared/diagnostics",
                "shared/reboots",
                "shared/acceptance",
                "shared/edges",
                "shared/smoke",
                "shared/ai/runtimes",
                "shared/ai/staging",
            ):
                parent = root / relative
                if not os.path.lexists(parent):
                    continue
                metadata = parent.lstat()
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                    or metadata.st_mode & 0o022
                ):
                    raise InstallFailure("CLEANUP_LAYOUT_UNSAFE")
                unknown += sum(entry.name.startswith(".") for entry in parent.iterdir())
            if unknown:
                builder.add(
                    component="unknown",
                    status=FindingStatus.WARN,
                    code="CLEANUP_UNKNOWN_LEFT_UNTOUCHED",
                    message=f"{unknown} ambiguous objects left untouched",
                )
            if apply and unknown:
                builder.add(
                    component="cleanup",
                    status=FindingStatus.FAIL,
                    code="CLEANUP_UNSAFE",
                    message="ambiguous objects prevent deletion",
                )
                return builder.build()
            if apply:
                for path, device, inode in candidates:
                    info = path.lstat()
                    if (info.st_dev, info.st_ino) != (device, inode) or not stat.S_ISLNK(
                        info.st_mode
                    ):
                        raise InstallFailure("CLEANUP_STATE_CHANGED")
                    path.unlink()
                descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            builder.add(
                component="cleanup",
                status=FindingStatus.OK,
                code="CLEANUP_APPLIED" if apply else "CLEANUP_DRY_RUN",
                message=f"{len(candidates)} activation temporary links "
                + ("removed" if apply else "eligible"),
            )
    except (InstallFailure, OSError):
        builder.add(
            component="cleanup",
            status=FindingStatus.FAIL,
            code="CLEANUP_REFUSED",
            message="cleanup refused or failed",
        )
    return builder.build()
