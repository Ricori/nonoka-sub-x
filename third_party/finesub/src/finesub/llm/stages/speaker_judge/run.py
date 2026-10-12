"""The speaker judge's step in a correction task: one call per correction window, the sidecar.

Runs after the preparation task and before the correction windows (`correction_translation._run_task`),
on the raw ASR rows and their acoustic labels; the two never read each other's results in route one
(`docs/llm_harness_behavior.md`「说话人裁判」). Each correction window is one judge window (owner
2026-10-09), judged in one call that also names who speaks, from the preparation task's speakers and
people and the knowledge base's streamers. The modality is the switch's (`--diarize judge-text` /
`judge-audio`), not the correction's; the thinking cell is the correction's (owner 2026-10-08).

The correction's planning sized the window, so it normally fits. As a fallback it is halved the way the
correction halves a window that does not fit (`docs/llm_harness_behavior.md`「重试与拼接」): when a reply
reaches the output ceiling (API usage), when the retries ran out and the last reply left `<judge>` open
(an agent's usage cannot tell), or, before any call, when the prompt is past the judge's route's input envelope.
One level only: a half that still does not fit fails, and its rows keep their acoustic labels.

Resume is the sidecar (`speech.diarization.judgments`): once cut, its window list stands (owner
2026-10-09) -- a later re-plan of the correction neither re-cuts nor re-judges -- and is re-cut from
the current correction plan only when there is no usable sidecar or the rows themselves changed.
Each unit (a window, or a half of one) is written the moment its answer is in, so an interrupted run loses at
most the calls in flight. A unit is judged again only when it failed, is pending, or no longer matches
its rows: a verdict an older prompt gave is reused (owner 2026-10-09), as resume carries on a task's own
results.

Quota-type errors stop the whole judge rather than retrying window by window: the judge runs first,
and the quota it would burn is the correction's.
"""

from __future__ import annotations

import json
import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ....speech.diarization import judgments as sidecar
from ...chunking import SubtitleWindow
from ...client import extract_token_distribution, is_likely_output_limited
from ...routing.model_router import FailureKind, classify_failure
from ...exchange_log import ExchangeLogger
from ...exchange_metadata import llm_exchange_metadata
from ...knowledge.base import append_task_artifact
from ...media_upload import window_media_ref
from ...routing.config import LLMRole
from ...routing.profiles import TranslationProfile
from ...run_context import bind_llm_worker, llm_worker_context
from .answers import AnswerError, cut_off, parse_judge, parse_tracks, results
from .prompt import (
    SAMPLE_GAP,
    Candidate,
    Carried,
    audio_layout,
    cell_tracks,
    judge_prompt,
    profiles,
    rows_of,
)
from .windows import JudgeWindow, halves, split_windows

#: The judge's own prompt version, apart from the correction's `PROMPT_VERSION`, recorded with each
#: verdict; a bump re-judges nothing (owner 2026-10-09: a verdict that still matches its rows is reused).
PROMPT_VERSION = "speaker-judge-v3"
#: Calls per unit before it is recorded as failed (agy's malformed replies ran about one in five).
MAX_ATTEMPTS = 4
#: Empty replies in a row that read as a frozen quota rather than bad luck: from at least two units, and
#: each costing no output (a reply that spent tokens and came back empty is the model's, not the quota's).
EMPTY_REPLIES_TO_STOP = 3
#: Development switch: `0` hides the confidence marks from the judge's prompt (round 9's input).
MARKS_SWITCH = "FINESUB_SPEAKER_JUDGE_MARKS"
#: The share of the route's input envelope a unit's prompt may take before it is cut.
INPUT_SHARE = 0.9


def marks_shown() -> bool:
    return os.environ.get(MARKS_SWITCH, "1").strip() != "0"


def judge_group(media: str) -> str:
    """The route group the judge's call takes: its own modality's, whatever the correction's."""
    return "correction-mm" if media == "audio" else "correction-text"


class JudgeStopped(RuntimeError):
    """The provider said "not now" (quota, rate limit, a run of empty replies): stop judging."""


