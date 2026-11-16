"""The desktop's runtime page over the prebuilt layers: status, install jobs, repair and removal (WP-E3).

The managed environment is three layers (plan 4.2 items 14, 19) -- the Python environment, the
models, the tools -- installed by `nonoka_x.runtime.layers.LayerManager` under the big-file root
(`runtime\\layers\\<layer>\\<version>\\`, 4.4 item 24). No venv is built, uv is not run and nothing is
fetched from Hugging Face: each layer is one archive, from the offline package or downloaded through
Nonoka's proxy setting, checked against its manifest before `current.json` moves.

The status payload keeps the shape the runtime page reads (`desktop/frontend/src/bridge/runtime.ts`):
`runtime` is the Python layer, `resources` the tools of the tool layer, `models` /
`optional_models` the model layer's models; `layers` and `unused_layers` are new, for WP-UI's
"clean up unused". An install target names what the page offers; each maps to the layers it needs
(`runtime_install.TARGET_LAYERS`), so installing any tool installs the whole tool layer.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import stat
import sys
import threading
from pathlib import Path
from typing import Any

from nonoka_x.download_route import apply_proxy_environment, live_proxy, proxy_label
from nonoka_x.runtime import layer_manifest as lm
from nonoka_x.runtime.paths import NonokaPaths

OPTIONAL_TOOLS = ("yt-dlp", "aria2c", "node", "pot-provider")
#: Part of the tool layer like every other tool; never removable.
REQUIRED_TOOLS = ("tokcount",)

# 按组安装
TOOL_GROUPS = {
    "video-tools": ("yt-dlp", "aria2c", "node", "pot-provider"),
}
#: Models only some runs need: speaker diarization's default backend
#: (`model_install.DIARIZATION_MODEL_IDS`). Listed on their own and never a reason a transcription is
#: blocked; they come with the model layer like the others.
OPTIONAL_MODELS = ("nemotron3", "speaker-embedding")
OPTIONAL_MODELS_TARGET = "optional-models"
REPAIR_TARGET = "repair"
TARGETS = {"media", "runtime", "models", "all", OPTIONAL_MODELS_TARGET, REPAIR_TARGET,
           *OPTIONAL_TOOLS, *REQUIRED_TOOLS, *TOOL_GROUPS}
PIPELINE_MODELS = ("separator", "whisper", "qwen-referee")

# The two runtime buttons differ only in how far they go, so each target says so in its own words.
START_MESSAGES = {
    "media": "正在准备下载工具层（FFmpeg 等）",
    "runtime": "正在准备安装运行时（不含模型）",
    "models": "正在准备下载模型层",
    "all": "正在准备安装运行时并下载模型",
    OPTIONAL_MODELS_TARGET: "正在准备下载模型层（含说话人分离模型）",
    REPAIR_TARGET: "正在准备完整核对运行环境",
    "yt-dlp": "正在准备安装工具层（含 yt-dlp）",
    "tokcount": "正在准备安装工具层（含 tokcount）",
    "aria2c": "正在准备安装工具层（含 aria2c）",
    "node": "正在准备安装工具层（含 Node.js）",
    "pot-provider": "正在准备安装工具层（含 PO Token 生成器）",
    "video-tools": "正在准备安装工具层（含视频下载工具）",
}
DONE_MESSAGES = {
    "media": "工具层已就绪（FFmpeg 与 FFprobe 可用）",
    "runtime": "运行时已就绪；模型仍需单独下载",
    "models": "模型层已下载并核对完成",
    "all": "运行时与模型已全部准备就绪",
    OPTIONAL_MODELS_TARGET: "模型层已下载并核对完成（含说话人分离模型）",
    REPAIR_TARGET: "运行环境已完整核对，有问题的层已重新安装",
    "yt-dlp": "工具层已安装并核对完成（含 yt-dlp）",
    "tokcount": "工具层已安装并核对完成（含 tokcount）",
    "aria2c": "工具层已安装并核对完成（含 aria2c）",
    "node": "工具层已安装并核对完成（含 Node.js）",
    "pot-provider": "工具层已安装并核对完成（含 PO Token 生成器）",
    "video-tools": "工具层已安装并核对完成（含视频下载工具）",
}
CANCELLED_MESSAGE = "已取消；已下载的部分会保留，下次继续时不必重头再来"

#: The children of the big-file root's `cache\\` that "clear the cache" empties: archives being
#: fetched, and the engines' compile caches (`_cache_entry_names`; rebuilt by the next run).
RUNTIME_CACHE_ENTRIES = ("downloads",)


def _cache_entry_names() -> tuple[str, ...]:
    from nonoka_x import finesub_bridge

    return (*RUNTIME_CACHE_ENTRIES, *finesub_bridge.COMPILE_CACHES)

LAYER_DETAILS = {
    "missing": "尚未安装{label}（下载约 {size}）",
    "outdated": "这个版本需要新的{label}，更新时整层重新下载（约 {size}）",
    "damaged": "{label}与清单不符，需要修复：{problem}",
}


class RuntimeProvisionError(RuntimeError):
    pass


class RuntimeProvisionCancelled(RuntimeError):
    """Raised at our own checkpoints once the user has asked the job to stop."""


def _size_label(size: int) -> str:
    if size >= 2**30:
        return f"{size / 2**30:.1f} GiB"
    return f"{max(1, round(size / 2**20))} MiB"


def _remove_managed_tree(target: Path) -> None:
    """Delete one managed directory, clearing read-only files as they surface.

    `runtime.fsops.remove_tree` stays the only descent, because it refuses to follow a junction out
    of the store; only the exact file it names is made writable before retrying. A file that is
    genuinely in use blocks twice in a row and is reported.
    """

    from nonoka_x.runtime.fsops import is_directory_link, remove_tree

    if os.path.lexists(target) and not target.is_dir() and not is_directory_link(target):
        try:
            target.unlink()
        except PermissionError:
            os.chmod(target, os.stat(target).st_mode | stat.S_IWRITE)
            target.unlink()
        return
    unblocked: set[str] = set()
    while True:
        try:
            remove_tree(target)
            return
        except PermissionError as error:
            blocked = error.filename
            if not blocked or blocked in unblocked:
                raise
            unblocked.add(blocked)
            os.chmod(blocked, os.stat(blocked).st_mode | stat.S_IWRITE)


def _measure_cache_tree(path: Path, usage: dict[str, Any], *, top: bool = False) -> None:
    """Add path's files to usage without following a link out of the tree.

    Only the top-level entry may be a link, the same one clear_cache() empties through.
    """

    from nonoka_x.runtime.fsops import is_directory_link

    if not top and is_directory_link(path):
        return
    try:
        children = list(os.scandir(path))
    except OSError:
        return
    for child in children:
        child_path = Path(child.path)
        try:
            if child.is_dir(follow_symlinks=False) and not is_directory_link(child_path):
                _measure_cache_tree(child_path, usage)
                continue
            if not child.is_file(follow_symlinks=False):
                continue
            info = os.lstat(child.path)
        except OSError:
            continue
        usage["bytes"] += info.st_size
        usage["files"] += 1
        if info.st_nlink <= 1:
            usage["reclaimable"] += info.st_size


class RuntimeProvisioner:
    """The runtime page's view of one installation's layers, and its one install job at a time.

    `install_dir` is the big-file root (`runtime`, 4.4 item 24) when the desktop passes it; otherwise
    `storage.json`'s record, else `<data_dir>\\runtime` (`NonokaPaths.for_roots`). `vendor` is kept for
    the callers that pass it; nothing here reads FineSub's tree any more.
    """

    def __init__(
        self,
        data_dir: str | Path,
        vendor: str | Path,
        install_dir: str | Path | None = None,
        *,
        index_path: str | Path | None = None,
        layer_options: dict[str, Any] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.vendor = Path(vendor).expanduser().resolve()
        self.paths = NonokaPaths.for_roots(self.data_dir, install_dir)
        self.install_root = self.paths.runtime
        self.platform = self._platform_id()
        self._runtime_supported = self.platform == lm.PLATFORM
        # The tool layer is built for Windows only so far (plan appendix B, question 31).
        self._media_supported = self.platform == lm.PLATFORM
        self._bootstrap_error = ""
        self.layers = None
        if self._runtime_supported:
            try:
                from nonoka_x.runtime.layers import LayerManager, load_index

                index = load_index(Path(index_path) if index_path is not None else None)
                self.layers = LayerManager(self.paths, index, **(layer_options or {}))
            except Exception as exc:  # noqa: BLE001 - shown on the page instead of failing the sidecar
                self._bootstrap_error = f"{type(exc).__name__}: {exc}"
        self._lock = threading.RLock()
        # Set while clear_cache() is deleting, so an install cannot start and download into the
        # directories that are being emptied under it.
        self._clearing_cache = False
        self._cancel = threading.Event()
        self._job: dict[str, Any] = {
            "state": "idle",
            "target": "",
            "resource": "",
            "stage": "",
            "message": "",
            "progress": None,
            "error": None,
            "proxy": "",
        }

    @staticmethod
    def _platform_id() -> str:
        if sys.platform == "win32":
            return "windows-x64"
        if sys.platform == "darwin":
            machine = platform.machine().lower()
            if machine in {"arm64", "aarch64"}:
                return "macos-arm64"
            if machine in {"x86_64", "amd64"}:
                return "macos-amd64"
        return sys.platform

    # -- status ------------------------------------------------------------------------------------------------

    def _job_copy(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._job))

    def status(self) -> dict[str, Any]:
        job = self._job_copy()
        base = {
            "schema": 1,
            "platform": self.platform,
            "supported": self._runtime_supported and self.layers is not None,
            "runtime_supported": self._runtime_supported and self.layers is not None,
            "media_supported": self._media_supported and self.layers is not None,
            "bootstrap_error": self._bootstrap_error,
            "root": str(self.paths.runtime),
        }
        if self.layers is None:
            detail = (f"运行环境层不可用：{self._bootstrap_error}" if self._bootstrap_error
                      else "本地 GPU 运行时当前仅支持 Windows x64/NVIDIA")
            return {
                **base,
                "media_ready": False,
                "runtime": {"id": "python", "version": "3.12", "state": "missing", "detail": detail},
                "resources": [],
                "models": [{"id": model_id, "state": "missing"} for model_id in PIPELINE_MODELS],
                "optional_models": [{"id": model_id, "state": "missing"} for model_id in OPTIONAL_MODELS],
                "layers": [],
                "unused_layers": [],
                "unused_bytes": 0,
                "job": job,
            }
        overview = self.layers.overview()
        by_layer = {item["id"]: item for item in overview["layers"]}
        runtime_item = {"id": "python", "version": "3.12", **self._item_state("python", by_layer)}
        models_state = self._item_state("models", by_layer)
        return {
            **base,
            "media_ready": self.tool_path("ffmpeg") is not None and self.tool_path("ffprobe") is not None,
            "runtime": runtime_item,
            "resources": self._tool_items(by_layer),
            "models": [{"id": model_id, **models_state} for model_id in PIPELINE_MODELS],
            "optional_models": [{"id": model_id, **models_state} for model_id in OPTIONAL_MODELS],
            "layers": overview["layers"],
            "unused_layers": overview["unused"],
            "unused_bytes": overview["unused_bytes"],
            "job": job,
        }

    def _item_state(self, layer: str, by_layer: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """A layer's state in the runtime page's words: ready / missing / outdated / failed (damaged)."""

        item = by_layer.get(layer) or {"state": "missing", "problems": [], "archive_bytes": 0, "label": layer}
        state = item["state"]
        if state == "ready":
            return {"state": "ready"}
        detail = LAYER_DETAILS[state].format(
            label=item.get("label", layer), size=_size_label(int(item.get("archive_bytes") or 0)),
            problem=(item.get("problems") or ["?"])[0],
        )
        return {"state": "failed" if state == "damaged" else state, "detail": detail}

    def _tool_items(self, by_layer: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        from nonoka_x.runtime import manifests

        tools_state = self._item_state("tools", by_layer)
        items = []
        for resource in manifests.tool_manifest().get("resources") or ():
            tool_id = resource["id"]
            if tool_id in lm.TOOLS_LEFT_OUT:
                continue
            item = {"id": tool_id, "version": resource.get("version"), "source": "managed", **tools_state}
            if tool_id == "tokcount" and item["state"] != "ready":
                system_tool = shutil.which(tool_id)
                if system_tool:
                    item.update(state="ready", source="system", detail=f"使用系统安装：{system_tool}")
            items.append(item)
        return items

    # -- the install job -----------------------------------------------------------------------------------------

    def start(self, target: str) -> dict[str, Any]:
        if target not in TARGETS:
            raise RuntimeProvisionError(
                f"target must be one of: media, runtime, models, all, {OPTIONAL_MODELS_TARGET}, {REPAIR_TARGET}, "
                + ", ".join((*OPTIONAL_TOOLS, *REQUIRED_TOOLS, *TOOL_GROUPS))
            )
        if target == "media" and not self._media_supported:
            raise RuntimeProvisionError(f"managed FFmpeg is unavailable on {self.platform}")
        if target != "media" and not self._runtime_supported:
            raise RuntimeProvisionError("FineSub managed GPU runtime currently supports Windows x64 only")
        if self.layers is None:
            raise RuntimeProvisionError(f"运行环境层不可用：{self._bootstrap_error}")
        with self._lock:
            if self._job["state"] == "running":
                raise RuntimeProvisionError("FineSub runtime installation is already running")
            if self._clearing_cache:
                raise RuntimeProvisionError("正在清理下载缓存，请稍后再安装")
            self._cancel.clear()
            self._job = {
                "state": "running",
                "target": target,
                "resource": "",
                "stage": "preparing",
                "message": START_MESSAGES[target],
                "progress": None,
                "error": None,
                # Which local proxy, if any, this job's downloads go through.
                "proxy": proxy_label(live_proxy()),
            }
        thread = threading.Thread(target=self._run, args=(target,), name="nonoka-runtime-provision", daemon=True)
        thread.start()
        return self.status()

    def ensure_required_tools(self) -> None:
        """Kept for the sidecar's startup call; nothing to do with layers.

        It used to fetch tokcount for installs that predated the requirement. tokcount is in the tool
        layer now, which every install of the runtime brings, so there is no separate fetch to start.
        """

    def cancel(self) -> dict[str, Any]:
        """Ask the running job to stop at its next checkpoint.

        A download keeps its `.part` and resumes from it; an unpack is redone; the pointer only moves
        once a layer checks out, so a cancelled install leaves no half-installed layer behind.
        """

        with self._lock:
            if self._job["state"] != "running":
                raise RuntimeProvisionError("当前没有正在进行的安装任务")
            self._cancel.set()
            self._job.update(message="正在取消，等待当前步骤停止…")
        return self.status()

    def _cancelled(self) -> bool:
        return self._cancel.is_set()

    def _stop_if_cancelled(self) -> None:
        if self._cancel.is_set():
            raise RuntimeProvisionCancelled(CANCELLED_MESSAGE)

    def _update(self, **values: Any) -> None:
        with self._lock:
            self._job.update(values)

    def _layer_progress(self, value: Any) -> None:
        progress = None
        if value.total:
            progress = {"completed": min(int(value.completed), int(value.total)), "total": int(value.total),
                        "unit": "bytes", "bytes_per_second": float(value.bytes_per_second)}
        self._update(resource=value.layer, stage=value.stage, message=value.message, progress=progress)

    def _run(self, target: str) -> None:
        from nonoka_x.runtime.layers import LayerBusy, PathTooDeep
        from nonoka_x.runtime_install import layers_for, ordered, vc_runtime_problem

        try:
            assert self.layers is not None
            self._stop_if_cancelled()
            needed = ordered(layers_for(target))
            if target == REPAIR_TARGET:
                for layer in needed:
                    self._stop_if_cancelled()
                    if self.layers.state(layer, fresh=True).state == "missing":
                        continue
                    self._update(resource=layer, stage="checking", message=f"正在完整核对{layer}层", progress=None)
                    self.layers.repair(layer, progress=self._layer_progress, should_pause=self._cancelled)
            else:
                pending = [layer for layer in needed if not self.layers.state(layer, fresh=True).ready]
                problem = vc_runtime_problem(pending)
                if problem:
                    raise RuntimeProvisionError(problem)
                for layer in pending:
                    self._stop_if_cancelled()
                    state = self.layers.install(layer, progress=self._layer_progress, should_pause=self._cancelled)
                    if not state.ready:
                        raise RuntimeProvisionError(f"{layer} 层安装完成后仍不可用：{'；'.join(state.problems[:3])}")
            self._update(state="completed", stage="completed", message=DONE_MESSAGES[target], progress=None)
        except Exception as exc:
            # Whatever surfaces once the user has asked to stop is that request arriving -- our own
            # checkpoints or the download's `DownloadPaused` -- so it is a cancellation, not a failure.
            if self._cancel.is_set():
                self._update(state="cancelled", stage="cancelled", message=CANCELLED_MESSAGE, resource="",
                             progress=None, error=None)
                return
            code = ("install_path_too_deep" if isinstance(exc, PathTooDeep)
                    else "runtime_in_use" if isinstance(exc, LayerBusy) else "runtime_install_failed")
            self._update(state="failed", stage="failed", message=str(exc), error={"code": code, "message": str(exc)},
                         progress=None)

    # -- what workers use ----------------------------------------------------------------------------------------

    def worker_python(self) -> Path | None:
        return self.layers.python_executable() if self.layers is not None else None

    def tool_path(self, name: str) -> Path | None:
        if self.layers is None or name not in {"ffmpeg", "ffprobe", "tokcount"}:
            return None
        return self.layers.tool_file(name)

    def model_root(self) -> Path | None:
        """The model layer's current directory, or None when not ready."""

        return self.layers.root("models") if self.layers is not None else None

    def engine_model_dir(self) -> Path:
        """The model directory an engine is pointed at (its launch context's `runtime.models`).

        The wanted model layer version even before it is installed (the
        readiness checks keep a run from starting without it), and somewhere
        under `layers\\models\\` when this build has no layers.json.
        """

        if self.layers is not None:
            return self.model_root() or self.layers.wanted_dir("models")
        return self.paths.layer("models") / "unavailable"

    def worker_environment(self) -> dict[str, str]:
        """The parent's environment plus the tool layer and the proxy.

        What an engine reads of its own (the model layer, its keys and files)
        is added by its bridge from the launch context (`nonoka_x.finesub_bridge`).
        """

        environment = os.environ.copy()
        path_dirs: list[str] = []
        for name in ("ffmpeg", "ffprobe", "tokcount"):
            executable = self.tool_path(name)
            if executable is not None and str(executable.parent) not in path_dirs:
                path_dirs.append(str(executable.parent))
        if path_dirs:
            environment["PATH"] = os.pathsep.join([*path_dirs, environment.get("PATH", "")])
        yt_dlp = self.layers.tool_dir("yt-dlp") if self.layers is not None else None
        if yt_dlp is not None:
            environment["PYTHONPATH"] = os.pathsep.join(
                part for part in (str(yt_dlp), environment.get("PYTHONPATH", "")) if part
            )
        apply_proxy_environment(environment)
        return environment

    # -- removal, cleaning, caches -------------------------------------------------------------------------------

    def _refuse_while_busy(self, words: str) -> None:
        with self._lock:
            if self._job["state"] == "running":
                raise RuntimeProvisionError(f"安装任务运行中，无法{words}；请先取消安装")
        if self.layers is not None and self.layers._in_use():
            raise RuntimeProvisionError(f"有未结束的任务，无法{words}；请等任务结束")

    def remove_all(self) -> dict[str, Any]:
        """Remove every layer and the download cache, never tasks or user data."""

        if self.layers is None:
            raise RuntimeProvisionError(f"运行环境层不可用：{self._bootstrap_error}")
        self._refuse_while_busy("删除环境")
        failures = self.layers.remove_all()
        for entry in self._cache_entries():
            if os.path.lexists(entry):
                for child in list(entry.iterdir()):
                    try:
                        _remove_managed_tree(child)
                    except OSError as error:
                        failures.append(f"{entry.name}/{child.name}（{error.strerror or error}）")
        if failures:
            detail = "以下目录未能完全删除，可能有文件正在被占用：" + "、".join(failures)
            self._update(state="failed", target="remove-all", resource="", stage="failed", message=detail,
                         progress=None, error={"code": "remove_failed", "message": detail})
            raise RuntimeProvisionError(detail)
        self._update(state="completed", target="remove-all", resource="", stage="completed",
                     message="运行时、模型、工具与下载缓存已全部删除；任务、字幕和设置已保留",
                     progress=None, error=None)
        return self.status()

    def unused_layers(self) -> dict[str, Any]:
        """For WP-UI's "clean up unused": the versions and leftovers no one uses, with their sizes."""

        if self.layers is None:
            return {"schema": 1, "unused": [], "bytes": 0}
        unused = [item.to_dict() for item in self.layers.unused()]
        return {"schema": 1, "unused": unused, "bytes": sum(item["bytes"] for item in unused)}

    def remove_unused_layers(self) -> dict[str, Any]:
        if self.layers is None:
            raise RuntimeProvisionError(f"运行环境层不可用：{self._bootstrap_error}")
        with self._lock:
            if self._job["state"] == "running":
                raise RuntimeProvisionError("安装任务运行中，无法清理；请等待安装完成或先取消安装")
        return {"schema": 1, **self.layers.remove_unused()}

    def cache_usage(self) -> dict[str, Any]:
        """Measure the downloads clear_cache() would empty (`reclaimable` leaves out hard-linked files)."""

        usage = {"schema": 1, "bytes": 0, "reclaimable": 0, "files": 0}
        for entry in self._cache_entries():
            _measure_cache_tree(entry, usage, top=True)
        return usage

    def clear_cache(self) -> dict[str, Any]:
        """Empty the download cache (partly downloaded archives) and report what was freed."""

        with self._lock:
            if self._job["state"] == "running":
                raise RuntimeProvisionError("安装任务运行中，无法清理缓存；请等待安装完成或先取消安装")
            if self._clearing_cache:
                raise RuntimeProvisionError("正在清理下载缓存")
            self._clearing_cache = True
        try:
            before = self.cache_usage()
            failures: list[str] = []
            for entry in self._cache_entries():
                if not os.path.lexists(entry):
                    continue
                # Empty the entry rather than remove it: it may be a link the user made to keep the
                # cache on another drive.
                for child in entry.iterdir():
                    try:
                        _remove_managed_tree(child)
                    except OSError as error:
                        failures.append(f"{entry.name}/{child.name}（{error.strerror or error}）")
            after = self.cache_usage()
        finally:
            with self._lock:
                self._clearing_cache = False
        return {
            **after,
            "freed": max(0, before["reclaimable"] - after["reclaimable"]),
            "removed": max(0, before["bytes"] - after["bytes"]),
            "failures": failures,
        }

    def _cache_entries(self) -> list[Path]:
        cache = self.paths.cache
        allowed_roots = (self.paths.runtime.resolve(), self.paths.data.resolve())
        entries: list[Path] = []
        for name in _cache_entry_names():
            entry = cache / name
            # Clearing empties the entry's children, so an entry that resolves to the big-file root or
            # above it would take the layers with it.
            resolved = entry.resolve()
            if any(resolved == root or resolved in root.parents for root in allowed_roots):
                raise RuntimeProvisionError(f"拒绝清理托管目录本身：{resolved}")
            entries.append(entry)
        return entries


__all__ = [
    "CANCELLED_MESSAGE",
    "DONE_MESSAGES",
    "OPTIONAL_MODELS",
    "OPTIONAL_MODELS_TARGET",
    "OPTIONAL_TOOLS",
    "PIPELINE_MODELS",
    "REPAIR_TARGET",
    "REQUIRED_TOOLS",
    "START_MESSAGES",
    "TARGETS",
    "TOOL_GROUPS",
    "RuntimeProvisionCancelled",
    "RuntimeProvisionError",
    "RuntimeProvisioner",
]
