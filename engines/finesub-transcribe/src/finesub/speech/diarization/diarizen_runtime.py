"""The conditions the exported DiariZen graphs have to run under, in one place.

Three context managers, and together they are the **correctness contract of the port** rather
than tidiness. `docs/diarization-findings.md` 5.2 has the measurements; the short version is that
this chain has no "small enough not to matter" numerical difference, because the hard powerset
argmax turns a last-bit float difference into a flipped speaker label. Five switches were measured
changing the answer on real audio:

| switch | what it costs when wrong | lives |
| --- | --- | --- |
| TF32 | 0.2503% of frames differ from the research path | `tf32_disabled` |
| TorchScript fusion | call 2 onward drifts 1.4e-3, ~0.02% of frames | `jit_fusion_disabled` |
| batch size | batches of 8 and 32 flip 0.0117% of frames | `diarizen_runner.DEFAULT_BATCH_SIZE` |
| `torch.jit.freeze` | folds BatchNorm, answers move 4.5e-4~7.3e-4 | never used; `tools/diarizen_export/` |
| clustering `MAX_ITERS` | most files never converge, so it picks a point on a trajectory | `diarizen_cluster.MAX_ITERS` |

⚠ **They were private names in `diarizen_runner` until 2026-09-20, and six callers imported them
anyway** -- the embedder, the exporter's equivalence check, and three measurement beds. A contract
that every consumer has to reach through an underscore to obey is a contract that the next
consumer will simply not obey.

`loaded_model` is here for the same reason even though its subject is memory rather than
arithmetic: it is the other thing every entry point must do and every entry point got wrong once.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from ..runtime.native_paths import load_torchscript


@contextmanager
def jit_fusion_disabled():
    """Stop TorchScript fusing kernels, because fusing changes the arithmetic.

    ⚠ The profiling executor recompiles a traced module after its FIRST run on a given shape and
    fuses pointwise chains: call 1 is bit-exact with eager, calls 2 onward differ by 1.4e-3, and
    through the hard powerset argmax that is flipped labels on about 0.02% of frames. Which also
    means a check that runs each shape once cannot see it -- repeat one shape.

    ⚠ **texpr is the only switch that matters here.** Four arms on a repeated shape: texpr alone
    gives 0 drift, nothing disabled gives 1.362e-3. `_jit_set_nvfuser_enabled` is a documented
    no-op in the torch we pin (and prints a warning on every call), and the `can_fuse` overrides
    add nothing, so neither is used.

    Scoped and restored, because this is a process-wide switch -- and not thread-safe, which is
    fine only because diarization runs in the asr bin, whose concurrency is 1.
    """
    import torch

    # ⚠ Restore what was there, not `True`: the docstring says scoped, and a caller who had
    # already disabled the fuser for its own reasons would otherwise get it switched back on.
    previous = torch._C._jit_texpr_fuser_enabled()
    torch._C._jit_set_texpr_fuser_enabled(False)
    try:
        yield
    finally:
        torch._C._jit_set_texpr_fuser_enabled(previous)


@contextmanager
def tf32_disabled():
    """Turn TF32 off for the duration, then put it back exactly as it was.

    ⚠ This is not a precaution, it is a correctness requirement. The research path runs under
    pyannote's `fix_reproducibility`, which clears both TF32 flags, and every frozen reading came
    from that. With TF32 left on, matmuls lose ten mantissa bits, and the hard powerset argmax
    turns that into flipped labels -- measured at 0.0210% of frames on hina-32, which is not a
    rounding difference but a different answer.

    Scoped rather than global: these flags are process-wide, and the rest of the pipeline did not
    ask for slower matmuls.
    """
    import torch

    matmul = torch.backends.cuda.matmul.allow_tf32
    cudnn = torch.backends.cudnn.allow_tf32
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    try:
        yield
    finally:
        torch.backends.cuda.matmul.allow_tf32 = matmul
        torch.backends.cudnn.allow_tf32 = cudnn


@contextmanager
def loaded_model(model_path: str | Path, device: str):
    """Load an exported model, and give its memory back when the caller is done with it.

    ⚠ Not tidiness. Both entry points load their own copy, so a process that handles several files
    in a row -- a batch run, or the lab's two-arm comparison -- stacks one set per file in torch's
    caching allocator, which never returns blocks to the driver on its own.

    ⚠ On Windows that does not fail loudly. Once the dedicated side fills, the driver spills into
    SHARED memory and the run keeps going over PCIe, an order of magnitude slower, while
    `nvidia-smi` still reports only the dedicated side sitting at a flat high-water mark that looks
    like a healthy plateau. Measured: seven files in one process took the dedicated side to
    15.8 of 16.3 GiB and put 8 GiB in shared memory, and each file went from minutes to tens of
    minutes. Nothing raised.
    """
    import torch

    model = load_torchscript(Path(model_path), map_location=device).eval()
    try:
        yield model
    finally:
        del model
        if str(device).startswith("cuda"):
            torch.cuda.empty_cache()


__all__ = ["jit_fusion_disabled", "loaded_model", "tf32_disabled"]
