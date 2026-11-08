"""One ladder for every download the environment makes.

Every resource -- the managed tools, the runtime's wheels and interpreter, the
models -- tries the same rungs in the same order:

1. the official source, through the live proxy if there is one;
2. the official source, directly;
3. a mirror, directly: gh-proxy.com for GitHub files, the cn lock's hosts for
   PyPI and PyTorch wheels, hf-mirror for Hugging Face. A resource with no
   mirror skips this rung;
4. an error telling the user to turn a proxy on.

FineSub on its own picks `cn` or `global` per machine and goes mirror-first on a
mainland exit. Nothing here asks, and `FINESUB_DOWNLOAD_REGION` is not honoured:
`resolve_region` always answers `global`, so no engine call site reaches for a
mirror by itself, and the rungs are added at the points its downloads funnel
through (`_download_locked`, `_fetch_unverified`, `fetch_with_fallback`;
`runtime_install` does the same for uv).

A rung that works but crawls is abandoned too: under 512 KB/s for a whole window
(`SpeedWatch`), the download moves on and resumes what it has. The last rung has
nowhere to move to, so it is left to finish, and a rung abandoned for speed is
tried again unwatched if every other rung fails outright: slow beats nothing.
Bytes are counted in flight wherever they can be. uv reports none, so the steps
it runs are timed with a short sample before they start (`ensure_fast_enough`).

The mirror is the last rung, so FineSub's per-machine degrade rule does not
apply to it: giving up on the last resort would leave nothing but the error.

Which proxy rung 1 uses follows the desktop's settings page
(`install_app_proxy_policy`): a custom proxy saved in preferences.json replaces
the system proxy and the environment outright; without one, the system proxy and
then the environment, as FineSub already does. The Go half
(`desktop/internal/netproxy`, `desktop/internal/app/download_route.go`) applies
the same rule to everything it sends, so every request from the app takes one
route.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
import os
from pathlib import Path
import time
from typing import Any, TypeVar
from urllib.parse import urlparse

import httpx

_INSTALLED_ATTRIBUTE = "_nonoka_proxy_route_policy"
_APP_POLICY_ATTRIBUTE = "_nonoka_app_proxy_policy"
_LADDER_ATTRIBUTE = "_nonoka_download_ladder"

_T = TypeVar("_T")

#: The GitHub rung. `FINESUB_GITHUB_FILE_PROXY` replaces it; emptied, it removes it.
GITHUB_MIRROR = "https://gh-proxy.com/"
_GITHUB_HOSTS = ("https://github.com/", "https://raw.githubusercontent.com/")
_PYTHON_BUILD_RELEASES = "https://github.com/astral-sh/python-build-standalone/releases/download"
#: Times the route to GitHub before uv fetches an interpreter. An immutable
#: release from the same repository; which one does not matter, only the route.
PYTHON_BUILD_SAMPLE = (
    f"{_PYTHON_BUILD_RELEASES}/20250317/cpython-3.12.9%2B20250317-x86_64-pc-windows-msvc-install_only_stripped.tar.gz"
)

UNREACHABLE_MESSAGE = "连不上下载源，请开启代理后重试"

#: Below this for a whole window, a rung is abandoned for the next one. A window
#: rather than an instant so a passing dip does not throw away a good route.
MIN_BYTES_PER_SECOND = 512 * 1024
SLOW_WINDOW_SECONDS = 30.0
#: How long a sample gets to show the floor before uv is started on a rung.
PROBE_SECONDS = 8.0

#: Set on a child pinned to a direct rung, so the proxy policy it installs for
#: itself does not put the proxy straight back.
DIRECT_ENVIRONMENT = "NONOKA_DOWNLOAD_DIRECT"

#: What a transport failure looks like once only its text survives. FineSub's
#: markers are English words, and on a Chinese Windows the OS half of the message
#: is not, so these key on what every locale keeps: the client's own wording and
#: the Winsock error numbers.
_TRANSPORT_MARKERS = (
    "error sending request",
    "failed to fetch",
    "request failed after",
    "client error (connect)",
    "tcp connect",
    "dns error",
    "connecterror",
    "proxyerror",
    "protocolerror",
    "getaddrinfo",
    "error 10053",
    "error 10054",
    "error 10060",
    "error 10061",
    "error 11001",
    "error 11004",
)
_LOCAL_OS_ERRORS = ("(os error 5)", "(os error 28)", "(os error 112)")

#: preferences.json, set by the sidecar for itself and every child it spawns.
#: Unset (tests, a hand-run worker) the app policy stays out of the way.
PREFERENCES_ENVIRONMENT = "NONOKA_PREFERENCES_FILE"
_PROXY_NAMES = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")
_LOCAL_HOSTS = "localhost,127.0.0.1,::1"
_custom_cache: tuple[str, int, int, str | None] | None = None


@dataclass(frozen=True, slots=True)
class DownloadStage:
    """One rung of the ladder."""

    kind: str  # "proxy" | "direct" | "mirror"
    proxy: str | None = None

    @property
    def mirror(self) -> bool:
        return self.kind == "mirror"

    @property
    def label(self) -> str:
        if self.kind == "proxy":
            return proxy_label(self.proxy)
        return "镜像" if self.mirror else "直连官方源"


class DownloadUnreachable(RuntimeError):
    """Every rung of the ladder failed."""


class DownloadTooSlow(RuntimeError):
    """This rung works, but too slowly to stay on while another is left to try."""


class SpeedWatch:
    """Tells a transfer that crawled for a whole window from one that dipped."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._window_started: float | None = None
        self._bytes = 0

    def add(self, count: int) -> None:
        """Count bytes. The clock starts at the first one: setup is not transfer."""

        if count > 0 and self._window_started is None:
            self._window_started = self._clock()
        self._bytes += count

    def check(self) -> None:
        """Raise `DownloadTooSlow` once a full window came in under the floor."""

        if self._window_started is None:
            return
        now = self._clock()
        elapsed = now - self._window_started
        if elapsed < SLOW_WINDOW_SECONDS:
            return
        if self._bytes / elapsed < MIN_BYTES_PER_SECOND:
            raise DownloadTooSlow(f"低于 {MIN_BYTES_PER_SECOND // 1024} KB/s")
        self._window_started = now
        self._bytes = 0


