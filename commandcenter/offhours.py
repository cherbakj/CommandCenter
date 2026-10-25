"""Off-hours work log: how long Joe is active at this PC outside work hours (config `offhours:`).

What counts as working: the Windows session is unlocked (privacy.windows_locked) and there was keyboard or mouse
input (GetLastInputInfo). A background job samples once a minute and marks the minute of the latest input as
active. Active minutes up to `idle_minutes` (default 10) apart are merged into one session, so a short pause to
read or think counts; a longer gap, or a sample that finds Windows locked, ends the session. Only the parts of a
session outside work hours (work_start - work_end on work_days, local time; Saturday and Sunday are off by
default) are stored, split at midnight. Times only: never which app, window, file or key.

Storage: SQLite table offhours_log (day TEXT PRIMARY KEY, sessions TEXT, updated TEXT). `sessions` is a
DPAPI-sealed (db.seal) JSON list of [start, end] minutes after midnight for that day (end exclusive, 1440 = the
next midnight); `day` stays in clear for lookups and retention (retention.offhours_days, default 400, enforced by
app.maintenance; listed under "Unencrypted metadata" in Privacy & Data). Work data: shown in Privacy mode.

Command Center starts at sign-in, but time while it isn't running (closed, restarting) or while the PC sleeps
simply isn't logged. Input that software injects (a "mouse jiggler", remote control) counts like real input.
"""
import asyncio
import ctypes
import ctypes.wintypes as wt
import json
import logging
import re
from datetime import date, datetime, time as dtime, timedelta

from fastapi import APIRouter

from . import config, db

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/offhours")

SAMPLE_SECONDS = 60
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULTS = {"enabled": True, "work_start": "08:00", "work_end": "17:00", "work_days": ["mon", "tue", "wed", "thu", "fri"],
            "idle_minutes": 10}
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_warned: set = set()


def _warn_once(key: str, text: str):
    if key not in _warned:
        _warned.add(key)
        log.warning(text)


def _hhmm(value, key: str) -> int:
    m = HHMM.match(str(value or "").strip())
    if not m:
        _warn_once(key, f"offhours.{key} should be a time like \"17:00\"; using {DEFAULTS[key]}")
        m = HHMM.match(DEFAULTS[key])
    return int(m.group(1)) * 60 + int(m.group(2))


def settings() -> dict:
    """offhours: from config.yaml, parsed defensively (a bad value means its default, logged once). Times are
    minutes after midnight; work_days are weekday numbers (Monday = 0)."""
    try:
        s = config.section("offhours", {}) or {}
    except Exception:
        s = {}
    if not isinstance(s, dict):
        _warn_once("section", "offhours: in config.yaml isn't a section; using the defaults")
        s = {}
    enabled = s.get("enabled", True)
    if not isinstance(enabled, bool):
        _warn_once("enabled", "offhours.enabled should be true or false; using true")
        enabled = True
    start, end = _hhmm(s.get("work_start", DEFAULTS["work_start"]), "work_start"), _hhmm(s.get("work_end", DEFAULTS["work_end"]), "work_end")
    if end <= start:
        _warn_once("order", "offhours.work_end must be after work_start; using 08:00 - 17:00")
        start, end = _hhmm(DEFAULTS["work_start"], "work_start"), _hhmm(DEFAULTS["work_end"], "work_end")
    raw_days = s.get("work_days", DEFAULTS["work_days"])
    days = set()
    if isinstance(raw_days, (list, tuple)):
        for d in raw_days:
            key = str(d).strip().lower()[:3]
            if key in DAY_NAMES:
                days.add(DAY_NAMES.index(key))
            else:
                _warn_once(f"day:{d}", f"offhours.work_days: {str(d)[:20]!r} isn't a weekday (mon ... sun); ignored")
    else:
        _warn_once("work_days", "offhours.work_days should be a list like [mon, tue, wed, thu, fri]; using Monday - Friday")
        days = {0, 1, 2, 3, 4}
    idle = s.get("idle_minutes", DEFAULTS["idle_minutes"])
    if isinstance(idle, bool) or not isinstance(idle, (int, float)) or not 1 <= idle <= 120:
        _warn_once("idle_minutes", "offhours.idle_minutes should be 1 - 120; using 10")
        idle = DEFAULTS["idle_minutes"]
    return {"enabled": enabled, "work_start": start, "work_end": end, "work_days": sorted(days), "idle_minutes": int(idle)}


