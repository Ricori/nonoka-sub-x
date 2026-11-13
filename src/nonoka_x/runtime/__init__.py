"""Nonoka's managed runtime, behind one facade.

New code (the transcription engines, WP1/WP3/WP5) asks about installed components, tool paths and
the worker environment only through here, never through `nonoka_x.provision` directly
(`docs/transcription-engines-plan.md`, section 6).

Since WP-E3 the answers come from the prebuilt layers (`nonoka_x.runtime.layers.LayerManager`, plan
4.2 item 19): the Python environment layer's interpreter, the tool layer's executables, the model
layer's model directories, each only when the version this release wants is the current one and
checks out. The facade is built over the runtime page's provisioner (`provision.RuntimeProvisioner`,
which owns the manager and the install job) so that `status()` -- what the two FineSub adapters read,
with `tool_path()` (WP5) -- keeps its shape.

Component ids, as engines name them in `EngineEnvironment`:

- `asr-runtime`: the Python environment layer the local ASR stack runs in;
- `media`: ffmpeg and ffprobe together (tool layer);
- every managed tool by its own id (`ffmpeg`, `tokcount`, `yt-dlp`, ...);
- a layer by its own id (`python`, `models`, `tools`).

Model ids are those of Nonoka's model manifest (`resources/model-manifest.json`); the model layer
holds `layer_manifest.MODEL_LAYER_IDS`, and `model_dir` answers None for any other.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from nonoka_x.runtime import layer_manifest

ASR_RUNTIME = "asr-runtime"
MEDIA = "media"
#: The tools `tool_path` can locate.
TOOLS = ("ffmpeg", "ffprobe", "tokcount")
#: The layers, as component ids.
LAYERS = layer_manifest.LAYERS


class UnknownComponent(LookupError):
    pass


class RuntimeManager:
    """The managed runtime of one install root."""

    def __init__(self, provisioner: Any | None) -> None:
        # A `provision.RuntimeProvisioner` (or something answering for one), or None where the
        # sidecar has none (tests, a launcher that failed to load): then nothing is installed.
        self._provisioner = provisioner

    @classmethod
    def open(
        cls,
        data_dir: str | Path,
        vendor: str | Path,
        install_dir: str | Path | None = None,
    ) -> "RuntimeManager":
        from nonoka_x.provision import RuntimeProvisioner

        return cls(RuntimeProvisioner(data_dir, vendor, install_dir))

    @property
    def layers(self) -> Any | None:
        """The `LayerManager`, or None when there is none (no provisioner, no layers.json, not Windows)."""

        if self._provisioner is None:
            return None
        return getattr(self._provisioner, "layers", None)

    def status(self) -> Mapping[str, Any]:
        """The provisioner's full status payload (what the runtime page shows)."""

        if self._provisioner is None:
            return {}
        return self._provisioner.status()

    def asr_runtime_available(self) -> bool:
        """Whether the Python environment layer is installed and ready."""

        return self.worker_python() is not None

    def worker_python(self) -> Path | None:
        """The Python environment layer's interpreter (`provision.worker_python`), or None."""

        if self._provisioner is None:
            return None
        return self._provisioner.worker_python()

    def tool_path(self, name: str) -> Path | None:
        """The managed executable `name` (one of `TOOLS`) in the tool layer, or None when absent."""

        if name not in TOOLS:
            raise UnknownComponent(f"no tool {name!r}; known: {', '.join(TOOLS)}")
        if self._provisioner is None:
            return None
        return self._provisioner.tool_path(name)

    def layer_ready(self, layer: str) -> bool:
        if layer not in LAYERS:
            raise UnknownComponent(f"no layer {layer!r}; known: {', '.join(LAYERS)}")
        layers = self.layers
        return layers is not None and layer in layers.index.layers and layers.state(layer).ready

    def component_ready(self, component: str) -> bool:
        if component == ASR_RUNTIME:
            return self.asr_runtime_available()
        if component == MEDIA:
            return self.tool_path("ffmpeg") is not None and self.tool_path("ffprobe") is not None
        if component in TOOLS:
            return self.tool_path(component) is not None
        if component in LAYERS:
            return self.layer_ready(component)
        resources = {item.get("id"): item for item in self.status().get("resources") or ()}
        if self._provisioner is not None and component not in resources:
            raise UnknownComponent(f"no runtime component {component!r}")
        item = resources.get(component)
        return item is not None and item.get("state") == "ready"

    def model_ready(self, model_id: str) -> bool:
        """Whether the runtime page lists `model_id` (a model of the model layer) as ready."""

        status = self.status()
        models = [*(status.get("models") or ()), *(status.get("optional_models") or ())]
        by_id = {item.get("id"): item for item in models}
        if self._provisioner is not None and model_id not in by_id:
            raise UnknownComponent(f"no managed model {model_id!r}")
        item = by_id.get(model_id)
        return item is not None and item.get("state") == "ready"

    def model_dir(self, model_id: str) -> Path | None:
        """The installed files of model `model_id` (an id of Nonoka's model manifest), or None.

        Inside the model layer: the pinned snapshot directory for a Hugging Face model, the model's
        directory otherwise; only when the model layer is ready and holds the model. Nothing is ever
        downloaded here; a caller without the model goes without it.
        """

        from nonoka_x.runtime import manifests

        if model_id not in (manifests.model_manifest().get("models") or {}):
            raise UnknownComponent(f"no model {model_id!r} in the model manifest")
        layers = self.layers
        return layers.model_dir(model_id) if layers is not None else None

    def layer_overview(self) -> Mapping[str, Any]:
        """Every layer's state and size and the unused versions (WP-UI's "clean up unused" reads this)."""

        layers = self.layers
        return layers.overview() if layers is not None else {"layers": [], "unused": [], "unused_bytes": 0}

    def check(self, *, full: bool = False) -> Mapping[str, Any]:
        """Each layer's readiness; `full` hashes every file (what a repair or a user's "verify" runs)."""

        layers = self.layers
        if layers is None:
            return {}
        return {layer: state.to_dict() for layer, state in layers.states(full=full).items()}

    def worker_environment(self) -> dict[str, str]:
        """The environment a worker process is spawned into.

        The provisioner's (`provision.worker_environment`): the parent's environment plus the model
        layer, the tool layer's directories and the proxy. WP-R5 and WP-H replace it with one built
        from nothing.
        """

        if self._provisioner is None:
            import os

            return os.environ.copy()
        return self._provisioner.worker_environment()


__all__ = ["ASR_RUNTIME", "LAYERS", "MEDIA", "TOOLS", "RuntimeManager", "UnknownComponent"]
