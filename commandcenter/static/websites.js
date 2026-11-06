// Websites tab: sites added right here (websites.py), each shown inside the tab, above any value monitors from
// config.yaml (app.js "websites"). A site that refuses to be framed still shows, because in the desktop app the window
// lays its own WebView2 over this tab's stage (websites.WebsiteView), placed and hidden through nativestage.js. In a
// plain browser (serve mode) a site opens in the default browser instead. The view's title / host / path come from the
// site, so they're shown as text only (CC.el). The password only ever goes one way: it's never sent back to this page.
// Tabs (2026-11-06): a link in a site that wants a new window (an Okta app tile) opens as a tab in the strip, closed
// with its ×; a page on another site waits for Allow (remembered under Allowed sites) or Open in browser.
(function () {
  "use strict";
  if (!window.CC) return;
  const { el, api, openExternal, guarded, showDialog, confirmDialog } = CC;

  const KEY = "cc.websites.site";
  const MONITORS = "monitors";
  const section = document.querySelector("#tab-websites");
  const monitorParts = [section.querySelector(".toolbar"), section.querySelector("#web-list")];
  const st = { sites: [], tabs: [], monitors: 0, available: false, views: {}, error: null, choice: saved(), openedSeen: null };
  const strip = el("div", { class: "ws-tabs", role: "tablist", "aria-label": "Websites" });
  const bar = el("div", { class: "pv-bar ws-bar" });
  const ask = el("div", { class: "ws-ask", role: "alert", hidden: true });
  const hole = el("div", { class: "pv-hole" });
  const stage = el("div", { class: "pv-stage" }, hole);
  const empty = el("div", { class: "pv-fallback panel ws-empty" },
    el("div", { class: "panel-label" }, "WEBSITES"),
    el("p", {}, "Add a site with its address and, if it needs one, its login. It opens right here in this tab."),
    el("button", { type: "button", class: "primary", onclick: () => siteDialog() }, "+ Add website"));
  let poll, nativeStage, barKey = "", stripKey = "";

  function saved() { try { return localStorage.getItem(KEY) || ""; } catch { return ""; } }
  const hostOf = (url) => { try { return new URL(url).host; } catch { return url; } };
  const siteName = (id) => st.sites.find((s) => s.id === id)?.name || "Website";
  // A tab's name: its page's title, else the site it came from.
  const tabName = (t) => (st.views[t.id]?.title || "").trim() || `${siteName(t.opener)} ›`;
  const tabEntry = (t) => ({ id: t.id, name: tabName(t), tab: true, opener: t.opener, url: "" });
  function current() {
    if (st.choice === MONITORS && st.monitors) return null;
    const tab = st.tabs.find((t) => t.id === st.choice);
    if (tab) return tabEntry(tab);
    return st.sites.find((s) => s.id === st.choice) || st.sites[0] || null;
  }

  function choose(id) {
    st.choice = id;
    // tabs last only as long as Command Center runs: remember a saved site (or the site a tab came from)
    const keep = st.tabs.find((t) => t.id === id)?.opener || id;
    try { localStorage.setItem(KEY, keep); } catch { /* storage blocked */ }
    render();
    if (id !== MONITORS) loadStatus();
    nativeStage?.schedule();
  }

  function renderStrip(site) {
    const key = JSON.stringify([st.sites.map((s) => [s.id, s.name]), st.tabs.map((t) => [t.id, tabName(t)]), st.monitors, site?.id || "", st.available]);
    if (key === stripKey) return;
    stripKey = key;
    const tabBtn = (id, label, title, extra = null) => el("div", { class: `ws-tab${site?.id === id || (!site && id === MONITORS) ? " on" : ""}` },
      el("button", { type: "button", role: "tab", class: "ws-tab-go", title, "aria-selected": String(site?.id === id || (!site && id === MONITORS)),
        onclick: () => choose(id) }, label), extra);
    strip.replaceChildren(
      ...st.sites.map((s) => tabBtn(s.id, s.name, hostOf(s.url))),
      ...st.tabs.map((t) => {
        const name = tabName(t);
        return tabBtn(t.id, name, `${name} (opened from ${siteName(t.opener)})`,
          el("button", { type: "button", class: "ws-x", title: `Close ${name}`, "aria-label": `Close ${name}`, onclick: () => closeTab(t.id) }, "×"));
      }),
      ...(st.monitors ? [tabBtn(MONITORS, "Monitors", "Value monitors from config.yaml")] : []),
      el("button", { type: "button", class: "small subtle ws-add", title: "Add a website", onclick: () => siteDialog() }, "+ Add"));
    strip.style.display = st.sites.length || st.monitors ? "" : "none";
  }

  function renderAsk(site, v) {
    const key = site && st.available && v.ask;
    ask.hidden = !key;
    if (!key) { ask.dataset.key = ""; return; }
    if (ask.dataset.key === `${site.id}|${key}`) return;
    ask.dataset.key = `${site.id}|${key}`;
    ask.replaceChildren(
      el("span", { class: "ws-ask-text" }, "This page goes to ", el("b", {}, key), ". Show it here in Command Center?"),
      el("button", { type: "button", class: "small primary", title: `Allow ${key} (and its subdomains) in the Websites tab from now on`,
        onclick: () => act("allow", { domain: key }) }, `Allow ${key}`),
      el("button", { type: "button", class: "small alt", onclick: () => act("outside") }, "Open in browser"),
      el("button", { type: "button", class: "small subtle", onclick: () => act("dismiss") }, "Not now"));
  }

  function render() {
    const site = current();
    const showMonitors = !site && st.monitors > 0;
    if (!site || !st.available) barKey = "";
    section.classList.toggle("fill", !!site);
    stage.style.display = site ? "" : "none";
    empty.style.display = !site && !st.monitors ? "" : "none";
    for (const part of monitorParts) part.style.display = showMonitors ? "" : "none";
    renderStrip(site);
    const v = (site && st.views[site.id]) || {};
    renderAsk(site, v);
    if (!site) { bar.replaceChildren(); bar.style.display = "none"; return; }
    bar.style.display = "";
    const edit = site.tab ? null : el("button", { type: "button", class: "small subtle", title: `Edit ${site.name}`, onclick: () => siteDialog(current()) }, "Edit");
    if (!st.available) {
      bar.replaceChildren(...[edit, el("span", { class: "muted pv-where" }, hostOf(site.url)),
        el("button", { type: "button", class: "small alt", onclick: () => openExternal(site.url) }, "Open in browser")].filter(Boolean));
      hole.dataset.site = "";
      hole.replaceChildren(el("div", { class: "pv-fallback panel" },
        el("div", { class: "panel-label" }, site.name),
        el("p", {}, `${site.name} shows here in the Command Center window. In a browser, open it in its own tab.`),
        el("button", { type: "button", class: "primary", onclick: () => openExternal(site.url) }, `Open ${site.name}`)));
      return;
    }
    const now = Date.now() / 1000;
    const note = (v.failed && v.note) || (v.note && now - (v.note_at || 0) < 15 ? v.note : null) ||
      (v.outside && now - v.outside.at < 15 ? `Opened ${v.outside.host} in your browser` : null) || st.error;
    // Only when something changed: rebuilding every poll would lose the focus.
    const key = JSON.stringify([st.sites.map((s) => [s.id, s.name, s.username, s.has_password]), site.id, v.can_back, v.can_forward, v.loading, v.ready, v.title, v.host, v.path, note, v.failed]);
    if (key === barKey) return;
    barKey = key;
    const tool = (action, label, title, off) => el("button", { type: "button", class: "small subtle", title, "aria-label": title, disabled: off, onclick: () => act(action) }, label);
    bar.replaceChildren(...[edit,
      !site.tab && (site.username || site.has_password) ? el("button", { type: "button", class: "small primary", title: "Fill this page's login form with the saved login. You press the site's own button.", onclick: () => act("signin") }, "Sign in") : null,
      tool("back", "←", "Back", !v.can_back), tool("forward", "→", "Forward", !v.can_forward),
      tool("reload", "↻", "Reload"), site.tab ? null : tool("home", "⌂", `Home (${hostOf(site.url)})`),
      el("span", { class: "pv-where", title: v.host ? `${v.host}${v.path || ""}` : "" },
        v.loading ? el("span", { class: "pv-loading", "aria-hidden": "true" }) : null,
        el("span", { class: "pv-title" }, v.title || (v.ready ? "" : "Starting…")),
        v.host ? el("span", { class: "muted pv-host" }, `${v.host}${v.path && v.path !== "/" ? v.path : ""}`) : null),
      note ? el("span", { class: `pv-note${v.failed || st.error ? " err" : ""}`, title: note }, note) : null,
      el("button", { type: "button", class: "small subtle", title: "Other sites you allowed in this tab", onclick: allowedDialog }, "Allowed sites"),
      el("button", { type: "button", class: "small alt", title: "Open this page in your default browser", onclick: () => act("open") }, "Open in browser"),
    ].filter(Boolean));
    if (hole.dataset.site !== site.id) {
      hole.dataset.site = site.id;
      hole.replaceChildren(el("div", { class: "pv-wait" }, site.tab ? "" : site.name.toUpperCase()));
    }
  }

  // ---- add / edit -----------------------------------------------------------------

  function siteDialog(site) {
    const name = el("input", { maxlength: "60", placeholder: "Optional: what to call it", value: site?.name || "" });
    const url = el("input", { maxlength: "500", placeholder: "example.com or https://example.com/start", value: site?.url || "", spellcheck: "false" });
    const username = el("input", { maxlength: "256", autocomplete: "off", spellcheck: "false", placeholder: "Optional", value: site?.username || "" });
    const password = el("input", { type: "password", maxlength: "512", autocomplete: "new-password",
      placeholder: site?.has_password ? "Saved. Leave empty to keep it" : "Optional" });
    const others = el("input", { spellcheck: "false", placeholder: "login.example.com, sso.example.net", value: (site?.other_hosts || []).join(", ") });
    const forget = site?.username || site?.has_password ? el("input", { type: "checkbox" }) : null;
    const err = el("div", { class: "err", role: "alert" });
    const save = async () => {
      err.textContent = "";
      // No name given: the site's own, without its www.
      const named = name.value.trim() || hostOf(url.value.includes("://") ? url.value : `https://${url.value}`).replace(/^www\./, "");
      try {
        const out = await api("/api/websites/sites", { id: site?.id || null, name: named, url: url.value, other_hosts: others.value,
          username: username.value, password: password.value, clear_login: !!forget?.checked });
        password.value = "";
        document.querySelector("#dialog").close();
        await loadSites();
        choose(out.id);
      } catch (e) { err.textContent = e.message; }
    };
    const removeSite = async () => {
      const yes = await confirmDialog(`Remove ${site.name}?`,
        `It leaves the Websites tab, and its saved username and password are deleted from Windows Credential Manager. If you're signed in to it here, sign out on the site first: that session otherwise stays in this tab's browser profile.`, "Remove");
      if (!yes) return siteDialog(site);
      await guarded(async () => { await api("/api/websites/sites/remove", { id: site.id }); await loadSites(); });
    };
    showDialog(el("h3", {}, site ? `Edit ${site.name}` : "Add a website"),
      el("label", { class: "field-label" }, "Address"), url,
      el("label", { class: "field-label" }, "Name"), name,
      el("label", { class: "field-label" }, "Username"), username,
      el("label", { class: "field-label" }, "Password"), password,
      el("p", { class: "muted ws-help" }, "Saved in Windows Credential Manager. Sign in fills the page when you click it, and you press the site's own button."),
      forget && el("label", { class: "ws-check" }, forget, " Delete the saved login"),
      el("label", { class: "field-label" }, "Other addresses (optional)"), others,
      el("p", { class: "muted ws-help" }, "Other sites it signs in through, such as an Okta or Google page. A page anywhere else asks first (Allow, or Open in browser). The login is only ever filled on the site's own address and these."),
      err,
      el("div", { class: "row" },
        site && el("button", { type: "button", class: "subtle", onclick: removeSite }, "Remove"),
        el("span", { class: "spacer" }),
        el("button", { value: "cancel", class: "subtle" }, "Cancel"),
        el("button", { type: "button", class: "primary", onclick: save }, site ? "Save" : "Add website")));
    (site ? name : url).focus();
  }

  // Other sites allowed from a tab or a site's link (kv websites:allowed): listed, each with Remove.
  async function allowedDialog() {
    const list = el("ul", { class: "ws-allowed" });
    const draw = (domains) => list.replaceChildren(...(domains.length ? domains.map((d) => el("li", {},
      el("span", {}, d),
      el("button", { type: "button", class: "small subtle", onclick: () => guarded(async () => {
        await api("/api/websites/allowed/remove", { domain: d });
        draw((await api("/api/websites/allowed")).allowed);
      }) }, "Remove"))) : [el("li", { class: "muted" }, "None yet.")]));
    await guarded(async () => {
      draw((await api("/api/websites/allowed")).allowed);
      showDialog(el("h3", {}, "Allowed sites"),
        el("p", { class: "muted" }, "Sites you allowed to show in the Websites tab when a site or a tab went there (an Okta app, a sign-in page). Each includes its subdomains, except on shared hosting, where only that exact name is allowed. A removed one asks again next time."),
        list,
        el("div", { class: "row" }, el("span", { class: "spacer" }), el("button", { value: "cancel" }, "Close")));
    });
  }

  // ---- talking to the server ------------------------------------------------------

  function act(action, extra = {}) {
    return guarded(async () => {
      await api("/api/window/websites/action", { site: current()?.id, action, ...extra });
      setTimeout(loadStatus, 400);
    });
  }

  function closeTab(id) {
    return guarded(async () => {
      if (st.choice === id) {  // the next tab along, else the site it came from
        const i = st.tabs.findIndex((t) => t.id === id);
        const next = st.tabs[i + 1] || st.tabs[i - 1];
        st.choice = next?.id || st.tabs[i]?.opener || "";
      }
      st.tabs = st.tabs.filter((t) => t.id !== id);
      render();
      nativeStage?.schedule();
      await api("/api/window/websites/action", { site: id, action: "close" });
      setTimeout(loadStatus, 400);
    });
  }

  async function loadStatus() {
    try {
      const s = await api("/api/window/websites");
      st.available = !!s.available;
      st.views = s.views || {};
      st.tabs = s.tabs || [];
      st.error = s.error || null;
      // A link in the view just opened a tab: show it (the view already does). Not for one opened before this page.
      const at = s.opened?.at || 0;
      if (st.openedSeen === null) st.openedSeen = at;
      else if (at > st.openedSeen) {
        st.openedSeen = at;
        if (st.tabs.some((t) => t.id === s.opened.id)) st.choice = s.opened.id;
      }
    } catch { /* the bar just stays as it was */ }
    render();
    nativeStage?.schedule();
  }

  async function loadSites() {
    st.sites = (await api("/api/websites/sites")).sites;
    render();
    await loadStatus();
  }

  function follow() {  // title, back / forward, new tabs and notes, only while the tab is on screen
    clearInterval(poll);
    poll = setInterval(() => {
      if (!section.classList.contains("active")) { clearInterval(poll); return; }
      if (st.available && current()) loadStatus();
    }, 1500);
  }

  section.prepend(strip, bar, ask, empty, stage);
  render();
  // Each time the tab opens, app.js loads the monitors and says how many there are.
  document.addEventListener("cc:websites", (e) => { st.monitors = e.detail.monitors; guarded(loadSites); follow(); });
  nativeStage = CC.nativeStage?.({ view: "websites", section, hole, available: () => st.available, site: () => current()?.id || st.sites[0]?.id, shown: () => !!current() });
  CC.addPaletteSource(() => [
    ...st.sites.map((s) => ({ group: "Websites", label: `Website: ${s.name}`, hint: hostOf(s.url), run: () => { choose(s.id); CC.openTab("websites"); } })),
    ...st.tabs.map((t) => ({ group: "Websites", label: `Website tab: ${tabName(t)}`, hint: `from ${siteName(t.opener)}`, run: () => { choose(t.id); CC.openTab("websites"); } })),
    { group: "Websites", label: "Add a website", run: () => { CC.openTab("websites"); siteDialog(); } },
    { group: "Websites", label: "Websites: allowed sites", hint: "other sites allowed in the tab", run: allowedDialog },
  ]);
  guarded(loadSites);
})();
