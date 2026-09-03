"""Dump the Recharts SVG structure on the usage page.

Logs in manually (solve captcha + login in the visible window), navigates
to the usage page, and prints the structure of the Recharts chart: bar
groups, data attributes, tooltip content, and the __next_f SSR chunks'
shape. Values are truncated; no credentials are used or printed.
"""

import asyncio
import json
import os
import sys

from playwright.async_api import async_playwright

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ergon import USAGE_XHR_WAIT_MS, REALISTIC_USER_AGENT  # noqa: E402
from app.errors import AuthenticationError  # noqa: E402

EMAIL_SELECTORS = (
    'input[aria-label="Email Address"]',
    'input[type="email"]',
)
PASSWORD_SELECTORS = (
    'input[aria-label="Password"]',
    'input[type="password"]',
)
SUBMIT_SELECTORS = ("button[type=submit]", "input[type=submit]")


async def _first_visible(page, selectors):
    for selector in selectors:
        try:
            if await page.locator(selector).count() > 0:
                return selector
        except Exception:
            continue
    return None


async def auto_login(page, email: str, password: str) -> None:
    """Solve nothing automatically: wait out the captcha, then fill the
    login form if it appears and let the user intervene if anything fails.
    Waits up to 5 minutes for the post-login account link."""

    # Stage 1: wait for either the login form or the account link
    # (already-logged-in session).  Challenge pages have no inputs.
    for _ in range(60):  # 5 minutes in 5s polls
        if await page.locator('a[href*="/portal/A-"]').count() > 0:
            return
        if await _first_visible(page, EMAIL_SELECTORS) is not None:
            break
        await page.wait_for_timeout(5_000)
    else:
        raise SystemExit("Neither login form nor account link appeared.")

    # Stage 2: fill + submit if still on the login page
    if await page.locator('a[href*="/portal/A-"]').count() == 0:
        email_selector = await _first_visible(page, EMAIL_SELECTORS)
        password_selector = await _first_visible(page, PASSWORD_SELECTORS)
        try:
            await page.fill(email_selector, email, timeout=10_000)
            await page.fill(password_selector, password, timeout=10_000)
            submit = await _first_visible(page, SUBMIT_SELECTORS)
            await page.click(submit, timeout=10_000)
        except Exception as error:
            print(f"Auto-fill failed ({type(error).__name__}); "
                  "log in manually in the window.")

    # Stage 3: wait for the account link to appear (5 min)
    for _ in range(60):
        if await page.locator('a[href*="/portal/A-"]').count() > 0:
            return
        await page.wait_for_timeout(5_000)
    raise SystemExit("Login did not complete in time.")


