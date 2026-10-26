"""FastAPI app: JSON API + the dashboard page. Listens on 127.0.0.1 only."""
import asyncio
import hashlib
import importlib
import logging
import os
import secrets
import time
import traceback
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import alerts, config, db, orders, security, today
from . import profile as user_profile
from .connectors import asana, outlook, sql, teams, web

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


def _load_feature_modules():
    """Optional feature modules (each in its own file) that add API routes and background jobs."""
    found = []
    for name in ("security", "toolkit", "pulse", "terminal", "vscode", "planner", "profile", "feeds", "daily", "vault", "privacy",
                 "spam", "offhours", "reminders", "snip"):
        try:
            found.append(importlib.import_module(f".{name}", __package__))
        except ModuleNotFoundError as e:
            if e.name != f"{__package__}.{name}":
                raise  # a real import error inside the module
    return found


FEATURE_MODULES = _load_feature_modules()


def _mark_old_start_alerts_read():
    """One-time clean-up (idempotent): the unread "Command Center / Started" alerts earlier versions raised.
    Titles are encrypted, so they are compared after decrypting."""
    with db._lock:
        rows = db._db().execute("SELECT id, title FROM alerts WHERE source = 'Command Center' AND read = 0").fetchall()
        ids = [r["id"] for r in rows if db.unseal_or(r["title"]) == "Started"]
        for i in ids:
            db._db().execute("UPDATE alerts SET read = 1 WHERE id = ?", (i,))
        db._db().commit()
    if ids:
        log.info("Marked %d old 'Started' alerts read", len(ids))


def frames(exc: BaseException, limit: int = 12) -> str:
    """Where an exception happened, as "file:line function" frames (innermost last), plus the type names of the
    exceptions it was raised from. Never the messages: those can quote data (a pyodbc error can include row
    values, an Outlook error a subject)."""
    parts, seen, e = [], set(), exc
    while e is not None and id(e) not in seen and len(seen) < 4:
        seen.add(id(e))
        tb = traceback.extract_tb(e.__traceback__)[-limit:]
        where = " > ".join(f"{os.path.basename(f.filename)}:{f.lineno} {f.name}" for f in tb)
        parts.append(f"{type(e).__name__} [{where}]" if e is not exc else f"[{where}]")
        e = e.__cause__ or e.__context__
    return " from ".join(parts)


MAINTENANCE_SECONDS = 24 * 3600


def maintenance() -> dict:
    """Encrypts plaintext left by older versions, enforces retention (config `retention:`), and VACUUMs when
    rows were sealed or deleted (or every retention.vacuum_days). Each step is its own verified transaction, so
    a failed step changes nothing and doesn't stop the others. Counts only are logged. Meeting notes are
    encrypted by daily.py itself and kept until deleted."""
    r = db.retention_settings()
    sealed, deleted, failed = {}, {}, []

    def step(name, fn, into):
        try:
            result = fn()
            into.update(result) if isinstance(result, dict) else into.__setitem__(name, result)
        except Exception as e:
            failed.append(name)
            log.error("Data maintenance step failed: %s (nothing was changed by it): %s at %s", name,
                      type(e).__name__, frames(e))

    try:
        from . import toolkit
    except ModuleNotFoundError:
        toolkit = None
    try:
        from . import offhours
    except ModuleNotFoundError:
        offhours = None
    vacuumed = False
    # The rollback journal of these steps holds the old plaintext pages: it is overwritten afterwards, not just
    # deleted (db.scrubbed_journal).
    with db.scrubbed_journal():
        step("seal", db.seal_existing, sealed)
        step("retention", lambda: db.enforce_retention(r), deleted)
        step("dismissed_followups", today.prune_dismissed, deleted)
        if toolkit:
            step("toolkit_changes", toolkit.seal_log, sealed)
            step("toolkit_changes", lambda: toolkit.prune_log(r["toolkit_changes_days"]), deleted)
        if offhours:
            step("offhours", lambda: offhours.prune(r["offhours_days"]), deleted)
        changed = any(sealed.values()) or any(deleted.values())
        try:
            vacuumed = db.vacuum(force=changed, every_days=r["vacuum_days"])
        except Exception as e:
            failed.append("vacuum")
            log.error("Data maintenance step failed: vacuum (%s)", type(e).__name__)
    log.info("Data maintenance: sealed %s, deleted %s, vacuum %s%s", sealed, deleted,
             "done" if vacuumed else "not needed", f", failed: {failed}" if failed else "")
    return {"sealed": sealed, "deleted": deleted, "vacuumed": vacuumed, "failed": failed}


