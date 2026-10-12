"""The LLM speaker judge's results, read back for the `spk` cell and the split-track export.

The judge (`finesub.llm.stages.speaker_judge`) reads the raw ASR rows with their acoustic labels window
by window and answers per row: keep, move to another track, open a new track, or abstain. Its answers
live in `<stem>-speaker-judge.json` next to `-stable.json`, which never changes -- deleting the sidecar
is going back to the acoustic labels. This module is the one reader, shared by the correction's
`-annotated.csv` and by `stages.py`'s exports, so both see the same verdicts.

Reading is checked, because the sidecar outlives what it was judged on (it is a record, never cleaned
up, and only a model call could make it again):

* the rows' ids, times and text must hash as they did when the windows were cut -- a re-transcribed
  stable is a different file, and the whole sidecar is dropped;
* the windows must tile the file in order, each row in exactly one, and a window halved into `parts`
  (its answer or its prompt did not fit) must be tiled by them -- anything else is damage;
* each unit's key -- a window's, or each part's when it was cut: its rows, their acoustic labels and
  marks, the tracks the profiles drew on -- must still match, or that unit falls back to the acoustic
  labels: re-running diarization renumbers the tracks, and an old verdict must never land on a new id;
* each unit's results must read (a list of rows in the shapes the judge writes), or the unit is owed;
* each result's `from` (the row's acoustic main track when it was judged) must still be the row's;
* a carried track's `of` must be the verdict its opener holds now, or that row falls back.

New tracks continue the acoustic numbering: past the largest track id anything in the stable mentions
(a filled line's `mark.track` or a `low` mark's `alternative` can name a track that leads no line, and
ids need not be contiguous), in unit order, then first appearance in the unit; each unit's new tracks
get their own numbers -- the same person in two units is two tracks, merged by a person if at all --
unless a unit judged after it carried the track on (units judged in order hand the next one the tracks
the previous one opened or used, owner 2026-10-09), which the sidecar records as that unit's id and
label (and the opener's verdict). So the numbering follows from the stable and the sidecar alone, whatever order they finished in.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .labels import SegmentSpeaker, filled_track, speaker_of

SIDECAR_SUFFIX = "-speaker-judge.json"
#: 2: one window per correction window, cut into `parts` only when its answer does not fit (owner
#: 2026-10-09); a version-1 file (150-row windows, a cast pick each) reads as absent and is judged again.
SIDECAR_VERSION = 2


def sidecar_path(stable_json: str | Path) -> Path:
    """`<stem>-speaker-judge.json` beside `<stem>-stable.json`."""
    stable = Path(stable_json)
    return stable.with_name(f"{stable.name.removesuffix('.json').removesuffix('-stable')}{SIDECAR_SUFFIX}")


def segment_ids(segments: Sequence[Mapping[str, Any]]) -> list[str]:
    """Row ids as `load_segments_from_stable_json` reads them: an explicit `id`, else the 1-based position."""
    return [str(segment.get("id") or index) for index, segment in enumerate(segments, start=1)]


def rows_hash(segments: Sequence[Mapping[str, Any]]) -> str:
    """The rows' ids, times and text -- not their speakers, so re-running diarization keeps the windows."""
    rows = [[key, segment.get("start"), segment.get("end"), str(segment.get("text", ""))]
            for key, segment in zip(segment_ids(segments), segments)]
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def acoustic_main(speaker: SegmentSpeaker) -> int | dict | None:
    """A row's `from`: its acoustic track, `{"filled": k}` for a line the marks filled, None when unassigned."""
    if speaker.track is not None:
        return int(speaker.track)
    filled = filled_track(speaker)
    return {"filled": filled} if filled is not None else None


def profile_tracks(speakers: Sequence[SegmentSpeaker]) -> list[int]:
    """The tracks a profile is written for: every track leading at least one line."""
    return sorted({int(speaker.track) for speaker in speakers if speaker.track is not None})


def largest_track(segments: Sequence[Mapping[str, Any]]) -> int:
    """The largest track id anything in these speaker objects names; -1 when none does."""
    found = [-1]
    for segment in segments:
        payload = segment.get("speaker")
        if not isinstance(payload, Mapping):
            continue
        if payload.get("track") is not None:
            found.append(int(payload["track"]))
        found += [int(track) for track in payload.get("tracks") or ()]
        found += [int(track) for track in (payload.get("coverage") or {})]
        mark = payload.get("mark")
        if isinstance(mark, Mapping):
            found += [int(mark[field]) for field in ("track", "alternative") if mark.get(field) is not None]
    return max(found)


