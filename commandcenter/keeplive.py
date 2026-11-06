"""Keeps Joe shown as Available in Teams while Command Center runs (Joe's call 2026-10-30). The Stay Available switch on
the Teams tab and in Ctrl+K turns it on and off; until it's first used, teams.keep_available in config.yaml decides (default
on). Teams goes Away when Windows has seen no input for about 5 minutes, so after 2 quiet minutes
this sends Windows a mouse move of zero distance: the idle timer restarts and the pointer stays put. No keys, no clicks,
nothing sent to Teams.

Not while Windows is locked: Teams shows Away then, as it should. It stops Windows' own idle timers too, so a screen saver,
display-off, sleep or inactivity lock set on this PC wouldn't fire while Command Center runs. None is set today (checked
2026-10-30: display and sleep Never, no lock policy). The off-hours log doesn't count these nudges (offhours.real_idle_seconds).

Work hours only (Joe's call 2026-11-06, for when he forgets to close Command Center): when work hours end (offhours.work_end,
17:00 Mon-Fri) the switch turns itself off, once; turning it back on in the evening is left alone. At the next work start
it goes back on, only if this turned it off and nobody touched the switch since. teams.keep_available_after_hours: true
turns that off."""
import asyncio
import ctypes
import ctypes.wintypes as wt
import logging
from datetime import datetime

from fastapi import APIRouter, Body, HTTPException

from . import config, db, offhours, privacy, security

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/teams/keep-available")

SETTING = "teams:keep_available"  # the switch; kept until changed
HOURS = "teams:keep_available_hours"  # {"handled": the off-hours stretch was dealt with, "turned_off": by this rule, "at"}

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


def hours_only() -> bool:
    return not bool((config.section("teams", {}) or {}).get("keep_available_after_hours", False))


def follow_hours(now: datetime | None = None) -> str | None:
    """The work-hours rule, run every check: "off" when it turned the switch off, "on" when it put it back, else None."""
    if not available() or not hours_only():
        return None
    st = db.kv_get(HOURS) or {}
    off = offhours.is_off(now or datetime.now(), offhours.settings())
    if off and not st.get("handled"):
        did = is_on()
        if did:
            db.kv_set(SETTING, False)
        db.kv_set(HOURS, {"handled": True, "turned_off": did, "at": (now or datetime.now()).isoformat(timespec="minutes")})
        if did:
            log.info("Teams keep-available: off (work hours ended)")
            return "off"
    elif not off and st.get("handled"):
        back = bool(st.get("turned_off")) and not is_on()
        if back:
            db.kv_set(SETTING, True)
        db.kv_set(HOURS, {"handled": False, "turned_off": False})
        if back:
            log.info("Teams keep-available: on (work hours started)")
            return "on"
    return None


async def keep_available_loop():
    log.info("Teams keep-available: %s", "on" if enabled() else "off")
    refused = False
    while True:
        try:
            await asyncio.to_thread(follow_hours)
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
    st = db.kv_get(HOURS) or {}
    auto = None
    if available() and hours_only():
        auto = "off" if st.get("turned_off") and not is_on() else None
    end = offhours.settings()["work_end"]
    return {"available": available(), "on": is_on(), "auto": auto, "hours_only": hours_only(),
            "work_end": f"{end // 60:02d}:{end % 60:02d}"}


@router.post("")
async def set_on(on: bool = Body(..., embed=True)):
    if not available():
        raise HTTPException(400, "Stay Available works only in the desktop app with Teams on")
    db.kv_set(SETTING, on)
    st = db.kv_get(HOURS) or {}
    if st.get("turned_off"):  # Joe has taken it from here: the morning leaves it as he set it
        db.kv_set(HOURS, {**st, "turned_off": False})
    log.info("Teams keep-available: %s", "on" if on else "off")
    return await status()


background = [keep_available_loop]
