from meyar.extraction.redaction import redact_block_text


def test_email_is_redacted() -> None:
    text = "Contact: jane.doe@example.com for details."
    result = redact_block_text(text)
    assert "jane.doe@example.com" not in result
    assert "[REDACTED_EMAIL]" in result


def test_phone_is_redacted() -> None:
    text = "Mobile: +994 50 123 45 67"
    result = redact_block_text(text)
    assert "994 50 123 45 67" not in result
    assert "[REDACTED_PHONE]" in result


def test_phone_without_separators_is_redacted() -> None:
    text = "Call me at 0501234567 anytime"
    result = redact_block_text(text)
    assert "0501234567" not in result
    assert "[REDACTED_PHONE]" in result


def test_employment_date_range_is_not_redacted() -> None:
    text = "Backend Developer — Python — 2021-2025"
    result = redact_block_text(text)
    assert "2021-2025" in result
    assert "[REDACTED_PHONE]" not in result


def test_year_to_present_range_is_not_redacted() -> None:
    text = "Senior Engineer, 2019 - Present"
    result = redact_block_text(text)
    assert "2019" in result
    assert "Present" in result
    assert "[REDACTED_PHONE]" not in result


def test_labeled_dob_is_redacted() -> None:
    text = "Date of Birth: 1990-05-12"
    result = redact_block_text(text)
    assert "1990-05-12" not in result
    assert "[REDACTED_FIELD]" in result


def test_labeled_gender_is_redacted() -> None:
    text = "Gender: Female"
    result = redact_block_text(text)
    assert "Female" not in result
    assert "[REDACTED_FIELD]" in result


def test_labeled_marital_status_is_redacted() -> None:
    text = "Marital Status: Married"
    result = redact_block_text(text)
    assert "Married" not in result
    assert "[REDACTED_FIELD]" in result


def test_labeled_religion_is_redacted() -> None:
    text = "Religion: Not disclosed"
    result = redact_block_text(text)
    assert "[REDACTED_FIELD]" in result


def test_unrelated_professional_text_survives_untouched() -> None:
    text = "Skills: Python, FastAPI, PostgreSQL, Docker"
    assert redact_block_text(text) == text
