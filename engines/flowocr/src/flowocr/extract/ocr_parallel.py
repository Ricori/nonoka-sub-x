"""`run_ocr2 --workers N` 里**不碰模型的那部分**：切段、给子进程改参数、把各段的 `_meta` 合成一份。

放共享层是为了守卫够得着（audit-4 C8）。依据（fps-and-rec-budget 报告）：单进程时 GPU
大半时间在等主线程的 CPU 活，N 个进程各跑一段能把空档填上——3 个 worker 在 quick 三段上 −26%~−47%。

**切点对齐采样格**：切点帧号在采样网格上（`framegrid.TimeGrid`）。代价之一：每段开头没有"上一帧"，复用链从头开始、
强制刷新换了相位——所以 `workers` 是**产物参数**（进 `_meta.config`），两种跑法的产物不混用。

⚠ **有 pts 缺口 / 平均帧率偏离名义帧率的素材上不能开**（2026-09-10 审查）。子进程的起点是
`切点帧号 / src_fps` 秒——和 `--start` 同一个"帧号 ÷ 平均帧率"的换算——而 `src_fps` 是容器的
**平均**帧率。一段里若含丢帧缺口，它实际覆盖的时间比算出来的长，和下一段**重叠**（f3 实测 +4.65 s）；
不含缺口时它比算出来的短，和下一段之间**漏一截**（f3 上按平均帧率推，每个切点可达数秒，没实测），
而且 PTS 仍然递增、**段间递增判据拦不住**。所以两道门：
父进程先按 `seam_drift_sec` 预估**整个文件**上的切点漂移，超过半个源帧就拒绝；
拼的时候再逐个切点核首尾间隔（`seam_problems`），重叠和漏帧都判未完成。
漂移**按整个文件**估、门限**半个源帧**（复审 2026-09-10）：按窗口长度估的话，f3 上 5 分钟窗口
算出 0.109 s 会被放行，段与段之间静默差出好几个源帧——落在切点核对的容差里、拦不住。
按整个文件估，有缺口的素材不论取哪段都拒；被放行的素材（f5 整片 0.001 s、游戏切片 0）
切点漂移不到半帧，**采样帧和单进程逐帧相同**。
`r_frame_rate` 当名义帧率在某些容器上可能是时基派生值、造成误拒；现有素材没撞上，报错里两个帧率都印着。
根治要把窗口改成按 pts 定义（fps-and-rec-budget 报告第 1 条的待定项）。
"""
from __future__ import annotations

import json
import subprocess

from flowocr.extract import ocr_complete

SUM_KEYS = ("frames_advanced", "frames_requested", "sampled_frames", "boxes", "rec_calls",
            "rec_predict_calls", "reused_boxes", "dropped_dense", "dropped_tiny", "lost_rec", "grid_skipped")
"""各段相加就是整体的计数。"""

DICT_SUM_KEYS = ("reuse_v2", "reuse_v2_sec", "edge", "stage_sec", "rec_spec", "grid_off")
"""各段里是"计数字典"的键，逐键相加进整体（数值型的键才加；bool 不算）。"""

DICT_MAX_KEYS = frozenset({"cache_peak", "aux_shift"})
"""这些键**不是计数，是峰值 / 偏移**：取各段的最大值，不然 `--reuse-cache-mb 8` 的三个 worker
会被加成 21 MB（2026-09-17 审计时从产物里发现的口径错）。"""

SEAM_TOL = 0.5
"""切点处相邻两段首尾的间隔，允许偏离一个采样间隔多少（倍）。正常就是恰好一个采样间隔。"""

DRIFT_MAX_FRAMES = 0.5
"""整个文件上预估的切点漂移不许超过几个**源帧**。半帧以内，切点落在哪一帧和单进程一致。
f5 整片 0.001 s（0.06 帧）、游戏切片 0；f3 12.1 s、f4 6.1 s。"""


def split_frames(start_idx: int, end_idx: int, grid, n: int) -> list[int]:
    """`[start_idx, end_idx)` 等分成 n 段，返回 n−1 个**内部切点帧号**（在采样网格 `grid` 上、严格递增；`start_idx` 在格上）。

    段太短切不开时抛——那种窗口本来就不值得并行。
    """
    if n < 2:
        return []
    span = end_idx - start_idx
    k0 = grid.index(start_idx)
    cuts = [grid.at(k0 + round(span * k / n * grid.p / grid.q)) for k in range(1, n)]   # 等距时就是 start + round(…/步长) × 步长
    pts = [start_idx, *cuts, end_idx]
    if any(b <= a for a, b in zip(pts, pts[1:])):
        raise ValueError(f"窗口太短，切不成 {n} 段：{pts}")
    return cuts


