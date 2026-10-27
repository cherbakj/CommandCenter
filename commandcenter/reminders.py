"""Personal reminders: pop-ups at set times of day for personal things Joe must do (the Personal tab, personal.js).

Storage is split so reminders can fire while the personal vault is locked without exposing what they say:
- The reminder itself (text, note, time, repeat rule, options) is one personal vault record (kind
  "personal-reminder"): AES-256-GCM under Joe's passphrase, like personal calendar entries. It is the source of
  truth; the schedule copy below is rebuilt from it whenever the vault is open.
- A schedule-only copy (kv "reminders:schedule": record id -> time, repeat rule, days / date, enabled, snooze
  allowed, created-since) and the firing state (kv "reminders:state": which days' occurrences fired, were done or
  are snoozed, and the open pop-ups) are work-data kv values, DPAPI-sealed by db.py. They hold times and ids, never
  text. Locking the vault (Lock, Windows lock, sleep, Privacy mode, restart) doesn't touch them, so schedules keep
  firing; resetting the vault deletes them with everything else (vault.register_store wipe).
- Done / fired history is kept retention.reminders_done_days (30 by default).

Firing (reminder_loop): checks every TICK seconds (sooner when the next occurrence is closer), on the local wall
clock, so a clock change, DST or a resume from sleep is picked up at the next check. Occurrences are local naive
times keyed by their day, so the DST repeat hour (fall back) can't fire one twice and the skipped hour (spring
forward) fires at the first time after it. An occurrence fires once; it stays an open pop-up until Done (or until
the same reminder's next occurrence fires). One more than LATE_SECONDS late (Command Center wasn't running, or the
PC slept) is shown as "missed", all of them in the same single pop-up; ones over STALE_HOURS old never fire.
Occurrences before the reminder was created / its time last changed never fire.

Pop-ups and the Windows toast: the toast always says only "Personal reminder" (whatever alerts.toast_detail
says), plays the alert sound when alerts.sound_for includes high (as meeting reminders do), and, like meeting
reminders (high priority), shows during focus mode too. Reminders don't go into the Alerts feed. In the desktop app
the window is restored and brought in front (front_hook), except while Command Center is locked or the screen may be
on show (share_risk: presentation / full-screen app, microphone or camera in use, duplicated display): then only
the toast, and the pop-up waits in the window. The in-app pop-up
(personal.js) shows the text only while the vault is open and Privacy mode is off; otherwise "Personal reminder at
3:00 PM" with Unlock. Done and Snooze work without unlocking (they need only the id and day).

Logs: counts and short id prefixes only.
"""
import asyncio
import ctypes
import logging
import re
import threading
import time
import winreg
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Body, HTTPException

from . import alerts, config, db, vault

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/reminders")

KIND = "personal-reminder"
SCHEDULE_KEY = "reminders:schedule"
STATE_KEY = "reminders:state"
MAX_REMINDERS = 200
MAX_TEXT = 200
MAX_NOTE = 2000
TICK = 20                 # seconds between checks (at most)
LATE_SECONDS = 120        # fired later than this = "missed" (Command Center wasn't running / the PC slept)
STALE_HOURS = 12          # older occurrences never fire
SNOOZE_CHOICES = (5, 10, 15, 30, 60)
REPEATS = ("weekdays", "daily", "days", "once")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")   # date.weekday() order
TIME = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
RECORD_ID = re.compile(r"[0-9a-f]{8,64}")
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")

_lock = threading.RLock()
_cache: dict = {}         # {"schedule": {...}, "state": {...}} once loaded


def settings() -> dict:
    s = config.section("personal", {}) or {}
    raw = s.get("snooze_minutes", 15) if isinstance(s, dict) else 15
    try:
        minutes = int(raw)
        if isinstance(raw, bool) or not 1 <= minutes <= 240:
            raise ValueError
    except (TypeError, ValueError):
        minutes = 15
    return {"snooze_minutes": minutes}


# ---- schedule rules (pure; unit-tested) ---------------------------------------------------------------

