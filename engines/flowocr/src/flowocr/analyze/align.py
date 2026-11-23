"""区域的**对齐方式**（左 / 居中 / 右）：阶段 2 判好写进 `regions[].align`，叠加导出只照着锚边。

方案：overlay-style 计划的「对齐」一节（owner 2026-09-24：叠加默认居中，原文多行左对齐时靠左）。

两级判据，**区域永远有值**：

1. **区域统计**：区域里可用事件的左沿 / 中心 / 右沿，各自的四分位距除以中位行高。
   某一个 ≤ `TOL` 行高、而且 ≤ 另两者较小值的 `DOMINANCE` 倍，就判成它。
   为什么用区域统计当主判据：左对齐对话框里的**单行台词**，只看邻居的话，旁边正好挂着名牌时才判得出。
   判不出的情况：行宽几乎不变（三个量一样稳）、样本太少、聚类误合并把统计搅散。
2. **成对投票**（owner 说的"上下附近有左边界一致的其他字幕"）：同时在屏、上下相邻的两行，
   左沿齐而中心不齐 → 一票 `left`，右沿对称；中心齐而两沿都不齐 → 一票 `center`。
   赢家要 ≥ `MIN_VOTES` 票且严格多于别的；平局或票不够 → `center`。

可用事件 = 不在动（`moving`：框跟着字走，对齐没有意义）、不是 `scrolling`（它的框是整条滚动的并集）、
不是名牌（短、位置固定，会把统计拉偏）、没被判成 UI。UI 用 `tracksio.ui_flagged`：
这里的作用域正是**区域轨**自己，它上面的 `ui_filtered` 就是这条轨的判决。
名牌不进统计，但可以当投票的邻居（名牌 + 正文左沿齐，正是左对齐对话框的样子）。
"""
from __future__ import annotations

import statistics
from collections import Counter

from flowocr.artifacts import tracksio

TOL = 0.5
"""离散度 / 位置差的容差，单位是中位行高。"""
DOMINANCE = 0.5
"""区域统计的赢家要 ≤ 另两者较小值的这么多倍，才算"明显更稳"。"""
MIN_EVENTS = 5
"""区域统计至少要这么多个可用事件。"""
WIDTH_MIN_IQR = 1.0
"""行宽的四分位距不到这么多行高：三条边一样稳，统计分不出，交给投票。"""
NEIGHBOR_GAP = 1.5
"""投票的邻居：上下两框的垂直间隙不超过这么多行高。"""
MIN_VOTES = 2
"""投票的赢家至少要这么多票（1 票太容易是一对误读的框）。"""


def usable(ev: dict) -> bool:
    flags = set(ev.get("flags") or ())
    b = ev["box"]
    return (not flags & {"moving", "scrolling", "nameplate"} and not tracksio.ui_flagged(ev)
            and b[3] > b[1] and b[2] > b[0])


def iqr(xs: list[float]) -> float:
    q = statistics.quantiles(xs, n=4, method="inclusive")
    return q[2] - q[0]


def by_stats(evs: list[dict]) -> tuple[str | None, dict | None]:
    """区域统计。返回 (判定或 None, 三个离散度)；样本不够时离散度也是 None。"""
    if len(evs) < MIN_EVENTS:
        return None, None
    h = statistics.median(e["box"][3] - e["box"][1] for e in evs)
    x0 = [e["box"][0] for e in evs]
    x1 = [e["box"][2] for e in evs]
    cx = [(a + b) / 2 for a, b in zip(x0, x1)]
    spread = {"left": iqr(x0) / h, "center": iqr(cx) / h, "right": iqr(x1) / h}
    rounded = {k: round(v, 3) for k, v in spread.items()}
    if iqr([b - a for a, b in zip(x0, x1)]) / h < WIDTH_MIN_IQR:
        return None, rounded
    best = min(spread, key=spread.get)
    others = min(v for k, v in spread.items() if k != best)
    # 严格小于：两条边一样稳（比如都是 0）时分不出，别按 dict 顺序挑一个
    if spread[best] <= TOL and spread[best] < DOMINANCE * others:
        return best, rounded
    return None, rounded


