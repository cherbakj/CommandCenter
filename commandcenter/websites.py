"""Sites added in the Websites tab: a name, an address and, if the site needs one, a login. Each shows inside the tab in
a WebView2 of its own (WebsiteView, the same isolation as the Pulse and Teams views; profile data/webview-websites).
The value monitors in config.yaml are separate (connectors/web.py).

  * The list (name, address, other addresses) is sealed work data: kv websites:sites, kept until removed.
  * The username and password live only in Windows Credential Manager (CommandCenter-Websites). The password never comes
    back to the page, and logs carry counts and types only.
  * Sign in fills the login form of the page on show, only on Joe's click and only on the site's own address or one he
    listed for it. It's the one script any site view ever runs, and it doesn't press the site's button: he does.
Addresses are https only.

Tabs and other sites (Joe's call 2026-11-06, for the Okta dashboard): a link that wants a new window (an Okta app tile,
a target=_blank link), clicked in a site, opens as a tab inside the Websites tab (WebsiteView.tabs, memory only, at
most MAX_TABS; closed with its x or when its page closes itself). A page on an address its site doesn't cover (in a
saved site or a tab) isn't shown until Joe allows it: the bar asks "Allow <domain> in CC?" with Allow / Open in
browser, and an allowed domain (with its subdomains; the exact name for shared hosting such as azurewebsites.net) is
remembered (kv websites:allowed, sealed, until removed under Allowed sites). Only https pages on the normal port can be
allowed; anything else opens in the default browser after a click, as in the other views. Tabs have the same
isolation as the sites (same profile, no Command Center cookie, limited downloads, permissions refused)."""
import json
import logging
import re
import secrets
import threading
import time
import urllib.parse
import winreg

import os
import webbrowser

import keyring
import keyring.errors
from fastapi import APIRouter, Body, HTTPException

from . import db, security
from .siteview import OKTA_HOSTS, SiteView, _host_ok, _https_on

log = logging.getLogger(__name__)
router = APIRouter()

KV_SITES = "websites:sites"
KEYRING_SERVICE = "CommandCenter-Websites"
MAX_SITES = 30
MAX_OTHER_HOSTS = 20
SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu"}  # example.co.uk: the domain is three labels
KV_ALLOWED = "websites:allowed"
KV_SEEDED = "websites:seeded"
MAX_ALLOWED = 200
MAX_TABS = 12
# Domains many unrelated sites share: allowing one allows only that exact name, never the whole domain.
SHARED_DOMAINS = {"azurewebsites.net", "cloudfront.net", "amazonaws.com", "herokuapp.com", "github.io", "netlify.app",
                  "vercel.app", "windows.net", "azureedge.net", "appspot.com", "web.app", "firebaseapp.com", "pages.dev",
                  "workers.dev", "onrender.com", "blogspot.com", "wordpress.com", "sharepoint.com", "myshopify.com",
                  "okta.com", "oktapreview.com", "screenconnect.com", "force.com", "my.salesforce.com", "zendesk.com",
                  "atlassian.net", "service-now.com", "servicenow.com", "box.com", "ngrok.io", "ngrok-free.app"}
# Sonance's Okta dashboard, added once (Joe asked for it, 2026-11-06); removing it keeps it removed.
SEED_SITES = ({"name": "Okta", "url": "https://sonance.okta.com/"},)
_SC_SCHEME = re.compile(r"sc-[0-9a-f]{16}")  # the installed ScreenConnect client's own link (Join on its web page)
_lock = threading.Lock()
_sites: list[dict] | None = None  # read on every page load in the view, so kept in memory


def sites() -> list[dict]:
    global _sites
    if _sites is None:
        _sites = [s for s in db.kv_get(KV_SITES, []) or [] if isinstance(s, dict) and s.get("id") and s.get("url")]
        _seed()
    return _sites


def _seed():
    """Adds SEED_SITES once (kv websites:seeded lists the ones done), unless a site with that address is there."""
    global _sites
    done = db.kv_get(KV_SEEDED, []) or []
    todo = [x for x in SEED_SITES if x["url"] not in done]
    if not todo:
        return
    have = {(urllib.parse.urlsplit(s["url"]).hostname or "").lower() for s in _sites}
    names = {s["name"].lower() for s in _sites}
    new = [{"id": secrets.token_hex(6), "name": x["name"], "url": x["url"], "other_hosts": []} for x in todo
           if (urllib.parse.urlsplit(x["url"]).hostname or "") not in have and x["name"].lower() not in names]
    if new:
        _sites = [*_sites, *new]
        db.kv_set(KV_SITES, _sites)
        log.info("Websites: added %d built-in site(s)", len(new))
    db.kv_set(KV_SEEDED, [*done, *(x["url"] for x in todo)])


