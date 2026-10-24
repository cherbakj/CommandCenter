"""Embedded Claude Code terminal for the Projects tab.

Runs the real Claude Code CLI (projects.claude_path()) in a Windows pseudo-console (ConPTY, through
pywinpty) and streams it to xterm.js in the page over a WebSocket.

- One Claude process per project, at most MAX_SESSIONS running at once.
- A session outlives the browser tab: it keeps the last SCROLLBACK characters of output and replays them
  when the page attaches again (tab switch, reload, second window).
- Every process is put in a Windows job object that kills the whole tree when it is closed, so nothing
  Claude started survives "End session", app shutdown, or even a crash of Command Center.

WebSocket /ws/claude?path=<project>&resume=0|1&attach=0|1&cols=&rows=[&unsafe=1]
  unsafe=1 is needed to start Claude in a folder other accounts can modify (projects.writable_by_others).
  client -> server  text JSON  {"type": "input", "data": "..."} | {"type": "resize", "cols": n, "rows": n}
  server -> client  text       terminal output
                    binary     JSON control: attached, replay-start, replay-end, exit, ended, error
  attach=1 only attaches to an existing session (used when the page reconnects); it never starts Claude.
"""
import asyncio
import atexit
import json
import logging
import os
import subprocess
import threading
import time
import winreg
from collections import deque
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, WebSocket, WebSocketDisconnect

from . import projects
from .security import ws_allowed

log = logging.getLogger(__name__)
router = APIRouter()

MAX_SESSIONS = 6
SCROLLBACK = 256 * 1024          # characters of output kept per session for replay
MAX_INPUT = 1024 * 1024          # one input message (a big paste)
MAX_CLIENT_BACKLOG = 4000        # queued frames before a stuck client is dropped (it can reconnect)
NO_WINDOW = subprocess.CREATE_NO_WINDOW

SESSIONS: dict[str, "Session"] = {}
_lock = threading.Lock()         # guards SESSIONS for the atexit path (normally only the event loop touches it)


