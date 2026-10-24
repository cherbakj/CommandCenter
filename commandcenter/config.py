"""Loads config.yaml (created from config.example.yaml on first run)."""
import shutil
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
EXAMPLE_PATH = ROOT / "config.example.yaml"
DATA_DIR = ROOT / "data"

_config: dict = {}


def load() -> dict:
    global _config
    if not CONFIG_PATH.exists():
        shutil.copy(EXAMPLE_PATH, CONFIG_PATH)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        _config = yaml.safe_load(f) or {}
    DATA_DIR.mkdir(exist_ok=True)
    return _config


def get() -> dict:
    return _config or load()


def section(name: str, default=None):
    value = get().get(name)
    return default if value is None else value
