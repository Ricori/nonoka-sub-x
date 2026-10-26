"""The control endpoint, with Modal replaced by fakes."""

from __future__ import annotations

import modal
import pytest
from fastapi.testclient import TestClient

from nonoka_x_compute import control as control_module
from nonoka_x_compute.control import build_control_api, probe_state


JOB = "job_" + "b" * 24
AUTH = {"Authorization": "Bearer control-secret"}


class FakeCall:
    def __init__(self, call_id: str, outcome=None, cancel_error: Exception | None = None) -> None:
        self.object_id = call_id
        self.outcome = outcome
        self.cancel_error = cancel_error
        self.cancelled = False
        self.timeouts: list[float] = []

    def get(self, timeout=None):
        self.timeouts.append(timeout)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    def cancel(self):
        if self.cancel_error:
            raise self.cancel_error
        self.cancelled = True


@pytest.fixture
def fakes():
    state = {"spawned": [], "calls": {}, "purged": []}

    def spawn(owner_id, job_id, request, run_id):
        state["spawned"].append((owner_id, job_id, request, run_id))
        return "fc-spawned1"

    def function_call(call_id):
        if call_id not in state["calls"]:
            raise modal.exception.NotFoundError(f"no call {call_id}")
        return state["calls"][call_id]

    def purge(job_id):
        state["purged"].append(job_id)
        return True

    client = TestClient(build_control_api(token="control-secret", spawn=spawn, function_call=function_call, purge=purge))
    state["client"] = client
    return state


def test_root_is_open(fakes) -> None:
    assert fakes["client"].get("/").json() == {"ok": True}


@pytest.mark.parametrize("path", ["/spawn", "/cancel", "/probe", "/purge"])
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "control-secret"}, {"Authorization": "Bearer control-secret-but-longer"}])
def test_every_operation_wants_the_token(fakes, path, headers) -> None:
    response = fakes["client"].post(path, json={}, headers=headers)
    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}
    assert fakes["spawned"] == [] and fakes["purged"] == []


def test_no_configured_token_authorizes_nothing() -> None:
    client = TestClient(build_control_api(token="", spawn=lambda *a: "fc-x"))
    assert client.post("/spawn", json={}, headers={"Authorization": "Bearer "}).status_code == 401


def test_spawn_starts_the_scheduler(fakes) -> None:
    body = {"owner_id": "key-1", "job_id": JOB, "run_id": "run_abc", "request": {"language": "ja"}}
    response = fakes["client"].post("/spawn", json=body, headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"call_id": "fc-spawned1"}
    assert fakes["spawned"] == [("key-1", JOB, {"language": "ja"}, "run_abc")]


@pytest.mark.parametrize(
    "body",
    [
        {"owner_id": "key-1", "job_id": "../etc", "run_id": "r", "request": {}},
        {"owner_id": "key-1", "job_id": JOB, "run_id": "r", "request": "nope"},
        {"owner_id": "", "job_id": JOB, "run_id": "r", "request": {}},
        {"owner_id": "key-1", "job_id": JOB, "request": {}},
    ],
)
def test_spawn_validates_its_input(fakes, body) -> None:
    assert fakes["client"].post("/spawn", json=body, headers=AUTH).status_code == 422
    assert fakes["spawned"] == []


def test_cancel_reports_each_call(fakes) -> None:
    fakes["calls"]["fc-one"] = FakeCall("fc-one")
    fakes["calls"]["fc-two"] = FakeCall("fc-two", cancel_error=RuntimeError("already gone"))
    response = fakes["client"].post("/cancel", json={"call_ids": ["fc-one", "fc-two", "fc-missing", "bad id"]}, headers=AUTH)
    result = response.json()
    assert result["cancelled"] == ["fc-one"]
    assert fakes["calls"]["fc-one"].cancelled
    assert set(result["errors"]) == {"fc-two", "fc-missing", "bad id"}
    assert "already gone" in result["errors"]["fc-two"]


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (TimeoutError(), {"state": "running"}),
        (modal.exception.TimeoutError("still going"), {"state": "running"}),
        (None, {"state": "finished"}),
        (RuntimeError("worker died"), {"state": "failed", "error": "RuntimeError"}),
        (modal.exception.ConnectionError("no control plane"), {"state": "unknown", "error": "ConnectionError"}),
    ],
)
def test_probe_never_waits(fakes, outcome, expected) -> None:
    fakes["calls"]["fc-probed"] = call = FakeCall("fc-probed", outcome)
    response = fakes["client"].post("/probe", json={"call_id": "fc-probed"}, headers=AUTH)
    assert response.json() == expected
    assert call.timeouts == [0]


def test_probe_of_an_unknown_call_is_failed(fakes) -> None:
    response = fakes["client"].post("/probe", json={"call_id": "fc-nobody"}, headers=AUTH)
    assert response.json() == {"state": "failed", "error": "NotFoundError"}


def test_purge_takes_only_a_job_id(fakes) -> None:
    assert fakes["client"].post("/purge", json={"job_id": JOB}, headers=AUTH).json() == {"removed": True}
    assert fakes["client"].post("/purge", json={"job_id": "../../models"}, headers=AUTH).status_code == 422
    assert fakes["purged"] == [JOB]


def test_purge_removes_the_breakpoint_tree(tmp_path, monkeypatch) -> None:
    class Volume:
        commits = 0

        def reload(self):
            pass

        def commit(self):
            Volume.commits += 1

    monkeypatch.setattr(control_module, "scratch_volume", Volume())
    tree = tmp_path / "tasks" / JOB
    (tree / "nested").mkdir(parents=True)
    (tree / "nested" / "source.m4a").write_bytes(b"x")
    keep = tmp_path / "tasks" / ("job_" + "c" * 24)
    keep.mkdir()

    assert control_module._purge(JOB, data_root=tmp_path) is True
    assert not tree.exists() and keep.exists()
    assert Volume.commits == 1
    assert control_module._purge(JOB, data_root=tmp_path) is False
    assert Volume.commits == 1


def test_probe_state_is_usable_on_its_own() -> None:
    assert probe_state(FakeCall("fc-x", None)) == {"state": "finished"}
