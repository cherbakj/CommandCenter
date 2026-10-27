"""Pulse feedback tickets, read straight from Pulse's Postgres (Supabase), read-only.

What a "ticket" is in Pulse: a row in public.data_reviews (the Feedback page). It has a
ticket_no (#1001 and up), a type (Bug, DataIssue, FeatureRequest, Feedback, DataReview),
a headline (sub_section), detail (note), the reporter (flagged_by), and its state:
resolved, on_hold, and monkey_with ('admin' = waiting on the Pulse team, 'reporter' =
waiting on the person who filed it). Pulse has no assignee or priority columns, so here:
  - "mine"     = open and waiting on the Pulse team (Joe is the only master admin who works
                 tickets), or filed/closed by Joe himself (pulse.me in config.yaml)
  - priority   = derived from type (pulse.high_priority_types -> high; FeatureRequest ->
                 normal; everything else -> low)
Ticket link: https://pulse.sonance.com/feedback?ticket=<ticket_no>

Ticket aging (tickets waiting on Joe):
  - "waiting on Joe" = a "mine" ticket that is open (not resolved) and not on hold. A hold
    pauses the wait, the same as open_mine in the summary.
  - It has been waiting since the LATEST of these columns (whichever are present):
      created_at  - filed (the monkey starts with the admins)
      monkey_at   - the monkey last moved; it's on Joe's side now, so that move handed it over
      replied_at  - the latest admin reply (Pulse's cache of the last admin message)
      hold_at     - only when NOT on hold: when it came off hold (Pulse stamps hold_at on release)
    Pulse doesn't record when a ticket is reopened (resolved_at is cleared), so a reopened
    ticket ages from its last monkey move / reply / creation.
  - Age = business days since then: weekdays strictly after the start date, up to and including
    today, with a weekend start counted from the Monday. Same day = 0, Friday -> Monday = 1,
    Saturday -> Monday = 0. Holidays aren't known, so they count.
  - Buckets: fresh (< pulse.aging_warn_days), warn, overdue (>= pulse.aging_overdue_days);
    "unknown" if the row has none of the columns above.
  - One daily alert on workday mornings (pulse.aging_alert_time) if any are warn/overdue.

Security:
  - Connects as a dedicated Postgres login that can only SELECT the listed columns of
    data_reviews (setup SQL: SETUP_SQL below / the Pulse tab). Setup refuses a login that
    can write, is a superuser, bypasses RLS, or belongs to postgres/service_role.
  - Every session is READ ONLY and rolled back; only the fixed, parameterised SELECT here runs.
  - TLS with certificate and host-name verification (sslmode=verify-full).
  - The password lives only in Windows Credential Manager (CommandCenter-Pulse), bound to
    user@host. It is never written to config, the database, logs, or any response.
"""
import asyncio
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

import keyring
import keyring.errors
from fastapi import APIRouter, Body, HTTPException

from . import alerts, config, db

log = logging.getLogger(__name__)
router = APIRouter()

KEYRING_SERVICE = "CommandCenter-Pulse"
DEFAULTS = {
    "enabled": True,
    "url": "https://pulse.sonance.com",
    "poll_minutes": 2,
    "host": "",
    "port": 5432,
    "database": "postgres",
    "user": "",
    "ssl_root_cert": "system",
    "me": ["joec@sonance.com", "Joe.Cherbak"],
    "high_priority_types": ["Bug", "DataIssue"],
    "recent_days": 7,
    "alert_new": True,
    "alert_changes": True,
    "aging_warn_days": 2,
    "aging_overdue_days": 5,
    "aging_alert_time": "08:00",
}
CONNECTION_KEYS = ("host", "port", "database", "user", "ssl_root_cert")
COLUMNS = ("id", "ticket_no", "type", "page", "section", "sub_section", "flagged_by", "note",
           "created_at", "resolved", "resolved_by", "resolved_at", "monkey_with", "monkey_by",
           "monkey_at", "on_hold", "hold_by", "hold_at", "replied_at")
