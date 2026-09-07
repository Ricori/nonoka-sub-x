from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nonoka_x import worker


class WorkerAdapterTests(unittest.TestCase):
    def test_task_request_maps_to_pipeline_and_returns_hashed_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            task_dir = Path(temp) / "task"
            source = Path(temp) / "video.mp4"
            source.write_bytes(b"media")
            captured: dict = {}

            def run_pipeline(input_path, **kwargs):
                captured["input"] = input_path
                captured.update(kwargs)
                output = Path(kwargs["output_path"])
                output.parent.mkdir(parents=True, exist_ok=True)
                # A raw-srt run: recognition landed, the LLM stages did not.
                # The fake stands in for `PipelinePaths`, so it names every
                # artifact the real one does -- the worker reports the ones
                # that exist, and absence is what a skipped stage looks like.
                stable = output.with_name(f"{output.stem}-stable.json")
                raw = output.with_name(f"{output.stem}-raw.srt")
                vocal = output.with_name(f"{output.stem}-vocal.ogg")
                stable.write_text('{"segments": []}\n', encoding="utf-8")
                raw.write_text("1\n00:00:00,000 --> 00:00:01,000\nx\n", encoding="utf-8")
                vocal.write_bytes(b"OggS")
                return SimpleNamespace(
                    vocal_audio=vocal,
                    resolve_vocal_audio=lambda: vocal,
                    vad_json=output.with_name(f"{output.stem}-vad.json"),
                    vad_energy_npz=output.with_name(f"{output.stem}-vad-energy.npz"),
                    aligned_json=output.with_name(f"{output.stem}-aligned.json"),
                    stable_json=stable,
                    raw_srt=raw,
                    translated_srt=output.with_name(f"{output.stem}-translated.srt"),
                    final_srt=output,
                    metadata_json=output.with_name(f"{output.stem}.run.json"),
                )

            @contextlib.contextmanager
            def passthrough(_value):
                yield

            finesub = types.ModuleType("finesub")
            finesub.__path__ = []
            stages = types.ModuleType("finesub.stages")
            stages.run_pipeline = run_pipeline
            reporting = types.ModuleType("finesub.reporting")
            reporting.reporting_to = passthrough
            reporting.quieted_libraries = passthrough
            previous = {name: sys.modules.get(name) for name in ("finesub", "finesub.stages", "finesub.reporting")}
            sys.modules.update({"finesub": finesub, "finesub.stages": stages, "finesub.reporting": reporting})
            request = {
                "schema": 1,
                "provider": "local",
                "source": {"kind": "local_file", "path": str(source), "title": "video"},
                "target": "raw-srt",
                "language": "ja",
                "device": "cuda:0",
                "gpu_budget_gb": 8,
                "correction": {"media": "audio", "retrieval": "local", "difficulty": "quality"},
                "knowledge": "none",
            }
            output = io.StringIO()
            old_stdin = sys.stdin
            old_cuda_visible = os.environ.get("CUDA_VISIBLE_DEVICES")
            try:
                sys.stdin = io.StringIO(json.dumps(request) + "\n")
                with contextlib.redirect_stdout(output):
                    result = worker.main(
                        [
                            "--task-id",
                            "abc123",
                            "--task-dir",
                            str(task_dir),
                            "--vendor",
                            str(ROOT / "third_party/finesub"),
                        ]
                    )
            finally:
                sys.stdin = old_stdin
                if old_cuda_visible is not None:
                    os.environ["CUDA_VISIBLE_DEVICES"] = old_cuda_visible
                else:
                    os.environ.pop("CUDA_VISIBLE_DEVICES", None)
                for name, value in previous.items():
                    if value is None:
                        sys.modules.pop(name, None)
                    else:
                        sys.modules[name] = value
            self.assertEqual(result, 0)
            self.assertEqual(captured["input"], str(source))
            self.assertEqual(captured["stage"], "raw-srt")
            self.assertEqual(captured["device"], "cuda")
            # Upstream 0.5.0 retired `--gpu-budget-gb` for `--gpu-tier`, and a
            # request queued before the upgrade still carries the number.
            self.assertEqual(captured["gpu_tier"], "standard")
            self.assertNotIn("gpu_budget_gb", captured)
            # No profile switch and no separator profile: the local worker
            # calls `run_pipeline` with upstream's own arguments, which is what
            # makes "local is unpatched upstream" checkable rather than a claim.
            self.assertNotIn("execution_profile", captured)
            self.assertNotIn("vocal_profile", captured)
            self.assertTrue(captured["resume"])
            events = [json.loads(line) for line in output.getvalue().splitlines()]
            completed = next(event for event in events if event["type"] == "completed")
            manifest = completed["payload"]["artifacts"]
            self.assertEqual(
                manifest["engine_commit"],
                json.loads(
                    (ROOT / "third_party/finesub/UPSTREAM.json").read_text(encoding="utf-8")
                )["commit"],
            )
            self.assertEqual(
                set(manifest["artifacts"]), {"vocal_audio", "stable_json", "raw_srt"}
            )
            self.assertEqual(len(manifest["artifacts"]["stable_json"]["sha256"]), 64)

    def test_source_text_axis_skips_recognition_and_only_runs_the_llm_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            task_dir = Path(temp) / "task"
            source = Path(temp) / "video.mp4"
            source.write_bytes(b"media")
            captured: dict = {}

            def run_full_correction(**kwargs):
                captured.update(kwargs)
                output = Path(kwargs["output_path"])
                output.write_text("1\n00:00:00,000 --> 00:00:01,000\n你好\n", encoding="utf-8")
                output.with_name(f"{output.stem}-annotated.csv").write_text("# header\n", encoding="utf-8")
                return output

            @contextlib.contextmanager
            def passthrough(_value):
                yield

            finesub = types.ModuleType("finesub")
            finesub.__path__ = []
            llm = types.ModuleType("finesub.llm")
            llm.__path__ = []
            routing = types.ModuleType("finesub.llm.routing")
            routing.__path__ = []
            correction = types.ModuleType("finesub.llm.correction_translation")
            correction.run_full_correction = run_full_correction
            profiles = types.ModuleType("finesub.llm.routing.profiles")
            profiles.resolve_profile = lambda *args: SimpleNamespace(id="-".join(args))
            stages = types.ModuleType("finesub.stages")
            stages.run_pipeline = lambda *args, **kwargs: self.fail("recognition must not run for a source-text axis")
            stages.resolve_llm_media_for_source = lambda path, **kwargs: (kwargs["llm_media"], None, "")
            reporting = types.ModuleType("finesub.reporting")
            reporting.reporting_to = passthrough
            reporting.quieted_libraries = passthrough
            names = (
                "finesub", "finesub.llm", "finesub.llm.routing", "finesub.llm.correction_translation",
                "finesub.llm.routing.profiles", "finesub.stages", "finesub.reporting",
            )
            previous = {name: sys.modules.get(name) for name in names}
            sys.modules.update({
                "finesub": finesub,
                "finesub.llm": llm,
                "finesub.llm.routing": routing,
                "finesub.llm.correction_translation": correction,
                "finesub.llm.routing.profiles": profiles,
                "finesub.stages": stages,
                "finesub.reporting": reporting,
            })
            request = {
                "schema": 1,
                "provider": "local",
                "source": {"kind": "local_file", "path": str(source), "title": "video"},
                "target": "final-srt",
                "language": "ja",
                "device": "cuda",
                "gpu_budget_gb": 8,
                "correction": {"media": "text", "retrieval": "none", "difficulty": "quality"},
                "knowledge": "none",
                "axis": {
                    "kind": "ja",
                    "filename": "轴.ass",
                    "rows": [
                        {"t0": 0.0, "t1": 1.0, "ja": "こんにちは", "zh": "", "spk": ""},
                        {"t0": 1.0, "t1": 2.0, "ja": "", "zh": "", "spk": ""},
                    ],
                },
            }
            output = io.StringIO()
            old_stdin = sys.stdin
            try:
                sys.stdin = io.StringIO(json.dumps(request) + "\n")
                with contextlib.redirect_stdout(output):
                    result = worker.main([
                        "--task-id", "abc123", "--task-dir", str(task_dir),
                        "--vendor", str(ROOT / "third_party/finesub"),
                    ])
            finally:
                sys.stdin = old_stdin
                for name, value in previous.items():
                    if value is None:
                        sys.modules.pop(name, None)
                    else:
                        sys.modules[name] = value
            self.assertEqual(result, 0)
            # The axis replaces recognition outright: no audio is decoded when
            # the correction reference is text, and the blank row is not handed
            # to the model as something to correct.
            self.assertIsNone(captured["audio_path"])
            stable = json.loads(Path(captured["stable_json"]).read_text(encoding="utf-8"))
            self.assertEqual(stable["segments"], [{"id": "1", "start": 0.0, "end": 1.0, "text": "こんにちは"}])
            events = [json.loads(line) for line in output.getvalue().splitlines()]
            manifest = next(event for event in events if event["type"] == "completed")["payload"]["artifacts"]
            self.assertEqual(set(manifest["artifacts"]), {"stable_json", "annotated_csv", "final_srt"})

    def test_uncalibrated_vector_notice_is_dropped_but_siblings_still_speak(self) -> None:
        # Driven through the engine's own `profile_warnings` rather than a
        # hand-written string: the suppression matches on wording, so an
        # upstream rewording has to fail here instead of quietly letting the
        # line back into every task log.
        from finesub.llm.routing.capabilities import profile_warnings
        from finesub.llm.routing.profiles import parse_profile_id

        vector = (
            "correction_media=video,planning_media=video,"
            "retrieval=none,difficulty=quality,continuity="
        )

        def emitted(profile_id: str) -> list[dict]:
            messages = profile_warnings(parse_profile_id(profile_id))
            self.assertTrue(messages, f"engine said nothing for {profile_id}")
            output = io.StringIO()
            reporter = worker.NonokaXReporter()
            with contextlib.redirect_stdout(output):
                for message in messages:
                    reporter.warning("routing-profile", message)
            return [json.loads(line) for line in output.getvalue().splitlines()]

        self.assertEqual(emitted(vector + "serial"), [])
        parallel = emitted(vector + "parallel")
        self.assertEqual([event["type"] for event in parallel], ["warning"])
        self.assertIn("continuity=parallel", parallel[0]["payload"]["message"])

    def test_normalize_engine_device(self) -> None:
        self.assertEqual(worker._normalize_engine_device("cuda:0"), ("cuda", "0"))
        self.assertEqual(worker._normalize_engine_device("cuda:1"), ("cuda", "1"))
        self.assertEqual(worker._normalize_engine_device("cuda"), ("cuda", None))
        self.assertEqual(worker._normalize_engine_device("cpu"), ("cpu", None))
        self.assertEqual(worker._normalize_engine_device(None), ("cuda", None))

    def test_llm_model_desktop_route_expands_to_upstream_task_groups(self) -> None:
        from finesub.llm.routing.model_routes import (
            default_model_routes,
            install_runtime_preferred,
            runtime_preferred,
        )

        try:
            worker.install_llm_model_override(
                {"llm_model": {"correction": "local-agy-media-gemini-3_7-flash"}}
            )
            self.assertEqual(
                runtime_preferred(),
                {
                    "correction-mm": "local-agy-media-gemini-3_7-flash",
                    "correction-text": "local-agy-media-gemini-3_7-flash",
                },
            )
            # Exercise the real 0.5 route validator, not only the adapter's map.
            default_model_routes()
        finally:
            install_runtime_preferred({})

    def test_llm_model_rejects_conflicting_friendly_and_exact_routes(self) -> None:
        with self.assertRaisesRegex(ValueError, "conflicting targets"):
            worker.install_llm_model_override(
                {
                    "llm_model": {
                        "correction": "local-agy-media-gemini-3_7-flash",
                        "correction-mm": "local-agy-opus-4_6",
                    }
                }
            )


    def test_separator_onnx_cuda_warning_is_filtered(self) -> None:
        import logging

        logger = logging.getLogger("separator")
        record_warning = logging.LogRecord(
            name="separator",
            level=logging.WARNING,
            pathname="separator.py",
            lineno=422,
            msg="CUDAExecutionProvider not available in ONNXruntime, so acceleration will NOT be enabled",
            args=(),
            exc_info=None,
        )
        record_normal = logging.LogRecord(
            name="separator",
            level=logging.WARNING,
            pathname="separator.py",
            lineno=423,
            msg="Another warning",
            args=(),
            exc_info=None,
        )
        self.assertFalse(logger.filter(record_warning))
        self.assertTrue(logger.filter(record_normal))


if __name__ == "__main__":
    unittest.main()
