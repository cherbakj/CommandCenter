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
from .overlay import NativeOverlay
from .ssmsdock import SsmsDock

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


def _no_autofill(settings, core=None):
    """WebView2 must not remember what is typed (form autofill) or offer to save passwords: the vault passphrase,
    Logi passwords and Toolkit fields would otherwise land in the profile in data/webview. CoreWebView2Settings 4+
    (the bundled Microsoft.Web.WebView2.Core has both properties). Anything an older version let it save is
    cleared from the profile once per launch."""
    for name in ("IsGeneralAutofillEnabled", "IsPasswordAutosaveEnabled"):
        try:
            setattr(settings, name, False)
        except Exception as e:
            log.warning("WebView2: couldn't turn off %s (%s)", name, type(e).__name__)
    if core is not None:
        try:
            from Microsoft.Web.WebView2.Core import CoreWebView2BrowsingDataKinds as kinds
            core.Profile.ClearBrowsingDataAsync(kinds.GeneralAutofill | kinds.PasswordAutosave)  # fire and forget
        except Exception as e:
            log.warning("WebView2: couldn't clear saved autofill data (%s)", type(e).__name__)


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


# ---- Pulse site view (the WORK Pulse tab) -----------------------------------------------------
# pulse.sonance.com refuses to be framed (frame-ancestors 'self'), so the Pulse tab is a second WebView2 that this
# window hosts itself, laid over the tab's empty stage. Being a top-level page in its own control, it isn't a frame.
# The dashboard reports where the stage is (CSS px) and whether anything of its own must be on top (a dialog, the
# palette, the splash: hide; a toast: cut a hole) through /api/window/pulse/* (security.py). There is still no
# js_api, and the Pulse site gets no channel to Command Center at all: its own profile (data/webview-pulse, so no
# Command Center cookie), and navigation limited to the Pulse sites and the sign-in hosts they use.

PULSE_DEFAULTS = {"prod": "https://pulse.sonance.com", "dev": "https://dev.pulse.sonance.com"}
# Where the sign-in really goes (checked 2026-10: both sites 302 to Okta, and Okta's IdP discovery for sonance.com
# answers type OKTA, so no Microsoft hop). More can be added with pulse.view_sso_hosts in config.yaml.
PULSE_SSO_HOSTS = ("sonance.okta.com",)
# Non-web links the site may use: mail, Teams, and Okta Verify (FastPass). Windows opens them; anything else is dropped.
PULSE_OUT_SCHEMES = ("mailto", "msteams", "com-okta-authenticator")
# Downloads go to the Windows Downloads folder, and only these types (exports, documents, images). Not legacy .xls
# (macro-capable; Pulse exports .xlsx / .csv, and an .xls ticket attachment can still be opened in the browser).
PULSE_DOWNLOAD_TYPES = {".csv", ".xlsx", ".pdf", ".docx", ".pptx", ".txt", ".json", ".png", ".jpg", ".jpeg",
                        ".gif", ".webp"}
_HOSTNAME_RE = None


def _host_ok(host) -> bool:
    global _HOSTNAME_RE
    import re
    _HOSTNAME_RE = _HOSTNAME_RE or re.compile(r"^(?=.{1,200}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")
    return isinstance(host, str) and bool(_HOSTNAME_RE.match(host))


def pulse_sites() -> dict[str, str]:
    """{"prod": "https://pulse.sonance.com/", "dev": "https://dev.pulse.sonance.com/"} (pulse.url / pulse.dev_url)."""
    from urllib.parse import urlsplit
    s = config.section("pulse", {}) or {}
    out = {}
    for site, key in (("prod", "url"), ("dev", "dev_url")):
        url = str(s.get(key) or PULSE_DEFAULTS[site]).strip().rstrip("/")
        p = urlsplit(url)
        if p.scheme != "https" or not _host_ok((p.hostname or "").lower()) or p.port or p.username or p.path:
            url = PULSE_DEFAULTS[site]
        out[site] = url + "/"
    return out