async def maintenance_loop():
    """At start and then daily (see maintenance())."""
    while True:
        try:
            await asyncio.to_thread(maintenance)
        except Exception:
            log.exception("Data maintenance failed")
        await asyncio.sleep(MAINTENANCE_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [asyncio.create_task(maintenance_loop()),  # encryption of legacy rows + retention, at start and daily
             asyncio.create_task(outlook.watch_loop()), asyncio.create_task(web.watch_loop()),
             asyncio.create_task(today.briefing_loop()), asyncio.create_task(teams.watch_loop()),
             asyncio.create_task(orders.clipboard_loop())]
    for module in FEATURE_MODULES:
        for job in getattr(module, "background", []):
            tasks.append(asyncio.create_task(job()))
    # A start is logged, not raised as an alert (each restart and test copy used to add an unread one).
    log.info("Command Center started; watching Outlook and websites")
    try:
        _mark_old_start_alerts_read()
    except Exception:
        log.exception("Couldn't mark old 'Started' alerts read")
    try:
        await asyncio.to_thread(security.folder_alert)  # once per launch, if a folder above the app is unsafe
    except Exception:
        log.exception("Couldn't check the folders above the app")
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Command Center", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def local_only(request: Request, call_next):
    # The access rules live in security.py: foreign Host (DNS rebinding), cross-site fetches,
    # X-CommandCenter + Origin on non-GET (CSRF), same Windows user, session cookie.
    # security.guard() enforces them before the app is reached; this re-checks anything that
    # arrives without passing through it (e.g. the app served some other way).
    denied = security.http_denied(request)
    if denied is not None:
        return denied
    response = await call_next(request)
    if not request.scope.get("cc.guarded"):
        for name, value in security.response_headers(request.url.path):
            response.headers[name.decode()] = value.decode()
    return response


@app.exception_handler(Exception)
async def errors(request: Request, exc: Exception):
    # The log gets the exception type, the path and the code locations only (see frames()), never str(exc).
    log.error("Request failed: %s %s: %s %s", request.method, request.url.path, type(exc).__name__, frames(exc))
    return JSONResponse({"error": str(exc).splitlines()[0] if str(exc) else type(exc).__name__}, status_code=500)


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


# ---- alerts / status ---------------------------------------------------------

@app.get("/api/alerts")
async def get_alerts(unread_only: bool = False):
    return {"alerts": db.list_alerts(unread_only=unread_only), "unread": db.unread_count()}


@app.post("/api/alerts/read")
async def read_alerts(id: int | None = Body(None, embed=True)):
    db.mark_read(id)
    return {"ok": True}


@app.post("/api/alerts/clear")
async def clear_alerts():
    db.clear_alerts()
    return {"ok": True}


@app.get("/api/status")
async def status():
    t = config.section("today", {})
    return {
        "outlook": db.kv_get("outlook:status"),
        "websites": {m["name"]: db.kv_get(f"web:{m['name']}") for m in web.monitors() if m.get("enabled", True)},
        "teams": db.kv_get("teams:state") if teams.is_set_up() else None,
        "asana": await asyncio.to_thread(asana.connected),
        "focus": alerts.focus_state(),
        "ui": {"accent": user_profile.get()["accent"],
               "workday": [t.get("workday_start", "08:00"), t.get("workday_end", "17:00")]},
        "desktop": hasattr(app.state, "show_window"),
        "config_path": str(config.CONFIG_PATH),
        "folder_security": {k: v for k, v in (await asyncio.to_thread(security.folder_security)).items()
                            if k in ("ok", "unsafe", "fix")},
    }


@app.post("/api/focus")
async def focus(minutes: int = Body(..., embed=True)):
    alerts.set_focus(minutes)
    return alerts.focus_state()


@app.post("/api/window/show")
async def show_window():
    """Used by a second launch of the app to bring the running window forward."""
    show = getattr(app.state, "show_window", None)
    show() if show else alerts.open_dashboard()  # serve mode: a fresh one-time sign-in link
    return {"ok": True}


@app.post("/api/open")
async def open_external(url: str = Body(..., embed=True)):
    """Opens a link with Windows (default browser, or the Teams app for msteams: links)."""
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    if (scheme not in ("http", "https", "msteams") or (scheme != "msteams" and not parts.netloc)
            or len(url) > 4000 or any(ord(c) < 32 or c == '"' for c in url)):
        raise HTTPException(400, "Only web and Teams links can be opened")
    await asyncio.to_thread(os.startfile, url)
    return {"ok": True}


@app.get("/api/weather")
async def get_weather(zip: str | None = None):
    from .connectors import weather
    if zip and zip not in [p["zip"] for p in weather.locations()]:
        raise HTTPException(400, "Unknown weather location")
    return await asyncio.to_thread(weather.current, zip)


@app.post("/api/weather/select")
async def select_weather(zip: str = Body(..., embed=True)):
    from .connectors import weather
    try:
        weather.select(zip)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return await asyncio.to_thread(weather.current, zip)


@app.get("/api/search")
async def search(q: str):
    q = q.strip()
    if len(q) < 3:
        return {"mail": []}
    return {"mail": await outlook.call(outlook.search_mail, q[:100])}


@app.post("/api/reload")
async def reload_config():
    config.load()
    return {"ok": True}


# ---- today: agenda, tasks, follow-ups ----------------------------------------

@app.get("/api/today")
async def get_today(refresh: bool = False):
    return await today.build(refresh)


@app.post("/api/tasks")
async def create_task(source: str = Body(...), subject: str = Body(...), due: str | None = Body(None),
                      notes: str = Body(""), priority: str = Body("normal")):
    if not subject.strip():
        raise HTTPException(400, "Task needs a title")
    if priority not in today.PRIORITIES:
        raise HTTPException(400, "Priority is high, normal or low")
    if source == "asana":
        task_id = await asyncio.to_thread(asana.create_task, subject.strip(), due or None, notes)
        if priority != "normal":
            await asyncio.to_thread(today.set_local_priority, "asana", task_id, priority)
    else:
        task_id = await outlook.call(outlook.create_task, subject.strip(), due or None, notes, priority)
    return {"ok": True, "id": task_id}


@app.post("/api/tasks/priority")
async def task_priority(source: str = Body(...), id: str = Body(...), priority: str = Body(...)):
    """High / Normal / Low. Outlook tasks: Outlook's own Importance; Asana: kept locally (today.set_local_priority)."""
    if priority not in today.PRIORITIES:
        raise HTTPException(400, "Priority is high, normal or low")
    if source == "asana":
        if not (isinstance(id, str) and id.isascii() and id.isdigit() and len(id) <= 30):
            raise HTTPException(400, "That isn't an Asana task id")
        await asyncio.to_thread(today.set_local_priority, "asana", id, priority)
    elif source == "outlook":
        try:
            await outlook.call(outlook.set_task_priority, id, priority)
        except ValueError as e:
            raise HTTPException(400, str(e))
    else:
        raise HTTPException(400, "Unknown task source")
    return {"ok": True, "priority": priority}


@app.post("/api/tasks/complete")
async def complete_task(source: str = Body(...), id: str = Body(...)):
    if source == "asana":
        await asyncio.to_thread(asana.complete, id)
    else:
        await outlook.call(outlook.complete_item, id)
    today._followup_cache.clear()  # a completed flag leaves the Flagged follow-ups at once
    return {"ok": True}


@app.post("/api/followups/dismiss")
async def dismiss_followup(id: str = Body(..., embed=True)):
    today.dismiss(id)
    return {"ok": True}


@app.get("/api/followups/muted")
async def muted_senders():
    return {"muted": today.muted()}


@app.post("/api/followups/mute")
async def mute_sender(sender: str = Body(..., embed=True)):
    today.mute(sender)
    return {"ok": True}


@app.post("/api/followups/unmute")
async def unmute_sender(sender: str = Body(..., embed=True)):
    today.unmute(sender)
    return {"ok": True}


@app.get("/api/followups/urgent")
async def get_urgent_people():
    from . import people
    return {"people": people.urgent_people(), "from_config": config.section("followups", {}).get("urgent_people") or [],
            "internal_domains": people.internal_domains()}


@app.post("/api/followups/urgent")
async def save_urgent_people(people_list: list = Body(..., embed=True, alias="people")):
    from . import people
    try:
        saved = people.set_urgent_people(people_list)
    except ValueError as e:
        raise HTTPException(400, str(e))
    today._followup_cache.clear()  # re-sort follow-ups with the new list
    return {"people": saved}


# Important senders (people.py): GET lists them; POST replaces the app list (`senders`, the manage dialog) or
# changes one email's entries (`add` one address / @domain, `remove` some), so two quick marks don't overwrite
# each other. Entries from config.yaml (the "VIP sender" rule) come back read-only in `from_config`.
_important_lock = asyncio.Lock()


def _important_state() -> dict:
    from . import people
    return {"senders": people.app_important_senders(), "from_config": people.config_important_senders(),
            "internal_domains": people.internal_domains(), "shared_providers": people.shared_providers(),
            "max": people.MAX_IMPORTANT}


@app.get("/api/people/important")
async def get_important_senders():
    return _important_state()


@app.post("/api/people/important")
async def save_important_senders(senders: list | None = Body(None), add: str | None = Body(None),
                                 remove: list | None = Body(None)):
    from . import people
    if senders is None and add is None and remove is None:
        raise HTTPException(400, "Nothing to change")
    async with _important_lock:
        current = people.app_important_senders() if senders is None else list(senders)
        if remove:
            drop = {str(r).strip().lower() for r in remove}
            current = [p for p in current if str(p).strip().lower() not in drop]
        if add is not None:
            current.append(add)
        try:
            saved = people.set_important_senders(current)
        except ValueError as e:
            raise HTTPException(400, str(e))
    today._followup_cache.clear()  # the "Important senders" group is rescanned with the new list
    log.info("Important senders saved: %d in the app list", len(saved))
    return _important_state()


@app.post("/api/asana/token")
async def asana_token(token: str = Body(..., embed=True)):
    return {"ok": True, "name": await asyncio.to_thread(asana.set_token, token)}


@app.post("/api/asana/disconnect")
async def asana_disconnect():
    await asyncio.to_thread(asana.disconnect)
    return {"ok": True}


# ---- teams -------------------------------------------------------------------

@app.get("/api/teams")
async def teams_info():
    try:
        activity = await outlook.call(outlook.teams_activity)
    except Exception as e:
        activity = {"error": str(e)}
    # recent: unread / recent DMs and @mentions from the background read (names and previews, memory only)
    return {"set_up": teams.is_set_up(), "state": db.kv_get("teams:state"), "url": teams.url(), "activity": activity,
            **teams.details()}


@app.post("/api/teams/login")
async def teams_login():
    asyncio.create_task(teams.login())
    return {"ok": True}


@app.post("/api/teams/check")
async def teams_check():
    return await teams.check()


# ---- outlook -----------------------------------------------------------------

@app.get("/api/mail")
async def mail(unread_only: bool = False, limit: int = 50):
    return {"mail": await outlook.call(outlook.recent_mail, min(limit, 200), unread_only)}


@app.get("/api/mail/item")
async def mail_item(id: str):
    return await outlook.call(outlook.get_message, id)


# ---- verified sending: nothing is sent unless it was reviewed first ------------------------------
# /preview builds the reply as Outlook would send it and returns a single-use approval for exactly that
# text and those recipients. /reply with send=true only works with that approval (5-minute lifetime),
# and Outlook re-checks the recipients right before sending. Opening a draft (send=false) needs no approval
# because Joe presses Send in Outlook himself.
APPROVAL_SECONDS = 300
_approvals: dict[str, dict] = {}


def _approval_key(id, text, reply_all) -> str:
    return hashlib.sha256(f"{id}\0{reply_all}\0{text}".encode("utf-8")).hexdigest()


@app.post("/api/mail/reply/preview")
async def mail_reply_preview(id: str = Body(...), text: str = Body(...), reply_all: bool = Body(False)):
    if not text.strip():
        raise HTTPException(400, "Write a reply first")
    if len(text) > 20000:
        raise HTTPException(400, "Reply is too long")
    preview = await outlook.call(outlook.reply_preview, id, text, reply_all)
    now = time.time()
    for k in [k for k, v in _approvals.items() if v["expires"] < now]:
        _approvals.pop(k)
    approval_id = secrets.token_urlsafe(18)
    _approvals[approval_id] = {"key": _approval_key(id, text, reply_all), "signature": preview["signature"],
                               "expires": now + APPROVAL_SECONDS}
    return {"approval_id": approval_id, "subject": preview["subject"], "recipients": preview["recipients"],
            "text": text, "expires_in": APPROVAL_SECONDS}


@app.post("/api/mail/reply")
async def mail_reply(id: str = Body(...), text: str = Body(...), reply_all: bool = Body(False), send: bool = Body(True),
                     remind_days: float = Body(0), approval_id: str | None = Body(None)):
    signature = None
    if send:
        approval = _approvals.pop(approval_id or "", None)  # single use, even if it then fails
        if not approval or approval["expires"] < time.time():
            raise HTTPException(409, "Review the reply before sending (the review expired or was already used)")
        if approval["key"] != _approval_key(id, text, reply_all):
            raise HTTPException(409, "The reply changed after you reviewed it. Review it again.")
        signature = approval["signature"]
    try:
        info = await outlook.call(outlook.reply, id, text, reply_all, send, signature)
    except ValueError as e:
        raise HTTPException(409, str(e))
    if send:  # encrypted at rest (db.kv_set); entries older than retention.mail_sent_log_days are dropped
        cutoff = db._cutoff(db.retention_settings()["mail_sent_log_days"])
        sent = [e for e in db.kv_get("mail:sent_log", []) if str(e.get("ts", "")) >= cutoff]
        sent.insert(0, {"ts": db.now(), "subject": info["subject"], "to": info["recipients"][:20], "via": "Command Center"})
        db.kv_set("mail:sent_log", sent[:200])
        if remind_days > 0:
            today.watch_for_reply(info, remind_days, id)
    return {"ok": True}


@app.get("/api/mail/sent-log")
async def mail_sent_log():
    """Replies sent from Command Center: when, subject and recipients (no message bodies)."""
    return {"sent": db.kv_get("mail:sent_log", [])}


@app.post("/api/mail/read")
async def mail_read(id: str = Body(...), read: bool = Body(True)):
    await outlook.call(outlook.set_read, id, read)
    return {"ok": True}


@app.post("/api/mail/flag")
async def mail_flag(id: str = Body(...), flagged: bool = Body(True)):
    await outlook.call(outlook.set_flag, id, flagged)
    today._followup_cache.clear()  # the Flagged follow-ups change at once
    return {"ok": True}


@app.post("/api/outlook/open")
async def outlook_open(id: str = Body(..., embed=True)):
    await outlook.call(outlook.open_item, id)
    return {"ok": True}


@app.get("/api/meetings")
async def meetings(hours: int = 24):
    return {"meetings": await outlook.call(outlook.upcoming_meetings, min(hours, 24 * 14))}


@app.get("/api/meetings/prep")
async def meeting_prep(id: str):
    return await outlook.call(outlook.meeting_prep, id)


@app.post("/api/calendar/block")
async def calendar_block(subject: str = Body(...), start: str = Body(...), minutes: int = Body(60), notes: str = Body("")):
    if not 5 <= minutes <= 600:
        raise HTTPException(400, "Pick a length between 5 minutes and 10 hours")
    return {"ok": True, "id": await outlook.call(outlook.create_block, subject, start, minutes, notes)}


# ---- snooze / reminders --------------------------------------------------------

@app.get("/api/snoozes")
async def get_snoozes():
    return {"snoozes": today.snoozes(), "reply_watches": today.reply_watches()}


@app.post("/api/snooze")
async def snooze(id: str = Body(...), until: str = Body(...), subject: str = Body(""), who: str = Body(""), kind: str = Body("mail")):
    today.snooze(id, until, {"subject": subject, "who": who, "kind": kind})
    return {"ok": True}


@app.post("/api/unsnooze")
async def unsnooze(id: str = Body(..., embed=True)):
    today.unsnooze(id)
    return {"ok": True}


@app.post("/api/reply-watch")
async def reply_watch(id: str = Body(...), days: float = Body(2)):
    info = await outlook.call(outlook.conversation_of, id)
    today.watch_for_reply(info, days, id)
    return {"ok": True}


@app.post("/api/reply-watch/cancel")
async def cancel_reply_watch(conversation: str = Body(..., embed=True)):
    today.cancel_reply_watch(conversation)
    return {"ok": True}


# ---- orders --------------------------------------------------------------------

@app.get("/api/orders/lookup")
async def order_lookup(number: str):
    if not number.isdigit():
        raise HTTPException(400, "Order numbers are digits only")
    return await asyncio.to_thread(orders.lookup, number)


@app.get("/api/orders/status")
async def order_status():
    return {"problem": await asyncio.to_thread(orders.ready),
            "pattern": orders.settings().get("pattern", orders.DEFAULT_PATTERN)}


# ---- websites ----------------------------------------------------------------

@app.get("/api/websites")
async def websites():
    return {"monitors": [{**m, "state": db.kv_get(f"web:{m['name']}")} for m in web.monitors()]}


@app.post("/api/websites/check")
async def website_check(name: str = Body(..., embed=True)):
    m = web.find(name)
    if not m:
        raise HTTPException(404, f"No monitor named {name!r}")
    return await web.check(m)


@app.post("/api/websites/login")
async def website_login(name: str = Body(..., embed=True)):
    m = web.find(name)
    if not m:
        raise HTTPException(404, f"No monitor named {name!r}")
    asyncio.create_task(web.login(m.get("profile", "public"), m.get("login_url") or m["url"]))
    return {"ok": True}


# ---- sql ---------------------------------------------------------------------

@app.get("/api/sql")
async def sql_info():
    conns = []
    for c in sql.connections():
        conns.append({k: c.get(k) for k in ("name", "server", "database", "username", "auth")}
                     | {"password_saved": await asyncio.to_thread(sql.has_password, c["name"])})
    return {"connections": conns, "queries": [{k: q.get(k) for k in ("name", "connection", "params", "description")}
                                              for q in sql.queries()]}


@app.post("/api/sql/password")
async def sql_password(connection: str = Body(...), password: str = Body(...)):
    await asyncio.to_thread(sql.set_password, connection, password)
    return {"ok": True}


@app.post("/api/sql/test")
async def sql_test(connection: str = Body(..., embed=True)):
    return await asyncio.to_thread(sql.test, connection)


@app.post("/api/sql/run")
async def sql_run(query: str = Body(...), params: list = Body([])):
    return await asyncio.to_thread(sql.run_saved, query, params)


@app.get("/api/sql/tables")
async def sql_tables(connection: str):
    return {"tables": await asyncio.to_thread(sql.tables, connection)}


@app.get("/api/sql/preview")
async def sql_preview(connection: str, table: str):
    return await asyncio.to_thread(sql.preview, connection, table)


# ---- projects (Claude Code) ------------------------------------------------------

@app.get("/api/projects")
async def get_projects():
    from . import projects
    return {"projects": await asyncio.to_thread(projects.list_projects), "claude": await asyncio.to_thread(projects.claude_path),
            "roots": [str(r) for r in projects.roots()]}


@app.post("/api/projects/launch")
async def launch_project(path: str = Body(...), resume: bool = Body(False), prompt: str = Body(""),
                         allow_unsafe: bool = Body(False)):
    from . import projects
    try:
        await asyncio.to_thread(projects.launch, path, resume, prompt, allow_unsafe)
    except projects.UnsafeFolder as e:
        return JSONResponse({"error": str(e), "unsafe_folder": True}, status_code=409)
    except ValueError as e:  # not a project / bad prompt: a 400, not a logged server error
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True}


@app.post("/api/projects/folder")
async def open_project_folder(path: str = Body(..., embed=True)):
    from . import projects
    await asyncio.to_thread(projects.open_folder, path)
    return {"ok": True}


@app.post("/api/projects/new")
async def new_project(name: str = Body(..., embed=True)):
    from . import projects
    return await asyncio.to_thread(projects.create, name)


for _module in FEATURE_MODULES:
    if hasattr(_module, "router"):
        app.include_router(_module.router)
