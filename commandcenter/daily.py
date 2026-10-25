"""Workday helpers on Today: Top 3 priorities, the uncommitted-work reminder, the "Yesterday" recap, and
private meeting notes with action items.

Privacy: Top 3 items and meeting notes live only in the local SQLite database (inside the app's private data
folder), their text encrypted with Windows DPAPI for the current user, the same way as personal calendar
entries (planner.py). They are never written to Outlook, alerts or the log; the briefing and wrap-up alerts
mention counts only. A meeting's notes are keyed by a hash of its Outlook EntryID + start time (recurring
meetings share one EntryID), so the database shows neither the meeting nor its notes in plain text.
An action item becomes an Outlook or Asana task only when Joe clicks the button for it.

Git checks run through projects._git (GIT_SAFE: no hooks, fsmonitor or untracked cache; read-only).
"""
import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import date, datetime, timedelta

import pywintypes
import win32crypt
from fastapi import APIRouter, Body, HTTPException

from . import alerts, config, db, projects
from .connectors import asana, outlook

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/daily")

TOP3_LABEL = "CommandCenter Top 3"
NOTES_LABEL = "CommandCenter meeting notes"
TOP3_KEEP_DAYS = 120
MAX_ITEM = 200          # a Top 3 line
MAX_ACTION = 300        # an action item
MAX_NOTES = 20000       # a meeting's notes
MAX_ACTIONS = 40
MAX_SUBJECT = 300
OUTLOOK_ID = re.compile(r"[0-9A-Fa-f]{16,1024}")
ASANA_ID = re.compile(r"\d{1,30}")
ASANA_URL = re.compile(r"https://app\.asana\.com/[^\s\"'<>]{1,500}")
START_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
PR_MESSAGE_CLASS = "http://schemas.microsoft.com/mapi/proptag/0x001A001F"
OL_RESPONSE_DECLINED = 4
OL_MEETING_CANCELED = (5, 7)
OL_FREE = 0


def settings() -> dict:
    s = config.section("daily", {})
    return s if isinstance(s, dict) else {}


# ---- private storage (DPAPI) ----------------------------------------------------------------

_ready = False


def _tables():
    global _ready
    if _ready:
        return
    with db._lock:
        c = db._db()
        c.execute("CREATE TABLE IF NOT EXISTS daily_top3 (day TEXT PRIMARY KEY, secret BLOB NOT NULL, updated TEXT NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS meeting_notes ("
                  " key TEXT PRIMARY KEY, start TEXT NOT NULL, secret BLOB NOT NULL, updated TEXT NOT NULL)")
        c.execute("DELETE FROM daily_top3 WHERE day < ?", ((date.today() - timedelta(days=TOP3_KEEP_DAYS)).isoformat(),))
        c.commit()
    _ready = True


def _seal(payload: dict, label: str) -> bytes:
    return win32crypt.CryptProtectData(json.dumps(payload).encode("utf-8"), label, None, None, None, 0)


def _open(blob: bytes) -> dict:
    return json.loads(win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1].decode("utf-8"))


def _flat(value, limit) -> str:
    """Text that came from Outlook (a subject, a task title): control characters become spaces, then trimmed."""
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()[:limit]


def _one_line(value, limit, what) -> str:
    text = str(value or "").strip()
    if len(text) > limit or any(ord(c) < 32 for c in text):
        raise HTTPException(400, f"{what}: one line, up to {limit} characters")
    return text


# ---- Top 3 ------------------------------------------------------------------------------------

def _clean_link(link):
    """An optional pointer to the task or email a priority is about. Only ids of the expected shape are kept."""
    if not link:
        return None
    if not isinstance(link, dict) or link.get("kind") not in ("task", "mail") or not isinstance(link.get("id"), str):
        raise HTTPException(400, "That link isn't a task or an email")
    kind, source, item_id = link["kind"], link.get("source") or "outlook", link["id"]
    if kind == "mail":
        source = "outlook"
    if source == "asana" and kind == "task":
        ok = ASANA_ID.fullmatch(item_id)
    elif source == "outlook":
        ok = OUTLOOK_ID.fullmatch(item_id)
    else:
        ok = False
    if not ok:
        raise HTTPException(400, "That link isn't a task or an email")
    out = {"kind": kind, "source": source, "id": item_id,
           "label": _flat(link.get("label"), MAX_SUBJECT)}
    url = link.get("url")
    if source == "asana" and isinstance(url, str) and ASANA_URL.fullmatch(url):
        out["url"] = url
    return out


