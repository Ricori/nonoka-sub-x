"""FineSub as a transcription engine: the transcription package's adapter (`finesub_transcribe.engine`).

Moved from the core (`nonoka_x.transcription.engines.finesub`, WP4a) into the
transcription package (WP-P1). That the algorithms receive the baseline
arguments through it is the quality gate's call equivalence, which runs the
worker and, since WP-P1, the package's own process
(`tests/test_finesub_call_equivalence.py`); here are the adapter's own
pieces, run in this process: the job -> `run_transcription` keywords, the
products, `transcript.json`, and the core's reading of what the package
declares (its spec, its readiness).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from finesub_transcribe import engine as engine_module  # noqa: E402
from finesub_transcribe.engine import (  # noqa: E402
    TranscriptError,
    engine_device,
    engine_stage,
    project_transcript,
    run_plan,
)

from nonoka_x import engine_host  # noqa: E402
from nonoka_x.transcription import (  # noqa: E402
    TranscriptDocument,
    TranscriptionResult,
    read_transcript,
)

ENGINES = ROOT / "engines"
PACKAGE = engine_host.discover(ENGINES)["finesub-transcribe"]
SPEC = PACKAGE.engine_spec()
STABLE_FIXTURE = ROOT / "tests" / "fixtures" / "finesub" / "sample-stable.json"
NATIVE = Path("native") / "finesub-transcribe"

#: `run_pipeline` keywords the worker passes that only the stages after the
#: stable read (the correction request, the speaker exports); the engine
#: leaves them to the correction.
CORRECTION_ONLY = frozenset({
    "llm_media", "llm_retrieval", "llm_difficulty", "llm_proofread", "llm_fast",
    "extra_style", "task_summary", "speaker_tracks", "knowledge_subjects",
})


class RecordingReporter:
    """FineSub's reporter protocol, recorded."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    def __getattr__(self, method: str) -> Any:
        if method.startswith("_"):
            raise AttributeError(method)

        def record(*args: Any, **kwargs: Any) -> None:
            self.events.append((method, args))

        return record


class SpecTests(unittest.TestCase):
    """What the core learns of the engine without starting it: `PACKAGE.json`."""

    def test_spec(self) -> None:
        self.assertEqual(SPEC.id, "finesub")
        self.assertEqual(SPEC.kind, "local-gpu")
        self.assertEqual(SPEC.provider, "local")
        self.assertEqual(SPEC.keys, ())
        self.assertEqual(SPEC.features.diarization_levels, ("off", "acoustics", "judge-text", "judge-audio"))
        self.assertTrue(SPEC.features.speakers and SPEC.features.word_timestamps and SPEC.features.separate)
        self.assertEqual(SPEC.environment.components, ("asr-runtime", "media"))
        self.assertEqual(SPEC.environment.models, ("separator", "whisper", "qwen-referee"))
        self.assertEqual(SPEC.environment.optional_models, ("nemotron3", "speaker-embedding"))
        self.assertEqual(type(SPEC).from_dict(json.loads(json.dumps(SPEC.to_dict()))), SPEC)

    def test_the_declared_ids_are_the_runtime_facades(self) -> None:
        from nonoka_x import runtime
        from nonoka_x.model_install import DIARIZATION_MODEL_IDS, PIPELINE_MODEL_IDS

        self.assertEqual(SPEC.environment.components, (runtime.ASR_RUNTIME, runtime.MEDIA))
        self.assertEqual(SPEC.environment.models, PIPELINE_MODEL_IDS)
        self.assertEqual(SPEC.environment.optional_models, DIARIZATION_MODEL_IDS)

    def test_the_gpu_tiers_are_the_engines(self) -> None:
        from finesub.speech.runtime.resources import gpu_tier_cli_choices

        self.assertEqual(PACKAGE.gpu_tiers, tuple(gpu_tier_cli_choices()))

    def test_the_proxy_engine_is_a_transcription_engine(self) -> None:
        from nonoka_x.transcription import TranscriptionEngine

        self.assertIsInstance(engine_host.PackageTranscriptionEngine(PACKAGE), TranscriptionEngine)


