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

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "third_party" / "finesub"
VENDOR_SOURCE = VENDOR / "src"
FIXTURES = ROOT / "tests" / "fixtures" / "quality"
