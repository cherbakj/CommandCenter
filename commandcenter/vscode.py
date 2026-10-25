"""VS Code tab: the real VS Code UI (with the Claude Code extension) in the browser, served from this PC.

How it runs
- VS Code's local web mode is the "VS Code Server" build (the thing `code serve-web` downloads and wraps).
  We run that build directly: its bundled node.exe + out/server-main.js, with list arguments and no console
  window, so no cmd.exe/batch file is ever involved. `code serve-web` itself was tried first and does not
  work on this PC: its server download fails silently (it re-downloads forever) and, with the build
  pre-installed, launching code-server.cmd fails ("The system cannot find the path specified") and the CLI
  panics. It also writes the connection token to <cli-data-dir>/serve-web-token in plain text, and its
  wrapper sends X-Frame-Options: SAMEORIGIN, which stops the dashboard from embedding it.
- The build matching the installed desktop VS Code (same commit) is downloaded once from Microsoft's
  update service over HTTPS, checked against the SHA-256 the service publishes, and kept in
  data/vscode/builds/<commit>. All server state (extensions, settings, logs) lives in data/vscode/server,
  separate from the desktop VS Code profile.
- The server starts only when asked (POST /api/vscode/start), listens on 127.0.0.1 on a random free port,
  and stops on request, when Command Center shuts down, or (Job object) whenever Command Center dies.

Mirror of desktop VS Code (one way, before every start; "Sync from desktop VS Code" restarts to re-run it)
- Extensions: every extension desktop VS Code lists in ~/.vscode/extensions/extensions.json is copied (same
  version, same PC so the win32-x64 builds match) into data/vscode/server/extensions and registered in its
  extensions.json. UI-only extensions (they can't run in the server's extension host) are skipped. Extensions
  installed some other way are never removed; ones we copied are removed when desktop no longer has them.
- Settings: desktop settings.json (minus Settings Sync keys) goes into the server's Machine settings, which the
  page applies at load for every scope except application ones. The helper extension below applies those few.
- Keybindings and snippets: contributed by a small helper extension Command Center writes itself
  (commandcenter.cc-helper, no network, reads only files in data/). It also shows the Claude Code panel when
  the window starts and when the tab's Claude button is pressed.
- Desktop files are only read, never changed. Setting values are never logged (they can hold connection
  strings); only counts are.
- Integrity: copies come only from real folders (no links) directly inside ~/.vscode/extensions whose
  package.json matches the id and version desktop VS Code recorded when it installed them from the Marketplace
  (it checks their signatures then). The Authenticode check below covers the server build only; extension
  binaries (C# .NET DLLs, debugpy...) aren't re-checked, so they're trusted exactly as much as desktop VS Code
  trusts them.

Security
- A fresh random connection token (secrets.token_urlsafe(32)) per launch, held only in memory. It is handed
  to the server through a one-shot named pipe (current user only, local only, and only the server process
  we just started may read it), so it is never on a command line or in a file. Every line of server output
  is scrubbed of it before being kept or logged. The page gets it only from GET /api/vscode/url.
- Requests without the token are rejected by the server (403).
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import tarfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from collections import deque
from pathlib import Path

import pywintypes
import win32api
import win32con
import win32file
import win32job
import win32pipe
import win32security
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from . import config, projects

log = logging.getLogger(__name__)
router = APIRouter()

NO_WINDOW = subprocess.CREATE_NO_WINDOW
UPDATE_API = "https://update.code.visualstudio.com/api"
PLATFORM = "server-win32-x64-web"
PIPE_REJECT_REMOTE_CLIENTS = 0x8
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
DEFAULT_EXTENSIONS = ["anthropic.claude-code"]
HELPER_ID = "commandcenter.cc-helper"
HELPER_DIR = "commandcenter.cc-helper-1.0.0"
EXT_ID = re.compile(r"[A-Za-z0-9][\w-]*\.[A-Za-z0-9][\w-]*")


def settings() -> dict:
    return config.section("vscode", {})


def home() -> Path:
    return config.DATA_DIR / "vscode"


def builds_dir() -> Path:
    return home() / "builds"


def server_data_dir() -> Path:
    return home() / "server"


# ---- state -------------------------------------------------------------------

class _State:
    def __init__(self):
        self.lock = threading.RLock()
        self.status = "stopped"       # stopped | downloading | installing | starting | ready | error
        self.detail = ""
        self.progress = None          # download percent
        self.error = ""
        self.port = None
        self.commit = None
        self.version = None
        self.proc: subprocess.Popen | None = None
        self.job = None
        self.token: str | None = None
        self.lines = deque(maxlen=60)
        self.worker: threading.Thread | None = None
        self.stopping = False
        self.extensions: list[str] = []
        self.mirror: dict = {}        # what the last desktop mirror did (counts and skipped ids, no values)


S = _State()


def _scrub(text: str) -> str:
    token = S.token
    if token and token in text:
        text = text.replace(token, "***")
    return re.sub(r"(tkn=|connection-token[= ])[A-Za-z0-9_\-]{16,}", r"\1***", text)


def _note(line: str):
    line = _scrub(line.rstrip())
    if line.strip():
        S.lines.append(line[:400])


def _set(status, detail="", **kw):
    with S.lock:
        S.status, S.detail = status, detail
        for k, v in kw.items():
            setattr(S, k, v)
    if detail:
        _note(f"[{status}] {detail}")


# ---- finding / downloading the server build -------------------------------------

def _desktop_install() -> Path | None:
    candidates = [settings().get("install_dir"),
                  os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code"),
                  os.path.expandvars(r"%ProgramFiles%\Microsoft VS Code")]
    for c in candidates:
        if c and (Path(c) / "Code.exe").exists():
            return Path(c)
    return None


def _desktop_commit() -> str | None:
    """Commit of the installed desktop VS Code, so the web version matches it."""
    root = _desktop_install()
    if not root:
        return None
    found = list(root.glob("*/resources/app/product.json")) + list(root.glob("resources/app/product.json"))
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for p in found:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if d.get("quality") == "stable" and re.fullmatch(r"[0-9a-f]{40}", d.get("commit", "")):
                return d["commit"]
        except (OSError, ValueError):
            continue
    return None


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _release_info(platform=PLATFORM, commit=None) -> dict:
    commit = commit or settings().get("commit") or _desktop_commit()
    url = f"{UPDATE_API}/versions/commit:{commit}/{platform}/stable" if commit else f"{UPDATE_API}/latest/{platform}/stable"
    info = _get_json(url)
    if not re.fullmatch(r"[0-9a-f]{40}", info.get("version", "")) or not re.fullmatch(r"[0-9a-f]{64}", info.get("sha256hash", "")):
        raise RuntimeError("Unexpected answer from the VS Code update service")
    if not info.get("url", "").startswith("https://"):
        raise RuntimeError("The VS Code download link isn't HTTPS")
    return info


def _download(info: dict, dest: Path, label: str) -> Path:
    """Downloads info["url"] to dest and checks it against the SHA-256 the update service publishes."""
    _set("downloading", label, progress=0)
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(info["url"], timeout=60) as r, open(dest, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while chunk := r.read(1 << 20):
                if S.stopping:
                    raise RuntimeError("Cancelled")
                f.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if total:
                    S.progress = round(done * 100 / total)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    if digest.hexdigest() != info["sha256hash"]:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"{dest.name} failed its SHA-256 check; nothing was installed")
    S.progress = None
    return dest


def _overlay_web_bundles(commit: str, target: Path):
    """Adds the built-in extensions' browser bundles, which this server build leaves out.

    The web page has its own list of built-in extensions that run in the browser (Git base, GitHub sign-in,
    JSON/TypeScript/HTML/CSS language features, Emmet, merge conflicts...) and loads their code from the
    server, but server-win32-x64-web doesn't ship those files (404), so Git and IntelliSense don't start.
    Microsoft's matching "vscode-web" build (same commit) has them; only files missing here are copied in.
    """
    try:
        info = _release_info("web-standalone", commit)
        if info["version"] != commit:
            raise RuntimeError("the web build doesn't match the server's commit")
        tgz = _download(info, builds_dir() / f"{commit}-web.tar.gz", "Downloading the built-in extensions' browser files (about 55 MB)")
        _set("downloading", "Adding the built-in extensions' browser files", progress=None)
        root = target.resolve()
        added = 0
        with tarfile.open(tgz, "r:gz") as t:
            for m in t:
                parts = m.name.split("/")
                if not m.isfile() or len(parts) < 4 or parts[0] != "vscode-web" or parts[1] != "extensions":
                    continue
                if not (target / "extensions" / parts[2]).is_dir():
                    continue  # only extensions the server build has
                dest = (target / "/".join(parts[1:])).resolve()
                if not dest.is_relative_to(root) or dest == root or dest.exists():
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with t.extractfile(m) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                added += 1
        tgz.unlink(missing_ok=True)
        _note(f"Added {added} browser files for built-in extensions")
    except Exception as e:
        _note(f"Couldn't add the built-in extensions' browser files ({e}); Git and some language features won't start")


def _installed_build() -> tuple[str, Path] | None:
    """Newest complete build already on disk (used when the update service can't be reached)."""
    if not builds_dir().is_dir():
        return None
    done = [p for p in builds_dir().iterdir() if (p / ".cc-complete").exists() and (p / "node.exe").exists()]
    done.sort(key=lambda p: (p / ".cc-complete").stat().st_mtime, reverse=True)
    return (done[0].name, done[0]) if done else None


def _ensure_build() -> Path:
    try:
        info = _release_info()
    except Exception as e:
        have = _installed_build()
        if have:
            _note(f"Update service unreachable ({e}); using the build already downloaded")
            S.commit = have[0]
            return have[1]
        raise RuntimeError(f"Couldn't reach the VS Code update service to download the web server: {e}")
    commit = info["version"]
    S.commit, S.version = commit, info.get("productVersion") or info.get("name")
    target = builds_dir() / commit
    if (target / ".cc-complete").exists() and (target / "node.exe").exists():
        return target
    builds_dir().mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    archive = _download(info, builds_dir() / f"{commit}.zip", f"Downloading VS Code {S.version} web server (one time, about 200 MB)")
    _set("downloading", "Unpacking the VS Code web server", progress=None)
    with zipfile.ZipFile(archive) as z:
        # Extract in place (renaming a freshly unpacked folder is refused here, likely the virus scanner).
        root = target.resolve()
        for item in z.infolist():
            name = item.filename.split("/", 1)[1] if "/" in item.filename else ""
            if not name:
                continue
            dest = (target / name).resolve()
            # Path containment, not a string prefix: C:\...\<commit>-evil also starts with C:\...\<commit>.
            if not dest.is_relative_to(root) or dest == root:
                raise RuntimeError("Unsafe path in the VS Code server archive")
            if item.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(item) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
    archive.unlink(missing_ok=True)
    _overlay_web_bundles(commit, target)
    _set("installing", "Checking the Microsoft signatures of the VS Code server's programs")
    verify_build_signatures(target)  # raises (and the build is never marked complete) if any program isn't signed
    (target / ".cc-complete").write_text(commit, encoding="utf-8")
    for old in builds_dir().iterdir():  # keep only the current build
        if old != target and old.is_dir() and re.fullmatch(r"[0-9a-f]{40}", old.name):
            shutil.rmtree(old, ignore_errors=True)
    return target


# ---- Authenticode check of the build's programs ----------------------------------------------
# The download is checked against Microsoft's published SHA-256, but that only proves the archive matches what
# the update service said. Before anything in a build runs, every program in it (node.exe and the .exe/.node/.dll
# files node loads) must carry a valid Authenticode signature from Microsoft (a few bundled GitHub tools are
# signed by GitHub, Inc., a Microsoft company). Checked after every download and once per launch for a build
# already on disk; results are cached per file (path, size, modified time) so a restart in the same launch is fast.

SIGNED_TYPES = (".exe", ".node", ".dll")
TRUSTED_SIGNERS = {"Microsoft Corporation", "GitHub, Inc."}
_sig_cache: dict[tuple[str, int, int], bool] = {}
_SIG_SCRIPT = ("$ErrorActionPreference='Stop'; $env:CC_SIG_FILES -split \"`n\" | Where-Object { $_ } | ForEach-Object { "
               "$s = Get-AuthenticodeSignature -LiteralPath $_; "
               "[pscustomobject]@{ Path = $_; Status = [string]$s.Status; "
               "Subject = $(if ($s.SignerCertificate) { $s.SignerCertificate.Subject } else { '' }) } } | ConvertTo-Json -Compress")


def _signer_org(subject: str) -> str:
    """O= of a certificate subject such as 'CN=Microsoft Corporation, O=Microsoft Corporation, L=Redmond'."""
    m = re.search(r'(?:^|,\s*)O=("(?:[^"]|"")*"|[^,]*)', subject or "")
    if not m:
        return ""
    value = m.group(1).strip()
    return value[1:-1].replace('""', '"') if value.startswith('"') else value


def _check_signatures(files: list[Path]) -> dict[str, tuple[str, str]]:
    """{path: (status, signer subject)} from Windows' own Authenticode check. Inline PowerShell (no .ps1 file),
    list arguments, no console window; the paths go in through an environment variable, never the command text."""
    ps = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    found = {}
    chunks, chunk, size = [], [], 0
    for f in files:  # environment variables are limited to 32767 characters
        if chunk and size + len(str(f)) > 20000:
            chunks.append(chunk)
            chunk, size = [], 0
        chunk.append(str(f))
        size += len(str(f)) + 1
    if chunk:
        chunks.append(chunk)
    for chunk in chunks:
        env = {**os.environ, "CC_SIG_FILES": "\n".join(chunk)}
        out = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", _SIG_SCRIPT],
                             capture_output=True, text=True, timeout=300, creationflags=NO_WINDOW, env=env,
                             stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace")
        if out.returncode != 0 or not out.stdout.strip():
            raise RuntimeError(f"Couldn't check the VS Code server's signatures: {(out.stderr or out.stdout).strip()[:200]}")
        rows = json.loads(out.stdout)
        for r in rows if isinstance(rows, list) else [rows]:
            found[os.path.normcase(r["Path"])] = (r.get("Status") or "", r.get("Subject") or "")
    return found


def verify_build_signatures(build: Path):
    """Raises RuntimeError unless node.exe and every .exe/.node/.dll in the build is validly signed by Microsoft
    (node.exe) or a trusted signer (the rest)."""
    node = build / "node.exe"
    if not node.is_file():
        raise RuntimeError("The VS Code server build has no node.exe")
    files = sorted({node, *(f for f in build.rglob("*") if f.suffix.lower() in SIGNED_TYPES and f.is_file())})
    keys = {}
    for f in files:
        st = f.stat()
        keys[f] = (os.path.normcase(str(f)), st.st_size, st.st_mtime_ns)
    todo = [f for f in files if not _sig_cache.get(keys[f])]
    if todo:
        results = _check_signatures(todo)
        bad = []
        for f in todo:
            status, subject = results.get(os.path.normcase(str(f)), ("Missing", ""))
            org = _signer_org(subject)
            ok = status == "Valid" and (org == "Microsoft Corporation" if f == node else org in TRUSTED_SIGNERS)
            if ok:
                _sig_cache[keys[f]] = True
            else:
                bad.append(f"{f.relative_to(build)} ({status or 'no signature'}{', ' + org if org else ''})")
        if bad:
            log.warning("VS Code server build %s failed the signature check: %s", build.name, "; ".join(bad[:10]))
            raise RuntimeError("The VS Code server's programs aren't all signed by Microsoft, so it won't be started: "
                               + "; ".join(bad[:5]) + (" ..." if len(bad) > 5 else ""))
    _note(f"Signatures checked: {len(files)} programs signed by Microsoft")


def _seed_machine_settings():
    """First run only: dark theme to match the dashboard. Change it in VS Code (Settings > Remote) any time."""
    f = server_data_dir() / "data" / "Machine" / "settings.json"
    if not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"workbench.colorTheme": "Default Dark Modern"}, indent=2), encoding="utf-8")


