"""The quality locks: every locked engine file, by hash, in each engine package.

Each engine package's `QUALITY_LOCK.json` (`engines/<package>/QUALITY_LOCK.json`)
records the sha256 of every file under its `src/` except the orchestration the
refactor is allowed to change (plan section 0, rule 2): `finesub/stages.py`
and `finesub_bootstrap/`. Bytes are hashed as stored: `.gitattributes` marks
`engines/*/src/**` as `-text`, so a checkout on any platform has exactly the
committed bytes.

The packages were split out of one vendored tree in WP-P1; that tree's lock is
kept as `tests/engine-packages/vendor-quality-lock.json`, and every entry of a
package's lock must be that lock's entry for the same path: splitting moved
files, it changed none (`tests/test_engine_packages.py` also accounts for every
file of the old lock).

Changing a lock is a separate, user-approved engine change; `write` exists
for that commit and for nothing else.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from . import PACKAGE_IDS, PACKAGES_ROOT, ROOT, SOURCES

LOCK_NAME = "QUALITY_LOCK.json"
#: The lock of the vendored tree the packages were split from (WP-P1).
VENDOR_LOCK = ROOT / "tests" / "engine-packages" / "vendor-quality-lock.json"
#: Unlocked single files, relative to a package's `src/`.
UNLOCKED_FILES = ("finesub/stages.py",)
#: Unlocked directories (everything below them), same base. `finesub_bootstrap/`
#: was unlocked by the owner on 2026-11-11: Nonoka supplies what the engine
#: needs from it. The transcription package's original is guarded by content
#: instead, in `src/nonoka_x/resources/RUNTIME_PINS.json` (`tests/test_runtime_pins.py`);
#: the other two packages' compatibility packages by `tests/test_engine_packages.py`.
UNLOCKED_DIRS = ("finesub_bootstrap/",)


def lock_file(package: str) -> Path:
    return PACKAGES_ROOT / package / LOCK_NAME


def is_unlocked(relative: str) -> bool:
    return relative in UNLOCKED_FILES or any(relative.startswith(prefix) for prefix in UNLOCKED_DIRS)


def iter_files(root: Path) -> Iterable[str]:
    """Every file below `root` as a POSIX relative path, sorted by that spelling.

    Sorted by the string rather than by `Path`, which orders by the platform's
    own spelling (backslashes, case folding on Windows).
    """

    selected = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}
    ]
    return sorted(selected)


def locked_files() -> dict[str, Path]:
    """Every locked file of every package, relative path -> the first package's copy (transcription first)."""

    found: dict[str, Path] = {}
    for package in PACKAGE_IDS:
        for relative in iter_files(SOURCES[package]):
            if not is_unlocked(relative):
                found.setdefault(relative, SOURCES[package] / relative)
    return dict(sorted(found.items()))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute(package: str) -> dict[str, Any]:
    root = SOURCES[package]
    files = [
        {"path": relative, "sha256": sha256_file(root / relative), "bytes": (root / relative).stat().st_size}
        for relative in iter_files(root)
        if not is_unlocked(relative)
    ]
    unlocked = [path for path in (*UNLOCKED_FILES, *UNLOCKED_DIRS)
                if (root / path).exists()]
    return {
        "schema": 1,
        "root": f"engines/{package}/src",
        "split_from": VENDOR_LOCK.relative_to(ROOT).as_posix(),
        "rule": (
            "Every file below root except the unlocked paths must match this list byte for byte, "
            "and no file may be added or removed. Each entry is the pre-split vendor lock's entry for the "
            "same path. Updating this file is a separate engine change the owner approves "
            "(docs/transcription-engines-plan.md section 0)."
        ),
        "unlocked": sorted(unlocked),
        "files": files,
    }


def render(lock: dict[str, Any]) -> bytes:
    return (json.dumps(lock, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write(package: str) -> Path:
    path = lock_file(package)
    path.write_bytes(render(compute(package)))
    return path


def recorded(package: str) -> dict[str, Any]:
    path = lock_file(package)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def problems(package: str | None = None) -> list[str]:
    """Every way a package's tree differs from its lock, one line each; empty when they match.

    Without `package`, every package's, each line prefixed with the package.
    """

    if package is None:
        return [f"{name}: {line}" for name in PACKAGE_IDS for line in problems(name)]
    root = SOURCES[package]
    path = lock_file(package)
    if not path.is_file():
        return [f"{path.relative_to(ROOT).as_posix()} is missing"]
    lock = json.loads(path.read_text(encoding="utf-8"))
    expected = {entry["path"]: entry for entry in lock.get("files", [])}
    vendor = {entry["path"]: entry for entry in json.loads(VENDOR_LOCK.read_text(encoding="utf-8"))["files"]}
    found: list[str] = []
    for relative in sorted(expected):
        if is_unlocked(relative):
            found.append(f"lock lists an unlocked path: {relative}")
        if vendor.get(relative) != expected[relative]:
            found.append(f"lock entry differs from the pre-split lock: {relative}")
    actual = {relative for relative in iter_files(root) if not is_unlocked(relative)}
    for relative in sorted(actual - set(expected)):
        found.append(f"added (not in the lock): {relative}")
    for relative in sorted(set(expected) - actual):
        found.append(f"removed (locked): {relative}")
    for relative in sorted(actual & set(expected)):
        entry = expected[relative]
        file = root / relative
        digest = sha256_file(file)
        if digest != entry["sha256"] or file.stat().st_size != entry["bytes"]:
            found.append(f"modified (locked): {relative}  sha256 {entry['sha256'][:12]}… -> {digest[:12]}…")
    return found
