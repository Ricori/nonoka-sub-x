"""Nonoka's network route and its resumable, verified file download.

Copied from FineSub v0.7.0 (`third_party/finesub/src/finesub_bootstrap/`, 7123055)
so that Nonoka's own code stops importing `finesub_bootstrap`
(`docs/transcription-engines-plan.md` 4.2 item 12, WP-R2):

- `http_client.py`: the route discovery (`network_routes`: live proxies, then
  direct), the registry and environment proxy readers, the local-proxy liveness
  probe, `create_client` and the `TunnelWatch` CONNECT guard -- copied whole;
- `downloader.py`: `download_file` is its `download_asset` (one lock per
  destination, a resumable `.part` with the digest it is heading for beside it,
  size then sha256, a mismatch quarantined as `.bad`) -- copied whole;
- `models.py`: `DownloadAsset` and `DownloadProgress`, as plain dataclasses
  instead of pydantic models.

Nonoka's proxy setting (R0 document D17) is layered on top here: it used to be
monkey-patched into FineSub's `http_client` by `nonoka_x.download_route`. Once
`install_app_policy` has run (the desktop names its preferences file in
`NONOKA_PREFERENCES_FILE`), a custom proxy saved on the settings page replaces
the system proxy and the environment outright; without one, the Windows system
proxy and then the environment as before. Whatever route is live is written into
this process's environment explicitly: the proxy variables when there is one,
`NO_PROXY=*` when direct, so a client that would otherwise fall back to the
registry (Python's `urllib`, Rust clients) takes the same route.

There is no mirror and no route choice by region: downloads go to the official
source, through the live proxy or directly (plan 4.2 item 18).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import socket
import time
from typing import Any
from urllib.parse import urlparse, urlsplit, urlunsplit

import httpx

from nonoka_x.runtime.fsops import holding_lock, replace_path

try:
    import winreg
except ImportError:  # pragma: no cover - Windows production code
    winreg = None  # type: ignore[assignment]

#: The connect phase of a download. It bounds only what happens before the
#: server has said anything (TCP, a proxy's CONNECT reply, TLS), never a slow
#: transfer, which `read` governs. A working route, proxy included, connects
#: well inside 15 s.
CONNECT_TIMEOUT_SEC = 15.0

#: The proxy variables, upper and lower case, in the order they are consulted.
PROXY_NAMES = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")
#: `NO_PROXY` while a proxy is in use: local services are never proxied.
LOCAL_HOSTS = "localhost,127.0.0.1,::1"
_LOCAL_HOST_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})

#: preferences.json, set by the sidecar for itself and every child it spawns.
#: Unset (tests, a hand-run worker) the app policy stays out of the way.
PREFERENCES_ENVIRONMENT = "NONOKA_PREFERENCES_FILE"
#: Set on a child pinned to the direct route, so the policy it installs for
#: itself does not put the proxy straight back.
DIRECT_ENVIRONMENT = "NONOKA_DOWNLOAD_DIRECT"


@dataclass(frozen=True)
class NetworkRoute:
    label: str
    proxy: str | None


class NetworkConnectionError(httpx.ConnectError):
    """Raised after every usable proxy route and direct access have failed."""


# -- proxy discovery (finesub_bootstrap/http_client.py) ---------------------------------------------------------


def normalise_proxy(value: str, scheme: str = "http") -> str:
    value = value.strip()
    if "://" not in value:
        value = f"{scheme}://{value}"
    return value


def without_userinfo(proxy: str) -> str:
    """`http://user:pw@host:port` -> `http://***@host:port`, for a label.

    The label goes into error text and logs; the proxy itself keeps its
    credentials for the request.
    """

    parts = urlsplit(proxy)
    if "@" not in parts.netloc:
        return proxy
    host = parts.netloc.rpartition("@")[2]
    return urlunsplit(parts._replace(netloc=f"***@{host}"))


def windows_proxy() -> str | None:
    """The Windows system proxy from the registry, or None (SOCKS-only counts as none)."""

    if winreg is None:
        return None
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled = int(winreg.QueryValueEx(key, "ProxyEnable")[0])
            server = str(winreg.QueryValueEx(key, "ProxyServer")[0]).strip()
    except (OSError, TypeError, ValueError):
        return None
    if not enabled or not server:
        return None

    if "=" not in server:
        return normalise_proxy(server)

    entries: dict[str, str] = {}
    for item in server.split(";"):
        protocol, separator, address = item.partition("=")
        if separator and address.strip():
            entries[protocol.strip().lower()] = address.strip()
    # A SOCKS-only system proxy is not used: such a machine downloads directly.
    for protocol in ("https", "http"):
        address = entries.get(protocol)
        if address:
            return normalise_proxy(address)
    return None


def environment_proxies() -> list[str]:
    """The proxy variables of this process, de-duplicated, in `PROXY_NAMES` order."""

    values: list[str] = []
    for name in PROXY_NAMES:
        value = os.environ.get(name, "").strip()
        if value and value not in values:
            values.append(value)
    return values


def local_proxy_is_listening(proxy: str) -> bool:
    """False only for a local proxy whose client is not running; a remote one is assumed up."""

    parsed = urlparse(proxy)
    host = parsed.hostname
    try:
        port = parsed.port
    except ValueError:
        return False
    if not host or not port:
        return False
    if host.lower() not in _LOCAL_HOST_NAMES:
        return True
    try:
        with socket.create_connection((host, port), timeout=0.35):
            return True
    except OSError:
        return False


# -- the app's proxy setting (was `nonoka_x.download_route.install_app_proxy_policy`) ----------------------------

#: (path, mtime_ns, size, value) of the last preferences.json read.
_custom_cache: tuple[str, int, int, str | None] | None = None
#: The environment's proxies as they were before the app policy started
#: rewriting them; None while the policy is not installed.
_ambient: list[str] | None = None


def custom_proxy() -> str | None:
    """The custom proxy saved on the settings page, as a URL, or None.

    Read from preferences.json on every call, behind an mtime check, so a save
    on the settings page reaches the next download or LLM call of a process that
    is already running.
    """

    global _custom_cache
    path = os.environ.get(PREFERENCES_ENVIRONMENT, "").strip()
    if not path:
        return None
    try:
        stat = os.stat(path)
    except OSError:
        return None
    cached = _custom_cache
    if cached is not None and cached[:3] == (path, stat.st_mtime_ns, stat.st_size):
        return cached[3]
    value: str | None = None
    try:
        entry = json.loads(Path(path).read_text(encoding="utf-8")).get("proxy")
        value = custom_proxy_url(entry)
    except (OSError, ValueError, AttributeError):
        value = None
    _custom_cache = (path, stat.st_mtime_ns, stat.st_size, value)
    return value


def custom_proxy_url(entry: Any) -> str | None:
    """Validate the `proxy` entry the way the desktop's `netproxy.Custom` does."""

    if not isinstance(entry, dict):
        return None
    scheme = str(entry.get("scheme") or "http").strip().lower()
    host = str(entry.get("host") or "127.0.0.1").strip().strip("[]")
    port = entry.get("port")
    if scheme not in {"http", "socks5"} or isinstance(port, bool) or not isinstance(port, int):
        return None
    if not 1 <= port <= 65535 or any(character in host for character in "/@?# "):
        return None
    if ":" in host:
        host = f"[{host}]"
    return f"{scheme}://{host}:{port}"


