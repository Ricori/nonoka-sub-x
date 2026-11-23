"""剧本译文里的占位（`{NICKNAME}`、`{REALNAME[…]}` 这类花括号）：阶段 3 标出来、阶段 4 填值或删掉（owner 2026-10-05）。

这里只认**花括号本身**，不管它是什么、该填什么——猜值是匹配器的事（原神从视频原文读玩家名，`flowocr.analyze.matchers.gametext`），
猜出来的写进匹配产物的 `fills`（`{占位: {"value": 值或 None, "by_lang": {"jp": …, "cn": …}, "how": 从哪来, …}}`：
玩家起的名字各语种相同，记 `value`；从候选里选的名字（星铁 `{TEXTJOIN#n}`）各语种不同，记 `by_lang`（`jp` / `cn`，剧本带英文时另有 `en`），输出哪种语言取哪个）。

* 阶段 2（`gamescript`）：性别、注音、按平台的写法先按游戏处理掉，剩下的花括号就是占位，在**译文**和原文模板里原样留着；
  用来匹配的原文里删掉（`drop`）。
* 阶段 3（`Marks`）：每种占位按出现先后编号，正文里写成 `` [`0:值] ``（值是匹配器猜的，没有就空着），
  字幕稿开头记图例 `FlowOCR Placeholder 0: {NICKNAME} …`。方括号 + 反引号：VSFilter 不认花括号的转义，
  这种写法 Aegisub 预览照原样显示；三款游戏的全部文本里 `` [` `` 出现 0 次。值里的半角方括号换成全角（不打断标记）。
  SRT 也写成这样（没有阶段 4，用户自己搜 `` [` `` 改）。
* 阶段 4（`apply`）：图例里有的编号，标记换成值；值是空的就删掉、连带收拾落单的标点。用户已经手改掉的标记不在了，就没什么可换。
  Ruby 的读音里也可以有占位（星铁 `{RUBY_B#{TEXTJOIN#254}}`，标记在注释块 `{ruby:…}` 里），照样换；读音换空了，这处 Ruby 的标记去掉、正文留着。
"""
from __future__ import annotations

import re
from collections import Counter

from flowocr import richtext

PLACEHOLDER_RE = re.compile(r"\{[^{}]*\}")
PLAYER = "{NICKNAME}"
"""玩家起的名字（三款游戏同一个写法）。"""

DROP = "\0"
"""删掉的占位先换成它，按两边的标点收拾完再删：玩家名删掉后，原来跟着名字的分隔号会落单（`#{NICKNAME}，刚刚我说过` → `，刚刚我说过`，
2026-10-05 owner 在原神叠加里看到行首一个逗号）。"""
SEP = "，、,"
END = "。！？!?…」』）)"
LEAD = SEP + "！？!?"
"""行首占位删掉后跟着一起删的标点：分隔号和叹号 / 问号（`#{NICKNAME}！快！` → `快！`，原神任务文本里中文 62 + 37 条）。
句号和省略号不删：`…我觉得` 读着通，整行只剩一个标点（`{NICKNAME}。`）时也留着、不删成空行。"""
_TAGS = r"(?:\{[^{}]*\})*"
"""字幕稿正文行首的 ASS tag 块（`{\\an7\\pos(…)}`）：阶段 4 收拾标点时，行首指的是 tag 块后面。"""
OPEN = "（「『("
"""开口括号后面也算句首：`（{NICKNAME}，接下来由你来指挥…）` 删完是 `（接下来…）`，不是 `（，接下来…）`
（原神中文 35 条、星铁中文 90 条、原神日文 30 条是这种写法）。"""
_SP = r"[ \t\u3000]*"
"""收拾标点时只吃同一行里的空白：吃掉换行会把两行并成一行，阶段 4 按原文框逐行摆的行数就对不上了。"""


