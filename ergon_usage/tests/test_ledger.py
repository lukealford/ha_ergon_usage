from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ergon_usage.app.ledger import Ledger
from ergon_usage.app.models import CostComponent, RatePeriod, StatisticPoint, TariffRate, UsageReading


ACCOUNT = "A-TEST"
TARIFF = "Tariff 11"
START = datetime(2026, 9, 1, tzinfo=timezone.utc)


def reading(hour: int, kwh: str) -> UsageReading:
    return UsageReading(ACCOUNT, TARIFF, START + timedelta(hours=hour), Decimal(kwh))


def component(hour: int, usage: str, supply: str = "0") -> CostComponent:
    return CostComponent(ACCOUNT, TARIFF, START + timedelta(hours=hour), Decimal(usage), Decimal(supply))


@pytest.fixture
def ledger(tmp_path):
    instance = Ledger.open(tmp_path / "ergon_usage.sqlite3")
    yield instance
    instance.close()


def test_new_readings_are_persisted_with_decimal_cumulative_sums(ledger):
    result = ledger.upsert_readings([reading(0, "1.20"), reading(1, "2.35")])

    assert (result.new, result.unchanged, result.corrected, result.earliest_changed) == (
        2,
        0,
        0,
        START,
    )
    assert ledger.points_from(ACCOUNT, TARIFF, None) == [
        StatisticPoint(START, Decimal("1.20")),
        StatisticPoint(START + timedelta(hours=1), Decimal("3.55")),
    ]


def test_repeated_readings_are_unchanged(ledger):
    ledger.upsert_readings([reading(0, "1.20")])

    result = ledger.upsert_readings([reading(0, "1.20")])

    assert (result.new, result.unchanged, result.corrected, result.earliest_changed) == (0, 1, 0, None)
    assert [point.sum for point in ledger.points_from(ACCOUNT, TARIFF, None)] == [Decimal("1.20")]


def test_correction_recalculates_all_later_sums(ledger):
    ledger.upsert_readings([reading(0, "1.0"), reading(1, "2.0"), reading(2, "3.0")])

    changed = ledger.upsert_readings([reading(1, "2.5")])

    assert changed.corrected == 1
    assert [point.sum for point in ledger.points_from(ACCOUNT, TARIFF, changed.earliest_changed)] == [
        Decimal("3.5"),
        Decimal("6.5"),
    ]


def test_missing_hour_is_not_zero_filled(ledger):
    ledger.upsert_readings([reading(0, "1.0"), reading(2, "3.0")])

    points = ledger.points_from(ACCOUNT, TARIFF, None)

    assert [(point.start.hour, point.sum) for point in points] == [
        (0, Decimal("1.0")),
        (2, Decimal("4.0")),
    ]


def test_rate_observations_produce_effective_periods_and_changed_boundaries(ledger):
    observed = START + timedelta(minutes=15)
    rate = TariffRate(ACCOUNT, TARIFF, observed, Decimal("0.28895"), Decimal("1.80508"))

    first = ledger.record_rates([rate])
    repeated = ledger.record_rates([rate])
    per_kwh_observed = START + timedelta(hours=2, minutes=15)
    per_kwh_changed = ledger.record_rates(
        [TariffRate(ACCOUNT, TARIFF, per_kwh_observed, Decimal("0.30000"), Decimal("1.80508"))]
    )
    supply_observed = START + timedelta(hours=4, minutes=15)
    supply_changed = ledger.record_rates(
        [TariffRate(ACCOUNT, TARIFF, supply_observed, Decimal("0.30000"), Decimal("2.00000"))]
    )

    assert (first.changed, first.unchanged, first.earliest_changed) == (1, 0, START + timedelta(hours=1))
    assert (repeated.changed, repeated.unchanged, repeated.earliest_changed) == (0, 1, None)
    assert (per_kwh_changed.changed, per_kwh_changed.unchanged, per_kwh_changed.earliest_changed) == (
        1,
        0,
        START + timedelta(hours=3),
    )
    assert (supply_changed.changed, supply_changed.unchanged, supply_changed.earliest_changed) == (
        1,
        0,
        START + timedelta(hours=14),
    )
    assert ledger.rate_periods(ACCOUNT, TARIFF) == [
        RatePeriod(ACCOUNT, TARIFF, START + timedelta(hours=1), START + timedelta(hours=14), Decimal("0.28895"), Decimal("1.80508")),
        RatePeriod(ACCOUNT, TARIFF, START + timedelta(hours=3), START + timedelta(hours=14), Decimal("0.30000"), Decimal("1.80508")),
        RatePeriod(ACCOUNT, TARIFF, START + timedelta(hours=5), START + timedelta(hours=14), Decimal("0.30000"), Decimal("2.00000")),
    ]


