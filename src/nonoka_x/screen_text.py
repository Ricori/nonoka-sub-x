"""On-screen text (文字识别): read the text in a video's frames, translate it, and the tracks it becomes.

An add-on of the editor, not a transcription: a task with target
`screen-text` runs the on-screen text package (`engines/flowocr`, kind `ocr`)
on the video, translates what it read with the user's own LLM route, and when
it ends the result is appended to the video's document as extra tracks whose
ids start with `ocr` -- the default (speech) track and the user's own tracks
are never touched (`DocumentStore.replace_generated_tracks`).

The request (`TaskRequest.screen_text`):

    {range: {start, end} | null, roi: [x0, y0, x1, y1] | null, skip_main: false,
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
import statistics
import time
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
    for name, default in (("skip_main", False), ("trial", False)):
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
    span_desc = (f"{float(span.get('start') or 0.0):.1f}s–{float(span.get('end') or 0.0):.1f}s"
                 if float(span.get("end") or 0.0) > 0 else "整片")
    emit("log", {"message": f"开始文字识别（范围：{span_desc}，设备：{request.get('device') or 'auto'}）"})
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
    if not total:
        return
    batches = (total + batch - 1) // batch

    def _log(message: str) -> None:
        if reporter is not None and hasattr(reporter, "debug"):
            reporter.debug(message)

    if reporter is not None:
        reporter.stage_started("screen-translate", detail=f"共 {total} 条待翻译（分 {batches} 批）")
    _log(f"开始翻译文字：共 {total} 条，分 {batches} 批")
    for batch_idx, offset in enumerate(range(0, total, batch), start=1):
        chunk = pending[offset:offset + batch]
        started = time.monotonic()
        if reporter is not None:
            reporter.progress("screen-translate", completed=done, total=total, unit="items",
                              detail=f"正在翻译第 {batch_idx}/{batches} 批（{done}/{total} 条）…")
        _log(f"翻译第 {batch_idx}/{batches} 批：请求模型翻译 {len(chunk)} 条")
        answers = parse_answer(complete(batch_messages(chunk, speech, names)))
        missing = [cue for cue in chunk if not answers.get(str(cue["id"]), "").strip()]
        if missing:
            if reporter is not None:
                reporter.progress("screen-translate", completed=done, total=total, unit="items",
                                  detail=f"第 {batch_idx}/{batches} 批补翻 {len(missing)} 条…")
            _log(f"第 {batch_idx}/{batches} 批有 {len(missing)} 条未返回，正在补翻")
            answers.update(parse_answer(complete(batch_messages(missing, speech, names))))
        for cue in chunk:
            key = str(cue["id"])
            if key in answers:
                # An empty answer is the model calling the text unreadable (see the prompt)
                cue["zh"] = answers[key].strip()
            else:
                # Still unanswered after asking again: show what was read rather than nothing
                cue["zh"] = "\n".join(str(line["text"]) for line in cue["lines"])
                cue["untranslated"] = True
        done += len(chunk)
        elapsed = time.monotonic() - started
        if reporter is not None:
            reporter.progress("screen-translate", completed=done, total=total, unit="items",
                              detail=f"已完成 {done}/{total} 条")
        _log(f"翻译第 {batch_idx}/{batches} 批完成（{done}/{total} 条，耗时 {elapsed:.1f}s）")


# --- tracks & the style ----------------------------------------------------------------------------------

ASS_STYLE_FORMAT = (
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
    "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
    "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
)
#: Consecutive cues of one track may overlap this much (OCR timing jitter): the earlier one is trimmed.
TRACK_OVERLAP_SLACK = 0.15


def _style_line(name: str, *, fontsize: int = 54, an: int = 5, ml: int = 12, mr: int = 12, mv: int = 12) -> str:
    return (
        f"Style: {name},思源黑体 Heavy,{fontsize},&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
        f"0,0,0,0,100,100,0,0,1,2,0,{an},{ml},{mr},{mv},1"
    )


def track_style_name(number: int) -> str:
    """The style of screen-text track `number` (1-based): `文字识别`, `文字识别 2`, ... like the tracks' names."""

    return STYLE_NAME if number == 1 else f"{STYLE_NAME} {number}"


