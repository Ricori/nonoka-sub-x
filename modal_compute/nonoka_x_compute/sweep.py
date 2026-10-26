"""The scratch Volume's safety net.

A breakpoint tree `tasks/<job_id>/` normally goes away with its job: the CPU
postprocess leg removes it after a successful hand-back, and the Worker asks
`control` to purge it when the desktop deletes the job or the Worker's own
sweep expires it. What this catches is the tree nothing came back for -- a
purge that failed, a job the Worker forgot. A week is far past the Worker's
48-hour job TTL, so nothing a retry or a resume could still use is removed.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import modal

from .images import control_image
from .resources import DATA_ROOT, app, scratch_volume


SCRATCH_MAX_AGE_SEC = 7 * 24 * 60 * 60


def _newest_mtime(directory: Path) -> float:
    """The latest modification anywhere in the tree, the directory's own included.

    A directory's own mtime only moves when an entry is added or removed, so a
    run rewriting a file inside it would otherwise look idle.
    """

    newest = directory.stat().st_mtime
    for path in directory.rglob("*"):
        try:
            newest = max(newest, path.lstat().st_mtime)
        except OSError:
            continue
    return newest


def plan_scratch_sweep(root: Path, *, now: float, max_age_sec: float = SCRATCH_MAX_AGE_SEC) -> list[Path]:
    """The breakpoint trees under `root/tasks` untouched for longer than `max_age_sec`."""

    tasks = root / "tasks"
    if not tasks.is_dir():
        return []
    expired = []
    for directory in sorted(tasks.iterdir()):
        if not directory.is_dir() or directory.is_symlink():
            continue
        try:
            if now - _newest_mtime(directory) > max_age_sec:
                expired.append(directory)
        except OSError:
            continue
    return expired


@app.function(
    image=control_image,
    volumes={"/data": scratch_volume},
    schedule=modal.Period(days=1),
    timeout=60 * 15,
)
def sweep_scratch() -> dict[str, int]:
    scratch_volume.reload()
    expired = plan_scratch_sweep(DATA_ROOT, now=time.time())
    for directory in expired:
        shutil.rmtree(directory, ignore_errors=True)
    if expired:
        scratch_volume.commit()
    result = {"removed": len(expired)}
    print(json.dumps({"scope": "nonoka-x-compute", "type": "scratch-sweep", **result}), flush=True)
    return result
