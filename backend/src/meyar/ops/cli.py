"""`meyar-ops` console entry point (`backend/pyproject.toml`
`[project.scripts]`). Every subcommand prints exactly one `OpsResult` as
JSON on stdout and sets a documented exit code — see docs/MEYAR_OPS.md.

Argument parsing errors are argparse's own `SystemExit(2)`, which is
already `OpsExitCode.INVALID_INVOCATION` — no special-casing needed. Any
other uncaught exception is caught once at the top level and reported as
`OpsExitCode.INFRASTRUCTURE_FAILURE`, never a raw traceback that could
carry an unredacted secret/path."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from meyar.ops.preflight import run_preflight
from meyar.ops.readiness import run_readiness
from meyar.ops.redact import safe_exception_text
from meyar.ops.result import (
    FindingStatus,
    OpsExitCode,
    OpsResult,
    build_single_finding_result,
    exit_code_for,
)
from meyar.ops.status import run_status
from meyar.ops.verify_release import verify_release


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meyar-ops")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("preflight", help="Non-destructive host/runtime checks before deployment.")

    status_parser = sub.add_parser("status", help="Read-only operational metadata.")
    status_parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Path to an installed release manifest JSON file, if any.",
    )

    sub.add_parser("readiness", help="Component-level local operator readiness.")

    verify_parser = sub.add_parser(
        "verify-release", help="Verify a release manifest/artifact — never installs/extracts."
    )
    verify_parser.add_argument("--manifest", type=Path, required=True)
    verify_parser.add_argument("--sha256sums", type=Path, required=True)
    verify_parser.add_argument("--artifact", type=Path, required=True)
    verify_parser.add_argument("--expected-release-id", type=str, default=None)

    return parser


def _run_command(args: argparse.Namespace) -> OpsResult:
    if args.command == "preflight":
        return run_preflight()
    if args.command == "status":
        return asyncio.run(run_status(manifest_path=args.manifest))
    if args.command == "readiness":
        return asyncio.run(run_readiness())
    if args.command == "verify-release":
        return verify_release(
            manifest_path=args.manifest,
            sha256sums_path=args.sha256sums,
            artifact_path=args.artifact,
            expected_release_id=args.expected_release_id,
        )
    raise AssertionError(f"unreachable: unknown command {args.command!r}")  # argparse enforces this


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)  # raises SystemExit(2) on invalid invocation

    try:
        result = _run_command(args)
    except Exception as exc:  # noqa: BLE001 - single top-level CLI safety boundary
        result = build_single_finding_result(
            action=args.command,
            component=args.command,
            status=FindingStatus.FAIL,
            code="UNCAUGHT_EXCEPTION",
            message=safe_exception_text(exc),
        )
        print(result.model_dump_json())
        raise SystemExit(OpsExitCode.INFRASTRUCTURE_FAILURE) from None

    print(result.model_dump_json())
    raise SystemExit(exit_code_for(result))


if __name__ == "__main__":
    main()
