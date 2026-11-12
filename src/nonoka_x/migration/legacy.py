"""The old data layout, as far as migration needs to recognise it.

The inventory is `docs/runtime-and-config.md` 1.13 (WP-R0), read with the
plan's replacements (`docs/transcription-engines-plan.md` 4.4 items 24-25,
appendix A.3: D5, D10, D18, D21 and D23 are superseded):

* the **main data directory** (`%APPDATA%\\Nonoka X`, `~/Library/Application
  Support/Nonoka X`, or a development checkout's `.local`) held FineSub's
  settings and keys, the model catalog, the rate-limit state, the knowledge
  base and `finesub-data\\`;
* the **big-file root** was named `finesub`: `<main>\\finesub` by default, or
  `<chosen>\\Nonoka X\\finesub` when `storage.json` records a relocation. It
  held `runtime\\` (the venv, the interpreter builds, the tools), `models\\`,
  `cache\\`, the store marker and the registration script.

This is the one module of `nonoka_x` that names FineSub's old files
(`tests/test_finesub_boundary.py` allows it exactly that): migration has to
recognise the layout it replaces. Nothing here reads a key or a setting --
files are only found, measured and, later, moved or deleted.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys

from nonoka_x.runtime.paths import RUNTIME_DIRECTORY_NAME, STORAGE_RECORD_NAME, TASKS_DIRECTORY_NAME, recorded_runtime_dir

#: The big-file root's old name (the desktop's `runtimeName` before WP-R6).
OLD_RUNTIME_NAME = "finesub"

# -- inside the old big-file root ------------------------------------------------------------------------------

#: FineSub's managed install directory: the venv, `python-builds\\` and the tools.
#: Cleared whole (plan 4.4 item 25; user 2026-11-12: unrecognised entries too).
OLD_INSTALL_DIRECTORY = "runtime"
OLD_PYTHON_ENVIRONMENTS = ("python", "python-builds")
OLD_TOOLS = ("ffmpeg", "uv", "tokcount", "yt-dlp", "aria2c", "node", "pot-provider")
OLD_MODELS_DIRECTORY = "models"
#: The uv cache and anything else the old installer kept as a cache.
OLD_CACHE_DIRECTORY = "cache"
#: FineSub's store marker and registration script: no longer needed (D18).
OLD_STORE_FILES = (".finesub-store.json", "register-location.cmd")

#: Inside the moved models: what is removed before the model layer check.
#: `accel\\` holds AOTI builds (acceleration is off, plan 0.1 item 1); `xet\\`
#: is hf_xet's download cache; `.locks\\` and `*.lock` are download locks
#: (user 2026-11-12). Every other extra file is judged against the model layer
#: manifest by WP-E3.
MODEL_EXTRA_DIRECTORIES = (
    ("audio-separator", "accel"),
    ("huggingface", "xet"),
    ("huggingface", "hub", ".locks"),
)
MODEL_LOCK_SUFFIX = ".lock"

# -- inside the main data directory ----------------------------------------------------------------------------

#: Settings and keys: read into `settings.json` / `secrets.json` by WP-R4's
#: `config/migrate.py`, then renamed as a backup.
SETTINGS_FILES = ("finesub.env", "finesub.toml")
#: Derived per process from now on, so deleted (R0 2.7 item 5).
REBUILDABLE_FILES = ("model_catalog.toml", "model_catalog.psv")
FINESUB_DATA_DIRECTORY = "finesub-data"
#: `locations.json` is gone (D18) and so is download-route state (no mirrors, 4.4 item 24).
FINESUB_DATA_FILES = ("locations.json", "locations.lock", "download-routes.json")
#: Moved by WP-M2, content unchanged (plan 4.4 item 25, 4.6 item 35a).
KNOWLEDGE_ENTRIES = ("knowledge", "knowledge.lock")
LIMIT_STATE_ENTRIES = ("state", "state.lock")
APP_RESOURCES_DIRECTORY = "app-resources"
#: A staged engine is a source checkout as far as FineSub can tell, so its
#: compiled-kernel cache lands inside it (R0 1.8). Only this directory is
#: touched under `app-resources\\` (user 2026-11-12).
STAGED_VENDOR_CACHE = ("nonoka_x", "third_party", "finesub", "cache")
#: The same cache in a development checkout.
CHECKOUT_VENDOR_CACHE = ("third_party", "finesub", "cache")
#: A development checkout's main data directory (`scripts/run_local_sidecar.py`).
CHECKOUT_DATA_DIRECTORY = ".local"

#: Task states that mean nothing is running (`local_provider.TERMINAL`).
TERMINAL_TASK_STATES = frozenset({"completed", "failed", "cancelled"})


def default_data_dir() -> Path | None:
    """The desktop's main data directory on this platform (`desktop/internal/app/runtime.go`)."""

    if sys.platform == "win32":
        app_data = os.environ.get("APPDATA", "").strip()
        return Path(app_data) / "Nonoka X" if app_data else None
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Nonoka X"
    return None


