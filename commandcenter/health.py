"""The health light: one dot in the header (green / amber / red) and, on a click, what's behind it.

Every check reads state Command Center already keeps (no extra Outlook, Teams or network calls), so it's cheap
enough for /api/status's 5-second refresh. States: ok, warn (amber: something's off, CC still works), bad (red: a part
isn't working), off (not set up or switched off) and note (worth knowing, never colours the light: the spam rule that
can't be used on Joe's mailbox). Each check gives plain words and, where there is one, what to do.

Also tracked here, in memory: background jobs that stopped (a loop that ended with an exception would otherwise go
silently) and errors logged in the last hour (logger names and counts only, never the message).
"""
import asyncio
import logging
import shutil
import time
from collections import deque
from datetime import datetime

from fastapi import APIRouter

from . import config, db, security
from .connectors import asana, teams, web

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/health")

ERROR_WINDOW = 3600
DISK_WARN = 2 * 1024 ** 3
DISK_BAD = 500 * 1024 ** 2
_RANK = {"ok": 0, "off": 0, "note": 0, "warn": 1, "bad": 2}

# ---- background jobs and logged errors -----------------------------------------------------------

_stopped: dict[str, dict] = {}
_errors: deque = deque(maxlen=200)  # (time, logger name)


def watch_task(task: asyncio.Task, name: str):
    """Called by app.py for each background job: one that ends while CC runs (an exception, or a return) is listed."""
    def done(t: asyncio.Task):
        if t.cancelled():
            return  # shutting down
        e = t.exception()
        _stopped[name] = {"at": db.now(), "error": type(e).__name__ if e else None}
        log.error("Background job %s stopped (%s)", name, type(e).__name__ if e else "returned")
    task.add_done_callback(done)


class _ErrorCounter(logging.Handler):
    def emit(self, record):
        if record.name != __name__:  # the stopped-job line above is reported by its own check
            _errors.append((record.created, record.name))


_counter = _ErrorCounter(level=logging.ERROR)
logging.getLogger().addHandler(_counter)


# ---- checks ----------------------------------------------------------------------------------------

def _age_min(iso) -> float | None:
    try:
        return (datetime.now() - datetime.fromisoformat(str(iso))).total_seconds() / 60
    except (TypeError, ValueError):
        return None


def _ago(minutes) -> str:
    if minutes is None:
        return "never"
    if minutes < 1.5:
        return "just now"
    if minutes < 90:
        return f"{round(minutes)} min ago"
    return f"{round(minutes / 60)} h ago"


def _check(id, name, state, text, fix=None, action=None) -> dict:
    return {"id": id, "name": name, "state": state, "text": text, "fix": fix, "action": action}


_STARTED = time.monotonic()
STARTING = 180  # seconds after a start in which "not read yet" is expected, not a problem


def _not_yet(id, name) -> dict:
    if time.monotonic() - _STARTED < STARTING:
        return _check(id, name, "note", "Starting: the first read is on its way.")
    return _check(id, name, "warn", "Not read yet since Command Center started.")


def _outlook() -> dict:
    st = db.kv_get("outlook:status")
    if not st:
        return _not_yet("outlook", "Outlook")
    age = _age_min(st.get("checked"))
    poll = max(30, int(config.section("outlook", {}).get("poll_seconds", 60) or 60)) / 60
    if not st.get("ok"):
        if st.get("error") == "Outlook is closed":
            return _check("outlook", "Outlook", "bad", "Outlook is closed: no mail, calendar or meeting alerts.", "Open Outlook.")
        return _check("outlook", "Outlook", "bad", f"The last read failed ({_ago(age)}).",
                      "It's retried every minute. If it keeps failing, restart Outlook, then Command Center.")
    if age is not None and age > poll * 5 + 2:
        return _check("outlook", "Outlook", "warn", f"Last read {_ago(age)}; it normally reads every {round(poll)} min.",
                      "Outlook may be busy or stuck. If this stays, restart Outlook.")
    return _check("outlook", "Outlook", "ok", f"Read {_ago(age)}.")


def _teams() -> dict:
    cfg = teams.settings()
    if not cfg.get("enabled", True):
        return _check("teams", "Teams reader", "off", "Switched off (teams.enabled).")
    if not teams.is_set_up():
        return _check("teams", "Teams reader", "off", "Not signed in, so no Teams counts or alerts.",
                      "Sign in on the Teams tab.", "teams")
    st = db.kv_get("teams:state") or {}
    age = _age_min(st.get("checked"))
    poll = max(1, int(cfg.get("poll_minutes", 2) or 2))
    if st.get("ok") is False:
        if st.get("login_needed"):
            return _check("teams", "Teams reader", "bad", "Teams wants you to sign in again: no Teams counts or alerts.",
                          "Sign in on the Teams tab.", "teams")
        return _check("teams", "Teams reader", "warn", f"The last read failed ({_ago(age)}).",
                      "It's retried at the next check. If it keeps failing, sign in again on the Teams tab.", "teams")
    if age is None:
        return _not_yet("teams", "Teams reader")
    if age > poll * 4 + 3:
        return _check("teams", "Teams reader", "warn", f"Last read {_ago(age)}; it normally reads every {poll} min.",
                      "The background browser may be stuck. Restarting Command Center starts it afresh.")
    details = "" if st.get("details", "ok") == "ok" else " (counts only: chat details unavailable)"
    return _check("teams", "Teams reader", "ok", f"Read {_ago(age)}{details}.")


