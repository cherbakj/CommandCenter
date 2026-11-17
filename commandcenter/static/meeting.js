// Meeting tab (WORK; Joe's call 2026-11-17): Teams meetings inside Command Center, with the camera, microphone and
// screen sharing, in a view of their own (desktop.MeetingView, profile data/webview-meeting), so the Teams tab's
// chats stay free during a call and Work | Work can put the call beside ScreenConnect or VS Code.
// Before a meeting: the coming Teams meetings (GET /api/meetings) with Join, and Join with a pasted Teams link.
// During one: the meeting fills the tab (nativestage.js), the bar has Leave and "Switch to the Teams app", and a
// "● Meeting" button in the header (every tab) brings it back. Lock, Restart and Close end it.
// The view's page title and address come from Teams, so they're shown as text only (CC.el).
(function () {
  "use strict";
  if (!window.CC || CC.side) return;  // the side window has no native views: its Join goes to this tab (app.js)
  const { el, api, guarded, toast, openExternal, confirmDialog } = CC;

  const st = { available: null, meeting: null, view: {}, error: null };
  const meetings = { list: null, at: 0, error: null };
  let section, bar, stage, hole, idle, poll, nativeStage = null;
  const chip = el("button", { id: "meeting-chip", type: "button", class: "small meeting-chip", hidden: true,
    title: "A Teams meeting is open in Command Center. Click to go back to it", onclick: () => CC.openTab("meeting") }, "● Meeting");

  const timeOf = (iso) => new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const inMeeting = () => !!(st.available && st.meeting);

  // ---- tab -----------------------------------------------------------------------

  function render(sec) {
    section = sec;
    section.classList.add("fill");
    bar = el("div", { class: "pv-bar mt-bar" });
    idle = el("div", { class: "mt-idle" });
    hole = el("div", { class: "pv-hole mt-hole" }, idle);
    stage = el("div", { class: "pv-stage" }, hole);
    section.append(bar, stage);
    renderBar();
    renderIdle();
  }

  async function loadStatus() {
    const was = inMeeting();
    try {
      const s = await api("/api/window/meeting");
      st.available = !!s.available;
      st.meeting = s.meeting || null;
      st.view = (s.views || {}).web || {};
      st.error = s.error || null;
    } catch { if (st.available === null) st.available = false; }
    chip.hidden = !inMeeting();
    if (was !== inMeeting()) renderIdle();
    renderBar();
    nativeStage?.schedule();
  }

  async function loadMeetings(force = false) {
    if (!force && meetings.list && Date.now() - meetings.at < 60000) return;
    try {
      const r = await api("/api/meetings?hours=24");
      meetings.list = r.meetings || [];
      meetings.error = null;
    } catch (e) { meetings.error = e.message; meetings.list = meetings.list || []; }
    meetings.at = Date.now();
    renderIdle();
  }

  function onShow() {
    loadStatus();
    loadMeetings();
    startPoll();
  }

  // While the tab is on screen: every 2 s (bar, notes); while a meeting is open elsewhere: every 15 s (the header button).
  function startPoll() {
    clearInterval(poll);
    let tick = 0;
    poll = setInterval(() => {
      const shown = section?.classList.contains("active") && !document.hidden;
      if (shown || (inMeeting() && ++tick % 8 === 0)) loadStatus();
      if (!shown && !inMeeting()) { clearInterval(poll); poll = 0; }
    }, 2000);
  }

  function act(action, extra = {}) {
    return guarded(async () => {
      await api("/api/window/meeting/action", { action, ...extra });
      setTimeout(loadStatus, 300);
    });
  }

  async function join(link) {
    if (st.available === null) await loadStatus();
    if (!st.available) return openExternal(link);
    if (inMeeting() && !await confirmDialog("Join another meeting?", "You'll leave the meeting that's open now.", "Join")) return;
    await guarded(async () => {
      await api("/api/window/meeting/action", { action: "meeting", link });
      st.meeting = { at: Date.now() / 1000 };
      chip.hidden = false;
      renderIdle();
      CC.openTab("meeting");
      startPoll();
      setTimeout(loadStatus, 500);
    });
  }

  async function leave() {
    if (!await confirmDialog("Leave the meeting?", "The meeting closes here; your camera and microphone go off.", "Leave")) return;
    await act("leave");
    st.meeting = null;
    chip.hidden = true;
    renderIdle();
    renderBar();
    nativeStage?.schedule();
  }

  async function toApp() {
    if (!await confirmDialog("Carry on in the Teams app?", "You leave the meeting here first, then the Teams app opens it.", "Open in Teams app")) return;
    await act("app");
    st.meeting = null;
    chip.hidden = true;
    renderIdle();
  }

  function renderBar() {
    if (!bar) return;
    const v = st.view || {};
    const now = Date.now() / 1000;
    const note = (v.failed && v.note) || (v.note && now - (v.note_at || 0) < 15 ? v.note : null) ||
      (v.outside && now - v.outside.at < 15 ? `Opened ${v.outside.host} in your browser` : null) || st.error;
    if (!inMeeting()) {
      bar.replaceChildren(el("span", { class: "mt-head" }, "Teams meetings"),
        el("span", { class: "muted mt-sub" }, st.available === false ? "Meetings open in the Teams app (desktop app only here)"
          : "Camera, microphone and screen sharing work only during a meeting here"),
        el("span", { class: "spacer" }),
        el("button", { type: "button", class: "small subtle", title: "Read the meetings again", onclick: () => loadMeetings(true) }, "↻ Refresh"));
      return;
    }
    bar.replaceChildren(...[
      el("span", { class: "mt-live", title: "A meeting is open here" }, "●"),
      el("span", { class: "pv-where", title: v.host ? `${v.host}${v.path || ""}` : "" },
        v.loading ? el("span", { class: "pv-loading", "aria-hidden": "true" }) : null,
        el("span", { class: "pv-title" }, v.title || "Joining…"),
        st.meeting?.at ? el("span", { class: "muted pv-host" }, `since ${timeOf(st.meeting.at * 1000)}`) : null),
      note ? el("span", { class: `pv-note${v.failed || st.error ? " err" : ""}` }, note) : null,
      el("button", { type: "button", class: "small subtle", title: "Reload the meeting page", "aria-label": "Reload", onclick: () => act("reload") }, "↻"),
      el("button", { type: "button", class: "small alt", title: "Leave here and carry on in the Teams app", onclick: toApp }, "Teams app"),
      el("button", { type: "button", class: "small danger", title: "Leave the meeting (camera and microphone off)", onclick: leave }, "Leave"),
    ].filter(Boolean));
  }

  // The list under the view: shown while no meeting is open (the view covers it during one).
  function renderIdle() {
    if (!idle) return;
    if (inMeeting()) { idle.replaceChildren(el("div", { class: "pv-wait" }, "MEETING")); return; }
    const now = new Date();
    const rows = (meetings.list || []).filter((m) => m.join_url && new Date(m.end) > now && !m.lunch)
      .sort((a, b) => new Date(a.start) - new Date(b.start)).slice(0, 12);
    const list = rows.length ? el("ul", { class: "mt-list" }, rows.map((m) => {
      const on = new Date(m.start) <= now, soon = !on && new Date(m.start) - now < 15 * 60000;
      const day = new Date(m.start).toDateString() === now.toDateString() ? "" : `${new Date(m.start).toLocaleDateString([], { weekday: "short" })} `;
      return el("li", { class: `mt-row ${on ? "on" : soon ? "soon" : ""}` },
        el("span", { class: "mt-when" }, on ? "Now" : `${day}${timeOf(m.start)}`),
        el("span", { class: "mt-what" }, el("span", { class: "mt-subject" }, m.subject || "(no subject)"),
          el("span", { class: "muted mt-times" }, `${timeOf(m.start)} – ${timeOf(m.end)}${m.location ? ` · ${m.location}` : ""}`)),
        el("button", { type: "button", class: `small ${on || soon ? "primary" : "alt"}`, title: "Join here, with your camera and microphone",
          "aria-label": `Join ${m.subject || "meeting"}`, onclick: () => join(m.join_url) }, "Join"));
    })) : el("p", { class: "muted" }, meetings.list === null ? "Reading your calendar…"
      : meetings.error ? `Couldn't read your calendar (${meetings.error}).` : "No Teams meetings in the next 24 hours.");

    const link = el("input", { type: "url", placeholder: "Paste a Teams meeting link", "aria-label": "Teams meeting link", spellcheck: "false", autocomplete: "off" });
    const go = () => { const v = link.value.trim(); if (v) join(v); };
    link.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } });

    const calls = CC.teamsCalls;
    idle.replaceChildren(el("div", { class: "mt-panel panel" },
      el("div", { class: "panel-label" }, "NEXT TEAMS MEETINGS"),
      list,
      el("div", { class: "mt-link" }, link, el("button", { type: "button", class: "small alt", onclick: go }, "Join link")),
      st.available && calls ? el("label", { class: "mt-switch", title: "Join buttons elsewhere in Command Center (header, Today, Calendar, alerts)" },
        el("input", { type: "checkbox", role: "switch", checked: calls.on, onchange: (e) => CC.setTeamsCalls(e.target.checked).then(renderIdle) }),
        "Join buttons everywhere open meetings here") : null,
      el("p", { class: "muted mt-help" }, st.available === false
        ? "Meetings join inside the Command Center window. Here, Join opens the Teams app."
        : "Teams asks for your camera and microphone when you join, and shows its own preview first. Screen sharing starts only from Teams' Share button, and Windows asks which screen or window. Lock, Restart and Close end the meeting.")));
  }

  // ---- for other modules -------------------------------------------------------------

  CC.meetingTab = {
    get available() { return !!st.available; },
    get inMeeting() { return inMeeting(); },
    join,
    reveal() { st.meeting = st.meeting || { at: Date.now() / 1000 }; chip.hidden = false; renderIdle(); CC.openTab("meeting"); startPoll(); setTimeout(loadStatus, 500); },
  };

  CC.registerTab({ id: "meeting", label: "Meeting", render, onShow, pane: "work" });
  if (section && CC.nativeStage) {
    nativeStage = CC.nativeStage({ view: "meeting", section, hole, available: () => !!st.available, shown: inMeeting });
  }
  document.querySelector("#search-btn")?.before(chip);
  CC.addPaletteSource(() => [
    { group: "Commands", label: "Meeting", hint: inMeeting() ? "back to the meeting" : "Teams meetings in Command Center", run: () => CC.openTab("meeting") },
    inMeeting() && { group: "Commands", label: "Leave the meeting", hint: "Meeting tab", run: leave },
  ].filter(Boolean));
  loadStatus();
})();
