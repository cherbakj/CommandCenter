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
  path) and *.icloud.com, port 443, global (public internet) addresses only, redirects re-checked the same way,
  10 MB cap. The connection is made to the address that was checked (no second DNS lookup, so no rebinding);
  TLS still verifies the certificate for the host name. No proxy is used.
- Parsing is bounded in time and memory whatever a feed contains (see _parse): at most 5000 VEVENTs near the
  window, 1000 events, a 3 s budget, no SECONDLY / MINUTELY rules; one download + parse per calendar at a time.
- Events are kept in memory only (refetched every 15 minutes when the calendar is viewed). Nothing is
  written to Outlook or to disk.

Older versions kept the list in SQLite (kv "feeds:list") and the links in Credential Manager
("CommandCenter-Calendars"). The first unlock of the vault moves them in and deletes those copies.
"""
import asyncio
import http.client
import ipaddress
import itertools
import re
import socket
import ssl
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
# Parsing limits (a hostile or broken feed must not exhaust CPU or memory; see _parse).
MAX_EVENTS = 1000            # occurrences returned per calendar
MAX_VEVENTS = 5000           # VEVENTs considered after dropping those outside the window
MAX_OCCURRENCES = 5000       # occurrences collected (all series) before sorting
MAX_SERIES_STEPS = 20_000    # estimated recurrences one series needs from its DTSTART to the window's end
MAX_TOTAL_STEPS = 60_000     # the same, summed over every series in the calendar
PARSE_SECONDS = 3.0          # time budget per parse (checked as it runs)
FETCH_TIMEOUT = 20           # download (socket timeout)
HOSTS_HELP = "Only Google Calendar (calendar.google.com) and iCloud (icloud.com) calendar links can be added"
_cache: dict[str, dict] = {}  # feed id -> {"at": ts, "events": [...], "error": str|None, "days": n, "detail": level}
_epoch = [0]                  # bumped on every vault lock: a fetch that started before a lock never fills the cache
_inflight: dict = {}          # (feed id, detail, days) -> asyncio future: one download + parse per feed at a time


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


def _public_ips(host: str) -> list[str]:
    """Every address the name resolves to; ValueError unless all are global (public internet) addresses, so a
    feed can't be pointed at this PC, the LAN, the VPN, carrier-grade NAT (100.64/10) or another special range."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ValueError(f"Couldn't find {host}. Check the link (and your internet connection)")
    ips = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise ValueError("Calendar links must point to a public internet address")
        ips.append(str(ip))
    if not ips:
        raise ValueError(f"Couldn't find {host}")
    return ips


def _public_host(host: str):
    _public_ips(host)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Resolves the host once, checks every address (_public_ips) and connects to a checked address, so DNS can't
    answer differently between the check and the connection (rebinding). TLS still verifies the certificate
    against the host name (SNI + hostname check), and the Host header is the name."""

    def connect(self):
        if self._tunnel_host:
            raise OSError("Proxies aren't used for calendar links")
        last = None
        for ip in _public_ips(self.host):
            try:
                sock = socket.create_connection((ip, self.port or 443), self.timeout, self.source_address)
                break
            except OSError as e:
                last = e
        else:
            raise last or OSError("Couldn't connect")
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PinnedHTTPSConnection, req, context=self._context)


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)  # raises for non-https, other hosts or private targets (the connection re-checks)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# No proxy (it would bypass the address check): calendar links go straight out, pinned to checked addresses.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                      _PinnedHTTPSHandler(context=ssl.create_default_context()), _HttpsOnlyRedirects())


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "CommandCenter/1.0 (read-only calendar overlay)",
                                               "Accept": "text/calendar, */*;q=0.5"})
    with _opener.open(req, timeout=FETCH_TIMEOUT) as r:
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


class TooComplex(ValueError):
    pass


_PER_DAY = {"YEARLY": 1 / 365, "MONTHLY": 1 / 28, "WEEKLY": 1 / 7, "DAILY": 1.0, "HOURLY": 24.0}


def _n(rule, key) -> int:
    v = rule.get(key)
    return len(v) if isinstance(v, (list, tuple)) else (1 if v else 0)


def _rule_steps(ev, window_end: datetime) -> float:
    """Upper estimate of how many recurrences a series makes from its DTSTART to the window's end: what dateutil
    walks through (and caches) to answer any query. inf for SECONDLY / MINUTELY rules, which this overlay never
    expands (no real calendar uses them; one alone can make millions of occurrences)."""
    try:
        first, _ = _local(ev.get("DTSTART").dt)
    except Exception:
        return float("inf")
    rules = ev.get("RRULE")
    total = 0.0
    for rule in rules if isinstance(rules, list) else ([rules] if rules is not None else []):
        freq = str((rule.get("FREQ") or ["?"])[0]).upper()
        if freq not in _PER_DAY:
            return float("inf")  # SECONDLY, MINUTELY or unknown
        try:
            interval = max(1, int((rule.get("INTERVAL") or [1])[0]))
        except (TypeError, ValueError):
            interval = 1
        if freq == "HOURLY":
            per_day = 24.0 / interval
        else:
            days = {"DAILY": 1, "WEEKLY": max(1, _n(rule, "BYDAY")),
                    "MONTHLY": max(1, _n(rule, "BYMONTHDAY"), 5 * _n(rule, "BYDAY")),
                    "YEARLY": max(1, _n(rule, "BYMONTH")) * max(1, _n(rule, "BYMONTHDAY"), 5 * _n(rule, "BYDAY"))
                    + _n(rule, "BYYEARDAY") + 7 * _n(rule, "BYWEEKNO")}[freq]
            per_day = min(1.0, _PER_DAY[freq] * days / interval) * max(1, _n(rule, "BYHOUR"))
        per_day *= max(1, _n(rule, "BYMINUTE")) * max(1, _n(rule, "BYSECOND"))
        stop = window_end
        until = rule.get("UNTIL")
        if until:
            try:
                stop = min(stop, _local(until[0])[0])
            except Exception:
                pass
        steps = max(0.0, (stop - first).total_seconds() / 86400 + 1) * per_day
        try:
            if rule.get("COUNT"):
                steps = min(steps, int(rule.get("COUNT")[0]))
        except (TypeError, ValueError):
            pass
        total += steps
    rdates = ev.get("RDATE")
    for r in rdates if isinstance(rdates, list) else ([rdates] if rdates is not None else []):
        total += len(getattr(r, "dts", []) or [])
    return total


def _event_span(ev):
    """(start, end) of a VEVENT's own DTSTART / DTEND as local naive datetimes, or None."""
    try:
        s_dt, all_day = _local(ev.get("DTSTART").dt)
    except Exception:
        return None
    try:
        e = ev.get("DTEND")
        e_dt = _local(e.dt)[0] if e is not None else s_dt + (timedelta(days=1) if all_day else timedelta(hours=1))
    except Exception:
        e_dt = s_dt
    return s_dt, max(e_dt, s_dt)