class Cut(Exception):
    """This window does not fit: judge it in two halves."""


@dataclass
class JudgeReport:
    windows: int = 0
    judged: int = 0
    reused: int = 0
    failed: int = 0
    pending: int = 0
    #: windows this run halved because an answer or a prompt did not fit
    cut: int = 0
    skipped: str = ""
    stopped: str = ""
    usage: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class JudgeInputs:
    """What the judge is told besides the rows: the title, the preparation task's summary, and the
    candidates (empty when neither the knowledge base nor the preparation task names anyone)."""

    title: str
    summary: str = ""
    pool: tuple[Candidate, ...] = ()


def vocal_audio_for(stable_json: str | Path) -> Path | None:
    """The separated vocals beside a stable, by the pipeline's own rule: `-vocal.ogg`, else `.flac`."""
    stable = Path(stable_json)
    base = stable.name.removesuffix(".json").removesuffix("-stable")
    for suffix in (".ogg", ".flac"):
        path = stable.with_name(f"{base}-vocal{suffix}")
        if path.exists():
            return path
    return None


def run_speaker_judge(
    *,
    stable_json: str | Path,
    correction_windows: Sequence[SubtitleWindow],
    profile: TranslationProfile,
    client: Any,
    inputs: JudgeInputs,
    media: str,
    audio: str | Path | None,
    task_artifact_dir: str | Path,
    task_id: str,
    parallel: int = 1,
    warn: Callable[[str], None] = lambda message: None,
    limits: Any = None,
    counter: Any = None,
) -> JudgeReport:
    """Judge every unit the sidecar does not already hold; returns what happened.

    `media` is the judge's modality (`text` / `audio`); `audio` the recording it hears with `audio`
    (the separated vocals). `limits` is the judge's route group's planning envelope and `counter` a
    token counter: with both, a window whose prompt would not fit is halved before it is sent."""
    report = JudgeReport()
    stable_json = Path(stable_json)
    segments = json.loads(stable_json.read_text(encoding="utf-8")).get("segments", [])
    rows = rows_of(segments)
    if not sidecar.judgeable(segments):
        multiple = any(row.speaker.block != 0 for row in rows)
        report.skipped = "speaker labels in more than one block" if multiple else "no speaker labels"
        if multiple:
            warn("说话人裁判跳过：声学标签分了多个 block，裁判只认一个 block 的轨号")
        return report
    ids = [row.id for row in rows]
    tracks = sidecar.profile_tracks([row.speaker for row in rows])
    path = sidecar.sidecar_path(stable_json)
    windows, entries = _window_list(path, segments, ids, correction_windows)
    report.windows = len(windows)
    use_audio = media == "audio" and audio is not None
    media = "audio" if use_audio else "text"
    state = _Run(segments=segments, ids=ids, tracks=tracks, entries=entries, windows=windows)
    todo = [index for index in range(len(windows)) if state.owed(index)]
    report.reused = len(windows) - len(todo)
    other = sum(1 for unit in state.units() if sidecar.holds(segments, unit, tracks)
                and unit.entry.get("media") not in (None, media))
    if other:
        warn(f"{other} 个说话人裁判窗是用另一种模态判的，照旧沿用（续跑延续任务当时的选择）；要按这次的档位重判，删掉 {path.name}")
    session = _Session(client, profile, Path(task_artifact_dir), task_id, judge_group(media))
    serial = profile.continuity == "serial"

    def write() -> None:
        payload = {"version": sidecar.SIDECAR_VERSION, "route": "separate",
                   "rows_hash": sidecar.rows_hash(segments), "windows": entries}
        temporary = path.with_name(f".{path.name}")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        temporary.replace(path)

    def one(index: int) -> None:
        if session.stopped.is_set():
            return
        try:
            _judge_whole_window(state, index, session, rows, inputs, audio=Path(audio) if use_audio else None,
                                serial=serial, limits=limits, counter=counter, write=write, warn=warn)
        except JudgeStopped as stop:
            with state.lock:  # the reason first: a window that only saw the flag must not overwrite it
                report.stopped = report.stopped or str(stop)
            session.stopped.set()

    with state.lock:
        for index in todo:
            state.mark_pending(index)
        write()
    # The run's reporter, generation pins and agent sessions follow the workers, as in the correction's pools.
    if serial:
        # In order, so each unit can be handed the tracks the one before it opened or used.
        for index in todo:
            one(index)
    else:
        worker_context = llm_worker_context()
        try:
            with ThreadPoolExecutor(max(1, parallel), thread_name_prefix="llm-speaker-judge",
                                    initializer=bind_llm_worker, initargs=(worker_context,)) as pool:
                list(pool.map(one, todo))
        finally:
            worker_context.release_lanes()
    report.judged = sum(status == "success" for status in state.outcome.values())
    report.failed = sum(status == "failed" for status in state.outcome.values())
    report.pending = sum(unit.entry.get("status") == "pending" for unit in state.units())
    report.cut = len(state.cut)
    report.usage = session.usage
    return report


