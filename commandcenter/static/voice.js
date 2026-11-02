// Voice (voice.py), a test tab: talk to OpenAI's Realtime API about what's in Command Center. The server makes the
// call, so this page holds no key; audio and events then run over WebRTC to OpenAI. The Microphone switch really lets
// go of the microphone when it's off (Windows' in-use icon goes), and a session ends by itself after a quiet spell,
// when Windows locks, or with the page (Lock, reload, Close). The transcript lives here only.
(() => {
  const CC = window.CC;
  if (!CC) return;
  const { el, api, guarded, confirmDialog, toast } = CC;

  const MAX_ROWS = 200;
  const MAX_TOOLS_PER_TURN = 8;
  const CONNECT_MS = 15000;
  const GUARD_MS = 20000;
  const TOOL_LABEL = {
    get_schedule: "Schedule", get_next_meetings: "Next meetings", get_followups: "Follow-ups", get_tasks: "Tasks",
    get_teams_recent: "Teams", get_unread_alerts: "Alerts", get_unread_mail: "Unread mail", search_mail: "Mail search",
    read_email: "An email", get_pulse_tickets: "Pulse tickets", open_tab: "Opened a tab",
  };
  const TABS = ["today", "alerts", "mail", "calendar", "teams", "pulse-feedback"];

  let info = null;  // GET /api/voice
  let call = null;  // { pc, dc, sender, audio, guard, lastActive, turns, cost, usage rows }
  let track = null;  // the live microphone track, only while the switch is on
  let busy = false, wanted = false;  // busy: the switch is on its way to `wanted`

  const statusLine = el("p", { class: "vc-status", role: "status" }, "");
  const costLine = el("span", { class: "muted vc-cost" }, "");
  const micBox = el("input", { type: "checkbox", role: "switch" });
  const micSwitch = el("label", { class: "vc-mic", title: "Off: Command Center can't use the microphone at all" }, micBox, "Microphone");
  const endBtn = el("button", { type: "button", class: "small subtle", hidden: true, onclick: () => endAndRelease("Session ended.") }, "End session");
  const keyArea = el("div", { class: "vc-key" });
  const log = el("ol", { class: "vc-log", "aria-live": "polite" });
  const chip = el("button", { id: "voice-chip", type: "button", class: "small voice-chip", hidden: true, title: "Command Center is listening. Click to turn the microphone off", onclick: () => setMic(false) }, "🎙 Listening");

  const say = (text) => { statusLine.textContent = text; };

  function addRow(cls, who, text = "") {
    const body = el("span", { class: "vc-text" }, text);
    const row = el("li", { class: `vc-row ${cls}` }, el("span", { class: "vc-who" }, who), body);
    log.append(row);
    while (log.children.length > MAX_ROWS) log.firstChild.remove();
    log.parentElement?.scrollTo?.({ top: log.parentElement.scrollHeight });
    return body;
  }
  function rowFor(map, id, cls, who) {
    if (!map.has(id)) map.set(id, addRow(cls, who));
    return map.get(id);
  }

  function renderControls() {
    micBox.checked = busy ? wanted : !!track;
    micBox.disabled = busy || !info?.key_set;
    chip.hidden = !track;
    endBtn.hidden = !call;
    if (!info) return;
    if (!info.key_set) {
      const input = el("input", { type: "password", autocomplete: "off", spellcheck: "false", placeholder: "sk-…", "aria-label": "OpenAI API key" });
      const save = () => guarded(async () => {
        await api("/api/voice/key", { key: input.value });
        input.value = "";
        await load();
        toast("OpenAI key saved in Credential Manager");
      });
      keyArea.replaceChildren(
        el("p", { class: "muted" }, "Needs an OpenAI API key, from platform.openai.com > API keys (billed separately from ChatGPT). It's checked with OpenAI, then kept in Windows Credential Manager."),
        el("form", { class: "row", onsubmit: (e) => { e.preventDefault(); save(); } }, input, el("button", { type: "submit", class: "small" }, "Save key")));
    } else {
      keyArea.replaceChildren(
        el("span", { class: "muted" }, `OpenAI key saved · ${info.model} · voice ${info.voice}`),
        el("button", { type: "button", class: "small subtle", onclick: () => removeKey() }, "Remove key"));
    }
  }

  async function removeKey() {
    if (!await confirmDialog("Remove the OpenAI key?", "It's deleted from Credential Manager and any session ends.")) return;
    endAndRelease("Session ended.");
    guarded(async () => { await api("/api/voice/disconnect", {}); await load(); });
  }

  async function getTrack() {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
      return stream.getAudioTracks()[0];
    } catch (e) {
      throw new Error(e.name === "NotFoundError" ? "No microphone was found."
        : e.name === "NotAllowedError" ? "The microphone was refused. In Windows, check Settings > Privacy & security > Microphone > Let desktop apps access your microphone."
        : "The microphone couldn't be opened; another app may be holding it.");
    }
  }

  // The switch. On: starts a session, or carries on the open one. Off: lets go of the microphone; the session stays
  // open so the conversation can go on, until End session or the quiet timer.
  async function setMic(on) {
    if (busy || on === !!track) return renderControls();
    busy = true;
    wanted = on;
    renderControls();
    try {
      if (on) {
        await api("/api/voice/mic", { on: true });
        track = await getTrack();
        if (call) await call.sender.replaceTrack(track);
        else await startCall();
        say("Listening. Talk normally; it answers when you pause.");
      } else {
        await releaseMic();
        say(call ? "Microphone off. The session stays open until you end it or it goes quiet." : "Microphone off.");
      }
    } catch (e) {
      await releaseMic();
      endCall(e.message);
    }
    busy = false;
    renderControls();
  }

  async function releaseMic() {
    track?.stop();
    track = null;
    try { await call?.sender?.replaceTrack(null); } catch { /* the call is going anyway */ }
    await api("/api/voice/mic", { on: false }).catch(() => {});
  }

  async function startCall() {
    say("Connecting…");
    const pc = new RTCPeerConnection();
    const audio = new Audio();
    audio.autoplay = true;
    pc.ontrack = (e) => { audio.srcObject = e.streams[0]; };
    const sender = pc.addTrack(track);
    const dc = pc.createDataChannel("oai-events");
    call = { pc, dc, sender, audio, lastActive: Date.now(), toolsThisTurn: 0, cost: 0, users: new Map(), replies: new Map() };
    const mine = call;
    dc.addEventListener("message", (e) => { if (call === mine) onEvent(JSON.parse(e.data)); });
    dc.addEventListener("close", () => { if (call === mine) endAndRelease("The session ended on OpenAI's side."); });
    pc.addEventListener("connectionstatechange", () => {
      if (call === mine && pc.connectionState === "failed") endAndRelease("The connection to OpenAI dropped.");
    });
    await pc.setLocalDescription(await pc.createOffer());
    const { sdp } = await api("/api/voice/call", { sdp: pc.localDescription.sdp });
    await pc.setRemoteDescription({ type: "answer", sdp });
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("OpenAI accepted the call but the audio link didn't connect. A firewall or the VPN may be blocking WebRTC.")), CONNECT_MS);
      dc.addEventListener("open", () => { clearTimeout(timer); resolve(); }, { once: true });
    });
    mine.guard = setInterval(() => guard(mine), GUARD_MS);
  }

  async function guard(mine) {
    if (call !== mine) return;
    const s = await api("/api/voice").catch(() => null);
    if (call !== mine) return;
    if (s?.windows_locked) return endAndRelease("Session ended: Windows was locked.");
    if (Date.now() - mine.lastActive > (info?.idle_minutes || 5) * 60000) endAndRelease(`Session ended after ${info?.idle_minutes || 5} quiet minutes.`);
  }

  function endCall(message) {
    const c = call;
    call = null;
    if (c) {
      clearInterval(c.guard);
      try { c.dc.close(); } catch { /* already closed */ }
      c.pc.close();
      c.audio.srcObject = null;
    }
    if (message) say(message);
    renderControls();
  }
  function endAndRelease(message) {
    endCall(message);
    releaseMic().then(renderControls);
  }

  function addCost(usage) {
    const p = info?.prices;
    if (!p || !usage) return;
    const inp = usage.input_token_details || {}, out = usage.output_token_details || {}, cached = inp.cached_tokens_details || {};
    call.cost += (((inp.audio_tokens || 0) - (cached.audio_tokens || 0)) * p.audio_in + (cached.audio_tokens || 0) * p.audio_cached
      + ((inp.text_tokens || 0) - (cached.text_tokens || 0)) * p.text_in + (cached.text_tokens || 0) * p.text_cached
      + (out.audio_tokens || 0) * p.audio_out + (out.text_tokens || 0) * p.text_out) / 1e6;
    costLine.textContent = `About $${call.cost.toFixed(2)} this session`;
  }

  function onEvent(ev) {
    call.lastActive = Date.now();
    switch (ev.type) {
      case "input_audio_buffer.speech_started":
        call.toolsThisTurn = 0;
        rowFor(call.users, ev.item_id, "vc-you", "You");
        break;
      case "conversation.item.input_audio_transcription.delta":
        rowFor(call.users, ev.item_id, "vc-you", "You").textContent += ev.delta || "";
        break;
      case "conversation.item.input_audio_transcription.completed":
        rowFor(call.users, ev.item_id, "vc-you", "You").textContent = ev.transcript || "(not understood)";
        break;
      case "response.output_audio_transcript.delta":
      case "response.output_text.delta":
        rowFor(call.replies, ev.item_id, "vc-ai", "Voice").textContent += ev.delta || "";
        break;
      case "response.output_audio_transcript.done":
        rowFor(call.replies, ev.item_id, "vc-ai", "Voice").textContent = ev.transcript || "";
        break;
      case "response.done":
        addCost(ev.response?.usage);
        if (ev.response?.status === "failed") say(ev.response.status_details?.error?.message || "OpenAI couldn't answer that.");
        runTools((ev.response?.output || []).filter((o) => o.type === "function_call"));
        break;
      case "error":
        say(ev.error?.message || "OpenAI reported an error.");
        break;
    }
  }

  // The model's reads: each goes through the server's fixed list, then the answers go back and it carries on.
  async function runTools(calls) {
    if (!calls.length) return;
    const mine = call;
    for (const c of calls) {
      addRow("vc-tool", "Read", TOOL_LABEL[c.name] || c.name);
      const output = await runTool(c);
      if (call !== mine) return;
      mine.dc.send(JSON.stringify({ type: "conversation.item.create", item: { type: "function_call_output", call_id: c.call_id, output: JSON.stringify(output) } }));
    }
    if (call === mine) mine.dc.send(JSON.stringify({ type: "response.create" }));
  }

  async function runTool(c) {
    if (++call.toolsThisTurn > MAX_TOOLS_PER_TURN) return { error: "Too many reads for one question. Answer with what you have." };
    let args = {};
    try { args = JSON.parse(c.arguments || "{}"); } catch { return { error: "Bad arguments" }; }
    if (c.name === "open_tab") {
      if (!TABS.includes(args.tab)) return { error: "No such tab" };
      CC.openTab(args.tab);
      return { ok: true };
    }
    try { return (await api("/api/voice/tool", { name: c.name, args })).output; }
    catch { return { error: "That couldn't be read right now." }; }
  }

  async function load() {
    info = await api("/api/voice");
    if (info.mic && !track && !busy) {  // left on by a page that has gone (reload, Lock)
      info = { ...info, mic: false };
      await api("/api/voice/mic", { on: false }).catch(() => {});
    }
    renderControls();
  }

  function render(section) {
    section.append(el("div", { class: "vc-wrap" },
      el("h2", {}, "Voice ", el("span", { class: "tag" }, "test")),
      el("p", { class: "muted" }, "Ask about your schedule, follow-ups, tasks, mail, Teams, alerts and Pulse tickets. It can read those and open a tab, nothing else: it can't send, reply or change anything, and personal data is never offered."),
      el("div", { class: "vc-bar" }, micSwitch, endBtn, costLine),
      statusLine,
      keyArea,
      el("div", { class: "vc-scroll" }, log),
      el("p", { class: "muted vc-note" }, "While you use it, your voice and whatever it reads (mail, chats, tickets) go to OpenAI. The transcript is only on this page and goes with a reload. While the microphone is on, reminders don't jump to the front (only toast and sound), the same as during a call.")));
    micBox.addEventListener("change", () => setMic(micBox.checked));
    load().catch(() => {});
  }

  function mountChip() {
    document.querySelector("#search-btn")?.before(chip);
  }

  CC.registerTab({ id: "voice", label: "Voice", pane: "work", render, onShow: () => load().catch(() => {}) });
  CC.addPaletteSource(() => [
    { group: "Commands", label: "Voice", hint: "open the Voice tab", run: () => CC.openTab("voice") },
    ...(info?.key_set ? [{ group: "Commands", label: `Voice: microphone ${track ? "off" : "on"}`, hint: track ? "listening now" : "off", run: () => setMic(!track) }] : []),
  ]);
  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", mountChip, { once: true }) : mountChip();
})();
