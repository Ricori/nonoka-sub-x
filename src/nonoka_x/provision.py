"""FineSub-native managed runtime and model provisioning for the desktop sidecar."""

from __future__ import annotations

import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from nonoka_x.download_route import (
    DownloadTooSlow,
    SpeedWatch,
    apply_proxy_environment,
    climb_huggingface,
    live_proxy,
    proxy_label,
    speed_watched,
)

#: Where the installer's own two assets sit inside the FineSub snapshot. They
#: used to live under `resources/` and `runtime/`; upstream 0.5.0 made them
#: package data, resolved by `finesub_bootstrap.resources` and `.environment`
#: with `Path(__file__).with_name(...)`, so the snapshot no longer carries a
#: copy anywhere else -- and a copy elsewhere would be the one nobody reads.
VENDOR_BOOTSTRAP = Path("src") / "finesub_bootstrap"

OPTIONAL_TOOLS = ("yt-dlp", "aria2c", "node", "pot-provider")
#: Installed with the runtime, and fetched at startup by installations that
#: predate the requirement. Never removable.
REQUIRED_TOOLS = ("tokcount",)

# 按组安装
TOOL_GROUPS = {
    "video-tools": ("yt-dlp", "aria2c", "node", "pot-provider"),
}
REMOVABLE_TOOL_GROUPS = frozenset(TOOL_GROUPS)
#: Models only some runs need: speaker diarization's default backend
#: (`model_install.DIARIZATION_MODEL_IDS`). Listed and installable on their own,
#: never part of `models` / `all`, and never a reason a transcription is blocked.
OPTIONAL_MODELS = ("nemotron3", "speaker-embedding")
OPTIONAL_MODELS_TARGET = "optional-models"
TARGETS = {"media", "runtime", "models", "all", OPTIONAL_MODELS_TARGET, *OPTIONAL_TOOLS, *REQUIRED_TOOLS, *TOOL_GROUPS}
PIPELINE_MODELS = ("separator", "whisper", "qwen-referee")
#: The Windows FFmpeg resource from before the build was pinned.
UNPINNED_FFMPEG_VERSION = "n9.0-latest"
UNPINNED_FFMPEG_DIRECTORY = "ffmpeg-n9.0-latest-win64-gpl-9.0"

#: What an installed runtime whose dependency lock moved on is told: an engine
#: sync changed the pinned packages, so the environment is rebuilt from the new
#: lock -- with unchanged wheels reused from the local cache.
RUNTIME_OUTDATED_DETAIL = "引擎依赖有变化，需要更新 Python 运行环境（只下载有变化的部分）"

# The two runtime buttons differ only in how far they go, so each target says so
# in its own words: a shared "已准备就绪" told the user models were ready after a
# runtime-only install, which is exactly the confusion these messages exist to
# prevent.
START_MESSAGES = {
    "media": "正在准备下载 FFmpeg",
    "runtime": "正在准备安装运行时（不含模型）",
    "models": "正在准备下载缺失的模型",
    "all": "正在准备安装运行时并下载缺失的模型",
    OPTIONAL_MODELS_TARGET: "正在准备下载说话人分离模型",
    "yt-dlp": "正在准备安装可选工具 yt-dlp",
    "tokcount": "正在准备安装 tokcount",
    "aria2c": "正在准备安装可选工具 aria2c",
    "node": "正在准备安装可选工具 Node.js",
    "pot-provider": "正在准备安装 PO Token 生成器",
    "video-tools": "正在准备安装视频下载工具",
}
DONE_MESSAGES = {
    "media": "FFmpeg 与 FFprobe 已准备就绪",
    "runtime": "运行时已就绪；模型仍需单独下载",
    "models": "缺失的模型已全部下载并校验完成",
    "all": "运行时与所需模型已全部准备就绪",
    OPTIONAL_MODELS_TARGET: "说话人分离模型已下载并校验完成",
    "yt-dlp": "可选工具 yt-dlp 已安装并校验完成",
    "tokcount": "tokcount 已安装并校验完成",
    "aria2c": "可选工具 aria2c 已安装并校验完成",
    "node": "可选工具 Node.js 已安装并校验完成",
    "pot-provider": "PO Token 生成器已安装并校验完成",
    "video-tools": "视频下载工具已安装并校验完成",
}
CANCELLED_MESSAGE = "已取消；已下载的部分会保留，下次继续时不必重头再来"


