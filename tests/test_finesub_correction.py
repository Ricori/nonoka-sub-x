"""WP4b: finesub correction (`nonoka_x.correction.finesub`).

Three things are proven here (`docs/transcription-engines-plan.md` WP4b):

- **A native stable gives the model the same requests.** The quality gate's
  LLM replay (10 cases) and its cloud-continuation call scenarios are run with
  the worker's `run_pipeline` replaced by `FineSubCorrection.run` on a
  `CorrectionJob` built from the very arguments the worker passed -- the job
  crosses JSON on the way, as it will into an engine package -- and compared
  with the gate's own baselines, which are not touched.
- **A `transcript.json` with nothing but lines reaches `raw-srt`** in a clean
  child interpreter that never imports torch or numpy, and the speakers of its
  lines are written as `-diar-rows.json`.
- **The module stays movable**: it imports the two contracts and nothing else
  of Nonoka's, never a transcription engine, and FineSub only lazily.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from nonoka_x import correction
from nonoka_x.correction import finesub as engine
from nonoka_x.transcription import TranscriptDocument, TranscriptionResult

ROOT = Path(__file__).resolve().parents[1]
VENDOR_SOURCE = ROOT / "third_party" / "finesub" / "src"
MODULE = ROOT / "src" / "nonoka_x" / "correction" / "finesub.py"


def _document(lines: list[dict], **fields) -> TranscriptDocument:
    return TranscriptDocument.from_dict(
        {"schema": 1, "engine": "mai2", "model": "mai-transcribe-2", "language": "ja", "duration": 9.0,
         "lines": lines, **fields}
    )


# --- transcript.json -> stable -------------------------------------------------------


def test_lines_become_segments_in_finesubs_order() -> None:
    document = _document([
        {"id": "b", "start": 2.0, "end": 3.0, "text": "二", "speaker": "S2"},
        {"id": "a", "start": 1.0, "end": 2.5, "text": "一", "speaker": "S1",
         "words": [{"text": "一", "start": 1.0, "end": 2.5, "confidence": 0.9}]},
        {"id": "c", "start": 2.0, "end": 2.8, "text": "三", "speaker": "S1", "note": "kept out"},
        {"id": "d", "start": 4.0, "end": 4.5, "text": ""},
    ])
    stable = engine.stable_from_transcript(document)
    assert stable["segments"] == [
        {"start": 1.0, "end": 2.5, "text": "一", "line_speaker": "S1", "lang": "ja",
         "words": [{"word": "一", "start": 1.0, "end": 2.5}]},
        # The same start: by speaker, as FineSub orders a cloud transcription's lines.
        {"start": 2.0, "end": 2.8, "text": "三", "line_speaker": "S1", "lang": "ja"},
        {"start": 2.0, "end": 3.0, "text": "二", "line_speaker": "S2", "lang": "ja"},
        {"start": 4.0, "end": 4.5, "text": "", "lang": "ja"},
    ]
    assert stable["metadata"] == {
        "asr_align": {"backend": "mai2", "language": "ja", "audio_duration": 9.0},
        "joint_asr": {"backend": "mai2", "model": "mai-transcribe-2"},
    }


def test_the_source_language_reaches_the_correction_mode(tmp_path: Path) -> None:
    """A Chinese transcription only corrects: FineSub reads the language from the stable."""

    from finesub.llm.stages.three_pass import source_language

    stable = tmp_path / "x-stable.json"
    engine.write_stable(engine.stable_from_transcript(_document(
        [{"id": "1", "start": 0.0, "end": 1.0, "text": "你好"}], language="zh")), stable)
    assert source_language(stable) == "zh"


def test_a_document_without_a_model_is_named_by_its_engine() -> None:
    stable = engine.stable_from_transcript(TranscriptDocument.from_dict(
        {"schema": 1, "engine": "gemini-transcribe", "language": "ja", "duration": 1.0, "lines": []}))
    assert stable["segments"] == []
    assert stable["metadata"]["joint_asr"] == {"backend": "gemini-transcribe", "model": "gemini-transcribe"}


def test_a_stable_is_written_once(tmp_path: Path) -> None:
    path = tmp_path / "w" / "x-stable.json"
    engine.write_stable({"segments": [{"text": "first"}]}, path)
    engine.write_stable({"segments": [{"text": "second"}]}, path)
    assert "first" in path.read_text(encoding="utf-8")
    assert [item.name for item in path.parent.iterdir()] == ["x-stable.json"]


# --- the native stable -------------------------------------------------------------


def test_speakers_follow_the_input() -> None:
    assert engine.speakers_for({engine.NATIVE_STABLE: "s.json"}) == ("acoustic", "vocal")
    assert engine.speakers_for({engine.NATIVE_STABLE: "s.json", engine.NATIVE_DIAR: "d.json"}) == ("acoustic", "vocal")
    assert engine.speakers_for({}) == ("line-labels", "source")
    assert engine.speakers_for({"mai2.raw_responses": "r"}) == ("line-labels", "source")


def test_native_files_are_placed_byte_for_byte_and_once(tmp_path: Path) -> None:
    from finesub.stages import default_pipeline_paths

    origin = tmp_path / "native"
    origin.mkdir()
    files = {
        engine.NATIVE_STABLE: origin / "a-stable.json",
        engine.NATIVE_DIAR: origin / "a-diar.json",
        engine.NATIVE_DIAR_EVIDENCE: origin / "a-diar-evidence.npz",
        engine.NATIVE_DIAR_ROWS: origin / "a-diar-rows.json",
    }
    for key, path in files.items():
        path.write_bytes(f"{key}\r\n\x00 first".encode())
    media = tmp_path / "media.m4a"
    output = engine.output_path(tmp_path / "task", "节目")
    output.parent.mkdir(parents=True)

    paths = engine.place_native(media, output, files)
    expected = default_pipeline_paths(media, output)
    assert paths == expected
    placed = {
        engine.NATIVE_STABLE: Path(expected.stable_json),
        engine.NATIVE_DIAR: Path(expected.diar_json),
        engine.NATIVE_DIAR_EVIDENCE: output.with_name("节目-diar-evidence.npz"),
        engine.NATIVE_DIAR_ROWS: Path(expected.diar_rows_json),
    }
    for key, path in placed.items():
        assert path.read_bytes() == files[key].read_bytes(), key
    assert sorted(path.name for path in output.parent.iterdir()) == sorted(path.name for path in placed.values())

    for path in files.values():
        path.write_bytes(b"second")
    engine.place_native(media, output, files)
    assert all(b"first" in path.read_bytes() for path in placed.values())


def test_a_native_stable_already_in_place_is_left_alone(tmp_path: Path) -> None:
    output = engine.output_path(tmp_path, "t")
    output.parent.mkdir(parents=True)
    stable = output.with_name("t-stable.json")
    stable.write_text("{}", encoding="utf-8")
    engine.place_native(tmp_path / "t.wav", output, {engine.NATIVE_STABLE: stable})
    assert stable.read_text(encoding="utf-8") == "{}"
    assert [item.name for item in output.parent.iterdir()] == ["t-stable.json"]


# --- the job --------------------------------------------------------------------------


def test_the_job_carries_the_task_fields_through_json(tmp_path: Path) -> None:
    job = correction.CorrectionJob(
        transcription=TranscriptionResult(transcript_json=tmp_path / "transcript.json",
                                          native={engine.NATIVE_STABLE: tmp_path / "a-stable.json"}),
        source=tmp_path / "v.mp4", target="final-srt", task_dir=tmp_path,
        options={"media": None, "fast": "auto", "proofread": "off"},
        knowledge={"knowledge": "read", "knowledge_subjects": ["streamer/某人"]},
        diarization_level="judge-text", title="节目", task_id="t1", task_summary="字幕任务：节目",
    )
    again = correction.CorrectionJob.from_dict(json.loads(json.dumps(job.to_dict(), ensure_ascii=False)))
    assert again == job
    old = {key: value for key, value in job.to_dict().items() if key not in {"title", "task_id", "task_summary"}}
    assert correction.CorrectionJob.from_dict(old).title == ""
    with pytest.raises(correction.ContractError):
        correction.CorrectionJob(transcription=job.transcription, source=job.source, target="raw-srt",
                                 task_dir=tmp_path, task_id=7)


def test_the_engine_meets_the_protocol() -> None:
    instance = engine.FineSubCorrection()
    assert isinstance(instance, correction.CorrectionEngine)
    assert instance.spec.id == correction.DEFAULT_ENGINE
    assert instance.spec.environment.components == () and instance.spec.environment.gpu == "none"
    from nonoka_x.transcription import ReadinessContext

    assert instance.readiness(ReadinessContext(target="final-srt"), {}).ready


# --- the module stays movable ------------------------------------------------------


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports would not survive the move into a package"
            names.add(node.module or "")
    return names


def test_only_the_contracts_of_nonoka_are_imported() -> None:
    nonoka = {name for name in _imports(MODULE) if name.split(".")[0] == "nonoka_x"}
    assert nonoka == {"nonoka_x.correction", "nonoka_x.transcription"}
    assert not any(name.startswith("nonoka_x.transcription.engines") for name in _imports(MODULE))


def test_importing_it_loads_neither_finesub_nor_the_asr_stack() -> None:
    code = (
        "import sys\n"
        "import nonoka_x.correction.finesub\n"
        "heavy = sorted(m for m in sys.modules if m.split('.')[0] in {'torch', 'numpy', 'finesub', 'finesub_bootstrap'})\n"
        "print(','.join(heavy))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, check=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    assert completed.stdout.strip() == ""


def test_the_axis_keeps_its_own_error() -> None:
    from nonoka_x.axis import AxisError, stable_from_axis

    with pytest.raises(AxisError, match="no source text"):
        stable_from_axis([{"t0": 0.0, "t1": 1.0, "ja": " "}])


# --- a transcript.json with only lines, to raw-srt ---------------------------------

_LINES = [
    {"id": "l2", "start": 2.2, "end": 3.0, "text": "ございます", "speaker": "S2",
     "words": [{"text": "ございます", "start": 2.2, "end": 3.0}]},
    {"id": "l1", "start": 1.0, "end": 2.0, "text": "おはよう", "speaker": "S1",
     "words": [{"text": "おはよう", "start": 1.0, "end": 2.0}]},
    {"id": "l3", "start": 4.0, "end": 5.0, "text": "またね", "speaker": "S1"},
    {"id": "l4", "start": 5.5, "end": 6.0, "text": "うん"},
]

_LINES_CHILD = textwrap.dedent(
    """
    import json
    import sys
    from pathlib import Path

    from nonoka_x.correction import CorrectionJob
    from nonoka_x.correction.finesub import FineSubCorrection
    from nonoka_x.transcription import TranscriptDocument, TranscriptionResult, write_transcript

    root = Path(sys.argv[1])
    lines = json.loads(sys.argv[2])
    transcript = write_transcript(TranscriptDocument.from_dict({
        "schema": 1, "engine": "mai2", "model": "mai-transcribe-2", "language": "ja", "duration": 6.5,
        "lines": lines,
    }), root / "task" / "transcription" / "transcript.json")
    source = root / "media" / "节目.wav"
    source.parent.mkdir()
    source.write_bytes(b"")


    class Recorder:
        def __init__(self):
            self.events = []

        def stage(self, stage, message=""):
            self.events.append(["stage", stage])

        def progress(self, stage, *, completed, total=None, unit="", message=""):
            pass

        def log(self, message, fields=None):
            self.events.append(["log", message])

        def warning(self, code, message):
            self.events.append(["warning", code])

        def cancelled(self):
            return False


    recorder = Recorder()
    job = CorrectionJob(
        transcription=TranscriptionResult(transcript_json=transcript),
        source=source, target="raw-srt", task_dir=root / "task", title="节目", task_id="t1",
    )
    result = FineSubCorrection().run(CorrectionJob.from_dict(json.loads(json.dumps(job.to_dict()))), recorder)
    workspace = root / "task" / "workspace"
    print(json.dumps({
        "modules": sorted(name for name in ("torch", "numpy") if name in sys.modules),
        "artifacts": {name: path.name for name, path in result.artifacts.items()},
        "metadata": dict(result.metadata),
        "files": {path.name: path.read_text(encoding="utf-8") for path in sorted(workspace.iterdir())
                  if path.is_file() and not path.name.endswith("-metadata.json")},
        "events": recorder.events,
    }, ensure_ascii=False))
    """
)


@pytest.fixture(scope="module")
def lines_run(tmp_path_factory: pytest.TempPathFactory) -> dict:
    from tests.quality_harness.hygiene import isolated_environment

    root = tmp_path_factory.mktemp("lines")
    environment, _removed = isolated_environment(root)
    environment.update({
        "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(VENDOR_SOURCE)]),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TEMP": str(root / "tmp"),
        "TMP": str(root / "tmp"),
    })
    (root / "tmp").mkdir()
    (root / "runs").mkdir()
    completed = subprocess.run(
        [sys.executable, "-c", _LINES_CHILD, str(root / "runs"), json.dumps(_LINES, ensure_ascii=False)],
        cwd=root, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=300,
    )
    if completed.returncode != 0:
        raise AssertionError(f"the child run failed:\n{completed.stdout}\n{completed.stderr}")
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_lines_reach_raw_srt_without_torch_or_numpy(lines_run: dict) -> None:
    assert lines_run["modules"] == []
    raw = lines_run["files"]["节目-raw.srt"]
    for text in ("おはよう", "ございます", "またね", "うん"):
        assert text in raw
    assert raw.index("おはよう") < raw.index("ございます")
    assert lines_run["metadata"] == {"input": "transcript", "speaker_source": "line-labels",
                                     "judge_audio": "source"}


def test_the_lines_speakers_are_written_as_the_rows_record(lines_run: dict) -> None:
    files = lines_run["files"]
    assert "节目-diar.json" not in files, "there is no acoustic timeline to read or write"
    rows = files["节目-diar-rows.json"]
    assert '"joint_speaker": "S1"' in rows
    assert '"joint_speaker": "S2"' in rows
    assert '"joint:mai2"' in rows
    assert '"mai-transcribe-2"' in rows
    assert {"raw_srt", "diar_rows_json", "stable_json"} <= set(lines_run["artifacts"])


def test_the_converted_stable_is_the_documented_one(lines_run: dict) -> None:
    stable = json.loads(lines_run["files"]["节目-stable.json"])
    assert stable == json.loads(json.dumps(engine.stable_from_transcript(_document(_LINES, duration=6.5)),
                                           ensure_ascii=False))


def test_the_run_reports_which_input_it_used(lines_run: dict) -> None:
    events = lines_run["events"]
    assert ["log", "纠错输入：由 transcript.json 转成的 stable，说话人来自各行标签"] in events
    stages = [stage for kind, stage in events if kind == "stage"]
    assert stages == ["diarize", "raw-srt"]


# --- the quality gate, through the engine ------------------------------------------

#: Run inside the gate's own children (`calls.child_main`, `replay.child_main`), once the
#: worker environment is in place: the worker's `run_pipeline` becomes a `CorrectionJob`
#: for `FineSubCorrection.run`, built from exactly the arguments the worker passed.
_GATE_CHILD = textwrap.dedent(
    """
    import json
    import sys
    from pathlib import Path

    mode, spec_path = sys.argv[1], Path(sys.argv[2])
    root = Path(json.loads(spec_path.read_text(encoding="utf-8"))["root"])

    from tests.quality_harness import calls, replay

    #: Every keyword the worker hands `run_pipeline`; any other one would be lost here.
    KNOWN = {
        "output_path", "stage", "language", "device", "gpu_tier", "separate", "llm_media", "llm_retrieval",
        "llm_difficulty", "llm_proofread", "llm_fast", "extra_info", "extra_style", "knowledge",
        "task_summary", "task_id", "task_artifact_dir", "resume", "speaker_tracks", "diarize",
        "diarize_max_speakers", "knowledge_subjects",
    }


    def install():
        import finesub.stages as stages

        from nonoka_x.correction import CorrectionJob
        from nonoka_x.correction import finesub as engine
        from nonoka_x.transcription import EventReporter, TranscriptionResult

        def run_pipeline(input_path, **given):
            unknown = sorted(set(given) - KNOWN)
            if unknown:
                raise TypeError(f"the worker passed keywords the job cannot carry: {unknown}")
            output = Path(given["output_path"])
            task_dir = output.parent.parent
            paths = stages.default_pipeline_paths(input_path, output)
            if not Path(paths.stable_json).exists():
                raise AssertionError("only a continuation from a stable runs through the engine here")
            assert output == engine.output_path(task_dir, output.stem)
            assert Path(given["task_artifact_dir"]) == engine.artifact_dir(task_dir)
            assert given["resume"] is True and given["speaker_tracks"] == "off"
            native = {engine.NATIVE_STABLE: root / "cloud" / "cloud.stable.json"}
            if (root / "cloud" / "cloud.diar.json").exists():
                native[engine.NATIVE_DIAR] = root / "cloud" / "cloud.diar.json"
            job = CorrectionJob(
                transcription=TranscriptionResult(transcript_json=task_dir / "transcript.json", native=native),
                source=Path(input_path),
                target=given["stage"],
                task_dir=task_dir,
                options={
                    "media": given["llm_media"], "retrieval": given["llm_retrieval"],
                    "difficulty": given["llm_difficulty"], "proofread": given["llm_proofread"],
                    "fast": given["llm_fast"], "extra_info": given["extra_info"],
                    "extra_style": given["extra_style"],
                },
                knowledge={"knowledge": given["knowledge"],
                           "knowledge_subjects": list(given.get("knowledge_subjects") or ())},
                diarization_level=given["diarize"],
                title=output.stem,
                task_id=given["task_id"],
                task_summary=given["task_summary"],
            )
            job = CorrectionJob.from_dict(json.loads(json.dumps(job.to_dict(), ensure_ascii=False)))
            result = engine.FineSubCorrection().run(job, EventReporter(lambda kind, payload: None))
            assert result.metadata["input"] == engine.INPUT_NATIVE
            return paths

        stages.run_pipeline = run_pipeline


    adopt = calls.adopt_worker_environment


    def adopt_then_install(data_dir, settings):
        environment = adopt(data_dir, settings)
        install()
        return environment


    calls.adopt_worker_environment = adopt_then_install
    replay.adopt_worker_environment = adopt_then_install
    raise SystemExit((calls if mode == "calls" else replay).child_main(spec_path))
    """
)


def _through_the_engine(harness, mode: str):
    """`harness`'s own children, entered through `_GATE_CHILD` instead of its `--child`."""

    def run(command, **kwargs):
        assert command[1:3] == ["-m", f"tests.quality_harness.{mode}"], command
        return subprocess.run([command[0], "-c", _GATE_CHILD, mode, str(command[-1])], **kwargs)

    return mock.patch.object(harness, "subprocess", SimpleNamespace(run=run))


