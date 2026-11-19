// Meeting reminder pop-up (meetingpop.py; Joe's call 2026-11-19). Once, 5 minutes before each meeting
// (outlook.meetings.remind_minutes), its own modal <dialog> comes up like a personal reminder's (the desktop app also
// brings the window in front, unless CC is locked or the screen may be on show) and stays until it's dealt with:
// Join (Teams meetings; then it's done), Prep (the Prep card opens over it), Remind me when it starts, or Got it.
// Esc and clicks outside don't close it; another dialog can open over it and it's back on top when that closes. It
// goes by itself once the meeting is over, or when the alert is opened from the feed. Everything is inserted as text.
(() => {
  const CC = window.CC;
  if (!CC) return;
  const { el, api, guarded } = CC;

  let pop = null, items = [], sig = "", closing = false, wakeTimer = null;
  const clock = (iso) => new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  function countdown(i) {
    const min = Math.round((new Date(i.start) - Date.now()) / 60000);
    if (min > 0) return `Starts in ${min} min`;
    if (min === 0) return "Starting now";
    return `Started ${-min} min ago`;
  }

  function dialog() {
    if (pop) return pop;
    pop = el("dialog", { class: "mp-pop", "aria-label": "Meeting reminder" });
    pop.addEventListener("cancel", (e) => e.preventDefault());  // Esc doesn't close it
    pop.addEventListener("close", () => {
      if (closing || !items.length) return;
      setTimeout(() => { if (items.length && !pop.open) pop.showModal(); }, 0);  // closed some other way: back it comes
    });
    document.body.append(pop);
    return pop;
  }

  function row(i) {
    const soon = new Date(i.start) - Date.now() > 30000;
    return el("li", { class: "mp-pop-row" },
      el("div", { class: "mp-pop-when" }, countdown(i)),
      el("div", { class: "mp-pop-subj" }, i.subject),
      el("div", { class: "muted mp-pop-meta" }, [`${clock(i.start)}${i.end ? ` – ${clock(i.end)}` : ""}`, i.location].filter(Boolean).join(" · ")),
      el("div", { class: "mp-pop-acts" },
        i.join_url && el("button", { type: "button", class: "primary mp-pop-join", onclick: () => joinItem(i) }, "Join"),
        el("button", { type: "button", class: "small alt", onclick: () => CC.prepDialog?.(i.meeting_id) }, "Prep"),
        soon && el("button", { type: "button", class: "small subtle", onclick: () => atStart(i) }, "Remind me when it starts"),
        el("button", { type: "button", class: "small mp-pop-ok", onclick: () => doneItem(i) }, "Got it")));
  }

  function draw() {
    const n = items.length;
    dialog().replaceChildren(
      el("div", { class: "panel-label" }, n === 1 ? "MEETING" : `${n} MEETINGS`),
      el("ul", { class: "list mp-pop-list" }, items.map(row)));
  }

  function show() {
    if (!items.length) { hide(); return; }
    draw();
    if (!pop.open) { pop.showModal(); (pop.querySelector(".mp-pop-join") || pop.querySelector(".mp-pop-ok"))?.focus(); }
  }

  function hide() {
    if (pop?.open) { closing = true; pop.close(); closing = false; }
  }

  function drop(i) {
    items = items.filter((x) => x.id !== i.id);
    sig = JSON.stringify(items.map((x) => x.id));
    show();
  }

  async function doneItem(i) {
    await guarded(async () => { await api("/api/meeting-reminders/done", { id: i.id }); drop(i); CC.refreshAlerts?.(); });
  }

  async function joinItem(i) {
    try { await api("/api/meeting-reminders/done", { id: i.id }); } catch { /* already gone: join anyway */ }
    drop(i);
    CC.refreshAlerts?.();
    CC.joinMeeting(i.join_url);
  }

  async function atStart(i) {
    await guarded(async () => { const r = await api("/api/meeting-reminders/at-start", { id: i.id }); drop(i); arm(r.wake); });
  }

  function arm(wake) {
    if (!wake) return;
    if (wakeTimer && wakeTimer.at <= wake) return;
    clearTimeout(wakeTimer?.id);
    const ms = wake * 1000 - Date.now() + 1000;
    wakeTimer = { at: wake, id: setTimeout(() => { wakeTimer = null; poll(); }, Math.max(1000, Math.min(ms, 2 ** 31 - 1))) };
  }

  let polling = false;
  async function poll() {
    if (CC.side) return;  // pop-ups show in the main window only (the side window would be a second copy)
    if (polling) return;
    polling = true;
    try {
      const res = await fetch("/api/meeting-reminders/pending");
      if (res.status === 401) { items = []; hide(); return; }  // session ended
      if (!res.ok) return;
      const r = await res.json();
      arm(r.next_wake);
      const s = JSON.stringify(r.items.map((x) => x.id));
      if (s === sig && !!pop?.open === !!r.items.length) return;
      items = r.items;
      sig = s;
      show();
    } catch { /* server restarting: the next poll tries again */ } finally { polling = false; }
  }

  // The countdown keeps itself current while it shows.
  setInterval(() => { if (pop?.open) pop.querySelectorAll(".mp-pop-when").forEach((n, k) => { if (items[k]) n.textContent = countdown(items[k]); }); }, 15000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  window.addEventListener("focus", () => poll());
  setInterval(poll, 30000);
  CC.meetingPoll = poll;  // the desktop app calls it when it brings the window forward for a meeting
  poll();
})();