class FakeRuntime:
    """The runtime facade as the readiness check asks it: `status()` and `tool_path()`."""

    def __init__(self, *, state: str = "ready", missing_models: set[str] = frozenset(),
                 tools: bool = True, managed: bool = True) -> None:
        self.state, self.missing_models, self.tools, self.managed = state, set(missing_models), tools, managed

    def status(self) -> dict:
        if not self.managed:
            return {}
        return {
            "runtime": {"state": self.state},
            "models": [
                {"id": model, "state": "missing" if model in self.missing_models else "ready"}
                for model in SPEC.environment.models
            ],
        }

    def tool_path(self, name: str) -> Path | None:
        return Path(f"{name}.exe") if self.tools else None


class ReadinessTests(unittest.TestCase):
    """The sidecar's `raw-srt` tier, from what the package declares, with its codes and words unchanged (WP5)."""

    def readiness(self, runtime: FakeRuntime | None, *, platform: str = "win32", missing_modules=frozenset(),
                  which: bool = False) -> list[dict]:
        def find_spec(name, *_args, **_kwargs):
            return None if name in missing_modules else object()

        with (
            mock.patch.object(engine_host.sys, "platform", platform),
            mock.patch("shutil.which", return_value="x" if which else None),
            mock.patch("importlib.util.find_spec", side_effect=find_spec),
        ):
            readiness = engine_host.runtime_engine_readiness(PACKAGE, runtime)
        return [issue.to_dict() for issue in readiness.issues]

    def test_ready_when_the_managed_runtime_and_models_are_there(self) -> None:
        self.assertEqual(self.readiness(FakeRuntime(), missing_modules={"torch"}), [])

    def test_missing_models_are_named(self) -> None:
        issues = self.readiness(FakeRuntime(missing_models={"whisper", "qwen-referee"}))
        self.assertEqual(issues, [{"code": "missing_model", "message": "ASR 所需模型尚未安装：whisper、qwen-referee"}])

    def test_an_outdated_runtime_asks_for_an_update_instead_of_listing_modules(self) -> None:
        issues = self.readiness(FakeRuntime(state="outdated"), missing_modules={"torch"})
        self.assertEqual([issue["code"] for issue in issues], ["runtime_outdated"])
        self.assertIn("需要更新", issues[0]["message"])

    def test_a_missing_runtime_lists_the_modules_the_launcher_lacks(self) -> None:
        issues = self.readiness(FakeRuntime(state="missing"), missing_modules={"torch", "audio_separator"})
        self.assertEqual(issues, [{"code": "missing_runtime", "message": "AI 运行时缺失：torch、audio_separator"}])

    def test_media_tools_from_the_tool_layer_only(self) -> None:
        # WP-H (owner H7): the package's process finds its tools in the tool
        # layer only, so a copy on the user's PATH no longer makes it ready.
        missing = [{"code": "missing_ffmpeg", "message": "缺少项目必备依赖 FFmpeg 或 FFprobe"}]
        self.assertEqual(self.readiness(FakeRuntime(tools=False), which=True), missing)
        self.assertEqual(self.readiness(FakeRuntime(tools=False)), missing)
        self.assertEqual(self.readiness(FakeRuntime(), which=False), [])

    def test_only_windows_runs_the_local_gpu_stack(self) -> None:
        issues = self.readiness(FakeRuntime(), platform="darwin")
        self.assertEqual(issues, [{"code": "unsupported_platform", "message": "本地 GPU 流水线当前仅支持 Windows x64/NVIDIA"}])

    def test_nothing_managed_asks_no_model(self) -> None:
        self.assertEqual(self.readiness(FakeRuntime(managed=False)), [])

    def test_no_runtime_is_not_ready(self) -> None:
        self.assertEqual(
            self.readiness(None),
            [{"code": "runtime_unavailable", "message": "无法查询本机运行环境，FineSub 不可用"}],
        )


def _job(task_dir: Path, target: str = "raw-srt", **options: Any) -> dict[str, Any]:
    return {
        "source": str(task_dir.parent / "media" / "节目.m4a"),
        "language": "ja",
        "task_dir": str(task_dir),
        "cache_dir": str(task_dir / "cache"),
        "target": target,
        "options": options,
    }


