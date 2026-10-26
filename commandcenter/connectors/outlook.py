"""Outlook connector: drives the classic Outlook desktop app over COM, as the signed-in user.

COM objects must stay on the thread that created them, so every call runs on one
dedicated worker thread via `call()`.
"""
import asyncio
import ctypes
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
import win32gui
import win32process

from .. import alerts, config, db, people

log = logging.getLogger(__name__)

OL_FOLDER_SENT = 5
OL_FOLDER_INBOX = 6
OL_FOLDER_CALENDAR = 9
OL_FOLDER_TODO = 28
OL_FOLDER_JUNK = 23
OL_MAIL = 43
OL_TASK = 48
OL_TASK_ITEM = 3
OL_APPOINTMENT_ITEM = 1
OL_BUSY = 2
OL_MARK_NO_DATE = 4
OL_FLAG_COMPLETE = 1
OL_IMPORTANCE_HIGH = 2
# Task priority <-> Outlook Importance (olImportanceLow 0 / Normal 1 / High 2).
PRIORITY_IMPORTANCE = {"low": 0, "normal": 1, "high": 2}
IMPORTANCE_PRIORITY = {v: k for k, v in PRIORITY_IMPORTANCE.items()}
OL_FORMAT_HTML = 2
NO_DATE_YEAR = 4501  # Outlook's "None" date is 1/1/4501
OL_FOLDER_TASKS = 13
PR_FLAG_STATUS = "http://schemas.microsoft.com/mapi/proptag/0x10900003"
PR_SENDER_NAME = "http://schemas.microsoft.com/mapi/proptag/0x0C1A001F"
PR_DELIVERY_TIME = "http://schemas.microsoft.com/mapi/proptag/0x0E060040"
PID_TASK_DUE = "http://schemas.microsoft.com/mapi/id/{00062003-0000-0000-C000-000000000046}/81050040"
PID_TODO_ORDINAL = "http://schemas.microsoft.com/mapi/id/{00062008-0000-0000-C000-000000000046}/85A00040"  # when it was flagged
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


def _blocked():
    """The spam blocks (spam.py) as a matcher, or None when there are none (or that module isn't there).
    matcher.hit(sender, subject, name): subject may be a callable, so it's only read from Outlook when needed."""
    try:
        from .. import spam
    except ModuleNotFoundError as e:
        if e.name != f"{__package__.rpartition('.')[0]}.spam":
            raise
        return None
    m = spam.matcher()
    return m if m else None


# ---- reads -------------------------------------------------------------------

def recent_mail(limit=50, unread_only=False) -> list[dict]:
    out = []
    blocked = _blocked()
    important = people.important_senders()
    for item in _iter_inbox(limit * 4 if unread_only or blocked else limit):
        if unread_only and not item.UnRead:
            continue
        if blocked and blocked.hit(_sender_smtp(item), lambda: item.Subject, item.SenderName):
            continue  # blocked as spam (Junk Email); the Outlook rule may not have moved it yet
        m = _summary(item)
        m["important"] = people.is_important(m["sender"], important)  # the star on Mail rows
        out.append(m)
        if len(out) >= limit:
            break
    return out


def sent_mail(limit=50) -> list[dict]:
    """The newest sent emails (Sent Items), with who they went to."""
    out = []
    for item in _iter_folder(OL_FOLDER_SENT, "[SentOn]", limit):
        m = _summary(item)
        m.update(to=item.To or "", folder="Sent", important=False)
        out.append(m)
    return out


UNREAD_MAX = 100   # unread Inbox mail the Mail tab lists on top, newest first
SEARCH_MAX = 60


