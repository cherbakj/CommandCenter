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

// Like guarded(), but shows the error inside the open dialog (keeps what the user typed).
async function guardedIn(errBox, fn) {
  errBox.textContent = "";
  try { await fn(); } catch (e) { errBox.textContent = e.message; }
}

const pad2 = (n) => String(n).padStart(2, "0");
// Local time as "YYYY-MM-DDTHH:MM:SS" (what the backend expects: no timezone suffix).
const localIso = (d) => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}T${pad2(d.getHours())}:${pad2(d.getMinutes())}:00`;
const localInput = (d) => localIso(d).slice(0, 16);  // value for <input type="datetime-local">
const roundUp = (d, minutes) => { const ms = minutes * 60000; return new Date(Math.ceil(d.getTime() / ms) * ms); };
const dayTime = (d) => d.toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const duration = (mins) => mins >= 60 ? `${Math.floor(mins / 60)}h${mins % 60 ? " " + (mins % 60) + "m" : ""}` : `${mins}m`;

// Opens a web / Teams link through Windows (one tab stop, works the same in the desktop window and a browser).
function linkButton(url, label, cls = "small") {
  return el("button", { type: "button", class: cls, onclick: () => openExternal(url) }, label);
}

// Short, non-blocking confirmation in the corner.
function toast(text) {
  let box = $("#toast");
  if (!box) { box = el("div", { id: "toast", role: "status", "aria-live": "polite" }); document.body.append(box); }
  box.textContent = text;
  box.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => box.classList.remove("show"), 3500);
}

// ---- dialog ------------------------------------------------------------------

function showDialog(...content) {
  $("#dialog-body").replaceChildren(...content.flat().filter((c) => c !== null && c !== undefined && c !== false && c !== ""));
  if (!$("#dialog").open) $("#dialog").showModal();
}

// Enter in a dialog text field runs the dialog's main (primary) action. It never "submits" the
// <form method="dialog">: that would press the first button (often Cancel) and lose what was typed.
const NO_ENTER_ACTION = new Set(["checkbox", "radio", "range", "color", "file", "button", "submit", "reset", "image"]);
function primaryFor(node, root) {
  // The closest primary button wins (e.g. the Snooze button next to the custom time), else the dialog's last one.
  for (let n = node.parentElement; n && root.contains(n); n = n.parentElement) {
    const found = [...n.querySelectorAll("button.primary")].filter((b) => !b.disabled && b.offsetParent !== null);
    if (found.length) return found[found.length - 1];
    if (n === root) break;
  }
  return null;
}
$("#dialog").addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || e.isComposing || e.defaultPrevented || e.target.tagName !== "INPUT") return;
  e.preventDefault();
  if (!NO_ENTER_ACTION.has(e.target.type)) primaryFor(e.target, $("#dialog"))?.click();
});

// Something modal is up (a dialog, the restart overlay, the launch splash): global shortcuts stay quiet.
const modalOpen = () => !!document.querySelector("dialog[open], .restart-overlay, .sp-overlay, [aria-modal='true']");

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

// Two panes: COMMS (left: messaging and alerts) and WORK (right: Claude, VS Code, tools). Each pane has its
// own active tab and its own nav group in the header. Below SPLIT_MIN_WIDTH (or when Joe picks "Single")
// only one pane is shown at a time and the app behaves like a single tabbed page.

const loaders = {};

const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { v === null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* storage blocked */ } },
};

const SPLIT_MIN_WIDTH = 1600;
const SPLIT_DEFAULT = 0.44;
const PANE_OF = {
  today: "comms", alerts: "comms", mail: "comms", calendar: "comms", teams: "comms",
  projects: "work", vscode: "work", toolkit: "work", sql: "work", pulse: "work", websites: "work",
};
const TAB_ORDER = { comms: ["today", "alerts", "mail", "calendar", "teams"], work: ["projects", "vscode", "toolkit", "sql", "pulse", "websites"] };
const TAB_ID = /^[a-z0-9_-]{1,40}$/i;

const layout = (() => {
  let saved = {};
  try { saved = JSON.parse(store.get("cc.layout.tabs") || "{}") || {}; } catch { saved = {}; }
  const pick = (v, d) => (typeof v === "string" && TAB_ID.test(v) ? v : d);
  return {
    split: false,
    active: { comms: pick(saved.comms, "today"), work: pick(saved.work, "projects") },
    shown: saved.shown === "work" ? "work" : "comms",   // the pane shown in single mode
    visible: new Set(),
    ready: false,
  };
})();

const navButton = (id) => document.querySelector(`.nav-group button[data-tab="${CSS.escape(id)}"]`);
const paneOf = (id) => navButton(id)?.closest(".nav-group")?.dataset.pane || PANE_OF[id] || null;
const tabsIn = (pane) => [...document.querySelectorAll(`#pane-${pane} .pane-body > section.tab`)].map((s) => s.id.slice(4));

function saveLayoutTabs() {
  store.set("cc.layout.tabs", JSON.stringify({ comms: layout.active.comms, work: layout.active.work, shown: layout.shown }));
}

function wantSplit() {
  return window.innerWidth >= SPLIT_MIN_WIDTH && store.get("cc.layout.mode") !== "single";
}

// Puts the classes in line with the layout state. Returns the tabs that just became visible.
function applyLayout() {
  layout.split = wantSplit();
  const main = $("#workspace");
  main.classList.toggle("ws-split", layout.split);
  main.classList.toggle("ws-single", !layout.split);
  document.body.classList.toggle("split-view", layout.split);
  const nowVisible = new Set();
  for (const pane of ["comms", "work"]) {
    const ids = tabsIn(pane);
    if (!ids.includes(layout.active[pane])) layout.active[pane] = ids[0] || layout.active[pane];
    const show = layout.split || layout.shown === pane;
    $(`#pane-${pane}`).classList.toggle("shown", show);
    for (const id of ids) {
      const on = show && id === layout.active[pane];
      $(`#tab-${id}`).classList.toggle("active", on);
      navButton(id)?.classList.toggle("active", on);
      navButton(id)?.setAttribute("aria-current", on ? "page" : "false");
      if (on) nowVisible.add(id);
    }
  }
  const fresh = [...nowVisible].filter((id) => !layout.visible.has(id));
  layout.visible = nowVisible;
  const toggle = $("#layout-toggle");
  toggle.hidden = window.innerWidth < SPLIT_MIN_WIDTH;
  toggle.textContent = layout.split ? "◧ Split" : "▭ Single";
  toggle.setAttribute("aria-pressed", String(layout.split));
  toggle.title = layout.split ? "Two panes: COMMS left, WORK right. Click for a single pane." : "One pane at a time. Click to show COMMS and WORK side by side.";
  if (layout.split) applySplit();
  document.dispatchEvent(new CustomEvent("cc:layout", { detail: { split: layout.split } }));
  return fresh;
}

function runLoaders(ids) { for (const id of ids) loaders[id]?.(); }

function openTab(name) {
  const pane = paneOf(name);
  if (!pane || !$(`#tab-${name}`)) return;
  layout.active[pane] = name;
  layout.shown = pane;
  saveLayoutTabs();
  const fresh = applyLayout();
  runLoaders(new Set([name, ...fresh]));  // the clicked tab always reloads, as before
}

document.querySelector(".navs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-tab]");
  if (b) openTab(b.dataset.tab);
});

$("#layout-toggle").addEventListener("click", () => {
  store.set("cc.layout.mode", layout.split ? "single" : "split");
  runLoaders(applyLayout());
});

window.addEventListener("resize", () => {
  if (!layout.ready) return;
  if (wantSplit() !== layout.split || $("#layout-toggle").hidden !== (window.innerWidth < SPLIT_MIN_WIDTH)) runLoaders(applyLayout());
  else if (layout.split) applySplit();
});

// The nav groups stay on the header row while they fit; when they don't (big text size, narrow window)
// they move to a second header row instead of scrolling out of sight.
let headerFrame = 0;
function fitHeader() {
  cancelAnimationFrame(headerFrame);
  headerFrame = requestAnimationFrame(() => {
    const h = document.querySelector("header"), n = document.querySelector(".navs");
    const overflows = () => n.scrollWidth > n.clientWidth + 1;
    // 1) Labels on the right ("Search", "Outlook", "Teams") when everything fits on one row.
    // 2) Otherwise drop those labels to keep one row. 3) Otherwise the navs take a second row,
    //    which leaves room for the labels again.
    h.classList.remove("nav-wrap", "hdr-tight");
    if (overflows()) {
      h.classList.add("hdr-tight");
      if (overflows()) { h.classList.remove("hdr-tight"); h.classList.add("nav-wrap"); }
    }
    navFade();
  });
}
// A narrow window scrolls the nav sideways: fade the edge that has more tabs behind it.
function navFade() {
  const n = document.querySelector(".navs");
  n.classList.toggle("fade-r", n.scrollLeft + n.clientWidth < n.scrollWidth - 2);
  n.classList.toggle("fade-l", n.scrollLeft > 2);
}
document.querySelector(".navs").addEventListener("scroll", navFade, { passive: true });
(() => {
  const ro = new ResizeObserver(fitHeader);
  ro.observe(document.querySelector("header"));
  ro.observe(document.querySelector(".header-right"));
  document.querySelectorAll(".nav-group").forEach((n) => ro.observe(n));
})();

// ---- splitter between the panes ------------------------------------------------

let splitFrac = (() => { const v = parseFloat(store.get("cc.layout.split")); return v >= 0.1 && v <= 0.9 ? v : SPLIT_DEFAULT; })();

function splitBounds() {
  const main = $("#workspace");
  const w = main.clientWidth;
  const rem = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
  const sw = $("#splitter").offsetWidth || 8;
  const minL = Math.min(28 * rem, w * 0.4), minR = Math.min(34 * rem, w * 0.45);
  return { w, sw, minL, maxL: Math.max(minL, w - sw - minR) };
}

function applySplit() {
  const { w, minL, maxL } = splitBounds();
  if (!w) return;
  const left = Math.round(Math.max(minL, Math.min(maxL, splitFrac * w)));
  $("#workspace").style.setProperty("--left", `${left}px`);
  const sp = $("#splitter");
  sp.setAttribute("aria-valuemin", String(Math.round((minL / w) * 100)));
  sp.setAttribute("aria-valuemax", String(Math.round((maxL / w) * 100)));
  sp.setAttribute("aria-valuenow", String(Math.round((left / w) * 100)));
  sp.setAttribute("aria-valuetext", `COMMS ${Math.round((left / w) * 100)}%, WORK ${100 - Math.round((left / w) * 100)}%`);
}

function setSplit(frac, save = true) {
  const { w, minL, maxL } = splitBounds();
  if (!w) return;
  splitFrac = Math.max(minL, Math.min(maxL, frac * w)) / w;
  applySplit();
  if (save) store.set("cc.layout.split", splitFrac.toFixed(4));
}

(() => {
  const sp = $("#splitter");
  let drag = null, frame = 0;
  sp.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    sp.setPointerCapture(e.pointerId);
    const r = $("#workspace").getBoundingClientRect();
    drag = { id: e.pointerId, left: r.left, w: r.width, offset: e.clientX - sp.getBoundingClientRect().left };
    document.body.classList.add("resizing");
    sp.focus({ preventScroll: true });
  });
  sp.addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const x = e.clientX;
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => setSplit((x - drag.offset - drag.left) / drag.w, false));
  });
  const end = (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    drag = null;
    document.body.classList.remove("resizing");
    store.set("cc.layout.split", splitFrac.toFixed(4));
  };
  sp.addEventListener("pointerup", end);
  sp.addEventListener("pointercancel", end);
  sp.addEventListener("lostpointercapture", end);
  sp.addEventListener("dblclick", () => setSplit(SPLIT_DEFAULT));
  sp.addEventListener("keydown", (e) => {
    const step = e.shiftKey ? 0.1 : 0.02;
    const map = { ArrowLeft: splitFrac - step, ArrowRight: splitFrac + step, Home: 0, End: 1, Enter: SPLIT_DEFAULT };
    if (!(e.key in map)) return;
    e.preventDefault();
    setSplit(map[e.key]);
  });
  new ResizeObserver(() => { if (layout.split) applySplit(); }).observe($("#workspace"));
})();