# ---- extensions -------------------------------------------------------------------

def _server_cmd(build: Path, *args) -> list[str]:
    return [str(build / "node.exe"), str(build / "out" / "server-main.js"),
            "--server-data-dir", str(server_data_dir()), "--accept-server-license-terms", *args]


def _installed_extensions() -> list[str]:
    ext = server_data_dir() / "extensions"
    return sorted(p.name for p in ext.iterdir() if p.is_dir() and not p.name.startswith(".")) if ext.is_dir() else []


def _has_extension(ext_id: str) -> bool:
    return any(n.lower().startswith(ext_id.lower() + "-") for n in _installed_extensions())


def _run_cli(build: Path, *args, timeout=300) -> str:
    out = subprocess.run(_server_cmd(build, *args), capture_output=True, text=True, timeout=timeout,
                         creationflags=NO_WINDOW, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
    text = (out.stdout or "") + (out.stderr or "")
    for line in text.splitlines():
        if "DeprecationWarning" not in line and "trace-deprecation" not in line:
            _note(line)
    if out.returncode != 0:
        raise RuntimeError(text.strip().splitlines()[-1] if text.strip() else f"exit code {out.returncode}")
    return text


def _desktop_extension_dir(ext_id: str) -> Path | None:
    root = _desktop_extensions_root()
    found = [p for p in root.glob(f"{ext_id}-*") if (p / "package.json").exists() and (p / ".vsixmanifest").exists()]
    key = lambda p: [int(x) if x.isdigit() else 0 for x in re.findall(r"\d+|\D+", p.name)]
    return sorted(found, key=key)[-1] if found else None


def _vsix_from_desktop(ext_dir: Path) -> Path:
    """Offline fallback: repackage the desktop copy of an extension as a .vsix (the format VS Code installs)."""
    tmp = home() / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    vsix = tmp / f"{ext_dir.name}.vsix"
    types = ('<?xml version="1.0" encoding="utf-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
             '<Default Extension=".json" ContentType="application/json"/><Default Extension=".vsixmanifest" ContentType="text/xml"/>'
             '<Default Extension=".js" ContentType="application/javascript"/><Default Extension=".md" ContentType="text/markdown"/>'
             '<Default Extension=".png" ContentType="image/png"/><Default Extension=".svg" ContentType="image/svg+xml"/>'
             '<Default Extension=".css" ContentType="text/css"/><Default Extension=".exe" ContentType="application/octet-stream"/>'
             '<Default Extension=".txt" ContentType="text/plain"/></Types>')
    with zipfile.ZipFile(vsix, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", types)
        z.write(ext_dir / ".vsixmanifest", "extension.vsixmanifest")
        for f in ext_dir.rglob("*"):
            if f.is_file() and f.name != ".vsixmanifest":
                z.write(f, "extension/" + f.relative_to(ext_dir).as_posix())
    return vsix


def _ensure_extensions(build: Path):
    """config vscode.extensions (default: Claude Code) when the desktop mirror didn't bring them in."""
    wanted = settings().get("extensions", DEFAULT_EXTENSIONS) or []
    for ext_id in wanted:
        if not EXT_ID.fullmatch(str(ext_id)) or _has_extension(ext_id):
            continue
        _set("installing", f"Installing {ext_id} into web VS Code")
        try:
            _run_cli(build, "--install-extension", ext_id)  # from the Marketplace: signed, right platform build
        except Exception as e:
            src = _desktop_extension_dir(ext_id)
            if not src:
                _note(f"Couldn't install {ext_id}: {e}")
                continue
            _note(f"Marketplace install failed ({e}); installing your desktop copy {src.name}")
            vsix = _vsix_from_desktop(src)
            try:
                _run_cli(build, "--install-extension", str(vsix))
            except Exception as e2:
                _note(f"Couldn't install {ext_id}: {e2}")
            finally:
                vsix.unlink(missing_ok=True)
    S.extensions = _installed_extensions()


# ---- mirror of desktop VS Code (see the module docstring) -----------------------------------------

SKIP_SETTINGS = re.compile(r"(sync|settingsSync)\.")   # Settings Sync's own keys and state stay on desktop
# Settings (or fields inside a setting's value) that may hold a credential stay on desktop: the copies are plain files
# under data/vscode and in the page's own storage, outside Credential Manager.
SECRET_SETTING = re.compile(r"password|passwd|token|secret|api_?key|authorization|credential|environmentvariables|cookie",
                            re.I)


def _without_secrets(value):
    if isinstance(value, dict):
        return {k: _without_secrets(v) for k, v in value.items() if not SECRET_SETTING.search(str(k))}
    if isinstance(value, list):
        return [_without_secrets(v) for v in value]
    return value
SNIPPET_FILE = re.compile(r"[\w.-]{1,80}\.(json|code-snippets)", re.I)


def _desktop_extensions_root() -> Path:
    return Path.home() / ".vscode" / "extensions"


def _desktop_user_dir() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Code" / "User"


def _mirror_state_file() -> Path:
    return server_data_dir() / "cc-mirror.json"


def _machine_settings_file() -> Path:
    return server_data_dir() / "data" / "Machine" / "settings.json"


def _jsonc(text: str):
    """Parses VS Code's JSON with comments and trailing commas (settings.json, keybindings.json)."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            if c in "}]":  # drop a trailing comma (strings are whole chunks, so this is never inside one)
                k = len(out) - 1
                while k >= 0 and out[k].isspace():
                    k -= 1
                if k >= 0 and out[k] == ",":
                    del out[k]
            out.append(c)
            i += 1
    return json.loads("".join(out))


def _read_jsonc(path: Path):
    """The file's value; None if it's missing or empty. Raises ValueError (never quoting the file) if unreadable."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ValueError(f"{path.name} couldn't be read ({e.strerror})") from None
    if not text.strip():
        return None
    try:
        return _jsonc(text)
    except ValueError:
        raise ValueError(f"{path.name} isn't valid JSON") from None


def _write_if_changed(path: Path, text: str) -> bool:
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".cc-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return True


def _version_key(v) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", str(v or "").split("-")[0])[:4])


def _is_link(p: Path) -> bool:
    return p.is_symlink() or p.is_junction()


def _location(folder: Path) -> dict:
    """A file URI the way VS Code stores it in extensions.json."""
    p = str(folder)
    drive, rest = p[0].lower(), p[2:].replace("\\", "/")
    return {"$mid": 1, "fsPath": f"{drive}:{p[2:]}", "_sep": 1,
            "external": "file:///" + urllib.parse.quote(f"{drive}:{rest}", safe="/"),
            "path": f"/{drive}:{rest}", "scheme": "file"}


def _default_dark_theme(build: Path) -> str:
    """Desktop VS Code's default theme, used when desktop settings don't name one (the web page defaults to light)."""
    try:
        themes = json.loads((build / "extensions" / "theme-defaults" / "package.json").read_text(encoding="utf-8"))
        ids = {t.get("id") or t.get("label") for t in themes["contributes"]["themes"]}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        ids = set()
    return next((t for t in ("Dark 2026", "Dark Modern") if t in ids), "Default Dark Modern")


def _mirror_settings(build: Path, state: dict, report: dict):
    """Desktop settings.json -> the server's Machine settings. Keys mirrored last time but gone from desktop are
    removed; keys only set in web VS Code (Settings > Remote) are kept."""
    desktop = _read_jsonc(_desktop_user_dir() / "settings.json") or {}
    if not isinstance(desktop, dict):
        raise ValueError("desktop settings.json isn't an object")
    wanted = {k: _without_secrets(v) for k, v in desktop.items()
              if isinstance(k, str) and not SKIP_SETTINGS.match(k) and not SECRET_SETTING.search(k)}
    wanted.setdefault("workbench.colorTheme", _default_dark_theme(build))
    f = _machine_settings_file()
    current = _read_jsonc(f) or {}
    if not isinstance(current, dict):
        raise ValueError("the web server's Machine settings.json isn't an object")
    merged = {k: v for k, v in current.items() if k in wanted or k not in set(state.get("settings") or [])}
    merged.update(wanted)
    _write_if_changed(f, json.dumps(merged, indent=2, ensure_ascii=False) + "\n")
    state["settings"] = sorted(wanted)
    report["settings"] = len(wanted)


def _desktop_extensions(build: Path) -> tuple[list[dict], list[dict], set[str] | None]:
    """(copyable, skipped, every id) of the extensions desktop VS Code lists as installed. The ids are None when
    desktop's list is missing or empty (e.g. read while VS Code rewrites it), so no copy is removed because of it."""
    root = _desktop_extensions_root()
    entries = _read_jsonc(root / "extensions.json") or []
    try:
        product = json.loads((build / "product.json").read_text(encoding="utf-8"))
        product_kinds = {str(k).lower(): v for k, v in (product.get("extensionKind") or {}).items()}
    except (OSError, ValueError, AttributeError):
        product_kinds = {}
    usable, skipped, ids = [], [], set()
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict):
            continue
        ext_id = str((e.get("identifier") or {}).get("id") or "")
        version, rel = str(e.get("version") or ""), str(e.get("relativeLocation") or "")
        if not EXT_ID.fullmatch(ext_id) or ext_id.lower() == HELPER_ID:
            continue
        ids.add(ext_id.lower())

        def skip(reason):
            skipped.append({"id": ext_id, "reason": reason})

        src = root / rel
        if not re.fullmatch(r"[\w][\w.+-]*", rel) or src.parent != root or not src.is_dir() or _is_link(src):
            skip("its folder is missing, or isn't a plain folder in ~/.vscode/extensions")
            continue
        if (e.get("metadata") or {}).get("source") != "gallery":
            skip("not installed from the Marketplace (e.g. a .vsix file): install it in web VS Code yourself if wanted")
            continue
        try:
            pkg = json.loads((src / "package.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            skip("its package.json can't be read")
            continue
        if f"{pkg.get('publisher')}.{pkg.get('name')}".lower() != ext_id.lower() or str(pkg.get("version")) != version:
            skip("its package.json doesn't match what desktop VS Code installed")
            continue
        kind = product_kinds.get(ext_id.lower(), pkg.get("extensionKind"))
        kinds = [kind] if isinstance(kind, str) else list(kind or [])
        if kinds and "workspace" not in kinds:
            skip("UI-only extension: it runs in desktop VS Code's own window, not on a VS Code server")
            continue
        usable.append({"id": ext_id, "version": version, "rel": rel, "src": src, "entry": e})
    return usable, skipped, ids or None


def _copy_extension(src: Path, dest: Path):
    def links(folder, names):  # never follow a link out of the extension's folder
        return [n for n in names if _is_link(Path(folder) / n)]
    shutil.copytree(src, dest, ignore=links)


def _mark_obsolete(names: set[str], obsolete: set[str]):
    """Updates the server's .obsolete list (folders it deletes when it starts)."""
    f = server_data_dir() / "extensions" / ".obsolete"
    try:
        current = json.loads(f.read_text(encoding="utf-8"))
        current = current if isinstance(current, dict) else {}
    except (OSError, ValueError):
        current = {}
    new = {k: v for k, v in current.items() if k not in names} | {n: True for n in obsolete}
    if new != current:
        _write_if_changed(f, json.dumps(new))


def _ext_folder(ext_dir: Path, rel) -> Path | None:
    """A folder directly inside ext_dir named by a relativeLocation from the server's extensions.json (which web VS
    Code and its extensions can write), or None: an absolute path or one with .. never reaches rmtree."""
    rel = str(rel or "")
    if not re.fullmatch(r"[\w][\w.+-]*", rel):
        return None
    folder = ext_dir / rel
    return folder if folder.resolve().parent == ext_dir.resolve() else None


def _remove_extension_folder(folder: Path | None, obsolete: set[str]):
    if folder is None or _is_link(folder):
        return
    shutil.rmtree(folder, ignore_errors=True)
    if folder.exists():  # something held a file open: the server removes it at its next start
        obsolete.add(folder.name)


def _mirror_extensions(build: Path, state: dict, report: dict) -> list[dict]:
    """Copies desktop extensions that web VS Code lacks (or has older), registers them, and drops copies of
    extensions desktop VS Code no longer has. Returns the server's extensions.json entries (not yet saved)."""
    ext_dir = server_data_dir() / "extensions"
    ext_dir.mkdir(parents=True, exist_ok=True)
    manifest = _read_jsonc(ext_dir / "extensions.json") or []  # unreadable: raises, and nothing is changed
    if not isinstance(manifest, list):
        raise ValueError("the web server's extensions.json isn't a list")
    manifest = [m for m in manifest if isinstance(m, dict) and isinstance(m.get("identifier"), dict)]
    by_id = {str(m["identifier"].get("id", "")).lower(): m for m in manifest}
    usable, skipped, desktop_ids = _desktop_extensions(build)
    ours = {k.lower(): v for k, v in (state.get("extensions") or {}).items()}   # id -> folder we copied
    keep = {str(x).lower() for x in DEFAULT_EXTENSIONS + list(settings().get("extensions") or [])}
    restored, obsolete = set(), set()

    todo = []
    for x in usable:
        cur = by_id.get(x["id"].lower())
        have = cur and _ext_folder(ext_dir, cur.get("relativeLocation"))
        if have and have.is_dir() and _version_key(cur.get("version")) >= _version_key(x["version"]):
            continue  # already there (the same version, or one web VS Code updated itself)
        todo.append((x, cur))
    for i, (x, cur) in enumerate(todo, 1):
        if S.stopping:
            raise RuntimeError("Cancelled")
        _set("installing", f"Copying {x['id']} {x['version']} from desktop VS Code ({i} of {len(todo)})")
        dest = ext_dir / x["rel"]
        if dest.exists():  # a copy that didn't finish (it was never registered)
            shutil.rmtree(dest)
        try:
            _copy_extension(x["src"], dest)
        except (OSError, shutil.Error) as e:
            shutil.rmtree(dest, ignore_errors=True)
            skipped.append({"id": x["id"], "reason": f"copying it failed ({type(e).__name__})"})
            continue
        if cur:  # replaces an older version
            manifest.remove(cur)
            old = str(cur.get("relativeLocation", ""))
            if old and old != x["rel"]:
                _remove_extension_folder(_ext_folder(ext_dir, old), obsolete)
        entry = json.loads(json.dumps(x["entry"]))
        entry["location"], entry["relativeLocation"] = _location(dest), x["rel"]
        entry["metadata"] = {**(entry.get("metadata") or {}), "installedTimestamp": int(time.time() * 1000)}
        manifest.append(entry)
        by_id[x["id"].lower()] = entry
        ours[x["id"].lower()] = x["rel"]
        restored.add(x["rel"])
        report["copied"] += 1

    for ext_id in list(ours) if desktop_ids is not None else []:  # copies of extensions removed from desktop VS Code
        if ext_id in desktop_ids or ext_id in keep:
            continue
        cur = by_id.pop(ext_id, None)
        if cur:
            manifest.remove(cur)
            if cur.get("relativeLocation"):
                _remove_extension_folder(_ext_folder(ext_dir, cur["relativeLocation"]), obsolete)
        del ours[ext_id]
        report["removed"] += 1

    _mark_obsolete(restored, obsolete)
    state["extensions"] = ours
    report["extensions"] = len(usable)
    report["skipped"] = skipped
    return manifest


HELPER_JS = r"""// Command Center's helper for its embedded VS Code. Written by Command Center (commandcenter/vscode.py) at every
// start. It reads only files Command Center wrote under data/vscode and takes no other input.
// - Shows the Claude Code panel (secondary side bar) when the window starts, and again once a folder is trusted.
// - Shows it again when Command Center's "Claude panel" button touches open-claude.signal next to this file.
// - Applies mirrored desktop settings the server's Machine settings can't carry (application scope ones).
"use strict";
const vscode = require("vscode");
const fs = require("fs");
const path = require("path");

function readJson(file) {
  try { return JSON.parse(fs.readFileSync(file, "utf8").replace(/^﻿/, "")); } catch { return null; }
}

function equal(a, b) {
  if (a === b) return true;
  if (!a || !b || typeof a !== "object" || typeof b !== "object" || Array.isArray(a) !== Array.isArray(b)) return false;
  const ka = Object.keys(a), kb = Object.keys(b);
  return ka.length === kb.length && ka.every((k) => Object.prototype.hasOwnProperty.call(b, k) && equal(a[k], b[k]));
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Claude Code is off in Restricted Mode; trusting the folder runs this again. Right after that (or at start) the
// extension and its view may not be registered yet, so this keeps trying for a while.
let showing = null;
function showClaude() {
  if (!vscode.workspace.isTrusted) return Promise.resolve();
  showing = showing || (async () => {
    try {
      for (let i = 0; i < 40; i++) {
        const ext = vscode.extensions.getExtension("anthropic.claude-code");
        if (ext) {
          try { await ext.activate(); } catch { return; }
          for (const view of ["claudeVSCodeSidebarSecondary.focus", "claudeVSCodeSidebar.focus"]) {
            try { await vscode.commands.executeCommand(view); return; } catch { /* that view isn't registered (yet) */ }
          }
        }
        await sleep(750);
      }
    } finally { showing = null; }
  })();
  return showing;
}

async function applyUserSettings(context) {
  const info = readJson(path.join(context.extensionPath, "mirror.json")) || {};
  const machine = (typeof info.settingsFile === "string" && readJson(info.settingsFile)) || {};
  const keys = (Array.isArray(info.keys) ? info.keys : []).filter((k) => typeof k === "string" && !k.startsWith("["));
  const cfg = vscode.workspace.getConfiguration();
  const applied = new Set(context.globalState.get("cc.appliedSettings", []));
  for (const key of keys) {
    if (!Object.prototype.hasOwnProperty.call(machine, key)) continue;
    const seen = cfg.inspect(key);
    log.debug(`${key}: ${seen ? "global " + (seen.globalValue === undefined ? "unset" : equal(seen.globalValue, machine[key]) ? "same" : "different") : "no inspect"}`);
    if (!seen || equal(seen.globalValue, machine[key])) continue;  // the Machine settings carry it already
    try {
      // An application-scope setting has no effect in the server's Machine settings. While it is there, VS Code
      // also writes "Global" values back into that file, so take it out first; the value then lands in the
      // page's own user settings. (Command Center puts it back in the Machine file at the next start: harmless.)
      await cfg.update(key, undefined, vscode.ConfigurationTarget.Global);
      await vscode.workspace.getConfiguration().update(key, machine[key], vscode.ConfigurationTarget.Global);
      applied.add(key);
      log.info(`Applied ${key} to user settings`);  // names only: values can hold connection strings
    } catch (e) { log.info(`Couldn't apply ${key}: ${String((e && e.message) || e).slice(0, 200)}`); }  // VS Code's messages name the key, not the value
  }
  for (const key of [...applied]) {  // removed from desktop VS Code
    if (keys.includes(key)) continue;
    try { await cfg.update(key, undefined, vscode.ConfigurationTarget.Global); } catch { /* gone */ }
    applied.delete(key);
    log.info(`Removed ${key} from user settings`);
  }
  await context.globalState.update("cc.appliedSettings", [...applied]);
}

function watchSignal(context) {
  const file = path.join(context.extensionPath, "open-claude.signal");
  const stamp = () => { try { return fs.statSync(file).mtimeMs; } catch { return 0; } };
  let last = stamp();
  const timer = setInterval(() => {
    const now = stamp();
    if (now && now !== last) { last = now; showClaude(); }
  }, 700);
  context.subscriptions.push({ dispose: () => clearInterval(timer) });
}

let log = { info() {}, debug() {} };

exports.activate = async (context) => {
  log = vscode.window.createOutputChannel("Command Center", { log: true });
  context.subscriptions.push(log);
  watchSignal(context);
  context.subscriptions.push(vscode.workspace.onDidGrantWorkspaceTrust(() => showClaude()));
  try { await applyUserSettings(context); } catch { /* keep going: the panel matters more */ }
  if (vscode.workspace.getConfiguration("commandCenter").get("showClaudeOnStart", true)) await showClaude();
};
exports.deactivate = () => {};
"""


def _helper_files(state: dict, report: dict) -> dict[str, str]:
    """The helper extension's files: desktop keybindings and snippets as contributions, plus the panel logic."""
    user = _desktop_user_dir()
    keybindings, removals = [], 0
    try:
        raw = _read_jsonc(user / "keybindings.json") or []
    except ValueError as e:
        raw = []
        report["errors"].append(str(e))
    for k in raw if isinstance(raw, list) else []:
        if not isinstance(k, dict) or not isinstance(k.get("key"), str) or not isinstance(k.get("command"), str):
            continue
        if k["command"].startswith("-"):  # removing a default binding can't be expressed by an extension
            removals += 1
            continue
        keybindings.append({f: k[f] for f in ("key", "command", "when", "args", "mac", "linux", "win") if f in k})
    files, snippets = {}, []
    folder = user / "snippets"
    for f in sorted(folder.iterdir()) if folder.is_dir() else []:
        if not SNIPPET_FILE.fullmatch(f.name) or not f.is_file() or _is_link(f):
            continue
        try:
            files[f"snippets/{f.name}"] = f.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        item = {"path": f"./snippets/{f.name}"}
        if f.suffix.lower() == ".json":
            item["language"] = f.stem  # <language>.json; .code-snippets files name their languages inside
        snippets.append(item)
    pkg = {
        "name": "cc-helper", "publisher": "commandcenter", "displayName": "Command Center helper", "version": "1.0.0",
        "description": "Written by Command Center: your desktop VS Code keybindings and snippets, and the Claude Code panel at start.",
        "engines": {"vscode": "^1.90.0"}, "main": "./extension.js", "extensionKind": ["workspace"],
        "activationEvents": ["onStartupFinished"],
        "capabilities": {"untrustedWorkspaces": {"supported": True}, "virtualWorkspaces": True},
        "contributes": {
            "configuration": {"title": "Command Center", "properties": {"commandCenter.showClaudeOnStart": {
                "type": "boolean", "default": True, "description": "Show the Claude Code panel when the window opens."}}},
            "keybindings": keybindings, "snippets": snippets,
        },
    }
    files["package.json"] = json.dumps(pkg, indent=2, ensure_ascii=False) + "\n"
    files["extension.js"] = HELPER_JS
    # Setting names only; the helper reads the values from the Machine settings file.
    files["mirror.json"] = json.dumps({"settingsFile": str(_machine_settings_file()), "keys": state.get("settings") or []}, indent=2) + "\n"
    report["keybindings"], report["keybinding_removals"], report["snippets"] = len(keybindings), removals, len(snippets)
    return files


def _write_helper(manifest: list[dict], state: dict, report: dict) -> bool:
    """Writes the helper extension and its extensions.json entry. True if anything changed."""
    folder = server_data_dir() / "extensions" / HELPER_DIR
    files = _helper_files(state, report)
    changed = False
    for name, text in files.items():
        changed |= _write_if_changed(folder / name, text)
    for old in (folder / "snippets").glob("*") if (folder / "snippets").is_dir() else []:
        if f"snippets/{old.name}" not in files:
            old.unlink(missing_ok=True)
            changed = True
    entry = next((m for m in manifest if str(m["identifier"].get("id", "")).lower() == HELPER_ID), None)
    if entry is None or entry.get("relativeLocation") != HELPER_DIR or changed:
        # A new timestamp also changes extensions.json, which is what makes the server re-read the extension.
        if entry is not None:
            manifest.remove(entry)
        manifest.append({"identifier": {"id": HELPER_ID}, "version": "1.0.0", "location": _location(folder),
                         "relativeLocation": HELPER_DIR,
                         "metadata": {"installedTimestamp": int(time.time() * 1000), "source": "vsix", "pinned": True}})
        changed = True
    return changed


def _mirror_desktop(build: Path):
    """Brings desktop VS Code's extensions, settings, keybindings and snippets in (one way). Never raises: the
    server starts with whatever it already has if something here fails."""
    report = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "copied": 0, "removed": 0, "extensions": 0, "skipped": [],
              "settings": 0, "keybindings": 0, "keybinding_removals": 0, "snippets": 0, "errors": []}
    try:
        state = json.loads(_mirror_state_file().read_text(encoding="utf-8"))
        state = state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        state = {}
    _set("installing", "Syncing settings from desktop VS Code")
    try:
        _mirror_settings(build, state, report)
    except ValueError as e:
        report["errors"].append(f"Settings not synced: {e}")
    try:
        manifest = _mirror_extensions(build, state, report)
        if _write_helper(manifest, state, report) or report["copied"] or report["removed"]:
            _write_if_changed(server_data_dir() / "extensions" / "extensions.json", json.dumps(manifest))
    except ValueError as e:
        report["errors"].append(f"Extensions not synced: {e}")
    except RuntimeError:
        raise  # cancelled with Stop
    except OSError as e:
        report["errors"].append(f"Extensions not synced: {type(e).__name__} {e.strerror or ''}".strip())
    _write_if_changed(_mirror_state_file(), json.dumps(state, indent=2) + "\n")
    S.mirror = report
    _note(f"Synced from desktop VS Code: {report['extensions']} extensions ({report['copied']} copied, {report['removed']} removed), "
          f"{report['settings']} settings, {report['keybindings']} keybindings, {report['snippets']} snippet files")
    for s in report["skipped"]:
        _note(f"Not synced: {s['id']} ({s['reason']})")
    for e in report["errors"]:
        _note(e)


# ---- token pipe + process ---------------------------------------------------------

def _pipe_security():
    """Security attributes for the token pipe: only this Windows user may open it."""
    user = win32security.GetTokenInformation(
        win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY), win32security.TokenUser)[0]
    dacl = win32security.ACL()
    dacl.AddAccessAllowedAce(win32security.ACL_REVISION, win32con.GENERIC_ALL, user)
    sd = win32security.SECURITY_DESCRIPTOR()
    sd.SetSecurityDescriptorDacl(1, dacl, 0)
    sa = win32security.SECURITY_ATTRIBUTES()
    sa.SECURITY_DESCRIPTOR = sd
    return sa


def _serve_token_once(handle, token: str, allowed_pid: threading.Event, holder: dict):
    """Writes the token to the first client of the pipe, but only if that client is the server we started."""
    try:
        try:
            win32pipe.ConnectNamedPipe(handle, None)
        except pywintypes.error as e:
            if e.winerror != 535:  # ERROR_PIPE_CONNECTED: client connected before we waited
                raise
        allowed_pid.wait(10)
        client = win32pipe.GetNamedPipeClientProcessId(handle)
        if client != holder.get("pid"):
            _note(f"Refused the connection token to an unexpected process (pid {client})")
            return
        win32file.WriteFile(handle, token.encode("ascii"))
        win32file.FlushFileBuffers(handle)
    except pywintypes.error as e:
        _note(f"Token pipe: {e.strerror}")
    finally:
        win32file.CloseHandle(handle)  # closing (not disconnecting) gives the reader a clean end-of-file


def _kill_on_close_job():
    job = win32job.CreateJobObject(None, "")
    info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
    return job


def _pump_output(proc: subprocess.Popen, ready: threading.Event):
    for line in proc.stdout:
        _note(line)
        m = re.search(r"Extension host agent listening on (\d+)", line)
        if m:
            S.port = int(m.group(1))
            ready.set()
    ready.set()


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _choose_port() -> int:
    """A random free port, picked once and then reused while it stays free.

    VS Code web keeps your settings, layout, theme and folder trust in browser storage, which belongs to
    one origin (http://127.0.0.1:<port>). A new port every start would reset all of that.
    """
    configured = settings().get("port")
    if configured:
        port = int(configured)
        if not 1024 < port < 65536:
            raise RuntimeError("vscode.port in config.yaml must be between 1025 and 65535")
        return port
    saved_file = home() / "port.txt"
    try:
        saved = int(saved_file.read_text(encoding="ascii").strip())
        if 1024 < saved < 65536 and _port_free(saved):
            return saved
    except (OSError, ValueError):
        pass
    for _ in range(50):
        port = 20000 + secrets.randbelow(29000)  # below the Windows ephemeral range
        if _port_free(port):
            saved_file.write_text(str(port), encoding="ascii")
            return port
    raise RuntimeError("Couldn't find a free port for VS Code web")


def _launch(build: Path):
    token = secrets.token_urlsafe(32)
    pipe_name = r"\\.\pipe\commandcenter-vscode-" + secrets.token_hex(16)
    handle = win32pipe.CreateNamedPipe(
        pipe_name, win32pipe.PIPE_ACCESS_OUTBOUND | FILE_FLAG_FIRST_PIPE_INSTANCE,
        win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS, 1, 4096, 4096, 0, _pipe_security())
    pid_ready, holder = threading.Event(), {}
    threading.Thread(target=_serve_token_once, args=(handle, token, pid_ready, holder), daemon=True).start()
    S.token = token
    port = _choose_port()
    args = _server_cmd(build, "--host", "127.0.0.1", "--port", str(port), "--connection-token-file", pipe_name, "--disable-telemetry")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CC_", "VSCODE_", "ELECTRON_"))}
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            creationflags=NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP, env=env,
                            cwd=str(server_data_dir()), text=True, encoding="utf-8", errors="replace")
    holder["pid"] = proc.pid
    pid_ready.set()
    job = _kill_on_close_job()
    try:
        h = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, proc.pid)
        win32job.AssignProcessToJobObject(job, h)
        win32api.CloseHandle(h)
    except pywintypes.error as e:
        _note(f"Couldn't attach the server to a job object ({e.strerror}); it will be stopped with taskkill")
    S.proc, S.job = proc, job
    ready = threading.Event()
    threading.Thread(target=_pump_output, args=(proc, ready), daemon=True).start()
    ready.wait(90)
    if proc.poll() is not None or not S.port:
        try:  # release the token pipe if the server never read it (we're not its expected client, so no token)
            open(pipe_name, "rb").close()
        except OSError:
            pass
        raise RuntimeError("The VS Code server didn't start. See the log lines below.")
    deadline = time.time() + 20
    while time.time() < deadline:  # wait until it answers HTTP
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{S.port}/version", timeout=2).read()
            break
        except Exception:
            time.sleep(0.3)


