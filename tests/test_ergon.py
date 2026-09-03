"""Browser workflow tests for ErgonClient using fake Playwright objects.

No real browser is launched and playwright is never imported here.  The
fakes below mimic only the slice of the Playwright API that ``ErgonClient``
uses, reusing the sanitized fixture payload as the captured JSON response.

Fake behaviour contract (mirrors real Playwright):
- ``browser_factory(settings)`` returns an async context manager yielding a
  browser; ``browser.new_context()`` is sync; ``context.new_page()`` is async
  (matching the real Playwright API).
- ``page.on("response", handler)`` registers a listener; the page fires the
  handler for responses emitted around a navigation.
- ``page.url`` is a plain attribute updated by navigation.
- ``click()`` resolves when the navigation *starts*, not completes: it does
  NOT synchronously update ``page.url``.  Callers must explicitly await
  ``wait_for_url(pattern, timeout=...)``; a failed login never reaches the
  portal URL, so ``wait_for_url`` raises ``FakeTimeoutError``.
"""

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.errors import (
    AccountDiscoveryError,
    AuthenticationError,
    ExtractionError,
)
from app.ergon import ErgonClient, FetchResult
from app.models import TariffRate

FIXTURES = Path(__file__).parent / "fixtures"
STRUCTURED_PAYLOAD = json.loads(
    (FIXTURES / "structured_usage.json").read_text(encoding="utf-8")
)
TARIFF_HTML = (FIXTURES / "tariff_page.html").read_text(encoding="utf-8").replace(
    '        <p class="fine-print">Estimated solar feed-in credit: $15.00 per day.</p>\n',
    "",
)

LOGIN_URL = "https://login.myaccount.ergonretail.com.au/"
PORTAL_URL = "https://myaccount.ergonretail.com.au/portal/A-TEST123/dashboard"
TARIFF_URL = "https://myaccount.ergonretail.com.au/portal/A-TEST123/tariff-metering"


class Settings:
    """Minimal stand-in matching the real Settings attribute surface."""

    def __init__(self) -> None:
        self.ergon_email = "test@example.com"
        self.ergon_password = "not-a-real-password"
        self.request_delay_seconds = 0
        self.retry_limit = 0


class FakeResponse:
    def __init__(self, url: str, status: int, content_type: str, payload: object) -> None:
        self.url = url
        self.status = status
        self.headers = {"content-type": content_type}
        self._payload = payload

    async def json(self) -> object:
        return self._payload


class FakeTimeoutError(Exception):
    """Mimics ``playwright.async_api.TimeoutError`` for failed waits."""


class FakePage:
    """Fake Page: navigation, form fill, response events, content."""

    def __init__(self, scenario: "Scenario") -> None:
        self.scenario = scenario
        self.url = "about:blank"
        self.filled: list[tuple[str, str]] = []
        self.clicked: list[str] = []
        self.listeners: list = []
        self.visible_selectors: set[str] = {
            'input[type="email"]',
            'input[type="password"]',
            'button[type="submit"]',
        }

    class _Locator:
        def __init__(self, count: int) -> None:
            self._count = count

        async def count(self) -> int:
            return self._count

    def locator(self, selector: str) -> "FakePage._Locator":
        return FakePage._Locator(1 if selector in self.visible_selectors else 0)

    def on(self, event: str, handler) -> None:
        assert event == "response"
        self.listeners.append(handler)

    async def emit(self, response: FakeResponse) -> None:
        for handler in list(self.listeners):
            await handler(response)

    async def goto(self, url: str, **_kwargs) -> None:
        self.scenario.navigations.append(url)
        self.url = url
        error = self.scenario.errors.get(url)
        if error is not None:
            raise error
        if "/usage" in url:
            for response in self.scenario.usage_responses:
                await self.emit(response)

    async def fill(self, selector: str, value: str) -> None:
        self.filled.append((selector, value))

    async def click(self, selector: str) -> None:
        self.clicked.append(selector)
        self.scenario.navigations.append(f"click:{selector}")
        # Real Playwright click() resolves when navigation starts, NOT when
        # it completes, so page.url is deliberately NOT updated here.

    async def wait_for_url(self, _pattern: str, timeout=None) -> None:
        self.scenario.waited_for_url += 1
        # Successful submit eventually lands on the portal dashboard URL;
        # failed logins never navigate, so the wait times out.
        if self.scenario.login_succeeds:
            self.url = self.scenario.post_login_url
        else:
            raise FakeTimeoutError(f"Timeout {timeout}ms waiting for URL.")

    async def content(self) -> str:
        return self.scenario.page_html

    async def close(self) -> None:
        self.scenario.pages_closed += 1


