"""Entry point.

  python -m commandcenter                     desktop app (same as "app")
  python -m commandcenter app [--minimized]   desktop window + tray icon
  python -m commandcenter serve               server only, dashboard in your browser
  python -m commandcenter install             Start menu, desktop and sign-in shortcuts
  python -m commandcenter uninstall           remove those shortcuts
  python -m commandcenter login <profile> <url>
  python -m commandcenter setpass <connection>
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
    handlers = [logging.FileHandler(config.DATA_DIR / "commandcenter.log", encoding="utf-8")]
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
        from .desktop import install_shortcuts
        for line in install_shortcuts(startup=not args.no_startup):
            print("Created", line)
    elif args.cmd == "uninstall":
        from .desktop import uninstall_shortcuts
        for line in uninstall_shortcuts() or ["(no shortcuts found)"]:
            print("Removed", line)
    elif args.cmd == "serve":
        import uvicorn
        from .alerts import dashboard_url
        if not args.no_browser:
            threading.Timer(1.5, webbrowser.open, [dashboard_url()]).start()
        uvicorn.run("commandcenter.app:app", host="127.0.0.1", port=config.section("server", {}).get("port", 8765),
                    log_level="warning", log_config=None)
    else:
        from .desktop import DesktopApp
        try:
            DesktopApp(start_hidden=getattr(args, "minimized", False)).run()
        except Exception:
            logging.exception("Desktop app crashed")
            raise


if __name__ == "__main__":
    main()