def _summaries(entry_ids, folder="Inbox") -> list[dict]:
    """Mail-list rows for these EntryIDs (spam-blocked mail left out, important senders starred)."""
    ns, blocked, important, out = _ns(), _blocked(), people.important_senders(), []
    for entry_id in entry_ids:
        try:
            item = ns.GetItemFromID(entry_id)
            if item.Class != OL_MAIL:
                continue
            if folder == "Inbox" and blocked and blocked.hit(_sender_smtp(item), lambda: item.Subject, item.SenderName):
                continue
            m = _summary(item)
            if folder == "Sent":
                m["to"] = item.To or ""  # the list shows who it went to
        except pywintypes.com_error:
            continue  # moved or deleted meanwhile
        m["important"] = people.is_important(m["sender"], important)
        m["folder"] = folder
        out.append(m)
    return out


def _newest(rows, tag=None) -> list:
    """(EntryID, time, MessageClass) table rows -> EntryIDs of the emails, newest first (tag: (when, EntryID, tag))."""
    mail = [(when.timestamp() if when else 0, eid) for eid, when, cls in rows if (cls or "").startswith("IPM.Note")]
    mail.sort(reverse=True)
    return [(t, eid, tag) for t, eid in mail] if tag else [eid for _, eid in mail]


def _search_where(q: str, body: bool) -> str:
    v = q.replace("'", "''")
    parts = [f'"urn:schemas:httpmail:{f}" LIKE \'%{v}%\'' for f in ("subject", "fromname", "fromemail", "displayto")]
    if body:  # the message text, through Outlook's search index (word / phrase match)
        parts.append(f'"urn:schemas:httpmail:textdescription" ci_phrasematch \'{v}\'')
    return "@SQL=(" + " OR ".join(parts) + ")"


def _search_rows(folder_id, date_field, q) -> list:
    for body in (True, False):
        try:
            return list(_table_rows(folder_id, _search_where(q, body), ["EntryID", date_field, "MessageClass"], 5000))
        except pywintypes.com_error:
            continue  # a store without a search index refuses ci_phrasematch: subject and people only
    return []


def mail_view(limit=50, unread_only=False, query="", folder="inbox") -> dict:
    """The Mail tab. No query: every unread Inbox email (newest UNREAD_MAX) plus the newest `limit` (unread only:
    just the unread); folder "sent": the newest `limit` sent emails instead. Query (either folder): the newest
    SEARCH_MAX emails in the Inbox and Sent Items whose subject, sender, recipients or text match. unread_total counts
    the Inbox's unread emails."""
    unread_rows = list(_table_rows(OL_FOLDER_INBOX, "[UnRead] = True", ["EntryID", "ReceivedTime", "MessageClass"], 5000))
    unread_ids = _newest(unread_rows)
    total = len(unread_ids)
    if query:
        hits = _newest(_search_rows(OL_FOLDER_INBOX, "ReceivedTime", query), "Inbox") \
            + _newest(_search_rows(OL_FOLDER_SENT, "SentOn", query), "Sent")
        hits.sort(reverse=True)  # newest first across both folders; opened only until SEARCH_MAX are shown
        found = []
        for _, eid, folder in hits[:SEARCH_MAX * 4]:  # (spam-blocked mail still in the Inbox is skipped)
            found += _summaries([eid], folder)
            if len(found) >= SEARCH_MAX:
                break
        return {"mail": found, "unread_total": total, "query": query}
    if folder == "sent":
        return {"mail": sent_mail(limit), "unread_total": total, "query": "", "folder": "sent"}
    mail = _summaries(unread_ids[:UNREAD_MAX])
    if not unread_only:
        seen = {m["id"] for m in mail}
        mail += [m | {"folder": "Inbox"} for m in recent_mail(limit) if m["id"] not in seen]
    return {"mail": mail, "unread_total": total, "query": ""}


def get_message(entry_id) -> dict:
    item = _ns().GetItemFromID(entry_id)
    d = _summary(item)
    d.update(to=item.To or "", cc=item.CC or "", body=item.Body or "", important=people.is_important(d["sender"]))
    try:
        ns = _ns()
        if ns.CompareEntryIDs(item.Parent.EntryID, ns.GetDefaultFolder(OL_FOLDER_SENT).EntryID):
            d.update(folder="Sent", important=False)  # the reader offers no Spam / Mark important on Joe's own mail
    except pywintypes.com_error:
        pass
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


