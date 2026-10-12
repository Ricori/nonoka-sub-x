"""Per-track re-decoded overlap text, offered to the correction round as candidates (plan 5r B2).

Experiment, off unless ``FINESUB_OVERLAP_CANDIDATES`` names a ``<stem>-overlap-candidates.json``
written by ``tools/diarization_explore/redecode_candidates.py``. With it set, a correction window gets
the candidates whose time overlaps its rows, each with the local row ids it overlaps; the prompt
fragment that frames them is ``fragment_overlap_candidates_v1.md``. Unset, nothing is rendered and
the correction prompt is byte-identical to what it was.

Data only: no prompt wording lives here.

⚠ Known flaws, left as they are because the switch is kept only for a later restart (owner, 2026-09-18);
fix them before that restart, not after:

* the candidates are cut from the **mix** over each target track's active time and re-recognised, but
  the fragment says each speaker's voice was "cut out on its own" -- the template overstates them;
* the switch is a process-wide environment variable, so a batch applies one candidates file to every task;
* it is not part of ``PROMPT_VERSION``, the task fingerprint or ``session_replay``, so a resumed task can
  mix windows prompted with and without candidates;
* ``_load`` caches by path, so a candidates file edited mid-process is not re-read.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from .chunking import SubtitleWindow

ENV = "FINESUB_OVERLAP_CANDIDATES"
#: Seconds a candidate and a row must share before the row counts as covered.
MIN_SHARED = 0.1


@lru_cache(maxsize=4)
def _load(path: str) -> tuple[dict, ...]:
    return tuple(json.loads(Path(path).read_text(encoding="utf-8")))


def candidate_rows(window: SubtitleWindow) -> str:
    """`track|covers|text` rows for this window, or "" when the switch is off or nothing overlaps."""

    path = os.environ.get(ENV, "").strip()
    if not path or not window.segments:
        return ""
    rows = []
    for candidate in _load(path):
        covers = [
            str(index)
            for index, segment in enumerate(window.segments, start=1)
            if min(candidate["end"], segment.end) - max(candidate["start"], segment.start) > MIN_SHARED
        ]
        if covers:
            text = str(candidate["text"]).replace("|", "｜").replace("\n", " ").strip()
            rows.append(f"s{candidate['track']}|{','.join(covers)}|{text}")
    return "\n".join(rows)
