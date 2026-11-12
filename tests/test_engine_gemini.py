"""Gemini 3.5 Transcribe as a transcription engine (`docs/transcription-engines-plan.md` WP3b).

Offline: WP1's recorded answers (`tests/fixtures/transcription/cases/gemini-*`, recorded through FineSub's
unmodified `speech/joint/` with repair off, as the first version runs) replayed through the engine, whose lines
must be WP1's line baselines (`plain`: the first version has no sentence-boundary model). The time repair, the
repetition check (a window split in halves), the early-stop continuation and the key switch on a spent day are
each tested on their own as well. Free keys only: the paid pool is never read.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from nonoka_x.transcription import ReadinessContext, TranscriptionEngine, audio, keys, read_transcript  # noqa: E402
from nonoka_x.transcription.engines import cloud, gemini  # noqa: E402
from test_engine_mai2 import (  # noqa: E402
    SOURCE,
    Recorder,
    Runtime,
    Store,
    baseline_lines,
    job_for,
    load,
    run_in_launcher,
)

FREE = [("one", "k1"), ("two", "k2"), ("three", "k3")]


@pytest.fixture
def store(tmp_path: Path):
    yield Store(tmp_path, {keys.GEMINI_FREE: list(FREE), "GEMINI_PAID": [("", "paid")]})
    keys.use_store(None)
    audio.use_runtime(None)


def replay(tmp_path: Path, store, monkeypatch, name: str, *, options=None):
    case, expected = load(name)
    monkeypatch.setattr(audio, "probe_media_duration", lambda path, **k: case["duration"])

    def refuse(*a, **k):
        raise AssertionError("an offline test must not call Gemini")

    monkeypatch.setattr(gemini.Caller, "__call__", refuse)
    report = Recorder()
    result = gemini.GeminiEngine(store, Runtime()).transcribe(job_for(tmp_path, name, options=options), report)
    return read_transcript(result.transcript_json), expected, report


# --- spec and readiness -------------------------------------------------------------------------------------


def test_the_spec_asks_for_the_free_pool_only() -> None:
    spec = gemini.SPEC
    assert isinstance(gemini.GeminiEngine(), TranscriptionEngine)
    assert (spec.id, spec.kind, spec.keys) == ("gemini-transcribe", "cloud-api", ("GEMINI_FREE",))
    assert spec.environment.components == ("media",) and not spec.environment.models
    assert "GEMINI_FREE" in spec.billing and "付费池" in spec.billing


def test_readiness_wants_a_free_key_and_ignores_the_paid_pool(tmp_path: Path) -> None:
    paid_only = Store(tmp_path, {"GEMINI_PAID": [("", "paid")]})
    issues = gemini.GeminiEngine(paid_only, Runtime()).readiness(ReadinessContext()).issues
    assert [i.code for i in issues] == ["missing_key"] and "GEMINI_FREE" in issues[0].message
    assert gemini.GeminiEngine(Store(tmp_path, {keys.GEMINI_FREE: FREE}), Runtime()).readiness(
        ReadinessContext()).ready
    assert [i.code for i in gemini.GeminiEngine(Store(tmp_path, {keys.GEMINI_FREE: FREE}),
                                                Runtime(media=False)).readiness(ReadinessContext()).issues] == [
        "missing_component"]


def test_without_a_free_key_the_run_stops_before_any_call(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(audio, "probe_media_duration", lambda path, **k: 700.0)
    with pytest.raises(cloud.CloudEngineError, match="GEMINI_FREE"):
        gemini.GeminiEngine(Store(tmp_path, {"GEMINI_PAID": [("", "paid")]}), Runtime()).transcribe(
            job_for(tmp_path, "gemini-windows"), Recorder())
    keys.use_store(None)


# --- runs against the baselines -----------------------------------------------------------------------------


def test_early_stops_continue_and_the_lines_are_the_baselines(tmp_path: Path, store, monkeypatch) -> None:
    """A 25-min source in 9-min windows; two windows stop early and are continued from 10 s before their last
    word; one bad time is fixed. Lines: FineSub's joint stage for 35t, without the sentence-boundary model."""
    document, expected, report = replay(tmp_path, store, monkeypatch, "gemini-windows")
    assert (document.engine, document.model, document.language) == ("gemini-transcribe", "gemini-3.5-transcribe", "ja")
    assert [line.to_dict() for line in document.lines] == baseline_lines(expected, "scale-1.0-plain")
    assert document.metadata["log"] == expected["transcript"]["log"]
    assert [e for e in document.metadata["log"] if "early_stop" in e] == [
        {"early_stop": [480.0, 1020.0], "last_word": 864.6, "next": 854.6},
        {"early_stop": [854.6, 1394.6], "last_word": 1360.3, "next": 1350.3}]
    assert document.metadata["repair"] == "off" and document.metadata["sentence_boundary_model"] is False
    assert report.warnings == []  # every window cached: nothing uploaded


