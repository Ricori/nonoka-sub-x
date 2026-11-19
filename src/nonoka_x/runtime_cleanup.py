"""Which layers an engine needs, and what the settings page's "clean up unused" removes (WP-UI).

Plan 4.9 item 39: switching engines keeps what is installed. "Clean up unused" deletes the layer
versions nothing points at any more; the layers neither the current transcription engine nor the
correction package needs go only when the user ticks them (2026-11-19). A layer an unfinished task
may still use is never deleted.

An engine names what it needs as runtime components and models (`EngineEnvironment`, plan 4.2
item 15); here they become the three layers (`runtime.layer_manifest.LAYERS`). The tool layer is
always kept: the core's own media tier probes with its ffmpeg and ffprobe, whatever engine is current
(appendix B question 94).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from nonoka_x.runtime import ASR_RUNTIME, LAYERS, MEDIA, TOOLS
from nonoka_x.runtime_install import INSTALL_ORDER

#: Kept whatever engine is current: the core's media tier needs the tool layer's ffmpeg / ffprobe.
CORE_LAYERS = ("tools",)
#: The other managed tools; each lives in the tool layer like ffmpeg (plan 4.2 item 14).
_OTHER_TOOLS = ("yt-dlp", "aria2c", "node", "pot-provider")

OLD_VERSION = "old-version"
UNNEEDED = "unneeded"

TASKS_RUNNING = "有未结束的任务"
LAYER_IN_USE = "有未结束的任务用到这一层"


def ordered(layers: Iterable[str]) -> list[str]:
    wanted = set(layers)
    return [layer for layer in INSTALL_ORDER if layer in wanted]


def component_layers(component: str) -> tuple[str, ...]:
    """The layers one runtime component lives in; an unknown one counts as all of them."""

    if component == ASR_RUNTIME:
        return ("python",)
    if component == MEDIA or component in TOOLS or component in _OTHER_TOOLS:
        return ("tools",)
    if component in LAYERS:
        return (component,)
    # Nothing it might need is ever offered for removal.
    return tuple(LAYERS)


def environment_layers(environment: Any) -> dict[str, list[str]]:
    """`{"required": [...], "optional": [...]}`: the layers an `EngineEnvironment` (or its dict) names.

    `optional` holds only the layers that are not required as well.
    """

    def field(name: str) -> tuple[str, ...]:
        value = environment.get(name) if isinstance(environment, Mapping) else getattr(environment, name, ())
        return tuple(str(item) for item in value or ())

    required: set[str] = set()
    for component in field("components"):
        required.update(component_layers(component))
    if field("models"):
        required.add("models")
    optional: set[str] = set()
    for component in field("optional_components"):
        optional.update(component_layers(component))
    if field("optional_models"):
        optional.add("models")
    return {"required": ordered(required), "optional": ordered(optional - required)}


def spec_layers(spec: Any) -> set[str]:
    """Every layer an engine spec may use, required or optional."""

    layers = environment_layers(getattr(spec, "environment", None) or {})
    return {*layers["required"], *layers["optional"]}


def needed_layers(engine_layers: Iterable[str] | None, correction_layers: Iterable[str]) -> set[str] | None:
    """The layers kept for the current engine: its own, the correction's and the core's.

    None when the current engine is unknown (removed, or not registered): then no whole layer is offered.
    """

    if engine_layers is None:
        return None
    return {*engine_layers, *correction_layers, *CORE_LAYERS}


def _row(item: dict[str, Any], reason: str) -> dict[str, Any]:
    return {key: item[key] for key in ("layer", "name", "path", "bytes")} | {"reason": reason}


def plan(manager: Any, *, needed: set[str] | None, busy: set[str], tasks_running: bool) -> dict[str, Any]:
    """What "clean up unused" can delete now, item by item, and what it frees.

    `manager` is the `LayerManager`. `needed` the layers the current engine keeps (None: no layer is
    offered as a whole); `busy` the layers an unfinished task may use; `tasks_running` whether any
    task is unfinished (an old version is only removed when none is, as `LayerManager.remove_unused`).
    Each item is `{layer, name, path, bytes, reason, blocked}`; `blocked` says why it stays.

    Two kinds, deleted differently (the user, 2026-11-19): the old versions nothing uses
    (`old-version`) go by default; a layer the current engine does not need (`unneeded`: its
    current and wanted versions) goes only when the user ticks it, since switching back means
    downloading it again. `bytes` is what the default frees, `layer_bytes` what the layers add.
    """

    items: list[dict[str, Any]] = []
    old = manager.unused()
    old_paths = {str(item.path) for item in old}
    if needed is not None:
        for layer in ordered(set(manager.index.layers) - needed):
            blocked = LAYER_IN_USE if layer in busy else ""
            items.extend(
                item.to_dict() | {"reason": UNNEEDED, "blocked": blocked}
                for item in manager.versions(layer) if str(item.path) not in old_paths
            )
    for item in old:
        items.append(item.to_dict() | {"reason": OLD_VERSION, "blocked": TASKS_RUNNING if tasks_running else ""})

    def total(reason: str, blocked: bool) -> int:
        return sum(item["bytes"] for item in items if item["reason"] == reason and bool(item["blocked"]) == blocked)

    return {
        "items": items,
        "bytes": total(OLD_VERSION, False),
        "layer_bytes": total(UNNEEDED, False),
        "blocked_bytes": total(OLD_VERSION, True) + total(UNNEEDED, True),
    }


class UnknownLayers(ValueError):
    """The user ticked a layer that is not offered as a whole now."""


def execute(manager: Any, *, needed: set[str] | None, busy: set[str], tasks_running: bool,
            layers: Iterable[str] = ()) -> dict[str, Any]:
    """Delete the old versions `plan` offers unblocked, and the whole `layers` the user ticked.

    `{removed, kept, freed}` as `LayerManager.remove_unused` reports. The plan is worked out again
    here, not taken from the page: a task may have started since. A ticked layer that is no longer
    offered (the engine changed, it is not installed) raises `UnknownLayers`; one an unfinished task
    may use now stays and is reported in `kept`.
    """

    current = plan(manager, needed=needed, busy=busy, tasks_running=tasks_running)
    chosen = set(layers)
    offered = {item["layer"] for item in current["items"] if item["reason"] == UNNEEDED}
    if chosen - offered:
        raise UnknownLayers(
            "这些层现在不能整层删除（当前引擎要用它，或没有安装）：" + "、".join(ordered(chosen - offered))
        )
    removed: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    freed = 0
    whole: set[str] = set()
    for layer in ordered(chosen):
        rows = [item for item in current["items"] if item["layer"] == layer and item["reason"] == UNNEEDED]
        if any(item["blocked"] for item in rows):
            kept.extend(_row(item, item["blocked"]) for item in rows)
            continue
        try:
            result = manager.remove_layer(layer)
        except Exception as error:  # noqa: BLE001 - one layer that will not go does not stop the rest
            kept.extend(_row(item, str(error)) for item in rows)
            continue
        whole.add(layer)
        removed.extend(result["removed"])
        kept.extend(result["kept"])
        freed += result["freed"]
    for item in current["items"]:
        if item["reason"] == OLD_VERSION and item["blocked"]:
            kept.append(_row(item, item["blocked"]))
    old_layers = ordered({
        item["layer"] for item in current["items"]
        if item["reason"] == OLD_VERSION and not item["blocked"] and item["layer"] not in whole
    })
    for layer in old_layers:
        result = manager.remove_unused(layer)
        removed.extend(result["removed"])
        kept.extend(result["kept"])
        freed += result["freed"]
    return {"removed": removed, "kept": kept, "freed": freed}


__all__ = [
    "CORE_LAYERS",
    "LAYER_IN_USE",
    "OLD_VERSION",
    "TASKS_RUNNING",
    "UnknownLayers",
    "UNNEEDED",
    "component_layers",
    "environment_layers",
    "execute",
    "needed_layers",
    "ordered",
    "plan",
    "spec_layers",
]
