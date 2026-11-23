"""把条目的起止时间回抠到视频帧精度，并分辨文字的出现方式——**结算**那一半 + CLI。

每条条目给三个时刻而不是两个：

    t_start   首字出现
    t_full    全字出现
    t_end     全字消失

`t_end` 用框内像素的归一化相关系数：拿 run 中间那次观测的裁剪当模板，最后一帧仍像它就还在。
**首字 / 全字是多信号融合**（`flowocr.extract.typewriter_fuse`，2026-09-12 起）：OCR 中途读数外推、
首字格"开始出现"、逐字格定稿时刻的稳健直线、尾字格定稿，OCR 证据给硬区间，投票取同意最多的那簇。
原来的"整框相关系数过门"在打字机上系统性偏早（最后一两个字对整框贡献太小，没打完就过门），
五款游戏的盲标上全字出现几乎全在 4 帧以外（text-effects 报告）。

2026-09-22 拆成两半（正规化 project-structure）：**像素测量**在 `flowocr.extract.refine_video`（开视频、取裁剪、
`measure()` 写 `job["measured"]`），这里只做**在已有证据上结算**——`refine_from_obs` 从 obs 行的 `edge`（在线量的）
重建同一种 `measured`，`apply` 把 `measured` 结算成三个时刻，`attach_ocr` 挂 OCR 中途读数。`refine_tracks` 串起
"测量 + 结算"：`build_tracks --refine auto`（默认，2026-09-25）用 obs 证据那条路在建轨末尾结算；开视频测的两条路（cv2 / ffmpeg）只给开发探针
`dev_tools/refine_offline_probe.py` 核对在线证据用。**没有命令行入口**（owner 2026-09-26：测量只在阶段 1、结算只在阶段 2，不另存 `-tracks-refined.json`）。
提取侧的名字这里 re-export，老调用方的 `rb.make_job` / `rb.measure` 不变。

2 fps 采样意味着边界天然有 ±0.25 s 的量化误差，而真正缺的信息只在**每条 run 首尾那一段**里：
单向顺序解码一遍视频（局部 seek 比顺序读贵：1080p60 上 HEVC 约 35 倍、VP9 约 64 倍，随编码和 GOP 变，boundary-refinement 报告），窗内的帧 retrieve、窗外只 grab。

obs 里没有在线证据（旧 obs、`--no-refine-fused`、非默认的解码器 / 复用设置）时，建轨停在采样级并提示；
要帧级时刻就用默认设置重跑阶段 1（`run_ocr2` 默认在线量证据）。
"""
from __future__ import annotations

import bisect
import json
import statistics
from collections import defaultdict
from pathlib import Path

from flowocr.analyze import build_tracks as bt
from flowocr.artifacts import tracksio
from flowocr.extract import edge_refine, ptsclock
from flowocr.extract import typewriter_fuse as TF
from flowocr.extract.framegrid import TimeGrid
from flowocr.extract.refine_video import (  # noqa: F401   提取那一半（re-export 给老调用方）
    LEAD_US, PAD, SEEK_WORTH_FRAMES, crop_of, first_true, last_true, make_job, measure, refine_ffmpeg,
    refine_sequential, video_codec, video_fps, wants,
)


CODE_FP_FILES = ("src/flowocr/analyze/refine_boundaries.py", "src/flowocr/extract/refine_video.py",
                 "src/flowocr/extract/typewriter_fuse.py",
                 "src/flowocr/extract/edge_refine.py", "src/flowocr/artifacts/tracksio.py",
                 # `ptsclock` 定的是窗口开在哪一帧、以及帧号换回什么时刻——**它一变，回抠的数就变**
                 # （2026-09-18 审计补上；`framesource` 的窗口 seek 也读同一份时钟）
                 "src/flowocr/extract/ptsclock.py", "src/flowocr/extract/framesource.py")
"""回抠这一级的代码指纹覆盖哪些文件（口径同 build_tracks.code_fp，仓库相对路径）。"""

FADE_MAX_US = 500_000
"""完全显示结束最多比完全消失早这么多（30 帧 @60fps）；实测最长淡出约 18 帧。"""

MAX_GROW_US = 30_000_000
"""按 obs 证据回抠时，一条 run 的采样级全字最多比它的起点晚这么久（找段首行只往前看这么远）。"""

