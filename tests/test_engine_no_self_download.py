"""WP-R7: a missing model stops the engine with a clear error, and nothing reaches for the network.

The engine used to fetch a model it found missing (`finesub_bootstrap.model_ensure`,
one download child per attempt, and `model_fetch.fetch_fixed_files` for the
separator). Plan 4.2 item 16 removes that: `ensure_hf_model` and
`fetch_fixed_files` raise `ModelUnavailable` instead. Here the engine's own entry
for its weights (`finesub.speech.runtime.hf_weights.prepare`, locked, unchanged)
is asked for every model with its directory gone, under an audit hook that
records every connection, name lookup and child process: the error has to name
the model and where it was looked for, and the record has to stay empty.

With the weights present, nothing changes: the same fast path, the same answer.

The separator's own library still fetches its files when they are missing
(`separation.place_separator_files`, locked, swallows the error): that path is
closed by the readiness check, which holds the model layer to its manifest
(plan 4.2 item 16, `nonoka_x.runtime.layers`).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT / "src", ROOT / "third_party" / "finesub" / "src"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from finesub.speech.runtime import hf_weights  # noqa: E402
from finesub_bootstrap import hf_verify, model_caches, model_ensure, model_fetch, model_manifest  # noqa: E402
from finesub_bootstrap.model_ensure import ModelUnavailable  # noqa: E402

#: What the audit hook records while `_RECORDING` is on: (event, detail).
_SEEN: list[tuple[str, str]] = []
_RECORDING = [False]
_WATCHED = {
    "socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.sendto",
    "subprocess.Popen", "os.system", "os.startfile", "os.spawn", "os.posix_spawn", "os.exec",
}


def _audit(event: str, arguments: tuple) -> None:
    if _RECORDING[0] and event in _WATCHED:
        _SEEN.append((event, repr(arguments)[:200]))


sys.addaudithook(_audit)


@pytest.fixture
def offline():
    """Record every connection, lookup and child process for the duration of a test."""

    _SEEN.clear()
    _RECORDING[0] = True
    try:
        yield _SEEN
    finally:
        _RECORDING[0] = False


def test_the_record_sees_a_lookup_and_a_child(offline):
    """The record is worth trusting: it does see what the tests below assert never happens."""

    import socket
    import subprocess

    socket.getaddrinfo("localhost", 9)
    subprocess.run([sys.executable, "-c", "pass"], check=True)

    assert {event for event, _ in offline} >= {"socket.getaddrinfo", "subprocess.Popen"}


@pytest.fixture
def models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty model layer, named the way the bridge names it for the transcription half."""

    layer = tmp_path / "models"
    (layer / "huggingface" / "hub").mkdir(parents=True)
    for name in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "HF_ENDPOINT", "HF_HUB_OFFLINE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HF_HOME", str(layer / "huggingface"))
    monkeypatch.setenv("FINESUB_MODEL_DIR", str(layer))
    # FineSub's managed layout (`%LOCALAPPDATA%\FineSub`) is asked for too; keep it off the machine's.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    # The loader's cache root is asked of `huggingface_hub` when it is importable.
    try:
        from huggingface_hub import constants
    except ImportError:
        pass
    else:
        monkeypatch.setattr(constants, "HF_HUB_CACHE", str(layer / "huggingface" / "hub"))
    return layer


# -- a small model of our own, so the present case needs no gigabytes ------------------------------------------

PAYLOAD = {"model.bin": b"weights" * 64, "config.json": b"{}"}
REVISION = "0123456789abcdef0123456789abcdef01234567"
CACHE_DIR = "models--example--tiny"


@pytest.fixture
def tiny(models: Path, monkeypatch: pytest.MonkeyPatch):
    """A manifest entry `tiny` with two listed files, placed as the model layer places them."""

    entry = model_manifest.ModelEntry(
        model_id="tiny",
        repo="example/tiny",
        revision=REVISION,
        files=tuple(
            model_manifest.ManifestFile(
                name=name, url=f"https://huggingface.co/example/tiny/resolve/{REVISION}/{name}",
                size=len(data), sha256=hashlib.sha256(data).hexdigest(),
            )
            for name, data in PAYLOAD.items()
        ),
    )
    real = model_manifest.entry_for
    monkeypatch.setattr(model_ensure, "entry_for", lambda model_id: entry if model_id == "tiny" else real(model_id))
    monkeypatch.setitem(model_caches._ENSURABLE_HF_CACHE_DIRS, "tiny", CACHE_DIR)
    hub = models / "huggingface" / "hub"
    snapshot = hub / CACHE_DIR / "snapshots" / REVISION
    snapshot.mkdir(parents=True)
    for name, data in PAYLOAD.items():
        (snapshot / name).write_bytes(data)
    hf_verify.write_marker(hub, CACHE_DIR, entry)
    return entry, hub, snapshot


def test_a_present_model_takes_the_fast_path_unchanged(tiny, offline):
    entry, hub, snapshot = tiny
    marker = hf_verify.marker_path(hub, CACHE_DIR).read_bytes()

    model_ensure.ensure_hf_model("tiny", data_root=hub.parent, models_root=None)
    plan = hf_weights.prepare("tiny")

    assert plan.revision == REVISION
    assert plan.local_files_only is True
    assert model_ensure.pinned_snapshot_dir("tiny") == snapshot
    assert hf_verify.marker_path(hub, CACHE_DIR).read_bytes() == marker
    assert offline == []


