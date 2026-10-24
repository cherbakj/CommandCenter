// Command Center dashboard. All content is inserted as text (never innerHTML) because
// mail subjects/bodies and web page values are untrusted.

const $ = (sel) => document.querySelector(sel);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "class") node.className = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c !== null && c !== undefined && c !== false) node.append(c instanceof Node ? c : String(c));
  }
  return node;
}

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CommandCenter": "1" },
    body: JSON.stringify(body),
  };
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || data.detail || res.statusText);
  return data;
}

function when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const sameDay = d.toDateString() === new Date().toDateString();
  return sameDay ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
                 : d.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function fill(list, items, emptyText) {
  list.replaceChildren(...(items.length ? items : [el("li", { class: "empty" }, emptyText)]));
}

async function guarded(fn) {
  try { await fn(); } catch (e) { showMessage("Error", e.message); }
}

// ---- dialog ------------------------------------------------------------------

function showDialog(...content) {
  $("#dialog-body").replaceChildren(...content);
  if (!$("#dialog").open) $("#dialog").showModal();
}

function showMessage(title, text) {
  showDialog(el("h3", {}, title), el("p", {}, text), el("div", { class: "row" }, el("button", {}, "OK")));
}

function confirmDialog(title, text, okLabel = "OK") {
  return new Promise((resolve) => {
    const d = $("#dialog");
    showDialog(el("h3", {}, title), el("p", {}, text),
      el("div", { class: "row" }, el("button", { value: "no" }, "Cancel"),
        el("button", { value: "yes", class: "primary" }, okLabel)));
    d.addEventListener("close", () => resolve(d.returnValue === "yes"), { once: true });
  });
}

// ---- tabs --------------------------------------------------------------------

const loaders = {};
document.querySelectorAll("nav button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll("nav button, .tab").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    $(`#tab-${b.dataset.tab}`).classList.add("active");
    loaders[b.dataset.tab]?.();
  }));

function openTab(name) { document.querySelector(`nav button[data-tab="${name}"]`).click(); }

// ---- alerts ------------------------------------------------------------------

function alertAction(a) {
  const act = a.action;
  if (!act) return null;
  if (act.type === "today") return el("button", { class: "small", onclick: () => openTab("today") }, "Open Today");
  if (act.type === "mail") return el("button", { class: "small", onclick: () => { openTab("mail"); showMail(act.id); } }, "View mail");
  if (act.type === "url") return el("a", { href: act.url, target: "_blank", rel: "noopener" }, el("button", { class: "small" }, "Open site"));
  if (act.type === "meeting") return [
    act.join_url && el("a", { href: act.join_url, target: "_blank", rel: "noopener" }, el("button", { class: "small primary" }, "Join")),
    el("button", { class: "small", onclick: () => guarded(() => api("/api/outlook/open", { id: act.id })) }, "Open"),
  ];
  return null;
}

loaders.alerts = () => guarded(async () => {
  const { alerts } = await api(`/api/alerts?unread_only=${$("#alerts-unread").checked}`);
  fill($("#alerts-list"), alerts.map((a) => el("li", { class: a.read ? "" : "unread" },
    el("span", { class: `prio ${a.priority}` }),
    el("div", { class: "grow" },
      el("div", { class: "title" }, a.title),
      a.body && el("div", {}, a.body),
      el("div", { class: "muted" }, `${a.source} · ${when(a.ts)}`)),
    el("div", { class: "actions" }, alertAction(a),
      !a.read && el("button", { class: "small subtle", onclick: () => guarded(async () => { await api("/api/alerts/read", { id: a.id }); refresh(); }) }, "✓")),
  )), "No alerts yet");
});

$("#alerts-unread").addEventListener("change", loaders.alerts);
$("#alerts-readall").addEventListener("click", () => guarded(async () => { await api("/api/alerts/read", {}); refresh(); }));
$("#alerts-clear").addEventListener("click", () => guarded(async () => {
  if (await confirmDialog("Clear alerts", "Delete all alerts from the feed?", "Clear")) { await api("/api/alerts/clear", {}); refresh(); }
}));
$("#reload").addEventListener("click", () => guarded(async () => { await api("/api/reload", {}); showMessage("Reloaded", "config.yaml was reloaded."); }));

