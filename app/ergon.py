"""Playwright-driven client for the Ergon myAccount portal.

The client owns the browser lifecycle and login flow only; retries and
delays belong to the coordinator.  Credentials, cookies, response bodies,
headers, and tokens are never logged.

Playwright is imported lazily inside the default browser factory so this
module (and the whole test suite) imports cleanly without playwright
installed.  Tests inject a ``browser_factory`` and never import playwright.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Awaitable, Callable, Literal

from .config import Settings
from .errors import AccountDiscoveryError, AuthenticationError, ExtractionError
from .extractor import CapturedJson, select_usage_payload, extract_dom
from .models import TariffRate, UsageReading
from .normalize import discover_single_account
from .tariff_rates import extract_tariff_rates

logger = logging.getLogger(__name__)

LOGIN_URL = "https://login.myaccount.ergonretail.com.au/"
PORTAL_BASE = "https://myaccount.ergonretail.com.au/portal"
USAGE_URL_TEMPLATE = PORTAL_BASE + "/{account}/usage"
TARIFF_URL_TEMPLATE = PORTAL_BASE + "/{account}/tariff-metering"

# Bounded wait (ms) for the portal to load after submitting credentials.
LOGIN_WAIT_MS = 15_000

# Generous wait (ms) for a human to solve the AWS WAF challenge in headful
# mode before the login form appears.
CHALLENGE_WAIT_MS = 300_000

# Bot protection rejects Playwright's default "HeadlessChrome" UA.  This UA
# matches the Chromium version Playwright ships with, minus the headless tag.
REALISTIC_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

# Ordered, explicit selectors for the login form.  The portal's inputs are
# only distinguishable by their aria-labels ("Email Address" / "Password"),
# so those come first, with type/name/id fallbacks.  The first selector that
# resolves to an element is used.
EMAIL_SELECTORS = (
    'input[aria-label="Email Address"]',
    'input[type="email"]',
    'input[name="email"]',
    'input[id="email"]',
    'input[autocomplete="username"]',
)
PASSWORD_SELECTORS = (
    'input[aria-label="Password"]',
    'input[type="password"]',
    'input[name="password"]',
    'input[id="password"]',
    'input[autocomplete="current-password"]',
)
SUBMIT_SELECTORS = (
    'button[type="submit"]',
    'input[type="submit"]',
)


@dataclass(frozen=True)
class FetchResult:
    account_id: str
    readings: tuple[UsageReading, ...]
    source: Literal["structured", "dom"]


BrowserOpener = Callable[[Settings], object]


async def _first_visible(page, selectors: tuple[str, ...]) -> str:
    """Return the first selector that matches an element on the page."""

    for selector in selectors:
        if await page.locator(selector).count() > 0:
            return selector
    raise AuthenticationError("Login form fields could not be found.")


class ErgonClient:
    """Automates the Ergon portal with one authenticated browser per run."""

    def __init__(
        self,
        settings: Settings,
        browser_factory: BrowserOpener | None = None,
        *,
        headful: bool = False,
    ) -> None:
        self._settings = settings
        if browser_factory is None:
            self._headful = headful
            browser_factory = lambda settings: _default_browser_factory(
                settings, headful=headful
            )
        else:
            # Injected factories manage their own launch options.
            self._headful = False
        self._browser_factory = browser_factory

    # -- public API ------------------------------------------------------

    async def fetch_rolling(self) -> FetchResult:
        """Fetch the last three days of usage and return one payload."""

        return await self._fetch_usage(None)

    async def fetch_day(self, day: date) -> FetchResult:
        """Fetch usage for a single Brisbane day."""

        if not isinstance(day, date):
            raise TypeError("day must be a date.")
        return await self._fetch_usage(day)

    async def fetch_rates(self) -> tuple[TariffRate, ...]:
        """Fetch the tariff-metering page and parse its rate cards."""

        observed_at = datetime.now(timezone.utc)
        async with self._run() as portal:
            url = TARIFF_URL_TEMPLATE.format(account=portal.account_id)
            page = await portal.context.new_page()
            try:
                await page.goto(url)
                html = await page.content()
            finally:
                await page.close()
        rates = extract_tariff_rates(html, portal.account_id, observed_at)
        return tuple(rates)

    # -- internals --------------------------------------------------------

    def _usage_url(self, account_id: str, day: date | None) -> str:
        if day is None:
            return USAGE_URL_TEMPLATE.format(account=account_id) + "?periodDays=3"
        return (
            USAGE_URL_TEMPLATE.format(account=account_id)
            + f"?day={day.strftime('%d/%m/%Y')}"
        )

    async def _fetch_usage(self, day: date | None) -> FetchResult:
        candidates: list[CapturedJson] = []
        async with self._run() as portal:
            page = await portal.context.new_page()
            try:
                page.on(
                    "response",
                    lambda response: _capture_response(response, candidates),
                )
                await page.goto(self._usage_url(portal.account_id, day))
                html = await page.content()
            finally:
                await page.close()
        return _resolve_readings(portal.account_id, candidates, html, day)

    def _run(self):
        """Context manager performing login and account discovery."""

        return _AuthenticatedRun(
            self._settings, self._browser_factory, headful=self._headful
        )



class _Portal:
    """Result of a successful login plus account discovery."""

    def __init__(self, context, account_id: str) -> None:
        self.context = context
        self.account_id = account_id


class _AuthenticatedRun:
    """Async context manager: one browser, one context, login, discovery."""

    def __init__(
        self, settings: Settings, browser_factory: BrowserOpener, *, headful: bool = False
    ) -> None:
        self._settings = settings
        self._browser_factory = browser_factory
        self._headful = headful
        self._opener = None
        self._browser = None
        self._context = None

    async def __aenter__(self) -> _Portal:
        self._opener = self._browser_factory(self._settings)
        self._browser = await self._opener.__aenter__()
        try:
            # The portal's bot protection (Cloudflare/AWS WAF) rejects
            # Playwright's default "HeadlessChrome" user agent outright.
            # Present a normal Chrome UA on the same engine version.
            self._context = await self._browser.new_context(
                user_agent=REALISTIC_USER_AGENT
            )
            page = await self._context.new_page()
            try:
                await self._login(page)
                account_id = await self._discover_account(page)
            finally:
                await page.close()
            return _Portal(self._context, account_id)
        except Exception:
            await self._cleanup()
            raise

    async def __aexit__(self, *_exc) -> None:
        await self._cleanup()

    async def _cleanup(self) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:  # noqa: BLE001 - cleanup must never mask errors
                logger.warning("Browser context close failed.")
            self._context = None
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:  # noqa: BLE001
                logger.warning("Browser close failed.")
            self._browser = None
        if self._opener is not None:
            try:
                await self._opener.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                logger.warning("Playwright shutdown failed.")
            self._opener = None

    async def _login(self, page) -> None:
        # Navigate to the portal; unauthenticated visitors are redirected to
        # the auth sign-in page (confirmed live: /portal ->
        # /auth/signin?callbackUrl=/portal).
        await page.goto(PORTAL_BASE, wait_until="domcontentloaded")
        # A WAF challenge page shows no inputs until solved; in headful mode
        # the operator solves it manually, so allow generous time.  The
        # challenge auto-advances to the sign-in form once passed.
        try:
            await page.wait_for_selector("input", timeout=LOGIN_WAIT_MS if not self._headful else CHALLENGE_WAIT_MS)
        except Exception:  # noqa: BLE001 - challenge or blocked page
            raise AuthenticationError() from None
        email_selector = await _first_visible(page, EMAIL_SELECTORS)
        password_selector = await _first_visible(page, PASSWORD_SELECTORS)
        await page.fill(email_selector, self._settings.ergon_email)
        await page.fill(password_selector, self._settings.ergon_password)
        submit_selector = await _first_visible(page, SUBMIT_SELECTORS)
        await page.click(submit_selector)
        # The portal is a single-page app: after login the URL stays at
        # /portal while the dashboard renders.  Wait for the account link
        # (e.g. /portal/A-XXXXXXXX) to appear in the nav instead of watching
        # the URL.  A failed login never renders it, so the bounded wait
        # times out and is converted into an AuthenticationError.
        try:
            await page.wait_for_selector(
                'a[href*="/portal/A-"]',
                timeout=CHALLENGE_WAIT_MS if self._headful else LOGIN_WAIT_MS,
            )
        except Exception:  # noqa: BLE001 - navigation failure means bad login
            raise AuthenticationError() from None

    @staticmethod
    async def _discover_account(page) -> str:
        # The login wait has already confirmed an account link exists; this
        # collects the hrefs (including the SPA nav) and extracts the single
        # A-... account from them.
        links = await _page_hrefs(page)
        try:
            return discover_single_account(links)
        except AccountDiscoveryError:
            # Fall back to the URL itself (older flows embedded the account).
            try:
                return discover_single_account([str(page.url)])
            except AccountDiscoveryError:
                raise AccountDiscoveryError() from None


async def _page_hrefs(page) -> list[str]:
    """Best-effort href collection from the post-login page."""

    try:
        return await page.eval_on_selector_all(
            "a[href]", "elements => elements.map(e => e.href)"
        )
    except Exception:  # noqa: BLE001 - portal markup may differ
        return []


async def _capture_response(response, candidates: list[CapturedJson]) -> None:
    """Retain parsed JSON payloads in memory only; never log content."""

    try:
        url = response.url
        status = response.status
        content_type = response.headers.get("content-type", "")
    except Exception:  # noqa: BLE001 - detached responses are skipped
        return
    if "json" not in content_type.lower():
        return
    try:
        payload = await response.json()
    except Exception:  # noqa: BLE001 - non-JSON bodies are skipped
        return
    candidates.append(
        CapturedJson(url=url, status=status, content_type=content_type, payload=payload)
    )


def _resolve_readings(
    account_id: str,
    candidates: list[CapturedJson],
    html: str,
    day: date | None,
) -> FetchResult:
    """Prefer a structured payload; fall back to DOM extraction once."""

    try:
        readings = select_usage_payload(candidates, account_id, day)
        source: Literal["structured", "dom"] = "structured"
    except ExtractionError:
        readings = extract_dom(html, account_id, day)
        source = "dom"
    return FetchResult(account_id=account_id, readings=tuple(readings), source=source)


def _default_browser_factory(settings: Settings, headful: bool = False):
    """Launch Chromium; playwright is imported lazily here."""

    from playwright.async_api import async_playwright  # noqa: PLC0415

    class _Opener:
        async def __aenter__(self):
            self._pw = await async_playwright().start()
            return await self._pw.chromium.launch(headless=not headful)

        async def __aexit__(self, *_exc) -> None:
            await self._pw.stop()

    return _Opener()
