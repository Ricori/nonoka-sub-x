"""An engine package's process with the quality gate's stand-ins in it.

Since WP-P1 the engines run in their packages' own processes
(`nonoka_x.engine_host`), so the stand-ins that record what an algorithm entry
point receives (`stubs`), the replay's model (`replay`) and the knowledge
cases' hooks have to be put in place there, after the package's bridge turned
its launch context into FineSub's variables and files -- where the worker used
to have them in its one process. Only the gate's entry changes: the package's
own `main` runs as it does for the desktop, with `after_context` called once
the context is applied.

`python -m tests.quality_harness.package_child <entry> <spec.json>`, where the
spec names the hook (`module:function`, called with the spec, the entry and
the launch context). `use_in_worker` makes the core's engine host start every
package this way, in the process that runs the worker.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

from . import ROOT


def main(argv: list[str]) -> int:
    entry, spec_path = argv[0], Path(argv[1])
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    module_name, _, function = str(spec["hook"]).partition(":")
    hook = getattr(importlib.import_module(module_name), function)
    package_main = importlib.import_module(f"{entry}.__main__").main
    return package_main([], after_context=lambda context: hook(spec, entry, context))


def use_in_worker(spec: Mapping[str, Any], spec_path: Path) -> None:
    """Make this process's engine host start every package through this module.

    `spec` is written to `spec_path` for the package processes to read. The
    repository root joins their import path, after the package's own roots,
    for this module only: it holds `tests`, not the core (`src` is not a root).
    """

    from nonoka_x import engine_host

    spec_path.write_text(json.dumps(dict(spec), ensure_ascii=False), encoding="utf-8")
    original_environment = engine_host.package_environment

    def package_command(package: Any, python: Any, *arguments: str) -> list[str]:
        return [str(python), "-m", "tests.quality_harness.package_child", package.entry, str(spec_path), *arguments]

    def package_environment(package: Any, base: Mapping[str, str] | None = None) -> dict[str, str]:
        environment = original_environment(package, base)
        environment["PYTHONPATH"] = os.pathsep.join([environment["PYTHONPATH"], str(ROOT)])
        return environment

    engine_host.package_command = package_command
    engine_host.package_environment = package_environment


class SharedRecord(list):
    """A list of records that also lands, one JSON line each, in a file every process of a run appends to.

    The stand-ins record in whichever package process an entry point runs
    in; the file keeps them in the order they happened across processes,
    which is the order one process recorded them in before.
    """

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path

    def append(self, value: Any) -> None:  # type: ignore[override]
        super().append(value)
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def read_shared(path: Path) -> list[Any]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
