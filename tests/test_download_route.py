from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from finesub_bootstrap import download_routes, http_client
from finesub_bootstrap.http_client import NetworkRoute

from nonoka_x import download_route


@pytest.fixture
def policy(monkeypatch: pytest.MonkeyPatch):
    """Install the policy over a stub upstream that always says `cn`."""

    calls: list[Path] = []

    def upstream(data_root, *args, **kwargs):
        calls.append(data_root)
        return download_routes.RouteDecision(region="cn", source="probe")

    monkeypatch.setattr(download_routes, "resolve_region", upstream)
    monkeypatch.delenv(download_routes.REGION_ENVIRONMENT, raising=False)
    download_route.install_proxy_route_policy()
    return calls


def _routes(monkeypatch: pytest.MonkeyPatch, proxy: str | None) -> None:
    routes = ([NetworkRoute(f"代理 {proxy}", proxy)] if proxy else []) + [NetworkRoute("直连", None)]
    monkeypatch.setattr(http_client, "network_routes", lambda: routes)


def test_live_proxy_routes_to_official_sources(monkeypatch, policy, tmp_path):
    _routes(monkeypatch, "http://127.0.0.1:7890")

    decision = download_routes.resolve_region(tmp_path)

    assert decision.region == "global"
    assert decision.source == "proxy"
    # Not probed and so not cached: the cache only ever describes a direct exit.
    assert policy == []


def test_without_proxy_upstream_decides(monkeypatch, policy, tmp_path):
    _routes(monkeypatch, None)

    decision = download_routes.resolve_region(tmp_path)

    assert decision.region == "cn"
    assert policy == [tmp_path]


def test_explicit_region_beats_proxy(monkeypatch, policy, tmp_path):
    _routes(monkeypatch, "http://127.0.0.1:7890")
    monkeypatch.setenv(download_routes.REGION_ENVIRONMENT, "cn")

    assert download_routes.resolve_region(tmp_path).region == "cn"
    assert policy == [tmp_path]


def test_install_is_idempotent(monkeypatch, policy, tmp_path):
    installed = download_routes.resolve_region
    download_route.install_proxy_route_policy()

    assert download_routes.resolve_region is installed


def test_route_lookup_failure_falls_back_to_upstream(monkeypatch, policy, tmp_path):
    def broken():
        raise OSError("registry unavailable")

    monkeypatch.setattr(http_client, "network_routes", broken)

    assert download_routes.resolve_region(tmp_path).region == "cn"


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


# --- the app's proxy setting -------------------------------------------------

_ENV_NAMES = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy", "NO_PROXY", "no_proxy")


@pytest.fixture
def app_policy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Install the app policy over a stub system proxy and liveness probe.

    Everything it patches is first set through monkeypatch, so teardown puts
    the real functions and the environment back.
    """

    from finesub.llm import http as llm_http

    state = {"system": "http://127.0.0.1:7890", "live": {"http://127.0.0.1:7890"}, "llm": None}
    preferences = tmp_path / "preferences.json"
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(download_route.PREFERENCES_ENVIRONMENT, str(preferences))
    monkeypatch.setattr(download_route, "_custom_cache", None)
    monkeypatch.setattr(http_client, "_windows_proxy", lambda: state["system"])
    monkeypatch.setattr(http_client, "_environment_proxies", http_client._environment_proxies)
    monkeypatch.setattr(http_client, "_local_proxy_is_listening", lambda proxy: proxy in state["live"])
    monkeypatch.setattr(llm_http, "configured_proxy", lambda: state["llm"])

    def save(proxy) -> None:
        preferences.write_text(json.dumps({"schema": 2, "proxy": proxy}), encoding="utf-8")
        # mtime granularity can be coarse; the cache key must not hide a save.
        download_route._custom_cache = None

    save(None)
    download_route.install_app_proxy_policy()
    return state, save, llm_http


def test_app_policy_follows_system_proxy_by_default(app_policy):
    state, _save, llm_http = app_policy

    assert http_client.network_routes()[0].proxy == "http://127.0.0.1:7890"
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert os.environ["NO_PROXY"] == "localhost,127.0.0.1,::1"
    assert llm_http.configured_proxy() == "http://127.0.0.1:7890"


def test_custom_proxy_replaces_system_and_environment(app_policy):
    state, save, llm_http = app_policy
    state["live"].add("socks5://127.0.0.1:7891")
    save({"scheme": "socks5", "host": "127.0.0.1", "port": 7891})

    routes = http_client.network_routes()

    assert [route.proxy for route in routes] == ["socks5://127.0.0.1:7891", None]
    assert os.environ["ALL_PROXY"] == "socks5://127.0.0.1:7891"
    assert llm_http.configured_proxy() == "socks5://127.0.0.1:7891"


def test_dead_custom_proxy_goes_direct_not_system(app_policy):
    state, save, llm_http = app_policy
    save({"scheme": "http", "host": "127.0.0.1", "port": 9999})

    assert http_client.network_routes()[0].proxy is None
    assert "HTTPS_PROXY" not in os.environ
    # Direct is explicit, so registry-reading clients cannot pick the system proxy up.
    assert os.environ["NO_PROXY"] == "*"
    assert llm_http.configured_proxy() == llm_http.DIRECT


def test_child_environment_follows_the_route(app_policy):
    state, save, _llm_http = app_policy
    child = dict(os.environ)
    download_route.apply_proxy_environment(child)
    assert child["HTTPS_PROXY"] == "http://127.0.0.1:7890"

    state["live"].clear()
    child = dict(os.environ)
    download_route.apply_proxy_environment(child)
    assert "HTTPS_PROXY" not in child
    assert child["NO_PROXY"] == "*"


def test_explicit_llm_proxy_in_config_still_wins(app_policy):
    state, _save, llm_http = app_policy
    state["llm"] = "direct"

    assert llm_http.configured_proxy() == "direct"


@pytest.mark.parametrize(
    "entry",
    [None, "x", {"scheme": "ftp", "port": 21}, {"port": 0}, {"port": True}, {"port": "7890"}, {"host": "a/b", "port": 1}],
)
def test_invalid_custom_proxy_is_ignored(entry):
    assert download_route._custom_proxy_url(entry) is None


def test_custom_proxy_url_defaults_and_ipv6():
    assert download_route._custom_proxy_url({"port": 7890}) == "http://127.0.0.1:7890"
    assert download_route._custom_proxy_url({"scheme": "SOCKS5", "host": "::1", "port": 1080}) == "socks5://[::1]:1080"


def test_app_policy_is_inert_outside_the_desktop(monkeypatch):
    monkeypatch.delenv(download_route.PREFERENCES_ENVIRONMENT, raising=False)
    original = http_client._windows_proxy

    download_route.install_app_proxy_policy()

    assert http_client._windows_proxy is original