def tidy(t: str) -> str:
    """收拾 `DROP` 两边落单的标点再删掉它（换行是 `\\n`）：行首的吃掉后面的分隔号 / 叹号 / 问号（`{X}，你好` → `你好`），
    开口括号后面的同样处理（`（{X}，走）` → `（走）`），句末的吃掉前面的分隔号（`你好，{X}。` → `你好。`），
    夹在两个分隔号中间的只留一个（`嗯，{X}，走吧` → `嗯，走吧`），整行只剩它和分隔号的留成空行（不并行）。连着的几个当一个处理。"""
    while True:                                            # `{X}，{X}，快醒醒` 要收两遍
        u = re.sub(rf"(^|\n)({_TAGS}){_SP}{DROP}+{_SP}[{LEAD}]+{_SP}(?=\S)", r"\1\2", t)
        u = re.sub(rf"([{OPEN}]){_SP}{DROP}+{_SP}[{LEAD}]+{_SP}(?=[^\s」』）)])", r"\1", u)   # `「{X}！」` 留成 `「！」`，不成空括号
        if u == t:
            break
        t = u
    t = re.sub(rf"(^|\n)({_TAGS}){_SP}{DROP}+{_SP}[{SEP}]+{_SP}(?=\n|$)", r"\1\2", t)   # 整行只剩占位 + 分隔号：留成空行
    t = re.sub(rf"{_SP}[{SEP}]{_SP}{DROP}+{_SP}(?=[{END}]|\n|$)", "", t)
    t = re.sub(rf"([{SEP}]){_SP}{DROP}+{_SP}[{SEP}]", r"\1", t)
    # 拉丁文的词间空格：`I'm {X} and` 删完不留两个空格，`thanks {X}.` 不留标点前的空格（前面是拉丁字时才收，绝区零中文的 `[ {X} ]` 照旧）
    t = re.sub(rf"(?<=[A-Za-z0-9.,!?;:'\"]) {DROP}+ ", " ", t)
    t = re.sub(rf"(?<=[A-Za-z0-9'\"]) {DROP}+(?=[.,!?;:])", "", t)
    t = re.sub(rf"(^|\n)({_TAGS}){DROP}+ (?=[A-Za-z])", r"\1\2", t)             # 行首：`{X} and I…` 不留行首空格
    return t.replace(DROP, "")


def drop(t: str) -> str:
    """删掉全部占位，连带收拾落单的标点（`tidy`）。"""
    return tidy(PLACEHOLDER_RE.sub(DROP, t))


MARK_RE = re.compile(r"\[`(\d+):([^\]\n]*)\]")
INFO_PREFIX = "FlowOCR Placeholder "
"""字幕稿 `[Script Info]` 里图例的键：`FlowOCR Placeholder <编号>: <占位> <从哪来>`。"""
SAFE = str.maketrans({"[": "［", "]": "］", "{": "｛", "}": "｝", "\\": "＼", "\n": " "})
"""值里会打断标记或被 ASS 当成特效 / 换行的字换成全角（最终也是全角：阶段 4 分不清哪个全角是这里换的）。"""


