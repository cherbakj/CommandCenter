"""Teams connector.

Reading: a background browser (saved profile "teams", logged in once by you) opens Teams on the web and reads,
without clicking or typing anything. The browser stays open between checks, like a Teams tab left open, and each
check re-reads the open page; it starts afresh every `keep_open_minutes` (60) and after a failed read
(0 = close it after every read, which cold-starts Teams each time: several cores for ~20 s). It reads:
  * the unread count from the page title, e.g. "(3) Chat | Microsoft Teams";
  * which chats are unread and which carry an @mention of you, from the chat list (role=treeitem rows with
    data-item-type="chat"/"channel"; the conversation id in data-fui-tree-item-value; Teams' own screen-reader
    texts in aria-labelledby: "chat_list_unread_text", "channel-list-unread-text", "...personal...mention...");
  * sender, preview, time and per-chat unread count for those chats, and your @mentions (Activity feed), from
    Teams' local cache in the same page (IndexedDB conversation-manager / replychain-manager / activity-manager),
    opened read-only.
A part that can't be read is reported as unavailable; the title count always stays.
The page is told it's hidden and unfocused (so Teams doesn't mark the chat it opens on as read), and every write
to a chat (read markers, messages) is blocked at the network level.
Names and previews are work data: they stay in memory (`_details`). The database keeps counts (kv teams:state)
and the ids already alerted on (kv teams:alerted, DPAPI-sealed); logs get counts only.
Details also come from Teams' missed-activity emails in Outlook.
Replying: Teams deep links (msteams:/l/chat/...) open a chat in the Teams app, with your message filled in where
given; you press send.
"""
import asyncio
import logging
import re
import time
import urllib.parse
from datetime import datetime

from playwright.async_api import async_playwright

from .. import alerts, config, db, people
from . import web

log = logging.getLogger(__name__)
PROFILE = "teams"
TITLE_COUNT = re.compile(r"^\((\d+)\)")
RECENT_DAYS = 3
RECENT_CAP = 20
PREVIEW_LEN = 140
ALERTS_PER_CHECK = 5
_THREAD_ID = re.compile(r"19:[A-Za-z0-9_.:@-]{1,200}")
_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# Writes to chats (read markers, messages, "mark all read"): never sent from the background browser.
_WRITES = re.compile(r"/users/ME/conversations/|consumptionhorizon|mark(all)?(as)?read", re.I)
_details: dict = {}  # the last read's Recent list (names, previews): memory only

# Runs before Teams' own scripts: the page looks hidden and unfocused, as a background tab would.
_HIDDEN_JS = """
Object.defineProperty(Document.prototype, 'visibilityState', { get: () => 'hidden', configurable: true });
Object.defineProperty(Document.prototype, 'hidden', { get: () => true, configurable: true });
Document.prototype.hasFocus = () => false;
"""

