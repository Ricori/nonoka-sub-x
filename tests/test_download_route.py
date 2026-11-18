"""The app's route: the proxy setting, no mirror, and FineSub's region pinned to `global`.

Covers `nonoka_x.download_route` (this process's route, a child's
environment, the launch context's proxy) and, since WP-P1, the FineSub half
each engine package's bridge installs in its own process
(`finesub_correct.bridge`: FineSub's region answer pinned without a probe,
its route discovery and API calls taking the route the core resolved). The
engine downloads nothing any more (WP-R7), so nothing of its downloader is
patched. Nothing here touches the network.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finesub_bootstrap import download_routes, downloader, http_client, model_fetch  # noqa: E402
from finesub_correct import bridge  # noqa: E402

from nonoka_x import download_route, engine_contexts  # noqa: E402
from nonoka_x.runtime import fetch  # noqa: E402

PROXY = "http://127.0.0.1:7890"
SOURCE = Path(__file__).resolve().parents[1] / "src" / "nonoka_x" / "download_route.py"


def _routes(monkeypatch: pytest.MonkeyPatch, proxy: str | None) -> None:
    routes = ([fetch.NetworkRoute(f"代理 {proxy}", proxy)] if proxy else []) + [fetch.NetworkRoute("直连", None)]
    monkeypatch.setattr(fetch, "network_routes", lambda: routes)


# --- no mirror --------------------------------------------------------------------------------------------------


def test_no_mirror_or_backup_is_left():
    source = SOURCE.read_text(encoding="utf-8")

    for gone in ("gh-proxy", "hf-mirror", "cloudfile.nonoka.online", "github_mirror_url", "github_backup_url",
                 "GITHUB_BACKUP", "FINESUB_GITHUB_FILE_PROXY", "has_mirror"):
        assert gone not in source, gone
    assert not hasattr(download_route, "github_url_on")


def test_the_engines_downloader_is_left_alone():
    """WP-R7: no engine download is left to route, so nothing of FineSub's downloader is patched."""

    locked, routes = downloader._download_locked, http_client.network_routes
    bridge.install_region_policy()

    assert downloader._download_locked is locked
    assert http_client.network_routes is routes
    assert not hasattr(model_fetch, "fetch_with_fallback")
    assert not hasattr(model_fetch, "_fetch_unverified")
    for gone in ("climb", "download_stages", "offline_file", "climb_huggingface", "SpeedWatch"):
        assert not hasattr(download_route, gone), gone


