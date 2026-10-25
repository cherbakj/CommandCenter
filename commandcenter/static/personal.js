// Personal tab (COMMS) and personal reminder pop-ups (reminders.py).
// The tab: today's reminders as a checklist, every reminder (add / edit / delete), the next 7 days of personal
// calendar entries and recently saved personal entries (planner.py's vault records, read, not copied).
// Personal text comes back from the server only while the personal vault is unlocked and Privacy mode is off;
// every node holding it carries data-personal, so privacy.js removes it when the page's session ends (401).
// The pop-up: its own modal <dialog> that stays until each reminder is Done (or snoozed). Esc and clicks outside
// don't close it; another dialog can open over it, and it is back on top when that closes. Done and Snooze are
// stored server-side, so a reload or restart shows a not-done reminder again. While the vault is locked (or in
// Privacy mode) it says only "Personal reminder at 3:00 PM", with Unlock to see the text; Done works without it.
// Everything is inserted as text.
(() => {
  const CC = window.CC;
  if (!CC) return;
  const { el, api, guarded } = CC;

  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = "/static/personal.css";
  document.head.append(link);

  const DAYS = [["mon", "Mon"], ["tue", "Tue"], ["wed", "Wed"], ["thu", "Thu"], ["fri", "Fri"], ["sat", "Sat"], ["sun", "Sun"]];
  const REPEATS = [["weekdays", "Weekdays"], ["daily", "Every day"], ["days", "Days…"], ["once", "Once"]];
  const SNOOZE_MORE = [5, 30, 60];
  const pad = (n) => String(n).padStart(2, "0");
  const isoDay = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const hhmm = (t) => (t ? new Date(`2000-01-01T${t}`).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "");
  const clock = (iso) => new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const shortDay = (iso) => new Date(`${String(iso).slice(0, 10)}T00:00`).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
  const nextLabel = (iso) => {
    if (!iso) return "";
    const d = new Date(iso), now = new Date();
    const tomorrow = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
    const day = d.toDateString() === now.toDateString() ? "today" : d.toDateString() === tomorrow.toDateString() ? "tomorrow" : shortDay(iso);
    return `Next: ${day} ${clock(iso)}`;
  };
  const personalText = (tag, attrs, ...kids) => el(tag, { ...attrs, "data-personal": "" }, ...kids);
  let snoozeDefault = 15;

  // Runs fn once personal data is available (asks to set up / unlock the vault first). Privacy mode: asks first.
  async function withPersonal(fn, why) {
    if (!CC.privacy) return fn();
    if (CC.privacy.isPrivate()) {
      if (!await CC.confirmDialog("Privacy mode is on", "Personal items are hidden. Turn Privacy mode off and unlock your personal vault?", "Show personal")) return;
      await CC.privacy.setPrivacy(false);
    }
    if (!CC.privacy.available() && !await CC.privacy.ensureUnlocked(why)) return;
    return fn();
  }

  // ---- the tab ------------------------------------------------------------------------------------
  let section, body, data = null;

  function render(s) {
    section = s;
    body = el("div", { class: "pr-body" });
    s.append(
      el("div", { class: "toolbar" },
        el("span", { class: "panel-label" }, "PERSONAL"),
        el("span", { class: "muted pr-sub" }, "Only on this PC, in your personal vault"),
        el("span", { class: "spacer" }),
        el("button", { type: "button", class: "small primary pr-add", onclick: () => addReminder() }, "+ Reminder"),
        el("button", { type: "button", class: "small", onclick: () => load() }, "↻ Refresh")),
      body);
  }

  async function load() {
    if (!body) return;
    try { data = await api("/api/reminders"); } catch (e) {
      body.replaceChildren(el("div", { class: "panel err" }, e.message));
      return;
    }
    snoozeDefault = data.snooze_minutes || snoozeDefault;
    draw();
  }

  function lockedPanel(ps, counts) {
    const text = ps.privacy ? "Privacy mode is on: personal items are hidden."
      : !ps.set_up ? "Set up your personal vault to add personal reminders. Reminders, personal calendar entries and notes are encrypted with your own passphrase."
      : "Your personal vault is locked.";
    const label = ps.privacy ? "Show personal" : !ps.set_up ? "Set up personal vault" : "Unlock";
    return el("div", { class: "panel pr-locked" },
      el("div", { class: "pr-lock-ico", "aria-hidden": "true" }, "🔒"),
      el("p", {}, text),
      !ps.privacy && ps.set_up && counts && counts.today
        ? el("p", { class: "muted" }, `${counts.today} reminder${counts.today === 1 ? "" : "s"} today, ${counts.done} done. They still pop up while locked (without their text).`) : null,
      el("div", { class: "row" }, el("button", { type: "button", class: "primary",
        onclick: () => withPersonal(() => load(), "Unlock to see your personal reminders and notes.") }, label)));
  }

  function draw() {
    if (!body || !data) return;
    const ps = data.state || {};
    section.querySelector(".pr-add").hidden = !ps.available && !!ps.privacy;
    if (!ps.available) { body.replaceChildren(lockedPanel(ps, data.counts)); return; }
    body.replaceChildren(
      el("div", { class: "pr-grid" },
        el("div", { class: "panel pr-today" },
          el("div", { class: "panel-head" }, el("span", { class: "panel-label" }, "TODAY'S REMINDERS"),
            el("span", { class: "chip" }, `${data.today.filter((t) => t.done).length}/${data.today.length} done`)),
          el("ul", { class: "list pr-list" }, data.today.length ? data.today.map(todayRow)
            : [el("li", { class: "empty" }, data.reminders.length ? "No reminders today." : "No reminders yet. Add one with + Reminder.")])),
        el("div", { class: "panel pr-all" },
          el("div", { class: "panel-head" }, el("span", { class: "panel-label" }, "ALL REMINDERS"), el("span", { class: "chip" }, String(data.reminders.length))),
          el("ul", { class: "list pr-list" }, data.reminders.length ? data.reminders.map(reminderRow)
            : [el("li", { class: "empty" }, "None yet.")])),
        el("div", { class: "panel pr-upcoming" },
          el("div", { class: "panel-head" }, el("span", { class: "panel-label" }, "NEXT 7 DAYS"), el("span", { class: "spacer" }),
            el("button", { type: "button", class: "small subtle", onclick: () => CC.openTab("calendar") }, "Calendar")),
          el("ul", { class: "list pr-list" }, data.upcoming.length ? data.upcoming.map(entryRow)
            : [el("li", { class: "empty" }, "No personal calendar entries in the next 7 days.")])),
        el("div", { class: "panel pr-recent" },
          el("div", { class: "panel-head" }, el("span", { class: "panel-label" }, "RECENT NOTES"),
            el("span", { class: "muted pr-sub" }, "quick capture and personal entries, last 14 days")),
          el("ul", { class: "list pr-list" }, data.recent.length ? data.recent.map(recentRow)
            : [el("li", { class: "empty" }, "Nothing saved in the last 14 days. Quick capture (Ctrl+Alt+Space) → Personal.")]))));
  }

  function todayRow(t) {
    const box = el("input", { type: "checkbox", class: "pr-done", "aria-label": "Done" });
    box.checked = !!t.done;
    box.addEventListener("change", () => guarded(async () => {
      await api("/api/reminders/done", { id: t.id, day: isoDay(new Date()), done: box.checked });
      await load();
      poll();
    }));
    const snoozed = t.open && t.wake && t.wake * 1000 > Date.now();
    return el("li", { class: `pr-row ${t.done ? "done" : ""} ${t.overdue ? "overdue" : ""}` },
      el("label", { class: "pr-check" }, box),
      el("div", { class: "pr-time" }, hhmm(t.time)),
      el("div", { class: "grow" },
        personalText("div", { class: "title" }, t.text),
        t.note ? personalText("div", { class: "muted pr-note" }, t.note) : null,
        el("div", { class: "muted src-line" },
          t.overdue && !snoozed ? el("span", { class: "tag overdue" }, "Overdue") : null,
          snoozed ? el("span", { class: "tag today" }, `Snoozed until ${clock(t.wake * 1000)}`) : null,
          ` ${t.repeat_label} · ${nextLabel(t.next) || "No more"}`)),
      el("div", { class: "actions" },
        t.open && !t.done && !snoozed && t.snooze ? el("button", { type: "button", class: "small subtle", onclick: () => guarded(async () => {
          await api("/api/reminders/snooze", { id: t.id, day: isoDay(new Date()) }); await load(); poll();
        }) }, `Snooze ${snoozeDefault} min`) : null,
        el("button", { type: "button", class: "small subtle", onclick: () => editor(t) }, "Edit")));
  }

  function reminderRow(r) {
    // On / off is a toggle button, so it doesn't look like the Done checkboxes above.
    const on = el("button", { type: "button", class: "small pr-toggle", "aria-pressed": String(!!r.enabled),
      title: r.enabled ? "On: click to pause this reminder" : "Off: click to turn it back on",
      onclick: () => guarded(async () => { await api("/api/reminders", { ...pick(r), enabled: !r.enabled }); await load(); poll(); }) },
      r.enabled ? "On" : "Off");
    return el("li", { class: `pr-row ${r.enabled ? "" : "off"}` },
      el("div", { class: "pr-time" }, hhmm(r.time)),
      el("div", { class: "grow" },
        personalText("div", { class: "title" }, r.text),
        el("div", { class: "muted src-line" },
          r.repeat === "once" ? `Once · ${shortDay(r.date)}` : r.repeat_label,
          r.enabled ? ` · ${nextLabel(r.next) || "No more"}` : " · Off", r.snooze ? "" : " · no snooze")),
      el("div", { class: "actions" }, on, el("button", { type: "button", class: "small subtle", onclick: () => editor(r) }, "Edit")));
  }

  function entryRow(e) {
    return el("li", { class: "pr-row pr-entry clickable", onclick: () => CC.openTab("calendar") },
      el("div", { class: "pr-time" }, e.all_day ? "All day" : hhmm(e.start)),
      el("div", { class: "grow" },
        personalText("div", { class: "title" }, e.title || "(untitled)"),
        el("div", { class: "muted src-line" }, el("span", { class: "src src-personal" }, "Personal"), ` ${shortDay(e.day)}`)));
  }

  function recentRow(e) {
    return el("li", { class: "pr-row pr-entry clickable", onclick: () => CC.openTab("calendar") },
      el("div", { class: "grow" },
        personalText("div", { class: "title" }, e.title || "(untitled)"),
        el("div", { class: "muted src-line" }, `For ${shortDay(e.day)}${e.all_day ? "" : ` ${hhmm(e.start)}`} · saved ${CC.when(e.updated)}`)));
  }

  const pick = (r) => ({ id: r.id, text: r.text, note: r.note || "", time: r.time, repeat: r.repeat, days: r.days || [],
    date: r.date || null, enabled: r.enabled !== false, snooze: r.snooze !== false });

  // ---- add / edit dialog ------------------------------------------------------------------------------
  function addReminder() {
    withPersonal(() => editor({}), "Unlock your personal vault to add a reminder.");
  }

  function editor(r) {
    const now = new Date();
    const defTime = `${pad((now.getHours() + 1) % 24)}:00`;
    const text = el("input", { id: "pr-text", maxlength: "200", placeholder: "e.g. Stretch, take a walk, call home", value: r.text || "" });
    const time = el("input", { id: "pr-time", type: "time", value: r.time || defTime });
    const day = el("input", { id: "pr-date", type: "date", value: r.date || isoDay(now) });
    const note = el("textarea", { id: "pr-note", maxlength: "2000", placeholder: "Note (optional)" });
    note.value = r.note || "";
    const enabled = el("input", { type: "checkbox" });
    enabled.checked = r.enabled !== false;
    const snooze = el("input", { type: "checkbox" });
    snooze.checked = r.snooze !== false;
    let repeat = r.repeat || "weekdays";
    const days = new Set(r.days && r.days.length ? r.days : ["mon", "tue", "wed", "thu", "fri"]);
    const repBtns = REPEATS.map(([id, label]) => el("button", { type: "button", class: "pr-seg", "data-rep": id, onclick: () => { repeat = id; sync(); } }, label));
    const dayBtns = DAYS.map(([id, label]) => el("button", { type: "button", class: "pr-day", "data-day": id,
      onclick: () => { days.has(id) ? days.delete(id) : days.add(id); sync(); } }, label));
    const dayRow = el("div", { class: "pr-days", role: "group", "aria-label": "Days of the week" }, dayBtns);
    const dateRow = el("div", {}, el("label", { class: "field-label", for: "pr-date" }, "Date"), day);
    function sync() {
      repBtns.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.rep === repeat)));
      dayBtns.forEach((b) => b.setAttribute("aria-pressed", String(days.has(b.dataset.day))));
      dayRow.hidden = repeat !== "days";
      dateRow.hidden = repeat !== "once";
    }
    sync();
    const err = el("p", { class: "err", hidden: true });
    const save = () => guarded(async () => {
      try {
        await api("/api/reminders", { id: r.id ?? null, text: text.value, note: note.value, time: time.value, repeat,
          days: DAYS.map(([id]) => id).filter((id) => days.has(id)), date: repeat === "once" ? day.value : null,
          enabled: enabled.checked, snooze: snooze.checked });
      } catch (e) { err.textContent = e.message; err.hidden = false; return; }
      CC.$("#dialog").close();
      await load();
      poll();
    });
    const remove = () => guarded(async () => {
      if (!await CC.confirmDialog("Delete reminder", "Delete this personal reminder?", "Delete")) return;
      await api("/api/reminders/delete", { id: r.id });
      await load();
      poll();
    });
    CC.showDialog(el("h3", {}, r.id ? "Edit personal reminder" : "New personal reminder"),
      el("p", { class: "muted" }, "Pops up at that time until you click Done. Saved in your personal vault; the Windows notification only says “Personal reminder”."),
      el("label", { class: "field-label", for: "pr-text" }, "Remind me to"), text,
      el("div", { class: "pr-form-row" },
        el("div", {}, el("label", { class: "field-label", for: "pr-time" }, "Time"), time),
        el("div", { class: "grow" }, el("span", { class: "field-label" }, "Repeat"), el("div", { class: "pr-segs", role: "group", "aria-label": "Repeat" }, repBtns))),
      dayRow, dateRow,
      el("label", { class: "field-label", for: "pr-note" }, "Note"), note,
      el("div", { class: "pr-opts" }, el("label", { class: "pr-opt" }, enabled, "On"), el("label", { class: "pr-opt" }, snooze, "Show Snooze")),
      err,
      el("div", { class: "row" },
        r.id && el("button", { type: "button", class: "subtle", onclick: remove }, "Delete"),
        el("button", { value: "cancel", class: "subtle" }, "Cancel"),
        el("button", { type: "button", class: "primary", onclick: save }, "Save")));
    text.focus();
  }

  // ---- the pop-up ---------------------------------------------------------------------------------------
  let pop = null, popItems = [], popSig = "", popAvailable = false, popPrivate = false, closing = false, wakeTimer = null;

  function popDialog() {
    if (pop) return pop;
    pop = el("dialog", { class: "pr-pop", "aria-labelledby": "pr-pop-title" });
    pop.addEventListener("cancel", (e) => e.preventDefault());  // Esc doesn't close it
    pop.addEventListener("close", () => {
      if (closing || !popItems.length) return;
      setTimeout(() => { if (popItems.length && !pop.open) pop.showModal(); }, 0);  // closed some other way: back it comes
    });
    document.body.append(pop);
    return pop;
  }

  const sig = (items, available, priv = popPrivate) => JSON.stringify([available, priv, items.map((i) => [i.id, i.day, i.text ?? null, i.note ?? null])]);

  function drawPop() {
    const d = popDialog();
    const missed = popItems.filter((i) => i.missed).length;
    const n = popItems.length;
    const rows = popItems.map((i) => {
      const more = el("span", { class: "pr-more", hidden: true },
        SNOOZE_MORE.filter((m) => m !== snoozeDefault).map((m) => el("button", { type: "button", class: "small subtle", onclick: () => snoozeItem(i, m) }, `${m} min`)));
      return el("li", { class: `pr-pop-row ${i.missed ? "missed" : ""}` },
        el("div", { class: "pr-pop-time" }, clock(i.at)),
        el("div", { class: "grow" },
          i.text !== undefined ? personalText("div", { class: "pr-pop-text" }, i.text)
            : el("div", { class: "pr-pop-text pr-hidden" }, "Personal reminder", el("span", { class: "muted" }, " · unlock to see")),
          i.note ? personalText("div", { class: "muted pr-note" }, i.note) : null,
          i.missed ? el("div", { class: "muted" }, el("span", { class: "tag overdue" }, "Missed"), ` was due ${i.day === isoDay(new Date()) ? "" : `${shortDay(i.day)} `}${clock(i.at)}`) : null),
        el("div", { class: "pr-pop-acts" },
          el("button", { type: "button", class: "primary pr-pop-done", onclick: () => doneItem(i) }, "Done"),
          i.snooze ? el("span", { class: "pr-snooze" },
            el("button", { type: "button", class: "small", onclick: () => snoozeItem(i, null) }, `Snooze ${snoozeDefault} min`),
            el("button", { type: "button", class: "small subtle", "aria-label": "Other snooze lengths", title: "Other snooze lengths",
              onclick: () => { more.hidden = !more.hidden; } }, "▾"), more) : null));
    });
    d.replaceChildren(...[
      el("div", { class: "panel-label" }, "PERSONAL REMINDER"),
      el("h3", { id: "pr-pop-title" }, n === 1 ? (missed ? "Missed while you were away" : "Time for this") : `${n} personal reminders${missed ? ` · ${missed} missed while you were away` : ""}`),
      el("ul", { class: "list pr-pop-list" }, rows),
      !popAvailable ? el("div", { class: "pr-pop-lock" },
        el("span", { class: "muted" }, `${popPrivate ? "Privacy mode is on" : "Your personal vault is locked"}, so the text is hidden. Done works without unlocking.`),
        el("button", { type: "button", class: "small alt", onclick: () => unlockFromPop() }, "Unlock")) : null].filter(Boolean));
  }

  function showPop() {
    if (!popItems.length) { hidePop(); return; }
    drawPop();
    if (!pop.open) {
      pop.showModal();
      pop.querySelector(".pr-pop-done")?.focus();
    }
  }

  function hidePop() {
    if (pop?.open) { closing = true; pop.close(); closing = false; }
  }

  async function doneItem(i) {
    await guarded(async () => {
      await api("/api/reminders/done", { id: i.id, day: i.day, done: true });
      popItems = popItems.filter((x) => !(x.id === i.id && x.day === i.day));
      popSig = sig(popItems, popAvailable);
      showPop();
      if (data && section?.classList.contains("active")) load();
    });
  }

  async function snoozeItem(i, minutes) {
    await guarded(async () => {
      const r = await api("/api/reminders/snooze", { id: i.id, day: i.day, minutes });
      popItems = popItems.filter((x) => !(x.id === i.id && x.day === i.day));
      popSig = sig(popItems, popAvailable);
      showPop();
      armWake(r.wake);
      if (data && section?.classList.contains("active")) load();
    });
  }

  async function unlockFromPop() {
    if (CC.privacy && await CC.privacy.ensureUnlocked("Unlock to see your personal reminders.")) await poll();
  }

  function armWake(wake) {
    if (!wake) return;
    const ms = wake * 1000 - Date.now() + 1500;
    if (wakeTimer && wakeTimer.at <= wake) return;
    clearTimeout(wakeTimer?.id);
    wakeTimer = { at: wake, id: setTimeout(() => { wakeTimer = null; poll(); }, Math.max(1000, Math.min(ms, 2 ** 31 - 1))) };
  }

  let polling = false;
  async function poll() {
    if (polling) return;
    polling = true;
    try {
      const res = await fetch("/api/reminders/active");
      if (res.status === 401) { popItems = []; hidePop(); return; }  // session ended (privacy.js clears the page)
      if (!res.ok) return;
      const r = await res.json();
      snoozeDefault = r.snooze_minutes || snoozeDefault;
      armWake(r.next_wake);
      const s = sig(r.items, !!r.available, !!r.privacy);
      if (s === popSig && (!!pop?.open === !!r.items.length)) return;
      popItems = r.items;
      popAvailable = !!r.available;
      popPrivate = !!r.privacy;
      popSig = s;
      showPop();
    } catch { /* server restarting: try again on the next poll */ } finally { polling = false; }
  }

  // ---- wiring -------------------------------------------------------------------------------------
  CC.registerTab({ id: "personal", label: "Personal", pane: "comms", render, onShow: load });
  CC.addPaletteSource(() => [
    { group: "Commands", label: "Personal", hint: "reminders, personal calendar, notes", run: () => CC.openTab("personal") },
    ...(CC.privacy?.isPrivate() ? [] : [{ group: "Commands", label: "Add personal reminder", hint: "pops up at a time of day", run: () => addReminder() }]),
  ]);
  // Personal data no longer available (locked, Privacy mode, session ended): drop what's shown at once, then reload.
  document.addEventListener("cc:privacy", (e) => {
    if (!e.detail?.available || !section?.classList.contains("active")) { data = null; body?.replaceChildren(); }
    poll();
    if (section?.classList.contains("active")) load();
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  window.addEventListener("focus", () => poll());
  setInterval(poll, 10000);
  setInterval(() => { if (section?.classList.contains("active") && !document.hidden) load(); }, 60000);  // overdue marks, next times
  CC.addPersonalReminder = addReminder;
  poll();
})();