// ---- mail --------------------------------------------------------------------

let selectedMail = null;

loaders.mail = () => guarded(async () => {
  const { mail } = await api(`/api/mail?unread_only=${$("#mail-unread").checked}`);
  fill($("#mail-list"), mail.map((m) => el("li", {
      class: `clickable ${m.unread ? "unread" : ""} ${m.id === selectedMail ? "selected" : ""}`,
      "data-id": m.id,
      onclick: () => showMail(m.id),
    },
    el("div", { class: "grow" },
      el("div", { class: "title" }, m.high_importance ? "❗ " : "", m.flagged ? el("span", { class: "flag" }, "⚑ ") : "", m.subject),
      el("div", {}, m.sender_name),
      el("div", { class: "muted" }, when(m.received), " · ", m.preview.slice(0, 120))),
  )), "No mail");
});

async function showMail(id) {
  selectedMail = id;
  const view = $("#mail-view");
  view.className = "panel";
  view.replaceChildren(el("div", { class: "muted" }, "Loading…"));
  await guarded(async () => {
    const m = await api(`/api/mail/item?id=${encodeURIComponent(id)}`);
    if (m.unread) {
      await api("/api/mail/read", { id, read: true });
      m.unread = false;
      loaders.mail();
    }
    const act = (path, body, msg) => guarded(async () => { await api(path, body); if (msg) showMessage("Done", msg); loaders.mail(); showMail(id); });
    view.replaceChildren(
      el("h2", {}, m.subject),
      el("div", {}, `${m.sender_name} <${m.sender}>`),
      el("div", { class: "muted" }, `To: ${m.to}${m.cc ? "  ·  Cc: " + m.cc : ""}  ·  ${when(m.received)}`),
      el("div", { class: "actions", style: "margin:10px 0" },
        el("button", { class: "primary", onclick: () => replyDialog(m, false) }, "Reply"),
        el("button", { onclick: () => replyDialog(m, true) }, "Reply all"),
        el("button", { onclick: () => act("/api/mail/read", { id, read: m.unread }) }, m.unread ? "Mark read" : "Mark unread"),
        el("button", { onclick: () => act("/api/mail/flag", { id, flagged: !m.flagged }) }, m.flagged ? "Clear flag" : "Flag"),
        el("button", { onclick: () => newTaskDialog({ subject: m.subject, notes: `From ${m.sender_name} <${m.sender}>, ${when(m.received)}` }) }, "+ Task"),
        el("button", { onclick: () => guarded(() => api("/api/outlook/open", { id })) }, "Open in Outlook")),
      el("pre", {}, m.body),
    );
    document.querySelectorAll("#mail-list li").forEach((li) => li.classList.toggle("selected", li.dataset.id === id));
  });
}

function replyDialog(m, replyAll) {
  const text = el("textarea", { placeholder: "Your reply…" });
  const send = async (sendNow) => guarded(async () => {
    if (!text.value.trim()) return;
    await api("/api/mail/reply", { id: m.id, text: text.value, reply_all: replyAll, send: sendNow });
    $("#dialog").close();
    showMessage(sendNow ? "Sent" : "Draft opened", sendNow ? "Your reply was sent from Outlook." : "Review and send it in Outlook.");
  });
  showDialog(
    el("h3", {}, `${replyAll ? "Reply all" : "Reply"}: ${m.subject}`), text,
    el("div", { class: "row" },
      el("button", { value: "cancel" }, "Cancel"),
      el("button", { type: "button", onclick: () => send(false) }, "Open draft in Outlook"),
      el("button", { type: "button", class: "primary", onclick: () => send(true) }, "Send")));
  text.focus();
}

$("#mail-unread").addEventListener("change", loaders.mail);
$("#mail-refresh").addEventListener("click", loaders.mail);

