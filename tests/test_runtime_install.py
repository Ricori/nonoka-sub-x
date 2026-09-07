from pathlib import Path
from types import SimpleNamespace

from finesub_bootstrap.models import DownloadProgress
from finesub_bootstrap.environment import RuntimeEnvironment
from finesub_bootstrap.paths import AppPaths
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


def test_runtime_accepts_markers_from_the_packaged_042_lock(tmp_path: Path) -> None:
    """The 0.5 header move must not rebuild an unchanged multi-GB runtime."""

    runtime = RuntimeEnvironment(
        paths=AppPaths.for_root(
            tmp_path / "install",
            data_root=tmp_path / "data",
            big_data=tmp_path / "install",
        ),
        app_source=VENDOR,
        runtime_lock=RUNTIME / "pylock.win-py312.toml",
        uv_executable=lambda: tmp_path / "uv.exe",
    )

    for legacy_hash in (
        "86e22beb2c6d2f603547a1147f3cecad2c388e1b43ca37aadf13bd7c6d6280ce",
        "fb58922d3725f64e505849877657bec8c96611287da6e4e36795596a17231b4d",
    ):
        assert runtime._marker_is_current(
            {
                "schemaVersion": 2,
                "pythonVersion": "3.12",
                "runtimeLockHash": legacy_hash,
            }
        )


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