def _remove_file(hub: Path, snapshot: Path, entry) -> None:
    (snapshot / "model.bin").unlink()


def _remove_directory(hub: Path, snapshot: Path, entry) -> None:
    import shutil

    shutil.rmtree(hub / CACHE_DIR)


def _truncate(hub: Path, snapshot: Path, entry) -> None:
    (snapshot / "model.bin").write_bytes(b"short")


def _mark_failed(hub: Path, snapshot: Path, entry) -> None:
    hf_verify.write_marker(hub, CACHE_DIR, entry, failed=("model.bin",))


def _mark_stale(hub: Path, snapshot: Path, entry) -> None:
    other = model_manifest.ModelEntry(model_id="tiny", repo="example/tiny", revision="f" * 40, files=entry.files)
    hf_verify.write_marker(hub, CACHE_DIR, other)


@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        (_remove_directory, "没有钉住的版本"),
        (_remove_file, "清单列的文件不全"),
        (_truncate, "清单列的文件不全"),
        (_mark_failed, "上次校验没有通过"),
        (_mark_stale, "校验标记与模型清单对不上"),
    ],
)
def test_a_model_not_as_pinned_raises_without_the_network(tiny, offline, damage, reason):
    entry, hub, snapshot = tiny
    damage(hub, snapshot, entry)
    before = sorted(str(path) for path in hub.rglob("*"))

    with pytest.raises(ModelUnavailable) as raised:
        hf_weights.prepare("tiny")

    message = str(raised.value)
    assert "tiny" in message and str(hub / CACHE_DIR) in message and reason in message
    assert "不会自己下载" in message
    assert isinstance(raised.value, hf_verify.VerificationMismatch)
    assert offline == []
    # Nothing written, moved aside or removed.
    assert sorted(str(path) for path in hub.rglob("*")) == before


# -- the real manifest, with the model directories gone --------------------------------------------------------


@pytest.mark.parametrize("model_id", sorted(model_caches._ENSURABLE_HF_CACHE_DIRS))
def test_every_engine_model_missing_from_the_layer_is_a_clear_error(models, offline, model_id):
    if model_manifest.entry_for(model_id) is None:
        pytest.skip(f"{model_id} is not in the engine's manifest")

    with pytest.raises(ModelUnavailable) as raised:
        hf_weights.prepare(model_id)
    assert model_id in str(raised.value)
    with pytest.raises(ModelUnavailable):
        hf_weights.prepare_directory(model_id)

    assert offline == []
    assert list((models / "huggingface" / "hub").iterdir()) == []


def test_the_download_child_is_gone():
    assert not hasattr(model_ensure, "main")
    assert not hasattr(model_ensure, "_download")
    assert not hasattr(model_ensure, "verify_downloaded")
    assert "download" not in model_ensure.ensure_hf_model.__code__.co_varnames


# -- the separator's fixed files -------------------------------------------------------------------------------


def _separator_entry(directory: Path) -> model_manifest.ModelEntry:
    files = {"model.ckpt": b"checkpoint" * 32, "model.yaml": b"a: 1\n"}
    for name, data in files.items():
        (directory / name).write_bytes(data)
    listed = [
        model_manifest.ManifestFile(name=name, url=f"https://github.com/x/{name}", size=len(data),
                                    sha256=hashlib.sha256(data).hexdigest())
        for name, data in files.items()
    ]
    (directory / "index.json").write_text("{}", encoding="utf-8")
    listed.append(model_manifest.ManifestFile(name="index.json", url="https://raw.githubusercontent.com/x/index.json",
                                              size=0, sha256=""))
    return model_manifest.ModelEntry(model_id="separator", files=tuple(listed))


def test_separator_files_in_place_are_checked_and_stamped_as_before(tmp_path, offline):
    entry = _separator_entry(tmp_path)

    model_fetch.fetch_fixed_files(entry, tmp_path, data_root=tmp_path, region="global")

    for wanted in entry.files:
        stamp = model_fetch.verified_stamp_path(tmp_path / wanted.name)
        if wanted.is_verifiable:
            assert json.loads(stamp.read_text(encoding="utf-8"))["sha256"] == wanted.sha256
        else:
            assert not stamp.exists()
    assert offline == []


@pytest.mark.parametrize("name", ["model.ckpt", "index.json"])
def test_a_missing_separator_file_raises_without_the_network(tmp_path, offline, name):
    entry = _separator_entry(tmp_path)
    (tmp_path / name).unlink()

    with pytest.raises(ModelUnavailable) as raised:
        model_fetch.fetch_fixed_files(entry, tmp_path, data_root=tmp_path, region="global")

    assert name in str(raised.value) and str(tmp_path) in str(raised.value)
    assert not (tmp_path / name).exists()
    assert offline == []


def test_a_wrong_separator_checkpoint_is_not_replaced(tmp_path, offline):
    entry = _separator_entry(tmp_path)
    (tmp_path / "model.ckpt").write_bytes(b"x" * len(b"checkpoint" * 32))

    with pytest.raises(ModelUnavailable):
        model_fetch.fetch_fixed_files(entry, tmp_path, data_root=tmp_path, region="global")

    assert (tmp_path / "model.ckpt").read_bytes() == b"x" * len(b"checkpoint" * 32)
    assert offline == []
