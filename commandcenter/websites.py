"""Sites added in the Websites tab: a name, an address and, if the site needs one, a login. Each shows inside the tab in
a WebView2 of its own (WebsiteView, the same isolation as the Pulse and Teams views; profile data/webview-websites).
The value monitors in config.yaml are separate (connectors/web.py).

  * The list (name, address, other addresses) is sealed work data: kv websites:sites, kept until removed.
  * The username and password live only in Windows Credential Manager (CommandCenter-Websites). The password never comes
    back to the page, and logs carry counts and types only.
  * Sign in fills the login form of the page on show, only on Joe's click and only on the site's own address or one he
    listed for it. It's the one script any site view ever runs, and it doesn't press the site's button: he does.
Addresses are https only. A page on another address opens in the default browser, after a click, as in the other views."""
import json
import logging
import re
import secrets
import threading
import urllib.parse

import keyring
import keyring.errors
from fastapi import APIRouter, Body, HTTPException

from . import db, security
from .siteview import SiteView, _host_ok, _https_on

log = logging.getLogger(__name__)
router = APIRouter()

KV_SITES = "websites:sites"
KEYRING_SERVICE = "CommandCenter-Websites"
MAX_SITES = 30
MAX_OTHER_HOSTS = 20
SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu"}  # example.co.uk: the domain is three labels
_lock = threading.Lock()
_sites: list[dict] | None = None  # read on every page load in the view, so kept in memory


def sites() -> list[dict]:
    global _sites
    if _sites is None:
        _sites = [s for s in db.kv_get(KV_SITES, []) or [] if isinstance(s, dict) and s.get("id") and s.get("url")]
    return _sites


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
    domain = _domain(host_of(site))
    return _https_on(url, {host}) and (host in fill_hosts(site) or host == domain or host.endswith("." + domain))


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
    """The sites added in the Websites tab, each keeping its own page. Nothing is handed to other apps. A video keeps
    playing when another tab is opened, as in a browser tab; Lock mutes it."""
    NAME = "Websites view"
    PROFILE = "webview-websites"
    OUT_SCHEMES = ()
    _muted = False

    @property
    def SITES(self):
        return tuple(s["id"] for s in sites()) or ("none",)

    def sites(self):
        return {s["id"]: s["url"] for s in sites()}

    def allowed(self, url):
        return any(page_allowed(s, url) for s in sites())

    def site_of(self, url):
        return next((s["id"] for s in sites() if _https_on(url, {host_of(s)})), None)

    def _showing(self, want):
        return super()._showing(want) and find(want["site"]) is not None

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
        """The site was removed or its address changed: closes its page (a video stops with it)."""
        self._invoke(lambda: self._drop(site))

    def _drop(self, site):
        with self._lock:
            v = self.views.pop(site, None)
        if v is None:
            return
        self._set_visible(self._form(), v, False)
        if v.controller is not None:
            v.controller.Close()
        v.panel.Dispose()

    # ---- Sign in ----
    def handle_action(self, body: dict) -> dict:
        if body.get("action") != "signin":
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
