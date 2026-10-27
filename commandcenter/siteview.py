"""Web sites shown inside the Command Center window: the Pulse site (WORK Pulse tab) and Teams on the web (COMMS
Teams tab). Both refuse to be framed, so each is a WebView2 of its own that the window hosts, laid over its tab's
empty stage (overlay.NativeOverlay: the page reports where the stage is and what must be on top). Being a top-level
page in its own control, the site isn't a frame.

Isolation, the same for every site view:
  * its own profile folder in data/ (webview-pulse, webview-teams): its cookies and sign-in never mix with Command
    Center's, and the site has no Command Center cookie, so it can't use the local API;
  * devtools, host objects, web messages, browser extensions, swipe navigation, status bar, form autofill and
    password saving off; the OS-account single sign-on switch off; no script is injected into the site;
  * top-level navigation only to the view's allowlist (the site and the sign-in hosts it really uses). Anything
    else, and every new-window request, opens in the default browser; mail / Teams / Okta Verify links go to
    Windows; other schemes are dropped. Only after a click (IsUserInitiated), at most 3 per 10 s. The one exception
    is Okta FastPass, which the Okta page launches from script after its own button;
  * downloads only of export / document / image types, into the Windows Downloads folder; save-page-as and screen
    capture refused; permission requests (camera, microphone, location, notifications, clipboard read...) denied,
    except several downloads in a row;
  * nothing from the site reaches Command Center except the host, path and title shown in the tab's bar.
Keys: Ctrl+K (palette), Ctrl+Shift+P (Privacy mode) and F11 (full screen) inside the site go to Command Center.
"""
import ctypes
import logging
import os
import re
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

from . import config
from .overlay import NativeOverlay

log = logging.getLogger(__name__)

# Downloads go to the Windows Downloads folder, and only these types (exports, documents, images). Not legacy .xls
# (macro-capable; Pulse exports .xlsx / .csv, and an .xls file can still be opened in the browser).
DOWNLOAD_TYPES = {".csv", ".xlsx", ".pdf", ".docx", ".pptx", ".txt", ".json", ".png", ".jpg", ".jpeg", ".gif", ".webp"}
OKTA_HOSTS = ("sonance.okta.com",)  # Sonance's sign-in (Pulse directly; Microsoft 365 federates to it)
_HOSTNAME_RE = re.compile(r"^(?=.{1,200}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")


def _host_ok(host) -> bool:
    return isinstance(host, str) and bool(_HOSTNAME_RE.match(host))


def _https_on(url: str, hosts) -> bool:
    """https on 443, no user:pass@, host in hosts."""
    try:
        p = urllib.parse.urlsplit(str(url))
        return (p.scheme == "https" and not p.username and not p.password and p.port in (None, 443)
                and (p.hostname or "").lower() in hosts)
    except ValueError:
        return False


def _extra_hosts(section: str, key: str) -> set[str]:
    extra = (config.section(section, {}) or {}).get(key) or []
    return {str(h).strip().lower() for h in (extra if isinstance(extra, list) else []) if _host_ok(str(h).strip().lower())}


def _downloads_folder() -> Path:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") as k:
            return Path(winreg.QueryValueEx(k, "{374DE290-123F-4565-9164-39C4925E467B}")[0])
    except OSError:
        return Path.home() / "Downloads"


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


class _Site:
    def __init__(self, site, home):
        self.site, self.home = site, home
        self.panel = None        # WinForms Panel: positioned, shown/hidden and clipped (Region) by the view
        self.controller = None   # CoreWebView2Controller inside it
        self.pending_url = None  # a page asked for before the view existed
        self.failed = False
        self.region = None
        self.state = {"ready": False, "loading": False, "host": "", "path": "", "title": "", "can_back": False,
                      "can_forward": False, "outside": None, "note": None, "failed": False}


