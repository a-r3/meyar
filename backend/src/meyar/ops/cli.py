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
from typing import NoReturn

from meyar.ops.build_release import BuildReleaseRequest, build_release
from meyar.ops.deployment_ready import run_deployment_ready
from meyar.ops.host_config import verify_host_config
from meyar.ops.model_manifest import ModelApprovalStatus
from meyar.ops.offline_bundle import BundleBuildRequest, build_deployment_bundle
from meyar.ops.preflight import run_preflight
from meyar.ops.readiness import run_readiness
from meyar.ops.redact import safe_exception_text
from meyar.ops.release_manifest import RollbackCompatibility
from meyar.ops.result import (
    FindingStatus,
    OpsExitCode,
    OpsResult,
    build_single_finding_result,
    exit_code_for,
)
from meyar.ops.schema_init import run_schema_init
from meyar.ops.service_lifecycle import run_service_lifecycle
from meyar.ops.service_plist import ServiceSpec, run_service_render, verify_service_plist
from meyar.ops.service_status import run_service_status
from meyar.ops.status import run_status
from meyar.ops.verify_release import verify_release


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse otherwise echoes unrecognized argument values, which may
        # include a mistakenly supplied DB URL or password.
        self.exit(OpsExitCode.INVALID_INVOCATION, f"{self.prog}: invalid arguments\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(prog="meyar-ops")
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

    config_parser = sub.add_parser(
        "config-verify", help="Verify host-local production configuration without exposing values."
    )
    config_parser.add_argument("--install-root", type=Path, required=True)

    schema_parser = sub.add_parser(
        "schema-init", help="Initialize an empty database to the exact active release schema."
    )
    schema_parser.add_argument("--install-root", type=Path, required=True)

    deployment_parser = sub.add_parser(
        "deployment-ready", help="Read-only readiness gate for an installed, started deployment."
    )
    deployment_parser.add_argument("--install-root", type=Path, required=True)
    deployment_parser.add_argument("--label", type=str, required=True)

    render_parser = sub.add_parser(
        "service-render",
        help="Render a macOS LaunchDaemon plist for the MEYAR application process.",
    )
    render_parser.add_argument("--label", type=str, required=True)
    render_parser.add_argument("--user-name", type=str, required=True)
    render_parser.add_argument("--install-root", type=Path, required=True)
    render_parser.add_argument("--port", type=int, required=True)
    render_parser.add_argument("--output", type=Path, required=True)

    verify_plist_parser = sub.add_parser(
        "service-verify", help="Verify a rendered LaunchDaemon plist's shape/security contract."
    )
    verify_plist_parser.add_argument("--plist", type=Path, required=True)
    verify_plist_parser.add_argument("--install-root", type=Path, required=True)
    verify_plist_parser.add_argument("--expected-label", type=str, default=None)

    status_plist_parser = sub.add_parser(
        "service-status",
        help="Read-only probe of the MEYAR LaunchDaemon in the system launchd domain (macOS only).",
    )
    status_plist_parser.add_argument("--label", type=str, required=True)

    for command in ("service-install", "service-start", "service-stop", "service-restart"):
        lifecycle_parser = sub.add_parser(command, help="Privileged system LaunchDaemon lifecycle.")
        lifecycle_parser.add_argument("--label", type=str, required=True)
        lifecycle_parser.add_argument("--user-name", type=str, required=True)
        lifecycle_parser.add_argument("--install-root", type=Path, required=True)
        lifecycle_parser.add_argument("--install-owner-uid", type=int, required=True)
        lifecycle_parser.add_argument("--port", type=int, required=True)

    build_release_parser = sub.add_parser(
        "build-release",
        help="Build an immutable application release artifact from an exact Git commit.",
    )
    build_release_parser.add_argument("--source-sha", type=str, required=True)
    build_release_parser.add_argument("--output-dir", type=Path, required=True)
    build_release_parser.add_argument(
        "--rollback-compatibility",
        type=str,
        required=True,
        choices=[member.value for member in RollbackCompatibility],
    )

    bundle_parser = sub.add_parser(
        "bundle-build", help="Build a hash-bound offline macOS arm64 deployment directory."
    )
    bundle_parser.add_argument("--artifact", type=Path, required=True)
    bundle_parser.add_argument("--manifest", type=Path, required=True)
    bundle_parser.add_argument("--sha256sums", type=Path, required=True)
    bundle_parser.add_argument("--output-dir", type=Path, required=True)
    bundle_parser.add_argument("--runtime-version", type=str, required=True)
    bundle_parser.add_argument("--runtime-executable-sha256", type=str, required=True)
    build_release_parser.add_argument("--model-manifest-reference", type=str, required=True)
    build_release_parser.add_argument(
        "--model-approval-status",
        type=str,
        required=True,
        choices=[member.value for member in ModelApprovalStatus],
    )

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
    if args.command == "config-verify":
        return verify_host_config(args.install_root)
    if args.command == "schema-init":
        return run_schema_init(args.install_root)
    if args.command == "deployment-ready":
        return run_deployment_ready(args.install_root, args.label)
    if args.command == "service-render":
        spec = ServiceSpec(
            label=args.label,
            user_name=args.user_name,
            install_root=args.install_root,
            port=args.port,
        )
        return run_service_render(spec, args.output)
    if args.command == "service-verify":
        return verify_service_plist(
            args.plist, install_root=args.install_root, expected_label=args.expected_label
        )
    if args.command == "service-status":
        return run_service_status(label=args.label)
    if args.command in {"service-install", "service-start", "service-stop", "service-restart"}:
        return run_service_lifecycle(
            args.command,
            ServiceSpec(args.label, args.user_name, args.install_root, args.port),
            args.install_owner_uid,
        )
    if args.command == "build-release":
        return build_release(
            BuildReleaseRequest(
                source_sha=args.source_sha,
                output_dir=args.output_dir,
                rollback_compatibility=RollbackCompatibility(args.rollback_compatibility),
                model_manifest_reference=args.model_manifest_reference,
                model_approval_status=ModelApprovalStatus(args.model_approval_status),
            )
        )
    if args.command == "bundle-build":
        return build_deployment_bundle(
            BundleBuildRequest(
                artifact_path=args.artifact,
                release_manifest_path=args.manifest,
                sha256sums_path=args.sha256sums,
                output_dir=args.output_dir,
                runtime_version=args.runtime_version,
                runtime_executable_sha256=args.runtime_executable_sha256,
            )
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
