"""`finesub_bootstrap.locks` for the finesub-correct package: only the names its FineSub code uses (WP-P1).

Copied from FineSub v0.7.0 (`7123055`), `src/finesub_bootstrap/locks.py`, as
vendored in Nonoka X (the transcription package carries the original). Kept: the
top-level definitions the names below need, unchanged and in the original's order;
everything else is left out. `__all__` is exactly the names this package's FineSub
code imports (docs/engine-packages.md section 4); `tests/test_engine_packages.py`
holds this package to them.

The original's description:

    Advisory cross-process locks over a sidecar file.

    FineSub runs as several independent processes against one shared tree: the
    desktop app, the CLI shell, the packaged command line, and worker subprocesses.
    Anything that reads a location, decides something, and writes it back needs to
    serialize across all of them -- the runtime swap, the data migrations, the
    knowledge base's auto-commit, the big-data location record.

    Waiting is polled rather than blocking so a pause request still gets through,
    and the sidecar file is never deleted: removing it would let a process that
    still holds the byte lock coexist with one that just created a fresh file.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any
import uuid

__all__ = ["AGENT_ACTIVITY_ROOT_VARIABLE", "AGENT_CAPSULE_ROOT_VARIABLE", "AGENT_IDENTITY_ANCHOR_VARIABLE", "AGENT_LOCATOR_KIND_MANAGED", "AGENT_LOCATOR_KIND_VARIABLE", "LockUnavailable", "holding_activity", "holding_lock"]


LogCallback = Callable[[str], None]


PauseCheck = Callable[[], bool]


POLL_SECONDS = 0.5


#: Stable coordination lives in user-data, not under the big-data tree that a
#: relocation renames. Every new run briefly takes the gate while publishing a
#: unique lease; relocation holds it for the whole move, so no run can appear
#: between the liveness check and the rename.
ACTIVITY_GATE_NAME = ".task-activity.lock"


ACTIVITY_LEASE_DIRECTORY = ".task-activity"


# A managed worker cannot always rediscover its launcher layout from source
# location alone: the thin CLI runs vendored sources from site-packages, and a
# custom FINESUB_HOME is intentionally unrelated to that path. The launcher
# therefore hands the unified agent resolver this one complete location tuple.
AGENT_CAPSULE_ROOT_VARIABLE = "FINESUB_AGENT_CAPSULE_ROOT"


AGENT_ACTIVITY_ROOT_VARIABLE = "FINESUB_AGENT_ACTIVITY_ROOT"


AGENT_IDENTITY_ANCHOR_VARIABLE = "FINESUB_AGENT_IDENTITY_ANCHOR"


AGENT_LOCATOR_KIND_VARIABLE = "FINESUB_AGENT_LOCATOR_KIND"


# `finesub.llm.agent.agent_paths` refuses a locator kind it does not recognise, so the two
# launchers that set the variable must not each spell it out. It is declared
# here because bootstrap may not import `llm`, only the other way round.
AGENT_LOCATOR_KIND_MANAGED = "managed_big_data"


def activity_gate_path(coordination_root: Path) -> Path:
    return Path(coordination_root) / ACTIVITY_GATE_NAME


def activity_lease_path(coordination_root: Path, lease_id: str) -> Path:
    digest = hashlib.sha256(lease_id.encode("utf-8")).hexdigest()
    return (
        Path(coordination_root)
        / ACTIVITY_LEASE_DIRECTORY
        / f"{digest}.lock"
    )


class LockUnavailable(RuntimeError):
    """Raised by `holding_lock` when `timeout` elapses without the lock."""


def _acquire(handle) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _release(handle) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _write_lease(lock_path: Path, lease: Mapping[str, Any]) -> None:
    """Best effort: a tasks root nobody can write to keeps today's behaviour."""

    try:
        lease_path(lock_path).write_text(
            json.dumps(dict(lease), ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


def lease_path(lock_path: Path) -> Path:
    """The metadata file beside one lock: who is holding it, and since when.

    Beside rather than inside: the lock file is opened and locked by every
    process that so much as asks whether a task is busy, and a reader that has
    to take the lock to learn who holds it learns nothing -- it cannot take it.
    """

    return lock_path.with_suffix(".json")


@contextmanager
def holding_lock(
    lock_path: Path,
    *,
    waiting_message: str = "",
    log: LogCallback | None = None,
    should_pause: PauseCheck | None = None,
    timeout: float | None = None,
    on_pause: Callable[[], BaseException] | None = None,
    lease: Mapping[str, Any] | None = None,
) -> Iterator[None]:
    """Hold the lock at `lock_path`, waiting for it.

    `timeout` of None waits indefinitely; a number raises `LockUnavailable`
    once it elapses, which is how callers that must not block the user
    (the knowledge base, whose failures degrade to warnings) give up.

    `lease` is written beside the lock while it is held and removed on the way
    out, so that whoever finds the lock taken can say *who* has it. It is
    commentary, not control: writing it is best effort, and nothing reads it to
    decide whether work may proceed.
    """

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        announced = False
        while True:
            if _acquire(handle):
                break
            if should_pause is not None and should_pause():
                raise (
                    on_pause() if on_pause is not None else LockUnavailable(
                        f"Paused while waiting for {lock_path.name}"
                    )
                )
            if deadline is not None and time.monotonic() >= deadline:
                raise LockUnavailable(
                    f"Timed out waiting for {lock_path.name}"
                )
            if not announced and log is not None and waiting_message:
                log(waiting_message)
            announced = True
            time.sleep(POLL_SECONDS)
        if lease is not None:
            _write_lease(lock_path, lease)
        try:
            yield
        finally:
            if lease is not None:
                # Best effort like the write: a sidecar that cannot be removed
                # must not turn a finished task into a failure, and the lock
                # below has to be released no matter what. A leftover lease
                # beside a free lock is exactly what readers already treat as
                # commentary, not evidence.
                try:
                    lease_path(lock_path).unlink(missing_ok=True)
                except OSError:
                    pass
            _release(handle)
    finally:
        handle.close()


@contextmanager
def holding_activity(
    coordination_root: Path,
    *,
    lease_id: str | None = None,
    timeout: float | None = 10,
) -> Iterator[None]:
    """Publish one independently lockable running-process lease.

    Registration is serialized with relocation by the short-lived gate. The
    lease itself is held for the body, so any number of unrelated tasks can be
    represented at once and a crashed process is released by the OS.
    """

    identifier = lease_id or f"{os.getpid()}-{uuid.uuid4().hex}"
    lease = activity_lease_path(coordination_root, identifier)
    lease_stack = ExitStack()
    with holding_lock(activity_gate_path(coordination_root), timeout=timeout):
        lease_stack.enter_context(holding_lock(lease, timeout=0))
    try:
        yield
    finally:
        lease_stack.close()
        try:
            with holding_lock(
                activity_gate_path(coordination_root), timeout=0
            ):
                lease.unlink(missing_ok=True)
        except (OSError, LockUnavailable):
            # A later barrier removes this now-free, uniquely named lease.
            pass
