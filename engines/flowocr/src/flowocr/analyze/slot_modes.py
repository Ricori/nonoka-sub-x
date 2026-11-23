"""**原型**：跨窗缝合换成"锚点模态"——不依赖顺序、签名不会漂（next-steps 的方案 A 往下再走一步）。

现行 `stitch_slots` 有三处叠在一起的病（读代码看到的，不是猜的）：

1. 共边判据取 `min(|Δy0|, |Δy1|)`——**两条边谁近认谁**，同一个槽位可以先靠上边收一个、再靠下边收另一个；
2. 签名是成员的**池化并集**，收一个漂一点（`--slot-geom median` 想治它，五部上方向不一致）；
3. 贪心按 run 数降序挑种子，**结果依赖顺序**（任何改签名的动作都会把分配重洗一遍）。

这里三处一起换掉：

* **锚点先于分配定死**：全片所有窗区域的上边 / 下边 / 竖直中心、左边 / 右边 / 水平中心、字高（对数），
  各自做一维密度估计取**峰**。一个窗区域 = 一票（于是一个时间窗对一个峰至多贡献它的区域数，
  密集 OCR 的几百条 run 主导不了——方案 B 的"每窗权重设上限"在这里是结构性的）。峰定了就不再动。
* **对齐方式由整个槽位共同决定**：候选槽位 = 联合格子 `(y 锚类型, y 峰, x 锚类型, x 峰, 字高峰)`，
  3 × 3 = 9 种对齐组合。同一条带 1 行和 3 行的实例共享"不动的那条边"的峰，落在同一个格子里；
  **高度变化本身不构成驱逐理由**。
* **按支持度剥离**：每轮取**覆盖不同时间窗最多**的格子，把它里面还没分配的窗区域整体立为一个槽位，
  再重数。先立住的是"最典型的"，不是"最大的"；和输入顺序无关（平手按格子键排序）。

⚠ 已知管不了的：**不同时刻占同一位置的不同元素**（纯几何分不开）、**随时间迁移的带**（峰是全片的）。
先拿尺量，再决定要不要加时间。

用法：
    from flowocr.analyze import slot_modes; slots = slot_modes.stitch(L)        # L 来自 cluster_layers.load
    python -m flowocr.analyze.slot_modes f5                           # 打印槽位统计
"""
from __future__ import annotations

import bisect
import math
import sys
from collections import defaultdict


BW_Y, BW_X, BW_H = 0.012, 0.015, math.log(1.25)
"""核宽：y / x 是画面高 / 宽的比例（1080p 上约 13 px / 29 px），字高是对数比。"""
H_GAP = math.log(1.3)
"""同一个槽位里字高排序后相邻两个的比超过它就断开（现行 `slot_fits` 的字高比门是 1.7，那个是对签名比的）。"""
MIN_SUPPORT = 2
"""格子至少覆盖这么多个时间窗才立槽；再小的各自成单例槽（不硬凑）。"""


def peaks(vals: list[float], bw: float) -> list[float]:
    """一维三角核密度的局部极大（每个样本一票）。返回峰的位置，升序。"""
    xs = sorted(vals)
    if not xs:
        return []

    def dens(v: float) -> float:
        lo, hi = bisect.bisect_left(xs, v - bw), bisect.bisect_right(xs, v + bw)
        return sum(1 - abs(xs[k] - v) / bw for k in range(lo, hi))
    uniq = sorted(set(xs))
    d = [dens(v) for v in uniq]
    out = []
    for i, v in enumerate(uniq):
        lo, hi = bisect.bisect_left(uniq, v - bw), bisect.bisect_right(uniq, v + bw)
        if d[i] >= max(d[lo:hi]) and (not out or v - out[-1] > bw):
            out.append(v)
    return out


def snap(v: float, pk: list[float], bw: float) -> int:
    """最近的峰的下标；超出 1.5 个核宽就不属于任何峰（-1）。"""
    i = bisect.bisect_left(pk, v)
    best = min((j for j in (i - 1, i) if 0 <= j < len(pk)), key=lambda j: abs(pk[j] - v), default=-1)
    return best if best >= 0 and abs(pk[best] - v) <= 1.5 * bw else -1


