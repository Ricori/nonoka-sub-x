"""Attach speaker tracks to subtitle segments.

The acoustic layer's timeline is per-track and continuous; subtitles are a
different grid. This joins them, and the joining is where two of the plan's
commitments become code:

* **A subtitle may carry several speakers.** Merged source segments, a VAD that
  did not split at a turn, and genuine overlap all produce one line with two
  people in it. Both end-to-end models and overlap-aware clustering pipelines
  can supply these timelines. Collapsing to the loudest would throw overlap away.
* **Nothing active means unlabelled, not `unknown`.** `unknown` is a judgement
  ("someone spoke, we cannot say who"); silence is the absence of one.

Nothing here decides who a track is. The LLM speaker judge
(`finesub.llm.stages.speaker_judge`) may later move lines between tracks or open
new ones, but it writes its own record and reads this partition rather than
competing with it (the ordered chain, P15); names come only from a person.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .labels import SegmentSpeaker
from .segments import TrackSegment


@dataclass(frozen=True)
class AssignmentParameters:
    min_share: float = 0.20
    """A track must cover this fraction of the subtitle to be named at all."""

    second_share: float = 0.20
    """And this much of the leader's coverage to be named *alongside* it.
    Below it, the runner-up becomes a review flag rather than a second label.

    Calibrated 2026-09-06 against VoxConverse dev (11 files spanning 0-36.6%
    reference overlap, 316 annotated segments, 166 of them genuinely two-speaker):

        share   dual-labelled   precision   recall     F1
        0.20        53.8%         0.918     0.940    0.929
        0.35        49.1%         0.948     0.886    0.916
        0.50        43.0%         0.993     0.813    0.894
        0.80        25.9%         1.000     0.494    0.661

    AliMeeting (3 Mandarin meetings, 84 minutes, 2450 segments, 1860 of them
    two-speaker) also peaks at 0.20 (F1 0.900, precision 0.969, recall 0.840).

    ⚠ That peak is close to a tautology, not independent support: "really two
    speakers" is itself defined as the second speaker covering >20% of the line,
    so F1 is bound to peak near 0.20 -- adding 0.10 / 0.15 to the grid changes
    nothing, and redefining the truth at 0.3 moves the optimum to 0.35. The
    reason for 0.20 is the plan's cost model, not the F1 table: a line in one
    track too many costs a deletion, a line missing from a track has to be
    noticed, found and moved."""

    review_margin: float = 0.15
    """Leader and runner-up within this fraction of each other -> review."""

    def __post_init__(self) -> None:
        for name in ("min_share", "second_share", "review_margin"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be a fraction, got {value}")


def _overlap(first_start: float, first_end: float, second_start: float, second_end: float) -> float:
    return max(0.0, min(first_end, second_end) - max(first_start, second_start))


def track_coverage(
    start: float, end: float, timeline: Sequence[TrackSegment]
) -> dict[int, float]:
    """How much of `[start, end)` each track covers, as a fraction of its length."""
    span = end - start
    if span <= 0:
        return {}
    covered: dict[int, float] = {}
    for segment in timeline:
        if segment.end <= start:
            continue
        if segment.start >= end:
            break
        amount = _overlap(start, end, segment.start, segment.end)
        if amount > 0:
            covered[segment.track] = covered.get(segment.track, 0.0) + amount
    return {track: amount / span for track, amount in covered.items()}


def assign_subtitle(
    start: float,
    end: float,
    timeline: Sequence[TrackSegment],
    *,
    block: int = 0,
    parameters: AssignmentParameters | None = None,
) -> SegmentSpeaker:
    """Which track(s) spoke over one subtitle segment.

    Returns an unlabelled `SegmentSpeaker` when no track reaches `min_share` --
    the caller writes no `speaker` field at all for those.
    """
    parameters = parameters or AssignmentParameters()
    coverage = track_coverage(start, end, timeline)
    ranked = sorted(coverage.items(), key=lambda item: (-item[1], item[0]))
    ranked = [(track, share) for track, share in ranked if share >= parameters.min_share]
    if not ranked:
        return SegmentSpeaker(block=block)

    leader, leader_share = ranked[0]
    tracks = [leader]
    review = False
    if len(ranked) > 1:
        runner_up, runner_share = ranked[1]
        if runner_share >= leader_share * parameters.second_share:
            tracks.append(runner_up)
        if leader_share - runner_share <= parameters.review_margin * leader_share:
            review = True

    return SegmentSpeaker(
        track=leader,
        block=block,
        review=review,
        extras={
            "tracks": tracks,
            "coverage": {str(track): round(share, 4) for track, share in coverage.items()},
        },
    )


def assign_all(
    subtitles: Sequence[Mapping[str, float]],
    timeline: Sequence[TrackSegment],
    *,
    block: int = 0,
    parameters: AssignmentParameters | None = None,
) -> list[SegmentSpeaker]:
    """`assign_subtitle` over a whole stable.json, in order."""
    ordered = sorted(timeline, key=lambda segment: segment.start)
    return [
        assign_subtitle(
            float(subtitle["start"]), float(subtitle["end"]), ordered, block=block, parameters=parameters
        )
        for subtitle in subtitles
    ]