def install_app_policy() -> bool:
    """Make this process's route follow the app's proxy setting; whether it now does.

    A no-op outside the desktop (no preferences file named). Idempotent: the
    environment's own proxies are remembered the first time only, because from
    then on the policy itself writes those variables.
    """

    global _ambient
    if _ambient is not None:
        return True
    if not os.environ.get(PREFERENCES_ENVIRONMENT, "").strip():
        return False
    _ambient = list(dict.fromkeys(value for value in (os.environ.get(name, "").strip() for name in PROXY_NAMES) if value))
    return True


def app_policy_installed() -> bool:
    return _ambient is not None


def pinned_direct() -> bool:
    """Whether this process was started pinned to the direct route (`DIRECT_ENVIRONMENT`)."""

    return bool(os.environ.get(DIRECT_ENVIRONMENT, "").strip())


def policy_system_proxy() -> str | None:
    """The system-proxy slot under the app policy: the custom proxy if one is saved."""

    if pinned_direct():
        return None
    custom = custom_proxy()
    return custom if custom else windows_proxy()


def _policy_candidates() -> list[str]:
    if pinned_direct():
        return []
    custom = custom_proxy()
    if custom:
        return [custom]
    system = windows_proxy()
    return ([system] if system else []) + list(_ambient or ())


