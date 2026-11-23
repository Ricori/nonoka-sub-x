"""The package's requests: `check` (can this machine run it) and `recognize` (a video -> `ocr.json`).

`recognize` runs FlowOCR's own command-line stages as child processes (its
supervisor for the reading, then the track builder), reads what they write
only through `flowocr.artifacts.tracksio`, and turns it into Nonoka's
`ocr.json` (`to_ocr_document`):

    {schema: 1, engine: {id, version, commit}, video: {w, h, fps, duration},
     range: {start, end},
     regions: [{id, label, name, box, skip}],
     cues: [{id, region, t0, t1, lines: [{text, box, conf}], box, an, fs, fill}]}

Every box is normalized to the frame (0..1, `[x0, y0, x1, y1]`); times are
seconds. `an` is the ASS alignment the region's text anchors on (7/8/9: top
left / centre / right); `fs` is the line height as a fraction of the frame
height; `fill` is the background colour around the text (`#RRGGBB`), or None
when it could not be measured.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

SCHEMA = 1
#: Region labels whose text is never translated: the clock, a watermark or overlay, a fixed UI, noise.
SKIPPED_LABELS = frozenset(("noise", "clock", "ui-static", "static-overlay"))
#: The track names the editor shows, by region label: short and plain.
LABEL_NAMES = {
    "subtitle": "字幕",
    "dialogue-or-caption": "对白",
    "text-wall": "大段文字",
    "misc": "文字识别",
    "static-overlay": "固定文字",
    "ui-static": "界面",
    "clock": "时间",
    "noise": "杂字",
}
ALIGN_AN = {"left": 7, "center": 8, "right": 9}
#: FlowOCR's exit codes the core reads differently from a crash (`run_ocr2`).
EXIT_INCOMPLETE = 3
EXIT_BAD_PTS = 4
_PROGRESS = re.compile(r"^\[\s*([0-9.]+)%\]")


class OcrFailure(RuntimeError):
    """A failure with its own short message for the user (`kind` names it)."""

    def __init__(self, kind: str, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.detail = detail


# --- check -------------------------------------------------------------------------------------------------


def _driver() -> tuple[str, int]:
    """The NVIDIA driver version and the first card's memory (MB); ("", 0) without one."""

    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=driver_version,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=20, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return "", 0
    first = (out.strip().splitlines() or [""])[0]
    parts = [part.strip() for part in first.split(",")]
    if len(parts) < 2:
        return "", 0
    try:
        return parts[0], int(float(parts[1]))
    except ValueError:
        return parts[0], 0


def _driver_ok(version: str) -> bool:
    try:
        return int(version.split(".")[0]) >= 580
    except ValueError:
        return False


def _nvdec() -> bool:
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-hwaccels"], capture_output=True, text=True, timeout=20,
                             check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "cuda" in out.split()


def _models() -> tuple[bool, bool, list[str]]:
    """(source models present, derived graphs current, what is wrong) -- read only, nothing is fetched or made."""

    from flowocr import models
    from flowocr.burn import inpaint, lamagraph

    problems: list[str] = []
    present = True
    for key in ("det", "rec", "lama"):
        model = models.MODELS[key]
        for name in model.files:
            if not (model.dir / name).is_file():
                present = False
                problems.append(f"缺模型文件：{model.dir.name}/{name}")
    current = not models.derived_stale(models.MODELS["rec"])
    try:
        import onnxruntime as ort

        lama = models.MODELS["lama"]
        for cfg in inpaint.PRESETS.values():
            h, w = cfg.win
            dst = lama.dir / f"lama_{h}x{w}_act{cfg.act}_v{lamagraph.VERSION}.onnx"
            mark = dst.with_name(dst.name + ".src-sha256")
            want = f"{lama.files[lama.main]} lamagraph{lamagraph.VERSION} ort{ort.__version__}"
            if not (dst.is_file() and mark.is_file() and mark.read_text(encoding="utf-8").strip() == want):
                current = False
    except ImportError:
        current = False
    if not current:
        problems.append("派生模型和运行环境对不上")
    return present, current, problems


