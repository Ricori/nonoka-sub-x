"""`python -m flowocr.typeset`：阶段 4 的命令行。见包的文件头。"""
from __future__ import annotations

import argparse
from pathlib import Path

from flowocr.typeset import core


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="阶段 4：字幕稿 -> 最终叠加 ASS（可以反复重跑；--restore 还原字幕稿）")
    ap.add_argument("ass", help="字幕稿（*.script.ass），或阶段 4 自己的输出")
    ap.add_argument("-o", "--out", default="", help="默认 x.script.ass -> x.ass，其余 x.ass -> x.fx.ass；--restore 时 -> x.script.ass")
    ap.add_argument("--preset", default="default", choices=list(core.LOOKS), help="长什么样")
    ap.add_argument("--opt", action="append", default=[], metavar="K=V",
                    help="plate（box / rows / text / src）、plate_alpha（如 0x20，只管近黑底板）、plate_color（on / off：底板颜色取自画面，要读源视频）、"
                         "typewriter（on / dim / off）、fade（on / off）、unknown（error / ignore：不认识的 fo 参数）")
    ap.add_argument("--video", default="", help="底板取色读的源视频（默认按字幕稿 [Script Info] 的 FlowOCR Video）")
    ap.add_argument("--fx", action="append", default=[], metavar="模块",
                    help="扩展效果：内置名 / 模块名 / .py 路径，可多次（见 flowocr.typeset.fx）")
    ap.add_argument("--restore", action="store_true", help="只还原：删掉生成行，把源行恢复成字幕稿")
    ap.add_argument("--force", action="store_true", help="--restore 时输出的字幕稿已经在也覆盖（默认不覆盖：可能是改过的）")
    a = ap.parse_args(argv)
    if a.restore:
        out, n = core.restore_file(Path(a.ass), Path(a.out) if a.out else None, a.force)
        print(f"-> {out}（还原 {n} 条源行）")
        return 0
    opts = {}
    for kv in a.opt:
        if "=" not in kv:
            ap.error(f"--opt 要 K=V：{kv}")
        k, v = kv.split("=", 1)
        opts[k] = v
    core.typeset_file(Path(a.ass), Path(a.out) if a.out else None, a.preset, opts, a.fx, video=a.video or None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
