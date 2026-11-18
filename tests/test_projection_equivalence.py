"""The red line of plan 4.8 item 38: the same FineSub task projects to the same document after WP-L.

Before WP-L the core lined FineSub's products up itself (`tests/legacy_projector.py`,
the projector as it was); now the correction package writes `correction.json` and
the core copies it. For every case here, the products of one real run are
projected both ways and the documents compared whole -- before and after the
provider's own steps (the speaker tracks, the hidden source lane):

- every LLM replay case of the quality gate (`tests/quality_harness/replay.py`;
  the five over the real sample when `test_data/` has them): `final` mode as the
  run left it, the `stable` mode of the same products (a run that stopped at the
  raw SRT), and the relaxed lining-up a cloud library entry was fetched with
  (what the one-time conversion uses). They include a speaker case (the
  multi-person sample, judge-text). The replay's `zh` case records
  `correct_only: false`, so a recorded correct-only run is made here too;
- every call-equivalence scenario (`tests/quality_harness/calls.py`), whose
  stand-ins leave products of other shapes: raw-srt targets with and without
  speakers, `stable` targets (described at projection time), the Japanese axis
  (`axis-ja-*`), cloud continuations. The stand-ins of an LLM stage leave rows
  that do not line up, which the old projection refused: there the run's
  manifest carries the same refusal (`projection_error`), which the provider
  raises.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse
from urllib.request import url2pathname

from finesub_correct.document import product_rows, write_correction
from nonoka_x import projector
from nonoka_x.axis import split_diarized_tracks
from tests import legacy_projector
from tests.quality_harness import calls, replay

TITLE = "节目"


def legacy(stable: Path, annotated: Path | None, final: Path | None, mode: Path | None, *, relaxed: bool = False) -> dict:
    return legacy_projector.project_edit_document(
        stable, annotated_csv=annotated, final_srt=final, correction_mode=mode,
        video_id="v", title=TITLE, relaxed_srt=relaxed,
    )


def current(correction: Path) -> dict:
    return projector.project_edit_document(correction_json=correction, video_id="v", title=TITLE)


def finished(document: Mapping[str, Any]) -> dict:
    """The provider's steps after the projection, without an axis (`LocalProvider._project_manifest`)."""

    document = split_diarized_tracks(document)
    if document["projection"].get("correct_only"):
        document = projector.hide_source_lanes(document)
    return document


def file_of(entry: Mapping[str, Any] | None) -> Path | None:
    if not entry:
        return None
    return Path(url2pathname(urlparse(entry["uri"]).path))


class ProjectionEquivalenceTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        legacy_projector.set_product_reader(product_rows)
        cls.scratch = Path(tempfile.mkdtemp(prefix="nx-eq-"))

    @classmethod
    def tearDownClass(cls) -> None:
        legacy_projector.set_product_reader(None)
        shutil.rmtree(cls.scratch, ignore_errors=True)

    def assertSameDocument(self, before: dict, after: dict) -> None:
        self.assertEqual(after, before)
        self.assertEqual(finished(after), finished(before))

    def test_every_replay_case_projects_the_same_document(self) -> None:
        names = [name for name in replay.CASES if replay.available(name)]
        if len(names) < len(replay.CASES):
            print(f"\nprojection equivalence: replay cases without their inputs here, not compared: "
                  f"{sorted(set(replay.CASES) - set(names))}")
        self.assertTrue(names)
        with ThreadPoolExecutor(max_workers=4) as pool:
            records = dict(zip(names, pool.map(lambda name: replay.run_case(name, self.scratch / "replay"), names)))
        for name in names:
            with self.subTest(case=name):
                self.assertEqual(records[name]["exit"], 0, records[name].get("failed"))
                task_dir = self.scratch / "replay" / name / "data" / "tasks" / replay.TASK_ID
                native = task_dir / replay.CORRECTION_OUTPUTS
                stable, annotated, final = native / f"{TITLE}-stable.json", native / f"{TITLE}-annotated.csv", native / f"{TITLE}.srt"
                mode = native / "llm-artifacts" / "correction-mode.json"
                mode = mode if mode.is_file() else None
                correction = task_dir / "correction.json"
                self.assertTrue(correction.is_file())
                if annotated.is_file() and final.is_file():
                    self.assertEqual(json.loads(correction.read_text(encoding="utf-8"))["mode"], "final")
                    self.assertSameDocument(legacy(stable, annotated, final, mode), current(correction))
                    relaxed = write_correction(task_dir / "relaxed.json", stable, annotated_csv=annotated,
                                               final_srt=final, relaxed=True)
                    self.assertSameDocument(legacy(stable, annotated, final, None, relaxed=True), current(relaxed))
                else:
                    self.assertSameDocument(legacy(stable, None, None, mode), current(correction))
                raw = write_correction(task_dir / "stable-mode.json", stable)
                self.assertSameDocument(legacy(stable, None, None, None), current(raw))
        speakers = [name for name in names if any(
            line.get("speaker") for line in json.loads(
                (self.scratch / "replay" / name / "data" / "tasks" / replay.TASK_ID / "correction.json")
                .read_text(encoding="utf-8"))["lines"])]
        if "real-multi-person-judge-text" in names:
            self.assertIn("real-multi-person-judge-text", speakers)

    def test_a_correct_only_run_projects_the_same_document(self) -> None:
        """The replay's `zh` case records `correct_only: false` (its stable is Japanese), so a recorded one is made here."""

        root = self.scratch / "correct-only"
        root.mkdir(parents=True)
        stable = root / "zh-stable.json"
        stable.write_text(json.dumps({"segments": [
            {"start": 1.0, "end": 2.0, "text": "就是那个那个新角色"},
            {"start": 2.4, "end": 2.6, "text": "呃"},
            {"start": 3.0, "end": 4.0, "text": "下期再讲"},
        ]}, ensure_ascii=False), encoding="utf-8")
        annotated = root / "zh-annotated.csv"
        annotated.write_text(
            "# type|position|duration|gap|corrected|translation|conf|char_count|spk|note\n"
            "sub|1|1.0||就是那个那个新角色|就是那个新角色|high|7||\n"
            "sub|2|0.2|0.4|呃||high|0||整行只有垫词\n"
            "sub|3|1.0|0.4|下期再讲|下期再讲|high|4||\n",
            encoding="utf-8",
        )
        final = root / "zh.srt"
        final.write_text("1\n00:00:01,000 --> 00:00:02,300\n就是那个新角色\n\n"
                         "2\n00:00:03,000 --> 00:00:04,300\n下期再讲\n", encoding="utf-8")
        mode = root / "correction-mode.json"
        mode.write_text('{"version": 1, "mode": "single", "correct_only": true}', encoding="utf-8")
        correction = write_correction(root / "correction.json", stable, annotated_csv=annotated,
                                      final_srt=final, correction_mode=mode)
        before = legacy(stable, annotated, final, mode)
        self.assertTrue(before["projection"].get("correct_only"))
        self.assertSameDocument(before, current(correction))

    def test_every_call_scenario_projects_the_same_document_or_refuses_alike(self) -> None:
        names = list(calls.SCENARIOS)
        scratch = self.scratch / "calls"
        def run(name: str) -> None:
            # Once more when the harness could not read a product back: a
            # file resolved while something else held it open comes back as
            # an extended-length path, whose URI is not a local file one. That
            # happens under parallel children whatever this test checks (WP-L notes).
            try:
                calls.run_scenario(name, scratch)
            except OSError:
                calls.run_scenario(name, scratch)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(run, names))
        compared = []
        for name in names:
            raw = json.loads((scratch / name / "record.json").read_text(encoding="utf-8"))
            manifest = raw.get("manifest")
            if not manifest:
                continue
            entries = manifest["artifacts"]
            if "stable_json" not in entries and "transcript_json" not in entries:
                continue
            if "stable_json" not in entries:
                # Only a transcript (an `aligned` target): the old projection had
                # nothing it could read and failed the task; now the transcript is it.
                self.assertNotIn("correction_json", entries)
                current_transcript = projector.project_edit_document(
                    transcript_json=file_of(entries["transcript_json"]), video_id="v", title=TITLE)
                self.assertEqual(current_transcript["projection"]["mode"], "stable")
                compared.append((name, "transcript only"))
                continue
            with self.subTest(scenario=name):
                annotated, final = file_of(entries.get("annotated_csv")), file_of(entries.get("final_srt"))
                together = annotated is not None and final is not None
                try:
                    before = legacy(file_of(entries["stable_json"]), annotated if together else None,
                                    final if together else None, file_of(entries.get("correction_mode_json")))
                except legacy_projector.ProjectionError as error:
                    self.assertNotIn("correction_json", entries)
                    self.assertEqual(manifest.get("projection_error"), f"ProjectionError: {error}")
                    compared.append((name, "refused"))
                    continue
                self.assertNotIn("projection_error", manifest)
                correction = file_of(entries.get("correction_json"))
                if correction is None:
                    # A target up to `stable` runs no correction: the provider
                    # asks the correction package to describe its stable when it
                    # projects (`LocalProvider._described`, `describe`), which
                    # writes this.
                    self.assertFalse(together)
                    correction = write_correction(scratch / name / "described.json", file_of(entries["stable_json"]))
                self.assertSameDocument(before, current(correction))
                compared.append((name, "final" if together else "stable"))
        kinds = {kind for _name, kind in compared}
        # The stand-ins of a final-srt scenario leave products the old projection
        # refused (no row lines up), so `final` is the replay test's to compare.
        self.assertEqual(kinds, {"stable", "refused", "transcript only"}, compared)
        self.assertTrue(any(name.startswith("stable-") and kind == "stable" for name, kind in compared), compared)
        self.assertTrue(any(name.startswith("axis-ja") for name, _kind in compared), compared)


