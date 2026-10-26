// Launch splash: jigsaw pieces fly in and lock into the Command Center logo, then a sync pass checks each
// connected system, then the app is revealed. Plays every time the dashboard loads (launch, restart, unlock);
// click or Esc skips it.
// Gate mode (window.CC_GATE, set by the desktop's start-up page, desktop.py _gate_page): the same pieces assemble
// behind the Windows Hello prompt, then it waits ("SIGN IN"); no API calls, nothing to skip. After Hello the
// dashboard opens at /#signed-in and its splash starts assembled and goes straight to the sync pass.
// Built only with DOM/SVG APIs (no innerHTML, no inline style attributes) so it works under the app's CSP.
(() => {
  const SVG = "http://www.w3.org/2000/svg";
  const COLS = 4, ROWS = 3, S = 110;               // puzzle grid and piece size (SVG units)
  const W = COLS * S, H = ROWS * S;
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const gate = window.CC_GATE === true;
  const signedIn = !gate && location.hash === "#signed-in";
  if (signedIn) history.replaceState(null, "", location.pathname + location.search);
  if (!gate) {
    try {
      if (localStorage.getItem("cc-splash-off")) return;  // Profile > Launch animation off
    } catch { /* storage blocked: play it */ }
  }

  const svgEl = (tag, attrs = {}, ...kids) => {
    const n = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    kids.forEach((k) => n.append(k));
    return n;
  };
  const div = (cls, ...kids) => { const n = document.createElement("div"); n.className = cls; kids.forEach((k) => n.append(k)); return n; };

  // ---- jigsaw geometry --------------------------------------------------------
  // Each internal edge gets a random tab direction; neighbours share it with opposite sign.
  const rnd = () => (Math.random() < 0.5 ? 1 : -1);
  const hTabs = Array.from({ length: ROWS - 1 }, () => Array.from({ length: COLS }, rnd)); // bottom edge of row r
  const vTabs = Array.from({ length: ROWS }, () => Array.from({ length: COLS - 1 }, rnd)); // right edge of col c

  // One edge from (x0,y0) to (x1,y1), with a knob pushed outward (tab=1), inward (-1) or flat (0).
  function edge(x0, y0, x1, y1, tab) {
    if (!tab) return `L${x1},${y1}`;
    const len = Math.hypot(x1 - x0, y1 - y0), ux = (x1 - x0) / len, uy = (y1 - y0) / len;
    const nx = uy * tab, ny = -ux * tab;           // outward normal for a clockwise outline (y down)
    const p = (t, s) => `${(x0 + ux * t * len + nx * s * len).toFixed(1)},${(y0 + uy * t * len + ny * s * len).toFixed(1)}`;
    return `L${p(0.34, 0)} C${p(0.3, 0.2)} ${p(0.38, 0.29)} ${p(0.5, 0.29)} C${p(0.62, 0.29)} ${p(0.7, 0.2)} ${p(0.66, 0)} L${p(1, 0)}`;
  }

  function piecePath(r, c) {
    const x = c * S, y = r * S;
    const top = r === 0 ? 0 : -hTabs[r - 1][c];
    const right = c === COLS - 1 ? 0 : vTabs[r][c];
    const bottom = r === ROWS - 1 ? 0 : hTabs[r][c];
    const left = c === 0 ? 0 : -vTabs[r][c - 1];
    return `M${x},${y} ${edge(x, y, x + S, y, top)} ${edge(x + S, y, x + S, y + S, right)} ` +
           `${edge(x + S, y + S, x, y + S, bottom)} ${edge(x, y + S, x, y, left)} Z`;
  }

  // ---- artwork revealed by the assembled puzzle ----------------------------------
  function artwork() {
    const g = svgEl("g", { class: "sp-art" });
    g.append(svgEl("rect", { x: 0, y: 0, width: W, height: H, class: "sp-art-bg" }));
    for (let i = 1; i < 11; i++) g.append(svgEl("line", { x1: i * W / 11, y1: 0, x2: i * W / 11, y2: H, class: "sp-art-grid" }));
    for (let i = 1; i < 8; i++) g.append(svgEl("line", { x1: 0, y1: i * H / 8, x2: W, y2: i * H / 8, class: "sp-art-grid" }));
    // circuit traces
    for (const d of ["M20,250 H120 L150,220 H210", "M420,80 H330 L300,110 H250", "M40,60 H110 L140,90", "M400,270 H320 L290,240"]) {
      g.append(svgEl("path", { d, class: "sp-art-trace" }));
    }
    // the JC monogram (same geometry as static/jc-logo.svg and the app icon), centered
    const s = 8.2, ox = W / 2 - 16 * s, oy = H / 2 - 16 * s;
    const shape = (pts, cls) => svgEl("polygon", { points: pts.map(([px, py]) => `${ox + px * s},${oy + py * s}`).join(" "), class: cls });
    g.append(shape([[4, 4], [15, 4], [15, 8], [12.5, 8], [12.5, 23], [8, 28], [2.5, 28], [2.5, 24], [6.5, 24], [8.5, 22], [8.5, 8], [4, 8]], "sp-logo-a"));
    g.append(shape([[29.5, 4], [20.5, 4], [16.5, 8], [16.5, 24], [20.5, 28], [29.5, 28], [29.5, 24], [22.5, 24], [20.5, 22], [20.5, 10], [22.5, 8], [29.5, 8]], "sp-logo-b"));
    return g;
  }

  // ---- build the overlay ------------------------------------------------------------
  const svg = svgEl("svg", { viewBox: `-60 -60 ${W + 120} ${H + 120}`, class: "sp-puzzle", "aria-hidden": "true" });
  const defs = svgEl("defs");
  svg.append(defs);
  const pieces = [];
  for (let r = 0; r < ROWS; r++) {
    for (let c = 0; c < COLS; c++) {
      const id = `sp-clip-${r}-${c}`, d = piecePath(r, c);
      defs.append(svgEl("clipPath", { id }, svgEl("path", { d })));
      const g = svgEl("g", { class: "sp-piece" });
      g.append(svgEl("g", { "clip-path": `url(#${id})` }, artwork()));
      g.append(svgEl("path", { d, class: "sp-edge" }));
      g.style.transformOrigin = `${c * S + S / 2}px ${r * S + S / 2}px`;
      svg.append(g);
      pieces.push({ g, r, c });
    }
  }

  const title = div("sp-title", "COMMAND", Object.assign(document.createElement("span"), { textContent: "//" }), "CENTER");
  const phase = div("sp-phase", "ASSEMBLING");
  const fill = div("sp-bar-fill");
  const bar = div("sp-bar", fill);
  const systems = div("sp-systems");
  const skip = div("sp-skip", gate ? "" : "Click or press Esc to skip");
  const overlay = div("sp-overlay", div("sp-stage", svg, title, phase, bar, systems), skip);
  overlay.setAttribute("role", "status");
  overlay.setAttribute("aria-label", "Command Center is starting");
  if (gate) overlay.classList.add("gate");

  const mount = () => document.body.prepend(overlay);
  document.body ? mount() : document.addEventListener("DOMContentLoaded", mount, { once: true });

  // ---- sync checks (real, read-only GETs; each capped so a slow system can't hold the app) ----
  const getJson = (url, ms = 3500) => Promise.race([
    fetch(url, { credentials: "same-origin" }).then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.status)))),
    new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), ms)),
  ]);

  const CHECKS = [
    ["Outlook", async () => {
      for (let i = 0; i < 6; i++) {          // the watcher's first check can take a moment after launch
        const st = await getJson("/api/status");
        if (st.outlook) return st.outlook.ok ? "ok" : "warn";
        await new Promise((r) => setTimeout(r, 400));
      }
      return "warn";
    }],
    ["Calendar", async () => (Array.isArray((await getJson("/api/meetings?hours=12")).meetings) ? "ok" : "warn")],
    ["Teams", async () => { const t = (await getJson("/api/status")).teams; return t ? (t.ok ? "ok" : "warn") : "off"; }],
    ["Pulse", async () => { const p = await getJson("/api/pulse/status"); return p.configured ? (p.error || p.last_error ? "warn" : "ok") : "off"; }],
    ["Toolkit", async () => { const t = await getJson("/api/toolkit/settings"); return t.configured && t.password_saved ? "ok" : "off"; }],
    ["Asana", async () => ((await getJson("/api/status")).asana ? "ok" : "off")],
  ];
  const LABEL = { ok: "online", warn: "check", off: "not set up", err: "offline" };

  function runChecks() {
    let done = 0;
    return Promise.all(CHECKS.map(([name, check], i) => {
      const state = div("sp-sys-state", "…");
      const row = div("sp-sys pending", div("sp-sys-dot"), div("sp-sys-name", name), state);
      systems.append(row);
      return new Promise((r) => setTimeout(r, reduced ? 0 : i * 110))
        .then(check).catch(() => "err")
        .then((result) => {
          row.className = `sp-sys ${result}`;
          state.textContent = LABEL[result];
          fill.style.width = `${Math.round((++done / CHECKS.length) * 100)}%`;
        });
    }));
  }

  // ---- timeline ---------------------------------------------------------------------
  let finished = false;
  function finish() {
    if (finished) return;
    finished = true;
    const out = overlay.animate([{ opacity: 1 }, { opacity: 0 }], { duration: reduced ? 150 : 450, easing: "cubic-bezier(.4, 0, .2, 1)", fill: "forwards" });
    if (!reduced) {
      // The card lifts away softly while the app shows through.
      overlay.querySelector(".sp-stage").animate([{ transform: "scale(1)", filter: "blur(0)" }, { transform: "scale(1.05)", filter: "blur(6px)" }],
        { duration: 450, easing: "cubic-bezier(.4, 0, .2, 1)", fill: "forwards" });
    }
    out.onfinish = () => overlay.remove();
  }
  if (!gate) {
    overlay.addEventListener("click", finish);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" || e.key === "Enter") finish(); });
  }

  async function play() {
    if (reduced || signedIn) {
      overlay.classList.add("assembled");
    } else {
      const flights = pieces.map(({ g }, i) => {
        const angle = Math.random() * Math.PI * 2, dist = 700 + Math.random() * 400;
        const from = `translate(${Math.cos(angle) * dist}px, ${Math.sin(angle) * dist}px) rotate(${(Math.random() * 2 - 1) * 200}deg) scale(.55)`;
        const a = g.animate(
          [{ transform: from, opacity: 0 }, { opacity: 1, offset: 0.25 }, { transform: "translate(0,0) rotate(0) scale(1)", opacity: 1 }],
          { duration: 760 + Math.random() * 220, delay: 120 + i * 75, easing: "cubic-bezier(.18,1.25,.4,1)", fill: "backwards" });
        a.onfinish = () => g.classList.add("snapped");
        return a.finished;
      });
      await Promise.all(flights);
      overlay.classList.add("assembled");     // seams fade, glow pulse, title appears
      await new Promise((r) => setTimeout(r, 650));
    }
    if (gate) {  // wait for Windows Hello; the desktop replaces the page once it's done
      phase.textContent = "SIGN IN";
      overlay.classList.add("waiting");
      return;
    }
    phase.textContent = "SYNCING";
    overlay.classList.add("syncing");
    const started = Date.now();
    await runChecks();
    const minSync = reduced ? 300 : 900;      // let the sync read before the reveal
    await new Promise((r) => setTimeout(r, Math.max(0, minSync - (Date.now() - started))));
    phase.textContent = "READY";
    overlay.classList.add("ready");
    await new Promise((r) => setTimeout(r, reduced ? 100 : 420));
    finish();
  }

  play();
  if (!gate) setTimeout(finish, 12000);        // safety net: never block the app
})();
