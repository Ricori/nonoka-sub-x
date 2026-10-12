import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from finesub_bootstrap.environment import RuntimeEnvironment
from finesub_bootstrap.models import DownloadProgress
import nonoka_x.runtime_install as runtime_install
from nonoka_x.runtime_install import (
    KNOWN_WHEEL_SIZES,
    ProgressRuntimeEnvironment,
    large_wheels_from_lock,
)


VENDOR = Path(__file__).resolve().parents[1] / "third_party" / "finesub"
#: The runtime locks are package data since upstream 0.5.0; the snapshot no
#: longer carries the `runtime/` copy the desktop split used to publish.
RUNTIME = VENDOR / "src" / "finesub_bootstrap"


def test_runtime_locks_expose_torch_as_an_observable_large_download() -> None:
    canonical = large_wheels_from_lock(RUNTIME / "pylock.win-py312.toml")
    mainland = large_wheels_from_lock(RUNTIME / "pylock.win-py312.cn.toml")

    assert [wheel.package for wheel in canonical] == ["torch"]
    assert [wheel.package for wheel in mainland] == ["torch"]
    assert canonical[0].size == mainland[0].size == next(iter(KNOWN_WHEEL_SIZES.values()))
    assert canonical[0].sha256 == mainland[0].sha256
    assert canonical[0].url.startswith("https://download-r2.pytorch.org/")
    assert mainland[0].url.startswith("https://mirror.sjtu.edu.cn/")


def test_runtime_wheel_keeps_a_valid_filename_after_url_decoding() -> None:
    wheel = large_wheels_from_lock(RUNTIME / "pylock.win-py312.toml")[0]

    assert wheel.filename == "torch-2.11.0+cu128-cp312-cp312-win_amd64.whl"


def test_prefetch_forwards_real_byte_progress_to_the_desktop(
    tmp_path: Path,
    monkeypatch,
) -> None:
    progress: list[DownloadProgress] = []
    stages: list[tuple[str, str]] = []
    runtime = object.__new__(ProgressRuntimeEnvironment)
    runtime.paths = SimpleNamespace(cache=tmp_path)
    runtime.download_progress = progress.append
    runtime._progress_stage = lambda stage, message: stages.append((stage, message))

    def fake_download(asset, destination, report, should_pause) -> None:
        report(
            DownloadProgress(
                downloaded=asset.size // 2,
                total=asset.size,
                bytes_per_second=10 * 1024 * 1024,
            )
        )
        report(
            DownloadProgress(
                downloaded=asset.size,
                total=asset.size,
                bytes_per_second=10 * 1024 * 1024,
            )
        )

    monkeypatch.setattr(runtime_install, "download_asset", fake_download)
    paths = runtime._prefetch_large_wheels(
        RUNTIME / "pylock.win-py312.toml",
        stage_message="正在下载 {package}（{size}）",
        should_pause=lambda: False,
    )

    assert stages == [("downloading", "正在下载 torch（2.6 GiB）")]
    assert len(paths) == 1
    assert progress[0].downloaded == progress[0].total // 2
    assert progress[-1].downloaded == progress[-1].total


def test_an_installed_wheel_is_neither_downloaded_nor_reinstalled(tmp_path: Path, monkeypatch) -> None:
    site_packages = tmp_path / "Lib" / "site-packages"
    wheel = large_wheels_from_lock(RUNTIME / "pylock.win-py312.toml")[0]
    assert not runtime_install.wheel_installed(wheel, site_packages)
    (site_packages / "torch-2.10.0+cu128.dist-info").mkdir(parents=True)
    assert not runtime_install.wheel_installed(wheel, site_packages)
    (site_packages / "torch-2.11.0+cu128.dist-info").mkdir()
    assert runtime_install.wheel_installed(wheel, site_packages)

    runtime = object.__new__(ProgressRuntimeEnvironment)
    runtime.paths = SimpleNamespace(cache=tmp_path)
    runtime.download_progress = lambda _value: None
    runtime._progress_stage = None
    monkeypatch.setattr(
        runtime_install, "download_asset",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not download")),
    )
    assert runtime._prefetch_large_wheels(
        RUNTIME / "pylock.win-py312.toml",
        stage_message="{package}{size}",
        should_pause=lambda: False,
        installed_in=site_packages,
    ) == []


def _bare_runtime(tmp_path: Path) -> ProgressRuntimeEnvironment:
    runtime = object.__new__(ProgressRuntimeEnvironment)
    runtime.paths = SimpleNamespace(runtime=tmp_path / "runtime", cache=tmp_path / "cache")
    runtime.development_python = None
    runtime.runtime_root.mkdir(parents=True)
    return runtime


def test_an_outdated_runtime_is_updated_in_place_and_marked_last(tmp_path: Path, monkeypatch) -> None:
    runtime = _bare_runtime(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(runtime_install, "apply_network_environment", lambda environment: None)
    runtime._install_dependencies = lambda uv, python, environment, **kwargs: calls.append(f"install:{python.name}")
    runtime._verify_staging = lambda *args, **kwargs: calls.append("verify")
    runtime.runtime_validator = lambda python: (True, "")
    runtime._marker = lambda: {"runtimeLockHash": "new"}
    runtime.status = lambda: "ready"

    assert runtime._update_in_place(Path("uv.exe"), stage=None, log=None, should_pause=None, verify_hashes=False) == "ready"
    assert calls == ["install:python.exe", "verify"]
    assert json.loads(runtime.marker_path.read_text(encoding="utf-8")) == {"runtimeLockHash": "new"}

    # A runtime that fails its import probe keeps the old marker: still outdated.
    runtime.marker_path.write_text('{"runtimeLockHash": "old"}', encoding="utf-8")
    runtime.runtime_validator = lambda python: (False, "torch import failed")
    with pytest.raises(RuntimeError, match="torch import failed"):
        runtime._update_in_place(Path("uv.exe"), stage=None, log=None, should_pause=None, verify_hashes=False)
    assert json.loads(runtime.marker_path.read_text(encoding="utf-8")) == {"runtimeLockHash": "old"}


def test_a_failed_in_place_update_falls_back_to_the_staging_rebuild(tmp_path: Path, monkeypatch) -> None:
    runtime = _bare_runtime(tmp_path)
    logs: list[str] = []
    rebuilt: list[bool] = []
    runtime.condition = lambda: "outdated"

    def failing_update(*args, **kwargs):
        raise RuntimeError("uv exited with 2")

    runtime._update_in_place = failing_update
    monkeypatch.setattr(
        RuntimeEnvironment, "_install_locked", lambda self, uv, **kwargs: rebuilt.append(True) or "rebuilt"
    )
    assert runtime._install_locked(Path("uv.exe"), stage=None, log=logs.append, should_pause=None) == "rebuilt"
    assert rebuilt == [True]
    assert "完整重建" in logs[0]

    # A cancellation stays a cancellation.
    def paused(*args, **kwargs):
        raise runtime_install.DownloadPaused("paused")

    runtime._update_in_place = paused
    with pytest.raises(runtime_install.DownloadPaused):
        runtime._install_locked(Path("uv.exe"), stage=None, log=None, should_pause=None)
    assert rebuilt == [True]

    # Anything but an upgrade is upstream's own path.
    runtime.condition = lambda: "damaged"
    runtime._update_in_place = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("not for damage"))
    assert runtime._install_locked(Path("uv.exe"), stage=None, log=None, should_pause=None) == "rebuilt"
