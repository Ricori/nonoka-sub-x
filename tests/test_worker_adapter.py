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
from unittest import mock
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from urllib.request import url2pathname

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nonoka_x import engine_host, worker  # noqa: E402
from nonoka_x import correction as correction_layer  # noqa: E402
from nonoka_x import transcription as transcription_layer  # noqa: E402
from nonoka_x.correction import CorrectionResult, CorrectionSpec  # noqa: E402
from nonoka_x.transcription import (  # noqa: E402
    EngineSpec,
    Readiness,
    TranscriptDocument,
    TranscriptLine,
    TranscriptionResult,
    write_transcript,
)


def file_of(uri: str) -> Path:
    return Path(url2pathname(urlparse(uri).path))


ENGINES = ROOT / "engines"


def for_task_mode(request: dict, record_dir: Path) -> dict:
    """What the correction package answers the worker's `task_mode` with (in this process).

    Imported here, not at the top: the orchestration children import this
    module for `TranscriptOnlyEngine` with only the core on their path.
    """

    from finesub_correct import engine as correction_package

    return correction_package.for_task_mode(request, record_dir)


def prepare_knowledge_subjects(request: dict, task_id: str, *, knowledge_root: Path) -> dict:
    """What the knowledge package answers the worker's `prepare_subjects` with (in this process)."""

    from finesub_knowledge.manage import prepare_subjects

    asked = worker.subject_request(request, task_id)
    if asked is None:
        return {"names": [], "created": []}
    return prepare_subjects(**{**asked, "knowledge_root": knowledge_root})


def engines_without(root: Path, *removed: str) -> Path:
    """An engines root with every package but `removed`, each one the real package (a directory junction)."""

    import _winapi

    engines = root / "engines"
    engines.mkdir(parents=True, exist_ok=True)
    for package in ENGINES.iterdir():
        if package.is_dir() and package.name not in removed and not (engines / package.name).exists():
            _winapi.CreateJunction(str(package), str(engines / package.name))
    return engines


# --- stand-in engines ------------------------------------------------------------


class RecordingEngine:
    """A transcription engine that hands every job to `transcribe`."""

    def __init__(self, engine_id: str, transcribe) -> None:  # noqa: ANN001
        self.spec = EngineSpec(id=engine_id, label=engine_id, kind="cloud-api")
        self._transcribe = transcribe

    def readiness(self, context) -> Readiness:  # noqa: ANN001
        return Readiness()

    def transcribe(self, job, report):  # noqa: ANN001, ANN201
        return self._transcribe(job, report)


class RecordingCorrection:
    """A correction engine that hands every job to `run`."""

    spec = CorrectionSpec(id="finesub", label="recording correction")

    def __init__(self, run) -> None:  # noqa: ANN001
        self._run = run

    def readiness(self, context, request) -> Readiness:  # noqa: ANN001
        return Readiness()

    def run(self, job, report):  # noqa: ANN001, ANN201
        return self._run(job, report)


class TranscriptOnlyEngine:
    """An engine that hands over `transcript.json` and nothing else: no native attachment at all.

    Imported by the worker from `worker.TRANSCRIPTION_ENGINES` in the children
    below, in place of FineSub. Each call leaves `transcribed.txt` behind, so a
    test can tell whether it ran.
    """

    spec = EngineSpec(id="transcript-only", label="Transcript only", kind="cloud-api")

    def readiness(self, context) -> Readiness:  # noqa: ANN001
        return Readiness()

    def transcribe(self, job, report):  # noqa: ANN001, ANN201
        report.stage("stable", "正在处理")
        with (Path(job.task_dir) / "transcribed.txt").open("a", encoding="utf-8") as marker:
            marker.write(f"{job.target}\n")
        document = TranscriptDocument(
            engine=self.spec.id,
            model="stand-in",
            language=job.language,
            duration=6.0,
            lines=(
                TranscriptLine(id="1", start=0.5, end=2.0, text="おはようございます", speaker="S1"),
                TranscriptLine(id="2", start=2.4, end=5.5, text="今日は雨ですね", speaker="S2"),
            ),
        )
        target = Path(job.task_dir) / "transcript.json"
        write_transcript(document, target)
        return TranscriptionResult(transcript_json=target)