LUNCH_DEFAULTS = {"subjects": ["lunch", "blocked"], "between": "11:00-14:30"}


def _lunch_rule() -> tuple[set, int, int]:
    """outlook.meetings.lunch: (subjects, window start, window end in minutes after midnight)."""
    cfg = {**LUNCH_DEFAULTS, **((config.section("outlook", {}).get("meetings") or {}).get("lunch") or {})}
    subjects = {str(s).strip().lower() for s in cfg.get("subjects") or [] if str(s).strip()}
    try:
        a, b = (int(h) * 60 + int(m) for h, m in (part.strip().split(":") for part in str(cfg["between"]).split("-")))
    except (ValueError, KeyError):
        a, b = 11 * 60, 14 * 60 + 30
    return subjects, a, b


def _is_lunch(item, subject, start, end, rule) -> bool:
    """A lunch block: a plain appointment (not a meeting with attendees), not all day, whose whole subject is one of
    the lunch subjects, starting and ending on the same day inside the lunch window."""
    subjects, a, b = rule
    if item.AllDayEvent or subject.strip().lower() not in subjects or start.date() != end.date():
        return False
    try:
        if item.MeetingStatus != 0:  # olNonMeeting
            return False
    except pywintypes.com_error:
        return False
    return a <= start.hour * 60 + start.minute and end.hour * 60 + end.minute <= b


