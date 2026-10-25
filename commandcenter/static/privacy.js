// Privacy & Data: the personal vault (set up / unlock / lock / change passphrase / reset), Privacy mode, and
// the data inventory panel. Personal data (personal calendar entries, overlay calendars) is shown only while
// the vault is unlocked and Privacy mode is off; the server enforces that, this file only reflects it.
//
// API for other modules:
//   CC.privacy.state                 {set_up, unlocked, privacy, available, retry_after, legacy}
//   CC.privacy.isPrivate()           Privacy mode is on (hide every personal control)
//   CC.privacy.ensureUnlocked(why)   Promise<boolean>: asks to set up / unlock the vault when needed
//   CC.privacy.open()                the Privacy & Data panel
//   document event "cc:privacy"      {detail: state} whenever the state changes
// Passphrases are typed only into this file's own <dialog> (no <form>, autocomplete off), sent once in a POST
// body, and cleared from the inputs as soon as the dialog closes. Nothing here is logged or stored.
(() => {
  const CC = window.CC;
  if (!CC) return;
  const { el, api, guarded } = CC;

  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = "/static/privacy.css";
  document.head.append(link);

  let state = { set_up: false, unlocked: false, privacy: false, available: false, retry_after: 0, legacy: {} };
  let known = false;

  // ---- server calls (with the HTTP status, for 401 / 429) ----------------------------------------
  async function post(path, body) {
    const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-CommandCenter": "1" },
      body: JSON.stringify(body || {}) });
    const data = await res.json().catch(() => ({}));
    return { ok: res.ok, status: res.status, data, message: data.error || data.detail || res.statusText };
  }

  function setState(next) {
    const before = JSON.stringify([state.available, state.privacy, state.unlocked, state.set_up]);
    state = { ...state, ...next };
    document.documentElement.classList.toggle("cc-private", !!state.privacy);
    document.documentElement.classList.toggle("cc-personal-off", !state.available);
    renderEye();
    if (!known || before !== JSON.stringify([state.available, state.privacy, state.unlocked, state.set_up])) {
      known = true;
      document.dispatchEvent(new CustomEvent("cc:privacy", { detail: { ...state } }));
    }
  }

  async function refresh() {
    try { setState(await api("/api/vault/status")); } catch { /* keep the last state */ }
    return state;
  }

  // ---- header: Privacy mode eye --------------------------------------------------------------------
  const SVG = "http://www.w3.org/2000/svg";
  function svgIcon(off) {
    const s = document.createElementNS(SVG, "svg");
    s.setAttribute("viewBox", "0 0 24 24");
    s.setAttribute("aria-hidden", "true");
    const add = (tag, attrs) => { const n = document.createElementNS(SVG, tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); s.append(n); };
    add("path", { d: "M1.5 12S5.5 5 12 5s10.5 7 10.5 7-4 7-10.5 7S1.5 12 1.5 12z" });
    add("circle", { cx: "12", cy: "12", r: "3.2" });
    if (off) add("path", { d: "M3.5 3.5l17 17", class: "pv-slash" });
    return s;
  }
  const eye = el("button", { type: "button", class: "pv-eye", "aria-pressed": "false", onclick: () => togglePrivacy() });

  function renderEye() {
    const on = !!state.privacy;
    eye.replaceChildren(svgIcon(on));
    eye.classList.toggle("on", on);
    eye.setAttribute("aria-pressed", String(on));
    const label = on ? "Privacy mode is on: personal items hidden. Click to show them (Ctrl+Shift+P)"
      : "Privacy mode: hide all personal items and lock the personal vault (Ctrl+Shift+P)";
    eye.title = label;
    eye.setAttribute("aria-label", on ? "Privacy mode on. Turn off" : "Turn on Privacy mode");
  }

  function placeEye() {
    const slot = document.querySelector("#privacy-slot");
    if (slot && !eye.isConnected) slot.append(eye);
  }

  async function setPrivacy(on) {
    if (on) {
      setState({ privacy: true, unlocked: false, available: false });  // hide at once; the server follows
      const r = await post("/api/privacy/mode", { on: true });
      if (r.ok) setState(r.data.vault);
      toast("Privacy mode on: personal items hidden, vault locked");
      return;
    }
    const r = await post("/api/privacy/mode", { on: false });
    if (!r.ok) throw new Error(r.message);
    setState(r.data.vault);
    if (state.set_up && !state.unlocked) await unlockDialog("Privacy mode is off. Unlock to show your personal calendars.");
  }
  const togglePrivacy = () => guarded(() => setPrivacy(!state.privacy));

  document.addEventListener("keydown", (e) => {
    if (!(e.ctrlKey && e.shiftKey && !e.altKey && !e.metaKey && e.key.toLowerCase() === "p")) return;
    const t = e.target;
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;  // typing
    e.preventDefault();
    togglePrivacy();
  });

  function toast(text) {
    let box = document.querySelector("#toast");
    if (!box) { box = el("div", { id: "toast", role: "status", "aria-live": "polite" }); document.body.append(box); }
    box.textContent = text;
    box.classList.add("show");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => box.classList.remove("show"), 3500);
  }

  // ---- the vault dialog (its own <dialog>, never inside a <form>) ------------------------------------
  let dlg = null, settle = null;
  function vaultDialog(...content) {
    if (!dlg) {
      dlg = el("dialog", { class: "pv-dialog", "aria-labelledby": "pv-title" });
      dlg.addEventListener("close", () => {
        dlg.querySelectorAll("input").forEach((i) => { i.value = ""; });  // drop typed passphrases
        const s = settle; settle = null; s?.(false);
      });
      document.body.append(dlg);
    }
    settle?.(false);
    dlg.replaceChildren(...content.flat().filter(Boolean));
    if (!dlg.open) dlg.showModal();
    return new Promise((resolve) => { settle = resolve; });
  }
  function finish(result) {
    const s = settle; settle = null;
    s?.(result);
    dlg?.close();
  }
  const secretInput = (id, placeholder, auto = "off") => el("input", { id, type: "password", placeholder, autocomplete: auto,
    spellcheck: "false", autocapitalize: "off", maxlength: "256", "data-lpignore": "true", "data-form-type": "other" });
  const onEnter = (input, fn) => input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); fn(); } });
  const errBox = () => el("p", { class: "err pv-err", role: "alert" });

  function countdown(btn, seconds, label) {
    clearInterval(btn._timer);
    let left = seconds;
    const tick = () => {
      if (left <= 0) { clearInterval(btn._timer); btn.disabled = false; btn.textContent = label; return; }
      btn.disabled = true; btn.textContent = `Wait ${left}s`; left -= 1;
    };
    tick();
    btn._timer = setInterval(tick, 1000);
  }

  function waitingText() {
    const l = state.legacy || {};
    const parts = [l["planner entries"] ? `${l["planner entries"]} personal entr${l["planner entries"] === 1 ? "y" : "ies"}` : "",
      l["overlay calendars"] ? `${l["overlay calendars"]} overlay calendar${l["overlay calendars"] === 1 ? "" : "s"}` : ""].filter(Boolean);
    return parts.length ? `${parts.join(" and ")} from before will move into the vault, and their old copies (Windows DPAPI / Credential Manager) will be deleted.` : "";
  }

  function movedText(moved) {
    const m = moved || {};
    const parts = [m["planner entries"] ? `${m["planner entries"]} entr${m["planner entries"] === 1 ? "y" : "ies"}` : "",
      m["overlay calendars"] ? `${m["overlay calendars"]} calendar${m["overlay calendars"] === 1 ? "" : "s"}` : ""].filter(Boolean);
    return parts.length ? ` Moved ${parts.join(" and ")} into the vault and deleted the old copies.` : "";
  }

  function unlockDialog(reason) {
    if (!state.set_up) return setupDialog();
    const pass = secretInput("pv-pass", "Your personal passphrase");
    const err = errBox();
    const go = el("button", { type: "button", class: "primary", onclick: () => submit() }, "Unlock");
    async function submit() {
      if (go.disabled) return;
      err.textContent = "";
      if (!pass.value) { err.textContent = "Enter your passphrase."; pass.focus(); return; }
      go.disabled = true; go.textContent = "Checking…";
      const r = await post("/api/vault/unlock", { passphrase: pass.value });
      pass.value = "";
      if (r.ok) {
        setState(r.data.status);
        finish(true);
        toast(`Personal vault unlocked.${movedText(r.data.moved)}`);
        return;
      }
      err.textContent = r.message || "Couldn't unlock.";
      go.disabled = false; go.textContent = "Unlock";
      const wait = /(\d+) seconds/.exec(r.message || "");
      if ((r.status === 429 || r.status === 401) && wait) countdown(go, Number(wait[1]), "Unlock");
      pass.focus();
    }
    onEnter(pass, submit);
    const p = vaultDialog(
      el("h3", { id: "pv-title" }, "Unlock personal vault"),
      el("p", { class: "muted" }, reason || "Your personal calendars are locked."),
      state.privacy ? el("p", { class: "pv-note" }, "Privacy mode is on. Unlocking turns it off.") : null,
      waitingText() ? el("p", { class: "pv-note" }, waitingText()) : null,
      el("label", { class: "field-label", for: "pv-pass" }, "Passphrase"), pass, err,
      el("div", { class: "row" },
        el("button", { type: "button", class: "subtle pv-link", onclick: () => { dlg.close(); resetDialog(); } }, "Forgot it?"),
        el("span", { class: "spacer" }),
        el("button", { type: "button", class: "subtle", onclick: () => finish(false) }, "Cancel"), go));
    if (state.retry_after > 0) countdown(go, state.retry_after, "Unlock");
    pass.focus();
    return p;
  }

  function setupDialog() {
    if (state.set_up) return unlockDialog();
    const pass = secretInput("pv-new", `At least ${state.min_length || 12} characters`, "new-password");
    const again = secretInput("pv-again", "Type it again", "new-password");
    const ack = el("input", { type: "checkbox", id: "pv-ack" });
    const hint = el("div", { class: "pv-hint muted", "aria-live": "polite" });
    const err = errBox();
    const go = el("button", { type: "button", class: "primary", onclick: () => submit() }, "Create vault");
    const rate = () => {
      const v = pass.value, n = v.length, min = state.min_length || 12;
      const classes = [/[a-z]/, /[A-Z]/, /\d/, /[^A-Za-z0-9]/].filter((re) => re.test(v)).length;
      hint.textContent = !n ? "Tip: a short sentence only you would think of (16+ characters) is strong and easy to remember."
        : n < min ? `${min - n} more character${min - n === 1 ? "" : "s"}`
        : n < 16 && classes < 3 ? "Under 16 characters: mix upper case, lower case, numbers or symbols, or make it longer"
        : "Length looks good. The server also checks for common words and patterns.";
      hint.classList.toggle("good", n >= min && (n >= 16 || classes >= 3));
    };
    pass.addEventListener("input", rate);
    rate();
    async function submit() {
      if (go.disabled) return;
      err.textContent = "";
      if (pass.value !== again.value) { err.textContent = "The two passphrases don't match."; again.focus(); return; }
      if (!ack.checked) { err.textContent = "Tick the box to confirm there's no way to recover a forgotten passphrase."; return; }
      go.disabled = true; go.textContent = "Creating…";
      const r = await post("/api/vault/setup", { passphrase: pass.value, confirm: again.value });
      if (r.ok) {
        pass.value = ""; again.value = "";
        setState(r.data.status);
        finish(true);
        toast(`Personal vault ready and unlocked.${movedText(r.data.moved)}`);
        return;
      }
      go.disabled = false; go.textContent = "Create vault";
      err.textContent = r.message || "Couldn't create the vault.";
      pass.focus();
    }
    onEnter(again, submit);
    onEnter(pass, () => again.focus());
    const p = vaultDialog(
      el("h3", { id: "pv-title" }, "Set up your personal vault"),
      el("p", {}, "Personal calendar entries and your Google / iCloud calendar links are encrypted with a passphrase only you know. ",
        "It isn't saved anywhere: not in a file, not in Windows Credential Manager, not with your company account. ",
        "Work data stays protected by Windows as before."),
      el("ul", { class: "pv-rules" },
        el("li", {}, el("b", {}, "Don't reuse "), "your Windows / Sonance password or any work password."),
        el("li", {}, el("b", {}, "No recovery: "), "if you forget it, the only option is to delete your personal data and start over."),
        el("li", {}, "Locks itself when you lock Command Center or Windows, turn on Privacy mode, or restart.")),
      waitingText() ? el("p", { class: "pv-note" }, waitingText()) : null,
      el("label", { class: "field-label", for: "pv-new" }, "Passphrase"), pass, hint,
      el("label", { class: "field-label", for: "pv-again" }, "Confirm passphrase"), again,
      el("label", { class: "pv-check" }, ack, "I understand a forgotten passphrase can't be recovered."),
      err,
      el("div", { class: "row" }, el("button", { type: "button", class: "subtle", onclick: () => finish(false) }, "Cancel"), go));
    pass.focus();
    return p;
  }

  function changeDialog() {
    const cur = secretInput("pv-cur", "Current passphrase");
    const pass = secretInput("pv-new2", `New passphrase (at least ${state.min_length || 12} characters)`, "new-password");
    const again = secretInput("pv-again2", "Type the new one again", "new-password");
    const err = errBox();
    const go = el("button", { type: "button", class: "primary", onclick: () => submit() }, "Change passphrase");
    async function submit() {
      if (go.disabled) return;
      err.textContent = "";
      if (pass.value !== again.value) { err.textContent = "The two new passphrases don't match."; again.focus(); return; }
      go.disabled = true; go.textContent = "Re-encrypting…";
      const r = await post("/api/vault/change", { current: cur.value, passphrase: pass.value, confirm: again.value });
      if (r.ok) {
        setState(r.data.status);
        finish(true);
        toast(`Passphrase changed. ${r.data.reencrypted} record(s) re-encrypted with the new key.`);
        return;
      }
      go.disabled = false; go.textContent = "Change passphrase";
      err.textContent = r.message || "Couldn't change it.";
      const wait = /(\d+) seconds/.exec(r.message || "");
      if (wait) countdown(go, Number(wait[1]), "Change passphrase");
    }
    onEnter(again, submit);
    const p = vaultDialog(
      el("h3", { id: "pv-title" }, "Change vault passphrase"),
      el("p", { class: "muted" }, "Everything in the vault is re-encrypted with a key from the new passphrase. Don't reuse a work password."),
      el("label", { class: "field-label", for: "pv-cur" }, "Current passphrase"), cur,
      el("label", { class: "field-label", for: "pv-new2" }, "New passphrase"), pass,
      el("label", { class: "field-label", for: "pv-again2" }, "Confirm new passphrase"), again, err,
      el("div", { class: "row" }, el("button", { type: "button", class: "subtle", onclick: () => finish(false) }, "Cancel"), go));
    cur.focus();
    return p;
  }

  const RESET_PHRASE = "DELETE PERSONAL DATA";
  function resetDialog() {
    const typed = el("input", { id: "pv-reset", type: "text", autocomplete: "off", spellcheck: "false", placeholder: RESET_PHRASE });
    const err = errBox();
    const go = el("button", { type: "button", class: "danger-solid", disabled: true, onclick: () => submit() }, "Delete all personal data");
    typed.addEventListener("input", () => { go.disabled = typed.value.trim().toUpperCase() !== RESET_PHRASE; });
    async function submit() {
      if (go.disabled) return;
      go.disabled = true;
      const r = await post("/api/vault/reset", { confirm: typed.value });
      if (r.ok) {
        setState(r.data.status);
        finish(true);
        toast(`Personal data deleted (${r.data.deleted} record(s)). Set up a new vault any time.`);
        return;
      }
      go.disabled = false;
      err.textContent = r.message;
    }
    const p = vaultDialog(
      el("h3", { id: "pv-title", class: "pv-danger" }, "Delete all personal data"),
      el("p", {}, "This permanently deletes every personal calendar entry, every overlay calendar link, the vault and its passphrase check, ",
        "plus any older personal copies (DPAPI / Credential Manager). Work data isn't touched. It can't be undone."),
      el("p", { class: "muted" }, "Use this if you forgot your passphrase: nothing (including this app) can decrypt the vault without it."),
      el("label", { class: "field-label", for: "pv-reset" }, `Type ${RESET_PHRASE} to confirm`), typed, err,
      el("div", { class: "row" }, el("button", { type: "button", class: "subtle", onclick: () => finish(false) }, "Cancel"), go));
    typed.focus();
    return p;
  }

  async function ensureUnlocked(reason) {
    await refresh();
    if (state.available) return true;
    const ok = state.set_up ? await unlockDialog(reason) : await setupDialog();
    await refresh();
    return ok && state.available;
  }

  async function lockVault() {
    const r = await post("/api/vault/lock", {});
    if (r.ok) setState(r.data.status);
    toast("Personal vault locked");
  }

  // ---- Privacy & Data panel ------------------------------------------------------------------------
  const DETAIL_LABEL = { full: "Full details (title, time, place)", titles: "Titles only (title + time)", busy: "Busy only (no title)" };
  const ACTIONS = {
    asana: { disconnect: { label: "Disconnect", path: "/api/asana/disconnect", text: "Disconnect Asana? The token is deleted from Credential Manager." } },
    pulse: { disconnect: { label: "Disconnect", path: "/api/pulse/disconnect", text: "Disconnect Pulse? The database password is deleted from Credential Manager." } },
    toolkit: { forget: { label: "Forget password", path: "/api/toolkit/forget", text: "Forget the Toolkit SQL password? It's deleted from Credential Manager." } },
  };
  let panelOpen = false;

  function vaultCard(v) {
    const status = !v.set_up ? "Not set up" : v.privacy ? "Privacy mode on (locked)" : v.unlocked ? "Unlocked" : "Locked";
    const act = (label, fn, cls = "small") => el("button", { type: "button", class: cls, onclick: () => guarded(async () => { await fn(); openPanel(); }) }, label);
    return el("section", { class: "pv-vault" },
      el("div", { class: "pv-vault-head" },
        el("span", { class: `pv-lock ${v.available ? "open" : ""}`, "aria-hidden": "true" }, v.available ? "🔓" : "🔒"),
        el("div", { class: "grow" },
          el("div", { class: "pv-vault-title" }, "Personal vault", el("span", { class: `tag ${v.available ? "pv-ok" : "locked"}` }, status)),
          el("div", { class: "muted" }, "AES-256-GCM, key derived (scrypt) from a passphrase only you know. Not stored anywhere; your company can't recover it."))),
      el("div", { class: "row pv-vault-acts" },
        !v.set_up && act("Set up vault", setupDialog, "small primary"),
        v.set_up && !v.unlocked && act("Unlock", () => unlockDialog(), "small primary"),
        v.unlocked && act("Lock now", lockVault),
        v.set_up && v.unlocked && act("Change passphrase", changeDialog),
        el("button", { type: "button", class: `small ${v.privacy ? "primary" : "alt"}`, "aria-pressed": String(!!v.privacy),
          onclick: () => guarded(async () => { await setPrivacy(!state.privacy); openPanel(); }) },
          v.privacy ? "Privacy mode: ON" : "Privacy mode: off"),
        act("Delete all personal data", resetDialog, "small danger")));
  }

  function calendarRows(cals) {
    if (!cals?.length) return null;
    return el("ul", { class: "pv-cals" }, cals.map((c) => {
      const sel = el("select", { "aria-label": `Detail level for ${c.name}` },
        Object.entries(DETAIL_LABEL).map(([k, label]) => el("option", { value: k }, label)));
      sel.value = c.detail;
      sel.addEventListener("change", () => guarded(async () => {
        await api("/api/feeds/update", { id: c.id, detail: sel.value });
        toast(`${c.name}: ${DETAIL_LABEL[sel.value]}`);
        CC.loaders.calendar && document.querySelector("#tab-calendar.active") && CC.loaders.calendar();
      }));
      const sw = el("i", { class: "pv-sw" });
      sw.style.background = c.color;
      return el("li", {}, sw, el("span", { class: "grow" }, c.name, el("span", { class: "tag" }, c.kind)), sel,
        el("button", { type: "button", class: "small subtle", onclick: () => guarded(async () => {
          if (!await CC.confirmDialog("Remove calendar", `Remove "${c.name}" and delete its link from the vault? (Nothing changes in the calendar itself.)`, "Remove")) return openPanel();
          await api("/api/feeds/delete", { id: c.id });
          openPanel();
        }) }, "Remove"));
    }));
  }

  function itemCard(it) {
    const acts = (it.actions || []).map((a) => {
      const spec = ACTIONS[it.id]?.[a];
      if (!spec) return null;
      return el("button", { type: "button", class: "small subtle", onclick: () => guarded(async () => {
        if (!await CC.confirmDialog(spec.label, spec.text, spec.label)) return openPanel();
        await api(spec.path, {});
        toast(`${it.name}: done`);
        openPanel();
      }) }, spec.label);
    }).filter(Boolean);
    if (it.id === "overlay-calendars" && state.available) acts.push(el("button", { type: "button", class: "small alt", onclick: () => { document.querySelector("#dialog").close(); CC.openTab("calendar"); CC.openCalendars?.(); } }, "Add / manage"));
    const dot = it.connected === true ? "on" : it.connected === false ? "off" : "na";
    return el("article", { class: `pv-item pv-${it.category.toLowerCase()}` },
      el("header", {},
        el("span", { class: `pv-dot ${dot}`, title: it.connected === true ? "Connected / in use" : it.connected === false ? "Not connected" : "" }),
        el("span", { class: "pv-name" }, it.name),
        el("span", { class: `pv-cat ${it.category.toLowerCase()}` }, it.category),
        el("span", { class: "pv-state muted" }, it.state || "")),
      el("dl", {},
        el("dt", {}, "Reads"), el("dd", {}, it.accesses),
        el("dt", {}, "Kept on this PC"), el("dd", {}, it.stored),
        el("dt", {}, "Protection"), el("dd", {}, it.encryption),
        el("dt", {}, "Kept for"), el("dd", {}, it.retention)),
      it.id === "overlay-calendars" ? calendarRows(it.calendars) : null,
      acts.length ? el("div", { class: "pv-acts" }, acts) : null);
  }

  async function openPanel() {
    const data = await api("/api/privacy/inventory");
    setState(data.vault);
    const work = data.items.filter((i) => i.category === "Work");
    const personal = data.items.filter((i) => i.category === "Personal");
    panelOpen = true;
    CC.wideDialog(
      el("h3", {}, "Privacy & Data"),
      el("p", { class: "muted pv-intro" }, "Everything Command Center reads and keeps, on this PC only. Work data is protected by your Windows account (DPAPI). ",
        "Personal data is in your personal vault, which only your own passphrase opens. Privacy mode (eye icon, Ctrl+Shift+P) hides all personal items at once."),
      vaultCard(data.vault),
      el("div", { class: "panel-label gap" }, `Personal (${personal.length})`),
      el("div", { class: "pv-grid" }, personal.map(itemCard)),
      el("div", { class: "panel-label gap" }, `Work (${work.length})`),
      el("div", { class: "pv-grid" }, work.map(itemCard)),
      el("p", { class: "muted pv-foot" }, `Vault auto-lock: when Command Center locks${data.settings.lock_on_windows_lock ? ", when Windows locks" : ""}`,
        data.settings.auto_lock_minutes ? `, ${data.settings.auto_lock_minutes} min after unlocking` : "", ", on restart and in Privacy mode. Settings: privacy: in config.yaml."),
      el("div", { class: "row" }, el("button", { value: "cancel", class: "subtle" }, "Close")));
  }
  document.querySelector("#dialog")?.addEventListener("close", () => { panelOpen = false; });

  // A "Privacy & Data" link inside the Profile panel (profile.js builds it; we add the link when it appears).
  const dialogBody = document.querySelector("#dialog-body");
  if (dialogBody) {
    new MutationObserver(() => {
      const grid = dialogBody.querySelector(".pf-grid");
      if (!grid || dialogBody.querySelector(".pv-profile-link")) return;
      grid.after(el("div", { class: "pv-profile-link" },
        el("button", { type: "button", class: "small alt", onclick: () => guarded(openPanel) }, "Privacy & Data…"),
        el("span", { class: "muted" }, "Data sources, personal vault, Privacy mode")));
    }).observe(dialogBody, { childList: true });
  }

  CC.addPaletteSource(() => [
    { group: "Commands", label: "Privacy & Data", hint: "data sources, personal vault", run: () => guarded(openPanel) },
    { group: "Commands", label: state.privacy ? "Privacy mode: turn off" : "Privacy mode: turn on", hint: "Ctrl+Shift+P", run: togglePrivacy },
    state.unlocked ? { group: "Commands", label: "Lock personal vault", hint: "", run: () => guarded(lockVault) }
      : { group: "Commands", label: state.set_up ? "Unlock personal vault" : "Set up personal vault", hint: "personal calendars", run: () => ensureUnlocked() },
  ]);

  CC.privacy = {
    get state() { return { ...state }; },
    isPrivate: () => !!state.privacy,
    available: () => !!state.available,
    ensureUnlocked, refresh, open: () => guarded(openPanel), lock: lockVault, setPrivacy,
  };

  // The server can lock on its own (Command Center / Windows lock, auto-lock): check now and then.
  setInterval(() => { if (!document.hidden) refresh(); }, 15000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  window.addEventListener("focus", () => refresh());
  // Panel open while the state changes elsewhere: keep it current.
  document.addEventListener("cc:privacy", () => { if (panelOpen && document.querySelector("#dialog")?.open && !dlg?.open) guarded(openPanel); });

  const start = () => { placeEye(); renderEye(); refresh(); };
  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", start, { once: true }) : start();
})();
