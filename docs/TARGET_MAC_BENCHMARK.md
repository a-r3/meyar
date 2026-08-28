# MEYAR — Target-Mac Benchmark & Model-Approval Gate

**FINAL TARGET-MAC GATE NOT YET EXECUTED.** This document describes the
harness and process; it does not itself constitute the acceptance run.

## Why this gate exists

`docs/DECISIONS.md` D-001 records that the current development machine
is not Apple Silicon and is explicitly not the target — production LLM
(`nomic-embed-text` is likewise flagged as a dev-only placeholder,
D-014) and embedding model selection is deferred until real numbers
exist from the actual target hardware.

## Canonical target hardware

**Not yet recorded anywhere in project documentation.** Every existing
reference to "Mac Mini" / "target hardware" is generic (see
`docs/MASTER_SPEC.md`, `docs/MVP_PLAN.md`). This is an **owner input
required** item — the exact model, chip, RAM, and macOS version must be
supplied and recorded here (and in `docs/DECISIONS.md`) before a
benchmark run can count as the official acceptance gate.

## Acceptance policy for this pass (owner decision, 2026-08-28)

No arbitrary hard latency threshold is approved. Final Target-Mac
acceptance initially requires:

- successful execution of the approved benchmark workflow;
- measured latencies recorded transparently;
- model/runtime configuration recorded;
- no correctness/security failure.

If measured behavior is obviously impractical (e.g. an operation takes
minutes where a synchronous HR workflow expects seconds), that is
reported back to the owner for a decision — it is not grounds to
silently fail or silently pass the gate. Candidate numeric thresholds
may be proposed later, but only as explicit proposals subject to owner
approval, never invented and applied unilaterally.

## Harness

`backend/scripts/target_mac_benchmark.py` — run on the target machine:

```bash
cd backend
uv run python scripts/target_mac_benchmark.py --out target-mac-report.json
```

It records, per operation: latency, dataset size, cold/warm state,
success/failure, and never invents a number for an operation it did not
actually run. Alongside per-operation results it records hardware
CPU/OS/Python identification (RAM must be recorded manually — not
programmatically detected), PostgreSQL/Ollama versions, and the exact
LLM/embedding model name in use.

**Current scope (this preparation pass):** the harness measures PDF
parse, DOCX parse, profile extraction, and embedding creation directly
— these need only a reachable local Ollama, no seeded database. It does
NOT yet measure structured search, semantic/hybrid search, NL planning,
deterministic score, or batch rank — those require a seeded tenant/job/
candidate fixture and are explicitly deferred to be wired in using the
same synthetic end-to-end fixture Phase F will build, at the time of the
actual target-Mac run — not fabricated ahead of that fixture existing.

A dry run of this harness was executed on the development machine
during Slice 13 preparation purely to prove the harness code itself
works (JSON schema, error handling, non-fatal degradation when a model
is missing). **That dev-machine run is not the target-Mac gate and
implies no model approval** — see
`docs/STATUS.md` for the explicit note distinguishing the two.

## Model-approval gate

Do not approve `qwen3:0.6b`, `nomic-embed-text`, or any other model as
final production selection solely because this harness exists or has
been dry-run on a non-target machine. Production model approval
requires: real numbers from the approved target hardware, recorded
model name + revision, and an explicit decision recorded in
`docs/DECISIONS.md` (D-020 or a dedicated entry).

## Status

- Target hardware supplied: **NO**
- Benchmark harness built: **YES** (partial — see scope above)
- Benchmark executed on target hardware: **NO**
- Production LLM model approved: **NO**
- Production embedding model approved: **NO**
- M5 / issue #20 Target-Mac row: **remains OPEN**
