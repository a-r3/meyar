import hashlib

# Bump whenever build_professional_embedding_text's output shape
# changes — every CandidateEmbeddingVersion persists the exact
# serializer version used, mirroring PROMPT_VERSION/SCHEMA_VERSION.
SERIALIZER_VERSION = "candidate-professional-embedding-text-v1"


def build_professional_embedding_text(profile_content: dict) -> str:
    """Deterministically serializes a CandidateProfileVersion's
    profile_content (a CandidateProfileExtraction.model_dump()) into a
    normalized text suitable for embedding. Same profile_content ->
    same text -> same SHA-256 (see compute_source_sha256), independent
    of any incidental dict/JSON key ordering. Reads ONLY the fixed
    professional-fact fields defined on CandidateProfileExtraction —
    that schema has no name/email/phone field to begin with (extra=
    "forbid"), so identity data cannot enter this text even accidentally.
    Deliberately omits raw evidence quotes (they can contain verbatim CV
    text such as a header line with the candidate's own contact details)
    — only normalized structured facts are included."""
    lines: list[str] = []

    lines.append("SKILLS:")
    for item in profile_content.get("skills", []) or []:
        name = item.get("name", "")
        category = item.get("category")
        lines.append(f"- {name} ({category})" if category else f"- {name}")

    lines.append("EMPLOYMENT HISTORY:")
    for item in profile_content.get("employment_history", []) or []:
        title = item.get("title", "")
        organization = item.get("organization")
        start_date = item.get("start_date")
        end_date = item.get("end_date")
        is_current = item.get("is_current", False)
        piece = title
        if organization:
            piece += f" at {organization}"
        span_end = end_date or ("present" if is_current else None)
        if start_date or span_end:
            piece += f" ({start_date or '?'} - {span_end or '?'})"
        lines.append(f"- {piece}")

    lines.append("EDUCATION:")
    for item in profile_content.get("education", []) or []:
        institution = item.get("institution")
        degree = item.get("degree")
        field_of_study = item.get("field_of_study")
        date = item.get("date")
        parts = [p for p in (degree, field_of_study, institution) if p]
        piece = ", ".join(parts)
        if date:
            piece += f" ({date})"
        lines.append(f"- {piece}")

    lines.append("CERTIFICATIONS:")
    for item in profile_content.get("certifications", []) or []:
        name = item.get("name", "")
        issuer = item.get("issuer")
        date = item.get("date")
        piece = name
        if issuer:
            piece += f" — {issuer}"
        if date:
            piece += f" ({date})"
        lines.append(f"- {piece}")

    lines.append("LANGUAGES:")
    for item in profile_content.get("languages", []) or []:
        language = item.get("language", "")
        proficiency = item.get("proficiency")
        lines.append(f"- {language} ({proficiency})" if proficiency else f"- {language}")

    lines.append("PROJECTS:")
    for item in profile_content.get("projects", []) or []:
        lines.append(f"- {item.get('description', '')}")

    return "\n".join(lines)


def compute_source_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
