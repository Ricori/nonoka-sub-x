"""Filesystem and process operations that survive Windows and the users we actually have.

Copied from FineSub v0.7.0 (`finesub_bootstrap/`, 7123055; the original now ships
in `engines/finesub-transcribe/src/finesub_bootstrap/`) so that Nonoka's own code stops importing `finesub_bootstrap`
(`docs/transcription-engines-plan.md` 4.2 item 12, WP-R2):

- `fsops.py`: renames that wait out a held handle, directory links, tree
  removal/copy/move that never follow a link, atomic writes -- copied whole;
- `locks.py`: the advisory sidecar-file lock (`holding_lock`, `try_lock`,
  `LockUnavailable`, the lease beside it). FineSub's task, activity and agent
  coordination (task locks, activity leases, the agent capsule variables) stays
  behind: it describes FineSub's own processes, not Nonoka's;
- `processes.py`: `no_window`, `which`, `terminate_process_tree` -- copied whole.

The behaviour is FineSub's, unchanged; only docstrings that named FineSub's own
front ends were trimmed. `finesub_bootstrap` itself is not modified.

Two things go wrong repeatedly, so they live in one place: directory links (a
`shutil.rmtree` walks straight through a junction into whatever the user
redirected a directory to), and interrupted moves (a cross-volume `shutil.move`
is copy-then-delete, so a crash leaves half a tree at the destination and turns
one bad moment into a state a human has to untangle).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
from typing import Any

# -- processes (finesub_bootstrap/processes.py) ----------------------------------------------------------------

#: Windows searches the working directory for an executable before PATH unless
#: `NoDefaultCurrentDirectoryInExePath` is set. `shutil.which` honours the
#: variable only from 3.12, and only when whoever called in set it, so `which`
#: does not rely on it.
_WHICH_SEARCHES_CWD = os.name == "nt"


def no_window() -> int:
    """Creation flags for a child that must not open a console window.

    Windows gives a console child the console of its parent. A parent that has
    none -- anything started windowless, a GUI host, `pythonw`, a scheduled
    task -- makes each console child allocate a new one, and that one is
    *visible*: a black box that flashes for as long as the child runs. With
    this flag the child gets a hidden console instead, and every process *it*
    starts inherits that, so the whole tree below stays quiet too.

    Not for a child that must share the user's terminal: a separate hidden
    console means it neither prints there nor receives the terminal's Ctrl+C.
    """

    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def which(name: str) -> str | None:
    """`shutil.which` that never answers with a copy found only in the working directory.

    What the launcher finds it runs, and the working directory may be a
    downloaded media folder. Where `shutil.which` still searches it first, a
    match there is looked up again along PATH alone; an entry naming that
    directory still counts, as it does on 3.12.
    """

    found = shutil.which(name)
    if found is None or not _WHICH_SEARCHES_CWD or Path(found).resolve().parent != Path.cwd().resolve():
        return found
    suffixes = [""] if Path(name).suffix else os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep)
    for entry in filter(None, os.environ.get("PATH", "").split(os.pathsep)):
        for suffix in suffixes:
            candidate = Path(entry) / f"{name}{suffix}"
            if candidate.is_file():
                return str(candidate)
    return None


def terminate_process_tree(process: Any) -> None:
    """Kill `process` and everything it started (`taskkill /T` on Windows, its group elsewhere)."""

    pid = int(process.pid)
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=no_window(),
        )
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        process.terminate()


# -- renames and directory links (finesub_bootstrap/fsops.py) --------------------------------------------------


@dataclass(frozen=True)
class ReplaceBudget:
    """How long a rename waits out whoever is still holding a name."""

    attempts: int
    backoff_seconds: float
    cap_seconds: float


#: Publishing a tree or a downloaded file. Windows denies a rename while any
#: handle is open on either name, and the holder is almost always something
#: that lets go on its own: an antivirus reading the 2.8GB just written, a sync
#: client, an ordinary reader (Python's `open` does not share deletion). Those
#: windows are short; the budget exists so a sub-second overlap cannot discard
#: minutes of download. Worst case is ~10s, paid once.
PUBLISH_REPLACE = ReplaceBudget(attempts=8, backoff_seconds=0.4, cap_seconds=2.0)

#: Records -- an index, a ledger, a settings file. Deliberately much shorter: a
#: tree is published once and losing it means downloading again, while a record
#: is rewritten on every status update, so a name that stayed locked would make
#: every later write pay the full budget.
RECORD_REPLACE = ReplaceBudget(attempts=4, backoff_seconds=0.05, cap_seconds=0.2)


def replace_path(
    source: Path | str,
    destination: Path | str,
    *,
    budget: ReplaceBudget = PUBLISH_REPLACE,
) -> None:
    """`os.replace`, waiting out a handle that is about to be released.

    Every publish in the installer ends in a rename, and on Windows that is the
    step most likely to fail for a reason unrelated to the work: the bytes are
    already written and verified, and something is merely *reading* one of the
    two names.

    Not every rename belongs here. Two in this module stay bare on purpose,
    because their failure carries information rather than costing work: the
    same-volume probe in `move_directory`, whose `OSError` *means* "different
    volume", and the quarantine rename after a digest mismatch
    (`nonoka_x.runtime.fetch`).
    """

    for attempt in range(1, budget.attempts + 1):
        try:
            os.replace(source, destination)
            return
        except OSError:
            if attempt == budget.attempts:
                raise
            time.sleep(min(budget.backoff_seconds * attempt, budget.cap_seconds))


def is_directory_link(path: Path) -> bool:
    """Whether `path` is a symlink or a junction (which `is_symlink` misses)."""

    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    return bool(is_junction(path)) if is_junction is not None else False


def _unlink_directory_link(path: Path) -> None:
    """Remove the link itself, never the tree it points at."""

    try:
        path.rmdir()
    except OSError:
        path.unlink()


def read_directory_link(path: Path) -> str | None:
    """Where a junction or symlink points, or None if `path` is neither."""

    if not is_directory_link(path):
        return None
    try:
        return os.readlink(path)
    except OSError:
        return None


def create_directory_link(path: Path, target: str, *, symlink: bool = False) -> None:
    r"""Recreate a directory link of the kind it came from.

    No stdlib call makes a junction, hence `mklink /J`. Junctions are the kind
    that matters here: a symlink needs administrator rights or developer mode,
    so the redirects users actually manage to create are junctions. The target
    is passed through exactly as `os.readlink` returned it, `\\?\` prefix and
    all -- `mklink` accepts that form and the new link reads back byte-identical
    to the original.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    if symlink or os.name != "nt":
        os.symlink(target, path, target_is_directory=True)
        return
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(path), target],
        capture_output=True,
        creationflags=no_window(),
    )
    if result.returncode != 0:
        raise OSError(
            f"Could not recreate the directory link {path} -> {target}: "
            f"{result.stdout.decode(errors='replace').strip()}"
        )


def _directory_links(root: Path) -> list[tuple[Path, str, bool]]:
    """Every directory link under `root`: where it sits, points, and its kind.

    Never descends through one, so a link inside a link is not reported -- it
    belongs to whoever owns the target, and recreating the outer link brings it
    along anyway.
    """

    found: list[tuple[Path, str, bool]] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            target = read_directory_link(entry)
            if target is not None:
                found.append((entry.relative_to(root), target, entry.is_symlink()))
            elif entry.is_dir():
                stack.append(entry)
    return found


def remove_tree(path: Path) -> None:
    """Delete a directory we own, without ever following a link out of it.

    The descent is ours rather than `shutil.rmtree`'s because that function
    only stopped recursing into junctions in CPython 3.12, and the guarantee
    has to belong to this code and not to whichever interpreter imports it:
    users do redirect parts of the data directory off the system drive.
    """

    if not os.path.lexists(path):
        return
    if is_directory_link(path):
        _unlink_directory_link(path)
        return
    for entry in path.iterdir():
        if is_directory_link(entry):
            _unlink_directory_link(entry)
        elif entry.is_dir():
            remove_tree(entry)
        else:
            entry.unlink(missing_ok=True)
    path.rmdir()


def _tree_summary(path: Path) -> tuple[int, int]:
    """Count the files this tree owns, without counting through a link.

    `os.walk` descends into a junction, since `is_symlink` is False for one.
    `copy_tree` deliberately recreates such links instead of duplicating what
    they point at, so counting through them would make the verification in
    `move_directory` fail on precisely the setup it exists to preserve.
    """

    if is_directory_link(path):
        return 0, 0
    files = 0
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            entry_path = Path(entry.path)
            if is_directory_link(entry_path):
                continue
            try:
                if entry.is_dir():
                    stack.append(entry_path)
                    continue
                total += entry.stat().st_size
            except OSError:
                continue
            files += 1
    return files, total


def copy_tree(source: Path, destination: Path) -> None:
    """Copy a directory, keeping directory links as links.

    `robocopy` is on every Windows and is far faster than walking the tree in
    Python for multi-GB directories. Its exit codes are a bitmask where anything
    below 8 means "copied, possibly with extras"; 8 and above are real failures.
    `/W:1` matters as much as the speed: the default is a 30-second wait per
    retry, so one file held open by a scanner would look like a hang.

    `/XJ` and the recreation afterwards are what make a relocate across volumes
    agree with one within a volume, where `os.replace` moves the link itself.
    Without them robocopy walks through a junction and writes a second physical
    copy at the destination. `shutil.copytree` needs the same treatment for the
    same reason `remove_tree` does -- `symlinks=True` only covers real
    symlinks, and a junction is not one.
    """

    link_target = read_directory_link(source)
    if link_target is not None:
        # The directory *is* the redirect. Copying it would mean copying a tree
        # that belongs to someone else, which is also the one robocopy case
        # `/XJ` does not cover.
        remove_tree(destination)
        create_directory_link(destination, link_target, symlink=source.is_symlink())
        return
    nested = _directory_links(source)
    if os.name == "nt" and shutil.which("robocopy"):
        result = subprocess.run(
            ["robocopy", str(source), str(destination), "/E", "/J", "/XJ", "/R:1", "/W:1", "/NFL", "/NDL", "/NP"],
            capture_output=True,
            creationflags=no_window(),
        )
        if result.returncode >= 8:
            raise OSError(f"robocopy failed with exit code {result.returncode} copying {source}")
    else:
        shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)
    for relative, target, symlink in nested:
        placed = destination / relative
        # The fallback copies straight through a junction, so a real directory
        # may be sitting where the link belongs.
        remove_tree(placed)
        create_directory_link(placed, target, symlink=symlink)


def move_directory(source: Path, destination: Path) -> tuple[bool, Path | None]:
    """Place one directory at `destination`.

    Returns whether anything was placed there, and a source still waiting to be
    removed (cross-volume only).

    Within one volume this is a rename, which keeps hardlinks, and leaves no
    source behind. Across volumes the copy is verified before anything is
    released, and the source is returned rather than deleted so the caller can
    record the new location first -- a crash between the two should cost a
    copy, not the data.

    Either way the destination is complete or absent, never half-populated.
    """

    if not source.is_dir() or not any(source.iterdir()):
        return False, None
    if destination.is_dir() and any(destination.iterdir()):
        # Adopting a tree that already holds this: merging two of them is the
        # user's decision, not ours.
        return False, None
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        remove_tree(destination)
    try:
        # Bare on purpose: this failure is the volume probe.
        os.replace(source, destination)
        return True, None
    except OSError:
        pass  # Different volume: fall through to copy-verify-then-release.
    staging = destination.with_name(f"{destination.name}.incoming")
    remove_tree(staging)
    try:
        copy_tree(source, staging)
        if _tree_summary(source) != _tree_summary(staging):
            raise OSError(f"Copy of {source} did not match the source; nothing was moved")
        replace_path(staging, destination)
    except BaseException:
        remove_tree(staging)
        raise
    return True, source


def move_store(
    source_root: Path,
    destination_root: Path,
    names: Sequence[str],
) -> tuple[list[str], list[Path]]:
    """Move the named directories between two roots, one `move_directory` each.

    Moves the contents rather than the root itself. Returns what was moved and
    which sources are still waiting to be removed.
    """

    moved: list[str] = []
    leftovers: list[Path] = []
    for name in names:
        placed, leftover = move_directory(source_root / name, destination_root / name)
        if placed:
            moved.append(name)
        if leftover is not None:
            leftovers.append(leftover)
    return moved, leftovers


def move_tree(source: Path, destination: Path) -> None:
    """Move a directory so that no crash can leave a half-populated destination.

    Copies to a sibling staging directory of the destination, compares the two
    trees, and only then renames into place -- a rename within one volume is
    atomic, so the destination either does not exist or is complete. The source
    is removed last, which is why an interrupted run costs disk rather than
    data.
    """

    if destination.exists():
        raise FileExistsError(f"Move destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(f"{destination.name}.incoming")
    remove_tree(staging)
    try:
        copy_tree(source, staging)
        if _tree_summary(source) != _tree_summary(staging):
            raise OSError(f"Copy of {source} did not match the source; nothing was moved")
        replace_path(staging, destination)
    except BaseException:
        remove_tree(staging)
        raise
    remove_tree(source)


def write_atomic(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
    newline: str = "\n",
) -> None:
    """Write a small file so a crash cannot leave it half-written.

    Temp file in the same directory, then `os.replace` -- the rename is atomic
    within a volume, so readers see either the old content or the new one and
    never a truncated middle.

    For records that must survive a crash *and* concurrent writers, take the
    lock first (`holding_lock`); this only guarantees atomicity.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        temporary.write_text(text, encoding=encoding, newline=newline)
        replace_path(temporary, path, budget=RECORD_REPLACE)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# -- locks (finesub_bootstrap/locks.py) ------------------------------------------------------------------------

LogCallback = Callable[[str], None]
PauseCheck = Callable[[], bool]

#: Waiting is polled rather than blocking so a pause request still gets through.
POLL_SECONDS = 0.5


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


def lease_path(lock_path: Path) -> Path:
    """The metadata file beside one lock: who is holding it, and since when.

    Beside rather than inside: a reader that has to take the lock to learn who
    holds it learns nothing -- it cannot take it.
    """

    return lock_path.with_suffix(".json")


def _write_lease(lock_path: Path, lease: Mapping[str, Any]) -> None:
    """Best effort: a directory nobody can write to keeps the lock working."""

    try:
        lease_path(lock_path).write_text(json.dumps(dict(lease), ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def read_lease(lock_path: Path) -> dict[str, Any] | None:
    """Whatever the current holder wrote, or None if nobody left anything.

    Extra information only, never the answer to "is it busy" -- that question
    is the lock's, and only the lock's.
    """

    try:
        record = json.loads(lease_path(lock_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


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
    """Hold the advisory lock at `lock_path`, waiting for it.

    A one-byte lock on a sidecar file, shared by every process that names the
    same path. The sidecar is never deleted: removing it would let a process
    that still holds the byte lock coexist with one that just created a fresh
    file.

    `timeout` of None waits indefinitely; a number raises `LockUnavailable`
    once it elapses. `lease` is written beside the lock while it is held and
    removed on the way out, so whoever finds the lock taken can say *who* has
    it; it is commentary, not control.
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
                    on_pause() if on_pause is not None else LockUnavailable(f"Paused while waiting for {lock_path.name}")
                )
            if deadline is not None and time.monotonic() >= deadline:
                raise LockUnavailable(f"Timed out waiting for {lock_path.name}")
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
                # Best effort like the write: the lock below has to be released
                # no matter what.
                try:
                    lease_path(lock_path).unlink(missing_ok=True)
                except OSError:
                    pass
            _release(handle)
    finally:
        handle.close()


def try_lock(lock_path: Path) -> bool:
    """Whether the lock is free right now, without holding it.

    For preconditions ("nothing is running") rather than mutual exclusion: the
    answer is stale the moment it is returned.
    """

    if not lock_path.is_file():
        return True
    try:
        handle = open(lock_path, "a+b")
    except OSError:
        return False
    try:
        if not _acquire(handle):
            return False
        _release(handle)
        return True
    finally:
        handle.close()


__all__ = [
    "PUBLISH_REPLACE",
    "RECORD_REPLACE",
    "LockUnavailable",
    "ReplaceBudget",
    "copy_tree",
    "create_directory_link",
    "holding_lock",
    "is_directory_link",
    "lease_path",
    "move_directory",
    "move_store",
    "move_tree",
    "no_window",
    "read_directory_link",
    "read_lease",
    "remove_tree",
    "replace_path",
    "terminate_process_tree",
    "try_lock",
    "which",
    "write_atomic",
]