@dataclass
class _Run:
    """The sidecar being written and what this run did, shared by the window workers."""

    segments: list
    ids: list[str]
    tracks: list[int]
    entries: list[dict]
    windows: list[JudgeWindow]
    lock: threading.Lock = field(default_factory=threading.Lock)
    #: this run's calls, per unit id (a reused verdict is not "judged")
    outcome: dict[str, str] = field(default_factory=dict)
    cut: set[int] = field(default_factory=set)

    def units(self) -> list[sidecar.Unit]:
        return sidecar.units(self.entries, self.ids) or []

    def window_units(self, index: int) -> list[sidecar.Unit]:
        window = self.windows[index]
        return [unit for unit in self.units() if window.low <= unit.low < window.high]

    def stands(self, unit: sidecar.Unit) -> bool:
        return sidecar.holds(self.segments, unit, self.tracks)

    def owed(self, index: int) -> bool:
        return any(not self.stands(unit) for unit in self.window_units(index))

    def mark_pending(self, index: int) -> None:
        for unit in self.window_units(index):
            if not self.stands(unit):
                _record(self.entries[index], unit.id, {"status": "pending"})

    def key(self, low: int, high: int) -> str:
        return sidecar.window_key(self.segments[low:high], self.ids[low:high], self.tracks)


def _record(window: dict, unit_id: str, fields: Mapping[str, Any]) -> None:
    """Replace what a unit -- the window itself, or one of its parts -- says, keeping where it is."""
    target = window if str(window.get("id")) == unit_id else next(
        part for part in window.get("parts") or () if str(part.get("id")) == unit_id)
    kept = {key: target[key] for key in ("id", "chunk", "first", "last", "parts") if key in target}
    target.clear()
    target.update({**kept, **fields})


def _halve(state: _Run, index: int, rows: Sequence[Any]) -> None:
    """Turn a window's entry into its two halves, pending (the window list itself never grows: parallel
    workers write by window index)."""
    window, judged = state.entries[index], state.windows[index]
    name = str(window.get("id"))
    kept = {key: window[key] for key in ("id", "chunk", "first", "last") if key in window}
    window.clear()
    window.update({**kept, "parts": [
        {"id": sidecar.part_id(name, half), "first": state.ids[low], "last": state.ids[high - 1], "status": "pending"}
        for half, (low, high) in enumerate(halves(rows, judged.low, judged.high))]})


