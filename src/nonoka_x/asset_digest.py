"""Ask the Nonoka cloud, not GitHub, what a moving release asset hashes to.

FineSub installs ffmpeg from BtbN's `latest` tag, whose bytes are rebuilt every
day, so instead of a pinned digest it asks GitHub's release API for one at
install time (`finesub_bootstrap.asset_resolve`). That API allows 60
unauthenticated calls an hour per IP, and a mainland user's IP is a proxy exit
or a carrier NAT shared with thousands of others: the allowance is usually spent
before the installer arrives, and the install stops with "GitHub API 速率限制
已用尽".

So the cloud asks GitHub on everyone's behalf, with its own token and at most
once every few minutes (`/releases/github/...`, nonoka-sub-cloud
`src/releases/`), and the installer reads that record first. The answer has the
same `name`/`size`/`digest` shape as GitHub's own asset entry, so
`resolve_asset` validates and pins it exactly as before. When the cloud cannot
answer -- unreachable, or no such asset -- the lookup falls through to GitHub as
FineSub always did; the cloud is a shortcut, never a new way to fail.

Mainland users are the ones this exists for, and they are also the ones whose
route to Cloudflare drops out at peak hours. So a cloud that cannot be reached
is asked again over the backup route (`nonoka_x.cloud_route`), the reverse
proxy in Tokyo, before the lookup gives up on it.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlsplit

CLOUD_BACKEND = "https://cloud.nonoka.online"

_INSTALLED_ATTRIBUTE = "_nonoka_cloud_digest_source"


def cloud_asset(owner: str, repo: str, tag: str, name: str) -> dict[str, Any] | None:
    """The cloud's entry for one release asset, or None when it has no answer."""

    try:
        import httpx

        from finesub_bootstrap.http_client import create_client, network_routes
    except ImportError:
        return None

    from .cloud_route import BACKUP_HOSTS, STATE

    path = f"/releases/github/{quote(owner, safe='')}/{quote(repo, safe='')}/{quote(tag, safe='')}"
    primary = urlsplit(CLOUD_BACKEND).hostname or ""
    backup = BACKUP_HOSTS.get(primary)
    hosts = [primary] + ([backup] if backup else [])
    if backup and STATE.prefer_backup(primary):
        hosts.reverse()
    # Short: every second spent here is added to the GitHub fallback.
    timeout = httpx.Timeout(connect=8.0, read=15.0, write=15.0, pool=8.0)
    for route in network_routes():
        for host in hosts:
            try:
                with create_client(route, timeout=timeout) as client:
                    response = client.get(f"https://{host}{path}")
                    response.raise_for_status()
                    payload = response.json()
            except httpx.TransportError:
                # Not reached: the other route to the same backend may be.
                STATE.failed(primary, host)
                continue
            except Exception:  # noqa: BLE001 - an answer; the backup is the same backend
                break
            if not isinstance(payload, dict):
                return None
            for entry in payload.get("assets") or []:
                if isinstance(entry, dict) and entry.get("name") == name:
                    return entry
            return None
    return None


def install_cloud_digest_source() -> None:
    """Wrap `asset_resolve._find_asset` so the cloud is asked before GitHub.

    `resolve_asset` looks `_find_asset` up in its module at call time, so
    replacing the module attribute reaches it. Idempotent, like
    `install_proxy_route_policy`.
    """

    try:
        from finesub_bootstrap import asset_resolve
    except ImportError:
        return

    original = asset_resolve._find_asset
    if getattr(original, _INSTALLED_ATTRIBUTE, False):
        return

    def _find_asset(owner: str, repo: str, tag: str, name: str) -> dict:
        entry = cloud_asset(owner, repo, tag, name)
        return entry if entry is not None else original(owner, repo, tag, name)

    setattr(_find_asset, _INSTALLED_ATTRIBUTE, True)
    _find_asset.__doc__ = original.__doc__
    asset_resolve._find_asset = _find_asset
