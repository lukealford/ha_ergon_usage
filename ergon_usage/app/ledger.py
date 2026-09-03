"""Transactional SQLite persistence for readings and synchronization progress."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
from typing import Iterator, Sequence

from .models import CostComponent, RatePeriod, StatisticPoint, TariffRate, UsageReading


def _utc_datetime(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware.")
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return _utc_datetime(value, "timestamp").isoformat()


def _from_timestamp(value: str) -> datetime:
    return _utc_datetime(datetime.fromisoformat(value), "timestamp")


def _require_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not (cleaned := value.strip()):
        raise ValueError(f"{field_name} must be a non-empty string.")
    return cleaned


def _require_day(value: date, field_name: str) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{field_name} must be a date.")
    return value


@dataclass(frozen=True, slots=True)
class UpsertResult:
    new: int
    unchanged: int
    corrected: int
    earliest_changed: datetime | None


@dataclass(frozen=True, slots=True)
class RateUpsertResult:
    changed: int
    unchanged: int
    earliest_changed: datetime | None

    @property
    def earliest_affected_boundary(self) -> datetime | None:
        """The stored observation boundary; Task 5 will resolve effective boundaries."""

        return self.earliest_changed


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    imports: dict[str, datetime]
    completed_backfill_days: int


class Ledger:
    """Own the durable, atomic state used by each Ergon synchronization run."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    @classmethod
    def open(cls, path: Path) -> "Ledger":
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS readings (
                account_id TEXT NOT NULL,
                tariff TEXT NOT NULL,
                interval_start TEXT NOT NULL,
                kwh TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (account_id, tariff, interval_start)
            );
            CREATE TABLE IF NOT EXISTS tariff_rates (
                account_id TEXT NOT NULL,
                tariff TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                per_kwh_aud TEXT NOT NULL,
                daily_supply_aud TEXT,
                PRIMARY KEY (account_id, tariff, observed_at)
            );
            CREATE TABLE IF NOT EXISTS cost_components (
                account_id TEXT NOT NULL,
                tariff TEXT NOT NULL,
                interval_start TEXT NOT NULL,
                usage_aud TEXT NOT NULL,
                supply_aud TEXT NOT NULL,
                PRIMARY KEY (account_id, tariff, interval_start)
            );
            CREATE TABLE IF NOT EXISTS backfill_days (
                day TEXT PRIMARY KEY,
                completed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS imports (
                statistic_id TEXT PRIMARY KEY,
                through_timestamp TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runtime_status (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        return cls(connection)

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def upsert_readings(self, readings: Sequence[UsageReading]) -> UpsertResult:
        new = unchanged = corrected = 0
        earliest_changed: datetime | None = None
        with self._transaction():
            for reading in readings:
                if not isinstance(reading, UsageReading):
                    raise TypeError("readings must contain UsageReading values.")
                row = self._connection.execute(
                    """
                    SELECT kwh FROM readings
                    WHERE account_id = ? AND tariff = ? AND interval_start = ?
                    """,
                    (reading.account_id, reading.tariff, _timestamp(reading.interval_start)),
                ).fetchone()
                if row is None:
                    new += 1
                    self._connection.execute(
                        """
                        INSERT INTO readings (account_id, tariff, interval_start, kwh, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            reading.account_id,
                            reading.tariff,
                            _timestamp(reading.interval_start),
                            str(reading.kwh),
                            datetime.now(timezone.utc).isoformat(),
                        ),
                    )
                    earliest_changed = _earlier(earliest_changed, reading.interval_start)
                elif Decimal(row["kwh"]) == reading.kwh:
                    unchanged += 1
                else:
                    corrected += 1
                    self._connection.execute(
                        """
                        UPDATE readings SET kwh = ?, updated_at = ?
                        WHERE account_id = ? AND tariff = ? AND interval_start = ?
                        """,
                        (
                            str(reading.kwh),
                            datetime.now(timezone.utc).isoformat(),
                            reading.account_id,
                            reading.tariff,
                            _timestamp(reading.interval_start),
                        ),
                    )
                    earliest_changed = _earlier(earliest_changed, reading.interval_start)
        return UpsertResult(new, unchanged, corrected, earliest_changed)

    def points_from(
        self, account_id: str, tariff: str, earliest: datetime | None
    ) -> list[StatisticPoint]:
        account_id = _require_text(account_id, "account_id")
        tariff = _require_text(tariff, "tariff")
        earliest_timestamp = _timestamp(earliest) if earliest is not None else None
        rows = self._connection.execute(
            """
            SELECT interval_start, kwh FROM readings
            WHERE account_id = ? AND tariff = ?
            ORDER BY interval_start
            """,
            (account_id, tariff),
        ).fetchall()
        cumulative = Decimal("0")
        points: list[StatisticPoint] = []
        for row in rows:
            cumulative += Decimal(row["kwh"])
            if earliest_timestamp is None or row["interval_start"] >= earliest_timestamp:
                points.append(StatisticPoint(_from_timestamp(row["interval_start"]), cumulative))
        return points

    def record_rates(self, rates: Sequence[TariffRate]) -> RateUpsertResult:
        changed = unchanged = 0
        earliest_changed: datetime | None = None
        with self._transaction():
            for rate in rates:
                if not isinstance(rate, TariffRate):
                    raise TypeError("rates must contain TariffRate values.")
                observed_at = _timestamp(rate.observed_at)
                row = self._connection.execute(
                    """
                    SELECT per_kwh_aud, daily_supply_aud FROM tariff_rates
                    WHERE account_id = ? AND tariff = ? AND observed_at = ?
                    """,
                    (rate.account_id, rate.tariff, observed_at),
                ).fetchone()
                values = (str(rate.per_kwh_aud), _decimal_text(rate.daily_supply_aud))
                if row is not None and _rate_values_match(row, rate):
                    unchanged += 1
                    continue
                changed += 1
                self._connection.execute(
                    """
                    INSERT INTO tariff_rates (
                        account_id, tariff, observed_at, per_kwh_aud, daily_supply_aud
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(account_id, tariff, observed_at) DO UPDATE SET
                        per_kwh_aud = excluded.per_kwh_aud,
                        daily_supply_aud = excluded.daily_supply_aud
                    """,
                    (rate.account_id, rate.tariff, observed_at, *values),
                )
                earliest_changed = _earlier(earliest_changed, rate.observed_at)
        return RateUpsertResult(changed, unchanged, earliest_changed)

    def rate_periods(self, account_id: str, tariff: str) -> list[RatePeriod]:
        _require_text(account_id, "account_id")
        _require_text(tariff, "tariff")
        # Tariff observations are deliberately not converted to effective periods
        # here. Their effective boundaries are a Task 5 concern.
        return []

    def cost_components_from(
        self, account_id: str, tariff: str, earliest: datetime | None
    ) -> list[CostComponent]:
        account_id = _require_text(account_id, "account_id")
        tariff = _require_text(tariff, "tariff")
        earliest_timestamp = _timestamp(earliest) if earliest is not None else None
        query = """
            SELECT interval_start, usage_aud, supply_aud FROM cost_components
            WHERE account_id = ? AND tariff = ?
        """
        parameters: tuple[str, ...] = (account_id, tariff)
        if earliest_timestamp is not None:
            query += " AND interval_start >= ?"
            parameters += (earliest_timestamp,)
        query += " ORDER BY interval_start"
        rows = self._connection.execute(query, parameters).fetchall()
        return [
            CostComponent(
                account_id,
                tariff,
                _from_timestamp(row["interval_start"]),
                Decimal(row["usage_aud"]),
                Decimal(row["supply_aud"]),
            )
            for row in rows
        ]

    def replace_cost_components_from(
        self,
        account_id: str,
        tariff: str,
        earliest: datetime,
        components: Sequence[CostComponent],
    ) -> None:
        account_id = _require_text(account_id, "account_id")
        tariff = _require_text(tariff, "tariff")
        earliest = _utc_datetime(earliest, "earliest")
        seen: set[datetime] = set()
        for component in components:
            if not isinstance(component, CostComponent):
                raise TypeError("components must contain CostComponent values.")
            if (component.account_id, component.tariff) != (account_id, tariff):
                raise ValueError("components must belong to the supplied account and tariff.")
            if component.interval_start < earliest:
                raise ValueError("components must not precede earliest.")
            if component.interval_start in seen:
                raise ValueError("components must not contain duplicate interval starts.")
            seen.add(component.interval_start)
        with self._transaction():
            self._connection.execute(
                """
                DELETE FROM cost_components
                WHERE account_id = ? AND tariff = ? AND interval_start >= ?
                """,
                (account_id, tariff, _timestamp(earliest)),
            )
            self._connection.executemany(
                """
                INSERT INTO cost_components (
                    account_id, tariff, interval_start, usage_aud, supply_aud
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        component.account_id,
                        component.tariff,
                        _timestamp(component.interval_start),
                        str(component.usage_aud),
                        str(component.supply_aud),
                    )
                    for component in components
                ],
            )

    def mark_imported(self, statistic_id: str, through: datetime) -> None:
        statistic_id = _require_text(statistic_id, "statistic_id")
        through = _utc_datetime(through, "through")
        with self._transaction():
            row = self._connection.execute(
                "SELECT through_timestamp FROM imports WHERE statistic_id = ?", (statistic_id,)
            ).fetchone()
            if row is None or _from_timestamp(row["through_timestamp"]) < through:
                self._connection.execute(
                    """
                    INSERT INTO imports (statistic_id, through_timestamp) VALUES (?, ?)
                    ON CONFLICT(statistic_id) DO UPDATE SET through_timestamp = excluded.through_timestamp
                    """,
                    (statistic_id, _timestamp(through)),
                )

    def pending_backfill(self, start: date, end: date) -> list[date]:
        start = _require_day(start, "start")
        end = _require_day(end, "end")
        if end < start:
            raise ValueError("end must not precede start.")
        completed = {
            date.fromisoformat(row["day"])
            for row in self._connection.execute(
                "SELECT day FROM backfill_days WHERE day >= ? AND day < ?", (start.isoformat(), end.isoformat())
            )
        }
        pending: list[date] = []
        day = start
        while day < end:
            if day not in completed:
                pending.append(day)
            day = date.fromordinal(day.toordinal() + 1)
        return pending

    def complete_backfill(self, day: date) -> None:
        day = _require_day(day, "day")
        with self._transaction():
            self._connection.execute(
                """
                INSERT INTO backfill_days (day, completed_at) VALUES (?, ?)
                ON CONFLICT(day) DO NOTHING
                """,
                (day.isoformat(), datetime.now(timezone.utc).isoformat()),
            )

    def status(self) -> StatusSnapshot:
        rows = self._connection.execute(
            "SELECT statistic_id, through_timestamp FROM imports ORDER BY statistic_id"
        ).fetchall()
        completed = self._connection.execute("SELECT COUNT(*) AS count FROM backfill_days").fetchone()
        return StatusSnapshot(
            imports={row["statistic_id"]: _from_timestamp(row["through_timestamp"]) for row in rows},
            completed_backfill_days=completed["count"],
        )


def _earlier(current: datetime | None, candidate: datetime) -> datetime:
    return candidate if current is None or candidate < current else current


def _decimal_text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _rate_values_match(row: sqlite3.Row, rate: TariffRate) -> bool:
    stored_supply = Decimal(row["daily_supply_aud"]) if row["daily_supply_aud"] is not None else None
    return Decimal(row["per_kwh_aud"]) == rate.per_kwh_aud and stored_supply == rate.daily_supply_aud