def check(request: Mapping[str, Any]) -> dict[str, Any]:
    """Whether this machine can run it, and on what; `issues` are what stops it (short words)."""

    issues: list[dict[str, str]] = []
    backend = "cpu"
    try:
        import onnxruntime as ort

        if "CUDAExecutionProvider" in ort.get_available_providers():
            backend = "cuda"
    except ImportError:
        issues.append({"code": "runtime_missing", "message": "文字识别层不完整，请到设置里修复"})
    driver, vram = _driver()
    driver_ok = _driver_ok(driver) if driver else False
    if str(request.get("device") or "auto") == "cpu" or not driver_ok:
        backend = "cpu"
    present, current, problems = _models()
    if not present or not current:
        issues.append({"code": "models_stale", "message": "文字识别层需要更新，请到设置里修复"})
    warnings: list[str] = []
    if backend == "cpu":
        warnings.append("没有可用的 NVIDIA 显卡，会用 CPU，速度很慢" if not driver
                        else "显卡驱动太旧（要 580 以上），会用 CPU，速度很慢" if not driver_ok
                        else "会用 CPU，速度很慢")
    return {
        "backend": backend,
        "driver": driver,
        "driver_ok": driver_ok,
        "vram_mb": vram,
        "nvdec": _nvdec(),
        "models_ok": present,
        "marks_ok": current,
        "problems": problems,
        "issues": issues,
        "warnings": warnings,
    }


# --- recognize ---------------------------------------------------------------------------------------------


def _device_arg(device: str) -> str:
    """Nonoka's device (`auto`, `cpu`, `cuda`, `cuda:N`) as FlowOCR's `--device`."""

    device = (device or "auto").strip()
    if device == "cpu":
        return "cpu"
    if device.startswith("cuda:"):
        return "gpu:" + device.split(":", 1)[1]
    return "auto"