# Reads the chat list and Teams' local cache. Returns ids, kinds, flags and short texts; changes nothing.
_READ_JS = r"""
async ({ days, previewLen }) => {
  const out = { dom: false, convs: false, feed: false, tenant: null, chats: [], mentions: [] };
  const clean = (s, n = previewLen) => String(s ?? '').replace(/\s+/g, ' ').trim().slice(0, n);
  const textOf = (id) => { const n = id && document.getElementById(id); return n ? clean(n.textContent, 120) : ''; };
  const cutoff = Date.now() - days * 864e5;

  // 1) The chat list: unread / @mention flags and names.
  const rows = {}, titles = {};
  try {
    for (const n of document.querySelectorAll('[role="treeitem"][data-item-type="chat"], [role="treeitem"][data-item-type="channel"]')) {
      const m = /OneGQL_(\w+?)Conversation\|([^/|]+)$/.exec(n.getAttribute('data-fui-tree-item-value') || '');
      if (!m) continue;
      const refs = `${n.getAttribute('aria-labelledby') || ''} ${n.getAttribute('aria-describedby') || ''}`.split(/\s+/).filter(Boolean);
      const id = m[2], channel = n.getAttribute('data-item-type') === 'channel';
      const title = textOf(refs.find((r) => /^title-(chat-list-item_|channel-list-item-)/.test(r)));
      const team = channel ? textOf(refs.find((r) => r.startsWith('title-team-list-item-'))) : '';
      if (title) titles[id] = team ? `${team} › ${title}` : title;
      const row = rows[id] || (rows[id] = { id, type: m[1], channel, unread: false, mention: false });
      row.unread ||= refs.includes('chat_list_unread_text') || refs.includes('channel-list-unread-text');
      row.mention ||= refs.some((r) => /personal[-_]?(at[-_]?)?mention/i.test(r));
    }
    out.dom = Object.keys(rows).length > 0;
  } catch (e) { /* chat list not readable: counts fall back to the title */ }

  // 2) Teams' cache (IndexedDB), opened read-only; only databases that already exist.
  let names = [];
  try { names = (await indexedDB.databases()).map((d) => d.name).filter(Boolean); } catch (e) {}
  const named = (re) => names.find((n) => re.test(n));
  const open = (name) => new Promise((res, rej) => {
    if (!name) { rej(new Error('missing')); return; }
    const r = indexedDB.open(name);
    r.onsuccess = () => res(r.result);
    r.onerror = () => rej(r.error);
    r.onblocked = () => rej(new Error('blocked'));
    r.onupgradeneeded = () => { try { r.transaction.abort(); } catch (e) {} };  // never create or change one
  });
  const done = (r) => new Promise((res, rej) => { r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); });
  // HTML message bodies to text with plain string work (Teams' Trusted Types policy blocks DOMParser here).
  const ENT = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' };
  const plain = (html) => String(html || '').replace(/<(style|script)\b[\s\S]*?<\/(style|script)>/gi, ' ').replace(/<[^>]*>/g, ' ')
    .replace(/&#(\d{1,6});/g, (all, n) => String.fromCodePoint(Math.min(+n, 0x10ffff)))
    .replace(/&(amp|lt|gt|quot|apos|nbsp);/gi, (all, e) => ENT[e.toLowerCase()]);
  const nameOf = (v) => clean(typeof v === 'string' ? v : v && typeof v.displayName === 'string' ? v.displayName : '', 120);
  const previewOf = (text, type) => {
    const p = clean(text);
    if (p) return p;
    type = String(type || '');
    return /CallRecording/i.test(type) ? '(call recording)' : /Card/i.test(type) ? '(card)' : /Call/i.test(type) ? '(call)'
      : /Media_/i.test(type) ? '(attachment)' : '';
  };
  const dbs = [];
  const convs = {};
  try {
    const cdb = await open(named(/^Teams:conversation-manager:/)); dbs.push(cdb);
    for (const c of await done(cdb.transaction('conversations', 'readonly').objectStore('conversations').getAll())) convs[c.id] = c;
    out.convs = true;
  } catch (e) {}
  let rdb = null, rstore = null;
  try {
    rdb = await open(named(/^Teams:replychain-manager:/)); dbs.push(rdb);
    rstore = [...rdb.objectStoreNames].filter((s) => /^replychains/.test(s))
      .filter((s) => rdb.transaction(s, 'readonly').objectStore(s).indexNames.contains('latestDeliveryTimeByConversationId')).pop() || null;
  } catch (e) {}
  const horizonOf = (c) => Number(String(c?.properties?.consumptionhorizon || '').split(';')[0]) || 0;
  const unreadCount = async (id, since) => {
    const idx = rdb.transaction(rstore, 'readonly').objectStore(rstore).index('latestDeliveryTimeByConversationId');
    let n = 0;
    for (const ch of await done(idx.getAll(IDBKeyRange.bound([id, since], [id, Infinity]))))
      for (const m of Object.values(ch.messageMap || {}))
        if (m.originalArrivalTime > since && !m.isSentByCurrentUser && /^(RichText|Text)/i.test(m.messageType || '')) n++;
    return n;
  };
  for (const c of Object.values(convs)) { const t = c?.threadProperties?.tenantid; if (t) { out.tenant = t; break; } }

  // Chats: every unread one in the list, plus (from the cache) chats whose last message is someone else's, recently.
  const ids = new Set(Object.values(rows).filter((r) => r.unread && !r.channel).map((r) => r.id));
  for (const c of Object.values(convs)) {
    const lm = c.lastMessage, tp = c.threadProperties || {};
    if (c.type !== 'Chat' || !lm || tp.isLastMessageFromMe) continue;
    if (/^(ThreadActivity|Event)/i.test(lm.messagetype || '')) continue;  // joins, renames, calls
    if ((Date.parse(lm.originalarrivaltime || lm.composetime || '') || c.lastMessageTimeUtc || 0) >= cutoff) ids.add(c.id);
  }
  for (const id of ids) {
    if (!id.startsWith('19:')) continue;  // your own notes and system streams
    const r = rows[id], c = convs[id], lm = c && c.lastMessage;
    const type = r ? r.type : c ? (c.type === 'Meeting' ? 'MeetingChat' : /@unq\.gbl\.spaces$/.test(id) ? 'OneOnOneChat' : 'GroupChat') : '';
    const item = { id, type, name: titles[id] || clean(c?.threadProperties?.topic, 120), unread: out.dom ? !!(r && r.unread) : null,
      mention: !!(r && r.mention), sender: lm ? nameOf(lm.imdisplayname) : '',
      preview: lm ? previewOf(typeof lm.preview === 'string' ? lm.preview : plain(lm.content), lm.messagetype) : '',
      time: lm ? Date.parse(lm.originalarrivaltime || lm.composetime || '') || c.lastMessageTimeUtc || null : null,
      from_me: c ? !!c.threadProperties?.isLastMessageFromMe : null, count: null };
    if (item.unread && c && rstore) { try { item.count = await unreadCount(id, horizonOf(c)) || null; } catch (e) {} }
    out.chats.push(item);
  }

  // @mentions of you (Activity feed): unread ones whatever their age, read ones from the last few days.
  try {
    const adb = await open(named(/^Teams:activity-manager:/)); dbs.push(adb);
    const items = await done(adb.transaction('feed-items', 'readonly').objectStore('feed-items').getAll());
    out.feed = true;
    for (const i of items) {
      if (!/^mention/i.test(i.activityType || '') || /^(everyone|channel|team)$/i.test(i.activitySubtype || '')) continue;
      if (i.isRead && !(i.timestamp >= cutoff)) continue;
      const thread = String(i.sourceThreadId || ''), c = convs[thread];
      let msg = null;
      if (rstore) {
        try {
          const ch = await done(rdb.transaction(rstore, 'readonly').objectStore(rstore).get([thread, String(i.sourceReplyChainId || i.sourceMessageId)]));
          msg = ch && Object.values(ch.messageMap || {}).find((m) => m.id === String(i.sourceMessageId));
        } catch (e) {}
      }
      const channel = i.activityType === 'mention' || (!!c && !['Chat', 'Meeting'].includes(c.type));
      out.mentions.push({ id: String(i.activityId), thread, message: String(i.sourceMessageId || ''), parent: String(i.sourceReplyChainId || ''),
        channel, where: titles[thread] || clean(c?.threadProperties?.topic, 120), sender: msg ? nameOf(msg.imDisplayName) : '',
        preview: msg ? previewOf(plain(msg.content), msg.messageType) : '', time: Number(i.timestamp) || null, unread: !i.isRead });
    }
  } catch (e) {}
  if (!out.feed && out.dom) {  // no feed: the chat list's @ flags still give a count
    for (const r of Object.values(rows)) if (r.unread && r.mention)
      out.mentions.push({ id: `row:${r.id}`, thread: r.id, message: '', parent: '', channel: r.channel, where: titles[r.id] || '',
        sender: '', preview: '', time: null, unread: true });
  }
  for (const d of dbs) { try { d.close(); } catch (e) {} }
  return out;
}
"""


