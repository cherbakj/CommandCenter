"""Snip: opens Windows' own screen-snip overlay (ms-screenclip:, the same as Win+Shift+S) from Command Center.

Joe picks rectangle / free-form / window / full screen on the overlay's toolbar; Windows copies the snip to the
clipboard (and offers to mark it up in Snipping Tool). It works over the native Pulse / Teams / SSMS views too,
since it's a screen capture by Windows, not by the page. `delay` (0-10 s) waits first, so a menu or tooltip can
be opened before the overlay appears. Command Center never sees or stores the image.
"""
import asyncio
import logging
import os

from fastapi import APIRouter, Body, HTTPException

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/snip")

MAX_DELAY = 10


@router.post("")
async def snip(delay: int = Body(0, embed=True)):
    if not 0 <= delay <= MAX_DELAY:
        raise HTTPException(400, f"Delay is 0 to {MAX_DELAY} seconds")
    if delay:
        await asyncio.sleep(delay)
    try:
        await asyncio.to_thread(os.startfile, "ms-screenclip:")
    except OSError as e:
        log.warning("Snip overlay didn't open: %s", type(e).__name__)
        raise HTTPException(503, "Windows' snipping overlay didn't open. Is Snipping Tool installed? (Win+Shift+S)")
    log.info("Snip overlay opened (delay %d s)", delay)
    return {"ok": True}