// ---- "Working on" project (right pane) ----------------------------------------

const proj = { list: [], path: store.get("cc.activeProject") || "" };
const samePath = (a, b) => String(a || "").toLowerCase() === String(b || "").toLowerCase();
const baseName = (p) => String(p || "").split(/[\\/]/).filter(Boolean).pop() || String(p || "");

function activeProject() {
  if (!proj.path) return null;
  const p = proj.list.find((x) => samePath(x.path, proj.path));
  return { path: p ? p.path : proj.path, name: p ? p.name : baseName(proj.path) };
}

function renderProjectSelect() {
  const sel = $("#project-select");
  const opts = proj.list.map((p) => el("option", { value: p.path, title: p.path }, p.name));
  if (proj.path && !proj.list.some((p) => samePath(p.path, proj.path))) {
    opts.unshift(el("option", { value: proj.path, title: proj.path }, `${baseName(proj.path)} (not in list)`));
  }
  sel.replaceChildren(el("option", { value: "" }, proj.list.length ? "Choose a project…" : "No projects found"), ...opts);
  const cur = activeProject();
  sel.value = cur ? cur.path : "";
  $("#project-path").textContent = cur ? cur.path : "Pick the project Claude and VS Code work in";
  $("#project-path").title = cur ? cur.path : "";
}

function setActiveProject(path) {
  path = String(path || "");
  if (samePath(path, proj.path)) return;
  proj.path = path;
  store.set("cc.activeProject", path || null);
  renderProjectSelect();
  const cur = activeProject();
  document.dispatchEvent(new CustomEvent("cc:project", { detail: cur ? { path: cur.path, name: cur.name } : { path: "", name: "" } }));
}

async function loadProjectList() {
  try { proj.list = ((await api("/api/projects")).projects || []).filter((p) => p && p.path); } catch { /* keep the old list */ }
  renderProjectSelect();
}

$("#project-select").addEventListener("change", (e) => setActiveProject(e.target.value));
$("#project-refresh").addEventListener("click", loadProjectList);

// ---- alerts ------------------------------------------------------------------

function alertAction(a) {
  const act = a.action;
  if (!act) return null;
  if (act.type === "today") return el("button", { class: "small", onclick: () => openTab("today") }, "Open Today");
  if (act.type === "mail") return el("button", { class: "small", onclick: () => { openTab("mail"); showMail(act.id); } }, "View mail");
  if (act.type === "url") return linkButton(act.url, "Open site");
  if (act.type === "meeting") return [
    act.join_url && linkButton(act.join_url, "Join", "small primary"),
    act.prep && el("button", { class: "small alt", onclick: () => prepDialog(act.id) }, "Prep"),
    el("button", { class: "small", onclick: () => guarded(() => api("/api/outlook/open", { id: act.id })) }, "Open"),
  ];
  if (act.type === "order" && act.number) return el("button", { class: "small alt", onclick: () => orderDialog(String(act.number)) }, "Show order");
  return null;
}

function alertRow(a, compact = false) {
  return el("li", { class: `alert-row ${a.read ? "" : "unread"}` },
    el("span", { class: `prio ${a.priority}` }),
    el("div", { class: "grow" },
      el("div", { class: "title" }, a.title),
      a.body && el("div", { class: compact ? "fu-preview" : "" }, a.body),
      el("div", { class: "muted src-line" }, srcTag(alertSource(a), a.source), `${a.source} · ${when(a.ts)}`)),
    el("div", { class: "actions" }, alertAction(a),
      !a.read && el("button", { class: "small subtle", title: "Mark read", "aria-label": `Mark "${a.title}" read`,
        onclick: () => guarded(async () => { await api("/api/alerts/read", { id: a.id }); refresh(); loadTodayAlerts(); }) }, "✓")));
}

loaders.alerts = () => guarded(async () => {
  const { alerts } = await api(`/api/alerts?unread_only=${$("#alerts-unread").checked}`);
  fill($("#alerts-list"), alerts.map((a) => alertRow(a)), "No alerts yet");
});

// The newest alerts, on Today (unread first, then by time).
async function loadTodayAlerts() {
  try {
    const { alerts, unread } = await api("/api/alerts?unread_only=false");
    const rows = alerts.slice(0, 40).sort((a, b) => (a.read ? 1 : 0) - (b.read ? 1 : 0)).slice(0, 12);
    fill($("#today-alerts"), rows.map((a) => alertRow(a, true)), "No alerts yet");
    $("#today-alerts-count").textContent = unread ? `${unread} UNREAD` : "ALL READ";
  } catch (e) {
    fill($("#today-alerts"), [el("li", { class: "err" }, e.message)], "");
  }
}
$("#today-alerts-all").addEventListener("click", () => openTab("alerts"));

$("#alerts-unread").addEventListener("change", loaders.alerts);
$("#alerts-readall").addEventListener("click", () => guarded(async () => { await api("/api/alerts/read", {}); refresh(); }));
$("#alerts-clear").addEventListener("click", () => guarded(async () => {
  if (await confirmDialog("Clear alerts", "Delete all alerts from the feed?", "Clear")) { await api("/api/alerts/clear", {}); refresh(); }
}));
$("#reload").addEventListener("click", () => guarded(async () => { await api("/api/reload", {}); showMessage("Reloaded", "config.yaml was reloaded."); }));

// ---- mail --------------------------------------------------------------------

let selectedMail = null;

// ---- sourcing: every item says where it came from ----------------------------
const SOURCES = {
  outlook: ["Outlook", "Outlook email"], sent: ["Sent", "Outlook Sent Items"], flagged: ["Flagged", "Outlook flagged email"],
  teams: ["Teams", "Microsoft Teams (from Teams' missed-activity email in Outlook)"], calendar: ["Calendar", "Outlook calendar"],
  meeting: ["Teams mtg", "Outlook calendar · Teams meeting"], todo: ["To Do", "Outlook Tasks / Microsoft To Do"],
  asana: ["Asana", "Asana"], pulse: ["Pulse", "Pulse (pulse.sonance.com)"], sql: ["SQL", "SQL Server"],
  website: ["Website", "Website monitor"], system: ["System", "Command Center"],
};
function srcTag(kind, detail = "") {
  const [label, where] = SOURCES[kind] || SOURCES.system;
  return el("span", { class: `src src-${SOURCES[kind] ? kind : "system"}`, title: detail ? `${where} · ${detail}` : where }, label);
}
// "Name <email>" as the visible sender line, so the origin of every message is explicit.
const fromLine = (name, email) => (email && name && !name.includes(email) ? `${name} <${email}>` : name || email || "");
const ALERT_SOURCES = { Outlook: "outlook", Calendar: "calendar", Teams: "teams", Pulse: "pulse", Website: "website",
  Order: "sql", Toolkit: "sql", Snooze: "outlook", "Follow-up": "sent", Urgent: "outlook" };
function alertSource(a) {
  if (a.source === "Urgent" && /teams/i.test(a.title)) return "teams";
  if (a.source === "Outlook" && /teams message/i.test(a.title)) return "teams";
  return ALERT_SOURCES[a.source] || "system";
}

