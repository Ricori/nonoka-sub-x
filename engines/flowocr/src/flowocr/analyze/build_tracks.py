"""把逐帧 OCR 观测（obs.jsonl）合成带起止时间的条目，再聚成文字轨/区域。

三级结构：
    obs    逐帧的一个检测框
    run    同一位置、同一文本在连续采样帧上的一次出现 —— 带 start/end 的条目
    track  同一"行位置"上的所有 run（例如底部字幕这一行）
    region 若干 track 组成的块（例如整面滚动评论墙、一个对话框）

引擎无关：任何引擎只要吐出 obs.jsonl 就能用这个工具，指标口径才可比。

用法：
    python -m flowocr.analyze.build_tracks out/zh-multi/rapidocr-v6small.jsonl --outdir out/zh-multi
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import json
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
import re
from difflib import SequenceMatcher
from pathlib import Path

from flowocr.artifacts import evalkit          # noqa: E402
from flowocr.artifacts import obsio            # noqa: E402
from flowocr.artifacts import srtio            # noqa: E402
from flowocr.artifacts import tracksio  # noqa: E402
from flowocr.analyze import align  # noqa: E402
from flowocr.analyze import uigate  # noqa: E402
from flowocr import textfold  # noqa: E402
from flowocr.output import run_srt  # noqa: E402   写 SRT / 清旧产物（阶段 C 第二刀搬过去的，这里只 re-export）
from flowocr.provenance import DIRTY_PATHS, git_head, portable, version, code_fp as _code_fp  # noqa: E402,F401


# ---------- 基础几何 ----------

def iou(a: list[float], b: list[float]) -> float:
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / (area_a + area_b - inter)


def x_overlap_ratio(a: list[float], b: list[float], mutual: bool = False) -> float:
    """x 方向重叠占多少。

    默认除以**较窄**的那个——"这个窄条整个落在那条宽带的 x 范围内"算完全重叠。
    `mutual=True` 除以**较宽**的那个，要求互相都覆盖不少。

    为什么需要后者（yuka 整片实测）：这游戏在几乎每个高度都渲染小字，
    行高分位 5/50/95 是 26/37/60 px——**"字号相近"那道闸门形同虚设**；
    而宽度分位是 28/110/726 px，于是一条 111 px 的窄条落在 372 px 的字幕带里
    就算完全重叠，把字幕带和满屏小字串成一个 y 跨度 0.92 的巨型区域，
    字幕带作为独立区域直接消失（3536/3749 条 run 被吞）。缩小时间窗救不回来，
    因为串联发生在窗内。
    """
    ov = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    wa, wb = a[2] - a[0], b[2] - b[0]
    return ov / max(1.0, max(wa, wb) if mutual else min(wa, wb))


# 时钟/日期长什么样：12:34、12:34:56、06/17、2026-09-03
TIME_RE = re.compile(r"\d{1,2}\s*[:：]\s*\d{2}|\d{1,4}\s*[/\-年月]\s*\d{1,2}")


SIM_THR = 0.55
"""run 续接的文本相似度门（`--sim`）：一个观测和在场 run 的 `text_sim` 到这么多才算同一句（几何另有 `--iou`）。"""


PREFIX_MIN_CHARS = 3
"""前缀加分要求短的那一侧至少几个字（`--prefix-min 0` 关掉这道门）。

**3 不是新常数**：它就是 owner 定的三档里"有内容 ≥3 字"那条线
（`evalkit.triviality`），1–2 字属于 trivial 档、没有判别力。

为什么需要（methodology-audit-4 报告 C4 的第二条，2026-09-08 量的）：
`a.startswith(b)` 直接给 0.95，不看两边差多少。**在 2 fps 采样下，
打字机一帧能长出十几个字，所以低长度比本身是正常的**——
审计说的"1 个字不该算前缀"里，"低比例"那半是错的：

    短  5 'すがのオレ'      -> 长 22 'すがのオレでも、人間とフェイの関係がさらに悪'
    短 10 '今令尹から聞いたわ。' -> 长 32 '今令尹から聞いたわ。あなたの言う通り…'

**但"短到 1–2 字"那半是对的**。把"只有靠这条分支才配得上"（SequenceMatcher
< sim_thr）的那些匹配按短边字数拆开：

| 素材 | 决定性前缀匹配 | 短边 1–2 字 |
| --- | ---: | ---: |
| gi-s2 | 80 | 33 = **41%** |
| wuwa-s1 | 270 | 151 = **56%** |
| hsr-s2 | 381 | 224 = **59%** |

而 1–2 字那批的样例是 `1` / `0` / `6` / `が死` / `任務`——数字和碎片，
`1` 就这么配上了 `1-ザ-ID:712688376`。≥3 字那批则全是真的打字机增长。
**这条线把两类干净地分开了。**
"""


@functools.lru_cache(maxsize=1 << 18)   # 约 26 万对，内存几十 MB；重复是时间上局部的，LRU 留得住
def _ratio(a: str, b: str) -> float:
    """`SequenceMatcher(None, a, b).ratio()`，按 (a, b) 缓存——**纯函数**，同一段 UI / 字幕逐帧重复出现、
    整场 gi1 上 build_runs 调它 2.7M 次、difflib 占了建轨的 1/3（2026-09-24 profile）。⚠ 参数顺序别换：ratio 不对称。"""
    return SequenceMatcher(None, a, b).ratio()


def text_sim(a: str, b: str, prefix_min: int = 0) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if ((a.startswith(b) or b.startswith(a))    # 打字机：逐字长出来
            and min(len(a), len(b)) >= prefix_min):
        return 0.95
    return _ratio(a, b)


def text_sim_at_least(a: str, b: str, thr: float, prefix_min: int = 0) -> bool:
    """`text_sim(a, b, prefix_min) >= thr` 的**逐位等价**快路：相等 / 前缀两条照 `text_sim` 先判（1.0 / 0.95），
    其余先过长度上界，够不着就不算 ratio。上界写成和 difflib 算 ratio **同一个浮点表达式**（`2.0 * M / (la + lb)`，
    匹配数 M ≤ min(la, lb)、浮点除法对分子单调），所以"上界 < thr ⇒ ratio < thr"不用绕有理数逼近。守卫拿原定义对拍。"""
    if not a or not b:
        return 0.0 >= thr
    if a == b:
        return 1.0 >= thr
    if (a.startswith(b) or b.startswith(a)) and min(len(a), len(b)) >= prefix_min:
        return 0.95 >= thr
    la, lb = len(a), len(b)
    if 2.0 * min(la, lb) / (la + lb) < thr:
        return False
    return _ratio(a, b) >= thr


def best_overlap(head: str, tail: str, min_k: int = 4, thr: float = 0.85) -> int:
    """head 的后缀与 tail 的前缀重叠多少个字符（允许 OCR 误差）。

    **不能"从长到短、命中即返回"**：新闻 ticker 里 `万盎司`、`了19` 这种重复子串会让
    某个错误的 k 先过阈值，接出 `6245万盎司月6万盎司增加了19` 这种断头话。
    改成在所有过阈值的 k 里取 `ratio × k` 最大的——既要匹配得好，也要重叠得长。

    返回 0 表示没有可信重叠，**调用方必须放弃拼接**，否则 OCR 抖动（`ABC` / `ABD`）
    会被首尾相接成 `ABCABD`。
    """
    best_k, best_score = 0, 0.0
    # 快路（2026-09-24，逐位等价）：两段都是 k 个字时 ratio 的上界是 quick_ratio = 字符多重集交集 / k。
    # 交集随 k 增量维护（head 的后缀在左边多一个字、tail 的前缀在右边多一个字），上界够不着 thr 的 k 不建 SequenceMatcher。
    # 整场 zzz 上这个函数原来 53 s（滚动字幕的拼接）
    n = min(len(head), len(tail))
    ca: dict[str, int] = {}
    cb: dict[str, int] = {}
    inter = 0
    for k in range(1, n + 1):
        x, y = head[-k], tail[k - 1]
        if ca.get(x, 0) < cb.get(x, 0):
            inter += 1
        ca[x] = ca.get(x, 0) + 1
        if cb.get(y, 0) < ca.get(y, 0):
            inter += 1
        cb[y] = cb.get(y, 0) + 1
        if k < min_k or 2.0 * inter / (2 * k) < thr:  # 和 difflib quick_ratio 同一个浮点表达式
            continue
        ratio = _ratio(head[-k:], tail[:k])
        if ratio < thr:
            continue
        score = ratio * k
        if score > best_score:
            best_k, best_score = k, score
    return best_k


def stitch_window_texts(texts: list[str]) -> str:
    """把一串"同一句话的滑动窗口"拼回整句。

    滚动字幕（新闻 ticker、片尾）的完整句子从不在单帧里出现：文字从右边长出来、
    从左边滚出去，每一帧只看得到一个窗口。按时间顺序做后缀-前缀重叠拼接即可。

    静止文本与打字机是这件事的退化情形——前者所有窗口相同，后者互为前缀，
    拼出来就是最长的那个，所以这个函数可以无条件地替代"取最长"。
    """
    out = ""
    for t in texts:
        t = t.strip()
        if not t:
            continue
        if not out:
            out = t
            continue
        if t in out:                       # 完全被包含：没有新信息
            continue
        if out in t:                       # 打字机长出来的：直接换成更长的
            out = t
            continue
        k = best_overlap(out, t)
        # 滑动窗口相邻两帧本该重叠大半；重叠太少说明这一帧要么是抖动、
        # 要么中间断了片，硬接只会造出断头话。宁可丢这一帧。
        if k >= 0.4 * min(len(out), len(t)):
            out = out + t[k:]
    return out


def looks_like_sliding(texts: list[str]) -> bool:
    """判断这串观测是不是"滑动窗口"，而不是静止/打字机。

    判据：存在一对观测，彼此都不是对方的前缀（排除打字机），却有可观的首尾重叠。
    """
    uniq = list(dict.fromkeys(t.strip() for t in texts if t.strip()))
    for a, b in zip(uniq, uniq[1:]):
        if a.startswith(b) or b.startswith(a) or a in b or b in a:
            continue
        if best_overlap(a, b, min_k=4) >= 4:
            return True
    return False


class DisjointSet:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def groups(self) -> list[list[int]]:
        out: dict[int, list[int]] = {}
        for i in range(len(self.parent)):
            out.setdefault(self.find(i), []).append(i)
        return list(out.values())


# ---------- 数据结构 ----------

@dataclass
class Run:
    box: list[float]
    text: str
    t_start: int
    t_end: int
    n_obs: int
    conf: float
    texts: list[str] = field(default_factory=list)
    reused: list[bool] = field(default_factory=list)
    """`texts` 里每一票是不是**沿用**来的（run_ocr2 的 `reused` 字段）。

    为什么要记（methodology-audit-4 报告 C4）：框级复用把上一帧的文本原样搬过来，
    可下游投票**把它当成一次独立的读数**。实测沿用占全部观测的 32%–55%
    （wuwa-s1 / gi-s2），也就是**三分之一到一半的票不是独立的**——
    一次读错会被自己复制出压倒性多数。"""
    scrolling: bool = False
    moving: bool = False
    """几何上跳过位置（IoU 掉下阈值、又不是打字机在长）。和 `scrolling` 不是一回事：
    那个是**文本**像滑动窗口，这个是**框**在动，两者可以各自成立。"""
    confs: list[float] = field(default_factory=list)
    """逐票的置信度。`conf` 字段只留最大值，看不出**趋势**——而淡入淡出正是
    "读数随时间变"（text-motion 计划的 `fade` 原语）。和 `texts`/`reused` 同长。"""
    boxes: list[tuple[int, list[float]]] = field(default_factory=list)
    """位置历史 `[(t_us, box)]`。**只有动过的才留全**（owner 2026-09-09：
    `boxes[]` 只给 moving 的事件落盘），没动过的就只有首帧那一条——
    122k 条 run 每条都留全帧位置，内存和产物都不必要地大。"""
    times: list[int] = field(default_factory=list)
    """逐票的观测时刻，和 `texts` 同长。只在内存里用（`sampled_full`），不落盘。"""
    clip_start: int | None = None
    clip_end: int | None = None
    """被**自选范围的时间关闭段**截断的那一端（µs，切点时刻；ocr-regions 计划）。起点：run 的首票前一个采样间隔里
    有切点切到这个框（"范围打开的那一刻起才看得见"，不是"文字在那时出现"）；终点：末票后一个采样间隔里有切点切到它，
    t_end 截到切点。被截的那一端回抠不去找自然出现 / 消失（refine_boundaries.apply）。"""

    @property
    def cx(self) -> float:
        return (self.box[0] + self.box[2]) / 2

    @property
    def cy(self) -> float:
        return (self.box[1] + self.box[3]) / 2

    @property
    def h(self) -> float:
        return self.box[3] - self.box[1]

    @property
    def w(self) -> float:
        return self.box[2] - self.box[0]


# ---------- 阶段一：时间合并 ----------

def estimate_shift(prev: list[dict], cur: list[dict]) -> tuple[float, float]:
    """估计相邻两个采样帧之间**滚动文字**的整体位移。

    做法：把两帧里文本几乎相同的框两两配对，收集位移，按 12 px 的网格投票。
    静态文字会在 (0,0) 堆出一座大山，所以这里**只找非零的那个众数**——
    静止的情况由调用方单独试 (0,0)，两者取匹配更好的一个。这样同一帧里
    "ticker 在滚 + 其它都不动" 也能各自匹配上。
    """
    votes: Counter[tuple[int, int]] = Counter()
    for a in prev:
        for b in cur:
            if not text_sim_at_least(a["text"], b["text"], 0.9):
                continue
            dx = (b["box"][0] + b["box"][2] - a["box"][0] - a["box"][2]) / 2
            dy = (b["box"][1] + b["box"][3] - a["box"][1] - a["box"][3]) / 2
            if abs(dx) < 4 and abs(dy) < 4:
                continue                                   # 没动，不参与投票
            votes[(round(dx / 12), round(dy / 12))] += 1
    if not votes:
        return (0.0, 0.0)
    top = votes.most_common(2)
    (gx, gy), n = top[0]
    second = top[1][1] if len(top) > 1 else 0
    # **光有 3 票不够，还得赢得干净。** yuka input4 上屏幕里有一排重复的短 UI 文字，
    # 相邻帧的"第 k 个"和"第 k+1 个"互相配对，在网格间距上（288=24×12、408=34×12）
    # 轻松凑够 3 票，下一帧符号还反过来——那不是滚动，是噪声。3000 个采样时刻能累到
    # (-30204, -19584) px。真在滚的东西只有一个众数；重复 UI 的票会散在几个间距上。
    #
    # **这条余量我撤过一次又装回来了，因为第一次的账没算全。**
    # 撤的理由是它没修好 input4 的 run 断裂（真凶是 gap_frames，见 build_runs），
    # 而且 f2/f3 上有代价。但当时**还没测不带余量的 f4**。五部测全之后（剧本命中）：
    #   f1 5814 -> 5813 (-1)   f2 5058 -> 5172 (+114)   f3 4911 -> 4934 (+23)
    #   f4 3908 -> **3250 (-658)**   f5 2795 -> 2793 (-2)
    # 跨片可比的口径上，f4 从 +1.0% 掉到 **-16.0%**（主轨 4,387 -> 3,633 条、
    # 区域里的 track 从 1,234 掉到 1,047）。一部的塌方远大于两部的小赚，装回来。
    if n < 3 or n < 2 * second:
        return (0.0, 0.0)
    return (gx * 12.0, gy * 12.0)


TYPEWRITER_MIN_CHARS = 2
"""打字机增长匹配要求上一帧至少打出了几个字。**默认 2 是旧行为**；`--typewriter-min-chars 1` 是对照臂。

