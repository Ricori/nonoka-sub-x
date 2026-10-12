from __future__ import annotations

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
