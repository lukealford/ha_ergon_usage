"""One-off diagnostic: follow the human path from the Ergon home page.

Starts at the residential landing page, finds the login link, clicks
through, and dumps the resulting form structure (main frame + iframes).
Prints only element attributes — no credentials involved.
"""

import asyncio

from playwright.async_api import async_playwright

START_URL = "https://myaccount.ergonretail.com.au/portal"

DUMP_JS = """els => els.map(el => ({
    tag: el.tagName,
    type: el.getAttribute('type'),
    name: el.getAttribute('name'),
    id: el.getAttribute('id'),
    ariaLabel: el.getAttribute('aria-label'),
    placeholder: el.getAttribute('placeholder'),
    text: (el.textContent || '').trim().slice(0, 40),
}))"""

LINK_JS = """els => els
    .map(el => ({ text: (el.textContent || '').trim().slice(0, 60), href: el.href }))
    .filter(item => item.text.toLowerCase().includes('login') || item.href.toLowerCase().includes('login'))"""


async def dump_frame(frame, label: str) -> None:
    inputs = await frame.eval_on_selector_all("input", DUMP_JS)
    buttons = await frame.eval_on_selector_all(
        "button, input[type=submit], [role=button]", DUMP_JS
    )
    if inputs or buttons:
        print(f"\n-- {label}: INPUTS --")
        for item in inputs:
            print(item)
        print(f"-- {label}: BUTTONS --")
        for item in buttons:
            print(item)


async def main() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=500)
        # Override the UA so the (headless) browser does not advertise
        # "HeadlessChrome" — the portal's bot check rejects that outright.
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()
        await page.goto(START_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3_000)

        links = await page.eval_on_selector_all("a[href]", LINK_JS)
        print("-- LOGIN LINKS ON LANDING PAGE --")
        for item in links:
            print(item)

        if not links:
            print("No login links found; dumping frames anyway.")

        # Click the first login link IN PLACE (it is a '#' anchor that opens
        # a menu or modal rather than navigating).
        if links:
            print("\nClicking login link in place...")
            await page.click("text=Login", timeout=10_000)
            await page.wait_for_timeout(3_000)
            # Look for any link that appeared after the click.
            revealed = await page.eval_on_selector_all(
                "a[href]", LINK_JS
            )
            print("-- LINKS AFTER CLICK --")
            for item in revealed:
                print(item)
        await page.wait_for_timeout(8_000)

        print("\nFinal URL:", page.url)
        print("Frames:", len(page.frames))
        for index, frame in enumerate(page.frames):
            label = "(main)" if frame is page.main_frame else (frame.name or "(anonymous)")
            print(f"\n=== frame {index}: url={frame.url} name={label} ===")
            await dump_frame(frame, f"frame{index}")

        try:
            await page.wait_for_selector("input", timeout=10_000)
            print("\nAn input appeared somewhere within 10s.")
        except Exception as error:
            print(f"\nNo input appeared: {type(error).__name__}")

        print("\nHTML length:", len(await page.content()))
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