// ---- calendar ----------------------------------------------------------------

loaders.calendar = () => guarded(async () => {
  const { meetings } = await api("/api/meetings?hours=24");
  fill($("#cal-list"), meetings.map((m) => el("li", {},
    el("div", { class: "grow" },
      el("div", { class: "title" }, m.subject),
      el("div", {}, `${when(m.start)} – ${new Date(m.end).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`),
      el("div", { class: "muted" }, [m.location, m.organizer].filter(Boolean).join(" · "))),
    el("div", { class: "actions" },
      m.join_url && el("a", { href: m.join_url, target: "_blank", rel: "noopener" }, el("button", { class: "small primary" }, "Join")),
      el("button", { class: "small", onclick: () => guarded(() => api("/api/outlook/open", { id: m.id })) }, "Open")),
  )), "Nothing in the next 24 hours");
});
$("#cal-refresh").addEventListener("click", loaders.calendar);

// ---- websites ----------------------------------------------------------------

loaders.websites = () => guarded(async () => {
  const { monitors } = await api("/api/websites");
  fill($("#web-list"), monitors.map((m) => {
    const s = m.state || {};
    const statusText = !m.enabled && m.enabled !== undefined ? el("span", { class: "muted" }, "disabled")
      : !s.checked ? el("span", { class: "muted" }, "not checked yet")
      : s.ok ? el("span", { class: "good" }, "OK") : el("span", { class: "err" }, s.login_needed ? "login needed" : "error");
    return el("li", {},
      el("div", { class: "grow" },
        el("div", { class: "title" }, m.name, "  ", statusText),
        el("div", {}, "Value: ", s.value ?? "—"),
        s.ok === false && el("div", { class: "err" }, s.error),
        el("div", { class: "muted" }, `${m.url} · profile: ${m.profile || "public"} · alert on ${m.alert_on || "change"}${m.threshold != null ? " " + m.threshold : ""} · every ${m.interval_minutes || 15} min`),
        el("div", { class: "muted" }, s.checked ? `Checked ${when(s.checked)}` : "", s.changed ? ` · last change ${when(s.changed)}` : "")),
      el("div", { class: "actions" },
        el("button", { class: "small", onclick: (e) => checkSite(e.target, m.name) }, "Check now"),
        el("button", { class: "small", onclick: () => loginSite(m) }, "Log in"),
        el("a", { href: m.url, target: "_blank", rel: "noopener" }, el("button", { class: "small subtle" }, "Open"))));
  }), "No monitors in config.yaml");
});

async function checkSite(button, name) {
  button.disabled = true; button.textContent = "Checking…";
  await guarded(() => api("/api/websites/check", { name }));
  loaders.websites();
}

async function loginSite(m) {
  if (!await confirmDialog("Log in", `A browser window will open for profile "${m.profile || "public"}". Log in, then close that window to save the session.`, "Open browser")) return;
  guarded(() => api("/api/websites/login", { name: m.name }));
}
$("#web-refresh").addEventListener("click", loaders.websites);

// ---- sql ---------------------------------------------------------------------

let sqlInfo = { connections: [], queries: [] };
let allTables = [];

function renderResult(r, label) {
  $("#sql-meta").textContent = `${label} · ${r.rows.length} row(s)${r.truncated ? " (first 1000)" : ""} · ${r.seconds}s`;
  $("#sql-table").replaceChildren(
    el("thead", {}, el("tr", {}, r.columns.map((c) => el("th", {}, c)))),
    el("tbody", {}, r.rows.map((row) => el("tr", {}, row.map((v) => el("td", { title: v ?? "" }, v ?? "NULL"))))));
}

function renderParams() {
  const q = sqlInfo.queries.find((x) => x.name === $("#sql-query").value);
  $("#sql-params").replaceChildren(
    ...(q?.description ? [el("div", { class: "muted", style: "margin-bottom:6px" }, q.description)] : []),
    ...(q?.params || []).map((p) => el("input", { placeholder: p, "data-param": "1", onkeydown: (e) => e.key === "Enter" && $("#sql-run").click() })));
}

