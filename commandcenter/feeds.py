"""Overlay calendars: read-only iCal feeds (Google Calendar "secret address", iCloud public calendars)
shown color-coded on the 10-day calendar.

Personal data, so it lives in the personal vault (vault.py), not in DPAPI or Credential Manager:
- Each calendar (name, color, detail level, and its private link) is one encrypted vault record. The link
  never leaves the server and is never logged.
- While the vault is locked or Privacy mode is on: no calendars are listed, nothing is fetched, no events
  are returned, and the in-memory cache is emptied.
- Detail level per calendar, enforced here before anything is cached or returned:
    "full"   title, time, location
    "titles" title and time (default)
    "busy"   time only, shown as "Busy"
  Descriptions, attendees, organizers and the like are never kept.
- Links: https only (webcal:// is treated as https), only calendar.google.com (or a *.google.com /calendar/
  path) and *.icloud.com, port 443, public internet addresses only, redirects re-checked the same way, 10 MB cap.
- Events are kept in memory only (refetched every 15 minutes when the calendar is viewed). Nothing is
  written to Outlook or to disk.

Older versions kept the list in SQLite (kv "feeds:list") and the links in Credential Manager
("CommandCenter-Calendars"). The first unlock of the vault moves them in and deletes those copies.
"""
import asyncio
import ipaddress
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

import keyring
import keyring.errors
from fastapi import APIRouter, Body, HTTPException

from . import db, vault

router = APIRouter(prefix="/api/feeds")
KIND = "overlay-calendar"
LEGACY_KEYRING_SERVICE = "CommandCenter-Calendars"
LEGACY_KV = "feeds:list"
CACHE_SECONDS = 900
MAX_BYTES = 10 * 1024 * 1024
MAX_FEEDS = 12
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
FEED_ID = re.compile(r"^[0-9a-f]{8,64}$")
DETAILS = ("full", "titles", "busy")
DEFAULT_DETAIL = "titles"
DEFAULT_COLORS = ["#ff9f43", "#b388ff", "#4dd0e1", "#ffd166", "#ff6bcb", "#7cf29a", "#ff7b7b"]
HOSTS_HELP = "Only Google Calendar (calendar.google.com) and iCloud (icloud.com) calendar links can be added"
_cache: dict[str, dict] = {}  # feed id -> {"at": ts, "events": [...], "error": str|None, "days": n, "detail": level}
_epoch = [0]                  # bumped on every vault lock: a fetch that started before a lock never fills the cache


def _forget_cache():
    _cache.clear()
    _epoch[0] += 1


# ---- feed list (in the vault) -----------------------------------------------------------------

def _meta(rec: dict) -> dict:
    """A vault record without its link."""
    detail = rec.get("detail") if rec.get("detail") in DETAILS else DEFAULT_DETAIL
    return {"id": rec["id"], "name": rec.get("name", "Calendar"), "color": rec.get("color", DEFAULT_COLORS[0]),
            "enabled": rec.get("enabled", True) is not False, "kind": rec.get("kind", "Calendar"),
            "added": rec.get("added"), "detail": detail}


def feeds() -> list[dict]:
    """The calendars, without links; [] while personal data isn't available."""
    if not vault.available():
        return []
    try:
        recs = vault.records(KIND)
    except vault.VaultLocked:
        return []
    return sorted((_meta(r) for r in recs), key=lambda f: f.get("added") or "")


def _record(feed_id: str) -> dict:
    rec = vault.get(KIND, feed_id) if isinstance(feed_id, str) and FEED_ID.match(feed_id) else None
    if not rec:
        raise HTTPException(404, "No such calendar")
    return rec


def _kind(host: str) -> str:
    if host == "google.com" or host.endswith(".google.com"):
        return "Google"
    if host == "icloud.com" or host.endswith(".icloud.com"):
        return "iCloud"
    return "Calendar"


def allowed_host(host: str, path: str) -> bool:
    host = (host or "").lower()
    if host == "calendar.google.com":
        return True
    if host == "google.com" or host.endswith(".google.com"):
        return path.startswith("/calendar/")  # e.g. www.google.com/calendar/ical/... (older Google links)
    return host == "icloud.com" or host.endswith(".icloud.com")


def _check_url(url: str, resolve: bool = True) -> str:
    url = url.strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[9:]
    if len(url) > 2000 or any(ord(c) < 33 for c in url):
        raise ValueError("That link doesn't look right")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("Use the calendar's https:// (or webcal://) iCal link")
    try:
        port = parts.port
    except ValueError:
        raise ValueError("That link doesn't look right")
    if port not in (None, 443):
        raise ValueError("Calendar links must use the standard https port")
    if not allowed_host(parts.hostname, parts.path):
        raise ValueError(HOSTS_HELP)
    if resolve:
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
        _check_url(newurl)  # raises for non-https, other hosts or private targets
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


