from __future__ import annotations

import httpx
import pytest

from nonoka_x import cloud_route
from nonoka_x.cloud_route import FailoverTransport, RouteState

PRIMARY = "cloud.nonoka.online"
BACKUP = "cloud-jp.nonoka.online"
URL = f"https://{PRIMARY}/v1/llm/chat/completions"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Upstream:
    """The two routes: each host either answers with its name and the body it
    got, refuses the connection, or breaks the answer mid-stream."""

    def __init__(self) -> None:
        self.down: set[str] = set()
        self.breaks: set[str] = set()
        self.seen: list[tuple[str, str, bytes]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.seen.append((host, request.headers["host"], request.content))
        if host in self.down:
            raise httpx.ConnectError("refused", request=request)
        if host in self.breaks:
            return httpx.Response(200, stream=_Broken(request))
        return httpx.Response(200, json={"host": host, "body": request.content.decode()})


class _Broken(httpx.SyncByteStream):
    def __init__(self, request: httpx.Request) -> None:
        self._request = request

    def __iter__(self):
        yield b"data: {"
        raise httpx.ReadError("connection reset", request=self._request)


@pytest.fixture
def route():
    upstream = Upstream()
    clock = Clock()
    state = RouteState(clock=clock)
    transport = FailoverTransport(httpx.MockTransport(upstream.handler), state=state)
    client = httpx.Client(transport=transport)
    yield upstream, clock, state, client
    client.close()


def test_a_refused_primary_is_resent_to_the_backup_with_its_body(route):
    upstream, _, state, client = route
    upstream.down.add(PRIMARY)

    response = client.post(URL, json={"model": "m"})

    assert response.json() == {"host": BACKUP, "body": '{"model":"m"}'}
    assert upstream.seen[-1][1] == BACKUP  # the proxy is addressed by its own name
    assert state.prefer_backup(PRIMARY)


def test_the_backup_is_kept_until_the_stickiness_lapses(route):
    upstream, clock, _, client = route
    upstream.down.add(PRIMARY)
    client.post(URL, json={})
    upstream.down.clear()

    assert client.post(URL, json={}).json()["host"] == BACKUP
    clock.now += cloud_route.STICKINESS_SECONDS + 1
    assert client.post(URL, json={}).json()["host"] == PRIMARY


def test_a_failing_backup_hands_the_call_back_to_the_primary(route):
    upstream, _, state, client = route
    upstream.down.add(PRIMARY)
    client.post(URL, json={})
    upstream.down = {BACKUP}

    assert client.post(URL, json={}).json()["host"] == PRIMARY
    assert not state.prefer_backup(PRIMARY)


def test_both_routes_down_raises_the_primary_error(route):
    upstream, _, _, client = route
    upstream.down = {PRIMARY, BACKUP}

    with pytest.raises(httpx.ConnectError):
        client.post(URL, json={})


def test_a_stream_broken_mid_answer_sends_the_retry_to_the_other_route(route):
    upstream, _, state, client = route
    upstream.breaks.add(PRIMARY)

    with pytest.raises(httpx.ReadError):
        client.post(URL, json={}).read()

    assert state.prefer_backup(PRIMARY)
    assert client.post(URL, json={}).json()["host"] == BACKUP


def test_an_answer_is_not_rerouted(route):
    upstream, _, state, _ = route

    def refuse(request: httpx.Request) -> httpx.Response:
        upstream.seen.append((request.url.host, "", b""))
        return httpx.Response(503, text="busy")

    transport = FailoverTransport(httpx.MockTransport(refuse), state=state)
    with httpx.Client(transport=transport) as client:
        assert client.post(URL, json={}).status_code == 503
    assert [host for host, _, _ in upstream.seen] == [PRIMARY]


def test_other_hosts_pass_straight_through(route):
    upstream, _, _, client = route
    upstream.down.add("api.openai.com")

    with pytest.raises(httpx.ConnectError):
        client.post("https://api.openai.com/v1/chat/completions", json={})
    assert [host for host, _, _ in upstream.seen] == ["api.openai.com"]


def test_mounts_follow_the_clients_proxy():
    assert cloud_route.failover_mounts({"trust_env": True}) == {}
    mounts = cloud_route.failover_mounts({"proxy": "http://127.0.0.1:7890", "trust_env": False})
    assert list(mounts) == [f"https://{PRIMARY}"]
    assert isinstance(mounts[f"https://{PRIMARY}"], FailoverTransport)
    assert cloud_route.failover_mounts({"proxy": None, "trust_env": False})


def test_install_reaches_the_engines_client_factory(monkeypatch):
    from finesub.llm import http as llm_http

    monkeypatch.setattr(llm_http, "client_kwargs", lambda: {"proxy": None, "trust_env": False})
    cloud_route.install_cloud_route()
    cloud_route.install_cloud_route()  # idempotent

    kwargs = llm_http.client_kwargs()
    assert list(kwargs["mounts"]) == [f"https://{PRIMARY}"]
    assert getattr(llm_http.client_kwargs, "_nonoka_cloud_route")
    with llm_http.llm_http_client(timeout=5) as client:
        assert isinstance(client._transport_for_url(httpx.URL(URL)), FailoverTransport)
        assert not isinstance(client._transport_for_url(httpx.URL("https://api.openai.com/")), FailoverTransport)
