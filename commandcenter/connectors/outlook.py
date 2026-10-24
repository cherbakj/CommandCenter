"""Outlook connector: drives the classic Outlook desktop app over COM, as the signed-in user.

COM objects must stay on the thread that created them, so every call runs on one
dedicated worker thread via `call()`.
"""
import asyncio
import html
import logging
import re
import subprocess
import threading
import winreg
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pythoncom
import pywintypes
import win32com.client

from .. import alerts, config, db, people

log = logging.getLogger(__name__)

OL_FOLDER_SENT = 5
OL_FOLDER_INBOX = 6
OL_FOLDER_CALENDAR = 9
OL_FOLDER_TODO = 28
OL_MAIL = 43
OL_TASK = 48
OL_TASK_ITEM = 3
OL_APPOINTMENT_ITEM = 1
OL_BUSY = 2
OL_MARK_NO_DATE = 4
OL_FLAG_COMPLETE = 1
OL_IMPORTANCE_HIGH = 2
OL_FORMAT_HTML = 2
NO_DATE_YEAR = 4501  # Outlook's "None" date is 1/1/4501
OL_FOLDER_TASKS = 13
PR_FLAG_STATUS = "http://schemas.microsoft.com/mapi/proptag/0x10900003"
PR_SENDER_NAME = "http://schemas.microsoft.com/mapi/proptag/0x0C1A001F"
PR_DELIVERY_TIME = "http://schemas.microsoft.com/mapi/proptag/0x0E060040"
PID_TASK_DUE = "http://schemas.microsoft.com/mapi/id/{00062003-0000-0000-C000-000000000046}/81050040"
PR_HEADERS = "http://schemas.microsoft.com/mapi/proptag/0x007D001F"
PR_LAST_VERB = "http://schemas.microsoft.com/mapi/proptag/0x10810003"
PR_SENDER_SMTP = "http://schemas.microsoft.com/mapi/proptag/0x5D01001F"         # PR_SENDER_SMTP_ADDRESS
PR_SENT_REPR_SMTP = "http://schemas.microsoft.com/mapi/proptag/0x5D02001F"      # PR_SENT_REPRESENTING_SMTP_ADDRESS
PR_SENDER_EMAIL = "http://schemas.microsoft.com/mapi/proptag/0x0C1F001F"        # SMTP or an Exchange DN
# Links in a plain-text preview: http(s)/www URLs (Safe Links included), with Outlook's <...> around them.
PREVIEW_URL = re.compile(r"<?\b(?:https?://|www\.)[^\s<>]+>?", re.I)
REPLIED_VERBS = (102, 103, 104)  # reply, reply all, forward

_executor = ThreadPoolExecutor(1, initializer=pythoncom.CoInitialize, thread_name_prefix="outlook")
_local = threading.local()
TEAMS_LINK = re.compile(r"https://teams\.microsoft\.com/l/meetup-join/[^\s\"'<>]+")


async def call(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(_executor, _guarded, fn, args)


def _guarded(fn, args):
    try:
        return fn(*args)
    except pywintypes.com_error:
        _local.ns = None  # Outlook may have restarted; reconnect next time
        raise


class OutlookClosed(Exception):
    pass


def is_running() -> bool:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq OUTLOOK.EXE", "/NH"], capture_output=True,
                         text=True, creationflags=subprocess.CREATE_NO_WINDOW).stdout
    return "outlook.exe" in out.lower()


def launch():
    """Starts the Outlook desktop app (visible), found through its App Paths registration."""
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\OUTLOOK.EXE") as key:
                path = winreg.QueryValue(key, None)
            subprocess.Popen([path], creationflags=subprocess.DETACHED_PROCESS, close_fds=True)
            return
        except OSError:
            continue
    raise OutlookClosed("Couldn't find Outlook to start it")


def _ns():
    if getattr(_local, "ns", None) is None:
        # Dispatch would silently start a hidden Outlook, so only attach to one that's running.
        if not is_running():
            raise OutlookClosed("Outlook is closed. Open Outlook to use mail, calendar and tasks.")
        _local.ns = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
    return _local.ns


def _local_time(value) -> datetime:
    # pywin32 labels Outlook's local times as UTC; drop the tz to keep them local.
    return datetime(value.year, value.month, value.day, value.hour, value.minute, value.second)


