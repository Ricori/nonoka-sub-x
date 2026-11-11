"""HTTP and WebSocket clients for the cloud ASR APIs, on Nonoka's network route.

Replaces what the moved backends took from FineSub's `finesub/api_http.py` (WP1): the client, the connect
timeout, which failures and statuses are worth sending again (copied unchanged), and the WebSocket options of
35t Live.

The route is Nonoka's proxy setting (R0 document D17), the one downloads take (`runtime.fetch.live_proxy`): the
custom proxy from the settings page, else the Windows system proxy, else the environment's, each only while its
local client is listening; otherwise direct. Inside FineSub the same calls reached this route through the
bridge's patch of `api_http.configured_proxy` (`finesub_bridge.install_app_proxy_policy`), so the route is what
it was. As there, a SOCKS route goes direct with a warning: httpx would need `socksio`, and its SOCKS handshake
reads without a timeout. The client never consults the environment itself (`trust_env=False`).
"""

from __future__ import annotations

import threading
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

from nonoka_x.runtime import fetch

from .reporting import current_reporter

#: The connect phase of every cloud ASR call (FineSub `api_http.CONNECT_TIMEOUT_SECONDS`): it bounds only what
#: happens before the server has said anything -- TCP, a proxy's CONNECT reply, TLS.
CONNECT_TIMEOUT_SECONDS = 15.0

#: Failures where nothing about the request was wrong, so the identical request may well succeed (copied from
#: FineSub `api_http.RETRYABLE_TRANSPORT`): no response at all or a timeout, judged by type, never by text.
RETRYABLE_TRANSPORT: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
    httpx.ProxyError,
    TimeoutError,
    ConnectionError,
)

_warned_socks = False
_warned_lock = threading.Lock()


def retryable_status(status: int) -> bool:
    """Whether an HTTP status says "not now" rather than "not this request": 408, 409, 429 and every 5xx, and
    425 (copied from FineSub `api_http.retryable_status`)."""

    return status in (408, 409, 425, 429) or status >= 500


def is_retryable_http_error(exc: BaseException) -> bool:
    """By type, then by a status the exception carries (`response.status_code`, or a `status_code` attribute);
    never by its text (copied from FineSub `api_http.is_retryable_http_error`)."""

    if isinstance(exc, RETRYABLE_TRANSPORT):
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if not isinstance(status, int):
        status = getattr(exc, "status_code", None)
    return isinstance(status, int) and status > 0 and retryable_status(status)


def api_proxy() -> str | None:
    """The proxy a cloud ASR call goes through now, or None for direct (module docstring)."""

    global _warned_socks
    proxy = fetch.live_proxy()
    if proxy and proxy.lower().startswith("socks"):
        with _warned_lock:
            first, _warned_socks = not _warned_socks, True
        if first:
            current_reporter().warning(
                "api-proxy-socks",
                "云端转写的 API 调用不支持 SOCKS 代理，改为直连",
                action="在设置页把代理换成代理软件的 HTTP 端口",
            )
        return None
    return proxy or None


def api_http_client(timeout: Any = None, **kwargs: Any) -> httpx.Client:
    """An `httpx.Client` for one cloud ASR call, on the route of `api_proxy`; its HTTPS requests carry the
    CONNECT guard (`runtime.fetch.watch_tunnel`)."""

    client = httpx.Client(timeout=timeout, **{"proxy": api_proxy(), "trust_env": False, **kwargs})
    client.event_hooks["request"].append(fetch.watch_tunnel)
    return client


def websocket_kwargs(host: str) -> dict[str, Any]:
    """`websocket-client`'s connection options for the same route: the proxy, or `host` on the no-proxy list
    (so it does not read `HTTPS_PROXY` itself)."""

    proxy = api_proxy()
    if proxy is None:
        return {"http_no_proxy": [host]}
    url = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
    out: dict[str, Any] = {"http_proxy_host": url.hostname, "http_proxy_port": url.port or 80, "proxy_type": "http"}
    if url.username:
        out["http_proxy_auth"] = (unquote(url.username), unquote(url.password or ""))
    return out


__all__ = [
    "CONNECT_TIMEOUT_SECONDS",
    "RETRYABLE_TRANSPORT",
    "api_http_client",
    "api_proxy",
    "is_retryable_http_error",
    "retryable_status",
    "websocket_kwargs",
]