def _clean_items(items) -> list:
    if not isinstance(items, list) or len(items) > 3:
        raise HTTPException(400, "Up to three priorities")
    out = []
    for it in items:
        if it is None or (isinstance(it, dict) and not str(it.get("text") or "").strip()):
            out.append(None)
            continue
        if not isinstance(it, dict):
            raise HTTPException(400, "Each priority needs some text")
        carried = it.get("from")
        if carried is not None and not (isinstance(carried, str) and DAY_RE.fullmatch(carried)):
            carried = None
        out.append({"text": _one_line(it.get("text"), MAX_ITEM, "Each priority"), "done": it.get("done") is True,
                    "link": _clean_link(it.get("link")), "from": carried})
    return out + [None] * (3 - len(out))


def _row_items(secret) -> list:
    try:
        items = _open(secret).get("items") or []
    except Exception:
        return [{"text": "(can't decrypt: saved under another Windows account)", "done": False, "link": None,
                 "from": None, "locked": True}, None, None]
    items = [i if isinstance(i, dict) and i.get("text") else None for i in items[:3]]
    return items + [None] * (3 - len(items))


def top3_get(day: str) -> list:
    _tables()
    with db._lock:
        row = db._db().execute("SELECT secret FROM daily_top3 WHERE day = ?", (day,)).fetchone()
    return _row_items(row["secret"]) if row else [None, None, None]


def top3_set(day: str, items: list):
    _tables()
    with db._lock:
        if not any(items):
            db._db().execute("DELETE FROM daily_top3 WHERE day = ?", (day,))
        else:
            db._db().execute("INSERT OR REPLACE INTO daily_top3 (day, secret, updated) VALUES (?, ?, ?)",
                             (day, _seal({"items": items}, TOP3_LABEL), db.now()))
        db._db().commit()