1 字的首帧过不了这道门：hsr 5591 s 那一句首帧只采到 `知`（0.5 s），整句 `知ってる、捕まえたのは僕だ。`
从下一帧才开始，两者成了两条 run，`知` 于是单独成了一条 cue（owner 2026-09-12 看预览时指出）。
放到 1 字时**前缀必须逐字相同**（1 个字没有"允许 OCR 噪声"的余地），几何判据照旧。"""


GROW_H_RATIO = 1.7
"""打字机生长的框高比门（`--grow-h-ratio`，0 = 不设门）：上一帧和这一帧的框高相差超过这个倍数就不算"长出来的"。
**2026-09-24 owner 定从 1.4 放到 1.7**（defaults §1.22）：yuka-f1 遮罩臂上整句的框被 det 撑到 53 px、半截是 35 px（1.51 倍），
1.4 让链断、首字晚 41 帧（ocr-regions 计划末尾）；1.7 在六份打字机真值上逐项不变、11 段用途 2 覆盖 ±0 / 重复认领 −1、
碎片少，再放到 2.0 或不设门重复认领反而涨（h-ratio 计划）。仍是手设常数，同字高比一类（owner：保持警惕、防过拟合）。"""


def typewriter_growth(prev_box: list[float], prev_text: str,
                      cur_box: list[float], cur_text: str,
                      min_chars: int = TYPEWRITER_MIN_CHARS, h_ratio: float = GROW_H_RATIO) -> bool:
    """当前这个框，是不是上一帧那个框"多打出来几个字"的样子？

    打字机的中间态过不了常规匹配：`[クり` 和 `[クリス] 乾燥させると…` 的文本相似度低，
    框也从窄变宽、IoU 掉到阈值以下，于是被当成两条独立的 run，
    在 SRT 里变成一条半截字幕 + 一条完整字幕。整片实测这是"多出条目"的主要来源之一。

    判据两条，都不含时长/速度常数（owner：ms/char 不许钉死）：
      1. 短的那段文本是长的那段的**前缀**（允许 OCR 噪声；只有 1 个字时逐字相同）
      2. 两个框在**同一行**上，且是从同一侧长出去的

    前缀比较之前两边先过 `textfold.fold`（NFKC + 小书写假名折大），字数的两道门仍按原文数：
    zzz-s1 245.8 s 半截读成 `ビリー、行くよ!えっと…`、整句读成 `ビリー、行くょ！えつと…`，逐字比只有 0.75（`!`/`！`、`っ`/`つ`、`よ`/`ょ`
    三处都是同一个字的读法差），链断成两条、整句那条没有段首证据，首字晚 40 帧（2026-10-05，followups 的 zzz-s1 那条）。
    """
    a, b = "".join(prev_text.split()), "".join(cur_text.split())
    if len(a) < max(1, min_chars) or len(b) <= len(a):
        return False
    # 几何的几条先判、文本相似度最后算（全是"且"，先后不改结果；2026-09-24：整场 zzz 上这个函数 540 万次、70 s，大半花在 difflib 上）
    ah, bh = prev_box[3] - prev_box[1], cur_box[3] - cur_box[1]
    if h_ratio and max(ah, bh) / max(1.0, min(ah, bh)) > h_ratio:
        return False
    lo, hi = max(prev_box[1], cur_box[1]), min(prev_box[3], cur_box[3])
    if hi - lo < 0.6 * min(ah, bh):                       # 不在同一行
        return False
    tol = 0.6 * min(ah, bh)
    grew_right = abs(cur_box[0] - prev_box[0]) <= tol and cur_box[2] > prev_box[2]
    grew_both = (cur_box[0] <= prev_box[0] + tol and cur_box[2] >= prev_box[2] - tol
                 and abs((cur_box[0] + cur_box[2]) - (prev_box[0] + prev_box[2])) <= 2 * tol)
    if not (grew_right or grew_both):
        return False
    fa, fb = textfold.fold(a), textfold.fold(b)
    if len(a) < 2:
        return fb.startswith(fa)
    return _ratio(fa, fb[:len(fa)]) >= 0.8


def _joined(pieces: list[dict]) -> str:
    """几块按从左到右拼成一行。两块之间的水平间隙 ≥ 0.3 个行高才隔一个空格（原文那里有空），否则直接拼——
    按几何判，不按字符类别：`HP` | `100` 紧挨着切开的不该多出空格（peer 审查 6b47a0c 第 3 条）。"""
    out, prev = "", None
    for p in pieces:
        if prev is not None:
            h = min(prev["box"][3] - prev["box"][1], p["box"][3] - p["box"][1])
            if p["box"][0] - prev["box"][2] >= 0.3 * h:
                out += " "
        out += p["text"]
        prev = p
    return out


def rejoin_split_lines(obs_t: list[dict], open_runs: list[Run], iou_thr: float, sim_thr: float) -> list[dict]:
    """det 在某个采样点上把**一条在场 run 的那一行**切成几块时，把这几块拼回一个观测（followups「det 切行的碎片另起事件」）。

    不拼的话，切出的块各自另起 run：叠加里重影、骗出假打字机（`今令尹から聞いたわ。` 接着长回整句，被画成打了 1 s）、
    段落模式的主轨 SRT 里同一行出现两遍（wuwa-s1 31.5 s、125.7 s）。通用判据，不看素材：
      1. run 是静止的（没动过、不是滚动文本）；
      2. 块都在这一行上（和 run 的框竖直重叠 ≥ 0.6 个较小行高、横向落在它的框里，两侧各留 0.6 个行高），至少两块，彼此横向不重叠过半；
      3. 从左到右拼起来的文本和 run 的文本过 `sim_thr`，块的外接框和 run 的框过 `iou_thr`——两道门和普通匹配是同一对；
      4. 拼起来的得分（IoU + 文本相似，同普通匹配）高于其中任何单独一块。
    拼成的观测：框取外接、文本取拼接、置信度取最低、`reused` 只在每块都是沿用时才算沿用；`rejoined` 记块数（只在内存里）。
    每块只拼一次；run 按观测数从多到少先挑。"""
    if len(obs_t) < 2 or not open_runs:
        return obs_t
    taken: set[int] = set()
    merged: list[dict] = []
    for r in sorted(open_runs, key=lambda r: -r.n_obs):
        if r.moving or r.scrolling:
            continue
        rb, rh = r.box, r.h
        tol = 0.6 * rh
        pieces = []
        for k, o in enumerate(obs_t):
            if k in taken:
                continue
            b = o["box"]
            bh = b[3] - b[1]
            if min(rb[3], b[3]) - max(rb[1], b[1]) < 0.6 * min(rh, bh):
                continue
            if b[0] < rb[0] - tol or b[2] > rb[2] + tol:
                continue
            pieces.append(k)
        if len(pieces) < 2:
            continue
        pieces.sort(key=lambda k: obs_t[k]["box"][0])
        ps = [obs_t[k] for k in pieces]
        if any(x_overlap_ratio(a["box"], b["box"]) > 0.5 for a, b in zip(ps, ps[1:])):
            continue
        box = [min(p["box"][0] for p in ps), min(p["box"][1] for p in ps),
               max(p["box"][2] for p in ps), max(p["box"][3] for p in ps)]
        text = _joined(ps)
        geo, sim = iou(box, rb), text_sim(text, r.text)
        if geo < iou_thr or sim < sim_thr:
            continue
        # 切出的长块单独也可能过门（wuwa-s1 31.5 s：右边 23 字那块和整行 IoU 0.48 > 0.35），照常匹配就会接上它、左边那块另起 run。
        # 所以不是"有单块能接上就不拼"，而是拼起来的比任何单块都更像这一行才拼（得分同普通匹配：IoU + 文本相似）
        if geo + sim <= max(iou(p["box"], rb) + text_sim(p["text"], r.text) for p in ps):
            continue
        taken.update(pieces)
        merged.append({**max(ps, key=lambda p: p["box"][2] - p["box"][0]), "box": box, "text": text,
                       "conf": min(p["conf"] for p in ps), "reused": all(bool(p.get("reused")) for p in ps),
                       "rejoined": len(ps)})
    if not taken:
        return obs_t
    return [o for k, o in enumerate(obs_t) if k not in taken] + merged


def build_runs(obs: list[dict], frame_us: int, iou_thr: float, sim_thr: float,
               gap_frames: int, grow_typewriter: bool = True,
               text_pick: str = "vote", tail_max: int = 3,
               vote_independent: bool = False, prefix_min: int = 0,
               typewriter_min_chars: int = TYPEWRITER_MIN_CHARS,
               cuts: list | tuple = (), grow_h_ratio: float = GROW_H_RATIO,
               rejoin_split: bool = True) -> list[Run]:
    """`cuts`：自选范围的时间切点 `[(t_us, 那一刻遮罩变了的像素 H×W bool)]`（`regions.cuts_us`）。
    切到某个 run 的框的切点，run **不许跨过它**延续；落在首票前 / 末票后一个采样间隔里的记成 `clip_start` / `clip_end`。
    `rejoin_split`：det 把在场的一行切成几块时拼回去再匹配（`rejoin_split_lines`；`--no-rejoin-split` 是对照臂）。"""

    def cut_hit(box, lo: int, hi: int) -> int | None:
        """(lo, hi] 里第一个切到这个框的切点。"""
        for tc, area in cuts:
            if lo < tc <= hi:
                x0, y0 = max(0, int(box[0])), max(0, int(box[1]))
                if area[y0:max(y0, int(box[3])), x0:max(x0, int(box[2]))].any():
                    return tc
        return None

    by_t: dict[int, list[dict]] = {}
    for o in obs:
        by_t.setdefault(o["t_us"], []).append(o)
    times = sorted(by_t)

    # 每个采样间隔的滚动位移，累加成"从第一帧起的累计位移"，
    # 这样跨了几帧的 run 也能算出该往哪挪。
    cum: dict[int, tuple[float, float]] = {times[0]: (0.0, 0.0)} if times else {}
    for prev_t, t in zip(times, times[1:]):
        dx, dy = estimate_shift(by_t[prev_t], by_t[t])
        px, py = cum[prev_t]
        cum[t] = (px + dx, py + dy)

    open_runs: list[Run] = []
    closed: list[Run] = []
    for t in times:
        # **半帧的余量不是可有可无的。** `frame_us = round(1e6/sample_fps)`，
        # 而实际采样间隔会在它上下抖 1 µs（源视频 fps 不是整数时尤其）。
        # 没有余量时，只要 round 向下落了一点点，**每隔一两帧就会有一个间隔比
        # tol 大 1 µs**，run 当场收摊：
        #   input4  sample_fps 1.99957 -> frame_us 500,108；实测间隔 500,108/500,109
        #           **38% 的间隔超出 1 µs** -> obs/run 只有 2.1，主轨 20,108 条里
        #           64% 是上一条的逐字重复
        #   input3  sample_fps 1.99927 -> frame_us 500,182；实测 500,182/500,181
        #           round 恰好落在上面，一次都没超 -> 完全正常（重复率 2%）
        # 半帧余量不会放宽语义：真丢一帧是 2 个间隔，仍然超出。
        tol = int((gap_frames + 0.5) * frame_us)
        still: list[Run] = []
        for r in open_runs:                                   # 断太久的收摊；被时间关闭段切到的也收摊
            (still if t - r.t_end <= tol and not (cuts and cut_hit(r.box, r.t_end, t) is not None)
             else closed).append(r)
        open_runs = still
        obs_t = rejoin_split_lines(by_t[t], open_runs, iou_thr, sim_thr) if rejoin_split else by_t[t]

        used: set[int] = set()
        for o in sorted(obs_t, key=lambda x: -x["conf"]):
            best_i, best_score = -1, 0.0
            for i, r in enumerate(open_runs):
                if i in used:
                    continue
                sim = text_sim(o["text"], r.text, prefix_min)
                # 打字机中间态要单独判：它**既可能**文本对不上（`[クり` 对整句），
                # **也可能**文本对得上但框对不上——`ひょ` 长成 `ひょこひょこしてる〜`
                # 之后框宽了 4.8 倍，IoU 只有 0.19，照样被几何判据挡掉。
                grow = grow_typewriter and typewriter_growth(r.box, r.text,
                                                             o["box"], o["text"],
                                                             typewriter_min_chars, grow_h_ratio)
                if sim < sim_thr and not grow:
                    continue
                # 静止假设与滚动假设各试一次，取好的那个：
                # 同一帧里既有不动的字幕、又有滚动的 ticker 时，两者都能匹配上。
                dx = cum[t][0] - cum[r.t_end][0]
                dy = cum[t][1] - cum[r.t_end][1]
                geo = iou(o["box"], r.box)
                if dx or dy:
                    moved = [r.box[0] + dx, r.box[1] + dy, r.box[2] + dx, r.box[3] + dy]
                    geo = max(geo, iou(o["box"], moved))
                if geo < iou_thr and not grow:
                    continue
                # 打字机中间态的 sim 会很低（`[クり` 对整句），靠 grow 把它抬到 1.0，
                # 否则一个几何上更巧合的候选就把它抢走了。
                # **比较用的键和存下来的必须是同一个**——原来比较用加权值、存的是
                # `geo + sim`，后来的普通候选拿 1.05 去比 0.49，boost 等于没有。
                key = geo + max(sim, 1.0 if grow else 0.0)
                if key > best_score:
                    best_i, best_score = i, key
            if best_i >= 0:
                r = open_runs[best_i]
                used.add(best_i)
                moving = (iou(o["box"], r.box) < iou_thr
                          and not typewriter_growth(r.box, r.text, o["box"], o["text"],
                                                    typewriter_min_chars, grow_h_ratio))
                prev_t = r.t_end                              # 上一次观测的时刻（轨迹要用）
                r.t_end = t
                r.n_obs += 1
                r.texts.append(o["text"])
                r.times.append(t)
                r.reused.append(bool(o.get("reused")))
                if len(o["text"]) > len(r.text):               # 打字机：留最长的那一版
                    r.text = o["text"]
                if moving:
                    # 轨迹：只有动过的才记。**位移之前要先把静止段收尾**——
                    # 不然一条"先站着不动、1.5 秒后才上移"的字，轨迹里只有首帧和
                    # 移动后两个点，导出成 `\move` 就是**从头开始匀速滑**，
                    # 把停顿抹掉了（运动形态审计正是要看停顿）。
                    # 只在**发生位移时**补一个点，代价是 O(位移次数)，不是 O(观测数)。
                    if r.boxes and r.boxes[-1][0] != prev_t:
                        r.boxes.append((prev_t, list(r.box)))
                    r.moving = True
                    r.boxes.append((t, list(o["box"])))
                    r.box = list(o["box"])                    # 跟着文字走，别把整条滚动轨迹并成一个大框
                else:
                    r.box = [min(r.box[0], o["box"][0]), min(r.box[1], o["box"][1]),
                             max(r.box[2], o["box"][2]), max(r.box[3], o["box"][3])]
                r.conf = max(r.conf, o["conf"])
                r.confs.append(float(o["conf"]))
            else:
                # **新建的 run 也要占位**（methodology-audit-4 报告 C4）：
                # 不占位的话，**同一帧**里后面那个重叠/相似的框还能匹配到它，
                # `n_obs += 1`——于是"一个时刻的两个框"被记成"两帧都看到过"。
                # 下游拿 `n_obs` 当时间支持度（还用它推代表观测时刻去取模板），
                # 会去一个**根本没观测过的帧**上抠图。
                # 同帧去重是另一件事（该不该合），这里只保证**时间支持度不虚增**。
                open_runs.append(Run(box=list(o["box"]), text=o["text"], t_start=t, t_end=t,
                                     n_obs=1, conf=o["conf"], texts=[o["text"]], times=[t],
                                     reused=[bool(o.get("reused"))],
                                     confs=[float(o["conf"])],
                                     boxes=[(t, list(o["box"]))],
                                     clip_start=cut_hit(o["box"], t - frame_us, t) if cuts else None))
                used.add(len(open_runs) - 1)
    closed.extend(open_runs)
    for r in closed:
        # 最后一段静止也要收尾（同上：末次位移之后它还在屏幕上待了一会儿）
        if r.moving and r.boxes and r.boxes[-1][0] != r.t_end:
            r.boxes.append((r.t_end, list(r.box)))
        tc = cut_hit(r.box, r.t_end, r.t_end + frame_us) if cuts else None
        r.t_end += frame_us                                   # 末帧也占满一个采样间隔
        if tc is not None:                                    # 自选范围在这一个间隔里关掉了：截到切点
            r.t_end, r.clip_end = tc, tc
        if looks_like_sliding(r.texts):
            # 滚动文本：整句从不在单帧出现，得按重叠拼回来
            stitched = stitch_window_texts(r.texts)
            if len(stitched) > len(r.text):
                r.text = stitched
                r.scrolling = True
                continue
        # 一条 run 最终用哪一版文本。**这是个有取舍的选择，不是显然的**：
        #   vote     多数投票，能挡掉抖动帧的垃圾尾巴（`本当に帰る` 8 次 vs `本当に帰る火` 1 次）
        #   longest  取最长，能保住打字机打完的那一版（`ナノカの【幻視】……)` 只出现 2 次，
        #            却是对的），但会把上面那个 `火` 一起收进来
        #   tail     折中：多数投票，但若最长版是多数版的延长且只多几个字，取最长
        # 实测（yuka 窗口）见 script-corpus 报告，别凭直觉选。
        # **投票只数独立的读数**（`--vote-reused` 可以退回旧行为）：
        # 框级复用把上一帧的文本原样搬过来，旧代码把它当成一次独立的票，
        # 于是一次读错会被自己复制出压倒性多数（沿用占 32%–55%，audit-4 C4）。
        # 一条 run 若**全部**是沿用的（首帧那一票之外再没重识别过），
        # 就没有独立票可数，只好退回全体——**不能因此把它清空**。
        votes = ([(t, c) for t, c, ru in zip(r.texts, r.confs, r.reused) if not ru] if vote_independent
                 else list(zip(r.texts, r.confs))) or list(zip(r.texts, r.confs))
        pool = [t for t, _ in votes]
        common = Counter(pool).most_common(1)[0][0]
        longest = max(pool, key=len)
        if text_pick in ("conf", "maxconf"):
            # conf    每一票按 rec 置信度加权（Subtitle Edit 的"时长 × conf"：我们每票时长都是一个采样间隔，退化成 conf 加权）
            # maxconf 取置信度最高的那一票（videocr-PaddleOCR 的做法）
            # 同样守着"不比最长版短太多"那道门，和 vote 可比（综合清单 M3，try-list-results 报告）
            if text_pick == "conf":
                w: Counter = Counter()
                for t, c in votes:
                    w[t] += c
                pick = max(w, key=lambda t: (w[t], -pool.index(t)))
            else:
                pick = max(votes, key=lambda tc: tc[1])[0]
            if len(pick) >= len(r.text) * 0.8:
                r.text = pick
        elif text_pick == "longest":
            r.text = longest
        elif text_pick == "tail" and longest.startswith(common) and                 0 < len(longest) - len(common) <= tail_max:
            r.text = longest
        elif len(common) >= len(r.text) * 0.8:
            r.text = common
    closed.sort(key=lambda r: (r.t_start, r.cy, r.cx))
    return closed


# ---------- 阶段二/三：空间聚类 ----------

def build_tracks(runs: list[Run], W: int, H: int) -> list[list[int]]:
    """行级聚类：按"到轨道质心的距离"分配，而不是并查集。

    并查集在这里会**串联**——滚动的评论墙里每条 run 都和上下相邻的那条挨得够近，
    一路 union 下去就把整面墙合成一条"行"（实测 ja-title 出现过 y 跨度 0.29H 的单轨）。
    质心法不会传染：新 run 只跟轨道当前的中位位置比。
    """
    order = sorted(range(len(runs)), key=lambda i: (runs[i].cy, runs[i].cx))
    tracks: list[list[int]] = []
    cent: list[tuple[float, float, float]] = []      # cy, cx, h 的中位数
    for i in order:
        r = runs[i]
        best, best_d = -1, None
        for k, (cy, cx, h) in enumerate(cent):
            if max(r.h, h) / max(1.0, min(r.h, h)) > 1.6:
                continue
            dy = abs(r.cy - cy)
            if dy > max(0.4 * min(r.h, h), 0.010 * H):
                continue
            members = [runs[j] for j in tracks[k]]
            if (max(x_overlap_ratio(r.box, m.box) for m in members) < 0.30
                    and abs(r.cx - cx) > 0.05 * W):
                continue
            if best_d is None or dy < best_d:
                best, best_d = k, dy
        if best < 0:
            tracks.append([i])
            cent.append((r.cy, r.cx, r.h))
        else:
            tracks[best].append(i)
            ms = [runs[j] for j in tracks[best]]
            cent[best] = (statistics.median(m.cy for m in ms),
                          statistics.median(m.cx for m in ms),
                          statistics.median(m.h for m in ms))
    return tracks


def cooccur(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> bool:
    """两串已合并、已排序的时段有没有交集（= 同屏过）。"""
    i = j = 0
    while i < len(a) and j < len(b):
        if a[i][0] <= b[j][1] and b[j][0] <= a[i][1]:
            return True
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return False


def alternations(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> int:
    """两条轨的时段按起点并成一串之后，在 A / B 之间切换了几次。

    "你一条我一条"（同一块里的几行，行数随台词变，未必同屏）切换很多次；
    "你一段我一段"（阅读面板念一阵、字幕带播一阵）只切换一两次。"""
    i = j = n = 0
    last = ""
    while i < len(a) or j < len(b):
        take_a = j >= len(b) or (i < len(a) and a[i][0] <= b[j][0])
        src = "a" if take_a else "b"
        if src != last:
            n += 1
            last = src
        i += take_a
        j += not take_a
    return max(0, n - 1)


REGION_H_RATIO = 1.7
"""窗内并区的字高比门（`--region-h-ratio`，0 = 不设门）：两条行级轨的中位字高相差超过这个倍数就不连边。
第一版（2026-09-04）手设的常数，没扫过、没标定（2026-09-24 查的来历）；h-ratio 计划重新量：在平台上、维持。
⚠ owner："保持警惕、很容易过拟合"——素材间方向相反；某款游戏的版式吃它，**做成按游戏的补丁**（matchers.gametext.PATCHES），别动这里。"""


def build_regions(runs: list[Run], tracks: list[list[int]], W: int, H: int,
                  mutual_x: bool = False, time_gate: int = 0,
                  h_ratio: float = REGION_H_RATIO) -> list[list[int]]:
    """把行级 track 再并成块：x 方向重叠、字号相近（`h_ratio`，0 = 不看字号）、y 相邻。

    `time_gate > 0` 再加一道**时间**门（默认 0 = 关）：两条轨要么同屏过，要么时段交错
    ≥`time_gate` 次，才连边。它针对的是窗内并查集把**从不同屏的两块**串成一块
    （星铁的遗器阅读面板和字幕带，game-text-corpus 报告）。
    量过的账在 game-text-corpus 报告里：星铁整场 +34（cue、对不上剧本、重复认领三项同时变好），绝区零 +1，
    六段切片 ±0，**但原神 −49 / −58 / −30**——不是门判错，是**主轨只能挑一个区域**接不住
    被正确分开的几条带（product-goals 第 15 条）。所以默认关，按游戏显式开
    （第 14 条；星铁的证据是一场整片 + 两段切片）。"""
    def stat(tr: list[int]) -> tuple[float, float, float, float, float]:
        rs = [runs[i] for i in tr]
        return (statistics.median(r.box[0] for r in rs), statistics.median(r.box[2] for r in rs),
                statistics.median(r.cy for r in rs), statistics.median(r.h for r in rs),
                statistics.median(r.cx for r in rs))

    st = [stat(t) for t in tracks]
    iv = ([uigate.merge_intervals(sorted((runs[i].t_start, runs[i].t_end) for i in t)) for t in tracks]
          if time_gate else None)
    ds = DisjointSet(len(tracks))
    for i in range(len(tracks)):
        for j in range(i + 1, len(tracks)):
            (ax0, ax1, acy, ah, acx), (bx0, bx1, bcy, bh, bcx) = st[i], st[j]
            if h_ratio and max(ah, bh) / max(1.0, min(ah, bh)) > h_ratio:
                continue
            if (x_overlap_ratio([ax0, 0, ax1, 1], [bx0, 0, bx1, 1], mutual_x) < 0.45
                    and abs(acx - bcx) > 0.05 * W):
                continue
            # 邻接按**较小**的那个行高判，不能按较大的：否则一个大字号的框
            # （zh-news 里有 h=173px 的画中画标题）能允许 Δcy 到 0.35H，
            # 并查集顺着它把整屏从 cy 0.0 串到 0.9，串成一个假的"字幕区"。
            if abs(acy - bcy) > 2.2 * min(ah, bh):
                continue
            if iv is not None and not (cooccur(iv[i], iv[j])
                                       or alternations(iv[i], iv[j]) >= time_gate):
                continue
            ds.union(i, j)
    return ds.groups()


# ---------- 阶段三之二：按时间分段聚类 ----------
#
# 为什么需要（full-film-run 报告）：区域聚类原本是**纯空间**的。
# 60 秒片段上很漂亮，3h20m 整片上退化成 44 个区域、其中一个塞了 278 条轨、
# y 跨度 0.96——因为 200 分钟里文字几乎在每个位置都出现过，轨与轨全部连通。
#
# 修法不是把空间判据调严（那会在短片上误分），而是**别把整片的 run 汇到一起聚**。
# 分两步：
#   1. 在时间窗内聚类（窗内版式基本不变，等价于我们已经验证过的 60 秒场景）
#   2. 跨窗把位置一致的区域缝成"版式槽位"（slot）
#
# 缝合顺带给出一个**免费且很有用的统计量**：槽位在多少个窗里出现过。
# 主字幕带会在几乎每个窗里都出现；片头标题只出现在一两个窗里。
# 这比 label_region 里那堆单帧几何阈值可靠得多——它是全片统计。


SAFE_RUNS_PER_WINDOW = 1000
"""窗内 run 数的经验安全线。见 clustering 报告的窗长扫描：
1200s 窗（每窗 ~860 条）仍正常，2400s 窗（每窗 ~1725 条）退化。取中间的整数。"""


def window_of(r: Run, window_us: int) -> int:
    """run 归哪个窗：按**中点**，跨窗的 run 不会被切成两半。"""
    return ((r.t_start + r.t_end) // 2) // window_us


def cluster_windowed(runs: list[Run], W: int, H: int, window_us: int,
                     mutual_x: bool = False, time_gate: int = 0,
                     h_ratio: float = REGION_H_RATIO) -> tuple[list[list[int]], list[dict]]:
    """窗内聚类。返回 (全局 tracks, 窗内区域列表)。

    tracks 用全局 run 下标，方便下游沿用；窗内区域记 {win, tracks, runs}。
    """
    by_win: dict[int, list[int]] = {}
    for i, r in enumerate(runs):
        by_win.setdefault(window_of(r, window_us), []).append(i)

    all_tracks: list[list[int]] = []
    win_regions: list[dict] = []
    for win in sorted(by_win):
        idx = by_win[win]
        sub = [runs[i] for i in idx]
        tr = build_tracks(sub, W, H)
        rg = build_regions(sub, tr, W, H, mutual_x, time_gate, h_ratio)
        base = len(all_tracks)
        all_tracks.extend([[idx[j] for j in t] for t in tr])
        for group in rg:
            win_regions.append({"win": win,
                                "tracks": [base + t for t in group],
                                "runs": [idx[j] for t in group for j in tr[t]]})
    return all_tracks, win_regions


def pct(vals: list[float], q: float) -> float:
    v = sorted(vals)
    return v[min(len(v) - 1, max(0, int(round(q * (len(v) - 1)))))] if v else 0.0


def region_geom(runs: list[Run], run_ids: list[int]) -> tuple[float, float, float, float, float, float]:
    """区域的几何签名：(cx, x0, x1, y0, y1, h)。

    **端点用 2%/98% 分位，不用 min/max。** 这是 predet 里踩过的同一个坑
    （predet 报告）：min/max 会被离群框撑大，成员越多包围盒越大，
    最后一个槽位能吞掉整屏。分位数让签名随样本收敛而不是发散。
    """
    rs = [runs[i] for i in run_ids]
    return (statistics.median(r.cx for r in rs),
            pct([r.box[0] for r in rs], 0.02), pct([r.box[2] for r in rs], 0.98),
            pct([r.box[1] for r in rs], 0.02), pct([r.box[3] for r in rs], 0.98),
            statistics.median(r.h for r in rs))


def span_overlap(a0: float, a1: float, b0: float, b1: float, floor: float) -> float:
    """两个区间的重叠，除以**较短**那个的长度。长度给一个下限，
    否则单行区域（span≈0）会退化成除以 0 或动辄 1.0。"""
    ov = min(a1, b1) - max(a0, b0)
    if ov <= 0:
        return 0.0
    return ov / max(floor, min(a1 - a0, b1 - b0))


SLOT_ANCHOR_H = 1.2
"""槽位与成员"共边"的容差，单位是行高。见下面 stitch_slots 的说明。"""

SLOT_SPAN_RATIO = 3.0
"""槽位与成员的 y 跨度最多差几倍（跨度先垫到 2 倍行高再比）。"""

SLOT_H_RATIO = 1.7
"""跨窗缝合的字高比门（`--slot-h-ratio`，0 = 不设门）：区域和槽位签名的字高相差超过这个倍数就不缝。
和 `REGION_H_RATIO` 同一个来历（手设、没标定），分开成两个旋钮是为了分层量。"""


def slot_fits(a: tuple[float, float, float, float, float, float],
              b: tuple[float, float, float, float, float, float],
              W: int, anchor: bool, mutual_x: bool = False,
              span_ratio: float = SLOT_SPAN_RATIO, h_ratio: float = SLOT_H_RATIO) -> float | None:
    """两个区域/槽位的几何签名能不能算同一个版式槽位。返回 y 重叠度，不匹配返回 None。

    签名是 region_geom 的 (cx, x0, x1, y0, y1, h)。判据见 stitch_slots 的说明。

    `mutual_x`（`--slot-mutual-x`，默认关）把 x 判据从"除以较窄的"换成"除以较宽的"，
    也就是 `build_regions` 的 `--mutual-x` 在**缝合层**的对应物。
    来历见 stitch_slots 的"大吃小还没治干净"那段。
    """
    return slot_fits_why(a, b, W, anchor, mutual_x, span_ratio, h_ratio)[0]


def slot_fits_why(a: tuple[float, float, float, float, float, float],
                  b: tuple[float, float, float, float, float, float],
                  W: int, anchor: bool, mutual_x: bool = False,
                  span_ratio: float = SLOT_SPAN_RATIO, h_ratio: float = SLOT_H_RATIO) -> tuple[float | None, str]:
    """`slot_fits` 的可解释版：返回 `(y 重叠度或 None, 是哪一条拦下的)`。

    `slot_fits` 就是它的第一个返回值——**只写一份判据**。
    存在的理由：整片上 44–45% 的槽位成员不再满足最终签名
    （gamestream-first-look 报告），"有多少"已经量出来了，
    "**卡在哪一条**"才是决定要不要允许拆分/重分配的设计输入。
    同 `nameplate.explain()` 的做法。
    """
    (cx, x0, x1, y0, y1, h), (scx, sx0, sx1, sy0, sy1, sh) = a, b
    if h_ratio and max(h, sh) / max(1.0, min(h, sh)) > h_ratio:
        return None, f"字高比>{h_ratio:g}"
    if (x_overlap_ratio([x0, 0, x1, 1], [sx0, 0, sx1, 1], mutual_x) < 0.30
            and abs(cx - scx) > 0.08 * W):
        return None, "x 不重叠且 cx 差远"
    yo = span_overlap(y0, y1, sy0, sy1, min(h, sh))
    if yo < 0.5:
        return None, "y 重叠<0.5"
    if anchor:
        if min(abs(y0 - sy0), abs(y1 - sy1)) > SLOT_ANCHOR_H * max(h, sh):
            return None, "上下边都没对齐"
        floor = 2.0 * min(h, sh)
        sa, sb = y1 - y0, sy1 - sy0
        if max(sa, sb) / max(floor, min(sa, sb)) > span_ratio:
            return None, "y 跨度比超限"
    return yo, ""


def slot_geom_median(runs: list[Run], win_regions: list[dict], members: list[int]):
    """槽位签名 = 各成员窗区域**各自几何的逐分量中位数**（`--slot-geom median`）。

    默认的 `pooled` 是把成员的 run 全倒进一个池子再取 2%/98% 分位，
    于是**一个巨大的成员会把签名撑开**：f5 的槽位吃进辩论屏之后，签名从
    名牌带的 `x=[214,761] y=[588,856]` 长成 `x=[90,1762] y=[157,865]`，
    之后任何窄带都"整个落在里面"。中位数对这种成员不敏感——
    这和 `region_geom` 用分位数而不是 min/max 是同一条理由，只是又往上了一层。

    **道理对，五部一跑还是否掉了**（名牌纯度：i1 1.5 / i2 0.9 / i3 87.0 / i4 37.4 /
    i5 76.3）。它和 `--slot-mutual-x` 坏在同样的两部——那两部的名牌槽位本来就是
    贪心顺序的运气，任何改签名的动作都会把分配重新洗一遍。留着这条路是为了
    别再走第二遍；要走通得先治"贪心依赖顺序"本身。
    """
    gs = [region_geom(runs, win_regions[w]["runs"]) for w in members]
    return tuple(statistics.median(g[k] for g in gs) for k in range(6))


def moving_share(runs: list[Run], ids: list[int]) -> float:
    """这一批 run 里**带 `move` 原语**的占比（text-motion 计划）。

    两个原语都算："框在动"（`Run.moving`，几何上跳过位置）和"文本像滑动窗口"
    （`Run.scrolling`，`looks_like_sliding` 判的）。两者可以各自成立：
    单行 ticker 常常只有后者，评论墙整块上滚常常只有前者。

    **占比而不是布尔**：一个区域里混着不动的行是常态（片尾表里也有静止的标题）。
    """
    if not ids:
        return 0.0
    n = sum(1 for i in ids if runs[i].moving or runs[i].scrolling)
    return n / len(ids)


def stitch_slots(runs: list[Run], win_regions: list[dict], W: int, H: int,
                 anchor: bool = True, mutual_x: bool = False,
                 span_ratio: float = SLOT_SPAN_RATIO,
                 geom_mode: str = "pooled", move_seed_last: float = 0.0,
                 reassign: bool = False, reassign_iters: int = 3,
                 h_ratio: float = SLOT_H_RATIO) -> list[list[int]]:
    """把各窗的区域缝成跨窗的"版式槽位"。返回每个槽位含哪些 win_regions 下标。

    和 build_tracks 一样用**质心分配**而不是并查集：并查集在这里会重新引入
    我们刚修掉的那个病——槽位 A 和 B 各差一点，一路 union 又串成一片。

    判据是 **y 区间重叠**，不是 y 中心距离。中心距离在多行字幕上是错的：
    同一条字幕带，1 行时中心在 0.85、3 行时在 0.76，差 54 px 就缝不上了
    （实测整片上对话被拆成 #1 266 条 / #3 63 条两个槽位）。区间重叠没这个问题。

    **但光有区间重叠会被"大吃小"**（6.2h 整片实测，clustering 报告）：
    `span_overlap` 除的是较短那个区间，于是任何小区域只要整个落在大区域里
    就得 1.0。片尾滚动 staff 表在一个 60 s 窗里是一个 y 跨度 0.97 的合法区域，
    而 `stitch_slots` 按 run 数降序排——它必然第一个当种子，然后把 5302 个
    窗区域里的 3249 个吸进同一个槽位（29222 条 run 吃掉 19122 条），
    这些成员的 y 跨度中位数只有 0.041。
    病**不在窗内聚类**：窗内区域只有 19/5302 超过 0.5H，且全是这张滚动表。

    两条对称性判据（`anchor=False` 可关掉对比）：
      **共边** 同一个版式槽位是屏幕上一个**固定位置**：字幕带 1 行变 3 行时，
              往上长还是往下长取决于对齐方式，但总有一条边不动。要求
              min(|Δy0|, |Δy1|) <= SLOT_ANCHOR_H × 行高。
      **同量级** 槽位和成员应当是**同一个框**，而不是一个套在另一个里面。
              跨度比（垫 2 倍行高后）超过 SLOT_SPAN_RATIO 就不是。
    """
    def sig_of(members: list[int]):
        if geom_mode == "median":
            return slot_geom_median(runs, win_regions, members)
        return region_geom(runs, [j for w in members for j in win_regions[w]["runs"]])

    # **在动的区域不当种子**（text-motion 计划第 2 步，`--move-seed-last`，默认关）：
    # 65% 那次事故的机制是"排序键把最坏的样本排在最前面"——片尾滚动表在一个 60 s 窗里
    # 是个 y 跨度 0.97 的**合法**大区域，按 run 数降序它必然第一个当种子。
    # 这里不把它从缝合里删掉（那会丢 run），只是**排到最后**：等静止的版式先立住，
    # 它再去找位置；找不到就自己开一个槽位，那正是它该待的地方。
    mv = ([moving_share(runs, win_regions[i]["runs"]) for i in range(len(win_regions))]
          if move_seed_last > 0 else [0.0] * len(win_regions))
    order = sorted(range(len(win_regions)),
                   key=lambda i: (mv[i] >= move_seed_last if move_seed_last > 0 else False,
                                  -len(win_regions[i]["runs"])))
    slots: list[list[int]] = []
    cent: list[tuple[float, float, float, float, float, float]] = []
    for i in order:
        cx, x0, x1, y0, y1, h = region_geom(runs, win_regions[i]["runs"])
        best, best_d = -1, None
        for k, sig in enumerate(cent):
            yo = slot_fits((cx, x0, x1, y0, y1, h), sig, W, anchor, mutual_x, span_ratio, h_ratio)
            if yo is None:
                continue
            if best_d is None or 1 - yo < best_d:
                best, best_d = k, 1 - yo
        if best < 0:
            slots.append([i])
            cent.append((cx, x0, x1, y0, y1, h))
        else:
            slots[best].append(i)
            cent[best] = sig_of(slots[best])

    if not anchor:
        return slots                    # --no-slot-anchor = 整条 2026-09-04 的修法都退回

    # 收敛后再合并一轮。**贪心分配天生依赖顺序**：排序键是 run 数，
    # 第一个种子是"最大"的而不是"最典型"的。yuka 整片实测——字幕带的种子
    # 是个 y=[0.943,0.998] 的底部窄片（62 条 run），紧接着来的 [0.849,0.932]
    # 和它不重叠只好另开一个槽位；等前者长到 [0.799,0.974] 时后者已经独立存在，
    # 于是同一条字幕带散在 13 个槽位里（947 个窗区域最多的那个只占 326）。
    # 槽位的几何这时已经收敛，用同一套判据再扫一遍就能缝上，而且不会把
    # anchor/同量级挡掉的东西放进来。
    cent = [sig_of(sl) for sl in slots]
    changed = True
    while changed:                      # 合并会让几何变，直到不动点为止
        changed = False
        a = 0
        while a < len(slots):
            b = a + 1
            while b < len(slots):
                if slot_fits(cent[a], cent[b], W, anchor, mutual_x, span_ratio, h_ratio) is None:
                    b += 1
                    continue
                slots[a].extend(slots[b])
                del slots[b], cent[b]
                cent[a] = sig_of(slots[a])
                changed = True
            a += 1

    if reassign:
        # **允许重分配 / 分裂**（audit-4 C1，2026-09-20）：上面那两步只会"合"，
        # 于是成员是按**当时**的签名收进来的、而签名随后被别的成员改掉——
        # 整片实测 44~45% 的成员不再满足最终签名的闸门。这里对着**最终**签名重判一遍：
        # 不满足就换一个满足的，都不满足就自己独立成槽。换完签名又变，所以迭代到不动点。
        #
        # ⚠ **`slot_consistency` 不是这一步的验收**（审计七）：它用的是**同一个谓词**
        # （`slot_fits_why`），所以那个百分比只是这个循环的**收敛残差**；而且它把
        # `len(sl) < 2` 的槽位排出分母——全拆成单例也能让它归零。所以这里连
        # **收敛与否**和**碎片化**一起打出来，两个数摆在一起才读得了。
        n0, single0 = len(slots), sum(1 for sl in slots if len(sl) == 1)
        mem0 = sorted(i for sl in slots for i in sl)
        converged, used = False, 0
        for _ in range(max(1, reassign_iters)):
            cent = [sig_of(sl) for sl in slots]
            moved = 0
            for k in range(len(slots)):
                if len(slots[k]) <= 1:
                    continue
                for i in list(slots[k]):
                    g = region_geom(runs, win_regions[i]["runs"])
                    if slot_fits(g, cent[k], W, anchor, mutual_x, span_ratio, h_ratio) is not None:
                        continue
                    best, best_d = -1, None
                    for k2, sig in enumerate(cent):
                        if k2 == k:
                            continue
                        yo = slot_fits(g, sig, W, anchor, mutual_x, span_ratio, h_ratio)
                        if yo is not None and (best_d is None or 1 - yo < best_d):
                            best, best_d = k2, 1 - yo
                    if len(slots[k]) <= 1:
                        break
                    slots[k].remove(i)
                    if best >= 0:
                        slots[best].append(i)
                    else:
                        slots.append([i])
                        cent.append(g)
                    moved += 1
            slots = [sl for sl in slots if sl]
            used += 1
            if not moved:
                converged = True
                break
        # **成员守恒**：重分配只搬家，不许凭空多出或丢掉一个窗区域
        mem1 = sorted(i for sl in slots for i in sl)
        if mem1 != mem0:
            raise AssertionError(f"重分配把成员弄丢/弄多了：{len(mem0)} -> {len(mem1)}")
        n_mem = len(mem1)
        print(f"缝合重分配（--slot-reassign）：{used}/{max(1, reassign_iters)} 轮、"
              + ("**收敛**" if converged else "**没收敛**（还有成员在动，加 --slot-reassign-iters）")
              + f"；槽位 {n0} → {len(slots)}（单成员 {single0} → "
              + f"{sum(1 for sl in slots if len(sl) == 1)}）、成员 {n_mem} 条"
              + "。⚠ 一致性那个百分比是这一步的**收敛残差**，不是质量指标（audit-7）",
              flush=True)
    return slots


# ---------- 特征与判读 ----------

def dominant_text_share(rs: list[Run]) -> float:
    """最大的一组"意思相同"的文本占了多少时长。

    水印被 OCR 抖成十几种写法时，distinct_text_ratio 会骗人（看着像内容一直在变），
    这个指标不会：抖动版本之间的相似度很高，会被归到同一组。
    """
    # 下面是 `text_sim(a, rep) >= 0.8` 的**逐位等价**展开，只为把 SequenceMatcher
    # 挡在门外。这一个函数就是长片上 build_tracks 的全部开销：
    # 6.2h yuka（218 个区域、最大的一个 6,316 条 run）cProfile 下累计 683.9 s，
    # 而 stitch_slots 只有 10.0 s——**瓶颈不在聚类，在这个统计量**。
    # 两道门，都是 ratio 的上界，达不到 0.8 就不必真跑：
    #   长度门        ratio <= 2*min/(min+max)，要它 >= 0.8 就得 3*min >= 2*max
    #   quick_ratio   字符多重集的交集
    # 相等/前缀两条在门之前判——它们返回 1.0 / 0.95，和长度无关。
    # 实测（整片 build_tracks 端到端）：yuka 5m13 -> 2m05，sz 31.6 s -> 18.7 s，
    # 两片的全部产物逐字节相同。
    # 每组一个 SequenceMatcher、seq2 = 代表文本：b2j（difflib 里最贵的那步）只在代表换了时重建，
    # 原来每比一次都 set_seq2 重建一遍（2026-09-24 profile：整场 gi1 上 __chain_b 33 s）。seq1 / seq2 的角色不变（ratio 不对称）
    groups: list[tuple[str, float]] = []            # 代表文本, 累计时长
    sms: list[SequenceMatcher | None] = []          # 和 groups 对齐；代表为空的组是 None
    for r in rs:
        dur = (r.t_end - r.t_start) / 1e6
        a = r.text
        la = len(a)
        for k, (rep, tot) in enumerate(groups):
            if not a or not rep:
                continue
            if not (a == rep or a.startswith(rep) or rep.startswith(a)):
                lb = len(rep)
                if 3 * min(la, lb) < 2 * max(la, lb):
                    continue
                sm = sms[k]
                sm.set_seq1(a)
                if sm.quick_ratio() < 0.8 or sm.ratio() < 0.8:
                    continue
            new_rep = rep if len(rep) >= la else a
            groups[k] = (new_rep, tot + dur)
            if new_rep is not rep:
                sms[k] = SequenceMatcher(None, "", new_rep) if new_rep else None
            break
        else:
            groups.append((a, dur))
            sms.append(SequenceMatcher(None, "", a) if a else None)
    total = sum(t for _, t in groups)
    return max(t for _, t in groups) / total if total else 0.0


def script_of(ch: str) -> str:
    """一个字符属于哪套文字。用码位范围，不引第三方库。"""
    o = ord(ch)
    if 0x3040 <= o <= 0x30FF or 0x31F0 <= o <= 0x31FF:
        return "kana"
    if 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF:
        return "hangul"
    if 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or 0xF900 <= o <= 0xFAFF:
        return "han"
    if 0x0400 <= o <= 0x04FF:
        return "cyrillic"
    if ch.isascii() and ch.isalpha():
        return "latin"
    if ch.isdigit() or (0xFF10 <= o <= 0xFF19):
        return "digit"
    return "other"


def text_language(t: str) -> tuple[str, float]:
    """一段文本的语言，以及"有多少字是真文字"（不含数字/符号）。

    只按码位判，不猜语种细分：**判读不求完备**（owner 2026-09-03）。
    日文靠假名认，但**不能用"假名占总字数"当阈值**——中文字幕被多语模型识别时
    会掺进几个假名噪声，zh-multi 上就有 6.9%。改用 `假名/(假名+汉字)`：
    日文正常在 40% 以上，中文的噪声在 10% 以下，中间空得很开。
    """
    c: Counter[str] = Counter()
    for ch in t:
        if not ch.isspace():
            c[script_of(ch)] += 1
    total = sum(c.values())
    if not total:
        return "none", 0.0
    letter_frac = (total - c["digit"] - c["other"]) / total
    cjk = c["kana"] + c["han"]
    if cjk and c["kana"] / cjk >= 0.15:
        lang = "ja"
    elif c["hangul"] >= 0.15 * total:
        lang = "ko"
    elif c["han"] >= 0.2 * total:
        lang = "zh"
    elif c["cyrillic"] >= 0.2 * total:
        lang = "ru"
    elif c["latin"] >= 0.2 * total:
        lang = "latin"
    else:
        lang = "sym"                       # 数字/符号为主：多半是 UI、比分、时钟
    return lang, round(letter_frac, 3)


def region_language(texts: list[str]) -> tuple[str, str, float]:
    """区域的语言：主语言、混合成分、真文字占比。

    **逐条判再统计，不是把整区文本拼起来判**：双语字幕带（zh-sub 就是中日两行叠着）
    拼起来只会得到一个多数语言，丢掉"这一带同时有两种语言"这个对用途 2 很要紧的信息
    ——哪一行是原文、哪一行是译文，正是匹配器要知道的。
    """
    langs = [text_language(t) for t in texts if t.strip()]
    if not langs:
        return "none", "", 0.0
    c = Counter(l for l, _ in langs)
    letter_frac = round(sum(f for _, f in langs) / len(langs), 3)
    main = c.most_common(1)[0][0]
    mix = "+".join(sorted(k for k, v in c.items()
                          if k not in ("none", "sym") and v >= 0.2 * len(langs)))
    return main, mix, letter_frac

def primary_score(f: dict, label: str) -> float:
    """这个槽位有多像"主字幕/主对话轨"。

    Owner 的方向是**把可能高价值的标出来**，不求完备判读，所以这里只出一个可比的分，
    由调用方看排序，而不是给一个 True/False 断言。

    三个乘数，都来自已经有的统计量，没有引入新常数：
      - 条数：主轨的条目一定多
      - 常驻度：主轨在大多数时间窗里都出现（[clustering.md](clustering.md)）
      - 是不是"会换内容"：水印/时钟 fill_active 高但 dominant_share 也高，要排掉
    单条时长离谱（太短或长到像常驻）和纯符号轨直接给 0。
    """
    if label in ("text-wall", "clock", "noise", "static-overlay"):
        return 0.0                        # 这几类已被现成规则认出来，不在打分里重造判据
    if f["lang"] in ("sym", "none") or f["letter_frac"] < 0.5:
        return 0.0
    # **只有下限，没有上限**（2026-09-08 去掉上限）。上限原来是 15 s，职责是
    # "排掉常驻"，可那件事下一行的 resident 判据已经在做了——**两道闸门管同一件事，
    # 而上限是个悬崖**：原神对话节奏慢，cue 时长中位 14.5 s 紧贴 15，
    # 把同一段素材从 10 分钟切成 5 分钟，中位挪到 16.2 s，label 从 subtitle 翻成 misc、
    # 分数归零、**主轨凭空消失**（data/samples.md 的 q-gi-s2，quick 集第一次跑就撞上）。
    # 去掉之前在 22 份现成产物上做过模拟（不重跑，只拿 tracks.json 的 features 重算）：
    # **只有那一份的主轨会变，其余 21 份一模一样**。
    if f["median_dur_s"] < 0.3:
        return 0.0
    if f["fill_active"] > 0.7 and f["dominant_share"] > 0.5:      # 常驻不换内容 = 水印
        return 0.0
    # 最后两项都在说同一件事：**主字幕带是一条又宽又扁的带**。
    #
    #   `w`            —— 直播评论墙老是抢主轨（ja-title、zh-gamesub 上实测），
    #                     它和字幕带最干净的差别就是宽度：墙是竖窄条（w 0.04–0.11），
    #                     字幕带横贯画面（w 0.19–0.34）。
    #   `w/(w+y_span)` —— "扁"。这一项是被 yuka 逼出来的：法庭辩论那一屏的大字散在
    #                     画面中部，聚类把它和说话人名牌并成一个区域，724 条 run 压过了
    #                     真字幕带的 604 条（129.5 对 126.2）。那个区域 y 跨度 0.40，
    #                     真字幕带只有 0.11——**宽度分不开它们，扁不扁分得开**。
    #
    # 两项都是必要条件，所以相乘，和上面几项同一个写法。**不引入新常数**：
    # w 和 y_span 都是现成的统计量，比值本身无量纲。
    band = f["w"] / max(1e-6, f["w"] + f["y_span"])
    return round(f["n_runs"] * f["persistence"] * (1 - f["dominant_share"])
                 * f["w"] * band, 2)

def covered_windows(runs: list[Run], run_ids: list[int], window_us: int) -> list[int]:
    """这些 run **真实覆盖**到的时间窗（用来算常驻度）。

    和 `window_of()` 的分工（methodology-audit-4 报告 C2）：
    `window_of` 按中点给 run 定**一个**归属窗——那是为了"同一条 run 不被重复输出"，
    是对的；但拿归属窗数常驻度会被**切分**左右：一条 0–180 秒一直在屏上的 run
    只归中间那个窗，persistence 算 1/3；把同一段文字切成三条相邻 run 就变成 1.0。
    **屏上占用没变，分却变了。** 所以常驻度改用真实覆盖。
    """
    out: set[int] = set()
    for i in run_ids:
        r = runs[i]
        lo = r.t_start // window_us
        hi = max(r.t_start, r.t_end - 1) // window_us
        out.update(range(lo, hi + 1))
    return sorted(out)


def slot_consistency(runs: list[Run], win_regions: list[dict], slots: list[list[int]],
                     W: int, anchor: bool, mutual_x: bool,
                     span_ratio: float, geom_mode: str, h_ratio: float = SLOT_H_RATIO
                     ) -> tuple[int, int, Counter]:
    """缝合完之后，回头看**每个成员还满不满足最终签名的闸门**。

    返回 `(不满足的, 总数, 卡在哪一条的计数)`——第三项是 2026-09-08 加的：
    整片上这个数是 44–45%，"卡在哪一条"才是决定要不要允许拆分/重分配的设计输入。

    存在的理由（methodology-audit-4 报告 C1）：`stitch_slots` 是贪心的，
    每并进一个成员就用池化后的几何重算签名，**只合不分**，
    从不回头检查早先的成员对最终签名还成不成立。
    "每次合并都过了闸门" != "最终全体成员都遵守闸门"——
    审计给的反例：五个窗区域纵向依次偏移 20 px，默认参数把它们并成一个槽位，
    而拿最终签名去问 `slot_fits`，中间那个成员返回 None。

    **这里只报数、不改算法**：要不要允许拆分/重分配是聚类架构的取舍，
    得先有这个数才知道它值不值得动（audit-4 的建议也是先把它做成可测指标）。
    """
    bad = tot = 0
    why: Counter[str] = Counter()
    for sl in slots:
        if len(sl) < 2:
            continue
        ids = [j for w in sl for j in win_regions[w]["runs"]]
        sig = (slot_geom_median(runs, win_regions, sl) if geom_mode == "median"
               else region_geom(runs, ids))
        for w in sl:
            tot += 1
            g = region_geom(runs, win_regions[w]["runs"])
            ok, reason = slot_fits_why(sig, g, W, anchor, mutual_x, span_ratio, h_ratio)
            if ok is None:
                bad += 1
                why[reason] += 1
    return bad, tot, why


MAIN_LABELS = ("subtitle", "dialogue-or-caption")
"""能当正文轨的标签。**排名和接受是两件事**（methodology-audit-4 报告 C3）。"""


MAIN_BAND_DEFAULT = 3.0
"""`--main-band` 的默认值：并带时允许的中心 y 差，单位是**主轨行高**。

