// Pulse tab: feedback tickets from pulse.sonance.com (read-only). Built only with CC.el
// (text nodes), never innerHTML, because ticket text is typed by other people.
(function () {
  "use strict";
  if (!window.CC) return;
  const { el, api, guarded, fill, when, showDialog, showMessage, confirmDialog, openExternal } = CC;

  const css = document.createElement("link");
  css.rel = "stylesheet";
  css.href = "/static/pulse.css";
  document.head.append(css);

  const state = { filter: "mine", search: "", tickets: [], status: null, loading: false };
  let root, body, timer, searchTimer;

  const FILTERS = [["mine", "Mine"], ["open", "Open"], ["all", "All"]];
  const STATUS_TAG = { "Open": "p-open", "Waiting on reporter": "p-wait", "On hold": "p-hold", "Resolved": "p-done" };
  const TYPE_LABEL = { Bug: "Bug", DataIssue: "Data issue", FeatureRequest: "Request", Feedback: "Feedback", DataReview: "Review" };

  function age(iso) {
    if (!iso) return "";
    const mins = Math.max(0, (Date.now() - new Date(iso)) / 60000);
    if (mins < 60) return `${Math.round(mins)}m`;
    if (mins < 60 * 24) return `${Math.round(mins / 60)}h`;
    const days = Math.round(mins / 1440);
    return days < 60 ? `${days}d` : `${Math.round(days / 30)}mo`;
  }

  function render(section) {
    root = section;
    body = el("div", { class: "pulse-body" });
    section.append(body);
  }

  async function load() {
    clearTimeout(timer);
    try {
      state.status = await api("/api/pulse/status");
    } catch (e) {
      body.replaceChildren(el("div", { class: "panel" }, el("div", { class: "err" }, e.message)));
      return;
    }
    if (!state.status.configured) { renderSetup(); return; }
    await loadTickets();
    timer = setTimeout(() => { if (root.classList.contains("active")) load(); }, 60000);
  }

  async function loadTickets() {
    const p = new URLSearchParams({ status: state.filter === "all" ? "all" : "open", limit: "200" });
    if (state.filter === "mine") p.set("mine", "true");
    if (state.search) p.set("search", state.search);
    state.loading = true;
    renderList();
    try {
      const data = await api(`/api/pulse/tickets?${p}`);
      if (!data.configured) { renderSetup(); return; }
      state.tickets = data.tickets;
      state.error = null;
    } catch (e) {
      state.error = e.message;
    }
    state.loading = false;
    renderList();
  }

  // ---- ticket list ----------------------------------------------------------

  function toolbar() {
    const search = el("input", {
      type: "search", class: "pulse-search", placeholder: "Search tickets, #number, reporter…", value: state.search, maxlength: "100",
      oninput: (e) => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => { state.search = e.target.value.trim(); loadTickets(); }, 350);
      },
    });
    const seg = el("div", { class: "pulse-seg", role: "group", "aria-label": "Filter" },
      FILTERS.map(([k, label]) => el("button", {
        class: `small ${state.filter === k ? "primary" : "subtle"}`, "aria-pressed": String(state.filter === k),
        onclick: () => { state.filter = k; loadTickets(); },
      }, label)));
    const st = state.status || {};
    return el("div", { class: "toolbar" }, seg, search, el("span", { class: "spacer" }),
      st.error ? el("span", { class: "err", title: st.error }, "Last poll failed") :
        st.last_ok ? el("span", { class: "muted" }, `Polled ${when(st.last_ok)}`) : null,
      el("button", { class: "small", onclick: () => loadTickets() }, "↻ Refresh"),
      el("button", { class: "small alt", onclick: () => openExternal(`${st.url || "https://pulse.sonance.com"}/feedback`) }, "Open Pulse"),
      el("button", { class: "small subtle", onclick: settingsDialog }, "Settings"));
  }

  function row(t) {
    const pr = t.priority === "high" ? el("span", { class: "tag overdue" }, "High")
      : t.priority === "normal" ? el("span", { class: "tag p-normal" }, "Normal") : el("span", { class: "tag p-low" }, "Low");
    return el("li", { class: `pulse-row${t.mine && t.status === "Open" ? " pulse-mine" : ""}` },
      el("div", { class: "pulse-no" }, t.ticket_no != null ? `#${t.ticket_no}` : "—"),
      el("div", { class: "grow" },
        el("div", { class: "title" }, t.title,
          el("span", { class: `tag ${STATUS_TAG[t.status] || ""}` }, t.status), pr,
          el("span", { class: "tag p-type" }, TYPE_LABEL[t.type] || t.type || "")),
        t.detail && t.detail !== t.title ? el("div", { class: "pulse-detail" }, t.detail) : null,
        el("div", { class: "muted" }, [
          t.requester && `From ${t.requester}`,
          t.with && `With ${t.with}`,
          t.where,
          t.created_at && `Opened ${when(t.created_at)} (${age(t.created_at)} ago)`,
          t.resolved_by && `Closed by ${t.resolved_by}`,
        ].filter(Boolean).join(" · "))),
      el("div", { class: "actions" },
        el("button", { class: "small primary", onclick: () => openExternal(t.url) }, "Open in Pulse")));
  }

  function renderList() {
    if (!body || !state.status?.configured) return;
    const list = el("ul", { class: "list panel-list pulse-list" });
    if (state.error) fill(list, [el("li", { class: "err" }, state.error)], "");
    else if (state.loading && !state.tickets.length) fill(list, [], "Loading tickets…");
    else fill(list, state.tickets.map(row), state.search ? "No tickets match that search." :
      state.filter === "mine" ? "Nothing waiting on you. Inbox zero." : "No tickets.");
    const counts = el("span", { class: "chip" }, `${state.tickets.length} TICKET${state.tickets.length === 1 ? "" : "S"}`);
    body.replaceChildren(toolbar(),
      el("div", { class: "panel pulse-panel" },
        el("div", { class: "panel-head" }, el("span", { class: "panel-label" }, "PULSE // FEEDBACK TICKETS"), counts,
          state.loading ? el("span", { class: "muted" }, "refreshing…") : null),
        list));
  }

  // ---- setup ----------------------------------------------------------------

  function field(label, input, hint) {
    return el("label", { class: "pulse-field" }, el("span", { class: "pulse-flabel" }, label), input,
      hint ? el("span", { class: "muted" }, hint) : null);
  }

  function setupForm(st, onDone) {
    const f = {
      host: el("input", { value: st.host || "", placeholder: "aws-0-us-west-1.pooler.supabase.com", autocomplete: "off", spellcheck: "false" }),
      port: el("input", { value: String(st.port || 5432), inputmode: "numeric", autocomplete: "off" }),
      database: el("input", { value: st.database || "postgres", autocomplete: "off" }),
      user: el("input", { value: st.user || "", placeholder: "commandcenter_ro.<project ref>", autocomplete: "off", spellcheck: "false" }),
      password: el("input", { type: "password", autocomplete: "new-password", placeholder: st.configured ? "(unchanged — leave empty to re-test)" : "" }),
      ssl_root_cert: el("input", { value: st.ssl_root_cert || "system", autocomplete: "off", spellcheck: "false" }),
    };
    const msg = el("div", { class: "muted pulse-msg" });
    const connect = el("button", {
      type: "button", class: "primary",
      onclick: async () => {
        connect.disabled = true;
        msg.className = "muted pulse-msg";
        msg.textContent = "Connecting (read-only)…";
        try {
          await api("/api/pulse/setup", Object.fromEntries(Object.entries(f).map(([k, v]) => [k, v.value])));
          f.password.value = "";
          msg.className = "good pulse-msg";
          msg.textContent = "Connected.";
          onDone?.();
        } catch (e) {
          msg.className = "err pulse-msg";
          msg.textContent = e.message;
        } finally {
          f.password.value = "";
          connect.disabled = false;
        }
      },
    }, st.configured ? "Save and test" : "Connect");
    return el("div", { class: "pulse-form" },
      field("Host", f.host, "Supabase → Connect → Session pooler host (no https://)"),
      el("div", { class: "pulse-two" }, field("Port", f.port), field("Database", f.database)),
      field("User", f.user, "The read-only login from the setup SQL, e.g. commandcenter_ro.<project ref>"),
      field("Password", f.password, "Stored only in Windows Credential Manager"),
      field("CA certificate", f.ssl_root_cert, "'system', or the path of Supabase's CA file (Database settings → SSL)"),
      el("div", { class: "row" }, el("button", { type: "button", class: "subtle", onclick: () => sqlDialog(st) }, "Setup SQL"), connect),
      msg);
  }

  function sqlDialog(st) {
    const pre = el("pre", { class: "pulse-sql" }, st.setup_sql || "");
    (CC.wideDialog || showDialog)(el("h3", {}, "Read-only login for Pulse"),
      el("p", { class: "muted" }, "Run this once in the Supabase SQL editor for the production Pulse project (with a long random password). " +
        "It can only read the ticket columns Command Center shows; it cannot change anything."),
      pre,
      el("div", { class: "row" },
        el("button", { type: "button", onclick: () => navigator.clipboard?.writeText(st.setup_sql || "") }, "Copy"),
        el("button", { class: "primary" }, "Close")));
  }

  function renderSetup() {
    const st = state.status || {};
    body.replaceChildren(el("div", { class: "pulse-setup" },
      el("div", { class: "panel" },
        el("div", { class: "panel-head" }, el("span", { class: "panel-label" }, "CONNECT PULSE"), el("span", { class: "chip" }, "READ-ONLY")),
        el("p", {}, "Shows your Pulse feedback tickets here and alerts you when one lands on you or changes."),
        el("ol", { class: "pulse-steps" },
          el("li", {}, "Create the read-only login: click ", el("b", {}, "Setup SQL"), ", run it in Supabase (production Pulse project)."),
          el("li", {}, "Copy the Session pooler host from Supabase → Connect, and use ", el("b", {}, "commandcenter_ro.<project ref>"), " as the user."),
          el("li", {}, "Enter the password below. It's kept in Windows Credential Manager only.")),
        !st.driver ? el("p", { class: "err" }, "The Postgres driver isn't installed: .venv\\Scripts\\python.exe -m pip install \"psycopg[binary]\"") : null,
        setupForm(st, () => load()))));
  }

  function settingsDialog() {
    const st = state.status || {};
    showDialog(el("h3", {}, "Pulse connection"),
      el("p", { class: "muted" }, `Connected as ${st.user} on ${st.host}. Polls every ${st.poll_minutes} min.`,
        st.error ? el("span", { class: "err" }, ` Last error: ${st.error}`) : null),
      setupForm(st, () => { document.querySelector("#dialog")?.close(); load(); }),
      el("div", { class: "row" },
        el("button", {
          type: "button", class: "subtle", onclick: async () => {
            document.querySelector("#dialog")?.close();
            if (await confirmDialog("Disconnect Pulse?", "Removes the saved password from Credential Manager.", "Disconnect")) {
              await guarded(async () => { await api("/api/pulse/disconnect", {}); state.tickets = []; await load(); });
            }
          },
        }, "Disconnect"),
        el("button", {}, "Close")));
  }

  CC.registerTab({ id: "pulse", label: "Pulse", render, onShow: () => guarded(load), before: "websites" });

  // "Go to Pulse" comes from the core palette (one entry per nav tab), so it isn't repeated here.
  CC.addPaletteSource(() => [
    ...state.tickets.slice(0, 50).map((t) => ({
      group: "Pulse", label: `#${t.ticket_no} ${t.title}`, hint: `${t.status} · ${t.requester || ""}`,
      run: () => openExternal(t.url),
    })),
  ]);

  // Prime the palette with the tickets waiting on Joe, quietly.
  api("/api/pulse/tickets?mine=true&status=open&limit=100")
    .then((d) => { if (d.configured && !state.tickets.length) state.tickets = d.tickets; })
    .catch(() => {});
})();