def _meetings(start, end, limit=50, all_day=False) -> list[dict]:
    """Calendar items overlapping [start, end). Lunch blocks (_is_lunch) come back as subject "Lunch" with
    lunch: True: shown, but not counted, reminded or prepped as meetings."""
    items = _ns().GetDefaultFolder(OL_FOLDER_CALENDAR).Items
    items.Sort("[Start]")
    items.IncludeRecurrences = True
    fmt = "%m/%d/%Y %I:%M %p"
    found = items.Restrict(f"[Start] <= '{end.strftime(fmt)}' AND [End] >= '{start.strftime(fmt)}'")
    out, item, rule = [], found.GetFirst(), _lunch_rule()
    while item is not None and len(out) < limit:
        if all_day or not item.AllDayEvent:
            body = item.Body or ""
            link = TEAMS_LINK.search(body)
            subject = item.Subject or "(no subject)"
            m_start, m_end = _local_time(item.Start), _local_time(item.End)
            lunch = _is_lunch(item, subject, m_start, m_end, rule)
            out.append({
                "id": item.EntryID,
                "subject": "Lunch" if lunch else subject,
                "lunch": lunch,
                "start": m_start.isoformat(timespec="minutes"),
                "end": m_end.isoformat(timespec="minutes"),
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

OL_DISCARD = 1
OL_TO, OL_CC, OL_BCC = 1, 2, 3


def _recipients(msg) -> list[dict]:
    out = []
    for r in msg.Recipients:
        email = ""
        try:
            entry = r.AddressEntry
            user = entry.GetExchangeUser() if entry else None
            email = (user.PrimarySmtpAddress if user else r.Address) or ""
        except pywintypes.com_error:
            email = r.Address or ""
        out.append({"name": r.Name or email, "email": email,
                    "type": {OL_TO: "To", OL_CC: "CC", OL_BCC: "BCC"}.get(r.Type, "To"),
                    "external": bool(email) and "@" in email and not people.is_internal(email)})
    return out


def recipients_signature(subject, recipients) -> str:
    """What the review screen promised: subject + who gets it. A send must match this exactly."""
    who = sorted(f"{r['type']}:{(r['email'] or r['name']).lower()}" for r in recipients)
    return (subject or "") + "|" + ";".join(who)


def reply_preview(entry_id, text, reply_all=False) -> dict:
    """Builds the reply exactly as Outlook would send it, reports subject/recipients, then discards it."""
    item = _ns().GetItemFromID(entry_id)
    msg = item.ReplyAll() if reply_all else item.Reply()
    try:
        recips = _recipients(msg)
        subject = msg.Subject or ""
    finally:
        msg.Close(OL_DISCARD)
    return {"subject": subject, "recipients": recips, "text": text,
            "signature": recipients_signature(subject, recips)}


def reply(entry_id, text, reply_all=False, send=True, expect_signature=None) -> dict:
    """Returns the conversation and recipients, so a no-reply reminder can watch the thread.
    Sending requires `expect_signature` from reply_preview(); it's re-checked right before Send()."""
    item = _ns().GetItemFromID(entry_id)
    msg = item.ReplyAll() if reply_all else item.Reply()
    if send:
        recips = _recipients(msg)
        if expect_signature is None or recipients_signature(msg.Subject or "", recips) != expect_signature:
            msg.Close(OL_DISCARD)
            raise ValueError("The recipients or subject changed since you reviewed this reply. Review it again.")
    if msg.BodyFormat == OL_FORMAT_HTML:
        snippet = "<div>" + html.escape(text).replace("\n", "<br>") + "</div><br>"
        msg.HTMLBody = re.sub(r"(<body[^>]*>)", lambda m: m.group(1) + snippet, msg.HTMLBody, count=1, flags=re.I)
    else:
        msg.Body = text + "\n\n" + msg.Body
    info = {"conversation": msg.ConversationID, "subject": msg.Subject or "", "to": msg.To or "",
            "recipients": [r["email"] or r["name"] for r in _recipients(msg)]}
    if send:
        msg.Send()
    else:
        _show(msg)
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


def _show(item):
    """Opens an item in its own Outlook window, in front of Command Center.

    Windows only lets the foreground process hand focus to another one, so a plain Display() opens the
    window behind the (usually full-screen) Command Center window. Command Center is the foreground
    process when its Open button is clicked, so it lets Outlook come forward, then raises the window."""
    main = win32gui.FindWindow("rctrl_renwnd32", None)  # Outlook's main window
    pid = win32process.GetWindowThreadProcessId(main)[1] if main else 0
    if pid:
        ctypes.windll.user32.AllowSetForegroundWindow(pid)
    item.Display(False)
    try:
        item.GetInspector.Activate()
    except pywintypes.com_error:
        pass
    fg = win32gui.GetForegroundWindow()
    if not pid or (fg and win32process.GetWindowThreadProcessId(fg)[1] == pid):
        return
    # Still behind: raise the newest visible Outlook item window ourselves (allowed while we're in front).
    found = []
    win32gui.EnumWindows(lambda h, _: found.append(h) if (
        win32gui.IsWindowVisible(h) and h != main and win32gui.GetClassName(h) == "rctrl_renwnd32"
        and win32process.GetWindowThreadProcessId(h)[1] == pid) else None, None)
    if found:  # EnumWindows lists top to bottom; a new window is first among Outlook's
        try:
            win32gui.SetForegroundWindow(found[0])
        except pywintypes.error:
            pass


def open_item(entry_id):
    _show(_ns().GetItemFromID(entry_id))


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
        "priority": IMPORTANCE_PRIORITY.get(r[3], "normal"),
    } for r in _table_rows(OL_FOLDER_TASKS, "[Complete] = False", ["EntryID", "Subject", "DueDate", "Importance"])]

    flagged = []
    # The To-Do search folder ignores GetTable filters, so filter the rows here.
    base = ["EntryID", "Subject", "MessageClass", PR_FLAG_STATUS, PR_SENDER_NAME, PR_DELIVERY_TIME, PID_TASK_DUE,
            PID_TODO_ORDINAL]
    try:
        rows = _table_rows(OL_FOLDER_TODO, "", base + [PR_SENDER_SMTP, PR_SENT_REPR_SMTP, PR_SENDER_EMAIL])
    except pywintypes.com_error:  # a store that can't add these columns: names only
        rows = [tuple(r) + (None, None, None) for r in _table_rows(OL_FOLDER_TODO, "", base)]
    for entry_id, subject, msg_class, flag, sender, received, due, flagged_at, smtp, repr_smtp, email in rows:
        if flag != 2 or not (msg_class or "").startswith("IPM.Note"):
            continue
        received = _utc_to_local(received)
        flagged_at = _utc_to_local(flagged_at) if flagged_at and flagged_at.year < NO_DATE_YEAR else None
        address = next((a for a in (smtp, repr_smtp, email) if isinstance(a, str) and "@" in a), "")
        flagged.append({
            "id": entry_id, "subject": subject or "(no subject)", "sender_name": sender or "", "sender": address,
            "due": due.date().isoformat() if due and due.year < NO_DATE_YEAR else None,
            "received": received.isoformat(timespec="minutes") if received else None,
            "flagged_at": flagged_at.isoformat(timespec="minutes") if flagged_at else None,
        })
    return {"tasks": tasks, "flagged": flagged}


