// Mark as spam / block (spam.py). A "Spam" action on emails from outside Sonance (Mail rows and reader, Today
// follow-up cards, X in triage) opens a dialog: block the exact sender (default), the sender's whole domain, or a
// subject phrase. It shows how many Inbox emails already match (and a few of them) from POST /api/spam/preview, and
// moves those only if the box is ticked. "Mark as spam" is a deliberate click: Enter in the phrase box never confirms,
// Esc cancels. The server re-checks every guard and only moves the previewed emails.
// "Spam blocks" (Mail toolbar, Ctrl+K) lists the blocks with Unblock and the Outlook rule status (Retry).
// Subjects and addresses go in as text only (CC.el), never as HTML.
(() => {
  const CC = window.CC;
  if (!CC) return;
  const { el, api } = CC;

  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = "/static/spam.css";
  document.head.append(link);

  const KIND_LABEL = { sender: "Sender", domain: "Domain", subject: "Subject phrase" };
  const PHRASE_MIN = 6, PHRASE_MAX = 120;
  const info = { internal: ["sonance.com"] };

  async function loadInfo() {
    try {
      const r = await api("/api/spam/blocks");
      if (Array.isArray(r.internal_domains) && r.internal_domains.length) info.internal = r.internal_domains.map((d) => String(d).toLowerCase());
      return r;
    } catch { return null; }
  }
  loadInfo();

  // Only emails from outside Sonance (and not Teams' notification emails or urgent people). The server decides;
  // this just keeps the button off where it would always be refused.
  function eligible(m) {
    const sender = String(m?.sender || "").trim().toLowerCase();
    if (!m || !m.id || m.urgent || m.from_name || m.internal === true || m.folder === "Sent" || !sender.includes("@")) return false;
    const host = sender.split("@").pop();
    if (host === "teams.microsoft.com" || host.endsWith(".teams.microsoft.com")) return false;
    return !info.internal.some((d) => host === d || host.endsWith(`.${d}`));
  }

  CC.addMailAction((m, where) => {
    if (!eligible(m) || m.hoxhunt) return null;  // a Hoxhunt test is reported with Hoxhunt's button, not blocked
    return el("button", {
      type: "button", class: `small subtle spam-btn ${where === "row" ? "spam-row-btn" : ""}`,
      title: "Mark as spam: send mail like this to Junk Email", "aria-label": `Mark as spam: ${m.subject || "this email"}`,
      onclick: (e) => { e.stopPropagation(); open(m); },
    }, "Spam");
  });

  const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
  const fmt = (n) => Number(n || 0).toLocaleString();

  // ---- the dialog ----------------------------------------------------------------------------------
  async function open(m, opts = {}) {
    const d = CC.$("#dialog");
    const closeRow = () => el("div", { class: "row" }, el("button", { value: "cancel" }, "Close"));
    let finished = false;
    const onClose = () => { if (!finished) { finished = true; opts.onClose?.(); } };
    CC.showDialog(el("div", { class: "spam-dlg" }, el("h3", {}, "Mark as spam"), el("p", { class: "muted" }, "Checking this email…")));
    d.addEventListener("close", onClose, { once: true });
    let o;
    try {
      o = await api(`/api/spam/options?id=${encodeURIComponent(m.id)}`);
    } catch (e) {
      if (d.open && !finished) CC.showDialog(el("div", { class: "spam-dlg" }, el("h3", {}, "Mark as spam"), el("p", { class: "err" }, e.message), closeRow()));
      return;
    }
    if (!d.open || finished) return;

    const radios = {};
    const radio = (kind) => (radios[kind] = el("input", { type: "radio", name: "spam-kind", value: kind, id: `spam-k-${kind}`,
      disabled: !o.kinds?.[kind]?.ok, checked: kind === "sender" }));
    const phrase = el("input", { type: "text", class: "spam-phrase", maxlength: String(PHRASE_MAX), spellcheck: "false",
      "aria-label": "Subject phrase to block", autocomplete: "off" });
    phrase.value = o.phrase || "";
    const domainWhy = o.kinds?.domain?.ok ? null : el("div", { class: "spam-why", id: "spam-domain-why" }, o.kinds?.domain?.reason || "Not available");
    const kinds = el("fieldset", { class: "spam-kinds" },
      el("legend", { class: "field-label" }, "Block future mail by"),
      el("label", { class: "spam-kind", for: "spam-k-sender" }, radio("sender"),
        el("span", { class: "spam-kind-text" }, el("b", {}, "This sender"), el("span", { class: "spam-val" }, o.sender))),
      el("label", { class: `spam-kind ${domainWhy ? "off" : ""}`, for: "spam-k-domain" }, radio("domain"),
        el("span", { class: "spam-kind-text" }, el("b", {}, "The whole domain"), el("span", { class: "spam-val" }, `anything @${o.domain}`),
          domainWhy)),
      el("label", { class: "spam-kind", for: "spam-k-subject" }, radio("subject"),
        el("span", { class: "spam-kind-text" }, el("b", {}, "Subjects containing"), phrase)));
    if (domainWhy) radios.domain.setAttribute("aria-describedby", "spam-domain-why");
    for (const r of Object.values(radios)) r?.addEventListener("change", () => refresh(true));

    const count = el("div", { class: "spam-count" });
    const samples = el("ul", { class: "spam-samples" });
    const previewBox = el("div", { class: "spam-preview", "aria-live": "polite" }, count, samples);
    const move = el("input", { type: "checkbox", id: "spam-move", disabled: true });
    const moveText = el("span", {}, "Also move matching emails to Junk");
    const moveLabel = el("label", { class: "spam-move", for: "spam-move" }, move, moveText);
    const err = el("div", { class: "err", role: "alert" });
    const confirmBtn = el("button", { type: "button", class: "primary spam-confirm", disabled: true, onclick: () => confirm() }, "Mark as spam");

    CC.showDialog(el("div", { class: "spam-dlg" },
      el("h3", {}, "Mark as spam"),
      el("div", { class: "spam-mail" },
        el("div", { class: "spam-from" }, el("span", { class: "muted" }, "From "), o.sender_name && o.sender_name !== o.sender ? `${o.sender_name} <${o.sender}>` : o.sender),
        el("div", { class: "spam-subj" }, o.subject || "(no subject)")),
      kinds, previewBox, moveLabel,
      el("p", { class: "muted spam-note" }, "Future mail like this goes to Junk Email (Outlook rule) — you can unblock anytime."),
      err,
      el("div", { class: "row" }, el("button", { value: "cancel" }, "Cancel ", el("kbd", {}, "Esc")), confirmBtn)));
    radios.sender.focus();

    let seq = 0, timer = null, current = null, busy = false;
    const kind = () => Object.entries(radios).find(([, r]) => r?.checked)?.[0] || "sender";

    function setPreviewEmpty(text, cls = "muted") {
      current = null;
      confirmBtn.disabled = true;
      move.checked = false;
      move.disabled = true;
      moveText.textContent = "Also move matching emails to Junk";
      count.className = `spam-count ${cls}`;
      count.textContent = text;
      samples.replaceChildren();
    }

    function refresh(now) {
      clearTimeout(timer);
      const k = kind();
      seq++;
      if (k === "subject" && phrase.value.trim().length < PHRASE_MIN) {
        setPreviewEmpty(`Type a phrase of at least ${PHRASE_MIN} characters.`);
        return;
      }
      setPreviewEmpty("Counting matching emails in your Inbox…");
      const my = seq;
      const go = async () => {
        try {
          const p = await api("/api/spam/preview", { id: m.id, kind: k, value: k === "subject" ? phrase.value : "" });
          if (my !== seq || !d.open || finished) return;
          show(p);
        } catch (e) {
          if (my !== seq || !d.open || finished) return;
          setPreviewEmpty(e.message, "err");
        }
      };
      now ? go() : (timer = setTimeout(go, 450));
    }

    function show(p) {
      current = { ...p, at: Date.now() };
      const n = p.match_count;
      const what = { sender: "from this sender", domain: "from this domain", subject: "with this phrase in the subject" }[p.kind];
      count.className = "spam-count";
      count.replaceChildren(...[  // (replaceChildren would print a bare null)
        n ? el("span", {}, el("b", {}, fmt(n)), ` other ${n === 1 ? "email" : "emails"} in your Inbox ${what}.`)
          : el("span", {}, `No other emails in your Inbox ${what}. Only this one moves to Junk now.`),
        p.exists && el("span", { class: "tag" }, "Already blocked")].filter(Boolean));
      samples.replaceChildren(...(p.sample || []).map((s) => el("li", {},
        el("span", { class: "spam-s-subj" }, s.subject), el("span", { class: "spam-s-from muted" }, s.sender),
        el("span", { class: "spam-s-when muted" }, CC.when(s.received)))));
      if (n > (p.sample || []).length) samples.append(el("li", { class: "spam-more muted" }, `…and ${fmt(n - p.sample.length)} more`));
      move.disabled = !p.movable;
      move.checked = false;
      moveText.textContent = !p.movable ? "Also move matching emails to Junk (none)"
        : p.movable < n ? `Also move the newest ${fmt(p.movable)} of the ${fmt(n)} matching emails to Junk`
          : `Also move ${plural(n, "matching email")} to Junk`;
      confirmBtn.disabled = false;
    }

    async function confirm() {
      if (!current || busy) return;
      if (Date.now() - current.at > ((current.expires_in || 300) - 15) * 1000) {
        err.textContent = "The preview expired. Check the matches again, then confirm.";
        refresh(true);
        return;
      }
      busy = true;
      err.textContent = "";
      confirmBtn.disabled = true;
      confirmBtn.textContent = "Saving the Outlook rule…";
      try {
        const r = await api("/api/spam/block", { preview_id: current.preview_id, move_existing: move.checked && !move.disabled });
        finished = true;
        if (d.open) d.close();
        done(m, r, opts);
        opts.onClose?.();
      } catch (e) {
        busy = false;
        confirmBtn.textContent = "Mark as spam";
        refresh(true);  // the preview was used up: count again for the next try
        err.textContent = e.message;
      }
    }

    phrase.addEventListener("focus", () => { if (!radios.subject.checked) { radios.subject.checked = true; refresh(true); } });
    phrase.addEventListener("input", () => { radios.subject.checked = true; refresh(false); });
    phrase.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" || e.isComposing) return;
      e.preventDefault();  // never confirms (the dialog would press its primary button): just count now
      refresh(true);
    });
    refresh(true);
  }

  // ---- after a block: refresh the views (Mail, Today, the alert feed and badge), toast with Undo -----
  function done(m, r, opts) {
    CC.forgetMail?.(m.id);
    CC.loaders.mail?.();
    CC.loaders.today?.();
    CC.refreshAlerts?.();
    opts.onBlocked?.(r);
    const moved = (r.moved || 0) + (r.trigger_moved ? 1 : 0);
    const parts = [`Blocked ${r.block.label}`, `moved ${moved} to Junk`];
    if (r.alerts_removed) parts.push(`${plural(r.alerts_removed, "alert")} removed`);
    if (r.rule && r.rule.ok === false) parts.push("Outlook rule not saved (see Spam blocks)");
    if (r.move_error) parts.push(r.move_error);
    toast(parts.join(" · "), r.block.id, (r.undo_seconds || 30) * 1000 - 2000);
  }

  let toastTimer = null;
  function toast(text, undoId = null, ms = 6000) {
    let box = document.getElementById("spam-toast");
    if (!box) { box = el("div", { id: "spam-toast", role: "status", "aria-live": "polite" }); document.body.append(box); }
    const hide = () => { box.classList.remove("show"); clearTimeout(toastTimer); };
    const undoBtn = undoId && el("button", { type: "button", class: "small alt", onclick: () => undo(undoId, undoBtn) }, "Undo");
    box.replaceChildren(...[el("span", { class: "spam-toast-text" }, text), undoBtn,
      el("button", { type: "button", class: "small subtle", "aria-label": "Dismiss", onclick: hide }, "✕")].filter(Boolean));
    box.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(hide, Math.max(3000, ms));
  }

  async function undo(blockId, btn) {
    if (btn) btn.disabled = true;
    try {
      const r = await api("/api/spam/undo", { block_id: blockId });
      toast(`Undone${r.unblocked ? ": unblocked" : ""} · ${plural(r.moved_back, "email")} back in the Inbox${r.error ? ` · ${r.error}` : ""}`);
      CC.loaders.mail?.();
      CC.loaders.today?.();
    } catch (e) { toast(e.message); }
  }

  // ---- the manage list -------------------------------------------------------------------------------
  function ruleLine(rule, rerender) {
    if (!rule || rule.ok === null || rule.ok === undefined) {
      return el("div", { class: "spam-rule muted" }, "Outlook rules: none yet.");
    }
    if (rule.ok) {
      return el("div", { class: "spam-rule good" }, `Outlook rules: synced${rule.checked ? ` · ${CC.when(rule.checked)}` : ""}`,
        rule.local_only ? el("span", { class: "muted" }, " · runs only while Outlook is open on this PC") : null);
    }
    const retry = el("button", { type: "button", class: "small alt", onclick: async () => {
      retry.disabled = true;
      retry.textContent = "Saving…";
      try { await api("/api/spam/sync", {}); } catch (e) { /* the status line shows the reason */ }
      rerender();
    } }, "Retry");
    return el("div", { class: "spam-rule bad", role: "status" },
      el("span", {}, el("b", {}, "Outlook rule not saved: "), rule.error || "unknown reason"),
      el("span", { class: "muted" }, " Command Center still hides this mail and moves new matches to Junk while it runs."), retry);
  }

  async function manage() {
    const d = CC.$("#dialog");
    if (!d.open) CC.showDialog(el("div", { class: "spam-dlg" }, el("h3", {}, "Spam blocks"), el("p", { class: "muted" }, "Loading…")));
    const r = await loadInfo();
    if (!r) { CC.showDialog(el("div", { class: "spam-dlg" }, el("h3", {}, "Spam blocks"), el("p", { class: "err" }, "Couldn't load the spam blocks."), el("div", { class: "row" }, el("button", {}, "Close")))); return; }
    const blocks = r.blocks || [];
    const unblock = async (b) => {
      const ok = await CC.confirmDialog("Unblock", `Unblock ${b.label}? New mail like this will stay in your Inbox again. Mail already in Junk Email stays there.`, "Unblock");
      if (ok) {
        try { await api("/api/spam/unblock", { id: b.id }); } catch (e) { CC.showMessage("Unblock", e.message); return; }
        CC.loaders.mail?.();
        CC.loaders.today?.();
      }
      manage();
    };
    CC.showDialog(el("div", { class: "spam-dlg spam-manage" },
      el("h3", {}, "Spam blocks"),
      ruleLine(r.rule, manage),
      el("p", { class: "muted" }, `Blocked mail goes to Junk Email through Outlook rules named “${(r.rule_names || []).join("” and “")}”, `
        + "so it works with Command Center closed and on your phone. Unblocking doesn't move mail back."),
      blocks.length
        ? el("ul", { class: "list spam-list" }, blocks.map((b) => el("li", {},
            el("span", { class: `tag spam-kind-tag k-${b.kind}` }, KIND_LABEL[b.kind] || b.kind),
            el("div", { class: "grow" }, el("div", { class: "title spam-list-val" }, b.kind === "domain" ? `@${b.value}` : b.value),
              el("div", { class: "muted" }, `Blocked ${CC.when(b.created)}`)),
            el("button", { type: "button", class: "small subtle", "aria-label": `Unblock ${b.label}`, onclick: () => unblock(b) }, "Unblock"))))
        : el("p", { class: "muted" }, "Nothing blocked. Use Spam on an email from outside Sonance to block it."),
      el("div", { class: "row" }, el("button", {}, "Close"))));
  }

  function mountButton() {
    const bar = document.querySelector("#tab-mail .toolbar");
    if (!bar || bar.querySelector(".spam-manage-btn")) return;
    const btn = el("button", { type: "button", class: "small subtle spam-manage-btn", title: "Blocked senders, domains and subject phrases", onclick: manage }, "Spam blocks");
    const refreshBtn = bar.querySelector("#mail-refresh");
    refreshBtn ? bar.insertBefore(btn, refreshBtn) : bar.append(btn);
  }

  CC.addPaletteSource(() => [{ group: "Commands", label: "Spam blocks", hint: "blocked senders, domains and subject phrases · unblock", run: manage }]);
  CC.spam = { eligible, open, manage };
  function start() {
    mountButton();
    // Today / the Mail list can render before this script runs (their first fetch may finish between script
    // loads): draw them once more so their Spam buttons appear.
    if (CC.todayData) CC.loaders.today?.();
    if (document.querySelector("#mail-list li[data-id]")) CC.loaders.mail?.();
  }
  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", start, { once: true }) : start();
})();
