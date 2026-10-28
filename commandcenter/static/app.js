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
  today: "comms", alerts: "comms", mail: "comms", calendar: "comms", teams: "comms", "pulse-feedback": "comms", personal: "comms",
  vscode: "work", toolkit: "work", sql: "work", pulse: "work", websites: "work",
};
const TAB_ORDER = { comms: ["today", "alerts", "mail", "calendar", "teams", "pulse-feedback", "personal"], work: ["vscode", "toolkit", "sql", "pulse", "websites"] };
const TAB_ID = /^[a-z0-9_-]{1,40}$/i;
// "projects" was the terminal Claude tab; Claude now lives in the VS Code tab.
const pickTab = (v, d) => (v === "projects" ? "vscode" : typeof v === "string" && TAB_ID.test(v) ? v : d);

// Focus layout: the Pulse site on the left, VS Code on the right; the header (nav, badges, status, clock) stays as
// it is. While it's on, the Pulse tab and its nav button move to COMMS (VS Code stays put: moving its iframe would
// reload it), so the other tabs still work: COMMS tabs replace Pulse on the left, WORK tabs replace VS Code.
// It needs two panes; in a narrow window it falls back to one pane at a time, Pulse under COMMS.
const FOCUS_LEFT = "pulse", FOCUS_RIGHT = "vscode";
const FOCUS_SPLIT_DEFAULT = 0.5;

