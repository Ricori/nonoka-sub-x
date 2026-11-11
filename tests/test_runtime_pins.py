"""The runtime pins (`src/nonoka_x/resources/RUNTIME_PINS.json`) and the engine's bootstrap surface.

`RUNTIME_PINS.json` records what the FineSub runtime is pinned to as of the
start of the refactor (docs/transcription-engines-plan.md, WP-Q item 5): every
package of both runtime locks with version and artefact hashes, every tool
with version, address and hash, every model with revision and file hashes,
the download sources, and the `finesub_bootstrap` constants that decide which
model files are loaded and what the products are called. Since the owner
unlocked `finesub_bootstrap/` (2026-11-11), this is what guards those
contents: whatever Nonoka ships in their place has to state the same pins.
Today the source is `finesub_bootstrap`'s own files; WP-R1 points
`derived_pins()` at Nonoka's manifests.

`tests/fixtures/quality/bootstrap-surface.json` lists every `finesub_bootstrap`
name the locked engine files use. Whatever supplies `finesub_bootstrap` to
the engine must provide each of them, importable without torch or numpy.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.quality_harness import pins  # noqa: E402


def derived_pins() -> dict:
    """The pins as the runtime's current source of truth states them."""

    return pins.with_constants(pins.derive())


class RuntimePinsTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.recorded = json.loads(pins.PINS_FILE.read_text(encoding="utf-8"))

    def test_the_record_matches_what_the_runtime_is_pinned_to(self) -> None:
        derived = derived_pins()
        for section in ("python", "tools", "models", "download_sources", "engine_constants"):
            with self.subTest(section):
                self.assertEqual(derived[section], self.recorded[section])

    def test_every_package_is_pinned_by_version_and_hash(self) -> None:
        for lock in ("global", "cn"):
            packages = self.recorded["python"][lock]
            self.assertGreater(len(packages), 50, lock)
            for package in packages:
                with self.subTest(lock=lock, package=package["name"]):
                    self.assertTrue(package["version"])
                    artefacts = [*package["wheels"], *([package["sdist"]] if "sdist" in package else [])]
                    hashes = [item["sha256"] for item in artefacts]
                    if "archive" in package:
                        hashes.append(package["archive"]["hashes"]["sha256"])
                    self.assertTrue(hashes and all(hashes), "every artefact carries a sha256")
        names = {package["name"] for package in self.recorded["python"]["global"]}
        self.assertTrue({"torch", "ctranslate2", "audio-separator"} <= names)

    def test_every_tool_and_model_file_is_pinned(self) -> None:
        for tool in self.recorded["tools"]["resources"]:
            with self.subTest(tool=tool["id"]):
                self.assertTrue(tool["version"] and tool["url"] and tool["sha256"])
        self.assertIn("ffmpeg", {tool["id"] for tool in self.recorded["tools"]["resources"]})
        for model_id, model in self.recorded["models"].items():
            with self.subTest(model=model_id):
                self.assertTrue(model["files"])
                if "repo" in model:
                    self.assertRegex(model["revision"], r"^[0-9a-f]{40}$")
                # A moving upstream index is fetched unverified on purpose (model-manifest `_note`).
                unpinned = [item["name"] for item in model["files"] if not item.get("sha256")]
                self.assertTrue(set(unpinned) <= {"download_checks.json"}, unpinned)

    def test_the_model_constants_name_the_pinned_models(self) -> None:
        constants = self.recorded["engine_constants"]["finesub_bootstrap.model_caches"]
        self.assertEqual(constants["WHISPER_REPO_ID"], "mobiuslabsgmbh/faster-whisper-large-v3-turbo")
        separator_files = {item["name"] for item in self.recorded["models"]["separator"]["files"]}
        self.assertIn(constants["SEPARATOR_CHECKPOINT"], separator_files)


class BootstrapSurfaceTests(unittest.TestCase):
    def test_the_locked_engine_uses_exactly_the_recorded_surface(self) -> None:
        recorded = json.loads(pins.SURFACE_FILE.read_text(encoding="utf-8"))
        self.assertEqual(pins.bootstrap_surface(), recorded)

    def test_every_name_the_engine_uses_resolves_without_the_asr_stack(self) -> None:
        recorded = json.loads(pins.SURFACE_FILE.read_text(encoding="utf-8"))
        self.assertEqual(pins.unresolvable(recorded), [])


if __name__ == "__main__":
    unittest.main()