loaders.mail = () => guarded(async () => {
  const { mail } = await api(`/api/mail?unread_only=${$("#mail-unread").checked}`);
  fill($("#mail-list"), mail.map((m) => el("li", {
      class: `clickable ${m.unread ? "unread" : ""} ${m.id === selectedMail ? "selected" : ""}`,
      "data-id": m.id,
      onclick: () => showMail(m.id),
    },
    el("div", { class: "grow" },
      el("div", { class: "title" }, m.high_importance ? "❗ " : "", m.flagged ? el("span", { class: "flag" }, "⚑ ") : "", m.subject),
      el("div", { class: "src-line" }, srcTag("outlook", "Inbox"), fromLine(m.sender_name, m.sender)),
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
        el("button", { onclick: () => snoozeDialog({ id, subject: m.subject, who: m.sender_name, kind: "mail" }) }, "Snooze"),
        m.sender && el("button", { class: "alt", onclick: () => openTeamsChat(m.sender, "") }, "Chat in Teams"),
        el("button", { onclick: () => guarded(() => api("/api/outlook/open", { id })) }, "Open in Outlook")),
      orderChips,
      el("pre", {}, m.body),
    );
    document.querySelectorAll("#mail-list li").forEach((li) => li.classList.toggle("selected", li.dataset.id === id));
    renderOrderChips(orderChips, `${m.subject}\n${m.body}`, id);
  });
}

// ---- order numbers ---------------------------------------------------------------

const orderChips = el("div", { class: "order-chips" });
let orderInfo = null, orderInfoAt = 0;

async function orderStatus() {
  if (!orderInfo || Date.now() - orderInfoAt > 60000) {
    orderInfo = await api("/api/orders/status");
    orderInfoAt = Date.now();
  }
  return orderInfo;
}

// The pattern comes from config.yaml as a Python regex. Most syntax is the same in JS; translate the rest.
function pyRegex(src) {
  let s = String(src || ""), flags = "gi";
  const inline = /^\(\?([aiLmsux]+)\)/.exec(s);
  if (inline) {
    s = s.slice(inline[0].length);
    if (inline[1].includes("s")) flags += "s";
    if (inline[1].includes("m")) flags += "m";
  }
  s = s.replace(/\(\?P<(\w+)>/g, "(?<$1>").replace(/\(\?P=(\w+)\)/g, "\\k<$1>").replace(/\\A/g, "^").replace(/\\Z/g, "$");
  try { return new RegExp(s, flags); } catch { return null; }
}

function findOrders(text, re) {
  const out = [], seen = new Set();
  // Digits inside links (tracking ids, safelinks) and email addresses are never order numbers.
  const plain = String(text).slice(0, 200000)
    .replace(/\b(?:https?:\/\/|www\.)\S+/gi, (s) => " ".repeat(s.length))
    .replace(/[^\s<>()"',;:]+@[^\s<>()"',;:]+/g, (s) => " ".repeat(s.length));
  let rx;
  try { rx = new RegExp(re.source, re.flags.includes("d") ? re.flags : re.flags + "d"); } catch { rx = re; }
  for (const m of plain.matchAll(rx)) {
    const n = m[1] ?? m[0];
    if (!n) continue;
    // A number glued to letters or more digits is part of something else (tracking number, part code).
    const [s, e] = m.indices?.[m[1] !== undefined ? 1 : 0] || [m.index, m.index + n.length];
    if (/[A-Za-z0-9]/.test(plain[s - 1] || "") || /[A-Za-z0-9]/.test(plain[e] || "")) continue;
    // 2296106 and 0002296106 are the same order: keep the first form seen (the subject comes first).
    const key = n.replace(/^0+(?=\d)/, "");
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(n);
    if (out.length >= 8) break;
  }
  return out;
}

async function renderOrderChips(box, text, mailId) {
  box.replaceChildren();
  let info;
  try { info = await orderStatus(); } catch { return; }
  const re = pyRegex(info.pattern);
  if (!re || selectedMail !== mailId) return;
  box.replaceChildren(...findOrders(text, re).map((n) =>
    el("button", { type: "button", class: "order-chip", title: `Look up order ${n} in SQL`, onclick: () => orderDialog(n) },
      el("span", { class: "k" }, "Order"), el("b", {}, n), el("span", { class: "go" }, "→ Look up"))));
}

async function orderDialog(number) {
  const d = $("#dialog");
  d.classList.add("wide");
  const close = el("div", { class: "row" }, el("button", {}, "Close"));
  showDialog(el("h3", {}, `Order ${number}`), el("p", { class: "muted" }, "Looking it up…"));
  let info;
  try { info = await orderStatus(); } catch (e) { showDialog(el("h3", {}, `Order ${number}`), el("p", { class: "err" }, e.message), close); return; }
  if (info.problem) {
    showDialog(el("h3", {}, `Order ${number}`),
      el("p", {}, "Order lookup isn't set up yet."), el("p", { class: "setup" }, info.problem),
      el("div", { class: "row" },
        el("button", { type: "button", class: "primary", onclick: () => { d.close(); openTab("sql"); } }, "Go to SQL tab"),
        el("button", {}, "Close")));
    return;
  }
  try {
    const r = await api(`/api/orders/lookup?number=${encodeURIComponent(number)}`);
    if (!d.open) return;
    if (r.error) throw new Error(r.error);
    showDialog(el("h3", {}, `Order ${r.number}`),
      el("div", { class: "muted" }, `${r.query} · ${r.rows.length} row(s)${r.truncated ? " (first 1000)" : ""} · ${r.seconds}s`),
      r.rows.length
        ? el("div", { class: "table-wrap dialog-table" }, el("table", {},
            el("thead", {}, el("tr", {}, r.columns.map((c) => el("th", {}, c)))),
            el("tbody", {}, r.rows.map((row) => el("tr", {}, row.map((v) => el("td", { title: v ?? "" }, v ?? "NULL")))))))
        : el("p", {}, `No order found for ${r.number}.`),
      close);
  } catch (e) {
    if (d.open) showDialog(el("h3", {}, `Order ${number}`), el("p", { class: "err" }, e.message), close);
  }
}

function replyDialog(m, replyAll) {
  const text = el("textarea", { placeholder: "Your reply…", "aria-label": "Reply text" });
  const remind = el("select", { id: "remind-days" },
    [0, 1, 2, 3, 5].map((n) => el("option", { value: n }, n ? `${n} day${n > 1 ? "s" : ""}` : "Don't remind me")));
  const err = el("div", { class: "err", role: "alert" });
  const send = async (sendNow) => guardedIn(err, async () => {
    if (!text.value.trim()) { text.focus(); return; }
    const days = sendNow ? Number(remind.value) : 0;
    await api("/api/mail/reply", { id: m.id, text: text.value, reply_all: replyAll, send: sendNow, remind_days: days });
    $("#dialog").close();
    showMessage(sendNow ? "Sent" : "Draft opened", sendNow
      ? `Your reply was sent from Outlook.${days ? ` If nobody replies within ${days} day${days > 1 ? "s" : ""}, you'll get an alert.` : ""}`
      : "Review and send it in Outlook.");
  });
  showDialog(
    el("h3", {}, `${replyAll ? "Reply all" : "Reply"}: ${m.subject}`), text,
    el("div", { class: "inline-field" },
      el("label", { for: "remind-days" }, "Remind me if no reply in"), remind,
      el("span", { class: "muted" }, "(when sent from here)")),
    err,
    el("div", { class: "row" },
      el("button", { value: "cancel" }, "Cancel"),
      el("button", { type: "button", onclick: () => send(false) }, "Open draft in Outlook"),
      el("button", { type: "button", class: "primary", onclick: () => send(true) }, "Send")));
  text.focus();
}

$("#mail-unread").addEventListener("change", loaders.mail);
$("#mail-refresh").addEventListener("click", loaders.mail);

// ---- calendar ----------------------------------------------------------------

let calMeetings = [];

loaders.calendar = () => guarded(async () => {
  const { meetings } = await api("/api/meetings?hours=24");
  calMeetings = meetings;
  fill($("#cal-list"), meetings.map((m) => el("li", {},
    el("div", { class: "grow" },
      el("div", { class: "title" }, m.subject),
      el("div", {}, `${when(m.start)} – ${new Date(m.end).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`),
      el("div", { class: "muted src-line" }, srcTag(m.join_url ? "meeting" : "calendar"), [m.location, m.organizer && `organizer ${m.organizer}`].filter(Boolean).join(" · "))),
    el("div", { class: "actions" },
      m.join_url && linkButton(m.join_url, "Join", "small primary"),
      el("button", { class: "small alt", onclick: () => prepDialog(m.id, m) }, "Prep"),
      el("button", { class: "small", onclick: () => guarded(() => api("/api/outlook/open", { id: m.id })) }, "Open")),
  )), "Nothing in the next 24 hours");
});

// ---- meeting prep ------------------------------------------------------------------

const RESPONSE = {
  accepted: ["yes", "✓", "Accepted"], organizer: ["yes", "★", "Organizer"], tentative: ["maybe", "?", "Tentative"],
  declined: ["no", "✕", "Declined"], "no response": ["none", "…", "No response"], none: ["none", "…", "No response"],
};

function followupsInvolving(attendees) {
  const f = todayData && todayData.followups && !todayData.followups.error ? todayData.followups : {};
  const keys = attendees.flatMap((a) => [a.email, a.name]).filter((k) => k && k.length >= 4).map((k) => k.toLowerCase());
  const hit = (...vals) => vals.some((v) => { const s = String(v || "").toLowerCase(); return s && keys.some((k) => s.includes(k)); });
  return [
    ["Needs your reply", f.needs_reply, (m) => hit(m.sender, m.sender_name), (m) => m.sender_name],
    ["Awaiting their reply", f.awaiting_reply, (m) => hit(m.to), (m) => `to ${m.to}`],
    ["Flagged", f.flagged, (m) => hit(m.sender, m.sender_name), (m) => m.sender_name],
    ["Unread from VIPs", f.unread_vip, (m) => hit(m.sender, m.sender_name), (m) => m.sender_name],
  ].flatMap(([group, items, match, who]) => (items || []).filter(match).map((m) => ({ group, who: who(m), ...m })));
}

async function prepDialog(id, meta) {
  const d = $("#dialog");
  d.classList.add("wide");
  showDialog(el("h3", {}, "Meeting prep"), el("p", { class: "muted" }, "Gathering attendees and recent mail…"));
  let p;
  try { p = await api(`/api/meetings/prep?id=${encodeURIComponent(id)}`); } catch (e) {
    showDialog(el("h3", {}, "Meeting prep"), el("p", { class: "err" }, e.message), el("div", { class: "row" }, el("button", {}, "Close")));
    return;
  }
  if (!d.open) return;
  meta = meta || (Array.isArray(todayData?.agenda) ? todayData.agenda : []).concat(calMeetings).find((m) => m.id === id);
  const now = new Date();
  let timeText = "";
  if (meta?.start) {
    const s = new Date(meta.start), e = new Date(meta.end);
    const mins = Math.round((s - now) / 60000);
    timeText = `${s.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })} · ${timeOf(meta.start)} – ${timeOf(meta.end)}` +
      (s <= now && now < e ? " · live now" : mins > 0 && mins < 24 * 60 ? ` · in ${duration(mins)}` : e <= now ? " · ended" : "");
  }
  const counts = {};
  for (const a of p.attendees) { const r = RESPONSE[a.response] || RESPONSE.none; counts[r[2]] = (counts[r[2]] || 0) + 1; }
  const viewMailFromDialog = (mid) => el("button", { type: "button", class: "small", onclick: () => { d.close(); openTab("mail"); showMail(mid); } }, "View");
  const related = followupsInvolving(p.attendees);
  const section = (title, ...body) => el("section", { class: "prep-sec" }, el("h4", {}, title), ...body);

  showDialog(
    el("div", { class: "prep-head" },
      el("div", { class: "grow" },
        el("div", { class: "panel-label" }, "MEETING PREP"),
        el("h3", { class: "prep-title" }, p.subject),
        el("div", { class: "prep-meta" },
          timeText && el("span", {}, el("b", {}, "When "), timeText),
          p.location && el("span", {}, el("b", {}, "Where "), p.location),
          p.organizer && el("span", {}, el("b", {}, "Organizer "), p.organizer))),
      el("div", { class: "actions" },
        p.join_url && linkButton(p.join_url, "Join", "primary"),
        el("button", { type: "button", onclick: () => guarded(() => api("/api/outlook/open", { id })) }, "Open in Outlook"))),
    el("div", { class: "prep-grid" },
      el("div", {},
        section(`Attendees (${p.attendees.length})`,
          el("div", { class: "prep-counts muted" }, Object.entries(counts).map(([k, v]) => `${v} ${k.toLowerCase()}`).join(" · ") || "Just you"),
          el("div", { class: "att-list" }, p.attendees.map((a) => {
            const [cls, sym, label] = RESPONSE[a.response] || RESPONSE.none;
            return el("span", { class: `att ${cls}`, title: `${a.email || a.name} · ${label}${a.optional ? " · optional" : ""}` },
              el("i", { "aria-hidden": "true" }, sym), a.name, a.optional && el("small", {}, "opt"),
              el("span", { class: "sr-only" }, ` (${label}${a.optional ? ", optional" : ""})`));
          }))),
        section(`Open follow-ups with them (${related.length})`,
          related.length ? el("ul", { class: "list" }, related.map((m) => el("li", {},
            el("div", { class: "grow" }, el("div", { class: "title" }, m.subject), el("div", { class: "muted" }, `${m.group} · ${m.who}`)),
            el("div", { class: "actions" }, viewMailFromDialog(m.id)))))
            : el("p", { class: "muted" }, "Nothing open with these people."))),
      el("div", {},
        section(`Recent emails (${p.recent_mail.length})`,
          p.recent_mail.length ? el("ul", { class: "list" }, p.recent_mail.map((m) => el("li", {},
            el("div", { class: "grow" }, el("div", { class: "title" }, m.subject),
              el("div", { class: "muted" }, `${m.folder === "Sent" ? "You → " + m.to : m.sender_name} · ${when(m.received)}`)),
            el("div", { class: "actions" }, viewMailFromDialog(m.id)))))
            : el("p", { class: "muted" }, "No mail with these attendees in the last 3 weeks.")),
        section("Meeting notes", el("pre", { class: "prep-notes" }, p.agenda || "No notes in the invite.")))),
    el("div", { class: "row" }, el("button", {}, "Close")));
}
$("#cal-refresh")?.addEventListener("click", () => loaders.calendar());

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
        linkButton(m.url, "Open", "small subtle")));
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

let WORKDAY = { start: 8, end: 17 };
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
  const weekend = isWeekend(now);
  $(".a-clock .xp")?.classList.toggle("weekend", weekend);
  if (weekend) {
    $("#xp-label").textContent = "WEEKEND · OFF THE CLOCK";
    $("#xp-pct").textContent = "—";
    $("#xp-fill").style.width = "100%";
    return;
  }
  const hours = now.getHours() + now.getMinutes() / 60;
  const pct = Math.round(Math.min(1, Math.max(0, (hours - WORKDAY.start) / (WORKDAY.end - WORKDAY.start))) * 100);
  $("#xp-pct").textContent = `${pct}%`;
  const fmt = (h) => new Date(2000, 0, 1, Math.floor(h), Math.round((h % 1) * 60)).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  $("#xp-label").textContent = `WORKDAY ${fmt(WORKDAY.start)} – ${fmt(WORKDAY.end)}`;
  $("#xp-fill").style.width = `${pct}%`;
}

// Saturday and Sunday have no workday: no progress, no free slots.
const isWeekend = (d = new Date()) => d.getDay() === 0 || d.getDay() === 6;

function segments(value, max, hot = false, n = 12) {
  const on = Math.round(Math.min(1, Math.max(0, value / max)) * n);
  return el("div", { class: `segments ${hot ? "hot" : ""}` }, Array.from({ length: n }, (_, i) => el("i", { class: i < on ? "on" : "" })));
}

function meter(label, valueText, value, max, hot) {
  return el("div", {}, el("div", { class: "meter-head" }, el("span", {}, label), el("b", {}, valueText)), segments(value, max, hot));
}

// Segmented "Lake Forest | San Clemente" switch; the choice is stored on the server.
function weatherToggle(w) {
  const locs = Array.isArray(w.locations) ? w.locations.filter((l) => l && /^\d{5}$/.test(String(l.zip))) : [];
  if (locs.length < 2) return null;
  const current = String(w.selected || w.zip || "");
  return el("div", { class: "seg weather-seg", role: "group", "aria-label": "Weather location" },
    locs.map((l) => el("button", {
      type: "button", class: `seg-btn${String(l.zip) === current ? " on" : ""}`, "aria-pressed": String(String(l.zip) === current),
      title: `Weather for ${l.name} ${l.zip}`,
      onclick: (e) => selectWeather(String(l.zip), e.currentTarget),
    }, l.name)));
}

async function selectWeather(zip, button) {
  if (button) button.disabled = true;
  try {
    const w = await api("/api/weather/select", { zip });
    if (todayData) todayData.weather = w;
    renderWeather(w);
  } catch (e) {
    toast(`Weather: ${e.message}`);
    if (button) button.disabled = false;
  }
}

function renderWeather(w) {
  const panel = $("#weather");
  if (w.error) {
    panel.replaceChildren(el("div", { class: "panel-head weather-head" }, el("span", { class: "panel-label" }, "ENV.WEATHER"), el("span", { class: "spacer" }), weatherToggle(w)),
      el("p", { class: "err" }, w.error));
    return;
  }
  panel.replaceChildren(
    el("div", { class: "panel-head weather-head" },
      el("span", { class: "panel-label" }, `ENV.WEATHER // ${w.location} ${w.zip}`), el("span", { class: "spacer" }), weatherToggle(w)),
    el("div", { class: "weather-main" },
      el("div", { class: "w-icon" }, w.icon),
      el("div", {},
        el("div", { class: "w-temp" }, w.temp, el("small", {}, w.unit)),
        el("div", { class: "w-desc" }, w.text),
        // Each piece wraps as a whole (never "6:06" on one line and "PM" on the next).
        el("div", { class: "w-loc" },
          el("span", {}, `FEELS ${w.feels_like}°`), " · ", el("span", {}, `H ${w.high}° / L ${w.low}°`), " · ",
          el("span", { title: "Sunrise" }, `☀ ${timeOf(w.sunrise)}`), " ", el("span", { title: "Sunset" }, `☾ ${timeOf(w.sunset)}`))),
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
  const fu = ["urgent_reply", "needs_reply", "teams_reply", "awaiting_reply", "flagged", "unread_vip"].reduce((n, k) => n + (f[k]?.length || 0), 0);
  const urgent = (f.urgent_reply?.length || 0) + (f.teams_reply || []).filter((m) => m.urgent).length;
  const stat = (n, label, color, cls = "") => el("div", { class: `stat ${cls}`, style: `--c:${color}` }, el("div", { class: "n" }, n), el("div", { class: "l" }, label));
  $("#stats").replaceChildren(
    stat(urgent, "Urgent replies", urgent ? "var(--red)" : "var(--accent)", urgent ? "alarm" : ""),
    stat(left, "Meetings left today", "var(--accent-2)"),
    stat(due, "Tasks due / overdue", due ? "var(--amber)" : "var(--accent)"),
    stat(f.needs_reply?.length || 0, "Need your reply", "var(--hot)"),
    stat(fu, "Follow-ups total", "var(--accent-2)"),
    stat(Math.max(0, lastUnread), "Unread alerts", lastUnread > 0 ? "var(--red)" : "var(--accent)"));
  // Pulse tile appears once the Pulse connection is set up.
  api("/api/pulse/summary").then((p) => {
    if (!p.configured || p.error) return;
    const tile = stat(p.open_mine, "Pulse tickets on you", p.high_priority ? "var(--red)" : p.open_mine ? "var(--amber)" : "var(--accent)");
    tile.title = `${p.new_today} new today · ${p.high_priority} high priority · ${p.open_total} open`;
    tile.style.cursor = "pointer";
    tile.addEventListener("click", () => openTab("pulse"));
    $("#stats").append(tile);
    balanceStats();
  }).catch(() => {});
  balanceStats();
}

// The stat tiles sit in balanced rows: all on one row if they fit, else two even rows (3+3, 4+3), else
// three, never a lone tile on a row of its own.
const STAT_MIN_REM = 8.2;
function balanceStats() {
  const box = $("#stats");
  const n = box.children.length;
  if (!n || !box.clientWidth) return;
  const rem = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
  const gap = parseFloat(getComputedStyle(box).columnGap) || 0;
  const fits = (cols) => (box.clientWidth - gap * (cols - 1)) / cols >= STAT_MIN_REM * rem;
  let cols = [n, Math.ceil(n / 2), Math.ceil(n / 3)].find(fits) || Math.min(n, 2);
  if (!fits(cols)) cols = Math.max(1, Math.min(n, 2));
  box.style.setProperty("grid-template-columns", `repeat(${cols}, minmax(0, 1fr))`);
  box.style.setProperty("grid-auto-flow", "row");
}
new ResizeObserver(() => balanceStats()).observe($("#stats"));

// Open time today: inside the workday, from now on, between meetings, at least 30 minutes.
function freeSlots(agenda, now = new Date()) {
  if (isWeekend(now)) return [];
  const at = (h) => new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, Math.round(h * 60));
  const dayEnd = at(WORKDAY.end);
  let cursor = new Date(Math.max(at(WORKDAY.start), roundUp(now, 15)));
  const busy = (Array.isArray(agenda) ? agenda : []).map((m) => [new Date(m.start), new Date(m.end)])
    .filter(([s, e]) => !isNaN(s) && !isNaN(e)).sort((a, b) => a[0] - b[0]);
  const slots = [];
  const push = (s, e) => { if (e - s >= 30 * 60000) slots.push({ start: s, end: e }); };
  for (const [s, e] of busy) {
    if (cursor >= dayEnd) break;
    if (s > cursor) push(cursor, new Date(Math.min(s, dayEnd)));
    if (e > cursor) cursor = e;
  }
  if (cursor < dayEnd) push(cursor, dayEnd);
  return slots;
}

let dragTask = null;

function slotRow(slot) {
  const mins = Math.round((slot.end - slot.start) / 60000);
  const drop = (e) => { e.preventDefault(); e.currentTarget.classList.remove("over"); const t = dragTask; endDrag(); if (t) scheduleDialog(t, slot); };
  return el("li", {
      class: "slot",
      ondragover: (e) => { if (!dragTask) return; e.preventDefault(); e.dataTransfer.dropEffect = "copy"; e.currentTarget.classList.add("over"); },
      ondragleave: (e) => e.currentTarget.classList.remove("over"),
      ondrop: drop,
    },
    el("div", { class: "time-col" }, timeOf(slot.start), el("div", { class: "muted" }, timeOf(slot.end))),
    el("div", { class: "grow" },
      el("div", { class: "slot-title" }, `Open · ${duration(mins)}`),
      el("div", { class: "muted slot-hint" }, "Drop a task here to block time")));
}

function endDrag() { dragTask = null; document.body.classList.remove("drag-task"); }

function renderAgenda(agenda) {
  if (agenda.error) { fill($("#agenda"), [el("li", { class: "err" }, agenda.error)], ""); return; }
  const now = new Date();
  $("#agenda-count").textContent = `${agenda.length} EVENT${agenda.length === 1 ? "" : "S"}`;
  const rows = agenda.map((m) => {
    const start = new Date(m.start), end = new Date(m.end);
    const live = start <= now && now < end;
    return { at: start, node: el("li", { class: live ? "now" : end <= now ? "past" : "" },
      el("div", { class: "time-col" }, timeOf(m.start), el("div", { class: "muted" }, timeOf(m.end))),
      el("div", { class: "grow" },
        el("div", { class: "title" }, m.subject, live && el("span", { class: "tag live" }, "Live")),
        el("div", { class: "muted src-line" }, srcTag(m.join_url ? "meeting" : "calendar"), [m.location, m.organizer && `organizer ${m.organizer}`].filter(Boolean).join(" · "))),
      el("div", { class: "actions" },
        m.join_url && end > now && linkButton(m.join_url, "Join", "small primary"),
        end > now && el("button", { class: "small alt", title: "Attendees, recent mail and notes", onclick: () => prepDialog(m.id, m) }, "Prep"),
        el("button", { class: "small", onclick: () => guarded(() => api("/api/outlook/open", { id: m.id })) }, "Open"))) };
  });
  // Ties go to the open slot first: a slot always ends where the next meeting starts.
  rows.push(...freeSlots(agenda, now).map((s) => ({ at: s.start, slot: true, node: slotRow(s) })));
  rows.sort((a, b) => a.at - b.at || (b.slot ? 1 : 0) - (a.slot ? 1 : 0));
  const nodes = rows.map((r) => r.node);
  const weekend = isWeekend(now);
  if (weekend) {
    nodes.unshift(el("li", { class: "weekend-row" },
      el("div", { class: "time-col" }, now.toLocaleDateString([], { weekday: "short" }).toUpperCase()),
      el("div", { class: "grow" },
        el("div", { class: "slot-title" }, "Weekend"),
        el("div", { class: "muted slot-hint" }, agenda.length ? "No work hours today. Meetings below are still on your calendar." : "No work hours and no meetings today."))));
  } else if (!agenda.length) nodes.unshift(el("li", { class: "empty" }, "No meetings today. Clear skies."));
  fill($("#agenda"), nodes, "No meetings today. Clear skies.");
}

function renderTasks(t) {
  asanaConnected = t.asana.connected;
  const btn = $("#asana-btn");
  btn.textContent = asanaConnected ? "Asana ✓" : "Connect Asana";
  btn.className = `small ${asanaConnected ? "" : "subtle"}`;
  const notes = [t.error && `Outlook: ${t.error}`, t.asana.error && `Asana: ${t.asana.error}`].filter(Boolean);
  $("#tasks-note").replaceChildren(...notes.map((n) => el("div", { class: "err" }, n)));
  const today = isoDate(new Date());
  fill($("#tasks"), t.items.map((task) => el("li", {
      class: `task ${task.due && task.due < today ? "overdue" : task.due === today ? "due-today" : ""}`,
      draggable: "true",
      title: "Drag onto an open slot in the agenda to block time for it",
      ondragstart: (e) => {
        dragTask = task;
        e.dataTransfer.effectAllowed = "copy";
        e.dataTransfer.setData("text/plain", task.subject);
        document.body.classList.add("drag-task");
      },
      ondragend: endDrag,
    },
    el("button", { class: "check", title: "Mark complete", "aria-label": `Mark "${task.subject}" complete`, onclick: () => completeTask(task) }),
    el("div", { class: "grow" },
      el("div", { class: "title" }, task.subject, srcTag(task.source === "asana" ? "asana" : "todo", task.project || ""), dueTag(task.due)),
      el("div", { class: "muted" }, dueText(task.due), task.project ? ` · ${task.project}` : "")),
    el("div", { class: "actions" },
      el("button", { class: "small", title: "Block time on your calendar for this", onclick: () => scheduleDialog(task) }, "Schedule"),
      el("button", { class: "small subtle", onclick: () => snoozeDialog({ id: task.id, subject: task.subject, who: task.source === "asana" ? "Asana" : "Outlook", kind: "task" }) }, "Snooze"),
      task.url ? linkButton(task.url, "Open", "small subtle")
               : el("button", { class: "small subtle", onclick: () => guarded(() => api("/api/outlook/open", { id: task.id })) }, "Open")),
  )), "No open tasks. Inbox zero energy.");
}

function scheduleDialog(task, slot) {
  if (!slot) slot = todayData && freeSlots(todayData.agenda)[0];
  const subject = el("input", { value: `Focus: ${task.subject}`, maxlength: "250", "aria-label": "Title" });
  const start = el("input", { type: "datetime-local", step: "300", "aria-label": "Start", value: localInput(slot ? slot.start : roundUp(new Date(), 15)) });
  const fits = slot ? (slot.end - slot.start) / 60000 : 120;
  const pick = fits >= 60 ? 60 : [45, 30].find((m) => m <= fits) || 30;
  const len = el("select", { "aria-label": "Length" }, [30, 45, 60, 90, 120].map((m) => el("option", { value: m, selected: m === pick }, duration(m))));
  const hint = el("div", { class: "muted" });
  const updateHint = () => {
    hint.textContent = /focus/i.test(subject.value)
      ? "“Focus” is in the title, so focus mode turns on during this block."
      : "Tip: put “Focus” in the title to turn on focus mode during this block.";
  };
  subject.addEventListener("input", updateHint);
  updateHint();
  const err = el("div", { class: "err", role: "alert" });
  const save = () => guardedIn(err, async () => {
    if (!subject.value.trim()) { subject.focus(); throw new Error("Give the block a title."); }
    const s = new Date(start.value);
    if (isNaN(s)) throw new Error("Pick a start time.");
    const minutes = Number(len.value);
    await api("/api/calendar/block", {
      subject: subject.value.trim(), start: localIso(s), minutes,
      notes: `Time blocked for: ${task.subject}${task.url ? "\n" + task.url : ""}`,
    });
    $("#dialog").close();
    showMessage("Time blocked", `“${subject.value.trim()}” is on your calendar: ${dayTime(s)} – ${timeOf(new Date(s.getTime() + minutes * 60000))}.`);
    loaders.today();
  });
  showDialog(el("h3", {}, "Block time for a task"),
    el("p", { class: "muted" }, task.subject),
    el("label", { class: "field-label" }, "Title"), subject, hint,
    el("div", { class: "two-col" },
      el("div", {}, el("label", { class: "field-label" }, "Start"), start),
      el("div", {}, el("label", { class: "field-label" }, "Length"), len)),
    err,
    el("div", { class: "row" }, el("button", { value: "cancel", class: "subtle" }, "Cancel"),
      el("button", { type: "button", class: "primary", onclick: save }, "Add to calendar")));
  subject.focus();
}

// ---- snooze + no-reply reminders --------------------------------------------------

function snoozeTimes(now = new Date()) {
  const day = (offset, h) => new Date(now.getFullYear(), now.getMonth(), now.getDate() + offset, h, 0, 0);
  const tomorrow = day(1, 8);
  const later = now.getHours() >= 17 ? tomorrow : roundUp(new Date(now.getTime() + 3 * 3600000), 5);
  return { later, tomorrow, monday: day((8 - now.getDay()) % 7 || 7, 8) };
}

function snoozeDialog(item) {
  const t = snoozeTimes();
  const err = el("div", { class: "err", role: "alert" });
  const custom = el("input", { type: "datetime-local", step: "300", value: localInput(t.tomorrow), "aria-label": "Snooze until" });
  const go = (d) => guardedIn(err, async () => {
    if (isNaN(d) || d <= new Date()) throw new Error("Pick a time in the future.");
    await api("/api/snooze", { id: item.id, until: localIso(d), subject: item.subject || "", who: item.who || "", kind: item.kind });
    $("#dialog").close();
    toast(`Snoozed until ${dayTime(d)}`);
    loaders.today();
  });
  const opt = (label, d) => el("button", { type: "button", class: "snooze-opt", onclick: () => go(d) }, el("b", {}, label), el("span", {}, dayTime(d)));
  showDialog(el("h3", {}, "Snooze"),
    el("p", { class: "muted" }, item.subject),
    el("div", { class: "snooze-opts" },
      opt(t.later === t.tomorrow ? "Later (tomorrow)" : "Later today", t.later),
      opt("Tomorrow", t.tomorrow),
      opt("Monday", t.monday)),
    el("label", { class: "field-label" }, "Or pick a date and time"),
    el("div", { class: "inline-field" }, custom, el("button", { type: "button", class: "primary", onclick: () => go(new Date(custom.value)) }, "Snooze")),
    err,
    el("p", { class: "muted" }, item.kind === "task" ? "The task is hidden from Today until then." : "It's hidden from follow-ups until then, and you get an alert when it's back."),
    el("div", { class: "row" }, el("button", { value: "cancel", class: "subtle" }, "Cancel")));
}

function remindDialog(m) {
  const err = el("div", { class: "err", role: "alert" });
  const go = (days) => guardedIn(err, async () => {
    await api("/api/reply-watch", { id: m.id, days });
    $("#dialog").close();
    toast(`You'll get an alert if there's no reply in ${days} day${days > 1 ? "s" : ""}.`);
    loaders.today();
  });
  showDialog(el("h3", {}, "Remind me if no reply"),
    el("p", { class: "muted" }, `${m.subject} · to ${m.to}`),
    el("div", { class: "snooze-opts" }, [1, 2, 3, 5].map((d) =>
      el("button", { type: "button", class: "snooze-opt", onclick: () => go(d) }, el("b", {}, `${d} day${d > 1 ? "s" : ""}`), el("span", {}, dayTime(new Date(Date.now() + d * 86400000)))))),
    err,
    el("div", { class: "row" }, el("button", { value: "cancel", class: "subtle" }, "Cancel")));
}

async function snoozedDialog() {
  await guarded(async () => {
    const { snoozes, reply_watches: watches } = await api("/api/snoozes");
    const rows = Object.entries(snoozes || {}).sort((a, b) => String(a[1].until).localeCompare(String(b[1].until)));
    const d = $("#dialog");
    const unsnooze = (id) => guarded(async () => { await api("/api/unsnooze", { id }); await snoozedDialog(); loaders.today(); });
    showDialog(el("h3", {}, `Snoozed (${rows.length})`),
      rows.length ? el("ul", { class: "list dialog-list" }, rows.map(([id, s]) => el("li", {},
        el("div", { class: "grow" },
          el("div", { class: "title" }, s.subject || "(no subject)"),
          el("div", { class: "muted" }, [s.kind === "task" ? "Task" : "Mail", s.who, `back ${dayTime(new Date(s.until))}`].filter(Boolean).join(" · "))),
        el("div", { class: "actions" },
          s.kind !== "task" && el("button", { type: "button", class: "small", onclick: () => { d.close(); openTab("mail"); showMail(id); } }, "View"),
          el("button", { type: "button", class: "small primary", onclick: () => unsnooze(id) }, "Unsnooze")))))
        : el("p", { class: "muted" }, "Nothing snoozed. Use Snooze on a follow-up, task or email."),
      el("h3", { class: "gap" }, `No-reply reminders (${(watches || []).length})`),
      watches?.length ? el("ul", { class: "list dialog-list" }, watches.map((w) => el("li", {},
        el("div", { class: "grow" },
          el("div", { class: "title" }, w.subject || "(no subject)"),
          el("div", { class: "muted" }, `To ${String(w.to || "").slice(0, 80)} · alert ${dayTime(new Date(w.due))} if nobody replies`)),
        el("div", { class: "actions" },
          el("button", { type: "button", class: "small", onclick: () => { d.close(); openTab("mail"); showMail(w.id); } }, "View"),
          el("button", { type: "button", class: "small subtle", onclick: () => guarded(async () => {
            await api("/api/reply-watch/cancel", { conversation: w.conversation }); await snoozedDialog(); loaders.today();
          }) }, "Cancel")))))
        : el("p", { class: "muted" }, "None. Pick “Remind me if no reply” when you send a reply, or use Remind me on an awaiting-reply item."),
      el("div", { class: "row" }, el("button", {}, "Close")));
  });
}

function renderSnoozedButton(d) {
  const n = d.snoozed || 0, w = d.reply_watches || 0;
  const btn = $("#snoozed-btn");
  btn.textContent = `Snoozed (${n})${w ? ` · Watching ${w}` : ""}`;
  btn.title = `${n} snoozed · ${w} no-reply reminder${w === 1 ? "" : "s"}`;
}

async function completeTask(task) {
  if (!await confirmDialog("Complete task", `Mark "${task.subject}" complete?`, "Complete")) return;
  await guarded(async () => { await api("/api/tasks/complete", { source: task.source, id: task.id }); loaders.today(); });
}

function fuGroup(title, items, render, note, empty, cls = "") {
  items = items || [];
  return el("div", { class: `fu-group ${cls}` },
    el("h4", {}, title, el("span", { class: "chip" }, items.length)),
    el("ul", { class: "list" }, items.length ? items.map(render) : el("li", { class: "empty" }, empty)),
    note && el("div", { class: "fu-note" }, note));
}

// Every follow-up gets Snooze and Dismiss; Mute only where muting the sender makes sense.
const fuActions = (m, { mute, who, kind = "mail" } = {}, ...buttons) => el("div", { class: "actions" }, ...buttons,
  el("button", { class: "icon subtle", title: "Snooze", onclick: () => snoozeDialog({ id: m.id, subject: m.subject, who: who ?? m.sender_name, kind }) }, "Snooze"),
  mute && el("button", { class: "icon subtle", title: `Mute ${mute}`, onclick: () => muteSender(mute) }, "Mute"),
  el("button", { class: "icon subtle", title: "Dismiss", "aria-label": `Dismiss "${m.subject}"`, onclick: () => dismissFollowup(m.id) }, "✕"));

const viewMail = (id) => el("button", { class: "small", onclick: () => { openTab("mail"); showMail(id); } }, "View");

function replyRow(m, { urgent = false } = {}) {
  return el("li", { class: `${m.unread ? "unread" : ""} ${urgent ? "sev-urgent" : ""}` },
    el("div", { class: "grow" }, el("div", { class: "title" }, m.subject, urgent && el("span", { class: "tag urgent" }, "Urgent")),
      el("div", { class: "muted src-line" }, srcTag("outlook", m.internal ? "internal sender" : "external sender"), `${fromLine(m.sender_name, m.sender)} · ${when(m.received)}`)),
    fuActions(m, { mute: urgent ? null : m.sender || m.sender_name }, viewMail(m.id),
      el("button", { class: "small primary", onclick: () => replyDialog(m, false) }, "Reply"),
      m.sender && el("button", { class: "small alt", title: "Chat in Teams", onclick: () => openTeamsChat(m.sender, "") }, "Teams"),
      !urgent && (m.sender || m.sender_name) && el("button", { class: "icon subtle", title: `Always treat ${m.sender_name || m.sender} as urgent`, onclick: () => markUrgent(m.sender || m.sender_name) }, "Mark urgent")));
}

function teamsRow(m) {
  return el("li", { class: `${m.unread ? "unread" : ""} ${m.urgent ? "sev-urgent" : ""}` },
    el("div", { class: "grow" },
      el("div", { class: "title" }, m.from_name || m.subject, m.urgent && el("span", { class: "tag urgent" }, "Urgent")),
      el("div", { class: "muted src-line" }, srcTag("teams", `notification from ${m.sender || "Teams"}`), [m.from_name ? m.subject : "", when(m.received)].filter(Boolean).join(" · ")),
      m.preview && el("div", { class: "fu-preview" }, String(m.preview).slice(0, 160))),
    fuActions(m, { who: m.from_name || m.sender_name },
      el("button", { class: "small alt", onclick: () => openExternal("msteams:") }, "Open Teams"),
      el("button", { class: "small", title: "Mark this Teams email read", onclick: () => guarded(async () => { await api("/api/mail/read", { id: m.id, read: true }); loaders.today(); }) }, "Done")));
}

function renderFollowups(f) {
  const box = $("#followups");
  if (f.error) { box.replaceChildren(el("div", { class: "err" }, f.error)); return; }
  const h = f.flagged_hidden || {};
  const hiddenNote = [h.more && `${h.more} more`, h.older && `${h.older} older`, h.automated && `${h.automated} automated`]
    .filter(Boolean).join(" · ");
  box.replaceChildren(
    fuGroup("Urgent — reply needed", f.urgent_reply, (m) => replyRow(m, { urgent: true }),
      null, "No urgent replies. Set who counts under Urgent people.", `urgent ${f.urgent_reply?.length ? "hot" : ""}`),
    fuGroup("Needs your reply", f.needs_reply, (m) => replyRow(m), null, "Nothing waiting on you."),
    fuGroup("Teams — reply needed", f.teams_reply, teamsRow, null, "No unread Teams messages.",
      f.teams_reply?.some((m) => m.urgent) ? "hot" : ""),
    fuGroup("Awaiting their reply", f.awaiting_reply, (m) => el("li", {},
      el("div", { class: "grow" }, el("div", { class: "title" }, m.subject),
        el("div", { class: "muted src-line" }, srcTag("sent"), `To ${String(m.to || "").slice(0, 60)} · sent ${when(m.sent)}`)),
      fuActions(m, { who: `to ${String(m.to || "").slice(0, 60)}` },
        el("button", { class: "small", title: "Opens a follow-up draft in Outlook", onclick: () => nudge(m) }, "Nudge"),
        el("button", { class: "small", title: "Alert me if nobody replies", onclick: () => remindDialog(m) }, "Remind me"))),
      null, "No one owes you a reply."),
    fuGroup("Flagged", f.flagged, (m) => el("li", {},
      el("div", { class: "grow" }, el("div", { class: "title" }, m.subject, dueTag(m.due)),
        el("div", { class: "muted src-line" }, srcTag("flagged"), `${fromLine(m.sender_name, m.sender)} · ${when(m.received)}`)),
      fuActions(m, { mute: m.sender || m.sender_name }, viewMail(m.id),
        el("button", { class: "small", onclick: () => completeTask({ source: "outlook", id: m.id, subject: m.subject }) }, "Done"))),
      hiddenNote && `Hidden: ${hiddenNote} (of ${h.total} flagged)`, "No recent flags."),
    fuGroup("Unread from VIPs", f.unread_vip, (m) => el("li", { class: "unread" },
      el("div", { class: "grow" }, el("div", { class: "title" }, m.subject),
        el("div", { class: "muted src-line" }, srcTag("outlook", "VIP sender"), `${fromLine(m.sender_name, m.sender)} · ${when(m.received)}`)),
      fuActions(m, { mute: m.sender || m.sender_name }, viewMail(m.id))),
      null, "No unread VIP mail."));
}

// ---- urgent people ---------------------------------------------------------------

const lowerSet = (list) => new Set((list || []).map((p) => String(p).toLowerCase()));

async function markUrgent(who) {
  await guarded(async () => {
    const u = await api("/api/followups/urgent");
    const locked = lowerSet(u.from_config);
    const mine = (u.people || []).filter((p) => !locked.has(String(p).toLowerCase()));
    if (lowerSet(u.people).has(String(who).toLowerCase())) { toast(`${who} is already urgent.`); return; }
    await api("/api/followups/urgent", { people: [...mine, who] });
    toast(`${who} added to urgent people.`);
    loaders.today(true);
  });
}

async function urgentDialog() {
  await guarded(async () => {
    const u = await api("/api/followups/urgent");
    const locked = lowerSet(u.from_config);
    const fixed = (u.people || []).filter((p) => locked.has(String(p).toLowerCase()));
    const mine = (u.people || []).filter((p) => !locked.has(String(p).toLowerCase()));
    const list = el("ul", { class: "list dialog-list" });
    const err = el("div", { class: "err", role: "alert" });
    const input = el("input", { placeholder: "Full name (Sean Cordero) or email address", maxlength: "120", "aria-label": "Add a person",
      onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); add(); } } });
    const draw = () => list.replaceChildren(
      ...fixed.map((p) => el("li", {}, el("div", { class: "grow mono" }, p),
        el("span", { class: "tag locked", title: "Listed in config.yaml (followups.urgent_people). Edit it there." }, "config.yaml"))),
      ...mine.map((p, i) => el("li", {}, el("div", { class: "grow mono" }, p),
        el("button", { type: "button", class: "small subtle", "aria-label": `Remove ${p}`, onclick: () => { mine.splice(i, 1); draw(); } }, "Remove"))),
      ...(!fixed.length && !mine.length ? [el("li", { class: "empty" }, "No one yet. Add the people whose mail should jump the queue.")] : []));
    const add = () => {
      const v = input.value.trim();
      err.textContent = "";
      if (!v) return;
      if (v.length > 120 || !/^[\p{L}\p{N}_ .'@&()-]+$/u.test(v)) {
        err.textContent = "Use a name or email address (letters, digits, spaces and . ' @ & ( ) - only).";
        return;
      }
      if (![...fixed, ...mine].some((p) => p.toLowerCase() === v.toLowerCase())) mine.push(v);
      input.value = "";
      draw();
      input.focus();
    };
    draw();
    showDialog(el("h3", {}, "Urgent people"),
      el("p", { class: "muted" }, `Mail and Teams messages from these people go to “Urgent — reply needed”. Match by full name or email.${u.internal_domains?.length ? ` Internal domains: ${u.internal_domains.join(", ")}.` : ""}`),
      list,
      el("div", { class: "inline-field" }, input, el("button", { type: "button", onclick: add }, "+ Add")),
      err,
      fixed.length ? el("p", { class: "muted" }, "Entries marked config.yaml can't be removed here; edit followups.urgent_people in config.yaml.") : null,
      el("div", { class: "row" }, el("button", { value: "cancel", class: "subtle" }, "Cancel"),
        el("button", { type: "button", class: "primary", onclick: () => guardedIn(err, async () => {
          if (input.value.trim()) add();
          if (err.textContent) return;
          await api("/api/followups/urgent", { people: mine });
          $("#dialog").close();
          toast("Urgent people saved.");
          loaders.today(true);
        }) }, "Save")));
    input.focus();
  });
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
    renderSnoozedButton(todayData);
    loadTodayAlerts();
  } finally {
    btn.disabled = false; btn.textContent = "↻ Rescan";
  }
});

