"""The runtime pins (`src/nonoka_x/resources/RUNTIME_PINS.json`) and the engine's bootstrap surface.

`RUNTIME_PINS.json` records what the FineSub runtime is pinned to as of the
start of the refactor (docs/transcription-engines-plan.md, WP-Q item 5): every
package of both runtime locks with version and artefact hashes, every tool
with version, address and hash, every model with revision and file hashes,
the download sources, and the `finesub_bootstrap` constants that decide which
model files are loaded and what the products are called. Since the owner
unlocked `finesub_bootstrap/` (2026-11-11), this is what guards those
contents: whatever Nonoka ships in their place has to state the same pins.

Since WP-R1 the source is Nonoka's own manifests (`nonoka_x.runtime.manifests`):
the runtime lock, the tools and the models, plus the Python interpreter build
the environment layer is based on (plan 4.2 items 13 and 14). Three sections
are not Nonoka's to restate and still come from `finesub_bootstrap`: the
China-mirror lock (`python.cn`; not copied, plan 4.2 item 13), the download
sources, and the engine constants. The transcription package keeps reading
`finesub_bootstrap`'s own copies at run time, so those are held to the same
record too (what `scripts/quality_gate.py` checks).

Regenerate the record from the manifests with
`python tests/test_runtime_pins.py --write` -- only for a pin change the owner
approved, committed on its own.

`tests/fixtures/quality/bootstrap-surface.json` lists every `finesub_bootstrap`
name the locked engine files use. Whatever supplies `finesub_bootstrap` to
the engine must provide each of them, importable without torch or numpy.
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "src"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from nonoka_x.runtime import manifests  # noqa: E402
from tests.quality_harness import pins  # noqa: E402

#: The sections of RUNTIME_PINS.json that `finesub_bootstrap`'s own files state
#: as well (the only ones before WP-R1); `interpreter` is Nonoka's alone.
BOOTSTRAP_SECTIONS = ("python", "tools", "models", "download_sources", "engine_constants")
SECTIONS = (*BOOTSTRAP_SECTIONS, "interpreter")


def _interpreter(manifest: dict[str, Any]) -> dict[str, Any]:
    spec = manifest["interpreter"]
    return {
        "platform": manifest.get("platform"),
        **{key: spec[key] for key in ("implementation", "version", "build")},
        "url": spec["asset"]["url"],
        "size": spec["asset"].get("size"),
        "sha256": spec["asset"].get("sha256"),
    }


def derived_pins() -> dict:
    """The pins as Nonoka's manifests state them (the shape of `pins.derive`)."""

    # rule, schema, python.cn and download_sources: still finesub_bootstrap's.
    bootstrap = pins.derive()
    tools = manifests.tool_manifest()
    models = manifests.model_manifest()
    return pins.with_constants({
        **bootstrap,
        "python": {
            "requires": manifests.runtime_lock().get("requires-python"),
            "global": pins._lock_packages(manifests.RUNTIME_LOCK),
            "cn": bootstrap["python"]["cn"],
        },
        "interpreter": _interpreter(tools),
        "tools": {
            "platform": tools.get("platform"),
            "resources": [
                {
                    "id": item["id"],
                    "version": item["version"],
                    "url": item["asset"]["url"],
                    "size": item["asset"].get("size"),
                    "sha256": item["asset"].get("sha256"),
                    "required_files": item.get("required_files", []),
                }
                for item in tools["resources"]
            ],
        },
        "models": {
            model_id: {
                **{key: spec[key] for key in ("repo", "revision", "listedOnly") if key in spec},
                "files": [
                    {key: item[key] for key in ("name", "url", "size", "sha256") if key in item}
                    for item in spec.get("files", [])
                ],
            }
            for model_id, spec in sorted(models["models"].items())
        },
    })


def write() -> Path:
    pins.PINS_FILE.write_bytes(pins.render(derived_pins()))
    return pins.PINS_FILE


class RuntimePinsTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.recorded = json.loads(pins.PINS_FILE.read_text(encoding="utf-8"))

    def test_the_record_matches_what_nonokas_manifests_pin(self) -> None:
        derived = derived_pins()
        self.assertEqual(sorted(derived), sorted(self.recorded))
        for section in SECTIONS:
            with self.subTest(section):
                self.assertEqual(derived[section], self.recorded[section])

    def test_finesub_bootstraps_own_files_state_the_same_pins(self) -> None:
        derived = pins.with_constants(pins.derive())
        for section in BOOTSTRAP_SECTIONS:
            with self.subTest(section):
                self.assertEqual(derived[section], self.recorded[section])

    def test_the_interpreter_is_the_pinned_standalone_build(self) -> None:
        interpreter = self.recorded["interpreter"]
        self.assertEqual(interpreter["build"], "cpython-3.12.1+20240107-x86_64-pc-windows-msvc-shared-install_only")
        self.assertEqual((interpreter["implementation"], interpreter["version"]), ("cpython", "3.12.1"))
        self.assertTrue(interpreter["url"].startswith("https://github.com/astral-sh/python-build-standalone/"))
        self.assertTrue(interpreter["url"].endswith(".tar.gz"))
        self.assertRegex(interpreter["sha256"], r"^[0-9a-f]{64}$")
        # The prefix the sandbox experiment recorded (docs/engine-sandbox-experiment.md, 2.0).
        self.assertTrue(interpreter["sha256"].startswith("fd5a9e0f"))
        self.assertGreater(interpreter["size"], 0)

    def test_the_lock_is_for_the_pinned_interpreter(self) -> None:
        version = tuple(int(part) for part in self.recorded["interpreter"]["version"].split("."))
        requires = re.fullmatch(r">=(\d+)\.(\d+)", self.recorded["python"]["requires"])
        self.assertIsNotNone(requires)
        self.assertGreaterEqual(version[:2], tuple(int(part) for part in requires.groups()))
        self.assertTrue(manifests.RUNTIME_LOCK.name.endswith(f"py{version[0]}{version[1]}.toml"))

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
    if sys.argv[1:] == ["--write"]:
        print(write())
    else:
        unittest.main()
