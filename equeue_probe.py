#!/usr/bin/env python3
"""
equeue_probe.py - Recon PoC for the Ukrainian passport e-queue (pasport.org.ua).

READ-ONLY. Nothing is booked. This just helps us understand the API.

It opens the e-queue page in a REAL headful Chromium (persistent profile) so YOU
can pass the Cloudflare Turnstile / waiting room manually. Then it:
  - logs every request/response to /api/v1/PreReg/GetDays (method, URL, post body, JSON)
  - dumps the #service dropdown options (service id -> label) to learn service IDs
    (e.g. adult biometric passport vs child under 14)
  - logs other /api/ traffic for context

Usage:
  python -m playwright install chromium      # once, if the browser is missing
  python equeue_probe.py                      # Prague by default
  python equeue_probe.py --url https://warszawa.pasport.org.ua/solutions/e-queue

Steps:
  1. Run it. A browser window opens.
  2. Solve the Cloudflare check / wait out the queue if shown.
  3. Pick a service in the dropdown yourself -> watch this console for GetDays.
  4. Ctrl+C to stop.
"""
import argparse
import asyncio
import json
from datetime import datetime

from playwright.async_api import async_playwright

DEFAULT_URL = "https://prague.pasport.org.ua/solutions/e-queue"
PROFILE_DIR = ".equeue_profile"
TARGET = "/api/v1/PreReg/GetDays"
STEALTH_JS = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


async def dump_services(page) -> bool:
    """Print #service options. Returns True if options were found."""
    try:
        options = await page.eval_on_selector_all(
            "#service option",
            "els => els.map(e => ({value: e.value, label: (e.textContent || '').trim()}))",
        )
    except Exception as e:
        print("[{}] could not read #service: {}".format(ts(), e))
        return False

    options = [o for o in (options or []) if o.get("value")]
    if not options:
        return False

    print("\n[{}] #service options (service id -> label):".format(ts()))
    for o in options:
        print("    value={!r}  label={!r}".format(o["value"], o["label"]))
    print("")
    return True


async def read_get_days(resp) -> None:
    if TARGET not in resp.url:
        return
    print("\n[{}] <<< RESPONSE {} {}".format(ts(), resp.status, resp.url))
    try:
        body = await resp.text()
    except Exception as e:
        print("        (could not read body: {})".format(e))
        return
    try:
        data = json.loads(body)
        pretty = json.dumps(data, ensure_ascii=False, indent=2)
        print(pretty[:4000] + ("\n... (truncated)" if len(pretty) > 4000 else ""))
    except Exception:
        print(body[:2000])


def log_request(req) -> None:
    if TARGET in req.url:
        print("\n[{}] >>> REQUEST {} {}".format(ts(), req.method, req.url))
        post = req.post_data
        if post:
            print("        post_data: {}".format(post))
    elif "/api/" in req.url:
        print("[{}] (api) {} {}".format(ts(), req.method, req.url))


async def main() -> None:
    parser = argparse.ArgumentParser(description="e-queue API recon (read-only)")
    parser.add_argument("--url", default=DEFAULT_URL, help="e-queue page URL")
    args = parser.parse_args()

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            viewport={"width": 1280, "height": 900},
            locale="uk-UA",
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        await ctx.add_init_script(STEALTH_JS)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        page.on("request", log_request)
        page.on("response", lambda r: asyncio.create_task(read_get_days(r)))

        print("[{}] Opening {}".format(ts(), args.url))
        print("Pass the Cloudflare check if shown, then select a service in the dropdown.")
        print("Watch here for GetDays traffic. Ctrl+C to quit.\n")
        try:
            await page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print("[{}] goto warning: {}".format(ts(), e))

        dumped = False
        try:
            while True:
                await asyncio.sleep(5)
                if not dumped and await page.query_selector("#service"):
                    dumped = await dump_services(page)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await ctx.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped")
