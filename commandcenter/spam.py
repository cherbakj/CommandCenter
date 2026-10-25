"""Mark as spam / block: future mail like this goes to Outlook's Junk Email folder, and Command Center hides it.

Joe picks what to block each time: the exact sender address, the sender's whole domain, or a subject phrase.
- Local list (kv `spam:blocks`, DPAPI-sealed like other work data): {id, kind: sender|domain|subject, value
  (normalized, lowercase), created}. It is the source of truth; kept until unblocked.
- Outlook rules: two receive rules on the default store, rebuilt from the local list on every change:
    "Command Center spam blocks — senders":  Conditions.SenderAddress.Address = the sender addresses plus "@domain"
                                              entries (any sender whose address contains the text).
    "Command Center spam blocks — subjects": Conditions.Subject.Text = the phrases (the subject contains one).
  Two rules because the conditions in one rule are AND-ed. Action: move to Junk Email. Both have an exception for
  internal domains and urgent people's addresses. Outlook's sender-address condition is a "contains" match, so
  "@promo-blast.com" covers anything@promo-blast.com but not anything@mail.promo-blast.com, and (rarely) also
  matches an address such as x@promo-blast.company.com; an exact address entry also matches a longer address that
  contains it (bob@x.com in jimbob@x.com). The app filter below is exact (address equal / domain equal).
  When a list becomes empty its rule is deleted (only a rule with our exact name; Outlook can't keep an empty
  condition list, and a disabled rule would still carry the unblocked entries). No other rule is ever changed.
  Rules.Save() can be slow and can fail (rule storage full, offline...): the block is still kept and enforced by
  the app filter, and the reason is shown with a Retry.
- App filter (matcher()): the mail list, follow-ups, important senders, new-mail alerts, triage and briefing counts skip
  blocked mail; the poller moves new blocked Inbox mail to Junk (outlook.scan_new_mail). Internal senders, Exchange
  (directory) senders and urgent people are never matched.
- Deliberate, two-step: POST /preview validates and counts the Inbox mail that already matches and returns a
  single-use preview_id (5 min) bound to the kind/value and the exact EntryIDs; POST /block uses it. Existing mail
  moves only if Joe ticks the box, and only the previewed ids that still match. /undo (30 s) removes the block and
  moves what this action moved back to the Inbox (new EntryIDs kept in memory only).
- Logging: counts and kinds only, never addresses, domains, subjects or phrases.
"""
import asyncio
import logging
import re
import secrets
import time
from datetime import datetime

import pywintypes
from fastapi import APIRouter, Body, HTTPException

from . import config, db, people
from .connectors import outlook

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/spam")

KV_BLOCKS = "spam:blocks"
KV_RULE = "spam:rule_status"
RULE_SENDERS = "Command Center spam blocks — senders"
RULE_SUBJECTS = "Command Center spam blocks — subjects"
OUR_RULES = (RULE_SENDERS, RULE_SUBJECTS)
OL_FOLDER_JUNK = 23
OL_RULE_RECEIVE = 0
KINDS = ("sender", "domain", "subject")
PREVIEW_SECONDS = 300
UNDO_SECONDS = 30
MAX_BLOCKS = 500       # keeps the two rules well inside Exchange's rule storage
MAX_MOVE = 500         # existing mail moved by one action
MOVE_CHUNK = 50        # moves per Outlook-thread call, so other Outlook work can run in between
SAMPLE = 5
PHRASE_MIN, PHRASE_MAX = 6, 120

DEFAULT_SHARED = ["gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "yahoo.com",
                  "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com"]
DEFAULT_STOPLIST = ["re", "fw", "fwd", "meeting", "invoice", "order", "update", "hello", "hi", "thanks", "thank",
                    "you", "your", "the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "at", "from", "with",
                    "re:", "fw:", "fwd:", "request", "question", "reminder", "important", "urgent", "info", "new",
                    "message", "follow", "up", "call", "today", "tomorrow", "week", "please", "help", "quote", "po"]
EMAIL = re.compile(r"[^@\s<>\"'(),;:]+@([a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+)")
DOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+")
REPLY_PREFIX = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|wg)\s*(?:\[\d+\])?\s*:\s*)+", re.I)


