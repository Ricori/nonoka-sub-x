"""The layer manager on small made-up layers and a local origin (`nonoka_x.runtime.layers`, WP-E3).

Covers the plan's list: installing the three layers into an empty root from a local HTTP server, a
resumed download, a wrong archive digest, an archive that does not unpack to its manifest, a process
killed part way (while downloading, after unpacking, after the rename), and moving the whole root.
Also: the path-depth refusal, the offline package, the quick and full checks, extra files, cleaning
old versions, and completing moved models for the migration.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nonoka_x.runtime import layer_manifest as lm  # noqa: E402
from nonoka_x.runtime import layers as L  # noqa: E402
from nonoka_x.runtime.fetch import DigestMismatch, NetworkRoute  # noqa: E402
from nonoka_x.runtime.paths import NonokaPaths  # noqa: E402

from tests import layer_fakes as fakes  # noqa: E402

pytestmark = pytest.mark.skipif(os.name != "nt", reason="layers are Windows-only (tar.exe)")


def direct() -> list[NetworkRoute]:
    return [NetworkRoute("直连", None)]


@pytest.fixture
def built(tmp_path_factory):
    work = tmp_path_factory.mktemp("built")
    return {layer: fakes.build_layer(work, layer) for layer in lm.LAYERS}


@pytest.fixture
def origin(tmp_path_factory, built):
    served = fakes.publish(built, tmp_path_factory.mktemp("origin"))
    server = fakes.Origin(served)
    yield server
    server.close()


def manager(tmp_path: Path, built, origin, *, index: bytes | None = None, in_use=lambda: False,
            data_name: str = "data dir 数据", offline=lambda: None) -> L.LayerManager:
    paths = NonokaPaths.for_roots(tmp_path / data_name)
    return L.LayerManager(paths, L.parse_index(index or fakes.index_bytes(built, origin.base)),
                          in_use=in_use, routes=direct, offline=offline)


# --- the index ------------------------------------------------------------------------------------------------


def test_the_index_is_read_as_wp_e2_writes_it(built, origin):
    index = L.parse_index(fakes.index_bytes(built, origin.base))

    assert set(index.layers) == set(lm.LAYERS)
    assert index.file_layers == ()  # no per-file objects since 2026-11-13
    assert index.entry("python").version == built["python"].version
    assert index.url(index.entry("tools").archive.path).startswith(origin.base + "v1/tools/")


@pytest.mark.parametrize("override, words", [
    ({"python": {"version": "0" * 16}}, "first 16 digits"),
    ({"python": {"archive": {"path": "../x", "size": 1, "sha256": "0" * 64}}}, "python.archive"),
    ({"python": {"manifest": {"path": "a.json", "size": 1, "sha256": "zz"}}}, "not a sha256"),
])
def test_an_index_that_cannot_be_right_is_refused(built, origin, override, words):
    with pytest.raises(L.LayerIndexError, match=words):
        L.parse_index(fakes.index_bytes(built, origin.base, overrides=override))


def test_the_base_url_and_platform_are_checked(built):
    with pytest.raises(L.LayerIndexError, match="baseUrl"):
        L.parse_index(fakes.index_bytes(built, "https://layers.example/no-slash"))
    body = json.loads(fakes.index_bytes(built, "https://layers.example/"))
    body["platform"] = "macos-arm64"
    with pytest.raises(L.LayerIndexError, match="macos-arm64"):
        L.parse_index(json.dumps(body).encode())


# --- installing ----------------------------------------------------------------------------------------------


def test_three_layers_install_into_an_empty_root(tmp_path, built, origin):
    layers = manager(tmp_path, built, origin)
    stages: list[str] = []

    states = layers.ensure(lm.LAYERS, progress=lambda value: stages.append(value.stage))

    assert all(state.ready for state in states.values())
    for layer in lm.LAYERS:
        version = built[layer].version
        pointer = json.loads(layers.paths.layer_pointer(layer).read_text(encoding="utf-8"))
        assert pointer == {"schema": 1, "layer": layer, "version": version}
        root = layers.paths.layer_version(layer, version)
        assert not L.inspect(built[layer].manifest, root, full=True).problems
        # The archive's modification times survive (the separator stamps state them).
        assert all(int((root / item.path).stat().st_mtime) == lm.FIXED_MTIME for item in built[layer].manifest.files)
    assert {"manifest", "downloading", "unpacking", "checking"} <= set(stages)
    # The downloaded archives are gone once the pointers moved; the cache holds no layer.
    assert not list(layers.downloads.glob("*.tar.zst"))
    assert layers.python_executable() == layers.paths.layer_version("python", built["python"].version) / "python.exe"
    assert layers.tool_file("ffprobe").name == "ffprobe.exe"
    assert layers.tool_file("tokcount").parent.name == "tokcount"
    assert layers.tool_dir("yt-dlp").name == "yt-dlp"
    assert layers.model_dir("whisper").name == fakes.HF_REVISION
    assert layers.model_dir("separator").name == "audio-separator"
    assert layers.model_dir("moss-transcribe-diarize").name == fakes.HF_REVISION
    assert layers.model_dir("speaker-embedding").name == fakes.HF_REVISION
    assert layers.model_dir("nemotron3") is None


def test_installing_a_ready_layer_downloads_nothing(tmp_path, built, origin):
    layers = manager(tmp_path, built, origin)
    layers.install("tools")
    origin.requests.clear()

    assert layers.install("tools").ready
    assert origin.requests == []


def test_an_interrupted_download_resumes(tmp_path, tmp_path_factory, origin):
    big = {"python": fakes.build_layer(tmp_path_factory.mktemp("big"), "python", big=3 * 1024 * 1024)}
    fakes.publish(big, origin.root)
    layers = manager(tmp_path, big, origin, index=fakes.index_bytes(big, origin.base))
    origin.cut_after = 1024 * 1024 + 17

    with pytest.raises(Exception):
        layers.install("python")
    partial = layers.downloads / f"{big['python'].archive_sha256}.tar.zst.part"
    assert partial.is_file() and partial.stat().st_size >= 1024 * 1024

    assert layers.install("python").ready
    ranges = [wanted for _path, wanted in origin.archive_requests()]
    assert ranges[0] is None and ranges[-1] and ranges[-1].startswith("bytes=")


def test_a_wrong_archive_digest_is_refused_and_nothing_switches(tmp_path, built, origin):
    entry = built["tools"].entry()
    wrong = {"tools": {"archive": {**entry["archive"], "sha256": "f" * 64}}}
    layers = manager(tmp_path, built, origin, index=fakes.index_bytes(built, origin.base, overrides=wrong))

    with pytest.raises(DigestMismatch):
        layers.install("tools")

    assert layers.pointer("tools") is None
    assert not layers.paths.layer_version("tools", built["tools"].version).exists()
    assert list(layers.downloads.glob("*.part.bad"))


def test_a_wrong_manifest_digest_is_refused_before_the_archive(tmp_path, built, origin):
    entry = built["tools"].entry()
    body = json.loads(fakes.index_bytes(built, origin.base))
    body["layers"]["tools"]["manifest"] = {**entry["manifest"], "sha256": "e" * 64}
    body["layers"]["tools"]["version"] = "e" * 16
    layers = manager(tmp_path, built, origin, index=json.dumps(body).encode())

    with pytest.raises(DigestMismatch):
        layers.install("tools")
    assert origin.archive_requests() == []


def test_an_archive_that_does_not_unpack_to_its_manifest_is_refused(tmp_path, tmp_path_factory, origin):
    files = fakes.default_files("tools")
    tampered = {**files, "tokcount/tokcount.exe": b"MZ tokcounX"}  # same size, other bytes
    bad = {"tools": fakes.build_layer(tmp_path_factory.mktemp("bad"), "tools", files, archive_files=tampered)}
    fakes.publish(bad, origin.root)
    layers = manager(tmp_path, bad, origin, index=fakes.index_bytes(bad, origin.base))

    # The quick check after a fresh unpack goes by sizes; the full one finds the changed bytes.
    with pytest.raises(L.LayerMismatch, match="sha256 differs: tokcount/tokcount.exe"):
        layers.install("tools", full=True)
    assert layers.pointer("tools") is None
    version = bad["tools"].version
    assert not layers.paths.layer_version("tools", version).exists()
    assert not layers.paths.layer("tools").joinpath(version + L.UNPACK_SUFFIX).exists()


def test_an_archive_with_a_missing_file_fails_even_the_quick_check(tmp_path, tmp_path_factory, origin):
    files = fakes.default_files("tools")
    short = {name: data for name, data in files.items() if name != "tokcount/tokcount.exe"}
    bad = {"tools": fakes.build_layer(tmp_path_factory.mktemp("short"), "tools", files, archive_files=short)}
    fakes.publish(bad, origin.root)
    layers = manager(tmp_path, bad, origin, index=fakes.index_bytes(bad, origin.base))

    with pytest.raises(L.LayerMismatch, match="missing: tokcount/tokcount.exe"):
        layers.install("tools")


# --- killed part way --------------------------------------------------------------------------------------------

_CHILD = r"""
import json, os, sys
sys.path[:0] = [{src!r}, {root!r}]
from nonoka_x.runtime import layers as L
from nonoka_x.runtime.fetch import NetworkRoute
from nonoka_x.runtime.paths import NonokaPaths
spec = json.loads(open(sys.argv[1], encoding="utf-8").read())
manager = L.LayerManager(NonokaPaths.for_roots(spec["data"]), L.parse_index(spec["index"].encode()),
                         in_use=lambda: False, routes=lambda: [NetworkRoute("direct", None)], offline=lambda: None)
