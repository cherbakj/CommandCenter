// Side window only: the Outlook tab, classic Outlook (Mail and Calendar) docked into the side window (outlookdock.py),
// placed and hidden through nativestage.js like the SQL tab's SSMS (it shares ssms.css's bar and stage). Mail and
// Calendar switch Outlook's own view. Window titles come from Outlook, so they're shown as text only (CC.el).
(function () {
  "use strict";
  if (!window.CC || !CC.side) return;  // pinned with 📌 Pin in the side window (security.SIDE_TABS "outlook")
  const { el, api, guarded } = CC;
  const st = { available: null, s: {}, busy: false };
  let section, bar, msg, hole, poll, nativeStage;

  async function load() {
    try {
      st.s = await api("/api/window/outlook");
      st.available = !!st.s.available;
    } catch { st.s = {}; st.available = false; }
    render();
    nativeStage?.schedule();
  }

  async function act(action, extra = {}) {
    st.busy = true;
    render();
    await guarded(async () => { await api("/api/window/outlook/action", { action, ...extra }); });
    st.busy = false;
    setTimeout(load, 300);
    setTimeout(load, 1500);
  }

  function render() {
    if (!bar) return;
    const s = st.s || {};
    const parts = [el("span", { class: "panel-label ss-label" }, "OUTLOOK")];
    if (st.available && s.docked) {
      parts.push(
        el("div", { class: "seg", role: "group", "aria-label": "Outlook view" },
          el("button", { type: "button", class: "small", onclick: () => act("view", { view: "mail" }) }, "✉ Mail"),
          el("button", { type: "button", class: "small", onclick: () => act("view", { view: "calendar" }) }, "📅 Calendar")),
        el("span", { class: "ss-title", title: s.docked.title }, s.docked.title || "Outlook"),
        s.hung ? el("span", { class: "ss-note err" }, "Outlook isn't responding") : null,
        el("span", { class: "spacer" }),
        el("button", { type: "button", class: "small subtle", title: "Give Outlook back its own window (nothing is closed)", onclick: () => act("undock") }, "Undock"));
    } else {
      parts.push(s.note ? el("span", { class: "ss-note" }, s.note) : null, el("span", { class: "spacer" }),
        st.available && s.running ? el("button", { type: "button", class: "small primary", disabled: st.busy, onclick: () => act("dock") }, "Dock Outlook here") : null);
    }
    bar.replaceChildren(...parts.filter(Boolean));
    const body = st.available === null ? [el("p", { class: "muted" }, "Checking for Outlook…")]
      : !st.available ? [el("p", {}, "Classic Outlook docks here in the Command Center desktop app's side window.")]
      : s.docked ? [el("p", { class: "muted" }, "Outlook is docked here.")]
      : !s.running ? [el("p", {}, "Outlook isn't running. Command Center starts it again within a minute; then dock it here.")]
      : [el("p", {}, "Your Outlook, docked into this window: Mail and Calendar on the bar switch its view. Undock gives it back as a normal window; nothing is ever closed."),
        el("div", { class: "row" }, el("button", { type: "button", class: "primary", disabled: st.busy, onclick: () => act("dock") }, "Dock Outlook here"))];
    msg.replaceChildren(el("div", { class: "panel ss-card" }, el("div", { class: "panel-label" }, "CLASSIC OUTLOOK"), ...body));
  }

  function onShow() {
    load();
    clearInterval(poll);
    poll = setInterval(() => {  // docked / closed / not responding, only while the tab is on screen
      if (!section.classList.contains("active")) { clearInterval(poll); return; }
      load();
    }, 2000);
  }

  CC.registerTab({
    id: "outlook", label: "Outlook", pane: "comms", onShow,
    render(sec) {
      section = sec;
      bar = el("div", { class: "ss-bar" });
      msg = el("div", { class: "ss-hole" });
      hole = msg;
      section.classList.add("fill");
      section.replaceChildren(bar, el("div", { class: "ss-stage" }, hole));
      render();
    },
  });
  nativeStage = CC.nativeStage?.({ view: "outlook", section, hole, available: () => !!st.available });
})();
