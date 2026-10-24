"""Desktop shell: runs the server in the background and shows the dashboard in a native window
(Edge WebView2 through pywebview), with a tray icon. Closing the window hides it to the tray so
alerts keep running; Quit in the tray menu exits.
"""
import ctypes
import json
import logging
import threading
import time
import urllib.request
from pathlib import Path

import pystray
import uvicorn
import webview
from PIL import Image, ImageDraw

from . import alerts, config, db

log = logging.getLogger(__name__)
APP_ID = "Sonance.CommandCenter"
TITLE = "Command Center"
ICON_PATH = Path(__file__).parent / "static" / "icon.ico"


def icon_image(size=256) -> Image.Image:
    """The logo: two slanted bars, neon green and cyan, on near-black."""
    img = Image.new("RGBA", (size, size), (5, 7, 13, 255))
    d = ImageDraw.Draw(img)
    s = size / 32
    d.polygon([(4 * s, 27 * s), (14 * s, 5 * s), (19 * s, 5 * s), (9 * s, 27 * s)], fill=(57, 255, 20, 255))
    d.polygon([(14 * s, 27 * s), (24 * s, 5 * s), (29 * s, 5 * s), (19 * s, 27 * s)], fill=(0, 229, 255, 255))
    return img


def ensure_icon_file() -> Path:
    if not ICON_PATH.exists():
        icon_image().save(ICON_PATH, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return ICON_PATH


def _post(url, path):
    req = urllib.request.Request(url + path, data=b"{}", method="POST",
                                 headers={"X-CommandCenter": "1", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.load(r)


def _already_running(url) -> bool:
    try:
        _post(url, "api/window/show")
        return True
    except Exception:
        return False


def _wait_for_server(url, seconds=20):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url + "api/status", timeout=1).close()
            return True
        except Exception:
            time.sleep(0.25)
    return False


def _set_window_icon(title):
    try:
        import win32con
        import win32gui
        hwnd = win32gui.FindWindow(None, title)
        if not hwnd:
            return
        for kind, px in ((win32con.ICON_BIG, 256), (win32con.ICON_SMALL, 32)):
            hicon = win32gui.LoadImage(0, str(ICON_PATH), win32con.IMAGE_ICON, px, px, win32con.LR_LOADFROMFILE)
            win32gui.SendMessage(hwnd, win32con.WM_SETICON, kind, hicon)
    except Exception as e:
        log.warning("Couldn't set window icon: %s", e)


class DesktopApp:
    def __init__(self, start_hidden=False):
        self.port = int(config.section("server", {}).get("port", 8765))
        self.url = f"http://127.0.0.1:{self.port}/"
        self.start_hidden = start_hidden or config.section("ui", {}).get("start_minimized", False)
        self.quitting = False
        self.window = None
        self.tray = None
        self.server = None

    # ---- window ----
    def show(self):
        if self.window:
            self.window.show()
            self.window.restore()
            self.window.on_top = True  # nudge it in front of other windows
            self.window.on_top = False

    def _on_closing(self):
        if self.quitting:
            return True
        self.window.hide()
        return False  # cancel the close; keep running in the tray

    def _remember_geometry(self, *_):
        w = self.window
        if w and w.width > 400 and w.height > 300:
            db.kv_set("window:geometry", {"width": w.width, "height": w.height, "x": w.x, "y": w.y})

    def _on_shown(self):
        _set_window_icon(TITLE)

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
        if self.server:
            self.server.should_exit = True
        if self.tray:
            self.tray.stop()
        if self.window:
            self.window.destroy()

    # ---- run ----
    def run(self):
        if _already_running(self.url):
            return  # the running copy brings its window forward
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        ensure_icon_file()

        from .app import app
        app.state.show_window = self.show
        alerts.open_hook = self.show
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_config=None,
                                                    log_level="warning"))
        threading.Thread(target=self.server.run, name="server", daemon=True).start()
        if not _wait_for_server(self.url):
            raise RuntimeError("The Command Center server didn't start; see data/commandcenter.log")

        geo = db.kv_get("window:geometry") or {}
        self.window = webview.create_window(
            TITLE, self.url, width=geo.get("width", 1600), height=geo.get("height", 1000),
            x=geo.get("x"), y=geo.get("y"), min_size=(760, 560), background_color="#05070d",
            hidden=self.start_hidden, text_select=True,
        )
        self.window.events.closing += self._on_closing
        self.window.events.resized += self._remember_geometry
        self.window.events.moved += self._remember_geometry
        self.window.events.shown += self._on_shown

        self.tray = self._build_tray()
        self.tray.run_detached()
        webview.start(private_mode=False, storage_path=str(config.DATA_DIR / "webview"))
        # webview.start returns once the window is destroyed (Quit)
        if self.server:
            self.server.should_exit = True


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
