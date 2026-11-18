"""`finesub_bootstrap.http_client` for the finesub-correct package: only the names its FineSub code uses (WP-P1).

Copied from FineSub v0.7.0 (`7123055`), `src/finesub_bootstrap/http_client.py`, as
vendored in Nonoka X (the transcription package carries the original). Kept: the
top-level definitions the names below need, unchanged and in the original's order;
everything else is left out. `__all__` is exactly the names this package's FineSub
code imports (docs/engine-packages.md section 4); `tests/test_engine_packages.py`
holds this package to them.
"""

from __future__ import annotations

from typing import Any, Callable
import httpx

__all__ = ["in_tunnel_setup", "watch_tunnel"]


_TUNNEL_WATCH = "finesub_tunnel_watch"


_HEADERS_ENDED = ("http11.receive_response_headers.complete", "http11.receive_response_headers.failed")


class TunnelWatch:
    """An httpcore `trace` hook that keeps a proxy's CONNECT exchange in the connect phase.

    Through a proxy, an HTTPS request first sends `CONNECT host:443` and waits for the proxy's reply, and httpcore
    waits for that reply with the *read* timeout: a proxy that accepts the connection but never answers (hung, or
    one that dials the blocked upstream before replying) cost the whole read timeout -- a download's 120 s, a
    generation call's hard deadline -- and ended as a `ReadTimeout`, which no reachability rule counts. This waits
    for that one reply with the connect timeout instead and marks the request while it does, so
    :func:`in_tunnel_setup` sees the timeout for what it is.

    Keyed on the CONNECT request itself (httpcore traces it with the real request's extensions), not on the order
    of events: an `https://` proxy has its own TLS handshake before the CONNECT. If an httpcore release renames
    the event this stays inert and the old behaviour returns, which `test_llm_api_reachability.py` pins with a
    real socket.
    """

    def __init__(self, chained: Callable[[str, dict], Any] | None = None) -> None:
        self.pending = False
        self._chained = chained
        self._saved_read: tuple[dict, Any] | None = None

    def __call__(self, name: str, info: dict) -> None:
        # Only `.started` carries the request; `.complete` and `.failed` carry just the outcome, so the end of the
        # CONNECT reply is recognised by the state its start left.
        if name == "http11.receive_response_headers.started":
            if getattr(info.get("request"), "method", None) == b"CONNECT":
                self.pending = True
                timeouts = info["request"].extensions.get("timeout")
                if isinstance(timeouts, dict) and timeouts.get("connect") is not None:
                    # The CONNECT request shares the real request's extensions: put the read timeout back after.
                    self._saved_read = (timeouts, timeouts.get("read"))
                    timeouts["read"] = timeouts["connect"]
        elif self.pending and name in _HEADERS_ENDED:
            # Still pending after `.failed`: that is the timeout `in_tunnel_setup` is asked about.
            self.pending = name.endswith(".failed")
            if self._saved_read is not None:
                timeouts, read = self._saved_read
                timeouts["read"] = read
                self._saved_read = None
        if self._chained is not None:
            self._chained(name, info)


def watch_tunnel(request: httpx.Request) -> None:
    """An httpx request hook that gives every HTTPS request a :class:`TunnelWatch`."""

    if request.url.scheme == "https" and _TUNNEL_WATCH not in request.extensions:
        watch = TunnelWatch(request.extensions.get("trace"))
        request.extensions["trace"] = watch
        request.extensions[_TUNNEL_WATCH] = watch


def in_tunnel_setup(exc: BaseException) -> bool:
    """A timeout while a proxy's CONNECT reply was still awaited (:class:`TunnelWatch`)."""

    if not isinstance(exc, httpx.TimeoutException):
        return False
    request = getattr(exc, "_request", None)
    watch = request.extensions.get(_TUNNEL_WATCH) if request is not None else None
    return watch is not None and watch.pending
