"""Teams connector.

Reading: a background browser (saved profile "teams", logged in once by you) opens Teams on the
web and reads the unread count from the page title, e.g. "(3) Chat | Microsoft Teams". Details
come from Teams' missed-activity emails in Outlook.
Replying: Teams deep links (https://teams.microsoft.com/l/chat/...) open a chat in the Teams app
with your message filled in; you press send.
"""
import asyncio
import logging
import re

from playwright.async_api import async_playwright

from .. import alerts, config, db
from . import web

log = logging.getLogger(__name__)
PROFILE = "teams"
TITLE_COUNT = re.compile(r"^\((\d+)\)")


def settings() -> dict:
    return config.section("teams", {})


def url() -> str:
    return settings().get("url", "https://teams.microsoft.com/v2/")


async def read_unread() -> int:
    async with web._lock(PROFILE), async_playwright() as pw:
        ctx = await web._launch(pw, PROFILE, headless=True)
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(url(), wait_until="domcontentloaded", timeout=60000)
            # Teams takes a while to boot; wait until the title says Teams, or we land on a sign-in page.
            for _ in range(45):
                if "login.microsoftonline.com" in page.url or "login.live.com" in page.url:
                    raise web.LoginNeeded("Teams needs you to sign in. Use Log in on the Teams tab.")
                title = await page.title()
                if "Teams" in title and "|" in title:
                    break
                await page.wait_for_timeout(1000)
            else:
                raise TimeoutError(f"Teams didn't finish loading (title: {await page.title()!r})")
            await page.wait_for_timeout(4000)  # let the unread badge settle
            m = TITLE_COUNT.match(await page.title())
            return int(m.group(1)) if m else 0
        finally:
            await ctx.close()


async def check() -> dict:
    state = db.kv_get("teams:state", {})
    was_ok = state.get("ok", True)
    try:
        unread = await read_unread()
        if unread > state.get("unread", 0):
            alerts.raise_alert("Teams", f"{unread} unread in Teams", "", settings().get("priority", "normal"),
                               {"type": "url", "url": url()}, toast=f"{int(unread)} unread")
        state = {"ok": True, "unread": unread, "checked": db.now()}
    except Exception as e:
        msg = str(e).splitlines()[0][:300]
        login = isinstance(e, web.LoginNeeded)
        if was_ok:
            alerts.raise_alert("Teams", "Teams needs a sign-in" if login else "Teams check failed", msg, "normal",
                               toast="sign-in needed" if login else "check failed")
        state = {**state, "ok": False, "checked": db.now(), "error": msg, "login_needed": login}
    db.kv_set("teams:state", state)
    return state


async def login():
    await web.login(PROFILE, url())


def is_set_up() -> bool:
    return (config.DATA_DIR / "browser" / PROFILE).exists()


async def watch_loop():
    while True:
        cfg = settings()
        if cfg.get("enabled", True) and is_set_up():
            try:
                await check()
            except Exception:
                log.exception("Teams check crashed")
        await asyncio.sleep(max(60, int(cfg.get("poll_minutes", 3)) * 60))
