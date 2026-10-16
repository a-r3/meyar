"""One boundary rule for the evaluation date (issue #45, D-116).

Deterministic domain services (search, evaluation, scoring, ranking) never read
the wall clock: they receive an explicit ``date``. Adapters call
``effective_business_date`` exactly once per request, pass the result
explicitly down, and return/persist it as the effective date.

- omitted → the configured business "today" (``Settings.business_timezone``);
- supplied → used unchanged, so past/future dates stay reproducible."""

from datetime import date

from meyar.core.business_date import resolve_business_date


def effective_business_date(supplied: date | None, *, timezone_name: str) -> date:
    if supplied is not None:
        return supplied
    return resolve_business_date(timezone_name)
