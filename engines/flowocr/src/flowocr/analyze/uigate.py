"""**框位（footprint）这一层的共享实现**：按 IoU 聚框位、定正文带、量相对位置。

为什么抽出来（2026-09-19）：判据原来只活在 a1-mask-reuse 实验里的 `footprint_probe.py` 里，
而现在有第二个消费者——**名牌该改吃框位，不该吃区域**。
诊断见 ui-gate 计划：五部整片上按名表认，**只有 2 部存在"名牌区域"**；
f1 / f2 / f5 上名牌的 run（2292 / 1990 / 725 条）被一个 5000~9000 条的**巨型区域**吞掉了
（名字只占它 14%~26%）。**在区域那一侧换什么判据都救不了，因为输入里就没有那个区域。**
这个项目在"同一条规则抄两份"上栽过（5 个工具各写各的 SRT 解析），所以放这里、两边都 import。

输入统一成 `Item`：一个**在某个位置、某段时间、有文本**的东西。
obs 的一行是一个 Item（t0 = t1 = t_us），`tracks.json` 的一条 run 也是一个 Item。
"""
from __future__ import annotations

import statistics as st
from collections import Counter
from dataclasses import dataclass, field

from flowocr.artifacts import evalkit

NP_DY = (-6.0, -1.0)
"""名牌相对**它那条正文带**的位置窗（单位：带的字高，负数 = 在带上方）。

owner 2026-09-19："绝大多数情况下可认为在正文上方（一般正上方或靠左对齐）。
或许有少数情况下在左方，不过很稀少了，可能不需要考虑。几乎不存在其他情况。"
⚠ 现有素材里 15/15 个摆帧核过的名牌都在上方，**这个窗没遇到过反例**（ui-gate 计划）。"""

BODY_CLEN = 8
"""内容字到这个数才算**正文候选**（名牌是短的，正文是长的）。

散在三处过（挑候选、`relate` 的 `is_body`、`pick_nameplates`），抽成一个常量——
这个项目在"同一个数写两遍"上栽过。"""


@dataclass
class Item:
    box: tuple[int, int, int, int]
    t0: int                       # 微秒
    t1: int
    text: str
    key: object = None            # 调用方自己的标识（挑中之后要映射回去）
    clipped: bool = False         # 端点被自选范围的时间切点截过（时长是配置截的、不是自然上下屏；ocr-regions 计划）
    w: int = 1
    """这条 Item 代表多少次**观测**。

    ⚠ **`churn` 的门是在 obs 行上标定的**（0.06），而 `tracks.json` 的一条 run 是
    时间合并之后的结果：`Auto` 在 20 分钟窗里只有 **4 条 run**（在屏 894 s），
    按 run 数算 churn = 1/4 = **0.25**，直接被挡在门外——而按 obs 算是 0.0003。
    那个翻车场景（`Auto` 污染 293/595 条主轨 cue）**因此连框位门也接不住**。
    所以 run 这一侧要传 `w = n_obs`；obs 那一侧 `w = 1`，行为不变。"""


@dataclass
class Footprint:
    box: list[int]
    items: list[Item] = field(default_factory=list)
    # 下面这些由 measure() 填
    m: dict = field(default_factory=dict)


GRID_PX = 64
"""`footprints` 的网格索引边长（像素）。只影响速度、不影响结果（IoU > 0 的两框必共享格子）。"""


def iou(p, q) -> float:
    iw = min(p[2], q[2]) - max(p[0], q[0])
    ih = min(p[3], q[3]) - max(p[1], q[1])
    if iw <= 0 or ih <= 0:
        return 0.0
    i = iw * ih
    return i / ((p[2] - p[0]) * (p[3] - p[1]) + (q[2] - q[0]) * (q[3] - q[1]) - i)


