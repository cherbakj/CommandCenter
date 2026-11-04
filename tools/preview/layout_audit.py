"""Layout audit: every tab of the preview (sample data, no server) at laptop and monitor sizes, with Joe's settings.

Reports, per size and splitter position:
  spill    an element whose content pokes out of its own box (it doesn't clip or scroll), e.g. the weather's place
           switch sticking out of its panel on a squeezed Today strip (2026-11-04)
  overlap  two cards / blocks side by side that cover each other
  hscroll  the page scrolls sideways
  header   the header taller than one row (it must stay one row: app.js fitHeader)
  errors   page errors
Exit code 1 if any were found. Screenshots of every tab go to out/audit/ (git-ignored, with the rest of out/).

Run (builds the preview first):
  C:\\Sonance\\internal\\CommandCenter\\.venv\\Scripts\\python.exe tools\\preview\\layout_audit.py
  ... --sizes joe,2k150 --tabs today,mail --splits 0.44,0.62 --scale 1 --no-compact --no-build
"""
import argparse
import json
import runpy
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
PAGE = HERE / "out" / "preview.html"
SHOTS = HERE / "out" / "audit"
BASE = "https://preview.invalid/"
ALLOWED = ("https://fonts.googleapis.com/", "https://fonts.gstatic.com/")
SKELETON = ('<!doctype html><html><head><meta charset=utf8><meta name=viewport content="width=device-width,initial-scale=1">'
            '<style>body{margin:0}[hidden]{display:none!important}</style></head><body>\n')
# name: (css width, css height, device scale). The desktop window fills the work area (screen minus the 48 px taskbar).
SIZES = {
    "joe": (1920, 1032, 1),          # Joe's 1920x1080 screen, full screen
    "2k150": (1707, 928, 1.5),       # a 2560x1440 laptop at 150%
    "2k125": (2048, 1114, 1.25),     # ... at 125%
    "1536": (1536, 824, 1.25),       # a 1080p laptop at 125%
    "1366": (1366, 728, 1),
    "1440p": (2560, 1392, 1),
    "uw": (3440, 1392, 1),           # the 3440x1440 ultrawide
}
HEADER_MAX = 80  # px: one row is 46-72 depending on text size

AUDIT = r"""() => {
  const out = [];
  const vis = (e) => { const r = e.getBoundingClientRect(); if (!r.width || !r.height) return false;
    const s = getComputedStyle(e); return s.visibility !== "hidden" && s.display !== "none" && +s.opacity > 0.05; };
  const name = (e) => (e.id ? "#" + e.id : e.tagName.toLowerCase()) + (typeof e.className === "string" && e.className.trim() ? "." + e.className.trim().split(/\s+/).slice(0, 3).join(".") : "");
  const path = (e) => { const p = []; for (let x = e; x && x !== document.body && p.length < 4; x = x.parentElement) p.unshift(name(x)); return p.join(" > "); };
  const clips = (e) => { const s = getComputedStyle(e); return s.overflowX !== "visible" || s.overflowY !== "visible"; };
  const skip = (e) => e.closest("svg, dialog:not([open]), [hidden], .tab:not(.active)");
  for (const e of document.querySelectorAll("body *")) {
    if (!vis(e) || clips(e) || skip(e)) continue;
    const s = getComputedStyle(e);
    if (s.display === "inline") continue;
    const dx = e.scrollWidth - e.clientWidth, dy = e.scrollHeight - e.clientHeight;
    if (!((dx > 3 && e.clientWidth > 0) || (dy > 3 && e.clientHeight > 0 && s.height !== "auto"))) continue;
    const r = e.getBoundingClientRect();
    const kid = [...e.querySelectorAll("*")].find((k) => { if (!vis(k) || getComputedStyle(k).position === "fixed") return false;
      const c = k.getBoundingClientRect(); return c.right > r.right + 3 || c.bottom > r.bottom + 3 || c.left < r.left - 3; });
    if (kid) out.push({ kind: "spill", where: path(e), child: name(kid) });
  }
  for (const e of document.querySelectorAll("body *")) {
    if (!vis(e) || skip(e)) continue;
    const kids = [...e.children].filter((k) => vis(k) && !["absolute", "fixed"].includes(getComputedStyle(k).position));
    for (let i = 0; i < kids.length; i++) for (let j = i + 1; j < kids.length; j++) {
      const a = kids[i].getBoundingClientRect(), b = kids[j].getBoundingClientRect();
      if (Math.min(a.right, b.right) - Math.max(a.left, b.left) > 4 && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 4)
        out.push({ kind: "overlap", where: path(e), child: `${name(kids[i])} / ${name(kids[j])}` });
    }
  }
  const de = document.documentElement;
  if (de.scrollWidth > de.clientWidth + 1) out.push({ kind: "hscroll", where: `${de.scrollWidth} > ${de.clientWidth}` });
  const h = document.querySelector("body > header");
  if (h.offsetHeight > %d) out.push({ kind: "header", where: `${h.offsetHeight}px tall (${h.className})` });
  return out;
}""" % HEADER_MAX