class _MeteredStream(httpx.SyncByteStream):
    """A response body that answers to a `SpeedWatch`.

    At the raw socket reads, because FineSub's downloader asks for 1 MiB chunks:
    counted there, a connection crawling at a few KB/s would not be noticed for
    minutes.
    """

    def __init__(self, stream: httpx.SyncByteStream, watch: SpeedWatch) -> None:
        self._stream = stream
        self._watch = watch

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._stream:
            self._watch.add(len(chunk))
            self._watch.check()
            yield chunk

    def close(self) -> None:
        self._stream.close()


def _meter_response(response: httpx.Response) -> None:
    response.stream = _MeteredStream(response.stream, SpeedWatch())


#: The rung this download is held to, when a caller is already climbing.
_pinned_stage: ContextVar[DownloadStage | None] = ContextVar("nonoka_download_stage", default=None)
#: Whether this rung has another to move to, so a crawl is worth acting on.
_watching_speed: ContextVar[bool] = ContextVar("nonoka_download_watching_speed", default=False)


def speed_watched() -> bool:
    """Whether the rung being tried right now should give up on a crawl."""

    return _watching_speed.get()


@contextmanager
def pinned_stage(stage: DownloadStage) -> Iterator[None]:
    """Hold the file downloads inside this block to one rung."""

    token = _pinned_stage.set(stage)
    try:
        yield
    finally:
        _pinned_stage.reset(token)


