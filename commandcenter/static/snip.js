// Snip (snip.py): ✂ in the header opens Windows' screen-snip overlay (as Win+Shift+S); the snip lands on the
// clipboard. Ctrl+K: "Snip screen" and "Snip in 3 seconds" (time to open a menu first).
(() => {
  const CC = window.CC;
  if (!CC) return;
  const { el, api, guarded } = CC;

  function snip(delay = 0) {
    if (delay) CC.toast?.(`Snip in ${delay} seconds…`);
    return guarded(() => api("/api/snip", { delay }));
  }

  function mount() {
    const ref = document.querySelector("#focus-chip");
    if (!ref || document.querySelector("#snip-btn")) return;
    ref.before(el("button", { id: "snip-btn", type: "button", class: "small subtle snip-btn",
      title: "Snip: copy part of the screen to the clipboard (Windows Snipping Tool, Win+Shift+S)",
      "aria-label": "Snip part of the screen to the clipboard", onclick: () => snip() }, "✂", el("span", { class: "snip-word" }, " Snip")));
  }

  CC.addPaletteSource(() => [
    { group: "Commands", label: "Snip screen", hint: "copy part of the screen to the clipboard", run: () => setTimeout(snip, 250) },  // the palette closes first
    { group: "Commands", label: "Snip in 3 seconds", hint: "time to open a menu first", run: () => snip(3) },
  ]);
  CC.snip = snip;
  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", mount, { once: true }) : mount();
})();
