"""Tests for tariff rate extraction from Ergon tariff pages."""

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.errors import ExtractionError
from app.tariff_rates import extract_tariff_rates

BRISBANE = ZoneInfo("Australia/Brisbane")
FIXTURE = Path(__file__).parent / "fixtures" / "tariff_page.html"
OBSERVED_AT = datetime(2026, 9, 3, 9, 15, tzinfo=BRISBANE)


@pytest.fixture
def tariff_html() -> str:
    # The sanitized fixture deliberately contains a "$15.00 per day" label
    # outside any tariff card (for the rejection test); the happy-path tests
    # remove that fine-print paragraph first.
    html = FIXTURE.read_text(encoding="utf-8")
    return html.replace(
        '        <p class="fine-print">Estimated solar feed-in credit: $15.00 per day.</p>\n',
        "",
    )


def by_tariff(rates):
    return {rate.tariff: rate for rate in rates}


def test_extracts_decimal_rates_by_card(tariff_html):
    rates = extract_tariff_rates(tariff_html, "A-TEST123", OBSERVED_AT)
    tariffs = by_tariff(rates)
    assert tariffs["Tariff 11"].per_kwh_aud == Decimal("0.28895")
    assert tariffs["Tariff 11"].daily_supply_aud == Decimal("1.80508")
    assert tariffs["Tariff 33"].per_kwh_aud == Decimal("0.16764")
    assert tariffs["Tariff 33"].daily_supply_aud is None


def test_rates_carry_account_and_observed_at(tariff_html):
    rates = extract_tariff_rates(tariff_html, "A-TEST123", OBSERVED_AT)
    assert len(rates) == 2
    for rate in rates:
        assert rate.account_id == "A-TEST123"
        assert rate.observed_at == OBSERVED_AT


@pytest.mark.parametrize(
    "mutate",
    [
        # Duplicate tariff cards with the same heading.
        lambda html: html.replace(
            "<h3>Tariff 33</h3>", "<h3>Tariff 11</h3>", 1
        ),
        # Negative per-kWh value.
        lambda html: html.replace("$0.28895", "$-0.28895"),
        # Negative supply value.
        lambda html: html.replace("$1.80508", "$-1.80508"),
        # Tariff card without a per-kWh value.
        lambda html: html.replace("$0.16764 per kWh", "no usage price listed"),
    ],
)
def test_rejects_invalid_tariff_pages(tariff_html, mutate):
    with pytest.raises(ExtractionError):
        extract_tariff_rates(mutate(tariff_html), "A-TEST123", OBSERVED_AT)


def test_monetary_label_outside_any_card_is_rejected(tariff_html):
    # The fixture's fine-print line carries "$15.00 per day" outside any card.
    full_fixture = FIXTURE.read_text(encoding="utf-8")
    assert "$15.00 per day" in full_fixture
    with pytest.raises(ExtractionError):
        extract_tariff_rates(full_fixture, "A-TEST123", OBSERVED_AT)


def test_monetary_label_outside_any_card_is_rejected_inline():
    html = """
    <main>
      <p>A fixed charge of $15.00 per day applies.</p>
      <div class="tariff-card"><h3>Tariff 11</h3><p>$0.28895 per kWh</p></div>
    </main>
    """
    with pytest.raises(ExtractionError):
        extract_tariff_rates(html, "A-TEST123", OBSERVED_AT)


def test_duplicate_tariff_cards_are_rejected():
    html = """
    <main>
      <div class="tariff-card"><h3>Tariff 11</h3><p>$0.28895 per kWh</p></div>
      <div class="tariff-card"><h3>Tariff 11</h3><p>$0.30000 per kWh</p></div>
    </main>
    """
    with pytest.raises(ExtractionError):
        extract_tariff_rates(html, "A-TEST123", OBSERVED_AT)


def test_negative_value_is_rejected():
    html = (
        '<div class="tariff-card"><h3>Tariff 11</h3>'
        "<p>$-0.28895 per kWh</p></div>"
    )
    with pytest.raises(ExtractionError):
        extract_tariff_rates(html, "A-TEST123", OBSERVED_AT)


def test_card_without_per_kwh_value_is_rejected():
    html = (
        '<div class="tariff-card"><h3>Tariff 11</h3>'
        "<p>$1.80508 per day</p></div>"
    )
    with pytest.raises(ExtractionError):
        extract_tariff_rates(html, "A-TEST123", OBSERVED_AT)


def test_unlabelled_money_is_ignored(tariff_html):
    # "$0.28895" without a "per kWh" label must not be picked up anywhere.
    html = tariff_html.replace("$0.28895 per kWh", "$0.28895 (usage rate)")
    with pytest.raises(ExtractionError):
        extract_tariff_rates(html, "A-TEST123", OBSERVED_AT)


# -- Live-portal accordion layout ------------------------------------------

# Reproduced from the live portal (2026-09-03, scripts/diagnose_rates_page.py):
# tariffs are accordion sections with H1 headings — no card containers.
ACCORDION_HTML = """
<html>
  <body>
    <h2>Your Tariff Info</h2>
    <h1>Tariff 11</h1>
    <div><p>All usage per kWh $ 0.28895 Supply charge per day $ 1.80508</p></div>
    <h1>Tariff 33</h1>
    <div><p>All usage per kWh <b>$ 0.16764</b></p></div>
  </body>
</html>
"""


def test_extracts_rates_from_accordion_sections():
    rates = extract_tariff_rates(ACCORDION_HTML, "A-TEST123", OBSERVED_AT)
    tariffs = by_tariff(rates)
    assert tariffs["Tariff 11"].per_kwh_aud == Decimal("0.28895")
    assert tariffs["Tariff 11"].daily_supply_aud == Decimal("1.80508")
    assert tariffs["Tariff 33"].per_kwh_aud == Decimal("0.16764")
    assert tariffs["Tariff 33"].daily_supply_aud is None


def test_label_before_value_order_is_accepted():
    # "per kWh $X" / "per day $X" — label first, value after, within a window.
    html = """
    <h1>Tariff 11</h1>
    <p>per kWh $0.28895 and per day $1.80508</p>
    """
    rates = extract_tariff_rates(html, "A-TEST123", OBSERVED_AT)
    tariffs = by_tariff(rates)
    assert tariffs["Tariff 11"].per_kwh_aud == Decimal("0.28895")
    assert tariffs["Tariff 11"].daily_supply_aud == Decimal("1.80508")


def test_section_scope_ends_at_next_heading():
    # A heading of ANY level closes the previous tariff's scope.  The per-day
    # value under the non-tariff "Supply charges" heading therefore falls
    # outside any tariff section and must be rejected as stray.
    html = """
    <h1>Tariff 11</h1>
    <p>$0.28895 per kWh</p>
    <h2>Supply charges</h2>
    <p>$1.80508 per day</p>
    """
    with pytest.raises(ExtractionError):
        extract_tariff_rates(html, "A-TEST123", OBSERVED_AT)
