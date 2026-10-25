"""Headless Edge check of out/preview.html, wrapped in the artifact skeleton and served at a fake origin.

Every request is recorded; anything other than the page itself and Google Fonts is aborted and reported.
Screenshots go to out/ (shot-<viewport>-<step>.png).
Run:  C:\\Sonance\\internal\\CommandCenter\\.venv\\Scripts\\python.exe check.py
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
PAGE = HERE / "out" / "preview.html"
BASE = "https://preview.invalid/"
SKELETON = ('<!doctype html><html><head><meta charset=utf8><meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">'
            '<style>:root{color-scheme:light;box-sizing:border-box;padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}'
            'html{scroll-padding-top:env(safe-area-inset-top,0px)}body{margin:0;padding:0;font:14px -apple-system,BlinkMacSystemFont,sans-serif;background:#fff;color:#000}'
            'img{max-width:100%}[hidden]:not([hidden=until-found i]){display:none!important}</style></head><body>\n')
VIEWPORTS = [("1920", 1920, 1080), ("3440", 3440, 1440), ("390", 390, 844)]
ALLOWED = ("https://fonts.googleapis.com/", "https://fonts.gstatic.com/")


def run():
    html = SKELETON + PAGE.read_text(encoding="utf-8") + "\n</body></html>"
    report = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        for name, w, h in VIEWPORTS:
            r = {"console": [], "pageerrors": [], "requests": [], "blocked": [], "steps": [], "hscroll": []}
            ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=1)
            page = ctx.new_page()
            page.on("console", lambda m: r["console"].append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
            page.on("pageerror", lambda e: r["pageerrors"].append(str(e)))

            def route(route):
                url = route.request.url
                r["requests"].append(url)
                if url == BASE:
                    route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
                elif url.startswith(ALLOWED):
                    route.continue_()
                else:
                    r["blocked"].append(url)
                    route.abort()
            page.route("**/*", route)
            page.goto(BASE)
            page.wait_for_timeout(2500)

            def shot(step):
                page.screenshot(path=str(HERE / "out" / f"shot-{name}-{step}.png"))
                r["steps"].append(step)
                sw = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth, document.body.scrollWidth]")
                if sw[0] > sw[1] + 1 or sw[2] > sw[1] + 1:
                    r["hscroll"].append(f"{step}: {sw}")

            def close_dialogs():
                page.keyboard.press("Escape")
                page.evaluate("document.querySelectorAll('dialog[open]').forEach((d) => d.close()); document.querySelector('#palette')?.classList.remove('open')")
                page.wait_for_timeout(150)

            r["dialog_open_on_load"] = page.evaluate("[...document.querySelectorAll('dialog[open]')].map((d) => d.className || d.id)")
            shot("01-load")
            # Today sub-tabs
            for tw in ("agenda", "tasks", "follow"):
                page.click(f"#tw-tab-{tw}")
                page.wait_for_timeout(300)
                shot(f"02-today-{tw}")
            # KPI tiles
            n = page.locator("#stats .stat.clickable").count()
            r["kpi_tiles"] = n
            for i in range(n):
                page.evaluate("() => { const b = document.querySelector('.nav-group button[data-tab=\"today\"]'); b && b.click(); }")
                page.wait_for_timeout(250)
                tile = page.locator("#stats .stat.clickable").nth(i)
                label = tile.locator(".l").inner_text()
                tile.click()
                page.wait_for_timeout(700)
                if i in (0, n - 1) or "Off-hours" in label or "Uncommitted" in label:
                    shot(f"03-kpi-{i}-{label.split()[0].lower()}")
                close_dialogs()
            # every nav tab
            tabs = page.evaluate("[...document.querySelectorAll('.nav-group button[data-tab]')].map((b) => b.dataset.tab)")
            r["tabs"] = tabs
            for t in tabs:
                page.locator(f'.nav-group button[data-tab="{t}"]').click()
                page.wait_for_timeout(900)
                if t == "toolkit":
                    page.wait_for_timeout(400)
                    page.locator("#tab-toolkit .tk-view.active button", has_text="Load accounts").first.click()
                    page.wait_for_timeout(500)
                    page.locator("#tab-toolkit .tk-view.active tr.tk-click").first.click()
                    page.wait_for_timeout(500)
                if t == "mail":
                    page.locator("#mail-list li.clickable").first.click()
                    page.wait_for_timeout(500)
                shot(f"04-tab-{t}")
                close_dialogs()
            page.locator('.nav-group button[data-tab="today"]').click()
            page.wait_for_timeout(400)
            # Ctrl+K
            page.keyboard.press("Control+k")
            page.wait_for_timeout(300)
            shot("05-palette")
            page.keyboard.type("order 4410287")
            page.wait_for_timeout(600)
            shot("05-palette-search")
            close_dialogs()
            # triage, quick capture
            page.evaluate("CC.triage()")
            page.wait_for_timeout(600)
            shot("06-triage")
            close_dialogs()
            page.evaluate("CC.quickCapture()")
            page.wait_for_timeout(300)
            page.keyboard.type("call Avery fri")
            shot("07-capture")
            close_dialogs()
            # profile theme
            page.click(".pf-avatar")
            page.wait_for_timeout(300)
            page.click('.pf-preset[data-key="synth"]')
            page.wait_for_timeout(200)
            shot("08-profile")
            page.click("text=Save profile")
            page.wait_for_timeout(400)
            r["accent_after_save"] = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()")
            shot("08-profile-saved")
            page.click(".pf-avatar")
            page.wait_for_timeout(200)
            page.click('.pf-preset[data-key="neon"]')
            page.click("text=Save profile")
            page.wait_for_timeout(300)
            # layout toggles (wide screens)
            if w >= 1600:
                page.click("#layout-toggle")
                page.wait_for_timeout(400)
                shot("09-single")
                page.click("#layout-toggle")
                page.wait_for_timeout(400)
                page.click("#focus-layout")
                page.wait_for_timeout(600)
                shot("10-focus-layout")
                page.click("#focus-layout")
                page.wait_for_timeout(400)
            # reminder pop-up demo
            page.keyboard.press("Control+k")
            page.keyboard.type("pop-up demo")
            page.wait_for_timeout(300)
            page.keyboard.press("Enter")
            page.wait_for_timeout(800)
            r["popup_shown"] = page.evaluate("!!document.querySelector('dialog.pr-pop[open]')")
            shot("11-reminder-pop")
            if r["popup_shown"]:
                page.click(".pr-pop-done")
                page.wait_for_timeout(500)
            r["unknown_endpoints"] = page.evaluate("window.__ccPreview.unknown")
            r["requests"] = sorted(set(u.split("?")[0] for u in r["requests"]))
            report[name] = r
            ctx.close()
        browser.close()
    print(json.dumps(report, indent=1))
    bad = any(v["pageerrors"] or v["blocked"] or [c for c in v["console"] if c.startswith("error")] or v["hscroll"] for v in report.values())
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(run())
