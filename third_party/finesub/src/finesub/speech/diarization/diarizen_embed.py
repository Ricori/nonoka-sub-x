"""One speaker embedding per (chunk, local speaker), the way the pipeline asks for them.

Between the local segmentation and the clustering sits a step that is easy to get subtly wrong:
for every 16 s chunk and every one of its four local speakers, embed the audio *weighted by that
speaker's frame mask*. Four details decide whether the numbers match, and none of them announces
itself:

* **Overlap is excluded, with a fallback.** Frames where two or more speakers are active are zeroed
  first; if what remains is too short (`> min_num_frames`, which works out to 2 frames here) the
  speaker's FULL mask is used instead. Skipping the fallback silently embeds near-silence for
  anyone who only ever speaks over somebody.
* **The chunk is cropped with padding, not clipped.** The last chunks run past the end of the file
  and must still be a full 16 s, or the embedder sees a different duration than the mask describes.
* **Embeddings come out in `(chunk, speaker, dimension)` order**, speaker-fastest. Reshaping the
  other way transposes every file.
* **Batch size 2**, matching the research path. Same reason as the segmentation half: this chain
  amplifies last-bit differences, so a batch that is "just throughput" is not.

⚠ NaN is expected in the output. A speaker absent from a chunk gets an all-zero mask, and a
weighted mean over zero weight is NaN. The clustering filters those rows; do not "fix" them here.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from .diarizen_runner import (
    MAX_SPEAKERS_PER_CHUNK,
    SAMPLE_RATE,
    SEGMENTATION_DURATION,
    SEGMENTATION_STEP_RATIO,
)
from .diarizen_runtime import jit_fusion_disabled, loaded_model, tf32_disabled

#: `PyannoteAudioPretrainedSpeakerEmbedding.min_num_samples` for this checkpoint, probed rather
#: than assumed: fewer samples than this and the embedder raises instead of returning something.
MIN_NUM_SAMPLES = 400

#: The research path's `embedding_batch_size`. Not a throughput knob -- see the module docstring.
EMBEDDING_BATCH_SIZE = 2


def chunk_masks(segmentation, *, exclude_overlap: bool = True):
    """`(chunks, frames, speakers)` weights, overlap removed where that leaves enough frames."""
    import numpy as np

    data = np.asarray(segmentation, dtype=np.float32)
    if not exclude_overlap:
        return data
    _, num_frames, _ = data.shape
    num_samples = SEGMENTATION_DURATION * SAMPLE_RATE
    min_num_frames = math.ceil(num_frames * MIN_NUM_SAMPLES / num_samples)
    clean = data * (data.sum(axis=2, keepdims=True) < 2)
    # Per (chunk, speaker): keep the cleaned mask only when something survives it.
    enough = clean.sum(axis=1, keepdims=True) > min_num_frames
    return np.where(enough, clean, data)


def crop_chunk(waveform, index: int, *, window_size: int, step_size: int):
    """The `index`-th chunk, zero-padded when it runs past the end (`mode="pad"`)."""
    import torch

    start = index * step_size
    piece = waveform[..., start : start + window_size]
    missing = window_size - int(piece.shape[-1])
    if missing > 0:
        piece = torch.nn.functional.pad(piece, (0, missing))
    return piece


#: WeSpeaker ResNet34's output width: the shape of an all-NaN answer when nothing was embedded.
EMBEDDING_DIMENSION = 256


def embed(model_path: str | Path, waveform, segmentation, *, device: str = "cuda",
          batch_size: int = EMBEDDING_BATCH_SIZE, exclude_overlap: bool = True,
          only_active: bool = False) -> Any:
    """`(chunks, speakers, dimension)` embeddings for one file.

    `waveform` is `(1, samples)` at 16 kHz; `segmentation` is the `(chunks, frames, speakers)`
    activity for the same audio -- `diarizen_runner.segment`'s, or a timeline cut into the same
    windows (`nemotron3_backend`).

    `only_active` embeds only the (chunk, speaker) pairs whose mask holds more than the minimum
    number of frames and leaves the rest NaN, which is how the Nemotron-3 chain was measured
    (`tools/diarization_explore/over8.py`, `embed_local`): with eight slots most pairs are silent,
    and embedding them costs twice the time for rows the clustering throws away. ⚠ It is not a
    free switch for DiariZen: batches are formed from the pairs that are embedded, and batch
    composition changes the answer on this chain (module docstring).
    """
    import numpy as np
    import torch

    from .diarizen_features import compute_fbank

    masks = chunk_masks(segmentation, exclude_overlap=exclude_overlap)
    chunks, num_frames, speakers = masks.shape
    window_size = int(SEGMENTATION_DURATION * SAMPLE_RATE)
    step_size = round(SEGMENTATION_DURATION * SEGMENTATION_STEP_RATIO * SAMPLE_RATE)
    min_num_frames = math.ceil(num_frames * MIN_NUM_SAMPLES / (SEGMENTATION_DURATION * SAMPLE_RATE))
    out = np.full((chunks, speakers, EMBEDDING_DIMENSION), np.nan, dtype=np.float32)

    with (loaded_model(model_path, device) as model, jit_fusion_disabled(),
          tf32_disabled(), torch.inference_mode()):
        audio = waveform.to(device)
        pending_audio: list = []
        pending_mask: list = []
        pending_index: list = []

        def flush():
            if not pending_audio:
                return
            fbank = compute_fbank(torch.stack(pending_audio))
            weights = torch.stack(pending_mask)
            for (index, speaker), vector in zip(pending_index, model(fbank, weights).cpu().numpy()):
                out[index, speaker] = vector
            pending_audio.clear()
            pending_mask.clear()
            pending_index.clear()

        for index in range(chunks):
            piece = crop_chunk(audio, index, window_size=window_size, step_size=step_size)
            for speaker in range(speakers):
                if only_active and masks[index, :, speaker].sum() <= min_num_frames:
                    continue
                pending_audio.append(piece)
                pending_mask.append(
                    torch.from_numpy(masks[index, :, speaker]).to(device)
                )
                pending_index.append((index, speaker))
                if len(pending_audio) == batch_size:
                    flush()
        flush()
        del audio

    return out


__all__ = ["chunk_masks", "crop_chunk", "embed", "MAX_SPEAKERS_PER_CHUNK"]
