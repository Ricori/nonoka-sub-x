"""The cloud engines' line building gives what it gave inside FineSub (WP1).

For every recorded case, the transcript FineSub's `speech/joint/` produced (`expected/<case>.json`) goes through
the moved `lines.py` exactly as `stage.py` built lines per backend -- MAI2: one DP by start, widened edges; 35t:
a DP per turn on the ungridded words, with and without sentence-end evidence; MOSS Pro: S5b timing from the
recorded frame probabilities (and S6b with a stand-in aligner), then the merged segments -- and must give the
recorded lines and derived VAD. Baselines: `tests/fixtures/transcription/record_original.py`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nonoka_x.transcription import lines as L  # noqa: E402
from nonoka_x.transcription.segmentation import dp as seg  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "transcription"
CASES = sorted(p.name for p in (FIXTURES / "cases").iterdir() if (p / "case.json").is_file())


def load(name: str) -> tuple[dict, dict]:
    case = json.loads((FIXTURES / "cases" / name / "case.json").read_text(encoding="utf-8"))
    expected = json.loads((FIXTURES / "expected" / f"{name}.json").read_text(encoding="utf-8"))
    return case, expected


def units_of(expected: dict) -> list[dict]:
    return [dict(u) for turn in expected["transcript"]["turns"] for u in turn["units"]]


def plain(data):
    return json.loads(json.dumps(data))


def fake_sentence_ends(words: list[dict]) -> None:
    for w in words:
        w[seg.SENTENCE_END_BONUS_KEY] = 0.9 if w["word"][-1:] in "。.?？!！" else 0.05


def fake_align(long_ones: list[dict], every: list[dict]) -> dict[int, list[dict]]:
    return {k: [dict(u, start=round(u["start"], 3), end=round(u["end"], 3)) for u in L.interpolated(s)]
            for k, s in enumerate(long_ones) if k % 2 == 0}


def build(case: dict, units: list[dict]) -> dict:
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
            np = pytest.importorskip("numpy")
            from nonoka_x.transcription.enhance import moss_timing

            probs = np.concatenate([np.full(int(n), v, np.float32) for v, n in case["probabilities"]])
            for label, align in (("s5b", None), ("s6b", fake_align)):
                timed, counts = moss_timing.timed(units, probs, duration=duration, params=params, align=align)
                segments, vad = L.segment_lines(timed, duration=duration, params=params, language=language)
                out[f"scale-{scale}-{label}"] = {"counts": counts, "segments": segments, "vad": vad}
    return out


@pytest.mark.parametrize("name", CASES)
def test_lines_match_the_baseline(name: str) -> None:
    case, expected = load(name)
    got = plain(build(case, units_of(expected)))
    assert sorted(got) == sorted(expected["lines"])
    for variant, want in expected["lines"].items():
        assert got[variant] == want, f"{name} {variant}: lines differ from the baseline"


def test_every_line_has_a_speaker_and_the_language() -> None:
    case, expected = load("gemini-windows")
    lines = build(case, units_of(expected))["scale-1.0-plain"]["segments"]
    assert lines and all(line[L.LINE_SPEAKER] and line["lang"] == "ja" for line in lines)
    starts = [line["start"] for line in lines]
    assert starts == sorted(starts)


def test_the_sentence_boundary_model_matches_its_baseline() -> None:
    """`enhance/sbd.py` against the baseline recorded on FineSub's original (`record_sbd.py`). Needs the model and
    an interpreter of the Python environment layer: set `NONOKA_SBD_PYTHON` (e.g. the PBS environment) and
    `NONOKA_SBD_HF_HOME` (the Hugging Face cache holding the model) to run it; skipped otherwise."""

    import os

    baseline = json.loads((FIXTURES / "expected" / "sbd.json").read_text(encoding="utf-8"))
    assert [len(c["bonus"]) for c in baseline["cases"]] == [len(c["words"]) for c in baseline["cases"]]
    python, hf_home = os.environ.get("NONOKA_SBD_PYTHON"), os.environ.get("NONOKA_SBD_HF_HOME")
    if not python or not hf_home:
        pytest.skip("NONOKA_SBD_PYTHON and NONOKA_SBD_HF_HOME not set")
    env = {**os.environ, "HF_HOME": hf_home, "HF_HUB_OFFLINE": "1", "PYTHONPATH": str(ROOT / "src"),
           "PYTHONIOENCODING": "utf-8"}
    done = subprocess.run([python, "-B", "-s", str(FIXTURES / "record_sbd.py"), "moved", "compare"],
                          env=env, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 0, done.stdout + done.stderr


def test_lines_import_in_a_launcher_only_environment() -> None:
    """`import nonoka_x.transcription.lines` needs nothing beyond the standard library (plan WP1)."""

    code = (
        f"import sys; sys.path.insert(0, {str(ROOT / 'src')!r}); "
        "import builtins; real = builtins.__import__\n"
        "def guard(name, *a, **k):\n"
        "    if name.split('.')[0] in {'finesub', 'finesub_bootstrap', 'numpy', 'torch', 'httpx', 'onnx'}:\n"
        "        raise ImportError('not in the launcher: ' + name)\n"
        "    return real(name, *a, **k)\n"
        "builtins.__import__ = guard\n"
        "import nonoka_x.transcription.lines, nonoka_x.transcription.stitch, nonoka_x.transcription.units\n"
    )
    subprocess.run([sys.executable, "-I", "-c", code], check=True, cwd=ROOT)
