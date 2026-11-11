"""`nonoka_x.runtime.fsops`: atomic writes, locks, directory links, process trees."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nonoka_x.runtime import fsops  # noqa: E402

WINDOWS = os.name == "nt"


def test_write_atomic_replaces_in_one_step_and_leaves_no_temporary(tmp_path):
    record = tmp_path / "nested" / "state.json"

    fsops.write_atomic(record, "first\n")
    fsops.write_atomic(record, "second\n")

    assert record.read_text(encoding="utf-8") == "second\n"
    assert sorted(path.name for path in record.parent.iterdir()) == ["state.json"]


def test_write_atomic_keeps_the_old_content_when_the_write_fails(tmp_path, monkeypatch):
    record = tmp_path / "state.json"
    record.write_text("old", encoding="utf-8")

    def refuse(*args, **kwargs):
        raise OSError("denied")

    monkeypatch.setattr(fsops, "replace_path", refuse)
    with pytest.raises(OSError):
        fsops.write_atomic(record, "new")

    assert record.read_text(encoding="utf-8") == "old"
    assert not (tmp_path / "state.json.tmp").exists()


def test_write_atomic_keeps_the_line_endings_asked_for(tmp_path):
    record = tmp_path / "config.toml"

    fsops.write_atomic(record, "a = 1\r\nb = 2\r\n", newline="")

    assert record.read_bytes() == b"a = 1\r\nb = 2\r\n"


def test_replace_path_waits_out_a_brief_holder(tmp_path, monkeypatch):
    attempts: list[int] = []
    real = os.replace

    def flaky(source, destination):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError("[WinError 5] held by a scanner")
        real(source, destination)

    source = tmp_path / "a"
    source.write_text("x", encoding="utf-8")
    monkeypatch.setattr(fsops.os, "replace", flaky)
    monkeypatch.setattr(fsops.time, "sleep", lambda _seconds: None)

    fsops.replace_path(source, tmp_path / "b", budget=fsops.ReplaceBudget(attempts=4, backoff_seconds=0, cap_seconds=0))

    assert (tmp_path / "b").read_text(encoding="utf-8") == "x"
    assert len(attempts) == 3


def test_a_held_lock_times_out_for_a_second_holder(tmp_path):
    lock = tmp_path / "x.lock"

    with fsops.holding_lock(lock, lease={"pid": os.getpid()}):
        assert fsops.read_lease(lock) == {"pid": os.getpid()}
        assert fsops.try_lock(lock) is False
        started = time.monotonic()
        with pytest.raises(fsops.LockUnavailable):
            with fsops.holding_lock(lock, timeout=0.2):
                pass
        assert time.monotonic() - started < 5

    assert fsops.try_lock(lock) is True
    assert fsops.read_lease(lock) is None
    assert lock.exists()  # The sidecar is never deleted.


def test_a_lock_is_shared_across_processes(tmp_path):
    lock = tmp_path / "x.lock"
    script = (
        "import sys; sys.path.insert(0, sys.argv[2]);"
        "from pathlib import Path; from nonoka_x.runtime import fsops;"
        "print(fsops.try_lock(Path(sys.argv[1])))"
    )
    source = str(Path(__file__).resolve().parents[1] / "src")

    with fsops.holding_lock(lock):
        held = subprocess.run([sys.executable, "-c", script, str(lock), source], capture_output=True, text=True)
    free = subprocess.run([sys.executable, "-c", script, str(lock), source], capture_output=True, text=True)

    assert held.stdout.strip() == "False"
    assert free.stdout.strip() == "True"


def test_a_pause_while_waiting_for_a_lock_is_raised(tmp_path):
    lock = tmp_path / "x.lock"

    class Paused(Exception):
        pass

    with fsops.holding_lock(lock):
        with pytest.raises(Paused):
            with fsops.holding_lock(lock, should_pause=lambda: True, on_pause=Paused):
                pass


def _junction(link: Path, target: Path) -> None:
    if WINDOWS:
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    else:
        os.symlink(target, link, target_is_directory=True)


def test_remove_tree_never_follows_a_directory_link(tmp_path):
    outside = tmp_path / "someone-elses"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    owned = tmp_path / "ours"
    (owned / "deep").mkdir(parents=True)
    (owned / "deep" / "file.bin").write_bytes(b"x")
    _junction(owned / "deep" / "redirect", outside)

    assert fsops.is_directory_link(owned / "deep" / "redirect")
    assert not fsops.is_directory_link(owned / "deep")
    fsops.remove_tree(owned)

    assert not owned.exists()
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_remove_tree_of_a_link_removes_only_the_link(tmp_path):
    outside = tmp_path / "target"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    link = tmp_path / "link"
    _junction(link, outside)

    fsops.remove_tree(link)

    assert not os.path.lexists(link)
    assert (outside / "keep.txt").exists()


def test_move_tree_keeps_a_nested_link_as_a_link(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big.bin").write_bytes(b"x" * 10)
    source = tmp_path / "source"
    (source / "sub").mkdir(parents=True)
    (source / "sub" / "a.txt").write_text("a", encoding="utf-8")
    _junction(source / "models", outside)
    destination = tmp_path / "moved here 空格" / "tree"

    fsops.move_tree(source, destination)

    assert not source.exists()
    assert (destination / "sub" / "a.txt").read_text(encoding="utf-8") == "a"
    assert fsops.is_directory_link(destination / "models")
    assert (outside / "big.bin").exists()


def test_move_directory_does_not_merge_into_a_populated_destination(tmp_path):
    source = tmp_path / "a"
    source.mkdir()
    (source / "x").write_text("x", encoding="utf-8")
    destination = tmp_path / "b"
    destination.mkdir()
    (destination / "y").write_text("y", encoding="utf-8")

    assert fsops.move_directory(source, destination) == (False, None)
    assert (source / "x").exists() and not (destination / "x").exists()


def test_terminate_process_tree_kills_the_child_and_its_children(tmp_path):
    marker = tmp_path / "grandchild.pid"
    grandchild = "import time; time.sleep(60)"
    child = (
        "import subprocess, sys, time;"
        f"p = subprocess.Popen([sys.executable, '-c', {grandchild!r}]);"
        f"open({str(marker)!r}, 'w').write(str(p.pid));"
        "time.sleep(60)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", child],
        creationflags=fsops.no_window(),
        start_new_session=not WINDOWS,
    )
    deadline = time.monotonic() + 20
    while not marker.exists() or not marker.read_text():
        assert time.monotonic() < deadline, "the child never started its own child"
        time.sleep(0.05)
    grandchild_pid = int(marker.read_text())

    fsops.terminate_process_tree(process)
    process.wait(timeout=20)

    assert not _alive(grandchild_pid)


def _alive(pid: int) -> bool:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if WINDOWS:
            listed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, creationflags=fsops.no_window()
            ).stdout
            if str(pid) not in listed:
                return False
        else:
            try:
                os.kill(pid, 0)
            except OSError:
                return False
        time.sleep(0.1)
    return True


def test_which_ignores_a_copy_found_only_in_the_working_directory(tmp_path, monkeypatch):
    if not WINDOWS:
        pytest.skip("only Windows searches the working directory first")
    (tmp_path / "nonoka-probe-tool.exe").write_bytes(b"")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path.parent))

    assert fsops.which("nonoka-probe-tool") is None


def test_no_window_is_windows_only():
    assert (fsops.no_window() != 0) is WINDOWS
