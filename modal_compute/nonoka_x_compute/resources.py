"""The compute app, its Volumes and its Secrets.

Modal holds no user data any more. Keys, quota, the library, jobs and their
logs live in the Cloudflare Worker (`nonoka-cloud-cf`), and so does the bucket:
this app downloads a job's source audio and hands its result back through the
Worker's internal API (`channel`), and never sees an R2 credential.

What is left here is compute and its working files:

- `nonoka-x-scratch`, mounted at `/data`: the breakpoint tree `tasks/<job_id>/`
  every leg of a run reads and writes. A new Volume rather than the old
  `nonoka-x-artifacts`, which also held the library and records the Worker now
  owns; nothing here outlives the run that wrote it except a breakpoint a
  retry or resume may still want (`sweep` removes those after a week).
- `nonoka-x-models`, reused by name: the Hugging Face cache, the separator's
  AOTI packages and the diarization weights already primed there. Renaming it
  would mean priming the fleet again.
"""

from __future__ import annotations

from pathlib import Path

import modal


APP_NAME = "nonoka-x-compute"
DATA_ROOT = Path("/data")
CT2_COMMIT = "0d8bcd362ac75ef860ef161d6f0efad0ae439ff0"
#: The card both GPU pipeline legs rent, and the one priming builds for.  An
#: L4 since 2026-10-13: sm89 runs the Whisper encoder with flash attention
#: (FineSub's `auto`), and measured against the T4 over an 18.5-minute task the
#: two legs finished 29% sooner for 4% less GPU spend.  Switching cards again
#: needs the AOTI packages primed for the new one first
#: (`operations/prime.py::prime_fleet`).  The Worker reports the card to the
#: desktop from its own configuration, so a switch is made in both places.
WORKER_GPU = "L4"

app = modal.App(APP_NAME)

scratch_volume = modal.Volume.from_name("nonoka-x-scratch", create_if_missing=True)
models_volume = modal.Volume.from_name("nonoka-x-models", create_if_missing=True)

huggingface_secret = modal.Secret.from_name("huggingface")
#: `CONTROL_TOKEN` (what the Worker presents to `control`), `CF_CALLBACK_URL`
#: and `WORKER_CALLBACK_TOKEN` (what every leg presents to the Worker).
control_secret = modal.Secret.from_name("nonoka-compute-control")


def task_dir(job_id: str) -> Path:
    """A job's breakpoint tree on the scratch Volume."""

    return DATA_ROOT / "tasks" / job_id
