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
    extract_chart_payloads,
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


# -- Recharts chart payload extraction ---------------------------------------


@pytest.fixture
def chart_rows():
    return json.loads((FIXTURES / "chart_payloads.json").read_text(encoding="utf-8"))


def make_chart_row(day: str, **tariffs: float | None) -> dict:
    payload = {"date": "x", "day": day}
    payload.update(tariffs)
    return payload


def full_day_rows(**fixed_tariffs) -> list[dict]:
    """A full Brisbane day: UTC 14:00 (day-1) through 13:00 (same day)."""
    return [
        make_chart_row(
            f"2026-08-{30 + (14 + hour) // 24:02d} {(14 + hour) % 24:02d}:00:00+00:00",
            **fixed_tariffs,
        )
        for hour in range(24)
    ]


def test_chart_payloads_extract_both_tariffs_from_shared_rows(chart_rows):
    rows = extract_chart_payloads(chart_rows, ACCOUNT_ID, REQUESTED_DAY)

    assert {row.tariff for row in rows} == {"RTC11", "RTC33"}
    assert {row.tariff for row in rows}.isdisjoint({"date", "day"})


def test_chart_payloads_extract_24_hours_per_tariff():
    rows = extract_chart_payloads(
        full_day_rows(RTC11=1.0, RTC33=0.5), ACCOUNT_ID, REQUESTED_DAY
    )

    assert len(rows) == 48
    assert {row.tariff for row in rows} == {"RTC11", "RTC33"}
    for tariff in ("RTC11", "RTC33"):
        starts = [r.interval_start for r in rows if r.tariff == tariff]
        assert len({s.hour for s in starts}) == 24
        assert all(s.astimezone(__import__("zoneinfo").ZoneInfo("Australia/Brisbane")).date() == REQUESTED_DAY for s in starts)


def test_chart_payloads_skip_zero_and_absent_keys(chart_rows):
    rows = extract_chart_payloads(chart_rows, ACCOUNT_ID, REQUESTED_DAY)

    # Row 1: both nonzero. Row 2: RTC33 absent. Row 3: RTC33 == 0.
    rtc33 = [r for r in rows if r.tariff == "RTC33"]
    assert len(rtc33) == 1
    assert rtc33[0].kwh == Decimal("0.435")
    assert rtc33[0].interval_start == datetime(2026, 8, 30, 14, tzinfo=timezone.utc)


def test_chart_payloads_use_utc_day_field_and_handle_iso_variants():
    rows = extract_chart_payloads(
        [
            make_chart_row("2026-08-30 14:00:00+00:00", RTC11=1.0),
            make_chart_row("2026-08-30T15:00:00.000Z", RTC11=2.0),
            make_chart_row("2026-08-30T16:00:00Z", RTC11=3.0),
        ],
        ACCOUNT_ID,
        REQUESTED_DAY,
    )

    assert len(rows) == 3
    assert [r.interval_start for r in rows] == [
        datetime(2026, 8, 30, 14, tzinfo=timezone.utc),
        datetime(2026, 8, 30, 15, tzinfo=timezone.utc),
        datetime(2026, 8, 30, 16, tzinfo=timezone.utc),
    ]
    assert rows[0].kwh == Decimal("1.0")


def test_chart_payloads_reject_negative_and_non_finite_values():
    with pytest.raises(ExtractionError):
        extract_chart_payloads(
            [make_chart_row("2026-08-30 14:00:00+00:00", RTC11=-1.0)],
            ACCOUNT_ID,
            REQUESTED_DAY,
        )
    with pytest.raises(ExtractionError):
        extract_chart_payloads(
            [make_chart_row("2026-08-30 14:00:00+00:00", RTC11=float("nan"))],
            ACCOUNT_ID,
            REQUESTED_DAY,
        )


def test_chart_payloads_ignore_none_values():
    rows = extract_chart_payloads(
        [make_chart_row("2026-08-30 14:00:00+00:00", RTC11=None, RTC33=0.5)],
        ACCOUNT_ID,
        REQUESTED_DAY,
    )

    assert {row.tariff for row in rows} == {"RTC33"}


def test_chart_payloads_deduplicate_duplicate_shapes():
    row = make_chart_row("2026-08-30 14:00:00+00:00", RTC11=1.0, RTC33=0.5)
    rows = extract_chart_payloads([row, dict(row), dict(row)], ACCOUNT_ID, REQUESTED_DAY)

    assert len(rows) == 2
    assert {(r.tariff, r.kwh) for r in rows} == {("RTC11", Decimal("1.0")), ("RTC33", Decimal("0.5"))}


def test_chart_payloads_filter_by_requested_brisbane_day():
    rows = extract_chart_payloads(
        [
            make_chart_row("2026-08-30 14:00:00+00:00", RTC11=1.0),
            make_chart_row("2026-08-30 15:00:00+00:00", RTC11=2.0),
        ],
        ACCOUNT_ID,
        REQUESTED_DAY,  # UTC 14:00 == Brisbane 31 Aug 00:00 -> in day
    )

    assert len(rows) == 2
    assert [r.kwh for r in rows] == [Decimal("1.0"), Decimal("2.0")]


def test_chart_payloads_empty_after_filter_raises():
    with pytest.raises(ExtractionError, match="requested Brisbane day"):
        extract_chart_payloads(
            [make_chart_row("2026-08-29 14:00:00+00:00", RTC11=1.0)],
            ACCOUNT_ID,
            REQUESTED_DAY,
        )


def test_chart_payloads_reject_malformed_rows():
    with pytest.raises(ExtractionError):
        extract_chart_payloads([], ACCOUNT_ID, REQUESTED_DAY)
    with pytest.raises(ExtractionError):
        extract_chart_payloads(["not-a-dict"], ACCOUNT_ID, REQUESTED_DAY)
    with pytest.raises(ExtractionError):
        extract_chart_payloads([{"date": "x"}], ACCOUNT_ID, REQUESTED_DAY)  # no day
    with pytest.raises(ExtractionError):
        extract_chart_payloads(
            [make_chart_row("garbage", RTC11=1.0)], ACCOUNT_ID, REQUESTED_DAY
        )
    with pytest.raises(ExtractionError):
        extract_chart_payloads(
            [{"day": "2026-08-31 14:00:00+00:00", "RTC11": "abc"}],
            ACCOUNT_ID,
            REQUESTED_DAY,
        )


def test_chart_payloads_tariff_keys_are_dynamic_not_hardcoded():
    rows = extract_chart_payloads(
        [make_chart_row("2026-08-30 14:00:00+00:00", RTC77=0.25)],
        ACCOUNT_ID,
        REQUESTED_DAY,
    )

    assert len(rows) == 1
    assert rows[0].tariff == "RTC77"
    assert rows[0].kwh == Decimal("0.25")


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
