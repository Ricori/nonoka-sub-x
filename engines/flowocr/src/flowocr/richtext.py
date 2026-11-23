"""剧本里的行内样式（粗体 / 斜体 / 下划线 / 删除线 / 字色）和 Ruby（正文上方的小字：读音，中文里多是语义注释）：
阶段 2 从剧本原文读出、存进剧本与匹配产物，阶段 3 投成字幕稿里的 ASS 标签和 Ruby 标记（SRT 是基础输出，都不带，owner 2026-10-23）。

**规范写法**（产物里存的）：`<b>` `<i>` `<u>` `<s>` `<color=#rrggbb>` `<ruby=读音>` 和对应的闭标签，成对、按层嵌套，只有这几种。
游戏的 Unity 富文本（`<color=#DBC291FF>`、`<size=30>`、`<unbreak>`…）经 `from_unity` 收成它：认得的几种归一，别的（字号、对齐、
`<unbreak>` 这类不改外观的）删掉、留下里面的字。字号不带：`<size=30>` 是游戏里的绝对像素，换算到叠加的字号要按游戏定基准；
对齐不带：游戏里是一段里某几行右对齐（书信落款），ASS 的对齐是整条 Dialogue 一个，要拆行才画得出（owner 2026-10-23）。

Ruby：`<ruby=读音>正文</ruby>`，读音是纯文字（没有标签、没有换行；可以有占位的花括号，`{TEXTJOIN#254}`、`{NICKNAME}`），
不嵌套（里面那层丢掉、正文留着）。Ruby 不拆：和样式交叉时拆样式（`<b>a<ruby=x>b</b>c</ruby>` → `<b>a</b><ruby=x><b>b</b>c</ruby>`，
样式拆开外观不变，Ruby 拆开读音就重复了）。各游戏的写法在阶段 2 先换成 Unity 的 `<ruby="读音">`（`unity_ruby`，`gamescript.ruby_rich`）。
阶段 3 写成字幕稿正文里单独的注释块 `{ruby:读音}正文{/ruby}`（`to_ass`）：渲染器不画、Aegisub 可隐藏；阶段 4 把它画成正文上方的小字（`flowocr.typeset.ruby`，owner 2026-10-23）。

规范写法和同一句的纯文字（剧本行的 `cn` / `jp_tpl`）只差标签：`plain(带样式) == 纯文字`（Ruby 的读音在标签里，跟着删）。纯文字经过断行、去空白这些处理以后，
样式按"非空白的字逐个对上"投回去（`project`），对不上就不给样式（返回 None），不猜。样式和 Ruby 各有开关（`style` / `ruby`），关掉的那种用 `select` 剥掉。
"""
from __future__ import annotations

import re
from typing import Callable

SIMPLE = ("b", "i", "u", "s")
RUBY = "ruby"
CANON_RE = re.compile(r"<(/?)(b|i|u|s|color|ruby)(?:=((?<=<color=)#[0-9a-f]{6}|(?<=<ruby=)[^<>]+))?>")
"""规范写法的标签：第 1 组是闭标签的 `/`，第 2 组名字，第 3 组值（字色 `#rrggbb`、Ruby 的读音）。只认这几种，正文里别的尖括号（OCR 原文、玩家名）不当标签。"""
RUBY_BLOCK = re.compile(r"ruby:[^{}]*|/ruby")
"""字幕稿正文里 Ruby 标记的花括号块的**内容**（`{ruby:读音}` / `{/ruby}`，`to_ass` 写的）。不带 `\\`，渲染器当注释；阶段 4 认它不是状态（`core.carry_rows`），按它画读音（`typeset.ruby`）。"""
RUBY_PAIR = re.compile(r"\{ruby:([^{}]*)\}(.*?)\{/ruby\}", re.S)
"""字幕稿正文里的一处 Ruby：(读音, 正文)。正文里可以有样式的 tag 块。"""
RUBY_SAFE = str.maketrans({"{": "｛", "}": "｝", "\\": "＼", "\n": " "})
"""读音写进注释块时换成全角的字：`}` 会提前结束注释、`\\` 会被当成 tag；换行换成空格。"""
UNITY_TAG = re.compile(r"<[^<>]*>")
"""Unity 富文本的标签：和 `gamescript.TAG_RE` 同一个式子——纯文字是按它删的，样式按它读，两边的字才对得上。"""
UNITY_ONE = re.compile(r"<(/?)\s*([A-Za-z]+)\s*(?:=\s*(.*?))?\s*/?>", re.S)
UNITY_NAMES = {
    "aqua": "00ffff", "cyan": "00ffff", "black": "000000", "blue": "0000ff", "brown": "a52a2a", "darkblue": "0000a0",
    "fuchsia": "ff00ff", "magenta": "ff00ff", "green": "008000", "grey": "808080", "lightblue": "add8e6", "lime": "00ff00",
    "maroon": "800000", "navy": "000080", "olive": "808000", "orange": "ffa500", "purple": "800080", "red": "ff0000",
    "silver": "c0c0c0", "teal": "008080", "white": "ffffff", "yellow": "ffff00"}
