"""SQLite store for alerts and small bits of connector state.

Work data is encrypted at rest with Windows DPAPI for the current Windows user (same approach as daily.py
and planner.py), so the database file on its own shows no subjects, names, recipients or ticket text:
  * alerts: title, body and action (ids, ts, source, priority and the read flag stay clear for sorting).
  * kv: every value except the operational keys in CLEAR_KV / CLEAR_KV_PREFIXES (status, watermarks, weather,
    timers). So the mail sent log, snoozes, reply watches, dismissed/muted follow-ups, urgent people, important senders, the
    profile (name, initials and appearance), task priorities, the calendar focus block, website state, Asana account,
    Toolkit/Pulse settings, and any key a new module adds are sealed by default.
  * toolkit_changes (toolkit.py): pk, before_json, after_json and error.
A sealed value is text: SEAL_PREFIX + base64(DPAPI blob). Readers accept sealed and legacy plaintext values,
and seal_existing() (run at start and daily, see maintenance()) encrypts any plaintext left by older versions.

Retention (config `retention:`; see retention_settings): alerts, the mail sent log, `seen` keys, the Toolkit
change log and the off-hours log (offhours.py) are pruned at start and daily. The connection runs with secure_delete=ON and an in-memory temp
store, and maintenance() VACUUMs after it seals or deletes rows (and at least every `vacuum_days`), so deleted
plaintext isn't left in free pages or temp files. Sealing, VACUUM and the vault's moves of old copies run inside
scrubbed_journal(): the rollback journal (which holds the original, plaintext pages) is kept in place and
overwritten with zeros afterwards instead of just being deleted (see _ScrubbedJournal).
"""
import base64
import json
import logging
import os
import sqlite3
import threading
from collections import OrderedDict
from datetime import datetime, timedelta

import pywintypes
import win32crypt

from . import config

log = logging.getLogger(__name__)
_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SEAL_PREFIX = "ccsealed:v1:"
_SEAL_ENTROPY = b"CommandCenter.db.v1"
_SEAL_LABEL = "CommandCenter"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
# Values that stay clear: purely operational state with no work data in it (and read on hot paths).
CLEAR_KV = {"outlook:status", "outlook:watermark", "focus:until", "focus:skip", "urgent:checked",
            "window:geometry", "capture:hotkey_alerted", "browser:ua", "pulse:status",
            "teams:state", "weather:selected"}
# (pulse:state, the last ticket ids / numbers / statuses, and profile, which holds Joe's name, are sealed like any
# other key; seal_existing() encrypts a plaintext copy left by an older version, and kv_get reads either.)
CLEAR_KV_PREFIXES = ("weather:loc:", "maint:")
_EMPTY_JSON = {"null", "[]", "{}", '""'}  # nothing to hide; also keeps an older running copy working on empty values
ALERT_SEALED = ("title", "body", "action")

RETENTION_DEFAULTS = {"alerts_days": 30, "mail_sent_log_days": 90, "toolkit_changes_days": 365, "seen_days": 30,
                      "dismissed_followups_days": 60, "offhours_days": 400, "reminders_done_days": 30,
                      "vacuum_days": 7}


