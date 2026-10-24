"""SQL Server connector: saved, parameterized, read-only queries.

Passwords live in Windows Credential Manager (service "CommandCenter-SQL"), never in config.
Every query runs inside a transaction that is rolled back, and only SELECT/WITH statements run.
"""
import re
import time
from datetime import date, datetime, time as dtime
from decimal import Decimal

import keyring
import pyodbc

from .. import config

KEYRING_SERVICE = "CommandCenter-SQL"
MAX_ROWS = 1000


def connections() -> list[dict]:
    return config.section("sql", {}).get("connections") or []


def queries() -> list[dict]:
    return config.section("sql", {}).get("queries") or []


def _connection(name) -> dict:
    c = next((c for c in connections() if c.get("name") == name), None)
    if not c:
        raise ValueError(f"No SQL connection named {name!r} in config.yaml")
    return c


def _key(c) -> str:
    return f"{c['name']}:{c.get('username', '')}"


def has_password(name) -> bool:
    c = _connection(name)
    return c.get("auth") == "windows" or keyring.get_password(KEYRING_SERVICE, _key(c)) is not None


def set_password(name, password):
    keyring.set_password(KEYRING_SERVICE, _key(_connection(name)), password)


def _brace(value: str) -> str:
    return "{" + str(value).replace("}", "}}") + "}"


def _connect(name) -> pyodbc.Connection:
    c = _connection(name)
    parts = [
        f"DRIVER={{{c.get('driver', 'ODBC Driver 18 for SQL Server')}}}",
        f"SERVER={_brace(c['server'])}",
        f"DATABASE={_brace(c['database'])}",
        "Encrypt=" + ("yes" if c.get("encrypt", True) else "no"),
        "TrustServerCertificate=" + ("yes" if c.get("trust_server_certificate", True) else "no"),
        "ApplicationIntent=ReadOnly",
        "APP=CommandCenter",
    ]
    if c.get("auth") == "windows":
        parts.append("Trusted_Connection=yes")
    else:
        password = keyring.get_password(KEYRING_SERVICE, _key(c))
        if password is None:
            raise ValueError(f"No password saved for {name!r}. Set it on the SQL tab or run: run.cmd setpass {name}")
        parts += [f"UID={_brace(c['username'])}", f"PWD={_brace(password)}"]
    conn = pyodbc.connect(";".join(parts), autocommit=False, timeout=15)
    conn.timeout = c.get("query_timeout", 60)
    return conn


def _first_keyword(sql: str) -> str:
    stripped = re.sub(r"(--[^\n]*\n)|(/\*.*?\*/)", " ", sql + "\n", flags=re.S).strip()
    return stripped.split(None, 1)[0].upper() if stripped else ""


def _jsonable(v):
    if isinstance(v, (datetime, date, dtime)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (bytes, bytearray)):
        return "0x" + v.hex()
    return v


def _run(conn_name, sql, params=()) -> dict:
    if _first_keyword(sql) not in ("SELECT", "WITH"):
        raise ValueError("Only SELECT / WITH queries are allowed")
    started = time.perf_counter()
    conn = _connect(conn_name)
    try:
        cur = conn.cursor()
        cur.execute(sql, list(params))
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(MAX_ROWS + 1) if columns else []
        truncated = len(rows) > MAX_ROWS
        return {
            "columns": columns,
            "rows": [[_jsonable(v) for v in r] for r in rows[:MAX_ROWS]],
            "truncated": truncated,
            "seconds": round(time.perf_counter() - started, 2),
        }
    finally:
        conn.rollback()
        conn.close()


def run_saved(query_name, params: list) -> dict:
    q = next((q for q in queries() if q.get("name") == query_name), None)
    if not q:
        raise ValueError(f"No saved query named {query_name!r}")
    expected = len(q.get("params") or [])
    if len(params) != expected:
        raise ValueError(f"{query_name!r} needs {expected} value(s)")
    return _run(q["connection"], q["sql"], params)


def test(conn_name) -> dict:
    return _run(conn_name, "SELECT @@SERVERNAME AS server_name, DB_NAME() AS database_name, SUSER_SNAME() AS login_name")


def tables(conn_name) -> list[str]:
    result = _run(conn_name, "SELECT TABLE_SCHEMA + '.' + TABLE_NAME FROM INFORMATION_SCHEMA.TABLES ORDER BY 1")
    return [r[0] for r in result["rows"]]


def preview(conn_name, table, top=100) -> dict:
    if table not in tables(conn_name):
        raise ValueError(f"Unknown table {table!r}")
    schema, name = table.split(".", 1)
    quoted = "[" + schema.replace("]", "]]") + "].[" + name.replace("]", "]]") + "]"
    return _run(conn_name, f"SELECT TOP ({int(top)}) * FROM {quoted}")
