// Today's calendar: the current day as a time grid in Today's side column (#today-day). Same sources as the
// Calendar tab (GET /api/planner?days=1): Outlook meetings (all-day ones in a strip on top), overlay calendars in
// their own colors, and personal entries (those two only while the personal vault is unlocked and Privacy mode is
// off; the server leaves them out otherwise). Overlapping events share the width; a line marks the current time.
// Fetched with Today's refreshes (at most once a minute) and when the day or the privacy state changes; the grid is
// redrawn every minute from what it has. Text goes in as text only (CC.el).
(() => {
  const CC = window.CC;
  if (!CC) return;
  const { el, api, guarded } = CC;

  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = "/static/daycal.css";
  document.head.append(link);

  const MIN_FETCH_MS = 60000;
  const pad = (n) => String(n).padStart(2, "0");
  const isoDay = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const clock = (d) => d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const hourLabel = (h) => new Date(2000, 0, 1, h).toLocaleTimeString([], { hour: "numeric" });

  let panel, dateChip, note, allDay, scroller, grid;
  let data = null, fetchedAt = 0, fetchedDay = "", seq = 0, scrolled = false;

  // ---- events: one shape for all sources ------------------------------------------------------------
  function events(day) {
    const date = day.date;
    const out = [];
    for (const m of day.meetings || []) {
      out.push({ kind: m.join_url ? "meeting" : "calendar", allDay: m.all_day, start: new Date(m.start), end: new Date(m.end),
        title: m.subject, where: m.location, m });
    }
    for (const e of day.overlays || []) {
      out.push({ kind: "overlay", allDay: e.all_day, start: new Date(e.start), end: new Date(e.end),
        title: e.busy ? "Busy" : e.title, where: e.location, calendar: e.calendar, color: e.color });
    }
    for (const p of day.personal || []) {
      out.push({ kind: "personal", allDay: p.all_day || !p.start, start: new Date(`${date}T${p.start || "00:00"}`),
        end: new Date(`${date}T${p.end || p.start || "00:00"}`), title: p.title });
    }
    return out.filter((e) => e.title !== undefined && !Number.isNaN(e.start.getTime()));
  }

  // Columns for overlapping events: groups of events that overlap (directly or through others) share the width.
  function layoutColumns(timed) {
    timed.sort((a, b) => a.s - b.s || b.e - a.e);
    let group = [], groupEnd = -Infinity;
    const close = () => {
      const ends = [];
      for (const ev of group) {
        let col = ends.findIndex((end) => end <= ev.s);
        if (col < 0) { col = ends.length; ends.push(0); }
        ends[col] = ev.e;
        ev.col = col;
      }
      for (const ev of group) ev.cols = ends.length;
      group = [];
    };
    for (const ev of timed) {
      if (group.length && ev.s >= groupEnd) close();
      groupEnd = group.length ? Math.max(groupEnd, ev.e) : ev.e;
      group.push(ev);
    }
    if (group.length) close();
  }

  const KIND_LABEL = { meeting: "Teams meeting", calendar: "Outlook", personal: "Personal" };

  function block(ev, startH, now) {
    const past = ev.end <= now, live = ev.start <= now && now < ev.end;
    const span = `${clock(ev.start)} – ${clock(ev.end)}`;
    const source = ev.kind === "overlay" ? ev.calendar : KIND_LABEL[ev.kind];
    const tip = [span, ev.title, ev.where, source].filter(Boolean).join(" · ");
    const short = ev.e - ev.s <= 0.5;
    const text = [el("span", { class: "dc-title" }, ev.title), el("span", { class: "dc-time" }, short ? clock(ev.start) : span)];
    let main;
    if (ev.m) main = el("button", { type: "button", class: "dc-main", title: `${tip}\nOpen in Outlook`, "aria-label": `${ev.title}, ${span}: open in Outlook`,
      onclick: () => guarded(() => api("/api/outlook/open", { id: ev.m.id })) }, ...text);
    else if (ev.kind === "personal") main = el("button", { type: "button", class: "dc-main", title: `${tip}\nOpen Personal`, "aria-label": `${ev.title}, ${span}: open Personal`,
      onclick: () => CC.openTab("personal") }, ...text);
    else main = el("div", { class: "dc-main", title: tip }, ...text);
    const node = el("div", { class: `dc-ev k-${ev.kind} ${past ? "past" : ""} ${live ? "live" : ""} ${short ? "short" : ""}` },
      main, ev.m?.join_url && !past ? el("button", { type: "button", class: "small primary dc-join", title: "Join the Teams meeting",
        "aria-label": `Join ${ev.title}`, onclick: () => CC.openExternal(ev.m.join_url) }, "Join") : null);
    node.style.setProperty("--t", String(ev.s - startH));
    node.style.setProperty("--h", String(Math.max(ev.e - ev.s, 0.25)));
    node.style.setProperty("--col", String(ev.col));
    node.style.setProperty("--cols", String(ev.cols));
    if (ev.color) node.style.setProperty("--ev", ev.color);
    return node;
  }

  function chip(ev) {
    const node = el("span", { class: `dc-allday-ev k-${ev.kind}`, title: [ev.title, ev.kind === "overlay" ? ev.calendar : KIND_LABEL[ev.kind]].filter(Boolean).join(" · ") },
      ev.title);
    if (ev.color) node.style.setProperty("--ev", ev.color);
    return node;
  }

  // ---- drawing ----------------------------------------------------------------------------------------
  function render() {
    if (!panel) return;
    const now = new Date();
    dateChip.textContent = now.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" }).toUpperCase();
    const day = data?.days?.[0];
    renderNote();
    if (!day || day.date !== isoDay(now)) { allDay.replaceChildren(); allDay.hidden = true; if (!data) grid.replaceChildren(el("div", { class: "dc-empty muted" }, "Loading…")); return; }

    const all = events(day);
    const dayStart = new Date(`${day.date}T00:00`).getTime();
    const hoursOf = (d) => Math.min(24, Math.max(0, (d.getTime() - dayStart) / 3600000));
    const timed = all.filter((e) => !e.allDay).map((e) => ({ ...e, s: hoursOf(e.start), e: Math.max(hoursOf(e.end), hoursOf(e.start)) }));
    const chips = all.filter((e) => e.allDay);
    allDay.hidden = !chips.length;
    allDay.replaceChildren(...(chips.length ? [el("span", { class: "dc-allday-label" }, "All day"), ...chips.map(chip)] : []));

    const wd = CC.workday || { start: 8, end: 17 };
    const weekend = now.getDay() === 0 || now.getDay() === 6;
    let startH = Math.floor(wd.start), endH = Math.ceil(wd.end);
    for (const ev of timed) { startH = Math.min(startH, Math.floor(ev.s)); endH = Math.max(endH, Math.ceil(ev.e)); }
    startH = Math.max(0, startH); endH = Math.min(24, Math.max(endH, startH + 1));
    layoutColumns(timed);

    const rows = [];
    for (let h = startH; h < endH; h++) {
      const off = weekend || h < wd.start || h >= wd.end;
      rows.push(el("div", { class: `dc-hour ${off ? "off" : ""}` }, el("span", { class: "dc-hour-label" }, hourLabel(h))));
    }
    const nowH = hoursOf(now);
    const nowLine = nowH >= startH && nowH <= endH
      ? el("div", { class: "dc-now", title: `Now ${clock(now)}`, "aria-hidden": "true" }) : null;
    nowLine?.style.setProperty("--t", String(nowH - startH));
    const left = timed.filter((e) => e.end > now).length;
    const empty = !timed.length && !chips.length
      ? el("div", { class: "dc-empty muted" }, weekend ? "Weekend: nothing on the calendar." : "Nothing on the calendar today.") : null;
    grid.style.setProperty("--hours", String(endH - startH));
    grid.replaceChildren(...rows, el("div", { class: "dc-events" }, ...timed.map((ev) => block(ev, startH, now))), ...[nowLine, empty].filter(Boolean));
    grid.setAttribute("aria-label", `${timed.length} timed ${timed.length === 1 ? "event" : "events"}, ${left} still ahead`);

    if (!scrolled && scroller.clientHeight) {  // first draw: now a quarter of the way down (or the first event at the top)
      scrolled = true;
      const hourPx = grid.offsetHeight / Math.max(1, endH - startH);
      const top = nowLine ? (nowH - startH) * hourPx - scroller.clientHeight / 4 : timed.length ? (timed[0].s - startH - 0.25) * hourPx : 0;
      scroller.scrollTop = Math.max(0, top);
    }
  }

  function renderNote() {
    const ps = data?.personal_state;
    const parts = [];
    if (data?.outlook_error) parts.push(el("span", { class: "err" }, `Outlook: ${data.outlook_error}`));
    if (ps && !ps.available && ps.set_up) {
      parts.push(el("span", { class: "muted" }, ps.privacy ? "Personal calendars hidden (Privacy mode)" : "🔒 Personal calendars locked"),
        !ps.privacy && CC.privacy ? el("button", { type: "button", class: "small subtle", onclick: async () => {
          if (await CC.privacy.ensureUnlocked("Unlock to show your personal calendars.")) load(true);
        } }, "Unlock") : null);
    }
    note.hidden = !parts.length;
    note.replaceChildren(...parts.filter(Boolean));
  }

  // ---- data --------------------------------------------------------------------------------------------
  async function load(force = false) {
    const today = isoDay(new Date());
    if (!panel || (!force && fetchedDay === today && Date.now() - fetchedAt < MIN_FETCH_MS)) return;
    const my = ++seq;
    fetchedAt = Date.now();
    fetchedDay = today;
    try {
      const r = await api("/api/planner?days=1");
      if (my !== seq) return;  // a newer load (privacy change) is on its way
      data = r;
    } catch (e) {
      if (my !== seq) return;
      fetchedAt = 0;
      if (!data) { grid.replaceChildren(el("div", { class: "dc-empty err" }, e.message)); return; }
    }
    render();
  }

  function mount() {
    panel = document.querySelector("#today-day");
    if (!panel) return;
    dateChip = el("span", { class: "chip" });
    note = el("div", { class: "dc-note", role: "status", hidden: true });
    allDay = el("div", { class: "dc-allday", hidden: true });
    grid = el("div", { class: "dc-grid", role: "group" });
    scroller = el("div", { class: "dc-scroll", tabindex: "0", "aria-label": "Today's schedule" }, grid);
    panel.replaceChildren(
      el("div", { class: "panel-head" }, el("span", { class: "panel-label" }, "TODAY'S CALENDAR"), dateChip, el("span", { class: "spacer" }),
        el("button", { type: "button", class: "small subtle", title: "The next 10 days", onclick: () => CC.openTab("calendar") }, "Calendar")),
      note, allDay, scroller);
    render();
    load(true);
    // Each minute: move the now line and dim what's over; a new day fetches again.
    setInterval(() => { isoDay(new Date()) !== fetchedDay ? load(true) : render(); }, 60000);
  }

  document.addEventListener("cc:today", () => load());
  // Privacy mode / vault lock: personal and overlay events leave the page at once, then the day reloads.
  document.addEventListener("cc:privacy", (e) => {
    const st = e.detail || {};
    const ps = data?.personal_state;
    if (!st.available && data?.days?.[0]) {
      data.days[0].personal = [];
      data.days[0].overlays = [];
      data.personal_state = { ...(ps || {}), ...st, available: false };
      render();
    }
    if (!ps || st.available !== ps.available || st.privacy !== ps.privacy) load(true);
  });
  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", mount, { once: true }) : mount();
})();
