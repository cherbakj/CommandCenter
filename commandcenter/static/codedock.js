// VS Code tab in the desktop app: the real desktop VS Code docked into the tab (codedock.py), placed and hidden through
// nativestage.js like the SQL tab's SSMS. One window per project: the "Working on" project's window shows, others stay
// docked (and running) behind it. A Command Center restart keeps them: they're handed back as windows and docked again.
// In a browser (serve mode) there's nothing to dock, and the tab keeps the web VS Code (vscode.js).
// Window titles come from VS Code (file and folder names), so they're shown as text only (CC.el).
(function () {
  "use strict";

  function start() {
    const CC = window.CC;
    const { el, api, guarded } = CC;
    const section = document.getElementById("tab-vscode");
    if (!section) return;

    const css = document.createElement("link");
    css.rel = "stylesheet";
    css.href = "/static/codedock.css";
    document.head.append(css);

    const UNSAFE_TEXT = "Other accounts on this PC can modify this folder — Claude hooks/settings there could run code as you. Open it anyway?";
    const st = { available: null, s: {}, projects: [], busy: false, unsafeOk: new Set() };
    let poll, nativeStage, built = false;
    const bar = el("div", { class: "cd-bar" });
    const msg = el("div", { class: "cd-msg" });
    const hole = el("div", { class: "cd-hole" }, msg);
    const panel = el("div", { class: "panel cd-panel", hidden: true }, bar, el("div", { class: "cd-stage" }, hole));

    const samePath = (a, b) => String(a || "").toLowerCase() === String(b || "").toLowerCase();
    const known = (path) => st.projects.find((p) => samePath(p.path, path));
    const project = () => { const ap = CC.activeProject; return ap && known(ap.path) ? known(ap.path) : null; };
    const docked = () => st.s.docked || [];
    const active = () => docked().find((d) => d.active);
    const nameOf = (d) => d.name || (d.title || "VS Code").split(" - ").slice(-2, -1)[0] || "VS Code";
    const dockedFor = (p) => p && docked().find((d) => samePath(d.folder, p.path) || (d.title || "").split(" - ").some((t) => t.replace(/^●\s*/, "").trim().toLowerCase() === p.name.toLowerCase()));

    async function loadProjects() {
      try { st.projects = ((await api("/api/projects")).projects || []).filter((p) => p.path); } catch { /* keep the old list */ }
    }

    async function load() {
      try {
        st.s = await api("/api/window/code");
        st.available = !!st.s.available;
      } catch { st.s = {}; st.available = false; }
      if (st.available && !built) build();
      if (built) render();
      nativeStage?.schedule();
    }

    // In the desktop app the dock replaces the web VS Code panel in this tab.
    function build() {
      built = true;
      section.classList.add("cd-on");
      section.append(panel);
      panel.hidden = false;
      nativeStage = CC.nativeStage?.({ view: "code", section, hole, available: () => !!st.available });
    }

    async function confirmUnsafe(p, button = "Open anyway") {
      if (!p?.writable_by_others || st.unsafeOk.has(p.path.toLowerCase())) return true;
      if (!(await CC.confirmDialog("Folder isn't private", UNSAFE_TEXT, button))) return false;
      st.unsafeOk.add(p.path.toLowerCase());
      return true;
    }

    async function act(action, extra = {}) {
      st.busy = true;
      render();
      try { await api("/api/window/code/action", { action, ...extra }); }
      finally { st.busy = false; setTimeout(load, 250); setTimeout(load, 1200); }
    }

    async function open(p = project()) {
      if (!p || !(await confirmUnsafe(p))) return;
      try {
        await act("open", { folder: p.path, allow_unsafe: st.unsafeOk.has(p.path.toLowerCase()) });
      } catch (e) {
        if (!/Other accounts on this PC/.test(e.message)) throw e;
        p.writable_by_others = true;  // the list was out of date: ask, then try once more
        if (!(await confirmUnsafe(p))) return;
        await act("open", { folder: p.path, allow_unsafe: true });
      }
    }

    async function launchClaudeWindow() {
      const p = project();
      if (!p || !(await confirmUnsafe(p, "Start anyway"))) return;
      await api("/api/projects/launch", { path: p.path, resume: false, prompt: "", allow_unsafe: st.unsafeOk.has(p.path.toLowerCase()) });
    }

    function dockRunning() {
      const list = st.s.running || [];
      if (list.length === 1) return guarded(() => act("dock", { hwnd: list[0].hwnd }));
      CC.showDialog(el("h3", {}, "Dock a VS Code window"),
        el("p", { class: "muted" }, "VS Code windows open as you. Docking moves the window into Command Center; Undock gives it back."),
        el("ul", { class: "list" }, list.map((w) => el("li", {},
          el("div", { class: "grow" }, el("div", { class: "title" }, w.title || "Visual Studio Code")),
          el("div", { class: "actions" }, el("button", { type: "button", class: "small primary",
            onclick: () => { document.querySelector("#dialog").close(); guarded(() => act("dock", { hwnd: w.hwnd })); } }, "Dock"))))),
        el("div", { class: "row" }, el("button", {}, "Close")));
    }

    function render() {
      const s = st.s || {};
      const p = project();
      const mine = dockedFor(p);
      const cur = active();
      const tabs = docked().map((d) => el("button", {
        type: "button", class: `small ${d.active ? "primary" : "subtle"} cd-win`, title: d.title || "", "aria-pressed": String(!!d.active),
        onclick: () => guarded(() => act("show", { hwnd: d.hwnd })),
      }, nameOf(d)));
      bar.replaceChildren(...[
        el("span", { class: "panel-label" }, "VS.CODE // CLAUDE"),
        ...tabs,
        s.starting ? el("span", { class: "cd-note" }, `Opening ${s.starting}…`) : null,
        s.restoring ? el("span", { class: "cd-note" }, "Docking your VS Code windows again…") : null,
        s.hung ? el("span", { class: "cd-note err" }, "VS Code isn't responding") : null,
        s.note ? el("span", { class: "cd-note" }, s.note) : null,
        el("span", { class: "spacer" }),
        p && !(mine && mine.active) ? el("button", { type: "button", class: "small primary", disabled: st.busy || !!s.starting || !s.installed,
          title: p.path, onclick: () => guarded(() => open(p)) }, mine ? `Show ${p.name}` : `Open ${p.name}`) : null,
        (s.running || []).length ? el("button", { type: "button", class: "small subtle", onclick: dockRunning },
          s.running.length > 1 ? `Dock running VS Code (${s.running.length})` : "Dock running VS Code") : null,
        el("button", { type: "button", class: "small", disabled: !p, title: "Open Claude for this project in a separate Windows Terminal window",
          onclick: () => guarded(launchClaudeWindow) }, "New window"),
        cur ? el("button", { type: "button", class: "small subtle", title: "Give this VS Code window back (nothing is closed)",
          onclick: () => guarded(() => act("undock", { hwnd: cur.hwnd })) }, "Undock") : null,
      ].filter(Boolean));
      renderStage(p);
    }

    // What shows while no VS Code window is docked here (a docked one covers it).
    function renderStage(p) {
      const s = st.s || {};
      let body;
      if (!s.installed) body = [el("p", { class: "err" }, "Desktop VS Code isn't installed."), el("p", { class: "muted" }, "Install it from code.visualstudio.com (user install), then come back.")];
      else if (active()) body = [el("p", { class: "muted" }, "VS Code is docked here.")];
      else body = [
        el("p", {}, p ? `Open ${p.name} in your desktop VS Code, docked here.` : "Pick a project in WORKING ON above, then open it here."),
        el("p", { class: "muted" }, "It's your real VS Code (your extensions, settings and Claude Code). Restarting Command Center keeps it running: the window steps out during the restart and comes back after."),
        el("p", { class: "muted" }, "While VS Code has the keyboard, Ctrl+K, Ctrl+Shift+P and F11 are VS Code's. Click Command Center's header first for its own shortcuts."),
        el("div", { class: "row" },
          p ? el("button", { type: "button", class: "primary", disabled: st.busy || !!s.starting, onclick: () => guarded(() => open(p)) }, s.starting ? "Opening…" : `Open ${p.name}`) : null,
          (s.running || []).length ? el("button", { type: "button", class: "alt", onclick: dockRunning }, "Dock running VS Code") : null)];
      msg.replaceChildren(el("div", { class: "panel cd-card" }, el("div", { class: "panel-label" }, "DESKTOP VS CODE"), ...body.filter(Boolean)));
    }

    // Follow WORKING ON: show that project's docked window; if VS Code is showing here, open the new project's too.
    document.addEventListener("cc:project", () => {
      if (!built) return;
      const p = project();
      const mine = dockedFor(p);
      if (mine && !mine.active) guarded(() => act("show", { hwnd: mine.hwnd }));
      else if (!mine && p && active() && section.classList.contains("active")) guarded(() => open(p));
      else render();
    });

    function onShow() {
      loadProjects().then(load);
      clearInterval(poll);
      poll = setInterval(() => {  // docked / closed / not responding, only while the tab is on screen
        if (!section.classList.contains("active")) { clearInterval(poll); return; }
        load();
      }, 2000);
    }
    const coreLoader = CC.loaders.vscode;
    CC.loaders.vscode = () => (st.available === null ? load() : Promise.resolve()).then(() => (st.available ? onShow() : coreLoader?.()));

    // vscode.js asks this before using the web VS Code (palette entries).
    CC.codeDock = {
      on: () => !!st.available,
      open: (path) => { CC.setActiveProject(path); CC.openTab("vscode"); loadProjects().then(() => guarded(() => open(known(path)))); },
    };
    CC.addPaletteSource(() => st.available ? docked().map((d) => ({ group: "VS Code", label: `VS Code: ${nameOf(d)}`, hint: "docked window",
      run: () => { CC.openTab("vscode"); guarded(() => act("show", { hwnd: d.hwnd })); } })) : []);

    loadProjects();
    load();  // also lets the dock re-attach windows after a restart as soon as the page reports
  }

  if (window.CC) start();
  else document.addEventListener("cc:ready", start, { once: true });
})();
