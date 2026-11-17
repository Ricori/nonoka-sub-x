"""The Nemotron-3 + vbx adapter: the default backend, and one that may be used commercially.

Two halves. **Nemotron-3-Diarization** (`nemotron3_model`) gives frame-level activity for up to
eight arrival-ordered tracks -- boundaries, overlap, who is locally different from whom. **vbx**
then gives the global identity: that timeline, smoothed and cut into DiariZen's 16 s / 1.6 s
windows, stands in for DiariZen's local segmentation, and the clustering half of the DiariZen
port (`diarizen_embed` / `diarizen_cluster` / `diarizen_assemble`: WeSpeaker ResNet34 voiceprints,
PLDA, VBx) runs on it unchanged. DiariZen's own WavLM segmentation takes no part.

Why both: on its own Nemotron-3 collapses similar voices into one track and cannot hold more than
eight people; the voiceprints split them again (魔裁 one-to-one 60.3% against 48.1% alone). Why
not DiariZen: less hand fixing on both in-domain corpora, about 3.5 times faster, and every part
may be used commercially. What it gives up: detection on far-field meetings (AliMeeting miss 14%
against DiariZen's 6%). All of it is in `docs/diarization-findings.md` 2.6.

⚠ The vbx half is a faithful copy of the chain that was measured (`tools/diarization_explore/over8.py`,
`method_vbx` with the production voiceprint configuration), not a redesign: the smoothing that
rasterises the activity, how the windows are cut, which (window, track) pairs get a voiceprint.
Each of those decides which embeddings the clustering sees.

⚠ Identity evidence comes out in the same shape as DiariZen's (`ClusterEvidence`), so hints
and samples need nothing new.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from .backend import ARTIFACT_ENV, Backend, BackendOutput, DiarizationUnavailable, artifact_candidates
from .evidence import ClusterEvidence

MODEL_NAME = "nvidia/Nemotron-3-Diarization"

#: Manifest ids (`model-manifest.json`). The model comes straight from NVIDIA's repository at a
#: pinned revision; the voiceprint model and PLDA from ours, both builds in one repository.
HOSTED_MODEL = "nemotron3"
HOSTED_EMBEDDING = "speaker-embedding"

MODEL_FILES = ("config.json", "model.safetensors")
PLDA_FILES = ("plda/plda.npz", "plda/xvec_transform.npz")


def embedding_file(device: str) -> str:
    """The voiceprint TorchScript for `device`: a traced graph keeps the device it was traced on."""
    return "wespeaker-cuda.ts" if str(device).startswith("cuda") else "wespeaker-cpu.ts"


def required_files(device: str) -> tuple[str, ...]:
    return (*MODEL_FILES, embedding_file(device), *PLDA_FILES)


#: The windows vbx cuts the timeline into: DiariZen's segmentation geometry, which the clustering
#: half was built for. `diarizen_embed` and `diarizen_assemble` read the same two numbers.
WINDOW_SECONDS = 16.0
STEP_SECONDS = 1.6

#: The rate the activity comes back at: the clustering half's native 20 ms.
NATIVE_STEP = 0.02


@dataclass(frozen=True)
class Handle:
    """Where each piece is. The model and the voiceprints come from different repositories."""

    model_dir: Path
    embedding: Path
    plda_dir: Path


def _local(directory: Path, device: str) -> Handle:
    return Handle(directory, directory / embedding_file(device), directory / "plda")


def _already_fetched() -> bool:
    """Whether both hosted pieces are on disk -- a stat, so the notice fires only on a fetch."""
    try:
        from finesub.paths import resolve_managed_app_paths
        from finesub_bootstrap.model_ensure import pinned_snapshot_dir

        paths = resolve_managed_app_paths()
        root = paths.models if paths else None
        return all(pinned_snapshot_dir(entry, models_root=root) is not None
                   for entry in (HOSTED_MODEL, HOSTED_EMBEDDING))
    except Exception:  # noqa: BLE001 - worst case is one notice too many
        return False


def _require_loader() -> None:
    try:
        import safetensors.torch  # noqa: F401
    except ImportError as error:
        raise DiarizationUnavailable(f"speaker diarization needs the [asr] extra: {error}") from error


def ensure(artifacts: str | Path | None, device: str = "cuda") -> Handle:
    """Every file the run opens: a local directory if one is named, else the hosted copies.

    A directory given explicitly or through `FINESUB_DIARIZATION_ARTIFACTS` wins when it holds
    all of `required_files(device)`. Otherwise both hosted pieces are fetched once (mirror first,
    sha256-verified against the manifest) and reused from the Hugging Face cache after that.

    ⚠ Every file is checked before anything loads: a gap found later surfaces as a bare exception
    from torch or numpy, which `_run_diarization_stage` does not catch -- the run would end after
    the ASR instead of losing only the labels. The same goes for `safetensors`, which reaches an
    install only through `transformers` in `[asr]`.
    """
    _require_loader()
    for candidate in artifact_candidates(artifacts):
        if all((candidate / name).exists() for name in required_files(device)):
            return _local(candidate, device)
    from ..runtime import hf_weights
    from finesub_bootstrap.hf_verify import VerificationMismatch

    if not _already_fetched():
        from ...reporting import current_reporter

        current_reporter().warning(
            "nemotron3-download",
            "首次使用：正在下载说话人分离模型（Nemotron-3 与声纹模型，约 450 MB，只下这一次）",
            action="说明与许可见 docs/manual/diarization.md",
        )
    directories = {}
    for entry in (HOSTED_MODEL, HOSTED_EMBEDDING):
        try:
            directories[entry] = hf_weights.prepare_directory(entry)
        except VerificationMismatch as error:
            # Fetched, hashed, and wrong: a corrupt model must not load, and the stage catches only
            # `DiarizationUnavailable` -- the labels go, the subtitles do not.
            raise DiarizationUnavailable(
                f"the downloaded speaker model ({entry}) failed verification: {error}") from error
        if directories[entry] is None:
            raise DiarizationUnavailable(
                f"could not fetch the speaker model ({entry}); check the network or the download "
                f"mirror setting, or point {ARTIFACT_ENV} at a directory holding "
                f"{', '.join(required_files(device))}")
    embedding = directories[HOSTED_EMBEDDING]
    return Handle(directories[HOSTED_MODEL], embedding / embedding_file(device), embedding / "plda")


def rasterised(probabilities, *, samples: int, source_step: float, sample_rate: int = 16000):
    """The model's probabilities, smoothed as production smooths them, as 0/1 on the 20 ms grid.

    `segments_from_activity` with its default parameters at the model's own rate, then each
    surviving segment painted onto `ceil(seconds / 0.02)` frames.
    """
    import numpy as np

    from .segments import segments_from_activity

    frames = int(np.ceil(samples / sample_rate / NATIVE_STEP))
    grid = np.zeros((frames, probabilities.shape[1]), dtype=np.float32)
    for segment in segments_from_activity(probabilities.tolist(), frame_seconds=source_step):
        grid[int(round(segment.start / NATIVE_STEP)):int(round(segment.end / NATIVE_STEP)), segment.track] = 1.0
    return grid


def windows(grid):
    """`(chunks, frames_per_window, tracks)`: the 20 ms map cut the way DiariZen's segmentation is.

    Chunk `c` starts at `closest_frame(c * step + half a frame)` -- the frame `diarizen_assemble`
    will put it back at -- and runs one window's worth of frames, zero past the end.
    """
    import numpy as np

    from . import diarizen_assemble

    per_window = int(round(WINDOW_SECONDS / diarizen_assemble.FRAME_STEP))
    duration = grid.shape[0] * diarizen_assemble.FRAME_STEP
    chunks = max(1, int(np.ceil(max(0.0, duration - WINDOW_SECONDS) / STEP_SECONDS)) + 1)
    local = np.zeros((chunks, per_window, grid.shape[1]), dtype=np.float32)
    for index in range(chunks):
        start = diarizen_assemble.closest_frame(index * STEP_SECONDS + 0.5 * diarizen_assemble.FRAME_DURATION)
        piece = grid[max(0, start):start + per_window]
        local[index, :len(piece)] = piece
    return local


def frame_activity(waveform, *, handle: Handle, device):
    """Nemotron-3's `(frames, 8)` activity at 10 ms, with the network loaded and released here."""
    import torch

    from . import nemotron3_model

    network = nemotron3_model.load(handle.model_dir, device)
    try:
        return nemotron3_model.speaker_probabilities(network, waveform, device=device)
    finally:
        # ⚠ Released before the voiceprints load: a batch run builds one of these per file, and
        # torch's caching allocator never hands blocks back on its own (`diarizen_runtime.loaded_model`).
        del network
        if str(device).startswith("cuda"):
            torch.cuda.empty_cache()