class FakeContext:
    def __init__(self, scenario: "Scenario") -> None:
        self.scenario = scenario
        self.closed = False

    async def new_page(self) -> FakePage:
        self.scenario.pages_created += 1
        return FakePage(self.scenario)

    async def close(self) -> None:
        self.closed = True
        self.scenario.context_closed = True


class FakeBrowser:
    def __init__(self, scenario: "Scenario") -> None:
        self.scenario = scenario
        self.closed = False

    async def new_context(self, **_kwargs) -> FakeContext:
        return FakeContext(self.scenario)

    async def close(self) -> None:
        self.closed = True
        self.scenario.browser_closed = True


class Scenario:
    """Configurable fake-backend behaviour for one client run."""

    def __init__(self) -> None:
        self.navigations: list[str] = []
        self.pages_created = 0
        self.pages_closed = 0
        self.context_closed = False
        self.browser_closed = False
        self.login_succeeds = True
        self.waited_for_url = 0
        self.post_login_url = PORTAL_URL
        self.page_html = ""
        self.usage_responses: list[FakeResponse] = []
        self.errors: dict[str, Exception] = {}

    def factory(self, _settings: object):
        browser = FakeBrowser(self)

        class _Ctx:
            async def __aenter__(self_inner) -> FakeBrowser:
                return browser

            async def __aexit__(self_inner, *_exc) -> None:
                await browser.close()

        return _Ctx()


def usage_json_response(
    url: str = PORTAL_URL, payload: object = STRUCTURED_PAYLOAD
) -> FakeResponse:
    return FakeResponse(url, 200, "application/json", payload)


def make_client(scenario: Scenario) -> ErgonClient:
    return ErgonClient(Settings(), browser_factory=scenario.factory)


def rolling_url(account: str = "A-TEST123") -> str:
    return f"https://myaccount.ergonretail.com.au/portal/{account}/usage?periodDays=3"


def day_url(day: date, account: str = "A-TEST123") -> str:
    return (
        f"https://myaccount.ergonretail.com.au/portal/{account}/usage"
        f"?day={day.strftime('%d/%m/%Y')}"
    )


class TestFetchRolling:
    @pytest.mark.asyncio
    async def test_login_discovers_account_and_collects_json(self):
        scenario = Scenario()
        scenario.usage_responses = [usage_json_response()]
        result = await make_client(scenario).fetch_rolling()
        assert isinstance(result, FetchResult)
        assert result.account_id == "A-TEST123"
        assert result.source == "structured"
        assert {r.tariff for r in result.readings} == {"Tariff 11", "Tariff 33"}
        # Lifecycle: every opened page plus context and browser all closed.
        assert scenario.pages_closed == scenario.pages_created > 0
        assert scenario.context_closed
        assert scenario.browser_closed
        # Login page visited first.
        assert scenario.navigations[0] == LOGIN_URL

    @pytest.mark.asyncio
    async def test_rolling_uses_period_days_parameter(self):
        scenario = Scenario()
        scenario.usage_responses = [usage_json_response()]
        await make_client(scenario).fetch_rolling()
        assert rolling_url() in scenario.navigations

    @pytest.mark.asyncio
    async def test_dom_fallback_when_no_valid_json(self):
        scenario = Scenario()
        scenario.usage_responses = [
            usage_json_response(payload={"series": [{"name": "Tariff 11", "data": []}]})
        ]
        scenario.page_html = (
            '<div data-tariff="Tariff 11" data-timestamp="31 Aug 2026 12:00AM" '
            'data-kwh="1.25"></div>'
        )
        result = await make_client(scenario).fetch_rolling()
        assert result.source == "dom"
        assert len(result.readings) == 1
        assert result.readings[0].kwh == Decimal("1.25")
        assert result.readings[0].tariff == "Tariff 11"

    @pytest.mark.asyncio
    async def test_extraction_failure_raises_when_nothing_extracts(self):
        scenario = Scenario()
        scenario.usage_responses = [usage_json_response(payload={"series": []})]
        scenario.page_html = "<p>nothing useful</p>"
        with pytest.raises(ExtractionError) as error:
            await make_client(scenario).fetch_rolling()
        assert error.value.retryable is True
        # Cleanup still happens after the failure.
        assert scenario.browser_closed and scenario.context_closed

    @pytest.mark.asyncio
    async def test_non_json_and_error_responses_are_ignored(self):
        scenario = Scenario()
        scenario.usage_responses = [
            FakeResponse(PORTAL_URL, 401, "application/json", {"error": "denied"}),
            FakeResponse(PORTAL_URL, 200, "text/html", "<html></html>"),
            usage_json_response(),
        ]
        result = await make_client(scenario).fetch_rolling()
        assert result.source == "structured"
        assert result.readings

    @pytest.mark.asyncio
    async def test_fills_email_and_password_before_submit(self):
        scenario = Scenario()
        scenario.usage_responses = [usage_json_response()]
        # Capture the page created by the client to inspect its fills.
        pages: list[FakePage] = []
        original_new_page = FakeContext.new_page

        async def new_page(self: FakeContext) -> FakePage:
            page = await original_new_page(self)
            pages.append(page)
            return page

        FakeContext.new_page = new_page  # type: ignore[method-assign]
        try:
            await make_client(scenario).fetch_rolling()
        finally:
            FakeContext.new_page = original_new_page  # type: ignore[method-assign]
        assert len(pages) == 2  # one login page + one usage page
        login_page = pages[0]
        selectors = [selector for selector, _value in login_page.filled]
        values = [value for _selector, value in login_page.filled]
        assert selectors == ['input[type="email"]', 'input[type="password"]']
        assert values == ["test@example.com", "not-a-real-password"]
        assert login_page.clicked == ['button[type="submit"]']  # submit happened


