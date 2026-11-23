"""阶段 4：把字幕稿里的 Ruby 标记（`{ruby:读音}正文{/ruby}`，`richtext.RUBY_BLOCK`）画成正文上方的一条小字（owner 2026-10-23，Ruby 计划第二期）。

一处 Ruby 画成一个单独的正文元素：读音以正文那几个字的墨迹中心为中心（`\\an2`），底边在这一行字形顶往上 `RUBY_GAP` 个字号；
字号是这一行的 `RUBY_SCALE` 倍，比这一行正文宽就缩到放得下、不小于 `RUBY_MIN_SCALE` 倍，到下限还放不下就两边伸出去。
这里只从记号里认出 Ruby、算位置和大小；元素怎么拼、底板怎么并进来在 `core`。
行内 `\\fs` 改过的字宽不计（前缀和正文按这一行的字号量），已知限度。"""
from __future__ import annotations

from dataclasses import dataclass

from flowocr import richtext
from flowocr.output import export, layout
from flowocr.typeset import assfile

RUBY_SCALE = 0.5
"""读音的字号是这一行正文字号的这么多倍。"""
RUBY_MIN_SCALE = 0.3
"""读音比这一行正文宽时往小缩，不小于正文字号的这么多倍；到这里还放不下就照这个字号画、两边伸出去（中文的长注释）。"""
RUBY_GAP = 0.08
"""读音底边离这一行正文字形顶的距离，正文字号的这么多倍。"""


@dataclass
class Ruby:
    """一处 Ruby：读音、正文，正文前面同一可见行里的字（`prefix`）、正文开头处生效的行内样式块（去掉了 `\\fs`）、
    正文第一个字出现的时刻（毫秒；None = 不逐字）、在第几个可见行（行里的软换行 / 整块画的几行分开数）。"""
    reading: str
    prefix: str
    base: str
    tags: list[str]
    t: int | None
    line: int


@dataclass
class Placed:
    """摆好的一处 Ruby：读音的底边中点 `(x, y)`、字号、画出来的宽。"""
    ruby: Ruby
    x: float
    y: float
    size: int
    w: float

    @property
    def rect(self) -> list[float]:
        """读音占的矩形（`\\an2` 锚在底边中点，竖直按一个字号的行高算，比字形高、底板多盖不露）。"""
        return [self.x - self.w / 2, self.y - self.size, self.x + self.w / 2, self.y]


def is_mark(t: tuple) -> bool:
    return t[0] == "tags" and bool(richtext.RUBY_BLOCK.fullmatch(t[1]))


def rubies_of(tokens: list[tuple]) -> list[Ruby]:
    """一条的记号（`core.text_rows` 的一行，或整块画并起来的几行）里的各处 Ruby。开标记到闭标记之间的字是正文；
    还没闭就换了可见行（人在范围里加了换行、按原框逐行摆时闭标记落到下一行）的只画在开头那行、盖到行尾；落单的闭标记不管。
    读音空、正文没有字的不要。"""
    out: list[Ruby] = []
    line, prefix, styles = 0, "", []
    cur: Ruby | None = None

    def close() -> None:
        nonlocal cur
        if cur is not None and cur.reading and cur.base.strip():
            out.append(cur)
        cur = None
    for t in tokens:
        if is_mark(t):
            if t[1].startswith("ruby:"):
                close()
                cur = Ruby(t[1][len("ruby:"):], prefix, "", list(styles), None, line)
            else:
                close()
        elif t[0] == "tags":
            styles.append(t[1])
            if cur is not None and not cur.base:                   # 开在 Ruby 里、正文第一个字前面的（星铁 `{RUBY_B#<color=…>…`）
                cur.tags.append(t[1])
        elif t[0] == "nl":
            close()
            line, prefix = line + 1, ""
        else:
            ch = " " if t[1] == "\\h" else t[1]                       # 同 `core.PLAIN`：硬空格量字宽时是空格
            prefix += ch
            if cur is not None:
                if not cur.base:
                    cur.t = t[2]
                cur.base += ch
    close()
    for r in out:
        r.tags = [b for b in (no_fs(s) for s in r.tags) if b.strip()]
    return out


def no_fs(block: str) -> str:
    """行内样式块去掉 `\\fs`：读音的字号是按这一行算的，正文那几个字前面的 `\\fs80` 不该把小字也放大。"""
    return "".join(t for t in assfile.split_tags(block) if (assfile.tag_of(t) or ("",))[0] != "fs")


