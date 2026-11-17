"""`finesub_bootstrap.fsops` for the finesub-correct package: only the names its FineSub code uses (WP-P1).

Copied from FineSub v0.7.0 (`7123055`), `src/finesub_bootstrap/fsops.py`, as
vendored in Nonoka X (the transcription package carries the original). Kept: the
top-level definitions the names below need, unchanged and in the original's order;
everything else is left out. `__all__` is exactly the names this package's FineSub
code imports (docs/engine-packages.md section 4); `tests/test_engine_packages.py`
holds this package to them.

The original's description:

    Filesystem operations that survive Windows and the users we actually have.

    Two things go wrong repeatedly in this project, so they live in one place:
    directory links (a `shutil.rmtree` walks straight through a junction into
    whatever the user redirected a directory to), and interrupted moves (a
    cross-volume `shutil.move` is copy-then-delete, so a crash leaves half a tree
    at the destination and turns one bad moment into a state a human has to
    untangle).
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import time

__all__ = ["RECORD_REPLACE", "is_directory_link", "remove_tree", "replace_path", "write_atomic"]


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


#: Records -- an index, a ledger, a `.env`. Deliberately much shorter: a tree
#: is published once and losing it means downloading again, while a record is
#: rewritten on every status update, so a name that stayed locked would make
#: every later write pay the full budget. Losing one entry until the next write
#: is cheaper than stalling each write for seconds.
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
    two names. A resource activation dies with `[WinError 5]` and discards a
    multi-minute download; the desktop then shows that line, which tells a user
    nothing they can act on.

    Not every rename belongs here. Two in this module stay bare on purpose,
    because their failure carries information rather than costing work: the
    same-volume probe in `move_directory`, whose `OSError` *means* "different
    volume" and is the signal to fall through to the copy, and the quarantine
    rename after a digest mismatch. A bounded wait also cannot outlast a reader
    that reopens the name in a loop -- that is a starved writer, a different
    problem, and no caller here polls that way.
    """

    for attempt in range(1, budget.attempts + 1):
        try:
            os.replace(source, destination)
            return
        except OSError:
            if attempt == budget.attempts:
                raise
            time.sleep(
                min(budget.backoff_seconds * attempt, budget.cap_seconds)
            )


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


def remove_tree(path: Path) -> None:
    """Delete a directory we own, without ever following a link out of it.

    The descent is ours rather than `shutil.rmtree`'s because that function
    only stopped recursing into junctions in CPython 3.12. The desktop app
    pins 3.12, but the CLI wheel declares `requires-python >= 3.10` and runs
    `finesub_bootstrap.shell` -- including `uninstall` -- on the *launcher's*
    interpreter, so on a 3.10/3.11 machine a junction nested anywhere under the
    target would be followed and someone else's data deleted. Users do make
    such links (redirecting `models`/`cache`/`tasks` off the system drive is a
    documented setup), so the guarantee has to belong to this code and not to
    whichever interpreter happens to import it.
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
    never a truncated middle. This idiom was hand-rolled in seven places and
    skipped in six others; the ones that skipped it are the records whose loss
    is expensive. `pyvenv.cfg` is the sharpest example: a torn write there
    removes the `home` line, the managed interpreter can no longer find its
    stdlib, and the fix is a multi-GB rebuild -- caused by the very routine
    that exists to avoid one.

    For records that must survive a crash *and* concurrent writers, take the
    lock first (see `locks.holding_lock`); this only guarantees atomicity.
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
