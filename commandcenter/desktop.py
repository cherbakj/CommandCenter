"""Desktop shell: runs the server in the background and shows the dashboard in a native window
(Edge WebView2 through pywebview), with a tray icon. The window is open, minimized or closed for good:
closing it (Alt+F4) minimizes it so alerts keep running; Close in the power menu or Quit in the tray exits.
"Force quit Command Center" in the taskbar button's right-click menu ends a stuck copy from outside (forcequit.py).

Unlock: the window starts on a local start-up page, not the dashboard: the launch animation's puzzle
assembling (static/splash.js in gate mode, no API calls). The first time it is shown (at launch, or from
the tray / a toast when it started minimized at sign-in) Windows Hello asks who you are (Windows password
if Hello isn't set up) in front of it; Unlock / Minimize / Quit appear only if that fails. Only then
does the window open the one-time /auth link that gives it the session cookie, and the dashboard's splash
carries on with the sync checks. Lock shows the padlock page (LOCK_HTML) instead. There is no idle re-lock (security.unlock_on_start);
Lock in the dashboard (POST /api/session/lock) replaces the session secret and asks again.

Window: frameless, full screen by default (ui.start_fullscreen). Command Center draws its own window
controls through the /api/window/* routes in security.py; elements with the CSS class
pywebview-drag-region drag the window when it isn't full screen.
"""
import ctypes
import html
import json
import logging
import os
import threading
import time
import webbrowser
from pathlib import Path

import pystray
import uvicorn
import webview
from PIL import Image, ImageDraw

from . import alerts, config, db, security
from .siteview import PulseView, TeamsView, _no_autofill
from .codedock import CodeDock
from .ssmsdock import SsmsDock

log = logging.getLogger(__name__)
APP_ID = "Sonance.CommandCenter"
TITLE = "Command Center"
QUIT_DEADLINE = 45  # s from Quit / Restart to the process ending, whatever hangs (Restart's new copy waits 60 s)


def exit_process(code: int):
    """Ends the process for sure once the app has closed (or failed to). The atexit hooks (the docked VS Code and SSMS
    safety nets, Claude terminals) get 10 s, then os._exit: a normal interpreter exit waits for every worker thread,
    and one stuck in an Outlook COM call kept a closed Command Center running with no window, so the next launch
    handed off to that copy instead of starting."""
    import atexit
    hooks = threading.Thread(target=atexit._run_exitfuncs, name="exit-hooks", daemon=True)
    hooks.start()
    hooks.join(10)
    log.info("Process ended (code %d)", code)
    logging.shutdown()
    os._exit(code)
ICON_PATH = Path(__file__).parent / "static" / "icon.ico"
# Lock-screen buttons navigate here; the navigation guard cancels the navigation and acts on it.
# (No pywebview js_api: its dispatcher lets page JS reach any attribute path on the exposed object.)
ACTION_URL = "https://commandcenter.invalid/"

