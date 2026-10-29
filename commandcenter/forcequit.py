"""Force quit: "Force quit Command Center" in the taskbar button's right-click menu (a jump-list task the desktop app
registers at start) runs `python -m commandcenter kill`, a separate process that ends a Command Center that's stuck,
without losing Joe's docked VS Code or SSMS windows.

1. Finds the running copy from data/instance-<port>.json (its pid and handoff secret; the folder is private to this
   user). The pid must still be that copy: our instance mutex exists, the process is a Python of this Windows user, and
   it started before the instance file was written (a recycled pid started later).
2. Asks it to quit the normal way (POST /api/app/quit with the handoff secret). A copy whose server still answers then
   puts its docked windows back itself and records them for the next start. Up to GRACE seconds.
3. Still running: puts back, from outside, every docked window (the top window of an installed, signed Code.exe or
   Ssms.exe of this user whose parent is one of Command Center's windows) as a normal top-level window: the one showing
   maximized, the others minimized. Each call is bounded (a hung parent can't hold this up), and if one still can't be
   put back the force quit goes ahead (that window would then close, as with Task Manager's End task).
4. Ends the process, then its leftover helpers that are ours alone (image under the app folder, or a command line
   naming the data folder: the Teams reader's Edge, the web VS Code server). Code.exe, SSMS and Windows Terminal are
   never touched.

The docked VS Code windows are already recorded (kv vscode:dock, written on every dock) and SSMS too (kv ssms:dock), so
the next start docks them again. Windows' own "End task" in the taskbar menu, or Task Manager, kills outright: docked
windows close with Command Center. Logs carry counts only.
"""
import ctypes
import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request

import win32api
import win32con
import win32event
import win32gui
import win32process

from . import config, security

log = logging.getLogger(__name__)

GRACE = 15          # s for a normal quit before forcing
CALL_TIMEOUT = 4    # s for each Win32 call on a docked window (its old parent may be hung)
TASK_TITLE = "Force quit Command Center"
SYNCHRONIZE, PROCESS_TERMINATE = 0x00100000, 0x0001
OVERLAPPED = (win32con.WS_CAPTION | win32con.WS_THICKFRAME | win32con.WS_SYSMENU | win32con.WS_MINIMIZEBOX
              | win32con.WS_MAXIMIZEBOX)
KEEP = {"code.exe", "ssms.exe", "windowsterminal.exe", "wt.exe", "openconsole.exe"}


# ---- the jump list (registered by the desktop app) ----------------------------------------------
def register_jump_list(app_id: str, icon: str):
    """Adds the Force quit task to the taskbar button's (and Start menu entry's) right-click menu. Runs on its own
    thread with its own COM apartment, so it never touches the window's."""
    def work():
        import pythoncom
        from win32comext.propsys import propsys, pscon
        from win32comext.shell import shell
        pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
        try:
            dl = pythoncom.CoCreateInstance(shell.CLSID_DestinationList, None, pythoncom.CLSCTX_INPROC_SERVER,
                                            shell.IID_ICustomDestinationList)
            dl.SetAppID(app_id)
            dl.BeginList()
            tasks = pythoncom.CoCreateInstance(shell.CLSID_EnumerableObjectCollection, None,
                                               pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IObjectCollection)
            link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER,
                                              shell.IID_IShellLink)
            link.SetPath(str(config.ROOT / ".venv" / "Scripts" / "pythonw.exe"))
            link.SetArguments("-m commandcenter kill")
            link.SetWorkingDirectory(str(config.ROOT))
            link.SetIconLocation(icon, 0)
            link.SetDescription("Ends Command Center even if it isn't responding; docked VS Code and SSMS stay open")
            store = link.QueryInterface(propsys.IID_IPropertyStore)
            store.SetValue(pscon.PKEY_Title, propsys.PROPVARIANTType(TASK_TITLE))
            store.Commit()
            tasks.AddObject(link)
            dl.AddUserTasks(tasks)
            dl.CommitList()
        except Exception as e:
            log.warning("Taskbar Force quit task not registered (%s)", type(e).__name__)
        finally:
            pythoncom.CoUninitialize()
    t = threading.Thread(target=work, name="jump-list", daemon=True)
    t.start()
    return t


# ---- finding the running copy ---------------------------------------------------------------------
def _mutex_exists(port: int) -> bool:
    try:
        win32event.OpenMutex(SYNCHRONIZE, False, f"Local\\Sonance.CommandCenter.{security.MY_SID_STR}.{port}").Close()
        return True
    except Exception:
        return False


