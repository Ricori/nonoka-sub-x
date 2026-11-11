"""Where Nonoka keeps things: the data layout of `docs/transcription-engines-plan.md` 4.4 item 24.

Starts from FineSub v0.7.0's `finesub_bootstrap/paths.py` (`AppPaths`, 7123055):
one frozen record of every location, derived from the roots and nothing else.
What FineSub layered on top -- `locations.json`, the `.finesub-store.json`
marker, `register-location.cmd`, the install/app/versions tree -- is not carried
over: Nonoka has no `locations.json` (R0 document D18) and the big-file root is
found from `storage.json` alone.

Two roots:

* the **main data directory** (decided by the desktop: `%APPDATA%\\Nonoka X` on
  Windows) holds Nonoka's own small files -- `settings.json`, `secrets.json`,
  tasks, the library -- plus `runtime-state\\` (the installer's small state,
  D18), `engines\\<package>\\` (each engine package's private directory, D4;
  the transcription package's sandbox under it, plan 4.5) and
  `tasks\\<id>\\native\\<package>\\` (an engine's own outputs, D7);
* the **big-file root** `runtime` (default `<main data directory>\\runtime`; a
  relocated one is recorded as `runtimeDir` in `storage.json`) holds the
  prebuilt layers, `layers\\<layer>\\<version>\\` with `current.json` beside the
  versions (plan 4.2 item 19), and `cache\\`, which only holds what may be
  deleted at any time.

Nothing here creates a directory: this is the map, not the installer.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re

#: The desktop's record of relocated directories (`desktop/internal/storage/locations.go`).
STORAGE_RECORD_NAME = "storage.json"
#: The big-file root's name, by default inside the main data directory.
RUNTIME_DIRECTORY_NAME = "runtime"
LAYERS_DIRECTORY_NAME = "layers"
#: Beside a layer's versions: which one is in use.
CURRENT_POINTER_NAME = "current.json"
CACHE_DIRECTORY_NAME = "cache"
RUNTIME_STATE_DIRECTORY_NAME = "runtime-state"
ENGINES_DIRECTORY_NAME = "engines"
TASKS_DIRECTORY_NAME = "tasks"
NATIVE_DIRECTORY_NAME = "native"

#: One path component that names something of ours: a layer, a version, a
#: package, a task, a process. No separators, no `..`, nothing Windows refuses.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


def _component(kind: str, value: str) -> str:
    if not isinstance(value, str) or not _NAME.match(value) or value.endswith(".") or ".." in value:
        raise ValueError(f"not a valid {kind} name: {value!r}")
    return value


def recorded_runtime_dir(data_dir: Path) -> Path | None:
    """The big-file root `storage.json` records, or None for the default.

    Read the way the desktop reads it (`storage.Load`): a missing, unreadable
    or corrupt record means "still the default", and so does a relative path,
    which would otherwise resolve against whatever directory the process
    happens to be in. A recorded directory that has since gone missing is still
    returned, as the desktop does: falling back would hide an installed runtime
    on an unplugged disk.
    """

    try:
        record = json.loads((Path(data_dir) / STORAGE_RECORD_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = record.get("runtimeDir") if isinstance(record, dict) else None
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value.strip())
    return Path(os.path.normpath(path)) if path.is_absolute() else None


@dataclass(frozen=True, slots=True)
class NonokaPaths:
    """Every location of one installation, derived from its two roots."""

    data: Path
    runtime: Path
    layers: Path
    cache: Path
    runtime_state: Path
    engines: Path
    tasks: Path

    @classmethod
    def for_roots(cls, data_dir: Path | str, runtime_dir: Path | str | None = None) -> "NonokaPaths":
        """Lay out an installation from its main data directory.

        `runtime_dir` is the big-file root when the caller already knows it
        (the desktop passes it to the sidecar); otherwise `storage.json`'s
        record, else `<data_dir>\\runtime`.
        """

        data = Path(data_dir).expanduser().resolve()
        if runtime_dir is not None:
            runtime = Path(runtime_dir).expanduser().resolve()
        else:
            runtime = recorded_runtime_dir(data) or data / RUNTIME_DIRECTORY_NAME
        return cls(
            data=data,
            runtime=runtime,
            layers=runtime / LAYERS_DIRECTORY_NAME,
            cache=runtime / CACHE_DIRECTORY_NAME,
            runtime_state=data / RUNTIME_STATE_DIRECTORY_NAME,
            engines=data / ENGINES_DIRECTORY_NAME,
            tasks=data / TASKS_DIRECTORY_NAME,
        )

    # -- the big-file root --------------------------------------------------------------------------------------

    def layer(self, layer: str) -> Path:
        """`layers\\<layer>\\`: every version of one layer, and its pointer."""

        return self.layers / _component("layer", layer)

    def layer_version(self, layer: str, version: str) -> Path:
        """`layers\\<layer>\\<version>\\`: one unpacked version (the version is its manifest's hash)."""

        return self.layer(layer) / _component("layer version", version)

    def layer_pointer(self, layer: str) -> Path:
        """`layers\\<layer>\\current.json`: which version is in use."""

        return self.layer(layer) / CURRENT_POINTER_NAME

    # -- the main data directory --------------------------------------------------------------------------------

    def engine(self, package: str) -> Path:
        """`engines\\<package>\\`: one engine package's private directory (D4)."""

        return self.engines / _component("package", package)

    def engine_run(self, package: str, process: str) -> Path:
        """`engines\\<package>\\run\\<process>\\`: one process's derived files, removed when it ends (D6)."""

        return self.engine(package) / "run" / _component("process", process)

    def engine_user_data(self, package: str) -> Path:
        """`engines\\<package>\\user-data\\`: logs and reference data (D21)."""

        return self.engine(package) / "user-data"

    def engine_state(self, package: str) -> Path:
        """`engines\\<package>\\state`: the package's state file."""

        return self.engine(package) / "state"

    def engine_sandbox(self, package: str) -> Path:
        """`engines\\<package>\\sandbox\\`: where a sandboxed package's default locations point (plan 4.5)."""

        return self.engine(package) / "sandbox"

    def task(self, task_id: str) -> Path:
        return self.tasks / _component("task", task_id)

    def task_native(self, task_id: str, package: str) -> Path:
        """`tasks\\<id>\\native\\<package>\\`: an engine's own outputs for one task (D7)."""

        return self.task(task_id) / NATIVE_DIRECTORY_NAME / _component("package", package)


__all__ = [
    "CURRENT_POINTER_NAME",
    "RUNTIME_DIRECTORY_NAME",
    "STORAGE_RECORD_NAME",
    "NonokaPaths",
    "recorded_runtime_dir",
]
