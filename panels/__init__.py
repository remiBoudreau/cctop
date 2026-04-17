"""Panel registry for cctop.

Each panel is a pluggable module that renders into the bottom of the
cctop window. The default panel is `chain_ready` which shows the
currently selected engagement's CHAIN-READY findings.

To register a new panel:
  1. Create panels/<name>.py exposing a class `Panel` that inherits
     from panels.base.Panel.
  2. Add the module name to the `REGISTRY` below.

Or load a panel from an arbitrary file via `--panel-path`.
"""
from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from typing import Type

from .base import NoPanel, Panel

REGISTRY: dict[str, str] = {
    "chain_ready": "panels.chain_ready",
    "none": "__none__",
}


def load_panel(name: str) -> Type[Panel]:
    """Load a panel class by name from the registry."""
    if name == "none":
        return NoPanel
    if name not in REGISTRY:
        raise ValueError(f"Unknown panel '{name}'. Registered: {list(REGISTRY)}")
    module = importlib.import_module(REGISTRY[name])
    return module.Panel


def load_panel_from_path(path: Path) -> Type[Panel]:
    """Load a panel class from an arbitrary Python file."""
    spec = importlib.util.spec_from_file_location("user_panel", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Could not load panel from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Panel
