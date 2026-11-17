"""The DiariZen adapter: five ported stages behind one `Backend`.

`segment -> median filter -> embed -> cluster -> assemble`, each in its own `diarizen_*` module,
tied together here. This file is the only place that knows the order; nothing above it knows
there are five stages, and nothing below it knows there is a `Backend`.

⚠ **Production imports neither `pyannote` nor `diarizen`.** It runs the TorchScript that
`tools/diarizen_export/` produces -- one build per device, hosted on Hugging Face and fetched on
first use (`ensure`) -- plus numpy for the clustering half. The checkpoint behind those artifacts
is CC-BY-NC 4.0, and so are the builds.

⚠ This backend is the only one with **identity evidence** -- centroids, per-chunk distances, the
native 20 ms activity. `evidence.py` owns what that is; here it is only assembled and handed up.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .backend import ARTIFACT_ENV, Backend, BackendOutput, DiarizationUnavailable, artifact_candidates
from .evidence import ClusterEvidence

MODEL_NAME = "BUT-FIT/diarizen-wavlm-large-s80-md-v2"

#: Everything `activity` opens. `ensure` stats all of it, because a gap found later surfaces as a
#: bare exception from torch or numpy, which the stage does not catch.
ARTIFACT_NAMES = (
    "diarizen_segmentation.ts",
    "diarizen_embedding.ts",
    "plda/plda.npz",
    "plda/xvec_transform.npz",
)

#: DiariZen's own frame rate. Its receptive field is 20 ms, four times finer than
#: `FRAME_SECONDS` -- see `diarizen_assemble.FRAME_STEP`.
NATIVE_STEP = 0.02


#: The hosted TorchScript builds, one per device (`model-manifest.json`). A traced graph keeps the
#: device it was traced on, in both directions, so the CUDA build cannot serve CPU or vice versa.
HOSTED = {"cuda": "diarizen-ts-cuda", "cpu": "diarizen-ts-cpu"}


def hosted_build(device: str) -> str:
    return HOSTED["cuda" if str(device).startswith("cuda") else "cpu"]


def _already_fetched(build: str) -> bool:
    """Whether the hosted build is on disk already -- a stat, so the notice fires only on a fetch."""
    try:
        from finesub.paths import resolve_managed_app_paths
        from finesub_bootstrap.model_ensure import pinned_snapshot_dir

        paths = resolve_managed_app_paths()
        return pinned_snapshot_dir(build, models_root=paths.models if paths else None) is not None
    except Exception:  # noqa: BLE001 - worst case is one notice too many
        return False


def traced_device(directory: Path) -> str | None:
    """Which device the export in `directory` was traced on, read from the graph itself.

    `torch.jit.trace` bakes the device into the code it records: a CUDA trace of the segmentation
    model carries `cuda:0` constants (23 of them), a CPU trace carries none. That makes the archive
    its own witness -- no metadata that an older export might lack or a copied directory might
    get wrong. `None` when the archive cannot be read.
    """
    import zipfile

    try:
        with zipfile.ZipFile(directory / "diarizen_segmentation.ts") as archive:
            code = b"".join(archive.read(name) for name in archive.namelist() if name.endswith(".py"))
    except (OSError, zipfile.BadZipFile):
        return None
    return "cuda" if b"cuda" in code else "cpu"


def _refuse_other_device(directory: Path, device: str) -> None:
    """A local export traced for the other device fails inside TorchScript with a bare error.

    Checked before anything loads, because that failure is a bare `RuntimeError` the stage does not
    catch -- it would end the run after the ASR. An export whose device cannot be read is refused
    too: loading it is exactly the gamble this exists to avoid.
    """
    traced_on = traced_device(directory)
    wanted = "cuda" if str(device).startswith("cuda") else "cpu"
    if traced_on != wanted:
        what = f"traced on {traced_on}" if traced_on else "of unknown device"
        raise DiarizationUnavailable(
            f"{directory} holds a DiariZen export {what}, and this run uses {device}; "
            "a traced graph only runs on the device it was traced on. Export again with "
            f"--device {wanted}, or unset {ARTIFACT_ENV} to use the hosted build")


def ensure(artifacts: str | Path | None, device: str = "cuda") -> Path:
    """The TorchScript build for `device`: a local export if one is named, else the hosted one.

    A directory given explicitly or through `FINESUB_DIARIZATION_ARTIFACTS` wins and is trusted to
    match the device -- that is the developer's own export. Otherwise the hosted build for this
    device is fetched once (mirror first, sha256-verified against the manifest) and reused from
    the Hugging Face cache after that (owner 2026-09-21: `--diarize` downloads what it needs).

    ⚠ CC-BY-NC 4.0: DiariZen's weights are non-commercial, and so is our conversion of them. The
    notice is printed when a fetch is needed; `docs/manual/diarization.md` says it in full.
    """
    for candidate in artifact_candidates(artifacts):
        # ⚠ Every file the run will open, not just the first one. Checking only the segmentation
        # model once let a run through that then died inside `torch.jit.load` or `np.load` with a
        # bare exception -- which `_run_diarization_stage` does not catch, so the whole pipeline
        # fell over AFTER the ASR was done. A missing model costs the labels, not the transcript.
        if all((candidate / name).exists() for name in ARTIFACT_NAMES):
            _refuse_other_device(candidate, device)
            return candidate
    from ..runtime import hf_weights
    from finesub_bootstrap.hf_verify import VerificationMismatch

    build = hosted_build(device)
    if not _already_fetched(build):
        from ...reporting import current_reporter

        current_reporter().warning(
            "diarizen-download",
            "首次使用：正在下载 DiariZen 说话人模型（约 300 MB，只下这一次）。"
            "它的许可是 CC BY-NC 4.0——仅限非商业使用",
            action="说明见 docs/manual/diarization.md",
        )
    try:
        directory = hf_weights.prepare_directory(build)
    except VerificationMismatch as error:
        # Fetched, hashed, and wrong. `prepare` raises this on purpose (a corrupt model must not
        # be loaded), but it is not what the stage catches -- left alone it would end the run
        # after the ASR. Same promise as a missing model: the labels go, the subtitles do not.
        raise DiarizationUnavailable(f"the downloaded DiariZen model ({build}) failed verification: "
                                     f"{error}") from error
    if directory is not None:
        return directory
    raise DiarizationUnavailable(
        f"could not fetch the DiariZen model ({build}); check the network or the download mirror "
        f"setting, or point {ARTIFACT_ENV} at a local export "
        "(tools/diarizen_export/export_diarizen.py). Or drop --diarize-backend for the default backend"
    )


def activity(waveform, *, handle, device, max_speakers=None) -> BackendOutput:
    """Five stages -- segment, filter, embed, cluster, assemble -- to one activity map.

    ⚠⚠ **Returned at DiariZen's native 20 ms, and that is a measured decision** (A3, 2026-09-20).
    Until then this reduced to the 80 ms product grid with a max first, on the argument that the
    chain's thresholds were calibrated against an 80 ms raster. That argument was tested rather
    than assumed, pre-registered before the run (closeout §2d, `de36883e`): both arms from ONE
    clustering pass, so only the segmenting grid differed.

        姐姐咚咚 x15   edit cost -6.87 [-11.87, -2.00]  fixed +0.18  F1 +0.33  (both significant)
        hina x19       edit cost -3.63 [-8.00, -0.16]   fixed, F1 flat

    Net gain on the owner's rule, and it survives pricing abstention at 2 (-5.93 [-10.87, -1.07])
    -- the native arm leaves ~0.45 more interjections unlabelled per file, so that had to be
    ruled out as the source. The smoothing still applies unchanged: its `min_gap` and
    `min_duration` are in seconds, so they mean the same thing on either grid.

    ⚠ The limit that came with it: the truth axis is itself an 80 ms raster, so the gain can only
    have come through which track wins a line. The finer boundaries themselves were never measured
    -- a coarse truth can hide a loss as easily as a gain, so this is support for the line
    assignment, not a bound on boundary quality (Codex audit 2026-09-21 corrected "cannot be worth
    less").
    """
    import numpy as np
    import torch

    from . import diarizen_assemble, diarizen_cluster, diarizen_embed, diarizen_runner
    from .samples import pick_samples

    # ⚠ `handle` must be the build for `device` (`ensure` picks it): a graph traced on CUDA raises
    # from inside TorchScript on CPU and the reverse, so the device cannot be changed after `ensure`.
    handle = Path(handle)
    local = diarizen_runner.segment(
        handle / "diarizen_segmentation.ts", waveform, device=device
    ).data
    filtered = diarizen_runner.median_filtered(local)
    embeddings = diarizen_embed.embed(
        handle / "diarizen_embedding.ts", waveform, filtered, device=device
    )
    clustered = diarizen_cluster.cluster(embeddings, filtered, handle / "plda",
                                         max_speakers=max_speakers)
    native = diarizen_assemble.assemble(filtered, clustered.hard).astype(np.float32)

    chunk_step = diarizen_runner.SEGMENTATION_DURATION * diarizen_runner.SEGMENTATION_STEP_RATIO
    evidence = ClusterEvidence(soft=clustered.soft, centroids=clustered.centroids,
                               native_activity=native, native_step=NATIVE_STEP,
                               centroid_of_track=clustered.centroid_of_track,
                               centroid_weights=clustered.centroid_weights,
                               single_speaker_log_bayes=clustered.single_speaker_log_bayes,
                               log_bayes_embeddings=clustered.log_bayes_embeddings)
    # ⚠ Chosen here and not later: picking the span needs `soft`, which exists only while the run
    # is happening. Cutting the audio is a few seconds of ffmpeg at any later time, so only the
    # expensive half is stored (`tools/diarizen_export/cut_samples.py` does the cheap half).
    picks = pick_samples(native, step=NATIVE_STEP, evidence=evidence, chunk_step=chunk_step)
    return BackendOutput(
        torch.from_numpy(native),
        replace(evidence, samples=tuple(
            {"track": s.track, "start": s.start, "end": s.end, "closeness": s.closeness}
            for s in picks
        )),
    )


BACKEND = Backend(
    name="diarizen",
    model=MODEL_NAME,
    # ⚠ NOT `FRAME_SECONDS`. The timeline is cut on the native 20 ms grid (see `activity`), and
    # `run_diarization` segments with this value, so the two must agree or every boundary lands at
    # four times its real time.
    frame_seconds=NATIVE_STEP,
    ensure=ensure,
    activity=activity,
)
