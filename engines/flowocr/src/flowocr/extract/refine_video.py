"""回抠的**像素测量**那一半：开视频、按 run 的解码窗取裁剪、量终点 / 首字 / 全字的信号，写进 `job["measured"]`。

2026-09-22 从 `flowocr.analyze.refine_boundaries` 拆出来（正规化 project-structure：打开视频、测像素信号在阶段 1；
在已有证据上结算时间在阶段 2）。**这里不写时间**——`measure()` 的输出是五元组 `(new_e, ok_gone, sig, last_full, end_how)`，
和 `run_ocr2 --refine-fused` 在线量好、经 `edge_refine.measured_from_records` 重建出来的是**同一种证据**；
结算只有 `flowocr.analyze.refine_boundaries.apply` 一份。

两条取帧的路：`refine_sequential`（cv2 顺序解码，窗内 retrieve、窗外 grab）和 `refine_ffmpeg`
（ffmpeg 子进程只送各 run 的解码窗，av1 走 libdav1d；像素和 cv2 那条路不逐字节相同，时刻可能差 ±1 帧）。
`make_job` 定每条 run 要哪些帧（打字机窗 / 结尾窗 / 模板帧），时间 ↔ 帧号两头都走 `ptsclock.IndexClock`。
"""
from __future__ import annotations

import time

import cv2
import numpy as np

from flowocr.extract import framesource, ptsclock
from flowocr.extract import typewriter_fuse as TF


PAD = 6
"""首字 / 全字的裁剪在框外各扩这么多像素（首尾字格要带一点边，不然贴边的笔画被切掉）。"""

LEAD_US = 50_000
"""打字机窗在"首字前一个采样间隔"之外再往前多取这么久：窗口首帧当"还没字"的参照。"""

def crop_of(frame: np.ndarray, box: list[float], pad: int = 0) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    """灰度裁剪 + 框在裁剪里的位置 (x0, y0, x1, y1)。贴边时外扩被截掉，位置照实给。"""
    h, w = frame.shape[:2]
    bx0, by0 = max(0, int(box[0])), max(0, int(box[1]))
    bx1, by1 = min(w, int(box[2])), min(h, int(box[3]))
    if bx1 - bx0 < 4 or by1 - by0 < 4:
        return np.zeros((4, 4), np.uint8), (0, 0, 4, 4)
    x0, y0 = max(0, bx0 - pad), max(0, by0 - pad)
    x1, y1 = min(w, bx1 + pad), min(h, by1 + pad)
    return (cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY),
            (bx0 - x0, by0 - y0, bx1 - x0, by1 - y0))

def video_codec(path: str) -> str:
    """容器里的视频编码四字码（`av01` / `vp09` / `avc1` / `hev1`…），小写；打不开就是空串。"""
    cap = cv2.VideoCapture(path)
    v = int(cap.get(cv2.CAP_PROP_FOURCC)) if cap.isOpened() else 0
    cap.release()
    return "".join(chr((v >> (8 * k)) & 0xFF) for k in range(4)).strip().lower() if v else ""

def video_fps(path: str) -> float:
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cap.release()
    # 打不开的文件 OpenCV 给 **−1**，`or 30.0` 放它过去：整条时间轴按负帧率算，
    # 产物是垃圾而**一行报错都没有**（2026-09-17 审计时拿错路径撞上的：起点平均移动 8158 s）。
    # `--decoder obs` 不开视频，但仍要这个 fps 换算帧号，所以这一步一样不能猜
    if fps <= 0:
        raise SystemExit(f"读不出帧率（{fps}）：{path} 打不开或不是视频。"
                         f"回抠所有时刻都按它换算，猜一个会让整条时间轴错到不像话")
    return fps

SEEK_WORTH_FRAMES = 900
"""跳过这么多帧以上才值得 seek，否则 grab 过去更便宜。
按实测（seek 85 ms / grab 0.3 ms）盈亏点在约 280 帧，这里取三倍余量——
估偏了也只是多 grab 一会儿，不会出错。"""