die = spec["die"]
def progress(value):
    if die == "downloading" and value.stage == "downloading" and value.completed >= 1024 * 1024:
        os._exit(9)
    if die == "checking" and value.stage == "checking":
        os._exit(9)
if die == "pointer":
    manager._write_pointer = lambda layer, version: os._exit(9)
manager.install(spec["layer"], progress=progress)
"""


def _killed(tmp_path: Path, layers: L.LayerManager, index: bytes, layer: str, die: str) -> int:
    script = tmp_path / "child.py"
    script.write_text(_CHILD.format(src=str(ROOT / "src"), root=str(ROOT)), encoding="utf-8")
    spec = tmp_path / f"spec-{die}.json"
    spec.write_text(json.dumps({"data": str(layers.paths.data), "index": index.decode(), "layer": layer, "die": die}),
                    encoding="utf-8")
    return subprocess.run([sys.executable, str(script), str(spec)], capture_output=True, timeout=120).returncode


@pytest.fixture
def big(tmp_path_factory, origin):
    layer = {"python": fakes.build_layer(tmp_path_factory.mktemp("big"), "python", big=3 * 1024 * 1024)}
    fakes.publish(layer, origin.root)
    return layer


def test_killed_while_downloading_the_next_attempt_resumes(tmp_path, big, origin):
    index = fakes.index_bytes(big, origin.base)
    layers = manager(tmp_path, big, origin, index=index)

    assert _killed(tmp_path, layers, index, "python", "downloading") == 9
    assert layers.pointer("python") is None

    assert layers.install("python").ready
    assert origin.archive_requests()[-1][1].startswith("bytes=")


def test_killed_after_unpacking_the_half_made_directory_is_redone(tmp_path, big, origin):
    index = fakes.index_bytes(big, origin.base)
    layers = manager(tmp_path, big, origin, index=index)
    version = big["python"].version

    assert _killed(tmp_path, layers, index, "python", "checking") == 9
    assert layers.paths.layer("python").joinpath(version + L.UNPACK_SUFFIX).is_dir()
    assert layers.pointer("python") is None

    assert layers.install("python").ready
    assert not layers.paths.layer("python").joinpath(version + L.UNPACK_SUFFIX).exists()


def test_killed_before_the_pointer_moved_the_finished_version_is_adopted(tmp_path, big, origin):
    index = fakes.index_bytes(big, origin.base)
    layers = manager(tmp_path, big, origin, index=index)
    version = big["python"].version

    assert _killed(tmp_path, layers, index, "python", "pointer") == 9
    assert layers.paths.layer_version("python", version).is_dir() and layers.pointer("python") is None
    origin.requests.clear()

    assert layers.install("python").ready
    assert origin.archive_requests() == []  # nothing downloaded again


# --- where it may go --------------------------------------------------------------------------------------------


def test_a_root_too_deep_for_the_deepest_file_is_refused_before_downloading(tmp_path, built, origin):
    deep = "d" * 120
    layers = manager(tmp_path, built, origin, data_name=f"{deep}/{deep[:60]}")
    manifest = built["python"].manifest
    staging = layers.wanted_dir("python").with_name(built["python"].version + L.UNPACK_SUFFIX)
    expected, _ = L.longest_path(staging, manifest)
    assert expected > L.MAX_PATH_CHARS

    with pytest.raises(L.PathTooDeep) as refused:
        layers.install("python")

    assert refused.value.longest == expected and refused.value.excess == expected - L.MAX_PATH_CHARS
    message = str(refused.value)
    assert L.PATH_TOO_DEEP_MESSAGE in message and f"{expected - L.MAX_PATH_CHARS} 个字符" in message
    assert origin.archive_requests() == []


def test_the_whole_root_moves_to_another_path_with_spaces_and_stays_ready(tmp_path, built, origin):
    layers = manager(tmp_path, built, origin)
    layers.ensure(lm.LAYERS)
    moved = tmp_path / "其他 盘" / "Nonoka X" / "runtime"
    moved.parent.mkdir(parents=True)
    shutil.move(str(layers.paths.runtime), str(moved))

    elsewhere = L.LayerManager(NonokaPaths.for_roots(tmp_path / "data dir 数据", moved), layers.index,
                               in_use=lambda: False, routes=direct, offline=lambda: None)

    assert all(state.ready for state in elsewhere.states(full=True).values())
    assert elsewhere.python_executable().is_relative_to(moved)


# --- the offline package -----------------------------------------------------------------------------------


def test_the_offline_package_comes_first_and_is_never_deleted(tmp_path, built, origin):
    pack = fakes.publish(built, tmp_path / "offline pack")
    (pack / L.OFFLINE_PACK_MARKER).write_text("{}", encoding="utf-8")
    origin.fail_with = 500
    layers = manager(tmp_path, built, origin, offline=lambda: pack)

    assert all(state.ready for state in layers.ensure(lm.LAYERS).values())
    assert origin.requests == []
    for item in built.values():
        assert all((pack / path).is_file() for path in item.object_paths())


def test_offline_package_lookup_needs_its_marker(tmp_path, monkeypatch):
    monkeypatch.setenv(L.OFFLINE_PACK_ENVIRONMENT, str(tmp_path))
    assert L.offline_pack() is None
    (tmp_path / L.OFFLINE_PACK_MARKER).write_text("{}", encoding="utf-8")
    assert L.offline_pack() == tmp_path


# --- checks, repair and extra files ---------------------------------------------------------------------------


def test_the_quick_check_compares_sizes_and_the_full_check_hashes(tmp_path, built, origin):
    layers = manager(tmp_path, built, origin)
    root = layers.install("tools").path
    (root / "tokcount" / "tokcount.exe").write_bytes(b"MZ tokcounX")

    assert layers.state("tools", fresh=True).ready
    damaged = layers.state("tools", full=True)
    assert damaged.state == "damaged" and damaged.problems == ("sha256 differs: tokcount/tokcount.exe",)

    assert layers.repair("tools").ready
    assert (root / "tokcount" / "tokcount.exe").read_bytes() == b"MZ tokcount"
    assert not list(layers.downloads.glob("*.tar.zst"))


def test_a_missing_file_makes_the_layer_damaged(tmp_path, built, origin):
    layers = manager(tmp_path, built, origin)
    root = layers.install("tools").path
    (root / "tokcount" / "tokcount.exe").unlink()

    state = layers.state("tools", fresh=True)
    assert state.state == "damaged" and "missing: tokcount/tokcount.exe" in state.problems
    assert layers.tool_file("tokcount") is None


def test_extra_files_are_named_without_making_the_layer_unready(tmp_path, built, origin):
    layers = manager(tmp_path, built, origin)
    root = layers.install("python").path
    cache = root / "Lib" / "site-packages" / "librosa" / "core" / "__pycache__" / "spectrum._x-1035.py312.nbi"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"numba")

    state = layers.state("python", fresh=True)
    assert state.ready
    assert state.extra == ("Lib/site-packages/librosa/core/__pycache__/spectrum._x-1035.py312.nbi",)

    assert layers.repair("python").ready
    assert not cache.exists()


def test_a_pointer_at_another_version_is_outdated(tmp_path, tmp_path_factory, built, origin):
    layers = manager(tmp_path, built, origin)
    layers.install("tools")
    newer = {**built, "tools": fakes.build_layer(tmp_path_factory.mktemp("newer"), "tools", tag="2")}
    fakes.publish(newer, origin.root)
    updated = manager(tmp_path, newer, origin, index=fakes.index_bytes(newer, origin.base))

    state = updated.state("tools")
    assert state.state == "outdated" and state.current == built["tools"].version
    assert updated.tool_file("ffmpeg") is None  # only the version this release wants is used

    assert updated.install("tools").ready
    assert not updated.paths.layer_version("tools", built["tools"].version).exists()


# --- cleaning ---------------------------------------------------------------------------------------------------


def test_old_versions_stay_while_a_task_is_unfinished(tmp_path, tmp_path_factory, built, origin):
    busy = {"value": True}
    layers = manager(tmp_path, built, origin, in_use=lambda: busy["value"])
    layers.install("tools")
    old = layers.paths.layer_version("tools", built["tools"].version)
    newer = {**built, "tools": fakes.build_layer(tmp_path_factory.mktemp("newer"), "tools", tag="2")}
    fakes.publish(newer, origin.root)
    updated = L.LayerManager(layers.paths, L.parse_index(fakes.index_bytes(newer, origin.base)),
                             in_use=lambda: busy["value"], routes=direct, offline=lambda: None)

    assert updated.install("tools").ready
    assert old.is_dir()
    assert [item.name for item in updated.unused()] == [built["tools"].version]
    overview = updated.overview()
    assert overview["unused_bytes"] > 0 and {item["id"] for item in overview["layers"]} == set(lm.LAYERS)

    busy["value"] = False
    result = updated.remove_unused()
    assert [item["name"] for item in result["removed"]] == [built["tools"].version] and result["freed"] > 0
    assert not old.exists()


def test_a_version_with_open_files_is_left_whole(tmp_path, built, origin):
    layers = manager(tmp_path, built, origin)
    stray = layers.paths.layer("tools") / "0123456789abcdef"
    (stray / "bin").mkdir(parents=True)
    (stray / "bin" / "a.dll").write_bytes(b"x")
    (stray / "b.txt").write_bytes(b"y")

    with open(stray / "bin" / "a.dll", "rb"):
        result = layers.remove_unused()
    assert result["removed"] == [] and len(result["kept"]) == 1
    assert (stray / "b.txt").is_file()  # renamed aside first, so nothing was half-deleted

    assert layers.remove_unused()["removed"]


def test_the_migrated_models_directory_is_never_a_leftover(tmp_path, built, origin):
    layers = manager(tmp_path, built, origin)
    (layers.paths.layer("models") / L.MIGRATED_NAME).mkdir(parents=True)

    assert layers.unused() == []


def test_the_task_check_reads_unfinished_snapshots(tmp_path, built, origin):
    paths = NonokaPaths.for_roots(tmp_path / "data")
    layers = L.LayerManager(paths, L.parse_index(fakes.index_bytes(built, origin.base)), routes=direct,
                            offline=lambda: None)
    task = paths.tasks / "task-1"
    task.mkdir(parents=True)
    (task / "snapshot.json").write_text(json.dumps({"state": "running"}), encoding="utf-8")
    assert layers._in_use()
    (task / "snapshot.json").write_text(json.dumps({"state": "completed"}), encoding="utf-8")
    assert not layers._in_use()


# --- migration: completing moved models ----------------------------------------------------------------------


def _moved_models(layers: L.LayerManager, built, *, drop: tuple[str, ...] = (), extra: dict[str, bytes] | None = None,
                  old_stamp: bool = False) -> Path:
    """What WP-M1 leaves: the old models root renamed to `layers\\models\\migrated`."""

    directory = layers.paths.layer_version("models", L.MIGRATED_NAME)
    files = fakes.default_files("models")
    for name, data in files.items():
        if name in drop:
            continue
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    if old_stamp:
        # The engine stamped the time it found when the file was downloaded.
        checkpoint = directory / fakes.SEPARATOR_FILE
        os.utime(checkpoint, (1700000000, 1700000000))
        stamp = json.dumps({"sha256": built["models"].manifest.by_path()[fakes.SEPARATOR_FILE].sha256,
                            "size": checkpoint.stat().st_size, "mtime_ns": 1700000000 * 10**9})
        (directory / f"{fakes.SEPARATOR_FILE}.verified").write_text(stamp, encoding="utf-8")
    for name, data in (extra or {}).items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return directory


def test_moved_models_that_all_match_are_registered_without_downloading(tmp_path, built, origin):
    layers = manager(tmp_path, built, origin)
    moved = _moved_models(layers, built, old_stamp=True,
                          extra={f"{fakes.HF_DIRECTORY}/blobs/abc": b"old blob", "audio-separator/accel/x.so": b"aoti",
                                 f"{fakes.HF_DIRECTORY}/refs/main": fakes.HF_REVISION.encode()})

    report = layers.complete_models(moved)

    assert origin.archive_requests() == [] and report.downloaded == 0 and report.fetched == ()
    assert report.rebuilt == (f"{fakes.SEPARATOR_FILE}.verified",)
    assert set(report.removed) == {f"{fakes.HF_DIRECTORY}/blobs/abc", "audio-separator/accel/x.so",
                                   f"{fakes.HF_DIRECTORY}/refs/main"}
    assert not moved.exists()
    state = layers.state("models", full=True)
    assert state.ready and not state.extra
    assert int((state.path / fakes.SEPARATOR_FILE).stat().st_mtime) == lm.FIXED_MTIME


def test_moved_models_with_missing_or_wrong_files_get_only_those_from_the_archive(tmp_path, built, origin):
    layers = manager(tmp_path, built, origin)
    moved = _moved_models(layers, built, drop=(f"{fakes.HF_SNAPSHOT}/model.bin",))
    (moved / fakes.HF_SNAPSHOT / "config.json").write_bytes(b"{ }")
    kept = moved / fakes.SEPARATOR_FILE
    kept_identity = kept.stat().st_ino

    report = layers.complete_models(moved)

    assert set(report.fetched) == {f"{fakes.HF_SNAPSHOT}/model.bin", f"{fakes.HF_SNAPSHOT}/config.json"}
    assert report.source == "network" and report.downloaded == built["models"].archive.stat().st_size
    root = layers.paths.layer_version("models", built["models"].version)
    assert (root / fakes.SEPARATOR_FILE).stat().st_ino == kept_identity  # kept, not unpacked again
    assert layers.state("models", full=True).ready
    assert not list(layers.downloads.glob("*.tar.zst"))


def test_model_completion_takes_the_archive_from_the_offline_package(tmp_path, built, origin):
    pack = fakes.publish(built, tmp_path / "pack")
    (pack / L.OFFLINE_PACK_MARKER).write_text("{}", encoding="utf-8")
    origin.fail_with = 500
    layers = manager(tmp_path, built, origin, offline=lambda: pack)
    moved = _moved_models(layers, built, drop=(fakes.SEPARATOR_FILE,))

    report = layers.complete_models(moved)

    assert report.source == "offline" and report.downloaded == 0 and origin.requests == []
    assert layers.state("models", full=True).ready
    assert (pack / built["models"].object_paths()[1]).is_file()


def test_model_completion_can_run_again_after_an_interruption(tmp_path, built, origin):
    layers = manager(tmp_path, built, origin)
    moved = _moved_models(layers, built)
    layers.complete_models(moved)

    again = layers.complete_models(moved)  # the directory is already the layer version
    assert again.kept == 0 and again.fetched == ()
    assert layers.state("models").ready


def test_a_link_inside_a_layer_makes_it_damaged_and_is_not_followed(tmp_path, built, origin):
    import _winapi

    layers = manager(tmp_path, built, origin)
    root = layers.install("tools").path
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"not ours")
    _winapi.CreateJunction(str(outside), str(root / "junction"))
    try:
        state = layers.state("tools", fresh=True)
        assert state.state == "damaged" and state.problems == ("link (layers hold only regular files): junction",)
        assert not any("secret" in item for item in state.extra)
    finally:
        os.rmdir(root / "junction")  # removes the junction only
    assert (outside / "secret.txt").is_file()


def test_the_single_pass_scan_agrees_with_the_manifest_compare(tmp_path, built, origin):
    layers = manager(tmp_path, built, origin)
    root = layers.install("python").path
    (root / "python.exe").write_bytes(b"MZ")
    (root / "extra.txt").write_bytes(b"x")
    (root / "Lib" / "empty").mkdir()
    manifest = built["python"].manifest

    found = L.inspect(manifest, root, full=True).problems
    expected = [line.split(" (")[0] for line in lm.compare(manifest, root, full=True)]  # compare adds sizes
    assert sorted(found) == sorted(expected) and len(found) == 3


def test_install_from_single_layer_offline_archive_uses_no_network(tmp_path, built, origin):
    # Origin is deliberately closed/unused: installing from a picked .tar.zst must make 0 HTTP calls.
    before = len(origin.requests)
    mgr = manager(tmp_path, built, origin)
    picked = tmp_path / "downloaded-tools-layer.tar.zst"
    shutil.copyfile(built["tools"].archive, picked)
    # Seed a leftover .part file from an abandoned slow online download to verify it gets cleaned up.
    entry = mgr.index.entry("tools")
    mgr.downloads.mkdir(parents=True, exist_ok=True)
    part = mgr.downloads / f"{entry.archive.sha256}.tar.zst.part"
    part.write_bytes(b"half")

    stages: list[str] = []
    state = mgr.install("tools", archive_path=picked, progress=lambda item: stages.append(item.stage))

    assert state.ready
    assert len(origin.requests) == before
    assert picked.is_file()  # user's local archive is never deleted
    assert not part.exists()
    assert "offline" in stages and "unpacking" in stages


def test_install_from_offline_archive_rejects_wrong_layer_and_corrupt_file(tmp_path, built, origin):
    mgr = manager(tmp_path, built, origin)
    # Passing python's archive to tools should identify the wrong layer clearly.
    with pytest.raises(L.LayerMismatch, match="Python 运行环境"):
        mgr.install("tools", archive_path=built["python"].archive)

    # Corrupt file of identical size should fail sha256 verification.
    bad = tmp_path / "corrupt-tools.tar.zst"
    bad.write_bytes(b"\x00" * built["tools"].archive.stat().st_size)
    with pytest.raises(L.LayerMismatch, match="校验失败"):
        mgr.install("tools", archive_path=bad)
    assert not mgr.state("tools").ready