def occurs_on(item: dict, d: date) -> bool:
    rep = item.get("repeat")
    if rep == "daily":
        return True
    if rep == "weekdays":
        return d.weekday() < 5
    if rep == "days":
        return DAYS[d.weekday()] in (item.get("days") or [])
    if rep == "once":
        return item.get("date") == d.isoformat()
    return False


def at(item: dict, d: date) -> datetime:
    h, m = map(int, item["time"].split(":"))
    return datetime(d.year, d.month, d.day, h, m)


def _ts(dt: datetime) -> float:
    try:
        return dt.timestamp()  # local time -> epoch (DST-aware)
    except (OverflowError, OSError, ValueError):
        return 0.0


def next_occurrence(item: dict, now: datetime) -> datetime | None:
    """The first occurrence strictly after now (enabled or not), or None (a past one-off)."""
    if item.get("repeat") == "once":
        try:
            d = date.fromisoformat(item.get("date") or "")
        except ValueError:
            return None
        t = at(item, d)
        return t if t > now else None
    for i in range(0, 8):
        d = now.date() + timedelta(days=i)
        if occurs_on(item, d) and at(item, d) > now:
            return at(item, d)
    return None


def _since(item: dict) -> datetime:
    try:
        return datetime.fromisoformat(item.get("since") or "")
    except ValueError:
        return datetime.min


def tick(schedule: dict, state: dict, now: datetime, now_ts: float | None = None) -> tuple[bool, list[dict]]:
    """One check. Updates `state` in place; returns (changed, entries to announce: newly fired + snoozes that ended).
    state: {"fired": {rid: [day]}, "done": {rid: [day]}, "active": [{rid, day, at, missed, wake}]}."""
    now_ts = time.time() if now_ts is None else now_ts
    fired, done = state.setdefault("fired", {}), state.setdefault("done", {})
    active = state.setdefault("active", [])
    changed, announce = False, []
    # Open pop-ups of reminders that were deleted or turned off go away.
    keep = [a for a in active if a.get("rid") in schedule and schedule[a["rid"]].get("enabled", True)]
    if len(keep) != len(active):
        active[:] = keep
        changed = True
    stale = now - timedelta(hours=STALE_HOURS)
    for rid, item in schedule.items():
        if not item.get("enabled", True) or not TIME.fullmatch(str(item.get("time", ""))):
            continue
        since = _since(item)
        for d in (now.date() - timedelta(days=1), now.date()):
            if not occurs_on(item, d):
                continue
            key, when = d.isoformat(), at(item, d)
            if when > now or when < since or key in fired.get(rid, []) or key in done.get(rid, []):
                continue
            fired.setdefault(rid, []).append(key)
            changed = True
            if when < stale:
                continue  # too old to be useful: recorded, never shown
            # One open pop-up per reminder: a newer occurrence replaces an older one still open.
            active[:] = [a for a in active if a.get("rid") != rid]
            entry = {"rid": rid, "day": key, "at": when.isoformat(timespec="minutes"),
                     "missed": now_ts - _ts(when) > LATE_SECONDS, "wake": 0}
            active.append(entry)
            announce.append(entry)
    for a in active:
        if a.get("wake") and a["wake"] <= now_ts:
            a["wake"] = 0  # snooze over: it pops up again
            changed = True
            announce.append(a)
    return changed, announce


def prune(state: dict, today: date, days: float) -> int:
    """Drops fired / done days older than `days` (retention). Returns how many were dropped."""
    cutoff = (today - timedelta(days=float(days))).isoformat()
    n = 0
    for name in ("fired", "done"):
        table = state.get(name, {})
        for rid in list(table):
            keep = [d for d in table[rid] if d >= cutoff]
            n += len(table[rid]) - len(keep)
            if keep:
                table[rid] = keep
            else:
                del table[rid]
    return n


# ---- storage --------------------------------------------------------------------------------------

def _load():
    if "schedule" not in _cache:
        sched = db.kv_get(SCHEDULE_KEY, {}) or {}
        state = db.kv_get(STATE_KEY, {}) or {}
        _cache["schedule"] = sched if isinstance(sched, dict) else {}
        _cache["state"] = state if isinstance(state, dict) else {}
    return _cache["schedule"], _cache["state"]


