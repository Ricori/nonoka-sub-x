"""The scheduler's stage order, skip guards and failure handling, with Modal faked."""

from __future__ import annotations

import pytest

from nonoka_x_compute import scheduler
from nonoka_x_compute.channel import TaskCancelled


JOB = "job_" + "d" * 24


class FakeChannel:
    """Records what the scheduler tells the Worker; `active` is scripted."""

    def __init__(self, log: list, *, active=None, cancel_on_child=False) -> None:
        self.log = log
        self._active = list(active or [])
        self.cancel_on_child = cancel_on_child
        self.children: list = []
        self.failures: list = []
        self.handoffs: list = []

    def active(self) -> bool:
        self.log.append("active?")
        return self._active.pop(0) if self._active else True

    def handoff(self, stage, payload) -> None:
        self.log.append(f"handoff:{stage}")
        self.handoffs.append((stage, payload))

    def child(self, call_id) -> None:
        self.log.append(f"child:{call_id}")
        self.children.append(call_id)
        if call_id is not None and self.cancel_on_child:
            raise TaskCancelled("cancelled")

    def fail(self, code, message, *, stage=None) -> bool:
        self.log.append(f"fail:{code}")
        self.failures.append((code, message))
        return True


class FakeCall:
    def __init__(self, name, log, error=None, on_get=None) -> None:
        self.object_id = f"fc-{name}"
        self.name = name
        self.log = log
        self.error = error
        self.on_get = on_get
        self.cancelled = False

    def get(self):
        self.log.append(f"{self.name}:get")
        if self.on_get:
            self.on_get()
        if self.error:
            raise self.error

    def cancel(self):
        self.log.append(f"{self.name}:cancel")
        self.cancelled = True


class FakeWorker:
    def __init__(self, name, log, **call_kwargs) -> None:
        self.name = name
        self.log = log
        self.call_kwargs = call_kwargs
        self.calls: list[FakeCall] = []
        self.spawned: list[tuple] = []

    def spawn(self, *args):
        self.log.append(f"{self.name}:spawn")
        self.spawned.append(args)
        call = FakeCall(self.name, self.log, **self.call_kwargs)
        self.calls.append(call)
        return call


class FakeThread:
    def __init__(self, log) -> None:
        self.log = log

    def join(self, timeout=None):
        self.log.append(f"qwen:join:{timeout:.0f}")

    def is_alive(self):
        return False


@pytest.fixture
def harness(tmp_path, monkeypatch):
    log: list[str] = []
    h = type("Harness", (), {})()
    h.log = log
    h.work = tmp_path / "tasks" / JOB
    h.work.mkdir(parents=True)
    h.channel = FakeChannel(log)
    h.phase_error = {}
    h.vocal = FakeWorker("vocal", log)
    h.aligned = FakeWorker("aligned", log)
    h.referee = type("Referee", (), {"closed": False, "close": lambda self: setattr(self, "closed", True)})()

    def run_phase(owner_id, job_id, request, run_id, *, phase, qwen_referee_instance=None):
        log.append(f"phase:{phase}")
        if phase == "cpu":
            h.cpu_referee = qwen_referee_instance
        if phase in h.phase_error:
            raise h.phase_error[phase]

    def start_preload(job_id):
        log.append("qwen:start")
        return {"referee": h.referee}, FakeThread(log)

    class Vocal:
        def __init__(self):
            self.run = h.vocal

    monkeypatch.setattr(scheduler, "JobChannel", lambda job_id, run_id: h.channel)
    monkeypatch.setattr(scheduler, "run_pipeline_phase", run_phase)
    monkeypatch.setattr(scheduler, "VocalSeparator", Vocal)
    monkeypatch.setattr(scheduler, "run_aligned_stage", h.aligned)
    monkeypatch.setattr(scheduler, "task_dir", lambda job_id: tmp_path / "tasks" / job_id)
    monkeypatch.setattr(scheduler, "_start_qwen_preload", start_preload)

    def run():
        scheduler._orchestrate("key-1", JOB, {"language": "ja"}, "run-1")

    h.run = run
    return h


