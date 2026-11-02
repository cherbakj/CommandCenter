// The next meeting, in the header's free space between the tab groups and the buttons on the right, on every tab.
// GET /api/meetings/next gives what's on now and what's coming; which one to show is worked out here, so the countdown
// and the switch to the next meeting need no server call. Click: the Prep card. Join: for Teams meetings, up to the day
// they're on. How much of it shows follows the room it has (style.css "next meeting"); no meeting to come: nothing shows.
(function () {
  "use strict";
  if (!window.CC) return;
  const { el, api, timeOf, duration, $ } = CC;

  const SOON_MIN = 15;    // amber from here on, the first of the meeting reminders
  const SWITCH_MIN = 5;   // a meeting still running gives way to the next one this close to its start
  const POLL_MS = 60000, TICK_MS = 15000;

  const box = $("#next-meeting");
  let meetings = [], shown = null, busy = false;

  const parts = {
    when: el("span", { class: "nm-when" }),
    subject: el("span", { class: "nm-subject" }),
    time: el("span", { class: "nm-time" }),
    then: el("span", { class: "nm-then" }),
  };
  const main = el("button", { type: "button", class: "nm-main", onclick: () => shown && CC.prepDialog(shown.id, shown) },
    parts.when, parts.subject, parts.time, parts.then);
  const join = el("button", { type: "button", class: "small primary nm-join", onclick: () => shown?.join_url && CC.openExternal(shown.join_url) }, "Join");
  const chip = el("div", { class: "nm-chip" }, main, join);
  box.append(chip);

  function pick(now) {
    const open = meetings.filter((m) => new Date(m.end) > now);
    const coming = open.filter((m) => new Date(m.start) > now), live = open.filter((m) => new Date(m.start) <= now);
    const nearly = coming[0] && new Date(coming[0].start) - now <= SWITCH_MIN * 60000;
    return live.length && !nearly ? { m: live[live.length - 1], then: coming[0] } : { m: coming[0] };
  }

  function dayName(d, now) {
    const tomorrow = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
    return d.toDateString() === tomorrow.toDateString() ? "Tomorrow" : d.toLocaleDateString([], { weekday: "short" });
  }

  function describe(m, then, now) {
    const start = new Date(m.start), end = new Date(m.end);
    const range = `${timeOf(m.start)} – ${timeOf(m.end)}`;
    if (start <= now) return { kind: "nm-live", when: "Now", time: `ends in ${duration(Math.max(1, Math.ceil((end - now) / 60000)))}`,
      then: then && new Date(then.start).toDateString() === now.toDateString() ? `then ${timeOf(then.start)}` : "", today: true };
    if (start.toDateString() !== now.toDateString()) return { kind: "nm-later", when: `${dayName(start, now)} ${timeOf(m.start)}`, time: "", then: "", today: false };
    const mins = Math.ceil((start - now) / 60000);
    return { kind: mins <= SOON_MIN ? "nm-soon" : "", when: mins <= 1 ? "Starting" : `In ${duration(mins)}`, time: range, then: "", today: true };
  }

  function render() {
    const now = new Date();
    const { m, then } = pick(now);
    shown = m || null;
    if (box.hidden !== !m) box.hidden = !m;
    if (!m) return;
    const d = describe(m, then, now);
    // Only what changed, so the button keeps the keyboard focus and the native views aren't re-checked for nothing.
    for (const k of ["when", "time", "then"]) if (parts[k].textContent !== d[k]) parts[k].textContent = d[k];
    if (parts.subject.textContent !== m.subject) parts.subject.textContent = m.subject;
    const cls = `nm-chip ${d.kind}`.trim();
    if (chip.className !== cls) chip.className = cls;
    const canJoin = !!m.join_url && d.today;
    if (join.hidden === canJoin) join.hidden = !canJoin;
    const label = `${m.subject}, ${new Date(m.start).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })} ${timeOf(m.start)} – ${timeOf(m.end)}${m.location ? `, ${m.location}` : ""}. Click for attendees and recent mail.`;
    if (main.title !== label) { main.title = label; main.setAttribute("aria-label", label); }
  }

  async function load() {
    if (busy || document.hidden) return;
    busy = true;
    try {
      const r = await api("/api/meetings/next");
      if (Array.isArray(r.meetings)) meetings = r.meetings;  // an Outlook problem keeps what's there
    } catch { /* server away for a moment: same */ } finally { busy = false; }
    render();
  }

  load();
  setInterval(load, POLL_MS);
  setInterval(render, TICK_MS);
  document.addEventListener("visibilitychange", load);
  document.addEventListener("cc:today", load);  // Today has just re-read the calendar (a block was added, Rescan)
})();
