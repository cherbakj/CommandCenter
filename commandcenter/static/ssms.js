// SQL tab (WORK): SQL Server Management Studio docked into the window (ssmsdock.py), placed and hidden through
// nativestage.js like the Pulse site. The tab is the SSMS dock only: the old "CC queries" view (saved queries,
// connections, table browser) was removed (Joe's call 2026-10-28).
// Window titles come from SSMS (file names, server, database), so they're shown as text only (CC.el).
(function () {
  "use strict";
  if (!window.CC) return;
  const { el, api, guarded, showDialog } = CC;

  const css = document.createElement("link");
  css.rel = "stylesheet";
  css.href = "/static/ssms.css";
  document.head.append(css);

  const section = document.getElementById("tab-sql");
  if (!section) return;
  const st = { available: null, s: {}, busy: false };
  let poll, nativeStage;
  try { localStorage.removeItem("cc.sql.view"); } catch { /* the old SSMS | CC queries choice; storage blocked */ }

  const bar = el("div", { class: "ss-bar" });
  const msg = el("div", { class: "ss-msg" });
  const hole = el("div", { class: "ss-hole" }, msg);
  const stage = el("div", { class: "ss-stage" }, hole);
  section.classList.add("fill");
  section.replaceChildren(bar, stage);

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
    const parts = [el("span", { class: "panel-label ss-label" }, "SSMS")];
    if (st.available) {
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
    else if (!st.available) body = [el("p", {}, "SQL Server Management Studio docks here in the Command Center desktop app.")];
    else if (!s.installed) body = [el("p", { class: "err" }, `SSMS isn't installed at ${s.path}.`),
      el("p", { class: "muted" }, "Set sql.ssms_path in config.yaml if it's somewhere else.")];
    else if (s.docked) body = [el("p", { class: "muted" }, "SSMS is docked here.")];
    else body = [
      el("p", {}, "Start SSMS here, docked into Command Center", s.server ? ` (connecting to ${s.server}${s.user ? " as " + s.user : ""}; it asks for the password)` : "", "."),
      el("p", { class: "muted" }, "First connection to this server: Encryption Mandatory and “Trust server certificate” in SSMS's connect dialog (Options), because SQL Server only has a self-signed certificate."),
      el("div", { class: "row" },
        el("button", { type: "button", class: "primary", disabled: s.starting || st.busy, onclick: () => act("start") }, s.starting ? "Starting…" : "Start SSMS"),
        (s.running || []).some((p) => !p.docked && p.has_window) ? el("button", { type: "button", class: "alt", onclick: dockRunning }, "Dock running SSMS") : null)];
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

  CC.loaders.sql = onShow;
  nativeStage = CC.nativeStage?.({ view: "ssms", section, hole, available: () => !!st.available });
  CC.addPaletteSource(() => [
    { group: "Commands", label: "SSMS: start / show docked", hint: "SQL tab", run: () => { CC.openTab("sql"); if (!st.s.docked) act("start"); } },
  ]);
  render();
  load();
})();
