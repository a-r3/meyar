"""PR4-bound, render-only LaunchDaemon contract on disposable host roots."""

from __future__ import annotations

import os
import plistlib
from pathlib import Path

import pytest

from meyar.ops.offline_host import InstallFailure, verify_active_release
from meyar.ops.service_plist import (
    REQUIRED_PLIST_KEYS,
    ServiceRenderOutputExistsError,
    ServiceRenderOutputPathPrivilegedError,
    ServiceSpec,
    render_service_plist,
    render_service_plist_to_file,
    run_service_render,
    validate_absolute_path,
    validate_label,
    validate_port,
    validate_user_name,
    verify_service_plist,
)


def spec(root: Path, **overrides: object) -> ServiceSpec:
    fields = {
        "label": "meyar.application",
        "user_name": "meyar-svc",
        "install_root": root,
        "port": 8000,
    }
    fields.update(overrides)
    return ServiceSpec(**fields)  # type: ignore[arg-type]


def payload(root: Path) -> dict:
    return plistlib.loads(render_service_plist(spec(root)))


def checked(root: Path, candidate: dict, *, expected_label: str | None = None):
    path = root.parent / "candidate.plist"
    path.write_bytes(plistlib.dumps(candidate))
    return verify_service_plist(path, install_root=root, expected_label=expected_label)


def test_render_uses_verified_active_release_and_shared_paths(ops_host_root: Path) -> None:
    root = ops_host_root
    value = payload(root)
    assert set(value) == REQUIRED_PLIST_KEYS
    assert value["ProgramArguments"] == [
        str(root / "current/.venv/bin/python"),
        "-m",
        "uvicorn",
        "meyar.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
    ]
    assert value["WorkingDirectory"] == str(root / "shared/config")
    assert value["StandardOutPath"] == str(root / "shared/logs/meyar.stdout.log")
    assert value["StandardErrorPath"] == str(root / "shared/logs/meyar.stderr.log")
    assert value["KeepAlive"] == {"SuccessfulExit": False}
    assert "EnvironmentVariables" not in value and "Program" not in value
    assert not (root / "current/backend/.env").exists()
    assert render_service_plist(spec(root)) == render_service_plist(spec(root))
    assert checked(root, value, expected_label="meyar.application").ok


def test_active_python_must_remain_executable(ops_host_root: Path) -> None:
    python = (ops_host_root / "current/.venv/bin/python").resolve()
    output = ops_host_root.parent / "nonexecutable.plist"
    assert verify_active_release(ops_host_root)
    python.chmod(0o444)
    with pytest.raises(InstallFailure, match="ACTIVE_PYTHON_NOT_EXECUTABLE"):
        verify_active_release(ops_host_root)
    assert not run_service_render(spec(ops_host_root), output).ok
    assert not output.exists()
    python.chmod(0o555)
    assert verify_active_release(ops_host_root)
    assert run_service_render(spec(ops_host_root), output).ok
    assert output.exists()


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_invalid_port_rejected(ops_host_root: Path, port: int) -> None:
    with pytest.raises(ValueError):
        render_service_plist(spec(ops_host_root, port=port))


@pytest.mark.parametrize("port", [1, 8000, 65535])
def test_valid_port(ops_host_root: Path, port: int) -> None:
    validate_port(port)
    assert payload(ops_host_root)["ProgramArguments"][7] == "8000"


def test_label_user_and_path_validation(ops_host_root: Path) -> None:
    for label in ("", "meyar/app", "meyar app", "meyar\x01app"):
        with pytest.raises(ValueError):
            validate_label(label)
    for name in ("", "root", "meyar svc"):
        with pytest.raises(ValueError):
            validate_user_name(name)
    for name in ("python", "/opt/../etc", "/tmp/\x00bad"):
        with pytest.raises(ValueError):
            validate_absolute_path(name, field_name="test")
    with pytest.raises(ValueError):
        render_service_plist(spec(ops_host_root, user_name="root"))


def test_render_no_overwrite_and_no_partial_file(ops_host_root: Path) -> None:
    output = ops_host_root.parent / "meyar.plist"
    render_service_plist_to_file(spec(ops_host_root), output)
    original = output.read_bytes()
    with pytest.raises(ServiceRenderOutputExistsError):
        render_service_plist_to_file(spec(ops_host_root), output)
    assert output.read_bytes() == original
    missing = ops_host_root.parent / "invalid.plist"
    assert not run_service_render(spec(ops_host_root, label=""), missing).ok
    assert not missing.exists()


