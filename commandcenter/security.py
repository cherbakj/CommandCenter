"""Security: who may talk to the local server, the Windows Hello unlock, and folder permissions.

Every connection to the server must pass all of these (guard() checks them before the app sees a
request; app.py's local_only middleware and ws_allowed() check them again):
  1. Loopback only: the server listens on 127.0.0.1 with SO_EXCLUSIVEADDRUSE (listen_socket), so
     nothing on the network can connect and no other account can take over the port.
  2. Same Windows user: the process at the other end of each connection must run as the user who
     started Command Center (TCP table -> owning PID -> process token SID). Other accounts on this
     PC are refused even if they learn the session cookie.
  3. Session cookie: a random per-launch secret, held only in memory, sent as an HttpOnly,
     SameSite=Strict cookie. The only way to get it is a one-time /auth?k=<code> link, which the
     desktop window opens after Windows Hello (or `serve` prints). Everything except /auth needs it.
  4. Browser rules: Host must be 127.0.0.1/localhost:<port> (DNS rebinding); requests a browser
     marks cross-site or same-site (another localhost port) are refused; non-GET requests need
     X-CommandCenter: 1 and our Origin when one is sent (CSRF); WebSockets need our exact Origin
     (cross-site WebSocket hijacking); CSP frame-ancestors + X-Frame-Options (clickjacking).

ws_allowed(websocket): call first in every WebSocket endpoint; closes and returns False unless all
of the above pass.

CC_DEV_NO_AUTH=1 skips only the cookie (3), and only in `serve` mode (configure("serve")).
GET routes must never change anything: only non-GET requests get the CSRF header check.
"""
import ctypes
import ctypes.wintypes as wt
import hmac
import html
import json
import logging
import os
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

import pywintypes
import win32api
import win32event
import win32process
import win32security
import winerror
from fastapi import APIRouter, Request, WebSocket
from starlette.datastructures import Headers
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import config

log = logging.getLogger(__name__)

# Per-launch secrets, held in memory. Only HANDOFF_SECRET is ever written down: into
# data/instance-<port>.json, in a folder only this user, SYSTEM and Administrators can read.
SESSION_SECRET = secrets.token_urlsafe(32)
HANDOFF_SECRET = secrets.token_urlsafe(32)
HANDOFF_HEADER = "x-cc-handoff"
PUBLIC_PATHS = {"/auth"}

_state = {"mode": "app", "dev_no_auth": False}
_codes: dict[str, float] = {}  # one-time bootstrap code -> expiry (monotonic seconds)
_codes_lock = threading.Lock()


def settings() -> dict:
    return config.section("security", {}) or {}


def port() -> int:
    return int(config.section("server", {}).get("port", 8765))


def _allowed_hosts() -> set[str]:
    return {f"127.0.0.1:{port()}", f"localhost:{port()}"}


def _allowed_origins() -> set[str]:
    return {f"http://{h}" for h in _allowed_hosts()}


def cookie_name() -> str:
    # Cookies ignore ports, so the name carries it: a test copy on another port can't clobber this one.
    return f"cc_session_{port()}"


def configure(mode: str):
    """Call once at start-up with "app" or "serve". CC_DEV_NO_AUTH=1 is honored only for "serve"."""
    _state["mode"] = mode
    wanted = os.environ.get("CC_DEV_NO_AUTH") == "1"
    _state["dev_no_auth"] = wanted and mode == "serve"
    if _state["dev_no_auth"]:
        bar = "!" * 78
        msg = (f"\n{bar}\n!!  CC_DEV_NO_AUTH=1: the session cookie is NOT required on port {port()}.\n"
               f"!!  Development and testing only. Host, Origin, X-CommandCenter and same-user checks still apply.\n{bar}")
        log.warning(msg)
        if sys.stderr:
            print(msg, file=sys.stderr, flush=True)
    elif wanted:
        log.warning("CC_DEV_NO_AUTH is set but ignored: it only works with `serve`")


# ---- one-time bootstrap codes and the session cookie ------------------------------

def new_bootstrap_code(ttl: float = 60) -> str:
    code = secrets.token_urlsafe(32)
    now = time.monotonic()
    with _codes_lock:
        for k, expiry in list(_codes.items()):
            if expiry < now:
                del _codes[k]
        while len(_codes) >= 8:
            _codes.pop(next(iter(_codes)))
        _codes[code] = now + ttl
    return code


def bootstrap_url(ttl: float = 60) -> str:
    return f"http://127.0.0.1:{port()}/auth?k={new_bootstrap_code(ttl)}"


def _consume_code(code: str) -> bool:
    if not code or len(code) > 100:
        return False
    found = None
    with _codes_lock:
        for k in list(_codes):
            if hmac.compare_digest(k.encode(), code.encode("utf-8", "replace")):
                found = k
        if found is None:
            return False
        expiry = _codes.pop(found)  # one use only
    return expiry >= time.monotonic()


