"""MOSS Pro as a transcription engine (`docs/transcription-engines-plan.md` WP3c).

Offline: WP1's recorded answers (`tests/fixtures/transcription/cases/moss-*`, recorded through FineSub's
unmodified `speech/joint/` without a voiceprint model) replayed through the engine. The first version has no
Silero and no voiceprints (plan 4.1 item 6, owner 2026-11-12, WP3 questions 1-3):

- gaps are not re-requested: the engine's transcript is the baseline's less exactly the segments the gap
  re-request recovered, its log the baseline's less the gap entries;
- a continuation's speakers are new people (`c1:S01`), as in the baseline; across parts they are linked by
  overlapping time;
- unit times inside a segment are interpolated by weighted character count, which is what FineSub's S5b
  timing falls back to when Silero hears nothing (shown against `enhance.moss_timing` where numpy is there).

Segment-level stitching of parts, the output-cap continuation and the key pool's daily account each have
their own tests.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import shutil
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from nonoka_x.transcription import ReadinessContext, TranscriptionEngine, audio, keys, read_transcript  # noqa: E402
from nonoka_x.transcription import lines as L  # noqa: E402
from nonoka_x.transcription.engines import cloud, moss_pro  # noqa: E402
from nonoka_x.transcription.reporting import reporting_to  # noqa: E402
from nonoka_x.transcription.segmentation import dp as seg  # noqa: E402
from test_engine_mai2 import FIXTURES, SOURCE, Recorder, Runtime, Store, job_for, load, run_in_launcher  # noqa: E402

POOL = [("", "m1"), ("", "m2"), ("", "m3"), ("", "m4")]


@pytest.fixture
def store(tmp_path: Path):
    yield Store(tmp_path, {keys.MOSS_PRO: list(POOL)})
    keys.use_store(None)
    audio.use_runtime(None)


def refuse_calls(monkeypatch) -> list:
    asked = []

    def refuse(*a, **k):
        asked.append(a)
        raise AssertionError("an offline test must not call MOSS Pro")

    monkeypatch.setattr(moss_pro.Caller, "__call__", refuse)
    return asked


def run_engine(tmp_path: Path, store, monkeypatch, name: str, *, options=None, cache=None):
    case, expected = load(name)
    monkeypatch.setattr(audio, "probe_media_duration", lambda path, **k: case["duration"])
    asked = refuse_calls(monkeypatch)
    job = job_for(tmp_path, name, options=options, cached=cache is None)
    if cache is not None:
        shutil.copytree(cache, job.cache_dir)
    report = Recorder()
    result = moss_pro.MossProEngine(store, Runtime()).transcribe(job, report)
    assert not asked
    return read_transcript(result.transcript_json), expected, report


def baseline_units(expected: dict) -> list[dict]:
    return [u for turn in expected["transcript"]["turns"] for u in turn["units"]]


def without_gap_reasks(expected: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """(units, log, the dropped units) of the baseline without what its gap re-requests recovered."""
    log = expected["transcript"]["log"]
    units = baseline_units(expected)
    spans, relinks = [], 0  # each re-request with speakers is the next relink call: its labels are `c<k>:`
    for entry in log:
        relinks += "speakers" in entry
        if "gap" in entry and not entry.get("skipped"):
            spans.append((entry["gap"], f"c{relinks}:"))
    dropped = [u for u in units if any(lo <= u["start"] and u["end"] <= hi and tag in (u["speaker"] or "")
                                       for (lo, hi), tag in spans)]
    return units, [e for e in log if "gap" not in e], dropped


# --- spec and readiness -------------------------------------------------------------------------------------


def test_the_spec_says_what_the_engine_needs() -> None:
    spec = moss_pro.SPEC
    assert isinstance(moss_pro.MossProEngine(), TranscriptionEngine)
    assert (spec.id, spec.kind, spec.keys) == ("moss-pro", "cloud-api", ("MOSS_PRO",))
    assert spec.environment.components == ("media",) and not spec.environment.models
    assert spec.features.speakers and not spec.features.word_timestamps


def test_readiness_wants_a_moss_pro_key(tmp_path: Path) -> None:
    issues = moss_pro.MossProEngine(Store(tmp_path, {}), Runtime()).readiness(ReadinessContext()).issues
    assert [i.code for i in issues] == ["missing_key"]
    assert moss_pro.MossProEngine(Store(tmp_path, {keys.MOSS_PRO: POOL}), Runtime()).readiness(
        ReadinessContext()).ready


def test_the_paid_option_is_a_boolean_defaulting_on() -> None:
    assert moss_pro.paid_option({}) is True and moss_pro.paid_option({"paid": False}) is False
    with pytest.raises(cloud.CloudEngineError, match="paid"):
        moss_pro.paid_option({"paid": "no"})


# --- runs against the baselines -----------------------------------------------------------------------------


def test_parts_and_a_continuation_without_gap_reasks(tmp_path: Path, store, monkeypatch) -> None:
    """A 66-min source in two parts (1 h, 60 s overlap) stitched by segment; the first part hit the output cap
    and was continued; a gap Silero heard speech in was re-requested in the baseline, and is not here."""
    name = "moss-parts-continue-gap"
    _, expected = load(name)
    want, want_log, dropped = without_gap_reasks(expected)
    (gap,) = [e for e in expected["transcript"]["log"] if "gap" in e and not e.get("skipped")]
    assert len(dropped) == gap["recovered"] == 6

    got = moss_pro.transcribe(SOURCE, cache_root=FIXTURES / "cases" / name / "cache", language="zh", offline=True,
                              duration=4000.0, gaps=False, relink=moss_pro.NewSpeakers(),
                              caller=moss_pro.Caller(entries=[]))
    units = json.loads(json.dumps([u.as_dict() for u in got.units()]))
    assert units == [u for u in want if u not in dropped]  # the baseline less the re-request, nothing else moved
    assert got.log == want_log
    assert want_log[0]["speakers"] == {f"S0{k}": f"c1:S0{k}" for k in range(1, 5)}  # new people, as recorded
    assert want_log[-1] == {"parts": [[0.0, 3600.0], [3540.0, 4000.0]]}

    document, _, report = run_engine(tmp_path, store, monkeypatch, name)
    assert (document.engine, document.model, document.language) == ("moss-pro", "moss-transcribe-diarize-pro", "zh")
    assert document.metadata["log"] == want_log
    assert document.metadata["timing"] == "interpolated" and document.metadata["gap_reasks"] is False
    lines = L.segment_lines(L.interpolated_timed(units, duration=4000.0), duration=4000.0,
                            params=seg.DEFAULT_SPLIT_PARAMS, language="zh")[0]
    assert [line.to_dict() for line in document.lines] == [
        {"id": str(i + 1), "start": s["start"], "end": s["end"], "text": s["text"], "speaker": s["line_speaker"],
         "words": [{"text": w["word"], "start": w["start"], "end": w["end"]} for w in s["words"]]}
        for i, s in enumerate(lines)]
    assert any(line.speaker.endswith(":c1:S01") for line in document.lines)  # part 1's `w0:` before it
    assert report.warnings == [("moss-relink", "第一版不接声纹重连：MOSS Pro 续调回来的说话人记为新说话人；"
                                               "影响：同一个人可能被记成两个说话人")]
    assert any("空档不补问" in message for message, _ in report.logs)


def test_a_wrong_script_is_asked_again_once(tmp_path: Path, store, monkeypatch) -> None:
    name = "moss-wrong-script"
    document, expected, report = run_engine(tmp_path, store, monkeypatch, name, options={"split_length_scale": 1.3})
    assert document.metadata["log"] == expected["transcript"]["log"] == [{"wrong_script": [0.0, 600.0]}]
    want = baseline_units(expected)
    assert sum(len(line.text) for line in document.lines) <= sum(len(u["text"]) for u in want) + len(want)
    assert {line.speaker for line in document.lines} == {u["speaker"] for u in want}
    assert report.warnings == []

    # wrong twice: the run stops
    twice = tmp_path / "twice"
    shutil.copytree(FIXTURES / "cases" / name / "cache", twice)
    first = twice / "moss-pro" / "s-0.0-600.0.raw.json"
    shutil.copy(first, twice / "moss-pro" / "s-0.0-600.0-retry.raw.json")
    with pytest.raises(moss_pro.MossProError, match="another transcription engine"):
        run_engine(tmp_path / "again", store, monkeypatch, name, cache=twice)


# --- line timing without Silero -----------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["moss-parts-continue-gap", "moss-wrong-script"])
def test_interpolated_times_are_what_s5b_falls_back_to(name: str) -> None:
    """With Silero hearing nothing, FineSub's S5b timing interpolates every segment: `interpolated_timed` is that,
    on the baseline's segments (numpy, which `enhance.moss_timing` needs, is only here to show it)."""
    np = pytest.importorskip("numpy")
    from nonoka_x.transcription.enhance import moss_timing

    case, expected = load(name)
    units = baseline_units(expected)
    silent = np.zeros(int(case["duration"] / moss_timing.FRAME) + 1, np.float32)
    timed, _ = moss_timing.timed(units, silent, duration=case["duration"], params=seg.DEFAULT_SPLIT_PARAMS)
    assert L.interpolated_timed(units, duration=case["duration"]) == timed


