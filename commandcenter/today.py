"""The Today view: date, weather, agenda, tasks (Outlook + Asana) and follow-ups, plus a daily briefing alert."""
import asyncio
import logging
import re
import time
from datetime import date, datetime, timedelta

from . import alerts, config, db
from .connectors import asana, outlook, weather

log = logging.getLogger(__name__)
FOLLOWUP_CACHE_SECONDS = 120
_followup_cache: dict = {}
AUTOMATED = re.compile(r"no-?reply|donotreply|notifications?@|mailer-daemon|postmaster", re.I)


# ---- dismissed items and muted senders (stored in the local database) --------

def dismissed() -> dict:
    return db.kv_get("followups:dismissed", {})


def dismiss(item_id):
    d = {k: v for k, v in dismissed().items() if v >= (date.today() - timedelta(days=60)).isoformat()}
    d[item_id] = date.today().isoformat()
    db.kv_set("followups:dismissed", d)


def muted() -> list[str]:
    return db.kv_get("followups:muted", [])


def mute(sender):
    sender = sender.strip().lower()
    if sender and sender not in muted():
        db.kv_set("followups:muted", muted() + [sender])


def unmute(sender):
    db.kv_set("followups:muted", [m for m in muted() if m != sender.strip().lower()])


def _vip_senders() -> list[str]:
    senders = []
    for rule in config.section("outlook", {}).get("rules") or []:
        senders += rule.get("senders") or []
    return senders


def _keep(item, cfg, hidden_ids, mutes) -> bool:
    if item["id"] in hidden_ids:
        return False
    who = " ".join(str(item.get(k, "")) for k in ("sender", "sender_name", "to")).lower()
    if any(m in who for m in mutes):
        return False
    for pattern in cfg.get("ignore_subjects") or []:
        if re.search(pattern, item.get("subject", ""), re.I):
            return False
    return not any(r.lower() in item.get("to", "").lower() for r in cfg.get("ignore_recipients") or [])


async def _followups(todo, refresh=False) -> dict:
    cfg = config.section("followups", {})
    if refresh or time.time() - _followup_cache.get("at", 0) > FOLLOWUP_CACHE_SECONDS:
        _followup_cache["data"] = await outlook.call(
            outlook.followups, cfg.get("awaiting_reply_days", 7), cfg.get("needs_reply_days", 5),
            cfg.get("min_wait_hours", 4), _vip_senders())
        _followup_cache["at"] = time.time()
    raw = _followup_cache["data"]
    hidden_ids, mutes = set(dismissed()), muted()
    out = {k: [i for i in v if _keep(i, cfg, hidden_ids, mutes)] for k, v in raw.items()}

    # Flagged mail: skip automated senders and old flags; overdue first, then newest.
    cutoff = (datetime.now() - timedelta(days=cfg.get("flagged_days", 30))).isoformat()
    automated = older = 0
    flagged = []
    for f in todo.get("flagged", []):
        if AUTOMATED.search(f["sender_name"]):
            automated += 1
        elif (f["received"] or "") < cutoff and not (f["due"] and f["due"] >= date.today().isoformat()):
            older += 1
        elif _keep(f, cfg, hidden_ids, mutes):
            flagged.append(f)
    flagged.sort(key=lambda f: f["received"] or "", reverse=True)
    out["flagged"] = flagged[:25]
    out["flagged_hidden"] = {"automated": automated, "older": older, "more": max(0, len(flagged) - 25),
                             "total": len(todo.get("flagged", []))}
    return out


async def _safe(coro):
    try:
        return await coro
    except Exception as e:
        log.warning("Today section failed: %s", e)
        return {"error": str(e).splitlines()[0] if str(e) else type(e).__name__}