const layout = (() => {
  let saved = {}, focus = null;
  try { saved = JSON.parse(store.get("cc.layout.tabs") || "{}") || {}; } catch { saved = {}; }
  try { focus = JSON.parse(store.get("cc.layout.focus") || "null"); } catch { focus = null; }
  const prev = focus && focus.on === true && focus.prev && typeof focus.prev === "object" ? focus.prev : null;
  return {
    split: false,
    active: { comms: pickTab(saved.comms, "today"), work: pickTab(saved.work, "vscode") },
    shown: saved.shown === "work" ? "work" : "comms",   // the pane shown in single mode
    // {prev: {comms, work, shown}}: on, with the tabs to go back to when it's turned off
    focus: focus && focus.on === true ? { prev: { comms: pickTab(prev?.comms, "today"), work: pickTab(prev?.work, "vscode"), shown: prev?.shown === "work" ? "work" : "comms" } } : null,
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

// Focus layout is available once both its tabs are registered (pulseview.js, vscode.js).
const focusReady = () => !!($(`#tab-${FOCUS_LEFT}`) && $(`#tab-${FOCUS_RIGHT}`) && navButton(FOCUS_LEFT));
const focusOn = () => !!layout.focus && focusReady();

function wantSplit() {
  return window.innerWidth >= SPLIT_MIN_WIDTH && (focusOn() || store.get("cc.layout.mode") !== "single");
}

// Moves the Pulse tab (section + nav button) into COMMS for the focus layout, or back to its place in WORK.
function placeFocusTab() {
  const sec = $(`#tab-${FOCUS_LEFT}`), btn = navButton(FOCUS_LEFT);
  if (!sec || !btn) return;
  const pane = focusOn() ? "comms" : "work";
  const body = $(`#pane-${pane}-body`), nav = $(`#nav-${pane}`);
  if (sec.parentElement !== body) body.append(sec);
  if (btn.parentElement === nav) return;
  const later = TAB_ORDER[pane].slice(TAB_ORDER[pane].indexOf(FOCUS_LEFT) + 1);
  const ref = pane === "work" ? [...nav.querySelectorAll("button[data-tab]")].find((b) => later.includes(b.dataset.tab)) : null;
  ref ? nav.insertBefore(btn, ref) : nav.append(btn);
}

function setFocusLayout(on) {
  if (on === !!layout.focus || (on && !focusReady())) return;
  if (on) {
    layout.focus = { prev: { comms: layout.active.comms, work: layout.active.work, shown: layout.shown } };
    Object.assign(layout.active, { comms: FOCUS_LEFT, work: FOCUS_RIGHT });
    layout.shown = "comms";
  } else {
    const prev = layout.focus.prev;
    layout.focus = null;
    // A pane still showing its focus tab goes back to the tab it had before; one Joe has since switched keeps it.
    if (layout.active.comms === FOCUS_LEFT) layout.active.comms = prev.comms === FOCUS_LEFT ? "today" : prev.comms;
    if (layout.active.work === FOCUS_RIGHT) layout.active.work = prev.work;
    layout.shown = prev.shown;
  }
  store.set("cc.layout.focus", layout.focus ? JSON.stringify({ on: true, prev: layout.focus.prev }) : null);
  saveLayoutTabs();
  splitFrac = savedSplit();
  runLoaders(applyLayout());
}

function renderFocusButton() {
  const b = $("#focus-layout");
  if (!b) return;
  const on = focusOn(), narrow = window.innerWidth < SPLIT_MIN_WIDTH;
  b.hidden = !focusReady();
  b.setAttribute("aria-pressed", String(on));
  b.title = on ? `Focus layout: Pulse left, VS Code right${narrow ? " (window too narrow for both: one pane at a time)" : ""}. Click to go back.`
    : "Focus layout: the Pulse site on the left and VS Code on the right; the header and alerts stay";
}

// Puts the classes in line with the layout state. Returns the tabs that just became visible.
function applyLayout() {
  placeFocusTab();
  layout.split = wantSplit();
  const main = $("#workspace");
  main.classList.toggle("ws-split", layout.split);
  main.classList.toggle("ws-single", !layout.split);
  main.classList.toggle("ws-focus", focusOn());
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
  toggle.hidden = window.innerWidth < SPLIT_MIN_WIDTH || focusOn();  // the focus layout is always two panes
  toggle.textContent = layout.split ? "◧ Split" : "▭ Single";
  toggle.setAttribute("aria-pressed", String(layout.split));
  toggle.title = layout.split ? "Two panes: COMMS left, WORK right. Click for a single pane." : "One pane at a time. Click to show COMMS and WORK side by side.";
  renderFocusButton();
  if (layout.split) applySplit();
  navPill();
  document.dispatchEvent(new CustomEvent("cc:layout", { detail: { split: layout.split, focus: focusOn() } }));
  return fresh;
}

// The active tab's pill (style.css .nav-group::before) slides under the active button of each nav group.
function navPill() {
  document.querySelectorAll(".nav-group").forEach((g) => {
    const b = g.querySelector("button.active");
    const was = g.classList.contains("pill-on");
    g.classList.toggle("pill-on", !!b);
    if (!b) return;
    g.style.setProperty("--pill-x", `${b.offsetLeft}px`);
    g.style.setProperty("--pill-w", `${b.offsetWidth}px`);
    if (was) g.classList.add("pill-anim");  // the first placement doesn't slide in from the edge
  });
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

// Focus layout button, next to Split/Single (its state is drawn by renderFocusButton).
$("#layout-toggle").after(el("button", {
  id: "focus-layout", class: "layout-toggle focus-layout small subtle", type: "button", hidden: true, "aria-pressed": "false",
  onclick: () => setFocusLayout(!layout.focus),
}, "⧉ Pulse | Code"));

window.addEventListener("resize", () => {
  if (!layout.ready) return;
  const toggleHidden = window.innerWidth < SPLIT_MIN_WIDTH || focusOn();
  if (wantSplit() !== layout.split || $("#layout-toggle").hidden !== toggleHidden) runLoaders(applyLayout());
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
    navPill();
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

// The focus layout keeps its own splitter position (cc.layout.focusSplit), so Pulse | VS Code and COMMS | WORK each stay put.
const splitKey = () => (focusOn() ? "cc.layout.focusSplit" : "cc.layout.split");
const splitDefault = () => (focusOn() ? FOCUS_SPLIT_DEFAULT : SPLIT_DEFAULT);
function savedSplit() { const v = parseFloat(store.get(splitKey())); return v >= 0.1 && v <= 0.9 ? v : splitDefault(); }
let splitFrac = SPLIT_DEFAULT;  // read in initLayout, once the module tabs (and so the focus layout) are known

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
  if (save) store.set(splitKey(), splitFrac.toFixed(4));
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
    store.set(splitKey(), splitFrac.toFixed(4));
  };
  sp.addEventListener("pointerup", end);
  sp.addEventListener("pointercancel", end);
  sp.addEventListener("lostpointercapture", end);
  sp.addEventListener("dblclick", () => setSplit(splitDefault()));
  sp.addEventListener("keydown", (e) => {
    const step = e.shiftKey ? 0.1 : 0.02;
    const map = { ArrowLeft: splitFrac - step, ArrowRight: splitFrac + step, Home: 0, End: 1, Enter: splitDefault() };
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

// Feature modules handle their own alert action types (CC.registerAlertAction), e.g. "meeting-notes".
const alertActionTypes = {};
// ...and add buttons to meeting rows (Today agenda, prep card, Calendar tab): CC.addMeetingAction(fn(meeting, where)).
const meetingActionSources = [];
function meetingActions(m, where) {
  if (m?.lunch) return [];  // a lunch block (outlook.py _is_lunch) isn't a meeting: no notes / prep actions
  return meetingActionSources.flatMap((fn) => { try { return [fn(m, where)].flat().filter(Boolean); } catch { return []; } });
}
// ...and to received emails (Mail list rows, the reader, a follow-up row's ⋯ menu): CC.addMailAction(fn(mail, where)),
// where = "row" | "reader" | "followup".
const mailActionSources = [];
function mailActions(m, where) {
  return mailActionSources.flatMap((fn) => { try { return [fn(m, where)].flat().filter(Boolean); } catch { return []; } });
}
function alertAction(a) {
  const act = a.action;
  if (!act) return null;
  if (Object.hasOwn(alertActionTypes, act.type)) {
    try { return alertActionTypes[act.type](act, a); } catch { return null; }
  }
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

// The Alerts tab: All | Unread and a filter by kind on top (both remembered per viewer: cc.alerts.filter,
// cc.alerts.kind), then the alerts grouped by day. A row is one line of facts: priority rail, time, source, title with
// the body under it (one line each; the full text on hover), and its actions (✓ Mark read on hover / focus).
const alertsState = { filter: store.get("cc.alerts.filter") === "unread" ? "unread" : "all", kind: store.get("cc.alerts.kind") || "", data: [] };
// The filter's kinds, from each alert's source tag (alertSource): Outlook and the reminders about mail are Mail.
const ALERT_KINDS = [["mail", "Mail", ["outlook", "sent"]], ["teams", "Teams", ["teams"]], ["calendar", "Calendar", ["calendar"]],
  ["pulse", "Pulse", ["pulse"]], ["other", "Other", []]];
const alertKind = (a) => (ALERT_KINDS.find(([, , tags]) => tags.includes(alertSource(a))) || ["other"])[0];

// Opening what an alert points at (View mail, Open Pulse, Join, Prep, Open chat...) marks it read: it's been seen.
function alertRow(a) {
  const markRead = () => {
    if (a.read) return;
    a.read = 1;
    row.classList.remove("unread");
    row.querySelector(".alert-done")?.remove();
    api("/api/alerts/read", { id: a.id }).then(refresh).catch(() => { a.read = 0; });
  };
  const actions = el("div", { class: "al-acts" }, alertAction(a),
    !a.read && el("button", { class: "small subtle alert-done", title: "Mark read", "aria-label": `Mark "${a.title}" read`,
      onclick: () => guarded(async () => { await api("/api/alerts/read", { id: a.id }); refresh(); loaders.alerts(); }) }, "✓"));
  actions.addEventListener("click", (e) => { const b = e.target.closest("button"); if (b && !b.classList.contains("alert-done")) markRead(); });
  const kind = alertSource(a);
  const row = el("li", { class: `alert-row prio-${a.priority} ${a.read ? "" : "unread"}` },
    // In column order: the wide layout places them in the list's shared columns one after another
    el("span", { class: `prio ${a.priority}`, title: `${a.priority === "high" ? "High" : a.priority === "low" ? "Low" : "Normal"} priority` }),
    el("span", { class: "al-time", title: new Date(a.ts).toLocaleString() }, timeOf(a.ts)),
    el("span", { class: "al-src" }, el("span", { class: `src src-${kind}`, title: SOURCES[kind]?.[1] || "Command Center" }, a.source)),
    el("div", { class: "al-main" },
      el("div", { class: "al-title", title: a.title }, a.title),
      a.body && el("div", { class: "al-body", title: a.body }, a.body)),
    actions);
  return row;
}

// "Today", "Yesterday", the weekday this week, else the date.
function alertDay(d) {
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const days = Math.round((today - new Date(d.getFullYear(), d.getMonth(), d.getDate())) / 864e5);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return d.toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" });
  return d.toLocaleDateString([], { month: "short", day: "numeric", year: d.getFullYear() === today.getFullYear() ? undefined : "numeric" });
}

function renderAlerts() {
  const all = alertsState.data, unread = all.filter((a) => !a.read).length;
  $("#alerts-f-all").setAttribute("aria-pressed", String(alertsState.filter !== "unread"));
  $("#alerts-f-unread").setAttribute("aria-pressed", String(alertsState.filter === "unread"));
  $("#alerts-unread-count").textContent = unread;
  $("#alerts-unread-count").hidden = !unread;
  $("#alerts-readall").disabled = !unread;
  // Kind filter: the kinds in the feed (unread count when showing Unread); a remembered kind no longer there shows all.
  const pool = all.filter((a) => alertsState.filter !== "unread" || !a.read);
  const counts = new Map();
  for (const a of all) counts.set(alertKind(a), 0);
  for (const a of pool) counts.set(alertKind(a), counts.get(alertKind(a)) + 1);
  if (alertsState.kind && !counts.has(alertsState.kind)) alertsState.kind = "";
  const kindBtn = (key, label, n) => el("button", { type: "button", class: "small", "aria-pressed": String(alertsState.kind === key),
    onclick: () => { alertsState.kind = key; store.set("cc.alerts.kind", key || null); renderAlerts(); } }, label, n > 0 && el("span", { class: "chip" }, n));
  const kinds = ALERT_KINDS.filter(([key]) => counts.has(key));
  $("#alerts-sources").replaceChildren(kindBtn("", "Everything"), ...kinds.map(([key, label]) => kindBtn(key, label, counts.get(key))));
  $("#alerts-sources").hidden = kinds.length < 2;

  const shown = pool.filter((a) => !alertsState.kind || alertKind(a) === alertsState.kind);
  const items = [];
  let day = null;
  for (const a of shown) {
    const label = alertDay(new Date(a.ts));
    if (label !== day) {
      day = label;
      const group = shown.filter((b) => alertDay(new Date(b.ts)) === label), n = group.filter((b) => !b.read).length;
      items.push(el("li", { class: "al-group", role: "presentation" }, el("span", {}, label), el("span", { class: "chip" }, group.length),
        n > 0 && alertsState.filter !== "unread" && el("span", { class: "al-group-unread" }, `${n} unread`)));
    }
    items.push(alertRow(a));
  }
  const what = alertsState.kind ? `${ALERT_KINDS.find(([key]) => key === alertsState.kind)[1]} alerts` : "alerts";
  // Column headings over the table (hidden in a narrow pane, where rows stack)
  if (items.length) items.unshift(el("li", { class: "al-cols", "aria-hidden": "true" }, el("span"), el("span", {}, "Time"),
    el("span", {}, "Source"), el("span", {}, "Alert"), el("span", {}, "Action")));
  fill($("#alerts-list"), items, alertsState.filter === "unread" ? `No unread ${what}` : `No ${what} yet`);
}

loaders.alerts = () => guarded(async () => {
  alertsState.data = (await api("/api/alerts")).alerts;
  renderAlerts();
});

const setAlertsFilter = (f) => { alertsState.filter = f; store.set("cc.alerts.filter", f === "unread" ? f : null); renderAlerts(); };
$("#alerts-f-all").addEventListener("click", () => setAlertsFilter("all"));
$("#alerts-f-unread").addEventListener("click", () => setAlertsFilter("unread"));
$("#alerts-readall").addEventListener("click", () => guarded(async () => { await api("/api/alerts/read", {}); refresh(); loaders.alerts(); }));
$("#alerts-readall").after(menuButton("⋯", "More alert actions", () => [
  el("button", { type: "button", onclick: () => guarded(async () => {
    if (await confirmDialog("Clear alerts", "Delete all alerts from the feed?", "Clear")) { await api("/api/alerts/clear", {}); refresh(); loaders.alerts(); }
  }) }, "Clear all alerts…"),
  el("button", { type: "button", onclick: () => guarded(async () => { await api("/api/reload", {}); showMessage("Reloaded", "config.yaml was reloaded."); }) }, "Reload config"),
], "small subtle"));

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
// The star before an important sender (people.py; `important: true` on the item from the server; important.js marks them).
const importantStar = () => el("span", { class: "imp-star", title: "Important sender", "aria-label": "Important sender", role: "img" }, "★");
const ALERT_SOURCES = { Outlook: "outlook", Calendar: "calendar", Teams: "teams", Pulse: "pulse", Website: "website",
  Order: "sql", Toolkit: "sql", Snooze: "outlook", "Follow-up": "sent", Urgent: "outlook" };
function alertSource(a) {
  if (a.source === "Urgent" && /teams/i.test(a.title)) return "teams";
  if (a.source === "Outlook" && /teams message/i.test(a.title)) return "teams";
  return ALERT_SOURCES[a.source] || "system";
}

// The Mail tab: every unread Inbox email on top ("Unread"), then the newest mail ("Earlier"); or a search of the
// Inbox and Sent Items (subject, people, text). Rows are two lines: who and when, then the subject and a one-line
// preview; the address shows on hover and in the reader. An email read here stays in the Unread section (shown
// read) until the list is refreshed, so the list doesn't jump. On a narrow pane the list and the reader take turns.
// An email opened from elsewhere (View on a follow-up, the calendar…) that isn't in the list is pinned at the top.
let pinnedMail = null;
// Inbox | Sent picks the list (remembered in cc.mail.folder); All | Unread applies to the Inbox only.
const mailState = { query: "", filter: store.get("cc.mail.filter") === "unread" ? "unread" : "all",
  folder: store.get("cc.mail.folder") === "sent" ? "sent" : "inbox", sticky: new Set(), seq: 0, data: null };

// Today: the time; this week: the weekday; this year: "Oct 21"; older: the date.
function mailTime(iso) {
  if (!iso) return "";
  const d = new Date(iso), now = new Date();
  if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (now - d < 6 * 864e5 && d < now) return d.toLocaleDateString([], { weekday: "short" });
  if (d.getFullYear() === now.getFullYear()) return d.toLocaleDateString([], { month: "short", day: "numeric" });
  return d.toLocaleDateString([], { year: "2-digit", month: "numeric", day: "numeric" });
}

function mailRow(m) {
  const sent = m.folder === "Sent";
  const who = sent ? `To: ${m.to || "(no recipients)"}` : m.sender_name || m.sender || "(unknown sender)";
  return el("li", {
      class: `mail-row clickable ${m.unread ? "unread" : ""} ${m.id === selectedMail ? "selected" : ""}`, "data-id": m.id, tabindex: "0",
      "aria-label": `${m.unread ? "Unread. " : ""}${who}. ${m.subject}. ${when(m.received)}`,
      onclick: () => showMail(m.id),
      onkeydown: (e) => { if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); showMail(m.id); } },
    },
    el("span", { class: "mr-dot", "aria-hidden": "true" }),
    el("div", { class: "mr-main" },
      el("div", { class: "mr-top" },
        el("span", { class: "mr-from", title: sent ? m.to || "" : fromLine(m.sender_name, m.sender) }, m.important ? importantStar() : "", who),
        !sent && m.sender && m.sender_name && el("span", { class: "mr-addr" }, m.sender),
        el("span", { class: "mr-icons" },
          sent && mailState.data?.query && el("span", { class: "tag" }, "Sent"), m.pinned && el("span", { class: "tag" }, "Opened"),
          m.high_importance && el("span", { class: "mr-imp", title: "High importance" }, "❗"),
          m.flagged && el("span", { class: "flag", title: "Flagged" }, "⚑")),
        el("time", { class: "mr-time", datetime: m.received || "", title: when(m.received) }, mailTime(m.received))),
      el("div", { class: "mr-subj" }, m.subject),
      m.preview && el("div", { class: "mr-prev" }, m.preview.slice(0, 220))),
    el("div", { class: "mr-acts" }, mailActions(m, "row")));
}

const mailGroup = (label, n) => el("li", { class: "mail-group", role: "presentation" }, el("span", {}, label), n != null && el("span", { class: "chip" }, String(n)));

function renderMail() {
  const r = mailState.data;
  if (!r) return;
  let mail = r.mail.slice();
  if (pinnedMail && pinnedMail.id === selectedMail && !mail.some((m) => m.id === pinnedMail.id)) mail.unshift(pinnedMail);
  const sent = r.folder === "sent";
  const unreadish = (m) => !sent && (m.unread || mailState.sticky.has(m.id));
  if (mailState.filter === "unread" && !sent && !r.query) mail = mail.filter((m) => unreadish(m) || m.pinned);
  const byDate = (a, b) => String(b.received || "").localeCompare(String(a.received || ""));
  const pinned = mail.filter((m) => m.pinned);
  const unread = mail.filter((m) => !m.pinned && unreadish(m)).sort(byDate);
  const rest = mail.filter((m) => !m.pinned && !unreadish(m)).sort(byDate);
  const rows = [];
  if (pinned.length) rows.push(mailGroup("Opened"), ...pinned.map(mailRow));
  if (unread.length) rows.push(mailGroup("Unread", unread.filter((m) => m.unread).length), ...unread.map(mailRow));
  if (rest.length) rows.push(mailGroup(r.query ? (unread.length ? "Other results" : "Results") : sent ? "Sent" : "Earlier"), ...rest.map(mailRow));
  const empty = r.query ? `Nothing in the Inbox or Sent Items matches “${r.query}”.` : sent ? "No sent mail"
    : mailState.filter === "unread" ? "No unread mail. Inbox zero." : "No mail";
  fill($("#mail-list"), rows, empty);
  const count = $("#mail-unread-count");
  count.hidden = !r.unread_total;
  count.textContent = r.unread_total || "";
  setMailStatus(r.query ? [`${r.mail.length}${r.mail.length >= 60 ? "+" : ""} result${r.mail.length === 1 ? "" : "s"} for “${r.query}” in the Inbox and Sent Items `,
    el("button", { type: "button", class: "small subtle", onclick: () => setMailQuery("") }, "Clear")] : null);
}

function setMailStatus(parts, cls = "") {
  const box = $("#mail-status");
  box.hidden = !parts;
  box.className = `mail-status ${cls}`;
  box.replaceChildren(...[parts || []].flat().filter(Boolean));
}

loaders.mail = async () => {
  const my = ++mailState.seq, q = mailState.query;
  if (q) setMailStatus(`Searching for “${q}”…`);
  try {
    const folder = mailState.folder;
    const r = await api(`/api/mail?unread_only=${mailState.filter === "unread" && !q && folder === "inbox"}&folder=${folder}&q=${encodeURIComponent(q)}`);
    if (my !== mailState.seq) return;  // a newer search or filter is on its way
    mailState.data = r;
    renderMail();
  } catch (e) {
    if (my !== mailState.seq) return;
    setMailStatus(e.message, "err");
  }
};

// Refresh, a new search or another filter starts the list over (emails read meanwhile leave the Unread section).
function reloadMail() { mailState.sticky.clear(); return loaders.mail(); }
function setMailQuery(q) {
  mailState.query = q;
  if ($("#mail-search").value !== q) $("#mail-search").value = q;
  reloadMail();
}
function setMailFilter(f) {
  mailState.filter = f;
  store.set("cc.mail.filter", f === "unread" ? "unread" : null);
  $("#mail-f-all").setAttribute("aria-pressed", String(f !== "unread"));
  $("#mail-f-unread").setAttribute("aria-pressed", String(f === "unread"));
  reloadMail();
}
function showMailFolder() {
  $("#mail-d-inbox").setAttribute("aria-pressed", String(mailState.folder === "inbox"));
  $("#mail-d-sent").setAttribute("aria-pressed", String(mailState.folder === "sent"));
  $("#mail-filter").hidden = mailState.folder === "sent";
}
function setMailFolder(f) {
  mailState.folder = f;
  store.set("cc.mail.folder", f === "sent" ? "sent" : null);
  showMailFolder();
  if (mailState.query) setMailQuery("");  // a folder pick ends a search (search covers both folders)
  else reloadMail();
}

function closeReader() {
  const id = selectedMail;
  selectedMail = null;
  $("#tab-mail").classList.remove("reading");
  $("#mail-view").className = "panel empty";
  $("#mail-view").textContent = "Select a message";
  document.querySelectorAll("#mail-list li.selected").forEach((li) => li.classList.remove("selected"));
  if (id) document.querySelector(`#mail-list li[data-id="${CSS.escape(id)}"]`)?.focus();
}

async function showMail(id) {
  selectedMail = id;
  $("#tab-mail").classList.add("reading");
  const view = $("#mail-view");
  view.className = "panel";
  view.replaceChildren(el("div", { class: "muted" }, "Loading…"));
  document.querySelectorAll("#mail-list li.mail-row").forEach((li) => li.classList.toggle("selected", li.dataset.id === id));
  await guarded(async () => {
    const m = await api(`/api/mail/item?id=${encodeURIComponent(id)}`);
    // Reading it closes its alerts too (server side, both ways); the badge follows.
    const seen = m.unread ? await api("/api/mail/read", { id, read: true }) : await api("/api/alerts/read-mail", { id });
    if (m.unread) {
      m.unread = false;
      mailState.sticky.add(id);  // stays in the Unread section until the list is refreshed
      const row = mailState.data?.mail.find((x) => x.id === id);
      if (row) { row.unread = false; renderMail(); }
    }
    if (seen?.alerts_read) { refresh(); if ($("#tab-alerts").classList.contains("active")) loaders.alerts(); }
    if (selectedMail !== id) return;  // another email was opened meanwhile
    const act = (path, body, msg) => guarded(async () => { await api(path, body); if (msg) showMessage("Done", msg); loaders.mail(); showMail(id); });
    const more = () => [
      el("button", { type: "button", onclick: () => newTaskDialog({ subject: m.subject, notes: `From ${m.sender_name} <${m.sender}>, ${when(m.received)}` }) }, "+ Task"),
      el("button", { type: "button", onclick: () => snoozeDialog({ id, subject: m.subject, who: m.sender_name, kind: "mail" }) }, "Snooze"),
      m.sender && el("button", { type: "button", onclick: () => openTeamsChat(m.sender, "") }, "Chat in Teams"),
      el("button", { type: "button", onclick: () => guarded(() => api("/api/outlook/open", { id })) }, "Open in Outlook"),
      ...mailActions(m, "reader"),
    ].filter(Boolean);
    view.replaceChildren(
      el("div", { class: "mv-head" },
        el("button", { type: "button", class: "small subtle mail-back", onclick: closeReader }, "← Back to list"),
        el("h2", {}, m.subject),
        el("div", { class: "mv-from" }, m.important ? importantStar() : "", el("b", {}, m.sender_name || m.sender),
          m.sender && m.sender_name && el("span", { class: "muted" }, ` <${m.sender}>`),
          el("span", { class: "mv-when muted" }, when(m.received))),
        el("div", { class: "mv-to muted" }, `To: ${m.to}`, m.cc ? ` · Cc: ${m.cc}` : "")),
      el("div", { class: "mv-actions" },
        el("button", { type: "button", class: "small primary", onclick: () => replyDialog(m, false) }, "Reply"),
        el("button", { type: "button", class: "small", onclick: () => replyDialog(m, true) }, "Reply all"),
        el("button", { type: "button", class: "small", onclick: () => act("/api/mail/read", { id, read: m.unread }) }, m.unread ? "Mark read" : "Mark unread"),
        el("button", { type: "button", class: "small", onclick: () => act("/api/mail/flag", { id, flagged: !m.flagged }) }, m.flagged ? "Clear flag" : "⚑ Flag"),
        menuButton("More ▾", "More actions for this email", more, "small subtle", "left")),
      orderChips,
      el("pre", { class: "mv-body" }, m.body),
    );
    view.scrollTop = 0;
    const listed = [...document.querySelectorAll("#mail-list li.mail-row")].some((li) => li.dataset.id === id);
    if (!listed || pinnedMail?.id === id) {
      pinnedMail = { id, subject: m.subject, sender_name: m.sender_name, sender: m.sender, received: m.received,
        preview: (m.body || "").replace(/\s+/g, " "), flagged: m.flagged, high_importance: m.high_importance, important: m.important, pinned: true };
      if (!listed) { if (mailState.data) renderMail(); else await loaders.mail(); }
    }
    document.querySelectorAll("#mail-list li.mail-row").forEach((li) => li.classList.toggle("selected", li.dataset.id === id));
    document.querySelector("#mail-list li.selected")?.scrollIntoView({ block: "nearest" });
    renderOrderChips(orderChips, `${m.subject}\n${m.body}`, id);
  });
}

// Search as you type (after a pause; Enter at once, Esc clears); All | Unread; Refresh; arrow keys move along the list.
{
  const input = $("#mail-search");
  let timer = 0;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length === 1) return;  // wait for a second character
    timer = setTimeout(() => setMailQuery(q), 400);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); clearTimeout(timer); setMailQuery(input.value.trim().length >= 2 ? input.value.trim() : ""); }
    else if (e.key === "Escape" && input.value) { e.preventDefault(); e.stopPropagation(); clearTimeout(timer); setMailQuery(""); }
    else if (e.key === "ArrowDown") { e.preventDefault(); document.querySelector("#mail-list li.mail-row")?.focus(); }
  });
  $("#mail-d-inbox").addEventListener("click", () => setMailFolder("inbox"));
  $("#mail-d-sent").addEventListener("click", () => setMailFolder("sent"));
  showMailFolder();
  $("#mail-f-all").addEventListener("click", () => setMailFilter("all"));
  $("#mail-f-unread").addEventListener("click", () => setMailFilter("unread"));
  $("#mail-refresh").addEventListener("click", reloadMail);
  $("#mail-list").addEventListener("keydown", (e) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    const rows = [...document.querySelectorAll("#mail-list li.mail-row")];
    const i = rows.indexOf(document.activeElement);
    if (i < 0) return;
    e.preventDefault();
    rows[Math.max(0, Math.min(rows.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)))].focus();
  });
  $("#mail-f-all").setAttribute("aria-pressed", String(mailState.filter !== "unread"));
  $("#mail-f-unread").setAttribute("aria-pressed", String(mailState.filter === "unread"));
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
      el("div", { class: "row" }, el("button", {}, "Close")));
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

