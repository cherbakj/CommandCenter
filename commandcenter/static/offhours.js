// Off-hours work log (offhours.py): a tile in Today's stats strip with today's off-hours time at this PC and this
// week's (Monday - Sunday) total; clicking it shows this week's sessions per day. Times only: the server logs when
// Windows was unlocked with keyboard / mouse use outside work hours, never what was on screen.
// Drawn with CC.el / textContent only.
(() => {
  const CC = window.CC;
  if (!CC?.addStatTile) return;
  const { el, api } = CC;

  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = "/static/offhours.css";
  document.head.append(link);

  let last = null;  // the latest /api/offhours answer, for the dialog

  const dur = (mins) => {
    const h = Math.floor(mins / 60), m = mins % 60;
    return h ? `${h}h${m ? ` ${m}m` : ""}` : `${m}m`;
  };
  // "HH:MM" (24:00 = the next midnight) -> local clock text.
  const clock = (hhmm) => {
    const [h, m] = String(hhmm).split(":").map(Number);
    return new Date(2000, 0, 1, h, m).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  };
  const DAYS = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" };
  const workDays = (s) => {
    const d = s.work_days || [];
    const all = Object.keys(DAYS);
    const run = d.length > 1 && d.every((x, i) => i === 0 || all.indexOf(x) === all.indexOf(d[i - 1]) + 1);
    return !d.length ? "no days" : run ? `${DAYS[d[0]]} – ${DAYS[d[d.length - 1]]}` : d.map((x) => DAYS[x]).join(", ");
  };
  const rules = (s) => `Work hours ${clock(s.work_start)} – ${clock(s.work_end)}, ${workDays(s)}. Counted: Windows unlocked with `
    + `keyboard or mouse use; pauses over ${s.idle_minutes} min don't count. Only logged while Command Center is running `
    + `(it starts when you sign in to Windows), and never while the PC sleeps.`;

  function tile(r) {
    const today = r.today.minutes, week = r.week_minutes;
    return CC.statTile(dur(today), r.active ? "Off-hours today · now" : "Off-hours today", r.active ? "var(--hot)" : "var(--accent-2)", {
      cls: `oh-tile${r.active ? " oh-live" : ""}`,
      sub: `Week ${dur(week)}`,
      title: `Time at this PC outside work hours. Today ${dur(today)} · this week (Mon – Sun) ${dur(week)}.\n${rules(r.settings)}\nClick for this week's sessions.`,
      onclick: weekDialog,
    });
  }

  CC.addStatTile(async () => {
    try {
      const r = await api("/api/offhours");
      last = r;
      return r.enabled ? tile(r) : null;
    } catch { return null; }  // server restarting: no tile this round
  });

  async function weekDialog() {
    let r = last;
    try { r = await api("/api/offhours"); last = r; } catch (e) { if (!r) { CC.showMessage("Off-hours", e.message); return; } }
    const todayIso = r.today.day;
    const rows = r.days.map((d) => {
      const date = new Date(`${d.day}T00:00`);
      return el("li", { class: `oh-day${d.day === todayIso ? " now" : ""}${d.future ? " oh-future" : ""}${d.workday ? "" : " oh-weekend"}` },
        el("div", { class: "time-col" }, date.toLocaleDateString([], { weekday: "short" }),
          el("div", { class: "muted" }, date.toLocaleDateString([], { month: "short", day: "numeric" }))),
        el("div", { class: "grow" },
          d.sessions.length
            ? el("div", { class: "oh-sessions" }, d.sessions.map((x) => el("span", { class: "oh-session" },
                `${clock(x.start)} – ${clock(x.end)}`, el("small", {}, dur(x.minutes)))))
            : el("div", { class: "muted" }, d.future ? "—" : d.workday ? "No off-hours time" : "Weekend · no time logged")),
        el("b", { class: "oh-total" }, d.minutes ? dur(d.minutes) : ""));
    });
    CC.showDialog(
      el("h3", {}, "Off-hours this week"),
      el("p", { class: "muted" }, rules(r.settings)),
      el("ul", { class: "list dialog-list oh-list" }, rows),
      el("div", { class: "oh-sum" }, el("span", {}, "This week (Mon – Sun)"), el("b", {}, dur(r.week_minutes))),
      el("p", { class: "muted oh-note" }, "Only start and end times are kept, encrypted on this PC (Privacy & Data: Off-hours work log)."),
      el("div", { class: "row" }, el("button", { value: "close" }, "Close")));
  }

  CC.addPaletteSource(() => [{ group: "Commands", label: "Off-hours this week", hint: "time at this PC outside work hours", run: weekDialog }]);
})();
