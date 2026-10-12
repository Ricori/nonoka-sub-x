"""The acoustic evidence a clustering backend produces, kept instead of discarded.

The clustering already answers "who resembles whom, and by how much" -- `cluster()` returns the
per-(chunk, speaker) distances to every centroid and the centroids themselves -- and until now the
seam threw both away, keeping only the hard assignment. Every downstream question of the form "is
this the same person" then had nothing to ask but the timeline, which does not know: the old
single-speaker veto used co-activity and failed, and the `not_same` verdict calibrated on 23
same-person pairs fired on all 36 pairs of a three-person show. Neither was a bad threshold; both
asked the wrong object.

⚠ **Deliberately not part of `-diar.json`.** That file is a product contract and it is
backend-agnostic; this is a float matrix and it is backend-specific. It is also recomputable from
the audio and the artifacts, which under `README_DEV.md` 开发原则's classification makes it a removable
intermediate rather than a record -- so it lives in its own sidecar and is registered as
deletable.

⚠ **Sortformer produces none of this.** It has no embeddings at all. Everything built on this
evidence therefore needs a clustering backend -- nemotron3 (the default) or diarizen, which share
the clustering half -- and the fallback is "no evidence, no claim" -- not a weaker
substitute signal, which is exactly the mistake that sank the first veto.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

def _finite(value):
    """NaN in the file means the backend computed none; None is what callers see."""
    import math

    if value is None:
        return None
    number = float(value)
    return None if math.isnan(number) else number


def _or_empty(value, dtype):
    """An array to save; an empty one stands for `None` (npz has no null)."""
    import numpy as np

    return np.asarray(value, dtype=dtype) if value is not None else np.zeros(0, dtype=dtype)


def _present(data, name):
    """The saved array, or `None` when it is missing (an older sidecar) or empty (a backend without it)."""
    value = data[name] if name in data else None
    return None if value is None or value.size == 0 else value


#: What the sidecar is called, appended to the subtitle stem.
SUFFIX = "-diar-evidence.npz"


def timeline_key(tracks) -> "tuple[int, int]":
    """`(distinct track ids, segment count)` -- what this evidence is allowed to sit beside.

    ⚠ The sidecar indexes `soft` and `centroids` by TRACK ID, so it is meaningless beside any
    timeline but the one it came from -- and the two files expire independently: `-diar.json` is
    kept on existence alone, the sidecar is registered as deletable, and a rerun on another
    backend writes no sidecar at all. Each of those leaves a stale npz whose columns describe a
    timeline that is gone, and nothing about it looks wrong. This is the cheapest thing that can
    tell them apart: a different partition almost always has a different segment count, and a
    consumer that gets `None` simply goes without the extra.
    """
    segments = list(tracks or ())
    return len({int(segment.track) for segment in segments}), len(segments)


@dataclass(frozen=True)
class ClusterEvidence:
    """One file's acoustic evidence, as the clustering computed it.

    `soft` is `(chunks, speakers, clusters)`: how close each chunk's local speaker sits to each
    global cluster. `centroids` is `(clusters, dimension)` in the embedding space.

    `native_activity` is the `(frames, clusters)` 0/1 map at the model's OWN frame rate, 20 ms,
    before the reduction to the 80 ms product grid. ⚠ Kept because the product grid is reduced
    with a max, which widens activity outward, and word timestamps run 100-300 ms -- too coarse
    for anything that needs a cut point. Consumers that want precision read this; `-diar.json`
    keeps its 80 ms contract untouched.

    `samples` is one clean span per track: where that track sounds alone and closest to its own
    centroid. ⚠ Kept here rather than as cut audio because the two halves have opposite costs --
    choosing the span needs `soft`, which exists only while the run is happening, while cutting it
    is a few seconds of ffmpeg from the vocal track at any later time. Store the expensive half.
    """

    soft: Any
    centroids: Any
    native_activity: Any
    native_step: float
    #: Track id -> column in `soft`/`centroids`, `-1` where a track has none.
    #: ⚠ Not the identity: the renumbering inside `cluster()` gives the unplaceable-speaker
    #: sentinel an id of its own, so indexing by track id reads the wrong column.
    centroid_of_track: Any = None
    #: ⚠⚠ **The divisor `centroids` is missing.** What is stored is a weighted SUM, so a centroid
    #: is proportional to how much its cluster SPOKE -- fine for production, which only takes a
    #: cosine of it, and wrong for anything that applies a transform with a translation. The
    #: identity beds did exactly that and had track length folded into every distance they
    #: reported (2026-09-20 audit, finding 5). ⚠ Kept here so this file is not the same trap for
    #: the next consumer: `diarizen_cluster.mean_centroids` is the accessor, and this is also the
    #: effective sample count behind each voiceprint, which is the other thing an identity
    #: representation has to carry.
    centroid_weights: Any = None
    #: `ELBO_K - ELBO_1`: how much better the clustering's model explains the embeddings than a
    #: single-speaker model. Large means the model prefers several speakers. ⚠ ⚠ **Not calibrated on the failure it is for.** What it should catch is genuinely single-speaker material being over-split, and the bed's single-speaker arm always clustered to K=1, where this ratio is identically 0 by construction -- a perfect separation that demonstrates nothing (identity plan A.4, gap tracked as Q13).
    #: ⚠ **A TOTAL over the embeddings**, so
    #: it scales with how long the file is and two files' values are not comparable until each is
    #: divided by `log_bayes_embeddings`. ⚠ **A number, not a verdict** -- nothing suppresses
    #: labels on it. `None` when the backend computed none.
    single_speaker_log_bayes: "float | None" = None
    #: `[{"track": int, "start": float, "end": float, "closeness": float | None}]`.
    samples: "tuple[dict, ...]" = ()
    #: How many embeddings `single_speaker_log_bayes` was summed over. ⚠ Without it that number
    #: is unreadable across files: it is a total, so it grows with duration.
    log_bayes_embeddings: "int | None" = None
    #: `timeline_key()` of the timeline this belongs to, or None when it was saved without one.
    timeline: "tuple[int, int] | None" = None
    #: nemotron3 only, `None` elsewhere -- what the per-line confidence marks read (`marks.py`):
    #: `slot_grid` is the rasterised `(frames, slots)` 0/1 slot activity the clustering windows were cut
    #: from; `slot_probs` is Nemotron's `(frames, slots)` probabilities on the same 20 ms grid as
    #: `native_activity`; `hard` is the `(windows, slots)` track each local slot was clustered to.
    #: ⚠ float32 on disk, not float16: the marks are thresholds on these, and a quantised copy would
    #: flip lines near them for no reason.
    slot_grid: Any = None
    slot_probs: Any = None
    hard: Any = None

    def save(self, path: str | Path) -> Path:
        import numpy as np

        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            destination,
            soft=np.asarray(self.soft, dtype=np.float32),
            centroids=np.asarray(self.centroids, dtype=np.float32),
            # int8 rather than float: it is 0/1, and a 40-minute file is 120k frames.
            native_activity=np.asarray(self.native_activity, dtype=np.int8),
            # ⚠ float64: it is a geometry constant that consumers divide times by, and
            # float32 turns 0.02 into 0.019999999552965164 for no reason at all.
            native_step=np.float64(self.native_step),
            # ⚠ NOT `dtype=object`: that makes it a pickle, and reading one means executing it.
            # A plain unicode scalar loads under `allow_pickle=False`.
            samples=np.array(json.dumps(list(self.samples))),
            log_bayes_embeddings=np.int64(-1 if self.log_bayes_embeddings is None
                                          else self.log_bayes_embeddings),
            timeline=np.asarray(self.timeline if self.timeline is not None else (-1, -1),
                                dtype=np.int64),
            single_speaker_log_bayes=np.float64(
                np.nan if self.single_speaker_log_bayes is None
                else self.single_speaker_log_bayes),
            centroid_of_track=(np.asarray(self.centroid_of_track, dtype=np.int16)
                               if self.centroid_of_track is not None
                               else np.zeros(0, dtype=np.int16)),
            centroid_weights=(np.asarray(self.centroid_weights, dtype=np.float32)
                              if self.centroid_weights is not None
                              else np.zeros(0, dtype=np.float32)),
            slot_grid=_or_empty(self.slot_grid, np.uint8),
            slot_probs=_or_empty(self.slot_probs, np.float32),
            hard=_or_empty(self.hard, np.int16),
        )
        return destination

    @classmethod
    def load(cls, path: str | Path) -> "ClusterEvidence":
        import numpy as np

        # ⚠ `allow_pickle` stays FALSE: these files travel between users inside `out/<stem>/`,
        # and a pickle in one is arbitrary code at read time. Everything here is plain arrays.
        with np.load(Path(path), allow_pickle=False) as data:
            bound = data["timeline"].tolist() if "timeline" in data else [-1, -1]
            counted = int(data["log_bayes_embeddings"]) if "log_bayes_embeddings" in data else -1
            return cls(
                soft=data["soft"],
                centroids=data["centroids"],
                native_activity=data["native_activity"],
                native_step=float(data["native_step"]),
                samples=tuple(json.loads(str(data["samples"]))) if "samples" in data else (),
                centroid_of_track=data.get("centroid_of_track"),
                centroid_weights=data.get("centroid_weights"),
                single_speaker_log_bayes=_finite(data.get("single_speaker_log_bayes")),
                log_bayes_embeddings=None if counted < 0 else counted,
                timeline=None if bound[0] < 0 else (int(bound[0]), int(bound[1])),
                slot_grid=_present(data, "slot_grid"),
                slot_probs=_present(data, "slot_probs"),
                hard=_present(data, "hard"),
            )


def read_if_present(path: str | Path, *, tracks=None) -> "ClusterEvidence | None":
    """The evidence beside a timeline, or None.

    ⚠ Fail-soft on purpose: every consumer of this is an extra -- a hint, a veto's input, a
    proofreading mark. A corrupt, half-written or stale sidecar costs that extra, never the run,
    and the file is recomputable by deleting **the timeline** and rerunning (see `timeline_key`).

    Pass `tracks` -- the timeline being read alongside -- and a sidecar that does not belong to it
    is treated as absent. ⚠ A sidecar saved without a binding is also treated as absent, because
    the alternative is trusting columns nobody can check.
    """
    location = Path(path)
    if not location.exists():
        return None
    try:
        evidence = ClusterEvidence.load(location)
    except Exception:  # noqa: BLE001 - see the docstring
        return None
    if tracks is not None and evidence.timeline != timeline_key(tracks):
        return None
    return evidence


def evidence_path(diar_json: str | Path) -> Path:
    """Where the sidecar for a given timeline lives."""
    timeline = Path(diar_json)
    return timeline.with_name(timeline.name[: -len("-diar.json")] + SUFFIX)
