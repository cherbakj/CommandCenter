"""Raises an alert: stores it for the dashboard feed, shows a Windows toast, plays a sound."""
import logging
import threading
import webbrowser
import winsound

from . import config, db

log = logging.getLogger(__name__)
_toaster = None


def dashboard_url() -> str:
    return f"http://127.0.0.1:{config.section('server', {}).get('port', 8765)}/"


def _toast(title: str, body: str):
    global _toaster
    try:
        from windows_toasts import Toast, WindowsToaster

        if _toaster is None:
            _toaster = WindowsToaster("Command Center")
        toast = Toast([title, body[:250]] if body else [title])
        toast.on_activated = lambda _: webbrowser.open(dashboard_url())
        _toaster.show_toast(toast)
    except Exception as e:  # toasts are best-effort; the feed still has the alert
        log.warning("Toast failed: %s", e)


def _sound():
    threading.Thread(
        target=winsound.PlaySound, args=("SystemExclamation", winsound.SND_ALIAS), daemon=True
    ).start()


def raise_alert(source: str, title: str, body: str = "", priority: str = "normal", action: dict | None = None):
    """priority: low | normal | high. action: optional dict the dashboard turns into a button."""
    settings = config.section("alerts", {})
    db.add_alert(source, title, body, priority, action)
    log.info("[%s] %s: %s", priority, source, title)
    if settings.get("toast", True) and priority != "low":
        _toast(f"{source}: {title}", body)
    if priority in settings.get("sound_for", ["high"]):
        _sound()