@pytest.mark.parametrize("scale", [1.0, 1.3])
def test_a_repeating_window_is_split_and_the_lines_are_the_baselines(tmp_path: Path, store, monkeypatch,
                                                                     scale) -> None:
    """A window repeating one word 25 times is asked again as two halves; a window without speakers keeps its
    words unlabelled (`unknown`). Both length scales the baseline was recorded with."""
    document, expected, _ = replay(tmp_path, store, monkeypatch, "gemini-split-speakerless",
                                   options={"split_length_scale": scale})
    assert [line.to_dict() for line in document.lines] == baseline_lines(expected, f"scale-{scale}-plain")
    log = document.metadata["log"]
    assert log == expected["transcript"]["log"]
    assert log[0]["split"] == [0.0, 540.0] and log[0]["repeat"] == 25
    assert {"no_speaker_windows": 1} in log
    assert "unknown" in {line.speaker for line in document.lines}
    assert document.metadata["segment_split"]["length_scale"] == scale


# --- the repairs, one by one --------------------------------------------------------------------------------


def _w(start, end, text, speaker="A"):
    return {"start": start, "end": end, "text": text, "speaker": speaker}


def test_time_repair_shifts_a_run_back_between_matching_jumps() -> None:
    words = [_w(10.0, 10.3, "a"), _w(10.4, 10.7, "b"), _w(0.8, 1.1, "c"), _w(1.2, 1.5, "d"), _w(11.8, 12.1, "e")]
    # c and d jumped 9.9 s back; e jumps as far forward again: the run c-d is moved as a whole, by the jump plus
    # one median word (0.3 s), and lands between b and e.
    fixed, events = gemini.fix_times(words)
    assert [(w["start"], w["end"]) for w in fixed] == [(10.0, 10.3), (10.4, 10.7), (11.0, 11.3), (11.4, 11.7),
                                                       (11.8, 12.1)]
    assert [e["fix"] for e in events] == ["shift"] and events[0]["run"] == ["c", "d"] and events[0]["shift"] == 10.2


def test_time_repair_clamps_a_small_glitch_and_keeps_an_unmatched_jump() -> None:
    fixed, events = gemini.fix_times([_w(0.0, 0.3, "a"), _w(0.5, 0.4, "b")])
    assert fixed[1]["end"] == pytest.approx(0.51) and [e["fix"] for e in events] == ["clamp"]
    fixed, events = gemini.fix_times([_w(0.0, 0.3, "a"), _w(10.0, 10.3, "b"), _w(10.4, 10.7, "c")])
    assert fixed == [_w(0.0, 0.3, "a"), _w(10.0, 10.3, "b"), _w(10.4, 10.7, "c")] and events == []
    fixed, events = gemini.fix_times([_w(0.0, 0.3, "a"), _w(0.4, 9.0, "b")])  # a word over BAD_DUR
    assert fixed[1]["end"] == pytest.approx(0.7) and [e["fix"] for e in events] == ["word-keep-start"]


def test_repetition_is_counted_over_one_to_four_words() -> None:
    words = [_w(i * 0.2, i * 0.2 + 0.1, t) for i, t in enumerate(["x"] + ["は", "い"] * 21 + ["y"])]
    assert gemini.longest_repeat(words) == (21, "はい")
    assert gemini.longest_repeat([_w(0, 0.1, "a"), _w(0.2, 0.3, "b")])[0] == 1


