"""Shared D-055 coordination-split authority.

A same-sentence conjunction ("X and Y ...") may split into independent
per-side occurrences only when every side carries its own explicit,
unambiguous modality. Otherwise the complete source clause is retained as
one attributable material span for human review.

The single server-owned segmenter (``meyar.agent.semantic_requirements``)
enforces this rule through this primitive. Issue #84 removed the former
second JD/model-grounding segmenter so exactly one segmentation policy owns
JD source spans.
"""

from collections.abc import Callable, Sequence


def coordinated_split_authorized(
    parts: Sequence[str], is_side_authorized: Callable[[str], bool]
) -> bool:
    """D-055: split only when every coordinated side is independently authorized."""
    return len(parts) > 1 and all(is_side_authorized(part) for part in parts)