class Marks:
    """一份输出里的占位：按出现先后编号、换成标记，记下每个编号出现了几处。`fills` 是匹配产物的 `fills`，`lang` 是输出的语种（`jp` / `cn` / `en`）。"""

    def __init__(self, fills: dict | None = None, lang: str | None = None):
        self.fills = fills or {}
        self.lang = lang
        self.ids: dict[str, int] = {}
        self.n: Counter = Counter()

    def mark(self, text: str | None) -> str | None:
        if not text or "{" not in text:
            return text

        def one(m: re.Match) -> str:
            tag = m.group(0)
            i = self.ids.setdefault(tag, len(self.ids))
            self.n[i] += 1
            return f"[`{i}:{self.value(tag).translate(SAFE)}]"
        return PLACEHOLDER_RE.sub(one, text)

    def value(self, tag: str) -> str:
        f = self.fills.get(tag) or {}
        return (f.get("by_lang") or {}).get(self.lang) or f.get("value") or ""

    def how(self, tag: str) -> str:
        f = self.fills.get(tag) or {}
        return f.get("how") or ("匹配器没给值" if not f.get("value") else "")

    def legend(self) -> dict[str, str]:
        """写进字幕稿 `[Script Info]` 的图例。"""
        return {f"{INFO_PREFIX}{i}": f"{tag} {self.how(tag)}".strip() for tag, i in self.ids.items()}

    def say(self, where: str, srt: bool = False, texts=None) -> None:
        """打一段：出现了哪些占位、各几处、现在填什么、怎么改。`texts` 给了就按它数（SRT 只出一部分条目，按写出去的行数）。"""
        n = Counter(int(m.group(1)) for t in texts for m in MARK_RE.finditer(t)) if texts is not None else self.n
        shown = [(tag, i) for tag, i in self.ids.items() if n[i]]
        if not shown:
            return
        print(f"[占位] {where}的译文里有 {len(shown)} 种占位，标成 [`编号:值]：", flush=True)
        for tag, i in shown:
            v = self.value(tag)
            print(f"    {i}  {tag}  {n[i]} 处  -> {v or '（空）'}；{self.how(tag)}", flush=True)
        print("  SRT 不自动替换：搜 [` 逐处改成要的文字。" if srt else
              "  阶段 4 把标记换成值、空的连同落单的标点删掉；要改就在字幕稿正文里改值，或把整个标记换成文字。", flush=True)


def legend_of(info_lines: list[str]) -> dict[int, str]:
    """字幕稿 `[Script Info]` 的行 -> `{编号: 占位}`（阶段 4 只换图例里有的编号）。"""
    out = {}
    for ln in info_lines:
        if ln.startswith(INFO_PREFIX) and ":" in ln[len(INFO_PREFIX):]:
            k, v = ln[len(INFO_PREFIX):].split(":", 1)
            if k.strip().isdigit():
                v = v.strip()
                tag = PLACEHOLDER_RE.match(v)
                out[int(k)] = tag.group(0) if tag else v.split(" ", 1)[0]
    return out


WRAPPED = re.compile(r"\{(?:\\1c&H[0-9A-Fa-f]+&|\\[bius]1)+\}(\0+)\{(?:\\1c|\\[bius]0)+\}")
"""一个只开样式的 tag 块、删空的占位、一个只关样式的 tag 块（`richtext.to_ass` 写的那种）。行首的定位块、人写的别的 tag 不算。"""


def _ruby(m: re.Match) -> str:
    """一处 Ruby 标记（`richtext.RUBY_PAIR`）换完值以后：读音空了、或正文只剩删空的占位，标记去掉、正文留着；读音里删空的占位照样收拾标点。"""
    reading, base = m.group(1), m.group(2)
    if DROP in reading:
        reading = tidy(reading)
    if not reading.strip() or not PLACEHOLDER_RE.sub("", base).replace(DROP, "").strip():
        return base
    return "{ruby:%s}%s{/ruby}" % (reading, base)


def apply(text: str, ids) -> tuple[str, list[tuple[int, str]]]:
    """阶段 4：正文里编号在 `ids` 里的标记换成值，值是空的删掉、收拾标点。返回（新正文, [(编号, 值)…]）。正文里的换行是 `\\N`。
    Ruby 的注释块里的标记照样换；读音换空、正文删空的那处 Ruby 先去掉标记（`_ruby`），包着它的样式块和标点再照常收拾。"""
    got: list[tuple[int, str]] = []

    def one(m: re.Match) -> str:
        i = int(m.group(1))
        if i not in ids:
            return m.group(0)
        got.append((i, m.group(2)))
        return m.group(2) or DROP
    t = MARK_RE.sub(one, text)
    if DROP in t:
        t = richtext.RUBY_PAIR.sub(_ruby, t)
        # 剧本样式整段包着占位（`<color=…>{NICKNAME}</color>，…`，三款游戏各几十处）：删空以后那一对 tag 块也去掉，
        # 否则落单的标点隔着闭标签收拾不到（闭标签回到包之前的状态，两块一起去掉等于没包）
        t = WRAPPED.sub(r"\1", t)
        t = tidy(t.replace("\\N", "\n")).replace("\n", "\\N")
    return t, got
