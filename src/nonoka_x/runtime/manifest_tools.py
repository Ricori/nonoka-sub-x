"""Direct-manifest tool layer manager for platforms without R2 prebuilt `.tar.zst` layers (macOS).

Windows ships prebuilt `.tar.zst` layers indexed by `resources/layers.json`. macOS (`macos-arm64`,
`macos-amd64`) pins its media and download tools (`ffmpeg`, `ffprobe`, `yt-dlp`) in
`resources/runtime-manifest.<platform>.json` with exact byte sizes and sha256 digests.

`ManifestToolsManager` subclasses `LayerManager` and materialises those pinned archives into the
exact same on-disk layer layout (`layers/tools/<version>/<tool_id>/`, `.nonoka-layer.json`, and
`layers/tools/current.json`), so the Go desktop (`managedtools.Find`, `managedtools.FindYTDLP`),
the Python runtime facade (`RuntimeManager`), cloud API engines (`media`), and the settings UI all
work uniformly on macOS without a separate code path.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
from typing import Any
import zipfile

from nonoka_x.runtime import layer_manifest as lm
from nonoka_x.runtime.fetch import (
    DownloadAsset,
    DownloadPaused,
    DownloadProgress,
    NetworkRoute,
    download_file,
)
from nonoka_x.runtime.fsops import holding_lock, remove_tree, replace_path, write_atomic
from nonoka_x.runtime.layers import (
    LABELS,
    LOCK_NAME,
    UNPACK_SUFFIX,
    LayerEntry,
    LayerIndex,
    LayerIndexError,
    LayerManager,
    LayerMismatch,
    LayerProgress,
    LayerState,
    ObjectRef,
    PauseCheck,
    ProgressCallback,
    inspect,
    offline_pack,
)
from nonoka_x.runtime.paths import NonokaPaths

_CHUNK_BYTES = 256 * 1024
_PLACEHOLDER_BASE_URL = "https://layers.nonoka.online/"


def manifest_tools_version(resources: Any) -> str:
    """Deterministic 16-hex version derived from the pinned tool manifest entries."""

    canonical = json.dumps(list(resources or ()), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:16]


def _resources_from(manifest_data: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = manifest_data.get("resources")
    if not isinstance(raw, list) or not raw:
        raise LayerIndexError("tool manifest has no resources")
    resources: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise LayerIndexError("tool manifest resource is not an object")
        tool_id = str(item.get("id") or "")
        if not tool_id or tool_id in lm.TOOLS_LEFT_OUT:
            continue
        asset = item.get("asset")
        if not isinstance(asset, Mapping):
            raise LayerIndexError(f"tool {tool_id}: missing asset")
        url = str(asset.get("url") or "")
        sha256 = str(asset.get("sha256") or "").lower()
        size = int(asset.get("size") or 0)
        if not url or len(sha256) != 64 or size <= 0:
            raise LayerIndexError(f"tool {tool_id}: invalid asset metadata")
        directory = lm.check_path(str(item.get("directory") or tool_id))
        required = [lm.check_path(str(entry)) for entry in (item.get("required_files") or ())]
        resources.append({
            "id": tool_id,
            "version": str(item.get("version") or ""),
            "directory": directory,
            "archive_type": str(item.get("archive_type") or "zip"),
            "required_files": required,
            "asset": {"url": url, "size": size, "sha256": sha256},
        })
    if not resources:
        raise LayerIndexError("tool manifest has no installable resources")
    return resources


def _build_index(platform_id: str, resources: list[dict[str, Any]]) -> LayerIndex:
    canonical = json.dumps(resources, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    version = digest[:16]
    total_size = sum(int(item["asset"]["size"]) for item in resources)
    entry = LayerEntry(
        layer="tools",
        version=version,
        manifest=ObjectRef(f"v1/tools/{digest}.json", len(canonical), digest),
        archive=ObjectRef(f"v1/tools/{digest}.zip", total_size, digest),
    )
    return LayerIndex(platform=platform_id, base_url=_PLACEHOLDER_BASE_URL, layers={"tools": entry})


def _extract_zip(archive: Path, destination: Path, should_pause: PauseCheck | None = None) -> None:
    """Safely extract a zip/wheel archive into `destination`, rejecting links and traversal."""

    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    try:
        with zipfile.ZipFile(archive, "r") as zf:
            for info in zf.infolist():
                if should_pause is not None and should_pause():
                    raise DownloadPaused("Unpacking paused")
                raw_name = info.filename.replace("\\", "/")
                if not raw_name or raw_name == "/":
                    continue
                is_dir = info.is_dir() or raw_name.endswith("/")
                cleaned = raw_name.rstrip("/")
                if not cleaned:
                    continue
                try:
                    rel = lm.check_path(cleaned)
                except lm.LayerError as error:
                    raise LayerMismatch(f"压缩包 {archive.name} 包含非法路径：{info.filename!r}") from error
                unix_full = info.external_attr >> 16
                if stat.S_ISLNK(unix_full):
                    raise LayerMismatch(f"压缩包 {archive.name} 包含符号链接，已拒绝解压：{rel}")
                target = (destination / rel).resolve()
                try:
                    target.relative_to(root)
                except ValueError as error:
                    raise LayerMismatch(f"压缩包 {archive.name} 路径越界：{info.filename!r}") from error
                if is_dir:
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as src, target.open("wb") as dst:
                    while True:
                        if should_pause is not None and should_pause():
                            raise DownloadPaused("Unpacking paused")
                        chunk = src.read(_CHUNK_BYTES)
                        if not chunk:
                            break
                        dst.write(chunk)
                unix_perm = unix_full & 0o777
                if unix_perm:
                    os.chmod(target, unix_perm)
    except zipfile.BadZipFile as error:
        raise LayerMismatch(f"工具压缩包损坏，无法解压（{archive.name}）：{error}") from error


def _ensure_executable(path: Path) -> None:
    if os.name == "nt":
        return
    mode = path.stat().st_mode & 0o777
    desired = mode | 0o755
    if desired != mode:
        os.chmod(path, desired)


def _is_binary_required_file(rel_path: str) -> bool:
    return PurePosixPath(rel_path).suffix == ""


class ManifestToolsManager(LayerManager):
    """LayerManager implementation for platforms that install `tools` from a pinned tool manifest."""

    def __init__(
        self,
        paths: NonokaPaths,
        manifest_data: Mapping[str, Any],
        *,
        in_use: Callable[[], bool] | None = None,
        offline: Callable[[], Path | None] = offline_pack,
        routes: Callable[[], list[NetworkRoute]] | None = None,
        tar: Path | None = None,
    ) -> None:
        self.platform = str(manifest_data.get("platform") or lm.current_platform())
        self._resources = _resources_from(manifest_data)
        index = _build_index(self.platform, self._resources)
        super().__init__(paths, index, in_use=in_use, offline=offline, routes=routes, tar=tar)

    def _state(self, layer: str, entry: LayerEntry, *, full: bool) -> LayerState:
        current = self.pointer(layer)
        base = dict(layer=layer, wanted=entry.version, current=current, full=full)
        if current is None:
            return LayerState(state="missing", path=None, **base)
        try:
            directory = self.paths.layer_version(layer, current)
        except ValueError:
            return LayerState(state="damaged", path=None, problems=(f"current.json names {current!r}",), **base)
        if current != entry.version:
            return LayerState(state="outdated", path=directory if directory.is_dir() else None, **base)
        if not directory.is_dir():
            return LayerState(state="missing", path=None, **base)
        try:
            manifest, _ = lm.manifest_in(directory)
        except (OSError, ValueError, lm.LayerError) as error:
            return LayerState(state="damaged", path=directory, problems=(f"manifest: {error}",), **base)
        if manifest.layer != layer or manifest.platform != self.platform:
            return LayerState(state="damaged", path=directory, problems=("manifest platform or layer mismatch",), **base)
        if manifest_tools_version(manifest.sources.get("tools") or ()) != entry.version:
            return LayerState(state="damaged", path=directory, problems=("manifest sources do not match wanted tools",), **base)
        try:
            found = inspect(manifest, directory, full=full)
        except OSError as error:
            return LayerState(state="damaged", path=directory, problems=(f"unreadable: {error}",), **base)
        problems = list(found.broken)
        for item in self._resources:
            tool_root = directory / item["directory"]
            for req in item["required_files"]:
                req_path = tool_root / req
                if not req_path.is_file():
                    problems.append(f"missing required tool file: {item['directory']}/{req}")
                elif os.name != "nt" and _is_binary_required_file(req) and not os.access(req_path, os.X_OK):
                    problems.append(f"tool binary is not executable: {item['directory']}/{req}")
        extra = tuple(found.extra + [f"{path}/" for path in found.extra_dirs])
        if problems:
            return LayerState(state="damaged", path=directory, problems=tuple(problems), extra=extra, **base)
        return LayerState(state="ready", path=directory, extra=extra, **base)

    def install(
        self,
        layer: str,
        *,
        archive_path: Path | None = None,
        progress: ProgressCallback | None = None,
        should_pause: PauseCheck | None = None,
        full: bool = False,
        force: bool = False,
    ) -> LayerState:
        if layer != "tools":
            raise LayerIndexError(f"{self.platform} only supports the 'tools' layer, not {layer!r}")
        if archive_path is not None:
            raise LayerMismatch(f"{self.platform} 暂不支持从离线 .tar.zst 包导入工具层，请直接在线安装")

        entry = self.index.entry("tools")
        label = LABELS.get("tools", "tools")
        total_bytes = entry.archive.size
        lock_file = self.paths.layer("tools") / LOCK_NAME
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        with holding_lock(
            lock_file,
            should_pause=should_pause,
            on_pause=lambda: DownloadPaused("Install paused"),
        ):
            if not force:
                existing = self.state("tools", full=full, fresh=True)
                if existing.ready and not existing.extra:
                    if progress is not None:
                        progress(LayerProgress("tools", "completed", f"{label}已准备就绪", total_bytes, total_bytes))
                    return existing

            downloaded: list[Path] = []
            prior = 0
            for item in self._resources:
                if should_pause is not None and should_pause():
                    raise DownloadPaused("Download paused")
                res_id = item["id"]
                asset = item["asset"]
                url = asset["url"]
                size = int(asset["size"])
                sha256 = asset["sha256"]
                suffix = ".whl" if url.lower().endswith(".whl") else ".zip"
                dest = self.downloads / f"{sha256}-{res_id}{suffix}"

                def _on_download(
                    dl: DownloadProgress,
                    *,
                    _prior: int = prior,
                    _res_id: str = res_id,
                ) -> None:
                    if progress is not None:
                        progress(
                            LayerProgress(
                                "tools",
                                "downloading",
                                f"正在下载{label}（{_res_id}）",
                                _prior + dl.downloaded,
                                total_bytes,
                                dl.bytes_per_second,
                            )
                        )

                path = download_file(
                    DownloadAsset(url=url, size=size, sha256=sha256),
                    dest,
                    _on_download,
                    should_pause,
                    routes=self._routes,
                )
                downloaded.append(path)
                prior += size

            target = self.paths.layer_version("tools", entry.version)
            staging = target.with_name(entry.version + UNPACK_SUFFIX)
            remove_tree(staging)
            staging.mkdir(parents=True, exist_ok=True)
            try:
                for idx, (item, archive) in enumerate(zip(self._resources, downloaded), 1):
                    res_id = item["id"]
                    if progress is not None:
                        progress(LayerProgress("tools", "unpacking", f"正在解压{label}（{res_id}）", idx - 1, len(self._resources)))
                    if item["archive_type"] != "zip":
                        raise LayerMismatch(f"不支持的工具归档格式：{item['archive_type']}")
                    tool_dest = staging / item["directory"]
                    _extract_zip(archive, tool_dest, should_pause=should_pause)
                    for req in item["required_files"]:
                        req_path = tool_dest / req
                        if not req_path.is_file():
                            raise LayerMismatch(f"{res_id} 解压后缺少必需文件：{req}")
                        if _is_binary_required_file(req):
                            _ensure_executable(req_path)

                if progress is not None:
                    progress(LayerProgress("tools", "checking", f"正在校验{label}文件"))
                files, empty_dirs = lm.scan(staging)
                manifest = lm.LayerManifest(
                    layer="tools",
                    platform=self.platform,
                    files=files,
                    empty_dirs=empty_dirs,
                    sources={"tools": [dict(item) for item in self._resources]},
                )
                write_atomic(staging / lm.MANIFEST_NAME, lm.render(manifest).decode("utf-8"))
                checked = inspect(manifest, staging, full=False)
                if checked.problems:
                    raise LayerMismatch(f"{label}校验失败：" + "；".join(checked.problems[:5]))

                if os.path.lexists(target):
                    self._retire(target)
                replace_path(staging, target)
            except BaseException:
                remove_tree(staging)
                raise

            self._write_pointer("tools", entry.version)
            self.forget("tools")
            with self._lock:
                self._manifests.pop(("tools", entry.version), None)
            for path in downloaded:
                path.unlink(missing_ok=True)
                try:
                    path.with_suffix(path.suffix + ".lock").unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                self.remove_unused("tools")
            except OSError:
                pass
            state = self.state("tools", fresh=True)
            if progress is not None and state.ready:
                progress(LayerProgress("tools", "completed", f"{label}已准备就绪", total_bytes, total_bytes))
            return state


def open_tools_manager(
    paths: NonokaPaths,
    platform_id: str | None = None,
    **options: Any,
) -> ManifestToolsManager | None:
    """Open a `ManifestToolsManager` for `platform_id` (default: host) if a tool manifest exists."""

    from nonoka_x.runtime import manifests

    resolved = platform_id or lm.current_platform()
    data = manifests.tool_manifest_for(resolved)
    if data is None:
        return None
    return ManifestToolsManager(paths, data, **options)


__all__ = [
    "ManifestToolsManager",
    "manifest_tools_version",
    "open_tools_manager",
]
