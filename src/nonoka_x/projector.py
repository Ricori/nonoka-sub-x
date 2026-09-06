"""Project FineSub artifacts into the editor's stable EditDocument schema."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


class ProjectionError(ValueError):
    """FineSub artifacts cannot be mapped without guessing or data loss."""


#: A cue whose two timestamps coincide is not a mapping ambiguity, so it is
#: repaired rather than rejected. The engine can produce one: profile-1
#: stabilization re-seats a segment's edges on its surviving words, and a
#: segment left with a single word whose ASR timing collapsed comes out with
#: `end == start`; `subtitles/time_order.py` only reports that, it does not
#: rewrite it. Everything downstream -- lane seating in `axis.py`, the editor's
#: timeline, the ASS renderer -- assumes a cue occupies time, and one degenerate
#: row is no reason to throw a finished run away, so the row is widened to this
#: floor instead. Below a frame, it stays where the engine put it.
MIN_CUE_SECONDS = 0.04


def _repaired_end(start: float, end: float, following: float | None) -> float:
    """The end this cue needs to occupy time, without displacing the next one.

    `following` is the next cue's start when there is one: widening stops there
    so repairing one row cannot reorder rows that were already fine.
    """

    if end > start:
        return end
    widened = start + MIN_CUE_SECONDS
    if following is not None and start < following < widened:
        return following
    return widened


@dataclass(frozen=True)
class SrtCue:
    start: float
    end: float
    text: str


_TIMING = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*"
    r"(?P<end>\d{2}:\d{2}:\d{2},\d{3})"
)


def _srt_seconds(value: str) -> float:
    hours, minutes, tail = value.split(":")
    seconds, millis = tail.split(",")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000


def parse_srt(text: str) -> list[SrtCue]:
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").splitlines()
    timings: list[tuple[int, re.Match[str]]] = []
    for line_index, line in enumerate(lines):
        # Cloud output can contain extra separators, prose, or cue ids in
        # unexpected places. Timing lines are the only structural boundary we
        # need; everything between two boundaries can be recovered as text.
        match = _TIMING.match(line.strip())
        if match:
            timings.append((line_index, match))
    if not timings:
        raise ProjectionError("final SRT has no timing lines")

    cues: list[SrtCue] = []
    for timing_index, (line_index, match) in enumerate(timings):
        next_line = timings[timing_index + 1][0] if timing_index + 1 < len(timings) else len(lines)
        body = list(lines[line_index + 1:next_line])
        while body and not body[-1].strip():
            body.pop()
        # The next cue id sits immediately before its timing line, so it lands
        # at the end of this slice. It is metadata, not subtitle text.
        if body and body[-1].strip().isdigit():
            body.pop()
        while body and not body[0].strip():
            body.pop(0)
        start = _srt_seconds(match.group("start"))
        end = _srt_seconds(match.group("end"))
        cues.append(SrtCue(start, end, "\n".join(line for line in body if line.strip()).strip()))
    for index, cue in enumerate(cues):
        if cue.end <= cue.start:
            following = cues[index + 1].start if index + 1 < len(cues) else None
            cues[index] = SrtCue(cue.start, _repaired_end(cue.start, cue.end, following), cue.text)
    return cues


def _decode_cell(value: str) -> str:
    decoded = (value or "").strip().replace(r"\n", "\n")
    return re.sub(r"\n{2,}", "\n", decoded)


def _confidence(value: str) -> str | None:
    normalized = (value or "").strip().lower()
    if normalized in {"high", "median", "low"}:
        return normalized
    try:
        legacy = int(normalized)
    except ValueError:
        return None
    if not 1 <= legacy <= 9:
        return None
    return "high" if legacy >= 7 else "median" if legacy >= 4 else "low"


def parse_annotated(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate((text or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split("|", 8)
        if len(fields) != 9:
            raise ProjectionError(f"annotated.csv line {line_number} does not have 9 columns")
        kind, position, duration, _gap, corrected, translation, conf, _chars, _note = fields
        try:
            float(duration)
        except ValueError as exc:
            raise ProjectionError(
                f"annotated.csv line {line_number} has no numeric duration; regenerate it with this engine"
            ) from exc
        normalized_kind = kind.strip().lower() or "sub"
        source_ids = [] if normalized_kind == "insert" else [
            item.strip() for item in position.split(",") if item.strip()
        ]
        if normalized_kind != "insert" and not source_ids:
            raise ProjectionError(f"annotated.csv line {line_number} has no source ids")
        rows.append(
            {
                "kind": "insert" if normalized_kind == "insert" else "sub",
                "source_ids": source_ids,
                "duration": float(duration),
                "corrected": _decode_cell(corrected),
                "translation": _decode_cell(translation),
                "conf": _confidence(conf),
            }
        )
    return rows


def _load_stable(path: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectionError(f"cannot read stable JSON: {exc}") from exc
    segments = payload.get("segments") if isinstance(payload, dict) else payload
    if not isinstance(segments, list) or not segments:
        raise ProjectionError("stable JSON must contain a non-empty segments list")
    normalized: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for index, value in enumerate(segments, start=1):
        if not isinstance(value, dict):
            raise ProjectionError(f"stable segment {index} is not an object")
        source_id = str(value.get("id", index))
        if source_id in by_id:
            raise ProjectionError(f"stable JSON has duplicate source id {source_id!r}")
        try:
            start, end = float(value["start"]), float(value["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProjectionError(f"stable segment {source_id!r} has invalid timing") from exc
        words = value.get("words")
        segment = {
            "id": source_id,
            "start": start,
            "end": end,
            "text": str(value.get("text") or "").strip(),
            "words": list(words) if isinstance(words, list) else [],
            "low_conf": bool(
                value.get("low_conf")
                or value.get("low_confidence")
                or str(value.get("confidence_label") or "").lower() == "low"
            ),
        }
        normalized.append(segment)
        by_id[source_id] = segment
    for index, segment in enumerate(normalized):
        # `by_id` holds these same objects, so the repair reaches the
        # final-mode lookups as well.
        if segment["end"] <= segment["start"]:
            following = normalized[index + 1]["start"] if index + 1 < len(normalized) else None
            segment["end"] = _repaired_end(segment["start"], segment["end"], following)
    return normalized, by_id


def _words_for(source_ids: Iterable[str], stable: Mapping[str, dict[str, Any]]) -> list[Any]:
    words: list[Any] = []
    for source_id in source_ids:
        if source_id not in stable:
            raise ProjectionError(f"annotated.csv references unknown stable source id {source_id!r}")
        words.extend(stable[source_id]["words"])
    return words


def project_edit_document(
    stable_json: str | Path,
    *,
    annotated_csv: str | Path | None = None,
    final_srt: str | Path | None = None,
    video_id: str,
    title: str,
    source: str = "",
    fingerprint: str | None = None,
    relaxed_srt: bool = False,
) -> dict[str, Any]:
    """Create an editor-compatible document from raw or final FineSub artifacts."""

    stable_segments, stable_by_id = _load_stable(Path(stable_json))
    if (annotated_csv is None) != (final_srt is None):
        raise ProjectionError("annotated_csv and final_srt must be supplied together")
    projected: list[dict[str, Any]] = []
    mode = "stable"
    if annotated_csv is None:
        for segment in stable_segments:
            item = {
                "t0": segment["start"],
                "t1": segment["end"],
                "ja": segment["text"],
                "zh": "",
            }
            if segment["words"]:
                item["words"] = segment["words"]
            if segment["low_conf"]:
                item["low_conf"] = True
            projected.append(item)
    else:
        annotated = parse_annotated(Path(annotated_csv).read_text(encoding="utf-8"))
        if relaxed_srt:
            # Cloud artifacts produced by older engines are not guaranteed to
            # obey one exact SRT block layout. Retrieval must remain possible:
            # consume any cues we can recognize, then recover missing timing
            # and text from the row-aligned annotated/stable artifacts.
            try:
                cues = parse_srt(Path(final_srt).read_text(encoding="utf-8"))
            except ProjectionError:
                cues = []
        else:
            cues = parse_srt(Path(final_srt).read_text(encoding="utf-8"))
        if not relaxed_srt and len(annotated) != len(cues):
            raise ProjectionError(
                "annotated.csv and final SRT row counts differ "
                f"({len(annotated)} vs {len(cues)}); refusing positional truncation"
            )
        mode = "final"
        previous_end = 0.0
        for index, row in enumerate(annotated):
            words = _words_for(row["source_ids"], stable_by_id)
            cue = cues[index] if index < len(cues) else None
            if cue is not None:
                start, end, translated = cue.start, cue.end, cue.text or row["translation"]
            elif row["source_ids"]:
                sources = [stable_by_id[source_id] for source_id in row["source_ids"]]
                start, translated = sources[0]["start"], row["translation"]
                # Every source segment occupies time by now, but a row whose
                # ids are not in timeline order can still span backwards.
                end = _repaired_end(start, sources[-1]["end"], None)
            else:
                # Insert rows have no stable source. In the unlikely event that
                # their SRT cue is unreadable, keep the text and place it after
                # the preceding row instead of rejecting the whole document.
                start = previous_end
                end = start + max(float(row["duration"]), MIN_CUE_SECONDS)
                translated = row["translation"]
            item = {
                "t0": start,
                "t1": end,
                "ja": row["corrected"],
                # The annotated table is the LLM stage's row-aligned source of
                # truth and often still contains the translation when a loose
                # SRT renderer emits an empty cue. Keep the final SRT wording
                # when present, otherwise recover that row instead of rejecting
                # the entire cloud document.
                "zh": translated,
            }
            if words:
                item["words"] = words
            source_low = any(stable_by_id[source_id]["low_conf"] for source_id in row["source_ids"])
            if row["conf"] == "low" or source_low:
                item["low_conf"] = True
            projected.append(item)
            previous_end = end
    return {
        "schema": 1,
        "video_id": video_id,
        "title": title,
        "source": source,
        "fp": fingerprint,
        "rev": 0,
        "subtitles": projected,
        "tracks": [],
        "track_meta": {
            "name": "默认轨",
            "ja": {"hidden": False, "style": "JP"},
            "zh": {"hidden": False, "style": "CN"},
        },
        "effects": [],
        "projection": {"schema": 1, "mode": mode},
    }

