from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from ergon_usage.app.errors import AccountDiscoveryError
from ergon_usage.app.models import StatisticPoint, TariffRate, UsageReading
from ergon_usage.app.normalize import (
    discover_single_account,
    parse_brisbane_timestamp,
    statistic_id,
)


def test_timestamp_is_aware_utc():
    result = parse_brisbane_timestamp("31 Aug 2026 07:00PM")
    assert result.isoformat() == "2026-08-31T09:00:00+00:00"


def test_statistic_id_is_stable_and_account_scoped():
    assert statistic_id("A-1A5FAA4B", "Tariff 33") == "ergon:a_1a5faa4b_tariff_33"


def test_discovery_rejects_multiple_accounts():
    with pytest.raises(AccountDiscoveryError):
        discover_single_account(["/portal/A-ONE/x", "/portal/A-TWO/x"])


def test_discovery_requires_exactly_one_account_and_ignores_unrelated_urls():
    assert discover_single_account(
        ["https://example/portal/A-ONE/x", "https://example/portal/A-ONE/y", "/login"]
    ) == "A-ONE"
    with pytest.raises(AccountDiscoveryError):
        discover_single_account(["/login"])


def test_domain_models_are_frozen_and_normalize_to_utc():
    brisbane = datetime(2026, 8, 31, 19, tzinfo=__import__("zoneinfo").ZoneInfo("Australia/Brisbane"))
    reading = UsageReading("A-ONE", "Tariff 11", brisbane, Decimal("1.25"))
    assert reading.interval_start == datetime(2026, 8, 31, 9, tzinfo=timezone.utc)
    with pytest.raises(Exception):
        reading.kwh = Decimal("2")


def test_domain_models_reject_invalid_values():
    aware = datetime(2026, 8, 31, 9, tzinfo=timezone.utc)
    for value in (Decimal("-1"), Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(ValueError):
            UsageReading("A-ONE", "Tariff 11", aware, value)
    with pytest.raises(ValueError):
        UsageReading("A-ONE", "  ", aware, Decimal("1"))
    with pytest.raises(ValueError):
        UsageReading("A-ONE", "Tariff 11", datetime(2026, 8, 31, 9), Decimal("1"))
    with pytest.raises(TypeError):
        UsageReading("A-ONE", "Tariff 11", aware, 1.0)
    with pytest.raises(ValueError):
        TariffRate("A-ONE", "Tariff 11", aware, Decimal("1"), Decimal("-0.1"))
    with pytest.raises(ValueError):
        StatisticPoint(aware, Decimal("NaN"))


def test_timestamp_formats_are_strict_and_invalid_values_rejected():
    assert parse_brisbane_timestamp("31/08/2026 07:00 PM").isoformat() == "2026-08-31T09:00:00+00:00"
    with pytest.raises(ValueError):
        parse_brisbane_timestamp("2026-08-31 19:00:00")


def test_timestamp_can_be_checked_against_requested_brisbane_day():
    from ergon_usage.app.normalize import parse_brisbane_timestamp_for_day

    parse_brisbane_timestamp_for_day("31 Aug 2026 07:00PM", date(2026, 8, 31))
    with pytest.raises(ValueError):
        parse_brisbane_timestamp_for_day("01 Sep 2026 07:00PM", date(2026, 8, 31))
