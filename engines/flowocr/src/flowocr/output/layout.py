"""叠加字幕的排版：字号、锚点、底板、画面边界、按轨迹逐段运动。阶段 3（字幕稿要写预览位置）和阶段 4（生成最终行）共用这一份。

方案：overlay-style 计划（对齐、锚点、底板）、script-fx 计划（拆成字幕稿 + 特效渲染之后这些从 `overlay` 挪到这里）。
这里只做几何，不做判断：对齐方式是阶段 2 判好的，效果时刻是阶段 3 判好的。
"""
from __future__ import annotations

import re

from flowocr.artifacts import tracksio
from flowocr.output import export

PLATE_PAD_PX = 2
PLATE_PAD_EM = 0.05
"""底板比 OCR 框（和正文）外扩 `PLATE_PAD_PX` + `PLATE_PAD_EM` × 字号（`plate_pad`）：盖住原字笔画的边缘、字周围留点边，按字号缩放。
2026-10-05 owner 看对比图后从 6 px 加到 14 px（6 时字贴着底板边、显挤）；2026-11-10 owner 看 14 px 的预览嫌比字大一圈，改成按字号的窄边距
（固定 14 px 在绝区零主播布局十几 px 的字上差不多是一个字宽）。"""


def plate_pad(size: float) -> float:
    """字号 `size` 的条目，底板四周外扩多少像素。"""
    return PLATE_PAD_PX + PLATE_PAD_EM * size
EDGE_PAD = 6
"""字离画面边的最小距离（锚轴往里推、扩出的行不出画面、译文字号的宽度上限）。和底板外扩分开：边距加大不该把贴满整屏的长译文字号压小一档。"""

PLATE_MODES = ("box", "rows", "text", "src")
"""底板怎么画（owner 2026-09-12 要三种比观感）：
* `box`：原字幕正文框的并集，一整块；
* `rows`：逐行一块，每行宽 = max(原文这一行的区间, 译文这一行的区间)——多行字幕不填满矩形；
* `text`：只有译文文字底下有底板；
* `src`：只盖原文各行（原文框外扩边距），译文超出原文的部分不加底板，更接近原生字幕（owner 2026-10-30；
  阶段 5 的 inpainting 路线默认用它，只抹原文）。调用方传原文框（`src_box`），`text_w` / `axis` / `text_h` 不看；
  整块画的行（单框、行数对不上）和别的方案一样取原文框的外接矩形，几个框之间的空隙也盖上。"""

OVERLAY_MIN_SCALE = 0.75
"""叠加字号不低于"按框高换算的字号"的这么多（owner 2026-09-12：第五个选项字被压得过小）。只给译文。"""

ANCHOR = {"left": 4, "center": 5, "right": 6}
"""对齐方式 → `\\an`（竖直居中，横向锚左沿 / 中心 / 右沿）。"""
ALIGN_OF = {v: k for k, v in ANCHOR.items()}


def text_span(axis: float, w: float, align: str) -> tuple[float, float]:
    """锚在 `axis` 上、宽 `w` 的一行字占的横向区间。"""
    if align == "left":
        return axis, axis + w
    if align == "right":
        return axis - w, axis
    return axis - w / 2, axis + w / 2


def anchor_axis(rows: list[list[float]], align: str, common: bool, fixed: float | None = None) -> list[float]:
    """每行的锚轴。`common`（matched 的多行）取整组的公共轴：居中用**最宽那行**的中心，左 / 右用最外的沿——
    `rows_of` 的行框只是这一行**被收下的**事件的并集，一行被 det 切成两框、其中一个被剔时只剩半行，
    拿它自己的中心当轴，这一行会歪半个行宽；缺了半截的那行不会是最宽的（两行时中位数就是平均，挡不住）。
    不 `common`（tracks 一个事件一行）取这一行自己的框。`fixed` 给了就各行都用它（`stacked_axes` 定的）。"""
    def own(r):
        return r[0] if align == "left" else r[2] if align == "right" else (r[0] + r[2]) / 2
    if fixed is not None:
        return [fixed] * len(rows)
    if not common:
        return [own(r) for r in rows]
    if align == "left":
        a = min(r[0] for r in rows)
    elif align == "right":
        a = max(r[2] for r in rows)
    else:
        a = own(max(rows, key=lambda r: r[2] - r[0]))
    return [a] * len(rows)


