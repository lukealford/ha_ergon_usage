"""Immutable, validated records shared by the Ergon Usage application."""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal


def _utc_datetime(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware.")
    return value.astimezone(timezone.utc)


def _decimal(value: Decimal, field_name: str, *, allow_none: bool = False) -> Decimal | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name} must be a Decimal.")
    if not value.is_finite():
        raise ValueError(f"{field_name} must be finite.")
    if value < 0:
        raise ValueError(f"{field_name} must not be negative.")
    return value


def _text(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string.")
    value = value.strip()
    if not value:
        raise ValueError(f"{field_name} must not be empty.")
    return value


@dataclass(frozen=True, slots=True)
class UsageReading:
    account_id: str
    tariff: str
    interval_start: datetime
    kwh: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_id", _text(self.account_id, "account_id"))
        object.__setattr__(self, "tariff", _text(self.tariff, "tariff"))
        object.__setattr__(self, "interval_start", _utc_datetime(self.interval_start, "interval_start"))
        object.__setattr__(self, "kwh", _decimal(self.kwh, "kwh"))


@dataclass(frozen=True, slots=True)
class TariffRate:
    account_id: str
    tariff: str
    observed_at: datetime
    per_kwh_aud: Decimal
    daily_supply_aud: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_id", _text(self.account_id, "account_id"))
        object.__setattr__(self, "tariff", _text(self.tariff, "tariff"))
        object.__setattr__(self, "observed_at", _utc_datetime(self.observed_at, "observed_at"))
        object.__setattr__(self, "per_kwh_aud", _decimal(self.per_kwh_aud, "per_kwh_aud"))
        object.__setattr__(
            self,
            "daily_supply_aud",
            _decimal(self.daily_supply_aud, "daily_supply_aud", allow_none=True),
        )


@dataclass(frozen=True, slots=True)
class StatisticPoint:
    start: datetime
    sum: Decimal
    state: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", _utc_datetime(self.start, "start"))
        object.__setattr__(self, "sum", _decimal(self.sum, "sum"))
        object.__setattr__(self, "state", _decimal(self.state, "state", allow_none=True))
