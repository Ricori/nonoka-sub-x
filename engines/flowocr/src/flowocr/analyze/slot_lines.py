"""**原型二**：在**行（run）这一层**找锚点模态，再把行位并成带——完全不经过窗区域（next-steps）。

为什么要有它：`slot_modes`（原型一）在窗区域的几何上找峰，而标注一出来就看到**窗区域的几何本身不稳**——
同一个名牌，这个窗里单独成区域（字高 76），那个窗里和别的东西并成一块（字高 148）；同一条对话带的
y 跨度随这一分钟里出现过几行而变。在那一层做任何几何缝合，上限都被它压着。

而**一行字**的位置是硬的：版式把第 1 / 2 / 3 行各钉在固定的 y 上，左对齐的行 x0 不动、居中的行 cx 不动。
"同一条带 1 行变 3 行、总有一条边不动"在这一层不需要专门处理——行位本来就在一张固定网格上。

两步：

1. **行位**：对全片 run 的 `y 中心`、`x0 / cx / x1` 各做一维密度峰（一个时间窗对一个格子只算一票，
   密集 OCR 主导不了）。格子 = `(y 峰, x 锚类型, x 峰)`，按**覆盖的时间窗数**从大到小剥离；
   字高只做相容性检查（同 `slot_modes.h_major` 的理由）。
2. **带**：两个行位并成一条带，当且仅当 x 锚对得上、字高相容，并且竖直方向**相邻**（间隙 ≤ `GAP_H` 倍字高：
   多行正文的上下行）**或重叠**（居中版式里 1 行和 2 行的位置互斥、但压在同一块地方）。
   这一步是并查集——串联的风险被"同一 x 锚 + 同字高"限住了：串起来的顶多是同一列菜单项（标注里是 `widget`，不计分）。

支持度不够的 run（行位覆盖 < `MIN_SUPPORT` 个窗）各自成单例。

管线（`build_tracks --cluster lines`）在两步之上再并"同刻同框的上下行"（`band_cooccur`），散 run 回落到缝合分组
（`groups_for_pipeline`）；聚类的尺量的就是这份分组（`slot_pairs.pipeline_assign`）。

用法：
    python -m flowocr.analyze.slot_lines f5 gi2        # 各片的带数、最大的几条带（散 run 各自成单例）
"""
from __future__ import annotations

import math
import statistics
import sys
from collections import defaultdict

from flowocr.analyze.slot_modes import H_GAP, peaks, snap  # noqa: E402

BW_Y, BW_X = 0.008, 0.012
"""核宽（画面高 / 宽的比例；1080p 上约 9 px / 23 px）。行的 y 比窗区域的边硬得多，所以比 `slot_modes` 的窄。"""
MIN_SUPPORT = 2
GAP_H = 0.9
"""两个行位的竖直间隙不超过这么多倍字高，才算上下相邻的行。"""
COL_MIN_W = 0.15
"""『同一栏』规则只对至少这么宽（画面宽的比例）的行位生效——窄的 HUD 数字左右边对齐是常态，不说明什么。"""
WINDOW_US = 60_000_000


