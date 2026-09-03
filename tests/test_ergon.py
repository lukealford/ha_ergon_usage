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

import asyncio
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
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

LOGIN_URL = "https://myaccount.ergonretail.com.au/portal"
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


class FakeMouse:
    """Records mouse click coordinates (verification click forwarding)."""

    def __init__(self, page: "FakePage") -> None:
        self.page = page
        self.clicks: list[tuple[int, int]] = []

    async def click(self, x: int, y: int) -> None:
        self.clicks.append((x, y))
        self.page.clicked.append(f"mouse:{x},{y}")


class FakePage:
    """Fake Page: navigation, form fill, response events, content."""

    def __init__(self, scenario: "Scenario") -> None:
        self.scenario = scenario
        self.url = "about:blank"
        self.filled: list[tuple[str, str]] = []
        self.clicked: list[str] = []
        self.listeners: list = []
        self.evaluate_result: object = []
        self.evaluate_calls: list[str] = []
        self.mouse = FakeMouse(self)
        self.screenshot_count = 0
        self.screenshot_error: Exception | None = None
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

        async def all(self) -> list:
            return []

    class _FakeHeading:
        """One heading element returned by locator(...).all()."""

        def __init__(self, page: "FakePage", text: str) -> None:
            self._page = page
            self.text = text

        async def text_content(self) -> str:
            return self.text

        async def click(self, timeout=None) -> None:
            self._page.clicked.append(self.text)
            self._page.scenario.navigations.append(f"click:heading:{self.text}")

    def locator(self, selector: str) -> "FakePage._Locator":
        if selector == "h1, h2, h3":
            return FakePage._HeadingLocator(self)
        if selector == "input":
            # Any visible input-type selector means a rendered form.
            count = 1 if any(
                selector.startswith("input")
                for selector in self.visible_selectors
            ) else 0
            return FakePage._Locator(count)
        return FakePage._Locator(1 if selector in self.visible_selectors else 0)

    class _HeadingLocator:
        """Locator for heading tags; yields headings found in page_html."""

        _HEADING_RE = re.compile(r"<h([1-3])[^>]*>(.*?)</h\1>", re.DOTALL)

        def __init__(self, page: "FakePage") -> None:
            self._page = page

        async def all(self) -> list["FakePage._FakeHeading"]:
            html = self._page.scenario.page_html
            headings = []
            for _level, inner in self._HEADING_RE.findall(html):
                text = " ".join(re.sub(r"<[^>]+>", "", inner).split())
                headings.append(FakePage._FakeHeading(self._page, text))
            return headings

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

    async def wait_for_selector(self, selector: str, timeout=None) -> None:
        """Model real Playwright: waits until the selector exists.

        The account-link selector (a[href*="/portal/A-"]) only appears on
        successful login.  Any other selector (login form inputs) is always
        present in the fake.
        """
        if "/portal/A-" in selector:
            self.scenario.waited_for_url += 1
            if self.scenario.login_succeeds:
                self.url = self.scenario.post_login_url
            else:
                raise FakeTimeoutError(f"Timeout {timeout}ms waiting for {selector}.")
        return

    async def wait_for_timeout(self, _ms: int) -> None:
        return

    async def evaluate(self, script: str) -> object:
        """Default to no chart payloads so chart extraction is a no-op."""

        self.evaluate_calls.append(script)
        # The shape-count diagnostic query returns a number; it is the only
        # evaluate that targets '.recharts-bar-rectangle' WITHOUT walking
        # React fibers.
        if "recharts-bar-rectangle" in script and "__react" not in script:
            return 0
        return self.evaluate_result

    async def screenshot(self, type: str = "png") -> bytes:
        self.screenshot_count += 1
        if self.screenshot_error is not None:
            raise self.screenshot_error
        return b"fake-png-bytes"

    async def content(self) -> str:
        return self.scenario.page_html

    async def close(self) -> None:
        self.scenario.pages_closed += 1


class FakeContext:
    def __init__(self, scenario: "Scenario") -> None:
        self.scenario = scenario
        self.closed = False
        self.init_scripts: list[str] = []
        self.added_cookies: list[list[dict]] = []

    async def add_init_script(self, script: str) -> None:
        self.init_scripts.append(script)

    async def add_cookies(self, cookies: list[dict]) -> None:
        self.added_cookies.append(cookies)

    async def cookies(self) -> list[dict]:
        return []

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


