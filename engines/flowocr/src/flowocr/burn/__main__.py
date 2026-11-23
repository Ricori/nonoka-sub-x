"""`python -m flowocr.burn`：阶段 5 的命令行。见包的文件头。"""
from __future__ import annotations

import argparse
from fractions import Fraction
from pathlib import Path

from flowocr.burn import encode, pipeline


def size_of(s: str) -> tuple[int, int]:
    w, h = s.lower().split("x")
    return int(w), int(h)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="flowocr-burn", description="阶段 5：把阶段 4 的叠加 ASS 烧进源视频（默认原样烧；--inpaint 先修补掉底板盖住的原文）")
    ap.add_argument("ass", help="阶段 4 的叠加 ASS")
    ap.add_argument("--video", default="", help="源视频（默认按 ASS 的 [Script Info] 里的 FlowOCR Video）")
    ap.add_argument("--inpaint", nargs="?", const="default", choices=("default", "hq"),
                    help="走 inpainting：default（快）/ hq（慢一些、补得更好）")
    ap.add_argument("--codec", default="auto", help=f"编码器：auto（H.265 硬件 → H.264 硬件 → libx264）| {' | '.join(encode.ENCODERS)}")
    ap.add_argument("--quality", help="质量值（CRF / NVENC 的 cq，越大文件越小）| auto = 按目标 VMAF 在源上搜；"
                                      f"不给：不短于 {encode.AUTO_MIN_S // 60} 分钟就搜，否则用编码器的保守默认值")
    ap.add_argument("--target", type=float, default=encode.TARGET, help="搜质量值时的目标 VMAF（抽样帧均值）")
    ap.add_argument("--fps", type=Fraction, help="输出帧率（默认按源）")
    ap.add_argument("--size", type=size_of, metavar="宽x高", help="输出尺寸（默认按源）")
    ap.add_argument("--start", type=float, help="只烧这一段：起点（秒）")
    ap.add_argument("--end", type=float, help="只烧这一段：终点（秒）")
    ap.add_argument("-o", "--out", type=Path, help="默认和 ASS 放在一起：x.ass -> x.mp4（inpainting 是 x.inpaint.mp4）")
    a = ap.parse_args(argv)
    if a.quality not in (None, "auto") and not a.quality.isdigit():
        ap.error(f"--quality 要整数或 auto：{a.quality}")
    try:
        job = pipeline.plan(Path(a.ass), a.video or None, a.out, bool(a.inpaint), a.start, a.end, a.size, a.fps,
                            a.codec, a.quality, a.target)
    except ValueError as ex:
        ap.error(str(ex))
    if job.probe:
        p = job.probe
        print(f"[阶段 5] 质量值 {p['q']}（VMAF {p['vmaf']:.2f}{'' if p['met'] else '，到下限仍不达标'}，"
              f"默认值 {p['default_q']} 时 {p['default_kbps']} kbps → {p['kbps']} kbps；试了 {p['tried']}，{p['search_s']} s）", flush=True)
    pipeline.burn_inpaint(job, a.inpaint) if a.inpaint else pipeline.burn_plate(job)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
