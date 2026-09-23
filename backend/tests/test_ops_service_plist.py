"""`meyar-ops service-render` / `service-verify` (issue #35 PR2). Covers
plist generation, the exact narrow key set, path/label/user validation,
no-overwrite/no-partial-write output behavior, bounded input reads, and
malformed/tampered plist rejection. See docs/MEYAR_OPS.md."""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from meyar.ops.result import FindingStatus
from meyar.ops.service_plist import (
    APPLICATION_MODULE,
    REQUIRED_HOST,
    REQUIRED_PLIST_KEYS,
    ServiceRenderOutputExistsError,
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


def _valid_spec(**overrides: object) -> ServiceSpec:
    base = {
        "label": "meyar.application",
        "user_name": "meyar-svc",
        "working_directory": "/opt/meyar/current/backend",
        "executable": "/opt/meyar/venv/bin/python3",
        "port": 8000,
        "stdout_path": "/var/log/meyar/app.out.log",
        "stderr_path": "/var/log/meyar/app.err.log",
    }
    base.update(overrides)
    return ServiceSpec(**base)  # type: ignore[arg-type]


# --- 1. plist generation via plistlib / round-trip -------------------------


def test_render_service_plist_round_trips_through_plistlib() -> None:
    data = render_service_plist(_valid_spec())
    payload = plistlib.loads(data)
    assert payload["Label"] == "meyar.application"
    assert payload["ProgramArguments"][0] == "/opt/meyar/venv/bin/python3"


def test_render_service_plist_is_deterministic() -> None:
    spec = _valid_spec()
    assert render_service_plist(spec) == render_service_plist(spec)


# --- 2. exact generated key set --------------------------------------------


def test_render_service_plist_has_exactly_the_required_key_set() -> None:
    payload = plistlib.loads(render_service_plist(_valid_spec()))
    assert set(payload.keys()) == set(REQUIRED_PLIST_KEYS)


# --- 3. absolute executable path requirement --------------------------------


def test_relative_executable_is_rejected() -> None:
    with pytest.raises(ValueError, match="absolute"):
        render_service_plist(_valid_spec(executable="python3"))


def test_render_service_plist_never_uses_program_key_or_bare_uv() -> None:
    payload = plistlib.loads(render_service_plist(_valid_spec()))
    assert "Program" not in payload
    args = payload["ProgramArguments"]
    assert args[0].startswith("/")
    assert "uv" not in Path(args[0]).name


# --- 4. UserName required / root rejected -----------------------------------


def test_root_user_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="root"):
        render_service_plist(_valid_spec(user_name="root"))


def test_empty_user_name_is_rejected() -> None:
    with pytest.raises(ValueError):
        validate_user_name("")


def test_user_name_with_whitespace_is_rejected() -> None:
    with pytest.raises(ValueError):
        validate_user_name("meyar svc")


# --- 5. loopback host pinned to 127.0.0.1 -----------------------------------


def test_program_arguments_always_use_required_host() -> None:
    payload = plistlib.loads(render_service_plist(_valid_spec()))
    args = payload["ProgramArguments"]
    assert args[4] == "--host"
    assert args[5] == REQUIRED_HOST == "127.0.0.1"


def test_program_arguments_invoke_expected_application_module() -> None:
    payload = plistlib.loads(render_service_plist(_valid_spec()))
    args = payload["ProgramArguments"]
    assert args[1:4] == ["-m", "uvicorn", APPLICATION_MODULE]


# --- 6. port validation ------------------------------------------------------


@pytest.mark.parametrize("port", [0, -1, 65536, 100000])
def test_out_of_range_port_is_rejected(port: int) -> None:
    with pytest.raises(ValueError):
        validate_port(port)


@pytest.mark.parametrize("port", [1, 8000, 65535])
def test_in_range_port_is_accepted(port: int) -> None:
    validate_port(port)  # must not raise


def test_port_is_embedded_as_string_argv_entry() -> None:
    payload = plistlib.loads(render_service_plist(_valid_spec(port=8123)))
    args = payload["ProgramArguments"]
    assert args[6] == "--port"
    assert args[7] == "8123"


