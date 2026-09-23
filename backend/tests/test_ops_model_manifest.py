"""Model-manifest schema (issue #35 PR1 §8) — approval states are
supported by the schema but this PR ships no PRODUCTION_APPROVED
instance anywhere in the repo (see docs/MEYAR_OPS.md)."""

import pytest
from pydantic import ValidationError

from meyar.ops.model_manifest import (
    ModelApprovalStatus,
    ModelManifest,
    ModelManifestEntry,
    ModelRole,
)


def test_valid_model_manifest_current_truth() -> None:
    """The actual current dev-integration models (see
    meyar.config.Settings defaults / docs/DECISIONS.md), both
    DEVELOPMENT_INTEGRATION — no production approval exists yet."""
    manifest = ModelManifest(
        entries=[
            ModelManifestEntry(
                role=ModelRole.LLM,
                model_name="qwen3:0.6b",
                approval_status=ModelApprovalStatus.DEVELOPMENT_INTEGRATION,
            ),
            ModelManifestEntry(
                role=ModelRole.LLM,
                model_name="qwen3:1.7b",
                approval_status=ModelApprovalStatus.DEVELOPMENT_INTEGRATION,
                benchmark_reference="local acceptance override, not a target-Mac benchmark",
            ),
            ModelManifestEntry(
                role=ModelRole.EMBEDDING,
                model_name="nomic-embed-text",
                embedding_dimensions=768,
                serializer_version="1",
                approval_status=ModelApprovalStatus.DEVELOPMENT_INTEGRATION,
            ),
        ]
    )
    approved = ModelApprovalStatus.PRODUCTION_APPROVED
    assert all(e.approval_status != approved for e in manifest.entries)
    reparsed = ModelManifest.model_validate_json(manifest.model_dump_json())
    assert reparsed == manifest


def test_approval_status_supports_all_three_states() -> None:
    values = {s.value for s in ModelApprovalStatus}
    assert values == {
        "DEVELOPMENT_INTEGRATION",
        "BENCHMARKED_PENDING_APPROVAL",
        "PRODUCTION_APPROVED",
    }


def test_model_manifest_schema_supports_production_approved_when_declared() -> None:
    """The schema itself must be able to represent PRODUCTION_APPROVED —
    only an explicit future decision may ever ship an instance with it."""
    entry = ModelManifestEntry(
        role=ModelRole.LLM,
        model_name="some-future-approved-model",
        approval_status=ModelApprovalStatus.PRODUCTION_APPROVED,
        benchmark_reference="docs/TARGET_MAC_BENCHMARK.md#future-run",
    )
    assert entry.approval_status is ModelApprovalStatus.PRODUCTION_APPROVED


def test_model_manifest_requires_at_least_one_entry() -> None:
    with pytest.raises(ValidationError):
        ModelManifest(entries=[])


def test_model_manifest_entry_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ModelManifestEntry(
            role=ModelRole.LLM,
            model_name="x",
            approval_status=ModelApprovalStatus.DEVELOPMENT_INTEGRATION,
            unexpected="nope",
        )


def test_model_manifest_entry_rejects_invalid_role() -> None:
    with pytest.raises(ValidationError):
        ModelManifestEntry(
            role="RERANKER",  # not LLM/EMBEDDING
            model_name="x",
            approval_status=ModelApprovalStatus.DEVELOPMENT_INTEGRATION,
        )


def test_model_manifest_entry_rejects_empty_model_name() -> None:
    with pytest.raises(ValidationError):
        ModelManifestEntry(
            role=ModelRole.LLM,
            model_name="",
            approval_status=ModelApprovalStatus.DEVELOPMENT_INTEGRATION,
        )


def test_development_integration_does_not_require_benchmark_reference() -> None:
    entry = ModelManifestEntry(
        role=ModelRole.LLM,
        model_name="qwen3:0.6b",
        approval_status=ModelApprovalStatus.DEVELOPMENT_INTEGRATION,
    )
    assert entry.benchmark_reference is None


def test_benchmarked_pending_approval_requires_benchmark_reference() -> None:
    with pytest.raises(ValidationError, match="benchmark_reference"):
        ModelManifestEntry(
            role=ModelRole.LLM,
            model_name="candidate-model",
            approval_status=ModelApprovalStatus.BENCHMARKED_PENDING_APPROVAL,
        )


def test_production_approved_requires_benchmark_reference() -> None:
    with pytest.raises(ValidationError, match="benchmark_reference"):
        ModelManifestEntry(
            role=ModelRole.LLM,
            model_name="candidate-model",
            approval_status=ModelApprovalStatus.PRODUCTION_APPROVED,
        )


def test_benchmarked_pending_approval_rejects_blank_benchmark_reference() -> None:
    with pytest.raises(ValidationError, match="benchmark_reference"):
        ModelManifestEntry(
            role=ModelRole.LLM,
            model_name="candidate-model",
            approval_status=ModelApprovalStatus.BENCHMARKED_PENDING_APPROVAL,
            benchmark_reference="   ",
        )
