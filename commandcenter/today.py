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
# These kv values (and snoozes / reply_watches below, which hold subjects and names) are encrypted at rest by
# db.kv_set (DPAPI, current Windows user); see db.py.

def dismissed() -> dict:
    return db.kv_get("followups:dismissed", {})


def _dismiss_cutoff() -> str:
    return (date.today() - timedelta(days=db.retention_settings()["dismissed_followups_days"])).isoformat()


def dismiss(item_id):
    d = {k: v for k, v in dismissed().items() if v >= _dismiss_cutoff()}
    d[item_id] = date.today().isoformat()
    db.kv_set("followups:dismissed", d)


def undismiss(item_id):
    d = dismissed()
    if d.pop(item_id, None) is not None:
        db.kv_set("followups:dismissed", d)


def prune_dismissed() -> int:
    """Retention (retention.dismissed_followups_days, default 60): forgets old dismissals. Returns how many."""
    d = dismissed()
    keep = {k: v for k, v in d.items() if v >= _dismiss_cutoff()}
    if len(keep) != len(d):
        db.kv_set("followups:dismissed", keep)
    return len(d) - len(keep)


def muted() -> list[str]:
    return db.kv_get("followups:muted", [])


def mute(sender):
    sender = sender.strip().lower()
    if sender and sender not in muted():
        db.kv_set("followups:muted", muted() + [sender])


def unmute(sender):
    db.kv_set("followups:muted", [m for m in muted() if m != sender.strip().lower()])


# ---- task priority ---------------------------------------------------------------
# Outlook / To Do tasks keep their priority in Outlook's own Importance (outlook.set_task_priority), so Outlook
# and To Do on the phone show it too. Sources without a priority of their own (Asana) keep it here: kv
# tasks:priority {"asana:<gid>": "high" | "low"} (Normal = no entry), sealed like other work data and pruned
# when the task is no longer open.
PRIORITIES = ("high", "normal", "low")
PRIORITY_RANK = {"high": 0, "normal": 1, "low": 2}


def local_priorities() -> dict:
    p = db.kv_get("tasks:priority", {})
    return p if isinstance(p, dict) else {}


def set_local_priority(source, task_id, priority):
    if priority not in PRIORITIES:
        raise ValueError("Priority is high, normal or low")
    with db._lock:
        p = local_priorities()
        key = f"{source}:{task_id}"
        if priority == "normal":
            p.pop(key, None)
        else:
            p[key] = priority
        db.kv_set("tasks:priority", p)


def _apply_priorities(tasks, listed_sources):
    """Adds `priority` to every task (Outlook's comes with the task) and forgets local priorities of tasks that
    are gone from a source that was listed completely this time."""
    with db._lock:
        stored = local_priorities()
        for t in tasks:
            if t["source"] == "outlook":
                t.setdefault("priority", "normal")
            else:
                t["priority"] = stored.get(f"{t['source']}:{t['id']}", "normal")
        keep = {k: v for k, v in stored.items()
                if k.split(":", 1)[0] not in listed_sources or any(f"{t['source']}:{t['id']}" == k for t in tasks)}
        if len(keep) != len(stored):
            db.kv_set("tasks:priority", keep)


def sort_tasks(tasks) -> list:
    """High first, then Normal, then Low; within each by due date (no date last), then title."""
    return sorted(tasks, key=lambda t: (PRIORITY_RANK.get(t.get("priority"), 1), t["due"] is None, t["due"] or "",
                                        t["subject"].lower()))


# ---- snooze and no-reply reminders -------------------------------------------

# A snoozed email whose time has come stays in the same record with a "back" time: it's then the Follow-ups group
# "Back from snooze" until it's dealt with (reply, Done / dismiss, snoozed again) or BACK_DAYS have passed.
BACK_DAYS = 7


def snoozes() -> dict:
    """Every record, the emails that are back from snooze included."""
    return db.kv_get("snoozes", {})


def active_snoozes() -> dict:
    """Snoozed now (not yet back)."""
    return {k: v for k, v in snoozes().items() if not v.get("back")}


