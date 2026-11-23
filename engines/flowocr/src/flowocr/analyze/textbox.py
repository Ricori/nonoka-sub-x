"""叠加用的**文本框画像**和**原文整块**（line-breaks 计划第 1 步、匹配器这一侧）。

阶段 3 / 4 要照目标语言版的样子分行、摆行（`output.script.wrap_lines`、`output.layout.place_rows`），要用到几样只有剧本在手才判得出的东西，
所以在匹配器里判、写进 matched（`annotate`；字段的契约在 artifacts.md 的匹配产物一节）：

* **原文整块**（正文 / extra 条目的 `block` / `block_src` / `block_drawn_by`）：这句日文在屏上每一行的框，按日文剧本的行号从上到下排。
  收进条目的 OCR 行按文字对到剧本第几行（`assign`）；没收进来的行先在同一条 cue 里找真实事件（缺的首行常被认成名牌），
  找不到按行距合成（`fill`）。日文剧本只有一行的条目，整块就是 OCR 行。**不覆盖 `rows`**：名牌分组（`scriptmatch.name_roles`）拿正文 `rows` 的首行
  判名牌挂没挂在台词上，补出的首行正好落在那个名牌事件上，写进 `rows` 会静默改掉名牌层。
* **画像**（`overlay.textbox[区域号]`，`profile`）：`valign` / `pitch`（按整块的行数分组、用下沿量）、`em`（行宽对字宽的回归斜率）、
  `chars`（自动折行的证据定的"一行能放几个全角字"）、`box_w`（`box_width`）、`align` / `h_inertia`（整行读全的行给三种对齐打分，`align_of`）、`evidence`。
  按单个匹配产物（一段视频）估，按区域分。
* **惯性**（`inertia`）：整块建好之后，按同区域前后各 `INERTIA_K` 条正文条目的真实行吸附——单句的 OCR 框会因漏行、半行框、Ruby 撑高、
  界面字混进来而抖，前后同一个文本框里台词行的位置是一样的。只改画在哪里，不改 `rows`、不改名牌分组。
* **版式文件**（`layout_for`，`layouts/<游戏>.json`）：每款游戏从整场产物导出的画像（`dev_tools/layout_export.py`），按游戏画面存；
  先定游戏画面在录像里的位置（`locate`：手给的、整个画面、按对话框反推），再只补这一段量不出的字段
  （短片段样本不够：《魔裁》的文本框宽要 2000 句、zzz 的折行证据整场才 7 条），量得出的照旧用现量的。
* **按字数放宽**（`widen`）：惯性之后，有 `em` 时每一行按它该显示的日文放宽到墨迹宽 × `em`（从对齐边往外，只放宽）：补出的行、OCR 漏读了行尾或只读到一截的行
  框比日文窄，不放宽日文行尾会露在底板外。

整块里的位置是**屏上的行**：日文剧本的一行被游戏自动折成几行时，这一行占几个位置（每个 OCR 行一个），行号顺序不变。

收进条目的 OCR 行已经剔过界面字和注音（`tracksio.cue_lines` 按轨的 `ui_lines` 和 `ruby` 标不给，`scriptmatch.body_of` 只留字落在剧本原文里的），
没被标出的注音（「うなだ」这类读音）、混进来的界面字（「NO DATA」）对不上剧本的任何一行，不进整块、不算折行证据。
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from flowocr import provenance, textfold
from flowocr.artifacts import tracksio

MODES = ("top", "bottom", "center")
"""`valign` 的取值，同阶段 3 的 `output.layout.VALIGNS`（守卫核两边一致；这里不 import 阶段 3）。判不出时 `top`。"""

ROW_IN_LINE = 0.6
"""一个 OCR 行的内容字（去掉标点空白、`textfold.fold` 折过）有这么多按顺序落在日文剧本的某一行里，才算这一行的字（`assign`、`fill`）；
同 `scriptmatch.BODY_IN_REF` 的口径。"""
ROW_NEAR = 0.5
"""按位置核对行（`assign`）：一个 OCR 行按它对到的剧本行推出的整块首行下沿，和其余行推出的中位差超过这么多个行距就不收。"""
EM_ROW_IN_LINE = 0.9
"""量 `em` 只用对得很准的行：内容字有这么多落在剧本行里。"""
EM_MIN_ROWS = 20
"""量 `em` 至少要这么多行（行宽各不相同的），不够就没有 `em`（阶段 3 退回按框适配字号）。"""
EM_TRIM = 3.0
"""量 `em` 时先回归一次，残差超过这么多倍 MAD（×1.4826）的行（框被别的字撑宽的）去掉再回归一次。"""
PITCH_MIN = 3
"""行距至少要这么多个相邻两行的样本，不够就没有 `pitch`（阶段 3 按整块现算）。"""
PITCH_OF_HEIGHT = 1.4
"""没量出行距的区域，补行、换算锚点时按行高的这么多倍当行距（《魔裁》f1 主对话区行距 58 px、行高 42 px，约 1.38）。"""
VALIGN_MIN_ITEMS = 5
"""判 `valign`：一种行数至少要这么多条正文条目才算一组；至少要两组行数不同的才判得出。"""
VALIGN_TOL = 0.5
"""判 `valign`：三种锚点里最稳的那个，各组中位的极差要在这么多个行距以内，否则判不出（`top`）。"""
EVENT_NEAR = 0.5
"""补缺的行：同一条 cue 里的真实事件，下沿和按行距推出的位置差在这么多个行距以内才用。"""
WRAP_MIN_EVIDENCE = 2
"""一段视频里自动折行的证据至少要这么多条才给 `chars`（一行能放几个全角字），不够就不折（line-breaks 计划，暂定）。"""
WRAP_MIN_HELD = 0.5
"""`chars` 让这么多比例的折行证据成立才给，不到就不折（暂定）：成立得少说明证据不是同一个框宽折出来的（别的版式、读错的行混在里面），
取出的 C 只代表其中一小撮。次要区域多是这样，zzz 区 7 只有 9 / 23 条成立；主对话区 gi1 / gi2 / hsr / zzz 是 228 / 251、614 / 630、98 / 109、6 / 7。"""
NO_BREAK_START = "ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶー々ゝゞヽヾ"
"""不放行首的字（小书写假名、长音、叠字号）：断点后面是它们时，游戏可能是按避头规则提前断的，那一行不是被宽度逼满的，不当证据。"""
ALIGN_MIN_ROWS = 5
"""给对齐打分至少要这么多个整行读全的 OCR 行（`align_of`），不够就用建轨判的对齐、横向不吸（暂定）。"""
ALIGN_MAX_SPREAD = 0.25
"""对齐打分：最好的那种对齐的离散（字格边的 MAD，以 `em` 计）不超过它才算判得出（暂定）。判得出的主对话区（九场里 zzz 以外的八场）都在 0.03–0.08。"""
ALIGN_MARGIN = 0.5
"""对齐打分：最好的离散还要不超过次好的这么多倍，两种对齐分不开（行宽都差不多）的不判（暂定）。"""
INERTIA_K = 10
"""惯性的参照：同一区域、时间上前后各这么多条正文 / extra 条目（暂定）。"""
INERTIA_TOL = 0.5
"""惯性：条目整块的锚和参照差不到这么多个行高（参照真实行的中位行高）、吸过去之后每个真实行的下沿也挪不到这么多个行高，就是同一版式、吸过去；
否则当成另一种版式，用自己的框（暂定）。"""
INERTIA_EDGE_Q = 0.2
"""惯性·横向的参照边：左对齐取参照真实行左沿的这个分位、右对齐取右沿的 1 − 它（居中取中心的中位）。不取中位：开头是括号、省略号的行
左沿偏右，这种行多的几段里中位会偏到真左沿右边（f1 两段中位约 450、真左沿约 426），本身缩进了的行就吸不回去。"""
INERTIA_MIN_REF = 3
"""惯性：前后有真实行的参照条目不到这么多条就用自己的框。"""
WIDEN_TOL = 0.5
"""按字数放宽（`widen`）：行框比期望宽窄不到这么多个 `em` 的不放（OCR 框的抖动）。"""
BOX_MIN_LINES = 2000
"""游戏几乎不自动折行（折行证据不到 `WRAP_MIN_EVIDENCE` 条）时，整行读全的行里不同的剧本行到这么多句，才拿其中最宽的一行
（再加 `WIDEN_TOL` 个 `em`）当文本框宽 `box_w`（`box_width`，暂定）。最宽的行要够多句才贴着框的右沿：《魔裁》f1–f5 主对话区按时间连续取 N 句（按整块里的 OCR 行宽近似），
最宽那行比整场最宽窄（中位 / 最差，字宽）N = 500 时 1.2–1.7 / 2.3–4.7，1000 时 0.6–1.1 / 2.0–3.3，2000 时 0.4–0.9 / 0.8–1.5；
整场 4.1k–9.6k 句，最宽那行的字格宽 30.2–30.8，f4 唯一一条折行证据给的框宽是 30–31。少了框宽偏窄、把贴着框的译文误缩；
不到这么多句（《魔裁》每分钟 12–16 句，约 2.2–2.7 小时）不给，译文照旧可以放到画面宽——超框出一两个字比误缩轻。"""
WIDEN_CAP_Q = 0.99
"""按字数放宽的上限：区域有折行证据（到 `WRAP_MIN_EVIDENCE` 条）、`chars` 却是 null（成立的不到一半）时，期望宽超过本区域 OCR 行宽的这个分位
（再加 `WIDEN_TOL` 个 `em`）的行不放（暂定）。这种区域游戏会折行、只是框宽定不下来，补出的行拿的又是剧本整行——zzz 区 7 没有这条时合成行放到 73.5 个字宽，OCR 行约 23。
没有折行证据的区域不设：OCR 只读到一截的长行要放到整行宽，它常比 p99 宽（《魔裁》f1「……どうやら、またしてもエマくんの勘違いだったみたいだね。」读到 420 px、整行约 1000 px，
九场里设了会挡掉 f1–f5 6 / 6 / 8 / 4 / 8 行、行尾露在底板外）；`chars` 有值的区域靠 `chars` 那条判。
不取最大值：读错、混进来的宽行会把它拉高。混排的区域（zzz 区 7：约 41 字宽的任务描述和约 20 字宽的聊天）上限按最宽的版式取，
窄版式里的整行合成行仍可能放宽到它。"""
FULL_INK = "…‥"
"""行尾的这几个标点字形占满一格；其余全角的句读点、问叹号、右括号（和行首的左括号）字形只占半格左右，期望宽扣掉半格（`ink_cw`）。"""


@dataclass
class Row:
    """收进条目的一个 OCR 行（`row_groups` 分出来的）：框、行内事件按左右拼起来的字、行里有没有打了 `ruby` 标的事件。"""
    box: list[float]
    text: str
    ruby: bool = False


@dataclass
class Slot:
    """原文整块的一行。`line` 是日文剧本的行号（整块就是 OCR 行时 −1）；`src` 是 `ocr` / `event:<id>` / `synth`，补之前缺的是 None；
    `jp` 是这一屏行该显示的日文（`row_jp`；补的行是剧本那一整行），按它放宽行框（`widen`），对不上剧本的行是 None。"""
    box: list[float] | None
    src: str | None
    line: int
    drawn_by: int | None = None
    jp: str | None = None


@dataclass
class Draft:
    """一个正文 / extra 条目的原文整块（建块、补行、吸附都在它上面做）。`hits` 是每个 OCR 行对到的 (剧本行号或 −1, 分, 起, 止)，
    起止是这一行在剧本那一行里盖住的字的下标（`align`）。"""
    idx: int
    region: int
    start: int
    lines: list[str]
    rows: list[Row]
    hits: list[tuple[int, float, int, int]]
    slots: list[Slot]
    cue_evs: list[dict] = field(default_factory=list)
    own: set[int] = field(default_factory=set)


# ---------------------------------------------------------------- OCR 行与剧本行对位


def row_groups(evs: list[dict]) -> list[tuple[list[float], list[dict]]]:
    """正文事件按 cy 分行：(行框 = 行内事件框的并集, 行内事件)，从上到下。`scriptmatch.rows_of` 取的就是这里的行框。"""
    rows: list[tuple[list[float], list[dict]]] = []
    for e in sorted(evs, key=lambda e: (e["box"][1] + e["box"][3]) / 2):
        x0, y0, x1, y1 = e["box"]
        if rows and (y0 + y1) / 2 - (rows[-1][0][1] + rows[-1][0][3]) / 2 <= 0.5 * (y1 - y0):
            r = rows[-1][0]
            rows[-1] = ([min(r[0], x0), min(r[1], y0), max(r[2], x1), max(r[3], y1)], rows[-1][1] + [e])
        else:
            rows.append(([x0, y0, x1, y1], [e]))
    return rows


def ocr_rows(evs: list[dict]) -> list[Row]:
    return [Row(list(b), "".join(e["text"] for e in sorted(m, key=lambda e: e["box"][0])),
                any("ruby" in (e.get("flags") or ()) for e in m)) for b, m in row_groups(evs)]


def _content(s: str) -> list[tuple[str, int]]:
    """内容字：去掉空白和标点（Unicode 类别 P*），每个字 `textfold.fold` 折过，带它在原串里的下标。"""
    return [(textfold.fold(c), i) for i, c in enumerate(s) if not c.isspace() and not unicodedata.category(c).startswith("P")]


def align(text: str, line: str) -> tuple[float, int, int]:
    """OCR 行 `text` 对剧本的一行 `line`：(内容字按顺序落在 `line` 里的比例, 起, 止)，起止是盖住的字在 `line` 里的下标（左闭右开）；
    盖到了这一行的头一个 / 最后一个内容字时，起止延到行首 / 行尾（开头的「、末尾的。…也算这一行的）。
    整行都是标点的 OCR 行只对得上整行都是标点的剧本行（`……`）。"""
    q, c = _content(text), _content(line)
    if not q:
        return (1.0, 0, len(line)) if not c and text.strip() else (0.0, 0, 0)
    blocks = [b for b in SequenceMatcher(None, [k for k, _ in q], [k for k, _ in c], autojunk=False).get_matching_blocks() if b.size]
    if not blocks:
        return 0.0, 0, 0
    lo, hi = blocks[0].b, blocks[-1].b + blocks[-1].size - 1
    s = 0 if lo == 0 else c[lo][1]
    e = len(line) if hi == len(c) - 1 else c[hi][1] + 1
    return sum(b.size for b in blocks) / len(q), s, e


def positions(out: list[tuple[int, float, int, int]]) -> dict[int, int]:
    """对上了剧本行的 OCR 行在整块里的位置（从上往下第几行）：剧本第 j 行之前每被折开一次多占一个位置；
    同一剧本行里盖的字和前一个重叠的（同一截读了两遍）和前一个同位置。"""
    pos: dict[int, int] = {}
    extra, last_j, last_e = 0, -1, 0
    for r, (j, _, s, e) in enumerate(out):
        if j < 0:
            continue
        if j == last_j and s >= last_e - 1:                       # 同一剧本行接着往下的一截：自动折行
            extra += 1
            last_e = e
        elif j != last_j:
            last_e = e
        pos[r] = j + extra
        last_j = j
    return pos


def assign(rows: list[Row], lines: list[str], p: float | None = None) -> list[tuple[int, float, int, int]]:
    """每个 OCR 行对到剧本第几行：行号从上到下不减（同一行可以连着收几个 OCR 行：自动折行），分数和最大；
    够不上 `ROW_IN_LINE` 的不对（−1）。同一剧本行收到的几个 OCR 行盖的字重叠（同一行读了两遍）时只留一个，首尾相接的（被折开的两截）都留。

    给了行距 `p`（区域的，`annotate` 第二遍）时再按位置核：每个对上的行的下沿减去（它在整块里的位置 × `p`）是整块首行的下沿，
    读了两遍的留和其余行推出的首行下沿（中位）近的那个，没给 `p` 或推不出时留分高的；对上的行有 3 个以上时，
    推出的首行下沿离中位超过 `ROW_NEAR` 个行距的不收——误读会把一行对错位：f1「ありえない可能性を除けば……」那条首行被读成第 2 行的字，
    只按字对，真正的第 2 行被当成重读丢掉、首行又补到文本框上方去。"""
    n, k = len(rows), len(lines)
    sc = [[align(r.text, ln) for ln in lines] for r in rows]
    best = [[0.0] * (k + 1) for _ in range(n + 1)]
    pick = [[-1] * (k + 1) for _ in range(n + 1)]
    for r in range(n - 1, -1, -1):
        for j0 in range(k, -1, -1):
            b, c = best[r + 1][j0], -1
            for j in range(j0, k):
                s = sc[r][j][0]
                if s >= ROW_IN_LINE and s + best[r + 1][j] > b + 1e-9:
                    b, c = s + best[r + 1][j], j
            best[r][j0], pick[r][j0] = b, c
    out: list[tuple[int, float, int, int]] = []
    j0 = 0
    for r in range(n):
        j = pick[r][j0]
        out.append((j, *sc[r][j]) if j >= 0 else (-1, 0.0, 0, 0))
        if j >= 0:
            j0 = j
    none = (-1, 0.0, 0, 0)

    def anchors() -> dict[int, float]:
        return {r: rows[r].box[3] - q * p for r, q in positions(out).items()} if p else {}
    changed = True
    while changed:
        changed = False
        anc = anchors()
        mid = statistics.median(anc.values()) if anc else None
        for j in range(k):
            on = [r for r in range(n) if out[r][0] == j]
            for a, b in zip(on, on[1:]):
                if out[b][2] < out[a][3] - 1:                       # 后一个从前一个盖到的地方之前就开始：同一截读了两遍
                    da, db = (abs(anc[a] - mid), abs(anc[b] - mid)) if mid is not None else (0.0, 0.0)
                    if p and abs(da - db) > ROW_NEAR * p:
                        drop = a if da > db else b
                    else:
                        drop = b if out[b][1] <= out[a][1] else a
                    out[drop] = none
                    changed = True
                    break
            if changed:
                break
    anc = anchors()
    if len(anc) >= 3:
        mid = statistics.median(anc.values())
        for r, a in anc.items():
            if abs(a - mid) > ROW_NEAR * p:
                out[r] = none
    return out


def row_jp(rows: list[Row], lines: list[str], hits: list[tuple[int, float, int, int]]) -> list[str | None]:
    """每个 OCR 行在屏上显示的日文：剧本那一行只落在一个 OCR 行上就是那一整行（OCR 只读到一截也按整行）；被游戏折成几个 OCR 行的，
    按后一截盖到的起点切开、各取自己那一截（切点不递增时各取自己盖住的那段）。对不上剧本行的、打了 ruby 标的是 None。"""
    out: list[str | None] = [None] * len(rows)
    for j, line in enumerate(lines):
        on = [r for r, h in enumerate(hits) if h[0] == j and not rows[r].ruby]
        cuts = [0] + [hits[r][2] for r in on[1:]] + [len(line)]
        if all(a < b for a, b in zip(cuts, cuts[1:])):
            for r, a, b in zip(on, cuts, cuts[1:]):
                out[r] = line[a:b]
        else:
            for r in on:
                out[r] = line[hits[r][2]:hits[r][3]]
    return out


def draft_of(idx: int, it: dict, region: int, shown: list[dict], p: float | None = None) -> Draft:
    """条目的原文整块（补行之前）：剧本有几行就几个位置（一行被折成几个 OCR 行的占几个），没收进来的行留空（`box` 为 None）。
    剧本只有一行、或者没有一个 OCR 行对得上剧本时，整块就是 OCR 行。`p`：区域的行距，给了就按位置核对行（`assign`）。"""
    lines = [x for x in (it.get("jp") or "").split("\\N") if x.strip()]
    rows = ocr_rows(shown)
    hits = assign(rows, lines, p) if lines else [(-1, 0.0, 0, 0)] * len(rows)
    jps = row_jp(rows, lines, hits)
    if len(lines) <= 1 or all(h[0] < 0 for h in hits):
        slots = [Slot(list(r.box), "ocr", -1, jp=t) for r, t in zip(rows, jps)]
    else:
        slots = []
        for j in range(len(lines)):
            got = [r for r, h in enumerate(hits) if h[0] == j]
            slots += [Slot(list(rows[r].box), "ocr", j, jp=jps[r]) for r in got] or [Slot(None, None, j, jp=lines[j])]
    return Draft(idx, region, it["start_us"], lines, rows, hits, slots)


# ---------------------------------------------------------------- 画像


def offset(valign: str, i: int, k: int, p: float) -> float:
    """整块 k 行里第 i 行的下沿相对"整块的锚"的位置：`top` 的锚是首行下沿、`bottom` 是末行下沿、`center` 是首末两行下沿的中点。"""
    if valign == "bottom":
        return -(k - 1 - i) * p
    if valign == "center":
        return (i - (k - 1) / 2) * p
    return i * p


def real(slots: list[Slot]) -> list[tuple[int, Slot]]:
    """整块里的真实行（OCR 行和同一条 cue 里找到的事件，合成的和还缺着的不算），带它在整块里的位置。"""
    return [(i, s) for i, s in enumerate(slots) if s.box is not None and s.src != "synth"]


def anchor(slots: list[Slot], valign: str, p: float) -> float | None:
    """条目整块的锚：每个真实行的下沿按 `valign` 换算（`offset`）后取中位。"""
    xs = [s.box[3] - offset(valign, i, len(slots), p) for i, s in real(slots)]
    return statistics.median(xs) if xs else None


def own_pitch(slots: list[Slot]) -> float | None:
    """一个整块自己的行距：相邻两个真实行下沿之差 / 隔了几行，取中位；不到两个真实行是 None。"""
    rs = real(slots)
    xs = [(b.box[3] - a.box[3]) / (j - i) for (i, a), (j, b) in zip(rs, rs[1:])]
    return statistics.median(xs) if xs else None


def cw_sum(s: str) -> float:
    return sum(textfold.cw(c) for c in s)


def breakable(c: str) -> bool:
    """能当"宽度逼出来的折行"一侧的字：全角的字母 / 数字（汉字、假名…），不是空格、标点、符号。"""
    return textfold.cw(c) == 1.0 and unicodedata.category(c)[0] in "LN"


def wrap_evidence(d: Draft) -> list[dict]:
    """自动折行的证据：剧本的同一行连着落在两个 OCR 行上、断点前后是两个能断开的全角字（`breakable`，断点后不是 `NO_BREAK_START`），
    前一截刚好盖到断点。断在空格、标点处的可能是按词断的，不算（f1「…見た者はいない……␣正体は…」，按它估是 27.7 字）。
    每条证据：下界 = 被折的那一截的宽（全角字为单位，`textfold.cw`），上界 = 下界 + 下一截首字的宽。"""
    out = []
    for j, line in enumerate(d.lines):
        on = [r for r, h in enumerate(d.hits) if h[0] == j and not d.rows[r].ruby]
        for n, (a, b) in enumerate(zip(on, on[1:])):
            start = 0 if n == 0 else d.hits[a][2]
            brk = d.hits[b][2]
            if not (start < brk < len(line)) or abs(d.hits[a][3] - brk) > 1:
                continue
            if not (breakable(line[brk - 1]) and breakable(line[brk]) and line[brk] not in NO_BREAK_START):
                continue
            lo = cw_sum(line[start:brk])
            out.append({"lo": lo, "hi": lo + textfold.cw(line[brk]), "t_us": d.start, "jp": line})
    return out


def fit_chars(ev: list[dict]) -> tuple[float | None, int]:
    """一行能放几个全角字：让最多条折行证据成立的 C，返回 (C, 成立的条数)；证据不到 `WRAP_MIN_EVIDENCE` 条是 (None, 0)，
    成立的不到 `WRAP_MIN_HELD` 是 (None, 成立的条数)。
    一条证据在 lo ≤ C ≤ hi 时成立；候选 C 只取各条的下界（真见过放得下的宽），并列取最大的。
    hi 取闭：游戏字体是比例字、`cw` 把全角字一律算 1，断点前后差一个字在量的误差里——gi1 主对话区 251 条证据里下界 40 的 126 条、41 的 102 条，
    取 41 两组都成立，日文尺（多行 cue 断点全对上的比例）41 比 40 高（90.7% / 88.7%）。
    不取下界的最大值：一两条离群证据（读错的行、别的版式混进来）会把它拉高，gi1 有 2 条下界 50，按最大值取 50、日文尺 2.3%。"""
    if len(ev) < WRAP_MIN_EVIDENCE:
        return None, 0

    def held(c: float) -> int:
        return sum(1 for x in ev if x["lo"] <= c <= x["hi"])
    best = max({x["lo"] for x in ev}, key=lambda c: (held(c), c))
    n = held(best)
    return (best if n >= WRAP_MIN_HELD * len(ev) else None), n


def fit_em(samples: list[tuple[float, float]]) -> float | None:
    """行宽（px）对行字宽（全角字为单位）的回归斜率：去掉检测框外扩的常数项；先回归一次、去掉残差离群的（`EM_TRIM`）再回归一次。"""
    def ols(xy):
        n = len(xy)
        mx, my = sum(x for x, _ in xy) / n, sum(y for _, y in xy) / n
        sxx = sum((x - mx) ** 2 for x, _ in xy)
        if sxx <= 0:
            return None
        b = sum((x - mx) * (y - my) for x, y in xy) / sxx
        return b, my - b * mx
    if len(samples) < EM_MIN_ROWS or len({x for x, _ in samples}) < 3:
        return None
    f = ols(samples)
    if f is None:
        return None
    res = [y - (f[0] * x + f[1]) for x, y in samples]
    mad = statistics.median(abs(r) for r in res) * 1.4826
    kept = [s for s, r in zip(samples, res) if abs(r) <= EM_TRIM * mad] if mad > 0 else samples
    f = ols(kept) if len(kept) >= EM_MIN_ROWS else f
    return f[0] if f and f[0] > 0 else None


def valign_of(drafts: list[Draft], p: float) -> tuple[str, dict]:
    """按整块的行数分组（`VALIGN_MIN_ITEMS` 条以上的组），三种锚点（`offset`）各算各组的中位，取各组之间极差最小的那种；
    极差超过 `VALIGN_TOL` 个行距、或不到两组时判不出，`top`。返回 (valign, 诊断)。"""
    groups: dict[int, list[Draft]] = defaultdict(list)
    for d in drafts:
        if real(d.slots):
            groups[len(d.slots)].append(d)
    groups = {k: g for k, g in groups.items() if len(g) >= VALIGN_MIN_ITEMS}
    diag: dict = {"groups": {str(k): len(g) for k, g in sorted(groups.items())}}
    if len(groups) < 2:
        return "top", {**diag, "by": "none"}
    spread = {}
    for m in MODES:
        meds = [statistics.median(anchor(d.slots, m, p) for d in g) for g in groups.values()]
        spread[m] = round(max(meds) - min(meds), 1)
    best = min(MODES, key=lambda m: (spread[m], MODES.index(m)))
    diag["spread"] = spread
    if spread[best] > VALIGN_TOL * p:
        return "top", {**diag, "by": "none"}
    return best, {**diag, "by": "stats"}


def region_pitch(drafts: list[Draft]) -> tuple[float | None, int]:
    """区域的行距：各整块里相邻两个真实行下沿之差 / 隔了几行，取中位；样本不到 `PITCH_MIN` 个是 None。返回 (行距, 样本数)。"""
    xs = []
    for d in drafts:
        rs = real(d.slots)
        xs += [(b.box[3] - a.box[3]) / (j - i) for (i, a), (j, b) in zip(rs, rs[1:])]
    return (statistics.median(xs) if len(xs) >= PITCH_MIN else None), len(xs)


def pitch_eff(drafts: list[Draft], pitch: float | None) -> float:
    """补行、换算锚点用的行距：量出了就是它，没量出按真实行行高中位的 `PITCH_OF_HEIGHT` 倍；一个真实行都没有是 0。"""
    if pitch:
        return pitch
    hs = [s.box[3] - s.box[1] for d in drafts for _, s in real(d.slots)]
    return statistics.median(hs) * PITCH_OF_HEIGHT if hs else 0.0


def cell_edges(text: str, box: list[float], em: float) -> tuple[float, float]:
    """一行 OCR 框换成字格的左右沿：行首全角左括号、行尾全角句读点 / 右括号字形只占半格（`ink_cw` 同一张表），检测框只框到墨迹，各往外补半个 `em`。"""
    t = text.strip()
    x0, x1 = box[0], box[2]
    if t and textfold.cw(t[0]) == 1.0 and unicodedata.category(t[0]) in ("Ps", "Pi"):
        x0 -= 0.5 * em
    if len(t) > 1 and textfold.cw(t[-1]) == 1.0 and t[-1] not in FULL_INK and unicodedata.category(t[-1]) in ("Pe", "Pf", "Po"):
        x1 += 0.5 * em
    return x0, x1


def align_of(drafts: list[Draft], em: float | None, region_meta: dict | None) -> tuple[str, bool, dict]:
    """区域的横向对齐：(对齐, 判没判出, 诊断)。整行读全的 OCR 行（`align_scores`）里，离散最小的那种对齐不超过 `ALIGN_MAX_SPREAD`、
    又小于次好的 `ALIGN_MARGIN` 倍，就是它、横向吸附（`h_inertia`）；否则用建轨判的（`regions[].align`）、不吸。

    为什么不直接用建轨判的：建轨拿区域里全部的字统计，括号开头、漏读了开头的行，和混进来的别的字都算进去，判不出时退回投票
    （《魔裁》f5 主对话区是投票判的左对齐，按原来"统计判出才吸"的规则横向不吸，左沿偏右的 935 行中文再缩进一次）；
    投票还会判错方向（hsr 区 0 是居中的台词、建轨判左，gi2 区 21 是对话选项、字在图标后左对齐、建轨判居中）。
    这里只拿对上剧本一整行的行、按字格边比：《魔裁》f1–f5、gi1 / gi2 / hsr 的主对话区判得出，离散 0.03–0.08 个字宽，其余两种 1.9–5.5；
    zzz 的主对话区判不出（左 / 中 / 右 1.79 / 1.17 / 3.65，行首位置不定），照旧用建轨判的左对齐、横向不吸。
    **整块居中、块里各行左对齐**的版式（zzz 区 39 / 60 的旁白、gi2 区 29 线索簿的提示气泡）单行时打分是居中，多行时短行却贴着左沿：
    判出居中、而多行条目块里各行的左沿差得比中心少（`inner_left`）的，不判、照旧用建轨判的、不吸——两种对齐都画不对这种版式，不改动它。"""
    track = (region_meta or {}).get("align") or "center"
    sc = align_scores(drafts, em) if em else {"n": 0}
    if sc["n"] < ALIGN_MIN_ROWS:
        return track, False, {"by": "tracks", **sc}
    order = sorted(("left", "right", "center"), key=lambda k: sc[k])
    best, second = sc[order[0]], sc[order[1]]
    if best <= ALIGN_MAX_SPREAD and best < ALIGN_MARGIN * second:                # 严格小于：三种离散全是 0（同一行反复出现）时不判
        if order[0] == "center" and inner_left(drafts, em):
            return track, False, {"by": "inner_left", **sc}
        return order[0], True, {"by": "rows", **sc}
    return track, False, {"by": "tracks", **sc}


def inner_left(drafts: list[Draft], em: float) -> bool:
    """多行条目块里各行左沿对齐、中心不对齐（整块居中、行左对齐的版式）：各条目真实行字格左沿的极差、中心的极差各取中位，
    左沿的不超过 `ALIGN_MAX_SPREAD` 个 `em`、中心的超过 1 个 `em`。多行条目不到 `ALIGN_MIN_ROWS` 条的不算。"""
    lefts, centers = [], []
    for d in drafts:
        # 取全部 OCR 行（含只读到一截的），不像 `align_scores` 只取整行读全的：混合版式的证据正是短行，
        # 个别漏读行首的行把极差拉大，靠取中位压住（gi2 区 29 认对）
        cells = [cell_edges(r.text, r.box, em) for r in d.rows]
        if len(cells) >= 2:
            lefts.append(max(x0 for x0, _ in cells) - min(x0 for x0, _ in cells))
            cs = [(x0 + x1) / 2 for x0, x1 in cells]
            centers.append(max(cs) - min(cs))
    if len(lefts) < ALIGN_MIN_ROWS:
        return False
    return statistics.median(lefts) <= ALIGN_MAX_SPREAD * em and statistics.median(centers) > em


def full_cells(drafts: list[Draft], em: float) -> list[tuple[str, float, float]]:
    """整行读全的 OCR 行（对上剧本一整行、内容字 ≥ `EM_ROW_IN_LINE`、不带 Ruby）：(剧本那一行, 字格（`cell_edges`）左沿, 右沿)。"""
    return [(d.lines[j], *cell_edges(d.lines[j], r.box, em)) for d in drafts for r, (j, sc, s, e) in zip(d.rows, d.hits)
            if j >= 0 and sc >= EM_ROW_IN_LINE and not r.ruby and s == 0 and e == len(d.lines[j])]


def align_scores(drafts: list[Draft], em: float) -> dict:
    """三种对齐各自的离散：整行读全的 OCR 行（`full_cells`）的左沿 / 右沿 / 中心的 MAD，以 `em` 为单位；行数 `n` 不到 3 只给 `n`。"""
    cells = full_cells(drafts, em)
    ls, rs, cs = [x0 for _, x0, _ in cells], [x1 for _, _, x1 in cells], [(x0 + x1) / 2 for _, x0, x1 in cells]

    def mad(xs):
        m = statistics.median(xs)
        return statistics.median(abs(x - m) for x in xs) / em
    return {"n": len(ls), **({k: round(mad(v), 3) for k, v in (("left", ls), ("right", rs), ("center", cs))} if len(ls) >= 3 else {})}


def box_width(drafts: list[Draft], em: float | None, chars: float | None, evidence: int) -> tuple[float | None, dict]:
    """文本框宽 `box_w`（阶段 3 译文横向放宽、超出就整条缩小的上限）：有 `chars` 是 `chars × em`（游戏在这里折行）；
    没有、游戏又几乎不自动折行（折行证据不到 `WRAP_MIN_EVIDENCE` 条，如《魔裁》：超出时缩字号、不折，owner 2026-11-10）时，
    整行读全的行里不同的剧本行到 `BOX_MIN_LINES` 句，取最宽那行的字格宽加 `WIDEN_TOL` 个 `em`；都不够是 None（阶段 3 放到画面宽）。
    有折行证据、`chars` 却定不下来的（zzz 区 7）不在这里量：游戏会折，缩字号不是它的样子。返回 (框宽, 诊断，`n` 是不同的剧本行数)。"""
    if chars and em:
        return chars * em, {"by": "chars"}
    if not em or evidence >= WRAP_MIN_EVIDENCE:
        return None, {"by": "none", "n": 0}
    widest: dict[str, float] = {}
    for line, x0, x1 in full_cells(drafts, em):
        widest[line] = max(widest.get(line, 0.0), x1 - x0)
    if len(widest) >= BOX_MIN_LINES:
        return max(widest.values()) + WIDEN_TOL * em, {"by": "rows", "n": len(widest)}
    return None, {"by": "none", "n": len(widest)}


# ---------------------------------------------------------------- 版式文件

LAYOUT_SCHEMA = "flowocr-layout/2"
LAYOUT_DIR = Path(__file__).with_name("layouts")
"""随代码走的版式文件，一款游戏一份，文件名是匹配器认的游戏名（gametext 剧本的 `game`、naninovel 的 `--ctx game=`）。
条目按**游戏画面**（视口，`Viewport`）存：`dx` 是中心离视口中心的横向距离 / 视口高（左负右正），`cy` 是离视口顶 / 视口高，`em_h` 是一字宽 / 视口高；
顶层 `aspect` 是导出时视口的宽 / 高。横向按「离中心、以高为单位」量：游戏界面一般按高缩放、对话框居中，游戏画面变宽变窄这个距离不变。"""
LAYOUT_NEAR = 0.08
"""区域对上版式文件的一条：区域中心（建轨的 `features.cx / cy`）换到视口里，和条目的纵向差不超过它个视口高、横向差不超过它个视口宽（`aspect` 个视口高）（暂定）。
同一个对话框在 5 分钟切片里的中心比整场偏 0.01–0.03（行数不同时整块的中心跟着挪），zzz 切片里主对话区被拆成几块时到 0.05；
整场 zzz 离主对话框最近的两块界面字（QTE 提示、NPC 名牌）在 0.085 / 0.104，不认。
反推视口（`locate`）时视口上下各允许出界它个视口高。"""
LAYOUT_EM_TOL = 0.1
"""量出了 `em` 的区域，`em / 视口高` 还要和条目的相差不超过这个比例，才算同一种版式（暂定）：位置近、字号差得多的是别的框。"""
LAYOUT_OVER_MAX = 0.05
"""`entry_fits`：整行读全的行里宽过条目框宽的超过这个比例（至少 2 行），就不是这个框（暂定）。不要求一行都不超：主对话区也混着少数读宽了的行
（gi1 / gi2 主对话区 1.3% / 0.7%、zzz 0.1%，最宽到 57.7 个字）；短片段里整行读全的只有十几行，一行读宽就按比例超了，所以至少要 2 行。"""
VIEWPORT_MIN_H = 0.25
"""反推的视口高至少是录像高的这么多（暂定）：主播布局把游戏缩得再小也不到四分之一高，再小的多半是拿别的字配出来的。"""

LAYOUT_LENGTHS = ("em_h", "pitch_em", "box_em", "chars", "latin_em")
"""版式条目里的长度字段，有就得是正数；`em_h` 必填。"""


def load_layout(path: str | Path) -> dict:
    """读一份版式文件；`schema` 不对、`aspect` 或条目字段不合规就报错（别的格式静默当成没有条目，兜底就悄悄失效了；坏值写进匹配产物要到阶段 3 才报）。
    `chars` 存成整数。`source` / `sha` 记进产物的统计，文件改了产物对得上是哪一版（版式文件不算代码，不进 `code_fp`）。"""
    raw = Path(path).read_bytes()
    doc = json.loads(raw.decode("utf-8"))
    if doc.get("schema") != LAYOUT_SCHEMA:
        raise ValueError(f"{path} 不是这一版的版式文件（schema 要 {LAYOUT_SCHEMA}，是 {doc.get('schema')!r}；"
                         "换一份这个格式的版式文件，或者加 --ctx layout=none 不用）")
    boxes = doc.get("boxes")
    if not isinstance(boxes, list):
        raise ValueError(f"{path}：boxes 要是列表")

    def num(v) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)     # json 把 1e309 读成 inf

    if not (num(doc.get("aspect")) and doc["aspect"] > 0):
        raise ValueError(f"{path}：aspect={doc.get('aspect')!r} 要是有限的正数（游戏画面的宽 / 高）")
    for i, b in enumerate(boxes):
        where = f"{path} 的 boxes[{i}]（{b.get('id') if isinstance(b, dict) else b!r}）"
        if not isinstance(b, dict) or not isinstance(b.get("id"), str):
            raise ValueError(f"{where}：要是带字符串 id 的对象")
        bad = ([k for k, ok in (("dx", num(b.get("dx"))), ("cy", num(b.get("cy")) and 0 <= b["cy"] <= 1)) if not ok]
               + [k for k in LAYOUT_LENGTHS if (k in b or k == "em_h") and not (num(b.get(k)) and b[k] > 0)]
               + [k for k, ok in (("valign", MODES), ("align", tracksio.ALIGNS)) if k in b and b[k] not in ok])
        if "chars" not in bad and "chars" in b and b["chars"] != int(b["chars"]):
            bad.append("chars")
        if bad:
            raise ValueError(f"{where}：{'、'.join(f'{k}={b.get(k)!r}' for k in bad)} 不合规（dx 是有限的数，cy 在 0–1，长度是有限的正数、em_h 必填，"
                             f"chars 是整数，valign 取 {MODES}，align 取 {tracksio.ALIGNS}）")
        if "chars" in b:
            b["chars"] = int(b["chars"])
    return {**doc, "source": provenance.portable(path), "sha": hashlib.sha256(raw).hexdigest()[:12]}


def layout_for(path: str | None, game: str | None, viewport: str | None = None) -> dict | None:
    """这次匹配用的版式文件：`--ctx layout=<文件>` 给了就用它（`none` 是不用，A/B 的对照臂）；没给时按匹配器选中的游戏找
    `LAYOUT_DIR/<游戏>.json`；都没有是 None（只靠现量）。`viewport`（`--ctx viewport=`，没给时建轨记下的）是游戏画面在录像里的位置，记在返回的 `viewport` 里，
    不给时匹配时推算（`locate`）；不用版式文件（`none`）时它没有用处，只核格式。"""
    vp = tracksio.parse_viewport(viewport)
    if path == "none":
        return None
    if path:
        return {**load_layout(path), "viewport": vp}
    if game and (LAYOUT_DIR / f"{game}.json").is_file():
        return {**load_layout(LAYOUT_DIR / f"{game}.json"), "source": f"layouts/{game}.json", "viewport": vp}
    return None


@dataclass(frozen=True)
class Viewport:
    """游戏画面在录像里的位置（像素）；`frame` 是录像的 (宽, 高)，`by` 是怎么定的（`ctx` / `frame` / `inferred`，`locate`）。"""
    x: float
    y: float
    w: float
    h: float
    frame: tuple[float, float]
    by: str

    def rel(self, f: dict) -> tuple[float, float]:
        """区域中心（`features.cx / cy`，录像画面的比例）换成版式条目的 (`dx`, `cy`)。"""
        W, H = self.frame
        return (f["cx"] * W - (self.x + self.w / 2)) / self.h, (f["cy"] * H - self.y) / self.h

    def box(self) -> list[float]:
        return [round(v, 1) for v in (self.x, self.y, self.w, self.h)]


def layout_entry(layout: dict | None, region_meta: dict | None, em: float | None, vp: Viewport | None) -> dict | None:
    """区域对上版式文件里的哪一条：中心换到视口 `vp` 里在 `LAYOUT_NEAR` 以内、量出了 `em` 的再要 `em / 视口高` 在 `LAYOUT_EM_TOL` 以内，
    几条都对上取中心最近的；对不上是 None。"""
    f = (region_meta or {}).get("features") or {}
    if not layout or vp is None or "cx" not in f:
        return None
    dx, cy = vp.rel(f)
    best = None
    for e in layout.get("boxes") or ():
        dist = max(abs(dx - e["dx"]) / layout["aspect"], abs(cy - e["cy"]))
        if dist > LAYOUT_NEAR:
            continue
        if em and abs(em / vp.h - e["em_h"]) > LAYOUT_EM_TOL * e["em_h"]:
            continue
        if best is None or dist < best[0]:
            best = (dist, e)
    return best[1] if best else None


def entry_fits(entry: dict, drafts: list[Draft], em: float, ev: list[dict]) -> bool:
    """位置、字号对上了的条目，还要和这个区域自己的行对得上，才是同一个框：整行读全的行里宽过条目的框宽（`box_em`，没有就 `chars`）
    加 `WIDEN_TOL` 个 `em` 的不超过 `LAYOUT_OVER_MAX`（至少 2 行才算超），区域的折行证据在条目的 `chars` 下成立的不少于 `WRAP_MIN_HELD`。
    hsr 区 16 在主对话框旁边、字号一样，唯一一条折行证据却在 45–46 字处断（主对话框 55），是别的框。"""
    box = entry.get("box_em") or entry.get("chars")
    ws = [x1 - x0 for _, x0, x1 in full_cells(drafts, em)]
    if box and ws and sum(1 for w in ws if w > (box + WIDEN_TOL) * em) > max(1, LAYOUT_OVER_MAX * len(ws)):
        return False
    if ev and entry.get("chars"):
        return sum(1 for x in ev if x["lo"] <= entry["chars"] <= x["hi"]) >= WRAP_MIN_HELD * len(ev)
    return True


@dataclass
class Region:
    """一个区域的现量，画像（`profile`）和对版式文件（`locate`）共用：`meta` 是 tracks 的 `regions[]` 里这一项，
    `em` / `n_em`（用了几行）是 `region_em` 量的，`ev` 是折行证据（`wrap_evidence`）。"""
    meta: dict | None
    drafts: list[Draft]
    em: float | None
    n_em: int
    ev: list[dict]
    fits: dict[tuple[str, float], bool] = field(default_factory=dict)


def region_of(drafts: list[Draft], meta: dict | None) -> Region:
    return Region(meta, drafts, *region_em(drafts), [x for d in drafts for x in wrap_evidence(d)])


def match_entry(layout: dict | None, r: Region, vp: Viewport | None) -> tuple[dict | None, str | None]:
    """区域在视口 `vp` 下对上的条目（`layout_entry`），还要过 `entry_fits`（按条目和用的一字宽记在 `r.fits` 里：`locate` 试几个视口时不重算；
    量不出一字宽的区域用条目按视口高换算的，跟着视口变）。返回 (条目, 位置对上了、行对不上而不认的条目 id)。"""
    e = layout_entry(layout, r.meta, r.em, vp)
    if e is None:
        return None, None
    key = (e["id"], r.em or e["em_h"] * vp.h)
    if key not in r.fits:
        r.fits[key] = entry_fits(e, r.drafts, key[1], r.ev)
    return (e, None) if r.fits[key] else (None, e["id"])


def locate(layout: dict | None, regions: list[Region], W: float, H: float) -> tuple[Viewport | None, dict | None]:
    """游戏画面在录像里的位置（视口），返回 (视口, 诊断)；没有版式文件是 (None, None)。按这个顺序定：

    1. 给了（`layout["viewport"]`：`--ctx viewport=`，没给时建轨记下的 `tracksio.recorded_viewport`）就用它，出了录像画面报错；
    2. **整个画面**：按 `aspect` 放进录像、居中（16:9 录像就是整个画面）。有区域对得上条目（`match_entry`）就用它——全屏录像和视口之前的判法逐位相同；
    3. **反推**：整个画面一个区域都对不上时，每个量出了 `em` 的区域配每一条条目，按「这个区域就是这一条」反推视口（高 = `em / em_h`，
       再由中心和条目的 `dx` / `cy` 定位置）。视口纵向要落在录像里（上下各允许 `LAYOUT_NEAR` 个视口高出界）、中心横向在录像里
       （横向不要求整个落进去：游戏比版式文件窄、界面按高缩放时，按 `aspect` 算的视口比游戏画面宽）、高不小于 `VIEWPORT_MIN_H` 个录像高，
       配它的区域还要过 `entry_fits`。几个都行取对得上的区域条目数（认上剧本行的台词）最多的，打平取高的大的；
    4. 都不行按整个画面（对不上就不补），诊断记 `fallback`：和整个画面真对上了的 `frame` 分开，导出不收它。

    反推出来的视口按构造就让配它的区域位置、字号全对上，判据比整个画面弱，所以整个画面对得上就不反推。
    录像和版式文件的长宽比几乎一样（差不到千分之一）时整个画面就是录像本身：`aspect` 存的是 5 位小数，按它算会差零点几个像素。"""
    if not layout:
        return None, None
    a = layout["aspect"]
    if layout.get("viewport"):
        tracksio.check_viewport(layout["viewport"], W, H)
        vp = Viewport(*layout["viewport"], (W, H), "ctx")
        return vp, {"by": "ctx", "box": vp.box()}

    def support(vp: Viewport) -> int:
        return sum(len(r.drafts) for r in regions if match_entry(layout, r, vp)[0])

    same = abs(W / H - a) < 1e-3 * a
    h = H if same else min(H, W / a)
    w = W if same else h * a
    frame = Viewport((W - w) / 2, (H - h) / 2, w, h, (W, H), "frame")
    if support(frame):
        return frame, {"by": "frame", "box": frame.box()}
    best = None
    for r in regions:
        f = (r.meta or {}).get("features") or {}
        if not r.em or "cx" not in f:
            continue
        for e in layout["boxes"]:
            h = r.em / e["em_h"]
            vx, vy = f["cx"] * W - e["dx"] * h, f["cy"] * H - e["cy"] * h
            if h < VIEWPORT_MIN_H * H or vy < -LAYOUT_NEAR * h or vy + h > H + LAYOUT_NEAR * h or not 0 <= vx <= W:
                continue
            vp = Viewport(vx - h * a / 2, vy, h * a, h, (W, H), "inferred")
            if match_entry(layout, r, vp)[0] is not e:
                continue
            key = (support(vp), h)
            if best is None or key > best[0]:
                best = (key, vp, (r.meta or {}).get("index"), e["id"])
    if best is None:
        return frame, {"by": "fallback", "box": frame.box()}
    _, vp, reg, eid = best
    return vp, {"by": "inferred", "box": vp.box(), "region": reg, "entry": eid, "items": best[0][0]}


def region_em(drafts: list[Draft]) -> tuple[float | None, int]:
    """区域的一字宽：认上剧本行的 OCR 行，行宽对字宽回归（`fit_em`）。返回 (一字宽, 用了几行)。"""
    em_xy = []
    for d in drafts:
        for r, (j, sc, s, e) in zip(d.rows, d.hits):
            if j >= 0 and sc >= EM_ROW_IN_LINE and not r.ruby and e > s:
                em_xy.append((cw_sum(d.lines[j][s:e]), r.box[2] - r.box[0]))
    return fit_em(em_xy), len(em_xy)


def profile(r: Region, layout: dict | None = None, vp: Viewport | None = None) -> tuple[dict, dict]:
    """一个区域的画像（契约字段）和诊断。`r.meta` 是 tracks 的 `regions[]` 里这一项（对齐打分判不出时用它的 `align`，`align_of`）。

    `chars` 取让最多条折行证据成立的值（`fit_chars`；证据到 `WRAP_MIN_EVIDENCE` 条、成立的到 `WRAP_MIN_HELD` 才给），诊断里报成立、不成立各几条。
    不用全体行宽的分位数：屏上的行宽只是框宽的下界。

    区域在视口 `vp`（`locate`）下对上版式文件（`layout`，`match_entry`）的一条时，**只补量不出的**：`em`（按视口高换算）、`pitch`、判不出的 `valign`、`chars`、`box_w`、
    判不出的对齐（判出整块居中、行左对齐的不补，`align_of`），长度按 `em` 换算；英文一个拉丁字平均占几个一字宽（`latin_em`，从日文录像量不出）有就带；量得出的照旧用现量的，短片段上量错了也不改，
    长片段不会被过时的文件带偏（owner 2026-11-10：短片段允许错误率高一些，但不能全部失效）。诊断的 `layout` 记补了哪些。"""
    drafts, em, ev = r.drafts, r.em, r.ev
    pitch, n_pitch = region_pitch(drafts)
    entry, rejected = match_entry(layout, r, vp)
    got: list[str] = []
    e = entry or {}
    if not em and e:
        em = e["em_h"] * vp.h
        got.append("em")
    if not pitch and e.get("pitch_em") and em:
        pitch = e["pitch_em"] * em
        got.append("pitch")
    p_eff = pitch_eff(drafts, pitch)
    valign, vdiag = valign_of(drafts, p_eff) if p_eff else ("top", {"by": "none"})
    if vdiag["by"] == "none" and e.get("valign"):
        valign = e["valign"]
        got.append("valign")
    chars, held = fit_chars(ev)
    if chars is None and e.get("chars"):
        chars = e["chars"]
        got.append("chars")
    align, h_inertia, adiag = align_of(drafts, em, r.meta)
    if adiag["by"] == "tracks" and adiag.get("n", 0) < ALIGN_MIN_ROWS and e.get("align"):   # 行够了、分不开的是量过了，不补
        align, h_inertia = e["align"], True
        got.append("align")
    box_w, bdiag = box_width(drafts, em, chars, len(ev))
    if not box_w and e.get("box_em") and em:
        box_w = e["box_em"] * em
        got.append("box_w")
    prof = {"valign": valign, "pitch": round(pitch, 2) if pitch else None, "em": round(em, 3) if em else None,
            "chars": chars, "box_w": round(box_w, 1) if box_w else None,
            "align": align, "h_inertia": h_inertia, "evidence": len(ev)}
    if e.get("latin_em"):              # 只有版式文件给得出（日文录像量不出英文字宽），有就带
        prof["latin_em"] = e["latin_em"]
        got.append("latin_em")
    diag = {"valign": vdiag, "pitch_samples": n_pitch, "em_rows": r.n_em,
            "wrap_evidence": ev, "wrap_held": held, "wrap_broken": len(ev) - held if len(ev) >= WRAP_MIN_EVIDENCE else None,
            "align": {"tracks": (r.meta or {}).get("align"), **adiag}, "box_w": bdiag,
            "layout": {"entry": entry.get("id"), "fields": got} if entry else {"rejected": rejected} if rejected else None}
    return prof, diag


# ---------------------------------------------------------------- 补行


def fill(d: Draft, p: float, owner: dict[int, list[int]]) -> None:
    """补整块里缺的行（就地改）：先在同一条 cue 里找真实事件——字对得上缺的那一行（`ROW_IN_LINE`）、下沿离按行距推出的位置不到
    `EVENT_NEAR` 个行距、横向和真实几行的范围相交；找到用它的框（`event:<id>`），它被别的叠加条目画了（`owner`：事件 id → 引用它的条目下标）
    就记那个条目的下标。找不到就按行距合成（`synth`）：下沿从最近的真实行推，高取真实行的中位，横向取真实几行合起来的范围。"""
    rs = real(d.slots)
    if not rs or all(s.box is not None for s in d.slots):
        return
    x0, x1 = min(s.box[0] for _, s in rs), max(s.box[2] for _, s in rs)
    h = statistics.median(s.box[3] - s.box[1] for _, s in rs)
    used = set(d.own)
    for i, s in enumerate(d.slots):
        if s.box is not None:
            continue
        m, ref = min(rs, key=lambda t: abs(t[0] - i))
        bot = ref.box[3] + (i - m) * p
        best = None
        for e in d.cue_evs:
            if e["id"] in used:
                continue
            b = e["box"]
            sc = align(e["text"], d.lines[s.line])[0]
            if sc < ROW_IN_LINE or abs(b[3] - bot) > EVENT_NEAR * p or b[2] <= x0 or b[0] >= x1:
                continue
            key = (sc, -abs(b[3] - bot))
            if best is None or key > best[0]:
                best = (key, e)
        if best is not None:
            e = best[1]
            used.add(e["id"])
            s.box, s.src = [float(v) for v in e["box"]], f"event:{e['id']}"
            s.drawn_by = next((k for k in owner.get(e["id"], ()) if k != d.idx), None)
        else:
            s.box, s.src = [x0, bot - h, x1, bot], "synth"


# ---------------------------------------------------------------- 惯性


def edge(box: list[float], align_: str) -> float:
    return box[0] if align_ == "left" else box[2] if align_ == "right" else (box[0] + box[2]) / 2


def ref_edge(boxes: list[list[float]], align_: str) -> float:
    """横向的参照边：左对齐取左沿的低分位（`INERTIA_EDGE_Q`）、右对齐取右沿的高分位、居中取中心的中位（取最近的秩）。"""
    xs = sorted(edge(b, align_) for b in boxes)
    if align_ == "left":
        return xs[int(INERTIA_EDGE_Q * (len(xs) - 1))]
    if align_ == "right":
        return xs[-1 - int(INERTIA_EDGE_Q * (len(xs) - 1))]
    return statistics.median(xs)


def snap_x(box: list[float], align_: str, ref: float) -> list[float]:
    """横向吸附**只放宽、不收窄**，吸附后的框总盖住原框：对齐边只往参照外侧挪——左对齐左沿取 `min(x0, ref)`、右沿不小于 `ref`，右对齐对称；
    居中按 `ref` 对称地放到能盖住原框的最小范围。
    整框平移的话，偏右的行（开头括号的字形留白、OCR 漏读了行首）右沿跟着缩回来，原文行尾露在底板外（f1 `0101Adv17_Hanna006` 漏读「ふん、」：
    OCR 行 534–1092，平移到 428–986，行尾约 92 px 露在外面）；左沿硬放到参照的话，参照偏右的几段（f1 前后条目的左沿中位约 450、真左沿约 426）
    左边又露出 20 多 px。"""
    x0, y0, x1, y1 = box
    if align_ in ("left", "right"):                       # 两种都是把 `ref` 并进框：对齐边落到 `ref`（它在框外时），另一边不动
        return [min(x0, ref), y0, max(x1, ref), y1]
    return [min(x0, 2 * ref - x1), y0, max(x1, 2 * ref - x0), y1]


def inertia(drafts: list[Draft], prof: dict, p_eff: float, align_: str) -> dict:
    """一个区域的条目整块吸附到前后同类条目上（就地改 `slots` 的框），返回统计。

    参照 = 时间上前后各 `INERTIA_K` 条条目的**真实**行（合成的不算；用各自吸附之前的框）。竖直：每个真实行的下沿按 `valign` 换算成整块的锚
    （`offset`，行号用整块里的位置，缺首行的条目不会错一行）取中位，行距取参照条目各自行距的中位；条目自己的锚和它差不到 `INERTIA_TOL` 个行高、
    整块每一行的下沿放到参照锚 + 偏移（行高各留各的）之后每个真实行也挪不到 `INERTIA_TOL` 个行高，就是同一版式、吸过去；否则当成另一种版式，用自己的框。
    只看锚会把个别行推歪：hsr 区域 20（聊天记录）一条整块两行下沿相隔 30 px、参照行距约 67 px，锚差得少，按参照行距排第 2 行下沿就从 921 推到 954，落到日文第 2 行下面一行。
    同一版式、区域的对齐打分判得出（`h_inertia`，`align_of`）时，每一行按参照真实行的对齐边（`ref_edge`，不按行号分）放宽（`snap_x`：只放宽、不收窄）。
    参照不到 `INERTIA_MIN_REF` 条用自己的框。横向位移的统计是框的左右沿实际挪了多少（取大的）；`other_layout_row` 是另一种版式里锚对得上、按行判出来的条数。"""
    va = prof["valign"]
    order = sorted(drafts, key=lambda d: (d.start, d.idx))
    before = {d.idx: [None if s.box is None else list(s.box) for s in d.slots] for d in order}
    pitch_of = {d.idx: own_pitch(d.slots) for d in order}
    st = Counter()
    dys, dxs = [], []
    for n, d in enumerate(order):
        refs = [o for o in order[max(0, n - INERTIA_K):n] + order[n + 1:n + 1 + INERTIA_K] if real(o.slots)]
        if len(refs) < INERTIA_MIN_REF:
            st["few_ref"] += 1
            continue
        ps = [pitch_of[o.idx] for o in refs if pitch_of[o.idx]]
        p = statistics.median(ps) if ps else p_eff
        ref_rows = [(i, len(o.slots), before[o.idx][i]) for o in refs for i, _ in real(o.slots)]
        a_ref = statistics.median(b[3] - offset(va, i, k, p) for i, k, b in ref_rows)
        h_ref = statistics.median(b[3] - b[1] for _, _, b in ref_rows)
        a_own = anchor(d.slots, va, p)
        k = len(d.slots)
        if abs(a_ref - a_own) >= INERTIA_TOL * h_ref:
            st["other_layout"] += 1
            continue
        if any(abs(a_ref + offset(va, i, k, p) - s.box[3]) >= INERTIA_TOL * h_ref for i, s in real(d.slots)):
            st["other_layout"] += 1
            st["other_layout_row"] += 1
            continue
        st["same_layout"] += 1
        for i, s in enumerate(d.slots):
            bot = a_ref + offset(va, i, k, p)
            s.box = [s.box[0], bot - (s.box[3] - s.box[1]), s.box[2], bot]
        moved_y = max(abs(before[d.idx][i][3] - s.box[3]) for i, s in enumerate(d.slots))
        moved_x = 0.0
        if prof["h_inertia"]:
            e_ref = ref_edge([b for _, _, b in ref_rows], align_)
            for s in d.slots:
                nb = snap_x(s.box, align_, e_ref)
                moved_x = max(moved_x, abs(nb[0] - s.box[0]), abs(nb[2] - s.box[2]))
                s.box = nb
        dys.append(moved_y)
        dxs.append(moved_x)
        st["moved"] += moved_y > 0.5 or moved_x > 0.5
        st["moved_x"] += moved_x > 0.5
    return {**{k: st[k] for k in ("same_layout", "other_layout", "other_layout_row", "few_ref", "moved", "moved_x")},
            "dy": quantiles(dys), "dx": quantiles(dxs)}


# ---------------------------------------------------------------- 按字数放宽


def ink_cw(s: str) -> float:
    """一屏行日文的墨迹宽（全角字为单位）：字宽和（`cw`）减去行首左括号、行尾句读点 / 右括号字形里空着的半格（`FULL_INK` 除外）。
    检测框只框到墨迹：f1 整行读全的 OCR 行，行尾是标点的行宽比字宽和 × em 窄中位 0.47 个字、首尾都是标点的窄 1.10 个，首尾都不是的宽 0.13 个。"""
    t = s.strip()
    if not t:
        return 0.0
    w = cw_sum(t)
    if textfold.cw(t[0]) == 1.0 and unicodedata.category(t[0]) in ("Ps", "Pi"):
        w -= 0.5
    c = t[-1]
    if len(t) > 1 and textfold.cw(c) == 1.0 and c not in FULL_INK and unicodedata.category(c) in ("Pe", "Pf", "Po"):
        w -= 0.5
    return w


def widen(d: Draft, em: float, chars: float | None, align_: str, cap: float | None = None) -> list[tuple[str, float]]:
    """每一行按它该显示的日文（`Slot.jp`）放宽到期望宽 w = 墨迹宽（`ink_cw`）× `em`（就地改），返回放宽了的行 (来源, 放宽量 px)；
    窄不到 `WIDEN_TOL` 个 `em` 的不放。从对齐边往外放：左对齐 x1 = max(x1, x0 + w)，右对齐对称，居中按中心对称；**只放宽、不收窄**，同惯性。
    补出的行（cue 里的事件、合成的）框常比日文窄：合成的横向取真实几行的范围，被认成名牌的首行事件框只盖到行里一截；
    OCR 漏读了行尾、只读到一截的真实行也一样——底板只盖到读到的那截，日文行尾露在外面（f1 惯性之前比期望宽窄半个字以上的 1,613 行里，漏了行尾「……」（连同其后的标点）的 877 行，「決まっていた」对「決まっていた事だったんだ。」这类只读到一截的 100 行）。
    该显示的日文宽过 `chars` 的行不放：游戏会把它折成几行、这一屏行只是其中一截，不知道是哪一截（gi1 一行宽过 41 字的台词只收进了折出的第 2 截，按整行放会放到文本框宽）；
    期望宽超过 `cap`（有折行证据、`chars` 却是 null 的区域才有：本区域 OCR 行宽的 `WIDEN_CAP_Q` 分位，`annotate` 给）再加 `WIDEN_TOL` 个 `em` 的也不放，理由同上。
    居中的按这一行自己的中心放：只读到一截的行一侧仍会露一点、另一侧多盖，它放宽后可能成了最宽那行、带着阶段 3 的公共轴（`layout.anchor_axis`）挪
    （gi1 / gi2 / hsr / zzz 多行居中整块里最宽行中心挪了一个字宽以上的 1 / 2 / 6 / 0 条，含惯性的影响）。"""
    out = []
    for s in d.slots:
        if s.box is None or not s.jp or (chars and cw_sum(s.jp) > chars):
            continue
        w = ink_cw(s.jp) * em
        x0, y0, x1, y1 = s.box
        if x1 - x0 >= w - WIDEN_TOL * em or (cap is not None and w > cap + WIDEN_TOL * em):
            continue
        if align_ == "left":
            nb = [x0, y0, x0 + w, y1]
        elif align_ == "right":
            nb = [x1 - w, y0, x1, y1]
        else:
            c = (x0 + x1) / 2
            nb = [c - w / 2, y0, c + w / 2, y1]
        out.append(("event" if s.src.startswith("event:") else s.src, w - (x1 - x0)))
        s.box = nb
    return out


def quantiles(xs: list[float]) -> dict:
    """位移分布（px）：中位、p90、最大；空的是 {}。"""
    if not xs:
        return {}
    s = sorted(xs)
    return {"p50": round(s[(len(s) - 1) // 2], 1), "p90": round(s[int(0.9 * (len(s) - 1))], 1), "max": round(s[-1], 1)}


# ---------------------------------------------------------------- 入口


def annotate(items: list[dict], doc: dict, shown: dict[int, tuple[list[dict], list[dict]]],
             layout: dict | None = None) -> tuple[dict, dict]:
    """给 `items` 里的正文 / extra 条目写原文整块（`block` / `block_src` / `block_drawn_by`，就地改），返回 (画像 `overlay.textbox`, 统计)。

    `shown`：条目下标 → (这一段画的那条 cue 的正文事件（条目的 `rows` 就是它们的行框）, 那条 cue 的全部事件)，`scriptmatch.overlay_items` 给。
    条目的区域取它第一个事件的 `region`（阶段 3 同样取）。名牌、选项这些条目只当"谁画了这个事件"的查表用，不改。
    `layout`：版式文件（`layout_for`），先定游戏画面在录像里的位置（`locate`），区域量不出的字段从它补（`profile`）。"""
    events = doc["events"]
    owner: dict[int, list[int]] = defaultdict(list)
    for k, it in enumerate(items):
        for i in it.get("events") or ():
            owner[i].append(k)
    by_reg: dict[int, list[int]] = defaultdict(list)
    for k in shown:
        by_reg[events[items[k]["events"][0]]["region"]].append(k)

    def drafts_of(reg: int, p: float | None) -> list[Draft]:
        out = []
        for k in by_reg[reg]:
            d = draft_of(k, items[k], reg, shown[k][0], p)
            d.cue_evs, d.own = shown[k][1], set(items[k]["events"])
            out.append(d)
        return out
    metas = {r["index"]: r for r in doc.get("regions") or ()}
    regions = {}
    for reg in sorted(by_reg):
        # 两遍：先只按字对、量出区域的行距，再带着行距按位置核一遍（`assign`）
        ds = drafts_of(reg, None)
        pitch, _ = region_pitch(ds)
        regions[reg] = region_of(drafts_of(reg, pitch) if pitch else ds, metas.get(reg))
    vp, vdiag = locate(layout, list(regions.values()), *doc["size"]) if layout else (None, None)
    textbox, diag = {}, {}
    src = Counter()
    n_missing_first = n_first_filled = n_dropped = 0
    inert: dict = {}
    wid: list[tuple[str, float]] = []
    for reg, r in regions.items():
        ds = r.drafts
        prof, rdiag = profile(r, layout, vp)
        p_eff = pitch_eff(ds, prof["pitch"])
        for d in ds:
            if any(s.line >= 0 for s in d.slots):            # 整块按剧本行建的：对不上剧本行的 OCR 行不进整块
                n_dropped += sum(1 for h in d.hits if h[0] < 0)
            first_missing = bool(d.slots) and d.slots[0].box is None
            fill(d, p_eff, owner)
            n_missing_first += first_missing
            n_first_filled += first_missing and d.slots[0].box is not None
        align_ = prof["align"]
        inert[str(reg)] = inertia(ds, prof, p_eff, align_)
        if prof["em"]:                                       # 放宽在惯性之后：对齐边吸到参照后再从它往外放（OCR 漏读了行首的行左沿偏右）
            ws = sorted(s.box[2] - s.box[0] for d in ds for s in d.slots if s.src == "ocr" and s.box is not None)
            cap = (ws[int(WIDEN_CAP_Q * (len(ws) - 1))] if ws and prof["chars"] is None and prof["evidence"] >= WRAP_MIN_EVIDENCE
                   else None)
            for d in ds:
                wid += widen(d, prof["em"], prof["chars"], align_, cap)
        textbox[str(reg)] = prof
        diag[str(reg)] = {"items": len(ds), **rdiag}
        for d in ds:
            it = items[d.idx]
            it["block"] = [[round(v, 1) for v in s.box] for s in d.slots]
            it["block_src"] = [s.src for s in d.slots]
            it["block_drawn_by"] = [s.drawn_by for s in d.slots]
            src.update("event" if s.src.startswith("event:") else s.src for s in d.slots)
    stats = {"layout": {"source": layout["source"], "sha": layout["sha"]} if layout else None, "viewport": vdiag, "regions": diag, "block_src": dict(src), "ocr_rows_dropped": n_dropped,
             "first_missing": n_missing_first, "first_filled": n_first_filled,
             "drawn_by": sum(1 for it in items for x in it.get("block_drawn_by") or () if x is not None),
             "inertia": inert,
             "widened": {"rows": len(wid), "by_src": dict(Counter(k for k, _ in wid)), "dx": quantiles([x for _, x in wid])}}
    return textbox, stats


def summary(textbox: dict, stats: dict, top: int = 3) -> list[str]:
    """打印用的几行：条目最多的 `top` 个区域的画像与惯性，加整块各来源的行数。"""
    regs = sorted(stats["regions"], key=lambda r: -stats["regions"][r]["items"])[:top]
    out = []
    for r in regs:
        p, i = textbox[r], stats["inertia"][r]
        out.append(f"[文本框] 区域 {r}（{stats['regions'][r]['items']} 条）：{p['valign']}（{stats['regions'][r]['valign']['by']}）、"
                   f"行距 {p['pitch']}、一字宽 {p['em']}、一行 {p['chars']} 字（折行证据 {p['evidence']} 条）、{p['align']}对齐（{stats['regions'][r]['align']['by']}）、横向吸附 {'开' if p['h_inertia'] else '关'}；"
                   f"惯性：同一版式 {i['same_layout']}（挪了 {i['moved']}）、另一种版式 {i['other_layout']}（按行判出 {i['other_layout_row']}）、参照不够 {i['few_ref']}"
                   + (f"；版式文件补了 {stats['regions'][r]['layout']['fields']}" if (stats["regions"][r].get("layout") or {}).get("fields") else ""))
    if stats.get("layout"):
        v = stats["viewport"]
        how = ({"ctx": "给定的", "frame": "整个画面", "fallback": "整个画面，没有区域对上、也反推不出"}.get(v["by"])
               or f"反推：区域 {v['region']} 配 {v['entry']}，对上 {v['items']} 条")
        out.append(f"[文本框] 版式文件 {stats['layout']['source']}（{stats['layout']['sha']}）；游戏画面 {','.join(f'{x:g}' for x in v['box'])}（{how}）")
    out.append(f"[文本框] 原文整块的行：{stats['block_src']}，没进整块的 OCR 行 {stats['ocr_rows_dropped']}；缺首行的 {stats['first_missing']} 条补上 {stats['first_filled']}；"
               f"补的行被别的条目画着的 {stats['drawn_by']} 行；按字数放宽 {stats['widened']['rows']} 行 {stats['widened']['by_src']}、放宽量 {stats['widened']['dx']} px")
    return out
