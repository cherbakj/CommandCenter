// Keyboard shortcuts sheet: "?" anywhere in Command Center (not while typing or with a dialog open), or Ctrl+K
// "Keyboard shortcuts". A static list of what the app handles; the quick-capture hotkey is the configured one.
(() => {
  const CC = window.CC;
  if (!CC) return;
  const { el, api } = CC;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = "/static/keys.css";
  document.head.append(link);

  // k("Alt 1", "/", "Alt 2"): keys, with "/" as a muted "or".
  const k = (...keys) => el("span", { class: "ks-keys" }, keys.map((x) => (x === "/" ? el("span", { class: "ks-or" }, "or") : el("kbd", {}, x))));
  const row = (keys, what) => el("li", {}, keys, el("span", { class: "ks-what" }, what));

  function groups(captureKey) {
    return [
      ["Anywhere in Command Center", [
        row(k("?"), "This list"),
        row(k("Ctrl K"), "Search everything and run commands"),
        row(k("Shift T"), "Triage follow-ups, one at a time"),
        row(k("Ctrl Z"), "Undo the last dismiss, snooze, done, unflag or mark read (while its note shows)"),
        row(k("Ctrl Shift P"), "Privacy mode on / off"),
        row(k("F11"), "Full screen (desktop app)"),
        row(k("Esc"), "Close a dialog or menu"),
      ]],
      ["From any app", [
        captureKey ? row(k(captureKey), "Quick capture: a task, or a personal note") : row(el("span", { class: "muted" }, "off"), "Quick capture (ui.capture_hotkey)"),
      ]],
      ["Quick capture", [
        row(k("Enter"), "Save"),
        row(k("Alt 1", "/", "Alt 2"), "Task / Personal"),
        row(k("←", "/", "→"), "Switch the choice (when it has the focus)"),
      ]],
      ["Mail", [
        row(k("type"), "Search after 2 characters; Enter searches at once"),
        row(k("Esc"), "Clear the search"),
        row(k("↓"), "From the search box into the list"),
        row(k("↑", "/", "↓"), "Move along the list"),
        row(k("Enter"), "Open the email (on a conversation: its newest unread one)"),
        row(k("→", "/", "←"), "List a conversation's emails / close the list"),
      ]],
      ["Replying", [
        row(k("Ctrl Enter"), "Review the reply. Nothing sends without a click on Send"),
      ]],
      ["Triage", [
        row(k("R"), "Reply"),
        row(k("S"), "Snooze, then 1-3 to pick when"),
        row(k("D"), "Done"),
        row(k("T"), "Make a task"),
        row(k("O"), "Open in Outlook"),
        row(k("X"), "Spam (outside senders)"),
        row(k("J", "/", "→"), "Next"),
        row(k("K", "/", "←"), "Previous"),
        row(k("↑ ↓ PgUp PgDn Space"), "Scroll the email"),
        row(k("Esc"), "Close"),
      ]],
      ["Menus, lists and Ctrl K", [
        row(k("↑", "/", "↓"), "Move"),
        row(k("Home", "/", "End"), "First / last (menus)"),
        row(k("Enter"), "Run the highlighted item"),
        row(k("Esc"), "Close"),
      ]],
      ["Docked and site views", [
        row(el("span", { class: "ks-note" }, "VS Code, SSMS"), "After a click inside, keys are theirs. Click Command Center's header to use these again. In SSMS, Ctrl Shift P still toggles Privacy mode."),
        row(el("span", { class: "ks-note" }, "Pulse, Teams, Websites"), "Ctrl K, Ctrl Shift P and F11 work inside them."),
      ]],
    ];
  }

  async function open() {
    let captureKey = "Ctrl Alt Space";
    try {
      const st = await api("/api/status");
      if (st.ui && "capture_hotkey" in st.ui) captureKey = st.ui.capture_hotkey ? String(st.ui.capture_hotkey).replace(/\+/g, " ") : "";
    } catch { /* keep the default */ }
    CC.wideDialog(el("div", { class: "ks" },
      el("h3", {}, "Keyboard shortcuts"),
      el("div", { class: "ks-grid" }, groups(captureKey).map(([title, rows]) =>
        el("section", { class: "ks-group" }, el("h4", {}, title), el("ul", {}, rows)))),
      el("div", { class: "row" }, el("button", {}, "Close ", el("kbd", {}, "Esc")))));
  }

  document.addEventListener("keydown", (e) => {
    if (e.key !== "?" || e.ctrlKey || e.altKey || e.metaKey || e.repeat || e.defaultPrevented) return;
    const a = document.activeElement;
    if (a && (a.matches("input, textarea, select, [contenteditable], [contenteditable] *") || a.tagName === "IFRAME")) return;
    if (document.querySelector("dialog[open], .restart-overlay, .sp-overlay, [aria-modal='true'], #palette.open")) return;
    e.preventDefault();
    open();
  });

  CC.addPaletteSource(() => [{ group: "Commands", label: "Keyboard shortcuts", hint: "every shortcut · ?", run: () => setTimeout(open, 50) }]);
  CC.shortcuts = open;
})();