class SealError(Exception):
    """A sealed value that this Windows user can't decrypt (e.g. the database was copied elsewhere)."""


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        conn = sqlite3.connect(config.DATA_DIR / "commandcenter.db", check_same_thread=False, timeout=15)
        conn.row_factory = sqlite3.Row
        # Deleted rows are overwritten with zeros, and temp tables/VACUUM copies stay in memory, not %TEMP%.
        conn.execute("PRAGMA secure_delete = ON")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.executescript(
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
        _conn = conn
    return _conn


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---- sealing (DPAPI, current Windows user) ----------------------------------------------------------

def is_sealed(value) -> bool:
    return isinstance(value, str) and value.startswith(SEAL_PREFIX)


def seal(text):
    """Encrypts text for this Windows user. None, "" and already-sealed values are returned unchanged."""
    if text is None or text == "" or is_sealed(text):
        return text
    blob = win32crypt.CryptProtectData(str(text).encode("utf-8"), _SEAL_LABEL, _SEAL_ENTROPY, None, None,
                                       _CRYPTPROTECT_UI_FORBIDDEN)
    return SEAL_PREFIX + base64.b64encode(blob).decode("ascii")


_open_cache: "OrderedDict[str, str]" = OrderedDict()  # sealed -> plain; DPAPI costs ~0.2 ms per value
_OPEN_CACHE_MAX = 4096
_OPEN_CACHE_MAX_VALUE = 8192  # bigger values (e.g. pulse:state, rewritten every poll) aren't cached
_cache_lock = threading.Lock()


def unseal(value):
    """The plain text of a sealed value; legacy plaintext (and None) passes through unchanged."""
    if not is_sealed(value):
        return value
    with _cache_lock:
        hit = _open_cache.get(value)
        if hit is not None:
            _open_cache.move_to_end(value)
            return hit
    try:
        blob = base64.b64decode(value[len(SEAL_PREFIX):], validate=True)
        plain = win32crypt.CryptUnprotectData(blob, _SEAL_ENTROPY, None, None, _CRYPTPROTECT_UI_FORBIDDEN)[1].decode("utf-8")
    except (pywintypes.error, ValueError, UnicodeDecodeError) as e:
        raise SealError("Couldn't decrypt a stored value for this Windows user") from e
    if len(value) > _OPEN_CACHE_MAX_VALUE:
        return plain
    with _cache_lock:
        _open_cache[value] = plain
        if len(_open_cache) > _OPEN_CACHE_MAX:
            _open_cache.popitem(last=False)
    return plain


def unseal_or(value, fallback="(encrypted for another Windows user)"):
    try:
        return unseal(value)
    except SealError:
        return fallback


def kv_sealed(key: str) -> bool:
    return key not in CLEAR_KV and not key.startswith(CLEAR_KV_PREFIXES)


# ---- alerts -------------------------------------------------------------------------------------------

def add_alert(source, title, body="", priority="normal", action=None) -> int:
    with _lock:
        cur = _db().execute(
            "INSERT INTO alerts (ts, source, title, body, priority, action) VALUES (?,?,?,?,?,?)",
            (now(), source, seal(title), seal(body), priority, seal(json.dumps(action)) if action else None),
        )
        _db().commit()
        return cur.lastrowid


def _alert_row(r) -> dict:
    d = dict(r)
    d["title"] = unseal_or(d["title"])
    d["body"] = unseal_or(d["body"], "")
    try:
        d["action"] = json.loads(unseal(d["action"])) if d["action"] else None
    except (SealError, ValueError):
        d["action"] = None
    return d


def list_alerts(limit=200, unread_only=False) -> list[dict]:
    sql = "SELECT * FROM alerts"
    if unread_only:
        sql += " WHERE read = 0"
    sql += " ORDER BY id DESC LIMIT ?"
    with _lock:
        rows = _db().execute(sql, (limit,)).fetchall()
    return [_alert_row(r) for r in rows]


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


def action_alerts(kind: str) -> list[tuple[int, dict]]:
    """(alert id, action) of the alerts whose action is of this type ("mail", "teams", ...), newest first."""
    with _lock:
        rows = _db().execute("SELECT id, action FROM alerts WHERE action IS NOT NULL ORDER BY id DESC").fetchall()
    out = []
    for alert_id, action in rows:
        try:
            a = json.loads(unseal(action))
        except (SealError, ValueError):
            continue
        if isinstance(a, dict) and a.get("type") == kind:
            out.append((alert_id, a))
    return out


def mail_alerts() -> list[tuple[int, str]]:
    """(alert id, EntryID) of the alerts whose action opens an email, newest first."""
    return [(i, a["id"]) for i, a in action_alerts("mail") if isinstance(a.get("id"), str)]


def delete_mail_alerts(entry_id: str) -> int:
    """Deletes the alerts (read or not) that open this email: Joe replied to it."""
    return delete_alerts([a for a, e in mail_alerts() if e == entry_id])


def mark_mail_alerts_read(entry_id: str) -> int:
    """Marks read the unread alerts that open this email (it was just read in Command Center)."""
    ids = [a for a, e in mail_alerts() if e == entry_id]
    if not ids:
        return 0
    with _lock:
        n = _db().execute(f"UPDATE alerts SET read = 1 WHERE read = 0 AND id IN ({','.join('?' * len(ids))})", ids).rowcount
        _db().commit()
    return n


def delete_alerts(ids) -> int:
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    with _lock:
        n = 0
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            n += _db().execute(f"DELETE FROM alerts WHERE id IN ({','.join('?' * len(chunk))})", chunk).rowcount
        _db().commit()
    return n


# ---- key/value ----------------------------------------------------------------------------------------

def kv_get(key, default=None):
    with _lock:
        row = _db().execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(unseal(row[0]))
    except SealError:
        log.warning("Couldn't decrypt the stored value of %s; using the default", key)
        return default


def _kv_text(key, value) -> str:
    text = json.dumps(value)
    return seal(text) if kv_sealed(key) and text not in _EMPTY_JSON else text


def kv_set(key, value):
    with _lock:
        _db().execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, _kv_text(key, value)))
        _db().commit()