def find(site_id) -> dict | None:
    return next((s for s in sites() if s["id"] == site_id), None)


def _store(new: list[dict]):
    global _sites
    db.kv_set(KV_SITES, new)
    _sites = new


def clean_address(raw) -> str:
    """A plain https address, or ValueError saying why not. "example.com" means https://example.com."""
    url = str(raw or "").strip()
    if not url:
        raise ValueError("Enter the site's address.")
    if len(url) > 500 or any(ord(c) < 33 or c == '"' for c in url):
        raise ValueError("That doesn't look like a web address.")
    if "://" not in url:
        url = "https://" + url
    try:
        p = urllib.parse.urlsplit(url)
        host, port = (p.hostname or "").lower(), p.port
    except ValueError:
        raise ValueError("That doesn't look like a web address.") from None
    if p.scheme.lower() != "https":
        raise ValueError("Only https addresses can be added.")
    if p.username or p.password:
        raise ValueError("Put the username and password in their own fields, not in the address.")
    if not _host_ok(host) or port not in (None, 443):
        raise ValueError("That doesn't look like a web address (a name such as example.com, on the normal https port).")
    return urllib.parse.urlunsplit(("https", host, p.path or "/", p.query, p.fragment))


def clean_hosts(raw) -> list[str]:
    """Other addresses a site may go to: names only (a pasted address is cut down to its name)."""
    out = []
    for item in re.split(r"[\s,;]+", str(raw or "").strip()) if not isinstance(raw, list) else raw:
        text = str(item).strip().lower()
        if not text:
            continue
        host = (urllib.parse.urlsplit(text if "://" in text else "https://" + text).hostname or "").lower()
        if not _host_ok(host):
            raise ValueError(f"{text[:60]} isn't an address name such as login.example.com.")
        if host not in out:
            out.append(host)
    if len(out) > MAX_OTHER_HOSTS:
        raise ValueError(f"At most {MAX_OTHER_HOSTS} other addresses.")
    return out


def host_of(site: dict) -> str:
    return (urllib.parse.urlsplit(site["url"]).hostname or "").lower()


def fill_hosts(site: dict) -> set[str]:
    """Where its login may be filled in: exactly the site's own name and the ones listed for it."""
    return {host_of(site), *site.get("other_hosts", [])}


def _domain(host: str) -> str:
    labels = host.split(".")
    keep = 3 if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in SECOND_LEVEL else 2
    return ".".join(labels[-keep:])


def page_allowed(site: dict, url: str) -> bool:
    """May this site's view show this page? Its listed names, and its own domain (www., a consent page...)."""
    try:
        host = (urllib.parse.urlsplit(str(url)).hostname or "").lower()
    except ValueError:
        return False
    domain = allow_key(host_of(site))  # its domain; on shared hosting (okta.com, azurewebsites.net...) its own name only
    return _https_on(url, {host}) and (host in fill_hosts(site) or host == domain or host.endswith("." + domain))


# ---- other sites Joe allowed (from a tab or a site's link) -----------------------------------------------

def allow_key(host: str) -> str:
    """What allowing a page's host remembers: its domain (subdomains included), or the exact name on shared hosting."""
    host = host.lower()
    domain = _domain(host)
    shared = any(domain == d or domain.endswith("." + d) or host.endswith("." + d) for d in SHARED_DOMAINS)
    return host if shared else domain


def allowed_list() -> list[str]:
    return [d for d in db.kv_get(KV_ALLOWED, []) or [] if isinstance(d, str) and _host_ok(d)]


def is_allowed(url: str) -> bool:
    """A page on a domain (or exact name) Joe allowed: https on 443, no user:pass@."""
    try:
        host = (urllib.parse.urlsplit(str(url)).hostname or "").lower()
    except ValueError:
        return False
    if not _https_on(url, {host}) or not _host_ok(host):
        return False
    return any(host == d or host.endswith("." + d) for d in allowed_list())