def download_stages(*, has_mirror: bool) -> list[DownloadStage]:
    """The rungs for one resource, in the order they are tried."""

    proxy = live_proxy()
    stages = [DownloadStage("proxy", proxy)] if proxy else []
    stages.append(DownloadStage("direct"))
    if has_mirror:
        stages.append(DownloadStage("mirror"))
    return stages


def climb(
    stages: list[DownloadStage],
    attempt: Callable[[DownloadStage], _T],
    *,
    advances: Callable[[DownloadStage, Exception], bool],
    announce: Callable[[str], None] | None = None,
) -> _T:
    """Try each rung until one works; `advances` says which failures move on.

    A rung given up on for speed goes to the back of the queue, unwatched, so it
    is only used again once every other rung has failed outright.
    """

    failures: list[tuple[str, Exception]] = []
    pending = deque((stage, stage is not stages[-1]) for stage in stages)
    setback = ""
    while pending:
        stage, watched = pending.popleft()
        if setback and announce is not None:
            announce(f"上一条线路{setback}，正在改用{stage.label}重试")
        token = _watching_speed.set(watched)
        try:
            return attempt(stage)
        except DownloadTooSlow:
            pending.append((stage, False))
            setback = "速度过慢"
        except Exception as error:
            if not advances(stage, error):
                raise
            failures.append((stage.label, error))
            setback = "下载失败"
        finally:
            _watching_speed.reset(token)
    detail = "；".join(f"{label}：{_brief(error)}" for label, error in failures)
    raise DownloadUnreachable(f"{UNREACHABLE_MESSAGE}（{detail}）")


def ensure_fast_enough(url: str, stage: DownloadStage) -> None:
    """Sample a rung before starting a step that cannot report its own speed.

    uv prints no byte counts and goes quiet between phases, so nothing about a
    running uv tells a crawl from unpacking. Only a measured crawl or a timeout
    counts against the rung: any other failure says nothing about its speed, and
    the step itself will report it.
    """

    if not _watching_speed.get():
        return
    from finesub_bootstrap.http_client import NetworkRoute, create_client

    wanted = int(MIN_BYTES_PER_SECOND * PROBE_SECONDS)
    slow = DownloadTooSlow(f"采样低于 {MIN_BYTES_PER_SECOND // 1024} KB/s")
    try:
        route = NetworkRoute(stage.label, stage.proxy)
        with create_client(route, timeout=httpx.Timeout(PROBE_SECONDS)) as client:
            with client.stream("GET", url, headers={"Range": f"bytes=0-{wanted - 1}"}) as response:
                response.raise_for_status()
                started: float | None = None
                received = 0
                for chunk in response.iter_raw():
                    now = time.monotonic()
                    started = now if started is None else started
                    received += len(chunk)
                    if received >= wanted:
                        return
                    if now - started >= PROBE_SECONDS:
                        raise slow
    except httpx.TimeoutException as error:
        raise slow from error
    except httpx.HTTPError:
        return


def _brief(error: Exception) -> str:
    """The end of what failed: a subprocess's last words, not its exit status."""

    return str(getattr(error, "output", "") or error).strip()[-300:]


def is_transport_failure(error: BaseException) -> bool:
    """Whether a failure that only left text behind was the network's doing."""

    from finesub_bootstrap.model_fetch import LOCAL_FAILURE_MARKERS

    text = f"{error} {getattr(error, 'output', '') or ''}".lower()
    if any(marker in text for marker in (*LOCAL_FAILURE_MARKERS, *_LOCAL_OS_ERRORS)):
        return False
    return any(marker in text for marker in _TRANSPORT_MARKERS)


def stage_environment(base: Mapping[str, str], stage: DownloadStage) -> dict[str, str]:
    """A child's environment, pinned to one rung.

    Blank rather than absent when direct: FineSub merges this over `os.environ`,
    and `NO_PROXY=*` stops Python and Rust clients falling back to the registry.
    """

    environment = dict(base)
    for name in _PROXY_NAMES:
        environment[name] = stage.proxy or ""
    no_proxy = _LOCAL_HOSTS if stage.proxy else "*"
    environment["NO_PROXY"] = environment["no_proxy"] = no_proxy
    if stage.proxy:
        environment.pop(DIRECT_ENVIRONMENT, None)
    else:
        environment[DIRECT_ENVIRONMENT] = "1"
    return environment


