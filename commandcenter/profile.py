"""User profile: name/initials and appearance (theme colors, text size, density, effects), saved locally."""
import re

from fastapi import APIRouter, Body, HTTPException

from . import config, db

router = APIRouter(prefix="/api/profile")
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
PRESETS = {
    "neon": {"label": "Neon Green", "accent": "#39ff14", "accent2": "#00e5ff"},
    "cyan": {"label": "Cyber Cyan", "accent": "#00e5ff", "accent2": "#39ff14"},
    "synth": {"label": "Synthwave Magenta", "accent": "#ff2bd6", "accent2": "#00e5ff"},
    "amber": {"label": "Amber Terminal", "accent": "#ffb000", "accent2": "#ff6a3d"},
    "ice": {"label": "Ice Blue", "accent": "#7cc4ff", "accent2": "#b8f2ff"},
}
DEFAULTS = {
    "name": "Joe Cherbak", "initials": "JC", "preset": "neon", "accent": "#39ff14", "accent2": "#00e5ff",
    "text_scale": 100, "density": "comfortable", "glow": True, "scanlines": True, "grid": True,
    "animations": True, "splash": True,
}


def get() -> dict:
    saved = db.kv_get("profile") or {}
    profile = {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}
    if "accent" not in saved and config.section("ui", {}).get("accent"):
        profile["accent"] = config.section("ui", {})["accent"]  # older config value until a profile is saved
    return profile


def _clean(data: dict) -> dict:
    p = get()
    for key, value in data.items():
        if key not in DEFAULTS:
            continue
        if key in ("accent", "accent2"):
            if not isinstance(value, str) or not HEX.match(value):
                raise ValueError("Colors must look like #39ff14")
            p[key] = value.lower()
        elif key == "preset":
            if value not in PRESETS and value != "custom":
                raise ValueError("Unknown theme")
            p[key] = value
        elif key == "name":
            value = str(value).strip()
            if not 1 <= len(value) <= 60 or not re.fullmatch(r"[\w .'-]+", value):
                raise ValueError("Name: letters, spaces, . ' - only (up to 60)")
            p[key] = value
        elif key == "initials":
            value = str(value).strip().upper()
            if not re.fullmatch(r"[A-Z0-9]{1,3}", value):
                raise ValueError("Initials: 1-3 letters or digits")
            p[key] = value
        elif key == "text_scale":
            if not isinstance(value, (int, float)) or not 85 <= value <= 140:
                raise ValueError("Text size must be between 85% and 140%")
            p[key] = int(value)
        elif key == "density":
            if value not in ("comfortable", "compact"):
                raise ValueError("Density must be comfortable or compact")
            p[key] = value
        else:  # booleans
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be on or off")
            p[key] = value
    if p["preset"] in PRESETS:  # a preset owns its colors
        p["accent"], p["accent2"] = PRESETS[p["preset"]]["accent"], PRESETS[p["preset"]]["accent2"]
    return p


@router.get("")
async def read_profile():
    return {"profile": get(), "presets": PRESETS, "defaults": DEFAULTS}


@router.post("")
async def save_profile(profile: dict = Body(..., embed=True)):
    try:
        p = _clean(profile)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.kv_set("profile", p)
    return {"profile": p}


@router.post("/reset")
async def reset_profile():
    db.kv_set("profile", dict(DEFAULTS))
    return {"profile": get()}