# ---- bucketing (pure) ----------------------------------------------------------------------------------

def is_off(at: datetime, s: dict) -> bool:
    """True if `at` is outside work hours."""
    minute = at.hour * 60 + at.minute
    return at.weekday() not in s["work_days"] or not s["work_start"] <= minute < s["work_end"]


def off_segments(start: datetime, end: datetime, s: dict) -> list[tuple[datetime, datetime]]:
    """The parts of [start, end) outside work hours, split at midnight (so each piece belongs to one day)."""
    out = []
    day = start.date()
    while datetime.combine(day, dtime()) < end:
        d0 = datetime.combine(day, dtime())
        lo, hi = max(start, d0), min(end, d0 + timedelta(days=1))
        if lo < hi:
            if day.weekday() in s["work_days"]:
                ws, we = d0 + timedelta(minutes=s["work_start"]), d0 + timedelta(minutes=s["work_end"])
                pieces = ((lo, min(hi, ws)), (max(lo, we), hi))
            else:
                pieces = ((lo, hi),)
            out.extend((a, b) for a, b in pieces if a < b)
        day += timedelta(days=1)
    return out


def merge(intervals) -> list[list[int]]:
    """Sorted union of [start, end) minute intervals; touching ones join."""
    out = []
    for a, b in sorted((int(a), int(b)) for a, b in intervals if b > a):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


class Tracker:
    """The current activity session, in whole minutes (end exclusive). Feed it one sample a minute."""

    def __init__(self):
        self.start: datetime | None = None
        self.end: datetime | None = None

    def reset(self):
        self.start = self.end = None

    def sample(self, last_input: datetime | None, locked: bool, idle_minutes: int):
        """Returns (start, end) when the session grew or a new one started, else None. A locked session ends the
        current one (time behind the lock screen never counts, however short)."""
        if locked:
            self.reset()
            return None
        if last_input is None:
            return None
        t = last_input.replace(second=0, microsecond=0)
        if self.end is not None and t < self.end:
            return None  # this minute is already counted (or the clock went back)
        if self.end is not None and self.end <= t <= self.end + timedelta(minutes=idle_minutes):
            self.end = t + timedelta(minutes=1)
        else:
            self.start, self.end = t, t + timedelta(minutes=1)
        return self.start, self.end


# ---- storage ---------------------------------------------------------------------------------------------

_ready = False


def _tables():
    global _ready
    if _ready:
        return
    with db._lock:
        c = db._db()
        c.execute("CREATE TABLE IF NOT EXISTS offhours_log (day TEXT PRIMARY KEY, sessions TEXT NOT NULL, updated TEXT NOT NULL)")
        c.commit()
    _ready = True


def _load(raw) -> list[list[int]]:
    try:
        value = json.loads(db.unseal(raw)) if raw else []
        return merge(value) if isinstance(value, list) else []
    except (db.SealError, ValueError, TypeError):
        return []


def day_sessions(day: date) -> list[list[int]]:
    _tables()
    with db._lock:
        row = db._db().execute("SELECT sessions FROM offhours_log WHERE day = ?", (day.isoformat(),)).fetchone()
    return _load(row[0]) if row else []


