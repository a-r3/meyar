from datetime import UTC, datetime

import pytest

from meyar.core.business_date import (
    BusinessDateConfigurationError,
    resolve_business_date,
)


def test_business_date_uses_configured_timezone_at_day_boundary() -> None:
    instant = datetime(2026, 1, 1, 20, 30, tzinfo=UTC)
    assert resolve_business_date("Asia/Baku", now=instant).isoformat() == "2026-01-02"
    assert resolve_business_date("UTC", now=instant).isoformat() == "2026-01-01"


def test_business_date_rejects_unknown_timezone_and_naive_time() -> None:
    with pytest.raises(BusinessDateConfigurationError):
        resolve_business_date("Not/A-Timezone")
    with pytest.raises(BusinessDateConfigurationError):
        resolve_business_date("Asia/Baku", now=datetime(2026, 1, 1))
