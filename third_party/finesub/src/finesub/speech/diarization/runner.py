"""Run streaming Sortformer from exported TorchScript -- no `nemo_toolkit`.

Three artifacts, produced once by `tools/sortformer_export/`:

    sortformer_preprocessor.ts   128-mel front end
    sortformer_core.ts           pre_encode + Conformer + Transformer + head
    sortformer_cache_update.ts   the synchronous `streaming_update`

and this loop, which mirrors `SortformerEncLabelModel.forward_streaming` for the
synchronous path. Everything the update itself decides -- the silence threshold,
when the speaker cache is over its cap, how its predictions first come into
existence -- lives inside the third artifact, calling NVIDIA's own methods, so
this file never restates any of it.

Output is frame-level activity: `(frames, 4)` probabilities, one row per 80 ms.
Turning that into subtitle-aligned segments is P11's job, not this module's.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import torch

from ..runtime.native_paths import load_torchscript
from .chunking import (
    SortformerGeometry,
    chunk_prediction_span,
    chunk_windows,
    feature_frame_counts,
    feature_segments,
)

#: One pre-encoded frame, in seconds: 10 ms per feature frame times the
#: encoder's subsampling factor.
FRAME_SECONDS = 0.08

ARTIFACT_NAMES = (
    "sortformer_preprocessor.ts",
    "sortformer_core.ts",
    "sortformer_cache_update.ts",
)


class DiarizationArtifactsMissing(RuntimeError):
    """The exported model is not where we were told to look."""


@dataclass
class _StreamState:
    """What carries across chunks. Mirrors NeMo's `StreamingSortformerState`."""

    spkcache: torch.Tensor
    spkcache_preds: torch.Tensor
    has_preds: bool
    fifo: torch.Tensor
    fifo_preds: torch.Tensor
    mean_sil_emb: torch.Tensor
    n_sil_frames: torch.Tensor


