"""How the WP1 behaviour baselines were recorded (kept for the record; it needs FineSub's `speech/joint/`).

Run once, on the code before WP1 moved `third_party/finesub/src/finesub/speech/joint/` into Nonoka
(`main@6027246`), from the repository root:

    PYTHONPATH=third_party/finesub/src D:\\Envir\\Python312\\python tests/fixtures/transcription/record_original.py

1. **Provider answers.** A synthetic conversation (seeded, `random.Random(SEED)`) is "heard" by three fake
   providers in FineSub's own answer formats: MAI2 over OpenRouter and Azure, Gemini 3.5 Transcribe, MOSS Pro.
   Each window gets its own speaker numbering and timing jitter, so the cross-window speaker link and the seams
   have real work to do; some windows are made to stop early, glitch in time, repeat themselves, hit MOSS Pro's
   output cap or answer in the wrong script. The unmodified `transcribe()` of each backend asks for them through
   its own `CallCache` (ffmpeg's clip cut is replaced by a file naming the clip's span), so the cache layout under
   `cases/<case>/cache/` is the backends' own.
2. **Replay.** Every case is then run again with `offline=True` from those caches -- what the tests do after the
   move -- and the transcript (turns, units, log), the reporter's warnings and the lines (production's DP through
   `lines.py`, per backend as `stage.py` builds them, with the derived VAD) are written to
   `expected/<case>.json`. The sentence DP is also recorded on its own (`expected/segmentation.json`).

Speech spans (the Silero gate) and frame probabilities (MOSS Pro's S5b timing) come with each case
(`case.json`), as Silero would have produced them; the voiceprint model is absent (`embed()` is None), so every
re-ask's unmatched label takes the no-model path.
"""

from __future__ import annotations

import json
import random
import shutil
import sys
from pathlib import Path

import numpy as np

from finesub.reporting import reporting_to
from finesub.speech.joint import gemini35t, lines as L, mai2, moss_timing, mosspro, stitch
from finesub.speech.postprocessing import segmentation as seg

HERE = Path(__file__).resolve().parent
CASES = HERE / "cases"
EXPECTED = HERE / "expected"
SOURCE = HERE / "source.bin"
SEED = 20261111

JA = ["今日はいい天気ですね。", "そうですね、散歩に行きましょうか。", "えっと、それはちょっと待ってください。",
      "昨日の配信、見ましたか？", "見ました！すごく面白かったです。", "あの、ゲームの話なんですけど。",
      "なるほど、そういうことか。", "じゃあ、次の質問に行きます。", "ありがとうございます、本当に嬉しいです。",
      "うん、うん。", "それで、最後どうなったんですか？", "いや、全然わからなかったです。"]
EN = ["Good morning, everyone.", "Thanks for having me today.", "So what happened next?",
      "I honestly have no idea, it was a mess.", "Let's move on to the next question.", "Right, right.",
      "That's a great point, actually.", "Wait, can you say that again?", "Okay, I think we're done here.",
      "No way, that's incredible!", "Well, it depends on the budget.", "Sure."]
ZH = ["大家好，欢迎来到今天的节目。", "这个问题我们之前讨论过。", "对，对，我同意你的看法。",
      "那么接下来我们聊聊游戏。", "其实我也不太清楚。", "好的，谢谢大家的收看！",
      "你说的那个版本是哪一年的？", "嗯，我想想。", "这个真的很有意思。", "我们下次再见。"]
TEXTS = {"ja": JA, "en": EN, "zh": ZH}


# --- the conversation ---------------------------------------------------------------------------------------


def tokens_of(text: str, lang: str) -> list[str]:
    if lang == "en":
        return text.split()
    out: list[str] = []
    for ch in text:
        if ch in "。、，！？,.!?" and out:
            out[-1] += ch
        else:
            out.append(ch)
    return out


