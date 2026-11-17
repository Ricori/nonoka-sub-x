"""finesub correction: the correction package's adapter (`finesub_correct.engine`).

Moved from the core (`nonoka_x.correction.finesub`, WP4b) into the correction
package (WP-P1). That a native stable gives the model the same requests is
the quality gate's LLM replay and call equivalence, which since WP-P1 run the
worker and the package's own process (`tests/test_finesub_call_equivalence.py`).
Here:

- the adapter's own pieces: `transcript.json` -> stable, placing a
  transcription's files, the speakers of each input, the job's JSON;
- **a `transcript.json` with nothing but lines reaches `raw-srt`** in a clean
  child interpreter that has only the correction package on its path and never
  imports torch or numpy, and the speakers of its lines are written as
  `-diar-rows.json`;
- the core's reading of what the package declares (its spec, its readiness).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest import mock

import pytest

from finesub_correct import engine

from nonoka_x import correction, engine_host
from nonoka_x.transcription import TranscriptDocument, TranscriptionResult

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = engine_host.discover(ROOT / "engines")["finesub-correct"]


def _document(lines: list[dict], **fields) -> dict:
    """A `transcript.json` as the core writes and the adapter reads it."""

    document = TranscriptDocument.from_dict(
        {"schema": 1, "engine": "mai2", "model": "mai-transcribe-2", "language": "ja", "duration": 9.0,
         "lines": lines, **fields}
    )
    return json.loads(json.dumps(document.to_dict(), ensure_ascii=False))


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


def test_numbers_are_read_as_the_core_reads_them(tmp_path: Path) -> None:
    """Whole seconds in a `transcript.json` are floats to the core; so to the stable."""

    path = tmp_path / "transcript.json"
    path.write_text(json.dumps({"schema": 1, "engine": "mai2", "language": "ja", "duration": 2,
                                "lines": [{"id": "1", "start": 0, "end": 1, "text": "a",
                                           "words": [{"text": "a", "start": 0, "end": 1}]}]}), encoding="utf-8")
    stable = engine.stable_from_transcript(engine.read_transcript(path))
    assert json.dumps(stable["segments"]) == json.dumps(
        [{"start": 0.0, "end": 1.0, "text": "a", "words": [{"word": "a", "start": 0.0, "end": 1.0}], "lang": "ja"}]
    )
    assert stable["metadata"]["asr_align"]["audio_duration"] == 2.0


def test_the_source_language_reaches_the_correction_mode(tmp_path: Path) -> None:
    """A Chinese transcription only corrects: FineSub reads the language from the stable."""

    from finesub.llm.stages.three_pass import source_language

    stable = tmp_path / "x-stable.json"
    engine.write_stable(engine.stable_from_transcript(_document(
        [{"id": "1", "start": 0.0, "end": 1.0, "text": "你好"}], language="zh")), stable)
    assert source_language(stable) == "zh"


def test_a_document_without_a_model_is_named_by_its_engine() -> None:
    stable = engine.stable_from_transcript(
        {"schema": 1, "engine": "gemini-transcribe", "language": "ja", "duration": 1.0, "lines": []})
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
    """Everything FineSub's transcription left beside its stable, into this package's directory."""

    from finesub.stages import default_pipeline_paths

    origin = tmp_path / "native-transcribe"
    origin.mkdir()
    files = {
        engine.NATIVE_STABLE: origin / "a-stable.json",
        engine.NATIVE_DIAR: origin / "a-diar.json",
        engine.NATIVE_DIAR_EVIDENCE: origin / "a-diar-evidence.npz",
        engine.NATIVE_DIAR_ROWS: origin / "a-diar-rows.json",
        "finesub.vocal_audio": origin / "a-vocal.ogg",
        "finesub.vad_json": origin / "a-vad.json",
        "finesub.vad_energy_npz": origin / "a-vad-energy.npz",
        "finesub.aligned_json": origin / "a-aligned.json",
        "finesub.metadata_json": origin / "a-metadata.json",
    }
    for key, path in files.items():
        path.write_bytes(f"{key}\r\n\x00 first".encode())
    media = tmp_path / "media.m4a"
    job = {"source": str(media), "task_dir": str(tmp_path / "task"), "title": "节目", "options": {}}
    output = engine.output_path(job)
    assert output == tmp_path / "task" / "native" / "finesub-correct" / "节目.srt"
    output.parent.mkdir(parents=True)

    paths = engine.place_native(media, output, files)
    expected = default_pipeline_paths(media, output)
    assert paths == expected
    placed = {
        engine.NATIVE_STABLE: Path(expected.stable_json),
        engine.NATIVE_DIAR: Path(expected.diar_json),
        engine.NATIVE_DIAR_EVIDENCE: output.with_name("节目-diar-evidence.npz"),
        engine.NATIVE_DIAR_ROWS: Path(expected.diar_rows_json),
        "finesub.vocal_audio": Path(expected.vocal_audio),
        "finesub.vad_json": Path(expected.vad_json),
        "finesub.vad_energy_npz": Path(expected.vad_energy_npz),
        "finesub.aligned_json": Path(expected.aligned_json),
        "finesub.metadata_json": Path(expected.metadata_json),
    }
    for key, path in placed.items():
        assert path.read_bytes() == files[key].read_bytes(), key
    assert sorted(path.name for path in output.parent.iterdir()) == sorted(path.name for path in placed.values())

    for path in files.values():
        path.write_bytes(b"second")
    engine.place_native(media, output, files)
    assert all(b"first" in path.read_bytes() for path in placed.values())


def test_a_lossless_vocal_track_keeps_its_container(tmp_path: Path) -> None:
    origin = tmp_path / "a-vocal.flac"
    origin.write_bytes(b"fLaC")
    stable = tmp_path / "a-stable.json"
    stable.write_text("{}", encoding="utf-8")
    output = tmp_path / "c" / "节目.srt"
    paths = engine.place_native(tmp_path / "m.wav", output, {engine.NATIVE_STABLE: stable, "finesub.vocal_audio": origin})
    assert paths.resolve_vocal_audio().read_bytes() == b"fLaC"


def test_a_native_stable_already_in_place_is_left_alone(tmp_path: Path) -> None:
    output = tmp_path / "w" / "t.srt"
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
        options={"media": None, "fast": "auto", "native_dir": str(tmp_path / "native" / "finesub-correct")},
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


def test_the_package_engine_meets_the_protocol() -> None:
    instance = engine_host.PackageCorrectionEngine(PACKAGE)
    assert isinstance(instance, correction.CorrectionEngine)
    assert instance.spec.id == correction.DEFAULT_ENGINE
    assert instance.spec.label == "finesub correction"
    assert instance.spec.environment.components == () and instance.spec.environment.gpu == "none"


# --- readiness, from what the package declares ------------------------------------


class _Store:
    def __init__(self, llm_ready: bool) -> None:
        self.llm_ready = llm_ready

    def snapshot(self) -> dict:
        return {"llmReady": self.llm_ready}


class _Runtime:
    def __init__(self, state: str = "ready", tools: bool = True) -> None:
        self.state, self.tools = state, tools

    def status(self) -> dict:
        return {"runtime": {"state": self.state}, "models": []}

    def tool_path(self, name: str):  # noqa: ANN201
        return Path(f"{name}.exe") if self.tools else None


def _codes(store, runtime, target: str, *, missing=frozenset(), platform: str = "win32") -> list[str]:  # noqa: ANN001
    """The issue codes for `target`, with only `missing` absent from the launcher."""

    def find_spec(name, *_args, **_kwargs):  # noqa: ANN001, ANN202
        return None if name in missing else object()

    with (
        mock.patch("importlib.util.find_spec", side_effect=find_spec),
        mock.patch.object(engine_host.shutil, "which", return_value=None),
        mock.patch.object(engine_host.sys, "platform", platform),
    ):
        return [issue.code for issue in engine_host.launcher_engine_readiness(PACKAGE, runtime, store, target).issues]


def test_readiness_is_the_sidecars_llm_tiers() -> None:
    """WP5's `harness` and LLM tiers, now read from `PACKAGE.json` (codes and words unchanged)."""

    assert _codes(_Store(True), _Runtime(), "raw-srt") == [] and _codes(_Store(True), _Runtime(), "final-srt") == []
    # No LLM configured: only a translating target needs one.
    assert _codes(_Store(False), _Runtime(), "raw-srt") == []
    assert _codes(_Store(False), _Runtime(), "translated-srt") == ["missing_llm_key"]
    assert _codes(None, _Runtime(), "final-srt") == ["missing_llm_key"]
    # The launcher's harness is asked only until the managed runtime is ready.
    assert _codes(_Store(True), _Runtime(state="missing"), "raw-srt", missing={"opencc"}) == ["missing_harness"]
    assert _codes(_Store(True), _Runtime(state="missing"), "raw-srt", missing={"tzdata"}, platform="darwin") == []
    assert _codes(_Store(True), _Runtime(), "raw-srt", missing={"opencc"}) == []
    # The media tools: the runtime's or PATH's.
    assert _codes(_Store(True), _Runtime(tools=False), "raw-srt") == ["missing_ffmpeg"]