def line_slots(L, bw_y: float = BW_Y, bw_x: float = BW_X, min_support: int = MIN_SUPPORT):
    """第 1 步：行位。返回 `(lines, free, sig)`——`lines[k] = (格子键, 成员 run)`、`free` 是没进任何行位的 run、
    `sig[k]` 是行位的几何摘要。单独拿出来是因为"行位之间并不并"可以换别的判据（`slot_learned.py`）。"""
    runs = L.runs
    n = len(runs)
    win = [r.t_start // WINDOW_US for r in runs]
    f = {"ym": [r.cy / L.H for r in runs],
         "xl": [r.box[0] / L.W for r in runs], "xc": [r.cx / L.W for r in runs], "xr": [r.box[2] / L.W for r in runs]}
    lh = [math.log(max(1.0, r.h)) for r in runs]
    pid = {}
    for name, vals in f.items():
        bw = bw_y if name == "ym" else bw_x
        pk = peaks(vals, bw)
        pid[name] = [snap(v, pk, bw) for v in vals]

    keys_of = [[(pid["ym"][i], tx, pid[tx][i]) for tx in ("xl", "xc", "xr") if pid[tx][i] >= 0]
               if pid["ym"][i] >= 0 else [] for i in range(n)]
    cell: dict[tuple, set[int]] = defaultdict(set)
    for i, ks in enumerate(keys_of):
        for k in ks:
            cell[k].add(i)

    def h_major(mem: set[int]) -> list[int]:
        order = sorted(mem, key=lambda i: lh[i])
        segs, cur = [], [order[0]]
        for a, b in zip(order, order[1:]):
            if lh[b] - lh[a] > H_GAP:
                segs.append(cur)
                cur = []
            cur.append(b)
        segs.append(cur)
        return max(segs, key=lambda s: (len({win[i] for i in s}), -s[0]))

    lines: list[tuple[tuple, list[int]]] = []               # (格子键, 成员 run)
    free = set(range(n))
    support = {k: len({win[i] for i in m}) for k, m in cell.items()}     # 上界：剥离只会让它变小
    while True:
        best, best_s, best_mem = None, 0, None
        for k in sorted(support, key=lambda k: -support[k]):
            if support[k] < max(best_s, min_support):
                break                                       # 后面的上界都不够了
            mem = h_major(cell[k])
            s = len({win[i] for i in mem})
            support[k] = len({win[i] for i in cell[k]})
            if s > best_s or (s == best_s and k < best):
                best, best_s, best_mem = k, s, mem
        if best is None or best_s < min_support:
            break
        lines.append((best, sorted(best_mem)))
        for i in best_mem:
            free.discard(i)
            for k in keys_of[i]:
                cell[k].discard(i)
                support[k] = min(support[k], len(cell[k]))
        for k in [k for k, m in cell.items() if not m]:
            del cell[k], support[k]

    med = statistics.median
    sig = []
    for (py, tx, px), mem in lines:
        sig.append({"tx": tx, "y0": med(runs[i].box[1] for i in mem), "y1": med(runs[i].box[3] for i in mem),
                    "h": med(runs[i].h for i in mem),
                    "xl": med(f["xl"][i] for i in mem), "xc": med(f["xc"][i] for i in mem), "xr": med(f["xr"][i] for i in mem)})
    return lines, free, sig


def band_of(sig: list[dict], bw_x: float = BW_X, gap_h: float = GAP_H, column: bool = True) -> list[int]:
    """第 2 步：行位 -> 带（并查集）。返回每个行位的带号。`column=False` 只用"相邻行"那条规则。"""
    lines = sig
    parent = list(range(len(lines)))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def h_ok(A: dict, B: dict) -> bool:
        return max(A["h"], B["h"]) / max(1.0, min(A["h"], B["h"])) <= math.exp(H_GAP)

    # **同一栏**：左右两条边都对齐 + 字高相容 + 够宽，就不看 y（阅读面板里隔得远的段落、两端对齐的说明文）。
    # 对话正文的右边随句长变，这条规则碰不到它。dev 上率层 23 -> 18（n=103，小样本，别当成已证）。
    for a in range(len(lines) if column else 0):
        for b in range(a + 1, len(lines)):
            A, B = sig[a], sig[b]
            if (A["xr"] - A["xl"] >= COL_MIN_W and abs(A["xl"] - B["xl"]) <= 1.5 * bw_x
                    and abs(A["xr"] - B["xr"]) <= 1.5 * bw_x and h_ok(A, B)):
                parent[find(a)] = find(b)

    order = sorted(range(len(lines)), key=lambda k: sig[k]["y0"])
    for ai, a in enumerate(order):
        A = sig[a]
        for b in order[ai + 1:]:
            B = sig[b]
            if B["y0"] - A["y1"] > gap_h * max(A["h"], B["h"]):
                break                                       # 按 y0 排过序：再往后只会更远
            # **两个行位得是同一种 x 锚、且锚点对得上**。第一版是"任一方的锚对得上就并"，并查集于是一路串：
            # 存档菜单的 `NO DATA` / `Empty` 被串进对话正文那条带（dev 误合并 15 -> 2）。
            if A["tx"] != B["tx"] or abs(A[A["tx"]] - B[A["tx"]]) > 1.5 * bw_x or not h_ok(A, B):
                continue
            parent[find(a)] = find(b)
    return [find(k) for k in range(len(lines))]


COOCCUR = 0.5
"""同刻同框：两个行位里较小那个的 run 至少这么大比例和对方的某条 run 时间重叠，才按"同一个框的上下行"并。"""


def band_cooccur(lines: list, sig: list[dict], runs: list, band: list[int], gap_h: float = GAP_H) -> list[int]:
    """在 `band_of` 之上再并一步：**同刻同框的上下行**。`band_of` 要求两行的 x 锚类型相同，
    居中的多行对话框里各行被剥离成不同锚（有的按左边、有的按中心），于是一句多行台词被拆进几条带，
    喂给匹配器时每条 cue 只有一行、对不上整句（09-26 产物 A/B：zzz 整场丢的 434 句里 432 句是这个形状）。
    这里不看锚类型，只要竖直相邻、横向有重叠、字高相容，且较小那个行位的 run 大多和对方同时在场。"""
    parent = {b: b for b in band}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    iv = [sorted((runs[i].t_start, runs[i].t_end) for i in mem) for _, mem in lines]

    def co_frac(a: int, b: int) -> float:
        A, B = iv[a], iv[b]
        if len(A) > len(B):
            A, B = B, A
        hit, j = 0, 0
        for s, e in A:
            while j < len(B) and B[j][1] <= s:
                j += 1
            k = j
            while k < len(B) and B[k][0] < e:
                if B[k][1] > s:
                    hit += 1
                    break
                k += 1
        return hit / max(1, len(A))

    order = sorted(range(len(lines)), key=lambda k: sig[k]["y0"])
    for ai, a in enumerate(order):
        A = sig[a]
        for b in order[ai + 1:]:
            B = sig[b]
            if B["y0"] - A["y1"] > gap_h * max(A["h"], B["h"]):
                break
            if find(band[a]) == find(band[b]):
                continue
            ov = min(A["xr"], B["xr"]) - max(A["xl"], B["xl"])
            if ov <= 0.3 * min(A["xr"] - A["xl"], B["xr"] - B["xl"]):
                continue
            if max(A["h"], B["h"]) / max(1.0, min(A["h"], B["h"])) > math.exp(H_GAP):
                continue
            if co_frac(a, b) >= COOCCUR:
                parent[find(band[a])] = find(band[b])
    return [find(b) for b in band]


def groups_for_pipeline(runs: list, W: int, H: int, fallback: dict[int, int] | None = None) -> list[list[int]]:
    """给 `build_tracks --cluster lines` 用（实验性）：run 列表 -> 每个区域的 run 下标。行位 -> 带（`band_of`）再并同刻同框的上下行（`band_cooccur`），
    **没进任何行位的散 run** 按 `fallback`（run 下标 -> 现行缝合给它的槽位号）归堆，同 `slot_learned.groups_for_pipeline`：
    不归堆的话单字残片各自成一个 noise 区域，逐区域产物多出几百个文件。"""
    from types import SimpleNamespace
    lines, free, sig = line_slots(SimpleNamespace(runs=runs, W=W, H=H))
    band = band_cooccur(lines, sig, runs, band_of(sig))       # 管线要同刻同框的几行在一条轨上
    by: dict = {}
    for k, (_, mem) in enumerate(lines):
        for i in mem:
            by.setdefault(("b", band[k]), []).append(i)
    for i in sorted(free):
        key = ("fb", fallback[i]) if fallback is not None and i in fallback else ("s", i)
        by.setdefault(key, []).append(i)
    print(f"  行位 {len(lines)} 个 -> 带 {len(set(band))} 条；散 run {len(free)} 条"
          + (f" -> 回落到缝合分组 {sum(1 for k in by if k[0] == 'fb')} 个区域" if fallback is not None else ""), flush=True)
    return [sorted(v) for v in by.values()]


if __name__ == "__main__":
    from flowocr.analyze import cluster_layers as CL
    from collections import Counter
    for tag in sys.argv[1:]:
        L = CL.load(tag)
        asg = {i: k for k, g in enumerate(groups_for_pipeline(L.runs, L.W, L.H)) for i in g}
        c = Counter(asg.values())
        big = c.most_common(6)
        single = sum(1 for v in c.values() if v == 1)
        print(f"{tag}: run {len(L.runs)} -> 带 {len(c)}（单例 {single}，占 run 的 {single / len(L.runs):.1%}）；"
              f"最大的六条带 run 数 {[v for _, v in big]}")
        for k, v in big:
            mem = [i for i, s in asg.items() if s == k]
            ys = sorted(L.runs[i].cy for i in mem)
            txt = Counter(L.runs[i].text for i in mem[:400]).most_common(2)
            print(f"    {v:6} run  cy {ys[len(ys) // 20]:.0f}~{ys[-len(ys) // 20 - 1]:.0f}  {txt}")