LOCK_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Command Center</title><style>
html,body{margin:0;height:100%;background:#05070d;color:#cfe3ea;font:16px "Segoe UI",sans-serif}
body{display:grid;place-items:center}.drag{position:fixed;top:0;left:0;right:0;height:2.5rem}
.box{text-align:center;max-width:32rem;padding:2rem}
.logo{font:700 1.6rem Consolas,monospace;letter-spacing:.2em;color:#39ff14}.logo b{color:#00e5ff}
.lock{font-size:3rem;margin:1.2rem 0 .4rem}#status{min-height:3em;color:#9fb3bb;margin:.8rem 0 1.6rem}
a{display:inline-block;margin:.3rem;padding:.6rem 1.2rem;border:1px solid #39ff14;color:#39ff14;
text-decoration:none;border-radius:4px;font-weight:600}a.quiet{border-color:#334;color:#9fb3bb}
a:hover{background:#39ff1422}</style></head><body><div class="drag pywebview-drag-region"></div><div class="box">
<div class="logo">COMMAND<b>//</b>CENTER</div><div class="lock">&#128274;</div>
<div id="status">__STATUS__</div>
<a href="__ACTION__unlock">Unlock</a><a class="quiet" href="__ACTION__minimize">Minimize</a><a class="quiet"
href="__ACTION__quit">Quit</a>
</div><script>function setStatus(t){document.getElementById('status').textContent=t}</script></body></html>"""


def _lock_page(status: str) -> str:
    return LOCK_HTML.replace("__STATUS__", html.escape(status)).replace("__ACTION__", ACTION_URL)


# The start-up page: the launch animation's puzzle (splash.js / splash.css inlined, gate mode) with the status
# under it. Theme tokens are style.css's defaults (the Profile's colors live in the dashboard).
GATE_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Command Center</title><style>
:root{--bg:#04070a;--accent:#39ff14;--accent-rgb:57 255 20;--accent-2:#00e5ff;--accent-2-rgb:0 229 255;
--amber:#ffc21a;--amber-rgb:255 194 26;--text:#eef4f2;--muted:#b6c4c1;
--font-display:"Segoe UI",sans-serif;--font-ui:"Segoe UI",sans-serif}
html,body{margin:0;height:100%;overflow:hidden;background:#04070a;color:#eef4f2;font:16px "Segoe UI",sans-serif}
.drag{position:fixed;top:0;left:0;right:0;height:2.5rem;z-index:200}
.gate-foot{position:fixed;left:0;right:0;bottom:2rem;z-index:150;text-align:center;padding:0 1rem}
#status{min-height:1.5em;color:#b6c4c1;margin-bottom:.7rem}#actions[hidden]{display:none}
a{display:inline-block;margin:.3rem;padding:.6rem 1.2rem;border:1px solid #39ff14;color:#39ff14;
text-decoration:none;border-radius:4px;font-weight:600}a.quiet{border-color:#334;color:#9fb3bb}
a:hover{background:#39ff1422}
__SPLASH_CSS__</style></head><body><div class="drag pywebview-drag-region"></div>
<div class="gate-foot"><div id="status" role="status">__STATUS__</div><div id="actions" hidden>
<a href="__ACTION__unlock">Unlock</a><a class="quiet" href="__ACTION__minimize">Minimize</a><a class="quiet"
href="__ACTION__quit">Quit</a></div></div>
<script>window.CC_GATE=true;function setStatus(t,failed){document.getElementById('status').textContent=t;
document.getElementById('actions').hidden=!failed}</script><script>__SPLASH_JS__</script></body></html>"""


def _gate_page(status: str) -> str:
    static = Path(__file__).parent / "static"
    css, js = ((static / name).read_text(encoding="utf-8") for name in ("splash.css", "splash.js"))
    if "</" in css or "</script" in js.lower():
        return _lock_page(status)  # the inlined files must never close their own tags
    page = GATE_HTML.replace("__STATUS__", html.escape(status)).replace("__ACTION__", ACTION_URL)
    return page.replace("__SPLASH_CSS__", css).replace("__SPLASH_JS__", js)


JC_J = [(4, 4), (15, 4), (15, 8), (12.5, 8), (12.5, 23), (8, 28), (2.5, 28), (2.5, 24), (6.5, 24), (8.5, 22), (8.5, 8), (4, 8)]
JC_C = [(29.5, 4), (20.5, 4), (16.5, 8), (16.5, 24), (20.5, 28), (29.5, 28), (29.5, 24), (22.5, 24), (20.5, 22), (20.5, 10), (22.5, 8), (29.5, 8)]


def icon_image(size=256) -> Image.Image:
    """The JC monogram (same geometry as static/jc-logo.svg): green J, cyan C, on near-black."""
    big = size * 4  # draw large and shrink, for smooth edges at tray size
    img = Image.new("RGBA", (big, big), (5, 7, 13, 255))
    d = ImageDraw.Draw(img)
    s = big / 32
    d.polygon([(x * s, y * s) for x, y in JC_J], fill=(57, 255, 20, 255))
    d.polygon([(x * s, y * s) for x, y in JC_C], fill=(0, 229, 255, 255))
    return img.resize((size, size), Image.LANCZOS)


def ensure_icon_file() -> Path:
    if not ICON_PATH.exists():
        icon_image().save(ICON_PATH, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return ICON_PATH


def _message_box(text, error=True):
    ctypes.windll.user32.MessageBoxW(0, text, TITLE, 0x10 if error else 0x40)


def _find_own_window(title) -> int:
    import win32gui
    import win32process
    found = []

    def visit(hwnd, _):
        if win32process.GetWindowThreadProcessId(hwnd)[1] == os.getpid() and win32gui.GetWindowText(hwnd) == title:
            found.append(hwnd)
        return True

    win32gui.EnumWindows(visit, None)
    return found[0] if found else 0


def _set_window_icon(hwnd):
    try:
        import win32con
        import win32gui
        if not hwnd:
            return
        for kind, px in ((win32con.ICON_BIG, 256), (win32con.ICON_SMALL, 32)):
            hicon = win32gui.LoadImage(0, str(ICON_PATH), win32con.IMAGE_ICON, px, px, win32con.LR_LOADFROMFILE)
            win32gui.SendMessage(hwnd, win32con.WM_SETICON, kind, hicon)
    except Exception as e:
        log.warning("Couldn't set window icon: %s", e)


# ---- WebView2 hardening ---------------------------------------------------------------
_nav = {"origin": None, "action": None, "leaving": None}


def _harden_webview():
    """No devtools, no remote debugging, no file:// access; the window can only show this app.
    Other web pages open in the default browser; anything else is refused."""
    for name in ("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER",
                 "WEBVIEW2_USER_DATA_FOLDER", "WEBVIEW2_RELEASE_CHANNEL_PREFERENCE"):
        if os.environ.pop(name, None) is not None:
            log.warning("Ignored %s from the environment", name)
    webview.settings["ALLOW_FILE_URLS"] = False
    webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = False
    webview.settings["REMOTE_DEBUGGING_PORT"] = None
    webview.settings["ALLOW_DOWNLOADS"] = False
    webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = True  # only the drag-class element itself drags, not its buttons
    try:
        from webview.platforms import edgechromium
    except Exception as e:
        log.warning("WebView2 navigation guard not installed: %s", e)
        return
    original_ready = edgechromium.EdgeChrome.on_webview_ready

    def on_webview_ready(self, sender, args):
        if args.IsSuccess:  # before pywebview's handler loads the first page
            _no_autofill(sender.CoreWebView2.Settings, sender.CoreWebView2)
        original_ready(self, sender, args)
        if args.IsSuccess:
            sender.CoreWebView2.NavigationStarting += _on_navigation

    def on_new_window_request(self, sender, args):
        args.set_Handled(True)  # never open a second WebView2 window
        uri = str(args.get_Uri())
        if uri.lower().startswith(("https://", "http://")):
            webbrowser.open(uri)

    edgechromium.EdgeChrome.on_webview_ready = on_webview_ready
    edgechromium.EdgeChrome.on_new_window_request = on_new_window_request


def _on_navigation(sender, args):
    uri = str(args.Uri)
    if uri.startswith(_nav["origin"] or "\0") or uri.startswith(("about:", "data:text/html")):
        if _nav.get("leaving"):
            _nav["leaving"]()  # the dashboard page is going away (reload, lock page): the native views hide
        return
    args.Cancel = True
    if uri.startswith(ACTION_URL):
        if _nav["action"]:
            threading.Thread(target=_nav["action"], args=(uri[len(ACTION_URL):],), daemon=True).start()
    elif uri.lower().startswith(("https://", "http://")):
        webbrowser.open(uri)
    else:
        log.warning("Blocked navigation to %s", uri[:80])


# ---- global quick-capture hotkey -------------------------------------------------------------
# RegisterHotKey on a dedicated thread with its own message loop: WM_HOTKEY lands in that thread's
# queue (no window needed), and UnregisterHotKey must run on the same thread, so it does, on exit.
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
ERROR_HOTKEY_ALREADY_REGISTERED = 1409
DEFAULT_CAPTURE_HOTKEY = "Ctrl+Alt+Space"
_HOTKEY_MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT,
                "win": MOD_WIN, "windows": MOD_WIN}
_HOTKEY_KEYS = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "backspace": 0x08, "insert": 0x2D,
    "delete": 0x2E, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "up": 0x26, "down": 0x28,
    "left": 0x25, "right": 0x27, "pause": 0x13, ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE,
    "/": 0xBF, "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
}
_MOD_NAMES = ((MOD_CONTROL, "Ctrl"), (MOD_ALT, "Alt"), (MOD_SHIFT, "Shift"), (MOD_WIN, "Win"))


def parse_hotkey(text: str) -> tuple[int, int, str]:
    """'Ctrl+Alt+Space' -> (modifiers, virtual key, canonical name). At least one of Ctrl/Alt/Win is required
    (a bare key or Shift+key would swallow normal typing in every app). ValueError if it isn't a hotkey."""
    parts = [p.strip() for p in str(text or "").split("+")]
    if len(parts) < 2 or not all(parts):
        raise ValueError("use modifiers and a key, e.g. Ctrl+Alt+Space")
    mods = 0
    for p in parts[:-1]:
        bit = _HOTKEY_MODS.get(p.lower())
        if not bit or mods & bit:
            raise ValueError(f"unknown or repeated modifier {p[:12]!r}")
        mods |= bit
    key = parts[-1]
    low = key.lower()
    if len(key) == 1 and key.isascii() and key.isalnum():
        vk, label = ord(key.upper()), key.upper()
    elif low in _HOTKEY_KEYS:
        vk, label = _HOTKEY_KEYS[low], key if len(key) == 1 else low.capitalize()
    elif low[:1] == "f" and low[1:].isdigit() and 1 <= int(low[1:]) <= 24:
        vk, label = 0x6F + int(low[1:]), key.upper()
    else:
        raise ValueError(f"unknown key {key[:12]!r}")
    if not mods & (MOD_CONTROL | MOD_ALT | MOD_WIN):
        raise ValueError("include Ctrl, Alt or Win")
    name = "+".join([n for bit, n in _MOD_NAMES if mods & bit] + [label])
    return mods, vk, name


class GlobalHotkey:
    """One system-wide hotkey. start() registers it (False + .error if another app already has it);
    on_press runs on a short-lived thread so the message loop never blocks; stop() unregisters it."""

    def __init__(self, text: str, on_press, hotkey_id: int = 0xCC01):
        self.mods, self.vk, self.name = parse_hotkey(text)
        self.on_press = on_press
        self.id = hotkey_id
        self.error = ""
        self.error_code = 0
        self.registered = False
        self._tid = 0
        self._thread = None
        self._ready = threading.Event()
        self._busy = threading.Lock()

    def start(self, timeout: float = 5) -> bool:
        self._thread = threading.Thread(target=self._loop, name="capture-hotkey", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            self.error = "the hotkey thread didn't start"
        return self.registered

    def _loop(self):
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
        user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
        user32.PeekMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT)
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # creates this thread's message queue
        self._tid = ctypes.windll.kernel32.GetCurrentThreadId()
        try:
            if not user32.RegisterHotKey(None, self.id, self.mods | MOD_NOREPEAT, self.vk):
                self.error_code = ctypes.get_last_error()
                self.error = ("already used by another app" if self.error_code == ERROR_HOTKEY_ALREADY_REGISTERED
                              else f"Windows error {self.error_code}")
                return
            self.registered = True
        finally:
            self._ready.set()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:  # 0 = WM_QUIT, -1 = error
                if msg.message == WM_HOTKEY and msg.wParam == self.id:
                    self._fire()
        finally:
            user32.UnregisterHotKey(None, self.id)
            self.registered = False

    def _fire(self):
        if not self._busy.acquire(blocking=False):
            return  # the previous press is still being handled

        def run():
            try:
                self.on_press()
            except Exception as e:
                log.warning("Quick-capture hotkey handler failed: %s", type(e).__name__)
            finally:
                self._busy.release()
        threading.Thread(target=run, name="capture-hotkey-press", daemon=True).start()

    def stop(self, timeout: float = 3):
        if self._thread and self._thread.is_alive() and self._tid:
            ctypes.windll.user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            self._thread.join(timeout)


class DesktopApp:
    def __init__(self, start_hidden=False, after_pid=None):
        ui = config.section("ui", {})
        self.port = int(config.section("server", {}).get("port", 8765))
        self.url = f"http://127.0.0.1:{self.port}/"
        self.start_hidden = start_hidden or ui.get("start_minimized", False)
        self.fullscreen = ui.get("start_fullscreen", True) is not False
        self.after_pid = after_pid  # set by Restart: wait for the old copy to exit first
        self.require_unlock = security.settings().get("unlock_on_start", True)
        self.unlocked = False
        self.locked_by_user = False  # Lock in the dashboard: always ask again, whatever unlock_on_start says
        # True while the window shows the local lock page. Its buttons (ACTION_URL links) are honored only then:
        # any page could navigate to https://commandcenter.invalid/quit, but only the lock page should.
        self.lock_showing = True  # the window is created on the lock page
        self.gate_showing = True  # ...the start-up one (the splash's puzzle), not the padlock page Lock shows
        self._unlocking = False
        self._unlock_lock = threading.Lock()
        self._cancel = threading.Event()
        self.quitting = False
        self.restarting = False  # Restart (not Quit): docked VS Code windows are docked again by the new copy
        self.window = None
        self.tray = None
        self.server = None
        self._server_thread = None
        self.hotkey = None
        # Native windows over dashboard tabs (overlay.py; security.py's /api/window/<view>/* routes)
        self.pulse = PulseView(self)  # the Pulse tab's site view
        self.teams = TeamsView(self)  # Teams on the web in the Teams tab
        self.ssms = SsmsDock(self)    # the SQL tab's docked SSMS
        self.ssms.hotkey_factory = GlobalHotkey
        self.code = CodeDock(self)    # desktop VS Code docked in the VS Code tab
        self.overlays = {"pulse": self.pulse, "teams": self.teams, "ssms": self.ssms, "code": self.code}

    # ---- window (security.shell uses these for the /api/window/* routes) ----
    def show(self):
        if self.window:
            self.window.show()
            hwnd = self._hwnd()
            if ctypes.windll.user32.IsIconic(hwnd):  # un-minimize to its previous state (full screen stays full screen)
                ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            # Nudge it in front of other windows with plain Win32 calls, not window.on_top: pythonnet keeps the GIL
            # through a .NET property set, and TopMost's SetWindowPos waits on the window's thread, which may be
            # waiting for the GIL itself. That deadlock froze CC when a second launch or the tray showed it.
            # ctypes lets go of the GIL during the call.
            set_pos = ctypes.windll.user32.SetWindowPos
            flags = 0x0001 | 0x0002  # SWP_NOSIZE | SWP_NOMOVE
            set_pos(ctypes.c_void_p(hwnd), ctypes.c_void_p(-1), 0, 0, 0, 0, flags)  # HWND_TOPMOST
            set_pos(ctypes.c_void_p(hwnd), ctypes.c_void_p(-2), 0, 0, 0, 0, flags)  # HWND_NOTOPMOST
            self._unlock()

    FRONT_POLLS = ("reminderPoll", "hoxhuntPoll")  # the page's pop-up pollers a front request may run

    def raise_for_reminder(self, poll: str = "reminderPoll") -> str:
        """A personal reminder is due (reminders._notify) or a Hoxhunt test arrived (hoxhunt._arrived, poll
        "hoxhuntPoll"): restore the window and bring it in front so the pop-up shows in the middle of it. Not while
        locked (that would start Windows Hello by itself) or while the screen may be on show to others
        (reminders.share_risk); then the toast alone, and the pop-up waits in the window."""
        if poll not in self.FRONT_POLLS:
            raise ValueError("unknown pop-up")
        if not self.window or self.quitting or not self.unlocked or self.lock_showing:
            return "kept back (locked)"
        user32 = ctypes.windll.user32
        hwnd = self._hwnd()
        in_front = user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd) \
            and user32.GetAncestor(user32.GetForegroundWindow(), 2) & 0xFFFFFFFF == hwnd & 0xFFFFFFFF  # GA_ROOT: docked ones count
        if not in_front:
            from . import reminders
            risk = reminders.share_risk()
            if risk:
                return f"kept back ({risk})"
            self.show()
        self.window.run_js(f"window.CC && CC.{poll} && CC.{poll}()")  # the pop-up now, not at the next poll
        return "already in front" if in_front else "brought forward"

    def minimize(self):
        if self.window:
            self.window.minimize()

    def hide(self):
        if self.window:
            self.window.hide()

    def toggle_fullscreen(self) -> bool:
        if not self.window:
            return self.fullscreen
        if self.fullscreen:
            # pywebview restores the border style it saw when full screen began; at start-up that was the
            # titled style (frameless is applied after), so make sure we come back frameless.
            try:
                from System.Windows.Forms import FormBorderStyle
                self.window.native.old_style = getattr(FormBorderStyle, "None")
            except Exception as e:
                log.warning("Couldn't keep the window frameless: %s", e)
        self.window.toggle_fullscreen()
        self.fullscreen = not self.fullscreen
        return self.fullscreen

    def window_state(self) -> dict:
        return {"desktop": True, "fullscreen": self.fullscreen, "locked": not self.unlocked, "pulse_view": True,
                "teams_view": True, "ssms_dock": True, "code_dock": True}

    def lock(self):
        """Lock from the dashboard (the session secret was already replaced by security.end_session)."""
        self.unlocked = False
        self.locked_by_user = True
        self.ssms.on_lock()  # SSMS goes back to a normal (minimized) window; docked again after unlock
        self.code.on_lock()  # VS Code windows too
        for view in self.overlays.values():
            view.forget()  # no site view or SSMS stays over the lock screen
        if self.window:
            self.lock_showing, self.gate_showing = True, False
            self.window.load_html(_lock_page("Locked. Select Unlock to confirm it's you."))

    def restart(self):
        """Starts a fresh copy (which waits for this one to exit, so it doesn't just hand off to it), then quits."""
        import subprocess
        import sys
        args = [sys.executable, "-m", "commandcenter", "app", "--after-pid", str(os.getpid())]
        subprocess.Popen(args, cwd=str(config.ROOT), close_fds=True,
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        log.info("Restarting")
        self.restarting = True
        self.quit()

    def _hwnd(self) -> int:
        try:
            return int(self.window.native.Handle.ToInt64())
        except Exception:
            return _find_own_window(TITLE)

    def _on_closing(self):
        if self.quitting:
            return True
        self.window.minimize()
        return False  # cancel the close: open, minimized or quit, never hidden (Joe's call 2026-10-27)

    def _remember_geometry(self, *_):
        w = self.window
        if not w or self.fullscreen or ctypes.windll.user32.IsIconic(self._hwnd()):
            return  # remember the windowed size and position only
        if w.width > 400 and w.height > 300:
            db.kv_set("window:geometry", {"width": w.width, "height": w.height, "x": w.x, "y": w.y})

    def _on_shown(self):
        _set_window_icon(self._hwnd())
        self._unlock()

    # ---- unlock ----
    def _lock_status(self, text, failed=False):
        """failed: the start-up page shows its Unlock / Minimize / Quit links (the padlock page always does)."""
        def update():
            try:
                self.window.evaluate_js(f"setStatus({json.dumps(text)}, {json.dumps(failed)})")
            except Exception:
                pass
        threading.Thread(target=update, daemon=True).start()

    def _on_action(self, action):
        if not self.lock_showing:
            log.warning("Ignored a lock-screen action (%s) while the lock screen isn't showing", str(action)[:20])
            return
        actions = {"unlock": self._unlock, "quit": self.quit, "minimize": self.minimize}
        actions.get(action, lambda: None)()

    def _unlock(self):
        with self._unlock_lock:
            if self.unlocked or self._unlocking or self.quitting:
                return
            self._unlocking = True
        threading.Thread(target=self._unlock_worker, name="unlock", daemon=True).start()

    def _unlock_worker(self):
        try:
            if self.require_unlock or self.locked_by_user:
                self._lock_status("Confirm it's you with Windows Hello…")
                time.sleep(0.4)  # let the window come to the front first; the prompt is owned by it
                hwnd = self._hwnd()
                try:
                    import win32gui
                    win32gui.SetForegroundWindow(hwnd)
                except Exception:
                    pass
                ok, detail = security.verify_user(hwnd, "Unlock Command Center", self._cancel)
            else:
                ok, detail = True, "unlock_on_start is off"
            if ok and not self.quitting:
                log.info("Unlocked (%s)", detail)
                self.unlocked, self.locked_by_user = True, False
                self.lock_showing = False  # from here on the window shows the dashboard, not the lock page
                # From the start-up page the dashboard's splash starts assembled (no second fly-in).
                self.window.load_url(security.bootstrap_url(signed_in=self.gate_showing))
            else:
                log.info("Still locked: %s", detail)
                self._lock_status(f"{detail}. Command Center stays locked.", failed=True)
        finally:
            self._unlocking = False

    # ---- tray ----
    def _focus(self, minutes):
        return lambda icon, item: alerts.set_focus(minutes)

    def _build_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("Open Command Center", lambda icon, item: self.show(), default=True),
            pystray.MenuItem("Focus mode", pystray.Menu(
                pystray.MenuItem("30 minutes", self._focus(30)),
                pystray.MenuItem("1 hour", self._focus(60)),
                pystray.MenuItem("2 hours", self._focus(120)),
                pystray.MenuItem("Turn off", self._focus(0)),
            )),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda icon, item: self.quit()),
        )
        return pystray.Icon("CommandCenter", icon_image(64), TITLE, menu)

    # ---- quick capture (global hotkey) ----
    def _start_hotkey(self):
        raw = config.section("ui", {}).get("capture_hotkey", DEFAULT_CAPTURE_HOTKEY)
        if raw is None or raw is False or str(raw).strip().lower() in ("", "off", "none", "false"):
            log.info("Quick-capture hotkey is off (ui.capture_hotkey)")
            return
        try:
            hotkey = GlobalHotkey(str(raw), self._on_capture_hotkey)
        except ValueError as e:
            log.warning("ui.capture_hotkey isn't a usable hotkey: %s", e)
            self._hotkey_alert(str(raw)[:40], f"“{str(raw)[:40]}” isn't a hotkey Command Center understands ({e}).")
            return
        if hotkey.start():
            self.hotkey = hotkey
            log.info("Quick-capture hotkey %s registered", hotkey.name)
            db.kv_set("capture:hotkey_alerted", None)  # a later conflict alerts again
        else:
            log.warning("Quick-capture hotkey %s not registered: %s", hotkey.name, hotkey.error)
            self._hotkey_alert(hotkey.name, f"{hotkey.name} is {hotkey.error}, so quick capture can't use it.")

    @staticmethod
    def _hotkey_alert(name, problem):
        if db.kv_get("capture:hotkey_alerted") == name:
            return  # once per hotkey, not on every launch
        db.kv_set("capture:hotkey_alerted", name)
        alerts.raise_alert("System", "Quick-capture hotkey unavailable",
                           f"{problem} Set another one with ui.capture_hotkey in config.yaml (e.g. Ctrl+Alt+Shift+N), "
                           "or use Quick capture in the command palette (Ctrl+K).", "low",
                           toast="quick-capture hotkey unavailable")

    def _stop_hotkey(self):
        hotkey, self.hotkey = self.hotkey, None
        if hotkey:
            hotkey.stop()

    def _on_capture_hotkey(self):
        """Shows the window and opens the capture box. While locked it only brings up the lock screen
        (and Windows Hello): nothing from the dashboard runs or shows before unlock."""
        if not self.window or self.quitting:
            return
        user32 = ctypes.windll.user32
        hwnd = self._hwnd()
        restore = None if not hwnd else "hide" if not user32.IsWindowVisible(hwnd) else "minimize" if user32.IsIconic(hwnd) else None
        ready = self.unlocked and not self.lock_showing
        self.show()
        if not ready:
            return
        try:
            user32.SetForegroundWindow(hwnd)  # allowed: this process just received the hotkey input
        except Exception:
            pass
        if self.unlocked and not self.lock_showing and not self.quitting:  # still unlocked (no Lock in between)
            for view in self.overlays.values():
                view.release_focus()  # typing goes to the capture box, not a site view or SSMS
            # run_js, not evaluate_js: evaluate_js wraps the script in eval(), which the page's CSP (no 'unsafe-eval') blocks.
            self.window.run_js(f"window.CC && CC.quickCapture && CC.quickCapture({{restore: {json.dumps(restore)}}})")

    def privacy_key(self):
        """Ctrl+Shift+P pressed while the Pulse site or SSMS has the keyboard: the dashboard's own shortcut (privacy.js)."""
        if self.window and self.unlocked and not self.lock_showing and not self.quitting:
            self.window.run_js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'P', code: 'KeyP', "
                               "ctrlKey: true, shiftKey: true, bubbles: true}))")

    def quit(self):
        self.quitting = True
        # SSMS goes back to a normal window before this one is destroyed (never closed; minimized on Restart), recorded
        # so the next start docks it again
        self.ssms.release_for_exit("restart" if self.restarting else "quit", minimize=self.restarting)
        # Docked VS Code windows too (minimized on Restart, shown on Quit), recorded so the next start docks them again
        self.code.release_all("restart" if self.restarting else "quit", minimize=self.restarting, remember=True)
        # From here nothing of Joe's is inside the window, so the process may be ended outright if the orderly close
        # below gets stuck (e.g. the window's thread deadlocked): a copy left running with no window takes the next launch.
        watchdog = threading.Timer(QUIT_DEADLINE, lambda: (log.warning("Didn't close within %d s; ending the process",
                                                                       QUIT_DEADLINE), exit_process(0)))
        watchdog.daemon = True
        watchdog.start()
        self._stop_hotkey()
        self._cancel.set()  # closes a pending Windows Hello prompt
        if self.server:
            self.server.should_exit = True
        if self.tray:
            self.tray.stop()
        if self.window:
            self.window.destroy()

    # ---- run ----
    def _start_server(self) -> bool:
        try:
            sock = security.listen_socket(self.port)
        except OSError as e:
            _message_box(f"Command Center can't use port {self.port} on this PC ({e}).\n\n"
                         "Another program is using it. Change server.port in config.yaml or close that program.")
            return False
        from .app import app
        app.state.show_window = self.show
        alerts.open_hook = self.show
        from . import reminders
        reminders.front_hook = self.raise_for_reminder
        security.shell = self
        self.server = uvicorn.Server(uvicorn.Config(security.guard(app), host="127.0.0.1", port=self.port,
                                                    log_config=None, log_level="warning", access_log=False))
        self._server_thread = threading.Thread(target=self.server.run, kwargs={"sockets": [sock]}, name="server",
                                               daemon=True)
        self._server_thread.start()
        deadline = time.time() + 30
        while time.time() < deadline and self._server_thread.is_alive() and not self.server.started:
            time.sleep(0.1)
        if not self.server.started:
            raise RuntimeError("The Command Center server didn't start; see data/commandcenter.log")
        return True

    def run(self):
        security.configure("app")  # CC_DEV_NO_AUTH is ignored here
        if self.after_pid:
            _wait_for_exit(self.after_pid, 60)
        if not security.claim_instance(self.port):
            if not security.handoff_show(self.port):  # the running copy brings its window forward
                _message_box("Command Center is already running but didn't respond. Quit it from the tray icon and try again.")
            return
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        ensure_icon_file()
        from . import forcequit
        forcequit.register_jump_list(APP_ID, str(ICON_PATH))  # "Force quit Command Center" on the taskbar button
        security.harden_folders()
        if not self._start_server():
            return
        security.write_instance(self.port)
        try:
            _harden_webview()
            _nav["origin"], _nav["action"] = self.url, self._on_action
            _nav["leaving"] = lambda: [view.forget() for view in self.overlays.values()]
            geo = db.kv_get("window:geometry") or {}
            # Frameless (Command Center draws its own controls; its header is the drag region) and full
            # screen by default. The size and position below are the windowed state it returns to.
            self.window = webview.create_window(
                TITLE, html=_gate_page("Starting…" if self.require_unlock else "Opening…"),
                width=geo.get("width", 1600), height=geo.get("height", 1000), x=geo.get("x"), y=geo.get("y"),
                min_size=(760, 560), background_color="#05070d", hidden=self.start_hidden,
                frameless=True, easy_drag=False, fullscreen=self.fullscreen, text_select=True,
            )
            self.window.events.closing += self._on_closing
            self.window.events.resized += self._remember_geometry
            self.window.events.moved += self._remember_geometry
            self.window.events.shown += self._on_shown
            self.ssms.install_hooks()
            self.code.install_hooks()

            self.tray = self._build_tray()
            self.tray.run_detached()
            try:
                self._start_hotkey()
            except Exception as e:  # never keep the app from starting
                log.warning("Quick-capture hotkey not started: %s", type(e).__name__)
            webview.start(debug=False, private_mode=False, storage_path=str(config.DATA_DIR / "webview"))
            # webview.start returns once the window is destroyed (Quit / Restart)
        finally:
            self._stop_hotkey()
            security.shell = None
            security.remove_instance(self.port)
            if self.server:
                self.server.should_exit = True
            if self._server_thread:
                self._server_thread.join(20)  # let shutdown run (background jobs, Claude sessions) before exiting


