"""Where Nonoka still reaches into FineSub's own modules, in one file.

Nonoka's code outside the boundary files does not import `finesub` or
`finesub_bootstrap` (`docs/transcription-engines-plan.md` 4.2 item 12; the
boundary test `tests/test_finesub_boundary.py`). What it still has to do to
FineSub in-process -- route FineSub's downloads and API calls the way Nonoka
routes its own -- lives here, behind the same functions it had before:

- `install_app_proxy_policy`: FineSub's `http_client` discovers routes through
  Nonoka's proxy setting (`nonoka_x.runtime.fetch`), and `finesub.api_http`
  sends API calls the same way. Moved from
  `nonoka_x.download_route.install_app_proxy_policy`.
- `install_download_policy`: FineSub never picks a regional mirror by itself
  (`download_routes.resolve_region` always answers `global`), and its file and
  model downloads take Nonoka's route ladder (`nonoka_x.download_route.climb`).
  Moved from `nonoka_x.download_route.install_proxy_route_policy` and
  `_install_download_ladder`.
- `install_cloud_digest_source`: FineSub's release-asset lookup asks the Nonoka
  cloud before GitHub. Moved from `nonoka_x.asset_digest`.

The code moved verbatim (WP-R2, user decision 2026-11-11), with two changes
that follow from the same package: the ladder has no mirror or backup rung any
more, and the app policy reads the system proxy, the liveness probe and the
custom proxy from `nonoka_x.runtime.fetch` instead of FineSub's copies (the
same code). `nonoka_x.download_route` and `nonoka_x.asset_digest` keep their
public functions and call in here, so no caller changed. Everything here is
idempotent and does nothing when FineSub cannot be imported.

WP-R5 takes this file over; WP-P1 moves it into the engine packages.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import sys
from typing import Any, TypeVar
from urllib.parse import urlparse
import time

from nonoka_x import download_route
from nonoka_x.download_route import DownloadStage, DownloadUnreachable
from nonoka_x.runtime import fetch

_T = TypeVar("_T")

_INSTALLED_ATTRIBUTE = "_nonoka_proxy_route_policy"
_APP_POLICY_ATTRIBUTE = "_nonoka_app_proxy_policy"
_LADDER_ATTRIBUTE = "_nonoka_download_ladder"
_CLOUD_DIGEST_ATTRIBUTE = "_nonoka_cloud_digest_source"


# -- the app's proxy setting -------------------------------------------------------------------------------------


def install_app_proxy_policy() -> None:
    """Make FineSub's route discovery and its LLM layer follow the app's proxy setting.

    * `finesub_bootstrap.http_client`: a custom proxy is the only candidate;
      without one, the system proxy and the environment as before. Its
      liveness probe still skips a local proxy whose client is not running.
      Every route lookup also keeps `os.environ` on the live route
      (`fetch.sync_environment`).
    * `finesub.api_http`: API calls take the same route instead of httpx's own
      `trust_env` lookup. An explicit `[api] proxy` in config.toml still wins.
      A SOCKS route goes direct with a warning: the engine refuses SOCKS.

    Expects `fetch.install_app_policy()` to have run (it holds the environment's
    own proxies from before the policy started rewriting them).
    """

    try:
        from finesub_bootstrap import http_client
    except ImportError:
        return
    if getattr(http_client._environment_proxies, _APP_POLICY_ATTRIBUTE, False):
        return

    def environment_proxies() -> list[str]:
        return fetch.policy_environment_proxies()

    setattr(environment_proxies, _APP_POLICY_ATTRIBUTE, True)
    http_client._windows_proxy = fetch.policy_system_proxy
    http_client._environment_proxies = environment_proxies

    try:
        from finesub import api_http
    except Exception:  # noqa: BLE001 - the LLM layer is optional in installer processes
        api_http = None
    if api_http is not None:
        original_configured = api_http.configured_proxy
        warned_socks = False

        def configured_proxy() -> str | None:
            nonlocal warned_socks
            explicit = original_configured()
            if explicit is not None:
                return explicit
            proxy = download_route.live_proxy()
            if proxy and proxy.lower().startswith("socks"):
                if not warned_socks:
                    warned_socks = True
                    print(
                        "Warning: the transcription engine does not support SOCKS proxies; "
                        "its API calls go direct. Use the proxy's HTTP port to route them.",
                        file=sys.stderr,
                    )
                return api_http.DIRECT
            return proxy or api_http.DIRECT

        api_http.configured_proxy = configured_proxy


# -- FineSub's downloads -----------------------------------------------------------------------------------------


def install_download_policy() -> None:
    """Put FineSub's downloads in this process on Nonoka's ladder.

    Every engine call site imports `resolve_region` from the module at call
    time, so replacing the module attribute reaches all of them in this
    process. Each process that downloads installs it for itself; it is
    idempotent and the ladder is worked out per download, so turning the proxy
    on or off takes effect at the next one without a restart.
    """

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


def _seed_from_offline_pack(asset, destination: Path, progress, should_pause) -> None:
    """Put `asset` where the downloader looks first, if the offline package has it.

    The downloader then hashes it like any finished download and goes no
    further, so a package holding the wrong bytes cannot get them installed.
    """

    from finesub_bootstrap import downloader

    source = download_route.offline_file(asset.sha256)
    if source is None or (destination.is_file() and downloader._sha256(destination) == asset.sha256):
        return
    started = time.monotonic()

    def report(copied: int) -> None:
        if should_pause is not None and should_pause():
            raise downloader.DownloadPaused("Download paused")
        elapsed = max(time.monotonic() - started, 1e-6)
        progress(downloader.DownloadProgress(downloaded=copied, total=asset.size, bytes_per_second=copied / elapsed))

    download_route._copy_into_place(source, destination, report)


def _file_advances(stage: DownloadStage, error: Exception) -> bool:
    """Which failures of a verified file download move to the next rung.

    Wrong bytes never do: every rung fetches from the official source, and
    another route would fetch the same wrong file.
    """

    from finesub_bootstrap.downloader import DownloadError
    from finesub_bootstrap.model_fetch import is_mirror_failure

    if isinstance(error, DownloadError):
        return False
    return is_mirror_failure(error) or download_route.is_transport_failure(error)


def _laddered_download(download: Callable[..., _T]) -> Callable[..., _T]:
    """`_download_locked`, climbing the ladder unless a caller already is."""

    def download_locked(asset, destination, progress, should_pause):
        _seed_from_offline_pack(asset, destination, progress, should_pause)
        if download_route.current_stage() is not None:
            return download(asset, destination, progress, should_pause)

        def attempt(stage: DownloadStage):
            with download_route.pinned_stage(stage):
                return download(asset, destination, progress, should_pause)

        try:
            return download_route.climb(download_route.download_stages(), attempt, advances=_file_advances)
        except DownloadUnreachable as error:
            if download_route.offline_pack() is None:
                raise
            raise DownloadUnreachable(f"离线资源包里没有 {Path(urlparse(asset.url).path).name}，{error}") from error

    return download_locked


def _fingerprint(path: Path) -> tuple[int, int] | None:
    try:
        status = path.stat()
    except OSError:
        return None
    return status.st_mtime_ns, status.st_size


def _laddered_unverified(fetch_file: Callable[..., None]) -> Callable[..., None]:
    """`_fetch_unverified`, which reports nothing: a written file means it worked."""

    def fetch_unverified(url, destination, proxy, *, data_root=None) -> None:
        root = download_route.offline_pack()
        source = root / "unverified" / Path(urlparse(url).path).name if root is not None else None
        if source is not None and source.is_file():
            download_route._copy_into_place(source, destination)
            return
        for stage in download_route.download_stages():
            before = _fingerprint(destination)
            with download_route.pinned_stage(stage):
                fetch_file(url, destination, "", data_root=None)
            if _fingerprint(destination) != before:
                return

    return fetch_unverified


def _install_download_ladder() -> None:
    """Route FineSub's file and model downloads through `download_route.climb`.

    `_download_locked` rather than `download_asset`, which callers import by
    name: the wrapper is then the one every caller already reaches.
    """

    from finesub_bootstrap import downloader, http_client, model_fetch

    if getattr(downloader._download_locked, _LADDER_ATTRIBUTE, False):
        return
    routes = http_client.network_routes
    create_client = downloader.create_client

    def network_routes():
        stage = download_route.current_stage()
        return routes() if stage is None else [http_client.NetworkRoute(stage.label, stage.proxy)]

    def watched_client(route, *, timeout, headers=None):
        client = create_client(route, timeout=timeout, headers=headers)
        if download_route.speed_watched():
            client.event_hooks["response"].append(download_route.meter_response)
        return client

    def fetch_with_fallback(fetch_model, *, base_environment, data_root, region, is_retryable=None) -> None:
        download_route.climb_huggingface(fetch_model, base_environment, is_retryable=is_retryable)

    download_locked = _laddered_download(downloader._download_locked)
    setattr(download_locked, _LADDER_ATTRIBUTE, True)
    downloader._download_locked = download_locked
    downloader.network_routes = network_routes
    downloader.create_client = watched_client
    http_client.network_routes = network_routes
    model_fetch._fetch_unverified = _laddered_unverified(model_fetch._fetch_unverified)
    model_fetch.fetch_with_fallback = fetch_with_fallback


# -- release digests ---------------------------------------------------------------------------------------------


def install_cloud_digest_source(cloud_asset: Callable[[str, str, str, str], dict | None]) -> None:
    """Wrap `asset_resolve._find_asset` so `cloud_asset` is asked before GitHub.

    `resolve_asset` looks `_find_asset` up in its module at call time, so
    replacing the module attribute reaches it. Idempotent.
    """

    try:
        from finesub_bootstrap import asset_resolve
    except ImportError:
        return

    original = asset_resolve._find_asset
    if getattr(original, _CLOUD_DIGEST_ATTRIBUTE, False):
        return

    def _find_asset(owner: str, repo: str, tag: str, name: str) -> dict:
        entry = cloud_asset(owner, repo, tag, name)
        return entry if entry is not None else original(owner, repo, tag, name)

    setattr(_find_asset, _CLOUD_DIGEST_ATTRIBUTE, True)
    _find_asset.__doc__ = original.__doc__
    asset_resolve._find_asset = _find_asset


__all__ = ["install_app_proxy_policy", "install_cloud_digest_source", "install_download_policy"]
