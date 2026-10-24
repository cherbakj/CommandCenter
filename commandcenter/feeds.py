"""Overlay calendars: read-only iCal feeds (Google Calendar "secret address", iCloud shared calendars, ...)
shown color-coded on the 10-day calendar.

- Each feed's private URL is a secret: stored only in Windows Credential Manager, never returned to the page.
- Only https (webcal:// is treated as https), only public hosts, no redirects to anything else, 10 MB cap.
- Events are kept in memory only (refetched every 15 minutes when the calendar is viewed). Nothing is
  written to Outlook or to disk.
"""
import asyncio
import ipaddress
import re
import secrets
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

import keyring
import keyring.errors
from fastapi import APIRouter, Body, HTTPException

from . import db

router = APIRouter(prefix="/api/feeds")
KEYRING_SERVICE = "CommandCenter-Calendars"
CACHE_SECONDS = 900
MAX_BYTES = 10 * 1024 * 1024
MAX_FEEDS = 12
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
DEFAULT_COLORS = ["#ff9f43", "#b388ff", "#4dd0e1", "#ffd166", "#ff6bcb", "#7cf29a", "#ff7b7b"]
_cache: dict[str, dict] = {}  # feed id -> {"at": ts, "events": [...], "error": str|None}


# ---- feed list (metadata in SQLite; URL in Credential Manager) ------------------------------------

def feeds() -> list[dict]:
    return db.kv_get("feeds:list", [])


def _save(items):
    db.kv_set("feeds:list", items)


def _kind(host: str) -> str:
    if host.endswith("google.com"):
        return "Google"
    if host.endswith("icloud.com"):
        return "iCloud"
    return "Calendar"


def _check_url(url: str) -> str:
    url = url.strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[9:]
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("Use the calendar's https:// (or webcal://) iCal link")
    if len(url) > 2000 or any(ord(c) < 33 for c in url):
        raise ValueError("That link doesn't look right")
    _public_host(parts.hostname)
    return url


def _public_host(host: str):
    """Refuse localhost/private/link-local targets, so a feed can't be pointed at this PC or the LAN."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ValueError(f"Couldn't find {host}. Check the link (and your internet connection)")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise ValueError("Calendar links must point to a public internet address")


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)  # raises for non-https or private targets
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_HttpsOnlyRedirects())


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "CommandCenter/1.0 (read-only calendar overlay)",
                                               "Accept": "text/calendar, */*;q=0.5"})
    with _opener.open(req, timeout=20) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Calendar is larger than 10 MB")
    if b"BEGIN:VCALENDAR" not in data[:4096]:
        raise ValueError("That link didn't return a calendar (iCal) file")
    return data


def _local(value):
    """iCal date or datetime -> (naive local datetime, is_all_day)."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone().replace(tzinfo=None)
        return value, False
    return datetime(value.year, value.month, value.day), True


def _parse(data: bytes, start: datetime, end: datetime) -> list[dict]:
    import icalendar
    import recurring_ical_events
    cal = icalendar.Calendar.from_ical(data)
    out = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        if str(ev.get("STATUS", "")).upper() == "CANCELLED":
            continue
        s = ev.get("DTSTART")
        if s is None:
            continue
        s_dt, all_day = _local(s.dt)
        e = ev.get("DTEND")
        if e is not None:
            e_dt, _ = _local(e.dt)
        else:
            e_dt = s_dt + (timedelta(days=1) if all_day else timedelta(hours=1))
        out.append({
            "title": str(ev.get("SUMMARY", "") or "(busy)")[:200],
            "location": str(ev.get("LOCATION", "") or "")[:200],
            "start": s_dt.isoformat(timespec="minutes"), "end": e_dt.isoformat(timespec="minutes"),
            "all_day": all_day,
        })
        if len(out) >= 1000:
            break
    return out


def _fetch(feed: dict, days: int) -> dict:
    url = keyring.get_password(KEYRING_SERVICE, feed["id"])
    if not url:
        raise ValueError("The link for this calendar is missing; remove it and add it again")
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return {"at": time.time(), "events": _parse(_download(url), start, start + timedelta(days=days)), "error": None}


def _error_text(e: Exception) -> str:
    if isinstance(e, urllib.error.HTTPError):
        return f"The calendar link returned HTTP {e.code}. It may have been reset; paste the new link"
    if isinstance(e, urllib.error.URLError):
        return "Couldn't reach the calendar (offline or blocked)"
    return str(e).splitlines()[0][:200] if str(e) else type(e).__name__  # never includes the URL


