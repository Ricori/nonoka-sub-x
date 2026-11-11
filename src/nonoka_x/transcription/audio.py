"""The media work of the cloud ASR engines: the source's duration, the upload clip, the 16 kHz PCM.

Replaces what the moved backends took from FineSub's `finesub/media/ffmpeg.py` (WP1). The commands are copied
unchanged: the upload clip is `extract_upload_mp3` (`ffmpeg.py:328`, 16 kHz mono 64 kbps MP3, cut with `-ss`
and `-t` before `-i`), the PCM `decode_pcm16k`, the duration `probe_media_duration`.

ffmpeg and ffprobe come from Nonoka's managed tools through the `nonoka_x.runtime` facade, never from `PATH`:
the engine hands its `RuntimeManager` over once (`use_runtime`), or a call names the executable itself.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import threading
from typing import Any

from nonoka_x.runtime.fsops import no_window

_runtime: Any | None = None
_runtime_lock = threading.Lock()


class MediaToolMissing(RuntimeError):
    """ffmpeg or ffprobe is not installed (or no runtime was handed over)."""


def use_runtime(manager: Any | None) -> None:
    """Take ffmpeg and ffprobe -- and the optional repairs their models (`enhance.model_dir`) -- from `manager`
    (a `nonoka_x.runtime.RuntimeManager`) from now on; None forgets it."""

    global _runtime
    with _runtime_lock:
        _runtime = manager


def current_runtime() -> Any | None:
    with _runtime_lock:
        return _runtime


def tool(name: str, explicit: str | Path | None = None) -> str:
    """The executable `name` (`ffmpeg` or `ffprobe`): `explicit`, else the managed one."""

    if explicit:
        return str(explicit)
    with _runtime_lock:
        manager = _runtime
    if manager is None:
        raise MediaToolMissing(f"no runtime was handed to the cloud ASR engine, so {name} cannot be found")
    path = manager.tool_path(name)
    if path is None:
        raise MediaToolMissing(f"{name} is not installed (the media tools of the runtime)")
    return str(path)


def _format_seconds(seconds: float) -> str:
    return f"{max(0.0, seconds):.3f}"


def _run_capture(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a media helper and capture its output as UTF-8 text (FineSub `ffmpeg.run_capture`: the machine's ANSI
    code page would fail on a track title's UTF-8)."""

    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=no_window(),
    )


def _run_ffmpeg(args: list[str]) -> None:
    result = _run_capture(args)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"ffmpeg failed (exit {result.returncode}): {detail}")


def _clip_duration(clip_start: float, clip_end: float) -> float:
    duration = clip_end - clip_start
    if duration <= 0:
        raise ValueError(f"Empty clip range [{clip_start:.2f}, {clip_end:.2f}]")
    return duration


def probe_media_duration(media_path: str | Path, *, ffprobe: str | Path | None = None) -> float:
    """Media duration in seconds via ffprobe."""

    result = _run_capture([
        tool("ffprobe", ffprobe), "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(media_path),
    ])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"ffprobe failed (exit {result.returncode}): {detail}")
    text = (result.stdout or "").strip()
    if not text:
        raise RuntimeError(f"ffprobe returned no duration for {media_path}")
    return float(text)


def upload_mp3_command(ffmpeg: str, input_path: str | Path, clip_start: float, clip_end: float,
                       out_path: str | Path) -> list[str]:
    """The ffmpeg command line of `extract_upload_mp3` (FineSub `ffmpeg.py:328`, unchanged)."""

    duration = _clip_duration(clip_start, clip_end)
    return [
        ffmpeg, "-y", "-nostdin", "-loglevel", "error",
        "-ss", _format_seconds(clip_start), "-t", _format_seconds(duration),
        "-i", str(input_path), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", str(out_path),
    ]


def extract_upload_mp3(
    input_path: str | Path,
    clip_start: float,
    clip_end: float,
    out_path: str | Path,
    *,
    ffmpeg: str | Path | None = None,
) -> Path:
    """A span of the source as the cloud ASR backends upload it: 16 kHz mono 64 kbps MP3."""

    _clip_duration(clip_start, clip_end)
    ffmpeg_bin = tool("ffmpeg", ffmpeg)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_name(out.stem + ".part" + out.suffix)
    _run_ffmpeg(upload_mp3_command(ffmpeg_bin, input_path, clip_start, clip_end, temporary))
    temporary.replace(out)
    return out


def decode_pcm16k(input_path: str | Path, *, ffmpeg: str | Path | None = None, sample_format: str = "s16le") -> bytes:
    """The whole source as 16 kHz mono PCM: signed 16-bit little-endian, or `f32le` (float32)."""

    result = subprocess.run(
        [tool("ffmpeg", ffmpeg), "-nostdin", "-loglevel", "error", "-i", str(input_path),
         "-vn", "-ac", "1", "-ar", "16000", "-f", sample_format, "-"],
        capture_output=True,
        creationflags=no_window(),
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffmpeg failed (exit {result.returncode}): {detail}")
    return result.stdout


__all__ = [
    "MediaToolMissing",
    "current_runtime",
    "decode_pcm16k",
    "extract_upload_mp3",
    "probe_media_duration",
    "tool",
    "upload_mp3_command",
    "use_runtime",
]