$("#today-refresh").addEventListener("click", () => loaders.today(true));
$("#snoozed-btn").addEventListener("click", snoozedDialog);
$("#urgent-btn").addEventListener("click", urgentDialog);

// ---- launch bar (hotbar) -----------------------------------------------------------

let launchItems = [];

const launchGlyph = (url) => /^https?:/i.test(url) ? "◈" : /^msteams:/i.test(url) ? "✉" : "▣";

async function loadLauncher() {
  try { launchItems = (await api("/api/launcher")).items || []; } catch { /* keep what we have */ }
  renderLauncher();
}

function renderLauncher() {
  $("#launch-slots").replaceChildren(
    ...launchItems.map((it, i) => el("button", {
        type: "button", class: "hot-slot", "data-i": i,
        title: `${it.url}${i < 9 ? `  (Alt+${i + 1})` : ""}`,
        "aria-label": `Open ${it.name}${i < 9 ? `, Alt+${i + 1}` : ""}`,
        onclick: () => openLaunch(i),
      },
      el("span", { class: "key", "aria-hidden": "true" }, i < 9 ? String(i + 1) : "·"),
      el("span", { class: "glyph", "aria-hidden": "true" }, launchGlyph(it.url)),
      el("span", { class: "name" }, it.name))),
    el("button", { type: "button", class: "hot-slot add", onclick: launchAddDialog }, "+ Add"));
}