def _judge_whole_window(state: _Run, index: int, session: "_Session", rows: list, inputs: JudgeInputs, *,
                        audio: Path | None, serial: bool, limits: Any, counter: Any,
                        write: Callable[[], None], warn: Callable[[str], None]) -> None:
    """Judge a window's units in order until each holds, failed, or the judge stopped; halve a window
    that does not fit and go on with its halves."""
    while True:
        with state.lock:
            todo = [unit for unit in state.window_units(index)
                    if not state.stands(unit) and unit.id not in state.outcome]
            if not todo:
                return
            unit = todo[0]
            found = state.units()
            carried = _carried(state.segments, found, unit) if serial else []
        part = JudgeWindow(unit.id, str(state.entries[index].get("chunk")), unit.low, unit.high)
        whole = unit.id == str(state.entries[index].get("id"))
        try:
            entry = _judge_unit(session, rows, part, inputs, tracks=state.tracks, carried=carried, audio=audio,
                                limits=limits, counter=counter, may_halve=whole and part.size >= 2)
        except Cut:
            with state.lock:
                _halve(state, index, rows)
                state.cut.add(index)
                write()
            continue
        except JudgeStopped:
            raise
        except Exception as error:  # noqa: BLE001 - the judge is optional: a broken unit falls back, the run goes on
            warn(f"说话人裁判窗 {unit.id} 出错（{type(error).__name__}: {error}），这些行保留声学标签")
            entry = {"status": "failed", "prompt_version": PROMPT_VERSION}
        with state.lock:
            state.outcome[unit.id] = entry["status"]
            _record(state.entries[index], unit.id, {**entry, "key": state.key(unit.low, unit.high)})
            write()


def _carried(segments: list, found: Sequence[sidecar.Unit], unit: sidecar.Unit) -> list[Carried]:
    """The new tracks the unit before this one opened or carried on, as the reader numbers them
    (owner 2026-10-09: units judged in order hand them on, and may merge into them); none when that
    unit does not hold a verdict. Their lines come from before this unit only: a re-judge must not
    be shown its own earlier answers as evidence."""
    before = next((other for other in found if other.high == unit.low), None)
    if before is None or not sidecar.holds(segments, before):
        return []
    resolved = sidecar.resolve(segments, found)
    verdicts = {other.id: sidecar.verdict_of(other.entry) for other in found}
    used = [key for key in (_track_of(result, before.id, verdicts) for result in before.entry.get("results") or ())
            if key is not None]
    position = {row: at for at, row in enumerate(sidecar.segment_ids(segments))}
    earlier = [other for other in found if other.high <= unit.low]
    out = []
    for key in dict.fromkeys(used):
        number = resolved.opened.get(key)
        if number is None:
            continue
        lines = tuple(sorted(position[row] for row, track in resolved.judgments.moved.items()
                             if track == number and position[row] < unit.low))
        out.append(Carried(number, key[0], key[1], _name(earlier, key, number, verdicts), lines, verdicts[key[0]]))
    return out


def _track_of(result: Mapping[str, Any], unit_id: str, verdicts: Mapping[str, str]) -> tuple[str, str] | None:
    """The new track a result puts its row on -- `(opener id, label)` -- when the reader would apply it: a
    carry whose opener has since given another verdict is not one (`judgments.resolve`)."""
    if result.get("label") is not None:
        return unit_id, str(result["label"])
    carried = result.get("carried")
    if isinstance(carried, Mapping) and verdicts.get(str(carried.get("window"))) == carried.get("of"):
        return str(carried.get("window")), str(carried.get("label"))
    return None


def _name(units: Sequence[sidecar.Unit], key: tuple[str, str], number: int, verdicts: Mapping[str, str]) -> str:
    """Who the judge last said a new track is: `@label` in the unit that opened it, `s<number>` in the
    units after it that still carry it (only those before the one being judged)."""
    name = ""
    for unit in units:
        names = unit.entry.get("names") or {}
        if unit.id == key[0]:
            name = names.get(f"@{key[1]}", name)
        elif f"s{number}" in names and any(_track_of(result, unit.id, verdicts) == key
                                           for result in unit.entry.get("results") or ()):
            name = names[f"s{number}"]
    return name


def _window_list(path: Path, segments: list, ids: list[str],
                 correction_windows: Sequence[SubtitleWindow]) -> tuple[list[JudgeWindow], list[dict]]:
    """The sidecar's windows when it still describes these rows; else a fresh cut, all pending."""
    try:
        stored = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except (OSError, ValueError):
        stored = None
    if (isinstance(stored, dict) and stored.get("version") == sidecar.SIDECAR_VERSION
            and stored.get("rows_hash") == sidecar.rows_hash(segments)):
        entries = list(stored.get("windows") or [])
        spans = sidecar.tiling(entries, ids)
        if spans is not None and sidecar.units(entries, ids) is not None:
            return ([JudgeWindow(str(e.get("id")), str(e.get("chunk")), low, high) for e, (low, high) in zip(entries, spans)],
                    entries)
    windows = split_windows(ids, correction_windows)
    return windows, [{"id": w.id, "chunk": w.chunk, "first": ids[w.low], "last": ids[w.high - 1],
                      "status": "pending"} for w in windows]


