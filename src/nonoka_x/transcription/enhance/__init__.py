"""The cloud engines' optional repairs that need the Python environment layer (torch, models).

Moved out of FineSub's `speech/joint/` (WP1; plan 2.2, third group, and 4.1 item 6):

- `silero`: the Silero VAD model, one stateful pass at a time (copied from FineSub's `silero_ghost`);
- `moss_timing`: MOSS Pro's unit times inside a segment from Silero's frame probabilities (S5b/S6b);
- `relink` with `voiceprint`: the speakers of a re-ask relinked by voice (WeSpeaker + PLDA);
- `sbd`: 35t's sentence-boundary model (ONNX);
- `aligner`: Qwen3-ForcedAligner unit times (S6b);
- `live`: Gemini Live, 35t's repair (a WebSocket);
- `device`: the few device helpers they need (copied from FineSub's `speech/runtime/device.py`).

Nothing here is imported by this package: each module is imported where it is used, after `unavailable(feature)`
said it can run. Importing `nonoka_x.transcription.enhance` itself needs the standard library only.

Models come from Nonoka's model manifest through the `nonoka_x.runtime` facade (`model_dir`); nothing here
downloads one. A model that is not installed makes its repair unavailable, and the engine goes without it.
"""

from __future__ import annotations

import importlib.util
import threading
from pathlib import Path

#: One joint model computing on the GPU at a time: the forced aligner and local MOSS. ⚠ Take it before Silero's
#: lock (`silero.silero_exclusive`), never inside it: local MOSS runs Silero while holding this one.
GPU_LOCK = threading.Lock()

#: The Python modules each repair needs. All of them are in the Python environment layer, none in the launcher.
FEATURES: dict[str, tuple[str, ...]] = {
    "silero": ("numpy", "torch", "silero_vad"),
    "moss-timing": ("numpy", "torch", "silero_vad"),
    "voiceprint": ("numpy", "torch", "torchaudio", "scipy"),
    "sbd": ("numpy", "onnx", "onnxruntime", "sentencepiece", "huggingface_hub"),
    "aligner": ("numpy", "torch", "transformers"),
    "live": ("websocket",),
}

#: The models each repair needs, by model-manifest id (`nonoka_x.runtime.RuntimeManager.model_dir`). The sentence
#: boundary model and the forced aligner are not in the manifest yet: they still come through `huggingface_hub`
#: (where they go is WP-E1's, plan appendix C item 6).
MODELS: dict[str, tuple[str, ...]] = {"voiceprint": ("speaker-embedding",)}


def missing_modules(*modules: str) -> list[str]:
    """Those of `modules` this interpreter cannot import (found without importing them)."""

    out = []
    for name in modules:
        try:
            found = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            out.append(name)
    return out


def layer_available() -> bool:
    """Whether this interpreter has the Python environment layer: every module any repair needs."""

    return not missing_modules(*sorted({m for modules in FEATURES.values() for m in modules}))


def unavailable(feature: str) -> str | None:
    """Why `feature` (a key of `FEATURES`) cannot run here, or None when it can. The one judgement every engine
    asks before it imports a module of this package; the reason goes into the task log."""

    if feature not in FEATURES:
        raise KeyError(f"unknown enhancement {feature!r}; known: {', '.join(FEATURES)}")
    missing = missing_modules(*FEATURES[feature])
    if missing:
        return f"Python 环境层不可用（缺 {', '.join(missing)}）"
    for model_id in MODELS.get(feature, ()):
        if model_dir(model_id) is None:
            return f"模型 {model_id} 没有安装"
    return None


def model_dir(model_id: str) -> Path | None:
    """The installed files of `model_id`, from the runtime the engine handed over (`audio.use_runtime`)."""

    from nonoka_x.transcription import audio

    manager = audio.current_runtime()
    return None if manager is None else manager.model_dir(model_id)


__all__ = ["FEATURES", "GPU_LOCK", "MODELS", "layer_available", "missing_modules", "model_dir", "unavailable"]