# As the desktop app shows it: no preview chip or note, window buttons on, Joe's text size and density.
SETUP = """([scale, compact]) => { const r = document.documentElement; r.style.setProperty('--pf-scale', String(scale));
  r.classList.toggle('pf-compact', compact); document.querySelectorAll('.preview-chip, #preview-note').forEach((n) => n.remove());
  document.querySelectorAll('dialog[open]').forEach((d) => d.close()); document.querySelector('#win-controls').hidden = false;
  window.dispatchEvent(new Event('resize')); }"""


def run(args) -> int:
    if not args.no_build:
        sys.argv = [str(HERE / "build.py")]
        runpy.run_path(str(HERE / "build.py"), run_name="__main__")
    html = SKELETON + PAGE.read_text(encoding="utf-8") + "\n</body></html>"
    SHOTS.mkdir(parents=True, exist_ok=True)
    found = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        for size in args.sizes.split(","):
            w, h, dpr = SIZES[size]
            for split in args.splits.split(","):
                ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=dpr)
                ctx.add_init_script(f"localStorage.setItem('cc.layout.split', '{split}'); localStorage.setItem('cc-splash-off', '1')")
                page = ctx.new_page()
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)[:200]))
                page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
                           if route.request.url == BASE else route.continue_() if route.request.url.startswith(ALLOWED) else route.abort())
                page.goto(BASE)
                page.wait_for_timeout(2500)
                page.evaluate(SETUP, [args.scale, not args.no_compact])
                page.wait_for_timeout(600)
                tabs = args.tabs.split(",") if args.tabs else page.evaluate(
                    "[...document.querySelectorAll('.nav-group button[data-tab]')].map((b) => b.dataset.tab)")
                problems = {}
                for tab in tabs:
                    page.evaluate("(t) => document.querySelector(`.nav-group button[data-tab='${t}']`)?.click()", tab)
                    page.wait_for_timeout(700)
                    page.screenshot(path=str(SHOTS / f"{size}-{split}-{tab}.png"))
                    issues = page.evaluate(AUDIT)
                    if issues:
                        problems[tab] = issues
                if errors:
                    problems["errors"] = errors
                n = sum(len(v) for v in problems.values())
                found += n
                print(f"{size} split {split}: {'OK' if not n else f'{n} problem(s)'}")
                for tab, issues in problems.items():
                    for i in issues:
                        print(f"   {tab}: {json.dumps(i, ensure_ascii=False) if isinstance(i, dict) else i}")
                ctx.close()
        browser.close()
    print(f"Screenshots: {SHOTS}")
    return 1 if found else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sizes", default=",".join(SIZES), help=f"comma list of {', '.join(SIZES)}")
    ap.add_argument("--tabs", default="", help="comma list of tab ids (default: every tab)")
    ap.add_argument("--splits", default="0.44,0.62", help="COMMS share of the width (splitter positions)")
    ap.add_argument("--scale", type=float, default=0.85, help="Profile text size (Joe: 0.85)")
    ap.add_argument("--no-compact", action="store_true", help="normal density (Joe: compact)")
    ap.add_argument("--no-build", action="store_true", help="use out/preview.html as it is")
    sys.exit(run(ap.parse_args()))