def row_span(r: list[float], text_h: float) -> tuple[float, float]:
    """一行竖直方向要盖到哪：原文框和正文（高 `text_h`、以这一行的中心为准）取并。"""
    c = (r[1] + r[3]) / 2
    return min(r[1], c - text_h / 2), max(r[3], c + text_h / 2)


def padded(r: list[float], pad: float) -> list[float]:
    """一个框外扩底板的边距（`plate_pad`）。"""
    return [r[0] - pad, r[1] - pad, r[2] + pad, r[3] + pad]


def row_plates(rows: list[list[float]], text_w: list[float], axis, pad: float, align: str = "center",
               text_h: float | list[float] = 0.0, text_only: bool = False) -> list[list[float]]:
    """逐行的底板（还没劈开上下交叠）：每行宽 = max(这一行的框, 译文区间)（`text_only` 只取译文区间），竖直按 `row_span`，外扩 `pad`。
    `axis`、`text_h` 同 `plate_rects`。"""
    axes = axis if isinstance(axis, list) else [axis] * len(rows)
    ths = text_h if isinstance(text_h, list) else [text_h] * len(rows)
    out = []
    for r, w, a, h in zip(rows, text_w, axes, ths):
        tx0, tx1 = text_span(a, w, align)
        x0, x1 = (tx0, tx1) if text_only else (min(r[0], tx0), max(r[2], tx1))
        y0, y1 = row_span(r, h)
        out.append(padded([x0, y0, x1, y1], pad))
    return out


def bands(rects: list[list[float]]) -> list[tuple[list[float], frozenset[int]]]:
    """几块可能互相重叠的矩形 → 不重叠的横条，盖住它们的并：按全部上下沿切成横条，每条横向取盖着它的那几块的最左到最右，
    盖着的那几块相同的相邻横条并成一条。返回（横条, 盖着它的矩形下标）。
    译文行数和原文整块的行数不同时（`place_rows` 的 n ≠ k），译文各行和原文各行上下错开，逐行配对的底板既盖不全原文、
    互相之间又会叠（半透明时叠的地方更黑），所以按横条拼。"""
    ys = sorted({v for r in rects for v in (r[1], r[3])})
    out: list[tuple[list[float], frozenset[int]]] = []
    for ya, yb in zip(ys, ys[1:]):
        cover = frozenset(i for i, r in enumerate(rects) if r[1] <= ya and r[3] >= yb)
        if not cover:
            continue
        if out and out[-1][1] == cover and out[-1][0][3] == ya:
            out[-1][0][3] = yb
            continue
        out.append(([min(rects[i][0] for i in cover), ya, max(rects[i][2] for i in cover), yb], cover))
    return out


def row_bands(rows: list[list[float]], text_w: list[float], axis, align: str, text_h: float | list[float],
              src_rows: list[list[float]], pad: float, extra: list[list[float]] = ()) -> list[tuple[list[float], frozenset[int]]]:
    """`rows` 方案在译文行数和原文整块不同时的底板（`place_rows` 的 n ≠ k）：译文各行（`row_plates`）、原文整块各行、
    `extra`（读音的矩形）都外扩边距，拼成不重叠的横条（`bands`），没有译文的原文行也盖上。
    盖着横条的下标按这个顺序数：译文行 0..n−1、原文行 n..n+k−1、`extra` 接在后面（阶段 4 跟着字长时按它找每条的几段）。"""
    return bands(row_plates(rows, text_w, axis, pad, align, text_h) + [padded(r, pad) for r in src_rows] + [padded(r, pad) for r in extra])


