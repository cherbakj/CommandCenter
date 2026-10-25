"""Website monitor: reads a value from a page (public or behind a login) and alerts on change.

Each monitor names a browser `profile`. A profile is a saved Edge/Chrome profile under
data/browser/<profile>; log in once with `run.cmd login <profile> <url>` and later checks
reuse that session.
"""
import asyncio
import logging
import re
from datetime import datetime, timedelta

from playwright.async_api import async_playwright

from .. import alerts, config, db

log = logging.getLogger(__name__)
_profile_locks: dict[str, asyncio.Lock] = {}


def monitors() -> list[dict]:
    return [m for m in (config.section("websites", []) or []) if m.get("name") and m.get("url")]


def find(name) -> dict | None:
    return next((m for m in monitors() if m["name"] == name), None)


def _lock(profile) -> asyncio.Lock:
    return _profile_locks.setdefault(profile, asyncio.Lock())


def _profile_dir(profile):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", profile):
        raise ValueError(f"Profile names may only use letters, numbers, - and _: {profile!r}")
    return str(config.DATA_DIR / "browser" / profile)


def _browser_channel() -> str:
    return config.section("browser", {}).get("channel", "msedge")


async def _normal_user_agent(pw) -> str:
    # Headless Edge announces itself as "HeadlessEdg"; some sites (Teams included) refuse it.
    ua = db.kv_get("browser:ua")
    if not ua:
        browser = await pw.chromium.launch(channel=_browser_channel(), headless=True)
        page = await browser.new_page()
        ua = (await page.evaluate("navigator.userAgent")).replace("HeadlessEdg/", "Edg/").replace("HeadlessChrome", "Chrome")
        await browser.close()
        db.kv_set("browser:ua", ua)
    return ua


async def _launch(pw, profile, headless):
    # Edge takes a few seconds to let go of a profile after its window closes (e.g. right after a sign-in);
    # starting it on that profile in the meantime fails with "Target page, context or browser has been closed".
    for attempt in range(4):
        try:
            return await pw.chromium.launch_persistent_context(
                _profile_dir(profile), channel=_browser_channel(), headless=headless,
                viewport={"width": 1400, "height": 900},
                user_agent=await _normal_user_agent(pw) if headless else None,
            )
        except Exception as e:
            if attempt == 3 or "has been closed" not in str(e):
                raise
            await asyncio.sleep(3)


def _number(text):
    m = re.search(r"-?\d[\d,]*\.?\d*", text or "")
    return float(m.group(0).replace(",", "")) if m else None


class LoginNeeded(Exception):
    pass


async def read_value(m: dict) -> str:
    profile = m.get("profile", "public")
    async with _lock(profile), async_playwright() as pw:
        ctx = await _launch(pw, profile, headless=True)
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(m["url"], wait_until="domcontentloaded", timeout=45000)
            if m.get("logged_in_selector"):
                try:
                    await page.wait_for_selector(m["logged_in_selector"], timeout=20000)
                except Exception:
                    raise LoginNeeded(f"Not logged in. Run: run.cmd login {profile} {m['url']}")
            selector = m.get("selector") or "body"
            el = await page.wait_for_selector(selector, timeout=20000)
            text = re.sub(r"\s+", " ", (await el.inner_text()).strip())
        finally:
            await ctx.close()
    if m.get("regex"):
        found = re.search(m["regex"], text)
        if not found:
            raise ValueError(f"Pattern {m['regex']!r} not found in: {text[:120]}")
        text = found.group(1) if found.groups() else found.group(0)
    return text[:500]



def _should_alert(m, old, new) -> str | None:
    mode = m.get("alert_on", "change")
    if mode == "change":
        return f"Changed from {old!r} to {new!r}" if old is not None and old != new else None
    limit = m.get("threshold")
    n_old, n_new = _number(old), _number(new)
    if limit is None or n_new is None:
        return None
    crossed = (lambda v: v is not None and v > limit) if mode == "above" else (lambda v: v is not None and v < limit)
    if crossed(n_new) and not crossed(n_old):
        return f"Now {new} ({mode} {limit})"
    return None


async def check(m: dict) -> dict:
    key = f"web:{m['name']}"
    state = db.kv_get(key, {})
    old_value, was_ok = state.get("value"), state.get("ok", True)
    try:
        value = await read_value(m)
        reason = _should_alert(m, old_value, value)
        state = {"ok": True, "value": value, "checked": db.now(), "changed": state.get("changed")}
        if reason:
            state["changed"] = db.now()
            alerts.raise_alert("Website", f"{m['name']}: {reason}", m["url"], m.get("priority", "normal"),
                               {"type": "url", "url": m["url"]}, toast="watched page changed")
        elif not was_ok:
            alerts.raise_alert("Website", f"{m['name']} is reachable again", m["url"], "low",
                               {"type": "url", "url": m["url"]}, toast="page reachable again")
    except Exception as e:
        msg = str(e).splitlines()[0][:300]
        state = {**state, "ok": False, "checked": db.now(), "error": msg, "login_needed": isinstance(e, LoginNeeded)}
        if was_ok:  # alert on the transition only, not every failed check
            title = "login needed" if isinstance(e, LoginNeeded) else "check failed"
            alerts.raise_alert("Website", f"{m['name']}: {title}", msg, "normal", {"type": "url", "url": m["url"]},
                               toast=title)
    db.kv_set(key, state)
    return state


async def login(profile: str, url: str):
    """Opens a visible browser on the profile; the session is saved when the window closes."""
    async with _lock(profile), async_playwright() as pw:
        try:
            ctx = await _launch(pw, profile, headless=False)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(url)
            closed = asyncio.Event()
            ctx.on("close", lambda _: closed.set())
            await closed.wait()
        except Exception as e:  # runs as a background task: nothing else would report it
            alerts.raise_alert("Website", "Sign-in window failed", str(e).splitlines()[0][:300], "normal",
                               toast="sign-in window failed")
            raise
        await asyncio.sleep(3)  # let Edge release the profile before the next check opens it


async def watch_loop():
    last_run: dict[str, datetime] = {}
    while True:
        for m in monitors():
            if not m.get("enabled", True):
                continue
            due = last_run.get(m["name"], datetime.min) + timedelta(minutes=m.get("interval_minutes", 15))
            if datetime.now() >= due:
                last_run[m["name"]] = datetime.now()
                try:
                    await check(m)
                except Exception:
                    log.exception("Website check crashed: %s", m["name"])
        await asyncio.sleep(20)
