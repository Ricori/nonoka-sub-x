from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nonoka_x import model_install


REPOSITORY = "example/model"
REVISION = "abc123"


@pytest.fixture
def pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "offline-pack"
    snapshot = root / "huggingface" / "models--example--model" / REVISION / "plda"
    snapshot.mkdir(parents=True)
    (root / "pack.json").write_text("{}", encoding="utf-8")
    (snapshot.parent / "model.bin").write_bytes(b"weights")
    (snapshot / "plda.npz").write_bytes(b"nested")
    monkeypatch.setenv("NONOKA_OFFLINE_PACK", str(root))
    return root


def _entry() -> SimpleNamespace:
    return SimpleNamespace(repo=REPOSITORY, revision=REVISION)


def test_a_snapshot_is_copied_as_plain_files_with_progress(tmp_path: Path, pack: Path) -> None:
    models = tmp_path / "models"
    events: list[dict] = []

    snapshot = model_install._install_from_offline_pack(_entry(), "m", models, events.append)

    assert snapshot == models / "huggingface" / "hub" / "models--example--model" / "snapshots" / REVISION
    assert (snapshot / "model.bin").read_bytes() == b"weights"
    assert (snapshot / "plda" / "plda.npz").read_bytes() == b"nested"
    assert not snapshot.is_symlink()
    assert [path.name for path in snapshot.parent.iterdir()] == [REVISION]
    finished = [event for event in events if event["type"] == "progress"][-1]
    assert finished["completed"] == finished["total"] == len(b"weights") + len(b"nested")


def test_a_partial_snapshot_from_an_earlier_attempt_is_replaced(tmp_path: Path, pack: Path) -> None:
    models = tmp_path / "models"
    stale = models / "huggingface" / "hub" / "models--example--model" / "snapshots" / REVISION
    stale.mkdir(parents=True)
    (stale / "leftover").write_bytes(b"x")

    snapshot = model_install._install_from_offline_pack(_entry(), "m", models, lambda _event: None)

    assert not (snapshot / "leftover").exists()


def test_a_model_the_package_lacks_goes_online(tmp_path: Path, pack: Path) -> None:
    other = SimpleNamespace(repo="example/other", revision=REVISION)

    assert model_install._install_from_offline_pack(other, "m", tmp_path / "models", lambda _event: None) is None


def test_nothing_happens_without_a_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NONOKA_OFFLINE_PACK", raising=False)

    assert model_install._install_from_offline_pack(_entry(), "m", tmp_path / "models", lambda _event: None) is None
