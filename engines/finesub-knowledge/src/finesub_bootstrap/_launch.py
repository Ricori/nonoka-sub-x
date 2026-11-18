"""The launch context this compatibility package takes its locations from.

Nonoka code; not a copy of anything in FineSub. The package's bridge binds
it from the launch context (`paths.package_data`) before anything imports
`finesub`, so the engine never falls back to a location of its own: a
process that was not given one fails here, saying so, instead of writing to
``%LOCALAPPDATA%\\FineSub``.
"""

from __future__ import annotations

from pathlib import Path

_PACKAGE_DATA: Path | None = None


def bind(package_data: str | Path) -> None:
    """Bind the package's data directory, ``engines\\<package>\\`` in Nonoka's data directory."""

    global _PACKAGE_DATA
    _PACKAGE_DATA = Path(package_data).expanduser().resolve()


def package_data() -> Path:
    if _PACKAGE_DATA is None:
        raise RuntimeError(
            "finesub_bootstrap (compatibility package): no package data directory was bound from the "
            "launch context (paths.package_data); this process was not started by Nonoka's engine host"
        )
    return _PACKAGE_DATA