function openLaunch(i) {
  const slot = document.querySelector(`.hot-slot[data-i="${i}"]`);
  if (slot) { slot.classList.remove("fired"); void slot.offsetWidth; slot.classList.add("fired"); }
  guarded(() => api("/api/launcher/open", { index: i }));
}

async function saveLauncher(items) {
  const r = await api("/api/launcher", { items });
  launchItems = r.items;
  renderLauncher();
  $("#dialog").close();
  toast("Hotbar saved.");
}

const LAUNCH_HINT = "A web address (https://…), a Teams link (msteams:…) or a folder/file path (C:\\… or \\\\server\\share\\…).";

function launchAddDialog() {
  const name = el("input", { placeholder: "Name (e.g. Pulse)", maxlength: "40", "aria-label": "Name" });
  const url = el("input", { placeholder: "https://… or C:\\folder", "aria-label": "Link or path", class: "mono" });
  const err = el("div", { class: "err", role: "alert" });
  const save = () => guardedIn(err, async () => {
    if (!name.value.trim()) { name.focus(); throw new Error("Give the button a name."); }
    await saveLauncher([...launchItems, { name: name.value.trim(), url: url.value.trim() }]);
  });
  url.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); save(); } });
  showDialog(el("h3", {}, `Add hotbar slot ${launchItems.length + 1}`),
    el("label", { class: "field-label" }, "Name"), name,
    el("label", { class: "field-label" }, "Opens"), url,
    el("p", { class: "muted" }, LAUNCH_HINT), err,
    el("div", { class: "row" },
      el("button", { type: "button", class: "subtle", onclick: () => launchEditDialog() }, "Edit all…"),
      el("span", { class: "spacer" }),
      el("button", { value: "cancel", class: "subtle" }, "Cancel"),
      el("button", { type: "button", class: "primary", onclick: save }, "Add")));
  name.focus();
}