def test_download_route_does_not_import_finesub():
    import ast

    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    imported = {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level == 0
    } | {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert not imported & {"finesub", "finesub_bootstrap"}


# --- FineSub's region -------------------------------------------------------------------------------------------


@pytest.fixture
def policy(monkeypatch: pytest.MonkeyPatch):
    """Install the policy over a stub upstream that always says `cn`."""

    calls: list[Path] = []

    def upstream(data_root, *args, **kwargs):
        calls.append(data_root)
        return download_routes.RouteDecision(region="cn", source="probe")

    monkeypatch.setattr(download_routes, "resolve_region", upstream)
    bridge.install_region_policy()
    return calls


@pytest.mark.parametrize("proxy", [PROXY, None])
def test_no_engine_call_site_goes_mirror_first(monkeypatch, policy, tmp_path, proxy):
    _routes(monkeypatch, proxy)

    decision = download_routes.resolve_region(tmp_path)

    assert decision.region == "global"
    assert decision.source == "ladder"
    assert policy == []


@pytest.mark.parametrize("region", ["cn", "global"])
def test_the_region_variable_is_not_honoured(monkeypatch, policy, tmp_path, region):
    _routes(monkeypatch, None)
    monkeypatch.setenv(download_routes.REGION_ENVIRONMENT, region)

    assert download_routes.resolve_region(tmp_path).region == "global"
    assert policy == []


def test_install_is_idempotent(monkeypatch, policy, tmp_path):
    installed = download_routes.resolve_region
    bridge.install_region_policy()

    assert download_routes.resolve_region is installed


def test_proxy_label_names_local_and_remote_proxies():
    assert download_route.proxy_label(None) == ""
    assert download_route.proxy_label("http://127.0.0.1:7890") == "本地代理 127.0.0.1:7890"
    assert download_route.proxy_label("socks5://user:secret@proxy.example.com:1080") == "代理 proxy.example.com:1080"


def test_child_environment_carries_the_live_proxy(monkeypatch):
    _routes(monkeypatch, "http://127.0.0.1:7890")
    environment = {"HTTP_PROXY": "http://stale:1"}

    download_route.apply_proxy_environment(environment)

    assert environment["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert environment["HTTP_PROXY"] == "http://127.0.0.1:7890"


def test_child_environment_drops_proxy_variables_when_direct(monkeypatch):
    _routes(monkeypatch, None)
    environment = {"HTTPS_PROXY": "http://127.0.0.1:1"}

    download_route.apply_proxy_environment(environment)

    assert "HTTPS_PROXY" not in environment


# --- the app's proxy setting, in this process ---------------------------------------------------------------------

_ENV_NAMES = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy", "NO_PROXY", "no_proxy")


@pytest.fixture
def app_policy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Install the app policy over a stub system proxy and liveness probe.

    Everything it patches is first set through monkeypatch, so teardown puts
    the real functions, the policy state and the environment back.
    """

    state = {"system": "http://127.0.0.1:7890", "live": {"http://127.0.0.1:7890"}}
    preferences = tmp_path / "preferences.json"
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(download_route.PREFERENCES_ENVIRONMENT, str(preferences))
    monkeypatch.setattr(fetch, "_custom_cache", None)
    monkeypatch.setattr(fetch, "_ambient", None)
    monkeypatch.setattr(fetch, "windows_proxy", lambda: state["system"])
    monkeypatch.setattr(fetch, "local_proxy_is_listening", lambda proxy: proxy in state["live"])

    def save(proxy) -> None:
        preferences.write_text(json.dumps({"schema": 2, "proxy": proxy}), encoding="utf-8")
        # mtime granularity can be coarse; the cache key must not hide a save.
        fetch._custom_cache = None

    save(None)
    download_route.install_app_proxy_policy()
    return state, save


def test_app_policy_follows_system_proxy_by_default(app_policy):
    assert fetch.network_routes()[0].proxy == "http://127.0.0.1:7890"
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert os.environ["NO_PROXY"] == "localhost,127.0.0.1,::1"
    assert engine_contexts.proxy_context() == {"policy": True, "url": "http://127.0.0.1:7890"}


def test_custom_proxy_replaces_system_and_environment(app_policy):
    state, save = app_policy
    state["live"].add("socks5://127.0.0.1:7891")
    save({"scheme": "socks5", "host": "127.0.0.1", "port": 7891})

    assert [route.proxy for route in fetch.network_routes()] == ["socks5://127.0.0.1:7891", None]
    assert os.environ["ALL_PROXY"] == "socks5://127.0.0.1:7891"
    assert engine_contexts.proxy_context() == {"policy": True, "url": "socks5://127.0.0.1:7891"}


def test_dead_custom_proxy_goes_direct_not_system(app_policy):
    _state, save = app_policy
    save({"scheme": "http", "host": "127.0.0.1", "port": 9999})

    assert fetch.network_routes()[0].proxy is None
    assert "HTTPS_PROXY" not in os.environ
    # Direct is explicit, so registry-reading clients cannot pick the system proxy up.
    assert os.environ["NO_PROXY"] == "*"
    assert engine_contexts.proxy_context() == {"policy": True, "url": None}


def test_child_environment_follows_the_route(app_policy):
    state, _save = app_policy
    child = dict(os.environ)
    download_route.apply_proxy_environment(child)
    assert child["HTTPS_PROXY"] == "http://127.0.0.1:7890"

    state["live"].clear()
    child = dict(os.environ)
    download_route.apply_proxy_environment(child)
    assert "HTTPS_PROXY" not in child
    assert child["NO_PROXY"] == "*"


def test_a_child_pinned_direct_does_not_pick_the_proxy_back_up(app_policy, monkeypatch):
    monkeypatch.setenv(download_route.DIRECT_ENVIRONMENT, "1")

    assert fetch.network_routes()[0].proxy is None
    assert "HTTPS_PROXY" not in os.environ
    assert os.environ["NO_PROXY"] == "*"


def test_app_policy_is_inert_outside_the_desktop(monkeypatch):
    monkeypatch.delenv(download_route.PREFERENCES_ENVIRONMENT, raising=False)
    monkeypatch.setattr(fetch, "_ambient", None)

    download_route.install_app_proxy_policy()

    assert not fetch.app_policy_installed()
    assert engine_contexts.proxy_context() == {"policy": False, "url": None}


# --- in an engine package's process: FineSub takes the route the core resolved ------------------------------


@pytest.fixture
def engine_policy(monkeypatch: pytest.MonkeyPatch):
    """What the package's bridge patches, put back afterwards."""

    from finesub import api_http

    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(http_client, "_windows_proxy", lambda: "http://system.example:8080")
    monkeypatch.setattr(http_client, "_environment_proxies", lambda: ["http://ambient.example:3128"])
    monkeypatch.setattr(http_client, "_local_proxy_is_listening", lambda proxy: True)
    state = {"llm": None}
    monkeypatch.setattr(api_http, "configured_proxy", lambda: state["llm"])
    return api_http, state


def _finesub_routes() -> list[str | None]:
    """FineSub's own route lookup under the bridge's patches."""

    return [route.proxy for route in http_client.network_routes()]


def test_finesub_takes_the_resolved_proxy(engine_policy):
    api_http, _state = engine_policy
    bridge.install_proxy_policy({"policy": True, "url": PROXY})

    assert _finesub_routes()[0] == PROXY
    assert os.environ["HTTPS_PROXY"] == PROXY
    assert os.environ["NO_PROXY"] == "localhost,127.0.0.1,::1"
    assert api_http.configured_proxy() == PROXY


def test_a_socks_route_sends_the_api_calls_direct(engine_policy):
    api_http, _state = engine_policy
    bridge.install_proxy_policy({"policy": True, "url": "socks5://127.0.0.1:7891"})

    assert _finesub_routes()[0] == "socks5://127.0.0.1:7891"
    assert os.environ["ALL_PROXY"] == "socks5://127.0.0.1:7891"
    assert api_http.configured_proxy() == api_http.DIRECT


def test_direct_is_direct_for_finesub_too(engine_policy):
    api_http, _state = engine_policy
    bridge.install_proxy_policy({"policy": True, "url": None})

    assert _finesub_routes()[0] is None
    assert "HTTPS_PROXY" not in os.environ
    assert os.environ["NO_PROXY"] == "*"
    assert api_http.configured_proxy() == api_http.DIRECT


def test_explicit_llm_proxy_in_config_still_wins(engine_policy):
    api_http, state = engine_policy
    bridge.install_proxy_policy({"policy": True, "url": PROXY})
    state["llm"] = "direct"

    assert api_http.configured_proxy() == "direct"


def test_the_engine_policy_is_installed_once(engine_policy):
    bridge.install_proxy_policy({"policy": True, "url": PROXY})
    patched = http_client._environment_proxies
    bridge.install_proxy_policy({"policy": True, "url": "http://other:1"})

    assert http_client._environment_proxies is patched


def test_without_the_app_policy_finesub_goes_direct(engine_policy):
    # WP-H (D17, plan 4.5 item 30): outside the desktop's policy the route is
    # direct, spelled out -- never the Windows system proxy FineSub would find.
    api_http, _state = engine_policy
    bridge.install_proxy_policy({"policy": False, "url": PROXY})

    assert http_client._windows_proxy() is None
    assert http_client._environment_proxies() == []
    assert api_http.configured_proxy() == api_http.DIRECT