def _sender_smtp(item) -> str:
    try:
        if item.SenderEmailType == "EX":
            user = item.Sender.GetExchangeUser()
            if user:
                return user.PrimarySmtpAddress or ""
        return item.SenderEmailAddress or ""
    except pywintypes.com_error:
        return ""


def _summary(item) -> dict:
    return {
        "id": item.EntryID,
        "subject": item.Subject or "(no subject)",
        "sender_name": item.SenderName or "",
        "sender": _sender_smtp(item),
        "received": _local_time(item.ReceivedTime).isoformat(timespec="minutes"),
        "unread": bool(item.UnRead),
        "high_importance": item.Importance == OL_IMPORTANCE_HIGH,
        "flagged": bool(item.IsMarkedAsTask),
        "preview": _preview(item.Body),
    }


def _preview(body) -> str:
    """First ~300 characters of the body as one line, without tracking / Safe Links URLs."""
    text = PREVIEW_URL.sub(" ", (body or "")[:3000])
    return re.sub(r"\s+", " ", text).strip()[:300]


def _date_or_none(value):
    if value is None or value.year >= NO_DATE_YEAR:
        return None
    return _local_time(value).date().isoformat()


def _prop(item, tag):
    try:
        return item.PropertyAccessor.GetProperty(tag)
    except pywintypes.com_error:
        return None


def _iter_inbox(limit):
    return _iter_folder(OL_FOLDER_INBOX, "[ReceivedTime]", limit)


def _iter_folder(folder, sort_field, limit):
    items = _ns().GetDefaultFolder(folder).Items
    items.Sort(sort_field, True)
    item, n = items.GetFirst(), 0
    while item is not None and n < limit:
        if item.Class == OL_MAIL:
            yield item
            n += 1
        item = items.GetNext()


# ---- reads -------------------------------------------------------------------

def recent_mail(limit=50, unread_only=False) -> list[dict]:
    out = []
    for item in _iter_inbox(limit * 4 if unread_only else limit):
        if unread_only and not item.UnRead:
            continue
        out.append(_summary(item))
        if len(out) >= limit:
            break
    return out


def get_message(entry_id) -> dict:
    item = _ns().GetItemFromID(entry_id)
    d = _summary(item)
    d.update(to=item.To or "", cc=item.CC or "", body=item.Body or "")
    return d


def upcoming_meetings(hours=24) -> list[dict]:
    return _meetings(datetime.now(), datetime.now() + timedelta(hours=hours))


def meetings_today() -> list[dict]:
    return meetings_for_day(0)


def meetings_for_day(offset_days=0) -> list[dict]:
    day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=offset_days)
    return _meetings(day, day + timedelta(days=1))


def current_focus_block(keywords) -> dict | None:
    """The calendar event happening now whose subject contains a focus keyword, if any."""
    now = datetime.now()
    for m in _meetings(now, now + timedelta(minutes=1)):
        if any(k.lower() in m["subject"].lower() for k in keywords):
            return m
    return None


OL_BUSY_STATUS = {0: "free", 1: "tentative", 2: "busy", 3: "out of office", 4: "working elsewhere"}


def _meetings(start, end, limit=50, all_day=False) -> list[dict]:
    items = _ns().GetDefaultFolder(OL_FOLDER_CALENDAR).Items
    items.Sort("[Start]")
    items.IncludeRecurrences = True
    fmt = "%m/%d/%Y %I:%M %p"
    found = items.Restrict(f"[Start] <= '{end.strftime(fmt)}' AND [End] >= '{start.strftime(fmt)}'")
    out, item = [], found.GetFirst()
    while item is not None and len(out) < limit:
        if all_day or not item.AllDayEvent:
            body = item.Body or ""
            link = TEAMS_LINK.search(body)
            out.append({
                "id": item.EntryID,
                "subject": item.Subject or "(no subject)",
                "start": _local_time(item.Start).isoformat(timespec="minutes"),
                "end": _local_time(item.End).isoformat(timespec="minutes"),
                "location": item.Location or "",
                "organizer": item.Organizer or "",
                "join_url": link.group(0) if link else None,
                "all_day": bool(item.AllDayEvent),
                "status": OL_BUSY_STATUS.get(item.BusyStatus, ""),
            })
        item = found.GetNext()
    return out


def meetings_range(days=10) -> list[dict]:
    """Calendar items (including all-day ones) from the start of today through `days` days."""
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return _meetings(start, start + timedelta(days=days), limit=400, all_day=True)


