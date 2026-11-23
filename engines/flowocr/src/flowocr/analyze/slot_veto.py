"""**原型三**：现行缝合（`median` 签名）+ **文本形态否决**（next-steps，owner 2026-09-21 让逐一试的思路之一）。

为什么是"否决"而不是新算法：`median` 在尺上已经是最好的一条臂，它剩下的错里**误合并**这一半有个共同形状——
不同时刻占同一位置的不同元素（对话正文 vs 存档菜单的 `Empty` / 标题菜单按钮 / 伤害数字 / 道具名）。纯几何分不开它们，
而**文本形态**一眼就能分：台词是含假名的句子，菜单项是短名词，HUD 是数字。

量过的分离度（dev，`median` 判同槽的标注对；窗区域级的六项形态占比 L1 距离）：
真合并 48 对里 47 对 ≤ 0.87，误合并 23 对里 15 对 ≥ 1.16。所以门取 **1.0**——落在两簇之间的空档里，不是扫出来的最优点。

做法：每个窗区域一个文本画像 = 它那些"像样的 run"的字符类别占比（假名 / 汉字 / 数字 / 拉丁 / 标点）+ 句子样占比的均值；
槽位的画像 = 成员画像的逐分量中位数（和几何签名同一个道理）。窗区域进槽、槽与槽合并之前多过一道：画像距离 > `TD_MAX` 就不许。
像样的 run 不足 `MIN_GOOD` 条的窗区域没有画像，**不参与否决**（证据不足就不拦——否决只该在有把握时出手）。

⚠ 缝合的骨架是照 `build_tracks.stitch_slots` 抄的（贪心分配 + 收敛后合并；没抄 reassign——`median` 之下它开关分不出来）。
这是原型：赢了再把否决移植进 `stitch_slots`，不长期留两份骨架。

用法：
    from flowocr.analyze import slot_veto; slots = slot_veto.stitch(L)            # L 来自 cluster_layers.load
"""
from __future__ import annotations

import statistics
import sys

from flowocr.analyze import build_tracks as bt  # noqa: E402
from flowocr.analyze.pair_features import char_profile, good_run  # noqa: E402

KEYS = ("kana", "han", "digit", "latin", "punct", "sentence")
TD_MAX = 1.0
MIN_GOOD = 2


def profiles(L) -> list[dict | None]:
    out = []
    for w in L.win_regions:
        ps = [char_profile(L.runs[i].text) for i in w["runs"] if good_run(L.runs[i])]
        out.append({k: sum(p[k] for p in ps) / len(ps) for k in KEYS} if len(ps) >= MIN_GOOD else None)
    return out


def stitch(L, td_max: float = TD_MAX, soft: float = 0.0) -> list[list[int]]:
    """`soft > 0`：几何上有多个槽位相容时，挑的时候把文本距离也算进去（`1 - y 重叠 + soft × 文本距离`），
    而不是只看 y 重叠。否决管"不许进"，它管"进哪个"。"""
    runs, wr, W = L.runs, L.win_regions, L.W
    a = L.args
    anchor, mutual_x, span = not a["no_slot_anchor"], a["slot_mutual_x"], a["slot_span_ratio"]
    prof = profiles(L)

    def slot_prof(members: list[int]) -> dict | None:
        ps = [prof[w] for w in members if prof[w] is not None]
        return {k: statistics.median(p[k] for p in ps) for k in KEYS} if ps else None

    def td(p: dict | None, q: dict | None) -> float | None:
        return None if p is None or q is None else sum(abs(p[k] - q[k]) for k in KEYS)

    def vetoed(p: dict | None, q: dict | None) -> bool:
        d = td(p, q)
        return d is not None and d > td_max

    order = sorted(range(len(wr)), key=lambda i: -len(wr[i]["runs"]))
    slots: list[list[int]] = []
    cent, sprof = [], []
    for i in order:
        g = bt.region_geom(runs, wr[i]["runs"])
        best, best_d = -1, None
        for k, sig in enumerate(cent):
            yo = bt.slot_fits(g, sig, W, anchor, mutual_x, span)
            if yo is None or vetoed(prof[i], sprof[k]):
                continue
            d = 1 - yo + soft * (td(prof[i], sprof[k]) or 0.0)
            if best_d is None or d < best_d:
                best, best_d = k, d
        if best < 0:
            slots.append([i]); cent.append(g); sprof.append(prof[i])
        else:
            slots[best].append(i)
            cent[best] = bt.slot_geom_median(runs, wr, slots[best])
            sprof[best] = slot_prof(slots[best])
    changed = True
    while changed:
        changed = False
        x = 0
        while x < len(slots):
            y = x + 1
            while y < len(slots):
                if bt.slot_fits(cent[x], cent[y], W, anchor, mutual_x, span) is None or vetoed(sprof[x], sprof[y]):
                    y += 1
                    continue
                slots[x].extend(slots[y])
                del slots[y], cent[y], sprof[y]
                cent[x] = bt.slot_geom_median(runs, wr, slots[x])
                sprof[x] = slot_prof(slots[x])
                changed = True
            x += 1
    return slots


if __name__ == "__main__":
    from flowocr.analyze import cluster_layers as CL
    for tag in sys.argv[1:]:
        L = CL.load(tag)
        base, v = CL.stitch(L, geom_mode="median", reassign=False), stitch(L)
        print(f"{tag}: median-nr 槽位 {len(base)} -> 加文本否决 {len(v)}；"
              f"有画像的窗区域 {sum(p is not None for p in profiles(L))}/{len(L.win_regions)}")
