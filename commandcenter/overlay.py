"""Native windows laid over a dashboard tab: the Pulse site view (desktop.PulseView) and the docked SSMS
(ssmsdock.SsmsDock). A native window always paints above the HTML page, so the page reports where the tab's stage is
and whether any Command Center UI must be on top; these classes put a WinForms Panel there, clip it, or hide it.

Channel: POST /api/window/<view>/bounds (security.py: the normal session cookie, X-CommandCenter and Origin checks),
checked by parse_bounds(). There is no pywebview js_api. update() runs on the server thread and only stores the latest
report; one BeginInvoke at a time applies it on the window's UI thread (CSS px x DPI x dashboard zoom)."""
import logging
import math
import threading

log = logging.getLogger(__name__)
MAX_CUTS = 12


def _rect(v) -> tuple[float, float, float, float]:
    if not isinstance(v, (list, tuple)) or len(v) != 4 or not all(isinstance(n, (int, float)) and not isinstance(n, bool)
                                                                  and math.isfinite(n) for n in v):
        raise ValueError("a rectangle is [x, y, width, height]")
    x, y, w, h = (float(n) for n in v)
    if not (-1e5 < x < 1e5 and -1e5 < y < 1e5 and 0 <= w < 1e5 and 0 <= h < 1e5):
        raise ValueError("rectangle out of range")
    return x, y, w, h


def parse_bounds(body, sites=("main",)) -> dict:
    """Checks a /api/window/<view>/bounds report from the dashboard. Raises ValueError. `site` picks one of a view's
    windows (Pulse: prod / dev); a view with one window takes its only site when none is sent."""
    if not isinstance(body, dict):
        raise ValueError("expected an object")
    seq = body.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or not 0 <= seq < 2 ** 53:
        raise ValueError("seq must be a counter")
    site = body.get("site", sites[0] if len(sites) == 1 else None)
    if site not in sites:
        raise ValueError(f"site must be one of {', '.join(sites)}")
    visible = body.get("visible") is True
    cuts = body.get("cuts") or []
    if not isinstance(cuts, list) or len(cuts) > MAX_CUTS:
        raise ValueError(f"at most {MAX_CUTS} cut-outs")
    return {"page": str(body.get("page") or "")[:40], "seq": seq, "site": site, "visible": visible,
            "rect": _rect(body.get("rect")) if visible else None, "cuts": [_rect(c) for c in cuts] if visible else []}