def settings() -> dict:
    return config.section("teams", {})


def url() -> str:
    return settings().get("url", "https://teams.microsoft.com/v2/")


async def _block_writes(route):
    if route.request.method in ("GET", "HEAD", "OPTIONS"):
        await route.continue_()
    else:
        await route.abort()


class _Session:
    """The background browser with Teams open, kept between checks (only read(), login() and shutdown() touch it)."""
    pw = None
    ctx = None
    page = None
    opened = 0.0  # time.monotonic() when Teams was loaded
    hold = False  # a sign-in window has the profile: don't reopen


_session = _Session()


def _signed_out(page) -> bool:
    return "login.microsoftonline.com" in page.url or "login.live.com" in page.url


def _looks_loaded(title: str) -> bool:
    return "Teams" in title and "|" in title


async def _close():
    s = _session
    ctx, pw = s.ctx, s.pw
    s.pw = s.ctx = s.page = None
    for step in (ctx.close if ctx else None, pw.stop if pw else None):
        if step:
            try:
                await asyncio.wait_for(step(), 15)
            except Exception as e:
                log.warning("Teams browser didn't close cleanly (%s)", type(e).__name__)


async def _open():
    """Starts the browser and loads Teams: ~20 s on several cores, the expensive part, hence kept open."""
    s = _session
    s.pw = await async_playwright().start()
    try:
        s.ctx = await web._launch(s.pw, PROFILE, headless=True)
        await s.ctx.add_init_script(_HIDDEN_JS)
        await s.ctx.route(_WRITES, _block_writes)
        s.page = s.ctx.pages[0] if s.ctx.pages else await s.ctx.new_page()
        await s.page.goto(url(), wait_until="domcontentloaded", timeout=60000)
        # Teams takes a while to boot; wait until the title says Teams, or we land on a sign-in page.
        for _ in range(45):
            if _signed_out(s.page):
                raise web.LoginNeeded("Teams needs you to sign in. Use Log in on the Teams tab.")
            if _looks_loaded(await s.page.title()):
                break
            await s.page.wait_for_timeout(1000)
        else:
            raise TimeoutError(f"Teams didn't finish loading (title: {await s.page.title()!r})")
        try:  # the chat list renders after the title; Teams may also open on another app (then there's none)
            await s.page.wait_for_selector('[role="treeitem"][data-item-type="chat"]', state="attached", timeout=15000)
        except Exception:
            pass
        await s.page.wait_for_timeout(3000)  # let the unread badges settle
        s.opened = time.monotonic()
    except BaseException:
        await _close()
        raise