function launchEditDialog() {
  const rows = launchItems.map((x) => ({ ...x }));
  const list = el("ol", { class: "launch-edit" });
  const err = el("div", { class: "err", role: "alert" });
  const draw = (focus) => {
    list.replaceChildren(...rows.map((r, i) => {
      const move = (to) => { [rows[i], rows[to]] = [rows[to], rows[i]]; draw(`[data-move="${to}${to < i ? "u" : "d"}"]`); };
      return el("li", {},
        el("span", { class: "key", "aria-hidden": "true" }, i + 1),
        el("input", { value: r.name, placeholder: "Name", maxlength: "40", "aria-label": `Slot ${i + 1} name`, oninput: (e) => { r.name = e.target.value; } }),
        el("input", { value: r.url, placeholder: "https://… or C:\\folder", class: "mono", "aria-label": `Slot ${i + 1} link`, oninput: (e) => { r.url = e.target.value; } }),
        el("button", { type: "button", class: "icon subtle", "data-move": `${i}u`, title: "Move up", "aria-label": `Move slot ${i + 1} up`, disabled: i === 0, onclick: () => move(i - 1) }, "↑"),
        el("button", { type: "button", class: "icon subtle", "data-move": `${i}d`, title: "Move down", "aria-label": `Move slot ${i + 1} down`, disabled: i === rows.length - 1, onclick: () => move(i + 1) }, "↓"),
        el("button", { type: "button", class: "icon subtle danger", title: "Remove", "aria-label": `Remove slot ${i + 1}`, onclick: () => { rows.splice(i, 1); draw(); } }, "✕"));
    }));
    if (!rows.length) list.append(el("li", { class: "empty" }, "No buttons. Add one below."));
    if (focus) list.querySelector(focus)?.focus();
  };
  draw();
  const save = () => guardedIn(err, async () => {
    const items = rows.map((r) => ({ name: r.name.trim(), url: r.url.trim() })).filter((r) => r.name || r.url);
    const bad = items.find((r) => !r.name);
    if (bad) throw new Error(`${bad.url}: give it a name.`);
    await saveLauncher(items);
  });
  showDialog(el("h3", {}, "Edit hotbar"),
    el("p", { class: "muted" }, `${LAUNCH_HINT} Slots 1–9 also open with Alt+number.`),
    list, err,
    el("div", { class: "row" },
      el("button", { type: "button", class: "subtle", onclick: () => { if (rows.length < 24) { rows.push({ name: "", url: "" }); draw("li:last-child input"); } } }, "+ Add slot"),
      el("span", { class: "spacer" }),
      el("button", { value: "cancel", class: "subtle" }, "Cancel"),
      el("button", { type: "button", class: "primary", onclick: save }, "Save")));
}

