"""ElevenLabs Scribe as a transcription engine (ported from FineSub 0.7.1's `speech/joint/scribe.py`).

Offline: a Scribe-shaped answer (`tests/fixtures/transcription/scribe/answer.json` -- character times, two
speakers, a bracketed note, an audio event, a word opening on a closing mark, a glued word) is replayed through
the engine, and its lines are held to what FineSub 0.7.1's unmodified `scribe.py` and `lines.py` make of the same
answer without the sentence-boundary model (`expected.json`). ffprobe and ffmpeg are stood in for; the key pool
is driven with a fake balance and a fake caller.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from nonoka_x.transcription import ReadinessContext, TranscriptionEngine, TranscriptionJob, audio, keys, read_transcript  # noqa: E402
from nonoka_x.transcription import lines as L  # noqa: E402
from nonoka_x.transcription.engines import scribe  # noqa: E402
from nonoka_x.transcription.reporting import reporting_to  # noqa: E402
from test_engine_mai2 import Recorder, Runtime, Store  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "transcription" / "scribe"
ANSWER = json.loads((FIXTURE / "answer.json").read_text(encoding="utf-8"))
EXPECTED = json.loads((FIXTURE / "expected.json").read_text(encoding="utf-8"))
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clean():
    scribe._REFUSED.clear()
    scribe._WARNED.clear()
    yield
    keys.use_store(None)
    audio.use_runtime(None)


def job(tmp_path: Path) -> TranscriptionJob:
    task = tmp_path / "task"
    task.mkdir(exist_ok=True)
    source = tmp_path / "source.m4a"
    source.write_bytes(b"not audio, never decoded: ffmpeg is stood in for")
    return TranscriptionJob(source=source, language="ja", task_dir=task, cache_dir=task / "cache",
                            target="final-srt", options={})


@pytest.fixture
def offline(monkeypatch):
    """ffprobe answers the fixture's duration; ffmpeg writes an empty FLAC; no balance can be read; the call
    answers the fixture (and counts itself)."""

    calls: list[dict] = []
    monkeypatch.setattr(audio, "probe_media_duration", lambda path, **k: EXPECTED["duration"])
    monkeypatch.setattr(scribe, "upload_flac", lambda source, out, span=None: (out.write_bytes(b""), out)[1])
    monkeypatch.setattr(scribe, "subscription", lambda key: ("no_permission", None))

    def answer(self, audio_path, *, language):
        calls.append({"key": self.key, "language": language})
        return json.loads(json.dumps(ANSWER))

    monkeypatch.setattr(scribe.Caller, "__call__", answer)
    return calls


# --- spec and readiness -------------------------------------------------------------------------------------


def test_the_spec_says_what_the_engine_needs() -> None:
    spec = scribe.SPEC
    assert isinstance(scribe.ScribeEngine(), TranscriptionEngine)
    assert (spec.id, spec.kind, spec.keys) == ("scribe", "cloud-api", ("ELEVENLABS",))
    assert spec.environment.components == ("media",) and not spec.environment.models
    assert spec.features.speakers and spec.features.word_timestamps


def test_readiness_wants_an_elevenlabs_key(tmp_path: Path) -> None:
    issues = scribe.ScribeEngine(Store(tmp_path, {}), Runtime()).readiness(ReadinessContext()).issues
    assert [i.code for i in issues] == ["missing_key"]
    ready = scribe.ScribeEngine(Store(tmp_path, {keys.ELEVENLABS: [("", "xi-1")]}), Runtime())
    assert ready.readiness(ReadinessContext()).ready


def test_the_pool_takes_the_first_three_keys(tmp_path: Path) -> None:
    store = Store(tmp_path, {keys.ELEVENLABS: [("", f"xi-{k}") for k in range(5)]})
    assert [entry.key for entry in keys.pool(keys.ELEVENLABS, store=store)] == ["xi-0", "xi-1", "xi-2"]


# --- the answer, as FineSub reads it -----------------------------------------------------------------------


def test_the_answer_reads_as_finesub_0_7_1_reads_it() -> None:
    words, glued = scribe.unglued(ANSWER["words"])
    units, events = scribe.answer_units(words)
    assert [[g.first, g.last, g.chars] for g in glued] == EXPECTED["glued"]
    assert events == EXPECTED["audio_events_dropped"] == 1
    assert units == EXPECTED["units"]
    assert scribe.collapses(words) == [tuple(c) for c in EXPECTED["collapses"]] == []
    assert not any("聞" in u["text"] for u in units)  # the bracketed note is gone


def test_a_timestamp_collapse_is_found() -> None:
    run = [{"type": "word", "text": "え", "start": 100.0 + k, "end": 100.0 + k, "speaker_id": "s"} for k in range(16)]
    assert scribe.collapses(run) == [(100.0, 115.0)]
    burst = [{**w, "start": 100.0 + k * 0.1, "end": 100.0 + k * 0.1} for k, w in enumerate(run)]
    assert scribe.collapses(burst) == []  # said at one point: not strung over COLLAPSE_SPAN


# --- the engine ---------------------------------------------------------------------------------------------


def test_the_engine_writes_finesub_0_7_1_s_lines(tmp_path: Path, offline) -> None:
    store = Store(tmp_path, {keys.ELEVENLABS: [("main", "xi-1")]})
    report = Recorder()
    result = scribe.ScribeEngine(store, Runtime()).transcribe(job(tmp_path), report)
    document = read_transcript(result.transcript_json)
    assert (document.engine, document.model, document.language) == ("scribe", "scribe_v3", "ja")
    got = [{"start": line.start, "end": line.end, "text": line.text, "speaker": line.speaker,
            "words": [{"text": w.text, "start": w.start, "end": w.end} for w in line.words or ()]}
           for line in document.lines]
    want = [{"start": s["start"], "end": s["end"], "text": s["text"], "speaker": s["line_speaker"],
             "words": [{"text": w["word"], "start": w["start"], "end": w["end"]} for w in s["words"]]}
            for s in EXPECTED["lines"]]
    assert got == want
    assert document.metadata["repair"] == "off" and document.metadata["edge_pad"] == [scribe.START_PAD, 0.0]
    assert document.metadata["log"] == [{"audio_events_dropped": 1},
                                        {"glued": EXPECTED["glued"][0][:2], "chars": EXPECTED["glued"][0][2],
                                         "via": "split"}]
    assert offline == [{"key": "xi-1", "language": "ja"}]
    codes = [code for code, _ in report.warnings]
    assert codes.count("cloud-asr-upload") == 1 and "scribe-glued" in codes and "cloud-asr-cost" in codes


def test_a_rerun_reads_the_cached_answer(tmp_path: Path, offline) -> None:
    store = Store(tmp_path, {keys.ELEVENLABS: [("", "xi-1")]})
    engine = scribe.ScribeEngine(store, Runtime())
    first = read_transcript(engine.transcribe(job(tmp_path), Recorder()).transcript_json)
    again = read_transcript(engine.transcribe(job(tmp_path), Recorder()).transcript_json)
    assert len(offline) == 1 and first.lines == again.lines
    assert (tmp_path / "task" / "cache" / "scribe" / "full.raw.json").is_file()


def test_a_cancelled_task_uploads_nothing(tmp_path: Path, offline) -> None:
    store = Store(tmp_path, {keys.ELEVENLABS: [("", "xi-1")]})
    with pytest.raises(Exception, match="cancelled"):
        scribe.ScribeEngine(store, Runtime()).transcribe(job(tmp_path), Recorder(cancelled=True))
    assert offline == []


def test_lines_start_earlier_and_end_where_they_ended() -> None:
    lines = [{"start": 1.0, "end": 2.0, "text": "a"}, {"start": 2.1, "end": 3.0, "text": "b"}]
    out = L.widened(lines, pad=scribe.START_PAD, end_pad=0.0, duration=10.0)
    assert [(line["start"], line["end"]) for line in out] == [(0.83, 2.0), (2.0, 3.0)]
    assert L.widened(lines, pad=0.1, duration=10.0)[0]["end"] == 2.1  # MAI2's: both edges by default


# --- the key pool -------------------------------------------------------------------------------------------


class Balances:
    """A fake subscription endpoint: each key's (tier, used, limit), and the calls it was asked."""

    def __init__(self, table: dict[str, tuple[str, int, int] | None]):
        self.table = table

    def __call__(self, key: str):
        row = self.table.get(key)
        if row is None:
            return "no_permission", None
        tier, used, limit = row
        return "ok", scribe.Balance(tier, used, limit, NOW + timedelta(days=3))