def _has_session(headers: Headers) -> bool:
    for part in headers.get("cookie", "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == cookie_name() and hmac.compare_digest(value.encode("utf-8", "replace"), SESSION_SECRET.encode()):
            return True
    return False


def _has_handoff(scope, headers: Headers) -> bool:
    supplied = headers.get(HANDOFF_HEADER, "")
    return (scope.get("type") == "http" and scope.get("method") == "POST" and scope.get("path") == "/api/window/show"
            and bool(supplied) and hmac.compare_digest(supplied.encode("utf-8", "replace"), HANDOFF_SECRET.encode()))


# ---- same Windows user ---------------------------------------------------------------

def _token_sid(handle):
    token = win32security.OpenProcessToken(handle, win32security.TOKEN_QUERY)
    try:
        return win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()


MY_SID = _token_sid(win32api.GetCurrentProcess())
MY_SID_STR = win32security.ConvertSidToStringSid(MY_SID)


class _TcpRow(ctypes.Structure):
    _fields_ = [("state", wt.DWORD), ("local_addr", wt.DWORD), ("local_port", wt.DWORD),
                ("remote_addr", wt.DWORD), ("remote_port", wt.DWORD), ("pid", wt.DWORD)]


_iphlpapi = ctypes.WinDLL("iphlpapi")
_iphlpapi.GetExtendedTcpTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.DWORD), wt.BOOL, wt.ULONG, ctypes.c_int, wt.ULONG]
_iphlpapi.GetExtendedTcpTable.restype = wt.DWORD
TCP_TABLE_OWNER_PID_CONNECTIONS = 4


def _ipv4(addr: str) -> int:
    return struct.unpack("<I", socket.inet_aton(addr))[0]


def connection_pid(client, server_port: int) -> int | None:
    """PID of the process that owns the client end of a loopback TCP connection to us."""
    if not client or len(client) < 2:
        return None
    try:
        client_addr = _ipv4(client[0])
    except OSError:
        return None  # not IPv4 (we only listen on 127.0.0.1)
    size = wt.DWORD(64 * 1024)
    for _ in range(5):
        buf = ctypes.create_string_buffer(size.value)
        rc = _iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False, socket.AF_INET, TCP_TABLE_OWNER_PID_CONNECTIONS, 0)
        if rc == 0:
            break
        if rc != 122:  # ERROR_INSUFFICIENT_BUFFER: size now holds what's needed
            return None
        size = wt.DWORD(size.value + 4096)
    else:
        return None
    count = struct.unpack_from("<I", buf, 0)[0]
    rows = (_TcpRow * count).from_buffer(buf, 4)
    want_local, want_remote = socket.htons(int(client[1])), socket.htons(server_port)
    loopback = _ipv4("127.0.0.1")
    for r in rows:
        if (r.local_port & 0xFFFF) == want_local and (r.remote_port & 0xFFFF) == want_remote \
                and r.local_addr == client_addr and r.remote_addr == loopback:
            return r.pid
    return None


# (pid, process creation time) -> same user? A PID can be reused by another account's process the
# moment the first one exits, so the creation time is part of the key: a recycled PID is a new entry.
_pid_cache: dict[tuple[int, int], bool] = {}


def _creation_time(handle) -> int:
    t = win32process.GetProcessTimes(handle)["CreationTime"]
    return int(t.timestamp() * 1_000_000) if hasattr(t, "timestamp") else int(t)