def plate_rects(rows: list[list[float]], text_w: list[float], axis, mode: str, pad: float,
                align: str = "center", text_h: float | list[float] = 0.0,
                src_rows: list[list[float]] | None = None) -> list[list[float]]:
    """每行一个底板矩形 [x0, y0, x1, y1]（`box` 模式一整块）。`axis` 是锚轴（一个数 = 各行共用，或逐行一个）；
    译文区间按锚点算（`text_span`）。上下相邻的两行外扩后会叠一条，半透明时叠的地方更黑，所以从中间劈开。
    **字不出底板**：`text_h` 是正文画出来的高（以这一行的中心为准），比原文框高时底板跟着长；`box` 也把译文的左右范围并进来
    （2026-10-05 owner 截图：两行原文框隔得远、字号按两框合起来的高定成 90 号，字形 67 px 高而底板只有 46 px）。
    `text_h` 可以逐行给（原文逐行字号时各行字形高不同，矮行的底板不该被撑到大字那行的高）。
    `src_rows`：译文按 `place_rows` 摆、行数和原文整块不同时的原文整块（`rows` 是摆出来的译文行），`box` 方案的一整块并进它；
    `text` / `src` 不看它，`rows` 方案这时用 `row_bands`。`pad`：外扩的边距（`plate_pad`）。"""
    p = pad
    axes = axis if isinstance(axis, list) else [axis] * len(rows)
    ths = text_h if isinstance(text_h, list) else [text_h] * len(rows)
    th_of = {id(r): h for r, h in zip(rows, ths)}

    def tall(r):
        return row_span(r, th_of.get(id(r), max(ths, default=0.0)))
    if mode == "src":
        out = [padded(r, p) for r in rows]
    elif mode == "box":
        spans = [text_span(a, w, align) for w, a in zip(text_w, axes)] if text_w and axis is not None else []
        extra = list(src_rows or [])
        x0 = min([r[0] for r in rows + extra] + [s[0] for s in spans])
        x1 = max([r[2] for r in rows + extra] + [s[1] for s in spans])
        y0 = min([tall(r)[0] for r in rows] + [r[1] for r in extra])
        y1 = max([tall(r)[1] for r in rows] + [r[3] for r in extra])
        return [[x0 - p, y0 - p, x1 + p, y1 + p]]
    else:
        out = row_plates(rows, text_w, axes, p, align, ths, text_only=mode == "text")
    for a, b in zip(out, out[1:]):
        if b[1] < a[3]:
            a[3] = b[1] = (a[3] + b[1]) / 2
    return out


def chunks(p: str, n: int = 24) -> list[str]:
    """长段量字宽时切小块再加：`render_ink` 一页 4096 px 宽，整段渲会被截断。"""
    return [p[i:i + n] for i in range(0, len(p), n)] or [p]


def width_keys(lines: list[str], block: bool) -> list[str]:
    """一条要量哪些字宽：普通条目量每一行（行里的换行拆开量），面板量每段切成的小块。"""
    if block:
        return [c for p in lines for c in chunks(p)]
    return [x for ln in lines for x in ln.split("\n")]


def line_width(font: str, text: str, widths: dict) -> float:
    """字号 1 时一行（可能带换行、按最宽的那段算）的墨迹宽。"""
    return max((widths.get((font, x), 0.0) for x in text.split("\n")), default=0.0)


def fit_line(rows: list[list[float]], lines: list[str], font: str, widths: dict, translated: bool, W: int) -> int:
    """普通条目的字号 = `export.fit_size`：框高按行数分、框宽按 libass 真渲的墨迹宽反推，取小的。
    译文另有下限：框很窄（选项按钮 `「未着」` 日文括号窄、中文括号宽）时不许缩到框高换算字号的
    `OVERLAY_MIN_SCALE` 以下，宁可让字超出框、底板跟着放宽；但不许超出画面。原文压回自己的框，只按框定
    （框偏高时保底会把字撑出框，gi-s2 摆帧）。"""
    box = [min(r[0] for r in rows), rows[0][1], max(r[2] for r in rows), rows[-1][3]]
    size = export.fit_size("\n".join(lines), font, box[2] - box[0], box[3] - box[1], widths)
    if not translated:
        return size
    # 保底按**看得见的行数**分框高：一个框里画几行（人在字幕稿里加了换行）时，保底不能按一行算，不然几行叠起来冲出框
    n = sum(len([x for x in ln.split("\n") if x.strip()]) or 1 for ln in lines)
    by_h = (box[3] - box[1]) / max(1, n) * export.h_ratio(font)
    w1 = max((line_width(font, ln, widths) for ln in lines), default=0.0)
    size = max(size, int(OVERLAY_MIN_SCALE * by_h))
    return min(size, int((W - 4 * EDGE_PAD) / w1)) if w1 > 0 else size


