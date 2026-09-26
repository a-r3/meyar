"""Synthetic host config and direct production Settings safety gates."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from meyar.config import Settings
from meyar.ops.host_config import verify_host_config


def config(root: Path) -> Path:
    return root / "shared/config/.env"


def change(root: Path, old: str, new: str) -> None:
    path = config(root)
    path.write_text(path.read_text().replace(old, new))


@pytest.fixture
def runtime_service_identity(
    ops_host_root: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[int]:
    install_owner = os.geteuid()
    service_uid = install_owner + 1
    service_gid = (ops_host_root / "shared/config").stat().st_gid
    changed: list[tuple[Path, int]] = []
    for ancestor in ops_host_root.parents:
        metadata = ancestor.stat()
        mode = metadata.st_mode & 0o7777
        if metadata.st_uid == install_owner and not mode & 0o010:
            changed.append((ancestor, mode))
            ancestor.chmod(mode | 0o010)
    monkeypatch.chdir(ops_host_root / "shared/config")
    monkeypatch.setattr(os, "geteuid", lambda: service_uid)
    monkeypatch.setattr(os, "getegid", lambda: service_gid)
    monkeypatch.setattr(os, "getgroups", lambda: [service_gid])
    try:
        yield service_uid
    finally:
        for ancestor, mode in reversed(changed):
            ancestor.chmod(mode)


def test_valid_production_config(ops_host_root: Path) -> None:
    assert ops_host_root.stat().st_uid == os.geteuid()
    assert config(ops_host_root).stat().st_mode & 0o777 == 0o640
    with config(ops_host_root).open("a") as stream:
        stream.write("# operator note with no secret\n")
    result = verify_host_config(ops_host_root)
    assert result.ok
    assert result.findings[0].code == "CONFIG_VERIFIED"


def test_config_group_read_is_available_to_dedicated_runtime_group(ops_host_root: Path) -> None:
    root = ops_host_root
    file_stat = config(root).stat()
    service_gid = (root / "shared/config").stat().st_gid
    assert file_stat.st_uid == root.stat().st_uid
    assert file_stat.st_gid == service_gid
    assert file_stat.st_mode & 0o040
    assert not file_stat.st_mode & 0o037
    for directory in (root, root / "shared", root / "shared/config"):
        metadata = directory.stat()
        assert metadata.st_gid == service_gid
        assert metadata.st_mode & 0o010
        assert not metadata.st_mode & 0o022
    assert verify_host_config(root).ok


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o666])
def test_insecure_file_modes_rejected(ops_host_root: Path, mode: int) -> None:
    config(ops_host_root).chmod(mode)
    result = verify_host_config(ops_host_root)
    assert not result.ok
    assert result.findings[0].code == "CONFIG_PERMISSIONS_UNSAFE"


def test_config_with_untrusted_owner_rejected(
    ops_host_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import meyar.ops.host_config as host_config

    original_fstat = os.fstat

    def untrusted_owner(fd: int) -> SimpleNamespace:
        metadata = original_fstat(fd)
        return SimpleNamespace(
            st_mode=metadata.st_mode,
            st_nlink=metadata.st_nlink,
            st_size=metadata.st_size,
            st_uid=metadata.st_uid + 1,
            st_gid=metadata.st_gid,
        )

    monkeypatch.setattr(host_config.os, "fstat", untrusted_owner)
    result = verify_host_config(ops_host_root)
    assert not result.ok
    assert result.findings[0].code == "CONFIG_PERMISSIONS_UNSAFE"


def test_coherent_layout_owned_by_other_operator_rejected(
    ops_host_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import meyar.ops.host_config as host_config

    invoking_uid = os.geteuid()
    other_uid = invoking_uid + 1
    assert other_uid != invoking_uid
    root = ops_host_root
    # Simulate a complete, internally consistent tree owned by another UID.
    # Its operator-owned ancestors must agree too, so the existing ancestor
    # check cannot accidentally make this regression pass.
    other_owned = {root, root / "shared", root / "shared/config"}
    other_owned.update(parent for parent in root.parents if parent.stat().st_uid == invoking_uid)
    original_stat = Path.stat
    original_fstat = os.fstat

    def stat_as_other_owner(
        path: Path, *args: object, **kwargs: object
    ) -> SimpleNamespace | os.stat_result:
        metadata = original_stat(path, *args, **kwargs)
        if path not in other_owned:
            return metadata
        return SimpleNamespace(
            st_mode=metadata.st_mode,
            st_uid=other_uid,
            st_gid=metadata.st_gid,
        )

    def fstat_as_other_owner(fd: int) -> SimpleNamespace:
        metadata = original_fstat(fd)
        return SimpleNamespace(
            st_mode=metadata.st_mode,
            st_nlink=metadata.st_nlink,
            st_size=metadata.st_size,
            st_uid=other_uid,
            st_gid=metadata.st_gid,
        )

    monkeypatch.setattr(host_config.Path, "stat", stat_as_other_owner)
    monkeypatch.setattr(host_config.os, "fstat", fstat_as_other_owner)
    result = verify_host_config(root)
    assert not result.ok
    assert result.findings[0].code == "HOST_LAYOUT_UNSAFE"


@pytest.mark.parametrize("relative", [".", "shared", "shared/config"])
def test_writable_config_parent_rejected(ops_host_root: Path, relative: str) -> None:
    directory = ops_host_root / relative
    directory.chmod(directory.stat().st_mode | 0o020)
    result = verify_host_config(ops_host_root)
    assert not result.ok
    assert result.findings[0].code == "HOST_LAYOUT_UNSAFE"


def test_writable_install_root_parent_rejected(ops_host_root: Path) -> None:
    parent = ops_host_root.parent
    original_mode = parent.stat().st_mode
    try:
        parent.chmod(original_mode | 0o020)
        result = verify_host_config(ops_host_root)
        assert not result.ok
        assert result.findings[0].code == "HOST_LAYOUT_UNSAFE"
    finally:
        parent.chmod(original_mode)


def test_missing_config(ops_host_root: Path) -> None:
    config(ops_host_root).unlink()
    assert not verify_host_config(ops_host_root).ok


@pytest.mark.parametrize("content", ["MEYAR_ENV=production\nnot valid line @@@\n", "\xff"])
def test_malformed_config(ops_host_root: Path, content: str) -> None:
    config(ops_host_root).write_bytes(content.encode("latin1"))
    assert not verify_host_config(ops_host_root).ok


def test_oversized_config(ops_host_root: Path) -> None:
    with config(ops_host_root).open("ab") as stream:
        stream.write(b"#" + b"a" * 70000)
    assert verify_host_config(ops_host_root).findings[0].code == "CONFIG_TOO_LARGE"


def test_symlink_and_nonregular_config(ops_host_root: Path) -> None:
    path = config(ops_host_root)
    path.rename(path.with_name("saved"))
    path.symlink_to(path.with_name("saved"))
    assert not verify_host_config(ops_host_root).ok
    path.unlink()
    path.mkdir()
    assert not verify_host_config(ops_host_root).ok


def test_symlinked_shared_directory_rejected(ops_host_root: Path) -> None:
    shared = ops_host_root / "shared"
    shared.rename(ops_host_root / "real-shared")
    shared.symlink_to(ops_host_root / "real-shared")
    assert not verify_host_config(ops_host_root).ok


def test_duplicate_critical_key_rejected(ops_host_root: Path) -> None:
    with config(ops_host_root).open("a") as stream:
        stream.write("MEYAR_ENV=development\n")
    assert verify_host_config(ops_host_root).findings[0].code == "CONFIG_DUPLICATE_CRITICAL_KEY"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("MEYAR_ENV=production", "MEYAR_ENV=development"),
        ("postgresql+asyncpg://synthetic:synthetic@localhost:5432/meyar", "not-a-database-url"),
        (
            "synthetic:synthetic@localhost:5432/meyar",
            "meyar:meyar_dev_password@localhost:5432/meyar",
        ),
        (
            "MEYAR_DATABASE_URL=postgresql+asyncpg://synthetic:synthetic@localhost:5432/meyar",
            "MEYAR_DATABASE_URL=  ",
        ),
        ("synthetic-host-test-secret", "dev-insecure-pending-login-secret-change-me"),
        ("MEYAR_PENDING_LOGIN_SECRET=synthetic-host-test-secret", "MEYAR_PENDING_LOGIN_SECRET=  "),
        ("MEYAR_UI_COOKIE_SECURE=true", "MEYAR_UI_COOKIE_SECURE=false"),
        ("MEYAR_LLM_PROVIDER=ollama", "MEYAR_LLM_PROVIDER=cloud"),
        ("MEYAR_EMBEDDING_PROVIDER=ollama", "MEYAR_EMBEDDING_PROVIDER=cloud"),
        ("http://127.0.0.1:11434", "http://external.example:11434"),
    ],
)
def test_unsafe_production_values_rejected(ops_host_root: Path, old: str, new: str) -> None:
    change(ops_host_root, old, new)
    assert not verify_host_config(ops_host_root).ok


def test_storage_must_be_exact_shared_location(ops_host_root: Path) -> None:
    change(ops_host_root, str(ops_host_root / "shared/storage"), str(ops_host_root / "other"))
    assert verify_host_config(ops_host_root).findings[0].code == "CONFIG_STORAGE_ROOT_MISMATCH"


def test_secret_never_in_result_or_cli_output(
    ops_host_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from meyar.ops.cli import main

    sentinel = "synthetic-secret-do-not-print-741"
    change(ops_host_root, "synthetic-host-test-secret", sentinel)
    with config(ops_host_root).open("a") as stream:
        stream.write("MEYAR_ENV=bad\n")
    result = verify_host_config(ops_host_root)
    assert sentinel not in result.model_dump_json()
    with pytest.raises(SystemExit):
        main(["config-verify", "--install-root", str(ops_host_root)])
    output = capsys.readouterr()
    assert sentinel not in output.out + output.err


def test_direct_settings_production_fails_closed() -> None:
    safe = {
        "env": "production",
        "database_url": "postgresql+asyncpg://synthetic:synthetic@localhost:5432/meyar",
        "pending_login_secret": "synthetic-host-test-secret",
    }
    assert Settings(_env_file=None, **safe).env == "production"
    for override in (
        {"database_url": ""},
        {"database_url": "not-a-database-url"},
        {"database_url": Settings.model_fields["database_url"].default},
        {"pending_login_secret": ""},
        {"pending_login_secret": "dev-insecure-pending-login-secret-change-me"},
        {"ui_cookie_secure": False},
        {"ollama_base_url": "http://external.example:11434"},
        {"embedding_provider": "cloud"},
        {"llm_provider": "cloud"},
    ):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **(safe | override))
    with pytest.raises(ValidationError):
        Settings(_env_file=None, env="production", pending_login_secret="synthetic")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **(safe | {"env": "prodution"}))
    assert Settings(_env_file=None, env="development", ui_cookie_secure=False).env == "development"


async def test_application_startup_rejects_unsafe_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meyar.config import get_settings
    from meyar.main import app

    monkeypatch.setenv("MEYAR_ENV", "production")
    monkeypatch.setenv("MEYAR_PENDING_LOGIN_SECRET", "dev-insecure-pending-login-secret-change-me")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValidationError):
            async with app.router.lifespan_context(app):
                pass
    finally:
        get_settings.cache_clear()


async def test_host_startup_rejects_ambient_override(
    runtime_service_identity: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from meyar.config import get_settings
    from meyar.main import app

    monkeypatch.setenv("MEYAR_ENV", "development")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="overridden"):
            async with app.router.lifespan_context(app):
                pass
    finally:
        get_settings.cache_clear()


async def test_host_startup_accepts_distinct_service_uid(
    ops_host_root: Path, runtime_service_identity: int
) -> None:
    from meyar.config import get_settings
    from meyar.main import app

    assert runtime_service_identity != ops_host_root.stat().st_uid
    get_settings.cache_clear()
    try:
        async with app.router.lifespan_context(app):
            pass
    finally:
        get_settings.cache_clear()


async def test_host_startup_rejects_service_owned_fake_tree(
    ops_host_root: Path, runtime_service_identity: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from meyar.config import get_settings
    from meyar.main import app

    original_stat = Path.stat

    def service_owned_root(
        path: Path, *args: object, **kwargs: object
    ) -> SimpleNamespace | os.stat_result:
        metadata = original_stat(path, *args, **kwargs)
        if path != ops_host_root:
            return metadata
        return SimpleNamespace(
            st_mode=metadata.st_mode,
            st_uid=runtime_service_identity,
            st_gid=metadata.st_gid,
        )

    monkeypatch.setattr(Path, "stat", service_owned_root)
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="HOST_LAYOUT_UNSAFE"):
            async with app.router.lifespan_context(app):
                pass
    finally:
        get_settings.cache_clear()


async def test_host_startup_rejects_wrong_owned_config(
    runtime_service_identity: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from meyar.config import get_settings
    from meyar.main import app

    original_fstat = os.fstat

    def service_owned_config(fd: int) -> SimpleNamespace:
        metadata = original_fstat(fd)
        return SimpleNamespace(
            st_mode=metadata.st_mode,
            st_nlink=metadata.st_nlink,
            st_size=metadata.st_size,
            st_uid=runtime_service_identity,
            st_gid=metadata.st_gid,
        )

    monkeypatch.setattr(os, "fstat", service_owned_config)
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="CONFIG_PERMISSIONS_UNSAFE"):
            async with app.router.lifespan_context(app):
                pass
    finally:
        get_settings.cache_clear()


async def test_host_startup_requires_service_group_membership(
    runtime_service_identity: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from meyar.config import get_settings
    from meyar.main import app

    monkeypatch.setattr(os, "getegid", lambda: -1)
    monkeypatch.setattr(os, "getgroups", lambda: [])
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="HOST_LAYOUT_UNSAFE"):
            async with app.router.lifespan_context(app):
                pass
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("mode", [0o644, 0o660])
async def test_host_startup_rejects_writable_or_public_config(
    ops_host_root: Path, runtime_service_identity: int, mode: int
) -> None:
    from meyar.config import get_settings
    from meyar.main import app

    config(ops_host_root).chmod(mode)
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="CONFIG_PERMISSIONS_UNSAFE"):
            async with app.router.lifespan_context(app):
                pass
    finally:
        get_settings.cache_clear()