def _save_schedule():
    db.kv_set(SCHEDULE_KEY, _cache["schedule"])


def _save_state():
    db.kv_set(STATE_KEY, _cache["state"])


def _sched_fields(rec: dict) -> dict:
    return {"time": rec.get("time"), "repeat": rec.get("repeat"), "days": list(rec.get("days") or []),
            "date": rec.get("date"), "enabled": bool(rec.get("enabled", True)), "snooze": bool(rec.get("snooze", True)),
            "since": rec.get("since") or ""}


def sync_from_vault() -> int:
    """Vault open: rebuilds the schedule copy from the vault records (the source of truth). Returns 0 (a vault
    store's "moved" count: nothing is moved)."""
    if not vault.available():
        return 0
    try:
        recs = vault.records(KIND)
    except vault.VaultLocked:
        return 0
    with _lock:
        sched, _ = _load()
        fresh = {r["id"]: _sched_fields(r) for r in recs}
        if fresh != sched:
            _cache["schedule"] = fresh
            _save_schedule()
            log.info("Personal reminders: schedule copy rebuilt from the vault (%d reminder(s))", len(fresh))
    return 0


def _wipe():
    """Vault reset ("Delete all personal data"): the schedule copy and state go too."""
    with _lock:
        with db._lock, vault.secure_delete() as c:
            c.execute("DELETE FROM kv WHERE key IN (?, ?)", (SCHEDULE_KEY, STATE_KEY))
            c.commit()
        _cache.clear()
    log.info("Personal reminders: schedule copy deleted (vault reset)")


# migrate() runs on every unlock: used here to refresh the schedule copy from the vault.
vault.register_store("personal reminders", migrate=sync_from_vault, wipe_legacy=_wipe)


def check_now(now: datetime | None = None) -> list[dict]:
    now = now or datetime.now()
    with _lock:
        sched, state = _load()
        changed, announce = tick(sched, state, now)
        if state.get("pruned") != now.date().isoformat():
            days = db.retention_settings().get("reminders_done_days", 30)
            n = prune(state, now.date(), days)
            state["pruned"] = now.date().isoformat()
            changed = True
            if n:
                log.info("Personal reminders: %d old done/fired day(s) pruned", n)
        if changed:
            _save_state()
    return announce


front_hook = None  # set by the desktop app: brings its window in front for due reminders (DesktopApp.raise_for_reminder)

_CONSENT = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore"
QUNS_NAMES = {2: "full-screen app", 3: "full-screen game", 4: "presentation mode", 7: "full-screen app"}


def _device_in_use(capability: str) -> bool:
    """An app is using the microphone / camera right now: Windows' privacy record for it has a start time and no stop
    time (the same records as the taskbar's in-use icon; packaged apps directly under the key, others under NonPackaged)."""
    def walk(path, depth):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
                try:
                    if winreg.QueryValueEx(k, "LastUsedTimeStart")[0] and not winreg.QueryValueEx(k, "LastUsedTimeStop")[0]:
                        return True
                except OSError:
                    pass
                return depth > 0 and any(walk(f"{path}\\{winreg.EnumKey(k, i)}", depth - 1)
                                         for i in range(winreg.QueryInfoKey(k)[0]))
        except OSError:
            return False
    return walk(f"{_CONSENT}\\{capability}", 2)


def _display_cloned() -> bool:
    """The screen is duplicated to another display (a projector or a meeting-room TV): topology "clone"."""
    user32 = ctypes.windll.user32
    paths, modes, topology = ctypes.c_uint32(), ctypes.c_uint32(), ctypes.c_uint32()
    QDC_DATABASE_CURRENT, CLONE = 0x4, 0x2
    if user32.GetDisplayConfigBufferSizes(QDC_DATABASE_CURRENT, ctypes.byref(paths), ctypes.byref(modes)):
        return False
    path_buf = (ctypes.c_byte * (72 * max(paths.value, 1)))()  # DISPLAYCONFIG_PATH_INFO, DISPLAYCONFIG_MODE_INFO
    mode_buf = (ctypes.c_byte * (64 * max(modes.value, 1)))()
    if user32.QueryDisplayConfig(QDC_DATABASE_CURRENT, ctypes.byref(paths), path_buf, ctypes.byref(modes), mode_buf,
                                 ctypes.byref(topology)):
        return False
    return topology.value == CLONE