loaders.sql = () => guarded(async () => {
  sqlInfo = await api("/api/sql");
  $("#sql-query").replaceChildren(...sqlInfo.queries.map((q) => el("option", { value: q.name }, q.name)));
  $("#sql-browse-conn").replaceChildren(el("option", { value: "" }, "Choose a connection…"),
    ...sqlInfo.connections.map((c) => el("option", { value: c.name }, c.name)));
  renderParams();
  $("#sql-conns").replaceChildren(...sqlInfo.connections.map((c) => el("div", { class: "card" },
    el("div", { class: "title" }, c.name),
    el("div", { class: "muted" }, `${c.server} / ${c.database} · ${c.auth === "windows" ? "Windows login" : c.username}`),
    el("div", { class: c.password_saved ? "good" : "err" }, c.password_saved ? "Password saved" : "No password saved"),
    el("div", { class: "actions", style: "margin-top:6px" },
      c.auth !== "windows" && el("button", { class: "small", onclick: () => passwordDialog(c) }, "Set password"),
      el("button", { class: "small", onclick: () => guarded(async () => renderResult(await api("/api/sql/test", { connection: c.name }), `Connection test: ${c.name}`)) }, "Test")))));
});

function passwordDialog(c) {
  const input = el("input", { type: "password", placeholder: "Password", autocomplete: "off" });
  showDialog(el("h3", {}, `Password for ${c.name} (${c.username})`),
    el("p", { class: "muted" }, "Stored in Windows Credential Manager, not in any file."), input,
    el("div", { class: "row" }, el("button", { value: "cancel" }, "Cancel"),
      el("button", { type: "button", class: "primary", onclick: () => guarded(async () => {
        await api("/api/sql/password", { connection: c.name, password: input.value });
        $("#dialog").close(); loaders.sql();
      }) }, "Save")));
  input.focus();
}

$("#sql-query").addEventListener("change", renderParams);
$("#sql-run").addEventListener("click", () => guarded(async () => {
  const name = $("#sql-query").value;
  if (!name) return;
  const params = [...document.querySelectorAll("#sql-params [data-param]")].map((i) => i.value);
  $("#sql-meta").textContent = "Running…";
  renderResult(await api("/api/sql/run", { query: name, params }), name);
}));

function renderTables() {
  const f = $("#sql-filter").value.toLowerCase();
  const conn = $("#sql-browse-conn").value;
  fill($("#sql-tables"), allTables.filter((t) => t.toLowerCase().includes(f)).slice(0, 300).map((t) =>
    el("li", { class: "clickable", onclick: () => guarded(async () => {
      $("#sql-meta").textContent = "Loading…";
      renderResult(await api(`/api/sql/preview?connection=${encodeURIComponent(conn)}&table=${encodeURIComponent(t)}`), `Top 100 from ${t}`);
    }) }, t)), conn ? "No tables" : "Choose a connection");
}
$("#sql-browse-conn").addEventListener("change", () => guarded(async () => {
  const conn = $("#sql-browse-conn").value;
  allTables = conn ? (await api(`/api/sql/tables?connection=${encodeURIComponent(conn)}`)).tables : [];
  renderTables();
}));
$("#sql-filter").addEventListener("input", renderTables);

// ---- today -------------------------------------------------------------------

const WORKDAY = { start: 8, end: 17 };
let todayData = null;
let asanaConnected = false;

const isoDate = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const timeOf = (iso) => new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

function tickClock() {
  const now = new Date();
  const [hm, ampm] = now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }).split(" ");
  $("#clock").replaceChildren(hm, el("span", { class: "sec" }, String(now.getSeconds()).padStart(2, "0")),
    ampm ? el("span", { class: "ampm" }, ampm) : "");
  $("#date-line").textContent = `${now.toLocaleDateString([], { weekday: "long" })} // ${now.toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" })}`;
  const hours = now.getHours() + now.getMinutes() / 60;
  const pct = Math.round(Math.min(1, Math.max(0, (hours - WORKDAY.start) / (WORKDAY.end - WORKDAY.start))) * 100);
  $("#xp-pct").textContent = `${pct}%`;
  $("#xp-fill").style.width = `${pct}%`;
}