# Logins that are never acceptable here: they can read or change everything.
PRIVILEGED_USERS = {"postgres", "service_role", "supabase_admin", "authenticator", "supabase_auth_admin",
                    "supabase_storage_admin", "dashboard_user", "pgbouncer", "authenticated", "anon"}

SETUP_SQL = """-- Run once in the Supabase SQL editor of the PRODUCTION Pulse project.
-- Creates a login that can only read the ticket columns Command Center shows.
-- Use a long random password, and type it only into Command Center's setup card.
create role commandcenter_ro login password 'CHANGE-ME-long-random'
  nosuperuser nocreatedb nocreaterole noinherit nobypassrls connection limit 3;
alter role commandcenter_ro set default_transaction_read_only = on;
alter role commandcenter_ro set statement_timeout = '15s';
grant usage on schema public to commandcenter_ro;
grant select (""" + ", ".join(COLUMNS) + """)
  on public.data_reviews to commandcenter_ro;
-- data_reviews has RLS on with no policies (deny-all); this lets ONLY this role read it.
create policy commandcenter_ro_read on public.data_reviews
  for select to commandcenter_ro using (true);
-- To revoke later:  alter role commandcenter_ro nologin;"""

_HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?)+$")
_DB_RE = re.compile(r"^[A-Za-z0-9_-]{1,63}$")
_USER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


# ---- settings ------------------------------------------------------------------

def settings() -> dict:
    """config.yaml's pulse: section gives the defaults; the setup card's values (kv) win."""
    s = dict(DEFAULTS)
    s.update({k: v for k, v in (config.section("pulse", {}) or {}).items() if v is not None and v != ""})
    s.update(db.kv_get("pulse:settings") or {})
    return s


def _me(s: dict) -> set[str]:
    me = s.get("me") or []
    return {str(m).strip().lower() for m in ([me] if isinstance(me, str) else me) if str(m).strip()}


def _account(s: dict) -> str:
    return f"{s.get('user', '')}@{s.get('host', '')}"


def _password(s: dict) -> str | None:
    if not s.get("host") or not s.get("user"):
        return None
    try:
        return keyring.get_password(KEYRING_SERVICE, _account(s))
    except keyring.errors.KeyringError:
        return None


def is_configured(s: dict | None = None) -> bool:
    s = s or settings()
    return bool(s.get("enabled", True)) and _password(s) is not None


def ticket_url(ticket_no) -> str:
    base = str(settings().get("url") or DEFAULTS["url"]).rstrip("/")
    if not base.startswith("https://"):
        base = DEFAULTS["url"]
    return f"{base}/feedback?ticket={int(ticket_no)}" if ticket_no is not None else f"{base}/feedback"


def validate_settings(raw: dict) -> dict:
    """Checks the non-secret connection settings from the setup card. Raises ValueError."""
    host = str(raw.get("host") or "").strip().lower()
    if not _HOST_RE.match(host):
        raise ValueError("Host must be a plain host name, e.g. aws-0-us-west-1.pooler.supabase.com (no https://, no port)")
    try:
        port = int(raw.get("port") or 5432)
    except (TypeError, ValueError):
        raise ValueError("Port must be a number") from None
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    database = str(raw.get("database") or "postgres").strip()
    if not _DB_RE.match(database):
        raise ValueError("Database name may only use letters, digits, _ and -")
    user = str(raw.get("user") or "").strip()
    if not _USER_RE.match(user):
        raise ValueError("User may only use letters, digits, _ . and -")
    if user.split(".")[0].lower() in PRIVILEGED_USERS:
        raise ValueError(f"'{user.split('.')[0]}' can read and change everything. Create the read-only "
                         "commandcenter_ro login (see 'Setup SQL') and use that.")
    cert = str(raw.get("ssl_root_cert") or "system").strip()
    if cert != "system":
        p = Path(cert)
        if p.suffix.lower() not in (".crt", ".pem", ".cer") or not p.is_file():
            raise ValueError("CA certificate must be 'system' or the path of an existing .crt/.pem file")
        cert = str(p.resolve())
    return {"host": host, "port": port, "database": database, "user": user, "ssl_root_cert": cert}