class RuntimeProvisionError(RuntimeError):
    pass


class RuntimeProvisionCancelled(RuntimeError):
    """Raised at our own checkpoints once the user has asked the job to stop."""


def parse_model_install_event(line: str, model_id: str) -> dict[str, Any] | None:
    """Validate one child-process event, leaving ordinary log lines untouched."""

    try:
        value = json.loads(line)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("resource") != model_id:
        return None
    event_type = value.get("type")
    message = value.get("message")
    if not isinstance(message, str):
        return None
    if event_type == "stage":
        stage = value.get("stage")
        if stage not in {"preparing", "downloading", "verifying", "completed"}:
            return None
        return {"type": "stage", "stage": stage, "message": message}
    if event_type != "progress":
        return None
    try:
        completed = max(0, int(value["completed"]))
        total = max(0, int(value["total"]))
        bytes_per_second = max(0.0, float(value.get("bytes_per_second", 0.0)))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if total:
        completed = min(completed, total)
    return {
        "type": "progress",
        "completed": completed,
        "total": total,
        "bytes_per_second": bytes_per_second,
        "message": message,
    }


def _remove_managed_tree(target: Path) -> None:
    """Delete one managed directory, clearing read-only files as they surface.

    uv and pip leave read-only files inside the runtime and model stores, and
    Windows refuses to unlink those (WinError 5). FineSub's ``remove_tree``
    stays the only descent, because it is the one that refuses to follow a
    junction out of the store; only the exact file it names is made writable
    before retrying, so nothing outside the tree is ever touched. A file that
    is genuinely in use blocks twice in a row and is reported.
    """

    from finesub_bootstrap.fsops import remove_tree

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
            # Keep the other mode bits: on POSIX a bare S_IWRITE would leave a
            # surviving file unreadable if the removal fails for another reason.
            os.chmod(blocked, os.stat(blocked).st_mode | stat.S_IWRITE)


#: The children of the install root's `cache/` that are download caches: uv's
#: package cache, the runtime wheels the installer fetched itself, and the
#: resource archives. The list names what goes rather than what stays because
#: `cache/state` sits beside them and is FineSub's rate-limit record.
RUNTIME_CACHE_ENTRIES = ("uv", "runtime-wheels", "downloads")


