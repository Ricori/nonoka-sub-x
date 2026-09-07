"""Import an existing subtitle axis: the user's timeline outranks the engine's.

Four shapes arrive here; the frontend's `assAxis.ts` decides which one a file is
and lets the user overrule that call. They take three different routes:

  empty   -- timings only. FineSub has no way to be handed a timeline up front
             (there is no such parameter anywhere in `run_pipeline`), so
             recognition runs normally and `conform_to_axis` re-seats the
             finished projection onto these lines afterwards.
  ja      -- source text present, translation missing. `stable_from_axis` turns
             the rows into the one artifact the LLM stage consumes, so the
             worker skips separation, VAD and ASR entirely.
  zh, bi  -- nothing left to compute. `axis_projection` turns the rows straight
             into an EditDocument; no task runs at all.

Speakers ride along on every route: an axis that names who is talking (one ASS
style per person) is better evidence than any diarization model, so those rows
are split into editor tracks rather than re-derived.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

AXIS_KINDS = ("empty", "ja", "zh", "bi")

#: An axis is one human's work on one video. The cap exists so a malformed or
#: hostile payload cannot make the sidecar allocate without bound; a 4-hour
#: stream lands around 12k lines, so this leaves an order of magnitude of room.
MAX_AXIS_ROWS = 200_000

#: Two speakers get a track each. Beyond this the styles are almost certainly
#: typesetting rather than people, which is the same call `assAxis.ts` makes.
MAX_SPEAKERS = 10


class AxisError(ValueError):
    """The axis payload cannot be used without guessing."""


def normalize_axis(value: Any) -> dict[str, Any] | None:
    """Validate an axis payload from the UI into rows sorted by start time."""

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise AxisError("axis must be an object")
    kind = str(value.get("kind") or "")
    if kind not in AXIS_KINDS:
        raise AxisError(f"axis.kind must be one of {', '.join(AXIS_KINDS)}")
    raw_rows = value.get("rows")
    if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes)):
        raise AxisError("axis.rows must be a list")
    if not raw_rows:
        raise AxisError("axis.rows is empty")
    if len(raw_rows) > MAX_AXIS_ROWS:
        raise AxisError(f"axis has more than {MAX_AXIS_ROWS} rows")
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_rows, start=1):
        if not isinstance(raw, Mapping):
            raise AxisError(f"axis row {index} is not an object")
        try:
            t0, t1 = float(raw["t0"]), float(raw["t1"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AxisError(f"axis row {index} has invalid timing") from exc
        if not (t1 > t0 >= 0):
            raise AxisError(f"axis row {index} has a non-positive duration")
        rows.append(
            {
                "t0": t0,
                "t1": t1,
                "ja": str(raw.get("ja") or ""),
                "zh": str(raw.get("zh") or ""),
                "spk": str(raw.get("spk") or ""),
            }
        )
    rows.sort(key=lambda row: (row["t0"], row["t1"]))
    return {
        "kind": kind,
        "filename": str(value.get("filename") or ""),
        "rows": rows,
    }


def axis_speakers(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """Speakers the axis names, by descending time on screen.

    The first one owns the document's default lane, so ordering by airtime puts
    the host there instead of whoever happened to speak first.
    """

    airtime: dict[str, float] = {}
    for row in rows:
        speaker = str(row.get("spk") or "")
        if speaker:
            airtime[speaker] = airtime.get(speaker, 0.0) + (float(row["t1"]) - float(row["t0"]))
    ordered = sorted(airtime.items(), key=lambda item: (-item[1], item[0]))
    return [] if len(ordered) < 2 or len(ordered) > MAX_SPEAKERS else [name for name, _ in ordered]


def stable_from_axis(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The stable-JSON view of a source-text axis, for the LLM stage alone.

    Rows with no source text are dropped rather than carried as empty segments:
    the correction windows are built from this list, and a blank row is one the
    model would be asked to correct into something. Their timings survive in
    `axis.json` and are re-applied by `conform_to_axis` after projection.
    """

    segments = []
    for index, row in enumerate(rows, start=1):
        text = str(row.get("ja") or "").strip()
        if not text:
            continue
        segments.append(
            {"id": str(index), "start": float(row["t0"]), "end": float(row["t1"]), "text": text}
        )
    if not segments:
        raise AxisError("this axis has no source text to translate")
    return {"segments": segments}


@dataclass(frozen=True)
class AxisTranslation:
    """Where a source-text axis run left its artifacts."""

    stable_json: Path
    final_srt: Path
    annotated_csv: Path


