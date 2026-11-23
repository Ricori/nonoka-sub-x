"""ASS 文件的读写：**按字节保留**不认识的东西，只改自己动过的字段。

为什么不用 pysubs2（script-fx 计划第 0 步）：它读成对象再整份重写，时间、样式数值按它的格式重出；
`restore` 要还原出逐字节相同的字幕稿、别人的行（kara-templater 的 `fx`、`Banner;`）要原样留着，做不到。
Aegisub 保存时会加 BOM 和它自己的节（`[Aegisub Project Garbage]` 等），这里一律原样保留。

另有两样字幕稿专用的：Effect 字段里 `fo:…` 参数的编解码（`encode_fo` / `parse_fo`），和正文里几个 tag 的读取。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

EVENT_KINDS = ("Dialogue", "Comment")


@dataclass
class Section:
    """一节：`name` 是方括号里的名字（文件开头 `[` 之前的内容是 name=None 的一节），`lines` 是节头之后的原始行。"""
    name: str | None
    head: str
    lines: list[str] = field(default_factory=list)


@dataclass
class Doc:
    sections: list[Section]
    nl: str = "\n"
    bom: bool = False

    def section(self, name: str) -> Section | None:
        low = name.lower()
        return next((s for s in self.sections if s.name and s.name.lower() == low), None)

    def dumps(self) -> str:
        out: list[str] = []
        for s in self.sections:
            if s.name is not None:
                out.append(s.head)
            out.extend(s.lines)
        text = self.nl.join(out)
        return ("\ufeff" if self.bom else "") + text


def loads(text: str) -> Doc:
    bom = text.startswith("\ufeff")
    if bom:
        text = text[1:]
    nl = "\r\n" if "\r\n" in text else "\n"
    sections = [Section(None, "")]
    for ln in text.split(nl):
        m = re.fullmatch(r"\s*\[([^\]]+)\]\s*", ln)
        if m:
            sections.append(Section(m[1], ln))
        else:
            sections[-1].lines.append(ln)
    return Doc(sections, nl, bom)


def format_of(sec: Section) -> list[str] | None:
    """一节的 `Format:` 字段名（去掉空白）。"""
    for ln in sec.lines:
        if ln.startswith("Format:"):
            return [x.strip() for x in ln.split(":", 1)[1].split(",")]
    return None


@dataclass
class Row:
    """一条 `Style:` / `Dialogue:` / `Comment:` 行：`kind` + 冒号后的空白 + 按 `Format` 切开的字段（最后一个字段吃掉剩下的逗号）。
    字段保留原样（含空白），`str()` 拼回去逐字节相同。"""
    kind: str
    ws: str
    fields: list[str]
    names: list[str]

    def __str__(self) -> str:
        return f"{self.kind}:{self.ws}{','.join(self.fields)}"

    def get(self, name: str) -> str:
        return self.fields[self.names.index(name)]

    def set(self, name: str, value: str) -> None:
        self.fields[self.names.index(name)] = value


def parse_row(line: str, names: list[str], kinds: tuple[str, ...]) -> Row | None:
    m = re.match(r"(\w+):(\s*)(.*)\Z", line, re.S)
    if not m or m[1] not in kinds:
        return None
    return Row(m[1], m[2], m[3].split(",", len(names) - 1), names)


# ---- Effect 字段里的 fo:… 参数 ----

FO_PREFIX = "fo:"
FO_ESC = "%;,{}\\\n\r"
"""值里要按百分号编码的字符：`;` 是项的分隔；`,` 会切断 ASS 行的字段（Effect 不是最后一个字段，Aegisub 保存时还会把逗号换成分号）；
`{` `}` `\\` 放进正文会被 libass 当成 tag（第 0 步探针：`{\\p1}` 会进绘图模式），Effect 里用不着也一并编码；`%` 本身；换行。
数值列表用空格分隔（`box=374 891 1588 931|…`），所以正常的参数里没有要编码的字符。"""


def fo_escape(v: str) -> str:
    return "".join(f"%{ord(c):02X}" if c in FO_ESC else c for c in v)


def fo_unescape(v: str) -> str:
    return re.sub(r"%([0-9A-Fa-f]{2})", lambda m: chr(int(m[1], 16)), v)


def encode_fo(params: dict) -> str:
    """`{"box": "…", "plate": True}` → `fo:box=…;plate`（写进 Effect 字段）。值为 True 的只写名字，None / False 的不写。"""
    items = []
    for k, v in params.items():
        if v is None or v is False:
            continue
        items.append(k if v is True else f"{k}={fo_escape(str(v))}")
    return FO_PREFIX + ";".join(items)


FO_AT = re.compile(r"(?:^|;)\s*fo:")


def parse_fo(effect: str) -> dict | None:
    """Effect 字段里的 `fo:…` → 参数（只有名字的项值为 True）；没有是 None。
    `fo:` 可以跟在别的内容后面（`备注;fo:…`、`Banner;10;fo:…`），它之后到末尾都是参数；前面那段只留在源行上，阶段 4 不看。"""
    m = FO_AT.search(effect)
    if not m:
        return None
    params: dict = {}
    for item in effect[m.end():].strip().split(";"):
        if not item:
            continue
        k, eq, v = item.partition("=")
        params[k] = fo_unescape(v) if eq else True
    return params


# ---- 正文里的 tag ----

def blocks(text: str) -> list[tuple[str, str]]:
    """正文切成 [("tags", 花括号里的内容) | ("text", 文字)]。没配对的 `{` 当文字（libass 也这样）。"""
    out, i = [], 0
    while i < len(text):
        j = text.find("{", i)
        k = text.find("}", j) if j >= 0 else -1
        if j < 0 or k < 0:
            out.append(("text", text[i:]))
            break
        if j > i:
            out.append(("text", text[i:j]))
        out.append(("tags", text[j + 1:k]))
        i = k + 1
    return out


TAG_RE = {
    "an": r"\\an(\d)",
    "pos": r"\\pos\(([^)]*)\)",
    "move": r"\\move\(([^)]*)\)",
    "fs": r"\\fs(?=[\d.])([\d.]+)",
    "fad": r"\\fad\(([^)]*)\)",
    "bord": r"\\bord([\d.]+)",
}
"""阶段 4 要读的几个 tag（整个 tag 匹配，见 `tag_of`）。`\\bord` 只在行首块里归阶段 4（字幕稿预览底框的边距，成品的底板按字号重算），行内的照留。`\\fs` 后面必须紧跟数字（不是 `\\fsp` / `\\fscx`）。"""

KARAOKE_RE = r"\\(?:ko|kf|K|k)(\d+)"


def split_tags(block: str) -> list[str]:
    """一个花括号块的内容切成**顶层**的 tag（`\\t(0,1000,\\fs80)` 是一个，里面的 `\\fs80` 不单算）；
    第一个 `\\` 之前的文字（注释）单独一项。拼回去和原内容相同。"""
    out, cur, depth = [], "", 0
    for c in block:
        if c == "\\" and depth == 0 and cur:
            out.append(cur)
            cur = ""
        cur += c
        if c == "(":
            depth += 1
        elif c == ")" and depth:
            depth -= 1
    if cur:
        out.append(cur)
    return out


def tag_of(tag: str) -> tuple[str, str] | None:
    """一个顶层 tag 是不是阶段 4 要读的那几种：(名字, 参数)；卡拉OK 的名字是 `k`、参数是厘秒。别的是 None。"""
    for name, pat in TAG_RE.items():
        m = re.fullmatch(pat, tag.rstrip())
        if m:
            return name, m[1]
    m = re.fullmatch(KARAOKE_RE, tag.rstrip())
    return ("k", m[1]) if m else None


def first_tag(text: str, name: str, lead_only: bool = False) -> str | None:
    """整行里第一次出现的某个顶层 tag 的参数（libass 对 `\\pos` / `\\move` / `\\an` / `\\fad` 也只认第一个）。
    `lead_only`：只看正文开头那个 tag 块（`\\fs` 这类局部生效的：行内的 `A{\\fs80}B` 只管后面那几个字，不是整行的字号）。"""
    for i, (kind, s) in enumerate(blocks(text)):
        if kind != "tags":
            if lead_only:
                return None
            continue
        for t in split_tags(s):
            got = tag_of(t)
            if got and got[0] == name:
                return got[1]
        if lead_only:
            return None
    return None


COLOR_RE = re.compile(r"\\1?c(?:&H([0-9A-Fa-f]+)&?)?\s*")
"""顶层的主色 tag（`\\1c&HBBGGRR&` / `\\c&H…&`；没有值的是回到样式的颜色）。`\\clip` 不算（整个 tag 匹配）。"""


def head_color(text: str) -> str | None:
    """正文开头那个 tag 块里最后生效的主色（BBGGRR，大写）；没有是 None。"""
    parts = blocks(text)
    if not parts or parts[0][0] != "tags":
        return None
    got = [m[1] for t in split_tags(parts[0][1]) if (m := COLOR_RE.fullmatch(t)) and m[1]]
    return got[-1].upper().zfill(6)[-6:] if got else None


def recolor_inline(text: str, base: str | None, head: bool = True) -> str:
    """行内（`head` 时跳过开头那个 tag 块）没有值的主色 tag（回到样式的颜色，剧本样式的闭标签就写成它，`richtext.to_ass`）
    换成 `base`（这一行实际的字色，BBGGRR；None 不换）。有值的不动，`\\t(…)` 里面的不碰。"""
    out = []
    for i, (kind, s) in enumerate(blocks(text)):
        if kind != "tags" or (head and i == 0):
            out.append(s if kind == "text" else "{" + s + "}")
            continue
        tags = []
        for t in split_tags(s):
            m = COLOR_RE.fullmatch(t)
            if m and not m[1] and base:
                t = "\\1c&H%s&" % base
            tags.append(t)
        out.append("{" + "".join(tags) + "}")
    return "".join(out)


def numbers(s: str) -> list[float]:
    """`"1,2"`（tag 的参数）或 `"1 2"`（`fo` 里的数值列表）→ [1.0, 2.0]。"""
    return [float(x) for x in re.split(r"[,\s]+", s.strip()) if x]
