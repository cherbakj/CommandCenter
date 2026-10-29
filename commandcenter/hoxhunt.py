"""Hoxhunt phishing-test pop-ups. outlook.is_hoxhunt finds the tests and outlook.watch_loop raises a high-priority
alert for each (action type "hoxhunt"); this module turns the unread ones into a pop-up, like personal reminders:
the desktop window comes in front with it (reminders.front_hook: not while CC is locked or the screen may be on
show), and it comes back until the test is reported. Reporting is Hoxhunt's button in Outlook, which takes the email
out of the Inbox; a test no longer in the Inbox ends its reminders by itself (its alert is marked read). "Already
reported" does the same by hand, "Remind me in 15 min" holds it back for a while (in memory: a restart shows it
again). Nothing new is stored: the alert is the record. Logs carry counts only."""
import asyncio
import logging
import time

from fastapi import APIRouter, Body, HTTPException

from . import db, reminders
from .connectors import outlook

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/hoxhunt")
SNOOZE_MINUTES = 15
_snoozed: dict[int, float] = {}  # alert id -> when it may pop up again (time.time())


def _alerts() -> list[dict]:
    return [a for a in db.list_alerts(unread_only=True) if (a.get("action") or {}).get("type") == "hoxhunt"]


def _still_in_inbox(entry_ids: list[str]) -> set[str]:
    """COM thread: which of these emails are still in the Inbox (reported ones were taken out of it)."""
    ns = outlook._ns()
    inbox = ns.GetDefaultFolder(outlook.OL_FOLDER_INBOX).EntryID
    left = set()
    for eid in entry_ids:
        try:
            if ns.CompareEntryIDs(ns.GetItemFromID(eid).Parent.EntryID, inbox):
                left.add(eid)
        except outlook.pywintypes.com_error:
            pass  # deleted or moved away
    return left


def _arrived(n: int):
    """outlook.watch_loop: n new tests. Brings the window in front with the pop-up (as for a due reminder)."""
    if reminders.front_hook:
        try:
            log.info("Hoxhunt: %d new test(s); window %s", n, reminders.front_hook("hoxhuntPoll"))
        except Exception as e:
            log.warning("Hoxhunt: couldn't bring the window forward (%s)", type(e).__name__)


outlook.on_hoxhunt = _arrived


@router.get("/pending")
async def pending():
    """The tests to pop up now (unread alerts, not snoozed, still in the Inbox) and when to look again."""
    found = _alerts()
    if found and await asyncio.to_thread(outlook.is_running):
        try:
            left = await outlook.call(_still_in_inbox, [a["action"].get("id", "") for a in found])
        except Exception:
            left = None  # Outlook busy or closed: keep them all for now
        if left is not None:
            for a in [a for a in found if a["action"].get("id") not in left]:
                db.mark_read(a["id"])
                _snoozed.pop(a["id"], None)
                log.info("Hoxhunt: a test left the Inbox (reported); its reminders ended")
            found = [a for a in found if a["action"].get("id") in left]
    now = time.time()
    show = [a for a in found if _snoozed.get(a["id"], 0) <= now]
    wakes = [_snoozed[a["id"]] for a in found if _snoozed.get(a["id"], 0) > now]
    items = [{"id": a["id"], "mail_id": a["action"].get("id", ""), "ts": a["ts"],
              "subject": str(a["title"]).removeprefix("Hoxhunt test: ")} for a in show]
    return {"items": items, "next_wake": min(wakes) if wakes else None, "snooze_minutes": SNOOZE_MINUTES}


def _known(alert_id: int) -> dict:
    a = next((a for a in _alerts() if a["id"] == alert_id), None)
    if not a:
        raise HTTPException(404, "That test is no longer waiting")
    return a


@router.post("/snooze")
async def snooze(id: int = Body(..., embed=True), minutes: int = Body(SNOOZE_MINUTES, embed=True)):
    _known(id)
    _snoozed[id] = time.time() + max(1, min(int(minutes), 240)) * 60
    return {"ok": True, "wake": _snoozed[id]}


@router.post("/done")
async def done(id: int = Body(..., embed=True)):
    """Already reported: ends its reminders (marks its alert read)."""
    _known(id)
    db.mark_read(id)
    _snoozed.pop(id, None)
    return {"ok": True}
