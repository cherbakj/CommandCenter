// Workday helpers on Today: the uncommitted-work tile (in the stats strip), the "Yesterday" recap with a stand-up
// copy, and meeting notes with action items (Yesterday / Notes buttons on the Agenda tab). Notes are work data,
// DPAPI-encrypted on this PC (labelled "encrypted", not "private"), so they stay visible in Privacy mode.
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
  // A dialog that loads: its first view carries a marker, and the result replaces it only while that view is still
  // the one showing (if Joe closed it or opened another dialog meanwhile, that one is left alone).
  let loadSeq = 0;
  const loading = () => String(++loadSeq);
  const stillShowing = (mark) => dialog().open && !!dialog().querySelector(`[data-loading="${mark}"]`);

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

  const state = { uncommitted: null, uncommittedAt: 0, noteKeys: new Set() };
  let tools = null;

  // ---- on Today ------------------------------------------------------------------------------------
  // Projects with uncommitted / unpushed work: a stat tile (only while there are some, after daily.uncommitted_after).
  CC.addStatTile?.(() => {
    const u = state.uncommitted;
    if (!u || !u.show || !u.projects?.length) return null;
    return CC.statTile(u.projects.length, "Uncommitted projects", "var(--amber)", {
      title: `Uncommitted or unpushed: ${u.projects.map((p) => p.name).join(", ")}`, onclick: uncommittedDialog });
  });
  const render = () => CC.refreshStats?.();

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
    const mark = loading();
    CC.showDialog(el("h3", { "data-loading": mark }, "Uncommitted work"), el("p", { class: "muted" }, "Checking your projects…"));
    let r;
    try { r = await api("/api/daily/uncommitted?refresh=true"); } catch (e) {
      if (stillShowing(mark)) CC.showDialog(el("h3", {}, "Uncommitted work"), el("p", { class: "err" }, e.message), closeRow());
      return;
    }
    state.uncommitted = r; state.uncommittedAt = Date.now(); render();
    if (!stillShowing(mark)) return;
    const openIn = (p) => { dialog().close(); CC.setActiveProject(p.path); CC.openTab("vscode"); };
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
    const seq = ++recapSeq, mark = loading();
    CC.wideDialog(el("h3", { "data-loading": mark }, "Yesterday"), el("p", { class: "muted" }, "Gathering commits, meetings and mail…"));
    let r;
    const query = [day && `day=${encodeURIComponent(day)}`, refresh && "refresh=true"].filter(Boolean).join("&");
    try { r = await api(`/api/daily/recap${query ? `?${query}` : ""}`); } catch (e) {
      if (seq === recapSeq && stillShowing(mark)) CC.wideDialog(el("h3", {}, "Yesterday"), el("p", { class: "err" }, e.message), closeRow());
      return;
    }
    if (seq !== recapSeq || !stillShowing(mark)) return;
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
            el("button", { type: "button", class: `small subtle cc-notes-btn ${m.has_notes ? "has" : ""}`, "data-mid": m.id, "data-start": m.start,
              onclick: () => notesDialog(m) }, m.has_notes ? "Notes ✓" : "Notes"))))
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
      title: "Meeting notes and action items (encrypted, only on this PC)", onclick: () => notesDialog(m) }, has ? "Notes ✓" : "Notes");
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
    const delBtn = el("button", { type: "button", class: "subtle", hidden: !st.exists, onclick: () => remove() }, "Delete notes");
    async function save() {
      const r = await api("/api/daily/notes", payload());
      st.exists = r.exists;
      delBtn.hidden = !st.exists;  // shown from the first save, not only after reopening
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
          el("div", { class: "panel-label" }, "MEETING NOTES // ENCRYPTED"),
          el("h3", { class: "mn-title" }, st.subject),
          el("div", { class: "muted" }, `${when.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })} · ${clock(m.start)}${st.end ? ` – ${clock(st.end)}` : ""}`)),
        el("span", { class: "src mn-enc", title: "Work data: only on this PC, encrypted with your Windows account, never written to Outlook. Stays visible in Privacy mode." }, "Work · encrypted")),
      el("div", { class: "mn-grid" },
        el("section", { class: "mn-col" }, el("label", { class: "field-label" }, "Notes"), notes),
        el("section", { class: "mn-col" },
          el("div", { class: "field-label" }, "Action items"),
          el("div", { class: "mn-add" }, newItem, el("button", { type: "button", class: "primary small", onclick: add }, "Add")),
          list,
          el("div", { class: "mn-due" }, el("label", { class: "field-label", for: "mn-due" }, "Due date for new tasks (optional)"), due))),
      el("div", { class: "row" },
        status, el("span", { class: "spacer" }),
        delBtn,
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
      el("p", { class: "muted" }, "Meeting notes pads (work data): only on this PC, encrypted with your Windows account, never written to Outlook."),
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
    el("button", { class: "small alt", onclick: () => CC.openTab("vscode") }, "Open Claude"),
    el("button", { class: "small", onclick: uncommittedDialog }, "Details"),
  ]);

  CC.addPaletteSource(() => {
    const out = [
      { group: "Commands", label: "Yesterday recap", hint: "commits, meetings, mail, closed work", run: () => recapDialog() },
      { group: "Commands", label: "Copy stand-up for yesterday", hint: "plain-text bullets", run: copyStandup },
      { group: "Commands", label: "Meeting notes", hint: "notes and action items, encrypted on this PC", run: notesListDialog },
      { group: "Commands", label: "Uncommitted work", hint: "projects not committed or pushed", run: uncommittedDialog },
    ];
    const agenda = Array.isArray(CC.todayData?.agenda) ? CC.todayData.agenda : [];
    for (const m of agenda) {
      if (!m.all_day && START_RE.test(m.start || "")) out.push({ group: "Agenda", label: `Notes: ${m.subject}`, hint: clock(m.start), run: () => notesDialog(m) });
    }
    return out;
  });

  function mount() {
    const host = document.querySelector("#agenda-tools-slot");
    if (!host || tools) return;
    tools = [
      el("button", { type: "button", class: "small subtle", title: "What you did on the last workday, ready for stand-up", onclick: () => recapDialog() }, "Yesterday"),
      el("button", { type: "button", class: "small subtle", title: "Meeting notes and action items (encrypted on this PC)", onclick: notesListDialog }, "Notes"),
    ];
    host.append(...tools);
    loadNoteKeys();
    loadUncommitted();
  }

  document.addEventListener("cc:today", () => {
    if (!tools) mount();
    markNoteButtons();
    if (Date.now() - state.uncommittedAt > 5 * 60000) loadUncommitted();
  });
  setInterval(() => loadUncommitted(), 10 * 60000);
  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", mount, { once: true }) : mount();
})();
