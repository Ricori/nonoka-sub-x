import json
from pathlib import Path
import subprocess
import tomllib
from types import SimpleNamespace

import pytest

from finesub_bootstrap import http_client
from finesub_bootstrap.environment import RuntimeEnvironment
from finesub_bootstrap.http_client import NetworkRoute
from finesub_bootstrap.models import DownloadProgress
from nonoka_x.download_route import DownloadStage, DownloadTooSlow, DownloadUnreachable
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


def _ladder_runtime(tmp_path: Path, monkeypatch, *, proxy: str | None, fail: int, output: str):
    """A runtime whose uv records each `--requirement` run and fails the first `fail`."""

    monkeypatch.delenv("FINESUB_GITHUB_FILE_PROXY", raising=False)
    monkeypatch.setattr(
        http_client,
        "network_routes",
        lambda: ([NetworkRoute("代理", proxy)] if proxy else []) + [NetworkRoute("直连", None)],
    )
    monkeypatch.setattr(runtime_install, "site_packages_of", lambda python: tmp_path)
    monkeypatch.setattr(runtime_install, "ensure_fast_enough", lambda url, stage: None)
    runtime = object.__new__(ProgressRuntimeEnvironment)
    runtime.paths = SimpleNamespace(cache=tmp_path / "cache")
    runtime.runtime_lock = RUNTIME / "pylock.win-py312.toml"
    runtime._progress_stage = None
    runtime._prefetch_large_wheels = lambda lock, **kwargs: []
    runs: list[tuple[Path, dict[str, str]]] = []

    def run(command, environment, *, log, should_pause) -> None:
        runs.append((Path(command[command.index("--requirement") + 1]), environment))
        if len(runs) <= fail:
            raise subprocess.CalledProcessError(1, command, output=output)

    runtime._run = run
    return runtime, runs


def _install(runtime) -> None:
    runtime._install_dependencies(Path("uv.exe"), Path("python.exe"), {"PATH": "x"}, log=None, should_pause=None)


def test_dependencies_climb_proxy_direct_then_the_mirror_lock(tmp_path: Path, monkeypatch) -> None:
    proxy = "http://127.0.0.1:7890"
    network_error = "error sending request for url\ntcp connect error\n由于连接方没有响应 (os error 10060)"
    runtime, runs = _ladder_runtime(tmp_path, monkeypatch, proxy=proxy, fail=2, output=network_error)

    _install(runtime)

    assert [environment["HTTPS_PROXY"] for _lock, environment in runs] == [proxy, "", ""]
    assert [lock.name for lock, _environment in runs] == [
        "pylock.win-py312.toml",
        "pylock.win-py312.toml",
        "pylock.win-py312.cn.toml",
    ]


def test_the_mirror_lock_sends_github_files_to_the_github_mirror(tmp_path: Path, monkeypatch) -> None:
    runtime, _runs = _ladder_runtime(tmp_path, monkeypatch, proxy=None, fail=0, output="")

    lock = runtime._mirror_lock()

    body = lock.read_text(encoding="utf-8")
    assert lock.name == "pylock.win-py312.cn.toml"
    assert 'url = "https://gh-proxy.com/https://github.com/caca2331/finesub/' in body
    assert 'url = "https://github.com/' not in body
    assert "pypi.tuna.tsinghua.edu.cn" in body
    assert tomllib.loads(body)["packages"]


def test_the_backup_lock_sends_github_files_to_our_own_copy(tmp_path: Path, monkeypatch) -> None:
    runtime, _runs = _ladder_runtime(tmp_path, monkeypatch, proxy=None, fail=0, output="")

    lock = runtime._mirror_lock(DownloadStage("backup"))

    body = lock.read_text(encoding="utf-8")
    assert lock.name == "pylock.win-py312.cn.toml"
    assert 'url = "https://cloudfile.nonoka.online/github-mirror/github.com/caca2331/finesub/' in body
    assert 'url = "https://github.com/' not in body
    assert "pypi.tuna.tsinghua.edu.cn" in body


def _offline_pack_holding(tmp_path: Path, monkeypatch, *wheels: tuple[str, str]) -> Path:
    pack = tmp_path / "offline pack 离线"
    pack.mkdir()
    (pack / "pack.json").write_text("{}", encoding="utf-8")
    for sha256, name in wheels:
        (pack / "files" / sha256).mkdir(parents=True)
        (pack / "files" / sha256 / name).write_bytes(b"wheel")
    monkeypatch.setenv("NONOKA_OFFLINE_PACK", str(pack))
    return pack


def _first_lock_wheel() -> tuple[str, str]:
    body = (RUNTIME / "pylock.win-py312.toml").read_text(encoding="utf-8")
    found = runtime_install._LOCK_FILE.search(body)
    return found[3], found[1].rsplit("/", 1)[-1]


