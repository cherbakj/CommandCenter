"""Privacy & Data: Privacy mode, the data inventory, and the vault's automatic locks.

- Privacy mode (GET/POST /api/privacy/mode): hides every personal item in every view (the planner and feeds
  return none while it's on) and locks the personal vault. Stored server-side, so a reload stays private.
  Unlocking the vault turns it off again.
- Inventory (GET /api/privacy/inventory): every data source, labeled Work or Personal, with what it reads,
  what is kept on this PC, how that is protected, how long it's kept, and whether it's connected. Built from
  cheap local checks only (no network calls). It never contains secrets, links, titles or personal text.
- Automatic vault locks (background job, polls every 1.5 s, no edits needed in security.py / desktop.py):
    * Command Center locked: security.end_session() replaces security.SESSION_SECRET, and the desktop shell's
      `unlocked` flag goes False. Either one locks the vault.
    * Windows locked (Win+L) or the session disconnected / switched away (the input desktop isn't ours), when
      privacy.lock_on_windows_lock is on (default).
    * System sleep / hibernate, always: a power notification (PowerRegisterSuspendResumeNotification, a callback,
      no window needed) locks the vault on PBT_APMSUSPEND, before memory is written to the hibernation file.
      Backstop if that notification is missed (e.g. some Modern Standby transitions): every poll compares
      GetTickCount64 (counts sleep) with QueryUnbiasedInterruptTime (doesn't); a gap means the PC slept, and the
      vault is locked on resume.
    * privacy.auto_lock_minutes after an unlock (0 = off, the default; Joe doesn't want idle lock-outs).
    * privacy.stay_unlocked_today (default on; Joe unlocks once in the morning): the three locks above (Command
      Center lock, Windows lock, sleep / hibernate) are skipped, and the vault locks at midnight of the day it was
      unlocked instead. Getting back into Command Center after a lock still takes Windows Hello, and into Windows
      the Windows sign-in. Trade-off: the key stays in memory meanwhile, and in the hibernation file if Windows
      hibernates. Privacy mode, the vault's Lock button, auto_lock_minutes and a restart still lock.
  A restart always starts locked: the key exists only in memory. Bad config values fall back to the defaults
  (checked at load, logged once) and never stop the watch loop.
"""
import asyncio
import ctypes
import ctypes.wintypes as wt
import hashlib
import logging
from datetime import date

from fastapi import APIRouter, Body

from . import config, db, vault

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/privacy")


DEFAULTS = {"lock_on_windows_lock": True, "auto_lock_minutes": 0, "stay_unlocked_today": True}
_warned: set = set()


def _warn_once(key: str, text: str):
    if key not in _warned:
        _warned.add(key)
        log.warning(text)


def settings() -> dict:
    """privacy: from config.yaml, parsed defensively: a bad value means its default (logged once), never an error,
    so one typo can't switch off the automatic locks."""
    try:
        s = config.section("privacy", {}) or {}
    except Exception:
        s = {}
    if not isinstance(s, dict):
        _warn_once("section", "privacy: in config.yaml isn't a section; using the defaults")
        s = {}
    lock_win = s.get("lock_on_windows_lock", True)
    if not isinstance(lock_win, bool):
        _warn_once("lock_on_windows_lock", "privacy.lock_on_windows_lock should be true or false; using true")
        lock_win = True
    stay = s.get("stay_unlocked_today", True)
    if not isinstance(stay, bool):
        _warn_once("stay_unlocked_today", "privacy.stay_unlocked_today should be true or false; using true")
        stay = True
    raw = s.get("auto_lock_minutes", 0)
    try:
        if isinstance(raw, bool):
            raise ValueError
        minutes = float(raw or 0)
        if not 0 <= minutes <= 7 * 24 * 60 or minutes != minutes:
            raise ValueError
    except (TypeError, ValueError):
        _warn_once("auto_lock_minutes", "privacy.auto_lock_minutes should be a number of minutes (0 = off); using 0")
        minutes = 0
    return {"lock_on_windows_lock": lock_win, "auto_lock_minutes": int(minutes) if minutes == int(minutes) else minutes,
            "stay_unlocked_today": stay}


def stay_today() -> bool:
    """privacy.stay_unlocked_today: the vault stays unlocked until midnight through Command Center / Windows locks
    and sleep."""
    return settings()["stay_unlocked_today"]


