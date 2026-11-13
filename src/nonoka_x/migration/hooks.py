"""The steps migration leaves to other work packages, and how they plug in.

WP-M1 is the core: detect, plan, rename, delete. Six steps belong to code
that does not exist yet (`docs/transcription-engines-plan.md` WP-M1, WP-M2):

| step | owner | what it will do |
|---|---|---|
| `settings` | WP-R4 (`config/migrate.py`) | read the old settings and keys into `settings.json` / `secrets.json`, rename the old files as a backup |
| `knowledge` | WP-M2 | move the knowledge base into `engines\\finesub-knowledge\\`, content unchanged |
| `limit-state` | WP-M2 | move the rate-limit state into `engines\\finesub-correct\\` |
| `task-products` | WP-M2 | file FineSub's outputs in each task under `native\\<package>\\` |
| `model-completion` | WP-E3 | check the moved models against the model layer manifest, fetch only missing or wrong files from R2, then register the layer version |
| `layer-download` | WP-E3 | download the Python environment and tool layers |

Each is a `StepHook`. Until its owner provides one, the step stays in the
plan, says what it will touch and who implements it, and is skipped.
`available_hooks()` is what the owners provide so far; the command line passes
it, library callers pass their own (or none).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

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
    KNOWLEDGE: "WP-M2",
    LIMIT_STATE: "WP-M2",
    TASK_PRODUCTS: "WP-M2",
    MODEL_COMPLETION: "WP-E3",
    LAYER_DOWNLOAD: "WP-E3",
}

#: Where WP-M1 leaves the moved models: a fixed name, not a layer version
#: (a version is its manifest's hash, plan 4.2 item 19). No `current.json`
#: points at it; `model-completion` checks it against the manifest, completes
#: it, renames it to its version and writes the pointer.
MIGRATED_MODELS_NAME = "migrated"
MODELS_LAYER = "models"


@dataclass(frozen=True)
class MigrationContext:
    """What a step works with."""

    layout: LegacyLayout
    paths: NonokaPaths
    log: Callable[[str], None] = field(default=lambda message: None)

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
