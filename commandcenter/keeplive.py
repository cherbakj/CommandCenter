"""Keeps Joe shown as Available in Teams while Command Center runs (Joe's call 2026-10-30). The Stay Available switch on
the Teams tab and in Ctrl+K turns it on and off; until it's first used, teams.keep_available in config.yaml decides (default
on). Teams goes Away when Windows has seen no input for about 5 minutes, so after 2 quiet minutes
this sends Windows a mouse move of zero distance: the idle timer restarts and the pointer stays put. No keys, no clicks,
nothing sent to Teams.

Not while Windows is locked: Teams shows Away then, as it should. It stops Windows' own idle timers too, so a screen saver,
display-off, sleep or inactivity lock set on this PC wouldn't fire while Command Center runs. None is set today (checked
2026-10-30: display and sleep Never, no lock policy). The off-hours log doesn't count these nudges (offhours.real_idle_seconds)."""
import asyncio
import ctypes
import ctypes.wintypes as wt
import logging

from fastapi import APIRouter, Body, HTTPException

from . import config, db, offhours, privacy, security

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/teams/keep-available")

SETTING = "teams:keep_available"  # the switch; kept until changed

NUDGE_AFTER = 120  # s of no input
CHECK_SECONDS = 30
_INPUT_MOUSE, _MOUSEEVENTF_MOVE = 0, 0x0001


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("pad", ctypes.c_byte * 32)]  # the size of the largest input kind


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


def available() -> bool:
    """The desktop app only, with Teams on."""
    return security._state["mode"] == "app" and bool((config.section("teams", {}) or {}).get("enabled", True))


def is_on() -> bool:
    """The switch; config.yaml's setting until it's first used."""
    on = db.kv_get(SETTING)
    return bool((config.section("teams", {}) or {}).get("keep_available", True)) if on is None else bool(on)


def enabled() -> bool:
    return available() and is_on()


def _send_nudge() -> bool:
    move = _INPUT(_INPUT_MOUSE, _INPUTUNION(mi=_MOUSEINPUT(0, 0, 0, _MOUSEEVENTF_MOVE, 0, 0)))
    return ctypes.WinDLL("user32").SendInput(1, ctypes.byref(move), ctypes.sizeof(_INPUT)) == 1


def nudge_if_quiet() -> bool:
    """Sends a nudge if one is due. True if it did."""
    if not enabled() or privacy.windows_locked():
        return False
    idle = offhours.idle_seconds()
    if idle is None or idle < NUDGE_AFTER:
        return False
    offhours.before_nudge()
    return _send_nudge()


async def keep_available_loop():
    log.info("Teams keep-available: %s", "on" if enabled() else "off")
    refused = False
    while True:
        try:
            before = await asyncio.to_thread(offhours.idle_seconds)
            sent = await asyncio.to_thread(nudge_if_quiet)
            if not sent and enabled() and before is not None and before >= NUDGE_AFTER and not privacy.windows_locked():
                if not refused:  # an elevated window in front refuses it, for instance: Teams can go Away meanwhile
                    log.warning("Teams keep-available: Windows refused a nudge")
                refused = True
            elif sent:
                refused = False
        except Exception as e:  # never let the loop die
            log.warning("Teams keep-available: %s", type(e).__name__)
        await asyncio.sleep(CHECK_SECONDS)


@router.get("")
async def status():
    return {"available": available(), "on": is_on()}


@router.post("")
async def set_on(on: bool = Body(..., embed=True)):
    if not available():
        raise HTTPException(400, "Stay Available works only in the desktop app with Teams on")
    db.kv_set(SETTING, on)
    log.info("Teams keep-available: %s", "on" if on else "off")
    return await status()


background = [keep_available_loop]
