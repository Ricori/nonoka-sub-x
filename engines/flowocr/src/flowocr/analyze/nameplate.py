"""**名牌带的几何判据（七条阈值）**——⚠ **2026-09-20 起不再是唯一实现，也不再是推荐的那份**。

取代它的是 `flowocr.analyze.uigate` 的**框位 + 相对位置**判据（ui-gate 计划）：
五部整片上这七条**四部挑中 0 个**，框位那条五部都有输出，唯一可比的 f3 上精确率打平、召回 +14 个点。
病根有两层：①第 3 条"字比正文大"和第 6 条"时长落在 [2,10] 秒"**实测全不成立**
（owner 2026-09-19：第 3 条"不对，删掉"，第 6 条改成"时长通常 >= 台词"）；
②更要紧的是**输入**——五部里三部的名牌 run 被一个五千到一万条的巨型区域吞掉了，
**区域那一侧根本没有名牌可挑**。

**2026-09-20 起 `merge_nameplate` 默认不用它了**（`--np-source track`）：名牌改由
`build_tracks` 打标、投影成一条 `kind="nameplate"` 的轨。这里留下的是：
`attach()`（贴法 99.8% 准，和判据是两件事）+ 七条阈值本身（`--np-source geom` 的对照臂）。
**要删七条，先把 `probe_nameplate_geom.py` 一起处理掉**——它存在的意义就是评这七条。

以下是原来的说明。

来历（methodology-audit-2 报告）：把名牌并进主轨，input1 上命中 +66、
纯标点行漏条率 97.6% → 52.4%，比"并整个辩论屏区域"两项都好。但那次挑名牌用的是
**角色名表**——探针能用、修法不能用（换素材就没有名表了）。所以判据只看几何和
区域统计量，**一个字都不看**；名表只出现在 `probe_nameplate_geom.py` 里，用来评
这个模块准不准（精确率/召回）。

放在单独模块而不是留在探针里，是因为它现在有两个消费者
（`probe_nameplate_geom.py` 评判据、`merge_nameplate.py` 用判据）。
历史上这个项目在"同一条规则抄两份"上栽过——methodology-audit 报告那次
是 5 个工具各写各的 SRT 解析，代价是一个错了 30 小时的计数。

形状假设（每一条都在五部整片上验过，数见 methodology-audit-2 报告）：

* 位置固定：`cx` 在全片上几乎不动（`--cx-std`）；
* 在正文**上方紧挨着的一档**：`cy` 比主轨小 `--dy` ~ `--dy-max`——
  **上界不能省**，否则屏幕顶上的标题/计时器也会被挑中；
* **字比正文大**：字高中位是主轨的 `--font-ratio` 倍以上；
* **词表很小**：十来个名字轮着出，`distinct_text_ratio` 低；
* **cue 时长像台词**：中位 4.5–5.5 s。
  这一条是 2026-09-07 换上去的，换掉的是 `dominant_share <= 0.5`——
  那道闸门本意是排水印，实际**先把主角的名牌排除了**（`階堂ヒロ` 占那块区域
  68–85%），f3/f4/f5 的召回因此卡在 40–51%。真正分得开的是时长：
  名牌 4.5–5.5 s、`Auto` 按钮 21–43 s、误识的 `大`/`米`/`è` 0.5–1.5 s。
"""
from __future__ import annotations

import statistics as st
from bisect import bisect_left


def region_stats(d: dict) -> list[dict]:
    """把 `tracks.json` 的每个区域压成判据要用的那几个量（`runs` 原样带着）。"""
    W, H = d["size"]
    regs = []
    for r in d["regions"]:
        runs = r["runs"]
        if not runs:
            continue
        cx = [(b["box"][0] + b["box"][2]) / 2 / W for b in runs]
        f = r["features"]
        regs.append({"i": r["index"], "label": r["label"], "n": len(runs),
                     "cy": st.median((b["box"][1] + b["box"][3]) / 2 / H for b in runs),
                     "cx_std": st.pstdev(cx) if len(cx) > 1 else 0.0,
                     "h": st.median(b["box"][3] - b["box"][1] for b in runs),
                     "pers": f["persistence"], "distinct": f["distinct_text_ratio"],
                     "domi": f["dominant_share"], "dur": f["median_dur_s"],
                     "fill": f["fill_active"], "score": r["primary_score"], "runs": runs})
    return regs


