"""The runtime page's provisioner over the prebuilt layers (`nonoka_x.provision`, WP-E3).

Fake layers (`tests/layer_fakes.py`) served by a local origin; `layers.json` written next to them and
handed to the provisioner, as WP-E2 will ship it in `resources/`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nonoka_x import provision  # noqa: E402
from nonoka_x import runtime_install  # noqa: E402
from nonoka_x.provision import RuntimeProvisionError, RuntimeProvisioner  # noqa: E402
from nonoka_x.runtime import layer_manifest as lm  # noqa: E402
from nonoka_x.runtime.fetch import NetworkRoute  # noqa: E402

from tests import layer_fakes as fakes  # noqa: E402

VENDOR = ROOT / "engines"
WINDOWS = pytest.mark.skipif(os.name != "nt", reason="layers are Windows-only")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    work = tmp_path_factory.mktemp("built")
    return {layer: fakes.build_layer(work, layer) for layer in lm.LAYERS}


@pytest.fixture
def origin(tmp_path_factory, built):
    server = fakes.Origin(fakes.publish(built, tmp_path_factory.mktemp("origin")))
    yield server
    server.close()


@pytest.fixture
def index(tmp_path, built, origin) -> Path:
    path = tmp_path / "layers.json"
    path.write_bytes(fakes.index_bytes(built, origin.base))
    return path


def provisioner(tmp_path: Path, index: Path, *, data: str = "data 数据", install: Path | None = None,
                **options) -> RuntimeProvisioner:
    layer_options = {"routes": lambda: [NetworkRoute("直连", None)], "offline": lambda: None,
                     "in_use": lambda: False, **options}
    return RuntimeProvisioner(tmp_path / data, VENDOR, install, index_path=index, layer_options=layer_options)


def wait(provisioner: RuntimeProvisioner, seconds: float = 60) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        job = provisioner.status()["job"]
        if job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("the install job did not finish")


# --- status --------------------------------------------------------------------------------------------------


@WINDOWS
def test_status_keeps_the_runtime_page_shape_before_anything_is_installed(tmp_path, index):
    status = provisioner(tmp_path, index).status()

    assert {"schema", "platform", "supported", "runtime_supported", "media_supported", "media_ready",
            "bootstrap_error", "root", "runtime", "resources", "models", "optional_models", "job"} <= set(status)
    assert status["supported"] and status["bootstrap_error"] == "" and not status["media_ready"]
    assert status["runtime"]["id"] == "python" and status["runtime"]["state"] == "missing"
    assert "下载约" in status["runtime"]["detail"]
    ids = [item["id"] for item in status["resources"]]
    assert "uv" not in ids and {"ffmpeg", "tokcount", "yt-dlp", "aria2c", "node", "pot-provider"} <= set(ids)
    assert [item["id"] for item in status["models"]] == list(provision.PIPELINE_MODELS)
    assert [item["id"] for item in status["optional_models"]] == list(provision.OPTIONAL_MODELS)
    assert {item["id"] for item in status["layers"]} == set(lm.LAYERS)
    assert status["job"]["state"] == "idle"


@WINDOWS
def test_without_layers_json_the_page_says_why_and_nothing_installs(tmp_path):
    broken = RuntimeProvisioner(tmp_path / "data", VENDOR, index_path=tmp_path / "absent.json")
    status = broken.status()

    assert not status["supported"] and "layers" in status["bootstrap_error"].lower()
    assert status["runtime"]["state"] == "missing" and status["resources"] == []
    with pytest.raises(RuntimeProvisionError, match="运行环境层不可用"):
        broken.start("all")
    assert broken.worker_python() is None and broken.tool_path("ffmpeg") is None


def test_the_big_file_root_defaults_under_the_data_dir_and_follows_install_dir(tmp_path, index):
    default = provisioner(tmp_path, index)
    assert default.install_root == (tmp_path / "data 数据" / "runtime").resolve()
    moved = provisioner(tmp_path, index, install=tmp_path / "D 盘" / "Nonoka X" / "runtime")
    assert moved.install_root == (tmp_path / "D 盘" / "Nonoka X" / "runtime").resolve()
    assert moved.paths.layers == moved.install_root / "layers"


def test_an_unsupported_platform_installs_nothing(tmp_path, index, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    other = provisioner(tmp_path, index)

    assert not other.status()["runtime_supported"]
    with pytest.raises(RuntimeProvisionError, match="Windows x64 only"):
        other.start("runtime")
    with pytest.raises(RuntimeProvisionError, match="FFmpeg is unavailable"):
        other.start("media")


# --- install jobs --------------------------------------------------------------------------------------------


@WINDOWS
def test_all_installs_the_three_layers_and_workers_find_them(tmp_path, index, built, monkeypatch):
    monkeypatch.setattr(runtime_install, "msvc_runtime_problem", lambda **_: None)
    page = provisioner(tmp_path, index)

    page.start("all")
    job = wait(page)

    assert job["state"] == "completed" and job["message"] == provision.DONE_MESSAGES["all"]
    status = page.status()
    assert status["runtime"]["state"] == "ready" and status["media_ready"]
    assert all(item["state"] == "ready" for item in status["resources"] + status["models"] + status["optional_models"])
    models = page.paths.layer_version("models", built["models"].version)
    assert page.worker_python() == page.paths.layer_version("python", built["python"].version) / "python.exe"
    # The model layer reaches the engine through its bridge (the launch context's `runtime.models`).
    assert page.engine_model_dir() == models
    environment = page.worker_environment()
    assert "UV_CACHE_DIR" not in environment or environment["UV_CACHE_DIR"] == os.environ.get("UV_CACHE_DIR")
    tools = page.paths.layer_version("tools", built["tools"].version)
    assert environment["PATH"].split(os.pathsep)[:2] == [str(tools / "ffmpeg" / "ffmpeg-n9" / "bin"),
                                                         str(tools / "tokcount")]
    assert environment["PYTHONPATH"].split(os.pathsep)[0] == str(tools / "yt-dlp")


@WINDOWS
def test_media_installs_the_tool_layer_alone(tmp_path, index, origin):
    page = provisioner(tmp_path, index)
    page.start("media")

    assert wait(page)["state"] == "completed"
    status = page.status()
    assert status["media_ready"] and status["runtime"]["state"] == "missing"
    assert {path for path, _ in origin.archive_requests()} == {"/" + item for item in [
        json.loads(index.read_text())["layers"]["tools"]["archive"]["path"]]}


@WINDOWS
def test_an_unusable_vc_runtime_stops_the_python_layer_before_downloading(tmp_path, index, origin, monkeypatch):
    monkeypatch.setattr(runtime_install, "msvc_runtime_problem", lambda **_: "缺少 VC++ 运行库")
    page = provisioner(tmp_path, index)

    page.start("runtime")
    job = wait(page)

    assert job["state"] == "failed" and "缺少 VC++ 运行库" in job["message"]
    assert origin.archive_requests() == []
    # The tools alone do not need it.
    page.start("media")
    assert wait(page)["state"] == "completed"


@WINDOWS
def test_a_root_too_deep_fails_the_job_with_its_own_code(tmp_path, index, origin):
    deep = tmp_path / ("d" * 120) / ("e" * 70)
    page = provisioner(tmp_path, index, install=deep)

    page.start("runtime")
    job = wait(page)

    assert job["state"] == "failed" and job["error"]["code"] == "install_path_too_deep"
    assert "安装位置太深" in job["message"] and origin.archive_requests() == []


@WINDOWS
def test_progress_is_reported_in_bytes_per_layer(tmp_path, index, monkeypatch):
    page = provisioner(tmp_path, index)
    seen: list[dict] = []
    original = page._layer_progress

    def record(value):
        original(value)
        seen.append(dict(page._job))

    monkeypatch.setattr(page, "_layer_progress", record)
    page.start("media")
    wait(page)

    assert {item["stage"] for item in seen} >= {"manifest", "downloading", "unpacking", "checking"}
    assert all(item["resource"] == "tools" for item in seen)
    assert all(item["progress"] is None or item["progress"]["unit"] == "bytes" for item in seen)


def test_cancel_requires_a_running_job(tmp_path, index):
    with pytest.raises(RuntimeProvisionError, match="没有正在进行"):
        provisioner(tmp_path, index).cancel()


@WINDOWS
def test_a_cancelled_job_is_not_a_failure(tmp_path, index):
    page = provisioner(tmp_path, index)
    page._cancel.set()
    page._job["state"] = "running"
    page._run("media")

    job = page.status()["job"]
    assert job["state"] == "cancelled" and job["message"] == provision.CANCELLED_MESSAGE and job["error"] is None


def test_every_target_names_its_layers_and_its_words():
    assert set(runtime_install.TARGET_LAYERS) == provision.TARGETS
    assert set(provision.START_MESSAGES) == set(provision.DONE_MESSAGES) == provision.TARGETS
    assert runtime_install.layers_for("runtime") == ("tools", "python")
    assert runtime_install.ordered(["models", "python", "tools"]) == ("tools", "python", "models")
    assert "模型仍需单独下载" in provision.DONE_MESSAGES["runtime"]


@WINDOWS
def test_repair_reinstalls_only_a_layer_that_is_off(tmp_path, index, origin, built):
    page = provisioner(tmp_path, index)
    page.start("media")
    wait(page)
    tokcount = page.tool_path("tokcount")
    tokcount.write_bytes(b"MZ tokcounX")
    origin.requests.clear()

    page.start("repair")
    assert wait(page)["state"] == "completed"
    assert tokcount.read_bytes() == b"MZ tokcount"
    assert len(origin.archive_requests()) == 1  # the tool layer; the others were never installed


# --- removal, caches, unused layers ----------------------------------------------------------------------------


@WINDOWS
def test_remove_all_takes_the_layers_and_keeps_tasks_and_settings(tmp_path, index):
    page = provisioner(tmp_path, index)
    page.start("media")
    wait(page)
    task = page.paths.tasks / "t1"
    task.mkdir(parents=True)
    (task / "snapshot.json").write_text('{"state": "completed"}', encoding="utf-8")
    (page.paths.data / "settings.json").write_text("{}", encoding="utf-8")

    status = page.remove_all()

    assert status["job"]["target"] == "remove-all" and status["job"]["state"] == "completed"
    left = [entry.name for entry in page.paths.layer("tools").iterdir()]
    assert left == [".install.lock"]  # no version, no pointer; the lock file stays, as lock files do
    assert status["resources"][0]["state"] == "missing"
    assert (task / "snapshot.json").is_file() and (page.paths.data / "settings.json").is_file()


@WINDOWS
def test_remove_all_waits_for_unfinished_tasks(tmp_path, index):
    page = provisioner(tmp_path, index, in_use=lambda: True)
    with pytest.raises(RuntimeProvisionError, match="未结束的任务"):
        page.remove_all()


@WINDOWS
def test_remove_layer_removes_only_models_and_keeps_default_layers(tmp_path, index):
    page = provisioner(tmp_path, index)
    page.start("all")
    assert wait(page)["state"] == "completed"

    with pytest.raises(RuntimeProvisionError, match="仅允许删除模型层"):
        page.remove_layer("tools")
    with pytest.raises(RuntimeProvisionError, match="仅允许删除模型层"):
        page.remove_layer("python")

    status = page.remove_layer("models")
    assert status["job"]["target"] == "remove-models" and status["job"]["state"] == "completed"
    layers = {item["id"]: item["state"] for item in status["layers"]}
    assert layers == {"python": "ready", "models": "missing", "models-moss": "missing", "tools": "ready",
                      "screen-text": "missing"}


def test_cache_usage_and_clear_cache_cover_the_downloads(tmp_path, index):
    page = provisioner(tmp_path, index)
    downloads = page.paths.cache / "downloads"
    downloads.mkdir(parents=True)
    (downloads / "abc.tar.zst.part").write_bytes(b"x" * 1000)
    state = page.paths.cache / "state"
    state.mkdir()
    (state / "keep.json").write_text("{}", encoding="utf-8")

    assert page.cache_usage() == {"schema": 1, "bytes": 1000, "reclaimable": 1000, "files": 1}
    result = page.clear_cache()

    assert result["freed"] == 1000 and result["failures"] == [] and not any(downloads.iterdir())
    assert (state / "keep.json").is_file()


def test_clear_cache_also_empties_the_compile_cache(tmp_path, index):
    page = provisioner(tmp_path, index)
    numba = page.paths.cache / "finesub-transcribe" / "numba"
    numba.mkdir(parents=True)
    (numba / "f.nbi").write_bytes(b"x" * 300)
    downloads = page.paths.cache / "downloads"
    downloads.mkdir(parents=True)
    (downloads / "a.part").write_bytes(b"x" * 100)

    assert page.cache_usage()["bytes"] == 400
    result = page.clear_cache()

    assert result["freed"] == 400 and not any(numba.iterdir()) and numba.is_dir()
    # The rest of the package's cache directory is not the compile cache's.
    assert (page.paths.cache / "finesub-transcribe").is_dir()


def test_clear_cache_refuses_while_an_install_is_running(tmp_path, index):
    page = provisioner(tmp_path, index)
    page._job["state"] = "running"
    with pytest.raises(RuntimeProvisionError, match="安装任务运行中"):
        page.clear_cache()


@WINDOWS
def test_unused_layers_are_listed_and_removed(tmp_path, index):
    page = provisioner(tmp_path, index)
    stray = page.paths.layer("python") / "0123456789abcdef"
    stray.mkdir(parents=True)
    (stray / "python.exe").write_bytes(b"x" * 10)

    listed = page.unused_layers()
    assert listed["bytes"] == 10 and listed["unused"][0]["layer"] == "python"
    assert page.status()["unused_bytes"] == 10

    removed = page.remove_unused_layers()
    assert removed["freed"] == 10 and not stray.exists()


def _install_all(page: RuntimeProvisioner, monkeypatch) -> None:
    monkeypatch.setattr(runtime_install, "msvc_runtime_problem", lambda **_: None)
    page.start("all")
    assert wait(page)["state"] == "completed"


@WINDOWS
def test_cleanup_offers_the_layers_the_current_engine_does_not_need(tmp_path, index, origin, monkeypatch):
    """WP-UI: switched to a cloud API engine, the Python and model layers are unused; switching back
    to FineSub before cleaning downloads nothing again. By default only old versions go; a whole
    layer goes only when the user ticks it (2026-11-19)."""

    page = provisioner(tmp_path, index)
    _install_all(page, monkeypatch)
    downloaded = len(origin.archive_requests())
    stray = page.paths.layer("tools") / "0123456789abcdef"
    stray.mkdir(parents=True)
    (stray / "ffmpeg.exe").write_bytes(b"x" * 10)
    old_python = page.paths.layer("python") / "fedcba9876543210"
    old_python.mkdir(parents=True)
    (old_python / "python.exe").write_bytes(b"x" * 7)

    # FineSub (all three layers needed): only the old versions.
    plan = page.cleanup_plan({"python", "models", "tools"}, set())
    assert sorted((item["layer"], item["reason"]) for item in plan["items"]) == [
        ("python", "old-version"), ("tools", "old-version")]
    assert plan["bytes"] == 17 and plan["layer_bytes"] == 0
    # The engine unknown (`needed` None): the same.
    assert page.cleanup_plan(None, set())["items"] == plan["items"]

    # A cloud API engine: the tool layer stays; Python and models are offered as whole layers, apart
    # from the old versions, which the default frees on its own.
    plan = page.cleanup_plan({"tools"}, set())
    reasons = sorted((item["layer"], item["reason"]) for item in plan["items"])
    assert reasons == [("models", "unneeded"), ("python", "old-version"), ("python", "unneeded"), ("tools", "old-version")]
    assert plan["bytes"] == 17
    assert plan["layer_bytes"] == sum(item["bytes"] for item in plan["items"] if item["reason"] == "unneeded") > 0
    assert plan["blocked_bytes"] == 0

    # Switching back and forth before cleaning re-downloads nothing.
    page.start("all")
    assert wait(page)["state"] == "completed"
    assert len(origin.archive_requests()) == downloaded

    # Nothing ticked: the old versions go, every layer stays.
    result = page.cleanup({"tools"}, set())
    assert sorted(item["layer"] for item in result["removed"]) == ["python", "tools"]
    assert result["kept"] == [] and result["freed"] == 17
    assert not stray.exists() and not old_python.exists()
    status = page.status()
    assert status["runtime"]["state"] == "ready" and status["models"][0]["state"] == "ready"

    # A layer the engine needs cannot be ticked.
    with pytest.raises(RuntimeProvisionError, match="不能整层删除"):
        page.cleanup({"tools"}, set(), ("tools",))
    assert page.status()["media_ready"]

    # Python ticked: it goes as a whole, the model layer stays.
    result = page.cleanup({"tools"}, set(), ("python",))
    assert {item["layer"] for item in result["removed"]} == {"python"} and result["kept"] == []
    status = page.status()
    assert status["media_ready"] and status["runtime"]["state"] == "missing"
    assert status["models"][0]["state"] == "ready"
    assert not page.paths.layer_pointer("python").exists()
    assert [item["layer"] for item in page.cleanup_plan({"tools"}, set())["items"]] == ["models"]


@WINDOWS
def test_cleanup_never_touches_a_layer_an_unfinished_task_may_use(tmp_path, index, monkeypatch):
    page = provisioner(tmp_path, index)
    _install_all(page, monkeypatch)
    stray = page.paths.layer("tools") / "0123456789abcdef"
    stray.mkdir(parents=True)

    page.layers._in_use = lambda: True
    plan = page.cleanup_plan({"tools"}, {"python"})
    blocked = {(item["layer"], item["blocked"]) for item in plan["items"]}
    assert blocked == {("python", "有未结束的任务用到这一层"), ("models", ""), ("tools", "有未结束的任务")}
    assert plan["bytes"] == 0
    assert plan["layer_bytes"] == sum(item["bytes"] for item in plan["items"] if item["layer"] == "models")

    result = page.cleanup({"tools"}, {"python"}, ("python", "models"))
    assert [item["layer"] for item in result["removed"]] == ["models"]
    assert {item["layer"] for item in result["kept"]} == {"python", "tools"}
    assert page.worker_python() is not None and stray.exists()
    assert page.status()["models"][0]["state"] == "missing"



def test_cleanup_refuses_while_an_install_is_running(tmp_path, index):
    page = provisioner(tmp_path, index)
    page._job["state"] = "running"
    with pytest.raises(RuntimeProvisionError, match="安装任务运行中"):
        page.cleanup({"tools"}, set())


@WINDOWS
def test_an_install_waits_for_a_cleanup_in_progress(tmp_path, index):
    page = provisioner(tmp_path, index)
    page._cleaning = True
    with pytest.raises(RuntimeProvisionError, match="正在清理未使用的"):
        page.start("media")


@WINDOWS
def test_a_system_tokcount_serves_while_the_tool_layer_is_missing(tmp_path, index, monkeypatch):
    monkeypatch.setattr(provision.shutil, "which", lambda name: r"C:\tools\tokcount.exe" if name == "tokcount" else None)
    status = provisioner(tmp_path, index).status()
    tokcount = next(item for item in status["resources"] if item["id"] == "tokcount")

    assert tokcount["state"] == "ready" and tokcount["source"] == "system"
    assert next(item for item in status["resources"] if item["id"] == "ffmpeg")["state"] == "missing"


def test_startup_has_no_separate_tool_fetch_any_more(tmp_path, index, monkeypatch):
    page = provisioner(tmp_path, index)
    monkeypatch.setattr(page, "start", lambda target: pytest.fail("nothing to start"))
    page.ensure_required_tools()


@WINDOWS
def test_offline_layer_install_succeeds_without_network(tmp_path, built, origin, index):
    import shutil

    page = provisioner(tmp_path, index)
    before = len(origin.requests)
    archive = tmp_path / "my-tools.tar.zst"
    shutil.copyfile(built["tools"].archive, archive)

    page.start_offline("tools", archive)
    job = wait(page)

    assert job["state"] == "completed"
    assert job["target"] == "offline:tools"
    assert page.status()["media_ready"] is True
    assert len(origin.requests) == before
    assert archive.is_file()


@WINDOWS
def test_offline_layer_install_rejects_unknown_layer_and_empty_path(tmp_path, index):
    page = provisioner(tmp_path, index)
    with pytest.raises(RuntimeProvisionError, match="未知"):
        page.start_offline("bogus", tmp_path / "x.tar.zst")
    with pytest.raises(RuntimeProvisionError, match="离线包"):
        page.start_offline("tools", "")