$("#launch-edit").addEventListener("click", () => launchEditDialog());
document.addEventListener("keydown", (e) => {
  if (!e.altKey || e.ctrlKey || e.metaKey || !/^[1-9]$/.test(e.key)) return;
  if (modalOpen() || $("#palette").classList.contains("open")) return;
  const i = Number(e.key) - 1;
  if (launchItems[i]) { e.preventDefault(); openLaunch(i); }
});
$("#new-task").addEventListener("click", () => newTaskDialog());
$("#asana-btn").addEventListener("click", asanaDialog);
$("#muted-btn").addEventListener("click", mutedDialog);
setInterval(tickClock, 1000);
setInterval(() => { if ($("#tab-today").classList.contains("active")) loaders.today(); }, 120000);

// ---- settings from config (accent color, workday) -----------------------------

let appliedAccent = null;
function applyUi(ui) {
  if (!ui) return;
  const [ws, we] = (ui.workday || []).map((t) => { const [h, m] = String(t).split(":").map(Number); return h + (m || 0) / 60; });
  if (Number.isFinite(ws) && Number.isFinite(we) && we > ws) WORKDAY = { start: ws, end: we };
  const hex = /^#?([0-9a-f]{6})$/i.exec(ui.accent || "");
  if (hex && ui.accent !== appliedAccent) {
    const n = parseInt(hex[1], 16);
    const root = document.documentElement.style;
    root.setProperty("--accent", `#${hex[1]}`);
    root.setProperty("--accent-rgb", `${n >> 16} ${(n >> 8) & 255} ${n & 255}`);
    appliedAccent = ui.accent;
  }
}

async function openExternal(url) {
  await guarded(() => api("/api/open", { url }));
}

// ---- focus mode --------------------------------------------------------------

let focusInfo = { active: false };
function renderFocus(f) {
  focusInfo = f || { active: false };
  const chip = $("#focus-chip");
  if (focusInfo.active) {
    const mins = Math.max(0, Math.round((new Date(focusInfo.until) - new Date()) / 60000));
    chip.textContent = `Focus · ${mins >= 60 ? Math.floor(mins / 60) + "h " + (mins % 60) + "m" : mins + "m"}`;
    chip.className = "focus-chip small on";
    $("#focus-line").textContent = `▲ FOCUS MODE until ${timeOf(focusInfo.until)}${focusInfo.reason !== "manual" ? " · " + focusInfo.reason : ""} · only high-priority alerts pop up`;
  } else {
    chip.textContent = "Focus";
    chip.className = "focus-chip small subtle";
    $("#focus-line").textContent = "";
  }
}

async function setFocus(minutes) {
  await guarded(async () => renderFocus(await api("/api/focus", { minutes })));
}

function focusDialog() {
  const pick = (m) => el("button", { type: "button", class: m ? "primary" : "subtle", onclick: () => { $("#dialog").close(); setFocus(m); } },
    m === 0 ? "Turn off" : m < 60 ? `${m} min` : `${m / 60} hour${m > 60 ? "s" : ""}`);
  showDialog(el("h3", {}, "Focus mode"),
    el("p", { class: "muted" }, "While focus is on, only high-priority alerts pop up or play a sound. Everything still lands in the Alerts feed. A calendar event with “focus” in its title turns this on automatically."),
    focusInfo.active && el("p", { class: "good" }, `On until ${timeOf(focusInfo.until)}.`),
    el("div", { class: "row" }, pick(30), pick(60), pick(120), focusInfo.active && pick(0), el("button", { value: "cancel", class: "subtle" }, "Cancel")));
}
$("#focus-chip").addEventListener("click", focusDialog);

// ---- teams -------------------------------------------------------------------

let teamsUrl = "https://teams.microsoft.com/v2/";

function openTeamsChat(to, message) {
  const users = to.split(/[;,]/).map((s) => s.trim()).filter(Boolean).join(",");
  if (!users) { showMessage("Teams", "Enter at least one email address."); return; }
  openExternal(`msteams:/l/chat/0/0?users=${encodeURIComponent(users)}${message ? "&message=" + encodeURIComponent(message) : ""}`);
}

async function teamsLogin() {
  if (!await confirmDialog("Log in to Teams", "A browser window will open on Teams. Sign in (including MFA), wait until your chats load, then close that window. Command Center will then check your unread count every few minutes.", "Open browser")) return;
  guarded(() => api("/api/teams/login", {}));
}

loaders.teams = () => guarded(async () => {
  const t = await api("/api/teams");
  teamsUrl = t.url;
  const s = t.state || {};
  const box = $("#teams-status");
  if (!t.set_up) {
    box.replaceChildren(el("div", { class: "panel-label" }, "TEAMS STATUS"),
      el("p", {}, "Not connected yet. Log in once and Command Center will watch your Teams unread count in the background."),
      el("div", { class: "row" }, el("button", { class: "primary", onclick: teamsLogin }, "Log in to Teams")));
  } else {
    box.replaceChildren(el("div", { class: "panel-label" }, "TEAMS STATUS"),
      el("div", { class: "big-number" }, s.unread ?? "–"),
      el("div", { class: "muted" }, "unread chats and mentions", s.checked ? ` · checked ${when(s.checked)}` : " · first check pending"),
      s.ok === false && el("p", { class: "err" }, s.error),
      el("div", { class: "row" },
        el("button", { class: "small", onclick: (e) => { e.target.disabled = true; e.target.textContent = "Checking…"; guarded(() => api("/api/teams/check", {})).then(loaders.teams); } }, "Check now"),
        el("button", { class: "small alt", onclick: () => openExternal("msteams:") }, "Open Teams app"),
        el("button", { class: "small subtle", onclick: teamsLogin }, s.login_needed ? "Log in again" : "Re-login")));
  }
  const act = t.activity;
  if (act.error) fill($("#teams-activity"), [el("li", { class: "err" }, act.error)], "");
  else fill($("#teams-activity"), act.map((m) => el("li", { class: m.unread ? "unread" : "" },
    el("div", { class: "grow" }, el("div", { class: "title" }, m.subject), el("div", { class: "muted" }, when(m.received))),
    el("div", { class: "actions" }, viewMail(m.id)))),
    "No Teams emails yet. In Teams: Settings → Notifications → turn on email for missed activity.");
});
$("#teams-refresh").addEventListener("click", loaders.teams);
$("#teams-send").addEventListener("click", () => openTeamsChat($("#teams-to").value, $("#teams-msg").value));

// ---- command palette (Ctrl+K) ------------------------------------------------

const pal = { items: [], active: 0, mail: [], seq: 0, timer: null };

const extraPaletteSources = [];

function paletteSources() {
  const out = [];
  const add = (group, label, hint, run) => out.push({ group, label, hint: hint || "", run });
  for (const source of extraPaletteSources) {
    try { for (const i of source() || []) add(i.group || "Commands", i.label, i.hint, i.run); } catch { /* module not ready */ }
  }
  const order = /^#?(\d{7,10})$/.exec($("#palette-input").value.trim());
  if (order) add("Commands", `Look up order ${order[1]}`, "SQL", () => orderDialog(order[1]));
  launchItems.forEach((it, i) => add("Commands", `Open ${it.name}`, `hotbar ${i + 1}`, () => openLaunch(i)));
  add("Commands", "Edit hotbar", "launch buttons", () => launchEditDialog());
  add("Commands", "Show snoozed", "snoozes + no-reply reminders", snoozedDialog);
  add("Commands", "Urgent people", "who counts as urgent", urgentDialog);
  // "Go to <tab>" for every tab in both nav groups (module tabs included), each landing in its own pane.
  for (const b of document.querySelectorAll(".nav-group button[data-tab]")) {
    const label = `Go to ${b.dataset.label || b.textContent.trim()}`;
    if (!out.some((i) => i.label === label)) add("Commands", label, `${b.closest(".nav-group").dataset.pane === "work" ? "WORK" : "COMMS"} tab`, () => openTab(b.dataset.tab));
  }
  if (window.innerWidth >= SPLIT_MIN_WIDTH) add("Commands", layout.split ? "Single pane" : "Split panes", "layout", () => $("#layout-toggle").click());
  for (const p of proj.list) add("Projects", `Work on: ${p.name}`, p.path, () => { setActiveProject(p.path); openTab("projects"); });
  add("Commands", "New task", "Outlook or Asana", () => newTaskDialog());
  add("Commands", "Rescan follow-ups", "", () => { openTab("today"); loaders.today(true); });
  add("Commands", "Focus 30 minutes", "mute non-urgent alerts", () => setFocus(30));
  add("Commands", "Focus 1 hour", "mute non-urgent alerts", () => setFocus(60));
  add("Commands", "Focus 2 hours", "mute non-urgent alerts", () => setFocus(120));
  if (focusInfo.active) add("Commands", "Focus off", "", () => setFocus(0));
  add("Commands", "Send a Teams message", "", () => { openTab("teams"); $("#teams-to").focus(); });
  add("Commands", "Open Teams app", "", () => openExternal("msteams:"));
  add("Commands", "Mark all alerts read", "", () => guarded(async () => { await api("/api/alerts/read", {}); refresh(); }));
  add("Commands", "Reload config", "config.yaml", () => guarded(async () => { await api("/api/reload", {}); showMessage("Reloaded", "config.yaml was reloaded."); }));
  add("Commands", asanaConnected ? "Disconnect Asana" : "Connect Asana", "", asanaDialog);
  if (todayData) {
    const now = new Date();
    const agenda = Array.isArray(todayData.agenda) ? todayData.agenda : [];
    const next = agenda.find((m) => m.join_url && new Date(m.end) > now);
    if (next) add("Commands", `Join next meeting: ${next.subject}`, timeOf(next.start), () => openExternal(next.join_url));
    for (const m of agenda) add("Agenda", m.subject, timeOf(m.start), () => guarded(() => api("/api/outlook/open", { id: m.id })));
    for (const m of agenda.filter((m) => new Date(m.end) > now)) add("Agenda", `Prep: ${m.subject}`, timeOf(m.start), () => prepDialog(m.id, m));
    for (const t of todayData.tasks.items) add("Tasks", t.subject, dueText(t.due), () => t.url ? openExternal(t.url) : guarded(() => api("/api/outlook/open", { id: t.id })));
    const f = todayData.followups.error ? {} : todayData.followups;
    const seenFu = new Set();  // a message can be in two groups (e.g. needs a reply and flagged): list it once
    for (const m of [...(f.urgent_reply || []), ...(f.needs_reply || []), ...(f.teams_reply || []), ...(f.flagged || []), ...(f.unread_vip || [])]) {
      if (!m || seenFu.has(m.id)) continue;
      seenFu.add(m.id);
      add("Follow-ups", m.subject, m.sender_name, () => { openTab("mail"); showMail(m.id); });
    }
  }
  for (const q of sqlInfo.queries || []) add("SQL", `Run query: ${q.name}`, q.connection, () => { openTab("sql"); $("#sql-query").value = q.name; renderParams(); });
  for (const m of pal.mail) add("Mail", m.subject, `${m.folder === "Sent" ? "to " + m.to : m.sender_name} · ${when(m.received)}`, () => { openTab("mail"); showMail(m.id); });
  return out;
}

