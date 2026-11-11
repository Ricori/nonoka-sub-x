"""One ladder for every download the environment makes.

Every resource -- the managed tools, the runtime's wheels and interpreter, the
models -- tries the same rungs in the same order. An offline package beside the
application comes before all of them (`offline_pack`); whatever it lacks climbs
as usual:

1. the official source, through the live proxy if there is one;
2. the official source, directly;
3. an error telling the user to turn a proxy on.

There is no mirror and no route choice by region (plan 4.2 item 18): users
whose route to the official sources is poor rely on their own local proxy, which
the settings page names (`nonoka_x.runtime.fetch`, R0 document D17). FineSub on
its own picks `cn` or `global` per machine and goes mirror-first on a mainland
exit; the bridge stops that (`nonoka_x.finesub_bridge.install_download_policy`,
which `install_proxy_route_policy` installs), so no engine call site reaches
for a mirror by itself.

A rung that works but crawls is abandoned too: under 512 KB/s for a whole window
(`SpeedWatch`), the download moves on and resumes what it has. The last rung has
nowhere to move to, so it is left to finish, and a rung abandoned for speed is
tried again unwatched if every other rung fails outright: slow beats nothing.
Bytes are counted in flight wherever they can be. uv reports none, so the steps
it runs are timed with a short sample before they start (`ensure_fast_enough`).

Which proxy rung 1 uses follows the desktop's settings page
(`install_app_proxy_policy`): a custom proxy saved in preferences.json replaces
the system proxy and the environment outright; without one, the system proxy and
then the environment. The Go half (`desktop/internal/netproxy`,
`desktop/internal/app/download_route.go`) applies the same rule to everything it
sends, so every request from the app takes one route.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import os
from pathlib import Path
import time
from typing import TypeVar

import httpx

from nonoka_x.runtime import fetch
from nonoka_x.runtime.fetch import (
    DIRECT_ENVIRONMENT,
    LOCAL_HOSTS,
    PREFERENCES_ENVIRONMENT,
    PROXY_NAMES,
    custom_proxy,
    proxy_label,
)

_T = TypeVar("_T")

PYTHON_BUILD_RELEASES = "https://github.com/astral-sh/python-build-standalone/releases/download"
#: Times the route to GitHub before uv fetches an interpreter. An immutable
#: release from the same repository; which one does not matter, only the route.
PYTHON_BUILD_SAMPLE = (
    f"{PYTHON_BUILD_RELEASES}/20250317/cpython-3.12.9%2B20250317-x86_64-pc-windows-msvc-install_only_stripped.tar.gz"
)

UNREACHABLE_MESSAGE = "连不上下载源，请开启代理后重试"

#: Below this for a whole window, a rung is abandoned for the next one. A window
#: rather than an instant so a passing dip does not throw away a good route.
MIN_BYTES_PER_SECOND = 512 * 1024
SLOW_WINDOW_SECONDS = 30.0
#: How long a sample gets to show the floor before uv is started on a rung.
PROBE_SECONDS = 8.0

#: Hugging Face's own variables (`huggingface_hub`).
HF_ENDPOINT = "HF_ENDPOINT"
HF_DISABLE_XET = "HF_HUB_DISABLE_XET"
HF_DISABLE_IMPLICIT_TOKEN = "HF_HUB_DISABLE_IMPLICIT_TOKEN"

#: What a transport failure looks like once only its text survives. The OS half
#: of the message follows the Windows locale, so these key on what every locale
#: keeps: the client's own wording and the Winsock error numbers.
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
#: Failures of this machine, not of the route: another rung would fail the same
#: way. The words are FineSub's (`model_fetch.LOCAL_FAILURE_MARKERS`, v0.7.0).
LOCAL_FAILURE_MARKERS = ("no space left", "disk full", "permission denied", "access is denied", "out of memory")
_LOCAL_OS_ERRORS = ("(os error 5)", "(os error 28)", "(os error 112)")

#: The offline package, set by the desktop for the sidecar and every child.
OFFLINE_PACK_ENVIRONMENT = "NONOKA_OFFLINE_PACK"

__all__ = [
    # Re-exported from `nonoka_x.runtime.fetch`, where they now live.
    "DIRECT_ENVIRONMENT",
    "LOCAL_HOSTS",
    "PREFERENCES_ENVIRONMENT",
    "PROXY_NAMES",
    "custom_proxy",
    "proxy_label",
    # Defined here.
    "apply_proxy_environment",
    "live_proxy",
    "MIN_BYTES_PER_SECOND",
    "OFFLINE_PACK_ENVIRONMENT",
    "PROBE_SECONDS",
    "PYTHON_BUILD_SAMPLE",
    "SLOW_WINDOW_SECONDS",
    "UNREACHABLE_MESSAGE",
    "DownloadStage",
    "DownloadTooSlow",
    "DownloadUnreachable",
    "SpeedWatch",
    "climb",
    "climb_huggingface",
    "current_stage",
    "download_stages",
    "ensure_fast_enough",
    "install_app_proxy_policy",
    "install_proxy_route_policy",
    "is_transport_failure",
    "meter_response",
    "offline_file",
    "offline_pack",
    "offline_python",
    "offline_snapshot",
    "pinned_stage",
    "speed_watched",
    "stage_environment",
]


@dataclass(frozen=True, slots=True)
class DownloadStage:
    """One rung of the ladder."""

    kind: str  # "local" | "proxy" | "direct"
    proxy: str | None = None

    @property
    def label(self) -> str:
        if self.kind == "proxy":
            return proxy_label(self.proxy)
        if self.kind == "local":
            return "本地资源包"
        return "直连官方源"


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

    At the raw socket reads, because the downloaders ask for 1 MiB chunks:
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


def meter_response(response: httpx.Response) -> None:
    """An httpx response hook that puts the body under a fresh `SpeedWatch`."""

    response.stream = _MeteredStream(response.stream, SpeedWatch())


#: The rung this download is held to, when a caller is already climbing.
_pinned_stage: ContextVar[DownloadStage | None] = ContextVar("nonoka_download_stage", default=None)
#: Whether this rung has another to move to, so a crawl is worth acting on.
_watching_speed: ContextVar[bool] = ContextVar("nonoka_download_watching_speed", default=False)


def speed_watched() -> bool:
    """Whether the rung being tried right now should give up on a crawl."""

    return _watching_speed.get()


def current_stage() -> DownloadStage | None:
    """The rung the downloads in this context are held to, or None."""

    return _pinned_stage.get()


@contextmanager
def pinned_stage(stage: DownloadStage) -> Iterator[None]:
    """Hold the file downloads inside this block to one rung."""

    token = _pinned_stage.set(stage)
    try:
        yield
    finally:
        _pinned_stage.reset(token)


def live_proxy() -> str | None:
    """The proxy downloads would go through right now, or None for direct.

    Inside `pinned_stage`, the pinned rung's: a download already held to one
    rung reports that rung, as FineSub's patched route lookup does.
    """

    stage = _pinned_stage.get()
    return stage.proxy if stage is not None else fetch.live_proxy()


def apply_proxy_environment(environment: dict[str, str]) -> None:
    """Hand the live proxy to a child process through the proxy variables (`fetch.apply_proxy_environment`)."""

    stage = _pinned_stage.get()
    route = None if stage is None else fetch.NetworkRoute(stage.label, stage.proxy)
    fetch.apply_proxy_environment(environment, route)


def download_stages() -> list[DownloadStage]:
    """The rungs for one resource, in the order they are tried: the live proxy, then direct."""

    proxy = live_proxy()
    stages = [DownloadStage("proxy", proxy)] if proxy else []
    stages.append(DownloadStage("direct"))
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
    wanted = int(MIN_BYTES_PER_SECOND * PROBE_SECONDS)
    slow = DownloadTooSlow(f"采样低于 {MIN_BYTES_PER_SECOND // 1024} KB/s")
    try:
        route = fetch.NetworkRoute(stage.label, stage.proxy)
        with fetch.create_client(route, timeout=httpx.Timeout(PROBE_SECONDS)) as client:
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
    for name in PROXY_NAMES:
        environment[name] = stage.proxy or ""
    no_proxy = LOCAL_HOSTS if stage.proxy else "*"
    environment["NO_PROXY"] = environment["no_proxy"] = no_proxy
    if stage.proxy:
        environment.pop(DIRECT_ENVIRONMENT, None)
    else:
        environment[DIRECT_ENVIRONMENT] = "1"
    return environment


def offline_pack() -> Path | None:
    """The offline package this application came with, or None.

    Its files are found by what they are, not by which release they belong to:
    `files/<sha256>/`, `huggingface/<repo>/<revision>/`. A package is therefore
    never out of date as a whole, and new files can simply be merged into it.
    """

    value = os.environ.get(OFFLINE_PACK_ENVIRONMENT, "").strip()
    if not value:
        return None
    root = Path(value)
    return root if (root / "pack.json").is_file() else None


def offline_file(sha256: str) -> Path | None:
    """The file with this digest in the offline package, or None."""

    root = offline_pack()
    if root is None or not sha256:
        return None
    try:
        return next((entry for entry in (root / "files" / sha256.lower()).iterdir() if entry.is_file()), None)
    except OSError:
        return None


def offline_python() -> Path | None:
    """The offline package's interpreter directory, laid out as uv's mirror."""

    root = offline_pack()
    return root / "python" if root is not None and (root / "python").is_dir() else None


def offline_snapshot(repository: str, revision: str) -> Path | None:
    """A Hugging Face snapshot in the offline package, or None."""

    root = offline_pack()
    if root is None:
        return None
    snapshot = root / "huggingface" / f"models--{repository.replace('/', '--')}" / revision
    return snapshot if snapshot.is_dir() else None


def _copy_into_place(source: Path, destination: Path, report: Callable[[int], None] = lambda _copied: None) -> None:
    """Copy through a temporary name, so a half-written file is never visible."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.offline")
    copied = 0
    try:
        with source.open("rb") as reader, temporary.open("wb") as writer:
            while chunk := reader.read(8 * 1024 * 1024):
                writer.write(chunk)
                copied += len(chunk)
                report(copied)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _apply_xet_policy(environment: dict[str, str], *, endpoint: str) -> None:
    """Turn Xet and the implicit token off whenever the traffic is aimed anywhere but the official host.

    FineSub's rule (`model_fetch.apply_mirror_policy`, v0.7.0), kept for a
    user's own `HF_ENDPOINT`: such a host does not proxy Xet, so the first
    reconstruction request comes back 401, and left to itself the library sends
    the user's own login to it. A variable the user set explicitly still wins.
    """

    for name in (HF_DISABLE_XET, HF_DISABLE_IMPLICIT_TOKEN):
        if name in os.environ:
            continue
        if endpoint:
            environment[name] = "1"
        else:
            environment.pop(name, None)


