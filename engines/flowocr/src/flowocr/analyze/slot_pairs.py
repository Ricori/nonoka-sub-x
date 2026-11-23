"""**聚类质量的尺**：抽 run 对 → 摆帧 → 标"该不该在一起" → 给任何一种聚类量误合并 / 误拆分（next-steps）。

为什么是 **run 对**而不是窗区域对：窗区域是 `cluster_windowed` 的产物，下标随代码变；
而且窗内聚类自己也会错（星铁的"字幕行连进阅读面板"）。run 有稳定的身份（起止时刻 + 框 + 文本），
于是同一份标注能量**任何**一层：窗内聚类、跨窗缝合、最终区域，乃至以后完全不同的算法。

**几何和各臂的分歧只用来挑样本，不当真值**（cluster-ruler 计划的规矩）。四层抽样：

    S1  分歧对     在臂 X 里同槽、在臂 Y 里不同槽——信息量最大，但**只能比臂、算不出绝对错误率**
    S2  同槽随机对 基准臂（`BASE`）里同一个槽位的两个窗区域（按 run 数加权）——给**基准臂**的误合并率一个无偏的底
    S3  近邻异槽对 基准臂里不同槽、但 y 区间重叠且 x 相交——给**基准臂**的误拆分率的底
                   （⚠ 随机异槽对几乎全是"显然不同"，没信息；所以这个率是"近邻里的"，不是全体的）
    S4  窗内对     同一个窗区域里的两条 run——量窗内聚类
    S5  同刻相邻行 同时在场（时间重叠 ≥ 较短那条的一半）、上下相邻、横向有重叠的两条 run——**不照任何臂抽**，
                   各臂在它上面是同一个率；量的是"同一个框里的几行是不是一条轨"（`lines` 在产物上掉覆盖的形状，
                   原来的四层很少抽到这种对）。`sample-adjacent`

**`lines` / `learned` 两条臂量的是管线的最终分组**（`pipeline_assign`，和 `build_tracks --cluster` 调同一个函数）：
行位 -> 带之后，`lines` 再并同刻同框的上下行，两者的散 run 回落到缝合分组。2026-10-05 之前量的是 `slot_lines.assign` /
`slot_learned.assign`（不并同刻行、散 run 各自成单例），那时的读数不是管线里的算法。

⚠ **每条臂在照自己抽的样本上都最差**（S2 只照得见基准臂的误合并、S3 只照得见它的误拆分）。比两条臂，要两条臂各当一次基准、
合起来报（cluster-ruler 计划的更正框）。行级算法没有窗区域，用 `sample-learned` / `sample-lines`（run 级的 S1 / S2 / S3）。

标注四值（协议全文在 cluster-ruler 计划）：
    same       同一个屏幕元素 / 同一条带（行数不同、第 1 行和第 3 行，都算 same）
    widget     不同元素、但属于同一个控件（对话框的名牌 + 正文）——合不合都不算错
    different  不同元素（哪怕在不同时刻占同一个位置）
    unsure     看不出来（帧上找不到那行字、转场中……）

用法：
    python -m flowocr.analyze.slot_pairs sample f5 --n 40 --seed 1            # 抽样 + 摆帧（要 ffmpeg、PIL）
    python -m flowocr.analyze.slot_pairs sample-learned f5 --seed 4           # 照 slot_learned 自己的聚类抽（run 级、留这一部片子不训）
    python -m flowocr.analyze.slot_pairs sample-lines f5 --n 25 --seed 5      # 照 --cluster lines 的分组抽（run 级）
    python -m flowocr.analyze.slot_pairs sample-adjacent zzz --n 20 --seed 6  # S5：同刻相邻行（不照任何臂）
    python -m flowocr.analyze.slot_pairs merge data/gt/slotpairs-f5-s1.json tmp/slotpairs/f5/labels.json --by sonnet
    python -m flowocr.analyze.slot_pairs score data/gt/slotpairs-*.json       # 各臂的误合并 / 误拆分
"""
from __future__ import annotations

import argparse
import io
import json
import random
import subprocess
from collections import Counter
from pathlib import Path

from flowocr.analyze import build_tracks as bt  # noqa: E402
from flowocr.analyze import cluster_layers as CL  # noqa: E402
from flowocr import paths  # noqa: E402

