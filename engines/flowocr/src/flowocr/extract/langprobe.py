"""`--ocr-lang auto`：开跑前抽帧判一次识别语言（owner 2026-10-05）。

默认的 rec（v6 medium）字表没有韩文、西里尔字母、泰文，另有三个官方模型各管一种（`models.LANG_REC`）。`auto` 不知道视频是哪种，
就在片子里均匀取 `FRAMES` 帧，det 一次，把框**依次**送进每个候选 rec（一次只载一个，用完就关，不同时占几份显存），
按 Σ（每框置信度 × 字数）打分，只算置信度 ≥ `CONF_MIN` 的框。非默认模型要比默认模型高出 `MARGIN` 才选它，否则用默认（`zh`）——
三个另下的模型都认英文字母和数字，纯英文的画面上分数和默认差不多，按这条留在默认；帧里几乎没字（各模型读出的字最多也不到 `MIN_CHARS`）也用默认。
粒度是整部视频（或 `--start/--end` 窗口），不逐行判：逐行要每个框跑几个 rec。

**谁调它**：监督者（`supervisor.supervise`）和多组入口（`run_groups`）在起任何推理之前各调一次，作为**独立子进程**跑完退干净，
结果写进一个 JSON 文件、路径经环境变量 `supervisor.OCR_LANG_ENV` 传给 worker（`--workers` 切段的、退出码 75 整段重跑的、多组的各组都读同一份）。
worker 按它选 rec，把它记进 `_meta.ocr_lang_probe`；`config` 里照记 `auto`（复用判据比字面值，owner 2026-10-05 定）。

    python -m flowocr.extract.langprobe --probe-out x.json <run_ocr2 的参数…>          # 一般不手调
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

FRAMES = 30
MARGIN = 0.2
"""非默认模型的分数要比默认模型高出这么多（比例）才选它。"""
MIN_CHARS = 20
"""各模型在全部探测帧上读出的字（去掉空白）最多也不到这么多：帧里几乎没字，判不出，用默认。"""
MIN_BOX_H = 8
CONF_MIN = 0.8
"""只算这么高置信度的框：别的模型把读不懂的字读成乱码时字数多、置信度偏低（默认模型读泰文平均 0.81、泰文模型读日文 0.58–0.62），
全算的话乱码把分数抬上来。2026-10-05 在泰文合成片、英文片尾和已有样本里最接近的几段上离线比过（逐框转储重算）：0.8 时泰文合成片 th / zh 从 1.20 升到 1.33，
那几段里最高的非默认比值从 0.74 降到 0.43，英文片尾仍是 1.01（四个模型打平，留在默认）；按 0.8 整批实跑 14 段已有样本最高 0.46。
阈值是在同一批样本上挑的、没有留出集；泰文离门槛只差约一成，遇到真实泰文素材先复核。"""


def frame_times(t0: float, t1: float, n: int = FRAMES) -> list[float]:
    """[t0, t1] 里均匀取 n 个时刻（各段的中点，不取两端：片头片尾常是黑场）。"""
    span = max(0.0, t1 - t0)
    return [t0 + span * (k + 0.5) / n for k in range(n)]


def score(results: list[dict]) -> tuple[float, int]:
    """（Σ 置信度 × 字数, 字数），只算置信度 ≥ `CONF_MIN` 的框。字数不算空白。"""
    s, n = 0.0, 0
    for r in results:
        c = float(r.get("rec_score") or 0.0)
        if c < CONF_MIN:
            continue
        k = len("".join((r.get("rec_text") or "").split()))
        s += c * k
        n += k
    return s, n


def decide(scores: dict[str, tuple[float, int]]) -> tuple[str, str]:
    """各语言的（分数, 字数）-> （选哪个, 为什么）。`zh` 是默认模型。"""
    s0, n0 = scores.get("zh", (0.0, 0))
    if max((n for _, n in scores.values()), default=0) < MIN_CHARS:
        return "zh", f"帧里几乎没字（读出的字最多 {max((n for _, n in scores.values()), default=0)} 个 < {MIN_CHARS}），用默认"
    best = max((k for k in scores if k != "zh"), key=lambda k: scores[k][0], default=None)
    if best is not None and scores[best][0] > s0 * (1 + MARGIN):
        return best, f"{best} 的分数 {scores[best][0]:.1f} 比默认 {s0:.1f} 高出 {MARGIN:.0%} 以上"
    return "zh", f"没有哪个语言模型比默认（{s0:.1f}）高出 {MARGIN:.0%}"


def union_mask(groups, t: float):
    """几组范围在 t 时刻的并集（None = 全屏）。"""
    import numpy as np
    masks = [g.mask_at(t) for g in groups]
    if not masks or any(m is None for m in masks):
        return None
    return np.logical_or.reduce(masks)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="flowocr.extract.langprobe", description="--ocr-lang auto 的探测（监督者 / run_groups 调）")
    ap.add_argument("--probe-out", required=True, help="结果 JSON 写到哪")
    ap.add_argument("--probe-all-groups", action="store_true", help="--regions 里全部组的并集（run_groups 用）")
    ns, rest = ap.parse_known_args(argv)

    import cv2

    from flowocr import models
    from flowocr.extract import ocr_args, ortclient, recort, regions
    from flowocr.extract.fast_det import FastDet
    from flowocr.extract.run_ocr2 import gpu_blocker, onnx_path, ort_device
    args = ocr_args.build_parser().parse_args(rest)     # 只要视频、窗口、范围、设备；不过 resolve（不比配置）
    args.regions = args.regions or ""
    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        raise SystemExit(f"[判语言] 打不开 {args.video}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    dur = (cap.get(cv2.CAP_PROP_FRAME_COUNT) / fps) if fps > 0 else 0.0
    W, H = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    t0, t1 = args.start or 0.0, args.end or dur
    groups = []
    if args.regions:
        spec = json.loads(Path(args.regions).read_text(encoding="utf-8"))
        names = [g.get("name") for g in (spec.get("groups") or [{"name": "all"}])]
        pick = names if ns.probe_all_groups else [args.region_group or (names[0] if len(names) == 1 else "")]
        groups = [regions.from_spec(regions.group_spec(args.regions, n if len(names) > 1 else ""), W, H) for n in pick]
    frames = []
    for t in frame_times(t0, t1):
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
        ok, fr = cap.read()
        if ok:
            frames.append(regions.apply_mask(fr, union_mask(groups, t)) if groups else fr)
    cap.release()
    if not frames:
        raise SystemExit(f"[判语言] {args.video} 在 {t0:.1f}–{t1:.1f} s 里一帧都读不出来（解码器不支持或窗口越界），不悄悄退回默认语言")

    args.device = ocr_args.check_device(args.device)
    dev = "cpu" if args.device == "cpu" or (args.device == "auto" and gpu_blocker() is not None) else (
        "gpu" if args.device == "auto" else args.device)

    def client(spec: dict):
        try:
            return ortclient.OrtClient(spec, device=ort_device(dev)), dev
        except Exception as exc:                        # noqa: BLE001  探测只求判得出，GPU 起不来就在 CPU 上判
            if dev == "cpu":
                raise
            print(f"[判语言] {dev} 上起不来（{exc}），改在 CPU 上判", flush=True)
            return ortclient.OrtClient(spec, device="cpu"), "cpu"

    det_path = onnx_path(args, "det")
    det_cli, used = client({"det": det_path})
    try:
        det = FastDet.from_onnx(det_path, ortclient.make_det_runner(det_cli), default_yml_ok=True)
        crops = []
        for fr in frames:
            polys, _ = det.predict(fr)
            for p in polys:
                xs, ys = [q[0] for q in p], [q[1] for q in p]
                x0, y0, x1, y1 = max(0, int(min(xs))), max(0, int(min(ys))), int(max(xs)) + 1, int(max(ys)) + 1
                if y1 - y0 >= MIN_BOX_H and x1 > x0:
                    crops.append(fr[y0:y1, x0:x1])
    finally:
        det_cli.close()
    scores: dict[str, tuple[float, int]] = {}
    for lang, name in models.LANG_REC.items():
        path = models.path(name)                         # 缺了当场取（断网就在这里报错，不悄悄退回默认）
        cli, used = client({"rec": path})
        try:
            rec = recort.OrtRec(model=path, client=cli, pre="client")
            scores[lang] = score(rec.predict(crops, batch_size=16)) if crops else (0.0, 0)
        finally:
            cli.close()
    lang, why = decide(scores)
    out = {"lang": lang, "why": why, "frames": len(frames), "boxes": len(crops), "device": used,
           "scores": {k: {"score": round(v[0], 2), "chars": v[1]} for k, v in scores.items()},
           "window": [round(t0, 3), round(t1, 3)], "all_groups": ns.probe_all_groups}
    Path(ns.probe_out).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"[判语言] {lang}：{why}（{len(frames)} 帧、{len(crops)} 框；"
          + "、".join(f"{k} {v[0]:.1f}/{v[1]} 字" for k, v in scores.items()) + "）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
