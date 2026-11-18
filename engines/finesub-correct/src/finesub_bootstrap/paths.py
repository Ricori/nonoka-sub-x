"""`finesub_bootstrap.paths` for the finesub-correct package: only the names its FineSub code uses (WP-P1).

Copied from FineSub v0.7.0 (`7123055`), `src/finesub_bootstrap/paths.py`, as
vendored in Nonoka X (the transcription package carries the original). Kept: the
top-level definitions the names below need, unchanged and in the original's order;
everything else is left out. `__all__` is exactly the names this package's FineSub
code imports (docs/engine-packages.md section 4); `tests/test_engine_packages.py`
holds this package to them.

Written here, not copied: `default_data_root` and `load_app_paths` take the
package's data directory from the launch context (`_launch`, R0 document D21).

The original's description:

    Where a FineSub installation keeps things, and how it finds them again.

    Three roots, by how the data behaves rather than by who wrote it:

    * **data root** (`%LOCALAPPDATA%\\FineSub`) -- small and irreplaceable: settings,
      API keys, the knowledge base, task history. Identical for every end-user
      form, because one user's knowledge base should not depend on which front end
      opened it. This is also the anchor: everything else can be found from here.
    * **install root** -- the application itself plus `runtime/`. Version-bound and
      private to one installation, so it never moves on its own and is never
      shared: two installs at different versions would rebuild each other's
      environment in turn.
    * **big-data root** -- `models/`, `cache/`, `tasks/`, `agent-capsules/`.
      Defaults to the install root (a fresh install is self-contained) and may be
      pointed elsewhere, in which case several installations share one copy. Its location is recorded in
      `locations.json` beside the data root.

    The record is an accelerator, never the source of truth: every location can be
    re-derived from where the code is running, so a user who moves folders in
    Explorer is recovered from rather than punished.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from . import _launch

__all__ = ["default_data_root", "load_app_paths", "packaged_app_paths", "packaged_app_root"]


# Release packages put the pipeline sources at <root>/app/versions/<version>/,
# a tree that carries pyproject.toml and src/finesub and so is
# indistinguishable from a source checkout by content alone.
_APP_VERSIONS_LAYOUT = ("versions", "app")


@dataclass(frozen=True, slots=True)
class AppPaths:
    root: Path
    app: Path
    app_versions: Path
    app_current: Path
    runtime: Path
    data_root: Path
    user_data: Path
    big_data: Path
    models: Path
    cache: Path
    tasks: Path
    agent_capsules: Path
    logs: Path

    @classmethod
    def for_root(
        cls,
        root: Path,
        *,
        data_root: Path | None = None,
        big_data: Path | None = None,
    ) -> "AppPaths":
        """Lay out one installation.

        ``data_root`` defaults to the managed location shared by every end-user
        form; ``big_data`` defaults to the install root, which is what makes a
        fresh install self-contained.
        """

        resolved = root.expanduser().resolve()
        resolved_data = (
            data_root.expanduser().resolve()
            if data_root is not None
            else default_data_root()
        )
        resolved_big = (
            big_data.expanduser().resolve() if big_data is not None else resolved
        )
        user_data = resolved_data / "user-data"
        app = resolved / "app"
        return cls(
            root=resolved,
            app=app,
            app_versions=app / "versions",
            app_current=app / "current.json",
            runtime=resolved / "runtime",
            data_root=resolved_data,
            user_data=user_data,
            big_data=resolved_big,
            models=resolved_big / "models",
            cache=resolved_big / "cache",
            tasks=resolved_big / "tasks",
            agent_capsules=resolved_big / "agent-capsules",
            logs=user_data / "logs",
        )

    def with_big_data(self, big_data: Path) -> "AppPaths":
        resolved = big_data.expanduser().resolve()
        return replace(
            self,
            big_data=resolved,
            models=resolved / "models",
            cache=resolved / "cache",
            tasks=resolved / "tasks",
            agent_capsules=resolved / "agent-capsules",
        )


def packaged_app_root(source: Path) -> Path | None:
    """The install root shipping `source`, if it is a packaged app snapshot.

    Lets code that only knows where it is imported from tell a release package
    apart from a source checkout -- the pipeline resolves personal-data paths
    that way when it runs outside the launcher, which would otherwise walk up
    into ``app/versions/<version>`` and write there. That directory is replaced
    wholesale by the next update, so anything landing in it is lost silently.
    """

    resolved = source.expanduser().resolve()
    for candidate in (resolved, *resolved.parents):
        parent = candidate.parent
        if (parent.name, parent.parent.name) == _APP_VERSIONS_LAYOUT:
            return parent.parent.parent
    return None


def packaged_app_paths(source: Path) -> AppPaths | None:
    """`AppPaths` of the install shipping `source`; None outside a package."""

    root = packaged_app_root(source)
    if root is None:
        return None
    return load_app_paths(root)


def default_data_root() -> Path:
    """The package's own data directory, from its launch context (D21).

    The original answers ``%LOCALAPPDATA%\\FineSub`` (or the macOS/POSIX
    equivalent); here every location the engine derives from the data root --
    `user-data`, its logs, `agent-capsules` -- lands under
    ``engines\\<package>\\`` in Nonoka's data directory instead.
    """

    return _launch.package_data()


def load_app_paths(
    root: Path,
    *,
    data_root: Path | None = None,
    log: Callable[[str], None] | None = None,
) -> AppPaths:
    """`AppPaths` for `root`, as the original lays it out when nothing is recorded.

    The original also honours a big-data root recorded in ``locations.json``
    beside the data root and falls back across an interrupted move; Nonoka's
    engine packages have neither, so big data stays under `root` -- the
    original's answer for an installation without a record. Read-only, like
    the original. `log` is accepted for the callers' sake and unused.
    """

    resolved_data = (
        data_root.expanduser().resolve()
        if data_root is not None
        else default_data_root()
    )
    resolved_root = root.expanduser().resolve()
    return AppPaths.for_root(resolved_root, data_root=resolved_data, big_data=resolved_root)
