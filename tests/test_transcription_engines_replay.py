"""The moved cloud backends replay their recorded answers into the transcripts FineSub produced (WP1).

Every case under `tests/fixtures/transcription/cases/` holds the raw provider answers in each backend's own cache
layout, recorded through FineSub's unmodified `speech/joint/` (`record_original.py`). Replaying them offline
through `nonoka_x.transcription.engines` -- stitching of the windows, the cross-window speaker link, MAI2's
repair, 35t's window planning and time fixes, MOSS Pro's continuations, gap re-asks and the wrong-script retry --
must give the same turns, units and log, and the same warnings. No network, no ffmpeg, no model: the Silero gate
is the case's recorded speech spans and the voiceprint model is absent, as when the baseline was recorded.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nonoka_x.transcription.engines import gemini, mai2, moss_pro  # noqa: E402
from nonoka_x.transcription.reporting import compose, reporting_to  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "transcription"
SOURCE = FIXTURES / "source.bin"
CASES = sorted(p.name for p in (FIXTURES / "cases").iterdir() if (p / "case.json").is_file())


class NoVoiceprints:
    error = "no voiceprint model in the baseline"

    def embed(self):
        return None


class Recorder:
    """A WP0 `Reporter` that keeps the warnings."""

    def __init__(self) -> None:
        self.warnings: list[list[str]] = []

    def warning(self, code: str, message: str) -> None:
        self.warnings.append([code, message])

    def log(self, message, fields=None) -> None:
        pass

    def stage(self, *a, **k) -> None:
        pass

    def progress(self, *a, **k) -> None:
        pass

    def cancelled(self) -> bool:
        return False


def no_calls(*args, **kwargs):
    raise AssertionError("a replay must not call the provider")


class Speech:
    """The Silero gate (`moss_pro.SpeechSpans`) with the case's recorded spans."""

    def __init__(self, spans):
        self._inner = moss_pro.SpeechSpans(SOURCE, spans=[tuple(s) for s in spans], pcm=b"")

    def pcm(self):
        np = pytest.importorskip("numpy")
        return np.zeros(16, np.int16)

    def voiced(self, a, b):
        return self._inner.voiced(a, b)


def replay(name: str):
    case = json.loads((FIXTURES / "cases" / name / "case.json").read_text(encoding="utf-8"))
    kwargs = dict(cache_root=FIXTURES / "cases" / name / "cache", language=case["language"],
                  duration=case["duration"], offline=True, speech=Speech(case["speech_spans"]),
                  voiceprints=NoVoiceprints())
    if case["backend"] == "mai2":
        return mai2.transcribe(SOURCE, via=case["via"], repair=case["repair"], caller=no_calls, **kwargs)
    if case["backend"] == "gemini-35t":
        return gemini.transcribe(SOURCE, repair=case["repair"], caller=no_calls, **kwargs)
    return moss_pro.transcribe(SOURCE, caller=no_calls, **kwargs)


def as_dict(t) -> dict:
    return json.loads(json.dumps({
        "provider": t.provider, "model": t.model, "duration": t.duration, "unit": t.unit,
        "turns": [{"speaker": turn.speaker, "units": [u.as_dict() for u in turn.units]} for turn in t.turns],
        "log": t.log}))


@pytest.mark.parametrize("name", CASES)
def test_replay_matches_the_baseline(name: str) -> None:
    expected = json.loads((FIXTURES / "expected" / f"{name}.json").read_text(encoding="utf-8"))
    recorder = Recorder()
    with reporting_to(recorder):
        transcript = replay(name)
    got = as_dict(transcript)
    want = expected["transcript"]
    assert got["log"] == want["log"], f"{name}: the backend's log differs"
    assert [t["speaker"] for t in got["turns"]] == [t["speaker"] for t in want["turns"]], f"{name}: turns differ"
    assert got == want, f"{name}: units differ from the baseline"
    assert recorder.warnings == [[code, compose(message, impact=impact)] for _, code, message, impact in expected["events"]]


def test_the_cases_cover_what_the_baseline_is_for() -> None:
    logs = {name: json.loads((FIXTURES / "expected" / f"{name}.json").read_text(encoding="utf-8"))["transcript"]["log"]
            for name in CASES}
    flat = json.dumps(logs)
    for marker in ('"windows"', '"early_stop"', '"split"', '"no_speaker_windows"', '"continue"', '"gap"',
                   '"wrong_script"', '"parts"', '"repair": "miss"', '"fix": "unmatched"', '"fix": "clamp"'):
        assert marker in flat, marker