def snoozed_ids(now=None) -> set[str]:
    """Every email snoozed now: each record's own id and, for a snoozed conversation, the other emails in it ("also")."""
    now = now or datetime.now().isoformat(timespec="seconds")
    out = set()
    for k, v in snoozes().items():
        if not v.get("back") and v.get("until", "") > now:
            out.add(k)
            out.update(v.get("also") or [])
    return out


def back_ids() -> list[str]:
    """Emails back from snooze, newest back first."""
    return [k for k, v in sorted(snoozes().items(), key=lambda kv: kv[1].get("back") or "", reverse=True) if v.get("back")]


def clear_back(item_id):
    """Done with an email that was back from snooze (replied, dismissed, Done)."""
    s = snoozes()
    if s.get(item_id, {}).get("back"):
        s.pop(item_id)
        db.kv_set("snoozes", s)


def snooze(item_id, until, info):
    s = snoozes()
    was_back = (s.get(item_id) or {}).get("back")
    s[item_id] = {"until": until, **{k: info.get(k, "") for k in ("subject", "who", "kind")}}
    if info.get("also"):  # a whole conversation (Mail tab): its other emails are out of the way too; one comes back
        s[item_id]["also"] = [str(x) for x in info["also"] if x and x != item_id][:100]
    if was_back:
        s[item_id]["was_back"] = was_back  # so Unsnooze (Undo) puts it back in "Back from snooze"
    db.kv_set("snoozes", s)


def unsnooze(item_id):
    s = snoozes()
    rec = s.pop(item_id, None)
    if rec and rec.get("was_back") and not rec.get("back"):
        s[item_id] = {**{k: v for k, v in rec.items() if k != "was_back"}, "back": rec["was_back"]}
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
    gone = (datetime.now() - timedelta(days=BACK_DAYS)).isoformat(timespec="seconds")
    for item_id, info in list(s.items()):
        if info.get("back"):
            if info["back"] < gone:
                s.pop(item_id)  # back for a week and never dealt with
            continue
        if info["until"] <= now:
            if info.get("kind") == "task":
                s.pop(item_id)
            else:
                info["back"] = now  # an email: the Follow-ups group "Back from snooze" until it's dealt with
            alerts.raise_alert("Snooze", f"Back from snooze: {info['subject']}", info.get("who", ""), "normal",
                               {"type": "mail", "id": item_id} if info.get("kind") != "task" else {"type": "today"},
                               toast="item back from snooze")
    db.kv_set("snoozes", s)

    keep = []
    for w in reply_watches():
        if w["due"] > now:
            keep.append(w)
            continue
        if not await outlook.call(outlook.conversation_replied, w["conversation"], w["since"]):
            alerts.raise_alert("Follow-up", f"No reply yet: {w['subject']}", f"To {w['to'][:80]}", "normal",
                               {"type": "mail", "id": w["id"]}, toast="no reply yet")
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
                               {"type": "mail", "id": m["id"]}, toast="urgent reply needed")


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


def spam_hidden(item, blocked=None) -> bool:
    """True if a received message matches a spam block (spam.py). `blocked`: a matcher already in hand."""
    blocked = outlook._blocked() if blocked is None else blocked
    return bool(blocked and blocked.hit(item.get("sender", ""), item.get("subject", ""), item.get("sender_name", "")))