class Refused(ValueError):
    """A block that isn't allowed; the message says why in plain words."""


# ---- settings -----------------------------------------------------------------------------------

def _str_list(value, default) -> list[str]:
    if not isinstance(value, list):
        return list(default)
    return [str(v).strip().lower().lstrip("@") for v in value if str(v).strip()]


def settings() -> dict:
    """spam: in config.yaml (shared_providers, subject_stoplist); a missing or bad value means the default."""
    s = config.section("spam", {}) or {}
    s = s if isinstance(s, dict) else {}
    return {"shared_providers": _str_list(s.get("shared_providers"), DEFAULT_SHARED),
            "subject_stoplist": _str_list(s.get("subject_stoplist"), DEFAULT_STOPLIST)}


# ---- normalizing and guards (no Outlook needed) -------------------------------------------------

def normalize(kind: str, value: str) -> str:
    """Lowercase, trimmed form of a block value; raises Refused when it isn't one."""
    if kind not in KINDS:
        raise Refused("Pick sender, domain or subject phrase")
    v = str(value or "")
    if any(ord(c) < 32 for c in v):
        raise Refused("That has control characters in it")
    v = re.sub(r"\s+", " ", v).strip()
    if kind == "sender":
        v = v.lower().removeprefix("mailto:").strip("<> ")
        if not EMAIL.fullmatch(v) or len(v) > 254:
            raise Refused("That isn't an email address")
        return v
    if kind == "domain":
        v = v.lower().strip().lstrip("@").rstrip(".")
        if not DOMAIN.fullmatch(v) or len(v) > 253:
            raise Refused("That isn't a domain name")
        return v
    v = REPLY_PREFIX.sub("", v).strip().lower()
    return v


