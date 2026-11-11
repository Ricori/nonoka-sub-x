"""FineSub as a transcription engine (`nonoka_x.transcription.engines.finesub`, WP4a).

The proof that matters is `CallEquivalenceThroughTheEngineTests`: for every
call-baseline scenario the engine serves (a local recognition: no cloud
continuation, no Japanese axis), a child interpreter builds the job the way
the worker will (`job_for`), runs it through the engine with the quality
gate's recording stand-ins at FineSub's algorithm entry points, and

- the algorithms receive exactly the arguments recorded in
  `tests/fixtures/quality/calls/<scenario>.json` for the transcription half
  (every call before the first stage after the stable), and
- the keywords the engine hands `run_transcription` are the ones the worker
  hands `run_pipeline` for the same request, but for the correction's own
  and a target clamped to the stable.

The harness (`tests.quality_harness`) and its baselines are used as they are.

Run as `python -m tests.test_engine_finesub --child <spec.json>` for one child.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for _path in (ROOT / "src", ROOT / "third_party" / "finesub" / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from nonoka_x.transcription import (  # noqa: E402
    ReadinessContext,
    TranscriptError,
    TranscriptionEngine,
    TranscriptionJob,
    TranscriptionResult,
    read_transcript,
)
from nonoka_x.transcription.engines import finesub as engine_module  # noqa: E402
from nonoka_x.transcription.engines.finesub import (  # noqa: E402
    SPEC,
    FineSubEngine,
    engine_device,
    engine_stage,
    project_transcript,
    run_plan,
)

MODULE_FILE = ROOT / "src" / "nonoka_x" / "transcription" / "engines" / "finesub.py"
STABLE_FIXTURE = ROOT / "tests" / "fixtures" / "finesub" / "sample-stable.json"
TASK_ID = "gate-task"

#: `run_pipeline` keywords the worker passes that only the stages after the
#: stable read (the correction request, the speaker exports); the engine
#: leaves them to the correction.
CORRECTION_ONLY = frozenset({
    "llm_media", "llm_retrieval", "llm_difficulty", "llm_proofread", "llm_fast",
    "extra_style", "task_summary", "speaker_tracks", "knowledge_subjects",
})

#: The record entries of the transcription half, by the module they belong to.
TRANSCRIPTION_ENTRY_MODULES = frozenset({
    "separation", "vad_asr_stage", "recall_decode", "device", "stabilization", "diarization",
})
#: The worker's own call at start-up (the `llm_model` overlay): the correction's.
WORKER_PREAMBLE = "model_routes.install_runtime_preferred"


# --- the job the worker will build ----------------------------------------------------------


def job_for(request: Mapping[str, Any], task_dir: Path) -> TranscriptionJob:
    """The engine job for a validated TaskRequest, built the way the worker does it (WP4c).

    The options are resolved here, in the worker's own functions, exactly as
    the worker resolves them before `run_pipeline` today: the correct-only
    adjustment of a Chinese task, the knowledge subjects written into the
    extra-info (and created first, so the ASR context can find them), the GPU
    tier with a legacy budget migrated.
    """

    from nonoka_x import worker
    from nonoka_x.gpu_tier import resolve_request_gpu_tier

    request = worker.for_task_mode(request, task_dir / "workspace" / "llm-artifacts")
    correction = dict(request.get("correction") or {})
    extra_info = worker.compose_knowledge_context(request, str(correction.get("extra_info") or ""))
    worker.prepare_knowledge_subjects(request, task_dir.name)
    source = request["source"]
    return TranscriptionJob(
        source=Path(source["path"]),
        language=request.get("language", "ja"),
        task_dir=task_dir,
        cache_dir=task_dir / "cache",
        target=request["target"],
        options={
            "title": str(source.get("title") or Path(source["path"]).stem),
            "device": request.get("device"),
            "gpu_tier": resolve_request_gpu_tier(request),
            "separate": bool(request.get("separate", True)),
            "diarization": request.get("diarization"),
            "extra_info": extra_info,
            "knowledge": request.get("knowledge", "update"),
            "task_id": task_dir.name,
        },
    )


class RecordingReporter:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    def stage(self, stage: str, message: str = "") -> None:
        self.events.append(("stage", [stage, message]))

    def progress(self, stage, *, completed, total=None, unit="", message="") -> None:  # noqa: ANN001
        self.events.append(("progress", [stage, completed, total]))

    def log(self, message: str, fields=None) -> None:  # noqa: ANN001
        self.events.append(("log", message))

    def warning(self, code: str, message: str) -> None:
        self.events.append(("warning", [code, message]))

    def cancelled(self) -> bool:
        return False


# --- the child -------------------------------------------------------------------------------


class _Captured(Exception):
    """Raised by the stand-in `run_pipeline` once it has what the worker passed."""


def _plain(value: Any) -> Any:
    from tests.quality_harness import stubs

    return stubs.encode(value)


def child_main(spec_path: Path) -> int:
    from tests.quality_harness import calls, stubs

    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    root = Path(spec["root"])
    sys.meta_path.insert(0, stubs.RefuseHeavyImports())
    settings = calls.bind_settings(root / "data", spec["settings"])
    calls.adopt_worker_environment(root / "data", settings)
    replacements = {
        "$MEDIA": str(root / "media" / "节目.m4a"),
        "$VIDEO": str(root / "media" / "视频节目.mp4"),
    }
    if "subject" in spec["setup"]:
        replacements["$SUBJECT_ID"] = calls._setup_subject(Path(os.environ["FINESUB_KNOWLEDGE_ROOT"]))

    from nonoka_x.local_provider import validate_request

    result: dict[str, Any] = {
        "literals": {value: key for key, value in replacements.items() if key == "$SUBJECT_ID"}
    }
    request = validate_request(calls._substitute(spec["request"], replacements))
    stubs.install(calls.STABLE_FIXTURE)
    task_dir = root / "data" / "tasks" / TASK_ID
    environment = dict(os.environ)

    # 1. The engine, on a task directory as fresh as the baseline's.
    stubs.RECORD.clear()
    job = job_for(request, task_dir)
    plan = run_plan(job)
    reporter = RecordingReporter()
    transcription = FineSubEngine().transcribe(job, reporter)
    result["engine_calls"] = list(stubs.RECORD)
    result["plan"] = {"input": plan.input_path, "keywords": _plain(dict(plan.keywords))}
    result["result"] = transcription.to_dict()
    result["events"] = reporter.events
    if transcription.metadata.get("transcript"):
        try:
            result["transcript"] = read_transcript(transcription.transcript_json).to_dict()
        except TranscriptError as error:
            result["transcript_error"] = str(error)

    # 2. The worker, stopped where it calls `run_pipeline`, for the keywords it passes.
    os.environ.clear()
    os.environ.update(environment)
    import finesub.stages

    captured: dict[str, Any] = {}

    def run_pipeline(input_path: Any, **keywords: Any) -> Any:
        captured["input"] = str(input_path)
        captured["keywords"] = _plain(keywords)
        raise _Captured

    from nonoka_x import worker

    previous_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps(request, ensure_ascii=False) + "\n")
    try:
        with mock.patch.object(finesub.stages, "run_pipeline", run_pipeline), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            worker.main(["--task-id", TASK_ID, "--task-dir", str(task_dir), "--vendor", str(calls.VENDOR)])
    finally:
        sys.stdin = previous_stdin
    result["worker"] = captured
    Path(spec["out"]).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0


# --- the parent ------------------------------------------------------------------------------


def engine_scenarios() -> list[str]:
    """The call-baseline scenarios a local recognition runs: the engine's."""

    from tests.quality_harness import calls

    return [
        name
        for name, scenario in calls.SCENARIOS.items()
        if scenario["request"]["source"].get("kind", "local_file") == "local_file"
        and not scenario["request"].get("axis")
    ]


