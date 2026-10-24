// Claude tab (WORK pane): the real Claude Code CLI in an embedded terminal, for the project picked in the
// global "Working on:" selector (CC.activeProject / the "cc:project" event). If that project already has a
// session it is shown (attached); otherwise the panel offers "Start Claude in <project>" and waits for a click.
// The terminal is xterm.js (vendored) talking to /ws/claude, which runs claude.exe in a Windows ConPTY.
// Sessions live on the server: switching tabs, reloading or closing the window doesn't stop Claude.
// All DOM is built with CC.el (text only). Terminal output is rendered by xterm, never as HTML.
(() => {
  "use strict";

  function start() {
    const CC = window.CC;
    const { el, api, guarded } = CC;

    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = "/static/projects.css";
    document.head.append(link);

    const state = {
      projects: [], claude: null, roots: [], loaded: false,
      sessions: new Map(),     // path -> { term, fit, ws, host, name, path, alive, ... }
      active: null,            // path of the session shown in the terminal panel
      serverSessions: [],
      checked: false,          // first /api/terminal/sessions answer is in
      listOpen: false,         // project list (secondary now that the global selector drives the panel)
    };
    const ui = {};

    // ---- helpers ----------------------------------------------------------------

    const store = {
      get(k) { try { return localStorage.getItem(k); } catch { return null; } },
      set(k, v) { try { v === null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* private mode */ } },
    };

    function ago(iso) {
      if (!iso) return "";
      const s = (Date.now() - new Date(iso).getTime()) / 1000;
      if (!isFinite(s)) return "";
      if (s < 60) return "just now";
      if (s < 3600) return `${Math.floor(s / 60)}m ago`;
      if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
      if (s < 86400 * 30) return `${Math.floor(s / 86400)}d ago`;
      return new Date(iso).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
    }

    const samePath = (a, b) => (a || "").toLowerCase() === (b || "").toLowerCase();
    const findSession = (path) => { for (const [k, s] of state.sessions) if (samePath(k, path)) return s; return null; };
    const projectFor = (path) => state.projects.find((p) => samePath(p.path, path));

    // ---- vendored xterm, loaded on first use ------------------------------------------

    let xtermReady = null;
    function loadXterm() {
      if (xtermReady) return xtermReady;
      const script = (src) => new Promise((resolve, reject) => {
        const s = document.createElement("script");
        s.src = src; s.onload = resolve; s.onerror = () => reject(new Error(`Couldn't load ${src}`));
        document.head.append(s);
      });
      const css = document.createElement("link");
      css.rel = "stylesheet"; css.href = "/static/vendor/xterm.css";
      document.head.insertBefore(css, link);  // ours after, so our overrides win
      const fonts = document.fonts ? Promise.race([
        Promise.all([document.fonts.load("15px 'JetBrains Mono'"), document.fonts.load("600 15px 'JetBrains Mono'")]),
        new Promise((r) => setTimeout(r, 2500)),
      ]).catch(() => {}) : Promise.resolve();
      xtermReady = script("/static/vendor/xterm.js")
        .then(() => Promise.all([script("/static/vendor/addon-fit.js"), script("/static/vendor/addon-web-links.js"), fonts]))
        .catch((e) => { xtermReady = null; throw e; });
      return xtermReady;
    }

    const THEME = {
      background: "#04070a", foreground: "#e8f5ec", cursor: "#39ff14", cursorAccent: "#04070a",
      selectionBackground: "rgba(57, 255, 20, 0.32)", selectionInactiveBackground: "rgba(57, 255, 20, 0.18)",
      black: "#0d1417", red: "#ff5c7a", green: "#39ff14", yellow: "#ffd24a",
      blue: "#5ab0ff", magenta: "#ff6ad5", cyan: "#00e5ff", white: "#cfe3d6",
      brightBlack: "#6b8a7b", brightRed: "#ff8a9e", brightGreen: "#8dff73", brightYellow: "#ffe48a",
      brightBlue: "#8fcbff", brightMagenta: "#ff9fe6", brightCyan: "#80f2ff", brightWhite: "#ffffff",
    };

    function termFontSize() {
      const root = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
      return Math.max(12, Math.min(18, Math.round(root * 0.88)));
    }

    // ---- terminal sessions -------------------------------------------------------------

    function send(s, msg) {
      if (s.ws && s.ws.readyState === WebSocket.OPEN) s.ws.send(JSON.stringify(msg));
    }
    const sendInput = (s, data) => { if (!s.suppress && s.alive) send(s, { type: "input", data }); };

    async function copySelection(s) {
      const text = s.term.getSelection();
      if (!text) return;
      try { await navigator.clipboard.writeText(text); } catch { document.execCommand("copy"); }
      s.term.clearSelection();
    }
    async function pasteClipboard(s) {
      try {
        const text = await navigator.clipboard.readText();
        if (text) s.term.paste(text);
      } catch { CC.showMessage("Paste", "The clipboard couldn't be read. Use Ctrl+V instead."); }
    }

    function createTerminal(path, name) {
      const host = el("div", { class: "pj-xterm", hidden: true });
      ui.termWrap.append(host);
      const term = new window.Terminal({
        theme: THEME, fontFamily: "'JetBrains Mono', Consolas, monospace", fontSize: termFontSize(),
        lineHeight: 1.15, cursorBlink: true, cursorStyle: "bar", scrollback: 8000, allowProposedApi: false,
        macOptionIsMeta: false, rightClickSelectsWord: false, drawBoldTextInBrightColors: false, minimumContrastRatio: 1,
      });
      const fit = new window.FitAddon.FitAddon();
      term.loadAddon(fit);
      // Links open only on Ctrl+click and only web links (through the server's /api/open, which allows http/https).
      term.loadAddon(new window.WebLinksAddon.WebLinksAddon((event, uri) => {
        if (!(event.ctrlKey || event.metaKey)) return;
        try { const u = new URL(uri); if (u.protocol === "https:" || u.protocol === "http:") CC.openExternal(u.href); } catch { /* not a URL */ }
      }));
      term.open(host);
      const s = { term, fit, host, path, name, ws: null, alive: false, ended: false, suppress: false, retry: 0, timer: null, started: false };

      term.attachCustomKeyEventHandler((e) => {
        if (e.type !== "keydown") return true;
        const k = e.key.toLowerCase();
        if (e.ctrlKey && e.shiftKey && !e.altKey && k === "c") { e.preventDefault(); copySelection(s); return false; }
        if (e.ctrlKey && e.shiftKey && !e.altKey && k === "v") { e.preventDefault(); pasteClipboard(s); return false; }
        // Ctrl+C copies when text is selected (otherwise it interrupts Claude, as usual).
        if (e.ctrlKey && !e.shiftKey && !e.altKey && k === "c" && term.hasSelection()) { e.preventDefault(); copySelection(s); return false; }
        // Ctrl+V: let the browser fire a normal paste event, which xterm turns into a bracketed paste.
        if (e.ctrlKey && !e.shiftKey && !e.altKey && k === "v") return false;
        return true;
      });
      host.addEventListener("contextmenu", (e) => {  // right-click: copy the selection, or paste (like Windows Terminal)
        e.preventDefault();
        term.hasSelection() ? copySelection(s) : pasteClipboard(s);
      });
      term.onData((d) => sendInput(s, d));
      term.onBinary((d) => sendInput(s, d));
      term.onResize(({ cols, rows }) => send(s, { type: "resize", cols, rows }));
      return s;
    }

    function connect(s, { resume = false, attach = false } = {}) {
      clearTimeout(s.timer);
      const dims = s.term.cols && s.term.rows ? { cols: s.term.cols, rows: s.term.rows } : { cols: 120, rows: 30 };
      const q = new URLSearchParams({ path: s.path, resume: resume ? "1" : "0", attach: attach ? "1" : "0", cols: dims.cols, rows: dims.rows });
      const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/claude?${q}`);
      ws.binaryType = "arraybuffer";
      s.ws = ws;
      ws.onmessage = (e) => {
        if (ws !== s.ws) return;
        if (typeof e.data === "string") { s.term.write(e.data); return; }
        let msg;
        try { msg = JSON.parse(new TextDecoder().decode(e.data)); } catch { return; }
        control(s, msg);
      };
      ws.onopen = () => { s.retry = 0; };
      ws.onclose = () => {
        if (ws !== s.ws) return;
        s.ws = null;
        if (s.ended || !s.alive) { render(); return; }
        // Lost the connection (server restart, sleep): re-attach; the server replays the scrollback.
        s.retry = Math.min(s.retry + 1, 6);
        setBadge(s, "reconnecting");
        s.timer = setTimeout(() => connect(s, { attach: true }), 500 * 2 ** (s.retry - 1));
      };
    }

    function control(s, msg) {
      switch (msg.type) {
        case "attached":
          s.alive = !!msg.alive; s.pid = msg.pid; s.startedAt = msg.started; s.started = true;
          render();
          break;
        case "replay-start":
          s.suppress = true;   // don't answer terminal queries that are only being replayed
          s.term.reset();
          break;
        case "replay-end":
          s.term.write("", () => {
            s.suppress = false;
            if (samePath(state.active, s.path)) fitActive(true);
            send(s, { type: "resize", cols: s.term.cols, rows: s.term.rows });
          });
          break;
        case "exit":
          s.alive = false;
          // Reset attributes, leave any alternate screen and move below everything Claude drew before the note.
          s.term.write(`\x1b[0m\x1b[?1049l\x1b[?25h\x1b[999;1H\r\n\x1b[38;2;156;188;171m── Claude exited${msg.code === null || msg.code === undefined ? "" : ` (code ${msg.code})`}. Press Start again or Resume last. ──\x1b[0m\r\n`);
          render();
          refreshSessions();
          break;
        case "ended":
          s.ended = true; s.alive = false;
          dropSession(s.path);
          break;
        case "error":
          s.alive = false; s.ended = true;
          s.term.write(`\r\n\x1b[38;2;255;92;122m${String(msg.message || "Error").replace(/[\x00-\x1f\x7f]/g, " ")}\x1b[0m\r\n`);
          render();
          break;
      }
    }

    function setBadge(s, text) { s.badge = text; render(); }

    async function openSession(path, { resume = false } = {}) {
      if (CC.setActiveProject) CC.setActiveProject(path);
      try { await loadXterm(); } catch (e) { CC.showMessage("Terminal", e.message); return; }
      const p = projectFor(path);
      let s = findSession(path);
      if (s && (s.alive || (s.ws && !s.started))) { activate(s.path); return; }
      if (s) dropSession(s.path, true);
      if (state.serverSessions.filter((x) => x.alive).length >= 6 && !state.serverSessions.some((x) => x.alive && samePath(x.path, path))) {
        CC.showMessage("Too many sessions", "6 Claude sessions are already running. End one first.");
        return;
      }
      s = createTerminal(p ? p.path : path, p ? p.name : path.split(/[\\/]/).pop());
      state.sessions.set(s.path, s);
      activate(s.path);
      s.alive = true;
      connect(s, { resume });
      setTimeout(refreshSessions, 1500);
    }

    async function attachExisting(info) {
      await loadXterm();
      if (findSession(info.path)) return findSession(info.path);
      const s = createTerminal(info.path, info.name);
      s.alive = info.alive;
      state.sessions.set(s.path, s);
      connect(s, { attach: true });
      return s;
    }

    function dropSession(path, silent = false) {
      const s = findSession(path);
      if (!s) return;
      clearTimeout(s.timer);
      s.ended = true;
      const ws = s.ws; s.ws = null;
      if (ws) try { ws.close(); } catch { /* already closed */ }
      s.term.dispose();
      s.host.remove();
      state.sessions.delete(s.path);
      if (samePath(state.active, path)) state.active = null;
      if (!silent) render();
    }

    async function endSession(path) {
      const s = findSession(path);
      const name = s ? s.name : path;
      if (!await CC.confirmDialog("End session", `Stop Claude in ${name}? Claude and anything it started will be closed. You can pick the conversation up later with Resume last.`, "End session")) return;
      await guarded(async () => {
        try { await api("/api/terminal/end", { path }); } catch (e) { if (!/No Claude session/.test(e.message)) throw e; }
        dropSession(path);
        await refreshSessions();
      });
    }

    function activate(path, focus = true) {
      state.active = path;
      if (path) store.set("cc.projects.active", path);
      for (const s of state.sessions.values()) s.host.hidden = !samePath(s.path, path);
      render();
      if (path) fitActive(focus);
    }

    // Show the session of the globally selected project: attach to a running one, never start one.
    let followSeq = 0;
    async function followProject() {
      const ap = CC.activeProject;
      const seq = ++followSeq;
      if (!ap || !ap.path) { activate(null); return; }
      let s = findSession(ap.path);
      if (!s) {
        const info = state.serverSessions.find((x) => x.alive && samePath(x.path, ap.path));
        if (info) {
          try { s = await attachExisting(info); } catch (e) { CC.showMessage("Terminal", e.message); }
          if (seq !== followSeq) return;
        }
      }
      if (s) activate(s.path, false);
      else activate(null);
    }

    function setListOpen(open) {
      state.listOpen = !!open;
      store.set("cc.projects.listOpen", state.listOpen ? "1" : null);
      if (ui.layout) ui.layout.classList.toggle("list-open", state.listOpen);
      if (ui.listBtn) {
        ui.listBtn.setAttribute("aria-expanded", String(state.listOpen));
        ui.listBtn.textContent = state.listOpen ? "Hide projects" : "All projects";
      }
      fitActive();
    }

    let fitFrame = 0;
    function fitActive(focus = false) {
      cancelAnimationFrame(fitFrame);
      fitFrame = requestAnimationFrame(() => {
        const s = findSession(state.active);
        if (!s || s.host.hidden || !ui.termWrap.offsetParent) return;
        const size = termFontSize();
        if (s.term.options.fontSize !== size) s.term.options.fontSize = size;
        try { s.fit.fit(); } catch { /* not measurable yet */ }
        if (focus) s.term.focus();
      });
    }

    async function refreshSessions() {
      try {
        const r = await api("/api/terminal/sessions");
        state.serverSessions = r.sessions || [];
      } catch { state.serverSessions = []; }
      render();
    }


    // ---- projects list -------------------------------------------------------------------

    async function loadProjects() {
      ui.list.replaceChildren(el("li", { class: "empty" }, "Loading projects…"));
      try {
        const r = await api("/api/projects");
        state.projects = r.projects || []; state.claude = r.claude; state.roots = r.roots || [];
      } catch (e) {
        ui.list.replaceChildren(el("li", { class: "err" }, e.message));
        return;
      }
      state.loaded = true;
      render();
    }

    function newProjectDialog() {
      const input = el("input", { placeholder: "Project name (letters, numbers, spaces, - _ .)", maxlength: "61", autocomplete: "off", spellcheck: "false" });
      const create = () => guarded(async () => {
        const p = await api("/api/projects/new", { name: input.value });
        document.querySelector("#dialog").close();
        await loadProjects();
        CC.showMessage("Project created", `${p.path} is ready (empty git repo on branch main). Press Claude on it to start.`);
      });
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); create(); } });
      CC.showDialog(el("h3", {}, "New project"),
        el("p", { class: "muted" }, `Creates a folder in ${state.roots[0] || "the first projects root"} and runs git init.`),
        input,
        el("div", { class: "row" }, el("button", { value: "cancel", class: "subtle" }, "Cancel"),
          el("button", { type: "button", class: "primary", onclick: create }, "Create")));
      setTimeout(() => input.focus(), 30);
    }

    function projectCard(p) {
      const live = state.serverSessions.find((x) => samePath(x.path, p.path) && x.alive);
      const isActive = samePath(state.active, p.path);
      return el("li", { class: `pj-card${isActive ? " selected" : ""}${live ? " live" : ""}`, title: p.path },
        el("div", { class: "pj-card-main" },
          el("div", { class: "pj-card-top" },
            el("span", { class: "title pj-name" }, p.name),
            live && el("span", { class: "tag live" }, "live"),
            p.branch && el("span", { class: "chip pj-branch", title: "Git branch" }, "⎇ ", p.branch),
            p.git && p.changed_files > 0 && el("span", { class: "tag pj-dirty", title: `${p.changed_files} changed file(s)` }, `${p.changed_files} changed`)),
          p.last_commit
            ? el("div", { class: "pj-commit", title: p.last_commit }, el("span", { class: "pj-commit-msg" }, p.last_commit), el("span", { class: "pj-when" }, ago(p.last_commit_at)))
            : el("div", { class: "pj-commit muted" }, p.git ? "No commits yet" : "Not a git repo", p.modified && el("span", { class: "pj-when" }, `edited ${ago(p.modified)}`)),
          p.summary && el("div", { class: "pj-summary" }, p.summary)),
        el("div", { class: "pj-actions" },
          el("button", { class: "small primary", title: "Open Claude Code here, inside Command Center", onclick: () => openSession(p.path) }, live ? "Show" : "Claude"),
          !live && el("button", { class: "small", title: "Continue the most recent conversation in this folder (claude --continue)", onclick: () => openSession(p.path, { resume: true }) }, "Resume last"),
          el("button", { class: "small alt", title: "Open Claude in a separate Windows Terminal window", onclick: () => guarded(() => api("/api/projects/launch", { path: p.path, resume: false, prompt: "" })) }, "New window"),
          el("button", { class: "small subtle", title: "Open the folder in Explorer", onclick: () => guarded(() => api("/api/projects/folder", { path: p.path })) }, "Folder")));
    }

    function render() {
      if (!ui.list) return;
      if (state.loaded) {
        CC.fill(ui.list, state.projects.map(projectCard), "No projects found. Add folders under projects: in config.yaml.");
        ui.count.textContent = String(state.projects.length);
      }
      ui.warn.hidden = !state.loaded || !!state.claude;

      // session chips (one per terminal the page has, plus server sessions not attached yet)
      const chips = [];
      for (const s of state.sessions.values()) {
        const isActive = samePath(state.active, s.path);
        chips.push(el("button", { class: `pj-chip${isActive ? " active" : ""}${s.alive ? "" : " dead"}`, title: s.path,
          onclick: () => { if (CC.setActiveProject) CC.setActiveProject(s.path); activate(s.path); } },
          el("span", { class: `pj-dot${s.alive ? " on" : ""}` }), s.name));
      }
      ui.chips.replaceChildren(...chips);
      ui.chips.hidden = !chips.length;

      const s = findSession(state.active);
      const ap = CC.activeProject;
      ui.empty.hidden = !!s;
      if (!s) renderEmpty(ap);
      ui.title.textContent = s ? s.name : ap ? ap.name : "No project";
      ui.status.replaceChildren(
        !s ? "" : s.badge === "reconnecting" && s.alive && !s.ws ? el("span", { class: "tag today" }, "reconnecting")
          : s.alive ? el("span", { class: "tag live" }, "running") : el("span", { class: "tag overdue" }, s.ended ? "closed" : "exited"));
      ui.path.textContent = s ? s.path : ap ? ap.path : "";
      ui.end.hidden = !s;
      ui.end.textContent = s && s.alive ? "End session" : "Close";
      ui.restart.hidden = !s || s.alive;
      ui.window.hidden = !s;
    }

    const KEYS = () => el("div", { class: "pj-keys muted" },
      el("span", {}, el("kbd", {}, "Ctrl+Shift+C"), " copy"), el("span", {}, el("kbd", {}, "Ctrl+Shift+V"), " paste"),
      el("span", {}, el("kbd", {}, "Right-click"), " copy / paste"), el("span", {}, el("kbd", {}, "Ctrl+click"), " open link"));

    // The panel when no terminal is shown: pick a project, or start Claude in the selected one (click only).
    let emptyKey = "";
    function renderEmpty(ap) {
      const live = ap && state.serverSessions.some((x) => x.alive && samePath(x.path, ap.path));
      const key = `${ap ? ap.path + "|" + ap.name : ""}|${state.checked}|${live}|${!!state.claude || !state.loaded}`;
      if (key === emptyKey) return;
      emptyKey = key;
      const mark = el("div", { class: "pj-empty-mark" }, "◢◤");
      if (!ap) {
        ui.empty.replaceChildren(mark,
          el("div", { class: "pj-empty-title" }, "Choose what you're working on"),
          el("div", { class: "muted" }, "Pick a project in WORKING ON at the top of this pane. Claude Code then runs right here, in that project's folder."),
          KEYS());
        return;
      }
      if (!state.checked || live) {
        ui.empty.replaceChildren(mark, el("div", { class: "pj-empty-title" }, live ? `Attaching to ${ap.name}…` : "Checking for running sessions…"));
        return;
      }
      ui.empty.replaceChildren(mark,
        el("div", { class: "pj-empty-title" }, `Claude isn't running in ${ap.name}`),
        el("div", { class: "pj-empty-path muted" }, ap.path),
        el("div", { class: "pj-start-row" },
          el("button", { type: "button", class: "primary pj-start", onclick: () => openSession(ap.path) }, `Start Claude in ${ap.name}`),
          el("button", { type: "button", title: "Continue the most recent conversation in this folder (claude --continue)", onclick: () => openSession(ap.path, { resume: true }) }, "Resume last"),
          el("button", { type: "button", class: "alt", title: "Open Claude in a separate Windows Terminal window",
            onclick: () => guarded(() => api("/api/projects/launch", { path: ap.path, resume: false, prompt: "" })) }, "New window")),
        el("div", { class: "muted" }, "Sessions keep running when you switch tabs or close the window; End session stops them."),
        KEYS());
    }

    // ---- tab ---------------------------------------------------------------------------

    CC.registerTab({
      id: "projects", label: "Claude", pane: "work", before: "websites",
      render(section) {
        section.classList.add("fill");
        ui.count = el("span", { class: "chip" }, "");
        ui.list = el("ul", { class: "list pj-list" });
        ui.warn = el("div", { class: "pj-warn", hidden: true }, "Claude Code wasn't found. Install it, or set projects.claude_path in config.yaml.");
        ui.chips = el("div", { class: "pj-chips", hidden: true });
        ui.title = el("span", { class: "pj-term-title" }, "No project");
        ui.status = el("span", { class: "pj-status" });
        ui.path = el("span", { class: "pj-path muted" });
        ui.restart = el("button", { class: "small primary", hidden: true, onclick: () => state.active && openSession(state.active) }, "Start again");
        ui.window = el("button", { class: "small alt", hidden: true, title: "Open Claude for this project in a separate Windows Terminal window",
          onclick: () => state.active && guarded(() => api("/api/projects/launch", { path: state.active, resume: false, prompt: "" })) }, "New window");
        ui.end = el("button", { class: "small subtle", hidden: true, onclick: () => {
          const s = findSession(state.active);
          if (!s) return;
          s.alive ? endSession(s.path) : (dropSession(s.path), refreshSessions());
        } }, "End session");
        ui.listBtn = el("button", { class: "small subtle", type: "button", "aria-expanded": "false", "aria-controls": "pj-side",
          title: "Show or hide the full project list", onclick: () => setListOpen(!state.listOpen) }, "All projects");
        ui.empty = el("div", { class: "pj-empty" });
        ui.termWrap = el("div", { class: "pj-term" }, ui.empty);

        // The app opens its palette on Ctrl+K at document level. While the terminal has focus, Ctrl+K
        // belongs to the terminal: stop it here (capture phase, before xterm or the document see it)
        // and send Claude the same control code xterm would have sent.
        ui.termWrap.addEventListener("keydown", (e) => {
          if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "k") {
            e.preventDefault();
            e.stopPropagation();
            const s = findSession(state.active);
            if (s) sendInput(s, "\x0b");
          }
        }, true);

        ui.layout = el("div", { class: "pj-layout" },
          el("aside", { class: "panel pj-side", id: "pj-side" },
            el("div", { class: "panel-head" },
              el("span", { class: "panel-label" }, "PROJECTS"), ui.count,
              el("span", { class: "spacer" }),
              el("button", { class: "small subtle", title: "Refresh", "aria-label": "Refresh projects", onclick: () => { loadProjects(); refreshSessions(); } }, "↻"),
              el("button", { class: "small primary", onclick: newProjectDialog }, "+ New project")),
            ui.warn, ui.list),
          el("div", { class: "panel pj-main" },
            el("div", { class: "panel-head pj-head" },
              el("span", { class: "panel-label" }, "CLAUDE //"), ui.title, ui.status, ui.path,
              el("span", { class: "spacer" }), ui.restart, ui.window, ui.end, ui.listBtn),
            ui.chips, ui.termWrap));
        section.append(ui.layout);
        setListOpen(store.get("cc.projects.listOpen") === "1");
        render();

        // The terminal re-fits whenever its box changes: window resize, splitter drag, single/split switch.
        new ResizeObserver(() => fitActive()).observe(ui.termWrap);
        document.addEventListener("cc:layout", () => fitActive());
        document.addEventListener("cc:project", () => {
          if (state.checked) followProject();
          else render();
        });
      },
      async onShow() {
        if (!state.loaded) {
          await Promise.all([loadProjects(), refreshSessions()]);
          state.checked = true;
          // Re-attach to sessions that kept running while the page was closed or reloaded.
          const running = state.serverSessions.filter((x) => x.alive);
          try { for (const info of running) await attachExisting(info); } catch (e) { CC.showMessage("Terminal", e.message); }
          // Nothing picked yet: follow the session that was on screen last time (or the only one running).
          if (!CC.activeProject && running.length) {
            const remembered = store.get("cc.projects.active");
            const pick = running.find((x) => samePath(x.path, remembered)) || running[0];
            if (CC.setActiveProject) CC.setActiveProject(pick.path);
          }
          await followProject();
        } else {
          await refreshSessions();
          await followProject();
          fitActive(!!state.active);
        }
      },
    });

    CC.addPaletteSource(() => state.projects.map((p) => ({
      group: "Projects", label: `Claude: ${p.name}`, hint: p.branch ? `${p.branch} · ${p.path}` : p.path,
      run: () => { if (CC.setActiveProject) CC.setActiveProject(p.path); CC.openTab("projects"); openSession(p.path); },
    })));

    // Palette entries need the list even before the tab is opened.
    api("/api/projects").then((r) => { if (!state.loaded) { state.projects = r.projects || []; state.claude = r.claude; state.roots = r.roots || []; } }).catch(() => {});
  }

  if (window.CC) start();
  else document.addEventListener("cc:ready", start, { once: true });
})();
