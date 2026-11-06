"""ScreenConnect tab (WORK; static/screenconnect.js; Joe's call 2026-11-06): the remote-session windows of the
installed ScreenConnect (ConnectWise) client, docked into the main window's ScreenConnect tab, several at once (one
shows over the tab's stage, a button each on the bar).

Floating dock, like outlookdock.py, so killing Command Center never ends a remote session: a session window is never
made a child of Command Center. It stays a top-level window of its own process with its frame and taskbar button taken
off (caption / frame styles stripped, WS_EX_TOOLWINDOW), and Command Center holds it over the tab's stage: placed in
screen coordinates from the bounds channel (overlay.NativeOverlay), kept just above the main window in the z-order
(re-placed when the main window moves, resizes or comes to the front, and checked twice a second), clipped around
Command Center's toasts and menus with a window region and hidden whenever the tab isn't on show. The other docked
sessions stay docked, hidden, behind it. If Command Center ends without letting go, the dock guard (dockguard.py)
gives them back their frame; the next start docks them again from the record (kv `screenconnect:dock`: handle, pid and
the styles to put back; sealed).

Which windows: top-level, visible, unowned WinForms windows with a caption (the session window; its own toolbars and
dialogs are owned by it) of a ScreenConnect.WindowsClient.exe that runs as this Windows user, from an installed client
folder (%ProgramFiles(x86)% / %ProgramFiles% / %LOCALAPPDATA% "ScreenConnect Client (<16 hex>)", or a ClickOnce copy
under %LOCALAPPDATA%\\Apps\\2.0), whose file carries a valid Authenticode signature from ConnectWise, LLC (checked
before any dock).

Host or guest: this PC also runs the ScreenConnect guest agent (ScreenConnect.ClientService.exe as SYSTEM), whose
helpers in Joe's session are the same WindowsClient.exe started as `RunRole <guid> User`. A session Joe joins from the
ScreenConnect web page is started through the client's URL scheme, so its command line is a single
`sc-<16 hex>://<relay>/<session>/...` address. Only those (or a command line with y=Host) are host sessions, docked
by themselves when they appear (the "Dock new sessions here" switch, kv `screenconnect:autodock`, default on); Dock
running session may take any qualifying window. The command line is read (NtQueryInformationProcess) and only
classified: it carries the session's access key, so it's never logged, stored or sent to the page.

Never started, closed or killed here. Let go of (a normal window again): Undock (the one showing), Lock (minimized;
docked again after unlock), Restart (minimized) and Quit (shown; both docked again at the next start), the main
window's form going away, interpreter exit, unhandled exceptions and Windows ending the session. A session window that
closes (the session ended) just drops off the list.

Logs carry pids and counts only, never window titles (a title names the remote PC or the customer).
"""
import atexit
import ctypes
import logging
import os
import re
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

import win32con
import win32gui
import win32process

from . import db, dockguard, security
from .outlookdock import _guard_file
from .overlay import NativeOverlay
from .ssmsdock import STRIP_EXSTYLE, STRIP_STYLE, SWP_ASYNC, _image, _s32, verify_exe

log = logging.getLogger(__name__)

EXE = "screenconnect.windowsclient.exe"
SIGNER_ORG = "ConnectWise, LLC"  # the signing certificate's O= (checked 2026-11-06 on build b122a5faa1d17fca)
KV_DOCK = "screenconnect:dock"
KV_AUTO = "screenconnect:autodock"
CLIENT_DIR = re.compile(r"^ScreenConnect Client \([0-9a-f]{16}\)$", re.I)
SCHEME_ARG = re.compile(r"^sc-[0-9a-f]{16}://", re.I)
FORMS_CLASS = "WindowsForms10.Window."
MIN_W, MIN_H = 200, 120
SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x0002, 0x0001, 0x0010, 0x0020
SWP_SHOWWINDOW, SWP_HIDEWINDOW, SWP_NOZORDER, SWP_NOOWNERZORDER = 0x0040, 0x0080, 0x0004, 0x0200
SW_SHOWNOACTIVATE = 4


