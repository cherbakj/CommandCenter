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

  // An event from an overlay calendar (Google / iCloud), drawn in that calendar's color.
  function overlayTag(name, color) {
    const tag = el("span", { class: "src pl-ovl-tag", title: `${name}: read-only overlay calendar` }, name);
    tag.style.color = color;
    return tag;
  }

  function overlayRow(e) {
    const now = new Date();
    const live = !e.all_day && new Date(e.start) <= now && now < new Date(e.end);
    const li = el("li", { class: `pl-ovl ${live ? "live" : ""} ${!e.all_day && new Date(e.end) <= now ? "past" : ""}` },
      el("div", { class: "pl-time" }, e.all_day ? "All day" : clock(e.start)),
      el("div", { class: "pl-body" },
        el("div", { class: "pl-title" }, e.title),
        el("div", { class: "muted src-line" }, overlayTag(e.calendar, e.color),
          [e.all_day ? "" : `until ${clock(e.end)}`, e.location].filter(Boolean).join(" · "))));
    li.style.borderLeftColor = e.color;
    li.style.setProperty("--ovl", e.color);
    return li;
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
    // Outlook meetings and overlay-calendar events share one timeline, all-day items first.
    const overlays = d.overlays || [];
    const meetings = [...d.meetings.map((m) => ({ m })), ...overlays.map((e) => ({ e }))]
      .sort((a, b) => {
        const x = a.m || a.e, y = b.m || b.e;
        return (y.all_day - x.all_day) || x.start.localeCompare(y.start);
      })
      .map((row) => (row.m ? meetingRow(row.m) : overlayRow(row.e)));
    const block = (label, rows, cls) => rows.length ? el("div", { class: `pl-block ${cls}` }, el("div", { class: "pl-block-label" }, label), el("ul", { class: "pl-list" }, rows)) : null;
    return el("article", { class: `pl-day ${isToday ? "today" : ""} ${weekend ? "weekend" : ""}` },
      el("header", { class: "pl-day-head" },
        el("div", {},
          el("div", { class: "pl-dow" }, isToday ? "Today" : date.toLocaleDateString([], { weekday: "short" }),
            weekend && el("span", { class: "pl-wkd" }, "Weekend")),
          el("div", { class: "pl-date" }, date.toLocaleDateString([], { month: "short", day: "numeric" }))),
        el("div", { class: "pl-counts" },
          d.meetings.length ? el("span", { class: "chip", title: "Outlook meetings" }, `${d.meetings.length} mtg`) : null,
          overlays.length ? el("span", { class: "chip pl-chip-ovl", title: "Events from your other calendars" }, `${overlays.length} other`) : null,
          d.items.length ? el("span", { class: "chip pl-chip-fu", title: "Follow-ups" }, `${d.items.length} f/u`) : null),
        el("button", { class: "icon subtle", title: "Add a personal entry (stays on this PC)", "aria-label": `Add personal entry on ${d.date}`,
          onclick: () => personalDialog({ day: d.date, all_day: true }) }, "+")),
      block(overlays.length ? "Calendar" : "Outlook", meetings, "pl-b-mtg"),
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

  // ---- overlay calendars (Google / iCloud iCal links) ----------------------------------------------
  let calendars = [], palette = [];
  let legendCals;

  function renderLegend() {
    legendCals.replaceChildren(...calendars.map((c) => {
      const chip = el("button", { type: "button", class: `pl-cal-chip ${c.enabled ? "" : "off"}`, "aria-pressed": String(!!c.enabled),
        title: `${c.enabled ? "Hide" : "Show"} ${c.name}${c.error ? ` (problem: ${c.error})` : ""}`,
        onclick: () => guarded(async () => { await api("/api/feeds/update", { id: c.id, enabled: !c.enabled }); await load(); }) },
        el("i", {}), c.name, c.error ? el("span", { class: "pl-cal-warn" }, "!") : null);
      chip.querySelector("i").style.background = c.color;
      return chip;
    }));
  }

  function calendarsDialog() {
    const name = el("input", { id: "cal-name", placeholder: "e.g. Family (Google), Kids (iCloud)", maxlength: "40" });
    const color = el("input", { id: "cal-color", type: "color", value: palette[calendars.length % (palette.length || 1)] || "#ff9f43" });
    const url = el("input", { id: "cal-url", type: "password", placeholder: "Paste the private iCal link (https://… or webcal://…)", autocomplete: "off", spellcheck: "false" });
    const err = el("p", { class: "err", hidden: true });
    const list = el("ul", { class: "list pl-cal-list" });
    const fill = () => list.replaceChildren(...(calendars.length ? calendars.map((c) => {
      const swatch = el("input", { type: "color", value: c.color, title: "Change color", "aria-label": `Color for ${c.name}` });
      swatch.addEventListener("change", () => guarded(async () => { await api("/api/feeds/update", { id: c.id, color: swatch.value }); await refreshList(); }));
      const on = el("input", { type: "checkbox", title: "Show on the calendar", "aria-label": `Show ${c.name}` });
      on.checked = !!c.enabled;
      on.addEventListener("change", () => guarded(async () => { await api("/api/feeds/update", { id: c.id, enabled: on.checked }); await refreshList(); }));
      return el("li", {},
        swatch,
        el("div", { class: "grow" },
          el("div", { class: "title" }, c.name, el("span", { class: "tag" }, c.kind)),
          el("div", { class: c.error ? "err" : "muted" }, c.error || (c.count != null ? `${c.count} upcoming event(s) · checked ${c.fetched ? CC.when(c.fetched) : "—"}` : "Not checked yet"))),
        el("label", { class: "pl-check" }, on, "Show"),
        el("button", { type: "button", class: "small subtle", onclick: () => guarded(async () => {
          if (!await CC.confirmDialog("Remove calendar", `Remove "${c.name}" from Command Center? (Nothing changes in the calendar itself.)`, "Remove")) return calendarsDialog();
          await api("/api/feeds/delete", { id: c.id });
          await refreshList(true);
          calendarsDialog();
        }) }, "Remove"));
    }) : [el("li", { class: "empty" }, "No overlay calendars yet. Add one below.")]));
    const refreshList = async (silent) => {
      const data = await api("/api/feeds");
      calendars = data.feeds; palette = data.colors;
      fill(); renderLegend();
      if (!silent) load();
    };
    const add = () => guarded(async () => {
      err.hidden = true;
      if (!name.value.trim() || !url.value.trim()) { err.textContent = "Enter a name and paste the calendar's iCal link."; err.hidden = false; return; }
      try {
        const r = await api("/api/feeds", { name: name.value, url: url.value, color: color.value });
        url.value = ""; name.value = "";
        CC.showMessage("Calendar added", `${r.feed.name} is on your calendar (${r.upcoming} event(s) in the next two weeks).`);
        await refreshList(true);
        load();
      } catch (e) { err.textContent = e.message; err.hidden = false; }
    });
    fill();
    CC.wideDialog(el("h3", {}, "Overlay calendars"),
      el("p", { class: "muted" }, "Read-only: Command Center only reads these calendars. Nothing is added to them or copied to Outlook. Links are kept in Windows Credential Manager; events stay in memory."),
      list,
      el("div", { class: "panel-label gap" }, "Add a calendar"),
      el("div", { class: "pl-cal-add" },
        el("label", { class: "field-label", for: "cal-name" }, "Name"), name,
        el("label", { class: "field-label", for: "cal-color" }, "Color"), color,
        el("label", { class: "field-label", for: "cal-url" }, "Private iCal link"), url),
      el("details", { class: "pl-help" },
        el("summary", {}, "Where do I find the link?"),
        el("p", {}, el("b", {}, "Google Calendar (Gmail): "), "On a computer open calendar.google.com → Settings (gear) → under \"Settings for my calendars\" pick the calendar → Integrate calendar → copy \"Secret address in iCal format\". Repeat for each calendar you want (e.g. family calendars Skylight shows)."),
        el("p", {}, el("b", {}, "iCloud: "), "In the Calendar app (iPhone: Calendars → ⓘ next to the calendar) turn on \"Public Calendar\" and copy the link (starts with webcal://). Note: anyone with that link can view the calendar, so keep it private."),
        el("p", {}, el("b", {}, "Skylight: "), "Skylight shows calendars that live in Google or iCloud. Add those same calendars here.")),
      err,
      el("div", { class: "row" }, el("button", { value: "cancel", class: "subtle" }, "Close"),
        el("button", { type: "button", class: "primary", onclick: add }, "Add calendar")));
    name.focus();
  }

  function render(section) {
    note = el("div", { class: "muted pl-note" });
    grid = el("div", { class: "pl-grid" }, el("div", { class: "muted" }, "Loading the next 10 days…"));
    legendCals = el("span", { class: "pl-legend pl-legend-cals" });
    section.replaceChildren(
      el("div", { class: "toolbar pl-toolbar" },
        el("span", { class: "panel-label" }, `Next ${DAYS} days`),
        el("span", { class: "pl-legend" }, CC.srcTag("calendar"), CC.srcTag("meeting"), CC.srcTag("todo"), CC.srcTag("flagged"), personalTag()),
        legendCals,
        el("span", { class: "spacer" }),
        el("button", { class: "small alt", onclick: calendarsDialog }, "Calendars"),
        el("button", { class: "small", onclick: () => personalDialog({ day: isoDay(new Date()), all_day: true }) }, "+ Personal"),
        el("button", { class: "small", onclick: () => load() }, "↻ Refresh")),
      note, grid);
  }

  async function load() {
    if (!grid) return;
    await guarded(async () => {
      const [data, cals] = await Promise.all([api(`/api/planner?days=${DAYS}`), api("/api/feeds")]);
      calendars = cals.feeds; palette = cals.colors;
      renderLegend();
      const problems = (data.calendars || []).filter((c) => c.error)
        .map((c) => `${(calendars.find((x) => x.id === c.id) || {}).name || "Calendar"}: ${c.error}`);
      note.textContent = [data.outlook_error ? `Outlook: ${data.outlook_error}` : "", ...problems].filter(Boolean).join(" · ");
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
    { group: "Commands", label: "Add a Google / iCloud calendar", hint: "read-only overlay", run: () => { CC.openTab("calendar"); calendarsDialog(); } },
  ]);
  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", mount, { once: true }) : mount();
})();