# ---- database ------------------------------------------------------------------

def build_query(*, status="open", mine=False, me=(), search="", limit=100, recent_days=None):
    """Returns (sql, params). Only fixed SQL fragments are joined; every value is a parameter.
    status: open | all | recent (open, plus anything resolved in the last recent_days days)."""
    where, params = [], []
    if status == "open":
        where.append("not resolved")
    elif status == "recent":
        where.append("(not resolved or resolved_at >= now() - make_interval(days => %s))")
        params.append(int(recent_days or 7))
    elif status != "all":
        raise ValueError("status must be open, all or recent")
    if mine:
        me_list = sorted({m.lower() for m in me})
        where.append("(monkey_with = 'admin' or (monkey_with is null and not resolved)"
                     " or lower(flagged_by) = any(%s) or lower(coalesce(resolved_by, '')) = any(%s))")
        params += [me_list, me_list]
    search = (search or "").strip()[:100]
    if search:
        if re.fullmatch(r"#?\d{1,9}", search):
            where.append("ticket_no = %s")
            params.append(int(search.lstrip("#")))
        else:
            pattern = "%" + re.sub(r"([\\%_])", r"\\\1", search) + "%"
            fields = ("sub_section", "note", "page", "section", "flagged_by", "type")
            where.append("(" + " or ".join(f"{f} ilike %s escape '\\'" for f in fields) + ")")
            params += [pattern] * len(fields)
    sql = f"select {', '.join(COLUMNS)} from public.data_reviews"
    if where:
        sql += " where " + " and ".join(where)
    sql += " order by created_at desc, id limit %s"
    params.append(max(1, min(int(limit), 2000)))
    return sql, params


PRIVILEGE_SQL = """
select r.rolsuper or r.rolbypassrls or r.rolcreaterole as powerful,
       has_table_privilege(current_user, 'public.data_reviews', 'INSERT, UPDATE, DELETE, TRUNCATE') as writes_tickets,
       exists (select 1 from information_schema.table_privileges
               where grantee = current_user and privilege_type in ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE')) as writes_any,
       exists (select 1 from pg_roles p
               where p.rolname in ('postgres', 'service_role', 'supabase_admin', 'authenticated', 'anon')
                 and pg_has_role(current_user, p.oid, 'MEMBER')) as privileged_member
from pg_roles r where r.rolname = current_user
"""


def _connect(s: dict, password: str):
    try:
        import psycopg
    except ImportError:
        raise RuntimeError("The Postgres driver is missing. Run: .venv\\Scripts\\python.exe -m pip install \"psycopg[binary]\"") from None
    conn = psycopg.connect(
        host=s["host"], port=int(s["port"]), dbname=s["database"], user=s["user"], password=password,
        sslmode="verify-full", sslrootcert=s.get("ssl_root_cert") or "system",
        connect_timeout=10, application_name="CommandCenter (read-only)", prepare_threshold=None)
    conn.read_only = True  # every transaction on this connection is BEGIN READ ONLY
    return conn


def _clean_error(e: Exception, password: str | None) -> str:
    msg = (str(e).strip().splitlines() or [type(e).__name__])[0]
    if password:
        msg = msg.replace(password, "***")
    return msg[:300]


