"""Tests for non-retroactive tariff cost calculation."""

from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from ergon_usage.app.costs import calculate_costs, cost_statistic_points
from ergon_usage.app.models import (
    CostComponent,
    RatePeriod,
    TariffRate,
    UsageReading,
)

BRISBANE = ZoneInfo("Australia/Brisbane")
UTC = timezone.utc

ACCOUNT = "A-TEST123"
TARIFF = "Tariff 11"


def brisbane(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=BRISBANE)


def reading(local: datetime, kwh: str) -> UsageReading:
    return UsageReading(
        account_id=ACCOUNT, tariff=TARIFF, interval_start=local, kwh=Decimal(kwh)
    )


def rate(
    observed: str = "2026-08-31T09:15+10:00",
    per_kwh: str = "0.25",
    daily: str | None = None,
) -> RatePeriod:
    from ergon_usage.app.normalize import (
        effective_supply_boundary,
        effective_usage_boundary,
    )

    observed_at = datetime.fromisoformat(observed)
    tariff_rate = TariffRate(
        account_id=ACCOUNT,
        tariff=TARIFF,
        observed_at=observed_at,
        per_kwh_aud=Decimal(per_kwh),
        daily_supply_aud=Decimal(daily) if daily is not None else None,
    )
    return RatePeriod(
        account_id=tariff_rate.account_id,
        tariff=tariff_rate.tariff,
        usage_effective_at=effective_usage_boundary(observed_at),
        supply_effective_at=effective_supply_boundary(observed_at),
        per_kwh_aud=tariff_rate.per_kwh_aud,
        daily_supply_aud=tariff_rate.daily_supply_aud,
    )


def test_first_rate_never_prices_earlier_usage():
    # 10:00 Brisbane on 2026-08-31 is 00:00 UTC on 2026-09-01.
    readings = [
        reading(brisbane(2026, 8, 31, 8), "1.00"),
        reading(brisbane(2026, 8, 31, 9), "2.00"),
        reading(brisbane(2026, 8, 31, 10), "4.00"),
    ]
    periods = [rate(observed="2026-08-31T09:15+10:00", per_kwh="0.25")]
    components = calculate_costs(readings, periods)
    assert [c.interval_start.astimezone(BRISBANE).hour for c in components] == [10]
    assert components[0].usage_aud == Decimal("1.00")


def test_supply_is_added_once_from_next_midnight():
    readings = [
        reading(brisbane(2026, 8, 31, hour), "1.00") for hour in range(24)
    ]
    periods = [
        rate(observed="2026-08-31T09:15+10:00", per_kwh="0.25", daily="1.80")
    ]
    components = calculate_costs(readings, periods)
    charged = [c for c in components if c.supply_aud]
    assert len(charged) == 1
    assert charged[0].supply_aud == Decimal("1.80")


def test_supply_does_not_depend_on_midnight_energy_reading():
    readings = [
        reading(brisbane(2026, 8, 31, hour), "1.00")
        for hour in range(24)
        if hour != 0
    ]
    periods = [
        rate(observed="2026-08-31T09:15+10:00", per_kwh="0.25", daily="1.80")
    ]
    charged = [c for c in calculate_costs(readings, periods) if c.supply_aud]
    assert len(charged) == 1
    assert charged[0].supply_aud == Decimal("1.80")


def test_rate_change_uses_old_rate_until_new_boundary():
    readings = [
        reading(brisbane(2026, 8, 31, 9), "1.00"),
        reading(brisbane(2026, 8, 31, 10), "1.00"),
    ]
    periods = [
        rate(observed="2026-08-31T00:00+10:00", per_kwh="0.20"),
        rate(observed="2026-08-31T09:15+10:00", per_kwh="0.30"),
    ]
    components = calculate_costs(readings, periods)
    assert [c.usage_aud for c in components] == [Decimal("0.20"), Decimal("0.30")]


