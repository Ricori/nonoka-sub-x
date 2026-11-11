"""A clean environment for recording and checking baselines (plan WP-Q item 6).

The baselines describe FineSub as it behaves on a machine with nothing outside
Nonoka steering it (plan section 0, rule 4). This module:

- lists what on this machine could steer it: environment variables by name
  (never their values) and files in the locations FineSub and
  `finesub_bootstrap` fall back to when no variable names a path;
- builds child environments without those variables.

Two strengths of cleaning:

- `isolated_environment` (call equivalence, replay, environment baseline;
  nothing there needs the GPU or the managed runtime): the variables go, and
  the user's profile directories are replaced by empty ones under the run, so
  no default location can be read at all.
- `scrubbed_environment` (real samples): the variables go, the profile
  directories stay -- torch, the AOTI compiler and the managed runtime need
  the real ones, and the files FineSub could fall back to there are named by
  the `FINESUB_*` paths the provider sets explicitly. What remains is listed by
  `inventory()` so the report can say so.

The influence list is the plan's (`FINESUB_*`, `HF_*`, `TORCH_*`, `CUDA_*`,
`PYTORCH_*`, `OMP_*`, proxies, `PYTHON*`) widened with the libraries and
tools the engine is known to pull in. WP-R0 owns the complete table; this is
the recording-time safety net, not that table.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from . import ROOT, VENDOR

INFLUENCE_PREFIXES = (
    "FINESUB_", "NONOKA_", "MOSS_",
    "HF_", "HUGGINGFACE", "TRANSFORMERS_", "TOKENIZERS_", "DIFFUSERS_",
    "TORCH", "PYTORCH_", "CUDA_", "CUDNN", "NVIDIA_", "NVTOOLSEXT", "CUBLAS", "NCCL_", "TRITON_",
    "CT2_", "OMP_", "MKL_", "KMP_", "OPENBLAS_", "NUMEXPR_", "VECLIB_", "NUMBA_", "INTEL_", "MIC_",
    "ONNX", "ORT_", "HSA_", "HIP_", "ROCM",
    "PYTHON", "PIP_", "UV_", "CONDA", "VIRTUAL_ENV",
    "SSL_", "REQUESTS_CA", "CURL_CA", "NODE_EXTRA_CA", "NODE_USE_SYSTEM_CA",
    "XDG_",
    # Credentials and endpoints an SDK or an agent CLI would pick up by itself.
    "ANTHROPIC_", "OPENAI_", "GEMINI_", "GOOGLE_", "DEEPSEEK_", "DASHSCOPE_", "KIMI_", "EXA_", "TAVILY_",
    "CLAUDE", "CODEX_", "AGY_",
)
INFLUENCE_NAMES = frozenset(name.lower() for name in (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "INCLUDE", "LIB", "LIBPATH",
    "CUDA_PATH", "CUDA_VISIBLE_DEVICES", "EDITOR", "VISUAL",
))


def influences(name: str) -> bool:
    upper = name.upper()
    return upper.startswith(INFLUENCE_PREFIXES) or name.lower() in INFLUENCE_NAMES


def scrubbed_environment(base: Mapping[str, str] | None = None) -> tuple[dict[str, str], list[str]]:
    """`base` (default: this process) without the influencing variables; and their names."""

    base = dict(os.environ if base is None else base)
    removed = sorted(name for name in base if influences(name))
    return {name: value for name, value in base.items() if name not in removed}, removed


def isolated_environment(root: Path, base: Mapping[str, str] | None = None) -> tuple[dict[str, str], list[str]]:
    """Scrubbed, and the profile directories replaced by empty ones under `root`."""

    environment, removed = scrubbed_environment(base)
    profile = root / "profile"
    for name, relative in (
        ("USERPROFILE", "user"), ("HOME", "user"),
        ("APPDATA", "user/AppData/Roaming"), ("LOCALAPPDATA", "user/AppData/Local"),
    ):
        (profile / relative).mkdir(parents=True, exist_ok=True)
        environment[name] = str(profile / relative)
    environment["PYTHONNOUSERSITE"] = "1"
    return environment, removed


def default_locations() -> list[Path]:
    """Where FineSub and `finesub_bootstrap` look when no variable names a path."""

    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
    roaming = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    return [
        local / "FineSub",                      # finesub_bootstrap.paths: the managed data root
        home / ".finesub",                      # state and data root off Windows
        home / ".cache" / "finesub",            # paths.resolve_cache_dir's last resort
        home / ".cache" / "audio-separator",    # the separator's own default
        home / ".cache" / "huggingface",        # HF_HOME's default
        home / ".cache" / "torch",              # TORCH_HOME's default
        roaming / "Python",                     # user site-packages
        VENDOR / ".env", VENDOR / "config.toml", VENDOR / "model_catalog.toml",  # checkout data
        ROOT / ".env", ROOT / "config.toml", ROOT / "finesub.toml",              # the working directory
    ]


def inventory() -> dict[str, object]:
    """What could steer FineSub on this machine: variable names and default-location files."""

    names = sorted(name for name in os.environ if influences(name))
    files: dict[str, list[str]] = {}
    for location in default_locations():
        if location.is_file():
            files[str(location)] = [location.name]
        elif location.is_dir():
            entries = sorted(
                path.relative_to(location).as_posix()
                for path in location.rglob("*")
                if path.is_file()
            )
            files[str(location)] = entries[:40] + ([f"... {len(entries) - 40} more"] if len(entries) > 40 else [])
    return {"variables": names, "default_location_files": files}
