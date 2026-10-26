"""Container images for the compute app.

Ported unchanged from `modal_backend/nonoka_x_modal/images.py`, minus the API
image (the API is the Cloudflare Worker now) and plus `control_image` for the
control endpoint and the scratch sweep. Every build step below is byte-for-byte
the old one on purpose: Modal keys its layer cache on the step, so an identical
definition reuses the layers the old app already built -- the Vocal layer in
particular, whose unpinned solve only still works because it is cached.

The `add_local_dir` paths are relative to the directory `modal deploy` runs in,
which is `modal_compute/` (see `docs/compute.md`): `ct2_patches` is this
project's copy, and `../third_party/finesub` and `../src` are the engine and
Nonoka X's own package in the enclosing checkout -- the same layout the old app
was deployed from.
"""

from __future__ import annotations

import modal

from .resources import CT2_COMMIT


#: The control endpoint and the scratch sweep: no engine, no source mount.
control_image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("fastapi[standard]")
)

# Cloud Vocal deliberately mirrors the proven VOD worker instead of inheriting
# the general FineSub image.  How much source audio one Roformer window covers
# scales with the input rate, which is what makes the rate a cost dial at all;
# which rates are worth offering is decided in `vocal_policy`, not here.  This
# is the base layer only: the worker runs `vocal_accel_image` below, and the
# sample-rate benchmark runs this one.
VOCAL_MODEL = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
vocal_base_image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg")
    # Do not add to this list without re-resolving it deliberately. The versions
    # here are unpinned below audio-separator, so any edit re-solves the whole
    # set: at the time of writing that pulls librosa 1.0, which dropped the
    # `audioread` import audio-separator still makes, and the build dies on the
    # model download step. The layer that production runs on predates that.
    .pip_install("audio-separator[gpu]==0.44.5", "soundfile")
    .run_commands(
        "audio-separator --download_model_only "
        f"--model_filename {VOCAL_MODEL} --model_file_dir /models"
    )
)

#: The newest torchaudio the cu130 index carries, and -- not by accident --
#: the version the desktop pipeline resamples with. Deliberately behind the
#: Torch that audio-separator's unpinned solve baked into `vocal_base_image`
#: (2.13.0+cu130): `AF.resample` is pure Torch, so the pairing is only a
#: question of whether the extension loads, and it does. The build step below
#: is what proves that rather than assuming it.
_VOCAL_TORCHAUDIO_VERSION = "2.11.0"