def translate_axis(
    rows: Sequence[Mapping[str, Any]],
    *,
    output_path: str | Path,
    task_id: str,
    task_artifact_dir: str | Path,
    correction: Mapping[str, Any],
    knowledge: str = "none",
    source_path: str | Path | None = None,
    knowledge_root: str | Path | None = None,
    on_notice: Callable[[str], None] | None = None,
) -> AxisTranslation:
    """Run only the LLM stage, over subtitles the user already wrote.

    A source-text axis has everything separation, VAD and ASR would produce, so
    the whole speech half of the pipeline is skipped -- no GPU, no models, no
    audio decode. Stable JSON is the LLM stage's own input format, so writing
    one is the entire adaptation; `run_full_correction` is the same call
    `finesub.pipeline` makes for a normal run's second half.

    Shared by both execution modes: the desktop worker passes the media file so
    an audio or video correction reference stays available, the cloud passes
    `source_path=None` because it never receives the media at all and pins the
    reference to text either way.
    """

    import json

    from finesub.llm.correction_translation import run_full_correction
    from finesub.llm.routing.profiles import resolve_profile

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    stable_json = output.with_name(f"{output.stem}.stable.json")
    stable_json.write_text(
        json.dumps(stable_from_axis(rows), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    media = str(correction.get("media") or "text")
    video_path: str | Path | None = None
    if source_path is not None and media != "text":
        from finesub.stages import resolve_llm_media_for_source

        media, video_path, notice = resolve_llm_media_for_source(
            Path(source_path), stage="final-srt", llm_media=media, llm_video=None
        )
        if notice and on_notice is not None:
            on_notice(notice)
    elif source_path is None:
        media = "text"

    profile = resolve_profile(
        media,
        str(correction.get("retrieval") or "none"),
        str(correction.get("difficulty") or "quality"),
        "serial",
    )
    extra = {} if knowledge_root is None else {"knowledge_root": knowledge_root}
    final_srt = Path(run_full_correction(
        stable_json=stable_json,
        output_path=output,
        # A text reference reads nothing but the stable JSON, so the media file
        # is not opened at all -- which is what lets the cloud run this without
        # ever receiving one.
        audio_path=source_path if media != "text" else None,
        video_path=video_path,
        profile=profile,
        fast=str(correction.get("fast") or "auto"),
        extra_info=str(correction.get("extra_info") or ""),
        extra_style=str(correction.get("extra_style") or ""),
        knowledge=knowledge,
        task_id=task_id,
        task_artifact_dir=task_artifact_dir,
        resume=True,
        **extra,
    ))
    return AxisTranslation(
        stable_json=stable_json,
        final_srt=final_srt,
        annotated_csv=final_srt.with_name(f"{final_srt.stem}-annotated.csv"),
    )


def _track_meta(name: str) -> dict[str, Any]:
    return {"name": name, "ja": {"hidden": False, "style": "JP"}, "zh": {"hidden": False, "style": "CN"}}


def _new_track(index: int, name: str, segments: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": f"axis{index}",
        "name": name,
        # Styles are the editor's to assign: a name from the machine-level
        # `styles.ass` may not exist here, and JP/CN are the only two guaranteed
        # to resolve. `null` on the source lane matches what the editor writes
        # for a track it creates itself.
        "ja": {"hidden": False, "style": None},
        "zh": {"hidden": False, "style": None},
        "hja": 44,
        "hzh": 44,
        "segs": segments,
    }


def split_speaker_tracks(
    segments: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Deal row-aligned segments out to one track per named speaker.

    `segments` is positionally aligned with `rows` -- every caller here builds
    it that way. The busiest speaker keeps the document's own lane because that
    is the one the editor opens on; unlabelled rows join them rather than
    forming a nameless track.
    """

    speakers = axis_speakers(rows)
    if not speakers:
        return list(segments), [], _track_meta("默认轨")
    main, *others = speakers
    buckets: dict[str, list[dict[str, Any]]] = {name: [] for name in others}
    primary: list[dict[str, Any]] = []
    for segment, row in zip(segments, rows):
        speaker = str(row.get("spk") or "")
        buckets.get(speaker, primary).append(dict(segment))
    tracks = [
        _new_track(index, name, buckets[name])
        for index, name in enumerate(others, start=1)
        if buckets[name]
    ]
    return primary, tracks, _track_meta(main)


def axis_projection(
    rows: Sequence[Mapping[str, Any]],
    *,
    kind: str,
    video_id: str,
    title: str,
    source: str = "",
    fingerprint: str | None = None,
) -> dict[str, Any]:
    """An EditDocument built from the axis alone -- no engine, no task."""

    if kind not in {"zh", "bi"}:
        raise AxisError(f"axis kind {kind!r} cannot be imported without running a task")
    segments = [
        {"t0": float(row["t0"]), "t1": float(row["t1"]), "ja": str(row.get("ja") or ""), "zh": str(row.get("zh") or "")}
        for row in rows
    ]
    subtitles, tracks, meta = split_speaker_tracks(segments, rows)
    return {
        "schema": 1,
        "video_id": video_id,
        "title": title,
        "source": source,
        "fp": fingerprint,
        "rev": 0,
        "subtitles": subtitles,
        "tracks": tracks,
        "track_meta": meta,
        "effects": [],
        # Imported rows are as final as the run that produced them: nothing
        # downstream may re-derive timings from a stable layer that never
        # existed here.
        "projection": {"schema": 1, "mode": "final"},
    }


def _word_timing(word: Any) -> tuple[float, float, str] | None:
    if not isinstance(word, Mapping):
        return None
    try:
        start = float(word["start"] if "start" in word else word["t0"])
        end = float(word["end"] if "end" in word else word["t1"])
    except (KeyError, TypeError, ValueError):
        return None
    text = str(word.get("word") if "word" in word else word.get("text") or "")
    return start, end, text


def _seat(starts: Sequence[float], lanes: Sequence[Mapping[str, Any]], reach: float, t0: float, t1: float) -> int:
    """The axis line a span belongs to: most overlap, else nearest.

    Nearest rather than dropped -- a recognized phrase that falls in a gap the
    axis does not cover is still something the user said, and losing it
    silently is worse than seating it one line early or late.
    """

    index = max(0, bisect_left(starts, t0 - reach) - 1)
    best, best_overlap = -1, 0.0
    nearest, nearest_gap = index, float("inf")
    while index < len(lanes) and lanes[index]["t0"] < t1:
        lane = lanes[index]
        overlap = min(t1, lane["t1"]) - max(t0, lane["t0"])
        if overlap > best_overlap:
            best, best_overlap = index, overlap
        gap = max(lane["t0"] - t1, t0 - lane["t1"], 0.0)
        if gap < nearest_gap:
            nearest, nearest_gap = index, gap
        index += 1
    if best >= 0:
        return best
    # The scan stops at the first lane starting after the span, so a span that
    # ends before the axis begins never enters the loop at all -- it belongs on
    # the line the scan would have started from, not on the last one.
    return nearest if nearest_gap < float("inf") else min(index, len(lanes) - 1)


def _join(parts: Iterable[str]) -> str:
    return "\n".join(part for part in parts if part)


def conform_to_axis(projection: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Re-seat a finished projection onto the user's lines.

    The output has exactly one segment per axis row, carrying that row's exact
    timing -- that is the whole promise of importing an axis, and it holds even
    for rows nothing was recognized in (they come back empty for the user to
    fill).

    How the text is distributed depends on what ran. Word timings are the raw
    ASR's own, so they may be used to split a segment only while the text is
    still the raw one: after LLM correction the words no longer spell what the
    segment says, and a corrected line that spans several axis rows is seated
    whole on the one it overlaps most instead of being cut at a guessed point.

    The one place this cannot be right is simultaneous speech. The engine does
    no diarization (`features.diarization=false` on both providers), so a word
    carries when it was said and nothing about who said it; where two axis rows
    overlap, both cover the same words and whichever starts first takes them.
    Left as it is deliberately -- without speaker labels on the audio, every
    other tie-break is a different guess, not a better answer. The parser counts
    those rows (`assAxis.parseAxisFile`) so the UI can say they need a human.
    """

    segments = list(projection.get("subtitles") or [])
    lanes = [
        {"t0": float(row["t0"]), "t1": float(row["t1"]), "ja": [], "zh": [], "words": [], "low": False}
        for row in rows
    ]
    if not lanes:
        raise AxisError("axis has no rows")
    starts = [lane["t0"] for lane in lanes]
    reach = max(lane["t1"] - lane["t0"] for lane in lanes)
    corrected = str((projection.get("projection") or {}).get("mode") or "stable") == "final"

    for segment in segments:
        try:
            t0, t1 = float(segment["t0"]), float(segment["t1"])
        except (KeyError, TypeError, ValueError):
            continue
        words = [timing for timing in (_word_timing(word) for word in segment.get("words") or []) if timing]
        home = _seat(starts, lanes, reach, t0, t1)
        if words and not corrected:
            for start, end, text in words:
                lane = lanes[_seat(starts, lanes, reach, start, end)]
                lane["words"].append({"word": text, "start": start, "end": end})
                lane["ja"].append(text)
        else:
            for start, end, text in words:
                lanes[_seat(starts, lanes, reach, start, end)]["words"].append(
                    {"word": text, "start": start, "end": end}
                )
            source_text = str(segment.get("ja") or "")
            if source_text:
                lanes[home]["ja"].append(source_text)
        translation = str(segment.get("zh") or "")
        if translation:
            lanes[home]["zh"].append(translation)
        if segment.get("low_conf"):
            lanes[home]["low"] = True

    projected: list[dict[str, Any]] = []
    for lane in lanes:
        # Word-level seating rebuilds a Japanese line, which takes no spaces
        # between its pieces; whole segments joined into one row were separate
        # lines and keep a break so the merge stays visible and editable.
        item: dict[str, Any] = {
            "t0": lane["t0"],
            "t1": lane["t1"],
            "ja": ("".join(lane["ja"]) if not corrected and lane["words"] else _join(lane["ja"])),
            "zh": _join(lane["zh"]),
        }
        if lane["words"]:
            item["words"] = sorted(lane["words"], key=lambda word: word["start"])
        if lane["low"]:
            item["low_conf"] = True
        projected.append(item)

    subtitles, tracks, meta = split_speaker_tracks(projected, rows)
    conformed = dict(projection)
    conformed["subtitles"] = subtitles
    if tracks:
        conformed["tracks"] = tracks
        conformed["track_meta"] = meta
    return conformed
