"""Launch the disposable smoke from the checkout or verified installed release."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ops-install-root", type=Path)
    parser.add_argument("--smoke-id")
    parser.add_argument("--app-label")
    parser.add_argument("--postgres-image-ref")
    args = parser.parse_args()
    evidence_options = (
        args.ops_install_root,
        args.smoke_id,
        args.app_label,
        args.postgres_image_ref,
    )
    if any(value is not None for value in evidence_options) and not all(
        value is not None for value in evidence_options
    ):
        parser.error("all smoke evidence options must be supplied together")
    if args.ops_install_root is None:
        from meyar.smoke_workload import main as smoke_main

        smoke_main()
        return

    root = args.ops_install_root
    if not root.is_absolute() or ".." in root.parts:
        parser.error("installed root must be absolute without traversal")
    # The installed worker verifies the active release, its immutable source,
    # Python and migration head before touching a disposable database.
    python = root / "current/.venv/bin/python"
    backend = root / "current/backend"
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    completed = subprocess.run(
        [
            str(python),
            "-I",
            "-m",
            "meyar.smoke_workload",
            "--ops-install-root",
            str(root),
            "--smoke-id",
            args.smoke_id,
            "--app-label",
            args.app_label,
            "--postgres-image-ref",
            args.postgres_image_ref,
        ],
        cwd=backend,
        env=env,
        check=False,
    )
    raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()
