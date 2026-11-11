"""The Silero VAD model the cloud engines' gates and MOSS Pro's unit timing read, one pass at a time.

Copied from FineSub `finesub/speech/preprocessing/silero_ghost.py` (v0.7.0 @ 7123055), `silero_model` and
`silero_exclusive` only and unchanged (WP1). The model is the one the `silero_vad` package ships, loaded through
a file object (`native_paths.load_torchscript`). Inside FineSub this lock was shared with the local VAD's Silero
assist; here only the cloud engines take it. Nonoka maintains this copy; FineSub's original is left as it is.
"""

from __future__ import annotations

import contextlib
import threading

import torch

from .native_paths import load_torchscript

_MODEL = None
_LOCK = threading.RLock()


def silero_model():
    """The Silero VAD TorchScript model, loaded once per process."""
    global _MODEL
    if _MODEL is not None:  # loaded: no need to wait out a cloud task's exclusive pass
        return _MODEL
    with _LOCK:
        if _MODEL is None:
            # silero_vad/model.py calls torch.set_num_threads(1) at import time.
            # That is a process-global setting and it never restores it, so every
            # later torch op in this run would be single-threaded.
            # Importing anything under `silero_vad` runs its package __init__,
            # which imports that module.
            threads = torch.get_num_threads()
            try:
                from importlib import resources

                # Located the way `load_silero_vad()` locates it, but loaded
                # through a file object: its own loader hands the path string to
                # libtorch, which misreads non-ASCII paths (see native_paths).
                archive = resources.files("silero_vad.data").joinpath("silero_vad.jit")
                with resources.as_file(archive) as path:
                    _MODEL = load_torchscript(path, map_location="cpu").eval()
            finally:
                torch.set_num_threads(threads)
        return _MODEL


@contextlib.contextmanager
def silero_exclusive():
    """The shared model for one stateful pass over it (`reset_states`, calls, `reset_states`), one caller at
    a time: cloud-ASR items of a batch run side by side on the network lane (`scheduler`)."""
    with _LOCK:
        yield silero_model()
