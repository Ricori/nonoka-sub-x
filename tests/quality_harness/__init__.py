"""Shared machinery of the FineSub quality gate (`scripts/quality_gate.py`).

The gate exists to stop FineSub's transcription, correction and translation
results from changing while the orchestration around them is rebuilt
(`docs/transcription-engines-plan.md`, section 0 and WP-Q). Each module here is
one of its checks:

- `lock`: the locked engine files, byte for byte.
- `calls`: what Nonoka hands each FineSub algorithm entry point, for a matrix
  of task requests, plus the worker environment and generated config.
- `replay`: what the correction layer sends to the model for a fixed stable,
  and what it renders from fixed answers.
- `real`: real recognition runs on the sample media (needs the runtime).

Nothing here imports torch or numpy, and nothing reaches the network.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "third_party" / "finesub"
VENDOR_SOURCE = VENDOR / "src"
#: The engine packages (`nonoka_x.engine_host`); `NONOKA_QUALITY_ENGINES` names another root
#: (a test that runs a case with a package removed).
ENGINES = Path(os.environ.get("NONOKA_QUALITY_ENGINES") or ROOT / "engines")
#: What a gate child puts on its own import path to set a scenario up with FineSub's own code
#: (a speaker timeline, say). The runs themselves happen in the packages' processes.
SETUP_SOURCES = (VENDOR_SOURCE,)
FIXTURES = ROOT / "tests" / "fixtures" / "quality"
