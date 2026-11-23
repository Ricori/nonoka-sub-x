"""**两条 run 该不该在一起**的成对特征，按"通道"分组——用来回答"哪种信号真有信息"（next-steps，owner 2026-09-21）。

聚类的尺（`slot_pairs.py`）产出了带标注的 run 对；这里给每一对算特征，`pair_model.py` 拿它们做逐通道消融。
**先知道哪条通道有用，再决定把它怎么接进聚类**——不先造算法再找证据。

通道：

    geom     几何：x0 / cx / x1 / cy / 上下边的差、字高比、y 区间重叠。现行缝合只用这一条
    text     文本形态：长度、假名 / 汉字 / 拉丁 / 数字 / 标点占比、是不是句子样（句末标点、含假名）。
             同一个元素里的文本形态很稳（台词是句子、HUD 是数字、名牌是短名词）
    context  **时间当关系信号用**（owner："时间是个可用于否决的弱信号，但怎么用需要好的设计"）：
             一条 run 在屏时，**同屏还有哪些位置有字**。对话正文总和名牌、Auto 键一起出现；
             菜单项总和别的菜单项一起出现。两条 run 位置相同、同屏伙伴却完全不同 → 多半不是同一个元素。
             做法：画面切成 CELLS 网格，每秒记哪些格子有字；run 的上下文 = 它在屏期间出现过的格子
             （去掉自己那一格和紧邻的），按 idf 加权（常驻水印那种格子几乎不计）；两条 run 比加权 Jaccard
    time     朴素的时间：间隔（对数）、是否同一个 60 s 窗、在屏时长比
    block    块：同屏、同左边、同字高的行数（阅读面板 / 名单是一大块，对话是 1~3 行）
    motion   `Run.moving` / `Run.scrolling`

用法：
    F = PairFeatures(L)                 # L 来自 cluster_layers.load
    x, names = F.pair(i, j)             # 两条 run 的下标 -> 特征向量
"""
from __future__ import annotations

import math
import unicodedata
from collections import Counter, defaultdict


CELLS = (16, 18)                        # x 方向 16 格、y 方向 18 格（1080p 上 120 × 60 px）
CTX_SECONDS = 12                        # 一条 run 只取在屏的前这么多秒算上下文（常驻的不必扫全程）
CHANNELS = ("geom", "text", "context", "time", "block", "motion")


def char_profile(t: str) -> dict[str, float]:
    t = "".join(unicodedata.normalize("NFKC", t).split())
    n = max(1, len(t))
    c = Counter()
    for ch in t:
        o = ord(ch)
        if 0x3040 <= o <= 0x30FF:
            c["kana"] += 1
        elif 0x4E00 <= o <= 0x9FFF:
            c["han"] += 1
        elif ch.isdigit():
            c["digit"] += 1
        elif ch.isascii() and ch.isalpha():
            c["latin"] += 1
        elif not ch.isalnum():
            c["punct"] += 1
    out = {k: c[k] / n for k in ("kana", "han", "digit", "latin", "punct")}
    out["len"] = math.log1p(len(t))
    out["sentence"] = float(bool(t) and (t[-1] in "。！？!?…」』）)" or c["kana"] >= 3))
    return out


def content_len(t: str) -> int:
    return sum(1 for c in t if c.isalnum())


MIN_CONTENT, MIN_OBS = 3, 2
"""**只在"像样的文字"里抽**：内容 ≥3 字、至少 2 次观测。第一版按 run 数加权、不筛，
f5 头 4 对里 3 对是 OCR 把**鼠标指针**读成的字（只能标 unsure），标注预算被垃圾区域吃掉。
⚠ 这是一道筛子：尺量不到"垃圾 run 落在哪个槽位"——那是清噪的事，不是聚类的对错；
`sample` 会把筛掉的 run 占比打出来，读数时摆在旁边。"""


def good_run(r) -> bool:
    return content_len(r.text) >= MIN_CONTENT and r.n_obs >= MIN_OBS


FEATURE_NAMES = [
    "geom:dx_min", "geom:dx0", "geom:dcx", "geom:dx1", "geom:dcy_h", "geom:dcy", "geom:log_h", "geom:y_ov",
    "geom:line_pitch", "geom:adjacent", "geom:adjacent_cooccur", "text:d_kana", "text:d_han", "text:d_digit", "text:d_latin", "text:d_punct", "text:d_len",
    "text:both_sentence", "text:one_sentence", "text:both_digit", "context:jaccard", "context:empty",
    "context:size_ratio", "time:log_gap", "time:same_win", "time:cooccur", "time:dur_ratio", "block:d_log",
    "block:min", "motion:any", "motion:both"]
"""`pair()` 吐的特征顺序。落盘的模型按这张表核对——特征一改，旧模型就不许再用。"""