def styles_from(document: Mapping[str, Any]) -> str:
    """ASS `[V4+ Styles]` block with one style of its own for each screen-text track.

    Where each text sits and how big it is belongs to the segment (`pos`), so a track's style says
    how that track's text looks: names can be restyled apart from lines, say, in the style panel.
    """

    names = [track["zh"]["style"] for track in tracks_from(document)]
    if not names:
        return ""
    return "[V4+ Styles]\n" + ASS_STYLE_FORMAT + "\n" + "\n".join(_style_line(name) for name in names) + "\n"


def merge_missing_styles(existing: str, incoming: str) -> str:
    """Append styles from `incoming` whose names are not yet defined in `existing` (never overwrite user styles).

    A new track's style (`文字识别 3`) starts as a copy of the document's `文字识别` when there is one,
    so a look the user already gave the screen text carries over to the tracks a later run adds.
    """

    if not incoming or not incoming.strip():
        return existing or ""
    existing_text = existing or ""
    existing_names: set[str] = set()
    base_fields: str | None = None
    for raw in existing_text.splitlines():
        line = raw.strip()
        if line.lower().startswith("style:"):
            parts = line[6:].split(",")
            if parts:
                name = parts[0].strip()
                if name:
                    existing_names.add(name)
                if name == STYLE_NAME:
                    base_fields = ",".join(parts[1:])
    to_add: list[str] = []
    for raw in incoming.splitlines():
        line = raw.strip()
        if not line.lower().startswith("style:"):
            continue
        parts = line[6:].split(",")
        name = parts[0].strip() if parts else ""
        if name and name not in existing_names:
            existing_names.add(name)
            if base_fields is not None and name.startswith(f"{STYLE_NAME} "):
                line = f"Style: {name},{base_fields}"
            to_add.append(line)
    if not to_add:
        return existing_text
    if not existing_text.strip():
        return "[V4+ Styles]\n" + ASS_STYLE_FORMAT + "\n" + "\n".join(to_add) + "\n"
    sep = "" if existing_text.endswith("\n") else "\n"
    if "[v4+ styles]" not in existing_text.lower():
        return existing_text + sep + "\n[V4+ Styles]\n" + ASS_STYLE_FORMAT + "\n" + "\n".join(to_add) + "\n"
    return existing_text + sep + "\n".join(to_add) + "\n"


def _line_pitch(cue: Mapping[str, Any]) -> float:
    """Top-to-top distance of the cue's stacked lines (fraction of frame height); 0 for one line."""

    fs = float(cue.get("fs") or 0.0)
    tops = sorted(float(line["box"][1]) for line in cue.get("lines") or () if len(line.get("box") or ()) == 4)
    steps = [b - a for a, b in zip(tops, tops[1:]) if b - a > fs * 0.5]
    return statistics.median(steps) if steps else 0.0


def _pos(cue: Mapping[str, Any]) -> dict[str, Any]:
    """Where the original text's ink is: the translation is laid out to land on it.

    `fs` (glyph height) and `lh` (line pitch) are always the cue's own, so the editor can size
    and space the translation like the original whatever font the style picks.
    """

    x0, y0, x1, y1 = (float(v) for v in cue["box"])
    an = int(cue.get("an") or 8)
    if len(cue.get("lines") or ()) <= 1 and an in (7, 8, 9):
        # One line sits mid-box: whatever margin the box has above and below (a decoration, a
        # padding the ink fit could not shed) then splits evenly instead of all landing below
        an -= 3
    x = x0 if an in (1, 4, 7) else x1 if an in (3, 6, 9) else (x0 + x1) / 2
    y = y0 if an in (7, 8, 9) else y1 if an in (1, 2, 3) else (y0 + y1) / 2
    out: dict[str, Any] = {"x": round(x, 5), "y": round(y, 5), "an": an, "w": round(x1 - x0, 5)}
    cue_fs = float(cue.get("fs") or 0.0)
    if cue_fs > 0:
        out["fs"] = round(cue_fs, 5)
    pitch = _line_pitch(cue)
    if pitch > 0:
        out["lh"] = round(pitch, 5)
    return out


def segment_of(cue: Mapping[str, Any], *, cover: str, translated: bool = True) -> dict[str, Any]:
    source = "\n".join(str(line["text"]) for line in cue["lines"])
    return {
        "t0": float(cue["t0"]),
        "t1": max(float(cue["t1"]), float(cue["t0"]) + 0.04),
        "ja": source,
        # The track shows only this lane: an untranslated run puts what was read here
        "zh": str(cue.get("zh") or "") if translated else source,
        "pos": _pos(cue),
        "cover": _cover(cue, cover),
    }