# ---- actions -----------------------------------------------------------------

def reply(entry_id, text, reply_all=False, send=True) -> dict:
    """Returns the conversation and recipients, so a no-reply reminder can watch the thread."""
    item = _ns().GetItemFromID(entry_id)
    msg = item.ReplyAll() if reply_all else item.Reply()
    if msg.BodyFormat == OL_FORMAT_HTML:
        snippet = "<div>" + html.escape(text).replace("\n", "<br>") + "</div><br>"
        msg.HTMLBody = re.sub(r"(<body[^>]*>)", lambda m: m.group(1) + snippet, msg.HTMLBody, count=1, flags=re.I)
    else:
        msg.Body = text + "\n\n" + msg.Body
    info = {"conversation": msg.ConversationID, "subject": msg.Subject or "", "to": msg.To or ""}
    if send:
        msg.Send()
    else:
        msg.Display(False)
    return info


def conversation_of(entry_id) -> dict:
    item = _ns().GetItemFromID(entry_id)
    return {"conversation": item.ConversationID, "subject": item.Subject or "", "to": item.To or ""}


def conversation_replied(conversation_id, since_iso) -> bool:
    """True if anything arrived in the inbox for this conversation after `since_iso`."""
    since = datetime.fromisoformat(since_iso)
    for item in _iter_inbox(2000):
        if _local_time(item.ReceivedTime) < since:
            return False
        if item.ConversationID == conversation_id:
            return True
    return False


def create_block(subject, start_iso, minutes, notes="") -> str:
    """Books time on your calendar (shown as busy, 5-minute reminder)."""
    appt = _ns().Application.CreateItem(OL_APPOINTMENT_ITEM)
    appt.Subject = subject
    appt.Start = datetime.fromisoformat(start_iso)
    appt.Duration = int(minutes)
    appt.BusyStatus = OL_BUSY
    appt.ReminderSet = True
    appt.ReminderMinutesBeforeStart = 5
    if notes:
        appt.Body = notes
    appt.Save()
    return appt.EntryID


RESPONSES = {0: "none", 1: "organizer", 2: "tentative", 3: "accepted", 4: "declined", 5: "no response"}


def _agenda_text(body) -> str:
    # Drop the Teams join boilerplate that Outlook appends to meeting bodies.
    body = re.split(r"_{10,}|Microsoft Teams (?:meeting|Need help)|Join on your computer", body or "", maxsplit=1)[0]
    return re.sub(r"\n{3,}", "\n\n", body).strip()[:800]


def meeting_prep(entry_id, days=21) -> dict:
    """Attendees, the meeting's own notes, and recent mail with the attendees."""
    item = _ns().GetItemFromID(entry_id)
    _, me_smtp = _me()
    attendees = []
    for r in item.Recipients:
        if r.Type == 3:  # room / resource
            continue
        email = ""
        try:
            entry = r.AddressEntry
            user = entry.GetExchangeUser() if entry else None
            email = (user.PrimarySmtpAddress if user else r.Address) or ""
        except pywintypes.com_error:
            email = r.Address or ""
        key = (email or r.Name or "").lower()
        if email.lower() == me_smtp or any((a["email"] or a["name"]).lower() == key for a in attendees):
            continue
        attendees.append({"name": r.Name or email, "email": email, "optional": r.Type == 2,
                          "response": RESPONSES.get(r.MeetingResponseStatus, "none")})

    recent = []
    names = [a["name"].replace("'", "''") for a in attendees if a["name"]][:8]
    if names:
        since = (datetime.now() - timedelta(days=days)).strftime("%m/%d/%Y %I:%M %p")
        who_in = " OR ".join(f'"urn:schemas:httpmail:fromname" LIKE \'%{n}%\'' for n in names)
        who_out = " OR ".join(f'"urn:schemas:httpmail:displayto" LIKE \'%{n}%\'' for n in names)
        recent = (_mail_rows(OL_FOLDER_INBOX, f'@SQL=("urn:schemas:httpmail:datereceived" >= \'{since}\') AND ({who_in})', "ReceivedTime", 8)
                  + _mail_rows(OL_FOLDER_SENT, f'@SQL=("urn:schemas:httpmail:date" >= \'{since}\') AND ({who_out})', "SentOn", 8))
        recent = sorted(recent, key=lambda m: m["received"] or "", reverse=True)[:10]
    link = TEAMS_LINK.search(item.Body or "")
    # A recurring series' EntryID opens the series, whose Start is the first occurrence: leave times to the caller.
    times = {} if item.IsRecurring else {"start": _local_time(item.Start).isoformat(timespec="minutes"),
                                         "end": _local_time(item.End).isoformat(timespec="minutes")}
    return {"id": entry_id, "subject": item.Subject or "(no subject)", "organizer": item.Organizer or "",
            "location": item.Location or "", "join_url": link.group(0) if link else None, **times,
            "agenda": _agenda_text(item.Body), "attendees": attendees, "recent_mail": recent}