def pool(tmp_path: Path, labels: list[str], balances: Balances, answers: dict[str, object]):
    sent: list[str] = []

    def make_caller(key: str):
        def call(audio_path, *, language):
            sent.append(key)
            outcome = answers.get(key, {"words": []})
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return call

    entries = [keys.ApiKey(label, f"xi-{label}") for label in labels]
    return scribe.KeyPool(entries, state_path=tmp_path / "state" / "scribe.json", fetch=balances,
                          make_caller=make_caller, clock=lambda: NOW), sent


def test_a_spent_free_key_is_frozen_and_the_next_one_takes_the_call(tmp_path: Path) -> None:
    keys_pool, sent = pool(tmp_path, ["a", "b"], Balances({"xi-a": ("free", 9_990, 10_000), "xi-b": None}), {})
    with reporting_to(Recorder()):
        keys_pool(Path("x.flac"), language="ja", seconds=600.0)
    assert sent == ["xi-b"]
    assert keys_pool.frozen_until(keys.ApiKey("a", "xi-a")) == NOW + timedelta(days=3)
    sent.clear()
    with reporting_to(Recorder()):
        keys_pool(Path("x.flac"), language="ja", seconds=600.0)
    assert sent == ["xi-b"]  # still frozen: not asked its balance again


def test_a_key_short_of_the_call_is_passed_over_but_the_last_never_is(tmp_path: Path) -> None:
    table = {"xi-a": ("free", 0, 1_000), "xi-b": ("free", 9_000, 10_000)}  # a: ~27 min, b: ~27 min
    keys_pool, sent = pool(tmp_path, ["a", "b"], Balances(table), {})
    with reporting_to(Recorder()):
        keys_pool(Path("x.flac"), language="ja", seconds=3600.0)
    assert sent == ["xi-b"]


