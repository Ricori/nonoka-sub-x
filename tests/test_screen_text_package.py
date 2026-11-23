"""The on-screen text package (`engines/flowocr`) as the core sees it, and its `tracks.json` -> `ocr.json`.

What the engine host does with an `ocr` package: it runs in the sandbox, on its
own layer's interpreter or not at all, with no keys and no LLM routes in its
context, and never writes bytecode. The conversion is checked on a small
hand-made `tracks.json`, through the vendored `tracksio` itself.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from nonoka_x import engine_contexts, engine_host
from nonoka_x.runtime.paths import NonokaPaths

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "engines" / "flowocr"


def _ops() -> Any:
    for path in (PACKAGE / "src", PACKAGE / "adapter"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    spec = importlib.util.spec_from_file_location("flowocr_host_ops", PACKAGE / "adapter" / "flowocr_host" / "ops.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class Provisioner:
    def __init__(self, root: Path | None) -> None:
        self.root = root

    def screen_text_root(self) -> Path | None:
        return self.root

    def screen_text_python(self) -> Path | None:
        return self.root / "python.exe" if self.root is not None else None

    def tool_path(self, name: str) -> Path | None:
        return None


class Settings:
    def engine_secrets(self, names: Any) -> dict[str, str]:
        raise AssertionError("an ocr package is never handed keys")

    def llm_context(self) -> dict[str, Any]:
        raise AssertionError("an ocr package is never handed the LLM routes")


class EngineHostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.packages = engine_host.discover(ROOT / "engines")
        self.package = self.packages["flowocr"]
        self.temp = Path(tempfile.mkdtemp(prefix="nonoka-ocr-"))
        self.paths = NonokaPaths.for_roots(self.temp / "data", self.temp / "runtime")

    def test_it_is_found_as_the_ocr_package(self) -> None:
        self.assertEqual(self.package.kind, "ocr")
        self.assertIs(engine_host.ocr_package(self.packages), self.package)
        self.assertTrue(engine_host.sandboxed(self.package))
        self.assertEqual(engine_host.interpreter_flags(self.package), ["-I", "-B"])

    def test_its_context_has_its_layer_and_no_keys(self) -> None:
        layer = self.temp / "runtime" / "layers" / "screen-text" / "v1"
        context = engine_contexts.package_context(self.package, paths=self.paths, run=self.temp / "run",
                                                  settings=Settings(), provisioner=Provisioner(layer))
        self.assertNotIn("secrets", context)
        self.assertNotIn("llm", context)
        self.assertNotIn("agent_cli", context)
        runtime = context["runtime"]
        self.assertEqual(runtime["python"], str(layer / "python.exe"))
        self.assertEqual(runtime["models"], str(layer / "models"))
        self.assertEqual(runtime["cache"], str(self.paths.cache / "flowocr"))
        self.assertEqual(runtime["gametext"], str(self.paths.engine("flowocr") / "gametext"))

    def test_without_its_layer_it_does_not_start(self) -> None:
        context = engine_contexts.package_context(self.package, paths=self.paths, run=self.temp / "run",
                                                  settings=Settings(), provisioner=Provisioner(None))
        self.assertNotIn("python", context["runtime"])
        with self.assertRaises(engine_host.PackageUnavailable):
            engine_host.interpreter_for(self.package, context)

    def test_its_environment_writes_no_bytecode_and_uses_the_sandbox(self) -> None:
        context = {"paths": {"package_data": str(self.temp / "flowocr"), "run": str(self.temp / "flowocr" / "run")},
                   "runtime": {"tools": []}}
        with engine_host.SandboxTemp(engine_host.sandbox_root(context)) as sandbox:
            environment = engine_host.package_environment(self.package, context, {}, sandbox=sandbox)
        self.assertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(environment["TEMP"], str(sandbox.temp))
        self.assertNotIn("CLAUDE_CONFIG_DIR", environment)

    def test_an_unknown_kind_is_refused_not_treated_as_the_correction(self) -> None:
        stray = engine_host.Package(self.package.root, {**self.package.manifest, "kind": "something-new"})
        with self.assertRaises(engine_host.PackageUnavailable):
            engine_host.package_environment(stray, {}, {})


def _tracks() -> dict[str, Any]:
    """Two regions: a caption on the right (two lines, one cue) and the clock top left."""

    def event(eid: int, region: int, t0: int, t1: int, box: list[float], text: str) -> dict[str, Any]:
        return {"id": eid, "region": region, "t_start": t0, "t_full": None, "t_end": t1, "box": box,
                "text": text, "conf": 0.9 + eid / 100, "n_obs": 4, "n_independent": 4, "flags": []}

    events = [
        event(0, 0, 1_000_000, 3_000_000, [1400, 200, 1800, 260], "限定ガチャ"),
        event(1, 0, 1_000_000, 3_000_000, [1400, 270, 1700, 330], "本日まで"),
        event(2, 1, 0, 9_000_000, [20, 20, 140, 50], "12:00"),
    ]
    return {
        "schema": "flowocr-tracks/1", "tag": "v", "meta": {}, "frame_us": 500_000, "size": [1920, 1080],
        "window_sec": 60, "n_windows": 1, "provenance": {"main_track": ""},
        "regions": [{"index": 0, "label": "dialogue-or-caption", "align": "right", "align_how": {"by": "stats"}},
                    {"index": 1, "label": "clock"}],
        "tracks": [
            {"id": "r00", "kind": "region", "region": 0, "label": "dialogue-or-caption", "srt": "", "ui_lines": [],
             "cues": [{"id": "r00c00000", "t_start": 1_000_000, "t_end": 3_000_000, "events": [0, 1]}]},
            {"id": "r01", "kind": "region", "region": 1, "label": "clock", "srt": "", "ui_lines": [],
             "cues": [{"id": "r01c00000", "t_start": 0, "t_end": 9_000_000, "events": [2]}]},
        ],
        "events": events,
    }


class ConversionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ops = _ops()
        from flowocr.artifacts import tracksio

        tracksio.validate(_tracks())

    def test_regions_cues_and_normalized_boxes(self) -> None:
        document = self.ops.to_ocr_document(_tracks(), video={"w": 1920, "h": 1080, "fps": 30.0, "duration": 9.0},
                                            engine={"id": "flowocr"})
        self.assertEqual(document["schema"], 1)
        self.assertEqual(document["video"], {"w": 1920, "h": 1080, "fps": 30.0, "duration": 9.0})
        regions = {region["id"]: region for region in document["regions"]}
        self.assertEqual(regions["r00"]["name"], "画面·对白")
        self.assertFalse(regions["r00"]["skip"])
        self.assertTrue(regions["r01"]["skip"], "the clock is never translated")
        caption = next(cue for cue in document["cues"] if cue["region"] == "r00")
        self.assertEqual([line["text"] for line in caption["lines"]], ["限定ガチャ", "本日まで"])
        self.assertEqual((caption["t0"], caption["t1"]), (1.0, 3.0))
        self.assertEqual(caption["an"], 9, "a right-aligned region anchors top right")
        self.assertAlmostEqual(caption["box"][0], 1400 / 1920, places=4)
        self.assertAlmostEqual(caption["box"][3], 330 / 1080, places=4)
        self.assertAlmostEqual(caption["fs"], 60 / 1080, places=4)
        self.assertIsNone(caption["fill"])

    def test_a_repeated_label_is_numbered(self) -> None:
        tracks = _tracks()
        tracks["regions"][1] = {"index": 1, "label": "dialogue-or-caption"}
        tracks["tracks"][1]["label"] = "dialogue-or-caption"
        document = self.ops.to_ocr_document(tracks)
        self.assertEqual([region["name"] for region in document["regions"]], ["画面·对白", "画面·对白 2"])

    def test_the_main_band_is_kept_by_default(self) -> None:
        tracks = _tracks()
        tracks["provenance"]["main_track"] = "r00"
        regions = {region["id"]: region for region in self.ops.to_ocr_document(tracks)["regions"]}
        self.assertFalse(regions["r00"]["skip"])

    def test_the_roi_keeps_only_lines_centred_inside(self) -> None:
        document = self.ops.to_ocr_document(_tracks(), roi=[0.5, 0.0, 1.0, 0.25])
        caption = next(cue for cue in document["cues"] if cue["region"] == "r00")
        self.assertEqual([line["text"] for line in caption["lines"]], ["限定ガチャ"])
        self.assertFalse(any(cue["region"] == "r01" for cue in document["cues"]), "the clock is outside")

    def test_noise_with_confident_words_is_read_and_digits_are_not(self) -> None:
        tracks = _tracks()
        tracks["regions"][1]["label"] = "noise"
        tracks["tracks"][1]["label"] = "noise"
        regions = {region["id"]: region for region in self.ops.to_ocr_document(tracks)["regions"]}
        self.assertTrue(regions["r01"]["skip"], "12:00 is not words")
        tracks["events"][2]["text"] = "Options"
        regions = {region["id"]: region for region in self.ops.to_ocr_document(tracks)["regions"]}
        self.assertFalse(regions["r01"]["skip"])

    def test_line_boxes_are_fitted_to_the_ink_vertically_only(self) -> None:
        import numpy as np

        frame = np.full((100, 200, 3), (40, 20, 30), dtype=np.uint8)
        frame[30:50, 20:120] = (250, 250, 250)                 # the glyphs of a line
        cue = {"lines": [{"text": "x", "box": [0.05, 0.25, 0.75, 0.55]}], "box": [0.05, 0.25, 0.75, 0.55], "fs": 0.3}
        self.ops._tighten_lines(cue, frame, (40, 20, 30), np)
        self.assertEqual(cue["lines"][0]["box"], [0.05, 0.3, 0.75, 0.5], "top and bottom on the ink, sides kept")
        self.assertEqual(cue["fs"], 0.2)
        half_typed = {"lines": [{"text": "x", "box": [0.05, 0.0, 0.75, 1.0]}], "box": [0.05, 0.0, 0.75, 1.0], "fs": 1.0}
        self.ops._tighten_lines(half_typed, frame, (40, 20, 30), np)
        self.assertEqual(half_typed["lines"][0]["box"], [0.05, 0.0, 0.75, 1.0], "too little ink: not trusted")

    def test_the_anchor_follows_the_edge_the_lines_share(self) -> None:
        dialogue = [[0.217, 0.80, 0.322, 0.84], [0.217, 0.85, 0.487, 0.89], [0.218, 0.80, 0.672, 0.84],
                    [0.219, 0.80, 0.411, 0.83], [0.484, 0.77, 0.516, 0.81], [0.542, 0.79, 0.575, 0.81]]
        self.assertEqual(self.ops.align_of(dialogue, "center"), "left", "strays do not outvote the dialogue")
        mirrored = [[1 - b[2], b[1], 1 - b[0], b[3]] for b in dialogue]
        self.assertEqual(self.ops.align_of(mirrored, "center"), "right")
        self.assertEqual(self.ops.align_of(dialogue[:2], "right"), "right", "too few lines: FlowOCR's call")
        alike = [[0.2, 0.1 * i, 0.4, 0.1 * i + 0.05] for i in range(4)]
        self.assertEqual(self.ops.align_of(alike, "left"), "left", "all edges agree: FlowOCR's call")

    def test_the_device_names_map_onto_flowocrs(self) -> None:
        self.assertEqual(self.ops._device_arg("cpu"), "cpu")
        self.assertEqual(self.ops._device_arg("cuda:1"), "gpu:1")
        self.assertEqual(self.ops._device_arg("auto"), "auto")
        self.assertTrue(self.ops._driver_ok("580.12"))
        self.assertFalse(self.ops._driver_ok("576.88"))

    @unittest.skipUnless(sys.platform == "win32", "Windows Job Object only")
    def test_jobobject_bind_succeeds_on_64bit_windows(self) -> None:
        import subprocess

        adapter = str(ROOT / "engines" / "flowocr" / "adapter")
        out = subprocess.run(
            [sys.executable, "-c", f"import sys; sys.path.insert(0, {adapter!r}); from flowocr_host import jobobject; print(jobobject.bind())"],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(out.stdout.strip(), "True")


if __name__ == "__main__":
    unittest.main()
