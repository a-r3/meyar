import re
from collections.abc import Iterable

from meyar.search.planner_schemas import PlannerOutcome, PlannerReasonCode, SearchPlanResult
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

# When PlannerOutcome.UNSUPPORTED_SEMANTICS carries the internal
# MODEL_DECLINED_INTERPRETATION marker, the AI planner model itself — not
# a deterministic product-policy check — decided it could not interpret
# the request. This is honestly a different situation from "this concept
# is not part of the product" (true regardless of which model is
# configured): on a small local model it can simply be a misjudged
# ordinary request. Told separately so HR does not conclude their
# requirement is unsupported by MEYAR when it may just be this machine's
# configured model. See docs/DECISIONS.md D-025.
_MODEL_DECLINED_INTERPRETATION_TEXT = (
    "AI tələbi tam anlaya bilmədi",
    "Yerli AI planlaşdırıcı modeli bu tələbi etibarlı şəkildə şərh edə bilmədi. Bu, "
    "MEYAR-ın dəstəkləmədiyi bir şey demək deyil — konfiqurasiya olunmuş yerli modelin "
    "məhdudiyyəti ola bilər. Tələbi sadələşdirib yenidən cəhd edin.",
)

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
    "SKILL_EXPERIENCE": "Bacarıq üzrə təcrübə",
    "DOMAIN_EXPERIENCE": "Sahə təcrübəsi",
}


def _compact_number(value: str) -> str:
    try:
        return f"{float(value):g}"
    except ValueError:
        return value


