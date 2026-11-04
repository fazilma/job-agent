from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def _expand(p: str | os.PathLike) -> Path:
    path = Path(os.path.expanduser(str(p)))
    return path if path.is_absolute() else ROOT / path


class Config(dict):
    """Dict with a few helpers. Loaded fresh on every run."""

    def path(self, key: str) -> Path:
        return _expand(self["paths"][key])

    def section(self, *keys: str, default: Any = None) -> Any:
        node: Any = self
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node


def load_config(path: str | os.PathLike | None = None) -> Config:
    load_dotenv(ROOT / ".env")
    cfg_path = Path(path) if path else Path(os.environ.get("JOBAGENT_CONFIG", ROOT / "config.yaml"))
    with open(cfg_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return Config(data)
