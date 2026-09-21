"""Atomic local EditDocument storage with optimistic revisions and history."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


class DocumentError(RuntimeError):
    pass


class DocumentNotFound(DocumentError):
    pass


class RevisionConflict(DocumentError):
    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"document revision conflict: expected {expected}, current {actual}")
        self.expected = expected
        self.actual = actual


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


#: Windows denies `os.replace` while anything holds either name open, and
#: Python's `open` does not share deletion -- so a reader of `snapshot.json`
#: denies the writer. The desktop UI polls task status while the worker
#: writes it, which turned a record that had been written correctly into
#: `[WinError 5] 拒绝访问` and failed the whole task. A reader is gone in
#: milliseconds, so the swap waits for it rather than giving up; the wait
#: stays short because these records are rewritten on every status update.
_REPLACE_ATTEMPTS = 4
_REPLACE_BACKOFF_SECONDS = 0.05


def _replace_when_free(temporary: Path, path: Path) -> None:
    for attempt in range(1, _REPLACE_ATTEMPTS + 1):
        try:
            os.replace(temporary, path)
            return
        except OSError:
            if attempt == _REPLACE_ATTEMPTS:
                raise
            time.sleep(_REPLACE_BACKOFF_SECONDS * attempt)


#: The same denial, seen from the other side. `os.replace` is atomic in that a
#: reader sees the old record or the new one, never a torn one -- but on
#: Windows the name itself is briefly unopenable while the swap runs, and a
#: reader that arrives in that instant is denied rather than made to wait.
#: `LocalProvider.status` reads `snapshot.json` from the worker-reader thread
#: and from the desktop UI's poll while the worker rewrites it on every event,
#: so that instant is hit often: it failed a task with `[Errno 13] Permission
#: denied` on a record that was perfectly readable a millisecond later.
#: The budget outlasts the writer's, so a reader racing a writer that is
#: itself retrying still wins rather than both giving up together.
_READ_ATTEMPTS = _REPLACE_ATTEMPTS + 1
_READ_BACKOFF_SECONDS = _REPLACE_BACKOFF_SECONDS


def _read_json_when_free(path: Path) -> Any:
    """Read a record written by `_atomic_json`, waiting out a racing swap.

    A missing name is not a race and is not retried -- callers turn it into
    "no such task" or "no such document", and that answer must stay immediate.
    """
    for attempt in range(1, _READ_ATTEMPTS + 1):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise
        except OSError:
            if attempt == _READ_ATTEMPTS:
                raise
            time.sleep(_READ_BACKOFF_SECONDS * attempt)


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        _replace_when_free(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class DocumentStore:
    def __init__(self, root: str | Path, *, history_limit: int = 100) -> None:
        self.root = Path(root).expanduser().resolve()
        self.history_limit = max(1, history_limit)
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}

    def _lock(self, video_id: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(video_id, threading.RLock())

    def directory(self, video_id: str) -> Path:
        if not video_id or any(part in {"", ".", ".."} for part in Path(video_id).parts) or len(Path(video_id).parts) != 1:
            raise DocumentError("video_id must be one safe path component")
        return self.root / video_id

    def read(self, video_id: str) -> dict[str, Any]:
        path = self.directory(video_id) / "document.json"
        try:
            value = _read_json_when_free(path)
        except FileNotFoundError as exc:
            raise DocumentNotFound(video_id) from exc
        if not isinstance(value, dict):
            raise DocumentError(f"document {video_id!r} is not a JSON object")
        return value

    def create(
        self,
        video_id: str,
        projection: Mapping[str, Any],
        *,
        artifacts: Mapping[str, Any] | None = None,
        replace_default: bool = False,
    ) -> dict[str, Any]:
        with self._lock(video_id):
            directory = self.directory(video_id)
            path = directory / "document.json"
            incoming = copy.deepcopy(dict(projection))
            incoming["video_id"] = video_id
            incoming.setdefault("created_at", _timestamp())
            incoming["updated_at"] = _timestamp()
            if path.exists():
                if not replace_default:
                    raise DocumentError(f"document {video_id!r} already exists")
                current = self.read(video_id)
                incoming["tracks"] = copy.deepcopy(current.get("tracks", []))
                incoming["track_meta"] = copy.deepcopy(current.get("track_meta", incoming.get("track_meta")))
                incoming["effects"] = copy.deepcopy(current.get("effects", incoming.get("effects", [])))
                # 样式表跟着视频走，重新投影只换字幕内容，不该把用户调好的样式冲掉
                styles = current.get("styles", incoming.get("styles"))
                if styles is not None:
                    incoming["styles"] = styles
                incoming["rev"] = int(current.get("rev", 0)) + 1
                incoming["created_at"] = current.get("created_at", incoming["created_at"])
                self._snapshot(directory, current)
            else:
                incoming["rev"] = 0
                _atomic_json(directory / "original.json", incoming)
            _atomic_json(path, incoming)
            if artifacts is not None:
                _atomic_json(directory / "artifacts.json", artifacts)
            return incoming

    def save(
        self,
        video_id: str,
        *,
        expected_rev: int,
        subtitles: list[dict[str, Any]],
        tracks: list[dict[str, Any]] | None = None,
        track_meta: Mapping[str, Any] | None = None,
        effects: list[dict[str, Any]] | None = None,
        title: str | None = None,
        styles: str | None = None,
    ) -> dict[str, Any]:
        with self._lock(video_id):
            current = self.read(video_id)
            actual = int(current.get("rev", 0))
            if expected_rev != actual:
                raise RevisionConflict(expected_rev, actual)
            self._snapshot(self.directory(video_id), current)
            updated = copy.deepcopy(current)
            updated["subtitles"] = copy.deepcopy(subtitles)
            if tracks is not None:
                updated["tracks"] = copy.deepcopy(tracks)
            if track_meta is not None:
                updated["track_meta"] = copy.deepcopy(dict(track_meta))
            if effects is not None:
                updated["effects"] = copy.deepcopy(effects)
            if title is not None:
                updated["title"] = title
            if styles is not None:
                updated["styles"] = styles
            updated["rev"] = actual + 1
            updated["updated_at"] = _timestamp()
            _atomic_json(self.directory(video_id) / "document.json", updated)
            return updated

    def write_peaks(self, video_id: str, peaks: Mapping[str, Any]) -> None:
        with self._lock(video_id):
            _atomic_json(self.directory(video_id) / "peaks.json", dict(peaks))

    def _snapshot(self, directory: Path, document: Mapping[str, Any]) -> None:
        revision = int(document.get("rev", 0))
        history = directory / "history"
        _atomic_json(history / f"{revision:08d}.json", document)
        snapshots = sorted(history.glob("*.json"))
        for stale in snapshots[: -self.history_limit]:
            stale.unlink()