def create_task(subject, due=None, notes="", priority="normal") -> str:
    task = _ns().Application.CreateItem(OL_TASK_ITEM)
    task.Subject = subject
    if due:
        task.DueDate = datetime.fromisoformat(due)
    if notes:
        task.Body = notes
    task.Importance = PRIORITY_IMPORTANCE.get(priority, 1)
    task.Save()
    return task.EntryID


def set_task_priority(entry_id, priority):
    """Sets an Outlook task's own Importance (High / Normal / Low), so Outlook and To Do show it too. Tasks only:
    a flagged email's importance is the sender's, not Joe's priority."""
    if priority not in PRIORITY_IMPORTANCE:
        raise ValueError("Priority is high, normal or low")
    item = _ns().GetItemFromID(entry_id)
    if item.Class != OL_TASK:
        raise ValueError("Only Outlook tasks have a priority here")
    if item.Importance != PRIORITY_IMPORTANCE[priority]:
        item.Importance = PRIORITY_IMPORTANCE[priority]
        item.Save()


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
    return people.sender_matches(sender, patterns)  # one definition, shared with the important-senders list


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


# Where the quoted history of a reply starts: only the new text above it counts as calling Joe out.
QUOTE_START = re.compile(r"^\s*(?:\*?From:\*?\s|-{3,}\s*Original Message|_{10,}|On .{4,200} wrote:)", re.I | re.M)
_mention_cache: dict = {}  # EntryID -> bool (a received message's text doesn't change)


def _mentions_me(item, names, me_smtp) -> bool:
    """True if the subject or the new part of the body names Joe ("Joe", "Joe Cherbak", his address)."""
    key = item.EntryID
    if key not in _mention_cache:
        body = item.Body or ""
        m = QUOTE_START.search(body)
        text = f"{item.Subject or ''}\n{body[:m.start()] if m else body}"[:20000]
        pattern = r"(?<![\w.])(?:" + "|".join(re.escape(n) for n in names if n) + r")(?![\w])"
        found = bool(re.search(pattern, text, re.I)) or bool(me_smtp and me_smtp in text.lower())
        if len(_mention_cache) > 2000:
            _mention_cache.clear()
        _mention_cache[key] = found
    return _mention_cache[key]


