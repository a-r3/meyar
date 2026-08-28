"""Slice 13 — Target-Mac benchmark harness (Phase E: harness only).

FINAL TARGET-MAC GATE NOT YET EXECUTED. Exact target hardware has not
been supplied by the owner (docs/DECISIONS.md D-001 records only the
non-target dev machine spec). Running this script on any machine other
than the approved target Mac Mini does NOT satisfy the Target-Mac
acceptance gate in issue #20 / M5, and its output must never be used to
approve a production LLM or embedding model on its own.

This script only measures and records. It encodes NO pass/fail latency
threshold — none is approved (see docs/DECISIONS.md and issue #20).
Acceptance policy (functional-success-only vs. explicit thresholds) is
an explicit owner decision still pending.

Usage:

    cd backend
    uv run python scripts/target_mac_benchmark.py [--out report.json]

Requires a reachable local Postgres (MEYAR_DATABASE_URL) and, for the
LLM/embedding-dependent operations, a reachable local Ollama
(MEYAR_OLLAMA_BASE_URL) with the configured models pulled. Any operation
that cannot run in the current environment is recorded with
success=false and an error message — never silently skipped, never
fabricated.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "synthetic_cvs"


@dataclass
class OperationResult:
    operation: str
    success: bool
    latency_ms: float | None = None
    dataset_size: str | None = None
    cold_or_warm: str = "cold"
    error: str | None = None


@dataclass
class BenchmarkReport:
    hardware_model: str = "UNKNOWN — run on the owner-approved target Mac Mini"
    cpu: str = ""
    ram_note: str = "not programmatically detected — record manually from the target host"
    os_version: str = ""
    python_version: str = ""
    postgres_version: str = ""
    ollama_version: str = ""
    llm_model: str = ""
    llm_model_revision: str = ""
    embedding_model: str = ""
    embedding_model_revision: str = ""
    dataset_size: str = "1 synthetic candidate, 1 synthetic job"
    operations: list[OperationResult] = field(default_factory=list)
    note: str = (
        "FINAL TARGET-MAC GATE NOT YET EXECUTED. This report is only meaningful if "
        "hardware_model above is the owner-approved target Mac Mini. No latency "
        "threshold is applied — record only."
    )


@contextmanager
def _timed():
    start = time.perf_counter()
    box: dict[str, float] = {}
    try:
        yield box
    finally:
        box["latency_ms"] = (time.perf_counter() - start) * 1000


def _tool_version(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception as exc:  # noqa: BLE001 — best-effort metadata only
        return f"unavailable ({exc})"


async def _run_operations(report: BenchmarkReport) -> None:
    from meyar.config import get_settings
    from meyar.embedding.dependency import get_embedding_provider
    from meyar.extraction.view import ModelInputBlock, ProfessionalDocumentView
    from meyar.ingestion.parsers.local_text_parser import LocalTextParser
    from meyar.llm.dependency import get_llm_provider

    settings = get_settings()
    report.llm_model = settings.ollama_model
    report.embedding_model = settings.ollama_embedding_model

    parser = LocalTextParser()

    with _timed() as t:
        try:
            pdf_bytes = (FIXTURES_DIR / "valid_cv.pdf").read_bytes()
            await parser.parse(data=pdf_bytes, document_type="PDF")
            success, error = True, None
        except Exception as exc:  # noqa: BLE001
            success, error = False, str(exc)
    report.operations.append(
        OperationResult("pdf_parse", success, t["latency_ms"], "1 synthetic CV", error=error)
    )

    with _timed() as t:
        try:
            docx_bytes = (FIXTURES_DIR / "valid_cv.docx").read_bytes()
            await parser.parse(data=docx_bytes, document_type="DOCX")
            success, error = True, None
        except Exception as exc:  # noqa: BLE001
            success, error = False, str(exc)
    report.operations.append(
        OperationResult("docx_parse", success, t["latency_ms"], "1 synthetic CV", error=error)
    )

    view = ProfessionalDocumentView(
        canonical_document_id=uuid.uuid4(),
        blocks=[ModelInputBlock(page=1, block_index=0, text="Skills: Python, SQL, Docker")],
    )
    with _timed() as t:
        try:
            llm = get_llm_provider()
            report.llm_model_revision = getattr(llm, "model_revision", "")
            await llm.extract_candidate_profile(view)
            success, error = True, None
        except Exception as exc:  # noqa: BLE001
            success, error = False, f"{type(exc).__name__}: {exc}"
    report.operations.append(
        OperationResult(
            "profile_extraction", success, t["latency_ms"], "1 synthetic CV", error=error
        )
    )

    with _timed() as t:
        try:
            embedder = get_embedding_provider()
            embed_result = await embedder.embed("Skills: Python, SQL, Docker")
            report.embedding_model_revision = embed_result.model_revision
            success, error = True, None
        except Exception as exc:  # noqa: BLE001
            success, error = False, f"{type(exc).__name__}: {exc}"
    report.operations.append(
        OperationResult(
            "embedding_creation", success, t["latency_ms"], "1 synthetic CV", error=error
        )
    )

    for op_name in (
        "structured_search",
        "semantic_hybrid_search",
        "nl_planner",
        "deterministic_score",
        "batch_rank",
        "app_startup",
    ):
        report.operations.append(
            OperationResult(
                op_name,
                success=False,
                error=(
                    "Not measured by this harness pass — requires a seeded tenant/job/"
                    "criteria/candidate fixture and a running app instance; wire in during "
                    "the actual target-Mac run using the same synthetic E2E fixture as "
                    "Phase F, not fabricated here."
                ),
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

    report = BenchmarkReport(
        cpu=platform.processor() or platform.machine(),
        os_version=f"{platform.system()} {platform.release()}",
        python_version=platform.python_version(),
        postgres_version=_tool_version(["psql", "--version"]),
        ollama_version=_tool_version(["ollama", "--version"]),
    )

    asyncio.run(_run_operations(report))

    payload = asdict(report)
    text = json.dumps(payload, indent=2)
    print(text)
    if args.out:
        args.out.write_text(text)
        print(f"\nWritten to {args.out}", file=sys.stderr)

    print(
        "\nFINAL TARGET-MAC GATE NOT YET EXECUTED — this run's hardware_model is "
        f"{report.hardware_model!r}. Do not treat this report as target-Mac acceptance "
        "or as production model approval.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
