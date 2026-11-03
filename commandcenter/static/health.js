// The health light's details (health.py): the header dot opens a list of every check, problems first, each with
// what it means and what to do. Ctrl+K "Health check" opens it too.
(() => {
  const CC = window.CC;
  if (!CC) return;
  const { el, api } = CC;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = "/static/health.css";
  document.head.append(link);

  const ORDER = { bad: 0, warn: 1, note: 2, ok: 3, off: 4 };
  const WORD = { bad: "Not working", warn: "Needs a look", note: "Note", ok: "OK", off: "Off" };
  // A check's action: where to go to see or fix it.
  const ACTIONS = {
    spam: ["Spam blocks", () => CC.spam?.manage()],
    teams: ["Teams tab", () => CC.openTab("teams")],
    "pulse-feedback": ["Pulse Feedback", () => CC.openTab("pulse-feedback")],
  };

  function row(c) {
    const act = c.action && ACTIONS[c.action];
    return el("li", { class: `hl-row hl-${c.state}` },
      el("span", { class: `dot ${c.state}`, "aria-hidden": "true" }),
      el("div", { class: "hl-main" },
        el("div", { class: "hl-head" }, el("b", {}, c.name), el("span", { class: "hl-word" }, WORD[c.state] || c.state)),
        el("div", { class: "hl-text" }, c.text),
        c.fix && c.state !== "ok" && el("div", { class: "hl-fix muted" }, c.fix)),
      act && c.state !== "ok" ? el("button", { type: "button", class: "small subtle", onclick: () => { CC.$("#dialog").close(); act[1](); } }, act[0]) : null);
  }

  async function open() {
    const d = CC.$("#dialog");
    if (!d.open) CC.wideDialog(el("div", { class: "hl" }, el("h3", {}, "Health"), el("p", { class: "muted" }, "Checking…")));
    let r;
    try { r = await api("/api/health"); } catch (e) {
      CC.showDialog(el("div", { class: "hl" }, el("h3", {}, "Health"), el("p", { class: "err" }, `Couldn't check: ${e.message}`), el("div", { class: "row" }, el("button", {}, "Close"))));
      return;
    }
    const list = r.checks.slice().sort((a, b) => (ORDER[a.state] ?? 5) - (ORDER[b.state] ?? 5));
    const head = r.issues ? `${r.issues} thing${r.issues === 1 ? "" : "s"} to look at` : "Everything's working";
    CC.wideDialog(el("div", { class: "hl" },
      el("h3", {}, "Health"),
      el("p", { class: `hl-sum hl-${r.state}` }, el("span", { class: `dot ${r.state}` }), head,
        el("span", { class: "muted" }, ` · checked ${CC.timeOf ? CC.timeOf(r.checked) : r.checked}`)),
      el("ul", { class: "hl-list" }, list.map(row)),
      el("div", { class: "row" },
        el("button", { type: "button", class: "small subtle", onclick: () => open() }, "Check again"),
        el("button", {}, "Close"))));
  }

  CC.addPaletteSource(() => [{ group: "Commands", label: "Health check", hint: "Outlook, Teams, Pulse, background jobs, disk…", run: () => setTimeout(open, 50) }]);
  CC.health = { open };
})();