ARMS: dict[str, dict] = {
    "pooled": {"geom_mode": "pooled"},
    "pooled-nr": {"geom_mode": "pooled", "reassign": False},
    "median": {"geom_mode": "median"},
    "median-nr": {"geom_mode": "median", "reassign": False},
}
"""挑分歧对用的臂（`cluster_layers.stitch` 的覆盖项）。`score` 另外还能量外部给的分配。
**每条臂都把旋钮显式写出来**：第一版有一条 `"default": {}`，`--slot-geom` 一翻默认它就和 `median` 塌成同一条
（docs/dev-guide/verification.md「A/B 的规矩」：对照臂把关掉的旋钮显式写出来）。seed 1 / 2 的真值是按当时的默认（= 现在的 `pooled`）抽的。"""

BASE = "median"
"""S2 / S3 按哪条臂的同槽 / 异槽关系抽——取当前默认，于是这两层对**它**是无偏的率。"""

SHARE = {"S1": 0.40, "S2": 0.25, "S3": 0.25, "S4": 0.10}
LABELS = ("same", "widget", "different", "unsure")
SCHEMA = "flowocr-slotpairs/1"


def run_key(r) -> list:
    return [r.t_start, r.t_end, [round(v) for v in r.box], r.text]


from flowocr.analyze.pair_features import MIN_CONTENT, MIN_OBS, good_run  # noqa: E402


def pick_run(L, wi: int, rnd: random.Random, avoid: int = -1) -> int:
    return rnd.choice([i for i in L.win_regions[wi]["runs"] if i != avoid and good_run(L.runs[i])])


def slot_index(slots: list[list[int]]) -> dict[int, int]:
    return {w: k for k, sl in enumerate(slots) for w in sl}


def sample(L, n: int, seed: int) -> list[dict]:
    rnd = random.Random(seed)
    wr = L.win_regions
    arms = {name: slot_index(CL.stitch(L, **over)) for name, over in ARMS.items()}
    weight = [sum(1 for i in w["runs"] if good_run(L.runs[i])) for w in wr]
    idx = list(range(len(wr)))
    n_good = sum(weight)
    print(f"  [筛子] 像样的 run {n_good}/{len(L.runs)}（{n_good / len(L.runs):.1%}）；"
          f"有像样 run 的窗区域 {sum(1 for x in weight if x)}/{len(wr)}——只在这些里面抽")
    geom = [bt.region_geom(L.runs, w["runs"]) for w in wr]
    members: dict[str, dict[int, list[int]]] = {}
    for name, so in arms.items():
        m: dict[int, list[int]] = {}
        for w, k in so.items():
            m.setdefault(k, []).append(w)
        members[name] = m

    def mate(arm: str, i: int) -> int | None:
        c = [j for j in members[arm][arms[arm][i]] if j != i and weight[j]]
        return rnd.choices(c, [weight[j] for j in c])[0] if c else None

    def near(i: int) -> int | None:
        _, x0, x1, y0, y1, h = geom[i]
        c = [j for j in idx if weight[j] and arms[BASE][j] != arms[BASE][i] and wr[j]["win"] != wr[i]["win"]
             and bt.span_overlap(y0, y1, geom[j][3], geom[j][4], min(h, geom[j][5])) >= 0.5
             and min(x1, geom[j][2]) > max(x0, geom[j][1])]
        return rnd.choice(c) if c else None

    want = {s: round(n * p) for s, p in SHARE.items()}
    out, seen = [], set()

    def add(stratum: str, i: int, j: int, same_wr: bool = False) -> bool:
        a = pick_run(L, i, rnd)
        b = pick_run(L, j, rnd, avoid=a) if same_wr else pick_run(L, j, rnd)
        k = tuple(sorted((a, b)))
        if a == b or k in seen:
            return False
        seen.add(k)
        out.append({"stratum": stratum, "runs": [a, b], "wr": [i, j],
                    "together": {name: so[i] == so[j] for name, so in arms.items()}})
        return True

    pairs_x = [(x, y) for x in ARMS for y in ARMS if x != y and BASE in (x, y)]
    for stratum in ("S1", "S2", "S3", "S4"):
        got = tries = 0
        while got < want[stratum] and tries < 4000:
            tries += 1
            i = rnd.choices(idx, weight)[0]
            if stratum == "S1":
                x, y = pairs_x[tries % len(pairs_x)]
                j = mate(x, i)
                ok = j is not None and arms[y][i] != arms[y][j] and add("S1", i, j)
            elif stratum == "S2":
                j = mate(BASE, i)
                ok = j is not None and add("S2", i, j)
            elif stratum == "S3":
                j = near(i)
                ok = j is not None and add("S3", i, j)
            else:
                ok = weight[i] >= 2 and add("S4", i, i, same_wr=True)
            got += bool(ok)
        if got < want[stratum]:
            print(f"  ⚠ {stratum} 只抽到 {got}/{want[stratum]}（候选不够）")
    rnd.shuffle(out)                       # 标注的人不该从顺序里看出是哪一层
    return out


