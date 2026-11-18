"""Frame-level speaker activity -> per-track segments (P11).

The model emits four *independent* sigmoids per frame, not one label. On real
collab material 20.1% of speech has two of them active at once. Preserving that
overlap requires independent activity per speaker -- so the smoothing
here runs **per track** and can never collapse two simultaneous speakers into one.
That is not a preference; `test_diarization_segments.py` fails if it happens.

Order matters: bridge first, then drop. A stretch of speech broken by a blink of
low confidence must be rejoined *before* anything decides it is too short to keep,
or the two halves get dropped separately and the speech disappears with them.

⚠ The default thresholds below are provisional. They come from the shape of the
model's output, not from calibration against annotated material -- and the plan
(docs/plans/speaker-diarization-plan.md §4, calibration status) forbids setting defaults from the
synthetic fixtures we currently have. Treat them as a starting point that a real
gold set has to confirm.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

#: One row of activity, in seconds. The encoder subsamples by 8 from 10 ms frames.
FRAME_SECONDS = 0.08


@dataclass(frozen=True)
class TrackSegment:
    """One stretch where a single track was active."""

    track: int
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class SmoothingParameters:
    enter: float = 0.5
    """Probability at which a track counts as speaking."""

    leave: float = 0.4
    """And the lower one at which it stops -- hysteresis, so a track sitting near
    the threshold does not shatter into dozens of segments."""

    min_gap: float = 0.16
    """Gaps shorter than this inside one track are bridged, not honoured."""

    min_duration: float = 0.24
    """Segments shorter than this are dropped -- after bridging, never before."""

    def __post_init__(self) -> None:
        if not 0.0 < self.leave <= self.enter <= 1.0:
            raise ValueError(f"need 0 < leave <= enter <= 1, got leave={self.leave} enter={self.enter}")


def _runs_for_track(column: Sequence[float], parameters: SmoothingParameters) -> list[tuple[int, int]]:
    """Hysteresis over one track's probabilities, as [start, end) frame indices."""
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate(column):
        if start is None:
            if value >= parameters.enter:
                start = index
        elif value < parameters.leave:
            runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, len(column)))
    return runs


def _bridge(runs: list[tuple[int, int]], min_gap_frames: int) -> list[tuple[int, int]]:
    """Join runs separated by less than `min_gap_frames`.

    Deliberately unaware of the other tracks. Bridging one speaker across
    another's turn would be a real artefact -- it hands that turn's line a second
    speaker who is provably silent during it -- but at these defaults it cannot
    happen: a bridgeable gap is under 2 frames while a surviving turn needs 3, so
    the two conditions exclude each other. A guard here would be a mechanism for
    a problem this code does not have. ⚠ If calibration raises `min_gap` past
    `min_duration`, that stops being true and the guard has to come back.
    """
    if not runs:
        return []
    joined = [runs[0]]
    for start, end in runs[1:]:
        previous_start, previous_end = joined[-1]
        if start - previous_end < min_gap_frames:
            joined[-1] = (previous_start, end)
        else:
            joined.append((start, end))
    return joined


def segments_from_activity(
    activity: Sequence[Sequence[float]],
    *,
    frame_seconds: float = FRAME_SECONDS,
    parameters: SmoothingParameters | None = None,
) -> list[TrackSegment]:
    """Turn `(frames, tracks)` probabilities into segments, one track at a time.

    Returns segments ordered by start time; tracks may overlap, and that is the
    point. Frames are half-open: a run of frames `[i, j)` becomes
    `[i * frame_seconds, j * frame_seconds)`.
    """
    parameters = parameters or SmoothingParameters()
    if not activity:
        return []
    track_count = len(activity[0])
    min_gap_frames = max(1, round(parameters.min_gap / frame_seconds))
    min_duration_frames = max(1, round(parameters.min_duration / frame_seconds))

    found: list[TrackSegment] = []
    for track in range(track_count):
        column = [row[track] for row in activity]
        runs = _bridge(_runs_for_track(column, parameters), min_gap_frames)
        for start, end in runs:
            if end - start < min_duration_frames:
                continue
            found.append(
                TrackSegment(track=track, start=start * frame_seconds, end=end * frame_seconds)
            )
    found.sort(key=lambda segment: (segment.start, segment.track))
    return found


def segment_overlap_ratio(segments: Sequence[TrackSegment]) -> float:
    """Share of speech time where at least two tracks are active, **after smoothing**.

    ⚠ Not the same number as the frame-level overlap quoted in
    `docs/diarization-findings.md` (20.1% on the 4-way collab). This one is
    measured on segments, so bridging short gaps and dropping short bursts have
    already had their say -- on a 3-minute slice of that same collab it reads
    43.8%. Two statistics both called "overlap" invite exactly the comparison
    that means nothing, hence the longer name.

    The cheap statistic P14 wants: one mouth cannot talk over itself, so a solo
    recording split into several tracks should sit near zero while a real
    conversation does not.
    """
    if not segments:
        return 0.0
    edges = sorted({value for segment in segments for value in (segment.start, segment.end)})
    speech = 0.0
    overlapped = 0.0
    for left, right in zip(edges, edges[1:]):
        span = right - left
        if span <= 0:
            continue
        active = sum(1 for segment in segments if segment.start <= left and segment.end >= right)
        if active >= 1:
            speech += span
        if active >= 2:
            overlapped += span
    return overlapped / speech if speech else 0.0


def track_seconds(segments: Sequence[TrackSegment]) -> dict[int, float]:
    """Active seconds per track (segments of one track never overlap each other)."""
    out: dict[int, float] = {}
    for segment in segments:
        out[segment.track] = out.get(segment.track, 0.0) + max(0.0, segment.end - segment.start)
    return out


def union_seconds(segments: Sequence[TrackSegment]) -> float:
    """Seconds in which at least one track sounds."""
    total, reach = 0.0, float("-inf")
    for segment in sorted(segments, key=lambda s: s.start):
        if segment.end > reach:
            total += segment.end - max(segment.start, reach)
            reach = segment.end
    return total


def pair_co_activity(segments: Sequence[TrackSegment]) -> dict[tuple[int, int], float]:
    """For every pair of tracks, the share of the *shorter* one's time when both sound.

    The per-pair form of `segment_overlap_ratio`: one mouth cannot talk over
    itself, so two tracks that sound together a lot are often two people. Often,
    not always: one person split over two tracks sounds together with themself
    too, as much as 33.7% on real material, so this is reported as a number and
    never turned into a verdict (plan 5q). Relative to the shorter track, so a
    short track that only ever speaks over a long one still reads high.
    """
    by_track: dict[int, list[tuple[float, float]]] = {}
    for segment in segments:
        by_track.setdefault(segment.track, []).append((segment.start, segment.end))
    for spans in by_track.values():
        spans.sort()
    seconds = track_seconds(segments)
    tracks = sorted(by_track)
    out: dict[tuple[int, int], float] = {}
    for position, first in enumerate(tracks):
        for second in tracks[position + 1:]:
            shorter = min(seconds[first], seconds[second])
            if shorter > 0:
                out[(first, second)] = _overlap_seconds(by_track[first], by_track[second]) / shorter
    return out


def _overlap_seconds(a: Sequence[tuple[float, float]], b: Sequence[tuple[float, float]]) -> float:
    """Shared time of two sorted, internally disjoint span lists (one pass)."""
    i = j = 0
    total = 0.0
    while i < len(a) and j < len(b):
        start, end = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if end > start:
            total += end - start
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return total