def pid_is_current_user(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        handle = win32api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    except pywintypes.error:
        return False  # another account's (or a protected) process: we may not even open it
    try:
        key = (pid, _creation_time(handle))
        cached = _pid_cache.get(key)
        if cached is not None:
            return cached
        ok = _token_sid(handle) == MY_SID
    except pywintypes.error:
        return False
    finally:
        handle.Close()
    if len(_pid_cache) > 256:
        _pid_cache.clear()
    _pid_cache[key] = ok
    return ok


def same_user(scope) -> bool:
    """Always on (there is no setting to turn it off)."""
    client = scope.get("client")
    server = scope.get("server") or ("127.0.0.1", port())
    pid = connection_pid(client, int(server[1]))
    if pid is None:
        log.warning("Refused a connection: couldn't tell which process opened it")
        return False
    if not pid_is_current_user(pid):
        log.warning("Refused a connection from process %s, which belongs to another Windows account", pid)
        return False
    return True


# ---- the request check (used by guard, local_only and ws_allowed) ----------------------

def check(scope) -> tuple[int, str] | None:
    """None when the request may proceed, else (HTTP status, reason)."""
    headers = Headers(scope=scope)
    if headers.get("host") not in _allowed_hosts():
        return 403, "forbidden host"
    if headers.get("sec-fetch-site") in ("cross-site", "same-site"):
        return 403, "cross-site request refused"
    origin = headers.get("origin")
    if scope["type"] == "websocket":
        if origin not in _allowed_origins():
            return 403, "forbidden origin"
    elif scope.get("method") not in ("GET", "HEAD"):
        if headers.get("x-commandcenter") != "1":
            return 403, "missing X-CommandCenter header"
        if origin is not None and origin not in _allowed_origins():
            return 403, "forbidden origin"
    if not same_user(scope):
        return 403, "forbidden"
    if scope["type"] == "http" and scope.get("path") in PUBLIC_PATHS:
        return None
    if _has_session(headers) or _has_handoff(scope, headers) or _state["dev_no_auth"]:
        return None
    return 401, "not signed in"


CSP_TEMPLATE = ("default-src 'self'; script-src 'self'; "
                "style-src 'self' 'unsafe-inline'; "
                "font-src 'self'; img-src 'self' data: blob:; "
                "connect-src 'self' ws://127.0.0.1:{port} ws://localhost:{port}; "
                "frame-src http://127.0.0.1:*; frame-ancestors 'self'; "
                "object-src 'none'; base-uri 'none'; form-action 'self'")


def response_headers(path: str) -> list[tuple[bytes, bytes]]:
    headers = [
        (b"content-security-policy", CSP_TEMPLATE.format(port=port()).encode()),
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"no-referrer"),
        (b"x-frame-options", b"SAMEORIGIN"),
        (b"cross-origin-opener-policy", b"same-origin"),
        (b"cross-origin-resource-policy", b"same-origin"),
        (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
    ]
    if not path.startswith("/static/"):
        headers.append((b"cache-control", b"no-store"))  # keep mail etc. out of the browser's disk cache
    return headers


def _page(title: str, text: str) -> str:
    return ("<!doctype html><meta charset='utf-8'><title>Command Center</title>"
            "<body style='background:#05070d;color:#cfe;font:16px Segoe UI,sans-serif;display:grid;"
            "place-items:center;height:100vh;margin:0'><div style='max-width:34rem'>"
            f"<h2 style='color:#39ff14'>{html.escape(title)}</h2><p>{html.escape(text)}</p></div>")


def denied_response(scope, status: int, reason: str) -> Response:
    if scope.get("path", "").startswith("/api/") or status == 403:
        return JSONResponse({"error": reason}, status_code=status)
    hint = ("Run `run.cmd serve` and open the one-time link it prints."
            if _state["mode"] == "serve" else "Open Command Center from the Start menu or the tray icon.")
    return HTMLResponse(_page("Command Center is locked", hint), status_code=status)


class guard:
    """ASGI wrapper: runs check() before the app sees any HTTP request or WebSocket and adds
    security headers to every HTTP response. Serve the app as guard(app)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        denied = check(scope)
        if denied:
            if scope["type"] == "websocket":
                await receive()  # websocket.connect
                await send({"type": "websocket.close", "code": 1008})  # the handshake gets HTTP 403
                return
            return await self._with_headers(denied_response(scope, *denied), scope, receive, send)
        scope["cc.guarded"] = True
        if scope["type"] == "websocket":
            return await self.app(scope, receive, send)
        return await self._with_headers(self.app, scope, receive, send)

    @staticmethod
    async def _with_headers(app, scope, receive, send):
        extra = response_headers(scope.get("path", ""))
        names = {k for k, _ in extra}

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                current = list(message.get("headers", []))
                # a route may send its own CSP (e.g. a stricter one); everything else is ours
                own_csp = any(k.lower() == b"content-security-policy" for k, _ in current)
                current = [(k, v) for k, v in current
                           if k.lower() not in names or (own_csp and k.lower() == b"content-security-policy")]
                current += [(k, v) for k, v in extra if not (own_csp and k == b"content-security-policy")]
                message = {**message, "headers": current}
            await send(message)

        await app(scope, receive, send_with_headers)


def http_denied(request: Request) -> Response | None:
    """For app.py's local_only middleware: re-checks any request that didn't come through guard()."""
    if request.scope.get("cc.guarded"):
        return None
    denied = check(request.scope)
    return denied_response(request.scope, *denied) if denied else None


async def ws_allowed(websocket: WebSocket) -> bool:
    denied = check(websocket.scope)
    if denied:
        log.warning("WebSocket refused (%s): %s", denied[1], websocket.scope.get("path"))
        await websocket.close(code=1008)
        return False
    return True


def end_session():
    """Lock: a new session secret, so every cookie issued so far stops working; pending links too."""
    global SESSION_SECRET
    SESSION_SECRET = secrets.token_urlsafe(32)
    with _codes_lock:
        _codes.clear()
    try:  # locking Command Center also locks the personal vault immediately (its own poller is the backup)
        from . import vault
        vault.lock("Command Center locked")
    except Exception:
        log.warning("Personal vault lock on session end failed", exc_info=False)


router = APIRouter()

# ---- window / session / app controls (desktop app only) ------------------------------------
# The desktop app registers itself here (shell = DesktopApp). Like every /api route these need the
# session cookie, and as POSTs they also need X-CommandCenter and our Origin.
shell = None


def _no_shell():
    return JSONResponse({"error": "Only in the desktop app"}, status_code=409)


@router.get("/api/window/state", include_in_schema=False)
async def window_state():
    return shell.window_state() if shell else {"desktop": False, "fullscreen": False}


@router.post("/api/window/minimize", include_in_schema=False)
async def window_minimize():
    if not shell:
        return _no_shell()
    shell.minimize()
    return {"ok": True}


@router.post("/api/window/fullscreen", include_in_schema=False)
async def window_fullscreen():
    if not shell:
        return _no_shell()
    return {"fullscreen": shell.toggle_fullscreen()}


@router.post("/api/window/hide", include_in_schema=False)
async def window_hide():
    if not shell:
        return _no_shell()
    shell.hide()
    return {"ok": True}


@router.post("/api/session/lock", include_in_schema=False)
async def session_lock():
    if not shell:
        return _no_shell()
    end_session()
    log.info("Locked from the dashboard; Windows Hello is needed to get back in")
    threading.Timer(0.2, shell.lock).start()  # let this response reach the page first
    response = JSONResponse({"ok": True, "locked": True})
    response.delete_cookie(cookie_name(), path="/", httponly=True, samesite="strict")
    return response


@router.post("/api/app/restart", include_in_schema=False)
async def app_restart():
    if not shell:
        return _no_shell()
    log.info("Restart requested from the dashboard")
    threading.Timer(0.3, shell.restart).start()
    return {"ok": True, "restarting": True}


@router.get("/auth", include_in_schema=False)
async def auth(k: str = ""):
    """Exchanges a one-time code for the session cookie, then opens the dashboard."""
    if not _consume_code(k):
        log.warning("Refused a sign-in link that was invalid, already used or expired")
        return HTMLResponse(_page("This sign-in link has expired",
                                  "Links work once. Open Command Center again from the Start menu or tray"
                                  + (", or restart run.cmd serve." if _state["mode"] == "serve" else ".")), 401)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(cookie_name(), SESSION_SECRET, httponly=True, samesite="strict", path="/")
    return response


# ---- network socket ---------------------------------------------------------------------

def listen_socket(listen_port: int) -> socket.socket:
    """127.0.0.1 only, and exclusive: Windows won't let another socket (or account) share the port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        sock.bind(("127.0.0.1", listen_port))
    except OSError:
        sock.close()
        raise
    sock.set_inheritable(False)
    return sock


# ---- single instance + handoff ------------------------------------------------------------
# A named mutex in this logon session's Local\ namespace tells a second launch that we're running.
# It then reads the handoff secret from data/instance-<port>.json (folder readable only by this
# user, SYSTEM and Administrators) and POSTs /api/window/show with it. The secret authorizes that
# one route only, and the same-user check applies to it as well.

_mutex = None


def claim_instance(listen_port: int) -> bool:
    """True if this is the first copy for this user and port. Keep running while you hold it."""
    global _mutex
    _mutex = win32event.CreateMutex(None, False, f"Local\\Sonance.CommandCenter.{MY_SID_STR}.{listen_port}")
    return win32api.GetLastError() != winerror.ERROR_ALREADY_EXISTS


def _instance_file(listen_port: int):
    return config.DATA_DIR / f"instance-{listen_port}.json"


def write_instance(listen_port: int):
    restrict_folder(config.DATA_DIR)  # make sure the folder is private before the secret lands in it
    _instance_file(listen_port).write_text(
        json.dumps({"pid": os.getpid(), "port": listen_port, "handoff": HANDOFF_SECRET}), encoding="utf-8")


def remove_instance(listen_port: int):
    try:
        data = json.loads(_instance_file(listen_port).read_text(encoding="utf-8"))
        if data.get("pid") == os.getpid():
            _instance_file(listen_port).unlink()
    except (OSError, ValueError):
        pass


def handoff_show(listen_port: int, wait_seconds: float = 15) -> bool:
    """Asks the running copy to show its window. Retries while it's still starting up."""
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        try:
            data = json.loads(_instance_file(listen_port).read_text(encoding="utf-8"))
            req = urllib.request.Request(f"http://127.0.0.1:{listen_port}/api/window/show", data=b"{}", method="POST",
                                         headers={"X-CommandCenter": "1", "Content-Type": "application/json",
                                                  "X-CC-Handoff": data["handoff"]})
            with urllib.request.urlopen(req, timeout=3) as r:
                return r.status == 200
        except Exception:
            time.sleep(0.5)
    return False


# ---- folder permissions ----------------------------------------------------------------------

SYSTEM_SID = "S-1-5-18"
ADMINS_SID = "S-1-5-32-544"
FILE_ALL_ACCESS = 0x1F01FF
NO_WINDOW = subprocess.CREATE_NO_WINDOW


def _acl_is_private(path) -> bool:
    sd = win32security.GetNamedSecurityInfo(str(path), win32security.SE_FILE_OBJECT,
                                            win32security.DACL_SECURITY_INFORMATION)
    if not sd.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED:
        return False
    dacl = sd.GetSecurityDescriptorDacl()
    if dacl is None:
        return False  # a NULL DACL means everyone has full access
    allowed = {MY_SID_STR, SYSTEM_SID, ADMINS_SID}
    me_full = False
    for i in range(dacl.GetAceCount()):
        (ace_type, _flags), mask, sid = dacl.GetAce(i)
        sid_str = win32security.ConvertSidToStringSid(sid)
        if ace_type == win32security.ACCESS_ALLOWED_ACE_TYPE and sid_str not in allowed:
            return False
        if sid_str == MY_SID_STR and mask & FILE_ALL_ACCESS == FILE_ALL_ACCESS:
            me_full = True
    return me_full


def restrict_folder(path) -> bool:
    """Makes path (and everything that inherits from it) accessible only to this user, SYSTEM and
    Administrators. Administrators stay because they can take ownership anyway; dropping them would
    only break backup and IT tools. Returns True if anything had to change."""
    if _acl_is_private(path):
        return False
    grant = [f"*{sid}:(OI)(CI)F" for sid in (MY_SID_STR, SYSTEM_SID, ADMINS_SID)]
    out = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", *grant, "/C", "/Q"],
                         capture_output=True, text=True, creationflags=NO_WINDOW, timeout=600)
    if out.returncode != 0 or not _acl_is_private(path):
        raise OSError(f"icacls couldn't restrict {path}: {(out.stdout + out.stderr).strip()[:300]}")
    log.info("Restricted %s to this user, SYSTEM and Administrators", path)
    return True