def _run(s: dict, password: str, sql: str, params, check_privileges=False) -> list[dict]:
    conn = _connect(s, password)
    try:
        with conn.cursor() as cur:
            cur.execute("set local statement_timeout = '15s'")
            if check_privileges:
                cur.execute(PRIVILEGE_SQL)
                row = cur.fetchone()
                if row is None or any(row):
                    raise PermissionError("This login can change data or has admin rights. Command Center only "
                                          "accepts the read-only commandcenter_ro login (see 'Setup SQL').")
            cur.execute(sql, params)
            names = [c.name for c in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


_privileges_checked = False


def query(sql, params) -> list[dict]:
    """Runs one read-only query with the stored login. Re-checks the login's rights once per run."""
    global _privileges_checked
    s = settings()
    password = _password(s)
    if password is None:
        raise ValueError("Pulse isn't connected yet. Set it up on the Pulse Feedback tab.")
    try:
        rows = _run(s, password, sql, params, check_privileges=not _privileges_checked)
    except Exception as e:
        raise RuntimeError(_clean_error(e, password)) from None
    _privileges_checked = True
    return rows


# ---- shaping -------------------------------------------------------------------

def _iso(v):
    return v.isoformat() if isinstance(v, datetime) else v


def status_of(r: dict) -> str:
    if r.get("resolved"):
        return "Resolved"
    if r.get("on_hold"):
        return "On hold"
    if r.get("monkey_with") == "reporter":
        return "Waiting on reporter"
    return "Open"


def priority_of(r: dict, s: dict) -> str:
    high = {str(t).lower() for t in s.get("high_priority_types") or []}
    t = str(r.get("type") or "").lower()
    if t in high:
        return "high"
    return "normal" if t == "featurerequest" else "low"


def is_mine(r: dict, me: set[str]) -> bool:
    if (r.get("flagged_by") or "").lower() in me or (r.get("resolved_by") or "").lower() in me:
        return True
    if r.get("resolved"):
        return False
    return r.get("monkey_with") in ("admin", None)


def _when(v) -> datetime | None:
    """A timestamp column as an aware datetime (naive = local time), or None."""
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v.strip())
        except ValueError:
            return None
    if not isinstance(v, datetime):
        return None
    return v.astimezone()


def waiting_since(r: dict) -> datetime | None:
    """When an open ticket started waiting on its current side (see the module docstring)."""
    cols = ["created_at", "monkey_at", "replied_at"] + ([] if r.get("on_hold") else ["hold_at"])
    times = [t for t in (_when(r.get(c)) for c in cols) if t is not None]
    return max(times) if times else None


def business_days(start: datetime, now: datetime) -> int:
    """Weekdays strictly after start's date up to and including now's date (a weekend start
    counts from Monday), in now's time zone. 0 for the same day or a start in the future."""
    now = _when(now)
    first = _when(start).astimezone(now.tzinfo).date()
    last = now.date()
    if first.weekday() >= 5:  # Saturday/Sunday -> the Monday after
        first += timedelta(days=7 - first.weekday())
    if last <= first:
        return 0
    weeks, rest = divmod((last - first).days, 7)
    return weeks * 5 + sum(1 for i in range(1, rest + 1) if (first + timedelta(days=i)).weekday() < 5)


def _int_setting(v, default: int, lo: int, hi: int) -> int:
    try:
        v = int(v)
    except (TypeError, ValueError):
        return default
    return min(max(v, lo), hi)


def aging_thresholds(s: dict) -> tuple[int, int]:
    """(warn_days, overdue_days) from config; overdue is never below warn."""
    warn = _int_setting(s.get("aging_warn_days"), DEFAULTS["aging_warn_days"], 1, 60)
    overdue = _int_setting(s.get("aging_overdue_days"), DEFAULTS["aging_overdue_days"], 1, 365)
    return warn, max(warn, overdue)


def aging_bucket(age_days: int | None, warn: int, overdue: int) -> str:
    if age_days is None:
        return "unknown"
    return "overdue" if age_days >= overdue else "warn" if age_days >= warn else "fresh"


def waiting_on_me(r: dict, me: set[str]) -> bool:
    return not r.get("resolved") and not r.get("on_hold") and is_mine(r, me)