def _pulse() -> dict:
    try:
        from . import pulse
    except Exception:
        return _check("pulse", "Pulse tickets", "off", "Not loaded.")
    s = pulse.settings()
    if not pulse.is_configured(s):
        return _check("pulse", "Pulse tickets", "off", "Not set up (Pulse Feedback tab).")
    st = db.kv_get("pulse:status") or {}
    poll = max(1, int(s.get("poll_minutes") or 2))
    if st.get("ok") is False:
        return _check("pulse", "Pulse tickets", "bad", f"Can't read tickets; last good read {_ago(_age_min(st.get('last_ok')))}.",
                      "Check the VPN / internet. It retries with a growing pause, up to 15 minutes.", "pulse-feedback")
    age = _age_min(st.get("last_ok"))
    if age is None:
        return _not_yet("pulse", "Pulse tickets")
    if age > poll * 4 + 3:
        return _check("pulse", "Pulse tickets", "warn", f"Last read {_ago(age)}; it normally reads every {poll} min.")
    return _check("pulse", "Pulse tickets", "ok", f"Read {_ago(age)}.")


def _asana() -> dict:
    if not config.section("asana", {}).get("enabled", True):
        return _check("asana", "Asana", "off", "Switched off (asana.enabled).")
    return (_check("asana", "Asana", "ok", "Connected.") if asana.connected()
            else _check("asana", "Asana", "off", "Not connected (no token).", "Add your token from Today's Tasks card."))


def _spam() -> dict:
    st = db.kv_get("spam:rule_status") or {}
    if st.get("ok") is None:
        return _check("spam", "Spam blocks", "off", "Nothing blocked yet.")
    if st.get("ok"):
        return _check("spam", "Spam blocks", "ok", "Outlook rules saved: blocks work with Command Center closed too.")
    if st.get("unsupported"):
        return _check("spam", "Spam blocks", "note",
                      "Enforced by Command Center while it runs, not by an Outlook rule (your mailbox's own rules can't be "
                      "saved from here safely).", "For blocks with Command Center closed, add them to Outlook's Blocked "
                      "Senders: Spam blocks has the list.", "spam")
    return _check("spam", "Spam blocks", "warn", "The Outlook rule wasn't saved, so blocks work only while Command Center runs.",
                  "Open Spam blocks for the reason and Retry.", "spam")


def _websites() -> list[dict]:
    out = []
    for m in web.monitors():
        if not m.get("enabled", True):
            continue
        st = db.kv_get(f"web:{m['name']}")
        if st is None:
            continue
        out.append(_check(f"web:{m['name']}", f"Monitor: {m['name']}", "ok" if st.get("ok") else "warn",
                          "Checked fine." if st.get("ok") else "The last check failed."))
    return out


def _folders() -> dict:
    fs = security.folder_security()
    if fs.get("ok", True):
        return _check("folders", "App folder", "ok", "Only you, SYSTEM and Administrators can change it.")
    return _check("folders", "App folder", "bad",
                  f"Another account can change {len(fs.get('unsafe') or [])} folder(s) above the app.",
                  (fs.get("fix") or "See the Security alert for the fix.")[:600])


def _disk() -> dict:
    try:
        free = shutil.disk_usage(config.DATA_DIR).free
    except OSError:
        return _check("disk", "Disk space", "warn", "Couldn't read the free space.")
    gb = free / 1024 ** 3
    text = f"{gb:.1f} GB free on the app's drive."
    if free < DISK_BAD:
        return _check("disk", "Disk space", "bad", text, "Free some space: the database and logs can't be written when it's full.")
    if free < DISK_WARN:
        return _check("disk", "Disk space", "warn", text, "Free some space soon.")
    return _check("disk", "Disk space", "ok", text)


def _jobs() -> dict:
    if not _stopped:
        return _check("jobs", "Background jobs", "ok", "All running.")
    names = ", ".join(sorted(_stopped))
    return _check("jobs", "Background jobs", "bad", f"Stopped: {names}. What they do (alerts, reads) has stopped too.",
                  "Restart Command Center. The log has the details.")


def _logged_errors() -> dict:
    cutoff = time.time() - ERROR_WINDOW
    recent = [n for t, n in _errors if t >= cutoff]
    if not recent:
        return _check("errors", "Errors logged", "ok", "None in the last hour.")
    by = {}
    for n in recent:
        by[n] = by.get(n, 0) + 1
    parts = ", ".join(f"{n.removeprefix('commandcenter.')} ({c})" for n, c in sorted(by.items(), key=lambda kv: -kv[1])[:4])
    return _check("errors", "Errors logged", "warn", f"{len(recent)} in the last hour: {parts}.",
                  "Usually a one-off. If it repeats, the log (data\\commandcenter.log) says where.")


def checks() -> list[dict]:
    out = []
    for fn in (_outlook, _teams, _pulse, _asana, _spam, _websites, _folders, _disk, _jobs, _logged_errors):
        try:
            r = fn()
            out.extend(r if isinstance(r, list) else [r])
        except Exception as e:  # one broken check never hides the others
            out.append(_check(fn.__name__.strip("_"), fn.__name__.strip("_").title(), "warn", f"Couldn't check ({type(e).__name__})."))
    return out


def summary(items: list[dict] | None = None) -> dict:
    items = checks() if items is None else items
    worst = max((_RANK.get(c["state"], 0) for c in items), default=0)
    issues = [c for c in items if _RANK.get(c["state"], 0) > 0]
    return {"state": ("ok", "warn", "bad")[worst], "issues": len(issues), "names": [c["name"] for c in issues][:6]}


@router.get("")
async def get_health():
    items = await asyncio.to_thread(checks)
    return {"checks": items, **summary(items), "checked": db.now()}
