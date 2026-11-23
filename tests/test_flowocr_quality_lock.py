"""The on-screen text package's quality lock (`engines/flowocr/QUALITY_LOCK.json`).

Every file under `engines/flowocr/src/` must be byte-identical to the lock,
and no file may appear or disappear: that tree is upstream FlowOCR at the
commit `UPSTREAM.json` records. The integration lives in `adapter/`.

Self-contained on purpose, like `tests/test_finesub_quality_lock.py`.
"""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "engines" / "flowocr"


def tree() -> dict[str, Path]:
    source = PACKAGE / "src"
    return {
        path.relative_to(source).as_posix(): path
        for path in source.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}
    }


class FlowOcrQualityLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lock = json.loads((PACKAGE / "QUALITY_LOCK.json").read_text(encoding="utf-8"))

    def test_the_lock_names_the_upstream_commit(self) -> None:
        upstream = json.loads((PACKAGE / "UPSTREAM.json").read_text(encoding="utf-8"))
        manifest = json.loads((PACKAGE / "PACKAGE.json").read_text(encoding="utf-8"))
        self.assertEqual(self.lock["upstream"], upstream["commit"])
        self.assertEqual(manifest["upstream"], upstream)
        self.assertEqual(self.lock["root"], "engines/flowocr/src")
        self.assertEqual(self.lock["unlocked"], [])

    def test_every_file_matches_and_none_is_added_or_removed(self) -> None:
        locked = {item["path"]: item for item in self.lock["files"]}
        present = tree()
        self.assertEqual(sorted(present.keys() - locked.keys()), [], "files not in the lock")
        self.assertEqual(sorted(locked.keys() - present.keys()), [], "locked files missing")
        for relative, item in locked.items():
            data = present[relative].read_bytes()
            self.assertEqual(len(data), item["bytes"], relative)
            self.assertEqual(hashlib.sha256(data).hexdigest(), item["sha256"], relative)

    def test_nothing_installable_was_carried(self) -> None:
        for name in ("pyproject.toml", "setup.py", "setup.cfg", "uv.lock"):
            self.assertEqual(list(PACKAGE.rglob(name)), [], name)


if __name__ == "__main__":
    unittest.main()
