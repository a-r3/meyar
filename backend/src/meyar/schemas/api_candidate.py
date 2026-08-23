"""Slice 12 — external-facing DTO for `GET /api/v1/candidates/{id}/detail`.

`meyar.ui.view_models.CandidateDetailView` (built by
`meyar.ui.service.get_candidate_detail_view`) already has exactly the
right, privacy-safe shape for this response: identity (presentation-only,
never a scoring signal) + current profile facts/evidence + document/
evaluation summaries, and already excludes storage paths, raw CV bytes,
prompts, and vectors. Re-exporting it as the REST response model avoids
a duplicate parallel schema for an identical, already-reviewed shape.
"""

from meyar.ui.view_models import CandidateDetailView as ApiCandidateDetailResponse

__all__ = ["ApiCandidateDetailResponse"]