def github_mirror_url(url: str) -> str | None:
    """`url` through the GitHub mirror, or None when it has no such rung."""

    if not url.startswith(_GITHUB_HOSTS):
        return None
    prefix = os.environ.get("FINESUB_GITHUB_FILE_PROXY", GITHUB_MIRROR).strip()
    return f"{prefix.rstrip('/')}/{url}" if prefix else None


def python_build_mirror() -> str | None:
    """`UV_PYTHON_INSTALL_MIRROR` for the mirror rung."""

    return github_mirror_url(_PYTHON_BUILD_RELEASES)


def climb_huggingface(
    fetch: Callable[[dict[str, str]], None],
    environment: Mapping[str, str],
    *,
    is_retryable: Callable[[BaseException], bool] | None = None,
    announce: Callable[[str], None] | None = None,
) -> None:
    """Fetch a Hugging Face model, one process per rung.

    One process each because `huggingface_hub` reads its endpoint at import. A
    user's own `HF_ENDPOINT` is left alone and has no mirror rung; it still
    cannot serve Xet, which `apply_xet_policy` knows.
    """

    from finesub_bootstrap import model_fetch
    from finesub_bootstrap.download_sources import mirror_for

    chosen = os.environ.get(model_fetch.HF_ENDPOINT) or ""
    mirror = "" if chosen else mirror_for(model_fetch.RESOURCE_CLASS, "cn")

    def attempt(stage: DownloadStage) -> None:
        routed = stage_environment(environment, stage)
        if stage.mirror:
            routed[model_fetch.HF_ENDPOINT] = mirror
        model_fetch.apply_xet_policy(routed, endpoint=mirror if stage.mirror else chosen)
        fetch(routed)

    def advances(_stage: DownloadStage, error: Exception) -> bool:
        return is_retryable is None or is_retryable(error) or is_transport_failure(error)

    climb(download_stages(has_mirror=bool(mirror)), attempt, advances=advances, announce=announce)


def live_proxy() -> str | None:
    """The proxy downloads would go through right now, or None for direct."""

    try:
        from finesub_bootstrap.http_client import network_routes

        return network_routes()[0].proxy
    except Exception:  # noqa: BLE001 - deciding a route must never stop a download
        return None


def proxy_label(proxy: str | None) -> str:
    """How the proxy is named to the user: `本地代理 127.0.0.1:7890`."""

    if not proxy:
        return ""
    parsed = urlparse(proxy)
    address = parsed.netloc.rpartition("@")[2] or proxy
    local = (parsed.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}
    return f"{'本地代理' if local else '代理'} {address}"


def apply_proxy_environment(environment: dict[str, str]) -> None:
    """Hand the live proxy to a child process through the proxy variables.

    Python's own HTTP clients find the Windows system proxy in the registry,
    but much of what children run does not: uv, and Hugging Face's Rust Xet
    transfer that carries the model weights, only read the environment. So a
    child that downloads is told explicitly, the same route `network_routes`
    gives this process.
    """

    try:
        from finesub_bootstrap.http_client import apply_network_environment

        apply_network_environment(environment)
    except Exception:  # noqa: BLE001 - deciding a route must never stop a download
        return
    # Resolving the route just brought this process's NO_PROXY up to date
    # (`install_app_proxy_policy`); a copy taken before that still holds the old one.
    if os.environ.get(PREFERENCES_ENVIRONMENT, "").strip():
        for name in ("NO_PROXY", "no_proxy"):
            if name in os.environ:
                environment[name] = os.environ[name]