def stitch(L, bw_y: float = BW_Y, bw_x: float = BW_X, min_support: int = MIN_SUPPORT) -> list[list[int]]:
    wr = L.win_regions
    from flowocr.analyze import build_tracks as bt          # 在这里 import：`slot_lines` 只借本模块的 peaks / snap，别把管线拖进来
    g = [bt.region_geom(L.runs, w["runs"]) for w in wr]          # (cx, x0, x1, y0, y1, h)
    feats = {
        "yt": [q[3] / L.H for q in g], "yb": [q[4] / L.H for q in g], "ym": [(q[3] + q[4]) / 2 / L.H for q in g],
        "xl": [q[1] / L.W for q in g], "xr": [q[2] / L.W for q in g], "xc": [q[0] / L.W for q in g],
        "h": [math.log(max(1.0, q[5])) for q in g],
    }
    bws = {"yt": bw_y, "yb": bw_y, "ym": bw_y, "xl": bw_x, "xr": bw_x, "xc": bw_x, "h": BW_H}
    pid = {}
    for name, vals in feats.items():
        pk = peaks(vals, bws[name])
        pid[name] = [snap(v, pk, bws[name]) for v in vals]

    # 字高**不当锚点**（第一版当了）：它是个软量，量化成峰之后 36 和 38 会落在两个峰的两侧，
    # 同一条对话带（cx 相同、上边差 5 px）就这么被拆开（gi2-s1-023 / 038，标注是 same）。
    # 改成剥离时的相容性检查：格子里按 log 字高排序、相邻差 > H_GAP 处断开，取最大的那一段。
    keys_of: list[list[tuple]] = []
    for i in range(len(wr)):
        ks = []
        for ty in ("yt", "yb", "ym"):
            for tx in ("xl", "xc", "xr"):
                if pid[ty][i] >= 0 and pid[tx][i] >= 0:
                    ks.append((ty, pid[ty][i], tx, pid[tx][i]))
        keys_of.append(ks)

    def h_major(mem: set[int]) -> list[int]:
        order = sorted(mem, key=lambda i: feats["h"][i])
        runs_, cur = [], [order[0]]
        for a_, b_ in zip(order, order[1:]):
            if feats["h"][b_] - feats["h"][a_] > H_GAP:
                runs_.append(cur)
                cur = []
            cur.append(b_)
        runs_.append(cur)
        return max(runs_, key=lambda r: (len({wr[i]["win"] for i in r}), -r[0]))

    cell: dict[tuple, set[int]] = defaultdict(set)
    for i, ks in enumerate(keys_of):
        for k in ks:
            cell[k].add(i)
    free = set(range(len(wr)))
    slots: list[list[int]] = []
    while True:
        best, best_s = None, 0
        for k, mem in cell.items():
            if len(mem) < max(best_s, min_support):
                continue                                   # 成员数是窗数的上界，先剪
            s = len({wr[i]["win"] for i in h_major(mem)})
            if s > best_s or (s == best_s and best is not None and k < best):
                best, best_s = k, s
        if best is None or best_s < min_support:
            break
        mem = sorted(h_major(cell[best]))
        slots.append(mem)
        for i in mem:
            free.discard(i)
            for k in keys_of[i]:
                cell[k].discard(i)
        cell = {k: v for k, v in cell.items() if v}
    slots.extend([i] for i in sorted(free))
    return slots


if __name__ == "__main__":
    from flowocr.analyze import cluster_layers as CL
    for tag in sys.argv[1:]:
        L = CL.load(tag)
        sl = stitch(L)
        multi = [s for s in sl if len(s) > 1]
        nrun = sorted((sum(len(L.win_regions[w]["runs"]) for w in s) for s in sl), reverse=True)
        print(f"{tag}: 窗区域 {len(L.win_regions)} -> 槽位 {len(sl)}（多成员 {len(multi)}、单例 {len(sl) - len(multi)}）；"
              f"最大的五个槽位 run 数 {nrun[:5]}，单例里的 run 占 "
              f"{sum(len(L.win_regions[s[0]]['runs']) for s in sl if len(s) == 1) / len(L.runs):.1%}")