def _milestones(log):
    return [entry for entry in log if entry != "active?"]


def test_a_fresh_run_goes_through_all_five_stages_in_order(harness) -> None:
    harness.run()

    assert _milestones(harness.log) == [
        "phase:prepare",
        "handoff:gpu-vocal-queued",
        "vocal:spawn",
        "child:fc-vocal",
        "vocal:get",
        "child:None",
        "phase:aligned-prepare",
        "qwen:start",
        "handoff:gpu-aligned-queued",
        "aligned:spawn",
        "child:fc-aligned",
        "aligned:get",
        "child:None",
        "qwen:join:120",
        "phase:cpu",
    ]
    # Both GPU legs get the run's own arguments.
    assert harness.vocal.spawned == [("key-1", JOB, {"language": "ja"}, "run-1")]
    assert harness.aligned.spawned == [("key-1", JOB, {"language": "ja"}, "run-1")]
    # The preloaded referee reaches the postprocess leg and is released after it.
    assert harness.cpu_referee is harness.referee
    assert harness.referee.closed
    assert harness.channel.failures == []
    assert harness.channel.handoffs[0][1] == {
        "stage": "gpu-vocal-queued",
        "message": "源音频已就绪，正在启动 Vocal GPU 阶段",
        "run_id": "run-1",
    }


def test_a_committed_vocal_track_is_never_separated_again(harness) -> None:
    (harness.work / "subtitle-vocal.ogg").write_bytes(b"ogg")
    harness.run()
    assert harness.vocal.spawned == []
    assert "phase:aligned-prepare" in harness.log
    assert harness.aligned.spawned


def test_a_committed_aligned_json_skips_the_whisper_leg_and_the_preload(harness) -> None:
    (harness.work / "subtitle-vocal.ogg").write_bytes(b"ogg")
    (harness.work / "subtitle-aligned.json").write_text("{}")
    harness.run()
    assert _milestones(harness.log) == ["phase:prepare", "phase:aligned-prepare", "phase:cpu"]
    assert harness.cpu_referee is None


def test_a_run_that_stopped_after_prepare_rents_nothing(harness) -> None:
    harness.channel._active = [False]
    harness.run()
    assert _milestones(harness.log) == ["phase:prepare"]


def test_a_failed_child_stage_stops_the_scheduler(harness) -> None:
    # The Vocal worker published its own failure, so the run is no longer live.
    harness.channel._active = [True, True, False]
    harness.run()
    assert _milestones(harness.log)[-1] == "child:None"
    assert harness.aligned.spawned == []
    assert "phase:aligned-prepare" not in harness.log


def test_a_child_whose_run_stopped_meanwhile_is_cancelled(harness) -> None:
    harness.channel.cancel_on_child = True
    harness.run()
    assert harness.vocal.calls[0].cancelled
    assert "vocal:get" not in harness.log
    assert "phase:aligned-prepare" not in harness.log
    assert harness.channel.failures == []


def test_an_engine_error_is_published_as_pipeline_failed_and_raised(harness) -> None:
    harness.aligned.call_kwargs = {"error": RuntimeError("CUDA out of memory")}
    with pytest.raises(RuntimeError):
        harness.run()
    assert harness.channel.failures == [("pipeline_failed", "云端处理异常：CUDA out of memory")]
    # The child's call id is cleared even though the child raised, and the
    # preload is still joined and released.
    assert "child:None" in harness.log[harness.log.index("aligned:get"):]
    assert "qwen:join:120" in harness.log
    assert "phase:cpu" not in harness.log


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_an_interruption_never_publishes_a_failure(harness, interruption) -> None:
    harness.phase_error["aligned-prepare"] = interruption()
    with pytest.raises(interruption):
        harness.run()
    assert harness.channel.failures == []


def test_a_cancel_raised_inside_a_leg_ends_the_run_quietly(harness) -> None:
    harness.phase_error["prepare"] = TaskCancelled("cancelled")
    harness.run()
    assert harness.channel.failures == []
    assert harness.vocal.spawned == []
