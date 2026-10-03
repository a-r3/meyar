"""Internal target-release-only forward migration worker.

The operator-facing update command supplies fixed identifiers. The worker
loads the protected host configuration itself; no credential enters argv or
subprocess output. Only an exact source revision may be upgraded.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Never

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import create_async_engine

import meyar
from alembic import command
from meyar.ops.alembic_introspect import get_db_alembic_revision
from meyar.ops.host_config import load_host_settings
from meyar.ops.offline_host import _load_json, _verify_install


async def _revision(engine: object) -> str | None:
    result = await get_db_alembic_revision(engine)  # type: ignore[arg-type]
    return result.revisions[0] if result.table_exists and len(result.revisions) == 1 else None


async def _upgrade(config: Config, database_url: str, source: str, target: str) -> bool:
    engine = create_async_engine(
        database_url,
        hide_parameters=True,
        pool_pre_ping=True,
        connect_args={"timeout": 5.0},
    )
    try:
        if await _revision(engine) != source:
            return False

        def migrate(connection: object) -> None:
            config.attributes["connection"] = connection
            try:
                command.upgrade(config, target)
            finally:
                del config.attributes["connection"]

        async with engine.begin() as connection:
            await connection.run_sync(migrate)
        return await _revision(engine) == target
    finally:
        await engine.dispose()


class _PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> Never:
        raise ValueError("INVALID_INVOCATION")


def main(argv: list[str] | None = None) -> int:
    parser = _PrivateArgumentParser(add_help=False)
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument("--target-release-id", required=True)
    parser.add_argument("--from-head", required=True)
    parser.add_argument("--to-head", required=True)
    try:
        args = parser.parse_args(argv)
        root: Path = args.install_root
        release = root / "releases" / args.target_release_id
        if (
            os.geteuid() <= 0
            or not root.is_absolute()
            or ".." in root.parts
            or Path(sys.executable).resolve() != release / ".venv/bin/python"
            or Path(meyar.__file__).resolve().parent != release / "backend/src/meyar"
            or Path(__file__).resolve() != release / "backend/src/meyar/ops/update_worker.py"
        ):
            return 1
        _verify_install(root, args.target_release_id)
        manifest = _load_json(release / "release_manifest.json")
        if manifest.get("alembic_heads") != [args.to_head]:
            return 1
        ini = release / "backend/alembic.ini"
        config = Config(str(ini))
        if config.get_main_option("script_location") != str(release / "backend/alembic") or list(
            ScriptDirectory.from_config(config).get_heads()
        ) != [args.to_head]:
            return 1
        config.attributes["meyar_schema_init"] = True
        settings = load_host_settings(root)
        return (
            0
            if asyncio.run(_upgrade(config, settings.database_url, args.from_head, args.to_head))
            else 1
        )
    except BaseException:  # noqa: BLE001 - raw migration/driver errors never reach parent
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
