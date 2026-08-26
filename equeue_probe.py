#!/usr/bin/env python3
"""Read-only monitor for the Ukrainian passport e-queue (pasport.org.ua).

Opens the e-queue page in a real (headful) browser so you can pass Cloudflare /
waiting-room manually, then periodically triggers the #service dropdown and
captures the /api/v1/PreReg/GetDays response to detect free slots. When slots
appear it prints them and (optionally) sends a Telegram alert.

It does NOT book anything.
"""
import argparse
import asyncio
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Force UTF-8 on stdout/stderr so non-ASCII (Ukrainian/Czech) text and emoji do
# not crash print() on Windows consoles that default to cp1251.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:
    pass

from playwright.async_api import async_playwright  # noqa: E402

DEFAULT_URL = "https://prague.pasport.org.ua/solutions/e-queue"
PROFILE_DIR = ".equeue_profile"
TARGET = "/api/v1/PreReg/GetDays"
STEALTH_JS = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"

TRIGGER_JS = """
(serviceId) => {
  const sel = document.querySelector('#service');
  if (!sel) return false;
  sel.value = serviceId;
  sel.dispatchEvent(new Event('change', { bubbles: true }));
  return true;
}
"""


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


def parse_days(data: object) -> List[Tuple[str, int]]:
    """Return [(YYYY-MM-DD, count)] for FUTURE days with allowedJobCount > 0."""
    if isinstance(data, dict) and isinstance(data.get("days"), list):
        data = data["days"]
    if not isinstance(data, list):
        return []
    today = date.today()
    out: List[Tuple[str, int]] = []
    for d in data:
        if not isinstance(d, dict):
            continue
        cnt = d.get("allowedJobCount") or 0
        if not isinstance(cnt, int) or cnt <= 0:
            continue
        raw = d.get("date") or d.get("datePart")
        if not raw:
            continue
        try:
            dd = datetime.fromisoformat(str(raw).replace("Z", "")).date()
        except ValueError:
            continue
        if dd < today:
            continue
        out.append((dd.isoformat(), cnt))
    out.sort()
    return out


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


class Probe:
    def __init__(self, url: str, services: List[str], poll: int) -> None:
        self.url = url
        self.services = services
        self.poll = poll
        self.pending: Optional[asyncio.Future] = None
        self.labels: Dict[str, str] = {}
        self.dumped = False
        self.first_json_shown = False
        self.last_alert: Dict[str, List[str]] = {}
        self.token = os.getenv("BOT_TOKEN", "").strip()
        self.chat_id = os.getenv("CHAT_ID", "").strip()

    def on_response(self, resp) -> None:
        if TARGET not in resp.url:
            return
        asyncio.create_task(self._capture(resp))

    async def _capture(self, resp) -> None:
        try:
            body = await resp.text()
        except Exception:
            return
        try:
            data: object = json.loads(body)
        except ValueError:
            data = body
        if not self.first_json_shown:
            self.first_json_shown = True
            pretty = json.dumps(data, ensure_ascii=False, indent=2) if not isinstance(data, str) else data
            print("\n[{}] first GetDays response ({}):".format(ts(), resp.status))
            print(pretty[:3000] + ("\n... (truncated)" if len(pretty) > 3000 else ""))
        if self.pending and not self.pending.done():
            self.pending.set_result(data)

    async def dump_services(self, page) -> None:
        try:
            options = await page.eval_on_selector_all(
                "#service option",
                "els => els.map(e => ({value: e.value, label: (e.textContent || '').trim()}))",
            )
        except Exception:
            return
        options = [o for o in (options or []) if o.get("value")]
        if not options:
            return
        self.labels = {o["value"]: o["label"] for o in options}
        self.dumped = True
        print("\n[{}] #service options (id -> label):".format(ts()))
        for o in options:
            print("    {!r} -> {!r}".format(o["value"], o["label"]))
        print("")

    async def check_service(self, page, service_id: str) -> Optional[List[Tuple[str, int]]]:
        loop = asyncio.get_event_loop()
        self.pending = loop.create_future()
        ok = await page.evaluate(TRIGGER_JS, service_id)
        if not ok:
            self.pending = None
            return None
        try:
            data = await asyncio.wait_for(self.pending, timeout=20)
        except asyncio.TimeoutError:
            return None
        finally:
            self.pending = None
        return parse_days(data)

    def maybe_alert(self, service_id: str, days: List[Tuple[str, int]]) -> None:
        label = self.labels.get(service_id, "service {}".format(service_id))
        dates = [d for d, _ in days]
        if dates and dates != self.last_alert.get(service_id):
            self.last_alert[service_id] = dates
            total = sum(c for _, c in days)
            lines = ["\U0001F6A8 Слоти зʼявились: {}".format(label),
                     "Місто: {}".format(self.url.split("//")[-1].split(".")[0]),
                     "Всього: {} у дні:".format(total)]
            lines += ["  {} — {}".format(d, c) for d, c in days]
            lines.append(self.url)
            text = "\n".join(lines)
            print("\n[{}] *** SLOTS *** {} -> {}".format(ts(), label, days))
            if self.token and self.chat_id:
                try:
                    send_telegram(self.token, self.chat_id, text)
                    print("[{}] telegram alert sent".format(ts()))
                except Exception as e:
                    print("[{}] telegram send failed: {}".format(ts(), e))
        elif not dates:
            self.last_alert.pop(service_id, None)

    async def run(self) -> None:
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
            page.on("response", self.on_response)

            print("[{}] Opening {}".format(ts(), self.url))
            print("Pass Cloudflare/queue if shown. Then it auto-checks every {}s.".format(self.poll))
            print("Telegram: {}".format(
                "ON" if (self.token and self.chat_id) else "OFF (set BOT_TOKEN/CHAT_ID in .env)"))
            print("Ctrl+C to stop.\n")
            try:
                await page.goto(self.url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                print("[{}] goto warning: {}".format(ts(), e))

            try:
                while True:
                    has_select = await page.query_selector("#service")
                    if not has_select:
                        print("[{}] #service not visible yet (Cloudflare/queue/loading?) — waiting".format(ts()))
                    else:
                        if not self.dumped:
                            await self.dump_services(page)
                        for sid in self.services:
                            days = await self.check_service(page, sid)
                            label = self.labels.get(sid, "service {}".format(sid))
                            if days is None:
                                print("[{}] {}: no API response (timeout?)".format(ts(), label))
                            elif not days:
                                print("[{}] {}: 0 slots (всі місця зайняті)".format(ts(), label))
                            else:
                                print("[{}] {}: {} slots -> {}".format(
                                    ts(), label, sum(c for _, c in days), days))
                                self.maybe_alert(sid, days)
                    await asyncio.sleep(self.poll)
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
            finally:
                await ctx.close()


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="e-queue monitor + Telegram alert (read-only)")
    parser.add_argument("--url", default=DEFAULT_URL, help="e-queue page URL")
    parser.add_argument("--services", default="4",
                        help="comma-separated service ids (default: 4 = adult passport)")
    parser.add_argument("--poll", type=int, default=60, help="seconds between checks (default 60)")
    args = parser.parse_args()
    services = [s.strip() for s in args.services.split(",") if s.strip()]
    asyncio.run(Probe(args.url, services, args.poll).run())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped")
