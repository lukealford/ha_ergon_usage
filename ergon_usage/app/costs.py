"""Non-retroactive tariff cost calculation.

Usage cost is assigned per reading interval using the latest rate period
effective at that interval — a newly observed rate never prices usage from
before its effective boundary.  Supply cost is charged once per complete
eligible date (Brisbane local time) through the newest known usage date,
even when that date has no midnight energy reading.  Components sharing an
interval are merged, and costs are never rounded internally.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Sequence

from .models import CostComponent, RatePeriod, StatisticPoint, UsageReading
from .normalize import BRISBANE, UTC

ZERO = Decimal(0)


def _select_period(
    periods: Sequence[RatePeriod], interval_start: datetime
) -> RatePeriod | None:
    """Return the latest period whose usage boundary is not after the interval."""

    best: RatePeriod | None = None
    for period in periods:
        if period.usage_effective_at <= interval_start:
            if best is None or period.usage_effective_at > best.usage_effective_at:
                best = period
    return best


def _supply_dates(period: RatePeriod, newest_usage: datetime) -> list[datetime]:
    """Brisbane midnights for each complete eligible date, UTC values.

    Dates run from the period's supply boundary through the day after the
    newest known usage date (a supply day completes at the following
    midnight), in Brisbane local time.
    """

    if period.daily_supply_aud is None:
        return []
    # The supply boundary is already the first Brisbane midnight strictly
    # after observation; that midnight opens the first chargeable date.
    start_local = period.supply_effective_at.astimezone(BRISBANE)
    end_local = newest_usage.astimezone(BRISBANE)
    last_chargeable = datetime(
        end_local.year, end_local.month, end_local.day, tzinfo=BRISBANE
    ) + timedelta(days=1)
    dates: list[datetime] = []
    current = start_local.replace(hour=0, minute=0, second=0, microsecond=0)
    while current <= last_chargeable:
        dates.append(current.astimezone(UTC))
        current += timedelta(days=1)
    return dates


def calculate_costs(
    readings: Sequence[UsageReading], periods: Sequence[RatePeriod]
) -> list[CostComponent]:
    """Merge per-interval usage cost and daily supply cost into components."""

    if readings:
        newest_usage = max(reading.interval_start for reading in readings)
    else:
        newest_usage = None

    components: dict[datetime, CostComponent] = {}

    for reading in readings:
        period = _select_period(periods, reading.interval_start)
        if period is None:
            continue
        usage_aud = reading.kwh * period.per_kwh_aud
        existing = components.get(reading.interval_start)
        if existing is None:
            components[reading.interval_start] = CostComponent(
                account_id=reading.account_id,
                tariff=reading.tariff,
                interval_start=reading.interval_start,
                usage_aud=usage_aud,
                supply_aud=ZERO,
            )
        else:
            components[reading.interval_start] = CostComponent(
                account_id=existing.account_id,
                tariff=existing.tariff,
                interval_start=existing.interval_start,
                usage_aud=existing.usage_aud + usage_aud,
                supply_aud=existing.supply_aud,
            )

    if newest_usage is not None:
        for period in periods:
            if period.daily_supply_aud is None:
                continue
            for midnight in _supply_dates(period, newest_usage):
                existing = components.get(midnight)
                supply_aud = period.daily_supply_aud
                if existing is None:
                    components[midnight] = CostComponent(
                        account_id=period.account_id,
                        tariff=period.tariff,
                        interval_start=midnight,
                        usage_aud=ZERO,
                        supply_aud=supply_aud,
                    )
                else:
                    components[midnight] = CostComponent(
                        account_id=existing.account_id,
                        tariff=existing.tariff,
                        interval_start=existing.interval_start,
                        usage_aud=existing.usage_aud,
                        supply_aud=existing.supply_aud + supply_aud,
                    )

    return [components[key] for key in sorted(components)]


def cost_statistic_points(
    components: Sequence[CostComponent],
) -> list[StatisticPoint]:
    """Build cumulative statistic points from ordered cost components."""

    points: list[StatisticPoint] = []
    cumulative = ZERO
    for component in sorted(components, key=lambda item: item.interval_start):
        state = component.usage_aud + component.supply_aud
        cumulative += state
        points.append(
            StatisticPoint(start=component.interval_start, sum=cumulative, state=state)
        )
    return points