"""Unity 富文本自带的颜色名（原神有 `<color=cyan>`）。绝区零的 `#yellow` / `#grey03` 是游戏自己的调色板名，查不到，那段不上色。"""


def color_of(v: str | None) -> str | None:
    """`<color=…>` 的值 -> `#rrggbb`；认不出是 None。`#rrggbbaa` 去掉透明度（三款游戏全是 `ff`）；
    七位（`#00e1fff`、`#b9effff`，上游笔误）取前六位；五位这类长度不对的认不出。"""
    v = (v or "").strip().strip("\"'").lower()
    if v in UNITY_NAMES:
        return "#" + UNITY_NAMES[v]
    m = re.fullmatch(r"#([0-9a-f]{6})(?:[0-9a-f]|[0-9a-f]{2})?", v)
    return "#" + m.group(1) if m else None


def reading_of(v: str | None) -> str:
    """Unity 的 `<ruby=…>` 的值 -> 读音：去掉引号，空白（含换行）并成一个空格。空的是 ""（这处 Ruby 不要）。"""
    return " ".join((v or "").strip().strip("\"'").split())


def unity_ruby(reading: str) -> str:
    """Unity 的 Ruby 开标签 `<ruby="读音">`（各游戏的写法先换成它，`from_unity` 再收成规范写法；闭标签是 `</ruby>`）。
    读音里的标签删掉（星铁有带字色的读音）。"""
    return f'<ruby="{UNITY_TAG.sub("", reading)}">'