def harden_folders():
    """The whole app folder: code and .venv (so no other account can plant code that later runs as
    you) and data/ (database, log, browser sessions, WebView2 profile). Idempotent; fast once applied."""
    for path in (config.ROOT, config.DATA_DIR):
        try:
            restrict_folder(path)
        except Exception as e:
            log.error("Couldn't restrict permissions on %s: %s", path, e)
    try:
        status = folder_security(max_age=0)
        if not status["ok"]:
            log.warning("Folders above the app folder are writable by other accounts: %s", ", ".join(status["unsafe"]))
    except Exception:
        log.exception("Couldn't check the permissions of the folders above the app folder")


# ---- who else can change a folder (read-only checks; nothing here changes an ACL) ----------------

CREATOR_OWNER_SID = "S-1-3-0"
OWNER_RIGHTS_SID = "S-1-3-4"
TRUSTED_INSTALLER_SID = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
TRUSTED_SIDS = {MY_SID_STR, SYSTEM_SID, ADMINS_SID, CREATOR_OWNER_SID, TRUSTED_INSTALLER_SID}
_ALLOW_ACE_TYPES = {0, 5, 9, 11}  # ACCESS_ALLOWED (plain, object, callback, callback object)
_INHERIT_ONLY_ACE = 0x08
_INHERITED_ACE = 0x10
# Rights that let an account add files or folders, write, delete or rename entries, rewrite the ACL or
# take ownership: FILE_WRITE_DATA/ADD_FILE, FILE_APPEND_DATA/ADD_SUBDIRECTORY, FILE_DELETE_CHILD, DELETE,
# WRITE_DAC, WRITE_OWNER, GENERIC_WRITE, GENERIC_ALL.
WRITE_RIGHTS = 0x2 | 0x4 | 0x40 | 0x10000 | 0x40000 | 0x80000 | 0x40000000 | 0x10000000
# A drive root lets every signed-in user create new folders in it (Windows default). That can't touch
# folders that already exist, so on a root only deleting, renaming or re-permissioning entries counts.
ROOT_RIGHTS = 0x40 | 0x40000 | 0x80000 | 0x40000000 | 0x10000000