async def read() -> tuple[int, dict | None]:
    """(title count, raw details or None). Read-only; see the module docstring."""
    keep = max(0, int(settings().get("keep_open_minutes", 60))) * 60
    while True:
        while _session.hold:
            await asyncio.sleep(1)
        async with web._lock(PROFILE):
            if _session.hold:  # a sign-in started while this waited for the lock
                continue
            s = _session
            try:
                reused = s.page is not None and not s.page.is_closed() and time.monotonic() - s.opened < keep
                if reused and (_signed_out(s.page) or not _looks_loaded(await s.page.title())):
                    log.info("Teams page needs a fresh start")
                    reused = False
                if not reused:
                    await _close()
                    await _open()
                m = TITLE_COUNT.match(await s.page.title())
                total = int(m.group(1)) if m else 0
                try:
                    raw = await s.page.evaluate(_READ_JS, {"days": RECENT_DAYS, "previewLen": PREVIEW_LEN})
                except Exception as e:
                    log.warning("Teams details unavailable (%s)", type(e).__name__)
                    raw = None
                if not reused:
                    log.info("Teams background browser opened (kept open %d min)", keep // 60)
                if not keep:
                    await _close()
                return total, raw
            except BaseException:
                await _close()  # a failed read starts afresh next time
                raise


async def shutdown():
    """Closes the background browser (Command Center quitting)."""
    _session.hold = True
    async with web._lock(PROFILE):
        await _close()


# ---- the raw read -> counts and the Recent list ----------------------------------------------------------------

def _s(v, n=120) -> str:
    return re.sub(r"\s+", " ", v).strip()[:n] if isinstance(v, str) else ""


def _ms(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


def _iso(ms) -> str | None:
    try:
        return datetime.fromtimestamp(ms / 1000).isoformat(timespec="seconds") if ms else None
    except (OverflowError, OSError, ValueError):
        return None


def _q(v) -> str:
    return urllib.parse.quote(v, safe="")


def chat_link(thread: str, tenant: str | None) -> str | None:
    """msteams: link that opens a chat in the Teams app (the form Graph gives as a chat's webUrl)."""
    if not isinstance(thread, str) or not _THREAD_ID.fullmatch(thread):
        return None
    return f"msteams:/l/chat/{_q(thread)}/0" + (f"?tenantId={tenant}" if tenant and _GUID.fullmatch(tenant) else "")


def message_link(thread: str, message: str, parent: str, channel: bool, tenant: str | None) -> str | None:
    """msteams: link to one message (a chat message, or a channel post / reply)."""
    if not isinstance(thread, str) or not _THREAD_ID.fullmatch(thread) or not re.fullmatch(r"\d{1,20}", message or ""):
        return None
    if not channel:
        return f"msteams:/l/message/{_q(thread)}/{message}?context=" + _q('{"contextType":"chat"}')
    query = {"parentMessageId": parent if re.fullmatch(r"\d{1,20}", parent or "") else message}
    if tenant and _GUID.fullmatch(tenant):
        query["tenantId"] = tenant
    return f"msteams:/l/message/{_q(thread)}/{message}?" + urllib.parse.urlencode(query)


_KIND = {"OneOnOneChat": "dm", "BotChat": "dm", "GroupChat": "group", "MeetingChat": "meeting"}


def summarize(total: int, raw: dict | None, now_ms: float | None = None) -> tuple[dict, dict]:
    """(counts, found). Counts are None where that part couldn't be read. `found` holds names and previews:
    "recent" (newest first, capped) and "unread" (every unread chat and @mention, for alerts)."""
    now_ms = now_ms or datetime.now().timestamp() * 1000
    cutoff = now_ms - RECENT_DAYS * 86400000
    raw = raw if isinstance(raw, dict) else {}
    dom, feed = raw.get("dom") is True, raw.get("feed") is True
    tenant = raw.get("tenant") if isinstance(raw.get("tenant"), str) else None
    urgent = people.urgent_people()

    def is_urgent(*names):
        return any(n and people.matches("", n, urgent) for n in names)

    items = []
    for c in raw.get("chats") or []:
        if not isinstance(c, dict) or not isinstance(c.get("id"), str) or not _THREAD_ID.fullmatch(c["id"]):
            continue
        kind = _KIND.get(c.get("type"), "group")
        name, sender = _s(c.get("name")), _s(c.get("sender"))
        count = c.get("count")
        items.append({
            "key": f"c:{c['id']}", "kind": kind, "thread": c["id"], "unread": c.get("unread") is True,
            "who": sender or name or ("Teams DM" if kind == "dm" else "Teams chat"),
            "where": name if kind != "dm" and sender and name != sender else "",
            "preview": _s(c.get("preview"), PREVIEW_LEN), "ms": _ms(c.get("time")),
            "count": count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else None,
            "urgent": is_urgent(sender, name if kind == "dm" else ""), "link": chat_link(c["id"], tenant),
        })
    for m in raw.get("mentions") or []:
        if not isinstance(m, dict) or not isinstance(m.get("id"), str):
            continue
        sender, where = _s(m.get("sender")), _s(m.get("where"))
        thread = m.get("thread") if isinstance(m.get("thread"), str) else ""
        channel = m.get("channel") is True
        items.append({
            "key": f"m:{m['id']}", "kind": "mention", "thread": thread, "unread": m.get("unread") is True,
            # (a mention in a 1:1 chat has that person as the chat name: no "where" then)
            "who": sender or "Someone", "where": "" if sender and where == sender else where or ("a channel" if channel else ""),
            "preview": _s(m.get("preview"), PREVIEW_LEN), "ms": _ms(m.get("time")), "count": None, "urgent": is_urgent(sender),
            "link": message_link(thread, m.get("message") or "", m.get("parent") or "", channel, tenant) or chat_link(thread, tenant),
        })

    unread_chats = [i for i in items if i["unread"] and i["kind"] != "mention"]
    unread_mentions = [i for i in items if i["unread"] and i["kind"] == "mention"]
    counts = {
        "dms_unread": sum(i["kind"] == "dm" for i in unread_chats) if dom else None,
        "groups_unread": sum(i["kind"] == "group" for i in unread_chats) if dom else None,
        "meetings_unread": sum(i["kind"] == "meeting" for i in unread_chats) if dom else None,
        "mentions_unread": len(unread_mentions) if feed or dom else None,
        "details": "ok" if dom and feed else "partial" if dom or feed else "unavailable",
        "badge": None,
    }
    if dom:  # unread DMs and group chats, plus @mentions that aren't in one of those chats already
        threads = {i["thread"] for i in unread_chats if i["kind"] in ("dm", "group")}
        counts["badge"] = len(threads) + sum(m["thread"] not in threads for m in unread_mentions)

    # Recent: unread items whatever their age, read ones from the last RECENT_DAYS (no read meeting chats); a chat
    # row whose last message is an @mention row already is left out. Newest first; unread ones without a time on top.
    mentioned = {(i["thread"], int(i["ms"] // 1000)) for i in items if i["kind"] == "mention" and i["ms"]}
    recent = [i for i in items
              if (i["unread"] or ((i["ms"] or 0) >= cutoff and i["kind"] != "meeting"))
              and not (i["kind"] != "mention" and i["ms"] and (i["thread"], int(i["ms"] // 1000)) in mentioned)]
    recent.sort(key=lambda i: i["ms"] or (now_ms * 2 if i["unread"] else 0), reverse=True)
    for i in items:
        i["time"] = _iso(i.pop("ms"))
    return counts, {"recent": recent[:RECENT_CAP], "unread": unread_chats + unread_mentions}


def details() -> dict:
    """The last read's Recent list (memory only, so empty until the first check after a start)."""
    return {"recent": list(_details.get("recent", [])), "read_at": _details.get("read_at")}


# ---- alerts -------------------------------------------------------------------------------------------------

def _alert_new(unread: list[dict]):
    """One alert per newly unread DM / group chat (or a newer message in one) and per new @mention; never again
    for the same message on later checks. The first read only records what's already unread."""
    known = db.kv_get("teams:alerted")  # {key: time of the message last alerted on}; None before the first read
    current = {i["key"]: i["time"] or "" for i in unread if i["kind"] != "meeting"}  # meeting chats count, no pop-up
    if isinstance(known, dict):
        new = [i for i in unread if i["key"] in current and (i["key"] not in known or current[i["key"]] > (known[i["key"]] or ""))]
        mention_threads = {i["thread"] for i in new if i["kind"] == "mention"}
        new = [i for i in new if i["kind"] == "mention" or i["thread"] not in mention_threads]
        base = settings().get("priority", "normal")
        for i in new[:ALERTS_PER_CHECK]:
            mention = i["kind"] == "mention"
            title = (f"{i['who']} mentioned you" if mention else f"Teams message from {i['who']}") +                 (f" in {i['where']}" if i["where"] else "")
            alerts.raise_alert("Teams", title, i["preview"], "high" if i["urgent"] else base,
                               {"type": "teams", "url": i["link"] or "msteams:", "thread": i["thread"]},
                               toast="mention" if mention else "new message")
        if len(new) > ALERTS_PER_CHECK:
            alerts.raise_alert("Teams", f"{len(new) - ALERTS_PER_CHECK} more new in Teams", "", base,
                               {"type": "teams", "url": "msteams:"}, toast="new messages")
        if new:
            log.info("Teams: %d newly unread item(s)", len(new))
    db.kv_set("teams:alerted", current)


_LINK_THREAD = re.compile(r"/l/(?:chat|message)/([^/?#]+)/")


def _clear_answered(unread: list[dict]) -> int:
    """Deletes the alerts for chats that aren't unread any more: Joe read or answered them (in Command Center's Teams
    view, the Teams app or anywhere else). Only after a full read (chat list and activity feed), so a partial one never
    clears anything. Alerts without a chat ("N more new in Teams") stay."""
    still = {i["thread"] for i in unread}
    done = []
    for alert_id, a in db.action_alerts("teams"):
        thread = a.get("thread")
        if not thread:  # alerts from before the thread was stored: from the deep link
            m = _LINK_THREAD.search(str(a.get("url") or ""))
            thread = urllib.parse.unquote(m.group(1)) if m else None
        if thread and thread not in still:
            done.append(alert_id)
    n = db.delete_alerts(done)
    if n:
        log.info("Teams: removed %d alert(s) for chats read or answered", n)
    return n


async def check() -> dict:
    state = db.kv_get("teams:state", {})
    was_ok = state.get("ok", True)
    try:
        total, raw = await read()
        counts, found = summarize(total, raw)
        _details.clear()
        _details.update(recent=found["recent"], read_at=db.now())
        if counts["badge"] is not None:
            _alert_new(found["unread"])
            if counts["details"] == "ok":
                _clear_answered(found["unread"])
        elif total > state.get("unread", 0):  # no chat list this time: the title count alone, as before
            alerts.raise_alert("Teams", f"{total} unread in Teams", "", settings().get("priority", "normal"),
                               {"type": "teams", "url": "msteams:"}, toast=f"{int(total)} unread")
        log.info("Teams read: total=%d dms=%s groups=%s meetings=%s mentions=%s details=%s", total, counts["dms_unread"],
                 counts["groups_unread"], counts["meetings_unread"], counts["mentions_unread"], counts["details"])
        state = {"ok": True, "unread": total, **counts, "checked": db.now()}
    except Exception as e:
        msg = str(e).splitlines()[0][:300]
        login = isinstance(e, web.LoginNeeded)
        if was_ok:
            alerts.raise_alert("Teams", "Teams needs a sign-in" if login else "Teams check failed", msg, "normal",
                               toast="sign-in needed" if login else "check failed")
        state = {**state, "ok": False, "checked": db.now(), "error": msg, "login_needed": login}
    db.kv_set("teams:state", state)
    return state


async def login():
    # The sign-in window needs the profile, which the background browser keeps open: close it, and keep read()
    # from reopening it until the window has closed.
    _session.hold = True
    try:
        async with web._lock(PROFILE):
            await _close()
        await web.login(PROFILE, url())
    finally:
        _session.hold = False


def is_set_up() -> bool:
    return (config.DATA_DIR / "browser" / PROFILE).exists()


async def watch_loop():
    while True:
        cfg = settings()
        started = time.monotonic()
        if cfg.get("enabled", True) and is_set_up():
            try:
                await check()
            except Exception:
                log.exception("Teams check crashed")
        # poll_minutes from the start of one check to the next (a fresh start takes ~20 s), at least 15 s apart
        await asyncio.sleep(max(15, max(60, int(cfg.get("poll_minutes", 2)) * 60) - (time.monotonic() - started)))
