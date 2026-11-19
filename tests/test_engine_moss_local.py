"""Unit and integration tests for local MOSS 0.9B (`moss-local`)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from nonoka_x.transcription import (  # noqa: E402
    ReadinessContext,
    TranscriptionEngine,
    TranscriptionJob,
    audio,
    read_transcript,
)
from nonoka_x.transcription.engines import moss_local  # noqa: E402
from nonoka_x.transcription.engines.moss_local import rules as R  # noqa: E402
from nonoka_x.transcription.reporting import reporting_to  # noqa: E402
from nonoka_x.transcription.segmentation import dp as seg  # noqa: E402
from nonoka_x.transcription.units import Transcript, Unit, segment_turns  # noqa: E402
from test_engine_mai2 import Recorder, Runtime  # noqa: E402


class MossRuntime(Runtime):
    def __init__(
        self,
        *,
        media: bool = True,
        asr_runtime: bool = True,
        moss_model: bool = True,
        speaker_model: bool = True,
        worker_py: Path | None = None,
    ) -> None:
        super().__init__(media=media)
        self.asr_runtime = asr_runtime
        self.moss_model = moss_model
        self.speaker_model = speaker_model
        self._worker_py = worker_py

    def component_ready(self, component_id: str) -> bool:
        if component_id == "asr-runtime":
            return self.asr_runtime
        return super().component_ready(component_id)

    def model_dir(self, model_id: str) -> Path | None:
        if model_id == "moss-transcribe-diarize":
            return Path(__file__).resolve() if self.moss_model else None
        if model_id == "speaker-embedding":
            return Path(__file__).resolve() if self.speaker_model else None
        return None

    def model_ready(self, model_id: str) -> bool:
        return self.model_dir(model_id) is not None

    def worker_python(self) -> Path | None:
        return self._worker_py

    def worker_environment(self) -> dict[str, str]:
        import os

        return os.environ.copy()


def test_spec_and_contract() -> None:
    spec = moss_local.SPEC
    engine = moss_local.MossLocalEngine()
    assert isinstance(engine, TranscriptionEngine)
    assert (spec.id, spec.label, spec.kind) == ("moss-local", "MOSS 0.9B", "local-gpu")
    assert spec.keys == ()
    assert spec.environment.components == ("asr-runtime", "media")
    assert spec.environment.models == ("moss-transcribe-diarize", "speaker-embedding")
    assert spec.environment.gpu == "required"
    assert spec.features.speakers and not spec.features.word_timestamps and not spec.features.separate


def test_readiness_checks_components_models_and_gpu_mode(monkeypatch) -> None:
    monkeypatch.setattr(moss_local.sys, "platform", "win32")

    ready_rt = MossRuntime()
    assert moss_local.MossLocalEngine(runtime=ready_rt).readiness(ReadinessContext(device="cuda")).ready

    no_model_rt = MossRuntime(moss_model=False)
    issues = moss_local.MossLocalEngine(runtime=no_model_rt).readiness(ReadinessContext()).issues
    assert [i.code for i in issues] == ["missing_model"]

    no_asr_rt = MossRuntime(asr_runtime=False, moss_model=False)
    issues = moss_local.MossLocalEngine(runtime=no_asr_rt).readiness(ReadinessContext()).issues
    assert [i.code for i in issues] == ["missing_component", "missing_model"]

    cpu_issues = moss_local.MossLocalEngine(runtime=ready_rt).readiness(ReadinessContext(device="cpu")).issues
    assert [i.code for i in cpu_issues] == ["gpu_required"]

    tier_cpu_issues = moss_local.MossLocalEngine(runtime=ready_rt).readiness(
        ReadinessContext(options={"gpu_tier": "cpu"})
    ).issues
    assert [i.code for i in tier_cpu_issues] == ["gpu_required"]


def test_event_counts_aggregates_nested_rescue_events() -> None:
    events = [
        {"kind": "kv", "format": "turboquant-int4", "positions": 16384},
        {"kind": "loop", "lever": "penalty"},
        {
            "kind": "gap",
            "events": [
                {"kind": "kv", "format": "fp8", "positions": 4096},
                {"kind": "cap", "tokens": 2048},
                {"kind": "acoustic", "count": 2},
            ],
        },
        {
            "kind": "part",
            "events": [
                {"kind": "short_gap", "events": [{"kind": "loop", "lever": "sample"}]},
                {"kind": "tail_trim", "count": 3},
            ],
        },
    ]
    assert moss_local.event_counts(events) == {
        "acoustic": 1,
        "cap": 1,
        "gap": 1,
        "loop": 2,
        "short_gap": 1,
        "tail_trim": 1,
    }


def test_rules_pure_decisions() -> None:
    # 1. periodic token loop detection
    assert not R.periodic(list(range(60)))
    assert R.periodic([1, 2, 3, 4] * 15)

    # 2. wrong_language detection
    zh_text = "这是一段足够长的中文测试文本，用来验证语言检测规则不会把正常的中文识别结果误判成其他语言，共有四十个以上的汉字。"
    ja_text = "これは日本語のテストテキストです。ひらがなとカタカナがたくさん含まれているので、日本語として正しく判定されるはずです。"
    en_text = " ".join(["the", "quick", "brown", "fox", "is", "in", "the", "house", "and", "it", "was", "for", "you"] * 3)
    assert R.wrong_language(zh_text, "zh") is False
    assert R.wrong_language(ja_text, "zh") is True
    assert R.wrong_language(ja_text, "ja") is False
    assert R.wrong_language(zh_text, "ja") is True
    assert R.wrong_language(en_text, "en") is False

    # 3. gaps detection
    segs = [{"start": 5.0, "end": 12.0}, {"start": 50.0, "end": 60.0}]
    assert R.gaps(segs, 100.0, least=30.0) == [(12.0, 50.0), (60.0, 100.0)]

    # 4. tail trimming
    long_tail_segs = [
        {"start": 0.0, "end": 2.0, "speaker": "S01", "text": "正常语速的一句话测试"},
        {"start": 3.0, "end": 5.0, "speaker": "S01", "text": "第二句也是正常语速"},
        {"start": 6.0, "end": 20.0, "speaker": "S01", "text": "短句拖尾"},
    ]
    spans = [(0.0, 1.9), (3.0, 4.9), (6.0, 7.2)]
    trimmed_segs, trimmed_list = R.trim_tails(long_tail_segs, spans)
    assert len(trimmed_list) == 1
    assert trimmed_segs[2]["end"] < 20.0

    # 5. speaker label voiceprint merge
    merge_segs = [
        {"start": 0.0, "end": 5.0, "speaker": "S01", "text": "a"},
        {"start": 6.0, "end": 8.0, "speaker": "S02", "text": "b"},
    ]
    vec = np.array([1.0, 0.0], dtype=np.float32)
    merges = R.merge_labels(merge_segs, lambda s: {row["speaker"]: vec for row in s}, theta=0.8)
    assert merges == [("S02", "S01", 1.0)]
    assert [s["speaker"] for s in merge_segs] == ["S01", "S01"]


def test_build_lines_silero_s5b_and_fallback_interpolation(monkeypatch, tmp_path: Path) -> None:
    from nonoka_x.transcription.enhance import moss_timing

    units = [
        Unit(start=1.0, end=4.5, text="大家好，欢迎来到今天的节目。", speaker="S01"),
        Unit(start=5.0, end=8.0, text="谢谢邀请，很高兴和大家见面！", speaker="S02"),
    ]
    transcript = Transcript("moss-local", "moss-transcribe-diarize", 10.0, "segment", segment_turns(units))
    dummy_audio = tmp_path / "dummy.wav"
    dummy_audio.write_bytes(b"RIFF")

    monkeypatch.setattr(moss_local, "unavailable", lambda _: None)
    monkeypatch.setattr(
        audio,
        "decode_pcm16k",
        lambda path, sample_format="s16le": np.zeros(16000 * 10, dtype=np.int16).tobytes(),
    )
    probs = np.full(int(10.0 / moss_timing.FRAME) + 1, 0.8, dtype=np.float32)
    monkeypatch.setattr(moss_timing, "frame_probabilities", lambda pcm: probs)

    lines, meta = moss_local.build_lines(
        dummy_audio,
        transcript,
        duration=10.0,
        params=seg.DEFAULT_SPLIT_PARAMS,
        language="zh",
    )
    assert meta["timing"] == "silero-s5b"
    assert len(lines) >= 2
    assert {line["line_speaker"] for line in lines} == {"S01", "S02"}

    # Fallback to interpolated timing when Silero raises an error
    def broken_probs(_pcm):
        raise RuntimeError("silero unavailable in test")

    monkeypatch.setattr(moss_timing, "frame_probabilities", broken_probs)
    report = Recorder()
    with reporting_to(report):
        fallback_lines, fallback_meta = moss_local.build_lines(
            dummy_audio,
            transcript,
            duration=10.0,
            params=seg.DEFAULT_SPLIT_PARAMS,
            language="zh",
        )
    assert fallback_meta == {"timing": "interpolated"}
    assert len(fallback_lines) >= 2
    assert any(code == "moss-local-timing" for code, _ in report.warnings)


def test_engine_transcribe_in_process_writes_transcript_document(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "sample.wav"
    source.write_bytes(b"RIFF")
    task_dir = tmp_path / "task"
    task_dir.mkdir()

    units = [
        Unit(start=0.5, end=3.2, text="第一句本地MOSS转写测试。", speaker="S01"),
        Unit(start=3.6, end=6.8, text="第二句由另一位说话人回答。", speaker="S02"),
    ]
    fake_transcript = Transcript("moss-local", "moss-transcribe-diarize", 8.0, "segment", segment_turns(units))
    fake_record = {
        "revision": "628735fb22d6a0ee9a9fd8fe2ac1fbbe67be7e19",
        "kv": [{"format": "turboquant-int4", "positions": 16384}],
        "events": {"tail_trim": 1},
    }

    monkeypatch.setattr(moss_local, "missing_modules", lambda *names: ())
    monkeypatch.setattr(audio, "probe_media_duration", lambda path, **k: 8.0)
    monkeypatch.setattr(moss_local, "transcribe", lambda *a, **k: (fake_transcript, fake_record))
    monkeypatch.setattr(moss_local, "unavailable", lambda _: "no silero in unit test")

    job = TranscriptionJob(
        source=source,
        target="raw-srt",
        task_dir=task_dir,
        cache_dir=task_dir / "cache",
        language="zh",
        options={},
    )
    report = Recorder()
    engine = moss_local.MossLocalEngine(runtime=MossRuntime())
    result = engine.transcribe(job, report)

    assert result.transcript_json == task_dir / "transcript.json"
    doc = read_transcript(result.transcript_json)
    assert (doc.engine, doc.model, doc.language, doc.duration) == (
        "moss-local",
        "moss-transcribe-diarize",
        "zh",
        8.0,
    )
    assert [line.speaker for line in doc.lines] == ["S01", "S02"]
    assert [line.text for line in doc.lines] == ["第一句本地MOSS转写测试。", "第二句由另一位说话人回答。"]
    assert doc.metadata["revision"] == "628735fb22d6a0ee9a9fd8fe2ac1fbbe67be7e19"
    assert doc.metadata["timing"] == "interpolated"
    assert doc.metadata["events"] == {"tail_trim": 1}


def test_subprocess_reporter_protocol_forwards_stages_logs_and_errors(monkeypatch, tmp_path: Path) -> None:
    from nonoka_x.transcription.engines.moss_local import __main__ as moss_main

    emitted: list[dict[str, Any]] = []
    monkeypatch.setattr(moss_main, "_emit", lambda payload: emitted.append(dict(payload)))
    rep = moss_main._WireReporter()
    rep.stage("asr", "正在使用 MOSS 0.9B 本地识别")
    rep.warning("moss-local-timing", "回退插值", impact="均匀估算")
    rep.log("完成", {"lines": 2})

    assert [item["method"] for item in emitted] == ["stage", "warning", "log"]
    assert emitted[0]["args"] == ["asr", "正在使用 MOSS 0.9B 本地识别"]
    assert emitted[1]["args"] == ["moss-local-timing", "回退插值；影响：均匀估算"]
    assert emitted[1]["kwargs"] == {}