def conversation(rng: random.Random, duration: float, lang: str, speakers: int, *, silent=(), overlap_share=0.0,
                 sparse=0.0):
    """Utterances {speaker, start, end, text, words:[{start,end,text}]} until `duration`, none starting in a
    `silent` span. `overlap_share` of them start before the previous one ended (another speaker); after `sparse`
    of them nobody talks for 15-60 s (keeps the long cases' fixtures small)."""
    out, t = [], rng.uniform(0.3, 2.0)
    while t < duration - 3:
        if any(a <= t < b for a, b in silent):
            t = next(b for a, b in silent if a <= t < b) + rng.uniform(0.2, 1.0)
            continue
        speaker = rng.randrange(speakers)
        start = t
        if out and rng.random() < overlap_share:
            speaker = (out[-1]["speaker"] + 1 + rng.randrange(speakers - 1)) % speakers
            start = max(out[-1]["start"] + 0.3, out[-1]["end"] - rng.uniform(0.3, 1.5))
        text = rng.choice(TEXTS[lang])
        words, at = [], start
        per = 0.32 if lang == "en" else 0.14
        for tok in tokens_of(text, lang):
            d = per * (0.7 + 0.6 * rng.random()) * (1 + 0.15 * len(tok) if lang == "en" else 1)
            words.append({"start": round(at, 3), "end": round(at + d, 3), "text": tok})
            at += d + (rng.uniform(0.0, 0.08) if rng.random() < 0.8 else rng.uniform(0.2, 0.6))
        end = words[-1]["end"]
        if end >= duration:
            break
        out.append({"speaker": speaker, "start": start, "end": end, "text": text, "words": words})
        gap = rng.uniform(0.15, 1.2) if rng.random() < 0.85 else rng.uniform(1.5, 6.0)
        if sparse and rng.random() < sparse:
            gap = rng.uniform(15.0, 60.0)
        t = max(t, end) + gap
    return out


def speech_spans(utts) -> list[list[float]]:
    spans = sorted([u["start"], u["end"]] for u in utts)
    merged: list[list[float]] = []
    for a, b in spans:
        if merged and a - merged[-1][1] < 0.25:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [[round(a, 3), round(b, 3)] for a, b in merged]


def probabilities_rle(spans, duration: float) -> list[list[float]]:
    """Silero-like probabilities per 32 ms frame, run-length coded [[value, frames], ...]."""
    frames = int(duration * 16000) // moss_timing.HOP
    p = np.full(frames, 0.03, np.float32)
    for a, b in spans:
        i, j = int(a / moss_timing.FRAME), int(b / moss_timing.FRAME)
        p[i:j] = 0.92
        if i > 0:
            p[i - 1] = 0.4
        if j < frames:
            p[j] = 0.38
    out: list[list[float]] = []
    for v in p.tolist():
        if out and out[-1][0] == v:
            out[-1][1] += 1
        else:
            out.append([v, 1])
    return out


def probabilities(rle) -> np.ndarray:
    return np.concatenate([np.full(int(n), v, np.float32) for v, n in rle]) if rle else np.zeros(0, np.float32)


# --- the fake providers -------------------------------------------------------------------------------------


def clip_span(path: Path) -> tuple[float, float]:
    return tuple(json.loads(Path(path).read_text(encoding="utf-8")))


def fake_extract(src, start, end, out):
    out = Path(out)
    out.write_text(json.dumps([round(float(start), 3), round(float(end), 3)]), encoding="utf-8")
    return out


def window_view(utts, lo: float, hi: float, *, seed: str, labels: int):
    """The words a window hears: (utterance, words) with times relative to `lo`, jittered per window, and the
    window's own speaker numbering."""
    r = random.Random(f"{SEED}:{seed}:{lo:.1f}")
    perm = list(range(labels))
    r.shuffle(perm)
    jitter = r.uniform(-0.06, 0.06)
    out = []
    for u in utts:
        ws = []
        for w in u["words"]:
            if lo <= (w["start"] + w["end"]) / 2 < hi:
                ws.append({"start": round(max(0.0, w["start"] - lo + jitter), 3),
                           "end": round(max(0.0, w["end"] - lo + jitter), 3), "text": w["text"]})
        if ws:
            out.append((perm[u["speaker"]], u, ws))
    return out