def obs_sample_grid(meta: dict) -> TimeGrid:
    """obs 的采样网格（帧号上）：`_meta.sample_grid`（2026-09-24 起）；更早的只有等距的 `stride`。"""
    if meta.get("sample_grid"):
        return TimeGrid(*meta["sample_grid"])
    if meta.get("stride"):
        return TimeGrid.every(int(meta["stride"]))
    raise ValueError("obs 的 _meta 既没有 sample_grid 也没有 stride：判不了回抠证据的查找终点（开放边界），用默认设置重跑阶段 1")


def refine_from_obs(jobs: list[dict], obs_path: Path, us_per_frame: float, frame_us: int,
                    salvage_stale_end: bool = bt.REFINE_SALVAGE_END) -> dict:
    """两遍合一遍（flowocr.extract.edge_refine）：不开视频，从 obs 行的 `edge` 证据重建 `measure()` 的输出。
    段首行的 `on` 给首字那一侧、采样级全字之前最后一段的 `on` 给全字那一侧、run 末行的 `off` 给结尾。
    `salvage_stale_end`：被回补作废、窗口却凑齐了的结尾证据照样认领（理由见认领处）。返回对账用的计数。"""
    if not jobs:
        return {}
    n_salvage = got_salvaged = 0
    order = sorted(jobs, key=lambda j: j["run"]["t_start"])
    starts = [j["run"]["t_start"] for j in order]
    # "前一票"（起点前一个采样点）按 **1.5 个名义间隔**找，不按恰好一个间隔：时间网格上采样间隔不等距（差一帧，2026-09-24），
    # gi-s2 --fps 2.2 上前一票在 466.7 ms 前、名义间隔 454.5 ms，差 12 ms 整条证据没认领上。等距时认领到的集合不变（再前一票在 2 个间隔处）
    prev_tol = int(1.5 * frame_us)
    by_last: dict[int, list[dict]] = defaultdict(list)
    for j in jobs:
        by_last[j["run"]["t_end"] - frame_us].append(j)
    n_on = n_off = 0
    # 证据行和 run 的配法：**同一行 + x 重叠过半**（`edge_refine.overlap_match`），不是 ocr_points 那个"左沿差不到 1.5 个字高"——
    # det 把一行切成两个框时（gi-s2 `ハツ。` + `もしオレが…`），宽框那条链的证据窗才是全字那一侧的，
    # 它的左沿离 run 左沿 100 px，按左沿判被拒、只剩窄框那条早了 48 帧的证据（2026-09-16）。
    # 同一时刻多条都配上：取 x 重叠最大的；全字那一侧再在所有候选里取模板最晚（hi 最大）的
    with open(obs_path, encoding="utf-8") as f:
        sgrid = obs_sample_grid(json.loads(next(f))["_meta"])
        for line in f:
            if '"edge"' not in line:
                continue
            o = json.loads(line)
            ev, t, box = o.get("edge") or {}, o["t_us"], o["box"]
            if "on" in ev and edge_refine.usable(ev["on"]):
                n_on += 1
                for j in order[bisect.bisect_left(starts, t - MAX_GROW_US):bisect.bisect_right(starts, t + prev_tol)]:
                    run = j["run"]
                    tfs = run.get("t_full_sampled")
                    # 采样级全字之后**一个采样间隔内**起的段也算（两遍的打字机窗到 t_full_sampled + span 为止：
                    # 最后一两个字在 90% 那一票之后才打完，全字那一侧的证据在那一段上）
                    # 移动的 run（有 boxes 轨迹）拿起点那一帧的框配；起点前一个采样点起的段也收（打字机第一票
                    # 被 build_tracks 单独切成一条时，段首证据挂在那一票上），段的窗要盖到 run 起点才算
                    ov = edge_refine.overlap_match(box, run["boxes"][0][1] if run.get("boxes") else run["box"])
                    if ov and run["t_start"] - prev_tol <= t <= (tfs if tfs is not None else run["t_start"]) + prev_tol \
                            and (t >= run["t_start"]
                                 or edge_refine.rec_time(ev["on"], ev["on"]["hi"], us_per_frame)
                                 >= run["t_start"] - us_per_frame):
                        j.setdefault("ev_on", []).append((t, ov, ev["on"]))
            off = ev.get("off")
            # 被回补作废的结尾证据（`salvage_stale_end`）：回补改写的是那一段的**文本**，结尾窗量的是框里像素什么时候变、
            # 模板是末次观测那一帧——按"末次观测时刻 + 框"认领到 run 上（下面 by_last + overlap_match），这两样回补不改，证据仍然量的是它。
            # **只救回挂着它的那一行仍是同一句的**（证据量的文本和行上最终的文本过 run 续接的门 `bt.SIM_THR`）：换句那条路
            # （`edge_refine._batch` 里 `_end(key, prev[0], …)`）上回补若把这一行也改成了下一句，模板帧上已经是下一句，
            # 量出来的是下一句什么时候消失，恰有一条框重叠的 run 在这一行结束时会拿到错的结尾（09-26 审计）。
            # 不要求逐字相同：回补常把同一句重读出一个形近字（yuka-f1 1679.5 s `桜羽エマ` → `桜羽工マ`），那条证据是对的
            salvage = bool(salvage_stale_end and off and off.get("stale") and not off.get("short")
                           and bt.text_sim(off.get("text", ""), o["text"]) >= bt.SIM_THR)
            if off is not None and (edge_refine.usable(off) or salvage):
                n_off += 1
                n_salvage += salvage
                use = dict(off, stale=False) if salvage else off
                for j in by_last.get(t, []):
                    rb_ = j["run"]["boxes"][-1][1] if j["run"].get("boxes") else j["run"]["box"]
                    ov = edge_refine.overlap_match(box, rb_)
                    if ov and ov > j.get("ev_off_ov", 0.0):
                        j["ev_off"], j["ev_off_ov"] = use, ov
                        j["off_box"] = box             # 最后一次观测的真实框（末段在动的门用，`moving_at_end`）
                        j["ev_off_salvaged"] = salvage
    got_first = got_last = got_off = 0
    for j in jobs:
        run = j["run"]
        ons = j.get("ev_on", [])
        # 退一步：起点**前一票**起的段。前一票 = 起点之前 1.5 个名义间隔内最晚的那一票——不按 `t_start − frame_us` 精确相等配：
        # 时间网格上采样间隔不等距（差一帧），59.94 fps 素材上 pts 取整也会让间隔差 1 µs（2026-09-24）；等距整数帧率时候选只有原来那一票
        before = [t for t, _, _ in ons if run["t_start"] - prev_tol <= t < run["t_start"]]
        at_start = [(ov, r) for t, ov, r in ons if t == run["t_start"]] or \
                   [(ov, r) for t, ov, r in ons if before and t == max(before)]
        first = max(at_start, key=lambda x: x[0])[1] if at_start else None
        last = max(ons, key=lambda x: (x[2]["hi"], x[1]))[2] if ons else None
        if first is None:                    # run 起点那一行没有段首证据（链从更早接过来）：首字那一侧没法量
            last = None
        got_first += first is not None
        got_last += last is not None
        got_off += "ev_off" in j
        got_salvaged += bool(j.get("ev_off_salvaged"))
        m = edge_refine.measured_from_records(first, last, j.get("ev_off"), run["t_end"], us_per_frame, frame_us, sgrid)
        if m is not None:
            j["measured"] = m
    # `on_rows` / `off_rows` 数的是**能用的**证据行（`edge_refine.usable`：窗口凑齐、没被回补作废；开了救回时含救回的结尾证据）；
    # 作废的那些在 obs 的 `_meta.edge.stale_windows` / `_meta.reuse_v2.edge_stale` 里数
    out = {"on_rows": n_on, "off_rows": n_off, "jobs": len(jobs), "jobs_with_on": got_first,
           "jobs_with_off": got_off}
    if salvage_stale_end:
        out.update(off_rows_salvaged=n_salvage, jobs_with_off_salvaged=got_salvaged)
    return out

