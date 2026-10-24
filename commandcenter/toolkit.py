"""Toolkit tab: the former "Cherbak's Toolkit" (EDI ship mapping, SPS carrier lookup, Logi user
management, customer tree), editing LIVE production SQL Server data.

Safety rules enforced here (the browser is never trusted):
  * Every table, column and key comes from the fixed TABLES whitelist below. Database/schema/table
    names that may be changed in config/settings must match IDENT (letters, digits, underscore).
  * Every value is a bound parameter (?). No value is ever formatted into SQL text.
  * Writes act on exactly one row, found by its full primary key. Before an UPDATE/DELETE the row
    is read with UPDLOCK+HOLDLOCK; it must exist exactly once and still hold the values the user
    saw ("expect"), otherwise nothing runs. After each statement @@ROWCOUNT must be 1, otherwise
    the whole transaction is rolled back.
  * A batch (e.g. "add user" = Users + Roles + Rights + tree rows) runs in one transaction.
  * Every attempt is written to an append-only SQLite table (toolkit_changes) BEFORE it runs and
    finalized after; SQLite triggers refuse deletes and edits of finalized rows.
  * The SQL password lives only in Windows Credential Manager (keyring service
    "CommandCenter-Toolkit", user "<username>@<server>"). It is never returned, logged or written.
    Logi user passwords (a column in Logi.dbo.Users) are masked in the change log.
"""
import asyncio
import getpass
import json
import logging
import os
import re
from datetime import date, datetime, time as dtime
from decimal import Decimal

import keyring
import keyring.errors
from fastapi import APIRouter, Body, HTTPException

from . import config, db

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/toolkit")

KEYRING_SERVICE = "CommandCenter-Toolkit"
KV_SETTINGS = "toolkit:settings"
KV_LAST_TEST = "toolkit:last_test"
DRIVERS = ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server")
IDENT = re.compile(r"^[A-Za-z0-9_]{1,128}$")
SERVER_RE = re.compile(r"^[A-Za-z0-9._\-\\,]{1,253}$")       # host, host\instance, host,port
USER_RE = re.compile(r"^[A-Za-z0-9._\-\\@]{1,128}$")
MAX_ROWS = 10000
MAX_VALUE_LEN = 4000
MAX_BATCH = 60
MASKED_COLUMNS = {"Password"}       # app passwords shown in the UI but never written to the change log

DEFAULTS = {
    # Verify the server's certificate by default: this PC trusts Sonance's internal CAs (corp-*-CA).
    "server": "", "username": "", "driver": DRIVERS[0], "encrypt": True, "trust_server_certificate": False,
    "databases": {"staging": "FS92STG", "logi": "Logi", "sys": "FS92SYS"},
    "shipping_schema": "sps",
}