def writable_by(path, drive_root: bool = False) -> list[tuple[str, bool]]:
    """(SID, explicit) for each account outside TRUSTED_SIDS that can change path (see WRITE_RIGHTS),
    including a foreign owner (owners can always rewrite the ACL). [] means only you, SYSTEM,
    Administrators and TrustedInstaller can. Raises pywintypes.error if the ACL can't be read."""
    sd = win32security.GetNamedSecurityInfo(str(path), win32security.SE_FILE_OBJECT,
                                            win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION)
    found: dict[str, bool] = {}
    owner = sd.GetSecurityDescriptorOwner()
    owner_str = win32security.ConvertSidToStringSid(owner) if owner else ""
    if owner_str not in TRUSTED_SIDS:
        found[owner_str or "(no owner)"] = True
    dacl = sd.GetSecurityDescriptorDacl()
    if dacl is None:
        return [("S-1-1-0", True)]  # NULL DACL: everyone has full access
    rights = ROOT_RIGHTS if drive_root else WRITE_RIGHTS
    for i in range(dacl.GetAceCount()):
        ace = dacl.GetAce(i)
        (ace_type, flags), mask, sid = ace[0], ace[1], ace[-1]
        if ace_type not in _ALLOW_ACE_TYPES or flags & _INHERIT_ONLY_ACE or not mask & rights:
            continue
        sid_str = win32security.ConvertSidToStringSid(sid)
        if sid_str in TRUSTED_SIDS or (sid_str == OWNER_RIGHTS_SID and owner_str in TRUSTED_SIDS):
            continue
        found[sid_str] = found.get(sid_str, False) or not flags & _INHERITED_ACE
    return list(found.items())


