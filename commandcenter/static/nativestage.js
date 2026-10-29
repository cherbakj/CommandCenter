// Native windows over a tab (the Pulse site, the docked SSMS): the desktop app lays a window of its own over a tab's
// empty stage (overlay.py). A native window always paints above this page, so this tells the window where the stage
// is, in CSS px, and when Command Center UI has to be on top: a dialog, the palette, the splash or the restart overlay
// hide it; a toast or other floating piece over it cuts a hole (or hides it when it covers most of it).
//   const s = CC.nativeStage({ view: "pulse", section, hole, available: () => bool, site: () => "prod" });
//   s.schedule()  after anything the stage's own observers can't see (a site switch, the status arriving)
// Reports go to POST /api/window/<view>/bounds: page id + sequence number, at most one per 50 ms while the stage
// moves (hiding is never held back), re-checked every second.
(function () {
  "use strict";
  if (!window.CC) return;

  // Full-window Command Center UI: the view hides while any of it is up.
  const COVERS = "dialog[open], .restart-overlay, .sp-overlay, [aria-modal='true'], #palette.open";
  const SKIP = new Set(["HEADER", "MAIN", "SCRIPT", "STYLE", "LINK", "DIALOG", "TEMPLATE", "NOSCRIPT"]);
  const MARGIN = 6;  // px around a floating piece (its shadow)
  const page = Math.random().toString(36).slice(2, 12);
  const stages = [];

  const overlap = (a, b) => {
    const w = Math.min(a.right, b.right) - Math.max(a.left, b.left), h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
    return w > 0 && h > 0 ? w * h : 0;
  };

  // Floating pieces drawn over the page: toasts and anything else positioned at the top level, plus open popovers.
  function floating(hole) {
    const out = [];
    let open = [];
    try { open = [...document.querySelectorAll(":popover-open")]; } catch { /* no popover support */ }
    for (const n of [...document.body.children, ...open]) {
      if (SKIP.has(n.tagName) && !open.includes(n)) continue;
      if (n.id === "palette" || n.contains(hole)) continue;
      const cs = getComputedStyle(n);
      if (cs.display === "none" || cs.visibility === "hidden" || !/^(fixed|absolute|sticky)$/.test(cs.position)) continue;
      // a fade in / out counts as shown until it has finished
      if (parseFloat(cs.opacity) < 0.02 && !n.getAnimations().some((a) => a.playState === "running")) continue;
      const r = n.getBoundingClientRect();
      if (r.width < 1 || r.height < 1) continue;
      out.push(r);
    }
    return out;
  }

  function measure(s) {
    const hidden = { visible: false };
    const { section, hole } = s.opts;
    if (!hole || document.visibilityState !== "visible") return hidden;
    if (!section.classList.contains("active") || hole.offsetParent === null) return hidden;
    if (s.opts.shown && !s.opts.shown()) return hidden;
    if (document.querySelector(COVERS)) return hidden;
    const r = hole.getBoundingClientRect();
    const vw = document.documentElement.clientWidth, vh = document.documentElement.clientHeight;
    const box = { left: Math.max(0, r.left), top: Math.max(0, r.top), right: Math.min(vw, r.right), bottom: Math.min(vh, r.bottom) };
    const area = (box.right - box.left) * (box.bottom - box.top);
    if (box.right - box.left < 40 || box.bottom - box.top < 40) return hidden;
    const cuts = [];
    for (const f of floating(hole)) {
      const o = overlap(box, { left: f.left - MARGIN, top: f.top - MARGIN, right: f.right + MARGIN, bottom: f.bottom + MARGIN });
      if (!o) continue;
      if (o > area * 0.6 || cuts.length >= 12) return hidden;  // mostly covered: just hide it
      cuts.push([f.left - MARGIN, f.top - MARGIN, f.width + 2 * MARGIN, f.height + 2 * MARGIN]);
    }
    return { visible: true, rect: [box.left, box.top, box.right - box.left, box.bottom - box.top], cuts };
  }

  function send(s, m, keepalive = false) {
    return fetch(`/api/window/${s.opts.view}/bounds`, {
      method: "POST", keepalive, headers: { "Content-Type": "application/json", "X-CommandCenter": "1" },
      body: JSON.stringify({ page, seq: ++s.seq, site: s.opts.site?.(), visible: !!m.visible, rect: m.rect, cuts: m.cuts }),
    }).then((r) => { if (!r.ok) throw new Error(String(r.status)); });
  }

  // At most one report per 50 ms while it moves (splitter drags); hiding is never held back.
  function flush(s) {
    s.frame = 0;
    if (!s.opts.available()) return;  // no native window (browser / serve mode): nothing to tell the host
    const m = measure(s);
    s.shown = !!m.visible;
    // While one is on show, the page's background motion holds still (style.css: it re-blurs the whole window)
    const any = stages.some((x) => x.shown), root = document.documentElement;
    if (root.classList.contains("cc-native-shown") !== any) root.classList.toggle("cc-native-shown", any);
    const round = (a) => a.map((n) => Math.round(n * 10) / 10);
    const key = JSON.stringify([s.opts.site?.() || "", m.visible, m.visible ? round(m.rect) : 0, m.visible ? m.cuts.map(round) : 0]);
    if (key === s.last) return;
    clearTimeout(s.timer);
    const wait = m.visible ? 50 - (performance.now() - s.sent) : 0;
    if (wait > 0) { s.timer = setTimeout(() => flush(s), wait); return; }
    s.last = key;
    s.sent = performance.now();
    send(s, m).catch(() => { s.last = ""; });  // try again on the next change or tick
  }

  function scheduleAll() {
    for (const s of stages) if (!s.frame) s.frame = requestAnimationFrame(() => flush(s));
  }

  let watching = false;
  function watch() {
    if (watching) return;
    watching = true;
    window.addEventListener("resize", scheduleAll);
    document.addEventListener("cc:layout", scheduleAll);
    document.addEventListener("visibilitychange", scheduleAll);
    document.addEventListener("scroll", scheduleAll, { capture: true, passive: true });
    for (const t of ["transitionrun", "transitionend", "transitioncancel", "animationstart", "animationend"]) {
      document.addEventListener(t, scheduleAll, { capture: true, passive: true });
    }
    new MutationObserver(scheduleAll).observe(document.documentElement, {
      subtree: true, childList: true, attributes: true, attributeFilter: ["open", "class", "hidden", "style", "aria-modal"],
    });
    let dpr = matchMedia(`(resolution: ${devicePixelRatio}dppx)`);
    const onDpr = () => { dpr.removeEventListener("change", onDpr); dpr = matchMedia(`(resolution: ${devicePixelRatio}dppx)`); dpr.addEventListener("change", onDpr); scheduleAll(); };
    dpr.addEventListener("change", onDpr);
    setInterval(scheduleAll, 1000);  // catches anything the events above miss
    window.addEventListener("pagehide", () => {
      for (const s of stages) if (s.opts.available()) send(s, { visible: false }, true).catch(() => {});
    });
  }

  CC.nativeStage = (opts) => {
    const s = { opts, seq: 0, last: "", sent: 0, frame: 0, timer: 0, shown: false };  // shown: on show now
    s.schedule = () => { if (!s.frame) s.frame = requestAnimationFrame(() => flush(s)); };
    stages.push(s);
    new ResizeObserver(s.schedule).observe(opts.hole);
    watch();
    return s;
  };
})();