def explain(r: dict, main_r: dict, a) -> list[tuple[str, bool, str]]:
    """逐条判据的 (名字, 过没过, 实测值 vs 门槛)。**`pick()` 就是按它判的**——
    两处分开写必然漂移，这个项目在"同一条规则抄两份"上栽过。

    换素材时最常问的是"为什么一个都没挑中"，而那时**没有角色名表**
    （名表只有 yuka 那套有）。所以这个函数不看文本、不需要参照，
    `probe_nameplate_geom.py --names` 省掉时就靠它回答。
    """
    return [
        ("run 数", r["n"] >= a.min_runs, f"{r['n']} >= {a.min_runs}"),
        ("在正文上方一档", main_r["cy"] - a.dy_max <= r["cy"] <= main_r["cy"] - a.dy,
         f"cy {r['cy']:.3f}，要落在 [{main_r['cy'] - a.dy_max:.3f}, {main_r['cy'] - a.dy:.3f}]"),
        ("字比正文大", r["h"] >= main_r["h"] * a.font_ratio,
         f"字高 {r['h']:.0f} >= {main_r['h']:.0f}×{a.font_ratio} = {main_r['h'] * a.font_ratio:.0f}"),
        ("位置固定", r["cx_std"] <= a.cx_std, f"cx std {r['cx_std']:.3f} <= {a.cx_std}"),
        ("词表小", r["distinct"] <= a.max_distinct,
         f"distinct {r['distinct']:.3f} <= {a.max_distinct}"),
        ("时长像台词", a.dur_lo <= r["dur"] <= a.dur_hi,
         f"中位 {r['dur']:.1f}s 落在 [{a.dur_lo}, {a.dur_hi}]"),
        ("不是常驻", r["fill"] <= a.max_fill, f"fill {r['fill']:.2f} <= {a.max_fill}"),
    ]


def pick(regs: list[dict], a) -> tuple[dict, list[dict]]:
    """返回 `(主轨区域, 判为名牌的区域)`。`a` 是 `add_args()` 那组参数。"""
    main_r = max(regs, key=lambda x: x["score"])
    picked = [r for r in regs if r["i"] != main_r["i"]
              and all(ok for _, ok, _ in explain(r, main_r, a))]
    return main_r, picked


def attach(main: list, names: list) -> tuple[list, int]:
    """把名牌**贴进时间重叠最大的那条正文 cue 的首行**，返回 (SRT blocks, 贴上几条)。

    贴法决定一切（speaker-name-line 报告离线量过三种）：
    "覆盖 cue 起点的最后一条" 只有 73.1% 对、"起点最近 ±1.5s" 97.2%、
    **"时间重叠最大" 99.8%**（938/940）。第一版探针用的是 73% 那种，
    结论出来是"贴了等于没贴"——那是贴错了 27%，不是名字没用。

    两个消费者：`probe_add_speaker.py`（探针，名表过滤后贴）和
    `merge_nameplate.py --paste`（修法，几何判据挑区域后贴）。**别抄第三份。**
    """
    starts = [c.start for c in names]
    blocks, tagged = [], 0
    for c in main:
        i = bisect_left(starts, c.start)
        nm, best = None, 0.0
        for j in range(max(0, i - 8), min(len(names), i + 8)):
            o = min(names[j].end, c.end) - max(names[j].start, c.start)
            if o > best:
                nm, best = names[j], o
        lines = list(c.lines)
        if nm is not None and nm.lines and nm.text("") not in c.text(""):
            lines = [nm.lines[0]] + lines
            tagged += 1
        blocks.append((int(round(c.start * 1e6)), int(round(c.end * 1e6)), lines))
    return blocks, tagged


def add_args(ap) -> None:
    """判据的旋钮。两个消费者共用，**别抄第二份**。"""
    ap.add_argument("--dy", type=float, default=0.04,
                    help="名牌至少要在主轨上方这么多（归一化 cy）")
    ap.add_argument("--dy-max", type=float, default=0.15,
                    help="但也不能高太多——那是屏幕顶上的标题")
    ap.add_argument("--max-distinct", type=float, default=0.25,
                    help="不同文本的占比上限：名牌就十来个名字轮着出")
    ap.add_argument("--dur-lo", type=float, default=2.0,
                    help="cue 时长中位的下限（秒）：名牌跟着台词走，一句几秒")
    ap.add_argument("--dur-hi", type=float, default=10.0,
                    help="上限：常驻按钮/水印一挂就是几十秒（`Auto` 实测 21-43 s）")
    ap.add_argument("--max-fill", type=float, default=0.8,
                    help="fill_active 上限：真水印几乎全程在（`#` 实测 1.00）。"
                         "**在 yuka 五部上一个数都不改**，是给别的素材兜底的")
    ap.add_argument("--font-ratio", type=float, default=1.3,
                    help="字高中位至少是主轨的几倍")
    ap.add_argument("--cx-std", type=float, default=0.10,
                    help="cx 的标准差上限（位置固定）。0.06 -> 0.10 让 f4 的召回从 "
                         "50.7%% 涨到 90.1%%，另外四部一个数不动；0.14 和 0.10 结果完全相同，"
                         "**阈值坐在平台上而不是悬崖上**")
    ap.add_argument("--min-runs", type=int, default=30,
                    help="区域小于这么多 run 就不考虑")
