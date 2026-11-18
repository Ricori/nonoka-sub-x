"""Shared test import paths: the core, and the engine packages' code side by side.

Since WP-P1 FineSub runs in its engine packages' own processes
(`nonoka_x.engine_host`). A test that exercises a package's code in-process
-- an adapter, FineSub's own modules, the knowledge protocol's two ends --
imports it from the packages' import roots (`tests.quality_harness.union`).
Tests about the packages' isolation start real package processes instead.
"""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENGINES = ROOT / "engines"
for _root in (ROOT, ROOT / "src"):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

from tests.quality_harness import union  # noqa: E402

union.install()
