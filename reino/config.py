"""Carga config.toml del repo y le mezcla encima el del usuario."""
import tomllib
from pathlib import Path

from . import paths


def _merge(base: dict, over: dict) -> dict:
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def load(extra: Path | None = None) -> dict:
    paths.migrate_legacy()
    with open(paths.DEFAULT_CONFIG, "rb") as f:
        cfg = tomllib.load(f)
    for path in (paths.user_config(), extra):
        if path and Path(path).exists():
            with open(path, "rb") as f:
                _merge(cfg, tomllib.load(f))
    return cfg