// Replies are always verified before sending: Review shows exactly who gets it and what it says, and the
// server only sends a reply that came from that review (single-use approval, recipients re-checked).
// Unsent text is kept per message in memory only (Esc / Cancel don't lose it), and dropped once it's sent or
// opened as an Outlook draft.
const replyDrafts = new Map();
const REPLY_DRAFTS_MAX = 25;
function keepDraft(id, text) {
  replyDrafts.delete(id);
  if (!text.trim()) return;
  replyDrafts.set(id, text);
  if (replyDrafts.size > REPLY_DRAFTS_MAX) replyDrafts.delete(replyDrafts.keys().next().value);
}

function replyDialog(m, replyAll, draft = null, remindDays = 0, notice = "") {
  const text = el("textarea", { placeholder: "Your reply…", "aria-label": "Reply text" });
  text.value = draft ?? replyDrafts.get(m.id) ?? "";
  text.addEventListener("input", () => keepDraft(m.id, text.value));
  const remind = el("select", { id: "remind-days" },
    [0, 1, 2, 3, 5].map((n) => el("option", { value: n }, n ? `${n} day${n > 1 ? "s" : ""}` : "Don't remind me")));
  remind.value = String(remindDays);
  const err = el("div", { class: "err", role: "alert" });
  const openDraft = () => guardedIn(err, async () => {
    if (!text.value.trim()) { text.focus(); return; }
    await api("/api/mail/reply", { id: m.id, text: text.value, reply_all: replyAll, send: false });
    replyDrafts.delete(m.id);
    $("#dialog").close();
    showMessage("Draft opened", "Check it and press Send in Outlook.");
  });
  const review = () => guardedIn(err, async () => {
    if (!text.value.trim()) { text.focus(); return; }
    const typed = text.value;
    const p = await api("/api/mail/reply/preview", { id: m.id, text: typed, reply_all: replyAll });
    reviewReply(m, replyAll, p, Number(remind.value), typed);
  });
  // Ctrl+Enter only opens the review (as in triage); Ctrl+Shift+Enter opens an Outlook draft. Nothing here sends.
  text.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || !(e.ctrlKey || e.metaKey) || e.isComposing) return;
    e.preventDefault();
    e.shiftKey ? openDraft() : review();
  });
  showDialog(
    el("h3", {}, `${replyAll ? "Reply all" : "Reply"}: ${m.subject}`), text,
    el("div", { class: "inline-field" },
      el("label", { for: "remind-days" }, "Remind me if no reply in"), remind,
      el("span", { class: "muted" }, "(when sent from here)")),
    err,
    el("div", { class: "row" },
      el("button", { value: "cancel" }, "Cancel"),
      el("button", { type: "button", onclick: openDraft }, "Open draft in Outlook"),
      el("button", { type: "button", class: "primary", onclick: review }, "Review ", el("kbd", {}, "Ctrl+Enter"))));
  err.textContent = notice;
  text.focus();
}