# --- 7. no EnvironmentVariables ----------------------------------------------


def test_render_service_plist_never_includes_environment_variables() -> None:
    payload = plistlib.loads(render_service_plist(_valid_spec()))
    assert "EnvironmentVariables" not in payload


# --- 8. no explicit RunAtLoad -------------------------------------------------


def test_render_service_plist_never_includes_run_at_load() -> None:
    payload = plistlib.loads(render_service_plist(_valid_spec()))
    assert "RunAtLoad" not in payload
    assert "ThrottleInterval" not in payload


# --- 9. KeepAlive.SuccessfulExit == false -------------------------------------


def test_render_service_plist_keep_alive_is_structured_successful_exit_false() -> None:
    payload = plistlib.loads(render_service_plist(_valid_spec()))
    assert payload["KeepAlive"] == {"SuccessfulExit": False}


# --- 10. absolute WorkingDirectory/log paths ----------------------------------


@pytest.mark.parametrize(
    "field",
    ["working_directory", "stdout_path", "stderr_path"],
)
def test_relative_path_fields_are_rejected(field: str) -> None:
    with pytest.raises(ValueError, match="absolute"):
        render_service_plist(_valid_spec(**{field: "relative/path"}))


# --- 11. lexical traversal/control-character rejection ------------------------


def test_lexical_traversal_is_rejected() -> None:
    with pytest.raises(ValueError, match="traversal"):
        validate_absolute_path("/opt/meyar/../etc/passwd", field_name="X")


def test_control_character_in_path_is_rejected() -> None:
    with pytest.raises(ValueError, match="control"):
        validate_absolute_path("/opt/meyar/\x00evil", field_name="X")


def test_nul_in_path_is_rejected() -> None:
    with pytest.raises(ValueError):
        validate_absolute_path("/opt/meyar\x00", field_name="X")


def test_label_rejects_slash_whitespace_control_and_empty() -> None:
    with pytest.raises(ValueError):
        validate_label("")
    with pytest.raises(ValueError):
        validate_label("meyar/application")
    with pytest.raises(ValueError):
        validate_label("meyar application")
    with pytest.raises(ValueError):
        validate_label("meyar\x01application")


def test_symlink_shaped_component_is_not_blanket_rejected() -> None:
    # No final immutable-release-path contract exists yet — a path that
    # merely *looks* like it could contain a symlinked component (no `..`,
    # absolute, no control chars) must not be rejected lexically.
    validate_absolute_path("/opt/meyar/releases/current/backend", field_name="X")


# --- 12. output no-overwrite / no-partial-write behavior ----------------------


def test_render_to_file_writes_expected_bytes(tmp_path: Path) -> None:
    output = tmp_path / "meyar.plist"
    render_service_plist_to_file(_valid_spec(), output)
    assert output.exists()
    assert plistlib.loads(output.read_bytes())["Label"] == "meyar.application"


def test_render_to_file_refuses_to_overwrite_existing_output(tmp_path: Path) -> None:
    output = tmp_path / "meyar.plist"
    output.write_bytes(b"pre-existing content")
    with pytest.raises(ServiceRenderOutputExistsError):
        render_service_plist_to_file(_valid_spec(), output)
    assert output.read_bytes() == b"pre-existing content"


def test_render_to_file_leaves_no_output_on_invalid_spec(tmp_path: Path) -> None:
    output = tmp_path / "meyar.plist"
    with pytest.raises(ValueError):
        render_service_plist_to_file(_valid_spec(user_name="root"), output)
    assert not output.exists()


def test_run_service_render_reports_output_path_exists_as_fail(tmp_path: Path) -> None:
    output = tmp_path / "meyar.plist"
    output.write_bytes(b"pre-existing content")
    result = run_service_render(_valid_spec(), output)
    assert result.ok is False
    assert result.findings[0].code == "OUTPUT_PATH_EXISTS"
    assert output.read_bytes() == b"pre-existing content"


