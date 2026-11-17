"""The raw SRT, rendered by the core from a `transcript.json` (plan 4.6 item 33; appendix B question 45 item 6).

Only for a task without a correction package installed: with one, the raw
SRT is the correction engine's, whichever engine transcribed (WP-P1,
question 10), so every result made with the packages in place stays as it
is. Without one, a transcription engine still delivers subtitles: one cue per
line of the transcript, in its order and with its times and text, nothing
merged, split or corrected.
"""

from __future__ import annotations

from pathlib import Path

from .model import TranscriptDocument, read_transcript

#: The file the core writes in the task directory.
RAW_SRT_FILENAME = "raw.srt"


def srt_timestamp(seconds: float) -> str:
    """`HH:MM:SS,mmm`, to the nearest millisecond."""

    total = max(0, round(seconds * 1000))
    hours, rest = divmod(total, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    whole, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole:02d},{millis:03d}"


def render_srt(document: TranscriptDocument) -> str:
    """One cue per line that has text; numbered from 1, separated by a blank line."""

    cues = []
    for line in document.lines:
        text = "\n".join(part.strip() for part in line.text.strip().splitlines() if part.strip())
        if not text:
            continue
        cues.append(f"{len(cues) + 1}\n{srt_timestamp(line.start)} --> {srt_timestamp(line.end)}\n{text}\n")
    return "\n".join(cues)


def write_raw_srt(transcript_json: str | Path, path: str | Path) -> Path:
    """Render `transcript_json` to `path` (UTF-8, LF), replacing what was there."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(render_srt(read_transcript(transcript_json)), encoding="utf-8", newline="\n")
    temporary.replace(target)
    return target


__all__ = ["RAW_SRT_FILENAME", "render_srt", "srt_timestamp", "write_raw_srt"]
