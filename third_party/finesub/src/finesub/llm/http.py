"""The one place an LLM-layer HTTP client is built.

Everything this package talks to over the network -- Gemini REST, the
OpenAI-compat / Anthropic transports, Exa / Gemma4 / Tavily, and the free
`countTokens` endpoint -- goes out through :func:`llm_http_client`, so "does
this request use the proxy?" has one answer that can be read off the code.

**Scope is API calls, deliberately.** The download family
(`finesub_bootstrap.http_client`) has its own route discovery -- Windows system
proxy, environment, a liveness probe on local ports -- and is not touched by
this setting. That separation is why the proxy is resolved into an explicit
`proxy=` argument rather than written into `os.environ`: the environment is
read by `apply_network_environment`, so an ambient proxy would silently
reroute downloads too.

⚠ **Turning a proxy on changes the failure modes**, not just the route. Gemini
scores datacenter and shared-exit IPs, and answers a suspicious one with a
block that `client.LLMIPRiskError` recognises. A proxy that works for search
can still make correction calls fail this way.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any, Mapping

import httpx

from .. import settings

#: `[llm] proxy = "direct"` means "ignore the environment too". Without it,
#: unsetting the key would still leave `HTTPS_PROXY` in charge, and there would
#: be no way to say "no proxy for LLM calls" on a machine that needs one for
#: everything else.
DIRECT = "direct"


def configured_proxy() -> str | None:
    """`[llm] proxy` from config.toml: a URL, :data:`DIRECT`, or None.

    Read each time a client is built, not memoized: a client built after
    config.toml changed uses the new value, the same "edits take effect on the
    next read" contract as every other setting. The read itself is a cached,
    stat-invalidated lookup (`finesub.config`), negligible next to a request.
    """

    value = (settings.PROXY.resolve() or "").strip()
    return value or None


def client_kwargs() -> dict[str, Any]:
    """`proxy` / `trust_env` for one client, from the configured value.

    Three states, and the middle one is the reason this is not a bool:

    * key absent -- `trust_env=True`, i.e. `HTTPS_PROXY` still works. This is
      what the layer did before the setting existed, so adding it breaks nobody.
    * `proxy = "direct"` -- no proxy, and the environment is ignored.
    * `proxy = "<url>"` -- that proxy, and the environment is ignored. The file
      is more specific than the ambient environment, so it wins outright rather
      than being merged with it.
    """

    configured = configured_proxy()
    if configured is None:
        return {"trust_env": True}
    if configured.lower() == DIRECT:
        return {"proxy": None, "trust_env": False}
    return {"proxy": configured, "trust_env": False}


def llm_http_client(timeout: Any = None, **kwargs: Any) -> httpx.Client:
    """An `httpx.Client` for an LLM-layer API call.

    Signature-compatible with `httpx.Client` for the call sites that pass a
    bare `timeout=`, and with the `client_factory` seam the search and token
    clients already inject in tests.
    """

    return httpx.Client(timeout=timeout, **{**client_kwargs(), **kwargs})


class DeadlineExceeded(httpx.TimeoutException):
    """The whole request outlived its deadline and was torn down.

    A `TimeoutException`, and worded "timed out", on purpose: every retry and
    classification rule written for an httpx timeout applies to it unchanged
    (`llm_runtime._status_code_from_exception` reads it as NO_RESPONSE_TIMEOUT).
    """


#: The connect phase is not generation and does not scale with the deadline: a
#: host that never answers the handshake must not hold a call for all of it.
CONNECT_TIMEOUT_SECONDS = 45.0


#: httpcore trace steps up to the moment the request leaves. The event name is
#: `<logger>.<step>.<phase>`, and the logger is `connection`, `proxy` (the
#: CONNECT tunnel's TLS) or `socks` depending on the route, so steps are matched
#: by suffix. Past the deadline each of them aborts the attempt: nothing is sent
#: once the deadline has passed -- sending late would start a generation nobody
#: is waiting for.
_CONNECT_STEPS = (".connect_tcp.", ".start_tls.")
_SEND_STEP = "http11.send_request_headers.started"
#: A step that just produced a stream. Its socket is what `cut()` shuts down,
#: and TLS replaces it: `wrap_socket` detaches the raw socket (fileno -1), so
#: the one worth holding is the latest.
_STREAM_DONE = (".connect_tcp.complete", ".start_tls.complete")

#: How long the caller waits on the worker at a time. On Windows a thread join
#: with a timeout is not interruptible, so Ctrl+C would otherwise wait out the
#: whole deadline -- which can be an hour and a half.
_JOIN_SLICE_SECONDS = 0.5


class _Teardown:
    """Everything needed to stop one request from outside, and the one clock
    that says whether it is too late.

    Expiry is read off the monotonic clock, never only off "has the teardown
    run": the two race (a timer thread can wake ~15 ms late on Windows, and a
    socket timeout does not), and the answer must not depend on which won.
    """

    def __init__(self, deadline_seconds: float) -> None:
        self.deadline_seconds = deadline_seconds
        self.deadline_at = time.monotonic() + max(0.0, deadline_seconds)
        self._lock = threading.Lock()
        self._cut = False
        self._sockets: list[socket.socket] = []
        self._client: httpx.Client | None = None

    def expired(self) -> bool:
        return self._cut or time.monotonic() >= self.deadline_at

    def error(self) -> DeadlineExceeded:
        return DeadlineExceeded(
            f"request timed out: exceeded its {self.deadline_seconds:.0f}s hard "
            "deadline and was closed"
        )

    def adopt_client(self, client: httpx.Client) -> None:
        with self._lock:
            self._client = client
            late = self._cut
        if late:
            client.close()

    def trace(self, name: str, info: Mapping[str, Any]) -> None:
        """httpcore's `trace` extension: learn the socket each time a step
        produces a stream, and refuse to go on once the deadline has passed."""

        stream = info.get("return_value") if name.endswith(_STREAM_DONE) else None
        if stream is not None:
            sock = stream.get_extra_info("socket")
            if sock is not None:
                with self._lock:
                    self._sockets.append(sock)
        before_send = name == _SEND_STEP or any(step in name for step in _CONNECT_STEPS)
        if before_send and self.expired():
            if stream is not None:
                # Built after the caller gave up (it was stuck in DNS or a
                # handshake): no pool owns it yet, so nobody else will close it.
                _shut(stream.get_extra_info("socket"))
                stream.close()
            raise self.error()

    def cut(self) -> None:
        with self._lock:
            self._cut = True
            sockets, client = list(self._sockets), self._client
        for sock in sockets:
            _shut(sock)
        if client is not None:
            client.close()


def _shut(sock: socket.socket | None) -> None:
    if sock is None:
        return
    try:
        # The base-class method, deliberately: `SSLSocket.shutdown` also drops
        # the TLS object from under the thread that is reading it.
        socket.socket.shutdown(sock, socket.SHUT_RDWR)
    except OSError:
        pass


def post_within_deadline(
    url: str,
    *,
    headers: Mapping[str, str],
    json: Any,
    deadline_seconds: float,
) -> httpx.Response:
    """POST and read the whole response, but never for longer than
    `deadline_seconds` in total; the body is read before this returns.

    ⚠ **An httpx timeout is not a deadline.** It bounds each network operation,
    so a server that sends a byte now and then -- a chunked body trickling out,
    a keep-alive -- never trips it, and the call can hold its slot indefinitely.
    The per-operation timeout stays (set to the deadline, connect capped at
    :data:`CONNECT_TIMEOUT_SECONDS`) as stall detection; the deadline itself is
    enforced here, three ways:

    * The request runs on a worker thread and the caller waits for it only
      until the deadline, so the caller is back on time even while the worker
      sits in something nothing can interrupt (a slow DNS lookup).
    * At the deadline the request is torn down, not merely left behind: the
      socket is shut down (that wakes a read blocked in another thread on every
      platform; a bare close does not on Linux) and the client closed. The
      socket is learnt from httpcore's `trace` extension as each step produces
      one -- the TCP socket, then the TLS socket that replaces it -- so this
      works before any response has arrived too. ⚠ During a TLS handshake there
      is nothing to shut down (the raw socket is already detached, the TLS one
      not yet returned); that window is bounded by the connect timeout.
    * The same trace refuses every step up to sending the request once the
      deadline has passed, so a worker still stuck in DNS when the caller gave
      up can never send it afterwards.

    The caller gets :class:`DeadlineExceeded` whenever the deadline decided the
    outcome -- including when a per-read timeout happened to fire first.
    """

    teardown = _Teardown(deadline_seconds)
    outcome: dict[str, Any] = {}
    timeout = httpx.Timeout(
        deadline_seconds, connect=min(CONNECT_TIMEOUT_SECONDS, deadline_seconds)
    )

    def attempt() -> None:
        try:
            with llm_http_client(timeout=timeout) as client:
                teardown.adopt_client(client)
                with client.stream(
                    "POST",
                    url,
                    headers=dict(headers),
                    json=json,
                    extensions={"trace": teardown.trace},
                ) as response:
                    response.read()
            outcome["response"] = response
        except BaseException as exc:  # handed to the caller's thread
            outcome["error"] = exc

    worker = threading.Thread(target=attempt, name="llm-http-deadline", daemon=True)
    worker.start()
    try:
        while worker.is_alive():
            remaining = teardown.deadline_at - time.monotonic()
            if remaining <= 0:
                break
            worker.join(min(remaining, _JOIN_SLICE_SECONDS))
    except BaseException:
        # Ctrl+C lands on this thread, not the worker's: stop the request too.
        teardown.cut()
        raise
    if worker.is_alive():
        teardown.cut()
        raise teardown.error()
    error = outcome.get("error")
    if error is not None:
        if isinstance(error, DeadlineExceeded) or teardown.expired():
            raise teardown.error() from error
        raise error
    return outcome["response"]