def _start_worker():
    try:
        server_data_dir().mkdir(parents=True, exist_ok=True)
        build = _ensure_build()
        _set("installing", "Checking the Microsoft signatures of the VS Code server's programs")
        verify_build_signatures(build)  # also for a build that was already on disk (cached per file)
        if settings().get("mirror_desktop", True):
            _mirror_desktop(build)
        else:
            _seed_machine_settings()
        _ensure_extensions(build)
        if S.stopping:
            return
        _set("starting", "Starting the VS Code server")
        _launch(build)
        if S.stopping:  # Stop was pressed while it was starting
            _stop_process()
            return
        _set("ready", f"VS Code {S.version or ''} web is running on 127.0.0.1:{S.port}".replace("  ", " "), progress=None)
        threading.Thread(target=_watch_exit, args=(S.proc,), daemon=True).start()
    except Exception as e:
        _stop_process()
        if S.stopping:
            return  # cancelled with Stop: stay "stopped"
        log.warning("VS Code web didn't start: %s", _scrub(str(e)))
        _set("error", _scrub(str(e)), error=_scrub(str(e)))


def _watch_exit(proc):
    code = proc.wait()
    with S.lock:
        if S.proc is proc and not S.stopping:
            _cleanup_handles()
            _set("error", f"The VS Code server stopped unexpectedly (exit code {code})", error="stopped unexpectedly")


