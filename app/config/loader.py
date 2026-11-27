"""Load and hash configuration.

Load order (later files override earlier ones, deep-merged):
  1. config/default.yaml   - committed defaults (brief section 22)
  2. config/host.yaml      - per-host overrides (instance id, role, static IP); git-ignored

Secrets never come from these files; see `app.config.secrets`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from app.config.schema import PlatformConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FILES = (REPO_ROOT / "config" / "default.yaml", REPO_ROOT / "config" / "host.yaml")


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, Mapping) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(files: tuple[Path, ...] = DEFAULT_FILES) -> PlatformConfig:
    merged: dict[str, Any] = {}
    for f in files:
        if not f.exists():
            continue
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{f}: top level must be a mapping")
        merged = _deep_merge(merged, data)
    return PlatformConfig.model_validate(merged)


def canonical_json(obj: BaseModel | Mapping[str, Any]) -> str:
    """Deterministic JSON: sorted keys, no whitespace, Decimals/dates as strings."""
    data = obj.model_dump(mode="json") if isinstance(obj, BaseModel) else obj
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def config_hash(obj: BaseModel | Mapping[str, Any]) -> str:
    """Short SHA-256 of the canonical JSON. Stored with every signal and trade (section 7)."""
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()[:16]
