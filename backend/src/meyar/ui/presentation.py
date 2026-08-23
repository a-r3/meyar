from collections.abc import Iterable

from meyar.search.planner_schemas import PlannerOutcome, SearchPlanResult
from meyar.ui.view_models import PlannerOutcomeView

PLANNER_OUTCOME_TEXT: dict[PlannerOutcome, tuple[str, str]] = {
    PlannerOutcome.EXECUTABLE: (
        "Sorğu icra edildi",
        "Axtarış qəbul edilmiş plan və axtarış xidmətləri ilə icra olundu.",
    ),
    PlannerOutcome.PROHIBITED_REQUEST: (
        "Sorğu qəbul edilmədi",
        "Sorğuda istifadəsi qadağan edilmiş meyar aşkarlandı. Axtarış aparılmadı.",
    ),
    PlannerOutcome.UNSUPPORTED_SEMANTICS: (
        "Sorğunun mənası dəstəklənmir",
        "Bu tələb hazırkı axtarış imkanları ilə mənası zəiflədilmədən icra edilə bilmir.",
    ),
    PlannerOutcome.AMBIGUOUS_REQUEST: (
        "Sorğu qeyri-müəyyəndir",
        "Tələbi daha dəqiq yazın. Sistem məhdudiyyətləri özü təxmin etmədi.",
    ),
    PlannerOutcome.MALFORMED_MODEL_OUTPUT: (
        "Plan yaradıla bilmədi",
        "Yerli AI cavabı etibarlı plan sxeminə uyğun olmadı. Axtarış aparılmadı.",
    ),
    PlannerOutcome.PLANNER_PROVIDER_FAILURE: (
        "Yerli AI xidməti əlçatan deyil",
        "Yerli AI xidməti hazırda əlçatan deyil. Daha sonra yenidən cəhd edin.",
    ),
    PlannerOutcome.VALIDATION_FAILURE: (
        "Plan yoxlamadan keçmədi",
        "Yaradılmış plan təhlükəsiz icra tələblərinə uyğun olmadı. Axtarış aparılmadı.",
    ),
}

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
