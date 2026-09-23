from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class BusinessDateConfigurationError(ValueError):
    pass


def resolve_business_date(timezone_name: str, *, now: datetime | None = None) -> date:
    """Resolve one trusted business date at an application boundary."""
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise BusinessDateConfigurationError(
            f"Unknown business timezone: {timezone_name}"
        ) from exc
    instant = now or datetime.now(UTC)
    if instant.tzinfo is None:
        raise BusinessDateConfigurationError("Business-date resolution requires an aware time.")
    return instant.astimezone(timezone).date()
