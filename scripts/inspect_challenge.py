"""Inspect the Human Verification page structure (no credentials involved)."""

import asyncio

from playwright.async_api import async_playwright

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)


async def main() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(user_agent=UA)
        page = await context.new_page()
        await page.goto(
            "https://myaccount.ergonretail.com.au/portal",
            wait_until="domcontentloaded",
        )
        await page.wait_for_timeout(3_000)
        print("TITLE:", await page.title())
        elements = await page.eval_on_selector_all(
            "button, iframe, [id*=captcha], [class*=captcha], form",
            """els => els.slice(0, 25).map(e => ({
                tag: e.tagName,
                id: e.id,
                cls: (e.className || '').toString().slice(0, 60),
                src: e.src ? e.src.slice(0, 80) : undefined,
                txt: (e.textContent || '').trim().slice(0, 30),
            }))""",
        )
        for item in elements:
            print(item)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