def strip(tokens: list[tuple]) -> list[tuple]:
    """去掉 Ruby 标记的记号：正文元素里不再写标记（已经画成单独的元素）。"""
    return [t for t in tokens if not is_mark(t)]


def keys(rubies: list[Ruby]) -> list[str]:
    """要量的字宽：`前缀+正文`、正文、读音（字号 1 时的墨迹宽，`export.ink_widths`）。"""
    return [s for r in rubies for s in (r.prefix + r.base, r.base, r.reading)]


def place(rubies: list[Ruby], axis: float, y: float, valign: str, align: str, size: int, line_ws: list[float],
          font: str, widths: dict, W: int) -> list[Placed]:
    """一个正文元素（锚在墨迹的锚轴 `axis`、竖直锚点 `y`；`valign` 是 `top` / `middle` / `bottom`，几行按每行一个字号的行距排）
    上面的各处 Ruby。`line_ws` 是各可见行画出来的宽。
    横向：正文中心 = 行左沿 + 墨迹宽(前缀+正文) − 墨迹宽(正文) / 2——按正文的右沿往回算，前缀末尾的空格、字间的留白都不会让中心偏；
    读音超出画面就往里推到 `layout.EDGE_PAD`。竖直：读音底边 = 这一行的字形顶（行中心 − 字形高 / 2）往上 `RUBY_GAP` 个字号。"""
    n = len(line_ws)
    gh = size / export.h_ratio(font)
    out = []
    for r in rubies:
        if r.line >= n:
            continue
        j, w = r.line, line_ws[r.line]
        cy = (y + (j - (n - 1) / 2) * size if valign == "middle" else y + (j + 0.5) * size if valign == "top"
              else y - (n - 1 - j + 0.5) * size)
        left = layout.text_span(axis, w, align)[0]
        cx = left + (widths.get((font, r.prefix + r.base), 0.0) - widths.get((font, r.base), 0.0) / 2) * size
        w1 = widths.get((font, r.reading), 0.0)
        rs = RUBY_SCALE * size
        if w1 * rs > w:
            rs = max(RUBY_MIN_SCALE * size, w / w1)
        rs = max(1, int(rs))
        rw = w1 * rs
        lo, hi = layout.EDGE_PAD + rw / 2, W - layout.EDGE_PAD - rw / 2
        cx = W / 2 if lo > hi else min(max(cx, lo), hi)
        out.append(Placed(r, cx, cy - gh / 2 - RUBY_GAP * size, rs, rw))
    return out


def tokens_of(p: Placed) -> list[tuple]:
    """读音元素的记号：正文开头处生效的样式块，读音整段一个字——正文第一个字出现时整段出现（`core.body_of` 照逐字的算）。
    样式块后面再写一次读音的字号：人写的 `\\r` 会把字号退回样式的，读音就成了正文大小。"""
    tags = [("tags", "".join(p.ruby.tags) + "\\fs%d" % p.size)] if p.ruby.tags else []
    return tags + [("ch", p.ruby.reading, p.ruby.t)]


def union(plate: list[float], rects: list[list[float]], pad: float) -> list[float]:
    """底板矩形并进几处读音（各外扩底板的边距 `pad`）。"""
    return [min([plate[0]] + [r[0] - pad for r in rects]), min([plate[1]] + [r[1] - pad for r in rects]),
            max([plate[2]] + [r[2] + pad for r in rects]), max([plate[3]] + [r[3] + pad for r in rects])]


def with_rubies(plates: list[list[float]], rects: list[list[list[float]]], mode: str, pad: float) -> list[list[float]]:
    """底板并进读音：`box` 一整块并进全部，`rows` / `text` 每行那块并进这一行的（`rects[i]` 是第 i 行的读音矩形）。
    逐行的底板往上长进了上一行那块、而读音（外扩边距后）横向都在上一行那块以内时，上沿停在上一行的下沿：
    那一截已经被上一行盖着，再叠一层半透明会更黑；横向伸出去的照并（叠一条，R6 的已知限度）。"""
    if not plates or not any(rects):
        return plates
    if mode == "box":
        return [union(plates[0], [r for rs in rects for r in rs], pad)]
    out = [union(pl, rs, pad) for pl, rs in zip(plates, rects)]
    for k in range(1, len(out)):
        a, b = out[k - 1], out[k]
        if b[1] < a[3] and all(a[0] <= r[0] - pad and r[2] + pad <= a[2] for r in rects[k]):
            b[1] = min(a[3], plates[k][1])
    return out
