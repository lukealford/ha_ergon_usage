"""Discover the real usage page URL and its data requests.

Logs in manually (solve captcha + login in the visible browser), then
explores the portal nav to find the usage page. Dumps the final URL and
lists all JSON request URLs (paths only — no bodies, no tokens).
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


async def main() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=300)
        context = await browser.new_context(user_agent=REALISTIC_USER_AGENT)
        page = await context.new_page()

        json_requests: list[str] = []

        async def on_response(response):
            try:
                if "json" in response.headers.get("content-type", "").lower():
                    path = response.url.split("?")[0]
                    if path not in json_requests:
                        json_requests.append(path)
            except Exception:
                pass

        page.on("response", on_response)

        await page.goto(PORTAL_BASE, wait_until="domcontentloaded")
        print("Step 1: solve captcha + log in. Waiting for dashboard...")
        try:
            await page.wait_for_selector(
                'a[href*="/portal/A-"]', timeout=CHALLENGE_WAIT_MS
            )
            print("Logged in (account link visible).")
        except Exception:
            print("Timed out; continuing anyway. URL:", page.url)

        print("\nNAV LINKS:")
        links = await page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => e.href).filter(h => h.includes('/portal'))",
        )
        for href in dict.fromkeys(links):
            print(" ", href[:120])

        print("\nStep 2: opening each portal nav link (2s each).")
        for href in dict.fromkeys(links):
            if href.rstrip("/").endswith(PORTAL_BASE.rstrip("/")):
                continue
            try:
                await page.goto(href, wait_until="domcontentloaded")
                await page.wait_for_timeout(2_000)
                print(f"  {page.url[:110]}  title={await page.title()}")
            except Exception as error:
                print(f"  {href[:110]}  ERROR {type(error).__name__}")

        print("\nJSON REQUESTS OBSERVED:")
        for path in json_requests:
            print(" ", path[:140])

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