def _run(command: list[str], *, cwd: Path, on_line: Callable[[str], None] | None = None) -> tuple[int, list[str]]:
    """Run one FlowOCR stage; its output goes line by line to `on_line` and to stderr. Returns (code, last lines)."""

    tail: list[str] = []
    process = subprocess.Popen(
        command, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert process.stdout is not None
    for raw in process.stdout:
        line = raw.rstrip("\r\n")
        print(line, file=sys.stderr, flush=True)
        tail = (tail + [line])[-20:]
        if on_line is not None:
            on_line(line)
    return process.wait(), tail


def _python() -> list[str]:
    # FlowOCR's children find the package on the PYTHONPATH the core wrote (`engine_host`).
    return [sys.executable]


def recognize(request: Mapping[str, Any], reporter: Any) -> dict[str, Any]:
    """Read every piece of on-screen text in the video and write `ocr.json` into `out_dir`."""

    source = Path(str(request["source_path"]))
    out_dir = Path(str(request["out_dir"]))
    out_dir.mkdir(parents=True, exist_ok=True)
    if not source.is_file():
        raise OcrFailure("source_missing", "找不到视频文件", str(source))
    start = float(request.get("start") or 0.0)
    end = float(request.get("end") or 0.0)
    obs = out_dir / "obs.jsonl"
    reporter.planned(["screen-read", "screen-tracks"])

    reporter.stage_started("screen-read")
    command = [*_python(), "-m", "flowocr.extract.supervisor", str(source), "--out", str(obs),
               "--device", _device_arg(str(request.get("device") or "auto"))]
    if start > 0:
        command += ["--start", f"{start:.3f}"]
    if end > 0:
        command += ["--end", f"{end:.3f}"]
    last = [0.0]

    def progress(line: str) -> None:
        match = _PROGRESS.match(line.strip())
        if not match:
            return
        value = min(100.0, float(match.group(1)))
        now = time.monotonic()
        if value >= 100.0 or now - last[0] >= 1.0:
            last[0] = now
            reporter.progress("screen-read", completed=int(value * 10), total=1000, unit="permille")

    started = time.monotonic()
    code, tail = _run(command, cwd=out_dir, on_line=progress)
    if code == EXIT_INCOMPLETE:
        raise OcrFailure("incomplete", "识别没跑完，请重试", "\n".join(tail))
    if code == EXIT_BAD_PTS:
        raise OcrFailure("bad_timestamps", "这个视频的时间戳有问题，识别不了", "\n".join(tail))
    if code != 0 or not obs.is_file():
        raise OcrFailure("read_failed", "识别失败", "\n".join(tail))
    reporter.progress("screen-read", completed=1000, total=1000, unit="permille")

    reporter.stage_started("screen-tracks")
    code, tail = _run([*_python(), "-m", "flowocr.analyze.build_tracks", str(obs), "--outdir", str(out_dir),
                       "--tag", "v"], cwd=out_dir)
    tracks_json = out_dir / "v-tracks.json"
    if code != 0 or not tracks_json.is_file():
        raise OcrFailure("tracks_failed", "整理识别结果失败", "\n".join(tail))

    from flowocr.artifacts import tracksio

    doc = tracksio.load(tracks_json)
    video = _video_info(source)
    document = to_ocr_document(doc, video=video, start=start, end=end,
                               skip_main=bool(request.get("skip_main", True)), engine=request.get("engine") or {})
    if request.get("measure_fill", True):
        _measure_fills(document, source)
    ocr_json = out_dir / "ocr.json"
    ocr_json.write_text(json.dumps(document, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    cues = document["cues"]
    return {
        "ocr_json": str(ocr_json),
        "tracks_json": str(tracks_json),
        "stats": {
            "regions": len(document["regions"]),
            "regions_kept": sum(1 for region in document["regions"] if not region["skip"]),
            "cues": len(cues),
            "elapsed_sec": round(time.monotonic() - started, 1),
        },
    }


def _video_info(source: Path) -> dict[str, Any]:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height,avg_frame_rate:format=duration", "-of", "json", str(source)],
            capture_output=True, text=True, timeout=60, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
        data = json.loads(out or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    stream = (data.get("streams") or [{}])[0]
    fps = 0.0
    try:
        num, den = str(stream.get("avg_frame_rate") or "0/1").split("/")
        fps = float(num) / float(den) if float(den) else 0.0
    except ValueError:
        pass
    return {"w": int(stream.get("width") or 0), "h": int(stream.get("height") or 0), "fps": round(fps, 3),
            "duration": float((data.get("format") or {}).get("duration") or 0.0)}


# --- the conversion (pure: tested without models) ------------------------------------------------------------------


def _norm(box: list[float], w: float, h: float) -> list[float]:
    x0, y0, x1, y1 = (float(v) for v in box[:4])
    clamp = lambda v: round(min(1.0, max(0.0, v)), 5)  # noqa: E731
    return [clamp(x0 / w), clamp(y0 / h), clamp(x1 / w), clamp(y1 / h)]


def _union(boxes: list[list[float]]) -> list[float]:
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _names(regions: list[dict[str, Any]]) -> dict[int, str]:
    """`画面·对白`, `画面·对白 2`, ...: one per region, numbered when a label repeats."""

    count: dict[str, int] = {}
    names: dict[int, str] = {}
    for region in regions:
        label = LABEL_NAMES.get(str(region.get("label") or ""), "文字识别")
        count[label] = count.get(label, 0) + 1
        names[int(region["index"])] = f"画面·{label}" + (f" {count[label]}" if count[label] > 1 else "")
    return names


def to_ocr_document(doc: Mapping[str, Any], *, video: Mapping[str, Any] | None = None, start: float = 0.0,
                    end: float = 0.0, skip_main: bool = True, engine: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """A `tracks.json` (FlowOCR's, as `tracksio.load` returns it) as Nonoka's `ocr.json`."""

    from flowocr.artifacts import tracksio

    w, h = (float(v) for v in doc["size"][:2])
    main = tracksio.main_regions(doc) if skip_main else set()
    regions = sorted(doc["regions"], key=lambda region: int(region["index"]))
    names = _names(regions)
    region_rows: dict[int, dict[str, Any]] = {}
    for region in regions:
        index = int(region["index"])
        label = str(region.get("label") or "misc")
        region_rows[index] = {
            "id": f"r{index:02d}",
            "label": label,
            "name": names[index],
            "align": str(region.get("align") or "center"),
            "skip": label in SKIPPED_LABELS or index in main,
            "boxes": [],
        }
    cues: list[dict[str, Any]] = []
    for track in doc["tracks"]:
        if track.get("kind") != "region":
            continue
        index = int(track["region"])
        row = region_rows.get(index)
        if row is None:
            continue
        for cue in track["cues"]:
            rows = tracksio.cue_rows(doc, track, cue, drop_filtered=True)
            if not rows:
                continue
            lines = []
            for line in rows:
                text = str(line["text"]).strip()
                if not text:
                    continue
                confs = [float(e.get("conf") or 0.0) for e in line["events"]]
                lines.append({"text": text, "box": _norm(line["box"], w, h),
                              "conf": round(min(confs) if confs else 0.0, 3)})
            if not lines:
                continue
            box = _union([line["box"] for line in lines])
            heights = sorted(line["box"][3] - line["box"][1] for line in lines)
            row["boxes"].append(box)
            cues.append({
                "id": str(cue["id"]),
                "region": row["id"],
                "t0": round(float(cue["t_start"]) / 1e6, 3),
                "t1": round(float(cue["t_end"]) / 1e6, 3),
                "lines": lines,
                "box": box,
                "an": ALIGN_AN.get(row["align"], 8),
                "fs": round(heights[len(heights) // 2], 5),
                "fill": None,
            })
    cues.sort(key=lambda cue: (cue["t0"], cue["region"], cue["box"][1]))
    out_regions = []
    for row in region_rows.values():
        boxes = row.pop("boxes")
        row["box"] = _union(boxes) if boxes else [0.0, 0.0, 0.0, 0.0]
        row.pop("align")
        out_regions.append(row)
    info = dict(video or {})
    return {
        "schema": SCHEMA,
        "engine": dict(engine or {}),
        "video": {"w": int(info.get("w") or w), "h": int(info.get("h") or h), "fps": info.get("fps") or 0.0,
                  "duration": info.get("duration") or 0.0},
        "range": {"start": start, "end": end},
        "regions": out_regions,
        "cues": cues,
    }


# --- the background colour around the text --------------------------------------------------------------------


#: How far around the text box the background is sampled, as a fraction of the line height.
FILL_RING = 0.35


def _measure_fills(document: dict[str, Any], source: Path) -> None:
    """Each cue's `fill`: the median colour of a ring just outside its box, at the cue's middle frame.

    Read in time order with one decoder; a cue whose frame cannot be read keeps `fill` None.
    """

    try:
        import cv2
        import numpy as np
    except ImportError:
        return
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        return
    try:
        for cue in sorted(document["cues"], key=lambda item: (item["t0"] + item["t1"]) / 2):
            capture.set(cv2.CAP_PROP_POS_MSEC, (cue["t0"] + cue["t1"]) / 2 * 1000.0)
            ok, frame = capture.read()
            if not ok or frame is None:
                continue
            fh, fw = frame.shape[:2]
            x0, y0, x1, y1 = cue["box"]
            pad = max(2, int(round(cue["fs"] * fh * FILL_RING)))
            ax0, ay0 = max(0, int(x0 * fw) - pad), max(0, int(y0 * fh) - pad)
            ax1, ay1 = min(fw, int(x1 * fw) + pad), min(fh, int(y1 * fh) + pad)
            if ax1 <= ax0 or ay1 <= ay0:
                continue
            patch = frame[ay0:ay1, ax0:ax1]
            mask = np.ones(patch.shape[:2], dtype=bool)
            ix0, iy0 = int(x0 * fw) - ax0, int(y0 * fh) - ay0
            ix1, iy1 = int(x1 * fw) - ax0, int(y1 * fh) - ay0
            mask[max(0, iy0):max(0, iy1), max(0, ix0):max(0, ix1)] = False
            ring = patch[mask]
            if ring.size == 0:
                continue
            b, g, r = (int(v) for v in np.median(ring.reshape(-1, 3), axis=0))
            cue["fill"] = f"#{r:02X}{g:02X}{b:02X}"
    finally:
        capture.release()


def _intermediate_encoder(device: str) -> tuple[str, list[str]]:
    """High-quality intermediate encoder for `cleaned.mp4`: NVENC -cq 14 when available, else x264 -crf 12."""

    from flowocr.burn import encode

    if device != "cpu" and encode.usable("h264_nvenc"):
        return "h264_nvenc", ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "14", "-b:v", "0"]
    return "libx264", ["-c:v", "libx264", "-preset", "veryfast", "-crf", "12"]


def inpaint(job: Mapping[str, Any], reporter: Any) -> dict[str, Any]:
    """Erase original on-screen text with FlowOCR's LaMa inpainter, writing a video-only intermediate MP4.

    Nonoka's Go exporter owns the final encode, audio muxing, subtitle burn and atomic rename; this
    operation only produces `cleaned.mp4` covering `[start, end)` at constant source fps with colour
    metadata preserved.
    """

    from flowocr.burn import encode, inpaint as flow_inpaint, pipeline, plates

    source = Path(str(job["source_path"])).resolve()
    if not source.is_file():
        raise OcrFailure("missing_source", f"找不到源视频：{source}")
    plates_path = Path(str(job["plates_path"])).resolve()
    if not plates_path.is_file():
        raise OcrFailure("missing_plates", f"找不到底板 ASS：{plates_path}")
    out_path = Path(str(job["out_path"])).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = out_path.with_name(out_path.name + ".part.mp4")

    start = max(0.0, float(job.get("start") or 0.0))
    end = float(job.get("end") or 0.0)
    if end <= start:
        from flowocr.typeset import platecolor
        end = float(platecolor.duration(str(source)))
    if not (start < end < float("inf")):
        raise OcrFailure("invalid_range", f"抹字时间范围无效：{start}–{end}")

    device = str(job.get("device") or "auto").strip().lower()
    want_backend = "cpu" if device == "cpu" else "auto"
    preset = str(job.get("preset") or "default").strip().lower()
    if preset not in flow_inpaint.PRESETS:
        preset = "default"

    width, height = encode.src_size(str(source))
    fps = encode.src_fps(str(source))
    pl, _ = plates.load(plates_path.read_text(encoding="utf-8-sig"), (width, height))
    backend, why = flow_inpaint.backend_of(want_backend)
    if why:
        print(f"[inpaint] backend={backend}: {why}", file=sys.stderr, flush=True)
    cfg = flow_inpaint.PRESETS[preset]
    sess = flow_inpaint.session(cfg, backend, "fp16" if backend == "trt" else "fp32")
    inp = flow_inpaint.Inpainter(cfg, sess, float(fps), backend)

    enc_name, enc_args = _intermediate_encoder(device)
    col = pipeline.colors(str(source))
    matrix = pipeline.MATRIX[col["space"]]
    seek = ["-ss", f"{start:.3f}", "-t", f"{end - start:.3f}"]
    total = end - start

    reporter.stage_started("erase")
    procs: list[subprocess.Popen[Any]] = []
    frames = None
    writer = None
    count = 0
    started = time.perf_counter()
    last_report = 0.0
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        dec = subprocess.Popen(
            ["ffmpeg", "-v", "error", *seek, "-i", str(source), "-map", "0:V:0",
             "-vf", f"scale={width}:{height}:in_color_matrix={matrix}:in_range={col['range']}:flags={pipeline.SWS},fps={fps}",
             "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, creationflags=flags,
        )
        procs.append(dec)
        enc = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}",
             "-r", str(fps), "-i", "-", "-an", "-sn", "-dn",
             "-vf", ",".join([
                 f"scale=out_color_matrix={matrix}:out_range={col['range']}:flags={pipeline.SWS}",
                 "format=yuv420p",
                 f"setparams=colorspace={col['space']}:range={col['range']}:color_primaries={col['primaries']}:color_trc={col['trc']}",
             ]),
             *enc_args, "-pix_fmt", "yuv420p", "-f", "mp4", str(part_path)],
            stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags,
        )
        procs.append(enc)
        writer = pipeline.Writer(enc.stdin, pipeline.DEPTH)
        frames = pipeline.ahead(pipeline.decoded(dec, width, height, start, fps), pipeline.DEPTH)
        for ts, img in frames:
            writer.put(inp(img, *pl.at(ts)))
            count += 1
            now = time.perf_counter()
            if now - last_report >= 0.5:
                last_report = now
                done = max(0.0, min(total, ts - start))
                reporter.progress("erase", completed=round(done, 2), total=round(total, 2), unit="seconds")
        writer.close()
        writer = None
        enc_stderr = enc.stderr.read().decode("utf-8", errors="replace") if enc.stderr else ""
        if enc.wait() != 0:
            raise OcrFailure("inpaint_encode_failed", f"抹字中间视频编码失败：{enc_stderr.strip() or enc.returncode}")
        os.replace(part_path, out_path)
    except BaseException:
        for proc in procs:
            try:
                pipeline.stop_ffmpeg(proc)
            except Exception:
                pass
        if frames is not None:
            try:
                frames.close()
            except Exception:
                pass
        if writer is not None:
            try:
                writer.abort()
            except Exception:
                pass
        part_path.unlink(missing_ok=True)
        raise

    reporter.progress("erase", completed=round(total, 2), total=round(total, 2), unit="seconds")
    return {
        "cleaned_mp4": str(out_path),
        "backend": backend,
        "encoder": enc_name,
        "frames": count,
        "fps": float(fps),
        "start": start,
        "end": end,
        "elapsed": round(time.perf_counter() - started, 2),
    }


def engine_info() -> dict[str, Any]:
    """`{id, version, commit}` from the package's own `UPSTREAM.json`."""

    upstream = Path(__file__).resolve().parents[2] / "UPSTREAM.json"
    try:
        data = json.loads(upstream.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {"id": "flowocr", "version": str(data.get("engine_version") or ""), "commit": str(data.get("commit") or "")}


__all__ = ["OcrFailure", "check", "engine_info", "inpaint", "recognize", "to_ocr_document"]