def policy_environment_proxies() -> list[str]:
    """The environment-proxy slot under the app policy, settling `os.environ` on the way.

    The live candidate is written into the environment (`sync_environment`), so
    in-process clients that read it and every child spawned from a copy of it
    follow the same route. A custom proxy, or a pin to direct, leaves the
    environment's own proxies out of the candidates.
    """

    live = next(
        (proxy for proxy in map(normalise_proxy, _policy_candidates()) if local_proxy_is_listening(proxy)),
        None,
    )
    sync_environment(live)
    return [] if pinned_direct() or custom_proxy() else list(_ambient or ())


def sync_environment(proxy: str | None) -> None:
    """Spell the route out in `os.environ`: the proxy variables, or `NO_PROXY=*` for direct."""

    for name in PROXY_NAMES:
        if proxy:
            os.environ[name] = proxy
        else:
            os.environ.pop(name, None)
    no_proxy = LOCAL_HOSTS if proxy else "*"
    os.environ["NO_PROXY"] = no_proxy
    os.environ["no_proxy"] = no_proxy


# -- routes ------------------------------------------------------------------------------------------------------


def network_routes() -> list[NetworkRoute]:
    """Live proxy routes followed by a direct-access fallback."""

    routes: list[NetworkRoute] = []
    seen: set[str] = set()
    policy = app_policy_installed()

    system_proxy = policy_system_proxy() if policy else windows_proxy()
    environment = policy_environment_proxies() if policy else environment_proxies()
    candidates = ([("Windows 系统代理", system_proxy)] if system_proxy else []) + [
        ("环境代理", value) for value in environment
    ]

    for label, value in candidates:
        if value is None:
            continue
        proxy = normalise_proxy(value)
        if proxy in seen or not local_proxy_is_listening(proxy):
            continue
        seen.add(proxy)
        routes.append(NetworkRoute(f"{label} {without_userinfo(proxy)}", proxy))

    routes.append(NetworkRoute("直连", None))
    return routes


def live_proxy() -> str | None:
    """The proxy downloads would go through right now, or None for direct."""

    try:
        return network_routes()[0].proxy
    except Exception:  # noqa: BLE001 - deciding a route must never stop a download
        return None


def apply_proxy_environment(environment: dict[str, str], route: NetworkRoute | None = None) -> None:
    """Hand the live proxy (or `route`) to a child process through the proxy variables.

    Much of what children run does not read the Windows registry: uv, and
    Hugging Face's Rust Xet transfer, only read the environment. So a child
    that downloads is told explicitly, the same route `network_routes` gives
    this process. Under the app policy, resolving the route just brought this
    process's `NO_PROXY` up to date; a copy taken before that still holds the
    old one, so it is carried over too.
    """

    try:
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            environment.pop(name, None)
        if route is None:
            route = network_routes()[0]
        if route.proxy is not None:
            environment["HTTP_PROXY"] = route.proxy
            environment["HTTPS_PROXY"] = route.proxy
            environment["ALL_PROXY"] = route.proxy
    except Exception:  # noqa: BLE001 - deciding a route must never stop a download
        return
    if os.environ.get(PREFERENCES_ENVIRONMENT, "").strip():
        for name in ("NO_PROXY", "no_proxy"):
            if name in os.environ:
                environment[name] = os.environ[name]


def proxy_label(proxy: str | None) -> str:
    """How the proxy is named to the user: `本地代理 127.0.0.1:7890`."""

    if not proxy:
        return ""
    parsed = urlparse(proxy)
    address = parsed.netloc.rpartition("@")[2] or proxy
    local = (parsed.hostname or "").lower() in _LOCAL_HOST_NAMES
    return f"{'本地代理' if local else '代理'} {address}"


# -- clients (finesub_bootstrap/http_client.py) ------------------------------------------------------------------


def create_client(
    route: NetworkRoute,
    *,
    timeout: httpx.Timeout,
    headers: dict[str, str] | None = None,
) -> httpx.Client:
    """An httpx client pinned to one route: its proxy or none, never the environment's."""

    return httpx.Client(
        proxy=route.proxy,
        trust_env=False,
        headers=headers,
        follow_redirects=True,
        timeout=timeout,
        event_hooks={"request": [watch_tunnel]},
    )


_TUNNEL_WATCH = "nonoka_tunnel_watch"
_HEADERS_ENDED = ("http11.receive_response_headers.complete", "http11.receive_response_headers.failed")


