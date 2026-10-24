"""Entry point.  python -m commandcenter [serve | login <profile> <url> | setpass <connection>]"""
import argparse
import asyncio
import getpass
import logging
import threading
import webbrowser

from . import config


def main():
    parser = argparse.ArgumentParser(prog="commandcenter")
    sub = parser.add_subparsers(dest="cmd")
    serve = sub.add_parser("serve", help="Run the dashboard and watchers (default)")
    serve.add_argument("--no-browser", action="store_true", help="Don't open the dashboard")
    login = sub.add_parser("login", help="Open a browser profile so you can log in to a site once")
    login.add_argument("profile")
    login.add_argument("url")
    setpass = sub.add_parser("setpass", help="Save a SQL connection's password in Windows Credential Manager")
    setpass.add_argument("connection")
    args = parser.parse_args()

    config.load()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(config.DATA_DIR / "commandcenter.log", encoding="utf-8")],
    )

    if args.cmd == "login":
        from .connectors import web
        print(f"Log in to the site in the browser window, then close the window to save the session.")
        asyncio.run(web.login(args.profile, args.url))
        print("Session saved for profile:", args.profile)
    elif args.cmd == "setpass":
        from .connectors import sql
        sql.set_password(args.connection, getpass.getpass(f"Password for {args.connection}: "))
        print("Saved to Windows Credential Manager.")
    else:
        import uvicorn
        from .alerts import dashboard_url
        port = config.section("server", {}).get("port", 8765)
        if not getattr(args, "no_browser", False):
            threading.Timer(1.5, webbrowser.open, [dashboard_url()]).start()
        uvicorn.run("commandcenter.app:app", host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
