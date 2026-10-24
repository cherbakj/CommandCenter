"""Raises an alert: stores it for the dashboard feed, shows a Windows toast, plays a sound."""
import logging
import threading
import webbrowser
import winsound
from datetime import datetime, timedelta

from . import config, db

log = logging.getLogger(__name__)
_toaster = None


def dashboard_url() -> str:
    return f"http://127.0.0.1:{config.section('server', {}).get('port', 8765)}/"


open_hook = None  # set by the desktop app to show its window instead of opening a browser


def open_dashboard():
    if open_hook:
        open_hook()
    else:
        webbrowser.open(dashboard_url())


def _toast(title: str, body: str):
    global _toaster
    try:
        from windows_toasts import Toast, WindowsToaster

        if _toaster is None:
            _toaster = WindowsToaster("Command Center")
        toast = Toast([title, body[:250]] if body else [title])
        toast.on_activated = lambda _: open_dashboard()
        _toaster.show_toast(toast)
    except Exception as e:  # toasts are best-effort; the feed still has the alert
        log.warning("Toast failed: %s", e)


def _sound():
    threading.Thread(
        target=winsound.PlaySound, args=("SystemExclamation", winsound.SND_ALIAS), daemon=True
    ).start()


def focus_state() -> dict:
    """Focus mode is on while a manual timer runs or a calendar focus block is in progress."""
    now = datetime.now().isoformat(timespec="seconds")
    manual = db.kv_get("focus:until")
    calendar = db.kv_get("focus:calendar") or {}
    if manual and manual > now:
        return {"active": True, "until": manual, "reason": "manual"}
    if calendar.get("until", "") > now:
        return {"active": True, "until": calendar["until"], "reason": calendar.get("subject", "Calendar focus block")}
    return {"active": False}


def set_focus(minutes: int):
    until = (datetime.now() + timedelta(minutes=minutes)).isoformat(timespec="seconds") if minutes > 0 else None
    db.kv_set("focus:until", until)
    if minutes <= 0:  # turning focus off also ends the current calendar block early
        calendar = db.kv_get("focus:calendar") or {}
        if calendar.get("key"):
            db.kv_set("focus:skip", calendar["key"])
        db.kv_set("focus:calendar", None)


def raise_alert(source: str, title: str, body: str = "", priority: str = "normal", action: dict | None = None):
    """priority: low | normal | high. action: optional dict the dashboard turns into a button.
    In focus mode only high-priority alerts pop up; everything still lands in the feed."""
    settings = config.section("alerts", {})
    alert_id = db.add_alert(source, title, body, priority, action)
    # Titles and bodies can be email subjects, names or ticket text: the log gets only the id, source and priority.
    log.info("Alert %s raised (source=%s, priority=%s)", alert_id, source, priority)
    if priority != "high" and focus_state()["active"]:
        return
    if settings.get("toast", True) and priority != "low":
        _toast(f"{source}: {title}", body)
    if priority in settings.get("sound_for", ["high"]):
        _sound()
