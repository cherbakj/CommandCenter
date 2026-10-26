// VS Code tab (WORK pane, labelled "VS Code · Claude"): the real VS Code (web) for the project picked in the global
// "Working on:" selector, with your desktop VS Code's extensions and settings (mirrored by the server before every
// start) and the Claude Code panel open on the right. It replaced the terminal Claude tab.
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
    const state = { projects: [], status: { status: "stopped" }, openFolder: null, timer: null, section: null,
      unsafeOk: new Set() };  // folders other accounts can modify that Joe confirmed opening (this page load only)
    // VS Code and the Claude Code extension run tasks, hooks, settings and MCP servers from the folder, so a folder
    // other Windows accounts can change needs an explicit confirmation, sent as allow_unsafe (the server refuses
    // without it). The same goes for Claude in a separate window.
    const UNSAFE_TEXT = "Other accounts on this PC can modify this folder — Claude hooks/settings there could run code as you. Open it anyway?";
    const ui = {};

    function isVisible() { return state.section?.classList.contains("active"); }

    const samePath = (a, b) => String(a || "").toLowerCase() === String(b || "").toLowerCase();
    const known = (path) => state.projects.find((p) => samePath(p.path, path));
    const isUnsafe = (path) => !!(known(path) && known(path).writable_by_others);
    // The project to open: the one in "Working on:" (only listed projects can be opened).
    const folder = () => { const ap = CC.activeProject; return ap && known(ap.path) ? known(ap.path).path : null; };

    // ---- rendering ------------------------------------------------------------------

    function render(section) {
      state.section = section;
      ui.open = el("button", { class: "small primary", onclick: () => guarded(openSelected) }, "Open in VS Code");
      ui.claude = el("button", { class: "small alt", title: "Show the Claude Code panel in VS Code (in VS Code: the spark icon, or Ctrl+Esc to focus its input)",
        onclick: () => guarded(showClaude) }, "✳ Claude panel");
      ui.window = el("button", { class: "small", title: "Open Claude for this project in a separate Windows Terminal window",
        onclick: () => guarded(launchWindow) }, "New window");
      ui.power = el("button", { class: "small alt", onclick: () => guarded(togglePower) }, "Start server");
      ui.sync = el("button", { class: "small subtle", title: "Copy your desktop VS Code's extensions, settings, keybindings and snippets in again (restarts VS Code web)",
        onclick: () => guarded(syncDesktop) }, "Sync from desktop");
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
          el("span", { class: "panel-label" }, "VS.CODE // CLAUDE"),
          ui.open, ui.claude, ui.window, ui.warn, ui.where,
          el("span", { class: "spacer" }),
          ui.chip, ui.sync, ui.reload, ui.power),
        ui.frameWrap));
      showStage();
    }

    // true when the folder may be opened (private, or confirmed once on this page), false if Joe cancelled.
    async function confirmUnsafe(path, button = "Open anyway") {
      const key = String(path || "").toLowerCase();
      if (!isUnsafe(path) || state.unsafeOk.has(key)) return true;
      if (!(await CC.confirmDialog("Folder isn't private", UNSAFE_TEXT, button))) return false;
      state.unsafeOk.add(key);
      return true;
    }

    // Follow the global project selector. Never starts the server; if VS Code is already showing a
    // project, it switches to the new one.
    document.addEventListener("cc:project", () => {
      updateButtons();
      const f = folder();
      if (f && ui.frame && state.status.status === "ready" && !samePath(state.openFolder, f)) guarded(() => openFolder(f));
    });

    function updateButtons() {
      const s = state.status.status;
      const f = folder();
      const p = known(f);
      ui.power.textContent = s === "ready" || BUSY.includes(s) ? "Stop server" : "Start server";
      ui.power.className = s === "ready" || BUSY.includes(s) ? "small subtle" : "small alt";
      ui.open.disabled = !f || BUSY.includes(s);
      ui.open.textContent = s !== "ready" ? "Start + open" : ui.frame && samePath(state.openFolder, f) ? "Reopen" : "Open in VS Code";
      ui.open.title = p ? p.path : "Pick a project in WORKING ON first";
      ui.claude.hidden = !ui.frame || s !== "ready";
      ui.window.disabled = !f;
      ui.sync.disabled = BUSY.includes(s);
      ui.reload.hidden = !ui.frame;
      ui.warn.hidden = !isUnsafe(f);
      ui.chip.lastChild.textContent = LABELS[s] || s;
      ui.dot.className = "dot" + (s === "ready" ? " ok" : "");
      ui.chip.classList.toggle("bad", s === "error");
      ui.chip.classList.toggle("busy", BUSY.includes(s));
      const shown = known(state.openFolder);  // only worth saying when it isn't the WORKING ON project
      ui.where.textContent = ui.frame && shown && !samePath(shown.path, f) ? `Showing ${shown.name}` : "";
    }

    // What the last desktop mirror did (counts and skipped extensions; the server never sends setting values).
    function mirrorNote(m) {
      if (!m) return [];
      const when = m.at ? new Date(m.at).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "";
      const out = [el("p", { class: "muted" }, `Synced from desktop VS Code${when ? ` ${when}` : ""}: ${m.extensions} extensions`
        + `${m.copied ? ` (${m.copied} copied in)` : ""}, ${m.settings} settings, ${m.keybindings} keybindings, ${m.snippets} snippet files.`)];
      for (const s of m.skipped || []) out.push(el("p", { class: "muted vsc-skip" }, `Not synced: ${s.id} — ${s.reason}`));
      if (m.keybinding_removals) out.push(el("p", { class: "muted vsc-skip" }, `${m.keybinding_removals} keybinding removals ("-command") can't be copied; change those in VS Code here.`));
      for (const e of m.errors || []) out.push(el("p", { class: "vsc-err" }, e));
      return out;
    }

    function showStage() {
      // The placeholder shown while there's no VS Code page: status, progress and recent server output.
      const st = state.status;
      const s = st.status;
      const lines = [];
      if (s === "stopped") {
        lines.push(el("h3", {}, "VS Code with Claude Code"),
          el("p", {}, "Pick a project in WORKING ON above and press Start + open. Claude Code opens in the panel on the right of VS Code (the spark icon; Ctrl+Esc focuses its input). New window runs Claude in Windows Terminal instead."),
          el("p", { class: "muted" }, "Before every start, your desktop VS Code's extensions, settings, keybindings and snippets are copied in. It's one way: desktop VS Code is never changed. Sync from desktop does it again now."),
          el("p", { class: "muted" }, "First time in a folder, VS Code opens it in Restricted Mode: click Manage in its banner and choose Trust, or Claude Code, Git and most extensions won't start. VS Code remembers that."),
          el("p", { class: "muted" }, "It runs on this PC only (127.0.0.1) with a new secret for every start, and stops when you press Stop or quit Command Center."));
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
        if (s === "ready") lines.push(el("p", { class: "muted" }, folder() ? "Press Open in VS Code." : "Pick a project in WORKING ON above."));
      }
      lines.push(...mirrorNote(st.mirror));
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

    async function syncDesktop() {
      const s = state.status.status;
      if (s === "ready" && !(await CC.confirmDialog("Sync from desktop VS Code?",
        "VS Code web restarts to pick up your desktop extensions and settings. Unsaved changes in the VS Code page will be lost, and anything running in its terminals or in Claude Code will stop.",
        "Sync and restart"))) return;
      state.pendingOpen = state.openFolder || (s === "ready" ? null : folder());  // reopen what was showing
      dropFrame();
      state.status = await api("/api/vscode/sync", {});
      showStage();
      updateButtons();
      schedule();
    }

    async function openSelected() {
      const f = folder();
      if (!f) return;
      if (!(await confirmUnsafe(f))) return;
      const s = state.status.status;
      if (s === "ready") return openFolder(f);
      state.pendingOpen = f;
      state.status = await api("/api/vscode/start", {});
      showStage();
      updateButtons();
      schedule();
    }

    async function openFolder(path) {
      if (!(await confirmUnsafe(path))) return;
      const allow = state.unsafeOk.has(String(path).toLowerCase());
      let r;
      try {
        r = await api(`/api/vscode/url?folder=${encodeURIComponent(path)}${allow ? "&allow_unsafe=true" : ""}`);
      } catch (e) {
        // The list was out of date: the server found the folder isn't private. Ask, then try once more.
        if (allow || !/Other accounts on this PC/.test(e.message)) throw e;
        const p = known(path);
        if (p) p.writable_by_others = true;
        updateButtons();
        if (!(await CC.confirmDialog("Folder isn't private", UNSAFE_TEXT, "Open anyway"))) return;
        state.unsafeOk.add(String(path).toLowerCase());
        r = await api(`/api/vscode/url?folder=${encodeURIComponent(path)}&allow_unsafe=true`);
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
      state.openFolder = path;
      updateButtons();
      ui.frame.focus();
    }

    // The Command Center helper extension inside VS Code shows the panel (a page can't reach into the iframe).
    async function showClaude() {
      await api("/api/vscode/claude", {});
      ui.frame?.focus();
    }

    async function launchWindow() {
      const f = folder();
      if (!f || !(await confirmUnsafe(f, "Start anyway"))) return;
      await api("/api/projects/launch", { path: f, resume: false, prompt: "", allow_unsafe: state.unsafeOk.has(f.toLowerCase()) });
    }

    function newProjectDialog() {
      const input = el("input", { placeholder: "Project name (letters, numbers, spaces, - _ .)", maxlength: "61", autocomplete: "off", spellcheck: "false" });
      const create = () => guarded(async () => {
        const p = await api("/api/projects/new", { name: input.value });
        document.querySelector("#dialog").close();
        await loadProjects();
        document.querySelector("#project-refresh")?.click();  // the WORKING ON list
        CC.setActiveProject(p.path);
        CC.openTab("vscode");
        CC.showMessage("Project created", `${p.path} is ready (empty git repo on branch main). Press Start + open.`);
      });
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); create(); } });
      CC.showDialog(el("h3", {}, "New project"),
        el("p", { class: "muted" }, "Creates a folder in your first projects root and runs git init."),
        input,
        el("div", { class: "row" }, el("button", { value: "cancel", class: "subtle" }, "Cancel"),
          el("button", { type: "button", class: "primary", onclick: create }, "Create")));
      setTimeout(() => input.focus(), 30);
    }

    async function loadProjects() {
      const r = await api("/api/projects");
      state.projects = (r.projects || []).filter((p) => p.path);
      updateButtons();
    }

    CC.registerTab({
      id: "vscode", label: "VS Code · Claude", before: "websites", render,
      onShow: () => guarded(async () => { await Promise.all([loadProjects(), refreshStatus()]); }),
    });

    CC.addPaletteSource(() => state.projects.flatMap((p) => [
      { group: "Projects", label: `VS Code + Claude: ${p.name}`, hint: p.branch ? `${p.branch} · ${p.path}` : p.path,
        run: () => { if (CC.codeDock?.on()) return CC.codeDock.open(p.path); CC.setActiveProject(p.path); CC.openTab("vscode"); guarded(openSelected); } },
      { group: "Projects", label: `Claude (new window): ${p.name}`, hint: "Windows Terminal",
        run: () => { CC.setActiveProject(p.path); guarded(launchWindow); } },
    ]));
    CC.addPaletteSource(() => {
      const s = state.status.status;
      if (CC.codeDock?.on()) return [{ group: "Commands", label: "New project…", hint: "git init in your projects folder", run: newProjectDialog }];
      return [
        { group: "Commands", label: "New project…", hint: "git init in your projects folder", run: newProjectDialog },
        { group: "Commands", label: "Sync VS Code from desktop VS Code", hint: "extensions, settings, keybindings, snippets", run: () => { CC.openTab("vscode"); guarded(syncDesktop); } },
        ...(s === "ready" || BUSY.includes(s)
          ? [{ group: "Commands", label: "Stop VS Code web server", hint: "VS Code tab", run: () => { CC.openTab("vscode"); guarded(togglePower); } }]
          : []),
      ];
    });

    // Palette entries need the list even before the tab is opened.
    api("/api/projects").then((r) => { if (!state.projects.length) { state.projects = (r.projects || []).filter((p) => p.path); updateButtons(); } }).catch(() => {});
    api("/api/vscode/status").then((st) => { state.status = st; if (!ui.frame) showStage(); updateButtons(); }).catch(() => {});
  }

  if (window.CC) start();
  else document.addEventListener("cc:ready", start, { once: true });
})();