def strip_opts(argv: list[str], names: set[str]) -> list[str]:
    """从 argv 里去掉这些**带值**的选项（`--x v` 和 `--x=v` 两种写法都认），其余原样保留。"""
    out, skip = [], False
    for tok in argv:
        if skip:
            skip = False
            continue
        name = tok.split("=", 1)[0]
        if name in names:
            skip = "=" not in tok
            continue
        out.append(tok)
    return out


def parse_rate(s: str) -> float | None:
    """ffprobe 的 `60/1`、`60000/1001` → 浮点；`0/0`、`N/A`、空 → None。"""
    try:
        num, den = s.split("/") if "/" in s else (s, "1")
        num, den = float(num), float(den)
    except ValueError:
        return None
    return num / den if num > 0 and den > 0 else None


def probe_rates(video: str) -> tuple[float | None, float | None]:
    """`(名义帧率 r_frame_rate, 平均帧率 avg_frame_rate)`。探不到的给 None。"""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=r_frame_rate,avg_frame_rate", "-of", "json", video],
                             capture_output=True, text=True, timeout=60).stdout
        st = json.loads(out)["streams"][0]
    except Exception:
        return None, None
    return parse_rate(st.get("r_frame_rate", "")), parse_rate(st.get("avg_frame_rate", ""))


def seam_drift_sec(span_frames: int, avg_fps: float, nominal_fps: float) -> float:
    """按"帧号 ÷ 平均帧率"换算切点时，`span_frames` 帧上最多能漂出多少秒。
    调用方传**整个文件**的帧数，门限才是素材的属性、不随窗口变。"""
    return span_frames * abs(1.0 / avg_fps - 1.0 / nominal_fps)


def seam_problems(metas: list[dict], interval: float, tol: float = SEAM_TOL) -> list[str]:
    """逐个切点核：下一段的首帧 PTS − 上一段的末帧 PTS 应该恰好是一个采样间隔。
    ≤ 0 是**重叠**，太大是**漏帧**——后者 PTS 仍递增，只有这里能拦。"""
    out = []
    for k in range(len(metas) - 1):
        a, b = metas[k].get("pts_last_sec"), metas[k + 1].get("pts_first_sec")
        if a is None or b is None:
            out.append(f"切点 {k}：段首 / 段尾 PTS 缺失")
            continue
        d = b - a
        if d <= 0:
            out.append(f"切点 {k}：重叠 {-d:.3f} s（{a:.3f} → {b:.3f}）")
        elif abs(d - interval) > tol * interval:
            out.append(f"切点 {k}：间隔 {d:.3f} s，应为 {interval:.3f} s"
                       + ("（漏帧）" if d > interval else ""))
    return out


CHILD_ARGS = ["--workers", "1", "--decode-shards", "0"]
"""`--workers N` 的子进程**追加**的参数（排在父进程 argv 之后，同名的后者生效）。
解码分片和多 worker 互斥（`ocr_args.resolve_split`），可子进程只看得见 `--workers 1`、会按默认再开几路——
`--workers 3` 就成了 6~9 个解码上下文（2026-09-24 审计）。所以由父进程显式关掉；它们在 SCHEDULING 里，不改复用判据。"""


