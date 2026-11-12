"""The quality lock (`third_party/finesub/QUALITY_LOCK.json`).

Every file under `third_party/finesub/src/` except the unlocked places
(`finesub/stages.py`, `finesub/pipeline.py`, `finesub/speech/joint/`, and
`finesub_bootstrap/` since the owner unlocked it on 2026-11-11) must be
byte-identical to the lock, and no file may appear or disappear. The unlocked
places are not compared at all, so `speech/joint/` moving out, or
`finesub_bootstrap/` being replaced, does not fail this test. What
`finesub_bootstrap/` pins (runtime locks, tool and model manifests, the model
constants) is guarded by content in `tests/test_runtime_pins.py`.

The one approved change to locked files since the lock was made: WP-K moved
the knowledge access points of 18 locked files onto the knowledge package's
protocol (plan section 0.1 rule 5, owner 2026-11-12; `LOCAL_CHANGES.md` 0011),
and the lock was updated for exactly those files in its own commit.
`tests/test_knowledge_protocol.py` checks what changed in them (the import
source of the store functions, nothing else).

Deliberately self-contained: it re-implements the walk and the hash instead of
importing `tests.quality_harness.lock`, so a change to the writer cannot quietly
change what the check accepts.
"""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "third_party" / "finesub" / "src"
LOCK = ROOT / "third_party" / "finesub" / "QUALITY_LOCK.json"
UNLOCKED_FILES = {"finesub/stages.py", "finesub/pipeline.py"}
UNLOCKED_DIRS = ("finesub/speech/joint/", "finesub_bootstrap/")


def unlocked(relative: str) -> bool:
    return relative in UNLOCKED_FILES or relative.startswith(UNLOCKED_DIRS)


def tree() -> dict[str, Path]:
    return {
        path.relative_to(SOURCE).as_posix(): path
        for path in SOURCE.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}
    }


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class QualityLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lock = json.loads(LOCK.read_text(encoding="utf-8"))
        self.recorded = {entry["path"]: entry for entry in self.lock["files"]}

    def test_the_lock_names_exactly_the_unlocked_places(self) -> None:
        self.assertEqual(self.lock["schema"], 1)
        self.assertEqual(self.lock["root"], "third_party/finesub/src")
        self.assertEqual(set(self.lock["unlocked"]), UNLOCKED_FILES | set(UNLOCKED_DIRS))
        listed_unlocked = sorted(path for path in self.recorded if unlocked(path))
        self.assertEqual(listed_unlocked, [], "the lock must not record unlocked paths")

    def test_the_lock_covers_the_algorithms_and_their_runtime(self) -> None:
        """A guard against a lock regenerated over a narrower tree."""

        for required in (
            "finesub/settings.py",
            "finesub/config.py",
            "finesub/llm/correction_translation.py",
            "finesub/llm/client.py",
            "finesub/speech/postprocessing/stabilization.py",
            "finesub/speech/recognition/vad_asr_stage.py",
            "finesub/speech/preprocessing/separator/separation.py",
            "finesub/speech/diarization/stage.py",
            "finesub/subtitles/rendering.py",
            "finesub/paths.py",
            "finesub/api_keys.py",
            "finesub/speech/runtime/hf_weights.py",
        ):
            self.assertIn(required, self.recorded)
        templates = [path for path in self.recorded if path.startswith("finesub/llm/prompt_templates/")]
        self.assertGreater(len(templates), 20)

    def test_no_locked_file_was_added_or_removed(self) -> None:
        actual = {relative for relative in tree() if not unlocked(relative)}
        expected = set(self.recorded)
        self.assertEqual(
            (sorted(actual - expected), sorted(expected - actual)),
            ([], []),
            "(added, removed) locked files",
        )

    def test_every_locked_file_matches_its_hash(self) -> None:
        files = tree()
        changed = []
        for relative, entry in sorted(self.recorded.items()):
            path = files.get(relative)
            if path is None:
                continue  # reported by the add/remove test
            if path.stat().st_size != entry["bytes"] or sha256(path) != entry["sha256"]:
                changed.append(relative)
        self.assertEqual(changed, [], "locked engine files were modified")

    def test_unlocked_places_are_not_compared(self) -> None:
        """Present or gone, edited or not: nothing under them reaches the comparison."""

        for relative in tree():
            if unlocked(relative):
                self.assertNotIn(relative, self.recorded)


if __name__ == "__main__":
    unittest.main()