def test_a_refused_key_is_left_out_and_warned_about_once(tmp_path: Path) -> None:
    keys_pool, sent = pool(tmp_path, ["a", "b"], Balances({}), {"xi-a": scribe.ScribeRefused(401, "bad key")})
    report = Recorder()
    with reporting_to(report):
        keys_pool(Path("x.flac"), language="ja", seconds=60.0)
        keys_pool(Path("x.flac"), language="ja", seconds=60.0)
    assert sent == ["xi-a", "xi-b", "xi-b"]
    refused = [message for code, message in report.warnings if code == "scribe-key-refused"]
    assert len(refused) == 1 and "User: Read" in refused[0]


def test_the_last_key_refused_stops_the_run(tmp_path: Path) -> None:
    keys_pool, _ = pool(tmp_path, ["a"], Balances({}), {"xi-a": scribe.ScribeRefused(402, "no credits")})
    with reporting_to(Recorder()), pytest.raises(scribe.ScribeError, match="passed over or refused"):
        keys_pool(Path("x.flac"), language="ja", seconds=60.0)


def test_a_long_call_corrects_the_key_s_rate_within_the_clamp(tmp_path: Path) -> None:
    before, after = ("creator", 0, 100_000), ("creator", 1_500, 100_000)
    state = iter([before, after])
    keys_pool, _ = pool(tmp_path, ["a"], Balances({}), {})
    # The balance moves between the read before the call and the one after it.
    keys_pool._fetch = lambda key: ("ok", scribe.Balance(*next(state), None))
    with reporting_to(Recorder()):
        keys_pool(Path("x.flac"), language="ja", seconds=3600.0)
    entry = keys.ApiKey("a", "xi-a")
    assert keys_pool.rate(entry, scribe.Balance(*after, None)) == 1500.0
