"""The quality locks (`engines/<package>/QUALITY_LOCK.json`).

Every file under an engine package's `src/` except the unlocked places
(`finesub/stages.py`, and `finesub_bootstrap/` since the owner unlocked it on
2026-11-11) must be byte-identical to that package's lock, and no file may
appear or disappear. The unlocked places are not compared at all. What the
transcription package's `finesub_bootstrap/` pins (runtime locks, tool and
model manifests, the model constants) is guarded by content in
`tests/test_runtime_pins.py`; the other two packages' compatibility packages
by `tests/test_engine_packages.py`.

The packages were split out of one vendored tree in WP-P1. That tree's lock
is kept as `tests/engine-packages/vendor-quality-lock.json`, and each package's
lock entry must be that lock's entry for the same path: the split moved files,
it changed none. `tests/test_engine_packages.py` accounts for every file of the
old lock (in a package, or listed as not carried with the reason).

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
PACKAGES = ("finesub-transcribe", "finesub-correct", "finesub-knowledge")
VENDOR_LOCK = ROOT / "tests" / "engine-packages" / "vendor-quality-lock.json"
UNLOCKED_FILES = {"finesub/stages.py"}
UNLOCKED_DIRS = ("finesub_bootstrap/",)


def unlocked(relative: str) -> bool:
    return relative in UNLOCKED_FILES or relative.startswith(UNLOCKED_DIRS)


def tree(package: str) -> dict[str, Path]:
    source = ROOT / "engines" / package / "src"
    return {
        path.relative_to(source).as_posix(): path
        for path in source.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}
    }


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def lock(package: str) -> dict:
    return json.loads((ROOT / "engines" / package / "QUALITY_LOCK.json").read_text(encoding="utf-8"))


class QualityLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.vendor = {entry["path"]: entry for entry in json.loads(VENDOR_LOCK.read_text(encoding="utf-8"))["files"]}

    def test_each_lock_names_exactly_the_unlocked_places(self) -> None:
        for package in PACKAGES:
            with self.subTest(package=package):
                recorded = lock(package)
                self.assertEqual(recorded["schema"], 1)
                self.assertEqual(recorded["root"], f"engines/{package}/src")
                expected = set(UNLOCKED_DIRS) | ({"finesub/stages.py"} if package != "finesub-knowledge" else set())
                self.assertEqual(set(recorded["unlocked"]), expected)
                self.assertEqual(sorted(entry["path"] for entry in recorded["files"] if unlocked(entry["path"])), [],
                                 "a lock must not record unlocked paths")

    def test_every_entry_is_the_pre_split_locks(self) -> None:
        for package in PACKAGES:
            with self.subTest(package=package):
                differing = [entry["path"] for entry in lock(package)["files"]
                             if self.vendor.get(entry["path"]) != entry]
                self.assertEqual(differing, [])

    def test_the_locks_cover_the_algorithms_and_their_runtime(self) -> None:
        """A guard against a lock regenerated over a narrower tree."""

        covered = {entry["path"] for package in PACKAGES for entry in lock(package)["files"]}
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
            self.assertIn(required, covered)
        templates = [path for path in covered if path.startswith("finesub/llm/prompt_templates/")]
        self.assertGreater(len(templates), 20)

    def test_no_locked_file_was_added_or_removed(self) -> None:
        for package in PACKAGES:
            with self.subTest(package=package):
                actual = {relative for relative in tree(package) if not unlocked(relative)}
                expected = {entry["path"] for entry in lock(package)["files"]}
                self.assertEqual(
                    (sorted(actual - expected), sorted(expected - actual)),
                    ([], []),
                    "(added, removed) locked files",
                )

    def test_every_locked_file_matches_its_hash(self) -> None:
        for package in PACKAGES:
            with self.subTest(package=package):
                files = tree(package)
                changed = []
                for entry in lock(package)["files"]:
                    path = files.get(entry["path"])
                    if path is None:
                        continue  # reported by the add/remove test
                    if path.stat().st_size != entry["bytes"] or sha256(path) != entry["sha256"]:
                        changed.append(entry["path"])
                self.assertEqual(changed, [], "locked engine files were modified")

    def test_unlocked_places_are_not_compared(self) -> None:
        """Present or gone, edited or not: nothing under them reaches the comparison."""

        for package in PACKAGES:
            recorded = {entry["path"] for entry in lock(package)["files"]}
            for relative in tree(package):
                if unlocked(relative):
                    self.assertNotIn(relative, recorded)


if __name__ == "__main__":
    unittest.main()