def _in_domain(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def refusal(kind: str, value: str, *, sender: str = "", exchange: bool = False, name: str = "",
            me: tuple = (), urgent: list | None = None, internal: list | None = None,
            cfg: dict | None = None, important: list | None = None) -> str | None:
    """Why this block isn't allowed (plain words), or None. `value` is normalized. `sender`/`exchange`/`name`
    describe the email it was started from (that email's sender must itself be blockable)."""
    cfg = cfg or settings()
    urgent = people.urgent_people() if urgent is None else urgent
    internal = people.internal_domains() if internal is None else internal
    important = people.important_senders() if important is None else important
    me = {m.lower() for m in me if m}

    def who_refused(addr, display=""):
        host = addr.rpartition("@")[2]
        if exchange or "@" not in addr:
            return "This sender is in your organization's directory (Exchange), so it can't be marked as spam."
        if addr in me:
            return "That's your own address."
        if any(_in_domain(host, d) for d in internal):
            return f"{addr} is an internal ({host}) address. Internal mail can't be marked as spam."
        if people.matches(addr, display, urgent):
            return "This sender is on your urgent people list. Remove them there first if you really mean it."
        if people.sender_matches(addr, important):
            return "This sender is on your important senders list. Remove them there first if you really mean it."
        return None

    if sender or exchange:
        reason = who_refused(sender, name)
        if reason:
            return reason
    if kind == "sender":
        return who_refused(value)
    if kind == "domain":
        if any(_in_domain(value, d) for d in internal):
            return f"{value} is an internal domain. Internal mail can't be marked as spam."
        if any(_in_domain(value, p) for p in cfg["shared_providers"]):
            return (f"{value} is a shared mail provider used by millions of people, so the whole domain can't be "
                    "blocked. Block this sender instead.")
        if any(m.rpartition("@")[2] == value for m in me):
            return "That's the domain of your own address."
        for p in urgent:
            p = str(p).lower()
            if "@" in p and (p.lstrip("@") == value or p.rpartition("@")[2] == value):
                return "Someone on your urgent people list uses this domain. Block this sender instead."
        if any(str(p).lower().lstrip("@") == value or str(p).lower().rpartition("@")[2] == value for p in important):
            return "This domain (or someone at it) is on your important senders list. Block this sender instead."
        return None
    # subject phrase
    if len(value) < PHRASE_MIN:
        return f"Use a longer phrase (at least {PHRASE_MIN} characters), so it only catches this kind of mail."
    if len(value) > PHRASE_MAX:
        return f"Use a shorter phrase (up to {PHRASE_MAX} characters)."
    words = [w for w in re.split(r"[^\w:]+", value) if w]
    if not re.search(r"\w", value) or all(w in cfg["subject_stoplist"] or w.isdigit() for w in words):
        return "That phrase is too generic (it would catch ordinary mail). Add a more specific word."
    return None


# ---- the app filter -----------------------------------------------------------------------------

class Matcher:
    """Precompiled blocks. hit(sender, subject, name) -> the matching block, or None. `subject` may be a
    callable (read only when a phrase needs it). Never matches internal, Exchange or urgent senders."""

    def __init__(self, blocks, internal, urgent):
        self.senders = {b["value"]: b for b in blocks if b.get("kind") == "sender"}
        self.domains = {b["value"]: b for b in blocks if b.get("kind") == "domain"}
        phrases = [b for b in blocks if b.get("kind") == "subject" and b.get("value")]
        self.phrases = {b["value"]: b for b in phrases}
        self.phrase_re = re.compile("|".join(re.escape(v) for v in sorted(self.phrases, key=len, reverse=True))) \
            if phrases else None
        self.internal = tuple(internal)
        self.urgent = list(urgent)
        self.empty = not (self.senders or self.domains or self.phrases)

    def __bool__(self):
        return not self.empty

    def hit(self, sender, subject="", name=""):
        if self.empty:
            return None
        addr = (sender or "").strip().lower()
        if "@" not in addr:  # unknown or an Exchange DN: internal
            return None
        host = addr.rpartition("@")[2]
        if any(_in_domain(host, d) for d in self.internal):
            return None
        block = self.senders.get(addr) or self.domains.get(host)
        if block is None and self.phrase_re is not None:
            text = (subject() if callable(subject) else subject) or ""
            m = self.phrase_re.search(text.lower())
            block = self.phrases.get(m.group(0)) if m else None
        if block is not None and self.urgent and people.matches(addr, name or "", self.urgent):
            return None
        return block


_cache: dict = {}


def blocks() -> list[dict]:
    value = db.kv_get(KV_BLOCKS, [])
    return value if isinstance(value, list) else []


def matcher() -> Matcher:
    """The current blocks as a Matcher (rebuilt only when the list, internal domains or urgent people change)."""
    bl, internal, urgent = blocks(), people.internal_domains(), people.urgent_people()
    key = (tuple((b.get("kind"), b.get("value")) for b in bl), tuple(internal), tuple(urgent))
    if _cache.get("key") != key:
        _cache["key"], _cache["m"] = key, Matcher(bl, internal, urgent)
    return _cache["m"]


def single(kind, value) -> Matcher:
    return Matcher([{"id": "", "kind": kind, "value": value}], people.internal_domains(), people.urgent_people())


def label(kind, value) -> str:
    return {"sender": f"sender {value}", "domain": f"anything @{value}", "subject": f"subjects containing “{value}”"}[kind]


# ---- Outlook (each runs on the Outlook thread through outlook.call) ---------------------------------

def _message_info(entry_id) -> dict:
    item = outlook._ns().GetItemFromID(entry_id)
    if item.Class != outlook.OL_MAIL:
        raise Refused("Only emails can be marked as spam")
    received = getattr(item, "ReceivedTime", None)
    return {"sender": (outlook._sender_smtp(item) or "").strip().lower(),
            "exchange": (item.SenderEmailType or "").upper() == "EX",
            "name": item.SenderName or "", "subject": item.Subject or "",
            "received": outlook._local_time(received).isoformat(timespec="minutes") if received else None}


def _my_addresses() -> tuple:
    ns, out = outlook._ns(), set()
    try:
        for acc in ns.Accounts:
            if acc.SmtpAddress:
                out.add(acc.SmtpAddress.lower())
    except pywintypes.com_error:
        pass
    try:
        user = ns.CurrentUser.AddressEntry.GetExchangeUser()
        if user and user.PrimarySmtpAddress:
            out.add(user.PrimarySmtpAddress.lower())
    except (pywintypes.com_error, AttributeError):
        pass
    return tuple(sorted(out))


def _dasl(kind, value) -> str:
    v = value.replace("'", "''")
    if kind == "subject":
        return f'@SQL="urn:schemas:httpmail:subject" LIKE \'%{v}%\''
    v = v if kind == "sender" else "@" + v
    return (f'@SQL=("{outlook.PR_SENDER_EMAIL}" LIKE \'%{v}%\' OR "urn:schemas:httpmail:fromemail" LIKE \'%{v}%\')')


def _inbox_matches(kind, value, exclude_id) -> dict:
    """Inbox mail that already matches one block: total count, the newest MAX_MOVE ids, and SAMPLE for display.
    A DASL filter narrows the rows (a superset); the exact check is the app matcher."""
    cols = ["EntryID", "Subject", "SenderName", "ReceivedTime", "MessageClass", outlook.PR_SENDER_SMTP,
            outlook.PR_SENDER_EMAIL]
    try:
        rows = outlook._table_rows(outlook.OL_FOLDER_INBOX, _dasl(kind, value), cols, 20000)
    except pywintypes.com_error:  # a store that refuses the filter: read the whole Inbox
        rows = outlook._table_rows(outlook.OL_FOLDER_INBOX, "", cols, 20000)
    m = single(kind, value)
    hits = []
    for entry_id, subject, name, received, msg_class, smtp, email in rows:
        if entry_id == exclude_id or not (msg_class or "").startswith("IPM.Note"):
            continue
        addr = next((a for a in (smtp, email) if isinstance(a, str) and "@" in a), "")
        if m.hit(addr, subject or "", name or ""):
            hits.append((outlook._local_time(received) if received else datetime.min, entry_id, addr, subject, name))
    hits.sort(key=lambda h: h[0], reverse=True)
    return {"count": len(hits), "ids": [h[1] for h in hits[:MAX_MOVE]],
            "sample": [{"sender": fromline(h[4], h[2]), "subject": h[3] or "(no subject)",
                        "received": h[0].isoformat(timespec="minutes") if h[0] != datetime.min else None}
                       for h in hits[:SAMPLE]]}


def fromline(name, addr) -> str:
    return f"{name} <{addr}>" if name and addr and addr not in name else (name or addr or "")


def _folder_id(item) -> str:
    try:
        return item.Parent.EntryID
    except pywintypes.com_error:
        return ""


def _move(ids, to_junk: bool, kind=None, value=None, trigger=False) -> tuple[list, int]:
    """Moves mail to Junk (to_junk) or back to the Inbox. Returns (new EntryIDs, skipped).
    To Junk: an existing message moves only if it's still in the Inbox and still matches the block; the triggering
    message moves from any folder except Junk (its sender was checked when the block was previewed). Back to the
    Inbox: only messages that are still in Junk."""
    ns = outlook._ns()
    junk, inbox = ns.GetDefaultFolder(OL_FOLDER_JUNK), ns.GetDefaultFolder(outlook.OL_FOLDER_INBOX)
    junk_id, inbox_id = junk.EntryID, inbox.EntryID
    m = single(kind, value) if to_junk and not trigger else None
    moved, skipped = [], 0
    for entry_id in ids:
        try:
            item = ns.GetItemFromID(entry_id)
            if item.Class != outlook.OL_MAIL:
                skipped += 1
                continue
            where = _folder_id(item)
            if to_junk:
                ok = where != junk_id and (trigger or (where == inbox_id and (item.SenderEmailType or "").upper() != "EX"
                                                       and m.hit(outlook._sender_smtp(item), item.Subject or "",
                                                                 item.SenderName or "")))
            else:
                ok = where == junk_id
            if not ok:
                skipped += 1
                continue
            moved.append(item.Move(junk if to_junk else inbox).EntryID)
        except pywintypes.com_error:
            skipped += 1
    return moved, skipped


def _com_message(e) -> str:
    """Outlook's own description of a COM error (plain words), without codes."""
    try:
        text = (e.excepinfo and e.excepinfo[2]) or e.strerror or ""
    except (AttributeError, IndexError, TypeError):
        text = ""
    return re.sub(r"\s+", " ", str(text)).strip()[:300] or "Outlook gave no reason"


def _rule_lists(bl) -> tuple[list, list]:
    senders = sorted({b["value"] for b in bl if b["kind"] == "sender"} | {"@" + b["value"] for b in bl if b["kind"] == "domain"})
    phrases = sorted({b["value"] for b in bl if b["kind"] == "subject"})
    return senders, phrases


def _exceptions() -> list[str]:
    """Never moved by our rules: internal domains and urgent people's addresses."""
    urgent = [str(p).lower() for p in people.urgent_people() if "@" in str(p)]
    return sorted({"@" + d for d in people.internal_domains()} | set(urgent))


def _find(rules) -> dict:
    found, dupes = {}, 0
    for i in range(1, rules.Count + 1):
        name = rules.Item(i).Name
        if name in OUR_RULES:
            if name in found:
                dupes += 1
            else:
                found[name] = i
    return {"index": found, "dupes": dupes}


def _others_enabled(rules) -> dict:
    """{(name, n-th rule with that name): enabled} for every rule that isn't ours (read only)."""
    out, seen = {}, {}
    for i in range(1, rules.Count + 1):
        r = rules.Item(i)
        if r.Name not in OUR_RULES:
            seen[r.Name] = seen.get(r.Name, -1) + 1
            out[(r.Name, seen[r.Name])] = bool(r.Enabled)
    return out


def _vals(condition, attr) -> tuple:
    try:
        return tuple(str(v) for v in (getattr(condition, attr) or ()))
    except (pywintypes.com_error, TypeError):
        return ()


def _set_rule(rules, index, name, attr, values, exceptions, junk) -> tuple:
    """Creates or updates one of our rules (index None = create). Returns (rule, changed)."""
    changed = index is None
    rule = rules.Create(name, OL_RULE_RECEIVE) if index is None else rules.Item(index)
    if rule.RuleType != OL_RULE_RECEIVE:
        raise Refused(f'A rule named "{name}" exists but isn\'t a receive rule. Rename or delete it in Outlook, then Retry.')
    cond = rule.Conditions.SenderAddress if attr == "Address" else rule.Conditions.Subject
    if _vals(cond, attr) != tuple(values) or not cond.Enabled:
        setattr(cond, attr, tuple(values))
        cond.Enabled = True
        changed = True
    exc = rule.Exceptions.SenderAddress
    if exceptions and (_vals(exc, "Address") != tuple(exceptions) or not exc.Enabled):
        exc.Address = tuple(exceptions)
        exc.Enabled = True
        changed = True
    move = rule.Actions.MoveToFolder
    try:
        same_folder = move.Enabled and move.Folder is not None and move.Folder.EntryID == junk.EntryID
    except pywintypes.com_error:
        same_folder = False
    if not same_folder:
        move.Folder = junk
        move.Enabled = True
        changed = True
    if not rule.Enabled:
        rule.Enabled = True
        changed = True
    return rule, changed


def _sync_rules(senders, phrases, exceptions, force=False) -> dict:
    """Makes our two Outlook rules match the lists. Saves only when something changed (or `force`: the last save
    failed, and Outlook may still hold those unsaved edits). Never edits another rule; if Outlook switches other
    rules off while saving (rule storage full), that is reported, not undone."""
    ns = outlook._ns()
    junk = ns.GetDefaultFolder(OL_FOLDER_JUNK)
    rules = ns.DefaultStore.GetRules()
    found = _find(rules)
    changed = force
    wanted = {RULE_SENDERS: ("Address", senders), RULE_SUBJECTS: ("Text", phrases)}
    for name in OUR_RULES:  # deletions first (indexes shift), each found again by name right before
        if not wanted[name][1] and name in found["index"]:
            rules.Remove(_find(rules)["index"][name])
            changed = True
    found = _find(rules)
    made = {}
    for name, (attr, values) in wanted.items():
        if values:
            made[name], did = _set_rule(rules, found["index"].get(name), name, attr, values, exceptions, junk)
            changed = changed or did
    status = {"ok": True, "error": None, "senders": len(senders), "subjects": len(phrases), "local_only": False,
              "others_disabled": 0, "duplicates": found["dupes"]}
    if changed:
        before = _others_enabled(rules)
        try:
            rules.Save(False)
        except pywintypes.com_error as e:
            status.update(ok=False, error=f"Outlook couldn't save the rule: {_com_message(e)}")
            log.warning("Spam rule save failed (COM error 0x%08x)", e.hresult & 0xFFFFFFFF)
            try:
                after = _others_enabled(ns.DefaultStore.GetRules())
                status["others_disabled"] = sum(1 for k, on in before.items() if on and after.get(k) is False)
            except pywintypes.com_error:
                pass
            if status["others_disabled"]:
                status["error"] += (f" Outlook also switched off {status['others_disabled']} of your other rule(s) while "
                                    "saving; check them in Outlook > Rules.")
            return status
    try:
        status["local_only"] = any(bool(r.IsLocalRule) for r in made.values())
    except pywintypes.com_error:
        pass
    return status


def _rule_in_sync(senders, phrases) -> bool:
    """Read-only: do our rules hold exactly these lists (and exist only when needed)?"""
    rules = outlook._ns().DefaultStore.GetRules()
    found = _find(rules)["index"]
    for name, attr, values in ((RULE_SENDERS, "Address", senders), (RULE_SUBJECTS, "Text", phrases)):
        if not values:
            if name in found:
                return False
            continue
        if name not in found:
            return False
        rule = rules.Item(found[name])
        cond = rule.Conditions.SenderAddress if attr == "Address" else rule.Conditions.Subject
        if not rule.Enabled or not cond.Enabled or _vals(cond, attr) != tuple(values):
            return False
    return True


# ---- state ----------------------------------------------------------------------------------------

_previews: dict[str, dict] = {}
_undo: dict[str, dict] = {}
_lock = asyncio.Lock()


def _prune(store, now):
    for k in [k for k, v in store.items() if v["expires"] < now]:
        store.pop(k)


def rule_status() -> dict:
    return db.kv_get(KV_RULE) or {"ok": None, "error": None}


async def sync() -> dict:
    """Rebuilds our Outlook rules from the local list and stores the outcome (plain-words error if it failed)."""
    senders, phrases = _rule_lists(blocks())
    try:
        status = await outlook.call(_sync_rules, senders, phrases, _exceptions(), rule_status().get("ok") is False)
    except outlook.OutlookClosed:
        status = {"ok": False, "error": "Outlook is closed, so the Outlook rule wasn't saved. Command Center still "
                                        "hides this mail; press Retry once Outlook is open."}
    except Refused as e:
        status = {"ok": False, "error": str(e)}
    except pywintypes.com_error as e:
        status = {"ok": False, "error": f"Outlook couldn't update the rule: {_com_message(e)}"}
        log.warning("Spam rule update failed (COM error 0x%08x)", e.hresult & 0xFFFFFFFF)
    status.update(checked=db.now(), senders=len(senders), subjects=len(phrases), in_sync=bool(status.get("ok")))
    db.kv_set(KV_RULE, status)
    log.info("Spam rules synced: %d sender/domain entries, %d phrases, %s", len(senders), len(phrases),
             "saved" if status["ok"] else "not saved")
    return status


def _clear_caches():
    try:
        from . import today
        today._followup_cache.clear()  # follow-ups hide the new block right away
    except ImportError:
        pass


def _sender_refusal(info, me) -> str | None:
    """The email a block starts from must have a blockable sender (not internal, Exchange, you or urgent)."""
    if not info["sender"] and not info["exchange"]:
        return "This email has no sender address, so it can't be marked as spam here."
    return refusal("sender", info["sender"], sender=info["sender"], exchange=info["exchange"], name=info["name"], me=me)


def _outlook_error(e) -> HTTPException:
    if isinstance(e, outlook.OutlookClosed):
        return HTTPException(503, str(e))
    if isinstance(e, Refused):
        return HTTPException(400, str(e))
    return HTTPException(404, "That email couldn't be opened in Outlook (it may have been moved or deleted).")


# ---- routes -----------------------------------------------------------------------------------------

@router.get("/blocks")
async def list_blocks():
    bl = sorted(blocks(), key=lambda b: b.get("created", ""), reverse=True)
    return {"blocks": [{**b, "label": label(b["kind"], b["value"])} for b in bl], "rule": rule_status(),
            "internal_domains": people.internal_domains(), "rule_names": list(OUR_RULES)}


@router.get("/options")
async def options(id: str):
    """What the dialog offers for this email: its sender, domain and subject, and which kinds are allowed."""
    try:
        info = await outlook.call(_message_info, id)
        me = await outlook.call(_my_addresses)
    except (outlook.OutlookClosed, Refused, pywintypes.com_error) as e:
        raise _outlook_error(e)
    who = _sender_refusal(info, me)
    if who:
        raise HTTPException(400, who)
    host = info["sender"].rpartition("@")[2]
    domain_no = refusal("domain", host, me=me)
    phrase = REPLY_PREFIX.sub("", re.sub(r"\s+", " ", info["subject"])).strip()[:PHRASE_MAX]
    return {"sender": info["sender"], "sender_name": info["name"], "subject": info["subject"], "domain": host,
            "phrase": phrase,
            "kinds": {"sender": {"ok": True}, "domain": {"ok": not domain_no, "reason": domain_no},
                      "subject": {"ok": True}}}


@router.post("/preview")
async def preview(id: str = Body(...), kind: str = Body(...), value: str = Body("")):
    try:
        info = await outlook.call(_message_info, id)
        me = await outlook.call(_my_addresses)
    except (outlook.OutlookClosed, Refused, pywintypes.com_error) as e:
        raise _outlook_error(e)
    reason = _sender_refusal(info, me)
    if reason:
        raise HTTPException(400, reason)
    try:
        # A sender or domain block always comes from the email itself; a value sent along must agree with it.
        derived = {"sender": info["sender"], "domain": info["sender"].rpartition("@")[2]}.get(kind)
        v = normalize(kind, value if derived is None else derived)
        if derived is not None and value.strip() and normalize(kind, value) != v:
            raise Refused("That doesn't match this email's sender")
    except Refused as e:
        raise HTTPException(400, str(e))
    reason = refusal(kind, v, sender=info["sender"], exchange=info["exchange"], name=info["name"], me=me)
    if reason:
        raise HTTPException(400, reason)
    if len(blocks()) >= MAX_BLOCKS and not any(b["kind"] == kind and b["value"] == v for b in blocks()):
        raise HTTPException(400, f"You already have {MAX_BLOCKS} blocks. Unblock some under Spam blocks first.")
    found = await outlook.call(_inbox_matches, kind, v, id)
    now = time.time()
    _prune(_previews, now)
    while len(_previews) >= 50:
        _previews.pop(next(iter(_previews)))
    preview_id = secrets.token_urlsafe(18)
    _previews[preview_id] = {"id": id, "kind": kind, "value": v, "ids": tuple(found["ids"]), "sender": info["sender"],
                             "exchange": info["exchange"], "name": info["name"], "me": me,
                             "expires": now + PREVIEW_SECONDS}
    log.info("Spam preview: kind=%s, %d matching", kind, found["count"])
    return {"preview_id": preview_id, "kind": kind, "value": v, "label": label(kind, v), "match_count": found["count"],
            "movable": len(found["ids"]), "sample": found["sample"], "expires_in": PREVIEW_SECONDS,
            "exists": any(b["kind"] == kind and b["value"] == v for b in blocks())}


@router.post("/block")
async def block(preview_id: str = Body(...), move_existing: bool = Body(False)):
    p = _previews.pop(preview_id or "", None)  # single use, even if it then fails
    if not p or p["expires"] < time.time():
        raise HTTPException(409, "The preview expired or was already used. Check the matches again.")
    reason = refusal(p["kind"], p["value"], sender=p["sender"], exchange=p["exchange"], name=p["name"], me=p["me"])
    if reason:  # internal domains / urgent people may have changed since the preview
        raise HTTPException(400, reason)
    async with _lock:
        bl = blocks()
        existing = next((b for b in bl if b["kind"] == p["kind"] and b["value"] == p["value"]), None)
        created = existing is None
        if created:
            if len(bl) >= MAX_BLOCKS:
                raise HTTPException(400, f"You already have {MAX_BLOCKS} blocks. Unblock some under Spam blocks first.")
            existing = {"id": secrets.token_hex(6), "kind": p["kind"], "value": p["value"], "created": db.now()}
            db.kv_set(KV_BLOCKS, bl + [existing])
        _clear_caches()
        moved, skipped, trigger_moved, move_error = [], 0, False, None
        try:
            new, _ = await outlook.call(_move, [p["id"]], True, p["kind"], p["value"], True)
            trigger_moved, moved = bool(new), list(new)
            if move_existing:
                for i in range(0, len(p["ids"]), MOVE_CHUNK):
                    new, skip = await outlook.call(_move, list(p["ids"][i:i + MOVE_CHUNK]), True, p["kind"], p["value"])
                    moved += new
                    skipped += skip
        except outlook.OutlookClosed as e:
            move_error = str(e)
        except pywintypes.com_error as e:
            move_error = f"Outlook couldn't move the mail: {_com_message(e)}"
        rule = await sync()
    now = time.time()
    _prune(_undo, now)
    _undo[existing["id"]] = {"expires": now + UNDO_SECONDS, "created": created, "moved": moved}
    log.info("Spam block %s: kind=%s, trigger %s, moved %d existing, skipped %d, rule %s",
             "added" if created else "re-used", p["kind"], "moved" if trigger_moved else "not moved",
             len(moved) - int(trigger_moved), skipped, "saved" if rule.get("ok") else "not saved")
    return {"ok": True, "block": {**existing, "label": label(existing["kind"], existing["value"])}, "created": created,
            "trigger_moved": trigger_moved, "moved": len(moved) - int(trigger_moved), "skipped": skipped,
            "move_error": move_error, "rule": rule, "undo_seconds": UNDO_SECONDS}


@router.post("/unblock")
async def unblock(id: str = Body(..., embed=True)):
    async with _lock:
        bl = blocks()
        keep = [b for b in bl if b.get("id") != id]
        if len(keep) == len(bl):
            raise HTTPException(404, "That block is already gone")
        db.kv_set(KV_BLOCKS, keep)
        _undo.pop(id, None)
        _clear_caches()
        rule = await sync()
    log.info("Spam block removed (%d left)", len(keep))
    return {"ok": True, "rule": rule}


@router.post("/undo")
async def undo(block_id: str = Body(..., embed=True)):
    u = _undo.pop(block_id or "", None)
    if not u or u["expires"] < time.time():
        raise HTTPException(409, "It's too late to undo. Unblock it under Spam blocks (mail stays in Junk Email).")
    async with _lock:
        rule = None
        if u["created"]:
            db.kv_set(KV_BLOCKS, [b for b in blocks() if b.get("id") != block_id])
            _clear_caches()
            rule = await sync()
        back, skipped, error = [], 0, None
        try:
            for i in range(0, len(u["moved"]), MOVE_CHUNK):
                new, skip = await outlook.call(_move, u["moved"][i:i + MOVE_CHUNK], False)
                back += new
                skipped += skip
        except outlook.OutlookClosed as e:
            error = str(e)
        except pywintypes.com_error as e:
            error = f"Outlook couldn't move the mail back: {_com_message(e)}"
    log.info("Spam block undone: %d moved back, %d skipped", len(back), skipped)
    return {"ok": True, "unblocked": u["created"], "moved_back": len(back), "skipped": skipped, "error": error,
            "rule": rule}


@router.post("/sync")
async def retry_sync():
    async with _lock:
        return {"rule": await sync()}


async def check_loop():
    """A read-only look at our Outlook rules shortly after start and every 6 hours: marks the status out of sync
    if someone changed or deleted them in Outlook (Retry in Spam blocks rebuilds them). Never saves."""
    await asyncio.sleep(90)
    while True:
        try:
            senders, phrases = _rule_lists(blocks())
            st = rule_status()
            if senders or phrases or st.get("ok") is not None:
                ok = await outlook.call(_rule_in_sync, senders, phrases)
                if ok and not st.get("ok"):  # fixed in Outlook, or a Retry by hand
                    st.update(ok=True, error=None, in_sync=True, checked=db.now())
                    db.kv_set(KV_RULE, st)
                elif not ok and st.get("ok") is not False:  # keep an earlier save error's own words
                    st.update(ok=False, in_sync=False, checked=db.now(), error="The Outlook rules don't match your "
                              "blocks (changed or deleted in Outlook). Press Retry to rebuild them.")
                    db.kv_set(KV_RULE, st)
        except outlook.OutlookClosed:
            pass
        except Exception as e:
            log.warning("Spam rule check failed: %s", type(e).__name__)
        await asyncio.sleep(6 * 3600)


background = [check_loop]