def test_the_offline_package_is_tried_before_any_network_rung(tmp_path: Path, monkeypatch) -> None:
    sha256, name = _first_lock_wheel()
    _offline_pack_holding(tmp_path, monkeypatch, (sha256, name))
    runtime, runs = _ladder_runtime(tmp_path, monkeypatch, proxy=None, fail=0, output="")
    samples: list[str] = []
    monkeypatch.setattr(runtime_install, "ensure_fast_enough", lambda url, stage: samples.append(url))
    logged: list[str] = []

    runtime._install_dependencies(Path("uv.exe"), Path("python.exe"), {"PATH": "x"}, log=logged.append, should_pause=None)

    assert len(runs) == 1
    body = runs[0][0].read_text(encoding="utf-8")
    assert "local-lock" in str(runs[0][0])
    assert f'/offline pack 离线/files/{sha256}/' in body and 'path = "' in body
    assert tomllib.loads(body)["packages"]  # Still a lock, whatever the path holds.
    assert samples == []  # Nothing to time: no route is involved.
    assert any("离线资源包缺少" in line for line in logged)


def test_a_failed_offline_install_falls_through_to_the_network(tmp_path: Path, monkeypatch) -> None:
    _offline_pack_holding(tmp_path, monkeypatch, _first_lock_wheel())
    runtime, runs = _ladder_runtime(tmp_path, monkeypatch, proxy=None, fail=1, output="Hash mismatch for torch")

    _install(runtime)

    assert [lock.name for lock, _environment in runs] == ["pylock.win-py312.toml", "pylock.win-py312.toml"]
    assert "local-lock" in str(runs[0][0]) and "local-lock" not in str(runs[1][0])


def test_the_interpreter_comes_from_the_offline_package(tmp_path: Path, monkeypatch) -> None:
    pack = _offline_pack_holding(tmp_path, monkeypatch)
    (pack / "python").mkdir()
    monkeypatch.setattr(http_client, "network_routes", lambda: [NetworkRoute("直连", None)])
    samples: list[str] = []
    monkeypatch.setattr(runtime_install, "ensure_fast_enough", lambda url, stage: samples.append(url))
    attempts: list[dict[str, str]] = []
    monkeypatch.setattr(
        RuntimeEnvironment, "_run", lambda self, command, environment, *, log, should_pause: attempts.append(environment)
    )
    runtime = object.__new__(ProgressRuntimeEnvironment)

    runtime._run(["uv.exe", "python", "install", "3.12"], {}, log=None, should_pause=None)

    assert len(attempts) == 1
    assert attempts[0]["UV_PYTHON_INSTALL_MIRROR"].startswith("file:///")
    assert samples == []


def test_exhausted_dependency_ladder_asks_for_a_proxy(tmp_path: Path, monkeypatch) -> None:
    runtime, runs = _ladder_runtime(tmp_path, monkeypatch, proxy=None, fail=9, output="error sending request")

    with pytest.raises(DownloadUnreachable, match="请开启代理"):
        _install(runtime)

    assert len(runs) == 3
    assert "backup-lock" in str(runs[-1][0])


def test_a_non_network_uv_failure_stops_the_climb(tmp_path: Path, monkeypatch) -> None:
    runtime, runs = _ladder_runtime(tmp_path, monkeypatch, proxy=None, fail=9, output="Hash mismatch for torch")

    with pytest.raises(subprocess.CalledProcessError):
        _install(runtime)

    assert len(runs) == 1


def test_mending_a_tree_can_ask_for_packages_to_be_reinstalled(tmp_path: Path, monkeypatch) -> None:
    """FineSub's `_verify_staging` passes `reinstall=`; the override has to take it."""

    runtime, _runs = _ladder_runtime(tmp_path, monkeypatch, proxy=None, fail=0, output="")
    commands: list[list[str]] = []
    runtime._run = lambda command, environment, *, log, should_pause: commands.append(command)

    runtime._install_dependencies(
        Path("uv.exe"), Path("python.exe"), {}, log=None, should_pause=None, reinstall=["numpy"]
    )

    assert commands[0][-2:] == ["--reinstall-package", "numpy"]


def test_a_slow_rung_is_left_before_uv_starts_on_it(tmp_path: Path, monkeypatch) -> None:
    runtime, runs = _ladder_runtime(tmp_path, monkeypatch, proxy=None, fail=0, output="")
    samples: list[str] = []

    def sample(url: str, stage) -> None:
        samples.append(url)
        if stage.kind == "direct":
            raise DownloadTooSlow("slow")

    monkeypatch.setattr(runtime_install, "ensure_fast_enough", sample)

    _install(runtime)

    assert [lock.name for lock, _environment in runs] == ["pylock.win-py312.cn.toml"]
    assert samples[0].startswith("https://files.pythonhosted.org/")


def test_the_speed_sample_is_a_file_uv_itself_would_fetch() -> None:
    sample = runtime_install._speed_sample(RUNTIME / "pylock.win-py312.toml")

    assert "files.pythonhosted.org" in sample
    assert "torch-" not in sample


