"""Shared D-055 coordination-split authority.

A same-sentence conjunction ("X and Y ...") may split into independent
per-side occurrences only when every side carries its own explicit,
unambiguous modality. Otherwise the complete source clause is retained as
one attributable material span for human review.

Both the JD/model-grounding segmenter (``meyar.agent.jd_authority``) and the
deterministic semantic requirement builder
(``meyar.agent.semantic_requirements``) enforce this same rule through this
one shared primitive instead of maintaining two independently drifting
segmentation policies.
"""

from collections.abc import Callable, Sequence


def coordinated_split_authorized(
    parts: Sequence[str], is_side_authorized: Callable[[str], bool]
) -> bool:
    """D-055: split only when every coordinated side is independently authorized."""
    return len(parts) > 1 and all(is_side_authorized(part) for part in parts)
