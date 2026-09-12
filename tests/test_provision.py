from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from nonoka_x.provision import CANCELLED_MESSAGE, DONE_MESSAGES, OPTIONAL_TOOLS, TOOL_GROUPS, RuntimeProvisionError, RuntimeProvisioner, parse_model_install_event


VENDOR = Path(__file__).resolve().parents[1] / "third_party" / "finesub"


def test_runtime_provision_status_uses_finesub_manifests(tmp_path: Path) -> None:
    provisioner = RuntimeProvisioner(tmp_path, VENDOR)
    status = provisioner.status()
    assert status["schema"] == 1
    assert status["root"].startswith(str(tmp_path))
    resource_ids = {item["id"] for item in status["resources"]}
    assert "ffmpeg" in resource_ids
    if sys.platform == "darwin":
        assert status["media_supported"] is True
        assert "ffprobe" in resource_ids
    else:
        assert "uv" in resource_ids
    assert {item["id"] for item in status["models"]} == {"separator", "whisper", "qwen-referee"}


def test_runtime_provision_status_reports_bootstrap_failure(tmp_path: Path) -> None:
    """A broken bootstrap disables every install control in the desktop shell,
    so the payload has to carry the reason rather than only a tile tooltip."""
    provisioner = RuntimeProvisioner(tmp_path, VENDOR)
    assert provisioner.status()["bootstrap_error"] == ""
    provisioner._bootstrap_error = "RuntimeError: manifest missing"
    status = provisioner.status()
    assert status["supported"] is False
    assert status["bootstrap_error"] == "RuntimeError: manifest missing"
    assert "manifest missing" in status["runtime"]["detail"]


def test_runtime_provisioner_defaults_the_install_root_under_the_data_dir(tmp_path: Path) -> None:
    provisioner = RuntimeProvisioner(tmp_path, VENDOR)
    assert provisioner.install_root == (tmp_path / "finesub").resolve()


def test_runtime_provisioner_accepts_a_relocated_install_root(tmp_path: Path) -> None:
    """The desktop shell moves the multi-gigabyte install root off the system
    drive; settings and the knowledge base stay beside the data directory."""
    data = tmp_path / "data"
    install = tmp_path / "elsewhere" / "Nonoka X" / "finesub"
    provisioner = RuntimeProvisioner(data, VENDOR, install)
    assert provisioner.install_root == install.resolve()
    assert provisioner.status()["root"] == str(install.resolve())
    assert provisioner.paths is not None
    assert provisioner.paths.models == install.resolve() / "models"
    assert provisioner.paths.data_root == (data / "finesub-data").resolve()


def test_remove_managed_tree_clears_read_only_files(tmp_path: Path) -> None:
    """uv and pip leave read-only files behind; Windows refuses to unlink them."""
    from nonoka_x.provision import _remove_managed_tree

    target = tmp_path / "runtime"
    (target / "nested").mkdir(parents=True)
    locked = target / "nested" / "python.exe"
    locked.write_bytes(b"binary")
    locked.chmod(stat.S_IREAD)
    _remove_managed_tree(target)
    assert not target.exists()