**2026-09-08 由关（0）改成开（3），owner 拍板。** 依据是五部整片的头对头
（clustering 报告那一节）：默认几何 + 并带，净命中 +1/+3/+0/+1/+6，
"有内容 ≥3 字" 0/+3/0/0/+2，**五部没有一部是负的**；同时把两次"主轨换区域"
的事故各抹平一次（`--slot-geom median` 的 −191 追回 190、
`--rec-batch-size 8` 的 −662 追回 663）。用途 1 的产物零影响。

3 这个数不是扫出来的最优值，是"一个对话框的第二行大约差 1–3 倍行高"这个
版式常识取的上界，一次就用了它。**没扫过 2 / 4 / 5。**
"""


def band_regions(report, main_i: int, max_dcy: float,
                 h_lo: float = 0.7, h_hi: float = 1.4,
                 need_label: bool = False, need_x: bool = False,
                 geom: dict | None = None,
                 W: int = 0, mutual_x: bool = False) -> list[int]:
    """和主轨**同一条字幕带**的那些区域（不含主轨自己）。

    存在的理由（gamestream-first-look 报告）：匹配器只看主轨一个区域，
    所以聚类只要把字幕带切成两块，第二块里的台词就**整段消失**——
    f1 上 `--slot-geom median` 丢的 191 条连续台词一条没丢，只是跑到了 region07。
    audit-4 C1 的出路因此不是"让签名更严"（更严只是把"合错了"换成"没合上"），
    而是**让主轨可以由多个区域组成**。

    判据（`in_band`）相对**主轨自己的字高**归一化，无量纲（owner 指示 7）：
    中心 y 差 `max_dcy` 倍字高以内、字高比在 `[h_lo, h_hi]`。
    一个对话框的第二行大约差 1–3 倍字高。

    **时间共现这一维实测没用**（一次性探针 `probe_main_band.py`，f1-med）：
    几何选 13 个区域 1,907 条 run，再加"时间共现 ≥50%"只剔掉 1 条；
    而**只按时间共现**会选中 79 个区域 25,457 条——画面下方的常驻 UI 和谁都共现。
    所以这里只用几何，探针里那两列仍然打出来，好在别的素材上复查。
    """
    mf = next(f for ri, _, f in report if ri == main_i)
    mg = geom.get(main_i) if (need_x and geom is not None and W) else None
    out = []
    for ri, lab, f in report:
        if ri == main_i:
            continue
        # ① 和主轨的**接受判据**对齐（`--main-band-label`）。
        #    第一眼看它"太狠"（quick 六段里三段的并带大幅缩水），
        #    **摆出被挡掉的区域才发现挡对了**：q-yuka-f1-band 缩掉的那 96 条 cue
        #    来自 r00——`misc`、cy=0.982、cx=0.094、文本是 `Wns.3`/`Bsy`/`Duininy`，
        #    是屏幕左下角的 UI 垃圾。**先摆文本，再算比例。**
        if need_label and lab not in MAIN_LABELS:
            continue
        if in_band(mf, f, mg, geom.get(ri) if mg else None, max_dcy, h_lo, h_hi, W, mutual_x):
            out.append(ri)
    return out


def in_band(mf: dict, f: dict, mg: tuple | None, g: tuple | None, max_dcy: float,
            h_lo: float, h_hi: float, W: int = 0, mutual_x: bool = False) -> bool:
    """一块文字（一个区域，或 `band_windows` 的一个"区域 × 时间窗"）和主轨是不是同一条字幕带：
    `f` / `mf` 给中位 `cy`、`h`（画面比例），`g` / `mg` 是 `region_geom` 的几何签名（`None` = 不看 x）。

    y 和字高相对**主轨自己的字高**归一化，无量纲（owner 指示 7）。
    x 复用另外两层现成的判据（`--main-band-x`；区间重叠 **或** 中心对齐），不新写一条：
    靠左 / 靠右走区间重叠（短行整个落在长行里，分母取较窄的那个），居中走 cx 距离。"""
    mh = max(mf["h"], 1e-6)
    if not (abs(f["cy"] - mf["cy"]) / mh <= max_dcy and h_lo <= f["h"] / mh <= h_hi):
        return False
    if mg is not None and g is not None:
        if (x_overlap_ratio([mg[1], 0, mg[2], 1], [g[1], 0, g[2], 1], mutual_x) < 0.30
                and abs(mg[0] - g[0]) > 0.08 * W):
            return False
    return True


def band_windows(runs: list[Run], region_ids: dict[int, list[int]], report, main_i: int,
                 taken: set[int], window_us: int, max_dcy: float, h_lo: float, h_hi: float,
                 need_label: bool, mg: tuple | None, W: int, H: int,
                 mutual_x: bool = False) -> dict[int, list[int]]:
    """`--main-band-unit window`：没整块并进来的区域，再**按时间窗拆开**逐块判一次（`in_band` 同一条判据）。
    返回 `{区域: 并进主轨的 run 下标}`。

    病根（followups「并带仍会被区域组成的小扰动带偏」）：跨窗缝合把某几个窗里的字幕带缝进了一个混合槽位，
    整块区域的中位几何（cy / 字高）离主轨很远，按区域判就整块不并——可那几个窗里的 run 本身就在字幕带上。
    三处实例都是这个形状：zzz 整场 6 句在 r06（区域 cy 0.43、字高 2.06 倍；那几条事件 cy 0.61–0.65、字高 1.0–1.1 倍），
    f5 `median` 下 8 条在 r08（区域字高 1.61 倍；事件 1.19 倍），hsr 换行尾巴在 r49。
    一个"区域 × 时间窗"大致就是缝合之前的那个窗区域：窗内聚类的产物，几何还没被别的窗带偏。

    **块过了带，成员再逐条过一次同一个 `in_band`**（2026-10-06，验收 peer）：块判的是中位几何，过了就整块并进来的话，
    块里单条明显不在带上的也跟着进来——gi1 的 `Lv.90`（cy 0.94、字高 0.6 倍）、hsr 整块对话记录面板、读物面板，
    四场整场按窗并进的事件里四到六成单条过不了带。逐条判不等于逐条 run 单独判（10-05 量过的对照臂，hsr 对不上剧本 137 → 299，已删）：
    没过块判的那些块里的零星 run 照样进不来。"""
    mf = next(f for ri, _, f in report if ri == main_i)
    lab_of = {ri: lab for ri, lab, _ in report}

    def passes(sub: list[int]) -> bool:
        rs = [runs[i] for i in sub]
        f = {"cy": statistics.median(r.cy for r in rs) / H, "h": statistics.median(r.h for r in rs) / H}
        return in_band(mf, f, mg, region_geom(runs, sub) if mg is not None else None, max_dcy, h_lo, h_hi, W, mutual_x)

    out: dict[int, list[int]] = {}
    for ri, ids in region_ids.items():
        if ri in taken or (need_label and lab_of.get(ri) not in MAIN_LABELS):
            continue
        by_win: dict[int, list[int]] = {}
        for i in ids:
            by_win.setdefault(window_of(runs[i], window_us), []).append(i)
        for w, sub in sorted(by_win.items()):
            if passes(sub):
                keep = [i for i in sub if passes([i])]
                if keep:
                    out.setdefault(ri, []).extend(keep)
    return out


MAIN_MOVE_MAX = 0.3
"""`--main-move-max` 的默认：`moving_share` 达到这么多的区域不当主轨（owner 2026-09-11 翻成默认）。

