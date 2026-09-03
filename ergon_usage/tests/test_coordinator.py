"""Coordinator orchestration tests with fake clients and a real temporary ledger."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from ergon_usage.app.coordinator import Coordinator
from ergon_usage.app.errors import AuthenticationError, ExtractionError
from ergon_usage.app.errors import ImportError as ErgonImportError
from ergon_usage.app.ergon import FetchResult
from ergon_usage.app.ledger import Ledger
from ergon_usage.app.models import TariffRate, UsageReading
from ergon_usage.app.normalize import BRISBANE, effective_usage_boundary

UTC = timezone.utc

ACCOUNT = "A-TEST123"
TARIFF = "Tariff 11"

# The default observed rate: 09:15 Brisbane two days ago.  Its effective
# usage boundary is 10:00 Brisbane that day, so earlier backfilled readings
# are deliberately unpriced (gap reporting).
RATE_OBSERVED_LOCAL = datetime(2026, 1, 1, 9, 15, tzinfo=BRISBANE)  # replaced per-test


class _Always:
    """Sentinel marking a failure that repeats on every attempt."""


_ALWAYS = _Always()


def _rate_observed(today: date) -> datetime:
    observed_local = datetime.combine(
        today - timedelta(days=2), time(9, 15), tzinfo=BRISBANE
    )
    return observed_local.astimezone(UTC)


class FakeErgon:
    """Records every call and serves deterministic hourly readings."""

    def __init__(self, history_days: int = 3) -> None:
        self.today = datetime.now(BRISBANE).date()
        self.rate = TariffRate(
            ACCOUNT,
            TARIFF,
            _rate_observed(self.today),
            Decimal("0.30"),
            Decimal("1.00"),
        )
        self.days = [self.today - timedelta(days=d) for d in range(history_days + 1)]
        self.calls: list = []
        self.requested_days: list[date] = []
        self.day_failures: dict[date, list] = {}
        self.corrected: dict[tuple[date, int], str] = {}
        self.extra_rate: TariffRate | None = None

    def readings_for(self, day: date) -> list[UsageReading]:
        readings = []
        for hour in range(24):
            local = datetime(day.year, day.month, day.day, hour, tzinfo=BRISBANE)
            kwh = self.corrected.get((day, hour), "0.1")
            readings.append(
                UsageReading(ACCOUNT, TARIFF, local.astimezone(UTC), Decimal(kwh))
            )
        return readings

    def correct(self, day: date, hour: int, kwh: str) -> None:
        self.corrected[(day, hour)] = kwh

    def change_rate(self, per_kwh: str) -> None:
        # Observed mid-window (yesterday 09:00 Brisbane) so its effective
        # boundary falls inside the existing usage and recalculation has
        # intervals to reprice.
        observed = datetime.combine(
            self.today - timedelta(days=1), time(9, 0), tzinfo=BRISBANE
        ).astimezone(UTC)
        self.extra_rate = TariffRate(
            ACCOUNT,
            TARIFF,
            observed,
            Decimal(per_kwh),
            Decimal("1.00"),
        )

    async def fetch_rates(self) -> tuple[TariffRate, ...]:
        self.calls.append("rates")
        rates = [self.rate]
        if self.extra_rate is not None:
            rates.append(self.extra_rate)
        return tuple(rates)

    async def fetch_rolling(self) -> FetchResult:
        self.calls.append("rolling")
        readings = [r for day in self.days[-3:] for r in self.readings_for(day)]
        return FetchResult(ACCOUNT, tuple(readings), "structured")

    async def fetch_day(self, day: date) -> FetchResult:
        self.calls.append(("day", day))
        self.requested_days.append(day)
        failures = self.day_failures.get(day)
        if failures:
            failure = failures[0]
            if failure is _ALWAYS:
                raise ExtractionError()
            failures.pop(0)
            raise failure
        return FetchResult(ACCOUNT, tuple(self.readings_for(day)), "structured")


@dataclass
class ImportCall:
    metadata: object
    points: tuple


class FakeHA:
    """Records import calls; ``fail_after`` fails the n-th import (1-based)."""

    def __init__(self) -> None:
        self.calls: list[ImportCall] = []
        self.fail_after: int | None = None

    async def import_statistics(self, metadata, points) -> None:
        if self.fail_after is not None and len(self.calls) + 1 == self.fail_after:
            raise ErgonImportError()
        self.calls.append(ImportCall(metadata, tuple(points)))


class FakeSettings:
    def __init__(self, **overrides) -> None:
        self.poll_interval_hours = 12
        self.initial_history_days = 3
        self.backfill_batch_days = 30
        self.request_delay_seconds = 1
        self.retry_limit = 2
        for key, value in overrides.items():
            setattr(self, key, value)


@pytest.fixture
def ledger(tmp_path):
    opened = Ledger.open(tmp_path / "ledger.db")
    yield opened
    opened.close()


@pytest.fixture
def ergon():
    return FakeErgon()


@pytest.fixture
def ha():
    return FakeHA()


def make_coordinator(settings, ledger, ergon, ha) -> Coordinator:
    coordinator = Coordinator(
        settings,
        ergon,
        ledger,
        ha,
        sleep_func=_record_sleep,
        random_func=lambda: 0.5,
    )
    return coordinator


async def _record_sleep(seconds: float) -> None:  # pragma: no cover - replaced
    raise AssertionError("sleep hook not installed")


def make_recording_coordinator(settings, ledger, ergon, ha) -> tuple[Coordinator, list]:
    delays: list[float] = []

    async def sleep_func(seconds: float) -> None:
        delays.append(seconds)

    coordinator = Coordinator(
        settings,
        ergon,
        ledger,
        ha,
        sleep_func=sleep_func,
        random_func=lambda: 0.5,
    )
    return coordinator, delays


def backfill_start(ergon: FakeErgon, days: int) -> date:
    return ergon.today - timedelta(days=days)


def local_interval(day: date, hour: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=BRISBANE).astimezone(UTC)


@pytest.mark.asyncio
async def test_backfill_runs_oldest_first_batch_and_checkpoints(ledger, ergon, ha):
    settings = FakeSettings(initial_history_days=365)
    coordinator, _ = make_recording_coordinator(settings, ledger, ergon, ha)
    summary = await coordinator.run_once("startup")
    assert len(ergon.requested_days) == 30
    assert ergon.requested_days == sorted(ergon.requested_days)
    assert len(ledger.pending_backfill(backfill_start(ergon, 365), ergon.today)) == 335
    assert summary.backfill_days_processed == 30
    assert summary.backfill_days_failed == 0


@pytest.mark.asyncio
async def test_current_data_precedes_bounded_backfill(ledger, ergon, ha):
    coordinator, _ = make_recording_coordinator(FakeSettings(), ledger, ergon, ha)
    await coordinator.run_once("scheduled")
    assert ergon.calls[:2] == ["rates", "rolling"]
    assert ergon.calls[2:] == [("day", day) for day in sorted(ergon.requested_days)]


@pytest.mark.asyncio
async def test_corrected_overlap_reimports_from_earliest_change(ledger, ergon, ha):
    coordinator, _ = make_recording_coordinator(FakeSettings(), ledger, ergon, ha)
    await coordinator.run_once("startup")
    corrected_day = ergon.today - timedelta(days=2)
    ergon.correct(corrected_day, 1, "2.5")
    await coordinator.run_once("scheduled")
    energy_calls = [c for c in ha.calls if c.metadata.unit_class == "energy"]
    assert energy_calls
    last = energy_calls[-1]
    assert last.points[0].start == local_interval(corrected_day, 1)
    # Cumulative across the full ledger after correction:
    # 2.4 (day-3) + 4.8 (corrected day-2) + 2.4 (day-1) = 9.6.
    assert last.points[-1].sum == Decimal("9.6")


@pytest.mark.asyncio
async def test_first_rate_does_not_create_costs_before_boundary(ledger, ergon, ha):
    coordinator, _ = make_recording_coordinator(FakeSettings(), ledger, ergon, ha)
    await coordinator.run_once("startup")
    cost_calls = [c for c in ha.calls if c.metadata.unit_class is None]
    assert cost_calls
    assert all(call.metadata.name == f"Ergon {TARIFF} cost" for call in cost_calls)
    energy_calls = [c for c in ha.calls if c.metadata.unit_class == "energy"]
    assert all(call.metadata.name == f"Ergon {TARIFF}" for call in energy_calls)
    boundary = effective_usage_boundary(_rate_observed(ergon.today))
    assert min(p.start for call in cost_calls for p in call.points) >= boundary


@pytest.mark.asyncio
async def test_rate_change_recalculates_costs_from_new_boundary(ledger, ergon, ha):
    coordinator, _ = make_recording_coordinator(FakeSettings(), ledger, ergon, ha)
    await coordinator.run_once("startup")
    ergon.change_rate("0.35")
    summary = await coordinator.run_once("scheduled")
    assert summary.rates_changed == 1
    assert ergon.extra_rate is not None
    new_boundary = effective_usage_boundary(ergon.extra_rate.observed_at)
    cost_calls = [c for c in ha.calls if c.metadata.unit_class is None]
    latest = cost_calls[-1]
    assert latest.points
    assert min(p.start for p in latest.points) >= new_boundary


@pytest.mark.asyncio
async def test_supply_allocated_once_per_date_from_next_midnight(ledger, ergon, ha):
    coordinator, _ = make_recording_coordinator(FakeSettings(), ledger, ergon, ha)
    await coordinator.run_once("startup")
    cost_calls = [c for c in ha.calls if c.metadata.unit_class is None]
    latest = cost_calls[-1]
    midnights: dict[date, Decimal] = {}
    for point in latest.points:
        local = point.start.astimezone(BRISBANE)
        if (local.hour, local.minute) == (0, 0):
            midnights[local.date()] = point.state
    expected_dates = {ergon.today - timedelta(days=1), ergon.today}
    assert set(midnights) == expected_dates
    # Midnight supply lands on the midnight *ending* each complete date;
    # the newest usage day completes at tomorrow's midnight, which the
    # supply window does not include until that date's usage exists.
    # Supply is charged once per complete date: today-1 completes at
    # today's midnight, today completes at tomorrow's midnight.  The
    # midnight ending today-2 carries usage only (no supply yet).
    today_1 = midnights.pop(ergon.today - timedelta(days=1))
    today_0 = midnights.pop(ergon.today)
    assert today_1 == Decimal("1.030")  # 0.030 usage + 1.00 supply
    assert today_0 == Decimal("1.000")  # supply only, usage already counted


@pytest.mark.asyncio
async def test_transient_retry_backs_off_then_completes(ledger, ergon, ha):
    settings = FakeSettings(retry_limit=2)
    coordinator, delays = make_recording_coordinator(settings, ledger, ergon, ha)
    failing_day = ergon.today - timedelta(days=3)
    ergon.day_failures[failing_day] = [ExtractionError()]
    summary = await coordinator.run_once("startup")
    assert 1.5 in delays  # min(300, 2**0) + uniform(0.5)
    assert summary.errors == ()
    assert summary.backfill_days_failed == 0
    assert ledger.pending_backfill(backfill_start(ergon, 3), ergon.today) == []


@pytest.mark.asyncio
async def test_retry_exhaustion_stops_batch_and_retains_days(ledger, ergon, ha):
    settings = FakeSettings(retry_limit=1)
    coordinator, delays = make_recording_coordinator(settings, ledger, ergon, ha)
    # Backfill pending range is [today - 3, today); today-3 is the oldest.
    # The rolling window already covered today-3..today, but backfill days
    # are only marked complete via complete_backfill, so today-3 is pending.
    failing_day = ergon.today - timedelta(days=3)
    ergon.day_failures[failing_day] = [_ALWAYS]
    summary = await coordinator.run_once("startup")
    assert summary.backfill_days_failed == 1
    assert summary.errors
    remaining = ledger.pending_backfill(backfill_start(ergon, 3), ergon.today)
    # The failing day (oldest) stays pending; batch stopped before the rest.
    assert remaining == [
        ergon.today - timedelta(days=3),
        ergon.today - timedelta(days=2),
        ergon.today - timedelta(days=1),
    ]
    assert [call for call in ergon.calls if call == ("day", failing_day)].count(
        ("day", failing_day)
    ) == 2  # initial attempt + one retry


@pytest.mark.asyncio
async def test_authentication_error_stops_without_retry(ledger, ergon, ha):
    coordinator, delays = make_recording_coordinator(FakeSettings(), ledger, ergon, ha)
    failing_day = ergon.today - timedelta(days=3)
    ergon.day_failures[failing_day] = [AuthenticationError()]
    summary = await coordinator.run_once("startup")
    assert summary.backfill_days_failed == 1
    assert 1.5 not in delays  # no retry backoff happened
    assert failing_day in ledger.pending_backfill(backfill_start(ergon, 3), ergon.today)
    assert summary.errors


@pytest.mark.asyncio
async def test_no_concurrent_runs_and_one_coalesced_followup(ledger, ergon, ha):
    coordinator, delays = make_recording_coordinator(FakeSettings(), ledger, ergon, ha)
    stop = asyncio.Event()
    results: list[bool] = []
    requested = False

    async def sleep_func(seconds: float) -> None:
        nonlocal requested
        delays.append(seconds)
        if seconds == 1.0 and not requested:
            requested = True
            results.append(coordinator.request_run("during"))

    coordinator._sleep_func = sleep_func

    original_fetch_rates = ergon.fetch_rates

    async def counting_fetch_rates():
        result = await original_fetch_rates()
        if ergon.calls.count("rates") == 2:
            stop.set()
        return result

    ergon.fetch_rates = counting_fetch_rates

    await asyncio.wait_for(coordinator.serve(stop), timeout=5)
    assert results == [False]
    assert ergon.calls.count("rates") == 2
    assert ergon.calls.count("rolling") == 2


@pytest.mark.asyncio
async def test_gap_reporting_surfaces_unpriced_intervals(ledger, ergon, ha):
    coordinator, _ = make_recording_coordinator(FakeSettings(), ledger, ergon, ha)
    summary = await coordinator.run_once("startup")
    assert summary.gaps
    assert any(TARIFF in gap for gap in summary.gaps)


@pytest.mark.asyncio
async def test_checkpoint_only_after_ha_acknowledgement(ledger, ergon, ha):
    coordinator, _ = make_recording_coordinator(FakeSettings(), ledger, ergon, ha)
    # Rolling publish performs two imports (energy + cost); the third import
    # is the first backfill day's energy import, which must fail.
    ha.fail_after = 3
    summary = await coordinator.run_once("startup")
    assert summary.errors
    assert summary.backfill_days_failed == 1
    assert coordinator.snapshot().completed_backfill_days == 0
    failing_day = ergon.today - timedelta(days=3)
    assert failing_day in ledger.pending_backfill(backfill_start(ergon, 3), ergon.today)
