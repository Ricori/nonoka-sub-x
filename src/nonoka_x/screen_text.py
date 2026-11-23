"""On-screen text (文字识别): read the text in a video's frames, translate it, and the tracks it becomes.

An add-on of the editor, not a transcription: a task with target
`screen-text` runs the on-screen text package (`engines/flowocr`, kind `ocr`)
on the video, translates what it read with the user's own LLM route, and when
it ends the result is appended to the video's document as extra tracks whose
ids start with `ocr` -- the default (speech) track and the user's own tracks
are never touched (`DocumentStore.replace_generated_tracks`).

The request (`TaskRequest.screen_text`):

    {range: {start, end} | null, roi: null, skip_main: true,
     translate: "zh" | "none", cover: "plate" | "none",
     match: {game} | null, trial: false, on_edited: "replace" | "keep",
     speech: [{t0, t1, text}]}       # added by the provider: the default track, as translation context

Each cue becomes one segment, at the place the text was read: `pos` (anchor
point, ASS alignment, box width and glyph height, all as fractions of the
frame) and `cover` (the original text's box, and the plate colour, or None for
no plate). The editor's ASS builder turns them into `\\pos` and a plate.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

TARGET = "screen-text"
INPAINT_TARGET = "screen-text-inpaint"
#: Every track a screen-text run makes has an id starting with this; a re-run replaces them.
TRACK_PREFIX = "ocr"
#: The style the tracks' lanes name; the editor adds it to the document's sheet when missing.
STYLE_NAME = "文字识别"
SOURCE_KIND = "screen-text"
SCREEN_TEXT_FILENAME = "screen_text.json"
CLEANED_VIDEO_FILENAME = "cleaned.mp4"
TRANSLATE = ("zh", "none")
COVER = ("plate", "none")
ON_EDITED = ("replace", "keep")
INPAINT_PRESETS = ("default", "hq")
#: How many cues one translation call carries, and the speech lines around a cue it is shown with.
BATCH = 40
CONTEXT_SECONDS = 15.0
MAX_CONTEXT_LINES = 60


class ScreenTextError(ValueError):
    """A request or a result this module cannot use."""


# --- the request -------------------------------------------------------------------------------------------


def validate_options(value: Any) -> dict[str, Any]:
    """`TaskRequest.screen_text`, checked and with its defaults."""

    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise ScreenTextError("screen_text must be an object")
    options = dict(value)
    options.setdefault("range", None)
    span = options.get("range")
    if span is not None:
        if not isinstance(span, Mapping):
            raise ScreenTextError("screen_text.range must be {start, end} or null")
        try:
            start, end = float(span.get("start") or 0.0), float(span.get("end") or 0.0)
        except (TypeError, ValueError) as error:
            raise ScreenTextError("screen_text.range must hold numbers") from error
        if start < 0 or (end and end <= start):
            raise ScreenTextError("screen_text.range must have 0 <= start < end")
        options["range"] = {"start": start, "end": end}
    roi = options.get("roi")
    if roi is not None:
        if not (isinstance(roi, list) and len(roi) == 4 and all(isinstance(v, (int, float)) for v in roi)
                and 0 <= roi[0] < roi[2] <= 1 and 0 <= roi[1] < roi[3] <= 1):
            raise ScreenTextError("screen_text.roi must be [x0, y0, x1, y1] within 0..1")
    options.setdefault("roi", None)
    for name, allowed, default in (("translate", TRANSLATE, "zh"), ("cover", COVER, "plate"),
                                   ("on_edited", ON_EDITED, "replace")):
        options.setdefault(name, default)
        if options[name] not in allowed:
            raise ScreenTextError(f"screen_text.{name} must be one of: {', '.join(allowed)}")
    for name, default in (("skip_main", True), ("trial", False)):
        options.setdefault(name, default)
        if not isinstance(options[name], bool):
            raise ScreenTextError(f"screen_text.{name} must be a boolean")
    match = options.get("match")
    if match is not None and not (isinstance(match, Mapping) and isinstance(match.get("game"), str) and match["game"]):
        raise ScreenTextError("screen_text.match must be {game} or null")
    options.setdefault("match", None)
    speech = options.get("speech") or []
    if not isinstance(speech, list):
        raise ScreenTextError("screen_text.speech must be a list")
    options["speech"] = [
        {"t0": float(item.get("t0") or 0.0), "t1": float(item.get("t1") or 0.0), "text": str(item.get("text") or "")}
        for item in speech if isinstance(item, Mapping) and str(item.get("text") or "").strip()
    ]
    return options


def validate_inpaint_options(value: Any) -> dict[str, Any]:
    """`TaskRequest.inpaint` for target `screen-text-inpaint`, checked and normalized."""

    if not isinstance(value, Mapping):
        raise ScreenTextError("inpaint must be an object")
    plates_ass = value.get("plates_ass")
    plates_path = value.get("plates_path")
    if not ((isinstance(plates_ass, str) and plates_ass.strip()) or (isinstance(plates_path, str) and plates_path.strip())):
        raise ScreenTextError("inpaint requires plates_ass or plates_path")
    try:
        start = float(value.get("start") or 0.0)
        end = float(value.get("end") or 0.0)
    except (TypeError, ValueError) as error:
        raise ScreenTextError("inpaint start/end must be numbers") from error
    if start < 0 or (end and end <= start):
        raise ScreenTextError("inpaint must have 0 <= start < end")
    preset = str(value.get("preset") or "default").strip().lower()
    if preset not in INPAINT_PRESETS:
        raise ScreenTextError(f"inpaint.preset must be one of: {', '.join(INPAINT_PRESETS)}")
    out_path = value.get("out_path")
    if out_path is not None and not (isinstance(out_path, str) and out_path.strip()):
        raise ScreenTextError("inpaint.out_path must be a non-empty path string")
    return {
        "plates_ass": str(plates_ass) if isinstance(plates_ass, str) else "",
        "plates_path": str(plates_path) if isinstance(plates_path, str) else "",
        "start": start,
        "end": end,
        "preset": preset,
        "out_path": str(out_path).strip() if isinstance(out_path, str) and out_path.strip() else "",
    }


def run_inpaint(request: Mapping[str, Any], task_dir: Path, *, session: Any, packages: Mapping[str, Any],
                emit: Callable[[str, Mapping[str, Any]], None], reporter: Any) -> dict[str, Path]:
    """Run FlowOCR's LaMa inpainter to produce a video-only cleaned intermediate MP4."""

    from . import engine_host

    options = validate_inpaint_options(request.get("inpaint"))
    package = engine_host.ocr_package(packages)
    if package is None:
        raise RuntimeError("文字识别层未安装")
    if options["plates_ass"]:
        plates_path = task_dir / "plates.ass"
        plates_path.write_text(options["plates_ass"], encoding="utf-8")
    else:
        plates_path = Path(options["plates_path"])
    out_path = Path(options["out_path"]) if options["out_path"] else (task_dir / CLEANED_VIDEO_FILENAME)
    session.reporter = reporter
    process = session.start(package, task_dir=task_dir)
    result = process.request("inpaint", {"job": {
        "source_path": str(request["source"]["path"]),
        "plates_path": str(plates_path),
        "out_path": str(out_path),
        "start": options["start"],
        "end": options["end"],
        "device": str(request.get("device") or "auto"),
        "preset": options["preset"],
    }})
    process.close()
    cleaned = Path(str(result["cleaned_mp4"]))
    emit("log", {"message": f"文字识别抹除完成：{result.get('frames', 0)} 帧（{result.get('backend', 'auto')} / {result.get('encoder', '')}）"})
    return {"cleaned_mp4": cleaned}


