from collections.abc import Iterable

from meyar.search.planner_schemas import PlannerOutcome, SearchPlanResult
from meyar.ui.view_models import PlannerOutcomeView

PLANNER_OUTCOME_TEXT: dict[PlannerOutcome, tuple[str, str]] = {
    PlannerOutcome.EXECUTABLE: (
        "Sorğu icra edildi",
        "Tələbiniz uğurla başa düşüldü və axtarış aparıldı.",
    ),
    PlannerOutcome.PROHIBITED_REQUEST: (
        "Sorğu qəbul edilmədi",
        "Tələbdə istifadəsinə icazə verilməyən meyar aşkarlandı. Axtarış aparılmadı.",
    ),
    PlannerOutcome.UNSUPPORTED_SEMANTICS: (
        "Tələb hazırda dəstəklənmir",
        "Bu tələbi mənasını zəiflətmədən icra etmək mümkün olmadı. Tələbi sadələşdirib "
        "yenidən cəhd edin.",
    ),
    PlannerOutcome.AMBIGUOUS_REQUEST: (
        "Tələbi daha aydın yazın",
        "Axtarış meyarları müəyyən edilə bilmədi. Nə axtardığınızı daha konkret təsvir edin.",
    ),
    PlannerOutcome.MALFORMED_MODEL_OUTPUT: (
        "Sorğu emal edilə bilmədi",
        "AI xidmətinin cavabını təhlükəsiz axtarış planına çevirmək mümkün olmadı. "
        "Tələbi daha konkret ifadə edib yenidən cəhd edin.",
    ),
    PlannerOutcome.PLANNER_PROVIDER_FAILURE: (
        "AI xidməti əlçatan deyil",
        "AI axtarış xidməti hazırda əlçatan deyil. Bir qədər sonra yenidən cəhd edin.",
    ),
    PlannerOutcome.VALIDATION_FAILURE: (
        "Sorğu təhlükəsiz icra edilə bilmədi",
        "Sorğunu təhlükəsiz axtarış planına çevirmək mümkün olmadı. Tələbi daha konkret "
        "ifadə edib yenidən cəhd edin.",
    ),
}

READINESS_LABELS: dict[str | None, str] = {
    "COMPLETED": "Hazır",
    "MANUAL_REVIEW_REQUIRED": "Diqqət tələb edir",
    "FAILED": "Diqqət tələb edir",
    None: "Emal olunur",
}


def readiness_label(profile_status: str | None) -> str:
    """Coarse, HR-facing readiness for a candidate's current profile —
    collapses the underlying pipeline status into the three states the
    owner asked the candidate card/detail to communicate."""
    return READINESS_LABELS.get(profile_status, "Emal olunur")


def readiness_state(profile_status: str | None) -> str:
    """The data-state value used to color the readiness badge — reuses
    the existing status-badge CSS instead of adding new rules."""
    return profile_status or "PENDING"

FIT_BAND_LABELS = {
    "STRONG_MATCH": "Güclü uyğunluq",
    "POTENTIAL_MATCH": "Potensial uyğunluq",
    "INSUFFICIENT_EVIDENCE": "Kifayət qədər sübut yoxdur",
    "MANUAL_REVIEW_REQUIRED": "İnsan baxışı tələb olunur",
}

CRITERION_STATUS_LABELS = {
    "MATCH": "Uyğundur",
    "PARTIAL_MATCH": "Qismən uyğundur",
    "NOT_MATCHED": "Açıq sübuta əsasən uyğun gəlmir",
    "UNKNOWN": "Məlumat məlum deyil",
    "CONFLICTING_EVIDENCE": "Ziddiyyətli sübut",
    "MANUAL_REVIEW_REQUIRED": "İnsan baxışı tələb olunur",
}

# HR-facing labels for the deterministic policy engine's CriterionKind enum
# (docs/MASTER_SPEC.md). Purely a presentation lookup — never used by
# scoring/matching itself, which continues to key on the raw enum value.
CRITERION_KIND_LABELS = {
    "SKILL": "Bacarıq",
    "EXPERIENCE": "Təcrübə",
    "CERTIFICATION": "Sertifikat",
    "EDUCATION": "Təhsil",
    "LANGUAGE": "Dil",
}

STATE_LABELS = {
    "PENDING": "Gözləyir",
    "PARSED": "Emal olunub",
    "PARSE_FAILED": "Emal uğursuzdur",
    "COMPLETED": "Tamamlanıb",
    "FAILED": "Uğursuzdur",
    "MANUAL_REVIEW_REQUIRED": "İnsan baxışı tələb olunur",
    "INDEXED": "İndekslənib",
    "MISSING": "Mənbədə yoxdur",
}


def planner_outcome_view(
    plan: SearchPlanResult, *, result_count: int | None = None
) -> PlannerOutcomeView:
    title, message = PLANNER_OUTCOME_TEXT[plan.outcome]
    return PlannerOutcomeView(
        outcome=plan.outcome.value,
        title=title,
        message=message,
        executable=plan.executable,
        reason_codes=[reason.value for reason in plan.reason_codes],
        mode=(plan.search_request.mode.value if plan.search_request else None),
        result_count=result_count,
    )


def join_nonempty(values: Iterable[str | None], *, separator: str = " · ") -> str | None:
    parts = [value.strip() for value in values if value and value.strip()]
    return separator.join(parts) if parts else None
