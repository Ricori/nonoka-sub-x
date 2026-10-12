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
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

_INSTALLED_ATTRIBUTE = "_nonoka_proxy_route_policy"


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


def install_proxy_route_policy() -> None:
    """Wrap `download_routes.resolve_region` so a live proxy means `global`.

    Every engine call site imports `resolve_region` from the module at call
    time, so replacing the module attribute reaches all of them in this
    process. Each process that downloads installs it for itself; it is
    idempotent and evaluated per call, so turning the proxy on or off takes
    effect at the next download without a restart.
    """

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
