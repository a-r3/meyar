"""Storage-root write probe shared by `readiness`. A readiness probe must
never provision host state — it truthfully reports whether the
*already-provisioned* storage root is writable, never creates the root
or any parent directory. It never overwrites an existing file, and
always cleans up after itself. It never trusts anything derived from
outside this process (the probe filename is always a fresh UUID — there
is no user/candidate-supplied path segment anywhere here). Provisioning
the storage root belongs to a later #35 provisioning step, not
readiness."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from meyar.ops.redact import safe_exception_text


@dataclass(frozen=True)
class StorageProbeResult:
    writable: bool
    message: str


def probe_storage_writable(root: str | Path) -> StorageProbeResult:
    root_path = Path(root)

    if not root_path.exists():
        return StorageProbeResult(
            writable=False, message="storage root does not exist (not yet provisioned)"
        )
    if not root_path.is_dir():
        return StorageProbeResult(
            writable=False, message="storage root exists but is not a directory"
        )

    probe_path = root_path / f".meyar-ops-probe-{uuid.uuid4().hex}"

    # Defense in depth: the generated probe path must resolve inside the
    # configured root even though the name is always process-generated,
    # never derived from external input.
    try:
        resolved_root = root_path.resolve()
        resolved_probe = probe_path.resolve()
    except OSError as exc:
        return StorageProbeResult(
            writable=False, message=f"storage root path not resolvable: {safe_exception_text(exc)}"
        )
    if resolved_root != resolved_probe.parent:
        return StorageProbeResult(
            writable=False, message="storage probe path resolved outside the configured root"
        )

    fd = None
    try:
        # O_EXCL: fails instead of overwriting if the path already exists
        # (it never should, given the fresh UUID, but this makes "never
        # overwrite an existing file" an enforced guarantee, not a hope).
        fd = os.open(probe_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(fd, b"meyar-ops storage write probe\n")
    except OSError as exc:
        return StorageProbeResult(
            writable=False, message=f"storage root not writable: {safe_exception_text(exc)}"
        )
    finally:
        if fd is not None:
            os.close(fd)
        try:
            probe_path.unlink(missing_ok=True)
        except OSError:
            pass  # best-effort cleanup; the write result already determined

    return StorageProbeResult(writable=True, message="storage root is writable")
