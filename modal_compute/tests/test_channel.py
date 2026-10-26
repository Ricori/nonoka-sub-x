"""`JobChannel` against a fake Worker: the contract in `src/internal/routes.ts`."""

from __future__ import annotations

import socket

import pytest

from nonoka_x_compute.channel import (
    ChannelError,
    ChannelUnauthorized,
    ChannelUnavailable,
    JobChannel,
    TaskCancelled,
)


JOB = "job_" + "a" * 24


def make(worker, clock, **kwargs) -> JobChannel:
    return JobChannel(JOB, "run-1", base_url=worker.url, token="secret", sleep=clock.sleep, clock=clock, **kwargs)


def test_every_call_carries_the_token_and_the_run(worker, clock) -> None:
    channel = make(worker, clock)
    channel.stage("vocal", {"stage": "vocal", "message": "正在处理"})
    channel.run()

    post, get = worker.requests
    assert post["path"] == f"/internal/jobs/{JOB}/events"
    assert post["headers"]["Authorization"] == "Bearer secret"
    assert post["json"] == {
        "run_id": "run-1",
        "events": [{"type": "stage", "payload": {"stage": "vocal", "message": "正在处理"}}],
        "stage": "vocal",
        "progress": None,
    }
    assert get["method"] == "GET" and get["query"] == {"run_id": "run-1"}


def test_5xx_is_retried_with_backoff(worker, clock) -> None:
    worker.script("events", 503, {"detail": "busy"})
    worker.script("events", 502, {"detail": "busy"})
    worker.script("events", 200, {"active": True})

    make(worker, clock).warning({"code": "x", "message": "m"})

    assert len(worker.calls("events")) == 3
    assert clock.sleeps == [0.5, 1.0]


def test_429_is_retried_too(worker, clock) -> None:
    worker.script("child", 429, {"detail": "slow down"})
    make(worker, clock).child("fc-abc")
    assert len(worker.calls("child")) == 2


def test_persistent_5xx_gives_up(worker, clock) -> None:
    for _ in range(3):
        worker.script("events", 500, {"detail": "boom"})
    with pytest.raises(ChannelUnavailable):
        make(worker, clock, attempts=3).warning({"code": "x", "message": "m"})
    assert len(worker.calls("events")) == 3
    assert clock.sleeps == [0.5, 1.0]


def test_network_errors_are_retried(clock) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # Nothing listens on `port` any more: every attempt is refused.
    channel = JobChannel(JOB, "run-1", base_url=f"http://127.0.0.1:{port}", token="t", sleep=clock.sleep, clock=clock, attempts=4)
    with pytest.raises(ChannelUnavailable):
        channel.run()
    assert clock.sleeps == [0.5, 1.0, 2.0]


def test_401_raises_at_once(worker, clock) -> None:
    worker.script("events", 401, {"detail": "unauthorized"})
    with pytest.raises(ChannelUnauthorized):
        make(worker, clock).warning({"code": "x", "message": "m"})
    assert len(worker.calls("events")) == 1
    assert clock.sleeps == []


def test_other_4xx_is_an_error_not_a_retry(worker, clock) -> None:
    worker.script("events", 422, {"detail": "事件类型无效"})
    with pytest.raises(ChannelError, match="事件类型无效"):
        make(worker, clock).warning({"code": "x", "message": "m"})
    assert len(worker.calls("events")) == 1


@pytest.mark.parametrize("endpoint", ["events", "child", "complete"])
def test_inactive_run_raises_task_cancelled(worker, clock, endpoint) -> None:
    worker.script(endpoint, 200, {"active": False, "state": "cancelled"})
    channel = make(worker, clock)
    with pytest.raises(TaskCancelled):
        if endpoint == "events":
            channel.stage("aligned", {"stage": "aligned"})
        elif endpoint == "child":
            channel.child("fc-1")
        else:
            channel.complete({"stable_json": "{}"})


def test_a_job_that_is_gone_is_inactive(worker, clock) -> None:
    worker.script("run", 404, {"detail": "任务不存在"})
    worker.script("events", 404, {"detail": "任务不存在"})
    channel = make(worker, clock)
    assert channel.active() is False
    with pytest.raises(TaskCancelled):
        channel.warning({"code": "x", "message": "m"})


def test_wait_dispatched_polls_until_the_call_id_is_recorded(worker, clock) -> None:
    worker.script("run", 200, {"active": True, "call_id": None})
    worker.script("run", 200, {"active": True, "call_id": None})
    worker.script("run", 200, {"active": True, "call_id": "fc-1", "owner_id": "k", "request": {}})

    state = make(worker, clock).wait_dispatched()

    assert state["call_id"] == "fc-1"
    assert clock.sleeps == [0.1, 0.1]
    assert worker.calls("fail") == []


def test_wait_dispatched_fails_the_run_after_fifteen_seconds(worker, clock) -> None:
    for _ in range(200):
        worker.script("run", 200, {"active": True, "call_id": None})
    worker.script("fail", 200, {"active": False, "state": "failed"})

    assert make(worker, clock).wait_dispatched() is None

    assert sum(clock.sleeps) == pytest.approx(15.0, abs=0.11)
    (fail,) = worker.calls("fail")
    assert fail["json"]["code"] == "dispatch_incomplete"
    assert fail["json"]["run_id"] == "run-1"


