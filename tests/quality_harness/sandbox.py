"""The sandbox write check of `--real` (plan 4.5 item 32, WP-H's fourth proof).

Around the real sample runs, a snapshot of the places outside the
transcription process's sandbox it must not write to, taken before and after
(the way the sandbox experiment did, its report 1.0):

- **strict** (any file added, removed or changed fails the check): the
  default locations FineSub and `finesub_bootstrap` fall back to in the real
  profile (`%LOCALAPPDATA%\\FineSub`, `~\\.cache`, `~\\.finesub`), where the
  CUDA driver keeps its cache (`%APPDATA%\\NVIDIA`), the user site-packages
  (`%APPDATA%\\Python`), the installed app's data (`%APPDATA%\\Nonoka X`), the
  current version of each of the three layers, the transcription package's
  source directory, the core's source, and the two interpreters' directories
  (the launcher's and its base);
- **observed** (entries added or removed are listed and leave the check
  undecided, for a person to look at; owner 2026-11-18, WP-H question H4):
  the real profile, `%APPDATA%`, `%LOCALAPPDATA%` and `ProgramData` two levels
  deep, the real `%TEMP%` one level. Other programs write there all the time;
  the run's own data directory, big-file root and scratch are left out.

Files are compared by size and modification time, directories by their
entries. The registry, `C:\\Windows\\Temp` and libraries finding folders
through the Known Folder API are not covered (plan 4.5 item 31).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import ENGINES, ROOT

OBSERVED_DEPTH = 2


def _environment_path(name: str, fallback: Path) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else fallback


@dataclass
class Locations:
    strict: dict[str, Path] = field(default_factory=dict)
    observed: dict[str, tuple[Path, int]] = field(default_factory=dict)
    excluded: list[Path] = field(default_factory=list)


def _interpreter_directories(python: Path | None) -> list[Path]:
    """An interpreter's own directory, and its base's when it is a virtual environment."""

    if python is None:
        return []
    found = [python.resolve().parent.parent if python.parent.name.lower() == "scripts" else python.resolve().parent]
    config = found[0] / "pyvenv.cfg"
    if config.is_file():
        for line in config.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip().lower() == "home" and value.strip():
                found.append(Path(value.strip()).resolve())
    return found


def locations(provisioner: Any, launcher: Path | None, *, excluded: Iterable[Path] = ()) -> Locations:
    """The protected locations of this machine for a run on `provisioner`'s layers."""

    home = Path.home()
    appdata = _environment_path("APPDATA", home / "AppData" / "Roaming")
    local = _environment_path("LOCALAPPDATA", home / "AppData" / "Local")
    temp = _environment_path("TEMP", local / "Temp")
    program_data = _environment_path("ProgramData", Path(r"C:\ProgramData"))
    found = Locations()
    found.strict.update({
        "%LOCALAPPDATA%\\FineSub": local / "FineSub",
        "~\\.cache": home / ".cache",
        "~\\.finesub": home / ".finesub",
        "%APPDATA%\\NVIDIA": appdata / "NVIDIA",
        "%APPDATA%\\Python": appdata / "Python",
        "%APPDATA%\\Nonoka X": appdata / "Nonoka X",
        "engines\\finesub-transcribe": ENGINES / "finesub-transcribe",
        "src (the core)": ROOT / "src",
    })
    layers = getattr(provisioner, "layers", None)
    for layer in ("python", "models", "tools"):
        root = layers.root(layer) if layers is not None else None
        if root is not None:
            found.strict[f"layer {layer}"] = Path(root)
    for index, directory in enumerate(_interpreter_directories(launcher or Path(sys.executable))):
        found.strict[f"launcher interpreter {index}"] = directory
    found.observed.update({
        "~": (home, OBSERVED_DEPTH),
        "%APPDATA%": (appdata, OBSERVED_DEPTH),
        "%LOCALAPPDATA%": (local, OBSERVED_DEPTH),
        "%ProgramData%": (program_data, OBSERVED_DEPTH),
        "%TEMP%": (temp, 1),
    })
    found.excluded = [Path(path).resolve() for path in excluded]
    return found


def _files(root: Path) -> dict[str, list[int]]:
    """Every file under `root`, by relative path: [size, mtime_ns]. Links are not followed."""

    found: dict[str, list[int]] = {}
    if not root.is_dir():
        return found
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False) and not entry.is_junction():
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    stat = entry.stat(follow_symlinks=False)
                    found[Path(entry.path).relative_to(root).as_posix()] = [stat.st_size, stat.st_mtime_ns]
            except OSError:
                continue
    return found


def _entries(root: Path, depth: int, excluded: list[Path]) -> set[str]:
    """The names under `root`, `depth` levels deep, without the excluded paths."""

    found: set[str] = set()
    level = [root]
    for _ in range(depth):
        following = []
        for directory in level:
            try:
                entries = list(os.scandir(directory))
            except OSError:
                continue
            for entry in entries:
                path = Path(entry.path)
                resolved = path.resolve()
                if any(resolved == item or item in resolved.parents for item in excluded):
                    continue
                found.add(path.relative_to(root).as_posix())
                try:
                    if entry.is_dir(follow_symlinks=False):
                        following.append(path)
                except OSError:
                    continue
        level = following
    return found


def snapshot(where: Locations) -> dict[str, Any]:
    return {
        "strict": {name: _files(path) for name, path in where.strict.items()},
        "observed": {name: sorted(_entries(path, depth, where.excluded)) for name, (path, depth) in where.observed.items()},
    }


def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Per strict location: added, removed, modified files; per observed one: added, removed entries."""

    strict: dict[str, Any] = {}
    for name, files in before["strict"].items():
        later = after["strict"].get(name, {})
        change = {
            "added": sorted(set(later) - set(files)),
            "removed": sorted(set(files) - set(later)),
            "modified": sorted(path for path in set(files) & set(later) if files[path] != later[path]),
        }
        strict[name] = {key: value for key, value in change.items() if value}
    observed: dict[str, Any] = {}
    for name, entries in before["observed"].items():
        later = set(after["observed"].get(name, []))
        change = {"added": sorted(later - set(entries)), "removed": sorted(set(entries) - later)}
        observed[name] = {key: value for key, value in change.items() if value}
    return {
        "strict": {name: change for name, change in strict.items() if change},
        "observed": {name: change for name, change in observed.items() if change},
    }


def sandbox_state(data_dir: Path) -> dict[str, Any]:
    """What the sandbox itself holds after the runs: its home's top entries, and leftover temporary directories."""

    root = data_dir / "engines" / "finesub-transcribe" / "sandbox"
    temps = sorted(path.name for path in (root / "temp").iterdir()) if (root / "temp").is_dir() else []
    home_files = _files(root / "home")
    return {
        "exists": root.is_dir(),
        "leftover_temps": temps,
        "home_files": len(home_files),
        "home_top": sorted({"/".join(path.split("/")[:3]) for path in home_files})[:20],
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
