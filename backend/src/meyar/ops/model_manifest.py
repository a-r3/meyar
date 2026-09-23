"""Typed model-manifest contract (issue #35 PR1 §8). Defines the shape
only — this PR selects no production model. Current truth, recorded here
as documentation rather than a shipped "production" value:

- `qwen3:0.6b` is the source/default development/integration LLM setting
  (`meyar.config.Settings.ollama_model`).
- `qwen3:1.7b` is a recent local acceptance/browser-runtime override, not
  a benchmarked or production-approved model.
- The production model is **TBD**, blocked on issue #36 (real Target-Mac
  benchmark on the confirmed Mac mini M4 Pro reference hardware).

See docs/MEYAR_OPS.md and docs/DECISIONS.md.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ModelRole(StrEnum):
    LLM = "LLM"
    EMBEDDING = "EMBEDDING"


class ModelApprovalStatus(StrEnum):
    """Approval states are strictly ordered by trust; nothing in this PR
    instantiates PRODUCTION_APPROVED — that requires an explicit,
    separately reviewed decision after issue #36's benchmark."""

    DEVELOPMENT_INTEGRATION = "DEVELOPMENT_INTEGRATION"
    BENCHMARKED_PENDING_APPROVAL = "BENCHMARKED_PENDING_APPROVAL"
    PRODUCTION_APPROVED = "PRODUCTION_APPROVED"


class ModelManifestEntry(BaseModel):
    model_config = {"extra": "forbid"}

    role: ModelRole
    model_name: str = Field(min_length=1, max_length=200)
    # Ollama model digest/checksum when the runtime reports one; absent
    # for providers/models that don't expose it (see
    # meyar.embedding.ollama_provider, which has none for embeddings).
    digest: str | None = Field(default=None, max_length=128)
    runtime_version: str | None = Field(default=None, max_length=64)
    embedding_dimensions: int | None = Field(default=None, ge=1)
    serializer_version: str | None = Field(default=None, max_length=32)
    approval_status: ModelApprovalStatus
    # A pointer/id (e.g. a docs/DECISIONS.md entry, a benchmark report
    # path) — never raw benchmark data or candidate content.
    benchmark_reference: str | None = Field(default=None, max_length=300)


class ModelManifest(BaseModel):
    model_config = {"extra": "forbid"}

    manifest_schema_version: int = Field(default=1, ge=1)
    entries: list[ModelManifestEntry] = Field(min_length=1)
