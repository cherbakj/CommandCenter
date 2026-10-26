"""SQL tab: the real SQL Server Management Studio, docked into the Command Center window (static/ssms.js).

Docking: SSMS's main window becomes a child of a Panel in this window (its caption/frame styles are stripped, then
SetParent), and the panel is laid over the SQL tab's stage like the Pulse view (overlay.NativeOverlay: the same bounds
channel, hide rules and cut-outs). SSMS's own dialogs stay top-level windows; any that would open behind Command
Center are brought to the front. Undock puts it back as a normal window.

Which windows are touched: only the main window of an Ssms.exe process that runs as this Windows user and whose image
is the installed SSMS (ssms_path(): sql.ssms_path or the SSMS 20 default), which must carry a valid Authenticode
signature from Microsoft. SSMS is started with -S <server> -U <user> -nosplash from the Toolkit connection settings;
a password is never put on its command line.

Never lose SSMS work: Undock, Lock (undocked and minimized; docked again after unlock), Quit, Restart, the window being
destroyed (the panel's HandleDestroyed runs before its children are destroyed), Windows ending the session, and
unhandled exceptions all put SSMS back as a normal top-level window with its own styles and placement. SSMS is never
closed or killed by Command Center.
What can't be helped: if this process is killed outright (Task Manager, a hard crash), Windows destroys its windows'
children too, including a docked SSMS main window; SSMS's process is left running without a window, and unsaved query
text survives only in SSMS's AutoRecover files. (Checked with a parent / child window pair in two processes:
TerminateProcess on the parent destroys the child window, the child's process stays alive.)
Also: a cross-process child window shares input processing with this window (Windows attaches the two threads' input
queues), so an SSMS that stops responding can make Command Center's window slow too until SSMS recovers or is
undocked. The tab says when SSMS isn't responding.
"""
import atexit
import ctypes
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import win32api
import win32con
import win32gui
import win32process

from . import config, security
from .overlay import NativeOverlay

log = logging.getLogger(__name__)

SSMS_DEFAULT = r"C:\Program Files (x86)\Microsoft SQL Server Management Studio 20\Common7\IDE\Ssms.exe"
MAIN_TITLE = "Microsoft SQL Server Management Studio"
GLOW_CLASS = "VisualStudioGlowWindow"  # the four resize-glow windows SSMS draws around its main window
STRIP_STYLE = (win32con.WS_POPUP | win32con.WS_CAPTION | win32con.WS_THICKFRAME | win32con.WS_SYSMENU
               | win32con.WS_MINIMIZEBOX | win32con.WS_MAXIMIZEBOX | win32con.WS_MAXIMIZE | win32con.WS_MINIMIZE)
STRIP_EXSTYLE = (win32con.WS_EX_APPWINDOW | win32con.WS_EX_WINDOWEDGE | win32con.WS_EX_DLGMODALFRAME
                 | win32con.WS_EX_CLIENTEDGE | win32con.WS_EX_STATICEDGE | win32con.WS_EX_TOPMOST)
SWP_ASYNC = 0x4000  # SWP_ASYNCWINDOWPOS: never wait on SSMS's thread (it may be busy)
NOT_DIALOGS = {GLOW_CLASS, "SysShadow", "ComboLBox", "#32768", "tooltips_class32", "IME", "MSCTFIME UI"}


def _s32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v & 0x80000000 else v


def ssms_path() -> Path:
    raw = str((config.section("sql", {}) or {}).get("ssms_path") or SSMS_DEFAULT)
    p = Path(raw)
    return p if p.is_absolute() and p.name.lower() == "ssms.exe" else Path(SSMS_DEFAULT)


_sig_cache: dict[tuple, tuple[bool, str]] = {}


