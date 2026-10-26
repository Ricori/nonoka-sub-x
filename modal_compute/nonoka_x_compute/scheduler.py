"""CPU scheduler container for a cloud FineSub task.

Five stages, in this order:

1. `prepare` (here): download the source through the Worker, check its real
   length, decode it for the separator;
2. `VocalSeparator.run` (L4), skipped when `subtitle-vocal.ogg` is committed;
3. `aligned-prepare` (here): VAD;
4. `run_aligned_stage` (L4), skipped when `subtitle-aligned.json` is committed,
   with the CPU Qwen referee preloading here for its whole length;
5. `cpu` (here): Qwen finalize, stable, speaker labels, and the hand-back.
"""

from __future__ import annotations

import json
import threading
import time

from .channel import JobChannel, TaskCancelled
from .gpu import run_aligned_stage
from .images import scheduler_image
from .phases import run_pipeline_phase
from .resources import (
    app,
    control_secret,
    huggingface_secret,
    models_volume,
    scratch_volume,
    task_dir,
)
from .vocal import VocalSeparator


#: How long the scheduler waits for the CPU Qwen preload after the GPU stage
#: has finished. The preload has already run for the whole of that stage, so
#: this only bounds a load that is stuck rather than slow.
QWEN_PRELOAD_JOIN_SEC = 120.0


def _log(job_id: str, message: str, **fields: object) -> None:
    print(
        json.dumps(
            {
                "scope": "finesub",
                "task_id": job_id,
                "type": "log",
                "stage": "aligned",
                "message": message,
                **fields,
            },
            ensure_ascii=False,
            default=str,
        ),
        flush=True,
    )


def _report_qwen_preload(job_id: str, state: dict, thread) -> None:
    """Say what became of the preload instead of dropping it.

    A failed or abandoned preload is not a task failure -- the postprocess leg
    loads the referee itself -- but it was being swallowed entirely, so a
    container that never once managed to warm Qwen looked identical to one that
    always did.
    """

    error = state.get("error")
    if error is not None:
        _log(
            job_id,
            "qwen preload failed",
            fields={"error": f"{type(error).__name__}: {error}"},
        )
    elif thread is not None and thread.is_alive():
        _log(
            job_id,
            "qwen preload abandoned",
            fields={"waited_sec": f"{QWEN_PRELOAD_JOIN_SEC:.0f}"},
        )


def _close_qwen(state: dict) -> None:
    referee = state.pop("referee", None)
    if referee is not None:
        try:
            referee.close()
        except Exception:
            # Releasing weights on a container that is about to go away must
            # never be what fails a finished task.
            pass


def _start_qwen_preload(job_id: str) -> tuple[dict, threading.Thread]:
    state: dict[str, object] = {}

    def preload_qwen() -> None:
        try:
            from finesub.speech.verification.qwen_referee import QwenReferee

            referee = QwenReferee(device="cpu")
            referee.warm()
            state["referee"] = referee
        except BaseException as exc:
            state["error"] = exc

    thread = threading.Thread(
        target=preload_qwen,
        name=f"qwen-preload-{job_id}",
        daemon=True,
    )
    thread.start()
    return state, thread