class PairFeatures:
    def __init__(self, L) -> None:
        self.L = L
        self.runs = L.runs
        W, H = L.W, L.H
        self.cell = [(min(CELLS[0] - 1, int(r.cx / W * CELLS[0])), min(CELLS[1] - 1, int(r.cy / H * CELLS[1])))
                     for r in self.runs]
        self.prof = [char_profile(r.text) for r in self.runs]
        # 每秒哪些格子有字 / 哪些 run 在屏
        self.by_sec: dict[int, list[int]] = defaultdict(list)
        for i, r in enumerate(self.runs):
            for s in range(int(r.t_start // 1e6), int(r.t_end // 1e6) + 1):
                self.by_sec[s].append(i)
        occ = Counter()
        for s, ids in self.by_sec.items():
            for c in {self.cell[i] for i in ids}:
                occ[c] += 1
        T = max(1, len(self.by_sec))
        self.idf = {c: math.log(T / n) for c, n in occ.items()}
        self._ctx: dict[int, dict] = {}
        self._blk: dict[int, int] = {}

    def context(self, i: int) -> dict[tuple, float]:
        if i not in self._ctx:
            r, (cx, cy) = self.runs[i], self.cell[i]
            s0 = int(r.t_start // 1e6)
            cells = set()
            for s in range(s0, min(int(r.t_end // 1e6), s0 + CTX_SECONDS) + 1):
                cells.update(self.cell[j] for j in self.by_sec.get(s, ()) if j != i)
            self._ctx[i] = {c: self.idf.get(c, 0.0) for c in cells
                            if abs(c[0] - cx) > 1 or abs(c[1] - cy) > 1}
        return self._ctx[i]

    def block(self, i: int) -> int:
        """同屏、左边对齐（±1.5% 画面宽）、字高相近（比 ≤1.3）的行数（含自己）。"""
        if i not in self._blk:
            r = self.runs[i]
            s = int((r.t_start + min(r.t_end, r.t_start + 2_000_000)) // 2e6)
            self._blk[i] = sum(1 for j in self.by_sec.get(s, ())
                               if abs(self.runs[j].box[0] - r.box[0]) <= 0.015 * self.L.W
                               and max(self.runs[j].h, r.h) / max(1.0, min(self.runs[j].h, r.h)) <= 1.3)
        return self._blk[i]

    def pair(self, i: int, j: int) -> tuple[list[float], list[str]]:
        a, b, W, H = self.runs[i], self.runs[j], self.L.W, self.L.H
        h = max(1.0, min(a.h, b.h))
        f: dict[str, float] = {}
        dx0, dcx, dx1 = abs(a.box[0] - b.box[0]) / W, abs(a.cx - b.cx) / W, abs(a.box[2] - b.box[2]) / W
        f["geom:dx_min"] = min(dx0, dcx, dx1)
        f["geom:dx0"], f["geom:dcx"], f["geom:dx1"] = dx0, dcx, dx1
        f["geom:dcy_h"] = min(20.0, abs(a.cy - b.cy) / h)
        f["geom:dcy"] = abs(a.cy - b.cy) / H
        f["geom:log_h"] = abs(math.log(max(1.0, a.h) / max(1.0, b.h)))
        ov = min(a.box[3], b.box[3]) - max(a.box[1], b.box[1])
        f["geom:y_ov"] = max(0.0, ov) / h
        f["geom:line_pitch"] = abs((abs(a.cy - b.cy) / h) - round(abs(a.cy - b.cy) / h / 1.35) * 1.35)   # 离"整数行距"多远
        # **相邻行**：同锚点、同字高、隔一到两个行距。线性模型只有"竖直距离越大越不像"一项，表达不了
        # "多行正文的上下行是同一块"——hsr 的三行台词因此被拆成两个区域、每段都够不着剧本行（2026-09-21）。
        # 同屏出现的相邻行是最强的证据（同一时刻、同一个锚点、差一个行距 = 同一块多行文本），单列一项。
        gap0 = max(0, max(a.t_start, b.t_start) - min(a.t_end, b.t_end))
        adj = float(0.7 <= f["geom:dcy_h"] <= 2.9 and f["geom:dx_min"] <= 0.02 and f["geom:log_h"] <= 0.26)
        f["geom:adjacent"] = adj
        f["geom:adjacent_cooccur"] = adj * float(gap0 == 0)
        pa, pb = self.prof[i], self.prof[j]
        for k in ("kana", "han", "digit", "latin", "punct", "len"):
            f[f"text:d_{k}"] = abs(pa[k] - pb[k])
        f["text:both_sentence"] = pa["sentence"] * pb["sentence"]
        f["text:one_sentence"] = abs(pa["sentence"] - pb["sentence"])
        f["text:both_digit"] = float(pa["digit"] > 0.3 and pb["digit"] > 0.3)
        ca, cb = self.context(i), self.context(j)
        inter = sum(min(ca[c], cb[c]) for c in ca.keys() & cb.keys())
        union = sum(ca.values()) + sum(cb.values()) - inter
        f["context:jaccard"] = inter / union if union > 0 else 0.0
        f["context:empty"] = float(not ca or not cb)
        f["context:size_ratio"] = abs(math.log((1 + len(ca)) / (1 + len(cb))))
        gap = max(0, max(a.t_start, b.t_start) - min(a.t_end, b.t_end)) / 1e6
        f["time:log_gap"] = math.log1p(gap)
        f["time:same_win"] = float(a.t_start // 60_000_000 == b.t_start // 60_000_000)
        f["time:cooccur"] = float(gap == 0)
        f["time:dur_ratio"] = abs(math.log((1 + a.t_end - a.t_start) / (1 + b.t_end - b.t_start)))
        f["block:d_log"] = abs(math.log(self.block(i) / self.block(j)))
        f["block:min"] = math.log(min(self.block(i), self.block(j)))
        f["motion:any"] = float(a.moving or a.scrolling or b.moving or b.scrolling)
        f["motion:both"] = float((a.moving or a.scrolling) and (b.moving or b.scrolling))
        names = list(f)
        assert names == FEATURE_NAMES, "pair() 的特征顺序和 FEATURE_NAMES 对不上"
        return [f[k] for k in names], names