# What the Vocal worker runs. Identical to the image above plus what FineSub's
# weightless AOTI packages need to build and load, which `vocal_accel` keeps
# switched off by default -- see that module for the measurement that decided it.
#
# Everything here is an *appended* layer. The audio-separator solve above is
# unpinned and only still works because Modal cached it, so nothing may edit
# that step; appending leaves it untouched.
#
# g++ and Triton are already present. Only CUDA headers are missing, and the
# reason they are awkward is that this image has no CUDA toolkit: the pieces
# arrive inside pip `nvidia` wheels, split across three of them (runtime has
# cuda_runtime_api.h, nvcc has the `crt/` tree it includes, cccl has `nv/target`)
# and laid out where Torch does not look. Inductor compiles the AOTI wrapper with
# g++ and asks Torch for include paths, which Torch resolves under CUDA_HOME, so
# the symlinks below assemble the layout it expects -- `lib64` included, because
# that is the name Torch joins. The `test -f` guards fail the build loudly if the
# wheel prefix ever moves. No nvcc binary is needed: Triton carries its own ptxas
# for the device code.
vocal_accel_image = (
    vocal_base_image.pip_install(
        "nvidia-cuda-nvcc", "nvidia-cuda-cccl", "nvidia-ml-py"
    )
    .run_commands(
        "CUDA_ROOT=$(ls -d /usr/local/lib/python3.*/site-packages/nvidia/cu13)"
        " && mkdir -p /opt/cuda"
        " && ln -s $CUDA_ROOT/include /opt/cuda/include"
        " && ln -s $CUDA_ROOT/lib /opt/cuda/lib64"
        " && ln -s $CUDA_ROOT/lib /opt/cuda/lib"
        " && test -f /opt/cuda/include/cuda_runtime.h"
        " && test -f /opt/cuda/include/crt/host_defines.h"
        " && test -f /opt/cuda/include/nv/target"
    )
    # The link side of the same fiction. The step above assembles the headers
    # Inductor compiles against; this assembles the names its linker asks for.
    # A real CUDA toolkit ships `libcudart.so` beside `libcudart.so.13`, and
    # `-lcudart` resolves only the unversioned one -- the pip wheels carry just
    # the versioned file, so AOTI got all the way through compilation and died
    # at `ld: cannot find -lcudart`. Measured on a T4, 2026-09-04: with this
    # step missing every cloud AOTI build failed and the stage degraded to
    # eager, which `vocal_accel` is designed to swallow silently.
    #
    # Every `lib*.so.*` in the wheels gets the symlink rather than cudart
    # alone: the set Inductor links against is decided by the graph it lowered,
    # so fixing them one ld error at a time is a queue, not a fix. Existing
    # names are never overwritten.
    .run_commands(
        "for d in $(ls -d /usr/local/lib/python3.*/site-packages/nvidia/*/lib"
        " 2>/dev/null); do"
        "  for so in $d/lib*.so.*; do"
        "    [ -e \"$so\" ] || continue;"
        "    base=${so%.so.*}.so;"
        "    [ -e \"$base\" ] || ln -s \"$so\" \"$base\";"
        "  done;"
        " done"
        " && test -f /opt/cuda/lib64/libcudart.so"
    )
    # FineSub's own ASR delivery encoder, which this worker now calls instead
    # of shelling out to ffmpeg, resamples through `torchaudio.functional`.
    # The earlier note here said torchaudio was unnecessary because a
    # transformers-only build never reached the FineSub module that imports
    # it. The delivery does reach it: producing the same 16 kHz mono Vorbis
    # file the desktop produces is the point, and ffmpeg's resampler is not
    # the desktop's.
    #
    # `--no-deps` is load-bearing: torchaudio declares a `torch` requirement,
    # and letting pip resolve it would pull a second Torch over the one
    # audio-separator's unpinned solve installed -- the layer this file is not
    # allowed to disturb. The version is behind that Torch by two minors
    # because cu130 carries nothing newer, which is fine for the one function
    # we call and is checked here rather than at the first separation.
    .run_commands(
        "python -m pip install --no-deps"
        " --index-url https://download.pytorch.org/whl/cu130"
        f" torchaudio=={_VOCAL_TORCHAUDIO_VERSION}"
        " && python -c \"import torch, torchaudio.functional as AF;"
        " assert AF.resample(torch.zeros(1, 44100), 44100, 16000).shape[-1]"
        " == 16000\""
    )
    .env(
        {
            # Nonoka X's own package joins the engine on the path: the Vocal
            # stage plans its blocks with `nonoka_x.vocal_blocks`, a pinned copy
            # of arithmetic it cannot import from the engine here.
            "PYTHONPATH": "/opt/finesub/src:/opt/nonoka_x",
            # Points FineSub's artefact root at the models Volume. Not /models:
            # that is where this image bakes the checkpoint, and a Volume
            # mounted over it would hide the weights.
            "FINESUB_MODEL_DIR": "/accel",
            "CUDA_HOME": "/opt/cuda",
        }
    )
    # Must stay last: Modal refuses a build step after add_local_*, and mounting
    # rather than copying keeps FineSub edits off the image rebuild path.
    .add_local_dir("../third_party/finesub", "/opt/finesub")
    .add_local_dir("../src", "/opt/nonoka_x")
)

