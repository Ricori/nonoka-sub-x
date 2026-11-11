"""Nonoka's managed runtime, behind one facade.

New code (the transcription engines, WP1/WP3/WP5) asks about installed
components, tool paths and the worker environment only through here, never
through `nonoka_x.provision` directly, so the runtime line can replace what is
underneath (WP-R3, WP-R5) without touching the engine line
(`docs/transcription-engines-plan.md`, section 6).

For now this is a thin layer over `provision.RuntimeProvisioner`.

Component ids, as engines name them in `EngineEnvironment`:

- `asr-runtime`: the managed Python runtime the local ASR stack runs in;
- `media`: ffmpeg and ffprobe together;
- every managed tool by its own id (`ffmpeg`, `tokcount`, `yt-dlp`, ...).

Model ids are the provisioner's (`separator`, `whisper`, `qwen-referee`,
`nemotron3`, `speaker-embedding`); `model_dir` (WP1) takes any id of Nonoka's
model manifest (`resources/model-manifest.json`) and says where its files are.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

ASR_RUNTIME = "asr-runtime"
MEDIA = "media"
#: The tools `tool_path` can locate.
TOOLS = ("ffmpeg", "ffprobe", "tokcount")


class UnknownComponent(LookupError):
    pass


class RuntimeManager:
    """The managed runtime of one install root."""

    def __init__(self, provisioner: Any | None) -> None:
        # A `provision.RuntimeProvisioner`, or None where the sidecar has none
        # (tests, a bootstrap that failed to load): then nothing is installed.
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

    def status(self) -> Mapping[str, Any]:
        """The provisioner's full status payload (what the runtime page shows)."""

        if self._provisioner is None:
            return {}
        return self._provisioner.status()

    def asr_runtime_available(self) -> bool:
        """Whether the managed Python runtime is installed and ready."""

        return self._provisioner is not None and self._provisioner.worker_python() is not None

    def worker_python(self) -> Path | None:
        if self._provisioner is None:
            return None
        return self._provisioner.worker_python()

    def tool_path(self, name: str) -> Path | None:
        """The managed executable `name` (one of `TOOLS`), or None when absent."""

        if name not in TOOLS:
            raise UnknownComponent(f"no tool {name!r}; known: {', '.join(TOOLS)}")
        if self._provisioner is None:
            return None
        return self._provisioner.tool_path(name)

    def component_ready(self, component: str) -> bool:
        if component == ASR_RUNTIME:
            return self.asr_runtime_available()
        if component == MEDIA:
            return self.tool_path("ffmpeg") is not None and self.tool_path("ffprobe") is not None
        if component in TOOLS:
            return self.tool_path(component) is not None
        resources = {item.get("id"): item for item in self.status().get("resources") or ()}
        if self._provisioner is not None and component not in resources:
            raise UnknownComponent(f"no runtime component {component!r}")
        item = resources.get(component)
        return item is not None and item.get("state") == "ready"

    def model_ready(self, model_id: str) -> bool:
        status = self.status()
        models = [*(status.get("models") or ()), *(status.get("optional_models") or ())]
        by_id = {item.get("id"): item for item in models}
        if self._provisioner is not None and model_id not in by_id:
            raise UnknownComponent(f"no managed model {model_id!r}")
        item = by_id.get(model_id)
        return item is not None and item.get("state") == "ready"

    def model_dir(self, model_id: str) -> Path | None:
        """The installed files of model `model_id` (an id of Nonoka's model manifest), or None.

        Today the pinned snapshot in the managed Hugging Face cache
        (`<models>/huggingface/hub/models--<repo>/snapshots/<revision>`), and
        only when every file the manifest lists is there at its listed size
        (stats only: the installer verified the digests). Nothing is ever
        downloaded here; a caller without the model goes without it. WP-E3
        answers it from the model layer instead.
        """

        from nonoka_x.runtime import manifests

        entry = manifests.model_manifest().get("models", {}).get(model_id)
        if entry is None:
            raise UnknownComponent(f"no model {model_id!r} in the model manifest")
        if self._provisioner is None:
            return None
        repo, revision = entry.get("repo"), entry.get("revision")
        if not repo or not revision:
            return None
        models = Path(self._provisioner.paths.models)
        snapshot = models / "huggingface" / "hub" / ("models--" + repo.replace("/", "--")) / "snapshots" / revision
        for item in entry.get("files") or ():
            path = snapshot / item["name"]
            try:
                if not path.is_file() or ("size" in item and path.stat().st_size != int(item["size"])):
                    return None
            except OSError:
                return None
        return snapshot

    def worker_environment(self) -> dict[str, str]:
        """The environment a worker process is spawned into.

        Today the provisioner's (`provision.worker_environment`): the parent's
        environment plus the managed model, cache and tool locations and the
        proxy. WP-R5 and WP-H replace it with one built from nothing.
        """

        if self._provisioner is None:
            import os

            return os.environ.copy()
        return self._provisioner.worker_environment()


__all__ = ["ASR_RUNTIME", "MEDIA", "TOOLS", "RuntimeManager", "UnknownComponent"]