function segments(value, max, hot = false, n = 12) {
  const on = Math.round(Math.min(1, Math.max(0, value / max)) * n);
  return el("div", { class: `segments ${hot ? "hot" : ""}` }, Array.from({ length: n }, (_, i) => el("i", { class: i < on ? "on" : "" })));
}

function meter(label, valueText, value, max, hot) {
  return el("div", {}, el("div", { class: "meter-head" }, el("span", {}, label), el("b", {}, valueText)), segments(value, max, hot));
}

function renderWeather(w) {
  const panel = $("#weather");
  if (w.error) {
    panel.replaceChildren(el("div", { class: "panel-label" }, "ENV.WEATHER"), el("p", { class: "err" }, w.error));
    return;
  }
  panel.replaceChildren(
    el("div", { class: "panel-label" }, `ENV.WEATHER // ${w.location} ${w.zip}`),
    el("div", { class: "weather-main" },
      el("div", { class: "w-icon" }, w.icon),
      el("div", {},
        el("div", { class: "w-temp" }, w.temp, el("small", {}, w.unit)),
        el("div", { class: "w-desc" }, w.text),
        el("div", { class: "w-loc" }, `FEELS ${w.feels_like}° · H ${w.high}° / L ${w.low}° · ☀ ${timeOf(w.sunrise)} ☾ ${timeOf(w.sunset)}`)),
      el("div", { class: "w-meters" },
        meter("Rain chance", `${w.rain_chance ?? 0}%`, w.rain_chance ?? 0, 100, false),
        meter("Humidity", `${w.humidity}%`, w.humidity, 100, false),
        meter("Wind", `${w.wind} ${w.wind_unit}`, w.wind, 30, w.wind >= 20))),
    el("div", { class: "hours" }, w.hours.map((h) => el("div", { class: "hour" },
      el("div", {}, new Date(h.time).toLocaleTimeString([], { hour: "numeric" })),
      el("div", { class: "hi", title: h.text }, h.icon),
      el("b", {}, `${h.temp}°`),
      el("div", { class: "rain" }, `${h.rain ?? 0}% rain`)))));
}

function dueTag(due) {
  if (!due) return null;
  const today = isoDate(new Date());
  if (due < today) return el("span", { class: "tag overdue" }, "Overdue");
  if (due === today) return el("span", { class: "tag today" }, "Today");
  return null;
}

function dueText(due) {
  return due ? `Due ${new Date(due + "T00:00").toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })}` : "No due date";
}

function renderStats(d) {
  const agenda = Array.isArray(d.agenda) ? d.agenda : [];
  const left = agenda.filter((m) => new Date(m.end) > new Date()).length;
  const today = isoDate(new Date());
  const due = d.tasks.items.filter((t) => t.due && t.due <= today).length;
  const f = d.followups.error ? {} : d.followups;
  const fu = ["needs_reply", "awaiting_reply", "flagged", "unread_vip"].reduce((n, k) => n + (f[k]?.length || 0), 0);
  const stat = (n, label, color) => el("div", { class: "stat", style: `--c:${color}` }, el("div", { class: "n" }, n), el("div", { class: "l" }, label));
  $("#stats").replaceChildren(
    stat(left, "Meetings left today", "var(--cyan)"),
    stat(due, "Tasks due / overdue", due ? "var(--amber)" : "var(--lime)"),
    stat(f.needs_reply?.length || 0, "Need your reply", "var(--magenta)"),
    stat(fu, "Follow-ups total", "var(--cyan)"),
    stat(Math.max(0, lastUnread), "Unread alerts", lastUnread > 0 ? "var(--red)" : "var(--lime)"));
}

