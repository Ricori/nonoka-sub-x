"""What FlowOCR reads, derived in this package's process from its launch context.

The core describes the process in a neutral launch context: its run and data
directories, the on-screen text layer (`runtime.python`, `runtime.models`),
the package's cache directory, the game-text directory, the proxy. Here,
before anything of FlowOCR's is imported, that becomes FlowOCR's own
variables:

- every `FLOWOCR_*` variable this process inherited is dropped first, so only
  what is derived here reaches the engine;
- the data root is the package's cache directory (FlowOCR's scratch and the
  TensorRT engine cache live there), the model root is the layer's
  `models\\` (read-only: the layer carries the derived graphs, so nothing is
  written into it), the game-text directory is the package's data directory's.

What a package's process has in its environment is classified twice over:
the neutral half (`nonoka_x.engine_host.CLASSIFICATION`), which the core
builds the process from, and this module's `CLASSIFICATION`, what the bridge
writes on top from the launch context, and what FlowOCR reads that is never
handed in.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

#: The prefix of every variable FlowOCR reads of its own.
VARIABLE_PREFIX = "FLOWOCR_"

_O = "ocr"
_ALL = frozenset((_O,))

Row = tuple  # (name, category, processes, reason)


def _rows(category: str, processes: frozenset, reason: str, *names: str) -> tuple[Row, ...]:
    return tuple((name, category, processes, reason) for name in names)


#: The variables the bridge writes (`engine_variables`).
WRITTEN = ("FLOWOCR_DATA_ROOT", "FLOWOCR_MODELS", "FLOWOCR_TRT", "FLOWOCR_GAMETEXT")

CLASSIFICATION: tuple[Row, ...] = (
    # -- written here --------------------------------------------------------------------------------------------
    *_rows("nonoka", _ALL,
           "where FlowOCR keeps its data (this package's cache directory), its models (the layer's `models\\`, "
           "read-only), its TensorRT engine cache and the game-text bundles (`engine_variables`)", *WRITTEN),
    # -- FlowOCR's own hand-offs to its children: never handed in --------------------------------------------------
    *_rows("absent", _ALL,
           "set by FlowOCR itself for the children it starts (worker, decode hub, ORT server, run groups, "
           "language probe); never present when the process starts",
           "FLOWOCR_CHILD_KEY", "FLOWOCR_DECODE_HUB", "FLOWOCR_DECODE_HUB_KEY", "FLOWOCR_DECODE_HUB_REASON",
           "FLOWOCR_HWACCEL_REASON_FILE", "FLOWOCR_HWACCEL_RETRIED", "FLOWOCR_OCR_LANG_PROBE",
           "FLOWOCR_OCR_WORKER", "FLOWOCR_ORT_ADDR", "FLOWOCR_ORT_SHARED", "FLOWOCR_RUN_GROUPS",
           "FLOWOCR_TRT_INDEX"),
    # -- read, never written ------------------------------------------------------------------------------------------
    *_rows("absent", _ALL,
           "development and fault-injection switches: their defaults (off)",
           "FLOWOCR_FAULT_HWACCEL_AT", "FLOWOCR_FAULT_NO_JOB", "FLOWOCR_FAULT_ORPHAN", "FLOWOCR_REUSE_DEBUG",
           "FLOWOCR_TIMELINE", "FLOWOCR_ORT_PYTHON", "FP_CHECK"),
    *_rows("absent", _ALL,
           "a Hugging Face mirror: the models come with the layer and nothing is downloaded", "HF_ENDPOINT"),
    *_rows("absent", _ALL,
           "cuBLAS' workspace setting: its default", "CUBLAS_WORKSPACE_CONFIG"),
)


def classification_for(process: str) -> dict[str, tuple[str, str]]:
    """The package's half of the table for one process (`ocr`): name -> (category, reason)."""

    return {name: (category, reason) for name, category, processes, reason in CLASSIFICATION if process in processes}


def drop_inherited() -> None:
    """Take every `FLOWOCR_*` variable out of this process's environment."""

    for name in list(os.environ):
        if name.upper().startswith(VARIABLE_PREFIX):
            del os.environ[name]


def engine_variables(context: Mapping[str, Any]) -> dict[str, str]:
    """FlowOCR's data root, model root, TensorRT cache and game-text directory, from the launch context."""

    runtime = context.get("runtime") or {}
    cache = Path(str(runtime.get("cache") or (context.get("paths") or {}).get("package_data") or "."))
    variables = {
        "FLOWOCR_DATA_ROOT": str(cache),
        "FLOWOCR_TRT": str(cache / "trt"),
    }
    if runtime.get("models"):
        variables["FLOWOCR_MODELS"] = str(runtime["models"])
    else:
        # A model root that is not there reads as "no model": nothing is fetched into the cache.
        variables["FLOWOCR_MODELS"] = str(cache / "models-unavailable")
    if runtime.get("gametext"):
        variables["FLOWOCR_GAMETEXT"] = str(runtime["gametext"])
    return variables


def apply(context: Mapping[str, Any]) -> None:
    """The process's environment for FlowOCR: inherited `FLOWOCR_*` dropped, the context's written."""

    drop_inherited()
    variables = engine_variables(context)
    Path(variables["FLOWOCR_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
    os.environ.update(variables)