MOVING_END_SHIFT = 0.5
"""末段在动的门（defaults.md 1.65）：最后一步每个采样间隔平移超过这么多个字高，结尾窗给的、比采样级早的终点就不认。
贴屏边也按它判：框的一条边离画面边缘不到这么多个字高，就算被屏边裁住。"""


def moving_at_end(run: dict, frame_us: int, last_box: list | None = None, frame_size: tuple | None = None) -> bool:
    """这条 run 到最后一个采样点还在**平移**（弹幕飞、评论墙滚、地图拖动）：最后一步每个采样间隔平移超过 `MOVING_END_SHIFT` 个字高。

    结尾窗的证据（`curve` / `ncc`）量的是**最后那一格框**里的像素：字一移出那格就记成消失，可它还在屏上飞，
    终点判早（z-shuigong 的弹幕早到 28 帧）。过门的 run 只拒收比采样级早的终点（`apply`），起点照旧回抠。
    * 最后一步 = 轨迹 `boxes` 的倒数第二点到最后一次观测的框。最后一次观测的框优先用 obs 里那一行的真实框（`last_box`，
      `refine_from_obs` 认领结尾证据时记下）：build_tracks 收尾补的轨迹点是并集框（最后几票都在 IoU 门里时并进上一框），
      并集只有一条边在长，看不出平移；没有就退回轨迹的最后一点。轨迹只有 moving 的 run 才有，静止的 run 永远不过这道门。
    * **判平移、不判形变**：x、y 两轴各看一对对边，两条边同向挪才算平移、取两者里小的那个；一条边贴着屏边（被裁住）时只看另一条。
      原地淡出、det 把旁边的图标并进来（gi1「シヴァル」）、打字机长宽，都只有一条边在动，不过门。
      贴屏边的距离和平移门同一个（`MOVING_END_SHIFT` 个字高）：左沿 / 上沿离屏边不到这么近、最后一步只有远端那条边在长的移动 run，会被当成平移；只在过了门的 moving run 上碰得到，没见过实例。
    * 按两点之间隔了几个采样间隔归一；字高取两框里高的那个（出屏的字框被屏边裁矮）。"""
    pts = run.get("boxes") or []
    if len(pts) < 2:
        return False
    (t0, a), (t1, b) = pts[-2], pts[-1]
    if last_box is not None:
        b = last_box
    h = max(a[3] - a[1], b[3] - b[1])
    edge = MOVING_END_SHIFT * h
    shifts = []
    for lo, hi, lim in ((0, 2, frame_size[0] if frame_size else None), (1, 3, frame_size[1] if frame_size else None)):
        d_lo, d_hi = b[lo] - a[lo], b[hi] - a[hi]
        cut_lo = min(a[lo], b[lo]) <= edge
        cut_hi = lim is not None and max(a[hi], b[hi]) >= lim - edge
        if cut_lo != cut_hi:
            shifts.append(abs(d_hi) if cut_lo else abs(d_lo))
        else:
            shifts.append(min(abs(d_lo), abs(d_hi)) if d_lo * d_hi > 0 else 0)
    steps = max(1, round((t1 - t0) / frame_us))
    return max(shifts) / steps > edge