function reviewReply(m, replyAll, p, remindDays, typed = p.text) {
  const err = el("div", { class: "err", role: "alert" });
  const recips = Array.isArray(p.recipients) ? p.recipients.filter(Boolean) : [];
  const outside = recips.filter((r) => r.external);
  const person = (r) => el("span", { class: `rv-person ${r.external ? "external" : ""}`, title: r.external ? "Outside Sonance" : "" },
    r.external ? "⚠ " : "", r.name && r.email && r.name !== r.email ? `${r.name} <${r.email}>` : (r.email || r.name));
  const line = (label, type) => {
    const list = recips.filter((r) => r.type === type);
    return list.length ? el("div", { class: "rv-line" }, el("span", { class: "rv-label" }, label), el("div", { class: "rv-people" }, list.map(person))) : null;
  };
  const edit = (notice = "") => replyDialog(m, replyAll, typed, remindDays, notice);
  const sendBtn = el("button", { type: "button", class: "send-btn" }, "Send");
  sendBtn.addEventListener("click", async () => {
    sendBtn.disabled = true;
    try {
      await api("/api/mail/reply", { id: m.id, text: p.text, reply_all: replyAll, send: true, remind_days: remindDays, approval_id: p.approval_id });
    } catch (e) {
      // The approval is single-use and may be spent or expired: back to the text, so the next Review gets a fresh one.
      edit(`Not sent: ${e.message || "something went wrong"}`);
      return;
    }
    replyDrafts.delete(m.id);
    refresh();  // the email's alerts were removed with the reply
    if ($("#tab-alerts").classList.contains("active")) loaders.alerts();
    $("#dialog").close();
    showMessage("Sent", `Your reply was sent from Outlook.${remindDays ? ` If nobody replies within ${remindDays} day${remindDays > 1 ? "s" : ""}, you'll get an alert.` : ""}`);
  });
  // No recipients (e.g. Outlook couldn't resolve them): nothing to send to, so no Send button; a draft is offered.
  const openDraft = () => guardedIn(err, async () => {
    await api("/api/mail/reply", { id: m.id, text: typed, reply_all: replyAll, send: false });
    replyDrafts.delete(m.id);
    $("#dialog").close();
    showMessage("Draft opened", "Check the recipients and press Send in Outlook.");
  });
  showDialog(
    el("h3", {}, "Review before sending"),
    outside.length ? el("div", { class: "rv-warn" }, `⚠ ${outside.length} recipient${outside.length > 1 ? "s are" : " is"} outside Sonance`) : null,
    recips.length ? null : el("div", { class: "rv-warn" }, "No recipients — open a draft in Outlook instead."),
    el("div", { class: "rv-card" },
      line("To", "To"), line("CC", "CC"), line("BCC", "BCC"),
      el("div", { class: "rv-line" }, el("span", { class: "rv-label" }, "Subject"), el("div", {}, p.subject)),
      el("pre", { class: "rv-body" }, p.text)),
    el("p", { class: "muted" }, "The earlier thread is included below your text, as Outlook normally does.", remindDays && recips.length ? ` No-reply reminder: ${remindDays} day${remindDays > 1 ? "s" : ""}.` : ""),
    err,
    el("div", { class: "row" },
      el("button", { type: "button", class: "subtle", onclick: () => edit() }, "← Edit"),
      recips.length ? sendBtn : el("button", { type: "button", class: "primary", onclick: openDraft }, "Open draft in Outlook")));
}


// ---- calendar ----------------------------------------------------------------

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
    ["Important senders", f.unread_vip, (m) => hit(m.sender, m.sender_name), (m) => m.sender_name],
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
  meta = meta || (Array.isArray(todayData?.agenda) ? todayData.agenda : []).find((m) => m.id === id);
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
        meetingActions({ id, subject: p.subject, start: meta?.start || p.start, end: meta?.end || p.end }, "prep"),
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
  const head = (label) => el("div", { class: "weather-head" },
    el("span", { class: "panel-label" }, label), el("span", { class: "spacer" }), weatherToggle(w));
  if (w.error) {
    panel.replaceChildren(head("ENV.WEATHER"), el("p", { class: "err" }, w.error));
    return;
  }
  panel.replaceChildren(
    head(`ENV.WEATHER // ${w.location} ${w.zip}`),
    el("div", { class: "weather-main" },
      el("div", { class: "w-icon" }, w.icon),
      el("div", { class: "w-temp" }, w.temp, el("small", {}, w.unit)),
      el("div", { class: "w-info" },
        el("div", { class: "w-desc" }, w.text),
        // Each piece wraps as a whole (never "6:06" on one line and "PM" on the next).
        el("div", { class: "w-loc" },
          el("span", {}, `FEELS ${w.feels_like}°`), " · ", el("span", {}, `H ${w.high}° / L ${w.low}°`), " · ",
          el("span", { title: "Sunrise" }, `☀ ${timeOf(w.sunrise)}`), " ", el("span", { title: "Sunset" }, `☾ ${timeOf(w.sunset)}`))),
      el("div", { class: "w-meters" },
        meter("Rain", `${w.rain_chance ?? 0}%`, w.rain_chance ?? 0, 100, false),
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

// Follow-up totals, shared by the stat tiles and the Follow-ups tab.
function followupCounts(d) {
  const f = d?.followups && !d.followups.error ? d.followups : {};
  return {
    total: ["urgent_reply", "needs_reply", "teams_reply", "awaiting_reply", "flagged", "unread_vip"].reduce((n, k) => n + (f[k]?.length || 0), 0),
    urgent: (f.urgent_reply?.length || 0) + (f.teams_reply || []).filter((m) => m.urgent).length,
    needs: f.needs_reply?.length || 0,
  };
}

// A stat tile. With onclick it is a button (Enter / Space too).
function statTile(n, label, color, { cls = "", title = "", onclick = null, sub = null } = {}) {
  const tile = el("div", { class: `stat ${cls}`, style: `--c:${color}` }, el("div", { class: "n" }, n), el("div", { class: "l" }, label),
    sub && el("div", { class: "sub" }, sub));
  if (title) tile.title = title;
  if (onclick) {
    tile.classList.add("clickable");
    tile.setAttribute("role", "button");
    tile.tabIndex = 0;
    tile.addEventListener("click", onclick);
    tile.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onclick(); } });
  }
  return tile;
}

// Tiles from feature modules: CC.addStatTile(fn(todayData) -> tile | [tiles] | null, or a Promise of one).
const statSources = [];
let statSeq = 0;

function pulseTiles() {
  // Pulse tiles appear once the Pulse connection is set up.
  return api("/api/pulse/summary").then((p) => {
    if (!p.configured || p.error) return [];
    const open = () => openTab("pulse-feedback");
    const tiles = [statTile(p.open_mine, "Pulse tickets on you", p.high_priority ? "var(--red)" : p.open_mine ? "var(--amber)" : "var(--accent)",
      { title: `${p.new_today} new today · ${p.high_priority} high priority · ${p.open_total} open`, onclick: open })];
    // Tickets waiting on Joe longer than the warn/overdue thresholds (business days); no tile while all are fresh.
    const over = p.aging_overdue || 0, warn = p.aging_warn || 0;
    if (p.aging_waiting && (over || warn)) {
      tiles.push(statTile(over || warn, over ? "Pulse overdue" : "Pulse aging", over ? "var(--red)" : "var(--amber)", {
        cls: over ? "alarm" : "", onclick: open,
        title: p.oldest_days != null
          ? `Oldest waiting ${p.oldest_days} business day${p.oldest_days === 1 ? "" : "s"}` + (p.oldest_ticket_no != null ? ` (#${p.oldest_ticket_no})` : "")
          : `${p.aging_waiting} waiting on you`,
      }));
    }
    return tiles;
  });
}

// A stat tile's rows, once shown: scrolled into view and briefly highlighted. The rows may still be loading (the
// Alerts tab fetches when opened), so it looks again for up to 2 s.
function spotlight(selector, tries = 10) {
  const nodes = [...document.querySelectorAll(selector)].filter((n) => n.offsetParent !== null);
  if (!nodes.length) { if (tries > 0) setTimeout(() => spotlight(selector, tries - 1), 200); return; }
  nodes[0].scrollIntoView({ block: "nearest", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  for (const n of nodes) { n.classList.remove("spotlight"); void n.offsetWidth; n.classList.add("spotlight"); }
  setTimeout(() => nodes.forEach((n) => n.classList.remove("spotlight")), 2600);
}

// Opens collapsed follow-up sections so their rows can be shown.
function expandFollowups(...keys) {
  for (const k of keys) document.querySelector(`.fu-toggle[aria-controls="fu-${k}"][aria-expanded="false"]`)?.click();
}

function renderStats(d) {
  const agenda = Array.isArray(d.agenda) ? d.agenda : [];
  const left = agenda.filter((m) => !m.lunch && new Date(m.end) > new Date()).length;
  const today = isoDate(new Date());
  const due = d.tasks.items.filter((t) => t.due && t.due <= today).length;
  const { total: fu, urgent, needs } = followupCounts(d);
  const go = (tab, selector, ...sections) => () => {
    selectTodayTab(tab, { focus: true });
    expandFollowups(...sections);
    spotlight(selector);
  };
  const FU_ALL = ["urgent", "reply", "teams", "awaiting", "flagged", "vip"];
  const builtIn = [
    statTile(urgent, "Urgent replies", urgent ? "var(--red)" : "var(--accent)", { cls: urgent ? "alarm" : "",
      onclick: go("follow", "#fu-urgent .fu-row, #fu-teams .fu-row.sev-urgent", "urgent", "teams") }),
    statTile(left, "Meetings left today", "var(--accent-2)",
      { onclick: go("agenda", "#agenda > li:not(.past):not(.slot):not(.weekend-row):not(.empty):not(.err)") }),
    statTile(due, "Tasks due / overdue", due ? "var(--amber)" : "var(--accent)", { onclick: go("tasks", "#tasks > li.overdue, #tasks > li.due-today") }),
    statTile(needs, "Need your reply", "var(--hot)", { onclick: go("follow", "#fu-reply .fu-row", "reply") }),
    statTile(fu, "Follow-ups total", "var(--accent-2)", { onclick: go("follow", "#followups .fu-row", ...FU_ALL) }),
    statTile(Math.max(0, lastUnread), "Unread alerts", lastUnread > 0 ? "var(--red)" : "var(--accent)",
      { onclick: () => { openTab("alerts"); spotlight("#alerts-list > li.unread"); } }),
  ];
  // Module and Pulse tiles can be async: everything is put in at once, in a fixed order, so nothing jumps or
  // flickers on a refresh; a source that fails or takes over 3 s is left out this time.
  const seq = ++statSeq;
  const settle = (fn) => Promise.race([Promise.resolve().then(fn), new Promise((r) => setTimeout(() => r(null), 3000))]).catch(() => null);
  Promise.all([settle(pulseTiles), ...statSources.map((fn) => settle(() => fn(d)))]).then((extra) => {
    if (seq !== statSeq) return;
    $("#stats").replaceChildren(...builtIn, ...extra.flat().filter((n) => n instanceof Node));
    balanceStats();
  });
}

function refreshStats() { if (todayData) renderStats(todayData); }

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
      el("div", { class: "muted slot-hint" }, "Drop a task here to block time")),
    el("div", { class: "actions" },
      el("button", { type: "button", class: "small subtle", title: "Block this time on your Outlook calendar", onclick: () => scheduleDialog(null, slot) }, "Block")));
}

function endDrag() { dragTask = null; document.body.classList.remove("drag-task"); }

