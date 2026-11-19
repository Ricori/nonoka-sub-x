"""Call equivalence: Nonoka hands FineSub's algorithms what it handed them before.

For every scenario in `tests.quality_harness.calls.SCENARIOS` -- one TaskRequest
each, covering every target, language, separation switch, speaker level,
correction option, knowledge mode, cloud continuation and the Japanese axis --
a child interpreter runs the request through `validate_request` and
`nonoka_x.worker.main` with recording stand-ins at FineSub's algorithm entry
points, and the record must equal `tests/fixtures/quality/calls/<scenario>.json`
byte for byte. The worker environment and the generated `finesub.toml`, model
catalog and env file must equal `tests/fixtures/quality/environment.json`.

The baselines are recorded from the code as it was before the transcription
engine refactor (docs/transcription-engines-plan.md, WP-Q). A difference is a
change in what FineSub is asked to do; it is never fixed by re-recording
without the owner's say (`scripts/quality_gate.py --record`).
"""

from __future__ import annotations

import inspect
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.quality_harness import calls, environment, stubs  # noqa: E402


class CallEquivalenceTests(unittest.TestCase):
    maxDiff = None

    def test_every_scenario_has_a_baseline_and_no_baseline_is_orphaned(self) -> None:
        recorded = {path.stem for path in calls.BASELINE_DIR.glob("*.json")}
        self.assertEqual(recorded, set(calls.SCENARIOS))

    def test_the_matrix_covers_every_value_once(self) -> None:
        requests = [scenario["request"] for scenario in calls.SCENARIOS.values()]
        seen = lambda key: {json.dumps(request.get(key), sort_keys=True) for request in requests}  # noqa: E731
        self.assertTrue({"vocal", "aligned", "stable", "raw-srt", "translated-srt", "final-srt"}
                        <= {request["target"] for request in requests})
        self.assertTrue({"ja", "zh", "auto"} <= {request.get("language") for request in requests})
        self.assertTrue({"true", "false"} <= {json.dumps(request.get("separate", True)) for request in requests})
        levels = {(request.get("diarization") or {}).get("level", "off") for request in requests}
        self.assertTrue({"off", "acoustics", "judge-text", "judge-audio"} <= levels)
        self.assertTrue(any((request.get("diarization") or {}).get("max_speakers") for request in requests))
        corrections = [request.get("correction") or {} for request in requests]
        for key in ("media", "retrieval", "difficulty", "fast", "extra_info", "extra_style"):
            self.assertTrue(any(key in item for item in corrections), key)
        self.assertTrue({"text", "audio", "video"} <= {item.get("media") for item in corrections})
        self.assertTrue({"none", "local", "native"} <= {item.get("retrieval") for item in corrections})
        self.assertTrue({"efficiency", "quality"} <= {item.get("difficulty") for item in corrections})
        self.assertTrue({'"none"', '"read"', '"update"', "null"} <= seen("knowledge"))
        self.assertTrue(any(request.get("knowledge_subjects") for request in requests))
        kinds = [request["source"].get("kind") for request in requests]
        self.assertIn("cloud_transcript", kinds)
        native = [request["source"].get("native") or {} for request in requests
                  if request["source"].get("kind") == "cloud_transcript"]
        self.assertTrue(any("finesub.diar_json" in attachments for attachments in native))
        self.assertTrue(any("finesub.diar_json" not in attachments for attachments in native))
        self.assertTrue(any(request.get("axis") for request in requests))

    def test_algorithm_entry_points_receive_the_baseline_arguments(self) -> None:
        records = calls.run_all()
        for name, record in records.items():
            with self.subTest(name):
                self.assertNotIn("rejected", record, "a matrix request no longer validates")
                self.assertEqual(record.get("exit"), 0, record.get("failed"))
        found = calls.differences(records)
        self.assertFalse(found, "\n\n".join(f"== {name}\n{diff}" for name, diff in found.items()))

    def test_worker_environment_and_generated_config_match_the_baseline(self) -> None:
        diff = environment.difference(environment.run_all())
        self.assertEqual(diff, "", diff)


class LlmReplayTests(unittest.TestCase):
    """The LLM half from a fixed stable: requests sent, and what fixed answers render to.

    `tests.quality_harness.replay` catches every API call at
    `finesub.llm.llm_runtime.chat_complete` and answers it deterministically;
    the request sequence (messages in full, routed model, parameters) and the
    rendered `final.srt` / `-annotated.csv` must equal the baseline byte for
    byte. Cases built on the real sample's recognition run only where it was
    recorded (`python -m tests.quality_harness.real record`).
    """

    maxDiff = None

    def test_committed_cases_have_baselines(self) -> None:
        from tests.quality_harness import replay

        public = {name for name, case in replay.CASES.items() if not case.get("private")}
        self.assertEqual({path.stem for path in replay.BASELINE_DIR.glob("*.json")}, public)

    def test_requests_and_rendered_subtitles_match_the_baseline(self) -> None:
        from tests.quality_harness import replay

        records = replay.run_all()
        skipped = sorted(set(replay.CASES) - set(records))
        for name in skipped:
            self.assertTrue(replay.CASES[name].get("private"), f"{name} could not run")
        for name, record in records.items():
            with self.subTest(name):
                self.assertEqual(record["exit"], 0, record["failed"])
        found = replay.differences(records)
        self.assertFalse(found, "\n\n".join(f"== {name}\n{diff}" for name, diff in found.items()))


class RecorderSensitivityTests(unittest.TestCase):
    """The record reacts to an argument added, dropped or changed, and to nothing else."""

    def signature(self) -> inspect.Signature:
        def entry(audio, *, device="cuda", language=None, max_speakers=None):  # noqa: ANN001, ANN202
            return None

        return inspect.signature(entry)

    def recorded(self, *args, **kwargs) -> dict:
        stubs.RECORD.clear()
        stubs.record("toy", self.signature(), args, kwargs)
        return stubs.RECORD.pop()["arguments"]

    def test_positional_keyword_and_explicit_default_record_the_same(self) -> None:
        self.assertEqual(self.recorded("a.ogg"), self.recorded(audio="a.ogg", device="cuda"))

    def test_a_changed_value_is_recorded(self) -> None:
        self.assertNotEqual(self.recorded("a.ogg"), self.recorded("a.ogg", device="cpu"))
        self.assertNotEqual(self.recorded("a.ogg"), self.recorded("a.ogg", max_speakers=2))

    def test_an_argument_the_entry_point_does_not_take_fails_the_run(self) -> None:
        with self.assertRaises(TypeError):
            self.recorded("a.ogg", gpu_budget_gb=8)

    def test_a_missing_required_argument_fails_the_run(self) -> None:
        with self.assertRaises(TypeError):
            self.recorded(device="cuda")

    def test_signatures_read_from_source_match_the_locked_files(self) -> None:
        signature = stubs.signature_from_source("finesub/speech/recognition/vad_asr_stage.py", "run_vad_asr")
        self.assertIn("asr_context", signature.parameters)
        self.assertIn("vad_prefix_path", signature.parameters)

    def test_paths_under_a_run_root_are_normalized(self) -> None:
        root = Path(r"C:\runs\abc")
        normalizer = calls.Normalizer({"$ROOT": root})
        self.assertEqual(
            normalizer.text(str(root / "workspace" / ".节目-stable.part-123-0a1b2c3d.json")),
            "$ROOT/workspace/.节目-stable.part-$WRITER.json",
        )


if __name__ == "__main__":
    unittest.main()