def pulse_hosts() -> set[str]:
    from urllib.parse import urlsplit
    hosts = {urlsplit(u).hostname for u in pulse_sites().values()} | set(PULSE_SSO_HOSTS)
    extra = (config.section("pulse", {}) or {}).get("view_sso_hosts") or []
    hosts |= {str(h).strip().lower() for h in (extra if isinstance(extra, list) else []) if _host_ok(str(h).strip().lower())}
    return hosts


def pulse_allowed(url: str) -> bool:
    """May the Pulse view show this page? https on 443, a Pulse site or a sign-in host, no user:pass@."""
    from urllib.parse import urlsplit
    try:
        p = urlsplit(str(url))
        return (p.scheme == "https" and not p.username and not p.password and p.port in (None, 443)
                and (p.hostname or "").lower() in pulse_hosts())
    except ValueError:
        return False


def pulse_site_of(url: str):
    """"prod" / "dev" for a page on one of the Pulse sites themselves (not the sign-in hosts), else None."""
    from urllib.parse import urlsplit
    if not pulse_allowed(url):
        return None
    host = urlsplit(str(url)).hostname.lower()
    return next((site for site, home in pulse_sites().items() if urlsplit(home).hostname == host), None)


def _downloads_folder() -> Path:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") as k:
            return Path(winreg.QueryValueEx(k, "{374DE290-123F-4565-9164-39C4925E467B}")[0])
    except OSError:
        return Path.home() / "Downloads"


class _SiteView:
    def __init__(self, site, home):
        self.site, self.home = site, home
        self.panel = None        # WinForms Panel: positioned, shown/hidden and clipped (Region) by PulseView
        self.controller = None   # CoreWebView2Controller inside it
        self.pending_url = None  # a ticket asked for before the view existed
        self.failed = False
        self.region = None
        self.state = {"ready": False, "loading": False, "host": "", "path": "", "title": "", "can_back": False,
                      "can_forward": False, "outside": None, "note": None, "failed": False}