def window_key(segments: Sequence[Mapping[str, Any]], ids: Sequence[str], tracks: Sequence[int]) -> str:
    """One window's identity: its rows (id, text, the whole speaker object -- labels and marks) and the
    profile tracks. Not whether the prompt showed the marks: switching that does not void a verdict."""
    rows = [[key, str(segment.get("text", "")), segment.get("speaker")] for key, segment in zip(ids, segments)]
    payload = json.dumps([rows, list(tracks)], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Judgments:
    """What the sidecar changes, after every check: `moved` maps a row id to the track it now leads."""

    moved: Mapping[str, int]
    new_tracks: tuple[int, ...] = ()
    abstained: frozenset[str] = frozenset()


def _warn(message: str) -> None:
    print(f"Warning: {message}", file=sys.stderr)  # product output


def read_judgments(
    stable_json: str | Path,
    segments: Sequence[Mapping[str, Any]] | None = None,
    *,
    warn: Callable[[str], None] = _warn,
) -> Judgments | None:
    """The verdicts that still hold for this stable, or None when there is no usable sidecar.

    `segments` defaults to the stable's own; a caller that already parsed it passes them."""
    path = sidecar_path(stable_json)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if segments is None:
            segments = json.loads(Path(stable_json).read_text(encoding="utf-8")).get("segments", [])
    except (OSError, ValueError) as error:
        warn(f"{path.name} unreadable ({error}); speaker judge results ignored")
        return None
    if not isinstance(payload, dict) or payload.get("version") != SIDECAR_VERSION:
        warn(f"{path.name} has an unknown version; speaker judge results ignored")
        return None
    if payload.get("rows_hash") != rows_hash(segments):
        warn(f"{path.name} was judged on another transcript; speaker judge results ignored")
        return None
    ids = segment_ids(segments)
    found = units(payload.get("windows") or [], ids)
    if found is None:
        warn(f"{path.name} is damaged (its windows do not tile the file); speaker judge results ignored")
        return None
    resolved = resolve(segments, found)
    if resolved.stale:
        warn(f"{resolved.stale} speaker-judge window(s) no longer match their rows (diarization re-run?); "
             "those rows keep the acoustic labels")
    return resolved.judgments


@dataclass(frozen=True)
class Unit:
    """What one judge call answered: a whole window, or one part of a window that was cut. `entry` is
    the sidecar's record of it (status, key, results...); rows `[low, high)`."""

    id: str
    entry: Mapping[str, Any]
    low: int
    high: int


def part_id(window: str, half: int) -> str:
    """A half's id, as the correction names its split windows: `0001-a`, `0001-b`."""
    return f"{window}-{'ab'[half]}"


def units(windows: Sequence[Any], ids: Sequence[str]) -> list[Unit] | None:
    """The units the windows hold, in row order; None when the windows do not tile the file or a cut
    window's parts do not tile it."""
    spans = tiling(windows, ids)
    if spans is None:
        return None
    out = []
    for window, (low, high) in zip(windows, spans):
        parts = window.get("parts")
        if not parts:
            out.append(Unit(str(window.get("id")), window, low, high))
            continue
        inner = tiling(parts, ids[low:high])
        if inner is None:
            return None
        out += [Unit(str(part.get("id")), part, low + start, low + end) for part, (start, end) in zip(parts, inner)]
    return out


@dataclass(frozen=True)
class Resolved:
    judgments: Judgments
    #: `(unit id, label)` -> the track number that label opened
    opened: Mapping[tuple[str, str], int]
    stale: int = 0


def verdict_of(entry: Mapping[str, Any]) -> str:
    """One unit's verdict as it stands -- its key and its results: what a later unit carrying one of its new
    tracks on names, so a re-judged opener (the same label, maybe another person) never inherits it."""
    payload = json.dumps({"key": entry.get("key"), "results": entry.get("results")}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _well_formed(results: Any) -> bool:
    """Whether a unit's results read: a list of rows, each a move, a new track, a carried track or an
    abstention in the shape the judge writes. A damaged file falls back rather than failing the task."""
    if not isinstance(results, list):
        return False
    for result in results:
        if not isinstance(result, Mapping) or "row" not in result:
            return False
        track, carried = result.get("track"), result.get("carried")
        if track is not None and (isinstance(track, bool) or not isinstance(track, int)):
            return False
        if carried is not None and not (isinstance(carried, Mapping)
                                        and all(isinstance(carried.get(key), str) for key in ("window", "label", "of"))):
            return False
    return True


def holds(segments: Sequence[Mapping[str, Any]], unit: Unit, tracks: Sequence[int] | None = None) -> bool:
    """Whether a unit's verdict stands: it succeeded, on these very rows, and its results read."""
    ids = segment_ids(segments)
    tracks = profile_tracks([speaker_of(segment) for segment in segments]) if tracks is None else tracks
    return unit.entry.get("status") == "success" and _well_formed(unit.entry.get("results")) and \
        unit.entry.get("key") == window_key(segments[unit.low:unit.high], ids[unit.low:unit.high], tracks)


def resolve(segments: Sequence[Mapping[str, Any]], found: Sequence[Unit]) -> Resolved:
    """What the units that still hold say, in row order -- the reader's rule, and the numbers the judge
    itself shows for a track it carries forward (it must show the number the reader will give).

    A result is a move to an acoustic track (`track`), a new track (`label`, numbered here), a track an
    earlier unit opened and this one carried on (`carried`: that unit's id, label and verdict -- dropped
    when that unit no longer holds that very verdict), or an abstention."""
    ids = segment_ids(segments)
    speakers = [speaker_of(segment) for segment in segments]
    tracks = profile_tracks(speakers)
    position = {key: index for index, key in enumerate(ids)}
    moved: dict[str, int] = {}
    opened: dict[tuple[str, str], int] = {}
    abstained: set[str] = set()
    following = largest_track(segments) + 1
    stale = 0
    verdicts: dict[str, str] = {}
    for unit in found:
        low, high = unit.low, unit.high
        if unit.entry.get("status") != "success":
            continue
        if not holds(segments, unit, tracks):
            stale += 1
            continue
        name = unit.id
        verdicts[name] = verdict_of(unit.entry)
        for result in unit.entry.get("results") or ():
            row = str(result.get("row", ""))
            index = position.get(row)
            if index is None or not low <= index < high or result.get("from") != acoustic_main(speakers[index]):
                continue
            if result.get("abstain"):
                abstained.add(row)
            elif result.get("label") is not None:
                key = (name, str(result["label"]))
                if key not in opened:
                    opened[key] = following
                    following += 1
                moved[row] = opened[key]
            elif isinstance(result.get("carried"), Mapping):
                source = (str(result["carried"].get("window")), str(result["carried"].get("label")))
                if source in opened and verdicts.get(source[0]) == result["carried"].get("of"):
                    moved[row] = opened[source]
            elif result.get("track") is not None:
                moved[row] = int(result["track"])
    judgments = Judgments(moved=moved, new_tracks=tuple(sorted(opened.values())), abstained=frozenset(abstained))
    return Resolved(judgments, opened, stale)


def tiling(windows: Sequence[Any], ids: Sequence[str]) -> list[tuple[int, int]] | None:
    """Each window's `[low, high)` row span, or None unless they cover every row once, in order."""
    position = {key: index for index, key in enumerate(ids)}
    spans, cursor = [], 0
    for window in windows:
        if not isinstance(window, Mapping):
            return None
        low, high = position.get(str(window.get("first"))), position.get(str(window.get("last")))
        if low != cursor or high is None or high < low:
            return None
        spans.append((low, high + 1))
        cursor = high + 1
    return spans if cursor == len(ids) and ids else None


def judged_speakers(
    segments: Sequence[Mapping[str, Any]],
    speakers: Sequence[SegmentSpeaker],
    judgments: Judgments | None,
) -> list[SegmentSpeaker]:
    """The speakers with the verdicts applied: a moved row leads its new track alone, with no `?`, no
    alternative and no fill -- the judge decided it. Everything else is the acoustic speaker unchanged."""
    if not judgments or not judgments.moved:
        return list(speakers)
    out = []
    for key, speaker in zip(segment_ids(segments), speakers):
        track = judgments.moved.get(key)
        if track is None:
            out.append(speaker)
            continue
        out.append(SegmentSpeaker(labels=speaker.labels, track=track, block=speaker.block,
                                  extras={"tracks": [track], "coverage": {str(track): 1.0}}))
    return out


def judgeable(segments: Sequence[Mapping[str, Any]]) -> bool:
    """Whether the judge can run on these rows at all: some row has an acoustic track, all in one block."""
    speakers = [speaker_of(segment) for segment in segments]
    return any(s.track is not None for s in speakers) and all(s.block == 0 for s in speakers)


def covers(stable_json: str | Path) -> bool:
    """Whether a usable sidecar holds a successful verdict for every window on these very rows.

    The prompt version is not asked, here or by the judge (owner 2026-10-09): a verdict a bumped prompt
    would give differently is still the task's own result, reused like any other. A finished task the sidecar does not cover gets the missing
    windows judged; one the judge cannot run on at all (no labels, several blocks) counts as covered,
    so it keeps the finished-task shortcut."""
    path = sidecar_path(stable_json)
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        segments = json.loads(Path(stable_json).read_text(encoding="utf-8")).get("segments", [])
    except (OSError, ValueError):
        return False
    if not judgeable(segments):
        return True
    if not isinstance(payload, dict) or payload.get("version") != SIDECAR_VERSION \
            or payload.get("rows_hash") != rows_hash(segments):
        return False
    found = units(payload.get("windows") or [], segment_ids(segments))
    if found is None:
        return False
    tracks = profile_tracks([speaker_of(segment) for segment in segments])
    return all(holds(segments, unit, tracks) for unit in found)