def first_true(lo: int, hi: int, pred) -> tuple[int, bool]:
    """[lo, hi] 里第一个满足的下标 + **找没找到**。都不满足时返回 `(hi, False)`。

    第二个返回值不是可有可无的（methodology-audit-4 报告 C5）：
    原来"没找到"和"边界正好在 hi"返回同一个值，调用方分不开，
    于是**搜索失败的默认端点被当成了观测到的边界**。
    """
    for i in range(lo, hi + 1):
        if pred(i):
            return i, True
    return hi, False

def last_true(lo: int, hi: int, pred) -> tuple[int, bool]:
    """[lo, hi] 里最后一个满足的下标 + 找没找到。都不满足时返回 `(lo, False)`。"""
    for i in range(hi, lo - 1, -1):
        if pred(i):
            return i, True
    return lo, False

def make_job(run: dict, frame_us: int, us_per_frame: float, clock=None) -> dict:
    """一条 run 要解码的帧窗。span = 一个采样间隔的帧数：

    * `[lo, hi_start]`：打字机窗，从首字前一个多采样间隔到采样级全字之后一个采样间隔（最后一帧当"最终样子"）；
    * `[e, hi]`：找全字消失；
    * `mid`：消失判据的模板帧——**中间那次观测**，不是时间跨度的中点（n_obs=1 时中点已经没字了）。

    **时间 ↔ 帧号两头都走 `clock`**（`ptsclock.IndexClock`，2026-09-17）：run 的时间是真实 pts，
    而 `us_per_frame = 1e6 / src_fps` 是容器**平均**帧率——丢帧缺口的素材上两者差到几百毫秒（f4 20 分钟 −260 ms、
    f3 7.2 h 处 9.5 s），窗口就开在错的地方、写回去的时刻也带着同一份偏差。
    `clock=None` 时退回常量换算，和改之前**逐位相同**（CFR 素材上两者恒等）。"""
    clock = clock or ptsclock.IndexClock(us_per_frame)
    span = int(round(frame_us / us_per_frame))
    s_idx = int(round(clock.idx_of(run["t_start"])))
    e_idx = int(round(clock.idx_of(run["t_end"] - frame_us)))
    mid_us = run["t_start"] + max(0, run.get("n_obs", 1) - 1) / 2 * frame_us
    mid_idx = int(round(clock.idx_of(mid_us)))
    tfs = run.get("t_full_sampled")
    tfs_idx = int(round(clock.idx_of(tfs if tfs is not None else run["t_start"])))
    lo = max(0, s_idx - span - int(round(LEAD_US / us_per_frame)))
    # 窗口末帧就是"最终样子"的模板，**不许越过末次观测帧**：短条目（n_obs=2，采样级全字就是末次观测）
    # 往后多取一个采样间隔，模板会落在字已经消失 / 下一句的帧上，全字出现被判成消失那一刻
    # （水族馆 `(粉塵爆発…！)` 晚了 45 帧，2026-09-12）
    hi_start = max(s_idx, min(tfs_idx + span, e_idx))
    # 结尾窗：末次观测前半个多采样间隔（首帧当"字还完整"的模板）到 t_end 之后 0.2 s（淡出 / 交叉淡出的尾巴）
    end_lo = max(mid_idx, int(round(clock.idx_of(run["t_end"] - 1.5 * frame_us - 50_000))))
    end_hi = int(round(clock.idx_of(run["t_end"] + 200_000)))
    return {"run": run, "box": run["box"], "s": s_idx, "e": e_idx, "mid": mid_idx, "span": span,
            "end_lo": end_lo, "end_hi": end_hi,
            "lo": min(lo, mid_idx, end_lo), "tw_lo": lo, "hi_start": hi_start,
            "hi": max(e_idx + span, mid_idx, hi_start, end_hi),
            "us_per_frame": us_per_frame, "clock": clock, "frame_us": frame_us, "crops": {}, "ocr": [],
            "refined": False, "moved": False, "d_start": 0.0, "d_end": 0.0}

def wants(job: dict, i: int) -> bool:
    """这一帧在不在 job 的解码窗里（打字机窗 / 结尾窗 / 模板帧）。"""
    return ((job["tw_lo"] <= i <= job["hi_start"]) or (job["e"] <= i <= job["hi"]) or i == job["mid"]
            or (job["end_lo"] <= i <= job["end_hi"]))