def askable(url: str) -> str | None:
    """The key a page could be allowed under (https on the normal port, a real name), else None."""
    try:
        host = (urllib.parse.urlsplit(str(url)).hostname or "").lower()
    except ValueError:
        return None
    return allow_key(host) if _host_ok(host) and _https_on(url, {host}) else None


def add_allowed(key: str):
    with _lock:
        now = allowed_list()
        if key in now:
            return
        if len(now) >= MAX_ALLOWED:
            raise ValueError(f"At most {MAX_ALLOWED} allowed sites: remove some under Allowed sites.")
        db.kv_set(KV_ALLOWED, [*now, key])
    log.info("Websites: allowed another site (%d in all)", len(now) + 1)


def remove_allowed(key: str):
    with _lock:
        db.kv_set(KV_ALLOWED, [d for d in allowed_list() if d != key])
    log.info("Websites: removed an allowed site")


# ---- the login, in Credential Manager ------------------------------------------------------------

def get_login(site_id) -> dict:
    """{"u": username, "p": password}, or {} when none is saved."""
    try:
        raw = keyring.get_password(KEYRING_SERVICE, site_id)
    except keyring.errors.KeyringError as e:
        log.warning("Websites: couldn't read a login (%s)", type(e).__name__)
        return {}
    try:
        login = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return login if isinstance(login, dict) else {}


def _delete_login(site_id):
    try:
        keyring.delete_password(KEYRING_SERVICE, site_id)
    except keyring.errors.PasswordDeleteError:
        pass  # none was saved


def _public(site: dict) -> dict:
    """What the page gets: never the password."""
    login = get_login(site["id"])
    return {"id": site["id"], "name": site["name"], "url": site["url"], "other_hosts": site.get("other_hosts", []),
            "username": login.get("u") or "", "has_password": bool(login.get("p"))}


def save(site_id, name, url, other_hosts, username, password, clear_login) -> dict:
    """Adds or changes a site. An empty password keeps the saved one. Raises ValueError with what to correct."""
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    if not name or len(name) > 60:
        raise ValueError("Give the site a name of up to 60 characters.")
    url, other_hosts = clean_address(url), clean_hosts(other_hosts)
    username, password = str(username or "").strip(), str(password or "")
    if len(username) > 256 or len(password) > 512:
        raise ValueError("That username or password is too long.")
    if any(ord(c) < 32 for c in username + password):
        raise ValueError("The username or password has a line break in it.")
    with _lock:
        now = sites()
        old = next((s for s in now if s["id"] == site_id), None) if site_id else None
        if site_id and not old:
            raise ValueError("That site is no longer there.")
        if not old and len(now) >= MAX_SITES:
            raise ValueError(f"At most {MAX_SITES} sites.")
        if any(s["name"].lower() == name.lower() and s is not old for s in now):
            raise ValueError(f"There's already a site called {name}.")
        new = {"id": old["id"] if old else secrets.token_hex(6), "name": name, "url": url, "other_hosts": other_hosts}
        if clear_login:
            _delete_login(new["id"])
        else:
            kept = get_login(new["id"]) if old else {}
            login = {"u": username, "p": password or kept.get("p") or ""}
            if login["u"] or login["p"]:
                keyring.set_password(KEYRING_SERVICE, new["id"], json.dumps(login))
            elif kept:
                _delete_login(new["id"])
        _store([new if s is old else s for s in now] if old else [*now, new])
    log.info("Websites: saved a site (%d in all)", len(sites()))
    if old and host_of(old) != host_of(new):
        _drop_view(new["id"])  # it starts again at the new address
    return new


def remove(site_id):
    with _lock:
        _store([s for s in sites() if s["id"] != site_id])
        _delete_login(site_id)
    log.info("Websites: removed a site (%d left)", len(sites()))
    _drop_view(site_id)


def _drop_view(site_id):
    view = security._overlay("websites")
    if view:
        view.drop(site_id)


@router.get("/api/websites/sites")
async def list_sites():
    return {"sites": [_public(s) for s in sites()]}


@router.post("/api/websites/sites")
async def save_site(id: str | None = Body(None), name: str = Body(""), url: str = Body(""), other_hosts: str = Body(""),
                    username: str = Body(""), password: str = Body(""), clear_login: bool = Body(False)):
    try:
        return _public(save(id, name, url, other_hosts, username, password, clear_login))
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except keyring.errors.KeyringError as e:
        log.warning("Websites: couldn't save a login (%s)", type(e).__name__)
        raise HTTPException(500, "Windows Credential Manager couldn't save the login. Nothing was changed.") from None