def grab(video: str, t: float):
    from PIL import Image
    p = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", video,
                        "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                       capture_output=True)
    if p.returncode or not p.stdout:
        raise RuntimeError(f"ffmpeg 抽帧失败 t={t}: {p.stderr.decode(errors='replace')[:200]}")
    return Image.open(io.BytesIO(p.stdout)).convert("RGB")


def box_at(r, t_us: float) -> list[float]:
    """run 在 t 时刻的框。**动过的 run（滚动面板、评论墙）`r.box` 是它最后的位置**——
    拿它画在中途那一帧上，黄框会落在滚进那个位置的另一行字上（2026-09-24 复标查出来的：
    动过的对复标不一致 5/13，没动过的 5/37；cluster-ruler）。按轨迹取 t 之前最后一个点。"""
    if not getattr(r, "moving", False) or not r.boxes:
        return r.box
    past = [b for tb, b in r.boxes if tb <= t_us]
    return past[-1] if past else r.boxes[0][1]


def panel(L, r, tag: str, t: float | None = None):
    """一侧：上面是整帧（960 宽）+ 黄框，下面是框周围的放大条。`t`（秒）缺省 = 这条 run 自己的时刻。"""
    from PIL import Image, ImageDraw
    if t is None:
        t = (r.t_start + min(r.t_end, r.t_start + 4_000_000)) / 2e6   # 长驻的取前 4 秒的中点
    im = grab(L.video, t)
    sc = 960 / im.width
    small = im.resize((960, round(im.height * sc)))
    d = ImageDraw.Draw(small)
    box = box_at(r, t * 1e6)
    x0, y0, x1, y1 = [v * sc for v in box]
    d.rectangle([x0 - 3, y0 - 3, x1 + 3, y1 + 3], outline=(255, 230, 0), width=3)
    d.text((8, 6), f"{tag}  t={t:.1f}s", fill=(255, 230, 0))
    bx0, by0, bx1, by1 = box
    mx, my = 0.5 * (by1 - by0) + 40, 1.0 * (by1 - by0) + 20
    crop = im.crop((max(0, int(bx0 - mx)), max(0, int(by0 - my)),
                    min(im.width, int(bx1 + mx)), min(im.height, int(by1 + my))))
    z = min(960 / crop.width, 160 / crop.height)
    crop = crop.resize((max(1, round(crop.width * z)), max(1, round(crop.height * z))))
    out = Image.new("RGB", (960, small.height + 168), (24, 24, 24))
    out.paste(small, (0, 0))
    out.paste(crop, (0, small.height + 8))
    return out


def render(L, pair: dict, dest: Path) -> None:
    from PIL import Image
    t = pair.get("t_us")                    # S5 的两条同时在场：两侧摆同一帧
    a, b = (panel(L, L.runs[i], s, None if t is None else t / 1e6) for i, s in zip(pair["runs"], "AB"))
    im = Image.new("RGB", (a.width + b.width + 8, max(a.height, b.height)), (24, 24, 24))
    im.paste(a, (0, 0))
    im.paste(b, (a.width + 8, 0))
    im.save(dest, quality=85)