# ---- whitelist -------------------------------------------------------------------------------
# db: key into settings.databases.  pk: full key used in WHERE.  editable: columns an UPDATE may set.
# insert: columns the browser may send on INSERT.  required: insert columns that must be non-empty.
# constants: server-forced values added to every INSERT (from the original toolkit).
# unique: insert is skipped (not an error) if a row with these values exists (original behavior).
# upper: columns upper-cased on insert (OPRID = UPPER(UserID)).
TABLES = {
    "ship_account": {
        "label": "EDI shipping account", "db": "staging", "schema": "@shipping", "table": "SHIPPING_ACCOUNT",
        "pk": ["ACCOUNT_ID"], "columns": ["ACCOUNT_ID", "ACCOUNT_NAME", "PARTNER_ID", "ACCOUNT_NUMBER"],
        "editable": ["ACCOUNT_NAME", "PARTNER_ID", "ACCOUNT_NUMBER"],
        "insert": ["ACCOUNT_NAME", "PARTNER_ID", "ACCOUNT_NUMBER"], "required": [],
        "constants": {}, "unique": [], "upper": [],
        "children": "ship_method",   # delete refused while child rows exist
    },
    "ship_method": {
        "label": "EDI shipping method", "db": "staging", "schema": "@shipping", "table": "SHIPPING_METHOD",
        "pk": ["SHIPPING_METHOD_ID"],
        "columns": ["SHIPPING_METHOD_ID", "ACCOUNT_ID", "INBOUND_ID", "CARRIER_ID", "SHIP_VIA", "FREIGHT_TERMS"],
        "editable": ["INBOUND_ID", "CARRIER_ID", "SHIP_VIA", "FREIGHT_TERMS"],
        "insert": ["ACCOUNT_ID", "INBOUND_ID", "CARRIER_ID", "SHIP_VIA", "FREIGHT_TERMS"], "required": ["ACCOUNT_ID"],
        "constants": {}, "unique": [], "upper": [],
        "parent": ("ship_account", "ACCOUNT_ID"),   # insert requires the parent account to exist
    },
    "logi_users": {
        "label": "Logi.dbo.Users", "db": "logi", "schema": "dbo", "table": "Users",
        "pk": ["UserID"], "columns": ["UserID", "Password", "EmailID", "FullName"],
        "editable": ["UserID", "Password", "EmailID", "FullName"],
        "insert": ["UserID", "Password", "EmailID", "FullName"], "required": ["UserID"],
        # "Region Codes" was sent by the original Add/Copy User screens; the rest were server constants.
        "constants": {"Role Level": "Everything", "Region(Z_DOMINTL)": "Region",
                      "Territory (PM_TERRITORY)": "All", "Region Codes": "Everything"},
        "unique": ["UserID"], "upper": [],
    },
    "logi_roles": {
        "label": "Logi.dbo.Roles", "db": "logi", "schema": "dbo", "table": "Roles",
        "pk": ["UserID"], "columns": ["UserID"], "editable": [],
        "insert": ["UserID"], "required": ["UserID"],
        "constants": {"Role Level": "Region"}, "unique": ["UserID"], "upper": [],
    },
    "logi_rights": {
        "label": "Logi.dbo.Rights", "db": "logi", "schema": "dbo", "table": "Rights",
        "pk": ["UserID"], "columns": ["UserID", "PrimaryDashboardID"], "editable": [],
        "insert": ["UserID"], "required": ["UserID"],
        "constants": {"Region Codes": "Everything", "MenuTabIDs": "1", "PrimaryDashboardID": "1"},
        "unique": ["UserID"], "upper": [],
    },
    "logi_dash": {
        "label": "Customer tree access", "db": "sys", "schema": "dbo", "table": "@dash",
        "pk": ["OPRID", "TREE_NODE"], "columns": ["OPRID", "TREE_NAME", "TREE_NODE", "DEFAULT_TREE_NAME"],
        "editable": ["OPRID", "TREE_NAME", "TREE_NODE"],
        "insert": ["OPRID", "TREE_NAME", "TREE_NODE", "DEFAULT_TREE_NAME"], "required": ["OPRID", "TREE_NODE"],
        "constants": {}, "unique": ["OPRID", "TREE_NODE"], "upper": ["OPRID"],
    },
}
LOGI_LOOKUP = ("logi_users", "logi_roles", "logi_rights", "logi_dash")


# ---- settings ----------------------------------------------------------------------------------

def _cfg() -> dict:
    return config.section("toolkit", {}) or {}