async def build(refresh=False) -> dict:
    asana_on = asana.connected()
    weather_task = asyncio.create_task(_safe(asyncio.to_thread(weather.current)))
    asana_task = asyncio.create_task(_safe(asyncio.to_thread(asana.my_tasks))) if asana_on else None
    agenda = await _safe(outlook.call(outlook.meetings_today))
    todo = await _safe(outlook.call(outlook.todo_items))
    followups = await _safe(_followups(todo, refresh)) if "error" not in todo else todo

    tasks = list(todo.get("tasks", [])) if "error" not in todo else []
    asana_state = {"connected": asana_on}
    if asana_task:
        asana_rows = await asana_task
        if isinstance(asana_rows, dict):
            asana_state["error"] = asana_rows["error"]
        else:
            tasks += asana_rows
    tasks.sort(key=lambda t: (t["due"] is None, t["due"] or "", t["subject"].lower()))

    return {
        "now": datetime.now().isoformat(timespec="seconds"),
        "weather": await weather_task,
        "agenda": agenda,
        "tasks": {"items": tasks, "asana": asana_state, "error": todo.get("error")},
        "followups": followups,
    }


def summary(data) -> str:
    today = date.today().isoformat()
    agenda = data["agenda"] if isinstance(data["agenda"], list) else []
    remaining = [m for m in agenda if m["end"] >= datetime.now().isoformat()]
    due = [t for t in data["tasks"]["items"] if t["due"] and t["due"] <= today]
    f = data["followups"] if "error" not in data["followups"] else {}
    n_follow = sum(len(f.get(k, [])) for k in ("flagged", "awaiting_reply", "needs_reply", "unread_vip"))
    parts = [f"{len(remaining)} meeting(s)", f"{len(due)} task(s) due", f"{n_follow} follow-up(s)"]
    w = data["weather"]
    if "error" not in w:
        parts.insert(0, f"{w['temp']}{w['unit']} {w['text'].lower()}")
    return " · ".join(parts)


def _time_reached(at, now, window_hours=4) -> bool:
    """True from `at` ("HH:MM") until `window_hours` later, so a late start still catches up."""
    at = str(at or "").strip()
    if not at:
        return False
    hour, minute = (int(x) for x in at.split(":"))
    scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return scheduled <= now < scheduled + timedelta(hours=window_hours)


async def _briefing(now):
    data = await build(refresh=True)
    agenda = data["agenda"] if isinstance(data["agenda"], list) else []
    first = next((m for m in agenda if m["start"] >= now.isoformat(timespec="minutes")), None)
    body = f"First up: {first['subject']} at {datetime.fromisoformat(first['start']):%I:%M %p}" if first else ""
    alerts.raise_alert("Today", summary(data), body, "normal", {"type": "today"})


async def _wrapup(now):
    data = await build(refresh=True)
    next_day = 3 if now.weekday() == 4 else 1  # Friday wraps up to Monday
    tomorrow = await outlook.call(outlook.meetings_for_day, next_day)
    today_iso = date.today().isoformat()
    open_due = [t for t in data["tasks"]["items"] if t["due"] and t["due"] <= today_iso]
    f = data["followups"] if "error" not in data["followups"] else {}
    label = "Monday" if next_day == 3 else "Tomorrow"
    first = tomorrow[0] if tomorrow else None
    parts = [f"{len(open_due)} task(s) still due", f"{len(f.get('needs_reply', []))} reply(ies) owed",
             f"{len(f.get('awaiting_reply', []))} waiting on others"]
    body = (f"{label}: {len(tomorrow)} meeting(s), first is {first['subject']} at "
            f"{datetime.fromisoformat(first['start']):%I:%M %p}" if first else f"{label}: no meetings")
    alerts.raise_alert("Wrap-up", " · ".join(parts), body, "normal", {"type": "today"})


async def briefing_loop():
    """Morning briefing at today.briefing_time and end-of-day wrap-up at today.wrapup_time, once each per day."""
    while True:
        try:
            cfg = config.section("today", {})
            now = datetime.now()
            if not (cfg.get("weekdays_only", True) and now.weekday() >= 5):
                if _time_reached(cfg.get("briefing_time"), now) and not db.seen(f"briefing:{date.today()}"):
                    await _briefing(now)
                if _time_reached(cfg.get("wrapup_time"), now) and not db.seen(f"wrapup:{date.today()}"):
                    await _wrapup(now)
        except Exception:
            log.exception("Briefing failed")
        await asyncio.sleep(60)