def shape(r: dict, s: dict, me: set[str], now: datetime | None = None) -> dict:
    now = now or datetime.now().astimezone()
    waiting = waiting_on_me(r, me)
    since = waiting_since(r) if waiting else None
    age = business_days(since, now) if since else None
    return {
        "id": str(r["id"]),
        "ticket_no": r.get("ticket_no"),
        "title": (r.get("sub_section") or r.get("note") or "(no headline)")[:300],
        "detail": (r.get("note") or "")[:1000],
        "type": r.get("type"),
        "where": " / ".join(x for x in (r.get("page"), r.get("section")) if x),
        "requester": r.get("flagged_by"),
        "with": None if r.get("resolved") else ("reporter" if r.get("monkey_with") == "reporter" else "Pulse team"),
        "status": status_of(r),
        "priority": priority_of(r, s),
        "mine": is_mine(r, me),
        "created_at": _iso(r.get("created_at")),
        "updated_at": _iso(max((v for v in (r.get("created_at"), r.get("monkey_at"), r.get("hold_at"),
                                            r.get("replied_at"), r.get("resolved_at")) if v), default=None)),
        "resolved_by": r.get("resolved_by"),
        "url": ticket_url(r.get("ticket_no")),
        "waiting_on_me": waiting,
        "waiting_since": since.isoformat(timespec="seconds") if since else None,
        "age_days": age,
        "aging": aging_bucket(age, *aging_thresholds(s)) if waiting else None,
    }


def fetch(*, status="open", mine=False, search="", limit=100) -> list[dict]:
    s = settings()
    me = _me(s)
    sql, params = build_query(status=status, mine=mine, me=me, search=search, limit=limit,
                              recent_days=s.get("recent_days"))
    now = datetime.now().astimezone()
    return [shape(r, s, me, now) for r in query(sql, params)]


AGING_BUCKETS = ("fresh", "warn", "overdue", "unknown")


def aging_report(tickets: list[dict], s: dict | None = None) -> dict:
    """Tickets waiting on Joe (shaped), bucketed by age, oldest first (unknown ages last)."""
    warn, overdue = aging_thresholds(s or settings())
    waiting = [t for t in tickets if t.get("waiting_on_me")]
    waiting.sort(key=lambda t: (t["age_days"] is None, -(t["age_days"] or 0), t["waiting_since"] or "",
                                t["ticket_no"] or 0))
    counts = {b: sum(1 for t in waiting if t["aging"] == b) for b in AGING_BUCKETS}
    oldest = waiting[0] if waiting and waiting[0]["age_days"] is not None else None
    return {"warn_days": warn, "overdue_days": overdue, "total": len(waiting), "counts": counts,
            "oldest_days": oldest["age_days"] if oldest else None,
            "oldest_ticket_no": oldest["ticket_no"] if oldest else None, "tickets": waiting}


def _aging_summary(rep: dict) -> dict:
    """The numbers only (no ticket text): the summary is cached in the kv status row."""
    c = rep["counts"]
    return {"aging_fresh": c["fresh"], "aging_warn": c["warn"], "aging_overdue": c["overdue"],
            "aging_unknown": c["unknown"], "aging_waiting": rep["total"], "oldest_days": rep["oldest_days"],
            "oldest_ticket_no": rep["oldest_ticket_no"], "aging_warn_days": rep["warn_days"],
            "aging_overdue_days": rep["overdue_days"]}


def summarize(tickets: list[dict]) -> dict:
    today = datetime.now().astimezone().date()

    def created_today(t):
        try:
            return datetime.fromisoformat(t["created_at"]).astimezone().date() == today
        except (TypeError, ValueError):
            return False

    open_ = [t for t in tickets if t["status"] != "Resolved"]
    return {
        "open_mine": sum(1 for t in open_ if t["mine"] and t["status"] != "On hold"),
        "new_today": sum(1 for t in tickets if created_today(t)),
        "high_priority": sum(1 for t in open_ if t["priority"] == "high"),
        "open_total": len(open_),
        "on_hold": sum(1 for t in open_ if t["status"] == "On hold"),
        **_aging_summary(aging_report(tickets)),
    }


