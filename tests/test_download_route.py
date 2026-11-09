from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time

import httpx
import pytest

from finesub_bootstrap import download_routes, downloader, http_client
from finesub_bootstrap.http_client import NetworkRoute
from finesub_bootstrap.models import DownloadAsset

from nonoka_x import download_route
from nonoka_x.download_route import DownloadStage, DownloadTooSlow, DownloadUnreachable, SpeedWatch

# Installed before any test stubs `network_routes`, so the ladder wraps the real one.
download_route.install_proxy_route_policy()

PROXY = "http://127.0.0.1:7890"
GITHUB_URL = "https://github.com/example/repo/releases/download/v1/tool.zip"


@pytest.fixture(autouse=True)
def _no_mirror_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FINESUB_GITHUB_FILE_PROXY", raising=False)
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    monkeypatch.delenv("HF_HUB_DISABLE_XET", raising=False)
    monkeypatch.delenv("FINESUB_HF_ENDPOINT", raising=False)


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


def _routes(monkeypatch: pytest.MonkeyPatch, proxy: str | None) -> None:
    routes = ([NetworkRoute(f"代理 {proxy}", proxy)] if proxy else []) + [NetworkRoute("直连", None)]
    monkeypatch.setattr(http_client, "network_routes", lambda: routes)


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
    assert download_route.download_stages(has_mirror=True) == [DownloadStage("direct"), DownloadStage("mirror")]


def test_install_is_idempotent(monkeypatch, policy, tmp_path):
    installed = download_routes.resolve_region
    download_route.install_proxy_route_policy()

    assert download_routes.resolve_region is installed


def test_ladder_order_with_and_without_a_proxy(monkeypatch):
    _routes(monkeypatch, PROXY)
    assert download_route.download_stages(has_mirror=True) == [
        DownloadStage("proxy", PROXY),
        DownloadStage("direct"),
        DownloadStage("mirror"),
    ]

    _routes(monkeypatch, None)
    assert download_route.download_stages(has_mirror=True) == [DownloadStage("direct"), DownloadStage("mirror")]
    assert download_route.download_stages(has_mirror=False) == [DownloadStage("direct")]
    assert download_route.download_stages(has_mirror=True, has_backup=True) == [
        DownloadStage("direct"),
        DownloadStage("mirror"),
        DownloadStage("backup"),
    ]


def test_github_files_end_on_our_own_copy(monkeypatch):
    _routes(monkeypatch, None)
    backup = "https://cloudfile.nonoka.online/github-mirror/github.com/example/repo/releases/download/v1/tool.zip"

    assert download_route.github_backup_url(GITHUB_URL) == backup
    assert download_route.github_backup_url("https://nodejs.org/dist/node.zip") is None
    assert download_route.github_stages(GITHUB_URL)[-1] == DownloadStage("backup")
    assert download_route.github_stages("https://nodejs.org/dist/node.zip") == [DownloadStage("direct")]

    # Emptying the third-party mirror leaves ours.
    monkeypatch.setenv("FINESUB_GITHUB_FILE_PROXY", "")
    assert download_route.github_stages(GITHUB_URL) == [DownloadStage("direct"), DownloadStage("backup")]


def test_the_backup_host_matches_the_launcher():
    source = Path(__file__).resolve().parents[1] / "desktop" / "internal" / "app" / "python_bootstrap.go"

    assert f'bootstrapGitHubBackup  = "{download_route.GITHUB_BACKUP}"' in source.read_text(encoding="utf-8")


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

    stages = [DownloadStage("proxy", PROXY), DownloadStage("direct"), DownloadStage("mirror")]
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
        if stage.mirror:
            raise httpx.ConnectError("mirror down")
        return "done"

    result = download_route.climb(
        [DownloadStage("direct"), DownloadStage("mirror")],
        attempt,
        advances=lambda stage, error: True,
        announce=announced.append,
    )

    assert result == "done"
    assert order == [("direct", True), ("mirror", False), ("direct", False)]
    assert "速度过慢" in announced[0]
    assert "下载失败" in announced[1]


def test_a_mirror_that_works_is_kept_even_if_it_is_slow():
    """The last rung is unwatched, so a slow mirror beats going back."""

    order: list[str] = []

    def attempt(stage):
        order.append(stage.kind)
        if download_route.speed_watched():
            raise DownloadTooSlow("slow")

    download_route.climb([DownloadStage("direct"), DownloadStage("mirror")], attempt, advances=lambda stage, error: True)

    assert order == ["direct", "mirror"]


def test_github_mirror_only_covers_github(monkeypatch):
    assert download_route.github_mirror_url(GITHUB_URL) == f"https://gh-proxy.com/{GITHUB_URL}"
    assert download_route.github_mirror_url("https://nodejs.org/dist/node.zip") is None

    monkeypatch.setenv("FINESUB_GITHUB_FILE_PROXY", "https://mine.example")
    assert download_route.github_mirror_url(GITHUB_URL) == f"https://mine.example/{GITHUB_URL}"
    monkeypatch.setenv("FINESUB_GITHUB_FILE_PROXY", "")
    assert download_route.github_mirror_url(GITHUB_URL) is None


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


