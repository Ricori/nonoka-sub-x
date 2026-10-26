"""Short-lived Vocal GPU container."""

from __future__ import annotations

import json
import os
from pathlib import Path

import modal

from .channel import JobChannel, TaskCancelled
from .images import VOCAL_MODEL, vocal_accel_image
from .resources import (
    WORKER_GPU,
    app,
    control_secret,
    models_volume,
    scratch_volume,
    task_dir,
)
from .vocal_accel import ACCEL_ROOT, install as install_accel
from .vocal_policy import (
    DELIVERY_SAMPLE_RATE,
    PREPARED_SOURCE_NAME,
    tier_for_request,
)

#: Separator workers this container plans for.  One, and passed explicitly
#: rather than left implicit, because ``plan_separation_blocks`` makes the block
#: count a multiple of it: the same input planned for four workers gets
#: different edges and therefore a different vocal track.  Modal hands a task a
#: whole container and ``max_containers`` bounds the fleet rather than the task,
#: so there is never a second worker here to spread blocks across.
SEPARATOR_WORKERS = 1

#: Frames per write while merging blocks.  Same value FineSub merges with; it
#: only bounds the copy buffer, and nothing about the separation depends on it.
MERGE_CHUNK_FRAMES = 262144


@app.cls(
    image=vocal_accel_image,
    gpu=WORKER_GPU,
    cpu=2.0,
    memory=3072,
    timeout=60 * 30,
    scaledown_window=2,
    max_containers=2,
    volumes={"/data": scratch_volume, str(ACCEL_ROOT): models_volume},
    secrets=[control_secret],
    # Deliberately no retries -- see the scheduler's own decorator for why.
)
class VocalSeparator:
    """The only Vocal worker exposed by the cloud execution path.

    Loading the model in ``enter`` lets a container reuse it for a nearby task,
    while a two-second scale-down window releases an idle GPU promptly.  Which
    sample rate it separates at is per-task, pinned into the request by the Worker
    (see ``vocal_policy``); the checkpoint is rate-agnostic, so one loaded model
    serves every tier.
    """

    OUT_DIR = "/tmp/nonoka-x-vocals"

    @modal.enter()
    def load(self) -> None:
        import logging
        import time

        from audio_separator.separator import Separator

        checkpoint_started = time.perf_counter()
        Path(self.OUT_DIR).mkdir(parents=True, exist_ok=True)
        # No ``sample_rate`` keyword: this is upstream's own call, and every
        # tier's rate is applied per task by ``_pin_sample_rate`` below.
        #
        # ``output_dir``, ``output_format`` and ``output_single_stem`` describe
        # a file-to-file entry point this worker no longer calls: separation
        # goes through ``demix.separate_waveform``, which takes a waveform and
        # hands the stem straight back.  They stay at upstream's own values
        # because ``load_model`` reads them while building the model instance,
        # and the object this leaves behind is the one FineSub's local stage
        # hands to the same runner.
        self.separator = Separator(
            output_dir=self.OUT_DIR,
            model_file_dir="/models",
            output_format="WAV",
            output_single_stem="Vocals",
            use_autocast=True,
            log_level=logging.WARNING,
        )
        self.separator.load_model(model_filename=VOCAL_MODEL)
        self.checkpoint_load_sec = time.perf_counter() - checkpoint_started

        # Deliberately not installed here. The accelerator costs a fixed ~13 s
        # per container and saves time in proportion to the input's length, so
        # whether it is worth installing is a property of the task, not of the
        # container -- and in `enter` there is no task yet. `_install_accel`
        # decides on the first separation this container runs.
        self.accel: dict = {"backend": "eager", "reason": "not attempted"}

    def _install_accel(self, audio_sec: float) -> None:
        """Decide the accelerator once per container, against a real input.

        At most one attempt: a container that decided eager for a four-minute
        task must not re-decide for the next one, because the model it would
        install onto is the same one already separating, and a second attempt
        would only repeat a verdict that did not depend on the container.
        """

        if self.accel.get("reason") != "not attempted":
            return
        try:
            models_volume.reload()
        except Exception:
            # A stale snapshot only means a rebuild, never a failed task.
            pass
        self.accel = install_accel(
            self.separator.model_instance,
            model_name=VOCAL_MODEL,
            build_dir=Path(self.OUT_DIR),
            commit=models_volume.commit,
            audio_sec=audio_sec,
        )
        print(
            json.dumps(
                {"scope": "finesub", "type": "log", "stage": "vocal", **self.accel},
                ensure_ascii=False,
                default=str,
            ),
            flush=True,
        )

    def _pin_sample_rate(self, sample_rate: int) -> None:
        """Point the loaded model at this task's tier.

        audio-separator carries ``sample_rate`` as plain per-call state, and
        nothing about the BS-Roformer graph depends on it -- the chunk size
        comes from the checkpoint's STFT hop and ``dim_t``, so a lower rate
        simply means fewer windows over the same audio.  What reads it on this
        path is ``demix.separate_waveform``, which resamples the decoded block
        to it before the first window.  Reassigning it therefore costs nothing,
        while rebuilding the ``Separator`` per tier would reload 610 MB of
        weights on a billed T4 for no benefit.

        These two assignments are the same pair FineSub 0.4.2's
        ``separation._acquire_separator`` makes for ``--separator-rate``, and
        for the same stated reason.  We assign them here rather than call that
        function because that helper also acquires a shared-separator lease and
        a local block slot, neither of which exists in a container Modal has
        already given to one task.  ``test_the_worker_pins_the_rate_the
        _way_upstream_does`` fails if upstream starts applying it some other
        way.
        """

        self.separator.sample_rate = int(sample_rate)
        self.separator.model_instance.sample_rate = int(sample_rate)

    def _separate_blocks(
        self,
        source: Path,
        blocks: list,
        *,
        merged: Path,
        total_frames: int,
        pad_seconds: float,
        on_block,
    ) -> Path:
        """Separate the planned blocks and merge the cores into one track.

        This is FineSub's own blocked path, not a second implementation of it:
        each block is decoded with ``load_audio_slice``, separated by
        ``demix.separate_waveform`` and appended by ``_append_separated_block``,
        which are the three calls ``separation._separate_block`` and its merge
        loop make locally.  Nothing touches disk between the source and the
        merged track, and every block is read with ``pad`` seconds of real audio
        on both sides so the model never meets a manufactured edge.

        Driving the model directly is what closes the gap this worker used to
        have.  ``Separator.separate`` is a file-to-file wrapper: it reads the
        block with librosa, writes the stem through pydub, and both ends
        re-quantize.  The desktop has not gone through that wrapper since
        FineSub grew ``demix``, and a separated track that survived two more
        conversions than the desktop's is not the same audio -- measured on a
        127 s sample, the wrapper's track and the desktop's decode to different
        first lines even though they are within a fraction of a dB of each
        other in every band.

        The edges are still not upstream's, and cannot be.
        ``plan_separation_blocks`` sizes the block count to the worker count,
        and locally that is ``min(profile.vocal_separator_instances,
        separator_worker_limit(dur))`` -- two on an 8 GiB budget, four on 16
        GiB.  Here it is always one.  The counts coincide for some durations and
        not others (a 21.7-minute input gets three blocks here against local's
        four), so cloud and local produce *equivalent* vocal tracks for those
        durations, not identical ones.

        Sequential by construction: with one worker there is nothing to overlap,
        and holding one block at a time is what bounds this container's memory.
        """

        import soundfile as sf

        from finesub.speech.preprocessing.audio import load_audio_slice
        from finesub.speech.preprocessing.separator import demix
        from finesub.speech.preprocessing.separator.separation import (
            ASR_MERGE_FORMAT,
            _append_separated_block,
        )

        merged.unlink(missing_ok=True)
        out_file: sf.SoundFile | None = None
        try:
            for block in blocks:
                waveform, read_sr = load_audio_slice(
                    str(source),
                    block.read_start,
                    max(0, block.read_end - block.read_start),
                )
                if read_sr <= 0:
                    raise RuntimeError("prepared source audio is unreadable")
                stem, stem_rate = demix.separate_waveform(
                    self.separator.model_instance,
                    waveform,
                    read_sr,
                    # Not a tuning knob: the compiled packages are built under
                    # autocast, and upstream applies it in the wrapper this
                    # runner replaces.  Read off the separator, as FineSub does.
                    use_autocast=self.separator.use_autocast,
                )
                del waveform
                out_file = _append_separated_block(
                    out_file=out_file,
                    stem=stem,
                    stem_rate=stem_rate,
                    output_path=merged,
                    merge_format=ASR_MERGE_FORMAT,
                    block=block,
                    total_frames=total_frames,
                    pad_seconds=pad_seconds,
                    chunk_frames=MERGE_CHUNK_FRAMES,
                )
                del stem
                on_block(block.index + 1)
        finally:
            if out_file is not None:
                out_file.close()
        if not merged.is_file():
            raise RuntimeError("the separator produced no vocal track")
        return merged

    def _emit(
        self,
        channel: JobChannel,
        event_type: str,
        payload: dict,
    ) -> None:
        """Publish one event, or raise `TaskCancelled` once the run has stopped."""

        stage = str(payload.get("stage") or "vocal")
        # Performance diagnostics remain available in the Modal container log,
        # while the task event stream carries only lifecycle and progress.
        if event_type != "log":
            if event_type == "stage":
                channel.stage(stage, payload)
            elif event_type == "progress":
                channel.progress(stage, payload)
            else:
                raise ValueError(f"unsupported event type: {event_type}")
        else:
            channel.ensure_active()
        print(
            json.dumps(
                {
                    "scope": "finesub",
                    "task_id": channel.job_id,
                    "run_id": channel.run_id,
                    "type": event_type,
                    **payload,
                },
                ensure_ascii=False,
                default=str,
            ),
            flush=True,
        )

    @modal.method()
    def run(
        self,
        owner_id: str,
        job_id: str,
        request: dict,
        run_id: str,
    ) -> None:
        del owner_id
        import time

        import soundfile as sf

        started = time.perf_counter()
        channel = JobChannel(job_id, run_id)
        tier = tier_for_request(request)
        work = task_dir(job_id)
        source = work / PREPARED_SOURCE_NAME
        delivery = work / "subtitle-vocal.ogg"
        temporary = work / ".subtitle-vocal.part.ogg"
        merged: Path | None = None
        try:
            scratch_volume.reload()
            # The scheduler waited for the call id before spawning this stage,
            # so this only answers whether the run is still live.
            if channel.wait_dispatched() is None:
                return
            if delivery.is_file():
                # A committed delivery is this stage's checkpoint, and the one
                # worth honouring: separation is the single most expensive
                # thing a task buys, and every later stage keys off this file's
                # digest, so re-deriving it would also invalidate the prepared
                # VAD artifact and the aligned JSON that were built from it.
                #
                # Reached whenever the task is resumed past this point, by
                # /retry or by /resume after a preemption. The scheduler checks
                # the same file before renting this card; this is for anything
                # that reaches the stage anyway.
                self._emit(
                    channel,
                    "stage",
                    {
                        "message": f"复用已有分离结果（云端 · {tier.label}）",
                        "reused": True,
                    },
                )
                return
            if not source.is_file():
                raise FileNotFoundError("prepared source audio is missing")
            # Nonoka X's pinned copy of FineSub's planner, mounted at
            # /opt/nonoka_x. A copy rather than an import because the planner
            # sits in `separation.py` beside the CLI, the shared-separator pool
            # and the local resource ladder, none of which this worker drives.
            # Imported here rather than at module scope because `app` loads
            # this module in every container, and only the Vocal image mounts
            # /opt/nonoka_x.
            from nonoka_x.vocal_blocks import (
                DEFAULT_BLOCK_SECONDS,
                DEFAULT_PAD_SECONDS,
                plan_separation_blocks,
            )

            # Upstream's delivery encoder, called rather than reimplemented:
            # the file it writes is the one every later stage was tuned
            # against, down to the Vorbis compression level.
            from finesub.speech.preprocessing.separator.separation import (
                _encode_asr_delivery,
            )

            source_info = sf.info(str(source))
            total_frames = int(source_info.frames)
            if total_frames <= 0:
                raise RuntimeError("prepared source audio is empty")
            source_sr = int(source_info.samplerate)
            self._pin_sample_rate(tier.sample_rate)
            self._install_accel(float(source_info.duration))
            blocks = plan_separation_blocks(
                total_frames,
                source_sr,
                workers=SEPARATOR_WORKERS,
                max_core_seconds=DEFAULT_BLOCK_SECONDS,
                pad_samples=int(round(DEFAULT_PAD_SECONDS * source_sr)),
            )
            self._emit(
                channel,
                "stage",
                {
                    "stage": "vocal",
                    "message": f"正在处理（云端 · {tier.label}）",
                    "reused": False,
                },
            )
            self._emit(
                channel,
                "progress",
                {
                    "stage": "vocal",
                    "completed": 0,
                    "total": len(blocks),
                    "unit": "blocks",
                    "message": "",
                },
            )

            def block_finished(completed: int) -> None:
                # Also the cancellation check: `_emit` raises once the task is
                # cancelled or superseded, so a long blocked run now gives up at
                # the next block edge instead of at the end of the track.
                self._emit(
                    channel,
                    "progress",
                    {
                        "stage": "vocal",
                        "completed": completed,
                        "total": len(blocks),
                        "unit": "blocks",
                        "message": tier.label,
                    },
                )

            separation_started = time.perf_counter()
            merged = Path(self.OUT_DIR) / f"{job_id}-vocal-merged.rf64"
            self._separate_blocks(
                source,
                blocks,
                merged=merged,
                total_frames=total_frames,
                pad_seconds=DEFAULT_PAD_SECONDS,
                on_block=block_finished,
            )
            separation_sec = time.perf_counter() - separation_started
            delivery_started = time.perf_counter()
            info = sf.info(str(merged))
            if int(info.samplerate) != tier.sample_rate:
                raise RuntimeError(
                    f"vocal output has unexpected sample rate: {info.samplerate}"
                )
            # Separation runs in stereo above 16 kHz because that is where the
            # quality is; every consumer of this file reads 16 kHz mono, so the
            # downmix and the resample happen exactly once, here, and never
            # before the separator.
            #
            # FineSub's own encoder rather than an ffmpeg downmix, and Vorbis
            # rather than FLAC, because this file is the desktop's
            # `-vocal.ogg`: `_encode_asr_delivery` resamples through torchaudio
            # in windows that carry real audio past both edges, which is not
            # what ffmpeg's resampler produces, and it writes the same 16 kHz
            # mono Vorbis at the same compression level. Measured on a 127 s
            # sample, the two deliveries are within a fraction of a dB of each
            # other in every band and still transcribe differently; only this
            # one reproduces the desktop's transcript.
            temporary.unlink(missing_ok=True)
            _encode_asr_delivery(merged, temporary)
            if int(sf.info(str(temporary)).samplerate) != DELIVERY_SAMPLE_RATE:
                raise RuntimeError("vocal delivery has unexpected sample rate")
            os.replace(temporary, delivery)
            scratch_volume.commit()
            delivery_sec = time.perf_counter() - delivery_started
            try:
                import torch

                peak_gmem = torch.cuda.max_memory_allocated() / 1024**3
            except Exception:
                peak_gmem = 0.0
            self._emit(
                channel,
                "log",
                {
                    "stage": "vocal",
                    "message": "separator timing",
                    "fields": {
                        "accel": str(self.accel.get("backend", "eager")),
                        "tier": tier.name,
                        "sample_rate": str(tier.sample_rate),
                        "blocks": str(len(blocks)),
                        "channels": str(tier.channels),
                        "delivery_sample_rate": str(DELIVERY_SAMPLE_RATE),
                        "checkpoint_load_sec": f"{self.checkpoint_load_sec:.3f}",
                        "separation_sec": f"{separation_sec:.3f}",
                        "delivery_sec": f"{delivery_sec:.3f}",
                        "total_sec": f"{time.perf_counter() - started:.3f}",
                        "peak_gmem": f"{peak_gmem:.2f} GB",
                    },
                },
            )
        except BaseException as exc:
            if isinstance(exc, TaskCancelled):
                return
            # A preemption reaches here as an interruption, not as a separator
            # failure. Recording it as failed would offer the user /retry for
            # something /resume describes, so an interruption only raises and
            # the Worker's probe labels it -- the same split the FineSub phase
            # runner makes.
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            # Ignored by the Worker for a run that is no longer live.
            channel.fail("vocal_failed", f"{type(exc).__name__}: {exc}", stage="vocal")
            raise
        finally:
            temporary.unlink(missing_ok=True)
            if merged is not None:
                merged.unlink(missing_ok=True)