@contextlib.contextmanager
def registered(*engines):  # noqa: ANN001, ANN201
    """Only these engines in the two registries, and none of the built-in ones."""

    saved = {
        registry: list(registry)
        for registry in (transcription_layer.registry, correction_layer.registry)
    }
    for registry in saved:
        registry.clear()
    for engine in engines:
        layer = correction_layer.registry if isinstance(engine.spec, CorrectionSpec) else transcription_layer.registry
        layer.register(engine)
    try:
        with mock.patch.object(worker, "TRANSCRIPTION_ENGINES", ()), \
                mock.patch.object(worker, "CORRECTION_ENGINES", ()):
            yield
    finally:
        for registry, previous in saved.items():
            registry.clear()
            for engine in previous:
                registry.register(engine)


def run_worker(request: dict, task_dir: Path) -> tuple[int, list[dict]]:
    """`worker.main` in this process on `request`; its exit code and events."""

    output = io.StringIO()
    old_stdin = sys.stdin
    try:
        sys.stdin = io.StringIO(json.dumps(request, ensure_ascii=False) + "\n")
        with contextlib.redirect_stdout(output):
            result = worker.main([
                "--task-id", "abc123", "--task-dir", str(task_dir),
                "--engines", str(ENGINES),
            ])
    finally:
        sys.stdin = old_stdin
    return result, [json.loads(line) for line in output.getvalue().splitlines() if line.startswith("{")]


#: One child of the call-equivalence harness, with its recording stand-ins at
#: FineSub's algorithm entry points in the engine packages' processes, that
#: runs `worker.main` on the request as often as asked and keeps every event:
#: `tests.quality_harness.calls`'s child, plus the events, plus a choice of
#: the core's own transcription engines and of the installed packages.
_ORCHESTRATION_CHILD = r"""
import contextlib, io, json, sys
from pathlib import Path

spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
root = Path(spec["root"])
from tests.quality_harness import calls, stubs
from tests.quality_harness.package_child import read_shared, use_in_worker

sys.meta_path.insert(0, stubs.RefuseHeavyImports())
settings = calls.bind_settings(root / "data", "compat")
calls.adopt_worker_environment(root / "data", settings)
record = root / "calls.jsonl"
use_in_worker({"hook": "tests.quality_harness.calls:package_hook", "record": str(record)}, root / "package-child.json")
from nonoka_x.local_provider import validate_request

request = validate_request(spec["request"])
request.update(spec.get("after_validation") or {})
from nonoka_x import worker

if "transcription_engines" in spec:
    worker.TRANSCRIPTION_ENGINES = tuple(spec["transcription_engines"])
runs = []
for _ in range(spec.get("runs", 1)):
    stdout = io.StringIO()
    sys.stdin = io.StringIO(json.dumps(request, ensure_ascii=False) + "\n")
    with contextlib.redirect_stdout(stdout):
        code = worker.main(["--task-id", "orchestration", "--task-dir", str(root / "data" / "tasks" / "orchestration"),
                            "--engines", spec["engines"], "--data-dir", str(root / "data")])
    events = [json.loads(line) for line in stdout.getvalue().splitlines() if line.startswith("{")]
    runs.append({"exit": code, "events": events})
records = read_shared(record)
Path(spec["out"]).write_text(
    json.dumps({"runs": runs, "calls": [call["entry"] for call in records], "records": records}, ensure_ascii=False),
    encoding="utf-8",
)
"""