def test_wait_dispatched_returns_nothing_for_an_inactive_run(worker, clock) -> None:
    worker.script("run", 200, {"active": False, "call_id": None})
    assert make(worker, clock).wait_dispatched() is None
    assert worker.calls("fail") == []


def _progress(completed, total=10, message=""):
    return {"stage": "vocal", "completed": completed, "total": total, "unit": "blocks", "message": message}


def test_progress_skips_repeats_and_holds_to_one_per_second(worker, clock) -> None:
    channel = make(worker, clock)
    assert channel.progress("vocal", _progress(0)) is True
    # Same value: never published.
    clock.now += 5
    assert channel.progress("vocal", _progress(0)) is False
    assert channel.progress("vocal", _progress(1)) is True
    # Within a second of the last one.
    clock.now += 0.4
    assert channel.progress("vocal", _progress(2)) is False
    clock.now += 0.7
    assert channel.progress("vocal", _progress(3)) is True
    # The final value is never held back.
    clock.now += 0.1
    assert channel.progress("vocal", _progress(10)) is True

    sent = [call["json"] for call in worker.calls("events")]
    assert [body["progress"]["completed"] for body in sent] == [0, 1, 3, 10]
    assert sent[0]["stage"] == "vocal"
    assert sent[0]["events"] == [{"type": "progress", "payload": _progress(0)}]
    assert sent[0]["progress"] == {"completed": 0, "total": 10, "unit": "blocks", "message": ""}


def test_a_new_stage_lets_the_next_progress_through(worker, clock) -> None:
    channel = make(worker, clock)
    channel.progress("vocal", _progress(1))
    channel.stage("aligned", {"stage": "aligned"})
    assert channel.progress("vocal", _progress(1)) is True


def test_a_held_back_progress_still_notices_a_cancel(worker, clock) -> None:
    channel = make(worker, clock)
    channel.progress("vocal", _progress(1))
    clock.now += 6
    channel.progress("vocal", _progress(2))
    worker.script("run", 200, {"active": False, "call_id": "fc-1"})
    clock.now += 6
    with pytest.raises(TaskCancelled):
        channel.progress("vocal", _progress(2))


def test_ensure_active_asks_at_most_every_few_seconds(worker, clock) -> None:
    channel = make(worker, clock)
    channel.warning({"code": "x", "message": "m"})
    channel.ensure_active()
    assert worker.calls("run") == []
    clock.now += 6
    channel.ensure_active()
    assert len(worker.calls("run")) == 1


def test_fail_never_raises(worker, clock) -> None:
    worker.script("fail", 401, {"detail": "unauthorized"})
    assert make(worker, clock).fail("engine_failed", "boom") is False
    worker.script("fail", 404, {"detail": "任务不存在"})
    assert make(worker, clock).fail("engine_failed", "boom") is False
    assert make(worker, clock).fail("vocal_failed", "boom", stage="vocal") is True
    assert worker.calls("fail")[-1]["json"] == {"run_id": "run-1", "code": "vocal_failed", "message": "boom", "stage": "vocal"}


def test_complete_sends_the_artifacts(worker, clock) -> None:
    worker.script("complete", 200, {"active": True, "state": "completed"})
    result = make(worker, clock).complete({"stable_json": "{}", "diar_json": "[]"})
    assert result == {"active": True, "state": "completed"}
    assert worker.calls("complete")[0]["json"] == {"run_id": "run-1", "artifacts": {"stable_json": "{}", "diar_json": "[]"}}


def test_download_streams_through_a_part_file(worker, clock, tmp_path) -> None:
    audio = b"\x00\x01" * 300_000
    worker.script("source", 200, audio, {"Content-Type": "audio/mp4"})
    destination = tmp_path / "source.m4a"

    assert make(worker, clock).download_source(destination) == len(audio)

    assert destination.read_bytes() == audio
    assert not (tmp_path / "source.m4a.part").exists()
    assert worker.calls("source")[0]["query"] == {"run_id": "run-1"}


def test_download_retries_a_5xx(worker, clock, tmp_path) -> None:
    worker.script("source", 503, b"busy")
    worker.script("source", 200, b"audio")
    destination = tmp_path / "source.m4a"
    make(worker, clock).download_source(destination)
    assert destination.read_bytes() == b"audio"
    assert len(worker.calls("source")) == 2


def test_download_of_an_inactive_run_is_cancelled(worker, clock, tmp_path) -> None:
    worker.script("source", 409, {"detail": "任务已不在运行"})
    with pytest.raises(TaskCancelled):
        make(worker, clock).download_source(tmp_path / "source.m4a")


def test_download_of_missing_audio_is_not_retried(worker, clock, tmp_path) -> None:
    worker.script("source", 404, {"detail": "源音频不存在"})
    with pytest.raises(FileNotFoundError):
        make(worker, clock).download_source(tmp_path / "source.m4a")
    assert len(worker.calls("source")) == 1
    assert not (tmp_path / "source.m4a").exists()


def test_missing_configuration_is_refused(monkeypatch) -> None:
    monkeypatch.delenv("CF_CALLBACK_URL", raising=False)
    monkeypatch.setenv("WORKER_CALLBACK_TOKEN", "t")
    with pytest.raises(ChannelError):
        JobChannel(JOB, "run-1")
    monkeypatch.setenv("CF_CALLBACK_URL", "https://cloud.example/")
    channel = JobChannel(JOB, "run-1")
    assert channel._url("run") == f"https://cloud.example/internal/jobs/{JOB}/run"
