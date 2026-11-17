"""DiariZen's local segmentation: slide the exported model over a waveform, decode the powerset.

The research path reaches this through a vendored `pyannote.audio` fork -- `Inference.slide` plus
`Powerset.to_multilabel`. Production may not carry that fork, so the loop lives here. It is a
faithful reimplementation, not an improvement: the acceptance for this port is bit-exact agreement
with the frozen research outputs, so every choice below copies one.

Three of those choices are load-bearing and silent when wrong:

* **The last chunk is padded with zeros, not dropped and not reflected.** A file is almost never a
  whole number of steps, so this branch runs nearly every time.
* **The powerset decode is hard by default.** `argmax` over the 16 powerset classes, one-hot, then
  a matrix multiply by the subset-membership table. Taking a softmax instead -- the obvious
  "probability" reading -- produces plausible fractional activity that is simply not what the
  pipeline saw.
* **Subsets are enumerated by size, then `itertools.combinations` order.** The table is
  `{}, {0}, {1}, {2}, {3}, {0,1}, {0,2}, ...`, and any other ordering silently permutes speakers
  within every chunk.

Output is `(chunks, frames, speakers)` with a 16 s window stepping 1.6 s -- the chunk-level stack,
NOT an overlap-aggregated timeline. The pipeline aggregates later, after clustering has matched
speakers across chunks, and doing it here would destroy the per-chunk permutation freedom that
step depends on.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any

from .diarizen_runtime import jit_fusion_disabled, loaded_model, tf32_disabled

#: `config.toml`'s `[inference.args]`. Named here because the window geometry and the artifact have
#: to agree, and a caller passing its own duration would silently mismatch the exported model.
SEGMENTATION_DURATION = 16.0
SEGMENTATION_STEP_RATIO = 0.1
SAMPLE_RATE = 16000

#: `[model.args]`: four speakers per chunk, at most four at once -> 16 powerset classes.
MAX_SPEAKERS_PER_CHUNK = 4
MAX_SPEAKERS_PER_FRAME = 4


@dataclass(frozen=True)
class LocalSegmentation:
    """`(chunks, frames, speakers)` activity, and the window it was produced with."""

    data: Any
    duration: float
    step: float

    @property
    def chunks(self) -> int:
        return int(self.data.shape[0])


def powerset_mapping(num_classes: int = MAX_SPEAKERS_PER_CHUNK,
                     max_set_size: int = MAX_SPEAKERS_PER_FRAME):
    """`(powerset_classes, classes)` membership table, in the order the model was trained with.

    ⚠ Size-major, then `combinations` order. Sorting the subsets any other way, or enumerating
    them by counting in binary, gives a table of the right shape that permutes speakers inside
    every chunk -- which downstream clustering then happily "fixes" into a wrong answer.
    """
    import torch

    rows = [current for size in range(max_set_size + 1)
            for current in combinations(range(num_classes), size)]
    mapping = torch.zeros(len(rows), num_classes)
    for index, members in enumerate(rows):
        if members:
            mapping[index, list(members)] = 1.0
    return mapping


def to_multilabel(powerset, mapping):
    """Hard powerset decode: argmax, one-hot, then membership.

    ⚠ Hard, not soft. The pipeline calls this with `soft=False`, which means the winning subset
    takes the frame outright; a softmax here would be a different model.
    """
    import torch

    hot = torch.nn.functional.one_hot(
        torch.argmax(powerset, dim=-1), mapping.shape[0]
    ).float()
    return torch.matmul(hot, mapping)


#: `[inference.args] apply_median_filtering`. 11 frames along the time axis, reflected at the edges.
MEDIAN_FILTER_FRAMES = 11


def median_filtered(data):
    """The smoothing DiariZen applies between segmentation and everything downstream.

    ⚠ It sits AFTER `get_segmentations` and BEFORE the embeddings, which is why the frozen
    `diarizen-local.npz` -- captured at the return of `get_segmentations` -- does NOT have it.
    Feeding that cache straight into the embedder reproduces neither: the masks are then 11 frames
    rougher than the ones the research path embedded with, and the embeddings came out at a cosine
    of 0.997 instead of matching.
    """
    from scipy.ndimage import median_filter

    return median_filter(data, size=(1, MEDIAN_FILTER_FRAMES, 1), mode="reflect")


def _chunk_counts(samples: int, *, window_size: int, step_size: int) -> tuple[int, bool]:
    """`(complete, has_last)` -- how `Inference.slide` divides a waveform of this length."""
    complete = 1 + (samples - window_size) // step_size if samples >= window_size else 0
    has_last = samples < window_size or (samples - window_size) % step_size > 0
    return complete, has_last


def _padded_tail(waveform, *, complete: int, window_size: int, step_size: int):
    import torch.nn.functional as functional

    tail = waveform[:, complete * step_size:]
    return functional.pad(tail, (0, window_size - int(tail.shape[-1])))[None]


def chunk_batches(waveform, *, window_size: int, step_size: int, batch_size: int, device=None):
    """Yield the chunks `chunk_waveform` would, one batch at a time, without materialising them.

    ⚠ `unfold` returns a **view**; making the whole thing contiguous costs `window_size /
    step_size` times the audio -- ten times at 16 s / 1.6 s. Doing that on the GPU is what put
    6.9 GiB on the card for a 3 h file (0.69 GB of audio), and on Windows that does not fail: the
    driver spills into shared memory and the run crawls (`docs/bench-discipline.md`, 第七条).
    Slicing per batch makes the peak one batch instead.

    ⚠ **The partition is identical to batching `chunk_waveform`'s output**, padded tail included.
    That is not tidiness -- batch composition changes the answer on this chain (batches of 8 and
    32 flip 0.0117% of frames), so a tail chunk that used to share a batch with complete chunks
    must still share it.
    """
    import torch

    samples = int(waveform.shape[-1])
    complete, has_last = _chunk_counts(samples, window_size=window_size, step_size=step_size)
    view = waveform.unfold(1, window_size, step_size).permute(1, 0, 2) if complete else None

    total = complete + int(has_last)
    for start in range(0, total, batch_size):
        stop = min(start + batch_size, total)
        pieces = []
        if start < complete:
            pieces.append(view[start:min(stop, complete)])
        if has_last and stop > complete:
            pieces.append(_padded_tail(waveform, complete=complete,
                                       window_size=window_size, step_size=step_size))
        batch = pieces[0] if len(pieces) == 1 else torch.cat(pieces, dim=0)
        batch = batch.contiguous()
        yield batch.to(device) if device is not None else batch


def chunk_waveform(waveform, *, window_size: int, step_size: int):
    """`(chunks, 1, window)` plus whether the final chunk was zero-padded.

    Mirrors `Inference.slide`: whole chunks by `unfold`, and one final chunk padded with zeros when
    the remainder does not land on a step boundary.

    ⚠ **Materialises the whole overlapped copy** -- ten times the audio at this geometry. Fine for
    a test or a probe; a run should use `chunk_batches`, which yields the same batches one at a
    time. See that function for what the difference costs.
    """
    import torch

    samples = int(waveform.shape[-1])
    complete, has_last = _chunk_counts(samples, window_size=window_size, step_size=step_size)
    if complete:
        chunks = waveform.unfold(1, window_size, step_size).permute(1, 0, 2).contiguous()
    else:
        chunks = waveform.new_zeros((0, waveform.shape[0], window_size))
    if has_last:
        chunks = torch.cat([chunks, _padded_tail(waveform, complete=complete,
                                                 window_size=window_size,
                                                 step_size=step_size)], dim=0)
    return chunks, has_last


#: ⚠ The research path runs at 2 and every frozen reading came from that. This is NOT a neutral
#: throughput knob: measured on 64 chunks, batches of 8 and 32 flip 0.0117% of frames outright
#: (max|delta| 1.0), because the hard powerset argmax turns a last-bit float difference into a
#: different winning subset. Raising it is an execution-form change in the sense of `README_DEV.md` 开发原则's
#: A/B rule -- allowed, but it owes its own characterisation, and it must not be raised silently
#: while something is being compared against the research path.
DEFAULT_BATCH_SIZE = 2


def segment(model_path: str | Path, waveform, *, device: str = "cuda",
            batch_size: int = DEFAULT_BATCH_SIZE):
    """Slide the exported segmentation model over `(channels, samples)` audio."""
    import torch

    mapping = powerset_mapping().to(device)

    window_size = int(SEGMENTATION_DURATION * SAMPLE_RATE)
    step_size = round(SEGMENTATION_DURATION * SEGMENTATION_STEP_RATIO * SAMPLE_RATE)

    pieces = []
    # ⚠ The waveform stays on the host and each batch is moved as it is needed. Moving it first
    # and slicing there is the same arithmetic, but it puts the whole ten-times-overlapped copy
    # on the card -- 6.9 GiB for a 3 h file.
    with (loaded_model(model_path, device) as model, jit_fusion_disabled(),
          tf32_disabled(), torch.inference_mode()):
        for batch in chunk_batches(waveform, window_size=window_size, step_size=step_size,
                                   batch_size=batch_size, device=device):
            pieces.append(to_multilabel(model(batch), mapping).cpu())
            del batch
    del mapping
    data = torch.cat(pieces, dim=0) if pieces else torch.zeros((0, 0, MAX_SPEAKERS_PER_CHUNK))
    return LocalSegmentation(
        data=data.numpy(),
        duration=SEGMENTATION_DURATION,
        step=SEGMENTATION_DURATION * SEGMENTATION_STEP_RATIO,
    )
