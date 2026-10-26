"""Ephemeral maintenance containers: the Vocal separator cache, the diarization weights."""

from __future__ import annotations

import json

import modal

from ..images import VOCAL_MODEL, gpu_worker_image, vocal_accel_image
from ..resources import WORKER_GPU, huggingface_secret, models_volume
from ..vocal_accel import ACCEL_ROOT, cpu_tag, install


#: Ephemeral, never deployed: run from `modal_compute/`, e.g.
#: `modal run -m nonoka_x_compute.operations.prime::prime_fleet`. The packages
#: land on `nonoka-x-models`, which the deployed workers mount.
app = modal.App()

#: Containers per fleet priming round.  Each AOTI package is keyed by the host
#: CPU's flag set, so covering the fleet means landing on every CPU it has;
#: running the round's inputs side by side is what keeps them on different
#: machines.
FLEET_PRIME_WIDTH = 8


def _install_separator_packages() -> dict:
    """Run the Vocal worker's installer here, building only what is missing.

    Runs the Vocal worker's own setup rather than FineSub's separator pool: the
    package has to match the worker down to the batch size, the CPU feature set
    and the container image, and the only way to guarantee that is to build it
    the same way the worker loads it.
    """

    import logging
    import tempfile
    from pathlib import Path

    from audio_separator.separator import Separator

    models_volume.reload()
    with tempfile.TemporaryDirectory(prefix="nonoka-x-separator-prime-") as output_dir:
        separator = Separator(
            output_dir=output_dir,
            model_file_dir="/models",
            output_format="WAV",
            output_single_stem="Vocals",
            use_autocast=True,
            log_level=logging.WARNING,
        )
        separator.load_model(model_filename=VOCAL_MODEL)
        outcome = install(
            separator.model_instance,
            model_name=VOCAL_MODEL,
            build_dir=Path(output_dir),
            commit=models_volume.commit,
            force=True,
        )
    # `modal run` does not surface a plain function's return value.
    print(json.dumps(outcome, ensure_ascii=False, indent=2, default=str), flush=True)
    return outcome


def _priming_summary(outcome: dict) -> dict[str, str | int | bool]:
    return {
        "backend": str(outcome.get("backend", "eager")),
        "cpuTag": cpu_tag(),
        "packageDir": str(outcome.get("package_dir", "")),
        "reusedPackage": bool(outcome.get("reused_package", False)),
        "buildSec": str(outcome.get("build_sec", "")),
        "loadSec": str(outcome.get("load_sec", "")),
        "runners": int(outcome.get("runners", 0)),
    }


@app.function(
    image=vocal_accel_image,
    gpu=WORKER_GPU,
    cpu=2.0,
    memory=4096,
    timeout=60 * 30,
    volumes={str(ACCEL_ROOT): models_volume},
)
def prime_finesub_gpu() -> dict[str, str | int | bool]:
    """Build the AOTI packages outside a customer task invocation.

    Raises when the accelerator does not stick.  Priming that silently produced
    an eager container would be indistinguishable from not priming at all, and
    the whole point of this entrypoint is to absorb the two-minute build.
    """

    outcome = _install_separator_packages()
    if outcome.get("backend") != "aoti":
        raise RuntimeError(f"separator priming fell back to eager: {outcome}")
    return _priming_summary(outcome)


@app.function(
    image=vocal_accel_image,
    gpu=WORKER_GPU,
    cpu=2.0,
    memory=4096,
    timeout=60 * 30,
    max_containers=FLEET_PRIME_WIDTH,
    scaledown_window=2,
    volumes={str(ACCEL_ROOT): models_volume},
)
def prime_fleet_separator(index: int) -> dict[str, str | int | bool]:
    """`prime_finesub_gpu` on one container of a `prime_fleet` round.

    The package key carries the SM and card, so packages for a new card sit
    beside the old card's on the models Volume without touching them -- which
    is what lets a card switch be primed before it is deployed.  Reports
    instead of raising: one container falling back must not cancel the rest of
    a round, and the fleet entrypoint is what decides whether priming succeeded.
    """

    import torch

    summary = _priming_summary(_install_separator_packages())
    summary.update(index=index, card=torch.cuda.get_device_name(0))
    return summary


@app.local_entrypoint()
def prime_fleet(max_rounds: int = 6) -> None:
    """Prime every CPU the worker card's fleet shows, until a round shows nothing new.

    A round where every container reused an existing package is the stopping
    signal: the fleet's CPU mix is a fact about Modal, so coverage is measured
    rather than assumed, and a later round can still find a stray machine.
    """

    import time

    seen: dict[str, dict] = {}
    for round_index in range(int(max_rounds)):
        rows = list(prime_fleet_separator.map(
            range(round_index * FLEET_PRIME_WIDTH, (round_index + 1) * FLEET_PRIME_WIDTH)
        ))
        built = [row for row in rows if not row["reusedPackage"]]
        for row in rows:
            seen.setdefault(row["cpuTag"], row)
        print(json.dumps({
            "round": round_index,
            "tags": sorted({row["cpuTag"] for row in rows}),
            "built": [row["cpuTag"] for row in built],
            "eager": [row["cpuTag"] for row in rows if row["backend"] != "aoti"],
            "reused_load_sec": [row["loadSec"] for row in rows if row["reusedPackage"]],
        }, ensure_ascii=False), flush=True)
        if not built and all(row["backend"] == "aoti" for row in rows):
            break
        # Let this round's containers scale down so the next one lands fresh.
        time.sleep(10)
    print(json.dumps({"distinct_cpu_tags": len(seen), "tags": sorted(seen)},
                     ensure_ascii=False, indent=2), flush=True)


@app.function(
    image=gpu_worker_image,
    # Downloads only: the weights are fetched and verified, never loaded.
    cpu=2.0,
    memory=2048,
    timeout=60 * 30,
    volumes={"/models": models_volume},
    secrets=[huggingface_secret],
)
def prime_diarization_models() -> dict[str, str]:
    """Put FineSub 0.6.0's default diarization weights on the models Volume.

    The GPU worker would fetch them itself on the first task that asks for
    speaker diarization (about 450 MB, through the same pinned, sha256-checked
    path); priming moves that wait out of a customer's billed GPU run. Uses the
    engine's own fetch so the files land exactly where the stage looks.
    """

    from finesub.speech.diarization.nemotron3_backend import HOSTED_EMBEDDING, HOSTED_MODEL
    from finesub.speech.runtime import hf_weights

    models_volume.reload()
    found: dict[str, str] = {}
    for entry in (HOSTED_MODEL, HOSTED_EMBEDDING):
        directory = hf_weights.prepare_directory(entry)
        if directory is None:
            raise RuntimeError(f"diarization weights {entry!r} could not be fetched")
        found[entry] = str(directory)
    models_volume.commit()
    print(json.dumps(found, ensure_ascii=False, indent=2), flush=True)
    return found
