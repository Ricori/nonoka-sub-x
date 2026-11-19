"""`correction.json`: a FineSub correction's result in Nonoka's format (WP-L, plan 4.8 item 38).

The core's projection used to read FineSub's own products -- the stable's
product layer (`-stable.json` with its `-diar-rows.json` and what a speaker
judge changed), `-annotated.csv`, the final SRT and `correction-mode.json` --
and line them up into the editor's subtitles. That lining-up moved here,
unchanged, so that the core reads one Nonoka file instead
(`docs/provider-spec.md`, "correction.json"):

```json
{"schema": 1, "engine": "finesub", "mode": "final", "correct_only": true,
 "lines": [{"start": 0.77, "end": 5.075, "source": "...", "translation": "...",
            "speaker": "s1", "low_confidence": true, "words": [...]}]}
```

- `mode` is `stable` when the run stopped before the LLM (each line is a
  product-layer row of the transcription: `source` is its text, there is no
  `translation`), `final` when `-annotated.csv` and the final SRT are both there
  (each line is an annotated row: `source` the corrected text, `translation` the
  final wording);
- `correct_only` is written only when the run's record says it only corrected;
- `words` are the engine's word objects exactly as the stable holds them,
  `speaker` the lead speaker the editor files the line under, and
  `low_confidence` only when true; each only when there is one.

`correction_document` is the one conversion: the correction engine calls it at
the end of every run, and the one-time conversion of the cloud library and of
old local tasks calls it on stored products (`relaxed=True` reproduces how a
cloud library entry was projected when it was fetched).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA = 1
ENGINE_ID = "finesub"
#: The file in the task directory, beside `transcript.json`.
FILENAME = "correction.json"


class ProjectionError(ValueError):
    """FineSub's products cannot be lined up without guessing or data loss."""


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


def repaired_end(start: float, end: float, following: float | None) -> float:
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
        if not (text or "").lstrip("\ufeff").strip():
            return []
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
            cues[index] = SrtCue(cue.start, repaired_end(cue.start, cue.end, following), cue.text)
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


#: FineSub 0.6.0 writes a header naming the columns and adds `spk` (the
#: speaker tracks of the line's source segments) just before `note` -- `note`
#: stays last because it is the one cell allowed to hold a raw `|`. Files from
#: older engines, and cloud results fetched from them, have no header and the
#: nine columns below `spk`'s.
_LEGACY_ANNOTATED_COLUMNS = (
    "type", "position", "duration", "gap", "corrected", "translation", "conf", "char_count", "note",
)


def _annotated_columns(text: str) -> tuple[str, ...]:
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            columns = tuple(cell.strip() for cell in line.lstrip("#").split("|"))
            if "position" in columns and columns[-1] == "note":
                return columns
            continue
        break
    return _LEGACY_ANNOTATED_COLUMNS


def speaker_id(value: Any) -> str:
    """The main speaker a `spk` cell or a stable `speaker` object names, or "".

    The editor files a line under one speaker, so only the lead is kept: the
    first of a `+` list (FineSub orders it by coverage), the judge's track of a
    moved line (`s7<s3` -- left is the verdict), without the review marks
    (`s3?`, `s3?(s5)`, `s4*`). A name a person wrote is kept as it is.
    """

    if isinstance(value, Mapping):
        names = [
            str(label.get("name")).strip()
            for label in value.get("labels") or []
            if isinstance(label, Mapping) and str(label.get("name") or "").strip()
        ]
        if names:
            return names[0]
        tracks = value.get("tracks")
        track = tracks[0] if isinstance(tracks, list) and tracks else value.get("track")
        if track is None:
            mark = value.get("mark")
            if isinstance(mark, Mapping) and mark.get("tier") == "filled":
                track = mark.get("track")
        try:
            return f"s{int(track)}" if track is not None else ""
        except (TypeError, ValueError):
            return ""
    lead = str(value or "").strip().split("+", 1)[0]
    lead = lead.split("<", 1)[0].split("(", 1)[0].strip()
    return lead.rstrip("?*").strip()


def parse_annotated(text: str) -> list[dict[str, Any]]:
    columns = _annotated_columns(text)
    rows: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate((text or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split("|", len(columns) - 1)
        if len(fields) != len(columns):
            raise ProjectionError(
                f"annotated.csv line {line_number} does not have {len(columns)} columns"
            )
        cells = dict(zip(columns, fields))
        kind, position, duration = cells["type"], cells["position"], cells["duration"]
        corrected, translation, conf = cells["corrected"], cells["translation"], cells["conf"]
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
                "spk": speaker_id(cells.get("spk", "")),
            }
        )
    return rows


