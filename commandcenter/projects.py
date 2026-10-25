"""Projects: list your code folders and open a Claude Code session in one, no VS Code needed.

Projects are the direct sub-folders of projects.roots plus the folders listed in projects.extra.
The embedded terminal lives in terminal.py; launch() opens an external Windows Terminal instead.

Claude Code is found in this order: projects.claude_path in config, `claude` on PATH, then the copy bundled
with the newest Claude Code VS Code extension (which moves when the extension updates).
"""
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import config

NO_WINDOW = subprocess.CREATE_NO_WINDOW
DEFAULT_ROOTS = [r"C:\Sonance\internal", r"%USERPROFILE%\source"]
DEFAULT_EXTRA = [r"%USERPROFILE%\Desktop\Cherbak's Toolkit"]


def settings() -> dict:
    s = config.section("projects", {})
    return s if isinstance(s, dict) else {}


def _expand(paths) -> list[Path]:
    out = []
    for r in paths if isinstance(paths, list) else []:
        if isinstance(r, str) and r.strip():
            out.append(Path(os.path.expandvars(os.path.expanduser(r.strip()))))
    return out


def roots() -> list[Path]:
    """Folders whose direct sub-folders are projects."""
    return _expand(settings().get("roots") or DEFAULT_ROOTS)


def extras() -> list[Path]:
    """Individual project folders that don't live under a root."""
    extra = settings().get("extra")
    return _expand(DEFAULT_EXTRA if extra is None else extra)


def bundled_claude() -> str | None:
    """The claude.exe shipped inside the newest Claude Code VS Code extension."""
    ext_root = Path.home() / ".vscode" / "extensions"
    bundled = sorted(ext_root.glob("anthropic.claude-code-*/resources/native-binary/claude.exe"),
                     key=lambda p: [int(x) if x.isdigit() else 0 for x in re.findall(r"\d+|\D+", p.parent.parent.parent.name)])
    return str(bundled[-1]) if bundled else None


def claude_path() -> str | None:
    configured = settings().get("claude_path")
    if isinstance(configured, str) and configured and Path(os.path.expandvars(configured)).is_file():
        return os.path.expandvars(configured)
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    return bundled_claude()


# A repo's own config can name programs that git runs for status/log (core.fsmonitor, hooks, signature checks,
# pagers, external diff, ssh, credential helpers), so they are turned off for every call: listing projects must
# never run code from a project folder.
# log.showSignature=false + gpg.*.program=NUL: `git log` on a signed commit would otherwise run gpg.program from
# the repo's config. --no-optional-locks: reads never take the index lock or rewrite the index.
# -c values also reach child git processes (submodules) through GIT_CONFIG_PARAMETERS.
GIT_SAFE = ["-c", "core.fsmonitor=", "-c", "core.hooksPath=NUL", "-c", "core.untrackedCache=false",
            "-c", "log.showSignature=false", "-c", "gpg.program=NUL", "-c", "gpg.ssh.program=NUL",
            "-c", "gpg.x509.program=NUL", "-c", "core.pager=cat", "-c", "diff.external=", "-c", "core.sshCommand=NUL",
            "--no-optional-locks"]

# Keys in a repo's own config (local / worktree scope) that name a program, or pull in another config file, that
# git might run or read. A repo that sets any of them is not run through git at all (only its config is read):
# -c can't switch off includes, and a new kind of program key should fail closed.
RISKY_CONFIG = re.compile(
    r"^(gpg\.|log\.showsignature$|include\.|includeif\.|core\.sshcommand$|core\.gitproxy$|core\.askpass$|"
    r"core\.pager$|pager\.|diff\.external$|diff\..+\.(textconv|command)$|credential\.|filter\.|ssh\.)", re.I)
_config_cache: dict = {}  # normcase(path) -> (stamp, checked_at, risky)


