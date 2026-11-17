"""`finesub_bootstrap.logs` for the finesub-knowledge package: only the names its FineSub code uses (WP-P1).

Copied from FineSub v0.7.0 (`7123055`), `src/finesub_bootstrap/logs.py`, as
vendored in Nonoka X (the transcription package carries the original). Kept: the
top-level definitions the names below need, unchanged and in the original's order;
everything else is left out. `__all__` is exactly the names this package's FineSub
code imports (docs/engine-packages.md section 4); `tests/test_engine_packages.py`
holds this package to them.

The original's description:

    The one log directory, and the one rule for how much of it to keep.

    Installs and app sessions both write here. They share the budget on purpose:
    "keep the newest hundred files" is a promise about the folder, and two
    independent hundreds would be two folders' worth of clutter under one name.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["prune"]


# Small text files -- a hundred of them is a couple of megabytes and covers
# more history than anyone will look at, without growing forever.
DEFAULT_KEEP = 100


def prune(directory: Path | None, *, keep: int = DEFAULT_KEEP) -> None:
    """Drop all but the newest `keep` logs, whatever wrote them."""

    if directory is None:
        return
    try:
        # By modification time, not by name: the names are
        # install-<resource>-<stamp> and session-<stamp>, so sorting them puts
        # the prefix ahead of the timestamp and "the newest hundred" would mean
        # the hundred belonging to whichever prefix sorts last.
        logs = sorted(
            Path(directory).glob("*.log"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for stale in logs[keep:]:
        try:
            stale.unlink()
        except OSError:
            # Someone has it open; the next prune will try again.
            pass
