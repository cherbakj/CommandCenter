"""VS Code tab: the real desktop VS Code docked into the Command Center window (static/codedock.js), so restarting
Command Center doesn't end VS Code sessions (Claude Code chats, terminals, unsaved editors).

Docking works like the SQL tab's SSMS (ssmsdock.py, overlay.NativeOverlay): a VS Code window's caption/frame styles
are stripped and it becomes a child of a Panel laid over the tab's stage (same bounds channel, hide rules and
cut-outs). One window per project: several can be docked, the one for the "Working on" project shows and the others
are hidden inside the panel, still running. VS Code keeps a 7 px invisible resize border even when docked, so the
window is placed that much outside the panel and the panel clips it.

Restart / Quit: every docked window goes back as a normal window (minimized on Restart, shown on Quit; never closed)
and is recorded in kv `vscode:dock` (window handle, process id, folder); the next start docks the ones still open
again once it's unlocked, so a Claude Code chat in them carries on. Lock does the same in memory. Undock takes a
window off the list. After a reboot or a crash the recorded handles are gone and nothing is docked.

Which windows are touched: only top-level VS Code main windows (class Chrome_WidgetWin_1, title ending
"Visual Studio Code") of a Code.exe that runs as this Windows user and whose image is the installed desktop VS Code
(vscode._desktop_install), which must carry a valid Authenticode signature from Microsoft. A folder is opened only if
it's one of the projects (projects._known), with the same "other accounts can modify it" confirmation as everywhere
else; VS Code is started with the folder as a plain list argument (no shell), without Command Center's ELECTRON_* /
VSCODE_* environment.

What can't be helped (as with SSMS): if this process is killed outright (Task Manager, a crash), Windows destroys its
windows' children, including docked VS Code windows; VS Code then closes those windows (tested: a one-window VS Code
quit), so Claude chats running in them stop. Unsaved editors come back from VS Code's own backups. Keys: while VS Code
has the keyboard, Ctrl+K, Ctrl+Shift+P and F11 are VS Code's (no Privacy-mode hotkey is passed through here).
"""
import atexit
import ctypes
import logging
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

import win32con
import win32gui
import win32process

from . import db, projects, security
from .overlay import NativeOverlay
from .ssmsdock import NOT_DIALOGS, STRIP_EXSTYLE, STRIP_STYLE, SWP_ASYNC, _image, _s32, _same_file, _windows_of, verify_exe

log = logging.getLogger(__name__)

APP_NAME = "Visual Studio Code"
WIN_CLASS = "Chrome_WidgetWin_1"
KV_DOCK = "vscode:dock"   # windows to dock again after a restart (sealed kv: handle, pid, folder, active)
LAUNCH_WAIT = 90          # seconds to wait for a newly opened folder's window
MAX_INSET = 16

# Clicks in a docked window (_watch_clicks): WM_PARENTNOTIFY's low word is the button message.
WM_PARENTNOTIFY, WM_NCDESTROY = 0x0210, 0x0082
BUTTON_DOWNS = {0x0201, 0x0204, 0x0207, 0x020B, 0x0246}  # left, right, middle, X button, pointer (touch / pen)
SUBCLASS_ID = 0xCCD0
LRESULT = ctypes.c_ssize_t
SUBCLASSPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                  ctypes.c_size_t, ctypes.c_size_t)


def code_exe() -> Path | None:
    from .vscode import _desktop_install
    d = _desktop_install()
    return d / "Code.exe" if d else None


def is_our_code(pid: int) -> bool:
    """A Code.exe of this Windows user, running the installed desktop VS Code (the signature is checked on the file)."""
    exe, image = code_exe(), _image(pid)
    return bool(exe and image) and os.path.basename(image).lower() == "code.exe" and _same_file(image, exe) \
        and security.pid_is_current_user(pid)


def title_names(title: str, name: str) -> bool:
    """Does a VS Code window title ("file - Folder - Visual Studio Code", "● file - Folder - ...") name this folder?"""
    parts = [p.strip() for p in str(title or "").split(" - ")]
    if len(parts) < 2 or not parts[-1].startswith(APP_NAME):
        return False
    n = name.lower()
    return any(p.lstrip("●").strip().lower() in (n, f"{n} (workspace)") for p in parts[:-1])