class Mai2Fake:
    """MAI2: integer speakers per window; the first pass (the planned windows) misses the `drop` spans."""

    def __init__(self, utts, route: str, *, planned, drop=(), labels=4):
        self.utts, self.route, self.planned, self.drop, self.labels = utts, route, planned, drop, labels
        self.calls: list[list[float]] = []

    def __call__(self, clip, *, locale):
        lo, hi = clip_span(clip)
        self.calls.append([lo, hi])
        view = window_view(self.utts, lo, hi, seed=f"mai2-{self.route}", labels=self.labels)
        if [lo, hi] in self.planned:
            view = [(s, u, ws) for s, u, ws in view if not any(a <= u["start"] < b for a, b in self.drop)]
        if self.route == "openrouter":
            return {"text": "...", "words": [{"word": w["text"], "start": w["start"], "end": w["end"], "speaker": s}
                                             for s, _, ws in view for w in ws]}
        phrases = []
        for s, _, ws in view:
            phrases.append({"speaker": s, "offsetMilliseconds": 0, "durationMilliseconds": 0,
                            "words": [{"text": w["text"], "offsetMilliseconds": round(w["start"] * 1000),
                                       "durationMilliseconds": round((w["end"] - w["start"]) * 1000)} for w in ws]})
        return {"durationMilliseconds": round((hi - lo) * 1000), "phrases": phrases}


class GeminiFake:
    """35t: speaker strings, a 100 ms grid, output in utterance order (overlaps keep turns whole)."""

    def __init__(self, utts, *, early_stop=None, glitch=None, repeat=None, no_speaker=None, labels=4):
        self.utts, self.early_stop, self.glitch = utts, early_stop or {}, glitch
        self.repeat, self.no_speaker, self.labels = repeat, no_speaker, labels
        self.calls: list[list[float]] = []

    def __call__(self, clip, *, language, seconds):
        lo, hi = clip_span(clip)
        self.calls.append([lo, hi])
        if self.repeat is not None and [lo, hi] == self.repeat:
            anns = [{"type": "word_info", "start_offset": f"{1.0 + 0.4 * k:.1f}s", "end_offset": f"{1.3 + 0.4 * k:.1f}s",
                     "speaker": "speaker_1", "text": "はい"} for k in range(25)]
            return {"status": "completed", "steps": [{"content": [{"annotations": anns}]}]}
        stop = self.early_stop.get(f"{lo:.1f}")
        anns = []
        for s, u, ws in window_view(self.utts, lo, hi, seed="35t", labels=self.labels):
            if stop is not None and u["start"] >= stop:
                continue
            for k, w in enumerate(ws):
                a, b = round(w["start"], 1), round(w["end"], 1)
                if (self.glitch is not None and f"{lo:.1f}" == self.glitch[0] and u is self.utts[self.glitch[1]]
                        and 2 <= k < len(ws) - 1):
                    a, b = round(a + 8.0, 1), round(b + 8.0, 1)  # a run thrown 8 s late; the last word back
                speaker = None if self.no_speaker == f"{lo:.1f}" else f"speaker_{s + 1}"
                anns.append({"type": "word_info", "start_offset": f"{a:.1f}s", "end_offset": f"{b:.1f}s",
                             "speaker": speaker, "text": w["text"]})
        return {"status": "completed", "steps": [{"content": [{"type": "text", "annotations": anns}]}]}


