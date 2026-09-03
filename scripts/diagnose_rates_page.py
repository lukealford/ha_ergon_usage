"""Dump the tariff-metering page structure to verify rate extraction.

Logs in (headful, uses the shared WAF/session store), navigates to the
tariff-metering page, and prints:
- the page's text around any "$" monetary labels
- heading structure
- whether extract_tariff_rates succeeds

No credentials are printed. Monetary values and tariff names are the
public rate card, safe to show.
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ergon import (  # noqa: E402
    REALISTIC_USER_AGENT,
    TARIFF_URL_TEMPLATE,
    WafTokenStore,
)
from app.tariff_rates import extract_tariff_rates  # noqa: E402


async def main() -> None:
    from playwright.async_api import async_playwright

    email = os.environ.get("ERGON_EMAIL", "")
    password = os.environ.get("ERGON_PASSWORD", "")
    if not email or not password:
        raise SystemExit("Set ERGON_EMAIL / ERGON_PASSWORD first.")

    store = WafTokenStore(Path("waf_state.json"))

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False, slow_mo=300,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=REALISTIC_USER_AGENT,
            locale="en-AU",
            timezone_id="Australia/Brisbane",
            viewport={"width": 1280, "height": 800},
        )
        cookies = store.load()
        if cookies:
            print(f"Restoring {len(cookies)} cookies from WAF store...")
            await context.add_cookies(cookies)
        page = await context.new_page()

        await page.goto(
            "https://myaccount.ergonretail.com.au/portal",
            wait_until="domcontentloaded",
        )
        print("Waiting for login (solve captcha if challenged)...")
        try:
            await page.wait_for_selector('a[href*="/portal/A-"]', timeout=300_000)
        except Exception:
            print("Login not detected; continuing anyway.")
        links = await page.eval_on_selector_all(
            'a[href*="/portal/A-"]', "els => els.map(e => e.href)"
        )
        account_id = links[0].split("/portal/")[1].split("/")[0] if links else ""
        print("Account:", account_id[:3] + "..." + account_id[-2:] if account_id else "NONE")

        target = TARIFF_URL_TEMPLATE.format(account=account_id)
        print("Navigating to:", target)
        await page.goto(target, wait_until="domcontentloaded")
        await page.wait_for_timeout(USAGE_WAIT := 10_000)

        # The tariffs are accordions: expand each heading before reading.
        print("Expanding accordions...")
        for heading in await page.locator("h1, h2, h3").all():
            text = (await heading.text_content() or "").strip()
            if "tariff" in text.lower():
                try:
                    await heading.click(timeout=3_000)
                    await page.wait_for_timeout(1_000)
                    print("  expanded:", text[:40])
                except Exception as error:
                    print(f"  click failed on {text[:40]!r}: {type(error).__name__}")
        await page.wait_for_timeout(2_000)

        html = await page.content()
        print("HTML length:", len(html))

        # Extract monetary-context snippets
        import re
        for match in list(re.finditer(r"\$\s*[\d.]+", html))[:30]:
            start = max(0, match.start() - 150)
            end = min(len(html), match.end() + 150)
            snippet = re.sub(r"<[^>]+>", " ", html[start:end])
            snippet = re.sub(r"\s+", " ", snippet).strip()
            if "kwh" in snippet.lower() or "per day" in snippet.lower() or "tariff" in snippet.lower():
                print("  $", snippet[:200])

        # Headings
        headings = await page.evaluate(
            """() => [...document.querySelectorAll('h1,h2,h3,h4')]
                .map(h => h.tagName + ': ' + (h.textContent||'').trim().slice(0,60))"""
        )
        print("\nHEADINGS:")
        for h in headings[:25]:
            print(" ", h)

        # Try the real extractor
        print("\nEXTRACTOR RESULT:")
        from datetime import datetime, timezone
        try:
            rates = extract_tariff_rates(html, account_id, datetime.now(timezone.utc))
            for rate in rates:
                print(f"  {rate.tariff}: {rate.per_kwh_aud}/kWh, supply={rate.daily_supply_aud}")
        except Exception as error:
            print(f"  FAILED: {type(error).__name__}: {error}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
