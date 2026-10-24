// VS Code tab: the real VS Code (web) for a chosen project, with the Claude Code extension, in an iframe.
// The server runs on this PC only (127.0.0.1, random port, per-launch token) and starts when you ask.
// The token only ever comes from /api/vscode/url and goes straight into the iframe's src; nothing stores it.
// All DOM is built with CC.el (text only).
(() => {
  "use strict";

  function start() {
    const CC = window.CC;
    const { el, api, guarded } = CC;

    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = "/static/vscode.css";
    document.head.append(link);

    const BUSY = ["downloading", "installing", "starting"];
    const LABELS = { stopped: "Stopped", downloading: "Downloading", installing: "Installing", starting: "Starting", ready: "Running", error: "Error" };
    const state = { projects: [], status: { status: "stopped" }, folder: null, openFolder: null, timer: null, section: null,
      unsafeOk: new Set() };  // folders other accounts can modify that Joe confirmed opening (this page load only)
    // Same rule as the Claude tab: VS Code and the Claude Code extension run tasks, hooks, settings and MCP
    // servers from the folder, so a folder other Windows accounts can change needs an explicit confirmation,
    // sent as allow_unsafe (the server refuses without it).
    const UNSAFE_TEXT = "Other accounts on this PC can modify this folder — Claude hooks/settings there could run code as you. Open it anyway?";
    const ui = {};

    const store = {
      get(k) { try { return localStorage.getItem(k); } catch { return null; } },
      set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
    };

    function isVisible() { return state.section?.classList.contains("active"); }

    // ---- rendering ------------------------------------------------------------------

    function render(section) {
      state.section = section;
      ui.select = el("select", { class: "vsc-select", title: "Project to open", onchange: () => {
        state.folder = ui.select.value || null;
        if (state.folder) store.set("cc.vscode.folder", state.folder);
        updateButtons();
      } });
      ui.open = el("button", { class: "small primary", onclick: () => guarded(openSelected) }, "Open in VS Code");
      ui.power = el("button", { class: "small alt", onclick: () => guarded(togglePower) }, "Start server");
      ui.dot = el("span", { class: "dot" });
      ui.chip = el("span", { class: "chip vsc-state" }, ui.dot, "Stopped");
      ui.reload = el("button", { class: "small subtle", title: "Reload the VS Code page", onclick: () => reloadFrame() }, "Reload");
      ui.where = el("span", { class: "vsc-where muted" });
      ui.warn = el("span", { class: "vsc-unsafe", role: "alert", hidden: true, title: "Claude hooks/settings in this folder could run code as you" },
        "⚠ Other accounts on this PC can modify this folder");
      ui.stage = el("div", { class: "vsc-stage" });
      ui.frameWrap = el("div", { class: "vsc-frame-wrap" }, ui.stage);

      section.append(el("div", { class: "panel vsc-panel" },
        el("div", { class: "panel-head vsc-head" },
          el("span", { class: "panel-label" }, "VS.CODE // WEB"),
          ui.select, ui.open, ui.warn, ui.where,
          el("span", { class: "spacer" }),
          ui.chip, ui.reload, ui.power),
        ui.frameWrap));
      showStage();
    }

    const samePath = (a, b) => String(a || "").toLowerCase() === String(b || "").toLowerCase();
    const known = (path) => state.projects.find((p) => samePath(p.path, path));
    const isUnsafe = (path) => !!(known(path) && known(path).writable_by_others);

    // true when the folder may be opened (private, or confirmed once on this page), false if Joe cancelled.
    async function confirmUnsafe(folder) {
      const key = String(folder || "").toLowerCase();
      if (!isUnsafe(folder) || state.unsafeOk.has(key)) return true;
      if (!(await CC.confirmDialog("Folder isn't private", UNSAFE_TEXT, "Open anyway"))) return false;
      state.unsafeOk.add(key);
      return true;
    }

    function fillProjects() {
      // Default: the global "Working on:" project, then the last one opened here.
      const active = CC.activeProject?.path;
      const want = state.folder || (active && known(active) ? active : state.cleared ? null : store.get("cc.vscode.folder"));
      const opts = state.projects.map((p) => el("option", { value: p.path, title: p.path }, p.name));
      ui.select.replaceChildren(el("option", { value: "" }, state.projects.length ? "Choose a project…" : "No projects found"), ...opts);
      const hit = known(want);
      if (hit) { ui.select.value = hit.path; state.folder = hit.path; }
      updateButtons();
    }

    // Follow the global project selector. Never starts the server; if VS Code is already showing a
    // project, it switches to the new one.
    document.addEventListener("cc:project", (e) => {
      if (!e.detail || !e.detail.path) {  // WORKING ON cleared: clear the picker too (an open VS Code page stays)
        state.folder = null;
        state.cleared = true;  // and don't bring the last folder back when the list reloads
        if (ui.select) ui.select.value = "";
        updateButtons();
        return;
      }
      const hit = known(e.detail.path);
      if (!hit) return;
      state.cleared = false;
      state.folder = hit.path;
      if (ui.select) ui.select.value = hit.path;
      store.set("cc.vscode.folder", hit.path);
      updateButtons();
      if (ui.frame && state.status.status === "ready" && !samePath(state.openFolder, hit.path)) guarded(() => openFolder(hit.path));
    });

    function updateButtons() {
      const s = state.status.status;
      ui.power.textContent = s === "ready" || BUSY.includes(s) ? "Stop server" : "Start server";
      ui.power.className = s === "ready" || BUSY.includes(s) ? "small subtle" : "small alt";
      ui.open.disabled = !state.folder || BUSY.includes(s);
      ui.open.textContent = s === "ready" ? "Open in VS Code" : "Start + open";
      ui.reload.hidden = !ui.frame;
      ui.warn.hidden = !isUnsafe(state.folder);
      ui.chip.lastChild.textContent = LABELS[s] || s;
      ui.dot.className = "dot" + (s === "ready" ? " ok" : "");
      ui.chip.classList.toggle("bad", s === "error");
      ui.chip.classList.toggle("busy", BUSY.includes(s));
      const p = state.projects.find((x) => x.path === state.openFolder);
      ui.where.textContent = ui.frame && p ? `Showing ${p.name}` : "";
    }

    function showStage() {
      // The placeholder shown while there's no VS Code page: status, progress and recent server output.
      const st = state.status;
      const s = st.status;
      const lines = [];
      if (s === "stopped") {
        lines.push(el("h3", {}, "VS Code in Command Center"),
          el("p", {}, "Choose a project and press Start + open. The first start downloads the VS Code web server (about 200 MB, once) and installs the Claude Code extension into it."),
          el("p", { class: "muted" }, "First time in a folder, VS Code opens it in Restricted Mode: click Manage in its blue banner and choose Trust, or Claude Code and Git won't start. VS Code remembers that."),
          el("p", { class: "muted" }, "It runs on this PC only (127.0.0.1) with a new secret for every start, and stops when you press Stop or quit Command Center. Settings and extensions are kept apart from your desktop VS Code."));
      } else if (s === "error") {
        lines.push(el("h3", { class: "vsc-err" }, "VS Code web didn't start"), el("p", {}, st.error || st.detail || "Unknown error"));
      } else {
        lines.push(el("h3", {}, st.detail || LABELS[s] || s));
        if (typeof st.progress === "number") {
          const fillBar = el("div", { class: "vsc-fill" });
          fillBar.style.width = `${Math.max(2, st.progress)}%`;  // CSSOM, not a style attribute (CSP-safe)
          lines.push(el("div", { class: "xp-bar vsc-bar" }, fillBar),
            el("div", { class: "muted" }, `${st.progress}%`));
        }
        if (s === "ready") lines.push(el("p", { class: "muted" }, state.folder ? "Press Open in VS Code." : "Choose a project above."));
      }
      if (st.log?.length && s !== "stopped") lines.push(el("pre", { class: "vsc-log" }, st.log.slice(-12).join("\n")));
      ui.stage.replaceChildren(el("div", { class: "vsc-msg" }, ...lines));
      ui.stage.hidden = false;
    }

    function dropFrame() {
      if (ui.frame) { ui.frame.remove(); ui.frame = null; }
      state.openFolder = null;
      showStage();
      updateButtons();
    }

    function reloadFrame() {
      if (state.openFolder) guarded(() => openFolder(state.openFolder));
    }

    // ---- actions ----------------------------------------------------------------------

    async function refreshStatus() {
      const prev = state.status.status;
      state.status = await api("/api/vscode/status");
      const s = state.status.status;
      if (prev === "ready" && s !== "ready") dropFrame();
      if (!ui.frame) showStage();
      updateButtons();
      if (s === "ready" && state.pendingOpen) {
        const f = state.pendingOpen;
        state.pendingOpen = null;
        await openFolder(f);
      }
      schedule();
    }

    function schedule() {
      clearTimeout(state.timer);
      const s = state.status.status;
      const wait = BUSY.includes(s) ? 1200 : isVisible() ? 5000 : 0;
      if (wait) state.timer = setTimeout(() => refreshStatus().catch(() => schedule()), wait);
    }

    async function togglePower() {
      const s = state.status.status;
      if (s === "ready" || BUSY.includes(s)) {
        if (!(await CC.confirmDialog("Stop VS Code web?", "Unsaved changes in the VS Code page will be lost, and anything running in its terminals or in Claude Code will stop.", "Stop"))) return;
        state.pendingOpen = null;
        state.status = await api("/api/vscode/stop", {});
        dropFrame();
      } else {
        state.status = await api("/api/vscode/start", {});
        showStage();
      }
      updateButtons();
      schedule();
    }

    async function openSelected() {
      if (!state.folder) return;
      if (!(await confirmUnsafe(state.folder))) return;
      const s = state.status.status;
      if (s === "ready") return openFolder(state.folder);
      state.pendingOpen = state.folder;
      state.status = await api("/api/vscode/start", {});
      showStage();
      updateButtons();
      schedule();
    }

    async function openFolder(folder) {
      if (!(await confirmUnsafe(folder))) return;
      const allow = state.unsafeOk.has(String(folder).toLowerCase());
      let r;
      try {
        r = await api(`/api/vscode/url?folder=${encodeURIComponent(folder)}${allow ? "&allow_unsafe=true" : ""}`);
      } catch (e) {
        // The list was out of date: the server found the folder isn't private. Ask, then try once more.
        if (allow || !/Other accounts on this PC/.test(e.message)) throw e;
        const p = known(folder);
        if (p) p.writable_by_others = true;
        updateButtons();
        if (!(await CC.confirmDialog("Folder isn't private", UNSAFE_TEXT, "Open anyway"))) return;
        state.unsafeOk.add(String(folder).toLowerCase());
        r = await api(`/api/vscode/url?folder=${encodeURIComponent(folder)}&allow_unsafe=true`);
      }
      if (!ui.frame) {
        ui.frame = el("iframe", {
          class: "vsc-frame", title: "VS Code", referrerpolicy: "no-referrer",
          allow: "clipboard-read; clipboard-write; fullscreen",
        });
        ui.frameWrap.append(ui.frame);
      }
      ui.frame.src = r.url;
      ui.stage.hidden = true;
      state.openFolder = folder;
      updateButtons();
      ui.frame.focus();
    }

    async function loadProjects() {
      const r = await api("/api/projects");
      state.projects = (r.projects || []).filter((p) => p.path);
      fillProjects();
    }

    CC.registerTab({
      id: "vscode", label: "VS Code", before: "websites", render,
      onShow: () => guarded(async () => { await Promise.all([loadProjects(), refreshStatus()]); }),
    });

    CC.addPaletteSource(() => state.projects.map((p) => ({
      group: "VS Code", label: `VS Code: ${p.name}`, hint: p.path,
      run: () => {
        CC.openTab("vscode");
        state.folder = p.path;
        if (ui.select) ui.select.value = p.path;
        store.set("cc.vscode.folder", p.path);
        guarded(openSelected);
      },
    })));
    CC.addPaletteSource(() => {
      const s = state.status.status;
      return s === "ready" || BUSY.includes(s)
        ? [{ group: "Commands", label: "Stop VS Code web server", hint: "VS Code tab", run: () => { CC.openTab("vscode"); guarded(togglePower); } }]
        : [];
    });

    // Palette entries need the list even before the tab is opened.
    api("/api/projects").then((r) => { if (!state.projects.length) { state.projects = r.projects || []; fillProjects(); } }).catch(() => {});
    api("/api/vscode/status").then((st) => { state.status = st; if (!ui.frame) showStage(); updateButtons(); }).catch(() => {});
  }

  if (window.CC) start();
  else document.addEventListener("cc:ready", start, { once: true });
})();
