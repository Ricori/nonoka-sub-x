from __future__ import annotations

import httpx
import pytest

from finesub_bootstrap import asset_resolve, http_client
from finesub_bootstrap.http_client import NetworkRoute
from finesub_bootstrap.models import ResolvableAsset

from nonoka_x import asset_digest


URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg.zip"
CLOUD = "https://cloud.nonoka.online/releases/github/BtbN/FFmpeg-Builds/latest"
DIGEST = "sha256:" + "a" * 64


@pytest.fixture
def requests(monkeypatch: pytest.MonkeyPatch):
    """Route every client through a stub transport; `handlers` answers per URL."""

    seen: list[str] = []
    handlers: dict[str, httpx.Response | Exception] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        answer = handlers.get(url, httpx.Response(404))
        if isinstance(answer, Exception):
            raise answer
        return answer

    def create_client(route, *, timeout, headers=None):
        return httpx.Client(transport=httpx.MockTransport(handle), headers=headers, timeout=timeout)

    monkeypatch.setattr(http_client, "create_client", create_client)
    monkeypatch.setattr(asset_resolve, "create_client", create_client)
    monkeypatch.setattr(http_client, "network_routes", lambda: [NetworkRoute("直连", None)])
    monkeypatch.setattr(asset_resolve, "network_routes", lambda: [NetworkRoute("直连", None)])
    monkeypatch.setattr(asset_resolve, "_find_asset", asset_resolve._find_asset)
    asset_digest.install_cloud_digest_source()
    return seen, handlers


def _github_answer(digest: str) -> httpx.Response:
    return httpx.Response(200, json={"assets": [{"name": "ffmpeg.zip", "size": 99, "digest": digest}]})


def test_cloud_answer_pins_the_asset_without_asking_github(requests) -> None:
    seen, handlers = requests
    handlers[CLOUD] = httpx.Response(200, json={"assets": [{"name": "ffmpeg.zip", "size": 1234, "digest": DIGEST}]})

    pinned = asset_resolve.resolve_asset(ResolvableAsset(url=URL, digest_from="github-release-api"))

    assert (pinned.size, pinned.sha256) == (1234, "a" * 64)
    assert seen == [CLOUD]


@pytest.mark.parametrize(
    "cloud",
    [
        httpx.ConnectError("refused"),
        httpx.Response(502, json={"detail": "暂时无法从 GitHub 获取发布信息"}),
        httpx.Response(200, json={"assets": [{"name": "other.zip", "size": 1, "digest": DIGEST}]}),
        httpx.Response(200, text="not json"),
    ],
)
def test_without_a_cloud_answer_github_is_asked_as_before(requests, cloud) -> None:
    seen, handlers = requests
    handlers[CLOUD] = cloud
    github = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/tags/latest"
    handlers[github] = _github_answer("sha256:" + "b" * 64)

    pinned = asset_resolve.resolve_asset(ResolvableAsset(url=URL, digest_from="github-release-api"))

    assert (pinned.size, pinned.sha256) == (99, "b" * 64)
    assert seen == [CLOUD, github]


def test_a_bad_cloud_digest_is_still_refused(requests) -> None:
    _, handlers = requests
    handlers[CLOUD] = httpx.Response(200, json={"assets": [{"name": "ffmpeg.zip", "size": 1, "digest": "md5:x"}]})

    with pytest.raises(asset_resolve.AssetResolutionError):
        asset_resolve.resolve_asset(ResolvableAsset(url=URL, digest_from="github-release-api"))


def test_installing_twice_wraps_once(requests) -> None:
    wrapped = asset_resolve._find_asset
    asset_digest.install_cloud_digest_source()
    assert asset_resolve._find_asset is wrapped
