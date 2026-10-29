// Hoxhunt phishing-test pop-up (hoxhunt.py). When a test arrives, its own modal <dialog> comes up (the desktop app
// also brings the window in front, as for a personal reminder) and stays until each test is dealt with: Open in
// Outlook to report (Hoxhunt's button is there; the pop-up holds back 15 min and comes back if the email is still in
// the Inbox), Already reported, or Remind me in 15 min. Reporting takes the email out of the Inbox, which ends its
// reminders by itself. Esc and clicks outside don't close it; another dialog can open over it and it's back on top
// when that closes. Everything is inserted as text.
(() => {
  const CC = window.CC;
  if (!CC) return;
  const { el, api, guarded } = CC;

  let pop = null, items = [], sig = "", closing = false, wakeTimer = null, snoozeMinutes = 15;
  const clock = (iso) => new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

  function dialog() {
    if (pop) return pop;
    pop = el("dialog", { class: "hx-pop", "aria-labelledby": "hx-pop-title" });
    pop.addEventListener("cancel", (e) => e.preventDefault());  // Esc doesn't close it
    pop.addEventListener("close", () => {
      if (closing || !items.length) return;
      setTimeout(() => { if (items.length && !pop.open) pop.showModal(); }, 0);  // closed some other way: back it comes
    });
    document.body.append(pop);
    return pop;
  }

  function draw() {
    const n = items.length;
    dialog().replaceChildren(
      el("div", { class: "panel-label" }, "PHISHING TEST"),
      el("h3", { id: "hx-pop-title" }, n === 1 ? "Hoxhunt test in your Inbox" : `${n} Hoxhunt tests in your Inbox`),
      el("p", { class: "muted" }, n === 1 ? "Report it with the Hoxhunt button in Outlook. Don't open its links or reply. This goes away once it's reported."
        : "Report each one with the Hoxhunt button in Outlook. Don't open their links or reply. Each goes away once it's reported."),
      el("ul", { class: "list hx-pop-list" }, items.map((i) => el("li", { class: "hx-pop-row" },
        el("div", { class: "hx-pop-time" }, clock(i.ts)),
        el("div", { class: "grow hx-pop-subj" }, i.subject),
        el("div", { class: "hx-pop-acts" },
          el("button", { type: "button", class: "primary hx-pop-open", onclick: () => openItem(i) }, "Open in Outlook to report"),
          el("button", { type: "button", class: "small", onclick: () => doneItem(i) }, "Already reported"),
          el("button", { type: "button", class: "small subtle", onclick: () => snoozeItem(i) }, `Remind me in ${snoozeMinutes} min`))))));
  }

  function show() {
    if (!items.length) { hide(); return; }
    draw();
    if (!pop.open) { pop.showModal(); pop.querySelector(".hx-pop-open")?.focus(); }
  }

  function hide() {
    if (pop?.open) { closing = true; pop.close(); closing = false; }
  }

  function drop(i) {
    items = items.filter((x) => x.id !== i.id);
    sig = JSON.stringify(items.map((x) => x.id));
    show();
  }

  // Outlook comes in front with the email; the pop-up steps aside for 15 minutes and is back if it's still there.
  async function openItem(i) {
    await guarded(async () => {
      await api("/api/outlook/open", { id: i.mail_id });
      const r = await api("/api/hoxhunt/snooze", { id: i.id, minutes: snoozeMinutes });
      drop(i);
      arm(r.wake);
    });
  }

  async function doneItem(i) {
    await guarded(async () => { await api("/api/hoxhunt/done", { id: i.id }); drop(i); CC.refreshAlerts?.(); });
  }

  async function snoozeItem(i) {
    await guarded(async () => { const r = await api("/api/hoxhunt/snooze", { id: i.id, minutes: snoozeMinutes }); drop(i); arm(r.wake); });
  }

  function arm(wake) {
    if (!wake) return;
    if (wakeTimer && wakeTimer.at <= wake) return;
    clearTimeout(wakeTimer?.id);
    const ms = wake * 1000 - Date.now() + 1500;
    wakeTimer = { at: wake, id: setTimeout(() => { wakeTimer = null; poll(); }, Math.max(1000, Math.min(ms, 2 ** 31 - 1))) };
  }

  let polling = false;
  async function poll() {
    if (polling) return;
    polling = true;
    try {
      const res = await fetch("/api/hoxhunt/pending");
      if (res.status === 401) { items = []; hide(); return; }  // session ended
      if (!res.ok) return;
      const r = await res.json();
      snoozeMinutes = r.snooze_minutes || snoozeMinutes;
      arm(r.next_wake);
      const s = JSON.stringify(r.items.map((x) => x.id));
      if (s === sig && !!pop?.open === !!r.items.length) return;
      items = r.items;
      sig = s;
      show();
    } catch { /* server restarting: the next poll tries again */ } finally { polling = false; }
  }

  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  window.addEventListener("focus", () => poll());
  setInterval(poll, 30000);
  CC.hoxhuntPoll = poll;  // the desktop app calls it when it brings the window forward for a new test
  poll();
})();