def _orchestrate(owner_id: str, job_id: str, request: dict, run_id: str) -> None:
    """Schedule isolated GPU stages around CPU preparation and postprocess."""

    channel = JobChannel(job_id, run_id)

    def active() -> bool:
        # Any terminal state ends the pipeline, not just cancellation: the
        # Worker answers `active` only for this run while it is queued or
        # running and not cancelled, so a child stage that published a failure
        # stops the scheduler before it rents the next container.
        return channel.active()

    def run_child_stage(
        worker,
        *,
        stage: str,
        message: str,
        extra_args: tuple = (),
    ) -> bool:
        if not active():
            return False
        # Announced before the spawn, deliberately: the child starts publishing
        # its own stage events the moment its container is up, and a handoff
        # written afterwards would land after them in the log.
        try:
            channel.handoff(stage, {"stage": stage, "message": message, "run_id": run_id})
        except TaskCancelled:
            return False
        child_started = time.perf_counter()
        call = worker.spawn(owner_id, job_id, request, run_id, *extra_args)
        dispatch_sec = time.perf_counter() - child_started
        # A cancel needs the child's call id to reach it, so this write has to
        # happen; a run that stopped in the meantime takes the child with it.
        try:
            channel.child(call.object_id)
        except TaskCancelled:
            call.cancel()
            return False
        remote_started = time.perf_counter()
        try:
            call.get()
        finally:
            remote_sec = time.perf_counter() - remote_started
            _log(
                job_id,
                "child stage timing",
                fields={
                    "stage": stage,
                    "dispatch_sec": f"{dispatch_sec:.3f}",
                    "remote_sec": f"{remote_sec:.3f}",
                    "total_sec": f"{time.perf_counter() - child_started:.3f}",
                },
            )
            try:
                channel.child(None)
            except TaskCancelled:
                pass
        return active()

    try:
        run_pipeline_phase(owner_id, job_id, request, run_id, phase="prepare")
        if not active():
            return
        # Cloud intentionally has no client-facing quality-tier switch: every remote
        # task uses the same short-lived Vocal worker, and the rate it separates at
        # is the deployment's `vocal_policy` tier pinned into the request.
        #
        # Skipped outright when the Volume already holds a committed delivery, the
        # same way the aligned stage below is skipped: separation is the most
        # expensive thing a task buys, and a resumed run must not buy it twice.
        # The worker refuses to redo it anyway, but checking here is what keeps a
        # GPU from being rented and a checkpoint loaded to learn that.
        vocal_output = task_dir(job_id) / "subtitle-vocal.ogg"
        if not vocal_output.is_file():
            if not run_child_stage(
                VocalSeparator().run,
                stage="gpu-vocal-queued",
                message="源音频已就绪，正在启动 Vocal GPU 阶段",
            ):
                return
        elif not active():
            return
        run_pipeline_phase(
            owner_id,
            job_id,
            request,
            run_id,
            phase="aligned-prepare",
        )
        if not active():
            return
        aligned_output = task_dir(job_id) / "subtitle-aligned.json"
        qwen_state: dict[str, object] = {}
        qwen_thread = None
        if not aligned_output.is_file():
            qwen_state, qwen_thread = _start_qwen_preload(job_id)
            try:
                aligned_ok = run_child_stage(
                    run_aligned_stage,
                    stage="gpu-aligned-queued",
                    message="VAD 已就绪，正在启动短时 Whisper GPU 阶段",
                )
            finally:
                # Bounded, because this is an optimization and an unbounded join is
                # not. The preload runs for the whole Whisper stage, so by the time
                # it is waited on it has had minutes; anything still going is stuck
                # on a download that `from_pretrained` gives no way to interrupt,
                # and the alternative to giving up was hanging the scheduler until
                # its three-hour timeout. Giving up costs the postprocess leg one
                # cold Qwen load, which is what it paid before the preload existed.
                qwen_thread.join(timeout=QWEN_PRELOAD_JOIN_SEC)
            _report_qwen_preload(job_id, qwen_state, qwen_thread)
            if not aligned_ok:
                _close_qwen(qwen_state)
                return
        referee = qwen_state.get("referee")
        try:
            run_pipeline_phase(
                owner_id,
                job_id,
                request,
                run_id,
                phase="cpu",
                qwen_referee_instance=referee,
            )
        finally:
            # Read back rather than closing `referee`: a preload the join gave up
            # on can still land afterwards, and that instance would otherwise hold
            # its weights until the container died.
            _close_qwen(qwen_state)
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(exc, TaskCancelled):
            return
        # The Worker ignores this for a run that is no longer live, and never
        # overwrites a failure a leg already published with its own code.
        message = str(exc) or f"{type(exc).__name__}"
        channel.fail("pipeline_failed", f"云端处理异常：{message}")
        raise


@app.function(
    image=scheduler_image,
    cpu=2.0,
    memory=3584,
    scaledown_window=20,
    max_containers=10,
    timeout=60 * 60 * 3,
    volumes={"/data": scratch_volume, "/models": models_volume},
    secrets=[huggingface_secret, control_secret],
    # No automatic retries, here or on the GPU workers. Recovery on this
    # pipeline is the user's call and always was: /retry for a failed or
    # cancelled task, /resume for an interrupted one, both resuming off the
    # committed breakpoints on the scratch Volume rather than restarting. An
    # automatic retry cannot tell those apart, so it re-ran deterministic engine
    # errors -- renting a GPU to fail the same way, at both levels, up to nine
    # times for one bad task. A preemption still reaches the user as
    # `interrupted` (the Worker's probe finds the dead call) with resume offered.
)
def orchestrate_task(
    owner_id: str, job_id: str, request: dict, run_id: str
) -> None:
    """One run of one job, spawned by `control`'s `/spawn`."""

    _orchestrate(owner_id, job_id, request, run_id)
