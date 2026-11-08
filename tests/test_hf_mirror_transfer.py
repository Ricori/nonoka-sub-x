"""What a mirrored Hugging Face download has to keep doing.

`huggingface_hub` 1.x downloads through Xet by default, and a Hugging Face
mirror does not mirror Xet: the metadata it serves still names the official
`cas-server.xethub.hf.co`, whose CAS rejects the token that came with it. The
metadata request succeeds, so the failure lands at the end of the preparation
step -- `401 Unauthorized` against a host the mirror never proxied, and no
model installed at all on a machine routed to `cn`.

This used to be the contract for
`patches/finesub/0008-hf-mirror-disable-xet.patch`. Upstream 0.5.0 fixed it
itself (`model_fetch.apply_xet_policy`) and the patch is gone; what remains is
a regression guard, because the machine that loses this is one Nonoka X routes
to `cn` and cannot install a single model on.

What has to stay true:

- a mirrored fetch turns Xet off, and an official one does not;
- a user's own `HF_ENDPOINT` gets the same treatment, since it is a mirror
  too, while their own `HF_HUB_DISABLE_XET` -- `0` included -- always wins;
- that 401 counts as a mirror failure, so the next rung of the ladder runs;
- the desktop's model install takes each rung in its *own process*, because
  the endpoint is read when `huggingface_hub` is imported.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from finesub_bootstrap import http_client, model_fetch
from finesub_bootstrap.http_client import NetworkRoute

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nonoka_x import download_route  # noqa: E402
from nonoka_x.download_route import DownloadUnreachable, climb_huggingface  # noqa: E402
from nonoka_x.provision import RuntimeProvisionError, RuntimeProvisioner  # noqa: E402


VENDOR = ROOT / "third_party" / "finesub"

#: What the desktop reported, trimmed to the line that carries the diagnosis.
CAS_401 = (
    "RuntimeError: Task error: File reconstruction error: CAS Client Error: "
    "Request error: HTTP status client error (401 Unauthorized), domain: "
    "https://cas-server.xethub.hf.co/v2/reconstructions/"
    "b5de55e781fd93b7d472c8ca3f7d40d870a3be764f8bb1a9c4a0511c55f9ca5b"
)


@pytest.fixture(autouse=True)
def _neutral_download_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing here may depend on the machine running the tests."""

    for name in ("HF_ENDPOINT", "HF_HUB_DISABLE_XET", "FINESUB_HF_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(http_client, "network_routes", lambda: [NetworkRoute("直连", None)])


def _use_mirror(monkeypatch: pytest.MonkeyPatch, endpoint: str = "https://hf-mirror.com") -> None:
    monkeypatch.setenv("FINESUB_HF_ENDPOINT", endpoint)


def test_a_mirrored_endpoint_comes_with_xet_turned_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_mirror(monkeypatch)
    environment: dict[str, str] = {}

    model_fetch.apply_hf_endpoint(environment, data_root=tmp_path, region="cn")

    assert environment["HF_ENDPOINT"] == "https://hf-mirror.com"
    assert environment["HF_HUB_DISABLE_XET"] == "1"


def test_the_official_endpoint_keeps_xet(tmp_path: Path) -> None:
    environment: dict[str, str] = {}

    model_fetch.apply_hf_endpoint(environment, data_root=tmp_path, region="global")

    assert environment == {}


def test_a_user_endpoint_is_left_in_place_but_still_loses_xet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_ENDPOINT", "https://mirror.example")
    _use_mirror(monkeypatch)
    environment: dict[str, str] = {}

    model_fetch.apply_hf_endpoint(environment, data_root=tmp_path, region="cn")

    assert "HF_ENDPOINT" not in environment
    assert environment["HF_HUB_DISABLE_XET"] == "1"


def test_an_explicit_xet_setting_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Including the `0` that asks for Xet back."""

    monkeypatch.setenv("HF_HUB_DISABLE_XET", "0")
    _use_mirror(monkeypatch)
    environment: dict[str, str] = {}

    model_fetch.apply_hf_endpoint(environment, data_root=tmp_path, region="cn")

    assert environment["HF_ENDPOINT"] == "https://hf-mirror.com"
    assert "HF_HUB_DISABLE_XET" not in environment


def test_the_cas_401_is_blamed_on_the_mirror() -> None:
    assert model_fetch.is_mirror_failure(RuntimeError(CAS_401)) is True


def test_a_full_disk_is_still_not_the_mirrors_fault() -> None:
    assert model_fetch.is_mirror_failure(RuntimeError("OSError: no space left on device")) is False


def test_only_the_mirror_attempt_loses_the_endpoint_and_xet(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_mirror(monkeypatch)
    attempts: list[dict[str, str]] = []

    def fetch(environment) -> None:
        attempts.append(dict(environment))
        if len(attempts) == 1:
            raise RuntimeError(CAS_401)

    climb_huggingface(fetch, {}, is_retryable=model_fetch.is_mirror_failure)

    assert "HF_ENDPOINT" not in attempts[0]
    assert "HF_HUB_DISABLE_XET" not in attempts[0]
    assert attempts[1]["HF_ENDPOINT"] == "https://hf-mirror.com"
    assert attempts[1]["HF_HUB_DISABLE_XET"] == "1"


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
    _use_mirror(monkeypatch)
    provisioner = _provisioner(tmp_path)
    attempts: list[dict[str, str]] = []

    def run(model_id: str, environment: dict[str, str]) -> None:
        attempts.append(environment)
        if len(attempts) == 1:
            raise RuntimeProvisionError(CAS_401)

    monkeypatch.setattr(provisioner, "_run_model_installer", run)
    provisioner._install_model("whisper")

    assert len(attempts) == 2
    assert "HF_ENDPOINT" not in attempts[0]
    assert "HF_HUB_DISABLE_XET" not in attempts[0]
    assert attempts[1]["HF_ENDPOINT"] == "https://hf-mirror.com"
    assert attempts[1]["HF_HUB_DISABLE_XET"] == "1"


def test_a_mirror_never_turns_itself_off_as_the_last_rung(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A degrade counter from the mirror-first days must not remove the rung."""

    _use_mirror(monkeypatch)
    provisioner = _provisioner(tmp_path)
    provisioner.paths.data_root.mkdir(parents=True, exist_ok=True)
    (provisioner.paths.data_root / "download-routes.json").write_text(
        json.dumps({"failures": {model_fetch.RESOURCE_CLASS: 99}}), encoding="utf-8"
    )
    attempts: list[dict[str, str]] = []

    def run(model_id: str, environment: dict[str, str]) -> None:
        attempts.append(environment)
        if len(attempts) == 1:
            raise RuntimeProvisionError(CAS_401)

    monkeypatch.setattr(provisioner, "_run_model_installer", run)
    provisioner._install_model("whisper")

    assert attempts[1]["HF_ENDPOINT"] == "https://hf-mirror.com"


def test_the_separator_is_left_to_its_own_file_ladder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provisioner = _provisioner(tmp_path)
    attempts: list[dict[str, str]] = []

    def run(model_id: str, environment: dict[str, str]) -> None:
        attempts.append(environment)
        raise RuntimeProvisionError(CAS_401)

    monkeypatch.setattr(provisioner, "_run_model_installer", run)
    with pytest.raises(RuntimeProvisionError):
        provisioner._install_model("separator")

    assert len(attempts) == 1


def test_a_local_failure_is_reported_rather_than_retried_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_mirror(monkeypatch)
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


def test_without_a_mirror_the_ladder_ends_at_the_official_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_mirror(monkeypatch, "")
    provisioner = _provisioner(tmp_path)
    attempts: list[dict[str, str]] = []

    def run(model_id: str, environment: dict[str, str]) -> None:
        attempts.append(environment)
        raise RuntimeProvisionError(CAS_401)

    monkeypatch.setattr(provisioner, "_run_model_installer", run)
    with pytest.raises(DownloadUnreachable, match="请开启代理"):
        provisioner._install_model("whisper")

    assert len(attempts) == 1
    assert "HF_ENDPOINT" not in attempts[0]
