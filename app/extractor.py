"""Extraction of tariff usage readings from Ergon responses.

Two independent extraction paths are provided:

- ``extract_structured``: parse a captured JSON payload shaped as
  ``{"series": [{"name": ..., "data": [{"timestamp": ..., "value": ...}]}]}``.
- ``extract_dom``: parse explicit, accessible data attributes
  (``data-tariff`` / ``data-timestamp`` / ``data-kwh``) from the usage chart
  HTML.  Values are never inferred from presentation (bar heights, styles).

Both paths validate strictly: non-empty tariffs, parseable Brisbane
timestamps, finite non-negative numeric kWh, requested-day filtering,
per-tariff uniqueness, and at least one reading.  ``select_usage_payload``
tries every captured JSON response and accepts the payload only when exactly
one candidate extracts successfully.
"""

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Mapping, Sequence

from .errors import ExtractionError
from .models import UsageReading
from .normalize import BRISBANE, parse_brisbane_timestamp


@dataclass(frozen=True)
class CapturedJson:
    """One captured JSON response with its request metadata."""

    url: str
    status: int
    content_type: str
    payload: object


_RETURN_SKIPPED = object()


def _validate_reading(
    account_id: str,
    tariff: object,
    timestamp: object,
    kwh: object,
    requested_day: date | None,
) -> UsageReading | object:
    if not isinstance(tariff, str) or not tariff.strip():
        raise ExtractionError("Tariff name must be a non-empty string.")
    if not isinstance(timestamp, str) or not timestamp.strip():
        raise ExtractionError("Timestamp must be a non-empty string.")
    try:
        interval_start = parse_brisbane_timestamp(timestamp)
    except ValueError as error:
        raise ExtractionError(f"Unable to parse usage timestamp: {error}") from error

    if requested_day is not None and interval_start.astimezone(
        BRISBANE
    ).date() != requested_day:
        # Readings for other days are skipped, not errors; an empty result
        # after filtering is reported by the caller.
        return _RETURN_SKIPPED

    # Booleans are ints in Python but must never be accepted as kWh values.
    if isinstance(kwh, bool) or not isinstance(kwh, (int, float)):
        raise ExtractionError("Usage value must be a number.")
    if not math.isfinite(kwh) or kwh < 0:
        raise ExtractionError("Usage value must be finite and not negative.")
    return UsageReading(
        account_id=account_id,
        tariff=tariff.strip(),
        interval_start=interval_start,
        kwh=Decimal(str(kwh)),
    )


def _reject_duplicates(readings: list[UsageReading]) -> None:
    seen: set[tuple[str, object]] = set()
    for reading in readings:
        key = (reading.tariff, reading.interval_start)
        if key in seen:
            raise ExtractionError(
                f"duplicate reading for tariff {reading.tariff!r} at "
                f"{reading.interval_start.isoformat()}."
            )
        seen.add(key)


def _require_readings(readings: list[UsageReading]) -> list[UsageReading]:
    if not readings:
        raise ExtractionError(
            "No usage readings found for the requested Brisbane day."
        )
    return readings


def extract_structured(
    payload: object, account_id: str, requested_day: date | None
) -> list[UsageReading]:
    """Extract readings from a structured Ergon JSON payload."""

    if not isinstance(payload, dict):
        raise ExtractionError("Structured payload must be a JSON object.")
    series = payload.get("series")
    if not isinstance(series, list):
        raise ExtractionError("Structured payload must contain a 'series' list.")

    readings: list[UsageReading] = []
    for entry in series:
        if not isinstance(entry, dict):
            raise ExtractionError("Each series entry must be a JSON object.")
        name = entry.get("name")
        data = entry.get("data")
        if not isinstance(data, list):
            raise ExtractionError("Each series entry must contain a 'data' list.")
        for item in data:
            if not isinstance(item, dict):
                raise ExtractionError("Each data point must be a JSON object.")
            reading = _validate_reading(
                account_id, name, item.get("timestamp"), item.get("value"), requested_day
            )
            if reading is not _RETURN_SKIPPED:
                readings.append(reading)
    _reject_duplicates(readings)
    return _require_readings(readings)