def test_the_harness_message_is_the_one_wp5_had() -> None:
    def find_spec(name, *_args, **_kwargs):  # noqa: ANN001, ANN202
        return None if name == "opencc" else object()

    with mock.patch("importlib.util.find_spec", side_effect=find_spec), \
            mock.patch.object(engine_host.shutil, "which", return_value="x"):
        issues = engine_host.launcher_engine_readiness(PACKAGE, _Runtime(state="missing"), _Store(True), "raw-srt").issues
    assert [issue.message for issue in issues] == [
        "本地续跑依赖缺失：opencc-python-reimplemented；请在「运行环境」页重建 Python 启动环境，或安装完整运行环境"
    ]


# --- a transcript.json with only lines, to raw-srt ---------------------------------

_LINES = [
    {"id": "l2", "start": 2.2, "end": 3.0, "text": "ございます", "speaker": "S2",
     "words": [{"text": "ございます", "start": 2.2, "end": 3.0}]},
    {"id": "l1", "start": 1.0, "end": 2.0, "text": "おはよう", "speaker": "S1",
     "words": [{"text": "おはよう", "start": 1.0, "end": 2.0}]},
    {"id": "l3", "start": 4.0, "end": 5.0, "text": "またね", "speaker": "S1"},
    {"id": "l4", "start": 5.5, "end": 6.0, "text": "うん"},
]