def verify_exe(path: Path, what: str = "SSMS") -> tuple[bool, str]:
    """(ok, why): the file exists and Windows says its Authenticode signature is valid and from Microsoft.
    Inline PowerShell (no .ps1 file), the path passed in an environment variable, no console window.
    `what` names the app in messages (SSMS, VS Code: codedock.py uses this too)."""
    try:
        st = path.stat()
    except OSError:
        return False, f"{what} isn't installed at {path}"
    key = (os.path.normcase(str(path)), st.st_size, st.st_mtime_ns)
    if key in _sig_cache:
        return _sig_cache[key]
    ps = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    script = ("$s = Get-AuthenticodeSignature -LiteralPath $env:CC_SIG_FILE; [pscustomobject]@{ Status = [string]$s.Status; "
              "Subject = $(if ($s.SignerCertificate) { $s.SignerCertificate.Subject } else { '' }) } | ConvertTo-Json -Compress")
    try:
        out = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True,
                             timeout=60, creationflags=subprocess.CREATE_NO_WINDOW, stdin=subprocess.DEVNULL,
                             env={**os.environ, "CC_SIG_FILE": str(path)}, encoding="utf-8", errors="replace")
        r = json.loads(out.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return False, f"couldn't check {what}'s signature ({type(e).__name__})"
    subject, status = r.get("Subject") or "", r.get("Status") or "no signature"
    org = next((part.strip()[2:] for part in subject.split(",") if part.strip().startswith("O=")), "")
    ok = status == "Valid" and org == "Microsoft Corporation"
    result = (True, "signed by Microsoft") if ok else (False, f"{path.name} isn't validly signed by Microsoft "
                                                              f"({status}{', signed by ' + org if org else ''})")
    _sig_cache[key] = result
    if not ok:
        log.warning("%s dock: %s", what, result[1])
    return result


def _image(pid: int) -> str:
    try:
        h = win32api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    except Exception:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(1024)
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(ctypes.c_void_p(int(h)), 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        h.Close()


def _same_file(a, b) -> bool:
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except OSError:
        return False


def is_our_ssms(pid: int) -> bool:
    """An Ssms.exe of this Windows user, running the installed SSMS (the signature is checked on the file)."""
    image = _image(pid)
    return bool(image) and os.path.basename(image).lower() == "ssms.exe" and _same_file(image, ssms_path()) \
        and security.pid_is_current_user(pid)


def ssms_processes() -> list[int]:
    return [pid for pid in win32process.EnumProcesses() if pid and is_our_ssms(pid)]


def _windows_of(pid: int, visible_only=True) -> list[int]:
    out = []

    def visit(h, _):
        if win32process.GetWindowThreadProcessId(h)[1] == pid and (not visible_only or win32gui.IsWindowVisible(h)):
            out.append(h)
        return True
    win32gui.EnumWindows(visit, None)
    return out


def main_window(pid: int) -> int:
    """SSMS's main window (top-level, visible, no owner, a WPF HwndWrapper whose title ends with the product name)."""
    for h in _windows_of(pid):
        if (win32gui.GetWindow(h, win32con.GW_OWNER) == 0 and win32gui.GetClassName(h).startswith("HwndWrapper[")
                and win32gui.GetWindowText(h).endswith(MAIN_TITLE)):
            return h
    return 0


def connect_args() -> list[str]:
    """-S <server> [-U <user>] from the Toolkit connection (checked with the Toolkit's own rules). No password."""
    try:
        from . import toolkit
        s = toolkit.settings()
        server, user = str(s.get("server") or "").strip(), str(s.get("username") or "").strip()
        if not server or not toolkit.SERVER_RE.match(server):
            return []
        return ["-S", server] + (["-U", user] if user and toolkit.USER_RE.match(user) else [])
    except Exception as e:
        log.warning("SSMS dock: no Toolkit connection to start SSMS with (%s)", type(e).__name__)
        return []


class SsmsDock(NativeOverlay):
    NAME = "SSMS dock"
    SITES = ("ssms",)
    INTERVAL_MS = 500

    def __init__(self, app):
        super().__init__(app)
        self.panel = None
        self.region = None
        self.hwnd = 0           # the docked main window
        self.pid = 0
        self.saved = None       # its own styles, parent and placement, to put back on undock
        self.last = None        # (hwnd, pid) of the last window undocked by Undock or Lock, for Re-dock
        self.redock_after_unlock = False
        self.ours: set[int] = set()  # SSMS processes this app started
        self.starting = 0       # pid being waited for (main window not up yet)
        self.note = None
        self.hung = False
        self._seen: set[int] = set()
        self._size = (0, 0)
        self._timer = None
        self._dock_lock = threading.RLock()
        self._hooks = False
        self.hotkey_factory = None  # desktop.GlobalHotkey, set by DesktopApp (Ctrl+Shift+P while SSMS has the keyboard)
        self._privacy_hotkey = None

    # ---- from the server thread ----------------------------------------------------------
    def status(self) -> dict:
        exe = ssms_path()
        installed = exe.is_file()
        sig = _sig_cache.get(self._sig_key(exe)) if installed else None
        running = []
        for pid in (ssms_processes() if installed else []):
            docked = pid == self.pid and bool(self.hwnd)
            h = self.hwnd if docked else main_window(pid)
            running.append({"pid": pid, "title": win32gui.GetWindowText(h)[:200] if h else "", "docked": docked,
                            "started_here": pid in self.ours, "has_window": bool(h)})
        last = self.last if self.last and win32gui.IsWindow(self.last[0]) and not self.hwnd else None
        args = connect_args()
        return {
            "available": self.available(), "installed": installed, "path": str(exe),
            "signed": None if sig is None else sig[0],
            "server": args[1] if len(args) > 1 else "", "user": args[3] if len(args) > 3 else "",
            "docked": {"pid": self.pid, "title": win32gui.GetWindowText(self.hwnd)[:200]} if self.hwnd else None,
            "running": running, "starting": bool(self.starting), "can_redock": bool(last),
            "hung": self.hung, "note": self.note,
        }

    @staticmethod
    def _sig_key(exe):
        try:
            st = exe.stat()
            return (os.path.normcase(str(exe)), st.st_size, st.st_mtime_ns)
        except OSError:
            return None

    def action(self, name: str, pid=None) -> dict:
        if name == "start":
            return self._start()
        if name == "dock":
            if not isinstance(pid, int) or isinstance(pid, bool):
                raise ValueError("pid must be a number")
            return self._dock_pid(pid)
        if name == "undock":
            with self._dock_lock:
                if not self.hwnd:
                    return {"ok": True}
                self.last = (self.hwnd, self.pid)
                self.redock_after_unlock = False
                self.release("Undock", front=True)
            self._queue()
            return {"ok": True}
        if name == "redock":
            if not self.last or not win32gui.IsWindow(self.last[0]):
                raise ValueError("there's no undocked SSMS window to dock again")
            return self._dock_pid(self.last[1], self.last[0])
        raise ValueError("unknown action")

    def handle_action(self, body: dict) -> dict:
        return self.action(str(body.get("action") or ""), body.get("pid"))

    def _check_exe(self):
        exe = ssms_path()
        ok, why = verify_exe(exe)
        if not ok:
            raise ValueError(why)
        return exe

    def _dock_pid(self, pid: int, hwnd: int = 0) -> dict:
        if not self.available():
            raise ValueError("SSMS docks only in the Command Center desktop app")
        self._check_exe()
        if not is_our_ssms(pid):
            raise ValueError("that isn't the installed SSMS running as you")
        hwnd = hwnd if hwnd and win32gui.IsWindow(hwnd) and win32process.GetWindowThreadProcessId(hwnd)[1] == pid \
            else main_window(pid)
        if not hwnd:
            raise ValueError("that SSMS has no main window yet")
        self._invoke(lambda: self._dock(hwnd, pid))
        return {"ok": True, "pid": pid}

    def _start(self) -> dict:
        with self._dock_lock:
            if self.hwnd and win32gui.IsWindow(self.hwnd):
                return {"ok": True, "pid": self.pid, "already": True}
            if self.starting:
                return {"ok": True, "pid": self.starting, "starting": True}
        exe = self._check_exe()
        for pid in self.ours:  # one this app started that isn't docked (Undock): dock it again
            if is_our_ssms(pid) and main_window(pid) and self.available():
                return self._dock_pid(pid)
        args = [str(exe), *connect_args(), "-nosplash"]
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        kw = dict(cwd=str(exe.parent), close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL)
        try:  # its own process: never in a job of this one, so nothing that ends Command Center ends SSMS
            proc = subprocess.Popen(args, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kw)
        except OSError:
            proc = subprocess.Popen(args, creationflags=flags, **kw)
        self.ours.add(proc.pid)
        self.note = None
        log.info("SSMS dock: started SSMS (pid %s)%s", proc.pid, " with the Toolkit server" if len(args) > 2 else "")
        if self.available():
            self.starting = proc.pid
            threading.Thread(target=self._wait_and_dock, args=(proc.pid,), name="ssms-wait", daemon=True).start()
        return {"ok": True, "pid": proc.pid}

    def _wait_and_dock(self, pid: int, seconds: float = 180):
        deadline = time.time() + seconds
        try:
            while time.time() < deadline and not self.app.quitting:
                if not is_our_ssms(pid):
                    self.note = "SSMS closed before it opened."
                    return
                h = main_window(pid)
                if h:
                    self._invoke(lambda: self._dock(h, pid))
                    return
                time.sleep(0.5)
            self.note = "SSMS didn't open a window in time; use Dock running SSMS once it's up."
        finally:
            self.starting = 0

    # ---- putting SSMS back (any thread: plain Win32 calls on SSMS's window) --------------------
    def release(self, why: str, minimize=False, front=False):
        """Puts a docked SSMS back as a normal top-level window with its own styles and placement. Never closes it."""
        with self._dock_lock:
            hwnd, saved, pid = self.hwnd, self.saved, self.pid
            self.hwnd, self.saved, self.pid = 0, None, 0
        if not hwnd or not saved or not win32gui.IsWindow(hwnd):
            return False
        self.last = (hwnd, pid)  # Re-dock brings it back
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
            log.info("SSMS dock: SSMS (pid %s) is a normal window again (%s)", pid, why)
            return True
        except Exception as e:
            log.warning("SSMS dock: couldn't put SSMS back cleanly (%s: %s)", type(e).__name__, str(e)[:100])
            return False

    def on_lock(self):
        """Lock: SSMS goes back to a normal, minimized window (the lock screen covers Command Center, not SSMS);
        docked again when Command Center is unlocked and the SQL tab shows."""
        with self._dock_lock:
            if self.hwnd:
                self.last = (self.hwnd, self.pid)
                self.redock_after_unlock = True
                self.release("Lock", minimize=True)
        self._queue()

    def install_hooks(self):
        """Last lines of defence for SSMS's window: interpreter exit, unhandled exceptions, Windows ending the session."""
        if self._hooks:
            return
        self._hooks = True
        atexit.register(lambda: self.release("exit"))
        old_hook, old_thread_hook = sys.excepthook, threading.excepthook

        def excepthook(*a):
            self.release("unhandled error")
            old_hook(*a)

        def thread_hook(args):
            if args.exc_type is not SystemExit:
                self.release("unhandled error in a thread")
            old_thread_hook(args)
        sys.excepthook, threading.excepthook = excepthook, thread_hook
        try:
            from System import AppDomain, UnhandledExceptionEventHandler
            AppDomain.CurrentDomain.UnhandledException += UnhandledExceptionEventHandler(
                lambda s, e: self.release(".NET unhandled exception"))
        except Exception as e:
            log.warning("SSMS dock: no .NET exception hook (%s)", type(e).__name__)
        try:
            from Microsoft.Win32 import SessionEndingEventHandler, SystemEvents
            SystemEvents.SessionEnding += SessionEndingEventHandler(lambda s, e: self.release("Windows is signing out"))
        except Exception as e:
            log.warning("SSMS dock: no session-ending hook (%s)", type(e).__name__)

    # ---- UI thread -------------------------------------------------------------------------
    def _panels(self):
        return [self.panel]

    def _ensure_panel(self):
        if self.panel is None:
            self.panel = self._new_panel()
            # The window is being destroyed (quit, or anything else): the panel's WM_DESTROY comes before its
            # children are destroyed, so SSMS is let go here at the latest.
            self.panel.HandleDestroyed += lambda *_: self.release("Command Center window closing")
            self.panel.Resize += lambda *_: self._fit()
        return self.panel

    def _dock(self, hwnd: int, pid: int):
        if self.app.quitting or not win32gui.IsWindow(hwnd) or win32process.GetWindowThreadProcessId(hwnd)[1] != pid:
            return
        if not self.app.unlocked or self.app.lock_showing:
            self.last, self.redock_after_unlock = (hwnd, pid), True  # dock once unlocked
            return
        with self._dock_lock:
            if self.hwnd == hwnd:
                return
            if self.hwnd:
                self.release("another SSMS docked")
            if ctypes.windll.user32.IsHungAppWindow(hwnd):
                self.note = "SSMS isn't responding; try again when it is."
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
            self.last, self.redock_after_unlock, self.note, self.hung = None, False, None, False
            self._seen = set()  # every SSMS window already open (a connect dialog) gets checked once
        log.info("SSMS dock: docked SSMS (pid %s)", pid)
        self._tidy()
        self._start_timer()
        self._apply()

    def _fit(self):
        if self.hwnd and self.panel is not None:
            size = (max(self.panel.Width, 1), max(self.panel.Height, 1))
            if size != self._size:
                self._size = size
                win32gui.SetWindowPos(self.hwnd, 0, 0, 0, *size, win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE
                                      | SWP_ASYNC)

    def _apply(self):
        form = self._form()
        if form is None:
            return
        with self._lock:
            want = dict(self.want)
        show = self._showing(want)
        if show and not self.hwnd and self.redock_after_unlock and self.last and win32gui.IsWindow(self.last[0]):
            hwnd, pid = self.last
            if is_our_ssms(pid):
                self._dock(hwnd, pid)  # (calls _apply again)
                return
        on = bool(show and self.hwnd and win32gui.IsWindow(self.hwnd))
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
                self._privacy_key(False)
                self._timer.Stop()
                return
            if not win32gui.IsWindow(self.hwnd) or win32gui.GetParent(self.hwnd) != int(self.panel.Handle.ToInt64()):
                with self._dock_lock:
                    gone = not win32gui.IsWindow(self.hwnd)
                    self.hwnd, self.pid, self.saved = 0, 0, None
                self.note = "SSMS was closed." if gone else None
                self._privacy_key(False)
                self._timer.Stop()
                self._apply()
                return
            self.hung = bool(ctypes.windll.user32.IsHungAppWindow(self.hwnd))
            if not self.hung and (win32gui.IsIconic(self.hwnd) or ctypes.windll.user32.IsZoomed(self.hwnd)):
                # SSMS's own minimize / maximize buttons (drawn inside its window) on a docked window: back to filling
                # the stage (a minimized child would sit as an icon in the panel)
                win32gui.ShowWindow(self.hwnd, win32con.SW_SHOWNOACTIVATE)
                self._size = (0, 0)
                self._fit()
            self._tidy()
            self._privacy_key(self._has_keyboard())
        except Exception as e:
            log.warning("SSMS dock: check failed (%s)", type(e).__name__)

    def _has_keyboard(self) -> bool:
        """Command Center is the foreground window and the keyboard focus is inside the docked SSMS."""
        form = self._form()
        if not form or not self.hwnd or not self.panel.Visible:
            return False
        if win32gui.GetForegroundWindow() != int(form.Handle.ToInt64()):
            return False

        class GUITHREADINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint32), ("flags", ctypes.c_uint32), ("hwndActive", ctypes.c_void_p),
                        ("hwndFocus", ctypes.c_void_p), ("hwndCapture", ctypes.c_void_p), ("hwndMenuOwner", ctypes.c_void_p),
                        ("hwndMoveSize", ctypes.c_void_p), ("hwndCaret", ctypes.c_void_p), ("rcCaret", ctypes.c_long * 4)]
        info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
        tid = win32process.GetWindowThreadProcessId(self.hwnd)[0]
        if not ctypes.windll.user32.GetGUIThreadInfo(tid, ctypes.byref(info)) or not info.hwndFocus:
            return False
        focus = int(info.hwndFocus)
        return focus == self.hwnd or bool(ctypes.windll.user32.IsChild(ctypes.c_void_p(self.hwnd), ctypes.c_void_p(focus)))

    def _privacy_key(self, want: bool):
        """Ctrl+Shift+P (Privacy mode) reaches the dashboard while SSMS has the keyboard: a hotkey registered only
        for that time (SSMS keeps Ctrl+K, which it uses for its own Ctrl+K chords)."""
        if want and self._privacy_hotkey is None and self.hotkey_factory:
            try:
                hk = self.hotkey_factory("Ctrl+Shift+P", self.app.privacy_key, hotkey_id=0xCC02)
                if hk.start(timeout=2):
                    self._privacy_hotkey = hk
            except Exception as e:
                log.warning("SSMS dock: Ctrl+Shift+P not passed on (%s)", type(e).__name__)
        elif not want and self._privacy_hotkey is not None:
            hk, self._privacy_hotkey = self._privacy_hotkey, None
            threading.Thread(target=hk.stop, daemon=True).start()

    def _tidy(self):
        """While docked: SSMS's resize-glow windows stay hidden (they'd float where the old frame was), and a new
        SSMS window (connect dialog, options, a message) is brought in front of Command Center."""
        pid = self.pid
        current = _windows_of(pid)
        form_hwnd = int(self._form().Handle.ToInt64()) if self._form() else 0
        for h in current:
            cls = win32gui.GetClassName(h)
            if cls == GLOW_CLASS:
                win32gui.ShowWindow(h, win32con.SW_HIDE)
            elif h not in self._seen and cls not in NOT_DIALOGS and h != self.hwnd and not self._above(h, form_hwnd):
                # right above Command Center (not above whatever app Joe is in now); to the front if CC is
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
        """Is top-level window h above the Command Center window in the z-order?"""
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
