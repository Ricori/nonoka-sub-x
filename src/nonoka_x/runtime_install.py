"""Observable installation of the large wheels in FineSub's runtime lock,
and in-place updates of a runtime whose lock moved on."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
import os
from pathlib import Path
import tomllib
from typing import Any
from urllib.parse import unquote, urlparse

from finesub_bootstrap.downloader import (
    DigestMismatch,
    DownloadPaused,
    SizeMismatch,
    download_asset,
)
from finesub_bootstrap.environment import (
    RuntimeEnvironment,
    _is_retryable_from_mirror,
)
from finesub_bootstrap.http_client import apply_network_environment
from finesub_bootstrap.models import DownloadAsset, DownloadProgress
from finesub_bootstrap.runtime_health import site_packages_of

from nonoka_x.download_route import live_proxy, proxy_label
from nonoka_x.msvc_runtime import explaining, msvc_runtime_problem


# PyTorch's package index does not publish wheel sizes, so uv cannot put them
# into the generated PEP 751 lock. The digest still pins immutable bytes; this
# table supplies the matching Content-Length so FineSub's resumable downloader
# can report real progress and reject truncated downloads.
KNOWN_WHEEL_SIZES = {
    "7c78215c3af4f62e63f2b2e360f1722fc719b0853c7ac22666483d9810613a4c": 2_753_189_216,
}
LARGE_WHEEL_THRESHOLD = 256 * 1024 * 1024

ProgressCallback = Callable[[DownloadProgress], None]


@dataclass(frozen=True, slots=True)
class LockedWheel:
    package: str
    filename: str
    url: str
    size: int
    sha256: str


def large_wheels_from_lock(lock: Path) -> list[LockedWheel]:
    """Return locked wheels large enough to benefit from explicit progress."""

    body = tomllib.loads(lock.read_text(encoding="utf-8"))
    result: list[LockedWheel] = []
    for package in body.get("packages", []):
        if not isinstance(package, dict):
            continue
        package_name = str(package.get("name") or "")
        for wheel in package.get("wheels", []):
            if not isinstance(wheel, dict):
                continue
            hashes = wheel.get("hashes")
            if not isinstance(hashes, dict):
                continue
            sha256 = str(hashes.get("sha256") or "").lower()
            size_value = wheel.get("size", KNOWN_WHEEL_SIZES.get(sha256, 0))
            size = int(size_value) if isinstance(size_value, int) else 0
            url = str(wheel.get("url") or "")
            filename = Path(unquote(urlparse(url).path)).name
            if size < LARGE_WHEEL_THRESHOLD or not url or not filename:
                continue
            result.append(
                LockedWheel(
                    package=package_name,
                    filename=filename,
                    url=url,
                    size=size,
                    sha256=sha256,
                )
            )
    return result


def _normalized(name: str) -> str:
    return name.lower().replace("-", "_").replace(".", "_")


def wheel_installed(wheel: LockedWheel, site_packages: Path) -> bool:
    """Whether this exact distribution is already installed in `site_packages`.

    Read from the wheel's own filename (`name-version-tags.whl`) against the
    `name-version.dist-info` directory pip and uv leave behind, so a runtime
    being updated in place does not fetch a multi-GB wheel it already holds.
    """

    parts = wheel.filename.split("-")
    if len(parts) < 2 or not site_packages.is_dir():
        return False
    name, version = _normalized(parts[0]), parts[1].lower()
    for entry in site_packages.glob("*.dist-info"):
        installed_name, _, installed_version = entry.name.removesuffix(".dist-info").rpartition("-")
        if entry.is_dir() and _normalized(installed_name) == name and installed_version.lower() == version:
            return True
    return False


def _format_size(size: int) -> str:
    return f"{size / 1024**3:.1f} GiB" if size >= 1024**3 else f"{size / 1024**2:.0f} MiB"


class ProgressRuntimeEnvironment(RuntimeEnvironment):
    """Prefetch very large wheels with byte progress before invoking uv, and
    update an outdated runtime in place instead of rebuilding it.

    Upstream rebuilds a runtime whose dependency lock changed from scratch in a
    staging tree. That is cheap for upstream -- uv fetched every wheel by URL,
    so the rebuild finds them in its cache -- but not here: the largest wheels
    are fetched by this class, by file, and uv's cache cannot find a file-path
    entry again by URL. So an outdated runtime is first brought up to the new
    lock where it stands (only the packages that changed are installed), and
    only if that fails does the staging rebuild run. The prefetched wheels are
    also kept, so a rebuild that does happen finds them on disk.
    """

    def __init__(self, *, download_progress: ProgressCallback, **values: Any) -> None:
        super().__init__(**values)
        self.download_progress = download_progress
        self.runtime_validator = explaining(self.runtime_validator)

    def _install_dependencies(
        self,
        uv: Path,
        staging_python: Path,
        environment: dict[str, str],
        *,
        log,
        should_pause,
    ) -> None:
        regional = self.regional_lock()
        selected = regional or self.runtime_lock
        proxy = proxy_label(live_proxy())
        # A wheel the target already holds needs neither a download nor a
        # reinstall: true of every unchanged large wheel in an in-place update,
        # never of a fresh staging tree.
        installed_in = site_packages_of(staging_python)
        try:
            local_wheels = self._prefetch_large_wheels(
                selected,
                stage_message=(
                    "正在从中国大陆镜像下载 {package}（{size}）"
                    if regional is not None
                    else f"正在通过{proxy} 下载 {{package}}（{{size}}）"
                    if proxy
                    else "正在下载 {package}（{size}）"
                ),
                should_pause=should_pause,
                installed_in=installed_in,
            )
        except Exception as error:
            mirror_can_retry = isinstance(error, (DigestMismatch, SizeMismatch)) or _is_retryable_from_mirror(error)
            if regional is None or isinstance(error, DownloadPaused) or not mirror_can_retry:
                raise
            if log is not None:
                log(f"镜像下载失败，改用官方源重试：{error}")
            from finesub_bootstrap.download_routes import record_failure

            record_failure(self.paths.data_root, "pypi")
            local_wheels = self._prefetch_large_wheels(
                self.runtime_lock,
                stage_message="正在从官方源下载 {package}（{size}）",
                should_pause=should_pause,
                installed_in=installed_in,
            )

        if local_wheels:
            if self._progress_stage is not None:
                self._progress_stage(
                    "installing_dependencies",
                    "大型依赖下载完成，正在安装到隔离环境",
                )
            self._run(
                [
                    str(uv),
                    "pip",
                    "install",
                    "--python",
                    str(staging_python),
                    "--no-deps",
                    *(str(path) for path in local_wheels),
                ],
                environment,
                log=log,
                should_pause=should_pause,
            )

        # The prefetched wheels stay where they are, deliberately. uv's cache
        # entry for them is keyed by this file path, so with the file gone the
        # next rebuild -- any engine upgrade that changes the lock -- would
        # download the multi-GB wheel again; kept, the downloader finds it and
        # verifies its digest instead. "删除环境" removes the cache with the rest.
        super()._install_dependencies(
            uv,
            staging_python,
            environment,
            log=log,
            should_pause=should_pause,
        )

    def _prefetch_large_wheels(
        self,
        lock: Path,
        *,
        stage_message: str,
        should_pause,
        installed_in: Path | None = None,
    ) -> list[Path]:
        wheels = [
            wheel
            for wheel in large_wheels_from_lock(lock)
            if installed_in is None or not wheel_installed(wheel, installed_in)
        ]
        if not wheels:
            return []

        total = sum(wheel.size for wheel in wheels)
        completed_before = 0
        destinations: list[Path] = []
        for wheel in wheels:
            if should_pause is not None and should_pause():
                raise DownloadPaused("Python environment installation paused")
            if destinations:
                completed_before += wheels[len(destinations) - 1].size
            message = stage_message.format(
                package=wheel.package,
                size=_format_size(wheel.size),
            )
            # RuntimeEnvironment's public stage callback is supplied by the
            # provisioner; use it directly so the UI switches back to download.
            # It is temporarily attached for this call by install().
            if self._progress_stage is not None:
                self._progress_stage("downloading", message)

            before = completed_before

            def report(value: DownloadProgress, *, offset: int = before) -> None:
                self.download_progress(
                    DownloadProgress(
                        downloaded=offset + value.downloaded,
                        total=total,
                        bytes_per_second=value.bytes_per_second,
                    )
                )

            destination = (
                self.paths.cache
                / "runtime-wheels"
                / wheel.sha256
                / wheel.filename
            )
            download_asset(
                DownloadAsset(
                    url=wheel.url,
                    size=wheel.size,
                    sha256=wheel.sha256,
                ),
                destination,
                report,
                should_pause,
            )
            destinations.append(destination)
        return destinations

    def install(self, *, stage=None, log=None, should_pause=None):
        self._progress_stage = stage
        try:
            return super().install(
                stage=stage,
                log=log,
                should_pause=should_pause,
            )
        finally:
            self._progress_stage = None

    def _install_locked(self, uv: Path, *, stage, log, should_pause, force: bool = False,
                        verify_hashes: bool = False, holds_lease: bool = False):
        """Update an outdated runtime in place; rebuild it when that fails.

        Runs under upstream's install lock and after its in-use check, like the
        rebuild it stands in front of. The fallback is safe from any point:
        the rebuild builds a separate staging tree and never reads the live one.
        A pause is the user's cancellation and is not turned into a rebuild --
        the marker is still the old one, so the next attempt updates again.

        A machine whose VC++ runtime cannot load torch is refused before any
        download: the install would only fail in its import probe, gigabytes
        later. A runtime that is already ready is left alone.
        """

        problem = msvc_runtime_problem()
        if problem is not None and (force or self.status().state != "ready"):
            raise RuntimeError(problem)
        if not force and self.condition() == "outdated":
            try:
                return self._update_in_place(
                    uv, stage=stage, log=log, should_pause=should_pause, verify_hashes=verify_hashes
                )
            except DownloadPaused:
                raise
            except Exception as error:  # noqa: BLE001 - any failure falls back to the rebuild
                if log is not None:
                    log(f"增量更新未能完成，改为完整重建运行环境：{error}")
        return super()._install_locked(
            uv,
            stage=stage,
            log=log,
            should_pause=should_pause,
            force=force,
            verify_hashes=verify_hashes,
            holds_lease=holds_lease,
        )

    def _update_in_place(self, uv: Path, *, stage, log, should_pause, verify_hashes: bool):
        """Bring the live runtime to the current lock, installing only what changed.

        The same three steps that make a staging tree acceptable -- install
        from the lock, hold every file to it (`_verify_staging` mends what
        fails), import-probe it -- and then the marker, written last, so a run
        that stops part-way is still `outdated` and is retried, not trusted.
        """

        python = self.python_executable
        environment = os.environ.copy()
        environment.update(
            {
                "UV_CACHE_DIR": str(self.paths.cache / "uv"),
                "UV_PYTHON_INSTALL_DIR": str(self.paths.runtime / "python-builds"),
                "PYTHONUTF8": "1",
                "UV_SYSTEM_PYTHON": "1",
            }
        )
        apply_network_environment(environment)
        if stage is not None:
            stage("installing_dependencies", "正在更新运行环境：只安装有变化的依赖")
        self._install_dependencies(uv, python, environment, log=log, should_pause=should_pause)
        self._verify_staging(
            uv, python, environment, verify_hashes=verify_hashes, log=log, should_pause=should_pause
        )
        healthy, detail = self.runtime_validator(python)
        if not healthy:
            raise RuntimeError(detail or "FineSub runtime dependency validation failed")
        if should_pause is not None and should_pause():
            raise DownloadPaused("Python environment update paused")
        if stage is not None:
            stage("activating", "正在校验并启用 Python 环境")
        self.marker_path.write_text(
            json.dumps(self._marker(), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
            newline="\n",
        )
        return self.status()