def test_remove_all_reports_failures_instead_of_raising_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A partial removal has to reach the desktop shell as a stated reason, and
    leave the job in a state the panel can render after it re-reads status."""
    provisioner = RuntimeProvisioner(tmp_path, VENDOR)

    def refuse(target: Path) -> None:
        raise PermissionError(13, "被占用", str(target))

    monkeypatch.setattr("nonoka_x.provision._remove_managed_tree", refuse)
    with pytest.raises(RuntimeProvisionError, match="未能完全删除"):
        provisioner.remove_all()
    job = provisioner.status()["job"]
    assert job["state"] == "failed"
    assert job["error"]["code"] == "remove_failed"
    assert "未能完全删除" in job["message"]


def test_remove_tool_group_preserves_other_managed_assets(tmp_path: Path) -> None:
    provisioner = RuntimeProvisioner(tmp_path, VENDOR)
    assert provisioner.resources is not None

    video_roots = []
    for resource_id in TOOL_GROUPS["video-tools"]:
        if resource_id not in provisioner.resources.resources:
            continue
        root = provisioner.resources.install_path(resource_id).parent
        root.mkdir(parents=True, exist_ok=True)
        (root / "fixture").write_text("video", encoding="utf-8")
        video_roots.append(root)

    preserved = provisioner.resources.install_path("tokcount").parent
    preserved.mkdir(parents=True, exist_ok=True)
    (preserved / "fixture").write_text("optional", encoding="utf-8")

    status = provisioner.remove_tool_group("video-tools")
    assert video_roots and all(not root.exists() for root in video_roots)
    assert preserved.is_dir()
    assert status["job"]["target"] == "remove-video-tools"
    assert "其他运行时与模型保持不变" in status["job"]["message"]


def test_remove_tool_group_rejects_required_assets(tmp_path: Path) -> None:
    provisioner = RuntimeProvisioner(tmp_path, VENDOR)
    with pytest.raises(RuntimeProvisionError, match="仅支持卸载"):
        provisioner.remove_tool_group("runtime")


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows platform gate")
def test_runtime_install_refuses_unsupported_platform(tmp_path: Path) -> None:
    provisioner = RuntimeProvisioner(tmp_path, VENDOR)
    assert provisioner.status()["supported"] is False
    with pytest.raises(RuntimeProvisionError, match="Windows x64"):
        provisioner.start("all")


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS managed media resources")
def test_macos_manifests_pin_both_media_tools() -> None:
    resource_root = Path(__file__).resolve().parents[1] / "src" / "nonoka_x" / "resources"
    for architecture in ("arm64", "amd64"):
        manifest = json.loads(
            (resource_root / f"runtime-manifest.macos-{architecture}.json").read_text(encoding="utf-8")
        )
        resources = {item["id"]: item for item in manifest["resources"]}
        assert set(resources) == {"ffmpeg", "ffprobe"}
        for item in resources.values():
            assert item["asset"]["size"] > 0
            assert len(item["asset"]["sha256"]) == 64


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS managed media resources")
def test_macos_managed_tools_are_added_to_worker_path(tmp_path: Path) -> None:
    provisioner = RuntimeProvisioner(tmp_path, VENDOR)
    assert provisioner.resources is not None
    for name in ("ffmpeg", "ffprobe"):
        destination = provisioner.resources.install_path(name) / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"fixture")
        destination.chmod(0o755)
        (destination.parent.parent / "current.json").write_text('{"current":"9.0.1"}', encoding="utf-8")
        assert provisioner.tool_path(name) == destination
    path_entries = provisioner.worker_environment()["PATH"].split(os.pathsep)
    assert str(provisioner.tool_path("ffmpeg").parent) in path_entries
    assert str(provisioner.tool_path("ffprobe").parent) in path_entries


def test_runtime_progress_can_aggregate_multiple_resource_downloads(tmp_path: Path) -> None:
    provisioner = RuntimeProvisioner(tmp_path, VENDOR)

    class Progress:
        total = 40
        downloaded = 15
        bytes_per_second = 1024.0

    provisioner._progress(Progress(), completed_before=40, total_override=100)
    progress = provisioner.status()["job"]["progress"]
    assert progress == {
        "completed": 55,
        "total": 100,
        "unit": "bytes",
        "bytes_per_second": 1024.0,
    }


def test_cancel_requires_a_running_job(tmp_path: Path) -> None:
    provisioner = RuntimeProvisioner(tmp_path, VENDOR)
    with pytest.raises(RuntimeProvisionError, match="没有正在进行"):
        provisioner.cancel()


def test_cancelled_job_stops_before_downloading_and_is_not_a_failure(tmp_path: Path) -> None:
    provisioner = RuntimeProvisioner(tmp_path, VENDOR)
    provisioner._job.update(state="running", target="all", stage="preparing")
    provisioner.cancel()
    assert provisioner.status()["job"]["message"].startswith("正在取消")

    provisioner._run("all")
    job = provisioner.status()["job"]
    assert job["state"] == "cancelled"
    assert job["message"] == CANCELLED_MESSAGE
    assert job["error"] is None


def test_completion_copy_tells_runtime_only_apart_from_full_prepare() -> None:
    assert DONE_MESSAGES["runtime"] != DONE_MESSAGES["all"]
    assert "模型仍需单独下载" in DONE_MESSAGES["runtime"]


def test_model_installer_events_are_validated_before_updating_ui() -> None:
    event = parse_model_install_event(
        '{"type":"progress","resource":"whisper","completed":25,"total":100,"bytes_per_second":12.5,"message":"下载中"}',
        "whisper",
    )
    assert event == {
        "type": "progress",
        "completed": 25,
        "total": 100,
        "bytes_per_second": 12.5,
        "message": "下载中",
    }
    assert parse_model_install_event("ordinary log output", "whisper") is None
    assert parse_model_install_event('{"type":"progress","resource":"other"}', "whisper") is None


def test_optional_tools_are_explicit_install_targets() -> None:
    assert OPTIONAL_TOOLS == ("yt-dlp", "tokcount", "aria2c", "node", "pot-provider")
    assert TOOL_GROUPS["video-tools"] == ("yt-dlp", "aria2c", "node", "pot-provider")
    assert TOOL_GROUPS["optional-tools"] == ("tokcount",)
    assert all(tool in OPTIONAL_TOOLS for tool in TOOL_GROUPS["video-tools"])
    assert all(tool in DONE_MESSAGES for tool in OPTIONAL_TOOLS)


def test_remove_all_preserves_tasks_and_user_data(tmp_path: Path) -> None:
    provisioner = RuntimeProvisioner(tmp_path, VENDOR)
    assert provisioner.paths is not None
    replaceable = (
        provisioner.paths.runtime,
        provisioner.paths.models,
        provisioner.paths.cache,
        provisioner.paths.agent_capsules,
    )
    for directory in replaceable:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "fixture").write_text("replaceable", encoding="utf-8")
    task = provisioner.paths.tasks / "task.json"
    task.parent.mkdir(parents=True, exist_ok=True)
    task.write_text("important", encoding="utf-8")
    user_data = provisioner.paths.user_data / "settings.json"
    user_data.parent.mkdir(parents=True, exist_ok=True)
    user_data.write_text("important", encoding="utf-8")

    status = provisioner.remove_all()

    assert all(not directory.exists() for directory in replaceable)
    assert task.read_text(encoding="utf-8") == "important"
    assert user_data.read_text(encoding="utf-8") == "important"
    assert status["job"]["target"] == "remove-all"
