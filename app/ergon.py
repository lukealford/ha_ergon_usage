"""Playwright-driven client for the Ergon myAccount portal.

The client owns the browser lifecycle and login flow only; retries and
delays belong to the coordinator.  Credentials, cookies, response bodies,
headers, and tokens are never logged.

Playwright is imported lazily inside the default browser factory so this
module (and the whole test suite) imports cleanly without playwright
installed.  Tests inject a ``browser_factory`` and never import playwright.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Literal
from zoneinfo import ZoneInfo

from .config import Settings
from .errors import AccountDiscoveryError, AuthenticationError, ExtractionError
from .extractor import (
    CapturedJson,
    extract_chart_payloads,
    extract_dom,
    select_usage_payload,
)
from .models import TariffRate, UsageReading
from .normalize import BRISBANE, discover_single_account
from .tariff_rates import extract_tariff_rates

logger = logging.getLogger(__name__)

LOGIN_URL = "https://login.myaccount.ergonretail.com.au/"
PORTAL_BASE = "https://myaccount.ergonretail.com.au/portal"
USAGE_URL_TEMPLATE = PORTAL_BASE + "/{account}/tariff-metering/usage"
TARIFF_URL_TEMPLATE = PORTAL_BASE + "/{account}/tariff-metering"

# Bounded wait (ms) for the portal to load after submitting credentials.
LOGIN_WAIT_MS = 15_000
# Bounded wait (ms) for the usage page's XHR data to arrive after load.
USAGE_XHR_WAIT_MS = 15_000
# Generous wait (ms) for a human to solve the AWS WAF challenge in headful
# mode before the login form appears.
CHALLENGE_WAIT_MS = 300_000

# Bot protection rejects Playwright's default "HeadlessChrome" UA.  This UA
# matches the Chromium version Playwright ships with, minus the headless tag.
REALISTIC_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

# Standard stealth mitigations: keep Playwright's Chromium from looking
# automated.  Applied identically to headless and headful launches.
_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]

# Runs before any page script: removes ``navigator.webdriver``, fakes a
# benign ``chrome.runtime``, and pins plugins/languages to Chromium defaults.
# Content values only, never credentials or cookies.
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || {runtime: {}};
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5],
});
Object.defineProperty(navigator, 'languages', {
    get: () => ['en-AU', 'en'],
});
"""

# Cookie domains worth persisting: the Ergon portal plus the AWS WAF
# challenge infrastructure that issues the aws-waf-token cookie.
_WAF_COOKIE_DOMAIN_MARKERS = ("ergonretail.com.au", "awswaf")

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

# Walks every rendered Recharts bar shape and reads the React fiber's
# ``memoizedProps.payload`` object, which carries the hourly usage row
# (``{date, day, RTC11: ..., RTC33: ...}``).  Fiber keys are dynamic
# (``__reactFiber$*``), so they are located by prefix.  The walk hops up
# ``return``/``alternate`` links (bounded at 8) because on the live portal the
# props carrying ``payload`` sometimes sit a few fiber nodes above the shape
# element itself.  Returns ``[]`` when no payloads are found.
_RECHARTS_PAYLOAD_JS = """
() => {
    const shapes = [...document.querySelectorAll(
        '.recharts-bar-rectangle path, .recharts-bar-rectangle rect')];
    const rows = [];
    for (const el of shapes) {
        const keys = Object.keys(el).filter(k => k.startsWith('__react'));
        for (const k of keys) {
            let node = el[k];
            for (let hop = 0; hop < 8 && node; hop++) {
                const props = node.memoizedProps || node.pendingProps;
                if (props && props.payload) {
                    rows.push({
                        series: props.name || props.payload.name || null,
                        dataKey: typeof props.dataKey === 'string' ? props.dataKey : null,
                        payload: props.payload,
                    });
                    break;
                }
                node = node.return || node.alternate || null;
            }
        }
    }
    return rows;
}
"""


@dataclass(frozen=True)
class FetchResult:
    account_id: str
    readings: tuple[UsageReading, ...]
    source: Literal["structured", "chart", "dom"]


BrowserOpener = Callable[[Settings], object]


async def _first_visible(page, selectors: tuple[str, ...]) -> str:
    """Return the first selector that matches an element on the page."""

    for selector in selectors:
        if await page.locator(selector).count() > 0:
            return selector
    raise AuthenticationError("Login form fields could not be found.")


