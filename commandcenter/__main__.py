"""Entry point.

  python -m commandcenter                     desktop app (same as "app")
  python -m commandcenter app [--minimized]   desktop window + tray icon
  python -m commandcenter serve               server only, dashboard in your browser (one-time link)
  python -m commandcenter install             Start menu, desktop and sign-in shortcuts
  python -m commandcenter uninstall           remove those shortcuts
  python -m commandcenter kill [--yes]        force quit a stuck copy (the taskbar menu's "Force quit Command Center")
  python -m commandcenter login <profile> <url>
  python -m commandcenter setpass <connection>

CC_DEV_NO_AUTH=1 (development only) turns off the session-cookie check, and only for `serve`.
"""
import argparse
import asyncio
import getpass
import logging
import re
import sys
import threading
import webbrowser

from . import config

LOG_NAME = "commandcenter.log"
LOG_BACKUPS = 3
_RECORD = re.compile(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} [A-Z]+ [\w.]+: )(.*)", re.S)
# Messages older versions logged with work data in them. (logger: message regex; group 1 is the part kept.)
# - alerts: "[priority] Source: title" (alert titles are email subjects, names, ticket and calendar text)
# - toolkit: SQL Server / refusal messages, which can quote row values
# - orders: the copied order number
_LEAKY = [
    (re.compile(r"commandcenter\.alerts: $"), re.compile(r"()\[(?:low|normal|high)\] ")),
    (re.compile(r"commandcenter\.toolkit: $"),
     re.compile(r"(toolkit change refused/failed \(\d+ op\(s\)\): |toolkit request failed: )"
                r"(?!\w+(?: \(SQLSTATE \w+\))?\s*$)(?!\[redacted\])")),
    (re.compile(r"commandcenter\.orders: $"), re.compile(r"()Copied order (?!\[redacted\])")),
]


def _scrub_text(text: str) -> tuple[str, int]:
    out, n, dropping = [], 0, False
    for line in text.splitlines(keepends=True):
        m = _RECORD.match(line)
        if not m:  # continuation of the previous record (traceback, multi-line message)
            if not dropping:
                out.append(line)
            continue
        dropping = False
        head, msg = m.group(1), m.group(2)
        for logger, leaky in _LEAKY:
            hit = leaky.match(msg) if logger.search(head) else None
            if hit:
                ending = line[len(line.rstrip("\r\n")):]
                out.append(f"{head}{hit.group(1)}[redacted]{ending}")
                n += 1
                dropping = True
                break
        else:
            out.append(line)
    return "".join(out), n


def scrub_old_logs() -> int:
    """Rewrites data/commandcenter.log (and .1-.3) with work data from older versions replaced by "[redacted]";
    timestamps, levels and module names stay. Idempotent. Rewritten in place (another running copy may hold the
    file open and keeps appending at the end); anything appended while scrubbing is kept."""
    total = 0
    for path in [config.DATA_DIR / LOG_NAME] + [config.DATA_DIR / f"{LOG_NAME}.{i}" for i in range(1, LOG_BACKUPS + 1)]:
        try:
            with open(path, "r+b") as f:
                raw = f.read()
                clean, n = _scrub_text(raw.decode("utf-8", "surrogateescape"))
                if not n:
                    continue
                f.seek(len(raw))
                tail = f.read()  # written by another process since the read above
                f.seek(0)
                f.write(clean.encode("utf-8", "surrogateescape") + tail)
                f.truncate()
            total += n
        except FileNotFoundError:
            continue
        except OSError as e:
            print(f"Couldn't scrub {path.name}: {e}", file=sys.stderr)
    return total