class NativeOverlay:
    """What the overlays share: the latest report (page id + sequence number, so late reports are dropped), the
    coalesced apply on the UI thread, the CSS-px-to-window geometry, cut-outs (a window region on the panel: the
    native window inside is clipped to it and clicks there reach the dashboard) and keyboard focus going back to the
    dashboard when a focused panel hides."""
    NAME = "native view"
    SITES = ("main",)
    MIN_SIZE = 40  # physical px: smaller than this and the view stays hidden

    def __init__(self, app):
        self.app = app
        self.want = {"visible": False, "site": self.SITES[0], "rect": None, "cuts": []}
        self.page, self.seq = None, -1
        self._gone: list[str] = []  # pages that went away (reload, lock): their late reports are dropped
        self._lock = threading.Lock()
        self._queued = False

    # ---- from the server thread ----
    def available(self) -> bool:
        return self._form() is not None

    def update(self, report: dict):
        with self._lock:
            if report["page"] in self._gone:
                return  # a page that has gone away (its reports can arrive after the next page's first)
            if report["page"] != self.page:
                self.page, self.seq = report["page"], -1  # a new dashboard page: its counter starts again
            if report["seq"] <= self.seq:
                return  # an older report that arrived late
            self.seq = report["seq"]
            self.want = {k: report[k] for k in ("visible", "site", "rect", "cuts")}
        self._queue()

    def forget(self):
        """The dashboard page is going away (reload, lock, sign-out): hide until the next page reports."""
        with self._lock:
            self.want = {**self.want, "visible": False}
            if self.page:
                self._gone = [*self._gone[-7:], self.page]
            self.page, self.seq = None, -1
        self._queue()

    def release_focus(self):
        """Keyboard focus back to the dashboard (before it opens something that needs typing)."""
        self._invoke(self._focus_dashboard)

    # ---- UI thread ----
    def _form(self):
        try:
            form = self.app.window.native if self.app.window else None
            return form if form is not None and form.browser is not None else None
        except Exception:
            return None

    def _invoke(self, fn):
        form = self._form()
        if form is None:
            return
        from System import Action

        def run():
            try:
                fn()
            except Exception as e:
                log.warning("%s: %s (%s)", self.NAME, type(e).__name__, str(e)[:120])
        try:
            form.BeginInvoke(Action(run))
        except Exception as e:  # the window is closing
            log.debug("%s: couldn't reach the window (%s)", self.NAME, type(e).__name__)

    def _queue(self):
        with self._lock:
            if self._queued:
                return
            self._queued = True

        def apply():
            with self._lock:
                self._queued = False
            self._apply()
        self._invoke(apply)

    def _apply(self):
        raise NotImplementedError

    def _panels(self):
        return []

    def _showing(self, want) -> bool:
        return bool(want["visible"] and want["rect"] and self.app.unlocked and not self.app.lock_showing
                    and not self.app.quitting)

    def _focus_dashboard(self):
        form = self._form()
        if form and any(p is not None and p.ContainsFocus for p in self._panels()):
            form.browser.webview.Focus()

    def _new_panel(self):
        from System.Drawing import Color
        import System.Windows.Forms as WinForms
        panel = WinForms.Panel()
        panel.Visible = False
        panel.BackColor = Color.FromArgb(255, 5, 7, 13)
        self._form().Controls.Add(panel)
        panel.BringToFront()
        return panel

    def _geometry(self, form, rect, cuts):
        """(left, top, width, height, holes) in window px for a report's CSS-px rectangle, or None when too small."""
        from System.Drawing import Rectangle
        main = form.browser.webview
        try:
            scale = float(form.DeviceDpi) / 96 * float(main.ZoomFactor)
        except Exception:
            scale = 1.0
        ox, oy, cw, ch = main.Left, main.Top, main.ClientSize.Width, main.ClientSize.Height
        x, y, w, h = rect
        left, top = max(0, round(x * scale)), max(0, round(y * scale))
        right, bottom = min(cw, round((x + w) * scale)), min(ch, round((y + h) * scale))
        if right - left < self.MIN_SIZE or bottom - top < self.MIN_SIZE:
            return None
        holes = []
        for cx, cy, cwid, chei in cuts:
            hl, ht = max(left, round(cx * scale)), max(top, round(cy * scale))
            hr, hb = min(right, round((cx + cwid) * scale)), min(bottom, round((cy + chei) * scale))
            if hr > hl and hb > ht:
                holes.append(Rectangle(hl - left, ht - top, hr - hl, hb - ht))
        return ox + left, oy + top, right - left, bottom - top, holes

    @staticmethod
    def _clip(holder, width, height, holes):
        """Cuts the holes out of holder.panel (holder.region keeps the Region alive until it's replaced)."""
        from System.Drawing import Rectangle, Region
        old = holder.region
        if holes:
            holder.region = Region(Rectangle(0, 0, width, height))
            for hole in holes:
                holder.region.Exclude(hole)
        else:
            holder.region = None
        holder.panel.Region = holder.region  # children are clipped to it, and clicks there reach the dashboard
        if old is not None:
            old.Dispose()

    @staticmethod
    def _show_panel(form, panel, on):
        if panel is None:
            return
        if not on and panel.Visible and panel.ContainsFocus:
            form.browser.webview.Focus()  # never leave the keyboard in a hidden view
        if on:
            panel.BringToFront()
        panel.Visible = on
