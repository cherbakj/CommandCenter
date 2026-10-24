"""Order lookups: spot order numbers in copied text or emails and look them up with a saved SQL query.

The clipboard watcher only reacts when what you copied is short (an order number, or "Order #123"),
so copying a paragraph that happens to contain digits doesn't trigger a lookup.
"""
import asyncio
import logging
import re
import time

import win32clipboard

from . import alerts, config, db
from .connectors import sql

log = logging.getLogger(__name__)
MAX_COPIED_CHARS = 40
DEFAULT_PATTERN = r"(?:order\s*#?\s*)?\b(\d{7,10})\b"


def settings() -> dict:
    return config.section("orders", {})


def pattern() -> re.Pattern:
    return re.compile(settings().get("pattern", DEFAULT_PATTERN), re.I)


def find_orders(text) -> list[str]:
    seen, keys = [], set()
    for m in pattern().finditer(text or ""):
        number = m.group(1) if m.groups() else m.group(0)
        key = number.lstrip("0") or number  # 2296106 and 0002296106 are the same order
        if number and key not in keys:
            keys.add(key)
            seen.append(number)
    return seen


# The values config.example.yaml ships with: lookups can't work until they're replaced.
PLACEHOLDER_SERVERS = {r"yourserver\instance", "yourserver"}
PLACEHOLDER_DATABASES = {"yourdatabase"}


def _is_placeholder(value, known) -> bool:
    v = str(value or "").strip().lower()
    return not v or v in known or v.startswith("your")


def ready() -> str | None:
    """None when lookups can run; otherwise what still needs setting up."""
    name = settings().get("lookup_query", "Find order")
    q = next((q for q in sql.queries() if q.get("name") == name), None)
    if not q:
        return f"Add a saved SQL query named {name!r} (orders.lookup_query) to config.yaml"
    conn = next((c for c in sql.connections() if c.get("name") == q.get("connection")), None)
    if conn and (_is_placeholder(conn.get("server"), PLACEHOLDER_SERVERS)
                 or _is_placeholder(conn.get("database"), PLACEHOLDER_DATABASES)):
        return (f"Set your real SQL server and database for the {conn['name']!r} connection in config.yaml "
                "(sql.connections), then save the password on the SQL tab")
    if "yourorderstable" in str(q.get("sql", "")).lower():
        return f"Replace the placeholder table in the {name!r} query (sql.queries in config.yaml) with your real orders table"
    try:
        if not sql.has_password(q["connection"]):
            return f"Save the SQL password for {q['connection']!r} on the SQL tab"
    except ValueError as e:
        return str(e)
    return None


def lookup(number) -> dict:
    problem = ready()
    if problem:
        raise ValueError(problem)
    name = settings().get("lookup_query", "Find order")
    result = sql.run_saved(name, [number])
    return {"number": number, "query": name, **result}


def summarize(result) -> str:
    if not result["rows"]:
        return "not found"
    row = dict(zip(result["columns"], result["rows"][0]))
    wanted = settings().get("summary_columns") or result["columns"][:4]
    parts = [f"{c}: {row[c]}" for c in wanted if c in row and row[c] not in (None, "")]
    more = f" (+{len(result['rows']) - 1} more rows)" if len(result["rows"]) > 1 else ""
    return " · ".join(parts) + more


def _read_clipboard() -> str | None:
    for _ in range(3):  # another app may hold the clipboard for a moment
        try:
            win32clipboard.OpenClipboard()
            try:
                if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                    return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                return None
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            time.sleep(0.1)
    return None


async def _on_copy(text):
    numbers = find_orders(text)
    if not numbers:
        return
    number = numbers[0]
    if db.kv_get("orders:last") == [number, time.strftime("%Y-%m-%d %H:%M")]:
        return  # same number copied again within the minute
    db.kv_set("orders:last", [number, time.strftime("%Y-%m-%d %H:%M")])
    if ready():
        log.info("Copied order %s but lookups aren't set up: %s", number, ready())
        return
    try:
        result = await asyncio.to_thread(lookup, number)
        alerts.raise_alert("Order", f"Order {number}: {summarize(result)}", "", "normal",
                           {"type": "order", "number": number})
    except Exception as e:
        alerts.raise_alert("Order", f"Order {number}: lookup failed", str(e).splitlines()[0], "low")


async def clipboard_loop():
    last = win32clipboard.GetClipboardSequenceNumber()
    while True:
        await asyncio.sleep(1)
        if not settings().get("watch_clipboard", True):
            continue
        seq = win32clipboard.GetClipboardSequenceNumber()  # cheap; doesn't open the clipboard
        if seq == last:
            continue
        last = seq
        text = await asyncio.to_thread(_read_clipboard)
        if text and len(text.strip()) <= MAX_COPIED_CHARS:
            try:
                await _on_copy(text.strip())
            except Exception:
                log.exception("Order lookup from clipboard failed")
