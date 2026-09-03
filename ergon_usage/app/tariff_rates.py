"""Extraction of tariff rates from the Ergon tariff page.

Tariff rates are presented as semantic cards: a heading naming the tariff
followed by monetary values labelled ``per kWh`` (usage) and ``per day``
(supply).  Values are taken directly from the captured text as ``Decimal``
values — never inferred from presentation.  A monetary value paired with a
``per day``/``per kWh`` label that appears outside any tariff card is a
parsing failure, not a silent skip.

The effective rate boundaries are not reimplemented here; callers use
``normalize.effective_usage_boundary`` and ``normalize.effective_supply_boundary``.
"""

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

from .errors import ExtractionError
from .models import TariffRate

# Labels must be explicit: a bare "$0.28895" is never accepted.
_PER_KWH_RE = re.compile(r"\$\s*(?P<value>-?\d+(?:\.\d+)?)\s*(?:\n|\s)*per\s*kwh", re.IGNORECASE)
_PER_DAY_RE = re.compile(r"\$\s*(?P<value>-?\d+(?:\.\d+)?)\s*(?:\n|\s)*per\s*day", re.IGNORECASE)

# Heading levels and container classes treated as tariff card headings/cards.
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_CARD_CLASSES = frozenset({"tariff-card", "card", "tile"})


def _classes_of(attrs: dict[str, str | None]) -> set[str]:
    raw = attrs.get("class") or ""
    return set(raw.split())


def _parse_decimal(text: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation as error:
        raise ExtractionError("Tariff rate value is not a valid decimal.") from error


class _TariffCard:
    """One tariff card collected by the parser."""

    def __init__(self, name: str, in_card_container: bool) -> None:
        self.name = name
        self.in_card_container = in_card_container
        self.per_kwh: Decimal | None = None
        self.daily_supply: Decimal | None = None


class _TariffPageParser(HTMLParser):
    """Collect tariff cards from semantic headings within card containers."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[_TariffCard] = []
        self._stray_label = False
        self._stack: list[dict] = []  # open elements carrying scope info
        self._current_card: _TariffCard | None = None
        self._current_heading: str | None = None
        self._inside_heading = False

    # -- element handling ------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: (value or "") for key, value in attrs}
        classes = _classes_of(attributes)
        role = attributes.get("role") or ""
        aria_label = attributes.get("aria-label") or ""
        is_card = bool(classes & _CARD_CLASSES) or role == "region" and bool(aria_label)
        self._stack.append(
            {"tag": tag, "card_owner": self._current_card, "is_card": is_card}
        )
        if tag in _HEADING_TAGS:
            self._inside_heading = True
            self._current_heading = ""

    def handle_endtag(self, tag: str) -> None:
        if tag in _HEADING_TAGS:
            self._inside_heading = False
            self._finish_heading()
            self._pop_stack_until(tag)
            return
        self._pop_stack_until(tag)

    def _pop_stack_until(self, tag: str) -> None:
        while self._stack:
            frame = self._stack.pop()
            if frame["is_card"]:
                self._finish_card()
            if frame["tag"] == tag:
                break

    def handle_data(self, data: str) -> None:
        if self._inside_heading and self._current_heading is not None:
            self._current_heading += data
            return
        if self._current_card is not None:
            self._record(self._current_card, data)
        else:
            self._check_stray(data)

    # -- card bookkeeping ------------------------------------------------

    def _finish_heading(self) -> None:
        name = " ".join((self._current_heading or "").split())
        self._current_heading = None
        if name:
            self._finish_card()
            self._current_card = _TariffCard(name, self._inside_card_container())

    def _inside_card_container(self) -> bool:
        return any(frame["is_card"] for frame in self._stack)

    def _finish_card(self) -> None:
        card = self._current_card
        self._current_card = None
        if card is not None:
            self.cards.append(card)

    # -- monetary value scanning -----------------------------------------

    def _record(self, card: _TariffCard, text: str) -> None:
        per_kwh = _PER_KWH_RE.search(text)
        per_day = _PER_DAY_RE.search(text)
        if per_kwh:
            card.per_kwh = _parse_decimal(per_kwh.group("value"))
        if per_day:
            card.daily_supply = _parse_decimal(per_day.group("value"))

    def _check_stray(self, text: str) -> None:
        if _PER_KWH_RE.search(text) or _PER_DAY_RE.search(text):
            self._stray_label = True

    # -- result ----------------------------------------------------------

    def result(self) -> list[_TariffCard]:
        # Close any cards still open (e.g. unclosed tags at EOF).
        self._finish_card()
        if self._stray_label:
            raise ExtractionError(
                "Tariff page contains a monetary per kWh/per day label outside "
                "a tariff card."
            )
        return self.cards


def extract_tariff_rates(
    html: str, account_id: str, observed_at: datetime
) -> list[TariffRate]:
    """Extract one TariffRate per tariff card from an Ergon tariff page."""

    if not isinstance(html, str):
        raise ExtractionError("Tariff page must be a string.")
    parser = _TariffPageParser()
    parser.feed(html)
    parser.close()
    # A bare heading whose scope contains no labelled monetary value at all is
    # a page/section heading, not a tariff card.  A heading inside a card
    # container is always a tariff card, even if its values are missing (that
    # fails validation below).
    cards = [
        card
        for card in parser.result()
        if card.in_card_container
        or card.per_kwh is not None
        or card.daily_supply is not None
    ]

    if not cards:
        raise ExtractionError("No tariff cards found on the tariff page.")

    rates: list[TariffRate] = []
    seen: set[str] = set()
    for card in cards:
        if card.name in seen:
            raise ExtractionError(f"Duplicate tariff card for {card.name!r}.")
        seen.add(card.name)
        if card.per_kwh is None:
            raise ExtractionError(f"Tariff card {card.name!r} has no per kWh value.")
        if card.per_kwh < 0 or (card.daily_supply is not None and card.daily_supply < 0):
            raise ExtractionError(f"Tariff card {card.name!r} has a negative rate.")
        rates.append(
            TariffRate(
                account_id=account_id,
                tariff=card.name,
                observed_at=observed_at,
                per_kwh_aud=card.per_kwh,
                daily_supply_aud=card.daily_supply,
            )
        )
    return rates