#: The call scenarios that continue from a stable already in hand.
CONTINUATIONS = (
    "cloud-raw-srt-no-timeline",
    "cloud-raw-srt-timeline-acoustics",
    "cloud-final-srt-timeline-judge-audio",
    "cloud-translated-srt-zh-update",
)


def test_continuations_hand_the_algorithms_what_the_baseline_records() -> None:
    from tests.quality_harness import calls

    assert set(CONTINUATIONS) == {
        name for name, scenario in calls.SCENARIOS.items()
        if scenario["request"]["source"].get("kind") == "cloud_stable"
    }
    with _through_the_engine(calls, "calls"):
        records = calls.run_all(CONTINUATIONS, workers=4)
    assert calls.differences(records) == {}


def test_the_model_is_sent_the_baselines_requests() -> None:
    from tests.quality_harness import replay

    cases = [name for name in replay.CASES if replay.available(name)]
    with _through_the_engine(replay, "replay"):
        records = replay.run_all(cases)
    assert set(records) == set(cases)
    assert replay.differences(records) == {}
    missing = sorted(set(replay.CASES) - set(cases))
    if missing:
        pytest.skip(f"compared {len(cases)} replay cases; not on this machine: {', '.join(missing)}")


def test_the_continuation_seed_is_the_engines_placement() -> None:
    """The worker's cloud seed and the engine put the files in the same place."""

    from nonoka_x import worker

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        cloud = root / "cloud.stable.json"
        cloud.write_text('{"segments": []}', encoding="utf-8")
        timeline = root / "cloud.diar.json"
        timeline.write_text("{}", encoding="utf-8")
        media = root / "media.m4a"
        output = engine.output_path(root / "task", "节目")
        output.parent.mkdir(parents=True)
        request = {"source": {"kind": "cloud_stable", "path": str(media), "stable_json": str(cloud),
                              "diar_json": str(timeline)}}
        seeded = worker.seed_cloud_stable(request, output)
        paths = engine.place_native(media, output, {engine.NATIVE_STABLE: cloud, engine.NATIVE_DIAR: timeline})
        assert seeded == Path(paths.stable_json)
        assert Path(paths.diar_json).read_text(encoding="utf-8") == "{}"
