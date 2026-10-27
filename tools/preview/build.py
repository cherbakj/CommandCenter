"""Builds the single-file "Command Center" artifact preview from the app's current static files.

Reads (never writes) commandcenter/static in the repo, inlines index.html's body, every stylesheet and module
script in index.html's order, swaps the local fonts for Google Fonts, and puts mock.js (sample data, no server)
in front of the app's scripts. Output: tools/preview/out/preview.html (git-ignored).

Run:  C:\\Sonance\\internal\\CommandCenter\\.venv\\Scripts\\python.exe build.py   (from any directory)
"""
import base64
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = Path(r"C:\Sonance\internal\CommandCenter")
STATIC = REPO / "commandcenter" / "static"
OUT = HERE / "out" / "preview.html"

FONTS = ("https://fonts.googleapis.com/css2?family=Inter:wght@100..900"
         "&family=JetBrains+Mono:wght@400..700&family=Montserrat:wght@100..900&family=Orbitron:wght@400..900&display=swap")
SKIP_SCRIPTS = {"splash.js"}            # launch animation: off in the preview
SKIP_STYLES = {"fonts.css", "splash.css"}  # local @font-face files -> Google Fonts; splash is not loaded

# Real hostnames / database names in the app's text -> placeholders (the sample data never uses real ones).
SCRUB = [
    ("dev.pulse.sonance.com", "dev.pulse.example.com"),
    ("pulse.sonance.com", "pulse.example.com"),
    ("sonance.com", "example.com"),
    ("snc-xxxx-db01.example.com", "sql01.example.com"),
    ("FS92STG", "STAGINGDB"),
    ("FS92SYS", "SYSDB"),
    # personal details in code comments: the weather places (the sample data's are Brightwater / Seacliff) and the surname
    ("Lake Forest", "Brightwater"),
    ("San Clemente", "Seacliff"),
    ("Cherbak's Toolkit", "the Toolkit"),
]


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def scrub(text: str) -> str:
    for a, b in SCRUB:
        text = text.replace(a, b)
    return text


def safe_script(js: str) -> str:
    return js.replace("</script", "<\\/script")


def safe_style(css: str) -> str:
    return css.replace("</style", "<\\/style")


def main() -> int:
    index = read("index.html")
    head = index.split("<body>", 1)[0]
    body = index.split("<body>", 1)[1].rsplit("</body>", 1)[0]

    # Scripts in index.html's order (the splash is dropped).
    scripts = [s for s in re.findall(r'<script src="/static/([\w.-]+\.js)"></script>', index) if s not in SKIP_SCRIPTS]
    # Stylesheets: the head's links, then modules.css (body), then the CSS each module injects, in script order.
    styles = [s for s in re.findall(r'<link rel="stylesheet" href="/static/([\w.-]+\.css)"', head) if s not in SKIP_STYLES]
    styles += re.findall(r'<link rel="stylesheet" href="/static/([\w.-]+\.css)"', body)
    for s in scripts:
        for css in re.findall(r'\.href = "/static/([\w.-]+\.css)"', read(s)):
            if css not in styles and css not in SKIP_STYLES:
                styles.append(css)
    missing = [n for n in scripts + styles if not (STATIC / n).exists()]
    if missing:
        print("missing static files:", missing, file=sys.stderr)
        return 1

    # Body: no <script>/<link> tags (inlined below), no splash.
    body = re.sub(r'\s*<script src="/static/[^"]+"></script>', "", body)
    body = re.sub(r'\s*<link rel="stylesheet" href="/static/[^"]+">', "", body)
    body = re.sub(r"\s*<!-- feature modules \(each optional\) -->", "", body)
    logo = "data:image/svg+xml;base64," + base64.b64encode((STATIC / "jc-logo.svg").read_bytes()).decode()
    body = body.replace("/static/jc-logo.svg", logo)
    # Preview chip in the logo, and the one-line note under the header.
    body, n = re.subn(r'(<div class="logo"[^>]*>.*?)(</div>)',
                      r'\1<span class="preview-chip" title="This page runs on made-up sample data, with no server">Preview · sample data</span>\2',
                      body, count=1, flags=re.S)
    if not n:
        print("logo not found in index.html", file=sys.stderr)
        return 1
    note = ('\n  <div class="preview-note" id="preview-note" role="note"><span>Preview with made-up sample data: nothing is '
            'sent or saved. The Pulse site, SSMS and VS Code views work only in the desktop app.</span>'
            '<button type="button" class="icon subtle" id="preview-note-x" aria-label="Hide this note" title="Hide this note">✕</button></div>')
    body = body.replace("</header>", "</header>" + note, 1)
    body = scrub(body)

    css = "\n".join(f"/* ---- {n} ---- */\n{read(n)}" for n in styles)
    css = scrub(css) + "\n/* ---- preview.css ---- */\n" + (HERE / "preview.css").read_text(encoding="utf-8")
    mock = (HERE / "mock.js").read_text(encoding="utf-8")
    js = "\n".join(f"<script>/* ---- {n} ---- */\n{safe_script(scrub(read(n)))}\n</script>" for n in scripts)

    page = f"""<title>Command Center</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="{FONTS}">
<style>
{safe_style(css)}
</style>
{body.strip()}
<script>/* ---- mock.js (preview: sample data instead of the local server) ---- */
{safe_script(mock)}
</script>
{js}
"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(page, encoding="utf-8", newline="\n")
    size = OUT.stat().st_size
    print(f"wrote {OUT} ({size:,} bytes, {size / 1024:.1f} KB)")
    print("scripts:", ", ".join(["mock.js"] + scripts))
    print("styles:", ", ".join(styles + ["preview.css"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
