from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
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
    def test_knowledge_subjects_are_added_to_engine_extra_info(self) -> None:
        subjects = [
            {
                "id": "s1",
                "kind": "streamer",
                "subject": "猫又おかゆ",
                "aliases": "Okayu、猫",
                "description": "Hololive 所属 VTuber",
            },
            {"kind": "work", "subject": "Minecraft", "aliases": "", "description": ""},
        ]
        text = worker.compose_knowledge_context(
            {"knowledge": "update", "knowledge_subjects": subjects},
            "这期嘉宾是戌神ころね。",
        )

        self.assertIn("- 主播 / 频道：猫又おかゆ（别名 / 常用译名：Okayu、猫）；Hololive 所属 VTuber", text)
        self.assertIn("- 作品 / 节目：Minecraft", text)
        self.assertIn("归入上述主体中最贴切的一个", text)
        self.assertIn("【其他背景信息】\n这期嘉宾是戌神ころね。", text)

        # Reading names the subjects too, but asks for no write-back.
        collect = worker.compose_knowledge_context(
            {"knowledge": "read", "knowledge_subjects": subjects[:1]}, "原始背景"
        )
        self.assertIn("猫又おかゆ", collect)
        self.assertNotIn("归入", collect)
        self.assertEqual(
            worker.compose_knowledge_context(
                {"knowledge": "none", "knowledge_subjects": subjects}, "原始背景"
            ),
            "原始背景",
        )
        # A request queued before the list existed still carries its subject.
        legacy = worker.compose_knowledge_context(
            {"knowledge": "update", "knowledge_context": subjects[1]}
        )
        self.assertIn("Minecraft", legacy)

    def test_knowledge_subjects_resolve_existing_and_create_new(self) -> None:
        from finesub.llm.knowledge.node.repo import KnowledgeRepo

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "knowledge"
            first = {
                "knowledge": "update",
                "knowledge_subjects": [
                    {
                        "kind": "streamer",
                        "subject": "柊優花",
                        "aliases": "柊优花, 优花、yuka",
                        "description": "以游戏直播为主的主播",
                    },
                    {"kind": "work", "subject": "星之海", "aliases": "", "description": ""},
                ],
            }
            try:
                result = worker.prepare_knowledge_subjects(first, "task-1", knowledge_root=root)
                self.assertEqual(result["created"], ["柊優花", "星之海"])
                self.assertEqual(result["names"], ["streamer/柊優花", "common/星之海"])
                repo = KnowledgeRepo.open(root)
                resolved = repo.resolve("柊优花")
                assert resolved is not None
                self.assertEqual(resolved.key, "柊優花")
                self.assertIn("以游戏直播为主的主播", repo.index_text("streamer"))

                # A repeat creates nothing, and an existing subject travels by id.
                again = worker.prepare_knowledge_subjects(first, "task-2", knowledge_root=root)
                self.assertEqual(again["created"], [])
                self.assertEqual(again["rev"], result["rev"])
                ident = next(s.local_id for s in repo.subjects() if s.payload["surface"] == "星之海")
                read = worker.prepare_knowledge_subjects(
                    {
                        "knowledge": "read",
                        "knowledge_subjects": [
                            {"id": ident, "kind": "topic", "subject": "星之海", "aliases": "", "description": ""}
                        ],
                    },
                    "task-3",
                    knowledge_root=root,
                )
                self.assertEqual(read["names"], ["common/星之海"])

                # Reading never creates.
                with self.assertRaisesRegex(RuntimeError, "只有自动更新模式可以新建主体"):
                    worker.prepare_knowledge_subjects(
                        {
                            "knowledge": "read",
                            "knowledge_subjects": [
                                {"kind": "topic", "subject": "不存在", "aliases": "", "description": ""}
                            ],
                        },
                        "task-4",
                        knowledge_root=root,
                    )
                with self.assertRaisesRegex(RuntimeError, "已不在知识库中"):
                    worker.prepare_knowledge_subjects(
                        {
                            "knowledge": "read",
                            "knowledge_subjects": [
                                {"id": "gone", "kind": "topic", "subject": "旧主体", "aliases": "", "description": ""}
                            ],
                        },
                        "task-5",
                        knowledge_root=root,
                    )
            finally:
                KnowledgeRepo.forget(root)

    def test_knowledge_task_summary_reaches_the_update_with_subject_identity(self) -> None:
        summary = worker.knowledge_task_summary(
            {
                "knowledge": "update",
                "knowledge_subjects": [
                    {"kind": "streamer", "subject": "柊優花", "aliases": "优花", "description": ""},
                    {"kind": "topic", "subject": "Hololive", "aliases": "", "description": ""},
                ],
            },
            "测试视频",
        )
        self.assertIn("字幕任务：测试视频", summary)
        self.assertIn("- 主播 / 频道：柊優花", summary)
        self.assertIn("- 其他知识主题：Hololive", summary)

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
                    diar_json=output.with_name(f"{output.stem}-diar.json"),
                    speaker_judge_json=output.with_name(f"{output.stem}-speaker-judge.json"),
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
            self.assertEqual(captured["task_summary"], "字幕任务：video")
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

    def test_a_cloud_stable_is_seeded_under_the_engine_name_only_once(self) -> None:
        """The continuation is a file in the right place, and nothing else.

        `run_pipeline` resumes from whatever stable.json sits under its own
        name, so the seed has to land exactly there -- and a retry must find
        the copy its first attempt rendered from, not a fresh one.
        """

        sys.path.insert(0, str(ROOT / "third_party/finesub/src"))
        from finesub.stages import default_pipeline_paths

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cloud = root / "cloud.stable.json"
            cloud.write_text('{"segments": [{"text": "first"}]}', encoding="utf-8")
            media = root / "media.m4a"
            output = root / "task" / "workspace" / "片子.srt"
            output.parent.mkdir(parents=True)
            request = {"source": {"kind": "cloud_stable", "path": str(media), "stable_json": str(cloud)}}

            seeded = worker.seed_cloud_stable(request, output)
            self.assertEqual(seeded, Path(default_pipeline_paths(media, output).stable_json))
            self.assertEqual(seeded.read_text(encoding="utf-8"), cloud.read_text(encoding="utf-8"))
            self.assertEqual(sorted(path.name for path in output.parent.iterdir()), [seeded.name])

            cloud.write_text('{"segments": [{"text": "second"}]}', encoding="utf-8")
            worker.seed_cloud_stable(request, output)
            self.assertIn("first", seeded.read_text(encoding="utf-8"))

            local = {"source": {"kind": "local_file", "path": str(media)}}
            self.assertIsNone(worker.seed_cloud_stable(local, root / "other" / "x.srt"))

    def test_a_continuation_runs_without_any_module_of_the_asr_stack(self) -> None:
        """The reason the launcher can continue a cloud task at all.

        Run in a child whose import system refuses torch, numpy and the rest:
        a module-level import anywhere between `nonoka_x.worker` and the raw
        SRT -- vendor patch 0010 is what removed the last ones -- fails this
        rather than the first user without the managed runtime.
        """

        script = """
import importlib.abc, sys
BLOCKED = {"torch", "torchaudio", "numpy", "soundfile", "faster_whisper", "ctranslate2", "transformers", "audio_separator"}
class Refuse(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError("blocked for this test: " + name)
        return None
sys.meta_path.insert(0, Refuse())
from nonoka_x import worker
raise SystemExit(worker.main(sys.argv[1:]))
"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cloud = root / "cloud.stable.json"
            cloud.write_bytes((ROOT / "tests/fixtures/finesub/sample-stable.json").read_bytes())
            media = root / "media.mp4"
            media.write_bytes(b"media")
            request = {
                "schema": 1,
                "provider": "local",
                "source": {"kind": "cloud_stable", "path": str(media), "stable_json": str(cloud), "title": "片子"},
                "target": "raw-srt",
                "language": "ja",
                "device": "cpu",
                "correction": {},
                "knowledge": "none",
            }
            environment = dict(os.environ)
            environment["PYTHONPATH"] = os.pathsep.join(
                [str(ROOT / "src"), str(ROOT / "third_party/finesub/src")]
            )
            environment["PYTHONIOENCODING"] = "utf-8"
            environment["FINESUB_HOME"] = str(root / "finesub-home")
            result = subprocess.run(
                [
                    sys.executable, "-c", script,
                    "--task-id", "cont", "--task-dir", str(root / "task"),
                    "--vendor", str(ROOT / "third_party/finesub"),
                ],
                input=json.dumps(request, ensure_ascii=False) + "\n",
                capture_output=True, text=True, encoding="utf-8", env=environment, timeout=120,
            )
            events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            completed = next(event for event in events if event["type"] == "completed")
            self.assertEqual(
                set(completed["payload"]["artifacts"]["artifacts"]),
                {"stable_json", "raw_srt", "metadata_json"},
            )
            reused = {
                event["payload"]["stage"]
                for event in events
                if event["type"] == "stage" and event["payload"].get("reused")
            }
            self.assertTrue({"vocal", "aligned", "stable"} <= reused)

    def test_source_text_axis_skips_recognition_and_only_runs_the_llm_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            task_dir = Path(temp) / "task"
            source = Path(temp) / "video.mp4"
            source.write_bytes(b"media")
            captured: dict = {}

            def run_correction_task(task):
                captured.update(vars(task))
                output = Path(task.output_path)
                output.write_text("1\n00:00:00,000 --> 00:00:01,000\n你好\n", encoding="utf-8")
                output.with_name(f"{output.stem}-annotated.csv").write_text("# header\n", encoding="utf-8")
                return SimpleNamespace(result=output)

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
            correction.run_correction_task = run_correction_task
            correction_request = types.ModuleType("finesub.llm.correction_request")
            correction_request.CorrectionRequest = lambda **kwargs: SimpleNamespace(**kwargs)
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
                "finesub.llm.correction_request", "finesub.llm.routing.profiles", "finesub.stages",
                "finesub.reporting",
            )
            previous = {name: sys.modules.get(name) for name in names}
            sys.modules.update({
                "finesub": finesub,
                "finesub.llm": llm,
                "finesub.llm.routing": routing,
                "finesub.llm.correction_translation": correction,
                "finesub.llm.correction_request": correction_request,
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
            self.assertEqual(captured["task_summary"], "字幕任务：video")
            stable = json.loads(Path(captured["stable_json"]).read_text(encoding="utf-8"))
            self.assertEqual(stable["segments"], [{"id": "1", "start": 0.0, "end": 1.0, "text": "こんにちは"}])
            events = [json.loads(line) for line in output.getvalue().splitlines()]
            manifest = next(event for event in events if event["type"] == "completed")["payload"]["artifacts"]
            self.assertEqual(set(manifest["artifacts"]), {"stable_json", "annotated_csv", "final_srt"})

    def test_diarization_request_maps_to_pipeline_keys(self) -> None:
        self.assertEqual(worker.diarization_kwargs({}), {})
        self.assertEqual(worker.diarization_kwargs({"diarization": {"level": "off"}}), {})
        self.assertEqual(
            worker.diarization_kwargs({"diarization": {"level": "acoustics", "max_speakers": None}}),
            {"diarize": "acoustics"},
        )
        self.assertEqual(
            worker.diarization_kwargs({"diarization": {"level": "judge-text", "max_speakers": 3}}),
            {"diarize": "judge-text", "diarize_max_speakers": 3},
        )

    def test_diarization_evidence_path_matches_the_engine(self) -> None:
        from finesub.speech.diarization.evidence import evidence_path

        timeline = Path("/runs/节目-diar.json")
        self.assertEqual(worker.diarization_evidence_path(timeline), evidence_path(timeline))

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
                        "correction-mm": "local-agy-sonnet-5_5",
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
