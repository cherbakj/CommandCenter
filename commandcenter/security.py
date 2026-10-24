"""Security helpers shared by all routes.

ws_allowed(websocket): call first in every WebSocket endpoint; closes and returns False unless the
connection comes from this app's own page (same host/port, same origin).
"""
from fastapi import WebSocket

from . import config


def _allowed_hosts() -> set[str]:
    port = config.section("server", {}).get("port", 8765)
    return {f"127.0.0.1:{port}", f"localhost:{port}"}


async def ws_allowed(websocket: WebSocket) -> bool:
    host = websocket.headers.get("host", "")
    origin = websocket.headers.get("origin", "")
    if host not in _allowed_hosts() or origin not in {f"http://{h}" for h in _allowed_hosts()}:
        await websocket.close(code=1008)
        return False
    return True
