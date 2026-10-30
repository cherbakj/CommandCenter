// Triage mode: a full-window overlay that walks through the reply-needed follow-ups one at a time,
// urgent first, with single-key actions (R reply, S snooze, D done, T task, O Outlook, X spam (external senders,
// spam.js), J/K or arrows to move).
// Mail bodies are untrusted: everything goes in as text (CC.el / textContent), never as HTML, and nothing
// about a message is logged. Bodies are fetched on demand and dropped when triage closes.
(() => {
  const CC = window.CC;
  if (!CC) return;
  const { el, api } = CC;

  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = "/static/triage.css";
  document.head.append(link);

  const pad = (n) => String(n).padStart(2, "0");
  const localIso = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:00`;
  const isoDay = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const dayTime = (d) => d.toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  const fromLine = (name, email) => (email && name && !name.includes(email) ? `${name} <${email}>` : name || email || "");
  const MAX_BODY = 60000;

  function toast(text) {
    let box = document.querySelector("#toast");
    if (!box) { box = el("div", { id: "toast", role: "status", "aria-live": "polite" }); document.body.append(box); }
    box.textContent = text;
    box.classList.add("show");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => box.classList.remove("show"), 3500);
  }

  // ---- the queue ---------------------------------------------------------------------------------
  // Urgent first: urgent replies, urgent Teams, needs-your-reply, other Teams, recent flags, important senders' unread.
  // A message can sit in two groups (needs a reply and flagged): it comes up once, in the first.
  function buildQueue(data) {
    const f = data && data.followups && !data.followups.error ? data.followups : {};
    const teams = Array.isArray(f.teams_reply) ? f.teams_reply : [];
    const seen = new Set(), out = [];
    const add = (items, info) => {
      for (const m of Array.isArray(items) ? items : []) {
        if (!m || !m.id || seen.has(m.id)) continue;
        seen.add(m.id);
        out.push({ m, ...info, urgent: info.urgent || !!m.urgent });
      }
    };
    add(f.urgent_reply, { group: "Urgent — reply needed", src: "outlook", urgent: true });
    add(teams.filter((m) => m && m.urgent), { group: "Teams — urgent", src: "teams", urgent: true });
    add(f.needs_reply, { group: "Needs your reply", src: "outlook" });
    add(teams.filter((m) => m && !m.urgent), { group: "Teams — reply needed", src: "teams" });
    add(f.flagged, { group: "Flagged", src: "flagged" });
    add(f.unread_vip, { group: "Important senders", src: "outlook" });
    return out;
  }

  const S = { queue: [], i: 0, cleared: new Map(), bodies: new Map(), drafts: new Map(), pane: null, started: 0,
    changed: false, busy: false, escAt: 0, end: false, reviewing: false };
  let dlg, shell, topCount, topBar, card, keysBar, slotBtn;

  // ---- overlay skeleton -------------------------------------------------------------------------
  const CLEARING = new Set(["r", "s", "d", "t", "x"]);  // the actions that clear an item
  const KEYS = [
    ["r", "R", "Reply"], ["s", "S", "Snooze"], ["d", "D", "Done"], ["t", "T", "Task"], ["o", "O", "Outlook"],
    ["x", "X", "Spam"], ["prev", "← K", "Prev"], ["next", "→ J", "Next"], ["exit", "Esc", "Exit"],
  ];
  // X (mark as spam, spam.js) only for external senders' emails; the key is hidden otherwise.
  const spamOk = (item) => !!(item && item.src !== "teams" && CC.spam && CC.spam.eligible(item.m));

  function build() {
    topCount = el("span", { class: "tri-count", "aria-live": "polite" });
    topBar = el("div", { class: "tri-bar", "aria-hidden": "true" });
    card = el("div", { class: "tri-card" });
    keysBar = el("div", { class: "tri-keys", role: "toolbar", "aria-label": "Triage actions" });
    shell = el("div", { class: "tri-shell", tabindex: "-1" },
      el("div", { class: "tri-top" },
        el("span", { class: "tri-mark" }, "TRIAGE", el("span", { class: "slash" }, "//"), "MODE"),
        topCount, topBar,
        el("button", { type: "button", class: "small subtle tri-exit", onclick: exit }, "Exit ", el("kbd", {}, "Esc"))),
      card, keysBar);
    dlg = el("dialog", { class: "tri", "aria-label": "Triage follow-ups" }, shell);
    dlg.addEventListener("keydown", onKey);
    // Esc is handled on keydown; this catches a close request that comes some other way.
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); if (performance.now() - S.escAt > 150) exit(); });
    dlg.addEventListener("close", cleanup);
    document.body.append(dlg);
  }

  function renderKeys() {
    const item = S.queue[S.i];
    const teams = item && item.src === "teams";
    const off = S.end || !item;
    const handled = !off && S.cleared.has(item.m.id);  // already cleared this session: only O and moving on
    keysBar.replaceChildren(...KEYS.filter(([id]) => id !== "x" || (!off && spamOk(item))).map(([id, key, label]) => {
      const text = id === "r" && teams ? "Open Teams" : label;
      const disabled = (S.reviewing && id !== "exit") || (off && !["prev", "exit"].includes(id)) || (S.busy && id !== "exit")
        || (handled && CLEARING.has(id));
      return el("button", { type: "button", class: `tri-key ${id === "d" ? "k-done" : ""} ${id === "r" ? "k-reply" : ""} ${S.pane === id ? "on" : ""}`,
        disabled, "data-key": id, onclick: () => act(id) }, el("kbd", {}, key), el("span", {}, text));
    }));
  }

  function renderTop() {
    const total = S.queue.length, cleared = S.cleared.size;
    topCount.textContent = total ? `${S.end ? "End" : `${S.i + 1} of ${total}`} · ${cleared} cleared` : "Nothing waiting";
    const segs = total <= 60;
    topBar.classList.toggle("solid", !segs);
    if (segs) {
      topBar.replaceChildren(...S.queue.map((q, i) => el("i", {
        class: `${S.cleared.has(q.m.id) ? "done" : ""} ${i === S.i && !S.end ? "cur" : ""} ${q.urgent ? "hot" : ""}` })));
    } else {
      topBar.replaceChildren(el("i", { class: "fill", style: `width:${Math.round((cleared / total) * 100)}%` }));
    }
  }

  // ---- one message ------------------------------------------------------------------------------
  function loadBody(id) {
    if (!S.bodies.has(id)) {
      const p = api(`/api/mail/item?id=${encodeURIComponent(id)}`).catch((e) => ({ error: e.message || "Couldn't load it" }));
      S.bodies.set(id, p);
    }
    return S.bodies.get(id);
  }

  function statusText(kind) {
    return { done: "Done", snoozed: "Snoozed", replied: "Replied", task: "Task created", spam: "Marked as spam" }[kind] || "Cleared";
  }

  function renderItem() {
    S.pane = null;
    S.reviewing = false;
    card.classList.remove("with-pane", "reviewing");
    S.end = false;
    const item = S.queue[S.i];
    if (!item) { renderEnd(); return; }
    const { m } = item;
    const status = S.cleared.get(m.id);
    const body = el("pre", { class: "tri-body-text" }, m.preview ? `${String(m.preview)}\n\n…` : "");
    const bodyBox = el("div", { class: "tri-body loading", tabindex: "0", "aria-label": "Message" }, body);
    const sender = item.src === "teams" ? (m.from_name ? `${m.from_name} (via Teams notification)` : fromLine(m.sender_name, m.sender))
      : fromLine(m.sender_name, m.sender);
    const received = m.received ? new Date(m.received) : null;
    card.replaceChildren(
      el("div", { class: "tri-meta" },
        el("span", { class: `tri-group ${item.urgent ? "hot" : ""}` }, item.group),
        item.urgent && el("span", { class: "tag urgent" }, "Urgent"),
        m.high_importance && el("span", { class: "tag overdue" }, "High importance"),
        status && el("span", { class: "tri-status" }, `✓ ${statusText(status)}`)),
      el("h2", { class: "tri-subject" }, m.subject || "(no subject)"),
      status ? el("div", { class: "tri-handled", role: "status" },  // (replaceChildren would print a bare `undefined`)
        el("span", {}, `${statusText(status)} earlier in this session, so Reply, Snooze, Done and Task are off for it.`),
        status === "snoozed" && el("button", { type: "button", class: "small subtle", onclick: undoSnooze }, "Undo snooze")) : "",
      el("div", { class: "tri-from src-line" },
        CC.srcTag(item.src, item.src === "teams" ? `notification from ${m.sender || "Teams"}` : m.internal === false ? "external sender" : ""),
        el("span", { class: "tri-sender" }, sender),
        received && !isNaN(received) && el("span", { class: "tri-when" }, ` · ${dayTime(received)}`)),
      bodyBox,
      el("div", { class: "tri-pane", hidden: true }));
    renderTop();
    renderKeys();
    const id = m.id;
    loadBody(id).then((full) => {
      if (!dlg.open || S.queue[S.i]?.m.id !== id) return;
      bodyBox.classList.remove("loading");
      if (full.error) {
        bodyBox.prepend(el("div", { class: "err" }, `Couldn't load the full message: ${full.error}. Showing the preview.`));
        return;
      }
      const tidy = (raw) => {
        const text = String(raw || "").replace(/\r\n/g, "\n").replace(/\n{4,}/g, "\n\n\n");
        return text.length > MAX_BODY ? `${text.slice(0, MAX_BODY)}\n\n… (long message: open it in Outlook for the rest)` : text || "(no text)";
      };
      body.replaceWith(CC.mailContent(full, "tri-body-text", tidy));
      if (full.to || full.cc) {
        bodyBox.before(el("div", { class: "tri-to muted" }, `To: ${String(full.to || "").slice(0, 300)}${full.cc ? `  ·  Cc: ${String(full.cc).slice(0, 300)}` : ""}`));
      }
      item.full = full;
    });
    const next = S.queue[S.i + 1];
    if (next) loadBody(next.m.id);  // ready by the time Joe gets there
    shell.focus({ preventScroll: true });
  }

  function renderEnd() {
    S.end = true;
    S.pane = null;
    S.reviewing = false;
    card.classList.remove("with-pane", "reviewing");
    const total = S.queue.length;
    const left = S.queue.filter((q) => !S.cleared.has(q.m.id));
    const tally = {};
    for (const k of S.cleared.values()) tally[k] = (tally[k] || 0) + 1;
    const secs = Math.round((Date.now() - S.started) / 1000);
    const took = secs >= 60 ? `${Math.floor(secs / 60)}m ${pad(secs % 60)}s` : `${secs}s`;
    const clear = left.length === 0;
    const stat = (n, label) => el("div", { class: "tri-stat" }, el("b", {}, String(n)), el("span", {}, label));
    card.replaceChildren(el("div", { class: `tri-end ${clear ? "clear" : "partial"}`, role: "status" },
      el("div", { class: "tri-rings", "aria-hidden": "true" }, el("i"), el("i"), el("i"), el("span", { class: "tri-check" }, clear ? "✓" : "…")),
      el("div", { class: "tri-end-title" }, clear ? (total ? "QUEUE CLEAR" : "ALL CLEAR") : "END OF QUEUE"),
      el("p", { class: "tri-end-sub" }, clear
        ? (total ? `All ${total} follow-up${total === 1 ? "" : "s"} handled in ${took}. Inbox under control.` : "Nothing needs a reply right now. Enjoy it.")
        : `${left.length} still waiting. Go back to them, or come back later.`),
      total > 0 && el("div", { class: "tri-stats" },
        stat(S.cleared.size, "cleared"), stat(tally.replied || 0, "replied"), stat(tally.done || 0, "done"),
        stat(tally.snoozed || 0, "snoozed"), stat(tally.task || 0, "to tasks"), stat(took, "time")),
      el("div", { class: "row tri-end-acts" },
        !clear && el("button", { type: "button", class: "alt", onclick: () => go(S.queue.indexOf(left[0])) }, `Review the ${left.length} left`),
        el("button", { type: "button", class: "primary", onclick: exit }, "Back to Today"))));
    renderTop();
    renderKeys();
    (card.querySelector(".tri-end-acts button") || shell).focus({ preventScroll: true });
  }

  function go(i) {
    if (!S.queue.length) return;
    S.i = Math.max(0, Math.min(S.queue.length - 1, i));
    renderItem();
  }

  // The next item still waiting after this one; at the end of the list, the summary (it offers going back).
  function advance() {
    for (let j = S.i + 1; j < S.queue.length; j++) {
      if (!S.cleared.has(S.queue[j].m.id)) { S.i = j; renderItem(); return; }
    }
    renderEnd();
  }

  function markCleared(kind) {
    const item = S.queue[S.i];
    S.cleared.set(item.m.id, kind);
    S.drafts.delete(item.m.id);
    S.changed = true;
    advance();
  }

  // The one clearing action that can be taken back (the server has /api/unsnooze); the item is then open again.
  function undoSnooze() {
    const item = S.queue[S.i];
    if (!item || S.cleared.get(item.m.id) !== "snoozed") return;
    run(async () => {
      await api("/api/unsnooze", { id: item.m.id });
      S.cleared.delete(item.m.id);
      S.changed = true;
      toast("Snooze undone");
      renderItem();
    });
  }

  // ---- panes (reply / snooze / task) under the message ------------------------------------------
  function paneBox() { return card.querySelector(".tri-pane"); }

  function closePane() {
    S.reviewing = false;
    card.classList.remove("reviewing");
    const box = paneBox();
    if (box) { box.hidden = true; box.replaceChildren(); }
    S.pane = null;
    card.classList.remove("with-pane");
    renderKeys();
    shell.focus({ preventScroll: true });
  }

  function openPane(id, ...content) {
    const box = paneBox();
    if (!box) return null;
    S.pane = id;
    box.hidden = false;
    box.replaceChildren(...content);
    card.classList.add("with-pane");
    renderKeys();
    return box;
  }

  async function run(fn, errBox) {
    if (S.busy) return false;
    S.busy = true;
    renderKeys();
    try { await fn(); return true; } catch (e) {
      if (errBox) errBox.textContent = e.message || "Something went wrong";
      else toast(e.message || "Something went wrong");
      return false;
    } finally { S.busy = false; if (dlg.open) renderKeys(); }
  }

  // Reply box. Sending always goes through a server-checked review: Ctrl+Enter (or Review) asks
  // /api/mail/reply/preview for the exact recipients and a single-use approval, shows them, and only a
  // click on Send (or Enter/Space with the Send button itself focused) sends with that approval.
  // "Open draft in Outlook" needs no review: Joe sends it from Outlook himself.
  function replyPane() {
    const item = S.queue[S.i];
    const { m } = item;
    const text = el("textarea", { class: "tri-reply-text", placeholder: "Your reply…", "aria-label": "Reply text", spellcheck: "true" });
    text.value = S.drafts.get(m.id) || "";
    text.addEventListener("input", () => S.drafts.set(m.id, text.value));
    const all = el("input", { type: "checkbox", id: "tri-reply-all" });
    const remind = el("select", { id: "tri-remind", "aria-label": "Remind me if no reply" },
      [0, 1, 2, 3, 5].map((n) => el("option", { value: n }, n ? `Remind me in ${n} day${n > 1 ? "s" : ""}` : "No reminder")));
    const err = el("div", { class: "err", role: "alert" });
    const review = el("div", { class: "tri-review", hidden: true, tabindex: "-1", "aria-label": "Review the reply before sending" });
    const draftBtn = el("button", { type: "button", onclick: () => openDraft() }, "Open draft in Outlook");
    const reviewBtn = el("button", { type: "button", class: "alt", onclick: () => askReview() }, "Review & send… ", el("kbd", {}, "Ctrl+Enter"));
    const editor = el("div", { class: "tri-editor" }, text,
      el("div", { class: "tri-reply-row" },
        el("label", { for: "tri-reply-all", class: "tri-check-label" }, all, "Reply all"), remind,
        el("span", { class: "spacer" }),
        el("button", { type: "button", class: "subtle", onclick: closePane }, "Cancel ", el("kbd", {}, "Esc")),
        draftBtn, reviewBtn));

    const need = () => {
      err.textContent = "";
      if (text.value.trim()) return true;
      text.focus();
      err.textContent = "Write the reply first.";
      return false;
    };

    async function openDraft() {
      if (!need()) return;
      const ok = await run(() => api("/api/mail/reply", { id: m.id, text: text.value, reply_all: all.checked, send: false, remind_days: 0 }), err);
      if (!ok) return;
      toast("Draft opened in Outlook. Review and send it there.");
      S.drafts.delete(m.id);
      closePane();
    }

    async function askReview() {
      if (!need() || S.busy) return;
      const sent = { id: m.id, text: text.value, reply_all: all.checked };
      let p = null;
      const ok = await run(async () => { p = await api("/api/mail/reply/preview", sent); }, err);
      if (!ok || !p || !p.approval_id || !dlg.open || S.queue[S.i]?.m.id !== m.id || S.pane !== "r") return;
      showReview(sent, p);
    }

    function showReview(sent, p) {
      const recips = Array.isArray(p.recipients) ? p.recipients : [];
      const outside = recips.filter((r) => r && r.external);
      const days = Number(remind.value);
      const expires = Date.now() + (Number(p.expires_in) || 300) * 1000;
      const sendBtn = el("button", { type: "button", class: "tri-send", onclick: () => send(sent, p.approval_id, days, expires) },
        "Send now");
      const editBtn = el("button", { type: "button", class: "subtle", onclick: backToEdit }, "Edit ", el("kbd", {}, "Esc"));
      review.replaceChildren(
        el("div", { class: "tri-review-head" },
          el("span", { class: "panel-label" }, "REVIEW BEFORE SENDING"),
          el("span", { class: "muted" }, `${recips.length} recipient${recips.length === 1 ? "" : "s"}${outside.length ? ` · ${outside.length} outside Sonance` : ""}`)),
        el("div", { class: "tri-review-subj" }, el("span", { class: "muted" }, "Subject "), String(p.subject || m.subject || "")),
        el("ul", { class: "tri-recips" }, recips.length ? recips.map((r) => el("li", { class: r.external ? "ext" : "" },
          el("span", { class: "tri-rtype" }, r.type === "CC" ? "Cc" : "To"),
          el("span", { class: "tri-rname" }, fromLine(r.name, r.email) || "(unknown)"),
          r.external && el("span", { class: "tri-ext" }, "outside Sonance")))
          : el("li", { class: "ext" }, el("span", { class: "tri-rname" }, "No recipients found. Open it as a draft in Outlook instead."))),
        el("pre", { class: "tri-review-text" }, String(p.text ?? sent.text)),
        el("div", { class: "muted" }, "It goes out from Outlook as soon as you click Send.",
          days ? ` You'll get an alert if nobody replies in ${days} day${days > 1 ? "s" : ""}.` : ""),
        el("div", { class: "row tri-review-acts" }, editBtn, recips.length ? sendBtn : null));
      editor.hidden = true;
      review.hidden = false;
      S.reviewing = true;
      card.classList.add("reviewing");
      renderKeys();
      review.focus({ preventScroll: true });  // not the Send button: nothing sends without a deliberate click
      review.scrollIntoView({ block: "nearest" });
    }

    function backToEdit() {
      S.reviewing = false;  // the approval is dropped; any change needs a fresh review
      review.hidden = true;
      review.replaceChildren();
      editor.hidden = false;
      card.classList.remove("reviewing");
      renderKeys();
      text.focus();
    }

    async function send(sent, approval, days, expires) {
      if (Date.now() > expires) { backToEdit(); err.textContent = "The review expired. Review it again to send."; return; }
      const ok = await run(() => api("/api/mail/reply", { ...sent, send: true, approval_id: approval, remind_days: days }), err);
      if (!ok) { const msg = err.textContent; backToEdit(); err.textContent = `Not sent: ${msg}`; return; }
      S.reviewing = false;
      card.classList.remove("reviewing");
      toast("Reply sent from Outlook.");
      CC.refreshAlerts();  // the email's alerts were removed with the reply
      markCleared("replied");
    }

    text.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && !e.isComposing) {
        e.preventDefault();
        e.stopPropagation();
        e.shiftKey ? openDraft() : askReview();
      }
    });
    review.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); S.escAt = performance.now(); backToEdit(); }
    });
    const label = el("span", { class: "panel-label" }, "REPLY");
    openPane("r",
      el("div", { class: "tri-pane-head" }, label,
        el("span", { class: "muted" }, "to ", fromLine(m.sender_name, m.sender)),
        el("span", { class: "muted tri-pane-hint" }, el("kbd", {}, "Ctrl+Enter"), " review · ", el("kbd", {}, "Ctrl+Shift+Enter"), " draft")),
      editor, review, err);
    all.addEventListener("change", () => { label.textContent = all.checked ? "REPLY ALL" : "REPLY"; });
    text.focus();
  }

  function snoozeChoices(now = new Date()) {
    const day = (offset, h) => new Date(now.getFullYear(), now.getMonth(), now.getDate() + offset, h, 0, 0);
    const in3 = new Date(Math.ceil((now.getTime() + 3 * 3600000) / 300000) * 300000);
    return [["1", "3 hours", in3], ["2", "Tomorrow 8 AM", day(1, 8)], ["3", "Monday 8 AM", day((8 - now.getDay()) % 7 || 7, 8)]];
  }

  function snoozePane() {
    const { m } = S.queue[S.i];
    const err = el("div", { class: "err", role: "alert" });
    const choose = (d) => run(async () => {
      await api("/api/snooze", { id: m.id, until: localIso(d), subject: m.subject || "", who: m.from_name || m.sender_name || "", kind: "mail" });
      toast(`Snoozed until ${dayTime(d)}`);
      markCleared("snoozed");
    }, err);
    const opts = snoozeChoices().map(([k, label, d]) => el("button", { type: "button", class: "snooze-opt tri-snooze", "data-n": k, onclick: () => choose(d) },
      el("b", {}, el("kbd", {}, k), " ", label), el("span", {}, dayTime(d))));
    openPane("s",
      el("div", { class: "tri-pane-head" }, el("span", { class: "panel-label" }, "SNOOZE"),
        el("span", { class: "muted" }, "Hidden from follow-ups until then; you get an alert when it's back."),
        el("span", { class: "muted tri-pane-hint" }, el("kbd", {}, "1"), el("kbd", {}, "2"), el("kbd", {}, "3"), " pick · ", el("kbd", {}, "Esc"), " back")),
      el("div", { class: "snooze-opts tri-snooze-opts" }, opts), err);
    opts[0].focus();
  }

  function taskPane() {
    const { m } = S.queue[S.i];
    const subject = el("input", { type: "text", maxlength: "250", "aria-label": "Task title", value: m.subject || "" });
    const due = el("input", { type: "date", "aria-label": "Due date" });
    const err = el("div", { class: "err", role: "alert" });
    const quick = (label, d) => el("button", { type: "button", class: "small subtle", onclick: () => { due.value = d ? isoDay(d) : ""; subject.focus(); } }, label);
    const today = new Date();
    const create = () => run(async () => {
      if (!subject.value.trim()) { subject.focus(); throw new Error("Give the task a title."); }
      const received = m.received ? new Date(m.received) : null;
      await api("/api/tasks", { source: "outlook", subject: subject.value.trim(), due: due.value || null,
        notes: `From ${fromLine(m.sender_name, m.sender)}${received && !isNaN(received) ? `, ${dayTime(received)}` : ""}\nSubject: ${m.subject || ""}` });
      toast(`Task created${due.value ? ` · due ${new Date(`${due.value}T00:00`).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })}` : ""}`);
      markCleared("task");
    }, err);
    for (const input of [subject, due]) input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); create(); } });
    openPane("t",
      el("div", { class: "tri-pane-head" }, el("span", { class: "panel-label" }, "NEW TASK"),
        el("span", { class: "muted" }, "Outlook / To Do"),
        el("span", { class: "muted tri-pane-hint" }, el("kbd", {}, "Enter"), " create · ", el("kbd", {}, "Esc"), " back")),
      el("div", { class: "tri-task-row" }, subject, due),
      el("div", { class: "tri-task-quick" }, el("span", { class: "muted" }, "Due:"), quick("None", null), quick("Today", today),
        quick("Tomorrow", new Date(today.getFullYear(), today.getMonth(), today.getDate() + 1)),
        quick("Friday", new Date(today.getFullYear(), today.getMonth(), today.getDate() + (((5 - today.getDay() + 7) % 7) || 7))),
        el("span", { class: "spacer" }),
        el("button", { type: "button", class: "primary", onclick: create }, "Create task")),
      err);
    subject.focus();
    subject.select();
  }

  // ---- actions ------------------------------------------------------------------------------------
  function act(id) {
    if (id === "exit") { exit(); return; }
    if (id === "prev") { if (S.end) go(S.queue.length - 1); else if (S.i > 0) go(S.i - 1); return; }
    if (id === "next") { if (S.end) return; if (S.i < S.queue.length - 1) go(S.i + 1); else renderEnd(); return; }
    const item = S.queue[S.i];
    if (!item || S.end || S.busy) return;
    if (CLEARING.has(id) && S.cleared.has(item.m.id)) return;  // handled already (its keys are disabled too)
    if (S.pane === id) { closePane(); return; }
    if (id === "r") {
      if (item.src === "teams") { CC.openExternal("msteams:"); return; }
      replyPane();
    } else if (id === "s") snoozePane();
    else if (id === "t") taskPane();
    else if (id === "d") {
      run(async () => {
        await api("/api/mail/read", { id: item.m.id, read: true });
        if (item.src === "flagged") await api("/api/mail/flag", { id: item.m.id, flagged: false });  // done with a flag = unflag it
        await api("/api/followups/dismiss", { id: item.m.id });
        markCleared("done");
      });
    } else if (id === "o") run(() => api("/api/outlook/open", { id: item.m.id }));
    else if (id === "x" && spamOk(item)) {
      CC.spam.open(item.m, { onBlocked: () => { if (dlg.open && S.queue[S.i] === item) markCleared("spam"); },
        onClose: () => { if (dlg.open) shell.focus({ preventScroll: true }); } });
    }
  }

  const KEYMAP = { r: "r", s: "s", d: "d", t: "t", o: "o", x: "x", j: "next", k: "prev", arrowright: "next", arrowleft: "prev" };

  function onKey(e) {
    if (e.defaultPrevented || e.isComposing) return;
    if (e.key === "Escape") {
      e.preventDefault();
      S.escAt = performance.now();
      if (S.pane) closePane(); else exit();
      return;
    }
    if (S.reviewing) return;  // while a reply is under review only a click on Send (or Esc / Edit) does anything
    const t = e.target;
    const typing = t instanceof Element && (t.matches("input, textarea, select, [contenteditable], [contenteditable] *"));
    if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
    const key = e.key.toLowerCase();
    if (S.pane === "s" && /^[1-3]$/.test(key)) {
      e.preventDefault();
      paneBox()?.querySelector(`.tri-snooze[data-n="${key}"]`)?.click();
      return;
    }
    if (!S.end && (key === "arrowdown" || key === "arrowup" || key === "pagedown" || key === "pageup" || key === " ")) {
      const body = card.querySelector(".tri-body");
      if (body && t !== body && !(t instanceof HTMLButtonElement && key === " ")) {
        e.preventDefault();
        const step = key.startsWith("page") || key === " " ? body.clientHeight * 0.85 : 48;
        body.scrollBy({ top: key === "arrowup" || key === "pageup" ? -step : step });
      }
      return;
    }
    if (e.shiftKey && key !== "t") return;
    const id = KEYMAP[key];
    if (id) { e.preventDefault(); act(id); }
  }

  // ---- open / close -------------------------------------------------------------------------------
  async function open() {
    if (!dlg) build();
    if (dlg.open) { shell.focus(); return; }
    document.querySelector("#palette.open")?.classList.remove("open");
    let data = CC.todayData;
    S.queue = buildQueue(data);
    S.i = 0; S.cleared = new Map(); S.bodies = new Map(); S.drafts = new Map(); S.pane = null; S.changed = false;
    S.busy = false; S.end = false; S.started = Date.now();
    dlg.showModal();
    if (!data) {
      card.replaceChildren(el("div", { class: "tri-loading muted" }, "Scanning follow-ups…"));
      renderTop(); renderKeys();
      try { data = await api("/api/today"); } catch (e) {
        card.replaceChildren(el("div", { class: "err" }, `Couldn't load follow-ups: ${e.message}`));
        return;
      }
      if (!dlg.open) return;
      S.queue = buildQueue(data);
    }
    S.queue.length ? renderItem() : renderEnd();
  }

  function exit() {
    if (dlg?.open) dlg.close();
  }

  function cleanup() {
    // Drop the fetched bodies and drafts: nothing about the messages stays in memory after triage.
    S.bodies = new Map(); S.drafts = new Map(); S.queue = []; S.pane = null; S.reviewing = false;
    card.replaceChildren();
    if (S.changed) { S.changed = false; CC.loaders.today?.(); }
  }

  // ---- entry points ---------------------------------------------------------------------------------
  function waitingCount(data) {
    return buildQueue(data).length;
  }

  function mountButton() {
    const slot = document.querySelector("#followups-slot");
    if (!slot || slotBtn) return;
    slotBtn = el("button", { type: "button", class: "small alt tri-launch", title: "Go through follow-ups one at a time (Shift+T)", onclick: open },
      "▶ Triage", el("span", { class: "tri-launch-n" }));
    slot.append(slotBtn);
    updateButton(CC.todayData);
  }

  function updateButton(data) {
    if (!slotBtn) return;
    const n = data ? waitingCount(data) : null;
    slotBtn.querySelector(".tri-launch-n").textContent = n === null ? "" : ` ${n}`;
    slotBtn.setAttribute("aria-label", n === null ? "Triage follow-ups" : `Triage ${n} follow-up${n === 1 ? "" : "s"} (Shift+T)`);
    slotBtn.classList.toggle("has", !!n);
  }

  document.addEventListener("cc:today", (e) => updateButton(e.detail));

  const anyModal = () => !!document.querySelector("dialog[open], .restart-overlay, .sp-overlay, [aria-modal='true'], #palette.open");
  document.addEventListener("keydown", (e) => {
    if (e.key !== "T" || !e.shiftKey || e.ctrlKey || e.altKey || e.metaKey || e.repeat || e.defaultPrevented) return;
    const a = document.activeElement;
    if (a && (a.matches("input, textarea, select, [contenteditable], [contenteditable] *") || a.tagName === "IFRAME")) return;
    if (anyModal()) return;
    e.preventDefault();
    open();
  });

  CC.addPaletteSource(() => {
    const n = CC.todayData ? waitingCount(CC.todayData) : null;
    return [{ group: "Commands", label: "Triage follow-ups", hint: `one at a time · Shift+T${n === null ? "" : ` · ${n} waiting`}`, run: open }];
  });
  CC.triage = open;

  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", mountButton, { once: true }) : mountButton();
})();