def test_run_service_render_success_returns_ok_result(tmp_path: Path) -> None:
    output = tmp_path / "meyar.plist"
    result = run_service_render(_valid_spec(), output)
    assert result.ok is True
    assert result.action == "service-render"


def test_run_service_render_invalid_spec_is_fail_and_no_file(tmp_path: Path) -> None:
    output = tmp_path / "meyar.plist"
    result = run_service_render(_valid_spec(label=""), output)
    assert result.ok is False
    assert result.findings[0].code == "SERVICE_SPEC_INVALID"
    assert not output.exists()


# --- 13. bounded plist input read --------------------------------------------


def test_verify_service_plist_rejects_oversized_input(tmp_path: Path, monkeypatch) -> None:
    import meyar.ops.service_plist as service_plist_module

    monkeypatch.setattr(service_plist_module, "_MAX_PLIST_BYTES", 16)
    plist_path = tmp_path / "big.plist"
    plist_path.write_bytes(render_service_plist(_valid_spec()))
    result = verify_service_plist(plist_path)
    assert result.ok is False
    assert result.findings[0].code == "PLIST_TOO_LARGE"


def test_verify_service_plist_reports_unreadable_file(tmp_path: Path) -> None:
    result = verify_service_plist(tmp_path / "does-not-exist.plist")
    assert result.ok is False
    assert result.findings[0].code == "PLIST_UNREADABLE"


# --- 14. malformed plist rejection -------------------------------------------


def test_verify_service_plist_rejects_non_xml_garbage(tmp_path: Path) -> None:
    plist_path = tmp_path / "garbage.plist"
    plist_path.write_bytes(b"this is not a plist at all")
    result = verify_service_plist(plist_path)
    assert result.ok is False
    assert result.findings[0].code == "PLIST_INVALID"


def test_verify_service_plist_rejects_non_dict_top_level(tmp_path: Path) -> None:
    plist_path = tmp_path / "array.plist"
    plist_path.write_bytes(plistlib.dumps(["not", "a", "dict"]))
    result = verify_service_plist(plist_path)
    assert result.ok is False
    assert result.findings[0].code == "PLIST_NOT_A_DICT"


# --- 15. edited/tampered plist rejection -------------------------------------


def _write_plist(tmp_path: Path, payload: dict) -> Path:
    plist_path = tmp_path / "tampered.plist"
    plist_path.write_bytes(plistlib.dumps(payload))
    return plist_path


def _valid_payload() -> dict:
    return plistlib.loads(render_service_plist(_valid_spec()))


def test_verify_service_plist_accepts_a_freshly_rendered_plist(tmp_path: Path) -> None:
    plist_path = tmp_path / "ok.plist"
    plist_path.write_bytes(render_service_plist(_valid_spec()))
    result = verify_service_plist(plist_path, expected_label="meyar.application")
    assert result.ok is True
    codes = {f.component: f.status for f in result.findings}
    assert codes["label"] == FindingStatus.OK
    assert codes["host"] == FindingStatus.OK
    assert codes["port"] == FindingStatus.OK
    assert codes["keep_alive"] == FindingStatus.OK


def test_verify_service_plist_rejects_environment_variables_injection(tmp_path: Path) -> None:
    payload = _valid_payload()
    payload["EnvironmentVariables"] = {"SECRET": "leak"}
    plist_path = _write_plist(tmp_path, payload)
    result = verify_service_plist(plist_path)
    assert result.ok is False
    codes = {f.component: f.code for f in result.findings}
    assert codes["no_environment_variables"] == "ENVIRONMENT_VARIABLES_PRESENT"
    assert codes["allowed_keys"] == "UNSUPPORTED_KEY"


def test_verify_service_plist_rejects_run_at_load_injection(tmp_path: Path) -> None:
    payload = _valid_payload()
    payload["RunAtLoad"] = True
    plist_path = _write_plist(tmp_path, payload)
    result = verify_service_plist(plist_path)
    assert result.ok is False
    codes = {f.component: f.code for f in result.findings}
    assert codes["allowed_keys"] == "UNSUPPORTED_KEY"