def pair_vote(a: list[float], b: list[float]) -> str | None:
    """上下相邻的两行投一票：左沿齐而中心不齐 → left；右沿对称；中心齐而两沿都不齐 → center；其余不投。"""
    tol = TOL * (a[3] - a[1] + b[3] - b[1]) / 2
    d0, d1 = abs(a[0] - b[0]), abs(a[2] - b[2])
    dc = abs((a[0] + a[2]) / 2 - (b[0] + b[2]) / 2)
    if dc > tol and d0 <= tol < d1:
        return "left"
    if dc > tol and d1 <= tol < d0:
        return "right"
    if dc <= tol and d0 > tol and d1 > tol:
        return "center"
    return None


def adjacent(a: list[float], b: list[float]) -> bool:
    """上下相邻：左右有重叠，垂直间隙不超过 `NEIGHBOR_GAP` 行高。"""
    if min(a[2], b[2]) <= max(a[0], b[0]):
        return False
    gap = max(a[1], b[1]) - min(a[3], b[3])
    return 0 <= gap <= NEIGHBOR_GAP * (a[3] - a[1] + b[3] - b[1]) / 2


def votes(own: list[dict], names: list[dict]) -> Counter:
    """区域里同时在屏、上下相邻的每一对投一票（每对只投一次）。至少一方是本区域的事件；
    另一方可以是本区域的，也可以是名牌。按 `t_start` 扫，只比时间有交叠的。"""
    own_ids = {e["id"] for e in own}
    evs = sorted({e["id"]: e for e in [*own, *names]}.values(), key=lambda e: e["t_start"])
    out: Counter = Counter()
    active: list[dict] = []
    for e in evs:
        active = [a for a in active if a["t_end"] > e["t_start"]]
        for a in active:
            if (a["id"] in own_ids or e["id"] in own_ids) and adjacent(a["box"], e["box"]):
                v = pair_vote(a["box"], e["box"])
                if v:
                    out[v] += 1
        active.append(e)
    return out


def judge(own: list[dict], names: list[dict]) -> tuple[str, dict]:
    """一个区域的对齐方式和依据（`align_how`）。"""
    got, spread = by_stats(own)
    how = {"by": "stats", "n": len(own), "spread": spread, "votes": {}}
    if got:
        return got, how
    tally = votes(own, names)
    how.update(by="vote", votes=dict(sorted(tally.items())))
    ranked = tally.most_common()
    if ranked and ranked[0][1] >= MIN_VOTES and (len(ranked) == 1 or ranked[0][1] > ranked[1][1]):
        return ranked[0][0], how
    how["by"] = "none"
    return "center", how


def annotate(doc: dict) -> Counter:
    """给每个区域写 `align` / `align_how`（原地改 `doc`），返回各种判定的区域数。"""
    by_region: dict[int, list[dict]] = {}
    names = []
    for ev in doc["events"]:
        flags = set(ev.get("flags") or ())
        if "nameplate" in flags and not tracksio.ui_flagged(ev) and "moving" not in flags:
            names.append(ev)
        if usable(ev):
            by_region.setdefault(ev["region"], []).append(ev)
    n = Counter()
    for r in doc["regions"]:
        own = by_region.get(r["index"], [])
        near = []
        if own:
            # 只带区域外接框附近的名牌进来：名牌是全片的，每个区域都扫一遍全体太浪费
            h = max(e["box"][3] - e["box"][1] for e in own)
            x0, x1 = min(e["box"][0] for e in own), max(e["box"][2] for e in own)
            y0, y1 = min(e["box"][1] for e in own) - 2 * h, max(e["box"][3] for e in own) + 2 * h
            near = [e for e in names if e["box"][2] > x0 and e["box"][0] < x1
                    and e["box"][3] > y0 and e["box"][1] < y1]
        r["align"], r["align_how"] = judge(own, near)
        n[(r["align"], r["align_how"]["by"])] += 1
    return n
