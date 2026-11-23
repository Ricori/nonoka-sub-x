"""阶段 4 的实现：读一份字幕稿（或它自己的输出），按源行生成最终的叠加行。

方案：script-fx 计划。约定（artifacts.md 的字幕稿一节）：

* 源行 = `[Events]` 里 Effect 是 `fo:…`（阶段 3 写的参数）或空的 `Dialogue:`。处理过的源行改成 `Comment:`、
  Effect 前面加 `fo-src|`；生成行 Effect 标 `fo-fx`（底板标 `fo-fx|plate`，阶段 5 按它认出要抹的区域）、样式用派生的 `<Style>.fo-fx`。
  别的行（用户自己注释掉的、别人的 `fx`、`Banner;`…）原样留着。
* `restore` 删掉 `fo-fx` / `fo-fx|…` 行和派生样式、把 `fo-src|` 行恢复成源行，得到逐字节相同的字幕稿；处理之前先自动 `restore`，
  所以可以反复重跑。
* 带 `fo` 参数的源行按参数生成，**固定顺序**：排版（分行、字号、每行锚点、量文字边界）→ 元素（正文、底板、编号，
  外加扩展的元素）→ 运动（所有元素一起按 `path` 逐段展开）→ 时间效果（逐字 `\\alpha`、`\\fad`、配色）。
  不带 `fo` 的源行（手补的）照主流 tag 原样生成一条，只换样式。
* 谁说了算（owner 2026-09-27）：`fo` 的 `was` 记着阶段 3 写下的、归阶段 4 管的 tag 的值；源行里的值不一样就是人改过，
  这一项以人为准、管它的参数在这一行上作废，打一行提示。
* 占位（owner 2026-10-05）：正文里的 `` [`编号:值] ``（阶段 3 标的，图例在 `[Script Info]` 的 `FlowOCR Placeholder <编号>`）先换成值、
  值是空的删掉并收拾标点（`flowocr.placeholders.apply`），后面的排版都按换好的正文算；人已经把标记改成文字的就没什么可换。
* 打字机：`fo` 的 `tw`（首字出现到全字出现的毫秒数）逐字匀速显出；人在正文里写了 `\\k` 族 tag 的行按人写的节奏，`tw` 不用。
  打字机行的底板跟着字长（`grow`）：只盖原文已出现的部分 ∪ 译文已显出的部分，原文各行的出现时刻是 `fo` 的 `grow`。
* Ruby（owner 2026-10-23）：正文里的 `{ruby:读音}正文{/ruby}` 画成正文上方单独的一条小字（`ruby`），正文元素里不再写标记；
  阅读面板不画，整块画在 `\\move` 上的不画。
"""
from __future__ import annotations

import itertools
import re
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path

from flowocr import extensions, placeholders, provenance, richtext
from flowocr.output import export, layout
from flowocr.output import script as draft
from flowocr.typeset import assfile, grow, platecolor, ruby

EFFECT_SRC, EFFECT_FX = "fo-src|", "fo-fx"
"""处理过的源行 Effect 的前缀（后面是原来的 Effect，`restore` 去掉它）；生成行的 Effect。"""
EFFECT_PLATE = EFFECT_FX + "|plate"
"""底板行的 Effect（底板取色的小格也是）：阶段 5 的 inpainting 只认它，抹掉它盖住的区域；扩展效果画的绘图仍是 `fo-fx`，不抹。"""
FX_STYLE_SUFFIX = ".fo-fx"
"""派生样式名的后缀。`restore` 按它删派生样式，所以要一个用户不会自己起的名字（`.fx` 会撞上手起的 `Sign.fx`）。"""
INFO_KEY = "FlowOCR Typeset"
VIDEO_KEY = "FlowOCR Video"
"""`[Script Info]` 里源视频的路径（阶段 3 照产物的 provenance 写，`provenance.portable` 的写法）：底板取色要读它。"""

OVERLAY_COLOR = "101010"
"""底板颜色（ASS 的 BBGGRR）：近黑。透明度另给（`plate_alpha`）。"""
OFFMAIN_COLOR = "FFD773"
"""`dev` 里非主轨聚类的字色（BBGGRR）：#73d7ff 浅蓝（owner 2026-09-25：原来的淡红和白字区别不够明显）。"""
UI_COLOR = "33E0FF"
"""`dev` 里被判成常驻 UI 的字色（BBGGRR）：#ffe033 黄（owner 2026-09-25，原来是灰）。"""
LABEL_SCALE, LABEL_MIN = 0.6, 18
"""`dev` 的聚类编号：正文字号的这么多倍，不小于这个字号（ASS 的 Fontsize）。"""
LABEL_COLOR, LABEL_INSET = "2D2DFF", 3
"""`dev` 的聚类编号：字色 #ff2d2d（BBGGRR），画在这条字幕底板外接矩形的**内侧**右上角、离边这么多像素
（owner 2026-09-25：原来在底板外侧、太小，看不清也分不清属于哪条）。"""

FX_STYLE = {"OutlineColour": "&H00101010", "BackColour": "&H00000000", "BorderStyle": "1", "Outline": "2", "Shadow": "0"}
"""派生样式 `<Style>.fo-fx` 覆盖源样式的这几项：字幕稿的样式用 `BorderStyle=3` 出预览底框（没有描边），
生成行要的是现在叠加的样子（近黑描边 2、没有框、没有阴影）。字体、颜色、字号这些跟着源样式走。"""

TYPEWRITER_MODES = ("off", "on", "dim")
FO_PARAMS = {"ev", "key", "box", "va", "pitch", "em", "bw", "le", "syn", "fit", "rows", "cap", "plate", "path", "tw", "grow", "rowt", "wrap",
             "off", "why", "was", "spk"}
"""内置认得的 `fo` 参数；扩展效果的名字另加。不认识的当场报错（`unknown=ignore` 才跳过）。"""
KIND_OF_STYLE = {"Panel": "block", "Choice": "choice", "Offmain": "offmain", **dict.fromkeys(draft.NAME_STYLES, "name")}
GROW_LEAD = 1.0
"""底板跟着原文长时，原文的右沿往前领先这么多个行高（约一个字宽）：原文实际比匀速快时不露出一截。"""


@dataclass(frozen=True)
class Look:
    """阶段 4 的一个预设：长什么样。`label`：底板内侧右上角画区域号；`colors`：UI 黄字、非主轨浅蓝；
    `plate_color`：底板颜色取自画面（`platecolor`，要读源视频）。"""
    alpha: int
    plate: str
    typewriter: str
    fade: str
    label: bool
    colors: bool
    plate_color: str = "off"


LOOKS = {
    # owner 2026-10-31：底板默认不取色、只用近黑底（取色留作 --opt plate_color=on）
    "default": Look(0x20, "rows", "on", "on", False, False),
    # owner 2026-09-24：开发看——底板 50%、UI 黄字、非主轨浅蓝、底板内侧右上角红字区域号（颜色与编号位置 09-25 改）；
    # 底板不取色：半透明的近黑底板看得见底下的原文
    "dev": Look(0x80, "rows", "on", "on", True, True),
}


@dataclass
class Element:
    """一条生成行的内容：行首 tag 块是 `head` + 位置 + `tail` + 淡入淡出 + `tail2`，后面接 `body`。
    位置由 `(x, y)` 按轨迹算（`layout.pos_tag`），`where` 给了就直接用它（人改过位置的行）。
    `fade`：这个元素跟着源行淡入淡出；`tokens`：逐字显出的正文记号（`body` 由它按段重算）——
    在动的行按轨迹拆成几段时，时间效果按**源行**的时间算、换成各段的相对时间，不在每段从头放一遍。"""
    layer: int
    x: float
    y: float
    head: str
    tail: str
    body: str
    kind: str
    where: str | None = None
    tail2: str = ""
    fade: bool = False
    tokens: list | None = None
    effect: str = EFFECT_FX
    """这一行的 Effect：阶段 4 自己画的底板是 `EFFECT_PLATE`（`plate_el`），别的都是 `fo-fx`——扩展效果自称底板（kind 写 plate）的不打标。"""


@dataclass
class Line:
    """一条源行排好版的样子，扩展效果的挂点读它（只读）。"""
    style: str
    actor: str
    params: dict
    start_us: int
    end_us: int
    region: int
    kind: str
    rows: list[list[float]]
    lines: list[str]
    size: int
    align: str
    plates: list[list[float]] = field(default_factory=list)
    look: Look | None = None


# ---- 还原 ----

def source_video(info: assfile.Section | None, given: str | None = None) -> str:
    """源视频的本机路径：给了就是它，否则 `[Script Info]` 的 `FlowOCR Video`（`provenance.local` 换成本机路径）；都没有是空串。
    不核文件在不在：阶段 4 取色、阶段 5 烧录各自报各自的错。"""
    if given:
        return given
    v = next((ln.split(":", 1)[1].strip() for ln in (info.lines if info is not None else []) if ln.startswith(VIDEO_KEY + ":")), "")
    return str(provenance.local(v)) if v else ""


def is_fx(effect: str) -> bool:
    """阶段 4 的生成行：Effect 是 `fo-fx` 或 `fo-fx|<种类>`（如底板的 `EFFECT_PLATE`）。"""
    e = effect.strip()
    return e == EFFECT_FX or e.startswith(EFFECT_FX + "|")