async def main() -> None:
    from datetime import date

    email = os.environ.get("ERGON_EMAIL", "")
    password = os.environ.get("ERGON_PASSWORD", "")
    if not email or not password:
        raise SystemExit("Set ERGON_EMAIL / ERGON_PASSWORD (e.g. via Read-Host).")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=300)
        context = await browser.new_context(user_agent=REALISTIC_USER_AGENT)
        page = await context.new_page()

        await page.goto(
            "https://myaccount.ergonretail.com.au/portal",
            wait_until="domcontentloaded",
        )
        print("Waiting out challenge + login (auto-fill or manual)...")
        await auto_login(page, email, password)
        print("Logged in.")
        links = await page.eval_on_selector_all(
            'a[href*="/portal/A-"]', "els => els.map(e => e.href)"
        )
        account_id = links[0].split("/portal/")[1].split("/")[0]
        print("Account discovered:", account_id[:3] + "..." + account_id[-2:])

        day = date(2026, 9, 1)
        target = (
            "https://myaccount.ergonretail.com.au/portal/"
            f"{account_id}/tariff-metering/usage"
            f"?periodDays=custom&startDate={day.strftime('%d/%m/%Y')}"
            f"&endDate={day.strftime('%d/%m/%Y')}"
        )
        print("Navigating to usage page...")
        await page.goto(target, wait_until="domcontentloaded")
        await page.wait_for_selector(".recharts-bar-rectangle", timeout=30_000)
        await page.wait_for_timeout(2_000)

        # 1. Recharts bar structure: each series renders as <path> or <rect>
        #    inside .recharts-bar rectangles with tooltip payload data nearby.
        bars = await page.evaluate(
            """() => {
                const out = {};
                const chart = document.querySelector('.recharts-wrapper');
                if (!chart) return {noChart: true};

                // All recharts series layers
                out.layers = [...document.querySelectorAll('.recharts-bar, .recharts-area, .recharts-line')]
                    .map(l => ({
                        cls: l.getAttribute('class').slice(0, 80),
                        name: l.querySelector('.recharts-bar-rectangles') ? 'bar' : 'other',
                        shapes: [...l.querySelectorAll('path, rect')].length,
                    }));

                // Sample the first few shape elements fully
                const shapes = [...chart.querySelectorAll('.recharts-bar-rectangle path, .recharts-bar-rectangle rect')];
                out.shapeCount = shapes.length;
                out.sampleShapes = shapes.slice(0, 3).map(s => {
                    const attrs = {};
                    for (const a of s.attributes) attrs[a.name] = a.value.slice(0, 60);
                    return attrs;
                });

                // Tooltip content (hidden until hover, but may exist in DOM)
                const tooltip = document.querySelector('.recharts-tooltip-wrapper');
                out.tooltipHtml = tooltip ? tooltip.innerHTML.slice(0, 500) : null;

                // Legend labels (series names)
                out.legend = [...document.querySelectorAll('.recharts-legend-item-text')]
                    .map(e => e.textContent.trim().slice(0, 40));

                // X axis ticks (timestamps)
                out.xTicks = [...document.querySelectorAll('.recharts-cartesian-axis-tick-value')]
                    .slice(0, 5).map(e => e.textContent.trim());

                // Any data-* attributes on chart descendants
                const dataEls = [...chart.querySelectorAll('[data-tariff],[data-timestamp],[data-kwh],[data-series],[data-value],[data-point]')];
                out.dataAttrs = dataEls.slice(0, 5).map(e => {
                    const attrs = {};
                    for (const a of e.attributes) if (a.name.startsWith('data-')) attrs[a.name] = a.value.slice(0, 60);
                    return attrs;
                });
                return out;
            }"""
        )
        print("\n=== RECHARTS STRUCTURE ===")
        print(json.dumps(bars, indent=1)[:5000])

        # 2. Read the React props behind each bar shape. Recharts passes the
        #    underlying data row as the `payload` prop — real values, no pixel
        #    inference, no tooltip interaction needed.
        payload_dump = await page.evaluate(
            """() => {
                const shapes = [...document.querySelectorAll(
                    '.recharts-bar-rectangle path, .recharts-bar-rectangle rect')];
                if (!shapes.length) return {noShapes: true};
                const el = shapes[0];
                const keys = Object.keys(el).filter(k => k.startsWith('__react'));
                const out = {reactKeys: keys.map(k => k.slice(0, 20))};
                // Walk to find props with a payload
                for (const k of keys) {
                    let node = el[k];
                    for (let hop = 0; hop < 8 && node; hop++) {
                        const props = node.memoizedProps || node.pendingProps;
                        if (props && props.payload) {
                            out.foundVia = k.slice(0, 20) + ' hop ' + hop;
                            out.payloadSample = JSON.parse(JSON.stringify(
                                props.payload,
                                (key, value) => typeof value === 'string' && value.length > 60
                                    ? value.slice(0, 60) + '...' : value
                            ));
                            break;
                        }
                        node = node.return || node.alternate || null;
                    }
                    if (out.payloadSample) break;
                }
                if (!out.payloadSample) {
                    // Dump raw prop keys to guide the next probe
                    out.propKeySamples = keys.map(k => {
                        const p = el[k] && (el[k].memoizedProps || el[k].pendingProps);
                        return p ? Object.keys(p).slice(0, 20) : null;
                    });
                }
                return out;
            }"""
        )
        print("\n=== REACT PAYLOAD SAMPLE (first bar) ===")
        print(json.dumps(payload_dump, indent=1, default=str)[:4000])

        # 3. If props found, extract ALL bars' payloads.
        if payload_dump.get("payloadSample"):
            all_payloads = await page.evaluate(
                """() => {
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
                                        payload: props.payload,
                                    });
                                    break;
                                }
                                node = node.return || node.alternate || null;
                            }
                        }
                    }
                    return rows;
                }"""
            )
            print(f"\n=== ALL BAR PAYLOADS ({len(all_payloads)} shapes) ===")
            print(json.dumps(all_payloads[:6], indent=1, default=str)[:4000])
            with open("scripts/payload_dump.json", "w", encoding="utf-8") as fh:
                json.dump(all_payloads, fh, indent=1, default=str)
            print("\nFull dump written to scripts/payload_dump.json (LOCAL ONLY — do not commit)")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
