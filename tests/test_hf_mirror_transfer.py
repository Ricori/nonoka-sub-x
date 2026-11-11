"""How the desktop's Hugging Face model install climbs the ladder, now that there is no mirror.

There used to be an hf-mirror rung, and this file guarded what a mirrored
download needs (Xet off, the CAS 401 blamed on the mirror). Plan 4.2 item 18
removed the mirror (WP-R2); what is left to keep true:

- each rung -- the live proxy, then direct -- runs in its *own process*,
  because `huggingface_hub` reads its route when it is imported;
- neither rung names an endpoint or turns Xet off: both go to the official host;
- a user's own `HF_ENDPOINT` is left in place but loses Xet and the implicit
  token, since such a host does not serve Xet, and their own
  `HF_HUB_DISABLE_XET` -- `0` included -- always wins;
- a failure of this machine (a full disk) is not retried on another rung;
- a crawling rung is abandoned, and the last rung is left to finish.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nonoka_x import download_route  # noqa: E402
from nonoka_x.download_route import DownloadUnreachable, climb_huggingface  # noqa: E402
from nonoka_x.provision import RuntimeProvisionError, RuntimeProvisioner  # noqa: E402
from nonoka_x.runtime import fetch  # noqa: E402


VENDOR = ROOT / "third_party" / "finesub"
PROXY = "http://127.0.0.1:7890"

#: A transport failure as the installer reports it.
NETWORK_DOWN = "httpx.ConnectError: [WinError 10061] tcp connect error"


@pytest.fixture(autouse=True)
def _neutral_download_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing here may depend on the machine running the tests."""

    for name in ("HF_ENDPOINT", "HF_HUB_DISABLE_XET", "HF_HUB_DISABLE_IMPLICIT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    _routes(monkeypatch, None)


def _routes(monkeypatch: pytest.MonkeyPatch, proxy: str | None) -> None:
    routes = ([fetch.NetworkRoute("代理", proxy)] if proxy else []) + [fetch.NetworkRoute("直连", None)]
    monkeypatch.setattr(fetch, "network_routes", lambda: routes)


def test_both_rungs_go_to_the_official_host_with_xet(monkeypatch: pytest.MonkeyPatch) -> None:
    _routes(monkeypatch, PROXY)
    attempts: list[dict[str, str]] = []

    def fetch_model(environment) -> None:
        attempts.append(dict(environment))
        if len(attempts) == 1:
            raise RuntimeError(NETWORK_DOWN)

    climb_huggingface(fetch_model, {}, is_retryable=lambda error: False)

    assert [attempt["HTTPS_PROXY"] for attempt in attempts] == [PROXY, ""]
    for attempt in attempts:
        assert "HF_ENDPOINT" not in attempt
        assert "HF_HUB_DISABLE_XET" not in attempt


def test_a_user_endpoint_is_left_in_place_but_loses_xet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_ENDPOINT", "https://mirror.example")
    attempts: list[dict[str, str]] = []

    climb_huggingface(attempts.append, {})

    assert "HF_ENDPOINT" not in attempts[0]  # Inherited from the process, not rewritten.
    assert attempts[0]["HF_HUB_DISABLE_XET"] == "1"
    assert attempts[0]["HF_HUB_DISABLE_IMPLICIT_TOKEN"] == "1"


def test_an_explicit_xet_setting_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Including the `0` that asks for Xet back."""

    monkeypatch.setenv("HF_ENDPOINT", "https://mirror.example")
    monkeypatch.setenv("HF_HUB_DISABLE_XET", "0")
    attempts: list[dict[str, str]] = []

    climb_huggingface(attempts.append, {})

    assert "HF_HUB_DISABLE_XET" not in attempts[0]


def _provisioner(tmp_path: Path) -> RuntimeProvisioner:
    provisioner = RuntimeProvisioner(tmp_path, VENDOR)
    assert provisioner.paths is not None
    if provisioner.runtime is None:
        # The model installer is never actually started here, and the Windows
        # runtime cannot be built on the platform running the tests.
        provisioner.runtime = object()
    return provisioner


def test_model_install_takes_each_rung_in_its_own_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _routes(monkeypatch, PROXY)
    provisioner = _provisioner(tmp_path)
    attempts: list[dict[str, str]] = []

    def run(model_id: str, environment: dict[str, str]) -> None:
        attempts.append(environment)
        if len(attempts) == 1:
            raise RuntimeProvisionError(NETWORK_DOWN)

    monkeypatch.setattr(provisioner, "_run_model_installer", run)
    provisioner._install_model("whisper")

    assert len(attempts) == 2
    assert attempts[0] is not attempts[1]
    assert [attempt["HTTPS_PROXY"] for attempt in attempts] == [PROXY, ""]
    assert all("HF_ENDPOINT" not in attempt for attempt in attempts)


def test_the_separator_is_left_to_its_own_file_ladder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provisioner = _provisioner(tmp_path)
    attempts: list[dict[str, str]] = []

    def run(model_id: str, environment: dict[str, str]) -> None:
        attempts.append(environment)
        raise RuntimeProvisionError(NETWORK_DOWN)

    monkeypatch.setattr(provisioner, "_run_model_installer", run)
    with pytest.raises(RuntimeProvisionError):
        provisioner._install_model("separator")

    assert len(attempts) == 1


def test_a_local_failure_is_reported_rather_than_retried_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _routes(monkeypatch, PROXY)
    provisioner = _provisioner(tmp_path)
    attempts: list[dict[str, str]] = []

    def run(model_id: str, environment: dict[str, str]) -> None:
        attempts.append(environment)
        raise RuntimeProvisionError("OSError: [Errno 28] No space left on device")

    monkeypatch.setattr(provisioner, "_run_model_installer", run)
    with pytest.raises(RuntimeProvisionError):
        provisioner._install_model("whisper")

    assert len(attempts) == 1


#: The installer's protocol: some bytes, then silence for as long as it lives.
_PROGRESS_LINE = (
    'print(\'{"type":"progress","resource":"whisper","completed":1,"total":1000000000,'
    '"unit":"bytes","bytes_per_second":1.0,"message":"m"}\', flush=True)'
)
_CRAWLING_INSTALLER = f"import time\n{_PROGRESS_LINE}\ntime.sleep(60)\n"
_VERIFYING_INSTALLER = (
    f"import time\n{_PROGRESS_LINE}\n"
    'print(\'{"type":"stage","resource":"whisper","stage":"verifying","message":"m"}\', flush=True)\n'
    "time.sleep(1)\n"
)


def _fake_installer(monkeypatch: pytest.MonkeyPatch, provisioner: RuntimeProvisioner, script: str) -> None:
    real = subprocess.Popen

    def popen(args, **kwargs):
        if list(args[1:3]) == ["-m", "nonoka_x.model_install"]:
            return real([sys.executable, "-c", script], **kwargs)
        return real(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(download_route, "SLOW_WINDOW_SECONDS", 0.3)
    provisioner.runtime = SimpleNamespace(python_executable=sys.executable)


@pytest.fixture
def _watched_rung():
    token = download_route._watching_speed.set(True)
    yield
    download_route._watching_speed.reset(token)


def test_a_crawling_model_download_is_stopped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _watched_rung) -> None:
    provisioner = _provisioner(tmp_path)
    _fake_installer(monkeypatch, provisioner, _CRAWLING_INSTALLER)
    started = time.monotonic()

    with pytest.raises(download_route.DownloadTooSlow):
        provisioner._run_model_installer("whisper", dict(os.environ))

    assert time.monotonic() - started < 30


def test_verifying_a_model_is_not_mistaken_for_a_crawl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _watched_rung
) -> None:
    provisioner = _provisioner(tmp_path)
    _fake_installer(monkeypatch, provisioner, _VERIFYING_INSTALLER)

    provisioner._run_model_installer("whisper", dict(os.environ))


def test_the_last_rung_is_left_to_finish_however_slowly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provisioner = _provisioner(tmp_path)
    _fake_installer(monkeypatch, provisioner, f"import time\n{_PROGRESS_LINE}\ntime.sleep(1)\n")

    provisioner._run_model_installer("whisper", dict(os.environ))


def test_the_ladder_ends_at_the_official_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provisioner = _provisioner(tmp_path)
    attempts: list[dict[str, str]] = []

    def run(model_id: str, environment: dict[str, str]) -> None:
        attempts.append(environment)
        raise RuntimeProvisionError(NETWORK_DOWN)

    monkeypatch.setattr(provisioner, "_run_model_installer", run)
    with pytest.raises(DownloadUnreachable, match="请开启代理"):
        provisioner._install_model("whisper")

    assert len(attempts) == 1
    assert "HF_ENDPOINT" not in attempts[0]
