"""Side window: classic Outlook (Mail and Calendar), docked into the side window's Outlook tab (static/outlookdock.js;
Joe's call 2026-11-04). Like the SQL tab's SSMS (ssmsdock.py): Outlook's main window becomes a child of a Panel in the
side window (its caption / frame styles stripped, then SetParent), laid over the tab's stage through the same bounds
channel (overlay.NativeOverlay). Mail / Calendar on the tab's bar switch Outlook's own view through Outlook's object
model (outlook.show_folder); Outlook's other windows (an open email, a meeting) stay its own top-level windows.

Which window: only the main ("Explorer") window, class rctrl_renwnd32, top-level with no owner, of an OUTLOOK.EXE that
runs as this Windows user and whose file carries a valid Authenticode signature from Microsoft (checked before any
dock). Command Center never starts, closes or kills Outlook here (its own watch loop may start a closed Outlook).

Never lose Outlook: it goes back as a normal window, never closed, when the side window closes (closed by Joe: shown;
one screen left after undocking: shown, and docked again when the second screen is back), on Lock (minimized; docked
again after unlock), Restart (minimized) and Quit (shown), on Undock, when the side window's panel is destroyed (its
HandleDestroyed comes before its children are destroyed), on interpreter exit, unhandled exceptions and Windows ending
the session; Force quit (forcequit.py) puts it back from outside. What's docked is recorded in kv `outlook:dock`
(window handle, process id; sealed) so the next start docks it again once the side window shows the tab.
What can't be helped (as with SSMS): if Command Center's process is killed outright, Windows destroys the docked window
and Outlook closes; mail is on the server and Outlook keeps drafts, so it reopens as it was. A cross-process child also
shares input processing: an Outlook that stops responding can make the side window slow until it recovers (the bar
says so; Undock ends it).
"""
import atexit
import ctypes
import logging
import os
import sys
import threading
from pathlib import Path

import win32con
import win32gui
import win32process

from . import db, security
from .overlay import NativeOverlay
from .ssmsdock import STRIP_EXSTYLE, STRIP_STYLE, SWP_ASYNC, _image, _s32, _windows_of, verify_exe

log = logging.getLogger(__name__)

MAIN_CLASS = "rctrl_renwnd32"  # Outlook's Explorer (main) and Inspector windows share it; the Explorer has no owner
KV_DOCK = "outlook:dock"


def is_our_outlook(pid: int) -> bool:
    """An OUTLOOK.EXE of this Windows user (the signature is checked on its file before docking)."""
    image = _image(pid)
    return bool(image) and os.path.basename(image).lower() == "outlook.exe" and security.pid_is_current_user(pid)


def outlook_processes() -> list[int]:
    return [pid for pid in win32process.EnumProcesses() if pid and is_our_outlook(pid)]


def main_window(pid: int) -> int:
    """Outlook's main (Explorer) window: top-level, visible, no owner, class rctrl_renwnd32. The largest when there are
    several (a calendar opened in its own window is another Explorer)."""
    best, area = 0, -1
    for h in _windows_of(pid):
        if win32gui.GetWindow(h, win32con.GW_OWNER) == 0 and win32gui.GetClassName(h) == MAIN_CLASS:
            left, top, right, bottom = win32gui.GetWindowRect(h)
            if (right - left) * (bottom - top) > area:
                best, area = h, (right - left) * (bottom - top)
    return best


def _check_exe(pid: int):
    ok, why = verify_exe(Path(_image(pid)), "Outlook")
    if not ok:
        raise ValueError(why)


