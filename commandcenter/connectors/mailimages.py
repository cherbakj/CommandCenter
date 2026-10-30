"""Images in an email, for the Mail reader and Triage. Only what travels inside the email is shown: embedded pictures
and attached picture files, read from Outlook as bytes (nothing is written to disk). Images hosted on the web are
counted, never fetched: loading one tells the sender the email was opened.

The text stays Outlook's own (item.Body, which has no mark where a picture was). Each picture's place comes from the
HTML: its pieces of text are lined up with Body in order, and a picture goes after the line reached so far. A piece not
found (hidden text, say) is passed over, and Body's extras (a link's <address>) are stepped over, so neither moves a
picture far. Call these on the Outlook thread (outlook.call)."""
import logging
import re
import urllib.parse
from html.parser import HTMLParser

import pywintypes

log = logging.getLogger("commandcenter.mailimages")

OL_BY_VALUE = 1
OL_FORMAT_HTML = 2
PR_CONTENT_ID = "http://schemas.microsoft.com/mapi/proptag/0x3712001F"
PR_MIME_TAG = "http://schemas.microsoft.com/mapi/proptag/0x370E001F"
PR_DATA = "http://schemas.microsoft.com/mapi/proptag/0x37010102"
PICTURE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}
PICTURE_MIMES = {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp", "image/bmp"}
MAX_BYTES = 25 * 1024 * 1024
MAX_HTML = 2_000_000  # past this, pictures not yet reached are listed at the end
NEARBY = 2000  # how far ahead in Body to look for a piece of text (a link's address can be long)...
ANYWHERE = 20  # ...except a piece this long, which is distinctive enough to be looked for in all of the rest
MIN_JUMP = 4  # a shorter piece only counts if it's exactly next
TRACKER_PX = 2  # a web image this small is a tracking pixel: not worth a mention


def _sniff(data: bytes) -> str | None:
    """The type from the bytes themselves, never what the email claims. Raster formats only (no SVG)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    return None


def _width(attrs: dict) -> int | None:
    """The width the email shows it at (a signature logo is often far larger than it's drawn)."""
    raw = (attrs.get("width") or "").strip()
    px = float(raw) if re.fullmatch(r"\d+(\.\d+)?", raw) else None
    if px is None:
        m = re.search(r"(?:^|;)\s*width\s*:\s*([\d.]+)\s*(px|pt|in)\b", attrs.get("style") or "", re.I)
        if m:
            try:
                px = float(m.group(1)) * {"px": 1, "pt": 4 / 3, "in": 96}[m.group(2).lower()]
            except ValueError:
                return None
    return round(px) if px and 1 <= px <= 4000 else None


class _Walk(HTMLParser):
    """`seq`, in order: each piece of text (no whitespace) and each picture; and how many web images there are."""
    SKIP = {"style", "script", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip, self.remote, self.seq = 0, 0, []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "img":
            a = {k: v or "" for k, v in attrs}
            src = a.get("src", "").strip()
            low = src.lower()
            if low.startswith("cid:"):
                self.seq.append({"cid": urllib.parse.unquote(src[4:]).strip().strip("<>").lower(),
                                 "width": _width(a), "alt": a.get("alt", "").strip()[:200]})
            elif low.startswith(("http:", "https:")):
                sizes = [a.get("width", "").strip(), a.get("height", "").strip()]
                if not any(s.isdigit() and int(s) <= TRACKER_PX for s in sizes):
                    self.remote += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        piece = "".join(data.split())
        if piece and not self.skip:
            self.seq.append(piece)


def _advance(squeezed: str, piece: str, cursor: int) -> int | None:
    """Where Body's text is after this piece of the HTML's text, or None if it isn't there."""
    if squeezed.startswith(piece, cursor):
        return cursor + len(piece)
    if len(piece) < MIN_JUMP:
        return None
    at = squeezed.find(piece, cursor, cursor + NEARBY)
    if at < 0 and len(piece) >= ANYWHERE:
        at = squeezed.find(piece, cursor)
    return at + len(piece) if at >= 0 else None


def _place(body: str, seq: list) -> list[int]:
    """For each picture in `seq`, where in `body` it goes: the top if no text was reached yet, else after that line."""
    where = [i for i, ch in enumerate(body) if not ch.isspace()]
    squeezed = "".join(body[i] for i in where)
    cuts, cursor = [], 0
    for step in seq:
        if isinstance(step, str):
            moved = _advance(squeezed, step, cursor)
            cursor = cursor if moved is None else moved
        elif not cursor:
            cuts.append(0)
        else:
            nl = body.find("\n", where[cursor - 1])
            cuts.append(nl + 1 if nl >= 0 else len(body))
    return cuts


def _attachments(item) -> tuple[dict, dict, list]:
    """Attachments as content id -> {n, name}, file name -> the same, and the ones that are picture files."""
    by_cid, by_name, files = {}, {}, []
    atts = item.Attachments
    for n in range(1, atts.Count + 1):
        a = atts.Item(n)
        if a.Type != OL_BY_VALUE:
            continue
        name = a.FileName or ""
        entry = {"n": n, "name": name}
        pa = a.PropertyAccessor
        for prop in (PR_CONTENT_ID, PR_MIME_TAG):
            try:
                entry[prop] = pa.GetProperty(prop) or ""
            except pywintypes.com_error:
                entry[prop] = ""
        if entry[PR_CONTENT_ID]:
            by_cid[entry[PR_CONTENT_ID].strip().strip("<>").lower()] = entry
        by_name.setdefault(name.lower(), entry)
        if name.lower().rsplit(".", 1)[-1] in PICTURE_EXTS or entry[PR_MIME_TAG].lower() in PICTURE_MIMES:
            files.append(entry)
    return by_cid, by_name, files


def _strip_lines(text: str) -> str:
    return text.strip("\r\n")


def build(item, body: str) -> dict:
    """{"remote_images": N} plus, when the email carries pictures, "parts": its text and pictures in order, each part
    {"text": ...} or {"images": [{n, name, alt, width}], "attached": bool}."""
    walk = _Walk()
    if item.BodyFormat == OL_FORMAT_HTML:
        walk.feed((item.HTMLBody or "")[:MAX_HTML])
        walk.close()
    out = {"remote_images": walk.remote}
    if not item.Attachments.Count:
        return out
    by_cid, by_name, files = _attachments(item)
    shown, used = [], set()
    seq = []  # the walk, less pictures this email doesn't carry
    for p in walk.seq:
        if isinstance(p, str):
            seq.append(p)
            continue
        entry = by_cid.get(p["cid"]) or by_name.get(p["cid"].split("@")[0])
        if entry:
            used.add(entry["n"])
            shown.append((entry, p))
            seq.append(p)
    rest = [e for e in files if e["n"] not in used]
    if not shown and not rest:
        return out
    parts, start = [], 0
    group = None
    for (entry, p), cut in zip(shown, _place(body, seq)):
        if cut > start:
            text = _strip_lines(body[start:cut])
            if text.strip():
                parts.append({"text": text})
            start, group = cut, None
        if group is None:
            group = {"images": [], "attached": False}
            parts.append(group)
        group["images"].append({"n": entry["n"], "name": entry["name"], "alt": p["alt"], "width": p["width"]})
    text = _strip_lines(body[start:])
    if text.strip():
        parts.append({"text": text})
    if rest:
        parts.append({"images": [{"n": e["n"], "name": e["name"], "alt": "", "width": None} for e in rest], "attached": True})
    out["parts"] = parts
    return out


def read(item, n: int) -> tuple[bytes, str] | None:
    """One picture's bytes and type, or None when it isn't a picture this can show."""
    try:
        atts = item.Attachments
        if not 1 <= n <= atts.Count:
            return None
        a = atts.Item(n)
        if a.Type != OL_BY_VALUE or a.Size > MAX_BYTES:
            return None
        data = bytes(a.PropertyAccessor.GetProperty(PR_DATA))
    except pywintypes.com_error:
        return None
    kind = _sniff(data)
    return (data, kind) if kind else None