def test_rate_periods_persist_after_reopening(ledger, tmp_path):
    observed = START + timedelta(minutes=15)
    ledger.record_rates([TariffRate(ACCOUNT, TARIFF, observed, Decimal("0.28895"), None)])
    ledger.close()
    reopened = Ledger.open(tmp_path / "ergon_usage.sqlite3")

    try:
        assert reopened.rate_periods(ACCOUNT, TARIFF) == [
            RatePeriod(ACCOUNT, TARIFF, START + timedelta(hours=1), START + timedelta(hours=14), Decimal("0.28895"), None)
        ]
    finally:
        reopened.close()


def test_equivalent_rate_decimal_scales_are_unchanged(ledger):
    observed = START + timedelta(minutes=15)
    ledger.record_rates([TariffRate(ACCOUNT, TARIFF, observed, Decimal("0.2"), Decimal("1.80"))])

    result = ledger.record_rates(
        [TariffRate(ACCOUNT, TARIFF, observed, Decimal("0.20"), Decimal("1.800"))]
    )

    assert (result.changed, result.unchanged, result.earliest_changed) == (0, 1, None)


def test_cost_replacement_removes_stale_later_components_and_preserves_earlier_ones(ledger):
    ledger.replace_cost_components_from(
        ACCOUNT,
        TARIFF,
        START,
        [component(0, "0.10"), component(1, "0.20"), component(2, "0.30")],
    )

    ledger.replace_cost_components_from(
        ACCOUNT,
        TARIFF,
        START + timedelta(hours=1),
        [component(1, "0.25")],
    )

    assert ledger.cost_components_from(ACCOUNT, TARIFF, None) == [
        component(0, "0.10"),
        component(1, "0.25"),
    ]


def test_backfill_checkpoint_and_import_progress_resume_after_reopening(ledger, tmp_path):
    ledger.complete_backfill(date(2026, 9, 2))
    ledger.mark_imported("ergon:a_test_tariff_11", START + timedelta(hours=2))
    ledger.close()
    reopened = Ledger.open(tmp_path / "ergon_usage.sqlite3")

    try:
        assert reopened.pending_backfill(date(2026, 9, 1), date(2026, 9, 4)) == [
            date(2026, 9, 1),
            date(2026, 9, 3),
        ]
        assert reopened.status().imports == {"ergon:a_test_tariff_11": START + timedelta(hours=2)}
    finally:
        reopened.close()


def test_forward_models_are_frozen_and_validate_utc_datetimes_and_decimal_money():
    period = RatePeriod(
        ACCOUNT,
        TARIFF,
        START,
        START + timedelta(days=1),
        Decimal("0.28895"),
        None,
    )
    cost = component(0, "0.10", "1.80")

    assert period.per_kwh_aud == Decimal("0.28895")
    assert period.daily_supply_aud is None
    assert cost.supply_aud == Decimal("1.80")
    with pytest.raises((AttributeError, TypeError)):
        cost.usage_aud = Decimal("0.20")
    with pytest.raises(ValueError):
        RatePeriod(ACCOUNT, TARIFF, datetime(2026, 9, 1), START, Decimal("0.2"), Decimal("1"))
    with pytest.raises(TypeError):
        RatePeriod(ACCOUNT, TARIFF, START, START, Decimal("0.2"), 1.0)
    with pytest.raises(TypeError):
        CostComponent(ACCOUNT, TARIFF, START, 0.1, Decimal("0"))