class TunnelWatch:
    """An httpcore `trace` hook that keeps a proxy's CONNECT exchange in the connect phase.

    Through a proxy, an HTTPS request first sends `CONNECT host:443` and waits
    for the proxy's reply, and httpcore waits for that reply with the *read*
    timeout: a proxy that accepts the connection but never answers cost the
    whole read timeout and ended as a `ReadTimeout`. This waits for that one
    reply with the connect timeout instead and marks the request while it does,
    so `in_tunnel_setup` sees the timeout for what it is.

    Keyed on the CONNECT request itself, not on the order of events. If an
    httpcore release renames the event this stays inert and the old behaviour
    returns.
    """

    def __init__(self, chained: Callable[[str, dict], Any] | None = None) -> None:
        self.pending = False
        self._chained = chained
        self._saved_read: tuple[dict, Any] | None = None

    def __call__(self, name: str, info: dict) -> None:
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
    """An httpx request hook that gives every HTTPS request a `TunnelWatch`."""

    if request.url.scheme == "https" and _TUNNEL_WATCH not in request.extensions:
        watch = TunnelWatch(request.extensions.get("trace"))
        request.extensions["trace"] = watch
        request.extensions[_TUNNEL_WATCH] = watch


def in_tunnel_setup(exc: BaseException) -> bool:
    """A timeout while a proxy's CONNECT reply was still awaited (`TunnelWatch`)."""

    if not isinstance(exc, httpx.TimeoutException):
        return False
    request = getattr(exc, "_request", None)
    watch = request.extensions.get(_TUNNEL_WATCH) if request is not None else None
    return watch is not None and watch.pending


def is_connection_failure(error: BaseException) -> bool:
    return isinstance(error, httpx.TransportError)


def connection_error(attempts: list[tuple[str, BaseException]]) -> NetworkConnectionError:
    details = "；".join(f"{label}: {error}" for label, error in attempts)
    return NetworkConnectionError(f"所有连接方式均失败（{details}）")


# -- verified downloads (finesub_bootstrap/downloader.py, models.py) --------------------------------------------


