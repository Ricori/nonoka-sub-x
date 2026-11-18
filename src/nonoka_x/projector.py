"""Project a task's result in Nonoka's formats into the editor's stable EditDocument schema.

Two inputs, both Nonoka's (WP-L, plan 4.8 item 38; `docs/provider-spec.md`):

- `correction.json`, when a correction engine ran: its lines are the
  subtitles, already lined up by the engine (`source` -> `ja`, `translation`
  -> `zh`, `speaker` -> `spk`, `low_confidence` -> `low_conf`, the engine's
  `words` as they are). Copied line by line.
- `transcript.json` otherwise -- a transcription that no correction engine
  took further: one subtitle per line, its text as `ja`.

What follows the projection (an imported axis, the speaker tracks, the hidden
source lane of a correct-only run) is the provider's, on the document this
returns.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .correction.document import CorrectionDocumentError, read_document
from .transcription.model import TranscriptError, read_transcript


class ProjectionError(ValueError):
    """A task's result cannot be projected into a document."""


#: A line whose two timestamps coincide occupies no time, which everything
#: downstream -- lane seating in `axis.py`, the editor's timeline, the ASS
#: renderer -- assumes it does; it is widened to this floor instead of
#: rejected (a correction engine repairs its own lines the same way).
MIN_CUE_SECONDS = 0.04


def _repaired_end(start: float, end: float, following: float | None) -> float:
    """The end this line needs to occupy time, without displacing the next one."""

    if end > start:
        return end
    widened = start + MIN_CUE_SECONDS
    if following is not None and start < following < widened:
        return following
    return widened


def hide_source_lanes(projection: Mapping[str, Any]) -> dict[str, Any]:
    """Start every source lane hidden, so the document shows and exports the subtitle the run delivered.

    For a correct-only run, whose subtitle is the polished lane alone. Run after
    the speaker split and the axis conform, which rebuild `track_meta` and the
    tracks; they keep the source lane visible for a run with no second lane.
    """

    def hidden(lane: Any) -> dict[str, Any]:
        return {**(lane if isinstance(lane, Mapping) else {}), "hidden": True}

    meta = dict(projection.get("track_meta") or {})
    meta["ja"] = hidden(meta.get("ja"))
    return {
        **projection,
        "track_meta": meta,
        "tracks": [{**track, "ja": hidden(track.get("ja"))} for track in projection.get("tracks") or []],
    }


def _from_correction(path: Path) -> tuple[list[dict[str, Any]], str, bool]:
    try:
        document = read_document(path)
    except CorrectionDocumentError as exc:
        raise ProjectionError(str(exc)) from exc
    subtitles: list[dict[str, Any]] = []
    for line in document["lines"]:
        item: dict[str, Any] = {
            "t0": float(line["start"]),
            "t1": float(line["end"]),
            "ja": line["source"],
            "zh": line.get("translation", ""),
        }
        if line.get("words"):
            item["words"] = list(line["words"])
        if line.get("low_confidence"):
            item["low_conf"] = True
        if line.get("speaker"):
            item["spk"] = line["speaker"]
        subtitles.append(item)
    correct_only = document["mode"] == "final" and document.get("correct_only") is True
    return subtitles, document["mode"], correct_only


def _from_transcript(path: Path) -> list[dict[str, Any]]:
    try:
        document = read_transcript(path)
    except (OSError, TranscriptError) as exc:
        raise ProjectionError(f"cannot read transcript.json: {exc}") from exc
    lines = list(document.lines)
    subtitles: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        following = lines[index + 1].start if index + 1 < len(lines) else None
        item: dict[str, Any] = {
            "t0": line.start,
            "t1": _repaired_end(line.start, line.end, following),
            "ja": line.text.strip(),
            "zh": "",
        }
        if line.words:
            item["words"] = [word.to_dict() for word in line.words]
        if line.speaker:
            item["spk"] = line.speaker
        subtitles.append(item)
    return subtitles


def project_edit_document(
    *,
    transcript_json: str | Path | None = None,
    correction_json: str | Path | None = None,
    video_id: str,
    title: str,
    source: str = "",
    fingerprint: str | None = None,
) -> dict[str, Any]:
    """An editor-compatible document from a task's `correction.json`, else its `transcript.json`.

    A correct-only run (`correction.json` says so) fills the same two fields
    with other things: `ja` is the faithful transcript and `zh` its polish, and
    `projection.correct_only` tells the editor to name them so.
    """

    if correction_json is not None:
        subtitles, mode, correct_only = _from_correction(Path(correction_json))
    elif transcript_json is not None:
        subtitles, mode, correct_only = _from_transcript(Path(transcript_json)), "stable", False
    else:
        raise ProjectionError("the task has neither correction.json nor transcript.json")
    return {
        "schema": 1,
        "video_id": video_id,
        "title": title,
        "source": source,
        "fp": fingerprint,
        "rev": 0,
        "subtitles": subtitles,
        "tracks": [],
        "track_meta": {
            "name": "默认轨",
            "ja": {"hidden": False, "style": "origin"},
            "zh": {"hidden": False, "style": "cn"},
        },
        "effects": [],
        "projection": {"schema": 1, "mode": mode, **({"correct_only": True} if correct_only else {})},
    }