class _AccessibleDataParser(HTMLParser):
    """Collect explicit data attributes and aria-label readings from HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.samples: list[tuple[str | None, str | None, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value for key, value in attrs}
        if not any(key in attributes for key in ("data-tariff", "data-timestamp", "data-kwh")):
            return
        self.samples.append(
            (
                attributes.get("data-tariff"),
                attributes.get("data-timestamp"),
                attributes.get("data-kwh"),
            )
        )


def extract_dom(
    html: str, account_id: str, requested_day: date | None
) -> list[UsageReading]:
    """Extract readings from explicit data attributes in usage chart HTML.

    Only ``data-tariff`` / ``data-timestamp`` / ``data-kwh`` attributes are
    read.  Presentation values such as bar pixel heights or inline styles are
    ignored, so no reading can be inferred from how a bar is drawn.
    """

    if not isinstance(html, str) or not html.strip():
        raise ExtractionError("DOM payload must be a non-empty HTML string.")

    parser = _AccessibleDataParser()
    parser.feed(html)

    readings: list[UsageReading] = []
    for tariff, timestamp, kwh in parser.samples:
        kwh_value: object = kwh
        if kwh is not None:
            try:
                kwh_value = float(kwh)
            except ValueError as error:
                raise ExtractionError("Usage value must be a number.") from error
        reading = _validate_reading(
            account_id, tariff, timestamp, kwh_value, requested_day
        )
        if reading is not _RETURN_SKIPPED:
            readings.append(reading)
    _reject_duplicates(readings)
    return _require_readings(readings)


def select_usage_payload(
    candidates: Sequence[CapturedJson], account_id: str, requested_day: date | None
) -> list[UsageReading]:
    """Pick the single captured JSON response that extracts successfully.

    Every candidate's payload is attempted.  Zero successful extractions and
    two-or-more successful extractions are both failures; candidates are never
    merged.
    """

    valid: list[list[UsageReading]] = []
    for candidate in candidates:
        if candidate.status != 200 or "json" not in candidate.content_type.lower():
            continue
        try:
            valid.append(extract_structured(candidate.payload, account_id, requested_day))
        except ExtractionError:
            continue
    if not valid:
        raise ExtractionError("No valid usage payload found in captured responses.")
    if len(valid) > 1:
        raise ExtractionError(
            "ambiguous usage payload: multiple captured responses extracted "
            "successfully."
        )
    return valid[0]


# Tariff series keys inside a Recharts bar ``payload`` are dynamic RTC codes
# (e.g. ``RTC11`` / ``RTC33``).  They are never hard-coded; any key matching
# this pattern (and not ``date`` / ``day``) is treated as a tariff series.
_TARIFF_KEY_RE = re.compile(r"^RTC[A-Za-z0-9]+$", re.IGNORECASE)

# The canonical ``day`` value is a UTC hour-start with a space separator
# (``2026-08-31 14:00:00+00:00``); the ISO variants seen in the wild
# (``2026-08-31T14:00:00.000Z``) are also accepted.
_CHART_DAY_FORMATS = (
    "%Y-%m-%d %H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
)


def _parse_chart_day(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ExtractionError("Chart payload 'day' must be a non-empty string.")
    source = value.strip()
    for fmt in _CHART_DAY_FORMATS:
        try:
            parsed = datetime.strptime(source, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    raise ExtractionError(
        f"Chart payload 'day' value {value!r} is not a supported UTC format."
    )


def extract_chart_payloads(
    rows: Sequence[Mapping], account_id: str, requested_day: date | None
) -> list[UsageReading]:
    """Extract readings from Recharts bar-shape ``payload`` objects.

    The live portal renders usage as a Recharts chart; each bar shape carries
    a React ``payload`` dict holding one row per hour, e.g.::

        {"date": "01 Sep 12:00AM", "day": "2026-08-31 14:00:00+00:00",
         "RTC11": 1.094, "RTC33": 0.435}

    Contract facts (verified live):
    - ``day`` is the canonical UTC hour-start; 24 rows per day.
    - Both tariff series appear on the SAME row; only nonzero tariffs have
      keys, and keys are dynamic ``RTC*`` codes (never hard-coded here).
    - The same row is observed once per rendered series shape, so rows are
      deduplicated by (tariff, interval_start).

    Tariff names use the raw RTC code (e.g. ``"RTC11"``) rather than the
    chart's display name (``"Tariff 11"``): the ``series`` display name lives
    on the per-shape element, not the shared row, so it cannot be joined
    reliably to a row-level tariff key.
    """

    if not isinstance(rows, (list, tuple)) or not rows:
        raise ExtractionError("Chart payload must be a non-empty list of rows.")

    readings: list[UsageReading] = []
    seen: set[tuple[str, object]] = set()
    malformed = 0
    for row in rows:
        # The fiber walk can collect adjacent Recharts prop objects that are
        # not chart data rows; skip anything without a usable 'day' rather
        # than failing the whole extraction.
        if not isinstance(row, Mapping) or not row.get("day"):
            malformed += 1
            continue
        try:
            interval_start = _parse_chart_day(row.get("day"))
        except ExtractionError:
            malformed += 1
            continue
        local_date = interval_start.astimezone(BRISBANE).date()
        if requested_day is not None and local_date != requested_day:
            continue
        for key, raw_value in row.items():
            if key in ("date", "day") or not isinstance(key, str):
                continue
            if not _TARIFF_KEY_RE.match(key):
                continue
            if raw_value is None:
                continue
            # Zero-valued tariff keys are omitted-by-zero rather than real
            # readings: only nonzero tariffs carry keys on the live portal,
            # so explicit zeros are treated as absent.
            if isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool) and raw_value == 0:
                continue
            try:
                value = float(raw_value)  # type: ignore[arg-type]
            except (TypeError, ValueError) as error:
                raise ExtractionError("Usage value must be a number.") from error
            if not math.isfinite(value) or value < 0:
                raise ExtractionError("Usage value must be finite and not negative.")
            dedup_key = (key, interval_start)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            readings.append(
                UsageReading(
                    account_id=account_id,
                    tariff=key,
                    interval_start=interval_start,
                    kwh=Decimal(str(raw_value)),
                )
            )
    if requested_day is not None and not readings:
        # Every row was filtered out by the day boundary (or unparseable):
        # surface the row range so the mismatch is diagnosable from the
        # error alone.
        parsed_days = []
        raw_days: list[str] = []
        for row in rows:
            if not isinstance(row, Mapping) or not row.get("day"):
                continue
            raw = row["day"]
            raw_days.append(str(raw)[:40])
            try:
                parsed_days.append(_parse_chart_day(raw))
            except ExtractionError:
                continue
        if parsed_days:
            span = (
                f"{min(parsed_days).isoformat()} .. {max(parsed_days).isoformat()}"
                f" UTC ({len(parsed_days)} rows)"
            )
        else:
            sample = sorted(set(raw_days))[:3]
            span = f"{len(rows)} rows with unparseable 'day' values, e.g. {sample}"
        raise ExtractionError(
            "No usage readings found for the requested Brisbane day. "
            f"Chart rows span: {span}; requested Brisbane day: {requested_day}."
        )
    return _require_readings(readings)