_names: dict[str, str] = {}


def account_name(sid_str: str) -> str:
    if sid_str not in _names:
        try:
            name, domain, _ = win32security.LookupAccountSid(None, win32security.ConvertStringSidToSid(sid_str))
            _names[sid_str] = f"{domain}\\{name}" if domain else name
        except pywintypes.error:
            _names[sid_str] = sid_str
    return _names[sid_str]


def _fix_command(path) -> str:
    target = f'"{path}"' if " " in str(path) else str(path)
    return (f"icacls {target} /inheritance:r /grant:r *{MY_SID_STR}:(OI)(CI)F *{SYSTEM_SID}:(OI)(CI)F "
            f"*{ADMINS_SID}:(OI)(CI)F")


def check_ancestors(folder) -> dict:
    """Every folder above `folder`, up to the drive root: can another account change it? Anyone who can
    rename or replace one of them can swap in their own copy of the app (code, .venv) that you then run."""
    folder = os.path.abspath(str(folder))
    chain = []
    parent = os.path.dirname(folder)
    while parent and parent not in chain:
        chain.append(parent)
        if os.path.dirname(parent) == parent:
            break
        parent = os.path.dirname(parent)
    unsafe, details, commands = [], {}, []
    for path in chain:  # nearest first
        root = os.path.dirname(path) == path
        try:
            who = writable_by(path, drive_root=root)
        except pywintypes.error as e:
            who = [(f"(couldn't read the permissions: {e.strerror})", True)]
        if not who:
            continue
        unsafe.append(path)
        details[path] = [account_name(sid) if sid.startswith("S-") else sid for sid, _ in who]
        if not root:
            commands.append((path, any(explicit for _, explicit in who)))
    # Fix the top-most unsafe folder (its children inherit from it); deeper ones only if they add entries of their own.
    fix_cmds = [_fix_command(p) for i, (p, explicit) in enumerate(reversed(commands)) if i == 0 or explicit]
    unsafe.reverse()  # drive root first
    fix = " && ".join(fix_cmds)
    if unsafe and os.path.dirname(unsafe[0]) == unsafe[0]:
        fix = ((fix + "  Also: ") if fix else "") + (
            f"the drive root {unsafe[0]} lets other accounts delete, rename or re-permission folders in it; "
            "ask IT to restore its default permissions.")
    return {"ok": not unsafe, "unsafe": unsafe, "fix": fix, "accounts": details}


_folder_cache = {"at": 0.0, "value": None}
_folder_lock = threading.Lock()


def folder_security(max_age: float = 300) -> dict:
    """check_ancestors() for the app folder, cached for max_age seconds."""
    with _folder_lock:
        if _folder_cache["value"] is None or time.monotonic() - _folder_cache["at"] > max_age:
            _folder_cache["value"] = check_ancestors(config.ROOT)
            _folder_cache["at"] = time.monotonic()
        return _folder_cache["value"]


def folder_alert():
    """Once per launch (app start-up): a high-priority alert if a folder above the app is unsafe."""
    status = folder_security()
    if status["ok"]:
        return
    from . import alerts
    who = "; ".join(f"{p}: {', '.join(a)}" for p, a in status["accounts"].items())
    alerts.raise_alert(
        "Security", "Other accounts can change the folders above Command Center",
        f"{who}. Anyone who can rename or replace these folders can swap in code that runs as you. "
        f"Fix it from a Command Prompt (as you; you need to own the folder or have full control): {status['fix']}", "high",
        toast="folder permissions need fixing")


# ---- Windows Hello, with the Windows password as the fallback ----------------------------------

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text):
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


