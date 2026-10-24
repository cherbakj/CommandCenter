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


def _git(path, *args) -> str:
    try:
        out = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=5,
                             creationflags=NO_WINDOW, encoding="utf-8", errors="replace")
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _summary(path: Path) -> str:
    for name in ("PROJECT.md", "CLAUDE.md", "README.md"):
        f = path / name
        if f.exists():
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip().lstrip("#").strip()
                if line and not line.lower().startswith(path.name.lower()):
                    return line[:160]
    return ""


def describe(path: Path) -> dict:
    is_git = (path / ".git").exists()
    last = _git(path, "log", "-1", "--format=%s\x1f%cI") if is_git else ""
    subject, _, when = last.partition("\x1f")
    dirty = len([l for l in _git(path, "status", "--porcelain").splitlines() if l.strip()]) if is_git else 0
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
    if not isinstance(path, str) or not path or len(path) > 400 or "\x00" in path:
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
    if not (in_root or is_extra):
        raise bad
    return p


def launch(path: str, resume=False, prompt=""):
    """Open Claude Code in a new Windows Terminal window (outside Command Center)."""
    p = _known(path)
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
        # Windows Terminal treats ";" as a command separator, so escape it inside arguments.
        cmd = [wt, "-w", "new", "--title", f"Claude: {p.name}", "-d", str(p)] + [a.replace(";", "\\;") for a in args]
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
