"""阶段 4：打字机行的底板跟着字长（owner 2026-10-05）。

打字机行在 t 时刻的底板 = **原文已出现的部分 ∪ 译文已显出的部分**（`text` 方案只算译文），一个字都还没出现时不画。
每一段会长的东西从一个左沿往右长，右沿随时间是一条折线（`Front`）。底板仍画成静态矩形，外加一个随时间变的矩形 `\\clip`：
libass 里 `\\t(a,b,\\clip(…))` 四个坐标一起线性插值，所以右沿的匀速段直接是一个 `\\t`；跳变（逐字显出、新的一段出现、
左 / 上 / 下沿变）放进 `\\t(t,t+1,…)`，分段在每个跳变时刻切开，不然会被插值拖出斜边。
这里只有几何和时间，不碰 ASS 行的其余部分；谁调它、怎么拼生成行在 `core`。"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from itertools import combinations


Front = list[tuple[float, float]]
"""随时间变的一个坐标：(毫秒, 值) 按时刻排。第一个时刻之前还不存在（None）；相邻两点之间线性；
同一时刻的两个点是跳（先左极限、后右值）；最后一点之后不变。"""

SAME_LEFT = 12
"""两段的左沿差不超过这么多像素就当成同一个左沿、不切块（原来写成两倍底板边距，边距加到 14 之后和它脱钩）：原文重画在原文上时两边只差一两个像素，
切出来的左块只有几像素宽，白多一条生成行。多盖的这一条在边距以内。"""

EMPTY = (0, 0, 0, 0)
"""还没出现时的 clip：零面积，什么都不画。"""


def const(t: float, v: float) -> Front:
    """从 `t` 起一直是 `v`。"""
    return [(t, v)]


def ramp(s: float, f: float, v0: float, v1: float) -> Front:
    """`s` 时出现在 `v0`，匀速到 `f` 时的 `v1`，之后不变；`f <= s` 就是 `s` 时直接到 `v1`。"""
    return [(s, v0), (f, v1)] if f > s else [(s, v1)]


def steps(points: list[tuple[float, float]]) -> Front:
    """逐个跳上去的右沿：`points` 是（时刻, 跳到的值）。只往上跳（后显出的字不会让右沿往回缩），同一时刻取最大的。"""
    out: Front = []
    for t, v in sorted(points):
        if out and out[-1][0] == t:
            out[-1] = (t, max(out[-1][1], v))
            continue
        if out:
            if v <= out[-1][1]:
                continue
            out.append((t, out[-1][1]))
        out.append((t, v))
    return out


def at(fr: Front, t: float) -> float | None:
    """`t` 时的值（右值：同一时刻跳了就是跳之后的）；还没出现是 None。"""
    if not fr or t < fr[0][0]:
        return None
    times = [p[0] for p in fr]
    i = bisect_right(times, t) - 1
    if i == len(fr) - 1:
        return fr[i][1]
    (t0, v0), (t1, v1) = fr[i], fr[i + 1]
    return v0 + (v1 - v0) * (t - t0) / (t1 - t0)


def before(fr: Front, t: float) -> float | None:
    """`t` 之前一瞬的值（左极限）；`t` 时才出现的是 None。"""
    if not fr or t <= fr[0][0]:
        return None
    times = [p[0] for p in fr]
    i = bisect_left(times, t) - 1
    if i == len(fr) - 1:
        return fr[i][1]
    (t0, v0), (t1, v1) = fr[i], fr[i + 1]
    return v0 + (v1 - v0) * (t - t0) / (t1 - t0)


def envelope(fronts: list[Front], op=max) -> Front:
    """几条折线逐时刻取 `op`（max / min），只算已经出现的；结果仍是折线（相邻两点之间线性）。
    两两在一段里交叉的时刻补成节点，不然交叉处会被当成直线拉过去。"""
    fronts = [f for f in fronts if f]
    times = sorted({t for f in fronts for t, _ in f})
    cross = set()
    for a, b in zip(times, times[1:]):
        segs = [(at(f, a), before(f, b)) for f in fronts if at(f, a) is not None]
        for (p0, p1), (q0, q1) in combinations(segs, 2):
            d0, d1 = p0 - q0, p1 - q1
            if d0 * d1 < 0:
                cross.add(a + (b - a) * d0 / (d0 - d1))
    out: Front = []
    for t in sorted(set(times) | cross):
        left = [v for f in fronts if (v := before(f, t)) is not None]
        right = [v for f in fronts if (v := at(f, t)) is not None]
        lv, rv = (op(left) if left else None), op(right)
        if lv is not None and lv != rv:
            out.append((t, lv))
        out.append((t, rv))
    return simplify(out)


def simplify(fr: Front, tol: float = 0.5) -> Front:
    """去掉落在前后两点连线上的中间点（误差 `tol` 像素以内）：交叉补的节点在另一条线没赢的地方是多余的，
    留着会把一段匀速拆成一串 `\\t`。跳（同一时刻两个点）不动。"""
    out: Front = []
    for p in fr:
        if len(out) >= 2:
            (t0, v0), (t1, v1) = out[-2], out[-1]
            if t0 < t1 < p[0] and abs(v0 + (p[1] - v0) * (t1 - t0) / (p[0] - t0) - v1) <= tol:
                out.pop()
        out.append(p)
    return out


def clip_tags(x0: Front, y0: Front, x1: Front, y1: Front) -> str:
    """四条边 → `\\clip(…)` + 一串 `\\t(…,\\clip(…))`（时刻是相对这一行起点的毫秒）。四条边都出现之前是零面积的 clip。
    边上的值取整（两块底板拼接时共用同一个整数坐标，不留缝）；右沿小于左沿（底板外的一段）按零宽算。"""
    edges = (x0, y0, x1, y1)
    times = sorted({t for f in edges for t, _ in f if t >= 0})

    def rect(get, t):
        v = [get(f, t) for f in edges]
        if any(c is None for c in v):
            return EMPTY
        a, b, c, d = (round(c) for c in v)
        return a, b, max(a, c), max(b, d)

    def clip(r) -> str:
        return "\\clip(%d,%d,%d,%d)" % r
    cur = rect(at, 0)
    out = [clip(cur)]
    for i, t in enumerate(times):
        ti, jumped = round(t), False
        r = rect(at, t)
        if r != cur and t > 0:
            out.append("\\t(%d,%d,%s)" % (ti, ti + 1, clip(r)))
            cur, jumped = r, True
        if i + 1 < len(times):
            nxt = rect(before, times[i + 1])
            if nxt != cur:
                a = ti + jumped
                out.append("\\t(%d,%d,%s)" % (a, max(a + 1, round(times[i + 1])), clip(nxt)))
                cur = nxt
    return "".join(out)


Piece = tuple[float, float, float, Front]
"""一段会长的东西：（左沿, 上沿, 下沿, 右沿）。右沿第一次出现的时刻就是它出现的时刻。"""


def row_clips(static: list[float], parties: list[tuple[float, Front]]) -> list[str]:
    """逐行摆的一行（`rows` / `text` 方案）：`parties` 是这一行会长的几段（原文、译文；`text` 方案只有译文），各自（左沿, 右沿）。
    返回这一行要画的底板各自的 clip（每块都画 `static` 这个矩形，靠 clip 裁）。

    两段的左沿一般不在一处（居中行、两边宽度不同），一个矩形 clip 会把两段之间的空档也盖上，所以在**靠右的那个左沿**处切成两块：
    左块 `[靠左的左沿, 切点]` 只有左沿靠左的那段长得进来；右块 `[切点, …]` 两段都从切点往右长（靠左那段要先长过切点）。
    两块不重叠（半透明不会叠成更黑），拼起来正好是并集；左沿相同（或只有一段）时只有一块。"""
    parties = [(lft, f) for lft, f in parties if f]
    if not parties:
        return []
    y0, y1, right = static[1], static[3], static[2]
    cut = max(lft for lft, _ in parties)
    if cut - min(lft for lft, _ in parties) <= SAME_LEFT:
        parties = [(min(lft for lft, _ in parties), f) for _, f in parties]
        cut = parties[0][0]
    out = []
    lefty = [(lft, f) for lft, f in parties if lft < cut]
    if lefty:
        lft, f = min(lefty, key=lambda p: p[0])
        t0 = f[0][0]
        out.append(clip_tags(const(t0, lft), const(t0, y0), envelope([f, const(t0, cut)], min), const(t0, y1)))
    reach = envelope([const(0, cut)] + [f for _, f in parties], max)
    out.append(clip_tags(const(0, cut), const(0, y0), envelope([reach, const(0, right)], min), const(0, y1)))
    return out


def bbox_clip(static: list[float], pieces: list[Piece]) -> str:
    """整块画的底板（`box` 方案、`single` / `free`）：已出现的各段的外接矩形，限在 `static` 里；四条边都跟着走。"""
    pieces = [p for p in pieces if p[3]]
    if not pieces:
        return clip_tags([], [], [], [])

    def started(v, p):
        return const(p[3][0][0], v)
    t0 = min(p[3][0][0] for p in pieces)            # 限在 static 里的那几条也从第一段出现时才有，之前是零面积
    x0 = envelope([envelope([started(p[0], p) for p in pieces], min), const(t0, static[0])], max)
    y0 = envelope([envelope([started(p[1], p) for p in pieces], min), const(t0, static[1])], max)
    x1 = envelope([envelope([p[3] for p in pieces], max), const(t0, static[2])], min)
    y1 = envelope([envelope([started(p[2], p) for p in pieces], max), const(t0, static[3])], min)
    return clip_tags(x0, y0, x1, y1)