async def _followups(todo, refresh=False) -> dict:
    cfg = config.section("followups", {})
    if refresh or time.time() - _followup_cache.get("at", 0) > FOLLOWUP_CACHE_SECONDS:
        _followup_cache["data"] = await outlook.call(
            outlook.followups, cfg.get("awaiting_reply_days", 7), cfg.get("needs_reply_days", 5),
            cfg.get("min_wait_hours", 4), people.important_senders())
        _followup_cache["at"] = time.time()
    raw = _followup_cache["data"]
    now = datetime.now().isoformat(timespec="seconds")
    hidden_ids = set(dismissed()) | {i for i, v in snoozes().items() if v["until"] > now} | snoozed_ids(now)
    mutes = muted()
    # Spam blocks apply to the cached scan too, so a new block hides its mail at once (sent mail never matches).
    blocked = outlook._blocked() or False
    out = {k: [i for i in v if _keep(i, cfg, hidden_ids, mutes) and (k == "awaiting_reply" or not spam_hidden(i, blocked))]
           for k, v in raw.items()}
    important = people.important_senders()

    # Flagged mail: skip flags set before flagged_since, automated senders and old flags; overdue first, then newest.
    cutoff = (datetime.now() - timedelta(days=cfg.get("flagged_days", 30))).isoformat()
    automated = older = before = 0
    flagged = []
    for f in todo.get("flagged", []):
        if flagged_before_cutoff(f):
            before += 1
        elif AUTOMATED.search(f["sender_name"]):
            automated += 1
        elif (f["received"] or "") < cutoff and not (f["due"] and f["due"] >= date.today().isoformat()):
            older += 1
        elif _keep(f, cfg, hidden_ids, mutes) and not spam_hidden(f, blocked):
            flagged.append(f)
    flagged.sort(key=lambda f: f["received"] or "", reverse=True)
    out["flagged"] = flagged[:25]
    # Back from snooze: read fresh each time (a handful of emails), newest back first; an email that's gone (deleted,
    # moved) or spam-blocked drops out. It's listed here only, not also in the group it would otherwise be in.
    out["back"] = []
    snoozed = snoozes()
    gone = set(dismissed())  # Done / dismissed (its Undo brings it back); the record itself expires after BACK_DAYS
    ids = [i for i in back_ids() if i not in gone][:20]
    if ids:
        try:
            rows = {r["id"]: r for r in await outlook.call(outlook._summaries, ids)}
            out["back"] = [{**rows[i], "back": snoozed[i]["back"]} for i in ids
                           if i in rows and not spam_hidden(rows[i], blocked)
                           and not any(m in f"{rows[i]['sender']} {rows[i]['sender_name']}".lower() for m in mutes)]
        except Exception as e:
            log.warning("Back from snooze: couldn't read them (%s)", type(e).__name__)
        back = {m["id"] for m in out["back"]}
        for k in ("urgent_reply", "needs_reply", "teams_reply", "flagged", "unread_vip"):
            out[k] = [i for i in out.get(k, []) if i.get("id") not in back]
    # The star on received items (from the list as it is now, so a change shows before the next scan; the
    # "Important senders" group itself is rescanned: the routes that change the list clear the cache).
    for k, items in out.items():
        if k not in ("awaiting_reply", "teams_reply"):
            out[k] = [{**i, "important": people.is_important(i.get("sender", ""), important)} for i in items]
    out["flagged_hidden"] = {"automated": automated, "older": older, "more": max(0, len(flagged) - 25),
                             "before": before, "since": flagged_since(), "total": len(todo.get("flagged", []))}
    return out


def flagged_since() -> str:
    """followups.flagged_since (YYYY-MM-DD): flags set before that day are Outlook's old backlog, ignored by CC."""
    since = str(config.section("followups", {}).get("flagged_since") or "")
    return since if re.fullmatch(r"\d{4}-\d{2}-\d{2}", since) else ""


def flagged_before_cutoff(f: dict) -> bool:
    since = flagged_since()  # when it was flagged; mail with no such date goes by when it arrived
    return bool(since) and (f.get("flagged_at") or f.get("received") or "")[:10] < since


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
    listed = set()
    if asana_task:
        asana_rows = await asana_task
        if isinstance(asana_rows, dict):
            asana_state["error"] = asana_rows["error"]
        else:
            tasks += asana_rows
            if len(asana_rows) < asana.TASK_LIMIT:  # a full page may not be all of them: keep their priorities
                listed.add("asana")
    await asyncio.to_thread(_apply_priorities, tasks, listed)  # before snoozes: a snoozed task keeps its priority
    snoozed_now = {i for i, v in snoozes().items() if v["until"] > datetime.now().isoformat(timespec="seconds")}
    tasks = sort_tasks(t for t in tasks if t["id"] not in snoozed_now)

    return {
        "now": datetime.now().isoformat(timespec="seconds"),
        "weather": await weather_task,
        "agenda": agenda,
        "tasks": {"items": tasks, "asana": asana_state, "error": todo.get("error")},
        "followups": followups,
        "snoozed": len(active_snoozes()),
        "reply_watches": len(reply_watches()),
    }