def previous_workday(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _carry_source(today: date):
    """The most recent earlier day (within a week) that had a Top 3, and its unfinished items."""
    _tables()
    with db._lock:
        row = db._db().execute("SELECT day, secret FROM daily_top3 WHERE day < ? AND day >= ? ORDER BY day DESC LIMIT 1",
                               (today.isoformat(), (today - timedelta(days=7)).isoformat())).fetchone()
    if not row:
        return None, []
    return row["day"], [i for i in _row_items(row["secret"]) if i and not i.get("done") and not i.get("locked")]


def _carry_offer(today: date, items: list):
    """Unfinished items from the last workday, offered once per day while there's a free slot."""
    if db.kv_get("daily:carry_handled") == today.isoformat() or all(items):
        return None
    day, unfinished = _carry_source(today)
    current = {i["text"].casefold() for i in items if i}
    unfinished = [i for i in unfinished if i["text"].casefold() not in current]
    if not unfinished:
        return None
    return {"from": day, "items": [{"text": i["text"], "link": i.get("link")} for i in unfinished]}


def _top3_state() -> dict:
    today = date.today()
    items = top3_get(today.isoformat())
    return {"day": today.isoformat(), "items": items, "carry": _carry_offer(today, items)}


def briefing_note() -> str:
    """For the morning briefing: counts only (the items themselves stay encrypted, out of the alerts feed)."""
    items = [i for i in top3_get(date.today().isoformat()) if i]
    if not items:
        return "Set your Top 3 for today"
    return f"Top 3: {len(items)} priorit{'y' if len(items) == 1 else 'ies'} set"


def wrapup_note() -> str:
    items = [i for i in top3_get(date.today().isoformat()) if i]
    if not items:
        return ""
    left = [i for i in items if not i.get("done")]
    return f"Top 3: {len(left)} of {len(items)} unfinished" if left else "Top 3: all done"


@router.get("/top3")
async def get_top3():
    return await asyncio.to_thread(_top3_state)


@router.post("/top3")
async def save_top3(items: list = Body(..., embed=True)):
    clean = _clean_items(items)
    await asyncio.to_thread(top3_set, date.today().isoformat(), clean)
    return await asyncio.to_thread(_top3_state)


@router.post("/top3/carry")
async def carry_top3(accept: bool = Body(..., embed=True)):
    def run():
        today = date.today()
        items = top3_get(today.isoformat())
        if accept:
            offer = _carry_offer(today, items)
            if offer:
                todo = list(offer["items"])
                items = [i if i else ({**todo.pop(0), "done": False, "from": offer["from"]} if todo else None) for i in items]
                top3_set(today.isoformat(), _clean_items(items))
        db.kv_set("daily:carry_handled", today.isoformat())
        return _top3_state()
    return await asyncio.to_thread(run)


# ---- uncommitted work ---------------------------------------------------------------------------

_uncommitted: dict = {}
UNCOMMITTED_CACHE_SECONDS = 120


def _ahead(path) -> int | None:
    """Commits on the current branch that aren't on its upstream; None when there is no upstream."""
    out = projects._git(path, "rev-list", "--count", "@{u}..HEAD")
    return int(out) if out.isdigit() else None


def uncommitted_projects() -> list[dict]:
    out = []
    for p in projects.list_projects():
        if not p.get("git"):
            continue
        changed, ahead = p.get("changed_files"), _ahead(p["path"])
        if (changed or 0) > 0 or (ahead or 0) > 0:
            out.append({"name": p["name"], "path": p["path"], "branch": p.get("branch") or "",
                        "changed_files": changed, "ahead": ahead})
    return out


def _after_time(now: datetime) -> bool:
    at = str(settings().get("uncommitted_after") or "15:00")
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", at.strip())
    hour, minute = (int(m.group(1)), int(m.group(2))) if m else (15, 0)
    return now.weekday() < 5 and (now.hour, now.minute) >= (hour, minute)


def _describe(p: dict) -> str:
    bits = []
    if p["changed_files"]:
        bits.append(f"{p['changed_files']} changed file{'s' if p['changed_files'] != 1 else ''}")
    if p["ahead"]:
        bits.append(f"{p['ahead']} commit{'s' if p['ahead'] != 1 else ''} not pushed")
    return f"{p['name']}: {', '.join(bits)}"


@router.get("/uncommitted")
async def get_uncommitted(refresh: bool = False):
    if refresh or time.time() - _uncommitted.get("at", 0) > UNCOMMITTED_CACHE_SECONDS:
        _uncommitted["projects"] = await asyncio.to_thread(uncommitted_projects)
        _uncommitted["at"] = time.time()
        _uncommitted["checked"] = db.now()
    return {"show": _after_time(datetime.now()), "after": str(settings().get("uncommitted_after") or "15:00"),
            "projects": _uncommitted["projects"], "checked": _uncommitted["checked"]}


async def wrapup_uncommitted():
    """At wrap-up: one high-priority alert listing projects with uncommitted or unpushed work."""
    found = await asyncio.to_thread(uncommitted_projects)
    _uncommitted.update(projects=found, at=time.time(), checked=db.now())
    if not found:
        return
    n = len(found)
    alerts.raise_alert("Projects", f"{n} project{'s' if n != 1 else ''} with uncommitted work",
                       " · ".join(_describe(p) for p in found[:6]) + (f" · +{n - 6} more" if n > 6 else ""),
                       "high", {"type": "uncommitted"})


# ---- yesterday recap ------------------------------------------------------------------------------

def _git_commits(path, day: date) -> list[dict]:
    who = projects._git(path, "config", "user.email") or projects._git(path, "config", "user.name")
    if not who:
        return []
    nxt = day + timedelta(days=1)
    # -F: the author is matched as plain text. Every value is one argv entry (no shell).
    out = projects._git(path, "log", "--branches", "--remotes", "--no-merges", "-F", f"--author={who}",
                        f"--since={day.isoformat()} 00:00:00", f"--until={nxt.isoformat()} 00:00:00",
                        "-n", "60", "--format=%h%x1f%s%x1f%aI")
    commits = []
    for line in out.splitlines():
        sha, _, rest = line.partition("\x1f")
        subject, _, when = rest.partition("\x1f")
        if sha:
            commits.append({"sha": sha[:12], "subject": subject[:300], "at": when[:19]})
    return commits


def _recap_commits(day: date) -> list[dict]:
    out = []
    for p in projects.list_projects():
        if p.get("git"):
            commits = _git_commits(p["path"], day)
            if commits:
                out.append({"project": p["name"], "path": p["path"], "commits": commits})
    return out


def _fmt(d: datetime) -> str:
    return d.strftime("%m/%d/%Y %I:%M %p")


def _day_meetings(day: date) -> list[dict]:
    """Meetings that day you didn't decline (Outlook thread)."""
    start = datetime(day.year, day.month, day.day)
    end = start + timedelta(days=1)
    items = outlook._ns().GetDefaultFolder(outlook.OL_FOLDER_CALENDAR).Items
    items.Sort("[Start]")
    items.IncludeRecurrences = True
    found = items.Restrict(f"[Start] < '{_fmt(end)}' AND [End] > '{_fmt(start)}'")
    out, item = [], found.GetFirst()
    while item is not None and len(out) < 60:
        try:
            if (not item.AllDayEvent and item.BusyStatus != OL_FREE and item.ResponseStatus != OL_RESPONSE_DECLINED
                    and item.MeetingStatus not in OL_MEETING_CANCELED):
                s = outlook._local_time(item.Start)
                if s.date() == day:
                    out.append({"id": item.EntryID, "subject": item.Subject or "(no subject)",
                                "start": s.isoformat(timespec="minutes"),
                                "end": outlook._local_time(item.End).isoformat(timespec="minutes")})
        except pywintypes.com_error:
            pass
        item = found.GetNext()
    return out


def _sent_on(day: date) -> list[dict]:
    """Subjects and recipients of mail you sent that day (no bodies). The query window is padded a day each
    way and the rows are filtered on local time, so time-zone handling in the query can't drop any."""
    lo = datetime(day.year, day.month, day.day) - timedelta(days=1)
    hi = lo + timedelta(days=3)
    where = (f'@SQL="urn:schemas:httpmail:date" >= \'{_fmt(lo)}\' AND "urn:schemas:httpmail:date" < \'{_fmt(hi)}\''
             f' AND "{PR_MESSAGE_CLASS}" LIKE \'IPM.Note%\'')
    rows = outlook._mail_rows(outlook.OL_FOLDER_SENT, where, "SentOn", 500)
    return [{"subject": r["subject"], "to": r["to"][:200], "at": r["received"]}
            for r in rows if (r["received"] or "")[:10] == day.isoformat()]


def _tasks_completed(day: date) -> list[dict]:
    rows = outlook._table_rows(outlook.OL_FOLDER_TASKS, "[Complete] = True", ["EntryID", "Subject", "DateCompleted"], 5000)
    return [{"source": "outlook", "subject": subject or "(untitled)"}
            for _, subject, done in rows if done is not None and outlook._date_or_none(done) == day.isoformat()]


def _asana_completed(day: date) -> list[dict]:
    since = datetime(day.year, day.month, day.day).astimezone()
    rows = asana._request("GET", "/tasks", {
        "assignee": "me", "workspace": asana._workspace(), "completed_since": since.isoformat(), "limit": 100,
        "opt_fields": "name,completed,completed_at"}) or []
    out = []
    for t in rows:
        try:
            done_at = datetime.fromisoformat(str(t.get("completed_at") or "").replace("Z", "+00:00")).astimezone()
        except ValueError:
            continue
        if t.get("completed") and done_at.date() == day:
            out.append({"source": "asana", "subject": t.get("name") or "(untitled)"})
    return out


def _pulse_closed(day: date):
    try:
        from . import pulse
    except ImportError:
        return None
    if not pulse.is_configured():
        return None
    me = sorted(pulse._me(pulse.settings()))
    if not me:
        return None
    lo = datetime(day.year, day.month, day.day).astimezone()
    nxt = day + timedelta(days=1)
    hi = datetime(nxt.year, nxt.month, nxt.day).astimezone()
    rows = pulse.query(
        "select ticket_no, sub_section, note from public.data_reviews where resolved and resolved_at >= %s"
        " and resolved_at < %s and lower(coalesce(resolved_by, '')) = any(%s) order by resolved_at limit 100",
        [lo, hi, me])
    return [{"ticket_no": r.get("ticket_no"), "title": (r.get("sub_section") or r.get("note") or "(no headline)")[:200],
             "url": pulse.ticket_url(r.get("ticket_no"))} for r in rows]


def _err(e: Exception) -> str:
    return str(e).splitlines()[0][:200] if str(e) else type(e).__name__


_recap_cache: dict = {}


@router.get("/recap")
async def recap(day: str | None = None, refresh: bool = False):
    today = date.today()
    if day:
        if not DAY_RE.fullmatch(day):
            raise HTTPException(400, "Pick a valid date")
        try:
            d = date.fromisoformat(day)
        except ValueError:
            raise HTTPException(400, "Pick a valid date")
        if not today - timedelta(days=90) <= d <= today:
            raise HTTPException(400, "Pick a day in the last 90 days")
    else:
        d = previous_workday(today)
    cached = _recap_cache.get(d.isoformat())
    if cached and not refresh and time.time() - cached[0] < 120:
        return cached[1]

    errors = {}

    async def section(name, coro, default):
        try:
            return await coro
        except Exception as e:
            errors[name] = _err(e)
            return default

    commits = asyncio.create_task(section("commits", asyncio.to_thread(_recap_commits, d), []))
    pulse_task = asyncio.create_task(section("pulse", asyncio.to_thread(_pulse_closed, d), None))
    asana_task = asyncio.create_task(section("asana", asyncio.to_thread(_asana_completed, d), [])) if asana.connected() else None
    meetings = await section("meetings", outlook.call(_day_meetings, d), [])
    sent = await section("sent", outlook.call(_sent_on, d), [])
    tasks = await section("tasks", outlook.call(_tasks_completed, d), [])
    if asana_task:
        tasks += await asana_task
    from . import today as today_mod
    cleared = sum(1 for v in today_mod.dismissed().values() if v == d.isoformat())
    keys = await asyncio.to_thread(_note_keys)
    for m in meetings:
        m["has_notes"] = _note_key(m["id"], m["start"]) in keys
    data = {"day": d.isoformat(), "commits": await commits, "meetings": meetings,
            "sent": {"count": len(sent), "items": sent[:100]}, "tasks": tasks, "followups_cleared": cleared,
            "pulse": await pulse_task, "errors": errors}
    _recap_cache.clear()
    _recap_cache[d.isoformat()] = (time.time(), data)
    return data


# ---- meeting notes ----------------------------------------------------------------------------------

def _note_key(meeting_id: str, start: str) -> str:
    return hashlib.sha256(f"{meeting_id}|{start}".encode()).hexdigest()


def _check_meeting(meeting_id, start):
    if not isinstance(meeting_id, str) or not OUTLOOK_ID.fullmatch(meeting_id):
        raise HTTPException(400, "That isn't an Outlook meeting")
    if not isinstance(start, str) or not START_RE.fullmatch(start):
        raise HTTPException(400, "That meeting has no valid start time")
    try:
        datetime.fromisoformat(start)
    except ValueError:
        raise HTTPException(400, "That meeting has no valid start time")


def _clean_task_ref(task):
    if not isinstance(task, dict):
        return None
    source, task_id = task.get("source"), task.get("id")
    if source == "asana" and isinstance(task_id, str) and ASANA_ID.fullmatch(task_id):
        return {"source": "asana", "id": task_id}
    if source == "outlook" and isinstance(task_id, str) and OUTLOOK_ID.fullmatch(task_id):
        return {"source": "outlook", "id": task_id}
    return None


def _clean_actions(actions) -> list[dict]:
    if not isinstance(actions, list) or len(actions) > MAX_ACTIONS:
        raise HTTPException(400, f"Up to {MAX_ACTIONS} action items")
    out = []
    for a in actions:
        if not isinstance(a, dict):
            raise HTTPException(400, "Each action item needs some text")
        text = _one_line(a.get("text"), MAX_ACTION, "Each action item")
        if text:
            out.append({"text": text, "done": a.get("done") is True, "task": _clean_task_ref(a.get("task"))})
    return out


def note_get(meeting_id, start) -> dict | None:
    _tables()
    with db._lock:
        row = db._db().execute("SELECT secret, updated FROM meeting_notes WHERE key = ?", (_note_key(meeting_id, start),)).fetchone()
    if not row:
        return None
    try:
        data = _open(row["secret"])
    except Exception:
        return {"locked": True, "subject": "(can't decrypt: saved under another Windows account)", "notes": "", "actions": []}
    return {**data, "updated": row["updated"]}


def note_put(meeting_id, start, payload: dict):
    _tables()
    key = _note_key(meeting_id, start)
    with db._lock:
        if not payload["notes"].strip() and not payload["actions"]:
            db._db().execute("DELETE FROM meeting_notes WHERE key = ?", (key,))
        else:
            db._db().execute("INSERT OR REPLACE INTO meeting_notes (key, start, secret, updated) VALUES (?, ?, ?, ?)",
                             (key, start, _seal(payload, NOTES_LABEL), db.now()))
        db._db().commit()


def note_delete(meeting_id, start):
    _tables()
    with db._lock:
        db._db().execute("DELETE FROM meeting_notes WHERE key = ?", (_note_key(meeting_id, start),))
        db._db().commit()


def _note_keys() -> set[str]:
    _tables()
    with db._lock:
        return {r["key"] for r in db._db().execute("SELECT key FROM meeting_notes").fetchall()}


def notes_list(q: str = "", limit: int = 200) -> tuple[list[dict], list[dict]]:
    """(matching notes, the id/start of every note). Decrypted in memory only; q matches the meeting
    subject or the note's text."""
    _tables()
    with db._lock:
        rows = db._db().execute("SELECT start, secret, updated FROM meeting_notes ORDER BY start DESC").fetchall()
    words = [w for w in q.casefold().split() if w]
    out, keys = [], []
    for r in rows:
        try:
            n = _open(r["secret"])
        except Exception:
            continue
        keys.append({"id": n.get("id"), "start": n.get("start") or r["start"]})
        hay = " ".join([n.get("subject", ""), n.get("notes", "")] + [a.get("text", "") for a in n.get("actions", [])]).casefold()
        if (words and not all(w in hay for w in words)) or len(out) >= limit:
            continue
        actions = n.get("actions", [])
        out.append({"id": n.get("id"), "start": n.get("start") or r["start"], "end": n.get("end"),
                    "subject": n.get("subject", ""), "preview": re.sub(r"\s+", " ", n.get("notes", ""))[:160],
                    "actions": len(actions), "open": sum(1 for a in actions if not a.get("done")), "updated": r["updated"]})
    return out, keys


def _subject_of(meeting_id) -> str:
    try:
        return outlook._ns().GetItemFromID(meeting_id).Subject or ""
    except pywintypes.com_error:
        return ""


@router.get("/notes")
async def list_notes(q: str = ""):
    notes, keys = await asyncio.to_thread(notes_list, q.strip()[:100])
    return {"notes": notes, "keys": keys}


@router.get("/notes/item")
async def get_note(id: str, start: str):
    _check_meeting(id, start)
    note = await asyncio.to_thread(note_get, id, start)
    if note:
        return {"exists": True, "id": id, "start": start, **{k: note.get(k) for k in ("subject", "end", "notes", "actions", "updated", "locked")}}
    try:
        subject = await outlook.call(_subject_of, id)
    except Exception:
        subject = ""
    return {"exists": False, "id": id, "start": start, "subject": subject, "notes": "", "actions": []}


@router.post("/notes")
async def save_note(id: str = Body(...), start: str = Body(...), subject: str = Body(""), end: str | None = Body(None),
                    notes: str = Body(""), actions: list = Body([])):
    _check_meeting(id, start)
    if not isinstance(notes, str) or len(notes) > MAX_NOTES:
        raise HTTPException(400, f"Notes can be up to {MAX_NOTES} characters")
    if end is not None and not (isinstance(end, str) and START_RE.fullmatch(end)):
        end = None
    existing = await asyncio.to_thread(note_get, id, start)
    if existing and existing.get("locked"):
        raise HTTPException(409, "These notes were saved under another Windows account")
    payload = {"id": id, "start": start, "end": end,
               "subject": _flat(subject, MAX_SUBJECT) or "(meeting)",
               "notes": notes, "actions": _clean_actions(actions)}
    await asyncio.to_thread(note_put, id, start, payload)
    return {"ok": True, "exists": bool(payload["notes"].strip() or payload["actions"]), "updated": db.now()}


@router.post("/notes/delete")
async def delete_note(id: str = Body(...), start: str = Body(...)):
    _check_meeting(id, start)
    await asyncio.to_thread(note_delete, id, start)
    return {"ok": True}


@router.post("/notes/task")
async def action_to_task(id: str = Body(...), start: str = Body(...), index: int = Body(...),
                         source: str = Body("outlook"), due: str | None = Body(None)):
    """Turns one saved action item into an Outlook or Asana task (only on this explicit click)."""
    _check_meeting(id, start)
    if source not in ("outlook", "asana"):
        raise HTTPException(400, "Tasks go to Outlook or Asana")
    if due:
        try:
            due = date.fromisoformat(due).isoformat()
        except ValueError:
            raise HTTPException(400, "Pick a valid due date")
    note = await asyncio.to_thread(note_get, id, start)
    actions = (note or {}).get("actions") or []
    if not note or note.get("locked") or not 0 <= index < len(actions):
        raise HTTPException(404, "Save the action item first")
    item = actions[index]
    if item.get("task"):
        raise HTTPException(409, "That action item is already a task")
    when = datetime.fromisoformat(start).strftime("%a %b %d").replace(" 0", " ")
    body = f"Action item from the meeting \"{note.get('subject', '')}\" ({when})."
    if source == "asana":
        task_id = await asyncio.to_thread(asana.create_task, item["text"], due or None, body)
    else:
        task_id = await outlook.call(outlook.create_task, item["text"], due or None, body)
    item["task"] = _clean_task_ref({"source": source, "id": str(task_id)}) or {"source": source, "id": ""}
    await asyncio.to_thread(note_put, id, start, {**{k: note.get(k) for k in ("id", "start", "end", "subject", "notes")},
                                                  "actions": actions})
    return {"ok": True, "task": item["task"], "actions": actions}


# ---- background: offer to capture notes when a meeting ends -------------------------------------

async def meeting_end_loop():
    await asyncio.sleep(45)
    while True:
        try:
            if settings().get("meeting_notes_prompt", True) and await asyncio.to_thread(outlook.is_running):
                now = datetime.now()
                for m in await outlook.call(outlook.meetings_today):
                    if m.get("all_day") or m.get("status") == "free":
                        continue
                    end = datetime.fromisoformat(m["end"])
                    if not now - timedelta(minutes=20) <= end <= now or not OUTLOOK_ID.fullmatch(m["id"] or ""):
                        continue
                    if db.seen(f"mtg-notes:{m['id']}:{m['start']}"):
                        continue
                    existing = await asyncio.to_thread(note_get, m["id"], m["start"])
                    if existing and existing.get("actions"):
                        continue  # actions already captured
                    alerts.raise_alert("Calendar", f"Capture notes/actions for {m['subject']}?",
                                       "Private notes pad: stays on this PC, encrypted, never written to Outlook.", "low",
                                       {"type": "meeting-notes", "id": m["id"], "start": m["start"], "end": m["end"]})
        except outlook.OutlookClosed:
            pass
        except Exception:
            log.exception("Meeting-notes check failed")
        await asyncio.sleep(60)


background = [meeting_end_loop]
