"""Meeting reminder pop-ups (Joe's call 2026-11-19). outlook.watch_loop raises one high-priority alert per meeting,
`outlook.meetings.remind_minutes` before it (Joe's: 5 only), action type "meeting" with the meeting's start and end;
this module turns the unread ones into a pop-up like a personal reminder's: the desktop window comes in front with it
(reminders.front_hook: not while CC is locked or the screen may be on show, e.g. during another call) and it stays
until it's dealt with: Join (Teams meetings), Got it, or "Remind me when it starts" (held back until the start; in
memory, so a restart shows it again). Opening the alert from the feed (Join, Prep, Open) marks it read and ends it
too. Once the meeting is over it no longer pops up. Nothing new is stored: the alert is the record. Logs carry counts."""
import logging
import time
from datetime import datetime

from fastapi import APIRouter, Body, HTTPException

from . import db, reminders
from .connectors import outlook

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/meeting-reminders")
_snoozed: dict[int, float] = {}  # alert id -> when it may pop up again (time.time())


def _ts(iso) -> float | None:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return None


def _alerts(now: float) -> list[dict]:
    """Unread meeting alerts that carry their times (older ones don't) and whose meeting isn't over."""
    out = []
    for a in db.list_alerts(unread_only=True):
        act = a.get("action") or {}
        if act.get("type") != "meeting":
            continue
        start, end = _ts(act.get("start")), _ts(act.get("end"))
        if start is None:
            continue
        if now < (end if end and end > start else start + 30 * 60):
            out.append(a)
    return out


def _arrived(n: int):
    """outlook.watch_loop: n new meeting reminders. Brings the window in front with the pop-up."""
    if reminders.front_hook:
        try:
            log.info("Meeting reminders: %d due; window %s", n, reminders.front_hook("meetingPoll"))
        except Exception as e:
            log.warning("Meeting reminders: couldn't bring the window forward (%s)", type(e).__name__)


outlook.on_meeting = _arrived


@router.get("/pending")
async def pending():
    """The meetings to pop up now (unread alerts, not held back, not over) and when to look again."""
    now = time.time()
    found = _alerts(now)
    for gone in set(_snoozed) - {a["id"] for a in found}:
        _snoozed.pop(gone, None)
    show = [a for a in found if _snoozed.get(a["id"], 0) <= now]
    wakes = [_snoozed[a["id"]] for a in found if _snoozed.get(a["id"], 0) > now]
    items = []
    for a in sorted(show, key=lambda a: a["action"].get("start", "")):
        act = a["action"]
        items.append({"id": a["id"], "meeting_id": act.get("id", ""), "subject": act.get("subject") or str(a["title"]),
                      "location": a.get("body") or "", "start": act.get("start"), "end": act.get("end"),
                      "join_url": act.get("join_url") or ""})
    return {"items": items, "next_wake": min(wakes) if wakes else None}


def _known(alert_id: int) -> dict:
    a = next((a for a in _alerts(time.time()) if a["id"] == alert_id), None)
    if not a:
        raise HTTPException(404, "That meeting reminder is no longer waiting")
    return a


@router.post("/at-start")
async def at_start(id: int = Body(..., embed=True)):
    """Remind me when it starts: held back until the meeting's start time."""
    a = _known(id)
    start = _ts(a["action"].get("start"))
    if start is None or start <= time.time() + 30:
        raise HTTPException(409, "It's starting now")
    _snoozed[id] = start
    return {"ok": True, "wake": start}


@router.post("/done")
async def done(id: int = Body(..., embed=True)):
    """Got it (or joined): ends the pop-up (marks its alert read)."""
    _known(id)
    db.mark_read(id)
    _snoozed.pop(id, None)
    return {"ok": True}