def test_interpolation_shares_a_segment_by_weighted_characters() -> None:
    (segment,) = L.interpolated_timed([{"start": 1.0, "end": 4.0, "text": "你好 world", "speaker": "S01"},
                                       {"start": 9.0, "end": 9.5, "text": "  ", "speaker": "S02"}], duration=10.0)
    units = segment["units"]
    assert [u["text"] for u in units] == ["你", "好", "world"]
    assert units[0]["start"] == 1.0 and units[-1]["end"] == pytest.approx(4.0)
    assert all(a["end"] == pytest.approx(b["start"]) for a, b in zip(units, units[1:]))


# --- the continuation and the account -----------------------------------------------------------------------


def _answer(segments, tokens):
    return {"segments": [{"start": a, "end": b, "speaker": spk, "text": text} for a, b, spk, text in segments],
            "usage": {"completion_tokens": tokens}}


def test_an_answer_at_the_output_cap_is_continued_from_its_last_segment(tmp_path: Path) -> None:
    asked = []

    def caller(clip, *, seconds):
        lo, hi = map(float, clip.parent.name.split("_"))
        asked.append((lo, hi))
        if lo == 0.0:
            return _answer([(1.0, 5.0, "S01", "前半です"), (6.0, 200.0, "S02", "長い発言")], moss_pro.CAP_TOKENS)
        return _answer([(1.0, 3.0, "S01", "続きです"), (10.0, 20.0, "S02", "後半")], 300)

    def clip(src, start, end, out, **k):
        path = Path(out).parent / f"{start}_{end}" / "p.mp3"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"mp3")
        return path

    original = moss_pro.extract_upload_mp3
    moss_pro.extract_upload_mp3 = clip
    try:
        with reporting_to(Recorder()):
            got = moss_pro.transcribe(SOURCE, cache_root=tmp_path, language="ja", caller=caller, duration=600.0,
                                      gaps=False, relink=moss_pro.NewSpeakers())
    finally:
        moss_pro.extract_upload_mp3 = original
    assert asked == [(0.0, 600.0), (200.0, 600.0)]
    assert [(u.start, u.text, u.speaker) for u in got.units()] == [
        (1.0, "前半です", "S01"), (6.0, "長い発言", "S02"), (201.0, "続きです", "c1:S01"), (210.0, "後半", "c1:S02")]
    assert got.log == [{"continue": [200.0, 600.0], "recovered": 2, "tokens": 300,
                        "speakers": {"S01": "c1:S01", "S02": "c1:S02"}}]
    assert sorted(p.name for p in (tmp_path / "moss-pro").glob("*.raw.json")) == [
        "s-0.0-600.0.raw.json", "s-200.0-600.0.raw.json"]


