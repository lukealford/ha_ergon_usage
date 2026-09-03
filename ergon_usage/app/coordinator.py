"""Resumable coordination of Ergon synchronization, costing, and import.

The coordinator orchestrates one bounded run at a time: rates are recorded
before cost calculation, the rolling three-day window is kept current, and a
bounded batch of the oldest pending backfill days is completed only after
Home Assistant acknowledges the corresponding statistics imports.  Retry,
delay, and randomness are injected so tests never wait on real time.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import logging
import random
from typing import Awaitable, Callable, Literal

from .config import Settings
from .costs import calculate_costs, cost_statistic_points
from .ergon import ErgonClient, FetchResult
from .errors import AuthenticationError, ErgonError
from .home_assistant import HomeAssistantClient, StatisticMetadata
from .ledger import Ledger
from .models import StatisticPoint, TariffRate
from .normalize import BRISBANE, statistic_id

logger = logging.getLogger(__name__)

Reason = Literal["startup", "scheduled", "manual"]

MAX_BACKOFF_SECONDS = 300
STARTUP_JITTER_MIN_SECONDS = 5
STARTUP_JITTER_MAX_SECONDS = 60


@dataclass(frozen=True)
class RunSummary:
    """What one synchronization run observed, sanitized for logs."""

    reason: str
    rates_changed: int
    readings_new: int
    readings_corrected: int
    backfill_days_processed: int
    backfill_days_failed: int
    errors: tuple[str, ...] = field(default_factory=tuple)
    gaps: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RateView:
    """One rate as stored in the ledger, ready for serialization."""

    per_kwh_aud: Decimal
    daily_supply_aud: Decimal | None
    observed_at: datetime
    usage_effective_at: datetime
    supply_effective_at: datetime


@dataclass(frozen=True)
class CostView:
    """Accumulated cost components for one tariff."""

    usage_aud: Decimal
    supply_aud: Decimal


@dataclass(frozen=True)
class CoordinatorStatus:
    """Sanitized status view built from real ledger and run state."""

    phase: Literal["idle", "running", "error"]
    rates: dict[str, RateView]
    rate_periods: dict[str, list[RateView]]
    costs: dict[str, CostView]
    backfill_completed: int
    backfill_total: int
    imports: dict[str, datetime]
    last_run: RunSummary | None
    error: str | None
    gaps: tuple[str, ...]


class Coordinator:
    """Owns the run cycle: rates, rolling usage, bounded backfill, imports."""

    def __init__(
        self,
        settings: Settings,
        ergon: ErgonClient,
        ledger: Ledger,
        home_assistant: HomeAssistantClient,
        *,
        sleep_func: Callable[[float], Awaitable[None]] | None = None,
        random_func: Callable[[], float] | None = None,
        now_func: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._ergon = ergon
        self._ledger = ledger
        self._home_assistant = home_assistant
        self._sleep_func = sleep_func or asyncio.sleep
        self._random_func = random_func or random.random
        self._now_func = now_func or (lambda: datetime.now(timezone.utc))
        self._run_lock = asyncio.Lock()
        self._pending = asyncio.Event()
        self._account_id: str | None = None
        self._tariffs: tuple[str, ...] = ()
        self._last_run: RunSummary | None = None
        self._last_error: str | None = None
        self._run_task: asyncio.Task[RunSummary] | None = None

    # -- public API -------------------------------------------------------

    def snapshot(self) -> CoordinatorStatus:
        """Return the sanitized status view (no credentials)."""

        status = self._ledger.status()
        phase: Literal["idle", "running", "error"] = (
            "error" if self._last_error else "idle"
        )
        if self._run_lock.locked() or (
            self._run_task is not None and not self._run_task.done()
        ):
            phase = "running"
        rates: dict[str, RateView] = {}
        rate_periods: dict[str, list[RateView]] = {}
        costs: dict[str, CostView] = {}
        if self._account_id is not None:
            for tariff in self._tariffs:
                periods = self._ledger.rate_periods(self._account_id, tariff)
                if not periods:
                    continue
                current = periods[-1]
                rates[tariff] = RateView(
                    per_kwh_aud=current.per_kwh_aud,
                    daily_supply_aud=current.daily_supply_aud,
                    observed_at=self._last_observed(self._account_id, tariff),
                    usage_effective_at=current.usage_effective_at,
                    supply_effective_at=current.supply_effective_at,
                )
                rate_periods[tariff] = [
                    RateView(
                        per_kwh_aud=period.per_kwh_aud,
                        daily_supply_aud=period.daily_supply_aud,
                        observed_at=period.usage_effective_at,
                        usage_effective_at=period.usage_effective_at,
                        supply_effective_at=period.supply_effective_at,
                    )
                    for period in periods[:-1]
                ]
                components = self._ledger.cost_components_from(
                    self._account_id, tariff, None
                )
                costs[tariff] = CostView(
                    usage_aud=sum(
                        (component.usage_aud for component in components),
                        Decimal("0"),
                    ),
                    supply_aud=sum(
                        (component.supply_aud for component in components),
                        Decimal("0"),
                    ),
                )
        return CoordinatorStatus(
            phase=phase,
            rates=rates,
            rate_periods=rate_periods,
            costs=costs,
            backfill_completed=status.completed_backfill_days,
            backfill_total=status.completed_backfill_days + len(self._pending_backfill()),
            imports=dict(status.imports),
            last_run=self._last_run,
            error=self._last_error,
            gaps=self._last_run.gaps if self._last_run else (),
        )

    def _last_observed(self, account_id: str, tariff: str) -> datetime:
        periods = self._ledger.rate_periods(account_id, tariff)
        return max(period.usage_effective_at for period in periods)

    def _pending_backfill(self) -> list:
        if self._account_id is None:
            return []
        today = self._today_brisbane()
        start = today - timedelta(days=self._settings.initial_history_days)
        return self._ledger.pending_backfill(start, today)

    def request_run(self, reason: str) -> bool:
        """Request a run; return False when a run is active (coalesced)."""

        if self._run_lock.locked():
            self._pending.set()
            return False
        return True

    def run_now(self, reason: Reason) -> tuple[bool, bool]:
        """Trigger a manual run from the web layer.

        Returns ``(accepted, coalesced)``.  When idle, schedules the run as a
        task and returns ``(True, False)``; when a run is active, records the
        pending request and returns ``(True, True)`` so nothing is lost.
        """

        if self._run_lock.locked():
            self._pending.set()
            return (True, True)
        self._run_task = asyncio.get_running_loop().create_task(
            self.run_once(reason)
        )
        return (True, False)

    async def run_once(self, reason: Reason) -> RunSummary:
        """Perform one full synchronization cycle, serialized."""

        async with self._run_lock:
            summary = await self._run(reason)
            self._last_run = summary
            if summary.errors:
                self._last_error = summary.errors[-1]
            else:
                self._last_error = None
            return summary

    async def serve(self, stop: asyncio.Event) -> None:
        """Jittered startup run, then poll until stopped, honoring coalescing."""

        jitter = STARTUP_JITTER_MIN_SECONDS + self._random_func() * (
            STARTUP_JITTER_MAX_SECONDS - STARTUP_JITTER_MIN_SECONDS
        )
        await self._sleep(jitter)
        await self.run_once("startup")
        while not stop.is_set():
            if self._pending.is_set():
                self._pending.clear()
                await self.run_once("manual")
                continue
            try:
                await self._wait_for(stop, self._settings.poll_interval_hours * 3600)
            except TimeoutError:
                pass
            if stop.is_set():
                return
            await self.run_once("scheduled")

    # -- internals --------------------------------------------------------

    async def _wait_for(self, stop: asyncio.Event, timeout: float) -> None:
        """Wait until ``stop`` is set or ``timeout`` elapses."""

        await asyncio.wait_for(stop.wait(), timeout=timeout)

    async def _sleep(self, seconds: float) -> None:
        await self._sleep_func(seconds)

    async def _inter_request_delay(self) -> None:
        if self._settings.request_delay_seconds > 0:
            await self._sleep(float(self._settings.request_delay_seconds))

    async def _run(self, reason: Reason) -> RunSummary:
        errors: list[str] = []
        rate_boundary: datetime | None = None

        rates = await self._fetch_rates(errors)
        rate_result = None
        if rates:
            rate_result = self._ledger.record_rates(rates)
            if rate_result.changed:
                rate_boundary = rate_result.earliest_affected_boundary

        upsert = await self._sync_rolling(errors, rate_boundary)

        processed, failed = await self._backfill_batch(errors)

        return RunSummary(
            reason=reason,
            rates_changed=rate_result.changed if rate_result else 0,
            readings_new=upsert.new if upsert else 0,
            readings_corrected=upsert.corrected if upsert else 0,
            backfill_days_processed=processed,
            backfill_days_failed=failed,
            errors=tuple(errors),
            gaps=self._report_gaps(),
        )

    async def _fetch_rates(self, errors: list[str]) -> tuple[TariffRate, ...]:
        try:
            rates = await self._ergon.fetch_rates()
        except ErgonError as error:
            errors.append(self._sanitize(error))
            return ()
        if rates:
            self._account_id = rates[0].account_id
            seen: dict[str, None] = {}
            for rate in rates:
                seen.setdefault(rate.tariff, None)
            self._tariffs = tuple(seen)
        return rates

    async def _sync_rolling(
        self, errors: list[str], rate_boundary: datetime | None
    ) -> object | None:
        await self._inter_request_delay()
        try:
            result = await self._ergon.fetch_rolling()
        except ErgonError as error:
            errors.append(self._sanitize(error))
            return None
        self._note_scope(result)
        upsert = self._ledger.upsert_readings(result.readings)
        earliest = _earliest(upsert.earliest_changed, rate_boundary)
        await self._publish(errors, earliest)
        return upsert

    async def _backfill_batch(self, errors: list[str]) -> tuple[int, int]:
        today = self._today_brisbane()
        start = today - timedelta(days=self._settings.initial_history_days)
        pending = self._ledger.pending_backfill(start, today)
        batch = sorted(pending)[: self._settings.backfill_batch_days]
        processed = 0
        failed = 0
        for day in batch:
            if not await self._process_backfill_day(day, errors):
                failed += 1
                break  # stop the remaining batch after a failure
            processed += 1
        return processed, failed

    async def _process_backfill_day(self, day: date, errors: list[str]) -> bool:
        # Rates and the rolling window already loaded pages; every Ergon page
        # load is preceded by the configured inter-request delay.
        await self._inter_request_delay()
        result = await self._fetch_day(day, errors)
        if result is None:
            return False
        self._note_scope(result)
        upsert = self._ledger.upsert_readings(result.readings)
        try:
            await self._publish(errors, upsert.earliest_changed)
        except ErgonError:
            # Errors are already recorded by _import; the day's checkpoint is
            # written only after Home Assistant acknowledges every import.
            return False
        self._ledger.complete_backfill(day)
        return True

    async def _fetch_day(self, day: date, errors: list[str]) -> FetchResult | None:
        attempt = 0
        while True:
            try:
                return await self._ergon.fetch_day(day)
            except AuthenticationError as error:
                errors.append(self._sanitize(error))
                return None
            except ErgonError as error:
                if not error.retryable or attempt >= self._settings.retry_limit:
                    errors.append(self._sanitize(error))
                    return None
                await self._sleep(min(MAX_BACKOFF_SECONDS, 2**attempt) + self._random_func())
                attempt += 1

    async def _publish(self, errors: list[str], earliest: datetime | None) -> None:
        """Recalculate and import energy and cost statistics from ``earliest``."""

        if self._account_id is None:
            return
        for tariff in self._tariffs:
            energy_points = self._ledger.points_from(self._account_id, tariff, earliest)
            if energy_points:
                await self._import(
                    errors,
                    StatisticMetadata(
                        statistic_id=statistic_id(self._account_id, tariff),
                        name=f"Ergon {tariff}",
                        unit_class="energy",
                        unit_of_measurement="kWh",
                    ),
                    energy_points,
                )
            await self._publish_costs(errors, tariff, earliest)

    async def _publish_costs(
        self, errors: list[str], tariff: str, earliest: datetime | None
    ) -> None:
        assert self._account_id is not None
        periods = self._ledger.rate_periods(self._account_id, tariff)
        if not periods:
            return
        readings = self._ledger.readings_from(self._account_id, tariff, None)
        components = calculate_costs(readings, periods)
        if earliest is None:
            if not components:
                return
            replace_from = min(component.interval_start for component in components)
        else:
            replace_from = earliest
        applicable = [c for c in components if c.interval_start >= replace_from]
        if not applicable:
            return
        self._ledger.replace_cost_components_from(
            self._account_id, tariff, replace_from, applicable
        )
        all_points = cost_statistic_points(
            self._ledger.cost_components_from(self._account_id, tariff, None)
        )
        cost_points = [p for p in all_points if p.start >= replace_from]
        if cost_points:
            await self._import(
                errors,
                StatisticMetadata(
                    statistic_id=statistic_id(self._account_id, tariff) + "_cost",
                    name=f"Ergon {tariff} cost",
                    unit_class=None,
                    unit_of_measurement=None,
                ),
                cost_points,
            )

    async def _import(
        self, errors: list[str], metadata: StatisticMetadata, points: list[StatisticPoint]
    ) -> None:
        try:
            await self._home_assistant.import_statistics(metadata, points)
        except ErgonError as error:
            errors.append(self._sanitize(error))
            raise
        self._ledger.mark_imported(metadata.statistic_id, points[-1].start)

    def _note_scope(self, result: FetchResult) -> None:
        if self._account_id is None:
            self._account_id = result.account_id
        if not self._tariffs:
            seen: dict[str, None] = {}
            for reading in result.readings:
                seen.setdefault(reading.tariff, None)
            self._tariffs = tuple(seen)

    def _report_gaps(self) -> tuple[str, ...]:
        if self._account_id is None:
            return ()
        gaps: list[str] = []
        for tariff in self._tariffs:
            periods = self._ledger.rate_periods(self._account_id, tariff)
            if not periods:
                continue
            boundary = min(period.usage_effective_at for period in periods)
            unpriced = sum(
                1
                for point in self._ledger.points_from(self._account_id, tariff, None)
                if point.start < boundary
            )
            if unpriced:
                gaps.append(
                    f"{tariff}: {unpriced} interval(s) before first effective rate"
                )
        return tuple(gaps)

    def _today_brisbane(self) -> date:
        return self._now_func().astimezone(BRISBANE).date()

    @staticmethod
    def _sanitize(error: ErgonError) -> str:
        return f"{error.code}: {error.safe_message}"


def _earliest(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)
