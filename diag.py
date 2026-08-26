#!/usr/bin/env python3
"""Minimal diagnostic: tries to launch the headful browser and navigate.
Prints exactly where it dies. Run:  .venv\\Scripts\\python.exe -u diag.py > diag.log 2>&1
"""
import asyncio
import sys
import traceback

print("[diag] python:", sys.version, flush=True)
print("[diag] platform:", sys.platform, flush=True)

try:
    from playwright.async_api import async_playwright
    print("[diag] playwright imported", flush=True)
except Exception:
    print("[diag] FAILED to import playwright:", flush=True)
    traceback.print_exc()
    sys.exit(1)

URL = "https://prague.pasport.org.ua/solutions/e-queue"
PROFILE_DIR = ".equeue_profile"


async def try_launch(headless: bool) -> bool:
    mode = "headless" if headless else "headful"
    print("[diag] launching {} chromium (profile {}) ...".format(mode, PROFILE_DIR), flush=True)
    try:
        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                PROFILE_DIR,
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"],
                ignore_default_args=["--enable-automation"],
            )
            print("[diag] {}: launched OK, pages={}".format(mode, len(ctx.pages)), flush=True)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            print("[diag] {}: navigating to {} ...".format(mode, URL), flush=True)
            try:
                await page.goto(URL, timeout=60000)
                print("[diag] {}: navigated, title={!r}".format(mode, await page.title()), flush=True)
            except Exception:
                print("[diag] {}: goto failed:".format(mode), flush=True)
                traceback.print_exc()
            if not headless:
                print("[diag] headful: keeping window open 20s so you can see it", flush=True)
                await asyncio.sleep(20)
            await ctx.close()
            print("[diag] {}: closed OK".format(mode), flush=True)
            return True
    except Exception:
        print("[diag] {}: LAUNCH EXCEPTION:".format(mode), flush=True)
        traceback.print_exc()
        return False


async def main() -> None:
    ok = await try_launch(headless=False)
    if not ok:
        print("[diag] headful failed -> trying headless to isolate the cause", flush=True)
        await try_launch(headless=True)


asyncio.run(main())
print("[diag] done", flush=True)
