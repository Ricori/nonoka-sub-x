"""The quality lock: every engine file but the three unlocked places, by hash.

`third_party/finesub/QUALITY_LOCK.json` records the sha256 of each file under
`third_party/finesub/src/` except the orchestration the refactor is allowed to
change (plan section 0, rule 2). Bytes are hashed as stored: `.gitattributes`
marks `third_party/finesub/**` as `-text`, so a checkout on any platform has
exactly the committed bytes.

Changing the lock is a separate, user-approved engine change; `write` exists
for that commit and for nothing else.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from . import ROOT, VENDOR, VENDOR_SOURCE

LOCK_FILE = VENDOR / "QUALITY_LOCK.json"
#: Unlocked single files, relative to `third_party/finesub/src`.
UNLOCKED_FILES = ("finesub/pipeline.py", "finesub/stages.py")
#: Unlocked directories (everything below them), same base. `finesub_bootstrap/`
#: was unlocked by the owner on 2026-11-11: Nonoka supplies what the engine
#: needs from it. Its pinned contents are guarded by content instead, in
#: `src/nonoka_x/resources/RUNTIME_PINS.json` (`tests/test_runtime_pins.py`).
UNLOCKED_DIRS = ("finesub/speech/joint/", "finesub_bootstrap/")


def is_unlocked(relative: str) -> bool:
    return relative in UNLOCKED_FILES or any(relative.startswith(prefix) for prefix in UNLOCKED_DIRS)


def iter_files(root: Path = VENDOR_SOURCE) -> Iterable[str]:
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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute(root: Path = VENDOR_SOURCE) -> dict[str, Any]:
    files = [
        {"path": relative, "sha256": sha256_file(root / relative), "bytes": (root / relative).stat().st_size}
        for relative in iter_files(root)
        if not is_unlocked(relative)
    ]
    return {
        "schema": 1,
        "root": "third_party/finesub/src",
        "rule": (
            "Every file below root except the unlocked paths must match this list byte for byte, "
            "and no file may be added or removed. Updating this file is a separate engine change "
            "the owner approves (docs/transcription-engines-plan.md section 0)."
        ),
        "unlocked": sorted((*UNLOCKED_FILES, *UNLOCKED_DIRS)),
        "files": files,
    }


def render(lock: dict[str, Any]) -> bytes:
    return (json.dumps(lock, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write(path: Path = LOCK_FILE) -> Path:
    path.write_bytes(render(compute()))
    return path


def problems(root: Path = VENDOR_SOURCE, path: Path = LOCK_FILE) -> list[str]:
    """Every way the tree differs from the lock, one line each; empty when it matches."""

    if not path.is_file():
        return [f"{path.relative_to(ROOT).as_posix()} is missing"]
    recorded = json.loads(path.read_text(encoding="utf-8"))
    expected = {entry["path"]: entry for entry in recorded.get("files", [])}
    found: list[str] = []
    for relative in sorted(expected):
        if is_unlocked(relative):
            found.append(f"lock lists an unlocked path: {relative}")
    actual = {relative for relative in iter_files(root) if not is_unlocked(relative)}
    for relative in sorted(actual - set(expected)):
        found.append(f"added (not in the lock): {relative}")
    for relative in sorted(set(expected) - actual):
        if not is_unlocked(relative):
            found.append(f"removed (locked): {relative}")
    for relative in sorted(actual & set(expected)):
        entry = expected[relative]
        file = root / relative
        digest = sha256_file(file)
        if digest != entry["sha256"] or file.stat().st_size != entry["bytes"]:
            found.append(f"modified (locked): {relative}  sha256 {entry['sha256'][:12]}… -> {digest[:12]}…")
    return found