class RunPlanTests(unittest.TestCase):
    def test_devices(self) -> None:
        self.assertEqual(engine_device("cuda:1"), ("cuda", "1"))
        self.assertEqual(engine_device("cuda:x"), ("cuda", None))
        self.assertEqual(engine_device("cuda"), ("cuda", None))
        self.assertEqual(engine_device("CPU"), ("cpu", None))
        self.assertEqual(engine_device(None), ("cuda", None))

    def test_diarization_matches_the_worker(self) -> None:
        from nonoka_x import worker

        for block in (None, {"level": "off", "max_speakers": 3}, {"level": "acoustics", "max_speakers": None},
                      {"level": "judge-audio", "max_speakers": 2}):
            self.assertEqual(engine_module.diarize_keywords(block),
                             worker.diarization_kwargs({"diarization": block}), block)

    def test_targets_stop_at_the_stable(self) -> None:
        for target, stage in (("vocal", "vocal"), ("aligned", "aligned"), ("stable", "stable"),
                              ("raw-srt", "stable"), ("translated-srt", "stable"), ("final-srt", "stable")):
            self.assertEqual(engine_stage(target), stage)
        with self.assertRaises(ValueError):
            engine_stage("subtitles")

    def test_defaults(self) -> None:
        task_dir = Path("/data/tasks/t1")
        plan = run_plan(_job(task_dir))
        self.assertEqual(dict(plan.keywords), {
            "output_path": task_dir / NATIVE / "节目.srt",
            "stage": "stable",
            "language": "ja",
            "device": "cuda",
            "gpu_tier": "auto",
            "separate": True,
            "extra_info": "",
            "knowledge": "update",
            "task_id": "t1",
            "task_artifact_dir": task_dir / NATIVE / "llm-artifacts",
            "resume": True,
            "diarize": "off",
        })
        self.assertIsNone(plan.cuda_visible_devices)

    def test_the_core_names_the_directories(self) -> None:
        plan = run_plan(_job(Path("/data/tasks/t1"), native_dir="/n/t", record_dir="/n/c/llm-artifacts"))
        self.assertEqual(plan.keywords["output_path"], Path("/n/t/节目.srt"))
        self.assertEqual(plan.keywords["task_artifact_dir"], Path("/n/c/llm-artifacts"))

    def test_options(self) -> None:
        plan = run_plan(_job(
            Path("/data/tasks/t1"), "aligned", title="标题", device="cuda:1", gpu_tier="standard", separate=False,
            diarization={"level": "acoustics", "max_speakers": 3}, extra_info="背景", knowledge="none", task_id="x",
        ))
        self.assertEqual(plan.keywords["output_path"].name, "标题.srt")
        self.assertEqual(plan.stage, "aligned")
        self.assertEqual(plan.cuda_visible_devices, "1")
        self.assertEqual(
            {key: plan.keywords[key] for key in ("device", "gpu_tier", "separate", "extra_info", "knowledge",
                                                 "task_id", "diarize", "diarize_max_speakers")},
            {"device": "cuda", "gpu_tier": "standard", "separate": False, "extra_info": "背景",
             "knowledge": "none", "task_id": "x", "diarize": "acoustics", "diarize_max_speakers": 3},
        )

    def test_the_keywords_are_run_pipelines(self) -> None:
        from finesub.stages import _PIPELINE_DEFAULTS

        self.assertLessEqual(set(run_plan(_job(Path("/t"), diarization={"level": "acoustics", "max_speakers": 2}))
                                 .keywords), set(_PIPELINE_DEFAULTS))
        self.assertLessEqual(CORRECTION_ONLY, set(_PIPELINE_DEFAULTS))

    def test_evidence_path_matches_the_engine(self) -> None:
        from finesub.speech.diarization.evidence import evidence_path

        timeline = Path("/runs/节目-diar.json")
        self.assertEqual(engine_module.evidence_path(timeline), evidence_path(timeline))


def _stable(segments: list[Any], metadata: Any = None) -> dict[str, Any]:
    document: dict[str, Any] = {"segments": segments}
    if metadata is not None:
        document["metadata"] = metadata
    return document


class ProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="nonoka-projection-"))
        self.addCleanup(shutil.rmtree, self.directory, True)

    def write(self, name: str, value: Any) -> Path:
        path = self.directory / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def test_lines_follow_the_segments(self) -> None:
        stable = self.write("a-stable.json", _stable(
            [
                {"start": 0.5, "end": 1.5, "text": "いや", "lang": "ja", "confidence": 0.9,
                 "words": [{"word": "いや", "start": 0.5, "end": 1.0, "confidence": 0.6, "space_before": False}]},
                {"start": 1.0, "end": 2.0, "text": "", "tags": []},
            ],
            {"asr_align": {"model": "large-v3-turbo", "language": "ja"}},
        ))
        vad = self.write("a-vad.json", {"audio_duration": 184.1575})
        before = stable.read_bytes()
        document = project_transcript(stable, stage="stable", language="ja", vad_json=vad)
        self.assertEqual(stable.read_bytes(), before)
        expected = {
            "schema": 1,
            "engine": "finesub",
            "model": "large-v3-turbo",
            "language": "ja",
            "duration": 184.1575,
            "lines": [
                {"id": "1", "start": 0.5, "end": 1.5, "text": "いや",
                 "words": [{"text": "いや", "start": 0.5, "end": 1.0}]},
                {"id": "2", "start": 1.0, "end": 2.0, "text": ""},
            ],
            "metadata": {"stage": "stable", "projected_from": "finesub.stable_json",
                         "sha256": hashlib.sha256(before).hexdigest()},
        }
        self.assertEqual(document, expected)
        # What the core reads it back as is the same document.
        self.assertEqual(TranscriptDocument.from_dict(document).to_dict(), expected)

    def test_it_is_written_the_way_the_core_writes_one(self) -> None:
        from nonoka_x.transcription import write_transcript

        document = project_transcript(STABLE_FIXTURE, stage="stable", language="ja")
        ours = engine_module.write_transcript(document, self.directory / "ours.json")
        core = write_transcript(TranscriptDocument.from_dict(document), self.directory / "core.json")
        self.assertEqual(ours.read_bytes(), core.read_bytes())

    def test_the_fixture_projects(self) -> None:
        document = project_transcript(STABLE_FIXTURE, stage="stable", language="ja")
        self.assertEqual([line["id"] for line in document["lines"]], ["1", "2", "3"])
        self.assertNotIn("model", document)
        self.assertEqual(document["duration"], max(line["end"] for line in document["lines"]))

    def test_auto_takes_the_recognizers_language_then_the_segments(self) -> None:
        segments = [{"start": 0, "end": 1, "text": "a", "lang": "zh"}, {"start": 1, "end": 2, "text": "b", "lang": "ja"},
                    {"start": 2, "end": 3, "text": "c", "lang": "ja"}]
        recorded = self.write("r.json", _stable(segments, {"asr_align": {"language": "en"}}))
        self.assertEqual(project_transcript(recorded, stage="stable", language="auto")["language"], "en")
        unrecorded = self.write("u.json", _stable(segments, {"asr_align": {"language": None}}))
        self.assertEqual(project_transcript(unrecorded, stage="stable", language="auto")["language"], "ja")
        bare = self.write("b.json", _stable([{"start": 0, "end": 1, "text": "a"}]))
        self.assertEqual(project_transcript(bare, stage="stable", language="auto")["language"], "auto")
        self.assertEqual(project_transcript(recorded, stage="stable", language="zh")["language"], "zh")

    def test_a_segment_the_format_rejects_is_an_error(self) -> None:
        for segments in ([{"start": 2.0, "end": 1.0, "text": "a"}],
                         [{"start": -0.1, "end": 1.0, "text": "a"}],
                         [{"start": 0.0, "end": 1.0}],
                         [{"start": 0.0, "end": 1.0, "text": "a", "words": [{"word": "a", "start": 0.5}]}],
                         [{"start": 0.0, "end": 1.0, "text": "a", "words": {}}]):
            with self.subTest(segments), self.assertRaises(TranscriptError):
                project_transcript(self.write("bad.json", _stable(segments)), stage="stable", language="ja")
        with self.assertRaises(TranscriptError):
            project_transcript(self.write("none.json", {"metadata": {}}), stage="stable", language="ja")