function paletteScore(item, words) {
  if (!words.length) return item.group === "Commands" ? 1 : 0;
  if (item.group === "Mail") return 0.5;  // already matched by Outlook
  const text = `${item.label} ${item.hint}`.toLowerCase();
  if (!words.every((w) => text.includes(w))) return 0;
  return 2 - text.indexOf(words[0]) / 200 + (item.group === "Commands" ? 0.3 : 0);
}

function renderPalette() {
  const q = $("#palette-input").value.trim().toLowerCase();
  const words = q.split(/\s+/).filter(Boolean);
  pal.items = paletteSources().map((i) => ({ ...i, score: paletteScore(i, words) })).filter((i) => i.score > 0)
    .sort((a, b) => b.score - a.score).slice(0, 60);
  const order = ["Commands", "Projects", "Agenda", "Tasks", "Follow-ups", "Pulse", "Toolkit", "SQL", "Mail"];
  const rank = (g) => (order.includes(g) ? order.indexOf(g) : order.length - 1);  // unknown module groups before Mail
  pal.items.sort((a, b) => rank(a.group) - rank(b.group));
  pal.active = Math.min(pal.active, Math.max(0, pal.items.length - 1));
  const rows = [];
  let group = null;
  pal.items.forEach((item, i) => {
    if (item.group !== group) { group = item.group; rows.push(el("li", { class: "group" }, group)); }
    rows.push(el("li", { class: `item ${i === pal.active ? "active" : ""}`, onclick: () => runPalette(i), onmousemove: () => { if (pal.active !== i) { pal.active = i; renderPalette(); } } },
      el("span", { class: "label" }, item.label), el("span", { class: "hint" }, item.hint)));
  });
  if (!rows.length) rows.push(el("li", { class: "group" }, pal.searching ? "Searching mail…" : "No matches"));
  $("#palette-results").replaceChildren(...rows);
  $("#palette-results .item.active")?.scrollIntoView({ block: "nearest" });
}

function searchMail(q) {
  clearTimeout(pal.timer);
  pal.mail = [];
  const seq = ++pal.seq;
  pal.searching = q.length >= 3;
  if (!pal.searching) return;
  pal.timer = setTimeout(async () => {
    let mail = [];
    try { mail = (await api(`/api/search?q=${encodeURIComponent(q)}`)).mail || []; } catch { /* Outlook closed: commands still work */ }
    if (seq !== pal.seq) return;
    pal.mail = mail;
    pal.searching = false;  // done (found, empty or failed): the list now says "No matches" if nothing fits
    if ($("#palette").classList.contains("open")) renderPalette();
  }, 300);
}

function openPalette() {
  $("#palette").classList.add("open");
  $("#palette-input").value = "";
  pal.active = 0; pal.mail = []; pal.searching = false; pal.seq++;
  renderPalette();
  $("#palette-input").focus();
  if (!sqlInfo.queries?.length) api("/api/sql").then((d) => { sqlInfo = d; renderPalette(); }).catch(() => {});
}
function closePalette() { $("#palette").classList.remove("open"); }
function runPalette(i) {
  const item = pal.items[i];
  if (!item) return;
  closePalette();
  item.run();
}

$("#search-btn").addEventListener("click", openPalette);
$("#palette").addEventListener("click", (e) => { if (e.target.id === "palette") closePalette(); });
$("#palette-input").addEventListener("input", (e) => { pal.active = 0; searchMail(e.target.value.trim()); renderPalette(); });
$("#palette-input").addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown") { pal.active = Math.min(pal.active + 1, pal.items.length - 1); renderPalette(); e.preventDefault(); }
  else if (e.key === "ArrowUp") { pal.active = Math.max(pal.active - 1, 0); renderPalette(); e.preventDefault(); }
  else if (e.key === "Enter") { runPalette(pal.active); e.preventDefault(); }
  else if (e.key === "Escape") closePalette();
});
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    if (modalOpen()) return;  // never open the palette behind a modal
    $("#palette").classList.contains("open") ? closePalette() : openPalette();
  }
});

// ---- status + polling --------------------------------------------------------

let lastUnread = -1;
async function refresh() {
  try {
    const [{ unread }, st] = await Promise.all([api("/api/alerts?unread_only=true"), api("/api/status")]);
    $("#badge").hidden = unread === 0;
    $("#badge").textContent = unread;
    document.title = unread ? `(${unread}) Command Center` : "Command Center";
    applyUi(st.ui);
    renderFocus(st.focus);
    const sites = Object.values(st.websites).filter(Boolean);
    const sitesOk = sites.every((s) => s.ok);
    const dot = (state) => el("span", { class: `dot ${state === undefined ? "" : state ? "ok" : "bad"}` });
    const item = (state, name) => el("span", { title: `${name}: ${state === undefined ? "not checked" : state ? "OK" : "problem"}` },
      dot(state), el("span", { class: "status-name" }, name));
    $("#status").replaceChildren(
      item(st.outlook ? st.outlook.ok : undefined, "Outlook"),
      item(st.teams ? st.teams.ok : undefined, "Teams"),
      ...(sites.length ? [item(sitesOk, "Websites")] : []));
    renderWindowControls(st);
    if (unread !== lastUnread && $("#tab-alerts").classList.contains("active")) loaders.alerts();
    if (unread !== lastUnread && lastUnread !== -1 && $("#tab-today").classList.contains("active")) loadTodayAlerts();
    lastUnread = unread;
  } catch { $("#status").textContent = "Server not reachable"; }
}

// ---- window controls (desktop app: frameless window) ------------------------------

let isFullscreen = null, windowStateAsked = false;
function renderWindowControls(st) {
  const desktop = st.desktop === true;
  $("#win-controls").hidden = !desktop;
  if (!desktop) { setDragRegions(false); return; }
  if (!windowStateAsked) {
    windowStateAsked = true;
    api("/api/window/state").then((ws) => setFullscreenUi(!!ws.fullscreen)).catch(() => setFullscreenUi(false));
  }
}
// The frameless window drags by the header's empty parts (never its buttons), and only when not full screen.
const DRAG_PARTS = "header, header .logo, header .logo *, header .navs, header .nav-group, header .nav-label, header .header-right";
function setDragRegions(on) {
  document.querySelectorAll(DRAG_PARTS).forEach((n) => n.classList.toggle("pywebview-drag-region", on));
}
function setFullscreenUi(on) {
  isFullscreen = !!on;
  setDragRegions(!isFullscreen && !$("#win-controls").hidden);
  const b = $("#win-full");
  b.textContent = isFullscreen ? "❐" : "⛶";
  b.title = isFullscreen ? "Leave full screen (F11)" : "Full screen (F11)";
  b.setAttribute("aria-label", isFullscreen ? "Leave full screen" : "Full screen");
  b.setAttribute("aria-pressed", String(isFullscreen));
}
async function toggleFullscreen() {
  await guarded(async () => { const r = await api("/api/window/fullscreen", {}); setFullscreenUi(r.fullscreen); });
}
$("#win-lock").addEventListener("click", async () => {
  if (!await confirmDialog("Lock Command Center?", "You'll need Windows Hello to get back in. Alerts keep running.", "Lock")) return;
  // The server drops the session and the window switches itself to the local lock screen.
  await guarded(() => api("/api/session/lock", {}));
});
$("#win-restart").addEventListener("click", async () => {
  if (!await confirmDialog("Restart Command Center?", "The app closes and opens again. Claude sessions and the VS Code server stop.", "Restart")) return;
  const overlay = el("div", { class: "restart-overlay", role: "status", "aria-live": "assertive" },
    el("div", { class: "restart-box" }, el("div", { class: "restart-mark" }, "◢◤"), el("div", {}, "Restarting…")));
  document.body.append(overlay);
  try { await api("/api/app/restart", {}); } catch (e) { overlay.remove(); showMessage("Restart", e.message); }
});
// Buttons in the header must never start a window drag: stop their mousedown before pywebview's body listener.
document.querySelector("header").addEventListener("mousedown", (e) => {
  if (e.target.closest("button, input, select, textarea, a, [tabindex]")) e.stopPropagation();
});
$("#win-min").addEventListener("click", () => guarded(() => api("/api/window/minimize", {})));
$("#win-full").addEventListener("click", toggleFullscreen);
$("#win-hide").addEventListener("click", () => guarded(() => api("/api/window/hide", {})));
document.addEventListener("keydown", (e) => {
  if (e.key !== "F11" || $("#win-controls").hidden) return;
  e.preventDefault();
  toggleFullscreen();
});

tickClock();
loadLauncher();
loadProjectList();
refresh().then(() => loaders.today());
setInterval(refresh, 5000);


// ---- plug-in API for feature modules (static/<module>.js) ----------------------
// A module adds its own tab and palette entries without editing this file:
//   CC.registerTab({ id: "toolkit", label: "Toolkit", render(section) {...}, onShow() {...} })
//   CC.addPaletteSource(() => [{ group: "Toolkit", label: "...", hint: "...", run() {...} }])
window.CC = {
  $, el, api, guarded, fill, when, timeOf, srcTag, showDialog, showMessage, confirmDialog, openTab, openExternal, loaders,
  get todayData() { return todayData; },
  // The project picked in "Working on:" (right pane), or null. Changes fire document "cc:project" {detail:{path,name}}.
  get activeProject() { return activeProject(); },
  get projects() { return proj.list.slice(); },
  setActiveProject,
  get split() { return layout.split; },
  paneOf,
  // pane: "comms" | "work". Defaults: known ids by PANE_OF, everything else goes to WORK.
  registerTab({ id, label, render, onShow, before, pane }) {
    if (!TAB_ID.test(String(id)) || document.querySelector(`#tab-${id}`)) return;
    pane = pane === "comms" || pane === "work" ? pane : PANE_OF[id] || "work";
    const nav = $(`#nav-${pane}`);
    const button = el("button", { "data-tab": id, "data-label": label }, label);
    const order = TAB_ORDER[pane];
    let ref = null;
    if (order.includes(id)) {
      // known tabs keep a fixed order whatever order the modules load in
      const later = order.slice(order.indexOf(id) + 1);
      ref = [...nav.querySelectorAll("button[data-tab]")].find((b) => later.includes(b.dataset.tab)) || null;
    } else if (before) ref = nav.querySelector(`button[data-tab="${CSS.escape(before)}"]`);
    ref ? nav.insertBefore(button, ref) : nav.append(button);
    const section = el("section", { id: `tab-${id}`, class: "tab" });
    $(`#pane-${pane}-body`).append(section);
    render?.(section);
    if (onShow) loaders[id] = onShow;
    if (layout.ready) runLoaders(applyLayout());
  },
  addPaletteSource(fn) { extraPaletteSources.push(fn); },
  wideDialog(...content) { $("#dialog").classList.add("wide"); showDialog(...content); },
};
$("#dialog").addEventListener("close", () => $("#dialog").classList.remove("wide"));
document.dispatchEvent(new Event("cc:ready"));

// Modules register their tabs while their scripts load (right after this file); lay out once they're in.
function initLayout() {
  if (layout.ready) return;
  layout.ready = true;
  runLoaders(applyLayout().filter((id) => id !== "today"));  // Today loads with the first status refresh
}
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initLayout, { once: true });
else initLayout();
