"""`finesub_bootstrap.agy_records` for the finesub-correct package: only the names its FineSub code uses (WP-P1).

Copied from FineSub v0.7.0 (`7123055`), `src/finesub_bootstrap/agy_records.py`, as
vendored in Nonoka X (the transcription package carries the original). Kept: the
top-level definitions the names below need, unchanged and in the original's order;
everything else is left out. `__all__` is exactly the names this package's FineSub
code imports (docs/engine-packages.md section 4); `tests/test_engine_packages.py`
holds this package to them.

The original's description:

    The agy (Antigravity CLI) project records FineSub leaves behind.

    agy registers a project by writing ``~/.gemini/config/projects/<uuid>.json``
    itself (``--new-project``); FineSub only keeps the id. The location is agy's
    implementation detail -- read off its changelog, not a documented contract --
    so everything here treats it as foreign ground: a missing directory, a record
    that is not JSON, or a ``folderUri`` that is not a ``file://`` path is simply
    skipped, never an error.

    Stdlib-only on purpose: the uninstaller runs on the launcher's interpreter
    without the main package, and the main package's ``agent-clean`` shares the
    same function so the path is defined exactly once.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["agy_project_records_dir"]


def agy_project_records_dir() -> Path:
    return Path.home() / ".gemini" / "config" / "projects"
