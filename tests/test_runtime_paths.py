"""`nonoka_x.runtime.paths`: the layout of plan 4.4 item 24."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nonoka_x.runtime.paths import NonokaPaths, recorded_runtime_dir  # noqa: E402


def test_the_default_layout(tmp_path):
    data = tmp_path / "Nonoka X"

    paths = NonokaPaths.for_roots(data)

    assert paths.data == data.resolve()
    assert paths.runtime == data.resolve() / "runtime"
    assert paths.layers == paths.runtime / "layers"
    assert paths.cache == paths.runtime / "cache"
    assert paths.runtime_state == paths.data / "runtime-state"
    assert paths.engines == paths.data / "engines"
    assert paths.tasks == paths.data / "tasks"


def test_layers_have_versions_and_a_pointer(tmp_path):
    paths = NonokaPaths.for_roots(tmp_path)

    assert paths.layer_version("python", "3f2a9c") == paths.runtime / "layers" / "python" / "3f2a9c"
    assert paths.layer_pointer("models") == paths.runtime / "layers" / "models" / "current.json"


def test_engine_package_directories(tmp_path):
    paths = NonokaPaths.for_roots(tmp_path)
    engine = paths.data / "engines" / "finesub-transcribe"

    assert paths.engine("finesub-transcribe") == engine
    assert paths.engine_run("finesub-transcribe", "1234-ab") == engine / "run" / "1234-ab"
    assert paths.engine_user_data("finesub-correct") == paths.data / "engines" / "finesub-correct" / "user-data"
    assert paths.engine_state("finesub-correct") == paths.data / "engines" / "finesub-correct" / "state"
    assert paths.engine_sandbox("finesub-transcribe") == engine / "sandbox"
    assert paths.task_native("0123abcd", "finesub-transcribe") == (
        paths.data / "tasks" / "0123abcd" / "native" / "finesub-transcribe"
    )


def test_a_relocated_runtime_comes_from_storage_json(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    elsewhere = (tmp_path / "E drive" / "Nonoka X" / "runtime").resolve()
    (data / "storage.json").write_text(json.dumps({"schema": 1, "runtimeDir": str(elsewhere)}), encoding="utf-8")

    paths = NonokaPaths.for_roots(data)

    assert paths.runtime == elsewhere
    assert paths.layers == elsewhere / "layers"
    # Small state never moves with the big-file root.
    assert paths.runtime_state == data.resolve() / "runtime-state"


def test_an_explicit_runtime_dir_wins(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "storage.json").write_text(json.dumps({"runtimeDir": str(tmp_path / "recorded")}), encoding="utf-8")

    assert NonokaPaths.for_roots(data, tmp_path / "given").runtime == (tmp_path / "given").resolve()


@pytest.mark.parametrize(
    "record",
    ["not json", json.dumps([]), json.dumps({"runtimeDir": ""}), json.dumps({"runtimeDir": "relative/path"}),
     json.dumps({"runtimeDir": 5})],
)
def test_an_unusable_record_means_the_default(tmp_path, record):
    (tmp_path / "storage.json").write_text(record, encoding="utf-8")

    assert recorded_runtime_dir(tmp_path) is None
    assert NonokaPaths.for_roots(tmp_path).runtime == tmp_path.resolve() / "runtime"


def test_no_locations_json_and_no_route_state(tmp_path):
    """D18: the big-file root is found from `storage.json` alone; no download-route state."""

    (tmp_path / "locations.json").write_text(json.dumps({"big_data": str(tmp_path / "x")}), encoding="utf-8")
    paths = NonokaPaths.for_roots(tmp_path)

    assert paths.runtime == tmp_path.resolve() / "runtime"
    source = (Path(__file__).resolve().parents[1] / "src" / "nonoka_x" / "runtime" / "paths.py").read_text(encoding="utf-8")
    assert "download-routes" not in source


@pytest.mark.parametrize("name", ["", "..", "a/b", "a\\b", "x..y", "-lead", "trailing.", "C:"])
def test_names_cannot_leave_their_directory(tmp_path, name):
    paths = NonokaPaths.for_roots(tmp_path)

    with pytest.raises(ValueError):
        paths.layer_version("python", name)
    with pytest.raises(ValueError):
        paths.engine(name)
    with pytest.raises(ValueError):
        paths.task_native("0123abcd", name)


def test_nothing_is_created(tmp_path):
    paths = NonokaPaths.for_roots(tmp_path / "data")
    paths.layer_version("python", "abc")
    paths.engine_run("finesub-correct", "1")

    assert not (tmp_path / "data").exists()
