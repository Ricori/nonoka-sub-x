"""One clean sample per track: ten seconds that answer "who is this" faster than any prompt.

Naming tracks is the shape a language model is worst at -- read a whole-file summary, produce N
names -- and the shape a person is best at, given ten seconds of audio. So point at the audio.

⚠ **Choosing the span is what lives here; cutting it does not.** The two halves have opposite
costs: choosing needs `soft`, which exists only while the run is happening, while cutting is a few
seconds of ffmpeg from the vocal track at any later time. Only the expensive half is stored, and
`tools/diarizen_export/cut_samples.py` does the cheap half on demand. A `write_samples` used to
sit here with no caller at all, which is the shape this branch spent a close-out pass removing.

The pick is not "the longest stretch". It is the window where the track is **most itself**:
nobody else active, and its own chunk sitting closest to its own centroid. Both facts are already
in the evidence sidecar, which is the point of having kept it.

⚠ **The scored thing and the delivered thing are the same window.** The first version scored whole
exclusive runs, let the longest one win, and then cut ten seconds out of its middle -- so the
evidence that won the comparison could be the part that got cut away. Measured on a constructed
case: it delivered a window at closeness 0.90 and discarded one at 0.20. Candidate windows are
enumerated at delivery length now, and the winner is returned unaltered.

⚠ Exclusive frames only. A stretch where two people talk at once is the worst possible sample
for telling them apart, and overlap runs about 30% of labelled rows on real material.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: How much audio a person needs to recognise a voice. Long enough to hear phrasing, short
#: enough that listening to all 13 tracks of a busy file is about two minutes of work.
DEFAULT_SECONDS = 10.0

#: Never cut a sample shorter than this; below it the clip is a fragment rather than a voice.
MIN_SECONDS = 2.0

#: How long one segmentation chunk is. Here rather than imported from `diarizen_runner` because
#: it is a property of whatever produced `soft`, and a caller with different geometry says so.
DEFAULT_CHUNK_DURATION = 16.0


@dataclass(frozen=True)
class TrackSample:
    """Where one track sounds most like itself."""

    track: int
    start: float
    end: float
    #: How close the BEST covering chunk sits to this track's own centroid (`2 - soft.max()`,
    #: so lower is closer). `None` when the sidecar had no `soft` for it.
    closeness: float | None

    @property
    def seconds(self) -> float:
        return self.end - self.start


def exclusive_runs(activity, *, step: float, track: int, min_seconds: float = MIN_SECONDS):
    """`(start, end)` spans where `track` sounds and nothing else does."""
    import numpy as np

    data = np.asarray(activity)
    alone = (data[:, track] > 0) & (data.sum(axis=1) == 1)
    if not alone.any():
        return []

    edges = np.flatnonzero(np.diff(np.concatenate(([0], alone.view(np.int8), [0]))))
    spans = []
    for begin, stop in zip(edges[0::2], edges[1::2]):
        start, end = begin * step, stop * step
        if end - start >= min_seconds:
            spans.append((float(start), float(end)))
    return spans


def _closeness(evidence, track: int, start: float, end: float, *,
               chunk_step: float, chunk_duration: float = DEFAULT_CHUNK_DURATION) -> float | None:
    """How close the chunks covering this span sit to the track's own centroid.

    ⚠ **Covering, not merely overlapping.** Chunk `i` runs `[i*chunk_step, i*chunk_step +
    chunk_duration]`, which at 1.6 s / 16 s means the chunks that hold a ten-second window whole
    start up to ten steps EARLIER than the window does. Reading `start/chunk_step - 1 ..
    end/chunk_step + 1` -- which is what this did -- enumerates chunks that merely touch the
    window, mostly describing audio outside it. When nothing covers the span (a span longer than
    a chunk, or one at the very end of the file) the overlapping chunks are the honest fallback.
    """
    import numpy as np

    if evidence is None or evidence.soft is None:
        return None
    soft = np.asarray(evidence.soft)
    # ⚠ Through the mapping, never by track id: `cluster()`'s renumbering gives the sentinel an id,
    # so the two only coincide when every local speaker was placeable.
    mapping = getattr(evidence, "centroid_of_track", None)
    if mapping is None or not len(mapping):
        column = track
    elif track >= len(mapping) or mapping[track] < 0:
        return None
    else:
        column = int(mapping[track])
    if soft.ndim != 3 or column >= soft.shape[2] or not soft.shape[0]:
        return None
    low = max(0, int(np.ceil((end - chunk_duration) / chunk_step)))
    high = min(soft.shape[0], int(start / chunk_step) + 1)
    if high <= low:
        low = max(0, int(start / chunk_step) - 1)
        high = min(soft.shape[0], int(end / chunk_step) + 2)
    if high <= low:
        return None
    window = soft[low:high, :, column]
    values = window[np.isfinite(window)]
    # `soft` is `2 - cosine distance`, so larger is closer; report it as a distance.
    return float(2.0 - values.max()) if values.size else None


def delivery_windows(start: float, end: float, *, seconds: float, stride: float):
    """Every candidate clip inside one exclusive run, at the length that will be delivered.

    A run shorter than `seconds` is its own only candidate. Otherwise the window slides by
    `stride`, and the run's tail is always offered even when the stride does not land on it --
    the end of a run is often where a speaker is most alone.
    """
    if end - start <= seconds:
        return [(start, end)]
    found = []
    position = start
    while position + seconds <= end + 1e-9:
        found.append((round(position, 3), round(position + seconds, 3)))
        position += stride
    tail = (round(end - seconds, 3), round(end, 3))
    if not found or found[-1] != tail:
        found.append(tail)
    return found


def pick_samples(
    activity,
    *,
    step: float,
    evidence: Any = None,
    chunk_step: float = 1.6,
    seconds: float = DEFAULT_SECONDS,
) -> list[TrackSample]:
    """The best exclusive window for every track that has one.

    ⚠ Tracks with no exclusive stretch at all are omitted rather than given a bad sample. A track
    that never sounds alone is a real finding about the material -- usually a person who only ever
    speaks over somebody -- and handing back a clip of two voices would hide it.

    ⚠ Candidates are enumerated at **delivery length**, so the window that wins is the window that
    is handed over. Scoring whole runs and then cutting the middle out of the winner -- which is
    what this did -- lets a run win on evidence that the cut discards.
    """
    import numpy as np

    data = np.asarray(activity)
    picks: list[TrackSample] = []
    for track in range(data.shape[1]):
        candidates = [window
                      for span in exclusive_runs(data, step=step, track=track)
                      for window in delivery_windows(*span, seconds=seconds, stride=chunk_step)]
        if not candidates:
            continue
        scored = []
        for start, end in candidates:
            close = _closeness(evidence, track, start, end, chunk_step=chunk_step)
            # Prefer closeness where the sidecar has it; fall back to the longest window, which
            # is what this could do before the evidence was kept. Ties break on start time so
            # the answer does not depend on how the runs happened to be ordered.
            scored.append((close if close is not None else float("inf"), -(end - start),
                           start, end))
        scored.sort()
        closeness, _, start, end = scored[0]
        picks.append(TrackSample(
            track=track, start=start, end=end,
            closeness=None if closeness == float("inf") else closeness,
        ))
    return picks