class MossFake:
    """MOSS Pro: one segment per utterance, `S0<k>` per call; the output cap, a missed span, the wrong script."""

    def __init__(self, utts, *, planned, cap_at=None, missed=(), wrong_script=None, labels=4):
        self.utts, self.planned, self.cap_at, self.missed, self.wrong = utts, planned, cap_at, missed, wrong_script
        self.labels = labels
        self.calls: list[list[float]] = []
        self._seen: dict[tuple, int] = {}

    def __call__(self, clip, *, seconds):
        lo, hi = clip_span(clip)
        n = self._seen.get((lo, hi), 0)
        self._seen[(lo, hi)] = n + 1
        self.calls.append([lo, hi])
        segs, tokens = [], 1200
        first = n == 0 and [lo, hi] in self.planned
        for s, u, ws in window_view(self.utts, lo, hi, seed=f"moss-{n}", labels=self.labels):
            if first and any(a <= u["start"] < b for a, b in self.missed):
                continue
            if first and self.cap_at is not None and lo <= self.cap_at[0] < hi and u["start"] >= self.cap_at[1]:
                tokens = 16200
                continue
            text = u["text"]
            if self.wrong is not None and f"{lo:.1f}" == self.wrong and n == 0:
                text = "This is a translated line."
            segs.append({"start": ws[0]["start"], "end": ws[-1]["end"], "speaker": f"S{s + 1:02d}", "text": text})
        return {"status": "SUCCESS", "result": {"text": "".join(x["text"] for x in segs), "segments": segs},
                "usage": {"completion_tokens": tokens}}


class NoVoiceprints:
    error = "no voiceprint model in the baseline"

    def embed(self):
        return None


class Recorder:
    def __init__(self):
        self.events: list[list] = []

    def warning(self, code, message, *, impact="", action=""):
        self.events.append(["warning", code, message, impact])

    def debug(self, message, fields=None):
        pass

    def __getattr__(self, name):
        return lambda *a, **k: None


# --- the cases ----------------------------------------------------------------------------------------------


def build_cases() -> dict[str, dict]:
    rng = random.Random(SEED)
    cases: dict[str, dict] = {}

    utts = conversation(rng, 2100.0, "ja", 3, overlap_share=0.05, sparse=0.3)
    cases["mai2-openrouter-windows"] = {"backend": "mai2", "via": "openrouter", "language": "ja",
                                        "duration": 2100.0, "repair": "off", "utts": utts, "scales": [1.0]}

    utts = conversation(rng, 900.0, "en", 3, silent=[(400.0, 430.0)])
    cases["mai2-azure-repair"] = {"backend": "mai2", "via": "azure", "language": "en", "duration": 900.0,
                                  "repair": "on", "utts": utts, "drop": [[300.0, 326.0]], "scales": [1.0, 1.3]}

    utts = conversation(rng, 1500.0, "ja", 3, overlap_share=0.12, sparse=0.15)
    # the 2nd window ([480, 1020]) stops early; a run of one utterance's words in the 1st is thrown 8 s late
    glitch = next(i for i, u in enumerate(utts) if 100 < u["start"] < 300 and len(u["words"]) >= 6)
    cases["gemini-windows"] = {"backend": "gemini-35t", "language": "ja", "duration": 1500.0, "repair": "off",
                               "utts": utts, "early_stop": {"480.0": 900.0}, "glitch": ["0.0", glitch],
                               "no_speaker": None, "scales": [1.0]}

    utts = conversation(rng, 700.0, "en", 2, overlap_share=0.1)
    cases["gemini-split-speakerless"] = {"backend": "gemini-35t", "language": "en", "duration": 700.0,
                                         "repair": "off", "utts": utts, "repeat": [0.0, 540.0],
                                         "no_speaker": "240.0", "scales": [1.0, 1.3]}

    utts = conversation(rng, 4000.0, "zh", 4, silent=[(2000.0, 2045.0)], overlap_share=0.1, sparse=0.4)
    cases["moss-parts-continue-gap"] = {"backend": "moss-pro", "language": "zh", "duration": 4000.0, "utts": utts,
                                        "cap_at": [1.0, 3000.0], "missed": [[1500.0, 1550.0]], "scales": [1.0]}

    utts = conversation(rng, 600.0, "ja", 2, overlap_share=0.15)
    cases["moss-wrong-script"] = {"backend": "moss-pro", "language": "ja", "duration": 600.0, "utts": utts,
                                  "wrong_script": "0.0", "scales": [1.0, 1.3]}
    return cases