def expand_rows(rows: list[list[float]], k: int, H: int) -> list[list[float]]:
    """正文比原文框装得下的多出 `k` 行：按原来一行的高度（几个框时取行距）往下扩出 `k` 行，往下会出画面就往上扩。
    扩出的行横向取原文几行合起来的范围；压到别的字不管（owner 2026-09-27：一个框里分成两行时扩出一行，不为重叠做保证）。
    只给不是正文译文的行（tracks 的原文、名牌、选项里人加的换行）；正文译文按原文整块摆（`place_rows`）。"""
    rows = [list(r) for r in rows]
    if k <= 0:
        return rows
    h = rows[-1][3] - rows[-1][1]
    pitch = (rows[-1][1] - rows[0][1]) / (len(rows) - 1) if len(rows) > 1 else h
    x0, x1 = min(r[0] for r in rows), max(r[2] for r in rows)
    below = [[x0, rows[-1][1] + j * pitch, x1, rows[-1][1] + j * pitch + h] for j in range(1, k + 1)]
    if below[-1][3] <= H - EDGE_PAD:
        return rows + below
    return [[x0, rows[0][1] - j * pitch, x1, rows[0][1] - j * pitch + h] for j in range(k, 0, -1)] + rows


def expand_box(rect: list[float], cap: int, k: int, H: int) -> list[float]:
    """一个框本来装着 `cap` 行（OCR 原文带换行），正文多出 `k` 行：框按一行的高度（框高 / `cap`）长出 `k` 行，方向同 `expand_rows`。"""
    h = (rect[3] - rect[1]) / cap
    grown = expand_rows([[rect[0], rect[1] + i * h, rect[2], rect[1] + (i + 1) * h] for i in range(cap)], k, H)
    return [rect[0], grown[0][1], rect[2], grown[-1][3]]


VALIGNS = ("top", "bottom", "center")
"""文本框的竖直对齐（匹配器的文本框画像 `overlay.textbox` 的 `valign`，字幕稿 `fo` 的 `va`）：
行数变了时整块对齐原文整块的上沿 / 下沿 / 中心。判不出、没写时是 `top`。"""

EM_REF = "的一是了我不人在有这中大来上国个到说们为"
"""量"一个全角字多宽"的参考串（常用汉字，20 个）：`em_unit` 量它和它重复两遍的墨迹宽，差 / 字数就是步进宽（首尾字的留白抵掉）。"""
LATIN_REF = "We should head back to town and tell everyone what happened here."
"""量"一个拉丁字平均多宽"的参考句（对白里常见的英文，带空格和标点）：`latin_unit` 量它和它隔一个空格重复两遍的墨迹宽。"""
EM_KEYS = (EM_REF, EM_REF * 2, LATIN_REF, LATIN_REF + " " + LATIN_REF)
"""要量墨迹宽的串（阶段 3 / 4 量字宽时带上）。"""
LATIN_EM_DEFAULT = 0.55
"""游戏的版式文件没给 `latin_em`（英文一个拉丁字平均占几个原文一字宽）时用的值：三款量过的游戏原神 0.56、星铁 0.53、绝区零 0.66 的中间
（en-target 计划，英文版录像的断点尺上取的）。"""


def em_unit(font: str, widths: dict) -> float:
    """字号 1 时一个全角字的步进宽：`EM_REF` 重复两遍和一遍的墨迹宽之差 / 字数；没量过是 0。"""
    one, two = (widths.get((font, k), 0.0) for k in EM_KEYS[:2])
    return (two - one) / len(EM_REF) if one and two else 0.0


def latin_unit(font: str, widths: dict) -> float:
    """字号 1 时一个拉丁字（含空格、标点）的平均步进宽：`LATIN_REF` 隔一个空格重复两遍和一遍的墨迹宽之差 / 字数；没量过是 0。"""
    one, two = (widths.get((font, k), 0.0) for k in EM_KEYS[2:])
    return (two - one) / (len(LATIN_REF) + 1) if one and two else 0.0


LATIN_CHARS = "".join(chr(i) for i in range(33, 127)) + "—–‘’“”…"
"""逐字量步进宽的拉丁字（可打印 ASCII 和英文里常见的破折号、弯引号、省略号，空格另量）：英文折行按字形的真实宽度算（`latin_rel`）。
破折号、弯引号按 `cw` 是全角（东亚宽度不定），画在英文字号下其实各有各的宽，所以一起量。"""


def latin_keys() -> list[str]:
    """`latin_rel` 要量墨迹宽的串：每个字重复 10 遍和 20 遍（差 / 10 是步进宽，首尾留白抵掉），空格夹在两个 x 中间量。"""
    return [c * n for c in LATIN_CHARS for n in (10, 20)] + ["xx", "x" + " " * 10 + "x"]