# ---- Privacy mode ----------------------------------------------------------------------------------

@router.get("/mode")
async def get_mode():
    return {"on": vault.privacy_on(), "vault": await asyncio.to_thread(vault.status)}


@router.post("/mode")
async def set_mode(on: bool = Body(..., embed=True)):
    await asyncio.to_thread(vault.set_privacy, on)
    log.info("Privacy mode %s", "on" if on else "off")
    return {"on": vault.privacy_on(), "vault": await asyncio.to_thread(vault.status)}


# ---- inventory -------------------------------------------------------------------------------------

def _try(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _table_count(table: str) -> int | None:
    def q():
        with db._lock:
            c = db._db()
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone():
                return 0
            return c.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    return _try(q)


def _work_encryption() -> str:
    return "Windows DPAPI (this Windows account)" if hasattr(db, "seal") else "None (private data folder only)"


def _retention() -> dict:
    return _try(lambda: db.retention_settings(), {}) or {}


def _days(n) -> str:
    return f"{int(n)} days" if n else "until removed"


def inventory() -> list[dict]:
    from .connectors import asana
    from .connectors import sql as sqlc
    from .connectors import teams, weather, web
    r = _retention()
    dpapi = _work_encryption()
    items = []

    def add(id, name, category, accesses, stored, encryption, retention, connected=None, state="", actions=(), **extra):
        items.append({"id": id, "name": name, "category": category, "accesses": accesses, "stored": stored,
                      "encryption": encryption, "retention": retention, "connected": connected, "state": state,
                      "actions": list(actions), **extra})

    st = _try(lambda: db.kv_get("outlook:status")) or {}
    add("outlook", "Outlook", "Work",
        "Mail, calendar and To Do in the Outlook desktop app on this PC (through Outlook itself; Command Center has no cloud access).",
        "Follow-up state (snoozes, reply checks, dismissed and muted items, urgent people, important senders), the "
        "sent-mail log, alerts.",
        dpapi, f"Sent-mail log {_days(r.get('mail_sent_log_days'))}; dismissed follow-ups {_days(r.get('dismissed_followups_days'))}; "
        "urgent people and important senders until removed",
        connected=bool(st.get("ok")) if st else None, state="Connected" if st.get("ok") else (st.get("error") or "Not checked yet")[:80])

    teams_on = _try(teams.is_set_up, False)
    add("teams", "Teams", "Work",
        "Teams on the web, read-only by a background Edge profile you signed in to: the unread total, which chats are "
        "unread and your @mentions, with sender, chat name, a short preview and time (from the chat list and Teams' "
        "own cache in that page). Nothing is clicked, marked read or sent. Chats open in the Teams app.",
        "That browser profile (its cookies and Teams' own cache) in the private data folder; the unread counts; "
        "the chat / activity ids already alerted on. Names and previews stay in memory only (gone on restart); "
        "Teams alerts carry them like other alerts.",
        "Profile: Edge's own encryption (DPAPI), private folder; alerted ids: " + dpapi + "; counts: unencrypted metadata",
        "Profile until you sign out of it; alerted ids while unread; alerts " + _days(r.get("alerts_days")),
        connected=teams_on, state="Signed in" if teams_on else "Not set up")

    # The sites shown inside the window (siteview.py): each has its own WebView2 profile folder, created the first time
    # its tab shows it. Command Center never reads what's in them; only the site does.
    for vid, vname, folder, what in (
            ("teams_view", "Teams on the web (Teams tab)", "webview-teams",
             "Teams on the web, signed in by you (Microsoft 365 / Okta), shown in the Teams tab to read and answer chats. "
             "Separate from the background Teams reader's profile."),
            ("pulse_view", "Pulse site (Pulse tab)", "webview-pulse",
             "pulse.sonance.com and dev.pulse.sonance.com, signed in by you (Okta), shown in the Pulse tab.")):
        there = _try(lambda f=folder: (config.DATA_DIR / f).is_dir(), False)
        add(vid, vname, "Work", what + " Command Center only places the window; it reads nothing from the page except its "
            "title and address for the tab's bar.",
            f"That WebView2 profile in data/{folder}: the site's cookies and sign-in session, its cache and local storage. "
            "Form autofill and saved passwords are off (cleared at each launch).",
            "Edge / WebView2's own (cookies with DPAPI), private data folder",
            "Until you sign out of the site there (or delete the folder while Command Center is closed)",
            connected=there or None, state="Used" if there else "Not opened yet")

    try:
        from . import toolkit
        tk = _try(toolkit.password_saved, False)
    except ImportError:
        tk = None
    if tk is not None:
        add("toolkit", "Toolkit (SQL Server)", "Work",
            "Shipping, Logi and Customers data in SQL Server, with your SQL login.",
            "SQL password in Windows Credential Manager; every change attempt in the toolkit change log.",
            f"Credential Manager (DPAPI); change log: {dpapi}", f"Change log {_days(r.get('toolkit_changes_days'))}",
            connected=tk, state="Password saved" if tk else "No password saved", actions=["forget"] if tk else [])

    conns = _try(sqlc.connections, []) or []
    with_pw = sum(1 for c in conns if _try(lambda c=c: sqlc.has_password(c.get("name")), False))
    add("sql", "SQL tab", "Work", "SQL Server connections from config.yaml, read with saved queries.",
        "Passwords in Windows Credential Manager (SQL logins only); results stay in memory.",
        "Credential Manager (DPAPI)", "Results: not kept", connected=bool(with_pw) if conns else None,
        state=f"{len(conns)} connection(s), {with_pw} ready" if conns else "No connections configured")

    try:
        from . import pulse
        pc = _try(pulse.is_configured, False)
    except ImportError:
        pc = None
    if pc is not None:
        add("pulse", "Pulse tickets", "Work", "Pulse data reviews in Postgres through a read-only role.",
            "Database password in Windows Credential Manager; the last ticket state (ids, numbers, status and "
            "priority, for change alerts).",
            f"Credential Manager (DPAPI); ticket state: {dpapi}", "Until disconnected", connected=pc,
            state="Connected" if pc else "Not connected", actions=["disconnect"] if pc else [])

    ac = _try(asana.connected, False)
    add("asana", "Asana", "Work", "Your Asana tasks, read with your personal access token.",
        "The token in Windows Credential Manager. Tasks are read live, not stored; the High/Low priority you set on "
        "a task is kept by its task id only (kv tasks:priority, encrypted). Outlook task priority is Outlook's own.",
        "Credential Manager (DPAPI); priorities DPAPI", "Token until disconnected; a priority until its task is closed",
        connected=ac,
        state="Connected" if ac else "Not connected", actions=["disconnect"] if ac else [])

    running = None
    try:
        from . import terminal
        running = len(_try(terminal._running, []) or [])
    except ImportError:
        pass
    add("projects", "Projects / Claude", "Work",
        "Project folders (C:\\Sonance\\internal, ~\\source and projects.extra); runs the Claude Code CLI in them.",
        "Nothing extra from Command Center. Claude Code keeps its own session history in ~\\.claude.",
        "Claude Code's own files (not encrypted by Command Center)", "Claude Code's settings",
        connected=bool(running) if running is not None else None,
        state=f"{running} session(s) running" if running else "No sessions running")

    try:
        from . import vscode
        vs = _try(lambda: vscode.status().get("status"), None)
    except ImportError:
        vs = None
    add("vscode", "VS Code", "Work", "Your project folders, through a VS Code server on 127.0.0.1 only.",
        "VS Code settings, extensions and state in data\\vscode, including copies of desktop VS Code's extensions and "
        "settings (settings that look like passwords, tokens or keys are not copied).", "None (private data folder, owner-only ACL)",
        "Until deleted", connected=vs == "running", state=str(vs or "stopped").capitalize())

    mons = _try(web.monitors, []) or []
    if mons:
        add("websites", "Websites", "Work", "The pages listed under websites: in config.yaml, read with a background browser.",
            "The last value read from each page; the browser profiles you signed in to.", dpapi,
            "Last value only", connected=True, state=f"{len(mons)} page(s) watched")

    orders_on = (config.section("orders", {}) or {}).get("enabled", True) is not False
    add("orders", "Order lookup (clipboard)", "Work",
        "Text you copy: only a 7-10 digit order number is picked out and looked up in SQL.",
        "Nothing (the lookup result is shown, not stored).", "Not stored", "Not kept",
        connected=orders_on, state="On" if orders_on else "Off")

    zips = [p.get("zip") for p in (_try(weather.locations, []) or []) if p.get("zip")]
    add("weather", "Weather", "Work", "Only zip codes are sent (to zippopotam.us and Open-Meteo). No account.",
        "The zip codes' place names and the latest forecast.", "None (not sensitive: zip codes only)", "Forecast: 10 minutes",
        connected=bool(zips), state=f"{len(zips)} location(s)")

    try:
        from . import spam
        n_blocks = len(_try(spam.blocks, []) or [])
        rule_ok = (_try(spam.rule_status, {}) or {}).get("ok")
    except ImportError:
        n_blocks = None
    if n_blocks is not None:
        add("spam", "Spam blocks", "Work",
            "Emails you mark as spam: the sender address, domain or subject phrase you pick. Two Outlook rules named "
            "“Command Center spam blocks” move matching mail to Junk Email.",
            "The blocked addresses, domains and phrases (kv spam:blocks) and whether the Outlook rules saved. No "
            "subjects or bodies of the emails themselves.", dpapi, "Until you unblock it", connected=None,
            state=f"{n_blocks} block(s)" + ("" if not n_blocks or rule_ok else " · Outlook rule not saved"))

    n_alerts = _table_count("alerts")
    add("alerts", "Alerts and log", "Work",
        "Alerts raised by the other sources. The log file records ids, sources and errors, never titles or bodies.",
        f"{n_alerts if n_alerts is not None else '?'} alert(s) in SQLite; data\\commandcenter.log (5 MB, 3 old files).",
        f"Alerts: {dpapi}; log: private data folder", f"Alerts {_days(r.get('alerts_days'))}; log rotates at 5 MB",
        connected=None, state=f"{n_alerts} stored" if n_alerts is not None else "")

    notes = _table_count("meeting_notes")
    add("daily", "Meeting notes", "Work", "What you type into meeting notes and their action items.",
        f"{notes or 0} meeting note(s) in SQLite.", "Windows DPAPI (this Windows account)",
        "Until deleted", connected=None, state=f"{notes or 0} note(s) · visible in Privacy mode (work data)")

    try:
        from . import offhours
        oh = _try(offhours.settings, None)
    except ImportError:
        oh = None
    if oh is not None:
        oh_days = _table_count("offhours_log")
        add("offhours", "Off-hours work log", "Work",
            "Only whether this PC had keyboard or mouse input while Windows was unlocked (checked once a minute), "
            "never which app, window, file or key. Logged only while Command Center is running.",
            "Start and end times of activity outside work hours, per day (offhours_log). Nothing from work hours.",
            dpapi, _days(r.get("offhours_days")), connected=oh["enabled"],
            state=(f"On · {oh_days or 0} day(s) logged" if oh["enabled"] else "Off (offhours.enabled)") + " · visible in Privacy mode (work data)")

    seen_n, vault_n = _table_count("seen"), _table_count("vault_records")
    add("metadata", "Unencrypted metadata", "Work",
        "Kept in clear so the app can sort, prune and look things up without decrypting everything.",
        "Dates, ids and counts only, no titles, names or text: alert time, source, priority and read flag; "
        "meeting-note start / last-edit times; the days that have off-hours activity; 'already alerted' keys (Outlook item ids, Pulse "
        "aging days) with their times; personal vault record ids and how many there are; operational settings "
        "(status and error lines, Teams unread counts, watermarks, window size, weather places, timers).",
        "None (private data folder, owner-only ACL)",
        f"Alerts {_days(r.get('alerts_days'))}; 'already alerted' keys {_days(r.get('seen_days'))}; the rest with its data",
        connected=None, state=f"{seen_n or 0} key(s), {vault_n or 0} vault record id(s)")

    vs_ = vault.status()
    counts = _try(vault.counts, None) if vs_["available"] else None
    legacy = vs_["legacy"]
    vault_state = ("Not set up" if not vs_["set_up"] else "Privacy mode on" if vs_["privacy"]
                   else "Unlocked" if vs_["unlocked"] else "Locked")
    pe_waiting, oc_waiting = legacy.get("planner entries", 0), legacy.get("overlay calendars", 0)
    add("personal-entries", "Personal calendar entries", "Personal", "Only what you type into personal entries.",
        "Each entry (date, time, title, notes) as one encrypted record in SQLite.",
        "Personal vault: AES-256-GCM, key from your passphrase (not stored anywhere)", "Until you delete them",
        connected=vs_["available"],
        state=(f"{counts.get('planner-entry', 0)} entr(ies) · {vault_state}" if counts is not None else vault_state)
        + (f" · {pe_waiting} older entr(ies) waiting to move in" if pe_waiting else ""))
    cals = []
    if vs_["available"]:
        from . import feeds
        cals = [{"id": f["id"], "name": f["name"], "kind": f["kind"], "detail": f["detail"], "enabled": f["enabled"], "color": f["color"]}
                for f in _try(feeds.feeds, []) or []]
    add("overlay-calendars", "Overlay calendars (Google / iCloud)", "Personal",
        "Your private iCal links to Google Calendar / iCloud calendars (read-only).",
        "Each calendar's name, color, detail level and private link as one encrypted record. Events: memory only, 15 minutes.",
        "Personal vault: AES-256-GCM, key from your passphrase (not stored anywhere)", "Links until removed; events 15 min in memory",
        connected=vs_["available"] and bool(cals),
        state=(f"{len(cals)} calendar(s) · {vault_state}" if vs_["available"] else vault_state)
        + (f" · {oc_waiting} older calendar(s) waiting to move in" if oc_waiting else ""),
        calendars=cals)
    try:
        from . import reminders  # personal reminders (vault) + their times-only schedule copy (DPAPI)
        for item in _try(lambda: reminders.inventory(r, dpapi, vs_, counts), []) or []:
            add(**item)
    except ImportError:
        pass
    return items


@router.get("/inventory")
async def get_inventory():
    items = await asyncio.to_thread(inventory)
    return {"items": items, "vault": await asyncio.to_thread(vault.status), "settings": settings()}


# ---- automatic locks -------------------------------------------------------------------------------

def _session_fingerprint() -> str | None:
    """Changes whenever Command Center is locked (security.end_session issues a new secret). Only a hash is kept."""
    try:
        from . import security
        return hashlib.sha256(security.SESSION_SECRET.encode()).hexdigest()
    except Exception:
        return None


def _shell_locked() -> bool:
    try:
        from . import security
        shell = security.shell
        return shell is not None and getattr(shell, "unlocked", True) is False
    except Exception:
        return False


_user32 = {}


def windows_locked() -> bool:
    """True while this Windows session is locked (Win+L): the input desktop is then Winlogon's secure desktop,
    which a normal process can't open. (A UAC prompt counts too, which only locks the vault early.)"""
    try:
        if not _user32:
            u = ctypes.WinDLL("user32", use_last_error=True)
            u.OpenInputDesktop.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
            u.OpenInputDesktop.restype = wt.HANDLE
            u.GetUserObjectInformationW.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)]
            u.GetUserObjectInformationW.restype = wt.BOOL
            u.CloseDesktop.argtypes = [wt.HANDLE]
            _user32["u"] = u
        u = _user32["u"]
        h = u.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
        if not h:
            return True
        try:
            name, needed = ctypes.create_unicode_buffer(64), wt.DWORD()
            if not u.GetUserObjectInformationW(h, 2, name, ctypes.sizeof(name), ctypes.byref(needed)):  # UOI_NAME
                return False
            return name.value.lower() != "default"
        finally:
            u.CloseDesktop(h)
    except Exception:
        return False