def _cleanup_handles():
    S.proc, S.port, S.token = None, None, None
    if S.job is not None:
        try:
            win32api.CloseHandle(S.job)
        except pywintypes.error:
            pass
        S.job = None


def _stop_process():
    proc, job = S.proc, S.job
    if job is not None:
        try:
            win32job.TerminateJobObject(job, 1)
        except pywintypes.error:
            pass
    if proc is not None and proc.poll() is None:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, creationflags=NO_WINDOW)
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            pass
    _cleanup_handles()


def start():
    with S.lock:
        if S.status in ("downloading", "installing", "starting", "ready"):
            return
        if S.worker and S.worker.is_alive():
            raise HTTPException(409, "The previous start is still winding down. Try again in a few seconds.")
        S.stopping, S.error, S.progress = False, "", None
        S.lines.clear()
        _set("starting", "Preparing VS Code web")
        S.worker = threading.Thread(target=_start_worker, name="vscode-start", daemon=True)
        S.worker.start()


def stop():
    with S.lock:
        S.stopping = True
        _stop_process()
        _set("stopped", "")


def sync():
    """Sync from desktop VS Code: the mirror runs before every start, so this (re)starts the server."""
    stop()
    worker = S.worker
    if worker and worker.is_alive():
        worker.join(30)  # a start that was still copying stops at the next extension
        if worker.is_alive():  # still finishing one large extension: start as soon as it has stopped
            def later():
                worker.join()
                try:
                    start()
                except HTTPException as e:
                    _set("error", str(e.detail))
            threading.Thread(target=later, daemon=True, name="vscode-sync-restart").start()
            return
    start()