def run_case(name: str, case: dict, *, offline: bool):
    cache_root = CASES / name / "cache"
    utts, duration, language = case["utts"], case["duration"], case["language"]
    spans = speech_spans(utts)
    speech = mosspro.SpeechSpans(SOURCE, spans=[tuple(s) for s in spans], pcm=np.zeros(16, np.int16))
    kwargs = dict(cache_root=cache_root, language=language, duration=duration, offline=offline)
    if case["backend"] == "mai2":
        planned = [[lo, hi] for lo, hi in stitch.windows(0.0, duration, mai2.WINDOW[case["via"]], mai2.OVERLAP)]
        caller = Mai2Fake(utts, case["via"], planned=planned, drop=case.get("drop", ()))
        return mai2.transcribe(SOURCE, via=case["via"], repair=case["repair"], caller=caller, speech=speech,
                               voiceprints=NoVoiceprints(), **kwargs), caller
    if case["backend"] == "gemini-35t":
        caller = GeminiFake(utts, early_stop=case.get("early_stop"), glitch=case.get("glitch"),
                            repeat=case.get("repeat"), no_speaker=case.get("no_speaker"))
        return gemini35t.transcribe(SOURCE, repair=case["repair"], caller=caller, speech=speech,
                                    voiceprints=NoVoiceprints(), **kwargs), caller
    planned = [[lo, hi] for lo, hi in stitch.windows(0.0, duration, mosspro.PART_SECONDS, mosspro.PART_OVERLAP)]
    caller = MossFake(utts, planned=planned, cap_at=case.get("cap_at"), missed=case.get("missed", ()),
                      wrong_script=case.get("wrong_script"))
    return mosspro.transcribe(SOURCE, caller=caller, speech=speech, voiceprints=NoVoiceprints(), **kwargs), caller


def transcript_dict(t) -> dict:
    return {"provider": t.provider, "model": t.model, "duration": t.duration, "unit": t.unit,
            "turns": [{"speaker": turn.speaker, "units": [u.as_dict() for u in turn.units]} for turn in t.turns],
            "log": t.log}


def fake_sentence_ends(words):
    for w in words:
        w[seg.SENTENCE_END_BONUS_KEY] = 0.9 if w["word"][-1:] in "。.?？!！" else 0.05


def fake_align(long_ones, every):
    """S6b's shape: per index into `long_ones`, units -- here the plain interpolation of every other one."""
    return {k: [dict(u, start=round(u["start"], 3), end=round(u["end"], 3)) for u in L.interpolated(s)]
            for k, s in enumerate(long_ones) if k % 2 == 0}


def lines_of(case: dict, transcript, case_json: dict) -> dict:
    """The lines `stage.run` builds for the backend (and a few variants)."""
    units = [u.as_dict() for u in transcript.units()]
    duration, language = case["duration"], case["language"]
    out = {}
    for scale in case["scales"]:
        params = seg.split_params_for_length_scale(scale)
        if case["backend"] == "mai2":
            segments, vad = L.word_lines(units, duration=duration, params=params, language=language)
            segments = L.widened(segments, pad=L.MAI2_EDGE_PAD, duration=duration)
            vad = L.derived_vad(sorted((w for s in segments for w in s.get("words") or []), key=lambda w: w["start"]))
            out[f"scale-{scale}"] = {"segments": segments, "vad": vad}
        elif case["backend"] == "gemini-35t":
            for label, ends in (("plain", None), ("sbd", fake_sentence_ends)):
                segments, vad = L.word_lines(units, duration=duration, params=params, language=language,
                                             per_turn=True, grid=True, sentence_ends=ends)
                out[f"scale-{scale}-{label}"] = {"segments": segments, "vad": vad}
        else:
            probs = probabilities(case_json["probabilities"])
            for label, align in (("s5b", None), ("s6b", fake_align)):
                timed, counts = moss_timing.timed(units, probs, duration=duration, params=params, align=align)
                segments, vad = L.segment_lines(timed, duration=duration, params=params, language=language)
                out[f"scale-{scale}-{label}"] = {"counts": counts, "segments": segments, "vad": vad}
    return out


