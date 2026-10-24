// Calendar tab: the next 10 days. Outlook meetings are the base, dated follow-ups sit on top, and
// private personal entries (stored only on this PC, encrypted, never sent to Outlook) sit alongside.
(() => {
  const CC = window.CC;
  if (!CC) return;
  const { el, api, guarded } = CC;
  const DAYS = 10;

  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = "/static/planner.css";
  document.head.append(link);

  const pad = (n) => String(n).padStart(2, "0");
  const isoDay = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const dayDate = (iso) => new Date(`${iso}T00:00`);
  const clock = (iso) => new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const hhmm = (t) => (t ? new Date(`2000-01-01T${t}`).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "");

  let section, grid, note;

  function personalTag() { return el("span", { class: "src src-personal", title: "Personal: only on this PC, encrypted, never sent to Outlook" }, "Personal"); }

  function meetingRow(m) {
    const now = new Date();
    const live = new Date(m.start) <= now && now < new Date(m.end);
    return el("li", { class: `pl-mtg ${live ? "live" : ""} ${new Date(m.end) <= now ? "past" : ""} ${m.status === "tentative" ? "tentative" : ""}` },
      el("div", { class: "pl-time" }, m.all_day ? "All day" : `${clock(m.start)}`),
      el("div", { class: "pl-body" },
        el("div", { class: "pl-title" }, m.subject),
        el("div", { class: "muted src-line" }, CC.srcTag(m.join_url ? "meeting" : "calendar", m.organizer ? `organizer ${m.organizer}` : ""),
          [m.all_day ? "" : `until ${clock(m.end)}`, m.location, m.status && m.status !== "busy" ? m.status : ""].filter(Boolean).join(" · "))),
      el("div", { class: "pl-acts" },
        m.join_url && new Date(m.end) > now && el("button", { class: "small primary", onclick: () => CC.openExternal(m.join_url) }, "Join"),
        el("button", { class: "small subtle", title: "Open in Outlook", onclick: () => guarded(() => api("/api/outlook/open", { id: m.id })) }, "Open")));
  }

  const ITEM_LABEL = { task: "Due", flag: "Due",  // the source tag already says To Do / Flagged
    snooze: "Snooze ends", "reply-watch": "Reply check" };
  const shortDay = (iso) => dayDate(iso).toLocaleDateString([], { month: "short", day: "numeric" });

  // The real due date: overdue items are listed on today, so look the original date up in Today's data.
  function dueOf(x) {
    const t = CC.todayData;
    if (x.kind === "task") return (t?.tasks?.items || []).find((i) => i.id === x.id)?.due || (x.overdue ? null : x.day);
    if (x.kind === "flag") return (t?.followups?.flagged || []).find((i) => i.id === x.id)?.due || (x.overdue ? null : x.day);
    return null;
  }

  function itemRow(x) {
    const open = () => {
      if (x.url) return CC.openExternal(x.url);
      if (x.kind === "task" && x.source !== "asana") return guarded(() => api("/api/outlook/open", { id: x.id }));
      CC.openTab("mail");
    };
    // What kind of date this is ("Due Oct 22", "Flag due Oct 24", "Reply check 3:00 PM") is a tag in the
    // meta line; only items with a clock time use the time column.
    const due = dueOf(x);
    const label = ITEM_LABEL[x.kind] || "";
    const when = x.time ? hhmm(x.time) : due ? shortDay(due) : "";
    return el("li", { class: `pl-item ${x.overdue ? "overdue" : ""} ${x.time ? "" : "no-time"}` },
      x.time ? el("div", { class: "pl-time" }, hhmm(x.time)) : null,
      el("div", { class: "pl-body" },
        el("div", { class: "pl-title" }, x.title || "(untitled)", x.overdue && el("span", { class: "tag overdue" }, "Overdue")),
        el("div", { class: "muted src-line" }, CC.srcTag(x.source),
          label && el("span", { class: `pl-due ${x.overdue ? "late" : ""}` }, [label, when].filter(Boolean).join(" ")),
          x.who ? ` ${x.who}` : "")),
      el("div", { class: "pl-acts" }, el("button", { class: "small subtle", onclick: open }, "Open")));
  }

  function personalRow(p) {
    return el("li", { class: "pl-personal", tabindex: "0", role: "button", title: "Edit personal entry",
      onclick: () => personalDialog(p), onkeydown: (e) => { if (e.key === "Enter") personalDialog(p); } },
      el("div", { class: "pl-time" }, p.all_day ? "All day" : hhmm(p.start)),
      el("div", { class: "pl-body" },
        el("div", { class: "pl-title" }, p.title),
        el("div", { class: "muted src-line" }, personalTag(), p.all_day ? "" : `until ${hhmm(p.end)}`, p.notes ? ` · ${p.notes.slice(0, 80)}` : "")));
  }

  function dayCard(d) {
    const date = dayDate(d.date);
    const isToday = d.date === isoDay(new Date());
    const weekend = [0, 6].includes(date.getDay());
    const meetings = [...d.meetings].sort((a, b) => (b.all_day - a.all_day) || a.start.localeCompare(b.start));
    const block = (label, rows, cls) => rows.length ? el("div", { class: `pl-block ${cls}` }, el("div", { class: "pl-block-label" }, label), el("ul", { class: "pl-list" }, rows)) : null;
    return el("article", { class: `pl-day ${isToday ? "today" : ""} ${weekend ? "weekend" : ""}` },
      el("header", { class: "pl-day-head" },
        el("div", {},
          el("div", { class: "pl-dow" }, isToday ? "Today" : date.toLocaleDateString([], { weekday: "short" }),
            weekend && el("span", { class: "pl-wkd" }, "Weekend")),
          el("div", { class: "pl-date" }, date.toLocaleDateString([], { month: "short", day: "numeric" }))),
        el("div", { class: "pl-counts" },
          d.meetings.length ? el("span", { class: "chip", title: "Outlook meetings" }, `${d.meetings.length} mtg`) : null,
          d.items.length ? el("span", { class: "chip pl-chip-fu", title: "Follow-ups" }, `${d.items.length} f/u`) : null),
        el("button", { class: "icon subtle", title: "Add a personal entry (stays on this PC)", "aria-label": `Add personal entry on ${d.date}`,
          onclick: () => personalDialog({ day: d.date, all_day: true }) }, "+")),
      block("Outlook", meetings.map(meetingRow), "pl-b-mtg"),
      block("Follow-ups", d.items.map(itemRow), "pl-b-fu"),
      block("Personal", d.personal.map(personalRow), "pl-b-personal"),
      !meetings.length && !d.items.length && !d.personal.length ? el("div", { class: "pl-free" }, weekend ? "Weekend" : "Open day") : null);
  }

  function personalDialog(p) {
    const title = el("input", { id: "pl-title", placeholder: "e.g. Dentist, school pickup, gym", value: p.title || "", maxlength: "200" });
    const day = el("input", { id: "pl-day", type: "date", value: p.day || isoDay(new Date()) });
    const allDay = el("input", { id: "pl-allday", type: "checkbox" });
    allDay.checked = p.all_day !== false;
    const start = el("input", { id: "pl-start", type: "time", value: p.start || "09:00" });
    const end = el("input", { id: "pl-end", type: "time", value: p.end || "10:00" });
    const notes = el("textarea", { id: "pl-notes", placeholder: "Notes (optional)", maxlength: "4000" });
    notes.value = p.notes || "";
    const times = el("div", { class: "pl-times" }, el("label", { class: "field-label", for: "pl-start" }, "Start"), start,
      el("label", { class: "field-label", for: "pl-end" }, "End"), end);
    const sync = () => { times.hidden = allDay.checked; };
    allDay.addEventListener("change", sync);
    sync();
    const err = el("p", { class: "err", hidden: true });
    const save = () => guarded(async () => {
      try {
        await api("/api/planner/personal", { id: p.id ?? null, day: day.value, title: title.value, notes: notes.value,
          all_day: allDay.checked, start: allDay.checked ? null : start.value, end: allDay.checked ? null : end.value });
      } catch (e) { err.textContent = e.message; err.hidden = false; return; }
      document.querySelector("#dialog").close();
      load();
    });
    const remove = () => guarded(async () => {
      if (!await CC.confirmDialog("Delete personal entry", `Delete "${p.title}"?`, "Delete")) return;
      await api("/api/planner/personal/delete", { id: p.id });
      load();
    });
    CC.showDialog(el("h3", {}, p.id ? "Edit personal entry" : "New personal entry"),
      el("p", { class: "muted" }, "Private: saved only on this PC, encrypted with your Windows account, never sent to Outlook."),
      el("label", { class: "field-label", for: "pl-title" }, "Title"), title,
      el("label", { class: "field-label", for: "pl-day" }, "Date"), day,
      el("label", { class: "pl-check" }, allDay, "All day"), times,
      el("label", { class: "field-label", for: "pl-notes" }, "Notes"), notes, err,
      el("div", { class: "row" },
        p.id && el("button", { type: "button", class: "subtle", onclick: remove }, "Delete"),
        el("button", { value: "cancel", class: "subtle" }, "Cancel"),
        el("button", { type: "button", class: "primary", onclick: save }, "Save")));
    title.focus();
  }

  function render(section) {
    note = el("div", { class: "muted pl-note" });
    grid = el("div", { class: "pl-grid" }, el("div", { class: "muted" }, "Loading the next 10 days…"));
    section.replaceChildren(
      el("div", { class: "toolbar pl-toolbar" },
        el("span", { class: "panel-label" }, `Next ${DAYS} days`),
        el("span", { class: "pl-legend" }, CC.srcTag("calendar"), CC.srcTag("meeting"), CC.srcTag("todo"), CC.srcTag("flagged"), personalTag()),
        el("span", { class: "spacer" }),
        el("button", { class: "small", onclick: () => personalDialog({ day: isoDay(new Date()), all_day: true }) }, "+ Personal"),
        el("button", { class: "small", onclick: () => load() }, "↻ Refresh")),
      note, grid);
  }

  async function load() {
    if (!grid) return;
    await guarded(async () => {
      const data = await api(`/api/planner?days=${DAYS}`);
      note.textContent = data.outlook_error ? `Outlook: ${data.outlook_error}` : "";
      grid.replaceChildren(...data.days.map(dayCard));
    });
  }

  function mount() {
    section = document.querySelector("#tab-calendar");
    if (!section) return;
    render(section);
    CC.loaders.calendar = load;
    if (section.classList.contains("active")) load();
  }

  CC.addPaletteSource(() => [
    { group: "Commands", label: "Add personal calendar entry", hint: "private, this PC only", run: () => personalDialog({ day: isoDay(new Date()), all_day: true }) },
    { group: "Commands", label: "Calendar: next 10 days", hint: "", run: () => CC.openTab("calendar") },
  ]);
  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", mount, { once: true }) : mount();
})();
