"""Nonoka's own copy of FineSub's sentence DP and the pure-Python parts it and the line builder use.

The cloud engines' lines (`transcription.lines`) go through production's DP, as they did inside FineSub. Copied
from FineSub v0.7.0 @ 7123055 (`docs/transcription-engines-plan.md` 4.1 item 5); from here on Nonoka maintains it:

- `dp`: `speech/postprocessing/segmentation.py`, the DP itself;
- `segments`: `speech/recognition/segments.py`, the timeline contract (`clamp_segment_overlaps`,
  `normalize_segment_timeline`, `GAP_KEEP_REAL_MAX_SEC`);
- `line_groups`: `subtitles/line_groups.py`, which lines may overlap;
- `metrics`: `subtitles/metrics.py`, the weighted character count;
- `text`: the few helpers of `text.py` the above use.

The copies give the same output as the originals on the same input (`tests/test_transcription_segmentation.py`,
against baselines recorded on the originals). Standard library only.
"""