def summary(data) -> str:
    today = date.today().isoformat()
    agenda = data["agenda"] if isinstance(data["agenda"], list) else []
    remaining = [m for m in agenda if m["end"] >= datetime.now().isoformat() and not m.get("lunch")]
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


# ---- end-of-day carry over --------------------------------------------------------------------------------
# The wrap-up's Carry over (and Ctrl+K): replies owed are snoozed to the next workday's start (they come back as
# "Back from snooze"), tasks due today or overdue move to that day. Only counts are kept (kv today:carryover, one
# record, replaced each time), so the next morning's briefing can start with them.
CARRY_KV = "today:carryover"


def carried_over_text(day: str) -> str:
    c = db.kv_get(CARRY_KV) or {}
    if c.get("for") != day:
        return ""
    parts = [f"{c[k]} {name}{'' if c[k] == 1 else 's'}" for k, name in (("mail", "email"), ("tasks", "task"), ("tickets", "Pulse ticket"))
             if c.get(k)]
    return f"Carried over: {', '.join(parts)}" if parts else ""


async def carry_over(mail: list[dict], tasks: list[dict], until: str, day: str, tickets: int = 0) -> dict:
    done_mail, done_tasks, failed = 0, 0, []
    for m in mail[:100]:
        snooze(m["id"], until, {"subject": m.get("subject", ""), "who": m.get("who", ""), "kind": "mail"})
        done_mail += 1
    for t in tasks[:100]:
        try:
            if t.get("source") == "asana":
                await asyncio.to_thread(asana.set_due, t["id"], day)
            else:
                await outlook.call(outlook.set_task_due, t["id"], day)
            done_tasks += 1
        except Exception as e:
            log.warning("Carry over: a task wasn't moved (%s)", type(e).__name__)
            failed.append(t.get("subject") or "a task")
    db.kv_set(CARRY_KV, {"for": day, "mail": done_mail, "tasks": done_tasks, "tickets": max(0, int(tickets or 0)),
                         "at": db.now()})
    log.info("Carried over to %s: %d emails, %d tasks, %d failed", day, done_mail, done_tasks, len(failed))
    return {"mail": done_mail, "tasks": done_tasks, "failed": failed}


async def _briefing(now):
    data = await build(refresh=True)
    agenda = data["agenda"] if isinstance(data["agenda"], list) else []
    first = next((m for m in agenda if m["start"] >= now.isoformat(timespec="minutes") and not m.get("lunch")), None)
    body = f"First up: {first['subject']} at {datetime.fromisoformat(first['start']):%I:%M %p}" if first else ""
    carried = carried_over_text(date.today().isoformat())
    body = " · ".join(x for x in (carried, body) if x)
    alerts.raise_alert("Today", summary(data), body, "normal", {"type": "today"}, toast="morning briefing")


async def _wrapup(now):
    data = await build(refresh=True)
    next_day = 3 if now.weekday() == 4 else 1  # Friday wraps up to Monday
    tomorrow = [m for m in await outlook.call(outlook.meetings_for_day, next_day) if not m.get("lunch")]
    today_iso = date.today().isoformat()
    open_due = [t for t in data["tasks"]["items"] if t["due"] and t["due"] <= today_iso]
    f = data["followups"] if "error" not in data["followups"] else {}
    label = "Monday" if next_day == 3 else "Tomorrow"
    first = tomorrow[0] if tomorrow else None
    parts = [f"{len(open_due)} task(s) still due", f"{len(f.get('needs_reply', []))} reply(ies) owed",
             f"{len(f.get('awaiting_reply', []))} waiting on others"]
    body = (f"{label}: {len(tomorrow)} meeting(s), first is {first['subject']} at "
            f"{datetime.fromisoformat(first['start']):%I:%M %p}" if first else f"{label}: no meetings")
    alerts.raise_alert("Wrap-up", " · ".join(parts), body, "normal", {"type": "wrapup"}, toast="end-of-day wrap-up")
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