# ---- polling and alerts --------------------------------------------------------

def _set_status(**kw):
    st = db.kv_get("pulse:status") or {}
    st.update(kw)
    db.kv_set("pulse:status", st)


def diff_alerts(old: dict | None, tickets: list[dict], me: set[str]) -> tuple[list[dict], dict]:
    """Compares a poll with the saved state. Returns (alerts to raise, new state).
    The first poll (old is None) only records a baseline, so connecting doesn't flood the feed."""
    s = settings()
    now = datetime.now().astimezone()
    new_state = {"ts": now.isoformat(timespec="seconds"),
                 "tickets": {t["id"]: {"status": t["status"], "priority": t["priority"], "mine": t["mine"],
                                       "ticket_no": t["ticket_no"]} for t in tickets}}
    out = []
    if not old or "tickets" not in old:
        return out, new_state
    try:
        since = datetime.fromisoformat(old["ts"]).timestamp() - 300
    except (KeyError, TypeError, ValueError):
        since = now.timestamp() - 3600
    for t in tickets:
        prev = old["tickets"].get(t["id"])
        action = {"type": "pulse", "url": t["url"], "label": "Open in Pulse"}  # pulse.js: the Pulse tab
        label = f"#{t['ticket_no']} {t['title'][:80]}"
        if prev is None:
            if t["mine"] and t["status"] != "Resolved" and s.get("alert_new", True):
                try:
                    fresh = datetime.fromisoformat(t["created_at"]).timestamp() >= since
                except (TypeError, ValueError):
                    fresh = True
                out.append({"toast": "new ticket for you" if fresh else "ticket reopened",
                            "title": f"{'New' if fresh else 'Reopened'} ticket {label}",
                            "body": f"{t['type']} from {t['requester']}" + (f" · {t['where']}" if t["where"] else ""),
                            "priority": "high" if t["priority"] == "high" else "normal", "action": action})
            continue
        if not (t["mine"] or prev.get("mine")) or not s.get("alert_changes", True):
            continue
        if t["status"] != prev.get("status"):
            if t["status"] == "Resolved" and (t.get("resolved_by") or "").lower() in me:
                continue  # Joe closed it himself
            if t["status"] == "Waiting on reporter":
                continue  # not wanted (Joe, 2026-10-27); it coming back to Open still alerts
            back_on_you = t["status"] == "Open" and prev.get("status") in ("Waiting on reporter", "On hold", "Resolved")
            out.append({"toast": "ticket back on you" if back_on_you else "ticket status changed",
                        "title": f"{label}: {prev.get('status')} -> {t['status']}",
                        "body": "Back on you." if back_on_you else f"Reported by {t['requester']}",
                        "priority": "high" if back_on_you and t["priority"] == "high" else "normal", "action": action})
        elif t["priority"] != prev.get("priority"):
            out.append({"toast": "ticket priority changed",
                        "title": f"{label}: priority {prev.get('priority')} -> {t['priority']}",
                        "body": f"Type is now {t['type']}", "priority": "normal", "action": action})
    return out, new_state


