// End-of-day carry over (today.py carry_over): the wrap-up alert's "Carry over…", Ctrl+K "End of day: carry over" and
// Needs you > Manage ▾. Lists what's still open, all ticked: replies owed (snoozed to the next workday's start, so
// they come back first thing as "Back from snooze"), tasks due today or overdue (moved to that day) and Pulse tickets
// waiting on Joe (listed; they can't be moved). One button does the ticked ones; the next briefing starts with it.
(() => {
  const CC = window.CC;
  if (!CC) return;
  const { el, api } = CC;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = "/static/carryover.css";
  document.head.append(link);

  const pad = (n) => String(n).padStart(2, "0");
  const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const localIso = (d) => `${ymd(d)}T${pad(d.getHours())}:${pad(d.getMinutes())}:00`;

  function nextWorkday() {
    const d = new Date();
    d.setDate(d.getDate() + 1);
    while (d.getDay() === 0 || d.getDay() === 6) d.setDate(d.getDate() + 1);
    const start = CC.workday?.start ?? 8;
    d.setHours(Math.floor(start), Math.round((start % 1) * 60), 0, 0);
    return d;
  }

  function checkRow(label, sub, checked = true) {
    const box = el("input", { type: "checkbox", checked });
    return { box, li: el("li", { class: "co-row" }, el("label", {}, box, el("span", { class: "co-text" }, el("b", {}, label), sub && el("span", { class: "muted" }, sub)))) };
  }

  async function open() {
    let data = CC.todayData;
    try { if (!data) data = await api("/api/today"); } catch (e) { CC.showMessage("Carry over", e.message); return; }
    const f = data.followups && !data.followups.error ? data.followups : {};
    const today = ymd(new Date());
    const seen = new Set();
    const mail = [...(f.urgent_reply || []), ...(f.back || []), ...(f.needs_reply || []), ...(f.teams_reply || [])]
      .filter((m) => m && m.id && !seen.has(m.id) && seen.add(m.id));
    const tasks = (data.tasks?.items || []).filter((t) => t.due && t.due <= today);
    let tickets = [];
    try {
      const r = await api("/api/pulse/tickets?mine=true&status=open&limit=200");
      tickets = (r.tickets || []).filter((t) => t.waiting_on_me);
    } catch { /* Pulse not set up or not reachable: left out */ }

    const target = nextWorkday();
    const dayName = target.toLocaleDateString([], { weekday: "long" });
    const mailRows = mail.map((m) => ({ m, ...checkRow(m.subject, m.from_name || m.sender_name || m.sender) }));
    const taskRows = tasks.map((t) => ({ t, ...checkRow(t.subject, `${t.source === "asana" ? "Asana" : "Outlook"} · due ${t.due < today ? t.due : "today"}`) }));
    const btn = el("button", { type: "button", class: "primary" });
    const count = () => mailRows.filter((r) => r.box.checked).length + taskRows.filter((r) => r.box.checked).length;
    const label = () => { const n = count(); btn.textContent = n ? `Carry over ${n}` : "Nothing ticked"; btn.disabled = !n; };
    [...mailRows, ...taskRows].forEach((r) => r.box.addEventListener("change", label));
    label();
    const err = el("div", { class: "err", role: "alert" });
    btn.onclick = async () => {
      btn.disabled = true;
      btn.textContent = "Carrying over…";
      try {
        const r = await api("/api/carryover", {
          mail: mailRows.filter((x) => x.box.checked).map(({ m }) => ({ id: m.id, subject: m.subject, who: m.from_name || m.sender_name || "" })),
          tasks: taskRows.filter((x) => x.box.checked).map(({ t }) => ({ source: t.source, id: t.id, subject: t.subject })),
          until: localIso(target), day: ymd(target), tickets: tickets.length });
        CC.$("#dialog").close();
        const parts = [r.mail && `${r.mail} email${r.mail === 1 ? "" : "s"} back ${dayName} morning`, r.tasks && `${r.tasks} task${r.tasks === 1 ? "" : "s"} due ${dayName}`].filter(Boolean);
        if (r.failed?.length) CC.showMessage("Carry over", `${parts.join(", ") || "Nothing moved"}. Not moved: ${r.failed.join(", ")}.`);
        else CC.toast(`Carried over: ${parts.join(", ")}`);
        CC.loaders.today?.();
        CC.loaders.mail?.();
      } catch (e) { err.textContent = e.message; label(); }
    };
    const section = (title, rows, note) => rows.length && el("section", { class: "co-sec" }, el("h4", {}, title, el("span", { class: "chip" }, String(rows.length))),
      note && el("p", { class: "muted co-note" }, note), el("ul", { class: "co-list" }, rows.map((r) => r.li)));
    const empty = !mail.length && !tasks.length && !tickets.length;
    CC.wideDialog(el("div", { class: "co" },
      el("h3", {}, `Carry over to ${dayName}`),
      empty ? el("p", {}, "Nothing open: no replies owed, no tasks due, no tickets waiting. Done for the day.")
        : el("p", { class: "muted" }, `What's still open. Ticked items move to ${dayName}, and the morning briefing starts with them.`),
      section("Replies owed", mailRows, `Snoozed until ${target.toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })}: back at the top of Needs you then.`),
      section("Tasks due today or overdue", taskRows, `Due date moved to ${dayName} (in Outlook / Asana).`),
      tickets.length && el("section", { class: "co-sec" }, el("h4", {}, "Pulse tickets waiting on you", el("span", { class: "chip" }, String(tickets.length))),
        el("p", { class: "muted co-note" }, "These stay as they are; the briefing reminds you."),
        el("ul", { class: "co-list" }, tickets.slice(0, 15).map((t) => el("li", { class: "co-row" },
          el("span", { class: "co-text" }, el("b", {}, `${t.ticket_no ? "#" + t.ticket_no + " " : ""}${t.title}`),
            el("span", { class: "muted" }, t.age_days == null ? "" : t.age_days === 0 ? "new today" : `waiting ${t.age_days} business day${t.age_days === 1 ? "" : "s"}`)))))),
      err,
      el("div", { class: "row" }, el("button", { value: "cancel", class: "subtle" }, empty ? "Close" : "Cancel"), !empty && btn)));
    if (!empty) btn.focus();
  }

  CC.registerAlertAction("wrapup", () => el("button", { type: "button", class: "small primary", onclick: open }, "Carry over…"));
  CC.addPaletteSource(() => [{ group: "Commands", label: "End of day: carry over", hint: "move what's open to the next workday", run: () => setTimeout(open, 50) }]);
  const mount = () => {
    const box = document.querySelector("#fu-manage");
    if (box && !box.querySelector(".co-manage")) box.append(el("button", { type: "button", class: "small subtle co-manage", onclick: open }, "End of day: carry over"));
  };
  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", mount, { once: true }) : mount();
  CC.carryOver = open;
})();
