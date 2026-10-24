"""Asana connector using a Personal Access Token, which is stored in Windows Credential Manager.

Create a token: Asana > profile picture > Settings > Apps > Developer apps > Create new token.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

import keyring
import keyring.errors

from .. import config, db

API = "https://app.asana.com/api/1.0"
KEYRING_SERVICE = "CommandCenter-Asana"


def _token():
    return keyring.get_password(KEYRING_SERVICE, "token")


def connected() -> bool:
    return config.section("asana", {}).get("enabled", True) and _token() is not None


def _request(method, path, params=None, data=None, token=None):
    token = token or _token()
    if not token:
        raise ValueError("Asana isn't connected. Add your token on the Today tab.")
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    body = json.dumps({"data": data}).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r).get("data")
    except urllib.error.HTTPError as e:
        try:
            detail = json.load(e)["errors"][0]["message"]
        except Exception:
            detail = e.reason
        raise ValueError(f"Asana error {e.code}: {detail}") from None


def set_token(token: str) -> str:
    """Checks the token against Asana, saves it, and returns the account name."""
    me = _request("GET", "/users/me", {"opt_fields": "name,workspaces.name"}, token=token.strip())
    keyring.set_password(KEYRING_SERVICE, "token", token.strip())
    db.kv_set("asana:me", me)
    return me["name"]


def disconnect():
    try:
        keyring.delete_password(KEYRING_SERVICE, "token")
    except keyring.errors.PasswordDeleteError:
        pass
    db.kv_set("asana:me", None)


def _workspace() -> str:
    wanted = str(config.section("asana", {}).get("workspace") or "").strip()
    me = db.kv_get("asana:me") or _request("GET", "/users/me", {"opt_fields": "name,workspaces.name"})
    db.kv_set("asana:me", me)
    spaces = me.get("workspaces") or []
    if not spaces:
        raise ValueError("No Asana workspaces found for this account")
    for w in spaces:
        if wanted and wanted in (w["gid"], w["name"]):
            return w["gid"]
    return spaces[0]["gid"]


def my_tasks() -> list[dict]:
    rows = _request("GET", "/tasks", {
        "assignee": "me", "workspace": _workspace(), "completed_since": "now", "limit": 100,
        "opt_fields": "name,due_on,due_at,permalink_url,projects.name",
    }) or []
    return [{
        "source": "asana", "id": t["gid"], "subject": t["name"] or "(untitled)",
        "due": t.get("due_on") or (t.get("due_at") or "")[:10] or None,
        "url": t.get("permalink_url"),
        "project": ", ".join(p["name"] for p in t.get("projects") or []),
    } for t in rows if t.get("name")]


def create_task(name, due=None, notes="") -> str:
    data = {"name": name, "notes": notes, "assignee": "me", "workspace": _workspace()}
    if due:
        data["due_on"] = due
    return _request("POST", "/tasks", data=data)["gid"]


def complete(task_id):
    _request("PUT", f"/tasks/{urllib.parse.quote(task_id)}", data={"completed": True})
