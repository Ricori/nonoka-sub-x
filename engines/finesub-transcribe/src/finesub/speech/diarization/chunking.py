"""Chunk geometry for the streaming Sortformer loop.

Kept torch-free so the default suite can cover it: this is the part that decides
which feature frames each step sees, and getting it wrong is silent -- the run
still produces a timeline, just one whose predictions no longer line up with the
embeddings they were computed from.

Mirrors `SortformerModules.streaming_feat_loader` (NeMo).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class SortformerGeometry:
    """The parts of the checkpoint's config that shape the loop."""

    chunk_len: int = 188
    subsampling_factor: int = 8
    chunk_left_context: int = 1
    #: ⚠ **80, not the checkpoint's 1** (owner 2026-09-20). How much FUTURE audio each chunk may
    #: see: 80 x 8 feature frames x 10 ms = 6.4 s instead of 0.08 s. We batch whole files, so lookahead costs nothing a user can feel:
    #: measured over 16 files it is 5.4 min against 5.6 for the old value, at an identical
    #: 0.90 GiB peak.
    #: ⚠⚠ **The evidence is thin and points in a specific direction, so read it before changing
    #: this back or forward.** `base_right80` against the shipping geometry: on the 7-file
    #: development set B³ F1 **+5.67 pp [+0.16, +12.14]** and `fixed` +8.55 crossing zero; on the
    #: 9-file HELD-OUT set **both cross zero and both point estimates are mildly negative**
    #: (`fixed` −1.36 [−4.30, +1.15], F1 −2.26 [−6.50, +1.60]). So: no measured regression, no
    #: measured benefit either, and the one significant gain sits on the set the knob was picked
    #: on. That is the **third** time this chain has produced a development-set gain that did not
    #: reproduce (`high_right80` F1 +3.80, `refresh` +2.51). The owner shipped it knowing this;
    #: it is not a result to cite as "right80 is better".
    chunk_right_context: int = 80
    fifo_len: int = 0
    spkcache_len: int = 188
    spkcache_update_period: int = 188
    n_spk: int = 4
    emb_dim: int = 512
    feat_dim: int = 128
    sample_rate: int = 16000

    @property
    def chunk_frames(self) -> int:
        """Feature frames per step, before context is added."""
        return self.chunk_len * self.subsampling_factor


@dataclass(frozen=True)
class ChunkWindow:
    """One step's slice of the feature sequence.

    `start`/`end` bound the chunk proper; `left`/`right` are the context frames
    taken from either side, so the window handed to the model is
    ``features[start - left : end + right]``.
    """

    index: int
    start: int
    end: int
    left: int
    right: int
    valid_frames: int
    """How many frames of the window are real audio rather than padding."""

    @property
    def width(self) -> int:
        return self.end - self.start + self.left + self.right


def chunk_windows(total_frames: int, valid_frames: int, geometry: SortformerGeometry) -> Iterator[ChunkWindow]:
    """Walk the feature sequence the way NeMo's loader does.

    `total_frames` is the padded width (the front end pads to a multiple of 16),
    `valid_frames` how much of it is audio. Both matter: the loop covers the
    padding, but the model is told where the audio stops.
    """
    stride = geometry.chunk_frames
    start = 0
    index = 0
    while start < total_frames:
        left = min(geometry.chunk_left_context * geometry.subsampling_factor, start)
        end = min(start + stride, total_frames)
        right = min(geometry.chunk_right_context * geometry.subsampling_factor, total_frames - end)
        width = end - start + left + right
        valid = max(0, min(valid_frames - start + left, width))
        yield ChunkWindow(index=index, start=start, end=end, left=left, right=right, valid_frames=valid)
        start = end
        index += 1


#: The front end's hop at 16 kHz: one feature frame per 10 ms.
FEATURE_HOP_SAMPLES = 160
#: The front end pads its output to a multiple of this; padded frames are zeros.
FEATURE_PAD_TO = 16



def feature_frame_counts(samples: int) -> tuple[int, int]:
    """``(total, valid)`` feature frames the front end returns for `samples` of audio.

    The STFT is centred, so `samples` give ``samples // hop + 1`` frames, padded up to a multiple of
    `FEATURE_PAD_TO`; the front end reports ``samples // hop`` of them as valid. Checked against the
    exported artifact on fourteen lengths from 1 sample to 5,065,295.
    """
    valid = samples // FEATURE_HOP_SAMPLES
    total = -(-(valid + 1) // FEATURE_PAD_TO) * FEATURE_PAD_TO
    return total, valid


@dataclass(frozen=True)
class FeatureSegment:
    """One call to the front end on a long file.

    The audio handed over is ``[sample_start, sample_end)``; of the frames that call returns, keep
    ``[keep_from, keep_from + frame_count)``, which land at whole-file frames
    ``[frame_start, frame_start + frame_count)``.
    """

    sample_start: int
    sample_end: int
    keep_from: int
    frame_start: int
    frame_count: int


def feature_segments(
    samples: int,
    *,
    segment_frames: int = 60_000,
    context_frames: int = 1_000,
) -> list[FeatureSegment]:
    """Tile the valid feature frames of a long file into bounded front-end calls.

    Each call gets `context_frames` of real audio on both sides of what it keeps, so no kept frame is
    within reach of that call's own edge padding (the STFT window is 512 samples, about three frames; the
    default ten seconds of context is deliberately far more). At the very start and end of the file there
    is no audio to borrow -- but the whole-file call pads exactly the same edges, so those frames are
    computed from the same samples either way.

    Frames past `valid` are the front end's zero padding and are not produced here; the caller fills them.
    Every boundary is a multiple of the hop, which is what keeps a segment's frame grid on the whole file's:
    context that is not a multiple of the hop shifts every frame by a fraction and nothing matches at all.
    """
    if segment_frames <= 0 or context_frames < 0:
        raise ValueError("segment_frames must be positive and context_frames non-negative")
    _, valid = feature_frame_counts(samples)
    hop = FEATURE_HOP_SAMPLES
    out = []
    for frame_start in range(0, valid, segment_frames):
        frame_end = min(frame_start + segment_frames, valid)
        first_frame = max(0, frame_start - context_frames)
        sample_start = first_frame * hop
        sample_end = min(samples, (frame_end + context_frames) * hop)
        out.append(
            FeatureSegment(
                sample_start=sample_start,
                sample_end=sample_end,
                keep_from=frame_start - first_frame,
                frame_start=frame_start,
                frame_count=frame_end - frame_start,
            )
        )
    return out


def pre_encoded_length(frames: int, subsampling_factor: int) -> int:
    """Frames out of the three stride-2 convolutions, for `frames` in."""
    length = frames
    for _ in range(int(math.log2(subsampling_factor))):
        length = (length - 1) // 2 + 1
    return length


def chunk_prediction_span(
    window: ChunkWindow, encoded_width: int, encoded_valid: int, geometry: SortformerGeometry
) -> tuple[int, int, int]:
    """Where this step's own predictions sit, in pre-encoded frames.

    Returns ``(left_context, right_context, length)``. The length is bounded by
    the *valid* encoded length rather than the tensor width: the exported core
    concatenates by length, so its predictions cover only the valid part, and
    taking the width instead lets the embeddings outrun their predictions on the
    final short chunk -- a mismatch that stays silent until a step happens to
    contain a silence frame.
    """
    left = round(window.left / geometry.subsampling_factor)
    right = math.ceil(window.right / geometry.subsampling_factor)
    length = min(encoded_width, encoded_valid) - left - right
    return left, right, max(0, length)
