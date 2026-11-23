"""从富信息 JSON **投影**出 SRT，外加叠加 ASS 共用的工具（时间、转义、量字宽、定字号）。

契约（`output-format-plan（已归档）`）：**导出器不做任何判断。**
切分（cue）、常驻 UI 的判定、并带的结果都已经写在 `-tracks.json` 里了，
这里只是按 flag 取舍、按 cue 排版。所以回抠、并带、剔 UI 的产物自动一致——
audit-4 C6（`--srt-only` 绕过了输出过滤）在结构上不可能再发生。

    python -m flowocr.output.export out/gs-gi-s2/gi-s2-tracks.json            # 每条轨一份 SRT

叠加字幕（把字压回原位）分两步：阶段 3 的字幕稿在 `flowocr.output.script`，阶段 4 的特效在 `flowocr.typeset`，
入口是 `render` 的预设（`script`，以及两步连跑的 `default` / `default_all` / `dev`；script-fx 计划）。
"""
from __future__ import annotations

import argparse
from pathlib import Path

from flowocr.artifacts import srtio            # noqa: E402
from flowocr.artifacts import tracksio         # noqa: E402

FONT_CN = "Microsoft YaHei"
"""叠加 ASS 用的字体。**挑的是这台 Windows 上真有的**——写一个不存在的字体名，
libass 会静默换一个替身，下面那个量出来的换算比例当场作废。"""

FONT_H_RATIO = {FONT_CN: 1.35}
"""框高 → `Fontsize` 的换算，**量出来的**（`output-format-plan（已归档）`）。

`Fontsize` 不是像素高：字形高只有它的 0.58–0.81 倍，而且**随字体和是不是全角而变**
（同一个 Fontsize 在 Meiryo 和 Yu Mincho 下差 20%）。一次性探针 `probe_ass_fontsize.py` 用 libass
渲一条已知字号的 ASS、量白色字形的外接框高，`ratio = Fontsize / 字形高`，取 CJK 正文那一列；
拉丁为主的素材偏小约 6%。换字体请重跑那个探针，别照抄。
"""


def h_ratio(font: str) -> float:
    """`FONT_H_RATIO` 里查不到的字体（用户在字幕稿里换了字体）按 `FONT_CN` 的比例算；阶段 4 会打一行提示。"""
    return FONT_H_RATIO.get(font, FONT_H_RATIO[FONT_CN])


FONT_INK_DY = {FONT_CN: 0.04}
"""竖直居中（`\an4` / `\an5` / `\an6`）时，字形墨迹的中心比锚点低多少（× 字号），**量出来的**：libass 按字体的行框居中，
微软雅黑的中日文墨迹在行框里偏下。2026-11-10 用 libass 渲 39 号、100 号各几行中日文，墨迹中心比锚点低 0.026–0.045 个字号（拉丁字 0.05），取 0.04；
阶段 4 把逐行摆的字往上挪这么多，墨迹才在原文行（和底板）的竖直中心上（没挪时 39 号字下面比上面少约 3 px 底板）。按字体取常数、不逐行量：
同一个框里各行的基线要齐，逐行按墨迹居中会让「一二三」这类扁字的行上下跳。换字体照这个量法重量。"""


def ink_dy(font: str) -> float:
    """`FONT_INK_DY` 里查不到的字体按 `FONT_CN` 的算。"""
    return FONT_INK_DY.get(font, FONT_INK_DY[FONT_CN])


INK_REF_SIZE = 48
"""量字宽用的参考字号。**墨迹宽和字号成正比**（2026-09-12 在 24/48/72 三档上量过，
三种字体、六种文本，比例逐档相同到千分之五以内），所以渲一次就能反推任意字号的宽。"""
INK_PAGE_H, INK_PAGE_W = 12_000, 4096
"""一页多高。**行高按这一页里最大的字号定**：固定行高时，调用方传进来的大字号（核对探针用的是
适配后的字号，最大过 100）会把字形伸进下一行，下一行量到的宽就成了上一行的（第一版的 31 倍"溢出"就是这个）。"""


def ass_doc(W: int, H: int, styles: list[str], events: list[str], comment: str = "",
            info: dict | None = None) -> str:
    """一份 ASS。`info` 是另加进 `[Script Info]` 的键：要留得住就写成键，Aegisub 会丢掉 `;` 开头的注释行。"""
    return "\n".join(
        ["[Script Info]", *([f"; {comment}"] if comment else []),
         "ScriptType: v4.00+", "WrapStyle: 2", "ScaledBorderAndShadow: yes",
         f"PlayResX: {W}", f"PlayResY: {H}", *(f"{k}: {v}" for k, v in (info or {}).items()), "",
         "[V4+ Styles]",
         "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
         "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
         "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
         "MarginL, MarginR, MarginV, Encoding", *styles, "",
         "[Events]",
         "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
         *events]) + "\n"


