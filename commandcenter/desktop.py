"""Desktop shell: runs the server in the background and shows the dashboard in a native window
(Edge WebView2 through pywebview), with a tray icon. Closing the window hides it to the tray so
alerts keep running; Quit in the tray menu exits.

Unlock: the window starts on a local lock screen, not the dashboard. The first time it is shown
(at launch, or from the tray / a toast when it started minimized at sign-in) Windows Hello asks
who you are (Windows password if Hello isn't set up). Only then does the window open the one-time
/auth link that gives it the session cookie. There is no idle re-lock (security.unlock_on_start);
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

log = logging.getLogger(__name__)
APP_ID = "Sonance.CommandCenter"
TITLE = "Command Center"
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
href="__ACTION__hide">Hide to tray</a><a class="quiet" href="__ACTION__quit">Quit</a>
</div><script>function setStatus(t){document.getElementById('status').textContent=t}</script></body></html>"""


def _lock_page(status: str) -> str:
    return LOCK_HTML.replace("__STATUS__", html.escape(status)).replace("__ACTION__", ACTION_URL)


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
_nav = {"origin": None, "action": None}


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
        return
    args.Cancel = True
    if uri.startswith(ACTION_URL):
        if _nav["action"]:
            threading.Thread(target=_nav["action"], args=(uri[len(ACTION_URL):],), daemon=True).start()
    elif uri.lower().startswith(("https://", "http://")):
        webbrowser.open(uri)
    else:
        log.warning("Blocked navigation to %s", uri[:80])


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
        self._unlocking = False
        self._unlock_lock = threading.Lock()
        self._cancel = threading.Event()
        self.quitting = False
        self.window = None
        self.tray = None
        self.server = None
        self._server_thread = None

    # ---- window (security.shell uses these for the /api/window/* routes) ----
    def show(self):
        if self.window:
            self.window.show()
            hwnd = self._hwnd()
            if ctypes.windll.user32.IsIconic(hwnd):  # un-minimize to its previous state (full screen stays full screen)
                ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            self.window.on_top = True  # nudge it in front of other windows
            self.window.on_top = False
            self._unlock()

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
        return {"desktop": True, "fullscreen": self.fullscreen, "locked": not self.unlocked}

    def lock(self):
        """Lock from the dashboard (the session secret was already replaced by security.end_session)."""
        self.unlocked = False
        self.locked_by_user = True
        if self.window:
            self.window.load_html(_lock_page("Locked. Select Unlock to confirm it's you."))

    def restart(self):
        """Starts a fresh copy (which waits for this one to exit, so it doesn't just hand off to it), then quits."""
        import subprocess
        import sys
        args = [sys.executable, "-m", "commandcenter", "app", "--after-pid", str(os.getpid())]
        subprocess.Popen(args, cwd=str(config.ROOT), close_fds=True,
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        log.info("Restarting")
        self.quit()

    def _hwnd(self) -> int:
        try:
            return int(self.window.native.Handle.ToInt64())
        except Exception:
            return _find_own_window(TITLE)

    def _on_closing(self):
        if self.quitting:
            return True
        self.window.hide()
        return False  # cancel the close; keep running in the tray

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
    def _lock_status(self, text):
        def update():
            try:
                self.window.evaluate_js(f"setStatus({json.dumps(text)})")
            except Exception:
                pass
        threading.Thread(target=update, daemon=True).start()

    def _on_action(self, action):
        actions = {"unlock": self._unlock, "quit": self.quit, "hide": self.hide, "minimize": self.minimize}
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
                self.window.load_url(security.bootstrap_url())
            else:
                log.info("Still locked: %s", detail)
                self._lock_status(f"{detail}. Command Center stays locked.")
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

    def quit(self):
        self.quitting = True
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
        security.harden_folders()
        if not self._start_server():
            return
        security.write_instance(self.port)
        try:
            _harden_webview()
            _nav["origin"], _nav["action"] = self.url, self._on_action
            geo = db.kv_get("window:geometry") or {}
            # Frameless (Command Center draws its own controls; its header is the drag region) and full
            # screen by default. The size and position below are the windowed state it returns to.
            self.window = webview.create_window(
                TITLE, html=_lock_page("Starting…" if self.require_unlock else "Opening…"),
                width=geo.get("width", 1600), height=geo.get("height", 1000), x=geo.get("x"), y=geo.get("y"),
                min_size=(760, 560), background_color="#05070d", hidden=self.start_hidden,
                frameless=True, easy_drag=False, fullscreen=self.fullscreen, text_select=True,
            )
            self.window.events.closing += self._on_closing
            self.window.events.resized += self._remember_geometry
            self.window.events.moved += self._remember_geometry
            self.window.events.shown += self._on_shown

            self.tray = self._build_tray()
            self.tray.run_detached()
            webview.start(debug=False, private_mode=False, storage_path=str(config.DATA_DIR / "webview"))
            # webview.start returns once the window is destroyed (Quit / Restart)
        finally:
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
        made.append(f"{name}: {folder / (TITLE + '.lnk')}")
    return made


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