def _relevant(cal, start: datetime, end: datetime, deadline: float):
    """A copy of the calendar with only the VEVENTs that can show up in [start, end): recurring series cheap enough
    to expand (see _rule_steps; per series and summed), plus single events and moved occurrences near the window.
    Returns (calendar, series left out). Raises TooComplex past MAX_VEVENTS or the deadline."""
    import icalendar
    slack = timedelta(days=2)  # time zones and all-day events
    keep, series, skipped = [], [], 0
    for ev in cal.walk("VEVENT"):
        if time.monotonic() > deadline:
            raise TooComplex("This calendar takes too long to read")
        if ev.get("RRULE") is not None or ev.get("RDATE") is not None:
            steps = _rule_steps(ev, end + slack)
            if steps > MAX_SERIES_STEPS:
                skipped += 1
            else:
                series.append((steps, ev))
            continue
        span = _event_span(ev)
        moved_from = None
        if ev.get("RECURRENCE-ID") is not None:  # a moved occurrence: keep it if it moved into or out of the window
            try:
                moved_from = _local(ev.get("RECURRENCE-ID").dt)[0]
            except Exception:
                moved_from = None
        if (span and span[1] >= start - slack and span[0] <= end + slack) or \
                (moved_from and start - slack <= moved_from <= end + slack):
            keep.append(ev)
    total = 0.0
    for steps, ev in sorted(series, key=lambda x: x[0]):  # the cheapest series first, up to the overall budget
        total += steps
        if total > MAX_TOTAL_STEPS:
            skipped += 1
        else:
            keep.append(ev)
    if len(keep) > MAX_VEVENTS:
        raise TooComplex(f"This calendar has more than {MAX_VEVENTS} events around these dates")
    out = icalendar.Calendar()
    for k, v in cal.items():  # the VCALENDAR's own properties (CALSCALE, ...)
        out[k] = v
    for tz in cal.walk("VTIMEZONE"):
        out.add_component(tz)
    for ev in keep:
        out.add_component(ev)
    return out, skipped


_DAY = re.compile(rb"(\d{8})")


def _prop_day(line: bytes) -> str | None:
    """YYYYMMDD of a DTSTART / DTEND / RECURRENCE-ID line's value (after the last ':'), or None."""
    m = _DAY.match(line.rsplit(b":", 1)[-1].strip())
    return m.group(1).decode() if m else None