def test_intervals_before_first_effective_rate_are_absent():
    readings = [reading(brisbane(2026, 8, 31, 7), "1.00")]
    periods = [rate(observed="2026-08-31T09:15+10:00", per_kwh="0.25")]
    assert calculate_costs(readings, periods) == []


def test_supply_charged_once_per_day_through_newest_usage_date():
    # Two days of readings; newest usage date is 2026-09-01.
    readings = [
        reading(brisbane(2026, 8, 31, hour), "1.00") for hour in range(24)
    ] + [reading(brisbane(2026, 9, 1, 12), "1.00")]
    periods = [
        rate(observed="2026-08-31T09:15+10:00", per_kwh="0.25", daily="1.80")
    ]
    charged = [c for c in calculate_costs(readings, periods) if c.supply_aud]
    dates = {c.interval_start.astimezone(BRISBANE).date() for c in charged}
    assert dates == {
        __import__("datetime").date(2026, 9, 1),
        __import__("datetime").date(2026, 9, 2),
    }
    assert all(c.supply_aud == Decimal("1.80") for c in charged)


def test_supply_charged_without_any_midnight_reading():
    # No reading at midnight at all, yet the eligible date is still charged.
    readings = [reading(brisbane(2026, 8, 31, 14), "1.00")]
    periods = [
        rate(observed="2026-08-31T09:15+10:00", per_kwh="0.25", daily="1.80")
    ]
    charged = [c for c in calculate_costs(readings, periods) if c.supply_aud]
    assert len(charged) == 1
    assert charged[0].interval_start == brisbane(2026, 9, 1, 0)


def test_usage_and_supply_merge_on_same_timestamp():
    # A midnight reading shares the timestamp with the supply charge date.
    readings = [
        reading(brisbane(2026, 8, 31, hour), "1.00") for hour in range(24)
    ] + [reading(brisbane(2026, 9, 1, 0), "1.00")]
    periods = [
        rate(observed="2026-08-31T00:00+10:00", per_kwh="0.25", daily="1.80")
    ]
    components = calculate_costs(readings, periods)
    midnight = [c for c in components if c.interval_start == brisbane(2026, 9, 1, 0)]
    assert len(midnight) == 1
    assert midnight[0].usage_aud == Decimal("0.25")
    assert midnight[0].supply_aud == Decimal("1.80")


def test_periods_with_no_supply_charge_produce_no_supply_component():
    readings = [reading(brisbane(2026, 8, 31, 12), "1.00")]
    periods = [rate(observed="2026-08-31T00:00+10:00", per_kwh="0.25", daily=None)]
    components = calculate_costs(readings, periods)
    assert all(c.supply_aud == Decimal("0") for c in components)


def test_cost_statistic_points_are_cumulative():
    def component(start: datetime, usage: str, supply: str) -> CostComponent:
        return CostComponent(
            account_id=ACCOUNT,
            tariff=TARIFF,
            interval_start=start,
            usage_aud=Decimal(usage),
            supply_aud=Decimal(supply),
        )

    components = [
        component(brisbane(2026, 8, 31, 0), "0.25", "0"),
        component(brisbane(2026, 8, 31, 1), "0.50", "0"),
        component(brisbane(2026, 9, 1, 0), "0.25", "1.80"),
    ]
    points = cost_statistic_points(components)
    assert [p.state for p in points] == [
        Decimal("0.25"),
        Decimal("0.50"),
        Decimal("2.05"),
    ]
    assert [p.sum for p in points] == [
        Decimal("0.25"),
        Decimal("0.75"),
        Decimal("2.80"),
    ]


def test_costs_never_round():
    readings = [reading(brisbane(2026, 8, 31, 12), "0.123456")]
    periods = [rate(observed="2026-08-31T00:00+10:00", per_kwh="0.28895")]
    components = calculate_costs(readings, periods)
    assert components[0].usage_aud == Decimal("0.123456") * Decimal("0.28895")
