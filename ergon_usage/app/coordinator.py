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

    # -- public API -------------------------------------------------------

    def snapshot(self):
        """Return the sanitized ledger status (no credentials)."""

        return self._ledger.status()

    def request_run(self, reason: str) -> bool:
        """Request a run; return False when a run is active (coalesced)."""

        if self._run_lock.locked():
            self._pending.set()
            return False
        return True

    async def run_once(self, reason: Reason) -> RunSummary:
        """Perform one full synchronization cycle, serialized."""

        async with self._run_lock:
            return await self._run(reason)

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