def test_file_download_climbs_proxy_direct_mirror(monkeypatch, tmp_path):
    _routes(monkeypatch, PROXY)
    fake, calls = _recording_download(2, httpx.ConnectError("refused"))

    download_route._laddered_download(fake)(_asset(), tmp_path / "tool.zip", None, None)

    assert calls == [
        (GITHUB_URL, PROXY),
        (GITHUB_URL, None),
        (f"https://gh-proxy.com/{GITHUB_URL}", None),
    ]


def test_file_download_moves_on_after_an_http_error(monkeypatch, tmp_path):
    """FineSub's own loop only moves on for a transport failure."""

    _routes(monkeypatch, PROXY)
    error = httpx.HTTPStatusError("502", request=httpx.Request("GET", GITHUB_URL), response=httpx.Response(502))
    fake, calls = _recording_download(1, error)

    download_route._laddered_download(fake)(_asset(), tmp_path / "tool.zip", None, None)

    assert [proxy for _url, proxy in calls] == [PROXY, None]


def test_exhausted_ladder_asks_for_a_proxy(monkeypatch, tmp_path):
    _routes(monkeypatch, None)
    fake, calls = _recording_download(9, httpx.ConnectError("refused"))

    with pytest.raises(download_route.DownloadUnreachable, match="请开启代理"):
        download_route._laddered_download(fake)(_asset(), tmp_path / "tool.zip", None, None)

    assert [url for url, _proxy in calls] == [
        GITHUB_URL,
        f"https://gh-proxy.com/{GITHUB_URL}",
        download_route.github_backup_url(GITHUB_URL),
    ]


def test_a_host_without_a_mirror_skips_that_rung(monkeypatch, tmp_path):
    _routes(monkeypatch, None)
    fake, calls = _recording_download(9, httpx.ConnectError("refused"))

    with pytest.raises(download_route.DownloadUnreachable):
        download_route._laddered_download(fake)(_asset("https://nodejs.org/n.zip"), tmp_path / "n.zip", None, None)

    assert [url for url, _proxy in calls] == ["https://nodejs.org/n.zip"]


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
        download_route._laddered_download(fake)(_asset(), tmp_path / "tool.zip", None, None)

    assert len(calls) == 1


def test_a_download_already_on_a_rung_does_not_climb_again(monkeypatch, tmp_path):
    _routes(monkeypatch, PROXY)
    fake, calls = _recording_download(9, httpx.ConnectError("refused"))

    with download_route.pinned_stage(DownloadStage("direct")), pytest.raises(httpx.ConnectError):
        download_route._laddered_download(fake)(_asset(), tmp_path / "tool.zip", None, None)

    assert calls == [(GITHUB_URL, None)]


def test_real_download_reaches_the_mirror_after_the_official_source_fails(monkeypatch, tmp_path):
    """Through FineSub's real downloader, digest check included."""

    payload = b"nonoka-mirror-payload"
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            if not self.path.startswith("/mirror/"):
                self.send_error(503)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    official = f"{base}/official/tool.zip"
    try:
        _routes(monkeypatch, None)
        monkeypatch.setattr(download_route, "_GITHUB_HOSTS", (base,))
        monkeypatch.setenv("FINESUB_GITHUB_FILE_PROXY", f"{base}/mirror")
        asset = DownloadAsset(url=official, size=len(payload), sha256=hashlib.sha256(payload).hexdigest())

        result = downloader.download_asset(asset, tmp_path / "tool.zip", lambda _progress: None)
    finally:
        server.shutdown()
        server.server_close()

    assert result.read_bytes() == payload
    assert requests == ["/official/tool.zip", f"/mirror/{official}"]


def _local_server(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


class _QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass


class _DripHandler(_QuietHandler):
    """A body that arrives a byte at a time, for as long as anyone reads it."""

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "100000")
        self.end_headers()
        try:
            for _ in range(100000):
                self.wfile.write(b"x")
                time.sleep(0.02)
        except OSError:
            pass


@pytest.fixture
def watched():
    token = download_route._watching_speed.set(True)
    yield
    download_route._watching_speed.reset(token)


