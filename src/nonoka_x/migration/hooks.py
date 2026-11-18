"""The steps migration leaves to other work packages, and how they plug in.

WP-M1 is the core: detect, plan, rename, delete; WP-M2 added the knowledge
base, the rate-limit state and the old tasks (`runner`, `tasks`). Three steps
belong to code outside the migration package:

| step | owner | what it does |
|---|---|---|
| `settings` | WP-R4 (`config/migrate.py`) | read the old settings and keys into `settings.json` / `secrets.json`, rename the old files as a backup |
| `model-completion` | WP-E3 | check the moved models against the model layer manifest, fetch only missing or wrong files from R2, then register the layer version |
| `layer-download` | WP-E3 | download the Python environment and tool layers |

Each is a `StepHook`. Without one, the step stays in the plan, says what it
will touch and who implements it, and is skipped; a later run with the hook
runs a skipped step after all (`runner.prepare`). `available_hooks()` is what
the owners provide; the command line and the sidecar's start-up pass it,
library callers pass their own (or none).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from nonoka_x.migration.legacy import LegacyLayout
from nonoka_x.runtime.paths import NonokaPaths

SETTINGS = "settings"
KNOWLEDGE = "knowledge"
LIMIT_STATE = "limit-state"
TASK_PRODUCTS = "task-products"
MODEL_COMPLETION = "model-completion"
LAYER_DOWNLOAD = "layer-download"

#: Step id -> the work package that implements it.
DEFERRED_OWNERS: Mapping[str, str] = {
    SETTINGS: "WP-R4 (config/migrate.py)",
    MODEL_COMPLETION: "WP-E3",
    LAYER_DOWNLOAD: "WP-E3",
}
#: Steps an older journal (WP-M1, before WP-M2) lists as deferred to WP-M2: the migration's own now.
FORMERLY_DEFERRED = frozenset({KNOWLEDGE, LIMIT_STATE, TASK_PRODUCTS})

#: Where WP-M1 leaves the moved models: a fixed name, not a layer version
#: (a version is its manifest's hash, plan 4.2 item 19). No `current.json`
#: points at it; `model-completion` checks it against the manifest, completes
#: it, renames it to its version and writes the pointer.
MIGRATED_MODELS_NAME = "migrated"
MODELS_LAYER = "models"


@dataclass(frozen=True)
class MigrationContext:
    """What a step works with.

    `progress` receives the download steps' progress (`runtime.layers.LayerProgress`);
    `engines` is where the engine packages are, for converting old tasks (None: the
    default, `engine_host.default_engines_root()`).
    """

    layout: LegacyLayout
    paths: NonokaPaths
    log: Callable[[str], None] = field(default=lambda message: None)
    progress: Callable[[Any], None] | None = None
    engines: Path | None = None

    @property
    def migrated_models(self) -> Path:
        """`<runtime>\\layers\\models\\migrated\\`."""

        return self.paths.layer_version(MODELS_LAYER, MIGRATED_MODELS_NAME)


@runtime_checkable
class StepHook(Protocol):
    """One deferred step, as its owner implements it.

    `download_bytes` answers "how much will this fetch" for the plan (None if
    it cannot say yet); it must not touch the disk or the network. `run` does
    the step. It must be safe to call again after being interrupted: the
    journal re-runs a step that did not finish.
    """

    def download_bytes(self, context: MigrationContext) -> int | None: ...

    def run(self, context: MigrationContext) -> None: ...


Hooks = Mapping[str, StepHook]


def available_hooks() -> dict[str, StepHook]:
    """The deferred steps whose owners have implemented them.

    `settings` (WP-R4, `config.migrate`); `model-completion` and `layer-download` (WP-E3,
    `model_install.ModelCompletionHook`, `runtime_install.LayerDownloadHook`: the layer manager
    against this build's `resources/layers.json`).
    """

    from nonoka_x.config.migrate import SettingsHook
    from nonoka_x.model_install import ModelCompletionHook
    from nonoka_x.runtime_install import LayerDownloadHook

    return {SETTINGS: SettingsHook(), MODEL_COMPLETION: ModelCompletionHook(), LAYER_DOWNLOAD: LayerDownloadHook()}

__all__ = [
    "DEFERRED_OWNERS",
    "FORMERLY_DEFERRED",
    "KNOWLEDGE",
    "LAYER_DOWNLOAD",
    "LIMIT_STATE",
    "MIGRATED_MODELS_NAME",
    "MODELS_LAYER",
    "MODEL_COMPLETION",
    "SETTINGS",
    "TASK_PRODUCTS",
    "Hooks",
    "MigrationContext",
    "StepHook",
    "available_hooks",
]