def cmd_sample(a) -> int:
    root = paths.data_root()
    L = CL.load(a.tag)
    pairs = sample(L, a.n, a.seed)
    imgdir = root / "tmp" / "slotpairs" / a.tag
    imgdir.mkdir(parents=True, exist_ok=True)
    items = []
    for n, p in enumerate(pairs):
        pid = f"{a.tag}-s{a.seed}-{n:03d}"
        render(L, p, imgdir / f"{pid}.jpg")
        ra, rb = (L.runs[i] for i in p["runs"])
        items.append({"id": pid, "image": f"tmp/slotpairs/{a.tag}/{pid}.jpg",
                      "a": {"key": run_key(ra), "win": L.win_regions[p["wr"][0]]["win"]},
                      "b": {"key": run_key(rb), "win": L.win_regions[p["wr"][1]]["win"]},
                      "label": None, "labeled_by": None, "note": "",
                      # 下面两样**标注时不给看**（`labeling_view` 会剥掉），只在分析时用
                      "stratum": p["stratum"], "together": p["together"]})
        print(f"  {pid}  {p['stratum']}  A={ra.text[:18]!r}  B={rb.text[:18]!r}", flush=True)
    dest = root / "data" / "gt" / f"slotpairs-{a.tag}-s{a.seed}.json"
    doc = {"schema": SCHEMA, "tag": a.tag, "video": L.video, "obs": str(L.obs), "seed": a.seed,
           "arms": ARMS, "base": BASE, "share": SHARE, "split": a.split,
           "filter": {"min_content": MIN_CONTENT, "min_obs": MIN_OBS}, "items": items}
    if dest.exists() and not a.force:
        raise SystemExit(f"{dest} 已存在（里面可能有标注）；要覆盖给 --force")
    dest.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    view = [{"id": it["id"], "image": it["image"], "a_text": it["a"]["key"][3], "b_text": it["b"]["key"][3]}
            for it in items]
    (imgdir / f"to-label-s{a.seed}.json").write_text(json.dumps(view, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(items)} 对 -> {dest}；给标注者看的清单（不含分层和各臂的判断）-> {imgdir / f'to-label-s{a.seed}.json'}")
    return 0


RUN_SHARE = {"S1": 0.30, "S2": 0.35, "S3": 0.35}


def sample_runs(L, base: dict[int, int], other: dict[int, int], n: int, seed: int) -> list[dict]:
    """**run 级**抽样：基准是一份 `run 下标 -> 类号`（行级算法没有窗区域，`sample` 那套用不上）。

        S1 分歧对     base 同类 / other 异类，或反过来
        S2 同类随机对 base 同一类、不同的 60 s 窗——base 的误合并率
        S3 近邻异类对 base 不同类、不同窗、两个框 y 重叠 ≥ 半个字高且 x 相交——base 近邻里的误拆分率
    """
    rnd = random.Random(seed)
    good = [i for i, r in enumerate(L.runs) if good_run(r) and i in base]
    win = {i: L.runs[i].t_start // 60_000_000 for i in good}
    members: dict[int, list[int]] = {}
    for i in good:
        members.setdefault(base[i], []).append(i)
    print(f"  [筛子] 像样的 run {len(good)}/{len(L.runs)}；base 里含像样 run 的类 {len(members)} 个"
          f"（其中 ≥2 条的 {sum(1 for v in members.values() if len(v) > 1)}）")

    def near(i: int) -> int | None:
        a = L.runs[i]
        c = [j for j in good if base[j] != base[i] and win[j] != win[i]
             and min(a.box[3], L.runs[j].box[3]) - max(a.box[1], L.runs[j].box[1]) >= 0.5 * min(a.h, L.runs[j].h)
             and min(a.box[2], L.runs[j].box[2]) > max(a.box[0], L.runs[j].box[0])]
        return rnd.choice(c) if c else None

    out, seen = [], set()
    for stratum, share in RUN_SHARE.items():
        got = tries = 0
        while got < round(n * share) and tries < 6000:
            tries += 1
            i = rnd.choice(good)
            if stratum == "S3":
                j = near(i)
            else:
                c = [j for j in members[base[i]] if win[j] != win[i]]
                j = rnd.choice(c) if c else None
                if stratum == "S1" and (j is None or other.get(i) == other.get(j)):
                    j = near(i)                        # 反方向的分歧：base 异类、other 同类
                    if j is not None and other.get(i) != other.get(j):
                        j = None
            k = tuple(sorted((i, j))) if j is not None else None
            if k is None or k in seen:
                continue
            seen.add(k)
            out.append({"stratum": stratum, "runs": [i, j],
                        "together": {"base": base[i] == base[j], "other": other.get(i) == other.get(j)}})
            got += 1
        if got < round(n * share):
            print(f"  ⚠ {stratum} 只抽到 {got}/{round(n * share)}")
    rnd.shuffle(out)
    return out


def pipeline_assign(L, kind: str, model=None) -> dict[int, int]:
    """`build_tracks --cluster <kind>` 的最终分组：run 下标 -> 区域号。缝合用 `L` 的生效参数（同管线），
    之后调管线自己那个函数（`slot_learned.cluster_groups`）——尺量的就是管线产出的分组，不是另抄一份算法。
    `model`：learned 用的模型（尺传留一部片子训的；缺省 = 随代码走的那个）。"""
    from flowocr.analyze import slot_learned
    slot_of_run = {i: k for k, sl in enumerate(CL.stitch(L)) for w in sl for i in L.win_regions[w]["runs"]}
    groups = slot_learned.cluster_groups(kind, L.runs, L.W, L.H, slot_of_run, L.tag, model=model)
    return {i: g for g, ids in enumerate(groups) for i in ids}


def median_other(L) -> dict[int, int | None]:
    """run 下标 -> 默认臂（`median` 缝合）的槽号；run 级抽样的 S1（分歧对）拿它当对照。"""
    med = assignment(L, CL.stitch(L, **ARMS["median"]))
    return {i: med.get((r.t_start, r.t_end, r.text, tuple(round(v) for v in r.box))) for i, r in enumerate(L.runs)}


def write_run_sample(L, a, pairs: list[dict], head: dict) -> int:
    """run 级抽样（`sample-learned` / `sample-lines` / `sample-adjacent`）的共同落盘：摆帧、真值文件、给标注者的盲清单。"""
    root = paths.data_root()
    imgdir = root / "tmp" / "slotpairs" / a.tag
    imgdir.mkdir(parents=True, exist_ok=True)
    dest = root / "data" / "gt" / f"slotpairs-{a.tag}-s{a.seed}.json"
    if dest.exists() and not a.force:
        raise SystemExit(f"{dest} 已存在；要覆盖给 --force")
    items = []
    for n_, p_ in enumerate(pairs):
        pid = f"{a.tag}-s{a.seed}-{n_:03d}"
        render(L, p_, imgdir / f"{pid}.jpg")
        ra, rb = (L.runs[i] for i in p_["runs"])
        items.append({"id": pid, "image": f"tmp/slotpairs/{a.tag}/{pid}.jpg",
                      "a": {"key": run_key(ra), "win": int(ra.t_start // 60_000_000)},
                      "b": {"key": run_key(rb), "win": int(rb.t_start // 60_000_000)},
                      "label": None, "labeled_by": None, "note": "",
                      "stratum": p_["stratum"], "together": p_["together"]}
                     | ({"t_us": p_["t_us"]} if "t_us" in p_ else {}))
    doc = {"schema": SCHEMA, "tag": a.tag, "video": L.video, "obs": str(L.obs), "seed": a.seed, **head,
           "split": a.split, "filter": {"min_content": MIN_CONTENT, "min_obs": MIN_OBS}, "items": items}
    dest.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    view = [{"id": it["id"], "image": it["image"], "a_text": it["a"]["key"][3], "b_text": it["b"]["key"][3]} for it in items]
    (imgdir / f"to-label-s{a.seed}.json").write_text(json.dumps(view, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(items)} 对 -> {dest}")
    return 0


def cmd_sample_learned(a) -> int:
    """照 `--cluster learned` 的分组抽样（模型**留这一部片子不训**），给它报绝对错误率。"""
    from flowocr.analyze import pair_model
    from flowocr.analyze import slot_learned
    L = CL.load(a.tag)
    X, y, meta, _ = pair_model.load(a.train.split(","))
    keep = [k for k, m in enumerate(meta) if m[0] != a.tag]
    model = slot_learned.fit(X[keep][:, slot_learned.COLS], y[keep])
    pairs = sample_runs(L, pipeline_assign(L, "learned", model), median_other(L), a.n, a.seed)
    return write_run_sample(L, a, pairs, {"base": "learned(lofo, pipeline)", "train": a.train, "share": RUN_SHARE})


def cmd_sample_lines(a) -> int:
    """照 `--cluster lines` 的分组抽样，给它一个和 median / learned 对等的抽样框。
    S1 = 和 median 的分歧对，S2 / S3 = lines 的同组 / 近邻异组。"""
    L = CL.load(a.tag)
    pairs = sample_runs(L, pipeline_assign(L, "lines"), median_other(L), a.n, a.seed)
    return write_run_sample(L, a, pairs, {"base": "lines(pipeline)", "share": RUN_SHARE})


ADJ_CO = 0.5
"""S5：两条 run 的时间重叠至少是较短那条时长的这么多，才算"同时在场"。"""
ADJ_GAP_H = 1.0
"""S5：上下两行的竖直间隙不超过这么多倍字高（较高那行）。比 `slot_lines.GAP_H` 宽一点：抽样宁宽，判断交给标注。"""


def adjacent_pairs(L) -> list[tuple[int, int, int]]:
    """S5 的候选：`(i, j, 摆帧时刻 us)`。两条像样的、没动过的 run，同时在场、上下相邻（不是同一行：竖直重叠不到较矮那行的一半）、
    横向有重叠。**只看几何和时间，不看任何臂的分组**——所以 S5 上各臂的读数是同一个率。"""
    good = sorted((i for i, r in enumerate(L.runs) if good_run(r) and not getattr(r, "moving", False)),
                  key=lambda i: L.runs[i].t_start)
    out, active = [], []
    for i in good:
        a = L.runs[i]
        active = [j for j in active if L.runs[j].t_end > a.t_start]
        for j in active:
            b = L.runs[j]
            ov = min(a.t_end, b.t_end) - max(a.t_start, b.t_start)
            if ov < ADJ_CO * min(a.t_end - a.t_start, b.t_end - b.t_start):
                continue
            y_ov = min(a.box[3], b.box[3]) - max(a.box[1], b.box[1])
            if y_ov >= 0.5 * min(a.h, b.h) or -y_ov > ADJ_GAP_H * max(a.h, b.h):
                continue
            if min(a.box[2], b.box[2]) <= max(a.box[0], b.box[0]):
                continue
            t0 = max(a.t_start, b.t_start)
            out.append((j, i, (t0 + min(a.t_end, b.t_end, t0 + 4_000_000)) // 2))
        active.append(i)
    return out


def cmd_sample_adjacent(a) -> int:
    """S5（同刻相邻行）：在候选里均匀抽，一条 run 至多进一对（别让一屏密排的菜单占满样本）。"""
    L = CL.load(a.tag)
    cand = adjacent_pairs(L)
    rnd = random.Random(a.seed)
    rnd.shuffle(cand)
    used, pairs = set(), []
    for i, j, t in cand:
        if i in used or j in used:
            continue
        used |= {i, j}
        pairs.append({"stratum": "S5", "runs": [i, j], "t_us": int(t), "together": {}})
        if len(pairs) >= a.n:
            break
    print(f"  [S5] 候选 {len(cand)} 对（像样的 run {sum(1 for r in L.runs if good_run(r))}），抽 {len(pairs)}")
    return write_run_sample(L, a, pairs, {"base": "adjacent(S5)", "share": {"S5": 1.0},
                                          "adjacent": {"co": ADJ_CO, "gap_h": ADJ_GAP_H}})


def assignment(L, slots: list[list[int]]) -> dict[tuple, int]:
    out = {}
    for k, sl in enumerate(slots):
        for w in sl:
            for i in L.win_regions[w]["runs"]:
                r = L.runs[i]
                out[(r.t_start, r.t_end, r.text, tuple(round(v) for v in r.box))] = k
    return out


def item_key(side: dict) -> tuple:
    t0, t1, box, text = side["key"]
    return (t0, t1, text, tuple(box))


ANCHOR_IOU = 0.5
ANCHOR_SIM = 0.6


def reanchor(key: tuple, runs: list) -> tuple | None:
    """标注里的 run 在当前的层里对不回（建 run 的代码改了，起止 / 文本 / 框变了一点）时，找它现在是哪一条：
    时间重叠 ≥ 两者较短时长的一半、框 IoU ≥ `ANCHOR_IOU`、文本相似 ≥ `ANCHOR_SIM`，得分（IoU + 相似 + 时间重叠比）**唯一最高**的那条。
    找不到或并列就是 None（照旧记"对不回"）。只在精确键对不上时用，所以对得上的样本一条不动。"""
    t0, t1, text, box = key
    best, best_s, tie = None, -1.0, False
    for r in runs:
        ov = min(t1, r.t_end) - max(t0, r.t_start)
        if ov <= 0 or ov < 0.5 * max(1, min(t1 - t0, r.t_end - r.t_start)):
            continue
        g = bt.iou(list(box), r.box)
        if g < ANCHOR_IOU:
            continue
        s = bt.text_sim(text, r.text)
        if s < ANCHOR_SIM:
            continue
        sc = g + s + ov / max(1, max(t1 - t0, r.t_end - r.t_start))
        if sc > best_s + 1e-9:
            best, best_s, tie = r, sc, False
        elif abs(sc - best_s) <= 1e-9:
            tie = True
    if best is None or tie:
        return None
    return (best.t_start, best.t_end, best.text, tuple(round(v) for v in best.box))


def verdicts(assigns: dict[str, dict], it: dict, runs: list | None = None) -> dict[str, bool] | None:
    """一对标注在各臂上判得对不对。**任何一条臂对不回任何一端，就返回 None**——这一对不进任何臂的分母。
    直接写 `asg.get(a) == asg.get(b)` 是错的：两端都找不到时 `None == None` 被当成"同类"，
    标 `same` 的丢失样本就成了"预测正确"；而且各臂丢的不一样时分母也不一致（Codex 审计 2026-09-21）。
    给了 `runs`（当前的层）就先把对不回的一端重新锚定（`reanchor`），各臂共用同一份 run，锚定结果对各臂一致。
    两端锚到**同一条** run（切行拼接把标注时的两块拼成了一条）也返回 None：这一对在当前的层里已经不是两条 run，
    照算的话各臂都白拿"同槽"（2026-09-26 审计）。"""
    ka, kb, out = item_key(it["a"]), item_key(it["b"]), {}
    if runs is not None:
        any_asg = next(iter(assigns.values()))
        ka = ka if ka in any_asg else (reanchor(ka, runs) or ka)
        kb = kb if kb in any_asg else (reanchor(kb, runs) or kb)
        if ka == kb:
            return None
    for name, asg in assigns.items():
        if ka not in asg or kb not in asg:
            return None
        out[name] = (asg[ka] == asg[kb]) == (it["label"] == "same")
    return out


_ASSIGN: dict[str, dict] = {}
_RUNS: dict[str, list] = {}


def score_doc(doc: dict, arms: dict[str, dict], extra=None) -> dict:
    """每条臂：误合并 = 标 different 却同槽；误拆分 = 标 same 却异槽。`widget` / `unsure` 不进分母。
    S1 是按分歧挑的，**单独报**；S2 / S3 / S4 才是率。对不回的 run 先重新锚定（`reanchor`），锚上的记 `(层, "reanchored")`。"""
    res = {}
    if doc["tag"] not in _ASSIGN:                       # 同一部片子的几批标注共用一次缝合
        L = CL.load(doc["tag"])
        _RUNS[doc["tag"]] = L.runs
        _ASSIGN[doc["tag"]] = {name: assignment(L, CL.stitch(L, **over)) for name, over in arms.items()}
        for name, fn in (extra or {}).items():
            got = fn(L)                                  # 槽位表（窗区域下标）或 run 下标 -> 槽号
            _ASSIGN[doc["tag"]][name] = (
                {(r.t_start, r.t_end, r.text, tuple(round(v) for v in r.box)): got[i]
                 for i, r in enumerate(L.runs) if i in got} if isinstance(got, dict) else assignment(L, got))
    assigns = _ASSIGN[doc["tag"]]
    res = {name: Counter() for name in assigns}
    any_asg = next(iter(assigns.values()))
    for it in doc["items"]:
        if it["label"] not in ("same", "different"):
            continue
        v = verdicts(assigns, it, _RUNS.get(doc["tag"]))  # 任何一条臂对不回 -> 各臂都记 lost：**各臂的分母必须一致**
        moved = (item_key(it["a"]) not in any_asg) or (item_key(it["b"]) not in any_asg)
        for name in assigns:
            if v is not None and moved:
                res[name][(it["stratum"], "reanchored")] += 1
            if v is None:
                res[name][(it["stratum"], "lost")] += 1
            else:
                tog = v[name] == (it["label"] == "same")
                res[name][(it["stratum"], it["label"], "together" if tog else "apart")] += 1
    return res


def cmd_merge(a) -> int:
    """把标注者写的 `labels*.json` 并进真值文件。id 要逐条对上；已有标注不覆盖（除非 --force）。"""
    gt = Path(a.gt)
    doc = json.loads(gt.read_text(encoding="utf-8"))
    labs = {x["id"]: x for x in json.loads(Path(a.labels).read_text(encoding="utf-8"))}
    ids = [it["id"] for it in doc["items"]]
    if set(labs) != set(ids):
        raise SystemExit(f"id 对不上：标注多 {sorted(set(labs) - set(ids))[:3]}、少 {sorted(set(ids) - set(labs))[:3]}")
    n = 0
    for it in doc["items"]:
        x = labs[it["id"]]
        if x["label"] not in LABELS:
            raise SystemExit(f"{it['id']}: 未知标签 {x['label']!r}")
        if it["label"] is not None and not a.force:
            continue
        it.update(label=x["label"], labeled_by=a.by,
                  note=" / ".join(v for v in (x.get("what_a"), x.get("what_b"), x.get("note")) if v))
        n += 1
    gt.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{gt.name}: 并入 {n} 条（{a.by}）；{dict(Counter(it['label'] for it in doc['items']))}")
    return 0


def cmd_score(a) -> int:
    from flowocr.analyze import slot_modes
    from flowocr.analyze import slot_veto
    extra = {"modes": slot_modes.stitch, "lines": lambda L: pipeline_assign(L, "lines"), "veto": slot_veto.stitch,
             "veto+soft": lambda L: slot_veto.stitch(L, soft=0.3),
             "soft-only": lambda L: slot_veto.stitch(L, td_max=99.0, soft=0.3)}
    tot: dict[str, Counter] = {}
    for p in a.gt:
        doc = json.loads(Path(p).read_text(encoding="utf-8"))
        if a.split and doc.get("split") != a.split:
            continue
        lab = Counter(it["label"] for it in doc["items"])
        print(f"[{doc['tag']} s{doc['seed']} {doc.get('split')}] {dict(lab)}")
        for name, c in score_doc(doc, ARMS, extra).items():
            tot.setdefault(name, Counter()).update(c)
    strata = ("S1", "S2", "S3", "S4", "S2-4", "S5")
    for c in tot.values():                               # S2-4 = 三个"率"层合起来（S1 按分歧挑，不并）
        for k, v in list(c.items()):
            if k[0] in ("S2", "S3", "S4"):
                c[("S2-4",) + k[1:]] += v
    any_c = next(iter(tot.values()))
    n_of = {s: sum(v for k, v in any_c.items() if k[0] == s and len(k) == 3) for s in strata}
    print("\n每格 = 误合并+误拆分=合计；分母是该层标了 same 或 different 的对数（"
          + "、".join(f"{s} n={n_of[s]}" for s in strata) + "）")
    print(f"{'臂':<12}" + "".join(f"{s:>14}" for s in strata))
    for name, c in tot.items():
        row = f"{name:<12}"
        for s in strata:
            fm, fs = c[(s, "different", "together")], c[(s, "same", "apart")]
            row += f"{f'{fm}+{fs}={fm + fs}':>14}"
        lost = sum(v for k, v in c.items() if k[1] == "lost" and k[0] != "S2-4")
        moved = sum(v for k, v in c.items() if k[1] == "reanchored" and k[0] != "S2-4")
        print(row + (f"   ⚠ 对不回 run {lost}" if lost else "") + (f"   （重新锚定 {moved} 对）" if moved else ""))
    print("\n误合并 = 标 different 却同槽；误拆分 = 标 same 却异槽；widget / unsure 不进分母。\n"
          "⚠ S1 按各臂分歧挑，只能比臂。S2（默认臂同槽）/ S3（默认臂近邻异槽）对**默认臂**是无偏的率，\n"
          "  对别的臂是『同一批对上的表现』：默认臂在 S2 只可能误合并、在 S3 只可能误拆分，别的臂两种都可能。\n"
          "  S5（同刻相邻行）不照任何臂抽，各臂在它上面是同一个率；不并进 S2-4。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("tag")
    s.add_argument("--n", type=int, default=40)
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--split", default="dev", choices=("dev", "acceptance"))
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_sample)
    sl = sub.add_parser("sample-learned")
    sl.add_argument("tag")
    sl.add_argument("--n", type=int, default=40)
    sl.add_argument("--seed", type=int, default=4)
    sl.add_argument("--train", default="seed1,seed2,seed3")
    sl.add_argument("--split", default="dev", choices=("dev", "acceptance"))
    sl.add_argument("--force", action="store_true")
    sl.set_defaults(fn=cmd_sample_learned)
    sn = sub.add_parser("sample-lines")
    sn.add_argument("tag")
    sn.add_argument("--n", type=int, default=40)
    sn.add_argument("--seed", type=int, default=5)
    sn.add_argument("--split", default="dev", choices=("dev", "acceptance"))
    sn.add_argument("--force", action="store_true")
    sn.set_defaults(fn=cmd_sample_lines)
    sa = sub.add_parser("sample-adjacent")
    sa.add_argument("tag")
    sa.add_argument("--n", type=int, default=20)
    sa.add_argument("--seed", type=int, default=6)
    sa.add_argument("--split", default="dev", choices=("dev", "acceptance"))
    sa.add_argument("--force", action="store_true")
    sa.set_defaults(fn=cmd_sample_adjacent)
    m = sub.add_parser("merge")
    m.add_argument("gt")
    m.add_argument("labels")
    m.add_argument("--by", required=True, help="labeled_by：谁标的（sonnet / fable / owner）")
    m.add_argument("--force", action="store_true")
    m.set_defaults(fn=cmd_merge)
    c = sub.add_parser("score")
    c.add_argument("gt", nargs="+")
    c.add_argument("--split", default="", help="只量 dev 或 acceptance")
    c.set_defaults(fn=cmd_score)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