function renderAgenda(agenda) {
  if (agenda.error) { fill($("#agenda"), [el("li", { class: "err" }, agenda.error)], ""); return; }
  const now = new Date();
  $("#agenda-count").textContent = `${agenda.length} EVENT${agenda.length === 1 ? "" : "S"}`;
  fill($("#agenda"), agenda.map((m) => {
    const start = new Date(m.start), end = new Date(m.end);
    const live = start <= now && now < end;
    return el("li", { class: live ? "now" : end <= now ? "past" : "" },
      el("div", { class: "time-col" }, timeOf(m.start), el("div", { class: "muted" }, timeOf(m.end))),
      el("div", { class: "grow" },
        el("div", { class: "title" }, m.subject, live && el("span", { class: "tag live" }, "Live")),
        el("div", { class: "muted" }, [m.location, m.organizer].filter(Boolean).join(" · "))),
      el("div", { class: "actions" },
        m.join_url && end > now && el("a", { href: m.join_url, target: "_blank", rel: "noopener" }, el("button", { class: "small primary" }, "Join")),
        el("button", { class: "small", onclick: () => guarded(() => api("/api/outlook/open", { id: m.id })) }, "Open")));
  }), "No meetings today. Clear skies.");
}

function renderTasks(t) {
  asanaConnected = t.asana.connected;
  const btn = $("#asana-btn");
  btn.textContent = asanaConnected ? "Asana ✓" : "Connect Asana";
  btn.className = `small ${asanaConnected ? "" : "subtle"}`;
  const notes = [t.error && `Outlook: ${t.error}`, t.asana.error && `Asana: ${t.asana.error}`].filter(Boolean);
  $("#tasks-note").replaceChildren(...notes.map((n) => el("div", { class: "err" }, n)));
  fill($("#tasks"), t.items.map((task) => el("li", {},
    el("button", { class: "check", title: "Mark complete", onclick: () => completeTask(task) }),
    el("div", { class: "grow" },
      el("div", { class: "title" }, task.subject, el("span", { class: `tag ${task.source}` }, task.source === "asana" ? "Asana" : "Outlook"), dueTag(task.due)),
      el("div", { class: "muted" }, dueText(task.due), task.project ? ` · ${task.project}` : "")),
    el("div", { class: "actions" },
      task.url ? el("a", { href: task.url, target: "_blank", rel: "noopener" }, el("button", { class: "small subtle" }, "Open"))
               : el("button", { class: "small subtle", onclick: () => guarded(() => api("/api/outlook/open", { id: task.id })) }, "Open")),
  )), "No open tasks. Inbox zero energy.");
}

async function completeTask(task) {
  if (!await confirmDialog("Complete task", `Mark "${task.subject}" complete?`, "Complete")) return;
  await guarded(async () => { await api("/api/tasks/complete", { source: task.source, id: task.id }); loaders.today(); });
}

function fuGroup(title, items, render, note, empty) {
  return el("div", { class: "fu-group" },
    el("h4", {}, title, el("span", { class: "chip" }, items.length)),
    el("ul", { class: "list" }, items.length ? items.map(render) : el("li", { class: "empty" }, empty)),
    note && el("div", { class: "fu-note" }, note));
}

const fuActions = (id, sender, ...buttons) => el("div", { class: "actions" }, ...buttons,
  sender && el("button", { class: "icon subtle", title: `Mute ${sender}`, onclick: () => muteSender(sender) }, "Mute"),
  el("button", { class: "icon subtle", title: "Dismiss", onclick: () => dismissFollowup(id) }, "✕"));

const viewMail = (id) => el("button", { class: "small", onclick: () => { openTab("mail"); showMail(id); } }, "View");

