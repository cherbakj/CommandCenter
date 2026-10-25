// Toolkit tab: the former "Cherbak's Toolkit" (EDI ship mapping, SPS carrier lookup, Logi users,
// customer tree). It edits LIVE production SQL data, so every insert/update/delete is shown as a
// before -> after confirmation first, and the server re-checks everything (whitelist, single row,
// row unchanged since loaded, transaction, change log). DOM is built only with CC.el (text nodes).
(function () {
  "use strict";
  if (!window.CC) return;
  const { el, api } = CC;

  const css = document.createElement("link");
  css.rel = "stylesheet";
  css.href = "/static/toolkit.css";
  document.head.append(css);

  const VIEWS = [["shipping", "Shipping"], ["logi", "Logi users"], ["customers", "Customers"], ["changes", "Change log"], ["connection", "Connection"]];
  const SHIP_PARENT_COLS = ["ACCOUNT_NAME", "PARTNER_ID", "ACCOUNT_NUMBER"];
  const SHIP_CHILD_COLS = ["INBOUND_ID", "CARRIER_ID", "SHIP_VIA", "FREIGHT_TERMS"];
  const LOGI_USER_COLS = ["UserID", "Password", "EmailID", "FullName"];
  const DASH_EDIT_COLS = ["OPRID", "TREE_NAME", "TREE_NODE"];

  const state = {
    view: null, meta: null,
    accounts: null, accountFilter: "", selected: null, methods: {},
    logi: null, logiFilter: "",
    tree: null, treeSel: null,
    changesTable: "",
  };
  const views = {};
  let bar, statusChip;

  // ---- small helpers -------------------------------------------------------------------

  const nice = (c) => String(c).replace(/_/g, " ");
  const show = (v) => (v === null || v === undefined || v === "" ? "—" : String(v));
  const norm = (v) => (v === null || v === undefined ? null : String(v));
  const pick = (row, cols) => Object.fromEntries(cols.filter((c) => row && c in row).map((c) => [c, row[c]]));
  const tableMeta = (key) => (state.meta && state.meta.tables[key]) || { label: key, pk: [], columns: [], constants: {} };
  const spinner = (text = "Loading…") => el("div", { class: "tk-note" }, el("span", { class: "tk-spin" }), text);
  const errorBox = (e) => {
    const msg = e && e.message ? e.message : String(e);
    const box = el("div", { class: "tk-error" }, msg);
    if (/not configured|No SQL password|Connection/i.test(msg)) {
      box.append(" ", el("button", { type: "button", class: "small alt", onclick: () => setView("connection") }, "Set up connection"));
    }
    return box;
  };
  const masked = (col, v) => (col === "Password" && v ? "••••••••" : show(v));

  // New Logi passwords: 16 characters from an alphabet without look-alikes (no I/l/1, O/0), with at least one
  // upper-case letter, lower-case letter, digit and symbol. crypto.getRandomValues with rejection sampling
  // (no modulo bias), then a Fisher-Yates shuffle so the guaranteed characters aren't always first.
  const PW_SETS = ["ABCDEFGHJKLMNPQRSTUVWXYZ", "abcdefghijkmnpqrstuvwxyz", "23456789", "!#$%*+-=?@_"];
  function randBelow(n) {
    const limit = Math.floor(0x100000000 / n) * n;
    const a = new Uint32Array(1);
    do { crypto.getRandomValues(a); } while (a[0] >= limit);
    return a[0] % n;
  }
  function newPassword(length = 16) {
    const all = PW_SETS.join("");
    const chars = PW_SETS.map((set) => set[randBelow(set.length)]);
    while (chars.length < length) chars.push(all[randBelow(all.length)]);
    for (let i = chars.length - 1; i > 0; i--) { const j = randBelow(i + 1); [chars[i], chars[j]] = [chars[j], chars[i]]; }
    return chars.join("");
  }

  let toastTimer;
  function toast(text, bad = false) {
    let t = document.querySelector(".tk-toast");
    if (!t) { t = el("div", { class: "tk-toast" }); document.body.append(t); }
    t.textContent = text;
    t.classList.toggle("bad", bad);
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("show"), 3500);
  }

  async function copyText(text, label) {
    const str = text == null ? "" : String(text);
    if (!str.trim()) { toast("Nothing to copy", true); return; }
    try {
      await navigator.clipboard.writeText(str);
    } catch {
      const ta = el("textarea", { readonly: true, style: "position:fixed;left:-9999px;top:0" });
      ta.value = str;
      document.body.append(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      if (!ok) { toast("Copy blocked by the browser", true); return; }
    }
    toast(label ? `${label} copied` : "Copied");
  }

  function table(headers, rows, opts = {}) {
    return el("div", { class: "table-wrap tk-wrap" + (opts.cls ? " " + opts.cls : "") },
      el("table", { class: "tk-table" },
        el("thead", {}, el("tr", {}, headers.map((h) => el("th", h.cls ? { class: h.cls } : {}, h.label ?? h)))),
        el("tbody", {}, rows)));
  }

  // ---- stacked dialogs (own <dialog> elements so a confirm can sit on top of a form) -----

  function modal(build, opts = {}) {
    return new Promise((resolve) => {
      const d = el("dialog", { class: "tk-dialog" + (opts.wide ? " tk-wide" : "") + (opts.cls ? " " + opts.cls : "") });
      let result = null;
      const close = (value) => { result = value; d.close(); };
      const form = el("form", { method: "dialog", onsubmit: (e) => e.preventDefault() });
      form.append(...[].concat(build(close)));
      d.append(form);
      document.body.append(d);
      d.addEventListener("close", () => { d.remove(); resolve(result); }, { once: true });
      d.showModal();
      const first = form.querySelector("input:not([disabled]):not([type=checkbox]), select");
      if (first && opts.focus !== false) first.focus();
    });
  }

  const message = (title, text, bad = false) => modal((close) => [
    el("h3", {}, title), el("p", { class: bad ? "err" : "" }, text),
    el("div", { class: "row" }, el("button", { type: "button", class: "primary", onclick: () => close(true) }, "OK")),
  ]);

  const ask = (title, text, okLabel = "OK", danger = false) => modal((close) => [
    el("h3", {}, title), el("p", {}, text),
    el("div", { class: "row" },
      el("button", { type: "button", class: "subtle", onclick: () => close(false) }, "Cancel"),
      el("button", { type: "button", class: danger ? "tk-danger" : "primary", onclick: () => close(true) }, okLabel)),
  ]);

  // Confirmation of one or more changes: before -> after diff, full row for deletes.
  function confirmChanges(title, changes) {
    const deletes = changes.some((c) => c.action === "delete");
    const db = new Set(changes.map((c) => (tableMeta(c.table).name || "").split("].[")[0].replace("[", "")));
    return modal((close) => [
      el("h3", {}, title),
      el("p", { class: "tk-warn" },
        `LIVE PRODUCTION DATA (${[...db].join(", ")}). `,
        changes.length > 1
          ? `${changes.length} changes run in one transaction: if any one fails, none of them are applied.`
          : "This changes exactly one row; the server refuses it if the row changed since you loaded it."),
      el("div", { class: "tk-diffs" }, changes.map((c, i) => diffBlock(c, changes.length > 1 ? i + 1 : null))),
      el("div", { class: "row" },
        el("button", { type: "button", class: "subtle", onclick: () => close(false) }, "Back"),
        el("button", { type: "button", class: deletes ? "tk-danger" : "primary", onclick: () => close(true) },
          deletes ? (changes.length > 1 ? `Delete (${changes.length})` : "Delete row") : changes.length > 1 ? `Apply ${changes.length} changes` : "Apply change")),
    ], { wide: true });
  }

  function diffBlock(c, n) {
    const meta = tableMeta(c.table);
    const keyText = c.pk ? Object.entries(c.pk).map(([k, v]) => `${k} = ${show(v)}`).join(", ") : "new row";
    const head = el("div", { class: "tk-diff-head" },
      n ? el("span", { class: "chip" }, `#${n}`) : null,
      el("span", { class: `tag tk-act-${c.action}` }, c.action),
      el("b", {}, meta.label), el("span", { class: "muted mono" }, meta.name || ""),
      el("span", { class: "spacer" }), el("span", { class: "mono" }, keyText));
    let rows;
    if (c.action === "update") {
      const cols = [...new Set([...Object.keys(c.before), ...Object.keys(c.after)])];
      rows = cols.map((col) => {
        const changed = norm(c.before[col]) !== norm(c.after[col]);
        const before = col === "Password" && !changed ? masked(col, c.before[col]) : show(c.before[col]);
        return el("tr", { class: changed ? "tk-changed" : "tk-same" },
          el("td", {}, col), el("td", {}, before), el("td", {}, changed ? "→" : ""), el("td", {}, changed ? show(c.after[col]) : "unchanged"));
      });
      return el("div", { class: "tk-diff" }, head, table(["Column", "Before", "", "After"], rows));
    }
    if (c.action === "insert") {
      rows = Object.entries(c.values).map(([col, v]) => el("tr", { class: "tk-changed" }, el("td", {}, col), el("td", {}, show(v)), el("td", {}, "")));
      for (const [col, v] of Object.entries(meta.constants || {})) {
        rows.push(el("tr", { class: "tk-const" }, el("td", {}, col), el("td", {}, show(v)), el("td", { class: "muted" }, "set automatically")));
      }
      if (meta.unique && meta.unique.length) {
        rows.push(el("tr", { class: "tk-same" }, el("td", { colspan: "3", class: "muted" }, `Skipped if a row with this ${meta.unique.join(" + ")} already exists.`)));
      }
      return el("div", { class: "tk-diff" }, head, table(["Column", "New value", ""], rows));
    }
    rows = Object.entries(c.before).map(([col, v]) => el("tr", { class: "tk-deleted" }, el("td", {}, col), el("td", {}, col === "Password" ? masked(col, v) : show(v))));
    return el("div", { class: "tk-diff" }, head, table(["Column", "Value (row to be deleted)"], rows));
  }

  // Sends changes after confirmation. Returns the server response, or null if cancelled/failed.
  async function commit(title, changes) {
    if (!await confirmChanges(title, changes)) return null;
    const body = changes.map(({ action, table, pk, values, expect }) => ({ action, table, pk, values, expect }));
    try {
      const r = body.length === 1
        ? await api(`/api/toolkit/rows/${body[0].action}`, body[0].action === "insert"
          ? { table: body[0].table, values: body[0].values }
          : body[0].action === "update"
            ? { table: body[0].table, pk: body[0].pk, values: body[0].values, expect: body[0].expect }
            : { table: body[0].table, pk: body[0].pk, expect: body[0].expect })
        : await api("/api/toolkit/rows/batch", { ops: body });
      if (r.skipped && r.skipped.length) await message("Saved, with skips", r.skipped.join("\n"));
      else toast(changes.length > 1 ? `${changes.length} changes applied` : "Change applied");
      if (state.view === "changes") views.changes.load();
      return r;
    } catch (e) {
      await message("Change not applied", e.message, true);
      return null;
    }
  }

  // Form dialog. fields: [{name,label,value,mono,hint,disabled,options,browse}] -> values or null.
  function formDialog({ title, note, fields, okLabel = "Review change", validate }) {
    return modal((close) => {
      const inputs = {};
      const err = el("div", { class: "err tk-form-err" });
      const body = fields.map((f) => {
        let input;
        if (f.options) {
          input = el("select", { disabled: f.disabled || null },
            f.options.map(([v, label]) => el("option", { value: v }, label)));
          input.value = f.value == null ? "" : String(f.value);
        } else {
          input = el("input", { class: f.mono ? "mono" : "", disabled: f.disabled || null, autocomplete: "off", spellcheck: "false", placeholder: f.placeholder || "—" });
          input.value = f.value == null ? "" : String(f.value);
        }
        inputs[f.name] = input;
        const extra = [];
        if (f.browse) extra.push(el("button", { type: "button", class: "small alt", title: "Browse the customer tree", onclick: async () => { const n = await pickTreeNode(); if (n) input.value = n; } }, "Tree"));
        if (f.generate) extra.push(el("button", { type: "button", class: "small alt", onclick: () => { input.value = newPassword(); } }, "Generate"));
        if (f.copy) extra.push(el("button", { type: "button", class: "small subtle", onclick: () => copyText(input.value, f.label || f.name) }, "Copy"));
        return el("div", { class: "tk-field" },
          el("label", { class: "field-label" }, f.label || nice(f.name)),
          extra.length ? el("div", { class: "tk-inline" }, input, ...extra) : input,
          f.hint ? el("div", { class: "tk-hint" }, f.hint) : null);
      });
      const submit = () => {
        const values = {};
        for (const f of fields) if (!f.disabled) values[f.name] = inputs[f.name].value === "" ? null : inputs[f.name].value;
        const problem = validate ? validate(values) : null;
        if (problem) { err.textContent = problem; return; }
        close(values);
      };
      return [
        el("h3", {}, title), note ? el("p", { class: "muted" }, note) : null, ...body, err,
        el("div", { class: "row" },
          el("button", { type: "button", class: "subtle", onclick: () => close(null) }, "Cancel"),
          el("button", { type: "submit", class: "primary", onclick: submit }, okLabel)),
      ];
    });
  }

  // Edit one row: form -> diff -> send. Cancelling the diff goes back to the form with your edits.
  async function editRow({ table: key, row, cols, pkCols, title, fieldOpts = {}, after }) {
    let current = Object.fromEntries(cols.map((c) => [c, row[c]]));
    for (;;) {
      const values = await formDialog({
        title, note: pkCols.some((c) => cols.includes(c)) ? `Changing ${pkCols.join(" / ")} changes the row's key.` : null,
        fields: cols.map((c) => ({ name: c, label: c, value: current[c], mono: true, ...(fieldOpts[c] || {}) })),
      });
      if (!values) return null;
      current = { ...current, ...values };
      const changes = Object.fromEntries(Object.entries(values).filter(([c, v]) => norm(v) !== norm(row[c])));
      if (!Object.keys(changes).length) { await message("Nothing changed", "No values differ from the current row."); continue; }
      const meta = tableMeta(key);
      const pk = Object.fromEntries(pkCols.map((c) => [c, row[c] === undefined ? null : row[c]]));
      const r = await commit(title, [{ action: "update", table: key, pk, values: changes, expect: pick(row, meta.columns),
                                       before: pick(row, cols), after: { ...pick(row, cols), ...changes } }]);
      if (r) { if (after) await after(changes); return r; }
    }
  }

  async function deleteRow({ table: key, row, pkCols, title, after }) {
    const meta = tableMeta(key);
    const pk = Object.fromEntries(pkCols.map((c) => [c, row[c] === undefined ? null : row[c]]));
    const r = await commit(title, [{ action: "delete", table: key, pk, expect: pick(row, meta.columns), before: pick(row, meta.columns.filter((c) => c in row)) }]);
    if (r && after) await after();
    return r;
  }

  // ---- layout ------------------------------------------------------------------------------

  function render(section) {
    section.classList.add("tk");
    statusChip = el("button", { type: "button", class: "small subtle tk-status", onclick: () => setView("connection") }, "Connection…");
    bar = el("div", { class: "tk-bar" },
      el("span", { class: "panel-label" }, "TOOLKIT //"),
      el("div", { class: "tk-subnav" }, VIEWS.map(([id, label]) =>
        el("button", { type: "button", "data-view": id, onclick: () => setView(id) }, label))),
      el("span", { class: "spacer" }),
      el("span", { class: "tk-live" }, "LIVE PRODUCTION DATA · every change is confirmed and logged"),
      statusChip);
    section.append(bar);
    for (const [id] of VIEWS) {
      const v = el("div", { class: "tk-view", "data-view": id });
      section.append(v);
      views[id].root = v;
      views[id].build(v);
    }
  }

  function setView(id) {
    state.view = id;
    bar.querySelectorAll("[data-view]").forEach((b) => b.classList.toggle("active", b.dataset.view === id));
    document.querySelectorAll("#tab-toolkit .tk-view").forEach((v) => v.classList.toggle("active", v.dataset.view === id));
    views[id].shown?.();
  }

  async function loadMeta() {
    try {
      state.meta = await api("/api/toolkit/settings");
    } catch (e) {
      state.meta = null;
      statusChip.textContent = "Toolkit API unavailable";
      return;
    }
    const m = state.meta, s = m.settings, t = m.last_test;
    statusChip.classList.remove("ok", "bad");
    if (!m.configured) { statusChip.textContent = "Not set up"; statusChip.classList.add("bad"); }
    else if (!m.password_saved) { statusChip.textContent = "Password needed"; statusChip.classList.add("bad"); }
    else if (t) { statusChip.textContent = `${t.login} @ ${s.server}/${t.database}`; statusChip.classList.add("ok"); }
    else statusChip.textContent = `${s.username} @ ${s.server} (not tested)`;
    if (state.view === "connection") views.connection.fill();
  }

  async function onShow() {
    await loadMeta();
    if (!state.view) setView(state.meta && state.meta.configured && state.meta.password_saved ? "shipping" : "connection");
    else views[state.view].shown?.();
  }

  // ---- shipping -----------------------------------------------------------------------------

  views.shipping = {
    build(root) {
      this.spsInput = el("input", { placeholder: "EDI PO number (optional)", autocomplete: "off", spellcheck: "false" });
      this.spsInput.addEventListener("keydown", (e) => { if (e.key === "Enter") this.lookup(); });
      this.spsOut = el("div", { class: "tk-sps-out" }, el("div", { class: "tk-note" }, "Search SPS order documents by EDI PO number, fetch the carrier, then check it against the shipping methods."));
      this.acctFilter = el("input", { placeholder: "Search accounts…", autocomplete: "off", class: "tk-search" });
      let timer;
      this.acctFilter.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { state.accountFilter = this.acctFilter.value; this.drawAccounts(); }, 250); });
      this.acctCount = el("span", { class: "chip" }, "—");
      this.acctBody = el("div", { class: "tk-fill" }, el("div", { class: "tk-note" }, "Click Load accounts to read SHIPPING_ACCOUNT."));
      this.methHead = el("span", { class: "panel-label" }, "SHIPPING METHODS");
      this.methAdd = el("button", { type: "button", class: "small primary", disabled: true, onclick: () => this.addMethods(state.selected, []) }, "+ Add method");
      this.methBody = el("div", { class: "tk-fill" }, el("div", { class: "tk-note" }, "Select an account to see its shipping methods."));
      root.append(el("div", { class: "tk-ship" },
        el("div", { class: "panel tk-p-acct" },
          el("div", { class: "panel-head" }, el("span", { class: "panel-label" }, "EDI SHIP MAPPING // ACCOUNTS"), this.acctCount,
            el("span", { class: "spacer" }), this.acctFilter,
            el("button", { type: "button", class: "small", onclick: () => this.loadAccounts() }, "Load accounts"),
            el("button", { type: "button", class: "small primary", onclick: () => this.addAccount() }, "+ Add account")),
          this.acctBody),
        el("div", { class: "panel tk-p-meth" },
          el("div", { class: "panel-head" }, this.methHead, el("span", { class: "spacer" }),
            el("button", { type: "button", class: "small subtle", onclick: () => state.selected != null && this.loadMethods(state.selected) }, "↻"),
            this.methAdd),
          this.methBody),
        el("div", { class: "panel tk-p-sps" },
          el("div", { class: "panel-head" }, el("span", { class: "panel-label" }, "SPS LOOKUP // EDI PO → CARRIER → METHOD")),
          el("div", { class: "tk-inline" }, this.spsInput, el("button", { type: "button", class: "primary", onclick: () => this.lookup() }, "Lookup SPS IDs")),
          this.spsOut)));
    },

    async loadAccounts() {
      this.acctBody.replaceChildren(spinner());
      try {
        const r = await api("/api/toolkit/shipping/accounts", {});
        state.accounts = r.rows;
        this.drawAccounts(r.truncated);
      } catch (e) { this.acctBody.replaceChildren(errorBox(e)); }
    },

    drawAccounts(truncated) {
      if (!state.accounts) return;
      const term = state.accountFilter.trim().toLowerCase();
      const list = term ? state.accounts.filter((p) => SHIP_PARENT_COLS.some((f) => String(p[f] ?? "").toLowerCase().includes(term))) : state.accounts;
      this.acctCount.textContent = term ? `${list.length} / ${state.accounts.length}` : String(state.accounts.length);
      if (!list.length) { this.acctBody.replaceChildren(el("div", { class: "tk-note" }, "No accounts found.")); return; }
      const rows = list.map((p) => {
        const id = p.ACCOUNT_ID;
        const count = state.methods[id] ? state.methods[id].length : null;
        return el("tr", { class: "tk-click" + (state.selected === id ? " tk-sel" : ""), onclick: () => this.select(id) },
          el("td", { class: "tk-id" }, `#${id}`),
          SHIP_PARENT_COLS.map((f) => el("td", { class: p[f] == null ? "muted" : "" }, show(p[f]))),
          el("td", { class: "muted" }, count == null ? "—" : String(count)),
          el("td", { class: "tk-actions", onclick: (e) => e.stopPropagation() },
            el("button", { type: "button", class: "small", onclick: () => this.editAccount(p) }, "Edit"),
            el("button", { type: "button", class: "small tk-danger-o", onclick: () => this.deleteAccount(p) }, "Delete")));
      });
      this.acctBody.replaceChildren(
        table([{ label: "ID" }, ...SHIP_PARENT_COLS.map(nice), "Methods", { label: "", cls: "tk-actions" }], rows),
        truncated ? el("div", { class: "tk-hint" }, "Showing the first 10,000 rows.") : "");
    },

    select(id) {
      state.selected = id;
      this.drawAccounts();
      this.loadMethods(id);
    },

    async loadMethods(id) {
      const p = (state.accounts || []).find((x) => x.ACCOUNT_ID === id) || {};
      this.methHead.textContent = `SHIPPING METHODS // #${id} ${p.ACCOUNT_NAME ?? ""}`;
      this.methAdd.disabled = false;
      this.methBody.replaceChildren(spinner());
      try {
        const r = await api("/api/toolkit/shipping/methods", { account_id: id });
        state.methods[id] = r.rows;
        if (state.selected === id) this.drawMethods(id);
        this.drawAccounts();
      } catch (e) { this.methBody.replaceChildren(errorBox(e)); }
    },

    drawMethods(id) {
      const rows = state.methods[id] || [];
      if (!rows.length) { this.methBody.replaceChildren(el("div", { class: "tk-note" }, "No shipping methods configured for this account.")); return; }
      this.methBody.replaceChildren(table([{ label: "ID" }, ...SHIP_CHILD_COLS.map(nice), { label: "", cls: "tk-actions" }], rows.map((m) =>
        el("tr", {},
          el("td", { class: "tk-id" }, String(m.SHIPPING_METHOD_ID)),
          SHIP_CHILD_COLS.map((c) => el("td", { class: m[c] == null ? "muted" : "" }, show(m[c]))),
          el("td", { class: "tk-actions" },
            el("button", { type: "button", class: "small", onclick: () => this.editMethod(id, m) }, "Edit"),
            el("button", { type: "button", class: "small tk-danger-o", onclick: () => this.deleteMethod(id, m) }, "Delete"))))));
    },

    async editAccount(p) {
      await editRow({ table: "ship_account", row: p, cols: SHIP_PARENT_COLS, pkCols: ["ACCOUNT_ID"], title: `Edit account #${p.ACCOUNT_ID}`,
        after: (changes) => { Object.assign(p, changes); this.drawAccounts(); if (state.selected === p.ACCOUNT_ID) this.loadMethods(p.ACCOUNT_ID); } });
    },

    async deleteAccount(p) {
      await deleteRow({ table: "ship_account", row: p, pkCols: ["ACCOUNT_ID"], title: `Delete account #${p.ACCOUNT_ID}`,
        after: () => {
          state.accounts = state.accounts.filter((x) => x !== p);
          delete state.methods[p.ACCOUNT_ID];
          if (state.selected === p.ACCOUNT_ID) { state.selected = null; this.methAdd.disabled = true; this.methBody.replaceChildren(el("div", { class: "tk-note" }, "Select an account.")); }
          this.drawAccounts();
        } });
    },

    async addAccount() {
      let current = {};
      for (;;) {
        const v = await formDialog({ title: "New shipping account", okLabel: "Review insert",
          fields: SHIP_PARENT_COLS.map((c) => ({ name: c, label: nice(c), value: current[c], mono: true })) });
        if (!v) return;
        current = v;
        if (await commit("Insert shipping account", [{ action: "insert", table: "ship_account", values: v }])) { await this.loadAccounts(); return; }
      }
    },

    async editMethod(id, m) {
      await editRow({ table: "ship_method", row: m, cols: SHIP_CHILD_COLS, pkCols: ["SHIPPING_METHOD_ID"], title: `Edit shipping method #${m.SHIPPING_METHOD_ID}`,
        after: (changes) => { Object.assign(m, changes); this.drawMethods(id); } });
    },

    async deleteMethod(id, m) {
      await deleteRow({ table: "ship_method", row: m, pkCols: ["SHIPPING_METHOD_ID"], title: `Delete shipping method #${m.SHIPPING_METHOD_ID}`,
        after: () => { state.methods[id] = state.methods[id].filter((x) => x !== m); this.drawMethods(id); this.drawAccounts(); } });
    },

    // Add one or more methods. inboundIds prefill one entry each (from the SPS lookup).
    async addMethods(accountId, inboundIds) {
      if (!state.accounts) {
        try { state.accounts = (await api("/api/toolkit/shipping/accounts", {})).rows; this.drawAccounts(); }
        catch (e) { await message("Couldn't load accounts", e.message, true); return; }
      }
      const entries = (inboundIds && inboundIds.length ? inboundIds : [""]).map((v) => ({ INBOUND_ID: v || null }));
      let acct = accountId;
      for (;;) {
        const result = await modal((close) => {
          const sel = el("select", {}, el("option", { value: "" }, "(select account)"),
            state.accounts.map((p) => el("option", { value: String(p.ACCOUNT_ID) }, `#${p.ACCOUNT_ID} — ${show(p.ACCOUNT_NAME)}`)));
          sel.value = acct == null ? "" : String(acct);
          const err = el("div", { class: "err tk-form-err" });
          const inputs = entries.map(() => ({}));
          const blocks = entries.map((entry, i) => el("div", { class: "tk-entry" },
            entries.length > 1 ? el("div", { class: "panel-label" }, `ENTRY ${i + 1} — INBOUND ID ${show(entry.INBOUND_ID)}`) : null,
            el("div", { class: "tk-grid4" }, SHIP_CHILD_COLS.map((c) => {
              const input = el("input", { class: "mono", autocomplete: "off", placeholder: "—" });
              input.value = entry[c] ?? "";
              inputs[i][c] = input;
              return el("div", { class: "tk-field" }, el("label", { class: "field-label" }, nice(c)), input);
            }))));
          const submit = () => {
            if (!sel.value) { err.textContent = "Select the shipping account."; return; }
            inputs.forEach((m, i) => SHIP_CHILD_COLS.forEach((c) => { entries[i][c] = m[c].value === "" ? null : m[c].value; }));
            close(sel.value);
          };
          return [el("h3", {}, entries.length > 1 ? `New shipping methods (${entries.length})` : "New shipping method"),
            el("div", { class: "tk-field" }, el("label", { class: "field-label" }, "Account"), sel), ...blocks, err,
            el("div", { class: "row" },
              el("button", { type: "button", class: "subtle", onclick: () => close(null) }, "Cancel"),
              el("button", { type: "submit", class: "primary", onclick: submit }, "Review insert"))];
        }, { wide: true });
        if (!result) return;
        acct = Number(result);
        const changes = entries.map((e) => ({ action: "insert", table: "ship_method", values: { ACCOUNT_ID: acct, ...e } }));
        if (await commit(changes.length > 1 ? "Insert shipping methods" : "Insert shipping method", changes)) {
          this.select(acct);
          return;
        }
      }
    },

    // SPS: search documents -> fetch carrier -> check in methods -> add.
    async lookup() {
      this.spsOut.replaceChildren(spinner("Searching SPS documents…"));
      try {
        const r = await api("/api/toolkit/sps/search", { pattern: this.spsInput.value });
        if (!r.rows.length) { this.spsOut.replaceChildren(el("div", { class: "tk-note" }, "No SPS documents found.")); return; }
        this.spsOut.replaceChildren(table(["SPS ID", "Timestamp", { label: "", cls: "tk-actions" }], r.rows.map((d) =>
          el("tr", {}, el("td", { class: "mono" }, show(d.spsID)), el("td", {}, show(d.documentTimestamp)),
            el("td", { class: "tk-actions" }, el("button", { type: "button", class: "small alt", onclick: () => this.carrier(d.spsID) }, "Fetch carrier"))))),
          el("div", { class: "tk-hint" }, `Newest ${r.rows.length} matching order documents.`));
      } catch (e) { this.spsOut.replaceChildren(errorBox(e)); }
    },

    async carrier(spsid) {
      const back = [...this.spsOut.childNodes];
      this.spsOut.replaceChildren(spinner("Fetching carrier…"));
      try {
        const r = await api("/api/toolkit/sps/carrier", { spsid: String(spsid) });
        const backBtn = el("button", { type: "button", class: "small subtle", onclick: () => this.spsOut.replaceChildren(...back) }, "← Results");
        if (!r.rows.length) { this.spsOut.replaceChildren(backBtn, el("div", { class: "tk-note" }, `No carrier info found for ${spsid}.`)); return; }
        const row = r.rows[0];
        const carrier = row.CarrierAlphaCode ?? "", routing = row.CarrierRouting ?? "";
        const check = el("div", { class: "tk-check" });
        this.spsOut.replaceChildren(backBtn,
          el("div", { class: "tk-kv" },
            el("span", {}, "SPS ID"), el("b", { class: "mono" }, String(spsid)),
            el("span", {}, "CarrierAlphaCode"), el("b", { class: "mono" }, show(carrier)),
            el("span", {}, "CarrierRouting"), el("b", { class: "mono" }, show(routing))),
          el("button", { type: "button", class: "primary", onclick: () => this.check(carrier, routing, check) }, "Check in shipping methods"),
          check);
      } catch (e) { this.spsOut.replaceChildren(...back); toast(e.message, true); }
    },

    async check(carrier, routing, out) {
      out.replaceChildren(spinner("Checking shipping methods…"));
      try {
        const r = await api("/api/toolkit/sps/check", { CarrierAlphaCode: String(carrier), CarrierRouting: String(routing) });
        const found = Object.keys(r.results).filter((v) => r.results[v].length);
        const missing = Object.keys(r.results).filter((v) => !r.results[v].length);
        const parts = [];
        for (const v of found) {
          const rows = r.results[v], cols = Object.keys(rows[0]);
          parts.push(el("div", { class: "good tk-found" }, "✓ ", el("b", {}, v), " is already in shipping methods"),
            table(cols, rows.map((x) => el("tr", {}, cols.map((c) => el("td", {}, show(x[c])))))));
        }
        if (missing.length) {
          parts.push(el("div", { class: "tk-missing" }, `${missing.join(" and ")} not found in shipping methods.`),
            el("button", { type: "button", class: "primary", onclick: () => this.addMethods(state.selected, missing) },
              `Add method (${missing.length} entr${missing.length > 1 ? "ies" : "y"})`));
        }
        out.replaceChildren(...parts);
      } catch (e) { out.replaceChildren(errorBox(e)); }
    },
  };

  // ---- logi ---------------------------------------------------------------------------------

  views.logi = {
    build(root) {
      this.filter = el("input", { placeholder: "Filter by UserID or name…", autocomplete: "off", class: "tk-search" });
      this.filter.addEventListener("input", () => { state.logiFilter = this.filter.value; this.draw(); });
      this.count = el("span", { class: "chip" }, "—");
      this.body = el("div", { class: "tk-fill" }, el("div", { class: "tk-note" }, "Click Load users to read Logi.dbo.Users and the customer tree access table."));
      root.append(el("div", { class: "panel tk-p-logi" },
        el("div", { class: "panel-head" }, el("span", { class: "panel-label" }, "LOGI USER MANAGEMENT"), this.count,
          el("span", { class: "spacer" }), this.filter,
          el("button", { type: "button", class: "small", onclick: () => this.load() }, "Load users"),
          el("button", { type: "button", class: "small alt", onclick: () => this.lookup() }, "Look up user…"),
          el("button", { type: "button", class: "small primary", onclick: () => this.addUser() }, "+ Add user")),
        this.body));
    },

    async load() {
      this.body.replaceChildren(spinner());
      try {
        const r = await api("/api/toolkit/logi/all", {});
        state.logi = { users: r.users.rows, dash: r.dash.rows };
        this.draw();
      } catch (e) { this.body.replaceChildren(errorBox(e)); }
    },

    dashFor(uid) {
      const u = String(uid).toUpperCase();
      return (state.logi ? state.logi.dash : []).filter((r) => String(r.OPRID ?? "").toUpperCase() === u);
    },

    draw() {
      if (!state.logi) return;
      const f = state.logiFilter.trim().toLowerCase();
      const users = f ? state.logi.users.filter((r) => String(r.UserID ?? "").toLowerCase().includes(f) || String(r.FullName ?? "").toLowerCase().includes(f)) : state.logi.users;
      const dash = f ? state.logi.dash.filter((r) => String(r.OPRID ?? "").toLowerCase().includes(f)) : state.logi.dash;
      const ids = [...new Set([...users.map((r) => String(r.UserID ?? "").toUpperCase()), ...dash.map((r) => String(r.OPRID ?? "").toUpperCase())])].sort();
      const userMap = new Map(users.map((r) => [String(r.UserID ?? "").toUpperCase(), r]));
      const dashMap = new Map();
      for (const r of dash) { const k = String(r.OPRID ?? "").toUpperCase(); if (!dashMap.has(k)) dashMap.set(k, []); dashMap.get(k).push(r); }
      this.count.textContent = `${users.length} users · ${dash.length} access rows`;
      if (!ids.length) { this.body.replaceChildren(el("div", { class: "tk-note" }, "No users found.")); return; }
      const rows = ids.map((id) => {
        const u = userMap.get(id), d = dashMap.get(id) || [];
        const userCells = u
          ? LOGI_USER_COLS.map((c) => (c === "Password" ? this.passwordCell(u)
            : el("td", { class: u[c] == null ? "muted" : "" }, show(u[c]))))
          : [el("td", { colspan: String(LOGI_USER_COLS.length), class: "muted tk-norec" }, `${id} — no Users record`)];
        const userActions = el("td", { class: "tk-actions" }, u ? [
          el("button", { type: "button", class: "small", onclick: () => this.editUser(u) }, "Edit"),
          el("button", { type: "button", class: "small subtle", title: "Copy this user's details to the clipboard", onclick: () => this.clip(u) }, "Clipboard"),
          el("button", { type: "button", class: "small alt", title: "Create a new user based on this one", onclick: () => this.copyUser(u) }, "Copy user"),
          el("button", { type: "button", class: "small tk-danger-o", onclick: () => this.deleteUser(u.UserID) }, "Delete user"),
        ] : el("button", { type: "button", class: "small tk-danger-o", onclick: () => this.deleteUser(id) }, "Delete all"));
        const access = el("td", { class: "tk-access" },
          d.length ? d.map((r) => el("div", { class: "tk-node" },
            el("span", { class: "mono tk-node-code" }, show(r.TREE_NODE)),
            el("span", { class: "muted" }, show(r.TREE_NAME)),
            r.DEFAULT_TREE_NAME ? el("span", { class: "tag" }, `default ${r.DEFAULT_TREE_NAME}`) : null,
            el("span", { class: "spacer" }),
            el("button", { type: "button", class: "icon", onclick: () => this.editDash(r) }, "Edit"),
            el("button", { type: "button", class: "icon tk-danger-o", onclick: () => this.deleteDash(r) }, "Delete"))) : el("span", { class: "muted" }, "No tree access"),
          el("button", { type: "button", class: "icon alt tk-add-access", onclick: () => this.addAccess(u ? u.UserID : id) }, "+ Access"));
        return el("tr", {}, userCells, userActions, access);
      });
      this.body.replaceChildren(table([...LOGI_USER_COLS, { label: "", cls: "tk-actions" }, "Customer tree access (OPRID rows)"], rows, { cls: "tk-logi-table" }));
    },

    // Passwords aren't in the list (only has_password). Show / Clipboard / Copy fetch ONE user's password from
    // the server on that click; the server logs each one ("reveal" in the change log, without the value).
    passwordCell(u) {
      if (typeof u.Password === "string") {
        return el("td", { class: "mono" }, show(u.Password), " ",
          el("button", { type: "button", class: "icon subtle", title: "Hide the password again", onclick: () => { delete u.Password; this.draw(); } }, "Hide"));
      }
      if (!u.has_password) return el("td", { class: "mono muted" }, "—");
      return el("td", { class: "mono" }, "•••••••• ",
        el("button", { type: "button", class: "icon subtle", title: "Show this user's password (the reveal is logged)",
          onclick: async () => { try { u.Password = await this.passwordOf(u); this.draw(); } catch (e) { await message("Couldn't read the password", e.message, true); } } }, "Show"));
    },

    async passwordOf(u) {
      if (typeof u.Password === "string") return u.Password;
      if (!u.has_password) return "";
      return (await api("/api/toolkit/logi/password", { user_id: String(u.UserID) })).password;
    },

    async withPassword(u) {
      return { ...u, Password: await this.passwordOf(u) };
    },

    async clip(u) {
      let full;
      try { full = await this.withPassword(u); } catch (e) { await message("Couldn't read the password", e.message, true); return; }
      const nodes = this.dashFor(u.UserID).map((r) => r.TREE_NODE).filter((n) => n != null && String(n).trim() !== "");
      const text = LOGI_USER_COLS.map((c) => `${c}: ${full[c] ?? ""}`).join("\n") + `\nTree Nodes: ${nodes.length ? nodes.join(", ") : "(none)"}`;
      copyText(text, `${u.UserID} details`);
    },

    async editUser(u) {
      await editRow({ table: "logi_users", row: u, cols: LOGI_USER_COLS, pkCols: ["UserID"], title: `Edit Logi user ${u.UserID}`,
        fieldOpts: { UserID: { hint: "Renaming does not rename the Roles, Rights or tree access rows." },
          Password: { generate: true, copy: true, placeholder: typeof u.Password !== "string" && u.has_password ? "hidden: leave blank to keep it" : "—",
            hint: typeof u.Password !== "string" && u.has_password ? "Leave blank to keep the current password, or Generate a new one." : null } },
        after: (changes) => { Object.assign(u, changes); this.draw(); } });
    },

    async editDash(r) {
      await editRow({ table: "logi_dash", row: r, cols: DASH_EDIT_COLS, pkCols: ["OPRID", "TREE_NODE"], title: `Edit tree access ${r.OPRID} / ${show(r.TREE_NODE)}`,
        fieldOpts: { TREE_NODE: { browse: true } },
        after: (changes) => { Object.assign(r, changes); this.draw(); } });
    },

    async deleteDash(r) {
      await deleteRow({ table: "logi_dash", row: r, pkCols: ["OPRID", "TREE_NODE"], title: `Delete tree access ${r.OPRID} / ${show(r.TREE_NODE)}`,
        after: () => { state.logi.dash = state.logi.dash.filter((x) => x !== r); this.draw(); } });
    },

    // Reads all four tables for one user (exact UserID); used for "Look up user" and "Delete user".
    async fetchUser(uid) {
      return api("/api/toolkit/logi/user", { user_id: String(uid) });
    },

    async lookup() {
      const v = await formDialog({ title: "Look up a Logi user", note: "Reads Users, Roles, Rights and customer tree access for one exact UserID.",
        okLabel: "Look up", fields: [{ name: "UserID", label: "User ID", mono: true, value: state.logiFilter }], validate: (x) => (x.UserID ? null : "Enter a User ID") });
      if (!v) return;
      let r;
      try { r = await this.fetchUser(v.UserID.trim()); } catch (e) { await message("Lookup failed", e.message, true); return; }
      await modal((close) => [
        el("h3", {}, `Logi user ${v.UserID}`),
        ...["logi_users", "logi_roles", "logi_rights", "logi_dash"].map((k) => {
          const t = r[k] || {};
          const head = el("div", { class: "panel-label gap" }, `${t.label || k} (${t.rows ? t.rows.length : 0})`);
          if (t.error) return el("div", {}, head, el("div", { class: "tk-error" }, t.error));
          if (!t.rows.length) return el("div", {}, head, el("div", { class: "tk-note" }, "No rows."));
          return el("div", {}, head, table(t.columns, t.rows.map((row) => el("tr", {}, t.columns.map((c) => el("td", {}, masked(c, row[c])))))));
        }),
        el("div", { class: "row" }, el("button", { type: "button", class: "primary", onclick: () => close(true) }, "Close")),
      ], { wide: true });
    },

    async deleteUser(uid) {
      let r;
      try { r = await this.fetchUser(uid); } catch (e) { await message("Couldn't read the user", e.message, true); return; }
      const errors = Object.values(r).filter((t) => t.error).map((t) => `${t.label}: ${t.error}`);
      if (errors.length) { await message("Couldn't read the user", errors.join("\n"), true); return; }
      const changes = [];
      for (const k of ["logi_users", "logi_roles", "logi_rights", "logi_dash"]) {
        const meta = tableMeta(k);
        for (const row of r[k].rows) {
          changes.push({ action: "delete", table: k, pk: Object.fromEntries(meta.pk.map((c) => [c, row[c] ?? null])),
                         expect: pick(row, meta.columns), before: pick(row, r[k].columns) });
        }
      }
      if (!changes.length) { await message("Nothing to delete", `No rows found for ${uid}.`); return; }
      if (await commit(`Delete user ${uid} from all tables`, changes)) await this.load();
    },

    // Tree node rows used by Add user / Copy user / + Access.
    nodeRows(initial) {
      const box = el("div", { class: "tk-nodes" });
      const add = (value) => {
        const input = el("input", { class: "mono", placeholder: "Tree node — type or browse", autocomplete: "off" });
        input.value = value || "";
        const row = el("div", { class: "tk-inline" }, input,
          el("button", { type: "button", class: "small alt", onclick: async () => { const n = await pickTreeNode(); if (n) input.value = n; } }, "Tree"),
          el("button", { type: "button", class: "small tk-danger-o", onclick: () => { if (box.children.length > 1) row.remove(); else input.value = ""; } }, "✕"));
        box.append(row);
      };
      (initial && initial.length ? initial : [""]).forEach(add);
      return { box, add, values: () => [...box.querySelectorAll("input")].map((i) => i.value.trim()).filter(Boolean) };
    },

    userOps(uid, fields, nodes) {
      const tree = (state.meta && state.meta.settings.tree.tree) || "DI_CUST_ROLLUP";
      return [
        { action: "insert", table: "logi_users", values: { UserID: uid, FullName: fields.FullName, EmailID: fields.EmailID, Password: fields.Password } },
        { action: "insert", table: "logi_roles", values: { UserID: uid } },
        { action: "insert", table: "logi_rights", values: { UserID: uid } },
        ...[...new Set(nodes)].map((n) => ({ action: "insert", table: "logi_dash", values: { OPRID: uid.toUpperCase(), TREE_NAME: tree, TREE_NODE: n, DEFAULT_TREE_NAME: "Y" } })),
      ];
    },

    async addUser() {
      const keep = { UserID: "", FullName: "", EmailID: "", Password: newPassword(), nodes: [] };
      for (;;) {
        const res = await modal((close) => {
          const f = {};
          const mk = (name, label, extra = []) => {
            f[name] = el("input", { class: name === "Password" || name === "UserID" ? "mono" : "", autocomplete: "off", spellcheck: "false", placeholder: name === "UserID" ? "e.g. jsmith" : "—" });
            f[name].value = keep[name] || "";
            return el("div", { class: "tk-field" }, el("label", { class: "field-label" }, label), extra.length ? el("div", { class: "tk-inline" }, f[name], ...extra) : f[name]);
          };
          const nodes = this.nodeRows(keep.nodes);
          const err = el("div", { class: "err tk-form-err" });
          const submit = () => {
            for (const k of ["UserID", "FullName", "EmailID", "Password"]) keep[k] = f[k].value.trim();
            keep.Password = f.Password.value;
            keep.nodes = nodes.values();
            if (!keep.UserID) { err.textContent = "User ID is required."; return; }
            if (keep.UserID.length > 64) { err.textContent = "User ID is too long."; return; }
            if (!keep.nodes.length) { err.textContent = "Add at least one tree node."; return; }
            close(true);
          };
          return [el("h3", {}, "New Logi user"),
            el("p", { class: "muted" }, "Adds the user to Users, Roles, Rights and customer tree access in one transaction. Constants are set automatically; tables that already have this user are skipped."),
            mk("UserID", "User ID"), mk("FullName", "Full name"), mk("EmailID", "Email ID"),
            mk("Password", "Password", [el("button", { type: "button", class: "small alt", onclick: () => { f.Password.value = newPassword(); } }, "Generate"),
              el("button", { type: "button", class: "small subtle", onclick: () => copyText(f.Password.value, "Password") }, "Copy")]),
            el("div", { class: "tk-field" }, el("div", { class: "tk-inline" }, el("label", { class: "field-label grow" }, "Dashboard tree nodes"),
              el("button", { type: "button", class: "small", onclick: () => nodes.add("") }, "+ Add row")), nodes.box),
            err,
            el("div", { class: "row" }, el("button", { type: "button", class: "subtle", onclick: () => close(false) }, "Cancel"),
              el("button", { type: "submit", class: "primary", onclick: submit }, "Review insert"))];
        }, { wide: true });
        if (!res) return;
        const r = await commit(`Add Logi user ${keep.UserID}`, this.userOps(keep.UserID, keep, keep.nodes));
        if (r) { await this.load(); this.filter.value = state.logiFilter = keep.UserID; this.draw(); return; }
      }
    },

    async copyUser(src) {
      const srcNodes = this.dashFor(src.UserID).map((r) => r.TREE_NODE).filter((n) => n != null && String(n).trim() !== "");
      const keep = { UserID: "", Password: newPassword(), EmailID: src.EmailID ?? "", FullName: src.FullName ?? "", nodes: srcNodes.map(String) };
      for (;;) {
        const res = await modal((close) => {
          const f = {};
          const nodes = this.nodeRows(keep.nodes);
          const err = el("div", { class: "err tk-form-err" });
          const rows = LOGI_USER_COLS.map((c) => {
            f[c] = el("input", { class: "mono", autocomplete: "off", spellcheck: "false", placeholder: "—" });
            f[c].value = keep[c] ?? "";
            const sv = src[c];
            return el("div", { class: "tk-copy-row" },
              el("div", { class: "field-label" }, c),
              el("div", { class: "tk-copy-src" }, el("span", { class: "mono", title: c === "Password" ? "" : show(sv) },
                c === "Password" ? (src.has_password || sv ? "••••••••" : "—") : show(sv)),
                el("button", { type: "button", class: "icon subtle", title: `Copy source ${c}`, onclick: async () => {
                  if (c !== "Password") { copyText(sv, c); return; }
                  try { copyText(await this.passwordOf(src), c); } catch (e) { toast(e.message, true); }
                } }, "⧉")),
              el("div", { class: "tk-inline" }, f[c],
                c === "Password" ? el("button", { type: "button", class: "icon alt", onclick: () => { f.Password.value = newPassword(); } }, "↺") : null,
                el("button", { type: "button", class: "icon subtle", title: `Copy new ${c}`, onclick: () => copyText(f[c].value, c) }, "⧉")),
              el("div", { class: "tk-hint" }, c === "UserID" ? "required — must be new" : c === "Password" ? "auto-generated" : ""));
          });
          const submit = () => {
            for (const c of LOGI_USER_COLS) keep[c] = c === "Password" ? f[c].value : f[c].value.trim();
            keep.nodes = nodes.values();
            const uid = keep.UserID;
            if (!uid) { err.textContent = "New User ID is required."; return; }
            if (uid.toUpperCase() === String(src.UserID).toUpperCase()) { err.textContent = "New User ID must differ from the source user."; return; }
            if (state.logi.users.some((r) => String(r.UserID).toUpperCase() === uid.toUpperCase())) { err.textContent = `User "${uid}" already exists.`; return; }
            if (!keep.nodes.length) { err.textContent = "Add at least one tree node."; return; }
            close(true);
          };
          return [el("h3", {}, `Copy Logi user ${src.UserID}`),
            el("p", { class: "muted" }, "Creates a new user in Users, Roles, Rights and customer tree access, with the source user's tree nodes and a new password."),
            el("div", { class: "tk-copy-grid" },
              el("div", { class: "tk-copy-row tk-copy-head" }, el("span", {}, "Field"),
                el("span", {}, "Source value ", el("button", { type: "button", class: "icon subtle", title: "Copy all source values", onclick: async () => {
                  let full;
                  try { full = await this.withPassword(src); } catch (e) { toast(e.message, true); return; }
                  copyText(LOGI_USER_COLS.map((c) => `${c}: ${full[c] ?? ""}`).join("\n"), `${src.UserID} details`);
                } }, "⧉")),
                el("span", {}, "New value ", el("button", { type: "button", class: "icon subtle", title: "Copy all new values", onclick: () => copyText(LOGI_USER_COLS.map((c) => `${c}: ${f[c].value}`).join("\n"), "New credentials") }, "⧉")),
                el("span", {}, "")),
              ...rows),
            el("div", { class: "tk-field" }, el("div", { class: "tk-inline" },
              el("label", { class: "field-label grow" }, `Dashboard tree nodes — ${srcNodes.length} copied from ${src.UserID}`),
              el("button", { type: "button", class: "small", onclick: () => nodes.add("") }, "+ Add row")), nodes.box),
            err,
            el("div", { class: "row" }, el("button", { type: "button", class: "subtle", onclick: () => close(false) }, "Cancel"),
              el("button", { type: "submit", class: "primary", onclick: submit }, "Review copy"))];
        }, { wide: true });
        if (!res) return;
        const r = await commit(`Copy ${src.UserID} → ${keep.UserID}`, this.userOps(keep.UserID, keep, keep.nodes));
        if (r) { await this.load(); this.filter.value = state.logiFilter = keep.UserID; this.draw(); return; }
      }
    },

    async addAccess(uid) {
      let keep = [];
      for (;;) {
        const res = await modal((close) => {
          const nodes = this.nodeRows(keep);
          const err = el("div", { class: "err tk-form-err" });
          return [el("h3", {}, `Add tree access for ${String(uid).toUpperCase()}`),
            el("div", { class: "tk-field" }, el("div", { class: "tk-inline" }, el("label", { class: "field-label grow" }, "Tree nodes"),
              el("button", { type: "button", class: "small", onclick: () => nodes.add("") }, "+ Add row")), nodes.box),
            err,
            el("div", { class: "row" }, el("button", { type: "button", class: "subtle", onclick: () => close(null) }, "Cancel"),
              el("button", { type: "submit", class: "primary", onclick: () => { keep = nodes.values(); if (!keep.length) { err.textContent = "Add at least one tree node."; return; } close(keep); } }, "Review insert"))];
        });
        if (!res) return;
        const tree = (state.meta && state.meta.settings.tree.tree) || "DI_CUST_ROLLUP";
        const ops = [...new Set(res)].map((n) => ({ action: "insert", table: "logi_dash", values: { OPRID: String(uid).toUpperCase(), TREE_NAME: tree, TREE_NODE: n, DEFAULT_TREE_NAME: "Y" } }));
        if (await commit(`Add tree access for ${String(uid).toUpperCase()}`, ops)) { await this.load(); return; }
      }
    },
  };

  // ---- customer tree --------------------------------------------------------------------------

  async function ensureTree() {
    if (!state.tree) state.tree = await api("/api/toolkit/tree", {});
    return state.tree;
  }

  function treeList(rows, selected, onPick) {
    const list = el("div", { class: "tk-tree" });
    const draw = (term) => {
      const t = (term || "").toLowerCase();
      const shown = t ? rows.filter((r) => r.label.toLowerCase().includes(t) || r.node.toLowerCase().includes(t)) : rows;
      list.replaceChildren(...(shown.length ? shown.slice(0, 5000).map((r) => el("div", {
        class: "tk-tree-node" + (selected() === r.node ? " tk-sel" : ""), style: `padding-left:${0.6 + (t ? 0 : (Math.max(1, r.level) - 1) * 1.2)}rem`,
        onclick: (e) => { list.querySelectorAll(".tk-sel").forEach((x) => x.classList.remove("tk-sel")); e.currentTarget.classList.add("tk-sel"); onPick(r); },
      }, r.level > 1 && !t ? el("span", { class: "tk-tree-conn" }, "└") : null, el("span", { class: "tk-tree-label" }, r.label), el("span", { class: "tk-tree-code mono" }, r.node)))
        : [el("div", { class: "tk-note" }, "No nodes found")]));
    };
    draw("");
    return { list, draw };
  }

  // Picker used by tree-node inputs. Resolves the node code or null.
  async function pickTreeNode() {
    let tree;
    try { tree = await ensureTree(); } catch (e) { await message("Customer tree", e.message, true); return null; }
    let chosen = null;
    return modal((close) => {
      const sel = el("div", { class: "tk-tree-sel" }, "Click a node to select it.");
      const t = treeList(tree.rows, () => chosen, (r) => { chosen = r.node; sel.textContent = `Selected: ${r.label} (${r.node})`; });
      const search = el("input", { placeholder: "Search nodes…", autocomplete: "off" });
      search.addEventListener("input", () => t.draw(search.value));
      return [el("h3", {}, `${tree.as_of ? tree.as_of.slice(-4) + " " : ""}Customer tree — ${tree.tree}`), search, t.list, sel,
        el("div", { class: "row" }, el("button", { type: "button", class: "subtle", onclick: () => close(null) }, "Close"),
          el("button", { type: "submit", class: "primary", onclick: () => { if (chosen) close(chosen); else sel.textContent = "Select a node first."; } }, "Use selected node"))];
    }, { wide: true, cls: "tk-tree-dialog" });
  }

  views.customers = {
    build(root) {
      this.search = el("input", { placeholder: "Search nodes…", autocomplete: "off", class: "tk-search" });
      this.search.addEventListener("input", () => this.t && this.t.draw(this.search.value));
      this.sel = el("span", { class: "muted mono" }, "");
      this.copyBtn = el("button", { type: "button", class: "small alt", disabled: true, onclick: () => state.treeSel && copyText(state.treeSel.node, "Node code") }, "Copy node code");
      this.count = el("span", { class: "chip" }, "—");
      this.body = el("div", { class: "tk-fill" }, el("div", { class: "tk-note" }, "Click Load tree to read the customer rollup tree (dashBoard.rsTreeStructureByUser)."));
      root.append(el("div", { class: "panel tk-p-tree" },
        el("div", { class: "panel-head" }, el("span", { class: "panel-label" }, "CUSTOMER TREE // DI_CUST_ROLLUP"), this.count, el("span", { class: "spacer" }),
          this.sel, this.copyBtn, this.search, el("button", { type: "button", class: "small", onclick: () => this.load(true) }, "Load tree")),
        this.body));
    },
    async load(force) {
      this.body.replaceChildren(spinner());
      try {
        if (force) state.tree = null;
        const tree = await ensureTree();
        this.count.textContent = `${tree.rows.length} nodes`;
        this.t = treeList(tree.rows, () => state.treeSel && state.treeSel.node, (r) => {
          state.treeSel = r; this.sel.textContent = `${r.label} (${r.node})`; this.copyBtn.disabled = false;
        });
        this.body.replaceChildren(this.t.list);
        if (this.search.value) this.t.draw(this.search.value);
      } catch (e) { this.body.replaceChildren(errorBox(e)); }
    },
    shown() { if (state.tree && !this.t) this.load(false); },
  };

  // ---- change log -------------------------------------------------------------------------------

  function summarize(c) {
    const b = c.before_json || {}, a = c.after_json || {};
    if (c.action === "reveal") return "password shown or copied (the value isn't recorded)";
    if (c.action === "update") {
      return Object.keys(a).filter((k) => norm(a[k]) !== norm(b[k])).map((k) => `${k}: ${show(b[k])} → ${show(a[k])}`).join("; ") || "—";
    }
    if (c.action === "insert") return Object.entries(a).map(([k, v]) => `${k}=${show(v)}`).join(", ");
    return Object.entries(b).map(([k, v]) => `${k}=${show(v)}`).join(", ");
  }

  views.changes = {
    build(root) {
      this.select = el("select", { class: "tk-select" }, el("option", { value: "" }, "All tables"));
      this.select.addEventListener("change", () => { state.changesTable = this.select.value; this.load(); });
      this.count = el("span", { class: "chip" }, "—");
      this.body = el("div", { class: "tk-fill" });
      root.append(el("div", { class: "panel tk-p-log" },
        el("div", { class: "panel-head" }, el("span", { class: "panel-label" }, "CHANGE LOG // EVERY ATTEMPT, NEWEST FIRST"), this.count,
          el("span", { class: "spacer" }), this.select, el("button", { type: "button", class: "small", onclick: () => this.load() }, "↻ Refresh")),
        el("div", { class: "tk-hint" }, "Stored in data/commandcenter.db (table toolkit_changes, append-only). Logi passwords are not recorded."),
        this.body));
    },
    async load() {
      this.body.replaceChildren(spinner());
      try {
        const r = await api(`/api/toolkit/changes?table=${encodeURIComponent(state.changesTable)}`);
        if (this.select.options.length === 1) {
          for (const [k, label] of Object.entries(r.tables)) this.select.append(el("option", { value: k }, label));
          this.select.value = state.changesTable;
        }
        this.count.textContent = String(r.changes.length);
        if (!r.changes.length) { this.body.replaceChildren(el("div", { class: "tk-note" }, "No changes recorded yet.")); return; }
        this.body.replaceChildren(table(["Time", "Result", "Action", "Table", "Key", "Changes", "Windows user", "Connection", "Error"], r.changes.map((c) => {
          const status = c.ok === 1 ? (c.error ? ["skipped", "tk-st-skip"] : ["committed", "tk-st-ok"]) : c.ok === 0 ? ["failed", "tk-st-bad"] : ["unknown", "tk-st-skip"];
          return el("tr", { class: "tk-click", onclick: () => this.detail(c) },
            el("td", { class: "mono" }, show(c.ts).replace("T", " ")),
            el("td", {}, el("span", { class: `tag ${status[1]}` }, status[0])),
            el("td", {}, el("span", { class: `tag tk-act-${c.action}` }, c.action)),
            el("td", { title: c.table_name || "" }, (r.tables[c.table_key] || c.table_key)),
            el("td", { class: "mono" }, c.pk ? Object.values(c.pk).map(show).join(" / ") : "—"),
            el("td", { class: "tk-sum", title: summarize(c) }, summarize(c)),
            el("td", {}, show(c.windows_user)), el("td", { class: "mono" }, show(c.connection)),
            el("td", { class: "err tk-sum", title: c.error || "" }, c.ok === 1 ? (c.error || "") : show(c.error)));
        }), { cls: "tk-log-table" }));
      } catch (e) { this.body.replaceChildren(errorBox(e)); }
    },
    detail(c) {
      const b = c.before_json || {}, a = c.after_json || {};
      const cols = [...new Set([...Object.keys(b), ...Object.keys(a)])];
      modal((close) => [
        el("h3", {}, `Change #${c.id} — ${c.action} ${c.table_name || c.table_key}`),
        el("div", { class: "tk-kv" },
          el("span", {}, "Time"), el("b", {}, show(c.ts)), el("span", {}, "Windows user"), el("b", {}, show(c.windows_user)),
          el("span", {}, "Connection"), el("b", {}, show(c.connection)), el("span", {}, "Key"), el("b", { class: "mono" }, c.pk ? JSON.stringify(c.pk) : "—"),
          el("span", {}, "Result"), el("b", {}, c.ok === 1 ? "committed" : c.ok === 0 ? "failed / rolled back" : "unknown (app stopped mid-change)"),
          c.batch ? el("span", {}, "Batch") : null, c.batch ? el("b", { class: "mono" }, c.batch) : null,
          c.error ? el("span", {}, "Message") : null, c.error ? el("b", { class: c.ok === 1 ? "" : "err" }, c.error) : null),
        cols.length ? table(["Column", "Before", "After"], cols.map((k) => el("tr", { class: norm(b[k]) !== norm(a[k]) && c.action === "update" ? "tk-changed" : "" },
          el("td", {}, k), el("td", {}, k in b ? show(b[k]) : ""), el("td", {}, k in a ? show(a[k]) : "")))) : null,
        el("div", { class: "row" }, el("button", { type: "button", class: "primary", onclick: () => close(true) }, "Close")),
      ], { wide: true });
    },
    shown() { this.load(); },
  };

  // ---- connection ---------------------------------------------------------------------------------

  const CERT_ERROR = /certificate/i;
  const fingerprint = (hex) => (hex || "").match(/.{1,4}/g)?.join(" ") || "";

  views.connection = {
    build(root) {
      const inp = (ph, cls = "mono") => el("input", { class: cls, autocomplete: "off", spellcheck: "false", placeholder: ph });
      this.f = {
        server: inp("snc-xxxx-db01.example.com or HOST\\INSTANCE"), username: inp("SQL login"),
        driver: el("select", {}), encrypt: el("input", { type: "checkbox" }), trust: el("input", { type: "checkbox" }),
        staging: inp("FS92STG"), logi: inp("Logi"), sys: inp("FS92SYS"), schema: inp("sps"),
      };
      this.pw = el("input", { type: "password", autocomplete: "new-password", placeholder: "SQL password (stored in Windows Credential Manager)" });
      this.pw.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); this.savePassword(); } });
      this.status = el("div", { class: "tk-conn-status" });
      this.info = el("div", { class: "tk-hint" });
      this.cert = el("div", { class: "tk-cert" });
      const field = (label, input, hint) => el("div", { class: "tk-field" }, el("label", { class: "field-label" }, label), input, hint ? el("div", { class: "tk-hint" }, hint) : null);
      root.append(el("div", { class: "tk-conn" },
        el("div", { class: "panel" },
          el("div", { class: "panel-head" }, el("span", { class: "panel-label" }, "CONNECTION // SQL LOGIN")),
          this.status,
          el("div", { class: "tk-grid2" }, field("Server", this.f.server, "Host name (VPN must be up). Save stores it on this PC and overrides config.yaml toolkit:."), field("Username", this.f.username, "SQL Server login (not Windows auth).")),
          el("div", { class: "tk-grid2" }, field("ODBC driver", this.f.driver),
            el("div", { class: "tk-field" }, el("label", { class: "field-label" }, "Encryption"),
              el("label", { class: "tk-check-label" }, this.f.encrypt, "Encrypt connection"),
              el("label", { class: "tk-check-label" }, this.f.trust, "Trust server certificate without verifying it (not recommended; only if IT confirms the server)"))),
          el("div", { class: "tk-field" }, el("label", { class: "field-label" }, "Certificate"), this.cert),
          el("div", { class: "tk-grid4" }, field("Staging DB", this.f.staging), field("Logi DB", this.f.logi), field("Dashboard DB", this.f.sys), field("Shipping schema", this.f.schema)),
          el("div", { class: "row" }, el("button", { type: "button", class: "primary", onclick: () => this.saveSettings() }, "Save settings"))),
        el("div", { class: "panel" },
          el("div", { class: "panel-head" }, el("span", { class: "panel-label" }, "PASSWORD")),
          el("p", { class: "muted" }, "The password is tested, then stored in Windows Credential Manager. It is never shown again, logged or written to a file."),
          this.pw,
          el("div", { class: "row" },
            el("button", { type: "button", class: "subtle", onclick: () => this.forget() }, "Forget password"),
            el("button", { type: "button", class: "alt", onclick: () => this.test() }, "Test connection"),
            el("button", { type: "button", class: "primary", onclick: () => this.savePassword() }, "Save password & test")),
          this.info)));
    },
    fill() {
      const m = state.meta;
      if (!m) return;
      const s = m.settings;
      this.f.server.value = s.server || ""; this.f.username.value = s.username || "";
      this.f.driver.replaceChildren(...m.drivers.map((d) => el("option", { value: d }, d + (m.installed_drivers.includes(d) ? "" : " (not installed)"))));
      this.f.driver.value = s.driver;
      this.f.encrypt.checked = !!s.encrypt; this.f.trust.checked = !!s.trust_server_certificate;
      this.f.staging.value = s.databases.staging; this.f.logi.value = s.databases.logi; this.f.sys.value = s.databases.sys; this.f.schema.value = s.shipping_schema;
      const t = m.last_test;
      this.status.replaceChildren(
        !m.configured ? el("div", { class: "tk-error" }, "Not set up: enter the server and username, then save.")
          : !m.password_saved ? el("div", { class: "tk-error" }, `No password saved for ${s.username} on ${s.server}.`)
            : t ? el("div", { class: "tk-ok" }, `Connected as ${t.login} on ${t.server || s.server}/${t.database}`, el("span", { class: "muted" }, `  tested ${CC.when(t.at)}`))
              : el("div", { class: "tk-note" }, `Password saved for ${s.username} on ${s.server}. Not tested yet.`));
      const pin = m.cert_pin;
      this.cert.replaceChildren(
        pin ? el("div", { class: "tk-ok" }, "Pinned: only this certificate is accepted")
            : el("div", { class: "muted" }, "Not pinned: the server's certificate must come from a CA this PC trusts."),
        pin && el("div", { class: "tk-hint" }, el("span", { class: "mono" }, fingerprint(pin.sha256)),
          ` · ${pin.subject} · valid to ${pin.valid_to} · approved ${CC.when(pin.pinned_at)}`),
        el("div", { class: "row" },
          m.configured && el("button", { type: "button", class: "alt", onclick: () => this.certDialog() }, pin ? "Check certificate" : "Review & pin certificate"),
          pin && el("button", { type: "button", class: "subtle", onclick: () => this.unpin() }, "Remove pin")));
      this.info.replaceChildren(
        el("div", {}, "Credential Manager entry: ", el("span", { class: "mono" }, `${m.keyring.service} / ${m.keyring.user || "<username>@<server>"}`)),
        el("div", {}, "Tree access table: ", el("span", { class: "mono" }, m.tables.logi_dash.name || "—"),
          " · customer tree: ", el("span", { class: "mono" }, `${s.tree.user}, ${s.tree.tree}, ${s.tree.setid}, ${s.tree.as_of}`), " (config.yaml toolkit:)"));
    },
    async saveSettings() {
      const f = this.f;
      try {
        state.meta = await api("/api/toolkit/settings", {
          server: f.server.value.trim(), username: f.username.value.trim(), driver: f.driver.value,
          encrypt: f.encrypt.checked, trust_server_certificate: f.trust.checked,
          databases: { staging: f.staging.value.trim(), logi: f.logi.value.trim(), sys: f.sys.value.trim() }, shipping_schema: f.schema.value.trim(),
        });
        toast("Settings saved");
        await loadMeta();
      } catch (e) { await message("Settings not saved", e.message, true); }
    },
    async savePassword(retry) {
      const pw = retry || this.pw.value;
      if (!pw) { toast("Enter the password", true); return; }
      this.pw.value = "";
      this.status.replaceChildren(spinner("Testing login…"));
      try {
        const r = await api("/api/toolkit/password", { password: pw });
        toast(`Connected as ${r.test.login}`);
      } catch (e) {
        // A certificate problem: offer to review and pin it, then retry with the same password (kept only for this).
        if (CERT_ERROR.test(e.message) && await this.certDialog(e.message)) return this.savePassword(pw);
        await message("Password not saved", e.message, true);
      }
      await loadMeta();
    },
    async test() {
      this.status.replaceChildren(spinner("Testing connection…"));
      try { const r = await api("/api/toolkit/test", {}); toast(`Connected as ${r.login}`); }
      catch (e) {
        if (CERT_ERROR.test(e.message) && await this.certDialog(e.message)) return this.test();
        await message("Connection failed", e.message, true);
      }
      await loadMeta();
    },
    // Shows the certificate the server presents (no login is sent) and, after a deliberate click, pins it.
    // Resolves true when a certificate was pinned.
    async certDialog(problem) {
      let r;
      try { r = await api("/api/toolkit/cert/inspect", {}); }
      catch (e) { await message("Couldn't read the certificate", e.message, true); return false; }
      const c = r.cert, pin = r.pin;
      if (r.matches) { await message("Certificate matches", `The server presents the certificate you approved (${fingerprint(c.sha256)}).`); return false; }
      const kv = (k, v) => el("div", { class: "tk-cert-row" }, el("span", { class: "muted" }, k), el("span", { class: "mono" }, v));
      const ok = await modal((close) => [
        el("h3", {}, pin ? "SQL certificate changed" : "Pin the SQL server's certificate"),
        problem && el("p", { class: "err" }, problem),
        pin ? el("p", { class: "tk-warn" }, "This server now presents a different certificate than the one you approved. Approve it only if you expected it (SQL Server makes a new self-signed one when its service restarts, e.g. after patching). If you didn't expect it, check with IT first.")
            : el("p", {}, c.self_signed
              ? "The server uses a self-signed certificate (SQL Server's own), so no CA can vouch for it. Pinning makes Command Center accept only this exact certificate: anyone else presenting a different one is refused before your password is sent."
              : "No CA this PC trusts issued this certificate. Pinning makes Command Center accept only this exact certificate."),
        el("div", { class: "tk-cert" },
          kv("Server", `${c.host}:${c.port}`), kv("Subject", c.subject), kv("Issuer", c.issuer),
          kv("Valid", `${c.valid_from} → ${c.valid_to}`), kv("SHA-256", fingerprint(c.sha256)),
          pin && kv("Approved before", fingerprint(pin.sha256))),
        el("div", { class: "row" },
          el("button", { type: "button", class: "subtle", onclick: () => close(false) }, "Cancel"),
          el("button", { type: "button", class: pin ? "tk-danger" : "primary", onclick: () => close(true) }, pin ? "Approve new certificate" : "Pin this certificate")),
      ], { focus: false });
      if (!ok) return false;
      try { await api("/api/toolkit/cert/pin", { sha256: c.sha256 }); toast("Certificate pinned"); }
      catch (e) { await message("Not pinned", e.message, true); return false; }
      await loadMeta();
      return true;
    },
    async unpin() {
      if (!await ask("Remove pin", "Stop accepting the pinned certificate? Connections will then need a certificate from a trusted CA.", "Remove", true)) return;
      try { await api("/api/toolkit/cert/unpin", {}); toast("Pin removed"); } catch (e) { await message("Error", e.message, true); }
      await loadMeta();
    },
    async forget() {
      if (!await ask("Forget password", "Remove the Toolkit's SQL password from Windows Credential Manager?", "Forget", true)) return;
      try { await api("/api/toolkit/forget", {}); toast("Password removed"); } catch (e) { await message("Error", e.message, true); }
      await loadMeta();
    },
    shown() { this.fill(); },
  };

  // ---- register --------------------------------------------------------------------------------

  CC.registerTab({ id: "toolkit", label: "Toolkit", before: "websites", render, onShow });
  CC.addPaletteSource(() => [
    ...VIEWS.map(([id, label]) => ({ group: "Toolkit", label: `Toolkit: ${label}`, hint: "tab", run() { CC.openTab("toolkit"); setView(id); } })),
    { group: "Toolkit", label: "Toolkit: Add Logi user", hint: "Users, Roles, Rights, tree access", run() { CC.openTab("toolkit"); setView("logi"); views.logi.addUser(); } },
    { group: "Toolkit", label: "Toolkit: SPS carrier lookup", hint: "EDI PO → carrier", run() { CC.openTab("toolkit"); setView("shipping"); views.shipping.spsInput.focus(); } },
  ]);
})();
