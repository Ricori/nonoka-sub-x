"""The four task phases every cloud container runs through.

Not a container module: `prepare`, `aligned-prepare`, `gpu` and `cpu` are one
function with a phase switch, and the two containers that call it sit on
opposite sides of the CPU/GPU split -- the scheduler runs `prepare`,
`aligned-prepare` and `cpu` in its own process and spawns `run_aligned_stage`
for `gpu`.

What keeps the phases in one function rather than four is the state they
share: the same reporter, the same run fence, the same Volume commit discipline
and the same failure classification have to behave identically whichever leg
is running, because a task can be resumed into any of them.

Every word about the job goes through `JobChannel` to the Worker, which owns
the record and its log. The working files stay on the scratch Volume under
`tasks/<job_id>/`, and they are what makes a resumed run skip what it already
has.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path

from .channel import JobChannel, TaskCancelled
from .gpu_metrics import NvmlProcessMemorySampler, format_bytes
from .pipeline_rules import diarization_requested, source_duration_fault
from .resources import models_volume, scratch_volume, task_dir
from .vocal_policy import PREPARED_SOURCE_NAME, tier_for_request


class SourceRejected(RuntimeError):
    """The uploaded audio is not the audio the task was accepted for."""


def probe_duration(source) -> float:
    """Read the real length of the downloaded source, or 0 if it cannot be.

    ffprobe rather than a decode: the check has to happen before the prepare
    phase spends CPU on a file it may be about to refuse, and the container
    already carries ffmpeg. Never raises -- see `source_duration_fault` for why
    an unreadable probe must not be a task failure.
    """

    import subprocess

    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(source),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return float(probe.stdout.strip())
    except Exception:
        return 0.0


PHASE_LABELS = {
    "prepare": "CPU 调度容器正在准备源音频",
    "gpu": "GPU 容器已启动",
    "aligned-prepare": "CPU 容器正在执行 VAD/Whisper/Qwen",
    "cpu": "CPU 调度容器正在执行后处理",
}


class CloudReporter:
    """FineSub's reporter interface, published through the run's channel."""

    IMPORTANT_DEBUG = frozenset({
        "asr stabilize",
        "resource usage",
        "gpu memory usage",
        "separator timing",
        "aligned prepare timing",
        "qwen finalize timing",
        "silero assist",
        "stage timing",
        "subtitle length scale",
        "timing",
        "wrote subtitle",
    })

    # Warning codes that stay in the container log and never reach the
    # user-facing task timeline.  All are engine telemetry that names a
    # condition the user did not cause and cannot act on.
    #
    # `srt-line-budget`: the SRT validator measures each line against a
    # 25-weighted-character literal of its own, but line length is decided
    # upstream by the segmentation DP, whose walls are 20 (ideal) and 36
    # (ok) and travel with the user's length-scale knob -- and single-source
    # lines are exempt from the merge thresholds entirely.  Every line the
    # DP deliberately placed in (25, 36] therefore reports itself, in
    # English, with an empty impact and action, naming an internal
    # `/__modal/volumes/...` path the reader cannot open.
    #
    # `unverified-accelerator`: the support matrix has no row for a cloud
    # GPU on Linux, so every Whisper and Silero load says so -- about the hardware
    # this deployment chose, with advice (`--device cpu`) a cloud user has
    # no way to follow.
    #
    # `resource-budget`: the engine measures peak memory against the tier
    # it was calibrated on, not the card the task got; the L4 has 24 GiB and
    # finishes runs that overshoot the 8 GiB tier by a few percent.
    OPERATOR_ONLY_WARNINGS = ("srt-line-budget", "unverified-accelerator", "resource-budget")

    def __init__(self, channel: JobChannel, *, phase: str, volume=scratch_volume) -> None:
        self.channel = channel
        self.job_id = channel.job_id
        self.run_id = channel.run_id
        self._volume = volume
        self._gpu_phase = phase == "gpu"
        self._postprocess_phase = phase == "cpu"
        self._lock = threading.RLock()
        self._last_commit = time.monotonic()
        self._last_progress_log: dict[str, float] = {}
        self._stage = "starting"

    def _print(self, event_type: str, payload: dict) -> None:
        print(json.dumps({
            "scope": "finesub",
            "task_id": self.job_id,
            "run_id": self.run_id,
            "type": event_type,
            **payload,
        }, ensure_ascii=False, default=str), flush=True)

    def _commit_checkpoint(self, reason: str, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_commit < 30:
            return
        self._volume.commit()
        self._last_commit = now
        self._print("checkpoint", {"stage": self._stage, "message": reason})

    def _update(self, event_type: str, payload: dict) -> None:
        with self._lock:
            # Diagnostics still belong in Modal's container logs, but not
            # in the user-facing task timeline.  Persisting every timing,
            # resource and setup line made one logical pipeline look like
            # dozens of unrelated steps as it crossed CPU/GPU containers.
            if event_type == "log":
                self.channel.ensure_active()
                self._print(event_type, payload)
                return
            if event_type == "stage":
                self.channel.stage(str(payload.get("stage") or self._stage), payload)
            elif event_type == "progress":
                # A repeated progress value is still worth a container log
                # line, but a task event for it would grow the timeline
                # without telling the reader anything new -- and the
                # channel holds back values that arrive faster than 1/s.
                self.channel.progress(str(payload.get("stage") or self._stage), payload)
            elif event_type == "warning":
                self.channel.warning(payload)
            else:
                raise ValueError(f"unsupported event type: {event_type}")
            should_print = event_type != "progress"
            if event_type == "progress":
                now = time.monotonic()
                stage = str(payload.get("stage") or self._stage)
                last = self._last_progress_log.get(stage, 0.0)
                should_print = (
                    now - last >= 10
                    or payload.get("completed") == payload.get("total")
                )
                if should_print:
                    self._last_progress_log[stage] = now
            if should_print:
                self._print(event_type, payload)

    def planned(self, stages) -> None:
        values = list(stages)
        self._update("log", {"stage": "pipeline", "message": "流水线计划已生成", "stages": values, "total": len(values)})

    def stage_started(self, stage: str, *, reused: bool = False, detail: str = "") -> None:
        # During CPU postprocessing phase, run_pipeline checks earlier
        # prerequisites (vocal, aligned) from the Volume. Re-broadcasting
        # them as stage updates regresses the visible stage and progress.
        if self._postprocess_phase and reused and stage in {"vocal", "aligned"}:
            return
        with self._lock:
            if self._stage != "starting":
                self._commit_checkpoint(f"阶段 {self._stage} 已完成", force=True)
            self._stage = stage
        self._update("stage", {"stage": stage, "message": detail or ("复用已有结果" if reused else "正在处理"), "reused": reused})

    def progress(self, stage: str, *, completed: int, total: int | None = None, unit: str = "", detail: str = "") -> None:
        with self._lock:
            self._stage = stage
            self._commit_checkpoint("定时保存处理中间结果")
        self._update("progress", {"stage": stage, "completed": completed, "total": total, "unit": unit, "message": detail})

    def summary(self, stage: str, metrics) -> None:
        with self._lock:
            self._stage = stage
            self._commit_checkpoint(f"阶段 {stage} 摘要已保存", force=True)
        self._update("log", {"stage": stage, "message": "阶段摘要", "metrics": dict(metrics)})

    def warning(self, code: str, message: str, *, impact: str = "", action: str = "") -> None:
        payload = {"code": code, "message": message, "impact": impact, "action": action}
        if code.startswith(self.OPERATOR_ONLY_WARNINGS):
            with self._lock:
                self.channel.ensure_active()
                self._print("warning", payload)
            return
        self._update("warning", payload)

    def debug(self, message: str, fields=None) -> None:
        normalized = message.strip().lower()
        if normalized in self.IMPORTANT_DEBUG or normalized.startswith("skipping "):
            values = dict(fields or {})
            if (
                self._gpu_phase
                and normalized == "resource usage"
                and values.get("peak_gmem") == "0.00 B"
            ):
                values["peak_gmem"] = "untracked by PyTorch; see NVML metric"
                values["gmem_source"] = "torch-allocator"
            self._update("log", {"stage": self._stage, "message": message, "fields": values})

    def completed(self, output, elapsed_sec: float) -> None:
        with self._lock:
            self._commit_checkpoint("FineSub 流水线完成", force=True)
        self._update("log", {"stage": self._stage, "message": "FineSub 流水线完成", "output": str(output), "elapsed_sec": round(elapsed_sec, 3)})

    def failed(self, stage: str, message: str) -> None:
        # The engine's own notice that it is about to raise. The failure itself
        # is published once, by the phase's handler, which knows its code.
        with self._lock:
            self._stage = stage
            self._commit_checkpoint("失败前保存断点", force=True)
        self._print("failed", {"stage": stage, "message": message})


def result_artifacts(request: dict, stable_json: Path, diar_json: Path) -> dict[str, str]:
    """What the CPU postprocess leg hands the Worker.

    The stable JSON is the whole of the transcript: everything downstream of it,
    the raw SRT included, is recomputed on the desktop by `stages._use_or_create`.
    The speaker timeline rides along only when the request asked for it, so the
    desktop can run the judge levels over it the way a local run would.
    """

    candidates = {"stable_json": Path(stable_json)}
    if diarization_requested(request):
        candidates["diar_json"] = Path(diar_json)
    return {name: path.read_text(encoding="utf-8") for name, path in candidates.items() if path.is_file()}


def run_pipeline_phase(
    owner_id: str,
    job_id: str,
    request: dict,
    run_id: str,
    *,
    phase: str,
    qwen_referee_instance=None,
) -> None:
    """Run one leg selected by the CPU scheduler."""
    del owner_id
    import warnings

    # Numba falls back to Python's supported SipHash implementation.  The
    # warning describes no loss of functionality for FineSub and otherwise
    # appears once in both the CPU preparation and GPU alignment containers.
    warnings.filterwarnings(
        "ignore",
        message=r"FNV hashing is not implemented in Numba.*",
        category=UserWarning,
        module=r"numba\.cpython\.hashing",
    )
    if phase not in PHASE_LABELS:
        raise ValueError(f"unknown FineSub worker phase: {phase}")

    # A warm worker can otherwise retain a stale Volume view after another
    # container committed checkpoints for this task.
    try:
        scratch_volume.reload()
    except Exception:
        scratch_volume.commit()
        scratch_volume.reload()
    if phase in {"gpu", "aligned-prepare"}:
        # A warm container has a snapshot of the model Volume from when it was
        # created. Refresh it before loading anything so AOTI packages or model
        # files produced by another container are reused instead of rebuilt.
        try:
            models_volume.reload()
        except Exception:
            # Model loaders can still use the container's existing snapshot;
            # a cache refresh must never turn an otherwise runnable task into
            # a failure.
            pass

    channel = JobChannel(job_id, run_id)
    # The Worker only learns the scheduler's call id once `/spawn` has
    # returned. Publish nothing until it has, or a cancel arriving in between
    # could not reach the call that is running. A run that is no longer live --
    # cancelled, superseded, or already failed by another container -- does
    # not start work: /retry and /resume both mint a fresh run id.
    if channel.wait_dispatched() is None:
        return
    reporter = CloudReporter(channel, phase=phase)
    try:
        channel.stage("starting", {"stage": "starting", "message": PHASE_LABELS[phase], "run_id": run_id})
    except TaskCancelled:
        return

    work = task_dir(job_id)
    work.mkdir(parents=True, exist_ok=True)
    source = work / "source.m4a"
    vocal_tier = tier_for_request(request)
    vocal_source = work / PREPARED_SOURCE_NAME
    output = work / "subtitle.srt"
    prepare_phase = phase == "prepare"
    aligned_prepare_phase = phase == "aligned-prepare"
    gpu_phase = phase == "gpu"
    postprocess_phase = phase == "cpu"
    # Whisper is the only stage this deployment puts on a GPU: separation has
    # its own worker (`VocalSeparator`), and VAD and the Qwen referee were
    # moved onto the CPU scheduler.
    #
    # `stable` is where the cloud stops, whatever the stored request asks for:
    # correction and translation are the desktop's work, so nothing here could
    # serve a `final-srt` target.
    pipeline_target = {"gpu": "aligned", "cpu": "stable"}.get(phase)

    try:
        reporter._update("log", {"stage": "starting", "message": "正在准备云端运行环境"})
        if source.is_file():
            reporter._update("log", {"stage": "starting", "message": "复用已下载的源音频"})
        elif prepare_phase:
            reporter._update("log", {"stage": "starting", "message": "正在下载源音频"})
            channel.download_source(source)
            scratch_volume.commit()
            reporter._update("log", {"stage": "starting", "message": "源音频下载完成"})
        else:
            raise FileNotFoundError(
                "prepared source audio is missing from the shared Volume"
            )

        if prepare_phase:
            # The first point at which the real audio exists. Everything the
            # Worker checked about length -- the two-hour ceiling, and the tier
            # the separator will run at -- it checked against a number the
            # client sent before uploading anything, so this is where that
            # claim is tested. Before the decode, so a refused source costs no
            # CPU.
            fault = source_duration_fault(
                (request.get("source") or {}).get("duration") or 0,
                probe_duration(source),
            )
            if fault:
                raise SourceRejected(fault)
            if vocal_source.is_file():
                reporter._update(
                    "log",
                    {
                        "stage": "starting",
                        "message": f"复用已解码音频（{vocal_tier.label}）",
                    },
                )
            else:
                import subprocess

                decode_started = time.perf_counter()
                vocal_part = work / f".{PREPARED_SOURCE_NAME}.part"
                vocal_part.unlink(missing_ok=True)
                reporter._update(
                    "log",
                    {
                        "stage": "starting",
                        "message": f"CPU 正在解码源音频（{vocal_tier.label}）",
                    },
                )
                try:
                    subprocess.run(
                        [
                            "ffmpeg",
                            "-hide_banner",
                            "-loglevel",
                            "error",
                            "-y",
                            "-i",
                            str(source),
                            "-map",
                            "0:a:0",
                            "-vn",
                            # Two channels because separating in stereo is the
                            # policy `vocal_policy` exists to state. A mono
                            # source is duplicated here rather than inside the
                            # demix runner, which is the same signal either
                            # way, and anything wider is folded -- which the
                            # desktop does not do, and where the checkpoint
                            # would refuse the input outright.
                            "-ac",
                            str(vocal_tier.channels),
                            # Deliberately no `-ar` and no `-sample_fmt`. This
                            # is the desktop's `transcode_to_lossless_audio`,
                            # which changes the container and nothing else, and
                            # the tier's rate is applied afterwards by
                            # `demix.separate_waveform` resampling each block
                            # through librosa -- which is where the desktop
                            # applies it too. Resampling here put ffmpeg's
                            # resampler in that position, and an ASR chain that
                            # can tell FineSub's delivery downmix from ffmpeg's
                            # can tell these apart as well. A 44.1 kHz source
                            # never noticed, because neither side resampled it;
                            # a 48 kHz one, which is most video, would have.
                            #
                            # Dropping `-sample_fmt` is what stops truncating
                            # the separator's input. `pcm_s16le` cost 73 dB
                            # against the desktop's 24-bit decode, and
                            # BS-Roformer turned that into a stem that
                            # transcribed differently: measured on a 127 s
                            # sample, 16-bit in produced
                            # "エアーなぁー秒でバレたって..." where the
                            # desktop's own decode produced
                            # "エアバレたー!秒でバレたって...".
                            "-c:a",
                            "flac",
                            # ffmpeg infers the container from the suffix, and
                            # the atomic temp does not end in .flac.
                            "-f",
                            "flac",
                            str(vocal_part),
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=15 * 60,
                    )
                    os.replace(vocal_part, vocal_source)
                finally:
                    vocal_part.unlink(missing_ok=True)
                scratch_volume.commit()
                reporter._update(
                    "log",
                    {
                        "stage": "starting",
                        "message": "vocal prepare timing",
                        "fields": {
                            "device": "cpu",
                            # The tier is recorded but no longer applied here:
                            # this decode keeps the source's own rate, and the
                            # separator resamples each block to the tier's.
                            "tier": vocal_tier.name,
                            "separator_sample_rate": str(vocal_tier.sample_rate),
                            "channels": str(vocal_tier.channels),
                            "elapsed_sec": f"{time.perf_counter() - decode_started:.3f}",
                        },
                    },
                )
            return

        if gpu_phase:
            # Fast entry for the billed GPU path: importing finesub.stages
            # initializes LLM and postprocess routes this child never uses.
            from finesub.reporting import quieted_libraries, reporting_to
            from finesub import settings as finesub_settings
            from finesub.speech.recognition import vad_asr_stage
            from nonoka_x.gpu_tier import resolve_request_gpu_tier

            vocal_audio = work / "subtitle-vocal.ogg"
            aligned_output = work / "subtitle-aligned.json"
            # Derived, never restated: the CPU container writes the prefix
            # under the name upstream gives it, and both sides ask the same
            # function for it.
            prepared_vad = vad_asr_stage.default_vad_prefix_path(aligned_output)
            metadata_path = work / "subtitle-metadata.json"
            reporter.planned(["aligned"])
            reporter.stage_started("aligned")
            aligned_started = time.perf_counter()
            temporary_aligned = aligned_output.with_name(
                f".{aligned_output.stem}.gpu-part{aligned_output.suffix}"
            )
            temporary_aligned.unlink(missing_ok=True)
            gpu_memory = NvmlProcessMemorySampler().start()

            # No hot-word context for the referee: the knowledge base it was
            # selected from travels with the LLM half, which is the desktop's
            # now. Restoring it means the desktop resolving the terms against
            # its own store and sending the text up with the request.

            try:
                with reporting_to(reporter), quieted_libraries("normal"):
                    vad_asr_stage.run_vad_asr(
                        vocal_audio,
                        output_path=temporary_aligned,
                        language=request.get("language", "ja"),
                        device="cuda",
                        gpu_tier=resolve_request_gpu_tier(
                            request, warn=lambda message: reporter.warning("gpu-tier", message)
                        ),
                        vad_silero_assist=finesub_settings.VAD_SILERO_ASSIST.resolve(
                            request.get("vad_silero_assist")
                        ),
                        vad_prefix_path=prepared_vad,
                        qwen_verify="off",
                        run_metadata_path=metadata_path,
                    )
                os.replace(temporary_aligned, aligned_output)
                scratch_volume.commit()
            finally:
                temporary_aligned.unlink(missing_ok=True)
                peak_gmem = gpu_memory.stop()
                if peak_gmem is not None:
                    reporter.debug(
                        "gpu memory usage",
                        {
                            "peak_gmem": format_bytes(peak_gmem),
                            "gmem_source": "nvml-process-peak",
                        },
                    )
            aligned_elapsed = time.perf_counter() - aligned_started
            reporter.debug(
                "stage timing",
                {"stage": "asr", "elapsed_sec": f"{aligned_elapsed:.3f}"},
            )
            _diarize_on_gpu(request, vocal_audio, work / "subtitle-diar.json", reporter)
            reporter.completed(aligned_output, aligned_elapsed)
            return

        from finesub.stages import default_pipeline_paths, run_pipeline
        from finesub.reporting import quieted_libraries, reporting_to
        from finesub import settings as finesub_settings
        from finesub.speech.recognition import vad_asr_stage
        from nonoka_x.gpu_tier import resolve_request_gpu_tier

        with reporting_to(reporter), quieted_libraries("normal"):
            paths = default_pipeline_paths(source, output)
            silero_assist = finesub_settings.VAD_SILERO_ASSIST.resolve(
                request.get("vad_silero_assist")
            )
            if aligned_prepare_phase:
                vocal_audio = paths.resolve_vocal_audio()
                if paths.aligned_json.is_file():
                    reporter.planned(["aligned-prepare", "aligned"])
                    reporter.stage_started("aligned-prepare", reused=True)
                    return
                # Upstream 0.5.0 owns the split this container exists for:
                # `run_vad_prefix` is the same VAD half `run_vad_asr` runs
                # in-process, and `read_vad_prefix` answers "is the stored
                # one still this audio" -- None when it is not.
                reusable = (
                    vad_asr_stage.read_vad_prefix(
                        paths.vad_json,
                        source_path=vocal_audio,
                        vad_silero_assist=silero_assist,
                    )
                    is not None
                )
                reporter.planned(["aligned-prepare", "aligned"])
                reporter.stage_started("aligned-prepare", reused=reusable)
                if not reusable:
                    # Silero VAD requires CUDA precision to match local segment boundaries exactly.
                    # When silero_assist is enabled, defer VAD execution to the GPU container (`run_aligned_stage`)
                    # where run_vad_asr executes run_vad_prefix on device="cuda".
                    # Only compute on CPU if silero_assist is False.
                    if not silero_assist:
                        vad_asr_stage.write_vad_prefix(
                            paths.vad_json,
                            vad_asr_stage.run_vad_prefix(
                                vocal_audio,
                                device="cpu",
                                vad_silero_assist=silero_assist,
                            ),
                            source_path=vocal_audio,
                            vad_silero_assist=silero_assist,
                        )
                        scratch_volume.commit()
                return

            if pipeline_target is None:
                raise RuntimeError(f"pipeline target is missing for phase {phase}")
            if postprocess_phase and paths.aligned_json.is_file():
                reporter.stage_started(
                    "aligned",
                    detail="CPU 正在执行 Qwen 校验与 Aligned 收尾",
                )
                vad_asr_stage.finalize_qwen_verification(
                    paths.resolve_vocal_audio(),
                    paths.aligned_json,
                    device="cpu",
                    referee=qwen_referee_instance,
                )
                scratch_volume.commit()
            paths = run_pipeline(
                source,
                output_path=output,
                stage=pipeline_target,
                language=request.get("language", "ja"),
                # The CPU postprocess leg is the only caller left here, and
                # the stages it still has to run -- Qwen finalize, stable
                # and the speaker labels -- are host work by design.
                device="cpu",
                gpu_tier=resolve_request_gpu_tier(
                    request, warn=lambda message: reporter.warning("gpu-tier", message)
                ),
                vad_silero_assist=silero_assist,
                # No LLM switches: `stable` stops before the correction
                # stage, so each of them would be a value nothing reads.
                # The desktop passes its own when it continues this run
                # from the stable JSON.
                **_diarize_kwargs(request),
                resume=True,
            )
        # Only the CPU postprocess leg reaches here: prepare, the GPU leg and
        # aligned-prepare each return above, and publishing is what this one is
        # for. The Worker stores the result, marks the job completed and drops
        # the source audio -- unless a cancel landed first, which it reports as
        # `active: false` and which the handler below treats as a cancel.
        artifacts = result_artifacts(request, Path(paths.stable_json), Path(paths.diar_json))
        if "stable_json" not in artifacts:
            raise FileNotFoundError("the pipeline finished without writing the stable JSON")
        channel.complete(artifacts)
        shutil.rmtree(work, ignore_errors=True)
        scratch_volume.commit()
    except BaseException as exc:
        if isinstance(exc, TaskCancelled):
            return
        # A preemption is an interruption, not an engine error, and the desktop
        # acts on the difference: a failed task offers /retry, an interrupted
        # one offers /resume. Publishing "failed" for a container that was
        # signalled out from under a working run mislabels it, so nothing is
        # published and the Worker's probe finds the dead call and writes
        # `interrupted` -- which is what actually happened.
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        # An error raised in a run that was cancelled meanwhile -- the cancel
        # also stops the call, which is often what raised -- is the cancel's.
        try:
            still_active = channel.active()
        except Exception:
            still_active = True
        if not still_active:
            return
        # A refused source is the client's, not the engine's: the code is what
        # tells the desktop the upload was the problem rather than the run, so
        # it offers no pointless retry of the same file.
        code = "source_rejected" if isinstance(exc, SourceRejected) else "engine_failed"
        message = str(exc) if isinstance(exc, SourceRejected) else f"{type(exc).__name__}: {exc}"
        # Under the reporter's lock, so no reporter call from another thread is
        # between its fence check and its write while the failure lands.
        with reporter._lock:
            channel.fail(code, message)
        reporter._print("failed", {"stage": reporter._stage, "code": code, "message": message})
        raise
    finally:
        try:
            scratch_volume.commit()
        except Exception as commit_exc:
            reporter._print("warning", {"stage": reporter._stage, "message": f"final checkpoint commit failed: {commit_exc}"})


def _diarize_kwargs(request: dict) -> dict:
    """`run_pipeline`'s speaker keys, from what the Worker stored.

    The Worker sends every enabled level as `acoustics`: the cloud computes the
    timeline and writes the labels, and the judge levels run on the desktop.
    """

    if not diarization_requested(request):
        return {}
    block = request["diarization"]
    kwargs = {"diarize": str(block["level"])}
    if block.get("max_speakers") is not None:
        kwargs["diarize_max_speakers"] = int(block["max_speakers"])
    return kwargs


def _diarize_on_gpu(request: dict, vocal_audio: Path, diar_json: Path, reporter) -> None:
    """Compute the speaker timeline in the GPU leg, while the card is ours.

    The CPU postprocess leg runs the pipeline's own diarize stage, which finds
    this timeline and only writes the labels onto stable.json -- cheap and
    deterministic. Left to that leg, the model would run on the CPU at about a
    fifth of the audio's length instead of seconds on the card. A missing model
    or dependency costs the labels, never the subtitles, exactly as upstream's
    stage does; the CPU leg then tries again on its own.
    """

    kwargs = _diarize_kwargs(request)
    if not kwargs or diar_json.is_file():
        return

    from finesub.reporting import quieted_libraries, reporting_to
    from finesub.speech.diarization import stage as diarization
    from finesub.speech.diarization.evidence import evidence_path

    started = time.perf_counter()
    reporter.stage_started("diarize")
    try:
        with reporting_to(reporter), quieted_libraries("normal"):
            result = diarization.run_diarization(
                vocal_audio,
                device="cuda",
                evidence_path=evidence_path(diar_json),
                max_speakers=kwargs.get("diarize_max_speakers"),
            )
            diarization.write_timeline(result, diar_json)
        scratch_volume.commit()
    except diarization.DiarizationUnavailable as error:
        reporter.warning("diarize-skipped", f"说话人分离在 GPU 阶段未能运行：{error}")
        return
    reporter.debug(
        "stage timing",
        {"stage": "diarize", "elapsed_sec": f"{time.perf_counter() - started:.3f}"},
    )