翻之前是 1.0（不拦），理由是"当前素材上没发生过、是保险不是改善"。09-11 发生了：星铁 / 绝区零整场的
主轨分别被滚动的阅读面板（`moving_share` 0.536）和左下角实时弹幕（0.779）抢走，命中 71/626、0/1,288。
先翻成 0.5，同日改成 **0.3**：离线扫过，两场整场、九段切片、原神两场、《魔裁》五部共 16 份产物在
0.3–0.5 之间挑中的主轨完全相同；被接受的真主轨 `moving_share` 最高 0.048，0.5 离阅读面板只剩 0.036，
0.3 两边的余量都大得多（game-text-corpus 报告、docs/architecture/defaults.md）。"""


def pick_main(report: list[tuple[int, str, dict]],
              min_score: float = 0.0,
              move_max: float = MAIN_MOVE_MAX) -> tuple[tuple | None, list[tuple]]:
    """从区域里挑主轨，挑不出来就**明说没有**。返回 `(选中的, 候选列表)`。

    为什么不是 `max(primary_score)`（这是 2026-09-08 改的）：

    * 旧写法只要 `report` 非空就无条件指一个主轨，**哪怕所有区域都是 0 分**——
      菜单、片头、纯 UI 的片段也会被指一条送去当正文匹配，错误再沿链放大到
      挑名牌、并同带碎片（methodology-audit-4 报告 C3）。
    * 换素材实测：有台词的 5 段里错了 2 段（gamestream-first-look 报告）。
      `hsr-s2` 挑中主播自己压的中英双语字幕、`zzz-s2` 挑中屏幕顶部的角色名带，
      而且**错的那两条分数比对的还高**（11.5 / 25.6 对 5.4 / 6.0）——
      单靠调分数的权重救不回来。

    修法**不引入新常数**：`label_region` 里的 `subtitle` / `dialogue-or-caption`
    已经编码了"在画面下部、够宽、每条几秒"这些形状条件，只是 `primary_score`
    从来没用过它。把它当**接受判据**（分数排名照旧）之后，10 个有真值的素材
    （5 段游戏切片 + yuka 五部）**全部选对**。

    `move_max`（`--main-move-max`，默认 `MAIN_MOVE_MAX` = 0.3；1.0 = 不拦）是第二道接受判据：
    在动的区域不当主轨（text-motion 计划第 3 步）。滚动的东西——片尾表、
    ticker、评论墙、阅读面板、实时弹幕——都不是用途 2 要的那条正文轨；而它一旦当上主轨，
    整部片子的匹配就全废（星铁 / 绝区零整场实测，见 `MAIN_MOVE_MAX`）。
    """
    # `move_max >= 1.0` = **关**。别写成 `share < move_max`：`moving_share` 本身能到 1.0，
    # 于是"整块都在动"的区域在关着的时候也被排除了——关掉的旋钮不许改行为。
    cand = [x for x in report
            if x[2]["primary_score"] > max(0.0, min_score) and x[1] in MAIN_LABELS
            and (move_max >= 1.0 or x[2].get("moving_share", 0.0) < move_max)]
    cand.sort(key=lambda x: -x[2]["primary_score"])
    return (cand[0] if cand else None), cand


def region_features(rs: list[Run], n_tracks: int, W: int, H: int,
                    dur_us: int, active_us: int, persistence: float) -> dict:
    covered = uigate.merge_intervals([(r.t_start, r.t_end) for r in rs])
    on_us = sum(e - s for s, e in covered)
    texts = [r.text for r in rs]
    durs = [(r.t_end - r.t_start) / 1e6 for r in rs]
    lang, lang_mix, letter_frac = region_language(texts)
    return {
        "n_tracks": n_tracks,
        "n_runs": len(rs),
        # fill 是"占整片多久"，fill_active 是"在它出现的那些窗里占多久"。
        # 长片上必须分开看：片头标题的 fill 只有 0.002，fill_active 却接近 1，
        # 它不是噪声，只是**存在期很短**。只看 fill 会把它当噪声扔掉。
        "fill": round(on_us / max(1, dur_us), 3),
        "fill_active": round(on_us / max(1, active_us), 3),
        "persistence": round(persistence, 3),
        "dominant_share": round(dominant_text_share(rs), 3),
        "time_text_ratio": round(sum(bool(TIME_RE.search(t)) for t in texts) / max(1, len(texts)), 3),
        "distinct_text_ratio": round(len(set(texts)) / max(1, len(texts)), 3),
        # 带 `move` 原语的 run 占比（text-motion 计划）。落进产物是有意的：
        # 用途 1 要知道这块在不在动，`pick_main` 的第二道接受判据也读它。
        "moving_share": round(sum(1 for r in rs if r.moving or r.scrolling)
                              / max(1, len(rs)), 3),
        "median_dur_s": round(statistics.median(durs), 2),
        "cx": round(statistics.median(r.cx for r in rs) / W, 3),
        "cy": round(statistics.median(r.cy for r in rs) / H, 3),
        "w": round(statistics.median(r.w for r in rs) / W, 3),
        "h": round(statistics.median(r.h for r in rs) / H, 4),
        "lang": lang,
        "lang_mix": lang_mix,
        "letter_frac": letter_frac,
        # 端点用 2%/98% 分位，不用 min/max——与 region_geom 同一个理由
        # （predet 报告踩过的坑）：min/max 会被离群框撑大。这里不是纯统计洁癖，
        # y_span 直接喂 label_region 的 text-wall 判据，**一个离群 run 就能把主字幕带
        # 打成 text-wall**，primary_score 随即归 0、主轨凭空消失。
        "y_span": round((pct([r.cy for r in rs], 0.98) - pct([r.cy for r in rs], 0.02)) / H, 3),
        "x_span": round((pct([r.box[2] for r in rs], 0.98)
                         - pct([r.box[0] for r in rs], 0.02)) / W, 3),
    }


def label_region(f: dict, min_runs: int) -> str:
    """v1 启发式判读。故意写得直白，方便看清是哪条规则在起作用。

    按 owner 的方向：**不求完备**，只把可能高价值的标出来。
    v1 相对 v0 的唯一实质变化是把"常驻"判据从 fill 换成 **fill_active × persistence**——
    长片上 fill 会被"存在期短"稀释，水印和片头标题会被混为一谈。
    """
    # 噪声 = 又少又短。**只看条数会误杀常驻区域**：一个纹丝不动的时钟文本
    # 只会产生 1 条 run，却铺满整段——那是最不像噪声的东西。
    # 单独出现一次的真文字（台词、道具名、奖励标题）也会落进来（抽样约五分之一）；
    # "最长 run 内容字 ≥5 就不判 noise"量过：用途 2 的覆盖 11 组全不变，没收益，没留。
    if f["n_runs"] < min_runs and f["fill_active"] < 0.2:
        return "noise"
    resident = f["fill_active"] > 0.70 and f["persistence"] > 0.5   # 常驻 = 窗内满 且 大多数窗都在
    if resident and f["time_text_ratio"] >= 0.5:
        return "clock"                              # 常驻 + 文本长得就像时间，比统计 churn 可靠
    if resident and f["dominant_share"] > 0.50:
        return "static-overlay"                     # 水印 / 台标 / 常驻 UI（允许 OCR 抖动）
    if f["n_tracks"] >= 5 and f["y_span"] > 0.25 and f["x_span"] < 0.40:
        return "text-wall"                          # 滚动评论墙 / 弹幕列
    # 时长**只剩下限**，和 `primary_score` 保持同一口径。
    # 这里原来是 12、那边是 15，两层说同一件事却用两个数，代价是实测踩到的：
    # 原神某段对话节奏慢，主轨 cue 时长中位 14.5 s，于是**分数认它、label 不认它**，
    # 判成 misc，按 label 做接受判定时就把真主轨挡在门外（gamestream-first-look.md）。
    # 09-08 先对齐到 15（2,154 个区域里 62 个换标签），当天又发现**对齐还不够**：
    # 15 是个悬崖，同一段素材换个窗口长度就能从 14.5 s 挪到 16.2 s，主轨凭空消失。
    # 上限的职责"排掉常驻"由上面的 resident 判据承担，这里不再重复一遍。
    if f["cy"] > 0.60 and f["w"] > 0.12 and f["median_dur_s"] >= 0.3 and f["n_runs"] >= 3:
        return "subtitle"                           # 下方、宽、条目多
    if 0.35 < f["cy"] <= 0.95 and f["n_runs"] >= 3:
        return "dialogue-or-caption"
    if f["h"] < 0.03 and f["n_runs"] <= 3:
        return "ui-static"
    return "misc"


# ---------- 输出 ----------

srt_ts = srtio.fmt_ts_us
"""时间戳格式与"一条 = 一个 cue"的口径统一在 `flowocr.artifacts.srtio`。
历史上 5 个工具各写各的解析，导致 "条数" 被 shell 里一个写错的 grep 破坏了 30 小时
（methodology-audit 报告）。"""


def row_order(rs: list[Run]) -> list[Run]:
    """段里 run 的导出行序：先分行（`tracksio.row_index`），行从上往下，行内按左沿从左往右。
    同一行被破折号之类断成两个框时，按 `(cy, cx)` 排会让纵向中心略高的后半截排到前面
    （2026-10-05 魔裁：「大魔女を——ユキを殺す【犯人】は、」排成两行、还颠倒）。
    先按 `(cy, cx, t_start, text)` 定一个确定的初序：`segment_runs` 拿 id 列表判"内容没变"，同一组 run 每次都要排成一样。"""
    rs = sorted(rs, key=lambda r: (r.cy, r.cx, r.t_start, r.text))
    row = tracksio.row_index([r.box for r in rs])
    return [r for _, _, _, r in sorted((row[i], r.box[0], i, r) for i, r in enumerate(rs))]


def segment_runs(rs: list[Run]) -> list[tuple[int, int, list[Run]]]:
    """把一个区域内互相重叠的 run 切成不重叠的时间段，每段带当时在场的所有行。

    这是"字幕该长什么样"的定式：说话人名 + 台词同时在屏上，就该是一条两行的字幕，
    而不是两条时间重叠的条目。
    """
    bounds = sorted({t for r in rs for t in (r.t_start, r.t_end)})
    out: list[tuple[int, int, list[Run]]] = []
    for s, e in zip(bounds, bounds[1:]):
        active = [r for r in rs if r.t_start <= s and r.t_end >= e]
        if active:
            active = row_order(active)
            if out and [id(x) for x in out[-1][2]] == [id(x) for x in active] and out[-1][1] == s:
                out[-1] = (out[-1][0], e, active)      # 内容没变就接上，别切碎
            else:
                out.append((s, e, active))
    return out


def merge_growing_segments(segs: list[tuple[int, int, list[Run]]]
                           ) -> list[tuple[int, int, list[Run]]]:
    """把"行只增不减"的相邻段并成一条。

    打字机让同一条字幕的各行**错开出现**：说话人名先出、第一行再出、第二行最后出。
    `segment_runs` 在每个 run 的首尾都切一刀，于是一条字幕被切成三条，前两条是半截。
    整片实测这类半截占了"比望言多出的条目"的一大半。

    判据只用集合关系，不用任何时长常数：**前一段的行是后一段的真子集且时间相接**，
    就是"又多出来一行"，属于同一条字幕。反过来（行减少）说明这条字幕在收尾，
    不并——那才是真的换条。
    """
    out: list[tuple[int, int, list[Run]]] = []
    for s, e, active in segs:
        if out:
            ps, pe, prev = out[-1]
            ids, pids = {id(r) for r in active}, {id(r) for r in prev}
            if pe == s and pids < ids:
                out[-1] = (ps, e, active)
                continue
        out.append((s, e, active))
    return out

JOIN_HEAD_MISS = 0.15
"""同句合并：前一段的一行 `a` 被后一段同一行"接着"时，`a` 和 `b[:len(a)]` 对不上的字最多占 `a` 的这么多、至少容 1 个
（量的是 OCR 抖动：`飼育·展` 对 `飼育・展示…` 4 个字错 1 个，按相似度比是 0.75，一个字就够把短行挡在 0.85 的门外）。"""
NAME_CHANGE_SIM = 0.5
"""切点两侧的名牌相似度低于这个就当换了说话人、不并（只在两侧全部正文行都换了 run 时才比，见 `can_join`）。"""


def _sq(t: str) -> str:
    return "".join(t.split())


SENTENCE_END = "。．.！!？?」』）)…‥"
"""句末的标点：一行以它结尾就是说完了的一句，不是打字机的半截（`head_similar` 只认精确前缀）。"""


def head_similar(a: str, b: str) -> bool:
    """`a` 是 `b` 的开头：去空白后前缀相同，或者和 `b` 的同长开头对不上的字不超过 `JOIN_HEAD_MISS`（至少容 1 个）；
    `a` 至少 `BODY_MIN_CHARS` 个内容字。不用 `text_sim_at_least`：它只认精确前缀，否则整串比、先过长度上界——
    `飼育·展` 对 `飼育・展示…` 在长度门上就被拒。
    `a` 以句末标点（`SENTENCE_END`）结尾、`b` 在它后面还有字时只认精确前缀：魔裁 `……どうして。` 是单独说的一句、上屏 2.6 秒，
    下一句 `どうしてユキちゃんは、` 和它只差最后一个字，容 1 个字的门会把两句并成一条。`b` 后面没字了（同一句重读、
    只有句末标点读得不一样：`販売中!` / `販売中！`）照容。"""
    a, b = _sq(a), _sq(b)
    if evalkit.content_len(a) < BODY_MIN_CHARS:
        return False
    if b.startswith(a):
        return True
    if a[-1] in SENTENCE_END and evalkit.content_len(b[len(a):]) > 0:
        return False
    same = sum(m.size for m in SequenceMatcher(None, a, b[:len(a)], autojunk=False).get_matching_blocks())
    return same >= len(a) - max(1, int(JOIN_HEAD_MISS * len(a)))


def x_overlapped(r: Run, q: Run) -> bool:
    """横向重叠至少是较窄那个的一半：同一行上的"同一处字"（打字机从同一个左沿往右长、居中的往两边长都满足）；
    同一高度、横向离得远的两处同样的字（两个头像下的 `Lv.90`）不满足。"""
    ov = min(r.box[2], q.box[2]) - max(r.box[0], q.box[0])
    return ov >= 0.5 * max(1e-9, min(r.box[2] - r.box[0], q.box[2] - q.box[0]))


def continues(r: Run, q: Run, tol: int) -> bool:
    """`q` 接着 `r`：同一条 run（跨过了切点），或者同一行、`q` 在 `r` 消失的那一刻（± `tol`）出现、文本是 `r` 的延续——
    开头相似（`head_similar`），或者 `r` 是打字机首帧的 1–2 字碎片、正好是 `q` 的前缀（同 `body_segments.lead_of`）。
    同句合并认"接上"、`seg_hide` 认半截行都用它（同一个容差）。"""
    if q is r:
        return True
    if r.moving or q.moving or not tracksio.same_row(r.box, q.box) or not x_overlapped(r, q) or abs(q.t_start - r.t_end) > tol:
        return False
    a = _sq(r.text)
    return head_similar(r.text, q.text) or (bool(a) and evalkit.content_len(a) < BODY_MIN_CHARS and _sq(q.text).startswith(a))


FRAGMENT_COVER = 0.9
"""`fragment_of`：碎片的框至少这么多落在另一行的框里（同 `script.FRAGMENT_COVER` 的意思：det 偶尔把一行静止的字多切出一块）。"""


def fragment_of(r: Run, q: Run) -> bool:
    """`r` 是 `q` 的碎片：同一行、`r` 的框 ≥ `FRAGMENT_COVER` 落在 `q` 的框里、文本是 `q` 的一段（det 偶尔在一两个采样点
    把一行切出一块，星铁 `ずばり地脈共鳴！この` 是 `でもご心配なく、…ずばり地脈共鳴！この` 的后半截）。"""
    if q is r or r.moving or q.moving or not tracksio.same_row(r.box, q.box):
        return False
    a, b = _sq(r.text), _sq(q.text)
    if not a or len(a) >= len(b) or a not in b:
        return False
    ix = max(0.0, min(r.box[2], q.box[2]) - max(r.box[0], q.box[0])) * max(0.0, min(r.box[3], q.box[3]) - max(r.box[1], q.box[1]))
    return ix >= FRAGMENT_COVER * max(1e-9, (r.box[2] - r.box[0]) * (r.box[3] - r.box[1]))


def same_line_text(r: Run, q: Run, tol: int) -> bool:
    """`r` 和 `q` 是同一处的同一行字（对称）：都不在动、同一行、横向重叠过半，而且文本是一回事——相同、一方是另一方的开头
    （`head_similar`，OCR 抖动容 15%），或一方是打字机首帧的 1–2 字碎片、正好在另一方出现时消失（`continues`）。
    `seg_hide` 在这样的两条里按同一个全序留一条（`seg_hide`），所以谁顶替谁和选代表不会打架。"""
    if q is r or r.moving or q.moving or not tracksio.same_row(r.box, q.box) or not x_overlapped(r, q):
        return False
    a, b = _sq(r.text), _sq(q.text)
    if a == b or head_similar(r.text, q.text) or head_similar(q.text, r.text):
        return True
    return continues(r, q, tol) or continues(q, r, tol)


FLICKER_MAX_OBS = 2
"""杂行至多读到过几次（`flicker`）：闪烁的 ▼、HUD 数字、名牌的抖动版只有 1–2 次观测（`body_segments.keep` 的注释）。"""


def flicker(r: Run, s: int, e: int) -> bool:
    """杂行：盖不住这段的一半、一共只读到过 ≤ `FLICKER_MAX_OBS` 次的行。不限字数——▼ 读成的 `un2ra` 有 5 个内容字。
    按 run 自己的观测数判、不按它在这段里占多少判：一条早就在屏、正好在切点消失的长命行只有尾巴落在段里，
    按"在段里的比例"判会把它当成杂行、不再要求后一段接着它（wuwa 的广告横幅换了一条，两条被并成一句）。
    没合并过的段里每一行都盖满整段，永远不是杂行。"""
    inside = min(r.t_end, e) - max(r.t_start, s)
    return r.n_obs <= FLICKER_MAX_OBS and inside < 0.5 * (e - s)


def is_name(r: Run) -> bool:
    return bool(getattr(r, "nameplate", False))


def body_rows(rs: list[Run], s: int, e: int) -> list[Run]:
    """正文行：内容字 ≥ `BODY_MIN_CHARS`、没打名牌标、在 `[s, e]` 里不是杂行、不是同一批里另一行的碎片（`fragment_of`）。"""
    return [r for r in rs if evalkit.content_len(r.text) >= BODY_MIN_CHARS and not is_name(r) and not flicker(r, s, e)
            and not any(fragment_of(r, q) for q in rs)]


def can_join(prev: tuple[int, int, list[Run]], cur: tuple[int, int, list[Run]], tol: int) -> bool:
    """相邻两段是不是同一句（cue-shape 计划的 b）。杂行按合并后的整段判，不算数。

    * 时间相接（± `tol`），后一段有正文行；
    * 前一段在切点处还在的每条正文行，后一段里都有一条接着它（`continues`：同一条 run 挂过切点，或打字机的后续）；
      前一段没有正文行时（打字机首帧、名牌先出），它在切点处的每一行都要被后一段的某一行接上（同一条 run 挂过切点也算：
      原神的称号 `傭兵` 只有 2 个字、不算正文行，却一直在屏），而且至少有一行是被**另一条** run 接上的正文行（首帧真的打下去了）；
    * 后一段另有新的正文行时，新行要和"打下去的"那几行（被另一条 run 接上的）**一起消失**（± `tol`，同 `group_cues` 的定式）；
      没有打下去的（只是同一条 run 挂着、又多了一行），就和挂着的那几行比——常驻的 HUD 行（`矢を放つ`）一直挂着、
      对话一出现就被当成"又多了一行"并进去，挡的是这个；
    * 切点两侧的**全部**正文行都换了 run（翻页重画）时，再看名牌：两侧在屏的名牌没有共用的 run、相似度又低于
      `NAME_CHANGE_SIM`，就是换了说话人，不并。"""
    ps, pe, pr = prev
    cs, ce, cr = cur
    if cs - pe > tol:
        return False
    cb = body_rows(cr, ps, ce)
    if not cb:
        return False
    at_cut = [r for r in pr if r.t_end >= pe - tol]
    pb = body_rows(at_cut, ps, ce)
    if pb:
        match = {}
        for r in pb:
            q = next((q for q in cb if continues(r, q, tol)), None)
            if q is None:
                return False
            match[id(r)] = q
        held = list(match.values())
    else:
        rest = [r for r in at_cut if not is_name(r)]
        if rest:
            if not all(any(continues(r, q, tol) for q in cr) for r in rest):
                return False
        else:
            names_p = [r for r in at_cut if is_name(r)]
            return bool(names_p) and all(any(q is r or (tracksio.same_row(r.box, q.box) and text_sim(r.text, q.text) >= NAME_CHANGE_SIM)
                                             for q in cr if is_name(q)) for r in names_p)
        held = []
    typed = [q for q in cb for r in at_cut if q is not r and continues(r, q, tol)]
    if not pb and not typed:
        return False
    fresh = [q for q in cb if all(q is not t for t in typed) and all(q is not r for r in at_cut)]
    ref = typed or held
    if fresh and any(abs(q.t_end - t.t_end) > tol for q in fresh for t in ref):
        return False
    if pb and all(match[id(r)] is not r for r in pb):
        names_p = [r for r in at_cut if is_name(r)]
        names_c = [q for q in cr if is_name(q) and q.t_start <= cs + tol]
        if names_p and names_c and not any(a is b for a in names_p for b in names_c) \
                and max(text_sim(a.text, b.text) for a in names_p for b in names_c) < NAME_CHANGE_SIM:
            return False
    return True


def join_same_sentence(segs: list[tuple[int, int, list[Run]]], tol: int) -> list[tuple[int, int, list[Run]]]:
    """**同句合并**（`--cue-join on`，cue-shape 计划的 b）：`segment` 在每个 run 的首尾都切一刀，同一句被 ▼ 翻页提示的闪烁、
    打字机半截（OCR 把 `·` / `・` 读得不一样、run 断了没续上）、名牌 OCR 抖动切成几条（2026-10-05 量：21 臂 695 条里冗余 84 条）。
    相邻两段是同一句（`can_join`）就并成一段：起点取前一段的（首字出现），成员是并集（半截行、杂行留作成员，投影时由 `seg_hide` 藏）。"""
    out: list[tuple[int, int, list[Run]]] = []
    for seg in segs:
        if out and can_join(out[-1], seg, tol):
            ps, _, pr = out[-1]
            seen = {id(r) for r in pr}
            out[-1] = (ps, seg[1], row_order(pr + [r for r in seg[2] if id(r) not in seen]))
        else:
            out.append(seg)
    return out


_HIDE_CACHE: dict[tuple, tuple[tuple, tuple[set[int], set[int]]]] = {}
"""`seg_hide` 按段缓存（判 UI、写 SRT、记 cue 各要一遍，每遍都是两两比较）。键是段的起止和成员的 `id`，值里一并存着成员本身、
命中时核是不是同一批对象（守卫、工具在一个进程里反复调，旧 run 回收后 id 会被复用）；`main` 开头清空。"""


def seg_hide(s: int, e: int, rs: list[Run], tol: int) -> tuple[set[int], set[int]]:
    """一段里投影时不给的成员（`id(run)`），返回 `(全部要藏的, 其中的半截行)`。只从段本身算，各处调同一个函数。

    **不变量：藏掉的半截行，同一行里一定留着一条看得见的、和它是同一处同一行字的代表**（peer 代码审两轮：只看"有没有顶替它的"会让
    两条互相顶替、一起藏掉；谁顶替谁和选代表按两个不同的序会让两条都留、一行字写两遍）。所以只用一个全序：碎片放最后（它所在的整行先当代表），
    其余按"消失得晚、再按活得久"——打字机的后续、OCR 抖动断开的后半段都比前面那条消失得晚。按这个序一条一条看，和已选的代表是
    同一处同一行字（`same_line_text`），或是代表的碎片（`fragment_of`），就藏、记成半截行；否则自己当代表。被藏的半截行照旧给叠加贡献首字时刻（`row_leads`）。
    * **半截行**：所有投影都不给（它是那一行的早先状态、碎片或重复读数）；
    * **杂行**（`flicker`，在代表里判）：同一行还有看得见的非杂行，或者它只有 1–2 个内容字（▼ 读成的 `0`）才藏——
      自成一行、晚打出来、只读到 1–2 次的真正文（绝区零 `無事で何よりよ、ビリー`）不藏。剔 UI 的导出不给，全部文字的视图照给、按 UI 标。
    代表全被判成杂行时杂行不藏（半截行照藏）。"""
    key = (s, e, tol, tuple(id(r) for r in rs))
    hit = _HIDE_CACHE.get(key)
    if hit is not None and all(a is b for a, b in zip(hit[0], rs)):
        return hit[1]
    row = dict(zip((id(r) for r in rs), tracksio.row_index([r.box for r in rs])))
    frag = {id(r) for r in rs if not r.moving and any(fragment_of(r, q) for q in rs)}
    rep: list[Run] = []
    partial: set[int] = set()
    for r in sorted(rs, key=lambda r: (id(r) in frag, -r.t_end, -(r.t_end - r.t_start), r.t_start, r.text)):
        # 碎片的宿主自己被藏了（代表是抖动版、子串不成立）：同一处有看得见的代表就照样藏，不让半行露出来
        if any(same_line_text(r, q, tol) or fragment_of(r, q)
               or (id(r) in frag and tracksio.same_row(r.box, q.box) and x_overlapped(r, q)) for q in rep):
            partial.add(id(r))
        else:
            rep.append(r)
    flick = {id(r) for r in rep if flicker(r, s, e)
             and (evalkit.content_len(r.text) < BODY_MIN_CHARS
                  or any(q is not r and row[id(q)] == row[id(r)] and not flicker(q, s, e) for q in rep))}
    hide = partial | flick
    # 藏完一行都不剩时只撤掉杂行那部分（那就不是杂行，是一段只有短命行的 cue）；半截行照藏——代表总留着一条
    out = (partial, partial) if len(hide) >= len(rs) else (hide, partial)
    _HIDE_CACHE[key] = (tuple(rs), out)
    return out


def visible(segs: list[tuple[int, int, list[Run]]], tol: int | None) -> list[tuple[int, int, list[Run]]]:
    """段里去掉 `seg_hide` 藏的成员（`tol` 是 None = 不合并、不藏，原样返回）：判常驻 UI、数面板、写 SRT 都只看会导出的行。"""
    if tol is None:
        return segs
    return [(s, e, [r for r in a if id(r) not in seg_hide(s, e, a, tol)[0]]) for s, e, a in segs]


def group_cues(rs: list[Run], end_tol_us: int, min_overlap: float = 0.5
               ) -> list[tuple[int, int, list[Run]]]:
    """按"一起消失"把行并成一条字幕。

    `segment_runs` 在每个 run 的首尾都切一刀，对**打字机**是灾难：
    说话人名先出、第一行再出、第二行最后出，一条字幕就被切成三条，
    前两条都是半截。整片实测这类碎片占了"比望言多出的条目"的一大半
    （`[クり` / `[クリス]` / `[クリス] + 全句` 各自成条）。

    换个定式：**同一条字幕的所有行是一起消失的**，出现却可以错开。
    于是按 t_end 分组、组内取 min(t_start) 作起点——
    起点自然就是 owner 说的"首字出现"那一态（text-effects 报告）。

    容差 `end_tol_us` 挂在**采样间隔**上，不是拍脑袋的毫秒数：
    两行若在同一个采样点上还在、下一个采样点都没了，就是一起消失的。
    """
    cues: list[dict] = []
    for r in sorted(rs, key=lambda x: (x.t_end, x.t_start)):
        hit = None
        for c in reversed(cues):
            if c["end"] < r.t_end - end_tol_us:
                break                                   # 按 t_end 排过序，再往前只会更远
            ov = min(r.t_end, c["end"]) - max(r.t_start, c["start"])
            # 除以**较长**的那个，不是较短的：一条 3 秒台词落在一条 100 秒常驻行里，
            # 除以较短的会得到 1.0（全包含），把常驻行当成同一条字幕并进来——
            # 实测这样干起点误差 P90 从 1.0s 炸到 19.5s。除以较长的则是 0.03，正确排除。
            # 同一条字幕的各行只差一个打字的前导时间，时长本来就相当。
            if ov > 0 and ov / max(r.t_end - r.t_start, c["end"] - c["start"], 1) >= min_overlap:
                hit = c
                break
        if hit is None:
            cues.append({"start": r.t_start, "end": r.t_end, "runs": [r]})
        else:
            hit["runs"].append(r)
            hit["start"] = min(hit["start"], r.t_start)
            hit["end"] = max(hit["end"], r.t_end)
    out = []
    for c in sorted(cues, key=lambda c: (c["start"], c["end"])):
        c["runs"] = row_order(c["runs"])
        out.append((c["start"], c["end"], c["runs"]))
    return out

def drop_slivers(segs: list[tuple[int, int, list[Run]]], min_us: int) -> list[tuple[int, int, list[Run]]]:
    """丢掉过短的时间段。

    边界回抠之后，同一区域里两条 run 的首尾不再对齐到同一个采样点，
    于是段切分会在交界处留下几十毫秒的碎片。它们不是新内容，只是切分的副产品。

    直接丢弃、留一个小空隙，而不是并进邻居：并进去会把邻居的边界推走 min_seg 那么多，
    丢弃最多只损失碎片自身的长度。实测（zh-sub）并进邻居会让终点误差中位从 0.117 s
    退回 0.248 s，丢弃则不会。
    """
    return [(s, e, a) for s, e, a in segs if e - s >= min_us] or list(segs[:1])


UI_SHARE = 0.5
"""判"常驻 UI"的占比门（`--ui-share` 的默认值）。**别在别处再写一个 0.5**——
`track_health` 的报警门要严格低于它，那边 `from build_tracks import UI_SHARE` 读这一份
（2026-09-19 复审：那边原来另写了个字面量，注释还说"只跟着这边读"）。"""


def persistent_ui_lines(segs, min_share: float, max_len: int = 6,
                        min_support: int = 20, min_onshare: float = 0.0,
                        film_us: int = 0) -> set[str]:
    """判为"常驻 UI"的那些短行。抽出来是为了**能把删了什么打印出来**。

    `min_onshare` 是**全片尺度**的地板（默认 0 = 关，行为和以前逐字节一致）：
    含这一行的段总时长 / 全片时长。加它的理由见 `drop_persistent_ui` 的注释里
    那段"整片上塌了"；**别在没跑五部头对头之前打开它**。
    """
    if not segs or min_share <= 0:
        return set()
    cnt: Counter[str] = Counter()
    on: Counter[str] = Counter()
    for s, e, active in segs:
        # 一条 cue 里同一行出现两次只算一次，否则打字机重复会把数撑起来
        lines = {r.text.strip() for r in active if 0 < len(r.text.strip()) <= max_len}
        cnt.update(lines)
        for t in lines:
            on[t] += e - s
    keep = {k for k, v in cnt.items() if v / len(segs) >= min_share and v >= min_support}
    if min_onshare > 0:
        # film_us 拿不到就**不做这道过滤**，而不是拿区域自己的跨度顶替——
        # 那样又会退回"分母是区域"的老毛病（gamestream-first-look 报告）。
        if film_us > 0:
            keep = {k for k in keep if on[k] / film_us >= min_onshare}
        else:
            raise ValueError("给了 min_onshare 却没给 film_us——这道地板是全片尺度的")
    return keep


def drop_persistent_ui(segs, min_share: float = 0.5, max_len: int = 6,
                       min_support: int = 20, min_onshare: float = 0.0,
                       film_us: int = 0):
    """把**常驻 UI 短行**从每条 cue 里剔掉（`Auto` / `Skip` / 图标误识成的单字…）。

    为什么需要（head2head-5films 报告）：这类按钮常年挂在屏幕上、位置固定、
    字号和正文接近，聚类没有理由把它和正文分开。实测 yuka `input3` 的主轨里
    **97.5% 的 cue 带着 `Auto`**（5,714/5,873），`input5` 是 18.4%（`大`/`New`/`Game`），
    而人手框的基线是 0.0–0.2%。

    判据**不写死词表**（写死了换个游戏就没用）：某个短行作为独立一行出现在
    超过 `min_share` 比例的 cue 里，就不可能是台词。

    **阈值不能低。** 0.2 会把说话人名牌一起判进去（`階堂ヒロ` 在 f3 占 27%、f5 占 28%），
    而名牌该不该留是另一回事（speaker-name-line 报告）。默认 0.5：
    UI 按钮是常年挂着的，名牌因为要轮换说话人，天然到不了一半。

    **`min_support` 是绝对条数下限，不能省**（methodology-audit-2 报告）：
    只看比例的话，1 条 cue 的小区域里任何 ≤6 字的行都是 100%，**必被整片清空**。
    实测代价：加这条规则之后 yuka 五部的产物里出现了 89–139 个**空的区域 SRT**
    （之前是 0），`zh-news` 60 秒片段上一旋钮 A/B 删掉 16% 的 cue、
    23 个区域清空 11 个——里面有台标 `CCTV2` 和一条像人名条的 `李浩`。
    主轨感觉不到（它的 UI 行出现几千次），**用途 1 要的正是那些小区域**。
    默认 20：`Auto` 在 f3 出现 5,714 次、`大`/`New` 在 f5 约 700 次，离 20 都很远。

    实测（input3 整片，过同一个 match_ref 再对剧本）：
    命中 4,917 -> **4,934**，疑似真漏 97 -> **87**。
    f1/f2/f5 上这条规则**一行都不剔**（它们的常驻短行都够不着 0.5）。
    （注：f1 的产物仍然变了，但那是同一批改动里 `estimate_shift` 那条的效果，
    不是这条——两条一起改、只对了一次账，是我的疏忽。）

    **已知的假阳性**：真台词如果就是同一句短话、还占了过半的 cue，会被误删。
    写测试时踩到过（拿同一句台词复制 10 遍当样本，它自己被判成了 UI）。
    真实素材上不太可能——过半的 cue 说同一句 6 字以内的话，那本来也不像对话轨。

    **"名牌天然到不了一半"这句在整片上塌了**（2026-09-08，
    gamestream-first-look 报告）：`share` 的分母是**这个区域自己的 cue 数**，
    一个只在某几场戏里存在的小区域，里头任何一行都轻易过半；`min_support 20`
    在 3 小时素材上也拦不住一个次要角色。实测原神两场整片把
    `カチーナ` / `ムアラニ` / `ラウマ` 都当成常驻 UI 删了（一个区域掉 514 条 cue）。
    **主轨没被咬、`tracks.json` 不过滤，所以两个产物都没受影响**——是记账，不是急修。

    候选修法是 `min_onshare`（全片在场占比的地板，默认关）。
    **它不是免费的**：一次性探针 `probe_ui_burst.py` 量过，0.3 的地板在四部整片上
    只留下 `Auto`（yuka f3/f5）和 `Q`（gi2），把这条规则现在删的东西放过 97%——
    包括 docstring 上面记着有收益的那些。**要开先跑五部头对头。**
    """
    ui = persistent_ui_lines(segs, min_share, max_len, min_support, min_onshare, film_us)
    if not ui:
        return segs
    out = []
    for s, e, active in segs:
        keep = [r for r in active if r.text.strip() not in ui]
        if keep:
            out.append((s, e, keep))
    return out


BODY_MIN_CHARS = 3
"""`--srt-mode body` 里能当切点的行至少几个**内容字**——owner 三档里"有内容 ≥3 字"那条线，不是新常数。"""


def body_segments(rs: list[Run], end_tol_us: int = 500_000) -> list[tuple[int, int, list[Run]]]:
    """**按正文行切段**（`--srt-mode body`）：只有正文行的首尾是切点，别的行按时间重叠挂进去。

    `segment` 在**每个** run 的首尾都切一刀，于是这些不是正文的东西也在切 cue
    （game-text-corpus：整场 zzz 相邻 cue 近一半正文逐字相同；hsr 5591 s 一句切成五条）：
    * 名牌被 OCR 每帧读成不同的样子（`・・ビリー・・` / `ビリー` / `• ビU— •`），一变就是一条新 run；
    * ▼ 翻页提示半秒一闪（读成 `回` / `A`）；
    * 打字机首帧的 1–2 字碎片。

    **正文行（锚）** = 内容 ≥ `BODY_MIN_CHARS` 字、而且同一时刻下方没有另一条有内容的行的 run——
    名牌、称号、两行正文的上一行都在它上方，不当锚；碎片和闪烁的提示内容不够，也不当锚。
    * 锚的起点往前放宽到"和它重叠、比它先出现的非锚行"的最早起点（打字机先出名牌 / 先出第一行，
      cue 起点仍是首字出现），但不越过同一区域里上一个锚的终点（同一个说话人连说两句时，名牌那条 run 横跨两句）；
    * 一个锚都不沾的非锚行（没有名牌的纯标点台词、孤立的 UI）用它自己的首尾切；
    * 段里的行 = 在这段里出现过的全部 run（不要求盖满整段：闪烁的提示只盖住一部分），按 `row_order` 排；
    * 相邻两段锚集合相同、或者前一段的锚是后一段的真子集（又打出来一行）就接上。
    """
    if not rs:
        return []
    order = sorted(rs, key=lambda r: (r.t_start, r.t_end))
    content = {id(r) for r in rs if evalkit.content_len(r.text) >= BODY_MIN_CHARS}

    # 时间上重叠的对：扫一遍，活动表里只留还没结束的
    pairs: list[tuple[Run, Run]] = []
    active: list[Run] = []
    for r in order:
        active = [o for o in active if o.t_end > r.t_start]
        pairs.extend((o, r) for o in active)
        active.append(r)

    def height(r: Run) -> float:
        return r.box[3] - r.box[1]

    def dur(r: Run) -> int:
        return max(1, r.t_end - r.t_start)

    def ov(a: Run, b: Run) -> int:
        return min(a.t_end, b.t_end) - max(a.t_start, b.t_start)

    above: set[int] = set()                      # 同一时刻下方有另一条有内容的行
    fragment: set[int] = set()                   # 同一行里被一条更长久的有内容行盖住的碎片
    overlaps: dict[int, list[Run]] = {}
    for a, b in pairs:
        overlaps.setdefault(id(a), []).append(b)
        overlaps.setdefault(id(b), []).append(a)
        # 还要 x 上有重叠：名牌居中压在正文上方、两行正文上下对齐；角落里的 UID 水印不算"在上方"
        if id(a) in content and id(b) in content and min(a.box[2], b.box[2]) > max(a.box[0], b.box[0]):
            h = 0.5 * min(height(a), height(b))
            # 两行要**互相**陪着至少各自一半时长才算"上一行 / 下一行"（重叠 ≥ 较长那条的一半）：
            # ▼ 翻页提示被读成 `un2ra` 只闪 1 秒，不许把正文判成上一行；
            # 常驻的 HUD（`Lv.90`、血条 `19000/24829`）压在正文下面几十秒，也不许——
            # body 臂第二轮 gi1 丢的 8 行里 4 行是这个：正文被判成上一行、HUD 当了锚，正文盖不住半段被丢掉
            if a.cy < b.cy - h:
                if ov(a, b) >= 0.5 * max(dur(a), dur(b)):
                    above.add(id(a))
            elif b.cy < a.cy - h:
                if ov(a, b) >= 0.5 * max(dur(a), dur(b)):
                    above.add(id(b))
            else:
                # 同一行：短命的那条是碎片（HUD 数字 `15m` 在正文行上闪一下），不当锚
                short, long_ = (a, b) if dur(a) < dur(b) else (b, a)
                if dur(short) < 0.5 * dur(long_):
                    fragment.add(id(short))
    anchors = sorted((r for r in rs if id(r) in content and id(r) not in above and id(r) not in fragment),
                     key=lambda r: (r.t_start, r.t_end))
    is_anchor = {id(r) for r in anchors}

    def squash(s: str) -> str:
        return "".join(s.split())

    def attached(o: Run, a: Run) -> bool:
        """非锚行 o 和锚 a **真的同屏过**：重叠至少占 o 自己时长的一半。只是首尾挨着（run 末帧补的那半个
        采样间隔）不算——否则一句 1–2 字的短台词（`はあ…` / `うむ…`）会被下一句的锚吞进去
        （body 臂第一版在 gi1 丢的 6 行里有 4 行是这个形状）。"""
        return ov(o, a) >= 0.5 * dur(o)

    # **打字机首帧**：o 正好在锚 a 出现的那一刻消失，而且 a 的正文以 o 开头（hsr 5591 s 的 `知`）。
    # 两者时间上不重叠，进不了 `overlaps`，单独按"a 的起点 = o 的终点"找
    by_start: dict[int, list[Run]] = {}
    for a in anchors:
        by_start.setdefault(a.t_start, []).append(a)
    lead_of: dict[int, list[int]] = {}           # 锚 id -> 它的打字机首帧的起点
    is_lead: set[int] = set()
    for o in rs:
        if id(o) in is_anchor:
            continue
        for a in by_start.get(o.t_end, ()):
            if o.t_start < a.t_start and squash(a.text).startswith(squash(o.text)):
                lead_of.setdefault(id(a), []).append(o.t_start)
                is_lead.add(id(o))

    # 各自成句的非锚行（没挂在任何锚上：没有名牌的短台词）。锚的起点往前放宽时不许越过它们——
    # 名牌横跨 `はあ…` 和下一句时，没有这道闸，下一句的起点会被名牌一路拉回到 `はあ…` 的开头
    solo = [r for r in rs if id(r) not in is_anchor and id(r) not in is_lead
            and not any(id(o) in is_anchor and attached(r, o) for o in overlaps.get(id(r), ()))]
    barriers = sorted(anchors + solo, key=lambda r: r.t_start)
    start_of: dict[int, int] = {}
    j, reach = 0, -1
    for a in anchors:
        while j < len(barriers) and barriers[j].t_start < a.t_start:
            reach = max(reach, barriers[j].t_end)
            j += 1
        floor = min(reach, a.t_start)
        early = [o.t_start for o in overlaps.get(id(a), ())
                 if id(o) not in is_anchor and o.t_start < a.t_start and attached(o, a)]
        early += lead_of.get(id(a), [])
        start_of[id(a)] = max(min([a.t_start, *early]), floor) if early else a.t_start

    bounds = {t for a in anchors for t in (start_of[id(a)], a.t_end)}
    bounds |= {t for r in solo for t in (r.t_start, r.t_end)}
    span = {id(r): (start_of.get(id(r), r.t_start), r.t_end) for r in rs}

    segs: list[tuple[int, int, list[Run], frozenset[int]]] = []
    run_of = {id(r): r for r in rs}
    bs = sorted(bounds)
    by_start = sorted(rs, key=lambda r: span[id(r)][0])
    k, live = 0, []
    for s, e in zip(bs, bs[1:]):
        while k < len(by_start) and span[id(by_start[k])][0] < e:
            live.append(by_start[k])
            k += 1
        live = [r for r in live if span[id(r)][1] > s]
        here = list(live)
        if not here:
            continue
        key = frozenset(id(r) for r in here if id(r) in is_anchor)
        # 没有锚的段不往后并：`frozenset() < {锚}` 恒成立，并了就把前面那句短台词吞进下一句。
        # "又打出来一行"要求新锚和旧锚**一起消失**（容差同 group_cues：同一条字幕的各行一起消失）——
        # 常驻 HUD（`Lv.90`）当锚挂几十秒，正文一出现就被当成"HUD 那条字幕的第二行"并进去；
        # 两个对话框前后脚交替（zzz 2899 s）也会这样并成一条
        grows = (segs and segs[-1][3] and segs[-1][3] < key
                 and all(abs(run_of[i].t_end - run_of[j].t_end) <= end_tol_us
                         for i in segs[-1][3] for j in key - segs[-1][3]))
        if segs and segs[-1][1] == s and (segs[-1][3] == key or grows):
            ps, _, prev, _ = segs[-1]
            seen = {id(x) for x in prev}
            segs[-1] = (ps, e, prev + [r for r in here if id(r) not in seen], key)
            continue
        segs.append((s, e, here, key))

    def keep(r: Run, s: int, e: int) -> bool:
        # 非锚行盖不住这段一半的不挂进来：闪烁的 ▼ 提示、打字机首帧碎片、HUD 数字（`15m`）、
        # 名牌 / 水印被读成几种的抖动版（`NICAL` / `AICAL`、`•• •`）。第一版只滤 1–2 字的，
        # 3 字以上的抖动版全挂进来，cue 开头垫了 4 行，匹配器的读法（最多去掉开头 3 行）够不着正文。
        # 名牌、两行正文的上一行、常驻水印都盖满整段，照留；没有锚的段里它自己就是整段，照留
        if id(r) in is_anchor:
            return True
        inside = min(r.t_end, e) - max(r.t_start, s)
        # 盖不住半段、但自己**整条都在这段里、读到过 ≥3 次**的也留：那是一句真的短行
        # （名牌底下的 `…！！！`，body 臂第二轮 gi1 11064 s）；闪烁的 ▼ / HUD / 抖动版只有 1–2 次观测
        return inside >= 0.5 * (e - s) or (inside >= 0.9 * (r.t_end - r.t_start) and r.n_obs >= 3)
    def dedupe(lines: list[Run]) -> list[Run]:
        # 同一段里文本逐字相同的几条 run 只留活得最久的那条：同一行字被 OCR 断成几条 run 时全挂进来，
        # cue 里就是 `1つ教えてあげるね` 连写三遍，匹配器的相似度被拖到门下（body 臂第三轮 hsr 5318 s 丢的那一行）
        best: dict[str, Run] = {}
        for r in lines:
            k = squash(r.text)
            if k not in best or dur(r) > dur(best[k]):
                best[k] = r
        return list(best.values())

    return [(s, e, row_order(dedupe([r for r in lines if keep(r, s, e)] or lines)))
            for s, e, lines, _ in segs]


def cue_segments(rs: list[Run], mode: str = "cue", min_seg_us: int = 150_000,
                 end_tol_us: int = 500_000, min_overlap: float = 0.5, join: bool = False
                 ) -> list[tuple[int, int, list[Run]]]:
    """**切分**：一条轨上的 run 分成 cue。mode 四档：

    cue（按一起消失分条）/ segment（每个首尾边界都切，默认）/ body（只按正文行切，见 `body_segments`）/
    raw（一条 run 一条）。`join`（`--cue-join on`）只对 segment 生效：末尾再做一遍同句合并（`join_same_sentence`）。

    抽出来是因为切分的结果现在要**落进 JSON**（`docs/architecture/artifacts.md`）：
    导出器只投影、不重新切——切两遍就会有两份不一样的"条数"，而这个项目
    在"一条是什么"上已经栽过一次 30 小时的账。
    **段里 run 的顺序就是导出时的行序**，别在别处重排。
    """
    if mode == "cue":
        return [(s, e, a) for s, e, a in group_cues(rs, end_tol_us, min_overlap)
                if e - s >= min_seg_us]
    if mode == "segment":
        segs = merge_growing_segments(drop_slivers(segment_runs(rs), min_seg_us))
        return join_same_sentence(segs, end_tol_us) if join else segs
    if mode == "body":
        return drop_slivers(body_segments(rs, end_tol_us), min_seg_us)
    return [(r.t_start, r.t_end, [r]) for r in sorted(rs, key=lambda r: r.t_start)]


def split_hidden(rs: list[Run], hide: set[int], run_idx: dict[int, int], seg_kw: dict):
    """一条区域轨的切分：`hide` 里的 run（框位 UI、振り仮名——导出时都要剔的）和正文**分开切**，再按时间排回一份。
    返回 `(全部段, 只有正文的段)`；`hide` 为空时两者是同一份 `cue_segments(rs)`。"""
    if not hide:
        segs = cue_segments(rs, **seg_kw)
        return segs, segs
    body = [r for r in rs if run_idx[id(r)] not in hide]
    hid = [r for r in rs if run_idx[id(r)] in hide]
    body_segs = cue_segments(body, **seg_kw)
    if not hid:
        return body_segs, body_segs
    return sorted(body_segs + cue_segments(hid, **seg_kw), key=lambda s: (s[0], s[1])), body_segs


DROPPED_UI = run_srt.DROPPED_UI                 # 同一个 list 对象：run_srt 往里 append，main() 从这里读
write_srt_segments = run_srt.write_srt_segments
clear_stale_srts = run_srt.clear_stale_srts


def write_srt(path: Path, rs: list[Run], mode: str = "cue", min_seg_us: int = 150_000,
              end_tol_us: int = 500_000, min_overlap: float = 0.5,
              ui_share: float = 0.5, ui_min_support: int = 20,
              ui_min_onshare: float = 0.0, film_us: int = 0) -> int:
    """切分 + 判 UI + 投影，一步到位。**`build_tracks` 自己不用它**（它要把中间结果
    落进 JSON），留给回抠和探针这类"手上只有 run"的调用方。"""
    segs = cue_segments(rs, mode, min_seg_us, end_tol_us, min_overlap)
    ui = persistent_ui_lines(segs, ui_share, min_support=ui_min_support,
                             min_onshare=ui_min_onshare, film_us=film_us)
    return write_srt_segments(path, segs, ui)


SAMPLED_FULL_SHARE = 0.9
"""`sampled_full`：读到的内容字数达到最终文本的这么多，就算"全字出现"。留 10% 给 OCR 在最后几个字上的抖动。"""


def sampled_full(r: Run) -> int | None:
    """打字机"全字出现"的**采样级**估计：第一票内容字数达到最终文本 `SAMPLED_FULL_SHARE` 的那个观测时刻。

    和事件的 `t_full` 不是一回事：`t_full` 只由回抠（refine_boundaries）逐帧量出来填，**不许拿别的顶替**
    （owner 指示 4 的打字机三态）；这个是采样间隔精度的观测值，另起一个字段 `t_full_sampled`，
    给"导出时还原打字机效果"这类不需要逐帧精度的用途（字幕稿的打字机判定，`flowocr.output.script.effect_times`）。
    首票就读全了 = 没有打字过程，记成 `t_start`；滚动拼出来的文本（每票都只有一段）永远够不着，记 None。"""
    if not r.times or len(r.times) != len(r.texts):
        return None
    final = evalkit.content_len(r.text)
    if final == 0:
        return None
    for t, s in zip(r.times, r.texts):
        if evalkit.content_len(s) >= SAMPLED_FULL_SHARE * final:
            return min(max(t, r.t_start), r.t_end)
    return None


def event_dict(r: Run, eid: int, region: int, with_texts: bool = False) -> dict:
    """一条 run 落成 JSON 里的一个**事件**（`output-format-plan（已归档）`）。

    `t_full` 这里恒为 `None`：全字出现的时刻只有回抠量得出来，
    **不许拿 `t_start` 顶替**（owner 指示 4 的打字机三态，顶替就等于把三态压成两态）。
    """
    flags = []
    if r.moving:
        flags.append("moving")
    if r.scrolling:
        flags.append("scrolling")
    if r.clip_start is not None:
        flags.append("clipped_start")        # 自选范围的时间关闭段截的，不是文字自然出现（ocr-regions 计划）
    if r.clip_end is not None:
        flags.append("clipped_end")
    if getattr(r, "nameplate", False):
        # **名牌**：框位侧的判据挑的（`tools/uigate.pick_nameplates`，ui-gate 计划）。
        # 和 `ui_filtered` 一样是**打标不删**——用途 1 要留着、用途 2 可以拿它并进正文。
        flags.append("nameplate")
    ev = {"id": eid, "region": region, "t_start": r.t_start, "t_full": None,
          "t_end": r.t_end, "box": r.box, "text": r.text, "conf": r.conf,
          "n_obs": r.n_obs,
          # 独立票：框级复用把上一帧的文本原样搬过来，那不是一次新读数（audit-4 C4）。
          # **全是沿用票时就是 0**，不许回退成 `n_obs`——那正好把"这条一次都没被重新
          # 识别过"这件事抹掉，而它恰恰是下游最该知道的。（投票时那个"没有独立票就
          # 退回全体"的回退是另一回事，那是为了不把文本清空。）
          "n_independent": sum(1 for x in r.reused if not x),
          "flags": flags}
    full = sampled_full(r)
    if full is not None:
        ev["t_full_sampled"] = full
    if r.moving:                       # 轨迹**只给 moving 的**（owner 2026-09-09）
        ev["boxes"] = [[t, b] for t, b in r.boxes]
    if with_texts:
        ev["texts"] = [[t, bool(u)] for t, u in zip(r.texts, r.reused)]
    return ev


PANEL_MIN_LINES = 5
PANEL_MIN_H = 0.15
"""**面板样**（对话记录 / 列表式长面板）的判据：一条 cue 有 ≥ `PANEL_MIN_LINES` 行、
且框并集高过画面高的 `PANEL_MIN_H`。