def set_read(entry_id, read=True):
    item = _ns().GetItemFromID(entry_id)
    item.UnRead = not read
    item.Save()


def set_flag(entry_id, flagged=True):
    item = _ns().GetItemFromID(entry_id)
    if flagged:
        item.MarkAsTask(OL_MARK_NO_DATE)
    else:
        item.ClearTaskFlag()
    item.Save()


def open_item(entry_id):
    _ns().GetItemFromID(entry_id).Display(False)


# ---- tasks and follow-ups ----------------------------------------------------

def _table_rows(folder_id, where, columns, limit=10000):
    # Folder.GetTable reads thousands of rows in well under a second; walking Items does not.
    table = _ns().GetDefaultFolder(folder_id).GetTable(where)
    table.Columns.RemoveAll()
    for c in columns:
        table.Columns.Add(c)
    return table.GetArray(limit) or ()


def _utc_to_local(value):
    if value is None:
        return None
    utc = datetime(value.year, value.month, value.day, value.hour, value.minute, value.second, tzinfo=timezone.utc)
    return utc.astimezone().replace(tzinfo=None)


def todo_items() -> dict:
    """Open Outlook/To Do tasks, and flagged mail whose flag isn't complete."""
    tasks = [{
        "source": "outlook", "id": r[0], "subject": r[1] or "(untitled)",
        "due": _date_or_none(r[2]), "important": r[3] == OL_IMPORTANCE_HIGH,
    } for r in _table_rows(OL_FOLDER_TASKS, "[Complete] = False", ["EntryID", "Subject", "DueDate", "Importance"])]

    flagged = []
    # The To-Do search folder ignores GetTable filters, so filter the rows here.
    base = ["EntryID", "Subject", "MessageClass", PR_FLAG_STATUS, PR_SENDER_NAME, PR_DELIVERY_TIME, PID_TASK_DUE]
    try:
        rows = _table_rows(OL_FOLDER_TODO, "", base + [PR_SENDER_SMTP, PR_SENT_REPR_SMTP, PR_SENDER_EMAIL])
    except pywintypes.com_error:  # a store that can't add these columns: names only
        rows = [tuple(r) + (None, None, None) for r in _table_rows(OL_FOLDER_TODO, "", base)]
    for entry_id, subject, msg_class, flag, sender, received, due, smtp, repr_smtp, email in rows:
        if flag != 2 or not (msg_class or "").startswith("IPM.Note"):
            continue
        received = _utc_to_local(received)
        address = next((a for a in (smtp, repr_smtp, email) if isinstance(a, str) and "@" in a), "")
        flagged.append({
            "id": entry_id, "subject": subject or "(no subject)", "sender_name": sender or "", "sender": address,
            "due": due.date().isoformat() if due and due.year < NO_DATE_YEAR else None,
            "received": received.isoformat(timespec="minutes") if received else None,
        })
    return {"tasks": tasks, "flagged": flagged}


def create_task(subject, due=None, notes="") -> str:
    task = _ns().Application.CreateItem(OL_TASK_ITEM)
    task.Subject = subject
    if due:
        task.DueDate = datetime.fromisoformat(due)
    if notes:
        task.Body = notes
    task.Save()
    return task.EntryID


def complete_item(entry_id):
    """Completes an Outlook task, or marks a flagged email's flag complete."""
    item = _ns().GetItemFromID(entry_id)
    if item.Class == OL_TASK:
        item.MarkComplete()
    else:
        item.FlagStatus = OL_FLAG_COMPLETE
        item.Save()