def test_the_window_plan_splits_continues_and_finishes_the_tail(tmp_path: Path) -> None:
    """A fake 35t: the first window repeats itself (split in halves), the second stops early (continued from 10 s
    before its last word), and the last leaves a tail it is asked once more for."""
    asked: list[tuple[float, float]] = []

    def answer(start: float, end: float) -> dict:
        if (start, end) == (0.0, 540.0):
            words = [(1.0 + i * 0.3, "ね") for i in range(30)]
        elif start == 480.0:
            words = [(500.0 + i * 2.0, f"w{i}") for i in range(100)]  # last word near 698 s, window to 1020
        elif abs(start - 1168.2) < 0.05:
            words = [(start + 1.0 + i * 2.0, f"t{i}") for i in range(115)]  # the last window stops near 1400 s
        else:
            words = [(start + i * 2.0 + 1.0, f"v{i}") for i in range(int((end - start) // 2) - 1)]
        return {"status": "completed", "steps": [{"content": [{"annotations": [
            {"type": "word_info", "start_offset": f"{t - start:.2f}s", "end_offset": f"{t - start + 0.2:.2f}s",
             "speaker": "speaker_1", "text": text} for t, text in words]}]}]}

    def caller(clip, *, language, seconds):
        lo, hi = map(float, clip.parent.name.split("_"))
        asked.append((lo, hi))
        return answer(lo, hi)

    def clip(src, start, end, out, **k):
        path = Path(out).parent / f"{start}_{end}" / "w.mp3"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"mp3")
        return path

    original = gemini.extract_upload_mp3
    gemini.extract_upload_mp3 = clip
    try:
        transcript = gemini.transcribe(SOURCE, cache_root=tmp_path, language="ja", repair="off", caller=caller,
                                       duration=1600.0)
    finally:
        gemini.extract_upload_mp3 = original
    assert asked[:3] == [(0.0, 540.0), (0.0, 300.0), (240.0, 540.0)]
    log = transcript.log
    assert log[0]["split"] == [0.0, 540.0] and log[0]["repeat"] == 30
    (stop,) = [e for e in log if "early_stop" in e]
    assert stop["early_stop"] == [480.0, 1020.0] and stop["next"] == pytest.approx(stop["last_word"] - 10.0)
    (tail,) = [e for e in log if "tail" in e]
    assert tail["tail"][1] == 1600.0 and asked[-1] == (pytest.approx(tail["tail"][0] - 10.0), 1600.0)


def _clock(day: int):
    return lambda: datetime(2026, 11, day, 20, 0, tzinfo=timezone.utc)


def test_a_spent_day_moves_to_the_next_free_key_and_stays_spent(store, monkeypatch) -> None:
    used: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers.get("x-goog-api-key")
        if request.url.path.endswith("/files"):
            return httpx.Response(200, headers={"X-Goog-Upload-URL": "https://upload.test/session"})
        if request.url.host == "upload.test":
            return httpx.Response(200, json={"file": {"uri": "files/1"}})
        used.append(key)
        if key == "k1":
            return httpx.Response(429, json={"error": {"message": "quota",
                                                       "details": [{"quotaId": "RequestsPerDayPerProject"}]}})
        return httpx.Response(200, json={"status": "completed", "steps": []})

    monkeypatch.setattr(gemini, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    caller = gemini.GeminiEngine(store, Runtime()).caller()
    assert [e.key for e in caller.entries()] == ["k1", "k2"]  # the pool's first two, never the paid pool
    assert caller.ledger.path == store.state_file("cloud-asr-quota")
    caller.ledger._clock = _clock(11)
    caller._sleep = lambda s: None
    clip = store.root / "clip.mp3"
    clip.write_bytes(b"mp3")
    assert caller(clip, language="ja-JP", seconds=540.0) == {"status": "completed", "steps": []}
    assert caller(clip, language="ja-JP", seconds=540.0) == {"status": "completed", "steps": []}
    assert used == ["k1", "k2", "k2"]  # spent once, not asked again that day
    rows = json.loads(store.state_file("cloud-asr-quota").read_text(encoding="utf-8"))["keys"]
    assert rows["gemini-35t/gemini-3.5-transcribe/one"]["exhausted"] is True
    assert rows["gemini-35t/gemini-3.5-transcribe/two"]["calls"] == 2

    used.clear()

    def all_spent(request):
        if request.url.path.endswith("/files"):
            return httpx.Response(200, headers={"X-Goog-Upload-URL": "https://upload.test/session"})
        if request.url.host == "upload.test":
            return httpx.Response(200, json={"file": {"uri": "files/1"}})
        used.append(request.headers.get("x-goog-api-key"))
        return httpx.Response(429, json={"error": {"message": "You exceeded your current quota: requests per day"}})

    monkeypatch.setattr(gemini, "_client", lambda: httpx.Client(transport=httpx.MockTransport(all_spent)))
    with pytest.raises(keys.QuotaExhausted, match="GEMINI_FREE"):
        caller(clip, language="ja-JP", seconds=540.0)
    assert used == ["k2"] and "paid" not in used


def test_a_cancelled_task_makes_no_call(tmp_path: Path, store, monkeypatch) -> None:
    monkeypatch.setattr(audio, "probe_media_duration", lambda path, **k: 1500.0)
    asked = []
    monkeypatch.setattr(gemini.Caller, "__call__", lambda *a, **k: asked.append(a))
    with pytest.raises(cloud.EngineCancelled):
        gemini.GeminiEngine(store, Runtime()).transcribe(job_for(tmp_path, "gemini-windows", cached=False),
                                                         Recorder(cancelled=True))
    assert not asked


def test_the_engine_runs_where_only_the_launcher_is_installed(tmp_path: Path) -> None:
    run_in_launcher(tmp_path, module="gemini", engine="GeminiEngine", case="gemini-split-speakerless",
                    pools={"GEMINI_FREE": "k1"})