def test_verify_service_plist_rejects_root_user_name_tamper(tmp_path: Path) -> None:
    payload = _valid_payload()
    payload["UserName"] = "root"
    plist_path = _write_plist(tmp_path, payload)
    result = verify_service_plist(plist_path)
    assert result.ok is False
    codes = {f.component: f.code for f in result.findings}
    assert codes["user_name"] == "USER_NAME_INVALID"


def test_verify_service_plist_rejects_non_loopback_host_tamper(tmp_path: Path) -> None:
    payload = _valid_payload()
    payload["ProgramArguments"][5] = "0.0.0.0"
    plist_path = _write_plist(tmp_path, payload)
    result = verify_service_plist(plist_path)
    assert result.ok is False
    codes = {f.component: f.code for f in result.findings}
    assert codes["host"] == "HOST_NOT_LOOPBACK"


def test_verify_service_plist_rejects_out_of_range_port_tamper(tmp_path: Path) -> None:
    payload = _valid_payload()
    payload["ProgramArguments"][7] = "70000"
    plist_path = _write_plist(tmp_path, payload)
    result = verify_service_plist(plist_path)
    assert result.ok is False
    codes = {f.component: f.code for f in result.findings}
    assert codes["port"] == "PORT_INVALID"


def test_verify_service_plist_rejects_keep_alive_true_tamper(tmp_path: Path) -> None:
    payload = _valid_payload()
    payload["KeepAlive"] = True
    plist_path = _write_plist(tmp_path, payload)
    result = verify_service_plist(plist_path)
    assert result.ok is False
    codes = {f.component: f.code for f in result.findings}
    assert codes["keep_alive"] == "KEEP_ALIVE_INVALID"


def test_verify_service_plist_rejects_program_string_shell_wrapper(tmp_path: Path) -> None:
    payload = _valid_payload()
    del payload["ProgramArguments"]
    payload["Program"] = "/bin/sh -c 'run something'"
    plist_path = _write_plist(tmp_path, payload)
    result = verify_service_plist(plist_path)
    assert result.ok is False
    codes = {f.component: f.code for f in result.findings}
    assert codes["allowed_keys"] == "UNSUPPORTED_KEY"


def test_verify_service_plist_rejects_shell_executable() -> None:
    from meyar.ops.result import OpsResultBuilder
    from meyar.ops.service_plist import _check_no_shell_wrapper

    builder = OpsResultBuilder(action="service-verify")
    _check_no_shell_wrapper(builder, {}, ["/bin/bash", "-c", "echo hi"])
    result = builder.build()
    assert result.ok is False
    assert result.findings[0].code == "SHELL_WRAPPER_DETECTED"


def test_verify_service_plist_expected_label_mismatch_fails(tmp_path: Path) -> None:
    plist_path = tmp_path / "ok.plist"
    plist_path.write_bytes(render_service_plist(_valid_spec()))
    result = verify_service_plist(plist_path, expected_label="something.else")
    assert result.ok is False
    codes = {f.component: f.code for f in result.findings}
    assert codes["label"] == "LABEL_MISMATCH"


def test_verify_service_plist_missing_required_key_fails(tmp_path: Path) -> None:
    payload = _valid_payload()
    del payload["WorkingDirectory"]
    plist_path = _write_plist(tmp_path, payload)
    result = verify_service_plist(plist_path)
    assert result.ok is False
    codes = {f.component: f.code for f in result.findings}
    assert codes["required_keys"] == "MISSING_REQUIRED_KEY"
    assert codes["working_directory"] == "WORKING_DIRECTORY_MISSING_OR_INVALID_TYPE"


def test_verify_service_plist_malformed_program_arguments_skips_dependent_checks(
    tmp_path: Path,
) -> None:
    payload = _valid_payload()
    payload["ProgramArguments"] = "not-an-array"
    plist_path = _write_plist(tmp_path, payload)
    result = verify_service_plist(plist_path)
    assert result.ok is False
    codes = {f.component: f.status for f in result.findings}
    assert codes["executable_path"] == FindingStatus.SKIPPED
    assert codes["host"] == FindingStatus.SKIPPED
    assert codes["port"] == FindingStatus.SKIPPED
