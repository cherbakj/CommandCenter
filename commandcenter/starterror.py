"""The screen shown when Command Center can't start or stops unexpectedly: what went wrong, how to fix it, and every
detail that helps (where in the code, the full error, what changed, the end of the log). Without it a failed start
shows nothing, because pythonw has no console.

Standard library only and no imports from this package, so it works whatever else is broken. It also runs as a script
(`pythonw -I starterror.py <report>`): the screen is its own process, so the failed copy can end at once and let go of
the instance lock, and a new start isn't told "already running". The report is data/last-start-error.txt (private
folder); the next good start deletes it."""
import ctypes
import datetime
import logging
import os
import platform
import subprocess
import sys
import traceback
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "data" / "last-start-error.txt"
LOG = ROOT / "data" / "commandcenter.log"
TITLE = "Command Center"
SECTIONS = ("WHAT WENT WRONG", "HOW TO FIX IT", "WHILE IT'S DOWN", "DETAILS")
PLACE = None  # None: the middle of the screen
NO_WINDOW = 0x08000000
ERROR_ALREADY_EXISTS = 183


def shows_screen() -> bool:
    """The desktop app, or anything else with no console to print to (pythonw). Console commands print as usual."""
    return sys.stderr is None or sys.argv[1:2] in ([], ["app"])


def _rel(path) -> str:
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except (ValueError, OSError, TypeError):
        return str(path)


def _git(*args) -> str:
    try:
        return subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(ROOT), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=5, creationflags=NO_WINDOW).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _changed_files() -> dict:
    """Code not committed yet, as path -> git's status line. config.yaml and data/ are git-ignored."""
    out = {}
    for line in _git("status", "--short", "--untracked-files=all").splitlines():
        out[str(Path(line[3:].strip().strip('"').split(" -> ")[-1]))] = line
    return out


def _where(exc, tb) -> tuple[str | None, int | None, str]:
    """The place in Command Center's own code: (file, line, that line's text). A syntax error names its own place."""
    if isinstance(exc, SyntaxError):
        return exc.filename, exc.lineno, (exc.text or "").strip()
    own = [f for f in traceback.extract_tb(tb) if _rel(f.filename).startswith("commandcenter")]
    if own:
        return own[-1].filename, own[-1].lineno, own[-1].line or ""
    return None, None, ""


def _code_mistake(place: str, changed: dict) -> tuple[str, list]:
    """A mistake in the code itself: what to say depends on whether the file has changes not committed yet."""
    file = place.split(",")[0]
    if file in changed:
        return (f"The program has a mistake in {place}. That file has changes that haven't been committed, so this is "
                "very likely work in progress: an edit that wasn't finished when Command Center started."), [
            "If Claude is working on Command Center right now, wait for it to finish, or press Copy details and paste "
            "this into that chat so it can fix it.",
            "Then press Try again.",
            "To get going sooner, and only if nothing is working on it: in the Command Center folder run "
            "git stash push -u, then press Try again. That sets all uncommitted changes aside without losing them "
            "(git stash pop brings them back).",
        ]
    return (f"The program has a mistake in {place}. That file has no uncommitted changes, so the mistake is in the code "
            "as last committed."), [
        "Press Copy details and paste this into a Claude session in the Command Center folder so it can fix it.",
        "Then press Try again.",
        "To get going sooner: git log --oneline in the Command Center folder lists the latest changes, and git revert "
        "<hash> undoes one. Then press Try again.",
    ]


