"""Shared configuration loader for DC-Detector services."""

import os
import sys
import copy
import yaml

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_DEFAULT_CONFIG = os.path.join(_PROJECT_ROOT, "config.yaml")
_EXAMPLE_CONFIG = os.path.join(_PROJECT_ROOT, "config.example.yaml")


def project_root() -> str:
    return _PROJECT_ROOT


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base* (returns new dict)."""
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load_config(path: str | None = None) -> dict:
    """Load YAML config.

    Priority: *path* argument → config.yaml → config.example.yaml.
    Environment variables ``DC_CONFIG`` can also point to a file.
    """
    candidates = [
        path,
        os.environ.get("DC_CONFIG"),
        _DEFAULT_CONFIG,
        _EXAMPLE_CONFIG,
    ]
    chosen = None
    for c in candidates:
        if c and os.path.isfile(c):
            chosen = c
            break

    if chosen is None:
        print("WARNING: no config file found, using built-in defaults", file=sys.stderr)
        return {}

    with open(chosen, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data


# ---- Convenience accessors ------------------------------------------------

def get_section(cfg: dict, section: str) -> dict:
    return cfg.get(section, {})