def show_claude():
    """Asks the helper extension in the open VS Code page to show the Claude Code panel (it polls this file)."""
    with S.lock:
        if S.status != "ready":
            raise HTTPException(409, "VS Code web isn't running. Start it first.")
    folder = server_data_dir() / "extensions" / HELPER_DIR
    if not (folder / "extension.js").is_file():
        raise HTTPException(409, "The Command Center helper isn't in web VS Code yet. Press Sync from desktop on the VS Code tab.")
    (folder / "open-claude.signal").write_text(str(time.time_ns()), encoding="ascii")


def status() -> dict:
    with S.lock:
        return {
            "status": S.status, "detail": _scrub(S.detail), "progress": S.progress, "error": _scrub(S.error),
            "port": S.port, "version": S.version, "commit": S.commit[:10] if S.commit else None,
            "extensions": S.extensions or _installed_extensions(),
            "log": [_scrub(l) for l in list(S.lines)[-25:]],
            "desktop_vscode": bool(_desktop_install()),
            "mirror": S.mirror or None,
        }


# ---- folder check -------------------------------------------------------------------

def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(str(Path(p).resolve())))


def _project_folder(folder: str) -> Path:
    """Only folders that appear in the Projects list may be opened."""
    wanted = _norm(folder)
    for p in projects.list_projects():
        if _norm(p["path"]) == wanted:
            return Path(p["path"])
    raise HTTPException(403, "That folder isn't one of your projects")


