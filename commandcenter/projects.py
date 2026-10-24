"""Projects: list your code folders and open a Claude Code session in one (Windows Terminal), no VS Code needed.

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


def settings() -> dict:
    return config.section("projects", {})


def roots() -> list[Path]:
    return [Path(os.path.expandvars(os.path.expanduser(r))) for r in settings().get("roots") or [str(Path.home() / "source")]]


def claude_path() -> str | None:
    configured = settings().get("claude_path")
    if configured and Path(configured).exists():
        return configured
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    ext_root = Path.home() / ".vscode" / "extensions"
    bundled = sorted(ext_root.glob("anthropic.claude-code-*/resources/native-binary/claude.exe"),
                     key=lambda p: [int(x) if x.isdigit() else 0 for x in re.findall(r"\d+|\D+", p.parent.parent.parent.name)])
    return str(bundled[-1]) if bundled else None


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


def list_projects() -> list[dict]:
    found = []
    for root in roots():
        if root.is_dir():
            for p in sorted(root.iterdir()):
                if p.is_dir() and not p.name.startswith("."):
                    found.append(describe(p))
    found.sort(key=lambda d: d["modified"] or "", reverse=True)
    return found


def _known(path: str) -> Path:
    """Only folders inside a configured root can be launched or opened."""
    p = Path(path).resolve()
    if not any(p.parent == r.resolve() for r in roots()) or not p.is_dir():
        raise ValueError("That folder isn't one of your projects")
    return p


def launch(path: str, resume=False, prompt=""):
    p = _known(path)
    exe = claude_path()
    if not exe:
        raise ValueError("Couldn't find Claude Code. Install it, or set projects.claude_path in config.yaml")
    args = [exe]
    if resume:
        args.append("--continue")
    if prompt.strip():
        args.append(prompt.strip())
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
    name = name.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,60}", name):
        raise ValueError("Use letters, numbers, spaces, dots, dashes or underscores for the project name")
    root = roots()[0]
    p = root / name
    if p.exists():
        raise ValueError(f"{p} already exists")
    p.mkdir(parents=True)
    _git(p, "init", "-q", "-b", "main")
    return describe(p)