function renderFollowups(f) {
  const box = $("#followups");
  if (f.error) { box.replaceChildren(el("div", { class: "err" }, f.error)); return; }
  const h = f.flagged_hidden || {};
  const hiddenNote = [h.more && `${h.more} more`, h.older && `${h.older} older`, h.automated && `${h.automated} automated`]
    .filter(Boolean).join(" · ");
  box.replaceChildren(
    fuGroup("Needs your reply", f.needs_reply, (m) => el("li", { class: m.unread ? "unread" : "" },
      el("div", { class: "grow" }, el("div", { class: "title" }, m.subject),
        el("div", { class: "muted" }, `${m.sender_name} · ${when(m.received)}`)),
      fuActions(m.id, m.sender || m.sender_name, viewMail(m.id),
        el("button", { class: "small primary", onclick: () => replyDialog(m, false) }, "Reply"))),
      null, "Nothing waiting on you."),
    fuGroup("Awaiting their reply", f.awaiting_reply, (m) => el("li", {},
      el("div", { class: "grow" }, el("div", { class: "title" }, m.subject),
        el("div", { class: "muted" }, `To ${m.to.slice(0, 60)} · sent ${when(m.sent)}`)),
      fuActions(m.id, null,
        el("button", { class: "small", title: "Opens a follow-up draft in Outlook", onclick: () => nudge(m) }, "Nudge"))),
      null, "No one owes you a reply."),
    fuGroup("Flagged", f.flagged, (m) => el("li", {},
      el("div", { class: "grow" }, el("div", { class: "title" }, m.subject, dueTag(m.due)),
        el("div", { class: "muted" }, `${m.sender_name} · ${when(m.received)}`)),
      fuActions(m.id, m.sender_name, viewMail(m.id),
        el("button", { class: "small", onclick: () => completeTask({ source: "outlook", id: m.id, subject: m.subject }) }, "Done"))),
      hiddenNote && `Hidden: ${hiddenNote} (of ${h.total} flagged)`, "No recent flags."),
    fuGroup("Unread from VIPs", f.unread_vip, (m) => el("li", { class: "unread" },
      el("div", { class: "grow" }, el("div", { class: "title" }, m.subject),
        el("div", { class: "muted" }, `${m.sender_name} · ${when(m.received)}`)),
      fuActions(m.id, m.sender || m.sender_name, viewMail(m.id))),
      null, "No unread VIP mail."));
}

async function nudge(m) {
  await guarded(async () => {
    await api("/api/mail/reply", { id: m.id, text: "Hi, just following up on this. Any update?", reply_all: true, send: false });
    showMessage("Draft opened", "A follow-up draft is open in Outlook. Edit and send it there.");
  });
}

async function dismissFollowup(id) {
  await guarded(async () => { await api("/api/followups/dismiss", { id }); loaders.today(); });
}

async function muteSender(sender) {
  if (!await confirmDialog("Mute sender", `Hide follow-ups from "${sender}"? You can undo this under Muted senders.`, "Mute")) return;
  await guarded(async () => { await api("/api/followups/mute", { sender }); loaders.today(); });
}

async function mutedDialog() {
  await guarded(async () => {
    const { muted } = await api("/api/followups/muted");
    showDialog(el("h3", {}, "Muted senders"),
      muted.length ? el("ul", { class: "list" }, muted.map((s) => el("li", {},
        el("div", { class: "grow mono" }, s),
        el("button", { type: "button", class: "small", onclick: async () => { await api("/api/followups/unmute", { sender: s }); mutedDialog(); loaders.today(); } }, "Unmute"))))
        : el("p", { class: "muted" }, "No muted senders. Use Mute on a follow-up to hide that sender."),
      el("div", { class: "row" }, el("button", {}, "Close")));
  });
}

