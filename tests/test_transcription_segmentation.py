"""Nonoka's copy of the sentence DP gives what FineSub's original gave (plan 4.1 item 5).

`expected/segmentation.json` was recorded on FineSub's `speech/postprocessing/segmentation.py` before the copy
was made (`tests/fixtures/transcription/record_original.py`).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nonoka_x.transcription.segmentation import dp  # noqa: E402

BASELINE = ROOT / "tests" / "fixtures" / "transcription" / "expected" / "segmentation.json"


def _cases() -> list[dict]:
    return json.loads(BASELINE.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("index", range(len(_cases())))
def test_the_copy_splits_like_the_original(index: int) -> None:
    case = _cases()[index]
    params = dp.split_params_for_length_scale(case["scale"])
    assert dp.split_params_metadata(params) == case["params"]
    got = dp.split_segments(json.loads(json.dumps(case["segments"])), case["vad"], params=params)
    assert json.loads(json.dumps(got)) == case["result"]


def test_the_length_scale_keeps_its_range() -> None:
    assert dp.split_params_for_length_scale(1.0) is dp.DEFAULT_SPLIT_PARAMS
    for bad in (0.5, 1.7, float("nan"), True, "1.0"):
        with pytest.raises(ValueError):
            dp.check_length_scale(bad)
    assert dp.check_length_scale(1) == 1.0


def test_the_copy_needs_only_the_standard_library() -> None:
    code = (
        "import sys; import nonoka_x.transcription.segmentation.dp, nonoka_x.transcription.segmentation.segments; "
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in {'finesub', 'finesub_bootstrap', 'numpy', 'torch'}); "
        "assert not bad, bad"
    )
    subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(ROOT / 'src')!r}); {code}"],
                   check=True, cwd=ROOT)
