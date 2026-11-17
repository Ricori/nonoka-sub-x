"""Read one table of the shared `config.toml` from the provisioning layer.

The main package reads `config.toml` through `finesub.settings`, but this
layer runs in the thin launcher, which has no `finesub` on its path yet -- so
the few settings that decide what the launcher itself does are read here, with
`tomllib`, and nowhere else in `finesub_bootstrap`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def toml_parser():
    """`tomllib.load`, or the `tomli` backport, or None.

    Imported here rather than at module scope: `tomllib` is 3.11+, and this
    module is imported before *every* command the published CLI runs -- whose
    shell supports Python 3.10 (`cli/pyproject.toml`). A top-level import turned
    "no update notice" into `ModuleNotFoundError` for `setup`, transcription and
    uninstall alike. The wheel declares `tomli` for 3.10 so the settings keep
    working there; None is the belt-and-braces path if even that is absent.
    """

    try:
        from tomllib import load
    except ImportError:
        try:
            from tomli import load  # type: ignore[no-redef]
        except ImportError:
            return None
    return load


def read_section(user_data: Path | None, name: str) -> dict[str, Any] | None:
    """The `[name]` table of `<user_data>/config.toml`.

    `{}` when there is no such file, no such table, or a file nobody can parse
    -- a config that cannot be read states nothing. None only when there is a
    file but no TOML parser to read it with: then the caller cannot tell what
    the user wrote, and has to decide which way to fail.
    """

    if user_data is None:
        return {}
    config = Path(user_data) / "config.toml"
    if not config.is_file():
        return {}
    parser = toml_parser()
    if parser is None:
        return None
    try:
        with config.open("rb") as handle:
            data = parser(handle)
    except (OSError, ValueError):
        return {}
    section = data.get(name)
    return section if isinstance(section, dict) else {}