为什么要这个标（matcher 计划）：星铁的对话记录面板一屏列着好几句**旧台词**，
喂给匹配器时文本是真的、**时间是错的**（那句话是早先说的）。它在 hsr 上被聚类并进了
`dialogue-or-caption`，**按 label 剔不掉**；按行数单独剔也不行（"名牌 + 两行字幕"就是 3 行）。

阈值是量出来的（a1-mask-reuse 实验里的 `panel_features.py`，hsr 整场）：

| 判据 | 可疑认领背后的 cue | 换喂入新增命中背后的 cue | 主轨正文 cue |
| --- | ---: | ---: | ---: |
| 行数 ≥4 | 22/28 | 9/70 | 35/485 |
| **行数 ≥5 且框高 > 0.15** | **20/28** | 6/70 | **4/485** |
| 行数 ≥6 且框高 > 0.15 | 17/28 | 3/70 | 2/485 |

**只打标、不改任何投影**（artifacts.md 第 1 条：过滤不删事件、判定写进 JSON）：
现有 SRT / ASS 逐字节不变，`scriptmatch --feed` 那边按这个标取舍。"""


def panel_like(evs: list[dict], height: int) -> bool:
    """这条 cue 是不是"面板样"（见 `PANEL_MIN_LINES` / `PANEL_MIN_H`）。只看形状，不看文本。"""
    if len(evs) < PANEL_MIN_LINES or height <= 0:
        return False
    y0 = min(e["box"][1] for e in evs)
    y1 = max(e["box"][3] for e in evs)
    return (y1 - y0) / height > PANEL_MIN_H


def cues_from_segments(track_id: str, segs: list[tuple[int, int, list[Run]]],
                       eid_of: dict[int, int], events: list[dict] | None = None,
                       height: int = 0, tol: int | None = None) -> list[dict]:
    """把切分结果记成 cue 表。`events` 的顺序**就是**导出时的行序。

    给了 `events`（全量事件表）和 `height`（画面高）时顺便打 `panel_like` 标（见上，只数会导出的行）。
    给了 `tol`（同句合并开着）时记 `seg_hide` 藏的成员：`hide`（投影时不给）和其中的 `partial`（半截行，全部文字的视图也不给）。
    """
    out = []
    for i, (s, e, a) in enumerate(segs):
        ids = [eid_of[id(r)] for r in a]
        cue = {"id": f"{track_id}c{i:05d}", "t_start": s, "t_end": e, "events": ids}
        hide, partial = seg_hide(s, e, a, tol) if tol is not None else (set(), set())
        if hide:
            cue["hide"] = [eid_of[id(r)] for r in a if id(r) in hide]
        if partial:
            cue["partial"] = [eid_of[id(r)] for r in a if id(r) in partial]
        if events is not None and panel_like([events[eid_of[id(r)]] for r in a if id(r) not in hide], height):
            cue["panel_like"] = True
        out.append(cue)
    return out


def within_regions(segs: list[tuple[int, int, list[Run]]], placed: set[int]
                   ) -> tuple[list[tuple[int, int, list[Run]]], int]:
    """非区域轨（并带主轨、名牌轨）的段只留**区域轨里已有**的 run，空段丢掉；返回 `(段, 拿掉的行数)`。

    非区域轨是区域轨的另一种投影（`tracksio.validate` 的子集不变量）。可切分本身会按上下文筛行：
    `--srt-mode body` 的 `keep` / 去重、各模式的 `drop_slivers` 看的是**这一批 run 里**的锚和段，
    同一条 run 在自己的区域里被筛掉、在并带后的另一批里却可能留下——没有这道闸，产物写到最后一步才被 schema 拦下
    （2026-10-05：body 模式 12 臂里崩了 2 臂）。区域轨说了算：它筛掉的，投影里也不出现。"""
    out, dropped = [], 0
    for s, e, a in segs:
        keep = [r for r in a if id(r) in placed]
        dropped += len(a) - len(keep)
        if keep:
            out.append((s, e, keep))
    return out, dropped


def text_ui_lines(segs, args, ui_kw: dict) -> set[str]:
    """文本那条常驻 UI 判据（逐轨）——**`--ui-gate footprint` 时真的关掉它**。

    ⚠ 这个包装存在的唯一理由是审计七：`--ui-gate` 的帮助里写着
    "`footprint` = 只用框位那条"，而区域轨和并带主轨都**无条件**调用
    `persistent_ui_lines`，于是 `footprint` 和 `both` 在现存产物上
    `events` / `tracks` / `regions` 逐键相同——**消融臂塌成了默认臂**。
    正是 `ocr_ab.sh` 那次"默认一翻、空参数塌成同一条臂"的同形。
    """
    if args.ui_gate == "footprint":
        return set()
    return persistent_ui_lines(segs, args.ui_share, **ui_kw)


def ui_footprints(runs: list[Run], frame_us: int, args, observable=None) -> set[int]:
    """**框位 + 时间占比**的常驻 UI 门，返回判中的 run 的下标。

    和现行的 `persistent_ui_lines` 两处不同（ui-gate 计划）：
    ①**分母是时间**（在屏时长 ÷ 全片），不是 cue 数——读法变了它也不动；
    ②**按框位**，不按文本——于是每秒都在变的 HUD（`01:17:46`、`388`）也抓得到，
      而 `max_len <= 6` 那条硬截断挡住的长文本 UI（`UID: 1300174139`）也够得着。

    判据在 `flowocr.analyze.uigate`（判据只有一份）：静态 UI = 翻动小 + 内容字短；
    动态 HUD = 数字多且短。两档都要求在屏久、框宽稳、**不是名牌**、**不在正文带里**。
    """
    if args.ui_gate == "text":
        return set()
    # `w = n_obs`：一条 run 合并了多少次观测——`churn` 的门是在 obs 那一侧标定的
    items = [uigate.Item(tuple(r.box), r.t_start, r.t_end, r.text, key=i, w=max(1, r.n_obs))
             for i, r in enumerate(runs)]
    if not items:
        return set()
    # ⚠ **`step` 是采样间隔，不是 run 时长的中位数**（审计七）：原来传中位数，
    # 于是 `occupancy` 把每条短 run 都垫到中位数那么长，同一段占用拆得越碎占比越高。
    # run 的 `t_end` 本来就已经加过一个 `frame_us`，所以这里补齐是恒等的。
    step = max(1, frame_us)
    # ⚠ **分母是这份产物实际覆盖的跨度**，不是整片时长：窗口产物（`--start/--end` 切出来的
    # 20 分钟）上，拿 6.2 h 当分母会把 `Auto` 的 74.5% 算成 **4.0%**，门当然接不住——
    # 而 `Auto` 污染主轨那次翻车（293/595 条 cue）要接的正是它。整片产物上两者相同。
    dur_us = max(max(i.t1 for i in items) - min(i.t0 for i in items), 1)
    fps = uigate.footprints(items)
    # 自选范围（ocr-regions 计划）：占比的分母是**这个框位看得见的时长**，关闭时段不算"UI 缺席"
    t_lo = min(i.t0 for i in items)
    ms = [(f, uigate.measure(f, dur_us if observable is None else max(1, observable(f.box, t_lo, t_lo + dur_us)), step))
          for f in fps]
    cands = [{"box": f.box, "n": m["n"]} for f, m in ms
             if m["share"] >= 0.02 and m["clen"] >= uigate.BODY_CLEN and m["dig"] < 0.3]
    bands = uigate.bands(cands)
    out: set[int] = set()
    # ⚠ **名牌标是唯一的事实来源**：这里的带是全片算的，而名牌标是**按时间窗**挑的（`pick_nameplates`），
    # 两边各判一次就会打架——实测 wuwa-s2 上 UI 门咬掉了 11 条打了名牌标的 run（`ヴェリーナ` 这种）。所以直接认那个标、不重判；
    # 但**只保护名牌标所在的那几个窗**（`uigate.np_window`，和挑名牌同一种分窗；`t_lo` 和 `pick_nameplates` 的 `lo` 都是全部 run 的最早起点；按 run 的起点归窗，名牌 run 跨进下一个窗时，那个窗里同一框位没打标的 run 不受保护——和挑名牌的归窗约定一致）：同一个位置在别的时段可以是 UI。原来"框位里有一条名牌 run 就整个放行"，
    # 一条误标就豁免一整个位置——gi1 左上角「オート再生」4 条误标放走 165 条、gi2「再生中」3 条放走 114 条（审计七 §3.8）。
    win_us = max(1, int(args.nameplate_window * 1e6))
    for f, m in ms:
        m.update(uigate.relate(f.box, m["clen"], bands))
        np_wins = {uigate.np_window(it.t0, t_lo, win_us) for it in f.items if getattr(runs[it.key], "nameplate", False)}
        if (m["share"] < args.ui_fp_share or m["wvar"] > args.ui_fp_wvar
                or uigate.is_nameplate(m) or m.get("in_band")):
            continue
        static_ui = m["churn"] <= args.ui_fp_churn and m["clen"] <= args.ui_fp_clen
        dyn_hud = m["dig"] >= 0.3 and m["clen"] <= 6
        if static_ui or dyn_hud:
            out |= {it.key for it in f.items if uigate.np_window(it.t0, t_lo, win_us) not in np_wins}
    return out


RUBY_H = (0.3, 0.75)
"""注音字高 / 正文字高的范围。日文注音一般是正文的一半（wuwa-s1 `こんれいいん` 28 px 对 `今令尹…` 40–44 px）；两侧留宽，靠别的几条收紧。"""


def ruby_runs(runs: list[Run]) -> set[int]:
    """⚠ 观察项（09-26 审计定级：中度过拟合嫌疑，defaults.md 1.30）：判据有两条是按单个误标补的，常数没扫。
    10-05 在没参与调参的原神两场上摆帧核过（defaults.md 1.30 的留出检验）：标出的 143 条里 140 条是真注音；第 4 条因此改过。

    **振り仮名**：紧贴在一条含汉字的正文行上方、字高约一半、全是假名的短 run，返回下标（followups「振り仮名当成行」）。

    不标的话注音是独立事件：主轨 SRT 里排在正文前单独成行、叠加里单独一小块、回抠后还会拿到几秒"打字机"。判据只看几何和文字系统，
    不看素材：
      1. 文本全是假名（去掉空白和 `ー・`），2–16 字（单个假名多是读残的碎片：wuwa-s2 面板里的 `を`、gi-s1 的 `ヤ`）；
      2. 同时在场（时间重叠 ≥ 自己时长的一半）的某条正文 run 含汉字，字高比在 `RUBY_H` 里；
      3. 注音的下沿落在正文上沿上下（上方 0.8 个注音字高到下方 0.5 个之间），横向整个落在正文的 x 范围里（两侧各留一个注音字高）；
      4. 中心和一条**正文长度**的行（内容字 ≥ `uigate.BODY_CLEN`，名牌判据里"正文"的同一个门）对齐（差不到半个注音字高）的不标：
         那是居中压在对话上方的名牌（zzz-s1 `リン`）。正文是短标签时居中正是常态——地图地名的注音整词居中
         （「せいかきょうぎじょう」/「聖火競技場」），原来不分长短一律不标，留出的原神两场漏了 54 条真注音（defaults.md 1.30）；
      5. 注音横向覆盖的正文字符（按正文框等宽估）里汉字过半：真注音横跨的就是它注的那几个汉字（「けんてんかまえ」下是「懸天の構」），
         片假名的名字 / 称号 / 任务列表项恰好落在一行含汉字的正文上方时，底下多半是假名（gi2-w1 名牌「バーテンダー」下是「すまない、」，
         hsr `スターゲイザー`；peer 审查 edb5cc4 第 1 条）。
    """
    def kana_only(t: str) -> bool:
        s = "".join(ch for ch in t if not ch.isspace() and ch not in "ー・")
        return 2 <= len(s) <= 16 and all(script_of(ch) == "kana" for ch in s)

    cands = [i for i, r in enumerate(runs) if kana_only(r.text)]
    if not cands:
        return set()
    bucket_us = 10_000_000
    bodies: dict[int, list[int]] = {}
    for j, r in enumerate(runs):
        if any(script_of(ch) == "han" for ch in r.text):
            for k in range(r.t_start // bucket_us, r.t_end // bucket_us + 1):
                bodies.setdefault(k, []).append(j)
    out: set[int] = set()
    for i in cands:
        r = runs[i]
        dur = max(1, r.t_end - r.t_start)
        seen: set[int] = set()
        for k in range(r.t_start // bucket_us, r.t_end // bucket_us + 1):
            for j in bodies.get(k, ()):
                if j in seen or j == i:
                    continue
                seen.add(j)
                b = runs[j]
                if min(r.t_end, b.t_end) - max(r.t_start, b.t_start) < 0.5 * dur:
                    continue
                if not (RUBY_H[0] <= r.h / max(1.0, b.h) <= RUBY_H[1]):
                    continue
                if not (b.box[1] - 0.8 * r.h <= r.box[3] <= b.box[1] + 0.5 * r.h):
                    continue
                if r.box[0] < b.box[0] - r.h or r.box[2] > b.box[2] + r.h:
                    continue
                if abs(r.cx - b.cx) < 0.5 * r.h and evalkit.content_len(b.text) >= uigate.BODY_CLEN:
                    continue
                # 注音压在汉字上：按正文框等宽估出注音横向覆盖的是正文的哪几个字，其中汉字要过半
                n = len(b.text)
                cw = max(1.0, b.w) / max(1, n)
                k0, k1 = int((r.box[0] - b.box[0]) / cw), int((r.box[2] - b.box[0]) / cw)
                under = [ch for ch in b.text[max(0, k0):max(0, k1) + 1] if not ch.isspace()]
                if not under or 2 * sum(script_of(ch) == "han" for ch in under) < len(under):
                    continue
                out.add(i)
                break
            if i in out:
                break
    return out


RUN_SPECIFIC = ("outdir", "tag", "viewport")
"""每次都不一样、不进默认指纹的选项（`viewport` 是这段录像的游戏画面位置）。"""


def defaults_fingerprint(ap: argparse.ArgumentParser) -> tuple[str, str]:
    """当前**默认值**的指纹：`(12 位 hash, 一行 k=v)`。

    为什么要它（audit-5）：`SUFFIX` 为空的那条臂跑的是"当前默认"，
    而默认是会变的。改默认那次直接把 A/B 的基准臂**覆盖**掉了，三处文档里
    引的数从产物里数不回来。日志里留下这一行，"默认变了"至少是可见的。

    列表**不手挑**：手挑的清单一定会漏掉下一个新旋钮。直接遍历 parser
    （argparse 没有公开的"列出所有选项"接口，只能读 `_actions`）。
    """
    items = sorted(f"{a.dest}={a.default}" for a in ap._actions
                   if a.option_strings and a.dest not in ("help", "print_defaults")
                   and a.dest not in RUN_SPECIFIC)
    line = " ".join(items)
    return hashlib.sha1(line.encode("utf-8")).hexdigest()[:12], line


# `DIRTY_PATHS` / `git_head` / 指纹的实现 2026-09-21 搬进了 `flowocr.provenance`（文件头讲了为什么并成一份）；
# 这里只留带默认表的入口，`gamescript` / `scriptmatch` / `refine_boundaries` 仍借 `bt.code_fp` / `bt.git_head` 用。

CODE_FP_FILES = ("src/flowocr/analyze/build_tracks.py", "src/flowocr/artifacts/evalkit.py", "src/flowocr/artifacts/obsio.py", "src/flowocr/artifacts/srtio.py", "src/flowocr/artifacts/tracksio.py",
                 "src/flowocr/analyze/uigate.py", "src/flowocr/analyze/align.py", "src/flowocr/output/run_srt.py",
                 "src/flowocr/provenance.py", "src/flowocr/textfold.py",
                 # ⚠ 下面三个是**函数里懒 import** 的（自选范围的切点 / 可观察时长；`--cluster learned` / `lines`），同样决定产物——
                 # 2026-09-23 审计前守卫只认顶格 import，两个都漏了。`flowocr.extensions` 是加载器，用户匹配器的身份记在 provenance 的 `matcher`
                 "src/flowocr/extract/regions.py", "src/flowocr/analyze/slot_learned.py", "src/flowocr/analyze/slot_lines.py",
                 "src/flowocr/analyze/slot_modes.py", "src/flowocr/analyze/pair_features.py",   # slot_lines / slot_learned 顶格 import 的；模型另记在 cluster_fp
                 # `--refine auto`（默认）在建轨末尾结算回抠（`settle_refine` 里懒 import）；回抠自己的代码指纹
                 # 另记在产物的 `boundary_refined.code_fp`（refine_boundaries.CODE_FP_FILES）
                 "src/flowocr/analyze/refine_boundaries.py", "src/flowocr/output/export.py")
"""产出 `-tracks.json` 的代码：本文件 + 它 import 的本地模块，**仓库相对路径**。自检拿 import 行核这张表
（顺着 shim 找真实现）。`provenance.py` 在表里是因为它被直接 import——它一变全部产物的指纹都变，
这是诚实的：写 provenance 的代码就是产物的一部分。"""


REFINE_SALVAGE_END = True
"""回抠结算救回被回补作废、窗口凑齐的结尾证据（`--refine-salvage-end`，defaults.md 1.32）。
建轨、`refine_boundaries` 的两个入口、开发探针都读这一个值，离线探针对账的才是生产默认那条路。"""


def code_fp(files: tuple[str, ...] = CODE_FP_FILES, root: Path | None = None) -> str:
    """`flowocr.provenance.code_fp` 带上默认表；`root` 只给自检用。"""
    return _code_fp(files, root)


def cache_stale(doc: dict) -> str | None:
    """评测驱动判建轨缓存能不能复用：产它的代码和现在的不一样时返回一句原因，一样就是 None。
    三份指纹都要对上——本文件的表（`code_fp`）、`--cluster learned` / `lines` 另记的（`cluster_fp`）、
    建轨末尾结算回抠的（`refine_boundaries.refine_code_changed`）。原来驱动只比 build_tracks.py 的 mtime，
    默认行为放在 tracksio / refine_boundaries / slot_lines 里的改动会被静默复用（2026-09-26 审计）。
    参数对不对另由驱动比 argv（`tracksio.build_args`）。"""
    prov = doc["provenance"]
    now = code_fp()
    if prov.get("code_fp") != now:
        return f"建轨代码指纹对不上：产物是 {prov.get('code_fp')}，现在是 {now}"
    if prov.get("cluster_fp"):
        from flowocr.analyze import slot_learned
        kind = (prov.get("args") or {}).get("cluster") or "learned"
        cur = slot_learned.fingerprint(kind)
        if prov["cluster_fp"] != cur:
            return f"--cluster {kind} 的指纹对不上：产物是 {prov['cluster_fp']}，现在是 {cur}"
    from flowocr.analyze import refine_boundaries as RB
    return RB.refine_code_changed(doc)


def provenance(args, meta: dict, main_srt: str = "", main_track: str = "",
               main_cues: int = 0) -> dict:
    """"这是谁、用哪一版代码、什么参数产的"。**并进 `-tracks.json`，不再单独一个文件**
    （`output-format-plan（已归档）`：一级产物只有一个）。

    存在的理由（methodology-audit-2 报告）：09-06 那一夜改了三次 `build_tracks`，
    最终结果落在临时目录，而规范目录 `out/yuka-f1..f4` 留着改动前的产物；
    `h2h_report` 只按文件名 glob，于是**照着文档的复现命令跑，打出来的是另一张表**
    （而且更好看）。产物自己带上版本，下游才有得校验。

    `argv` 记的是**这次命令行上真正打了什么**（不是生效值）。
    `head2head.sh` 拿它和这一趟的 `BUILD_ARGS` 比：不比就会出
    methodology-audit-3 报告那个坑——同一个 `SUFFIX` 换了旋钮再跑，
    缓存只看 `build_tracks.py` 的 mtime，**产物照旧复用、旋钮被静默忽略**。
    """
    extra = {}
    if getattr(args, "cluster", "slots") in ("learned", "lines"):   # 这两条路另有代码（+ 模型），单独记指纹（默认臂的指纹不受它影响）
        from flowocr.analyze import slot_learned
        extra["cluster_fp"] = slot_learned.fingerprint(args.cluster)
    return {"tool": "build_tracks.py", "version": version(), "git_head": git_head(), "code_fp": code_fp(), **extra,
            # 路径经 `portable`（数据根下相对、用户目录下 `~/`）：产物会被分享，别带出用户名（release-plan F8）。
            # `argv` 和 `obs` 用同一个函数，`tracksio.build_args` 按字符串相等剥 obs 的那一格照样对得上
            "obs": portable(args.obs), "obs_mtime": Path(args.obs).stat().st_mtime,
            "video": portable(meta.get("video")), "sample_fps": meta.get("sample_fps"),
            "main_srt": main_srt, "main_track": main_track, "main_cues": main_cues,
            "argv": [portable(x) for x in sys.argv[1:]],
            # 游戏画面在录像里的位置（`--viewport`）：建轨不用它，匹配时没给 `--ctx viewport=` 就用它对版式文件（`tracksio.recorded_viewport`）
            **({"viewport": list(args.viewport_box)} if getattr(args, "viewport_box", None) else {}),
            # `ctx`：给匹配器的 `--matcher-ctx`；其中的 `game` 在用同一个匹配器匹配、没给 `--ctx game=` 时继承（`match.context_of`，《魔裁》的 `game=mocai` 只写一次）
            **({"matcher": {"spec": portable(args.matcher), "patch": args.matcher_patch, "overridden": args.matcher_overridden,
                            "ctx": {k: portable(v) for k, v in getattr(args, "matcher_context", {}).items()}}}
               if getattr(args, "matcher", None) else {}),
            "args": {k: portable(v) if isinstance(v, str) else v for k, v in sorted(vars(args).items()) if k not in ("matcher_context", "viewport_box")}}


OBS_NO_SIZE = "obs 缺 width / height（2026-10-06 删了按视频补探尺寸的回退）：这是很早的 obs，复用判据本来也拒，重跑 run_ocr2"
"""obs 头里没有画面尺寸时的报错（`build_tracks`、`cluster_layers` 共用）。"""


def effective_tag(args) -> str:
    """产物的标签：`--tag`，没给就是 obs 的文件名（去扩展名）。**只有这一处定**——
    匹配器补丁按它认游戏，产物文件名也用它；原来补丁那边拿的是 `args.tag`（None），
    于是 `hsr-s2.jsonl --matcher gametext` 不写 `--tag` 就静默丢了星铁的时间门（Codex 复审 P2 复现）。"""
    return args.tag or Path(args.obs).stem


def apply_matcher_patch(ap: argparse.ArgumentParser, argv: list[str] | None = None):
    """解析命令行，并把**匹配器的聚类补丁**按计划的顺序套进去：
    默认聚类规则 -> 匹配器补丁 -> 用户显式覆盖。

    做法：先解析一遍拿到 `--matcher` / `--matcher-ctx`，把补丁 `set_defaults` 成**新的默认值**，再解析一遍——
    于是命令行上显式给过的值自然压过补丁（argparse 的默认值只在没给时才用），
    "不能把 argparse 填入的默认值误当用户覆盖"这条也就不靠猜（计划）。

    冲突（补丁要 A、命令行显式给了 B）**明确报告**：打印出来 + 写进 provenance 的 `matcher.overridden`，不静默丢掉。
    补丁里出现解析器不认的键就当场报错——匹配器写错参数名不该悄悄没效果。
    """
    from flowocr import extensions
    args = ap.parse_args(argv)
    args.matcher_patch, args.matcher_overridden, args.matcher_context = {}, {}, {}
    if not args.matcher:
        return args
    ctx = {"tag": effective_tag(args)}   # 不写 --tag 时产物用 obs 文件名当标签，匹配器认游戏也得看同一个（复审 P2）
    for kv in args.matcher_ctx:
        if "=" not in kv:
            raise SystemExit(f"--matcher-ctx 要 K=V：{kv}")
        k, v = kv.split("=", 1)
        ctx[k] = v
    mod = extensions.load(args.matcher, "matcher")
    patch = dict(getattr(mod, "cluster_patch", lambda *_a, **_k: {})(ctx, None) or {})
    known = {a.dest for a in ap._actions if a.option_strings}
    unknown = sorted(set(patch) - known)
    if unknown:
        raise SystemExit(f"匹配器 `{args.matcher}` 的 cluster_patch 给了认不出的参数 {unknown}"
                         f"（键是参数名如 region_time_gate，不是 CLI 旗标）")
    if patch:
        ap.set_defaults(**patch)
        args = ap.parse_args(argv)
        args.matcher_patch = patch
        args.matcher_overridden = {k: getattr(args, k) for k, v in patch.items() if getattr(args, k) != v}
        kept = {k: v for k, v in patch.items() if k not in args.matcher_overridden}
        print(f"[匹配器补丁] {args.matcher}：{kept or '（无）'}"
              + (f"；命令行覆盖了 {args.matcher_overridden}" if args.matcher_overridden else ""), flush=True)
    args.matcher_context = {k: v for k, v in ctx.items() if k != "tag"}
    return args


def make_parser() -> argparse.ArgumentParser:
    """命令行解析器单独拿出来：只读 obs 重建某一层的工具（`flowocr.analyze.cluster_layers`）要的是
    **和管线同一份默认值**，不许在那边再抄一遍（抄的那份会在默认值翻转时静默落后）。"""
    ap = argparse.ArgumentParser(prog="flowocr-tracks")
    ap.add_argument("obs")
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--tag", default=None, help="产物文件名前缀，默认取 obs 文件名")
    # 补丁不是改全局默认，覆盖了哪几个会打印出来、也写进 provenance（matcher 计划）
    ap.add_argument("--matcher", default=None,
                    help="匹配器（内置名 / 模块名 / .py 路径，见 flowocr.extensions）。给了就取它的 `cluster_patch` "
                         "当默认聚类规则的补丁：生效顺序是内置默认 -> 匹配器补丁 -> 命令行显式给的")
    ap.add_argument("--matcher-ctx", action="append", default=[], metavar="K=V",
                    help="给匹配器 `cluster_patch` 的上下文，可多次；`tag` 自动带上（内置 gametext 按它认游戏）")
    ap.add_argument("--viewport", default=None, metavar="X,Y,W,H",
                    help="游戏画面在录像里的位置（像素，左上角和宽高）：主播布局、画中画、黑边这类游戏不占满录像的给它。"
                         "建轨不用，记进产物，匹配时对版式文件用（匹配的 --ctx viewport= 给了以那边为准）；不给匹配时推算")
    ap.add_argument("--iou", type=float, default=0.35)
    ap.add_argument("--sim", type=float, default=SIM_THR)
    # 半帧余量是给 `round()` 出来的标称间隔留的抖动余地（build_runs 里那段 1 µs 的账），不是给丢帧留的。
    # 代价实测（gamestream-first-look 报告）：wuwa-s1 过场里字幕掉单帧、run 断成两条，主轨逐字重复 15%；
    # `--gap-frames 3`（容忍 2 帧）降到 3%。改默认要五部头对头
    ap.add_argument("--gap-frames", type=int, default=1,
                    help="断链容差：tol = (g + 0.5) × 采样间隔，实际容忍 g-1 个丢帧——默认 1 = 一帧都不许丢")
    ap.add_argument("--min-conf", type=float, default=0.5)
    ap.add_argument("--min-runs", type=int, default=2,
                    help="区域少于这么多条 run 就判为 noise（单帧误检）")
    ap.add_argument("--window-sec", type=float, default=60.0,
                    help="聚类的时间窗（秒）。0 = 不分段，退回纯空间聚类——"
                         "只在短片上等价，长片上会把整屏串成一个区域")
    ap.add_argument("--min-persistence", type=float, default=0.0,
                    help="只保留在这么大比例的窗里出现过的槽位（0 = 全留）")
    ap.add_argument("--min-chars", type=int, default=0,
                    help="run 的文本短于这么多字就丢掉（去噪，默认关）")
    ap.add_argument("--min-obs", type=int, default=1,
                    help="run 被看到的次数少于这么多就丢掉（去噪，默认关）")
    # yuka 整片上默认口径会把字幕带和满屏小字串成 y 跨度 0.92 的巨型区域（x_overlap_ratio 的注释）
    ap.add_argument("--mutual-x", action="store_true",
                    help="区域合并的 x 重叠改成除以较宽的那个（要求互相覆盖）；默认除以较窄的——窄条落在宽带里就算完全重叠")
    # 针对"从不同屏的两块被并查集串成一块"：星铁整场 +34 条命中，绝区零 +1、六段切片 ±0，但原神 −49 / −58 / −30
    # （那边主轨只能挑一个区域，接不住被正确分开的几条带）。按游戏显式开（game-text-corpus 报告）
    ap.add_argument("--region-time-gate", type=int, default=0, metavar="N",
                    help="窗内并区多一道时间门：两条轨要么同屏过、要么时段交错 ≥N 次才连边（默认 0 = 关）")
    # 2026-09-24 从 1.4 放宽（defaults.md §1.22）；手设常数、没标定过
    ap.add_argument("--grow-h-ratio", type=float, default=GROW_H_RATIO, metavar="R",
                    help=f"打字机生长的框高比门：前后两帧框高相差超过 R 倍就不算长出来的（默认 {GROW_H_RATIO}；0 = 不看框高）")
    ap.add_argument("--rejoin-split", action=argparse.BooleanOptionalAction, default=True,
                    help="det 在某个采样点上把一条在场的行切成几块时，拼回去当那一行的延续，不另起 run（默认开）")
    # 1.7 是第一版手设的、没标定过（h-ratio 计划）
    ap.add_argument("--region-h-ratio", type=float, default=REGION_H_RATIO, metavar="R",
                    help=f"窗内并区的字高比门：两条行级轨中位字高相差超过 R 倍就不连边（默认 {REGION_H_RATIO}；0 = 不看字号）")
    ap.add_argument("--slot-h-ratio", type=float, default=SLOT_H_RATIO, metavar="R",
                    help=f"跨窗缝合的字高比门：区域和槽位签名字高相差超过 R 倍就不缝（默认 {SLOT_H_RATIO}；0 = 不看字号）")
    # learned（2026-09-21）不建议当默认：两个抽样框合计 74.2% vs median 70.2%，没见过的游戏（鸣潮）上没有优势、过度合并；
    # 训练过的游戏的长窗上重复认领降一半以上（cluster-ruler 计划）。模型只在《魔裁》/ 原神 / 星铁 / 绝区零的标注上训过。
    # lines（2026-09-26）接进管线，公平排名要照 slot_lines 自己抽样才能下
    ap.add_argument("--cluster", choices=("slots", "learned", "lines"), default="slots",
                    help="区域怎么来：slots（默认）= 窗内聚类 + 跨窗缝合；lines（实验性）= 手写规则的行位 -> 带"
                         "（`flowocr.analyze.slot_lines`，同刻同框的上下行并成一条带，散 run 回落到缝合分组）；learned（实验性）= 行位当节点、"
                         "并不并由学出来的成对亲和度决定（`flowocr.analyze.slot_learned`，模型随代码）")
    # owner 2026-09-21 翻的（原默认 pooled）。依据是聚类的尺：两个抽样框合计 312 对上 median 对 pooled 逐对 39 : 16、
    # 错误率 22% vs 30%（第一轮报的 32 : 8 只照 pooled 抽，有抽样框偏差），median 之下 --slot-reassign 开关分不出来；
    # 五部有内容档 +0/+14/-1/+21/-7、重复认领不涨；游戏两个 40 分钟窗上用途 2 逐数相同。
    # 09-07 在五部上被否过（名牌纯度 i1 1.5 / i2 0.9）、09-11 又因净命中 -191~+93 没采纳——那两把尺量的是名牌纯度
    # （奖励碎片化，audit-3）和主轨换区域（--main-band 进默认后不再出现）
    ap.add_argument("--slot-geom", choices=("pooled", "median"), default="median",
                    help="槽位签名怎么算：median（默认）= 各成员窗区域几何的逐分量中位数；"
                         "pooled = 成员的 run 倒一起取分位（一个大成员就能把签名撑开）")
    # "大吃小"的闸门：名牌带 268 px 落进 708 px 的混合槽位，比值 2.64 从 3.0 底下钻过去了
    ap.add_argument("--slot-span-ratio", type=float, default=SLOT_SPAN_RATIO,
                    help=f"槽位和候选的 y 跨度最多差几倍（垫 2 倍行高后；默认 {SLOT_SPAN_RATIO}）")
    # 名牌带（x=[214,761]）和满屏小字没有 x 重叠，但槽位质心一旦长成 x=[90,1762] 的大框，窄带就整个落在里面了
    ap.add_argument("--slot-mutual-x", action="store_true",
                    help="缝合层的 x 判据改成除以较宽的那个（--mutual-x 的跨窗版；默认关）")
    # 2026-09-04 之前的缝合。纯重叠会被大吃小——片尾滚动表当种子能吞掉全片 65% 的 run
    ap.add_argument("--no-slot-anchor", action="store_true",
                    help="退回老的跨窗缝合：纯区间重叠、且不做收敛后的合并轮（对照用）")
    ap.add_argument("--text-pick", choices=("vote", "longest", "tail", "conf", "maxconf"), default="vote",
                    help="一条 run 用哪一版文本：vote=多数投票（默认，挡抖动垃圾）、"
                         "longest=取最长（保住打字机打完那版，但会收进垃圾尾巴）、"
                         "tail=多数投票但允许补回 ≤--tail-max 个字的延长、"
                         "conf=按 rec 置信度加权投票、maxconf=取置信度最高的那一票（后两个是对照臂）")
    # 3 是 owner 三档里"有内容 ≥3 字"那条线，不是新常数。候选比较里 41%–59% 的前缀匹配短边只有 1–2 字（`1` 配上了
    # `1-ザ-ID:712688376`），≥3 字那批全是真打字机增长；实测产物几乎不变（三段整片切片里只有 hsr-s2 多出 3 条 run）——堵一个没发作过的洞
    ap.add_argument("--prefix-min", type=int, default=PREFIX_MIN_CHARS,
                    help=f"前缀加分（0.95）要求短的那一侧至少几个字（默认 {PREFIX_MIN_CHARS}；0 = 不设门）")
    # methodology-audit-4 报告 C4：复用把上一帧的文本原样搬过来，实测沿用占全部观测的 32%–55%（wuwa-s1 / gi-s2），
    # 一次读错会被自己复制出压倒性多数。开之前先跑 dev_tools/sampleset.sh quick
    ap.add_argument("--vote-independent", action="store_true",
                    help="一条 run 的文本投票只数独立的读数，排除框级复用搬过来的票（观测里的 `reused`；默认关）。"
                         "全是沿用的 run 退回全体投票")
    ap.add_argument("--tail-max", type=int, default=3,
                    help="--text-pick tail 时，最多补回几个字")
    ap.add_argument("--no-typewriter", action="store_true",
                    help="关掉打字机增长匹配（对照用）")
    # auto 是 owner 2026-09-25 定的；测量只在阶段 1、结算只在这里，不另存 -tracks-refined.json（owner 2026-09-26）
    ap.add_argument("--refine", choices=("auto", "off"), default="auto",
                    help="auto（默认）：obs 里有在线回抠的证据（run_ocr2 默认会量）就在建轨末尾结算，"
                         "产物和 SRT 直接是帧级时刻、带打字机 / 淡入淡出；没有证据就停在采样级并提示（用默认设置重跑阶段 1 即可）。"
                         "off = 不回抠（采样级，对照或给开发探针当输入）")
    # 2026-09-26 进默认（defaults.md 1.32）：yuka 两窗救回的结尾证据和开视频离线测量逐帧相同，结尾真值不变
    ap.add_argument("--refine-salvage-end", action=argparse.BooleanOptionalAction, default=REFINE_SALVAGE_END,
                    help="回抠结算时，被回补作废、窗口却凑齐了的结尾证据照样用（默认开）：回补改的是文本，"
                         "结尾量的是框里像素什么时候变，按末次观测时刻 + 框认领")
    ap.add_argument("--srt-mode", choices=("segment", "cue", "body", "raw"), default="segment",
                    help="segment=按同屏行集合切段，再把『行只增不减』的相邻段并掉"
                         "（默认，这是打字机错开出现的正解）；"
                         "cue=纯按『一起消失』分组（对常驻说话人名会失手，见 docs）；"
                         "body=只有正文行的首尾是切点，名牌 / 闪烁提示 / 碎片挂进去（见 body_segments，对照臂）；"
                         "raw=一条 run 一条")
    ap.add_argument("--typewriter-min-chars", type=int, default=TYPEWRITER_MIN_CHARS,
                    help=f"打字机增长匹配要求上一帧至少几个字（默认 {TYPEWRITER_MIN_CHARS}；"
                         "1 = 首帧只打出 1 个字也接上，那时前缀必须逐字相同，见 TYPEWRITER_MIN_CHARS）")
    # 2026-09-08 由 0 改的（MAIN_BAND_DEFAULT 的注释）。存在的理由：匹配器只看主轨一个区域，聚类一把字幕带切成两块，
    # 第二块的台词就整段消失（f1 上实测 191 条连续台词跑到了 region07，gamestream-first-look 报告）
    ap.add_argument("--main-band", type=float, default=MAIN_BAND_DEFAULT,
                    help=f"把和主轨同一条字幕带的区域并进主轨，参数是允许的中心 y 差（几倍主轨字高；默认 {MAIN_BAND_DEFAULT:g}，"
                         "0 = 关、主轨只是单个区域）。只影响并出来的 `<tag>-main.srt`，逐区域 SRT 不变")
    ap.add_argument("--main-band-h", type=float, nargs=2, default=[0.7, 1.4],
                    help="并带时允许的字高比区间")
    ap.add_argument("--ruby-mark", action=argparse.BooleanOptionalAction, default=True,
                    help="按几何标振り仮名（紧贴含汉字正文上方、字高约一半、全假名），事件打 ruby 标；SRT、匹配器、default 叠加不给，全部文字的叠加照画（默认开）")
    ap.add_argument("--seg-skip-footprint", action=argparse.BooleanOptionalAction, default=True,
                    help="切 cue 之前把框位门标了的常驻 UI run 拿出来（默认开）：区域轨里它们单独成 cue，并带主轨里直接不要。"
                         "不拿的话只含水印的空段会并进下一条 cue、把 cue 起点提前")
    # 实测不该开：quick 六段上三段的并带整个消失——被切下来的那半正因为"是碎片"（run 少、时长统计变了）才被判成 misc
    # （clustering 报告）；09-21 拆开量过，要挡的 UI 垃圾 --main-band-x 已经挡住（defaults.md 1.30）
    ap.add_argument("--main-band-label", action="store_true",
                    help="并带时要求被并区域也是可当主轨的标签（默认关，不建议开）")
    # 2026-09-26 进默认（defaults.md 1.30）：只看 y 的并带会把画面两侧同高度的 UI / 图标误读并进主轨
    ap.add_argument("--main-band-x", action=argparse.BooleanOptionalAction, default=True,
                    help="并带时再看 x：区间重叠 <0.30 且中心差 >0.08 画宽的区域不并（复用缝合层的 x 判据；默认开）")
    # 实验开关（followups「并带仍会被区域组成的小扰动带偏」）：区域整块不在带上时，按"区域 × 时间窗"再判一次
    ap.add_argument("--main-band-unit", choices=("region", "window"), default="region",
                    help="并带按什么单位判（默认 region = 整个区域）。window：整块没并进来的区域再按时间窗拆开逐块判、"
                         "块过了带的成员再逐条判一次，跨窗缝合缝进混合区域的那几个窗的字幕带也并进主轨。"
                         "改的是主轨（`<tag>-main.srt`、tracks 里 kind=main 的轨和事件的 band_merged 标），"
                         "经 `tracksio.in_main` 也影响选项 / 主轨外两层叠加和阶段 3 的主轨标")
    # 菜单 / 片头那种片段实测最高分只有 0.2；门槛多少合适没有数据支撑，留给调用方按用途定（methodology-audit-4 报告 C3）
    ap.add_argument("--main-min-score", type=float, default=0.0,
                    help="主轨分要高过这个才算数（默认 0 = 只要求 > 0）。宁可没有主轨也不要一条垃圾轨时调高")
    # 2026-09-20 起默认开（clustering 报告）：只合不分时整片实测 44~45% 的成员不再满足最终签名的闸门（audit-4 C1）
    ap.add_argument("--no-slot-reassign", dest="slot_reassign", action="store_false",
                    help="关掉重分配，退回只合不分的老缝合（对照用）。默认缝合收敛之后对着最终签名重判一遍："
                         "不满足的成员换槽位，都不满足就独立成槽")
    ap.add_argument("--slot-reassign-iters", type=int, default=3,
                    help="重分配迭代几轮（换完签名会变，所以要迭代；上限防振荡）")
    # 2026-09-20 翻成 both（ui-gate 计划）。文本那条门的分母是 cue 数（管线自己的产物），同一段素材三条臂读出
    # 56.0/53.0/49.2%、默认臂离门只有 6 个点（inference-runtime 计划）；时间占比三条臂完全相同、余量 34 个点
    ap.add_argument("--ui-gate", default="both", choices=("text", "footprint", "both"),
                    help="常驻 UI 用哪份判据：text = 按文本 + cue 占比；footprint = 按框位 + 时间占比"
                         "（flowocr.analyze.uigate）；both = 两个的并集（默认）")
    ap.add_argument("--ui-fp-share", type=float, default=0.25,
                    help="框位门：在屏时长占全片的比例下限")
    ap.add_argument("--ui-fp-wvar", type=float, default=0.13, help="框位门：框宽离散上限")
    ap.add_argument("--ui-fp-churn", type=float, default=0.06, help="框位门：文本翻动上限")
    ap.add_argument("--ui-fp-clen", type=int, default=13, help="框位门：内容字中位上限")
    ap.add_argument("--no-nameplate-mark", dest="nameplate_mark", action="store_false",
                    help="不给 run 打名牌标（默认打）。判据在 flowocr.analyze.uigate.pick_nameplates："
                         "在正文带上方一档 + 时长 >= 台词 × 比例 + 词表小，按时间分窗")
    # 不分窗的话全片只聚出一条带，"在带上方"会把整片落在它上方的一切都收了（f5 精确率 11%）
    ap.add_argument("--nameplate-window", type=float, default=120.0,
                    help="挑名牌的时间窗（秒）")
    # owner 2026-09-19：时长通常 >= 台词。这条是精确率的主力：去掉它 f3 从 76% 塌到 6%
    ap.add_argument("--nameplate-dur-ratio", type=float, default=0.8,
                    help="名牌时长至少是正文的多少倍")
    ap.add_argument("--nameplate-churn", type=float, default=0.8,
                    help="名牌的文本翻动上限（十来个名字轮着出）")
    # 2026-09-20 补的第 4 条判据：`z-xingchuan`（根本没有名牌）上前三条标了 198 条弹幕 / OCR 噪声，只有这条拦得住（198 → 21）。
    # 只有一个工作点、没扫过平台（审计七），要 A/B 就给这个旋钮；z-shuigong 上它掉的 70 条里真名牌 1 条（followups-evidence-0926 报告）
    ap.add_argument("--nameplate-cover", type=float, default=0.3,
                    help="名牌里属于重复文本的条目至少占这么多（`uigate.measure` 的 `cover`；0 = 关掉这条判据）")
    # 0.2 会把说话人名牌一起删
    ap.add_argument("--ui-share", type=float, default=UI_SHARE,
                    help="短行（<=6 字）在这么大比例的 cue 里独立出现就判为常驻 UI 并剔掉（0 = 关；别调到 0.5 以下）")
    # 存在的理由是 --ui-share 的分母是区域自己的 cue 数，场景局部的小区域里任何一行都轻易过半（整片上把角色名牌当 UI 删了，
    # gamestream-first-look 报告）。开之前先跑五部头对头：0.3 的地板在四部整片上只留下 `Auto` 和 `Q`，把现在删的东西放过 97%
    ap.add_argument("--ui-min-onshare", type=float, default=0.0,
                    help="文本那条 UI 门再加一道全片尺度的地板：含这一行的段总时长 / 全片时长要到这个数才算常驻 UI（默认 0 = 关）")
    # 只看比例的话 1 条 cue 的小区域会被整个清空（methodology-audit-2 报告）
    ap.add_argument("--ui-min-support", type=int, default=20,
                    help="常驻 UI 判定的绝对条数下限：那一行至少要出现这么多次")
    ap.add_argument("--cue-min-overlap", type=float, default=0.5,
                    help="并进同一条 cue 的行，重叠时长要占**较长**那条的多大比例")
    ap.add_argument("--cue-join", choices=("on", "off"), default="on",
                    help="segment 切分末尾做同句合并（cue-shape 计划的 b）：同一句被 ▼ 闪烁、打字机半截、名牌抖动切开的几条并回一条，"
                         "半截行 / 杂行留作成员、投影时藏。off = 不合并、不藏（2026-10-05 之前的切分）")
    ap.add_argument("--cue-end-tol-frames", type=float, default=1.0,
                    help="判定『一起消失』的容差，单位是采样间隔——不是固定毫秒")
    # text-motion 计划第 2 步
    ap.add_argument("--move-seed-last", type=float, default=0.0, metavar="SHARE",
                    help="在动的区域不当缝合种子：给一个占比阈值（例 0.5 = 过半 run 在动的区域）排到最后再去找槽位（默认 0 = 关）")
    # text-motion 计划第 3 步；0.3 的依据见 MAIN_MOVE_MAX 的注释
    ap.add_argument("--main-move-max", type=float, default=MAIN_MOVE_MAX, metavar="SHARE",
                    help=f"在动的区域不当主轨：`moving_share` 达到这个值就不接受（默认 {MAIN_MOVE_MAX}；1.0 = 不拦）")
    ap.add_argument("--event-texts", action="store_true",
                    help="每个事件把逐票文本和沿用标记也写进 JSON（大，默认关）")
    ap.add_argument("--print-defaults", action="store_true",
                    help="打印当前默认值的指纹后退出（head2head 的默认臂会记一行）")
    return ap


def settle_refine(doc: dict, obs_path: Path, tag: str, salvage_end: bool = REFINE_SALVAGE_END) -> bool:
    """`--refine auto`：obs 里有在线回抠的证据就在这里把回抠结算掉——**在内存里就地改 doc**，调用方随后重出 SRT、最后才写 tracks.json。
    结算用 `refine_boundaries.refine_tracks` 的 obs 证据那条路、默认标签；产物不另存 `-tracks-refined.json`：一级产物只有一份
    （AGENTS.md 护栏）。测量只在阶段 1（`run_ocr2` 默认在线量）、结算只在这里（owner 2026-09-26）。没有证据时产物停在采样级、打一行提示。"""
    from flowocr.analyze import refine_boundaries as RB      # 懒 import：RB 顶层 import 了本模块
    meta, fused = RB.obs_evidence(obs_path)
    if not fused or not meta.get("src_fps"):
        print("回抠（--refine auto）：obs 里没有在线回抠的证据（旧 obs 或 --refine-aux noop），产物停在采样级；"
              "要帧级时刻和打字机效果，用默认设置重跑阶段 1（run_ocr2 默认在线量证据），再建轨")
        return False
    fps = float(meta["src_fps"])
    print("回抠（--refine auto，读 obs 里的证据，不开视频）：", flush=True)
    try:
        st = RB.refine_tracks(doc, decoder="obs", fps=fps, obs=obs_path, obs_code_fp=meta.get("code_fp", ""),
                              salvage_stale_end=salvage_end)
    except (SystemExit, Exception) as exc:
        # 有证据却结算不了（一条都认领不到 = obs 和轨对不上；或证据行坏了抛 KeyError / ValueError）：tracks.json 还没写，
        # 不会留下被驱动当缓存复用的采样级产物（2026-09-25 / 26 复查）
        raise SystemExit(f"回抠（--refine auto）结算失败：{exc}\n  没写 {tag}-tracks.json（目录里的区域 SRT 是采样级的，"
                         f"别单独拿去用）。要采样级的产物就加 --refine off 重跑建轨") from None
    for line in RB.summary_lines(tag, st, doc, fps, "obs"):
        print(line)
    return True


def main() -> int:
    ap = make_parser()
    # **在 parse_args 之前拦**：`obs` 和 `--outdir` 是必填的，
    # 只想问"现在的默认是什么"时不该被逼着编两个路径出来。
    if "--print-defaults" in sys.argv[1:]:
        fp, line = defaults_fingerprint(ap)
        print(f"默认指纹 {fp}")
        print(line)
        return 0
    args = apply_matcher_patch(ap)

    meta, obs = obsio.read_obs(args.obs, args.min_conf)     # 逐行读、只留建 run 要的字段（resource 计划第 2 步）
    if not obs:
        raise SystemExit("没有满足置信度的观测")

    W, H = meta.get("width"), meta.get("height")
    if not W:
        raise SystemExit(OBS_NO_SIZE)
    if args.viewport:                        # 建轨不用它，只记；格式、出没出画面现在就核，别等到匹配才报
        try:
            args.viewport_box = tracksio.parse_viewport(args.viewport)
            tracksio.check_viewport(args.viewport_box, W, H)
        except ValueError as e:
            raise SystemExit(f"--viewport：{e}")
    frame_us = int(round(1e6 / meta["sample_fps"]))
    t0_us = min(o["t_us"] for o in obs)
    dur_us = max(o["t_us"] for o in obs) + frame_us

    cuts = ()
    observable = None                        # (框, t0, t1) -> 这个框位在 [t0, t1) 里看得见多久；None = 全程看得见
    if meta.get("regions"):                  # 自选范围：时间关闭段不许被事件跨过、时间支持度按可观察时间算（ocr-regions 计划）
        from flowocr.extract import regions as RG
        rgrp = RG.from_spec(meta["regions"], W, H)
        cuts = RG.cuts_us(rgrp)
        if not rgrp.unrestricted:
            observable = rgrp.observable_us
        print(f"[范围] 组 {meta['regions']['name']}：{len(cuts)} 个时间切点", flush=True)
    runs = build_runs(obs, frame_us, args.iou, args.sim, args.gap_frames,
                      grow_typewriter=not args.no_typewriter,
                      text_pick=args.text_pick, tail_max=args.tail_max,
                      vote_independent=args.vote_independent,
                      prefix_min=args.prefix_min,
                      typewriter_min_chars=args.typewriter_min_chars, grow_h_ratio=args.grow_h_ratio,
                      rejoin_split=args.rejoin_split,
                      cuts=cuts)
    n_obs = len(obs)
    del obs                                  # 之后只要条数，观测字典别留到结尾
    # 去噪：**默认不开**。整片上量过取舍（noise 报告），
    # 这两条砍掉的"多出条目"里既有 OCR 垃圾也有基线漏掉的真台词，
    # 只有"误伤了多少条对得上基线的"是能确证的代价，所以交给调用方决定。
    if args.min_chars > 0 or args.min_obs > 1:
        keep = [r for r in runs
                if len("".join(r.text.split())) >= args.min_chars and r.n_obs >= args.min_obs]
        print(f"去噪：{len(runs)} -> {len(keep)} 条 run"
              f"（min_chars={args.min_chars}, min_obs={args.min_obs}）")
        runs = keep

    # 时间合并（build_runs）始终是全局的——窗边界不能把一条字幕切断。
    # 只有**空间聚类**分窗做。
    if args.window_sec > 0:
        window_us = int(args.window_sec * 1e6)
        tracks, win_regions = cluster_windowed(runs, W, H, window_us, args.mutual_x,
                                               args.region_time_gate, args.region_h_ratio)
        # 窗内 run 太多，空间聚类会重新开始串联。实测（3h20m 整片）失效点在
        # 每窗 ~860 条（窗 1200s，仍正常）到 ~1725 条（窗 2400s，y 跨度 0.91）之间。
        # 默认 60s 窗在该片上每窗只有 43 条，余量 20 倍以上；这里只在越界时提醒。
        per_win = Counter(window_of(r, window_us) for r in runs)
        if per_win and max(per_win.values()) > SAFE_RUNS_PER_WINDOW:
            print(f"  ⚠ 有窗内 run 数达 {max(per_win.values())}（经验安全线 {SAFE_RUNS_PER_WINDOW}），"
                  f"聚类可能重新串联；把 --window-sec 调小", flush=True)
        slots = stitch_slots(runs, win_regions, W, H, not args.no_slot_anchor,
                             args.slot_mutual_x, args.slot_span_ratio, args.slot_geom,
                             move_seed_last=args.move_seed_last,
                             reassign=args.slot_reassign,
                             reassign_iters=args.slot_reassign_iters,
                             h_ratio=args.slot_h_ratio)
        # 缝合是贪心且只合不分的，**最终成员未必还满足最终签名的闸门**
        # （methodology-audit-4 报告 C1）。这里只报数，不改算法。
        bad_mem, tot_mem, why_mem = slot_consistency(
            runs, win_regions, slots, W, not args.no_slot_anchor,
            args.slot_mutual_x, args.slot_span_ratio, args.slot_geom, args.slot_h_ratio)
        if tot_mem:
            top = "、".join(f"{k} {v}" for k, v in why_mem.most_common(3))
            print(f"  槽位最终一致性：{bad_mem}/{tot_mem} = "
                  f"{bad_mem/tot_mem:.1%} 的成员**不满足最终签名的闸门**"
                  f"（贪心只合不分的代价，见 audit-4 C1；这是观测值，不是失败）"
                  + (f"；卡在：{top}" if top else ""),
                  flush=True)
        # 窗数按**观测实际覆盖的时间跨度**算，不是从 0 算到片尾。
        # 只跑一个时间窗（run_ocr2 的 --start/--end）时，后者会把 persistence 的
        # 分母撑成整片长度：40 分钟的主轨算成 40/181 = 0.22，primary_score 直接塌掉。
        # 整片跑时 t0_us≈0，这个改动是恒等的。
        n_windows = max(1, (dur_us - 1) // window_us - t0_us // window_us + 1)
        groups = []
        for sl in slots:
            run_ids = sorted({i for w in sl for i in win_regions[w]["runs"]})
            # 常驻度按 run **真实覆盖**到的窗算，不按"归属窗"（见 covered_windows）
            wins = covered_windows(runs, run_ids, window_us)
            groups.append((run_ids, len(wins), wins))
        if args.cluster in ("learned", "lines"):
            # 行级 track（`tracks`）照旧用窗内聚类的——下游拿它给事件挂 track 号；**只换"哪些 run 是一个区域"**
            n_slots = len(groups)
            slot_of_run = {i: k for k, (ids, _, _) in enumerate(groups) for i in ids}
            from flowocr.analyze import slot_learned       # 聚类的尺量的是同一个函数（`slot_pairs.pipeline_assign`）
            new = slot_learned.cluster_groups(args.cluster, runs, W, H, slot_of_run, effective_tag(args))
            groups = []
            for run_ids in new:
                wins = covered_windows(runs, run_ids, window_us)
                groups.append((run_ids, len(wins), wins))
            print(f"区域构建（--cluster {args.cluster}）：缝合给的 {n_slots} 个槽位换成 {len(groups)} 个区域", flush=True)
    else:
        if args.cluster in ("learned", "lines"):
            raise SystemExit(f"--cluster {args.cluster} 只接在分窗那条路上（散 run 要回落到跨窗缝合的分组）；别和 --window-sec 0 一起给")
        window_us, n_windows = dur_us, 1
        tracks = build_tracks(runs, W, H)
        groups = [(sorted(i for t in g for i in tracks[t]), 1, [0])
                  for g in build_regions(runs, tracks, W, H, args.mutual_x, args.region_time_gate,
                                         args.region_h_ratio)]

    if t0_us > window_us:
        print(f"  注：观测从 {t0_us/1e6/60:.1f} min 开始（窗口跑），"
              f"窗数按覆盖跨度算 = {n_windows}，不是从 0 算到片尾", flush=True)

    def active_span(rs: list[Run], wins: list[int]) -> int:
        """槽位"活着"的时长：所在时间窗的并集，再并上 run 自身的区间。

        只按 `窗数 × 窗长` 算是错的——一条 91 秒的 run 只归属一个窗（按中点），
        覆盖时长却超过窗长，于是 fill_active 会算出 1.19 这种数。
        """
        spans = [(w * window_us, min(dur_us, (w + 1) * window_us)) for w in wins]
        spans += [(r.t_start, r.t_end) for r in rs]
        return sum(e - s for s, e in uigate.merge_intervals(spans))

    track_of = {i: t for t, ids in enumerate(tracks) for i in ids}
    groups.sort(key=lambda g: -len(g[0]))

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    tag = effective_tag(args)

    n_stale = clear_stale_srts(outdir, tag)
    if n_stale:
        print(f"清掉上一轮的 {n_stale} 个旧产物（label 变了会留下同 index 的旧 SRT；"
              "`-provenance.json` 已并进 tracks.json，旧的那份也删；tracks.json 本轮成功才重写）")

    report = []
    doc = {"tag": tag, "meta": meta, "frame_us": frame_us, "size": [W, H],
           "window_sec": args.window_sec, "n_windows": n_windows,
           "regions": [], "tracks": [], "events": []}
    events: list[dict] = doc["events"]
    eid_of: dict[int, int] = {}                    # id(Run) -> 事件 id
    region_runs: dict[int, list[Run]] = {}
    region_ids: dict[int, list[int]] = {}
    placed: set[int] = set()                       # 进了某条区域轨 cue 的 run（`within_regions`）
    seg_kw = dict(mode=args.srt_mode, end_tol_us=int(args.cue_end_tol_frames * frame_us),
                  min_overlap=args.cue_min_overlap, join=args.cue_join == "on")
    # 同句合并开着时，半截行 / 杂行留作 cue 成员、投影时藏（`seg_hide`）；判 UI、数面板、写 SRT 都只看会导出的行（`visible`）
    tol = seg_kw["end_tol_us"] if seg_kw["join"] and args.srt_mode == "segment" else None
    _HIDE_CACHE.clear()
    ui_kw = dict(min_support=args.ui_min_support,
                 min_onshare=args.ui_min_onshare, film_us=dur_us)
    # **名牌标**：在出事件之前全片挑一次（**按时间分窗**，判据在 `flowocr.analyze.uigate`）。
    # 为什么不按区域挑：五部整片上**三部的名牌 run 被巨型区域吞掉了**，区域侧根本没得挑
    # （ui-gate 计划）。框位是按 IoU 聚的，不经过区域那一层。
    if args.nameplate_mark:
        _items = [uigate.Item(tuple(r.box), r.t_start, r.t_end, r.text, key=i, w=max(1, r.n_obs),
                              clipped=r.clip_start is not None or r.clip_end is not None)
                  for i, r in enumerate(runs)]
        _np = uigate.pick_nameplates(_items, win_us=int(args.nameplate_window * 1e6),
                                     dur_ratio=args.nameplate_dur_ratio,
                                     max_churn=args.nameplate_churn,
                                     min_cover=args.nameplate_cover,
                                     step_us=frame_us, observable=observable)
        _keys = {it.key for it in _np}
        for i, r in enumerate(runs):
            r.nameplate = i in _keys
        print(f"名牌标：{len(_keys)} / {len(runs)} 条 run"
              f"（窗 {args.nameplate_window:.0f}s，时长 ≥ 正文×{args.nameplate_dur_ratio}，"
              f"词表 ≤ {args.nameplate_churn}）")

    # **框位 + 时间占比的常驻 UI 门**（`--ui-gate`，2026-09-20 起默认 `both`）。
    ui_fp = ui_footprints(runs, frame_us, args, observable)
    run_idx = {id(r): i for i, r in enumerate(runs)}
    if args.ui_gate != "text":
        print(f"常驻 UI（框位门）：{len(ui_fp)} / {len(runs)} 条 run"
              f"（占比 ≥{args.ui_fp_share:.0%}、框宽离散 ≤{args.ui_fp_wvar}、"
              f"翻动 ≤{args.ui_fp_churn}、内容字 ≤{args.ui_fp_clen}；分母是**时间**不是 cue 数）")
    # 振り仮名：全局按几何标（`ruby_runs`），和框位 UI 一样在剔 UI 的导出里剔（`tracksio.cue_lines`）
    ruby = ruby_runs(runs) if args.ruby_mark else set()
    if ruby:
        print(f"振り仮名：{len(ruby)} / {len(runs)} 条 run（紧贴含汉字正文上方、字高 {RUBY_H[0]}–{RUBY_H[1]} 倍、全假名）")
    # 两个集合别混：`export_drop` 是剔 UI 的导出（SRT 投影）里不给的，和开关无关；
    # `seg_apart` 是切 cue 之前和正文分开切的，框位 UI 那一半跟着 --seg-skip-footprint 走
    export_drop = ui_fp | ruby
    seg_apart = (ui_fp if args.seg_skip_footprint else set()) | ruby

    ri = 0
    for run_ids, n_win, wins in groups:
        if n_win / n_windows < args.min_persistence:
            continue
        rs = [runs[i] for i in run_ids]
        region_runs[ri] = rs
        region_ids[ri] = run_ids
        span_us = dur_us
        if observable is not None:           # 自选范围：fill 的分母是这块区域看得见的时长（成员外接框里有有效像素的时段）
            bb = [min(r.box[0] for r in rs), min(r.box[1] for r in rs), max(r.box[2] for r in rs), max(r.box[3] for r in rs)]
            span_us = max(1, observable(bb, 0, dur_us))   # 口径同原来的 dur_us（从 0 算），只扣掉关闭时段
        f = region_features(rs, len({track_of[i] for i in run_ids}), W, H,
                            span_us, active_span(rs, wins), n_win / n_windows)
        lab = label_region(f, args.min_runs)
        f["primary_score"] = primary_score(f, lab)
        for r in sorted(rs, key=lambda r: r.t_start):
            eid_of[id(r)] = len(events)
            events.append(event_dict(r, len(events), ri, with_texts=args.event_texts))
        # 切分和判 UI 在这里做一次，**结果既写进 JSON 也用来出 SRT**——
        # 导出器不再重新判断（`output-format-plan（已归档）`）。
        # 框位 UI / 振り仮名和正文**分开切**（`--seg-skip-footprint`）：混着切的话只含水印的空段会并进下一条 cue、把起点提前，
        # 而默认 `--feed nonoise` 喂给匹配器的正是区域轨（peer 审查 6b47a0c 第 1 条：gi-s1 r13 15/17 条、zzz-s1 r06 最多早 168 s）。
        # 它们的事件照旧留在这条轨里、各自成 cue，用途 1 的叠加照样按轨标 UI；文本那条 UI 判据只数正文的段
        segs, body_segs = split_hidden(rs, seg_apart, run_idx, seg_kw)
        placed.update(id(r) for _, _, a in segs for r in a)
        ui = text_ui_lines(visible(body_segs, tol), args, ui_kw)
        for r in rs:
            # `ui_filtered` **逐轨**（文本那条判据）、`ui_footprint` **全局**（框位那条）——
            # 混成一个 flag 的话，导出器按 flag 投影时会把"区域轨里判的 UI"带进主轨，
            # 而那两处的分母本来就不同（主轨的 UI 是并带之后**重新判**的）。
            if r.text.strip() in ui:
                events[eid_of[id(r)]]["flags"].append("ui_filtered")
            if run_idx[id(r)] in ui_fp:
                events[eid_of[id(r)]]["flags"].append("ui_footprint")
            if run_idx[id(r)] in ruby:
                events[eid_of[id(r)]]["flags"].append("ruby")
        srt_name = f"{tag}-region{ri:02d}-{lab}.srt"
        write_srt_segments(outdir / srt_name, visible(segs, tol), ui, export_drop, run_idx)
        tid = f"r{ri:02d}"
        doc["tracks"].append({"id": tid, "kind": "region", "region": ri, "label": lab,
                              "srt": srt_name, "ui_lines": sorted(ui),
                              "cues": cues_from_segments(tid, segs, eid_of, events, H, tol)})
        doc["regions"].append({
            "index": ri, "label": lab, "n_windows_present": n_win,
            "lang": f["lang"], "primary_score": f["primary_score"], "features": f})
        report.append((ri, lab, f))
        ri += 1

    # **主轨是哪个文件，由这里定，不让下游去 glob 猜。**
    # `head2head.sh` 原来按 `f4-region01-*.srt | head -1` 取，隔夜的旧 label 排在前面
    # 就整部量错（见上面清理那段）。写进 provenance，下游直接读。
    main, cands = pick_main(report, args.main_min_score, args.main_move_max)
    main_srt = f"{tag}-region{main[0]:02d}-{main[1]}.srt" if main else ""
    main_track = f"r{main[0]:02d}" if main else ""
    band: list[int] = []
    if main and args.main_band > 0:
        # 几何用和另外两层同一个 `region_geom`（2%/98% 分位），别另算一套
        geom = {ri: region_geom(runs, ids) for ri, ids in region_ids.items()}
        band = band_regions(report, main[0], args.main_band, *args.main_band_h,
                            need_label=args.main_band_label,
                            need_x=args.main_band_x, geom=geom, W=W,
                            mutual_x=args.slot_mutual_x)
        part: dict[int, list[int]] = {}
        if args.main_band_unit == "window":
            part = band_windows(runs, region_ids, report, main[0], {main[0], *band}, window_us,
                                args.main_band, *args.main_band_h, args.main_band_label,
                                geom[main[0]] if args.main_band_x else None, W, H, args.slot_mutual_x)
            if part:
                print(f"  按窗并带（--main-band-unit window）：{len(part)} 个区域补进 "
                      f"{sum(len(v) for v in part.values())} 条 run（这些区域整块的几何不在带上）")
        if band or part:
            # **并成一份新产物，不动逐区域 SRT**：用途 1 要的是分开的块
            # （名牌/正文/称号是屏幕上三个位置），
            # 合并只是给匹配器用的主轨。
            merged = [r for i in [main[0], *band] for r in region_runs.get(i, [])]
            merged += [runs[j] for ri in sorted(part) for j in part[ri]]
            # **框位门标了的常驻 UI、振り仮名在切分之前拿掉**（`--seg-skip-footprint`，默认开）：投影时反正要剔，
            # 留着切分的话，只含水印的空段会并进下一条 cue、把 cue 起点提前（wuwa-s2 8 条、最多早 3.7 s）。
            # 标是全局按框位 / 几何定的，切分前已知。主轨不给它们单独成 cue（主轨的叠加本来就不画 UI）
            n_fp = 0
            if seg_apart:
                kept = [r for r in merged if run_idx[id(r)] not in seg_apart]
                n_fp, merged = len(merged) - len(kept), kept
            main_srt = f"{tag}-main.srt"
            main_track = "main"
            in_main = {id(r) for r in merged}
            for i in [*band, *sorted(part)]:
                for r in region_runs.get(i, []):
                    if id(r) in in_main:                # 被拿掉的 UI / 注音、按窗没并进来的不在任何主轨 cue 里，不打"并进了主轨"
                        events[eid_of[id(r)]]["flags"].append("band_merged")
            segs, n_out = within_regions(cue_segments(merged, **seg_kw), placed)
            if n_out:
                print(f"  主轨：{n_out} 行在自己的区域轨里被切分筛掉了，投影里也不给")
            # 并带后的常驻 UI 是**重新判**的：分母换成了合并后的 cue 数，
            # 同一行在区域轨里够比例、在主轨里不一定够。所以权威清单逐轨存
            # （tracksio 的 `tracks[].ui_lines`），不是事件上那个方便标记。
            ui = text_ui_lines(visible(segs, tol), args, ui_kw)
            # ⚠ **框位门在这里也要生效**：那次翻车（`Auto` 污染 293/595 条主轨 cue）
            # 就发生在这条并带主轨上。只接区域循环等于没接——主轨是另写的一份。
            n = write_srt_segments(outdir / main_srt, visible(segs, tol), ui, export_drop, run_idx)
            # `regions` 只列**整块**并进来的区域：下游（`scriptmatch.choice_items`、阶段 3 的 `main` 标）按"事件的区域在不在这里"
            # 判它在不在主轨上，列上只并了几个窗的区域会把那块区域里其余的事件也算成主轨（选项层 hsr 整场 112 → 73，2026-10-05 量过）。
            # 按窗补进来的事件看 `band_merged` 标
            doc["tracks"].append({"id": "main", "kind": "main", "regions": [main[0], *band],
                                  "label": main[1], "srt": main_srt, "ui_lines": sorted(ui),
                                  "cues": cues_from_segments("main", segs, eid_of, events, H, tol)})
            print(f"主轨并带（--main-band {args.main_band:g}）：region{main[0]:02d} + "
                  + "、".join(f"r{i:02d}" for i in band)
                  + (f"（按窗另补 {'、'.join(f'r{i:02d}' for i in sorted(part))}）" if part else "")
                  + f" -> {main_srt}，{len(merged)} 条 run、**{n} 条 cue**"
                  + (f"（切分前拿掉框位 UI / 振り仮名 {n_fp} 条 run）" if n_fp else ""))
    prov = provenance(args, meta, main_srt, main_track,
                      srtio.count_cues(outdir / main_srt) if main_srt else 0)
    # 候选也写进 provenance：**排名不等于确定正文**，下游要能自己挑
    # （methodology-audit-4 报告 C3）。
    prov["main_candidates"] = [
        {"index": ri, "label": lab, "score": f["primary_score"], "cy": round(f["cy"], 3),
         "srt": f"{tag}-region{ri:02d}-{lab}.srt", "track": f"r{ri:02d}"}
        for ri, lab, f in cands[:5]]
    # **名牌轨**：把打了标的 run 单独投影成一条轨（`kind="nameplate"`）。
    # 切分走的还是同一套 `cue_segments`——导出器不重新判断（docs/architecture/artifacts.md）。
    # ⚠ 它和区域轨**有重叠**：同一条 run 既在它自己的区域轨里，也在这条轨里。
    # 这是有意的（打标不删）：用途 1 要在原位看到名牌，用途 2 要把它并进正文。
    # ⚠ **只收有事件的 run**（审计七）：`eid_of` 只在保留下来的区域循环里填，
    # 而 `--min-persistence > 0` 会丢掉一批区域——它们里面的名牌 run 没有事件，
    # 收进来就是 `cues_from_segments` 里一个 `KeyError`（默认 0.0 时碰不到，
    # 而且 `tracksio` 的子集不变量也会在同一处挡住）。
    np_runs = [r for r in runs if getattr(r, "nameplate", False) and id(r) in eid_of]
    n_np_orphan = sum(1 for r in runs if getattr(r, "nameplate", False)) - len(np_runs)
    if n_np_orphan:
        print(f"  注：{n_np_orphan} 条名牌 run 所在的区域被 --min-persistence "
              f"{args.min_persistence} 丢掉了，不进名牌轨")
    np_segs, n_out = within_regions(cue_segments(sorted(np_runs, key=lambda r: r.t_start), **seg_kw), placed)
    if n_out:
        print(f"  名牌轨：{n_out} 行在自己的区域轨里被切分筛掉了，投影里也不给")
    if np_segs:
        np_name = f"{tag}-nameplate.srt"
        write_srt_segments(outdir / np_name, visible(np_segs, tol), set())
        doc["tracks"].append({"id": "np", "kind": "nameplate", "region": -1,
                              "label": "nameplate", "srt": np_name, "ui_lines": [],
                              "cues": cues_from_segments("np", np_segs, eid_of, events, H, tol)})
        print(f"名牌轨：{len(np_segs)} 条 cue -> {np_name}")

    doc["provenance"] = prov
    # 对齐方式在这里判、写进区域（叠加导出只照着锚边，`flowocr.analyze.align`）
    n_align = align.annotate(doc)
    print("对齐：" + "、".join(f"{a}（{by}）{k}" for (a, by), k in sorted(n_align.items())) + " 个区域")
    # **一级产物最后写**：回抠在内存里结算、SRT 重出成功之后才落 tracks.json（开头已删掉上一轮的那份）。
    # 中途哪一步失败都不留下 tracks.json——驱动判缓存只认它，留着一份就会配着采样级或写了一半的 SRT 被复用（2026-09-26 复查）
    doc = json.loads(json.dumps(doc))        # 结算吃的和落盘再读回来的是同一种结构（tuple 都成 list），同以前"写了再读回来结算"
    if args.refine == "auto" and settle_refine(doc, Path(args.obs), tag, args.refine_salvage_end):
        from flowocr.output import export
        export.export_srt(doc, outdir)       # SRT 换成帧级时刻
    tracksio.dump(outdir / f"{tag}-tracks.json", doc)

    win_note = f"窗 {args.window_sec:g}s × {n_windows}" if args.window_sec > 0 else "不分窗"
    ci = _ratio.cache_info()
    print(f"{tag}: {n_obs} obs -> {len(runs)} runs -> {len(tracks)} tracks -> {len(report)} regions"
          f"  ({W}x{H}, {dur_us / 1e6:.0f}s, 采样 {meta['sample_fps']:.1f}fps, {win_note})"
          f"；文本相似度缓存命中 {ci.hits}/{ci.hits + ci.misses}（满 {ci.currsize}/{ci.maxsize}）")
    cols = ("#", "label", "lang", "主轨分", "trk", "run", "pers", "fillA", "domi",
            "dur", "cx", "cy", "yspan")
    print(" ".join(f"{c:>{n}}" for c, n in zip(cols, (2, 20, 7, 8, 4, 5, 5, 5, 5, 5, 5, 5, 5))))
    for ri, lab, f in report:
        star = " ←主轨" if main and ri == main[0] else ""
        print(f"{ri:>2} {lab:<20} {(f['lang_mix'] or f['lang']):>7} {f['primary_score']:>8.1f} "
              f"{f['n_tracks']:>4} {f['n_runs']:>5} {f['persistence']:>5.2f} "
              f"{f['fill_active']:>5.2f} {f['dominant_share']:>5.2f} {f['median_dur_s']:>5.1f} "
              f"{f['cx']:>5.2f} {f['cy']:>5.2f} {f['y_span']:>5.2f}{star}")

    # **"没有可接受的主轨"是一个正当结果，不是要藏起来的失败。**
    # 屏上没有正文的片段（菜单、片头、探索态）本来就不该硬指一条
    # （methodology-audit-4 报告 C3）。
    if not main:
        best = max(report, key=lambda x: x[2]["primary_score"], default=None)
        print(f"**没有可接受的主轨**：没有区域同时满足『分数 > "
              f"{max(0.0, args.main_min_score):g}』和『label 属于 {'/'.join(MAIN_LABELS)}』。"
              + (f"分最高的是 region{best[0]:02d}（{best[1]}，{best[2]['primary_score']:.1f} 分），"
                 f"**它没被当成主轨**。" if best else "")
              + "\n  下游拿 provenance 里的 main_srt（现在是空的）应当明确失败，"
                "不要退回去 glob 猜。")
    elif len(cands) > 1:
        alt = "、".join(f"region{ri:02d}({lab} {f['primary_score']:.1f})"
                       for ri, lab, f in cands[1:4])
        print(f"主轨候选（排名不等于确定正文）：**region{main[0]:02d} "
              f"{main[1]} {main[2]['primary_score']:.1f} 分**；其次 {alt}")

    # **删了什么要打出来。** 静默删除正是上一次事故的形状：加了常驻 UI 剔除之后
    # 磁盘上多出 89–139 个空的区域 SRT，一行日志都没有（methodology-audit-2 报告）。
    if DROPPED_UI:
        tot_l = sum(x[2] for x in DROPPED_UI)
        tot_c = sum(x[3] for x in DROPPED_UI)
        print(f"常驻 UI 剔除（--ui-share {args.ui_share} / --ui-min-support "
              f"{args.ui_min_support}）：{len(DROPPED_UI)}/{len(report)} 个区域受影响，"
              f"共删掉 {tot_l} 行、其中 {tot_c} 条 cue 被整条删掉")
        for name, ui, dl, dc in sorted(DROPPED_UI, key=lambda x: -x[2])[:8]:
            print(f"    {name:<44} -{dl:>6} 行 / -{dc:>5} 条  {sorted(ui)[:6]}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