def _me() -> tuple[str, str]:
    ns = _ns()
    smtp = ""
    try:
        smtp = ns.Accounts.Item(1).SmtpAddress or ""
    except pywintypes.com_error:
        pass
    return (ns.CurrentUser.Name or "").lower(), smtp.lower()


def _sender_matches(sender, patterns) -> bool:
    sender = sender.lower()
    return any(sender.endswith(p.lower()) if p.startswith("@") else sender == p.lower() for p in patterns)


def _is_bulk(item, sender) -> bool:
    if re.search(r"no-?reply|donotreply|notifications?@|mailer-daemon", sender):
        return True
    headers = (_prop(item, PR_HEADERS) or "").lower()
    return "list-unsubscribe:" in headers or "precedence: bulk" in headers or "precedence: list" in headers


# Messages that never need a reply even from a coworker: auto-replies, sharing notices, meeting responses.
NO_REPLY_SUBJECT = re.compile(
    r"^(automatic reply|auto[- ]?reply|out of office|undeliverable|delivery has failed|accepted:|declined:|"
    r"tentative:|canceled:|cancelled:)|\bshared (?:the|a) (?:file|folder)\b|\binvited you to\b", re.I)
TEAMS_NOTIFY_DOMAIN = "teams.microsoft.com"
TEAMS_SUBJECT_PATTERNS = [
    re.compile(r"^(?P<name>.+?) (?:sent you|mentioned you|replied to|messaged you|is trying to reach you)", re.I),
    re.compile(r"new messages? from (?P<name>.+?)(?: in .*)?$", re.I),
    re.compile(r"^(?:missed|new) (?:chat|message) from (?P<name>.+)$", re.I),
]


def teams_sender(subject) -> str:
    """The person's name from a Teams missed-activity email subject, or "" if it can't be read."""
    for pattern in TEAMS_SUBJECT_PATTERNS:
        m = pattern.search(subject or "")
        if m:
            return m.group("name").strip(" :")
    return ""


def is_teams_notification(sender_smtp) -> bool:
    return (sender_smtp or "").lower().endswith(TEAMS_NOTIFY_DOMAIN)


def followups(awaiting_days=7, reply_days=5, min_wait_hours=4, vip_senders=()) -> dict:
    """Sent mail nobody answered, mail you haven't replied to (internal senders by default, urgent people
    split out), unread Teams missed-activity emails, and unread mail from VIP senders."""
    internal_only = config.section("followups", {}).get("needs_reply_scope", "internal") == "internal"
    urgent = people.urgent_people()
    me_name, me_smtp = _me()
    now = datetime.now()
    horizon = now - timedelta(days=max(awaiting_days, reply_days) + 7)

    # Latest message I sent in each conversation, plus the candidates for "awaiting reply".
    sent_latest, sent_candidates = {}, {}
    for item in _iter_folder(OL_FOLDER_SENT, "[SentOn]", 1500):
        sent_on = _local_time(item.SentOn)
        if sent_on < horizon:
            break
        conv = item.ConversationID
        sent_latest.setdefault(conv, sent_on)  # newest first, so the first one seen is the latest
        to = (item.To or "").strip()
        if (conv not in sent_candidates and sent_on >= now - timedelta(days=awaiting_days)
                and sent_on <= now - timedelta(hours=min_wait_hours) and to and to.lower() != me_name):
            sent_candidates[conv] = {"id": item.EntryID, "subject": item.Subject or "(no subject)",
                                     "to": to, "sent": sent_on.isoformat(timespec="minutes"), "_sent": sent_on}

    inbox_latest, needs_reply, urgent_reply, teams_reply, unread_vip, reply_seen = {}, [], [], [], [], set()
    recent = now - timedelta(days=reply_days)
    for item in _iter_inbox(3000):
        received = _local_time(item.ReceivedTime)
        if received < horizon:
            break
        conv = item.ConversationID
        inbox_latest.setdefault(conv, received)
        sender = _sender_smtp(item).lower()
        if sender == me_smtp:
            continue
        # Teams missed-activity emails stay pending until you mark them done (read) or dismiss them.
        if is_teams_notification(sender):
            if item.UnRead and received >= recent and len(teams_reply) < 25:
                who = teams_sender(item.Subject)
                teams_reply.append({**_summary(item), "from_name": who, "urgent": people.matches("", who, urgent)})
            continue
        if item.UnRead and vip_senders and _sender_matches(sender, vip_senders) and len(unread_vip) < 25:
            unread_vip.append(_summary(item))
        if received < recent or conv in reply_seen:
            continue
        is_urgent = people.matches(sender, item.SenderName or "", urgent)
        # Urgent people count even when you're only on CC.
        addressed = (item.To or "").lower() + (";" + (item.CC or "").lower() if is_urgent else "")
        if not (me_name and me_name in addressed):
            continue
        reply_seen.add(conv)  # only judge the newest message in each conversation
        if internal_only and not is_urgent and not people.is_internal(sender):
            continue
        replied = _prop(item, PR_LAST_VERB) in REPLIED_VERBS or sent_latest.get(conv, datetime.min) > received
        if replied or _is_bulk(item, sender) or NO_REPLY_SUBJECT.search(item.Subject or ""):
            continue
        summary = {**_summary(item), "internal": people.is_internal(sender), "urgent": is_urgent}
        if is_urgent and len(urgent_reply) < 25:
            urgent_reply.append(summary)
        elif not is_urgent and len(needs_reply) < 25:
            needs_reply.append(summary)

    awaiting = []
    for conv, s in sent_candidates.items():
        if inbox_latest.get(conv, datetime.min) <= s.pop("_sent"):
            awaiting.append(s)
    teams_reply.sort(key=lambda t: not t["urgent"])  # urgent people first
    return {"urgent_reply": urgent_reply, "needs_reply": needs_reply, "teams_reply": teams_reply,
            "awaiting_reply": awaiting[:25], "unread_vip": unread_vip}