def _cover(cue: Mapping[str, Any], cover: str) -> dict[str, Any]:
    """The original text's box, its plate colour when plates are on, and the sampled background
    (`bg`) either way: the colour a plate gets when the user switches it on later."""

    out: dict[str, Any] = {"box": [float(v) for v in cue["box"]], "fill": cue.get("fill") if cover == "plate" else None}
    if cue.get("fill"):
        out["bg"] = cue["fill"]
    return out


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


def _layers(cues: list[Mapping[str, Any]]) -> list[list[Mapping[str, Any]]]:
    """Cues dealt onto as few tracks as keep every track free of overlaps.

    Texts on screen at once (a name over its line, a menu's items) go to tracks of their own. A free
    track whose last cue came from the same region is preferred, so one place on screen tends to
    stay on one track. Overlaps up to TRACK_OVERLAP_SLACK are timing jitter, not two texts at once.
    """

    layers: list[list[Mapping[str, Any]]] = []
    for cue in sorted(cues, key=lambda item: (float(item["t0"]), float(item["t1"]))):
        t0 = float(cue["t0"])
        free = [layer for layer in layers if float(layer[-1]["t1"]) <= t0 + TRACK_OVERLAP_SLACK]
        region = str(cue.get("region") or "")
        pick = next((layer for layer in free if str(layer[-1].get("region") or "") == region),
                    free[0] if free else None)
        if pick is None:
            layers.append([cue])
        else:
            pick.append(cue)
    return layers


def tracks_from(document: Mapping[str, Any], *, task_id: str = "") -> list[dict[str, Any]]:
    """The recognized on-screen text as tracks `ocr`, `ocr2`, ...: one more for each text shown at once.

    Only a track's translation lane is used; the text as read stays in `ja` for reference.
    """

    options = document.get("options") or {}
    cover = str(options.get("cover") or "none")
    translated = options.get("translate") != "none"
    skipped = {str(region["id"]) for region in document.get("regions") or () if region.get("skip")}
    cues = [
        cue for cue in (document.get("cues") or ())
        if str(cue.get("region") or "") not in skipped
        # translated, yet empty: the model read it as noise
        and not (options.get("translate") == "zh" and "zh" in cue and not str(cue.get("zh") or "").strip())
    ]
    tracks: list[dict[str, Any]] = []
    for index, layer in enumerate(_layers(cues)):
        segments = [segment_of(cue, cover=cover, translated=translated) for cue in layer]
        for before, after in zip(segments, segments[1:]):
            before["t1"] = max(before["t0"] + 0.04, min(before["t1"], after["t0"]))
        number = index + 1
        style = track_style_name(number)
        tracks.append({
            "id": TRACK_PREFIX if number == 1 else f"{TRACK_PREFIX}{number}",
            "name": style,
            # each track its own style, so one kind of text (names, lines, menus) is restyled alone
            "ja": {"hidden": True, "style": style},
            "zh": {"hidden": False, "style": style},
            "hja": 44,
            "hzh": 44,
            "segs": segments,
            "source": {
                "kind": SOURCE_KIND,
                "task": task_id,
                "region": "all",
                "label": "screen-text",
                "hash": segments_hash(segments),
            },
        })
    return tracks


def edited(track: Mapping[str, Any]) -> bool:
    """Whether a screen-text track was changed since it was made (its lines no longer hash as made)."""

    source = track.get("source") or {}
    return bool(source.get("hash")) and segments_hash(track.get("segs") or []) != source.get("hash")


__all__ = [
    "ASS_STYLE_FORMAT", "CLEANED_VIDEO_FILENAME", "INPAINT_PRESETS", "INPAINT_TARGET", "ON_EDITED", "SOURCE_KIND",
    "STYLE_NAME", "TARGET", "TRACK_PREFIX", "ScreenTextError", "batch_messages", "edited", "merge_missing_styles",
    "parse_answer", "run", "run_inpaint", "segment_of", "segments_hash", "speech_lines", "styles_from",
    "track_style_name", "tracks_from", "translate", "validate_inpaint_options", "validate_options",
]
