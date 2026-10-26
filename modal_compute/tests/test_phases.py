"""What a phase tells the Worker, with the channel and the Volumes faked."""

from __future__ import annotations

import json

import pytest

from nonoka_x_compute import phases
from nonoka_x_compute.channel import TaskCancelled
from nonoka_x_compute.pipeline_rules import JOB_ARTIFACT_SUFFIXES, source_duration_fault


JOB = "job_" + "e" * 24


class FakeVolume:
    def __init__(self) -> None:
        self.commits = 0
        self.reloads = 0

    def commit(self) -> None:
        self.commits += 1

    def reload(self) -> None:
        self.reloads += 1


class FakeChannel:
    def __init__(self, *, dispatched=True, active=True) -> None:
        self.job_id = JOB
        self.run_id = "run-1"
        self.dispatched = dispatched
        self._active = active
        self.sent: list[tuple] = []
        self.failures: list[tuple] = []
        self.checks = 0

    def wait_dispatched(self):
        return {"active": True, "call_id": "fc-1"} if self.dispatched else None

    def active(self):
        return self._active

    def ensure_active(self):
        self.checks += 1
        if not self._active:
            raise TaskCancelled("cancelled")

    def stage(self, stage, payload):
        self.sent.append(("stage", stage, payload))

    def progress(self, stage, payload):
        self.sent.append(("progress", stage, payload))
        return True

    def warning(self, payload):
        self.sent.append(("warning", payload))

    def download_source(self, destination):
        destination.write_bytes(b"m4a")
        self.sent.append(("download", destination.name))
        return 3

    def fail(self, code, message, *, stage=None):
        self.failures.append((code, message))
        return True


@pytest.fixture
def leg(tmp_path, monkeypatch):
    channel = FakeChannel()
    scratch = FakeVolume()
    monkeypatch.setattr(phases, "JobChannel", lambda job_id, run_id: channel)
    monkeypatch.setattr(phases, "scratch_volume", scratch)
    monkeypatch.setattr(phases, "models_volume", FakeVolume())
    monkeypatch.setattr(phases, "task_dir", lambda job_id: tmp_path / "tasks" / job_id)
    return channel, scratch, tmp_path / "tasks" / JOB


def test_a_leg_publishes_nothing_until_the_call_id_is_recorded(leg) -> None:
    channel, _, work = leg
    channel.dispatched = False
    phases.run_pipeline_phase("key-1", JOB, {}, "run-1", phase="prepare")
    assert channel.sent == []
    assert not work.exists()


def test_a_source_longer_than_declared_is_rejected(leg, monkeypatch) -> None:
    channel, scratch, work = leg
    monkeypatch.setattr(phases, "probe_duration", lambda source: 3600.0)
    request = {"source": {"duration": 60}}
    with pytest.raises(phases.SourceRejected):
        phases.run_pipeline_phase("key-1", JOB, request, "run-1", phase="prepare")
    assert channel.sent[0] == ("stage", "starting", {"stage": "starting", "message": phases.PHASE_LABELS["prepare"], "run_id": "run-1"})
    assert ("download", "source.m4a") in channel.sent
    assert (work / "source.m4a").read_bytes() == b"m4a"
    assert channel.failures == [("source_rejected", source_duration_fault(60, 3600.0))]
    assert scratch.commits >= 1


def test_a_downloaded_source_is_reused(leg, monkeypatch) -> None:
    channel, _, work = leg
    work.mkdir(parents=True)
    (work / "source.m4a").write_bytes(b"old")
    monkeypatch.setattr(phases, "probe_duration", lambda source: 3600.0)
    with pytest.raises(phases.SourceRejected):
        phases.run_pipeline_phase("key-1", JOB, {"source": {"duration": 60}}, "run-1", phase="prepare")
    assert not any(entry[0] == "download" for entry in channel.sent)


def test_an_error_in_a_cancelled_run_publishes_nothing(leg, monkeypatch) -> None:
    channel, _, _ = leg

    def probe(source):
        channel._active = False
        raise RuntimeError("container is going away")

    monkeypatch.setattr(phases, "source_duration_fault", lambda declared, actual: "")
    monkeypatch.setattr(phases, "probe_duration", probe)
    phases.run_pipeline_phase("key-1", JOB, {}, "run-1", phase="prepare")
    assert channel.failures == []