def refine_ffmpeg(video: str, jobs: list[dict], thr: float, progress_every: int, pix: str = "nv12",
                  step: int = 1, scale: float = 1.0) -> None:
    """同 refine_sequential，但解码走 ffmpeg 子进程：先把所有 job 要的帧号并成区间，`select` 只送这些帧
    （av1 走 libdav1d，OpenCV 内置的 av1 解码慢 12 倍；hw-decode-results 报告）。
    帧的像素和 cv2 那条路**不逐字节相同**（nv12 -> BGR 的转换，max|Δ| 约 3），回抠出的时刻可能差 ±1 帧。"""
    if not jobs:
        return
    jobs.sort(key=lambda j: j["lo"])
    cap = cv2.VideoCapture(video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    W, H = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    need = set()
    for j in jobs:
        need.update(i for i in range(j["lo"], j["hi"] + 1) if wants(j, i))
    intervals = framesource.merge_intervals(sorted(need), gap=2)
    keep = [j["mid"] for j in jobs]                  # 模板帧无论 step 都要
    # **窗口解码要用同一份时钟**（2026-09-18 审计的 P1）：make_job 已经按真实 pts 算窗口了，
    # 但 seek 还在按 base/src_fps 走，取到的帧和标的帧号会错配。时钟就是各 job 共用的那一个。
    clock = jobs[0]["clock"]
    src = framesource.FfmpegWindows(video, intervals, src_fps=fps, width=W, height=H, pix=pix,
                                    step=step, keep=keep, scale=scale,
                                    ts_of=(lambda i: clock.t_of(i) / 1e6) if len(clock) else None)
    n_send = len(framesource.window_frames(intervals, step, keep))
    print(f"  [解码] ffmpeg 窗口模式：{len(intervals)} 个区间、{n_send} 帧（step {step}、scale {scale}；"
          f"顺序解码要过 {jobs[-1]['hi']} 帧）", flush=True)
    if scale < 1.0:                                  # 框按同一比例缩；裁剪 / 融合都在缩后的像素上做
        for j in jobs:
            j["box"] = [v * scale for v in j["run"]["box"]]
    active: list[dict] = []
    ji, done = 0, 0
    t0 = time.time()
    for idx, _t, frame in src:
        while ji < len(jobs) and jobs[ji]["lo"] <= idx:
            active.append(jobs[ji]); ji += 1
        for j in active:
            if wants(j, idx):
                j["crops"][idx] = crop_of(frame, j["box"], PAD)
        still = []
        for j in active:
            if idx >= j["hi"] and idx >= j["mid"]:
                measure(j, thr)
                done += 1
                if progress_every and done % progress_every == 0:
                    print(f"  已回抠 {done}/{len(jobs)} 条，用时 {(time.time()-t0)/60:.1f} min", flush=True)
            else:
                still.append(j)
        active = still
    for j in active:
        measure(j, thr)
    if src.stopped_early:
        print("  ⚠ ffmpeg 窗口解码提前结束：" + " | ".join(src.stderr_tail[-3:]), flush=True)

def refine_sequential(video: str, jobs: list[dict], thr: float, progress_every: int) -> None:
    """单向走一遍视频，每个 job 的解码窗一关就 `measure`（量终点、收集像素信号）；写时间在 `apply`。"""
    if not jobs:
        return
    jobs.sort(key=lambda j: j["lo"])
    cap = cv2.VideoCapture(video)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    idx, ji, done = 0, 0, 0
    active: list[dict] = []
    t0 = time.time()

    while idx < total and (ji < len(jobs) or active):
        while ji < len(jobs) and jobs[ji]["lo"] <= idx:
            active.append(jobs[ji]); ji += 1
        if not active:
            nxt = jobs[ji]["lo"] if ji < len(jobs) else total
            if nxt - idx > SEEK_WORTH_FRAMES:
                cap.set(cv2.CAP_PROP_POS_FRAMES, nxt)
                idx = nxt
                continue
        if not cap.grab():
            break
        need = [j for j in active if wants(j, idx)]
        if need:
            ok, frame = cap.retrieve()
            if ok:
                for j in need:
                    j["crops"][idx] = crop_of(frame, j["box"], PAD)
        idx += 1
        still = []
        for j in active:
            if idx > j["hi"] and idx > j["mid"]:
                measure(j, thr)
                done += 1
                if progress_every and done % progress_every == 0:
                    print(f"  已回抠 {done}/{len(jobs)} 条，用时 {(time.time()-t0)/60:.1f} min", flush=True)
            else:
                still.append(j)
        active = still
    for j in active:
        measure(j, thr)
    cap.release()

def measure(job: dict, thr: float) -> None:
    """解码窗关上时调：量终点、收集首字 / 全字的像素信号（`typewriter_fuse.signals`），**不写时间**。"""
    crops, run = job["crops"], job["run"]
    job["crops"] = {}
    if job["mid"] not in crops:
        return

    def inner(c):
        img, (x0, y0, x1, y1) = c
        return img[y0:y1, x0:x1]
    template = inner(crops[job["mid"]])
    if template.size < 64:
        return

    def looks_full(i: int) -> bool:
        c = crops.get(i)
        return c is not None and TF.ncc(inner(c), template) >= thr

    # 没被观测到的那一侧**原样退回**，不写"精确"的新时间（audit-4 C5）。
    # 终点和首字 / 全字各自有各自的证据：终点没观测到（长条目中途背景变了、模板不再像）
    # 不该连带把打字机两个时刻也丢掉——gi-s2 上两条 17–23 s 的台词就是这么整条没回抠的。
    t_gone, ok_gone = last_true(job["e"], job["hi"], looks_full)
    clock = job["clock"]                             # 帧号 -> 真实时间（见 make_job 的说明）
    old_e = run["t_end"]
    new_e = int(round(clock.t_of(t_gone + 1))) if ok_gone else old_e
    sig = None
    idxs = [i for i in range(job["tw_lo"], job["hi_start"] + 1) if i in crops]
    # step > 1 时窗内只有每 step 帧一张；两端都要在，中间至少有一半（step 1 时就是原来的"一帧不缺"）
    if idxs and idxs[0] == job["tw_lo"] and idxs[-1] == job["hi_start"] and \
            len(idxs) * job.get("step", 1) >= job["hi_start"] - job["tw_lo"] + 1:
        sig = TF.signals(np.stack([crops[i][0] for i in idxs]), [int(round(clock.t_of(i))) for i in idxs],
                         crops[idxs[-1]][1], run["text"])
    # 结尾两个关键帧：字形对比度曲线（typewriter_fuse.end_keyframes）。给得出"完全消失"就用它当 t_end，
    # 给不出（下一句紧接着在同位置出现、窗口首帧字就不完整）退回上面的整框相关系数
    # 开放边界（见 `edge_refine.measured_from_records`）：最后还像的那一帧之后，窗里再没有真解到的帧——
    # 一路像到窗尾，或者视频先结束了（片尾还在屏上的字）：没看到它消失，t_end 只是下界
    end_open = ok_gone and not any(i in crops for i in range(t_gone + 1, job["hi"] + 1))
    end_how = ("open" if end_open else "ncc") if ok_gone else None
    last_full = None
    eidx = [i for i in range(job["end_lo"], job["end_hi"] + 1) if i in crops]
    if eidx and eidx[0] == job["end_lo"] and eidx[-1] == job["end_hi"] and \
            len(eidx) * job.get("step", 1) >= job["end_hi"] - job["end_lo"] + 1:
        last_full, gone = TF.end_keyframes(np.stack([crops[i][0] for i in eidx]),
                                           [int(round(clock.t_of(i))) for i in eidx], crops[eidx[0]][1], run["text"])
        # 末次观测那一帧 OCR 还读到了字：曲线说更早就消失 = 被背景 / 模板骗了，不采纳
        # （水族馆一条被判到 t_full_sampled 之前，产物校验当场拒收，2026-09-12）
        if gone is not None and gone > clock.t_of(job["e"]):
            new_e, ok_gone, end_how = gone, True, "curve"
        if last_full is not None and last_full < clock.t_of(job["e"]) - job["frame_us"]:
            last_full = None
    if end_how == "open":
        last_full = None
    job["measured"] = (new_e, ok_gone, sig, last_full, end_how)