def apply(job: dict, v_ocr: float | None, v_cell: float | None) -> None:
    """全片解码完再调：首字外推要借全片的打字速度中位数（`v_cell` 来自各条的逐字格斜率），所以融合放在这里。"""
    if "measured" not in job:
        return
    new_e, ok_gone, sig, last_full, end_how = job.pop("measured")
    run = job["run"]
    old_s, old_e = run["t_start"], run["t_end"]
    if end_how is not None and new_e < old_e and moving_at_end(run, job["frame_us"], job.get("off_box"), job.get("frame_size")):
        # 末段在动、结尾窗给的终点比采样级早：字可能只是移出了那一格，终点和完全显示结束都不认，留采样级（`t_end_how = moving`）。
        # 不早于采样级的照收（`open` 的下界、曲线量出的消失落在采样点之后）：门防的错只会把终点判早。治标，见 `moving_at_end`
        new_e, ok_gone, last_full, end_how = old_e, False, None, "moving"
        run["t_end_how"] = end_how
        job["moving_end"] = True
    est = None
    if sig is not None:
        tfs = run.get("t_full_sampled")
        est = TF.combine(sig, old_s, tfs if tfs is not None else old_s, job["frame_us"], job["ocr"], v_ocr, v_cell)
    if est is not None and est["first_how"] == est["full_how"] == "none":
        est = None                                   # 两个时刻都只是采样级的退回值，不算量出来了
    if not ok_gone:
        job["no_evidence"] = True
        if est is None:
            return
    if est is not None:
        new_s, new_full = est["first"], est["full"]
    else:
        new_s, new_full = old_s, None
    # 自选范围的时间关闭段截断的那一端**不找自然边界**（ocr-regions 计划）：
    # "范围在那一刻打开 / 关上"不等于"文字在那一刻出现 / 消失"，回抠也不许量到范围外的时段
    flags = run.get("flags") or ()
    if "clipped_start" in flags:
        new_s = old_s
    if "clipped_end" in flags:
        new_e = old_e
    if new_e <= new_s:
        return
    run["t_start"], run["t_end"] = new_s, new_e
    # 字段留着、值为空——"没量出来"和"没有这个字段"是两回事
    run["t_full"] = None if new_full is None else min(max(new_full, new_s), new_e)
    if est is not None:
        run["t_full_how"] = {"first": est["first_how"], "full": est["full_how"]}
    if run["t_full"] is not None:
        run["grow_ms"] = round((run["t_full"] - run["t_start"]) / 1000)
    # 完全显示结束：不早于全字出现（没有就首字）、不晚于完全消失；没量出来就留 None（同 t_full 的规矩）
    # 另一道下限：不早于完全消失前 FADE_MAX_US——清晰显示短了难看、长了无妨（owner 2026-09-12）；
    # 实测最长淡出约 18 帧（原神慢淡出 / 星铁整屏渐黑），30 帧留余量
    floor = max(run["t_full"] if run["t_full"] is not None else new_s, new_e - FADE_MAX_US)
    run["t_full_end"] = None if last_full is None else min(max(last_full, floor), new_e)
    if end_how:
        run["t_end_how"] = end_how
    job["refined"] = True
    job["moved"] = run["t_start"] != old_s or run["t_end"] != old_e
    job["d_start"] = abs(run["t_start"] - old_s) / 1e6
    job["d_end"] = abs(run["t_end"] - old_e) / 1e6

