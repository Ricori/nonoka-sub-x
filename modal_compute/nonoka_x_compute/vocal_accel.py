"""Install FineSub's weightless AOTI packages on the cloud Vocal worker.

**Whether to install is decided per task, from the input's length.**  The
packages are worth 1.6-1.7x on the separation itself, with output numerically
identical to eager (SI-SDR 68-71 dB against it, correlation 1.000000).  But the
first ``aoti_load_package`` in a process costs 11-13 s -- a fixed runtime
initialization, not I/O and not per-runner: with the archives already on local
disk the load still took 11.3 s, and once paid, 24 more runners went on in
0.33 s.  This worker has a short scale-down window and each key may hold one task,
so a container separates once and dies; the install is paid in full every time.

That makes length the whole question.  At 32 kHz the accelerator saves 0.0435 s
per second of source (measured over 31.4 minutes: 217.9 s eager against 135.9 s),
and 0.0544 s at 44.1 kHz, so it breaks even somewhere between 240 and 300 s of
audio depending on the tier.  Below that, accelerating a four-minute song loses
a couple of seconds; at the two-hour ceiling the API accepts, it saves about
300 s of T4 time.  ``wanted()`` applies ``ACCEL_MIN_AUDIO_SEC``, and
``vocal`` calls it on the first real input rather than at container
start, where there is no input to weigh.

Two things are deliberately done here rather than through FineSub's own
``accel.apply_acceleration``:

* **The attention backend.** FineSub pins one SDPA kernel per transformer axis,
  chosen on an L4, and ``CUDNN_ATTENTION`` has no sm75 implementation -- on a T4
  the build dies with ``No available kernel``.  ``build_packages`` already takes
  ``attention_backend="auto"``, which is upstream's own parameter, so choosing it
  needs no vendor patch; only the installer FineSub wraps it in lacks the
  plumbing, and that is what this module replaces.
* **The cache key.** Inductor compiles the host-side wrapper with
  ``-march=native``.  A package built on one Modal worker and loaded on another
  with a narrower instruction set would not raise -- it would take the container
  down with an illegal instruction.  Modal's T4 fleet is not uniform (three
  distinct CPU feature sets showed up across a handful of runs, with 13%
  throughput spread), so the CPU's own feature set joins torch, CUDA, SM and
  batch size in the directory name.

Nothing here may raise into the stage.  Losing the accelerator costs seconds;
failing the task costs the whole run, so every path degrades to eager.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable


#: Where the models Volume is mounted on the Vocal worker.  Not ``/models``:
#: that path holds the checkpoint baked into the image, and a Volume mounted
#: over it would hide the weights.
ACCEL_ROOT = Path("/accel")

#: Set on the image so FineSub resolves its artefact root under the Volume.
MODEL_DIR_ENV = "FINESUB_MODEL_DIR"

#: ``auto`` (default), ``on`` or ``off``.  ``auto`` installs only when the input
#: is long enough to pay the install back; see ``ACCEL_MIN_AUDIO_SEC``.
ENABLED_ENV = "NONOKA_VOCAL_ACCEL"

#: How much audio makes the fixed install worth paying, in seconds of source.
#:
#: The saving is linear in duration and in rate, so each tier has its own
#: break-even against the ~13 s install: 239 s at 44.1 kHz (0.0544 s saved per
#: second of source), 299 s at 32 kHz (0.0435), 447 s at 22.05 kHz (0.0291).
#: 270 s clears the only one that matters, because the duration ladder in
#: ``vocal_policy`` hands every input under 12 minutes to the 44.1 kHz tier --
#: the tiers with the later break-evens are never selected until well past it.
#:
#: Above the threshold the gain grows without bound: a two-hour input, the most
#: the API accepts, saves about 300 s of T4 time.
ACCEL_MIN_AUDIO_SEC = 270.0

#: Only the 12 transformer pairs are compiled. ``band_split`` and
#: ``mask_estimator`` were measured separately and saved nothing at all (25.91 s
#: against 25.59 s eager), while costing 66 s of the build. Skipping them also
#: keeps ``build_packages`` out of ``_capture_module_inputs``, which is the only
#: thing that would drag FineSub's separation module -- and therefore torchaudio
#: -- into this image: with no captured examples the builder falls back to
#: upstream's ``AXES`` shapes, and at batch size 1 those are exactly the shapes a
#: capture produces ([62, 801, 512] and [801, 62, 512], verified against a
#: captured build's manifest).
BUILD_TARGETS = "transformers"


def wanted(audio_sec: float | None) -> tuple[bool, str]:
    """Whether to install for this input, and the reason either way.

    The install is a per-container constant and the saving is proportional to
    duration, so the answer is a property of the *task*, not of the deployment:
    a four-minute song loses two seconds by accelerating and a two-hour recording
    gains five minutes.  ``auto`` therefore decides per input, and the explicit
    settings exist for pinning the behaviour while measuring.
    """

    mode = os.environ.get(ENABLED_ENV, "auto").strip().lower()
    if mode in {"0", "off", "no", "false"}:
        return False, f"{ENABLED_ENV}=off"
    if mode in {"1", "on", "yes", "true"}:
        return True, f"{ENABLED_ENV}=on"
    if audio_sec is None:
        # Nothing to weigh the install against; the short case is the common one.
        return False, "input duration is unknown"
    if audio_sec < ACCEL_MIN_AUDIO_SEC:
        return False, f"input is {audio_sec:.0f}s, under the {ACCEL_MIN_AUDIO_SEC:.0f}s threshold"
    return True, f"input is {audio_sec:.0f}s"


def cpu_tag() -> str:
    """Identify the instruction set a compiled wrapper may assume.

    Reads the flag set rather than the model name: two workers can report
    different models and run the same ISA, and it is the ISA that decides
    whether ``-march=native`` output is loadable.  An unreadable cpuinfo falls
    back to a constant, which merely means the tag stops discriminating -- the
    package is still keyed by torch, CUDA and SM.
    """

    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            name, separator, value = line.partition(":")
            if separator and name.strip() == "flags":
                flags = " ".join(sorted(value.split()))
                return hashlib.sha256(flags.encode("utf-8")).hexdigest()[:12]
    except OSError:
        pass
    return "cpu-unknown"


def package_dir(model_name: str) -> Path | None:
    """Where this exact stack's packages live, or None if there can be none.

    Upstream's key already covers torch, CUDA and the SM version; the CPU tag
    is appended here rather than folded into it because only this worker
    compiles with ``-march=native`` and then reuses the result across machines.
    Batch size is not part of it: the cloud separates at batch 1 and nothing
    here offers another.
    """

    from finesub.speech.preprocessing.separator import accel

    paths = accel.resolve_accel_paths(model_name)
    if paths is None:
        return None
    return paths.root / cpu_tag() / "aoti"


def warm_up(model_instance: Any) -> None:
    """One forward at the shape the packages are built and loaded against.

    FineSub does this inside its separator pool, which the lean cloud worker
    does not go through.  It has to happen before either build or load: the
    build captures its example inputs from a forward exactly like this one, and
    both build and load read the rotary cache it populates.
    """

    import torch

    model = model_instance.model_run
    config = model_instance.model_data_cfgdict
    hop = getattr(config.model, "stft_hop_length", None) or config.audio.hop_length
    chunk = int(hop) * (int(config.inference.dim_t) - 1)
    channels = int(getattr(model, "audio_channels", 2))
    batch = max(1, int(getattr(model_instance, "batch_size", 1)))
    with torch.inference_mode(), torch.autocast("cuda", enabled=True):
        model(torch.zeros(batch, channels, chunk, dtype=torch.float32, device="cuda"))
    torch.cuda.synchronize()
    torch.cuda.empty_cache()


def load_subset(
    model_instance: Any,
    package_dir: Path,
    scratch: Path,
    keep: set[str] | None = None,
) -> int:
    """Install only some of the compiled modules; return the runner count.

    Installing everything costs ~0.43 s per runner and there are 26 of them, so
    which modules are worth their load time is a real question -- FineSub's own
    installer answers it "all of them", which is right for a process that runs
    many separations and wrong for a container that runs one.

    Done by filtering the manifest rather than by reimplementing the loader:
    ``load_packages`` iterates whatever the manifest lists, so a copy of the
    directory carrying a shorter manifest installs exactly that subset, with no
    vendor patch and no duplicate of the constant-injection logic.
    """

    import json
    import shutil

    from finesub.speech.preprocessing.separator import separator_aoti

    manifest = json.loads(
        (package_dir / "manifest.json").read_text(encoding="utf-8")
    )
    if keep is None:
        return separator_aoti.load_packages(model_instance, package_dir)
    packages = {
        name: entry for name, entry in manifest["packages"].items() if name in keep
    }
    missing = sorted(keep - set(packages))
    if missing:
        raise RuntimeError(f"package directory has no entry for: {missing}")
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    for entry in packages.values():
        shutil.copyfile(package_dir / entry["file"], scratch / entry["file"])
    (scratch / "manifest.json").write_text(
        json.dumps({**manifest, "packages": packages}, ensure_ascii=False),
        encoding="utf-8",
    )
    return separator_aoti.load_packages(model_instance, scratch)


def _publish(built: Path, destination: Path) -> bool:
    """Move a finished build into the shared cache; False if it lost a race.

    Two cold containers can build at once.  The loser does not retry and does
    not delete the winner's work -- it just loads what is now there, which is
    equivalent by construction.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(built, destination)
        return True
    except OSError:
        # Cross-device (the build happens on container disk, the cache lives on
        # a Volume) or a destination that appeared meanwhile.
        if destination.exists():
            return False
        try:
            shutil.copytree(built, destination)
            return True
        except (OSError, shutil.Error):
            return destination.exists()