# ---- which process / window (plain functions; the class holds them as swappable staticmethods) -------------
def _under(path: str, folder: str | None) -> bool:
    if not folder:
        return False
    a, b = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(folder))
    return a.startswith(b.rstrip("\\/") + os.sep)


def install_ok(image: str) -> bool:
    """The client's file is in an installed ScreenConnect client folder (or a ClickOnce copy of it)."""
    try:
        real = os.path.realpath(image)
    except (OSError, ValueError):
        return False
    p = Path(real)
    if p.name.lower() != EXE:
        return False
    env = os.environ
    roots = [env.get("ProgramFiles(x86)") or r"C:\Program Files (x86)", env.get("ProgramFiles") or r"C:\Program Files",
             env.get("ProgramW6432"), env.get("LOCALAPPDATA")]
    if CLIENT_DIR.match(p.parent.name) and any(
            r and os.path.normcase(os.path.abspath(str(p.parent.parent))) == os.path.normcase(os.path.abspath(r)) for r in roots):
        return True
    local = env.get("LOCALAPPDATA")
    return bool(local) and _under(real, os.path.join(local, "Apps", "2.0"))


def is_our_client(pid: int) -> bool:
    """A ScreenConnect.WindowsClient.exe of this Windows user, from an installed client folder (the signature is
    checked on its file before docking)."""
    image = _image(pid)
    return bool(image) and os.path.basename(image).lower() == EXE and install_ok(image) and security.pid_is_current_user(pid)


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_k32.CloseHandle.argtypes = (wintypes.HANDLE,)
_k32.LocalFree.argtypes = (ctypes.c_void_p,)
_nt = ctypes.WinDLL("ntdll")
_nt.NtQueryInformationProcess.restype = ctypes.c_long
_nt.NtQueryInformationProcess.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG,
                                          ctypes.POINTER(wintypes.ULONG))
_sh32 = ctypes.WinDLL("shell32")
_sh32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
_sh32.CommandLineToArgvW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int))


class _USTR(ctypes.Structure):
    _fields_ = [("Length", ctypes.c_ushort), ("MaximumLength", ctypes.c_ushort), ("Buffer", ctypes.c_void_p)]


