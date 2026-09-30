"""Load brands.yaml and rubric.yaml."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def load_brands(path: Path = CONFIG_DIR / "brands.yaml") -> list[dict]:
    brands = yaml.safe_load(path.read_text())["brands"]
    for b in brands:
        if not b.get("name") or not b.get("channel_id"):
            raise ValueError(f"brands.yaml entry missing name/channel_id: {b}")
    return brands


def load_rubric(path: Path | None = None) -> dict:
    """Returns {"version": str, "dimensions": {name: [allowed values]}}.
    Defaults to config/$RUBRIC_FILE (rubric.yaml if unset)."""
    path = path or CONFIG_DIR / os.getenv("RUBRIC_FILE", "rubric.yaml")
    raw = yaml.safe_load(path.read_text())
    dims = {name: list(spec["values"]) for name, spec in raw["dimensions"].items()}
    return {"version": str(raw.get("version", "1")), "dimensions": dims}
