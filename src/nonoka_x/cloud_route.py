"""Fail the engine's Nonoka Cloud LLM calls over to the backup route.

The free model is served at the backend's `/v1/llm`, on Cloudflare's free
plan. From mainland China, China Telecom reaches it through Amsterdam and
China Unicom through Los Angeles, and either route drops out at peak hours.
`cloud-jp.nonoka.online` is a reverse proxy in Tokyo in front of the same
backend, with far shorter routes from all three carriers.

The Go half (`desktop/internal/cloud/failover.go`) already sends the desktop's
own API calls and subtitle downloads there when the primary fails. The engine
talks to the relay itself, over httpx, so this module gives its LLM clients the
same behaviour: a request to the primary host that fails before an answer
arrives is sent again to the backup, and once the backup has had to step in,
calls keep going there for a while before the primary gets another chance.

Resending is safe here where it would not be for the Go API client: the relay
charges nothing and has no side effect, and the engine retries a failed call
anyway -- this only decides which route that call takes.

A response that breaks mid-stream cannot be replayed at this layer (the engine
has already read part of it), but it does count against its route: the
engine's retry of that call goes to the other one.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Iterator

import httpx

LOGGER = logging.getLogger(__name__)

#: The backend host and the reverse proxy serving the same backend. Kept the
#: same as `backupHosts` in desktop/internal/cloud/failover.go.
BACKUP_HOSTS = {"cloud.nonoka.online": "cloud-jp.nonoka.online"}

#: How long calls keep going to the backup once it had to step in.
STICKINESS_SECONDS = 10 * 60

_INSTALLED_ATTRIBUTE = "_nonoka_cloud_route"


class RouteState:
    """Which route each primary host is on. Shared by every client in the
    process: the engine builds a client per call, and what one call learnt
    about the route must hold for the next."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._backup_until: dict[str, float] = {}

    def prefer_backup(self, primary: str) -> bool:
        with self._lock:
            until = self._backup_until.get(primary)
            if until is None:
                return False
            if self._clock() < until:
                return True
            del self._backup_until[primary]
        LOGGER.warning("cloud route: trying the primary route to %s again", primary)
        return False

    def failed(self, primary: str, host: str) -> None:
        """`host` (the primary or its backup) just failed a call."""

        with self._lock:
            if host == primary:
                switched = primary not in self._backup_until
                self._backup_until[primary] = self._clock() + STICKINESS_SECONDS
            else:
                switched = self._backup_until.pop(primary, None) is not None
        if switched:
            target = BACKUP_HOSTS.get(primary, "?") if host == primary else primary
            LOGGER.warning("cloud route: %s failed, using %s", host, target)


STATE = RouteState()


class FailoverTransport(httpx.BaseTransport):
    """Sends requests for a host in `backups` to its backup when the route in
    use fails, over one inner transport that carries the client's proxy."""

    def __init__(
        self,
        inner: httpx.BaseTransport,
        backups: dict[str, str] | None = None,
        state: RouteState | None = None,
    ) -> None:
        self._inner = inner
        self._backups = BACKUP_HOSTS if backups is None else backups
        self._state = STATE if state is None else state

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        primary = request.url.host
        backup = self._backups.get(primary)
        if backup is None or request.url.scheme != "https":
            return self._inner.handle_request(request)
        # An LLM request body is a JSON document, small next to the answer;
        # reading it now is what lets it be sent a second time.
        request.read()
        first, second = (backup, primary) if self._state.prefer_backup(primary) else (primary, backup)
        try:
            response = self._send(request, first)
        except httpx.TransportError as error:
            self._state.failed(primary, first)
            try:
                response = self._send(request, second)
            except httpx.TransportError:
                self._state.failed(primary, second)
                raise error from None
            return self._watched(response, primary, second)
        return self._watched(response, primary, first)

    def _send(self, request: httpx.Request, host: str) -> httpx.Response:
        if host == request.url.host:
            return self._inner.handle_request(request)
        headers = httpx.Headers(request.headers)
        headers["Host"] = host
        rerouted = httpx.Request(
            request.method,
            request.url.copy_with(host=host),
            headers=headers,
            content=request.content,
            extensions=request.extensions,
        )
        return self._inner.handle_request(rerouted)

    def _watched(self, response: httpx.Response, primary: str, host: str) -> httpx.Response:
        stream = _WatchedStream(response.stream, lambda: self._state.failed(primary, host))
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=stream,
            extensions=response.extensions,
        )

    def close(self) -> None:
        self._inner.close()


class _WatchedStream(httpx.SyncByteStream):
    """A response body that reports a transport failure while it is read."""

    def __init__(self, inner: Any, on_failure: Callable[[], None]) -> None:
        self._inner = inner
        self._on_failure = on_failure

    def __iter__(self) -> Iterator[bytes]:
        try:
            yield from self._inner
        except httpx.TransportError:
            self._on_failure()
            raise

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if close is not None:
            close()


def failover_mounts(kwargs: dict[str, Any]) -> dict[str, httpx.BaseTransport]:
    """The `mounts` that put the backup route under a client built from
    `kwargs` (`finesub.llm.http.client_kwargs()`), or {} when that client
    finds its proxy in the environment: a mounted transport does not, and
    routing around the user's proxy would be worse than no backup."""

    if kwargs.get("trust_env", True):
        return {}
    proxy = kwargs.get("proxy")
    mounts: dict[str, httpx.BaseTransport] = {}
    for primary in BACKUP_HOSTS:
        mounts[f"https://{primary}"] = FailoverTransport(httpx.HTTPTransport(proxy=proxy))
    return mounts


def install_cloud_route() -> None:
    """Give every LLM-layer client of the engine the backup route.

    `finesub.llm.http.llm_http_client` builds each client from
    `client_kwargs()`, looked up at call time, so wrapping that function
    reaches every call site in this process. Call it after the app's proxy
    policy is installed (it is what makes `client_kwargs()` name a proxy).
    Idempotent.
    """

    try:
        from finesub.llm import http as llm_http
    except Exception:  # noqa: BLE001 - the LLM layer is optional in installer processes
        return
    if getattr(llm_http.client_kwargs, _INSTALLED_ATTRIBUTE, False):
        return
    original = llm_http.client_kwargs

    def client_kwargs() -> dict[str, Any]:
        kwargs = original()
        mounts = failover_mounts(kwargs)
        return {**kwargs, "mounts": mounts} if mounts else kwargs

    setattr(client_kwargs, _INSTALLED_ATTRIBUTE, True)
    llm_http.client_kwargs = client_kwargs
