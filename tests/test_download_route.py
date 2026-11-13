"""The download ladder: offline package, live proxy, direct -- and no mirror.

Covers `nonoka_x.download_route` and the FineSub half it installs through
`nonoka_x.finesub_bridge` (FineSub's downloader climbing the same ladder, its
region pinned to `global`, its route discovery and API calls following the app's
proxy setting). Nothing here touches the network: every server is local.
"""

from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading
import time

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finesub_bootstrap import download_routes, downloader, http_client  # noqa: E402
from finesub_bootstrap.models import DownloadAsset  # noqa: E402

from nonoka_x import download_route, finesub_bridge  # noqa: E402
from nonoka_x.download_route import DownloadStage, DownloadTooSlow, DownloadUnreachable, SpeedWatch  # noqa: E402
from nonoka_x.runtime import fetch  # noqa: E402

# Installed before any test stubs a route, so the ladder wraps the real lookup.
download_route.install_proxy_route_policy()

PROXY = "http://127.0.0.1:7890"
GITHUB_URL = "https://github.com/example/repo/releases/download/v1/tool.zip"
SOURCE = Path(__file__).resolve().parents[1] / "src" / "nonoka_x" / "download_route.py"


@pytest.fixture(autouse=True)
def _neutral(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HF_ENDPOINT", "HF_HUB_DISABLE_XET", "HF_HUB_DISABLE_IMPLICIT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(download_route.OFFLINE_PACK_ENVIRONMENT, raising=False)


def _routes(monkeypatch: pytest.MonkeyPatch, proxy: str | None) -> None:
    routes = ([fetch.NetworkRoute(f"代理 {proxy}", proxy)] if proxy else []) + [fetch.NetworkRoute("直连", None)]
    monkeypatch.setattr(fetch, "network_routes", lambda: routes)


# --- no mirror --------------------------------------------------------------------------------------------------


def test_the_ladder_is_the_live_proxy_then_direct(monkeypatch):
    _routes(monkeypatch, PROXY)
    assert download_route.download_stages() == [DownloadStage("proxy", PROXY), DownloadStage("direct")]

    _routes(monkeypatch, None)
    assert download_route.download_stages() == [DownloadStage("direct")]


def test_no_mirror_or_backup_is_left_in_the_ladder():
    source = SOURCE.read_text(encoding="utf-8")

    for gone in ("gh-proxy", "hf-mirror", "cloudfile.nonoka.online", "github_mirror_url", "github_backup_url",
                 "GITHUB_BACKUP", "FINESUB_GITHUB_FILE_PROXY", "has_mirror"):
        assert gone not in source, gone
    assert not hasattr(download_route, "github_url_on")
    assert not hasattr(DownloadStage("direct"), "mirror")


def test_download_route_does_not_import_finesub():
    import ast

    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    imported = {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level == 0
    } | {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert not imported & {"finesub", "finesub_bootstrap"}


# --- the offline package ----------------------------------------------------------------------------------------


def _offline_pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pack = tmp_path / "offline-pack"
    pack.mkdir()
    (pack / "pack.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv(download_route.OFFLINE_PACK_ENVIRONMENT, str(pack))
    return pack


def _packed(pack: Path, payload: bytes, *, digest: str | None = None) -> DownloadAsset:
    sha256 = digest or hashlib.sha256(payload).hexdigest()
    folder = pack / "files" / sha256
    folder.mkdir(parents=True)
    (folder / "tool.zip").write_bytes(payload)
    # Nothing listens on port 9: reaching the network would fail the test.
    return DownloadAsset(url="http://127.0.0.1:9/tool.zip", size=len(payload), sha256=sha256)


def test_a_directory_without_a_manifest_is_not_a_package(tmp_path, monkeypatch):
    monkeypatch.setenv(download_route.OFFLINE_PACK_ENVIRONMENT, str(tmp_path))

    assert download_route.offline_pack() is None


def test_a_packed_file_is_installed_without_the_network(tmp_path, monkeypatch):
    _routes(monkeypatch, None)
    asset = _packed(_offline_pack(tmp_path, monkeypatch), b"packed-bytes")
    reported: list[int] = []

    result = downloader.download_asset(asset, tmp_path / "out" / "tool.zip", lambda value: reported.append(value.total))

    assert result.read_bytes() == b"packed-bytes"
    assert reported and set(reported) == {len(b"packed-bytes")}


def test_wrong_bytes_in_a_package_are_not_accepted(tmp_path, monkeypatch):
    _routes(monkeypatch, None)
    asset = _packed(_offline_pack(tmp_path, monkeypatch), b"tampered", digest="0" * 64)

    with pytest.raises(DownloadUnreachable, match="离线资源包里没有 tool.zip"):
        downloader.download_asset(asset, tmp_path / "out" / "tool.zip", lambda _value: None)


def test_a_file_the_package_lacks_climbs_as_usual(tmp_path, monkeypatch):
    _routes(monkeypatch, None)
    _offline_pack(tmp_path, monkeypatch)
    fake, calls = _recording_download(0, httpx.ConnectError("unused"))

    finesub_bridge._laddered_download(fake)(_asset(), tmp_path / "tool.zip", None, None)

    assert calls == [(GITHUB_URL, None)]


def test_a_pinned_download_still_uses_the_package(tmp_path, monkeypatch):
    _routes(monkeypatch, None)
    asset = _packed(_offline_pack(tmp_path, monkeypatch), b"packed-bytes")
    destination = tmp_path / "out" / "tool.zip"

    with download_route.pinned_stage(DownloadStage("direct")):
        result = downloader.download_asset(asset, destination, lambda _value: None)

    assert result.read_bytes() == b"packed-bytes"


def test_an_unverified_file_comes_from_the_package(tmp_path, monkeypatch):
    _routes(monkeypatch, None)
    pack = _offline_pack(tmp_path, monkeypatch)
    (pack / "unverified").mkdir()
    (pack / "unverified" / "checks.json").write_text("{}", encoding="utf-8")
    destination = tmp_path / "out" / "checks.json"
    calls: list[str] = []

    finesub_bridge._laddered_unverified(lambda url, *args, **kwargs: calls.append(url))(
        "https://raw.githubusercontent.com/a/b/main/checks.json", destination, "", data_root=tmp_path
    )

    assert destination.read_text(encoding="utf-8") == "{}"
    assert calls == []


# --- FineSub's region -------------------------------------------------------------------------------------------


@pytest.fixture
def policy(monkeypatch: pytest.MonkeyPatch):
    """Install the policy over a stub upstream that always says `cn`."""

    calls: list[Path] = []

    def upstream(data_root, *args, **kwargs):
        calls.append(data_root)
        return download_routes.RouteDecision(region="cn", source="probe")

    monkeypatch.setattr(download_routes, "resolve_region", upstream)
    download_route.install_proxy_route_policy()
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
    assert download_route.download_stages() == [DownloadStage("direct")]


def test_install_is_idempotent(monkeypatch, policy, tmp_path):
    installed = download_routes.resolve_region
    locked = downloader._download_locked
    download_route.install_proxy_route_policy()

    assert download_routes.resolve_region is installed
    assert downloader._download_locked is locked


# --- climbing ---------------------------------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_speed_watch_starts_at_the_first_byte_and_judges_a_whole_window():
    clock = _Clock()
    watch = SpeedWatch(clock)

    watch.add(0)
    clock.now += 3600
    watch.check()  # Nothing has arrived yet: that is setup, not a crawl.
    watch.add(1)
    clock.now += download_route.SLOW_WINDOW_SECONDS - 1
    watch.check()  # Part of a window proves nothing.
    clock.now += 1
    with pytest.raises(DownloadTooSlow):
        watch.check()


def test_speed_watch_lets_a_window_at_the_floor_pass_and_starts_the_next():
    clock = _Clock()
    watch = SpeedWatch(clock)
    watch.add(int(download_route.MIN_BYTES_PER_SECOND * download_route.SLOW_WINDOW_SECONDS))
    clock.now += download_route.SLOW_WINDOW_SECONDS
    watch.check()

    clock.now += download_route.SLOW_WINDOW_SECONDS
    with pytest.raises(DownloadTooSlow):
        watch.check()


def test_climb_watches_every_rung_but_the_last():
    watched: list[bool] = []

    def attempt(stage):
        watched.append(download_route.speed_watched())
        raise httpx.ConnectError("refused")

    stages = [DownloadStage("local"), DownloadStage("proxy", PROXY), DownloadStage("direct")]
    with pytest.raises(DownloadUnreachable):
        download_route.climb(stages, attempt, advances=lambda stage, error: True)

    assert watched == [True, True, False]
    assert download_route.speed_watched() is False


def test_a_slow_rung_is_used_again_only_once_the_others_failed():
    order: list[tuple[str, bool]] = []
    announced: list[str] = []

    def attempt(stage):
        order.append((stage.kind, download_route.speed_watched()))
        if download_route.speed_watched():
            raise DownloadTooSlow("slow")
        if stage.kind == "direct":
            raise httpx.ConnectError("direct blocked")
        return "done"

    result = download_route.climb(
        [DownloadStage("proxy", PROXY), DownloadStage("direct")],
        attempt,
        advances=lambda stage, error: True,
        announce=announced.append,
    )

    assert result == "done"
    assert order == [("proxy", True), ("direct", False), ("proxy", False)]
    assert "速度过慢" in announced[0]
    assert "下载失败" in announced[1]


def test_the_last_rung_is_kept_even_if_it_is_slow():
    order: list[str] = []

    def attempt(stage):
        order.append(stage.kind)
        if download_route.speed_watched():
            raise DownloadTooSlow("slow")

    download_route.climb(
        [DownloadStage("proxy", PROXY), DownloadStage("direct")], attempt, advances=lambda stage, error: True
    )

    assert order == ["proxy", "direct"]


def _asset(url: str = GITHUB_URL) -> DownloadAsset:
    return DownloadAsset(url=url, size=4, sha256="0" * 64)


def _recording_download(fail_on: int, error: Exception):
    """A `_download_locked` that fails its first `fail_on` calls and records each."""

    calls: list[tuple[str, str | None]] = []

    def download(asset, destination, progress, should_pause):
        calls.append((asset.url, downloader.network_routes()[0].proxy))
        if len(calls) <= fail_on:
            raise error
        return destination

    return download, calls


def test_file_download_climbs_proxy_then_direct_on_the_same_url(monkeypatch, tmp_path):
    _routes(monkeypatch, PROXY)
    fake, calls = _recording_download(1, httpx.ConnectError("refused"))

    finesub_bridge._laddered_download(fake)(_asset(), tmp_path / "tool.zip", None, None)

    assert calls == [(GITHUB_URL, PROXY), (GITHUB_URL, None)]


def test_file_download_moves_on_after_an_http_error(monkeypatch, tmp_path):
    """FineSub's own loop only moves on for a transport failure."""

    _routes(monkeypatch, PROXY)
    error = httpx.HTTPStatusError("502", request=httpx.Request("GET", GITHUB_URL), response=httpx.Response(502))
    fake, calls = _recording_download(1, error)

    finesub_bridge._laddered_download(fake)(_asset(), tmp_path / "tool.zip", None, None)

    assert [proxy for _url, proxy in calls] == [PROXY, None]


def test_exhausted_ladder_asks_for_a_proxy(monkeypatch, tmp_path):
    _routes(monkeypatch, None)
    fake, calls = _recording_download(9, httpx.ConnectError("refused"))

    with pytest.raises(DownloadUnreachable, match="请开启代理"):
        finesub_bridge._laddered_download(fake)(_asset(), tmp_path / "tool.zip", None, None)

    assert calls == [(GITHUB_URL, None)]


@pytest.mark.parametrize(
    "error",
    [
        downloader.DownloadPaused("paused"),
        downloader.DigestMismatch("wrong bytes from the official source"),
        OSError("no space left on device"),
    ],
)
def test_failures_another_route_cannot_fix_stop_the_climb(monkeypatch, tmp_path, error):
    _routes(monkeypatch, PROXY)
    fake, calls = _recording_download(9, error)

    with pytest.raises(type(error)):
        finesub_bridge._laddered_download(fake)(_asset(), tmp_path / "tool.zip", None, None)

    assert len(calls) == 1


def test_a_download_already_on_a_rung_does_not_climb_again(monkeypatch, tmp_path):
    _routes(monkeypatch, PROXY)
    fake, calls = _recording_download(9, httpx.ConnectError("refused"))

    with download_route.pinned_stage(DownloadStage("direct")), pytest.raises(httpx.ConnectError):
        finesub_bridge._laddered_download(fake)(_asset(), tmp_path / "tool.zip", None, None)

    assert calls == [(GITHUB_URL, None)]


def test_inside_a_pinned_rung_the_live_proxy_is_that_rungs(monkeypatch):
    _routes(monkeypatch, PROXY)
    environment = {"HTTPS_PROXY": "http://stale:1"}

    with download_route.pinned_stage(DownloadStage("direct")):
        assert download_route.live_proxy() is None
        download_route.apply_proxy_environment(environment)

    assert "HTTPS_PROXY" not in environment
    assert download_route.live_proxy() == PROXY


def test_unverified_file_stops_at_the_first_rung_that_wrote_it(monkeypatch, tmp_path):
    _routes(monkeypatch, PROXY)
    destination = tmp_path / "config.yaml"
    calls: list[tuple[str, str | None]] = []

    def fake(url, target, proxy, *, data_root=None):
        calls.append((url, downloader.network_routes()[0].proxy))
        if downloader.network_routes()[0].proxy is None:
            target.write_text("ok", encoding="utf-8")

    finesub_bridge._laddered_unverified(fake)(GITHUB_URL, destination, "", data_root=tmp_path)

    assert destination.read_text(encoding="utf-8") == "ok"
    assert calls == [(GITHUB_URL, PROXY), (GITHUB_URL, None)]


# --- through FineSub's real downloader, against local servers ---------------------------------------------------


class _QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass


def _local_server(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def _drip(handler: BaseHTTPRequestHandler) -> None:
    """A body that arrives a byte at a time, for as long as anyone reads it."""

    handler.send_response(200)
    handler.send_header("Content-Length", "100000")
    handler.end_headers()
    try:
        for _ in range(100000):
            handler.wfile.write(b"x")
            time.sleep(0.02)
    except OSError:
        pass


def _serve(handler: BaseHTTPRequestHandler, payload: bytes) -> None:
    handler.send_response(200)
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def test_real_download_falls_back_from_a_failing_proxy_to_direct(monkeypatch, tmp_path):
    """The proxy answers 502; the file then comes straight from the origin, digest checked."""

    payload = b"nonoka-direct-payload"
    proxied: list[str] = []
    direct: list[str] = []

    class Proxy(_QuietHandler):
        def do_GET(self) -> None:
            proxied.append(self.path)
            self.send_error(502)

    class Origin(_QuietHandler):
        def do_GET(self) -> None:
            direct.append(self.path)
            _serve(self, payload)

    proxy_server, proxy = _local_server(Proxy)
    origin_server, origin = _local_server(Origin)
    url = f"{origin}/tool.zip"
    try:
        _routes(monkeypatch, proxy)
        asset = DownloadAsset(url=url, size=len(payload), sha256=hashlib.sha256(payload).hexdigest())

        result = downloader.download_asset(asset, tmp_path / "tool.zip", lambda _progress: None)
    finally:
        for server in (proxy_server, origin_server):
            server.shutdown()
            server.server_close()

    assert result.read_bytes() == payload
    assert proxied == [url]  # An HTTP proxy is asked for the absolute URL.
    assert direct == ["/tool.zip"]


def test_real_download_leaves_a_crawling_proxy_for_direct(monkeypatch, tmp_path):
    """Noticed at the socket, not per 1 MiB chunk."""

    payload = b"nonoka-direct-payload"

    class Proxy(_QuietHandler):
        def do_GET(self) -> None:
            _drip(self)

    class Origin(_QuietHandler):
        def do_GET(self) -> None:
            _serve(self, payload)

    proxy_server, proxy = _local_server(Proxy)
    origin_server, origin = _local_server(Origin)
    try:
        _routes(monkeypatch, proxy)
        monkeypatch.setattr(download_route, "SLOW_WINDOW_SECONDS", 0.2)
        asset = DownloadAsset(url=f"{origin}/tool.zip", size=len(payload), sha256=hashlib.sha256(payload).hexdigest())

        result = downloader.download_asset(asset, tmp_path / "tool.zip", lambda _progress: None)
    finally:
        for server in (proxy_server, origin_server):
            server.shutdown()
            server.server_close()

    assert result.read_bytes() == payload


# --- children and Hugging Face ----------------------------------------------------------------------------------


def test_stage_environment_pins_a_child():
    through = download_route.stage_environment({"PATH": "x"}, DownloadStage("proxy", PROXY))
    assert through["HTTPS_PROXY"] == PROXY
    assert through["NO_PROXY"] == "localhost,127.0.0.1,::1"
    assert download_route.DIRECT_ENVIRONMENT not in through

    direct = download_route.stage_environment({"HTTPS_PROXY": PROXY}, DownloadStage("direct"))
    assert direct["HTTPS_PROXY"] == ""
    assert direct["NO_PROXY"] == "*"
    assert direct[download_route.DIRECT_ENVIRONMENT] == "1"


def test_huggingface_climbs_with_one_environment_per_rung(monkeypatch):
    _routes(monkeypatch, PROXY)
    attempts: list[dict[str, str]] = []

    def fetch_model(environment) -> None:
        attempts.append(environment)
        if len(attempts) < 2:
            raise RuntimeError("Please check your internet connection")

    download_route.climb_huggingface(fetch_model, {"HF_HOME": "x"}, is_retryable=lambda error: True)

    assert [attempt["HTTPS_PROXY"] for attempt in attempts] == [PROXY, ""]
    for attempt in attempts:
        assert "HF_ENDPOINT" not in attempt
        assert "HF_HUB_DISABLE_XET" not in attempt


def test_a_users_own_hf_endpoint_is_kept_and_loses_xet(monkeypatch):
    _routes(monkeypatch, None)
    monkeypatch.setenv("HF_ENDPOINT", "https://mine.example")
    attempts: list[dict[str, str]] = []

    def fetch_model(environment) -> None:
        attempts.append(environment)
        raise RuntimeError("connection refused")

    with pytest.raises(DownloadUnreachable):
        download_route.climb_huggingface(fetch_model, {}, is_retryable=lambda error: True)

    assert len(attempts) == 1
    assert attempts[0]["HF_HUB_DISABLE_XET"] == "1"
    assert attempts[0]["HF_HUB_DISABLE_IMPLICIT_TOKEN"] == "1"


def test_transport_failures_are_recognised_in_any_locale():
    chinese = "tcp connect error: 由于连接方在一段时间后没有正确答复，连接尝试失败。 (os error 10060)"
    assert download_route.is_transport_failure(RuntimeError(chinese))
    assert not download_route.is_transport_failure(RuntimeError("磁盘空间不足。 (os error 112): tcp connect"))
    assert not download_route.is_transport_failure(RuntimeError("Hash mismatch for torch"))


def test_the_local_failure_words_are_finesubs():
    from finesub_bootstrap.model_fetch import LOCAL_FAILURE_MARKERS

    assert download_route.LOCAL_FAILURE_MARKERS == LOCAL_FAILURE_MARKERS


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


# --- the app's proxy setting ------------------------------------------------------------------------------------

_ENV_NAMES = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy", "NO_PROXY", "no_proxy")


@pytest.fixture
def app_policy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Install the app policy over a stub system proxy and liveness probe.

    Everything it patches is first set through monkeypatch, so teardown puts
    the real functions, the policy state and the environment back.
    """

    from finesub import api_http

    state = {"system": "http://127.0.0.1:7890", "live": {"http://127.0.0.1:7890"}, "llm": None}
    preferences = tmp_path / "preferences.json"
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(download_route.PREFERENCES_ENVIRONMENT, str(preferences))
    monkeypatch.setattr(fetch, "_custom_cache", None)
    monkeypatch.setattr(fetch, "_ambient", None)
    monkeypatch.setattr(fetch, "windows_proxy", lambda: state["system"])
    monkeypatch.setattr(fetch, "local_proxy_is_listening", lambda proxy: proxy in state["live"])
    monkeypatch.setattr(http_client, "_windows_proxy", http_client._windows_proxy)
    monkeypatch.setattr(http_client, "_environment_proxies", http_client._environment_proxies)
    monkeypatch.setattr(http_client, "_local_proxy_is_listening", lambda proxy: proxy in state["live"])
    monkeypatch.setattr(api_http, "configured_proxy", lambda: state["llm"])

    def save(proxy) -> None:
        preferences.write_text(json.dumps({"schema": 2, "proxy": proxy}), encoding="utf-8")
        # mtime granularity can be coarse; the cache key must not hide a save.
        fetch._custom_cache = None

    save(None)
    download_route.install_app_proxy_policy()
    return state, save, api_http


def _finesub_routes() -> list[str | None]:
    """FineSub's own route lookup under the bridge's patches.

    The ladder wraps `http_client.network_routes`; outside a pinned rung the
    wrapper hands over to FineSub's original lookup, which reads the patched
    system and environment proxies -- it must agree with Nonoka's.
    """

    return [route.proxy for route in http_client.network_routes()]


def test_app_policy_follows_system_proxy_by_default(app_policy):
    _state, _save, api_http = app_policy

    assert fetch.network_routes()[0].proxy == "http://127.0.0.1:7890"
    assert _finesub_routes()[0] == "http://127.0.0.1:7890"
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert os.environ["NO_PROXY"] == "localhost,127.0.0.1,::1"
    assert api_http.configured_proxy() == "http://127.0.0.1:7890"


def test_custom_proxy_replaces_system_and_environment(app_policy):
    state, save, api_http = app_policy
    state["live"].add("socks5://127.0.0.1:7891")
    save({"scheme": "socks5", "host": "127.0.0.1", "port": 7891})

    assert [route.proxy for route in fetch.network_routes()] == ["socks5://127.0.0.1:7891", None]
    assert _finesub_routes() == ["socks5://127.0.0.1:7891", None]
    assert os.environ["ALL_PROXY"] == "socks5://127.0.0.1:7891"
    assert api_http.configured_proxy() == api_http.DIRECT


def test_dead_custom_proxy_goes_direct_not_system(app_policy):
    _state, save, api_http = app_policy
    save({"scheme": "http", "host": "127.0.0.1", "port": 9999})

    assert fetch.network_routes()[0].proxy is None
    assert _finesub_routes()[0] is None
    assert "HTTPS_PROXY" not in os.environ
    # Direct is explicit, so registry-reading clients cannot pick the system proxy up.
    assert os.environ["NO_PROXY"] == "*"
    assert api_http.configured_proxy() == api_http.DIRECT


def test_child_environment_follows_the_route(app_policy):
    state, _save, _api_http = app_policy
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
    assert _finesub_routes()[0] is None
    assert "HTTPS_PROXY" not in os.environ
    assert os.environ["NO_PROXY"] == "*"


def test_explicit_llm_proxy_in_config_still_wins(app_policy):
    state, _save, api_http = app_policy
    state["llm"] = "direct"

    assert api_http.configured_proxy() == "direct"


def test_the_app_policy_is_installed_once(app_policy):
    patched = http_client._environment_proxies
    download_route.install_app_proxy_policy()

    assert http_client._environment_proxies is patched


def test_app_policy_is_inert_outside_the_desktop(monkeypatch):
    monkeypatch.delenv(download_route.PREFERENCES_ENVIRONMENT, raising=False)
    monkeypatch.setattr(fetch, "_ambient", None)
    original = http_client._windows_proxy

    download_route.install_app_proxy_policy()

    assert http_client._windows_proxy is original
    assert not fetch.app_policy_installed()