def rolling_url(account: str = "A-TEST123", *, today: date | None = None) -> str:
    today = today or datetime.now(ZoneInfo("Australia/Brisbane")).date()
    start = today - timedelta(days=2)
    return (
        f"https://myaccount.ergonretail.com.au/portal/{account}/tariff-metering/usage"
        f"?periodDays=custom&startDate={start.strftime('%d/%m/%Y')}"
        f"&endDate={today.strftime('%d/%m/%Y')}"
    )


def day_url(day: date, account: str = "A-TEST123") -> str:
    return (
        f"https://myaccount.ergonretail.com.au/portal/{account}/tariff-metering/usage"
        f"?periodDays=custom&startDate={day.strftime('%d/%m/%Y')}"
        f"&endDate={day.strftime('%d/%m/%Y')}"
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
        # Portal visited first; unauthenticated visitors are redirected to
        # the auth sign-in page.
        assert scenario.navigations[0] == LOGIN_URL

    @pytest.mark.asyncio
    async def test_rolling_uses_period_days_parameter(self):
        scenario = Scenario()
        scenario.usage_responses = [usage_json_response()]
        await make_client(scenario).fetch_rolling()
        assert rolling_url() in scenario.navigations

    @pytest.mark.asyncio
    async def test_chart_path_used_when_xhr_yields_nothing(self, monkeypatch):
        scenario = Scenario()
        # XHR captures an invalid payload; the Recharts evaluate returns
        # real hourly rows instead.
        scenario.usage_responses = [
            usage_json_response(payload={"series": [{"name": "Tariff 11", "data": []}]})
        ]

        async def evaluate(self: FakePage, script: str) -> object:
            self.evaluate_calls.append(script)
            if "recharts-bar-rectangle" in script and "__react" not in script:
                return 2  # shape count
            return [
                {"series": "Tariff 11", "dataKey": "RTC11", "payload": {"date": "31 Aug 12:00AM", "day": "2026-08-30 14:00:00+00:00", "RTC11": 1.25}},
                {"series": "Tariff 11", "dataKey": "RTC11", "payload": {"date": "31 Aug 01:00AM", "day": "2026-08-30 15:00:00+00:00", "RTC11": 0.5}},
                {"series": "Tariff 33", "dataKey": "RTC33", "payload": {"date": "31 Aug 01:00AM", "day": "2026-08-30 15:00:00+00:00", "RTC33": 0.75}},
            ]

        monkeypatch.setattr(FakePage, "evaluate", evaluate)
        result = await make_client(scenario).fetch_rolling()
        assert result.source == "chart"
        assert len(result.readings) == 3
        assert {(r.tariff, r.kwh) for r in result.readings} == {
            ("Tariff 11", Decimal("1.25")),
            ("Tariff 11", Decimal("0.5")),
            ("Tariff 33", Decimal("0.75")),
        }

    @pytest.mark.asyncio
    async def test_dom_fallback_when_no_valid_json(self, monkeypatch):
        scenario = Scenario()
        scenario.usage_responses = [
            usage_json_response(payload={"series": [{"name": "Tariff 11", "data": []}]})
        ]
        scenario.page_html = (
            '<div data-tariff="Tariff 11" data-timestamp="31 Aug 2026 12:00AM" '
            'data-kwh="1.25"></div>'
        )

        async def evaluate(self: FakePage, script: str) -> object:
            self.evaluate_calls.append(script)
            # No chart on the page: payload read yields [], shape count 0.
            if "recharts-bar-rectangle" in script and "__react" not in script:
                return 0
            return []

        monkeypatch.setattr(FakePage, "evaluate", evaluate)
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
        # Only ONE page is created: the login tab is reused for the usage
        # fetch (the WAF-protected SPA renders a second tab without data).
        assert len(pages) == 1
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
    async def test_fetch_rates_expands_tariff_accordions_before_content(self):
        # The live portal renders tariffs as collapsed accordions; fetch_rates
        # must click every tariff heading BEFORE reading page.content().
        scenario = Scenario()
        scenario.page_html = TARIFF_HTML
        await make_client(scenario).fetch_rates()
        tariff_clicks = [
            nav for nav in scenario.navigations if "Tariff 11" in nav or "Tariff 33" in nav
        ]
        assert tariff_clicks, "expected tariff heading clicks before content read"
        assert scenario.navigations.index(tariff_clicks[0]) < len(scenario.navigations)

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


def _ergon_cookie(name: str = "aws-waf-token", expires: float | None = None) -> dict:
    import time as _time

    return {
        "name": name,
        "value": "secret-token-value",
        "domain": ".myaccount.ergonretail.com.au",
        "path": "/",
        "expires": expires if expires is not None else _time.time() + 86400,
    }


def _unrelated_cookie() -> dict:
    return {
        "name": "other",
        "value": "x",
        "domain": ".example.org",
        "path": "/",
        "expires": 9999999999,
    }


class TestWafTokenStore:
    def test_save_load_round_trip_filters_non_ergon_cookies(self, tmp_path):
        from app.ergon import WafTokenStore

        store = WafTokenStore(tmp_path / "nested" / "waf_state.json")
        cookies = [_ergon_cookie(), _unrelated_cookie()]
        store.save(cookies)
        loaded = store.load()
        assert loaded is not None
        assert len(loaded) == 1
        assert loaded[0]["name"] == "aws-waf-token"

    def test_load_returns_none_when_file_absent(self, tmp_path):
        from app.ergon import WafTokenStore

        assert WafTokenStore(tmp_path / "waf_state.json").load() is None

    def test_load_returns_none_when_token_expired(self, tmp_path):
        import time

        from app.ergon import WafTokenStore

        path = tmp_path / "waf_state.json"
        store = WafTokenStore(path)
        store.save([_ergon_cookie(expires=time.time() - 10)])
        assert store.load() is None

    def test_load_returns_none_on_corrupt_file(self, tmp_path):
        from app.ergon import WafTokenStore

        path = tmp_path / "waf_state.json"
        path.write_text("{not json", encoding="utf-8")
        assert WafTokenStore(path).load() is None

    def test_load_returns_none_on_non_list_json(self, tmp_path):
        from app.ergon import WafTokenStore

        path = tmp_path / "waf_state.json"
        path.write_text('{"oops": 1}', encoding="utf-8")
        assert WafTokenStore(path).load() is None


class TestWafStoreIntegration:
    @pytest.mark.asyncio
    async def test_client_restores_and_persists_cookies(self, tmp_path, caplog):
        from app.ergon import WafTokenStore

        store = WafTokenStore(tmp_path / "waf_state.json")
        # Pre-seed so the run restores cookies into the context.
        store.save([_ergon_cookie()])

        scenario = Scenario()
        scenario.usage_responses = [usage_json_response()]
        contexts: list[FakeContext] = []
        original_new_context = FakeBrowser.new_context

        async def new_context(self: FakeBrowser, **kwargs) -> FakeContext:
            context = await original_new_context(self, **kwargs)
            contexts.append(context)
            return context

        async def get_cookies(self: FakeContext) -> list[dict]:
            return [_ergon_cookie(), _unrelated_cookie()]

        FakeBrowser.new_context = new_context  # type: ignore[method-assign]
        FakeContext.cookies = get_cookies  # type: ignore[attr-defined]
        try:
            result = await ErgonClient(
                Settings(), browser_factory=scenario.factory, waf_store=store
            ).fetch_rolling()
        finally:
            FakeBrowser.new_context = original_new_context  # type: ignore[method-assign]
            del FakeContext.cookies
        assert result.source == "structured"
        # Pre-seeded cookies were pushed into the context.
        assert contexts[0].added_cookies and contexts[0].added_cookies[0][0][
            "name"
        ] == "aws-waf-token"
        # Exit-time save filtered to Ergon domains only.
        loaded = store.load()
        assert loaded is not None
        assert [c["name"] for c in loaded] == ["aws-waf-token"]
        # Cookie values never appear in logs.
        with caplog.at_level(logging.WARNING):
            pass
        assert "secret-token-value" not in caplog.text

    @pytest.mark.asyncio
    async def test_client_without_store_does_no_cookie_io(self):
        scenario = Scenario()
        scenario.usage_responses = [usage_json_response()]
        contexts: list[FakeContext] = []
        original_new_context = FakeBrowser.new_context

        async def new_context(self: FakeBrowser, **kwargs) -> FakeContext:
            context = await original_new_context(self, **kwargs)
            contexts.append(context)
            return context

        FakeBrowser.new_context = new_context  # type: ignore[method-assign]
        try:
            await make_client(scenario).fetch_rolling()
        finally:
            FakeBrowser.new_context = original_new_context  # type: ignore[method-assign]
        assert contexts[0].added_cookies == []

    @pytest.mark.asyncio
    async def test_stale_cookie_format_does_not_crash_run(self, tmp_path):
        from app.ergon import WafTokenStore

        store = WafTokenStore(tmp_path / "waf_state.json")
        store.save([_ergon_cookie()])

        scenario = Scenario()
        scenario.usage_responses = [usage_json_response()]

        async def add_cookies(self: FakeContext, cookies: list[dict]) -> None:
            raise RuntimeError("stale format")

        FakeContext.add_cookies = add_cookies  # type: ignore[method-assign]
        try:
            result = await ErgonClient(
                Settings(), browser_factory=scenario.factory, waf_store=store
            ).fetch_rolling()
        finally:
            del FakeContext.add_cookies
        assert result.source == "structured"


class TestStealthConstants:
    def test_stealth_js_present_and_addresses_webdriver(self):
        from app.ergon import _STEALTH_JS

        assert _STEALTH_JS.strip()
        assert "webdriver" in _STEALTH_JS

    def test_launch_args_disable_automation_controlled(self):
        from app.ergon import _LAUNCH_ARGS

        assert "--disable-blink-features=AutomationControlled" in _LAUNCH_ARGS

    @pytest.mark.asyncio
    async def test_context_gets_stealth_init_script_and_locale(self):
        scenario = Scenario()
        scenario.usage_responses = [usage_json_response()]
        contexts: list[FakeContext] = []
        kwargs_seen: list[dict] = []
        original_new_context = FakeBrowser.new_context

        async def new_context(self: FakeBrowser, **kwargs) -> FakeContext:
            kwargs_seen.append(kwargs)
            context = await original_new_context(self, **kwargs)
            contexts.append(context)
            return context

        FakeBrowser.new_context = new_context  # type: ignore[method-assign]
        try:
            await make_client(scenario).fetch_rolling()
        finally:
            FakeBrowser.new_context = original_new_context  # type: ignore[method-assign]
        assert contexts[0].init_scripts and "webdriver" in contexts[0].init_scripts[0]
        context_kwargs = kwargs_seen[0]
        assert context_kwargs["locale"] == "en-AU"
        assert context_kwargs["timezone_id"] == "Australia/Brisbane"
        assert context_kwargs["viewport"] == {"width": 1280, "height": 800}


class _VerifyScenario:
    """Fake backend for VerificationSession: one tracked page/context."""

    def __init__(self) -> None:
        self.page = FakePage(Scenario())
        self.context = FakeContext(self.page.scenario)
        self.context_cookies: list[dict] = []
        self.context_created: list[FakeContext] = []
        self.browser = FakeBrowser(self.page.scenario)
        # Route every new_context() to the tracked context.
        async def _new_context(**_kwargs) -> FakeContext:
            return self.context
        self.browser.new_context = _new_context  # type: ignore[method-assign]
        # Route context.new_page() to the tracked page.
        async def _new_page() -> FakePage:
            self.page.scenario.pages_created += 1
            return self.page
        self.context.new_page = _new_page  # type: ignore[method-assign]

    def factory(self, _settings: object):
        browser = self.browser

        class _Ctx:
            async def __aenter__(self_inner):
                return browser

            async def __aexit__(self_inner, *_exc) -> None:
                await browser.close()

        return _Ctx()


def _ver_store(tmp_path):
    from app.ergon import WafTokenStore

    return WafTokenStore(tmp_path / "waf_state.json")


class TestVerificationSession:
    @pytest.mark.asyncio
    async def test_challenge_state_detected_when_no_inputs(self, tmp_path):
        from app.ergon import VerificationSession

        fake = _VerifyScenario()
        fake.page.visible_selectors = set()  # challenge page: hidden inputs only
        session = VerificationSession(Settings(), None, browser_factory=fake.factory)
        await session.start()
        assert session.status == "challenge"
        await session.close()

    @pytest.mark.asyncio
    async def test_signin_state_detected_with_login_inputs(self, tmp_path):
        from app.ergon import VerificationSession

        fake = _VerifyScenario()
        session = VerificationSession(Settings(), None, browser_factory=fake.factory)
        await session.start()
        assert session.status == "signin"
        await session.close()

    @pytest.mark.asyncio
    async def test_done_immediately_when_account_link_present(self, tmp_path):
        from app.ergon import VerificationSession

        fake = _VerifyScenario()
        cookie = {"name": "aws-waf-token", "domain": ".myaccount.ergonretail.com.au",
                  "expires": 9999999999.0, "value": "v"}
        store = _ver_store(tmp_path)
        fake.context_cookies = [cookie]
        async def _cookies() -> list[dict]:
            return fake.context_cookies
        fake.context.cookies = _cookies  # type: ignore[method-assign]
        fake.page.visible_selectors = {'a[href*="/portal/A-"]'}
        session = VerificationSession(Settings(), store, browser_factory=fake.factory)
        await session.start()
        assert session.status == "done"
        # Session valid: cookies saved to the store file.
        assert store.load() == [cookie]
        await session.close()

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self, tmp_path):
        from app.ergon import VerificationSession

        fake = _VerifyScenario()
        session = VerificationSession(Settings(), None, browser_factory=fake.factory)
        await session.start()
        await session.start()
        assert fake.page.scenario.pages_created == 1
        await session.close()

    @pytest.mark.asyncio
    async def test_click_forwards_coordinates_and_reclassifies(self, tmp_path):
        from app.ergon import VerificationSession

        fake = _VerifyScenario()
        fake.page.visible_selectors = set()  # starts as challenge
        session = VerificationSession(Settings(), None, browser_factory=fake.factory)
        await session.start()

        original_mouse_click = fake.page.mouse.click

        async def click_solves(x, y):
            await original_mouse_click(x, y)
            fake.page.visible_selectors = {'input[type="email"]'}

        fake.page.mouse.click = click_solves  # type: ignore[method-assign]
        await session.click(400, 300)
        assert fake.page.mouse.clicks == [(400, 300)]
        assert session.status == "signin"  # reclassified after the click
        await session.close()

    @pytest.mark.asyncio
    async def test_screenshot_returns_png_bytes_or_none(self, tmp_path):
        from app.ergon import VerificationSession

        fake = _VerifyScenario()
        session = VerificationSession(Settings(), None, browser_factory=fake.factory)
        assert await session.screenshot() is None  # not started
        await session.start()
        assert await session.screenshot() == b"fake-png-bytes"
        await session.close()
        assert await session.screenshot() is None

    @pytest.mark.asyncio
    async def test_error_status_on_navigation_failure(self, tmp_path):
        from app.ergon import VerificationSession

        fake = _VerifyScenario()
        fake.page.scenario.errors[LOGIN_URL] = RuntimeError("net down")
        session = VerificationSession(Settings(), None, browser_factory=fake.factory)
        await session.start()
        assert session.status == "error"
        message = session.error_message or ""
        assert "net down" not in message  # sanitized
        await session.close()

    @pytest.mark.asyncio
    async def test_monitor_timeout_closes_browser(self, tmp_path, monkeypatch):
        """On timeout the session must fully tear down the browser (C1)."""
        import app.ergon as ergon_mod
        from app.ergon import VerificationSession

        monkeypatch.setattr(ergon_mod, "VERIFY_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(ergon_mod, "VERIFY_POLL_SECONDS", 0.01)
        fake = _VerifyScenario()
        fake.page.visible_selectors = set()  # challenge page never advances
        session = VerificationSession(Settings(), None, browser_factory=fake.factory)
        await session.start()
        assert session.status == "challenge"
        for _ in range(100):
            if session.status == "error":
                break
            await asyncio.sleep(0.02)
        assert session.status == "error"
        assert "timed out" in (session.error_message or "").lower()
        # No orphaned browser: fake context/browser/opener all closed.
        assert fake.context.closed
        assert fake.browser.closed
        await session.close()  # must be safe to call again

    @pytest.mark.asyncio
    async def test_close_without_account_link_does_not_save_cookies(self, tmp_path):
        from app.ergon import VerificationSession

        fake = _VerifyScenario()
        store = _ver_store(tmp_path)
        fake.context_cookies = [{"name": "aws-waf-token", "domain": ".ergonretail.com.au",
                                 "expires": 9999999999.0, "value": "v"}]
        session = VerificationSession(Settings(), store, browser_factory=fake.factory)
        await session.start()
        await session.close()
        assert store.load() is None

    @pytest.mark.asyncio
    async def test_fill_login_submits_then_reaches_done(self, tmp_path, monkeypatch):
        import app.ergon as ergon_mod
        from app.ergon import VerificationSession

        monkeypatch.setattr(ergon_mod, "VERIFY_POLL_SECONDS", 0.01)
        fake = _VerifyScenario()
        session = VerificationSession(Settings(), None, browser_factory=fake.factory)
        await session.start()  # status "signin"
        await session.fill_login("e@example.com", "pw")
        assert ("input[type=\"email\"]", "e@example.com") in fake.page.filled
        # Submit takes the operator to a dashboard; the monitor spots it.
        fake.page.visible_selectors = {'a[href*="/portal/A-"]'}
        for _ in range(100):
            if session.status == "done":
                break
            await asyncio.sleep(0.02)
        assert session.status == "done"
        await session.close()

    @pytest.mark.asyncio
    async def test_monitor_polls_until_done(self, tmp_path, monkeypatch):
        import app.ergon as ergon_mod
        from app.ergon import VerificationSession

        monkeypatch.setattr(ergon_mod, "VERIFY_POLL_SECONDS", 0.01)
        fake = _VerifyScenario()
        session = VerificationSession(Settings(), None, browser_factory=fake.factory)
        await session.start()  # "signin"
        fake.page.visible_selectors = {'a[href*="/portal/A-"]'}
        for _ in range(100):
            if session.status == "done":
                break
            await asyncio.sleep(0.02)
        assert session.status == "done"
        await session.close()


class TestVerificationManager:
    @pytest.mark.asyncio
    async def test_concurrent_start_creates_one_session(self, tmp_path):
        """Overlapping start() calls must not leak a second browser (I1)."""
        from app.ergon import VerificationManager

        fake = _VerifyScenario()
        creations = []

        original_factory = fake.factory

        def counting_factory(settings):
            creations.append(1)
            return original_factory(settings)

        manager = VerificationManager(
            Settings(), None, browser_factory=counting_factory
        )
        # Gather without awaiting the first fully: both race on the lock.
        states = await asyncio.gather(manager.start(), manager.start())
        assert len(creations) == 1
        assert all(state["status"] == "signin" for state in states)
        await manager.stop()
    @pytest.mark.asyncio
    async def test_single_live_session_and_sanitized_state(self, tmp_path):
        from app.ergon import VerificationManager

        fake = _VerifyScenario()
        manager = VerificationManager(Settings(), None, browser_factory=fake.factory)
        state = await manager.start()
        assert state["status"] == "signin"
        assert set(state) == {"active", "status", "error"}
        # Starting again reuses the live session instead of leaking a second.
        fake.browser.closed = False
        await manager.start()
        assert not fake.browser.closed
        await manager.stop()
        assert fake.browser.closed

    @pytest.mark.asyncio
    async def test_stop_reports_and_clears(self, tmp_path):
        from app.ergon import VerificationManager

        fake = _VerifyScenario()
        manager = VerificationManager(Settings(), None, browser_factory=fake.factory)
        await manager.start()
        result = await manager.stop()
        assert result["stopped"] is True
        state = await manager.status()
        assert state["active"] is False

    @pytest.mark.asyncio
    async def test_manager_never_exposes_cookies(self, tmp_path):
        from app.ergon import VerificationManager

        fake = _VerifyScenario()
        fake.context_cookies = [{"name": "aws-waf-token", "domain": ".ergonretail.com.au",
                                 "expires": 9999999999.0, "value": "super-secret"}]
        manager = VerificationManager(Settings(), None, browser_factory=fake.factory)
        state = await manager.start()
        blob = json.dumps(state)
        assert "super-secret" not in blob
        await manager.stop()