def run_orchestration_child(root: Path, request: dict, *, without: tuple[str, ...] = (), **spec) -> dict:  # noqa: ANN003
    """`_ORCHESTRATION_CHILD` on `request` under `root` (the media and cloud files made here).

    `without` names the engine packages that are not installed for the run.
    """

    from tests.quality_harness import calls

    spec["engines"] = str(engines_without(root, *without) if without else ENGINES)

    for directory in ("media", "cloud", "data", "tmp"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    media = root / "media" / "节目.m4a"
    media.write_bytes(b"orchestration audio placeholder")
    (root / "cloud" / "cloud.stable.json").write_bytes(calls.STABLE_FIXTURE.read_bytes())
    request = json.loads(json.dumps(request).replace("$MEDIA", media.as_posix()).replace(
        "$CLOUD_STABLE", (root / "cloud" / "cloud.stable.json").as_posix()))
    spec_path = root / "spec.json"
    out = root / "record.json"
    spec_path.write_text(
        json.dumps({**spec, "request": request, "root": str(root), "out": str(out)}, ensure_ascii=False),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, "-c", _ORCHESTRATION_CHILD, str(spec_path)],
        cwd=ROOT, env=calls.hermetic_environment(root),
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    if completed.returncode != 0 or not out.is_file():
        raise AssertionError(f"orchestration child failed ({completed.returncode}):\n{completed.stderr[-4000:]}")
    return json.loads(out.read_text(encoding="utf-8"))


def reused_stages(events: list[dict]) -> list[str]:
    return [
        event["payload"]["stage"] for event in events
        if event["type"] == "stage" and event["payload"].get("message") == "已有结果，跳过"
    ]


def stage_names(events: list[dict]) -> list[str]:
    return [event["payload"]["stage"] for event in events if event["type"] == "stage"]


#: FineSub's recognition entry points (`tests.quality_harness.stubs`).
RECOGNITION_ENTRIES = frozenset({
    "separation.run_vocal_separation", "separation.encode_asr_delivery", "vad_asr_stage.run_vad_asr",
    "stabilization.run_asr_stabilize", "diarization.run_diarization",
})

BASE = {
    "schema": 1,
    "provider": "local",
    "source": {"kind": "local_file", "path": "$MEDIA", "title": "节目"},
    "language": "ja",
    "knowledge": "none",
}


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
                result = prepare_knowledge_subjects(first, "task-1", knowledge_root=root)
                self.assertEqual(result["created"], ["柊優花", "星之海"])
                self.assertEqual(result["names"], ["streamer/柊優花", "common/星之海"])
                repo = KnowledgeRepo.open(root)
                resolved = repo.resolve("柊优花")
                assert resolved is not None
                self.assertEqual(resolved.key, "柊優花")
                self.assertIn("以游戏直播为主的主播", repo.index_text("streamer"))

                # A repeat creates nothing, and an existing subject travels by id.
                again = prepare_knowledge_subjects(first, "task-2", knowledge_root=root)
                self.assertEqual(again["created"], [])
                self.assertEqual(again["rev"], result["rev"])
                ident = next(s.local_id for s in repo.subjects() if s.payload["surface"] == "星之海")
                read = prepare_knowledge_subjects(
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
                    prepare_knowledge_subjects(
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
                    prepare_knowledge_subjects(
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

    def zh_request(self, **extra) -> dict:
        return {
            "language": "zh",
            "knowledge": "update",
            "knowledge_subjects": [{"id": "s1", "kind": "streamer", "subject": "某主播"}],
            "correction": {"extra_info": "背景", "extra_style": "简洁"},
            **extra,
        }

    def test_a_new_chinese_task_drops_knowledge_and_style(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            request = for_task_mode(self.zh_request(), Path(temp) / "llm-artifacts")

        self.assertEqual(request["knowledge"], "none")
        self.assertEqual(request["correction"], {"extra_info": "背景", "extra_style": ""})
        self.assertEqual(worker.knowledge_subjects(request), [])
        self.assertEqual(worker.compose_knowledge_context(request, "背景"), "背景")

    def test_a_resumed_chinese_translation_keeps_its_settings(self) -> None:
        records = {
            "recorded before the field": '{"version": 1, "mode": "single", "proofread": true}',
            "recorded as translating": '{"version": 1, "mode": "single", "proofread": true, "correct_only": false}',
        }
        for reason, record in records.items():
            with self.subTest(reason), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                (directory / "correction-mode.json").write_text(record, encoding="utf-8")
                self.assertEqual(for_task_mode(self.zh_request(), directory), self.zh_request())
        with self.subTest("windows but no record"), tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "correction-windows.jsonl").write_text("", encoding="utf-8")
            self.assertEqual(for_task_mode(self.zh_request(), directory), self.zh_request())

    def test_a_resumed_correct_only_task_stays_correct_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "correction-mode.json").write_text(
                '{"version": 1, "mode": "single", "proofread": false, "correct_only": true}', encoding="utf-8"
            )
            (directory / "correction-windows.jsonl").write_text("", encoding="utf-8")
            self.assertEqual(for_task_mode(self.zh_request(), directory)["knowledge"], "none")

    def test_only_a_declared_chinese_source_without_a_japanese_axis_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            for request in (
                {**self.zh_request(), "language": "ja"},
                {**self.zh_request(), "language": "auto"},
                self.zh_request(axis={"kind": "ja", "rows": []}),
            ):
                self.assertEqual(for_task_mode(request, directory), request)

    def test_task_request_maps_to_the_engine_jobs_and_returns_hashed_manifest(self) -> None:
        """The request reaches each half as its job, and the manifest lists what they left.

        Recording stand-ins in both registries: the worker builds the jobs and
        never looks inside them, so the fields are the whole contract here.
        """

        with tempfile.TemporaryDirectory() as temp:
            task_dir = Path(temp) / "task"
            source = Path(temp) / "video.mp4"
            source.write_bytes(b"media")
            captured: dict = {}

            def transcribe(job, report):
                captured["transcription"] = job
                transcript = task_dir / "transcript.json"
                transcript.write_text("{}", encoding="utf-8")
                stable = task_dir / "workspace" / "video-stable.json"
                stable.parent.mkdir(parents=True, exist_ok=True)
                stable.write_text('{"segments": []}\n', encoding="utf-8")
                vocal = stable.with_name("video-vocal.ogg")
                vocal.write_bytes(b"OggS")
                return TranscriptionResult(
                    transcript_json=transcript,
                    native={"recorder.stable_json": stable, "recorder.vocal_audio": vocal,
                            "recorder.vad_json": stable.with_name("video-vad.json")},
                )

            def correct(job, report):
                captured["correction"] = job
                raw = task_dir / "workspace" / "video-raw.srt"
                raw.write_text("1\n00:00:00,000 --> 00:00:01,000\nx\n", encoding="utf-8")
                return CorrectionResult(artifacts={"raw_srt": raw, "final_srt": task_dir / "workspace" / "video.srt"})

            request = {
                "schema": 1,
                "provider": "local",
                "source": {"kind": "local_file", "path": str(source), "title": "video"},
                "transcription": {"engine": "recorder"},
                "target": "raw-srt",
                "language": "ja",
                "device": "cuda:0",
                "gpu_budget_gb": 8,
                "diarization": {"level": "acoustics", "max_speakers": 3},
                "correction": {"media": "audio", "retrieval": "local", "difficulty": "quality",
                               "extra_info": "背景"},
                "knowledge": "none",
            }
            with registered(RecordingEngine("recorder", transcribe), RecordingCorrection(correct)):
                result, events = run_worker(request, task_dir)

            self.assertEqual(result, 0, events)
            job = captured["transcription"]
            self.assertEqual(job.source, source)
            self.assertEqual(job.task_dir, task_dir)
            # One cache per task, inside the task's own directory: the cloud
            # engines key their answers by the audio and clear the directory
            # for another one, so two tasks must never share it (WP3).
            self.assertEqual(job.cache_dir, task_dir / "cache")
            self.assertEqual((job.target, job.language), ("raw-srt", "ja"))
            # In TaskRequest terms, resolved: the device as requested, the
            # legacy budget migrated to a tier (upstream 0.5.0 retired
            # `--gpu-budget-gb`), the speaker block as the provider gave it.
            self.assertEqual(job.options["device"], "cuda:0")
            self.assertEqual(job.options["gpu_tier"], "standard")
            self.assertNotIn("gpu_budget_gb", job.options)
            self.assertEqual(dict(job.options["diarization"]), {"level": "acoustics", "max_speakers": 3})
            self.assertEqual((job.options["title"], job.options["task_id"]), ("video", "abc123"))
            self.assertEqual((job.options["extra_info"], job.options["knowledge"]), ("背景", "none"))
            self.assertTrue(job.options["separate"])

            correction = captured["correction"]
            self.assertEqual(correction.transcription.transcript_json, task_dir / "transcript.json")
            self.assertEqual((correction.target, correction.title, correction.task_id), ("raw-srt", "video", "abc123"))
            # Whether to proofread is the correction engine's to resolve (its own route table).
            self.assertNotIn("proofread", correction.options)
            self.assertEqual(correction.options["media"], "audio")
            self.assertEqual(correction.task_summary, "字幕任务：video")
            self.assertEqual(correction.diarization_level, "acoustics")
            self.assertEqual(dict(correction.knowledge), {"knowledge": "none", "knowledge_subjects": []})

            completed = next(event for event in events if event["type"] == "completed")
            manifest = completed["payload"]["artifacts"]
            self.assertEqual(
                manifest["engine_commit"],
                engine_host.discover(ENGINES)["finesub-correct"].engine_commit,
            )
            # An attachment `<engine>.<name>` is listed as `<name>`; only files that exist.
            self.assertEqual(set(manifest["artifacts"]), {"vocal_audio", "stable_json", "raw_srt"})
            self.assertEqual(len(manifest["artifacts"]["stable_json"]["sha256"]), 64)
            self.assertEqual(events[-2]["payload"].get("message"), "字幕已完成")

    def test_a_cloud_stable_is_handed_to_the_correction_as_it_is(self) -> None:
        """A continuation is a transcription in hand: the cloud's files as attachments, no engine run.

        Where they go and that a retry keeps the first copy is the
        correction's business (`place_native`, tests/test_finesub_correction.py).
        """

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task_dir = root / "task"
            cloud = root / "cloud.stable.json"
            cloud.write_text('{"segments": []}', encoding="utf-8")
            timeline = root / "cloud.diar.json"
            timeline.write_text("{}", encoding="utf-8")
            media = root / "media.m4a"
            captured: dict = {}

            def correct(job, report):  # noqa: ANN001, ANN202
                captured["job"] = job
                return CorrectionResult()

            for source, native in (
                ({"stable_json": str(cloud)}, {"finesub.stable_json": cloud}),
                ({"stable_json": str(cloud), "diar_json": str(timeline)},
                 {"finesub.stable_json": cloud, "finesub.diar_json": timeline}),
            ):
                request = {**BASE, "target": "raw-srt",
                           "source": {"kind": "cloud_stable", "path": str(media), "title": "片子", **source}}
                with registered(RecordingCorrection(correct)):
                    code, events = run_worker(request, task_dir)
                self.assertEqual(code, 0, events)
                transcription = captured["job"].transcription
                self.assertEqual(dict(transcription.native), native)
                self.assertEqual(transcription.transcript_json, task_dir / "transcript.json")
                self.assertFalse(transcription.transcript_json.exists())
                self.assertIn("从云端识别结果续跑：跳过人声分离与识别",
                              [event["payload"].get("message") for event in events if event["type"] == "log"])
                self.assertEqual(reused_stages(events), ["vocal", "aligned", "stable"])

    def test_a_continuation_runs_without_any_module_of_the_asr_stack(self) -> None:
        """The reason the launcher can continue a cloud task at all.

        Run in a child whose import system refuses torch, numpy and the rest:
        a module-level import anywhere between `nonoka_x.worker` and the raw
        SRT -- vendor patch 0006 is what removed the last ones -- fails this
        rather than the first user without the managed runtime.
        """

        with tempfile.TemporaryDirectory() as temp:
            events = self.continue_without_the_asr_stack(Path(temp), {}, {})
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

    def test_a_cloud_speaker_timeline_labels_the_continuation_without_the_asr_stack(self) -> None:
        """The diarize stage labels from the cloud's timeline instead of recomputing it.

        Since engine 0.7.0 the labels live in `-diar-rows.json`, which the
        desktop writes from the delivered timeline. That takes no model and no
        numpy, so a diarized cloud task continues on the launcher.
        """

        from finesub.speech.diarization.segments import TrackSegment
        from finesub.speech.diarization.stage import DiarizationResult, write_timeline

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            timeline = write_timeline(
                DiarizationResult(
                    tracks=(TrackSegment(0, 0.0, 30.0), TrackSegment(1, 30.0, 600.0)),
                    frame_seconds=0.08, overlap=0.0,
                ),
                root / "cloud.diar.json",
            )
            stable = json.loads((ROOT / "tests/fixtures/finesub/sample-stable.json").read_text(encoding="utf-8"))
            stable.setdefault("metadata", {})["speaker"] = json.loads(timeline.read_text(encoding="utf-8"))["metadata"]
            events = self.continue_without_the_asr_stack(
                root, {"diar_json": str(timeline)}, {"diarization": {"level": "acoustics", "max_speakers": None}},
                stable=stable,
            )
            completed = next(event for event in events if event["type"] == "completed")
            artifacts = completed["payload"]["artifacts"]["artifacts"]
            self.assertIn("diar_json", artifacts)
            self.assertEqual(file_of(artifacts["diar_json"]["uri"]).name, "片子-diar.json")
            rows_file = file_of(artifacts["diar_rows_json"]["uri"])
            self.assertEqual(rows_file.name, "片子-diar-rows.json")
            rows = json.loads(rows_file.read_text(encoding="utf-8"))["rows"]
            self.assertTrue(any(row.get("speaker") for row in rows))

    def continue_without_the_asr_stack(
        self, root: Path, source: dict, request: dict, *, stable: dict | None = None
    ) -> list[dict]:
        # In every process of the run -- the worker's and the correction package's --
        # the import system refuses the ASR stack (a `sitecustomize` on the path).
        hook = root / "refuse"
        hook.mkdir()
        (hook / "sitecustomize.py").write_text("""
import importlib.abc, sys
BLOCKED = {"torch", "torchaudio", "numpy", "soundfile", "faster_whisper", "ctranslate2", "transformers", "audio_separator"}
class Refuse(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError("blocked for this test: " + name)
        return None
sys.meta_path.insert(0, Refuse())
""", encoding="utf-8")
        script = """
import sys
from nonoka_x import worker
raise SystemExit(worker.main(sys.argv[1:]))
"""
        cloud = root / "cloud.stable.json"
        if stable is None:
            cloud.write_bytes((ROOT / "tests/fixtures/finesub/sample-stable.json").read_bytes())
        else:
            cloud.write_text(json.dumps(stable, ensure_ascii=False), encoding="utf-8")
        media = root / "media.mp4"
        media.write_bytes(b"media")
        request = {
            "schema": 1,
            "provider": "local",
            "source": {"kind": "cloud_stable", "path": str(media), "stable_json": str(cloud), "title": "片子", **source},
            "target": "raw-srt",
            "language": "ja",
            "device": "cpu",
            "correction": {},
            "knowledge": "none",
            **request,
        }
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join([str(hook), str(ROOT / "src")])
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["FINESUB_HOME"] = str(root / "finesub-home")
        result = subprocess.run(
            [
                sys.executable, "-c", script,
                "--task-id", "cont", "--task-dir", str(root / "task"),
                "--engines", str(ENGINES),
                # Where the packages keep their own data (`paths.package_data`), as the sidecar always says.
                "--data-dir", str(root / "data"),
            ],
            input=json.dumps(request, ensure_ascii=False) + "\n",
            capture_output=True, text=True, encoding="utf-8", env=environment, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        return [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]

    def test_source_text_axis_skips_recognition_and_only_runs_the_llm_stage(self) -> None:
        """A Japanese axis goes straight to the correction package's axis run: no recognition at all."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            record = run_orchestration_child(root, {
                **BASE,
                "target": "final-srt",
                "device": "cuda",
                "gpu_budget_gb": 8,
                "correction": {"media": "text", "retrieval": "none", "difficulty": "quality"},
                "axis": {
                    "kind": "ja",
                    "filename": "轴.ass",
                    "rows": [
                        {"t0": 0.0, "t1": 1.0, "ja": "こんにちは", "zh": "", "spk": ""},
                        {"t0": 1.0, "t1": 2.0, "ja": "", "zh": "", "spk": ""},
                    ],
                },
            })
            (run,) = record["runs"]
            self.assertEqual(run["exit"], 0, run["events"])
            self.assertFalse(RECOGNITION_ENTRIES & set(record["calls"]), record["calls"])
            (task,) = [call["arguments"] for call in record["records"]
                       if call["entry"] == "correction_translation.run_correction_task"]
            request = next(iter(task.values()))
            # The axis replaces recognition outright: no audio is decoded when
            # the correction reference is text, and the blank row is not handed
            # to the model as something to correct.
            self.assertIsNone(request["audio_path"])
            self.assertEqual(request["task_summary"], "字幕任务：节目")
            stable_path = Path(request["stable_json"]["path"])
            self.assertEqual(stable_path.parent.name, "finesub-correct")
            stable = json.loads(stable_path.read_text(encoding="utf-8"))
            self.assertEqual(stable["segments"], [{"id": "1", "start": 0.0, "end": 1.0, "text": "こんにちは"}])
            manifest = next(event for event in run["events"] if event["type"] == "completed")["payload"]["artifacts"]
            self.assertEqual(set(manifest["artifacts"]), {"stable_json", "annotated_csv", "final_srt"})

    def test_diarization_request_maps_to_pipeline_keys(self) -> None:
        self.assertEqual(worker.diarization_kwargs({}), {"diarize": "off"})
        self.assertEqual(worker.diarization_kwargs({"diarization": {"level": "off"}}), {"diarize": "off"})
        self.assertEqual(
            worker.diarization_kwargs({"diarization": {"level": "acoustics", "max_speakers": None}}),
            {"diarize": "acoustics"},
        )
        self.assertEqual(
            worker.diarization_kwargs({"diarization": {"level": "judge-text", "max_speakers": 3}}),
            {"diarize": "judge-text", "diarize_max_speakers": 3},
        )

    def test_llm_model_desktop_route_expands_to_upstream_task_groups(self) -> None:
        from finesub.llm.routing.model_routes import (
            default_model_routes,
            install_runtime_preferred,
        )

        from finesub_correct.bridge import install_llm_model_override

        from nonoka_x.settings import TASK_GROUPS_BY_ROUTE

        try:
            install_llm_model_override({"correction": "local-agy-gemini-3_8-flash"}, TASK_GROUPS_BY_ROUTE)
            # Through the real route validator, not only the adapter's map.
            routes = default_model_routes()
            for group_id in ("correction-mm", "correction-text"):
                group, _cell = routes.resolve_binding(routes.active_preset_id, group_id, "quality")
                self.assertEqual(group.target_ids, ("local-agy-gemini-3_8-flash",))
        finally:
            install_runtime_preferred({})

    def test_llm_model_rejects_conflicting_friendly_and_exact_routes(self) -> None:
        from finesub_correct.bridge import install_llm_model_override

        from nonoka_x.settings import TASK_GROUPS_BY_ROUTE

        with self.assertRaisesRegex(ValueError, "conflicting targets"):
            install_llm_model_override(
                {"correction": "local-agy-media-gemini-3_7-flash", "correction-mm": "local-agy-sonnet-5_5"},
                TASK_GROUPS_BY_ROUTE,
            )


    def test_separator_onnx_cuda_warning_is_filtered(self) -> None:
        """In the transcription package's process, where the separator runs."""

        import logging

        from finesub_transcribe.engine import mute_separator_onnx_warning

        mute_separator_onnx_warning()
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


class OrchestrationTests(unittest.TestCase):
    """The worker as WP4c leaves it: engines by id, nothing FineSub-shaped in between.

    The children run `worker.main` behind the call-equivalence harness's
    recording stand-ins (no torch, no model, no LLM), so FineSub's real
    orchestration and finesub correction run end to end.
    """

    maxDiff = None
    TRANSCRIPT_ONLY = "tests.test_worker_adapter:TranscriptOnlyEngine"

    def test_a_transcript_only_engine_replaces_finesub_up_to_final_srt(self) -> None:
        """Replaceability: another engine, FineSub's transcription not registered, finesub correction after it."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            record = run_orchestration_child(
                root,
                {**BASE, "target": "final-srt", "correction": {"media": "text", "retrieval": "none"}},
                after_validation={"transcription": {"engine": "transcript-only"}},
                transcription_engines=[self.TRANSCRIPT_ONLY],
                without=("finesub-transcribe",),
            )
            (run,) = record["runs"]
            self.assertEqual(run["exit"], 0, run["events"])
            task_dir = root / "data" / "tasks" / "orchestration"
            self.assertEqual((task_dir / "transcribed.txt").read_text(encoding="utf-8"), "final-srt\n")
            # Nothing of FineSub's recognition ran; its correction did.
            self.assertFalse(RECOGNITION_ENTRIES & set(record["calls"]), record["calls"])
            self.assertIn("correction_translation.run_correction_task", record["calls"])
            messages = [event["payload"].get("message") for event in run["events"] if event["type"] == "log"]
            self.assertIn("纠错输入：由 transcript.json 转成的 stable，说话人来自各行标签", messages)
            artifacts = next(e for e in run["events"] if e["type"] == "completed")["payload"]["artifacts"]["artifacts"]
            self.assertTrue({"stable_json", "diar_rows_json", "raw_srt", "final_srt", "annotated_csv"}
                            <= set(artifacts), sorted(artifacts))
            rows = json.loads(file_of(artifacts["diar_rows_json"]["uri"]).read_text(encoding="utf-8"))["rows"]
            self.assertEqual([row["speaker"]["joint_speaker"] for row in rows][:2], ["S1", "S2"])
            self.assertEqual(len(stage_names(run["events"])), len(set(stage_names(run["events"]))))

    def test_the_engine_stops_at_its_own_target(self) -> None:
        """A target up to `stable` runs the transcription only; no correction engine is asked for."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task_dir = root / "task"

            def transcribe(job, report):  # noqa: ANN001, ANN202
                return TranscriptOnlyEngine().transcribe(job, report)

            with registered(RecordingEngine("transcript-only", transcribe)):
                media = root / "a.m4a"
                media.write_bytes(b"x")
                request = {**BASE, "source": {"kind": "local_file", "path": str(media), "title": "a"},
                           "transcription": {"engine": "transcript-only"}, "target": "stable"}
                code, events = run_worker(request, task_dir)
            self.assertEqual(code, 0, events)
            self.assertEqual((task_dir / "transcribed.txt").read_text(encoding="utf-8"), "stable\n")
            self.assertEqual(events[-1]["type"], "completed")
            self.assertEqual(events[-2]["payload"].get("message"), "字幕已完成")

    def test_an_engine_that_is_not_registered_fails_the_task_before_anything_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            record = run_orchestration_child(
                root, {**BASE, "target": "raw-srt"}, transcription_engines=[self.TRANSCRIPT_ONLY],
                without=("finesub-transcribe",),
            )
            (run,) = record["runs"]
            self.assertEqual(run["exit"], 1)
            failed = [event["payload"]["message"] for event in run["events"] if event["type"] == "failed"]
            self.assertEqual(failed, [
                "UnknownEngine: no transcription engine 'finesub' is registered (registered: transcript-only)"
            ])
            self.assertFalse(RECOGNITION_ENTRIES & set(record["calls"]))

    def test_a_cloud_continuation_needs_no_transcription_engine(self) -> None:
        """The cloud's recognition is a transcription in hand: with no transcription engine at all it continues."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            record = run_orchestration_child(
                root,
                {**BASE, "target": "raw-srt", "source": {"kind": "cloud_stable", "path": "$MEDIA",
                                                         "title": "节目", "stable_json": "$CLOUD_STABLE"}},
                transcription_engines=[],
                without=("finesub-transcribe",),
            )
            (run,) = record["runs"]
            self.assertEqual(run["exit"], 0, run["events"])
            self.assertFalse(RECOGNITION_ENTRIES & set(record["calls"]))
            self.assertEqual(reused_stages(run["events"])[:3], ["vocal", "aligned", "stable"])
            artifacts = next(e for e in run["events"] if e["type"] == "completed")["payload"]["artifacts"]["artifacts"]
            self.assertEqual(file_of(artifacts["stable_json"]["uri"]).name, "节目-stable.json")
            self.assertIn("raw_srt", artifacts)

    def test_the_default_engine_announces_each_stage_once(self) -> None:
        """No「已有结果，跳过」twice in one task log: on the first run, and on a retry where everything is reused."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            record = run_orchestration_child(
                root,
                {**BASE, "target": "final-srt", "diarization": {"level": "acoustics"},
                 "correction": {"media": "text", "retrieval": "none"}},
                runs=2,
            )
            first, retry = record["runs"]
            for run in (first, retry):
                self.assertEqual(run["exit"], 0, run["events"])
                stages = stage_names(run["events"])
                self.assertEqual(len(stages), len(set(stages)), stages)
                done = [e for e in run["events"] if e["type"] == "progress" and e["payload"].get("message") == "字幕已完成"]
                self.assertEqual(len(done), 1)
            self.assertEqual(reused_stages(first["events"]), [])
            self.assertEqual(
                reused_stages(retry["events"]),
                ["vocal", "aligned", "stable", "diarize", "raw-srt", "translated-srt", "final-srt"],
            )

    def test_a_request_the_correction_refuses_fails_before_the_transcription(self) -> None:
        """What `run_pipeline` refused before separation is still refused before the transcription runs.

        A speaker judge at the `efficiency` difficulty, which the correction
        cannot split for -- the provider would not let it through, so it is
        put in after validation.
        """

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            record = run_orchestration_child(
                root,
                {**BASE, "target": "final-srt", "correction": {"media": "text", "difficulty": "efficiency"}},
                after_validation={"transcription": {"engine": "transcript-only"},
                                  "diarization": {"level": "judge-text", "max_speakers": None}},
                transcription_engines=[self.TRANSCRIPT_ONLY],
            )
            (run,) = record["runs"]
            self.assertEqual(run["exit"], 1, run["events"])
            self.assertTrue([e for e in run["events"] if e["type"] == "failed"])
            self.assertFalse((root / "data" / "tasks" / "orchestration" / "transcribed.txt").exists())
            self.assertNotIn("correction_translation.run_correction_task", record["calls"])

    def test_the_worker_names_no_engine(self) -> None:
        """No branch on an engine's id, and FineSub's stages reached only through the engines."""

        import ast

        tree = ast.parse((ROOT / "src/nonoka_x/worker.py").read_text(encoding="utf-8"))
        imported = {
            node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
        } | {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        self.assertNotIn("finesub.stages", imported)
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                literals = [n.value for n in ast.walk(node) if isinstance(n, ast.Constant)]
                self.assertFalse({"finesub", "finesub-cloud"} & set(literals), ast.unparse(node))


if __name__ == "__main__":
    unittest.main()