@dataclass(frozen=True)
class DownloadAsset:
    """One file to fetch: where from, and the exact bytes expected."""

    url: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        # The same checks as FineSub's pydantic validators.
        parsed = urlparse(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("asset URL must use http or https")
        if self.size < 0:
            raise ValueError("asset size must not be negative")
        normalized = self.sha256.lower()
        if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
            raise ValueError("sha256 must contain 64 hexadecimal characters")
        object.__setattr__(self, "sha256", normalized)


@dataclass(frozen=True)
class DownloadProgress:
    downloaded: int
    total: int
    bytes_per_second: float


class DownloadError(RuntimeError):
    """Base class for verified download failures."""


class SizeMismatch(DownloadError):
    pass


class DigestMismatch(DownloadError):
    pass


class DownloadPaused(DownloadError):
    pass


ProgressCallback = Callable[[DownloadProgress], None]
PauseCheck = Callable[[], bool]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def part_path(destination: Path) -> Path:
    """Where the resumable partial of `destination` lives."""

    return destination.with_suffix(f"{destination.suffix}.part")


def _expectation_path(partial: Path) -> Path:
    return Path(f"{partial}.expect")


def _discard_mismatched_part(partial: Path, asset: DownloadAsset) -> None:
    """Drop a partial that was heading for different bytes than we want now.

    A `.part` can outlive the session that began it, and by then the target can
    have moved. Appending the tail of one build onto the head of another yields
    a file that can only fail the digest check after the whole download, so a
    partial is worth nothing once the target changes. Recording what each
    partial is aimed at is what lets us tell.
    """

    expectation = _expectation_path(partial)
    if partial.is_file():
        recorded = expectation.read_text(encoding="utf-8").strip() if expectation.is_file() else ""
        if recorded != asset.sha256:
            partial.unlink()
    else:
        expectation.unlink(missing_ok=True)
    expectation.write_text(asset.sha256, encoding="utf-8")


def download_file(
    asset: DownloadAsset,
    destination: Path,
    progress: ProgressCallback | None = None,
    should_pause: PauseCheck | None = None,
    *,
    routes: Callable[[], list[NetworkRoute]] | None = None,
) -> Path:
    """Fetch `asset` to `destination`, resuming a partial and verifying size and sha256.

    Tries each route of `network_routes()` (or `routes()`) in turn, moving on
    only for a transport failure. A file already at `destination` with the
    right digest is kept as is. Wrong bytes are moved aside as `.part.bad` and
    reported as `DigestMismatch`; a short or long body as `SizeMismatch`.
    """

    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    report = progress if progress is not None else (lambda _progress: None)
    # Two processes can want the same file at once. Serializing them costs
    # nothing (the loser finds the finished file and returns) and is the only
    # way to keep one resumable `.part` file.
    with holding_lock(
        destination.with_suffix(f"{destination.suffix}.lock"),
        waiting_message="Another process is downloading the same file; waiting for it to finish",
        should_pause=should_pause,
        on_pause=lambda: DownloadPaused("Download paused"),
    ):
        return _download_locked(asset, destination, report, should_pause, routes or network_routes)


def _download_locked(
    asset: DownloadAsset,
    destination: Path,
    progress: ProgressCallback,
    should_pause: PauseCheck | None,
    routes: Callable[[], list[NetworkRoute]],
) -> Path:
    if destination.is_file() and sha256_file(destination) == asset.sha256:
        # Already here: either a previous run got it, or the process we just waited for did.
        progress(DownloadProgress(downloaded=asset.size, total=asset.size, bytes_per_second=0.0))
        return destination
    partial = part_path(destination)
    _discard_mismatched_part(partial, asset)
    existing = partial.stat().st_size if partial.is_file() else 0
    if existing > asset.size:
        partial.write_bytes(b"")
        existing = 0

    started = time.perf_counter()
    transferred = 0
    if should_pause is not None and should_pause():
        raise DownloadPaused("Download paused")
    if existing < asset.size:
        timeout = httpx.Timeout(connect=CONNECT_TIMEOUT_SEC, read=120.0, write=30.0, pool=20.0)
        attempts: list[tuple[str, BaseException]] = []
        for route in routes():
            resume_at = partial.stat().st_size if partial.is_file() else 0
            if resume_at > asset.size:
                partial.write_bytes(b"")
                resume_at = 0
            headers = {"Range": f"bytes={resume_at}-"} if resume_at else {}
            try:
                with create_client(route, timeout=timeout) as client:
                    with client.stream("GET", asset.url, headers=headers) as response:
                        response.raise_for_status()
                        append = bool(resume_at and response.status_code == 206)
                        downloaded = resume_at if append else 0
                        mode = "ab" if append else "wb"
                        with partial.open(mode) as target:
                            for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                                if should_pause is not None and should_pause():
                                    raise DownloadPaused("Download paused")
                                if not chunk:
                                    continue
                                target.write(chunk)
                                downloaded += len(chunk)
                                transferred += len(chunk)
                                elapsed = max(time.perf_counter() - started, 1e-6)
                                progress(
                                    DownloadProgress(
                                        downloaded=downloaded,
                                        total=asset.size,
                                        bytes_per_second=transferred / elapsed,
                                    )
                                )
                                if should_pause is not None and should_pause():
                                    raise DownloadPaused("Download paused")
                break
            except Exception as error:
                if not is_connection_failure(error):
                    raise
                attempts.append((route.label, error))
        else:
            raise connection_error(attempts)

    if should_pause is not None and should_pause():
        raise DownloadPaused("Download paused")
    actual_size = partial.stat().st_size if partial.is_file() else 0
    if actual_size != asset.size:
        raise SizeMismatch(f"Expected {asset.size} bytes for {asset.url}, received {actual_size}")
    actual_digest = sha256_file(partial)
    if actual_digest != asset.sha256:
        quarantine = partial.with_suffix(f"{partial.suffix}.bad")
        # Bare on purpose: a digest mismatch is already the failure being
        # reported, and waiting on the rename would only delay saying so.
        os.replace(partial, quarantine)
        _expectation_path(partial).unlink(missing_ok=True)
        raise DigestMismatch(f"Expected SHA-256 {asset.sha256}, received {actual_digest}")

    replace_path(partial, destination)
    _expectation_path(partial).unlink(missing_ok=True)
    elapsed = max(time.perf_counter() - started, 1e-6)
    progress(DownloadProgress(downloaded=asset.size, total=asset.size, bytes_per_second=transferred / elapsed))
    return destination


__all__ = [
    "CONNECT_TIMEOUT_SEC",
    "DIRECT_ENVIRONMENT",
    "LOCAL_HOSTS",
    "PREFERENCES_ENVIRONMENT",
    "PROXY_NAMES",
    "DigestMismatch",
    "DownloadAsset",
    "DownloadError",
    "DownloadPaused",
    "DownloadProgress",
    "NetworkConnectionError",
    "NetworkRoute",
    "SizeMismatch",
    "apply_proxy_environment",
    "create_client",
    "custom_proxy",
    "download_file",
    "in_tunnel_setup",
    "install_app_policy",
    "live_proxy",
    "network_routes",
    "proxy_label",
    "sha256_file",
]
