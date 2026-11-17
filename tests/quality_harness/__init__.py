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
#: The engine packages as committed: what the locks and pins are about.
PACKAGES_ROOT = ROOT / "engines"
PACKAGE_IDS = ("finesub-transcribe", "finesub-correct", "finesub-knowledge")
#: Each package's FineSub sources (`engines/<package>/src`).
SOURCES = {package: PACKAGES_ROOT / package / "src" for package in PACKAGE_IDS}
#: The transcription package's: FineSub's transcription half and the original `finesub_bootstrap`.
TRANSCRIBE_SOURCE = SOURCES["finesub-transcribe"]
#: The engine packages a gate run uses (`nonoka_x.engine_host`); `NONOKA_QUALITY_ENGINES`
#: names another root (a test that runs a case with a package removed). A gate child that
#: sets a scenario up with FineSub's own code (a speaker timeline, say) imports the packages
#: through `union.install()`; the runs themselves happen in the packages' processes.
ENGINES = Path(os.environ.get("NONOKA_QUALITY_ENGINES") or PACKAGES_ROOT)


def source_file(relative: str) -> Path:
    """A FineSub file by its path below `src/`: the first package's copy that has it (transcription first).

    The packages' copies of one path are the same bytes (`tests/test_engine_packages.py`).
    """

    for package in PACKAGE_IDS:
        candidate = SOURCES[package] / relative
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"no engine package has {relative}")
FIXTURES = ROOT / "tests" / "fixtures" / "quality"
