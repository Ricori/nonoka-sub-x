"""Deployment entrypoint for the compute app, run from `modal_compute/`::

    modal deploy -m nonoka_x_compute.app

Each function is defined in its own module; importing them here registers them
on the shared app.
"""

from __future__ import annotations

from .control import control
from .gpu import run_aligned_stage
from .resources import APP_NAME, app
from .scheduler import orchestrate_task
from .sweep import sweep_scratch
from .vocal import VocalSeparator


__all__ = [
    "APP_NAME",
    "VocalSeparator",
    "app",
    "control",
    "orchestrate_task",
    "run_aligned_stage",
    "sweep_scratch",
]