def vbx(probabilities, waveform, *, handle: Handle, device, max_speakers=None):
    """`(native, clustered, grid)`: the 20 ms 0/1 map, the clustering behind it, and the rasterised slot
    activity the clustering's windows were cut from."""
    import numpy as np

    from . import diarizen_assemble, diarizen_cluster, diarizen_embed, nemotron3_model

    grid = rasterised(probabilities, samples=int(waveform.shape[-1]), source_step=nemotron3_model.FRAME_SECONDS)
    local = windows(grid)
    embeddings = diarizen_embed.embed(handle.embedding, waveform, local, device=device, only_active=True)
    clustered = diarizen_cluster.cluster(embeddings, local, handle.plda_dir, max_speakers=max_speakers)
    return diarizen_assemble.assemble(local, clustered.hard).astype(np.float32), clustered, grid


def activity(waveform, *, handle: Handle, device, max_speakers=None) -> BackendOutput:
    """Nemotron-3 activity, then voiceprints and VBx over its windows, to one 20 ms map."""
    import torch

    from . import marks, nemotron3_model
    from .samples import pick_samples

    probabilities = frame_activity(waveform, handle=handle, device=device)
    native, clustered, grid = vbx(probabilities, waveform, handle=handle, device=device,
                                  max_speakers=max_speakers)

    # ⚠ `slot_grid`, `slot_probs` and `hard` are what the per-line confidence marks read (`marks.py`): the
    # slot activity the clustering saw, Nemotron's probabilities on the native grid, and which track each
    # (window, slot) went to. Nothing else keeps them once this function returns.
    evidence = ClusterEvidence(soft=clustered.soft, centroids=clustered.centroids,
                               native_activity=native, native_step=NATIVE_STEP,
                               centroid_of_track=clustered.centroid_of_track,
                               centroid_weights=clustered.centroid_weights,
                               single_speaker_log_bayes=clustered.single_speaker_log_bayes,
                               log_bayes_embeddings=clustered.log_bayes_embeddings,
                               slot_grid=grid, hard=clustered.hard,
                               slot_probs=marks.resample(probabilities, nemotron3_model.FRAME_SECONDS,
                                                         native.shape[0]))
    picks = pick_samples(native, step=NATIVE_STEP, evidence=evidence, chunk_step=STEP_SECONDS)
    return BackendOutput(
        torch.from_numpy(native),
        replace(evidence, samples=tuple(
            {"track": s.track, "start": s.start, "end": s.end, "closeness": s.closeness}
            for s in picks
        )),
    )


BACKEND = Backend(
    name="nemotron3",
    model=MODEL_NAME,
    frame_seconds=NATIVE_STEP,
    ensure=ensure,
    activity=activity,
)
