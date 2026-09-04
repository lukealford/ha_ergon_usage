"""Tests for time-of-use window classification and statistics."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from ergon_usage.app.tou import (
    split_by_window,
    tou_statistic_id,
    tou_statistic_points,
    window_for,
)
from ergon_usage.app.models import UsageReading

BRISBANE = ZoneInfo("Australia/Brisbane")
ACCOUNT = "A-TEST123"
TARIFF = "Tariff 11"


def reading(local: datetime, kwh: str) -> UsageReading:
    return UsageReading(
        account_id=ACCOUNT, tariff=TARIFF, interval_start=local, kwh=Decimal(kwh)
    )


def test_every_hour_maps_to_exactly_one_window():
    # The three windows must tile the full day with no gaps or overlaps.
    day = datetime(2026, 9, 4, 0, 0, tzinfo=BRISBANE)
    for hour in range(24):
        window = window_for(day.replace(hour=hour))
        assert window in ("offpeak", "daytime", "peak")


def test_window_boundaries():
    day = datetime(2026, 9, 4, 0, 0, tzinfo=BRISBANE)
    assert window_for(day.replace(hour=10)) == "offpeak"   # 10:59.. 10am
    assert window_for(day.replace(hour=11)) == "daytime"   # 11am starts daytime
    assert window_for(day.replace(hour=15)) == "daytime"   # 3pm still daytime
    assert window_for(day.replace(hour=16)) == "peak"      # 4pm starts peak
    assert window_for(day.replace(hour=20)) == "peak"      # 8pm still peak
    assert window_for(day.replace(hour=21)) == "offpeak"   # 9pm starts off-peak
    assert window_for(day.replace(hour=0)) == "offpeak"    # midnight off-peak


def test_split_buckets_readings_by_window():
    day = datetime(2026, 9, 4, 0, 0, tzinfo=BRISBANE)
    readings = [
        reading(day.replace(hour=2), "1.0"),    # offpeak
        reading(day.replace(hour=12), "2.0"),   # daytime
        reading(day.replace(hour=18), "4.0"),   # peak
        reading(day.replace(hour=22), "8.0"),   # offpeak
    ]
    buckets = split_by_window(readings)
    assert [r.kwh for r in buckets["offpeak"]] == [Decimal("1.0"), Decimal("8.0")]
    assert [r.kwh for r in buckets["daytime"]] == [Decimal("2.0")]
    assert [r.kwh for r in buckets["peak"]] == [Decimal("4.0")]


def test_tou_points_are_cumulative():
    day = datetime(2026, 9, 4, 0, 0, tzinfo=BRISBANE)
    points = tou_statistic_points(
        [
            reading(day.replace(hour=22), "1.0"),
            reading(day.replace(hour=23), "0.5"),
        ]
    )
    assert [p.sum for p in points] == [Decimal("1.0"), Decimal("1.5")]
    assert [p.state for p in points] == [Decimal("1.0"), Decimal("0.5")]


def test_tou_statistic_id_format():
    assert (
        tou_statistic_id("A-TEST123", "Tariff 11", "offpeak")
        == "ergon:a_test123_tariff_11_offpeak"
    )