# ---- alert rules -------------------------------------------------------------

def _matches(rule, mail, body_lower) -> bool:
    checks = []
    if rule.get("senders"):
        checks.append(_sender_matches(mail["sender"], rule["senders"]))
    if rule.get("keywords"):
        text = mail["subject"].lower() + "\n" + body_lower
        checks.append(any(k.lower() in text for k in rule["keywords"]))
    if rule.get("high_importance"):
        checks.append(mail["high_importance"])
    if rule.get("flagged"):
        checks.append(mail["flagged"])
    return bool(checks) and all(checks)


def scan_new_mail() -> list[tuple[dict, dict]]:
    """Returns (mail, rule) pairs for mail received since the last scan."""
    cfg = config.section("outlook", {})
    rules = cfg.get("rules") or []
    started = datetime.now()
    watermark = db.kv_get("outlook:watermark")
    db.kv_set("outlook:watermark", started.isoformat())
    if watermark is None:  # first run: don't alert on the existing inbox
        return []
    since = datetime.fromisoformat(watermark) - timedelta(minutes=5)

    hits = []
    urgent = people.urgent_people()
    for item in _iter_inbox(200):
        if _local_time(item.ReceivedTime) < since:
            break
        mail = _summary(item)
        if db.seen("mail:" + mail["id"]):
            continue
        # Urgent people beat every other rule: high priority, with sound.
        if urgent:
            if is_teams_notification(mail["sender"]):
                who = teams_sender(mail["subject"])
                if people.matches("", who, urgent):
                    hits.append((mail, {"name": f"Urgent Teams message from {who}", "priority": "high"}))
                    continue
            elif people.matches(mail["sender"], mail["sender_name"], urgent):
                hits.append((mail, {"name": "Urgent reply needed", "priority": "high"}))
                continue
        body_lower = (item.Body or "")[:20000].lower()
        for rule in rules:
            if _matches(rule, mail, body_lower):
                hits.append((mail, rule))
                break  # first matching rule wins
    return hits


def scan_meetings() -> list[tuple[dict, int]]:
    reminders = sorted(config.section("outlook", {}).get("meetings", {}).get("remind_minutes", [10, 1]))
    if not reminders:
        return []
    due = []
    now = datetime.now()
    for m in upcoming_meetings(hours=max(reminders) / 60 + 1):
        minutes = (datetime.fromisoformat(m["start"]) - now).total_seconds() / 60
        if minutes < -1:
            continue
        hit = next((r for r in reminders if minutes <= r), None)
        if hit is not None and not db.seen(f"mtg:{m['id']}:{m['start']}:{hit}"):
            due.append((m, max(0, round(minutes))))
    return due