# ---- sleep / hibernate ---------------------------------------------------------------------------
PBT_APMSUSPEND, PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC = 0x4, 0x7, 0x12
DEVICE_NOTIFY_CALLBACK = 2
SLEEP_GAP_SECONDS = 5.0

_PowerCallback = ctypes.WINFUNCTYPE(wt.ULONG, ctypes.c_void_p, wt.ULONG, ctypes.c_void_p)


class _NotifyParams(ctypes.Structure):
    _fields_ = [("Callback", _PowerCallback), ("Context", ctypes.c_void_p)]


def _on_power(context, kind, setting):
    """Runs on a system thread. Windows waits for it before suspending, so the key is dropped before memory is
    written to the hibernation file. Resume notifications lock too, in case the suspend one was missed."""
    try:
        if stay_today():
            pass  # kept until midnight (the resume check below still catches a new day)
        elif kind == PBT_APMSUSPEND:
            vault.lock("system sleep / hibernate")
        elif kind in (PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
            vault.lock("resumed from sleep")
    except Exception:
        pass
    return 0


_power = {"cb": _PowerCallback(_on_power), "handle": None}


def register_power_notifications() -> bool:
    """Subscribes _on_power to suspend / resume (Windows 8+). True if registered."""
    if _power["handle"]:
        return True
    try:
        powrprof = ctypes.WinDLL("powrprof", use_last_error=True)
        fn = powrprof.PowerRegisterSuspendResumeNotification
        fn.argtypes = [wt.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        fn.restype = wt.DWORD
        params = _NotifyParams(_power["cb"], None)
        handle = ctypes.c_void_p()
        rc = fn(DEVICE_NOTIFY_CALLBACK, ctypes.byref(params), ctypes.byref(handle))
        if rc != 0:
            log.warning("Sleep notifications unavailable (error %s); relying on the resume check", rc)
            return False
        _power.update(handle=handle, params=params)  # params must stay alive while registered
        return True
    except Exception as e:
        log.warning("Sleep notifications unavailable (%s); relying on the resume check", type(e).__name__)
        return False


def unregister_power_notifications():
    handle = _power.get("handle")
    if not handle:
        return
    try:
        fn = ctypes.WinDLL("powrprof").PowerUnregisterSuspendResumeNotification
        fn.argtypes = [ctypes.c_void_p]
        fn(handle)
    except Exception:
        pass
    _power["handle"] = None


def _clocks() -> tuple[float, float] | None:
    """(seconds since boot including sleep, seconds since boot excluding sleep), or None."""
    try:
        k = ctypes.windll.kernel32
        k.GetTickCount64.restype = ctypes.c_ulonglong
        unbiased = ctypes.c_ulonglong()
        if not k.QueryUnbiasedInterruptTime(ctypes.byref(unbiased)):
            return None
        return k.GetTickCount64() / 1000.0, unbiased.value / 1e7
    except Exception:
        return None


class SleepDetector:
    """slept() is True when the PC slept since the last call (the two clocks drifted apart by > gap seconds)."""

    def __init__(self, clocks=_clocks, gap=SLEEP_GAP_SECONDS):
        self.clocks, self.gap, self.last = clocks, gap, clocks()

    def slept(self) -> bool:
        now = self.clocks()
        last, self.last = self.last, now
        if not now or not last:
            return False
        return (now[0] - last[0]) - (now[1] - last[1]) > self.gap


def check_once(state: dict):
    """One pass of the automatic locks. Each check is independent: one failing doesn't skip the others."""
    def attempt(name, fn):
        try:
            fn()
        except Exception as e:
            log.warning("Privacy watch (%s): %s", name, type(e).__name__)

    def session():
        fp = _session_fingerprint()
        changed = fp != state.get("fingerprint")
        state["fingerprint"] = fp
        if (changed or _shell_locked()) and not stay_today():
            vault.lock("Command Center locked")

    def sleep():
        if state["sleep"].slept() and not stay_today():
            vault.lock("resumed from sleep")

    def windows():
        if vault.unlocked() and settings()["lock_on_windows_lock"] and not stay_today() and windows_locked():
            vault.lock("Windows locked")

    def new_day():
        day = vault.unlocked_day()
        if day and day != date.today().isoformat():
            vault.lock("new day")

    def idle():
        minutes = settings()["auto_lock_minutes"]
        if minutes and vault.unlocked() and vault.unlocked_for() > minutes * 60:
            vault.lock(f"auto-lock after {minutes} min")

    for name, fn in (("session", session), ("sleep", sleep), ("windows", windows), ("new day", new_day), ("auto-lock", idle)):
        attempt(name, fn)


async def watch_loop():
    settings()  # validate the config once at start (bad values are logged and fall back to the defaults)
    register_power_notifications()
    state = {"fingerprint": _session_fingerprint(), "sleep": SleepDetector()}
    try:
        while True:
            await asyncio.sleep(1.5)
            try:
                check_once(state)
            except Exception as e:  # never let the loop die
                log.warning("Privacy watch: %s", type(e).__name__)
    finally:
        unregister_power_notifications()


background = [watch_loop]