def render_ink(items: list[tuple[str, float, str]]) -> list[tuple[int, int, int, int]]:
    """把 `(字体, 字号, 单行文本)` 逐条用 **libass 真渲**（ffmpeg 的 subtitles 滤镜），
    返回每条白色墨迹的 `(宽, 高, 左留白, 右留白)`；渲不出墨迹的记全 0。
    左 / 右留白是墨迹到这一行字格（步进宽）左 / 右边的距离：一遍左对齐、一遍右对齐各渲一次量出来。
    libass 按字格对齐，墨迹在字格里不一定居中（「。」的墨迹在左半边），底板按墨迹算位置时要它（2026-10-05：
    「疑惑，……问你。」居中排，墨迹比按墨迹居中算的偏左 17 px，冲出底板）。

    为什么不拿字体文件的度量估（第一版就是这么做的，一次性探针 `probe_ass_fit.py` 量过）：
    libass 的 `Fontsize` 不是 em、各字体缩放口径不同（Yu Mincho 估宽系统性偏大 17%），
    而且**字体里没有的字会被 libass 回退到别的字体**（Consolas 渲日文 −15%～+40%）——
    估不准的地方正好是最该量的地方。一页两次 ffmpeg（左右各对齐一遍），几百行一两秒。"""
    import subprocess
    import tempfile
    from PIL import Image
    out: list[tuple[int, int, int, int]] = []
    row_h = int(max((s for _, s, _ in items), default=INK_REF_SIZE) * 2)
    pad = 8
    per_page = max(1, INK_PAGE_H // row_h)
    with tempfile.TemporaryDirectory(prefix="flowocr-ink-") as d:
        for p0 in range(0, len(items), per_page):
            page = items[p0:p0 + per_page]
            H = row_h * len(page)
            fonts = sorted({f for f, _, _ in page})
            styles = [f"Style: s{i},{f},{INK_REF_SIZE},&H00FFFFFF,&H000000FF,&H00000000,"
                      "&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1" for i, f in enumerate(fonts)]
            boxes = {}
            for an, x in ((7, pad), (9, INK_PAGE_W - pad)):           # 左对齐量左留白，右对齐量右留白
                events = [f"Dialogue: 0,0:00:00.00,0:00:01.00,s{fonts.index(f)},,0,0,0,,"
                          + "{" + f"\\an{an}\\pos({x},{k * row_h + 4})\\fs{s:g}" + "}" + ass_text(t)
                          for k, (f, s, t) in enumerate(page)]
                (Path(d) / "ink.ass").write_text(ass_doc(INK_PAGE_W, H, styles, events), encoding="utf-8")
                # 相对文件名 + cwd：filter 语法里 Windows 盘符那个冒号不好转义（同 probe_ass_fontsize）
                r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                                    f"color=c=black:s={INK_PAGE_W}x{H}:d=1", "-vf", "subtitles=ink.ass",
                                    "-frames:v", "1", "ink.png"], capture_output=True, text=True, cwd=d)
                if r.returncode != 0:
                    raise SystemExit(f"字宽渲染失败（ffmpeg）：{r.stderr.strip()[:300]}")
                img = Image.open(Path(d) / "ink.png").convert("L").point(lambda v: 255 if v > 128 else 0)
                boxes[an] = [img.crop((0, k * row_h, INK_PAGE_W, (k + 1) * row_h)).getbbox() for k in range(len(page))]
            for lb, rb in zip(boxes[7], boxes[9]):
                out.append((lb[2] - lb[0], lb[3] - lb[1], lb[0] - pad, INK_PAGE_W - pad - rb[2]) if lb and rb else (0, 0, 0, 0))
    return out


SIDE = "\0side"
"""字宽表里另记左右留白的键 `(SIDE, 字体, 文本)` → `(左留白, 右留白)`（字号 1 时）。和墨迹宽放在同一张表里，
阶段 3 量好的交给阶段 4 时一起带过去；只查 `(字体, 文本)` 的地方碰不到它。"""


def ink_widths(pairs, known: dict | None = None) -> dict:
    """`{(字体, 单行文本): 字号 1 时的墨迹宽}`，另带 `{(SIDE, 字体, 单行文本): (左留白, 右留白)}`（`ink_side`）。
    同一行文本只渲一次；`known` 里已经量过的不再渲（叠加预设连跑阶段 3、4 时，阶段 4 用阶段 3 量好的）。返回的包含 `known` 里的。"""
    known = known or {}
    uniq = sorted({(f, ln) for f, t in pairs for ln in t.split("\n") if ln.strip()} - set(known))
    got = render_ink([(f, INK_REF_SIZE, ln) for f, ln in uniq]) if uniq else []
    out = {**known}
    for k, r in zip(uniq, got):
        out[k] = r[0] / INK_REF_SIZE
        out[(SIDE, *k)] = (r[2] / INK_REF_SIZE, r[3] / INK_REF_SIZE)
    return out


