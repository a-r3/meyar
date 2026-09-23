"""Typed, versioned release-manifest contract (issue #35 PR1 §7).

This PR defines the schema and a deterministic identity builder; it does
not build a real release artifact (deferred to a later #35 PR — see
docs/MEYAR_OPS.md). `release_version` is always the `[project].version`
value from `backend/pyproject.toml` — never the FastAPI/OpenAPI display
version (`meyar.main.app.version`), which is a display concern only and
is intentionally not treated as deployment identity here. This PR does
not change `pyproject.toml`'s version; the existing inconsistency between
it and the OpenAPI display version is a known, recorded gap (see
docs/DECISIONS.md), not something broadened in scope here.
"""

from __future__ import annotations

import re
import tomllib
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from meyar.ops.model_manifest import ModelApprovalStatus

_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


class RollbackCompatibility(StrEnum):
    """Never implies automatic Alembic downgrade semantics — this is a
    declared classification consumed by (future) human-operated
    update/rollback runbooks, not an executable rollback mechanism."""

    APP_ONLY = "APP_ONLY"
    FORWARD_COMPATIBLE_SCHEMA = "FORWARD_COMPATIBLE_SCHEMA"
    BACKUP_RESTORE_REQUIRED = "BACKUP_RESTORE_REQUIRED"
    PROHIBITED_PENDING_PROCEDURE = "PROHIBITED_PENDING_PROCEDURE"


class ModelManifestReference(BaseModel):
    """Points at a ModelManifest rather than embedding one, so a release
    manifest never needs to change just because model approval state
    changes independently."""

    model_config = {"extra": "forbid"}

    reference: str = Field(min_length=1, max_length=300)
    status: ModelApprovalStatus


class ReleaseManifest(BaseModel):
    model_config = {"extra": "forbid"}

    manifest_schema_version: int = Field(default=1, ge=1)
    release_version: str = Field(min_length=1, max_length=64)
    release_id: str = Field(min_length=1, max_length=128)
    source_sha: str
    built_at: datetime
    required_python_version: str = Field(min_length=1, max_length=32)
    uv_lock_sha256: str
    alembic_heads: list[str] = Field(min_length=1)
    rollback_compatibility: RollbackCompatibility
    model_manifest: ModelManifestReference
    artifact_format: str = Field(min_length=1, max_length=32)
    artifact_format_version: int = Field(default=1, ge=1)

    @field_validator("source_sha")
    @classmethod
    def _validate_source_sha(cls, value: str) -> str:
        if not _FULL_SHA_RE.fullmatch(value):
            raise ValueError("source_sha must be exactly 40 lowercase hex characters")
        return value

    @field_validator("uv_lock_sha256")
    @classmethod
    def _validate_uv_lock_sha256(cls, value: str) -> str:
        if not _SHA256_HEX_RE.fullmatch(value):
            raise ValueError("uv_lock_sha256 must be exactly 64 lowercase hex characters")
        return value


def read_project_version(pyproject_path: Path) -> str:
    data = tomllib.loads(pyproject_path.read_text())
    version = data.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise ValueError(f"{pyproject_path} has no [project].version")
    return version


def compute_release_id(*, release_version: str, source_sha: str) -> str:
    """Deterministic identity from (release_version, full source_sha) —
    never randomly generated, so the same accepted commit always yields
    the same release_id. Uses a short SHA prefix for a readable id while
    the manifest's own `source_sha` field always carries the full 40
    characters."""
    if not _FULL_SHA_RE.fullmatch(source_sha):
        raise ValueError("source_sha must be exactly 40 lowercase hex characters")
    return f"meyar-{release_version}+{source_sha[:12]}"
