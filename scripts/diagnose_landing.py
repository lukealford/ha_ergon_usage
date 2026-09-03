"""Diagnose the post-login landing page: URL and href patterns.

Run headful: solve the captcha manually, then this prints the landing
URL shape and all hrefs (values truncated; safe — URLs only).
"""

import asyncio
import os
import sys

from playwright.async_api import async_playwright

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ergon import (  # noqa: E402
    CHALLENGE_WAIT_MS,
    PORTAL_BASE,
    REALISTIC_USER_AGENT,
)

EMAIL = os.environ.get("ERGON_EMAIL", "")
PASSWORD = os.environ.get("ERGON_PASSWORD", "")
EMAIL_SELECTORS = ('input[aria-label="Email Address"]', 'input[type="email"]')
PASSWORD_SELECTORS = ('input[aria-label="Password"]', 'input[type="password"]')
SUBMIT_SELECTORS = ('button[type="submit"]', 'input[type="submit"]')


async def first_visible(page, selectors):
    for selector in selectors:
        if await page.locator(selector).count() > 0:
            return selector
    raise SystemExit("Login form fields not found.")


async def main() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=500)
        context = await browser.new_context(user_agent=REALISTIC_USER_AGENT)
        page = await context.new_page()
        await page.goto(PORTAL_BASE, wait_until="domcontentloaded")
        # Generous 5-minute window: solve the captcha, then log in manually
        # at your own pace. The script continues once the portal dashboard
        # URL contains the account ID (or the wait expires).
        print("Browser open. Solve captcha + login if prompted.")
        print("Waiting up to 60 seconds for the post-login portal page...")
        try:
            await page.wait_for_url(
                f"{PORTAL_BASE}/**", timeout=60_000
            )
        except Exception:
            pass  # URL may already be /portal (SPA doesn't change it)
        await page.wait_for_timeout(2_000)
        print("\nLANDING URL:", page.url)
        await page.wait_for_timeout(5_000)
        print("URL AFTER SETTLE:", page.url)
        hrefs = await page.eval_on_selector_all(
            "a[href]", "elements => elements.map(e => e.href)"
        )
        print(f"\nHREF COUNT: {len(hrefs)}")
        portal_hrefs = [h for h in hrefs if "/portal" in h or "A-" in h]
        print("PORTAL-RELATED HREFS:")
        for href in portal_hrefs[:30]:
            print(" ", href[:120])
        if not portal_hrefs:
            print(" (none — first 30 of all hrefs follow)")
            for href in hrefs[:30]:
                print(" ", href[:120])
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