@router.post("/api/websites/sites/remove")
async def remove_site(id: str = Body(..., embed=True)):
    if not find(id):
        raise HTTPException(404, "That site is no longer there.")
    remove(id)
    return {"ok": True}


@router.get("/api/websites/allowed")
async def list_allowed():
    return {"allowed": allowed_list()}


@router.post("/api/websites/allowed/remove")
async def remove_allowed_site(domain: str = Body(..., embed=True)):
    if domain not in allowed_list():
        raise HTTPException(404, "That site isn't on the list.")
    remove_allowed(domain)
    return {"ok": True}


def _scheme_registered(scheme: str) -> bool:
    """A link scheme an installed app registered (HKEY_CLASSES_ROOT\\<scheme> with "URL Protocol")."""
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, scheme) as k:
            winreg.QueryValueEx(k, "URL Protocol")
        return True
    except OSError:
        return False


# ---- the view -------------------------------------------------------------------------------------

# Fills the page on show. Checks the page's address itself as well, in case it changed since the host was checked.
# Without a password field, only a field that says it's for a username (never a search box); a site that asks for
# the username first gets the password on the next press.
FILL_JS = r"""(function (host, user, pass) {
  if (location.protocol !== "https:" || location.hostname !== host) return "moved";
  const shown = (e) => !e.disabled && !e.readOnly && e.offsetParent !== null;
  const fields = [...document.querySelectorAll("input")].filter(shown);
  const textual = (e) => ["text", "email", "tel", ""].includes(e.getAttribute("type") || "");
  const named = (e) => /username|email/i.test(e.autocomplete) || /user|email|login|account/i.test(`${e.name} ${e.id}`);
  const pw = fields.find((e) => e.type === "password");
  const before = pw ? fields.slice(0, fields.indexOf(pw)).filter(textual) : [];
  const un = before.find(named) || before[before.length - 1] || (pw ? null : fields.filter(textual).find(named));
  const put = (e, v) => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(e, v);
    e.dispatchEvent(new Event("input", { bubbles: true }));
    e.dispatchEvent(new Event("change", { bubbles: true }));
  };
  const did = [];
  if (un && user) { put(un, user); did.push("username"); }
  if (pw && pass) { put(pw, pass); did.push("password"); }
  return did.join("+") || "none";
})(%s, %s, %s)"""

FILLED = {
    "username+password": "Filled the username and password. Now press the site's own sign-in button.",
    "password": "Filled the password. Now press the site's own sign-in button.",
    "username": "Filled the username. Continue on the site, then press Sign in again for the password.",
    "none": "Nothing to fill on this page. Go to its sign-in page and press Sign in again.",
    "moved": "Not filled: the page changed. Press Sign in again.",
}