#: Run with nothing on the path but the correction package's own roots: no core, no other package.
_LINES_CHILD = textwrap.dedent(
    """
    import json
    import sys
    from pathlib import Path

    from finesub_correct.engine import FineSubCorrection

    root = Path(sys.argv[1])
    transcript = root / "task" / "transcript.json"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(sys.argv[2], encoding="utf-8")
    source = root / "media" / "节目.wav"
    source.parent.mkdir()
    source.write_bytes(b"")


    class Recorder:
        def __init__(self):
            self.events = []

        def __getattr__(self, method):
            def record(*args, **kwargs):
                if method == "stage_started":
                    self.events.append(["stage", args[0]])
            return record


    recorder = Recorder()
    logs = []
    job = {
        "transcription": {"transcript_json": str(transcript), "native": {}, "metadata": {}},
        "source": str(source), "target": "raw-srt", "task_dir": str(root / "task"), "options": {},
        "knowledge": {}, "diarization_level": "off", "title": "节目", "task_id": "t1", "task_summary": "",
    }
    result = FineSubCorrection(recorder, log=logs.append).run(job)
    native = root / "task" / "native" / "finesub-correct"
    print(json.dumps({
        "modules": sorted(name for name in ("torch", "numpy", "nonoka_x") if name in sys.modules),
        "artifacts": {name: Path(path).name for name, path in result["artifacts"].items()},
        "metadata": result["metadata"],
        "files": {path.name: path.read_text(encoding="utf-8") for path in sorted(native.iterdir())
                  if path.is_file() and not path.name.endswith("-metadata.json")},
        "events": recorder.events,
        "logs": logs,
    }, ensure_ascii=False))
    """
)


@pytest.fixture(scope="module")
def lines_run(tmp_path_factory: pytest.TempPathFactory) -> dict:
    from tests.quality_harness.hygiene import isolated_environment

    root = tmp_path_factory.mktemp("lines")
    environment, _removed = isolated_environment(root)
    environment.update({
        "PYTHONPATH": os.pathsep.join(str(path) for path in PACKAGE.paths),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TEMP": str(root / "tmp"),
        "TMP": str(root / "tmp"),
    })
    (root / "tmp").mkdir()
    (root / "runs").mkdir()
    transcript = TranscriptDocument.from_dict({
        "schema": 1, "engine": "mai2", "model": "mai-transcribe-2", "language": "ja", "duration": 6.5, "lines": _LINES,
    })
    from nonoka_x.transcription.model import dumps_transcript

    completed = subprocess.run(
        [sys.executable, "-c", _LINES_CHILD, str(root / "runs"), dumps_transcript(transcript)],
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
    assert lines_run["logs"] == ["纠错输入：由 transcript.json 转成的 stable，说话人来自各行标签"]
    stages = [stage for kind, stage in lines_run["events"] if kind == "stage"]
    assert stages == ["diarize", "raw-srt"]


# --- a source-text axis -----------------------------------------------------------


def test_blank_axis_rows_are_dropped_but_ids_stay_positional() -> None:
    rows = [{"t0": 1.0, "t1": 2.0, "ja": "おはよう"}, {"t0": 2.0, "t1": 3.0, "ja": " "},
            {"t0": 3.0, "t1": 4.0, "ja": "またね"}]
    assert engine.stable_from_source_rows(rows)["segments"] == [
        {"id": "1", "start": 1.0, "end": 2.0, "text": "おはよう"},
        {"id": "3", "start": 3.0, "end": 4.0, "text": "またね"},
    ]
    with pytest.raises(engine.NoSourceText, match="no source text"):
        engine.stable_from_source_rows([{"t0": 0.0, "t1": 1.0, "ja": " "}])