def _clock(hour: int = 2):
    return lambda: datetime(2026, 11, 12, hour, 0, tzinfo=timezone.utc)


def test_the_account_moves_to_the_next_key_and_the_paid_key_goes_on(store, monkeypatch) -> None:
    used: list[str] = []
    refuse = {"m1"}

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers["Authorization"].removeprefix("Bearer ")
        used.append(key)
        if key in refuse:
            return httpx.Response(402, json={"error": "quota"})
        return httpx.Response(200, json=_answer([(0.0, 1.0, "S01", "はい")], 10))

    monkeypatch.setattr(moss_pro, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    engine = moss_pro.MossProEngine(store, Runtime())
    caller = engine.caller(paid=True)
    assert [e.key for e in caller.entries()] == ["m1", "m2", "m3"]  # the pool's first three
    assert caller.ledger.path == store.state_file("cloud-asr-quota")
    caller.ledger._clock = _clock()
    clip = store.root / "clip.mp3"
    clip.write_bytes(b"mp3")
    hour = 3600.0

    caller(clip, seconds=hour)  # m1 refuses (402): spent for the day, m2 takes the hour
    assert used == ["m1", "m2"] and caller.refused == ["m1"]
    used.clear()
    caller(clip, seconds=0.5 * hour)  # m2 has 1.2 h a day (counted x1.06): no room, m3
    assert used == ["m3"]
    used.clear()
    caller(clip, seconds=2 * hour)  # m3 is the paid last key: past its free hours it goes on
    assert used == ["m3"]
    rows = json.loads(store.state_file("cloud-asr-quota").read_text(encoding="utf-8"))["keys"]
    by_key = {k.rsplit("/", 1)[1]: v for k, v in rows.items()}
    m1, m2, m3 = (keys.ApiKey("", k, named=False).key_id for k in ("m1", "m2", "m3"))
    assert by_key[m1]["exhausted"] and by_key[m1]["calls"] == 0  # refunded, marked spent
    assert by_key[m2]["seconds"] == hour and by_key[m3]["seconds"] == 2.5 * hour

    free_only = engine.caller(paid=False)
    free_only.ledger._clock = _clock()
    used.clear()
    with pytest.raises(keys.QuotaExhausted, match="every key"):
        free_only(clip, seconds=hour)  # m2 full, m3 over its free hours: nothing is asked
    assert used == []

    refuse.add("m3")
    used.clear()
    with pytest.raises(keys.QuotaExhausted, match="paid last key"):
        caller(clip, seconds=0.5 * hour)  # no room on m2; the paid key refused: its balance is empty; not marked spent
    rows = json.loads(store.state_file("cloud-asr-quota").read_text(encoding="utf-8"))["keys"]
    assert not {k.rsplit("/", 1)[1]: v for k, v in rows.items()}[m3]["exhausted"]


def test_a_new_day_refreshes_the_account(store, monkeypatch) -> None:
    def handler(request):
        return httpx.Response(200, json=_answer([(0.0, 1.0, "S01", "はい")], 10))

    monkeypatch.setattr(moss_pro, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    caller = moss_pro.MossProEngine(store, Runtime()).caller(paid=False)
    clip = store.root / "clip.mp3"
    clip.write_bytes(b"mp3")
    caller.ledger._clock = _clock(2)  # 10:00 Beijing
    for _ in range(3):
        caller(clip, seconds=3600.0)
    with pytest.raises(keys.QuotaExhausted):
        caller(clip, seconds=3600.0)
    caller.ledger._clock = _clock(17)  # 01:00 Beijing the next day
    caller(clip, seconds=3600.0)


def test_a_cancelled_task_makes_no_call(tmp_path: Path, store, monkeypatch) -> None:
    monkeypatch.setattr(audio, "probe_media_duration", lambda path, **k: 600.0)
    asked = refuse_calls(monkeypatch)
    with pytest.raises(cloud.EngineCancelled):
        moss_pro.MossProEngine(store, Runtime()).transcribe(job_for(tmp_path, "moss-wrong-script", cached=False),
                                                            Recorder(cancelled=True))
    assert not asked


def test_the_engine_runs_where_only_the_launcher_is_installed(tmp_path: Path) -> None:
    run_in_launcher(tmp_path, module="moss_pro", engine="MossProEngine", case="moss-parts-continue-gap",
                    pools={"MOSS_PRO": "m1"})
