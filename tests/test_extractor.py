import copy
import json
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.errors import ExtractionError
from app.extractor import (
    CapturedJson,
    extract_dom,
    extract_structured,
    select_usage_payload,
)


FIXTURES = Path(__file__).parent / "fixtures"
REQUESTED_DAY = date(2026, 8, 31)
ACCOUNT_ID = "A-TEST123"


@pytest.fixture
def fixture_json():
    return json.loads((FIXTURES / "structured_usage.json").read_text(encoding="utf-8"))


@pytest.fixture
def fixture_html():
    return (FIXTURES / "usage_chart.html").read_text(encoding="utf-8")


def captured(payload, *, url="https://example.invalid/usage", status=200, content_type="application/json"):
    return CapturedJson(url=url, status=status, content_type=content_type, payload=payload)


def test_extracts_both_tariffs_without_hard_coding(fixture_json):
    rows = extract_structured(fixture_json, ACCOUNT_ID, REQUESTED_DAY)

    assert {row.tariff for row in rows} == {"Tariff 11", "Tariff 33"}
    assert len(rows) == 48

    renamed = copy.deepcopy(fixture_json)
    renamed["series"][1]["name"] = "Controlled load"
    assert {row.tariff for row in extract_structured(renamed, ACCOUNT_ID, REQUESTED_DAY)} == {
        "Tariff 11",
        "Controlled load",
    }


def test_structured_extraction_normalizes_values_and_preserves_zero(fixture_json):
    rows = extract_structured(fixture_json, ACCOUNT_ID, REQUESTED_DAY)

    assert rows[0].account_id == ACCOUNT_ID
    assert rows[0].interval_start == datetime(2026, 8, 30, 14, tzinfo=timezone.utc)
    assert rows[0].kwh == Decimal("1.25")
    assert next(row for row in rows if row.tariff == "Tariff 33").kwh == Decimal("0")


def test_structured_extraction_filters_by_brisbane_local_day(fixture_json):
    mixed_days = copy.deepcopy(fixture_json)
    mixed_days["series"][0]["data"].append(
        {"timestamp": "01 Sep 2026 12:00AM", "value": 9.99}
    )

    rows = extract_structured(mixed_days, ACCOUNT_ID, REQUESTED_DAY)

    assert len(rows) == 48
    assert {row.interval_start.astimezone(__import__("zoneinfo").ZoneInfo("Australia/Brisbane")).date() for row in rows} == {
        REQUESTED_DAY
    }


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"series": []},
        {"series": [{"name": "Tariff 11", "data": []}]},
        {"series": [{"name": "", "data": [{"timestamp": "31 Aug 2026 12:00AM", "value": 1}]}]},
        {"series": [{"name": "Tariff 11", "data": [{"timestamp": "invalid", "value": 1}]}]},
        {"series": [{"name": "Tariff 11", "data": [{"timestamp": "31 Aug 2026 12:00AM", "value": True}]}]},
        {"series": [{"name": "Tariff 11", "data": [{"timestamp": "31 Aug 2026 12:00AM", "value": "NaN"}]}]},
        {"series": [{"name": "Tariff 11", "data": [{"timestamp": "31 Aug 2026 12:00AM", "value": -0.1}]}]},
    ],
)
def test_structured_extraction_rejects_malformed_or_empty_payloads(payload):
    with pytest.raises(ExtractionError):
        extract_structured(payload, ACCOUNT_ID, REQUESTED_DAY)


def test_requested_day_rejects_an_empty_filtered_result(fixture_json):
    with pytest.raises(ExtractionError, match="requested Brisbane day"):
        extract_structured(fixture_json, ACCOUNT_ID, date(2026, 9, 1))


def test_structured_extraction_rejects_duplicate_tariff_timestamp_keys(fixture_json):
    duplicate = copy.deepcopy(fixture_json)
    duplicate["series"][0]["data"].append(copy.deepcopy(duplicate["series"][0]["data"][0]))

    with pytest.raises(ExtractionError, match="duplicate"):
        extract_structured(duplicate, ACCOUNT_ID, REQUESTED_DAY)


def test_captured_json_keeps_all_response_metadata(fixture_json):
    candidate = CapturedJson(
        url="https://example.invalid/portal/usage.json",
        status=200,
        content_type="application/json; charset=utf-8",
        payload=fixture_json,
    )

    assert candidate.url.endswith("usage.json")
    assert candidate.status == 200
    assert candidate.content_type == "application/json; charset=utf-8"
    assert candidate.payload is fixture_json
    with pytest.raises(FrozenInstanceError):
        candidate.status = 500


def test_selector_accepts_exactly_one_valid_json_response(fixture_json):
    candidates = [
        captured({"error": "synthetic malformed candidate"}, url="https://example.invalid/error"),
        captured(fixture_json, content_type="application/json; charset=utf-8"),
        captured(fixture_json, status=500),
        captured(fixture_json, content_type="text/html"),
    ]

    rows = select_usage_payload(candidates, ACCOUNT_ID, REQUESTED_DAY)

    assert len(rows) == 48


def test_selector_rejects_zero_valid_payloads():
    candidates = [captured({"error": "synthetic malformed candidate"})]

    with pytest.raises(ExtractionError, match="No valid"):
        select_usage_payload(candidates, ACCOUNT_ID, None)


def test_selector_rejects_ambiguous_valid_payloads(fixture_json):
    two_valid_candidates = [
        captured(fixture_json, url="https://example.invalid/usage/one"),
        captured(copy.deepcopy(fixture_json), url="https://example.invalid/usage/two"),
    ]

    with pytest.raises(ExtractionError, match="ambiguous"):
        select_usage_payload(two_valid_candidates, ACCOUNT_ID, None)


def test_dom_extraction_uses_explicit_accessible_data_values(fixture_html):
    rows = extract_dom(fixture_html, ACCOUNT_ID, REQUESTED_DAY)

    assert len(rows) == 48
    assert {row.tariff for row in rows} == {"Tariff 11", "Tariff 33"}
    assert rows[0].kwh == Decimal("1.25")
    assert next(row for row in rows if row.tariff == "Tariff 33").kwh == Decimal("0")


def test_dom_extraction_never_infers_readings_from_pixel_height():
    html = '<div aria-label="Tariff 11" class="bar" style="height: 125px"></div>'

    with pytest.raises(ExtractionError):
        extract_dom(html, ACCOUNT_ID, REQUESTED_DAY)


def test_dom_extraction_rejects_duplicate_and_empty_filtered_results():
    duplicate_html = """
        <div data-tariff="Tariff 11" data-timestamp="31 Aug 2026 12:00AM" data-kwh="1.25"></div>
        <div data-tariff="Tariff 11" data-timestamp="31 Aug 2026 12:00AM" data-kwh="1.25"></div>
    """
    with pytest.raises(ExtractionError, match="duplicate"):
        extract_dom(duplicate_html, ACCOUNT_ID, REQUESTED_DAY)

    next_day_html = """
        <div data-tariff="Tariff 11" data-timestamp="01 Sep 2026 12:00AM" data-kwh="1.25"></div>
    """
    with pytest.raises(ExtractionError, match="requested Brisbane day"):
        extract_dom(next_day_html, ACCOUNT_ID, REQUESTED_DAY)