def test_render_rejects_launchdaemons_output(ops_host_root: Path) -> None:
    with pytest.raises(ServiceRenderOutputPathPrivilegedError):
        render_service_plist_to_file(
            spec(ops_host_root), Path("/Library/LaunchDaemons/com.meyar.plist")
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("WorkingDirectory", "/opt/meyar/current/backend"),
        ("StandardOutPath", "/opt/meyar/current/app.log"),
        ("StandardErrorPath", "/var/log/meyar/app.log"),
    ],
)
def test_verify_rejects_noncanonical_paths(ops_host_root: Path, field: str, value: str) -> None:
    candidate = payload(ops_host_root)
    candidate[field] = value
    result = checked(ops_host_root, candidate)
    assert not result.ok
    assert any(f.code == "SERVICE_BINDING_MISMATCH" for f in result.findings)


@pytest.mark.parametrize(
    "executable",
    ["/opt/meyar/venv/bin/python3", "/usr/bin/python3", "/bin/sh", "python", "uv", "uvicorn"],
)
def test_verify_rejects_stale_external_or_bare_executable(
    ops_host_root: Path, executable: str
) -> None:
    candidate = payload(ops_host_root)
    candidate["ProgramArguments"][0] = executable
    result = checked(ops_host_root, candidate)
    assert not result.ok
    assert any(f.code == "SERVICE_BINDING_MISMATCH" for f in result.findings)


def test_verify_rejects_unactivated_release_path(ops_host_root: Path) -> None:
    candidate = payload(ops_host_root)
    candidate["ProgramArguments"][0] = str(
        (ops_host_root / "current").resolve() / ".venv/bin/python"
    )
    assert not checked(ops_host_root, candidate).ok


def test_verify_rejects_nonloopback_and_shell_and_env(ops_host_root: Path) -> None:
    candidate = payload(ops_host_root)
    candidate["ProgramArguments"][5] = "0.0.0.0"
    assert not checked(ops_host_root, candidate).ok
    candidate = payload(ops_host_root)
    candidate["Program"] = "/bin/sh"
    candidate["EnvironmentVariables"] = {"SECRET": "synthetic-secret"}
    result = checked(ops_host_root, candidate)
    assert not result.ok
    assert "synthetic-secret" not in result.model_dump_json()
    assert any(f.code == "ENVIRONMENT_VARIABLES_PRESENT" for f in result.findings)


