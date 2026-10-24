"""10-day planner: Outlook calendar as the base, dated follow-ups on top, plus private personal entries.

Personal entries never go to Outlook or anywhere else: they live in the local SQLite database (inside the
app's private data folder) and their title/notes are encrypted with Windows DPAPI for the current user,
so another account, an administrator browsing the file, or a copied backup can't read them.
"""
import asyncio
import json
import re
from datetime import date, datetime, timedelta

import win32crypt
from fastapi import APIRouter, Body, HTTPException

from . import db, feeds, today
from .connectors import asana, outlook

router = APIRouter(prefix="/api/planner")
MAX_DAYS = 31
DPAPI_LABEL = "CommandCenter personal calendar"


# ---- private storage ---------------------------------------------------------------------

def _table():
    with db._lock:
        db._db().execute(
            "CREATE TABLE IF NOT EXISTS personal_events ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, start TEXT, end TEXT,"
            " all_day INTEGER NOT NULL DEFAULT 1, secret BLOB NOT NULL, updated TEXT NOT NULL)")
        db._db().commit()


def _seal(payload: dict) -> bytes:
    return win32crypt.CryptProtectData(json.dumps(payload).encode("utf-8"), DPAPI_LABEL, None, None, None, 0)


def _open(blob: bytes) -> dict:
    return json.loads(win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1].decode("utf-8"))


def _validate(day, start, end, all_day, title, notes):
    try:
        date.fromisoformat(day)
    except (TypeError, ValueError):
        raise HTTPException(400, "Pick a valid date")
    if not title or not title.strip():
        raise HTTPException(400, "Give the entry a title")
    if len(title) > 200 or len(notes or "") > 4000:
        raise HTTPException(400, "Title up to 200 characters, notes up to 4000")
    if not all_day:
        for t in (start, end):
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t or ""):
                raise HTTPException(400, "Times must look like 14:30")
        if end <= start:
            raise HTTPException(400, "End time must be after the start time")


def personal_between(first: date, last: date) -> list[dict]:
    _table()
    with db._lock:
        rows = db._db().execute(
            "SELECT id, day, start, end, all_day, secret FROM personal_events WHERE day >= ? AND day <= ? ORDER BY day, start",
            (first.isoformat(), last.isoformat())).fetchall()
    out = []
    for r in rows:
        try:
            secret = _open(r["secret"])
        except Exception:
            secret = {"title": "(can't decrypt: created under another Windows account)", "notes": ""}
        out.append({"id": r["id"], "day": r["day"], "start": r["start"], "end": r["end"],
                    "all_day": bool(r["all_day"]), "title": secret.get("title", ""), "notes": secret.get("notes", "")})
    return out


# ---- dated follow-ups ----------------------------------------------------------------------

async def _dated_items(first: date, last: date) -> list[dict]:
    items = []
    today_iso = date.today().isoformat()
    try:
        todo = await outlook.call(outlook.todo_items)
    except Exception:
        todo = {"tasks": [], "flagged": []}
    tasks = list(todo.get("tasks", []))
    if asana.connected():
        try:
            tasks += await asyncio.to_thread(asana.my_tasks)
        except Exception:
            pass
    for t in tasks:
        if t.get("due"):
            day = max(t["due"], today_iso)  # overdue items show on today
            if day <= last.isoformat():
                items.append({"kind": "task", "source": "asana" if t["source"] == "asana" else "todo", "id": t["id"],
                              "day": day, "title": t["subject"], "overdue": t["due"] < today_iso, "url": t.get("url")})
    recent_due = (date.today() - timedelta(days=7)).isoformat()  # older overdue flags stay on the Follow-ups panel
    for f in todo.get("flagged", []):
        if f.get("due") and f["due"] >= recent_due and not today.AUTOMATED.search(f.get("sender_name", "")):
            day = max(f["due"], today_iso)
            if day <= last.isoformat():
                items.append({"kind": "flag", "source": "flagged", "id": f["id"], "day": day, "title": f["subject"],
                              "who": f.get("sender_name", ""), "overdue": f["due"] < today_iso})
    for item_id, s in today.snoozes().items():
        day = s["until"][:10]
        if first.isoformat() <= day <= last.isoformat():
            items.append({"kind": "snooze", "source": "outlook" if s.get("kind") != "task" else "todo", "id": item_id,
                          "day": day, "time": s["until"][11:16], "title": s.get("subject", ""), "who": s.get("who", "")})
    for w in today.reply_watches():
        day = w["due"][:10]
        if first.isoformat() <= day <= last.isoformat():
            items.append({"kind": "reply-watch", "source": "sent", "id": w["id"], "day": day, "time": w["due"][11:16],
                          "title": w["subject"], "who": w.get("to", "")})
    return items


# ---- routes -----------------------------------------------------------------------------------

@router.get("")
async def planner(days: int = 10):
    days = max(1, min(days, MAX_DAYS))
    first = date.today()
    last = first + timedelta(days=days - 1)
    try:
        meetings = await outlook.call(outlook.meetings_range, days)
        outlook_error = None
    except Exception as e:
        meetings, outlook_error = [], str(e).splitlines()[0] if str(e) else "Outlook unavailable"
    dated = await _dated_items(first, last)
    personal = await asyncio.to_thread(personal_between, first, last)
    overlays, overlay_status = await feeds.events_between(first, last)

    def on_day(ev, d):  # an all-day event's end is exclusive (midnight of the next day)
        end_day = (datetime.fromisoformat(ev["end"]) - timedelta(minutes=1)).date().isoformat() if ev["all_day"] else ev["end"][:10]
        return ev["start"][:10] <= d <= max(end_day, ev["start"][:10])

    out = []
    for i in range(days):
        d = (first + timedelta(days=i)).isoformat()
        out.append({
            "date": d,
            "meetings": [m for m in meetings if on_day(m, d)],
            "overlays": sorted((e for e in overlays if on_day(e, d)), key=lambda e: (not e["all_day"], e["start"])),
            "items": [x for x in dated if x["day"] == d],
            "personal": [p for p in personal if p["day"] == d],
        })
    return {"days": out, "outlook_error": outlook_error, "calendars": overlay_status}


@router.post("/personal")
async def save_personal(day: str = Body(...), title: str = Body(...), notes: str = Body(""), all_day: bool = Body(True),
                        start: str | None = Body(None), end: str | None = Body(None), id: int | None = Body(None)):
    _validate(day, start, end, all_day, title, notes)
    blob = await asyncio.to_thread(_seal, {"title": title.strip(), "notes": notes or ""})
    _table()
    values = (day, None if all_day else start, None if all_day else end, int(all_day), blob, datetime.now().isoformat(timespec="seconds"))
    with db._lock:
        if id is None:
            cur = db._db().execute("INSERT INTO personal_events (day, start, end, all_day, secret, updated) VALUES (?,?,?,?,?,?)", values)
            new_id = cur.lastrowid
        else:
            cur = db._db().execute("UPDATE personal_events SET day=?, start=?, end=?, all_day=?, secret=?, updated=? WHERE id=?", (*values, id))
            if cur.rowcount != 1:
                raise HTTPException(404, "That entry no longer exists")
            new_id = id
        db._db().commit()
    return {"ok": True, "id": new_id}


@router.post("/personal/delete")
async def delete_personal(id: int = Body(..., embed=True)):
    _table()
    with db._lock:
        db._db().execute("DELETE FROM personal_events WHERE id = ?", (id,))
        db._db().commit()
    return {"ok": True}