class SiteView(NativeOverlay):
    """One WebView2 per site of the view (Pulse: Prod / Dev keep their own page), created the first time it's shown.
    update() / handle_action() are called from the server thread; everything that touches WinForms or WebView2 runs
    on the window's UI thread (BeginInvoke). Reports are coalesced: however many arrive, one apply is queued at a time.
    A subclass says which sites it has, where they may go, and where its profile lives."""
    NAME = "site view"
    PROFILE = "webview-site"
    OUT_SCHEMES = ("mailto", "msteams", "com-okta-authenticator")
    OS_SSO = False  # sign in with the Windows account (Microsoft 365 single sign-on, as Edge does)
    ACTIONS =("back", "forward", "reload", "home", "open", "navigate")

    def __init__(self, app):
        super().__init__(app)
        self.views: dict[str, _Site] = {}
        self.env = None
        self._env_started = False
        self._env_error = None
        self._scheduler = None
        self._last_out = (None, 0.0)
        self._launches: list[float] = []  # when links last went to the browser / Windows (3 per 10 s)

    # ---- what a subclass decides ----
    def sites(self) -> dict[str, str]:
        raise NotImplementedError

    def allowed(self, url: str) -> bool:
        """May the view show this top-level page?"""
        raise NotImplementedError

    def site_of(self, url: str):
        """The site a navigate action may open this URL in (a page of the site itself, never a sign-in page)."""
        raise NotImplementedError

    def on_permission(self, v, kind) -> bool:
        """Allow this permission request? (Several downloads in a row only, by default.)"""
        from Microsoft.Web.WebView2.Core import CoreWebView2PermissionKind
        return kind == CoreWebView2PermissionKind.MultipleAutomaticDownloads

    # ---- from the server thread ----
    def status(self) -> dict:
        with self._lock:
            views = {site: dict(v.state) for site, v in self.views.items()}
        return {"available": self.available(), "sites": self.sites(), "views": views, "error": self._env_error}

    def action(self, site: str, name: str, url: str | None = None):
        if name == "navigate":
            site = self.site_of(url or "")
            if not site:
                raise ValueError(f"only {self.NAME.split()[0]} pages can be opened here")
        if site not in self.sites():
            raise ValueError(f"site must be one of {', '.join(self.sites())}")
        if name not in self.ACTIONS:
            raise ValueError("unknown action")
        self._invoke(lambda: self._do(site, name, url))
        return site

    def handle_action(self, body: dict) -> dict:
        url = body.get("url")
        site = str(body.get("site") or (self.SITES[0] if len(self.SITES) == 1 else ""))
        site = self.action(site, str(body.get("action") or ""), url if isinstance(url, str) and len(url) <= 2000 else None)
        return {"ok": True, "site": site}

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
        v = _Site(site, self.sites()[site])
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
        folder = config.DATA_DIR / self.PROFILE  # its own profile: the site's cookies never mix with Command Center's
        folder.mkdir(exist_ok=True)
        opts = CoreWebView2EnvironmentOptions()
        opts.AdditionalBrowserArguments = ""  # no remote debugging or other switches (the env vars were removed too)
        opts.AreBrowserExtensionsEnabled = False
        opts.AllowSingleSignOnUsingOSPrimaryAccount = self.OS_SSO
        opts.ExclusiveUserDataFolderAccess = True
        scheduler = TaskScheduler.FromCurrentSynchronizationContext()

        def ready(task):
            if task.IsFaulted or task.IsCanceled:
                self._env_error = f"The {self.NAME} couldn't start (WebView2)."
                log.warning("%s: WebView2 environment failed (%s)", self.NAME,
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
                v.state.update(failed=True, note=f"The {self.NAME} couldn't start.")
                log.warning("%s: controller for %s failed", self.NAME, v.site)
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
        st.AreBrowserAcceleratorKeysEnabled = True  # F5, Ctrl+F, Alt+Left; Ctrl+K, Ctrl+Shift+P and F11 are passed on
        _no_autofill(st, core)  # sign-in fields aren't remembered either
        try:
            core.Profile.DefaultDownloadFolderPath = str(_downloads_folder())
        except Exception as e:
            log.warning("%s: couldn't set the download folder (%s)", self.NAME, type(e).__name__)
        core.NavigationStarting += lambda s, a: self._on_nav(v, a)
        core.FrameNavigationStarting += lambda s, a: self._on_frame_nav(v, a)
        core.NewWindowRequested += lambda s, a: self._on_new_window(v, a)
        core.LaunchingExternalUriScheme += lambda s, a: self._on_scheme(v, a)
        core.DownloadStarting += lambda s, a: self._on_download(v, a)
        core.PermissionRequested += lambda s, a: self._on_permission(v, a)
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

    # ---- events from the page (UI thread) ----
    @staticmethod
    def _note(v, text):
        v.state.update(note=text, note_at=time.time())  # the tab shows it for a short while

    def _track(self, v, loading=None):
        core = v.controller.CoreWebView2 if v.controller else None
        if core is None:
            return
        p = urllib.parse.urlsplit(str(core.Source))
        v.state.update(host=(p.hostname or "")[:100], path=(p.path or "/")[:200], title=str(core.DocumentTitle or "")[:200],
                       can_back=bool(core.CanGoBack), can_forward=bool(core.CanGoForward))
        if loading is not None:
            v.state["loading"] = loading

    def _on_nav(self, v, args):
        uri = str(args.Uri)
        if self.allowed(uri) or uri == "about:blank":
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
        if scheme in self.OUT_SCHEMES:
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
        if len(uri) > 4000 or any(ord(c) < 32 or c == '"' for c in uri):
            log.warning("%s: dropped an odd link", self.NAME)
            return
        scheme = uri.split(":", 1)[0].lower()
        now = time.monotonic()
        last = self._last_out
        if last[0] == uri and now - last[1] < 1:
            return
        self._last_out = (uri, now)
        what = (urllib.parse.urlsplit(uri).hostname or "")[:100] if scheme in ("https", "http") else f"{scheme[:20]}:"
        source = str(v.controller.CoreWebView2.Source) if v.controller else ""
        fastpass = scheme == "com-okta-authenticator" and (urllib.parse.urlsplit(source).hostname or "").lower() in OKTA_HOSTS
        if not user and not fastpass:
            self._note(v, f"Blocked {what}: the page opened it by itself (click a link to open it)")
            log.warning("%s: blocked %s opened without a click", self.NAME, what)
            return
        if scheme not in ("https", "http") and scheme not in self.OUT_SCHEMES:
            log.warning("%s: blocked a %s: link", self.NAME, scheme[:20])
            return
        self._launches = [t for t in self._launches if now - t < 10]
        if len(self._launches) >= 3:
            self._note(v, f"Blocked {what}: too many links opened at once")
            log.warning("%s: blocked %s (more than 3 links in 10 s)", self.NAME, what)
            return
        self._launches.append(now)
        if scheme in ("https", "http"):
            v.state["outside"] = {"host": what, "at": time.time()}
            log.info("%s: opened %s in the browser%s", self.NAME, what, " (new window)" if new_window else "")
            threading.Thread(target=webbrowser.open, args=(uri,), daemon=True).start()
        else:
            log.info("%s: handed a %s: link to Windows", self.NAME, scheme)
            threading.Thread(target=os.startfile, args=(uri,), daemon=True).start()

    def _on_download(self, v, args):
        name = Path(str(args.ResultFilePath)).name
        if Path(name).suffix.lower() not in DOWNLOAD_TYPES:
            args.Cancel = True
            self._note(v, f"Download blocked ({Path(name).suffix[:10] or 'no type'}): only exports, documents and images.")
            log.warning("%s: blocked a %s download", self.NAME, Path(name).suffix[:10] or "typeless")
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
        log.info("%s: download to Downloads (%s)", self.NAME, target.suffix)

    def _on_permission(self, v, args):
        from Microsoft.Web.WebView2.Core import CoreWebView2PermissionState
        allow = self.on_permission(v, args.PermissionKind)
        args.State = CoreWebView2PermissionState.Allow if allow else CoreWebView2PermissionState.Deny
        if not allow:
            log.info("%s: refused a %s permission request", self.NAME, str(args.PermissionKind)[:30])

    def _on_failed(self, v, args):
        kind = str(args.ProcessFailedKind)
        log.warning("%s: %s process failed (%s)", self.NAME, v.site, kind)
        if "BrowserProcessExited" in kind:
            v.failed = True
            v.state.update(failed=True, note=f"The {self.NAME} stopped. Reload to start it again.")
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
        elif key == 0x7A:  # F11: full screen, as in the dashboard
            args.Handled = True
            threading.Thread(target=self.app.toggle_fullscreen, daemon=True).start()

    # ---- actions from the tab's buttons (UI thread) ----
    def _open_target(self, site, src) -> str:
        """What Open in browser opens: the current page when it's on the site itself, else its home (never a
        sign-in page's one-time link)."""
        return src if self.site_of(src) == site else self.sites()[site]

    def _do(self, site, name, url):
        v = self.views.get(site)
        if name == "open":
            src = str(v.controller.CoreWebView2.Source) if v and v.controller else ""
            threading.Thread(target=webbrowser.open, args=(self._open_target(site, src),), daemon=True).start()
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
        """A page opened before that site's view exists: remembered, and opened when the view starts."""
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


# ---- Pulse (WORK Pulse tab) ------------------------------------------------------------------------

PULSE_DEFAULTS = {"prod": "https://pulse.sonance.com", "dev": "https://dev.pulse.sonance.com"}
# Where the sign-in really goes (checked 2026-10: both sites 302 to Okta, and Okta's IdP discovery for sonance.com
# answers type OKTA, so no Microsoft hop). More can be added with pulse.view_sso_hosts in config.yaml.
PULSE_SSO_HOSTS = OKTA_HOSTS


def pulse_sites() -> dict[str, str]:
    """{"prod": "https://pulse.sonance.com/", "dev": "https://dev.pulse.sonance.com/"} (pulse.url / pulse.dev_url)."""
    s = config.section("pulse", {}) or {}
    out = {}
    for site, key in (("prod", "url"), ("dev", "dev_url")):
        url = str(s.get(key) or PULSE_DEFAULTS[site]).strip().rstrip("/")
        p = urllib.parse.urlsplit(url)
        if p.scheme != "https" or not _host_ok((p.hostname or "").lower()) or p.port or p.username or p.path:
            url = PULSE_DEFAULTS[site]
        out[site] = url + "/"
    return out


def pulse_hosts() -> set[str]:
    hosts = {urllib.parse.urlsplit(u).hostname for u in pulse_sites().values()} | set(PULSE_SSO_HOSTS)
    return hosts | _extra_hosts("pulse", "view_sso_hosts")


def pulse_allowed(url: str) -> bool:
    """May the Pulse view show this page? https on 443, a Pulse site or a sign-in host, no user:pass@."""
    return _https_on(url, pulse_hosts())


def pulse_site_of(url: str):
    """"prod" / "dev" for a page on one of the Pulse sites themselves (not the sign-in hosts), else None."""
    if not pulse_allowed(url):
        return None
    host = urllib.parse.urlsplit(str(url)).hostname.lower()
    return next((site for site, home in pulse_sites().items() if urllib.parse.urlsplit(home).hostname == host), None)


class PulseView(SiteView):
    """pulse.sonance.com (Prod) and dev.pulse.sonance.com (Dev), each keeping its own page; profile data/webview-pulse."""
    NAME = "Pulse view"
    PROFILE = "webview-pulse"
    SITES = tuple(PULSE_DEFAULTS)

    def sites(self):
        return pulse_sites()

    def allowed(self, url):
        return pulse_allowed(url)

    def site_of(self, url):
        return pulse_site_of(url)


# ---- Teams on the web (COMMS Teams tab) ----------------------------------------------------------------

TEAMS_HOME = "https://teams.microsoft.com/v2/"
# Where Teams on the web and its sign-in really go (checked 2026-10 without signing in: teams.microsoft.com/v2 sends
# the browser to login.microsoftonline.com; Microsoft's realm lookup for sonance.com answers "Federated" with
# sonance.okta.com (WS-Fed) as the sign-in page, which posts back to login.microsoftonline.com). login.microsoft.com
# and teams.cloud.microsoft are Microsoft's newer names for the same services. More: teams.view_hosts in config.yaml.
TEAMS_HOSTS = ("teams.microsoft.com", "teams.cloud.microsoft", "login.microsoftonline.com", "login.microsoft.com") + OKTA_HOSTS
_THREAD_ID = re.compile(r"19:[A-Za-z0-9_.:@-]{1,200}")
_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_DIGITS = re.compile(r"\d{1,20}")
_CONTEXTS = {'{"contextType":"chat"}'}
_DEEP = re.compile(r"(?:msteams:|https://teams\.microsoft\.com)/l/(chat|message)/([^/?#\s]{1,300})/([^/?#\s]{1,30})(?:\?([^#\s]{0,600}))?")


def teams_web_link(link: str) -> str | None:
    """A Teams deep link from the background reader (msteams:/l/chat/<thread>/0?tenantId=..., or /l/message/<thread>/
    <message>?...) as the page Teams on the web opens for it (what its own "use the web app" launcher goes to):
    https://teams.microsoft.com/_#/l/chat/<thread>/0?tenantId=... Every part is checked and the link rebuilt; None
    when anything doesn't fit."""
    m = _DEEP.fullmatch(link or "") if isinstance(link, str) else None
    if not m:
        return None
    kind, thread, item, query = m.group(1), urllib.parse.unquote(m.group(2)), m.group(3), m.group(4) or ""
    if not _THREAD_ID.fullmatch(thread) or (kind == "chat" and item != "0") or (kind == "message" and not _DIGITS.fullmatch(item)):
        return None
    out = []
    for k, v in urllib.parse.parse_qsl(query, keep_blank_values=False):
        if k == "tenantId" and _GUID.fullmatch(v):
            out.append((k, v))
        elif k == "parentMessageId" and kind == "message" and _DIGITS.fullmatch(v):
            out.append((k, v))
        elif k == "context" and kind == "message" and v in _CONTEXTS:
            out.append((k, v))
        else:
            return None
    q = urllib.parse.urlencode(out, quote_via=urllib.parse.quote)
    return f"https://teams.microsoft.com/_#/l/{kind}/{urllib.parse.quote(thread, safe=':@')}/{item}" + (f"?{q}" if q else "")


def teams_hosts() -> set[str]:
    return set(TEAMS_HOSTS) | _extra_hosts("teams", "view_hosts")


class TeamsView(SiteView):
    """Teams on the web (teams.microsoft.com/v2) in the Teams tab, for reading and answering whole chats. Its own
    profile, data/webview-teams: not the background reader's Edge profile (data/browser/teams), which keeps working
    on its own. Calls and meetings belong in the Teams app: camera, microphone and the rest are refused, with a hint."""
    NAME = "Teams view"
    PROFILE = "webview-teams"
    SITES = ("web",)
    # Joe's call (2026-10-27). Without it, Teams on the web asks for a sign-in pop-up when its session lapses, and the
    # view never opens a second window (it went to the browser, where it can't hand the sign-in back): "Sign in" did
    # nothing. With it, Teams signs in silently with the Windows work account, as in Edge (tested in a stand-in
    # WebView2: off = Microsoft's sign-in page, on = straight into Teams). Pages are still limited to teams_hosts().
    OS_SSO = True

    def sites(self):
        return {"web": TEAMS_HOME}

    def allowed(self, url):
        return _https_on(url, teams_hosts())

    def site_of(self, url):
        return "web" if _https_on(url, {"teams.microsoft.com", "teams.cloud.microsoft"}) else None

    def on_permission(self, v, kind):
        from Microsoft.Web.WebView2.Core import CoreWebView2PermissionKind as K
        if kind in (K.Camera, K.Microphone):
            self._note(v, "Calls and meetings: use the Teams app (Open in Teams app)")
            v.state["call_hint_at"] = time.time()
        return super().on_permission(v, kind)

    def handle_action(self, body: dict) -> dict:
        name = str(body.get("action") or "")
        if name == "app":  # the Teams app itself, on the same chat when one is given
            link = body.get("link")
            web = teams_web_link(link) if link else None
            if link and not web:
                raise ValueError("not a Teams chat link")
            # rebuilt from the checked parts: msteams:/l/chat/<thread>/0?tenantId=...
            target = "msteams:" + web[len("https://teams.microsoft.com/_#"):] if web else "msteams:"
            threading.Thread(target=os.startfile, args=(target,), daemon=True).start()
            return {"ok": True}
        if name == "chat":  # a chat / message from the Recent list, opened in the view
            web = teams_web_link(body.get("link"))
            if not web:
                raise ValueError("not a Teams chat link")
            self.action("web", "navigate", web)
            return {"ok": True, "site": "web"}
        return super().handle_action({**body, "site": "web"})