def climb_huggingface(
    fetch_model: Callable[[dict[str, str]], None],
    environment: Mapping[str, str],
    *,
    is_retryable: Callable[[BaseException], bool] | None = None,
    announce: Callable[[str], None] | None = None,
) -> None:
    """Fetch a Hugging Face model, one process per rung.

    One process each because `huggingface_hub` reads its route at import. A
    user's own `HF_ENDPOINT` is left alone; it still cannot serve Xet, which
    `_apply_xet_policy` knows.
    """

    chosen = os.environ.get(HF_ENDPOINT) or ""

    def attempt(stage: DownloadStage) -> None:
        routed = stage_environment(environment, stage)
        _apply_xet_policy(routed, endpoint=chosen)
        fetch_model(routed)

    def advances(_stage: DownloadStage, error: Exception) -> bool:
        return is_retryable is None or is_retryable(error) or is_transport_failure(error)

    climb(download_stages(), attempt, advances=advances, announce=announce)


def install_app_proxy_policy() -> None:
    """Make this process's route, FineSub's route discovery and its LLM layer
    follow the app's proxy setting.

    * this process (`nonoka_x.runtime.fetch.install_app_policy`): a custom proxy
      is the only candidate; without one, the system proxy and the environment.
      The liveness probe still skips a local proxy whose client is not running.
    * FineSub's `http_client` and `finesub.api_http` follow the same route
      (`nonoka_x.finesub_bridge.install_app_proxy_policy`). An explicit
      `[api] proxy` in config.toml still wins.
    * `os.environ`: kept on that route, so in-process clients that read the
      environment (Hugging Face) and every child spawned from a copy of it
      follow too. Direct is spelled `NO_PROXY=*`, which stops Python and Rust
      clients from falling back to the registry's system proxy.

    Idempotent; a no-op outside the desktop (no preferences file named).
    """

    if not fetch.install_app_policy():
        return
    from nonoka_x import finesub_bridge

    finesub_bridge.install_app_proxy_policy()
    # Settle the environment now, before anything reads it.
    live_proxy()


def install_proxy_route_policy() -> None:
    """Put this process's downloads, FineSub's included, on the ladder.

    The app policy first, then FineSub's half
    (`nonoka_x.finesub_bridge.install_download_policy`): its region answer is
    pinned to `global` and its file and model downloads climb `climb`.
    Idempotent; the ladder is worked out per download, so turning the proxy on
    or off takes effect at the next one without a restart.
    """

    install_app_proxy_policy()
    from nonoka_x import finesub_bridge

    finesub_bridge.install_download_policy()
