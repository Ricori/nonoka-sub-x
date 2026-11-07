"""A live proxy takes precedence over the regional mirrors.

FineSub picks `cn` or `global` by asking where this machine's traffic comes
out, and caches the answer for a day. That leaves two gaps for a mainland user
who runs a proxy client: a verdict cached before the proxy was switched on
keeps routing to the domestic mirrors, and a proxy in rule mode may send the
probe out directly while the downloads themselves go abroad. Either way the
multi-GB wheels and models come from a mirror the proxy would have beaten.

So the rule here is the one a user expects:

* a proxy is configured and answering (the Windows system proxy browsers use,
  or the usual environment variables) -> `global`: the official sources,
  fetched through that proxy -- `network_routes()` already puts it first;
* no proxy -> FineSub's own decision: mirrors on a mainland exit, the official
  sources everywhere else.

An explicit `FINESUB_DOWNLOAD_REGION` still beats both. The probe that does run
now only ever runs without a proxy, so what it caches describes the direct
connection the next proxy-less download will actually use.

Which proxy that is follows the desktop's settings page (`install_app_proxy_policy`):
a custom proxy saved in preferences.json replaces the system proxy and the
environment outright; without one, the system proxy and then the environment,
as FineSub already does. The Go half (`desktop/internal/netproxy`) applies the
same rule to everything it sends, so every request from the app takes one route.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_INSTALLED_ATTRIBUTE = "_nonoka_proxy_route_policy"
_APP_POLICY_ATTRIBUTE = "_nonoka_app_proxy_policy"

#: preferences.json, set by the sidecar for itself and every child it spawns.
#: Unset (tests, a hand-run worker) the app policy stays out of the way.
PREFERENCES_ENVIRONMENT = "NONOKA_PREFERENCES_FILE"
_PROXY_NAMES = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")
_LOCAL_HOSTS = "localhost,127.0.0.1,::1"
_custom_cache: tuple[str, int, int, str | None] | None = None


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
    """Wrap `download_routes.resolve_region` so a live proxy means `global`.

    Every engine call site imports `resolve_region` from the module at call
    time, so replacing the module attribute reaches all of them in this
    process. Each process that downloads installs it for itself; it is
    idempotent and evaluated per call, so turning the proxy on or off takes
    effect at the next download without a restart.
    """

    install_app_proxy_policy()
    try:
        from finesub_bootstrap import download_routes
    except ImportError:
        return

    original = download_routes.resolve_region
    if getattr(original, _INSTALLED_ATTRIBUTE, False):
        return

    def resolve_region(data_root, *args: Any, **kwargs: Any):
        if download_routes.forced_region() is None and live_proxy() is not None:
            return download_routes.RouteDecision(region="global", source="proxy")
        return original(data_root, *args, **kwargs)

    setattr(resolve_region, _INSTALLED_ATTRIBUTE, True)
    resolve_region.__doc__ = original.__doc__
    download_routes.resolve_region = resolve_region


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

    def candidates() -> list[str]:
        custom = custom_proxy()
        if custom:
            return [custom]
        system = original_system()
        return ([system] if system else []) + ambient

    def windows_proxy() -> str | None:
        custom = custom_proxy()
        return custom if custom else original_system()

    def environment_proxies() -> list[str]:
        live = next(
            (proxy for proxy in map(http_client._normalise_proxy, candidates()) if http_client._local_proxy_is_listening(proxy)),
            None,
        )
        _sync_environment(live)
        return [] if custom_proxy() else list(ambient)

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