async def watch_loop():
    cfg = config.section("outlook", {})
    if not cfg.get("enabled", True):
        return
    poll = cfg.get("poll_seconds", 60)
    was_running = True
    while True:
        if not await asyncio.to_thread(is_running):
            if was_running:
                if cfg.get("auto_start", True):
                    try:
                        await asyncio.to_thread(launch)
                        alerts.raise_alert("Outlook", "Outlook was closed, so I restarted it",
                                           "Mail, calendar and task alerts need Outlook running.", "normal")
                    except Exception as e:
                        alerts.raise_alert("Outlook", "Outlook is closed and couldn't be restarted", str(e), "high")
                else:
                    alerts.raise_alert("Outlook", "Outlook is closed", "Mail and calendar alerts are paused.", "normal")
            was_running = False
            db.kv_set("outlook:status", {"ok": False, "checked": db.now(), "error": "Outlook is closed"})
            await asyncio.sleep(poll)
            continue
        was_running = True
        try:
            await _update_focus_block(cfg.get("focus_keywords", ["focus"]))
            for mail, rule in await call(scan_new_mail):
                alerts.raise_alert(
                    "Outlook",
                    f"{rule.get('name', 'Mail')}: {mail['subject']}",
                    f"From {mail['sender_name']} - {mail['preview'][:150]}",
                    rule.get("priority", "normal"),
                    {"type": "mail", "id": mail["id"]},
                )
            for m, minutes in await call(scan_meetings):
                when = "now" if minutes == 0 else f"in {minutes} min"
                action = {"type": "meeting", "id": m["id"], "join_url": m["join_url"], "prep": True}
                alerts.raise_alert("Calendar", f"{m['subject']} starts {when}", m["location"], "high", action)
            db.kv_set("outlook:status", {"ok": True, "checked": db.now()})
        except Exception as e:
            log.exception("Outlook scan failed")
            db.kv_set("outlook:status", {"ok": False, "checked": db.now(), "error": str(e)})
        await asyncio.sleep(poll)


async def _update_focus_block(keywords):
    if not keywords:
        return
    block = await call(current_focus_block, keywords)
    if not block:
        return
    key = f"{block['id']}:{block['start']}"
    if key == db.kv_get("focus:skip") or key == (db.kv_get("focus:calendar") or {}).get("key"):
        return
    db.kv_set("focus:calendar", {"key": key, "subject": block["subject"], "until": block["end"] + ":00"})
    until = datetime.fromisoformat(block["end"]).strftime("%I:%M %p").lstrip("0")
    alerts.raise_alert("Focus", f"Focus mode on until {until}", block["subject"], "low")


# ---- search ------------------------------------------------------------------

def _mail_rows(folder_id, where, date_field, limit) -> list[dict]:
    table = _ns().GetDefaultFolder(folder_id).GetTable(where)
    table.Columns.RemoveAll()
    for c in ("EntryID", "Subject", "SenderName", date_field, "UnRead", "To"):
        table.Columns.Add(c)
    table.Sort(f"[{date_field}]", True)
    out = []
    while not table.EndOfTable and len(out) < limit:
        entry_id, subject, sender, when, unread, to = table.GetNextRow().GetValues()
        out.append({"id": entry_id, "subject": subject or "(no subject)", "sender_name": sender or "",
                    "to": to or "", "received": _local_time(when).isoformat(timespec="minutes") if when else None,
                    "unread": bool(unread), "folder": "Sent" if folder_id == OL_FOLDER_SENT else "Inbox"})
    return out


def search_mail(query, limit=15) -> list[dict]:
    q = query.replace("'", "''")
    where = (f'@SQL=("urn:schemas:httpmail:subject" LIKE \'%{q}%\' OR '
             f'"urn:schemas:httpmail:fromname" LIKE \'%{q}%\' OR "urn:schemas:httpmail:displayto" LIKE \'%{q}%\')')
    found = _mail_rows(OL_FOLDER_INBOX, where, "ReceivedTime", limit) + _mail_rows(OL_FOLDER_SENT, where, "SentOn", limit)
    return sorted(found, key=lambda m: m["received"] or "", reverse=True)[:limit]


def teams_activity(limit=20) -> list[dict]:
    """Teams' missed-activity emails (chats, mentions) from the inbox."""
    where = '@SQL="urn:schemas:httpmail:fromemail" LIKE \'%teams.microsoft.com%\''
    return _mail_rows(OL_FOLDER_INBOX, where, "ReceivedTime", limit)
