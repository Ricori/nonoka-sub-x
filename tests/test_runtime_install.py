from pathlib import Path
from types import SimpleNamespace

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
