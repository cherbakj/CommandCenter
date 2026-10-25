// Pulse tab (WORK): the real Pulse site, Prod (pulse.sonance.com) or Dev (dev.pulse.sonance.com). The sites can't be
// framed (frame-ancestors 'self'), so in the desktop app the window lays its own WebView2 over this tab's stage
// (desktop.PulseView), placed and hidden through nativestage.js (a dialog, the palette, the splash... hide it; a toast
// cuts a hole). In a plain browser (serve mode) the tab offers the sites in the default browser instead.
// The view's page title / host / path come from another site, so they're shown as text only (CC.el).
(function () {
  "use strict";
  if (!window.CC) return;
  const { el, api, openExternal, guarded } = CC;

  const SITE_KEY = "cc.pulse.site";
  const LABEL = { prod: "Prod", dev: "Dev" };
  const st = {
    available: null,  // null until the first status; false in a browser / serve mode
    sites: { prod: "https://pulse.sonance.com/", dev: "https://dev.pulse.sonance.com/" },
    views: {}, error: null, site: savedSite(),
  };
  let section, bar, stage, hole, poll;

  function savedSite() { try { return localStorage.getItem(SITE_KEY) === "dev" ? "dev" : "prod"; } catch { return "prod"; } }
  function setSite(site) {
    if (site !== "prod" && site !== "dev") return;
    st.site = site;
    try { localStorage.setItem(SITE_KEY, site); } catch { /* storage blocked */ }
    renderBar();
    schedule();
  }

  // ---- tab -----------------------------------------------------------------------

  function render(sec) {
    section = sec;
    section.classList.add("fill");
    bar = el("div", { class: "pv-bar" });
    hole = el("div", { class: "pv-hole" }, el("div", { class: "pv-wait" }, "PULSE"));
    stage = el("div", { class: "pv-stage" }, hole);
    section.append(bar, stage);
    renderBar();
  }

  async function loadStatus() {
    try {
      const s = await api("/api/window/pulse");
      st.available = !!s.available;
      if (s.sites) st.sites = { ...st.sites, ...s.sites };
      st.views = s.views || {};
      st.error = s.error || null;
    } catch { st.available = st.available === null ? false : st.available; }
    renderBar();
    if (!st.available) renderFallback();
    schedule();
  }

  function onShow() {
    loadStatus();
    clearInterval(poll);
    poll = setInterval(() => {  // title, back/forward and notes, only while the tab is on screen
      if (!section.classList.contains("active")) { clearInterval(poll); return; }
      if (st.available) loadStatus();
    }, 2000);
  }

  function act(action) {
    return guarded(async () => {
      await api("/api/window/pulse/action", { site: st.site, action });
      setTimeout(loadStatus, 400);
    });
  }

  const hostOf = (url) => { try { return new URL(url).host; } catch { return url; } };

  function renderBar() {
    if (!bar) return;
    const v = st.views[st.site] || {};
    const seg = el("div", { class: "pv-seg", role: "group", "aria-label": "Pulse site" },
      ["prod", "dev"].map((s) => el("button", {
        type: "button", class: `small ${st.site === s ? "primary" : "subtle"}`, "aria-pressed": String(st.site === s),
        title: hostOf(st.sites[s]), onclick: () => setSite(s),
      }, LABEL[s])));
    if (!st.available) {
      bar.replaceChildren(seg, el("span", { class: "muted pv-where" }, hostOf(st.sites[st.site])), el("span", { class: "spacer" }),
        el("button", { type: "button", class: "small alt", onclick: () => openExternal(st.sites[st.site]) }, "Open in browser"));
      return;
    }
    // Notes stay for a short while (a failed view's note until it's reloaded).
    const now = Date.now() / 1000;
    const note = (v.failed && v.note) || (v.note && now - (v.note_at || 0) < 15 ? v.note : null) ||
      (v.outside && now - v.outside.at < 15 ? `Opened ${v.outside.host} in your browser` : null) || st.error;
    bar.replaceChildren(...[seg,
      el("button", { type: "button", class: "small subtle", title: "Back", "aria-label": "Back", disabled: !v.can_back, onclick: () => act("back") }, "←"),
      el("button", { type: "button", class: "small subtle", title: "Forward", "aria-label": "Forward", disabled: !v.can_forward, onclick: () => act("forward") }, "→"),
      el("button", { type: "button", class: "small subtle", title: "Reload", "aria-label": "Reload", onclick: () => act("reload") }, "↻"),
      el("button", { type: "button", class: "small subtle", title: `Home (${hostOf(st.sites[st.site])})`, "aria-label": "Home", onclick: () => act("home") }, "⌂"),
      el("span", { class: "pv-where", title: v.host ? `${v.host}${v.path || ""}` : "" },
        v.loading ? el("span", { class: "pv-loading", "aria-hidden": "true" }) : null,
        el("span", { class: "pv-title" }, v.title || (v.ready ? "" : "Starting…")),
        v.host ? el("span", { class: "muted pv-host" }, `${v.host}${v.path && v.path !== "/" ? v.path : ""}`) : null),
      note ? el("span", { class: `pv-note${v.failed || st.error ? " err" : ""}` }, note) : null,
      el("button", { type: "button", class: "small alt", title: "Open this page in your default browser", onclick: () => act("open") }, "Open in browser"),
    ].filter(Boolean));
  }

  // Serve mode / plain browser: no native view, so the sites open in the default browser.
  function renderFallback() {
    if (!hole || hole.dataset.fallback) return;
    hole.dataset.fallback = "1";
    const summary = el("p", { class: "muted" });
    api("/api/pulse/summary").then((s) => {
      if (!s.configured || s.error || s.aging_waiting == null) return;
      summary.textContent = s.aging_waiting ? [`${s.aging_waiting} ticket${s.aging_waiting === 1 ? "" : "s"} waiting on you`,
        s.aging_overdue ? `${s.aging_overdue} overdue` : null, s.aging_warn ? `${s.aging_warn} aging` : null].filter(Boolean).join(" · ")
        : "Nothing waiting on you.";
    }).catch(() => {});
    hole.replaceChildren(el("div", { class: "pv-fallback panel" },
      el("div", { class: "panel-label" }, "PULSE"),
      el("p", {}, "The Pulse site shows here in the Command Center window. In a browser, open it in its own tab:"),
      el("div", { class: "row" },
        el("button", { type: "button", class: "primary", onclick: () => openExternal(st.sites.prod) }, "Open Pulse"),
        el("button", { type: "button", class: "alt", onclick: () => openExternal(st.sites.dev) }, "Open Pulse Dev"),
        el("button", { type: "button", class: "subtle", onclick: () => CC.openTab("pulse-feedback") }, "Pulse Feedback")),
      summary));
  }

  // ---- telling the window where the stage is (nativestage.js) ------------------------

  let nativeStage = null;
  function schedule() { nativeStage?.schedule(); }

  // ---- for other modules -------------------------------------------------------------

  // CC.pulseSite.open(url): a Pulse page (a ticket) in the Pulse tab when the window has the view, else the browser.
  CC.pulseSite = {
    get available() { return !!st.available; },
    sites: () => ({ ...st.sites }),
    async open(url) {
      if (st.available === null) await loadStatus();
      if (!st.available) return openExternal(url);
      try {
        const r = await api("/api/window/pulse/action", { action: "navigate", url });
        setSite(r.site);
        CC.openTab("pulse");
      } catch { openExternal(url); }
    },
  };

  CC.registerTab({ id: "pulse", label: "Pulse", render, onShow, pane: "work" });
  if (section && CC.nativeStage) {
    nativeStage = CC.nativeStage({ view: "pulse", section, hole, available: () => !!st.available, site: () => st.site });
  }
  CC.addPaletteSource(() => [
    { group: "Pulse", label: "Pulse: Prod site", hint: hostOf(st.sites.prod), run: () => { setSite("prod"); CC.openTab("pulse"); } },
    { group: "Pulse", label: "Pulse: Dev site", hint: hostOf(st.sites.dev), run: () => { setSite("dev"); CC.openTab("pulse"); } },
  ]);
  loadStatus();
})();