def _alert_window(at, now: datetime, hours=4) -> bool:
    """True from `at` ("HH:MM", local) for `hours`, on weekdays only. "" or a bad value = off."""
    if now.weekday() >= 5:
        return False
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(at or ""))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return False
    start = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    return start <= now < start + timedelta(hours=hours)


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def daily_aging_alert(tickets: list[dict], s: dict | None = None, now: datetime | None = None) -> dict | None:
    """Raises at most one "waiting on you" alert per workday morning; returns it, or None.
    The day is only used up (db.seen) when there's something to say."""
    s = s or settings()
    now = now or datetime.now().astimezone()
    if not _alert_window(s.get("aging_alert_time", DEFAULTS["aging_alert_time"]), now):
        return None
    rep = aging_report(tickets, s)
    late = [t for t in rep["tickets"] if t["aging"] in ("warn", "overdue")]
    if not late or db.seen(f"pulse:aging:{now.date().isoformat()}"):
        return None
    shown = [f"#{t['ticket_no']} {t['title'][:80]}" for t in late[:3]]
    if len(late) > 3:
        shown.append(f"+{len(late) - 3} more")
    a = {"title": f"{_plural(len(late), 'ticket')} waiting on you (oldest {_plural(late[0]['age_days'], 'day')})",
         "body": " · ".join(shown),
         "priority": "high" if any(t["aging"] == "overdue" for t in late) else "normal",
         "action": {"type": "pulse", "url": ticket_url(None), "label": "Open Pulse"}}
    alerts.raise_alert("Pulse", a["title"], a["body"], a["priority"], a["action"],
                       toast=f"{_plural(len(late), 'ticket')} need you")
    return a


async def poll_once() -> dict:
    s = settings()
    tickets = await asyncio.to_thread(fetch, status="recent", limit=2000)
    found, state = diff_alerts(db.kv_get("pulse:state"), tickets, _me(s))
    for a in found[:10]:  # a burst (e.g. a bulk import) is capped; the tab shows everything
        alerts.raise_alert("Pulse", a["title"], a["body"], a["priority"], a["action"], toast=a.get("toast"))
    if len(found) > 10:
        alerts.raise_alert("Pulse", f"{len(found) - 10} more ticket updates", "Open the Pulse Feedback tab to see them.", "low",
                           {"type": "pulse", "url": ticket_url(None), "label": "Open Pulse"}, toast="more ticket updates")
    db.kv_set("pulse:state", state)
    try:
        daily_aging_alert(tickets, s)
    except Exception:
        log.exception("Pulse aging alert failed")
    summary = summarize(tickets)
    _set_status(ok=True, error=None, last_poll=db.now(), last_ok=db.now(), summary=summary)
    return summary


async def poll_loop():
    failures = 0
    last_error = None
    while True:
        s = settings()
        delay = max(1, int(s.get("poll_minutes") or 2)) * 60
        if is_configured(s):
            try:
                await poll_once()
                if last_error:
                    log.info("Pulse polling recovered")
                failures, last_error = 0, None
            except Exception as e:
                failures += 1
                msg = str(e)[:300] or type(e).__name__
                _set_status(ok=False, error=msg, last_poll=db.now())
                if msg != last_error:  # log each distinct error once, not every poll
                    log.warning("Pulse poll failed: %s", msg)
                    if failures == 1:  # one alert per outage, not one per poll
                        alerts.raise_alert("Pulse", "Can't read Pulse tickets", msg, "low", toast="can't read tickets")
                last_error = msg
                delay = min(delay * (2 ** min(failures, 3)), 15 * 60)  # back off up to 15 minutes
        await asyncio.sleep(delay)


background = [poll_loop]


# ---- routes --------------------------------------------------------------------

@router.get("/api/pulse/status")
async def get_status():
    s = settings()
    st = db.kv_get("pulse:status") or {}
    try:
        import psycopg  # noqa: F401
        driver = True
    except ImportError:
        driver = False
    return {
        "configured": is_configured(s), "enabled": bool(s.get("enabled", True)), "driver": driver,
        "host": s.get("host") or None, "port": s.get("port"), "database": s.get("database"),
        "user": s.get("user") or None, "ssl_root_cert": s.get("ssl_root_cert"),
        "url": ticket_url(None).rsplit("/feedback", 1)[0], "poll_minutes": s.get("poll_minutes"),
        "last_poll": st.get("last_poll"), "last_ok": st.get("last_ok"), "ok": st.get("ok"), "error": st.get("error"),
        "setup_sql": SETUP_SQL,
    }