IID_IUserConsentVerifierInterop = _GUID.parse("39E050C3-4E74-441A-8DC0-B81104DF949C")
# IAsyncOperation<UserConsentVerificationResult>. A WinRT generic IID is uuid5 of its signature
# (= fd596ffd-2318-558f-9dbe-d21df43764a5).
IID_IAsyncOperation_UCVResult = _GUID.parse(str(uuid.uuid5(
    uuid.UUID("11f47ad5-7b73-42c0-abae-878b1e16adee"),
    "pinterface({9fc2b0bb-e446-44e2-aa61-9cab8f636af2};"
    "enum(Windows.Security.Credentials.UI.UserConsentVerificationResult;i4))")))
IID_IAsyncInfo = _GUID.parse("00000036-0000-0000-C000-000000000046")

HELLO_RESULTS = {0: "Verified", 1: "DeviceNotPresent", 2: "NotConfiguredForUser", 3: "DisabledByPolicy",
                 4: "DeviceBusy", 5: "RetriesExhausted", 6: "Canceled"}
HELLO_UNAVAILABLE = {1, 2, 3}


def _vmethod(obj, index, restype, *argtypes):
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[index])


def _release(obj):
    if obj and obj.value:
        _vmethod(obj, 2, ctypes.c_ulong)(obj)


def hello_verify(hwnd: int, message: str, cancel: threading.Event | None = None) -> int:
    """Shows the Windows Hello prompt, owned by hwnd, through IUserConsentVerifierInterop (the
    desktop-app form of UserConsentVerifier.RequestVerificationAsync, which needs a parent window
    to come to the front). Returns a HELLO_RESULTS key."""
    combase = ctypes.WinDLL("combase")
    combase.RoInitialize.argtypes = [ctypes.c_int]
    combase.WindowsCreateString.argtypes = [wt.LPCWSTR, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
    combase.WindowsDeleteString.argtypes = [ctypes.c_void_p]
    combase.RoGetActivationFactory.argtypes = [ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
    combase.RoGetActivationFactory.restype = ctypes.HRESULT
    combase.RoInitialize(1)  # multithreaded; "already initialized" results are fine

    def hstring(text):
        h = ctypes.c_void_p()
        combase.WindowsCreateString(text, len(text), ctypes.byref(h))
        return h

    class_name, prompt = hstring("Windows.Security.Credentials.UI.UserConsentVerifier"), hstring(message)
    factory, op, info = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    try:
        combase.RoGetActivationFactory(class_name, ctypes.byref(IID_IUserConsentVerifierInterop), ctypes.byref(factory))
        # slots 0-5 are IUnknown + IInspectable; RequestVerificationForWindowAsync is next
        _vmethod(factory, 6, ctypes.HRESULT, wt.HWND, ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))(
            factory, hwnd, prompt, ctypes.byref(IID_IAsyncOperation_UCVResult), ctypes.byref(op))
        _vmethod(op, 0, ctypes.HRESULT, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))(
            op, ctypes.byref(IID_IAsyncInfo), ctypes.byref(info))  # QueryInterface
        get_status = _vmethod(info, 7, ctypes.HRESULT, ctypes.POINTER(ctypes.c_int))
        status = ctypes.c_int(0)
        while True:
            get_status(info, ctypes.byref(status))
            if status.value != 0:  # 0 = Started
                break
            if cancel is not None and cancel.is_set():
                _vmethod(info, 9, ctypes.HRESULT)(info)  # IAsyncInfo.Cancel
            time.sleep(0.1)
        if status.value == 1:  # Completed
            result = ctypes.c_int()
            _vmethod(op, 8, ctypes.HRESULT, ctypes.POINTER(ctypes.c_int))(op, ctypes.byref(result))  # GetResults
            return result.value
        if status.value == 2:  # Canceled
            return 6
        error = ctypes.c_int()
        _vmethod(info, 8, ctypes.HRESULT, ctypes.POINTER(ctypes.c_int))(info, ctypes.byref(error))
        raise OSError(f"Windows Hello failed (0x{error.value & 0xFFFFFFFF:08X})")
    finally:
        for obj in (info, op, factory):
            _release(obj)
        combase.WindowsDeleteString(class_name)
        combase.WindowsDeleteString(prompt)


class _CREDUI_INFOW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hwndParent", wt.HWND), ("pszMessageText", wt.LPCWSTR),
                ("pszCaptionText", wt.LPCWSTR), ("hbmBanner", wt.HANDLE)]


def _current_user_domain() -> str:
    secur32 = ctypes.WinDLL("secur32")
    secur32.GetUserNameExW.argtypes = [ctypes.c_int, wt.LPWSTR, ctypes.POINTER(wt.ULONG)]
    secur32.GetUserNameExW.restype = wt.BOOLEAN
    buf, size = ctypes.create_unicode_buffer(512), wt.ULONG(512)
    secur32.GetUserNameExW(2, buf, ctypes.byref(size))  # NameSamCompatible: DOMAIN\user
    return buf.value.rpartition("\\")[0]


