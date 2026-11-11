"""Nonoka's own runtime manifests (docs/transcription-engines-plan.md, 4.2 item 13).

The files under `nonoka_x/resources/` are the source of truth for what the
FineSub transcription engine's environment is built from (the build pipeline's
input, WP-E1):

- `pylock.win-py312.toml`: the Python packages, copied from
  `finesub_bootstrap/pylock.win-py312.toml`. The China-mirror copy of that
  lock (`pylock.win-py312.cn.toml`) is not carried over: it differs only in
  download addresses, and Nonoka builds no mirror (4.2 item 18).
- `runtime-manifest.windows-x64.json`: the tools, copied from
  `finesub_bootstrap/runtime-manifest.json`, plus `interpreter`, the
  python-build-standalone build the Python environment layer is based on.
- `model-manifest.json`: the models, copied from
  `finesub_bootstrap/model-manifest.json`.

What they pin is recorded in `RUNTIME_PINS.json` and held to it by
`tests/test_runtime_pins.py`; changing a pin is an owner-approved engine
change (plan section 0, rules 5 and 7). This module only reads them.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

RESOURCES = Path(__file__).resolve().parents[1] / "resources"

RUNTIME_LOCK = RESOURCES / "pylock.win-py312.toml"
TOOL_MANIFEST = RESOURCES / "runtime-manifest.windows-x64.json"
MODEL_MANIFEST = RESOURCES / "model-manifest.json"


def runtime_lock() -> dict[str, Any]:
    """The runtime lock, as parsed TOML (PEP 751 `pylock.toml`)."""

    return tomllib.loads(RUNTIME_LOCK.read_text(encoding="utf-8"))


def tool_manifest() -> dict[str, Any]:
    """The Windows tool manifest: `resources` (the tools) and `interpreter`."""

    return json.loads(TOOL_MANIFEST.read_text(encoding="utf-8"))


def model_manifest() -> dict[str, Any]:
    """The model manifest: `models`, by model id."""

    return json.loads(MODEL_MANIFEST.read_text(encoding="utf-8"))


def interpreter() -> dict[str, Any]:
    """The pinned Python interpreter build: id, version, build name and `asset` (url, size, sha256)."""

    return tool_manifest()["interpreter"]
