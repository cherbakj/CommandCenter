"""Side window: classic Outlook (Mail and Calendar), docked into the side window's Outlook tab (static/outlookdock.js;
Joe's call 2026-11-04). Mail / Calendar on the tab's bar switch Outlook's own view through Outlook's object model
(outlook.show_folder); Outlook's other windows (an open email, a meeting) stay its own windows.

Floating dock (2026-11-04), so Outlook survives Command Center being killed: Outlook's main window is never made a child
of Command Center (Windows destroys a window's children with it, and Outlook closed when the old child dock's host
was killed). It stays a top-level window of its own, with its frame and taskbar button taken off (caption / frame
styles stripped, WS_EX_TOOLWINDOW), and Command Center holds it over the tab's stage: placed in screen coordinates
from the same bounds channel (overlay.NativeOverlay), kept just above the side window in the z-order (re-placed when the
side window moves, resizes or comes to the front, and checked twice a second), clipped around Command Center's own
floating pieces with a window region (SetWindowRgn works on another process's window) and hidden whenever the tab isn't
on show or a dialog covers it. It gets the keyboard by itself when clicked, and a hung Outlook can't slow Command
Center (no shared input queue, unlike a docked child).

If Command Center ends without letting go (Task Manager, a crash, Force quit's kill), the dock guard (dockguard.py, a
tiny process outside Command Center's process tree) gives Outlook back its frame, taskbar button and placement. The
next start docks it again from the record (kv `outlook:dock`: handle, pid and the styles to put back; sealed), and
uses those saved styles if the guard couldn't run.

Which window: only the main ("Explorer") window, class rctrl_renwnd32, top-level with no owner, of an OUTLOOK.EXE that
runs as this Windows user and whose file carries a valid Authenticode signature from Microsoft (checked before any
dock). Command Center never starts, closes or kills Outlook here (its own watch loop may start a closed Outlook).

Let go of (a normal window again, never closed): the side window closing (closed by Joe: shown, not docked again by
itself; one screen left after undocking: shown, docked again with the second screen), Lock (minimized; docked again
after unlock), Restart (minimized), Quit (shown), Undock, unpinning Outlook, the side window's form going away,
interpreter exit, unhandled exceptions and Windows ending the session.
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

from . import config, db, dockguard, security
from .overlay import NativeOverlay
from .ssmsdock import STRIP_EXSTYLE, STRIP_STYLE, SWP_ASYNC, _image, _s32, _windows_of, verify_exe

log = logging.getLogger(__name__)

MAIN_CLASS = "rctrl_renwnd32"  # Outlook's Explorer (main) and Inspector windows share it; the Explorer has no owner
KV_DOCK = "outlook:dock"
SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x0002, 0x0001, 0x0010, 0x0020
SWP_SHOWWINDOW, SWP_HIDEWINDOW, SWP_NOZORDER, SWP_NOOWNERZORDER = 0x0040, 0x0080, 0x0004, 0x0200


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


def _guard_file() -> Path:
    return config.DATA_DIR / f"dockguard-{config.section('server', {}).get('port', 8765)}.json"


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
    image_name = "outlook.exe"

    def __init__(self, app):
        super().__init__(app)
        self.hwnd = 0
        self.pid = 0
        self.saved = None
        self.last = None          # (hwnd, pid) of the window last let go of, for Dock again
        self.redock = False       # dock it again as soon as the side window's tab is there (lock, restart, undocking)
        self._saved_hint = None   # the styles recorded at the last dock, for a window the guard couldn't put back
        self._restored = False
        self.note = None
        self.hung = False
        self._placed = None       # (x, y, w, h) last placed, screen px
        self._rgn_key = None
        self._shown = False
        self._timer = None
        self._hooked = set()      # forms whose move / activate events are hooked
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

    # ---- the guard's list (any thread) --------------------------------------------------------
    def _guard(self, entry):
        """Tells the dock guard what to put back if Command Center ends now (entry None: nothing)."""
        try:  # Outlook's entry beside the other docks' (dockguard.set_entries merges them; starts the guard once)
            dockguard.set_entries(_guard_file(), os.getpid(), "outlook", [entry] if entry else [])
        except Exception as e:
            log.warning("Outlook dock: dock guard not updated (%s)", type(e).__name__)

    # ---- putting Outlook back (any thread: plain Win32 calls on Outlook's window) --------------
    def release(self, why: str, minimize=False, front=False) -> bool:
        """Outlook back as a normal top-level window with its own styles and placement. Never closes it."""
        with self._dock_lock:
            hwnd, saved, pid = self.hwnd, self.saved, self.pid
            self.hwnd, self.saved, self.pid = 0, None, 0
            self._placed, self._rgn_key, self._shown = None, None, False
        if not hwnd or not saved or not win32gui.IsWindow(hwnd):
            return False
        self.last = (hwnd, pid)
        try:
            ctypes.windll.user32.SetWindowRgn(ctypes.c_void_p(hwnd), None, True)
            win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, saved["style"])
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, saved["exstyle"])
            flags, show, ptmin, ptmax, normal = saved["placement"]
            show_cmd = win32con.SW_SHOWMINNOACTIVE if minimize else (
                show if show in (win32con.SW_SHOWMAXIMIZED, win32con.SW_SHOWNORMAL) else win32con.SW_SHOWNORMAL)
            win32gui.SetWindowPlacement(hwnd, (flags, show_cmd, ptmin, ptmax, normal))
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_FRAMECHANGED
                                  | SWP_NOACTIVATE | SWP_SHOWWINDOW)
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
        finally:
            self._guard(None)

    def release_keep(self, why: str, minimize=False, front=False):
        """Let go of Outlook but dock it again later (Lock, Restart, Quit, one screen left): recorded, and redocked when
        the side window's Outlook tab is back."""
        with self._dock_lock:
            if self.hwnd:
                self._record({"hwnd": self.hwnd, "pid": self.pid, "saved": self.saved})
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
        """Once per start: the Outlook window recorded at the last Restart / Quit / Lock (or still floating-docked when
        Command Center was killed), if it's still that Outlook's top-level window, is docked again once the side
        window's tab is there; its recorded styles are kept for putting it back."""
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
        if not isinstance(h, int) or not isinstance(pid, int) or not win32gui.IsWindow(h):
            self._record(None)
            return
        try:
            if win32process.GetWindowThreadProcessId(h)[1] != pid or win32gui.GetParent(h) != 0 or not self.is_ours(pid):
                self._record(None)
                return
        except Exception:
            return
        saved = e.get("saved")
        self._saved_hint = (h, saved) if isinstance(saved, dict) and {"style", "exstyle", "placement"} <= set(saved) else None
        self.last, self.redock = (h, pid), True

    def install_hooks(self):
        """Last lines of defence for Outlook's window: interpreter exit, unhandled exceptions, Windows signing out.
        (Beyond those, the dock guard.)"""
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
        return []  # nothing of Command Center's holds Outlook

    def _hook_form(self, form):
        """The side window moving, resizing, coming to the front or going away: Outlook follows (or is let go of)."""
        key = int(form.Handle.ToInt64())
        if key in self._hooked:
            return
        self._hooked.add(key)
        follow = lambda *_: self._apply()  # noqa: E731
        form.LocationChanged += follow
        form.SizeChanged += follow
        form.Activated += follow
        form.HandleDestroyed += lambda *_: (self._hooked.discard(key), self.release_keep("side window closing"))

    def _dock(self, hwnd: int, pid: int):
        if self.app.quitting or not win32gui.IsWindow(hwnd) or win32process.GetWindowThreadProcessId(hwnd)[1] != pid:
            return
        form = self._form()
        if not self.app.unlocked or self.app.lock_showing or form is None:
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
            hint, self._saved_hint = self._saved_hint, None
            if hint and hint[0] == hwnd:
                saved = hint[1]  # left frameless by a killed Command Center the guard couldn't reach: the real styles
            else:
                saved = {"style": win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE),
                         "exstyle": win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE),
                         "placement": win32gui.GetWindowPlacement(hwnd)}
            if win32gui.IsIconic(hwnd) or ctypes.windll.user32.IsZoomed(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
            exstyle = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, _s32((style & ~STRIP_STYLE & ~win32con.WS_CHILD) | win32con.WS_POPUP))
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, _s32((exstyle & ~STRIP_EXSTYLE) | win32con.WS_EX_TOOLWINDOW))
            win32gui.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
                                  | SWP_FRAMECHANGED | SWP_HIDEWINDOW)  # hidden until placed over the tab
            self.saved, self.hwnd, self.pid = saved, hwnd, pid
            self._placed, self._rgn_key, self._shown = None, None, False
            self.last, self.redock, self.note, self.hung = None, False, None, False
        log.info("Outlook dock: docked Outlook (pid %s)", pid)
        entry = {"hwnd": hwnd, "pid": pid, "saved": saved}
        self._record(entry)  # docked again at the next start
        self._guard({"hwnd": hwnd, "pid": pid, "image": self.image_name, "style": saved["style"],
                     "exstyle": saved["exstyle"], "placement": saved["placement"]})
        self._hook_form(form)
        self._start_timer()
        self._apply()

    def _apply(self):
        form = self._form()
        with self._lock:
            want = dict(self.want)
        ready = self.app.unlocked and not self.app.lock_showing and not self.app.quitting
        if form is not None and ready and not self._restored:
            self._restore()
        if form is not None and ready and not self.hwnd and self.redock and self.last and win32gui.IsWindow(self.last[0]):
            hwnd, pid = self.last
            if self.is_ours(pid):
                self.redock = False
                self._dock(hwnd, pid)  # (calls _apply again)
                return
        if not self.hwnd or not win32gui.IsWindow(self.hwnd):
            return
        import System.Windows.Forms as WinForms
        on = bool(form is not None and self._showing(want) and form.Visible
                  and form.WindowState != WinForms.FormWindowState.Minimized)
        geo = self._geometry(form, want["rect"], want["cuts"]) if on else None
        if geo is None:
            self._hide()
            return
        left, top, width, height, holes = geo
        from System.Drawing import Point
        pt = form.PointToScreen(Point(left, top))
        rect = (pt.X, pt.Y, width, height)
        self._region(width, height, holes)
        # just above the side window: below whatever is above it now (another app Joe put on top stays there)
        form_hwnd = int(form.Handle.ToInt64())
        above = win32gui.GetWindow(form_hwnd, win32con.GW_HWNDPREV)
        after = win32con.HWND_TOP if not above else (0 if above == self.hwnd else above)
        flags = SWP_NOACTIVATE | SWP_SHOWWINDOW | SWP_NOOWNERZORDER | SWP_ASYNC | (SWP_NOZORDER if after == 0 else 0)
        if rect != self._placed or not self._shown or after != 0:
            win32gui.SetWindowPos(self.hwnd, after, *rect, flags)
            self._placed, self._shown = rect, True

    def _hide(self):
        if self._shown and self.hwnd:
            win32gui.SetWindowPos(self.hwnd, 0, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
                                  | SWP_HIDEWINDOW | SWP_ASYNC)
        self._shown = False

    def _region(self, width, height, holes):
        """Clips Outlook around Command Center's floating pieces over the stage (toasts, menus): a window region on
        Outlook's window, set only when it changes."""
        key = (width, height, tuple((h.X, h.Y, h.Width, h.Height) for h in holes))
        if key == self._rgn_key:
            return
        self._rgn_key = key
        user32, gdi = ctypes.windll.user32, ctypes.windll.gdi32
        if not holes:
            user32.SetWindowRgn(ctypes.c_void_p(self.hwnd), None, True)
            return
        rgn = gdi.CreateRectRgn(0, 0, width, height)
        for h in holes:
            hole = gdi.CreateRectRgn(h.X, h.Y, h.X + h.Width, h.Y + h.Height)
            gdi.CombineRgn(rgn, rgn, hole, 4)  # RGN_DIFF
            gdi.DeleteObject(hole)
        if not user32.SetWindowRgn(ctypes.c_void_p(self.hwnd), rgn, True):  # the window owns it once set
            gdi.DeleteObject(rgn)

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
            if not win32gui.IsWindow(self.hwnd) or win32gui.GetParent(self.hwnd) != 0:
                with self._dock_lock:
                    gone = not win32gui.IsWindow(self.hwnd)
                    self.hwnd, self.pid, self.saved = 0, 0, None
                self.note = "Outlook was closed." if gone else None
                self._guard(None)
                self._timer.Stop()
                return
            self.hung = bool(ctypes.windll.user32.IsHungAppWindow(self.hwnd))
            if not self.hung and (win32gui.IsIconic(self.hwnd) or ctypes.windll.user32.IsZoomed(self.hwnd)):
                win32gui.ShowWindow(self.hwnd, win32con.SW_SHOWNOACTIVATE)  # Win+Up / Win+Down on it: back over the tab
                self._placed = None
            if self._shown and self._placed:
                left, top, right, bottom = win32gui.GetWindowRect(self.hwnd)
                if (left, top, right - left, bottom - top) != self._placed:
                    self._placed = None  # moved or resized by something else: placed again
            self._apply()
        except Exception as e:
            log.warning("Outlook dock: check failed (%s)", type(e).__name__)
