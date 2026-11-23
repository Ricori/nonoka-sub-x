"""比较 OCR 读数用的字形折叠：同一个字被 rec 读成不同字形时，比较前先折成一样。

只折**读法差、不改内容**的两类：NFKC（全角 / 半角、兼容字形：`！` / `!`、`…` / `...`）和小书写假名折大（`っ` / `つ`、`ょ` / `よ`）。
`fold` 不去标点、不去空白——要不要去由调用方定（匹配器的 `norm` 去，打字机生长判前缀时不去：标点也是打出来的字）；
要去的用 `bare`，判两个读数是不是同一句字用 `same_reading`，判一个读数能不能是一句字出现的开头用 `leads`，
判一个读数能不能是一段字的一截（容几处错）用 `fits_within`。
"""
from __future__ import annotations

import unicodedata

SMALL_KANA = str.maketrans("ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶ",
                           "あいうえおつやゆよわアイウエオツヤユヨワカケ")
"""小书写假名折成大的。日文 rec 常把大小写读反（`あてぃし` → `あていし`，同一句前后两帧也会读得不一样）。"""


def cw(c: str) -> float:
    """一个字占几个全角字宽：全角（含宽度不定的引号、省略号）1，其余 0.5。
    阶段 3 折译文（`output.script.wrap_lines`）和匹配器量原文一行的宽（`analyze.textbox`）共用这一份。"""
    return 1.0 if unicodedata.east_asian_width(c) in "WFA" else 0.5


def fold(t: str) -> str:
    """NFKC + 小书写假名折大。"""
    return unicodedata.normalize("NFKC", t).translate(SMALL_KANA)


SAME_READING_MIN = 2
"""`same_reading` 认前缀时，短的那个至少这么多个字（去掉标点、空白之后）：一个字的前缀几乎什么都配得上。"""


def bare(t: str) -> str:
    """`fold` 之后去掉空白和标点（Unicode 类别 P*）。"""
    return "".join(c for c in fold(t) if not c.isspace() and not unicodedata.category(c).startswith("P"))


def same_reading(a: str, b: str) -> bool:
    """两个读数是不是同一句字：`bare` 之后相等，或者一个是另一个的前缀（打字中途的读数），短的至少 `SAME_READING_MIN` 个字。
    「裁判の後。」和「裁判の後」、「雪だ！ヒュー」和「雪だ！ヒューヒュー!」都算。"""
    x, y = bare(a), bare(b)
    if not x or not y:
        return False
    if x == y:
        return True
    short, long_ = (x, y) if len(x) <= len(y) else (y, x)
    return len(short) >= SAME_READING_MIN and long_.startswith(short)


def leads(a: str, b: str) -> bool:
    """读数 `a` 可以是 `b` 这句字出现的开头：同一句（`same_reading`），或者是 `b` 的开头几个字（打字刚出一两个字，不设字数下限），
    或者只有标点（打字机先打出来的「……」）。只看字，在不在同一处由调用方判。"""
    x = bare(a)
    return not x or bare(b).startswith(x) or same_reading(a, b)


def fits_within(q: str, s: str, edits: int) -> bool:
    """读数 `q` 能不能是 `s` 的一截：`s` 里有一段和 `q` 的编辑距离不超过 `edits`，两头没读到的字不算差
    （`階堂ヒロ` 是 `二階堂ヒロ` 少读了头一个字，0 处差；`階堂ヒ口` 再错一个字形，1 处差）。两边先 `bare`，有一边为空就不算。
    门（`edits`）和长度门由调用方定：名牌投票（`scriptmatch.fuzzy_names`）、夹缝认领（`game_align.claim_flanked`）。"""
    q, s = bare(q), bare(s)
    if not q or not s:
        return False
    prev = [0] * (len(s) + 1)                 # 第 0 行全 0：从 `s` 的任意位置起头都不罚
    for i, a in enumerate(q, 1):
        cur = [i] + [0] * len(s)
        for j, b in enumerate(s, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a != b))
        prev = cur
    return min(prev) <= edits                 # 取最后一行的最小值：在 `s` 的任意位置收尾都不罚