def _prefilter(data: bytes, start: datetime, end: datetime, deadline: float) -> bytes:
    """Drops, as text and before the slow full parse, every single (non-recurring) VEVENT that is clearly outside
    [start, end) (2 days of slack for time zones). Anything it can't read is kept. Raises TooComplex if more than
    MAX_VEVENTS are left, or past the deadline."""
    lo = (start - timedelta(days=2)).strftime("%Y%m%d")
    hi = (end + timedelta(days=2)).strftime("%Y%m%d")
    lines = re.sub(rb"\r?\n[ \t]", b"", data).splitlines()  # unfold
    out, block, depth, kept = [], None, 0, 0
    for i, line in enumerate(lines):
        if i % 2000 == 0 and time.monotonic() > deadline:
            raise TooComplex("This calendar takes too long to read")
        up = line[:16].upper()
        if block is None:
            if up.startswith(b"BEGIN:VEVENT"):
                block, depth = [line], 1
            else:
                out.append(line)
            continue
        block.append(line)
        if up.startswith(b"BEGIN:"):
            depth += 1
        elif up.startswith(b"END:"):
            depth -= 1
        if depth:
            continue
        props = {}
        for ln in block[1:]:
            key = re.split(rb"[;:]", ln, maxsplit=1)[0].upper()
            props.setdefault(key, ln)
        s_day = _prop_day(props[b"DTSTART"]) if b"DTSTART" in props else None
        e_day = _prop_day(props[b"DTEND"]) if b"DTEND" in props else s_day
        r_day = _prop_day(props[b"RECURRENCE-ID"]) if b"RECURRENCE-ID" in props else None
        recurring = b"RRULE" in props or b"RDATE" in props or b"DURATION" in props
        outside = (s_day is not None and e_day is not None and (e_day < lo or s_day > hi)
                   and (r_day is None or r_day < lo or r_day > hi))
        if recurring or not outside:
            kept += 1
            if kept > MAX_VEVENTS:
                raise TooComplex(f"This calendar has more than {MAX_VEVENTS} events around these dates")
            out.extend(block)
        block = None
    return b"\r\n".join(out) + b"\r\n"


def _parse(data: bytes, start: datetime, end: datetime, detail: str = DEFAULT_DETAIL,
           budget: float = PARSE_SECONDS) -> list[dict]:
    """Events between start and end, reduced to what `detail` allows (nothing else is kept).
    Bounded in time and memory, whatever the feed contains:
    - single events far from the window are dropped as text first (_prefilter), at most MAX_VEVENTS are parsed;
    - series too costly to expand are left out (SECONDLY / MINUTELY rules, dense HOURLY or BY* rules running since
      long ago; see _rule_steps), per series and in total;
    - each series' occurrences in the window are streamed (a generator, capped per series), the total is capped,
      and only the first MAX_EVENTS by start time are kept;
    - a time budget is checked throughout (TooComplex past it).
    (recurring_ical_events' after() re-queries every series for each time slice it tries, which is far slower
    with many series, so the series are walked one by one here.)"""
    import icalendar
    import recurring_ical_events
    deadline = time.monotonic() + budget
    detail = detail if detail in DETAILS else DEFAULT_DETAIL

    def tick():
        if time.monotonic() > deadline:
            raise TooComplex("This calendar takes too long to read")

    cal = icalendar.Calendar.from_ical(_prefilter(data, start, end, deadline))
    tick()
    cal, _ = _relevant(cal, start, end, deadline)
    query = recurring_ical_events.of(cal, skip_bad_series=True)
    found = []
    for series in query.series:
        tick()
        try:
            for occ in itertools.islice(series.between(start, end), MAX_EVENTS):
                found.append(occ)
                if len(found) % 200 == 0:
                    tick()
                if len(found) > MAX_OCCURRENCES:
                    raise TooComplex("This calendar has too many events in this period")
        except TooComplex:
            raise
        except Exception:  # one broken series doesn't hide the rest
            continue
    found.sort()
    out = []
    for occ in found:
        ev = occ.as_component(False)
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
        if s_dt >= end or e_dt < start:
            continue
        item = {"start": s_dt.isoformat(timespec="minutes"), "end": e_dt.isoformat(timespec="minutes"), "all_day": all_day}
        if detail == "busy":
            item.update(title="Busy", busy=True)
        else:
            item["title"] = str(ev.get("SUMMARY", "") or "(busy)")[:200]
            if detail == "full":
                item["location"] = str(ev.get("LOCATION", "") or "")[:200]
        out.append(item)
        if len(out) >= MAX_EVENTS:
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


async def _fetch_once(feed_id: str, detail: str, days: int) -> dict:
    """_fetch in a worker thread, shared: concurrent planner loads of a feed wait for the download + parse already
    running instead of starting their own (one in flight per feed and detail level). Stops waiting after the
    download timeouts + the parse budget; the parse also stops itself at its own deadline."""
    key = (feed_id, detail, days)
    fut = _inflight.get(key)
    if fut is None:
        fut = asyncio.ensure_future(asyncio.to_thread(_fetch, feed_id, detail, days))
        _inflight[key] = fut
        def finished(f):
            if _inflight.get(key) is f:
                _inflight.pop(key, None)
            if not f.cancelled():
                f.exception()  # retrieved here, so a result nobody waited for isn't logged as unhandled
        fut.add_done_callback(finished)
    try:
        return dict(await asyncio.wait_for(asyncio.shield(fut), FETCH_TIMEOUT * 2 + PARSE_SECONDS + 5))
    except asyncio.TimeoutError:
        raise TooComplex("The calendar took too long to load")


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
                c = {**await _fetch_once(feed["id"], feed["detail"], max(days, 14)), "days": max(days, 14)}
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