class TranscribeTests(unittest.TestCase):
    """`transcribe` around a stand-in `run_transcription` that writes what the real one would."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="nonoka-engine-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "media").mkdir()
        (self.root / "media" / "节目.m4a").write_bytes(b"audio")
        self.task_dir = self.root / "tasks" / "t1"
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def fake_run_transcription(self, input_path: str, **keywords: Any) -> Any:
        from finesub.stages import default_pipeline_paths

        self.calls.append((input_path, keywords))
        paths = default_pipeline_paths(Path(input_path), keywords["output_path"])
        stage = keywords["stage"]
        paths.vocal_audio.parent.mkdir(parents=True, exist_ok=True)
        paths.vocal_audio.write_bytes(b"OggS")
        paths.metadata_json.write_text("{}", encoding="utf-8")
        if stage in ("aligned", "stable"):
            paths.vad_json.write_text(json.dumps({"audio_duration": 9.5}), encoding="utf-8")
            shutil.copyfile(STABLE_FIXTURE, paths.aligned_json)
        if stage == "stable":
            shutil.copyfile(STABLE_FIXTURE, paths.stable_json)
            if keywords.get("diarize") != "off":
                paths.diar_json.write_text("{}", encoding="utf-8")
                paths.diar_rows_json.write_text("{}", encoding="utf-8")
        return paths

    def transcribe(self, target: str, **options: Any) -> TranscriptionResult:
        with mock.patch("finesub.stages.run_transcription", self.fake_run_transcription), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CUDA_VISIBLE_DEVICES", None)
            self.reporter = RecordingReporter()
            result = engine_module.transcribe(_job(self.task_dir, target, **options), self.reporter)
            self.visible = os.environ.get("CUDA_VISIBLE_DEVICES")
        return TranscriptionResult.from_dict(json.loads(json.dumps(result)))

    def test_to_the_stable(self) -> None:
        result = self.transcribe("final-srt", diarization={"level": "acoustics"}, device="cuda:1")
        native = self.task_dir / NATIVE
        self.assertEqual(self.calls[0][1]["stage"], "stable")
        self.assertEqual(self.visible, "1")
        self.assertEqual(result.transcript_json, self.task_dir / "transcript.json")
        self.assertEqual(dict(result.metadata), {"stage": "stable", "transcript": True})
        self.assertEqual(dict(result.native), {
            "finesub.vocal_audio": native / "节目-vocal.ogg",
            "finesub.vad_json": native / "节目-vad.json",
            "finesub.aligned_json": native / "节目-aligned.json",
            "finesub.stable_json": native / "节目-stable.json",
            "finesub.diar_json": native / "节目-diar.json",
            "finesub.diar_rows_json": native / "节目-diar-rows.json",
            "finesub.metadata_json": native / "节目-metadata.json",
        })
        self.assertLessEqual({key.split(".", 1)[1] for key in result.native}, set(engine_module.NATIVE_PRODUCTS))
        self.assertEqual((native / "节目-stable.json").read_bytes(), STABLE_FIXTURE.read_bytes())
        document = read_transcript(result.transcript_json)
        self.assertEqual(document.duration, 9.5)
        self.assertEqual(document.metadata["projected_from"], "finesub.stable_json")

    def test_to_aligned_projects_the_aligned_record(self) -> None:
        result = self.transcribe("aligned")
        self.assertEqual(dict(result.metadata), {"stage": "aligned", "transcript": True})
        self.assertNotIn("finesub.stable_json", result.native)
        self.assertEqual(result.read_transcript().metadata["projected_from"], "finesub.aligned_json")

    def test_to_the_vocal_track_writes_no_transcript(self) -> None:
        result = self.transcribe("vocal")
        self.assertEqual(dict(result.metadata), {"stage": "vocal", "transcript": False})
        self.assertFalse(result.transcript_json.exists())
        self.assertEqual(set(result.native), {"finesub.vocal_audio", "finesub.metadata_json"})

    def test_finesub_reports_to_the_reporter_it_is_handed(self) -> None:
        from finesub.reporting import current_reporter

        seen = []

        def run(input_path: str, **keywords: Any) -> Any:
            seen.append(current_reporter())
            return self.fake_run_transcription(input_path, **keywords)

        reporter = RecordingReporter()
        with mock.patch("finesub.stages.run_transcription", run):
            engine_module.transcribe(_job(self.task_dir, "vocal"), reporter)
        self.assertIs(seen[0], reporter)


if __name__ == "__main__":
    unittest.main()