@router.post("/api/pulse/setup")
async def setup(body: dict = Body(...)):
    global _privileges_checked
    try:
        new = validate_settings(body or {})
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    password = body.get("password")
    if password is not None and not isinstance(password, str):
        raise HTTPException(400, "Password must be text")
    password = (password or "").strip("\r\n")
    if not password:
        old = settings()
        if all(str(old.get(k)) == str(new[k]) for k in CONNECTION_KEYS) and _password(old):
            password = _password(old)  # same login, re-test only
        else:
            raise HTTPException(400, "Enter the password for this login")
    if len(password) > 512 or "\x00" in password:
        raise HTTPException(400, "That password isn't valid")
    s = {**settings(), **new}
    sql, params = build_query(status="all", limit=1)
    try:
        await asyncio.to_thread(_run, s, password, sql, params, True)
    except PermissionError as e:
        raise HTTPException(400, str(e)) from None
    except Exception as e:
        raise HTTPException(400, f"Couldn't connect: {_clean_error(e, password)}") from None
    old = settings()
    if _account(old) != _account(s) and _password(old):
        try:
            keyring.delete_password(KEYRING_SERVICE, _account(old))
        except keyring.errors.KeyringError:
            pass
    keyring.set_password(KEYRING_SERVICE, _account(s), password)
    db.kv_set("pulse:settings", new)
    db.kv_set("pulse:state", None)  # new baseline on the next poll, no flood of "new" alerts
    _privileges_checked = True
    _set_status(ok=None, error=None)
    task = asyncio.create_task(_poll_soon())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"ok": True, "configured": True}


_tasks: set = set()


async def _poll_soon():
    try:
        await poll_once()
    except Exception as e:
        _set_status(ok=False, error=str(e)[:300], last_poll=db.now())


@router.post("/api/pulse/disconnect")
async def disconnect():
    s = settings()
    try:
        keyring.delete_password(KEYRING_SERVICE, _account(s))
    except keyring.errors.KeyringError:
        pass
    db.kv_set("pulse:state", None)
    db.kv_set("pulse:status", None)
    return {"ok": True, "configured": False}


@router.get("/api/pulse/tickets")
async def tickets(mine: bool = False, status: str = "open", search: str = "", limit: int = 100):
    if status not in ("open", "all"):
        raise HTTPException(400, "status must be open or all")
    if not 1 <= limit <= 500:
        raise HTTPException(400, "limit must be between 1 and 500")
    if len(search) > 100:
        raise HTTPException(400, "Search is limited to 100 characters")
    if not is_configured():
        return {"configured": False, "tickets": []}
    try:
        rows = await asyncio.to_thread(fetch, status=status, mine=mine, search=search, limit=limit)
    except Exception as e:
        raise HTTPException(502, str(e)[:300]) from None
    return {"configured": True, "tickets": rows}


@router.get("/api/pulse/aging")
async def aging():
    """Tickets waiting on Joe: counts per bucket (fresh / warn / overdue / unknown), oldest first."""
    if not is_configured():
        return {"configured": False}
    try:
        rows = await asyncio.to_thread(fetch, status="open", mine=True, limit=2000)
    except Exception as e:
        raise HTTPException(502, str(e)[:300]) from None
    return {"configured": True, **aging_report(rows), "as_of": db.now()}


@router.get("/api/pulse/summary")
async def summary():
    if not is_configured():
        return {"configured": False}
    st = db.kv_get("pulse:status") or {}
    if st.get("summary") and st.get("ok"):
        return {"configured": True, **st["summary"], "as_of": st.get("last_ok")}
    try:
        data = summarize(await asyncio.to_thread(fetch, status="recent", limit=2000))
    except Exception as e:
        return {"configured": True, "error": str(e)[:300]}
    return {"configured": True, **data, "as_of": db.now()}