def test_real_download_leaves_a_crawling_source_for_the_mirror(monkeypatch, tmp_path):
    """Through FineSub's real downloader: noticed at the socket, not per 1 MiB chunk."""

    payload = b"nonoka-mirror-payload"
    requests: list[str] = []

    class Handler(_DripHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            if not self.path.startswith("/mirror/"):
                super().do_GET()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server, base = _local_server(Handler)
    official = f"{base}/official/tool.zip"
    try:
        _routes(monkeypatch, None)
        monkeypatch.setattr(download_route, "SLOW_WINDOW_SECONDS", 0.2)
        monkeypatch.setattr(download_route, "_GITHUB_HOSTS", (base,))
        monkeypatch.setenv("FINESUB_GITHUB_FILE_PROXY", f"{base}/mirror")
        asset = DownloadAsset(url=official, size=len(payload), sha256=hashlib.sha256(payload).hexdigest())

        result = downloader.download_asset(asset, tmp_path / "tool.zip", lambda _progress: None)
    finally:
        server.shutdown()
        server.server_close()

    assert result.read_bytes() == payload
    assert requests == ["/official/tool.zip", f"/mirror/{official}"]


def test_the_probe_notices_a_crawl(monkeypatch, watched):
    server, base = _local_server(_DripHandler)
    monkeypatch.setattr(download_route, "PROBE_SECONDS", 0.2)
    try:
        with pytest.raises(DownloadTooSlow):
            download_route.ensure_fast_enough(f"{base}/sample", DownloadStage("direct"))
    finally:
        server.shutdown()
        server.server_close()


def test_the_probe_passes_a_fast_route(monkeypatch, watched):
    body = b"x" * (2 * 1024 * 1024)

    class Handler(_QuietHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass

    server, base = _local_server(Handler)
    monkeypatch.setattr(download_route, "PROBE_SECONDS", 1.0)
    try:
        download_route.ensure_fast_enough(f"{base}/sample", DownloadStage("direct"))
    finally:
        server.shutdown()
        server.server_close()


def test_the_probe_does_not_blame_a_rung_for_an_http_error(watched):
    class Handler(_QuietHandler):
        def do_GET(self) -> None:
            self.send_error(403)

    server, base = _local_server(Handler)
    try:
        download_route.ensure_fast_enough(f"{base}/sample", DownloadStage("direct"))
    finally:
        server.shutdown()
        server.server_close()


def test_the_probe_is_skipped_on_an_unwatched_rung():
    # Port 9 is never listened on here; touching the network would raise or time out.
    download_route.ensure_fast_enough("http://127.0.0.1:9/sample", DownloadStage("mirror"))


def test_unverified_file_stops_at_the_first_rung_that_wrote_it(monkeypatch, tmp_path):
    _routes(monkeypatch, PROXY)
    destination = tmp_path / "config.yaml"
    calls: list[tuple[str, str | None]] = []

    def fake(url, target, proxy, *, data_root=None):
        calls.append((url, downloader.network_routes()[0].proxy))
        if url.startswith("https://gh-proxy.com/"):
            target.write_text("ok", encoding="utf-8")

    download_route._laddered_unverified(fake)(GITHUB_URL, destination, "", data_root=tmp_path)

    assert destination.read_text(encoding="utf-8") == "ok"
    assert calls == [
        (GITHUB_URL, PROXY),
        (GITHUB_URL, None),
        (f"https://gh-proxy.com/{GITHUB_URL}", None),
    ]


def test_stage_environment_pins_a_child(monkeypatch):
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
    monkeypatch.setenv("FINESUB_HF_ENDPOINT", "https://hf-mirror.com")
    attempts: list[dict[str, str]] = []

    def fetch(environment) -> None:
        attempts.append(environment)
        if len(attempts) < 3:
            raise RuntimeError("Please check your internet connection")

    download_route.climb_huggingface(fetch, {"HF_HOME": "x"}, is_retryable=lambda error: True)

    assert [attempt["HTTPS_PROXY"] for attempt in attempts] == [PROXY, "", ""]
    assert "HF_ENDPOINT" not in attempts[0] and "HF_ENDPOINT" not in attempts[1]
    assert "HF_HUB_DISABLE_XET" not in attempts[1]
    assert attempts[2]["HF_ENDPOINT"] == "https://hf-mirror.com"
    assert attempts[2]["HF_HUB_DISABLE_XET"] == "1"


def test_a_users_own_hf_endpoint_has_no_mirror_rung(monkeypatch):
    _routes(monkeypatch, None)
    monkeypatch.setenv("HF_ENDPOINT", "https://mine.example")
    attempts: list[dict[str, str]] = []

    def fetch(environment) -> None:
        attempts.append(environment)
        raise RuntimeError("connection refused")

    with pytest.raises(download_route.DownloadUnreachable):
        download_route.climb_huggingface(fetch, {}, is_retryable=lambda error: True)

    assert len(attempts) == 1
    assert attempts[0]["HF_HUB_DISABLE_XET"] == "1"


def test_transport_failures_are_recognised_in_any_locale():
    chinese = "tcp connect error: 由于连接方在一段时间后没有正确答复，连接尝试失败。 (os error 10060)"
    assert download_route.is_transport_failure(RuntimeError(chinese))
    assert not download_route.is_transport_failure(RuntimeError("磁盘空间不足。 (os error 112): tcp connect"))
    assert not download_route.is_transport_failure(RuntimeError("Hash mismatch for torch"))


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


def test_a_child_pinned_direct_does_not_pick_the_proxy_back_up(app_policy, monkeypatch):
    monkeypatch.setenv(download_route.DIRECT_ENVIRONMENT, "1")

    assert http_client.network_routes()[0].proxy is None
    assert "HTTPS_PROXY" not in os.environ
    assert os.environ["NO_PROXY"] == "*"


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