# --- the sentence DP on its own -----------------------------------------------------------------------------


def dp_inputs(rng: random.Random) -> list[dict]:
    """Whisper-shaped input segments with words, a VAD, and the parameters; ja, en, zh; with gaps, a long run
    without punctuation, and one segment carrying sentence-end bonuses."""
    cases = []
    for k, (lang, n) in enumerate((("ja", 25), ("en", 25), ("zh", 20), ("ja", 12))):
        utts = conversation(random.Random(f"{SEED}:dp:{k}"), 400.0, lang, 2)[:n]
        words = []
        for u in utts:
            for i, w in enumerate(u["words"]):
                text = w["text"] if k != 3 else w["text"].strip("。、，！？,.!?")
                words.append({"start": w["start"], "end": w["end"], "word": text,
                              "space_before": lang == "en" and bool(words), "speaker": u["speaker"],
                              "seam": i == 0})
        if k == 3:  # no punctuation: the DP has only time and length to go on
            for w in words:
                w[seg.SENTENCE_END_BONUS_KEY] = round(rng.random(), 3)
        segs = L._input_segments(words)
        vad = L.derived_vad(sorted(words, key=lambda w: w["start"]))
        for scale in (0.8, 1.0, 1.5):
            cases.append({"language": lang, "scale": scale, "segments": segs, "vad": vad})
    return cases


def record_segmentation() -> dict:
    rng = random.Random(f"{SEED}:dp")
    out = []
    for item in dp_inputs(rng):
        params = seg.split_params_for_length_scale(item["scale"])
        result = seg.split_segments(json.loads(json.dumps(item["segments"])), item["vad"], params=params)
        out.append({**item, "params": seg.split_params_metadata(params), "result": result})
    return {"cases": out}


def dumps(data) -> str:
    """One JSON line per top-level item: compact, still diffable by item."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def main() -> int:
    SOURCE.write_bytes(bytes(range(256)) * 16)
    for directory in (CASES, EXPECTED):
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)
    import finesub.speech.joint.gemini35t as g
    import finesub.speech.joint.mai2 as m
    import finesub.speech.joint.mosspro as p

    g.extract_upload_mp3 = m.extract_upload_mp3 = p.extract_upload_mp3 = fake_extract
    for name, case in build_cases().items():
        spans = speech_spans(case["utts"])
        case_json = {k: v for k, v in case.items() if k != "utts"}
        case_json["speech_spans"] = spans
        if case["backend"] == "moss-pro":
            case_json["probabilities"] = probabilities_rle(spans, case["duration"])
        (CASES / name).mkdir(parents=True)
        (CASES / name / "case.json").write_text(json.dumps(case_json, ensure_ascii=False, indent=1), encoding="utf-8")
        with reporting_to(Recorder()):
            _, caller = run_case(name, case, offline=False)  # 1. ask the fake provider, through the cache
        recorder = Recorder()
        with reporting_to(recorder):  # 2. replay, as the tests will
            transcript, _ = run_case(name, case, offline=True)
            lines = lines_of(case, transcript, case_json)
        expected = {"calls": caller.calls, "transcript": transcript_dict(transcript), "events": recorder.events,
                    "lines": lines}
        (EXPECTED / f"{name}.json").write_text(dumps(expected), encoding="utf-8")
        print(name, len(caller.calls), "calls", sum(len(t["units"]) for t in expected["transcript"]["turns"]),
              "units", {k: len(v["segments"]) for k, v in lines.items()})
    (EXPECTED / "segmentation.json").write_text(dumps(record_segmentation()), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