def install_proxy_route_policy() -> None:
    """Put this process's downloads on the ladder.

    Every engine call site imports `resolve_region` from the module at call
    time, so replacing the module attribute reaches all of them in this
    process. Each process that downloads installs it for itself; it is
    idempotent and the ladder is worked out per download, so turning the proxy
    on or off takes effect at the next one without a restart.
    """

    install_app_proxy_policy()
    try:
        from finesub_bootstrap import download_routes
    except ImportError:
        return

    original = download_routes.resolve_region
    if not getattr(original, _INSTALLED_ATTRIBUTE, False):

        def resolve_region(data_root, *args: Any, **kwargs: Any):
            return download_routes.RouteDecision(region="global", source="ladder")

        setattr(resolve_region, _INSTALLED_ATTRIBUTE, True)
        resolve_region.__doc__ = original.__doc__
        download_routes.resolve_region = resolve_region
    _install_download_ladder()


def _file_advances(stage: DownloadStage, error: Exception) -> bool:
    """Which failures of a verified file download move to the next rung.

    Wrong bytes are only worth another rung when a mirror served them. From the
    official source another route would fetch the same wrong file.
    """

    from finesub_bootstrap.downloader import DownloadError, DownloadPaused
    from finesub_bootstrap.model_fetch import is_mirror_failure

    if isinstance(error, DownloadPaused):
        return False
    if isinstance(error, DownloadError):
        return stage.mirror
    return is_mirror_failure(error) or is_transport_failure(error)


def _laddered_download(download: Callable[..., _T]) -> Callable[..., _T]:
    """`_download_locked`, climbing the ladder unless a caller already is."""

    def download_locked(asset, destination, progress, should_pause):
        if _pinned_stage.get() is not None:
            return download(asset, destination, progress, should_pause)
        mirror = github_mirror_url(asset.url)

        def attempt(stage: DownloadStage):
            target = asset.model_copy(update={"url": mirror}) if stage.mirror else asset
            with pinned_stage(stage):
                return download(target, destination, progress, should_pause)

        return climb(download_stages(has_mirror=mirror is not None), attempt, advances=_file_advances)

    return download_locked


def _fingerprint(path: Path) -> tuple[int, int] | None:
    try:
        status = path.stat()
    except OSError:
        return None
    return status.st_mtime_ns, status.st_size


def _laddered_unverified(fetch: Callable[..., None]) -> Callable[..., None]:
    """`_fetch_unverified`, which reports nothing: a written file means it worked."""

    def fetch_unverified(url, destination, proxy, *, data_root=None) -> None:
        mirror = github_mirror_url(url)
        for stage in download_stages(has_mirror=mirror is not None):
            before = _fingerprint(destination)
            with pinned_stage(stage):
                fetch(mirror if stage.mirror else url, destination, "", data_root=None)
            if _fingerprint(destination) != before:
                return

    return fetch_unverified


def _install_download_ladder() -> None:
    """Route FineSub's file and model downloads through `climb`.

    `_download_locked` rather than `download_asset`, which callers import by
    name: the wrapper is then the one every caller already reaches.
    """

    from finesub_bootstrap import downloader, http_client, model_fetch

    if getattr(downloader._download_locked, _LADDER_ATTRIBUTE, False):
        return
    routes = http_client.network_routes
    create_client = downloader.create_client

    def network_routes():
        stage = _pinned_stage.get()
        return routes() if stage is None else [http_client.NetworkRoute(stage.label, stage.proxy)]

    def watched_client(route, *, timeout, headers=None):
        client = create_client(route, timeout=timeout, headers=headers)
        if _watching_speed.get():
            client.event_hooks["response"].append(_meter_response)
        return client

    def fetch_with_fallback(fetch, *, base_environment, data_root, region, is_retryable=None) -> None:
        climb_huggingface(fetch, base_environment, is_retryable=is_retryable)

    download_locked = _laddered_download(downloader._download_locked)
    setattr(download_locked, _LADDER_ATTRIBUTE, True)
    downloader._download_locked = download_locked
    downloader.network_routes = network_routes
    downloader.create_client = watched_client
    http_client.network_routes = network_routes
    model_fetch._fetch_unverified = _laddered_unverified(model_fetch._fetch_unverified)
    model_fetch.fetch_with_fallback = fetch_with_fallback


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
        value = _custom_proxy_url(entry)
    except (OSError, ValueError, AttributeError):
        value = None
    _custom_cache = (path, stat.st_mtime_ns, stat.st_size, value)
    return value


