"""The Today view: date, weather, agenda, tasks (Outlook + Asana) and follow-ups, plus a daily briefing alert."""
import asyncio
import logging
import re
import time
from datetime import date, datetime, timedelta

from . import alerts, config, db, people
from .connectors import asana, outlook, weather

log = logging.getLogger(__name__)
FOLLOWUP_GROUPS = ("urgent_reply", "needs_reply", "teams_reply", "awaiting_reply", "flagged", "unread_vip")
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


# ---- snooze and no-reply reminders -------------------------------------------

def snoozes() -> dict:
    return db.kv_get("snoozes", {})


def snooze(item_id, until, info):
    s = snoozes()
    s[item_id] = {"until": until, **{k: info.get(k, "") for k in ("subject", "who", "kind")}}
    db.kv_set("snoozes", s)


def unsnooze(item_id):
    s = snoozes()
    s.pop(item_id, None)
    db.kv_set("snoozes", s)


def reply_watches() -> list[dict]:
    return db.kv_get("reply_watches", [])


def watch_for_reply(info, days, entry_id):
    """Remind me in `days` days if nobody has answered this thread by then."""
    now = datetime.now()
    watches = [w for w in reply_watches() if w["conversation"] != info["conversation"]]
    watches.append({"conversation": info["conversation"], "subject": info["subject"], "to": info["to"],
                    "id": entry_id, "since": now.isoformat(timespec="seconds"),
                    "due": (now + timedelta(days=float(days))).isoformat(timespec="seconds")})
    db.kv_set("reply_watches", watches)


def cancel_reply_watch(conversation):
    db.kv_set("reply_watches", [w for w in reply_watches() if w["conversation"] != conversation])


async def _check_snoozes_and_reminders():
    now = datetime.now().isoformat(timespec="seconds")
    s = snoozes()
    for item_id, info in list(s.items()):
        if info["until"] <= now:
            s.pop(item_id)
            alerts.raise_alert("Snooze", f"Back from snooze: {info['subject']}", info.get("who", ""), "normal",
                               {"type": "mail", "id": item_id} if info.get("kind") != "task" else {"type": "today"})
    db.kv_set("snoozes", s)

    keep = []
    for w in reply_watches():
        if w["due"] > now:
            keep.append(w)
            continue
        if not await outlook.call(outlook.conversation_replied, w["conversation"], w["since"]):
            alerts.raise_alert("Follow-up", f"No reply yet: {w['subject']}", f"To {w['to'][:80]}", "normal",
                               {"type": "mail", "id": w["id"]})
    db.kv_set("reply_watches", keep)
    await _escalate_urgent()


async def _escalate_urgent():
    """Every 10 minutes: one high-priority reminder per urgent email/Teams message still unanswered after
    followups.urgent_escalate_hours."""
    hours = float(config.section("followups", {}).get("urgent_escalate_hours", 4) or 0)
    if not hours or not people.urgent_people() or time.time() - db.kv_get("urgent:checked", 0) < 600:
        return
    db.kv_set("urgent:checked", time.time())
    data = await _followups({"flagged": []}, refresh=True)
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="minutes")
    waiting = data.get("urgent_reply", []) + [t for t in data.get("teams_reply", []) if t.get("urgent")]
    for m in waiting:
        if m["received"] <= cutoff and not db.seen(f"urgent-escalate:{m['id']}"):
            who = m.get("from_name") or m.get("sender_name", "")
            alerts.raise_alert("Urgent", f"Still waiting on your reply: {m['subject']}", f"From {who}", "high",
                               {"type": "mail", "id": m["id"]})


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
    now = datetime.now().isoformat(timespec="seconds")
    hidden_ids = set(dismissed()) | {i for i, v in snoozes().items() if v["until"] > now}
    mutes = muted()
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
    snoozed_now = {i for i, v in snoozes().items() if v["until"] > datetime.now().isoformat(timespec="seconds")}
    tasks = [t for t in tasks if t["id"] not in snoozed_now]
    tasks.sort(key=lambda t: (t["due"] is None, t["due"] or "", t["subject"].lower()))

    return {
        "now": datetime.now().isoformat(timespec="seconds"),
        "weather": await weather_task,
        "agenda": agenda,
        "tasks": {"items": tasks, "asana": asana_state, "error": todo.get("error")},
        "followups": followups,
        "snoozed": len(snoozes()),
        "reply_watches": len(reply_watches()),
    }


def summary(data) -> str:
    today = date.today().isoformat()
    agenda = data["agenda"] if isinstance(data["agenda"], list) else []
    remaining = [m for m in agenda if m["end"] >= datetime.now().isoformat()]
    due = [t for t in data["tasks"]["items"] if t["due"] and t["due"] <= today]
    f = data["followups"] if "error" not in data["followups"] else {}
    n_follow = sum(len(f.get(k, [])) for k in FOLLOWUP_GROUPS)
    n_urgent = len(f.get("urgent_reply", [])) + len([t for t in f.get("teams_reply", []) if t.get("urgent")])
    parts = [f"{len(remaining)} meeting(s)", f"{len(due)} task(s) due", f"{n_follow} follow-up(s)"]
    if n_urgent:
        parts.insert(0, f"{n_urgent} URGENT repl{'y' if n_urgent == 1 else 'ies'}")
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
    top3 = await _daily_note("briefing_note")
    alerts.raise_alert("Today", summary(data), " · ".join(p for p in (body, top3) if p), "normal", {"type": "today"})


async def _daily_note(name) -> str:
    """Top 3 line for the briefing / wrap-up from daily.py (counts only; "" if that module isn't there)."""
    try:
        from . import daily
        return await asyncio.to_thread(getattr(daily, name))
    except Exception:
        log.exception("Top 3 summary failed")
        return ""


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
    top3 = await _daily_note("wrapup_note")
    if top3:
        parts.insert(0, top3)
    body = (f"{label}: {len(tomorrow)} meeting(s), first is {first['subject']} at "
            f"{datetime.fromisoformat(first['start']):%I:%M %p}" if first else f"{label}: no meetings")
    alerts.raise_alert("Wrap-up", " · ".join(parts), body, "normal", {"type": "today"})
    try:  # projects with uncommitted / unpushed work: a separate high-priority alert
        from . import daily
        await daily.wrapup_uncommitted()
    except ImportError:
        pass
    except Exception:
        log.exception("Uncommitted-work check failed")


async def briefing_loop():
    """Morning briefing at today.briefing_time and end-of-day wrap-up at today.wrapup_time, once each per day."""
    while True:
        try:
            await _check_snoozes_and_reminders()
        except outlook.OutlookClosed:
            pass
        except Exception:
            log.exception("Snooze/reminder check failed")
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