def record(start: datetime, end: datetime, s: dict) -> int:
    """Adds the off-hours parts of [start, end) to the log. Returns how many day rows changed."""
    by_day: dict[date, list] = {}
    for a, b in off_segments(start, end, s):
        d0 = datetime.combine(a.date(), dtime())
        by_day.setdefault(a.date(), []).append([int((a - d0).total_seconds() // 60), int((b - d0).total_seconds() // 60)])
    if not by_day:
        return 0
    _tables()
    changed = 0
    with db._lock:
        c = db._db()
        for day, new in by_day.items():
            row = c.execute("SELECT sessions FROM offhours_log WHERE day = ?", (day.isoformat(),)).fetchone()
            old = _load(row[0]) if row else []
            merged = merge(old + new)
            if merged != old:
                c.execute("INSERT OR REPLACE INTO offhours_log (day, sessions, updated) VALUES (?, ?, ?)",
                          (day.isoformat(), db.seal(json.dumps(merged)), db.now()))
                changed += 1
        c.commit()
    return changed


def prune(days: float) -> int:
    """Retention (retention.offhours_days): deletes days older than that. Returns how many."""
    _tables()
    cutoff = (date.today() - timedelta(days=float(days))).isoformat()
    with db._lock:
        n = db._db().execute("DELETE FROM offhours_log WHERE day < ?", (cutoff,)).rowcount
        db._db().commit()
    return n


def logged_days() -> int:
    _tables()
    with db._lock:
        return db._db().execute("SELECT COUNT(*) FROM offhours_log").fetchone()[0]


# ---- sampling ----------------------------------------------------------------------------------------------

class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


_win = {}


def idle_seconds() -> float | None:
    """Seconds since the last keyboard / mouse input in this Windows session, or None if unknown. Both clocks
    are GetTickCount (milliseconds since boot, counting sleep), compared modulo 2^32 for the 49-day wrap."""
    try:
        if not _win:
            u, k = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32")
            u.GetLastInputInfo.argtypes = [ctypes.POINTER(_LASTINPUTINFO)]
            u.GetLastInputInfo.restype = wt.BOOL
            k.GetTickCount.restype = wt.DWORD
            _win.update(u=u, k=k)
        info = _LASTINPUTINFO(ctypes.sizeof(_LASTINPUTINFO), 0)
        if not _win["u"].GetLastInputInfo(ctypes.byref(info)):
            return None
        return ((_win["k"].GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:
        return None


def _windows_locked() -> bool:
    from . import privacy  # its helper: the input desktop isn't ours (Win+L, session switched / disconnected)
    return privacy.windows_locked()


_tracker = Tracker()
state = {"last_sample": None, "started": None, "active": False}


def sample_once(now: datetime | None = None, idle: float | None = None, locked: bool | None = None) -> bool:
    """One sample (the arguments are for tests). Returns True if the log changed."""
    s = settings()
    now = now or datetime.now()
    state["started"] = state["started"] or now.isoformat(timespec="seconds")
    state["last_sample"] = now.isoformat(timespec="seconds")
    if not s["enabled"]:
        _tracker.reset()
        state["active"] = False
        return False
    locked = _windows_locked() if locked is None else locked
    idle = idle_seconds() if idle is None else idle
    last_input = now - timedelta(seconds=idle) if idle is not None and idle >= 0 else None
    grown = _tracker.sample(last_input, locked, s["idle_minutes"])
    # "Working off-hours right now": the session reached this minute or the one before, outside work hours.
    state["active"] = bool(_tracker.end and now - _tracker.end < timedelta(minutes=2) and is_off(now, s))
    return bool(grown and record(*grown, s))


async def watch_loop():
    settings()  # validate the config once at start (bad values are logged and fall back to the defaults)
    while True:
        try:
            await asyncio.to_thread(sample_once)
        except Exception as e:  # never let the loop die
            log.warning("Off-hours log: %s", type(e).__name__)
        await asyncio.sleep(SAMPLE_SECONDS)


background = [watch_loop]


# ---- API ---------------------------------------------------------------------------------------------------

def _clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}" if minutes < 1440 else "24:00"


def summary(today: date | None = None) -> dict:
    """Today's off-hours and this week's (Monday - Sunday), per day with sessions."""
    s = settings()
    today = today or date.today()
    monday = today - timedelta(days=today.weekday())
    _tables()
    with db._lock:
        rows = db._db().execute("SELECT day, sessions FROM offhours_log WHERE day >= ? AND day <= ?",
                                (monday.isoformat(), (monday + timedelta(days=6)).isoformat())).fetchall()
    stored = {r["day"]: _load(r["sessions"]) for r in rows}
    days = []
    for i in range(7):
        d = monday + timedelta(days=i)
        sessions = stored.get(d.isoformat(), [])
        days.append({"day": d.isoformat(), "workday": d.weekday() in s["work_days"], "future": d > today,
                     "minutes": sum(b - a for a, b in sessions),
                     "sessions": [{"start": _clock(a), "end": _clock(b), "minutes": b - a} for a, b in sessions]})
    today_row = days[today.weekday()]
    return {"enabled": s["enabled"], "today": today_row, "week_minutes": sum(d["minutes"] for d in days), "days": days,
            "active": state["active"], "tracking_since": state["started"], "last_sample": state["last_sample"],
            "settings": {"work_start": _clock(s["work_start"]), "work_end": _clock(s["work_end"]),
                         "work_days": [DAY_NAMES[i] for i in s["work_days"]], "idle_minutes": s["idle_minutes"]}}


@router.get("")
async def get_summary():
    return await asyncio.to_thread(summary)
