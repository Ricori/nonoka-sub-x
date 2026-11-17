#!/usr/bin/env python3
"""Run the development Local Provider sidecar from a source checkout."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nonoka_x.sidecar import main


if __name__ == "__main__":
    raise SystemExit(main(["--data-dir", str(ROOT / ".local"), "--engines", str(ROOT / "engines"), *sys.argv[1:]]))

