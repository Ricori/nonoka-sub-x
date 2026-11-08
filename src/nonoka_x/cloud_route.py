"""Which route to the backend the installer's cloud lookups are on.

From mainland China, China Telecom reaches the backend on Cloudflare through
Amsterdam and China Unicom through Los Angeles, and either route drops out at
peak hours. `cloud-jp.nonoka.online` is a reverse proxy in Tokyo in front of
the same backend, with far shorter routes from all three carriers.

The Go half (`desktop/internal/cloud/failover.go`) sends the desktop's own API
calls and subtitle downloads there when the primary fails, and
`asset_digest` does the same for release digests, keeping its choice here:
once the backup has had to step in, lookups keep going there for a while
before the primary gets another chance.

The engine's LLM calls are not part of this: Tokyo serves `/v1/llm` itself
and has no second route (`settings.CLOUD_LLM_BASE_URL`).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

LOGGER = logging.getLogger(__name__)

#: The backend host and the reverse proxy serving the same backend. Kept the
#: same as `backupHosts` in desktop/internal/cloud/failover.go.
BACKUP_HOSTS = {"cloud.nonoka.online": "cloud-jp.nonoka.online"}

#: How long calls keep going to the backup once it had to step in.
STICKINESS_SECONDS = 10 * 60


class RouteState:
    """Which route each primary host is on, shared across the process: what
    one lookup learnt about the route must hold for the next."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._backup_until: dict[str, float] = {}

    def prefer_backup(self, primary: str) -> bool:
        with self._lock:
            until = self._backup_until.get(primary)
            if until is None:
                return False
            if self._clock() < until:
                return True
            del self._backup_until[primary]
        LOGGER.warning("cloud route: trying the primary route to %s again", primary)
        return False

    def failed(self, primary: str, host: str) -> None:
        """`host` (the primary or its backup) just failed a call."""

        with self._lock:
            if host == primary:
                switched = primary not in self._backup_until
                self._backup_until[primary] = self._clock() + STICKINESS_SECONDS
            else:
                switched = self._backup_until.pop(primary, None) is not None
        if switched:
            target = BACKUP_HOSTS.get(primary, "?") if host == primary else primary
            LOGGER.warning("cloud route: %s failed, using %s", host, target)


STATE = RouteState()
