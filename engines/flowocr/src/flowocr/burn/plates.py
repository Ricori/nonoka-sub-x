"""inpainting 路线要抹哪里：从叠加 ASS 读阶段 4 打了标（Effect `fo-fx|plate`）的底板，给出每一帧在屏的底板和要抹的部分（视频像素）。

* 矩形：绘图的宽高加 `\\pos` / `\\move` 的位置，`\\move` 按它的时段线性插值（行解析和插值用 `typeset.platecolor.Ev`）。
* 要抹的部分：矩形 ∩ 那一刻的 `\\clip`。打字机靠 `\\t(a,b,\\clip(…))` 把裁剪框一段段拉宽，`\\t` 里线性插值；`\\clip` 是屏幕坐标、不随 `\\move` 走。
  没有 `\\clip` 就是整块，裁剪框是空的就这一刻不抹。
* 提前量 `LEAD`：底板出现前这么多秒就算在屏、消失后多算这么多秒（底板的时间来自 OCR 采样，原文可能比底板早出、晚消；多抹的只是还没出字的背景）。
  在屏的底板（分组、推理、沿用）和要抹的部分用同一个在屏时段，但**裁剪框和位置一律取当前时刻**：
  * 裁剪框：阶段 4 的打字机底板已经领先原文一个行高（`typeset.core.GROW_LEAD`），采样级的时刻也已往前放过一个采样间隔（阶段 3 `grow_times`）；
    再按 `LEAD` 之后的裁剪框展开会让涂抹区跑在原文前面一大截（gi-s2 20.0 s：一行 1.1 s 打完 1226 px，涂抹区右沿 1473、原文右沿 1184），
    多抹的那截背景被修补器换掉、看得出来（owner 2026-11-04 看烧片）。底板出现之前那段（`ms < 0`）是起始的裁剪框，打字机一般是空的、不抹。
  * 位置（`\\move`）：要抹的部分必须落在这一刻的矩形里，不然移动的底板会抹到原文前面去、把原文留下。
* 坐标：ASS 的 PlayRes 按比例换成视频像素（阶段 3 写的 PlayRes 就是视频尺寸，比例一般是 1），四舍五入到整数。
* 只认标，不按长相认：扩展效果画的框也是绘图，不该抹。稿里没有标却有绘图（旧阶段 4 出的）、或者标着的行不是一整块矩形
  （阶段 4 的底板取色把底板换成了小格）都报错，不静默地什么都不抹。
"""
from __future__ import annotations

from flowocr.typeset import assfile, core, platecolor

LEAD = 0.25


def shown_at(ev: platecolor.Ev, t: float) -> tuple[float, float, float, float] | None:
    """t 秒时这块底板实际画出来的部分（ASS 坐标）：`rect_at(t)` ∩ t 秒的 `\\clip`；画不出来是 None。"""
    r = ev.rect_at(t)
    m = platecolor.CLIP_INIT.search(ev.text)
    if not m:
        return r
    c = [float(v) for v in m.groups()]
    ms = (t - ev.start) * 1000
    for tm in platecolor.CLIP_STEP.finditer(ev.text):
        a, b, *dst = (float(v) for v in tm.groups())
        if ms >= b:
            c = dst
        elif ms > a:
            u = (ms - a) / (b - a)
            c = [x + (y - x) * u for x, y in zip(c, dst)]
        else:
            break
    x0, y0, x1, y1 = max(r[0], c[0]), max(r[1], c[1]), min(r[2], c[2]), min(r[3], c[3])
    return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None


class Plates:
    """按帧序取在屏的底板：事件按"开始 − 提前量"排好，`at(t)` 只往前滑（t 不减），不每帧扫全部底板。
    给出的顺序是稿里的行序（修补器的分组按顺序合并，顺序不同结果可能不同）。"""

    def __init__(self, evs: list[platecolor.Ev], scale: tuple[float, float] = (1.0, 1.0), lead: float = LEAD, mode: str | None = None):
        self.evs, self.sx, self.sy, self.lead = evs, scale[0], scale[1], lead
        self.mode = mode                        # 阶段 4 的底板模式（`[Script Info]` 的 FlowOCR Typeset 里的 plate=…）；读不到是 None
        self.sweep = platecolor.Sweep([((ev.start - lead, ev.end + lead), (i, ev)) for i, ev in enumerate(evs)])

    def px(self, r) -> tuple[int, int, int, int]:
        return (int(round(r[0] * self.sx)), int(round(r[1] * self.sy)), int(round(r[2] * self.sx)), int(round(r[3] * self.sy)))

    def at(self, t: float) -> tuple[list[tuple[int, int, int, int]], list[tuple[int, int, int, int]]]:
        """(在屏底板的矩形, 要抹的部分)，视频像素。"""
        on = [ev for _, ev in sorted(self.sweep.at(t), key=lambda x: x[0])]
        rects = [self.px(ev.rect_at(t)) for ev in on]
        shown = [self.px(r) for r in (shown_at(ev, t) for ev in on) if r is not None]
        return rects, shown


def load(text: str, size: tuple[int, int], lead: float = LEAD) -> tuple[Plates, list[str]]:
    """读叠加 ASS 的全文：返回底板（按 PlayRes 换成 `size` 的像素），和去掉底板行的全文各行（烧其余的行用）。"""
    doc = assfile.loads(text)
    info = doc.section("Script Info")
    res = {k: float(ln.split(":", 1)[1]) for ln in (info.lines if info is not None else [])
           for k in ("PlayResX", "PlayResY") if ln.startswith(k + ":")}
    pw, ph = res.get("PlayResX", 1920), res.get("PlayResY", 1080)          # 没写时按阶段 4 的口径（libass 自己的默认是 384×288；阶段 4 总会写）
    ts = next((ln.split(":", 1)[1] for ln in (info.lines if info is not None else []) if ln.startswith(core.INFO_KEY + ":")), "")
    mode = next((w[6:] for w in ts.split() if w.startswith("plate=")), None)
    evs, rest, drawn = [], [], 0
    for ln in text.splitlines():
        ev = platecolor.parse(ln)
        if ev is None or ev.fields[8] != core.EFFECT_PLATE:
            rest.append(ln)
            drawn += ev is not None and "\\p1" in ev.text
            continue
        if not platecolor.DRAW.search(ev.text):
            raise ValueError("叠加 ASS 的底板取过色（换成了小格），inpainting 读不出底板的矩形：阶段 4 不开取色（去掉 --opt plate_color=on）重出")
        evs.append(ev)
    if not evs and drawn:
        raise ValueError(f"叠加 ASS 里没有打了标（Effect {core.EFFECT_PLATE}）的底板，却有 {drawn} 条绘图：多半是旧版阶段 4 出的，用现在的阶段 4 重出")
    return Plates(evs, (size[0] / pw, size[1] / ph), lead, mode), rest