class _Session:
    """The calls of one judge run: routing, logging, usage, the stop signal."""

    def __init__(self, client: Any, profile: TranslationProfile, artifact_dir: Path, task_id: str, group: str) -> None:
        self.client, self.profile, self.artifact_dir, self.task_id = client, profile, artifact_dir, task_id
        self.group = group
        self.logger = ExchangeLogger.for_task_artifact_dir(artifact_dir)
        self.stopped = threading.Event()
        self.usage: dict[str, int] = {}
        #: per worker thread: the unit being judged -- its model and usage, for its sidecar entry
        self.local = threading.local()
        self._empty: list[str] = []
        self._lock = threading.Lock()

    def start_unit(self) -> None:
        self.local.model, self.local.usage = "", {}

    def call(self, name: str, system: str, user: str, *, file_ref: Any = None) -> Any:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        try:
            result = self.client.complete(LLMRole.AUDIO_MULTIMODAL, messages, file_ref=file_ref,
                                          task_group=self.group, difficulty=self.profile.difficulty)
        except Exception as error:  # noqa: BLE001 - classified below; the unit decides what to do
            if classify_failure(error) in (FailureKind.QUOTA, FailureKind.RATE_LIMIT):  # the router's rule: 429, 402
                raise JudgeStopped(f"{type(error).__name__}: {error}") from error
            raise
        tokens = extract_token_distribution(result.raw_response)
        self.local.model = result.model
        for key, value in tokens.items():
            self.local.usage[key] = self.local.usage.get(key, 0) + int(value or 0)
        with self._lock:
            for key, value in tokens.items():
                self.usage[key] = self.usage.get(key, 0) + int(value or 0)
            spent = int(tokens.get("output_tokens") or 0) + int(tokens.get("thinking_tokens") or 0)
            if (result.content or "").strip() or spent:
                self._empty = []
            else:
                self._empty.append(name.rsplit("-attempt", 1)[0])
            empty_run, units = len(self._empty), len(set(self._empty))
        append_task_artifact(self.artifact_dir, kind="speaker_judge_response", task_id=self.task_id,
                             payload={"call": name, "model": result.model, "usage": tokens})
        if self.logger is not None:
            self.logger.log(name, messages=messages, response_text=result.content,
                            metadata=llm_exchange_metadata(result, session=name, prompt_version=PROMPT_VERSION))
        if empty_run >= EMPTY_REPLIES_TO_STOP and units >= 2:
            raise JudgeStopped(f"{empty_run} empty replies in a row")
        return result

    def upload(self, path: Path) -> Any:
        return window_media_ref(path, execution_settings=getattr(self.client, "execution_settings", None),
                                routes=getattr(getattr(self.client, "router", None), "routes", None))


def _too_long(system: str, user: str, seconds: float, limits: Any, counter: Any) -> bool:
    """Whether a unit's prompt is past the route's input envelope (never, with nothing to check against)."""
    if limits is None or counter is None:
        return False
    tokens = counter.count_text(system) + counter.count_text(user)
    if seconds:
        tokens += math.ceil(seconds * limits.audio_tokens_per_second)
    return tokens > INPUT_SHARE * limits.prompt_input_limit


