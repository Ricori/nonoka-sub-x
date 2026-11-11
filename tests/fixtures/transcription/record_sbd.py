"""The sentence-boundary model's baseline (`enhance/sbd.py`, was FineSub `speech/joint/sbd.py`).

Needs the model (`1-800-BAD-CODE/xlm-roberta_punctuation_fullstop_truecase` at the pinned revision, about
1.1 GB) in the Hugging Face cache `HF_HOME` names, and an interpreter with onnx, onnxruntime and sentencepiece.
WP1 recorded it on 2026-11-11 with the model in `D:\\Development\\nonoka-sub-x\\.tmp\\sbd-hf`:

    record:  HF_HOME=<cache> PYTHONPATH=<FineSub src at 6027246> <installed runtime python> -I record_sbd.py original record
    compare: HF_HOME=<cache> PYTHONPATH=<FineSub src at 6027246> <PBS python, no onnx>       -I record_sbd.py original compare
    after:   HF_HOME=<cache> PYTHONPATH=src                     <PBS python, no onnx>       -I record_sbd.py moved compare

The input is the 35t fixtures' DP words (ja, en) and the MOSS Pro fixture's units (zh) as `lines.dp_words` makes
them, stored with the baseline. Each run starts from the downloaded graph: the exposed copy beside it
(`sbd.EXPOSED`) is deleted first, so every environment builds it with its own `onnx`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASELINE = HERE / "expected" / "sbd.json"


def inputs(lines) -> list[dict]:
    out = []
    for name in ("gemini-windows", "gemini-split-speakerless", "moss-parts-continue-gap"):
        expected = json.loads((HERE / "expected" / f"{name}.json").read_text(encoding="utf-8"))
        units = [u for turn in expected["transcript"]["turns"] for u in turn["units"]]
        words = lines.dp_words(units, duration=expected["transcript"]["duration"], keep_order=True)
        out.append({"case": name, "words": [{"word": w["word"], "space_before": w["space_before"]} for w in words]})
    return out


def main(impl: str, mode: str) -> int:
    if impl == "original":
        from finesub.speech.joint import lines, sbd
    else:
        from nonoka_x.transcription import lines
        from nonoka_x.transcription.enhance import sbd

    original = Path(sbd._fetch("model.onnx"))
    exposed = original.with_name(sbd.EXPOSED)
    exposed.unlink(missing_ok=True)
    cases = inputs(lines) if mode == "record" else json.loads(BASELINE.read_text(encoding="utf-8"))["cases"]
    got = []
    for case in cases:
        words = [dict(w) for w in case["words"]]
        sbd.sentence_ends(words)
        got.append({"case": case["case"], "words": case["words"],
                    "bonus": [w[sbd.SENTENCE_END_BONUS_KEY] for w in words]})
    if mode == "record":
        BASELINE.write_text(json.dumps({"cases": got}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print("recorded", [(c["case"], len(c["bonus"])) for c in got])
        return 0
    bad = [c["case"] for c, want in zip(got, cases) if c["bonus"] != want["bonus"]]
    import onnx

    print("onnx", getattr(onnx, "__version__", "?"), "from", Path(onnx.__file__).parent)
    print("MISMATCH" if bad else "IDENTICAL", bad or [(c["case"], len(c["bonus"])) for c in got])
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
