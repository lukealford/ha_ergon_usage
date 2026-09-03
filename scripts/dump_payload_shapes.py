"""Dump the *shape* of JSON responses on the usage page.

Logs in manually (solve captcha + login), navigates to the usage page,
and prints each captured JSON response's structure: key paths and value
types only. No account data, no numbers, no timestamps.
"""

import asyncio
import json
import os
import sys

from playwright.async_api import async_playwright

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ergon import (  # noqa: E402
    USAGE_XHR_WAIT_MS,
    ErgonClient,
    REALISTIC_USER_AGENT,
)
from app.config import Settings  # noqa: E402
from scripts.capture_fixture import _settings_from_environment  # noqa: E402


def shape(value, depth: int = 0, max_depth: int = 6):
    if depth >= max_depth:
        return "..."
    if isinstance(value, dict):
        return {key: shape(item, depth + 1, max_depth) for key, item in value.items()}
    if isinstance(value, list):
        if not value:
            return "[]"
        return [shape(value[0], depth + 1, max_depth), f"...({len(value)} items)"]
    if isinstance(value, str):
        return f"str(len={len(value)})"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return type(value).__name__
    if value is None:
        return "null"
    return type(value).__name__


async def main() -> None:
    email = os.environ.get("ERGON_EMAIL", "")
    password = os.environ.get("ERGON_PASSWORD", "")
    if not email or not password:
        raise SystemExit("ERGON_EMAIL and ERGON_PASSWORD required.")

    from datetime import date
    from playwright.async_api import async_playwright

    payloads: list[dict] = []
    all_requests: list[str] = []

    async def on_response(response):
        try:
            url_path = response.url.split("?")[0]
            content_type = response.headers.get("content-type", "").split(";")[0]
            if url_path not in all_requests:
                all_requests.append(f"{content_type or '?':30} {url_path}")
            if "json" not in content_type.lower():
                return
            body = await response.json()
        except Exception:
            return
        payloads.append({"url": response.url.split("?")[0], "body": body})

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=300)
        context = await browser.new_context(user_agent=REALISTIC_USER_AGENT)
        page = await context.new_page()
        page.on("response", on_response)

        await page.goto(
            "https://myaccount.ergonretail.com.au/portal",
            wait_until="domcontentloaded",
        )
        print("Solve captcha + login. Waiting for account link...")
        try:
            await page.wait_for_selector(
                'a[href*="/portal/A-"]', timeout=300_000
            )
        except Exception:
            print("Login not detected in time; continuing.")
        links = await page.eval_on_selector_all(
            'a[href*="/portal/A-"]', "els => els.map(e => e.href)"
        )
        account_id = links[0].split("/portal/")[1].split("/")[0] if links else ""
        print("Account discovered:", bool(account_id))

        target = ErgonClient(
            _settings_from_environment(os.environ)
        )._usage_url(account_id, date(2026, 9, 1))
        print("Navigating to:", target.split("?")[0], "(params redacted)")
        await page.goto(target, wait_until="domcontentloaded")
        await page.wait_for_timeout(USAGE_XHR_WAIT_MS)

        # The usage data arrives in the SPA's SSR/initial HTML, not via a
        # separate XHR. Dump the DOM chart structure instead: look for
        # chart containers, canvas/svg elements, and embedded data blobs.
        print(f"\n=== {len(payloads)} JSON responses captured (likely none relevant) ===")
        for index, item in enumerate(payloads):
            print(f"\n--- response {index}: {item['url'][-80:]} ---")
            print(json.dumps(shape(item["body"]), indent=1)[:2000])

        print("\n=== DOM probes ===")
        probes = await page.evaluate(
            """() => {
                const out = {};
                out.canvases = document.querySelectorAll('canvas').length;
                out.svgs = document.querySelectorAll('svg').length;
                out.chartDivs = [...document.querySelectorAll('[class*=chart], [id*=chart]')]
                    .slice(0, 5).map(e => ({tag: e.tagName, cls: e.className.slice(0,60)}));
                // Common SPA state carriers
                out.nextData = !!document.getElementById('__NEXT_DATA__');
                out.nuxtData = !!window.__NUXT__;
                out.reduxState = !!document.querySelector('[data-reactroot]');

                // Script tags with large JSON payloads
                out.jsonScripts = [...document.querySelectorAll('script[type="application/json"], script[type="text/javascript"]:not([src])')]
                    .map(s => ({id: s.id, len: (s.textContent||'').length,
                                preview: (s.textContent||'').slice(0, 80).replace(/\\s+/g,' ')}))
                    .filter(s => s.len > 500)
                    .slice(0, 10);
                // window-level state objects
                out.globals = Object.keys(window).filter(k =>
                    /data|state|store|config|INITIAL|PROPS|NUXT|NEXT/i.test(k)).slice(0, 20);
                return out;
            }"""
        )
        import json as _json
        print(_json.dumps(probes, indent=1)[:3000])

        # If __NEXT_DATA__ exists, dump its shape
        next_data = await page.evaluate(
            """() => {
                const el = document.getElementById('__NEXT_DATA__');
                if (!el) return null;
                try { return JSON.parse(el.textContent); } catch { return null; }
            }"""
        )
        if next_data:
            print("\n=== __NEXT_DATA__ shape ===")
            print(json.dumps(shape(next_data, max_depth=8), indent=1)[:4000])

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