def ink_side(font: str, text: str, widths: dict) -> tuple[float, float]:
    """字号 1 时这一行的（左留白, 右留白）；没量过是 (0, 0)。"""
    return widths.get((SIDE, font, text), (0.0, 0.0))


FIT_MIN_SIZE = 8


def fit_size(text: str, font: str, box_w: float, box_h: float,
             widths: dict[tuple[str, str], float]) -> int:
    """给一条事件定字号：**框高和框宽各给一个上限，取小的**。

    只按框高定（上一版）时，字体比屏幕上的字宽、或者 OCR 多读 / 读错了字，渲出来就冲出框外，
    用途 1 的 overlay 会盖到邻居（2026-09-12 owner 看预览时指出）。
    宽度上限拿 `ink_widths` 真渲出来的宽反推：`Fontsize = 框宽 / 字号 1 时的墨迹宽`。"""
    lines = [ln for ln in text.split("\n") if ln.strip()] or [text]
    by_h = box_h / len(lines) * h_ratio(font)
    w1 = max(widths.get((font, ln), 0.0) for ln in lines)
    by_w = box_w / w1 if w1 > 0 else by_h
    return max(FIT_MIN_SIZE, int(min(by_h, by_w)))


def ass_time(us: int, up: bool) -> str:
    """µs → `H:MM:SS.cc`。ASS 只到厘秒：**起点向上取整、终点向下取整**，
    免得两条相邻事件重叠出一个假的"同屏"（`output-format-plan（已归档）`）。"""
    cs = -(-us // 10_000) if up else us // 10_000
    cs = max(cs, 0)
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6_000)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def ass_text(s: str) -> str:
    """正文进 ASS：换行变 `\\N`，花括号会被当成覆盖标签，换成半角括号。"""
    return s.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", "\\N")


def cue_start(doc: dict, cue: dict, evs: list[dict], start: str) -> int:
    """这条 cue 用哪个起点。**这是投影，不是产物里的状态**（第二轮复审）：

    `full`（"整条打完"）= 各成员 `t_full` 的**最大值**，夹在 cue 里。
    逐行各算各的是错的：一条字幕里说话人名短、打完得早，台词长、打完得晚，
    取最早那行会把起点拉回"首字出现"——整片实测起点误差 0.100 s 退回 0.400 s。

    ⚠ **算完不许写回 `cue["t_start"]`**：写回去之后那个值就不再等于任何事件的
    首字时间，下一次想切回 `first` 就配不上、**回不去了**。
    两个起点口径必须都能从同一份 JSON 投影出来。
    """
    lo, hi = cue["t_start"], cue["t_end"]
    if start != "full":
        return lo
    fulls = [e["t_full"] for e in evs if e.get("t_full") is not None]
    return max(lo, min(max(fulls), hi - 1)) if fulls else lo


def export_srt(doc: dict, outdir: Path, only: str = "",
               suffix: str = "", start: str = "first") -> list[tuple[str, int]]:
    """每条轨一份 SRT。**和 `build_tracks` 写出来的应当逐字节相同**——
    两边都是"同一份 cue 表 + 同一份 ui_lines"的投影。

    `start="full"` 只改这次导出的起点口径，**不改 JSON**（见 `cue_start`）。
    """
    out = []
    for tr in doc["tracks"]:
        if only and tr["id"] != only:
            continue
        blocks = []
        for cue in tr["cues"]:
            evs = tracksio.cue_lines(doc, tr, cue)
            if evs:
                blocks.append((cue_start(doc, cue, evs, start), cue["t_end"],
                               [e["text"] for e in evs]))
        name = tr["srt"][:-4] + suffix + ".srt" if suffix else tr["srt"]
        n = srtio.write_srt_blocks(outdir / name, blocks)
        out.append((name, n))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="从 -tracks.json 导出每条轨的 SRT（叠加 ASS 走 flowocr.output.render）")
    ap.add_argument("tracks")
    ap.add_argument("--outdir", default="", help="默认写在 tracks.json 旁边")
    ap.add_argument("--track", default="", help="只导某一条轨")
    ap.add_argument("--start", choices=("first", "full"), default="first",
                    help="cue 起点口径：first = 首字出现（默认，和建轨写的 SRT 相同）；full = 全字出现（贴合以稳定态计时的工具），"
                         "文件名加 -full、不覆盖默认那份。起点口径只在导出时投影，不改 tracks.json")
    a = ap.parse_args(argv)
    src = Path(a.tracks)
    doc = tracksio.load(src)
    outdir = Path(a.outdir) if a.outdir else src.parent
    outdir.mkdir(parents=True, exist_ok=True)
    for name, n in export_srt(doc, outdir, a.track, suffix="-full" if a.start == "full" else "", start=a.start):
        print(f"{name}  {n} 条 cue")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