def _started(pid: int) -> float | None:
    """The process's start time (Unix seconds), or None if it isn't running."""
    try:
        h = win32api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    except Exception:
        return None
    try:
        if win32process.GetExitCodeProcess(h) != 259:  # STILL_ACTIVE
            return None
        created = win32process.GetProcessTimes(h)["CreationTime"]
        return created.timestamp()
    finally:
        h.Close()


def find_running(port: int) -> dict | None:
    """{pid, handoff, file} of the running copy on this port, or None."""
    from .ssmsdock import _image
    path = config.DATA_DIR / f"instance-{port}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        written = path.stat().st_mtime
        pid = int(data["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    started = _started(pid)
    if started is None or started > written + 2 or not _mutex_exists(port):
        return None  # gone, or the pid now belongs to something that started after the file was written
    if not os.path.basename(_image(pid)).lower().startswith("python") or not security.pid_is_current_user(pid):
        return None
    return {"pid": pid, "handoff": str(data.get("handoff") or ""), "file": path}


# ---- steps ------------------------------------------------------------------------------------------
def _ask_to_quit(port: int, handoff: str) -> bool:
    if not handoff:
        return False
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/app/quit", data=b"{}", method="POST",
                                 headers={"X-CommandCenter": "1", "Content-Type": "application/json",
                                          "X-CC-Handoff": handoff})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def _wait_exit(pid: int, seconds: float) -> bool:
    try:
        h = win32api.OpenProcess(SYNCHRONIZE, False, pid)
    except Exception:
        return True
    try:
        return win32event.WaitForSingleObject(h, int(seconds * 1000)) == win32event.WAIT_OBJECT_0
    finally:
        h.Close()


def _bounded(fn, *args) -> bool:
    """Runs one Win32 call on a worker thread; False if it raised or didn't return within CALL_TIMEOUT."""
    out = []

    def run():
        try:
            fn(*args)
            out.append(True)
        except Exception:
            out.append(False)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(CALL_TIMEOUT)
    return bool(out and out[0])


def docked_windows(pid: int) -> list[tuple[int, int]]:
    """(hwnd, owner pid) of every window docked into Command Center's windows: the top foreign window of an installed
    VS Code or SSMS of this user, i.e. whose parent belongs to Command Center's process."""
    from .codedock import is_our_code
    from .ssmsdock import is_our_ssms
    tops = []
    win32gui.EnumWindows(lambda h, _: tops.append(h) or True, None)
    found, checked = [], {}
    for top in tops:
        if win32process.GetWindowThreadProcessId(top)[1] != pid:
            continue
        kids = []
        try:
            win32gui.EnumChildWindows(top, lambda h, _: kids.append(h) or True, None)
        except Exception:
            continue  # no children
        for h in kids:
            owner = win32process.GetWindowThreadProcessId(h)[1]
            if owner == pid or win32process.GetWindowThreadProcessId(win32gui.GetParent(h))[1] != pid:
                continue
            if owner not in checked:
                checked[owner] = is_our_code(owner) or is_our_ssms(owner)
            if checked[owner]:
                found.append((h, owner))
    return found


def put_back(hwnd: int) -> bool:
    """One docked window back as a normal top-level window (its saved styles live in the stuck process, so the standard
    frame is given back). The one showing comes back maximized, hidden ones minimized. Never closed."""
    showing = win32gui.IsWindowVisible(hwnd)
    if not _bounded(win32gui.SetParent, hwnd, 0):
        return False
    style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
    style = (style & ~(win32con.WS_CHILD | win32con.WS_POPUP)) | OVERLAPPED
    win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, ctypes.c_int32(style & 0xFFFFFFFF).value)
    ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) | win32con.WS_EX_APPWINDOW | win32con.WS_EX_WINDOWEDGE
    win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ctypes.c_int32(ex & 0xFFFFFFFF).value)
    flags = (win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOZORDER | win32con.SWP_FRAMECHANGED
             | win32con.SWP_NOACTIVATE | 0x4000)  # SWP_ASYNCWINDOWPOS
    _bounded(win32gui.SetWindowPos, hwnd, 0, 0, 0, 0, 0, flags)
    _bounded(win32gui.ShowWindow, hwnd, win32con.SW_SHOWMAXIMIZED if showing else win32con.SW_SHOWMINNOACTIVE)
    return win32gui.GetParent(hwnd) == 0


