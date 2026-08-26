#!/usr/bin/env python3
"""Monitor the pasport.org.ua e-queue page and alert when booking opens up.

Opens the e-queue page in a real (headful) browser with a persistent profile,
then every N seconds reloads it and checks the page text. While the page shows
"Наразі всі місця зайняті" it stays quiet. As soon as that busy notice
disappears (i.e. it becomes possible to pick a time), it prints and sends a
Telegram alert so you can go and book manually.

Extras:
  * startup ping to Telegram (confirms the bot is alive and token/chat work);
  * heartbeat every N hours ("still running, current status ...");
  * runtime errors are caught and pushed to Telegram (throttled), the loop keeps
    going instead of dying silently.

Read-only: it books nothing.
"""
import argparse
import asyncio
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright

# Force UTF-8 on stdout/stderr so non-ASCII (Ukrainian/Czech) text and emoji do
# not crash print() on Windows consoles that default to cp1251.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:
    pass

DEFAULT_URL = "https://prague.pasport.org.ua/solutions/e-queue"
PROFILE_DIR = ".equeue_profile"

# If any of these appear, all slots are taken (no booking possible right now).
BUSY_MARKERS = [
    "всі місця зайняті",
    "спробуйте в інший час",
    "кількість талонів обмежена",
]
# Must be present for us to trust that the e-queue page really loaded (not a
# Cloudflare check, waiting-room queue, or a blank/error page).
PAGE_MARKER = "електронна черга"

ERROR_THROTTLE_SEC = 30 * 60  # send at most one error alert per 30 min


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (no external dependency)."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = "https://api.telegram.org/bot{}/sendMessage".format(token)
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(url, data=payload)
    with urllib.request.urlopen(req, timeout=15) as resp:  # nosec - user's own bot
        resp.read()


class Monitor:
    def __init__(self, url: str, poll: int, heartbeat_hours: float) -> None:
        self.url = url
        self.poll = poll
        self.heartbeat_sec = heartbeat_hours * 3600.0
        self.token = os.getenv("BOT_TOKEN", "").strip()
        self.chat_id = os.getenv("CHAT_ID", "").strip()
        self.was_available = False
        self.last_status = "?"
        self.last_heartbeat = 0.0
        self.last_error_sent = 0.0

    @property
    def tg_on(self) -> bool:
        return bool(self.token and self.chat_id)

    def notify(self, text: str) -> None:
        """Push to Telegram if configured; never raises."""
        if not self.tg_on:
            return
        try:
            send_telegram(self.token, self.chat_id, text)
        except Exception as e:
            print("[{}] telegram send failed: {}".format(ts(), e))

    def maybe_error(self, err: object) -> None:
        now = time.monotonic()
        if now - self.last_error_sent < ERROR_THROTTLE_SEC:
            return
        self.last_error_sent = now
        self.notify("\u26A0\uFE0F e-queue монітор: помилка\n{}\nЧас: {}".format(err, ts()))

    def maybe_heartbeat(self) -> None:
        if self.heartbeat_sec <= 0:
            return
        now = time.monotonic()
        if now - self.last_heartbeat < self.heartbeat_sec:
            return
        self.last_heartbeat = now
        self.notify("\U0001F7E2 Бот працює, перевіряю кожні {}s.\nПоточний стан: {}\nЧас: {}".format(
            self.poll, self.last_status, ts()))

    async def check(self, page) -> Optional[bool]:
        """Return True if booking is possible, False if busy, None if the page
        is not properly loaded yet (Cloudflare / waiting room / loading)."""
        await page.goto(self.url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(2)
        text = (await page.inner_text("body")).lower()
        if PAGE_MARKER not in text:
            return None
        busy = any(m in text for m in BUSY_MARKERS)
        return not busy

    def alert(self) -> None:
        city = self.url.split("//")[-1].split(".")[0]
        text = "\n".join([
            "\U0001F7E2 Зʼявилась можливість запису! ({})".format(city),
            "Блок «всі місця зайняті» зник — заходь і обирай час:",
            self.url,
        ])
        print("\n[{}] *** ЗАПИС ВІДКРИВСЯ *** {}".format(ts(), self.url))
        if self.tg_on:
            self.notify(text)
            print("[{}] telegram alert sent".format(ts()))
        else:
            print("[{}] (Telegram OFF: set BOT_TOKEN/CHAT_ID in .env)".format(ts()))

    async def run(self) -> None:
        async with async_playwright() as pw:
            try:
                ctx = await pw.chromium.launch_persistent_context(
                    user_data_dir=PROFILE_DIR,
                    headless=False,
                    viewport={"width": 1280, "height": 900},
                    locale="uk-UA",
                    args=["--disable-blink-features=AutomationControlled"],
                    ignore_default_args=["--enable-automation"],
                )
            except Exception as e:
                print("[{}] FATAL: cannot launch browser: {}".format(ts(), e))
                self.notify("\u26A0\uFE0F e-queue монітор НЕ запустився (браузер): {}".format(e))
                raise
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()

            print("[{}] Monitoring {}".format(ts(), self.url))
            print("Reloads every {}s. If a Cloudflare/queue page shows once, pass it manually.".format(self.poll))
            print("Telegram: {}".format("ON" if self.tg_on else "OFF (set BOT_TOKEN/CHAT_ID in .env)"))
            print("Heartbeat: {}".format(
                "every {}h".format(self.heartbeat_sec / 3600.0) if self.heartbeat_sec > 0 else "off"))
            print("Ctrl+C to stop.\n")

            self.last_heartbeat = time.monotonic()
            self.notify("\U0001F916 e-queue монітор запущено.\nПеревірка кожні {}s: {}".format(self.poll, self.url))

            try:
                while True:
                    try:
                        available = await self.check(page)
                    except Exception as e:
                        print("[{}] check error: {}".format(ts(), e))
                        self.maybe_error(e)
                        available = None

                    if available is None:
                        self.last_status = "не завантажилось (Cloudflare/черга?)"
                        print("[{}] page not ready (Cloudflare/queue/loading?) — waiting".format(ts()))
                        self.was_available = False
                    elif available:
                        self.last_status = "ВІЛЬНО"
                        print("[{}] ВІЛЬНО — запис можливий!".format(ts()))
                        if not self.was_available:
                            self.was_available = True
                            self.alert()
                    else:
                        self.last_status = "зайнято"
                        print("[{}] зайнято (всі місця зайняті)".format(ts()))
                        self.was_available = False

                    self.maybe_heartbeat()
                    await asyncio.sleep(self.poll)
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
            finally:
                await ctx.close()


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="e-queue availability monitor + Telegram alert (read-only)")
    parser.add_argument("--url", default=DEFAULT_URL, help="e-queue page URL")
    parser.add_argument("--poll", type=int, default=60, help="seconds between checks (default 60)")
    parser.add_argument("--heartbeat-hours", type=float, default=4.0,
                        help="hours between 'still alive' pings (0 = off, default 4)")
    args = parser.parse_args()
    asyncio.run(Monitor(args.url, args.poll, args.heartbeat_hours).run())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped")
