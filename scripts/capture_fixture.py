"""Manual fixture-capture probe for the Ergon portal.

This script is for development only.  It is NEVER invoked by the test
suite or by container startup.  It performs one real login, fetches usage
for a single specified day, prints only URL/status/content-type metadata,
and writes a redacted JSON shape.

Credentials come strictly from the ERGON_EMAIL and ERGON_PASSWORD
environment variables; they are never logged or written.

Redaction rules (see ``redact_payload``):
- Any key whose name matches ``account|customer|email|address|name|token|
  cookie|session`` (case-insensitive) is removed, EXCEPT the tariff series
  display ``name`` fields (``series[].name``), which are generic labels
  like "Tariff 11" and are kept so extraction stays testable.
- Numbers, timestamps, and other keys pass through untouched.

Usage:
    python -m scripts.capture_fixture 2026-08-31 out.json
    python -m scripts.capture_fixture 2026-08-31 out.json --replace
"""

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Mapping

from app.config import Settings
from app.ergon import ErgonClient

_REDACT_RE = re.compile(
    r"account|customer|email|address|name|token|cookie|session", re.IGNORECASE
)


def redact_payload(payload: object) -> object:
    """Recursively remove sensitive keys, keeping tariff series names.

    The one deliberate exception: inside a ``series`` list entry, the
    ``name`` key (the tariff display label, e.g. "Tariff 11") is preserved.
    Every other key matching the redaction pattern anywhere in the payload
    is dropped.
    """

    if isinstance(payload, dict):
        result: dict = {}
        for key, value in payload.items():
            key_text = str(key)
            if key_text == "series" and isinstance(value, list):
                result[key_text] = [_redact_series_entry(entry) for entry in value]
            elif _REDACT_RE.search(key_text):
                continue
            else:
                result[key_text] = redact_payload(value)
        return result
    if isinstance(payload, list):
        return [redact_payload(item) for item in payload]
    return payload


def _redact_series_entry(entry: object) -> object:
    """Redact one series entry while keeping its tariff display name."""

    if not isinstance(entry, dict):
        return redact_payload(entry)
    result: dict = {}
    for key, value in entry.items():
        key_text = str(key)
        if key_text == "name" and isinstance(value, str):
            result[key_text] = value  # tariff display name is safe to keep
        elif _REDACT_RE.search(key_text):
            continue
        else:
            result[key_text] = redact_payload(value)
    return result


def _settings_from_environment(environ: Mapping[str, str]) -> Settings:
    email = environ.get("ERGON_EMAIL", "").strip()
    password = environ.get("ERGON_PASSWORD", "").strip()
    if not email or not password:
        raise SystemExit(
            "ERGON_EMAIL and ERGON_PASSWORD environment variables are required."
        )
    # Settings is a frozen dataclass; only the fields the client uses are set
    # here.  The remaining fields are irrelevant to the probe.
    return Settings(
        ergon_email=email,
        ergon_password=password,
        supervisor_token="",
        poll_interval_hours=12,
        initial_history_days=365,
        backfill_batch_days=30,
        request_delay_seconds=0,
        retry_limit=0,
        tariff_name_overrides={},
        data_dir=Path(environ.get("ERGON_DATA_DIR", ".")),
    )


def _parse_day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise SystemExit(f"Day must be an ISO date (YYYY-MM-DD), got {text!r}.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture one day of Ergon usage as a redacted JSON fixture."
    )
    parser.add_argument("day", help="Brisbane local day, ISO format YYYY-MM-DD.")
    parser.add_argument("output", help="Output JSON file path.")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Allow overwriting an existing output file.",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help=(
            "Run with a visible browser window so the AWS WAF captcha can "
            "be solved manually. The solved aws-waf-token cookie (~3 day "
            "lifetime) is printed for reuse in headless runs."
        ),
    )
    args = parser.parse_args(argv)

    day = _parse_day(args.day)
    output_path = Path(args.output)
    if output_path.exists() and not args.replace:
        raise SystemExit(
            f"{output_path} already exists; pass --replace to overwrite it."
        )

    settings = _settings_from_environment(os.environ)
    client = ErgonClient(settings, headful=args.headful)

    async def run() -> dict:
        result = await client.fetch_day(day)
        return {
            "day": day.isoformat(),
            "source": result.source,
            "readings": [
                {
                    "tariff": reading.tariff,
                    "timestamp_brisbane": reading.interval_start.isoformat(),
                    "kwh": str(reading.kwh),
                }
                for reading in result.readings
            ],
        }

    capture = asyncio.run(run())

    output_path.write_text(
        json.dumps(redact_payload(capture), indent=2), encoding="utf-8"
    )
    print(f"Wrote redacted fixture to {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