def from_unity(t: str) -> str:
    """Unity 富文本 -> 规范写法：认得的标签归一，配不上对的、认不出颜色的、不带的删掉（里面的字留着），没闭合的在末尾补上，
    交叉的（`<b>x<i>y</b>z</i>`）按层拆开重开，空的一对去掉。`plain(结果) == UNITY_TAG.sub("", t)`。
    Ruby 不进样式的那一摞、也不拆：嵌在 Ruby 里的、读音空的 Ruby 丢掉（正文留着）；Ruby 外面开的样式在 Ruby 里面关的，
    回到 Ruby 开头先关掉、Ruby 里重开（样式拆开，Ruby 整个留着）；Ruby 关的时候里面开着的样式先关、Ruby 外面重开。"""
    out: list[str] = []
    stack: list[tuple[str, str | None]] = []                   # (名字, 开标签；None = 认不出颜色的那层，只占位不出标签)
    ruby: list[bool] = []                                      # 开着的 Ruby 各层：True = 出了标签的那层（最多一层）
    r_at, r_idx = 0, 0                                         # 出了标签的那层 Ruby：开的时候 stack 里有几层在它外面；它在 out 里的下标

    def close(k: int) -> None:                                 # 关掉 stack[k:] 并重开 stack[k+1:]
        above = stack[k + 1:]
        for name, tag in reversed(stack[k:]):
            if tag:
                out.append(f"</{name}>")
        del stack[k:]
        for name, tag in above:
            stack.append((name, tag))
            if tag:
                out.append(tag)

    def tags(seg: list[tuple[str, str | None]], shut: bool) -> list[str]:
        return [f"</{name}>" for name, tag in reversed(seg) if tag] if shut else [tag for _, tag in seg if tag]

    def ruby_close() -> None:                                  # Ruby 里面开着的样式先关、出闭标签、再在外面重开
        out.extend([*tags(stack[r_at:], True), "</ruby>", *tags(stack[r_at:], False)])

    pos = 0
    for m in UNITY_TAG.finditer(t):
        out.append(t[pos:m.start()])
        pos = m.end()
        g = UNITY_ONE.fullmatch(m.group(0))
        if not g:
            continue
        shut, name, val = g.group(1) == "/", g.group(2).lower(), g.group(3)
        if name == RUBY:
            if shut:
                if ruby and ruby.pop():
                    ruby_close()
            elif (r := reading_of(val)) and not any(ruby):
                ruby.append(True)
                r_at, r_idx = len(stack), len(out)
                out.append(f"<ruby={r}>")
            else:
                ruby.append(False)
            continue
        if name not in SIMPLE and name != "color":
            continue
        if shut:
            k = next((j for j in range(len(stack) - 1, -1, -1) if stack[j][0] == name), None)
            if k is not None:
                if any(ruby) and k < r_at:                     # 外面开的样式在 Ruby 里关：stack[k:r_at] 改成在 Ruby 开头关掉、Ruby 里重开
                    seg = stack[k:r_at]
                    out[r_idx:r_idx + 1] = [*tags(seg, True), out[r_idx], *tags(seg, False)]
                    r_idx += len(tags(seg, True))
                    r_at = k
                close(k)
            continue
        if name == "color":
            c = color_of(val)
            tag = f"<color={c}>" if c else None
        else:
            tag = f"<{name}>"
        stack.append((name, tag))
        if tag:
            out.append(tag)
    out.append(t[pos:])
    if any(ruby):
        ruby_close()
    if stack:
        close(0)
    s = "".join(out)
    while True:                                                # 空的、只包着空白的一对（绝区零逐字上色的 `<color=…>\r\n</color>`、正文是空白的 Ruby）：去掉标签、空白留着
        u = re.sub(r"<(b|i|u|s|color|ruby)(?:=[^<>]+)?>(\s*)</\1>", r"\2", s)
        if u == s:
            return s
        s = u


def plain(t: str) -> str:
    """规范写法 -> 纯文字（删掉标签，Ruby 的读音跟着删）。"""
    return CANON_RE.sub("", t)


def select(t: str, style: bool = True, ruby: bool = True) -> str:
    """只留开着的那种标签：`style` 管粗体 / 斜体 / 下划线 / 删除线 / 字色，`ruby` 管 Ruby（关掉的连读音删掉、正文留着）。两个都关就是 `plain`。"""
    return CANON_RE.sub(lambda m: m.group(0) if (ruby if m.group(2) == RUBY else style) else "", t)


def ruby_spans(t: str) -> list[tuple[int, int]]:
    """规范写法里每处 Ruby 的正文在 `plain(t)` 里的范围 `[a, b)`，按先后。"""
    out, n, at = [], 0, None
    pos = 0
    for m in CANON_RE.finditer(t):
        n += m.start() - pos
        pos = m.end()
        if m.group(2) == RUBY:
            if m.group(1):
                if at is not None:
                    out.append((at, n))
                at = None
            else:
                at = n
    return out


BREAKS = re.compile(r"\\[Nn]")
"""字幕的换行写法（`\\N` 两个字），投影时当空白。"""


