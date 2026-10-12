"""Write speaker labels onto a stable.json payload.

Owner decision (2026-09-05): the labels ride on the segments themselves. This is
the function that puts them there, and it is deliberately a pure transform --
wiring it into the pipeline is a separate concern, and keeping it separate is
what lets the default test suite cover the part that can silently corrupt a
run's output.

Three rules, all of them consequences of decisions recorded elsewhere:

* Segments with nothing to say get **no `speaker` key at all**, rather than an
  empty one. Absence is the documented way to say "not labelled" (labels.py), and
  writing `{}` would make every reader decide for itself what that means.
* Everything else on a segment is left exactly as it was. This runs after
  stabilization, over an artifact other stages already produced.
* The run-level record says which model produced the labels. Its conservative
  `reproducible: False` means that regeneration across numeric paths is not
  guaranteed, not that identical-input repeats are random (`docs/diarization-findings.md` 5.1).
"""

from __future__ import annotations

import copy
from typing import Any, Mapping, MutableMapping, Sequence

from .labels import SegmentSpeaker

#: Key on the run-level metadata block.
METADATA_KEY = "speaker"


def annotate_segments(
    segments: Sequence[Mapping[str, Any]],
    speakers: Sequence[SegmentSpeaker],
) -> list[dict[str, Any]]:
    """Copy `segments`, adding `speaker` to those that have one."""
    if len(segments) != len(speakers):
        raise ValueError(f"{len(segments)} segments but {len(speakers)} speaker records")
    annotated = []
    for segment, speaker in zip(segments, speakers):
        row = dict(segment)
        # A filled line has no `track` but a `mark` naming one (`marks.py`): it is written too.
        if speaker.is_labelled or speaker.track is not None or "mark" in speaker.extras:
            row["speaker"] = speaker.to_dict()
        else:
            row.pop("speaker", None)  # a stale label from an earlier run must not survive
        annotated.append(row)
    return annotated


def speaker_metadata(
    *,
    model: str,
    artifacts_revision: str,
    track_count: int,
    parameters: Mapping[str, Any],
) -> dict[str, Any]:
    """The run-level record that travels with the labels.

    `reproducible: False` is not boilerplate. Measured on 61.6 minutes: scaling
    the input by 1+1e-7 -- inaudible, far under any recording's noise floor --
    can change per-subtitle assignments because the speaker cache keeps frames
    by a discrete top-k. Same-input, same-path repeats were bit-exact in later
    tests; a changed input/path can change quality, not just arbitrary IDs.
    This field does not promise equivalence across such changes. Task artifacts
    are reused rather than silently regenerated with today's numerical path.
    """
    return {
        "model": model,
        "artifacts_revision": artifacts_revision,
        "track_count": track_count,
        "parameters": dict(parameters),
        "reproducible": False,
    }


def attach_metadata(payload: MutableMapping[str, Any], metadata: Mapping[str, Any]) -> None:
    """Put the run-level record on a stable.json payload, in place."""
    block = payload.setdefault("metadata", {})
    if not isinstance(block, MutableMapping):
        raise TypeError(f"metadata must be an object, got {type(block).__name__}")
    block[METADATA_KEY] = dict(metadata)


def annotate_payload(
    payload: Mapping[str, Any],
    speakers: Sequence[SegmentSpeaker],
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """A whole stable.json in, a whole stable.json out."""
    result = copy.deepcopy(dict(payload))
    segments = result.get("segments")
    if not isinstance(segments, list):
        raise TypeError("stable payload has no 'segments' list")
    result["segments"] = annotate_segments(segments, speakers)
    if metadata is not None:
        attach_metadata(result, metadata)
    return result
