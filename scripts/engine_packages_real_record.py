"""Record the FineSub modules a real worker run imports (WP-P0 question 1).

Runs `tests/quality_harness/real.py`'s worker launch (the same command and
environment as `quality_gate.py --real`) for both samples to raw-srt, with a
recorder `sitecustomize` prepended to the worker's PYTHONPATH, and writes
`docs/engine-packages/runtime-imports/real-<sample>.json`. It compares nothing
with any baseline and writes nothing else. `scripts/engine_packages.py` then
checks that every recorded module is in some package list.

Usage (take the GPU lock first, see WP-Q in docs/transcription-engines-plan.md)::

    D:\\Envir\\Python312\\python scripts/engine_packages_real_record.py

`NONOKA_P0_MAIN` names the main checkout (default D:\\Development\\nonoka-sub-x):
`test_data/` lives only there.

WP-P0's one-off. Since WP-P1 the worker imports no FineSub: the packages'
processes do, and they inherit the recorder on their `PYTHONPATH`, so each
writes the record when it exits and the last one wins. Rerunning this needs a
record per process first.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[1]
MAIN = Path(os.environ.get("NONOKA_P0_MAIN", r"D:\Development\nonoka-sub-x"))
sys.path[:0] = [str(WORKTREE), str(WORKTREE / "src")]

from tests.quality_harness import real  # noqa: E402
from nonoka_x.local_provider import validate_request  # noqa: E402

RECORDER = (WORKTREE / "scripts" / "engine_packages.py").read_text(encoding="utf-8").split("RECORDER = r'''", 1)[1].split("'''", 1)[0]
OUT = WORKTREE / "docs" / "engine-packages" / "runtime-imports"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    failed = 0
    for name, sample in real.SAMPLES.items():
        source = MAIN / "test_data" / "audio" / Path(sample["path"]).name
        with tempfile.TemporaryDirectory(prefix="p0-real-") as scratch:
            scratch_path = Path(scratch)
            hook = scratch_path / "hook"
            hook.mkdir()
            (hook / "sitecustomize.py").write_text(RECORDER, encoding="utf-8")
            record = scratch_path / "record.json"
            run_dir = scratch_path / "run"
            run_dir.mkdir()
            request = validate_request({
                "schema": 1, "provider": "local",
                "source": {"kind": "local_file", "path": str(source), "title": name},
                "target": real.TARGET, **sample["fields"],
            })
            command, environment, _task_dir, _removed = real._worker_launch(run_dir)
            environment["PYTHONPATH"] = os.pathsep.join([str(hook), environment.get("PYTHONPATH", "")])
            environment["NONOKA_P0_RECORD"] = str(record)
            started = time.time()
            process = subprocess.run(
                command, input=json.dumps(request, ensure_ascii=False) + "\n", capture_output=True,
                text=True, encoding="utf-8", errors="replace", env=environment, timeout=3600,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            data = json.loads(record.read_text(encoding="utf-8")) if record.exists() else {"modules": [], "heavy": []}
            result = {
                "name": f"real-{name}", "package": None, "python": "pbs-3.12.1",
                "command": ["<LocalProvider._default_worker_command>", f"sample={name}", f"target={real.TARGET}"],
                "fields": sample["fields"], "returncode": process.returncode,
                "seconds": round(time.time() - started, 1), **data,
            }
            (OUT / f"real-{name}.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n"
            )
            print(name, process.returncode, result["seconds"], len(data["modules"]), data["heavy"])
            if process.returncode:
                failed += 1
                print(process.stdout[-3000:])
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