class SortformerRunner:
    """Loads the exported pieces, and `diarize()` may then be called repeatedly.

    ⚠ **Close it when done.** The three modules are about 300 MB of GPU memory, and the caller in
    `stage.py` builds a fresh runner per file -- so a batch run, which handles N files in one
    process, otherwise stacks N sets of them in torch's caching allocator, which never returns
    blocks to the driver on its own.

    ⚠ On Windows that failure is silent. Once the dedicated side fills, the driver spills into
    SHARED memory and the run keeps going over PCIe, an order of magnitude slower, with nothing
    raised and `nvidia-smi` still showing only the dedicated side at a flat high-water mark that
    reads as a healthy plateau. It was found on the DiariZen backend, where the same shape put
    15.8 of 16.3 GiB on the dedicated side and 8 GiB in shared memory after seven files.
    """

    def __init__(
        self,
        artifact_dir: str | Path,
        *,
        device: str | torch.device = "cuda",
        geometry: SortformerGeometry | None = None,
    ) -> None:
        directory = Path(artifact_dir)
        missing = [name for name in ARTIFACT_NAMES if not (directory / name).exists()]
        if missing:
            raise DiarizationArtifactsMissing(
                f"{directory} is missing {', '.join(missing)} -- produce them with "
                "tools/sortformer_export/export_sortformer.py"
            )
        self.device = torch.device(device)
        self.geometry = geometry or SortformerGeometry()
        self._featurizer = load_torchscript(directory / ARTIFACT_NAMES[0], map_location=self.device)
        self._core = load_torchscript(directory / ARTIFACT_NAMES[1], map_location=self.device)
        self._cache_update = load_torchscript(directory / ARTIFACT_NAMES[2], map_location=self.device)
        for module in (self._featurizer, self._core, self._cache_update):
            module.eval()

    def close(self) -> None:
        """Drop the three modules and hand their memory back."""
        for name in ("_featurizer", "_core", "_cache_update"):
            if getattr(self, name, None) is not None:
                setattr(self, name, None)
        if self.device.type == "cuda":
            torch.cuda.empty_cache()

    def __enter__(self) -> "SortformerRunner":
        return self

    def __exit__(self, *exception) -> None:
        self.close()

    # -- internals ---------------------------------------------------------

    def _empty(self, width: int, channels: int) -> torch.Tensor:
        return torch.zeros((1, width, channels), device=self.device)

    def _initial_state(self) -> _StreamState:
        geometry = self.geometry
        return _StreamState(
            spkcache=self._empty(0, geometry.emb_dim),
            spkcache_preds=self._empty(0, geometry.n_spk),
            has_preds=False,
            fifo=self._empty(0, geometry.emb_dim),
            fifo_preds=self._empty(0, geometry.n_spk),
            mean_sil_emb=torch.zeros((1, geometry.emb_dim), device=self.device),
            n_sil_frames=torch.zeros((1,), dtype=torch.long, device=self.device),
        )

    def _features(self, waveform: torch.Tensor, samples: int) -> tuple[torch.Tensor, int, int]:
        """``(features, total_frames, valid_frames)`` for the whole waveform, always in segments.

        One call over a whole file costs 2.50 GiB per hour of audio, so eleven hours needed 27.5 GiB and
        spilled a 16 GiB card into Windows shared memory. Segmenting holds it flat at about 1.7 GiB
        whatever the length, which is what lets a 4 GB card run at all and stops a long-lived batch
        process from parking a 10 GiB high-water mark.

        There used to be a fork here -- one call under 90 minutes, segments above it -- so that existing
        short-file timelines stayed bit-identical. It is gone: the two paths are not bit-identical, but
        the difference measured smaller than what a meaningless 1e-7 nudge does to this same chain, so
        keeping two numerical paths to avoid it was not worth the branch. Readings and criteria are in
        the closeout plan's W4 entry -- they do not belong in a docstring, where nothing keeps them
        true. ⚠ Timelines are cached artifacts and never recomputed on demand, so nothing already on
        disk is disturbed by this.
        """
        total, valid = feature_frame_counts(samples)
        source = waveform.to(torch.float32).cpu()
        # Padding frames past `valid` are zeros in the whole-file call too.
        features = torch.zeros((1, self.geometry.feat_dim, total), dtype=torch.float32)
        for segment in feature_segments(samples):
            piece = source[:, segment.sample_start : segment.sample_end].to(self.device)
            part, part_valid = self._featurizer(
                piece, torch.tensor([segment.sample_end - segment.sample_start], device=self.device)
            )
            # The geometry is derived from `feature_frame_counts`, whose formula is pinned against
            # the exported front end on lengths up to about five minutes -- an eleven-hour file
            # extrapolates it. Ask the front end what it actually returned rather than trusting
            # that: a frame of drift here silently pairs embeddings with the wrong predictions.
            expected = feature_frame_counts(segment.sample_end - segment.sample_start)[1]
            if int(part_valid[0]) != expected:
                raise ValueError(
                    f"front end returned {int(part_valid[0])} valid frames for "
                    f"{segment.sample_end - segment.sample_start} samples, geometry expected {expected}"
                )
            kept = part[:, :, segment.keep_from : segment.keep_from + segment.frame_count]
            features[:, :, segment.frame_start : segment.frame_start + segment.frame_count] = kept.cpu()
            del piece, part, kept
        return features, total, valid

    @staticmethod
    def _mask_beyond(preds: torch.Tensor, valid: int) -> torch.Tensor:
        """`SortformerModules.apply_mask_to_preds`, batch of one."""
        if valid >= preds.shape[1]:
            return preds
        masked = preds.clone()
        masked[:, valid:, :] = 0.0
        return masked

    # -- public ------------------------------------------------------------

    @torch.inference_mode()
    def diarize(self, waveform: torch.Tensor, sample_count: int | None = None) -> torch.Tensor:
        """Frame-level speaker activity for one mono 16 kHz waveform.

        Returns `(frames, n_spk)` probabilities. The timeline covers the audio and
        not the front end's padding, which is why it can be up to two frames
        shorter than NeMo's own output for the same file; those frames are silence.
        """
        # The exported front end drops NeMo's two `autocast(enabled=False)` guards,
        # because TorchScript wants a constant device there. Running it under an
        # enabled autocast would therefore compute the spectrogram in half
        # precision, which those guards exist to prevent.
        if torch.is_autocast_enabled():
            raise RuntimeError("diarization must not run under autocast; see tools/sortformer_export/README.md")

        geometry = self.geometry
        if waveform.dim() == 1:
            waveform = waveform.unsqueeze(0)
        if waveform.shape[0] != 1:
            raise ValueError(f"one waveform at a time, got shape {tuple(waveform.shape)}")
        samples = sample_count if sample_count is not None else waveform.shape[1]

        features, total_frames, valid_frames = self._features(waveform, samples)

        state = self._initial_state()
        timeline = self._empty(0, geometry.n_spk)

        for window in chunk_windows(total_frames, valid_frames, geometry):
            slice_start = window.start - window.left
            slice_end = window.end + window.right
            # A no-op when the features already live on the device (the short path), so that path is unchanged.
            chunk = features[:, :, slice_start:slice_end].transpose(1, 2).contiguous().to(self.device)

            preds, chunk_embs, encoded_valid = self._core(
                chunk,
                torch.tensor([window.valid_frames], device=self.device),
                state.spkcache,
                torch.tensor([state.spkcache.shape[1]], device=self.device),
                state.fifo,
                torch.tensor([state.fifo.shape[1]], device=self.device),
            )
            cache_len = state.spkcache.shape[1]
            fifo_len = state.fifo.shape[1]
            preds = self._mask_beyond(preds, cache_len + fifo_len + int(encoded_valid[0]))

            left, _, length = chunk_prediction_span(
                window, int(chunk_embs.shape[1]), int(encoded_valid[0]), geometry
            )
            chunk_embs = chunk_embs[:, left : left + length]
            offset = cache_len + fifo_len + left
            chunk_preds = preds[:, offset : offset + length]

            state.fifo = torch.cat([state.fifo, chunk_embs], dim=1)
            state.fifo_preds = torch.cat(
                [preds[:, cache_len : cache_len + fifo_len], chunk_preds], dim=1
            )

            if fifo_len + length > geometry.fifo_len:
                pop = max(geometry.spkcache_update_period, length - geometry.fifo_len + fifo_len)
                pop = min(pop, fifo_len + length)
                pop_embs, pop_preds = state.fifo[:, :pop], state.fifo_preds[:, :pop]
                state.fifo, state.fifo_preds = state.fifo[:, pop:], state.fifo_preds[:, pop:]
                (
                    state.spkcache,
                    state.spkcache_preds,
                    state.has_preds,
                    state.mean_sil_emb,
                    state.n_sil_frames,
                ) = self._cache_update(
                    state.spkcache,
                    state.spkcache_preds,
                    state.has_preds,
                    state.mean_sil_emb,
                    state.n_sil_frames,
                    pop_embs,
                    pop_preds,
                    preds[:, :cache_len],
                )

            timeline = torch.cat([timeline, chunk_preds], dim=1)

        return timeline.squeeze(0)