def restore(doc: assfile.Doc, origin: list[int] | None = None) -> int:
    """删掉 `fo-fx` 行、派生样式和阶段 4 写的 `[Script Info]` 键，把 `fo-src|` 行恢复成源行（去掉前缀）。返回恢复的源行数。
    `origin` 给了就填上 `[Events]` 里留下的每一行原来是这一节的第几行（报提示时按用户打开的那份文件报行号）。"""
    n = 0
    ev = doc.section("Events")
    if ev is not None:
        names = assfile.format_of(ev) or []
        kept = []
        for i, ln in enumerate(ev.lines):
            r = assfile.parse_row(ln, names, assfile.EVENT_KINDS) if names else None
            if r is not None and is_fx(r.get("Effect")):
                continue
            if r is not None and r.kind == "Comment" and r.get("Effect").startswith(EFFECT_SRC):
                r.kind = "Dialogue"
                r.set("Effect", r.get("Effect")[len(EFFECT_SRC):])
                ln = str(r)
                n += 1
            kept.append(ln)
            if origin is not None:
                origin.append(i)
        ev.lines = kept
    st = doc.section("V4+ Styles")
    if st is not None:
        names = assfile.format_of(st) or []
        st.lines = [ln for ln in st.lines
                    if not ((r := assfile.parse_row(ln, names, ("Style",))) and r.get("Name").endswith(FX_STYLE_SUFFIX))]
    info = doc.section("Script Info")
    if info is not None:
        info.lines = [ln for ln in info.lines if not ln.startswith(INFO_KEY + ":")]
    return n


# ---- 读源行 ----

def cs_of(t: str) -> int:
    h, m, s = t.strip().split(":")
    return (int(h) * 3600 + int(m) * 60) * 100 + round(float(s) * 100)


def parse_was(v) -> dict[str, str]:
    if not isinstance(v, str):
        return {}
    return dict(x.split(":", 1) for x in v.split("|") if ":" in x)


def box4(s: str) -> list[float]:
    b = assfile.numbers(s)
    if len(b) != 4:
        raise ValueError(f"框要 4 个数（x0 y0 x1 y1），给的是 {s!r}")
    return b


def rows_of(v) -> list[list[float]]:
    return [box4(r) for r in str(v).split("|") if r]


def path_of(v) -> list:
    out = []
    for p in str(v).split("|"):
        t, b = p.split(":", 1)
        out.append([int(t), box4(b)])
    return out


def grow_of(v) -> list[tuple[int, int]]:
    """`grow=<首字>+<全字>|…`（毫秒，相对源行起点，和 `box` 的行一一对应）→ [(首字, 全字)]；全字早于首字的按首字算。"""
    out = []
    for seg in str(v).split("|"):
        s, f = seg.split("+")
        out.append((max(0, int(s)), max(0, int(s), int(f))))
    return out


def body_params(p: dict) -> dict:
    """正文译文的摆行参数（阶段 3 的 `script.body_fo` 写的）→ {va, pitch, em, bw, le, synth}。没写的（旧字幕稿、人手加的行）按缺省：
    `va` 取 `top`，`pitch` 是 None（`layout.place_rows` 按 `box` 现算），没有 `em` 字号用 `fit_line`，没有 `bw` 以画面宽为上限，没有 `le` 按全角字换算（不是英文），没有 `syn` 就是整块都是真实的框。"""
    def num(k: str) -> float | None:
        return float(p[k]) if p.get(k) not in (None, True) else None
    va = p["va"] if p.get("va") not in (None, True) else "top"
    if va not in layout.VALIGNS:
        raise ValueError(f"va 只能是 {' / '.join(layout.VALIGNS)}：{va!r}")
    syn = {int(v) for v in str(p["syn"]).split()} if p.get("syn") not in (None, True) else set()
    return {"va": va, "pitch": num("pitch"), "em": num("em"), "bw": num("bw"), "le": num("le"), "synth": syn}


def text_rows(text: str) -> tuple[list[list[tuple]], list[str], str, int | None]:
    """去掉 `fo` 之后的正文 → (逐行的记号, 逐行的纯文字, 行首块里不归阶段 4 管的 tag, 全字出现的时刻毫秒)。
    记号是 ("tags", 块内容) / ("ch", 字, 出现时刻毫秒或 None) / ("nl",)（行里的软换行 `\\n` 不算分行）。
    卡拉OK（人手写的节奏）：每个 `\\k` 族 tag 开一个音节，音节里的字在音节起点出现（音节起点 = 前面各音节时长之和，厘秒）；
    全字出现 = 最后一个音节的起点；没有卡拉OK是 None。
    Ruby 的标记块（`{ruby:读音}` / `{/ruby}`，`richtext.RUBY_BLOCK`）不是行首块，在正文开头也是行内的 ("tags", …)：
    `generate` 从记号里认出各处 Ruby（`ruby.rubies_of`）画成单独的元素，再把标记去掉。"""
    rows: list[list[tuple]] = [[]]
    t_cs, cur, has_k = 0, None, False
    lead = ""
    parts = assfile.blocks(text)
    for i, (kind, s) in enumerate(parts):
        if kind == "tags" and richtext.RUBY_BLOCK.fullmatch(s):
            rows[-1].append(("tags", s))
            continue
        if kind == "tags":
            kept = []
            for t in assfile.split_tags(s):                    # 顶层的 tag：\t(…,\fs80) 里面的不单算
                got = assfile.tag_of(t)
                if got and got[0] == "k":                      # 卡拉OK 计时换成逐字 \alpha，不留
                    cur = t_cs
                    t_cs += int(got[1])
                    has_k = True
                    continue
                if got and i == 0:                             # 行首块里归阶段 4 管的（\an \pos \move \fs \fad \bord）由阶段 4 重写
                    continue
                kept.append(t)                                 # 其余（行内的 \fs80、\1c、\t(…)…）原样留着
            rest = "".join(kept)
            if i == 0:
                lead = rest
            elif rest.strip():
                rows[-1].append(("tags", rest))
            continue
        j = 0
        while j < len(s):
            if s.startswith("\\N", j):
                rows.append([])
                j += 2
                continue
            if s.startswith("\\n", j):
                rows[-1].append(("nl",))
                j += 2
                continue
            ch = "\\h" if s.startswith("\\h", j) else s[j]              # 硬空格 \h 是一个字：逐字 \alpha 不能插进它中间
            rows[-1].append(("ch", ch, None if cur is None else cur * 10))
            j += len(ch)
    plain = ["".join(PLAIN.get(t[1], t[1]) for t in r if t[0] == "ch") for r in rows]
    return rows, plain, lead, cur * 10 if has_k else None


PLAIN = {"\\h": " "}
"""纯文字（量字宽、折行、给扩展效果看）里转义换成的字：`\\h` 是不断行的空格。"""


