"""`nonoka_x.runtime.fetch`: resumable verified downloads and the proxy route, offline.

Every server is a local `ThreadingHTTPServer`; the "proxy" is one too, which an
httpx client asks for the absolute URL of a plain-HTTP origin.
"""

from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nonoka_x.runtime import fetch  # noqa: E402
from nonoka_x.runtime.fetch import (  # noqa: E402
    DigestMismatch,
    DownloadAsset,
    DownloadPaused,
    NetworkRoute,
    SizeMismatch,
    download_file,
)

PAYLOAD = bytes(range(256)) * 64  # 16 KiB


class _Server:
    """A local origin that serves one payload, honouring `Range`, and records what it was asked."""

    def __init__(self, payload: bytes, *, ignore_range: bool = False, status: int | None = None) -> None:
        self.payload = payload
        self.requests: list[tuple[str, str | None]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                wanted = self.headers.get("Range")
                outer.requests.append((self.path, wanted))
                if status is not None:
                    self.send_error(status)
                    return
                body = outer.payload
                if wanted and not ignore_range:
                    start = int(wanted.removeprefix("bytes=").split("-")[0])
                    body = body[start:]
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{len(outer.payload) - 1}/{len(outer.payload)}")
                else:
                    self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self._server.server_port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def server():
    started: list[_Server] = []

    def start(payload: bytes = PAYLOAD, **options) -> _Server:
        instance = _Server(payload, **options)
        started.append(instance)
        return instance

    yield start
    for instance in started:
        instance.close()


def _direct() -> list[NetworkRoute]:
    return [NetworkRoute("直连", None)]


def _asset(base: str, payload: bytes = PAYLOAD, *, sha256: str | None = None, size: int | None = None) -> DownloadAsset:
    return DownloadAsset(
        url=f"{base}/files/tool.zip",
        size=len(payload) if size is None else size,
        sha256=sha256 or hashlib.sha256(payload).hexdigest(),
    )


# --- verified downloads -----------------------------------------------------------------------------------------


def test_a_download_is_verified_and_published(server, tmp_path):
    origin = server()
    reported: list[fetch.DownloadProgress] = []
    destination = tmp_path / "with space 空格" / "tool.zip"

    result = download_file(_asset(origin.base), destination, reported.append, routes=_direct)

    assert result == destination.resolve()
    assert destination.read_bytes() == PAYLOAD
    assert reported[-1].downloaded == reported[-1].total == len(PAYLOAD)
    assert not fetch.part_path(destination).exists()
    assert not Path(f"{fetch.part_path(destination)}.expect").exists()


def test_an_interrupted_download_resumes_where_it_stopped(server, tmp_path):
    origin = server()
    asset = _asset(origin.base)
    destination = tmp_path / "tool.zip"
    partial = fetch.part_path(destination)
    # What an interrupted run leaves: the first half, and the digest it was heading for.
    partial.write_bytes(PAYLOAD[:6000])
    Path(f"{partial}.expect").write_text(asset.sha256, encoding="utf-8")

    download_file(asset, destination, routes=_direct)

    assert destination.read_bytes() == PAYLOAD
    assert origin.requests == [("/files/tool.zip", "bytes=6000-")]


def test_a_partial_heading_for_other_bytes_is_not_resumed(server, tmp_path):
    origin = server()
    asset = _asset(origin.base)
    destination = tmp_path / "tool.zip"
    partial = fetch.part_path(destination)
    partial.write_bytes(b"x" * 6000)
    Path(f"{partial}.expect").write_text("f" * 64, encoding="utf-8")

    download_file(asset, destination, routes=_direct)

    assert destination.read_bytes() == PAYLOAD
    assert origin.requests == [("/files/tool.zip", None)]


def test_a_server_that_ignores_range_starts_over(server, tmp_path):
    origin = server(ignore_range=True)
    asset = _asset(origin.base)
    destination = tmp_path / "tool.zip"
    partial = fetch.part_path(destination)
    partial.write_bytes(PAYLOAD[:6000])
    Path(f"{partial}.expect").write_text(asset.sha256, encoding="utf-8")

    download_file(asset, destination, routes=_direct)

    assert destination.read_bytes() == PAYLOAD


def test_wrong_bytes_are_quarantined_and_reported(server, tmp_path):
    origin = server()
    asset = _asset(origin.base, sha256="0" * 64)
    destination = tmp_path / "tool.zip"

    with pytest.raises(DigestMismatch, match="Expected SHA-256 0000"):
        download_file(asset, destination, routes=_direct)

    assert not destination.exists()
    assert not fetch.part_path(destination).exists()
    assert Path(f"{fetch.part_path(destination)}.bad").read_bytes() == PAYLOAD


def test_a_short_body_is_a_size_mismatch(server, tmp_path):
    origin = server()
    asset = _asset(origin.base, size=len(PAYLOAD) + 10)

    with pytest.raises(SizeMismatch):
        download_file(asset, tmp_path / "tool.zip", routes=_direct)


def test_a_file_already_in_place_is_not_downloaded_again(server, tmp_path):
    origin = server()
    destination = tmp_path / "tool.zip"
    destination.write_bytes(PAYLOAD)

    download_file(_asset(origin.base), destination, routes=_direct)

    assert origin.requests == []


def test_a_pause_stops_the_download(server, tmp_path):
    origin = server()

    with pytest.raises(DownloadPaused):
        download_file(_asset(origin.base), tmp_path / "tool.zip", should_pause=lambda: True, routes=_direct)


def test_an_http_error_is_not_retried_on_another_route(server, tmp_path):
    """Only a transport failure moves on: the server answered, so another route would get the same answer."""

    origin = server(status=404)
    routes = [NetworkRoute("直连", None), NetworkRoute("直连（第二条）", None)]

    with pytest.raises(httpx.HTTPStatusError):
        download_file(_asset(origin.base), tmp_path / "tool.zip", routes=lambda: routes)
    assert len(origin.requests) == 1


def test_a_download_through_the_proxy(server, tmp_path):
    """The proxy route sends the request to the proxy, for the origin's absolute URL."""

    proxy = server()  # Answers every request itself, as a forwarding proxy would.
    asset = DownloadAsset(
        url="http://origin.invalid/files/tool.zip", size=len(PAYLOAD), sha256=hashlib.sha256(PAYLOAD).hexdigest()
    )

    download_file(asset, tmp_path / "tool.zip", routes=lambda: [NetworkRoute("代理", proxy.base)])

    assert (tmp_path / "tool.zip").read_bytes() == PAYLOAD
    assert proxy.requests == [("http://origin.invalid/files/tool.zip", None)]


def test_a_dead_proxy_falls_back_to_direct(server, tmp_path):
    origin = server()
    # Nothing listens on port 9: the proxy route fails to connect.
    routes = [NetworkRoute("代理", "http://127.0.0.1:9"), NetworkRoute("直连", None)]

    download_file(_asset(origin.base), tmp_path / "tool.zip", routes=lambda: routes)

    assert (tmp_path / "tool.zip").read_bytes() == PAYLOAD
    assert len(origin.requests) == 1


def test_every_route_failing_names_each_one(tmp_path):
    asset = DownloadAsset(url="http://127.0.0.1:9/tool.zip", size=1, sha256="0" * 64)

    with pytest.raises(fetch.NetworkConnectionError, match="所有连接方式均失败.*直连"):
        download_file(asset, tmp_path / "tool.zip", routes=_direct)


def test_an_asset_is_validated_like_finesubs():
    with pytest.raises(ValueError):
        DownloadAsset(url="ftp://example.com/x", size=1, sha256="0" * 64)
    with pytest.raises(ValueError):
        DownloadAsset(url="https://example.com/x", size=-1, sha256="0" * 64)
    with pytest.raises(ValueError):
        DownloadAsset(url="https://example.com/x", size=1, sha256="xyz")
    assert DownloadAsset(url="https://example.com/x", size=1, sha256="A" * 64).sha256 == "a" * 64


# --- routes and the proxy setting -------------------------------------------------------------------------------

_ENV_NAMES = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy", "NO_PROXY", "no_proxy")


@pytest.fixture
def machine(monkeypatch: pytest.MonkeyPatch):
    """A stub registry proxy and liveness probe; no app policy unless a test installs one."""

    state = {"system": None, "live": set()}
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(fetch.PREFERENCES_ENVIRONMENT, raising=False)
    monkeypatch.delenv(fetch.DIRECT_ENVIRONMENT, raising=False)
    monkeypatch.setattr(fetch, "_ambient", None)
    monkeypatch.setattr(fetch, "_custom_cache", None)
    monkeypatch.setattr(fetch, "windows_proxy", lambda: state["system"])
    monkeypatch.setattr(fetch, "local_proxy_is_listening", lambda proxy: proxy in state["live"])
    return state


def test_routes_are_system_then_environment_then_direct(machine, monkeypatch):
    machine["system"] = "http://127.0.0.1:7890"
    machine["live"] |= {"http://127.0.0.1:7890", "http://127.0.0.1:8080"}
    monkeypatch.setenv("HTTPS_PROXY", "127.0.0.1:8080")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")

    routes = fetch.network_routes()

    assert [route.proxy for route in routes] == ["http://127.0.0.1:7890", "http://127.0.0.1:8080", None]
    assert routes[-1].label == "直连"


def test_a_local_proxy_that_is_not_running_is_skipped(machine):
    machine["system"] = "http://127.0.0.1:7890"

    assert [route.proxy for route in fetch.network_routes()] == [None]


def test_without_the_app_policy_the_environment_is_left_alone(machine, monkeypatch):
    machine["system"] = "http://127.0.0.1:7890"
    machine["live"].add("http://127.0.0.1:7890")

    fetch.network_routes()

    assert "HTTPS_PROXY" not in os.environ
    assert "NO_PROXY" not in os.environ


def test_a_custom_proxy_replaces_the_system_one_and_is_written_out(machine, monkeypatch, tmp_path):
    machine["system"] = "http://127.0.0.1:7890"
    machine["live"] |= {"http://127.0.0.1:7890", "http://127.0.0.1:1081"}
    preferences = tmp_path / "preferences.json"
    preferences.write_text(json.dumps({"proxy": {"scheme": "http", "port": 1081}}), encoding="utf-8")
    monkeypatch.setenv(fetch.PREFERENCES_ENVIRONMENT, str(preferences))

    assert fetch.install_app_policy()
    routes = fetch.network_routes()

    assert [route.proxy for route in routes] == ["http://127.0.0.1:1081", None]
    # D17: the live route is spelled out for everything this process starts.
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:1081"
    assert os.environ["NO_PROXY"] == fetch.LOCAL_HOSTS


def test_direct_is_spelled_no_proxy_star(machine, monkeypatch, tmp_path):
    preferences = tmp_path / "preferences.json"
    preferences.write_text("{}", encoding="utf-8")
    monkeypatch.setenv(fetch.PREFERENCES_ENVIRONMENT, str(preferences))
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")  # Not running.

    fetch.install_app_policy()
    child: dict[str, str] = {"HTTPS_PROXY": "http://stale:1"}
    fetch.apply_proxy_environment(child)

    assert fetch.live_proxy() is None
    assert "HTTPS_PROXY" not in os.environ and os.environ["NO_PROXY"] == "*"
    assert "HTTPS_PROXY" not in child and child["NO_PROXY"] == "*"


def test_a_child_through_a_proxy_gets_every_variable(machine):
    machine["system"] = "http://127.0.0.1:7890"
    machine["live"].add("http://127.0.0.1:7890")
    child: dict[str, str] = {}

    fetch.apply_proxy_environment(child)

    assert child == {
        "HTTP_PROXY": "http://127.0.0.1:7890",
        "HTTPS_PROXY": "http://127.0.0.1:7890",
        "ALL_PROXY": "http://127.0.0.1:7890",
    }


def test_proxy_credentials_stay_out_of_the_label(machine):
    machine["system"] = "http://user:secret@proxy.example.com:3128"
    machine["live"].add("http://user:secret@proxy.example.com:3128")

    route = fetch.network_routes()[0]

    assert route.proxy == "http://user:secret@proxy.example.com:3128"
    assert "secret" not in route.label and "***@proxy.example.com:3128" in route.label


@pytest.mark.parametrize(
    "entry",
    [None, "x", {"scheme": "ftp", "port": 21}, {"port": 0}, {"port": True}, {"port": "7890"}, {"host": "a/b", "port": 1}],
)
def test_invalid_custom_proxy_is_ignored(entry):
    assert fetch.custom_proxy_url(entry) is None


def test_custom_proxy_url_defaults_and_ipv6():
    assert fetch.custom_proxy_url({"port": 7890}) == "http://127.0.0.1:7890"
    assert fetch.custom_proxy_url({"scheme": "SOCKS5", "host": "::1", "port": 1080}) == "socks5://[::1]:1080"


def test_the_client_never_reads_the_environment(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    with fetch.create_client(NetworkRoute("直连", None), timeout=httpx.Timeout(1.0)) as client:
        assert client._trust_env is False


def test_fetch_is_a_copy_not_a_wrapper():
    """Nonoka's runtime package stands on its own: no FineSub import anywhere in it."""

    import ast

    package = Path(fetch.__file__).parent
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            assert not any(name.split(".")[0] in {"finesub", "finesub_bootstrap"} for name in names), path.name