def product_rows(stable_json: str | Path) -> list[dict[str, Any]]:
    """A task's stable lines with their speakers, as FineSub reads its own products.

    `-stable.json` with its `-diar-rows.json` (labels, acoustic row splits)
    and what a speaker judge changed: the lines the ids in `annotated.csv`
    count, `<id>b` and `<id>i` too.
    """

    from finesub.speech.diarization.product import read_product

    return [dict(row) for row in read_product(Path(stable_json)).rows()]


def _load_stable(path: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectionError(f"cannot read stable JSON: {exc}") from exc
    segments = payload.get("segments") if isinstance(payload, dict) else payload
    if not isinstance(segments, list):
        raise ProjectionError("stable JSON must contain a segments list")
    if segments and all(isinstance(segment, dict) for segment in segments):
        # The engine's own reader: `-stable.json` with its `-diar-rows.json`
        # (labels, acoustic row splits) and what a speaker judge changed. These
        # are the lines the ids in annotated.csv count, `<id>b` and `<id>i` too.
        try:
            segments = product_rows(path)
        except (OSError, ValueError) as exc:
            raise ProjectionError(f"cannot read stable JSON: {exc}") from exc
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
            "spk": speaker_id(value.get("speaker")) if isinstance(value.get("speaker"), Mapping) else "",
        }
        normalized.append(segment)
        by_id[source_id] = segment
    for index, segment in enumerate(normalized):
        # `by_id` holds these same objects, so the repair reaches the
        # final-mode lookups as well.
        if segment["end"] <= segment["start"]:
            following = normalized[index + 1]["start"] if index + 1 < len(normalized) else None
            segment["end"] = repaired_end(segment["start"], segment["end"], following)
    return normalized, by_id


def _words_for(source_ids: Iterable[str], stable: Mapping[str, dict[str, Any]]) -> list[Any]:
    words: list[Any] = []
    for source_id in source_ids:
        if source_id not in stable:
            raise ProjectionError(f"annotated.csv references unknown stable source id {source_id!r}")
        words.extend(stable[source_id]["words"])
    return words


def recorded_correct_only(path: str | Path | None) -> bool:
    """Whether the task's `correction-mode.json` says it only corrected.

    The record rather than the requested language: `auto` can turn out Chinese,
    and a Chinese task begun before 0.7.0 goes on translating when resumed. A
    missing or unreadable record is a translation, as it is to the engine.
    """

    if path is None:
        return False
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("correct_only") is True


def _cues_by_row(
    annotated: list[dict[str, Any]], cues: list[SrtCue], *, relaxed: bool
) -> list[SrtCue | None]:
    """The final SRT cue of each annotated row, None for a row that has none.

    Since 0.7.0 a row with no translation stays in annotated.csv but gets no
    cue: a Chinese source's line that is only filler has its polished cell left
    empty, so the two files legitimately differ by exactly those rows. Any other
    difference cannot be mapped without guessing.
    """

    if len(annotated) == len(cues):
        return list(cues)
    with_text = [index for index, row in enumerate(annotated) if row["translation"]]
    if len(with_text) == len(cues):
        by_row: list[SrtCue | None] = [None] * len(annotated)
        for index, cue in zip(with_text, cues):
            by_row[index] = cue
        return by_row
    if relaxed:
        return [cues[index] if index < len(cues) else None for index in range(len(annotated))]
    raise ProjectionError(
        "annotated.csv and final SRT row counts differ "
        f"({len(annotated)} vs {len(cues)}); refusing positional truncation"
    )


def _line(start: float, end: float, source: str, translation: str | None, *,
          words: list[Any], low: bool, speaker: str) -> dict[str, Any]:
    line: dict[str, Any] = {"start": start, "end": end, "source": source}
    if translation is not None:
        line["translation"] = translation
    if words:
        line["words"] = words
    if low:
        line["low_confidence"] = True
    if speaker:
        line["speaker"] = speaker
    return line


