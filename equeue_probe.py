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
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

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

# After picking a service in the dropdown, the page shows a busy notice while no
# slots are free. We select the service, then look for these markers.
DEFAULT_SERVICE = "Закордонний паспорт та (або) ID-картка"

# If any of these appear, all slots are taken (no booking possible right now).
BUSY_MARKERS = [
    "вибачте, на даний момент всі місця зайняті",
    "всі місця зайняті",
    "спробуйте в інший час",
    "кількість талонів обмежена",
]
# Must be present for us to trust that the e-queue page really loaded (not a
# Cloudflare check, waiting-room queue, or a blank/error page).
PAGE_MARKER = "електронна черга"

# The site rate-limits aggressive polling with a "Too many requests" page.
RATE_LIMIT_MARKERS = [
    "too many requests",
    "забагато запитів",
]

# Positive availability signal: when a slot opens, the "Обрати день" (choose
# day) dropdown lists a bookable date in DD.MM.YYYY form. When all slots are
# taken that dropdown is empty and no such dotted date appears anywhere on the
# page (news uses word-months like "28 вересня 2026"). We alert only when this
# date is present, instead of merely when the busy notice is absent.
DAY_DATE_RE = re.compile(r"\b\d{2}\.\d{2}\.\d{4}\b")

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
    def __init__(self, url: str, poll: int, heartbeat_hours: float, cooldown: int,
                 service: str, jitter: int, confirm: int = 2, confirm_delay: int = 5,
                 dump_only: bool = False) -> None:
        self.url = url
        self.poll = poll
        self.cooldown = cooldown
        self.service = service
        self.jitter = max(0, jitter)
        self.confirm = max(0, confirm)
        self.confirm_delay = max(1, confirm_delay)
        self.dump_only = dump_only
        self.heartbeat_sec = heartbeat_hours * 3600.0
        self.token = os.getenv("BOT_TOKEN", "").strip()
        self.chat_id = os.getenv("CHAT_ID", "").strip()
        self.was_available = False
        self.was_ratelimited = False
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

    def poll_delay(self) -> float:
        """Base poll interval plus random jitter so requests are not identical."""
        if self.jitter <= 0:
            return float(self.poll)
        return self.poll + random.uniform(-self.jitter, self.jitter)

    async def select_service(self, page) -> bool:
        """Pick the wanted service in a native <select>. Return True on success."""
        target = self.service.lower()
        for sel in await page.query_selector_all("select"):
            for opt in await sel.query_selector_all("option"):
                label = (await opt.inner_text()).strip()
                if target in label.lower():
                    value = await opt.get_attribute("value")
                    if value is not None:
                        await sel.select_option(value=value)
                    else:
                        await sel.select_option(label=label)
                    return True
        return False

    async def check(self, page) -> str:
        """Return one of: 'available', 'busy', 'ratelimited', 'notready'."""
        await page.goto(self.url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(2)
        text = (await page.inner_text("body")).lower()
        if any(m in text for m in RATE_LIMIT_MARKERS):
            return "ratelimited"
        if PAGE_MARKER not in text:
            return "notready"
        if not await self.select_service(page):
            print("[{}] service '{}' not found in dropdown yet".format(ts(), self.service))
            return "notready"
        # Give the page time to load availability for the chosen service.
        await asyncio.sleep(2)
        text = (await page.inner_text("body")).lower()
        if any(m in text for m in RATE_LIMIT_MARKERS):
            return "ratelimited"
        if any(m in text for m in BUSY_MARKERS):
            return "busy"
        # Require a positive signal (a bookable DD.MM.YYYY date) before we treat
        # the page as open. Absence of the busy notice alone is not enough — a
        # page caught mid-load shows neither, and that used to fire false alerts.
        if DAY_DATE_RE.search(text):
            return "available"
        return "notready"

    async def confirm_available(self, page) -> bool:
        """Re-check a few times to filter out transient false 'available'
        readings (page caught mid-load before the busy block rendered).

        A real slot opening persists for minutes, so it survives repeated
        checks; a one-poll rendering blip does not. Returns True only if
        every extra check also reports 'available'."""
        for i in range(self.confirm):
            await asyncio.sleep(self.confirm_delay)
            try:
                status = await self.check(page)
            except Exception as e:
                print("[{}] confirm check error: {}".format(ts(), e))
                return False
            print("[{}] confirm {}/{}: {}".format(ts(), i + 1, self.confirm, status))
            if status != "available":
                return False
        return True

    def alert(self) -> None:
        city = self.url.split("//")[-1].split(".")[0]
        text = "\n".join([
            "\U0001F7E2 Зʼявилась можливість запису! ({})".format(city),
            "У «Обрати день» зʼявилась вільна дата — заходь і бронюй:",
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

            if self.dump_only:
                status = await self.check(page)
                full = await page.inner_text("body")
                low = full.lower()
                print("[{}] DUMP status={} url={}".format(ts(), status, page.url))
                print("page marker present: {}".format(PAGE_MARKER in low))
                print("busy markers matched: {}".format([m for m in BUSY_MARKERS if m in low]))
                print("rate-limit matched: {}".format([m for m in RATE_LIMIT_MARKERS if m in low]))
                for si, sel in enumerate(await page.query_selector_all("select")):
                    opts = []
                    for opt in await sel.query_selector_all("option"):
                        opts.append((await opt.inner_text()).strip())
                    print("SELECT[{}] options: {}".format(si, opts))
                Path("dump.txt").write_text(full, encoding="utf-8")
                print("saved full body text -> dump.txt ({} chars)".format(len(full)))
                await ctx.close()
                return

            print("[{}] Monitoring {}".format(ts(), self.url))
            print("Service: {}".format(self.service))
            print("Reloads every ~{}s (+/-{}s). If a Cloudflare/queue page shows once, pass it manually.".format(
                self.poll, self.jitter))
            print("Telegram: {}".format("ON" if self.tg_on else "OFF (set BOT_TOKEN/CHAT_ID in .env)"))
            print("Heartbeat: {}".format(
                "every {}h".format(self.heartbeat_sec / 3600.0) if self.heartbeat_sec > 0 else "off"))
            print("Ctrl+C to stop.\n")

            self.last_heartbeat = time.monotonic()
            self.notify("\U0001F916 e-queue монітор запущено.\nПослуга: {}\nПеревірка кожні ~{}s (+/-{}s): {}".format(
                self.service, self.poll, self.jitter, self.url))

            try:
                while True:
                    try:
                        status = await self.check(page)
                    except Exception as e:
                        print("[{}] check error: {}".format(ts(), e))
                        self.maybe_error(e)
                        status = "notready"

                    if status == "ratelimited":
                        self.last_status = "rate-limit (забагато запитів)"
                        mins = max(1, self.cooldown // 60)
                        print("[{}] rate-limited (too many requests) — back off {}s".format(ts(), self.cooldown))
                        if not self.was_ratelimited:
                            self.was_ratelimited = True
                            self.notify(
                                "⚠️ Забагато запитів (too many requests).\n"
                                "Чекаю {} хв і пробую знову.\nЧас: {}".format(mins, ts()))
                        self.was_available = False
                        self.maybe_heartbeat()
                        await asyncio.sleep(self.cooldown)
                        continue

                    if status == "notready":
                        self.last_status = "не завантажилось (Cloudflare/черга?)"
                        print("[{}] page not ready (Cloudflare/queue/loading?) — waiting".format(ts()))
                        self.was_available = False
                    elif status == "available":
                        if not self.was_available and self.confirm and not await self.confirm_available(page):
                            self.last_status = "хибне ВІЛЬНО (не підтвердилось)"
                            print("[{}] хибне ВІЛЬНО — не підтвердилось повторною перевіркою, пропускаю".format(ts()))
                            self.was_available = False
                        else:
                            self.last_status = "ВІЛЬНО"
                            print("[{}] ВІЛЬНО — запис можливий!".format(ts()))
                            if not self.was_available:
                                self.was_available = True
                                self.alert()
                    else:  # busy
                        self.last_status = "зайнято"
                        print("[{}] зайнято (всі місця зайняті)".format(ts()))
                        self.was_available = False

                    self.was_ratelimited = False
                    self.maybe_heartbeat()
                    await asyncio.sleep(self.poll_delay())
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
            finally:
                await ctx.close()


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="e-queue availability monitor + Telegram alert (read-only)")
    parser.add_argument("--url", default=DEFAULT_URL, help="e-queue page URL")
    parser.add_argument("--poll", type=int, default=60,
                        help="base seconds between checks (default 60)")
    parser.add_argument("--jitter", type=int, default=10,
                        help="random +/- seconds added to each poll (default 10 => 50-70s)")
    parser.add_argument("--service", default=DEFAULT_SERVICE,
                        help="dropdown option text to select (substring match)")
    parser.add_argument("--cooldown", type=int, default=600,
                        help="seconds to wait after a 'too many requests' page (default 600 = 10 min)")
    parser.add_argument("--heartbeat-hours", type=float, default=4.0,
                        help="hours between 'still alive' pings (0 = off, default 4)")
    parser.add_argument("--confirm", type=int, default=2,
                        help="extra re-checks required before firing an alert, to filter "
                             "out transient false 'available' blips (0 = off, default 2)")
    parser.add_argument("--confirm-delay", type=int, default=5,
                        help="seconds between confirmation re-checks (default 5)")
    parser.add_argument("--dump", action="store_true",
                        help="one-shot: open page, select service, print detected status and "
                             "save the full body text to dump.txt, then exit")
    args = parser.parse_args()
    asyncio.run(Monitor(args.url, args.poll, args.heartbeat_hours, args.cooldown,
                        args.service, args.jitter, args.confirm, args.confirm_delay,
                        dump_only=args.dump).run())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped")
