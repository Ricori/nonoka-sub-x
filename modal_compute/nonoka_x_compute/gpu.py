"""The Whisper GPU container, the deployment's only GPU pipeline leg besides Vocal.

Everything this container actually executes lives in `phases`, which the CPU
scheduler runs the other legs of in its own process. All that is left here is
the resource envelope: an L4, held for one short pass and released two seconds
later.
"""

from __future__ import annotations

from .images import gpu_worker_image
from .phases import run_pipeline_phase
from .resources import (
    WORKER_GPU,
    app,
    control_secret,
    huggingface_secret,
    models_volume,
    scratch_volume,
)


@app.function(
    image=gpu_worker_image,
    gpu=WORKER_GPU,
    # Host-side VAD and Qwen finalization run in the CPU scheduler. This GPU
    # worker is retained for the patched CTranslate2 Whisper pass and, when a
    # task asks for it, the speaker timeline right after it (FineSub 0.6.0
    # diarization: ~1 GB of VRAM, but the whole vocal track in host memory --
    # about 460 MB of float32 for a two-hour task -- beside the model).
    cpu=2.0,
    memory=3072,
    scaledown_window=2,
    max_containers=2,
    timeout=60 * 60 * 3,
    volumes={"/data": scratch_volume, "/models": models_volume},
    secrets=[huggingface_secret, control_secret],
    # Deliberately no retries -- see the scheduler's own decorator for why.
)
def run_aligned_stage(
    owner_id: str,
    job_id: str,
    request: dict,
    run_id: str,
) -> None:
    """Run the aligned (Whisper) stage, plus the speaker timeline when asked.

    Takes no stage argument: separation runs on `VocalSeparator`, whose block
    plan, AOTI packages and 16 kHz delivery this worker has none of.
    """

    run_pipeline_phase(owner_id, job_id, request, run_id, phase="gpu")