def diagnose(exc, tb, changed: dict) -> tuple[str, list]:
    """(what went wrong, the steps to fix it) in plain words, from the kind of error."""
    file, line, _ = _where(exc, tb)
    place = f"{_rel(file)}, line {line}" if file else ""
    module = type(exc).__module__
    frames = " ".join(f.filename for f in traceback.extract_tb(tb)).lower()
    again = "Then press Try again."
    if module.startswith("yaml"):
        mark = getattr(exc, "problem_mark", None)
        at = f" at line {mark.line + 1}" if mark else ""
        return f"The settings file, config.yaml, has a formatting mistake{at}, so it can't be read.", [
            f"Open config.yaml in the Command Center folder and correct{' that line' if mark else ' the mistake'}. Spacing "
            "matters. The error under DETAILS says what was expected, and config.example.yaml shows the right form.",
            again,
        ]
    if isinstance(exc, SyntaxError):
        return _code_mistake(place, changed)
    if isinstance(exc, ImportError):
        name = (exc.name or "").split(".")[0]
        if "DLL load failed" in str(exc):
            return f"A part of the program's Python packages ({name or 'see DETAILS'}) couldn't load.", [
                "Repair the packages: in the Command Center folder run "
                ".venv\\Scripts\\python.exe -m pip install -r requirements.lock",
                "If that doesn't help, check whether Cylance quarantined a file from the .venv folder.",
                again,
            ]
        if isinstance(exc, ModuleNotFoundError) and name and name != "commandcenter":
            return f"A Python package the program needs isn't installed: {name}.", [
                "Install what's missing: in the Command Center folder run "
                ".venv\\Scripts\\python.exe -m pip install -r requirements.lock",
                again,
            ]
        return _code_mistake(place, changed) if place else (f"Part of the program couldn't be loaded: {exc}", [
            "Press Copy details and paste this into a Claude session in the Command Center folder.", again])
    if module.startswith(("webview", "System", "Microsoft")) or "\\webview\\" in frames or "\\clr_loader\\" in frames:
        return "The part that draws the window (Microsoft Edge WebView2, through pywebview) failed.", [
            "Restart the PC if it has been up a long time, then press Try again.",
            "If it still fails, repair Microsoft Edge WebView2 Runtime under Windows Settings > Apps > Installed apps.",
            "If it still fails, press Copy details and paste this into a Claude session in the Command Center folder.",
        ]
    if module == "sqlite3":
        if "locked" in str(exc).lower():
            return "Its database in the data folder is locked, probably by another copy of Command Center.", [
                "In Task Manager, look for pythonw.exe left running. Use Force quit on the taskbar, or end it there.",
                again,
            ]
        return "Its database in the data folder can't be read and may be damaged.", [
            "Don't delete anything yet: it holds your alerts and settings.",
            "Press Copy details and paste this into a Claude session in the Command Center folder.",
        ]
    if isinstance(exc, PermissionError):
        target = _rel(exc.filename) if exc.filename else "a file it needs"
        return f"Windows refused access to {target}.", [
            "Check that no other copy is running: use Force quit on the taskbar, or look for pythonw.exe in Task Manager.",
            "Check whether Cylance blocked or quarantined the file.",
            again,
        ]
    if isinstance(exc, OSError) and exc.errno == 28:
        return "The disk is full.", ["Free up some space on the drive.", again]
    if isinstance(exc, MemoryError):
        return "The PC ran out of memory.", ["Close some programs, or restart the PC.", again]
    what = f"Something unexpected went wrong ({type(exc).__name__})"
    return f"{what} in {place}." if place else f"{what}.", [
        "Press Try again: if it was a passing problem, that's all it needs.",
        "If it happens again, press Copy details and paste this into a Claude session in the Command Center folder. "
        "The end of the log under DETAILS may also show what led up to it.",
    ]


def _log_tail(lines=40) -> str:
    try:
        with open(LOG, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 64 * 1024))
            return "\n".join(f.read().decode("utf-8", "replace").splitlines()[-lines:])
    except OSError as e:
        return f"(couldn't read it: {type(e).__name__})"


def build(exc, tb, started=False, changed=None) -> str:
    """The whole report, as plain text: the screen, Copy details and the file are all this."""
    changed = _changed_files() if changed is None else changed
    what, steps = diagnose(exc, tb, changed)
    file, line, source = _where(exc, tb)
    commit = _git("log", "-1", "--format=%h %cd %s", "--date=format:%Y-%m-%d %H:%M") or "(unknown)"
    stashes = _git("stash", "list").splitlines()
    return "\n".join([
        "Command Center stopped unexpectedly" if started else "Command Center couldn't start",
        "",
        "WHAT WENT WRONG",
        what,
        "",
        "HOW TO FIX IT",
        *[f"{i}. {s}" for i, s in enumerate(steps, 1)],
        "",
        "WHILE IT'S DOWN",
        "No alerts, reminders, mail watching or Teams reads until it's running again, so check Outlook and Teams yourself.",
        "VS Code and SSMS aren't closed by this: any that were docked are normal windows now (look on the taskbar) and "
        "dock again at the next good start.",
        "",
        "DETAILS",
        f"When: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}",
        f"Error: {type(exc).__name__}: {exc}",
        *([f"Where: {_rel(file)}, line {line}"] if file else []),
        *([f"That line: {source}"] if source else []),
        f"Command: {' '.join(sys.argv[1:]) or 'app'} (process {os.getpid()})",
        f"Code version: {commit}",
        "Uncommitted changes: " + ("none" if not changed else ""),
        *[f"  {v}" for v in changed.values()],
        *([f"Set aside in git stash: {len(stashes)}", *[f"  {s}" for s in stashes]] if stashes else []),
        f"Python {platform.python_version()} on Windows {platform.version()}",
        f"Saved in: {_rel(REPORT)}",
        "",
        "Full error:",
        "".join(traceback.format_exception(type(exc), exc, tb)).rstrip(),
        "",
        f"End of the log ({_rel(LOG)}):",
        _log_tail(),
    ])