class DescribedAtProjectionTests(unittest.TestCase):
    """A task that stopped at `stable`: its document is the correction package's reading of FineSub's stable."""

    def test_the_provider_asks_the_correction_package_and_records_the_file(self) -> None:
        from nonoka_x.local_provider import LocalProvider

        stable_fixture = Path(__file__).parent / "fixtures" / "finesub" / "sample-stable.json"
        with tempfile.TemporaryDirectory(prefix="nx-") as temp:
            root = Path(temp)
            provider = LocalProvider(root / "tasks", Path(__file__).resolve().parents[1] / "engines", issues=[])
            try:
                task_dir = root / "tasks" / "abc123"
                native = task_dir / "native" / "finesub-transcribe"
                native.mkdir(parents=True)
                stable = native / f"{TITLE}-stable.json"
                stable.write_bytes(stable_fixture.read_bytes())
                transcript = task_dir / "transcript.json"
                transcript.write_text(json.dumps({
                    "schema": 1, "engine": "finesub", "language": "ja", "duration": 6.0,
                    "lines": [{"id": "1", "start": 1.0, "end": 2.0, "text": "おはよう"}],
                }, ensure_ascii=False), encoding="utf-8")
                manifest = {"schema": 1, "task_id": "abc123", "engine_commit": "c", "artifacts": {
                    "stable_json": {"uri": stable.resolve().as_uri()},
                    "transcript_json": {"uri": transcript.resolve().as_uri()},
                }}
                (task_dir / "artifacts.json").write_text(json.dumps(manifest), encoding="utf-8")
                document = provider._project_manifest("loc_1", {"title": TITLE}, manifest)
                legacy_projector.set_product_reader(product_rows)
                try:
                    before = legacy(stable, None, None, None)
                finally:
                    legacy_projector.set_product_reader(None)
                recorded = json.loads((task_dir / "artifacts.json").read_text(encoding="utf-8"))
            finally:
                provider.shutdown()
        self.assertEqual(document["subtitles"], finished(before)["subtitles"])
        self.assertTrue(document["subtitles"][2]["low_conf"])
        self.assertIn("correction_json", recorded["artifacts"])
        self.assertEqual(len(recorded["artifacts"]["correction_json"]["sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
