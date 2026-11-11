"""Whether this machine's GPU can run the stack, decided in one place.

Two different situations end in the same CPU fallback: there is no usable CUDA
at all (no driver, no card, ``CUDA_VISIBLE_DEVICES`` emptied), and there is a
card whose architecture the installed PyTorch ships no kernels for. The second
one is why this module exists -- torch reports ``is_available() == True`` for a
GTX 1060, and the mismatch only surfaces at the first real op as ``no kernel
image is available for execution on the device``, which names neither the card
nor the fix.

The check is deliberately derived from ``torch.cuda.get_arch_list()`` rather
than a hard-coded floor: the supported set is a property of the installed wheel,
so it moves on its own when the torch pin moves.

The ASR stage asks a second copy of that question against a different binary
(``ct2_cuda_unusable_reason``), because CTranslate2 ships its own CUDA build and
its own architecture list. That one *is* a hard-coded floor -- the library
exposes no arch list to derive it from -- and it is checked here rather than
left to the first encode for exactly the reason above: below the floor the
failure is a driver-level "no kernel image", which names neither the card nor
the fix.

**Which vendor** is answered here too, and nowhere else. PyTorch's ROCm build
reuses the ``cuda`` device name and the whole ``torch.cuda`` API for AMD cards,
so a stage that says ``"cuda"`` runs on either vendor without knowing which --
and must not find out by probing ``torch.cuda`` itself. What does differ is
answered below: the kernel check (AMD kernels do not run across ``gfx``
targets, so it is membership, not a floor), the runtime/architecture strings a
compile cache is keyed on, and which of several cards to use. NVIDIA answers
are unchanged by all of it; a source guard (`test_device_probes_live_in_one_module`)
keeps the probes in this module.

Copied from FineSub `finesub/speech/runtime/device.py` (v0.7.0 @ 7123055) for the optional repairs and
local MOSS (WP1): only the definitions they reach are kept, unchanged; the imports point at Nonoka's
own modules. Nonoka maintains this copy; FineSub's original is left as it is.
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import sys
from typing import Optional

import torch

from ..reporting import current_reporter, reporter_delivers

#: FineSub `doc_links.REPO_INSTALL_DOC`, the page these messages point at.
REPO_INSTALL_DOC = (
    "https://github.com/caca2331/finesub/blob/main/docs/manual/repo-install.md"
)

# Spelled as product lines because that is what a user can actually check on
# their own machine. sm_75 is the oldest consumer architecture in the cu128
# wheel's kernel list; sm_70 covers the datacenter TITAN V / V100. Keep in sync
# with README.md's hardware table.
SUPPORTED_GPU_HINT = "GTX 1650/1660 or RTX 20/30/40/50 series and newer"

#: Environment variables through which a user already chose a card. When any is
#: set, the choice is theirs and `larger_amd_card_index` stays out of it.
_VISIBLE_DEVICE_VARIABLES = (
    "CUDA_VISIBLE_DEVICES",
    "HIP_VISIBLE_DEVICES",
    "ROCR_VISIBLE_DEVICES",
    "GPU_DEVICE_ORDINAL",
)


def is_rocm() -> bool:
    """Whether this PyTorch is the ROCm build (AMD), which still says ``cuda``."""

    return bool(getattr(torch.version, "hip", None))


def _gfx_from_override(value: str) -> Optional[str]:
    """``HSA_OVERRIDE_GFX_VERSION=10.3.0`` -> ``gfx1030``, the target it forces."""

    parts = [part for part in value.strip().split(".") if part]
    if not parts or not all(part.isdigit() for part in parts):
        return None
    return "gfx" + "".join(parts)


def _rocm_arch() -> Optional[str]:
    """The ``gfx`` target the ROCm runtime will load kernels for on this card.

    ``HSA_OVERRIDE_GFX_VERSION`` wins when set: it is exactly how RX 6000-era
    users run cards absent from a build's list, by telling the runtime to treat
    the card as a listed target -- so that target, not the card's own name, is
    what the kernel check must look for.
    """

    override = os.environ.get("HSA_OVERRIDE_GFX_VERSION")
    if override:
        forced = _gfx_from_override(override)
        if forced:
            return forced
    try:
        properties = torch.cuda.get_device_properties(torch.cuda.current_device())
    except Exception:  # pragma: no cover - driver-level failure
        return None
    name = str(getattr(properties, "gcnArchName", "") or "")
    return name.split(":", maxsplit=1)[0].strip() or None


def _gfx_matches(entry: str, arch: str) -> bool:
    """Does a build's arch-list entry cover this card's ``gfx`` target?

    Exact names, plus the two generic spellings ROCm builds use for a family:
    ``gfx11-generic`` and ``gfx110X``.
    """

    entry = entry.strip().lower()
    arch = arch.strip().lower()
    if entry == arch:
        return True
    if entry.endswith("-generic"):
        return arch.startswith(entry.removesuffix("-generic"))
    if entry.endswith("x"):
        return arch.startswith(entry[:-1])
    return False


def _rocm_build_has_kernels_for(arch: str, entries: list[str]) -> bool:
    """Membership, not a floor: AMD kernels do not run on another ``gfx`` target.

    An empty list means the build reports nothing to compare against; as on the
    NVIDIA side, let it try.
    """

    if not entries:
        return True
    return any(_gfx_matches(entry, arch) for entry in entries)


def _arch_number(entry: str) -> Optional[int]:
    """``sm_86`` / ``compute_90a`` -> 86 / 90, the way torch itself reads them.

    Mirrors ``torch.cuda._extract_arch_version``: the number is
    ``major * 10 + minor``, and a trailing ``a``/``f`` on entries like
    ``sm_90a`` marks a variant rather than part of the number.
    """

    parts = entry.split("_", maxsplit=2)
    if len(parts) < 2:
        return None
    base = parts[1].removesuffix("a").removesuffix("f")
    return int(base) if base.isdigit() else None


def _capability_number(capability: tuple[int, int]) -> int:
    """``(8, 6)`` -> 86, the same major*10+minor `_arch_number` reads out of
    ``sm_86`` -- so a card and an arch-list entry compare as plain integers."""

    return capability[0] * 10 + capability[1]


def _build_has_kernels_for(capability: tuple[int, int]) -> bool:
    """Can the installed torch actually run on a card of this capability?

    Only the *lower* bound of the build's arch list is treated as disqualifying,
    and membership is never required. Two traps drive that:

    - Requiring an exact ``sm_XY`` match would be plainly wrong. The cu128 list
      has no ``sm_89``, yet every RTX 40-series card runs on it, because a cubin
      is binary-compatible with later minor revisions of the same major version.
      Pushing those to CPU would be a far worse bug than the one this guard is
      here for.
    - Above the top of the list, a card may still work -- via a ``compute_XY``
      PTX entry the driver JITs forward, or that same in-major compatibility. So
      let torch try. A wrong fallback costs a silent 10x slowdown; a wrong
      attempt costs a loud error, which is the better way to be wrong.

    Below the minimum there is no such escape hatch (cubins are not backward
    compatible), which is exactly the GTX 10-series case. The bound therefore
    matches the condition torch warns about in ``torch.cuda._check_capability``.
    """

    numbers = [
        number
        for number in (_arch_number(entry) for entry in torch.cuda.get_arch_list())
        if number is not None
    ]
    if not numbers:
        # Nothing to compare against (ROCm, or a build that reports no list);
        # torch skips its own check here too, so assume the card is fine.
        return True
    return _capability_number(capability) >= min(numbers)


def cuda_unusable_reason() -> Optional[str]:
    """A reason CUDA cannot be used here, phrased for a user, or None if it can."""

    if not torch.cuda.is_available():
        if not (torch.version.cuda or getattr(torch.version, "hip", None)):
            # The one failure that looks exactly like "no card" but is not: a
            # torch installed from PyPI instead of the vendor index. It has no
            # GPU support compiled in, so every stage silently runs on the CPU
            # on a machine that has a perfectly good card. Both vendors reach
            # it -- `pip install -e ".[asr]"` over a ROCm torch replaces it
            # with this, which is why AMD on Windows has its own extra.
            return (
                f"this PyTorch has no GPU support compiled in "
                f"({torch.__version__}); install it from the index for your "
                f"card, see docs/manual/repo-install.md"
            )
        return "it is unavailable"
    if is_rocm():
        warn_if_another_amd_card_is_larger()
        try:
            arch = _rocm_arch()
            entries = list(torch.cuda.get_arch_list())
            if arch is None or _rocm_build_has_kernels_for(arch, entries):
                return None
            name = torch.cuda.get_device_name()
        except Exception as exc:  # pragma: no cover - driver-level failure
            return f"the AMD device could not be queried ({exc})"
        return (
            f"{name} ({arch}) is not among the AMD architectures this ROCm build of "
            f"PyTorch has kernels for ({', '.join(entries)}); see docs/manual/resources.md"
        )
    try:
        capability = torch.cuda.get_device_capability()
    except Exception as exc:  # pragma: no cover - driver-level failure
        return f"the CUDA device could not be queried ({exc})"
    if _build_has_kernels_for(capability):
        return None
    # Only now is the device name worth the call: this runs on every stage.
    major, minor = capability
    return (
        f"{torch.cuda.get_device_name()} (compute capability {major}.{minor}) is "
        f"older than anything this PyTorch build has kernels for; it needs "
        f"{SUPPORTED_GPU_HINT}"
    )


def cuda_usable() -> bool:
    """True when CUDA is present *and* this build has kernels for the card.

    The replacement for a bare ``torch.cuda.is_available()`` anywhere the answer
    decides whether real work goes to the GPU.
    """

    return cuda_unusable_reason() is None


#: Local MOSS (`--asr moss-local`) runs on NVIDIA cards from this compute capability on: below it bf16 has no
#: tensor cores and the prefill's SDPA falls back to a kernel whose score matrix does not fit (owner 2026-11-03).
MOSS_LOCAL_MIN_COMPUTE_CAPABILITY = (8, 0)
MOSS_LOCAL_GPU_HINT = ("an NVIDIA card of compute capability 8.0 or newer (RTX 30 series and newer) with the free VRAM "
                       "of the standard tier (an 8 GB card)")
#: The free VRAM it needs (`standard`'s figure): below it the weights, the prefill's peak and the margin leave no
#: room for a minute of audio, so the run would fail after the 1.7 GB download and the load.
MOSS_LOCAL_MIN_USABLE_GIB = 6.5


def moss_local_unusable_reason(gpu_tier: Optional[str], device: Optional[str]) -> Optional[str]:
    """Why this run cannot decode with local MOSS, as the sentence to stop on, or None if it can.

    Unlike every other stage there is no CPU fallback: someone who picked MOSS gets MOSS or this error, not
    another model behind their back. `gpu_tier` and `device` are the run's `--gpu-tier` and `--device`.
    """

    from .resources import AUTO_GPU_TIER, RESOURCE_PROFILES, get_resource_profile

    tier = str(gpu_tier or "").strip().lower()
    reason = None
    if str(device or "").strip().lower() == "cpu":
        reason = "--device cpu keeps this run off the GPU"
    elif tier not in ("", AUTO_GPU_TIER) and tier in RESOURCE_PROFILES and not RESOURCE_PROFILES[tier].gpu:
        reason = f"--gpu-tier {tier} keeps this run off the GPU"
    elif (unusable := cuda_unusable_reason()) is not None:
        reason = f"the GPU cannot be used ({unusable})"
    elif is_rocm():
        reason = f"{gpu_name()} is an AMD card"
    else:
        try:
            capability = tuple(torch.cuda.get_device_capability())
        except Exception as exc:  # pragma: no cover - driver-level failure
            capability, reason = None, f"the CUDA device could not be queried ({exc})"
        if capability is not None and capability < MOSS_LOCAL_MIN_COMPUTE_CAPABILITY:
            reason = f"{gpu_name()} has compute capability {capability[0]}.{capability[1]}"
        elif reason is None:
            profile = get_resource_profile(gpu_tier, device=device)
            if profile.usable_gib < MOSS_LOCAL_MIN_USABLE_GIB:
                reason = f"the run's GPU tier ({profile.gpu_tier}) gives it {profile.usable_gib:g} GiB of VRAM"
    if reason is None:
        return None
    return (f"--asr moss-local needs {MOSS_LOCAL_GPU_HINT}, but {reason}; use --asr local (Whisper) or a cloud "
            "backend (docs/manual/joint-asr.md)")


def multiprocessor_count() -> int:
    """Streaming multiprocessors on the current card (a kernel's grid is sized by it)."""

    return int(torch.cuda.get_device_properties(torch.cuda.current_device()).multi_processor_count)


def cuda_device_present() -> bool:
    """Whether the driver reports a CUDA device at all -- **not** whether we can use it.

    Deliberately weaker than :func:`cuda_usable`, and the two must not be
    confused. This one answers a *policy* question ("does this machine have a
    GPU, so is a GPU tier even meaningful"); `cuda_usable` answers a
    *placement* question ("may torch put work on it"). A card too old for this
    PyTorch build is present but unusable: the tier should still be a GPU tier,
    because another backend -- the patched CTranslate2, which has its own arch
    list -- may still run on it, and folding torch's verdict into the tier would
    decide that for every stage at once.

    Never use it for placement. That is what `cuda_usable` is for, and the
    reason a bare ``torch.cuda.is_available()`` is banned everywhere else.

    ⚠ **`device_count`, not `is_available`.** They disagree, and the case where
    they do is one this project has to get right. Measured 2026-09-02:

    ======================= ================ ================ ==============
    ``CUDA_VISIBLE_DEVICES``  ``is_available``  ``device_count``  CT2 count
    ======================= ================ ================ ==============
    ``-1``                    False            0                0
    ``""`` (empty)            **True**         **0**            **1**
    ======================= ================ ================ ==============

    The empty spelling is the one the module docstring above names as a way to
    have no usable CUDA, and `is_available()` answers True to it -- which put a
    machine with zero visible devices on a GPU tier. `device_count()` is the
    question actually being asked.

    (CTranslate2 still reports a device under the empty spelling: its runtime
    reads that as "unset". The two libraries genuinely disagree there, which is
    exactly why the ASR stage asks CT2 and the tier asks torch -- neither is
    speaking for the other.)
    """

    try:
        return int(torch.cuda.device_count()) > 0
    except Exception:  # pragma: no cover - driver-level failure
        return False


def resolve_device(requested_device: str, *, context: str = "VAD-ASR") -> str:
    """Honour a CUDA request only when this machine can actually run it.

    Warns through the bound reporter and returns ``"cpu"`` otherwise. Called per stage, so a
    pipeline run says once per stage which device it settled on.
    """

    device = str(requested_device or "cuda")
    normalized = device.strip().lower()
    if not normalized.startswith("cuda"):
        return device
    if normalized != "cuda":
        # Refused rather than passed through. Nothing here plumbs a device
        # index: the capability check below reads the *current* device, the
        # backend hands the string to CTranslate2 which rejects anything but
        # "cuda" (it takes the index as a separate device_index), and every CLI
        # advertises cpu/cuda. Accepting "cuda:1" would mean falling back to CPU
        # on the wrong card's verdict, then failing with an opaque ValueError
        # from inside faster-whisper.
        raise ValueError(
            f"--device accepts 'cpu' or 'cuda', not {device!r}; select a "
            f"specific GPU with the CUDA_VISIBLE_DEVICES environment variable."
        )
    reason = cuda_unusable_reason()
    if reason is None:
        return device
    current_reporter().warning(
        "cpu-fallback",
        f"CUDA requested for {context} but {reason}; falling back to CPU.",
        impact="速度会显著下降",
    )
    return "cpu"


def total_vram_gib() -> Optional[float]:
    """Whole-card VRAM in GiB, or None when no usable CUDA device answers.

    Lives here for the same reason ``cuda_usable`` does: it is a question about
    *this machine's* GPU, and answering it anywhere else means answering it from
    a bare ``is_available()`` again. Gated on ``cuda_usable()`` rather than
    ``is_available()`` so a card this build has no kernels for reports nothing --
    its VRAM is not a budget anyone can spend.
    """

    if not cuda_usable():
        return None
    try:
        properties = torch.cuda.get_device_properties(torch.cuda.current_device())
    except Exception:  # pragma: no cover - driver-level failure
        return None
    return float(properties.total_memory) / float(1024**3)


def gpu_name(index: Optional[int] = None) -> str:
    """The card's marketing name, of the current device unless an index is given."""

    if index is None:
        return str(torch.cuda.get_device_name())
    return str(torch.cuda.get_device_name(index))


# --- A card the OS can see and torch cannot ---------------------------------

_WARNED_UNUSED_CARD = False

#: PCI vendor ids worth reporting. Intel is left out on purpose: no stage uses
#: an Intel GPU yet, so naming one would be an offer we cannot keep.
_GPU_PCI_VENDORS = {"10de": "NVIDIA", "1002": "AMD"}

#: Fields of Windows' DISPLAY_DEVICEW, for `EnumDisplayDevicesW`.
class _DisplayDeviceW(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("DeviceName", ctypes.c_wchar * 32),
        ("DeviceString", ctypes.c_wchar * 128),
        ("StateFlags", ctypes.c_uint32),
        ("DeviceID", ctypes.c_wchar * 128),
        ("DeviceKey", ctypes.c_wchar * 128),
    ]


def installed_gpu_vendors() -> frozenset[str]:
    """Vendors of the graphics cards the OS reports, ignoring what torch can do.

    Asked only when torch says there is nothing, so it cannot go through torch.
    Stdlib and read-only on both platforms: ``EnumDisplayDevicesW`` on Windows
    and PCI class 0x03 devices under ``/sys`` on Linux. macOS returns nothing --
    no stage runs on an Apple GPU, so there is nothing to report.

    ⚠ **Windows asks the display API, not the driver registry.** The registry's
    display class keeps a subkey per adapter ever installed: this machine still
    lists the RTX 5060 Ti that was physically removed in 2026-08, so a machine
    whose card left (or changed vendor) would be told forever that it has one.
    A compute card with no outputs may not enumerate here, which errs the safe
    way -- silence, the behaviour before this probe existed.
    """

    vendors: set[str] = set()
    try:
        if os.name == "nt":
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            index = 0
            while True:
                device = _DisplayDeviceW()
                device.cb = ctypes.sizeof(_DisplayDeviceW)
                if not user32.EnumDisplayDevicesW(None, index, ctypes.byref(device), 0):
                    break
                index += 1
                identifier = str(device.DeviceID).lower()
                if not identifier.startswith("pci\\ven_"):
                    continue
                vendors.update(
                    label
                    for vendor, label in _GPU_PCI_VENDORS.items()
                    if identifier.startswith(f"pci\\ven_{vendor}")
                )
        elif sys.platform.startswith("linux"):
            for device in Path("/sys/bus/pci/devices").glob("*"):
                try:
                    if not (device / "class").read_text().strip().startswith("0x03"):
                        continue
                    vendor = (device / "vendor").read_text().strip().lower()
                except OSError:
                    continue
                label = _GPU_PCI_VENDORS.get(vendor.removeprefix("0x"))
                if label:
                    vendors.add(label)
    except Exception:  # pragma: no cover - a probe must never break a run
        return frozenset()
    return frozenset(vendors)


def warn_if_an_installed_card_is_unused() -> None:
    """Say once that a card is sitting there unused, when `auto` falls to the CPU.

    Falling to the CPU tier is right when the machine has no card, and silence
    is right with it. It is the same silence, though, when a card *is* there and
    the environment cannot reach it -- a torch without GPU support (one `[asr]`
    over a ROCm install does that), a missing or too-old driver, a
    ``CUDA_VISIBLE_DEVICES`` that hides everything. That run is many times
    slower for a reason nothing on screen mentions, which is what this says
    (owner 2026-09-17).
    """

    global _WARNED_UNUSED_CARD
    if _WARNED_UNUSED_CARD:
        return
    vendors = installed_gpu_vendors()
    if not vendors:
        return
    built_for = torch.version.cuda or getattr(torch.version, "hip", None)
    found = " / ".join(sorted(vendors))
    if not built_for:
        action = (
            f"这个 PyTorch（{torch.__version__}）不带 GPU 支持，按 {REPO_INSTALL_DOC} "
            f"从对应的索引重装 torch 三件套"
        )
    elif "AMD" in vendors and not is_rocm():
        # Naming the range matters more here than the instructions: AMD's
        # Windows ROCm covers only RX 7000/9000 and the Strix APUs, so for an
        # older card or an iGPU there is nothing to do, and a hint that does not
        # say so is one the user will chase.
        action = (
            f"A 卡要装 ROCm 版 PyTorch，而托管 CLI 装的是 NVIDIA 版；Windows 上这条路"
            f"只覆盖 RX 7000 / 9000 系与 Strix APU，且要改用源码装法（{REPO_INSTALL_DOC}"
            f"「Windows 上的 A 卡」）。不在这个范围内的卡目前只能走 CPU"
        )
    else:
        action = (
            "确认显卡驱动已安装且不过旧；若是有意屏蔽，检查 CUDA_VISIBLE_DEVICES / "
            "HIP_VISIBLE_DEVICES 之类的环境变量"
        )
    reporter = current_reporter()
    reporter.warning(
        "gpu-unused",
        f"a graphics card is installed ({found}) but nothing here can use it; "
        f"this run is on the CPU",
        impact="速度会比用得上显卡时慢很多（人声分离尤其）",
        action=action,
    )
    # Only a reporter that actually shows it counts as having said it, the same
    # rule `support_levels.note_accelerated_stage` follows: a first call on a
    # thread with no bound reporter would otherwise eat the one warning.
    if reporter_delivers(reporter):
        _WARNED_UNUSED_CARD = True


# --- Which card, when there are several -------------------------------------

_WARNED_LARGER_CARD = False


def larger_amd_card_index() -> Optional[int]:
    """Another visible AMD card with more memory than device 0, or None.

    On AMD an integrated GPU is often device 0 (Ryzen 7000 desktops, most
    laptops with a discrete card), and every placement, the tier and the kernel
    check then read the iGPU. This does **not** switch cards: torch's current
    device is per thread, so a process-wide ``set_device`` would leave the
    separator's worker threads on device 0 while the model sits elsewhere, and
    CTranslate2 takes its own index. Choosing is the user's, through a
    visible-devices variable, which applies to every thread and library at once;
    this only finds the card worth pointing them at. None when a user already
    chose (any visible-devices variable set), on NVIDIA, or with one card.
    """

    if not is_rocm():
        return None
    if any(os.environ.get(name) for name in _VISIBLE_DEVICE_VARIABLES):
        return None
    try:
        count = int(torch.cuda.device_count())
        if count <= 1:
            return None
        totals = [
            int(torch.cuda.get_device_properties(index).total_memory)
            for index in range(count)
        ]
    except Exception:  # pragma: no cover - driver-level failure
        return None
    best = max(range(count), key=lambda index: (totals[index], -index))
    return best or None


def warn_if_another_amd_card_is_larger() -> None:
    """Say once per process which card to select, when device 0 is not the largest."""

    global _WARNED_LARGER_CARD
    if _WARNED_LARGER_CARD:
        return
    index = larger_amd_card_index()
    if index is None:
        return
    _WARNED_LARGER_CARD = True
    try:
        names = (torch.cuda.get_device_name(0), torch.cuda.get_device_name(index))
    except Exception:  # pragma: no cover - driver-level failure
        names = ("device 0", f"device {index}")
    current_reporter().warning(
        "amd-device-choice",
        f"using {names[0]} (device 0), but {names[1]} (device {index}) has more memory",
        impact="集成显卡可能被当成主显卡，速度与档位都按它来",
        action=f"设置 HIP_VISIBLE_DEVICES={index} 后重跑，只让程序看见那张卡",
    )
