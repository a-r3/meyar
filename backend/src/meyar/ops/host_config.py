"""Read-only verification of the host-owned production .env file."""

from __future__ import annotations

import io
import os
import stat
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from dotenv.parser import parse_stream
from pydantic import ValidationError
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from meyar.config import Settings
from meyar.ops.offline_host import InstallFailure, _real_directory
from meyar.ops.result import FindingStatus, OpsResult, OpsResultBuilder

MAX_CONFIG_BYTES = 64 * 1024
CRITICAL_KEYS = frozenset(
    {
        "MEYAR_ENV",
        "MEYAR_DATABASE_URL",
        "MEYAR_PENDING_LOGIN_SECRET",
        "MEYAR_UI_COOKIE_SECURE",
        "MEYAR_STORAGE_ROOT",
        "MEYAR_LLM_PROVIDER",
        "MEYAR_EMBEDDING_PROVIDER",
        "MEYAR_OLLAMA_BASE_URL",
    }
)
_FAILURE_CODES = frozenset(
    {
        "INSTALL_ROOT_UNSAFE",
        "HOST_LAYOUT_UNSAFE",
        "CONFIG_UNAVAILABLE",
        "CONFIG_NOT_REGULAR",
        "CONFIG_TOO_LARGE",
        "CONFIG_UNREADABLE",
        "CONFIG_MALFORMED",
        "CONFIG_DUPLICATE_CRITICAL_KEY",
        "CONFIG_INTERPOLATION_UNSUPPORTED",
        "CONFIG_CRITICAL_KEY_MISSING",
        "CONFIG_COOKIE_NOT_SECURE",
        "CONFIG_VALUES_INVALID",
        "CONFIG_NOT_PRODUCTION",
        "CONFIG_STORAGE_ROOT_MISMATCH",
    }
)


class _FileSettings(Settings):
    """Validate only parsed file values, never ambient operator shell values."""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


def _read_config(root: Path) -> str:
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError("INSTALL_ROOT_UNSAFE")
    try:
        _real_directory(root / "shared" / "config")
        _real_directory(root / "shared" / "storage")
    except InstallFailure:
        raise ValueError("HOST_LAYOUT_UNSAFE") from None
    path = root / "shared" / "config" / ".env"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise ValueError("CONFIG_UNAVAILABLE") from None
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("CONFIG_NOT_REGULAR")
        if metadata.st_size > MAX_CONFIG_BYTES:
            raise ValueError("CONFIG_TOO_LARGE")
        chunks: list[bytes] = []
        remaining = MAX_CONFIG_BYTES + 1
        try:
            while remaining:
                chunk = os.read(fd, min(remaining, 8192))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        except OSError:
            raise ValueError("CONFIG_UNREADABLE") from None
        raw = b"".join(chunks)
        if len(raw) > MAX_CONFIG_BYTES:
            raise ValueError("CONFIG_TOO_LARGE")
        try:
            return raw.decode("utf-8")
        except UnicodeError:
            raise ValueError("CONFIG_MALFORMED") from None
    finally:
        os.close(fd)


def load_host_settings(root: Path) -> Settings:
    raw = _read_config(root)
    seen: set[str] = set()
    for binding in parse_stream(io.StringIO(raw)):
        if binding.error:
            raise ValueError("CONFIG_MALFORMED")
        if binding.key is None:
            continue  # blank line or comment
        if binding.value is None:
            raise ValueError("CONFIG_MALFORMED")
        if binding.key in CRITICAL_KEYS and binding.key in seen:
            raise ValueError("CONFIG_DUPLICATE_CRITICAL_KEY")
        seen.add(binding.key)
        if "${" in binding.value:
            raise ValueError("CONFIG_INTERPOLATION_UNSUPPORTED")
    if not CRITICAL_KEYS <= seen:
        raise ValueError("CONFIG_CRITICAL_KEY_MISSING")
    values = dotenv_values(stream=io.StringIO(raw), interpolate=False)
    if values.get("MEYAR_UI_COOKIE_SECURE") != "true":
        raise ValueError("CONFIG_COOKIE_NOT_SECURE")
    fields = {
        key.removeprefix("MEYAR_").lower(): value
        for key, value in values.items()
        if key.startswith("MEYAR_") and value is not None
    }
    try:
        settings = _FileSettings(**dict[str, Any](fields))
    except (ValidationError, ValueError):
        raise ValueError("CONFIG_VALUES_INVALID") from None
    if settings.env != "production":
        raise ValueError("CONFIG_NOT_PRODUCTION")
    storage = Path(settings.storage_root)
    expected = root / "shared" / "storage"
    if (
        not storage.is_absolute()
        or ".." in storage.parts
        or storage != expected
        or storage.is_symlink()
    ):
        raise ValueError("CONFIG_STORAGE_ROOT_MISMATCH")
    return settings


def verify_host_config(root: Path) -> OpsResult:
    builder = OpsResultBuilder(action="config-verify")
    try:
        load_host_settings(root)
    except (ValueError, OSError) as exc:
        # Only allowlisted codes may reach output. Parser, Pydantic and
        # filesystem exception text can contain paths or raw config values.
        code = str(exc) if str(exc) in _FAILURE_CODES else "CONFIG_VALUES_INVALID"
        builder.add(
            component="host_config",
            status=FindingStatus.FAIL,
            code=code,
            message="host production configuration failed verification",
        )
    else:
        builder.add(
            component="host_config",
            status=FindingStatus.OK,
            code="CONFIG_VERIFIED",
            message="host production configuration verified",
        )
    return builder.build()
