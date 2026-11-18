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

#: What a gate run wants in every package process before the package's code is
#: imported, now that nothing reaches one through its environment (WP-H): `.py`
#: files run first (os.pathsep-separated), and the variables they read (JSON).
#: Set in the process that calls `use_in_worker`; carried in the spec.
PRELOAD_ENVIRONMENT = "NONOKA_QUALITY_PACKAGE_PRELOAD"
PRELOAD_VARIABLES = "NONOKA_QUALITY_PACKAGE_VARIABLES"


class EnvironmentAudit:
    """What one package process reads and writes of its environment (WP-H's read audit).

    Installed as `os.environ` before anything of the package is imported, so
    every read through it -- `os.environ[...]`, `.get`, `in`, `os.getenv` --
    is recorded with whether the name was there and who put it there: the
    engine host (`handed`, the process's starting environment), the bridge
    while it applied the launch context (`bridge`), or the engine itself
    later (`engine`). A library reading the environment in native code is
    not seen; that is what "everything else does not exist" is for.
    Written to `<directory>/<entry>-<pid>.json` when the process exits.
    """

    def __init__(self, directory: Path, entry: str) -> None:
        self.path = directory / f"{entry}-{os.getpid()}.json"
        self.entry = entry
        self.handed = {self._key(name): value for name, value in os.environ.items()}
        self.phase = "bridge"
        self.reads: dict[str, dict[str, Any]] = {}
        self.writes: dict[str, str] = {}
        self.enumerated = 0

    def install(self) -> None:
        import atexit

        audit = self
        original = os.environ
        base = type(original)

        class Recording(base):  # type: ignore[misc, valid-type]
            def __getitem__(self, key: str) -> str:
                try:
                    value = base.__getitem__(self, key)
                except KeyError:
                    audit._read(key, None)
                    raise
                audit._read(key, value)
                return value

            def __setitem__(self, key: str, value: str) -> None:
                audit.writes.setdefault(audit._key(key), audit.phase)
                base.__setitem__(self, key, value)

            def __delitem__(self, key: str) -> None:
                audit.writes.setdefault(audit._key(key), audit.phase)
                base.__delitem__(self, key)

            def __iter__(self):  # type: ignore[no-untyped-def]
                audit.enumerated += 1
                return base.__iter__(self)

        recording = Recording.__new__(Recording)
        recording.__dict__.update(original.__dict__)
        os.environ = recording  # type: ignore[assignment]
        atexit.register(self.write)

    @staticmethod
    def _key(name: str) -> str:
        return str(name).upper() if os.name == "nt" else str(name)

    def _read(self, name: str, value: str | None) -> None:
        key = self._key(name)
        if key in self.reads:
            return
        if value is None:
            source = "absent"
        else:
            source = self.writes.get(key) or ("handed" if key in self.handed else "unknown")
        self.reads[key] = {"present": value is not None, "source": source}

    def write(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({
                "entry": self.entry, "pid": os.getpid(), "handed": sorted(self.handed),
                "reads": self.reads, "writes": self.writes, "enumerated": self.enumerated,
            }, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass


def no_stand_ins(spec: Mapping[str, Any], entry: str, context: Mapping[str, Any]) -> None:
    """A hook that puts nothing in place: the package runs as it does for the desktop (an audited real run)."""


def preload(spec: Mapping[str, Any]) -> None:
    """The gate's own variables and start-up files, in this package process, before its code is imported.

    What a `sitecustomize` on `PYTHONPATH` did for a package process before WP-H
    (the knowledge cases' fixed date, an audit hook): the process is isolated
    now, so the gate hands it over in the spec instead. The gate's variables
    are its own (`NONOKA_QUALITY_*`, an audit directory), never one an engine reads.
    """

    import runpy

    os.environ.update({str(name): str(value) for name, value in (spec.get("variables") or {}).items()})
    for path in spec.get("preload") or ():
        runpy.run_path(str(path), run_name="__nonoka_quality_preload__")


def main(argv: list[str]) -> int:
    entry, spec_path = argv[0], Path(argv[1])
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    preload(spec)
    audit = None
    if spec.get("audit"):
        audit = EnvironmentAudit(Path(spec["audit"]), entry)
        audit.install()
    module_name, _, function = str(spec["hook"]).partition(":")
    hook = getattr(importlib.import_module(module_name), function)
    package_main = importlib.import_module(f"{entry}.__main__").main

    def after_context(context: Mapping[str, Any]) -> None:
        if audit is not None:
            audit.phase = "engine"
        hook(spec, entry, context)

    return package_main([], after_context=after_context)


def use_in_worker(spec: Mapping[str, Any], spec_path: Path) -> None:
    """Make this process's engine host start every package through this module.

    `spec` is written to `spec_path` for the package processes to read. The
    repository root joins their import path, after the package's own roots,
    for this module only: it holds `tests`, not the core (`src` is not a root).
    Everything else about the process is the engine host's own: built from
    nothing, isolated, sandboxed for the transcription (WP-H).
    """

    from nonoka_x import engine_host

    spec = dict(spec)
    files = [part for part in os.environ.get(PRELOAD_ENVIRONMENT, "").split(os.pathsep) if part]
    if files:
        spec.setdefault("preload", files)
    if os.environ.get(PRELOAD_VARIABLES):
        spec.setdefault("variables", json.loads(os.environ[PRELOAD_VARIABLES]))
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    original_command = getattr(engine_host.package_command, "_quality_original", engine_host.package_command)
    original_environment = getattr(
        engine_host.package_environment, "_quality_original", engine_host.package_environment
    )

    def package_command(package: Any, python: Any, *arguments: str, **_ignored: Any) -> list[str]:
        # Started as the engine host starts it (isolated, its import path set
        # before anything is imported), with this module as the entry and the
        # repository root one more root (WP-H).
        return original_command(
            package, python, package.entry, str(spec_path), *arguments,
            module="tests.quality_harness.package_child", roots=[*package.paths, ROOT],
        )

    def package_environment(package: Any, *args: Any, **kwargs: Any) -> dict[str, str]:
        environment = original_environment(package, *args, **kwargs)
        environment["PYTHONPATH"] = os.pathsep.join([environment["PYTHONPATH"], str(ROOT)])
        return environment

    package_command._quality_original = original_command  # type: ignore[attr-defined]
    package_environment._quality_original = original_environment  # type: ignore[attr-defined]

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
