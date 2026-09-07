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
- that 401 counts as a mirror failure, so the fallback to the official source
  runs and the mirror is recorded as having failed;
- the desktop's model install takes that fallback in a *second process*,
  because the endpoint is read when `huggingface_hub` is imported.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from finesub_bootstrap import model_fetch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

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
    monkeypatch.setenv("FINESUB_DOWNLOAD_REGION", "global")


def _use_mirror(monkeypatch: pytest.MonkeyPatch, endpoint: str = "https://hf-mirror.com") -> None:
    monkeypatch.setenv("FINESUB_DOWNLOAD_REGION", "cn")
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


def test_the_official_attempt_gets_both_the_endpoint_and_xet_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_mirror(monkeypatch)
    attempts: list[dict[str, str]] = []

    def fetch(environment) -> None:
        attempts.append(dict(environment))
        if len(attempts) == 1:
            raise RuntimeError(CAS_401)

    model_fetch.fetch_with_fallback(
        fetch,
        base_environment={},
        data_root=tmp_path,
        region="cn",
        is_retryable=model_fetch.is_mirror_failure,
    )

    assert attempts[0] == {"HF_ENDPOINT": "https://hf-mirror.com", "HF_HUB_DISABLE_XET": "1"}
    assert attempts[1] == {}


def _provisioner(tmp_path: Path) -> RuntimeProvisioner:
    provisioner = RuntimeProvisioner(tmp_path, VENDOR)
    assert provisioner.paths is not None
    if provisioner.runtime is None:
        # The model installer is never actually started here, and the Windows
        # runtime cannot be built on the platform running the tests.
        provisioner.runtime = object()
    return provisioner


def test_model_install_retries_the_official_source_in_a_second_process(
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
    assert attempts[0]["HF_ENDPOINT"] == "https://hf-mirror.com"
    assert attempts[0]["HF_HUB_DISABLE_XET"] == "1"
    assert "HF_ENDPOINT" not in attempts[1]
    assert "HF_HUB_DISABLE_XET" not in attempts[1]
    routes = json.loads(
        (provisioner.paths.data_root / "download-routes.json").read_text(encoding="utf-8")
    )
    assert routes["failures"][model_fetch.RESOURCE_CLASS] == 1


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


def test_without_a_mirror_there_is_only_one_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provisioner = _provisioner(tmp_path)
    attempts: list[dict[str, str]] = []

    def run(model_id: str, environment: dict[str, str]) -> None:
        attempts.append(environment)
        raise RuntimeProvisionError(CAS_401)

    monkeypatch.setattr(provisioner, "_run_model_installer", run)
    with pytest.raises(RuntimeProvisionError):
        provisioner._install_model("whisper")

    assert len(attempts) == 1
    assert "HF_ENDPOINT" not in attempts[0]
