// Important senders (people.py, GET/POST /api/people/important). "Mark important" on an email (Mail rows and reader,
// a follow-up row's ⋯ menu) adds this sender, or everyone @their domain, to the list; "Remove from important" takes
// the matching entries off again. New mail from them raises a high-priority alert, their unread mail is listed under
// Follow-ups > Important senders, and their rows show a ★ (CC.importantStar, drawn by app.js from the server's
// `important` flag). Whole domains are never offered for internal senders or shared providers (gmail.com...).
// "Important senders" (Follow-ups > Manage, Ctrl+K) manages the list; config.yaml's "VIP sender" rule entries are
// shown read-only. Addresses go in as text only (CC.el), never as HTML.
(() => {
  const CC = window.CC;
  if (!CC) return;
  const { el, api } = CC;

  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = "/static/important.css";
  document.head.append(link);

  // The lists as the server last sent them (null until the first load). Lowercase.
  let info = null;
  const lower = (list) => (Array.isArray(list) ? list : []).map((p) => String(p).trim().toLowerCase()).filter(Boolean);
  function take(r) {
    info = { mine: lower(r.senders), config: lower(r.from_config), internal: lower(r.internal_domains),
      shared: lower(r.shared_providers), max: r.max || 200 };
    return info;
  }
  async function load() {
    try { return take(await api("/api/people/important")); } catch { return info; }
  }
  load();

  // Same semantics as people.sender_matches: an address entry equals the sender, an "@domain" entry ends it.
  const matches = (addr, entry) => (entry.startsWith("@") ? addr.endsWith(entry) : addr === entry);
  const inDomain = (host, d) => host === d || host.endsWith(`.${d}`);
  const addressOf = (m) => String(m?.sender || "").trim().toLowerCase();

  // Only received email with a real sender address (not Teams' notification emails, not sent mail).
  function eligible(m) {
    const addr = addressOf(m);
    if (!m || !m.id || m.from_name || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(addr)) return false;
    const host = addr.split("@").pop();
    return !["teams.microsoft.com", "teams.mail.microsoft"].some((d) => inDomain(host, d));  // Teams' notification emails
  }

  function state(m) {
    const addr = addressOf(m);
    if (!info) return { important: !!m.important, mine: [], config: [] };
    const mine = info.mine.filter((p) => matches(addr, p)), config = info.config.filter((p) => matches(addr, p));
    return { important: mine.length > 0 || config.length > 0, mine, config };
  }

  // What "Mark important" can offer: the address always; the whole domain only when it isn't internal or shared.
  function choices(m) {
    const addr = addressOf(m), host = addr.split("@").pop();
    const out = [{ value: addr, label: "This sender", detail: addr }];
    const internal = (info?.internal?.length ? info.internal : ["sonance.com"]).some((d) => inDomain(host, d));
    const shared = (info?.shared || []).some((d) => inDomain(host, d));
    if (!internal && !shared) out.push({ value: `@${host}`, label: `Everyone @${host}`, detail: `any address ending in @${host}` });
    return out;
  }

  function after(m, where, r, text) {
    take(r);
    CC.toast?.(text);
    CC.loaders.mail?.();
    CC.loaders.today?.();
    if (where === "reader") CC.showMail(m.id);
  }

  async function save(body) {
    return api("/api/people/important", body);
  }

  async function add(m, where, choice) {
    try {
      const r = await save({ add: choice.value });
      after(m, where, r, `${choice.value} is an important sender now.`);
    } catch (e) { CC.showMessage("Mark important", e.message); }
  }

  async function remove(m, where, entries) {
    try {
      const r = await save({ remove: entries });
      const still = state(m).config;  // config.yaml may list them too
      after(m, where, r, still.length ? `Removed ${entries.join(", ")}. Still important through config.yaml.` : `Removed ${entries.join(", ")} from important senders.`);
    } catch (e) { CC.showMessage("Remove from important", e.message); }
  }

  const who = (m) => (m.sender_name && m.sender_name.toLowerCase() !== addressOf(m) ? `${m.sender_name} <${addressOf(m)}>` : addressOf(m));
  const choiceButton = (c, onclick) => el("button", { type: "button", class: "imp-choice", onclick },
    el("b", {}, c.label), el("span", { class: "imp-val" }, c.detail));

  // One click when there's only one thing to do; a small choice dialog otherwise.
  async function toggle(m, where) {
    await load();  // fresh: the list may have changed since this row was drawn
    const s = state(m);
    if (s.important) {
      if (!s.mine.length) {
        CC.showMessage("Important senders", `${addressOf(m)} is important through config.yaml (${s.config.join(", ")} in the "VIP sender" rule under outlook.rules). Remove it there.`);
        return;
      }
      if (s.mine.length === 1) { await remove(m, where, s.mine); return; }
      CC.showDialog(el("div", { class: "imp-dlg" },
        el("h3", {}, "Remove from important"),
        el("p", { class: "muted" }, `${who(m)} is on the list more than once. Remove:`),
        ...s.mine.map((p) => choiceButton({ label: p.startsWith("@") ? `Everyone ${p}` : "This sender", detail: p },
          () => { CC.$("#dialog").close(); remove(m, where, [p]); })),
        choiceButton({ label: "Both", detail: s.mine.join(" and ") }, () => { CC.$("#dialog").close(); remove(m, where, s.mine); }),
        el("div", { class: "row" }, el("button", { value: "cancel" }, "Cancel"))));
      return;
    }
    const c = choices(m);
    if (c.length === 1) { await add(m, where, c[0]); return; }
    CC.showDialog(el("div", { class: "imp-dlg" },
      el("h3", {}, "Mark important"),
      el("p", { class: "muted" }, `From ${who(m)}. New mail from them raises a high-priority alert, and unread mail is listed under Follow-ups > Important senders.`),
      ...c.map((x) => choiceButton(x, () => { CC.$("#dialog").close(); add(m, where, x); })),
      el("div", { class: "row" }, el("button", { value: "cancel" }, "Cancel"))));
    CC.$("#dialog .imp-choice")?.focus();
  }

  CC.addMailAction((m, where) => {
    if (!eligible(m)) return null;
    const on = state(m).important;
    const onclick = (e) => { e.stopPropagation(); toggle(m, where); };
    if (where === "row") {
      return el("button", { type: "button", class: `small subtle imp-row-btn${on ? " on" : ""}`, onclick,
        title: on ? "Important sender: click to remove" : "Mark this sender important",
        "aria-label": `${on ? "Remove from important senders" : "Mark sender important"}: ${m.sender_name || addressOf(m)}` }, on ? "★" : "☆");
    }
    if (where === "reader") {
      return el("button", { type: "button", class: `imp-reader-btn${on ? " on" : ""}`, onclick,
        title: on ? "Remove this sender from your important senders" : "New mail from this sender raises a high-priority alert" },
        on ? "★ Important" : "☆ Mark important");
    }
    return el("button", { type: "button", onclick, title: on ? "Remove from your important senders" : "High-priority alerts for this sender's mail" },
      on ? "Remove from important" : "Mark sender important");
  });

  // ---- manage the list ------------------------------------------------------------------------------
  async function manage() {
    const r = await load();
    if (!r) { CC.showMessage("Important senders", "Couldn't load the list."); return; }
    const mine = r.mine.slice(), fixed = r.config;
    const list = el("ul", { class: "list dialog-list" });
    const err = el("div", { class: "err", role: "alert" });
    const input = el("input", { placeholder: "name@company.com or @company.com", maxlength: "254", spellcheck: "false",
      "aria-label": "Add an address or @domain", onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); addTyped(); } } });
    const draw = () => list.replaceChildren(
      ...fixed.map((p) => el("li", {}, el("div", { class: "grow mono" }, p),
        el("span", { class: "tag locked", title: "Listed in config.yaml (outlook.rules, the \"VIP sender\" rule). Edit it there." }, "config.yaml"))),
      ...mine.map((p, i) => el("li", {}, el("div", { class: "grow mono" }, p),
        el("button", { type: "button", class: "small subtle", "aria-label": `Remove ${p}`, onclick: () => { mine.splice(i, 1); draw(); } }, "Remove"))),
      ...(!fixed.length && !mine.length ? [el("li", { class: "empty" }, "No one yet. Use Mark important on an email, or add an address here.")] : []));
    const addTyped = () => {
      const v = input.value.trim().toLowerCase().replace(/^mailto:/, "");
      err.textContent = "";
      if (!v) return;
      if (v.length > 254 || !/^([^@\s<>"'(),;:]+)?@[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$/.test(v)) {
        err.textContent = "Use an email address (name@company.com) or a whole domain (@company.com).";
        return;
      }
      if (v.startsWith("@")) {
        const host = v.slice(1);
        if (r.internal.some((d) => inDomain(host, d))) { err.textContent = `${v} is an internal domain. Add the person's address instead.`; return; }
        if (r.shared.some((d) => inDomain(host, d))) { err.textContent = `${v} is a shared mail provider. Add the person's address instead.`; return; }
      }
      if (![...fixed, ...mine].includes(v)) mine.push(v);
      input.value = "";
      draw();
      input.focus();
    };
    draw();
    CC.showDialog(el("h3", {}, "Important senders"),
      el("p", { class: "muted" }, "New mail from these senders raises a high-priority alert (it pops up even in focus mode), unread mail is listed under Follow-ups > Important senders, and their mail shows a ★. An address, or @company.com for everyone there (not for internal domains or shared providers such as gmail.com)."),
      list,
      el("div", { class: "inline-field" }, input, el("button", { type: "button", onclick: addTyped }, "+ Add")),
      err,
      fixed.length ? el("p", { class: "muted" }, "Entries marked config.yaml come from the \"VIP sender\" rule under outlook.rules and can't be removed here; edit config.yaml.") : null,
      el("div", { class: "row" }, el("button", { value: "cancel", class: "subtle" }, "Cancel"),
        el("button", { type: "button", class: "primary", onclick: async () => {
          if (input.value.trim()) addTyped();
          if (err.textContent) return;
          try {
            take(await save({ senders: mine }));
          } catch (e) { err.textContent = e.message; return; }
          CC.$("#dialog").close();
          CC.toast?.("Important senders saved.");
          CC.loaders.mail?.();
          CC.loaders.today?.();
        } }, "Save")));
    input.focus();
  }

  function mountButton() {
    const urgent = CC.$("#urgent-btn");
    if (!urgent || CC.$("#important-btn")) return;
    urgent.after(el("button", { type: "button", id: "important-btn", class: "small subtle",
      title: "Senders whose new mail is a high-priority alert", onclick: manage }, "Important senders"));
  }

  CC.addPaletteSource(() => [{ group: "Commands", label: "Important senders", hint: "high-priority senders · mark or remove", run: manage }]);
  CC.important = { eligible, toggle, manage, isImportant: (m) => state(m).important };
  function start() {
    mountButton();
    // Today / the Mail list may have rendered before this script ran: draw them again for the Mark important actions.
    if (CC.todayData) CC.loaders.today?.();
    if (document.querySelector("#mail-list li[data-id]")) CC.loaders.mail?.();
  }
  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", start, { once: true }) : start();
})();