def _wait_for_exit(pid: int, seconds: float):
    import win32api
    import win32event
    try:
        handle = win32api.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    except Exception:
        return  # already gone
    try:
        win32event.WaitForSingleObject(handle, int(seconds * 1000))
    finally:
        handle.Close()


def install_shortcuts(startup=True):
    """Start menu + desktop shortcuts, and optionally one in Startup so it launches at sign-in (to the tray)."""
    import win32com.client
    ensure_icon_file()
    shell = win32com.client.Dispatch("WScript.Shell")
    pythonw = config.ROOT / ".venv" / "Scripts" / "pythonw.exe"
    places = {
        "Start menu": (Path(shell.SpecialFolders("Programs")), "-m commandcenter app"),
        "Desktop": (Path(shell.SpecialFolders("Desktop")), "-m commandcenter app"),
    }
    if startup:
        places["Startup"] = (Path(shell.SpecialFolders("Startup")), "-m commandcenter app --minimized")
    made = []
    for name, (folder, args) in places.items():
        link = shell.CreateShortcut(str(folder / f"{TITLE}.lnk"))
        link.TargetPath = str(pythonw)
        link.Arguments = args
        link.WorkingDirectory = str(config.ROOT)
        link.IconLocation = str(ICON_PATH)
        link.Description = "Outlook, Teams, tasks and alerts in one place"
        link.Save()
        _set_app_id(link.FullName)
        made.append(f"{name}: {folder / (TITLE + '.lnk')}")
    return made


def _set_app_id(path: str):
    """Gives a shortcut the window's AppUserModelID, so a pinned shortcut and the running window share one taskbar
    button, with the Force quit task."""
    try:
        import pythoncom
        from win32comext.propsys import propsys, pscon
        from win32comext.shell import shell
        link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
        link.QueryInterface(pythoncom.IID_IPersistFile).Load(path, 2)  # STGM_READWRITE
        store = link.QueryInterface(propsys.IID_IPropertyStore)
        store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(APP_ID))
        store.Commit()
        link.QueryInterface(pythoncom.IID_IPersistFile).Save(path, True)
    except Exception as e:
        log.warning("Shortcut app id not set (%s)", type(e).__name__)


def uninstall_shortcuts():
    import win32com.client
    shell = win32com.client.Dispatch("WScript.Shell")
    removed = []
    for special in ("Programs", "Desktop", "Startup"):
        link = Path(shell.SpecialFolders(special)) / f"{TITLE}.lnk"
        if link.exists():
            link.unlink()
            removed.append(str(link))
    return removed
