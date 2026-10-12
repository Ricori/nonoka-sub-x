"""What a diarization backend is, and nothing about which ones exist.

The three layers this package is built from meet here, and keeping them apart is the point:

1. **Acoustic partition** -- who spoke when, as relative track ids. That is what a `Backend`
   produces and it is the only thing `segments`, `assign`, `labels` and `export` read.
2. **Identity representation** -- voiceprints and the evidence behind them (`evidence.py`). A
   backend may or may not have any; Sortformer has none at all.
3. **Who a track is** -- which the LLM speaker judge (`llm/stages/speaker_judge/`) works out, and
   never here. The acoustic layer owns the partition; the judge's record sits beside it.

⚠ This module imports no sibling, which is what lets the adapters
(`nemotron3_backend`, `diarizen_backend`, `sortformer_backend`) depend on it while `stage` depends
on them. Put a registry here and that stops being true.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

#: Where the exported TorchScript lives, if the caller does not say. Shared: both backends keep
#: their artifacts under the same directory, because a user sets this once.
ARTIFACT_ENV = "FINESUB_DIARIZATION_ARTIFACTS"


class DiarizationUnavailable(RuntimeError):
    """The stage cannot run here -- missing artifacts, missing torch, no GPU.

    ⚠ The ONLY exception `_run_diarization_stage` catches. Anything else a backend raises takes
    the whole task down after the ASR has already finished, which is exactly what this stage
    promises not to do -- so a backend that cannot run has to say it in this type, before it
    starts work rather than from inside a model.
    """


@dataclass(frozen=True)
class BackendOutput:
    """What a backend hands back: the activity, and whatever evidence it had anyway.

    ⚠ `evidence` is `None` for a backend that computes none -- Sortformer has no embeddings -- and
    every consumer must treat that as "no claim" rather than reaching for a weaker signal. Giving
    one a substitute signal is the mistake that sank the first single-speaker veto (plan 5b, P14).
    """

    activity: Any
    evidence: "Any | None" = None


@dataclass(frozen=True)
class Backend:
    """One way to turn a waveform into `(frames, speakers)` activity.

    Everything downstream of the timeline -- `segments`, `assign`, `labels`, `export`, the speaker
    judge -- reads only that array and `TrackSegment`, so a backend is exactly these fields and
    nothing else touches it. The contract the array must satisfy: one row per `frame_seconds`,
    values in [0, 1], columns are relative track ids with no meaning across files.

    ⚠ `frame_seconds` is part of the backend, not a constant: Nemotron-3 and DiariZen are at 0.02 s,
    Sortformer at 0.08 s. The timeline schema records it, so a backend at a different rate must
    say so here rather than let the readers assume.
    """

    name: str
    #: Written into `-diar.json` so a later reader knows what produced it.
    model: str
    frame_seconds: float
    #: `(artifacts, device) -> handle`: raise `DiarizationUnavailable` naming the way out, or
    #: return a handle the activity call understands. The device is an argument because a
    #: backend may need a different build per device (DiariZen's traced graphs do).
    ensure: "Callable[[str | Path | None, str], Any]"
    #: `(waveform, handle, device, max_speakers) -> BackendOutput`.
    activity: "Callable[..., Any]"


def artifact_candidates(explicit: "str | Path | None") -> list[Path]:
    """Where to look for exported artifacts, most explicit first.

    Shared by both backends so that "the directory I pointed the environment variable at" means
    the same thing to each of them.
    """
    found = []
    if explicit:
        found.append(Path(explicit))
    import os

    from_environment = os.environ.get(ARTIFACT_ENV)
    if from_environment:
        found.append(Path(from_environment))
    return found
