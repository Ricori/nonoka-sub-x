from __future__ import annotations

import os
import signal
import subprocess
from typing import Any


def no_window() -> int:
    """Creation flags for a child that must not open a console window.

    Windows gives a console child the console of its parent. A parent that has
    none -- anything started windowless, a GUI host, `pythonw`, a scheduled
    task -- makes each console child allocate a new one, and that one is
    *visible*: a black box that flashes for as long as the child runs. With
    this flag the child gets a hidden console instead, and every process *it*
    starts inherits that, so the whole tree below stays quiet too -- including
    what a vendor CLI spawns, which nothing here can pass flags to. Except a
    child the vendor starts detached: it has no console to inherit, and its own
    console children open visible ones. agy's update check is one (switched
    off by `AGY_QUIET_ENV` in `local_agent.py`).

    Measured 2026-09-16 with a consoleless FineSub process: every `agy`
    invocation and `ffmpeg -version` showed a window; with a console (even a
    hidden one) none did.

    Not for a child that must share the user's terminal -- the pipeline the
    `finesub` shell runs, an editor: a separate hidden console means it neither
    prints there nor receives the terminal's Ctrl+C.
    `test_subprocess_console_window.py` holds the list of those.
    """

    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def terminate_process_tree(process: Any) -> None:
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
