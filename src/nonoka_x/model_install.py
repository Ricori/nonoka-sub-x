"""Install one pinned FineSub model inside the managed runtime.

The installer runs in a child process so Hugging Face can select a regional
endpoint before importing ``huggingface_hub``. Machine-readable events are
written as one JSON object per line for the desktop progress UI.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Callable


EventSink = Callable[[dict[str, Any]], None]
PIPELINE_MODEL_IDS = ("separator", "whisper", "qwen-referee")
#: Speaker diarization's default backend (FineSub 0.6.0 `nemotron3_backend`'s
#: `HOSTED_MODEL` / `HOSTED_EMBEDDING`). Optional: only a run that asks for
#: diarization needs them, so they are never part of the required set.
DIARIZATION_MODEL_IDS = ("nemotron3", "speaker-embedding")
INSTALLABLE_MODEL_IDS = (*PIPELINE_MODEL_IDS, *DIARIZATION_MODEL_IDS)


def emit_event(event: dict[str, Any]) -> None:
    # Keep the process protocol ASCII-only. On Windows, a child attached to a
    # pipe may otherwise encode Chinese with the active ANSI code page while
    # the parent correctly expects UTF-8, producing replacement characters in
    # an otherwise valid progress event. json.loads restores the Chinese text.
    print(json.dumps(event, ensure_ascii=True, separators=(",", ":")), flush=True)


def _cached_manifest_file_present(path: Path, expected) -> bool:
    """Check a verified Hub cache link without re-hashing gigabytes on every poll."""

    try:
        if not path.is_file() or path.stat().st_size != expected.size:
            return False
        target = path.resolve(strict=True)
    except OSError:
        return False
    # Hugging Face snapshots normally link into a content-addressed blob. If
    # they do, the blob name gives us the digest check for free. On systems
    # where symlinks are unavailable the Hub copies files into the snapshot;
    # those were fully hashed by install_model before this status check.
    return target.parent.name != "blobs" or target.name == expected.sha256


def _fixed_manifest_file_present(path: Path, expected) -> bool:
    try:
        return path.is_file() and (not expected.is_verifiable or path.stat().st_size == expected.size)
    except OSError:
        return False


def missing_managed_models(
    models_root: Path, model_ids: tuple[str, ...] = PIPELINE_MODEL_IDS
) -> tuple[str, ...]:
    """Return missing models using the same managed cache paths Nonoka X runs.

    The shared FineSub helper may deliberately choose a conventional user HF
    cache when it finds any repository there. Nonoka X workers, however, set
    ``HF_HOME`` to this managed directory explicitly. Looking in a different
    cache made completed downloads appear missing. We also inspect the pinned
    snapshot files rather than rejecting a valid model because Xet left an
    unrelated, uniquely suffixed ``*.incomplete`` file from an older attempt.
    """

    from finesub_bootstrap.model_manifest import entry_for

    missing: list[str] = []
    for model_id in model_ids:
        entry = entry_for(model_id)
        if entry is None:
            missing.append(model_id)
            continue
        if model_id == "separator":
            directory = models_root / "audio-separator"
            present = bool(entry.files) and all(
                _fixed_manifest_file_present(directory / wanted.name, wanted)
                for wanted in entry.files
            )
        else:
            repository = f"models--{entry.repo.replace('/', '--')}"
            snapshot = models_root / "huggingface" / "hub" / repository / "snapshots" / entry.revision
            present = bool(entry.files) and all(
                _cached_manifest_file_present(snapshot / wanted.name, wanted)
                for wanted in entry.files
                if wanted.is_verifiable
            )
        if not present:
            missing.append(model_id)
    return tuple(missing)


class ByteProgressReporter:
    """Throttle byte events and calculate a recent transfer rate."""

    def __init__(self, model_id: str, total: int, sink: EventSink) -> None:
        self.model_id = model_id
        self.total = max(0, int(total))
        self.sink = sink
        self._last_time = time.monotonic()
        self._last_bytes = 0
        self._last_emit = 0.0

    def update(self, completed: int, *, force: bool = False) -> None:
        now = time.monotonic()
        completed = max(0, int(completed))
        if self.total:
            completed = min(completed, self.total)
        if not force and completed < self.total and now - self._last_emit < 0.1:
            return
        elapsed = max(now - self._last_time, 1e-6)
        speed = max(0.0, (completed - self._last_bytes) / elapsed)
        self.sink({
            "type": "progress",
            "resource": self.model_id,
            "completed": completed,
            "total": self.total,
            "unit": "bytes",
            "bytes_per_second": speed,
            "message": f"正在下载模型：{self.model_id}",
        })
        self._last_time = now
        self._last_bytes = completed
        self._last_emit = now


class _SilentStream:
    """A file-like sink that prevents tqdm control characters in NDJSON."""

    def write(self, _value: str) -> int:
        return 0

    def flush(self) -> None:
        return None


def reporting_tqdm(reporter: ByteProgressReporter):
    """Build a tqdm subclass that reports Hugging Face's aggregate byte bar."""

    from tqdm.auto import tqdm

    class ReportingTqdm(tqdm):
        def __init__(self, *args, **kwargs) -> None:
            self._reports_bytes = str(kwargs.get("desc", "")).startswith("Downloading bytes")
            kwargs["file"] = _SilentStream()
            kwargs["disable"] = False
            super().__init__(*args, **kwargs)
            self._reports_bytes = str(getattr(self, "desc", "")).startswith("Downloading bytes")

        def update(self, n: int | float = 1):
            displayed = super().update(n)
            if getattr(self, "_reports_bytes", False):
                reporter.update(int(self.n))
            return displayed

        def close(self) -> None:
            if getattr(self, "_reports_bytes", False):
                reporter.update(int(self.n), force=True)
            super().close()

    return ReportingTqdm