def followups(awaiting_days=7, reply_days=5, min_wait_hours=4, important=()) -> dict:
    """Sent mail nobody answered, mail you haven't replied to (internal senders by default, urgent people
    split out), unread Teams missed-activity emails, and unread mail from important senders (`important`:
    people.important_senders(); listed under the old key "unread_vip")."""
    fu = config.section("followups", {})
    internal_only = fu.get("needs_reply_scope", "internal") == "internal"
    urgent = people.urgent_people()
    me_name, me_smtp = _me()
    # "Needs your reply" only when the message calls Joe out by name or address (urgent people always count).
    mention_names = [str(n) for n in fu.get("mention_names", ["Joe", "Joe Cherbak"]) or []] + [me_name]
    need_mention = fu.get("needs_reply_requires_mention", True) is not False
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
    blocked = _blocked()
    for item in _iter_inbox(3000):
        received = _local_time(item.ReceivedTime)
        if received < horizon:
            break
        conv = item.ConversationID
        inbox_latest.setdefault(conv, received)
        sender = _sender_smtp(item).lower()
        if sender == me_smtp or (blocked and blocked.hit(sender, lambda: item.Subject, item.SenderName)):
            continue
        # Teams missed-activity emails stay pending until you mark them done (read) or dismiss them.
        if is_teams_notification(sender):
            if item.UnRead and received >= recent and len(teams_reply) < 25:
                who = teams_sender(item.Subject)
                teams_reply.append({**_summary(item), "from_name": who, "urgent": people.matches("", who, urgent)})
            continue
        if item.UnRead and important and people.is_important(sender, important) and len(unread_vip) < 25:
            unread_vip.append({**_summary(item), "important": True})
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
        if need_mention and not is_urgent and not _mentions_me(item, mention_names, me_smtp):
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

    hits, spam = [], []
    urgent = people.urgent_people()
    important = people.important_senders()
    blocked = _blocked()
    for item in _iter_inbox(200):
        if _local_time(item.ReceivedTime) < since:
            break
        mail = _summary(item)
        if db.seen("mail:" + mail["id"]):
            continue
        if blocked and blocked.hit(mail["sender"], item.Subject or "", mail["sender_name"]):
            spam.append(item)  # no alert; moved below (not while walking the sorted Items)
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
        # Important senders (the config "VIP sender" rule's senders plus the list edited in the app) come next:
        # high priority even if that rule was removed from config.yaml.
        if important and people.is_important(mail["sender"], important):
            hits.append((mail, {"name": "Important sender", "priority": "high"}))
            continue
        body_lower = (item.Body or "")[:20000].lower()
        for rule in rules:
            if _matches(rule, mail, body_lower):
                hits.append((mail, rule))
                break  # first matching rule wins
    if spam:  # blocked mail the Outlook rule didn't catch (e.g. the rule couldn't be saved): to Junk Email
        junk, moved = _ns().GetDefaultFolder(OL_FOLDER_JUNK), 0
        for item in spam:
            try:
                item.Move(junk)
                moved += 1
            except pywintypes.com_error:
                pass
        log.info("Moved %d new blocked message(s) to Junk Email", moved)
    return hits


def scan_meetings() -> list[tuple[dict, int]]:
    reminders = sorted(config.section("outlook", {}).get("meetings", {}).get("remind_minutes", [10, 1]))
    if not reminders:
        return []
    due = []
    now = datetime.now()
    for m in upcoming_meetings(hours=max(reminders) / 60 + 1):
        if m["lunch"]:
            continue
        minutes = (datetime.fromisoformat(m["start"]) - now).total_seconds() / 60
        if minutes < -2:  # a "now" (0) reminder still fires if a scan lands up to 2 minutes after the start
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
                                           "Mail, calendar and task alerts need Outlook running.", "normal",
                                           toast="Outlook restarted")
                    except Exception as e:
                        alerts.raise_alert("Outlook", "Outlook is closed and couldn't be restarted", str(e), "high",
                                           toast="Outlook closed, restart failed")
                else:
                    alerts.raise_alert("Outlook", "Outlook is closed", "Mail and calendar alerts are paused.", "normal",
                                       toast="Outlook closed")
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
                    toast="important new mail" if rule.get("priority") == "high" else "new mail",
                )
            for m, minutes in await call(scan_meetings):
                when = "now" if minutes == 0 else f"in {minutes} min"
                action = {"type": "meeting", "id": m["id"], "join_url": m["join_url"], "prep": True}
                alerts.raise_alert("Calendar", f"{m['subject']} starts {when}", m["location"], "high", action,
                                   toast=f"meeting {when}")
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
    alerts.raise_alert("Focus", f"Focus mode on until {until}", block["subject"], "low", toast="focus mode on")


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