def test_interpreter_download_is_timed_before_uv_starts(monkeypatch) -> None:
    monkeypatch.delenv("FINESUB_GITHUB_FILE_PROXY", raising=False)
    monkeypatch.setattr(http_client, "network_routes", lambda: [NetworkRoute("直连", None)])
    samples: list[str] = []

    def sample(url: str, stage) -> None:
        samples.append(url)
        if stage.kind == "direct":
            raise DownloadTooSlow("slow")

    monkeypatch.setattr(runtime_install, "ensure_fast_enough", sample)
    attempts: list[dict[str, str]] = []
    monkeypatch.setattr(
        RuntimeEnvironment, "_run", lambda self, command, environment, *, log, should_pause: attempts.append(environment)
    )
    runtime = object.__new__(ProgressRuntimeEnvironment)

    runtime._run(["uv.exe", "python", "install", "3.12"], {}, log=None, should_pause=None)

    assert len(attempts) == 1
    assert attempts[0]["UV_PYTHON_INSTALL_MIRROR"].startswith("https://gh-proxy.com/")
    assert "python-build-standalone" in samples[0]


def test_interpreter_download_climbs_to_the_github_mirror(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("FINESUB_GITHUB_FILE_PROXY", raising=False)
    monkeypatch.setattr(http_client, "network_routes", lambda: [NetworkRoute("直连", None)])
    monkeypatch.setattr(runtime_install, "ensure_fast_enough", lambda url, stage: None)
    attempts: list[dict[str, str]] = []

    def base_run(self, command, environment, *, log, should_pause) -> None:
        attempts.append(environment)
        if len(attempts) == 1:
            raise subprocess.CalledProcessError(1, command, output="error sending request for url")

    monkeypatch.setattr(RuntimeEnvironment, "_run", base_run)
    runtime = object.__new__(ProgressRuntimeEnvironment)

    runtime._run(["uv.exe", "python", "install", "3.12"], {}, log=None, should_pause=None)

    assert "UV_PYTHON_INSTALL_MIRROR" not in attempts[0]
    assert attempts[1]["UV_PYTHON_INSTALL_MIRROR"].startswith("https://gh-proxy.com/https://github.com/astral-sh/")


def test_interpreter_download_ends_on_our_own_copy(monkeypatch) -> None:
    monkeypatch.delenv("FINESUB_GITHUB_FILE_PROXY", raising=False)
    monkeypatch.setattr(http_client, "network_routes", lambda: [NetworkRoute("直连", None)])
    samples: list[str] = []
    monkeypatch.setattr(runtime_install, "ensure_fast_enough", lambda url, stage: samples.append(url))
    attempts: list[dict[str, str]] = []

    def base_run(self, command, environment, *, log, should_pause) -> None:
        attempts.append(environment)
        if len(attempts) < 3:
            raise subprocess.CalledProcessError(1, command, output="error sending request for url")

    monkeypatch.setattr(RuntimeEnvironment, "_run", base_run)
    runtime = object.__new__(ProgressRuntimeEnvironment)

    runtime._run(["uv.exe", "python", "install", "3.12"], {}, log=None, should_pause=None)

    assert attempts[2]["UV_PYTHON_INSTALL_MIRROR"] == (
        "https://cloudfile.nonoka.online/github-mirror/github.com/astral-sh/python-build-standalone/releases/download"
    )
    # A rung is timed on its own host: the mirror's speed says nothing about GitHub's.
    assert samples[1].startswith("https://gh-proxy.com/")


def test_a_mirror_the_user_chose_for_the_interpreter_gets_no_copy_rungs(monkeypatch) -> None:
    monkeypatch.setattr(http_client, "network_routes", lambda: [NetworkRoute("直连", None)])
    monkeypatch.setattr(runtime_install, "ensure_fast_enough", lambda url, stage: None)
    attempts: list[dict[str, str]] = []

    def base_run(self, command, environment, *, log, should_pause) -> None:
        attempts.append(environment)
        raise subprocess.CalledProcessError(1, command, output="error sending request for url")

    monkeypatch.setattr(RuntimeEnvironment, "_run", base_run)
    runtime = object.__new__(ProgressRuntimeEnvironment)

    with pytest.raises(DownloadUnreachable):
        runtime._run(
            ["uv.exe", "python", "install", "3.12"],
            {"UV_PYTHON_INSTALL_MIRROR": "https://mine.example"},
            log=None,
            should_pause=None,
        )

    assert [environment["UV_PYTHON_INSTALL_MIRROR"] for environment in attempts] == ["https://mine.example"]


def _bare_runtime(tmp_path: Path) -> ProgressRuntimeEnvironment:
    runtime = object.__new__(ProgressRuntimeEnvironment)
    runtime.paths = SimpleNamespace(runtime=tmp_path / "runtime", cache=tmp_path / "cache")
    runtime.development_python = None
    runtime.runtime_root.mkdir(parents=True)
    return runtime


def test_an_outdated_runtime_is_updated_in_place_and_marked_last(tmp_path: Path, monkeypatch) -> None:
    runtime = _bare_runtime(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(runtime_install, "apply_proxy_environment", lambda environment: None)
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