def share_risk() -> str | None:
    """Why the screen might be on show to others right now (a call, a presentation, a duplicated display), or None.
    Called only when Command Center isn't the foreground window, so a full-screen state is another app's."""
    state = ctypes.c_int()
    if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0 and state.value in QUNS_NAMES:
        return QUNS_NAMES[state.value]
    if _device_in_use("microphone"):
        return "microphone in use"
    if _device_in_use("webcam"):
        return "camera in use"
    try:
        if _display_cloned():
            return "display duplicated"
    except Exception:
        pass
    return None


def _notify(entries: list[dict]):
    """One toast (and the alert sound) per batch. Generic text only: Windows keeps toast text unencrypted.
    Then the desktop window comes in front with the pop-up, unless the screen may be shared (front_hook decides)."""
    missed = sum(1 for e in entries if e.get("missed"))
    log.info("Personal reminders: %d due (%d missed)", len(entries), missed)
    s = config.section("alerts", {}) or {}
    if s.get("toast", True):
        alerts._toast("Personal reminder", "")
    if "high" in (s.get("sound_for") or ["high"]):
        alerts._sound()
    if front_hook:
        try:
            log.info("Personal reminders: window %s", front_hook())
        except Exception as e:
            log.warning("Personal reminders: couldn't bring the window forward (%s)", type(e).__name__)


def _seconds_to_next(now: datetime) -> float:
    with _lock:
        sched, state = _load()
        soonest = TICK
        for item in sched.values():
            if item.get("enabled", True) and TIME.fullmatch(str(item.get("time", ""))):
                n = next_occurrence(item, now)
                if n:
                    soonest = min(soonest, (n - now).total_seconds())
        for a in state.get("active", []):
            if a.get("wake"):
                soonest = min(soonest, a["wake"] - time.time())
    return max(1.0, soonest + 0.5)


_wake: dict = {}  # {"event": asyncio.Event}: set when a reminder is saved, so the loop re-plans its sleep


def _replan():
    ev = _wake.get("event")
    if ev:
        ev.set()