def latin_rel(font: str, widths: dict) -> dict[str, float]:
    """每个拉丁字（含空格）的步进宽 / 平均拉丁字宽（`latin_unit`）：英文折行时一个字占 `latin_em × 这个比值` 个全角字。量不出是空表。"""
    a = latin_unit(font, widths)
    if a <= 0:
        return {}
    out = {c: (widths.get((font, c * 20), 0.0) - widths.get((font, c * 10), 0.0)) / 10 / a for c in LATIN_CHARS}
    out[" "] = (widths.get((font, "x" + " " * 10 + "x"), 0.0) - widths.get((font, "xx"), 0.0)) / 10 / a
    return {c: r for c, r in out.items() if r > 0}


def block_pitch(block: list[list[float]]) -> float:
    """原文整块的行距：几行时取首末两行**下沿**之差 / (行数 − 1)，一行时取它的高。
    按下沿量：Ruby 只把一行的上沿撑高（《魔裁》f1 带 Ruby 的首行框上沿 853，平常 868）。"""
    if len(block) > 1:
        return (block[-1][3] - block[0][3]) / (len(block) - 1)
    return block[0][3] - block[0][1]


def place_rows(block: list[list[float]], n: int, valign: str = "top", pitch: float | None = None,
               H: int | None = None) -> list[list[float]]:
    """译文 `n` 行摆在原文整块（`block`，k 行，按原文行号排，含匹配器补出的行）上的位置，阶段 3 的预览位置和阶段 4 都用它：

    * n = k：译文第 i 行摆在原文第 i 行的框上（原样）；
    * n ≠ k：按行距 `pitch`（没给就 `block_pitch` 现算）排出 n 行，整块按 `valign` 对齐原文整块的上沿 / 下沿 / 中心
      （量的是下沿：`top` 第 j 行的下沿 = 原文第一行的下沿 + j × 行距，`bottom` 从原文最后一行往上数，`center` 绕首末两行下沿的中点对称）；
      每行高取原文整块各行高的中位（偶数行取小的那个：两行里一行带 Ruby 时不被它撑高），横向取原文几行合起来的范围；出画面（`H`，离边 `EDGE_PAD`）就整块往里推。"""
    k = len(block)
    if n == k:
        return [list(r) for r in block]
    p = pitch if pitch else block_pitch(block)
    hs = sorted(r[3] - r[1] for r in block)
    h = hs[(k - 1) // 2]
    x0, x1 = min(r[0] for r in block), max(r[2] for r in block)
    b0, b1 = block[0][3], block[-1][3]
    if valign == "bottom":
        bottoms = [b1 - (n - 1 - j) * p for j in range(n)]
    elif valign == "center":
        bottoms = [(b0 + b1) / 2 + (j - (n - 1) / 2) * p for j in range(n)]
    else:
        bottoms = [b0 + j * p for j in range(n)]
    shift = 0.0
    if H is not None:
        top, bot = bottoms[0] - h, bottoms[-1]
        shift = EDGE_PAD - top if top < EDGE_PAD else (H - EDGE_PAD) - bot if bot > H - EDGE_PAD else 0.0
    return [[x0, b + shift - h, x1, b + shift] for b in bottoms]


def block_axes(rows: list[list[float]], block: list[list[float]], synth: set[int] | list[int], text_w: list[float],
               align: str, W: int) -> list[float]:
    """按 `place_rows` 摆的译文行（`rows`）的锚轴：整组共用一根，按原文整块里**真实的框**算（匹配器合成的行 `synth` 不算），
    不按译文占的那几个框——取法同 `anchor_axis` 的公共轴（防半行框）；最宽那行放不进画面就整组往里推（`axes_in_screen`）。"""
    real = [b for i, b in enumerate(block) if i not in set(synth)] or block
    return axes_in_screen(rows, text_w, align, True, W, anchor_axis(real, align, True)[0])


def body_size(rows: list[list[float]], lines: list[str], font: str, widths: dict, em: float | None,
              box_w: float | None, W: int, latin_em: float | None = None) -> int:
    """正文译文（matched 的 body / extra）的字号，阶段 3 和阶段 4 共用（line-breaks 计划 D1，owner 2026-11-09）：

    * 一个全角字宽 ≈ 原文的一字宽 `em`（`em_unit` 换算）；没有 `em` 退回 `fit_line`（按 `rows` 的框适配、有保底）；
      英文译文（给了 `latin_em`，按输出语种给、不看内容）改成一个拉丁字的平均宽 ≈ `latin_em × em`（`latin_unit` 换算；`latin_em` 是这款游戏英文版量出的）
      ——英文版的字体比中文版的拉丁字宽，照全角字换算会比英文版小一截（en-target 计划）；
    * 译文行比原文宽就顺着对齐方向往外画（锚轴不动，左对齐往右长），上限是文本框宽 `box_w`，没有就是画面宽 `W − 4·EDGE_PAD`；
      最宽那行超过上限就缩到刚好放进去，不设 `OVERLAY_MIN_SCALE` 那条下限（缩就是为了放进去）；整条一个字号。
    选项、名牌不走这里（`fit_line` + 同栏同字号）。"""
    if latin_em and (lu := latin_unit(font, widths)) > 0:
        unit = lu / latin_em
    else:
        unit = em_unit(font, widths)
    size = em / unit if em and unit > 0 else fit_line(rows, lines, font, widths, True, W)
    cap = box_w if box_w else W - 4 * EDGE_PAD
    w1 = max((line_width(font, ln, widths) for ln in lines), default=0.0)
    if w1 > 0 and w1 * size > cap:
        size = cap / w1
    return max(export.FIT_MIN_SIZE, int(size))


def words(p: str, k: int) -> list[str]:
    """英文段按词折成每行至多 `k` 个字（单词比 `k` 长就独占一行），空格留在上一行末尾：几行无分隔地拼回去就是原段。"""
    out = [""]
    for w in re.findall(r"\s*\S+\s*", p) or [p]:
        if out[-1] and len(out[-1]) + len(w.rstrip()) > k:
            out.append("")
        out[-1] += w
    return out


def same_size(boxes: list[tuple[int, int, list[float]]], sizes: list[int]) -> list[int]:
    """同一栏、同时在屏的选项按钮字号取中位数：各按钮的框高、读法（整句 / 残读并进来的）不一，
    各算各的就一个大一个小（hsr 0:23 摆帧：第二个选项比兄弟大一号）。`boxes[i]` = (起, 止, 第一行框)。"""
    out = []
    for s0, e0, bi in boxes:
        peers = [sz for (s1, e1, bj), sz in zip(boxes, sizes)
                 if s1 < e0 and s0 < e1
                 and min(bi[2], bj[2]) - max(bi[0], bj[0]) > 0.5 * min(bi[2] - bi[0], bj[2] - bj[0])]
        out.append(sorted(peers)[len(peers) // 2])
    return out


def axes_in_screen(rows: list[list[float]], text_w: list[float], align: str, common: bool, W: int,
                   fixed: float | None = None) -> list[float]:
    """锚轴，最宽那行放不进画面就把整组的轴往里推（左 / 右锚时译文比原文长会从一侧伸出去）。"""
    axes = anchor_axis(rows, align, common, fixed)
    spans = [text_span(a, w, align) for a, w in zip(axes, text_w)]
    lo, hi = min(s for s, _ in spans), max(z for _, z in spans)
    pad = EDGE_PAD
    shift = pad - lo if lo < pad else (W - pad) - hi if hi > W - pad else 0
    return [a + shift for a in axes]


def stacked_axes(items: list[tuple[int, int, list[float]] | None], text_w: list[float], W: int) -> list[float | None]:
    """名牌下面的称号（或上面的，不看上下）和名字共用一根锚轴：居中的单框名牌条目 `items[i]` = (起, 止, 原文框)，
    不参加的给 None。称号认它的名字（`tracksio.stack_partners`：时间上重叠、竖直上紧挨着、字明显比它大、两框的中心互相落在对方框里），
    名字和认它的称号都取名字的框中心当轴；不成对的是 None（照用自己的框）。
    轴放不进画面时按组里最宽的那条（`text_w[i]`：这条最宽一行的字宽）一起往里推：`axes_in_screen` 是逐条推的，
    长名字推了、短称号没推，中线就又错开。

    称号两边常有花纹（原神的「傭兵」），det 时而只框进一边（gi-s2：0:27 起框是 930–1024，中心 977，名字的中心 958），
    按自己的框居中就歪出十几到二十 px；名字的框没有花纹、各条之间稳定，所以小字跟着大字走。阶段 3 写预览位置、阶段 4 排版都过这一处。"""
    groups: dict[int, list[int]] = {}
    for i, j in enumerate(tracksio.stack_partners(items)):
        if j is not None:
            groups.setdefault(j, [j]).append(i)
    axes: list[float | None] = [None] * len(items)
    for j, members in groups.items():
        box = items[j][2]
        a = axes_in_screen([box], [max(text_w[k] for k in members)], "center", True, W, (box[0] + box[2]) / 2)[0]
        for k in members:
            axes[k] = a
    return axes


def block_layout(box: list[float], paras: list[str], row_h: int, font: str, widths: dict,
                 size: int | None = None, latin: bool = False) -> tuple[int, list[str]]:
    """长文本块：译文按段折行填进块里。字号从原文行高换算起，放不下就往小缩（`size` 给了就用它，只折行）；
    折行按每段的平均字宽手算（libass 的自动折行只在空格处断，中文整段不会折）；英文译文（`latin`，按输出语种给）只在空格处断，
    断处的空格留在上一行末尾（阶段 4 把行无分隔地拼回段落，词不粘连）。返回（字号, 折好的行，段间空一行）。"""
    bw, bh = box[2] - box[0], box[3] - box[1]
    unit = [sum(widths.get((font, c), 0.0) for c in chunks(p)) / max(1, len(p)) for p in paras]

    def layout(size: int) -> list[str]:
        out = []
        for p, u in zip(paras, unit):
            k = max(1, int(bw / max(u * size, 1e-6)))
            out += (words(p, k) if latin else [p[i:i + k] for i in range(0, len(p), k)]) + [""]
        return out[:-1]
    if size is not None:
        return size, layout(size)
    size = max(export.FIT_MIN_SIZE, int(row_h * export.h_ratio(font)))
    while size > export.FIT_MIN_SIZE and len(layout(size)) * size > bh:
        size -= 1
    return size, layout(size)


def motion_segments(boxes: list | None, s: int, e: int) -> list[tuple[int, int, tuple, tuple]]:
    """在动的事件在 `[s, e]` 里**按轨迹逐段拆**，每段 = (起, 止, 起点框左上角, 止点框左上角)。

    一条从首点到末点的 `\\move` 会把中间过程抹平（先静止后上移的字被画成从头匀速滑、步进滚动被画成平滑滚动），
    所以同一位置的相邻两点是一段停顿，位置变了才是一段位移。轨迹段被 `[s, e]` 裁掉一半时端点**跟着插值**——
    照搬原端点会把整段位移重播一遍。轨迹之外的尾巴停在最后一个位置。不动的（没有轨迹）返回一段、端点为 None。"""
    if not boxes or len(boxes) < 2:
        return [(s, e, None, None)]

    def at(b0, b1, t0, t1, t):
        f = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)
        return (b0[0] + (b1[0] - b0[0]) * f, b0[1] + (b1[1] - b0[1]) * f)

    out = []
    for (t0, b0), (t1, b1) in zip(boxes, boxes[1:]):
        a, z = max(s, t0), min(e, t1)
        if z <= a:
            continue
        if b0[:2] == b1[:2]:
            out.append((a, z, (b0[0], b0[1]), (b0[0], b0[1])))
        else:
            out.append((a, z, at(b0, b1, t0, t1, a), at(b0, b1, t0, t1, z)))
    last_t, last_b = boxes[-1]
    if e > max(s, last_t):
        out.append((max(s, last_t), e, (last_b[0], last_b[1]), (last_b[0], last_b[1])))
    return out or [(s, e, None, None)]


def pos_tag(x: float, y: float, seg: tuple, base: tuple[float, float]) -> str:
    """一个元素在一段里的位置标签：静止用 `\\pos`，位移用 `\\move`。`(x, y)` 是按 `base`（条目框的左上角）
    排出来的静止位置，在动的段把它平移到轨迹上。"""
    a, z, p0, p1 = seg
    if p0 is None:
        return "\\pos(%.0f,%.0f)" % (x, y)
    x0, y0 = x + p0[0] - base[0], y + p0[1] - base[1]
    if p0 == p1:
        return "\\pos(%.0f,%.0f)" % (x0, y0)
    x1, y1 = x + p1[0] - base[0], y + p1[1] - base[1]
    return "\\move(%.0f,%.0f,%.0f,%.0f,0,%d)" % (x0, y0, x1, y1, max(z - a, 0) // 1000)