def transcription_prefix(baseline_calls: list[Mapping[str, Any]]) -> tuple[list[Any], list[Any]]:
    """The baseline's transcription-half calls, and what follows them."""

    calls_ = list(baseline_calls)
    if calls_ and calls_[0]["entry"] == WORKER_PREAMBLE:
        calls_ = calls_[1:]
    for index, call in enumerate(calls_):
        if call["entry"].split(".", 1)[0] not in TRANSCRIPTION_ENTRY_MODULES:
            return calls_[:index], calls_[index:]
    return calls_, []


def run_child(name: str, scratch: Path) -> dict[str, Any]:
    from tests.quality_harness import calls

    scenario = calls.SCENARIOS[name]
    root = (scratch / name).resolve()
    for directory in ("media", "data", "tmp"):
        (root / directory).mkdir(parents=True)
    (root / "media" / "节目.m4a").write_bytes(b"quality-gate audio placeholder")
    (root / "media" / "视频节目.mp4").write_bytes(b"quality-gate video placeholder")
    spec = {**scenario, "root": str(root), "out": str(root / "record.json")}
    spec_path = root / "spec.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-m", "tests.test_engine_finesub", "--child", str(spec_path)],
        cwd=ROOT,
        env=calls.hermetic_environment(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    out = root / "record.json"
    if completed.returncode != 0 or not out.is_file():
        raise RuntimeError(f"scenario {name} child failed ({completed.returncode}):\n{completed.stderr[-4000:]}")
    raw = json.loads(out.read_text(encoding="utf-8"))
    normalizer = calls.Normalizer({"$ROOT": root, "$REPO": ROOT}, raw.get("literals"))
    return {key: normalizer.value(value) for key, value in raw.items() if key != "literals"}


class CallEquivalenceThroughTheEngineTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.names = engine_scenarios()
        scratch = Path(tempfile.mkdtemp(prefix="nonoka-engine-finesub-"))
        try:
            with ThreadPoolExecutor(max_workers=6) as pool:
                cls.records = dict(zip(cls.names, pool.map(lambda name: run_child(name, scratch), cls.names)))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def baseline(self, name: str) -> dict[str, Any]:
        from tests.quality_harness import calls

        return json.loads(calls.baseline_path(name).read_text(encoding="utf-8"))

    def test_the_engine_serves_every_local_recognition_in_the_matrix(self) -> None:
        self.assertEqual(len(self.names), 26)
        targets = {self.baseline(name)["request"]["target"] for name in self.names}
        self.assertEqual(targets, {"vocal", "aligned", "stable", "raw-srt", "translated-srt", "final-srt"})

    def test_algorithm_entry_points_receive_the_baseline_arguments(self) -> None:
        for name in self.names:
            with self.subTest(name):
                expected, rest = transcription_prefix(self.baseline(name)["calls"])
                self.assertTrue(expected, "the baseline has no transcription calls")
                leaked = [call["entry"] for call in rest
                          if call["entry"].split(".", 1)[0] in TRANSCRIPTION_ENTRY_MODULES]
                self.assertEqual(leaked, [], "transcription calls after the post-stable stages began")
                self.assertEqual(self.records[name]["engine_calls"], expected)

    def test_run_transcription_gets_the_worker_keywords_of_the_transcription_half(self) -> None:
        for name in self.names:
            with self.subTest(name):
                record = self.records[name]
                worker, plan = record["worker"], record["plan"]
                self.assertEqual(Path(plan["input"]), Path(worker["input"]))
                expected = {key: value for key, value in worker["keywords"].items() if key not in CORRECTION_ONLY}
                self.assertEqual(engine_stage(expected.pop("stage")), plan["keywords"]["stage"])
                actual = {key: value for key, value in plan["keywords"].items() if key != "stage"}
                self.assertEqual(actual, expected)

    def test_each_run_hands_back_its_products_and_a_valid_transcript(self) -> None:
        for name in self.names:
            with self.subTest(name):
                record = self.records[name]
                result = TranscriptionResult.from_dict(record["result"])
                target = self.baseline(name)["request"]["target"]
                stage = engine_stage(target)
                self.assertEqual(dict(result.metadata), {"stage": stage, "transcript": stage != "vocal"})
                self.assertEqual(result.transcript_json.as_posix(), f"$ROOT/data/tasks/{TASK_ID}/transcript.json")
                self.assertNotIn("transcript_error", record)
                if stage == "vocal":
                    self.assertNotIn("transcript", record)
                    self.assertNotIn("finesub.aligned_json", result.native)
                    continue
                self.assertEqual(record["transcript"]["engine"], "finesub")
                self.assertEqual(len(record["transcript"]["lines"]), 3)
                product = f"finesub.{stage}_json"
                self.assertIn(product, result.native)
                self.assertEqual(record["transcript"]["metadata"]["projected_from"], product)

    def test_the_engine_reports_through_its_reporter(self) -> None:
        stages = [event[1][0] for event in self.records["raw-srt-ja-defaults"]["events"] if event[0] == "stage"]
        self.assertEqual(stages, ["vocal", "aligned", "stable"])


# --- unit tests --------------------------------------------------------------------------------


class ModuleBoundaryTests(unittest.TestCase):
    """Written to move into the engine package as it is (plan 4.6, item 35)."""

    def imports(self) -> tuple[set[str], set[str]]:
        tree = ast.parse(MODULE_FILE.read_text(encoding="utf-8"))
        top, nested = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {"." * node.level + (node.module or "")}
            else:
                continue
            (top if node in tree.body else nested).update(names)
        return top, nested

    def test_nothing_of_nonoka_x_but_the_transcription_protocol_is_imported(self) -> None:
        top, nested = self.imports()
        ours = {name for name in top | nested if name.split(".")[0] == "nonoka_x" or name.startswith(".")}
        self.assertEqual(ours, {"nonoka_x.transcription"})

    def test_finesub_is_imported_only_when_transcribing(self) -> None:
        top, nested = self.imports()
        self.assertFalse({name for name in top if name.split(".")[0] in {"finesub", "finesub_bootstrap"}})
        self.assertEqual(
            {name for name in nested if name.split(".")[0] == "finesub"},
            {"finesub.reporting", "finesub.stages"},
        )

    def test_the_correction_layer_is_not_loaded(self) -> None:
        code = (
            "import sys; import nonoka_x.transcription.engines.finesub; "
            "print([m for m in sys.modules if m.startswith(('nonoka_x.correction', 'finesub'))])"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        )
        self.assertEqual(completed.stdout.strip(), "[]")


class SpecTests(unittest.TestCase):
    def test_the_engine_implements_the_protocol(self) -> None:
        self.assertIsInstance(FineSubEngine(), TranscriptionEngine)

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


class FakeRuntime:
    def __init__(self, missing: set[str] = frozenset(), unknown: set[str] = frozenset()) -> None:
        self.missing, self.unknown, self.asked = set(missing), set(unknown), []

    def _answer(self, name: str) -> bool:
        self.asked.append(name)
        if name in self.unknown:
            raise LookupError(name)
        return name not in self.missing

    def component_ready(self, name: str) -> bool:
        return self._answer(name)

    def model_ready(self, name: str) -> bool:
        return self._answer(name)


class ReadinessTests(unittest.TestCase):
    def test_ready_when_everything_declared_is_installed(self) -> None:
        runtime = FakeRuntime()
        self.assertTrue(FineSubEngine(runtime).readiness(ReadinessContext(target="raw-srt")).ready)
        self.assertEqual(runtime.asked, ["asr-runtime", "media", "separator", "whisper", "qwen-referee"])

    def test_speaker_models_are_asked_for_only_with_speakers(self) -> None:
        runtime = FakeRuntime(missing={"nemotron3"})
        engine = FineSubEngine(runtime)
        self.assertTrue(engine.readiness(ReadinessContext(options={"diarization": {"level": "off"}})).ready)
        readiness = engine.readiness(ReadinessContext(options={"diarization": {"level": "acoustics"}}))
        self.assertEqual([issue.to_dict() for issue in readiness.issues],
                         [{"code": "missing_model", "message": "缺少模型：nemotron3"}])

    def test_missing_and_unknown_components_are_issues(self) -> None:
        readiness = FineSubEngine(FakeRuntime(missing={"media"}, unknown={"whisper"})).readiness(ReadinessContext())
        self.assertEqual([issue.code for issue in readiness.issues], ["missing_component", "missing_model"])

    def test_no_runtime_is_not_ready(self) -> None:
        readiness = FineSubEngine().readiness(ReadinessContext())
        self.assertEqual([issue.code for issue in readiness.issues], ["runtime_unavailable"])


def _job(task_dir: Path, target: str = "raw-srt", **options: Any) -> TranscriptionJob:
    return TranscriptionJob(
        source=task_dir.parent / "media" / "节目.m4a",
        language="ja",
        task_dir=task_dir,
        cache_dir=task_dir / "cache",
        target=target,
        options=options,
    )


class RunPlanTests(unittest.TestCase):
    def test_devices(self) -> None:
        self.assertEqual(engine_device("cuda:1"), ("cuda", "1"))
        self.assertEqual(engine_device("cuda:x"), ("cuda", None))
        self.assertEqual(engine_device("cuda"), ("cuda", None))
        self.assertEqual(engine_device("CPU"), ("cpu", None))
        self.assertEqual(engine_device(None), ("cuda", None))

    def test_devices_match_the_worker(self) -> None:
        from nonoka_x import worker

        for value in ("cuda:0", "cuda:1", "cuda:x", "cuda", "cpu", None, "mps"):
            self.assertEqual(engine_device(value), worker._normalize_engine_device(value), value)

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

    def test_defaults_are_the_workers(self) -> None:
        task_dir = Path("/data/tasks/t1")
        plan = run_plan(_job(task_dir))
        self.assertEqual(dict(plan.keywords), {
            "output_path": task_dir / "workspace" / "节目.srt",
            "stage": "stable",
            "language": "ja",
            "device": "cuda",
            "gpu_tier": "auto",
            "separate": True,
            "extra_info": "",
            "knowledge": "update",
            "task_id": "t1",
            "task_artifact_dir": task_dir / "workspace" / "llm-artifacts",
            "resume": True,
            "diarize": "off",
        })
        self.assertIsNone(plan.cuda_visible_devices)

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
        self.assertEqual(document.to_dict(), {
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
        })

    def test_the_fixture_projects(self) -> None:
        document = project_transcript(STABLE_FIXTURE, stage="stable", language="ja")
        self.assertEqual([line.id for line in document.lines], ["1", "2", "3"])
        self.assertIsNone(document.model)
        self.assertEqual(document.duration, max(line.end for line in document.lines))

    def test_auto_takes_the_recognizers_language_then_the_segments(self) -> None:
        segments = [{"start": 0, "end": 1, "text": "a", "lang": "zh"}, {"start": 1, "end": 2, "text": "b", "lang": "ja"},
                    {"start": 2, "end": 3, "text": "c", "lang": "ja"}]
        recorded = self.write("r.json", _stable(segments, {"asr_align": {"language": "en"}}))
        self.assertEqual(project_transcript(recorded, stage="stable", language="auto").language, "en")
        unrecorded = self.write("u.json", _stable(segments, {"asr_align": {"language": None}}))
        self.assertEqual(project_transcript(unrecorded, stage="stable", language="auto").language, "ja")
        bare = self.write("b.json", _stable([{"start": 0, "end": 1, "text": "a"}]))
        self.assertEqual(project_transcript(bare, stage="stable", language="auto").language, "auto")
        self.assertEqual(project_transcript(recorded, stage="stable", language="zh").language, "zh")

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
            result = FineSubEngine().transcribe(_job(self.task_dir, target, **options), RecordingReporter())
            self.visible = os.environ.get("CUDA_VISIBLE_DEVICES")
        return result

    def test_to_the_stable(self) -> None:
        result = self.transcribe("final-srt", diarization={"level": "acoustics"}, device="cuda:1")
        workspace = self.task_dir / "workspace"
        self.assertEqual(self.calls[0][1]["stage"], "stable")
        self.assertEqual(self.visible, "1")
        self.assertEqual(result.transcript_json, self.task_dir / "transcript.json")
        self.assertEqual(dict(result.metadata), {"stage": "stable", "transcript": True})
        self.assertEqual(dict(result.native), {
            "finesub.vocal_audio": workspace / "节目-vocal.ogg",
            "finesub.vad_json": workspace / "节目-vad.json",
            "finesub.aligned_json": workspace / "节目-aligned.json",
            "finesub.stable_json": workspace / "节目-stable.json",
            "finesub.diar_json": workspace / "节目-diar.json",
            "finesub.diar_rows_json": workspace / "节目-diar-rows.json",
            "finesub.metadata_json": workspace / "节目-metadata.json",
        })
        self.assertLessEqual({key.split(".", 1)[1] for key in result.native}, set(engine_module.NATIVE_PRODUCTS))
        self.assertEqual((workspace / "节目-stable.json").read_bytes(), STABLE_FIXTURE.read_bytes())
        document = result.read_transcript()
        self.assertEqual(document.duration, 9.5)
        self.assertEqual(document.metadata["projected_from"], "finesub.stable_json")
        self.assertEqual(TranscriptionResult.from_dict(json.loads(json.dumps(result.to_dict()))), result)

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

    def test_a_cancelled_task_does_not_start(self) -> None:
        reporter = RecordingReporter()
        reporter.cancelled = lambda: True  # type: ignore[method-assign]
        with mock.patch("finesub.stages.run_transcription", self.fake_run_transcription):
            with self.assertRaises(engine_module.EngineCancelled):
                FineSubEngine().transcribe(_job(self.task_dir), reporter)
        self.assertEqual(self.calls, [])


class ReporterTests(unittest.TestCase):
    def test_finesub_calls_become_task_events(self) -> None:
        recorder = RecordingReporter()
        reporter = engine_module.FineSubReporter(recorder)
        reporter.planned(["vocal"])
        reporter.stage_started("vocal")
        reporter.stage_started("aligned", reused=True)
        reporter.progress("aligned", completed=1, total=2, unit="段", detail="x")
        reporter.warning("w", "说一次")
        reporter.warning("w", "说一次")
        reporter.summary("stable", {"lines": 3})
        self.assertEqual(recorder.events, [
            ("stage", ["vocal", "正在处理"]),
            ("stage", ["aligned", "已有结果，跳过"]),
            ("progress", ["aligned", 1, 2]),
            ("warning", ["w", "说一次"]),
            ("log", "stable: lines 3"),
        ])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", type=Path)
    arguments, _rest = parser.parse_known_args()
    if arguments.child is not None:
        raise SystemExit(child_main(arguments.child))
    unittest.main()
