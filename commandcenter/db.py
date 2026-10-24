"""SQLite store for alerts and small bits of connector state."""
import json
import sqlite3
import threading
from datetime import datetime

from . import config

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DATA_DIR / "commandcenter.db", check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                source TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT,
                priority TEXT NOT NULL DEFAULT 'normal',
                action TEXT,
                read INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, ts TEXT);
            """
        )
    return _conn


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def add_alert(source, title, body="", priority="normal", action=None) -> int:
    with _lock:
        cur = _db().execute(
            "INSERT INTO alerts (ts, source, title, body, priority, action) VALUES (?,?,?,?,?,?)",
            (now(), source, title, body, priority, json.dumps(action) if action else None),
        )
        _db().commit()
        return cur.lastrowid


def list_alerts(limit=200, unread_only=False) -> list[dict]:
    sql = "SELECT * FROM alerts"
    if unread_only:
        sql += " WHERE read = 0"
    sql += " ORDER BY id DESC LIMIT ?"
    with _lock:
        rows = _db().execute(sql, (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["action"] = json.loads(d["action"]) if d["action"] else None
        out.append(d)
    return out


def unread_count() -> int:
    with _lock:
        return _db().execute("SELECT COUNT(*) FROM alerts WHERE read = 0").fetchone()[0]


def mark_read(alert_id=None):
    with _lock:
        if alert_id is None:
            _db().execute("UPDATE alerts SET read = 1 WHERE read = 0")
        else:
            _db().execute("UPDATE alerts SET read = 1 WHERE id = ?", (alert_id,))
        _db().commit()


def clear_alerts():
    with _lock:
        _db().execute("DELETE FROM alerts")
        _db().commit()


def kv_get(key, default=None):
    with _lock:
        row = _db().execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def kv_set(key, value):
    with _lock:
        _db().execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, json.dumps(value)))
        _db().commit()


def seen(key) -> bool:
    """Returns True if key was already recorded; records it otherwise."""
    with _lock:
        if _db().execute("SELECT 1 FROM seen WHERE key = ?", (key,)).fetchone():
            return True
        _db().execute("INSERT INTO seen (key, ts) VALUES (?, ?)", (key, now()))
        _db().commit()
        return False


def prune_seen(days=30):
    with _lock:
        _db().execute("DELETE FROM seen WHERE ts < datetime('now', ?)", (f"-{days} days",))
        _db().commit()