def _measure_cache_tree(path: Path, usage: dict[str, Any], *, top: bool = False) -> None:
    """Add path's files to usage without following a link out of the tree.

    Only the top-level entry may be a link, the same one clear_cache() empties
    through. `os.lstat` rather than `DirEntry.stat`: on Windows the latter
    always reports `st_nlink` as zero, and the link count is the whole point.
    """

    from finesub_bootstrap.fsops import is_directory_link

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
    """Own one asynchronous FineSub provisioning operation.

    FineSub's bootstrap remains the authority for downloads, hashes, safe
    extraction, mirrors and the Python lock. Nonoka X only exposes its state to
    Wails and selects the resulting interpreter for workers.
    """

    def __init__(
        self,
        data_dir: str | Path,
        vendor: str | Path,
        install_dir: str | Path | None = None,
    ) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        # The install root carries everything large -- the Python runtime, the
        # models and the download caches -- so the desktop shell can put it on
        # another drive while the small state files stay beside the data dir.
        # Defaulting keeps every existing installation exactly where it is.
        self.install_root = (
            Path(install_dir).expanduser().resolve()
            if install_dir
            else self.data_dir / "finesub"
        )
        self.vendor = Path(vendor).expanduser().resolve()
        self.platform = self._platform_id()
        self._runtime_supported = sys.platform == "win32"
        self._media_supported = self.platform in {"windows-x64", "macos-amd64", "macos-arm64"}
        install_root = self.install_root
        self._bootstrap_error = ""
        self.runtime = None
        try:
            from finesub_bootstrap.paths import AppPaths
            from finesub_bootstrap.resources import ResourceManager
            from finesub_bootstrap.shell import resource_specs

            from nonoka_x.asset_digest import install_cloud_digest_source
            from nonoka_x.download_route import install_proxy_route_policy

            install_proxy_route_policy()
            install_cloud_digest_source()
            self.paths = AppPaths.for_root(
                install_root,
                data_root=self.data_dir / "finesub-data",
                big_data=install_root,
            )
            if self.platform in {"macos-arm64", "macos-amd64"}:
                manifest_path = Path(__file__).resolve().parent / "resources" / self._manifest_name()
            else:
                manifest_path = self.vendor / VENDOR_BOOTSTRAP / self._manifest_name()
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.resources = ResourceManager(self.paths, resource_specs(manifest))
        except Exception as exc:
            self.paths = None
            self.resources = None
            self.runtime = None
            self._bootstrap_error = f"{type(exc).__name__}: {exc}"

        if not self._bootstrap_error and self._runtime_supported:
            from nonoka_x.runtime_install import ProgressRuntimeEnvironment

            def managed_uv() -> Path:
                assert self.resources is not None
                executable = self.resources.active_file("uv", "uv.exe")
                if executable is None:
                    raise FileNotFoundError("FineSub uv bootstrap resource is not installed")
                return executable

            self.runtime = ProgressRuntimeEnvironment(
                paths=self.paths,
                app_source=self.vendor,
                runtime_lock=self.vendor / VENDOR_BOOTSTRAP / "pylock.win-py312.toml",
                uv_executable=managed_uv,
                download_progress=self._progress,
            )
        self._lock = threading.RLock()
        # Set while clear_cache() is deleting, so an install cannot start and
        # download into the directories that are being emptied under it.
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

    def _manifest_name(self) -> str:
        if self.platform == "windows-x64":
            return "runtime-manifest.json"
        if self.platform == "macos-arm64":
            return "runtime-manifest.macos-arm64.json"
        if self.platform == "macos-amd64":
            return "runtime-manifest.macos-amd64.json"
        # Unsupported desktop platforms retain the Windows resource snapshot in
        # diagnostics, matching the historical status shape, but installation
        # remains blocked by the platform capability flags.
        return "runtime-manifest.json"

    def status(self) -> dict[str, Any]:
        if self._bootstrap_error:
            with self._lock:
                job = json.loads(json.dumps(self._job))
            return {
                "schema": 1,
                "platform": self.platform,
                "supported": False,
                "runtime_supported": False,
                "media_supported": False,
                "media_ready": False,
                # The desktop shell disables every install control while the
                # bootstrap is broken, so the reason travels with the payload
                # instead of hiding inside a single asset tooltip.
                "bootstrap_error": self._bootstrap_error,
                "root": str(self.install_root),
                "runtime": {"id": "python", "version": "3.12", "state": "missing", "detail": f"FineSub bootstrap 依赖缺失：{self._bootstrap_error}"},
                "resources": [],
                "models": [{"id": model_id, "state": "missing"} for model_id in PIPELINE_MODELS],
                "optional_models": [{"id": model_id, "state": "missing"} for model_id in OPTIONAL_MODELS],
                "job": job,
            }
        assert self.resources is not None and self.paths is not None
        from nonoka_x.model_install import missing_managed_models

        resources = [item.model_dump(mode="json") for item in self.resources.check_all()]
        for item in resources:
            item["source"] = "managed"
            if item["state"] == "missing" and self._unpinned_ffmpeg(item["id"], "ffmpeg.exe") is not None:
                item.update(state="outdated", installed_version=UNPINNED_FFMPEG_VERSION)
            if item["id"] != "tokcount" or item["state"] == "ready":
                continue
            system_tool = shutil.which(item["id"])
            if system_tool:
                item.update(
                    state="ready",
                    source="system",
                    detail=f"使用系统安装：{system_tool}",
                )
        if self.runtime is not None:
            runtime_status = self.runtime.status().model_dump(mode="json")
            # FineSub names this status after its installer, the same id as the
            # uv resource listed beside it.
            runtime_status["id"] = "python"
            if runtime_status.get("state") == "missing" and self._runtime_outdated():
                # Installed, but for an older dependency lock: say "update",
                # not "missing" -- the environment is there and most of it is
                # reused, which is a very different thing to be told.
                runtime_status.update(state="outdated", detail=RUNTIME_OUTDATED_DETAIL)
        else:
            runtime_status = {
                "id": "python",
                "version": "3.12",
                "state": "missing",
                "detail": "本地 GPU 运行时当前仅支持 Windows x64/NVIDIA",
            }
        missing = list(missing_managed_models(self.paths.models))
        missing_optional = set(missing_managed_models(self.paths.models, OPTIONAL_MODELS))
        with self._lock:
            job = json.loads(json.dumps(self._job))
        return {
            "schema": 1,
            "platform": self.platform,
            "supported": self._runtime_supported,
            "runtime_supported": self._runtime_supported,
            "media_supported": self._media_supported,
            "bootstrap_error": "",
            "media_ready": self.tool_path("ffmpeg") is not None and self.tool_path("ffprobe") is not None,
            "root": str(self.paths.root),
            "runtime": runtime_status,
            "resources": resources,
            "models": [
                {"id": model_id, "state": "missing" if model_id in missing else "ready"}
                for model_id in PIPELINE_MODELS
            ],
            "optional_models": [
                {"id": model_id, "state": "missing" if model_id in missing_optional else "ready"}
                for model_id in OPTIONAL_MODELS
            ],
            "job": job,
        }

    def _runtime_outdated(self) -> bool:
        """Whether the runtime is installed but was built from an older lock.

        `RuntimeEnvironment.status` reports both that and a runtime that was
        never installed as `missing`; upstream's `condition()` tells them
        apart (`absent` / `outdated` / `damaged`). A damaged runtime stays
        `missing`, with the engine's own explanation.
        """

        if self.runtime is None:
            return False
        try:
            return self.runtime.condition() == "outdated"
        except OSError:
            return False

    def start(self, target: str) -> dict[str, Any]:
        if target not in TARGETS:
            raise RuntimeProvisionError(
                f"target must be one of: media, runtime, models, all, {OPTIONAL_MODELS_TARGET}, "
                + ", ".join((*OPTIONAL_TOOLS, *REQUIRED_TOOLS, *TOOL_GROUPS))
            )
        if target == "media" and not self._media_supported:
            raise RuntimeProvisionError(f"managed FFmpeg is unavailable on {self.platform}")
        if target != "media" and not self._runtime_supported:
            raise RuntimeProvisionError("FineSub managed GPU runtime currently supports Windows x64 only")
        if self._bootstrap_error:
            raise RuntimeProvisionError(f"FineSub bootstrap is unavailable: {self._bootstrap_error}")
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
                # Shown beside the progress: which local proxy, if any, this
                # job's downloads go through. Every download in it follows the
                # same route -- see `nonoka_x.download_route`.
                "proxy": proxy_label(live_proxy()),
            }
        thread = threading.Thread(target=self._run, args=(target,), name="nonoka-runtime-provision", daemon=True)
        thread.start()
        return self.status()

    def ensure_required_tools(self) -> None:
        """Start fetching a required tool this installation does not have.

        An install that predates the requirement never runs the job that would
        bring it, and one that only uses the cloud never needs the runtime job
        at all. Best effort: the engine still works without it.
        """

        if self.resources is None or self._bootstrap_error or not self._runtime_supported:
            return
        for resource_id in REQUIRED_TOOLS:
            if resource_id not in self.resources.resources or shutil.which(resource_id):
                continue
            if self.resources.status(resource_id).state != "ready":
                self.start(resource_id)
                return

    def cancel(self) -> dict[str, Any]:
        """Ask the running job to stop at its next checkpoint.

        Cancelling is cooperative rather than abrupt: FineSub's downloader keeps
        the partial file and resumes from it, and an archive is only activated
        once it is complete, so a cancelled install leaves no half-installed
        resource behind and the next attempt continues where this one stopped.
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

    def _stage(self, stage: str, message: str, *, reset_progress: bool = True) -> None:
        values: dict[str, Any] = {"stage": stage, "message": message}
        if reset_progress:
            values["progress"] = None
        self._update(**values)

    def _progress(self, value: Any, *, completed_before: int = 0, total_override: int = 0) -> None:
        item_total = max(0, int(value.total))
        total = max(item_total, int(total_override))
        downloaded = max(0, int(value.downloaded)) + max(0, int(completed_before))
        if total:
            downloaded = min(downloaded, total)
        self._update(progress={"completed": downloaded, "total": total, "unit": "bytes", "bytes_per_second": value.bytes_per_second})

    def _run(self, target: str) -> None:
        try:
            assert self.paths is not None and self.resources is not None
            from nonoka_x.model_install import missing_managed_models
            from finesub_bootstrap.paths import ensure_store

            ensure_store(self.paths)
            self._stop_if_cancelled()
            if target in TOOL_GROUPS:
                self._install_tool_group(target)
            elif target in OPTIONAL_TOOLS or target in REQUIRED_TOOLS:
                self._install_optional_tool(target)
            if target == "media":
                self._install_media_tools()
            if target in {"runtime", "all"}:
                assert self.runtime is not None
                for resource_id in ("uv", "ffmpeg", *REQUIRED_TOOLS):
                    self._stop_if_cancelled()
                    if self.resources.status(resource_id).state != "ready":
                        self._stage("resource", f"正在安装 FineSub 资源：{resource_id}")
                        self.resources.install(resource_id, self._progress, stage=self._stage, should_pause=self._cancelled)
                self._stop_if_cancelled()
                if self.runtime.status().state != "ready":
                    self._stage("runtime", "正在安装 FineSub Python/CUDA 运行时")
                    self.runtime.install(stage=self._stage, log=lambda line: self._update(message=line), should_pause=self._cancelled)
            if target in {"models", "all"}:
                assert self.runtime is not None
                self._stop_if_cancelled()
                if self.runtime.status().state != "ready":
                    raise RuntimeProvisionError("请先安装 FineSub Python 运行时")
                for model_id in missing_managed_models(self.paths.models):
                    self._stop_if_cancelled()
                    self._install_model(model_id)
                remaining = missing_managed_models(self.paths.models)
                if remaining:
                    raise RuntimeProvisionError(
                        "模型安装进程已结束，但完整性复核仍未通过：" + "、".join(remaining)
                    )
            if target == OPTIONAL_MODELS_TARGET:
                assert self.runtime is not None
                self._stop_if_cancelled()
                if self.runtime.status().state != "ready":
                    raise RuntimeProvisionError("请先安装或更新 FineSub Python 运行时")
                for model_id in missing_managed_models(self.paths.models, OPTIONAL_MODELS):
                    self._stop_if_cancelled()
                    self._install_model(model_id)
                remaining = missing_managed_models(self.paths.models, OPTIONAL_MODELS)
                if remaining:
                    raise RuntimeProvisionError(
                        "模型安装进程已结束，但完整性复核仍未通过：" + "、".join(remaining)
                    )
            self._update(state="completed", stage="completed", message=DONE_MESSAGES[target], progress=None)
        except Exception as exc:
            # Whatever surfaces once the user has asked to stop is that request
            # arriving -- our own checkpoints, FineSub's `DownloadPaused`, or a
            # step failing because we killed the subprocess it was waiting on --
            # so it is reported as a cancellation rather than as a failure.
            if self._cancel.is_set():
                self._update(state="cancelled", stage="cancelled", message=CANCELLED_MESSAGE, resource="", progress=None, error=None)
                return
            self._update(state="failed", stage="failed", message=str(exc), error={"code": "runtime_install_failed", "message": str(exc)}, progress=None)

    def _install_tool_group(self, group: str) -> None:
        """安装一组工具。

        组员里可能有当前清单还没声明的资源（例如尚未发布的 pot-provider），
        那种情况跳过而不是让整组失败：装上其余几样仍然有用，缺的那样由
        「还缺什么」的上报去提示。
        """

        assert self.resources is not None
        for resource_id in TOOL_GROUPS[group]:
            self._stop_if_cancelled()
            if resource_id not in self.resources.resources:
                continue
            if self.resources.status(resource_id).state == "ready":
                continue
            self._install_optional_tool(resource_id)

    def _install_optional_tool(self, resource_id: str) -> None:
        assert self.resources is not None
        if resource_id not in self.resources.resources:
            raise RuntimeProvisionError(f"当前平台没有可安装的 {resource_id} 资源")
        self._update(resource=resource_id, stage="resource", message=f"正在准备 {resource_id}", progress=None)

        def resource_stage(stage: str, message: str) -> None:
            self._stage(stage, f"{resource_id}：{message}", reset_progress=stage == "downloading")

        self.resources.install(resource_id, self._progress, stage=resource_stage, should_pause=self._cancelled)
        if self.resources.status(resource_id).state != "ready":
            raise RuntimeProvisionError(f"{resource_id} 安装完成后仍不可用")

    def _install_media_tools(self) -> None:
        assert self.resources is not None
        resource_ids = ("ffmpeg",) if self.platform == "windows-x64" else ("ffmpeg", "ffprobe")
        pending = [resource_id for resource_id in resource_ids if self.resources.status(resource_id).state != "ready"]
        sizes = {
            resource_id: max(0, int(getattr(self.resources.resources[resource_id].asset, "size", 0)))
            for resource_id in pending
        }
        total = sum(sizes.values())
        completed_before = 0
        for resource_id in pending:
            self._stop_if_cancelled()
            self._update(resource=resource_id, stage="resource", message=f"正在准备 {resource_id}", progress=None)

            def resource_stage(stage: str, message: str, *, current: str = resource_id) -> None:
                with self._lock:
                    progress = self._job.get("progress")
                    if stage != "downloading" and isinstance(progress, dict):
                        progress = {**progress, "bytes_per_second": 0.0}
                    self._job.update(stage=stage, message=f"{current}：{message}", progress=progress)

            def resource_progress(value: Any, *, before: int = completed_before) -> None:
                self._progress(value, completed_before=before, total_override=total)

            self.resources.install(resource_id, resource_progress, stage=resource_stage, should_pause=self._cancelled)
            completed_before += sizes[resource_id]
        for tool in ("ffmpeg", "ffprobe"):
            executable = self.tool_path(tool)
            if executable is None:
                raise RuntimeProvisionError(f"{tool} 安装完成后仍不可用")
            if os.name != "nt":
                executable.chmod(executable.stat().st_mode | 0o755)

    def _install_model(self, model_id: str) -> None:
        assert self.paths is not None and self.runtime is not None
        self._stage("model", f"正在下载并校验 FineSub 模型：{model_id}")
        environment = self.worker_environment()
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        project_src = Path(__file__).resolve().parents[1]
        python_paths = [str(project_src), str(self.vendor / "src")]
        if environment.get("PYTHONPATH"):
            python_paths.append(environment["PYTHONPATH"])
        environment["PYTHONPATH"] = os.pathsep.join(python_paths)
        if model_id == "separator":
            # GitHub files, each already on the ladder inside the installer.
            self._run_model_installer(model_id, environment)
            return
        # Hugging Face reads its endpoint during import, so each rung has to be
        # its own process: `_install_huggingface` calls `snapshot_download`
        # directly, so `model_fetch.fetch_with_fallback` never sees this one.
        from finesub_bootstrap import model_fetch

        climb_huggingface(
            lambda routed: self._run_model_installer(model_id, routed),
            environment,
            is_retryable=model_fetch.is_mirror_failure,
            announce=lambda message: self._stage("model", f"{message}：{model_id}"),
        )

    def _run_model_installer(self, model_id: str, environment: dict[str, str]) -> None:
        assert self.paths is not None and self.runtime is not None
        # Polled rather than waited on, so a cancel reaches a model download that
        # would otherwise run for minutes. The reader thread is what keeps the
        # pipe from filling up and deadlocking the installer in the meantime.
        from finesub_bootstrap.processes import terminate_process_tree

        process = subprocess.Popen(
            [
                str(self.runtime.python_executable), "-m", "nonoka_x.model_install",
                "--model", model_id,
                "--models-root", str(self.paths.models),
                "--data-root", str(self.paths.data_root),
            ],
            cwd=self.vendor,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=(
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if os.name == "nt"
                else 0
            ),
            start_new_session=os.name != "nt",
        )
        tail: deque[str] = deque(maxlen=200)
        # Dropped once the bytes are in: verifying sends none, and is not a crawl.
        watch = SpeedWatch() if speed_watched() else None
        received = 0

        def read_output() -> None:
            nonlocal watch, received
            assert process.stdout is not None
            for line in process.stdout:
                output = line.rstrip()
                event = parse_model_install_event(output, model_id)
                if event is None:
                    tail.append(output)
                    continue
                if event["type"] == "progress":
                    if watch is not None:
                        watch.add(max(0, event["completed"] - received))
                        received = max(received, event["completed"])
                        if event["total"] and event["completed"] >= event["total"]:
                            watch = None
                    self._update(
                        resource=model_id,
                        stage="downloading",
                        message=event["message"],
                        progress={
                            "completed": event["completed"],
                            "total": event["total"],
                            "unit": "bytes",
                            "bytes_per_second": event["bytes_per_second"],
                        },
                    )
                else:
                    if event["stage"] in {"verifying", "completed"}:
                        watch = None
                    self._stage(event["stage"], event["message"], reset_progress=event["stage"] == "preparing")

        reader = threading.Thread(target=read_output, name="nonoka-model-installer-output", daemon=True)
        reader.start()

        def stop_installer() -> None:
            terminate_process_tree(process)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            reader.join(timeout=1)

        while process.poll() is None:
            if self._cancel.is_set():
                stop_installer()
                raise RuntimeProvisionCancelled(CANCELLED_MESSAGE)
            current = watch
            if current is not None:
                try:
                    current.check()
                except DownloadTooSlow:
                    stop_installer()
                    raise
            time.sleep(0.2)
        reader.join(timeout=1)
        if process.returncode != 0:
            detail = "\n".join(tail).strip()
            raise RuntimeProvisionError(detail[-4000:] or f"model installer exited with {process.returncode}")

    def worker_python(self) -> Path | None:
        if self.runtime is None:
            return None
        return self.runtime.python_executable if self.runtime.status().state == "ready" else None

    def remove_all(self) -> dict[str, Any]:
        """Remove only replaceable managed assets, never tasks or user data."""

        if self.paths is None:
            raise RuntimeProvisionError("FineSub runtime installer is unavailable")
        with self._lock:
            if self._job["state"] == "running":
                raise RuntimeProvisionError("安装任务运行中，无法删除环境；请先取消安装")
        targets = (self.paths.runtime, self.paths.models, self.paths.cache, self.paths.agent_capsules)
        allowed_roots = (self.paths.root.resolve(), self.paths.big_data.resolve())
        for target in targets:
            resolved = target.resolve()
            if not any(resolved != root and root in resolved.parents for root in allowed_roots):
                raise RuntimeProvisionError(f"拒绝删除托管目录之外的路径：{resolved}")
        failures: list[str] = []
        for target in targets:
            try:
                _remove_managed_tree(target)
            except OSError as error:
                failures.append(f"{target.name}（{error.strerror or error}）")
        if failures:
            # Reporting the reason beats the bare HTTP 500 a raw OSError used to
            # produce: the shell can show what is still on disk and why.
            detail = "以下目录未能完全删除，可能有文件正在被占用：" + "、".join(failures)
            self._update(
                state="failed",
                target="remove-all",
                resource="",
                stage="failed",
                message=detail,
                progress=None,
                error={"code": "remove_failed", "message": detail},
            )
            raise RuntimeProvisionError(detail)
        self._update(
            state="completed",
            target="remove-all",
            resource="",
            stage="completed",
            message="运行时、模型、可选工具与下载缓存已全部删除；任务、字幕和设置已保留",
            progress=None,
            error=None,
        )
        return self.status()

    def cache_usage(self) -> dict[str, Any]:
        """Measure the download caches clear_cache() would empty.

        `bytes` is what the files add up to; `reclaimable` leaves out the files
        uv hardlinked into the runtime, which stay on disk through the other
        link and are most of the uv cache on a single-volume install. The page
        offers the second number, because it is the one clearing delivers.
        """

        usage = {"schema": 1, "bytes": 0, "reclaimable": 0, "files": 0}
        if self.paths is None:
            return usage
        for entry in self._cache_entries():
            _measure_cache_tree(entry, usage, top=True)
        return usage

    def clear_cache(self) -> dict[str, Any]:
        """Empty the runtime's download caches and report what was freed.

        Only the entries in RUNTIME_CACHE_ENTRIES go; `cache/state` beside them
        is FineSub's rate-limit record and is not a cache at all. The install
        job state is left alone: this is not a provisioning step, and the
        runtime page's last result should not be replaced by it.
        """

        if self.paths is None:
            raise RuntimeProvisionError("FineSub runtime installer is unavailable")
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
                # Empty the entry rather than remove it: it may be a link the
                # user made to keep the cache on another drive, and removing
                # the link would leave that data behind and the drive unused.
                for child in entry.iterdir():
                    try:
                        _remove_managed_tree(child)
                    except OSError as error:
                        failures.append(f"{entry.name}/{child.name}（{error.strerror or error}）")
            after = self.cache_usage()
        finally:
            with self._lock:
                self._clearing_cache = False
        result = {
            **after,
            "freed": max(0, before["reclaimable"] - after["reclaimable"]),
            "removed": max(0, before["bytes"] - after["bytes"]),
            "failures": failures,
        }
        return result

    def _cache_entries(self) -> list[Path]:
        assert self.paths is not None
        cache = self.paths.cache
        allowed_roots = (self.paths.root.resolve(), self.paths.big_data.resolve())
        entries: list[Path] = []
        for name in RUNTIME_CACHE_ENTRIES:
            entry = cache / name
            # Clearing empties the entry's children, so an entry that resolves
            # to the install root or above it would take the runtime and the
            # models with it. A link to anywhere else is the user's own cache
            # arrangement and is emptied like the default.
            resolved = entry.resolve()
            if any(resolved == root or resolved in root.parents for root in allowed_roots):
                raise RuntimeProvisionError(f"拒绝清理托管目录本身：{resolved}")
            entries.append(entry)
        return entries

    def remove_tool_group(self, group: str) -> dict[str, Any]:
        """Remove one managed optional-tool group and leave every other asset intact."""

        if group not in REMOVABLE_TOOL_GROUPS:
            raise RuntimeProvisionError("仅支持卸载视频下载工具或可选工具")
        if self.paths is None or self.resources is None:
            raise RuntimeProvisionError("FineSub runtime installer is unavailable")
        with self._lock:
            if self._job["state"] == "running":
                raise RuntimeProvisionError("安装任务运行中，无法卸载工具；请先取消安装")

        runtime_root = self.paths.runtime.resolve()
        targets: list[Path] = []
        for resource_id in TOOL_GROUPS[group]:
            if resource_id not in self.resources.resources:
                continue
            resource_root = self.resources.install_path(resource_id).parent.resolve()
            if resource_root == runtime_root or runtime_root not in resource_root.parents:
                raise RuntimeProvisionError(f"拒绝删除托管运行时之外的路径：{resource_root}")
            targets.append(resource_root)

        failures: list[str] = []
        for target in targets:
            try:
                _remove_managed_tree(target)
            except OSError as error:
                failures.append(f"{target.name}（{error.strerror or error}）")
        if failures:
            detail = "以下工具未能完全卸载，可能有文件正在被占用：" + "、".join(failures)
            self._update(
                state="failed",
                target=f"remove-{group}",
                resource="",
                stage="failed",
                message=detail,
                progress=None,
                error={"code": "remove_failed", "message": detail},
            )
            raise RuntimeProvisionError(detail)

        label = "视频下载工具" if group == "video-tools" else "可选工具"
        self._update(
            state="completed",
            target=f"remove-{group}",
            resource="",
            stage="completed",
            message=f"{label}已卸载；其他运行时与模型保持不变",
            progress=None,
            error=None,
        )
        return self.status()

    def tool_path(self, name: str) -> Path | None:
        if self.resources is None or name not in {"ffmpeg", "ffprobe", "tokcount"}:
            return None
        filename = f"{name}.exe" if os.name == "nt" else name
        resource_ids = (name, "ffmpeg") if name == "ffprobe" else (name,)
        for resource_id in resource_ids:
            if resource_id not in self.resources.resources:
                continue
            executable = self.resources.active_file(resource_id, filename) or self._unpinned_ffmpeg(resource_id, filename)
            if executable is not None:
                return executable
        return None

    def _unpinned_ffmpeg(self, resource_id: str, filename: str) -> Path | None:
        """`filename` in a copy of the FFmpeg that followed upstream's `latest` tag.

        Pinning the build changed the manifest's required paths, which that copy
        no longer matches. It still works, so it keeps serving instead of the
        update declaring the media tools gone.
        """

        if resource_id != "ffmpeg" or self.resources.active_version(resource_id) != UNPINNED_FFMPEG_VERSION:
            return None
        candidate = (
            self.resources.install_path(resource_id).parent
            / UNPINNED_FFMPEG_VERSION
            / UNPINNED_FFMPEG_DIRECTORY
            / "bin"
            / filename
        )
        return candidate if candidate.is_file() else None

    def worker_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        if self.paths is None or self.resources is None:
            return environment
        environment.update({
            "FINESUB_MODEL_DIR": str(self.paths.models),
            "HF_HOME": str(self.paths.models / "huggingface"),
            "TORCH_HOME": str(self.paths.models / "torch"),
            "UV_CACHE_DIR": str(self.paths.cache / "uv"),
        })
        path_dirs: list[str] = []
        for name in ("ffmpeg", "ffprobe", "tokcount"):
            executable = self.tool_path(name)
            if executable is not None:
                path_dirs.append(str(executable.parent))
        if path_dirs:
            environment["PATH"] = os.pathsep.join([*path_dirs, environment.get("PATH", "")])
        if "yt-dlp" in self.resources.resources and self.resources.status("yt-dlp").state == "ready":
            yt_dlp_path = str(self.resources.install_path("yt-dlp"))
            environment["PYTHONPATH"] = os.pathsep.join(
                part for part in (yt_dlp_path, environment.get("PYTHONPATH", "")) if part
            )
        apply_proxy_environment(environment)
        return environment
