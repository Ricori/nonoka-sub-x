"""Which route this process and the engine's API calls take: the app's proxy setting.

Which proxy follows the desktop's settings page (`install_app_proxy_policy`): a
custom proxy saved in preferences.json replaces the system proxy and the
environment outright; without one, the system proxy and then the environment.
The Go half (`desktop/internal/netproxy`, `desktop/internal/app/download_route.go`)
applies the same rule to everything it sends, so every request from the app
takes one route.

There is no mirror and no route choice by region (plan 4.2 item 18): users
whose route to the official sources is poor rely on their own local proxy, which
the settings page names (`nonoka_x.runtime.fetch`, R0 document D17). FineSub on
its own picks `cn` or `global` per machine and probes a country endpoint to do
so; each engine package's bridge pins that answer to `global` in its own
process.

The engine downloads nothing any more (WP-R7: the models come in the prebuilt
model layer), so the route ladder its file and model fetches used to climb --
the offline package, the live proxy, then direct, with a crawl abandoned for the
next rung -- is gone with them. The prebuilt layers have their own downloader
(`nonoka_x.runtime.layers`, `nonoka_x.runtime.fetch`), which reads the offline
package itself.
"""

from __future__ import annotations

from nonoka_x.runtime import fetch
from nonoka_x.runtime.fetch import (
    DIRECT_ENVIRONMENT,
    LOCAL_HOSTS,
    PREFERENCES_ENVIRONMENT,
    PROXY_NAMES,
    custom_proxy,
    proxy_label,
)

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
    "install_app_proxy_policy",
    "live_proxy",
]


def live_proxy() -> str | None:
    """The proxy requests would go through right now, or None for direct."""

    return fetch.live_proxy()


def apply_proxy_environment(environment: dict[str, str]) -> None:
    """Hand the live proxy to a child process through the proxy variables (`fetch.apply_proxy_environment`)."""

    fetch.apply_proxy_environment(environment, None)


def install_app_proxy_policy() -> None:
    """Make this process's route follow the app's proxy setting.

    `nonoka_x.runtime.fetch.install_app_policy`: a custom proxy is the only
    candidate; without one, the system proxy and the environment. The
    liveness probe still skips a local proxy whose client is not running.
    `os.environ` is kept on that route, so in-process clients that read the
    environment and every child spawned from a copy of it follow too. Direct
    is spelled `NO_PROXY=*`, which stops Python and Rust clients from falling
    back to the registry's system proxy. An engine package's process is told
    the route in its launch context (`nonoka_x.engine_contexts.proxy_context`)
    and applies it itself.

    Idempotent; a no-op outside the desktop (no preferences file named).
    """

    if not fetch.install_app_policy():
        return
    # Settle the environment now, before anything reads it.
    live_proxy()