def _custom_proxy_url(entry: Any) -> str | None:
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


def install_app_proxy_policy() -> None:
    """Make FineSub's route discovery, the LLM layer and this process's
    environment follow the app's proxy setting.

    * `finesub_bootstrap.http_client`: a custom proxy is the only candidate;
      without one, the system proxy and the environment as before. Its liveness
      probe still skips a local proxy whose client is not running.
    * `finesub.llm.http`: API calls take the same route instead of httpx's own
      `trust_env` lookup. An explicit `[llm] proxy` in config.toml still wins.
    * `os.environ`: kept on that route, so in-process clients that read the
      environment (Hugging Face) and every child spawned from a copy of it
      follow too. Direct is spelled `NO_PROXY=*`, which stops Python and Rust
      clients from falling back to the registry's system proxy.

    Idempotent; a no-op outside the desktop (no preferences file named).
    """

    if not os.environ.get(PREFERENCES_ENVIRONMENT, "").strip():
        return
    try:
        from finesub_bootstrap import http_client
    except ImportError:
        return
    if getattr(http_client._environment_proxies, _APP_POLICY_ATTRIBUTE, False):
        return

    original_system = http_client._windows_proxy
    #: The environment as it was before this policy started rewriting it.
    ambient = [value for value in (os.environ.get(name, "").strip() for name in _PROXY_NAMES) if value]
    ambient = list(dict.fromkeys(ambient))

    def pinned_direct() -> bool:
        return bool(os.environ.get(DIRECT_ENVIRONMENT, "").strip())

    def candidates() -> list[str]:
        if pinned_direct():
            return []
        custom = custom_proxy()
        if custom:
            return [custom]
        system = original_system()
        return ([system] if system else []) + ambient

    def windows_proxy() -> str | None:
        if pinned_direct():
            return None
        custom = custom_proxy()
        return custom if custom else original_system()

    def environment_proxies() -> list[str]:
        live = next(
            (proxy for proxy in map(http_client._normalise_proxy, candidates()) if http_client._local_proxy_is_listening(proxy)),
            None,
        )
        _sync_environment(live)
        return [] if pinned_direct() or custom_proxy() else list(ambient)

    setattr(environment_proxies, _APP_POLICY_ATTRIBUTE, True)
    http_client._windows_proxy = windows_proxy
    http_client._environment_proxies = environment_proxies

    try:
        from finesub.llm import http as llm_http
    except Exception:  # noqa: BLE001 - the LLM layer is optional in installer processes
        llm_http = None
    if llm_http is not None:
        original_configured = llm_http.configured_proxy

        def configured_proxy() -> str | None:
            explicit = original_configured()
            if explicit is not None:
                return explicit
            return live_proxy() or llm_http.DIRECT

        llm_http.configured_proxy = configured_proxy

        # Over the route just settled: the backup's transport carries the
        # same proxy every other LLM call takes.
        from .cloud_route import install_cloud_route

        install_cloud_route()

    # Settle the environment now, before anything reads it.
    live_proxy()


def _sync_environment(proxy: str | None) -> None:
    for name in _PROXY_NAMES:
        if proxy:
            os.environ[name] = proxy
        else:
            os.environ.pop(name, None)
    no_proxy = _LOCAL_HOSTS if proxy else "*"
    os.environ["NO_PROXY"] = no_proxy
    os.environ["no_proxy"] = no_proxy