def test_an_engine_error_is_engine_failed(leg) -> None:
    channel, _, work = leg
    # Only prepare may download; any later leg without the source is broken.
    with pytest.raises(FileNotFoundError):
        phases.run_pipeline_phase("key-1", JOB, {}, "run-1", phase="cpu")
    assert channel.failures[0][0] == "engine_failed"
    assert channel.failures[0][1].startswith("FileNotFoundError: ")


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_an_interruption_publishes_nothing(leg, monkeypatch, interruption) -> None:
    channel, _, _ = leg

    def probe(source):
        raise interruption()

    monkeypatch.setattr(phases, "probe_duration", probe)
    with pytest.raises(interruption):
        phases.run_pipeline_phase("key-1", JOB, {}, "run-1", phase="prepare")
    assert channel.failures == []


def test_unknown_phase_is_refused(leg) -> None:
    with pytest.raises(ValueError):
        phases.run_pipeline_phase("key-1", JOB, {}, "run-1", phase="llm")


# -- reporter ---------------------------------------------------------------


def _reporter(phase="gpu"):
    channel = FakeChannel()
    return phases.CloudReporter(channel, phase=phase, volume=FakeVolume()), channel


def test_logs_stay_on_stdout(capsys) -> None:
    reporter, channel = _reporter()
    reporter.debug("stage timing", {"stage": "asr", "elapsed_sec": "1.0"})
    reporter.planned(["aligned"])
    assert channel.sent == []
    assert channel.checks == 2
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["type"] for line in lines] == ["log", "log"]


def test_operator_only_warnings_stay_on_stdout() -> None:
    reporter, channel = _reporter()
    reporter.warning("srt-line-budget:3", "too long")
    reporter.warning("unverified-accelerator", "no row")
    reporter.warning("gpu-tier", "fell back")
    assert channel.sent == [("warning", {"code": "gpu-tier", "message": "fell back", "impact": "", "action": ""})]


def test_stages_and_progress_go_to_the_worker() -> None:
    reporter, channel = _reporter()
    reporter.stage_started("aligned")
    reporter.progress("aligned", completed=3, total=10, unit="segments")
    assert channel.sent == [
        ("stage", "aligned", {"stage": "aligned", "message": "正在处理", "reused": False}),
        ("progress", "aligned", {"stage": "aligned", "completed": 3, "total": 10, "unit": "segments", "message": ""}),
    ]


def test_postprocess_does_not_rebroadcast_finished_prerequisites() -> None:
    reporter, channel = _reporter(phase="cpu")
    reporter.stage_started("vocal", reused=True)
    reporter.stage_started("aligned", reused=True)
    reporter.stage_started("stable", reused=False)
    assert [entry[1] for entry in channel.sent] == ["stable"]


def test_a_failure_notice_is_not_published_twice() -> None:
    reporter, channel = _reporter()
    reporter.failed("aligned", "boom")
    assert channel.sent == []


# -- result -----------------------------------------------------------------


def test_the_result_is_the_stable_json(tmp_path) -> None:
    stable = tmp_path / "subtitle-stable.json"
    diar = tmp_path / "subtitle-diar.json"
    stable.write_text('{"segments": []}', encoding="utf-8")
    diar.write_text('{"turns": []}', encoding="utf-8")
    assert phases.result_artifacts({}, stable, diar) == {"stable_json": '{"segments": []}'}
    assert phases.result_artifacts({"diarization": {"level": "off"}}, stable, diar) == {"stable_json": '{"segments": []}'}


def test_the_speaker_timeline_rides_along_when_asked_for(tmp_path) -> None:
    stable = tmp_path / "subtitle-stable.json"
    diar = tmp_path / "subtitle-diar.json"
    stable.write_text("{}", encoding="utf-8")
    request = {"diarization": {"level": "acoustics", "max_speakers": 3}}
    assert phases.result_artifacts(request, stable, diar) == {"stable_json": "{}"}
    diar.write_text("[]", encoding="utf-8")
    assert phases.result_artifacts(request, stable, diar) == {"stable_json": "{}", "diar_json": "[]"}
    assert set(JOB_ARTIFACT_SUFFIXES) == {"stable_json", "diar_json"}
    assert phases._diarize_kwargs(request) == {"diarize": "acoustics", "diarize_max_speakers": 3}
    assert phases._diarize_kwargs({}) == {}


def test_source_duration_fault() -> None:
    assert source_duration_fault(600, 0) == ""
    assert source_duration_fault(600, 650) == ""
    assert source_duration_fault(600, 600 * 1.1 + 61) != ""
    assert "2 小时" in source_duration_fault(0, 7201)
    assert source_duration_fault("x", 100) == ""