def _helpers(pid: int) -> list[int]:
    """Descendants of pid that are Command Center's own helpers (see the module docstring), deepest last."""
    import win32com.client
    rows = win32com.client.GetObject("winmgmts:").ExecQuery(
        "SELECT ProcessId, ParentProcessId, Name, ExecutablePath, CommandLine FROM Win32_Process")
    procs = {r.ProcessId: r for r in rows}
    kids: dict[int, list[int]] = {}
    for r in procs.values():
        kids.setdefault(r.ParentProcessId, []).append(r.ProcessId)
    root, data = str(config.ROOT).lower(), str(config.DATA_DIR).lower()
    out, todo = [], list(kids.get(pid, []))
    while todo:
        p = todo.pop(0)
        r = procs.get(p)
        if not r or (r.Name or "").lower() in KEEP:
            continue  # VS Code, SSMS, Windows Terminal and everything they started stay
        exe, cmd = (r.ExecutablePath or "").lower(), (r.CommandLine or "").lower()
        if (exe.startswith(root) or data in cmd) and security.pid_is_current_user(p):
            out.append(p)
        todo += kids.get(p, [])
    return out


def _terminate(pid: int) -> bool:
    try:
        h = win32api.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, pid)
    except Exception:
        return False
    try:
        win32api.TerminateProcess(h, 1)
        return win32event.WaitForSingleObject(h, 5000) == win32event.WAIT_OBJECT_0
    finally:
        h.Close()


def force_quit(running: dict, port: int, grace: float = GRACE) -> dict:
    pid = running["pid"]
    result = {"graceful": False, "kept": 0, "lost": 0, "helpers": 0, "ended": False}
    try:
        helpers_before = _helpers(pid)
    except Exception:
        helpers_before = []
    if _ask_to_quit(port, running["handoff"]) and _wait_exit(pid, grace):
        result.update(graceful=True, ended=True)
        log.info("Force quit: Command Center quit normally")
        return result
    for hwnd, _owner in docked_windows(pid):
        if put_back(hwnd):
            result["kept"] += 1
        else:
            result["lost"] += 1
    result["ended"] = _terminate(pid) or _wait_exit(pid, 1)
    for p in helpers_before:
        if _started(p) is not None and _terminate(p):
            result["helpers"] += 1
    if result["ended"]:
        try:
            running["file"].unlink()
        except OSError:
            pass
    log.info("Force quit: process %s, %d docked window(s) kept open, %d not, %d helper(s) ended",
             "ended" if result["ended"] else "NOT ended", result["kept"], result["lost"], result["helpers"])
    return result


# ---- the command ---------------------------------------------------------------------------------------
def _box(text: str, flags: int) -> int:
    return ctypes.windll.user32.MessageBoxW(None, text, TASK_TITLE, flags | win32con.MB_TOPMOST | win32con.MB_SETFOREGROUND)


def main(confirm: bool = True) -> int:
    port = security.port()
    running = find_running(port)
    if not running:
        _box("Command Center isn't running.\n\nIf a Command Center window is still on screen, end it in Task Manager.",
             win32con.MB_ICONINFORMATION) if confirm else None
        return 1
    if confirm and _box("Force quit Command Center?\n\nIt closes even if it isn't responding. Docked VS Code and SSMS "
                        "windows stay open as normal windows; unsaved Command Center text is lost.",
                        win32con.MB_YESNO | win32con.MB_ICONWARNING | win32con.MB_DEFBUTTON2) != win32con.IDYES:
        return 1
    r = force_quit(running, port)
    if not r["ended"]:
        _box("Command Center couldn't be ended. Use Task Manager (Details: pythonw.exe).", win32con.MB_ICONERROR)
        return 2
    if not confirm:
        return 0
    kept = "" if r["graceful"] or not (r["kept"] or r["lost"]) else (
        f"\n\n{r['kept']} docked window(s) kept open." + (f" {r['lost']} couldn't be saved." if r["lost"] else ""))
    if _box(f"Command Center has been closed.{kept}\n\nStart it again now?",
            win32con.MB_YESNO | win32con.MB_ICONINFORMATION) == win32con.IDYES:
        subprocess.Popen([str(config.ROOT / ".venv" / "Scripts" / "pythonw.exe"), "-m", "commandcenter", "app"],
                         cwd=str(config.ROOT), close_fds=True,
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    return 0


if __name__ == "__main__":
    sys.exit(main())