def _parse(data: bytes, start: datetime, end: datetime, detail: str = DEFAULT_DETAIL) -> list[dict]:
    """Events between start and end, reduced to what `detail` allows (nothing else is kept)."""
    import icalendar
    import recurring_ical_events
    detail = detail if detail in DETAILS else DEFAULT_DETAIL
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
        item = {"start": s_dt.isoformat(timespec="minutes"), "end": e_dt.isoformat(timespec="minutes"), "all_day": all_day}
        if detail == "busy":
            item.update(title="Busy", busy=True)
        else:
            item["title"] = str(ev.get("SUMMARY", "") or "(busy)")[:200]
            if detail == "full":
                item["location"] = str(ev.get("LOCATION", "") or "")[:200]
        out.append(item)
        if len(out) >= 1000:
            break
    return out


def _fetch(feed_id: str, detail: str, days: int) -> dict:
    url = (vault.get(KIND, feed_id) or {}).get("url")
    if not url:
        raise ValueError("The link for this calendar is missing; remove it and add it again")
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    events = _parse(_download(_check_url(url)), start, start + timedelta(days=days), detail)
    return {"at": time.time(), "events": events, "error": None, "detail": detail}


def _error_text(e: Exception) -> str:
    if isinstance(e, urllib.error.HTTPError):
        return f"The calendar link returned HTTP {e.code}. It may have been reset; paste the new link"
    if isinstance(e, urllib.error.URLError):
        return "Couldn't reach the calendar (offline or blocked)"
    if isinstance(e, vault.VaultLocked):
        return "Personal vault is locked"
    return str(e).splitlines()[0][:200] if str(e) else type(e).__name__  # never includes the URL


async def events_between(first: date, last: date) -> tuple[list[dict], list[dict]]:
    """Overlay events in [first, last], plus per-feed status. Nothing (and no fetch) while personal data is unavailable."""
    if not vault.available():
        return [], []
    epoch = _epoch[0]
    days = (last - first).days + 1
    status, events = [], []
    for feed in await asyncio.to_thread(feeds):
        if not feed["enabled"]:
            continue
        c = _cache.get(feed["id"])
        if not c or time.time() - c["at"] > CACHE_SECONDS or c.get("days", 0) < days or c.get("detail") != feed["detail"]:
            try:
                c = await asyncio.to_thread(_fetch, feed["id"], feed["detail"], max(days, 14))
                c["days"] = max(days, 14)
            except Exception as e:
                keep = c if c and c.get("detail") == feed["detail"] else {"events": []}
                c = {**keep, "at": time.time(), "error": _error_text(e), "days": max(days, 14), "detail": feed["detail"]}
            if epoch != _epoch[0] or not vault.available():  # locked while fetching
                return [], []
            _cache[feed["id"]] = c
        status.append({"id": feed["id"], "error": c.get("error"), "count": len(c.get("events", []))})
        for ev in c.get("events", []):
            if ev["start"][:10] <= last.isoformat() and ev["end"] >= first.isoformat():
                events.append({**ev, "feed": feed["id"], "calendar": feed["name"], "color": feed["color"]})
    return events, status


# ---- older copies (kv list + Credential Manager), moved into the vault on first unlock -----------

def _legacy_list() -> list[dict]:
    items = db.kv_get(LEGACY_KV, []) or []
    return [f for f in items if isinstance(f, dict) and isinstance(f.get("id"), str)]


def _legacy_count() -> int:
    return len(_legacy_list())


def _legacy_targets() -> list[str]:
    """Every Credential Manager entry the old version made (target names)."""
    try:
        import win32cred
        creds = win32cred.CredEnumerate(f"*{LEGACY_KEYRING_SERVICE}", 0) or []
    except Exception:  # none saved (ERROR_NOT_FOUND) or not available
        return []
    targets = [c.get("TargetName") or "" for c in creds]
    return [t for t in targets if t == LEGACY_KEYRING_SERVICE or t.endswith("@" + LEGACY_KEYRING_SERVICE)]


def _delete_legacy_secrets(ids):
    for feed_id in ids:
        try:
            keyring.delete_password(LEGACY_KEYRING_SERVICE, feed_id)
        except keyring.errors.KeyringError:
            pass
    targets = _legacy_targets()  # anything left over (e.g. from a calendar removed long ago)
    if targets:
        import win32cred
        for target in targets:
            try:
                win32cred.CredDelete(target, win32cred.CRED_TYPE_GENERIC, 0)
            except Exception:
                pass


def _delete_legacy_list():
    with db._lock, vault.secure_delete() as c:
        c.execute("DELETE FROM kv WHERE key = ?", (LEGACY_KV,))
        c.commit()