def code_windows() -> list[tuple[int, int]]:
    """(hwnd, pid) of the top-level VS Code main windows of ours, visible (or minimized)."""
    found, ours = [], {}

    def visit(h, _):
        if (win32gui.IsWindowVisible(h) and win32gui.GetClassName(h) == WIN_CLASS
                and win32gui.GetWindow(h, win32con.GW_OWNER) == 0
                and win32gui.GetWindowText(h).rsplit(" - ", 1)[-1].startswith(APP_NAME)):
            pid = win32process.GetWindowThreadProcessId(h)[1]
            if pid not in ours:
                ours[pid] = is_our_code(pid)
            if ours[pid]:
                found.append((h, pid))
        return True
    win32gui.EnumWindows(visit, None)
    return found


def _same_path(a, b) -> bool:
    return bool(a and b) and os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


class CodeDock(NativeOverlay):
    NAME = "VS Code dock"
    SITES = ("code",)
    INTERVAL_MS = 500

    def __init__(self, app):
        super().__init__(app)
        self.panel = None
        self.region = None
        self.docked: dict[int, dict] = {}  # hwnd -> {pid, folder, saved (its own styles/placement), inset (l, t, r, b)}
        self.active = 0                    # the docked window that shows
        self.pending: list[dict] | None = None  # windows to dock again (after a restart: from kv; after Lock: memory)
        self.starting = ""                 # project name being opened
        self.note = None
        self.hung = False
        self.extra_args: list[str] = []    # tests: a separate VS Code profile (--user-data-dir ...)
        self._seen: set[int] = set()
        self._fitted = None
        self._timer = None
        self._dock_lock = threading.RLock()
        self._hooks = False

    # ---- from the server thread ----------------------------------------------------------
    def status(self) -> dict:
        exe = code_exe()
        installed = bool(exe and exe.is_file())
        with self._dock_lock:
            docked = [(h, dict(d)) for h, d in self.docked.items()]
            active = self.active
        out_docked = [{"hwnd": h, "folder": d["folder"], "name": Path(d["folder"]).name if d["folder"] else "",
                       "title": win32gui.GetWindowText(h)[:200], "active": h == active}
                      for h, d in docked if win32gui.IsWindow(h)]
        running = [{"hwnd": h, "title": win32gui.GetWindowText(h)[:200]}
                   for h, _ in (code_windows() if installed else []) if h not in self.docked]
        return {"available": self.available(), "installed": installed, "path": str(exe) if exe else "",
                "docked": out_docked, "running": running, "starting": self.starting, "hung": self.hung,
                "note": self.note, "restoring": bool(self.pending)}

    def handle_action(self, body: dict) -> dict:
        name = str(body.get("action") or "")
        if name == "open":
            return self._open(body.get("folder"), body.get("allow_unsafe") is True)
        hwnd = body.get("hwnd")
        if hwnd is not None and (not isinstance(hwnd, int) or isinstance(hwnd, bool)):
            raise ValueError("hwnd must be a number")
        if name == "dock":
            return self._dock_running(hwnd)
        if name == "show":
            with self._dock_lock:
                if hwnd not in self.docked:
                    raise ValueError("that window isn't docked")
            self._invoke(lambda: self._activate(hwnd))
            return {"ok": True}
        if name == "undock":
            with self._dock_lock:
                target = hwnd if hwnd in self.docked else self.active
            if target:
                self.release(target, "Undock", front=True)
                self._queue()
            return {"ok": True}
        raise ValueError("unknown action")

    def _check_exe(self) -> Path:
        exe = code_exe()
        if not exe:
            raise ValueError("Desktop VS Code isn't installed (looked in %LOCALAPPDATA%\\Programs and Program Files)")
        ok, why = verify_exe(exe, "VS Code")
        if not ok:
            raise ValueError(why)
        return exe

    def _open(self, folder, allow_unsafe: bool) -> dict:
        """Shows the project's VS Code window: docked already, open as a normal window (docked now), or opened."""
        if not self.available():
            raise ValueError("VS Code docks only in the Command Center desktop app")
        path = projects._known(folder)
        projects.check_safe(path, allow_unsafe)
        exe = self._check_exe()
        name = path.name
        with self._dock_lock:
            for h, d in self.docked.items():
                if win32gui.IsWindow(h) and (_same_path(d["folder"], path) or title_names(win32gui.GetWindowText(h), name)):
                    self._invoke(lambda h=h: self._activate(h))
                    return {"ok": True, "hwnd": h}
        for h, pid in code_windows():
            if title_names(win32gui.GetWindowText(h), name):
                self._invoke(lambda h=h, pid=pid: self._dock(h, pid, str(path), True))
                return {"ok": True, "hwnd": h}
        if self.starting:
            raise ValueError(f"Still opening {self.starting}")
        before = {h for h, _ in code_windows()}
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("ELECTRON_", "VSCODE_"))}
        args = [str(exe), *self.extra_args, str(path)]
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        kw = dict(cwd=str(exe.parent), env=env, close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL)
        try:  # never in a job of this process: nothing that ends Command Center ends VS Code
            subprocess.Popen(args, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kw)
        except OSError:
            subprocess.Popen(args, creationflags=flags, **kw)
        self.starting, self.note = name, None
        log.info("VS Code dock: opening a project in VS Code")
        threading.Thread(target=self._wait_and_dock, args=(str(path), before), name="code-wait", daemon=True).start()
        return {"ok": True, "starting": name}

    def _wait_and_dock(self, folder: str, before: set):
        name = Path(folder).name
        deadline = time.time() + LAUNCH_WAIT
        try:
            while time.time() < deadline and not self.app.quitting:
                hits = [(h, pid) for h, pid in code_windows() if title_names(win32gui.GetWindowText(h), name)]
                hits.sort(key=lambda w: w[0] in before)  # a new window first
                if hits:
                    h, pid = hits[0]
                    self._invoke(lambda: self._dock(h, pid, folder, True))
                    return
                time.sleep(0.4)
            self.note = (f"VS Code didn't show a window for {name} in time. If it opened, use Dock running VS Code "
                         "(its title has to name the folder).")
        finally:
            self.starting = ""

    def _dock_running(self, hwnd) -> dict:
        if not self.available():
            raise ValueError("VS Code docks only in the Command Center desktop app")
        self._check_exe()
        match = next(((h, pid) for h, pid in code_windows() if h == hwnd), None)
        if not match:
            raise ValueError("that isn't a VS Code window of yours (it may have closed)")
        self._invoke(lambda: self._dock(match[0], match[1], "", True))
        return {"ok": True}

    # ---- putting windows back (any thread: plain Win32 calls on VS Code's windows) ------------
    def release(self, hwnd: int, why: str, minimize=False, front=False) -> bool:
        """Puts one docked window back as a normal top-level window with its own styles and placement. Never closes it."""
        with self._dock_lock:
            d = self.docked.pop(hwnd, None)
            if hwnd == self.active:
                self.active = next(iter(self.docked), 0)
                if self.active:
                    win32gui.ShowWindow(self.active, win32con.SW_SHOWNOACTIVATE)
                self._fitted = None
        if not d or not win32gui.IsWindow(hwnd):
            return False
        saved = d["saved"]
        try:
            win32gui.SetParent(hwnd, 0)
            win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, saved["style"])
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, saved["exstyle"])
            flags, show, ptmin, ptmax, normal = saved["placement"]
            show_cmd = win32con.SW_SHOWMINNOACTIVE if minimize else (
                show if show in (win32con.SW_SHOWMAXIMIZED, win32con.SW_SHOWNORMAL) else win32con.SW_SHOWNORMAL)
            win32gui.SetWindowPlacement(hwnd, (flags, show_cmd, ptmin, ptmax, normal))
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, 0, 0, 0, 0, win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
                                  | win32con.SWP_FRAMECHANGED | win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW)
            if front and not minimize:
                try:
                    win32gui.SetForegroundWindow(hwnd)
                except Exception:
                    pass
            log.info("VS Code dock: a VS Code window is a normal window again (%s)", why)
            return True
        except Exception as e:
            log.warning("VS Code dock: couldn't put a VS Code window back cleanly (%s)", type(e).__name__)
            return False
        finally:
            if why == "Undock":
                self._save()

    def _entries(self) -> list[dict]:
        with self._dock_lock:
            return [{"hwnd": h, "pid": d["pid"], "folder": d["folder"], "active": h == self.active}
                    for h, d in self.docked.items()]

    def release_all(self, why: str, minimize=False, remember=False):
        """Every docked window back to normal. remember: dock them again later (in memory, and in kv for the next
        start). Later calls with nothing docked (the window-closing handler, the exit hook after Restart / Quit) never
        overwrite what an earlier call recorded."""
        entries = self._entries()
        if not entries:
            return
        if remember:
            self.pending = entries
        for e in entries:
            self.release(e["hwnd"], why, minimize=minimize)
        try:
            db.kv_set(KV_DOCK, entries if remember else [])
        except Exception as e:
            log.warning("VS Code dock: couldn't record the docked windows (%s)", type(e).__name__)

    def on_lock(self):
        """Lock: VS Code windows go back to normal, minimized (the lock screen covers Command Center, not them); docked
        again after unlock."""
        self.release_all("Lock", minimize=True, remember=True)
        self._queue()

    def _save(self):
        try:
            db.kv_set(KV_DOCK, self._entries())
        except Exception as e:
            log.warning("VS Code dock: couldn't record the docked windows (%s)", type(e).__name__)

    def install_hooks(self):
        """Last lines of defence: interpreter exit and unhandled exceptions put VS Code's windows back."""
        if self._hooks:
            return
        self._hooks = True
        atexit.register(lambda: self.release_all("exit", remember=True))
        old_hook, old_thread_hook = sys.excepthook, threading.excepthook

        def excepthook(*a):
            self.release_all("unhandled error", remember=True)
            old_hook(*a)

        def thread_hook(args):
            if args.exc_type is not SystemExit:
                self.release_all("unhandled error in a thread", remember=True)
            old_thread_hook(args)
        sys.excepthook, threading.excepthook = excepthook, thread_hook
        try:
            from Microsoft.Win32 import SessionEndingEventHandler, SystemEvents
            SystemEvents.SessionEnding += SessionEndingEventHandler(lambda s, e: self.release_all("Windows is signing out"))
        except Exception as e:
            log.warning("VS Code dock: no session-ending hook (%s)", type(e).__name__)

    # ---- UI thread -------------------------------------------------------------------------
    def _panels(self):
        return [self.panel]

    def _ensure_panel(self):
        if self.panel is None:
            self.panel = self._new_panel()
            # The window is being destroyed: the panel's WM_DESTROY comes before its children are destroyed.
            self.panel.HandleDestroyed += lambda *_: self.release_all(
                "Command Center window closing", minimize=getattr(self.app, "restarting", False), remember=True)
            self.panel.Resize += lambda *_: self._fit()
            self._watch_clicks(int(self.panel.Handle.ToInt64()))
        return self.panel

    def _watch_clicks(self, panel_hwnd: int):
        """A click in a docked VS Code gives it the keyboard. As a child window VS Code (Chromium) is never the active
        window, so it doesn't take the focus itself and keys kept going to the dashboard. The panel hears of every
        button press in its children (WM_PARENTNOTIFY) through a window subclass."""
        comctl = ctypes.WinDLL("comctl32")
        try:
            set_sub, def_proc, remove = comctl.SetWindowSubclass, comctl.DefSubclassProc, comctl.RemoveWindowSubclass
        except AttributeError:  # comctl32 5.x exports them by ordinal only
            set_sub, def_proc, remove = comctl[410], comctl[413], comctl[412]
        def_proc.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        def_proc.restype = LRESULT

        def proc(hwnd, msg, wp, lp, uid, ref):
            try:
                if msg == WM_PARENTNOTIFY and (wp & 0xFFFF) in BUTTON_DOWNS:
                    self._invoke(self._take_keyboard)
                elif msg == WM_NCDESTROY:
                    remove(wintypes.HWND(hwnd), self._click_proc, SUBCLASS_ID)
            except Exception as e:
                log.warning("VS Code dock: click hook failed (%s)", type(e).__name__)
            return def_proc(hwnd, msg, wp, lp)
        self._click_proc = SUBCLASSPROC(proc)  # kept alive as long as the panel
        if not set_sub(wintypes.HWND(panel_hwnd), self._click_proc, SUBCLASS_ID, 0):
            log.warning("VS Code dock: no click hook, so typing in a docked VS Code won't work (Undock it)")

    def _take_keyboard(self):
        with self._dock_lock:
            hwnd = self.active
        if not hwnd or self.panel is None or not self.panel.Visible or not win32gui.IsWindow(hwnd):
            return
        user32 = ctypes.windll.user32
        focus = user32.GetFocus()  # this thread's input is shared with the docked VS Code, so its focus shows here
        if focus and (focus == hwnd or user32.IsChild(wintypes.HWND(hwnd), wintypes.HWND(focus))):
            return
        user32.SetFocus(wintypes.HWND(hwnd))

    def _dock(self, hwnd: int, pid: int, folder: str, activate: bool):
        if self.app.quitting or not win32gui.IsWindow(hwnd) or win32process.GetWindowThreadProcessId(hwnd)[1] != pid:
            return
        if not self.app.unlocked or self.app.lock_showing:
            self.pending = [*(self.pending or []), {"hwnd": hwnd, "pid": pid, "folder": folder, "active": activate}]
            return
        with self._dock_lock:
            if hwnd in self.docked:
                if folder and not self.docked[hwnd]["folder"]:
                    self.docked[hwnd]["folder"] = folder
                if activate:
                    self._activate(hwnd)
                return
            if ctypes.windll.user32.IsHungAppWindow(hwnd):
                self.note = "VS Code isn't responding; try again when it is."
                return
            panel = self._ensure_panel()
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
            exstyle = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            saved = {"style": style, "exstyle": exstyle, "placement": win32gui.GetWindowPlacement(hwnd)}
            if win32gui.IsIconic(hwnd) or style & win32con.WS_MAXIMIZE:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
            win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, _s32((style & ~STRIP_STYLE) | win32con.WS_CHILD
                                                                  | win32con.WS_CLIPSIBLINGS | win32con.WS_CLIPCHILDREN))
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, _s32(exstyle & ~STRIP_EXSTYLE))
            win32gui.SetParent(hwnd, int(panel.Handle.ToInt64()))
            w, h = max(panel.Width, 100), max(panel.Height, 100)
            win32gui.SetWindowPos(hwnd, 0, 0, 0, w, h, win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE
                                  | win32con.SWP_FRAMECHANGED | win32con.SWP_SHOWWINDOW)
            self.docked[hwnd] = {"pid": pid, "folder": folder, "saved": saved, "inset": self._measure_inset(hwnd)}
            self._seen |= set(_windows_of(pid))  # VS Code's other open windows stay where they are; only new ones come forward
            self.note, self.hung = None, False
            if activate or not self.active:
                self._activate(hwnd)
            else:
                win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
        log.info("VS Code dock: docked a VS Code window (pid %s, %d docked)", pid, len(self.docked))
        self._save()
        self._tidy()
        self._start_timer()
        self._apply()

    @staticmethod
    def _measure_inset(hwnd) -> tuple[int, int, int, int]:
        """VS Code's invisible border inside its window (left, top, right, bottom), from its client area."""
        try:
            wl, wt, wr, wb = win32gui.GetWindowRect(hwnd)
            cx, cy = win32gui.ClientToScreen(hwnd, (0, 0))
            _, _, cw, ch = win32gui.GetClientRect(hwnd)
            inset = (cx - wl, cy - wt, wr - (cx + cw), wb - (cy + ch))
            return tuple(max(0, min(MAX_INSET, v)) for v in inset)
        except Exception:
            return (0, 0, 0, 0)

    def _activate(self, hwnd: int):
        with self._dock_lock:
            if hwnd not in self.docked:
                return
            for other in self.docked:
                if other != hwnd:
                    win32gui.ShowWindow(other, win32con.SW_HIDE)
            win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
            self.active, self._fitted = hwnd, None
        self._fit()
        self._save()
        self._apply()

    def _fit(self):
        """The active window fills the panel, its invisible border just outside (the panel clips it)."""
        with self._dock_lock:
            hwnd = self.active
            d = self.docked.get(hwnd)
        if not d or self.panel is None or not win32gui.IsWindow(hwnd):
            return
        l, t, r, b = d["inset"]
        w, h = max(self.panel.Width, 1), max(self.panel.Height, 1)
        want = (hwnd, -l, -t, w + l + r, h + t + b)
        if want != self._fitted:
            self._fitted = want
            win32gui.SetWindowPos(hwnd, 0, *want[1:], win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE | SWP_ASYNC)

    def _restore(self):
        """Docks again the windows recorded before a restart (kv) or a Lock (memory), if they're still there."""
        if self.pending is None:
            try:
                saved = db.kv_get(KV_DOCK, []) or []
            except Exception:
                saved = []
            self.pending = [e for e in saved if isinstance(e, dict)]
        entries, self.pending = self.pending, []
        entries.sort(key=lambda e: bool(e.get("active")))  # the active one last, so it ends up showing
        for e in entries:
            h, pid = e.get("hwnd"), e.get("pid")
            if not isinstance(h, int) or not isinstance(pid, int) or h in self.docked or not win32gui.IsWindow(h):
                continue
            if win32process.GetWindowThreadProcessId(h)[1] != pid or win32gui.GetClassName(h) != WIN_CLASS:
                continue
            if win32gui.GetParent(h) != 0 or not is_our_code(pid):
                continue
            self._dock(h, pid, str(e.get("folder") or ""), bool(e.get("active")))

    def _apply(self):
        form = self._form()
        if form is None:
            return
        if (self.pending is None or self.pending) and self.app.unlocked and not self.app.lock_showing \
                and not self.app.quitting:
            self._restore()  # (docking calls _apply again)
        with self._lock:
            want = dict(self.want)
        show = self._showing(want)
        on = bool(show and self.active and win32gui.IsWindow(self.active))
        if on:
            geo = self._geometry(form, want["rect"], want["cuts"])
            on = geo is not None
            if on:
                left, top, width, height, holes = geo
                self.panel.SetBounds(left, top, width, height)
                self._fit()
                self._clip(self, width, height, holes)
        self._show_panel(form, self.panel, on)

    def _start_timer(self):
        if self._timer is None:
            import System.Windows.Forms as WinForms
            self._timer = WinForms.Timer()
            self._timer.Interval = self.INTERVAL_MS
            self._timer.Tick += lambda *_: self._tick()
        self._timer.Start()

    def _tick(self):
        try:
            panel_h = int(self.panel.Handle.ToInt64()) if self.panel is not None else 0
            gone = []
            with self._dock_lock:
                for h in list(self.docked):
                    if not win32gui.IsWindow(h) or win32gui.GetParent(h) != panel_h:
                        gone.append(not win32gui.IsWindow(h))
                        self.docked.pop(h, None)
                        if h == self.active:
                            self.active = next(iter(self.docked), 0)
                            if self.active:
                                win32gui.ShowWindow(self.active, win32con.SW_SHOWNOACTIVATE)
                            self._fitted = None
                if not self.docked:
                    self._timer.Stop()
            if gone:
                self.note = "A docked VS Code window was closed." if any(gone) else None
                self._save()
                self._apply()
                return
            hwnd = self.active
            if not hwnd:
                return
            self.hung = bool(ctypes.windll.user32.IsHungAppWindow(hwnd))
            if not self.hung:
                if win32gui.IsIconic(hwnd) or ctypes.windll.user32.IsZoomed(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)  # back to filling the stage
                    self._fitted = None
                elif self._fitted:
                    # VS Code moved or sized itself (full screen with F11, zoom): fit it to the stage again
                    l, t, r, b = win32gui.GetWindowRect(hwnd)
                    pl, pt = win32gui.ClientToScreen(panel_h, (0, 0))
                    if (l - pl, t - pt, r - l, b - t) != self._fitted[1:]:
                        self._fitted = None
                self._fit()
            self._tidy()
        except Exception as e:
            log.warning("VS Code dock: check failed (%s)", type(e).__name__)

    def _tidy(self):
        """A new window of a docked VS Code (a file dialog, a message box, another VS Code window) is brought in front
        of Command Center, so it doesn't open behind the full-screen window."""
        form = self._form()
        form_hwnd = int(form.Handle.ToInt64()) if form else 0
        with self._dock_lock:
            pids = {d["pid"] for d in self.docked.values()}
        current = [h for pid in pids for h in _windows_of(pid)]
        for h in current:
            if h in self._seen or h in self.docked or win32gui.GetClassName(h) in NOT_DIALOGS or self._above(h, form_hwnd):
                continue
            above = win32gui.GetWindow(form_hwnd, win32con.GW_HWNDPREV) or win32con.HWND_TOP
            win32gui.SetWindowPos(h, above, 0, 0, 0, 0, win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)
            if win32gui.GetForegroundWindow() == form_hwnd:
                try:
                    win32gui.SetForegroundWindow(h)
                except Exception:
                    pass
        self._seen = set(current)

    @staticmethod
    def _above(h, form_hwnd) -> bool:
        if not form_hwnd:
            return True
        n = win32gui.GetWindow(form_hwnd, win32con.GW_HWNDPREV)
        for _ in range(2000):
            if not n:
                return False
            if n == h:
                return True
            n = win32gui.GetWindow(n, win32con.GW_HWNDPREV)
        return True
