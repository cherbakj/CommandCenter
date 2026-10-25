// Quick capture: a small box for "get this out of my head" notes. Type, pick Task (Outlook / To Do) or
// Personal (a private calendar note on this PC), Enter to save. Dates typed at the start or end of the text
// ("call Ben fri", "tomorrow: pick up parts", "renew badge 10/30") become the due date / the day.
// Opens from the global hotkey (desktop.py, ui.capture_hotkey, default Ctrl+Alt+Space) and the palette.
// Everything is inserted as text; nothing typed here is logged.
(() => {
  const CC = window.CC;
  if (!CC) return;
  const { el, api } = CC;

  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = "/static/capture.css";
  document.head.append(link);

  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage blocked */ } },
  };
  const pad = (n) => String(n).padStart(2, "0");
  const isoDay = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const dayLabel = (d) => d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });

  function toast(text) {
    let box = document.querySelector("#toast");
    if (!box) { box = el("div", { id: "toast", role: "status", "aria-live": "polite" }); document.body.append(box); }
    box.textContent = text;
    box.classList.add("show");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => box.classList.remove("show"), 3500);
  }

  // ---- date parsing ------------------------------------------------------------------------------
  const WEEKDAYS = [["sun", "sunday"], ["mon", "monday"], ["tue", "tues", "tuesday"], ["wed", "weds", "wednesday"],
    ["thu", "thur", "thurs", "thursday"], ["fri", "friday"], ["sat", "saturday"]];
  const MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"];
  const dayOf = (word) => WEEKDAYS.findIndex((names) => names.includes(word));
  // "oct", "oct.", "octob", "october", "sept" -> a month index; anything else -> -1
  const monthOf = (word) => {
    const w = word.replace(/\.$/, "");
    return w.length >= 3 ? MONTHS.findIndex((m) => m.startsWith(w)) : -1;
  };
  const addDays = (base, n) => new Date(base.getFullYear(), base.getMonth(), base.getDate() + n);
  const validYmd = (y, m, d) => { const t = new Date(y, m, d); return t.getFullYear() === y && t.getMonth() === m && t.getDate() === d ? t : null; };

  // One date phrase -> Date (local midnight) or null. `strict` (used at the start of the text) leaves out
  // the short weekday names, which are often ordinary words there ("sat down…", "sun shade…").
  function phraseDate(p, now, strict) {
    const today = addDays(now, 0);
    let m;
    if (/^(today|tonight|tdy)$/.test(p)) return today;
    if (/^(tomorrow|tmrw|tmr|tomorow)$/.test(p)) return addDays(today, 1);
    if (p === "next week") return addDays(today, ((8 - today.getDay()) % 7) || 7);
    if ((m = /^in (\d{1,3}) (day|days|week|weeks)$/.exec(p))) return addDays(today, Number(m[1]) * (m[2].startsWith("week") ? 7 : 1));
    if ((m = /^(next |this )?([a-z]+)$/.exec(p))) {
      const wd = dayOf(m[2]);
      if (wd >= 0 && !(strict && !m[1] && m[2].length <= 4)) {
        let ahead = (wd - today.getDay() + 7) % 7 || 7;  // the coming one, never today
        if (m[1] === "next ") {
          // "next fri" said early in the week means the week after; said from Fri-Sun it's the coming one.
          const daysToSunday = (7 - today.getDay()) % 7;
          if (ahead <= daysToSunday) ahead += 7;
        }
        return addDays(today, ahead);
      }
    }
    if ((m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(p))) return validYmd(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
    if ((m = /^(\d{1,2})\/(\d{1,2})(?:\/(\d{2}|\d{4}))?$/.exec(p))) {
      const mo = Number(m[1]) - 1, d = Number(m[2]);
      if (m[3]) return validYmd(m[3].length === 2 ? 2000 + Number(m[3]) : Number(m[3]), mo, d);
      const t = validYmd(today.getFullYear(), mo, d);
      if (!t) return validYmd(today.getFullYear() + 1, mo, d);  // e.g. 2/29 in a non-leap year
      return t < addDays(today, -1) ? validYmd(today.getFullYear() + 1, mo, d) : t;  // "1/5" in October = next January
    }
    if ((m = /^([a-z]{3,9}\.?) (\d{1,2})(?:st|nd|rd|th)?$/.exec(p)) || (m = /^(\d{1,2})(?:st|nd|rd|th)? ([a-z]{3,9}\.?)$/.exec(p))) {
      const [word, num] = /^\d/.test(m[1]) ? [m[2], m[1]] : [m[1], m[2]];
      const mo = monthOf(word);
      if (mo < 0) return null;
      const t = validYmd(today.getFullYear(), mo, Number(num));
      return t && t < addDays(today, -1) ? validYmd(today.getFullYear() + 1, mo, Number(num)) : t;
    }
    return null;
  }

  // "call Ben fri" -> { title: "call Ben", date: <Fri> }. Only a phrase at the very start or end counts,
  // optionally with "on / by / due / for" in front, so dates inside a sentence stay part of the text.
  function parseQuick(text, now = new Date()) {
    const raw = String(text || "").replace(/\s+/g, " ").trim();
    const words = raw.split(" ");
    const lower = words.map((w) => w.toLowerCase().replace(/[,;]$/, ""));
    for (let n = Math.min(3, words.length); n >= 1; n--) {  // longest phrase first ("next fri", "in 3 days")
      const tail = lower.slice(-n).join(" ");
      const d = phraseDate(tail, now, false);
      if (d) {
        let rest = words.slice(0, -n);
        if (rest.length && /^(on|by|due|for|before)$/i.test(rest[rest.length - 1])) rest = rest.slice(0, -1);
        const title = rest.join(" ").replace(/[\s,;:–—-]+$/, "");
        if (title) return { title, date: d };
      }
      const head = lower.slice(0, n).join(" ").replace(/:$/, "");
      const d2 = phraseDate(head.replace(/^(on|by|due) /, ""), now, true);
      if (d2 && words.length > n) {
        const title = words.slice(n).join(" ").replace(/^[\s,;:–—-]+/, "");
        if (title) return { title, date: d2 };
      }
    }
    return { title: raw, date: null };
  }

  // ---- the box ----------------------------------------------------------------------------------
  const MODES = {
    task: { label: "Task", where: "Outlook / To Do", save: "Add task" },
    personal: { label: "Personal", where: "Calendar note · personal vault", save: "Add note" },
  };
  let dlg = null, input, chip, err, saveBtn, modeBtns = {}, restore = null, busy = false;
  let mode = store.get("cc.capture.mode") === "personal" ? "personal" : "task";

  function build() {
    input = el("input", { class: "qc-input", type: "text", maxlength: "200", autocomplete: "off", spellcheck: "true",
      placeholder: "Capture a task or note…  (try “call Ben fri”)", "aria-label": "What to capture",
      oninput: preview, onkeydown: onKey });
    chip = el("span", { class: "qc-when", "aria-live": "polite" });
    err = el("div", { class: "err qc-err", role: "alert" });
    for (const id of Object.keys(MODES)) {
      modeBtns[id] = el("button", { type: "button", class: "qc-mode", "data-mode": id, "aria-pressed": "false",
        onclick: () => { setMode(id); input.focus(); } },
        el("b", {}, MODES[id].label), el("span", {}, MODES[id].where));
    }
    saveBtn = el("button", { type: "button", class: "primary", onclick: save }, "Add task");
    dlg = el("dialog", { class: "qc", "aria-label": "Quick capture" },
      el("div", { class: "qc-head" },
        el("span", { class: "panel-label" }, "QUICK CAPTURE"),
        el("span", { class: "qc-keys muted" }, el("kbd", {}, "Enter"), " save ", el("kbd", {}, "Tab"), " switch ", el("kbd", {}, "Esc"), " close")),
      input,
      el("div", { class: "qc-row" },
        el("div", { class: "qc-modes", role: "group", "aria-label": "Save as" }, Object.values(modeBtns)),
        chip),
      err,
      el("div", { class: "qc-foot" },
        el("button", { type: "button", class: "subtle", onclick: () => close(true) }, "Cancel"),
        saveBtn));
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); close(true); });
    dlg.addEventListener("close", () => { busy = false; });
    document.body.append(dlg);
  }

  function setMode(id) {
    if (id === "personal" && CC.privacy?.isPrivate()) id = "task";  // Privacy mode: no personal option
    mode = id;
    store.set("cc.capture.mode", id);
    for (const [k, b] of Object.entries(modeBtns)) {
      b.classList.toggle("on", k === id);
      b.setAttribute("aria-pressed", String(k === id));
    }
    saveBtn.textContent = MODES[id].save;
    dlg.dataset.mode = id;
    preview();
  }

  function preview() {
    const { date } = parseQuick(input.value);
    if (mode === "task") chip.textContent = date ? `Due ${dayLabel(date)}` : "No due date";
    else chip.textContent = `On ${dayLabel(date || new Date())}${date ? "" : " (today)"}`;
    chip.classList.toggle("set", !!date);
  }

  function onKey(e) {
    if (e.isComposing) return;
    if (e.key === "Enter") { e.preventDefault(); save(); }
    else if (e.key === "Tab") { e.preventDefault(); setMode(mode === "task" ? "personal" : "task"); }
  }

  async function save() {
    if (busy) return;
    const { title, date } = parseQuick(input.value);
    err.textContent = "";
    if (!title) { input.focus(); err.textContent = "Type what you want to capture."; return; }
    busy = true;
    saveBtn.disabled = true;
    try {
      if (mode === "task") {
        await api("/api/tasks", { source: "outlook", subject: title, due: date ? isoDay(date) : null, notes: "" });
      } else {
        // Personal notes live in the personal vault: it must be unlocked (and Privacy mode off) first.
        if (CC.privacy?.isPrivate()) throw new Error("Privacy mode is on, so personal notes are off. Save it as a task, or turn Privacy mode off (Ctrl+Shift+P).");
        if (CC.privacy && !CC.privacy.available() && !await CC.privacy.ensureUnlocked("Unlock your personal vault to save this note.")) {
          throw new Error("Personal notes need your personal vault unlocked.");
        }
        await api("/api/planner/personal", { day: isoDay(date || new Date()), title, notes: "", all_day: true });
      }
      dlg.classList.add("saved");
      const msg = mode === "task" ? `Task added${date ? ` · due ${dayLabel(date)}` : ""}` : `Personal note on ${dayLabel(date || new Date())}`;
      setTimeout(() => {
        close(false);
        toast(msg);
        refreshViews();
      }, 450);
    } catch (e) {
      err.textContent = e.message || "Couldn't save it.";
      busy = false;
      saveBtn.disabled = false;
      input.focus();
    }
  }

  function refreshViews() {
    for (const id of ["today", "calendar"]) {
      if (document.querySelector(`#tab-${id}.active`)) CC.loaders[id]?.();
    }
  }

  // Back where the window was before the hotkey (hidden to the tray / minimized).
  function close(cancelled) {
    if (!dlg?.open) return;
    dlg.close();
    const back = restore;
    restore = null;
    if (back) setTimeout(() => api(`/api/window/${back}`, {}).catch(() => {}), cancelled ? 0 : 200);
  }

  // opts.restore: "hide" | "minimize" (only from the desktop hotkey) to put the window back afterwards.
  function quickCapture(opts = {}) {
    if (!dlg) build();
    const r = opts && typeof opts === "object" ? opts.restore : null;
    if (!dlg.open) {
      restore = r === "hide" || r === "minimize" ? r : null;
      input.value = "";
      err.textContent = "";
      busy = false;
      saveBtn.disabled = false;
      dlg.classList.remove("saved");
      setMode(mode);
      document.querySelector("#palette.open") && document.querySelector("#palette").classList.remove("open");
      dlg.showModal();
    }
    input.focus();
    input.select();
  }

  CC.quickCapture = quickCapture;
  CC.parseQuickDate = parseQuick;
  CC.addPaletteSource(() => [{ group: "Commands", label: "Quick capture", hint: "task or personal calendar note", run: () => quickCapture() }]);
})();