class WafTokenStore:
    """Persists portal cookies (notably ``aws-waf-token``) between runs.

    A solved AWS WAF captcha sets a token cookie valid for roughly three
    days; persisting it lets subsequent runs skip the manual challenge.
    Cookie values are NEVER logged.  The file is written atomically so a
    crash mid-write cannot corrupt the stored state.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> list[dict] | None:
        """Return stored cookies, or None if missing/corrupt/expired."""

        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, list):
            return None
        for cookie in raw:
            if (
                isinstance(cookie, dict)
                and cookie.get("name") == "aws-waf-token"
                and float(cookie.get("expires", -1)) <= time.time()
            ):
                # Token expired; stale state is useless to the portal.
                return None
        return raw

    def save(self, cookies: list[dict]) -> None:
        """Persist only Ergon/WAF-domain cookies atomically."""

        kept = [
            cookie
            for cookie in cookies
            if any(
                marker in str(cookie.get("domain", ""))
                for marker in _WAF_COOKIE_DOMAIN_MARKERS
            )
        ]
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(kept), encoding="utf-8"
        )  # Values never logged anywhere.
        tmp.replace(self._path)


class ErgonClient:
    """Automates the Ergon portal with one authenticated browser per run."""

    def __init__(
        self,
        settings: Settings,
        browser_factory: BrowserOpener | None = None,
        *,
        headful: bool = False,
        waf_store: WafTokenStore | None = None,
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
        self._waf_store = waf_store

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
        """Fetch the tariff-metering page and parse its rate sections."""

        observed_at = datetime.now(timezone.utc)
        async with self._run() as portal:
            url = TARIFF_URL_TEMPLATE.format(account=portal.account_id)
            page = await portal.context.new_page()
            try:
                await page.goto(url)
                # The live portal renders tariffs as collapsed accordions:
                # expand every heading naming a tariff before reading the DOM,
                # mirroring scripts/diagnose_rates_page.py.  Clicks are
                # best-effort; a failure leaves the section collapsed.
                try:
                    headings = await page.locator("h1, h2, h3").all()
                except Exception:
                    headings = []
                for heading in headings:
                    try:
                        text = ((await heading.text_content()) or "").strip()
                    except Exception:
                        continue
                    if "tariff" not in text.lower():
                        continue
                    try:
                        await heading.click(timeout=3_000)
                        await page.wait_for_timeout(1_000)
                    except Exception:
                        logger.debug("Tariff accordion click failed: %s", text[:40])
                html = await page.content()
            finally:
                await page.close()
        rates = extract_tariff_rates(html, portal.account_id, observed_at)
        return tuple(rates)

    # -- internals --------------------------------------------------------

    def _usage_url(self, account_id: str, day: date | None) -> str:
        # Live-confirmed format: custom period with DD/MM/YYYY dates.  The
        # rolling fetch uses a three-day window ending today.
        if day is None:
            today = datetime.now(ZoneInfo("Australia/Brisbane")).date()
            start = today - timedelta(days=2)
            return USAGE_URL_TEMPLATE.format(account=account_id) + (
                f"?periodDays=custom&startDate={start.strftime('%d/%m/%Y')}"
                f"&endDate={today.strftime('%d/%m/%Y')}"
            )
        return USAGE_URL_TEMPLATE.format(account=account_id) + (
            f"?periodDays=custom&startDate={day.strftime('%d/%m/%Y')}"
            f"&endDate={day.strftime('%d/%m/%Y')}"
        )

    async def _fetch_usage(self, day: date | None) -> FetchResult:
        candidates: list[CapturedJson] = []

        async def _capture(response) -> None:
            await _capture_response_into(response, candidates)

        async with self._run() as portal:
            # Reuse the login tab (WAF trust is page-scoped in practice); a
            # second tab was observed to render without data.
            page = portal.page
            try:
                page.on("response", _capture)
                await page.goto(self._usage_url(portal.account_id, day))
                # The usage page is a SPA: the data arrives via XHR after
                # load.  Wait until at least one JSON response has been
                # captured (bounded), then give rendering a moment before
                # taking the DOM snapshot for the fallback path.
                deadline = asyncio.get_running_loop().time() + USAGE_XHR_WAIT_MS / 1000
                while not candidates:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    await asyncio.sleep(min(0.25, remaining))
                await page.wait_for_timeout(1_000)
                # The chart renders asynchronously after load; wait for bar
                # shapes before reading React props (bounded, best-effort —
                # a non-usage page simply times out).
                try:
                    await page.wait_for_selector(
                        ".recharts-bar-rectangle", timeout=15_000
                    )
                    await page.wait_for_timeout(1_000)
                except Exception:  # noqa: BLE001 - not a chart page
                    pass
                html = await page.content()
                # The live portal renders usage as a Recharts chart; hourly
                # rows live in the React props of each bar shape.  Read them
                # while the page is open (returns [] on any failure).  Also
                # capture the shape count so a failed read is self-describing.
                chart_rows = await _read_chart_payloads(page)
                if chart_rows:
                    logger.info("Chart payload read: %d rows.", len(chart_rows))
                try:
                    chart_shape_count = await page.evaluate(
                        "document.querySelectorAll('.recharts-bar-rectangle path,"
                        " .recharts-bar-rectangle rect').length"
                    )
                except Exception:  # noqa: BLE001
                    chart_shape_count = -1
                if not chart_rows and chart_shape_count:
                    # Self-describing failure: dump the FIRST shape's React
                    # key names and its prop keys (names only — no values).
                    debug = await page.evaluate(
                        """() => {
                            const shape = document.querySelector(
                                '.recharts-bar-rectangle path, .recharts-bar-rectangle rect');
                            if (!shape) return {noShape: true};
                            const out = {keys: Object.keys(shape).map(k => k.slice(0, 24))};
                            for (const k of Object.keys(shape)) {
                                if (!k.startsWith('__react')) continue;
                                let node = shape[k];
                                const hops = [];
                                for (let hop = 0; hop < 8 && node; hop++) {
                                    const props = node.memoizedProps || node.pendingProps;
                                    hops.push(props ? Object.keys(props).slice(0, 12) : null);
                                    node = node.return || node.alternate || null;
                                }
                                out[k.slice(0, 20)] = hops;
                            }
                            return out;
                        }"""
                    )
                    logger.info("Chart debug: %s", json.dumps(debug)[:800])
            finally:
                await page.close()
        return _resolve_readings(
            portal.account_id, candidates, html, chart_rows, chart_shape_count, day
        )

    def _run(self):
        """Context manager performing login and account discovery."""

        return _AuthenticatedRun(
            self._settings,
            self._browser_factory,
            headful=self._headful,
            waf_store=self._waf_store,
        )



class _Portal:
    """Result of a successful login plus account discovery.

    ``page`` is the SAME tab the login happened in.  The live portal is a
    WAF-protected SPA; opening a second tab for the usage fetch gets a fresh
    (often blocked) render, so callers must reuse this page.
    """

    def __init__(self, context, account_id: str, page) -> None:
        self.context = context
        self.account_id = account_id
        self.page = page


class _AuthenticatedRun:
    """Async context manager: one browser, one context, login, discovery."""

    def __init__(
        self,
        settings: Settings,
        browser_factory: BrowserOpener,
        *,
        headful: bool = False,
        waf_store: WafTokenStore | None = None,
    ) -> None:
        self._settings = settings
        self._browser_factory = browser_factory
        self._headful = headful
        self._waf_store = waf_store
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
                user_agent=REALISTIC_USER_AGENT,
                locale="en-AU",
                timezone_id="Australia/Brisbane",
                viewport={"width": 1280, "height": 800},
                screen={"width": 1280, "height": 800},
            )
            # Stealth is applied identically for headless and headful runs.
            await self._context.add_init_script(_STEALTH_JS)
            if self._waf_store is not None:
                cookies = self._waf_store.load()
                if cookies:
                    try:
                        await self._context.add_cookies(cookies)
                    except Exception:  # noqa: BLE001 - stale format must not crash
                        logger.warning("Could not restore WAF cookies; continuing fresh.")
            page = await self._context.new_page()
            try:
                await self._login(page)
                account_id = await self._discover_account(page)
            except Exception:
                await page.close()
                raise
            # The login page stays OPEN; the usage fetch navigates this same
            # tab (matching the proven manual-probe flow).
            return _Portal(self._context, account_id, page)
        except Exception:
            await self._cleanup()
            raise

    async def __aexit__(self, *_exc) -> None:
        # Save cookies on reaching __aexit__ of a run: login + account
        # discovery succeeded inside __aenter__, so the WAF demonstrably let
        # us through, and the fresh token is worth persisting.  A run that
        # failed __aenter__ never reaches here (its __aexit__ is invoked on
        # the un-entered object is avoided because __aenter__ raised before
        # returning).  Best-effort; cleanup always proceeds.
        if self._waf_store is not None and self._context is not None:
            try:
                cookies = await self._context.cookies()
                self._waf_store.save(cookies)
            except Exception:  # noqa: BLE001 - persistence must not mask errors
                logger.warning("Could not persist WAF cookies.")
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
        # /auth/signin?callbackUrl=/portal).  With a restored session (WAF
        # store carries the auth session token too), the dashboard renders
        # immediately with no login form.
        await page.goto(PORTAL_BASE, wait_until="domcontentloaded")
        # Wait for EITHER state: already logged in (account link) or the
        # sign-in form.  Challenge pages show neither (only hidden WAF
        # inputs), so a generic "input" wait would mislead.
        try:
            await page.wait_for_selector(
                ', '.join((*EMAIL_SELECTORS, 'a[href*="/portal/A-"]')),
                timeout=LOGIN_WAIT_MS if not self._headful else CHALLENGE_WAIT_MS,
            )
        except Exception:  # noqa: BLE001 - challenge or blocked page
            raise AuthenticationError() from None
        if await page.locator('a[href*="/portal/A-"]').count() > 0:
            # Session restored; no login needed.
            return
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


async def _capture_response_into(response, candidates: list[CapturedJson]) -> None:
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


async def _read_chart_payloads(page) -> list[dict]:
    """Best-effort read of wrapped Recharts bar-shape rows via React fibers.

    Uses the exact algorithm proven by the manual probe: walk each bar
    shape's React fiber/props for a ``payload`` prop, keeping the shape's
    ``series`` display name alongside it.  Each returned item is
    ``{"series": str | None, "payload": {hour row}}``; the series name is
    what ``extract_chart_payloads`` uses to map RTC codes to display tariff
    names.  Retries briefly; returns [] only when no rows appear within the
    bounded window.
    """

    attempts = 10
    for attempt in range(attempts):
        try:
            wrapped = await page.evaluate(_RECHARTS_PAYLOAD_JS)
        except Exception:  # noqa: BLE001 - portal markup may differ
            return []
        if isinstance(wrapped, list) and wrapped:
            rows: list[dict] = []
            for item in wrapped:
                if not isinstance(item, dict):
                    continue
                payload = item.get("payload")
                if isinstance(payload, dict):
                    series = item.get("series")
                    data_key = item.get("dataKey")
                    rows.append(
                        {
                            "series": series if isinstance(series, str) else None,
                            "dataKey": data_key
                            if isinstance(data_key, str)
                            else None,
                            "payload": payload,
                        }
                    )
            if rows:
                return rows
        if attempt < attempts - 1:
            await page.wait_for_timeout(1_000)
    return []


def _resolve_readings(
    account_id: str,
    candidates: list[CapturedJson],
    html: str,
    chart_rows: list[dict],
    chart_shape_count: int,
    day: date | None,
) -> FetchResult:
    """Prefer structured payload, then Recharts rows, then DOM extraction."""

    try:
        readings = select_usage_payload(candidates, account_id, day)
        source: Literal["structured", "chart", "dom"] = "structured"
    except ExtractionError:
        try:
            readings = extract_chart_payloads(chart_rows, account_id, day)
            source = "chart"
        except ExtractionError as error:
            if chart_rows:
                # The chart was read (rows exist) but its data did not match
                # the request.  DOM cannot do better on a chart page; raise
                # the chart error so its span diagnostic is visible.
                raise ExtractionError(
                    f"{error.safe_message} (chart shapes on page: {chart_shape_count})"
                ) from None
            try:
                readings = extract_dom(html, account_id, day)
                source = "dom"
            except ExtractionError as dom_error:
                raise ExtractionError(
                    f"{dom_error.safe_message} (chart shapes on page: {chart_shape_count})"
                ) from None
    return FetchResult(account_id=account_id, readings=tuple(readings), source=source)


def _default_browser_factory(settings: Settings, headful: bool = False):
    """Launch Chromium; playwright is imported lazily here."""

    from playwright.async_api import async_playwright  # noqa: PLC0415

    class _Opener:
        async def __aenter__(self):
            self._pw = await async_playwright().start()
            return await self._pw.chromium.launch(
                headless=not headful, args=list(_LAUNCH_ARGS)
            )

        async def __aexit__(self, *_exc) -> None:
            await self._pw.stop()

    return _Opener()
