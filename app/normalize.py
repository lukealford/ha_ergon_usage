"""Strict normalization helpers for Ergon source values."""

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from .errors import AccountDiscoveryError


BRISBANE = ZoneInfo("Australia/Brisbane")
UTC = timezone.utc

# These are the two timestamp labels used by the sanitized Ergon fixtures.  Do
# not silently accept arbitrary browser/ISO date formats: an unrecognized
# source value should be visible as an extraction failure.
_TIMESTAMP_FORMATS = (
    "%d %b %Y %I:%M%p",
    "%d/%m/%Y %I:%M %p",
)

ACCOUNT_RE = re.compile(r"/portal/(A-[A-Za-z0-9]+)/")


def parse_brisbane_timestamp(value: str, requested_day: date | None = None) -> datetime:
    """Parse an Ergon Brisbane-local timestamp and return an aware UTC value.

    ``requested_day`` is optional for callers parsing a response for one
    custom day; when supplied, the source timestamp must fall on that local
    Brisbane date.
    """

    if not isinstance(value, str) or not value.strip():
        raise ValueError("Timestamp must be a non-empty string.")
    source = value.strip()
    for timestamp_format in _TIMESTAMP_FORMATS:
        try:
            local = datetime.strptime(source, timestamp_format).replace(tzinfo=BRISBANE)
        except ValueError:
            continue
        parsed = local.astimezone(UTC)
        if requested_day is not None and parsed.astimezone(BRISBANE).date() != requested_day:
            raise ValueError("Timestamp is outside the requested Brisbane day.")
        return parsed
    raise ValueError("Timestamp is not a supported Ergon Brisbane format.")


def parse_brisbane_timestamp_for_day(value: str, requested_day: date) -> datetime:
    """Parse a timestamp and require it belongs to ``requested_day`` locally."""

    if not isinstance(requested_day, date):
        raise TypeError("requested_day must be a date.")
    return parse_brisbane_timestamp(value, requested_day=requested_day)


def effective_usage_boundary(observed_at: datetime) -> datetime:
    """Return the first Brisbane whole hour at or after a rate observation."""

    observed_at = _aware_utc(observed_at, "observed_at")
    local = observed_at.astimezone(BRISBANE)
    boundary = local.replace(minute=0, second=0, microsecond=0)
    if local != boundary:
        boundary += timedelta(hours=1)
    return boundary.astimezone(UTC)


def effective_supply_boundary(observed_at: datetime) -> datetime:
    """Return the first Brisbane midnight strictly after a rate observation."""

    observed_at = _aware_utc(observed_at, "observed_at")
    next_day = observed_at.astimezone(BRISBANE).date() + timedelta(days=1)
    return datetime.combine(next_day, time.min, tzinfo=BRISBANE).astimezone(UTC)


def statistic_id(account_id: str, tariff: str) -> str:
    """Build the stable external statistic identifier for one account/tariff."""

    if not isinstance(account_id, str) or not account_id.strip():
        raise ValueError("account_id must not be empty.")
    if not isinstance(tariff, str) or not tariff.strip():
        raise ValueError("tariff must not be empty.")
    slug = re.sub(r"[^a-z0-9]+", "_", f"{account_id}_{tariff}".lower()).strip("_")
    if not slug:
        raise ValueError("account_id and tariff do not produce a statistic ID.")
    return f"ergon:{slug}"


def discover_single_account(urls: Iterable[str]) -> str:
    """Discover exactly one Ergon account ID from portal URLs."""

    accounts: set[str] = set()
    try:
        iterator = iter(urls)
    except TypeError as error:
        raise AccountDiscoveryError() from error
    for url in iterator:
        if not isinstance(url, str):
            continue
        match = ACCOUNT_RE.search(url)
        if match:
            accounts.add(match.group(1))
    if len(accounts) != 1:
        raise AccountDiscoveryError()
    return next(iter(accounts))


def validate_statistic_ids(account_tariffs: Iterable[tuple[str, str]]) -> dict[str, tuple[str, str]]:
    """Reject lossy slugification where two source pairs map to one ID."""

    seen: dict[str, tuple[str, str]] = {}
    for account_id, tariff in account_tariffs:
        key = statistic_id(account_id, tariff)
        pair = (account_id.strip(), tariff.strip())
        previous = seen.get(key)
        if previous is not None and previous != pair:
            raise ValueError("Statistic ID collision detected.")
        seen[key] = pair
    return seen


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware.")
    return value.astimezone(UTC)