def report(started=False):
    """Call from an except block: logs it, saves the report, puts up the screen and returns, so the caller can exit."""
    etype, exc, tb = sys.exc_info()
    if logging.getLogger().handlers:
        logging.exception("Command Center %s", "stopped unexpectedly" if started else "couldn't start")
    try:
        text = build(exc, tb, started)
    except Exception as e:  # the report itself must never hide the failure
        text = f"Command Center couldn't start\n\nDETAILS\n{etype.__name__}: {exc}\n(the full report failed too: {type(e).__name__})"
    try:
        REPORT.write_text(text, encoding="utf-8")
        exe = Path(sys.executable).with_name("pythonw.exe")
        subprocess.Popen([str(exe if exe.exists() else sys.executable), "-I", str(Path(__file__).resolve()), str(REPORT)], cwd=ROOT,
                         close_fds=True, creationflags=NO_WINDOW)
    except Exception:  # no data folder, or the screen couldn't start: at least say what happened
        ctypes.windll.user32.MessageBoxW(0, text[:1800], TITLE, 0x10)


def clear():
    """A good start: the last failure is over."""
    REPORT.unlink(missing_ok=True)


# ---- the screen (its own process) -------------------------------------------------------------------------------
def start_again():
    exe = Path(sys.executable).with_name("pythonw.exe")
    subprocess.Popen([str(exe if exe.exists() else sys.executable), "-m", "commandcenter", "app"], cwd=ROOT, close_fds=True,
                     creationflags=NO_WINDOW)


def open_log():
    subprocess.Popen(["notepad.exe", str(LOG)], close_fds=True)


def window(text: str):
    """The screen, built and ready: the text can be selected and copied. Returns the Tk root."""
    import tkinter as tk
    from tkinter import ttk
    root = tk.Tk()
    root.title(f"{TITLE}: problem")
    w, h = min(960, root.winfo_screenwidth() - 80), min(720, root.winfo_screenheight() - 120)
    root.geometry(f"{w}x{h}{PLACE or f'+{(root.winfo_screenwidth() - w) // 2}+{(root.winfo_screenheight() - h) // 2}'}")
    root.minsize(520, 360)
    try:
        root.iconbitmap(str(ROOT / "commandcenter" / "static" / "icon.ico"))
    except tk.TclError:
        pass
    bar = ttk.Frame(root, padding=(14, 10))
    bar.pack(side="bottom", fill="x")
    frame = ttk.Frame(root)
    frame.pack(side="top", fill="both", expand=True)
    bar_y = ttk.Scrollbar(frame)
    bar_y.pack(side="right", fill="y")
    box = tk.Text(frame, wrap="word", font=("Segoe UI", 10), padx=18, pady=14, relief="flat", spacing3=3, yscrollcommand=bar_y.set)
    box.pack(side="left", fill="both", expand=True)
    bar_y.config(command=box.yview)
    box.tag_configure("title", font=("Segoe UI Semibold", 15), foreground="#b3261e", spacing3=6)
    box.tag_configure("section", font=("Segoe UI Semibold", 10), foreground="#333333", spacing1=6)
    box.tag_configure("code", font=("Consolas", 9))
    in_details = False
    for i, line in enumerate(text.splitlines()):
        in_details = in_details or line == "DETAILS"
        tag = "title" if i == 0 else "section" if line in SECTIONS else "code" if in_details else ()
        box.insert("end", line + "\n", tag)
    # Read-only, but select and Ctrl+C still work (state="disabled" would make selecting unreliable).
    box.bind("<Key>", lambda e: None if e.state & 0x4 and e.keysym.lower() in ("c", "a") else "break")
    box.bind("<Control-a>", lambda e: (box.tag_add("sel", "1.0", "end"), "break")[1])

    def again():
        start_again()
        root.destroy()

    def copy(button):
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        button.config(text="Copied")
        root.after(2000, lambda: button.winfo_exists() and button.config(text="Copy details"))

    ttk.Button(bar, text="Try again", command=again, default="active").pack(side="left")
    copy_button = ttk.Button(bar, text="Copy details")
    copy_button.config(command=lambda: copy(copy_button))
    copy_button.pack(side="left", padx=(8, 0))
    ttk.Button(bar, text="Open log", command=open_log).pack(side="left", padx=(8, 0))
    ttk.Button(bar, text="Close", command=root.destroy).pack(side="right")
    root.bind("<Escape>", lambda e: root.destroy())
    return root


def main(path):
    # One screen at a time: a repeat of the failure while it's open has nothing new to show.
    ctypes.windll.kernel32.CreateMutexW(None, False, f"Local\\Sonance.CommandCenter.StartError.{zlib.crc32(str(ROOT).encode())}")
    if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (OSError, AttributeError):
        pass
    root = window(Path(path).read_text(encoding="utf-8"))
    root.lift()
    root.attributes("-topmost", True)  # in front of the other windows, but not stuck there
    root.after(500, lambda: root.attributes("-topmost", False))
    root.focus_force()
    root.mainloop()


if __name__ == "__main__":
    main(sys.argv[1])
