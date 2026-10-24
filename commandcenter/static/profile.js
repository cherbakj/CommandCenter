// User profile: an initials avatar at the top right opens a panel to set name/initials, theme colors,
// text size, density and effects. Saved server-side (/api/profile) and applied immediately.
(() => {
  const CC = window.CC;
  if (!CC) return;
  const { el, api, guarded } = CC;

  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = "/static/profile.css";
  document.head.append(link);

  let profile = null, presets = {};
  const avatar = el("button", { class: "pf-avatar", title: "Profile and appearance", "aria-label": "Profile and appearance", onclick: () => openPanel() }, "JC");

  const rgb = (hex) => { const n = parseInt(hex.slice(1), 16); return `${n >> 16} ${(n >> 8) & 255} ${n & 255}`; };

  function apply(p) {
    const root = document.documentElement;
    root.style.setProperty("--accent", p.accent);
    root.style.setProperty("--accent-rgb", rgb(p.accent));
    root.style.setProperty("--accent-2", p.accent2);
    root.style.setProperty("--accent-2-rgb", rgb(p.accent2));
    root.style.setProperty("--pf-scale", String(p.text_scale / 100));
    root.classList.toggle("pf-compact", p.density === "compact");
    root.classList.toggle("pf-no-glow", !p.glow);
    root.classList.toggle("pf-no-scanlines", !p.scanlines);
    root.classList.toggle("pf-no-grid", !p.grid);
    root.classList.toggle("pf-no-motion", !p.animations);
    avatar.textContent = p.initials || "JC";
    avatar.title = `${p.name} · profile and appearance`;
    try { localStorage.setItem("cc-splash-off", p.splash ? "" : "1"); } catch { /* per-viewer convenience only */ }
  }

  function placeAvatar() {
    const slot = document.querySelector("#profile-slot") || document.querySelector(".header-right") || document.querySelector("header");
    if (slot && !avatar.isConnected) slot.id === "profile-slot" ? slot.append(avatar) : slot.prepend(avatar);
  }

  async function load() {
    const data = await api("/api/profile");
    profile = data.profile; presets = data.presets;
    apply(profile);
  }

  function openPanel() {
    if (!profile) return;
    const draft = { ...profile };
    const field = (label, input) => el("label", { class: "pf-field" }, el("span", { class: "field-label" }, label), input);
    const name = el("input", { id: "pf-name", value: draft.name, maxlength: "60" });
    const initials = el("input", { id: "pf-initials", value: draft.initials, maxlength: "3" });
    const accent = el("input", { id: "pf-accent", type: "color", value: draft.accent });
    const accent2 = el("input", { id: "pf-accent2", type: "color", value: draft.accent2 });
    const scale = el("input", { id: "pf-scale", type: "range", min: "85", max: "140", step: "5", value: String(draft.text_scale) });
    const scaleOut = el("span", { class: "pf-scale-out" }, `${draft.text_scale}%`);
    const density = el("select", { id: "pf-density" }, el("option", { value: "comfortable" }, "Comfortable"), el("option", { value: "compact" }, "Compact"));
    density.value = draft.density;
    const toggle = (key, label) => {
      const box = el("input", { type: "checkbox", id: `pf-${key}` });
      box.checked = !!draft[key];
      box.addEventListener("change", () => { draft[key] = box.checked; preview(); });
      return el("label", { class: "pf-toggle" }, box, label);
    };
    const err = el("p", { class: "err", hidden: true });

    const swatches = el("div", { class: "pf-presets" }, [...Object.entries(presets), ["custom", { label: "Custom" }]].map(([key, pr]) => {
      const b = el("button", { type: "button", class: `pf-preset ${draft.preset === key ? "on" : ""}`, "data-key": key,
        onclick: () => {
          draft.preset = key;
          if (pr.accent) { draft.accent = pr.accent; draft.accent2 = pr.accent2; accent.value = pr.accent; accent2.value = pr.accent2; }
          swatches.querySelectorAll(".pf-preset").forEach((x) => x.classList.toggle("on", x.dataset.key === key));
          preview();
        } },
        el("span", { class: "pf-sw" }, el("i", {}), el("i", {})), pr.label);
      const [a, c] = b.querySelectorAll(".pf-sw i");
      a.style.background = pr.accent || draft.accent;
      c.style.background = pr.accent2 || draft.accent2;
      return b;
    }));
    const markCustom = () => {
      draft.preset = "custom";
      swatches.querySelectorAll(".pf-preset").forEach((x) => x.classList.toggle("on", x.dataset.key === "custom"));
    };
    accent.addEventListener("input", () => { draft.accent = accent.value; markCustom(); preview(); });
    accent2.addEventListener("input", () => { draft.accent2 = accent2.value; markCustom(); preview(); });
    scale.addEventListener("input", () => { draft.text_scale = Number(scale.value); scaleOut.textContent = `${scale.value}%`; preview(); });
    density.addEventListener("change", () => { draft.density = density.value; preview(); });
    initials.addEventListener("input", () => { avatar.textContent = initials.value.toUpperCase() || "JC"; });

    function preview() { apply({ ...draft, initials: initials.value.toUpperCase() || draft.initials, name: name.value || draft.name }); }

    const dialog = document.querySelector("#dialog");
    let saved = false;
    dialog.addEventListener("close", () => { if (!saved) apply(profile); }, { once: true });  // cancel = revert preview

    const save = () => guarded(async () => {
      try {
        const res = await api("/api/profile", { profile: { ...draft, name: name.value, initials: initials.value } });
        profile = res.profile;
      } catch (e) { err.textContent = e.message; err.hidden = false; return; }
      saved = true;
      apply(profile);
      dialog.close();
    });
    const reset = () => guarded(async () => {
      if (!await CC.confirmDialog("Reset appearance", "Go back to the default Neon Green look?", "Reset")) return;
      profile = (await api("/api/profile/reset", {})).profile;
      apply(profile);
    });

    CC.wideDialog(
      el("h3", {}, "Profile"),
      el("div", { class: "pf-grid" },
        el("section", {},
          el("div", { class: "panel-label" }, "You"),
          el("div", { class: "pf-row" }, field("Name", name), field("Initials", initials)),
          el("div", { class: "panel-label gap" }, "Theme"), swatches,
          el("div", { class: "pf-row" }, field("Accent", accent), field("Second accent", accent2))),
        el("section", {},
          el("div", { class: "panel-label" }, "Style"),
          field("Text size", el("div", { class: "pf-scale" }, scale, scaleOut)),
          field("Spacing", density),
          el("div", { class: "pf-toggles" }, toggle("glow", "Glow effects"), toggle("scanlines", "Scanlines"), toggle("grid", "Background grid"),
            toggle("animations", "Animations"), toggle("splash", "Launch animation")),
          el("p", { class: "muted" }, "Changes preview live. Saved only on this PC."))),
      err,
      el("div", { class: "row" },
        el("button", { type: "button", class: "subtle", onclick: reset }, "Reset to default"),
        el("button", { value: "cancel", class: "subtle" }, "Cancel"),
        el("button", { type: "button", class: "primary", onclick: save }, "Save profile")));
  }

  CC.addPaletteSource(() => [{ group: "Commands", label: "Profile and appearance", hint: "colors, text size, effects", run: openPanel }]);
  CC.openProfile = openPanel;

  const start = () => { placeAvatar(); guarded(load); };
  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", start, { once: true }) : start();
})();
