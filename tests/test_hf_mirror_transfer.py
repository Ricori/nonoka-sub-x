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
- a failure of this machine (a full disk) is not retried on another rung.

The desktop no longer downloads models itself (WP-E3: they come in the model
layer), so the ladder is exercised here directly, as `finesub_bridge` uses it
for the engine's own Hugging Face fetches until WP-R7 removes them. The
crawl tests went with the old model-installer subprocess they timed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nonoka_x import download_route  # noqa: E402
from nonoka_x.download_route import DownloadUnreachable, climb_huggingface  # noqa: E402
from nonoka_x.runtime import fetch  # noqa: E402


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


class InstallFailed(RuntimeError):
    """A rung's failure as a model fetch reports it."""


def _climb(run, *, retryable=None) -> None:
    climb_huggingface(run, {"PATH": "x"}, is_retryable=retryable)


def test_model_fetch_takes_each_rung_in_its_own_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _routes(monkeypatch, PROXY)
    attempts: list[dict[str, str]] = []

    def run(environment: dict[str, str]) -> None:
        attempts.append(environment)
        if len(attempts) == 1:
            raise InstallFailed(NETWORK_DOWN)

    _climb(run)

    assert len(attempts) == 2
    assert attempts[0] is not attempts[1]
    assert [attempt["HTTPS_PROXY"] for attempt in attempts] == [PROXY, ""]
    assert all("HF_ENDPOINT" not in attempt for attempt in attempts)


def test_a_local_failure_is_reported_rather_than_retried_elsewhere(monkeypatch: pytest.MonkeyPatch) -> None:
    _routes(monkeypatch, PROXY)
    attempts: list[dict[str, str]] = []

    def run(environment: dict[str, str]) -> None:
        attempts.append(environment)
        raise InstallFailed("OSError: [Errno 28] No space left on device")

    with pytest.raises(InstallFailed):
        _climb(run, retryable=lambda error: False)

    assert len(attempts) == 1


def test_the_ladder_ends_at_the_official_source(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[dict[str, str]] = []

    def run(environment: dict[str, str]) -> None:
        attempts.append(environment)
        raise InstallFailed(NETWORK_DOWN)

    with pytest.raises(DownloadUnreachable, match="请开启代理"):
        _climb(run)

    assert len(attempts) == 1
    assert "HF_ENDPOINT" not in attempts[0]
