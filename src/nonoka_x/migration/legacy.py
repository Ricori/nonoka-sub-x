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

This is the one module of `nonoka_x` that names FineSub's old files and the
old key file's variable names (`tests/test_finesub_boundary.py` allows it
exactly that): migration has to recognise the layout it replaces. Nothing here reads a key or a setting --
files are only found, measured and, later, moved or deleted.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys

from nonoka_x.runtime.paths import RUNTIME_DIRECTORY_NAME, STORAGE_RECORD_NAME, TASKS_DIRECTORY_NAME, NonokaPaths, recorded_runtime_dir

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
#: The backups the settings migration leaves (`<name>.pre-migration`, `.1`, `.2` if taken); deleted
#: once the migration is past the point of rollback (owner 2026-11-18/19, plan appendix B questions 89-90).
SETTINGS_BACKUP_SUFFIX = ".pre-migration"
#: The old key file's protector line (`finesub_bootstrap/secrets.py:58`): read, never carried.
KEYRING_NAME = "FINESUB_KEYRING"
#: FineSub's names for the cloud ASR engines' keys -> Nonoka's (WP1; plan appendix B question 36).
LEGACY_KEY_RENAMES = {
    "FINESUB_KEY_OPENROUTER_STT": "OPENROUTER_STT_KEY",
    "FINESUB_KEY_AZURE_SPEECH": "AZURE_SPEECH_KEY",
    "FINESUB_AZURE_SPEECH_ENDPOINT": "AZURE_SPEECH_ENDPOINT",
}
#: Derived per process from now on, so deleted (R0 2.7 item 5).
REBUILDABLE_FILES = ("model_catalog.toml", "model_catalog.psv")
FINESUB_DATA_DIRECTORY = "finesub-data"
#: (`finesub-data\\` itself, with whatever else is left in it, goes at the same point.)
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


def settings_backups(data: Path) -> list[Path]:
    """The settings migration's backups now in `data`, in name order."""

    found: list[Path] = []
    for name in SETTINGS_FILES:
        found.extend(sorted(path for path in data.glob(f"{name}{SETTINGS_BACKUP_SUFFIX}*") if path.is_file()))
    return found


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


#: Where the old main data directory's knowledge base and rate-limit state go: the private
#: directories of the packages that use them (plan 4.4 item 25, `engines\\<package>\\`).
KNOWLEDGE_PACKAGE = "finesub-knowledge"
CORRECTION_PACKAGE = "finesub-correct"
#: Where a task's transcription outputs go (`tasks\\<id>\\native\\<package>\\`, plan 4.6 item 34).
TRANSCRIPTION_PACKAGE = "finesub-transcribe"

#: The knowledge base's directory and its siblings: the write lock (`<root>.lock`) and the
#: knowledge processes' endpoint registrations (`<root>.run`, only while one runs).
KNOWLEDGE_DIRECTORY = "knowledge"
KNOWLEDGE_SIBLINGS = (".lock",)
KNOWLEDGE_REGISTRATIONS = ".run"
#: FineSub's rate-limit state is one JSON file (`finesub.state`), its lock beside it. A
#: `state` *directory* in the main data directory is Nonoka's own (`ConfigStore.state_file`).
LIMIT_STATE_FILE = "state"
LIMIT_STATE_LOCK = "state.lock"


def knowledge_root(paths: NonokaPaths) -> Path:
    """The knowledge base from now on: `engines\\finesub-knowledge\\knowledge\\` (plan 4.4 item 25, 4.6 item 35a).

    Every package process is pointed here (`engine_contexts`); the migration's
    `knowledge` step is what moves an old one here.
    """

    return paths.engine(KNOWLEDGE_PACKAGE) / KNOWLEDGE_DIRECTORY


def limit_state_file(paths: NonokaPaths) -> Path:
    """The cross-process rate-limit state from now on: `engines\\finesub-correct\\state`."""

    return paths.engine_state(CORRECTION_PACKAGE)


# -- a task's outputs (WP-M2) ----------------------------------------------------------------------------------

#: An old task kept both halves' outputs in `tasks\\<id>\\workspace\\`, named after the
#: task's title (`<title>-stable.json`, ...; FineSub's `default_pipeline_paths`). The
#: transcription's go to its package's directory -- the stable and the speaker files too,
#: which the correction copies into its own on its first run ("copied once", WP-P0 question
#: 7, item 5) -- the metadata to both (each half keeps its own from now on), the rest to the
#: correction's. `docs/engine-packages.md` sections 7 and 8.
WORKSPACE_DIRECTORY = "workspace"
TRANSCRIPTION_SUFFIXES = (
    "-vocal.ogg",
    "-vocal.flac",
    "-vad.json",
    "-vad-energy.npz",
    "-aligned.json",
    "-stable.json",
    "-diar.json",
    "-diar-rows.json",
    "-diar-evidence.npz",
)
BOTH_SUFFIXES = ("-metadata.json",)

#: Where WP-R4 derived FineSub's files until WP-R5 (owner 2026-11-12): a sealed copy of the keys,
#: under the main data directory's `engines\`. Removed at start-up.
TRANSITIONAL_ENGINE_DIRECTORY = "finesub-shared"


def remove_transitional_engine_files(data: Path | str) -> None:
    """Remove what WP-R4's transitional derivation left in the main data directory, if anything."""

    from nonoka_x.runtime import fsops
    from nonoka_x.runtime.paths import ENGINES_DIRECTORY_NAME

    transitional = Path(data) / ENGINES_DIRECTORY_NAME / TRANSITIONAL_ENGINE_DIRECTORY
    if os.path.lexists(transitional):
        try:
            fsops.remove_tree(transitional)
        except OSError as error:
            print(f"could not remove {transitional}: {error}", file=sys.stderr, flush=True)


__all__ = [
    "LegacyLayout",
    "knowledge_root",
    "limit_state_file",
    "TRANSITIONAL_ENGINE_DIRECTORY",
    "default_data_dir",
    "detect",
    "remove_transitional_engine_files",
    "running_tasks",
    "staged_vendor_caches",
]