def correction_document(
    stable_json: str | Path,
    *,
    annotated_csv: str | Path | None = None,
    final_srt: str | Path | None = None,
    correction_mode: str | Path | None = None,
    relaxed: bool = False,
) -> dict[str, Any]:
    """`correction.json` for FineSub's products of one task.

    `annotated_csv` and `final_srt` come together (a run that reached the LLM)
    or not at all (one that stopped at the raw SRT). `correction_mode` is the
    task's `correction-mode.json`. `relaxed` lines the final SRT up the way a
    cloud library entry was lined up when it was fetched: an SRT that does not
    parse, or does not match the annotated rows, is recovered row by row
    instead of refused.
    """

    stable_segments, stable_by_id = _load_stable(Path(stable_json))
    if (annotated_csv is None) != (final_srt is None):
        raise ProjectionError("annotated_csv and final_srt must be supplied together")
    lines: list[dict[str, Any]] = []
    if annotated_csv is None:
        for segment in stable_segments:
            lines.append(_line(segment["start"], segment["end"], segment["text"], None,
                               words=segment["words"], low=segment["low_conf"], speaker=segment["spk"]))
        document: dict[str, Any] = {"schema": SCHEMA, "engine": ENGINE_ID, "mode": "stable", "lines": lines}
        return document

    annotated = parse_annotated(Path(annotated_csv).read_text(encoding="utf-8"))
    if relaxed:
        # Cloud artifacts produced by older engines are not guaranteed to
        # obey one exact SRT block layout. Consume any cues we can recognize,
        # then recover missing timing and text from the row-aligned
        # annotated/stable artifacts.
        try:
            cues = parse_srt(Path(final_srt).read_text(encoding="utf-8"))
        except ProjectionError:
            cues = []
    else:
        cues = parse_srt(Path(final_srt).read_text(encoding="utf-8"))
    cue_of_row = _cues_by_row(annotated, cues, relaxed=relaxed)
    previous_end = 0.0
    for index, row in enumerate(annotated):
        words = _words_for(row["source_ids"], stable_by_id)
        cue = cue_of_row[index]
        if cue is not None:
            start, end, translated = cue.start, cue.end, cue.text or row["translation"]
        elif row["source_ids"]:
            sources = [stable_by_id[source_id] for source_id in row["source_ids"]]
            start, translated = sources[0]["start"], row["translation"]
            # Every source segment occupies time by now, but a row whose
            # ids are not in timeline order can still span backwards.
            end = repaired_end(start, sources[-1]["end"], None)
        else:
            # Insert rows have no stable source. In the unlikely event that
            # their SRT cue is unreadable, keep the text and place it after
            # the preceding row instead of rejecting the whole document.
            start = previous_end
            end = start + max(float(row["duration"]), MIN_CUE_SECONDS)
            translated = row["translation"]
        source_low = any(stable_by_id[source_id]["low_conf"] for source_id in row["source_ids"])
        # The `spk` cell already carries the speaker judge's verdicts; the
        # stable labels are the fallback for a file written without it.
        speaker = row["spk"] or next(
            (stable_by_id[source_id]["spk"] for source_id in row["source_ids"]
             if stable_by_id[source_id]["spk"]),
            "",
        )
        # The annotated table is the LLM stage's row-aligned source of truth
        # and often still contains the translation when a loose SRT renderer
        # emits an empty cue: the final SRT's wording when present, else that row's.
        lines.append(_line(start, end, row["corrected"], translated,
                           words=words, low=row["conf"] == "low" or source_low, speaker=speaker))
        previous_end = end
    document = {"schema": SCHEMA, "engine": ENGINE_ID, "mode": "final"}
    if recorded_correct_only(correction_mode):
        document["correct_only"] = True
    document["lines"] = lines
    return document


def write_document(document: Mapping[str, Any], path: str | Path) -> Path:
    """Write atomically; a rerun of the task replaces the file."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.part")
    partial.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(partial, target)
    return target


def write_correction(
    output: str | Path,
    stable_json: str | Path,
    *,
    annotated_csv: str | Path | None = None,
    final_srt: str | Path | None = None,
    correction_mode: str | Path | None = None,
    relaxed: bool = False,
) -> Path:
    """`correction_document` written to `output`. A product that is not a file counts as absent."""

    def present(value: str | Path | None) -> Path | None:
        return Path(value) if value is not None and Path(value).is_file() else None

    annotated, final = present(annotated_csv), present(final_srt)
    together = annotated is not None and final is not None
    return write_document(
        correction_document(
            stable_json,
            annotated_csv=annotated if together else None,
            final_srt=final if together else None,
            correction_mode=present(correction_mode),
            relaxed=relaxed,
        ),
        output,
    )