def speech_lines(document: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The default track's lines (`subtitles`) as translation context: time and the best text there is."""

    lines = []
    for seg in (document or {}).get("subtitles") or ():
        if not isinstance(seg, Mapping):
            continue
        text = str(seg.get("zh") or "").strip() or str(seg.get("ja") or "").strip()
        source = str(seg.get("ja") or "").strip()
        if text:
            lines.append({"t0": float(seg.get("t0") or 0.0), "t1": float(seg.get("t1") or 0.0),
                          "text": f"{source} / {text}" if source and source != text else text})
    return lines


# --- the run (in the task worker) ----------------------------------------------------------------------------


def run(request: Mapping[str, Any], task_dir: Path, *, session: Any, packages: Mapping[str, Any],
        emit: Callable[[str, Mapping[str, Any]], None], reporter: Any) -> dict[str, Path]:
    """Recognize, translate, write `screen_text.json`; the task's products by manifest name."""

    from . import engine_host

    options = validate_options(request.get("screen_text"))
    package = engine_host.ocr_package(packages)
    if package is None:
        raise RuntimeError("文字识别层未安装")
    out_dir = task_dir / "native" / package.id
    out_dir.mkdir(parents=True, exist_ok=True)
    span = options.get("range") or {}
    session.reporter = reporter
    process = session.start(package, task_dir=task_dir)
    result = process.request("recognize", {"job": {
        "source_path": str(request["source"]["path"]),
        "out_dir": str(out_dir),
        "start": span.get("start", 0.0),
        "end": span.get("end", 0.0),
        "device": str(request.get("device") or "auto"),
        "skip_main": options["skip_main"],
        "roi": options["roi"],
        "match": options["match"],
    }})
    process.close()
    ocr_json = Path(str(result["ocr_json"]))
    document = json.loads(ocr_json.read_text(encoding="utf-8"))
    stats = result.get("stats") or {}
    emit("log", {"message": f"文字识别：读到 {stats.get('cues', len(document.get('cues') or ()))} 处文字，"
                            f"{stats.get('regions_kept', 0)} 个区域要翻译"})
    if options["translate"] == "zh":
        corrector = engine_host.correction_package(packages)
        if corrector is None:
            raise RuntimeError("翻译需要纠错翻译包，但它没有安装")
        translator = session.start(corrector)

        def complete(messages: list[dict[str, str]]) -> str:
            answer = translator.request("llm.complete", {"request": {
                "role": "general_capable", "messages": messages, "max_tokens": 8192, "temperature": 0.3,
            }})
            if not isinstance(answer, Mapping) or not answer.get("ok"):
                error = (answer or {}).get("error") if isinstance(answer, Mapping) else None
                message = (error or {}).get("message") if isinstance(error, Mapping) else None
                raise RuntimeError(f"翻译失败：{message or '模型没有回答'}")
            return str(answer.get("content") or "")

        translate(document, options["speech"], complete, reporter=reporter)
        translator.close()
    document["options"] = {key: options[key] for key in ("translate", "cover", "skip_main", "trial", "on_edited")}
    output = task_dir / SCREEN_TEXT_FILENAME
    output.write_text(json.dumps(document, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return {"screen_text_json": output, "ocr_json": ocr_json}


# --- translation ---------------------------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "你在给视频做中文字幕。下面是视频画面上出现的文字（标题、标注、对话框、花字等），"
    "请把每一条翻成简体中文。\n"
    "要求：\n"
    "- 只翻译，不解释；保持简短，适合直接盖在原文位置上；\n"
    "- 人名、作品名按通行译名；数字、时间、符号照抄；\n"
    "- 已经是中文的照抄；读不通的乱码返回空字符串；\n"
    "- 参考同一时段的语音字幕理解上下文，但不要把语音内容写进译文。\n"
    "只输出一个 JSON 对象：键是条目的 id，值是译文。"
)


def _context_for(cues: list[Mapping[str, Any]], speech: list[Mapping[str, Any]]) -> list[str]:
    if not cues or not speech:
        return []
    start = min(float(cue["t0"]) for cue in cues) - CONTEXT_SECONDS
    end = max(float(cue["t1"]) for cue in cues) + CONTEXT_SECONDS
    lines = [f"[{line['t0']:.0f}s] {line['text']}" for line in speech if line["t1"] >= start and line["t0"] <= end]
    return lines[:MAX_CONTEXT_LINES]


def batch_messages(cues: list[Mapping[str, Any]], speech: list[Mapping[str, Any]],
                   region_names: Mapping[str, str]) -> list[dict[str, str]]:
    """One translation call: the system prompt, then the cues (and the speech around them) as JSON."""

    items = [{"id": str(cue["id"]), "where": region_names.get(str(cue["region"]), ""),
              "time": round(float(cue["t0"]), 1), "text": "\n".join(line["text"] for line in cue["lines"])}
             for cue in cues]
    body: dict[str, Any] = {"items": items}
    context = _context_for(cues, speech)
    if context:
        body["speech"] = context
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(body, ensure_ascii=False)}]