def _run_git(path, *args) -> str:
    try:
        # List arguments, no shell.
        out = subprocess.run(["git", *GIT_SAFE, "-C", str(path), *args], capture_output=True, text=True, timeout=5,
                             creationflags=NO_WINDOW, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _config_stamp(path):
    p = Path(path) / ".git"
    out = []
    for f in (p, p / "config", p / "config.worktree"):
        try:
            st = f.stat()
            out.append((st.st_mtime_ns, st.st_size, f.is_dir()))
        except OSError:
            out.append(None)
    return tuple(out)


def _risky_config(path) -> bool:
    """True if the repo's own config sets a key in RISKY_CONFIG. Reading config (without includes) runs nothing.
    Cached until .git/config changes (a .git file, i.e. a worktree or submodule, is re-read after 5 s)."""
    key = os.path.normcase(str(path))
    stamp = _config_stamp(path)
    hit = _config_cache.get(key)
    git_is_dir = bool(stamp[0] and stamp[0][2])
    if hit and hit[0] == stamp and (git_is_dir or time.monotonic() - hit[1] < 5):
        return hit[2]
    out = _run_git(path, "config", "--no-includes", "--list", "--show-scope", "--name-only")
    names = [line.partition("\t")[2] for line in out.splitlines() if line.startswith(("local\t", "worktree\t"))]
    risky = any(RISKY_CONFIG.match(n) for n in names)
    _config_cache[key] = (stamp, time.monotonic(), risky)
    return risky


def _git(path, *args) -> str:
    """git with GIT_SAFE. Nothing but `git config` runs in a repo whose own config sets a risky key (returns "")."""
    if args and args[0] not in ("config", "init") and _risky_config(path):
        return ""
    if args and args[0] == "log":
        args = ("log", "--no-show-signature", *args[1:])
    return _run_git(path, *args)


def _summary(path: Path) -> str:
    for name in ("PROJECT.md", "CLAUDE.md", "README.md"):
        f = path / name
        if f.exists():
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip().lstrip("#").strip()
                if line and not line.lower().startswith(path.name.lower()):
                    return line[:160]
    return ""


def _changed_files(path) -> int | None:
    """None when the repo's config is risky (see _risky_config; that includes filter drivers, which git status
    could run on the repo's files)."""
    if _risky_config(path):
        return None
    out = _git(path, "status", "--porcelain", "--ignore-submodules=all")
    return len([l for l in out.splitlines() if l.strip()])


# Windows Terminal treats ";" as its command separator and '"' ends an argument, so a folder with either in
# its name or path is never listed, launched or opened (Windows already forbids '"' in file names).
UNSAFE_PATH_CHARS = (";", '"')


def _bad_chars(path) -> bool:
    return any(c in str(path) for c in UNSAFE_PATH_CHARS)


UNSAFE_MESSAGE = ("Other accounts on this PC can modify this folder, so Claude hooks or settings there could run "
                  "code as you. Fix its permissions, or confirm that you want to start anyway.")


class UnsafeFolder(ValueError):
    pass


def writable_by_others(path) -> bool:
    """True if an account other than you, SYSTEM, Administrators or TrustedInstaller can change the project
    folder, its .claude or .git folder, or the files there that Claude Code or git act on. Read-only check."""
    from . import security
    p = Path(path)
    targets = [p, p / ".claude", p / ".git", p / ".claude" / "settings.json", p / ".claude" / "settings.local.json",
               p / ".mcp.json", p / "CLAUDE.md", p / ".git" / "config", p / ".git" / "hooks"]
    for t in targets:
        if t is not p and not os.path.lexists(t):
            continue
        try:
            if security.writable_by(t):
                return True
        except Exception:
            return True  # its permissions can't be read: treat as unsafe
    return False


def check_safe(path, allow_unsafe=False):
    """Raises UnsafeFolder unless the folder is private to you, or the request explicitly allows it."""
    if allow_unsafe is not True and writable_by_others(path):
        raise UnsafeFolder(UNSAFE_MESSAGE)


def describe(path: Path) -> dict:
    is_git = (path / ".git").exists()
    last = _git(path, "log", "-1", "--format=%s\x1f%cI") if is_git else ""
    subject, _, when = last.partition("\x1f")
    dirty = _changed_files(path) if is_git else 0
    try:
        modified = datetime.fromtimestamp(max((p.stat().st_mtime for p in path.iterdir()), default=path.stat().st_mtime))
    except OSError:
        modified = None
    return {
        "name": path.name, "path": str(path), "git": is_git,
        "branch": _git(path, "branch", "--show-current") if is_git else "",
        "last_commit": subject, "last_commit_at": when[:19] if when else None, "changed_files": dirty,
        "modified": modified.isoformat(timespec="minutes") if modified else None,
        "summary": _summary(path), "has_claude_md": (path / "CLAUDE.md").exists(),
        "writable_by_others": writable_by_others(path),
        "git_skipped": bool(is_git and _risky_config(path)),  # its config names programs: git isn't run there
    }


def _candidates() -> list[Path]:
    found = []
    for root in roots():
        try:
            children = sorted(root.iterdir()) if root.is_dir() else []
        except OSError:
            children = []
        found += [p for p in children if p.is_dir() and not p.name.startswith(".")]
    found += [p for p in extras() if p.is_dir()]
    out, seen = [], set()
    for p in found:
        if _bad_chars(p):
            continue
        try:
            key = os.path.normcase(str(p.resolve()))
        except OSError:
            continue
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def list_projects() -> list[dict]:
    found = [describe(p) for p in _candidates()]
    found.sort(key=lambda d: d["modified"] or "", reverse=True)
    return found


def _known(path) -> Path:
    """Only a root's direct sub-folders and the configured extra folders can be launched or opened.

    The path is resolved first (so "..", junctions and symlinks can't point outside). UNC and device
    paths are refused before touching the file system, because resolving a network path contacts that host.
    """
    bad = ValueError("That folder isn't one of your projects")
    if not isinstance(path, str) or not path or len(path) > 400 or "\x00" in path or _bad_chars(path):
        raise bad
    if path.startswith(("\\", "/")) or not re.match(r"[A-Za-z]:[\\/]", path):
        raise bad
    # Text check before any file system access: the path must sit under a configured location.
    absolute = os.path.normcase(os.path.abspath(path))
    allowed = [os.path.normcase(os.path.abspath(str(a))) for a in roots() + extras()]
    if not any(absolute == a or absolute.startswith(a.rstrip("\\") + "\\") for a in allowed):
        raise bad
    try:
        p = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        raise bad from None
    if not p.is_dir():
        raise bad
    in_root = not p.name.startswith(".") and any(p.parent == r.resolve() for r in roots() if r.is_dir())
    is_extra = any(p == e.resolve() for e in extras() if e.is_dir())
    if not (in_root or is_extra) or _bad_chars(p):
        raise bad
    return p


def launch(path: str, resume=False, prompt="", allow_unsafe=False):
    """Open Claude Code in a new Windows Terminal window (outside Command Center)."""
    p = _known(path)
    check_safe(p, allow_unsafe)
    exe = claude_path()
    if not exe:
        raise ValueError("Couldn't find Claude Code. Install it, or set projects.claude_path in config.yaml")
    args = [exe]
    if resume:
        args.append("--continue")
    prompt = prompt.strip() if isinstance(prompt, str) else ""
    if prompt:
        # It becomes a command-line argument: never let it be read as a Claude option.
        if prompt.startswith("-") or len(prompt) > 4000 or any(ord(c) < 32 for c in prompt):
            raise ValueError("The prompt can't start with '-', contain line breaks, or be over 4000 characters")
        args.append(prompt)
    wt = shutil.which("wt")
    if wt:
        # Windows Terminal treats ";" as a command separator, so it is escaped inside every argument
        # (_known already refuses folders containing one; this is the second line of defense).
        def esc(a):
            return a.replace(";", "\\;")
        cmd = [wt, "-w", "new", "--title", esc(f"Claude: {p.name}"), "-d", esc(str(p))] + [esc(a) for a in args]
        subprocess.Popen(cmd, creationflags=subprocess.DETACHED_PROCESS, close_fds=True)
    else:
        subprocess.Popen(args, cwd=str(p), creationflags=subprocess.CREATE_NEW_CONSOLE, close_fds=True)


def open_folder(path: str):
    os.startfile(str(_known(path)))


def create(name: str) -> dict:
    name = name.strip() if isinstance(name, str) else ""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,60}", name) or name.endswith((".", " ")):
        raise ValueError("Use letters, numbers, spaces, dots, dashes or underscores for the project name")
    root = roots()[0]
    p = root / name
    if p.exists():
        raise ValueError(f"{p} already exists")
    p.mkdir(parents=True)
    _git(p, "init", "-q", "-b", "main")
    return describe(p)