def password_verify(hwnd: int, message: str) -> tuple[bool, str]:
    """Windows credential prompt limited to the signed-in account, checked with LogonUserW. The
    resulting token must have this process's SID, so only your own Windows password works."""
    credui, ole32 = ctypes.WinDLL("credui"), ctypes.WinDLL("ole32")
    credui.CredUIPromptForWindowsCredentialsW.argtypes = [
        ctypes.POINTER(_CREDUI_INFOW), wt.DWORD, ctypes.POINTER(wt.ULONG), ctypes.c_void_p, wt.ULONG,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wt.ULONG), ctypes.POINTER(wt.BOOL), wt.DWORD]
    credui.CredUIPromptForWindowsCredentialsW.restype = wt.DWORD
    credui.CredUnPackAuthenticationBufferW.argtypes = [
        wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD), wt.LPWSTR,
        ctypes.POINTER(wt.DWORD), wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    my_domain = _current_user_domain()
    info = _CREDUI_INFOW(ctypes.sizeof(_CREDUI_INFOW), hwnd, message, "Command Center", None)
    auth_error = 0
    for _ in range(3):
        package, out_buf, out_size, save = wt.ULONG(0), ctypes.c_void_p(), wt.ULONG(0), wt.BOOL(False)
        rc = credui.CredUIPromptForWindowsCredentialsW(ctypes.byref(info), auth_error, ctypes.byref(package), None, 0,
                                                       ctypes.byref(out_buf), ctypes.byref(out_size), ctypes.byref(save),
                                                       0x200)  # CREDUIWIN_ENUMERATE_CURRENT_USER
        if rc == 1223:  # ERROR_CANCELLED
            return False, "Cancelled"
        if rc != 0:
            return False, f"The Windows sign-in prompt failed (error {rc})"
        user, domain, pwd = (ctypes.create_unicode_buffer(514) for _ in range(3))
        n_user, n_domain, n_pwd = wt.DWORD(514), wt.DWORD(514), wt.DWORD(514)
        try:
            if not credui.CredUnPackAuthenticationBufferW(0, out_buf, out_size, user, ctypes.byref(n_user), domain,
                                                          ctypes.byref(n_domain), pwd, ctypes.byref(n_pwd)):
                return False, "Use your Windows password (a PIN can't be checked here)"
            name, dom = user.value, domain.value
            if not dom and "\\" in name:
                dom, _, name = name.rpartition("\\")
            if not dom and "@" not in name:
                dom = my_domain
            try:
                # interactive logon type: also works from cached credentials when the VPN is down
                token = win32security.LogonUser(name, dom or None, pwd.value, win32security.LOGON32_LOGON_INTERACTIVE,
                                                win32security.LOGON32_PROVIDER_DEFAULT)
            except pywintypes.error as e:
                auth_error = e.winerror or 1326  # e.g. ERROR_LOGON_FAILURE: the next prompt says so
                continue
            try:
                same = win32security.GetTokenInformation(token, win32security.TokenUser)[0] == MY_SID
            finally:
                token.Close()
            if same:
                return True, "Verified with your Windows password"
            return False, "That isn't the signed-in Windows account"
        finally:
            ctypes.memset(pwd, 0, ctypes.sizeof(pwd))
            if out_buf.value:
                ctypes.memset(out_buf, 0, out_size.value)
                ole32.CoTaskMemFree(out_buf)
    return False, "Wrong password"


def default_verifier(hwnd: int, message: str, cancel: threading.Event | None = None) -> tuple[bool, str]:
    try:
        result = hello_verify(hwnd, message, cancel)
    except Exception as e:
        log.warning("Windows Hello unavailable (%s); asking for the Windows password", e)
        result = 1
    log.info("Windows Hello result: %s", HELLO_RESULTS.get(result, result))
    if result == 0:
        return True, "Verified with Windows Hello"
    if result in HELLO_UNAVAILABLE:
        return password_verify(hwnd, message)
    return False, {6: "Cancelled", 4: "Windows Hello is busy. Try again.",
                   5: "Too many attempts. Try again."}.get(result, f"Not verified ({result})")


_verifier = default_verifier


def set_verifier(fn):
    """Replaces the identity check (for tests): fn(hwnd, message, cancel) -> (ok, detail)."""
    global _verifier
    _verifier = fn


def verify_user(hwnd: int, message: str = "Unlock Command Center", cancel: threading.Event | None = None) -> tuple[bool, str]:
    try:
        return _verifier(hwnd, message, cancel)
    except Exception as e:
        log.exception("Identity check failed")
        return False, f"Couldn't verify you: {e}"