_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def parse_answer(text: str) -> dict[str, str]:
    """The model's JSON object (code fences tolerated); {} when it is not one."""

    cleaned = _FENCE.sub("", text.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        value = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return {}
    if not isinstance(value, Mapping):
        return {}
    return {str(key): str(item) for key, item in value.items() if isinstance(item, (str, int, float))}


def translate(document: dict[str, Any], speech: list[Mapping[str, Any]], complete: Callable[[list[dict[str, str]]], str],
              *, reporter: Any = None, batch: int = BATCH) -> None:
    """Fill each cue's `zh` in place: matched script text first, then the LLM, a missing answer asked once more."""

    skipped = {region["id"] for region in document.get("regions") or () if region.get("skip")}
    names = {region["id"]: region.get("name", "") for region in document.get("regions") or ()}
    pending = []
    for cue in document.get("cues") or ():
        matched = cue.get("matched") or {}
        if isinstance(matched, Mapping) and matched.get("cn"):
            cue["zh"] = str(matched["cn"])
        elif cue["region"] not in skipped:
            pending.append(cue)
    total = len(pending)
    done = 0
    if reporter is not None and total:
        reporter.stage_started("screen-translate")
    for offset in range(0, total, batch):
        chunk = pending[offset:offset + batch]
        answers = parse_answer(complete(batch_messages(chunk, speech, names)))
        missing = [cue for cue in chunk if not answers.get(str(cue["id"]), "").strip()]
        if missing:
            answers.update(parse_answer(complete(batch_messages(missing, speech, names))))
        for cue in chunk:
            cue["zh"] = answers.get(str(cue["id"]), "").strip()
        done += len(chunk)
        if reporter is not None:
            reporter.progress("screen-translate", completed=done, total=total, unit="items")


# --- tracks ------------------------------------------------------------------------------------------------


def _pos(cue: Mapping[str, Any]) -> dict[str, Any]:
    x0, y0, x1, y1 = (float(v) for v in cue["box"])
    an = int(cue.get("an") or 8)
    x = x0 if an in (1, 4, 7) else x1 if an in (3, 6, 9) else (x0 + x1) / 2
    y = y0 if an in (7, 8, 9) else y1 if an in (1, 2, 3) else (y0 + y1) / 2
    return {"x": round(x, 5), "y": round(y, 5), "an": an, "w": round(x1 - x0, 5), "fs": float(cue.get("fs") or 0.0)}


def segment_of(cue: Mapping[str, Any], *, cover: str) -> dict[str, Any]:
    return {
        "t0": float(cue["t0"]),
        "t1": max(float(cue["t1"]), float(cue["t0"]) + 0.04),
        "ja": "\n".join(str(line["text"]) for line in cue["lines"]),
        "zh": str(cue.get("zh") or ""),
        "pos": _pos(cue),
        "cover": {"box": [float(v) for v in cue["box"]], "fill": cue.get("fill") if cover == "plate" else None},
    }


def _round(value: Any, places: int) -> float:
    try:
        return round(float(value), places)
    except (TypeError, ValueError):
        return 0.0


def segments_hash(segments: Iterable[Mapping[str, Any]]) -> str:
    """What a track held when it was made: changed by an edit of its lines' text, times or places.

    Only those fields, rounded, so a document that went through the editor and back (which may add
    a style override or reformat a number) still hashes the same when nobody changed a line.
    """

    rows = []
    for seg in segments:
        pos = seg.get("pos") or {}
        box = (seg.get("cover") or {}).get("box") or []
        rows.append([
            _round(seg.get("t0"), 2), _round(seg.get("t1"), 2), str(seg.get("ja") or ""), str(seg.get("zh") or ""),
            _round(pos.get("x"), 3), _round(pos.get("y"), 3), [_round(v, 3) for v in box],
        ])
    canonical = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _lanes(cues: list[Mapping[str, Any]]) -> list[list[Mapping[str, Any]]]:
    """Cues of one region in lanes no two of whose cues overlap in time (first lane that is free)."""

    lanes: list[list[Mapping[str, Any]]] = []
    for cue in sorted(cues, key=lambda item: (float(item["t0"]), float(item["t1"]))):
        lane = next((lane for lane in lanes if float(lane[-1]["t1"]) <= float(cue["t0"]) + 1e-6), None)
        if lane is None:
            lanes.append([cue])
        else:
            lane.append(cue)
    return lanes


def tracks_from(document: Mapping[str, Any], *, task_id: str = "") -> list[dict[str, Any]]:
    """The extra tracks a finished run adds: one per region that is translated (more when its cues overlap)."""

    cover = str((document.get("options") or {}).get("cover") or "plate")
    by_region: dict[str, list[Mapping[str, Any]]] = {}
    for cue in document.get("cues") or ():
        by_region.setdefault(str(cue["region"]), []).append(cue)
    tracks: list[dict[str, Any]] = []
    for region in document.get("regions") or ():
        if region.get("skip"):
            continue
        cues = by_region.get(str(region["id"])) or []
        if not cues:
            continue
        for index, lane in enumerate(_lanes(cues)):
            segments = [segment_of(cue, cover=cover) for cue in lane]
            suffix = "" if index == 0 else f"-{index + 1}"
            tracks.append({
                "id": f"{TRACK_PREFIX}-{region['id']}{suffix}",
                "name": str(region.get("name") or "文字识别") + ("" if index == 0 else f"（{index + 1}）"),
                "ja": {"hidden": True, "style": STYLE_NAME},
                "zh": {"hidden": False, "style": STYLE_NAME},
                "hja": 44,
                "hzh": 44,
                "segs": segments,
                "source": {"kind": SOURCE_KIND, "task": task_id, "region": str(region["id"]),
                           "label": str(region.get("label") or ""), "hash": segments_hash(segments)},
            })
    return tracks


def edited(track: Mapping[str, Any]) -> bool:
    """Whether a screen-text track was changed since it was made (its lines no longer hash as made)."""

    source = track.get("source") or {}
    return bool(source.get("hash")) and segments_hash(track.get("segs") or []) != source.get("hash")


__all__ = [
    "CLEANED_VIDEO_FILENAME", "INPAINT_PRESETS", "INPAINT_TARGET", "ON_EDITED", "SOURCE_KIND", "STYLE_NAME",
    "TARGET", "TRACK_PREFIX", "ScreenTextError", "batch_messages", "edited", "parse_answer", "run",
    "run_inpaint", "segment_of", "segments_hash", "speech_lines", "tracks_from", "translate",
    "validate_inpaint_options", "validate_options",
]
