"""What a denied publishing rename has to keep doing, upstream or not.

A resource install ends by renaming its staging tree onto the version
directory, and on Windows that rename is denied while anything still holds a
handle inside the tree -- the antivirus scan of a freshly unpacked ffmpeg.exe
is the one users actually hit. A bare `os.replace` lets that `[WinError 5]`
end a multi-minute install and surfaces it verbatim in the desktop UI.

This used to be the contract for
`patches/finesub/0002-retry-directory-publish-rename.patch`. Upstream 0.5.0
took the behaviour (`fsops.ReplaceBudget`, `PUBLISH_REPLACE`,
`RECORD_REPLACE`) and the patch is gone, so what remains here is a regression
guard: the promise is Nonoka X's desktop UX, the implementation is now
upstream's, and a sync that quietly drops it must not pass.

What has to stay true:

- the publishing replace waits the holder out instead of failing on the first
  denial, and the wait is bounded rather than unbounded;
- `write_atomic` waits too, on a shorter budget: the same machine loses the
  same race on `snapshot.json`, and there it fails a whole task;
- the probing rename in `move_directory` -- whose failure *means* "different
  volume" -- is not slowed down by that wait;
- a denial that outlasts the retries reaches the user as something to act on,
  with the original error still attached;
- the cleanup that follows a failed install cannot replace the diagnosis with
  its own error, since the same handle also denies the delete.
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from finesub_bootstrap import fsops, resources
from finesub_bootstrap.models import ResourceSpec
from finesub_bootstrap.paths import AppPaths
from finesub_bootstrap.resources import ResourceManager


def _denials(count: int, real=os.replace):
    """An `os.replace` that is denied `count` times, then works."""

    state = {"calls": 0}

    def replace(source, destination):
        state["calls"] += 1
        if state["calls"] <= count:
            raise PermissionError(5, "Access is denied", str(source))
        return real(source, destination)

    return replace, state


def _record_sleeps(monkeypatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(fsops.time, "sleep", slept.append)
    return slept


def test_publishing_rename_waits_out_a_transient_denial(tmp_path, monkeypatch):
    source = tmp_path / "n9.0-latest.staging"
    (source / "bin").mkdir(parents=True)
    (source / "bin" / "ffmpeg.exe").write_bytes(b"fixture")
    destination = tmp_path / "n9.0-latest"

    replace, state = _denials(3)
    monkeypatch.setattr(fsops.os, "replace", replace)
    slept = _record_sleeps(monkeypatch)

    fsops.replace_path(source, destination)

    assert state["calls"] == 4
    assert (destination / "bin" / "ffmpeg.exe").read_bytes() == b"fixture"
    assert not source.exists()
    assert slept == [
        pytest.approx(fsops.PUBLISH_REPLACE.backoff_seconds * attempt) for attempt in (1, 2, 3)
    ]


def test_the_wait_is_bounded_and_the_original_error_survives(tmp_path, monkeypatch):
    source = tmp_path / "staging"
    source.mkdir()
    replace, state = _denials(fsops.PUBLISH_REPLACE.attempts)
    monkeypatch.setattr(fsops.os, "replace", replace)
    slept = _record_sleeps(monkeypatch)

    with pytest.raises(PermissionError) as failure:
        fsops.replace_path(source, tmp_path / "final")

    assert failure.value.errno == 5
    assert state["calls"] == fsops.PUBLISH_REPLACE.attempts
    assert len(slept) == fsops.PUBLISH_REPLACE.attempts - 1
    assert max(slept) <= fsops.PUBLISH_REPLACE.cap_seconds


def test_the_cross_volume_probe_is_not_retried(tmp_path, monkeypatch):
    """`move_directory` reads one failed rename as "different volume".

    Retrying it would spend the whole backoff budget on every cross-volume
    move, which is the case that is *supposed* to fall through to the copy.
    """

    source = tmp_path / "models"
    source.mkdir()
    (source / "weights.bin").write_bytes(b"fixture")
    destination = tmp_path / "moved" / "models"

    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def replace(from_path, to_path):
        calls.append((str(from_path), str(to_path)))
        if Path(from_path) == source:
            raise OSError(18, "Invalid cross-device link")
        return real_replace(from_path, to_path)

    monkeypatch.setattr(fsops.os, "replace", replace)
    monkeypatch.setattr(fsops.time, "sleep", lambda _: pytest.fail("probe was retried"))

    placed, leftover = fsops.move_directory(source, destination)

    assert placed and leftover == source
    assert (destination / "weights.bin").read_bytes() == b"fixture"
    assert [call for call in calls if call[0] == str(source)] == [
        (str(source), str(destination))
    ]


def _ffmpeg_manager(tmp_path, monkeypatch) -> tuple[ResourceManager, Path]:
    """A one-resource manager whose download is a local zip, not the network."""

    paths = AppPaths.for_root(tmp_path / "install", data_root=tmp_path / "data")
    spec = ResourceSpec(
        id="ffmpeg",
        version="n9.0-latest",
        destination="runtime",
        directory="ffmpeg",
        archive_type="zip",
        required_files=["bin/ffmpeg.exe"],
        asset={"url": "https://example.invalid/ffmpeg.zip", "size": 3, "sha256": "0" * 64},
    )

    def download_asset(asset, destination, progress, should_pause=None):
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, "w") as archive:
            archive.writestr("bin/ffmpeg.exe", "fixture")
        return destination

    monkeypatch.setattr(resources, "download_asset", download_asset)
    manager = ResourceManager(paths, [spec])
    return manager, manager.install_path("ffmpeg")


def test_a_denied_activation_reaches_the_user_as_something_to_act_on(
    tmp_path, monkeypatch
):
    manager, final = _ffmpeg_manager(tmp_path, monkeypatch)
    replace, _ = _denials(fsops.PUBLISH_REPLACE.attempts)
    monkeypatch.setattr(fsops.os, "replace", replace)
    monkeypatch.setattr(fsops.time, "sleep", lambda _: None)

    with pytest.raises(RuntimeError) as failure:
        manager.install("ffmpeg", lambda _: None)

    message = str(failure.value)
    assert "ffmpeg" in message and "杀毒软件" in message
    assert "Access is denied" in message
    assert isinstance(failure.value.__cause__, PermissionError)
    assert manager.active_version("ffmpeg") is None
    assert not final.exists()


def test_a_transient_denial_still_installs(tmp_path, monkeypatch):
    manager, final = _ffmpeg_manager(tmp_path, monkeypatch)
    replace, _ = _denials(2)
    monkeypatch.setattr(fsops.os, "replace", replace)
    monkeypatch.setattr(fsops.time, "sleep", lambda _: None)

    status = manager.install("ffmpeg", lambda _: None)

    assert status.state == "ready"
    assert manager.active_version("ffmpeg") == "n9.0-latest"
    assert (final / "bin" / "ffmpeg.exe").read_text() == "fixture"


def test_cleanup_cannot_replace_the_diagnosis_with_its_own_error(
    tmp_path, monkeypatch
):
    """The handle that denies the rename denies the delete that follows."""

    manager, _ = _ffmpeg_manager(tmp_path, monkeypatch)
    replace, _ = _denials(fsops.PUBLISH_REPLACE.attempts)
    monkeypatch.setattr(fsops.os, "replace", replace)
    monkeypatch.setattr(fsops.time, "sleep", lambda _: None)

    real_remove_tree = resources.remove_tree

    def remove_tree(path):
        if Path(path).name.endswith(".staging") and Path(path).is_dir():
            raise PermissionError(32, "The process cannot access the file")
        return real_remove_tree(path)

    monkeypatch.setattr(resources, "remove_tree", remove_tree)

    with pytest.raises(RuntimeError, match="杀毒软件"):
        manager.install("ffmpeg", lambda _: None)


def test_a_small_record_survives_a_denied_swap_on_a_shorter_budget(
    tmp_path, monkeypatch
):
    """`write_atomic` loses the same race, and there it fails a whole task.

    Its budget is deliberately smaller than the tree's: a record is rewritten
    on every status update, so a name that stays locked must not make each
    later write pay the full backoff.
    """

    assert fsops.RECORD_REPLACE.attempts < fsops.PUBLISH_REPLACE.attempts
    replace, state = _denials(2)
    monkeypatch.setattr(fsops.os, "replace", replace)
    slept = _record_sleeps(monkeypatch)

    fsops.write_atomic(tmp_path / "snapshot.json", '{"state": "running"}')

    assert (tmp_path / "snapshot.json").read_text(encoding="utf-8") == '{"state": "running"}'
    assert state["calls"] == 3
    assert len(slept) == 2
    assert not (tmp_path / "snapshot.json.tmp").exists()


def test_a_record_whose_name_stays_locked_still_fails(tmp_path, monkeypatch):
    replace, state = _denials(fsops.RECORD_REPLACE.attempts)
    monkeypatch.setattr(fsops.os, "replace", replace)
    _record_sleeps(monkeypatch)

    with pytest.raises(PermissionError):
        fsops.write_atomic(tmp_path / "snapshot.json", "{}")

    assert state["calls"] == fsops.RECORD_REPLACE.attempts
    # The half-written temp file is still cleaned up, as it was before.
    assert not (tmp_path / "snapshot.json.tmp").exists()


def test_no_publishing_replace_in_the_install_path_stayed_bare():
    """New publish points must not quietly reintroduce the failure.

    Every `os.replace` left in these modules is one whose failure carries
    information rather than one that publishes a finished file, and each is
    listed here with the reason it stays bare.
    """

    allowed = {
        # Two of these, and neither publishes: the call inside `replace_path`
        # itself, and `move_directory`'s probe, whose failure *means*
        # "different volume" and is the signal to fall through to the copy.
        ("fsops.py", "os.replace(source, destination)"),
        # Moving a bad download aside on a digest mismatch: a failure path.
        ("downloader.py", "os.replace(part_path, quarantine)"),
    }
    root = Path(__file__).resolve().parents[1] / "third_party/finesub/src/finesub_bootstrap"
    found = {
        (module.name, line.strip())
        for module in (root / "fsops.py", root / "resources.py", root / "downloader.py")
        for line in module.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("os.replace(")
    }
    assert found <= allowed, f"a publishing replace stayed bare: {sorted(found - allowed)}"
