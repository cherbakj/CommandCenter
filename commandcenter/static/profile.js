// User profile: an initials avatar at the top right opens a panel to set name/initials, the skin (Command Center
// or Pulse: skin-pulse.css), theme colors, text size, density and effects (glow, frosted glass, background motion,
// dot grid, animations). Saved server-side (/api/profile) and applied immediately.
(() => {
  const CC = window.CC;
  if (!CC) return;
  const { el, api, guarded } = CC;

  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = "/static/profile.css";
  document.head.append(link);

  // The skin from the last load, so the page doesn't paint in the wrong one while the profile loads.
  try { const s = localStorage.getItem("cc-skin"); if (s) document.documentElement.dataset.skin = s; } catch { /* per-viewer convenience only */ }

  let profile = null, presets = {}, skins = {};
  let panelClose = null;  // aborts the open panel's "close" listener when the panel is rebuilt
  const avatar = el("button", { class: "pf-avatar", title: "Profile and appearance", "aria-label": "Profile and appearance", onclick: () => openPanel() }, "JC");

  const rgb = (hex) => { const n = parseInt(hex.slice(1), 16); return `${n >> 16} ${(n >> 8) & 255} ${n & 255}`; };
  // Text on an accent fill: dark on a light accent, white on a dark custom one (whichever contrasts more).
  const onAccent = (hex) => {
    const lin = (c) => { c /= 255; return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
    const [r, g, b] = rgb(hex).split(" ").map(Number);
    const lum = 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
    return lum > 0.18 ? "#031400" : "#ffffff";
  };

  function apply(p) {
    const root = document.documentElement;
    root.style.setProperty("--accent", p.accent);
    root.style.setProperty("--accent-rgb", rgb(p.accent));
    root.style.setProperty("--accent-2", p.accent2);
    root.style.setProperty("--accent-2-rgb", rgb(p.accent2));
    root.style.setProperty("--on-accent", onAccent(p.accent));
    root.style.setProperty("--pf-scale", String(p.text_scale / 100));
    root.classList.toggle("pf-compact", p.density === "compact");
    // The Pulse skin is flat, like Pulse: no glow, glass, background motion or dot grid, whatever the switches say.
    const flat = p.skin === "pulse";
    root.dataset.skin = p.skin || "cc";
    root.classList.toggle("pf-no-glow", flat || !p.glow);
    root.classList.toggle("pf-no-glass", flat || p.glass === false);
    root.classList.toggle("pf-no-aurora", flat || p.aurora === false);
    root.classList.toggle("pf-no-grid", flat || !p.grid);
    root.classList.toggle("pf-no-motion", !p.animations);
    avatar.textContent = p.initials || "JC";
    avatar.title = `${p.name} · profile and appearance`;
    try {
      localStorage.setItem("cc-splash-off", p.splash ? "" : "1");
      localStorage.setItem("cc-skin", root.dataset.skin);
    } catch { /* per-viewer convenience only */ }
  }

  function placeAvatar() {
    const slot = document.querySelector("#profile-slot") || document.querySelector(".header-right") || document.querySelector("header");
    if (slot && !avatar.isConnected) slot.id === "profile-slot" ? slot.append(avatar) : slot.prepend(avatar);
  }

  async function load() {
    const data = await api("/api/profile");
    profile = data.profile; presets = data.presets; skins = data.skins || {};
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
    const boxes = {};
    const toggle = (key, label) => {
      const box = boxes[key] = el("input", { type: "checkbox", id: `pf-${key}` });
      box.checked = !!draft[key];
      box.addEventListener("change", () => { draft[key] = box.checked; preview(); });
      return el("label", { class: "pf-toggle" }, box, label);
    };
    // Glow, glass, motion and the dot grid don't exist in the Pulse skin: their switches are off and greyed there.
    const FLAT = ["glow", "glass", "aurora", "grid"];
    const flatNote = el("p", { class: "muted pf-flat-note" }, "The Pulse skin is flat, like Pulse: no glow, glass, background motion or dot grid.");
    function markEffects() {
      const flat = draft.skin === "pulse";
      for (const k of FLAT) {
        boxes[k].disabled = flat;
        boxes[k].checked = flat ? false : !!draft[k];
        boxes[k].closest(".pf-toggle").classList.toggle("off", flat);
      }
      flatNote.hidden = !flat;
    }
    const err = el("p", { class: "err", hidden: true });

    const swatches = el("div", { class: "pf-presets" }, [...Object.entries(presets), ["custom", { label: "Custom" }]].map(([key, pr]) => {
      const b = el("button", { type: "button", class: `pf-preset ${draft.preset === key ? "on" : ""}`, "data-key": key,
        "aria-pressed": String(draft.preset === key), title: pr.label,
        onclick: () => {
          draft.preset = key;
          if (pr.accent) { draft.accent = pr.accent; draft.accent2 = pr.accent2; accent.value = pr.accent; accent2.value = pr.accent2; }
          markPreset(key);
          preview();
        } },
        el("span", { class: "pf-sw" }, el("i", {}), el("i", {})), pr.label);
      const [a, c] = b.querySelectorAll(".pf-sw i");
      a.style.background = pr.accent || draft.accent;
      c.style.background = pr.accent2 || draft.accent2;
      return b;
    }));
    function markPreset(key) {
      swatches.querySelectorAll(".pf-preset").forEach((x) => {
        x.classList.toggle("on", x.dataset.key === key);
        x.setAttribute("aria-pressed", String(x.dataset.key === key));
      });
    }
    const markCustom = () => { draft.preset = "custom"; markPreset("custom"); };

    // Skin: picking one also picks its colors (Pulse: Sonance Blue), unless the colors are custom.
    const skinPicker = el("div", { class: "pf-skins", role: "group", "aria-label": "Skin" }, Object.entries(skins).map(([key, sk]) =>
      el("button", { type: "button", class: `pf-skin pf-skin-${key}`, "data-key": key, "aria-pressed": String((draft.skin || "cc") === key),
        onclick: () => {
          draft.skin = key;
          const pr = presets[sk.preset];
          if (pr && draft.preset !== "custom") {
            draft.preset = sk.preset; draft.accent = pr.accent; draft.accent2 = pr.accent2;
            accent.value = pr.accent; accent2.value = pr.accent2; markPreset(sk.preset);
          }
          skinPicker.querySelectorAll(".pf-skin").forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.key === key)));
          markEffects();
          preview();
        } },
        el("span", { class: "pf-skin-art", "aria-hidden": "true" }, el("i", {}), el("i", {}), el("i", {})),
        el("span", { class: "pf-skin-name" }, sk.label))));
    accent.addEventListener("input", () => { draft.accent = accent.value; markCustom(); preview(); });
    accent2.addEventListener("input", () => { draft.accent2 = accent2.value; markCustom(); preview(); });
    scale.addEventListener("input", () => { draft.text_scale = Number(scale.value); scaleOut.textContent = `${scale.value}%`; preview(); });
    density.addEventListener("change", () => { draft.density = density.value; preview(); });
    initials.addEventListener("input", () => { avatar.textContent = initials.value.toUpperCase() || "JC"; });

    function preview() { apply({ ...draft, initials: initials.value.toUpperCase() || draft.initials, name: name.value || draft.name }); }

    const dialog = document.querySelector("#dialog");
    let saved = false;
    panelClose?.abort();
    panelClose = new AbortController();
    dialog.addEventListener("close", () => { if (!saved) apply(profile); }, { once: true, signal: panelClose.signal });  // cancel = revert preview

    const save = () => guarded(async () => {
      try {
        const res = await api("/api/profile", { profile: { ...draft, name: name.value, initials: initials.value } });
        profile = res.profile;
      } catch (e) { err.textContent = e.message; err.hidden = false; return; }
      saved = true;
      apply(profile);
      dialog.close();
    });
    // The reset confirm sits inside the panel (a separate dialog would replace the panel, and cancelling
    // it would close everything). After a reset the panel reopens with the default values.
    const buttons = () => [
      el("button", { type: "button", class: "subtle", onclick: askReset }, "Reset to default"),
      el("button", { value: "cancel", class: "subtle" }, "Cancel"),
      el("button", { type: "button", class: "primary", onclick: save }, "Save profile")];
    const row = el("div", { class: "row pf-actions" }, buttons());
    function askReset() {
      const keep = el("button", { type: "button", class: "subtle", onclick: () => { row.classList.remove("pf-confirm"); row.replaceChildren(...buttons()); } }, "Keep my look");
      row.classList.add("pf-confirm");
      row.replaceChildren(
        el("span", { class: "pf-confirm-text", role: "alert" }, "Reset the skin, colors, text size and effects to the default Command Center look (Neon Green)?"),
        keep,
        el("button", { type: "button", class: "danger-solid", onclick: doReset }, "Reset"));
      keep.focus();
    }
    const doReset = () => guarded(async () => {
      try {
        profile = (await api("/api/profile/reset", {})).profile;
      } catch (e) { err.textContent = e.message; err.hidden = false; return; }
      saved = true;
      apply(profile);
      openPanel();  // same dialog, rebuilt with the reset values
    });

    const toggles = el("div", { class: "pf-toggles" }, toggle("glow", "Glow effects"), toggle("glass", "Frosted glass"), toggle("aurora", "Background motion"),
      toggle("grid", "Dot grid"), toggle("animations", "Animations"), toggle("splash", "Launch animation"));
    markEffects();
    CC.wideDialog(
      el("h3", {}, "Profile"),
      el("div", { class: "pf-grid" },
        el("section", {},
          el("div", { class: "panel-label" }, "You"),
          el("div", { class: "pf-row" }, field("Name", name), field("Initials", initials)),
          el("div", { class: "panel-label gap" }, "Skin"), skinPicker,
          el("div", { class: "panel-label gap" }, "Theme"), swatches,
          el("div", { class: "pf-row" }, field("Accent", accent), field("Second accent", accent2))),
        el("section", {},
          el("div", { class: "panel-label" }, "Style"),
          field("Text size", el("div", { class: "pf-scale" }, scale, scaleOut)),
          field("Spacing", density),
          toggles, flatNote,
          el("p", { class: "muted" }, "Changes preview live. Saved only on this PC."))),
      err, row);
  }

  CC.addPaletteSource(() => [{ group: "Commands", label: "Profile and appearance", hint: "colors, text size, effects", run: openPanel }]);
  CC.openProfile = openPanel;

  const start = () => { placeAvatar(); guarded(load); };
  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", start, { once: true }) : start();
})();
