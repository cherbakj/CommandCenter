// ScreenConnect tab (WORK): ScreenConnect (ConnectWise) remote-session windows docked into the main window
// (screenconnectdock.py), placed and hidden through nativestage.js like the SQL tab's SSMS (it shares ssms.css's bar
// and stage). Several sessions can be docked: one shows, a button each on the bar. A session Joe joins from the
// ScreenConnect web page docks here by itself ("Dock new sessions here"); the page is told and shows a note.
// Window titles name the remote PC, so they're shown as text only (CC.el) and never logged.
(function () {
  "use strict";
  if (!window.CC || CC.side) return;  // the main window's tab only (the side window has no ScreenConnect dock)
  const { el, api, guarded } = CC;

  if (!document.querySelector('link[href^="/static/ssms.css"]')) {  // the bar and stage styles (ssms.js adds it too)
    const css = document.createElement("link");
    css.rel = "stylesheet";
    css.href = "/static/ssms.css";
    document.head.append(css);
  }

  const st = { available: null, s: {}, busy: false, autoSeq: null, fresh: 0 };
  let section, bar, msg, hole, poll, bg, nativeStage, badge;

  // "Customer PC - ScreenConnect - Connected" -> "Customer PC"
  const nameOf = (title) => String(title || "").split(/\s+-\s+ScreenConnect\b/)[0].trim() || "Session";
  const docked = () => st.s.docked || [];
  const active = () => docked().find((d) => d.active);
  const isShown = () => !!section?.classList.contains("active");

  async function load() {
    try {
      st.s = await api("/api/window/screenconnect");
      st.available = !!st.s.available;
    } catch { st.s = {}; st.available = false; }
    noticeAuto();
    render();
    nativeStage?.schedule();
    if (st.available && !bg) bg = setInterval(() => { if (!isShown()) load(); }, 1500);  // new sessions while elsewhere
  }

  // A session docked by itself (Joe just joined it): the ScreenConnect tab opens to show it, with a note (Joe's call
  // 2026-11-06: the session window shouldn't seem to vanish). Only while this page is on show (the dock only takes a
  // session while the window is); otherwise a count on the tab's button until it's opened.
  function noticeAuto() {
    const auto = st.s.auto || {};
    if (typeof auto.seq !== "number") return;
    if (st.autoSeq === null) { st.autoSeq = auto.seq; return; }  // what happened before this page loaded isn't news
    if (auto.seq <= st.autoSeq) return;
    const n = auto.seq - st.autoSeq;
    st.autoSeq = auto.seq;
    if (isShown()) return;
    CC.toast(`ScreenConnect: ${n > 1 ? `${n} new sessions` : `“${nameOf(auto.title)}”`} docked in the ScreenConnect tab`);
    if (document.visibilityState === "visible") { CC.openTab("screenconnect"); return; }
    st.fresh += n;
    showBadge();
  }

  function showBadge() {
    const button = document.querySelector('.nav-group button[data-tab="screenconnect"]');
    if (!button) return;
    if (!badge) { badge = el("span", { class: "badge", hidden: true }); button.append(badge); }
    badge.hidden = !st.fresh;
    badge.textContent = st.fresh > 9 ? "9+" : String(st.fresh);
    button.title = st.fresh ? `${st.fresh} new ScreenConnect session${st.fresh > 1 ? "s" : ""} docked` : "";
  }

  async function act(action, extra = {}) {
    st.busy = true;
    render();
    await guarded(async () => { await api("/api/window/screenconnect/action", { action, ...extra }); });
    st.busy = false;
    setTimeout(load, 300);
    setTimeout(load, 1500);
  }

  function dockRunning() {
    const list = st.s.running || [];
    if (list.length === 1) return act("dock", { hwnd: list[0].hwnd });
    CC.showDialog(el("h3", {}, "Dock a ScreenConnect session"),
      el("p", { class: "muted" }, "ScreenConnect windows open as you. Docking holds the window over this tab; Undock gives it back. Nothing is closed."),
      el("ul", { class: "list" }, list.map((w) => el("li", {},
        el("div", { class: "grow" }, el("div", { class: "title" }, w.title || "ScreenConnect"),
          el("div", { class: "muted" }, w.host ? "A session you joined" : "Another ScreenConnect window (not a session you joined)")),
        el("div", { class: "actions" }, el("button", { type: "button", class: "small primary",
          onclick: () => { document.querySelector("#dialog").close(); act("dock", { hwnd: w.hwnd }); } }, "Dock"))))),
      el("div", { class: "row" }, el("button", {}, "Close")));
  }

  function autoSwitch() {
    const box = el("input", { type: "checkbox", checked: st.s.autodock !== false, disabled: st.busy });
    box.addEventListener("change", () => act("autodock", { on: box.checked }));
    return el("label", { class: "sc-auto", title: "A session you join from the ScreenConnect web page docks here by itself while Command Center is open" },
      box, "Dock new sessions here");
  }

  function render() {
    if (!bar) return;
    const s = st.s || {};
    const cur = active();
    const running = s.running || [];
    const parts = [el("span", { class: "panel-label ss-label" }, "SCREENCONNECT")];
    if (st.available) {
      parts.push(
        ...docked().map((d) => el("button", {
          type: "button", class: `small ${d.active ? "primary" : "subtle"} sc-win`, title: d.title || "", "aria-pressed": String(!!d.active),
          onclick: () => act("show", { hwnd: d.hwnd }),
        }, nameOf(d.title))),
        s.restoring ? el("span", { class: "ss-note" }, "Docking your sessions again…") : null,
        s.hung ? el("span", { class: "ss-note err" }, "Session isn't responding") : null,
        s.note ? el("span", { class: "ss-note" }, s.note) : null,
        el("span", { class: "spacer" }),
        autoSwitch(),
        running.length ? el("button", { type: "button", class: "small subtle", disabled: st.busy, onclick: dockRunning },
          running.length > 1 ? `Dock running session (${running.length})` : "Dock running session") : null,
        cur ? el("button", { type: "button", class: "small subtle", title: "Give this session window back (nothing is closed)",
          onclick: () => act("undock", { hwnd: cur.hwnd }) }, "Undock") : null);
    }
    bar.replaceChildren(...parts.filter(Boolean));
    renderStage();
  }

  // What shows while no session is docked here (a docked one covers it).
  function renderStage() {
    const s = st.s || {};
    let body;
    if (st.available === null) body = [el("p", { class: "muted" }, "Checking for ScreenConnect…")];
    else if (!st.available) body = [el("p", {}, "ScreenConnect session windows dock here in the Command Center desktop app."),
      el("p", { class: "muted" }, "In a browser, join sessions from the ScreenConnect web page as usual: they open in their own window.")];
    else if (active()) body = [el("p", { class: "muted" }, "The session is docked here.")];
    else body = [
      el("p", {}, s.autodock !== false
        ? "Join a session from the ScreenConnect web page and its window docks here by itself."
        : "Join a session from the ScreenConnect web page, then dock its window here."),
      el("p", { class: "muted" }, "Several sessions can be docked: a button each on the bar. Undock gives the one showing back as a normal window; nothing is ever closed, and a Command Center restart or crash never ends a session."),
      el("p", { class: "muted" }, "While a session has the keyboard, keys go to the remote PC (Ctrl+K included). Click Command Center's header first for its own shortcuts."),
      (s.running || []).length ? el("div", { class: "row" }, el("button", { type: "button", class: "primary", disabled: st.busy, onclick: dockRunning }, "Dock running session")) : null];
    msg.replaceChildren(el("div", { class: "panel ss-card" }, el("div", { class: "panel-label" }, "SCREENCONNECT SESSIONS"), ...body.filter(Boolean)));
  }

  function onShow() {
    st.fresh = 0;
    showBadge();
    load();
    clearInterval(poll);
    poll = setInterval(() => {  // docked / closed / not responding, only while the tab is on screen
      if (!isShown()) { clearInterval(poll); return; }
      load();
    }, 2000);
  }

  CC.registerTab({
    id: "screenconnect", label: "ScreenConnect", pane: "work", onShow,
    render(sec) {
      section = sec;
      bar = el("div", { class: "ss-bar sc-bar" });
      msg = el("div", { class: "ss-msg" });
      hole = el("div", { class: "ss-hole" }, msg);
      section.classList.add("fill");
      section.replaceChildren(bar, el("div", { class: "ss-stage" }, hole));
      render();
    },
  });
  nativeStage = CC.nativeStage?.({ view: "screenconnect", section, hole, available: () => !!st.available });

  CC.addPaletteSource(() => [
    { group: "ScreenConnect", label: "ScreenConnect", hint: docked().length ? `${docked().length} docked` : "remote sessions", run: () => CC.openTab("screenconnect") },
    ...(st.available && (st.s.running || []).length ? [{ group: "Commands", label: "Dock running ScreenConnect session", hint: `${st.s.running.length} open`,
      run: () => { CC.openTab("screenconnect"); dockRunning(); } }] : []),
    ...(st.available ? docked().map((d) => ({ group: "ScreenConnect", label: `ScreenConnect: ${nameOf(d.title)}`, hint: "docked session",
      run: () => { CC.openTab("screenconnect"); act("show", { hwnd: d.hwnd }); } })) : []),
  ]);

  load();  // also lets the dock re-attach sessions after a restart as soon as the page reports
})();