def seen(key) -> bool:
    """Returns True if key was already recorded; records it otherwise."""
    with _lock:
        if _db().execute("SELECT 1 FROM seen WHERE key = ?", (key,)).fetchone():
            return True
        _db().execute("INSERT INTO seen (key, ts) VALUES (?, ?)", (key, now()))
        _db().commit()
        return False


def _cutoff(days) -> str:
    return (datetime.now() - timedelta(days=float(days))).isoformat(timespec="seconds")


def prune_seen(days=30) -> int:
    with _lock:
        n = _db().execute("DELETE FROM seen WHERE ts < ?", (_cutoff(days),)).rowcount
        _db().commit()
    return n


# ---- one-time (idempotent) encryption of legacy plaintext ----------------------------------------------

class _Transaction:
    """BEGIN IMMEDIATE ... COMMIT under the module lock; ROLLBACK on any exception. Other processes (e.g. an
    older copy of the app still running) wait on SQLite's lock and never see a half-done migration."""

    def __enter__(self):
        _lock.acquire()
        try:
            conn = _db()
            if conn.in_transaction:
                conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            return conn
        except BaseException:
            _lock.release()
            raise

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                _db().commit()
            else:
                _db().rollback()
        finally:
            _lock.release()
        return False


def transaction() -> _Transaction:
    return _Transaction()


def _plain_where(cols) -> str:
    n = len(SEAL_PREFIX)
    return " OR ".join(f"({c} IS NOT NULL AND {c} != '' AND substr({c}, 1, {n}) != '{SEAL_PREFIX}')" for c in cols)


def seal_columns(conn, table, key_col, cols) -> int:
    """Seals every plaintext value of `cols` in `table`, inside the caller's transaction. Each value is checked to
    decrypt back to the original before it is written, and the row count and contents are re-verified after.
    Raises (so the caller rolls back) if anything doesn't match."""
    where = _plain_where(cols)
    total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    rows = conn.execute(f"SELECT {key_col}, {', '.join(cols)} FROM {table} WHERE {where}").fetchall()
    for r in rows:
        values = [seal(r[c]) for c in cols]
        if [unseal(v) for v in values] != [r[c] for c in cols]:
            raise RuntimeError(f"{table}: a value didn't decrypt back to the original; nothing changed")
        cur = conn.execute(f"UPDATE {table} SET {', '.join(f'{c} = ?' for c in cols)} WHERE {key_col} = ?",
                           (*values, r[key_col]))
        if cur.rowcount != 1:
            raise RuntimeError(f"{table}: a row changed during sealing; nothing changed")
    after = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    left = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]
    if after != total or left:
        raise RuntimeError(f"{table}: {total} rows before, {after} after, {left} still plain; nothing changed")
    for r in rows:  # read back what was written
        got = conn.execute(f"SELECT {', '.join(cols)} FROM {table} WHERE {key_col} = ?", (r[key_col],)).fetchone()
        if [unseal(got[c]) for c in cols] != [r[c] for c in cols]:
            raise RuntimeError(f"{table}: read-back mismatch; nothing changed")
    return len(rows)


def seal_existing() -> dict:
    """Encrypts plaintext alert fields and kv values left by older versions. Idempotent; one transaction per
    table, verified before commit. Returns counts (no values)."""
    out = {}
    with transaction() as conn:
        out["alerts"] = seal_columns(conn, "alerts", "id", ALERT_SEALED)
    with transaction() as conn:
        changed = {}
        rows = conn.execute("SELECT key, value FROM kv").fetchall()
        for r in rows:
            if r["value"] is None or is_sealed(r["value"]) or not kv_sealed(r["key"]) or r["value"] in _EMPTY_JSON:
                continue
            sealed = seal(r["value"])
            if unseal(sealed) != r["value"]:
                raise RuntimeError("kv: a value didn't decrypt back to the original; nothing changed")
            if conn.execute("UPDATE kv SET value = ? WHERE key = ? AND value = ?",
                            (sealed, r["key"], r["value"])).rowcount != 1:
                raise RuntimeError("kv: a value changed during sealing; nothing changed")
            changed[r["key"]] = r["value"]
        after = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM kv")}
        if len(after) != len(rows) or any(unseal(after.get(k)) != v for k, v in changed.items()):
            raise RuntimeError("kv: read-back mismatch; nothing changed")
        n = len(changed)
        out["kv"] = n
    return out


# ---- retention ------------------------------------------------------------------------------------------

