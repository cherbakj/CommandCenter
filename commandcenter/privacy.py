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
    * Windows locked (Win+L), when privacy.lock_on_windows_lock is on (default).
    * privacy.auto_lock_minutes after an unlock (0 = off, the default).
  A restart always starts locked: the key exists only in memory.
"""
import asyncio
import ctypes
import ctypes.wintypes as wt
import hashlib
import logging

from fastapi import APIRouter, Body

from . import config, db, vault

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/privacy")


def settings() -> dict:
    s = config.section("privacy", {}) or {}
    return {"lock_on_windows_lock": s.get("lock_on_windows_lock", True) is not False,
            "auto_lock_minutes": max(0, int(s.get("auto_lock_minutes", 0) or 0))}


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
        "Follow-up state (snoozes, reply checks, dismissed and muted items, urgent people), the sent-mail log, alerts.",
        dpapi, f"Sent-mail log {_days(r.get('mail_sent_log_days'))}; dismissed follow-ups {_days(r.get('dismissed_followups_days'))}",
        connected=bool(st.get("ok")) if st else None, state="Connected" if st.get("ok") else (st.get("error") or "Not checked yet")[:80])

    teams_on = _try(teams.is_set_up, False)
    add("teams", "Teams", "Work",
        "The missed-activity count, read by a background Edge profile you signed in to. Chats open in the Teams app.",
        "That browser profile (its cookies) in the private data folder; the unread count.",
        "Edge's own cookie encryption (DPAPI); private folder", "Until you sign out of that profile",
        connected=teams_on, state="Signed in" if teams_on else "Not set up")

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
            "Database password in Windows Credential Manager; the last ticket state (for change alerts).",
            f"Credential Manager (DPAPI); ticket state: {dpapi}", "Until disconnected", connected=pc,
            state="Connected" if pc else "Not connected", actions=["disconnect"] if pc else [])

    ac = _try(asana.connected, False)
    add("asana", "Asana", "Work", "Your Asana tasks, read with your personal access token.",
        "The token in Windows Credential Manager. Tasks are read live, not stored.",
        "Credential Manager (DPAPI)", "Until disconnected", connected=ac,
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
        "VS Code settings, extensions and state in data\\vscode.", "None (private data folder, owner-only ACL)",
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

    n_alerts = _table_count("alerts")
    add("alerts", "Alerts and log", "Work",
        "Alerts raised by the other sources. The log file records ids, sources and errors, never titles or bodies.",
        f"{n_alerts if n_alerts is not None else '?'} alert(s) in SQLite; data\\commandcenter.log (5 MB, 3 old files).",
        f"Alerts: {dpapi}; log: private data folder", f"Alerts {_days(r.get('alerts_days'))}; log rotates at 5 MB",
        connected=None, state=f"{n_alerts} stored" if n_alerts is not None else "")

    top3, notes = _table_count("daily_top3"), _table_count("meeting_notes")
    add("daily", "Top 3 and meeting notes", "Work", "What you type into Today's Top 3 and meeting notes.",
        f"{top3 or 0} day(s) of Top 3, {notes or 0} meeting note(s) in SQLite.", "Windows DPAPI (this Windows account)",
        "Top 3: 120 days; meeting notes until deleted", connected=None, state="")

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


async def watch_loop():
    last = _session_fingerprint()
    while True:
        await asyncio.sleep(1.5)
        try:
            fp = _session_fingerprint()
            if fp != last:
                last = fp
                vault.lock("Command Center locked")
            if _shell_locked():
                vault.lock("Command Center locked")
            if vault.unlocked():
                s = settings()
                if s["lock_on_windows_lock"] and windows_locked():
                    vault.lock("Windows locked")
                elif s["auto_lock_minutes"] and vault.unlocked_for() > s["auto_lock_minutes"] * 60:
                    vault.lock(f"auto-lock after {s['auto_lock_minutes']} min")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("Privacy watch: %s", type(e).__name__)


background = [watch_loop]
