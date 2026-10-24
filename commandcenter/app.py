"""FastAPI app: JSON API + the dashboard page. Listens on 127.0.0.1 only."""
import asyncio
import logging
import os
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import alerts, config, db, today
from .connectors import asana, outlook, sql, teams, web

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.prune_seen()
    tasks = [asyncio.create_task(outlook.watch_loop()), asyncio.create_task(web.watch_loop()),
             asyncio.create_task(today.briefing_loop()), asyncio.create_task(teams.watch_loop())]
    alerts.raise_alert("Command Center", "Started", "Watching Outlook and websites.", "low")
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Command Center", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def local_only(request: Request, call_next):
    # Block DNS-rebinding (foreign Host header) and cross-site requests from other web pages:
    # every non-GET call must carry a custom header, which browsers won't send cross-origin.
    port = config.section("server", {}).get("port", 8765)
    if request.headers.get("host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
        return JSONResponse({"error": "forbidden host"}, status_code=403)
    if request.method != "GET" and request.headers.get("x-commandcenter") != "1":
        return JSONResponse({"error": "missing X-CommandCenter header"}, status_code=403)
    return await call_next(request)


@app.exception_handler(Exception)
async def errors(request: Request, exc: Exception):
    log.exception("Request failed: %s", request.url.path)
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
        "focus": alerts.focus_state(),
        "ui": {"accent": config.section("ui", {}).get("accent", "#39ff14"),
               "workday": [t.get("workday_start", "08:00"), t.get("workday_end", "17:00")]},
        "desktop": hasattr(app.state, "show_window"),
        "config_path": str(config.CONFIG_PATH),
    }


@app.post("/api/focus")
async def focus(minutes: int = Body(..., embed=True)):
    alerts.set_focus(minutes)
    return alerts.focus_state()


@app.post("/api/window/show")
async def show_window():
    """Used by a second launch of the app to bring the running window forward."""
    show = getattr(app.state, "show_window", None)
    show() if show else webbrowser.open(alerts.dashboard_url())
    return {"ok": True}


@app.post("/api/open")
async def open_external(url: str = Body(..., embed=True)):
    """Opens a link with Windows (default browser, or the Teams app for msteams: links)."""
    if not url.startswith(("https://", "http://", "msteams:")):
        raise HTTPException(400, "Only web and Teams links can be opened")
    await asyncio.to_thread(os.startfile, url)
    return {"ok": True}


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
                      notes: str = Body("")):
    if not subject.strip():
        raise HTTPException(400, "Task needs a title")
    if source == "asana":
        task_id = await asyncio.to_thread(asana.create_task, subject.strip(), due or None, notes)
    else:
        task_id = await outlook.call(outlook.create_task, subject.strip(), due or None, notes)
    return {"ok": True, "id": task_id}


@app.post("/api/tasks/complete")
async def complete_task(source: str = Body(...), id: str = Body(...)):
    if source == "asana":
        await asyncio.to_thread(asana.complete, id)
    else:
        await outlook.call(outlook.complete_item, id)
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
    return {"set_up": teams.is_set_up(), "state": db.kv_get("teams:state"), "url": teams.url(), "activity": activity}


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


@app.post("/api/mail/reply")
async def mail_reply(id: str = Body(...), text: str = Body(...), reply_all: bool = Body(False), send: bool = Body(True)):
    await outlook.call(outlook.reply, id, text, reply_all, send)
    return {"ok": True}


@app.post("/api/mail/read")
async def mail_read(id: str = Body(...), read: bool = Body(True)):
    await outlook.call(outlook.set_read, id, read)
    return {"ok": True}


@app.post("/api/mail/flag")
async def mail_flag(id: str = Body(...), flagged: bool = Body(True)):
    await outlook.call(outlook.set_flag, id, flagged)
    return {"ok": True}


@app.post("/api/outlook/open")
async def outlook_open(id: str = Body(..., embed=True)):
    await outlook.call(outlook.open_item, id)
    return {"ok": True}


@app.get("/api/meetings")
async def meetings(hours: int = 24):
    return {"meetings": await outlook.call(outlook.upcoming_meetings, min(hours, 24 * 14))}


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