def retention_settings() -> dict:
    cfg = config.section("retention", {})
    out = dict(RETENTION_DEFAULTS)
    if isinstance(cfg, dict):
        for k, default in RETENTION_DEFAULTS.items():
            try:
                v = float(cfg.get(k, default))
                out[k] = v if v > 0 else default
            except (TypeError, ValueError):
                pass
    return out


def enforce_retention(r: dict | None = None) -> dict:
    """Deletes alerts, `seen` keys and mail-sent-log entries older than their retention. Returns counts."""
    r = r or retention_settings()
    out = {}
    with transaction() as conn:
        out["alerts"] = conn.execute("DELETE FROM alerts WHERE ts < ?", (_cutoff(r["alerts_days"]),)).rowcount
        out["seen"] = conn.execute("DELETE FROM seen WHERE ts < ?", (_cutoff(r["seen_days"]),)).rowcount
        row = conn.execute("SELECT value FROM kv WHERE key = 'mail:sent_log'").fetchone()
        out["mail_sent_log"] = 0
        if row and row[0]:
            entries = json.loads(unseal(row[0]))
            cutoff = _cutoff(r["mail_sent_log_days"])
            keep = [e for e in entries if isinstance(e, dict) and str(e.get("ts", "")) >= cutoff]
            if len(keep) != len(entries):
                conn.execute("UPDATE kv SET value = ? WHERE key = 'mail:sent_log'", (_kv_text("mail:sent_log", keep),))
                out["mail_sent_log"] = len(entries) - len(keep)
    return out


def vacuum(force=False, every_days=7) -> bool:
    """Rebuilds the file so freed pages (old plaintext, deleted rows) are gone. Runs when forced or when the last
    VACUUM is older than every_days. Needs a moment with no other writer; if the file is busy it waits for the
    next run."""
    last = kv_get("maint:vacuumed")
    if not force and last and last > _cutoff(every_days):
        return False
    with _lock:
        conn = _db()
        if conn.in_transaction:
            conn.commit()
        try:
            conn.execute("VACUUM")
        except sqlite3.OperationalError as e:
            log.warning("VACUUM skipped: %s", e)
            return False
    kv_set("maint:vacuumed", now())
    return True


# ---- rollback journal hygiene ------------------------------------------------------------------------------

class _ScrubbedJournal:
    """For one-time migrations, sealing, deletes of old copies and VACUUM: SQLite's rollback journal holds the
    original pages of everything a transaction changes, which then is old plaintext. In the default DELETE mode the
    journal file is only unlinked after each commit, so that plaintext would stay in free disk clusters.
    Inside this block the connection uses journal_mode=PERSIST (the journal file is reused in place, not deleted
    and recreated); on exit, with an EXCLUSIVE lock held (so no other connection is using the journal), the file
    is overwritten with zeros, flushed, truncated, and the mode goes back to DELETE (which removes the empty file).
    PERSIST keeps full crash safety, unlike journal_mode=MEMORY. Best effort: SSD wear levelling can still keep
    old copies of a block."""

    def __enter__(self):
        with _lock:
            c = _db()
            if c.in_transaction:
                c.commit()
            self.mode = str(c.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            c.execute("PRAGMA journal_mode = PERSIST").fetchone()
        return self

    def __exit__(self, *exc):
        with _lock:
            c = _db()
            if c.in_transaction:
                c.rollback()  # anything a failed step left open
            try:
                wipe_journal(c)
            except Exception as e:
                log.warning("Couldn't overwrite the rollback journal (%s)", type(e).__name__)
            finally:
                mode = self.mode if self.mode in ("delete", "truncate", "persist") else "delete"
                c.execute(f"PRAGMA journal_mode = {mode}").fetchone()
        return False


def scrubbed_journal() -> _ScrubbedJournal:
    return _ScrubbedJournal()


def wipe_journal(conn) -> bool:
    """Overwrites the database's -journal file with zeros and truncates it (caller holds _lock). Takes an
    EXCLUSIVE lock first, so no connection (here or in another process) is in a write transaction using it."""
    path = conn.execute("PRAGMA database_list").fetchone()[2] + "-journal"
    conn.execute("BEGIN EXCLUSIVE")
    try:
        try:
            size = os.path.getsize(path)
        except OSError:
            return False
        zeros = b"\0" * 65536
        with open(path, "r+b") as f:
            left = size
            while left > 0:
                n = min(left, len(zeros))
                f.write(zeros[:n])
                left -= n
            f.flush()
            os.fsync(f.fileno())
            f.truncate(0)
            f.flush()
            os.fsync(f.fileno())
        return True
    finally:
        conn.commit()
