"""10-day planner: Outlook calendar as the base, dated follow-ups on top, plus private personal entries.

Personal entries never go to Outlook or anywhere else. They live in the personal vault (vault.py): encrypted
(AES-256-GCM, date and time included) with a key made from Joe's own passphrase, which is not stored anywhere,
so neither another account, an administrator, a copied backup nor the domain's DPAPI recovery key can read
them. While the vault is locked or Privacy mode is on, the planner returns no personal entries at all.

Older versions kept entries in the personal_events table (title/notes DPAPI-encrypted). The first unlock
of the vault moves them in and deletes those rows (secure_delete), so no DPAPI copy is left.
"""
import asyncio
import json
import re
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Body, HTTPException

from . import db, feeds, today, vault
from .connectors import asana, outlook

router = APIRouter(prefix="/api/planner")
MAX_DAYS = 31
MAX_ENTRIES = 5000
KIND = "planner-entry"
RECORD_ID = re.compile(r"[0-9a-f]{8,64}")


# ---- older DPAPI copies (moved into the vault on first unlock) -------------------------------------

def _legacy_exists() -> bool:
    with db._lock:
        return db._db().execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'personal_events'").fetchone() is not None


def _legacy_count() -> int:
    if not _legacy_exists():
        return 0
    with db._lock:
        return db._db().execute("SELECT COUNT(*) FROM personal_events").fetchone()[0]


def _dpapi_open(blob: bytes) -> dict:
    import win32crypt
    return json.loads(win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1].decode("utf-8"))


def _migrate_legacy() -> int:
    """Vault unlocked: copy each DPAPI entry into the vault, then delete the old rows."""
    if not _legacy_exists():
        return 0
    with db._lock:
        rows = [dict(r) for r in db._db().execute("SELECT id, day, start, end, all_day, secret FROM personal_events").fetchall()]
    moved = []
    for r in rows:
        try:
            secret = _dpapi_open(r["secret"])
        except Exception:
            secret = {"title": "(couldn't decrypt an older entry: it was created under another Windows account)", "notes": ""}
        vault.put(KIND, {"day": r["day"], "start": r["start"], "end": r["end"], "all_day": bool(r["all_day"]),
                         "title": str(secret.get("title", ""))[:200], "notes": str(secret.get("notes", ""))[:4000]})
        moved.append(r["id"])
    with db._lock, vault.secure_delete() as c:
        c.executemany("DELETE FROM personal_events WHERE id = ?", [(i,) for i in moved])
        c.commit()
    return len(moved)


def _wipe_legacy():
    if _legacy_exists():
        with db._lock, vault.secure_delete() as c:
            c.execute("DELETE FROM personal_events")
            c.commit()


vault.register_store("planner entries", legacy_count=_legacy_count, migrate=_migrate_legacy, wipe_legacy=_wipe_legacy)


# ---- entries in the vault ------------------------------------------------------------------------

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
    """Personal entries in [first, last]; [] whenever personal data isn't available (locked / Privacy mode)."""
    if not vault.available():
        return []
    try:
        entries = vault.records(KIND)
    except vault.VaultLocked:
        return []
    lo, hi = first.isoformat(), last.isoformat()
    out = [{"id": e["id"], "day": e["day"], "start": e.get("start"), "end": e.get("end"), "all_day": bool(e.get("all_day")),
            "title": e.get("title", ""), "notes": e.get("notes", "")}
           for e in entries if lo <= str(e.get("day", "")) <= hi]
    return sorted(out, key=lambda e: (e["day"], e["start"] or ""))


def personal_state() -> dict:
    st = vault.status()
    return {k: st[k] for k in ("set_up", "unlocked", "privacy", "available")} | {"waiting": sum(st["legacy"].values())}


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
    overlays, overlay_status = await feeds.events_between(first, last)  # nothing while personal data is unavailable

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
    return {"days": out, "outlook_error": outlook_error, "calendars": overlay_status,
            "personal_state": await asyncio.to_thread(personal_state)}


def _save(entry_id, values: dict) -> str:
    vault.require_available()
    if entry_id is not None:
        if not isinstance(entry_id, str) or not RECORD_ID.fullmatch(entry_id) or vault.get(KIND, entry_id) is None:
            raise HTTPException(404, "That entry no longer exists")
    elif vault.record_count() >= MAX_ENTRIES:
        raise HTTPException(400, f"Up to {MAX_ENTRIES} personal entries")
    return vault.put(KIND, values, entry_id)


@router.post("/personal")
async def save_personal(day: str = Body(...), title: str = Body(...), notes: str = Body(""), all_day: bool = Body(True),
                        start: str | None = Body(None), end: str | None = Body(None), id: str | None = Body(None)):
    _validate(day, start, end, all_day, title, notes)
    values = {"day": day, "start": None if all_day else start, "end": None if all_day else end, "all_day": bool(all_day),
              "title": title.strip(), "notes": notes or "", "updated": datetime.now().isoformat(timespec="seconds")}
    try:
        new_id = await asyncio.to_thread(_save, id, values)
    except vault.VaultLocked:
        raise HTTPException(423, "Your personal vault is locked. Unlock it first")
    return {"ok": True, "id": new_id}


@router.post("/personal/delete")
async def delete_personal(id: str = Body(..., embed=True)):
    vault.require_available()
    try:
        ok = await asyncio.to_thread(vault.delete, KIND, id)
    except vault.VaultLocked:
        raise HTTPException(423, "Your personal vault is locked. Unlock it first")
    if not ok:
        raise HTTPException(404, "That entry no longer exists")
    return {"ok": True}