def main():
    parser = argparse.ArgumentParser(prog="commandcenter")
    sub = parser.add_subparsers(dest="cmd")
    app = sub.add_parser("app", help="Desktop window with tray icon (default)")
    app.add_argument("--minimized", action="store_true", help="Start in the tray without showing the window")
    app.add_argument("--after-pid", type=int, help=argparse.SUPPRESS)  # Restart: wait for the old copy to exit
    serve = sub.add_parser("serve", help="Run the server only and use the dashboard in a browser")
    serve.add_argument("--no-browser", action="store_true", help="Don't open the dashboard")
    install = sub.add_parser("install", help="Create Start menu, desktop and sign-in shortcuts")
    install.add_argument("--no-startup", action="store_true", help="Don't start automatically at sign-in")
    sub.add_parser("uninstall", help="Remove the shortcuts")
    kill = sub.add_parser("kill", help="Force quit a stuck Command Center; docked VS Code and SSMS stay open")
    kill.add_argument("--yes", action="store_true", help="No questions (no confirm, no start-again prompt)")
    login = sub.add_parser("login", help="Open a browser profile so you can log in to a site once")
    login.add_argument("profile")
    login.add_argument("url")
    setpass = sub.add_parser("setpass", help="Save a SQL connection's password in Windows Credential Manager")
    setpass.add_argument("connection")
    args = parser.parse_args()

    config.load()
    from logging.handlers import RotatingFileHandler
    scrubbed = scrub_old_logs()
    # ~5 MB per file, 3 old files kept (commandcenter.log.1 .. .3). Never log subjects, names, ticket or calendar
    # text, SQL row data or message bodies: ids, counts, sources and error types only.
    handlers = [RotatingFileHandler(config.DATA_DIR / LOG_NAME, maxBytes=5 * 1024 * 1024, backupCount=LOG_BACKUPS,
                                    encoding="utf-8")]
    if sys.stderr:  # pythonw has no console
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    if scrubbed:
        logging.getLogger("commandcenter").info("Redacted %d old log line(s) that held work data", scrubbed)

    if args.cmd == "login":
        from .connectors import web
        print("Log in to the site in the browser window, then close the window to save the session.")
        asyncio.run(web.login(args.profile, args.url))
        print("Session saved for profile:", args.profile)
    elif args.cmd == "setpass":
        from .connectors import sql
        sql.set_password(args.connection, getpass.getpass(f"Password for {args.connection}: "))
        print("Saved to Windows Credential Manager.")
    elif args.cmd == "install":
        from . import security
        from .desktop import install_shortcuts
        security.harden_folders()
        for line in install_shortcuts(startup=not args.no_startup):
            print("Created", line)
    elif args.cmd == "uninstall":
        from .desktop import uninstall_shortcuts
        for line in uninstall_shortcuts() or ["(no shortcuts found)"]:
            print("Removed", line)
    elif args.cmd == "kill":
        from . import forcequit
        sys.exit(forcequit.main(confirm=not args.yes))
    elif args.cmd == "serve":
        run_server(open_browser=not args.no_browser)
    else:
        from .desktop import DesktopApp, exit_process
        code = 0
        try:
            DesktopApp(start_hidden=getattr(args, "minimized", False), after_pid=getattr(args, "after_pid", None)).run()
        except Exception:
            logging.exception("Desktop app crashed")
            code = 1
        exit_process(code)  # never waits on a stuck worker thread (see exit_process)


def run_server(open_browser=True):
    """Server only. The browser gets in through a one-time link that sets the session cookie."""
    import uvicorn

    from . import alerts, security
    security.configure("serve")  # the only mode that honors CC_DEV_NO_AUTH=1
    security.harden_folders()
    port = security.port()
    try:
        sock = security.listen_socket(port)
    except OSError as e:
        sys.exit(f"Can't listen on 127.0.0.1:{port} ({e}). Is Command Center already running?")
    from .app import app

    def open_dashboard():  # toast clicks: a fresh one-time link each time
        webbrowser.open(security.bootstrap_url(ttl=120))

    alerts.open_hook = open_dashboard
    link = security.bootstrap_url(ttl=600)
    print(f"Command Center is running on http://127.0.0.1:{port}/\n"
          f"Open it with this one-time link (works once, within 10 minutes):\n  {link}", flush=True)
    if open_browser:
        threading.Timer(1.5, webbrowser.open, [link]).start()
    server = uvicorn.Server(uvicorn.Config(security.guard(app), host="127.0.0.1", port=port, log_level="warning",
                                           log_config=None, access_log=False))
    server.run(sockets=[sock])


if __name__ == "__main__":
    main()