def _migrate_legacy() -> int:
    items = _legacy_list()
    if not items:
        _delete_legacy_secrets([])
        return 0
    for f in items:
        try:
            url = keyring.get_password(LEGACY_KEYRING_SERVICE, f["id"])
        except keyring.errors.KeyringError:
            url = None
        rec = {"name": str(f.get("name", "Calendar"))[:40], "color": f["color"] if HEX.match(str(f.get("color", ""))) else DEFAULT_COLORS[0],
               "enabled": f.get("enabled", True) is not False, "kind": f.get("kind", "Calendar"), "added": f.get("added"),
               "detail": DEFAULT_DETAIL, "url": url}
        vault.put(KIND, rec, f["id"] if FEED_ID.match(f["id"]) else None)
    _delete_legacy_secrets([f["id"] for f in items])
    _delete_legacy_list()
    return len(items)


def _wipe_legacy():
    _delete_legacy_secrets([f["id"] for f in _legacy_list()])
    if _legacy_list():
        _delete_legacy_list()


vault.register_store("overlay calendars", legacy_count=_legacy_count, migrate=_migrate_legacy,
                     wipe_legacy=_wipe_legacy, on_lock=_forget_cache)


# ---- routes ---------------------------------------------------------------------------------------

def _public(feed: dict) -> dict:
    c = _cache.get(feed["id"], {})
    return {**feed, "error": c.get("error"), "count": len(c.get("events", [])) if c else None,
            "fetched": datetime.fromtimestamp(c["at"]).isoformat(timespec="minutes") if c.get("at") else None}


def _detail(value) -> str:
    if value not in DETAILS:
        raise HTTPException(400, "Detail must be full, titles or busy")
    return value


@router.get("")
async def list_feeds():
    items = await asyncio.to_thread(feeds)
    return {"feeds": [_public(f) for f in items], "colors": DEFAULT_COLORS, "details": list(DETAILS),
            "default_detail": DEFAULT_DETAIL, "available": vault.available()}


@router.post("")
async def add_feed(name: str = Body(...), url: str = Body(...), color: str | None = Body(None), detail: str = Body(DEFAULT_DETAIL)):
    vault.require_available()
    name = name.strip()
    if not 1 <= len(name) <= 40:
        raise HTTPException(400, "Give the calendar a short name (up to 40 characters)")
    detail = _detail(detail)
    items = await asyncio.to_thread(feeds)
    if len(items) >= MAX_FEEDS:
        raise HTTPException(400, f"Up to {MAX_FEEDS} calendars")
    color = color or DEFAULT_COLORS[len(items) % len(DEFAULT_COLORS)]
    if not HEX.match(color):
        raise HTTPException(400, "Colors must look like #ff9f43")
    try:
        url = await asyncio.to_thread(_check_url, url)
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        test = await asyncio.to_thread(_parse, await asyncio.to_thread(_download, url), start, start + timedelta(days=14), detail)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, _error_text(e))
    rec = {"name": name, "color": color.lower(), "enabled": True, "kind": _kind(urllib.parse.urlsplit(url).hostname or ""),
           "added": datetime.now().isoformat(timespec="seconds"), "detail": detail, "url": url}
    try:
        feed_id = await asyncio.to_thread(vault.put, KIND, rec)
    except vault.VaultLocked:
        raise HTTPException(423, "Your personal vault is locked. Unlock it first")
    if vault.available():
        _cache[feed_id] = {"at": time.time(), "events": test, "error": None, "days": 14, "detail": detail}
    feed = _meta({**rec, "id": feed_id})
    return {"feed": _public(feed), "upcoming": len(test)}


@router.post("/update")
async def update_feed(id: str = Body(...), name: str | None = Body(None), color: str | None = Body(None),
                      enabled: bool | None = Body(None), detail: str | None = Body(None)):
    vault.require_available()
    try:
        rec = await asyncio.to_thread(_record, id)
        if name is not None:
            if not 1 <= len(name.strip()) <= 40:
                raise HTTPException(400, "Give the calendar a short name (up to 40 characters)")
            rec["name"] = name.strip()
        if color is not None:
            if not HEX.match(color):
                raise HTTPException(400, "Colors must look like #ff9f43")
            rec["color"] = color.lower()
        if enabled is not None:
            rec["enabled"] = bool(enabled)
        if detail is not None and _detail(detail) != rec.get("detail"):
            rec["detail"] = detail
            _cache.pop(id, None)  # refetch at the new level; nothing more detailed stays in memory
        feed_id = rec.pop("id")
        await asyncio.to_thread(vault.put, KIND, rec, feed_id)
    except vault.VaultLocked:
        raise HTTPException(423, "Your personal vault is locked. Unlock it first")
    return {"feed": _public(_meta({**rec, "id": feed_id}))}


@router.post("/delete")
async def delete_feed(id: str = Body(..., embed=True)):
    vault.require_available()
    try:
        ok = await asyncio.to_thread(vault.delete, KIND, id)
    except vault.VaultLocked:
        raise HTTPException(423, "Your personal vault is locked. Unlock it first")
    if not ok:
        raise HTTPException(404, "No such calendar")
    _cache.pop(id, None)
    return {"ok": True}


@router.post("/refresh")
async def refresh_feeds():
    _cache.clear()
    _, status = await events_between(date.today(), date.today() + timedelta(days=13))
    return {"status": status}