def merge_metas(metas: list[dict], base: dict, *, wall: float, cuts_sec: list[float],
                interval: float) -> dict:
    """各段的 `_meta` → 整体的 `_meta`。`base` 是父进程按同一组参数建的那份（video / config / 模型…）。

    **完成**按**整个窗口**判，和单进程同一把尺：
    - 覆盖率：各段推进帧数之和 / 请求帧数之和过 `ocr_complete.is_complete`。
      **不能要求每段各自过**——`end=0` 时容器多报的那 1% 帧数全压在最后一段上，
      3 个 worker 时末段只有 97%，单进程是 99%（审查 2026-09-10）；
    - **中间各段**必须各自完成（它们的请求帧数是切出来的，不含容器的虚报）；
    - 没有坏 PTS，切点处首尾间隔恰好一个采样间隔（`seam_problems`）。
    """
    m = dict(base)
    for k in SUM_KEYS:
        m[k] = sum(x.get(k, 0) or 0 for x in metas)
    for k in DICT_SUM_KEYS:                          # 各段的计数字典逐键相加（reuse_v2 / edge 的统计）
        parts = [x[k] for x in metas if isinstance(x.get(k), dict)]
        if parts:
            def agg(kk: str, parts=parts):
                vals = [d[kk] for d in parts if isinstance(d.get(kk), (int, float))]
                # **峰值不能相加**（2026-09-17）：三个 worker 各自的 8 MB 上限被加成 21 MB，
                # 看着像"上限没守住"，其实是口径错了。峰值取最大，其余相加
                if kk in DICT_MAX_KEYS:
                    return round(max(vals), 2) if any(isinstance(v, float) for v in vals) else max(vals)
                return (round(sum(float(v) for v in vals), 2) if any(isinstance(v, float) for v in vals)
                        else sum(int(v) for v in vals))
            m[k] = {kk: agg(kk)
                    for kk in dict.fromkeys(kk for d in parts for kk in d if isinstance(d.get(kk), (int, float)))}
    # 实际用了几个 worker：退回单进程时 run_ocr2 自己写 1，并行跑完由这里写段数——
    # 不写的话并行产物里这个键干脆没有，读的人分不出"三个 worker"和"旧产物"（2026-09-17）
    m["workers_effective"] = len(metas)
    splits = [x["proc_split"] for x in metas if "proc_split" in x]   # 拆进程的生效值（各段一样就记一份，不一样逐段列）
    if splits:
        m["proc_split"] = splits[0] if all(sp == splits[0] for sp in splits) else splits
    # 硬解的生效值（2026-09-20 整体检查时补的）：父进程早早 `return run_parallel`、**不做**试解，
    # 而这里又是从父进程那份 `base` 起步——不写的话各段 `hwaccel_effective` 全丢，
    # 读的人分不出"真走了硬解"和"每段都退回了软解"（和上面 workers_effective 同一个形状）。
    # 各段可能不一致（有前摇的 av1 只有**第一段**从文件开头起解，后面几段 seek 到非零位置都没事）
    effs = [x.get("hwaccel_effective") for x in metas if "hwaccel_effective" in x]
    if effs:
        m["hwaccel_effective"] = (effs[0] if len(set(effs)) == 1
                                  else "mixed:" + ",".join(e or "soft" for e in effs))
    # 推理设备的生效值 / 用的模型文件同一个形状（2026-09-22 接手自查）：父进程不建模型、`base` 里没有，不抄就全丢。
    # `--device auto` 由各段各自解析（各自试推理），可能不一致——不一致时逐段列出来
    for key in ("device_resolved", "device_fallback", "device_effective", "models", "det_batch", "backend"):   # det_batch：auto 也是各段按设备解析的；backend：各段接同一个共享服务，照理相同，不同时照样逐段列
        vals = [x.get(key) for x in metas if key in x]
        if vals:
            m[key] = vals[0] if all(v == vals[0] for v in vals) else {"mixed": vals}
    # TRT 按次退回 CUDA EP 的计数（`_meta.ort.<模型>.trt_oop / trt_err`）是服务端的累计数：按各段实际连的服务（`server`）分组，
    # 同一个服务取最大（共享服务的回信每段都是整份，相加会重复算），不同服务相加（`--no-ort-share`、接外部地址失败后自己起的）
    per_srv: dict = {}
    trt_ort: dict = {}
    for i, x in enumerate(metas):
        for tag, st in (x.get("ort") or {}).items():
            if not isinstance(st, dict):
                continue
            got = trt_ort.setdefault(tag, {})
            srv = per_srv.setdefault((tag, st.get("server") or f"seg{i}"), {})
            for k in ("trt_oop", "trt_err"):
                if st.get(k):
                    srv[k] = max(srv.get(k, 0), st[k])
            if st.get("trt_err_first") and "trt_err_first" not in got:
                got["trt_err_first"] = st["trt_err_first"]
    for (tag, _), srv in per_srv.items():
        for k, v in srv.items():
            trt_ort[tag][k] = trt_ort[tag].get(k, 0) + v
    if any(trt_ort.values()):
        m["ort"] = {**(m.get("ort") or {}), **{t: v for t, v in trt_ort.items() if v}}
    seams = seam_problems(metas, interval)
    bad = next((x["bad_pts"] for x in metas if x.get("bad_pts")), None)
    overlap = [s for s in seams if "重叠" in s]
    if overlap and bad is None:
        bad = "段间 PTS 不递增：" + "；".join(overlap)
    complete = (all(x.get("complete") for x in metas[:-1])
                and ocr_complete.is_complete(m["frames_advanced"], m["frames_requested"])
                and bad is None and not seams)
    firsts = [x.get("pts_first_sec") for x in metas]
    lasts = [x.get("pts_last_sec") for x in metas]
    m.update({
        "complete": complete,
        "stopped_early": any(x.get("stopped_early") for x in metas),
        "bad_pts": bad,
        "seam_problems": seams,
        "reuse_rate": round(m["reused_boxes"] / max(1, m["boxes"]), 4),
        "wall_sec": round(wall, 2),
        "sec_per_frame": round(wall / max(m["sampled_frames"], 1), 3),
        "start_sec": metas[0].get("start_sec"),
        "seek_rev": metas[0].get("seek_rev"),
        "end_sec": metas[-1].get("end_sec"),
        "timebase": "pts",
        "pts_first_sec": firsts[0],
        "pts_last_sec": lasts[-1],
        "cuts_sec": cuts_sec,
        "part_wall_sec": [x.get("wall_sec") for x in metas],
    })
    span = (lasts[-1] - firsts[0]) if firsts[0] is not None and lasts[-1] is not None else 0.0
    m["realtime_x"] = round(span / wall, 2) if wall else None
    return m