def footprints(items: list[Item], thr: float = 0.6) -> list[Footprint]:
    """按 IoU 和**跑动代表框**聚，不看文本。

    ⚠ **别用格子量化**（左上角除以字高取整）：框抖一两个像素就可能跨过格子边界，
    实测同一个 `Auto` 被切成三个框位（33.3% + 27.7% + 12.0%），每个都远低于真实的 74.5%。
    """
    if thr <= 0:                 # 入参校验用 ValueError（`python -O` 下 assert 会消失，2026-09-24 审计）
        raise ValueError("footprints 的网格索引靠 IoU ≥ thr > 0 ⇒ 两框有交集；thr = 0 时原版会并进第一个框位、这里不会")
    fps: list[Footprint] = []
    # 网格索引（2026-09-24 profile：整场 gi1 上逐个框位算 IoU 3,260 万次、60 s）：IoU ≥ thr > 0 要求两框有交集，
    # 有交集就一定共享至少一个格子，所以只看共享格子的框位、**按创建顺序**取第一个过门的——和逐个扫逐位相同。
    # 框位的代表框会跟着成员挪（跑动平均），挪了就重新登记格子
    cell = GRID_PX
    grid: dict[tuple[int, int], set[int]] = {}

    def cells(b) -> list[tuple[int, int]]:
        return [(x, y) for x in range(int(b[0]) // cell, int(b[2]) // cell + 1)
                for y in range(int(b[1]) // cell, int(b[3]) // cell + 1)]

    for it in sorted(items, key=lambda x: x.t0):
        cand = sorted({i for c in cells(it.box) for i in grid.get(c, ())})
        for i in cand:
            f = fps[i]
            if iou(f.box, it.box) >= thr:
                f.items.append(it)
                n = len(f.items)
                old = f.box
                f.box = [int((f.box[k] * (n - 1) + it.box[k]) / n) for k in range(4)]
                if f.box != old:
                    for c in cells(old):
                        grid[c].discard(i)
                    for c in cells(f.box):
                        grid.setdefault(c, set()).add(i)
                break
        else:
            fps.append(Footprint(box=list(it.box), items=[it]))
            for c in cells(it.box):
                grid.setdefault(c, set()).add(len(fps) - 1)
    return fps


def merge_intervals(iv: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """区间并集。放在这里是因为 `measure` 的占比要用它，而 `build_tracks.active_span`
    也用它——**只写一份**（那边 `import uigate` 已经有了，反过来会循环）。"""
    out: list[list[int]] = []
    for s, e in sorted(iv):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def occupancy(items: list[Item], step_us: int) -> int:
    """这些 Item **一共占了多少时间**——区间**并集**，不是逐条相加。

    ⚠ 2026-09-20 审计七的病根就在这里：原来写的是
    `sum(max(it.t1 - it.t0, step_us) for it in items)`，两处都错——

    * **逐条相加不是并集**：同一时段的重叠观测被重复计；
    * `step_us` 被当成"每条至少算这么长"的地板，而调用方传进来的是**全部 run 时长的中位数**，
      于是比中位数短的 run 一律被拉长。实测：同一段 10 秒的占用，
      一条 run 时 share = 0.10，拆成 20 条 0.5 s 的 run 时 **0.20**——
      "分母换成时间就不随读法变化"这条立论当场不成立。

    `step_us` 的语义钉死为**采样间隔**：obs 那一侧 `t0 == t1`（点观测），补满一格；
    run 那一侧 `t_end` 本来就已经加过一个采样间隔，**原样用**——自选范围的切点把 run 截到不足一格时
    （`clipped_end`），补回一格会让占比超过 1（2026-09-23 Codex 审计：截到 0.1 s 的 run 得到 share 5）。
    """
    if not items:
        return 0
    return sum(e - s for s, e in
               merge_intervals([(it.t0, it.t1 if it.t1 > it.t0 else it.t0 + step_us) for it in items]))


def measure(f: Footprint, span_us: int, step_us: int) -> dict:
    """一个框位的物理量。**分母是时间，不是 cue**——读法变了它也不动。

    ⚠ 分子也必须是时间（`occupancy`，区间并集）。只换分母不换分子的话，
    同一段占用拆得越碎占比越高，这道门自己就会漂（审计七）。
    """
    texts = [it.text for it in f.items]
    dur = occupancy(f.items, step_us)
    digs = [sum(c.isdigit() for c in t) / max(1, len(t.strip())) for t in texts if t.strip()]
    ws = [it.box[2] - it.box[0] for it in f.items]
    return {
        "share": dur / max(1, span_us),
        "wvar": (st.pstdev(ws) / max(1, st.median(ws))) if len(ws) > 1 else 0.0,
        # **分母是观测数**，不是 Item 数（见 Item.w 的说明）
        "churn": len(set(texts)) / max(1, sum(it.w for it in f.items)),
        "clen": st.median([evalkit.content_len(t) for t in texts]) if texts else 0.0,
        "dig": st.median(digs) if digs else 0.0,
        "top": max(set(texts), key=texts.count) if texts else "",
        # **覆盖率**：属于"重复出现过的文本"的条目占比。名牌就十来个名字轮着出（高），
        # 弹幕 / OCR 噪声一次一个样（低）。⚠ 别写成"重复种数 / 条数"——那是**反的**
        # （台词几乎不重复、分子≈0，会把台词全放行；ui-gate 计划那次已经栽过）。
        # ⚠ **它只在"一个 Item = 一次上屏"时有意义**，也就是 run 那一侧（审计七）：
        # obs 那一侧一条文本在屏几帧就有几个 Item，`cover` 结构上恒 ≈ 1、放行一切。
        # 和 `churn` 不同，这里**不能**用 `w` 加权——`w` 数的是观测数，分子分母同时乘上去
        # 还是"观测级"的口径，救不了。要在 obs 上用就先按连续同文本并成段。
        # `pick_nameplates` 入口有断言钉着。
        "cover": (sum(n for n in Counter(texts).values() if n >= 2) / len(texts)) if texts else 0.0,
        "n": len(f.items),
    }


def bands(cands: list[dict], style_h: float = 1.6, max_lines: float = 4.0,
          min_w: float = 0.3) -> list[dict]:
    """把正文候选聚成**带**。候选是 `{"box", "n"}`，按 cy 排。

    三条都是被实测逼出来的（ui-gate 计划）：

    * **字高差超过 `style_h` 倍的不并进同一条带**——差这么多的本来就是两种样式
      （owner 2026-09-19：一种样式一簇）；
    * **跨度不超过 `max_lines` 个字高**：`e-yuka-f5` 上 46 行长文本被链式合并成
      一条 y=74→715 的带，真正的台词带被压没。这和 clustering 报告里
      片尾滚动表吞掉全片 65% run **是同一个形状**；
    * **带要够重**：只按"离得最近"挑会被一次性长文本骗到（精确率 88% → 62%）。

    ⚠ 合并容差取**两者较小的字高**：拿"当前这条"当尺，大字号候选会把 375 px 内的一切吸进来——
    不对称判据正是 `stitch_slots` 那个老毛病的同形。
    """
    groups: list[list[dict]] = []
    for d in sorted(cands, key=lambda x: x["box"][1]):
        h = d["box"][3] - d["box"][1]
        ok = False
        if groups:
            prev = groups[-1][-1]
            ph = prev["box"][3] - prev["box"][1]
            hh, big = min(h, ph), max(h, ph)
            ok = (d["box"][1] - prev["box"][1] <= 2.5 * hh
                  and big <= style_h * max(1, hh)
                  and d["box"][1] - groups[-1][0]["box"][1] <= max_lines * hh)
        (groups[-1].append(d) if ok else groups.append([d]))
    out = [{"top": st.median(x["box"][1] for x in g),
            "h": st.median(x["box"][3] - x["box"][1] for x in g),
            "x0": st.median(x["box"][0] for x in g),
            "xl": min(x["box"][0] for x in g), "xr": max(x["box"][2] for x in g),
            "w": sum(x["n"] for x in g),
            # 候选带了时刻集合就并起来——上层要算"这个框位和**这条带**共现多少"。
            # ⚠ **永远是集合**（没有就空集）：给 None 的话上层 `own & b["ts"]` 会当场炸，
            # 而"候选有没有 ts"是个隐式契约——两个消费者里只有探针带 ts。
            "ts": set().union(*[x["ts"] for x in g]) if all("ts" in x for x in g) else set(),
            } for g in groups]
    if out:
        wmax = max(b["w"] for b in out)
        out = [b for b in out if b["w"] >= min_w * wmax]
    return out


def relate(box: list[int], clen: float, bs: list[dict]) -> dict:
    """这个框位和正文带的关系。没有带就全是 None。

    **两个问题，分别答，不共用一次择带**（2026-09-20 审计七的返工）：

    * `dy_line` / `hr` / `band_top` 回答"**它可能是哪条带的名牌**"——
      ⚠ "所属"不等于"最近"：名牌在**自己那条**正文带上方 3~5 个字高，
      而它上面可能还有另一条带离得更近（1.8 个字高），按"最近"就会被判成"在带下方"而丢掉
      （f5 上这么丢了 **417 条，全是真名牌**，`階堂ヒロ` ×163…）。所以先问
      **有没有哪条带正好在它下方 1~6 个字高**（`NP_DY`），有就认那条；没有才退回最近的；
    * `in_band` 回答"**它自己在不在某条正文带里**"——**对所有带问一遍**，
      有任何一条同高且水平重叠就算在带里。
      ⚠ 这一条**不能跟着上面那次择带走**：真正文下方只要再多出一条带，
      它就会被改判成"在那条带上方 3 个字高"、`in_band` 由真变假，
      而 `build_tracks.ui_footprints` 拿它当**正文保护**——保护一失效，框位门就能咬真台词。
      多带素材（f5 整片 282 条带、`z-xingchuan` 的画中画面板）是常态，不是边角。
    """
    if not bs:
        return {"dy_line": None, "hr": None, "in_band": False, "is_body": clen >= BODY_CLEN}

    def dy_of(b):
        return (box[1] - b["top"]) / max(1, b["h"])

    def inside(b) -> bool:
        # 同一行（±1 个带高）**而且**水平方向和带有重叠。
        # ⚠ 只看竖直会误护右侧的按键提示（`ジャンプ` 和台词同高、但在画面最右）。
        return abs(dy_of(b)) <= 1.0 and min(box[2], b["xr"]) > max(box[0], b["xl"])

    below = [b for b in bs if NP_DY[0] <= dy_of(b) <= NP_DY[1]]
    b = (max(below, key=dy_of) if below                       # 认最贴近的那条
         else min(bs, key=lambda b: abs(dy_of(b))))
    dy = round(dy_of(b), 2)
    return {
        "dy_line": dy,
        "hr": round((box[3] - box[1]) / max(1, b["h"]), 2),
        "band_top": b["top"],
        "in_band": any(inside(x) for x in bs),
        "is_body": clen >= BODY_CLEN,
    }


def is_nameplate(m: dict) -> bool:
    """**名牌 = 某条正文带的附属块**：在它上方 1~6 个字高、字高比 0.6~4、自己不是正文候选。

    这条判据换掉了 `nameplate.py` 的七条阈值（owner 2026-09-19：
    "如果有更好的更 robust 了的话就不留"）。实测（ui-gate 计划）：
    UI 门里用它，误剔 **11 → 0**、召回一个数没动；15 个摆帧核过的名牌它认出 15 个，
    七条阈值只认出 2 个。

    ⚠ 已知的边界：**现有素材里名牌无一例外都在正文上方**（15/15 摆帧核过），
    所以这个窗**没遇到过反例**。owner 说侧面 / 嵌在行内的版式"很稀少，可能不需要考虑"，
    碰到了记进 ui-gate 计划。
    """
    return (not m.get("is_body") and m.get("dy_line") is not None
            and NP_DY[0] <= m["dy_line"] <= NP_DY[1] and 0.6 <= (m.get("hr") or 0) <= 4.0)


def np_window(t0: int, lo: int, win_us: int) -> int:
    """名牌分窗的窗号：从 `lo`（这批 run 里最早的起点）起每 `win_us` 一窗，按 run 的起点归窗。
    `pick_nameplates` 分窗和 `build_tracks.ui_footprints` 认"名牌标在哪几个窗"用的是同一个约定，只写这一份。"""
    return (t0 - lo) // win_us


def pick_nameplates(items: list[Item], win_us: int = 120_000_000,
                    dur_ratio: float = 0.8, max_churn: float = 0.8,
                    min_cover: float = 0.5, iou_thr: float = 0.6,
                    step_us: int = 1, observable=None) -> list[Item]:
    """整片上挑名牌：**按时间分窗**，每窗各自聚框位、定正文带、判名牌。

    三条判据（owner 2026-09-19 的修订，实测见 ui-gate 计划）：

    1. **在正文带上方一档**（`is_nameplate`：上方 1~6 个字高、字高比 0.6~4、自己不是正文）——
       owner："绝大多数情况下可认为在正文上方（一般正上方或靠左对齐）；
       少数在左方，不过很稀少；几乎不存在其他情况"；
    2. **时长 ≥ 台词 × `dur_ratio`**——owner 把旧判据那条"时长落在 [2,10] 秒"改成了这个。
       **它是精确率的主力**：去掉它 f3 精确 76% → 6%、f5 69% → 23%；
    3. **词表不太大**（`churn <= max_churn`）：名牌就十来个名字轮着出；
    4. **文本要反复出现**（`cover >= min_cover`，2026-09-20 补）：名牌是那十来个名字**轮着出**，
       所以"属于重复文本的条目"应当占大多数。补它的理由是**新素材**——
       `z-xingchuan`（中文字幕 + 画中画，**根本没有名牌**）上，前三条标了 **198 条**，
       全是弹幕和 OCR 噪声：它们在几何上确实"在带上方、够长、词表不大"，
       只有"反复出现"这一条能把它们拦住。

    **删掉的**：③"字比正文大"（owner：不对，删掉——四款游戏 + 五部 yuka 上全不成立）；
    ①"run 数"和⑦"不是常驻"**实测一个数都不动**（去掉前后逐条相同），
    ④"位置固定"在框位这一侧是**自带的**（IoU 聚类本身就要求位置几乎不动）。

    ⚠ **必须分窗**：不分窗时全片只聚出 1 条带，"在带上方"就把 6.2 小时里
    落在那条带上方的一切都收了（f5 精确率 **11%**）。
    这正是 clustering 报告那条"聚类必须按时间分窗"——同一个坑又踩了一次。

    **自选范围**（`observable(框, t0, t1) -> 看得见的 µs`，ocr-regions 计划）：两边比较都按可观察时间折算——
    时长（名牌对正文）只拿**没被配置截过**的 run 比（截过的时长是用户关掉的，不是自然上下屏；全被截过才退回全用），
    挑正文带候选的时间占比分母用这个框位在窗里看得见的时长，不用窗长。`cover` / `churn` 是计数，不受影响。
    """
    if not items:
        return []
    # ⚠ **只接 run 级 Item**：第 4 条判据 `cover` 数的是"一个 Item = 一次上屏"，
    # 点观测（obs 行，`t0 == t1`）喂进来时它结构上恒 ≈ 1、等于没这条判据（审计七）。
    if all(i.t1 <= i.t0 for i in items):
        raise ValueError("pick_nameplates 只接 run 级 Item（t1 > t0）："
                         "`cover` 那条判据在点观测上恒 ≈ 1，静默失效")
    # ⚠ `step_us` 是**采样间隔**，不是 Item 时长的中位数（`occupancy` 的说明）。
    step = max(1, step_us)
    lo = min(i.t0 for i in items)
    by_win: dict[int, list[Item]] = {}
    for i in items:
        by_win.setdefault(np_window(i.t0, lo, win_us), []).append(i)
    out: list[Item] = []
    def natural(its: list[Item]) -> list[Item]:
        return [it for it in its if not it.clipped] or its

    for w, chunk in by_win.items():
        if len(chunk) < 10:
            continue
        fps = footprints(chunk, iou_thr)
        wlo = lo + w * win_us
        spans = [observable(f.box, wlo, wlo + win_us) if observable else win_us for f in fps]
        # 窗里一点都看不见的框位直接跳过（审核：`max(1, 0)` 当分母会让占比大得离谱、混进正文带候选）
        ms = [(f, measure(f, sp, step)) for f, sp in zip(fps, spans) if sp > 0]
        cands = [{"box": f.box, "n": m["n"]} for f, m in ms
                 if m["share"] >= 0.02 and m["clen"] >= BODY_CLEN and m["dig"] < 0.3]
        bs = bands(cands)
        if not bs:
            continue
        bdur = st.median([st.median([it.t1 - it.t0 for it in natural(f.items)])
                          for f, m in ms if m["clen"] >= BODY_CLEN] or [step])
        for f, m in ms:
            m.update(relate(f.box, m["clen"], bs))
            dur = st.median([it.t1 - it.t0 for it in natural(f.items)])
            if (is_nameplate(m) and dur >= bdur * dur_ratio and m["churn"] <= max_churn
                    and m["cover"] >= min_cover):
                out += f.items
    return out