def _judge_unit(session: _Session, rows: list, unit: JudgeWindow, inputs: JudgeInputs, *,
                tracks: Sequence[int], carried: Sequence[Carried], audio: Path | None,
                limits: Any, counter: Any, may_halve: bool) -> dict[str, Any]:
    """One call (and its retries) for one unit. Raises `Cut` when a whole window does not fit; a half
    that does not fit comes back failed (one level only, as the correction's split)."""
    session.start_unit()
    entry: dict[str, Any] = {"prompt_version": PROMPT_VERSION, "media": "audio" if audio else "text"}
    profile = profiles(rows, unit, carried)
    layout = audio_layout(rows, unit, profile.samples) if audio is not None else None
    system, user, ids = judge_prompt(rows, unit, title=inputs.title, summary=inputs.summary, cast=inputs.pool,
                                     marks=marks_shown(), profile=profile, audio=layout)
    if _too_long(system, user, layout.seconds if layout else 0.0, limits, counter):
        if may_halve:
            raise Cut()
        return {**entry, "status": "failed"}
    file_ref = None
    if layout is not None:
        from ....media.ffmpeg import extract_spliced_audio

        clip = extract_spliced_audio(audio, layout.pieces, SAMPLE_GAP,
                                     session.artifact_dir / "clips" / f"speaker-judge-{unit.id}.aac")
        file_ref = session.upload(clip)
    shown = {track for index in unit.shown(rows) for track in cell_tracks(rows[index], marks=marks_shown())}
    known = [*tracks, *shown, *(track.track for track in carried)]
    left_open = False
    for attempt in range(MAX_ATTEMPTS):
        if session.stopped.is_set():
            raise JudgeStopped("another window stopped the judge")
        try:
            result = session.call(f"speaker_judge-{unit.id}-attempt{attempt}", system, user, file_ref=file_ref)
        except JudgeStopped:
            raise
        except Exception:  # noqa: BLE001 - a transient error: try again
            continue
        text = result.content or ""
        try:
            verdicts = parse_judge(text, ids, known)
        except AnswerError:
            # The output ceiling, by the API's usage (an agent's usage is the whole session's: never compared)
            ceiling = getattr(result, "requested_output_tokens", 0) or (limits.output_limit if limits else 0)
            if ceiling and is_likely_output_limited(result.raw_response, max_tokens=ceiling):
                if may_halve:
                    raise Cut()
                break
            left_open = cut_off(text)  # the last reply's, as the correction reads its final attempt
            continue
        entry.update(model=session.local.model, usage=dict(session.local.usage))
        return {**entry, "status": "success", "names": parse_tracks(text),
                "results": results(rows, unit, verdicts, {track.track: (track.window, track.label, track.of) for track in carried})}
    if left_open and may_halve:
        raise Cut()  # the fallback when usage cannot tell, as the correction's open `<translated>`
    entry.update(model=session.local.model, usage=dict(session.local.usage))
    return {**entry, "status": "failed"}


def _general_blocks(context_pack: Any) -> list[Mapping[str, Any]]:
    """The preparation task's general context, one block per research chunk: a long file researched in
    chunks merges them as `{chunk id: block}` (`research.merge_research_general_contexts`)."""
    general = getattr(context_pack, "general_context", None) or {}
    if not isinstance(general, Mapping):
        return []
    if "global_summary" in general or "entities" in general or "speakers" in general:
        return [general]
    return [value for key, value in sorted(general.items()) if isinstance(value, Mapping)]


def _listed(context_pack: Any, field: str) -> list[Mapping[str, Any]]:
    return [item for block in _general_blocks(context_pack) for item in block.get(field) or ()
            if isinstance(item, Mapping)]


def entities_of(context_pack: Any) -> list[Mapping[str, Any]]:
    """The preparation task's `entities` (prepare_task_v1.md), or none."""
    return _listed(context_pack, "entities")


def speakers_of(context_pack: Any) -> list[Mapping[str, Any]]:
    """Who the preparation task saw speaking (`speakers`, asked only with the judge on), or none."""
    return _listed(context_pack, "speakers")


#: A chunked run has one summary per chunk; the judge gets them joined, up to about two of them.
SUMMARY_MAX_CHARS = 1600


def summary_of(context_pack: Any) -> str:
    text = "\n".join(str(block.get("global_summary") or "").strip() for block in _general_blocks(context_pack)
                     if str(block.get("global_summary") or "").strip())
    return text if len(text) <= SUMMARY_MAX_CHARS else text[:SUMMARY_MAX_CHARS].rstrip() + "……"
