"""Time-of-use (ToU) classification and statistics for Tariff 11.

Ergon's Tariff 11 on a ToU plan prices three windows per day (Brisbane
local time).  The windows tile the full day, so every hourly reading
belongs to exactly one window:

- ``offpeak``  9:00pm  → 11:00am (next morning)
- ``daytime``  11:00am → 4:00pm
- ``peak``     4:00pm  → 9:00pm

For each window a sibling cumulative statistic is published, e.g.
``ergon:a_xxx_tariff_11_offpeak``.  Tariff 33 (controlled load) is never
split: it is a single flat rate and stays as its own statistic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from typing import Callable, Sequence

from .models import StatisticPoint, UsageReading
from .normalize import BRISBANE, statistic_id

OFFPEAK_START = time(21, 0)   # 9:00pm
DAYTIME_START = time(11, 0)   # 11:00am
PEAK_START = time(16, 0)      # 4:00pm

WINDOWS: tuple[str, ...] = ("offpeak", "daytime", "peak")


@dataclass(frozen=True)
class TouWindow:
    """A window label plus its Brisbane-local start and end times."""

    label: str
    start: time
    end: time


TOU_WINDOWS: dict[str, TouWindow] = {
    # Off-peak wraps midnight: 9pm -> 11am.
    "offpeak": TouWindow("offpeak", OFFPEAK_START, DAYTIME_START),
    "daytime": TouWindow("daytime", DAYTIME_START, PEAK_START),
    "peak": TouWindow("peak", PEAK_START, OFFPEAK_START),
}


def window_for(interval_start: datetime) -> str:
    """Return the ToU window label for a reading interval (Brisbane local)."""

    local_hour = interval_start.astimezone(BRISBANE).time()
    if DAYTIME_START <= local_hour < PEAK_START:
        return "daytime"
    if PEAK_START <= local_hour < OFFPEAK_START:
        return "peak"
    return "offpeak"


def split_by_window(
    readings: Sequence[UsageReading],
) -> dict[str, list[UsageReading]]:
    """Group Tariff 11 readings by ToU window."""

    buckets: dict[str, list[UsageReading]] = {label: [] for label in WINDOWS}
    for reading in readings:
        buckets[window_for(reading.interval_start)].append(reading)
    return buckets


def tou_statistic_points(
    readings: Sequence[UsageReading],
) -> list[StatisticPoint]:
    """Build a cumulative statistic from window-grouped readings.

    Readings must all belong to the same window (the caller splits first);
    points are ordered and cumulative exactly like ``ledger.points_from``.
    """

    ordered = sorted(readings, key=lambda r: r.interval_start)
    cumulative = Decimal("0")
    points: list[StatisticPoint] = []
    for reading in ordered:
        cumulative += reading.kwh
        points.append(StatisticPoint(reading.interval_start, cumulative, reading.kwh))
    return points


def tou_statistic_id(account_id: str, tariff: str, window: str) -> str:
    """Stable statistic ID for one ToU window, e.g. ``ergon:..._tariff_11_offpeak``."""

    if window not in WINDOWS:
        raise ValueError(f"Unknown ToU window: {window}")
    return statistic_id(account_id, tariff) + f"_{window}"


def tou_cost_points(
    window_readings: Sequence[UsageReading],
    rate_for: "Callable[[datetime], Decimal]",
) -> list[StatisticPoint]:
    """Build a cumulative cost statistic for one ToU window.

    ``rate_for`` maps an interval start to the $/kWh in effect (the caller
    applies the same period-selection and backfill rules as the main cost
    calculation, so window costs are consistent with the tariff total).
    """

    cumulative = Decimal("0")
    points: list[StatisticPoint] = []
    for reading in sorted(window_readings, key=lambda r: r.interval_start):
        cumulative += reading.kwh * rate_for(reading.interval_start)
        points.append(StatisticPoint(reading.interval_start, cumulative))
    return points