def install(
    model_instance: Any,
    *,
    model_name: str,
    build_dir: Path,
    commit: Callable[[], None] | None = None,
    allow_build: bool = True,
    force: bool = False,
    audio_sec: float | None = None,
) -> dict[str, Any]:
    """Warm the model, then put AOTI on it; report what actually stuck.

    ``allow_build`` lets a caller ask for load-only.  ``force`` bypasses the
    decision entirely, which is what ``prime_container`` needs: priming is an
    explicit operator action against a synthetic input, so it has no duration to
    weigh and must build regardless.
    """

    started = time.perf_counter()
    outcome: dict[str, Any] = {"backend": "eager", "cpu_tag": cpu_tag()}
    if audio_sec is not None:
        outcome["audio_sec"] = round(float(audio_sec), 1)
    if not force:
        install_wanted, reason = wanted(audio_sec)
        outcome["reason"] = reason
        if not install_wanted:
            return outcome
    try:
        # Imported behind the switch, and guarded: FineSub reaches this image as
        # a mount rather than an installed package, and a missing mount must
        # cost the accelerator, not the container.
        from finesub.speech.preprocessing.separator import separator_aoti
    except Exception as exc:
        outcome["reason"] = f"import: {type(exc).__name__}: {exc}"
        return outcome
    try:
        warm_up(model_instance)
        outcome["warm_up_sec"] = round(time.perf_counter() - started, 3)
    except Exception as exc:
        # A model that cannot even run a zero forward is not an accelerator
        # problem; let the separation itself surface it.
        outcome["reason"] = f"warm-up: {type(exc).__name__}: {exc}"
        return outcome

    try:
        destination = package_dir(model_name)
    except Exception as exc:
        outcome["reason"] = f"resolve: {type(exc).__name__}: {exc}"
        return outcome
    if destination is None:
        outcome["reason"] = "no accel cache directory on this container"
        return outcome
    outcome["package_dir"] = str(destination)

    ready = (destination / "manifest.json").is_file()
    if not ready:
        if not allow_build:
            outcome["reason"] = "no package and building is disabled here"
            return outcome
        build_started = time.perf_counter()
        scratch = build_dir / f"aoti-build-{os.getpid()}"
        shutil.rmtree(scratch, ignore_errors=True)
        try:
            # `auto` rather than FineSub's default `axis`; see the module note.
            separator_aoti.build_packages(
                scratch,
                model_instance=model_instance,
                attention_backend="auto",
                targets=BUILD_TARGETS,
            )
            outcome["build_sec"] = round(time.perf_counter() - build_started, 3)
            outcome["published"] = _publish(scratch, destination)
            if commit is not None:
                commit()
        except Exception as exc:
            outcome["reason"] = f"build: {type(exc).__name__}: {exc}"
            return outcome
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    else:
        outcome["reused_package"] = True

    # Stage the packages onto container disk before loading them. FineSub's
    # loader re-zips each package once per transformer block -- 26 runners off
    # 13 MB of archives -- and doing that against a Volume mount is a different
    # cost from doing it against local disk. Copying first makes the split
    # visible in the timings instead of hiding it inside load_sec, and the copy
    # is the part that a warm worker's Volume cache makes cheap.
    fetch_started = time.perf_counter()
    staged = build_dir / "aoti-staged"
    try:
        shutil.rmtree(staged, ignore_errors=True)
        shutil.copytree(destination, staged)
        outcome["package_bytes"] = sum(
            item.stat().st_size for item in staged.iterdir() if item.is_file()
        )
        outcome["fetch_sec"] = round(time.perf_counter() - fetch_started, 3)
    except Exception as exc:
        outcome["reason"] = f"stage: {type(exc).__name__}: {exc}"
        return outcome

    load_started = time.perf_counter()
    try:
        outcome["runners"] = separator_aoti.load_packages(model_instance, staged)
    except Exception as exc:
        # A package that will not load is stale or was built for another
        # machine. Drop it so the next container rebuilds rather than failing
        # the same way forever.
        shutil.rmtree(destination, ignore_errors=True)
        if commit is not None:
            try:
                commit()
            except Exception:
                pass
        outcome["reason"] = f"load: {type(exc).__name__}: {exc}"
        return outcome
    outcome["load_sec"] = round(time.perf_counter() - load_started, 3)
    outcome["backend"] = "aoti"
    outcome["total_sec"] = round(time.perf_counter() - started, 3)
    return outcome