async def events_between(first: date, last: date) -> tuple[list[dict], list[dict]]:
    """Overlay events in [first, last], plus per-feed status. Uses the cache; refetches stale feeds."""
    days = (last - first).days + 1
    status, events = [], []
    for feed in feeds():
        if not feed.get("enabled", True):
            continue
        c = _cache.get(feed["id"])
        if not c or time.time() - c["at"] > CACHE_SECONDS or c.get("days", 0) < days:
            try:
                c = await asyncio.to_thread(_fetch, feed, max(days, 14))
                c["days"] = max(days, 14)
            except Exception as e:
                c = {**(c or {"events": []}), "at": time.time(), "error": _error_text(e), "days": max(days, 14)}
            _cache[feed["id"]] = c
        status.append({"id": feed["id"], "error": c.get("error"), "count": len(c.get("events", []))})
        for ev in c.get("events", []):
            if ev["start"][:10] <= last.isoformat() and ev["end"] >= first.isoformat():
                events.append({**ev, "feed": feed["id"], "calendar": feed["name"], "color": feed["color"]})
    return events, status


# ---- routes ---------------------------------------------------------------------------------------

def _public(feed: dict) -> dict:
    c = _cache.get(feed["id"], {})
    return {**feed, "error": c.get("error"), "count": len(c.get("events", [])) if c else None,
            "fetched": datetime.fromtimestamp(c["at"]).isoformat(timespec="minutes") if c.get("at") else None}


@router.get("")
async def list_feeds():
    return {"feeds": [_public(f) for f in feeds()], "colors": DEFAULT_COLORS}


@router.post("")
async def add_feed(name: str = Body(...), url: str = Body(...), color: str | None = Body(None)):
    name = name.strip()
    if not 1 <= len(name) <= 40:
        raise HTTPException(400, "Give the calendar a short name (up to 40 characters)")
    items = feeds()
    if len(items) >= MAX_FEEDS:
        raise HTTPException(400, f"Up to {MAX_FEEDS} calendars")
    color = color or DEFAULT_COLORS[len(items) % len(DEFAULT_COLORS)]
    if not HEX.match(color):
        raise HTTPException(400, "Colors must look like #ff9f43")
    try:
        url = await asyncio.to_thread(_check_url, url)
        test = await asyncio.to_thread(_parse, await asyncio.to_thread(_download, url), datetime.now(), datetime.now() + timedelta(days=14))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, _error_text(e))
    feed = {"id": secrets.token_hex(6), "name": name, "color": color.lower(), "enabled": True,
            "kind": _kind(urllib.parse.urlsplit(url).hostname or ""), "added": datetime.now().isoformat(timespec="minutes")}
    keyring.set_password(KEYRING_SERVICE, feed["id"], url)
    _cache[feed["id"]] = {"at": time.time(), "events": test, "error": None, "days": 14}
    _save(items + [feed])
    return {"feed": _public(feed), "upcoming": len(test)}


@router.post("/update")
async def update_feed(id: str = Body(...), name: str | None = Body(None), color: str | None = Body(None),
                      enabled: bool | None = Body(None)):
    items = feeds()
    feed = next((f for f in items if f["id"] == id), None)
    if not feed:
        raise HTTPException(404, "No such calendar")
    if name is not None:
        if not 1 <= len(name.strip()) <= 40:
            raise HTTPException(400, "Give the calendar a short name (up to 40 characters)")
        feed["name"] = name.strip()
    if color is not None:
        if not HEX.match(color):
            raise HTTPException(400, "Colors must look like #ff9f43")
        feed["color"] = color.lower()
    if enabled is not None:
        feed["enabled"] = bool(enabled)
    _save(items)
    return {"feed": _public(feed)}


@router.post("/delete")
async def delete_feed(id: str = Body(..., embed=True)):
    items = feeds()
    if not any(f["id"] == id for f in items):
        raise HTTPException(404, "No such calendar")
    _save([f for f in items if f["id"] != id])
    _cache.pop(id, None)
    try:
        keyring.delete_password(KEYRING_SERVICE, id)
    except keyring.errors.PasswordDeleteError:
        pass
    return {"ok": True}


@router.post("/refresh")
async def refresh_feeds():
    _cache.clear()
    _, status = await events_between(date.today(), date.today() + timedelta(days=13))
    return {"status": status}