def command_line(pid: int) -> str:
    """A process's command line (ProcessCommandLineInformation, Windows 8.1+), "" if it can't be read. Only ever
    classified (session_role): it carries the session's key."""
    h = _k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        size = wintypes.ULONG(0)
        _nt.NtQueryInformationProcess(h, 60, None, 0, ctypes.byref(size))
        if not 0 < size.value <= 1 << 20:
            return ""
        buf = ctypes.create_string_buffer(size.value)
        if _nt.NtQueryInformationProcess(h, 60, buf, size, ctypes.byref(size)) != 0:
            return ""
        us = _USTR.from_buffer(buf)
        return ctypes.wstring_at(us.Buffer, us.Length // 2) if us.Buffer and us.Length else ""
    finally:
        _k32.CloseHandle(h)


def split_args(cmd: str) -> list[str]:
    if not cmd:
        return []
    n = ctypes.c_int(0)
    argv = _sh32.CommandLineToArgvW(cmd, ctypes.byref(n))
    if not argv:
        return []
    try:
        return [argv[i] for i in range(n.value)]
    finally:
        _k32.LocalFree(argv)


def classify(args: list[str]) -> str:
    """"host" (a session Joe joined: the client started through its sc-<16 hex>:// URL scheme, or y=Host), "guest"
    (the guest agent's helpers: RunRole, or y=Guest) or "other". args: the command line without the program."""
    low = [a.lower() for a in args]
    joined = " ".join(low)
    if "runrole" in low or re.search(r"(^|[?&])y=guest(&|$|\s)", joined):
        return "guest"
    if re.search(r"(^|[?&])y=host(&|$|\s)", joined) or any(SCHEME_ARG.match(a) for a in args):
        return "host"
    return "other"


def session_role(pid: int) -> str:
    return classify(split_args(command_line(pid))[1:])


def client_processes() -> list[int]:
    """Pids of our clients that have a visible top-level window (cheap: only those pids' files are looked at)."""
    pids = set()

    def visit(h, _):
        if win32gui.IsWindowVisible(h):
            pids.add(win32process.GetWindowThreadProcessId(h)[1])
        return True
    win32gui.EnumWindows(visit, None)
    return [pid for pid in pids if pid and _cached_ours(pid)]


_ours_cache: dict[int, tuple[float, bool]] = {}


def _cached_ours(pid: int) -> bool:
    """is_our_client for the watcher's scan, remembered for a minute per pid (a dock checks again, uncached)."""
    now = time.monotonic()
    hit = _ours_cache.get(pid)
    if hit and now - hit[0] < 60:
        return hit[1]
    if len(_ours_cache) > 512:
        _ours_cache.clear()
    ok = is_our_client(pid)
    _ours_cache[pid] = (now, ok)
    return ok


def session_windows(pid: int) -> list[int]:
    """The process's session windows: top-level, visible, no owner, a WinForms window with a caption, not a tool
    window, at least 200 x 120. Largest first (the session itself)."""
    out = []

    def visit(h, _):
        if win32process.GetWindowThreadProcessId(h)[1] != pid or not win32gui.IsWindowVisible(h):
            return True
        if win32gui.GetWindow(h, win32con.GW_OWNER) or not win32gui.GetClassName(h).startswith(FORMS_CLASS):
            return True
        style, ex = win32gui.GetWindowLong(h, win32con.GWL_STYLE), win32gui.GetWindowLong(h, win32con.GWL_EXSTYLE)
        if style & win32con.WS_CAPTION != win32con.WS_CAPTION or ex & win32con.WS_EX_TOOLWINDOW or style & win32con.WS_CHILD:
            return True
        left, top, right, bottom = win32gui.GetWindowRect(h)
        if right - left >= MIN_W and bottom - top >= MIN_H:
            out.append((-(right - left) * (bottom - top), h))
        return True
    win32gui.EnumWindows(visit, None)
    return [h for _, h in sorted(out)]


def _check_exe(pid: int):
    ok, why = verify_exe(Path(_image(pid)), "ScreenConnect", org=SIGNER_ORG, signer="ConnectWise")
    if not ok:
        raise ValueError(why)


def _title(hwnd: int) -> str:
    try:
        return win32gui.GetWindowText(hwnd)[:200]  # no message to the window (another process's): can't hang
    except Exception:
        return ""


class ScreenConnectDock(NativeOverlay):
    NAME = "ScreenConnect dock"
    SITES = ("main",)
    HOSTS = ("main",)
    INTERVAL_MS = 500
    WATCH_S = 1.5      # how often the watcher looks for new session windows (auto-dock on)
    WATCH_OFF_S = 5    # ...with auto-dock off (only the bar's list)
    # tests swap these for a stand-in window (never Joe's ScreenConnect)
    is_ours = staticmethod(is_our_client)
    processes = staticmethod(client_processes)
    find_windows = staticmethod(session_windows)
    role = staticmethod(session_role)
    check_exe = staticmethod(_check_exe)
    image_name = EXE

    def __init__(self, app):
        super().__init__(app)
        self.docked: dict[int, dict] = {}   # hwnd -> {"pid", "saved"}, in docking order
        self.active = 0                      # the one over the stage
        self.pending: list[tuple[int, int]] = []  # (hwnd, pid) to dock again once unlocked (Lock, the last start)
        self._hints: dict[int, dict] = {}    # hwnd -> the styles recorded at the last dock (a window the guard missed)
        self._want_active = 0                # the recorded active one, shown again when it's redocked
        self._restored = False
        self._seen: set[int] = set()         # windows auto-dock has seen (never docked by itself again)
        self._baselined = False
        self._cands: tuple[float, list] = (0.0, [])
        self._roles: dict[int, tuple[float, str]] = {}
        self.note = None
        self.hung = False
        self.auto_seq, self.auto_title = 0, ""
        self._placed = None
        self._rgn_key = None
        self._shown: set[int] = set()
        self._timer = None
        self._hooked = set()
        self._dock_lock = threading.RLock()
        self._hooks = False
        self._watcher = None

    # ---- settings ----------------------------------------------------------------------------
    @staticmethod
    def autodock() -> bool:
        try:
            v = db.kv_get(KV_AUTO)
        except Exception:
            v = None
        return v is not False

    def _role(self, pid: int) -> str:
        hit = self._roles.get(pid)
        if hit and time.monotonic() - hit[0] < 300:
            return hit[1]
        try:
            r = self.role(pid)
        except Exception:
            r = "other"
        if len(self._roles) > 256:
            self._roles.clear()
        self._roles[pid] = (time.monotonic(), r)
        return r

    def _scan(self) -> list[dict]:
        """Qualifying session windows not docked here: [{hwnd, pid, role}]."""
        out = []
        for pid in self.processes():
            role = None
            for h in self.find_windows(pid):
                if h in self.docked:
                    continue
                role = role or self._role(pid)
                out.append({"hwnd": h, "pid": pid, "role": role})
        self._cands = (time.monotonic(), out)
        return out

    def _candidates(self, max_age=2.0) -> list[dict]:
        at, cands = self._cands
        cands = cands if time.monotonic() - at < max_age else self._scan()
        return [c for c in cands if c["hwnd"] not in self.docked and win32gui.IsWindow(c["hwnd"])]

    # ---- from the server thread ----------------------------------------------------------
    def status(self) -> dict:
        available = self.available()
        cands = self._candidates() if available else []
        with self._dock_lock:
            docked = [{"hwnd": h, "pid": e["pid"], "title": _title(h), "active": h == self.active} for h, e in self.docked.items()]
        return {"available": available, "docked": docked, "hung": self.hung, "note": self.note,
                "running": [{"hwnd": c["hwnd"], "pid": c["pid"], "title": _title(c["hwnd"]), "host": c["role"] == "host"}
                            for c in cands],
                "autodock": self.autodock(), "restoring": bool(self.pending) and not docked,
                "auto": {"seq": self.auto_seq, "title": self.auto_title}}

    @staticmethod
    def _hwnd_of(body) -> int:
        h = body.get("hwnd")
        if not isinstance(h, int) or isinstance(h, bool) or not 0 < h < 2 ** 48:
            raise ValueError("hwnd must be a window handle")
        return h

    def handle_action(self, body: dict) -> dict:
        name = str(body.get("action") or "")
        if name == "autodock":
            on = body.get("on")
            if not isinstance(on, bool):
                raise ValueError("on must be true or false")
            if on:  # sessions open now stay where they are; only new ones dock by themselves
                self._seen |= {c["hwnd"] for c in self._scan()}
            db.kv_set(KV_AUTO, on)
            log.info("ScreenConnect dock: docking new sessions %s", "on" if on else "off")
            return {"ok": True, "autodock": on}
        if not self.available():
            raise ValueError("ScreenConnect sessions dock in the Command Center desktop app")
        if name == "dock":
            hwnd = self._hwnd_of(body)
            cand = next((c for c in self._scan() if c["hwnd"] == hwnd), None)
            if cand is None:
                raise ValueError("That isn't a ScreenConnect session window of yours (it may have closed)")
            self.check_exe(cand["pid"])
            self._seen.add(hwnd)
            self._invoke(lambda: self._dock(hwnd, cand["pid"]))
            return {"ok": True}
        if name == "show":
            hwnd = self._hwnd_of(body)
            with self._dock_lock:
                if hwnd not in self.docked:
                    raise ValueError("That session isn't docked here")
                self._set_active(hwnd)
            self._queue()
            return {"ok": True}
        if name == "undock":
            hwnd = self._hwnd_of(body) if body.get("hwnd") is not None else self.active
            if hwnd in self.docked:
                self.release(hwnd, "Undock", front=True)  # (not under the lock: a hung client can't stall the UI thread)
            self._queue()
            return {"ok": True}
        raise ValueError("unknown action")

    # ---- records (any thread) -----------------------------------------------------------------
    def _persist(self, windows=None):
        """kv screenconnect:dock: what to dock again at the next start (None once nothing is docked)."""
        with self._dock_lock:
            rows = windows if windows is not None else [
                {"hwnd": h, "pid": e["pid"], "saved": e["saved"], "active": h == self.active} for h, e in self.docked.items()]
        try:
            db.kv_set(KV_DOCK, rows or None)
        except Exception as e:
            log.warning("ScreenConnect dock: couldn't record the docked windows (%s)", type(e).__name__)

    def _guard_sync(self):
        """Tells the dock guard what to put back if Command Center ends now (beside Outlook's entry)."""
        with self._dock_lock:
            rows = [{"hwnd": h, "pid": e["pid"], "image": self.image_name, "style": e["saved"]["style"],
                     "exstyle": e["saved"]["exstyle"], "placement": e["saved"]["placement"]} for h, e in self.docked.items()]
        try:
            dockguard.set_entries(_guard_file(), os.getpid(), "screenconnect", rows)
        except Exception as e:
            log.warning("ScreenConnect dock: dock guard not updated (%s)", type(e).__name__)

    def _set_active(self, hwnd: int):
        if hwnd != self.active:
            self.active = hwnd
            self._placed, self._rgn_key = None, None
            self.hung = False

    # ---- putting windows back (any thread: plain Win32 calls on the client's windows) --------------
    def release(self, hwnd: int, why: str, minimize=False, front=False, forget=True) -> bool:
        """One session window back as a normal top-level window with its own styles and placement. Never closed.
        forget: off the record too (Undock); else the record stands (Lock, Restart, Quit)."""
        with self._dock_lock:
            e = self.docked.pop(hwnd, None)
            self._shown.discard(hwnd)
            self._cands = (0.0, [])
            if self.active == hwnd:
                self._set_active(next(reversed(self.docked), 0) if self.docked else 0)
        if not e:
            return False
        self._seen.add(hwnd)  # auto-dock never takes it back by itself
        saved = e["saved"]
        try:
            if not win32gui.IsWindow(hwnd):
                return False
            ctypes.windll.user32.SetWindowRgn(ctypes.c_void_p(hwnd), None, True)
            win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, _s32(saved["style"]))
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, _s32(saved["exstyle"]))
            flags, show, ptmin, ptmax, normal = saved["placement"]
            show_cmd = win32con.SW_SHOWMINNOACTIVE if minimize else (
                show if show in (win32con.SW_SHOWMAXIMIZED, win32con.SW_SHOWNORMAL) else win32con.SW_SHOWNORMAL)
            if not front and show_cmd == win32con.SW_SHOWNORMAL:
                show_cmd = SW_SHOWNOACTIVATE  # never take the keyboard unless Joe asked for it (Undock)
            win32gui.SetWindowPlacement(hwnd, (flags, show_cmd, tuple(ptmin), tuple(ptmax), tuple(normal)))
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_FRAMECHANGED
                                  | SWP_NOACTIVATE | SWP_SHOWWINDOW)
            if front and not minimize:
                try:
                    win32gui.SetForegroundWindow(hwnd)
                except Exception:
                    pass
            log.info("ScreenConnect dock: a session window (pid %s) is a normal window again (%s)", e["pid"], why)
            return True
        except Exception as ex:
            log.warning("ScreenConnect dock: couldn't put a session window back cleanly (%s)", type(ex).__name__)
            return False
        finally:
            self._guard_sync()
            if forget:
                self._persist()

    def release_keep(self, why: str, minimize=False):
        """Let go of every docked session but dock them again (Lock: after unlock; Restart / Quit / exit hooks: at the
        next start, from the record)."""
        with self._dock_lock:
            if not self.docked:
                return
            rows = [{"hwnd": h, "pid": e["pid"], "saved": e["saved"], "active": h == self.active} for h, e in self.docked.items()]
            active = self.active
            self.pending = [(r["hwnd"], r["pid"]) for r in rows]
            self._want_active = active
        self._persist(rows)
        for r in rows:  # the one showing comes back as it was, the others minimized
            self.release(r["hwnd"], why, minimize=minimize or r["hwnd"] != active, forget=False)

    def _restore(self):
        """Once per start: the session windows recorded at the last Restart / Quit / Lock (or still floating-docked
        when Command Center was killed), if they're still that client's top-level windows, are docked again."""
        self._restored = True
        try:
            rows = db.kv_get(KV_DOCK)
        except Exception:
            rows = None
        if not isinstance(rows, list):
            return
        keep = []
        for e in rows[:20]:
            if not isinstance(e, dict):
                continue
            h, pid = e.get("hwnd"), e.get("pid")
            if not isinstance(h, int) or not isinstance(pid, int) or h in self.docked or not win32gui.IsWindow(h):
                continue
            try:
                if win32process.GetWindowThreadProcessId(h)[1] != pid or win32gui.GetParent(h) != 0 or not self.is_ours(pid):
                    continue
            except Exception:
                continue
            saved = e.get("saved")
            if isinstance(saved, dict) and {"style", "exstyle", "placement"} <= set(saved):
                self._hints[h] = saved
            keep.append(e)
            if e.get("active"):
                self._want_active = h
        self.pending += [(e["hwnd"], e["pid"]) for e in keep]
        if len(keep) != len(rows):
            self._persist([e for e in keep])
        if keep:
            log.info("ScreenConnect dock: %d session window(s) to dock again", len(keep))

    def install_hooks(self):
        """Last lines of defence (beyond those, the dock guard), and the watcher that docks new sessions."""
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
            log.warning("ScreenConnect dock: no .NET exception hook (%s)", type(e).__name__)
        try:
            from Microsoft.Win32 import SessionEndingEventHandler, SystemEvents
            SystemEvents.SessionEnding += SessionEndingEventHandler(lambda s, e: self.release_keep("Windows is signing out"))
        except Exception as e:
            log.warning("ScreenConnect dock: no session-ending hook (%s)", type(e).__name__)
        self.start_watch()

    # ---- the watcher (its own thread: no UI-thread time unless something is to be docked) ------------------
    def start_watch(self):
        if self._watcher is None:
            self._watcher = threading.Thread(target=self._watch, name="screenconnect-watch", daemon=True)
            self._watcher.start()

    def _watch(self):
        while not self.app.quitting:
            auto = True
            try:
                auto = self.autodock()
                self.watch_once(auto)
            except Exception as e:
                log.warning("ScreenConnect dock: check failed (%s)", type(e).__name__)
            time.sleep(self.WATCH_S if auto else self.WATCH_OFF_S)

    def watch_once(self, auto: bool = True):
        app = self.app
        if app.quitting or not app.unlocked or app.lock_showing or not self.available():
            return
        if not self._restored:
            self._restore()
        pending, self.pending = self.pending, []
        for hwnd, pid in pending:  # Lock / the last start: dock them again
            try:
                if not win32gui.IsWindow(hwnd) or win32process.GetWindowThreadProcessId(hwnd)[1] != pid or not self.is_ours(pid):
                    continue
                self.check_exe(pid)
            except Exception as e:
                log.info("ScreenConnect dock: a recorded session window wasn't docked again (%s)", type(e).__name__)
                continue
            self._seen.add(hwnd)
            self._invoke(lambda h=hwnd, p=pid: self._dock(h, p, active=h == self._want_active))
        cands = self._scan()
        now = {c["hwnd"] for c in cands}
        if not self._baselined or not auto:
            self._baselined = True
            self._seen |= now  # open before Command Center started (or with the switch off): left where they are
            return
        for c in cands:
            if c["hwnd"] in self._seen:
                continue
            self._seen.add(c["hwnd"])
            if c["role"] != "host":
                continue
            if not self._main_on_screen():
                continue  # Command Center minimized or hidden: the session stays a normal window (and isn't taken later)
            try:
                self.check_exe(c["pid"])
            except ValueError:
                continue
            self._invoke(lambda h=c["hwnd"], p=c["pid"]: self._dock(h, p, auto=True))
        self._seen = {h for h in self._seen if h in now or h in self.docked or win32gui.IsWindow(h)}

    def _main_on_screen(self) -> bool:
        """The main window is shown and not minimized (plain Win32 reads, any thread), so a session docked now is seen:
        the page then opens the ScreenConnect tab."""
        try:
            hwnd = ctypes.c_void_p(int(self.app._hwnd()))
            u = ctypes.windll.user32
            return bool(u.IsWindowVisible(hwnd)) and not u.IsIconic(hwnd)
        except Exception:
            return False

    # ---- UI thread -------------------------------------------------------------------------
    def _panels(self):
        return []  # nothing of Command Center's holds the session windows

    def _hook_form(self, form):
        """The main window moving, resizing, coming to the front or going away: the session follows (or is let go)."""
        key = int(form.Handle.ToInt64())
        if key in self._hooked:
            return
        self._hooked.add(key)
        follow = lambda *_: self._apply()  # noqa: E731
        form.LocationChanged += follow
        form.SizeChanged += follow
        form.Activated += follow
        form.HandleDestroyed += lambda *_: (self._hooked.discard(key), self.release_keep("main window closing"))

    def _dock(self, hwnd: int, pid: int, auto=False, active=True):
        if self.app.quitting or not win32gui.IsWindow(hwnd) or win32process.GetWindowThreadProcessId(hwnd)[1] != pid:
            return
        form = self._form()
        if not self.app.unlocked or self.app.lock_showing or form is None:
            self.pending.append((hwnd, pid))
            return
        if not self.is_ours(pid):
            return
        with self._dock_lock:
            if hwnd in self.docked:
                if active:
                    self._set_active(hwnd)
                self._apply()
                return
            if ctypes.windll.user32.IsHungAppWindow(hwnd):
                self.note = "That session isn't responding; try again when it is."
                return
            hint = self._hints.pop(hwnd, None)
            saved = hint or {"style": win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE),
                             "exstyle": win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE),
                             "placement": win32gui.GetWindowPlacement(hwnd)}
            if win32gui.IsIconic(hwnd) or ctypes.windll.user32.IsZoomed(hwnd):
                win32gui.ShowWindow(hwnd, SW_SHOWNOACTIVATE)  # out of maximized / minimized, without taking the keyboard
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
            exstyle = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, _s32((style & ~STRIP_STYLE & ~win32con.WS_CHILD) | win32con.WS_POPUP))
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, _s32((exstyle & ~STRIP_EXSTYLE) | win32con.WS_EX_TOOLWINDOW))
            win32gui.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
                                  | SWP_FRAMECHANGED | SWP_HIDEWINDOW)  # hidden until placed over the tab
            self.docked[hwnd] = {"pid": pid, "saved": saved}
            self._cands = (0.0, [])  # the bar's list is read again
            self._shown.discard(hwnd)
            if active or not self.active:
                self._set_active(hwnd)
            self.note = None
            if auto:
                self.auto_seq += 1
                self.auto_title = _title(hwnd)
            n = len(self.docked)
        log.info("ScreenConnect dock: docked a session window (pid %s%s; %d docked)", pid, ", new session" if auto else "", n)
        self._persist()   # docked again at the next start
        self._guard_sync()
        self._hook_form(form)
        self._start_timer()
        self._apply()

    def _apply(self):
        form = self._form()
        with self._lock:
            want = dict(self.want)
        with self._dock_lock:
            active = self.active if self.active in self.docked else 0
            others = [h for h in self.docked if h != active]
        for h in others:
            self._hide(h)
        if not active or not win32gui.IsWindow(active):
            return
        import System.Windows.Forms as WinForms
        on = bool(form is not None and self._showing(want) and form.Visible
                  and form.WindowState != WinForms.FormWindowState.Minimized)
        geo = self._geometry(form, want["rect"], want["cuts"]) if on else None
        if geo is None:
            self._hide(active)
            return
        left, top, width, height, holes = geo
        from System.Drawing import Point
        pt = form.PointToScreen(Point(left, top))
        rect = (pt.X, pt.Y, width, height)
        self._region(active, width, height, holes)
        # just above the main window: below whatever is above it now (another app Joe put on top stays there)
        form_hwnd = int(form.Handle.ToInt64())
        above = win32gui.GetWindow(form_hwnd, win32con.GW_HWNDPREV)
        after = win32con.HWND_TOP if not above else (0 if above == active else above)
        flags = SWP_NOACTIVATE | SWP_SHOWWINDOW | SWP_NOOWNERZORDER | SWP_ASYNC | (SWP_NOZORDER if after == 0 else 0)
        if rect != self._placed or active not in self._shown or after != 0:
            win32gui.SetWindowPos(active, after, *rect, flags)
            self._placed = rect
            self._shown.add(active)

    def _hide(self, hwnd, force=False):
        if (force or hwnd in self._shown) and win32gui.IsWindow(hwnd):
            win32gui.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
                                  | SWP_HIDEWINDOW | SWP_ASYNC)
        self._shown.discard(hwnd)

    def _region(self, hwnd, width, height, holes):
        """Clips the session window around Command Center's floating pieces over the stage (a window region on the
        client's window, set only when it changes)."""
        key = (hwnd, width, height, tuple((h.X, h.Y, h.Width, h.Height) for h in holes))
        if key == self._rgn_key:
            return
        self._rgn_key = key
        user32, gdi = ctypes.windll.user32, ctypes.windll.gdi32
        if not holes:
            user32.SetWindowRgn(ctypes.c_void_p(hwnd), None, True)
            return
        rgn = gdi.CreateRectRgn(0, 0, width, height)
        for h in holes:
            hole = gdi.CreateRectRgn(h.X, h.Y, h.X + h.Width, h.Y + h.Height)
            gdi.CombineRgn(rgn, rgn, hole, 4)  # RGN_DIFF
            gdi.DeleteObject(hole)
        if not user32.SetWindowRgn(ctypes.c_void_p(hwnd), rgn, True):  # the window owns it once set
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
            with self._dock_lock:
                if not self.docked:
                    self._timer.Stop()
                    return
                gone = [h for h in self.docked if not win32gui.IsWindow(h) or win32gui.GetParent(h) != 0]
                for h in gone:
                    self.docked.pop(h, None)
                    self._shown.discard(h)
                if self.active not in self.docked:
                    self._set_active(next(reversed(self.docked), 0) if self.docked else 0)
                active = self.active
                others = [h for h in self.docked if h != active]
            if gone:
                self.note = "A session window closed." if len(gone) == 1 else f"{len(gone)} session windows closed."
                log.info("ScreenConnect dock: %d session window(s) closed", len(gone))
                self._persist()
                self._guard_sync()
            for h in others:
                if win32gui.IsWindowVisible(h):
                    self._hide(h, force=True)  # the client showed it again by itself: back behind the active one
            if active:
                self.hung = bool(ctypes.windll.user32.IsHungAppWindow(active))
                if not self.hung and (win32gui.IsIconic(active) or ctypes.windll.user32.IsZoomed(active)):
                    win32gui.ShowWindow(active, SW_SHOWNOACTIVATE)  # Win+Up / Win+Down on it: back over the tab
                    self._placed = None
                if active in self._shown and self._placed:
                    left, top, right, bottom = win32gui.GetWindowRect(active)
                    if (left, top, right - left, bottom - top) != self._placed:
                        self._placed = None  # moved or resized by something else (its own full screen): placed again
                elif active not in self._shown and win32gui.IsWindowVisible(active):
                    self._hide(active, force=True)
            self._apply()
        except Exception as e:
            log.warning("ScreenConnect dock: check failed (%s)", type(e).__name__)