def settings() -> dict:
    """Non-secret connection settings: config.yaml `toolkit:` defaults, overridden by the UI (SQLite kv)."""
    cfg, saved = _cfg(), db.kv_get(KV_SETTINGS, {}) or {}
    out = {k: (v.copy() if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    for src in (cfg, saved):
        for k in ("server", "username", "driver", "encrypt", "trust_server_certificate", "shipping_schema"):
            if src.get(k) not in (None, ""):
                out[k] = src[k]
        out["databases"].update({k: v for k, v in (src.get("databases") or {}).items() if k in out["databases"] and v})
    out["dash_table"] = cfg.get("dash_table") or "PS_ZZ_DASH_USERS_2025"
    tree = {"user": "DANA1", "tree": "DI_CUST_ROLLUP", "setid": "IT", "as_of": "12/31/2026"}
    tree.update({k: str(v) for k, v in (cfg.get("tree") or {}).items() if k in tree and v})
    out["tree"] = tree
    return out


def validate_settings(s: dict) -> dict:
    """Checks every field that ends up in a connection string or in SQL text as an identifier."""
    server, username = str(s.get("server", "")).strip(), str(s.get("username", "")).strip()
    if server and not SERVER_RE.match(server):
        raise ValueError("Server: letters, digits, . - _ \\ and , only (host, host\\instance or host,port)")
    if username and not USER_RE.match(username):
        raise ValueError("Username: letters, digits and . - _ \\ @ only")
    driver = s.get("driver") or DRIVERS[0]
    if driver not in DRIVERS:
        raise ValueError(f"Driver must be one of: {', '.join(DRIVERS)}")
    dbs = s.get("databases") or {}
    if set(dbs) - set(DEFAULTS["databases"]):
        raise ValueError("Unknown database key")
    for k, v in dbs.items():
        if not IDENT.match(str(v)):
            raise ValueError(f"Database name for {k}: letters, digits and _ only")
    schema = s.get("shipping_schema") or DEFAULTS["shipping_schema"]
    if not IDENT.match(str(schema)):
        raise ValueError("Shipping schema: letters, digits and _ only")
    for flag in ("encrypt", "trust_server_certificate"):
        if not isinstance(s.get(flag, True), bool):
            raise ValueError(f"{flag} must be true or false")
    return {"server": server, "username": username, "driver": driver,
            "encrypt": s.get("encrypt", True), "trust_server_certificate": s.get("trust_server_certificate", False),
            "databases": {k: str(v) for k, v in dbs.items()}, "shipping_schema": str(schema)}


def _keyring_user(s) -> str:
    return f"{s['username']}@{s['server']}"


def password_saved(s=None) -> bool:
    s = s or settings()
    if not (s["server"] and s["username"]):
        return False
    try:
        return keyring.get_password(KEYRING_SERVICE, _keyring_user(s)) is not None
    except Exception:
        return False


def _require_setup(s):
    if not (s["server"] and s["username"]):
        raise NotConfigured("Toolkit is not configured: enter the SQL server and username under Toolkit > Connection.")
    validate_settings(s)
    for name in (s["databases"][k] for k in s["databases"]):
        if not IDENT.match(name):
            raise NotConfigured("Toolkit database names are invalid; fix them under Toolkit > Connection.")
    if not IDENT.match(s["dash_table"]) or not IDENT.match(s["shipping_schema"]):
        raise NotConfigured("toolkit.dash_table / shipping_schema in config.yaml must be letters, digits and _ only.")


class NotConfigured(Exception):
    pass


# ---- connection ----------------------------------------------------------------------------------

def _brace(value) -> str:
    return "{" + str(value).replace("}", "}}") + "}"


def connection_string(s, database, password) -> str:
    parts = [
        f"DRIVER={{{s['driver']}}}",
        f"SERVER={_brace(s['server'])}",
        f"DATABASE={_brace(database)}",
        f"UID={_brace(s['username'])}",
        f"PWD={_brace(password)}",
        "Encrypt=" + ("yes" if s["encrypt"] else "no"),
        "TrustServerCertificate=" + ("yes" if s["trust_server_certificate"] else "no"),
        "APP=CommandCenter-Toolkit",
    ]
    return ";".join(parts)


_secret_for_scrub: set[str] = set()


def _connect(db_key: str, password: str | None = None):
    import pyodbc
    s = settings()
    _require_setup(s)
    if password is None:
        password = keyring.get_password(KEYRING_SERVICE, _keyring_user(s))
        if password is None:
            raise NotConfigured("No SQL password saved for the Toolkit: enter it under Toolkit > Connection.")
    if len(_secret_for_scrub) > 4:
        _secret_for_scrub.clear()
    _secret_for_scrub.add(password)
    if s["driver"] not in pyodbc.drivers():
        raise NotConfigured(f"{s['driver']} is not installed on this PC; pick another driver under Toolkit > Connection.")
    conn = pyodbc.connect(connection_string(s, s["databases"][db_key], password), autocommit=False, timeout=15)
    conn.timeout = 60
    return conn


def _clean_error(e: Exception) -> str:
    """One readable line, with any password scrubbed out (pyodbc messages never include it, but be sure)."""
    msg = str(e.args[1] if len(getattr(e, "args", ())) > 1 and isinstance(e.args[1], str) else e)
    for secret in _secret_for_scrub:
        if secret:
            msg = msg.replace(secret, "***")
    msg = re.sub(r"(\[[^\]]*\])+", "", msg.splitlines()[0] if msg else type(e).__name__)
    msg = re.sub(r"\s*\((\d+)\)\s*(\(SQL\w+\))?\s*$", "", msg).strip(" ;.")
    if re.search(r"certificate|SSL Provider|chain was issued", msg, re.I):
        msg += (". The SQL server's certificate couldn't be verified. Ask IT which CA issued it (this PC trusts "
                "Sonance's corp CAs), or, only if IT confirms the server, tick 'Trust server certificate' under Connection")
    return (msg or type(e).__name__)[:700]


def _jsonable(v):
    if isinstance(v, (datetime, date, dtime)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (bytes, bytearray)):
        return "0x" + v.hex()
    return v


def _rows(cur, limit=MAX_ROWS) -> dict:
    cols = [d[0] for d in cur.description] if cur.description else []
    raw = cur.fetchmany(limit + 1) if cols else []
    return {"columns": cols, "rows": [{c: _jsonable(v) for c, v in zip(cols, r)} for r in raw[:limit]],
            "truncated": len(raw) > limit}


def _read(db_key, sql_text, params=()) -> dict:
    """Read-only helper: runs in a transaction that is always rolled back."""
    conn = _connect(db_key)
    try:
        cur = conn.cursor()
        cur.execute(sql_text, list(params))
        return _rows(cur)
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


# ---- SQL building (pure functions; unit-tested without a server) -----------------------------------

def q(name: str) -> str:
    return "[" + str(name).replace("]", "]]") + "]"


def table_cfg(key: str) -> dict:
    t = TABLES.get(key) if isinstance(key, str) else None
    if not t:
        raise ValueError("Unknown table")
    return t


def full_name(key: str, s=None) -> str:
    s = s or settings()
    t = table_cfg(key)
    database = s["databases"][t["db"]]
    schema = s["shipping_schema"] if t["schema"] == "@shipping" else t["schema"]
    table = s["dash_table"] if t["table"] == "@dash" else t["table"]
    for part in (database, schema, table):
        if not IDENT.match(part):
            raise NotConfigured("Invalid database/schema/table name in toolkit settings")
    return f"{q(database)}.{q(schema)}.{q(table)}"


def _scalar(v, column):
    if v is None or isinstance(v, (bool, int, float)):
        if isinstance(v, float) and v != v:
            raise ValueError(f"{column}: invalid number")
        return v
    if isinstance(v, str):
        if len(v) > MAX_VALUE_LEN:
            raise ValueError(f"{column}: value is too long")
        if "\x00" in v:
            raise ValueError(f"{column}: invalid character")
        return v
    raise ValueError(f"{column}: only text, numbers or empty values are allowed")


def _where(t, pk: dict) -> tuple[str, list]:
    if not isinstance(pk, dict) or set(pk) != set(t["pk"]):
        raise ValueError(f"Key must be exactly: {', '.join(t['pk'])}")
    parts, params = [], []
    for c in t["pk"]:
        v = _scalar(pk[c], c)
        if v is None:
            parts.append(f"{q(c)} IS NULL")
        else:
            parts.append(f"{q(c)} = ?")
            params.append(v)
    return " AND ".join(parts), params


def _check_columns(values, allowed, what):
    if not isinstance(values, dict):
        raise ValueError(f"{what}: expected an object")
    bad = [c for c in values if c not in allowed]
    if bad:
        raise ValueError(f"{what}: column(s) not allowed: {', '.join(map(str, bad))[:200]}")
    return {c: _scalar(v, c) for c, v in values.items()}


def build_lock_select(key, pk, s=None):
    t = table_cfg(key)
    where, params = _where(t, pk)
    return f"SELECT * FROM {full_name(key, s)} WITH (UPDLOCK, HOLDLOCK) WHERE {where}", params


def build_count(key, where_values: dict, s=None):
    """COUNT(*) WHERE col = ? AND ... (used for insert-skip, parent and child checks)."""
    t = table_cfg(key)
    parts, params = [], []
    for c, v in where_values.items():
        if c not in t["columns"]:
            raise ValueError("Column not allowed")
        if v is None:
            parts.append(f"{q(c)} IS NULL")
        else:
            parts.append(f"{q(c)} = ?")
            params.append(_scalar(v, c))
    return f"SELECT COUNT(*) FROM {full_name(key, s)} WITH (UPDLOCK, HOLDLOCK) WHERE {' AND '.join(parts)}", params


def _guarded(dml: str) -> str:
    # NOCOUNT stops trigger row counts from mixing in; @@ROWCOUNT is read right after the statement.
    return f"SET NOCOUNT ON; DECLARE @cc_n int; {dml}; SET @cc_n = @@ROWCOUNT; SELECT @cc_n AS cc_affected;"


def build_insert(key, values, s=None):
    t = table_cfg(key)
    values = _check_columns(values, t["insert"], "Insert")
    for c in t["required"]:
        if values.get(c) in (None, ""):
            raise ValueError(f"{c} is required")
    for c in t["upper"]:
        if isinstance(values.get(c), str):
            values[c] = values[c].upper()
    row = {**values, **t["constants"]}   # constants win, as in the original toolkit
    if not row:
        raise ValueError("Nothing to insert")
    cols = ", ".join(q(c) for c in row)
    marks = ", ".join("?" for _ in row)
    return _guarded(f"INSERT INTO {full_name(key, s)} ({cols}) VALUES ({marks})"), list(row.values()), row


def build_update(key, pk, values, s=None):
    t = table_cfg(key)
    values = _check_columns(values, t["editable"], "Update")
    if not values:
        raise ValueError("Nothing to change")
    where, wparams = _where(t, pk)
    sets = ", ".join(f"{q(c)} = ?" for c in values)
    return _guarded(f"UPDATE {full_name(key, s)} SET {sets} WHERE {where}"), list(values.values()) + wparams, values


def build_delete(key, pk, s=None):
    t = table_cfg(key)
    where, params = _where(t, pk)
    return _guarded(f"DELETE FROM {full_name(key, s)} WHERE {where}"), params


# ---- change log (SQLite, append-only) --------------------------------------------------------------

_log_ready = False


def _log_db():
    global _log_ready
    conn = db._db()
    if not _log_ready:
        with db._lock:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS toolkit_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    windows_user TEXT,
                    connection TEXT,
                    table_key TEXT NOT NULL,
                    table_name TEXT,
                    pk TEXT,
                    action TEXT NOT NULL,
                    before_json TEXT,
                    after_json TEXT,
                    ok INTEGER,            -- NULL while running, 1 committed, 0 failed/rolled back
                    error TEXT,
                    batch TEXT
                );
                CREATE INDEX IF NOT EXISTS toolkit_changes_table ON toolkit_changes(table_key, id);
                CREATE TRIGGER IF NOT EXISTS toolkit_changes_no_delete BEFORE DELETE ON toolkit_changes
                BEGIN SELECT RAISE(ABORT, 'toolkit_changes is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS toolkit_changes_final BEFORE UPDATE ON toolkit_changes
                WHEN OLD.ok IS NOT NULL OR NEW.id IS NOT OLD.id OR NEW.ts IS NOT OLD.ts OR NEW.table_key IS NOT OLD.table_key
                     OR NEW.pk IS NOT OLD.pk OR NEW.action IS NOT OLD.action
                     OR NEW.windows_user IS NOT OLD.windows_user OR NEW.connection IS NOT OLD.connection
                BEGIN SELECT RAISE(ABORT, 'toolkit_changes rows are final'); END;
                """
            )
            conn.commit()
        _log_ready = True
    return conn


def _mask(row):
    if not isinstance(row, dict):
        return row
    return {k: ("•••• (hidden)" if k in MASKED_COLUMNS and v not in (None, "") else v) for k, v in row.items()}


def _windows_user() -> str:
    try:
        return os.getlogin()
    except OSError:
        return getpass.getuser()


def _dumps(v):
    return None if v is None else json.dumps(_mask({k: _jsonable(x) for k, x in v.items()}) if isinstance(v, dict) else v,
                                             default=str)


def log_start(key, action, pk, before, after, batch, s) -> int:
    conn = _log_db()
    with db._lock:
        cur = conn.execute(
            "INSERT INTO toolkit_changes (ts, windows_user, connection, table_key, table_name, pk, action, before_json,"
            " after_json, ok, error, batch) VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,?)",
            (db.now(), _windows_user(), f"{s['username']}@{s['server']}", key, full_name(key, s),
             json.dumps(pk, default=str) if pk is not None else None, action, _dumps(before), _dumps(after), batch))
        conn.commit()
        return cur.lastrowid


def log_finish(log_id, ok, error=None, before=None, after=None):
    """Finalizes a pending row (the SQLite trigger refuses any later change). `before` is the full row as
    read under lock from SQL Server, replacing the browser's copy logged at start."""
    conn = _log_db()
    with db._lock:
        conn.execute("UPDATE toolkit_changes SET ok = ?, error = ?, before_json = COALESCE(?, before_json),"
                     " after_json = COALESCE(?, after_json) WHERE id = ? AND ok IS NULL",
                     (1 if ok else 0, error, _dumps(before), _dumps(after), log_id))
        conn.commit()


def list_changes(table=None, limit=300) -> list[dict]:
    conn = _log_db()
    sql_text, params = "SELECT * FROM toolkit_changes", []
    if table:
        sql_text += " WHERE table_key = ?"
        params.append(table)
    sql_text += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    with db._lock:
        rows = conn.execute(sql_text, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("pk", "before_json", "after_json"):
            try:
                d[k] = json.loads(d[k]) if d[k] else None
            except ValueError:
                pass
        out.append(d)
    return out


# ---- write executor ---------------------------------------------------------------------------------

def _affected(cur) -> int:
    while True:
        if cur.description and cur.description[0][0] == "cc_affected":
            row = cur.fetchone()
            return int(row[0]) if row and row[0] is not None else -1
        if not cur.nextset():
            raise RuntimeError("The server did not report how many rows changed; rolled back")


def _norm(v):
    v = _jsonable(v)
    return None if v is None else str(v)


def _validate_op(op) -> dict:
    if not isinstance(op, dict):
        raise ValueError("Each change must be an object")
    action, key = op.get("action"), op.get("table")
    t = table_cfg(key)
    if action not in ("insert", "update", "delete"):
        raise ValueError("Action must be insert, update or delete")
    if action == "update" and not t["editable"]:
        raise ValueError(f"{t['label']} rows can't be edited here")
    if action in ("update", "delete"):
        _where(t, op.get("pk"))
        expect = op.get("expect")
        if not isinstance(expect, dict) or not expect:
            raise ValueError("Missing the row as you saw it (expect)")
        _check_columns(expect, t["columns"], "Expect")
    return op


def apply_changes(ops: list, connect=None, batch_label=None) -> dict:
    """Runs insert/update/delete operations in ONE transaction; each must touch exactly one row."""
    if not isinstance(ops, list) or not ops:
        raise ValueError("No changes given")
    if len(ops) > MAX_BATCH:
        raise ValueError(f"At most {MAX_BATCH} changes at once")
    ops = [_validate_op(op) for op in ops]
    s = settings()
    connect = connect or _connect
    batch_id = batch_label or (datetime.now().strftime("%Y%m%d%H%M%S%f") if len(ops) > 1 else None)

    # Build every statement first: a bad op fails before anything is logged or sent.
    plans = []
    for op in ops:
        key, action, t = op["table"], op["action"], TABLES[op["table"]]
        if action == "insert":
            sql_text, params, row = build_insert(key, op.get("values") or {}, s)
            plans.append({"op": op, "sql": sql_text, "params": params, "after": row, "pk": {c: row.get(c) for c in t["pk"]}
                          if all(c in row for c in t["pk"]) else None})
        elif action == "update":
            sql_text, params, changes = build_update(key, op["pk"], op.get("values") or {}, s)
            plans.append({"op": op, "sql": sql_text, "params": params, "changes": changes, "pk": op["pk"]})
        else:
            sql_text, params = build_delete(key, op["pk"], s)
            plans.append({"op": op, "sql": sql_text, "params": params, "pk": op["pk"]})

    _require_setup(s)   # after validation, so bad input is reported as such even before setup

    for p in plans:
        p["log"] = log_start(p["op"]["table"], p["op"]["action"], p["pk"], p["op"].get("expect"),
                             p.get("after") or p.get("changes"), batch_id, s)

    results, failure = [], None
    conn = None
    try:
        conn = connect(TABLES[ops[0]["table"]]["db"])
        cur = conn.cursor()
        for i, p in enumerate(plans):
            op, key, t = p["op"], p["op"]["table"], TABLES[p["op"]["table"]]
            label = f"#{i + 1} {t['label']} {op['action']}"
            if op["action"] == "insert":
                if t["unique"]:
                    sql_c, par_c = build_count(key, {c: p["after"].get(c) for c in t["unique"]}, s)
                    cur.execute(sql_c, par_c)
                    if cur.fetchone()[0] > 0:
                        p["skipped"] = True
                        results.append({"table": key, "action": "insert", "skipped": True,
                                        "reason": f"{t['label']}: a record already exists for "
                                                  + ", ".join(str(p['after'].get(c)) for c in t['unique'])})
                        continue
                if t.get("parent"):
                    pkey, pcol = t["parent"]
                    sql_c, par_c = build_count(pkey, {pcol: p["after"].get(pcol)}, s)
                    cur.execute(sql_c, par_c)
                    if cur.fetchone()[0] != 1:
                        raise ChangeRefused(f"{label}: parent account {p['after'].get(pcol)} does not exist")
            else:
                sql_l, par_l = build_lock_select(key, op["pk"], s)
                cur.execute(sql_l, par_l)
                found = _rows(cur, 3)
                if len(found["rows"]) != 1:
                    raise ChangeRefused(f"{label}: expected exactly 1 row for {op['pk']}, found "
                                        f"{len(found['rows'])}{'+' if found['truncated'] else ''}; nothing was changed")
                current = found["rows"][0]
                p["before"] = current
                stale = [c for c, v in op["expect"].items() if _norm(current.get(c)) != _norm(v)]
                if stale:
                    raise ChangeRefused(f"{label}: the row changed since you loaded it ({', '.join(stale)}). "
                                        "Reload and try again; nothing was changed")
                if op["action"] == "delete" and t.get("children"):
                    ckey = t["children"]
                    sql_c, par_c = build_count(ckey, {t["pk"][0]: op["pk"][t["pk"][0]]}, s)
                    cur.execute(sql_c, par_c)
                    n = cur.fetchone()[0]
                    if n:
                        raise ChangeRefused(f"{label}: this account still has {n} shipping method(s). "
                                            "Delete those first; nothing was changed")
            cur.execute(p["sql"], p["params"])
            n = _affected(cur)
            if n != 1:
                raise ChangeRefused(f"{label}: would have changed {n} rows instead of 1; rolled back")
            if op["action"] == "update":
                p["after"] = {**p["before"], **p["changes"]}
            results.append({"table": key, "action": op["action"], "ok": True})
        conn.commit()
    except Exception as e:
        failure = e
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    if failure is not None:
        msg = str(failure) if isinstance(failure, (ChangeRefused, NotConfigured)) else _clean_error(failure)
        for p in plans:
            log_finish(p["log"], False, msg if len(plans) == 1 else f"rolled back: {msg}", before=p.get("before"))
        log.warning("toolkit change refused/failed (%d op(s)): %s", len(plans), msg)
        if isinstance(failure, NotConfigured):
            raise failure
        raise ChangeRefused(msg)
    for p in plans:
        log_finish(p["log"], True, "skipped: already exists" if p.get("skipped") else None, before=p.get("before"),
                   after=p.get("after") if p["op"]["action"] != "delete" else None)
    log.info("toolkit: committed %d change(s): %s", len(plans),
             ", ".join(f"{p['op']['table']} {p['op']['action']}" for p in plans))
    return {"ok": True, "results": results, "skipped": [r["reason"] for r in results if r.get("skipped")]}


class ChangeRefused(Exception):
    pass


# ---- reads -----------------------------------------------------------------------------------------

def shipping_accounts():
    return _read("staging", f"SELECT * FROM {full_name('ship_account')} ORDER BY [ACCOUNT_ID]")


def shipping_methods(account_id: int):
    return _read("staging", f"SELECT * FROM {full_name('ship_method')} WHERE [ACCOUNT_ID] = ? ORDER BY [SHIPPING_METHOD_ID]",
                 [account_id])


def sps_search(pattern: str):
    # Same as the original: pattern is wrapped in %...% (LIKE wildcards in it still work), bound as a parameter.
    s = settings()
    return _read("staging", f"SELECT TOP 50 spsID, documentTimestamp FROM {q(s['databases']['staging'])}.[sps].[document] "
                 "WHERE contentType = 'Order' AND documentContent LIKE ? ORDER BY documentTimestamp DESC",
                 [f"%{pattern}%" if pattern else "%"])


def sps_carrier(spsid: str):
    s = settings()
    return _read("staging", "SELECT CarrierAlphaCode, CarrierRouting FROM "
                 f"{q(s['databases']['staging'])}.[sps].[Order_Header_CarrierInformation] WHERE spsID = ?", [spsid])


def check_carrier(values: list[str]):
    conn = _connect("staging")
    try:
        cur = conn.cursor()
        out = {}
        for v in values:
            cur.execute(f"SELECT * FROM {full_name('ship_method')} WHERE [INBOUND_ID] = ?", [v])
            out[v] = _rows(cur)["rows"]
        return out
    finally:
        conn.rollback()
        conn.close()


def logi_lookup(user_id: str):
    s = settings()
    conn_by_db, result = {}, {}
    try:
        for key in LOGI_LOOKUP:
            t = TABLES[key]
            value = user_id.upper() if key == "logi_dash" else user_id
            try:
                if t["db"] not in conn_by_db:
                    conn_by_db[t["db"]] = _connect(t["db"])
                cur = conn_by_db[t["db"]].cursor()
                cur.execute(f"SELECT * FROM {full_name(key, s)} WHERE {q(t['pk'][0])} = ?", [value])
                result[key] = {**_rows(cur, 500), "pk": t["pk"], "label": t["label"],
                               "constants": t["constants"]}
            except NotConfigured:
                raise
            except Exception as e:
                result[key] = {"error": _clean_error(e), "label": t["label"]}
    finally:
        for c in conn_by_db.values():
            try:
                c.rollback()
                c.close()
            except Exception:
                pass
    return result


def logi_all():
    users = _read("logi", f"SELECT [UserID], [Password], [EmailID], [FullName] FROM {full_name('logi_users')} ORDER BY [UserID]")
    dash = _read("sys", f"SELECT [OPRID], [TREE_NAME], [TREE_NODE], [DEFAULT_TREE_NAME] FROM {full_name('logi_dash')} ORDER BY [OPRID]")
    return {"users": users, "dash": dash}


def customer_tree():
    s = settings()
    tr = s["tree"]
    res = _read("staging", "SELECT TREE_LEVEL_NUM, REPLACE(NODE_DESCR, '&', ' AND ') AS NODE_DESCR, TREE_NODE FROM "
                f"{q(s['databases']['staging'])}.[dashBoard].[rsTreeStructureByUser](?, ?, ?, ?)",
                [tr["user"], tr["tree"], tr["setid"], tr["as_of"]])
    rows = [{"level": int(r["TREE_LEVEL_NUM"]) if r["TREE_LEVEL_NUM"] is not None else 1,
             "label": "" if r["NODE_DESCR"] is None else str(r["NODE_DESCR"]),
             "node": "" if r["TREE_NODE"] is None else str(r["TREE_NODE"])} for r in res["rows"]]
    return {"rows": rows, "tree": tr["tree"], "as_of": tr["as_of"], "truncated": res["truncated"]}


def test_connection(password=None) -> dict:
    s = settings()
    conn = _connect("staging", password)
    try:
        cur = conn.cursor()
        cur.execute("SELECT SUSER_SNAME() AS login_name, @@SERVERNAME AS server_name, DB_NAME() AS database_name")
        r = cur.fetchone()
        info = {"login": r[0], "server": r[1], "database": r[2], "host": s["server"], "at": db.now()}
    finally:
        conn.rollback()
        conn.close()
    db.kv_set(KV_LAST_TEST, info)
    return info


# ---- routes ------------------------------------------------------------------------------------------

async def _run(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except NotConfigured as e:
        raise HTTPException(409, str(e))
    except (ValueError, ChangeRefused) as e:
        raise HTTPException(400, str(e))
    except HTTPException:
        raise
    except Exception as e:
        msg = _clean_error(e)
        log.warning("toolkit request failed: %s", msg)
        raise HTTPException(502, msg)


def _text(v, name, max_len=200, required=False) -> str:
    if v is None:
        v = ""
    if not isinstance(v, (str, int)):
        raise HTTPException(400, f"{name}: expected text")
    v = str(v).strip()
    if len(v) > max_len or "\x00" in v:
        raise HTTPException(400, f"{name}: too long")
    if required and not v:
        raise HTTPException(400, f"{name} is required")
    return v


def _public_settings():
    s = settings()
    try:
        import pyodbc
        installed = [d for d in DRIVERS if d in pyodbc.drivers()]
    except Exception:
        installed = []
    return {"settings": {k: s[k] for k in ("server", "username", "driver", "encrypt", "trust_server_certificate",
                                            "databases", "shipping_schema", "dash_table", "tree")},
            "configured": bool(s["server"] and s["username"]), "password_saved": password_saved(s),
            "drivers": list(DRIVERS), "installed_drivers": installed, "last_test": db.kv_get(KV_LAST_TEST),
            "keyring": {"service": KEYRING_SERVICE, "user": _keyring_user(s) if s["server"] and s["username"] else None},
            "tables": {k: {f: t[f] for f in ("label", "pk", "columns", "editable", "insert", "constants", "unique")}
                       | {"name": _safe_name(k, s)}
                       for k, t in TABLES.items()}}


def _safe_name(key, s):
    try:
        return full_name(key, s)
    except Exception:
        return None


@router.get("/settings")
async def get_settings():
    return await asyncio.to_thread(_public_settings)


@router.post("/settings")
async def save_settings(body: dict = Body(...)):
    try:
        clean = validate_settings({**settings(), **{k: v for k, v in body.items() if k in DEFAULTS}})
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.kv_set(KV_SETTINGS, clean)
    db.kv_set(KV_LAST_TEST, None)
    return await asyncio.to_thread(_public_settings)


@router.post("/password")
async def save_password(password: str = Body(..., embed=True)):
    """Tests the password first; it is stored in Credential Manager only if the login works. Never echoed."""
    if not isinstance(password, str) or not password or len(password) > 256 or "\x00" in password:
        raise HTTPException(400, "Enter the SQL password (up to 256 characters)")
    info = await _run(test_connection, password)
    s = settings()
    await asyncio.to_thread(keyring.set_password, KEYRING_SERVICE, _keyring_user(s), password)
    return {"ok": True, "test": info}


@router.post("/forget")
async def forget_password():
    s = settings()
    if s["server"] and s["username"]:
        try:
            await asyncio.to_thread(keyring.delete_password, KEYRING_SERVICE, _keyring_user(s))
        except keyring.errors.PasswordDeleteError:
            pass
    db.kv_set(KV_LAST_TEST, None)
    return {"ok": True}


@router.post("/test")
async def test():
    return await _run(test_connection)


@router.post("/shipping/accounts")
async def get_accounts():
    return await _run(shipping_accounts)


@router.post("/shipping/methods")
async def get_methods(account_id: int = Body(..., embed=True)):
    return await _run(shipping_methods, account_id)


@router.post("/sps/search")
async def sps_lookup(pattern: str = Body("", embed=True)):
    return await _run(sps_search, _text(pattern, "Search text"))


@router.post("/sps/carrier")
async def sps_carrier_info(spsid: str = Body(..., embed=True)):
    return await _run(sps_carrier, _text(spsid, "SPS ID", 100, True))


@router.post("/sps/check")
async def sps_check(CarrierAlphaCode: str | None = Body(None), CarrierRouting: str | None = Body(None)):
    values = []
    for v, n in ((CarrierAlphaCode, "CarrierAlphaCode"), (CarrierRouting, "CarrierRouting")):
        v = _text(v, n)
        if v and v not in values:
            values.append(v)
    if not values:
        raise HTTPException(400, "No carrier values to check")
    return {"results": await _run(check_carrier, values)}


@router.post("/logi/all")
async def get_logi_all():
    return await _run(logi_all)


@router.post("/logi/user")
async def get_logi_user(user_id: str = Body(..., embed=True)):
    return await _run(logi_lookup, _text(user_id, "User ID", 128, True))


@router.post("/tree")
async def get_tree():
    return await _run(customer_tree)


@router.post("/rows/insert")
async def row_insert(table: str = Body(...), values: dict = Body(...)):
    return await _run(apply_changes, [{"action": "insert", "table": table, "values": values}])


@router.post("/rows/update")
async def row_update(table: str = Body(...), pk: dict = Body(...), values: dict = Body(...), expect: dict = Body(...)):
    return await _run(apply_changes, [{"action": "update", "table": table, "pk": pk, "values": values, "expect": expect}])


@router.post("/rows/delete")
async def row_delete(table: str = Body(...), pk: dict = Body(...), expect: dict = Body(...)):
    return await _run(apply_changes, [{"action": "delete", "table": table, "pk": pk, "expect": expect}])


@router.post("/rows/batch")
async def row_batch(ops: list = Body(..., embed=True)):
    return await _run(apply_changes, ops)


@router.get("/changes")
async def changes(table: str = "", limit: int = 300):
    if table and table not in TABLES:
        raise HTTPException(400, "Unknown table")
    return {"changes": await asyncio.to_thread(list_changes, table or None, max(1, min(limit, 2000))),
            "tables": {k: t["label"] for k, t in TABLES.items()}}