def attach_ocr(jobs: list[dict], obs_path: Path) -> float | None:
    """把每条 job 打字机窗里的 OCR 观测挂上去（`typewriter_fuse.ocr_points`），返回这段视频的典型打字速度（µs/字）。
    速度只从正文那几类区域的 job 统计（`job["effect_pool"]`，见 `EFFECT_LABELS`；没有这个键的算）。
    obs 只流式扫一遍：按窗口并集二分，不整份读进内存。"""
    wins = sorted(((j["run"]["t_start"], j["run"].get("t_full_sampled") or j["run"]["t_start"], j) for j in jobs),
                  key=lambda w: w[:2])
    starts = [w[0] for w in wins]
    ends_max = []                                    # 前缀最大终点：二分到的左边界之前也可能有窗盖住 t
    m = -1
    for _, e, _ in wins:
        m = max(m, e)
        ends_max.append(m)
    rows: dict[int, list] = defaultdict(list)
    with open(obs_path, encoding="utf-8") as f:
        next(f)                                      # _meta
        for line in f:
            i = line.find('"t_us": ')
            if i < 0:
                continue
            t = int(line[i + 8:line.find(",", i)])
            k = bisect.bisect_right(starts, t) - 1
            if k < 0 or ends_max[k] < t:
                continue
            o = json.loads(line)
            rows[t].append((t, o["box"], o["text"]))
    ts_sorted = sorted(rows)
    for s, e, j in wins:
        a, b = bisect.bisect_left(ts_sorted, s), bisect.bisect_right(ts_sorted, e)
        cand = [r for t in ts_sorted[a:b] for r in rows[t]]
        j["ocr"] = TF.ocr_points(cand, j["box"], TF.chars(j["run"]["text"]))
    vs = [v for j in jobs if j.get("effect_pool", True)
          and (v := TF.ocr_slope(j["ocr"], len(TF.chars(j["run"]["text"]))))]
    # 样本太少不给（`TF.OCR_SLOPES_MIN` 的来历）：两三句的中位数就是那两三句自己，一句假读数就能把全片速度定歪
    return statistics.median(vs) if len(vs) >= TF.OCR_SLOPES_MIN else None

DEFAULT_LABELS = "all"
"""默认回抠哪些标签的区域：全部（defaults.md 1.65）。时刻是逐条量出来的事实，标题、选项、提示、UI 量到证据就该是帧级；
2026-10-24 以前只回抠下面两类，那是开视频回抠的年代按条目数付 seek 钱时留下的，现在结算只读 obs 里阶段 1 已经量好的证据。"""

EFFECT_LABELS = frozenset({"subtitle", "dialogue-or-caption"})
"""整段素材的**文字效果**（`text_effect`：打字机判定、淡出时长）和**典型打字速度**（首字 / 全字外推借的 `v_ocr` / `v_cell`）
只从这几类区域统计。它们说的是正文怎么出现：淡入的标题、即时出现的 UI 混进来会把判定和速度带偏
（宣传片合成素材上全部区域一起统计时，离散度 0.2 → 1.0、判定从 typewriter 掉到 likely-typewriter）。"""


