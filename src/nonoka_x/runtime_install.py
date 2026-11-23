"""Installing the prebuilt layers for the runtime page and for the migration (WP-E3).

There is no venv, no uv and no package download any more (plan 4.2 items 14, 19): the Python
environment, the models and the tools each come as one layer, installed by
`nonoka_x.runtime.layers.LayerManager`. What is left here is what the runtime page's install targets
mean in layers, the one check that has to happen before the Python layer is downloaded (the system's
VC++ runtime, `msvc_runtime`, worded as before), and migration's `layer-download` step.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
import sys
from typing import Any

from nonoka_x.msvc_runtime import msvc_runtime_problem
from nonoka_x.runtime import layer_manifest as lm

#: The order layers are installed in when a target needs several: the tools are small and serve the
#: cloud engines as well, so they come first; the models last.
INSTALL_ORDER = ("tools", "python", "models", "models-moss", "screen-text")

#: What each install target of the runtime page needs, in layers. Every tool is in the one tool layer
#: (4.2 item 14), so installing any tool installs them all.
TARGET_LAYERS: dict[str, tuple[str, ...]] = {
    "media": ("tools",),
    "runtime": ("tools", "python"),
    "models": ("models",),
    "models-moss": ("models-moss",),
    "screen-text": ("screen-text",),
    "optional-models": ("models",),
    "all": ("tools", "python", "models"),
    "repair": INSTALL_ORDER,
    "yt-dlp": ("tools",),
    "tokcount": ("tools",),
    "aria2c": ("tools",),
    "node": ("tools",),
    "pot-provider": ("tools",),
    "video-tools": ("tools",),
}


def layers_for(target: str) -> tuple[str, ...]:
    return TARGET_LAYERS[target]


def ordered(layers: Iterable[str]) -> tuple[str, ...]:
    wanted = set(layers)
    return tuple(layer for layer in INSTALL_ORDER if layer in wanted)


def vc_runtime_problem(layers: Iterable[str], *, platform: str | None = None,
                       problem: Callable[..., str | None] | None = None) -> str | None:
    """Why the Python layer would not load here, checked before it is downloaded; None when it would.

    torch's `c10.dll` needs the system's VC++ 2015-2022 runtime, which neither the wheels nor the
    interpreter carry (plan 4.5 item 31). The words are `msvc_runtime`'s, unchanged.
    """

    if not set(layers) & {"python", "screen-text"}:
        return None
    return (problem or msvc_runtime_problem)(platform=platform or sys.platform)


class LayerDownloadHook:
    """Migration's `layer-download` step (`migration.hooks.LAYER_DOWNLOAD`): the Python and tool layers.

    The old environment is already gone when it runs (plan 4.4 item 25, "先删后下"); a failure can be
    retried and the step re-run, and a layer already installed is not downloaded again.
    """

    layers = ("tools", "python")

    def __init__(self, open_manager: Callable[[Any], Any] | None = None) -> None:
        self._open = open_manager

    def _manager(self, context: Any) -> Any:
        if self._open is not None:
            return self._open(context)
        if not lm.platform_supported():
            from nonoka_x.runtime.manifest_tools import open_tools_manager

            return open_tools_manager(context.paths)
        from nonoka_x.runtime.layers import LayerManager, load_index

        return LayerManager(context.paths, load_index())

    def download_bytes(self, context: Any) -> int | None:
        try:
            from nonoka_x.runtime.layers import load_index

            index = load_index()
        except Exception:  # noqa: BLE001 - the plan only asks; None means "cannot say yet"
            return None
        index_platform = getattr(index, "platform", None)
        if index_platform is not None and index_platform != lm.current_platform():
            from nonoka_x.runtime import manifests

            tool_manifest = manifests.tool_manifest_for(lm.current_platform())
            if tool_manifest is None:
                return 0
            return sum(
                int((item.get("asset") or {}).get("size") or 0)
                for item in tool_manifest.get("resources") or ()
                if item.get("id") not in lm.TOOLS_LEFT_OUT
            )
        return sum(index.entry(layer).archive.size for layer in self.layers if layer in index.layers)

    def run(self, context: Any) -> None:
        target_layers: tuple[str, ...] = self.layers
        if self._open is None and not lm.platform_supported():
            manager = self._manager(context)
            if manager is None:
                context.log(
                    f"layer-download: skipped on {lm.current_platform()} (prebuilt layers are {lm.PLATFORM} only)"
                )
                return
            target_layers = ("tools",)
            context.log(f"layer-download: python skipped on {lm.current_platform()} ({lm.PLATFORM} only)")
        else:
            problem = vc_runtime_problem(target_layers)
            if problem:
                raise RuntimeError(problem)
            manager = self._manager(context)
        progress = getattr(context, "progress", None)
        for layer in target_layers:
            context.log(f"layer-download: {layer}")
            state = manager.install(layer, **({"progress": progress} if progress is not None else {}))
            context.log(f"layer-download: {layer} {state.state} ({state.wanted})")


__all__ = ["INSTALL_ORDER", "TARGET_LAYERS", "LayerDownloadHook", "layers_for", "ordered", "vc_runtime_problem"]