@dataclass(frozen=True)
class LegacyLayout:
    """Where one installation's old layout is, and where its runtime goes.

    `old_root` is the old big-file root (it may be missing on disk: a recorded
    root on an unplugged drive is still the root); `new_root` is its sibling
    named `runtime`, so the rename never leaves the volume (R0 2.7 item 2).
    """

    data: Path
    old_root: Path | None
    new_root: Path
    recorded: bool
    checkout: Path | None = None

    def under_old_root(self, *parts: str) -> Path | None:
        return self.old_root.joinpath(*parts) if self.old_root is not None else None

    def current(self, path: Path) -> Path:
        """Where `path`, named under the old root, is now.

        Every step after the root rename finds what it works on under the new
        root; naming it by its old location keeps the plan readable to someone
        looking at the disk before migration.
        """

        if self.old_root is None:
            return path
        try:
            relative = path.relative_to(self.old_root)
        except ValueError:
            return path
        if os.path.lexists(self.old_root):
            return path
        return self.new_root / relative


def detect(data_dir: Path | str, *, checkout: Path | str | None = None) -> LegacyLayout:
    """Find the old big-file root of the installation at `data_dir`.

    `storage.json` decides, as it does for the desktop: a recorded root not
    yet named `runtime` is the old one (whether or not it is on disk); no
    record means the default `<data>\\finesub`, which counts only if it exists.
    """

    data = Path(os.path.abspath(Path(data_dir).expanduser()))
    recorded = recorded_runtime_dir(data)
    if recorded is not None:
        if recorded.name == RUNTIME_DIRECTORY_NAME:
            return LegacyLayout(data=data, old_root=None, new_root=recorded, recorded=True, checkout=_checkout(checkout))
        return LegacyLayout(
            data=data,
            old_root=recorded,
            new_root=recorded.parent / RUNTIME_DIRECTORY_NAME,
            recorded=True,
            checkout=_checkout(checkout),
        )
    default = data / OLD_RUNTIME_NAME
    return LegacyLayout(
        data=data,
        old_root=default if os.path.lexists(default) else None,
        new_root=data / RUNTIME_DIRECTORY_NAME,
        recorded=False,
        checkout=_checkout(checkout),
    )


def _checkout(checkout: Path | str | None) -> Path | None:
    return Path(os.path.abspath(Path(checkout).expanduser())) if checkout is not None else None


def staged_vendor_caches(data: Path) -> list[Path]:
    """`app-resources\\<version>\\nonoka_x\\third_party\\finesub\\cache\\` of every staged version."""

    resources = data / APP_RESOURCES_DIRECTORY
    try:
        versions = sorted(entry for entry in resources.iterdir() if entry.is_dir())
    except OSError:
        return []
    return [cache for version in versions if os.path.lexists(cache := version.joinpath(*STAGED_VENDOR_CACHE))]


def running_tasks(data: Path) -> list[tuple[str, str]]:
    """Tasks whose snapshot says they are not finished: `(task id, state)`.

    A snapshot that cannot be read is not counted: the sidecar could not have
    resumed that task either.
    """

    found: list[tuple[str, str]] = []
    try:
        tasks = sorted((data / TASKS_DIRECTORY_NAME).iterdir())
    except OSError:
        return found
    for task in tasks:
        try:
            snapshot = json.loads((task / "snapshot.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        state = snapshot.get("state") if isinstance(snapshot, dict) else None
        if isinstance(state, str) and state not in TERMINAL_TASK_STATES:
            found.append((task.name, state))
    return found


def storage_record_path(data: Path) -> Path:
    return data / STORAGE_RECORD_NAME


__all__ = [
    "LegacyLayout",
    "default_data_dir",
    "detect",
    "running_tasks",
    "staged_vendor_caches",
]