def _key(path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _int(value, default, lo, hi) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


# ---- process environment ---------------------------------------------------------

# Variables that leak in when Command Center itself was started from a Claude Code / VS Code session.
# They would make Claude think it's nested, connect to the wrong IDE, or carry another session's tokens.
_STRIP_PREFIXES = ("CLAUDECODE", "CLAUDE_CODE_", "CLAUDE_AGENT_SDK_", "CLAUDE_PID", "CLAUDE_EFFORT",
                   "VSCODE_", "ELECTRON_", "TERM_PROGRAM", "CC_", "PYWINPTY_")


def _persistent_env_names() -> set[str]:
    """Names set in the user's or the machine's saved environment (those are the user's own choice)."""
    names = set()
    for hive, sub in ((winreg.HKEY_CURRENT_USER, "Environment"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(hive, sub) as k:
                i = 0
                while True:
                    try:
                        names.add(winreg.EnumValue(k, i)[0].upper())
                    except OSError:
                        break
                    i += 1
        except OSError:
            pass
    return names


def _env_block() -> str:
    keep = _persistent_env_names()
    env = {k: v for k, v in os.environ.items()
           if "=" not in k and "\x00" not in v and (k.upper() in keep or not k.upper().startswith(_STRIP_PREFIXES))}
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    return "\0".join(f"{k}={v}" for k, v in env.items()) + "\0"


def _claude_exe() -> str:
    exe = projects.claude_path()
    if exe and not exe.lower().endswith(".exe"):  # e.g. an npm claude.cmd shim: use the native binary
        exe = projects.bundled_claude()
    if not exe or not Path(exe).is_file():
        raise ValueError("Couldn't find Claude Code (claude.exe). Set projects.claude_path in config.yaml")
    if '"' in exe:
        raise ValueError("projects.claude_path contains a quote character")
    return str(Path(exe).resolve())


# ---- job object: kill the whole process tree ----------------------------------------

def _make_job(pid: int):
    try:
        import win32api, win32con, win32job
        job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, pid)
        try:
            win32job.AssignProcessToJobObject(job, handle)
        finally:
            handle.Close()
        return job
    except Exception:
        log.warning("Couldn't put Claude (pid %s) in a job object; End session falls back to taskkill", pid, exc_info=True)
        return None


def _taskkill(pid: int):
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(int(pid))], capture_output=True, timeout=15,
                       creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        log.warning("taskkill failed for pid %s", pid, exc_info=True)


# ---- sessions ----------------------------------------------------------------------

class Session:
    def __init__(self, path: Path, resume: bool, cols: int, rows: int, loop: asyncio.AbstractEventLoop):
        from winpty import PTY
        self.path, self.key, self.name = path, _key(path), path.name
        self.resume = resume
        self.loop = loop
        self.started = datetime.now()
        self.exit_code = None
        self.alive = True
        self.clients: set[asyncio.Queue] = set()
        self.chunks: deque[str] = deque()
        self.size = 0
        self._killed = False
        exe = _claude_exe()
        args = ["--continue"] if resume else []
        self.pty = PTY(cols, rows)
        # The app path is quoted and the arguments are fixed constants: nothing typed by the user reaches
        # the command line. Input only ever goes to the running process through the console.
        self.pty.spawn(f'"{exe}"', cmdline=(" " + subprocess.list2cmdline(args)) if args else None,
                       cwd=str(path), env=_env_block())
        self.pid = self.pty.pid
        self.job = _make_job(self.pid)
        self._reader = threading.Thread(target=self._read_loop, name=f"claude-pty-{self.pid}", daemon=True)
        self._reader.start()
        log.info("Claude session started for %s (pid %s%s)", self.name, self.pid, ", --continue" if resume else "")

    # reader thread -> event loop
    def _read_loop(self):
        pty = self.pty
        while True:
            try:
                data = pty.read(blocking=True)
            except Exception:
                break  # WinptyError at EOF: the process exited or the console was closed
            if data:
                try:
                    self.loop.call_soon_threadsafe(self._on_output, data)
                except RuntimeError:
                    return  # event loop closed (app shutting down)
        try:
            self.loop.call_soon_threadsafe(self._finish)
        except RuntimeError:
            pass

    def _on_output(self, data: str):
        self.chunks.append(data)
        self.size += len(data)
        while self.size > SCROLLBACK and len(self.chunks) > 1:
            self.size -= len(self.chunks.popleft())
        if self.size > SCROLLBACK:  # one huge chunk
            self.chunks[0] = self.chunks[0][-SCROLLBACK:]
            self.size = len(self.chunks[0])
        for q in list(self.clients):
            self._push(q, ("out", data))

    def _push(self, q: asyncio.Queue, item):
        if q.qsize() > MAX_CLIENT_BACKLOG:
            self.clients.discard(q)
            q.put_nowait(("close", None))
        else:
            q.put_nowait(item)

    def _finish(self):
        """The process ended (on its own or killed). Runs on the event loop."""
        if not self.alive:
            return
        self.alive = False
        pty, self.pty = self.pty, None
        try:
            self.exit_code = pty.get_exitstatus()
        except Exception:
            self.exit_code = None
        try:
            pty.cancel_io()  # unblocks the reader thread so the console is released
        except Exception:
            pass
        log.info("Claude session for %s ended (exit %s)", self.name, self.exit_code)
        self._close_job()  # anything Claude left running (MCP servers, shells) goes too
        for q in list(self.clients):
            self._push(q, ("ctl", {"type": "exit", "code": self.exit_code}))

    def process_alive(self) -> bool:
        pty = self.pty
        try:
            return pty is not None and bool(pty.isalive())
        except Exception:
            return False

    def _close_job(self):
        job, self.job = self.job, None
        if job is not None:
            try:
                import win32job
                win32job.TerminateJobObject(job, 1)
            except Exception:
                pass
            try:
                job.Close()
            except Exception:
                pass

    def kill(self):
        """Kill Claude and every process it started. Blocking; safe to call more than once, from any thread."""
        if self._killed:
            return
        self._killed = True
        if self.process_alive():
            _taskkill(self.pid)
        self._close_job()

    def write(self, data: str):
        pty = self.pty
        if pty is not None:
            pty.write(data)

    def resize(self, cols: int, rows: int):
        pty = self.pty
        if pty is not None:
            pty.set_size(cols, rows)

    # attaching a page
    def attach(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        q.put_nowait(("ctl", {"type": "attached", **self.info()}))
        q.put_nowait(("ctl", {"type": "replay-start"}))
        if self.chunks:
            q.put_nowait(("out", "".join(self.chunks)))
        q.put_nowait(("ctl", {"type": "replay-end"}))
        if not self.alive:
            q.put_nowait(("ctl", {"type": "exit", "code": self.exit_code}))
        self.clients.add(q)
        return q

    def info(self) -> dict:
        return {"path": str(self.path), "name": self.name, "pid": self.pid, "alive": self.alive,
                "exit_code": self.exit_code, "resume": self.resume, "clients": len(self.clients),
                "started": self.started.isoformat(timespec="seconds")}


def _running() -> list[Session]:
    return [s for s in SESSIONS.values() if s.alive]


def _start(path: Path, resume: bool, cols: int, rows: int) -> Session:
    key = _key(path)
    old = SESSIONS.get(key)
    if old and old.alive:
        return old
    if len(_running()) >= MAX_SESSIONS:
        raise ValueError(f"{MAX_SESSIONS} Claude sessions are already running. End one first.")
    session = Session(path, resume, cols, rows, asyncio.get_running_loop())
    with _lock:
        SESSIONS[key] = session
        finished = [s for s in SESSIONS.values() if not s.alive]
        for s in sorted(finished, key=lambda s: s.started)[:max(0, len(finished) - MAX_SESSIONS)]:
            SESSIONS.pop(s.key, None)
    return session


async def _end(key: str) -> bool:
    with _lock:
        session = SESSIONS.pop(key, None)
    if not session:
        return False
    await asyncio.to_thread(session.kill)
    session._finish()
    for q in list(session.clients):
        q.put_nowait(("ctl", {"type": "ended"}))
        q.put_nowait(("close", None))
    session.clients.clear()
    return True


def end_all():
    """Kill every session (app shutdown). Synchronous so it also works from atexit."""
    with _lock:
        sessions = list(SESSIONS.values())
        SESSIONS.clear()
    for s in sessions:
        try:
            s.kill()
        except Exception:
            log.warning("Couldn't end Claude session %s", s.name, exc_info=True)


atexit.register(end_all)


# ---- routes ----------------------------------------------------------------------

@router.get("/api/terminal/sessions")
async def list_sessions():
    return {"sessions": [s.info() for s in SESSIONS.values()], "max": MAX_SESSIONS}


@router.post("/api/terminal/end")
async def end_session(path: str = Body(..., embed=True)):
    if not isinstance(path, str) or len(path) > 400 or "\x00" in path:
        raise HTTPException(400, "Bad path")
    if not await _end(_key(path)):
        raise HTTPException(404, "No Claude session is open for that project")
    return {"ok": True}


async def _sender(ws: WebSocket, q: asyncio.Queue):
    pending = None
    try:
        while True:
            kind, payload = pending or await q.get()
            pending = None
            if kind == "close":
                await ws.close(code=1000)
                return
            if kind == "ctl":
                await ws.send_bytes(json.dumps(payload).encode())
                continue
            parts, size = [payload], len(payload)
            while size < 256 * 1024 and not q.empty():  # coalesce bursts of output into one frame
                nxt = q.get_nowait()
                if nxt[0] != "out":
                    pending = nxt
                    break
                parts.append(nxt[1])
                size += len(nxt[1])
            await ws.send_text("".join(parts))
    except Exception:
        pass  # the page went away; the receive loop cleans up


async def _fail(ws: WebSocket, message: str, code: str | None = None):
    await ws.accept()
    await ws.send_bytes(json.dumps({"type": "error", "message": message, **({"code": code} if code else {})}).encode())
    await ws.close(code=1008)


@router.websocket("/ws/claude")
async def claude_socket(websocket: WebSocket):
    if not await ws_allowed(websocket):
        return
    qp = websocket.query_params
    cols = _int(qp.get("cols"), 120, 20, 1000)
    rows = _int(qp.get("rows"), 30, 5, 500)
    resume = qp.get("resume") == "1"
    attach_only = qp.get("attach") == "1"
    try:
        path = await asyncio.to_thread(projects._known, qp.get("path", ""))
    except ValueError as e:
        return await _fail(websocket, str(e))
    session = SESSIONS.get(_key(path))
    if session is None or (not session.alive and not attach_only):
        if attach_only:
            return await _fail(websocket, "No Claude session is open for that project")
        # Claude runs hooks, MCP servers and settings from the project folder: refuse folders other accounts
        # can change unless the page asked for it after an explicit confirmation (unsafe=1).
        if qp.get("unsafe") != "1" and await asyncio.to_thread(projects.writable_by_others, path):
            return await _fail(websocket, projects.UNSAFE_MESSAGE, code="unsafe_folder")
        try:
            session = _start(path, resume, cols, rows)
        except Exception as e:
            log.warning("Couldn't start Claude for %s", path.name, exc_info=True)
            return await _fail(websocket, str(e) or type(e).__name__)
    await websocket.accept()
    q = session.attach()
    sender = asyncio.create_task(_sender(websocket, q))
    try:
        while True:
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                break
            text = msg.get("text")
            if not text or len(text) > MAX_INPUT + 1024:
                continue
            try:
                m = json.loads(text)
            except ValueError:
                continue
            if not isinstance(m, dict) or not session.alive:
                continue
            if m.get("type") == "input" and isinstance(m.get("data"), str) and 0 < len(m["data"]) <= MAX_INPUT:
                try:
                    await asyncio.to_thread(session.write, m["data"])
                except Exception:
                    pass  # process just exited
            elif m.get("type") == "resize":
                c, r = m.get("cols"), m.get("rows")
                if type(c) is int and type(r) is int and 20 <= c <= 1000 and 5 <= r <= 500:
                    try:
                        session.resize(c, r)
                    except Exception:
                        pass
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        session.clients.discard(q)
        sender.cancel()


async def _supervise():
    """Notice processes that died without closing their console, and end everything on shutdown."""
    try:
        while True:
            await asyncio.sleep(2)
            for s in list(SESSIONS.values()):
                if s.alive and not s.process_alive():
                    s._finish()
    finally:
        end_all()


background = [_supervise]