class TestLogin:
    @pytest.mark.asyncio
    async def test_invalid_login_is_not_retryable(self):
        scenario = Scenario()
        scenario.login_succeeds = False
        with pytest.raises(AuthenticationError) as error:
            await make_client(scenario).fetch_rolling()
        assert error.value.retryable is False
        assert scenario.browser_closed and scenario.context_closed

    @pytest.mark.asyncio
    async def test_portal_without_account_raises_discovery_error(self):
        scenario = Scenario()
        # Successful login but portal URL carries no A-XXXX account segment.
        scenario.post_login_url = "https://myaccount.ergonretail.com.au/portal/home"
        with pytest.raises(AccountDiscoveryError):
            await make_client(scenario).fetch_rolling()
        assert scenario.browser_closed and scenario.context_closed


class TestFetchDay:
    @pytest.mark.asyncio
    async def test_fetch_day_uses_dd_mm_yyyy_parameter(self):
        scenario = Scenario()
        scenario.usage_responses = [usage_json_response()]
        result = await make_client(scenario).fetch_day(date(2026, 8, 31))
        assert day_url(date(2026, 8, 31)) in scenario.navigations
        assert result.account_id == "A-TEST123"
        assert result.source == "structured"

    @pytest.mark.asyncio
    async def test_fetch_day_rejects_readings_outside_requested_day(self):
        scenario = Scenario()
        # The sanitized fixture holds 31 Aug 2026 readings only; requesting a
        # different day yields nothing, which is an extraction failure.
        scenario.usage_responses = [usage_json_response()]
        with pytest.raises(ExtractionError):
            await make_client(scenario).fetch_day(date(2026, 9, 1))


class TestFetchRates:
    @pytest.mark.asyncio
    async def test_fetch_rates_loads_account_tariff_page(self):
        scenario = Scenario()
        scenario.page_html = TARIFF_HTML
        rates = await make_client(scenario).fetch_rates()
        assert TARIFF_URL in scenario.navigations
        assert {rate.tariff for rate in rates} == {"Tariff 11", "Tariff 33"}
        for rate in rates:
            assert isinstance(rate, TariffRate)
            assert rate.account_id == "A-TEST123"
            assert rate.per_kwh_aud > 0

    @pytest.mark.asyncio
    async def test_fetch_rates_observed_at_is_utc_now(self):
        scenario = Scenario()
        scenario.page_html = TARIFF_HTML
        before = datetime.now(timezone.utc)
        rates = await make_client(scenario).fetch_rates()
        after = datetime.now(timezone.utc)
        for rate in rates:
            assert rate.observed_at.tzinfo is not None
            assert before <= rate.observed_at <= after

    @pytest.mark.asyncio
    async def test_fetch_rates_requires_login(self):
        scenario = Scenario()
        scenario.login_succeeds = False
        with pytest.raises(AuthenticationError):
            await make_client(scenario).fetch_rates()


class TestNoSecretLeakage:
    @pytest.mark.asyncio
    async def test_credentials_never_appear_in_navigation(self):
        scenario = Scenario()
        scenario.usage_responses = [usage_json_response()]
        await make_client(scenario).fetch_rolling()
        joined = " ".join(scenario.navigations)
        assert "test@example.com" not in joined
        assert "not-a-real-password" not in joined
