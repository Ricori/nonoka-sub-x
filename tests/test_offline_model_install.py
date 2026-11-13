"""The offline package and the layers (WP-E3): every layer, the model layer included, comes from it first.

The package stores `layers.json`'s objects under the same relative paths (plan WP-E2) next to its
`pack.json`; the desktop names it in `NONOKA_OFFLINE_PACK`. Nothing in it is ever deleted.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nonoka_x.provision import RuntimeProvisioner  # noqa: E402
from nonoka_x.runtime import layer_manifest as lm  # noqa: E402
from nonoka_x.runtime import layers as L  # noqa: E402
from nonoka_x.runtime.fetch import NetworkRoute  # noqa: E402

from tests import layer_fakes as fakes  # noqa: E402

pytestmark = pytest.mark.skipif(os.name != "nt", reason="layers are Windows-only")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    built = {layer: fakes.build_layer(tmp_path / "built", layer) for layer in lm.LAYERS}
    origin = fakes.Origin(tmp_path / "empty origin")
    origin.fail_with = 503
    pack = fakes.publish(built, tmp_path / "offline 离线包")
    (pack / L.OFFLINE_PACK_MARKER).write_text("{}", encoding="utf-8")
    monkeypatch.setenv(L.OFFLINE_PACK_ENVIRONMENT, str(pack))
    index = tmp_path / "layers.json"
    index.write_bytes(fakes.index_bytes(built, origin.base))
    yield built, origin, pack, index
    origin.close()


def test_the_models_come_from_the_offline_package_through_the_job(tmp_path, setup, monkeypatch):
    built, origin, pack, index = setup
    from nonoka_x import runtime_install

    monkeypatch.setattr(runtime_install, "msvc_runtime_problem", lambda **_: None)
    page = RuntimeProvisioner(tmp_path / "data", ROOT / "third_party" / "finesub", index_path=index,
                              layer_options={"routes": lambda: [NetworkRoute("直连", None)], "in_use": lambda: False})
    page.start("all")
    deadline = time.monotonic() + 60
    while page.status()["job"]["state"] == "running" and time.monotonic() < deadline:
        time.sleep(0.05)

    assert page.status()["job"]["state"] == "completed"
    assert origin.requests == []
    assert all(item["state"] == "ready" for item in page.status()["models"])
    for item in built.values():
        assert all((pack / path).is_file() for path in item.object_paths())


def test_a_package_missing_an_archive_falls_through_to_the_network(tmp_path, setup):
    built, origin, pack, index = setup
    (pack / built["tools"].object_paths()[1]).unlink()
    layers = L.LayerManager(L.NonokaPaths.for_roots(tmp_path / "data"), L.load_index(index),
                            routes=lambda: [NetworkRoute("直连", None)], in_use=lambda: False)

    with pytest.raises(Exception):
        layers.install("tools")  # the origin answers 503
    assert [path for path, _ in origin.requests if path.endswith(".tar.zst")]


def test_a_corrupt_archive_in_the_package_is_not_used(tmp_path, setup):
    built, origin, pack, index = setup
    archive = pack / built["tools"].object_paths()[1]
    data = bytearray(archive.read_bytes())
    data[-1] ^= 0xFF
    archive.write_bytes(bytes(data))
    origin.fail_with = None
    fakes.publish(built, origin.root)
    layers = L.LayerManager(L.NonokaPaths.for_roots(tmp_path / "data"), L.load_index(index),
                            routes=lambda: [NetworkRoute("直连", None)], in_use=lambda: False)

    assert layers.install("tools").ready
    assert origin.archive_requests()  # the package's copy failed its digest; the network's was used
