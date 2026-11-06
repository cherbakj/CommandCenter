"""Dock guard: gives a "floating"-docked window (outlookdock.py, screenconnectdock.py) its frame back if Command Center ends without letting go
of it (Task Manager, a crash, Force quit's kill). Joe's call 2026-11-04.

Why it's needed: a floating-docked window is a normal top-level window of its own app, only stripped of its frame and
taskbar button and held over the tab by Command Center, so it survives Command Center being killed (a docked child
window would be destroyed with it). Left like that it would sit frameless, without a taskbar button. This guard, a tiny
process outside Command Center's process tree, waits for Command Center to end and then puts every window still listed
in the state file back as it was (styles, placement, no clipping region), as Undock does.

State file: data/dockguard-<port>.json (private folder), written by Command Center (write_state) on every dock and
release: {"cc_pid": <pid>, "windows": [{"hwnd", "pid", "image", "style", "exstyle", "placement"}]}, every dock's
windows merged into the one list (set_entries: each dock sets its own by owner name). The guard acts only
on a file still naming its own Command Center pid (a newer copy after a restart has its own guard), and only on a window
that still belongs to that process id with that program name. Standard library only, so it runs with `python -I`.

Run:  pythonw -I dockguard.py <command center pid> <state file>   (Command Center starts it through launch())
"""
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes

SYNCHRONIZE, INFINITE = 0x00100000, 0xFFFFFFFF
GWL_STYLE, GWL_EXSTYLE = -16, -20


def write_state(path, cc_pid: int, windows: list):
    """Command Center: what the guard should put back if it ends now ([] once nothing is floating-docked)."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"cc_pid": cc_pid, "windows": windows}, f)
    os.replace(tmp, path)


# ---- Command Center side: one list per floating dock, merged into the one state file -------------------------
# Several docks float windows (outlookdock.py: Outlook; screenconnectdock.py: ScreenConnect sessions). Each sets its
# own entries by owner name and the file always holds every owner's, so one dock letting go never drops another's
# windows from the guard. One guard process per Command Center process, started with the first entry.
_owners: dict = {}         # owner -> [entry, ...]
_launched: set = set()     # state files whose guard this process has started
_reg_lock = threading.Lock()


def set_entries(path, cc_pid: int, owner: str, windows: list, start=None):
    """Replaces `owner`'s entries ([] or None: none) and writes the merged list. Starts the guard the first time
    there's something to guard (start: the function that does it, launch by default; tests pass their own)."""
    with _reg_lock:
        if windows:
            _owners[owner] = [dict(w) for w in windows]
        else:
            _owners.pop(owner, None)
        merged = [w for name in sorted(_owners) for w in _owners[name]]
        write_state(path, cc_pid, merged)
        key = os.path.normcase(os.path.abspath(str(path)))
        if merged and key not in _launched:
            _launched.add(key)
            (start or launch)(path, cc_pid)
        return merged


def entries(owner: str | None = None) -> list:
    """What's registered now (one owner's, or all of them merged)."""
    with _reg_lock:
        if owner is not None:
            return [dict(w) for w in _owners.get(owner, [])]
        return [dict(w) for name in sorted(_owners) for w in _owners[name]]


def launch(path, cc_pid: int):
    """Command Center: starts the guard outside its own process tree (a launcher that exits at once), so that ending
    Command Center, even "End process tree", doesn't end the guard."""
    me = os.path.abspath(__file__)
    flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    subprocess.Popen([sys.executable, "-I", me, "--launch", str(cc_pid), str(path)], creationflags=flags,
                     close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class _PLACEMENT(ctypes.Structure):
    _fields_ = [("length", wintypes.UINT), ("flags", wintypes.UINT), ("showCmd", wintypes.UINT),
                ("ptMinPosition", _POINT), ("ptMaxPosition", _POINT), ("rcNormalPosition", _RECT)]


def _image(pid: int) -> str:
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
        return buf.value if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else ""
    finally:
        k32.CloseHandle(h)


def restore(w: dict) -> bool:
    """One window back as it was before docking. Never closes it."""
    u = ctypes.windll.user32
    hwnd = ctypes.c_void_p(int(w["hwnd"]))
    if not u.IsWindow(hwnd):
        return False
    pid = wintypes.DWORD()
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value != int(w["pid"]) or os.path.basename(_image(pid.value)).lower() != str(w["image"]).lower():
        return False  # not the window that was docked (the handle may have been reused)
    u.SetWindowRgn(hwnd, None, True)
    s32 = lambda v: ctypes.c_long(((int(v) & 0xFFFFFFFF) ^ 0x80000000) - 0x80000000)  # noqa: E731
    u.SetWindowLongW(hwnd, GWL_STYLE, s32(w["style"]))
    u.SetWindowLongW(hwnd, GWL_EXSTYLE, s32(w["exstyle"]))
    flags, show, ptmin, ptmax, normal = w["placement"]
    wp = _PLACEMENT(ctypes.sizeof(_PLACEMENT), flags, show if show in (1, 3) else 1, _POINT(*ptmin), _POINT(*ptmax), _RECT(*normal))
    u.SetWindowPlacement(hwnd, ctypes.byref(wp))
    u.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0020 | 0x0010 | 0x0040)  # frame changed, show
    return True


def watch(cc_pid: int, path: str):
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(SYNCHRONIZE, False, cc_pid)
    if h:
        k32.WaitForSingleObject(h, INFINITE)
        k32.CloseHandle(h)
    time.sleep(0.5)  # an orderly quit writes its last state just before it ends
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return
    if state.get("cc_pid") != cc_pid or not state.get("windows"):
        return  # let go of cleanly, or a newer Command Center's file
    for w in state["windows"]:
        try:
            restore(w)
        except Exception:
            pass
    try:
        write_state(path, cc_pid, [])
    except OSError:
        pass


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--launch":  # the launcher: start the watcher, then end at once
        flags = 0x00000008 | 0x00000200 | 0x08000000
        subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), sys.argv[2], sys.argv[3]], creationflags=flags,
                         close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif len(sys.argv) == 3:
        watch(int(sys.argv[1]), sys.argv[2])