class WebsiteView(SiteView):
    """The sites added in the Websites tab, each keeping its own page, and the tabs their links open (self.tabs).
    Nothing is handed to other apps but Okta FastPass (from Okta's own page) and the ScreenConnect client's link. A video
    keeps playing when another tab is opened, as in a browser tab; Lock mutes it."""
    NAME = "Websites view"
    PROFILE = "webview-websites"
    OUT_SCHEMES = ("com-okta-authenticator",)
    _muted = False

    def __init__(self, app):
        self.tabs: dict[str, dict] = {}   # id -> {"opener": site id, "url": first page, "at": time}; memory only
        self._asks: dict[str, dict] = {}  # site / tab id -> {"key", "url"}: the page waiting for Allow
        self._opened = None               # (tab id, time) of the newest tab, so the page switches to it
        super().__init__(app)             # (reads SITES, so the tabs come first)

    @property
    def SITES(self):
        return tuple(s["id"] for s in sites()) + tuple(self.tabs) or ("none",)

    def sites(self):
        return {**{s["id"]: s["url"] for s in sites()}, **{t: d["url"] for t, d in list(self.tabs.items())}}

    def allowed(self, url):
        return any(page_allowed(s, url) for s in sites()) or is_allowed(url)

    def allowed_in(self, v, url) -> bool:
        """What this site's (or tab's) own view may show: its site's pages and the sites Joe allowed."""
        tab = self.tabs.get(v.site)
        site = find(tab["opener"] if tab else v.site)
        return (site is not None and page_allowed(site, url)) or is_allowed(url)

    def site_of(self, url):
        return next((s["id"] for s in sites() if _https_on(url, {host_of(s)})), None)

    def _showing(self, want):
        return super()._showing(want) and (find(want["site"]) is not None or want["site"] in self.tabs)

    def status(self) -> dict:
        out = super().status()
        with self._lock:
            out["tabs"] = [{"id": t, "opener": d["opener"], "at": d["at"]} for t, d in self.tabs.items()]
            out["opened"] = {"id": self._opened[0], "at": self._opened[1]} if self._opened else None
            for site, ask in self._asks.items():
                if site in out["views"]:
                    out["views"][site]["ask"] = ask["key"]
        return out

    # ---- pages outside the site: ask first; links wanting a new window: a tab (UI thread) ----
    def _setup(self, v, controller):
        super()._setup(v, controller)
        core = controller.CoreWebView2
        # a tab's page closing itself (window.close() after a sign-in): the tab closes, after this event has returned
        core.WindowCloseRequested += lambda s, a: self._invoke(lambda: self._close_tab(v.site)) if v.site in self.tabs else None

    def _on_nav(self, v, args):
        uri = str(args.Uri)
        if self.allowed_in(v, uri) or uri == "about:blank":
            with self._lock:
                self._asks.pop(v.site, None)
            v.state.update(loading=True, note=None)
            return
        key = askable(uri)
        args.Cancel = True
        if key is None:
            self._outside(v, uri, user=bool(args.IsUserInitiated))
            return
        # Not shown until Joe allows it. A sign-in hop (an Okta app's SAML post) isn't a click either, so any page
        # asks; nothing opens without his Allow.
        with self._lock:
            self._asks[v.site] = {"key": key, "url": uri}
        v.state.update(loading=False)
        log.info("%s: asked before showing another site", self.NAME)

    def _on_new_window(self, v, args):
        uri = str(args.Uri)
        if not args.IsUserInitiated or askable(uri) is None:
            return super()._on_new_window(v, args)  # by itself: blocked; not https: the browser after a click
        args.Handled = True
        tab = None
        with self._lock:
            if len(self.tabs) < MAX_TABS:
                tab = "t" + secrets.token_hex(5)
                self.tabs[tab] = {"opener": self.tabs.get(v.site, {}).get("opener", v.site), "url": uri, "at": time.time()}
                self._opened = (tab, time.time())
                self.want = {**self.want, "site": tab}  # on show at once; the tab's page follows at its next check
        if tab is None:
            self._note(v, f"At most {MAX_TABS} tabs: close one first")
            return
        log.info("%s: a link opened a tab (%d open)", self.NAME, len(self.tabs))
        self._apply()

    def _outside(self, v, uri, new_window=False, user=True):
        scheme = uri.split(":", 1)[0].lower()
        if scheme == "com-okta-authenticator":
            source = str(v.controller.CoreWebView2.Source) if v.controller else ""
            if (urllib.parse.urlsplit(source).hostname or "").lower() not in OKTA_HOSTS:
                log.warning("%s: blocked Okta FastPass from a page that isn't Okta's", self.NAME)
                return
        if _SC_SCHEME.fullmatch(scheme):
            if not user or len(uri) > 4000 or not _scheme_registered(scheme):
                self._note(v, "Blocked the ScreenConnect link" + ("" if user else ": the page opened it by itself"))
                log.warning("%s: blocked a ScreenConnect link", self.NAME)
                return
            log.info("%s: handed a ScreenConnect link to Windows", self.NAME)
            threading.Thread(target=os.startfile, args=(uri,), daemon=True).start()
            return
        super()._outside(v, uri, new_window=new_window, user=user)

    def _open_target(self, site, src) -> str:
        if site in self.tabs:  # a tab: the page on show if it's a plain https page, else where it started
            return src if askable(src) else self.tabs[site]["url"]
        return super()._open_target(site, src)

    def _close_tab(self, tab):
        with self._lock:
            self.tabs.pop(tab, None)
            self._asks.pop(tab, None)
            if self._opened and self._opened[0] == tab:
                self._opened = None
        self._drop(tab)
        log.info("%s: closed a tab (%d open)", self.NAME, len(self.tabs))

    def _allow(self, site):
        """Allow pressed: the domain is remembered and the page opens. A tab still on its opener's pages (or on none)
        starts again from its first page, since an app's sign-in post can't be sent again; else the page itself."""
        with self._lock:
            ask = self._asks.pop(site, None)
        v = self.views.get(site)
        if not ask or v is None or v.controller is None:
            return
        tab = self.tabs.get(site)
        core = v.controller.CoreWebView2
        src = str(core.Source)
        opener = find(tab["opener"]) if tab else None
        fresh = tab is not None and (src in ("", "about:blank") or (opener is not None and page_allowed(opener, src)))
        core.Navigate(tab["url"] if fresh else ask["url"])

    def _apply(self):
        super()._apply()
        locked = not self.app.unlocked or self.app.lock_showing
        if locked == self._muted:
            return
        self._muted = locked
        for v in self.views.values():
            if v.controller is not None:
                v.controller.CoreWebView2.IsMuted = locked
        log.info("%s: sound %s", self.NAME, "off (locked)" if locked else "back on")

    def drop(self, site):
        """The site was removed or its address changed: closes its page (a video stops with it), and its tabs."""
        def run():
            for tab in [t for t, d in list(self.tabs.items()) if d["opener"] == site]:
                self._close_tab(tab)
            self._drop(site)
        self._invoke(run)

    def _drop(self, site):
        with self._lock:
            v = self.views.pop(site, None)
        if v is None:
            return
        self._set_visible(self._form(), v, False)
        if v.controller is not None:
            v.controller.Close()
        v.panel.Dispose()

    # ---- the tab's buttons: close a tab; Allow / Open in browser / Not now for a page that asked; Sign in ----
    def handle_action(self, body: dict) -> dict:
        name, site = body.get("action"), str(body.get("site") or "")
        if name == "close":
            if site not in self.tabs:
                raise ValueError("That tab is already closed.")
            self._invoke(lambda: self._close_tab(site))
            return {"ok": True}
        if name in ("allow", "outside", "dismiss"):
            with self._lock:
                ask = self._asks.get(site)
            if not ask:
                raise ValueError("Nothing is waiting there any more.")
            if name == "allow":
                if body.get("domain") != ask["key"]:
                    raise ValueError("That page changed. Look again before allowing it.")
                add_allowed(ask["key"])
                self._invoke(lambda: self._allow(site))
            else:
                with self._lock:
                    self._asks.pop(site, None)
                if name == "outside":  # Joe's click; an https page on the normal port (askable checked it)
                    threading.Thread(target=webbrowser.open, args=(ask["url"],), daemon=True).start()
                    log.info("%s: opened another site in the browser", self.NAME)
            return {"ok": True}
        if name != "signin":
            return super().handle_action(body)
        site = str(body.get("site") or "")
        if not find(site):
            raise ValueError("That site is no longer there.")
        login = get_login(site)  # here, not on the UI thread: Credential Manager is a call out of the process
        if not login:
            raise ValueError("No login is saved for this site. Add one with Edit.")
        self._invoke(lambda: self._fill(site, login))
        return {"ok": True, "site": site}

    def _fill(self, site, login):
        v, s = self.views.get(site), find(site)
        with self._lock:
            on_show = self.want["visible"] and self.want["site"] == site
        if v is None or s is None or v.controller is None or v.failed or not on_show:
            return
        core = v.controller.CoreWebView2
        host = (urllib.parse.urlsplit(str(core.Source)).hostname or "").lower()
        if not str(core.Source).startswith("https://") or host not in fill_hosts(s):
            self._note(v, f"Not filled: {host[:60]} isn't one of this site's saved addresses. If you trust it, add it with Edit.")
            log.warning("%s: refused to fill a login on an address not saved for the site", self.NAME)
            return
        from System import Action, String
        from System.Threading.Tasks import Task

        def done(task):
            if task.IsFaulted or task.IsCanceled:
                self._note(v, "Couldn't fill the page.")
                log.warning("%s: filling a login failed", self.NAME)
                return
            try:
                kind = json.loads(str(task.Result))
            except ValueError:
                kind = "none"
            self._note(v, FILLED.get(kind, FILLED["none"]))
            log.info("%s: sign-in fill: %s", self.NAME, kind if kind in FILLED else "other")
        script = FILL_JS % (json.dumps(host), json.dumps(login.get("u") or ""), json.dumps(login.get("p") or ""))
        core.ExecuteScriptAsync(script).ContinueWith(Action[Task[String]](done), self._scheduler)