def project(styled: str, target: str) -> str | None:
    """把 `styled`（规范写法）的标签插进 `target`（同一句话的纯文字，经过了断行、去空白之类的处理）：两边**非空白的字**逐个对上，
    开标签贴着它后面那个字、闭标签贴着它前面那个字；行的布局一律以 `target` 为准。两边的 `\\N` / `\\n`（字幕的换行写法）都当空白——
    存进产物的带样式那份本身就带 `\\N`。两个标签之间没有字的（只包着空白的一对）两个都不要。对不上（两边的字不一样）是 None。"""
    src: list[tuple[str, str]] = []                            # ("tag", 标签) / ("ch", 字)，空白和换行写法不要

    def chars(seg: str) -> list[tuple[str, str]]:
        return [("ch", c) for c in BREAKS.sub(" ", seg) if not c.isspace()]
    pos = 0
    for m in CANON_RE.finditer(styled):
        src += chars(styled[pos:m.start()])
        src.append(("tag", m.group(0)))
        pos = m.end()
    src += chars(styled[pos:])
    out: list[str] = []
    pending: list[str] = []                                    # 等下一个字的开标签
    k, j, n = 0, 0, len(target)
    while k < len(src):
        kind, v = src[k]
        if kind == "tag":
            name = CANON_RE.fullmatch(v).group(2)
            if v.startswith("</") and any(CANON_RE.fullmatch(p).group(2) == name for p in pending):
                pending.pop(max(i for i, p in enumerate(pending) if CANON_RE.fullmatch(p).group(2) == name))   # 中间没有字：这一对都不要
            elif v.startswith("</"):
                out.append(v)                                  # 闭标签贴着前一个字：先出
            else:
                pending.append(v)
            k += 1
            continue
        while j < n and (target[j].isspace() or target.startswith(("\\N", "\\n"), j)):
            step = 2 if target[j] == "\\" else 1
            out.append(target[j:j + step])
            j += step
        if j >= n or target[j] != v:
            return None
        out += pending
        pending = []
        out.append(v)
        j += 1
        k += 1
    rest = target[j:]
    if rest.replace("\\N", "").replace("\\n", "").strip():
        return None
    return "".join(out) + rest


def to_ass(t: str, esc: Callable[[str], str]) -> str:
    """规范写法 -> ASS 正文：字经 `esc`（转义花括号、换行变 `\\N`），每段字前面写一个覆盖标签块，只写和已经生效的状态不同的几项。
    闭标签回到外层的状态：字色回到外面那层的颜色，最外层回到样式的颜色（空的 `\\1c`）——阶段 4 给整行定了字色时把它换成那个色。
    Ruby 写成它自己的注释块 `{ruby:读音}` … `{/ruby}`，就在标签的位置，不和样式块合并（开之前先把样式块写出来，Ruby 的范围只包着正文和它里面的样式）；
    读音里的花括号、反斜杠换成全角，换行换成空格（`RUBY_SAFE`）。"""
    out: list[str] = []
    colors: list[str] = []
    depth = dict.fromkeys(SIMPLE, 0)
    shown: dict[str, object] = {"color": None, **dict.fromkeys(SIMPLE, False)}

    def sync() -> None:
        now = {"color": colors[-1] if colors else None, **{k: v > 0 for k, v in depth.items()}}
        block = [("\\1c" if v is None else ass_color(v)) if k == "color" else f"\\{k}{int(v)}"
                 for k, v in now.items() if v != shown[k]]
        if block:
            out.append("{" + "".join(block) + "}")
        shown.update(now)
    pos = 0
    for m in CANON_RE.finditer(t):
        if m.start() > pos:
            sync()
            out.append(esc(t[pos:m.start()]))
        pos = m.end()
        shut, name, val = m.group(1) == "/", m.group(2), m.group(3)
        if name == RUBY:
            if not shut:
                sync()
            out.append("{/ruby}" if shut else "{ruby:%s}" % (val or "").translate(RUBY_SAFE))
        elif name == "color":
            if shut:
                if colors:                                     # 规范写法成对，不该空；人改坏了也不崩
                    colors.pop()
            else:
                colors.append(val)
        else:
            depth[name] = max(0, depth[name] + (-1 if shut else 1))
    if pos < len(t):
        sync()
        out.append(esc(t[pos:]))
    return "".join(out)


def ass_color(c: str) -> str:
    """`#rrggbb` -> `\\1c&HBBGGRR&`。"""
    return "\\1c&H%s&" % (c[5:7] + c[3:5] + c[1:3]).upper()