async def reminder_loop():
    _wake["event"] = ev = asyncio.Event()
    await asyncio.sleep(2)
    while True:
        try:
            announce = await asyncio.to_thread(check_now)
            if announce:
                await asyncio.to_thread(_notify, announce)
        except Exception as e:  # never let the loop die
            log.warning("Personal reminders check failed (%s)", type(e).__name__)
        try:
            wait = await asyncio.to_thread(_seconds_to_next, datetime.now())
        except Exception:
            wait = TICK
        try:
            await asyncio.wait_for(ev.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
        ev.clear()


background = [reminder_loop]


# ---- views ------------------------------------------------------------------------------------------

def _personal_state() -> dict:
    st = vault.status()
    return {k: st[k] for k in ("set_up", "unlocked", "privacy", "available")}


def _repeat_label(item: dict) -> str:
    rep = item.get("repeat")
    if rep == "weekdays":
        return "Weekdays"
    if rep == "daily":
        return "Every day"
    if rep == "once":
        return "Once"
    days = [d for d in DAYS if d in (item.get("days") or [])]
    return ", ".join(d.capitalize() for d in days) or "No days"


def _active_now() -> list[dict]:
    """Open pop-ups that aren't snoozed right now."""
    now_ts = time.time()
    with _lock:
        _, state = _load()
        return [dict(a) for a in state.get("active", []) if not a.get("wake") or a["wake"] <= now_ts]


def active() -> dict:
    """The open pop-ups. Text only while personal data is available."""
    texts = {}
    if vault.available():
        try:
            texts = {r["id"]: r for r in vault.records(KIND)}
        except vault.VaultLocked:
            texts = {}
    with _lock:
        sched, state = _load()
        wakes = [a["wake"] for a in state.get("active", []) if a.get("wake")]
    items = []
    for a in sorted(_active_now(), key=lambda a: a["at"]):
        rec = texts.get(a["rid"])
        items.append({"id": a["rid"], "day": a["day"], "at": a["at"], "missed": bool(a.get("missed")),
                      "snooze": bool(sched.get(a["rid"], {}).get("snooze", True)),
                      **({"text": rec.get("text", ""), "note": rec.get("note", "")} if rec else {})})
    return {"items": items, "available": vault.available(), "privacy": vault.privacy_on(),
            "next_wake": min(wakes) if wakes else None, "snooze_minutes": settings()["snooze_minutes"]}


def overview(now: datetime | None = None) -> dict:
    """The Personal tab. Nothing personal while the vault is locked or Privacy mode is on (only, when locked and
    Privacy mode is off, how many reminders today and how many are done)."""
    now = now or datetime.now()
    ps = _personal_state()
    today = now.date()
    out = {"state": ps, "now": now.isoformat(timespec="seconds"), "today": [], "reminders": [], "upcoming": [],
           "recent": [], "snooze_minutes": settings()["snooze_minutes"]}
    with _lock:
        sched, state = _load()
        done = {rid: set(v) for rid, v in state.get("done", {}).items()}
        act = {a["rid"]: a for a in state.get("active", [])}
    if not ps["available"]:
        if not ps["privacy"]:
            todays = [rid for rid, it in sched.items() if it.get("enabled", True) and occurs_on(it, today)]
            out["counts"] = {"today": len(todays), "done": sum(1 for rid in todays if today.isoformat() in done.get(rid, ()))}
        return out
    sync_from_vault()
    try:
        recs = vault.records(KIND)
    except vault.VaultLocked:
        return out
    key = today.isoformat()
    for r in recs:
        item = _sched_fields(r)
        nxt = next_occurrence(item, now)
        row = {"id": r["id"], "text": r.get("text", ""), "note": r.get("note", ""), "time": item["time"],
               "repeat": item["repeat"], "days": item["days"], "date": item["date"], "enabled": item["enabled"],
               "snooze": item["snooze"], "repeat_label": _repeat_label(item),
               "next": nxt.isoformat(timespec="minutes") if nxt and item["enabled"] else None}
        out["reminders"].append(row)
        if item["enabled"] and occurs_on(item, today):
            when = at(item, today)
            a = act.get(r["id"])
            is_done = key in done.get(r["id"], ())
            out["today"].append({**row, "at": when.isoformat(timespec="minutes"), "done": is_done,
                                 "open": bool(a and a["day"] == key), "wake": (a or {}).get("wake") or None,
                                 "overdue": not is_done and when <= now})
    out["today"].sort(key=lambda x: x["at"])
    out["reminders"].sort(key=lambda x: (not x["enabled"], x["time"] or "", x["text"].lower()))
    # Upcoming personal calendar entries and recently saved ones (quick-capture notes are personal entries too),
    # read from the planner's vault records: nothing is copied.
    from . import planner
    out["upcoming"] = planner.personal_between(today, today + timedelta(days=6))
    try:
        entries = vault.records(planner.KIND)
    except vault.VaultLocked:
        entries = []
    cutoff = (now - timedelta(days=14)).isoformat(timespec="seconds")
    recent = [e for e in entries if str(e.get("updated") or "") >= cutoff]
    recent.sort(key=lambda e: str(e.get("updated")), reverse=True)
    out["recent"] = [{"id": e["id"], "day": e.get("day"), "title": e.get("title", ""), "all_day": bool(e.get("all_day")),
                      "start": e.get("start"), "updated": e.get("updated")} for e in recent[:10]]
    return out


def inventory(r: dict, dpapi: str, vs: dict, counts: dict | None) -> list[dict]:
    """Privacy & Data entries (privacy.py)."""
    vault_state = ("Not set up" if not vs["set_up"] else "Privacy mode on" if vs["privacy"]
                   else "Unlocked" if vs["unlocked"] else "Locked")
    with _lock:
        sched, _ = _load()
    days = r.get("reminders_done_days", 30)
    return [
        {"id": "personal-reminders", "name": "Personal reminders", "category": "Personal",
         "accesses": "Only what you type into reminders (Personal tab).",
         "stored": "Each reminder (text, note, time, repeat rule) as one encrypted record in SQLite.",
         "encryption": "Personal vault: AES-256-GCM, key from your passphrase (not stored anywhere)",
         "retention": "Until you delete them", "connected": vs["available"],
         "state": (f"{counts.get(KIND, 0)} reminder(s) · {vault_state}" if counts is not None else vault_state)},
        {"id": "reminder-schedule", "name": "Reminder schedule (times only)", "category": "Personal",
         "accesses": "A copy of each reminder's time and repeat rule, so reminders pop up while the vault is locked.",
         "stored": "Record id, time, repeat days / date, on/off, snooze allowed; which days were done, fired or are "
                   "snoozed. No reminder text or notes.",
         "encryption": dpapi,
         "retention": f"Schedule: until the reminder is deleted; done / fired days {int(days)} days",
         "connected": None, "state": f"{len(sched)} scheduled"},
    ]


# ---- validation -------------------------------------------------------------------------------------

def _clean(text, note, time_, repeat, days, date_, enabled, snooze) -> dict:
    text = (text or "").strip() if isinstance(text, str) else ""
    note = note if isinstance(note, str) else ""
    if not text:
        raise HTTPException(400, "Say what to remind you of")
    if len(text) > MAX_TEXT or len(note) > MAX_NOTE:
        raise HTTPException(400, f"Reminder up to {MAX_TEXT} characters, note up to {MAX_NOTE}")
    if not isinstance(time_, str) or not TIME.fullmatch(time_):
        raise HTTPException(400, "Pick a time, like 15:00")
    if repeat not in REPEATS:
        raise HTTPException(400, "Pick how often it repeats")
    days = [d for d in DAYS if isinstance(days, list) and d in days]
    if repeat == "days" and not days:
        raise HTTPException(400, "Pick at least one day")
    if repeat == "once":
        try:
            date.fromisoformat(date_ or "")
        except (TypeError, ValueError):
            raise HTTPException(400, "Pick a date")
    else:
        date_ = None
    return {"text": text, "note": note, "time": time_, "repeat": repeat, "days": days if repeat == "days" else [],
            "date": date_, "enabled": bool(enabled), "snooze": bool(snooze)}


def save(rid, values: dict) -> str:
    vault.require_available()
    now = datetime.now().isoformat(timespec="seconds")
    old = None
    if rid is not None:
        if not isinstance(rid, str) or not RECORD_ID.fullmatch(rid) or (old := vault.get(KIND, rid)) is None:
            raise HTTPException(404, "That reminder no longer exists")
    elif len(vault.records(KIND)) >= MAX_REMINDERS:
        raise HTTPException(400, f"Up to {MAX_REMINDERS} reminders")
    # Occurrences before now never fire for a new reminder, or for one whose time / repeat rule changed or that was
    # turned off and on again (times that passed while it was off aren't "missed").
    timing = lambda r: [r.get(k) for k in ("time", "repeat", "days", "date")] + [bool(r.get("enabled", True))]
    same = old is not None and timing(old) == timing(values)
    values = {**values, "since": old.get("since") if same else now, "created": (old or {}).get("created") or now, "updated": now}
    with _lock:
        new_id = vault.put(KIND, values, rid)
        sched, state = _load()
        sched[new_id] = _sched_fields(values)
        _save_schedule()
        if not same and old is not None:
            # The old time's pop-up closes, and today's new time can still fire (unless today is already done).
            state["active"] = [a for a in state.get("active", []) if a.get("rid") != new_id]
            today = date.today().isoformat()
            if new_id in state.get("fired", {}):
                state["fired"][new_id] = [d for d in state["fired"][new_id] if d < today]
            _save_state()
    log.info("Personal reminder %s saved", new_id[:6])
    return new_id


def delete(rid) -> bool:
    vault.require_available()
    if not vault.delete(KIND, rid):
        return False
    with _lock:
        sched, state = _load()
        sched.pop(rid, None)
        _save_schedule()
        for name in ("fired", "done"):
            state.get(name, {}).pop(rid, None)
        state["active"] = [a for a in state.get("active", []) if a.get("rid") != rid]
        _save_state()
    log.info("Personal reminder %s deleted", rid[:6])
    return True


def _known(rid, day) -> dict:
    if not isinstance(rid, str) or not RECORD_ID.fullmatch(rid) or not isinstance(day, str) or not DAY.fullmatch(day):
        raise HTTPException(400, "Unknown reminder")
    sched, _ = _load()
    if rid not in sched:
        raise HTTPException(404, "That reminder no longer exists")
    return sched[rid]


def set_done(rid, day, is_done: bool):
    """Works while the vault is locked (ids and days only). Undo makes the occurrence due again."""
    with _lock:
        _known(rid, day)
        _, state = _load()
        done = state.setdefault("done", {}).setdefault(rid, [])
        if is_done:
            if day not in done:
                done.append(day)
            state["active"] = [a for a in state.get("active", []) if not (a.get("rid") == rid and a.get("day") == day)]
        else:
            if day in done:
                done.remove(day)
            fired = state.setdefault("fired", {}).get(rid, [])
            if day in fired:
                fired.remove(day)  # due again: the next check pops it up if its time has passed
        _save_state()
    return check_now() if not is_done else []


def snooze(rid, day, minutes):
    with _lock:
        item = _known(rid, day)
        if not item.get("snooze", True):
            raise HTTPException(400, "Snooze is off for this reminder")
        try:
            minutes = int(minutes) if minutes is not None else settings()["snooze_minutes"]
        except (TypeError, ValueError):
            raise HTTPException(400, "Snooze for how long?")
        if minutes not in SNOOZE_CHOICES and minutes != settings()["snooze_minutes"]:
            raise HTTPException(400, "Snooze for how long?")
        _, state = _load()
        entry = next((a for a in state.get("active", []) if a.get("rid") == rid and a.get("day") == day), None)
        if entry is None:
            raise HTTPException(404, "That reminder isn't showing")
        entry["wake"] = time.time() + minutes * 60
        _save_state()
    return {"ok": True, "wake": entry["wake"], "minutes": minutes}


# ---- routes ---------------------------------------------------------------------------------------

@router.get("")
async def get_overview():
    return await asyncio.to_thread(overview)


@router.get("/active")
async def get_active():
    return await asyncio.to_thread(active)


@router.post("")
async def save_reminder(text: str = Body(...), time: str = Body(...), repeat: str = Body("weekdays"),
                        days: list[str] = Body([]), date: str | None = Body(None), note: str = Body(""),
                        enabled: bool = Body(True), snooze: bool = Body(True), id: str | None = Body(None)):
    values = _clean(text, note, time, repeat, days, date, enabled, snooze)
    try:
        new_id = await asyncio.to_thread(save, id, values)
    except vault.VaultLocked:
        raise HTTPException(423, "Your personal vault is locked. Unlock it first")
    _replan()  # the loop wakes at the new reminder's time
    return {"ok": True, "id": new_id}


@router.post("/delete")
async def delete_reminder(id: str = Body(..., embed=True)):
    try:
        ok = await asyncio.to_thread(delete, id)
    except vault.VaultLocked:
        raise HTTPException(423, "Your personal vault is locked. Unlock it first")
    if not ok:
        raise HTTPException(404, "That reminder no longer exists")
    return {"ok": True}


@router.post("/done")
async def done_reminder(id: str = Body(...), day: str = Body(...), done: bool = Body(True)):
    await asyncio.to_thread(set_done, id, day, done)
    _replan()
    return {"ok": True}


@router.post("/snooze")
async def snooze_reminder(id: str = Body(...), day: str = Body(...), minutes: int | None = Body(None)):
    result = await asyncio.to_thread(snooze, id, day, minutes)
    _replan()  # wakes again when the snooze ends
    return result
