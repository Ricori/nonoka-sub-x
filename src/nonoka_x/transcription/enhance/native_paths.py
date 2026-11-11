"""`torch.jit.load` without handing the path to libtorch (Windows non-ASCII paths).

Copied from FineSub `finesub/speech/runtime/native_paths.py` (v0.7.0 @ 7123055), `load_torchscript` only and
unchanged (WP1). Python hands a path to a native library as UTF-8 bytes; libtorch's `fopen` goes through the
Universal CRT, which turns them back into UTF-16 with the ANSI code page, so on a stock Chinese Windows a model
under a user folder with a Chinese name "does not exist". Reading the archive through a Python file object keeps the path
away from libtorch by construction. Nonoka maintains this copy; FineSub's original is left as it is.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def load_torchscript(path: Path, *, map_location: Any) -> Any:
    """`torch.jit.load` without handing the path to libtorch."""

    import torch

    with open(path, "rb") as handle:
        return torch.jit.load(handle, map_location=map_location)