function newTaskDialog(prefill = {}) {
  const subject = el("input", { placeholder: "What needs doing?", value: prefill.subject || "" });
  const due = el("input", { type: "date", value: prefill.due || "" });
  const notes = el("textarea", { placeholder: "Notes (optional)" });
  notes.value = prefill.notes || "";
  const dest = el("select", {},
    el("option", { value: "outlook" }, "Outlook / To Do"),
    el("option", { value: "asana", disabled: !asanaConnected }, asanaConnected ? "Asana" : "Asana (not connected)"));
  const save = () => guarded(async () => {
    if (!subject.value.trim()) { subject.focus(); return; }
    await api("/api/tasks", { source: dest.value, subject: subject.value, due: due.value || null, notes: notes.value });
    $("#dialog").close();
    loaders.today();
  });
  showDialog(el("h3", {}, "New task"),
    el("label", { class: "field-label" }, "Title"), subject,
    el("label", { class: "field-label" }, "Due date"), due,
    el("label", { class: "field-label" }, "Create in"), dest,
    el("label", { class: "field-label" }, "Notes"), notes,
    el("div", { class: "row" }, el("button", { value: "cancel", class: "subtle" }, "Cancel"),
      el("button", { type: "button", class: "primary", onclick: save }, "Create task")));
  subject.focus();
}

function asanaDialog() {
  if (asanaConnected) {
    confirmDialog("Disconnect Asana", "Remove the saved Asana token from this PC?", "Disconnect").then((yes) =>
      yes && guarded(async () => { await api("/api/asana/disconnect", {}); loaders.today(); }));
    return;
  }
  const token = el("input", { type: "password", placeholder: "Paste your Asana personal access token", autocomplete: "off" });
  showDialog(el("h3", {}, "Connect Asana"),
    el("ol", { class: "muted" },
      el("li", {}, "Open Asana → click your profile picture → Settings"),
      el("li", {}, "Apps tab → Manage developer apps (or Developer console)"),
      el("li", {}, "Create new token → name it “Command Center” → copy it")),
    token,
    el("p", { class: "muted" }, "Stored in Windows Credential Manager, not in any file."),
    el("div", { class: "row" }, el("button", { value: "cancel", class: "subtle" }, "Cancel"),
      el("button", { type: "button", class: "primary", onclick: () => guarded(async () => {
        const r = await api("/api/asana/token", { token: token.value });
        showMessage("Asana connected", `Signed in as ${r.name}.`);
        loaders.today();
      }) }, "Connect")));
  token.focus();
}

loaders.today = (refresh = false) => guarded(async () => {
  const btn = $("#today-refresh");
  btn.disabled = true; btn.textContent = "Scanning…";
  try {
    todayData = await api(`/api/today?refresh=${refresh}`);
    renderWeather(todayData.weather);
    renderAgenda(todayData.agenda);
    renderTasks(todayData.tasks);
    renderFollowups(todayData.followups);
    renderStats(todayData);
  } finally {
    btn.disabled = false; btn.textContent = "↻ Rescan";
  }
});

$("#today-refresh").addEventListener("click", () => loaders.today(true));
$("#new-task").addEventListener("click", () => newTaskDialog());
$("#asana-btn").addEventListener("click", asanaDialog);
$("#muted-btn").addEventListener("click", mutedDialog);
setInterval(tickClock, 1000);
setInterval(() => { if ($("#tab-today").classList.contains("active")) loaders.today(); }, 120000);

// ---- status + polling --------------------------------------------------------

let lastUnread = -1;
async function refresh() {
  try {
    const [{ unread }, st] = await Promise.all([api("/api/alerts?unread_only=true"), api("/api/status")]);
    $("#badge").hidden = unread === 0;
    $("#badge").textContent = unread;
    document.title = unread ? `(${unread}) Command Center` : "Command Center";
    const sites = Object.values(st.websites).filter(Boolean);
    const sitesOk = sites.every((s) => s.ok);
    $("#status").replaceChildren(
      el("span", {}, el("span", { class: `dot ${st.outlook ? (st.outlook.ok ? "ok" : "bad") : ""}` }), "Outlook"),
      el("span", {}, el("span", { class: `dot ${sites.length ? (sitesOk ? "ok" : "bad") : ""}` }), "Websites"));
    if (unread !== lastUnread && $("#tab-alerts").classList.contains("active")) loaders.alerts();
    lastUnread = unread;
  } catch { $("#status").textContent = "Server not reachable"; }
}

tickClock();
refresh().then(() => loaders.today());
setInterval(refresh, 5000);
