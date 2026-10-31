// Teams tab, right side: Teams on the web (teams.microsoft.com) in a native view of its own (siteview.TeamsView),
// placed and hidden through nativestage.js like the Pulse site. A chat from the Recent list opens here in full, to read
// and answer; "Open in Teams app" stays for calls and meetings. Its own profile (data/webview-teams), not the
// background reader's. Without the desktop host (serve mode) the tab keeps only the list, and chats open in the app.
// The page title / host / path come from Teams, so they're shown as text only (CC.el).
(function () {
  "use strict";
  if (!window.CC) return;
  const { el, api, guarded, openExternal, toast } = CC;

  const section = document.getElementById("tab-teams");
  const web = document.getElementById("teams-web");
  if (!section || !web) return;
  const st = { available: null, v: {}, error: null };
  let poll, nativeStage;

  const bar = el("div", { class: "tv-bar" });
  const hole = el("div", { class: "tv-hole" }, el("div", { class: "tv-wait" }, "TEAMS"));
  const stage = el("div", { class: "tv-stage" }, hole);
  web.append(bar, stage);
  section.classList.add("fill");

  // Narrow pane: the list and Teams on the web take turns (data-view="list" | "web"), with a switch on top of
  // each; wide: side by side and the switches are hidden (teams.css).
  const sideSwitch = el("div", { class: "tv-side-switch" });
  section.querySelector(".teams-side")?.prepend(sideSwitch);
  function setMode(mode) {
    section.dataset.view = mode === "list" ? "list" : "web";
    sideSwitch.replaceChildren(st.available ? modeSwitch() : "");
    renderBar();
    nativeStage?.schedule();
  }
  // Wide pane: the list (status, Recent, Send) can be hidden to the left so Teams on the web gets the whole tab;
  // remembered per viewer (localStorage cc.teams.sideHidden). Narrow panes use the Chats | Teams web switch instead.
  const SIDE_KEY = "cc.teams.sideHidden";
  let sideHidden = false;
  try { sideHidden = localStorage.getItem(SIDE_KEY) === "1"; } catch { /* storage blocked */ }
  function setSideHidden(hide) {
    sideHidden = !!hide;
    try { sideHidden ? localStorage.setItem(SIDE_KEY, "1") : localStorage.removeItem(SIDE_KEY); } catch { /* storage blocked */ }
    section.classList.toggle("side-hidden", sideHidden);
    renderBar();
    nativeStage?.schedule();
  }
  section.classList.toggle("side-hidden", sideHidden);
  const sideToggle = () => el("button", {
    type: "button", class: "small subtle tv-side-toggle", "aria-pressed": String(!sideHidden),
    title: sideHidden ? "Show the Teams status and Recent DMs list" : "Hide the list: Teams on the web uses the whole tab",
    "aria-label": sideHidden ? "Show the Teams list" : "Hide the Teams list", onclick: () => setSideHidden(!sideHidden),
  }, sideHidden ? "⟩ List" : "⟨");
  const modeSwitch = () => el("div", { class: "tv-switch", role: "group", "aria-label": "Teams view" },
    [["list", "Chats"], ["web", "Teams web"]].map(([m, label]) => el("button", {
      type: "button", class: `small ${section.dataset.view === m ? "primary" : "subtle"}`, "aria-pressed": String(section.dataset.view === m),
      onclick: () => setMode(m),
    }, label)));

  // Stay Available (keeplive.py): a switch on the bar, on screen whichever way the list is, and a Ctrl+K command. One
  // element, kept through the bar's rebuilds.
  let keep = null;
  const keepBox = el("input", { type: "checkbox" });
  const keepSwitch = el("label", { class: "tv-keep", title: "Teams won't go Away while Command Center is open and Windows is unlocked. Also holds off any screen saver, sleep or inactivity lock timer set on this PC." },
    keepBox, "Stay Available");
  const showKeep = () => { keepBox.checked = !!keep?.on; };
  const loadKeep = () => api("/api/teams/keep-available").then((k) => { keep = k; showKeep(); renderBar(); }).catch(() => {});
  const setKeep = (on) => guarded(async () => {
    keep = await api("/api/teams/keep-available", { on });
    toast(on ? "Stay Available on: Teams won't go Away while Command Center is open and Windows is unlocked"
      : "Stay Available off: Teams can go Away after about 5 minutes with no input");
  }).then(showKeep);  // what the server holds, if it refused
  keepBox.addEventListener("change", () => setKeep(keepBox.checked));
  CC.addPaletteSource(() => keep?.available ? [{ group: "Commands", label: `Stay Available in Teams: turn ${keep.on ? "off" : "on"}`, hint: `now ${keep.on ? "on" : "off"}`, run: () => setKeep(!keep.on) }] : []);

  async function load() {
    try {
      const s = await api("/api/window/teams");
      st.available = !!s.available;
      st.v = (s.views || {}).web || {};
      st.error = s.error || null;
    } catch { st.available = st.available === null ? false : st.available; }
    section.classList.toggle("tv-none", !st.available);
    sideSwitch.replaceChildren(st.available ? modeSwitch() : "");
    renderBar();
    nativeStage?.schedule();
  }

  function act(action, extra = {}) {
    return guarded(async () => {
      await api("/api/window/teams/action", { action, ...extra });
      setTimeout(load, 400);
    });
  }

  function renderBar() {
    if (!st.available) { bar.replaceChildren(); return; }
    const v = st.v;
    const now = Date.now() / 1000;
    const note = (v.failed && v.note) || (v.note && now - (v.note_at || 0) < 15 ? v.note : null) ||
      (v.outside && now - v.outside.at < 15 ? `Opened ${v.outside.host} in your browser` : null) || st.error;
    const call = v.call_hint_at && now - v.call_hint_at < 60;
    const keepFocused = document.activeElement === keepBox;
    bar.replaceChildren(...[sideToggle(), modeSwitch(),
      el("button", { type: "button", class: "small subtle", title: "Back", "aria-label": "Back", disabled: !v.can_back, onclick: () => act("back") }, "←"),
      el("button", { type: "button", class: "small subtle", title: "Reload", "aria-label": "Reload", onclick: () => act("reload") }, "↻"),
      el("button", { type: "button", class: "small subtle", title: "Teams home (chats)", "aria-label": "Teams home", onclick: () => act("home") }, "⌂"),
      el("span", { class: "tv-where", title: v.host ? `${v.host}${v.path || ""}` : "" },
        v.loading ? el("span", { class: "tv-loading", "aria-hidden": "true" }) : null,
        el("span", { class: "tv-title" }, v.title || (v.ready ? "" : "Starting…")),
        v.host && v.host !== "teams.microsoft.com" ? el("span", { class: "muted tv-host" }, v.host) : null),
      note ? el("span", { class: `tv-note${v.failed || st.error ? " err" : ""}` }, note) : null,
      keep?.available ? keepSwitch : null,
      el("button", { type: "button", class: `small ${call ? "primary" : "alt"}`, title: "Calls, meetings and anything else: the Teams app", onclick: () => act("app") }, "Open in Teams app"),
    ].filter(Boolean));
    if (keepFocused) keepBox.focus();
  }

  function startPoll() {
    clearInterval(poll);
    poll = setInterval(() => {  // title, back and notes, only while the tab is on screen
      if (!section.classList.contains("active")) { clearInterval(poll); return; }
      if (st.available) load();
    }, 2000);
  }

  // CC.teamsWeb: the Teams section (Recent rows, alerts) opens a chat here when the view exists, else in the app.
  CC.teamsWeb = {
    get available() { return !!st.available; },
    async open(link) {
      if (st.available === null) await load();
      if (!st.available) return openExternal(link);
      try {
        await api("/api/window/teams/action", { action: "chat", link });
        setMode("web");
        CC.openTab("teams");
        setTimeout(load, 600);
      } catch { openExternal(link); }
    },
    app(link) { return st.available ? act("app", link ? { link } : {}) : openExternal(link || "msteams:"); },
  };

  // The core Teams loader (counts, Recent list) runs after the view's status, so its rows know whether to offer "Open chat".
  const coreLoader = CC.loaders.teams;
  loadKeep();
  CC.loaders.teams = async () => { await load(); startPoll(); return coreLoader?.(); };
  if (CC.nativeStage) {
    nativeStage = CC.nativeStage({ view: "teams", section, hole, available: () => !!st.available });
  }
  CC.addPaletteSource(() => st.available ? [
    { group: "Commands", label: "Teams web: chats", hint: "Teams tab", run: () => { setMode("web"); CC.openTab("teams"); act("home"); } },
  ] : []);
  setMode("web");
  load();
})();
