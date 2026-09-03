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
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from html.parser import HTMLParser
from typing import Sequence

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