def with_tw(rows: list[list[tuple]], tw_ms: int, nl_slot: bool) -> list[list[tuple]]:
    """`tw` 匀速逐字：几行合起来的第 k 个位置（从 0 数）在 `tw_ms * k // (n - 1)` 毫秒出现——首字在起点、末字正好在全字出现。
    `nl_slot`：行里的换行（整块画在一条里的几行）也占一个位置（和拆之前逐字 `\\alpha` 的算法一样：tracks 事件本身带的换行算一个字）；
    按原框逐行摆的几行之间不占。"""
    kinds = ("ch", "nl") if nl_slot else ("ch",)
    n = sum(t[0] in kinds for r in rows for t in r)
    last, k, out = max(1, n - 1), 0, []
    for r in rows:
        row = []
        for t in r:
            if t[0] == "ch":
                row.append(("ch", t[1], tw_ms * k // last))
            else:
                row.append(t)
            k += t[0] in kinds
        out.append(row)
    return out


def with_rowt(rows: list[list[tuple]], rowt: list[tuple[int, int]], typing: bool) -> list[list[tuple]]:
    """逐行显字（`fo` 的 `rowt`，一行一段 `(首字, 全字)`）：每行在自己的 `[首字, 全字]` 里匀速逐字；
    这一行不打字（全字 = 首字），或阶段 4 关了打字机（`typing=False`），整行在首字时刻出现——按行起显是正确性，不是效果：
    一句一条之后，晚出现的行不能从条目起点就画出来。"""
    out = []
    for r, (f, full) in zip(rows, rowt):
        n = sum(t[0] == "ch" for t in r)
        last, k, row = max(1, n - 1), 0, []
        for t in r:
            if t[0] == "ch":
                row.append(("ch", t[1], f + (full - f) * k // last if typing and full > f else f))
                k += 1
            else:
                row.append(t)
        out.append(row)
    return out


def body_of(tokens: list[tuple], mode: str, full_ms: int | None = None, first_ms: int | None = None) -> str:
    """一行的正文：`on` 逐字 `\\alpha`（没出现的字全透明但照样占位置，居中的行不会边打边挪）；
    `dim` 全字出现（`full_ms`）之前整行半透明，给了 `first_ms`（逐行显字）时首字出现之前全透明；`off` 直接显出。"""
    if mode == "dim":
        plain = body_of(tokens, "off")
        if full_ms is None:
            return plain
        head = "\\alpha&HFF&\\t(%d,%d,\\alpha&H80&)" % (first_ms, first_ms + 1) if first_ms else "\\alpha&H80&"
        return "{%s\\t(%d,%d,\\alpha&H00&)}" % (head, full_ms, full_ms + 1) + plain
    out = []
    for t in tokens:
        if t[0] == "tags":
            out.append("{" + t[1] + "}")
        elif t[0] == "nl":
            out.append("\\N")
        elif mode == "on" and t[2] is not None:
            out.append("{\\alpha&HFF&\\t(%d,%d,\\alpha&H00&)}" % (t[2], t[2] + 1) + t[1])
        else:
            out.append(t[1])
    return "".join(out)


def rows_joined(rows: list[list[tuple]]) -> list[tuple]:
    """几行并成一行（行间 `\\N`）：一个原文框、译文却有几行时，整块画在这个框里。"""
    out: list[tuple] = []
    for k, r in enumerate(rows):
        if k:
            out.append(("nl",))
        out += r
    return out


@dataclass
class Src:
    """一条要处理的源行。"""
    no: int                                    # 文件里的行号（从 1 数），报提示用
    row: assfile.Row
    fo: dict | None
    text: str

    @property
    def style(self) -> str:
        return self.row.get("Style").strip()

    @property
    def actor(self) -> str:
        return self.row.get("Name")


# ---- 生成 ----

def derive_style(r: assfile.Row, name: str) -> str:
    """源样式 `r` → 派生样式 `<name>.fo-fx`（`FX_STYLE` 那几项换掉，其余照抄）。"""
    d = replace(r, fields=list(r.fields))
    d.set("Name", name + FX_STYLE_SUFFIX)
    for k, v in FX_STYLE.items():
        if k in r.names:
            d.set(k, v)
    return str(d)


def plate_el(rect: list[float], back: str, clip: str = "") -> Element:
    """一块底板：矩形 `[x0, y0, x1, y1]` 画成一条绘图（左上角锚点），`back` 是颜色和透明度；跟着源行淡入淡出。
    `clip`：跟着字长的底板的 `\\clip` 和它的 `\\t`（`grow`），静态的是空。"""
    x0, y0, x1, y1 = (round(v) for v in rect)     # 四条边各自取整再算宽高：位置和宽高分开取整，上下相邻的两块之间会空一像素的缝
    w, h = x1 - x0, y1 - y0
    return Element(0, x0, y0, "\\an7", "\\p1\\bord0\\shad0" + back + clip,
                   "m 0 0 l %.0f 0 %.0f %.0f 0 %.0f{\\p0}" % (w, w, h, h), "plate", fade=True, effect=EFFECT_PLATE)


def carry_rows(rows: list[list[tuple]]) -> list[list[tuple]]:
    """按原框逐行摆时每行是一条单独的 Dialogue；前面几行里的行内 tag（`A{\\1c&H0000FF&}B\\NCD` 的颜色）在源行里对下一行照样生效，
    所以按顺序补到下一行开头（后出现的覆盖先出现的，和 libass 读一整行时一样）。Ruby 的标记块不是状态、不补（`richtext.RUBY_BLOCK`）。"""
    out, carried = [], []
    for r in rows:
        out.append(([("tags", "".join(carried))] if carried else []) + r)
        carried += [t[1] for t in r if t[0] == "tags" and not richtext.RUBY_BLOCK.fullmatch(t[1])]
    return out


def runs_of(tokens: list[tuple]) -> list[list[tuple[str, float | None]]]:
    """一条的记号 → 看得见的每一行（行里的 `\\n` / 并起来的几行分开）按行内 `\\fs` 切成的几段 (文字, 字号；None = 整行字号)。
    `\\r` 回到样式的字号。`\\t(…,\\fs80)` 这种渐变不算（量的是起始的样子）。"""
    lines: list[list[tuple[str, float | None]]] = [[]]
    size, cur = None, ""
    for t in tokens:
        if t[0] == "tags":
            new = size_after(t[1], size)
            if new != size:
                if cur:
                    lines[-1].append((cur, size))
                cur, size = "", new
        elif t[0] == "nl":
            if cur:
                lines[-1].append((cur, size))
            cur = ""
            lines.append([])
        else:
            cur += PLAIN.get(t[1], t[1])
    if cur:
        lines[-1].append((cur, size))
    return lines


def size_after(block: str, size: float | None) -> float | None:
    """一个行内 tag 块之后的字号：`\\fs` 改字号，`\\r` 回到样式的（None）。`\\t(…,\\fs80)` 这种渐变不算。"""
    for tag in assfile.split_tags(block):
        got = assfile.tag_of(tag)
        if got and got[0] == "fs":
            size = float(got[1])
        elif re.fullmatch(r"\\r[^\\]*", tag.rstrip()):
            size = None
    return size


def cells_of(tokens: list[tuple]) -> list[list[tuple[str, int | None, float | None]]]:
    """一条的记号 → 看得见的每一行里逐字的 (字, 出现时刻毫秒或 None, 字号；None = 整行字号)。底板跟着译文长时按它算右沿。"""
    lines: list[list[tuple[str, int | None, float | None]]] = [[]]
    size = None
    for t in tokens:
        if t[0] == "tags":
            size = size_after(t[1], size)
        elif t[0] == "nl":
            lines.append([])
        else:
            lines[-1].append((PLAIN.get(t[1], t[1]), t[2], size))
    return lines


def tokens_width(tokens: list[tuple], size: int, font: str, widths: dict) -> float:
    """一条正文画出来的宽（像素）：看得见的每一行里各段的墨迹宽 × 各自的字号，取最宽的那行。行内没有 `\\fs` 时就是整行 × 字号。"""
    return max((sum(widths.get((font, s), 0.0) * (sz or size) for s, sz in ln) for ln in runs_of(tokens)), default=0.0)


def shift_times(block: str, a: int, dur: int) -> str:
    """一个 tag 块里人写的 `\\t(…)` / `\\fade(…)` 的时刻（相对源行起点）改成相对这一段的起点（源行起点后 `a` 毫秒）：
    段接段连起来和一整条相同，不在每段从头变一遍。`\\t` 没写时刻或结束时刻写 0 的是整条 `[0, dur]`（libass 把结束 0 读成到行尾）；
    时刻减完可以是负的（libass 照算进度），但段开始前已经变完的 `\\t` 换成里面的 tag 本身——结束时刻减到正好 0 会被当成到段尾。"""
    out = []
    for tag in assfile.split_tags(block):
        m = re.fullmatch(r"\\(t|fade)\((.*)\)\s*", tag, re.S)
        if not m:
            out.append(tag)
            continue
        args = split_args(m[2])
        if m[1] == "fade" and len(args) == 7:
            out.append("\\fade(%s,%s,%s,%d,%d,%d,%d)" % (*args[:3], *(int(float(v)) - a for v in args[3:])))
            continue
        if m[1] == "t":
            nums = [x for x in args if not x.lstrip().startswith("\\")]
            rest = ",".join(x for x in args if x.lstrip().startswith("\\"))
            t1, t2 = (float(nums[0]), float(nums[1]) or float(dur)) if len(nums) >= 2 else (0.0, float(dur))
            if t2 - a <= 0:
                out.append(rest)
                continue
            accel = nums[2] if len(nums) >= 3 else nums[0] if len(nums) == 1 else None
            out.append("\\t(%d,%d,%s%s)" % (t1 - a, t2 - a, accel + "," if accel else "", rest))
            continue
        out.append(tag)
    return "".join(out)


def split_args(s: str) -> list[str]:
    """tag 参数按顶层逗号切（`\\t(0,500,\\clip(1,2,3,4))` 里 clip 的逗号不切）。"""
    out, cur, depth = [], "", 0
    for c in s:
        if c == "," and depth == 0:
            out.append(cur)
            cur = ""
            continue
        cur += c
        depth += (c == "(") - (c == ")")
    out.append(cur)
    return out


def shift_tokens(tokens: list[tuple], off_ms: int, dur: int) -> list[tuple]:
    """逐字显出的时刻改成从这一段的起点（源行起点后 `off_ms`）算，在这之前已经出现的字直接显出；
    行内人写的 `\\t` / `\\fade` 也换算（`shift_times`）。"""
    out = []
    for t in tokens:
        if t[0] == "ch" and t[2] is not None:
            out.append(("ch", t[1], t[2] - off_ms if t[2] - off_ms > 0 else None))
        elif t[0] == "tags":
            out.append(("tags", shift_times(t[1], off_ms, dur)))
        else:
            out.append(t)
    return out


def fade_tag(fin: int, fout: int, dur: int, a: int, whole: bool) -> str:
    """源行 `[0, dur]` 毫秒上的淡入 `fin` / 淡出 `fout`，落在从 `a` 开始的这一段里的样子。整条就一段（`whole`）时是原样的 `\\fad`；
    在动的行按轨迹拆成几段时用七参数的 `\\fade`（透明度 255 = 全透明），段接段连起来和一整条淡入淡出相同，不在每段从头淡一遍。"""
    if not fin and not fout:
        return ""
    if whole:
        return "\\fad(%d,%d)" % (fin, fout)
    t3 = dur - fout - a if fout else dur - a + 1                       # 没有淡出：透明度到行尾都是 0
    if fin and a < fin:
        return "\\fade(%d,0,255,0,%d,%d,%d)" % (round(255 * (1 - a / fin)), fin - a, t3, max(t3, dur - a))
    if not fout:
        return ""
    if a < dur - fout:
        return "\\fade(0,0,255,0,0,%d,%d)" % (t3, dur - a)
    x = round(255 * (a - (dur - fout)) / fout)                         # 这一段从淡出中间开始
    return "\\fade(%d,%d,255,0,0,0,%d)" % (x, x, dur - a)


def text_hs(x: dict, size: int, tok: list[list[tuple]]) -> float | list[float]:
    """`text_h`，原文逐行字号（`x["sizes"]`）时逐行给：矮行（称号、并排的小标签）的底板不撑到大字那行的高。"""
    if x["mode"] == "rows" and x.get("sizes"):
        return [text_h(x, sz, tok) for sz in x["sizes"]]
    return text_h(x, size, tok)


def text_h(x: dict, size: int, tok: list[list[tuple]]) -> float:
    """正文画出来大约多高（底板竖直方向至少盖到这么高）：一行是字形高（字号 / `export.h_ratio`）；
    整块画的几行按每行一个字号的行距估（libass 的行距比字号略大，估低了几像素，底板的边距盖得住）。"""
    gh = size / export.h_ratio(x["font"])
    if x["mode"] == "rows":
        return gh
    n = max(1, len(runs_of(tok[0]))) if tok else 1
    return (n - 1) * size + gh


def ink_shift(tokens: list[tuple], size: int, font: str, widths: dict, align: str) -> float:
    """这一行墨迹相对"按墨迹宽、以锚点对齐"算出来的位置偏了多少（像素，右为正）：libass 按字格（步进宽）对齐，
    墨迹在字格里不一定居中——居中偏 (左留白 − 右留白) / 2，左对齐偏左留白，右对齐偏 −右留白（`export.ink_side`）。
    几个可见行（整块画的）取最宽那行（其余行在长底板里差几像素，多盖不露）；行内改过字号的按各段自己的字号。
    底板、长底板的译文左沿按它挪，字不动。"""
    lines = [ln for ln in runs_of(tokens) if ln]
    if not lines:
        return 0.0
    ln = max(lines, key=lambda r: sum(widths.get((font, s), 0.0) * (sz or size) for s, sz in r))
    (s0, z0), (s1, z1) = ln[0], ln[-1]
    lsb = export.ink_side(font, s0, widths)[0] * (z0 or size)
    rsb = export.ink_side(font, s1, widths)[1] * (z1 or size)
    return (lsb - rsb) / 2 if align == "center" else lsb if align == "left" else -rsb


def text_front(cells: list[tuple[str, int | None, float | None]], left: float, w: float, size: int, pad: float,
               dim_from: int | None) -> grow.Front:
    """一行译文显出到哪：逐字跳，右沿按字宽权重（全角 1、半角 0.5，乘这个字的字号）折算到这一行画出来的宽 `w` 上，再外扩底板的边距。
    宽度不定的（`east_asian_width` 是 `A`：`“ ” … — ·`）按全角算：叠加用的是中文字体，libass 实测引号、省略号、破折号都约一个字宽，
    按半个算刚显出的字会露一截；`·` 只有四分之一宽，多算是多盖、不露。
    没有逐字时刻的字（None）从 0 起就在；`dim_from` 不是 None 时是 `dim`：整行从这个时刻起就半透明地画着（逐行显字时是这一行的首字，
    不然是 0）——不能一律当 0：晚出现的那行在首字之前全透明，底板先出来就是一块没字的暗框。"""
    ws = [(1.0 if unicodedata.east_asian_width(c[:1]) in "WFA" else 0.5) * (sz or size) for c, _, sz in cells]
    tot, acc, pts = sum(ws), 0.0, []
    if tot <= 0:
        return []
    for (_, t, _), wt in zip(cells, ws):
        acc += wt
        pts.append((dim_from if dim_from is not None else 0 if t is None else t, left + w * acc / tot + pad))
    return grow.steps(pts)


def dialogue(layer: int, s_us: int, e_us: int, style: str, actor: str, text: str, effect: str = EFFECT_FX) -> str:
    return (f"Dialogue: {layer},{export.ass_time(s_us, True)},{export.ass_time(e_us, False)},"
            f"{style}{FX_STYLE_SUFFIX},{actor},0,0,0,{effect},{text}")


class Typesetter:
    """一次处理：`look` + 选项 + 扩展效果。`notes` 收提示（行号, 话），`counts` 收计数。"""

    def __init__(self, look: Look, fx_mods: tuple = (), unknown: str = "error", widths: dict | None = None,
                 video: str | None = None, window: tuple[float, float] | None = None):
        self.look = look
        self.video = video                      # 底板取色读的视频；None 就按字幕稿 [Script Info] 的 FlowOCR Video
        self.window = window                    # 底板只给和这段时间 [s, e)（秒）有交集的行取色（预览只出一段时）；None 是全部
        self.fx = {m.NAME: m for m in fx_mods}
        self.unknown = unknown
        self.widths = widths                    # 已经量好的字宽（连跑时阶段 3 交过来的），量过的不再起 ffmpeg
        self.H = 1080                           # 画面高（run 时从 PlayResY 读）：扩出行时别扩出画面
        self.notes: list[tuple[int, str]] = []
        self.counts: dict[str, int] = {}
        self.fills: dict[tuple[str, str], int] = {}  # （占位, 换成的值）-> 处数

    def count(self, k: str, n: int = 1) -> None:
        self.counts[k] = self.counts.get(k, 0) + n

    def run(self, doc: assfile.Doc) -> None:
        ev0 = doc.section("Events")
        base = (sum(len(s.lines) + (s.name is not None) for s in doc.sections[:doc.sections.index(ev0)]) + 1
                if ev0 is not None else 0)                       # 按输入文件数行号：restore 之前算
        origin: list[int] = []
        restore(doc, origin)
        info = doc.section("Script Info")
        def play_res(key: str, default: int) -> int:
            return int(next((ln.split(":", 1)[1] for ln in info.lines if ln.startswith(key + ":")), default)) if info else default
        W = play_res("PlayResX", 1920)
        self.H = play_res("PlayResY", 1080)
        st_sec, ev_sec = doc.section("V4+ Styles"), doc.section("Events")
        if ev_sec is None:
            raise ValueError("没有 [Events] 节，不是字幕稿")
        st_names = assfile.format_of(st_sec) or [] if st_sec else []
        styles = {r.get("Name").strip(): r for ln in (st_sec.lines if st_sec else [])
                  if (r := assfile.parse_row(ln, st_names, ("Style",)))}
        ev_names = assfile.format_of(ev_sec)
        if not ev_names or ev_names[-1] != "Text":
            raise ValueError("[Events] 的 Format 不对（最后一个字段要是 Text）")
        legend = placeholders.legend_of(info.lines) if info else {}
        srcs: dict[int, Src] = {}
        for i, ln in enumerate(ev_sec.lines):
            r = assfile.parse_row(ln, ev_names, assfile.EVENT_KINDS)
            if r is None or r.kind != "Dialogue":
                continue
            no = base + origin[i] + 1
            fo = assfile.parse_fo(r.get("Effect"))
            if fo is None and r.get("Effect").strip():
                self.notes.append((no, f"Effect 列是 {r.get('Effect').strip()[:40]!r}，不是源行，原样保留"))
                continue
            text, got = placeholders.apply(r.get("Text"), legend)
            for k, v in got:
                self.fills[(legend[k], v)] = self.fills.get((legend[k], v), 0) + 1
            if placeholders.MARK_RE.search(text):
                self.notes.append((no, "正文里有图例里没有的占位标记，原样留着"))
            srcs[i] = Src(no, r, fo, text)
        self.check_params(srcs.values())
        used = sorted({s.style for s in srcs.values()})
        fallback = styles.get("Default") or next(iter(styles.values()), None)
        base_style = {s: styles.get(s) or fallback for s in used}
        for s in used:
            if s not in styles:
                self.notes.append((0, f"样式 {s!r} 不存在，派生样式照 {fallback.get('Name').strip() if fallback else '（无）'} 生成"))
        fonts = {s: r.get("Fontname").strip() if r else export.FONT_CN for s, r in base_style.items()}
        for s, f in fonts.items():
            if f not in export.FONT_H_RATIO:
                self.notes.append((0, f"样式 {s!r} 的字体 {f!r} 没有量过框高换算（export.FONT_H_RATIO），字号适配按 {export.FONT_CN} 的比例"))
        laid, bad = {}, []
        for i, src in srcs.items():
            if src.fo is None:
                continue
            try:
                laid[i] = self.lay_out(src, fonts[src.style], W)
            except (ValueError, KeyError, IndexError) as e:        # 人手改坏的参数值（tw=1.5s、box 少一个数…）
                bad.append(f"第 {src.no} 行：{type(e).__name__}: {e}")
        if bad:
            raise ValueError(f"fo 参数的值读不了 {len(bad)} 处：" + "；".join(bad[:12]) + ("…" if len(bad) > 12 else ""))
        widths = export.ink_widths(((f, "\n".join(keys)) for f, keys in (x["measure"] for x in laid.values())),
                                   known=self.widths)
        for x in laid.values():
            self.fit(x, widths, W)
        self.same_size(laid)
        self.stack_axes(laid, widths, W)
        out, kinds = [], []                     # kinds：逐行的种类（源行 "src"、生成行是元素的 kind、其余 None），底板取色按它认行
        for i, ln in enumerate(ev_sec.lines):
            if i not in srcs:
                out.append(ln)
                kinds.append(None)
                continue
            src = srcs[i]
            gen = self.generate(laid[i], widths, W) if i in laid else [("plain", self.plain(src))]
            c = replace(src.row, fields=list(src.row.fields))
            c.kind = "Comment"
            c.set("Effect", EFFECT_SRC + src.row.get("Effect"))
            out.append(str(c))
            kinds.append("src")
            out += [g for _, g in gen]
            kinds += [k for k, _ in gen]
            self.count("源行")
            self.count("生成行", len(gen))
        derived = [derive_style(r, s) for s, r in base_style.items() if r is not None]
        if self.look.plate_color == "on" and "plate" in kinds:
            base = next((r for r in base_style.values() if r is not None), None)
            taken = {ln.split(":", 1)[1].split(",", 1)[0].strip() for ln in derived + (st_sec.lines if st_sec else [])
                     if ln.startswith("Style:")}
            pstyle = next(n for n in (f"{platecolor.STYLE}{k or ''}{FX_STYLE_SUFFIX}" for k in itertools.count())
                          if n not in taken) if base is not None else None
            # 主色不是默认白的样式（人改过的、阶段 3 写了游戏名牌色的 `Name` / `Title`，或第 3 步没有的样式）：正文照它，不自动改色
            mine = {s + FX_STYLE_SUFFIX for s, r in base_style.items()
                    if r is not None and (s not in draft.STYLES or r.get("PrimaryColour").strip().upper() != draft.STYLE_FILL)}
            got = platecolor.apply(out, kinds, self.video_of(info), W, self.H, window=self.window, style=pstyle, keep=mine)
            out = got.lines
            self.count("底板取色", got.sampled)
            self.count("底板小格", got.cells)
            self.count("底板取色·NVDEC" if got.hw else "底板取色·软解")
            if got.plates > got.sampled:
                self.notes.append((0, f"{got.plates - got.sampled} 块底板在视频结尾之后、没取到样，保留近黑底板"))
            if got.cells and base is not None:
                derived.append(platecolor.plate_style(base, pstyle))
        ev_sec.lines = out
        if st_sec is not None:
            last = max((k for k, ln in enumerate(st_sec.lines) if ln.startswith("Style:")), default=len(st_sec.lines) - 1)
            st_sec.lines[last + 1:last + 1] = derived
        if info is not None:
            lk = self.look
            val = f"plate={lk.plate} alpha=0x{lk.alpha:02X} typewriter={lk.typewriter} fade={lk.fade}" \
                  f"{' label' if lk.label else ''}{' colors' if lk.colors else ''}" \
                  f"{f' plate_color={platecolor.VERSION}' if lk.plate_color == 'on' else ''}"
            last = max((k for k, ln in enumerate(info.lines) if ln.strip()), default=-1)
            info.lines.insert(last + 1, f"{INFO_KEY}: {val}")

    def video_of(self, info) -> str:
        """底板取色读的视频：`--video` 给的，否则字幕稿 `[Script Info]` 的 `FlowOCR Video`（阶段 3 照产物记的）。找不到就报错——
        静默退回近黑底板会让人以为取过色。"""
        v = source_video(info, self.video)
        if not v or not Path(v).is_file():
            raise ValueError(f"底板取色要读源视频，{'找不到 ' + repr(v) if v else '字幕稿里没有 ' + VIDEO_KEY}："
                             "用 --video 指定，或 --opt plate_color=off 不取色")
        return v

    def check_params(self, srcs) -> None:
        known = FO_PARAMS | set(self.fx)
        bad = [(s.no, k) for s in srcs if s.fo for k in s.fo if k not in known]
        if bad and self.unknown != "ignore":
            where = "、".join(f"第 {no} 行 {k}" for no, k in bad[:12]) + ("…" if len(bad) > 12 else "")
            raise ValueError(f"不认识的 fo 参数 {len(bad)} 处：{where}。扩展效果用 --fx 加载；要跳过加 --opt unknown=ignore")
        for no, k in bad:
            self.notes.append((no, f"不认识的 fo 参数 {k!r}，跳过"))

    # -- 第 1 步：排版 --

    def lay_out(self, src: Src, font: str, W: int) -> dict:
        """解析一条带 `fo` 的源行，定下哪些参数有效（谁说了算）、要量哪些字宽。字号在 `fit` 里定。"""
        p = src.fo
        text = src.text
        rows_tok, plain, lead, full_ms = text_rows(text)
        was = parse_was(p.get("was"))
        pos, move, an, fad = (assfile.first_tag(text, k) for k in ("pos", "move", "an", "fad"))
        fs = assfile.first_tag(text, "fs", lead_only=True)      # 整行的字号；行内的 A{\fs80}B 只管后面几个字，不算

        def same(tag_args, recorded):                  # tag 的参数用逗号，fo 里记的用空格
            return tag_args is not None and assfile.numbers(tag_args) == assfile.numbers(recorded)
        moved = ("pos" in was and not same(pos, was["pos"])) or ("move" in was and not same(move, was["move"])) \
            or ("pos" in was and move is not None) or ("move" in was and pos is not None)
        realigned = "an" in was and not same(an, was["an"])
        if realigned:
            self.notes.append((src.no, f"对齐改过（\\an{an}）：以人为准，这一行按人给的锚点和位置画"))
        moved = moved or realigned
        resized = "fs" in was and not same(fs, was["fs"])
        box = rows_of(p["box"]) if p.get("box") else []
        kind = KIND_OF_STYLE.get(src.style, "line")
        if kind == "block" and not p.get("wrap"):
            kind = "line"
        if moved and not realigned:
            self.notes.append((src.no, "位置改过：以人为准，这一行不按原框逐行摆、不按轨迹走"))
        if resized:
            self.notes.append((src.no, f"字号改过（\\fs{fs}）：以人为准，不再适配"))
        mode = "free"
        fit = p.get("fit")
        cap = max(len(box), int(p["cap"]) if p.get("cap") not in (None, True) else 0)   # 原文框本来装几行（阶段 3 记的）
        base = tuple(box[0][:2]) if box else (0, 0)     # 轨迹 path 记的是原文框的位置：扩出行只改排版，不改这个基准
        src_box = [list(r) for r in box]                 # 扩行之前的原文框：底板跟着字长时原文只在这几行
        reflow = None
        if kind == "line" and fit == "tr" and p.get("rows") and box and not moved:
            # 正文译文：`box` 是原文整块，译文按它摆行（阶段 3 的 `view_rows` 同一函数、同一组参数）；人改过行数照样走这里
            reflow = body_params(p)
            box, mode = layout.place_rows(src_box, len(plain), reflow["va"], reflow["pitch"], self.H), "rows"
        elif kind != "block" and p.get("rows") and box and not moved:
            n = len(plain)
            if n == len(box):
                mode = "rows"
            elif n > cap == len(box):                      # 多出来的行按原来一行的高度扩出去，逐行摆
                box, mode = layout.expand_rows(box, n - cap, self.H), "rows"
            elif n > cap and len(box) == 1:                # 一个框本来装着几行：框按一行的高度长出去
                box, mode = [layout.expand_box(box[0], cap, n - cap, self.H)], "single"
            elif len(box) == 1:
                mode = "single"
            else:
                self.notes.append((src.no, f"正文 {n} 行、原文框 {len(box)} 个（装 {cap} 行）对不上：不按原框逐行摆"))
            if n > cap and mode != "free":
                self.notes.append((src.no, f"正文 {n} 行、原文框装 {cap} 行：按原来一行的高度扩出 {n - cap} 行（压到别的字不管）"))
        m = re.fullmatch(r"r(\d+)", src.actor.strip())
        s0, e0 = cs_of(src.row.get("Start")) * 10_000, cs_of(src.row.get("End")) * 10_000
        tw = int(p["tw"]) if p.get("tw") not in (None, True) and full_ms is None else None
        # 逐行显字（阶段 3 的句条目）：人写了 \k 节奏以人为准；行数和段数对不上（人改了行）退回整条匀速
        rowt = grow_of(p["rowt"]) if p.get("rowt") not in (None, True) and full_ms is None else None
        if rowt is not None and (mode != "rows" or len(rowt) != len(plain)):
            self.notes.append((src.no, f"rowt 有 {len(rowt)} 段、正文 {len(plain)} 行（{mode}），对不上：按整条的打字机节奏显字"))
            rowt = None
        x = {"src": src, "font": font, "rows_tok": rows_tok, "plain": plain, "lead": lead,
             "has_k": full_ms is not None or bool(tw) or bool(rowt), "tw": tw, "rowt": rowt,
             "full_ms": full_ms if full_ms is not None else max(b for _, b in rowt) if rowt else tw,
             "an": int(an) if an else 5, "pos": pos, "move": move, "fs": float(fs) if fs else None, "fad": fad,
             "box": box, "src_box": src_box, "grow": grow_of(p["grow"]) if p.get("grow") not in (None, True) else None,
             "base": base, "cap": cap, "kind": kind, "mode": mode, "moved": moved, "resized": resized, "reflow": reflow,
             "fit": (fit if fit in ("src", "tr") else "src") if fit and not resized else None,
             "translated": fit == "tr", "path": path_of(p["path"]) if p.get("path") and not moved else None,
             "region": int(m[1]) if m else -1, "start_us": s0, "end_us": e0}
        if kind == "block":
            paras = [re.sub(r"\\N", "", q) for q in re.split(r"\\N\\N", "\\N".join(plain))] if plain else []
            x["paras"], x["wrap"], x["latin"] = paras, int(p["wrap"]), p.get("le") not in (None, True)
            x["measure"] = (font, layout.width_keys(paras, True))
        else:
            lines = plain if mode != "single" else ["\n".join(plain)]
            x["lines"] = lines
            # 底板要按画出来的宽：行内 \fs 切开的几段各量各的（tokens_width）；字号适配按整行（fit_line）
            tok_rows = carry_rows(rows_tok) if mode == "rows" else [rows_joined(rows_tok)]
            runs = [s for r in tok_rows for ln in runs_of(r) for s, _ in ln]
            marks = [k for r in tok_rows for k in ruby.keys(ruby.rubies_of(r))]       # Ruby 的位置和读音的宽
            x["measure"] = (font, runs + layout.width_keys(lines, False) + marks + (list(layout.EM_KEYS) if reflow else []))
        return x

    def fit(self, x: dict, widths: dict, W: int) -> None:
        if x["kind"] == "block":
            box = x["box"][0] if x["box"] else [0, 0, W, 0]
            keep = int(x["fs"]) if x["resized"] and x["fs"] is not None else None
            x["size"], x["wrapped"] = layout.block_layout(box, x["paras"], x["wrap"], x["font"], widths, size=keep, latin=x["latin"])
            return
        x["sizes"] = None
        if x["fit"] == "src" and x["mode"] == "rows" and len(x["box"]) > 1 and len(x["lines"]) == len(x["box"]):
            # 原文按原框逐行摆：每行按自己的框适配字号（一句一条之后，名牌下的称号、并排的小标签和正文高度不同，
            # 统一一个字号会把小字撑出框——魔裁存档界面的 `[142] Empty`）；译文整条一个字号
            x["sizes"] = [layout.fit_line([b], [ln], x["font"], widths, False, W) for b, ln in zip(x["box"], x["lines"])]
            x["size"] = max(x["sizes"])
        elif x["fit"] and x["reflow"]:
            # 正文译文（D1）：一字宽跟原文的 `em`，宽过文本框（`bw`，没有就画面）才缩，整条一个字号；没有 `em` 退回 `fit_line`
            x["size"] = layout.body_size(x["box"], x["lines"], x["font"], widths, x["reflow"]["em"], x["reflow"]["bw"], W, x["reflow"]["le"])
        elif x["fit"] and x["box"] and x["mode"] != "free":
            x["size"] = layout.fit_line(x["box"], x["lines"], x["font"], widths, x["fit"] == "tr", W)
        elif x["fit"] and x["box"]:
            # 不按原框逐行摆（行数对不上 / 人挪过）：整块在几个框合起来的范围里适配，但正文行数比原文框装的少时
            # 行高按原来一行的高度算——两行并成一行不该把字放大到两行那么高（owner 2026-09-27 改字幕稿时撞到：\fs51 → \fs102）
            bx, n = x["box"], len(x["lines"])
            y0, y1 = bx[0][1], bx[-1][3]
            union = [min(r[0] for r in bx), y0, max(r[2] for r in bx), y0 + (y1 - y0) * n / max(n, x["cap"])]
            x["size"] = layout.fit_line([union], ["\n".join(x["lines"])], x["font"], widths, x["fit"] == "tr", W)
        else:
            x["size"] = int(x["fs"]) if x["fs"] is not None else 48

    def same_size(self, laid: dict) -> None:
        """同栏同时在屏的选项按钮字号取中位数（`layout.same_size`）；只算还在适配字号的那些。"""
        idx = [i for i, x in laid.items() if x["kind"] in ("choice", "offmain") and x["fit"] and x["box"]]
        got = layout.same_size([(laid[i]["start_us"], laid[i]["end_us"], laid[i]["box"][0]) for i in idx],
                               [laid[i]["size"] for i in idx])
        for i, s in zip(idx, got):
            laid[i]["size"] = s

    def body_tokens(self, x: dict) -> tuple[list, list, list[int]]:
        """普通条目逐行摆的正文元素：（各行的 Ruby 标记、去掉标记的各行元素、各行字号）。排版（`generate`）和共用锚轴的测宽（`stack_axes`）都用这一份，
        行内 `\\fs` 改过的字宽两处一样算。"""
        tok = x["rows_tok"] if x["mode"] == "rows" else [rows_joined(x["rows_tok"])]
        if x["rowt"]:
            tok = with_rowt(tok, x["rowt"], self.look.typewriter != "off")
        elif x["tw"]:
            tok = with_tw(tok, x["tw"], x["mode"] != "rows")
        if x["mode"] == "rows":
            tok = carry_rows(tok)
        marks = [ruby.rubies_of(t) for t in tok]                  # 各处 Ruby（带正文首字的出现时刻）
        tok = [ruby.strip(t) for t in tok]                         # 正文元素里去掉标记（落单、读音空的也去掉）
        sizes = x["sizes"] if x["mode"] == "rows" and x["sizes"] else [x["size"]] * len(tok)
        return marks, tok, sizes

    def stack_axes(self, laid: dict, widths: dict, W: int) -> None:
        """名牌下面的称号和名字共用一根锚轴（`layout.stacked_axes`）：居中、不在动、人没挪过、只有一个原文框的名牌行参加，
        和阶段 3 的 `script.stacks` 同一判据。定下的轴放进 `x["axis"]`，逐行摆时代替这一行自己的框中心；避画面边按一组里最宽的字宽一起推。"""
        idx = [i for i, x in laid.items() if x["kind"] == "name" and x["an"] == layout.ANCHOR["center"] and not x["moved"]
               and not x["path"] and len(x["src_box"]) == 1 and x["mode"] in ("rows", "single")]
        got = layout.stacked_axes([(laid[i]["start_us"], laid[i]["end_us"], laid[i]["src_box"][0]) for i in idx],
                                  [max((tokens_width(t, sz, laid[i]["font"], widths) for _, t, sz in zip(*self.body_tokens(laid[i]))),
                                       default=0.0) for i in idx], W)
        for i, a in zip(idx, got):
            laid[i]["axis"] = a

    # -- 第 2–4 步：元素、运动、时间效果 --

    def generate(self, x: dict, widths: dict, W: int) -> list[tuple[str, str]]:
        """第 2 步（元素）按种类摆好，其余（编号、扩展挂点、第 3 步运动、第 4 步时间效果）面板和普通条目走同一条路。"""
        src, look, p = x["src"], self.look, x["src"].fo
        back = "\\1c&H%s&\\1a&H%02X&" % (OVERLAY_COLOR, look.alpha)
        fad = assfile.numbers(x["fad"]) if x["fad"] and look.fade == "on" else []
        x["fad_ms"] = (int(fad[0]), int(fad[1])) if len(fad) == 2 else (0, 0)
        if any(x["fad_ms"]):
            self.count("fade")
        actor, style = src.actor, src.style
        s_us, e_us, size = x["start_us"], x["end_us"], x["size"]
        x["pad"] = pad = layout.plate_pad(size)
        user_where = "\\move(%s)" % x["move"] if x["move"] else "\\pos(%s)" % x["pos"] if x["pos"] else ""
        segs, base = [(s_us, e_us, None, None)], (0, 0)
        els: list[Element] = []
        if x["kind"] == "block":
            box = [x["box"][0]] if x["box"] else []
            plates = layout.plate_rects(box, [], None, "box", pad) if p.get("plate") and box else []
            els += [plate_el(pl, back) for pl in plates]
            body = "\\N".join(x["wrapped"])
            if box and not x["moved"]:
                els.append(Element(1, box[0][0], box[0][1], "\\an7", "\\fs%d" % size, body, "text", tail2=x["lead"], fade=True))
            else:                                              # 人挪过（\pos / \move）或改过锚点：照人给的画
                els.append(Element(1, 0, 0, "\\an%d" % x["an"] if x["moved"] else "\\an7", "\\fs%d" % size, body, "text",
                                   where=user_where, tail2=x["lead"], fade=True))
            align, lines = "left", x["paras"]
            x["mode_tw"] = "off"
        else:
            align = layout.ALIGN_OF.get(x["an"], {1: "left", 7: "left", 3: "right", 9: "right"}.get(x["an"], "center"))
            color = (UI_COLOR if look.colors and style == "UI" else OFFMAIN_COLOR if look.colors and p.get("off") else None)
            ctag = "\\1c&H%s&" % color if color else ""
            x["mode_tw"] = look.typewriter if x["has_k"] else "off"
            if x["rowt"]:                     # 逐行起显不受打字机开关管：关了就整行在首字时刻出现（with_rowt）
                x["mode_tw"] = "dim" if look.typewriter == "dim" else "on"
                self.count("rowt")
            if x["has_k"] and (x["tw"] or not x["rowt"] or any(b > a for a, b in x["rowt"])):
                self.count("typewriter")
            box, lines = x["box"], x["lines"]
            marks, tok, sizes = self.body_tokens(x)
            tw = [tokens_width(t, sz, x["font"], widths) for t, sz in zip(tok, sizes)]
            line_ws = [[sum(widths.get((x["font"], s), 0.0) * (z or sz) for s, z in ln) for ln in runs_of(t)]
                       for t, sz in zip(tok, sizes)]                  # 每个可见行画出来的宽（读音按它定中心、缩字号）
            plate_mode = p.get("plate") if isinstance(p.get("plate"), str) else look.plate
            x["bands"] = None
            if x["mode"] in ("rows", "single"):
                if x["reflow"]:                   # 正文译文：共用锚轴只按原文整块里真实的框（合成的不算）
                    axes = layout.block_axes(box, x["src_box"], x["reflow"]["synth"], tw, align, W)
                else:
                    axes = layout.axes_in_screen(box, tw, align, x["translated"], W, x.get("axis"))
                ink = [a + ink_shift(t, sz, x["font"], widths, align) for a, t, sz in zip(axes, tok, sizes)]   # 墨迹实际的锚轴
                # 字的锚点：墨迹中心落在这一行的竖直中心上（`export.ink_dy`：libass 竖直居中时墨迹偏下），底板按行中心上下对称地盖
                ys = [(r[1] + r[3]) / 2 - export.ink_dy(x["font"]) * sz for r, sz in zip(box, sizes)]
                x["rubies"] = [ruby.place(m, a, y, "middle", align, sz, lw, x["font"], widths, W)
                               for m, a, y, sz, lw in zip(marks, ink, ys, sizes, line_ws)]
                # 译文行数和原文整块不同（n ≠ k）：底板盖住原文整块 ∪ 译文各行
                spread = bool(x["reflow"]) and len(box) != len(x["src_box"])
                if spread and plate_mode == "rows" and p.get("plate"):
                    # 逐行配对盖不全原文、又会上下叠：译文各行、原文各行、读音拼成不重叠的横条，记下每条盖着谁（跟着字长时按它找这一条的几段）
                    got = layout.row_bands(box, tw, ink, align, text_hs(x, size, tok), x["src_box"], pad,
                                           [q.rect for rs in x["rubies"] for q in rs])
                    bare = plates = [b for b, _ in got]
                    x["bands"] = [c for _, c in got]
                else:
                    bare = (layout.plate_rects(x["src_box"] if plate_mode == "src" else box, tw, ink, plate_mode, pad, align,
                                               text_hs(x, size, tok), src_rows=x["src_box"] if spread else None) if p.get("plate") else [])
                    plates = (bare if plate_mode == "src"            # 只盖原文：译文扩出的行、读音都不并进来
                              else ruby.with_rubies(bare, [[q.rect for q in rs] for rs in x["rubies"]], plate_mode, pad))
                els += self.plate_els(x, plates, back, plate_mode, tok, tw, ink, align, widths, bare)
                for i, (y, a, t, sz) in enumerate(zip(ys, axes, tok, sizes)):
                    first, full = x["rowt"][i] if x["rowt"] else (None, x["full_ms"])
                    els.append(Element(1, a, y, "\\an%d" % layout.ANCHOR[align], "\\fs%d%s" % (sz, ctag),
                                       body_of(t, x["mode_tw"], full, first), "text", tail2=x["lead"], fade=True, tokens=t))
                    els += [self.ruby_el(x, q, ctag, full, first) for q in x["rubies"][i]]
                if x["path"]:
                    segs = layout.motion_segments(x["path"], s_us, e_us)
                base = x["base"]
            else:
                # 整块画在 \pos 上：字号按原来一行的高度适配，一般不冲出原文框的并集，没接 text_h（近似，计划里记过）
                pbox = x["src_box"] if plate_mode == "src" else box      # 单框扩行时 box 已往下扩，只盖原文就用原文框
                bare = layout.plate_rects(pbox, [], None, "box", pad) if p.get("plate") and pbox else []
                axis = [a + ink_shift(tok[0], size, x["font"], widths, align) for a in assfile.numbers(x["pos"])[:1]] if x["pos"] else []
                x["rubies"] = [[]]
                if marks[0] and (x["move"] or not x["pos"]):
                    self.notes.append((src.no, "整块画在 \\move 上（或没有 \\pos）：这一行不画 Ruby"))
                elif marks[0]:                     # 几行按 \pos 的锚点竖直等分（行距按字号）
                    an = x["an"]
                    valign = "bottom" if an in (1, 2, 3) else "top" if an in (7, 8, 9) else "middle"
                    x["rubies"] = [ruby.place(marks[0], axis[0], assfile.numbers(x["pos"])[1], valign, align, size, line_ws[0],
                                              x["font"], widths, W)]
                # 人挪过位置的，底板还在原文框上、不跟着字走：读音也不并进去
                plates = bare if x["moved"] or plate_mode == "src" else ruby.with_rubies(bare, [[q.rect for q in x["rubies"][0]]], "box", pad)
                els += self.plate_els(x, plates, back, plate_mode, tok, tw, axis, align, widths, bare)
                els.append(Element(1, 0, 0, "\\an%d" % x["an"], "\\fs%d%s" % (size, ctag),
                                   body_of(tok[0], x["mode_tw"], x["full_ms"]), "text",
                                   where=user_where, tail2=x["lead"], fade=True, tokens=tok[0]))
                els += [self.ruby_el(x, q, ctag, x["full_ms"], None) for q in x["rubies"][0]]
            self.count("ruby", sum(len(rs) for rs in x["rubies"]))
        # 扩展效果拿到的是整条一个字号（逐行字号时取最大的那行）：按字号摆位置的扩展和小字行会差一点，已知限度
        line = Line(style, actor, p, s_us, e_us, x["region"], x["kind"], box, lines, size, align, plates, look)
        if look.label and x["region"] >= 0 and plates and x["kind"] != "block":
            # 按画出来的底板边（`plate_el` 四条边各自取整）：边距不是整数时，按没取整的算会差一像素
            px, py = max(round(q[2]) for q in plates) - LABEL_INSET, min(round(q[1]) for q in plates) + LABEL_INSET
            els.append(Element(2, px, py, "\\an9", "\\fs%d\\bord1\\1c&H%s&" % (max(LABEL_MIN, int(LABEL_SCALE * size)), LABEL_COLOR),
                               f"{x['region']}" + ("ui" if style == "UI" else ""), "label"))
        for name, mod in self.fx.items():
            if name in p and hasattr(mod, "elements"):
                els += list(mod.elements(line, p[name]))
        x["line"] = line
        self.count(x["kind"])
        return self.emit(x, els, segs, base)

    def plate_els(self, x: dict, plates: list[list[float]], back: str, plate_mode: str, tok: list[list[tuple]],
                  tw: list[float], axes: list[float], align: str, widths: dict, bare: list[list[float]]) -> list[Element]:
        """底板元素。打字机行（逐字显出、不在动、人没挪过位置）跟着字长：只盖原文已出现的部分 ∪ 译文已显出的部分
        （`text` 方案只算译文，`src` 方案只算原文），见 `grow`；全字出现后是两者的并集（两段相连时就是静态底板）。其余照静态底板画。
        `plates` 已经并进了读音（`ruby.with_rubies`），`bare` 是并之前的（整块画的几行按它等分）；
        读音在正文首字出现时整段出现，它那一段的右沿那时一下跳到读音的右沿。"""
        if not plates or x["mode_tw"] == "off" or x["path"] or x["moved"] or not axes:
            return [plate_el(pl, back) for pl in plates]
        self.count("plate_grow")
        pad, size, font = x["pad"], x["size"], x["font"]
        src = [] if plate_mode == "text" else self.source_lines(x)
        rubies = x["rubies"]
        if plate_mode == "src" and x["mode"] == "rows":   # 一个原文框一块（`plates` 按 `src_box` 算的），各自跟着这一行原文长
            return [plate_el(static, back, c) for static, (r, f) in zip(plates, src) for c in grow.row_clips(static, [(r[0] - pad, f)])]

        def ruby_front(q: ruby.Placed, dim_from: int | None) -> grow.Front:
            t = dim_from if dim_from is not None else q.ruby.t or 0
            return grow.steps([(t, q.rect[2] + pad)])
        if x["mode"] == "rows":
            off = x["box"].index(x["src_box"][0]) if x["src_box"] and x["src_box"][0] in x["box"] else 0
            rows = []
            for i, (r, a, t, w) in enumerate(zip(x["box"], axes, tok, tw)):
                left = layout.text_span(a, w, align)[0]
                cells = [c for ln in cells_of(t) for c in ln]     # 一行里罕见的软换行 \n 压平成一行算：前半行打完时右沿偏窄、可能露一截，罕见、不处理
                sz = x["sizes"][i] if x["sizes"] else size
                dim_from = (x["rowt"][i][0] if x["rowt"] else 0) if x["mode_tw"] == "dim" else None
                rows.append((r, text_front(cells, left, w, sz, pad, dim_from), left, dim_from))
            if x["bands"] is not None:
                # 横条拼的底板（正文译文 n ≠ k 的 rows 方案）：每条按盖着它的那几段长——译文各行、原文各行、读音，下标同 `layout.row_bands` 的顺序
                grows = ([(left - pad, f) for _, f, left, _ in rows] + [(sr[0] - pad, sf) for sr, sf in src]
                         + [(min(left, q.rect[0]) - pad, ruby_front(q, d)) for (_, _, left, d), rs in zip(rows, rubies) for q in rs])
                return [plate_el(static, back, c) for static, cover in zip(plates, x["bands"])
                        for c in grow.row_clips(static, [grows[j] for j in sorted(cover)])]
            if plate_mode == "box":
                pieces = [(r[0] - pad, r[1] - pad, r[3] + pad, f) for r, f in src]
                th = text_hs(x, size, tok)         # 译文的竖直范围和静态底板同一个算法（`layout.row_span`），全字后 clip 等于静态底板
                ths = th if isinstance(th, list) else [th] * len(rows)
                pieces += [(left - pad, layout.row_span(r, h)[0] - pad, layout.row_span(r, h)[1] + pad, f) for (r, f, left, _), h in zip(rows, ths)]
                pieces += [(q.rect[0] - pad, q.rect[1] - pad, q.rect[3] + pad, ruby_front(q, row[3])) for row, rs in zip(rows, rubies) for q in rs]
                return [plate_el(plates[0], back, grow.bbox_clip(plates[0], pieces))]
            out = []
            for i, (static, (r, f, left, d)) in enumerate(zip(plates, rows)):
                parties = [(left - pad, f)]
                if 0 <= i - off < len(src):
                    sr, sf = src[i - off]
                    parties.append((sr[0] - pad, sf))
                # 读音那一段从正文左沿（读音伸到左边外面时从读音左沿）长起：和正文同一个左沿就不多切一块
                parties += [(min(left, q.rect[0]) - pad, ruby_front(q, d)) for q in rubies[i]]
                out += [plate_el(static, back, c) for c in grow.row_clips(static, parties)]
            return out
        # 整块画（一个原文框画几行 single / 行数对不上 free）：几行译文拼成一条居中画，纵向按等分算
        static, (y0, y1) = plates[0], (bare[0][1], bare[0][3])
        lines = cells_of(tok[0])
        runs = runs_of(tok[0])
        h = (y1 - y0) / max(1, len(lines))
        dim_from = 0 if x["mode_tw"] == "dim" else None
        pieces = [(r[0] - pad, r[1] - pad, r[3] + pad, f) for r, f in src]
        if plate_mode == "src":
            return [plate_el(static, back, grow.bbox_clip(static, pieces))]
        for j, (ln, rn) in enumerate(zip(lines, runs)):
            w = sum(widths.get((font, s), 0.0) * (sz or size) for s, sz in rn)
            left = layout.text_span(axes[0], w, align)[0]
            f = text_front(ln, left, w, size, pad, dim_from)
            pieces.append((left - pad, y0 + j * h, y0 + (j + 1) * h, f))
        pieces += [(q.rect[0] - pad, q.rect[1] - pad, q.rect[3] + pad, ruby_front(q, dim_from)) for q in rubies[0]]
        return [plate_el(static, back, grow.bbox_clip(static, pieces))]

    def ruby_el(self, x: dict, q: ruby.Placed, ctag: str, full: int | None, first: int | None) -> Element:
        """一处 Ruby 的读音元素：和正文同一种（`text`，底板取色一起改字色）、同样的字色 tag 和行首块，前面接正文开头处生效的样式块；
        跟着这一行淡入淡出、按轨迹分段（带自己的记号），打字机时正文第一个字出现时整段出现、`dim` 时跟着这一行半透明。"""
        t = ruby.tokens_of(q)
        return Element(1, q.x, q.y, "\\an2", "\\fs%d%s" % (q.size, ctag), body_of(t, x["mode_tw"], full, first), "text",
                       tail2=x["lead"], fade=True, tokens=t)

    def source_lines(self, x: dict) -> list[tuple[list[float], grow.Front]]:
        """原文各行（文字的框，没外扩）和它的右沿：在（首字, 全字）之间从左沿匀速长到右沿，领先 `GROW_LEAD` 个行高，再外扩底板的边距。
        时刻取 `fo` 的 `grow`（一行一段）；一个框装几行原文（`single`）时这个框按 `cap` 等分、这一段按行等分
        （逐行摆的 `rows` 里一个框装几行时整格从左往右长，不等分：那里的框和正文行一一对应）。
        没有 `grow`（人手加的打字机）或段数对不上（人改过 `box`）：原文在 `[0, 全字出现]` 里按框宽比例在各行间顺次匀速长。"""
        src, segs, pad = x["src_box"], x["grow"], x["pad"]
        if segs is not None and len(segs) != len(src):
            self.notes.append((x["src"].no, f"grow 有 {len(segs)} 段、原文框 {len(src)} 个，对不上：底板按整条的打字机节奏估原文"))
            segs = None
        if x["mode"] == "single" and len(src) == 1 and x["cap"] > 1:
            r, k = src[0], x["cap"]
            hh = (r[3] - r[1]) / k
            geo = [[r[0], r[1] + i * hh, r[2], r[1] + (i + 1) * hh] for i in range(k)]
            if segs:
                s, f = segs[0]
                segs = [(s + (f - s) * i / k, s + (f - s) * (i + 1) / k) for i in range(k)]
        else:
            geo = src
        if not segs:
            full = x["full_ms"] or 0
            ws = [max(0.0, r[2] - r[0]) for r in geo]
            tot, acc, segs = sum(ws) or 1.0, 0.0, []
            for w in ws:
                segs.append((full * acc / tot, full * (acc + w) / tot))
                acc += w
        out = []
        for r, (s, f) in zip(geo, segs):
            lead = GROW_LEAD * (r[3] - r[1])
            out.append((r, grow.ramp(s, f, min(r[0] + lead, r[2]) + pad, r[2] + pad)))
        return out

    def emit(self, x: dict, els: list[Element], segs, base) -> list[tuple[str, str]]:
        """第 3、4 步：所有元素一起按轨迹逐段展开，每段各一套（底板、正文、编号的先后照元素的顺序）；
        时间效果按**源行**的时间算——拆成几段时淡入淡出换成各段的 `\\fade`、逐字显出的时刻和人写的 `\\t` / `\\fade`
        都减去这一段的起点，不在每段从头放一遍。扩展效果的 `tags` 挂点在每一段的元素定稿之后调，改的东西各段都在。"""
        src, p = x["src"], x["src"].fo
        s_us, e_us = x["start_us"], x["end_us"]
        dur, (fin, fout) = (e_us - s_us) // 1000, x["fad_ms"]
        whole = len(segs) == 1
        out = []
        for seg in segs:
            a = (seg[0] - s_us) // 1000
            ftag = fade_tag(fin, fout, dur, a, whole)
            full = x["full_ms"] - a if x["full_ms"] is not None and x["full_ms"] - a > 0 else None
            for el in els:
                e = el if whole else replace(
                    el, tail2=shift_times(el.tail2, a, dur),
                    body=el.body if el.tokens is None else body_of(shift_tokens(el.tokens, a, dur), x["mode_tw"], full))
                e = replace(e)
                for name, mod in self.fx.items():
                    if name in p and hasattr(mod, "tags"):
                        mod.tags(x["line"], p[name], e)
                where = e.where if e.where is not None else layout.pos_tag(e.x, e.y, seg, base)
                text = "{%s%s%s%s%s}%s" % (e.head, where, e.tail, ftag if e.fade else "", e.tail2, e.body)
                if e.kind == "text":            # 行内回到样式色的 `\1c`（剧本样式的闭标签）回到这一行的字色（dev 的黄 / 浅蓝、人在行首写的颜色）
                    text = assfile.recolor_inline(text, assfile.head_color(text))
                out.append((e.kind, dialogue(e.layer, seg[0], seg[1], src.style, src.actor, text, e.effect)))
        return out

    def plain(self, src: Src) -> str:
        """不带 `fo` 的源行：照主流 tag 原样生成一条，只换派生样式、标 `fo-fx`。"""
        r = replace(src.row, fields=list(src.row.fields))
        r.set("Text", src.text)
        r.set("Style", src.style + FX_STYLE_SUFFIX)
        r.set("Effect", EFFECT_FX)
        self.count("plain")
        return str(r)


def look_of(preset: str, options: dict | None) -> tuple[Look, str]:
    o = dict(options or {})
    if preset not in LOOKS:
        raise ValueError(f"阶段 4 的预设只有 {list(LOOKS)}：{preset!r}")
    lk = LOOKS[preset]
    alpha = o.get("plate_alpha", lk.alpha)
    lk = replace(lk, plate=str(o.get("plate", lk.plate)), typewriter=str(o.get("typewriter", lk.typewriter)),
                 fade=str(o.get("fade", lk.fade)), alpha=int(alpha, 0) if isinstance(alpha, str) else int(alpha),
                 plate_color=str(o.get("plate_color", lk.plate_color)))
    if lk.plate_color not in ("off", "on"):
        raise ValueError(f"plate_color 只能是 off / on：{lk.plate_color!r}")
    if lk.plate not in layout.PLATE_MODES:
        raise ValueError(f"plate 只能取 {list(layout.PLATE_MODES)}：{lk.plate!r}")
    if lk.typewriter not in TYPEWRITER_MODES:
        raise ValueError(f"typewriter 只能取 {list(TYPEWRITER_MODES)}：{lk.typewriter!r}")
    if lk.fade not in ("off", "on"):
        raise ValueError(f"fade 只能是 off / on：{lk.fade!r}")
    unknown = str(o.get("unknown", "error"))
    if unknown not in ("error", "ignore"):
        raise ValueError(f"unknown 只能是 error / ignore：{unknown!r}")
    return lk, unknown


def load_fx(specs) -> tuple:
    mods = []
    for spec in specs or ():
        m = extensions.load(spec, "fx")
        if not isinstance(getattr(m, "NAME", None), str) or not (hasattr(m, "elements") or hasattr(m, "tags")):
            raise TypeError(f"扩展效果 `{spec}` 要有 NAME（它在 fo 里的参数名）和 elements / tags 至少一个挂点")
        mods.append(m)
    return tuple(mods)


def typeset_file(src: Path, out: Path | None = None, preset: str = "default", options: dict | None = None,
                 fx=(), quiet: bool = False, widths: dict | None = None, video: str | None = None,
                 window: tuple[float, float] | None = None) -> tuple[Path, Typesetter]:
    """读一份字幕稿（或阶段 4 自己的输出），写最终 ASS。默认输出：`x.script.ass` → `x.ass`，其余 `x.ass` → `x.fx.ass`。
    `widths`：已经量好的字宽（叠加预设连跑时阶段 3 交过来的）；`video`：底板取色读的视频（不给按字幕稿里记的）；
    `window`：底板只给和这段时间（秒）有交集的行取色（预览只出一段时）。"""
    src = Path(src)
    look, unknown = look_of(preset, options)
    doc = assfile.loads(src.read_text(encoding="utf-8"))
    ts = Typesetter(look, load_fx(fx), unknown, widths, video, window)
    ts.run(doc)
    out = Path(out) if out else default_out(src)
    out.write_text(doc.dumps(), encoding="utf-8")
    if not quiet:
        report(out, ts)
    return out, ts


def default_out(src: Path) -> Path:
    name = src.name
    if name.endswith(".script.ass"):
        return src.with_name(name[:-len(".script.ass")] + ".ass")
    return src.with_name(src.stem + ".fx.ass")


def restore_file(src: Path, out: Path | None = None, force: bool = False) -> tuple[Path, int]:
    """阶段 4 的输出 → 字幕稿（逐字节相同）。默认输出 `x.ass` / `x.fx.ass` → `x.script.ass`。
    输出的文件已经在时不覆盖（除非 `force`）：那份字幕稿可能是人改过的，拿一份旧的最终 ASS 还原会把改动冲掉。"""
    src = Path(src)
    if out is None:
        if src.name.endswith(".script.ass"):
            raise ValueError(f"{src.name} 已经是字幕稿；要还原的是阶段 4 的输出（x.ass），或用 -o 指定输出")
        stem = src.name[:-len(".fx.ass")] if src.name.endswith(".fx.ass") else src.stem
        out = src.with_name(stem + ".script.ass")
    out = Path(out)
    if out.exists() and not force:
        raise FileExistsError(f"{out} 已经在（可能是改过的字幕稿），不覆盖；确认要覆盖加 --force，或用 -o 另写一份")
    doc = assfile.loads(src.read_text(encoding="utf-8"))
    n = restore(doc)
    out.write_text(doc.dumps(), encoding="utf-8")
    return out, n


def report(out: Path, ts: Typesetter) -> None:
    c = ts.counts
    kinds = "、".join(f"{k} {c[k]}" for k in ("line", "name", "choice", "offmain", "block", "plain") if c.get(k))
    print(f"-> {out}（源行 {c.get('源行', 0)} 条 → 生成 {c.get('生成行', 0)} 条：{kinds}；"
          f"打字机 {c.get('typewriter', 0)} 条（底板跟着字长 {c.get('plate_grow', 0)} 条）、淡入淡出 {c.get('fade', 0)} 条"
          + (f"、Ruby {c['ruby']} 处" if c.get("ruby") else "")
          + (f"；底板取色 {c['底板取色']} 块、{c.get('底板小格', 0)} 格（{'NVDEC 解码' if c.get('底板取色·NVDEC') else '软解'}）"
             if c.get("底板取色") else "") + "）")
    if ts.fills:
        print("  占位：" + "；".join(f"{tag} -> {v or '（空，删掉）'} {n} 处" for (tag, v), n in ts.fills.items()))
    if ts.notes:
        print(f"  提示 {len(ts.notes)} 条：")
        for no, msg in ts.notes[:12]:
            print(f"    {'第 %d 行：' % no if no else ''}{msg}")
        if len(ts.notes) > 12:
            print(f"    …另 {len(ts.notes) - 12} 条")