# FineSub's fw-refine backend needs decoder trace fields that stock CTranslate2
# does not expose. Keep the expensive CUDA/CTranslate2 toolchain in a stable
# base image: FineSub changes are mounted only after compilation, so ordinary
# Python/API iteration can reuse the compiled layer.
gpu_worker_base_image = (
    modal.Image.from_registry(
        "nvidia/cuda:12.8.1-cudnn-devel-ubuntu22.04", add_python="3.12"
    )
    # Modal's Python 3.12 runtime records clang as its extension compiler.
    # audio-separator -> diffq has no Linux cp312 wheel, so clang is required
    # even though build-essential already provides gcc/g++.
    .apt_install("ffmpeg", "git", "cmake", "ninja-build", "build-essential", "clang")
    .add_local_dir("ct2_patches", "/opt/ct2-patches", copy=True)
    .run_commands(
        "python -m pip install --extra-index-url https://download.pytorch.org/whl/cu128 torch==2.11.0 torchaudio==2.11.0 torchvision==0.26.0",
        "python -m pip install faster-whisper==1.2.1 scipy onnxruntime audio-separator==0.44.5 audioread==3.1.0 diffq==0.2.4 numba silero-vad 'transformers>=5.13,<6' httpx opencc-python-reimplemented soundfile numpy wheel pybind11 boto3 nvidia-ml-py",
        "python -c \"from audio_separator.separator import Separator; assert Separator\"",
        "git clone --recursive https://github.com/OpenNMT/CTranslate2.git /opt/ctranslate2",
        f"cd /opt/ctranslate2 && git checkout {CT2_COMMIT}"
        " && git submodule update --init --recursive"
        " && git config user.email build@local"
        " && git config user.name Nonoka X-Build"
        " && git am /opt/ct2-patches/*.patch",
        "cmake -S /opt/ctranslate2 -B /opt/ctranslate2/build -G Ninja"
        " -DCMAKE_BUILD_TYPE=Release -DCMAKE_POLICY_VERSION_MINIMUM=3.5"
        " -DOPENMP_RUNTIME=COMP -DWITH_CUDA=ON -DCUDA_DYNAMIC_LOADING=ON"
        " -DWITH_MKL=OFF -DWITH_RUY=ON -DBUILD_CLI=OFF"
        # Patches 0012/0013 (FineSub 0.6.0): flash attention for the Whisper
        # encoder, compiled for the one kernel shape it uses. The kernels are
        # guarded by `__CUDA_ARCH__ >= 800`, so 7.5 still builds and a T4 runs
        # the standard attention -- FineSub's `auto` checks the card and
        # `flash_attention_available()` before asking. Moving the GPU worker
        # to an Ampere-or-newer card turns it on with no other change.
        " -DWITH_FLASH_ATTN=ON"
        " -DCUDA_ARCH_LIST='7.5;8.0;8.6;8.9;9.0+PTX'",
        "cmake --build /opt/ctranslate2/build --parallel 4"
        " && cmake --install /opt/ctranslate2/build --prefix /opt/ctranslate2/install",
        "cd /opt/ctranslate2/python"
        " && CTRANSLATE2_ROOT=/opt/ctranslate2/install python setup.py bdist_wheel"
        " && python -m pip install --force-reinstall --no-deps dist/*.whl"
        " && cp /opt/ctranslate2/install/lib/libctranslate2.so* /usr/local/lib/"
        " && ldconfig",
        "python -c \"import inspect, ctranslate2; doc = inspect.getdoc(ctranslate2.models.Whisper.generate) or ''; assert 'return_refine_paths' in doc, 'patched CTranslate2 trace API missing'; assert 'return_refine_weights' in doc, 'patched CTranslate2 disfluency weights missing'; assert ctranslate2.flash_attention_available(), 'CTranslate2 built without WITH_FLASH_ATTN'\"",
        # audio-separator 0.44.5 still uses librosa's deprecated `filename`
        # argument, which librosa 1.0 removed. Its dependency graph also pulls
        # the CPU ONNX distribution; replace it last so T4 separation really
        # uses CUDA instead of silently occupying a GPU while running on CPU.
        "python -m pip install librosa==0.10.2.post1"
        " && python -m pip uninstall -y onnxruntime"
        " && python -m pip install --no-deps onnxruntime-gpu==1.28.0",
        "python -c \"import inspect, librosa, onnxruntime as ort; from audio_separator.separator import Separator; assert 'filename' in inspect.signature(librosa.get_duration).parameters; assert 'CUDAExecutionProvider' in ort.get_available_providers(); assert Separator\"",
    )
)

# The scheduler performs source decoding, VAD, CPU Qwen verification and the
# LLM stages.  It never executes patched CTranslate2 and never owns a GPU, so
# inheriting ``gpu_worker_base_image`` made every scheduler cold start
# download the CUDA toolchain and print a misleading "NVIDIA Driver was not
# detected" banner.  Keep the CPU dependency set explicit and use CPU-only
# Torch wheels.
scheduler_base_image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg")
    .run_commands(
        "python -m pip install --index-url https://download.pytorch.org/whl/cpu torch==2.11.0 torchaudio==2.11.0 torchvision==0.26.0",
        "python -m pip install faster-whisper==1.2.1 scipy onnxruntime audioread==3.1.0 librosa==0.10.2.post1 numba silero-vad 'transformers>=5.13,<6' httpx opencc-python-reimplemented soundfile numpy boto3",
    )
    .pip_install("tzdata==2025.2")
)

# FineSub is pure Python and all of its dependencies are already pinned in the
# base image. Mount its checkout at container startup instead of baking it into
# an image; PYTHONPATH makes an installation step unnecessary.
gpu_worker_image = (
    gpu_worker_base_image
    .env({
        "PYTHONPATH": "/opt/finesub/src:/opt/nonoka_x",
        "HF_HOME": "/models/huggingface",
        # Keep the 610 MB separator checkpoint and its hardware-specific AOTI
        # package on the same persistent Volume as the Hugging Face models.
        # Without this, every cold T4 container falls back to its ephemeral
        # ~/.cache and cannot reuse the expensive separator setup.
        "FINESUB_MODEL_DIR": "/models",
    })
    .add_local_dir("../third_party/finesub", "/opt/finesub")
    .add_local_dir("../src", "/opt/nonoka_x")
)

# Debian slim does not include the IANA database that FineSub's LLM rate-limit
# window reads through zoneinfo. Add every build layer before the source mount:
# Modal can then reuse the image while updating FineSub independently.
scheduler_image = (
    scheduler_base_image
    .env({
        # Nonoka X's own package joins the engine on the path here too: the
        # CPU leg resolves this request's GPU tier through `nonoka_x.gpu_tier`,
        # the same helper the desktop worker resolves it with, so one request
        # means one answer wherever it runs.
        "PYTHONPATH": "/opt/finesub/src:/opt/nonoka_x",
        "HF_HOME": "/models/huggingface",
    })
    .add_local_dir("../third_party/finesub", "/opt/finesub")
    .add_local_dir("../src", "/opt/nonoka_x")
)
