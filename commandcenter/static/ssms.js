// SQL tab (WORK): SQL Server Management Studio docked into the window (ssmsdock.py), placed and hidden through
// nativestage.js like the Pulse site. "CC queries" switches the same tab to Command Center's own saved queries,
// connections and table browser (the original SQL tab, still used by the order lookup and the palette).
// Window titles come from SSMS (file names, server, database), so they're shown as text only (CC.el).
(function () {
  "use strict";
  if (!window.CC) return;
  const { el, api, guarded, showDialog } = CC;

  const css = document.createElement("link");
  css.rel = "stylesheet";
  css.href = "/static/ssms.css";
  document.head.append(css);

  const VIEW_KEY = "cc.sql.view";
  const section = document.getElementById("tab-sql");
  const grid = section?.querySelector(".sql-grid");
  if (!section || !grid) return;
  const st = { available: null, s: {}, view: savedView(), busy: false };
  let poll, nativeStage;

  function savedView() { try { return localStorage.getItem(VIEW_KEY) === "queries" ? "queries" : "ssms"; } catch { return "ssms"; } }

  const bar = el("div", { class: "ss-bar" });
  const msg = el("div", { class: "ss-msg" });
  const hole = el("div", { class: "ss-hole" }, msg);
  const stage = el("div", { class: "ss-stage" }, hole);
  section.prepend(bar);
  grid.before(stage);

  // view: "ssms" (the docked SSMS) or "queries" (Command Center's own); remembered per viewer
  function setView(view, save = true) {
    st.view = view === "queries" ? "queries" : "ssms";
    if (save) { try { localStorage.setItem(VIEW_KEY, st.view); } catch { /* storage blocked */ } }
    const ssms = st.view === "ssms";
    section.classList.toggle("fill", ssms);
    stage.hidden = !ssms;
    grid.hidden = ssms;
    render();
    nativeStage?.schedule();
    if (!ssms && section.classList.contains("active")) coreLoader?.();
  }

  async function load() {
    try {
      st.s = await api("/api/window/ssms");
      st.available = !!st.s.available;
    } catch { st.s = {}; st.available = false; }
    render();
    nativeStage?.schedule();
  }

  async function act(action, extra = {}) {
    st.busy = true;
    render();
    await guarded(async () => { await api("/api/window/ssms/action", { action, ...extra }); });
    st.busy = false;
    setTimeout(load, 300);
    setTimeout(load, 1500);
  }

  function dockRunning() {
    const others = (st.s.running || []).filter((p) => !p.docked && p.has_window);
    if (others.length === 1) return act("dock", { pid: others[0].pid });
    showDialog(el("h3", {}, "Dock a running SSMS"),
      el("p", { class: "muted" }, "SSMS windows open as you. Docking moves the window into Command Center; Undock gives it back."),
      el("ul", { class: "list" }, others.map((p) => el("li", {},
        el("div", { class: "grow" }, el("div", { class: "title" }, p.title || "SQL Server Management Studio"), el("div", { class: "muted" }, `pid ${p.pid}`)),
        el("div", { class: "actions" }, el("button", { type: "button", class: "small primary", onclick: () => { document.querySelector("#dialog").close(); act("dock", { pid: p.pid }); } }, "Dock"))))),
      el("div", { class: "row" }, el("button", {}, "Close")));
  }

  function render() {
    const s = st.s || {};
    const seg = el("div", { class: "ss-seg", role: "group", "aria-label": "SQL view" },
      [["ssms", "SSMS"], ["queries", "CC queries"]].map(([v, label]) => el("button", {
        type: "button", class: `small ${st.view === v ? "primary" : "subtle"}`, "aria-pressed": String(st.view === v),
        onclick: () => setView(v),
      }, label)));
    const parts = [seg];
    if (st.view === "ssms" && st.available) {
      const others = (s.running || []).filter((p) => !p.docked && p.has_window);
      if (s.docked) {
        parts.push(el("span", { class: "ss-title", title: s.docked.title }, s.docked.title || "SQL Server Management Studio"),
          s.hung ? el("span", { class: "ss-note err" }, "SSMS isn't responding") : null,
          el("span", { class: "spacer" }),
          el("button", { type: "button", class: "small subtle", title: "Give SSMS back its own window (nothing is closed)", onclick: () => act("undock") }, "Undock"));
      } else {
        parts.push(s.starting ? el("span", { class: "ss-note" }, "Starting SSMS…") : null,
          s.note ? el("span", { class: "ss-note" }, s.note) : null,
          el("span", { class: "spacer" }),
          s.can_redock ? el("button", { type: "button", class: "small alt", onclick: () => act("redock") }, "Re-dock") : null,
          others.length ? el("button", { type: "button", class: "small subtle", onclick: dockRunning }, others.length > 1 ? `Dock running SSMS (${others.length})` : "Dock running SSMS") : null,
          el("button", { type: "button", class: "small primary", disabled: !s.installed || s.starting || st.busy, onclick: () => act("start") }, "Start SSMS"));
      }
    } else {
      parts.push(el("span", { class: "spacer" }));
    }
    bar.replaceChildren(...parts.filter(Boolean));
    renderStage();
  }

  // What shows on the stage while no SSMS is docked (a docked one covers it).
  function renderStage() {
    const s = st.s || {};
    let body;
    if (st.available === null) body = [el("p", { class: "muted" }, "Checking for SSMS…")];
    else if (!st.available) body = [el("p", {}, "SQL Server Management Studio docks here in the Command Center desktop app."),
      el("div", { class: "row" }, el("button", { type: "button", class: "primary", onclick: () => setView("queries") }, "CC queries"))];
    else if (!s.installed) body = [el("p", { class: "err" }, `SSMS isn't installed at ${s.path}.`),
      el("p", { class: "muted" }, "Set sql.ssms_path in config.yaml if it's somewhere else.")];
    else if (s.docked) body = [el("p", { class: "muted" }, "SSMS is docked here.")];
    else body = [
      el("p", {}, "Start SSMS here, docked into Command Center", s.server ? ` (connecting to ${s.server}${s.user ? " as " + s.user : ""}; it asks for the password)` : "", "."),
      el("p", { class: "muted" }, "First connection to this server: Encryption Mandatory and “Trust server certificate” in SSMS's connect dialog (Options), because SQL Server only has a self-signed certificate."),
      el("div", { class: "row" },
        el("button", { type: "button", class: "primary", disabled: s.starting || st.busy, onclick: () => act("start") }, s.starting ? "Starting…" : "Start SSMS"),
        (s.running || []).some((p) => !p.docked && p.has_window) ? el("button", { type: "button", class: "alt", onclick: dockRunning }, "Dock running SSMS") : null,
        el("button", { type: "button", class: "subtle", onclick: () => setView("queries") }, "CC queries"))];
    msg.replaceChildren(el("div", { class: "panel ss-card" }, el("div", { class: "panel-label" }, "SQL SERVER MANAGEMENT STUDIO"), ...body.filter(Boolean)));
  }

  function onShow() {
    load();
    clearInterval(poll);
    poll = setInterval(() => {  // docked / closed / not responding, only while the tab is on screen
      if (!section.classList.contains("active")) { clearInterval(poll); return; }
      load();
    }, 2000);
  }

  // The core SQL tab loader (CC queries) still runs; this adds the SSMS state.
  const coreLoader = CC.loaders.sql;
  CC.loaders.sql = () => { onShow(); if (st.view === "queries") coreLoader?.(); };
  // CC.sqlShow("queries"): what the palette's "Run query" and the order lookup's "Go to SQL tab" need.
  CC.sqlShow = (view) => setView(view);
  nativeStage = CC.nativeStage?.({ view: "ssms", section, hole, available: () => !!st.available, shown: () => st.view === "ssms" });
  CC.addPaletteSource(() => [
    { group: "Commands", label: "SSMS: start / show docked", hint: "SQL tab", run: () => { setView("ssms"); CC.openTab("sql"); if (!st.s.docked) act("start"); } },
    { group: "Commands", label: "SQL: CC saved queries", hint: "SQL tab", run: () => { setView("queries"); CC.openTab("sql"); } },
  ]);
  setView(st.view, false);
  load();
})();