def criterion_explanation_az(*, reason_code: str, explanation: str, label: str) -> str:
    """Translate deterministic evaluator conclusions at the HR UI boundary.

    The evaluator payload remains unchanged for API/history compatibility.  This
    function only replaces its developer-facing English prose with concise
    Azerbaijani copy; status and reason-code semantics still come from the
    deterministic result.
    """
    duration = re.search(
        r"(?:shows|Computed) (?P<actual>\d+(?:\.\d+)?) year\(s\).*?"
        r"(?:minimum|required) (?P<required>\d+(?:\.\d+)?)",
        explanation,
    )
    language = re.search(
        r"Profile states '([^']+)' at CEFR ([A-C][12]).*?required CEFR ([A-C][12])",
        explanation,
    )
    if reason_code == "SKILL_DURATION_SUFFICIENT" and duration:
        return (
            f"CV-də {label} üzrə {_compact_number(duration['actual'])} il təsdiqlənən "
            f"təcrübə var; minimum {_compact_number(duration['required'])} il tələbi ödənir."
        )
    if reason_code == "SKILL_DURATION_INSUFFICIENT" and duration:
        return (
            f"CV-də {label} üzrə {_compact_number(duration['actual'])} il təsdiqlənən "
            f"təcrübə var; minimum {_compact_number(duration['required'])} il tələbi ödənmir."
        )
    if reason_code == "DOMAIN_DURATION_SUFFICIENT" and duration:
        return (
            f"CV-də {label} sahəsində {_compact_number(duration['actual'])} il təsdiqlənən "
            f"təcrübə var; minimum {_compact_number(duration['required'])} il tələbi ödənir."
        )
    if reason_code == "DOMAIN_DURATION_INSUFFICIENT" and duration:
        return (
            f"CV-də {label} sahəsində {_compact_number(duration['actual'])} il təsdiqlənən "
            f"təcrübə var; minimum {_compact_number(duration['required'])} il tələbi ödənmir."
        )
    if reason_code == "EXPERIENCE_DURATION_SUFFICIENT" and duration:
        return (
            f"CV-də {_compact_number(duration['actual'])} il təsdiqlənən ümumi təcrübə var; "
            f"minimum {_compact_number(duration['required'])} il tələbi ödənir."
        )
    if reason_code == "EXPERIENCE_DURATION_INSUFFICIENT" and duration:
        return (
            f"CV-də {_compact_number(duration['actual'])} il təsdiqlənən ümumi təcrübə var; "
            f"minimum {_compact_number(duration['required'])} il tələbi ödənmir."
        )
    language_name = None
    if language:
        localized_languages = {
            "english": "ingilis dili",
            "russian": "rus dili",
            "azerbaijani": "Azərbaycan dili",
            "turkish": "türk dili",
            "german": "alman dili",
            "french": "fransız dili",
        }
        language_name = localized_languages.get(
            language[1].casefold(), f"{language[1]} dili"
        )
    if reason_code == "LANGUAGE_LEVEL_THRESHOLD_MET" and language:
        return (
            f"CV-də {language_name} səviyyəsi {language[2]} olaraq göstərilib; "
            f"{language[3]} tələbi ödənir."
        )
    if reason_code == "LANGUAGE_LEVEL_THRESHOLD_NOT_MET" and language:
        return (
            f"CV-də {language_name} səviyyəsi {language[2]} olaraq göstərilib; "
            f"{language[3]} tələbi ödənmir."
        )

    domain_label = "bank" if label.casefold() == "banking" else label.casefold()
    templates = {
        "SKILL_EXPLICIT_MATCH": f"CV-də {label} bacarığı təsdiqlənir.",
        "SKILL_NOT_FOUND_IN_PROFILE": (f"CV-də {label} bacarığı üzrə açıq sübut tapılmadı."),
        "CERTIFICATION_EXPLICIT_MATCH": f"CV-də {label} sertifikatı təsdiqlənir.",
        "CERTIFICATION_NOT_FOUND_IN_PROFILE": (
            f"CV-də {label} sertifikatı üzrə açıq sübut tapılmadı."
        ),
        "EDUCATION_EXPLICIT_MATCH": f"CV-də {label} təhsil tələbi təsdiqlənir.",
        "EDUCATION_NOT_FOUND_IN_PROFILE": (
            f"CV-də {label} təhsil tələbi üzrə açıq sübut tapılmadı."
        ),
        "LANGUAGE_PRESENT": f"CV-də {label} dili üzrə bilik təsdiqlənir.",
        "LANGUAGE_LEVEL_UNSTATED": (
            f"CV-də {label} dili göstərilib, lakin tələb olunan səviyyəni "
            "təsdiqləyən məlumat yoxdur."
        ),
        "LANGUAGE_LEVEL_EXPLICIT_MATCH": (
            f"CV-də {label} dili üzrə tələb olunan səviyyə təsdiqlənir."
        ),
        "LANGUAGE_LEVEL_SCALE_INCOMPATIBLE": (
            f"CV-də göstərilən {label} dili səviyyəsi tələblə etibarlı müqayisə edilə bilmir."
        ),
        "LANGUAGE_NOT_FOUND_IN_PROFILE": (f"CV-də {label} dili üzrə açıq sübut tapılmadı."),
        "EXPERIENCE_NO_EMPLOYMENT_HISTORY": (
            "CV-də iş təcrübəsi barədə kifayət qədər məlumat yoxdur."
        ),
        "EXPERIENCE_DATES_UNPARSEABLE": (
            "CV-dəki iş tarixləri etibarlı hesablanmadığı üçün insan baxışı tələb olunur."
        ),
        "EXPERIENCE_OVERLAPPING_DATES": (
            "CV-də üst-üstə düşən iş tarixləri var; ümumi təcrübə etibarlı hesablanmır."
        ),
        "SKILL_DURATION_NO_ATTRIBUTABLE_PERIODS": (
            f"CV-də {label} üzrə təcrübə müddətini təsdiqləyən tarixli sübut yoxdur."
        ),
        "SKILL_DURATION_NO_ATTRIBUTABLE_INTERVAL": (
            f"CV-də {label} bacarığı göstərilib, lakin istifadə müddəti ayrıca təsdiqlənmir."
        ),
        "SKILL_DURATION_DATES_UNPARSEABLE": (
            f"CV-də {label} üzrə göstərilən tarixlər etibarlı hesablanmır."
        ),
        "SKILL_DURATION_EMPLOYMENT_INCOMPATIBLE": (
            f"CV-də {label} üzrə tarixlər əlaqəli iş dövrü ilə uyğun gəlmir."
        ),
        "DOMAIN_NOT_FOUND_IN_PROFILE": (
            f"CV-də {label} sahəsi üzrə açıq təcrübə sübutu tapılmadı."
        ),
        "DOMAIN_EXPLICIT_MATCH": (f"CV-də {domain_label} sahəsində təcrübə təsdiqlənir."),
        "DOMAIN_DURATION_DATES_UNPARSEABLE": (
            f"CV-də {label} sahəsi üzrə göstərilən tarixlər etibarlı hesablanmır."
        ),
        "DOMAIN_DURATION_EMPLOYMENT_INCOMPATIBLE": (
            f"CV-də {label} sahəsi üzrə tarixlər əlaqəli iş dövrü ilə uyğun gəlmir."
        ),
        "DOMAIN_DURATION_NO_ATTRIBUTABLE_INTERVAL": (
            f"CV-də {label} sahəsi üzrə təcrübə göstərilib, lakin müddət təsdiqlənmir."
        ),
        "UNSUPPORTED_CRITERION": (
            "Bu meyar avtomatik qiymətləndirilmir və insan baxışı tələb edir."
        ),
    }
    return templates.get(
        reason_code,
        "Nəticə CV-dəki təsdiqlənən məlumatlara əsasən müəyyən edilib.",
    )