class OutlookDock(NativeOverlay):
    NAME = "Outlook dock"
    SITES = ("outlook",)
    HOSTS = ("side",)
    INTERVAL_MS = 500
    # tests swap these for a stand-in window (never Joe's Outlook)
    is_ours = staticmethod(is_our_outlook)
    processes = staticmethod(outlook_processes)
    find_main = staticmethod(main_window)
    check_exe = staticmethod(_check_exe)

    def __init__(self, app):
        super().__init__(app)
        self.panel = None
        self.region = None
        self.hwnd = 0
        self.pid = 0
        self.saved = None
        self.last = None          # (hwnd, pid) of the window last let go of, for Dock again
        self.redock = False       # dock it again as soon as the tab's panel exists (after a lock, restart, undocking)
        self._restored = False
        self.note = None
        self.hung = False
        self._size = (0, 0)
        self._timer = None
        self._dock_lock = threading.RLock()
        self._hooks = False

    # ---- from the server thread ----------------------------------------------------------
    def status(self) -> dict:
        pids = self.processes()
        docked = {"pid": self.pid, "title": win32gui.GetWindowText(self.hwnd)[:200]} if self.hwnd else None
        return {"available": self.available(), "running": bool(pids), "docked": docked, "hung": self.hung,
                "note": self.note, "can_redock": bool(self.last and win32gui.IsWindow(self.last[0]) and not self.hwnd)}

    def handle_action(self, body: dict) -> dict:
        name = str(body.get("action") or "")
        if name == "dock":
            return self._dock_running()
        if name == "undock":
            with self._dock_lock:
                if not self.hwnd:
                    return {"ok": True}
                self.redock = False
                self.release("Undock", front=True)
            self._record(None)  # Undock means stay undocked, after a restart too
            self._queue()
            return {"ok": True}
        if name == "view":
            view = body.get("view")
            if view not in ("mail", "calendar"):
                raise ValueError("view must be mail or calendar")
            from .connectors import outlook
            outlook.call_sync(outlook.show_folder, view)
            return {"ok": True}
        raise ValueError("unknown action")

    def _dock_running(self) -> dict:
        if not self.available():
            raise ValueError("Outlook docks in the side window of the desktop app")
        pids = self.processes()
        if not pids:
            raise ValueError("Outlook isn't running")
        pid = pids[0]
        self.check_exe(pid)
        hwnd = self.find_main(pid)
        if not hwnd:
            raise ValueError("Outlook has no main window yet")
        self._invoke(lambda: self._dock(hwnd, pid))
        return {"ok": True}

    # ---- putting Outlook back (any thread: plain Win32 calls on Outlook's window) --------------
    def release(self, why: str, minimize=False, front=False) -> bool:
        """Outlook back as a normal top-level window with its own styles and placement. Never closes it."""
        with self._dock_lock:
            hwnd, saved, pid = self.hwnd, self.saved, self.pid
            self.hwnd, self.saved, self.pid = 0, None, 0
        if not hwnd or not saved or not win32gui.IsWindow(hwnd):
            return False
        self.last = (hwnd, pid)
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
            log.info("Outlook dock: Outlook (pid %s) is a normal window again (%s)", pid, why)
            return True
        except Exception as e:
            log.warning("Outlook dock: couldn't put Outlook back cleanly (%s)", type(e).__name__)
            return False

    def release_keep(self, why: str, minimize=False, front=False):
        """Let go of Outlook but dock it again later (Lock, Restart, Quit, one screen left): recorded, and redocked when
        the side window's Outlook tab is back."""
        with self._dock_lock:
            if self.hwnd:
                self._record({"hwnd": self.hwnd, "pid": self.pid})
                self.redock = True
                self.release(why, minimize=minimize, front=front)

    def release_forget(self, why: str):
        """Joe closed the side window (or unpinned Outlook): Outlook back, shown, and not docked again by itself."""
        with self._dock_lock:
            had = bool(self.hwnd)
            self.redock = False
            self.release(why, front=True)
        if had:
            self._record(None)

    def _record(self, entry):
        try:
            db.kv_set(KV_DOCK, entry)
        except Exception as e:
            log.warning("Outlook dock: couldn't record the docked window (%s)", type(e).__name__)

    def _restore(self):
        """Once per start: the Outlook window recorded at the last Restart / Quit / Lock, if it's still Outlook's
        top-level main window, is docked again once the tab's panel exists."""
        self._restored = True
        if self.hwnd:
            return  # docked already (Dock was pressed first): its own record stands
        try:
            e = db.kv_get(KV_DOCK)
        except Exception:
            e = None
        if not isinstance(e, dict):
            return
        h, pid = e.get("hwnd"), e.get("pid")
        if not isinstance(h, int) or not isinstance(pid, int) or self.hwnd or not win32gui.IsWindow(h):
            self._record(None)
            return
        try:
            if win32process.GetWindowThreadProcessId(h)[1] != pid or win32gui.GetParent(h) != 0 or not self.is_ours(pid):
                self._record(None)
                return
        except Exception:
            return
        self.last, self.redock = (h, pid), True

    def install_hooks(self):
        """Last lines of defence for Outlook's window: interpreter exit, unhandled exceptions, Windows signing out."""
        if self._hooks:
            return
        self._hooks = True
        atexit.register(lambda: self.release_keep("exit"))
        old_hook, old_thread_hook = sys.excepthook, threading.excepthook

        def excepthook(*a):
            self.release_keep("unhandled error")
            old_hook(*a)

        def thread_hook(args):
            if args.exc_type is not SystemExit:
                self.release_keep("unhandled error in a thread")
            old_thread_hook(args)
        sys.excepthook, threading.excepthook = excepthook, thread_hook
        try:
            from System import AppDomain, UnhandledExceptionEventHandler
            AppDomain.CurrentDomain.UnhandledException += UnhandledExceptionEventHandler(
                lambda s, e: self.release_keep(".NET unhandled exception"))
        except Exception as e:
            log.warning("Outlook dock: no .NET exception hook (%s)", type(e).__name__)
        try:
            from Microsoft.Win32 import SessionEndingEventHandler, SystemEvents
            SystemEvents.SessionEnding += SessionEndingEventHandler(lambda s, e: self.release_keep("Windows is signing out"))
        except Exception as e:
            log.warning("Outlook dock: no session-ending hook (%s)", type(e).__name__)

    # ---- UI thread -------------------------------------------------------------------------
    def _panels(self):
        return [self.panel]

    def _ensure_panel(self):
        if self.panel is None or self.panel.IsDisposed:
            self.panel = self._new_panel()
            self.region = None
            # The side window is being destroyed: the panel's WM_DESTROY comes before its children are destroyed, so
            # Outlook is let go here at the latest (and docked again when the side window is back).
            self.panel.HandleDestroyed += lambda *_: self.release_keep("side window closing")
            self.panel.Resize += lambda *_: self._fit()
        return self.panel

    def _dock(self, hwnd: int, pid: int):
        if self.app.quitting or not win32gui.IsWindow(hwnd) or win32process.GetWindowThreadProcessId(hwnd)[1] != pid:
            return
        if not self.app.unlocked or self.app.lock_showing or self._form() is None:
            self.last, self.redock = (hwnd, pid), True
            return
        with self._dock_lock:
            if self.hwnd == hwnd:
                return
            if self.hwnd:
                self.release("another Outlook window docked")
            if ctypes.windll.user32.IsHungAppWindow(hwnd):
                self.note = "Outlook isn't responding; try again when it is."
                return
            panel = self._ensure_panel()
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
            exstyle = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            self.saved = {"style": style, "exstyle": exstyle, "placement": win32gui.GetWindowPlacement(hwnd)}
            if win32gui.IsIconic(hwnd) or style & win32con.WS_MAXIMIZE:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
            win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, _s32((style & ~STRIP_STYLE) | win32con.WS_CHILD
                                                                  | win32con.WS_CLIPSIBLINGS | win32con.WS_CLIPCHILDREN))
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, _s32(exstyle & ~STRIP_EXSTYLE))
            win32gui.SetParent(hwnd, int(panel.Handle.ToInt64()))
            self.hwnd, self.pid = hwnd, pid
            self._size = (0, 0)
            win32gui.SetWindowPos(hwnd, 0, 0, 0, max(panel.Width, 1), max(panel.Height, 1), win32con.SWP_NOZORDER
                                  | win32con.SWP_NOACTIVATE | win32con.SWP_FRAMECHANGED | win32con.SWP_SHOWWINDOW)
            self.last, self.redock, self.note, self.hung = None, False, None, False
        log.info("Outlook dock: docked Outlook (pid %s)", pid)
        self._record({"hwnd": hwnd, "pid": pid})  # Force quit puts it back from outside: docked again next start
        self._start_timer()
        self._apply()

    def _fit(self):
        if self.hwnd and self.panel is not None:
            size = (max(self.panel.Width, 1), max(self.panel.Height, 1))
            if size != self._size:
                self._size = size
                win32gui.SetWindowPos(self.hwnd, 0, 0, 0, *size, win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE | SWP_ASYNC)

    def _apply(self):
        form = self._form()
        if form is None:
            return
        with self._lock:
            want = dict(self.want)
        ready = self.app.unlocked and not self.app.lock_showing and not self.app.quitting
        if ready and not self._restored:
            self._restore()
        if ready and not self.hwnd and self.redock and self.last and win32gui.IsWindow(self.last[0]):
            hwnd, pid = self.last
            if self.is_ours(pid):
                self.redock = False
                self._dock(hwnd, pid)  # (calls _apply again)
                return
        on = bool(self._showing(want) and self.hwnd and win32gui.IsWindow(self.hwnd))
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
            if not self.hwnd:
                self._timer.Stop()
                return
            if not win32gui.IsWindow(self.hwnd) or self.panel is None or self.panel.IsDisposed \
                    or win32gui.GetParent(self.hwnd) != int(self.panel.Handle.ToInt64()):
                with self._dock_lock:
                    gone = not win32gui.IsWindow(self.hwnd)
                    self.hwnd, self.pid, self.saved = 0, 0, None
                self.note = "Outlook was closed." if gone else None
                self._timer.Stop()
                self._apply()
                return
            self.hung = bool(ctypes.windll.user32.IsHungAppWindow(self.hwnd))
            if not self.hung and (win32gui.IsIconic(self.hwnd) or ctypes.windll.user32.IsZoomed(self.hwnd)):
                win32gui.ShowWindow(self.hwnd, win32con.SW_SHOWNOACTIVATE)  # its own minimize / maximize: fill the tab again
                self._size = (0, 0)
                self._fit()
        except Exception as e:
            log.warning("Outlook dock: check failed (%s)", type(e).__name__)
