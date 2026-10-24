"""Entry point.

  python -m commandcenter                     desktop app (same as "app")
  python -m commandcenter app [--minimized]   desktop window + tray icon
  python -m commandcenter serve               server only, dashboard in your browser (one-time link)
  python -m commandcenter install             Start menu, desktop and sign-in shortcuts
  python -m commandcenter uninstall           remove those shortcuts
  python -m commandcenter login <profile> <url>
  python -m commandcenter setpass <connection>

CC_DEV_NO_AUTH=1 (development only) turns off the session-cookie check, and only for `serve`.
"""
import argparse
import asyncio
import getpass
import logging
import sys
import threading
import webbrowser

from . import config


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
    login = sub.add_parser("login", help="Open a browser profile so you can log in to a site once")
    login.add_argument("profile")
    login.add_argument("url")
    setpass = sub.add_parser("setpass", help="Save a SQL connection's password in Windows Credential Manager")
    setpass.add_argument("connection")
    args = parser.parse_args()

    config.load()
    from logging.handlers import RotatingFileHandler
    # ~5 MB per file, 3 old files kept (commandcenter.log.1 .. .3)
    handlers = [RotatingFileHandler(config.DATA_DIR / "commandcenter.log", maxBytes=5 * 1024 * 1024, backupCount=3,
                                    encoding="utf-8")]
    if sys.stderr:  # pythonw has no console
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)

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
    elif args.cmd == "serve":
        run_server(open_browser=not args.no_browser)
    else:
        from .desktop import DesktopApp
        try:
            DesktopApp(start_hidden=getattr(args, "minimized", False), after_pid=getattr(args, "after_pid", None)).run()
        except Exception:
            logging.exception("Desktop app crashed")
            raise


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