def text_effect_of(regions_view: list[dict], us_per_frame: float) -> dict:
    """整段素材的文字效果 `text_effect`（打字机判定 + 淡出时长），只从 `EFFECT_LABELS` 那几类区域里结算过的 run 统计。"""
    # 打字机是**整段素材的属性**，不是逐条猜出来的。
    # 判据是"每字生长时间"的离散度：真打字机的速度是常数，分布很紧；
    # 淡入、场景切换这类杂音测出来的 grow 则四散。
    effect_regions = [reg for reg in regions_view if reg["label"] in EFFECT_LABELS]
    rates, grows = [], []
    for reg in effect_regions:
        for r in reg["runs"]:
            n = len(TF.chars(r.get("text", "")))
            if "grow_ms" in r and n >= 4:
                rates.append(r["grow_ms"] / n)
                grows.append(r["grow_ms"])
    effect = {"samples": len(rates)}
    if len(rates) >= 8:
        rates.sort()
        q1, med, q3 = (rates[len(rates)//4], statistics.median(rates), rates[3*len(rates)//4])
        spread = (q3 - q1) / med if med > 0 else float("inf")
        # 分三档而不是二值：owner 说判读不求完美，标出"可能是"就够。
        # **不设 ms/char 的绝对下限**（owner 2026-09-04）；唯一的物理下限是"生长必须长到视频帧分辨得出来"。
        med_grow = statistics.median(grows) if grows else 0
        verdict = ("instant" if med_grow < us_per_frame / 1000 else
                   "typewriter" if spread <= 0.7 else
                   "likely-typewriter" if spread <= 1.0 else "instant")
        effect.update({"ms_per_char_median": round(med, 1), "iqr_over_median": round(spread, 2),
                       "verdict": verdict})
    # 淡出：整段素材的属性，和打字机并列记（有 t_full_end 的条目里，完全显示结束到完全消失的中位时长）
    fades = sorted((r["t_end"] - r["t_full_end"]) / 1000 for reg in effect_regions for r in reg["runs"]
                   if r.get("t_full_end") is not None and r.get("t_end_how") == "curve")
    if fades:
        effect["fade_out_ms_median"] = round(statistics.median(fades), 1)
        effect["fade_out_ms_p75"] = round(fades[3 * len(fades) // 4], 1)
        effect["fade_out_samples"] = len(fades)
    return effect


def obs_evidence(obs_path: Path) -> tuple[dict, bool]:
    """obs 的 `_meta`，以及里面**真的有**在线回抠的证据没有。要"真的有证据"才算，不是"配置说开了融合"
    （2026-09-17 审计）：`--refine-aux noop` 那条空臂配置一模一样、产物里一条 `edge` 都没有；这种 obs 以前会被
    auto 挑成 obs，然后整段静默退回采样级。"""
    meta: dict = {}
    if obs_path.is_file():
        with open(obs_path, encoding="utf-8") as f:
            meta = json.loads(f.readline()).get("_meta", {}) or {}
    edge = meta.get("edge") or {}
    fused = bool(meta.get("config", {}).get("refine_fused")) and not edge.get("noop") and int(edge.get("on", 0)) > 0
    return meta, fused


def refine_tracks(data: dict, *, decoder: str, fps: float, obs: Path | None, video: str = "", thr: float = 0.80,
                  labels: str = DEFAULT_LABELS, regions: str = "", window: tuple[int, int] | None = None,
                  step: int = 1, scale: float = 1.0, progress_every: int = 200, obs_code_fp: str = "",
                  salvage_stale_end: bool = bt.REFINE_SALVAGE_END) -> dict:
    """在 `data`（`tracksio.load` 读出的 tracks）上**就地**回抠：测量（obs 证据 / 开视频）-> 结算三个时刻 ->
    整段文字效果 `text_effect` -> `boundary_refined` -> cue 首尾按成员重算。返回打印用的统计。
    `decoder="obs"`（读 obs 里的在线证据）是产物流程唯一的路：`build_tracks --refine auto` 在建轨末尾调它；
    `"cv2"` / `"ffmpeg"`（开视频测）只给开发探针 `dev_tools/refine_offline_probe.py` 核对在线证据。"""
    # **回抠之前把每个事件的首尾抄一份**：cue 的边界只跟着"当初生成它的那个成员边界"走，
    # 靠这份快照配对（tracksio.refresh_cue_bounds 的注释里写了为什么不能按 min/max 重算）。
    before = tracksio.event_times(data)
    # 区域视图**另存一个变量**：`data` 本身要原样写回去，
    # 把带 runs 的视图塞回 data["regions"] 会在产物里多出一份重复的事件。
    regions_view = tracksio.regions_with_runs(data)
    frame_us = data["frame_us"]
    want = None if labels == "all" else set(labels.split(","))
    only = {int(x) for x in regions.split(",") if x.strip()} if regions else None
    us_per_frame = 1_000_000 / fps

    n_runs = n_moved = 0
    start_shifts: list[float] = []
    end_shifts: list[float] = []
    how: dict[str, int] = defaultdict(int)
    evidence: dict = {}
    if obs is None or not obs.is_file():
        raise SystemExit(f"找不到 OCR 观测 {obs}（首字 / 全字融合要用中途读数）：用 --obs 指定")
    # 帧号 <-> 真实时间：拿 obs 逐采样点的 pts 当锚点（`ptsclock.IndexClock`，见 make_job）。
    # 没有锚点（旧产物的时间轴不是 pts）时它就是原来的常量换算
    clock = ptsclock.index_clock_from_obs(obs, us_per_frame)
    print(f"  [时钟] {len(clock)} 个 pts 锚点"
          + ("（帧号 × 1/src_fps，旧时间轴）" if not len(clock) else
             f"；不锚的话首尾差 {(clock.t_of(clock.f[-1]) - clock.f[-1] * us_per_frame) / 1000:+.1f} ms"), flush=True)
    jobs = [dict(make_job(run, frame_us, us_per_frame, clock), effect_pool=region["label"] in EFFECT_LABELS,
                 frame_size=tuple(data["size"]) if data.get("size") else None)
            for region in regions_view
            if (only is None or region["index"] in only)
            and (only is not None or want is None or region["label"] in want)
            for run in region["runs"]
            if not window or (run["t_start"] < window[1] and run["t_end"] > window[0])]
    v_ocr = attach_ocr(jobs, obs)
    if decoder == "obs":
        evidence = refine_from_obs(jobs, obs, us_per_frame, frame_us, salvage_stale_end)
        evidence["obs_code_fp"] = obs_code_fp   # 产这些证据的是哪一版代码（不是本工具的 code_fp）
        print(f"  [证据] obs 里 on {evidence.get('on_rows', 0)} 行 / off {evidence.get('off_rows', 0)} 行；"
              f"{evidence.get('jobs', 0)} 条 run 里段首证据 {evidence.get('jobs_with_on', 0)}、"
              f"结尾证据 {evidence.get('jobs_with_off', 0)}"
              + (f"（其中救回被回补作废的 {evidence['jobs_with_off_salvaged']} 条）" if "jobs_with_off_salvaged" in evidence else ""),
              flush=True)
        # 一条都没认领到 = 这份 obs 配不上这份轨（换了段、换了区域、或者证据全被作废）。
        # **静默退回采样级是错的**，口径同 compare_timing"配上 0 条非零退出"
        if jobs and not evidence["jobs_with_on"] and not evidence["jobs_with_off"]:
            raise SystemExit(f"[证据] {len(jobs)} 条 run 一条证据都没认领到（obs {obs}）："
                             f"obs 和轨对不上，或者证据都被回补作废了。要开视频就给 --decoder ffmpeg / cv2")
    elif decoder == "ffmpeg":
        if step > 1:
            for j in jobs:
                j["step"] = step
                # 窗口两端对齐到 step 的格子，不然"两端都在"永远不成立
                j["tw_lo"] -= j["tw_lo"] % step
                j["hi_start"] -= j["hi_start"] % step
                j["end_lo"] -= j["end_lo"] % step
                j["end_hi"] -= j["end_hi"] % step
                j["lo"] = min(j["lo"], j["tw_lo"], j["end_lo"])
        refine_ffmpeg(video, jobs, thr, progress_every, step=step, scale=scale)
    elif step > 1 or scale < 1.0:
        raise SystemExit("--step / --scale 只在 ffmpeg 窗口模式下有效（--decoder ffmpeg）")
    else:
        refine_sequential(video, jobs, thr, progress_every)
    # 打字速度：OCR 中途读数拟得出来就用它；拟不出来（魔裁：打字快，中途读数都只有一个）
    # 借逐字格直线斜率的全片中位数——**只给首字外推用**，借给全字外推在淡入字体上系统性偏晚
    speeds = [v for j in jobs if j.get("effect_pool", True) and (v := TF.cell_speed(j["measured"][2] if "measured" in j else None))]
    v_cell = statistics.median(speeds) if speeds else None
    for j in jobs:
        apply(j, v_ocr, v_cell)
    # n_runs 只数**真回抠成功**的（模板取失败、终点没观测到的不算）
    done = [j for j in jobs if j["refined"]]
    n_runs = len(done)
    n_moved = sum(1 for j in done if j["moved"])
    start_shifts = [j["d_start"] for j in done]
    end_shifts = [j["d_end"] for j in done]
    for j in done:
        h = j["run"].get("t_full_how")
        how[f"首字 {h['first']}" if h else "首字 未量"] += 1
        how[f"全字 {h['full']}" if h else "全字 未量"] += 1
        how[f"消失 {j['run'].get('t_end_how', '未量')}"] += 1     # open = 窗里没看到消失，t_end 只是下界
    # 末段在动、终点留采样级的（`t_end_how = moving`）另数：起点也没量到的那些不算回抠成功，上面的 `how` 数不到
    n_moving_end = sum(1 for j in jobs if j.get("moving_end"))

    data["text_effect"] = text_effect_of(regions_view, us_per_frame)
    mean_start = sum(start_shifts) / len(start_shifts) if start_shifts else 0.0
    mean_end = sum(end_shifts) / len(end_shifts) if end_shifts else 0.0
    data["boundary_refined"] = {"thr": thr, "labels": labels, "regions": regions or "all",
                                "typewriter": "fuse", "code_fp": bt.code_fp(CODE_FP_FILES),
                                "decoder": decoder, "step": step, "scale": scale,
                                **({"evidence": evidence} if decoder == "obs" else {}),
                                "runs": n_runs, "moved": n_moved, "how": dict(how), "end_moving": n_moving_end,
                                # 起点/终点分开报，别再合成一个"平均移动"
                                "mean_start_shift_s": round(mean_start, 4),
                                "mean_end_shift_s": round(mean_end, 4)}
    # 回抠**只更新时间**：cue 的首尾按成员重算，切分本身一个字不动（audit-4 C6）。
    # 起点口径（first / full）**不写进 JSON**，交给导出器投影——写回去就切不回来了
    tracksio.refresh_cue_bounds(data, before)
    return {"runs": n_runs, "moved": n_moved, "mean_start": mean_start, "mean_end": mean_end, "how": dict(how),
            "end_moving": n_moving_end}


def refine_code_changed(doc: dict) -> str | None:
    """产物回抠过（有 `boundary_refined`）、而产它的回抠代码和现在的不一样时，返回一句原因；否则 None。
    评测驱动判建轨缓存用：`--refine auto` 起建轨产物里的时刻由这一级的代码算，只看 build_tracks.py 的 mtime
    会在只改了回抠代码时静默复用旧产物（2026-09-26 复查）。"""
    br = doc.get("boundary_refined")
    if not br:
        return None
    now = bt.code_fp(CODE_FP_FILES)
    return None if br.get("code_fp") == now else f"回抠代码指纹对不上：产物是 {br.get('code_fp')}，现在是 {now}"


def summary_lines(tag: str, st: dict, data: dict, fps: float, decoder: str) -> list[str]:
    """回抠之后打印的几行（CLI 与 build_tracks 同一份）。"""
    out = [f"{tag}: 回抠成功 {st['runs']} 条，其中 {st['moved']} 条边界被移动，"
           f"起点平均移动 {st['mean_start']:.3f}s、终点 {st['mean_end']:.3f}s"
           f"（{fps:.0f} fps，采样间隔 {data['frame_us']/1e6:.2f}s，{decoder}）"]
    if st["how"]:
        out.append("   首字 / 全字 / 消失从哪来：" + "、".join(f"{k} {v}" for k, v in sorted(st["how"].items())))
    if st.get("end_moving"):
        out.append(f"   末段在动、终点留采样级（t_end_how = moving）：{st['end_moving']} 条")
    eff = data["text_effect"]
    if "verdict" in eff:
        out.append(f"   文字效果：**{eff['verdict']}** —— 每字生长 {eff['ms_per_char_median']} ms/char，"
                   f"离散度 IQR/中位 = {eff['iqr_over_median']}"
                   f"（≤0.7 打字机 / ≤1.0 疑似 / 其余一次性，样本 {eff['samples']} 条）")
    return out