def test_verify_rejects_missing_malformed_oversized_plist(
    ops_host_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import meyar.ops.service_plist as module

    path = ops_host_root.parent / "candidate.plist"
    assert not verify_service_plist(path, install_root=ops_host_root).ok
    path.write_bytes(b"not a plist")
    assert not verify_service_plist(path, install_root=ops_host_root).ok
    path.write_bytes(render_service_plist(spec(ops_host_root)))
    monkeypatch.setattr(module, "_MAX_PLIST_BYTES", 10)
    result = verify_service_plist(path, install_root=ops_host_root)
    assert not result.ok and result.findings[0].code == "PLIST_TOO_LARGE"


def test_active_pointer_tamper_rejected_without_output(ops_host_root: Path) -> None:
    candidate = payload(ops_host_root)
    (ops_host_root / "current").unlink()
    (ops_host_root / "current").symlink_to("releases/meyar-test+abcdef123456")
    output = ops_host_root.parent / "new.plist"
    assert not run_service_render(spec(ops_host_root), output).ok
    assert not output.exists()
    assert not checked(ops_host_root, candidate).ok


def test_tampered_installed_release_rejected(ops_host_root: Path) -> None:
    release = (ops_host_root / "current").resolve()
    source = release / "backend/src/meyar/main.py"
    source.chmod(0o644)
    source.write_text("# tampered\n")
    assert not run_service_render(spec(ops_host_root), ops_host_root.parent / "out.plist").ok


def test_symlinked_log_destination_rejected(ops_host_root: Path) -> None:
    (ops_host_root / "shared/logs/meyar.stdout.log").symlink_to(
        ops_host_root / "current/backend/src/meyar/main.py"
    )
    assert not run_service_render(spec(ops_host_root), ops_host_root.parent / "out.plist").ok


def test_service_has_no_mutation_calls(
    ops_host_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("service must never invoke launchctl or sudo")

    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    assert run_service_render(spec(ops_host_root), ops_host_root.parent / "rendered.plist").ok


def test_cli_render_and_verify_use_install_root(
    ops_host_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from meyar.ops.cli import main

    output = ops_host_root.parent / "cli.plist"
    with pytest.raises(SystemExit) as rendered:
        main(
            [
                "service-render", "--label", "meyar.application",
                "--user-name", "meyar-svc", "--install-root", str(ops_host_root),
                "--port", "8000", "--output", str(output),
            ]
        )
    assert rendered.value.code == 0
    assert '"ok":true' in capsys.readouterr().out
    with pytest.raises(SystemExit) as verified:
        main(["service-verify", "--plist", str(output), "--install-root", str(ops_host_root)])
    assert verified.value.code == 0


# --- preserved output race and symlink checks ------------------------


def test_render_to_file_cleans_up_partial_output_after_write_failure(
    tmp_path: Path, monkeypatch, ops_host_root: Path
) -> None:
    import meyar.ops.service_plist as service_plist_module

    output = tmp_path / "meyar.plist"
    real_fdopen = os.fdopen

    class _ExplodingFile:
        def __init__(self, fh: object) -> None:
            self._fh = fh

        def __enter__(self) -> _ExplodingFile:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            self._fh.close()  # type: ignore[attr-defined]
            return False

        def write(self, data: bytes) -> int:
            raise OSError("simulated write failure")

    def _fake_fdopen(fd: int, mode: str) -> _ExplodingFile:
        return _ExplodingFile(real_fdopen(fd, mode))

    monkeypatch.setattr(service_plist_module.os, "fdopen", _fake_fdopen)

    with pytest.raises(OSError, match="simulated write failure"):
        render_service_plist_to_file(spec(ops_host_root), output)

    assert not output.exists()


def test_render_to_file_cleanup_does_not_delete_replacement_file(
    tmp_path: Path, monkeypatch, ops_host_root: Path
) -> None:
    """Regression for the TOCTOU cleanup race: cleanup must identity-check
    the pathname (inode) before unlinking, so it never deletes a file a
    concurrent actor swapped into this pathname after this invocation's
    `os.open()` but before its write/close failure completes."""
    import meyar.ops.service_plist as service_plist_module

    output = tmp_path / "meyar.plist"
    sentinel_content = b"sentinel-must-survive-cleanup"
    real_fdopen = os.fdopen

    class _RacingFile:
        def __init__(self, fh: object) -> None:
            self._fh = fh

        def __enter__(self) -> _RacingFile:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            self._fh.close()  # type: ignore[attr-defined]
            return False

        def write(self, data: bytes) -> int:
            output.unlink()
            output.write_bytes(sentinel_content)
            raise OSError("simulated write failure after concurrent pathname replacement")

    def _fake_fdopen(fd: int, mode: str) -> _RacingFile:
        return _RacingFile(real_fdopen(fd, mode))

    monkeypatch.setattr(service_plist_module.os, "fdopen", _fake_fdopen)

    with pytest.raises(OSError, match="simulated write failure after concurrent"):
        render_service_plist_to_file(spec(ops_host_root), output)

    assert output.read_bytes() == sentinel_content


def test_render_to_file_cleans_up_partial_output_after_fdopen_failure(
    tmp_path: Path, monkeypatch, ops_host_root: Path
) -> None:
    """Regression: a failed `os.fdopen()` (not only a failed write) must
    not leave the just-created partial output file behind."""
    import meyar.ops.service_plist as service_plist_module

    output = tmp_path / "meyar.plist"

    def _fake_fdopen(fd: int, mode: str) -> object:
        raise OSError("simulated fdopen failure")

    monkeypatch.setattr(service_plist_module.os, "fdopen", _fake_fdopen)

    with pytest.raises(OSError, match="simulated fdopen failure"):
        render_service_plist_to_file(spec(ops_host_root), output)

    assert not output.exists()


def test_render_to_file_fdopen_failure_cleanup_does_not_delete_replacement_file(
    tmp_path: Path, monkeypatch, ops_host_root: Path
) -> None:
    """Same TOCTOU cleanup-identity regression as
    `test_render_to_file_cleanup_does_not_delete_replacement_file`, but for
    a failure raised by `os.fdopen()` itself rather than by the write."""
    import meyar.ops.service_plist as service_plist_module

    output = tmp_path / "meyar.plist"
    sentinel_content = b"sentinel-must-survive-fdopen-cleanup"

    def _fake_fdopen(fd: int, mode: str) -> object:
        # `fd` (this invocation's own descriptor) is deliberately left open
        # here, matching the realistic race: the concurrent actor swaps the
        # *pathname*, but this invocation has not released its own fd yet.
        # Closing it first would let the kernel immediately recycle the
        # freed inode number for the replacement file on some filesystems
        # (observed on tmpfs), which would make the identity check pass
        # for the wrong reason instead of proving pathname-vs-inode safety.
        output.unlink()
        output.write_bytes(sentinel_content)
        raise OSError("simulated fdopen failure after concurrent pathname replacement")

    monkeypatch.setattr(service_plist_module.os, "fdopen", _fake_fdopen)

    with pytest.raises(OSError, match="simulated fdopen failure after concurrent"):
        render_service_plist_to_file(spec(ops_host_root), output)

    assert output.read_bytes() == sentinel_content


# --- 18. symlink-aware output-parent-directory boundary ---------------------


def test_render_to_file_rejects_parent_symlink_resolving_into_privileged_target(
    tmp_path: Path, monkeypatch, ops_host_root: Path
) -> None:
    """A lexically innocuous output path (e.g.
    `/tmp/staging-link/com.meyar.plist`) must still be rejected when
    `staging-link` is a symlink whose canonical target is the configured
    privileged directory — this is the actual attack the lexical-only
    `abspath()` check in `_reject_privileged_output_path` cannot catch.
    The privileged-directory constant is monkeypatched to a tmp-path
    target so this proves the resolution logic without needing write
    access to the real `/Library/LaunchDaemons`."""
    import meyar.ops.service_plist as service_plist_module

    fake_privileged = tmp_path / "priv" / "LaunchDaemons"
    fake_privileged.mkdir(parents=True)
    monkeypatch.setattr(service_plist_module, "_PRIVILEGED_LAUNCHDAEMONS_DIR", str(fake_privileged))
    staging_link = tmp_path / "staging-link"
    staging_link.symlink_to(fake_privileged, target_is_directory=True)
    output = staging_link / "com.meyar.plist"

    with pytest.raises(ServiceRenderOutputPathPrivilegedError):
        render_service_plist_to_file(spec(ops_host_root), output)

    assert list(fake_privileged.iterdir()) == []


def test_render_to_file_symlinked_privileged_target_never_gets_a_created_file(
    tmp_path: Path, monkeypatch, ops_host_root: Path
) -> None:
    """Explicit proof that no file creation (`os.open`) is ever attempted
    once the resolved parent is identified as the privileged target: the
    resolved directory stays empty, and `os.open` is never called with a
    dir_fd anchored to it."""
    import meyar.ops.service_plist as service_plist_module

    fake_privileged = tmp_path / "priv" / "LaunchDaemons"
    fake_privileged.mkdir(parents=True)
    monkeypatch.setattr(service_plist_module, "_PRIVILEGED_LAUNCHDAEMONS_DIR", str(fake_privileged))
    staging_link = tmp_path / "staging-link"
    staging_link.symlink_to(fake_privileged, target_is_directory=True)
    output = staging_link / "com.meyar.plist"

    real_open = os.open

    def _fail_if_creating_basename(*args: object, **kwargs: object) -> int:
        if args and args[0] == "com.meyar.plist":
            raise AssertionError(
                "os.open must not create a file once the parent resolves to the privileged target"
            )
        return real_open(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(service_plist_module.os, "open", _fail_if_creating_basename)

    with pytest.raises(ServiceRenderOutputPathPrivilegedError):
        render_service_plist_to_file(spec(ops_host_root), output)

    assert list(fake_privileged.iterdir()) == []


def test_render_to_file_rejects_direct_privileged_target_dir_fd_open(
    tmp_path: Path, ops_host_root: Path
) -> None:
    """The low-level helper rejects a privileged parent before ever
    opening a dir-fd to it, proven directly against a tmp-path stand-in
    for the privileged constant (no monkeypatching needed for the
    default-argument override path)."""
    from meyar.ops.service_plist import _open_anchored_output_parent_dir

    privileged = tmp_path / "LaunchDaemons"
    privileged.mkdir()
    output = privileged / "com.meyar.plist"

    with pytest.raises(ServiceRenderOutputPathPrivilegedError):
        _open_anchored_output_parent_dir(output, privileged_dir=str(privileged))


def test_render_to_file_nonprivileged_symlinked_parent_still_succeeds(
    tmp_path: Path, monkeypatch, ops_host_root: Path
) -> None:
    """A parent-directory symlink that resolves somewhere ordinary (not
    the privileged target) must still be allowed — this module does not
    blanket-reject symlinks, only ones resolving into the privileged
    directory."""
    import meyar.ops.service_plist as service_plist_module

    fake_privileged = tmp_path / "priv" / "LaunchDaemons"
    fake_privileged.mkdir(parents=True)
    monkeypatch.setattr(service_plist_module, "_PRIVILEGED_LAUNCHDAEMONS_DIR", str(fake_privileged))
    real_staging = tmp_path / "real-staging"
    real_staging.mkdir()
    staging_link = tmp_path / "staging-link"
    staging_link.symlink_to(real_staging, target_is_directory=True)
    output = staging_link / "com.meyar.plist"

    render_service_plist_to_file(spec(ops_host_root), output)

    assert (real_staging / "com.meyar.plist").exists()