def _folder_query(path: Path) -> str:
    # VS Code web takes the folder as a URI path on the server: /c:/Users/me/project
    return "/" + path.as_posix().replace("\\", "/") if not path.as_posix().startswith("/") else path.as_posix()


# ---- routes ---------------------------------------------------------------------------

def _no_cross_site(request: Request):
    # Defense in depth beside the app's Host check: only this app's own page may ask for the token.
    # "same-site" is refused too, because any other port on 127.0.0.1 counts as the same site.
    if request.headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
        raise HTTPException(403, "Cross-site request refused")


@router.get("/api/vscode/status")
async def get_status():
    return status()


@router.post("/api/vscode/start")
async def post_start():
    await asyncio.to_thread(start)
    return status()


@router.post("/api/vscode/stop")
async def post_stop():
    await asyncio.to_thread(stop)
    return status()


@router.post("/api/vscode/sync")
async def post_sync():
    await asyncio.to_thread(sync)
    return status()


@router.post("/api/vscode/claude")
async def post_claude():
    await asyncio.to_thread(show_claude)
    return {"ok": True}


@router.get("/api/vscode/url")
async def get_url(request: Request, folder: str, allow_unsafe: bool = False):
    _no_cross_site(request)
    path = await asyncio.to_thread(_project_folder, folder)
    # VS Code and the Claude Code extension run tasks, settings, hooks and MCP servers from the folder.
    if not allow_unsafe and await asyncio.to_thread(projects.writable_by_others, path):
        return JSONResponse({"error": projects.UNSAFE_MESSAGE, "unsafe_folder": True}, status_code=409,
                            headers={"Cache-Control": "no-store"})
    with S.lock:
        if S.status != "ready" or not S.port or not S.token:
            raise HTTPException(409, "VS Code web isn't running. Start it first.")
        port, token = S.port, S.token
    q = urllib.parse.urlencode({"tkn": token, "folder": _folder_query(path)}, quote_via=urllib.parse.quote, safe="/:")
    # Must be the same host as the dashboard (127.0.0.1). VS Code signs the page in with a SameSite=Lax
    # cookie, which browsers refuse inside a cross-site iframe; http://localhost:<port> inside a page on
    # 127.0.0.1 is cross-site, and the frame then gets "Forbidden" (tested). The cost: cookies ignore
    # ports, so Command Center's session cookie also goes to this loopback server. It is HttpOnly (page
    # scripts in VS Code can't read it) and the VS Code server neither logs nor exposes request headers.
    return JSONResponse({"url": f"http://127.0.0.1:{port}/?{q}", "origin": f"http://127.0.0.1:{port}", "folder": str(path)},
                        headers={"Cache-Control": "no-store"})


async def lifecycle():
    """Background job: nothing at startup; stops the server when Command Center shuts down."""
    try:
        await asyncio.Event().wait()
    finally:
        stop()  # directly: the default executor may already be shut down at this point


background = [lifecycle]