JOB_STATUS_LABELS = {
    "ACTIVE": "Aktiv",
    "ARCHIVED": "Arxivləşdirilib",
}

# Slice 2 (issue #31) — HR-facing text for meyar.agent.schemas.AgentTurnOutcome.
# Deterministic, never model-authored, mirroring PLANNER_OUTCOME_TEXT above.
# "ANSWERED" is fixed server-owned copy. AgentDecision has no free-text
# response field; its closed response code is mapped to server copy in the
# orchestration service. A tool result
# with no model framing (search-loop or profile/evidence success) uses
# "ANSWERED_FROM_TOOL_RESULT" below instead — never plain "ANSWERED" with
# no message (see D-036: this was the root cause of a real bug where a
# fatal-looking outcome co-rendered with valid grounded results).
AGENT_TURN_OUTCOME_TEXT: dict[str, str] = {
    "ANSWERED": "Sorğu tamamlandı.",
    "ANSWERED_FROM_TOOL_RESULT": "Nəticələr aşağıdadır.",
    "CLARIFICATION_REQUESTED": "Aydınlaşdırma tələb olunur",
    "CANDIDATE_REF_NOT_FOUND": (
        "Göstərilən namizəd tapılmadı — əvvəlcə axtarış nəticələrindən birini seçin."
    ),
    "JOB_DRAFT_FAILED": (
        "Bu elandan kriteriya qaralaması hazırlana bilmədi. Mətni bir az fərqli "
        "şəkildə yenidən göndərin və ya vakansiyanı əl ilə yaradın."
    ),
    "TOOL_CALL_LIMIT_EXCEEDED": (
        "Bu sorğu üçün icazə verilən addım sayı aşıldı. Sorğunu sadələşdirib yenidən cəhd edin."
    ),
    "AGENT_PROVIDER_FAILURE": (
        "MEYAR AI xidməti hazırda əlçatan deyil. Bir qədər sonra yenidən cəhd edin."
    ),
    "MALFORMED_MODEL_OUTPUT": (
        "AI xidmətinin cavabını təhlükəsiz şəkildə emal etmək mümkün olmadı. Sorğunu daha "
        "konkret ifadə edib yenidən cəhd edin."
    ),
}


def agent_turn_outcome_message(outcome: str, message: str | None) -> str:
    """Render trusted server-owned text, else a fixed outcome fallback.

    ``message`` is never model prose: it is fixed copy selected from a
    closed response code or a server-built GroundedSelection rendering.
    Legacy persisted assistant text is filtered by the router before it
    reaches this function.
    """
    if message:
        return message
    return AGENT_TURN_OUTCOME_TEXT.get(outcome, "")


# CandidateProfileExtraction category key -> HR-facing label. Mirrors the
# section titles candidate_detail.html already uses for the same six
# categories — reused for the Slice 2 agent's GET_CANDIDATE_EVIDENCE
# result presentation (meyar.ui.service.build_agent_turn_view).
AGENT_EVIDENCE_CATEGORY_LABELS = {
    "skills": "Bacarıq",
    "employment_history": "İş təcrübəsi",
    "education": "Təhsil",
    "certifications": "Sertifikat",
    "languages": "Dil",
    "projects": "Layihə",
    "skill_experience": "Bacarıq təcrübəsi",
    "domain_experience": "Sahə təcrübəsi",
}

STATE_LABELS = {
    "UNAVAILABLE": "Əlçatan deyil",
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
    if (
        plan.outcome == PlannerOutcome.UNSUPPORTED_SEMANTICS
        and PlannerReasonCode.MODEL_DECLINED_INTERPRETATION in plan.reason_codes
    ):
        title, message = _MODEL_DECLINED_INTERPRETATION_TEXT
    else:
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
