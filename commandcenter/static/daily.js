// Workday helpers on Today: Top 3 priorities (strip at the top of Today), the uncommitted-work indicator,
// the "Yesterday" recap with a stand-up copy, and private meeting notes with action items.
// Everything is drawn with CC.el / textContent (never innerHTML): task, mail and meeting text is untrusted.
(() => {
  const CC = window.CC;
  if (!CC) return;
  const { el, api, guarded } = CC;

  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = "/static/daily.css";
  document.head.append(link);

  const OUTLOOK_ID = /^[0-9A-Fa-f]{16,1024}$/;
  const START_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/;
  const pad = (n) => String(n).padStart(2, "0");
  const isoDay = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const dayDate = (iso) => new Date(`${iso}T00:00`);
  const dayLabel = (iso) => dayDate(iso).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
  const clock = (iso) => new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const plural = (n, word, many = `${word}s`) => `${n} ${n === 1 ? word : many}`;
  const dialog = () => document.querySelector("#dialog");
  const closeRow = (...extra) => el("div", { class: "row" }, ...extra, el("button", { value: "close" }, "Close"));

  function previousWorkday(d = new Date()) {
    const x = new Date(d.getFullYear(), d.getMonth(), d.getDate() - 1);
    while (x.getDay() === 0 || x.getDay() === 6) x.setDate(x.getDate() - 1);
    return isoDay(x);
  }

  // Clipboard with a fallback for when the async clipboard is unavailable or refused.
  async function copyText(text, host) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      const ta = el("textarea", { class: "dl-copy-buffer", readonly: true, "aria-hidden": "true" });
      ta.value = text;
      (host || document.body).append(ta);  // inside the open dialog: the rest of the page is inert while it's modal
      ta.select();
      let ok = false;
      try { ok = document.execCommand("copy"); } catch { ok = false; }
      ta.remove();
      return ok;
    }
  }

  const state = { day: null, items: [null, null, null], carry: null, uncommitted: null, uncommittedAt: 0, noteKeys: new Set() };
  let strip = null;

  // ---- Top 3 -----------------------------------------------------------------------------------

  function openLink(lnk) {
    if (!lnk) return;
    if (lnk.kind === "mail") return CC.showMail(lnk.id);
    if (lnk.source === "asana") {
      const t = (CC.todayData?.tasks?.items || []).find((x) => x.source === "asana" && x.id === lnk.id);
      const url = t?.url || lnk.url;
      return url ? CC.openExternal(url) : CC.showMessage("Top 3", "That Asana task isn't in your open tasks any more.");
    }
    return guarded(() => api("/api/outlook/open", { id: lnk.id }));
  }

  async function saveTop3(items) {
    const r = await api("/api/daily/top3", { items });
    applyTop3(r);
  }

  function applyTop3(r) {
    state.day = r.day;
    state.items = (r.items || []).slice(0, 3);
    while (state.items.length < 3) state.items.push(null);
    state.carry = r.carry || null;
    render();
  }

  async function loadTop3() {
    try { applyTop3(await api("/api/daily/top3")); } catch { /* server restarting: keep what's shown */ }
  }

  function toggle(i) {
    const items = state.items.map((x) => (x ? { ...x } : null));
    if (!items[i]) return;
    items[i].done = !items[i].done;
    guarded(() => saveTop3(items));
  }

  function slotNode(item, i) {
    if (!item) {
      return el("button", { type: "button", class: "t3-slot t3-empty", title: "Set this priority", onclick: () => top3Dialog(i) },
        el("span", { class: "t3-n" }, i + 1), el("span", { class: "t3-text" }, "Set a priority"));
    }
    const textAttrs = { class: "t3-text", title: item.link ? `${item.text} (open the linked ${item.link.kind === "mail" ? "email" : "task"})` : item.text };
    return el("div", { class: `t3-slot ${item.done ? "done" : ""}` },
      el("button", { type: "button", class: `check t3-check ${item.done ? "on" : ""}`, "aria-pressed": String(!!item.done),
        title: item.done ? "Mark not done" : "Mark done", "aria-label": `${item.done ? "Reopen" : "Done"}: ${item.text}`,
        disabled: item.locked ? true : null, onclick: () => toggle(i) }, item.done ? "✓" : ""),
      el("span", { class: "t3-n" }, i + 1),
      item.link ? el("button", { type: "button", ...textAttrs, onclick: () => openLink(item.link) }, item.text)
                : el("span", textAttrs, item.text),
      item.link && el("span", { class: "t3-linked", "aria-hidden": "true", title: "Linked" }, item.link.kind === "mail" ? "✉" : "☐"),
      item.from && el("span", { class: "t3-carried", title: `Carried over from ${dayLabel(item.from)}` }, "↻"),
      el("button", { type: "button", class: "icon subtle t3-edit", title: "Edit Top 3", "aria-label": `Edit priority ${i + 1}`,
        onclick: () => top3Dialog(i) }, "✎"));
  }

  function carryRow() {
    const c = state.carry;
    if (!c || !c.items?.length) return null;
    const free = state.items.filter((x) => !x).length;
    return el("div", { class: "t3-carry", role: "group", "aria-label": "Carry over unfinished priorities" },
      el("span", { class: "t3-carry-label" }, `↻ ${plural(c.items.length, "unfinished priority", "unfinished priorities")} from ${dayLabel(c.from)}:`),
      el("span", { class: "t3-carry-list" }, c.items.map((x) => x.text).join(" · ")),
      el("span", { class: "spacer" }),
      el("button", { type: "button", class: "small alt", title: free < c.items.length ? `Room for ${free} today` : "",
        onclick: () => guarded(async () => applyTop3(await api("/api/daily/top3/carry", { accept: true }))) }, "Carry over"),
      el("button", { type: "button", class: "small subtle",
        onclick: () => guarded(async () => applyTop3(await api("/api/daily/top3/carry", { accept: false }))) }, "Not today"));
  }

  function uncommittedChip() {
    const u = state.uncommitted;
    if (!u || !u.show || !u.projects?.length) return null;
    const n = u.projects.length;
    return el("button", { type: "button", class: "small t3-warn", title: u.projects.map((p) => p.name).join(", "),
      onclick: uncommittedDialog }, `⚠ ${plural(n, "project")} with uncommitted work`);
  }

  function render() {
    if (!strip) return;
    const set = state.items.filter(Boolean);
    const done = set.filter((x) => x.done).length;
    strip.replaceChildren(
      el("div", { class: "t3-row" },
        el("div", { class: "t3-label" },
          el("span", { class: "panel-label" }, "TOP 3"),
          el("span", { class: "chip t3-count" }, set.length ? `${done}/${set.length} DONE` : "NOT SET")),
        el("div", { class: "t3-slots" }, state.items.map(slotNode)),
        el("div", { class: "t3-tools" },
          uncommittedChip(),
          el("button", { type: "button", class: "small subtle", title: "What you did on the last workday, ready for stand-up", onclick: () => recapDialog() }, "Yesterday"),
          el("button", { type: "button", class: "small subtle", title: "Private meeting notes and action items", onclick: notesListDialog }, "Notes"))),
      carryRow() || "");
  }

  function linkChoices() {
    const t = CC.todayData;
    const out = [];
    for (const task of t?.tasks?.items || []) {
      out.push({ group: "Tasks", label: task.subject, link: { kind: "task", source: task.source === "asana" ? "asana" : "outlook", id: task.id, label: task.subject, url: task.url || undefined } });
    }
    const f = t?.followups && !t.followups.error ? t.followups : {};
    const seen = new Set();
    for (const m of [...(f.urgent_reply || []), ...(f.needs_reply || []), ...(f.flagged || []), ...(f.awaiting_reply || [])]) {
      if (!m || seen.has(m.id)) continue;
      seen.add(m.id);
      out.push({ group: "Emails", label: `${m.subject}${m.sender_name ? ` (${m.sender_name})` : m.to ? ` (to ${m.to})` : ""}`, link: { kind: "mail", source: "outlook", id: m.id, label: m.subject } });
    }
    return out;
  }

  function top3Dialog(focus = 0) {
    const choices = linkChoices();
    const err = el("p", { class: "err", hidden: true });
    const rows = [0, 1, 2].map((i) => {
      const it = state.items[i] || {};
      const text = el("input", { id: `t3-text-${i}`, maxlength: "200", value: it.text || "", placeholder: ["Most important thing today", "Second priority", "Third priority"][i] });
      const sel = el("select", { id: `t3-link-${i}`, "aria-label": `Link for priority ${i + 1}` }, el("option", { value: "" }, "No link"));
      const map = new Map();
      if (it.link && !choices.some((c) => c.link.kind === it.link.kind && c.link.id === it.link.id)) {
        map.set("current", it.link);
        sel.append(el("option", { value: "current" }, `Linked: ${it.link.label || (it.link.kind === "mail" ? "email" : "task")}`));
      }
      for (const group of ["Tasks", "Emails"]) {
        const opts = choices.filter((c) => c.group === group);
        if (!opts.length) continue;
        sel.append(el("optgroup", { label: group }, opts.map((c, j) => {
          const key = `${group}:${j}`;
          map.set(key, c.link);
          return el("option", { value: key }, c.label.length > 90 ? `${c.label.slice(0, 88)}…` : c.label);
        })));
      }
      if (it.link) {
        const hit = [...map.entries()].find(([, l]) => l.kind === it.link.kind && l.id === it.link.id);
        if (hit) sel.value = hit[0];
      }
      sel.addEventListener("change", () => { const l = map.get(sel.value); if (l && !text.value.trim()) text.value = l.label.slice(0, 200); });
      const done = el("input", { type: "checkbox", id: `t3-done-${i}` });
      done.checked = !!it.done;
      return { i, it, text, sel, map, done };
    });
    const save = () => guarded(async () => {
      err.hidden = true;
      const items = rows.map((r) => r.text.value.trim() ? {
        text: r.text.value.trim(), done: r.done.checked, link: r.map.get(r.sel.value) || null, from: r.it.text ? r.it.from || null : null,
      } : null);
      try { await saveTop3(items); } catch (e) { err.textContent = e.message; err.hidden = false; return; }
      dialog().close();
    });
    CC.showDialog(el("h3", {}, "Top 3 for today"),
      el("p", { class: "muted" }, "The three things that make today a win. Private: saved only on this PC, encrypted with your Windows account."),
      rows.map((r) => el("div", { class: "t3-edit-row" },
        el("label", { class: "field-label", for: `t3-text-${r.i}` }, `Priority ${r.i + 1}`),
        r.text,
        el("div", { class: "t3-edit-sub" }, r.sel, el("label", { class: "t3-done-label" }, r.done, "Done")))),
      err,
      el("div", { class: "row" },
        el("button", { type: "button", class: "subtle", onclick: () => rows.forEach((r) => { r.text.value = ""; r.sel.value = ""; r.done.checked = false; }) }, "Clear all"),
        el("button", { value: "cancel", class: "subtle" }, "Cancel"),
        el("button", { type: "button", class: "primary", onclick: save }, "Save")));
    rows[Math.max(0, Math.min(2, focus))].text.focus();
  }

  // ---- uncommitted work ---------------------------------------------------------------------------

  async function loadUncommitted(refresh = false) {
    try {
      state.uncommitted = await api(`/api/daily/uncommitted${refresh ? "?refresh=true" : ""}`);
      state.uncommittedAt = Date.now();
      render();
    } catch { /* keep the last result */ }
  }

  const describe = (p) => [p.changed_files ? plural(p.changed_files, "changed file") : "",
    p.ahead ? `${plural(p.ahead, "commit")} not pushed` : ""].filter(Boolean).join(" · ");

  async function uncommittedDialog() {
    CC.showDialog(el("h3", {}, "Uncommitted work"), el("p", { class: "muted" }, "Checking your projects…"));
    let r;
    try { r = await api("/api/daily/uncommitted?refresh=true"); } catch (e) {
      CC.showDialog(el("h3", {}, "Uncommitted work"), el("p", { class: "err" }, e.message), closeRow());
      return;
    }
    state.uncommitted = r; state.uncommittedAt = Date.now(); render();
    if (!dialog().open) return;
    const openIn = (p) => { dialog().close(); CC.setActiveProject(p.path); CC.openTab("projects"); };
    CC.showDialog(el("h3", {}, "Uncommitted work"),
      el("p", { class: "muted" }, "Projects with changes that aren't committed, or commits that aren't pushed to their upstream branch."),
      r.projects.length ? el("ul", { class: "list dialog-list" }, r.projects.map((p) => el("li", {},
        el("div", { class: "grow" },
          el("div", { class: "title" }, p.name, p.branch && el("span", { class: "tag" }, p.branch)),
          el("div", { class: "muted" }, describe(p))),
        el("div", { class: "actions" }, el("button", { type: "button", class: "small alt", onclick: () => openIn(p) }, "Open in Claude")))))
        : el("p", { class: "good" }, "Everything is committed and pushed."),
      el("p", { class: "muted" }, `Checked ${CC.when(r.checked)}`),
      closeRow());
  }

  // ---- yesterday recap ----------------------------------------------------------------------------------

  function standupText(r) {
    const heading = r.day === previousWorkday() ? `Yesterday (${dayLabel(r.day)})` : dayLabel(r.day);
    const lines = [heading];
    for (const p of r.commits || []) {
      lines.push(`- ${p.project}: ${plural(p.commits.length, "commit")}`);
      for (const c of p.commits.slice(0, 8)) lines.push(`  - ${c.subject}`);
      if (p.commits.length > 8) lines.push(`  - and ${p.commits.length - 8} more`);
    }
    for (const t of r.tasks || []) lines.push(`- Done: ${t.subject}`);
    for (const t of r.pulse || []) lines.push(`- Closed Pulse #${t.ticket_no}: ${t.title}`);
    if (r.meetings?.length) lines.push(`- Meetings: ${r.meetings.map((m) => m.subject).join(", ")}`);
    if (r.sent?.count) lines.push(`- Sent ${plural(r.sent.count, "email")}`);
    if (r.followups_cleared) lines.push(`- Cleared ${plural(r.followups_cleared, "follow-up")}`);
    if (lines.length === 1) lines.push("- Nothing recorded");
    const top = state.items.filter(Boolean);
    if (top.length) {
      lines.push("", "Today");
      for (const t of top) lines.push(`- ${t.text}${t.done ? " (done)" : ""}`);
    }
    return lines.join("\n");
  }

  async function copyStandup() {
    try {
      const r = await api("/api/daily/recap");
      toast(await copyText(standupText(r)) ? "Stand-up copied" : "Couldn't copy: open Yesterday and copy the text there");
    } catch (e) { CC.showMessage("Stand-up", e.message); }
  }

  function toast(text) {
    let box = document.querySelector("#toast");
    if (!box) { box = el("div", { id: "toast", role: "status", "aria-live": "polite" }); document.body.append(box); }
    box.textContent = text;
    box.classList.add("show");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => box.classList.remove("show"), 3500);
  }

  let recapSeq = 0;
  async function recapDialog(day, refresh = false) {
    const seq = ++recapSeq;
    CC.wideDialog(el("h3", {}, "Yesterday"), el("p", { class: "muted" }, "Gathering commits, meetings and mail…"));
    let r;
    const query = [day && `day=${encodeURIComponent(day)}`, refresh && "refresh=true"].filter(Boolean).join("&");
    try { r = await api(`/api/daily/recap${query ? `?${query}` : ""}`); } catch (e) {
      if (seq === recapSeq) CC.wideDialog(el("h3", {}, "Yesterday"), el("p", { class: "err" }, e.message), closeRow());
      return;
    }
    if (seq !== recapSeq || !dialog().open) return;
    const shift = (n) => { const d = dayDate(r.day); d.setDate(d.getDate() + n); return isoDay(d); };
    const today = isoDay(new Date());
    const picker = el("input", { type: "date", value: r.day, max: today, "aria-label": "Recap day", class: "dl-date" });
    picker.addEventListener("change", () => { if (picker.value) recapDialog(picker.value); });
    const errs = r.errors || {};
    const sec = (title, count, body, err) => el("section", { class: "dl-sec" },
      el("h4", {}, title, count !== null && el("span", { class: "chip" }, count)),
      err ? el("p", { class: "err" }, err) : body);
    const empty = (text) => el("p", { class: "muted" }, text);
    const standup = standupText(r);
    const preview = el("textarea", { class: "dl-standup", readonly: true, rows: "8", "aria-label": "Stand-up text" });
    preview.value = standup;
    const status = el("span", { class: "muted", role: "status" });
    const nCommits = (r.commits || []).reduce((n, p) => n + p.commits.length, 0);

    CC.wideDialog(
      el("div", { class: "dl-head" },
        el("div", { class: "grow" },
          el("div", { class: "panel-label" }, r.day === previousWorkday() ? "YESTERDAY // LAST WORKDAY" : "RECAP"),
          el("h3", { class: "dl-title" }, dayDate(r.day).toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" }))),
        el("div", { class: "actions" },
          el("button", { type: "button", class: "small subtle", "aria-label": "Previous day", onclick: () => recapDialog(shift(-1)) }, "◀"),
          picker,
          el("button", { type: "button", class: "small subtle", "aria-label": "Next day", disabled: r.day >= today ? true : null, onclick: () => recapDialog(shift(1)) }, "▶"))),
      el("div", { class: "dl-grid" },
        el("div", {},
          sec("Commits", nCommits, (r.commits || []).length ? el("div", {}, r.commits.map((p) => el("div", { class: "dl-proj" },
            el("div", { class: "dl-proj-name" }, p.project, el("span", { class: "muted" }, ` · ${plural(p.commits.length, "commit")}`)),
            el("ul", { class: "list compact" }, p.commits.map((c) => el("li", {},
              el("span", { class: "mono dl-sha" }, c.sha), el("span", { class: "grow" }, c.subject), el("span", { class: "muted" }, c.at ? clock(c.at) : "")))))))
            : empty("No commits by you that day."), errs.commits),
          sec("Done", (r.tasks || []).length + (r.pulse || []).length,
            (r.tasks || []).length || (r.pulse || []).length || r.followups_cleared ? el("ul", { class: "list compact" },
              (r.tasks || []).map((t) => el("li", {}, CC.srcTag(t.source === "asana" ? "asana" : "todo"), el("span", { class: "grow" }, t.subject))),
              (r.pulse || []).map((t) => el("li", {}, CC.srcTag("pulse"), el("span", { class: "grow" }, `#${t.ticket_no} ${t.title}`))),
              r.followups_cleared ? el("li", { class: "muted" }, `Cleared ${plural(r.followups_cleared, "follow-up")}`) : null)
              : empty("No completed tasks or closed tickets recorded."),
            [errs.tasks && `Outlook tasks: ${errs.tasks}`, errs.asana && `Asana: ${errs.asana}`, errs.pulse && `Pulse: ${errs.pulse}`].filter(Boolean).join(" · ") || null)),
        el("div", {},
          sec("Meetings", (r.meetings || []).length, (r.meetings || []).length ? el("ul", { class: "list compact" }, r.meetings.map((m) => el("li", {},
            el("span", { class: "mono dl-time" }, clock(m.start)), el("span", { class: "grow" }, m.subject),
            el("button", { type: "button", class: `small subtle cc-notes-btn ${m.has_notes ? "has" : ""}`, onclick: () => notesDialog(m) }, m.has_notes ? "Notes ✓" : "Notes"))))
            : empty("No meetings that day."), errs.meetings),
          sec("Emails sent", r.sent?.count ?? 0, r.sent?.count ? el("details", { class: "dl-sent" },
            el("summary", {}, `Show ${plural(r.sent.count, "subject")}`),
            el("ul", { class: "list compact" }, r.sent.items.map((m) => el("li", {},
              el("span", { class: "mono dl-time" }, m.at ? clock(m.at) : ""), el("span", { class: "grow" }, m.subject, el("span", { class: "muted" }, m.to ? ` → ${m.to}` : ""))))))
            : empty("No mail sent that day."), errs.sent))),
      el("details", { class: "dl-standup-wrap" }, el("summary", {}, "Stand-up text"), preview),
      el("div", { class: "row" }, status,
        el("button", { type: "button", class: "subtle", onclick: () => recapDialog(r.day, true) }, "↻ Refresh"),
        el("button", { value: "close", class: "subtle" }, "Close"),
        el("button", { type: "button", class: "primary", onclick: async () => {
          const ok = await copyText(standup, dialog());
          status.textContent = ok ? "Copied to the clipboard." : "Couldn't copy: select the stand-up text and copy it.";
          if (!ok) { preview.closest("details").open = true; preview.select(); }
        } }, "Copy for stand-up")));
  }

  // ---- meeting notes ---------------------------------------------------------------------------------

  const noteKey = (id, start) => `${id}|${start}`;

  async function loadNoteKeys() {
    try {
      const r = await api("/api/daily/notes?q=");
      state.noteKeys = new Set((r.keys || []).map((k) => noteKey(k.id, k.start)));
      markNoteButtons();
    } catch { /* keep */ }
  }

  function markNoteButtons() {
    for (const b of document.querySelectorAll("button.cc-notes-btn[data-mid]")) {
      const has = state.noteKeys.has(noteKey(b.dataset.mid, b.dataset.start));
      b.classList.toggle("has", has);
      b.textContent = has ? "Notes ✓" : "Notes";
    }
  }

  CC.addMeetingAction((m) => {
    if (!m || !OUTLOOK_ID.test(m.id || "") || !START_RE.test(m.start || "")) return null;
    const has = state.noteKeys.has(noteKey(m.id, m.start));
    return el("button", { type: "button", class: `small subtle cc-notes-btn ${has ? "has" : ""}`, "data-mid": m.id, "data-start": m.start,
      title: "Private notes and action items (only on this PC)", onclick: () => notesDialog(m) }, has ? "Notes ✓" : "Notes");
  });

  let notesSeq = 0;
  async function notesDialog(m) {
    if (!m || !OUTLOOK_ID.test(m.id || "") || !START_RE.test(m.start || "")) {
      CC.showMessage("Meeting notes", "This meeting has no start time to file notes under.");
      return;
    }
    const seq = ++notesSeq;
    CC.wideDialog(el("h3", {}, "Meeting notes"), el("p", { class: "muted" }, "Opening…"));
    let n;
    try { n = await api(`/api/daily/notes/item?id=${encodeURIComponent(m.id)}&start=${encodeURIComponent(m.start)}`); } catch (e) {
      if (seq === notesSeq) CC.wideDialog(el("h3", {}, "Meeting notes"), el("p", { class: "err" }, e.message), closeRow());
      return;
    }
    if (seq !== notesSeq || !dialog().open) return;
    if (n.locked) {
      CC.wideDialog(el("h3", {}, "Meeting notes"), el("p", { class: "err" }, "These notes were saved under another Windows account and can't be opened here."), closeRow());
      return;
    }
    const st = { subject: n.subject || m.subject || "(meeting)", end: n.end || m.end || null, actions: (n.actions || []).map((a) => ({ ...a })),
      exists: !!n.exists, dirty: false, timer: null, gone: false };
    const asanaOn = !!CC.todayData?.tasks?.asana?.connected;
    const notes = el("textarea", { class: "mn-notes", maxlength: "20000", "aria-label": "Notes",
      placeholder: "Decisions, context, who said what…" });
    notes.value = n.notes || "";
    const status = el("span", { class: "muted mn-status", role: "status" }, st.exists ? `Saved ${CC.when(n.updated)}` : "Nothing saved yet");
    const list = el("ul", { class: "list mn-actions" });
    const newItem = el("input", { maxlength: "300", placeholder: "Add an action item, then Enter", "aria-label": "New action item" });
    const due = el("input", { type: "date", id: "mn-due", "aria-label": "Due date for tasks you create" });

    const payload = () => ({ id: m.id, start: m.start, end: st.end, subject: st.subject, notes: notes.value, actions: st.actions });
    async function save() {
      const r = await api("/api/daily/notes", payload());
      st.exists = r.exists;
      status.classList.remove("err");
      status.textContent = r.exists ? `Saved ${CC.when(r.updated)}` : "Nothing to save";
      const k = noteKey(m.id, m.start);
      r.exists ? state.noteKeys.add(k) : state.noteKeys.delete(k);
      markNoteButtons();
    }
    async function flush() {
      clearTimeout(st.timer);
      if (!st.dirty || st.gone) return;
      st.dirty = false;
      try { await save(); } catch (e) { st.dirty = true; status.textContent = `Not saved: ${e.message}`; status.classList.add("err"); throw e; }
    }
    const changed = () => {
      st.dirty = true;
      status.classList.remove("err");
      status.textContent = "Saving…";
      clearTimeout(st.timer);
      st.timer = setTimeout(() => flush().catch(() => {}), 1000);
    };
    notes.addEventListener("input", changed);

    async function toTask(i, source) {
      try {
        st.dirty = true;
        await flush();
        const r = await api("/api/daily/notes/task", { id: m.id, start: m.start, index: i, source, due: due.value || null });
        st.actions = r.actions;
        renderActions();
        status.textContent = `Task created in ${source === "asana" ? "Asana" : "Outlook / To Do"}`;
        CC.loaders.today?.();
      } catch (e) { status.textContent = e.message; status.classList.add("err"); }
    }

    function renderActions() {
      CC.fill(list, st.actions.map((a, i) => el("li", { class: a.done ? "done" : "" },
        el("button", { type: "button", class: `check mn-check ${a.done ? "on" : ""}`, "aria-pressed": String(!!a.done),
          "aria-label": `${a.done ? "Reopen" : "Done"}: ${a.text}`, onclick: () => { a.done = !a.done; renderActions(); changed(); } }, a.done ? "✓" : ""),
        el("div", { class: "grow" },
          el("div", { class: "title" }, a.text),
          a.task && el("div", { class: "muted src-line" }, CC.srcTag(a.task.source === "asana" ? "asana" : "todo"), "Task created")),
        el("div", { class: "actions" },
          !a.task && el("button", { type: "button", class: "small", title: "Create an Outlook / To Do task", onclick: () => toTask(i, "outlook") }, "→ To Do"),
          !a.task && asanaOn && el("button", { type: "button", class: "small subtle", title: "Create an Asana task", onclick: () => toTask(i, "asana") }, "→ Asana"),
          a.task?.source === "outlook" && a.task.id && el("button", { type: "button", class: "small subtle", onclick: () => guarded(() => api("/api/outlook/open", { id: a.task.id })) }, "Open"),
          el("button", { type: "button", class: "small subtle", title: "Remove from these notes", "aria-label": `Remove: ${a.text}`,
            onclick: () => { st.actions.splice(i, 1); renderActions(); changed(); } }, "✕")))),
        "No action items yet.");
    }
    const add = () => {
      const text = newItem.value.trim();
      if (!text) return;
      if (st.actions.length >= 40) { status.textContent = "Up to 40 action items"; return; }
      st.actions.push({ text: text.slice(0, 300), done: false, task: null });
      newItem.value = "";
      renderActions();
      changed();
      newItem.focus();
    };
    const remove = async () => {
      if (!await CC.confirmDialog("Delete notes", `Delete your notes and action items for "${st.subject}"? Tasks you already created stay.`, "Delete")) {
        notesDialog(m);
        return;
      }
      st.gone = true;
      clearTimeout(st.timer);
      await guarded(async () => {
        await api("/api/daily/notes/delete", { id: m.id, start: m.start });
        state.noteKeys.delete(noteKey(m.id, m.start));
        markNoteButtons();
      });
    };
    renderActions();
    const when = new Date(m.start);
    CC.wideDialog(
      el("div", { class: "mn-head" },
        el("div", { class: "grow" },
          el("div", { class: "panel-label" }, "MEETING NOTES // PRIVATE"),
          el("h3", { class: "mn-title" }, st.subject),
          el("div", { class: "muted" }, `${when.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })} · ${clock(m.start)}${st.end ? ` – ${clock(st.end)}` : ""}`)),
        el("span", { class: "src src-personal", title: "Only on this PC, encrypted with your Windows account, never written to Outlook" }, "Private")),
      el("div", { class: "mn-grid" },
        el("section", { class: "mn-col" }, el("label", { class: "field-label" }, "Notes"), notes),
        el("section", { class: "mn-col" },
          el("div", { class: "field-label" }, "Action items"),
          el("div", { class: "mn-add" }, newItem, el("button", { type: "button", class: "primary small", onclick: add }, "Add")),
          list,
          el("div", { class: "mn-due" }, el("label", { class: "field-label", for: "mn-due" }, "Due date for new tasks (optional)"), due))),
      el("div", { class: "row" },
        status, el("span", { class: "spacer" }),
        st.exists && el("button", { type: "button", class: "subtle", onclick: remove }, "Delete notes"),
        el("button", { value: "close", class: "subtle" }, "Close"),
        el("button", { type: "button", class: "primary", onclick: async () => {
          st.dirty = true;
          try { await flush(); dialog().close(); } catch { /* the status line says why */ }
        } }, "Save")));
    // Closing the dialog (Close, Esc) saves anything still pending.
    dialog().addEventListener("close", () => { flush().catch(() => {}); }, { once: true });
    (notes.value ? newItem : notes).focus();
  }

  async function notesListDialog() {
    const q = el("input", { placeholder: "Search by meeting subject or note text…", "aria-label": "Search meeting notes", maxlength: "100" });
    const list = el("ul", { class: "list dialog-list mn-list" }, el("li", { class: "empty" }, "Loading…"));
    let seq = 0, timer = null;
    const load = async () => {
      const mine = ++seq;
      try {
        const r = await api(`/api/daily/notes?q=${encodeURIComponent(q.value.trim())}`);
        if (mine !== seq) return;
        state.noteKeys = new Set((r.keys || []).map((k) => noteKey(k.id, k.start)));
        CC.fill(list, r.notes.map((n) => el("li", { class: "clickable", tabindex: "0", role: "button",
            onclick: () => notesDialog(n), onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); notesDialog(n); } } },
          el("div", { class: "time-col" }, dayLabel(n.start.slice(0, 10)), el("div", { class: "muted" }, clock(n.start))),
          el("div", { class: "grow" },
            el("div", { class: "title" }, n.subject),
            n.preview && el("div", { class: "muted mn-preview" }, n.preview),
            n.actions ? el("div", { class: "muted" }, `${plural(n.actions, "action item")}${n.open ? ` · ${n.open} open` : " · all done"}`) : null))),
          q.value.trim() ? "No notes match." : "No meeting notes yet. Use Notes on a meeting in your agenda or calendar.");
      } catch (e) { if (mine === seq) CC.fill(list, [el("li", { class: "err" }, e.message)], ""); }
    };
    q.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(load, 200); });
    CC.wideDialog(el("h3", {}, "Meeting notes"),
      el("p", { class: "muted" }, "Private notes pads: only on this PC, encrypted with your Windows account, never written to Outlook."),
      q, list, closeRow());
    q.focus();
    load();
  }

  // ---- alerts, palette, wiring --------------------------------------------------------------------------

  CC.registerAlertAction("meeting-notes", (act) => {
    if (!OUTLOOK_ID.test(act.id || "") || !START_RE.test(act.start || "")) return null;
    return el("button", { class: "small alt", onclick: () => notesDialog({ id: act.id, start: act.start, end: START_RE.test(act.end || "") ? act.end : null }) }, "Capture notes");
  });
  CC.registerAlertAction("uncommitted", () => [
    el("button", { class: "small alt", onclick: () => CC.openTab("projects") }, "Open Claude"),
    el("button", { class: "small", onclick: uncommittedDialog }, "Details"),
  ]);

  CC.addPaletteSource(() => {
    const out = [
      { group: "Commands", label: "Set Top 3 for today", hint: "today's priorities", run: () => top3Dialog(Math.max(0, state.items.findIndex((x) => !x))) },
      { group: "Commands", label: "Yesterday recap", hint: "commits, meetings, mail, closed work", run: () => recapDialog() },
      { group: "Commands", label: "Copy stand-up for yesterday", hint: "plain-text bullets", run: copyStandup },
      { group: "Commands", label: "Meeting notes", hint: "private notes and action items", run: notesListDialog },
      { group: "Commands", label: "Uncommitted work", hint: "projects not committed or pushed", run: uncommittedDialog },
    ];
    state.items.forEach((t, i) => {
      if (t && !t.locked) out.push({ group: "Top 3", label: `${t.done ? "Reopen" : "Done"}: ${t.text}`, hint: `priority ${i + 1}`, run: () => toggle(i) });
    });
    if (state.carry) out.push({ group: "Top 3", label: "Carry over unfinished priorities", hint: dayLabel(state.carry.from),
      run: () => guarded(async () => applyTop3(await api("/api/daily/top3/carry", { accept: true }))) });
    const agenda = Array.isArray(CC.todayData?.agenda) ? CC.todayData.agenda : [];
    for (const m of agenda) {
      if (!m.all_day && START_RE.test(m.start || "")) out.push({ group: "Agenda", label: `Notes: ${m.subject}`, hint: clock(m.start), run: () => notesDialog(m) });
    }
    return out;
  });

  function mount() {
    const host = document.querySelector("#tab-today .a-strip");
    if (!host || document.querySelector("#daily-strip")) return;
    strip = el("div", { class: "panel a-daily", id: "daily-strip", role: "region", "aria-label": "Top 3 priorities for today" });
    host.append(strip);
    render();
    loadTop3();
    loadNoteKeys();
    loadUncommitted();
  }

  document.addEventListener("cc:today", () => {
    if (!strip) mount();
    loadTop3();
    markNoteButtons();
    if (Date.now() - state.uncommittedAt > 5 * 60000) loadUncommitted();
  });
  setInterval(() => loadUncommitted(), 10 * 60000);
  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", mount, { once: true }) : mount();
})();