class PulseView(NativeOverlay):
    """The Pulse site, one WebView2 per site (Prod / Dev keep their own page), created the first time it's shown.
    update() / action() are called from the server thread; everything that touches WinForms or WebView2 runs on the
    window's UI thread (BeginInvoke). Reports are coalesced: however many arrive, one apply is queued at a time."""
    NAME = "Pulse view"
    SITES = tuple(PULSE_DEFAULTS)

    def __init__(self, app):
        super().__init__(app)
        self.views: dict[str, _SiteView] = {}
        self.env = None
        self._env_started = False
        self._env_error = None
        self._scheduler = None
        self._last_out = (None, 0.0)
        self._launches: list[float] = []  # when links last went to the browser / Windows (3 per 10 s)

    # ---- from the server thread ----
    def status(self) -> dict:
        with self._lock:
            views = {site: dict(v.state) for site, v in self.views.items()}
        return {"available": self.available(), "sites": pulse_sites(), "views": views, "error": self._env_error}

    def action(self, site: str, name: str, url: str | None = None):
        sites = pulse_sites()
        if name == "navigate":
            site = pulse_site_of(url or "")
            if not site:
                raise ValueError("only Pulse pages can be opened here")
        if site not in sites:
            raise ValueError("site must be prod or dev")
        if name not in ("back", "forward", "reload", "home", "open", "navigate"):
            raise ValueError("unknown action")
        self._invoke(lambda: self._do(site, name, url))
        return site

    # ---- UI thread ----
    def _panels(self):
        return [v.panel for v in self.views.values()]

    def _apply(self):
        form = self._form()
        if form is None:
            return
        with self._lock:
            want = dict(self.want)
        show = self._showing(want)
        if show and want["site"] not in self.views:
            self._create(want["site"])
        for site, v in self.views.items():
            on = show and site == want["site"] and v.controller is not None and not v.failed
            if on:
                on = self._place(form, v, want["rect"], want["cuts"])
            self._set_visible(form, v, on)

    def _place(self, form, v, rect, cuts) -> bool:
        from System.Drawing import Rectangle
        geo = self._geometry(form, rect, cuts)
        if geo is None:
            return False
        left, top, width, height, holes = geo
        v.panel.SetBounds(left, top, width, height)
        v.controller.Bounds = Rectangle(0, 0, width, height)
        self._clip(v, width, height, holes)
        return True

    def _set_visible(self, form, v, on):
        if v.panel is None:
            return
        self._show_panel(form, v.panel, on)
        if v.controller is not None:
            v.controller.IsVisible = on  # lets WebView2 throttle a hidden page

    def _create(self, site):
        v = _SiteView(site, pulse_sites()[site])
        v.panel = self._new_panel()
        v.panel.Resize += lambda *_: self._fit(v)
        with self._lock:
            self.views[site] = v
        self._start_env()
        if self.env is not None:
            self._create_controller(v)

    def _fit(self, v):
        if v.controller is not None:
            from System.Drawing import Rectangle
            v.controller.Bounds = Rectangle(0, 0, v.panel.Width, v.panel.Height)

    def _start_env(self):
        if self._env_started:
            return
        self._env_started = True
        from System import Action
        from System.Threading.Tasks import Task, TaskScheduler
        from Microsoft.Web.WebView2.Core import CoreWebView2Environment, CoreWebView2EnvironmentOptions
        folder = config.DATA_DIR / "webview-pulse"  # its own profile: Okta / Pulse cookies never mix with Command Center's
        folder.mkdir(exist_ok=True)
        opts = CoreWebView2EnvironmentOptions()
        opts.AdditionalBrowserArguments = ""  # no remote debugging or other switches (the env vars were removed too)
        opts.AreBrowserExtensionsEnabled = False
        opts.AllowSingleSignOnUsingOSPrimaryAccount = False
        opts.ExclusiveUserDataFolderAccess = True
        scheduler = TaskScheduler.FromCurrentSynchronizationContext()

        def ready(task):
            if task.IsFaulted or task.IsCanceled:
                self._env_error = "The Pulse view couldn't start (WebView2)."
                log.warning("Pulse view: WebView2 environment failed (%s)",
                            type(task.Exception).__name__ if task.Exception else "canceled")
                return
            self.env = task.Result
            for v in list(self.views.values()):
                if v.controller is None:
                    self._create_controller(v)
        self._scheduler = scheduler
        CoreWebView2Environment.CreateAsync(None, str(folder), opts).ContinueWith(
            Action[Task[CoreWebView2Environment]](ready), scheduler)

    def _create_controller(self, v):
        from System import Action
        from System.Threading.Tasks import Task
        from Microsoft.Web.WebView2.Core import CoreWebView2Controller

        def ready(task):
            if task.IsFaulted or task.IsCanceled:
                v.failed = True
                v.state.update(failed=True, note="The Pulse view couldn't start.")
                log.warning("Pulse view: controller for %s failed", v.site)
                return
            self._setup(v, task.Result)
        self.env.CreateCoreWebView2ControllerAsync(v.panel.Handle).ContinueWith(
            Action[Task[CoreWebView2Controller]](ready), self._scheduler)

    def _setup(self, v, controller):
        from System.Drawing import Color
        v.controller = controller
        controller.IsVisible = False
        controller.DefaultBackgroundColor = Color.FromArgb(255, 5, 7, 13)
        self._fit(v)
        core = controller.CoreWebView2
        st = core.Settings
        st.AreDevToolsEnabled = False
        st.AreHostObjectsAllowed = False
        st.IsWebMessageEnabled = False       # nothing on the site can post to this app
        st.IsStatusBarEnabled = False
        st.IsSwipeNavigationEnabled = False
        st.AreDefaultContextMenusEnabled = True  # copy / paste; "Inspect" isn't there with devtools off
        st.AreBrowserAcceleratorKeysEnabled = True  # F5, Ctrl+F, Alt+Left; Ctrl+K and F11 are passed on below
        _no_autofill(st, core)  # Okta sign-in fields aren't remembered either
        try:
            core.Profile.DefaultDownloadFolderPath = str(_downloads_folder())
        except Exception as e:
            log.warning("Pulse view: couldn't set the download folder (%s)", type(e).__name__)
        core.NavigationStarting += lambda s, a: self._on_nav(v, a)
        core.FrameNavigationStarting += lambda s, a: self._on_frame_nav(v, a)
        core.NewWindowRequested += lambda s, a: self._on_new_window(v, a)
        core.LaunchingExternalUriScheme += lambda s, a: self._on_scheme(v, a)
        core.DownloadStarting += lambda s, a: self._on_download(v, a)
        core.PermissionRequested += self._on_permission
        core.ScreenCaptureStarting += lambda s, a: setattr(a, "Cancel", True)
        core.SaveAsUIShowing += lambda s, a: setattr(a, "Cancel", True)  # Ctrl+S: no "save page" anywhere
        core.SourceChanged += lambda s, a: self._track(v)
        core.HistoryChanged += lambda s, a: self._track(v)
        core.DocumentTitleChanged += lambda s, a: self._track(v)
        core.NavigationCompleted += lambda s, a: self._track(v, loading=False)
        core.ProcessFailed += lambda s, a: self._on_failed(v, a)
        controller.AcceleratorKeyPressed += lambda s, a: self._on_key(a)
        url, v.pending_url = v.pending_url or v.home, None
        v.state["ready"] = True
        core.Navigate(url)
        self._apply()

    # ---- events from the Pulse page (UI thread) ----
    @staticmethod
    def _note(v, text):
        v.state.update(note=text, note_at=time.time())  # the tab shows it for a short while

    def _track(self, v, loading=None):
        from urllib.parse import urlsplit
        core = v.controller.CoreWebView2 if v.controller else None
        if core is None:
            return
        p = urlsplit(str(core.Source))
        v.state.update(host=(p.hostname or "")[:100], path=(p.path or "/")[:200], title=str(core.DocumentTitle or "")[:200],
                       can_back=bool(core.CanGoBack), can_forward=bool(core.CanGoForward))
        if loading is not None:
            v.state["loading"] = loading

    def _on_nav(self, v, args):
        uri = str(args.Uri)
        if pulse_allowed(uri) or uri == "about:blank":
            v.state.update(loading=True, note=None)
            return
        args.Cancel = True
        self._outside(v, uri, user=bool(args.IsUserInitiated))

    def _on_frame_nav(self, v, args):
        uri = str(args.Uri)
        scheme = uri.split(":", 1)[0].lower()
        if scheme in ("https", "http", "about", "data", "blob"):
            return  # frames are the site's own business (its CSP); only top-level pages are limited
        args.Cancel = True
        if scheme in PULSE_OUT_SCHEMES:
            self._outside(v, uri, user=bool(args.IsUserInitiated))

    def _on_new_window(self, v, args):
        args.Handled = True  # never a second window: links that want one open in the default browser
        self._outside(v, str(args.Uri), new_window=True, user=bool(args.IsUserInitiated))

    def _on_scheme(self, v, args):
        args.Cancel = True  # no WebView2 prompt; _outside decides (and NavigationStarting may already have)
        self._outside(v, str(args.Uri), user=bool(args.IsUserInitiated))

    def _outside(self, v, uri, new_window=False, user=True):
        """A link the view doesn't show: web pages open in the default browser, mail / Teams / Okta Verify links in
        their apps, anything else is dropped. Only for a click (WebView2's IsUserInitiated), so a script on an allowed
        page can't open tabs or launch apps by itself; the one exception is Okta FastPass, which the Okta sign-in page
        launches from script after its own button. At most 3 a 10 s, and the same link once a second (two events can
        report the same one)."""
        from urllib.parse import urlsplit
        if len(uri) > 4000 or any(ord(c) < 32 or c == '"' for c in uri):
            log.warning("Pulse view: dropped an odd link")
            return
        scheme = uri.split(":", 1)[0].lower()
        now = time.monotonic()
        last = self._last_out
        if last[0] == uri and now - last[1] < 1:
            return
        self._last_out = (uri, now)
        what = (urlsplit(uri).hostname or "")[:100] if scheme in ("https", "http") else f"{scheme[:20]}:"
        source = str(v.controller.CoreWebView2.Source) if v.controller else ""
        fastpass = scheme == "com-okta-authenticator" and (urlsplit(source).hostname or "").lower() in PULSE_SSO_HOSTS
        if not user and not fastpass:
            self._note(v, f"Blocked {what}: the page opened it by itself (click a link to open it)")
            log.warning("Pulse view: blocked %s opened without a click", what)
            return
        if scheme not in ("https", "http") and scheme not in PULSE_OUT_SCHEMES:
            log.warning("Pulse view: blocked a %s: link", scheme[:20])
            return
        self._launches = [t for t in self._launches if now - t < 10]
        if len(self._launches) >= 3:
            self._note(v, f"Blocked {what}: too many links opened at once")
            log.warning("Pulse view: blocked %s (more than 3 links in 10 s)", what)
            return
        self._launches.append(now)
        if scheme in ("https", "http"):
            host = (urlsplit(uri).hostname or "")[:100]
            v.state["outside"] = {"host": host, "at": time.time()}
            log.info("Pulse view: opened %s in the browser%s", host, " (new window)" if new_window else "")
            threading.Thread(target=webbrowser.open, args=(uri,), daemon=True).start()
        else:
            log.info("Pulse view: handed a %s: link to Windows", scheme)
            threading.Thread(target=os.startfile, args=(uri,), daemon=True).start()

    def _on_download(self, v, args):
        name = Path(str(args.ResultFilePath)).name
        if Path(name).suffix.lower() not in PULSE_DOWNLOAD_TYPES:
            args.Cancel = True
            self._note(v, f"Download blocked ({Path(name).suffix[:10] or 'no type'}): only exports, documents and images.")
            log.warning("Pulse view: blocked a %s download", Path(name).suffix[:10] or "typeless")
            return
        folder = _downloads_folder()
        target = Path(str(args.ResultFilePath))
        if target.parent != folder:  # WebView2 picks a free name in the profile's download folder (set to Downloads)
            stem, ext, n = Path(name).stem, Path(name).suffix, 1
            target = folder / name
            while target.exists():
                target, n = folder / f"{stem} ({n}){ext}", n + 1
            args.ResultFilePath = str(target)
        self._note(v, f"Downloading {target.name} to Downloads")
        log.info("Pulse view: download to Downloads (%s)", target.suffix)

    @staticmethod
    def _on_permission(sender, args):
        from Microsoft.Web.WebView2.Core import CoreWebView2PermissionKind, CoreWebView2PermissionState
        # More than one download in a row (exports): still only the allowed types, into Downloads. Everything else
        # (camera, microphone, location, notifications, clipboard read...) is refused.
        multiple = args.PermissionKind == CoreWebView2PermissionKind.MultipleAutomaticDownloads
        args.State = CoreWebView2PermissionState.Allow if multiple else CoreWebView2PermissionState.Deny

    def _on_failed(self, v, args):
        kind = str(args.ProcessFailedKind)
        log.warning("Pulse view: %s process failed (%s)", v.site, kind)
        if "BrowserProcessExited" in kind:
            v.failed = True
            v.state.update(failed=True, note="The Pulse view stopped. Reload to start it again.")
            self._apply()
        else:
            self._note(v, "The page stopped responding. Reload it.")

    def _on_key(self, args):
        from Microsoft.Web.WebView2.Core import CoreWebView2KeyEventKind
        if args.KeyEventKind not in (CoreWebView2KeyEventKind.KeyDown, CoreWebView2KeyEventKind.SystemKeyDown):
            return
        down = lambda vk: bool(ctypes.windll.user32.GetKeyState(vk) & 0x8000)  # noqa: E731
        ctrl, shift, alt = down(0x11), down(0x10), down(0x12)
        key = int(args.VirtualKey)
        if key == 0x4B and ctrl and not shift and not alt:  # Ctrl+K: Command Center's palette
            args.Handled = True
            self._focus_dashboard()
            # run_js waits for the UI thread (this one), so it runs from another thread
            threading.Thread(target=self.app.window.run_js, args=("document.getElementById('search-btn')?.click()",),
                             daemon=True).start()
        elif key == 0x50 and ctrl and shift and not alt:  # Ctrl+Shift+P: Privacy mode (hide personal now)
            args.Handled = True
            threading.Thread(target=self.app.privacy_key, daemon=True).start()
        elif int(args.VirtualKey) == 0x7A:  # F11: full screen, as in the dashboard
            args.Handled = True
            threading.Thread(target=self.app.toggle_fullscreen, daemon=True).start()

    # ---- actions from the tab's buttons (UI thread) ----
    def _do(self, site, name, url):
        v = self.views.get(site)
        if name == "open":
            src = str(v.controller.CoreWebView2.Source) if v and v.controller else ""
            target = src if pulse_site_of(src) else pulse_sites()[site]  # never a sign-in page's one-time link
            threading.Thread(target=webbrowser.open, args=(target,), daemon=True).start()
            return
        if v is None or v.controller is None:
            if name == "navigate":
                if v is None:
                    self._pending_for(site, url)
                else:
                    v.pending_url = url
            return
        if v.failed and name in ("reload", "home", "navigate"):
            self._restart(v, url if name == "navigate" else None)
            return
        core = v.controller.CoreWebView2
        if name == "back" and core.CanGoBack:
            core.GoBack()
        elif name == "forward" and core.CanGoForward:
            core.GoForward()
        elif name == "reload":
            core.Reload()
        elif name == "home":
            core.Navigate(v.home)
        elif name == "navigate":
            core.Navigate(url)

    def _pending_for(self, site, url):
        """A ticket opened before that site's view exists: remembered, and opened when the view starts."""
        self._create(site)
        self.views[site].pending_url = url

    def _restart(self, v, url=None):
        try:
            v.controller.Close()
        except Exception:
            pass
        v.controller, v.failed = None, False
        v.pending_url = url
        v.state.update(failed=False, note=None, ready=False)
        if self.env is not None:
            self._create_controller(v)


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
        self._unlocking = False
        self._unlock_lock = threading.Lock()
        self._cancel = threading.Event()
        self.quitting = False
        self.window = None
        self.tray = None
        self.server = None
        self._server_thread = None
        self.hotkey = None
        # Native windows over dashboard tabs (overlay.py; security.py's /api/window/<view>/* routes)
        self.pulse = PulseView(self)  # the Pulse tab's site view
        self.ssms = SsmsDock(self)    # the SQL tab's docked SSMS
        self.ssms.hotkey_factory = GlobalHotkey
        self.overlays = {"pulse": self.pulse, "ssms": self.ssms}

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
        return {"desktop": True, "fullscreen": self.fullscreen, "locked": not self.unlocked, "pulse_view": True,
                "ssms_dock": True}

    def lock(self):
        """Lock from the dashboard (the session secret was already replaced by security.end_session)."""
        self.unlocked = False
        self.locked_by_user = True
        self.pulse.forget()  # the Pulse site never stays over the lock screen
        self.ssms.on_lock()  # SSMS goes back to a normal (minimized) window; docked again after unlock
        self.ssms.forget()
        if self.window:
            self.lock_showing = True
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
        if not self.lock_showing:
            log.warning("Ignored a lock-screen action (%s) while the lock screen isn't showing", str(action)[:20])
            return
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
                self.lock_showing = False  # from here on the window shows the dashboard, not the lock page
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
            self.pulse.release_focus()  # typing goes to the capture box, not the Pulse site or SSMS
            self.ssms.release_focus()
            # run_js, not evaluate_js: evaluate_js wraps the script in eval(), which the page's CSP (no 'unsafe-eval') blocks.
            self.window.run_js(f"window.CC && CC.quickCapture && CC.quickCapture({{restore: {json.dumps(restore)}}})")

    def privacy_key(self):
        """Ctrl+Shift+P pressed while the Pulse site or SSMS has the keyboard: the dashboard's own shortcut (privacy.js)."""
        if self.window and self.unlocked and not self.lock_showing and not self.quitting:
            self.window.run_js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'P', code: 'KeyP', "
                               "ctrlKey: true, shiftKey: true, bubbles: true}))")

    def quit(self):
        self.quitting = True
        self.ssms.release("quit")  # SSMS goes back to a normal window before this one is destroyed (never closed)
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
            _nav["leaving"] = lambda: [view.forget() for view in self.overlays.values()]
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
            self.ssms.install_hooks()

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