function renderAgenda(agenda) {
  if (agenda.error) { fill($("#agenda"), [el("li", { class: "err" }, agenda.error)], ""); return; }
  const now = new Date();
  const rows = agenda.map((m) => {
    const start = new Date(m.start), end = new Date(m.end);
    const live = start <= now && now < end;
    if (m.lunch) return { at: start, node: el("li", { class: `lunch-row ${live ? "now" : end <= now ? "past" : ""}` },
      el("div", { class: "time-col" }, timeOf(m.start), el("div", { class: "muted" }, timeOf(m.end))),
      el("div", { class: "grow" },
        el("div", { class: "title" }, "🍽 Lunch", live && el("span", { class: "tag live" }, "Now")),
        el("div", { class: "muted src-line" }, srcTag("calendar", "lunch block"), "Not counted as a meeting"))) };
    return { at: start, node: el("li", { class: live ? "now" : end <= now ? "past" : "" },
      el("div", { class: "time-col" }, timeOf(m.start), el("div", { class: "muted" }, timeOf(m.end))),
      el("div", { class: "grow" },
        el("div", { class: "title" }, m.subject, live && el("span", { class: "tag live" }, "Live")),
        el("div", { class: "muted src-line" }, srcTag(m.join_url ? "meeting" : "calendar"), [m.location, m.organizer && `organizer ${m.organizer}`].filter(Boolean).join(" · "))),
      el("div", { class: "actions" },
        m.join_url && end > now && linkButton(m.join_url, "Join", "small primary"),
        end > now && el("button", { class: "small alt", title: "Attendees, recent mail and notes", onclick: () => prepDialog(m.id, m) }, "Prep"),
        meetingActions(m, "agenda"),
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

// Task priority: High / Normal / Low. Outlook / To Do tasks store it in Outlook's own Importance (it shows in
// Outlook and To Do too); Asana tasks keep it on this PC (today.py). The list is sorted by priority, then due date.
const PRIO = { high: ["High", "▲"], normal: ["Normal", ""], low: ["Low", "▼"] };
const PRIO_RANK = { high: 0, normal: 1, low: 2 };
const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
function sortTasks(items) {
  return items.sort((a, b) => (PRIO_RANK[a.priority] ?? 1) - (PRIO_RANK[b.priority] ?? 1)
    || (!a.due) - (!b.due) || cmp(a.due || "", b.due || "") || cmp(String(a.subject).toLowerCase(), String(b.subject).toLowerCase()));
}
const taskKey = (t) => `${t.source}:${t.id}`;

function priorityButton(task) {
  const p = PRIO[task.priority] ? task.priority : "normal";
  return menuButton([PRIO[p][1] && el("span", { "aria-hidden": "true" }, PRIO[p][1]), PRIO[p][0]],
    `Priority ${PRIO[p][0]}: change priority of "${task.subject}"`,
    () => Object.keys(PRIO).map((k) => el("button", { type: "button", role: "menuitemradio", "aria-checked": String(k === p),
      onclick: () => setTaskPriority(task, k) }, PRIO[k][1] ? `${PRIO[k][1]} ${PRIO[k][0]}` : PRIO[k][0])),
    `prio-pill ${p}`, "left");
}

async function setTaskPriority(task, priority) {
  if ((task.priority || "normal") === priority) return;
  const before = task.priority;
  const redraw = () => {
    if (!todayData) return;
    sortTasks(todayData.tasks.items);
    renderTasks(todayData.tasks);
    updateTodayTabs(todayData);
    requestAnimationFrame(() => $(`#tasks li[data-key="${CSS.escape(taskKey(task))}"] .prio-pill`)?.focus());
  };
  task.priority = priority;
  redraw();
  try {
    await api("/api/tasks/priority", { source: task.source, id: task.id, priority });
    toast(`${PRIO[priority][0]} priority${task.source === "outlook" ? " · saved in Outlook" : ""}: ${task.subject}`);
  } catch (e) {
    task.priority = before;
    redraw();
    showMessage("Priority not changed", e.message);
  }
}

function renderTasks(t) {
  closeMenu();
  asanaConnected = t.asana.connected;
  const btn = $("#asana-btn");
  btn.textContent = asanaConnected ? "Asana ✓" : "Connect Asana";
  btn.className = `small ${asanaConnected ? "" : "subtle"}`;
  const notes = [t.error && `Outlook: ${t.error}`, t.asana.error && `Asana: ${t.asana.error}`].filter(Boolean);
  $("#tasks-note").replaceChildren(...notes.map((n) => el("div", { class: "err" }, n)));
  const today = isoDate(new Date());
  fill($("#tasks"), t.items.map((task) => el("li", {
      class: `task ${task.due && task.due < today ? "overdue" : task.due === today ? "due-today" : ""} prio-${PRIO[task.priority] ? task.priority : "normal"}`,
      "data-key": taskKey(task),
      draggable: "true",
      title: "Drag onto an open slot in the agenda to block time for it (hover Today's Agenda to switch to it)",
      ondragstart: (e) => {
        dragTask = task;
        e.dataTransfer.effectAllowed = "copy";
        e.dataTransfer.setData("text/plain", task.subject);
        document.body.classList.add("drag-task");
      },
      ondragend: endDrag,
    },
    el("button", { class: "check", title: "Mark complete", "aria-label": `Mark "${task.subject}" complete`, onclick: () => completeTask(task) }),
    priorityButton(task),
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

// Blocks time on Joe's Outlook calendar (POST /api/calendar/block: a busy appointment with no attendees, 5-minute
// reminder). For a task (Schedule, or a task dropped on a free slot) or on its own (＋ Block time on the Agenda, a free
// slot's Block, Ctrl+K). The default title is never "Blocked": that subject at midday reads as Joe's lunch (_is_lunch).
function scheduleDialog(task, slot) {
  if (!slot && task) slot = todayData && freeSlots(todayData.agenda)[0];
  const subject = el("input", { value: task ? `Focus: ${task.subject}` : "Focus time", maxlength: "250", "aria-label": "Title" });
  const start = el("input", { type: "datetime-local", step: "300", "aria-label": "Start", value: localInput(slot ? slot.start : roundUp(new Date(), 15)) });
  const fits = slot ? (slot.end - slot.start) / 60000 : 120;
  const pick = fits >= 60 ? 60 : [45, 30, 15].find((m) => m <= fits) || 15;
  const len = el("select", { "aria-label": "Length" }, [15, 30, 45, 60, 90, 120, 180, 240].map((m) => el("option", { value: m, selected: m === pick }, duration(m))));
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
    if (/^\s*(blocked|lunch)\s*$/i.test(subject.value)) throw new Error("Use another title: “Blocked” or “Lunch” around midday counts as your lunch break.");
    await api("/api/calendar/block", {
      subject: subject.value.trim(), start: localIso(s), minutes,
      notes: task ? `Time blocked for: ${task.subject}${task.url ? "\n" + task.url : ""}` : "",
    });
    $("#dialog").close();
    showMessage("Time blocked", `“${subject.value.trim()}” is on your calendar: ${dayTime(s)} – ${timeOf(new Date(s.getTime() + minutes * 60000))}.`);
    loaders.today();
  });
  showDialog(el("h3", {}, task ? "Block time for a task" : "Block time on your calendar"),
    el("p", { class: "muted" }, task ? task.subject : "Adds a busy appointment to your Outlook calendar (no attendees, a reminder 5 minutes before)."),
    el("label", { class: "field-label" }, "Title"), subject, hint,
    el("div", { class: "two-col" },
      el("div", {}, el("label", { class: "field-label" }, "Start"), start),
      el("div", {}, el("label", { class: "field-label" }, "Length"), len)),
    err,
    el("div", { class: "row" }, el("button", { value: "cancel", class: "subtle" }, "Cancel"),
      el("button", { type: "button", class: "primary", onclick: save }, "Add to calendar")));
  subject.focus();
}

$("#block-time").addEventListener("click", () => scheduleDialog(null));

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

// ---- follow-ups: a dense list, one row per item ----------------------------------------------------------
// Each group (Urgent, Needs your reply, Teams, Awaiting their reply, Flagged, Important senders) is a list section with
// its count; a section can be collapsed (remembered per viewer in cc.today.fu.collapsed). A row shows the source
// tag, who (★ for an important sender), the subject, how long ago, and its main action; Snooze, Dismiss and the second
// action appear on hover or keyboard focus, and the rest (Teams chat, Mark urgent, Mute, module actions such as Mark
// important and Spam) are in its ⋯ menu.
const FU_COLLAPSED = "cc.today.fu.collapsed";
const fuCollapsed = (() => { try { return new Set(JSON.parse(store.get(FU_COLLAPSED) || "[]")); } catch { return new Set(); } })();

// "44m", "3h", "2d" since a time (the full date and time is the tooltip).
function ago(iso) {
  const mins = Math.round((Date.now() - new Date(iso)) / 60000);
  if (!iso || !Number.isFinite(mins)) return "";
  if (mins < 1) return "now";
  if (mins < 60) return `${mins}m`;
  return mins < 48 * 60 ? `${Math.floor(mins / 60)}h` : `${Math.floor(mins / 1440)}d`;
}

function fuSection(key, title, items, row, { empty, note, hot = false, urgent = false } = {}) {
  items = items || [];
  const id = `fu-${key}`;
  const collapsed = items.length > 0 && fuCollapsed.has(key);
  const list = items.length ? el("ul", { class: "list fu-rows", id, hidden: collapsed }, items.map(row)) : null;
  const noteEl = note ? el("div", { class: "fu-note", hidden: collapsed }, note) : null;
  const label = [el("span", { class: "fu-caret", "aria-hidden": "true" }), el("span", { class: "fu-sec-title" }, title), el("span", { class: "chip" }, items.length)];
  const toggle = (e) => {
    const open = e.currentTarget.getAttribute("aria-expanded") !== "true";
    e.currentTarget.setAttribute("aria-expanded", String(open));
    list.hidden = !open;
    if (noteEl) noteEl.hidden = !open;
    open ? fuCollapsed.delete(key) : fuCollapsed.add(key);
    store.set(FU_COLLAPSED, JSON.stringify([...fuCollapsed]));
  };
  return el("section", { class: `fu-sec${hot ? " hot" : ""}${urgent ? " urgent" : ""}${items.length ? "" : " is-empty"}`, "aria-label": title },
    el("h4", { class: "fu-sec-h" },
      items.length ? el("button", { type: "button", class: "fu-toggle", "aria-expanded": String(!collapsed), "aria-controls": id, onclick: toggle }, label)
                   : el("span", { class: "fu-toggle" }, label),
      !items.length && el("span", { class: "fu-empty" }, empty)),
    list, noteEl);
}

// One row. `more` builds the ⋯ menu's items (called again each time it opens); no items, no ⋯.
function fuRow(m, { cls = "", src, who, whoTitle = who, subject, about = subject, tags = [], at, primary, quick = [], more = () => [] }) {
  const extra = more().filter(Boolean);
  return el("li", { class: `fu-row ${cls}` },
    el("span", { class: "fu-src" }, src),
    el("span", { class: "fu-who", title: whoTitle }, m.important ? importantStar() : "", who),
    el("span", { class: "fu-subj", title: about }, tags.filter(Boolean), el("span", { class: "fu-subj-text" }, subject)),
    el("span", { class: "fu-age", title: at ? when(at) : "" }, ago(at)),
    el("span", { class: "fu-acts" }, primary, el("span", { class: "fu-quick" }, quick.filter(Boolean)),
      extra.length ? menuButton("⋯", `More actions: ${m.subject || "this item"}`, () => more().filter(Boolean), "icon subtle fu-more") : null));
}

const viewMail = (id) => el("button", { class: "small", onclick: () => { openTab("mail"); showMail(id); } }, "View");
const fuView = (m) => el("button", { type: "button", class: "small", "aria-label": `View "${m.subject}"`, onclick: () => { openTab("mail"); showMail(m.id); } }, "View");
const fuSnooze = (m, who = m.sender_name, kind = "mail") => el("button", { type: "button", class: "icon subtle", title: "Snooze",
  "aria-label": `Snooze "${m.subject}"`, onclick: () => snoozeDialog({ id: m.id, subject: m.subject, who, kind }) }, "Snooze");
const fuDismiss = (m) => el("button", { type: "button", class: "icon subtle", title: "Dismiss", "aria-label": `Dismiss "${m.subject}"`, onclick: () => dismissFollowup(m.id) }, "✕");
const fuMute = (who) => who && el("button", { type: "button", title: `Hide follow-ups from ${who}`, onclick: () => muteSender(who) }, "Mute sender");

function replyRow(m, { urgent = false } = {}) {
  const sender = m.sender || m.sender_name;
  return fuRow(m, {
    cls: `${m.unread ? "unread" : ""} ${urgent ? "sev-urgent" : ""}`,
    src: srcTag("outlook", m.internal ? "internal sender" : "external sender"),
    who: m.sender_name || m.sender, whoTitle: fromLine(m.sender_name, m.sender), subject: m.subject, at: m.received,
    tags: [urgent && el("span", { class: "tag urgent" }, "Urgent")],
    primary: el("button", { type: "button", class: "small primary", "aria-label": `Reply to "${m.subject}"`, onclick: () => replyDialog(m, false) }, "Reply"),
    quick: [fuView(m), fuSnooze(m), fuDismiss(m)],
    more: () => [
      m.sender && el("button", { type: "button", onclick: () => openTeamsChat(m.sender, "") }, "Chat in Teams"),
      !urgent && sender && el("button", { type: "button", title: `Always treat ${m.sender_name || m.sender} as urgent`, onclick: () => markUrgent(sender) }, "Mark urgent"),
      !urgent && fuMute(sender),
      ...mailActions(m, "followup")],
  });
}

function teamsRow(m) {
  const who = m.from_name || m.sender_name;
  const text = m.preview ? String(m.preview).slice(0, 200) : m.subject;
  return fuRow(m, {
    cls: `${m.unread ? "unread" : ""} ${m.urgent ? "sev-urgent" : ""}`,
    src: srcTag("teams", `notification from ${m.sender || "Teams"}`),
    who: m.from_name || m.subject, subject: text, about: [m.subject, m.preview].filter(Boolean).join(" — "), at: m.received,
    tags: [m.urgent && el("span", { class: "tag urgent" }, "Urgent")],
    primary: el("button", { type: "button", class: "small alt", onclick: () => openExternal("msteams:") }, "Open Teams"),
    quick: [el("button", { type: "button", class: "small", title: "Mark this Teams email read", "aria-label": `Done: ${m.subject}`,
      onclick: () => guarded(async () => { await api("/api/mail/read", { id: m.id, read: true }); loaders.today(); }) }, "Done"),
    fuSnooze(m, who), fuDismiss(m)],
    more: () => mailActions(m, "followup"),
  });
}

function awaitingRow(m) {
  const to = String(m.to || "").slice(0, 60);
  return fuRow(m, {
    src: srcTag("sent"), who: `To ${to}`, whoTitle: `To ${m.to || ""}`, subject: m.subject, at: m.sent,
    primary: el("button", { type: "button", class: "small", title: "Opens a follow-up draft in Outlook", onclick: () => nudge(m) }, "Nudge"),
    quick: [el("button", { type: "button", class: "small", title: "Alert me if nobody replies", onclick: () => remindDialog(m) }, "Remind me"),
      fuSnooze(m, `to ${to}`), fuDismiss(m)],
    more: () => mailActions(m, "followup"),
  });
}

function flaggedRow(m) {
  return fuRow(m, {
    src: srcTag("flagged"), who: m.sender_name || m.sender, whoTitle: fromLine(m.sender_name, m.sender), subject: m.subject, at: m.received,
    tags: [dueTag(m.due)],
    primary: fuView(m),
    quick: [el("button", { type: "button", class: "small", title: "Clear the flag in Outlook", "aria-label": `Unflag "${m.subject}"`,
      onclick: () => guarded(async () => { await api("/api/mail/flag", { id: m.id, flagged: false }); loaders.today(); }) }, "Unflag"),
    fuSnooze(m), fuDismiss(m)],
    more: () => [el("button", { type: "button", title: "Mark the flag complete in Outlook", onclick: () => completeTask({ source: "outlook", id: m.id, subject: m.subject }) }, "Mark flag complete"),
      fuMute(m.sender || m.sender_name), ...mailActions(m, "followup")],
  });
}

function vipRow(m) {
  return fuRow(m, {
    cls: "unread", src: srcTag("outlook", "important sender"), who: m.sender_name || m.sender, whoTitle: fromLine(m.sender_name, m.sender),
    subject: m.subject, at: m.received, primary: fuView(m), quick: [fuSnooze(m), fuDismiss(m)],
    more: () => [fuMute(m.sender || m.sender_name), ...mailActions(m, "followup")],
  });
}

function renderFollowups(f) {
  closeMenu();
  const box = $("#followups");
  if (f.error) { box.replaceChildren(el("div", { class: "err" }, f.error)); return; }
  const h = f.flagged_hidden || {};
  const hiddenNote = [h.more && `${h.more} more`, h.older && `${h.older} older`, h.automated && `${h.automated} automated`,
    h.before && `${h.before.toLocaleString()} flagged before ${new Date(`${h.since}T00:00`).toLocaleDateString([], { month: "short", day: "numeric" })}`]
    .filter(Boolean).join(" · ");
  box.replaceChildren(
    fuSection("urgent", "Urgent — reply needed", f.urgent_reply, (m) => replyRow(m, { urgent: true }),
      { empty: "None. Set who counts under Urgent people.", hot: !!f.urgent_reply?.length, urgent: true }),
    fuSection("reply", "Needs your reply", f.needs_reply, (m) => replyRow(m), { empty: "Nothing waiting on you." }),
    fuSection("teams", "Teams — reply needed", f.teams_reply, teamsRow, { empty: "No unread Teams messages.", hot: !!f.teams_reply?.some((m) => m.urgent) }),
    fuSection("awaiting", "Awaiting their reply", f.awaiting_reply, awaitingRow, { empty: "No one owes you a reply." }),
    fuSection("flagged", "Flagged", f.flagged, flaggedRow, { empty: "No recent flags.", note: hiddenNote && `Hidden: ${hiddenNote} (of ${Number(h.total || 0).toLocaleString()} flagged)` }),
    fuSection("vip", "Important senders", f.unread_vip, vipRow, { empty: "No unread mail from important senders." }));
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
    const input = el("input", { placeholder: "Full name (Jane Smith) or email address", maxlength: "120", "aria-label": "Add a person",
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

// In a narrow Today head the word is hidden (style.css .tw-word) and the button is just ↻; the name stays for hover and screen readers.
function rescanLabel(btn, word) {
  btn.replaceChildren("↻", el("span", { class: "tw-word" }, ` ${word}`));
  btn.title = word; btn.setAttribute("aria-label", word);
}

loaders.today = (refresh = false) => guarded(async () => {
  const btn = $("#today-refresh");
  btn.disabled = true; rescanLabel(btn, "Scanning…");
  loadTodayTeams();  // its own request: Teams shows even while Outlook is slow
  try {
    todayData = await api(`/api/today?refresh=${refresh}`);
    renderWeather(todayData.weather);
    renderAgenda(todayData.agenda);
    renderTasks(todayData.tasks);
    renderFollowups(todayData.followups);
    renderStats(todayData);
    renderSnoozedButton(todayData);
    updateTodayTabs(todayData);
    // Feature modules (static/<module>.js) can react to fresh Today data without editing this file.
    document.dispatchEvent(new CustomEvent("cc:today", { detail: todayData }));
  } finally {
    btn.disabled = false; rescanLabel(btn, "Rescan");
  }
});

$("#today-refresh").addEventListener("click", () => loaders.today(true));
$("#snoozed-btn").addEventListener("click", snoozedDialog);
$("#urgent-btn").addEventListener("click", urgentDialog);

// ---- small popup menus (⋯ on follow-up rows, task priority) ---------------------------------------------------
// role=menu under its button: arrow keys / Home / End move, Enter or Space picks, Esc and Tab close (focus goes
// back to the button). Closes on an outside click, any scroll or resize, and when the list under it re-renders.
let menuOpen = null;

function closeMenu(refocus = false) {
  if (!menuOpen) return;
  const { btn, menu } = menuOpen;
  menuOpen = null;
  menu.remove();
  btn.setAttribute("aria-expanded", "false");
  if (refocus && btn.isConnected) btn.focus();
}

function showMenu(btn, nodes, label) {
  closeMenu();
  const menu = el("div", { class: "cc-menu", role: "menu", "aria-label": label }, nodes);
  for (const n of nodes) {
    if (!String(n.getAttribute("role") || "").startsWith("menuitem")) n.setAttribute("role", "menuitem");
    n.tabIndex = -1;
    n.classList.add("cc-menu-item");
  }
  document.body.append(menu);
  const r = btn.getBoundingClientRect(), mr = menu.getBoundingClientRect();
  menu.style.top = `${r.bottom + mr.height + 8 > innerHeight ? Math.max(8, r.top - mr.height - 4) : r.bottom + 4}px`;
  const x = btn.dataset.menuAlign === "left" ? r.left : r.right - mr.width;  // under the button, on its own side
  menu.style.left = `${Math.max(8, Math.min(innerWidth - mr.width - 8, x))}px`;
  btn.setAttribute("aria-expanded", "true");
  menuOpen = { btn, menu, at: performance.now() };
  const items = () => [...menu.querySelectorAll("[role^=menuitem]")].filter((b) => !b.disabled);
  menu.addEventListener("keydown", (e) => {
    const list = items(), i = list.indexOf(document.activeElement);
    const go = (j) => { e.preventDefault(); list[(j + list.length) % list.length]?.focus(); };
    if (e.key === "ArrowDown") go(i + 1);
    else if (e.key === "ArrowUp") go(i - 1);
    else if (e.key === "Home") go(0);
    else if (e.key === "End") go(list.length - 1);
    else if (e.key === "Escape" || e.key === "Tab") { e.preventDefault(); e.stopPropagation(); closeMenu(true); }
  });
  // Closes after the item's own handler (which may stop the click's propagation); if that opened a dialog, the
  // dialog keeps the focus.
  menu.addEventListener("click", (e) => {
    if (!e.target.closest("[role^=menuitem]")) return;
    const mine = menuOpen;
    setTimeout(() => { if (menuOpen === mine) closeMenu(!document.querySelector("dialog[open]")); });
  }, true);
  (menu.querySelector("[aria-checked=true]") || items()[0])?.focus();
}

function menuButton(label, name, build, cls = "icon subtle", align = "right") {
  const open = () => showMenu(btn, build(), name);
  const btn = el("button", { type: "button", class: cls, "aria-haspopup": "menu", "aria-expanded": "false", "aria-label": name, title: name,
    "data-menu-align": align,
    onclick: (e) => { e.stopPropagation(); menuOpen?.btn === btn ? closeMenu(true) : open(); },
    onkeydown: (e) => { if (e.key === "ArrowDown") { e.preventDefault(); open(); } } }, label);
  return btn;
}

document.addEventListener("pointerdown", (e) => { if (menuOpen && !menuOpen.menu.contains(e.target) && !menuOpen.btn.contains(e.target)) closeMenu(); }, true);
// (A scroll right after opening is the button being scrolled into view: ignored.)
document.addEventListener("scroll", (e) => { if (menuOpen && !menuOpen.menu.contains(e.target) && performance.now() - menuOpen.at > 250) closeMenu(); }, true);
window.addEventListener("resize", () => closeMenu());

// The Follow-ups lists (urgent people, important senders, snoozed, muted) sit behind one "Manage" menu so the
// tools stay on the tabs' row; each item clicks its button in #fu-manage, which keeps its own label and handler.
$("#today-refresh").before(menuButton("Manage ▾", "Manage follow-up lists",
  () => [...$("#fu-manage").querySelectorAll("button")].map((b) =>
    el("button", { type: "button", title: b.title, onclick: () => b.click() }, b.textContent)), "small subtle"));

// ---- Today's main panel: Today's Agenda / Tasks / Follow-ups tabs ------------------------------------------
// ARIA tabs (arrow keys, Home / End; the tools next to them follow the open tab). The tab last picked is
// remembered per viewer (cc.today.tab); with nothing remembered yet, Today opens on Follow-ups when something is
// urgent, else on the agenda. Urgent follow-ups always show as a red count on the Follow-ups tab.
const TW = ["agenda", "tasks", "follow"];
let twPicked = TW.includes(store.get("cc.today.tab")) ? store.get("cc.today.tab") : null;  // remembered choice
let twTab = twPicked || "agenda";  // the tab showing
let twAuto = false;  // the first-data default was applied

function selectTodayTab(name, { save = true, focus = false } = {}) {
  if (!TW.includes(name)) return;
  twTab = name;
  if (save) { twPicked = name; store.set("cc.today.tab", name); }
  for (const t of TW) {
    const on = t === name, tab = $(`#tw-tab-${t}`);
    tab.setAttribute("aria-selected", String(on));
    tab.tabIndex = on ? 0 : -1;
    $(`#tw-${t}`).hidden = !on;
    document.querySelector(`.tw-tools[data-for="${t}"]`).hidden = !on;
  }
  closeMenu();
  if (focus) {
    if (!$("#tab-today").classList.contains("active")) openTab("today");
    $(`#tw-tab-${name}`).focus();
  }
}

function setBadge(sel, n, text) {
  const b = $(sel);
  b.hidden = !n;
  b.textContent = n ? text : "";
}

function updateTodayTabs(d) {
  const agenda = Array.isArray(d.agenda) ? d.agenda : [];
  $("#agenda-count").textContent = d.agenda?.error ? "!" : agenda.filter((m) => !m.lunch).length;
  const items = d.tasks?.items || [];
  const high = items.filter((t) => t.priority === "high").length;
  $("#tasks-count").textContent = items.length;
  setBadge("#tasks-high", high, `${high} high`);
  const { total, urgent } = followupCounts(d);
  $("#follow-count").textContent = d.followups?.error ? "!" : total;
  setBadge("#follow-urgent", urgent, `${urgent} urgent`);
  if (!twPicked && !twAuto) {
    twAuto = true;
    selectTodayTab(urgent ? "follow" : "agenda", { save: false });
  }
}

$(".tw-tabs").addEventListener("click", (e) => { const b = e.target.closest("[role=tab]"); if (b) selectTodayTab(b.dataset.tw); });
$(".tw-tabs").addEventListener("keydown", (e) => {
  const i = TW.indexOf(e.target.closest("[role=tab]")?.dataset.tw || twTab);
  const to = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: TW.length - 1 }[e.key];
  if (to === undefined) return;
  e.preventDefault();
  selectTodayTab(TW[(to + TW.length) % TW.length], { focus: true });
});
// Dragging a task: hovering the Today's Agenda tab opens it, so the task can be dropped on an open slot.
let twDragTimer = 0;
$("#tw-tab-agenda").addEventListener("dragenter", () => {
  if (!dragTask || twTab === "agenda") return;
  clearTimeout(twDragTimer);
  twDragTimer = setTimeout(() => { if (dragTask) selectTodayTab("agenda", { save: false }); }, 300);
});
$("#tw-tab-agenda").addEventListener("dragleave", () => clearTimeout(twDragTimer));
selectTodayTab(twTab, { save: false });

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
  if (!await confirmDialog("Log in to Teams", "A browser window will open on Teams. Sign in (including MFA), wait until your chats load, then close that window. Command Center will then check your unread DMs and mentions every few minutes (read-only).", "Open browser")) return;
  guarded(() => api("/api/teams/login", {}));
}
// Teams alerts carry a deep link to the chat or message (action type "teams"): the whole chat in the Teams tab's
// web view when the desktop app has it (teamsview.js), else the Teams app.
alertActionTypes.teams = (act) => {
  const link = teamsLink(act.url);
  return link && CC.teamsWeb?.available
    ? el("button", { type: "button", class: "small", onclick: () => CC.teamsWeb.open(link) }, "Open chat")
    : linkButton(link || "msteams:", "Open in Teams");
};

// Counts from the background check (teams.py): DMs, group / meeting chats and @mentions unread; null = not readable.
const teamsPlural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
function teamsTip(s) {
  if (!s || s.badge == null) return s?.unread ? `${teamsPlural(s.unread, "unread item")} in Teams (details unavailable)` : "";
  return [teamsPlural(s.dms_unread || 0, "DM"), s.groups_unread ? teamsPlural(s.groups_unread, "group chat") : "",
    teamsPlural(s.mentions_unread || 0, "mention")].filter(Boolean).join(" · ");
}
// The number on the Teams tab: unread DMs + group chats + @mentions (the title count when details can't be read).
let teamsChecked = null;
function renderTeamsBadge(s) {
  const badge = $("#teams-badge");
  if (!badge) return;
  const n = !s || s.ok === false ? 0 : s.badge ?? s.unread ?? 0;
  badge.hidden = !n;
  badge.textContent = n > 99 ? "99+" : String(n);
  badge.closest("button").title = n ? teamsTip(s) : "";
  // a new background read while the Teams tab or Today is open: show it
  if (s?.checked && teamsChecked && s.checked !== teamsChecked && $("#tab-teams").classList.contains("active")) loaders.teams();
  if (s?.checked && teamsChecked && s.checked !== teamsChecked && $("#tab-today").classList.contains("active")) loadTodayTeams();
  teamsChecked = s?.checked || teamsChecked;
}

function teamsCounts(s) {
  const count = (n, label, cls = "") => el("div", { class: `teams-count ${n ? cls : ""}` },
    el("div", { class: "n" }, n ?? "–"), el("div", { class: "l" }, label));
  if (s.badge == null) {  // only the page title could be read
    return [el("div", { class: "teams-counts" }, count(s.unread, "unread in Teams", "hot")),
      s.ok !== false && s.checked && el("p", { class: "muted small" }, "DM and mention details unavailable on the last check; this is Teams' own unread total.")];
  }
  const others = [s.groups_unread ? teamsPlural(s.groups_unread, "group chat") : "", s.meetings_unread ? teamsPlural(s.meetings_unread, "meeting chat") : ""].filter(Boolean);
  return [el("div", { class: "teams-counts", role: "group", "aria-label": "Unread in Teams" },
      count(s.dms_unread, s.dms_unread === 1 ? "unread DM" : "unread DMs", "hot"),
      count(s.mentions_unread, s.mentions_unread === 1 ? "unread mention" : "unread mentions", "hot")),
    el("div", { class: "muted small" }, others.length ? `Also unread: ${others.join(", ")}` : "No unread group or meeting chats",
      s.unread != null ? ` · Teams total ${s.unread}` : "")];
}

// Recent DMs and @mentions (newest first). Links come from teams.py; only Teams deep links are opened.
const TEAMS_KIND = { dm: ["DM", "Direct message"], group: ["Group", "Group chat"], meeting: ["Meeting", "Meeting chat"], mention: ["Mention", "@mention of you"] };
const teamsLink = (u) => (typeof u === "string" && /^(msteams:\/l\/|https:\/\/teams\.microsoft\.com\/l\/)[^\s"<>\\]+$/.test(u) ? u : null);
// Opens a chat from a Recent row: the Teams tab's web view (CC.teamsWeb switches to the tab), else the Teams app.
const openTeamsRecent = (link) => (CC.teamsWeb ? CC.teamsWeb.open(link) : openExternal(link));
// today: the row is on the Today tab, where Open chat always goes to the Teams tab (CC.teamsWeb falls back by itself).
function teamsRecentRow(r, today = false) {
  const [label, about] = TEAMS_KIND[r.kind] || ["Teams", "Teams"];
  const link = teamsLink(r.link);
  if (today) return fuRow(r, {
    cls: `teams-row ${r.unread ? "unread" : ""} ${r.urgent ? "sev-urgent" : ""} ${link ? "teams-open" : ""}`,
    src: el("span", { class: `src src-teams tk-${TEAMS_KIND[r.kind] ? r.kind : "dm"}`, title: `Microsoft Teams · ${about}${r.unread ? " · unread" : ""}` }, label),
    who: r.who, whoTitle: r.where ? `${r.who} · ${r.where}` : r.who,
    subject: r.preview || (r.kind === "mention" ? "mentioned you" : "(no preview)"),
    about: [r.where && `in ${r.where}`, r.preview].filter(Boolean).join(": "),
    tags: [r.urgent && el("span", { class: "tag urgent" }, "Urgent"), r.count > 1 && el("span", { class: "tag teams-n", title: `${r.count} unread messages` }, r.count)],
    at: r.time,
    primary: link ? el("button", { type: "button", class: "small primary", title: "Open this chat in the Teams tab", "aria-label": `Open chat: ${r.who}`, onclick: () => openTeamsRecent(link) }, "Open chat")
      : el("button", { type: "button", class: "small alt", onclick: () => openTab("teams") }, "Teams"),
    quick: [],
  });
  return fuRow(r, {
    cls: `teams-row ${r.unread ? "unread" : ""} ${r.urgent ? "sev-urgent" : ""}`,
    src: el("span", { class: `src src-teams tk-${TEAMS_KIND[r.kind] ? r.kind : "dm"}`, title: `Microsoft Teams · ${about}${r.unread ? " · unread" : ""}` }, label),
    who: r.who, whoTitle: r.where ? `${r.who} · ${r.where}` : r.who,
    subject: r.preview || (r.kind === "mention" ? "mentioned you" : "(no preview)"),
    about: [r.where && `in ${r.where}`, r.preview].filter(Boolean).join(": "),
    tags: [r.urgent && el("span", { class: "tag urgent" }, "Urgent"), r.count > 1 && el("span", { class: "tag teams-n", title: `${r.count} unread messages` }, r.count),
      r.where && el("span", { class: "tag teams-where" }, r.where)],
    at: r.time,
    // the whole chat in the web view beside the list (teamsview.js) when the desktop app has it, else the Teams app
    primary: link && CC.teamsWeb?.available
      ? el("button", { type: "button", class: "small primary", title: "Open the whole chat here, to read and reply", "aria-label": `Open chat: ${r.who}`, onclick: () => CC.teamsWeb.open(link) }, "Open chat")
      : link
        ? el("button", { type: "button", class: "small alt", title: "Open this chat in the Teams app", "aria-label": `Open in Teams: ${r.who}`, onclick: () => openExternal(link) }, "Open")
        : el("button", { type: "button", class: "small alt", onclick: () => openExternal("msteams:") }, "Open Teams"),
    quick: [link && CC.teamsWeb?.available && el("button", { type: "button", class: "small subtle", title: "Open this chat in the Teams app (calls, meetings)", "aria-label": `Open in the Teams app: ${r.who}`, onclick: () => CC.teamsWeb.app(link) }, "App")],
  });
}
function renderTeamsRecent(t) {
  const s = t.state || {};
  const recent = Array.isArray(t.recent) ? t.recent : [];
  $("#teams-recent-note").textContent = t.read_at ? `read ${when(t.read_at)}` : "";
  const empty = !t.set_up ? "Log in to Teams to see your DMs and mentions here."
    : !t.read_at ? "Shows up after the next background check (or Check now)."
    : s.details === "unavailable" ? "Details unavailable on the last check (only Teams' unread total could be read)."
    : "No DMs or mentions in the last 3 days.";
  fill($("#teams-recent-list"), recent.map((r) => teamsRecentRow(r)), empty);
  const unread = recent.filter((r) => r.unread).length;
  $("#teams-recent-count").textContent = unread;
  $("#teams-recent-count").hidden = !unread;
  $("#teams-recent-count").title = `${unread} unread`;
  // a click anywhere on a row (not on its buttons) opens that chat in the web view too
  $("#teams-recent-list").querySelectorAll("li.teams-row").forEach((li, i) => {
    const link = teamsLink(recent[i]?.link);
    if (!link || !CC.teamsWeb?.available) return;
    li.classList.add("teams-open");
    li.addEventListener("click", (e) => { if (!e.target.closest("button, a, input")) CC.teamsWeb.open(link); });
  });
}
// The Teams tab's Recent list is collapsed unless Joe opened it (remembered per viewer); Today shows the newest ones.
$("#teams-recent").open = store.get("cc.teams.recentOpen") === "1";
$("#teams-recent").addEventListener("toggle", (e) => store.set("cc.teams.recentOpen", e.currentTarget.open ? "1" : null));

// Today: the newest Recent DMs and mentions under the main panel (GET /api/teams/recent: the background read only,
// no Outlook). A row, or its Open chat, opens that chat in the Teams tab.
const TODAY_TEAMS_MAX = 6;
let todayTeamsSeq = 0;
async function loadTodayTeams() {
  const seq = ++todayTeamsSeq;
  let t;
  try { t = await api("/api/teams/recent"); } catch (e) {
    if (seq === todayTeamsSeq) fill($("#today-teams-list"), [el("li", { class: "err" }, e.message)], "");
    return;
  }
  if (seq !== todayTeamsSeq) return;
  const recent = (Array.isArray(t.recent) ? t.recent : []).slice(0, TODAY_TEAMS_MAX);
  const unread = (t.recent || []).filter((r) => r.unread).length;
  $("#today-teams-count").textContent = unread;
  $("#today-teams-count").hidden = !unread;
  $("#today-teams-count").title = `${unread} unread`;
  const empty = !t.set_up ? "Not connected: log in on the Teams tab." : !t.read_at ? "Shows up after the next Teams check." : "No DMs or mentions in the last 3 days.";
  fill($("#today-teams-list"), recent.map((r) => teamsRecentRow(r, true)), empty);
  $("#today-teams-list").querySelectorAll("li.teams-row").forEach((li, i) => {
    const link = teamsLink(recent[i]?.link);
    if (link) li.addEventListener("click", (e) => { if (!e.target.closest("button, a, input")) openTeamsRecent(link); });
  });
}
$("#today-teams-all").addEventListener("click", () => openTab("teams"));

loaders.teams = () => guarded(async () => {
  const t = await api("/api/teams");
  teamsUrl = t.url;
  const s = t.state || {};
  const box = $("#teams-status");
  renderTeamsRecent(t);
  if (!t.set_up) {
    box.replaceChildren(el("div", { class: "panel-label" }, "TEAMS STATUS"),
      el("p", {}, "Not connected yet. Log in once and Command Center will watch your Teams unread DMs and mentions in the background."),
      el("div", { class: "row" }, el("button", { class: "primary", onclick: teamsLogin }, "Log in to Teams")));
  } else {
    // (replaceChildren would print a false as text, unlike el(): leave the gaps out)
    box.replaceChildren(...[el("div", { class: "panel-label" }, "TEAMS STATUS"),
      ...teamsCounts(s),
      el("div", { class: "muted" }, s.checked ? `Checked ${when(s.checked)}` : "First check pending"),
      s.ok === false && el("p", { class: "err" }, s.error),
      el("div", { class: "row" },
        el("button", { class: "small", onclick: (e) => { e.target.disabled = true; e.target.textContent = "Checking…"; guarded(() => api("/api/teams/check", {})).then(loaders.teams); } }, "Check now"),
        el("button", { class: "small alt", onclick: () => openExternal("msteams:") }, "Open Teams app"),
        el("button", { class: "small subtle", onclick: teamsLogin }, s.login_needed ? "Log in again" : "Re-login"))].filter(Boolean));
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
  add("Commands", "Show snoozed", "snoozes + no-reply reminders", snoozedDialog);
  add("Commands", "Urgent people", "who counts as urgent", urgentDialog);
  // "Go to <tab>" for every tab in both nav groups (module tabs included), each landing in its own pane.
  for (const b of document.querySelectorAll(".nav-group button[data-tab]")) {
    const label = `Go to ${b.dataset.label || b.textContent.trim()}`;
    if (!out.some((i) => i.label === label)) add("Commands", label, `${b.closest(".nav-group").dataset.pane === "work" ? "WORK" : "COMMS"} tab`, () => openTab(b.dataset.tab));
  }
  if (window.innerWidth >= SPLIT_MIN_WIDTH && !focusOn()) add("Commands", layout.split ? "Single pane" : "Split panes", "layout", () => $("#layout-toggle").click());
  if (focusReady()) add("Commands", layout.focus ? "Leave focus layout" : "Focus layout: Pulse + VS Code", "Pulse left, VS Code right", () => setFocusLayout(!layout.focus));
  for (const p of proj.list) add("Projects", `Work on: ${p.name}`, p.path, () => { setActiveProject(p.path); openTab("vscode"); });
  add("Commands", "New task", "Outlook or Asana", () => newTaskDialog());
  add("Commands", "Rescan follow-ups", "", () => { openTab("today"); loaders.today(true); });
  add("Commands", "Block time on calendar", "a busy appointment in Outlook", () => scheduleDialog(null));
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
    for (const m of agenda.filter((m) => !m.lunch && new Date(m.end) > now)) add("Agenda", `Prep: ${m.subject}`, timeOf(m.start), () => prepDialog(m.id, m));
    for (const t of todayData.tasks.items) add("Tasks", t.subject, dueText(t.due), () => t.url ? openExternal(t.url) : guarded(() => api("/api/outlook/open", { id: t.id })));
    const f = todayData.followups.error ? {} : todayData.followups;
    const seenFu = new Set();  // a message can be in two groups (e.g. needs a reply and flagged): list it once
    for (const m of [...(f.urgent_reply || []), ...(f.needs_reply || []), ...(f.teams_reply || []), ...(f.flagged || []), ...(f.unread_vip || [])]) {
      if (!m || seenFu.has(m.id)) continue;
      seenFu.add(m.id);
      add("Follow-ups", m.subject, m.sender_name, () => { openTab("mail"); showMail(m.id); });
    }
  }
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
    renderTeamsBadge(st.teams);
    renderWindowControls(st);
    if (unread !== lastUnread && $("#tab-alerts").classList.contains("active")) loaders.alerts();
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
// Restart and Close: a confirm, then a full-window overlay while the app goes (native views hide under it).
async function appExit(path, title, text, verb, doing) {
  if (!await confirmDialog(title, text, verb)) return;
  const overlay = el("div", { class: "restart-overlay", role: "status", "aria-live": "assertive" },
    el("div", { class: "restart-box" }, el("img", { class: "restart-mark", src: "/static/jc-logo.svg", alt: "" }), el("div", {}, doing)));
  document.body.append(overlay);
  try { await api(path, {}); } catch (e) { overlay.remove(); showMessage(verb, e.message); }
}
const restartApp = () => appExit("/api/app/restart", "Restart Command Center?",
  "The app closes and opens again. Docked VS Code windows step out and dock again (Claude chats keep running); SSMS goes back to a normal window.",
  "Restart", "Restarting…");
const quitApp = () => appExit("/api/app/quit", "Close Command Center?",
  "Command Center closes completely. Alerts, reminders and the Teams reader stop until you start it again. Docked VS Code and SSMS go back to normal windows (nothing is closed).",
  "Close", "Closing…");
$("#win-restart").addEventListener("click", restartApp);
// ⏻ (where ✕ was): close the app for real, after a confirm. Minimize, full screen and Restart have their own buttons.
$("#win-power").addEventListener("click", quitApp);
// Buttons in the header must never start a window drag: stop their mousedown before pywebview's body listener.
document.querySelector("header").addEventListener("mousedown", (e) => {
  if (e.target.closest("button, input, select, textarea, a, [tabindex]")) e.stopPropagation();
});
$("#win-min").addEventListener("click", () => guarded(() => api("/api/window/minimize", {})));
$("#win-full").addEventListener("click", toggleFullscreen);
document.addEventListener("keydown", (e) => {
  if (e.key !== "F11" || $("#win-controls").hidden) return;
  e.preventDefault();
  toggleFullscreen();
});

tickClock();
loadProjectList();
refresh().then(() => loaders.today());
setInterval(refresh, 5000);


// ---- plug-in API for feature modules (static/<module>.js) ----------------------
// A module adds its own tab and palette entries without editing this file:
//   CC.registerTab({ id: "toolkit", label: "Toolkit", render(section) {...}, onShow() {...} })
//   CC.addPaletteSource(() => [{ group: "Toolkit", label: "...", hint: "...", run() {...} }])
window.CC = {
  $, el, api, guarded, fill, when, timeOf, srcTag, showDialog, showMessage, confirmDialog, openTab, openExternal, loaders,
  replyDialog,  // verified reply: compose -> review (recipients, subject, text) -> Send
  importantStar,  // the ★ before an important sender
  toast,  // short note in the corner
  get todayData() { return todayData; },
  get workday() { return { ...WORKDAY }; },  // work hours {start, end} in hours (config offhours:)
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
  // Today's stats strip: fn(todayData) returns a tile (CC.statTile), a list of them, null, or a Promise of one.
  addStatTile(fn) { if (typeof fn === "function") { statSources.push(fn); refreshStats(); } },
  statTile,
  refreshStats,
  // Today's main panel: "agenda" | "tasks" | "follow".
  todayTab(name) { selectTodayTab(name, { focus: true }); },
  wideDialog(...content) { $("#dialog").classList.add("wide"); showDialog(...content); },
  showMail(id) { openTab("mail"); showMail(id); },
  registerAlertAction(type, fn) {  // the built-in types can't be replaced
    if (/^[a-z0-9-]{1,40}$/.test(String(type)) && !["today", "mail", "url", "meeting", "order"].includes(type) && typeof fn === "function") alertActionTypes[type] = fn;
  },
  addMeetingAction(fn) { if (typeof fn === "function") meetingActionSources.push(fn); },
  meetingActions,
  addMailAction(fn) { if (typeof fn === "function") mailActionSources.push(fn); },
  // Alerts changed on the server (e.g. removed by a spam block): redraw the badge and the feed.
  refreshAlerts() { refresh(); loaders.alerts(); },
  // A message that left the Inbox (e.g. marked as spam): drop it from the reader and the pinned row.
  forgetMail(id) {
    if (pinnedMail?.id === id) pinnedMail = null;
    if (selectedMail === id) closeReader();
  },
};
$("#dialog").addEventListener("close", () => $("#dialog").classList.remove("wide"));
document.dispatchEvent(new Event("cc:ready"));

// Modules register their tabs while their scripts load (right after this file); lay out once they're in.
function initLayout() {
  if (layout.ready) return;
  layout.ready = true;
  splitFrac = savedSplit();
  runLoaders(applyLayout().filter((id) => id !== "today"));  // Today loads with the first status refresh
}
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initLayout, { once: true });
else initLayout();