def _stage(sink: EventSink, model_id: str, stage: str, message: str) -> None:
    sink({"type": "stage", "resource": model_id, "stage": stage, "message": message})


def _install_separator(entry, models_root: Path, data_root: Path, sink: EventSink) -> None:
    from finesub_bootstrap.download_routes import resolve_region
    from finesub_bootstrap.model_fetch import fetch_fixed_files

    total = sum(max(0, wanted.size) for wanted in entry.files if wanted.is_verifiable)
    reporter = ByteProgressReporter("separator", total, sink)
    completed_before = 0
    previous_downloaded = 0
    previous_total = 0

    def progress(value) -> None:
        nonlocal completed_before, previous_downloaded, previous_total
        downloaded = max(0, int(value.downloaded))
        item_total = max(0, int(value.total))
        if previous_total and (downloaded < previous_downloaded or item_total != previous_total):
            completed_before += previous_total
        previous_downloaded = downloaded
        previous_total = item_total
        reporter.update(completed_before + downloaded)

    _stage(sink, "separator", "downloading", "正在下载音频分离模型")
    fetch_fixed_files(
        entry,
        models_root / "audio-separator",
        data_root=data_root,
        region=resolve_region(data_root).region,
        progress=progress,
    )
    reporter.update(total, force=True)
    _stage(sink, "separator", "verifying", "正在校验音频分离模型")


def _install_huggingface(entry, model_id: str, models_root: Path, sink: EventSink) -> Path:
    from huggingface_hub import snapshot_download

    arguments = {
        "repo_id": entry.repo,
        "revision": entry.revision,
        "cache_dir": models_root / "huggingface" / "hub",
        # A `listedOnly` entry pins a few files out of a larger repository
        # (Nemotron's ships training checkpoints beside the one the backend
        # reads); without this the whole repository would be fetched.
        "allow_patterns": (
            [wanted.name for wanted in entry.files] if getattr(entry, "listed_only", False) else None
        ),
    }
    _stage(sink, model_id, "preparing", f"正在检查 {model_id} 模型缓存")
    dry_run = snapshot_download(**arguments, dry_run=True)
    total = sum(
        max(0, int(getattr(item, "file_size", 0) or 0))
        for item in dry_run
        if bool(getattr(item, "will_download", False))
    )
    reporter = ByteProgressReporter(model_id, total, sink)
    if total:
        reporter.update(0, force=True)
        _stage(sink, model_id, "downloading", f"正在下载模型：{model_id}")
    snapshot = Path(snapshot_download(**arguments, tqdm_class=reporting_tqdm(reporter)))
    reporter.update(total, force=True)
    return snapshot


def install_model(model_id: str, models_root: Path, data_root: Path, *, sink: EventSink = emit_event) -> None:
    from finesub_bootstrap.model_manifest import entry_for, file_matches

    entry = entry_for(model_id)
    if entry is None:
        raise RuntimeError(f"FineSub model manifest has no {model_id!r} entry")
    if model_id == "separator":
        _install_separator(entry, models_root, data_root, sink)
        _stage(sink, model_id, "completed", f"模型 {model_id} 已下载并校验")
        return

    if not entry.repo or not entry.revision:
        raise RuntimeError(f"FineSub model {model_id!r} has no pinned repository")
    snapshot = _install_huggingface(entry, model_id, models_root, sink)
    _stage(sink, model_id, "verifying", f"正在校验模型：{model_id}")
    mismatches = [wanted.name for wanted in entry.files if wanted.is_verifiable and not file_matches(snapshot / wanted.name, wanted)]
    if mismatches:
        raise RuntimeError(f"FineSub model {model_id!r} failed manifest verification: {', '.join(mismatches)}")
    _stage(sink, model_id, "completed", f"模型 {model_id} 已下载并校验")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, choices=INSTALLABLE_MODEL_IDS)
    parser.add_argument("--models-root", required=True, type=Path)
    parser.add_argument("--data-root", required=True, type=Path)
    args = parser.parse_args(argv)
    from nonoka_x.download_route import install_proxy_route_policy

    install_proxy_route_policy()
    install_model(args.model, args.models_root.expanduser().resolve(), args.data_root.expanduser().resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
