"""det/rec 分离的逐帧 OCR：每帧都检测，只对"新的或变了的"框做识别。

依据（acceleration 报告实测）：det 29 ms/帧、rec 327 ms/帧（11.3 ms/框），
钱的 92% 在识别上。所以第一有效的加速是**少送框**（批处理另见 `--rec-bucket`）。

判断一个框能不能沿用上一帧的文本，不需要跑模型：
上一帧有 IoU≥0.7 的对应框，且框内像素的归一化相关系数够高，就直接沿用。
实测这一步顺带还能压掉 OCR 抖动（同一批像素被读成 `CCTVCom` / `CCTVcom` 那种）。

输出格式与 run_ocr.py 完全一致，可直接喂给 flowocr.analyze.build_tracks；
每条观测多一个 `reused` 字段，标明这条文本是沿用的还是新识别的。

**`t_us` 是解码器给的真实 PTS**（`_meta.timebase = "pts"`，2026-09-09 改）。
在此之前是 `idx / src_fps`，而 `src_fps` 是容器的**平均**帧率——录像有丢帧缺口时
整条时间轴被拉长，f3 在 7.2 h 处晚 9.5 s、f4 晚 5.0 s
（fps-and-rec-budget 报告）。旧产物的时间轴不能靠乘一个比例修回来：
缺口之后还缺累计丢帧时长。

用法：
    python -m flowocr.extract.run_ocr2 tmp/clips/zh-sub.mp4 --fps 2 --out out/zh-sub/obs.jsonl
"""
from __future__ import annotations

import functools
import hashlib
import itertools
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from flowocr.paths import child_env, resolve_model   # 子进程要找得到 flowocr（源码 checkout 里 src/ 不在默认 sys.path）

from flowocr.extract import supervisor   # 外层监督者 + 环境变量名 / 退出码（不 import 推理库，console script 也能起）
from flowocr.extract.ocr_args import ort_device   # `--device` -> ORT worker 的设备（放共享层：守卫不 import run_ocr2 也够得着）
from flowocr.extract.supervisor import EXIT_HW_MISMATCH, FAULT_ORPHAN_ENV, HW_REASON_ENV, HW_RETRY_ENV, WORKER_ENV


if __name__ == "__main__" and not os.environ.get(WORKER_ENV):
    raise SystemExit(supervisor.supervise())

import cv2
import numpy as np

from flowocr.extract import (  # noqa: E402
    edge_refine,     # 两遍合一遍：在线回抠的信号（--refine-fused）
    ffcheck,         # ffmpeg / ffprobe 够不够用；`_meta.runtime`（只追溯）
    framesource,     # 采样帧从哪来（cv2 / ffmpeg 子进程 / 预取）
    decode_proc,     # --decode-shards：只解码的子进程（模块顶层只有标准库）
    ocr_args,        # 参数定义：复用判据要用同一个解析器（见文件头）
    ocr_complete,    # 判据放在共享层，守卫才够得着（audit-4 C8 的教训）
    ocr_parallel,    # --workers 的切段 / 拼 _meta
    ptsclock,        # 时间戳的判据也是"错了不会报错"的一类
    timeline as tl,  # --timeline：各级在做什么 / 等什么的区间（NON_PRODUCT，decode-buffer）
    recpack,         # rec 分桶的键和调用（守卫拿假 rec 就能测）
    regions,         # 自选 OCR 范围：遮罩、拆框、时间切点（--regions，ocr-regions 计划）
    reuse_v2,        # `--pregate` 要用它的 `_crop`（判据只有一份）；模块本身只依赖 numpy
)
from flowocr.extract.framegrid import TimeGrid  # noqa: E402    采样 / 辅助流的时间网格
from flowocr.extract.fast_det import FastDet  # noqa: E402
from flowocr.extract.recpool import RecPool  # noqa: E402    组批第二步：rec 的异步消费者（--rec-async）


STAGE: dict = defaultdict(float)
"""主线程各段的墙钟（秒），写进 `_meta.stage_sec`：wait_frames = 等取帧那一层交帧（解码跟不上时涨的就是它）、det、
decide（复用判据 + 灰度）、rec（裁剪 + 识别）、commit（建行 / 回补 / 落盘，含等在线回抠的证据）。
2026-09-17 加：融合多付的 +30% 猜了三轮（CPU / 帧环 / GIL）都不对，该直接量。"""


def stage(k: str, t0: float) -> None:
    """`STAGE[k]` 加上 t0 到现在的墙钟；开着 `--timeline` 时同一段也记成区间（谁在什么时候等谁）。"""
    STAGE[k] += tl.span(k, t0) - t0


def pregate_of(gray, prev_gray, polys):
    """**把复用判据里最贵的那一块预先算掉**（`--pregate`）：每个框"这一帧 vs 上一个采样点、同一位置"的
    归一化相关系数。

    为什么它搬得动（2026-09-18，先量后挪）：`reuse_v2._gate` 的像素门在**静止匹配**（`prev` / `assoc`，
    实测占沿用的 97%）上比的就是"同一个框、上一个采样点" —— **不需要任何链状态**，
    所以能在 det 这一层算完，而链状态只有主线程有。
    动机是 stage 表：主线程 `decide 5.31 s`（其中 `gate` 3.27 s）+ `commit 7.83 s`，
    而 det 线程那边有约 7 s 空闲（q-hsr-s2 5 分钟、单进程 + `--det-prefetch 2` + ORT）。

    **值按位相同**：用的是同一个 `corr` 和同一个 `_crop`、同样的两帧像素，所以产物逐字节相同
    （不是"应该相同"，是同一个函数同一组输入）。`_gate` 只在 `ref_seq == seq-1 且 ref_box == 框` 时取它。
    """
    if prev_gray is None:
        return gray, {}
    crop_fn = reuse_v2.ReuseV2._crop
    out = {}
    for p_ in polys:
        b = poly_to_box(p_)
        k = tuple(b)
        if k in out:
            continue
        out[k] = corr(crop_fn(gray, b), crop_fn(prev_gray, b))
    return gray, out


def det_stream(source, fast_det, batch: int, pregate: bool = False, lookahead: int = 0):
    """取帧 + 检测（`pregate` 时顺手算灰度和静态相关系数）。`batch > 1` 时 det **先行**：
    攒 N 帧一批检测，再逐帧交出去——复用判断和 rec 依赖上一帧的结果，仍然严格逐帧；det 不依赖，所以可以先行。
    取帧那一层每帧都是新数组（cv2 `read()` / ffmpeg 的 copy / cvtColor），攒着不会被覆盖。

    `lookahead = 1`（`--det-lookahead`，2026-09-25）：下一批的**前处理 + 推理**在辅助线程上跑，这边同时做上一批的**后处理**。
    原来三段串在一个线程上（gi-s1：推理 10.9 s、后处理 3.7 s、前处理 1.5 s，det 线程忙 16.5 s / 主循环 18.1 s），
    推理那段大半在等 ORT 回信、后处理是 Python + cv2——两段能叠。批的顺序、每批的计算都不变，产物按构造相同；
    只是解码头比 det 线程再多领先一批，帧环容量按它加（`edge_refine.ring_in_flight`）。"""
    prev_gray = None
    closed_at = getattr(source, "detection_closed_at", None)

    def closed(chunk):
        # 只省整批：不压缩混合批，避免改变开放帧的批维 / 推理结果。
        # 即使全关闭也交付真实帧，时间切点、rec 结算和回抠仍按原序推进。
        return closed_at is not None and all(closed_at(t) for _, t, _ in chunk)

    def infer(chunk):
        return None if closed(chunk) else fast_det.infer_batch([f for _, _, f in chunk])

    def pack(frame, polys):
        """返回 (灰度, 静态相关系数表) 或 None（没开 pregate）。"""
        nonlocal prev_gray
        if not pregate:
            return None
        t0 = time.perf_counter()
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        out = pregate_of(gray, prev_gray, polys)
        prev_gray = gray
        stage("pregate", t0)
        return out

    if batch <= 1:
        for idx, t_sec, frame in source:
            polys = [] if closed_at is not None and closed_at(t_sec) else fast_det.predict(frame)[0]
            yield idx, t_sec, frame, polys, pack(frame, polys)
        return
    it = iter(source)
    if lookahead and hasattr(fast_det, "infer_batch"):
        import concurrent.futures as _cf
        pool = _cf.ThreadPoolExecutor(1, thread_name_prefix="det-infer")
        try:
            t0 = time.perf_counter()
            chunk = list(itertools.islice(it, batch))
            stage("wait_frames", t0)
            fut = pool.submit(infer, chunk) if chunk else None
            while chunk:
                # 先把下一批领进来、交给辅助线程去推理，再回头做这一批的后处理——两批就叠起来了
                t0 = time.perf_counter()
                nxt = list(itertools.islice(it, batch))
                stage("wait_frames", t0)
                fut_n = pool.submit(infer, nxt) if nxt else None
                t0 = time.perf_counter()
                state = fut.result()
                preds = [([], []) for _ in chunk] if state is None else fast_det.post_batch(state)
                stage("det", t0)        # 开流水时 = 等辅助线程的推理 + 后处理（和上一批重叠掉的推理不在这里），和不开时不可直接比；分项看 --timeline
                for (idx, t_sec, frame), (polys, _) in zip(chunk, preds):
                    yield idx, t_sec, frame, polys, pack(frame, polys)
                chunk, fut = nxt, fut_n
        finally:
            pool.shutdown(wait=True)
        return
    while True:
        t0 = time.perf_counter()
        chunk = list(itertools.islice(it, batch))
        stage("wait_frames", t0)
        if not chunk:
            return
        t0 = time.perf_counter()
        preds = ([([], []) for _ in chunk] if closed(chunk) else
                 fast_det.predict_batch([f for _, _, f in chunk]))
        stage("det", t0)
        for (idx, t_sec, frame), (polys, _) in zip(chunk, preds):
            yield idx, t_sec, frame, polys, pack(frame, polys)


def det_stream_threaded(source, fast_det, batch: int, depth: int, pregate: bool = False, lookahead: int = 0):
    """`det_stream` 整段搬到后台线程，主循环只从队列里取（`--det-prefetch`）。

    **为什么值得**：stage 表里"等帧 + det"占 51%~62%、而 decide + rec + commit 占 41%~49%
    （inference-runtime 计划的候选 1），两边**互不依赖**——det 不看上一帧的结果（`det_stream` 的原注释）。
    串行时循环时间是两者之和，重叠之后上限是 `max(两者)`：gi-s2 上 41.2 s -> 22.9 s。

    **产物按构造逐字节相同**：交出去的帧序、det 结果、提交顺序都没变，只是谁在什么时候算。

    ⚠ **前瞻变长了，帧环要跟着放大**：队列里攒 `depth` 批 = 解码器最多跑在主循环前面
    `batch × (1 + depth)` 个采样点（2026-09-17 那个互等死锁就是前瞻装不下，见 `edge_refine.ring_capacity`）。
    这一项算在 `edge_refine.ring_in_flight` 里。

    ⚠ `STAGE` 里的 `wait_frames` / `det` 从此是**工作线程**的墙钟，和主线程那几段**重叠、不能再相加**；
    主线程等队列的时间单独记成 `wait_det`。
    """
    import queue
    import threading

    q: queue.Queue = queue.Queue(maxsize=max(1, batch * depth))
    DONE = object()

    def worker() -> None:
        try:
            for item in det_stream(source, fast_det, batch, pregate, lookahead):
                t0 = time.perf_counter()
                q.put(item)
                tl.span("det_put", t0, a=item[0])
        except BaseException as exc:                # 线程里抛的要带回主线程，不能吞
            q.put(exc)
        q.put(DONE)

    th = threading.Thread(target=worker, name="det-prefetch", daemon=True)
    th.start()
    while True:
        t0 = time.perf_counter()
        item = q.get()
        stage("wait_det", t0)
        if item is DONE:
            return
        if isinstance(item, BaseException):
            raise item
        yield item


SHARED_ENV = "FLOWOCR_ORT_SHARED"
"""`start_ort_services` 起了共享服务、子进程都接在上面时设成 1（`rec_inflight` 认它）。"""


def start_ort_services(args, n: int, who: str = " worker") -> tuple[list, dict]:
    """`n` 个管线进程共用的 ORT 服务（`--workers` 的各段 / `run_groups` 的各组）：起好之后返回 `(服务进程表, 子进程的环境)`，
    环境里 `FLOWOCR_ORT_ADDR` 指向它们。**调用方负责在 finally 里 kill 服务进程**。"""
    # **共享推理服务**（多 worker）：起**一个**服务进程，
    # 子进程靠 `FLOWOCR_ORT_ADDR` 接上去——**模型和显存只有一份**。
    # owner 2026-09-19 把 `--workers` 默认压回 1 的理由就是显存（三个 worker 各一套，
    # ORT 15.3 GB，而当时的配额是 8 GB，现在的预算是 6.5 GB）；共享是重新放宽的前提。
    import os

    env = dict(os.environ)
    srvs: list = []
    if n > 1 and args.ort_share and not env.get("FLOWOCR_ORT_ADDR"):
        from flowocr.extract import ortclient as _oc
        # **一个服务带 det 和 rec**（2026-10-05，resource 计划；和单 worker 一样）：原来按引擎各起一个，理由是当年服务端推理一把全局锁
        # （09-23 已去掉）；两个服务各一份 CUDA 上下文、各一份 arena，4 组时 rec 8.1 GB + det 6.4 GB。
        # 几组的请求同时压在一个服务上，激活靠准入封顶（`ort_server --admit`）：det 同时在算的图不超过一个 det 批，rec 的 批 × 宽 不超过一个最大的批
        # （两个最大的批时 4 组 ORT 6.1–6.2 GB，超预算；peer review 2026-10-05）。
        # `FLOWOCR_ORT_ADDR` 仍写成 `rec=host:port,det=host:port`（同一个端口），客户端按模型挑。
        # ⚠ `--exit-on-stdin-eof` + `stdin=PIPE`：服务进程握着显存、accept 循环自己不会停，
        # 这一趟被 Ctrl-C 或中途异常打断时不盯着就会留一个吃 GPU 的孤儿（审计七）。
        # **管道的写端跟着本进程的句柄表关**，所以本进程一没它就读到 EOF——
        # 拿 pid 探活在 Windows 上不管用（还有谁握着句柄就探不出来，实测挂过）。
        # 模型在父进程这里就取好（缺了当场下载），几个子进程不会同时去下同一个文件
        addrs = []
        # 服务的设备：显式给了就照给；`auto` 这里只做便宜检查——**试推理由各段自己做**（`resolve_device`），
        # 某段退回了 CPU，它握手时报的设备和服务对不上，服务不接、它自己起一个 CPU worker（ort_server 握手核设备）
        srv_dev = ort_device(args.device if args.device != "auto" else ("gpu" if gpu_blocker() is None else "cpu"))
        want = [("rec", onnx_path(args, "rec")), ("det", onnx_path(args, "det", srv_dev))]
        from flowocr.extract import recort
        admit = f"det={det_batch(args, 'cpu' if srv_dev == 'cpu' else 'gpu')}:img,rec={recort.BATCH_AREA}:bw"
        cmd0 = [os.path.abspath(_oc.server_python()), str(_oc.SERVER), "--listen", "0",
                *[a for m, p in want for a in ("--model", f"{m}={p}")], "--exit-on-stdin-eof",
                "--gpu-mem-mb", "6144", "--max-sessions", str(args.ort_max_sessions),
                "--device", srv_dev, "--admit", admit] + ort_server_args(args, "cpu" if srv_dev == "cpu" else "gpu")
        p0 = subprocess.Popen(cmd0, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                              bufsize=1)       # stdin 这根管子**一直不关**，见上
        srvs.append(p0)
        hello = _oc.read_ready(p0.stdout)              # 缺 TRT 引擎时服务先建（一两分钟），之前的提示行原样打出来
        if "port" in hello:
            addrs = [f"{m}=127.0.0.1:{hello['port']}" for m, _ in want]
            env[SHARED_ENV] = "1"
        else:
            # 起不来（比如 `--device gpu` 而这份 onnxruntime 没有 CUDA EP，退出码 3）：不共享，
            # 各段自己起 worker——显式 gpu 的那边会各自报同一个错，auto 的各自试、各自退
            print(f"[共享服务] 共享 ORT 服务（{srv_dev}）起不来：{hello}；各段自己起 worker", flush=True)
        if addrs:
            env["FLOWOCR_ORT_ADDR"] = ",".join(addrs)
            print(f"[共享服务] 共享 ORT 服务 {env['FLOWOCR_ORT_ADDR']}"
                  f"（{n} 个{who}共用一个服务，det 和 rec 都在里面）", flush=True)
    return srvs, env


def run_parallel(args, meta: dict, src_fps: float, grid, start_idx: int,
                 end_idx: int, total_frames: int) -> int:
    """`--workers N`：切成 N 段、N 个子进程同时跑，拼成一份观测。父进程不建模型。

    子进程的参数 = 父进程的 argv 去掉 `--out/--start/--end/--workers` 再补上各段的值，
    所以**其余旋钮一个不漏**地传下去。拼好之前先写一份 `complete=false` 的占位，
    父进程中途被杀也不会留下一份旧的"已完成"产物。
    """
    import subprocess

    try:
        cuts = ocr_parallel.split_frames(start_idx, end_idx, grid, args.workers)
    except ValueError as exc:
        raise SystemExit(f"--workers {args.workers}：{exc}——窗口太短，用 --workers 1") from None
    interval = grid.interval_us(src_fps) / 1e6
    # 平均帧率偏离名义帧率的素材（有丢帧缺口，f3 / f4）上，"切点帧号 ÷ src_fps"换出来的起点会漂，
    # 段与段重叠或漏帧（flowocr.extract.ocr_parallel 文件头）。**按整个文件**预估漂移，超过半个源帧就不开
    nominal, avg = ocr_parallel.probe_rates(args.video)
    if nominal and avg:
        drift = ocr_parallel.seam_drift_sec(total_frames, avg, nominal)
        if drift > ocr_parallel.DRIFT_MAX_FRAMES / nominal:
            raise SystemExit(
                f"--workers {args.workers}：这段素材的平均帧率 {avg:.4f} 和名义帧率 {nominal:.4f} "
                f"对不上（有丢帧缺口），切点最多会漂 {drift:.2f} s——段与段会重叠或漏帧。"
                f"用 --workers 1（根治要等窗口改成按 pts 定义）")
    out_path = Path(args.out)
    out_path.write_text(json.dumps({"_meta": {**meta, "complete": False}}, ensure_ascii=False)
                        + "\n", encoding="utf-8")
    bounds = [None, *cuts, None]                      # None = 沿用父进程的 --start / --end
    base_argv = ocr_parallel.strip_opts(sys.argv[1:], {"--out", "--start", "--end", "--workers"})
    parts = [out_path.with_name(f"{out_path.stem}.part{k}{out_path.suffix}")
             for k in range(args.workers)]
    t0 = time.time()
    srvs, env = start_ort_services(args, args.workers)
    procs = []
    try:
        for k, part in enumerate(parts):
            s = args.start if bounds[k] is None else bounds[k] / src_fps
            e = args.end if bounds[k + 1] is None else bounds[k + 1] / src_fps
            cmd = [sys.executable, "-m", "flowocr.extract.run_ocr2", *base_argv,
                   "--out", str(part), "--start", repr(s), "--end", repr(e), *ocr_parallel.CHILD_ARGS]
            procs.append(subprocess.Popen(cmd, env=child_env(env)))
        print(f"[并行] {args.workers} 段，切点 {[round(c / src_fps, 3) for c in cuts]} s", flush=True)
        rcs = [p.wait() for p in procs]
    finally:
        # **服务是这一趟起的，跟着这一趟走**——`try/finally`，不是只管正常路径：
        # 异常 / Ctrl-C 时它握着显存不放（服务端那条 `--exit-on-stdin-eof` 是第二道保险）
        for p0 in srvs:
            p0.kill()
    wall = time.time() - t0
    # 退出码 3 = 那一段自己的覆盖率没过。**完成与否按整个窗口判**（merge_metas）：
    # `end=0` 时容器虚报的帧数全压在末段上，末段自己过不了、整体却是够的
    if any(rc not in (0, 3) for rc in rcs):
        print(f"**[并行] 有子进程失败：退出码 {rcs}**——各段产物留在 {parts[0].parent}，"
              f"整体产物保持 complete=false", flush=True)
        return next(rc for rc in rcs if rc not in (0, 3))
    metas = []
    for part in parts:
        with part.open(encoding="utf-8") as fh:
            metas.append(json.loads(fh.readline())["_meta"])
    merged = ocr_parallel.merge_metas(metas, meta, wall=wall,
                                      cuts_sec=[c / src_fps for c in cuts], interval=interval)
    with out_path.open("w", encoding="utf-8") as out:
        out.write(json.dumps({"_meta": merged}, ensure_ascii=False) + "\n")
        for part in parts:
            with part.open(encoding="utf-8") as fh:
                next(fh)                                # 各段自己的 _meta 不要
                for line in fh:
                    out.write(line)
    if merged["complete"]:
        for part in parts:
            part.unlink()
    else:                                               # 没完成就留着各段，排查 / 看哪段断了
        print(f"[并行] 整体未完成，各段产物留在 {parts[0].parent}", flush=True)
    print(json.dumps({k: merged[k] for k in
                      ("complete", "sampled_frames", "boxes", "rec_calls", "reuse_rate",
                       "lost_rec", "wall_sec", "sec_per_frame", "realtime_x", "part_wall_sec")},
                     ensure_ascii=False), flush=True)
    if merged["bad_pts"]:
        print(f"**时间轴不可信**：{merged['bad_pts']}", flush=True)
        return 4
    if merged["seam_problems"]:
        print(f"**切点对不上**：{'；'.join(merged['seam_problems'])}", flush=True)
    return 0 if merged["complete"] else 3


def parallel_blocker(args, src_fps: float, grid, start_idx: int, end_idx: int,
                     total_frames: int) -> str | None:
    """`--workers N` 能不能开：不能就返回原因（同 run_parallel 开头那两道门，抽出来给退回单进程用）。"""
    try:
        ocr_parallel.split_frames(start_idx, end_idx, grid, args.workers)
    except ValueError as exc:
        return f"{exc}——窗口太短"
    nominal, avg = ocr_parallel.probe_rates(args.video)
    if nominal and avg:
        drift = ocr_parallel.seam_drift_sec(total_frames, avg, nominal)
        if drift > ocr_parallel.DRIFT_MAX_FRAMES / nominal:
            return (f"平均帧率 {avg:.4f} 和名义帧率 {nominal:.4f} 对不上（有丢帧缺口），"
                    f"切点最多会漂 {drift:.2f} s")
    return None


def onnx_path(args, kind: str, device: str | None = None) -> str:
    """`--rec-onnx` / `--det-onnx` 的生效路径：给了就用给的，空 = 官方默认（`flowocr.models`，缺了当场取）；
    rec 按生效的识别语言挑（`ocr_lang`、`models.LANG_REC`）。
    `device`：模型要跑在哪（解析过的 `gpu:N` / `cpu`）。`--det-precision fp16` 只在 GPU 上换成半精度派生图；
    CPU 上、或调用方不知道设备（`None`，如判语言的抽样探测）都取 fp32。"""
    from flowocr import models
    p = args.rec_onnx if kind == "rec" else args.det_onnx
    if p:
        return resolve_model(p)
    if kind == "det" and getattr(args, "det_precision", "fp32") == "fp16" and device not in (None, "cpu"):
        return models.fp16_path("det")
    return models.path(models.LANG_REC[ocr_lang(args)] if kind == "rec" else kind)


def lang_probe(args) -> dict | None:
    """`--ocr-lang auto` 判出的结果（监督者 / run_groups 探测好、经环境变量指的文件）；不是 auto 是 None。
    auto 却没有结果：没经过监督者（worker 不该自己探，各段各判会判出不同语言）——报错。"""
    if getattr(args, "ocr_lang", "zh") != "auto":
        return None
    from flowocr.extract.supervisor import OCR_LANG_ENV
    f = os.environ.get(OCR_LANG_ENV)
    if not f or not Path(f).is_file():
        raise SystemExit("--ocr-lang auto 要经监督者（flowocr-ocr / python -m flowocr.extract.run_ocr2）或 run_groups 起：判语言只在那里做一次")
    return json.loads(Path(f).read_text(encoding="utf-8"))


def ocr_lang(args) -> str:
    """生效的识别语言：点名的就是它，auto 是探测判出的。"""
    got = lang_probe(args)
    return got["lang"] if got else getattr(args, "ocr_lang", "zh")


def rec_inflight(args, device: str) -> int:
    """异步 rec 池实际同时在途几个请求（`--rec-inflight`）：只有异步池才有"在途"可言，同步路径当 1。

    **auto（默认）= GPU 上 2、CPU 上 1**（2026-09-25，defaults §1.17 末尾）：CPU 的 det 和 rec 各自就能吃满全部物理核
    （ORT 每个 session 一池物理核数的线程），第二个在途只是第三池线程来挤——gi-s1 60 s `--device cpu` 72.2 → 64.1 s（−11%）。
    显式给数就照给的。在途几个改批怎么拼和结果什么时候回来，读数回来就结算时也经时序改到哪些帧真读（owner 定的时序抖动，NOISE_EQUIV），生效值记在 `_meta.rec_pool.inflight`。"""
    if not args.rec_async:
        return 1
    if args.rec_inflight == "auto":
        # 接在 `start_ort_services` 起的共享服务上（多组 / `--workers`）时也只要 1 个：并发由几个组 / 段提供，
        # 每个在途再多一根调用线程 × 每个形状一套句柄（resource 计划，defaults §1.42）。认的是它设的标记，
        # 不是 `FLOWOCR_ORT_ADDR`——那个用户自己也能设（里面不一定有 rec）
        import os
        return 1 if device == "cpu" or os.environ.get(SHARED_ENV) else 2
    return max(1, args.rec_inflight)


def det_batch(args, device: str) -> int:
    """det 一批几帧（`--det-batch`）。**auto（默认）= GPU 上 4、CPU 上 1**（2026-10-04，defaults §1.35）：
    CPU 上量到批 1 更快更省（原神 / 鸣潮 30 s 窗端到端 −12% / −6%、整树 CPU 秒 −12% / −8%、提交峰值约 −14%~−19%），
    obs 逐字节相同；机制没单独拆。显式给数就照给的。只改拼批（owner 定为噪音级，NOISE_EQUIV），
    生效值记在 `_meta.det_batch`。"""
    if args.det_batch == "auto":
        return 1 if device == "cpu" else 4
    return args.det_batch


def ort_server_args(args, device: str) -> list[str]:
    """追加给 `ort_server` 的参数（单 worker 的管道模式和多 worker 的共享服务**同一份**）。
    `--ort-share-mem`：session 间共用 arena 与权重（decode-buffer）；
    `device` 是解析过的设备（`gpu` / `gpu:N` / `cpu`），CPU 上不按形状分 session、不预热（defaults §1.27）。"""
    import shlex

    from flowocr.extract import recort
    warm = ",".join(f"{b}x{w}" for b, w in recort.warm_shapes(max(1, args.rec_bucket or 1)))
    cpu = device == "cpu"
    # CPU 上**不按形状分 session、也不预热**（2026-09-25，defaults §1.27）：按形状分 session 是 CUDA EP 每换形状就重规划的对策，CPU EP 没有这个坑，
    # 23 个预热 session 只是白建（每个一池线程 + 一份权重）——gi-s1 60 s `--device cpu` 47.6 / 48.0 → 43.5 / 44.0 s（两个时间点）
    return ((["--share-mem", "1"] if args.ort_share_mem else [])
            + (["--warm", f"rec={warm}"] if args.ort_warm and not cpu else [])
            + (["--per-shape", "0"] if cpu else [])
            + ([] if args.ort_spin == "on" else ["--spin", "0"])      # 默认不自旋（owner 2026-09-25，defaults §1.27）
            # 显存两件（defaults §1.41）：arena 按需扩、cuBLAS 工作区 4 MB；CPU 上没有这两样
            + (["--arena-extend", args.ort_arena_extend] if args.ort_arena_extend != "pow2" and not cpu else [])
            + (["--cublas-ws", args.ort_cublas_ws] if args.ort_cublas_ws not in ("", "off") and not cpu else [])
            + ([] if cpu else trt_plan(args)["server_args"])
            # 探针 / A/B 用的原样透传（`--threads 4 --per-shape 0` 这类服务端旋钮）：进 config（ADDED_NOOP 只赦免空），整串记 `_meta.ort_server_args`
            + shlex.split(args.ort_server_extra))


def trt_plan(args) -> dict:
    """`--backend` 在客户端这一侧能定的部分（GPU 上；CPU 不调）：`server_args` 是给 `ort_server` 的 TensorRT 参数（空 = 不用），
    `why` 是不用的原因（进 `_meta.backend`）。哪些模型走 TRT：没自带 ONNX 的那几个，引擎一律从官方 fp32 图建
    （det 的 `--model` 仍是 `--det-precision` 选的那份，TRT 退回 CUDA EP 时用）。库在不在只看三个 DLL 的大小（不算哈希）；
    建引擎、自检、ORT 版本在服务端再核一遍（服务可能跑在另一个解释器里），对不上那边退回 CUDA EP。
    结果按生效值缓存（一趟里要用好几次），`--backend trt` 的警告只打一次；不挂在 args 上（`product_config` 会把 args 的属性全记进 config）。"""
    from flowocr import models
    return _trt_plan(args.backend, args.trt_precision, "" if args.det_onnx else models.path("det"),
                     "" if args.rec_onnx else onnx_path(args, "rec"))


@functools.lru_cache(maxsize=None)
def _trt_plan(backend: str, precision: str, det_src: str, rec_src: str) -> dict:
    got = {"server_args": [], "why": ""}
    if backend == "cuda":
        got["why"] = "--backend cuda"
    else:
        from flowocr import trt as T
        src = {k: v for k, v in (("det", det_src), ("rec", rec_src)) if v}
        loud = False                                # auto 时只有"装了却用不上"才警告：版本对不上、只装着别的 TensorRT 版本（升级后）
        if not all(T.have(m) for m in T.COMMON):
            old = T.stale_install()
            loud = bool(old)
            got["why"] = (f"装的是 TensorRT {old}，这一版要 {T.TRT_VERSION}（先 flowocr-trt remove 再 install）" if old
                          else "没装 TensorRT 库（flowocr-trt install）")
        else:
            loud = bool(T.ort_compatible())
            got["why"] = T.ort_compatible() or ("" if src else "自带的 --det-onnx / --rec-onnx 走 CUDA EP")
        if not got["why"]:
            got["server_args"] = ["--trt-lib", str(T.lib_dir()), "--trt-engines", str(T.engine_dir()), "--trt-version", T.TRT_VERSION,
                                  "--trt-ort", ",".join(k for k, v in T.ORT_TRT.items() if v == T.TRT_VERSION),
                                  "--trt-precision", precision,
                                  *[a for k, v in src.items() for a in ("--trt-model", f"{k}={v}")]]
        elif backend == "trt" or loud:
            print(f"[后端] ⚠ --backend {backend}：{got['why']}，用 CUDA EP", file=sys.stderr, flush=True)
    return got


def gpu_blocker(device: str = "gpu") -> str | None:
    """`--device auto` 的**便宜那一半**（不建模型）：这份 onnxruntime 有没有 CUDA EP（0.2 s）、有没有卡、要的那张卡在不在。不能用就返回原因。
    过了只说明值得去试——架构 / 驱动 / 运行库够不够，要在 GPU 上真建 session、真推理一次才知道（`trial_infer`）。
    卡数按 `ocr_args.visible_gpu_count` 数（`CUDA_VISIBLE_DEVICES` 或 `nvidia-smi -L`）——`--device gpu:1` 在一卡机器上原来要到 ORT 服务建 session 才报。
    数不到就照旧交给建 session 时报。"""
    import onnxruntime as ort
    if "CUDAExecutionProvider" not in ort.get_available_providers():
        return f"这份 onnxruntime（{ort.__version__}）没有 CUDAExecutionProvider（有的是 {ort.get_available_providers()}）"
    n = ocr_args.visible_gpu_count()
    if n is None:
        return None
    if n < 1:
        return "没有可用的 CUDA 设备（nvidia-smi / CUDA_VISIBLE_DEVICES 一张都没列出）"
    idx = int(ort_device(device).split(":")[1])
    return None if idx < n else f"要 {idx} 号卡，但只看得到 {n} 张（0~{n - 1}）"


class RecWorkerStart:
    """rec 的 ORT worker **在后台线程里起**（起进程、载模型，约 1.3 s），和这边的便宜检查、建 det 的前后处理并行
    （2026-09-23，decode-buffer：起动那几秒原来是串着付）。`client()` 取结果（线程里的异常在这里原样抛）；
    `discard()` 不用了就关掉（设备退回 / det 建失败）——不留孤儿 worker。"""

    def __init__(self, args, device: str) -> None:
        import threading

        from flowocr.extract import ortclient
        self.device = device
        self._res: list = []

        def run() -> None:
            try:
                ms = {"rec": onnx_path(args, "rec")}
                if not os.environ.get("FLOWOCR_ORT_ADDR"):
                    # （接父进程的共享服务时不塞：det 由 det 的 client 接到同一个服务的 det= 地址；这里塞了也连得上，但没必要）
                    # det 也放进**这个**服务（同一个 CUDA 上下文、各自的流并发，服务端不加锁）：少一个进程和一层跨进程分时，
                    # 交错两轮 −3.0%、显存比单独起一个 det 服务少 1 GB 上下（decode-buffer 的 D1）
                    ms["det"] = onnx_path(args, "det", device)
                side = ["--side-listen"] if rec_inflight(args, device) > 1 or "det" in ms else []     # 第 2..N 个在途请求 / det 从旁路端口接进来
                self._res.append(ortclient.OrtClient(ms, in_mb=96 if "det" in ms else 64, out_mb=48 if "det" in ms else 32,
                                                     max_sessions=args.ort_max_sessions, device=ort_device(device),
                                                     extra_args=ort_server_args(args, device) + side))
            except BaseException as exc:         # noqa: BLE001  交回主线程
                self._res.append(exc)

        self._th = threading.Thread(target=run, daemon=True, name="ort-rec-start")
        self._th.start()

    def client(self):
        self._th.join()
        r = self._res[0]
        if isinstance(r, BaseException):
            raise r
        return r

    def discard(self) -> None:
        self._th.join()
        if self._res and not isinstance(self._res[0], BaseException):
            close_clients(self._res[0])


def build_models(args, device: str, rec_start: RecWorkerStart | None = None):
    """在**解析过的** `device`（gpu / gpu:N / cpu）上建 det / rec，含 ORT worker。
    返回 `(rec, fast_det, det_cli, ort_cli)`；中途失败先关掉已经起来的 ORT worker 再抛（退回 CPU 时不留孤儿）。
    `rec_start`：已经在后台起着的 rec worker（同一设备），建 det 的同时它在起；设备不同就不该传进来。"""
    from flowocr.extract import ortclient, recort
    ort_cli = det_cli = None
    if rec_start is None:
        rec_start = RecWorkerStart(args, device)    # 没人预先起：这里起，照样和下面建 det 并行
    try:
        # **一个引擎一个 worker**：det 跑在预取线程上、rec 跑在主线程上，共用一根管道就串行了
        # （实测 det 段 24.8 → 26.1 s），而且一根管道上两个线程同时发请求会串线——det 走同一个服务的旁路端口
        ort_cli, rec_start = rec_start.client(), None
        print(f"[ort] rec worker：{onnx_path(args, 'rec')}", flush=True)
        if "det" in ort_cli.models and ort_cli.side_addr:
            det_cli = ort_cli.side_channel()      # 和 rec 同一个服务进程（见 RecWorkerStart）
            print(f"[ort] det 和 rec 同一个服务：{onnx_path(args, 'det', device)}", flush=True)
        else:                                     # 接在共享服务上（多组 / --workers）：det 的 client 接到同一个服务的 det= 地址（pick_addr）
            det_cli = ortclient.OrtClient({"det": onnx_path(args, "det", device)}, in_mb=96, out_mb=48,
                                          device=ort_device(device), extra_args=ort_server_args(args, device))
            print(f"[ort] det worker：{onnx_path(args, 'det', device)}", flush=True)
        rec = recort.OrtRec(model=onnx_path(args, "rec"),
                            max_batch=max(1, args.rec_bucket or 1), client=ort_cli, pre=args.rec_pre)
        if args.ort_batch_ladder:
            rec.ladder = tuple(sorted(int(x) for x in args.ort_batch_ladder.split(",")))
        # det 的前后处理配置和模型名读 ONNX 旁边的 inference.yml（FastDet.from_onnx），后处理是 detpost 照抄的那份
        try:
            fast_det = FastDet.from_onnx(onnx_path(args, "det", device), ortclient.make_det_runner(det_cli),
                                         default_yml_ok=args.det_onnx in ("", ocr_args.OLD_DET_ONNX))
        except ValueError as exc:
            raise SystemExit(f"--det-onnx：{exc}") from None
    except BaseException:
        close_clients(det_cli, ort_cli)
        if rec_start is not None:                   # 起 rec worker 时就失败了：后台那个线程也收掉
            rec_start.discard()
        raise
    return rec, fast_det, det_cli, ort_cli


def close_clients(*clis) -> None:
    for c in clis:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass


def trial_infer(rec, fast_det) -> None:
    """GPU 试推理（project-structure 计划："架构、驱动、运行库和实际 det / rec 初始化／试推理检查"）：det 过一帧、rec 过一个裁剪，
    **真走一遍前向**——只建得起模型不算数：缺这张卡的 SASS、cuDNN 找不到引擎、库版本不对，都在第一次前向才炸。
    走的是管线自己的调用路径，所以试过的就是之后要跑的。"""
    frame = np.zeros((320, 320, 3), np.uint8)
    fast_det.predict(frame)
    rec.predict([np.full((48, 160, 3), 255, np.uint8)], batch_size=1)


def resolve_device(args):
    """`--device` -> `(实际设备, 退回原因或 None, build_models 的四元组)`。

    * `gpu` / `gpu:N` / `cpu`：原样建。显式 `gpu` 不可用就照常报错——**不回退**，要的就是 GPU；
    * `auto`（默认）：便宜检查（`gpu_blocker`）-> GPU 上建模型 + 试推理（`trial_infer`）。**GPU 不可用**（便宜检查不过，
      或试推理的异常是 `ocr_args.GPU_UNAVAILABLE_MARKS` 里那几类：架构不支持、驱动太旧、没有设备、库加载不了）就整体退回 CPU
      （project-structure 计划："任一加速条件不满足就走 CPU"；不做 det 上 GPU、rec 退 CPU 的拼法——两者是同一个 ORT 服务、同一套 CUDA 库，
      一边不行另一边多半也不行，拼出来的组合也没人验过），原因大声打出来、写进 `_meta.device_fallback`；
      **GPU 可用但试推理出了别的错**就报错退出，不换 CPU（release-plan F5：没有可用 GPU 与 GPU 上出了错要分开）。

    只在开跑前试一次；跑到一半 GPU 出错**不**悄悄换 CPU 接着跑（那是失败，照常报）。
    参数写错（`SystemExit`）不算"GPU 不可用"，照常往外抛。

    ⚠ 显式 `gpu` 也先过 `gpu_blocker`：没卡 / 卡号越界在这里就报清楚，别等到建 session 时报一个和设备不相干的错。
    ⚠ ONNX 模型**先取好**（缺了就下载）再试 GPU：不然断网时"模型取不到"会被当成"GPU 试推理失败"、白退一次 CPU 再报同一个错。"""
    for kind in ("rec", "det"):
        onnx_path(args, kind)
    # rec 的 ORT worker 先在后台起（约 1.3 s），下面做便宜检查、建 det 的同时它在起
    first = args.device if args.device != "auto" else "gpu"
    rec_start = RecWorkerStart(args, first)
    if args.device != "auto":
        if args.device != "cpu" and (why := gpu_blocker(args.device)):
            rec_start.discard()
            raise SystemExit(f"--device {args.device}：{why}。要自动退回 CPU 用默认的 --device auto，要 CPU 就 --device cpu")
        return args.device, None, build_models(args, args.device, rec_start)
    why = gpu_blocker()
    if why is not None:
        rec_start.discard()                     # 便宜检查就不过：GPU 上那个不用了
    if why is None:
        models = None
        try:
            models = build_models(args, "gpu", rec_start)
            trial_infer(models[0], models[1])
        except Exception as exc:
            text = str(exc).strip()
            why = f"GPU 试推理失败：{type(exc).__name__}: {text.splitlines()[-1] if text else ''}"
            if models is not None:
                close_clients(models[2], models[3])
            models = None
            if not ocr_args.gpu_unavailable(f"{type(exc).__name__}: {text}"):
                raise SystemExit(f"[设备] GPU 可用，但试推理出错——不退回 CPU（那样会掩盖问题）：{why}\n"
                                 f"  确认要用 CPU 跑就给 --device cpu") from None
        if models is not None:
            return "gpu", None, models
    print(f"[设备] ⚠ --device auto 退回 CPU：{why}", flush=True)
    return "cpu", why, build_models(args, "cpu")


def poly_to_box(poly) -> list[int]:
    xs = [float(p[0]) for p in poly]
    ys = [float(p[1]) for p in poly]
    return [round(min(xs)), round(min(ys)), round(max(xs)), round(max(ys))]


def iou(a: list[int], b: list[int]) -> float:
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    return inter / ((a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - inter)


def estimate_shift(prev_boxes: list[list[int]], cur_boxes: list[list[int]]) -> tuple[int, int]:
    """从框的几何估计整帧的滚动位移，**不需要文本**。

    滚动的评论墙/片尾里，框的位置每帧都变，IoU 直接失配，复用率掉到零，
    可逐框相关系数的开销照付——反而比不复用还慢（实测 ja-title 0.342 vs 0.324）。
    做法：尺寸相近的框两两配对，位移按 12 px 网格投票，取非零众数。
    """
    from collections import Counter
    votes: Counter = Counter()
    for a in prev_boxes:
        aw, ah = a[2] - a[0], a[3] - a[1]
        for b in cur_boxes:
            bw, bh = b[2] - b[0], b[3] - b[1]
            if abs(aw - bw) > max(6, 0.12 * aw) or abs(ah - bh) > max(4, 0.2 * ah):
                continue
            dx = (b[0] + b[2] - a[0] - a[2]) // 2
            dy = (b[1] + b[3] - a[1] - a[3]) // 2
            if abs(dx) < 4 and abs(dy) < 4:
                continue
            votes[(round(dx / 12), round(dy / 12))] += 1
    if not votes:
        return (0, 0)
    (gx, gy), n = votes.most_common(1)[0]
    return (gx * 12, gy * 12) if n >= 3 else (0, 0)


def gray_crop(frame: np.ndarray, box: list[int]) -> np.ndarray | None:
    h, w = frame.shape[:2]
    x0, y0 = max(0, box[0]), max(0, box[1])
    x1, y1 = min(w, box[2]), min(h, box[3])
    if x1 - x0 < 6 or y1 - y0 < 6:
        return None
    return cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY).astype(np.float32)


def corr(a: np.ndarray | None, b: np.ndarray | None) -> float:
    if a is None or b is None:
        return -1.0
    if a.shape != b.shape:
        b = cv2.resize(b, (a.shape[1], a.shape[0]))
    a = a - a.mean(); b = b - b.mean()
    d = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float((a * b).sum() / d) if d > 1e-6 else 0.0


@dataclass
class VideoProbe:
    """`probe_video` 的结果：帧身份的帧率、画面尺寸、采样网格、窗口的起止帧号。"""
    src_fps: float
    avg_fps: float
    W: int
    H: int
    total_frames: int
    grid: TimeGrid
    start_idx: int
    end_idx: int


def probe_video(args) -> VideoProbe:
    """探视频的元数据、定采样网格和窗口（`main` 跑前准备的第一步）。探完就放掉 `cv2.VideoCapture`，真正的解码交给 framesource。"""
    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        raise SystemExit(f"无法打开 {args.video}")
    try:
        avg_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        # **帧身份的帧率**：ffmpeg 路径用容器的名义帧率（framesource.id_rate，复审第三轮 P1）——平均帧率在有缺口的文件上
        # 偏低，`round(t × 平均帧率)` 会让相邻两帧撞号。cv2 路径维持平均帧率：它的 seek（`POS_FRAMES`）语义就是平均帧率
        src_fps = avg_fps
        if args.decoder == "ffmpeg":
            src_fps = framesource.id_rate(ocr_parallel.probe_rates(args.video)[0], avg_fps)
        W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    finally:
        cap.release()
    # **采样网格**（`framegrid.TimeGrid`，2026-09-24）：按时间取 `--fps` 帧 / 秒，锚在帧号 0 上；整除时就是原来的每 stride 帧一帧。
    # 按帧号 `n` 数的选法（cv2 / `--frame-select index`）编不出不等距的网格，退回最近的整数步长（加时间网格之前的行为）
    try:
        grid = ocr_args.sample_grid(src_fps, args.fps, args.frame_select)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    if args.frame_select != "pts" and TimeGrid.for_rate(src_fps, args.fps) != grid:
        print(f"[采样] --frame-select index 只能等距：{grid.describe(src_fps)}（{grid.fps(src_fps):.3f} fps，要的是 {args.fps:g}）",
              flush=True)
    # 窗口起点对齐到采样网格，采样点才和整片跑时落在同一批帧上
    start_idx = grid.nearest(args.start * src_fps)
    # `--end 0` 的窗口尾按**时长 × 帧率**算，不按帧的个数：名义帧率下有缺口的文件，最后一帧的帧号比帧数大
    # （f3 大 728），按个数截会少跑最后约 12 秒。没缺口的文件两者相同
    end_idx = (int(round(args.end * src_fps)) if args.end > 0
               else int(-(-total_frames * src_fps // avg_fps)) if src_fps != avg_fps else total_frames)
    return VideoProbe(src_fps, avg_fps, W, H, total_frames, grid, start_idx, end_idx)


def check_args(args) -> None:
    """只看参数组合就能判的冲突，**在碰视频、建模型之前**一次报完（原来散在 `main` 里，有几条要等模型建完才报）。"""
    if (args.det_batch != "auto" and args.det_batch < 1) or args.workers < 1:
        raise SystemExit("--det-batch / --workers 至少是 1")
    if args.refine_fused and (not args.reuse_v2 or args.decoder != "ffmpeg"):
        raise SystemExit("--refine-fused 要 --reuse-v2（链事件从它来）和 --decoder ffmpeg（辅助流和采样流数同一批帧）")
    if args.hwaccel and args.decoder != "ffmpeg":
        # `--decoder cv2` 是显式回退，这里不自作主张退硬解——两个都显式给了就是自相矛盾，该报错
        raise SystemExit("--hwaccel 要 --decoder ffmpeg（要软解就 --hwaccel=）")
    # ⚠ 2026-09-20 之前这里还拦着 `--hwaccel` + `--refine-fused --refine-aux shared`，
    # 于是开硬解就自动退回两遍回抠（35–105 s），把硬解省下的全吃回去还倒欠。
    # 现在辅路在显存里 `scale_cuda` 缩完再 `hwdownload`，同一个 ffmpeg 分两路仍然成立。
    if args.trace and args.workers > 1:
        # 子进程的 argv 只换 `--out/--start/--end/--workers`，`--trace` 会**原样传给每个 worker**，
        # 三份轨迹写进同一个文件、最后一个赢——而文件看着一切正常（2026-09-19 自查）。
        raise SystemExit("--trace 要 --workers 1（否则几个 worker 写同一个轨迹文件，互相覆盖）")
    if args.rec_window and not args.reuse_v2:
        # 窗口把判决拆成 stage1/stage2 两半，这两半只有 reuse_v2 那条路上有
        raise SystemExit("--rec-window 要 --reuse-v2（默认开）")
    if args.rec_async and not (args.rec_window and args.rec_bucket):
        # 异步消费者是**窗口的后台版**：没有窗口就没有"未结算的帧"可言，没有分桶就没有"挑哪一批"
        raise SystemExit("--rec-async 要 --rec-window N（N ≥ 1）和 --rec-bucket（默认 16）")
    if args.exclude_regions and not args.predet:
        raise SystemExit("--exclude-regions 需要同时给 --predet")


def resolve_aux_grid(args, src_fps: float):
    """辅助流取哪些帧（`--aux-max-fps` + 时间网格，flowocr.extract.framegrid）；没开两遍合一遍是 None。
    120 fps 的片子默认也只收 60 fps、不要求整除、采样帧嵌套在里面。`config` 记的是请求值，
    生效网格记 `_meta.aux_grid`，复用判据比生效网格（ocr_complete.obs_reusable）。"""
    if not args.refine_fused:
        return None
    try:
        aux_grid = ocr_args.aux_grid(src_fps, args.fps, args.aux_max_fps, args.frame_select)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    if not aux_grid.uniform and (args.frame_select != "pts" or args.refine_aux in ("nvdec", "cuvid")):
        raise SystemExit(f"辅助流{aux_grid.describe(src_fps)}只配 --frame-select pts + --refine-aux shared / noop："
                         f"按帧号 `n` 数的选法和单独一路的硬解辅助流都编不出绝对帧号")
    return aux_grid


def base_meta(args, config: dict, vp: VideoProbe, aux_grid) -> dict:
    """obs 头一行 `_meta` 的起始内容（跑的过程中和跑完还会往里补：设备、范围、pts 区间……）。"""
    grid = vp.grid
    # `engine` 是没人读的历史身份串（det / rec 分离的那版管线最早跑在 PaddleOCR 上），不改
    # 路径记 `portable()`（同 `config["video"]`）；不记命令行 `argv`（owner 2026-10-06）：`config` 已是生效值，
    # 字面 argv 里的视频、`--out`、`--regions` 文件都是本机路径，逐个改要维护一张"哪些参数是路径"的表
    return {"video": config["video"], "engine": "paddleocr-split",
            # 产物要能追溯到"是拿哪组参数跑的"——不然换了 A/B 臂的旋钮、
            # 文件名没变，驱动就会静默复用上一条臂的产物（audit-4 C7 的形状）。**复用判据比的是 `config`**
            # （解析后的生效值，含默认值；flowocr.extract.ocr_args 文件头）
            "config": config,
            # 代码指纹：`config` 回答"哪组参数"，回答不了"哪一版代码"——而 obs 现在装的不只是文本和框，
            # 还有 `--refine-fused` 在线量出来的时刻（tools/ocr_args.CODE_FP_FILES 那段注释）。**不进 config**
            "code_fp": ocr_args.code_fp(),
            "runtime": ffcheck.runtime(),      # 包版本 / Python / ffmpeg：只追溯、不进复用判据
            "det_batch": args.det_batch,
            "rec_bucket": args.rec_bucket,
            "workers": args.workers,
            "reuse": not args.no_reuse, "reuse_corr": args.reuse_corr,
            "refresh_every": args.refresh_every,
            "width": vp.W, "height": vp.H, "device": args.device,
            "decoder": args.decoder, "pipe_pix": args.pipe_pix,
            "prefetch": args.prefetch,
            "src_fps": vp.src_fps, "avg_fps": vp.avg_fps, "sample_fps": grid.fps(vp.src_fps),
            # 等距时照旧记 `stride`（探针按它数）；不等距的只有 `sample_grid`
            "sample_grid": grid.as_list(), **({"stride": grid.step} if grid.uniform else {}),
            **({"aux_grid": aux_grid.as_list()} if aux_grid is not None else {}),
            "quality": ocr_args.quality_of(args),
            **({"ocr_lang_probe": lang_probe(args)} if getattr(args, "ocr_lang", "zh") == "auto" else {}),
            # 头一行只是占位，跑完会连同 pts 区间一起重写（见文末的 `clock.meta()`）。
            # **`t_us` 是解码器给的真实 PTS，不是 `idx / src_fps`**——为什么、
            # 以及三道门是什么，全在 `flowocr.extract.ptsclock` 的文件头。
            "timebase": "pts"}


def setup_regions(args, meta: dict, W: int, H: int):
    """**自选 OCR 范围**（ocr-regions 计划）：一次跑一个组。规格原样记进 meta（build_tracks 从这里重建时间关闭段的切点）。
    返回要涂遮罩的组；没给 --regions 或整段全屏时是 None。"""
    if not args.regions:
        return None
    spec = regions.group_spec(args.regions, args.region_group)
    g = regions.from_spec(spec, W, H)
    meta["regions"] = {**spec, "boundaries": g.boundaries()}
    grp = None if g.unrestricted else g
    print(f"[范围] 组 {spec['name']}：{len(spec['rects'])} 个矩形"
          + (f"、切点 {meta['regions']['boundaries']} s" if meta["regions"]["boundaries"] else "")
          + ("（整段全屏，不涂）" if grp is None else ""), flush=True)
    return grp


def apply_region_crop(args, grp, fast_det, meta: dict, W: int, H: int):
    """`--region-crop`：det 只看组范围的静态外接矩形（ocr-regions 计划的对照臂；改 det 的输入尺寸，框会变——进复用判据）。返回（可能包了一层的）FastDet。
    2026-09-24 起给了 --regions 就默认开，所以裁不了时**让路、说明原因**（同 resolve_split 的规矩），不报错；原因记 `_meta.region_crop_off`。"""
    if not (args.region_crop and grp is not None):
        return fast_det
    bb = grp.union_bbox()
    off = "组范围的外接矩形就是全屏（或某一时段全屏），没得裁" if bb is None or bb == (0, 0, W, H) else ""
    if off:
        meta["region_crop_off"] = off
        print(f"[范围] det 不裁剪：{off}", flush=True)
        return fast_det
    meta["region_crop"] = list(bb)
    print(f"[范围] det 只看外接矩形 {bb}（--region-crop）", flush=True)
    return regions.CroppedDet(fast_det, bb)


def record_devices(args, meta: dict, device: str, fallback, fast_det, det_cli, ort_cli) -> None:
    """**实际用上的设备和回退原因**（project-structure 计划："实际生效的设备和回退原因可见，不用 auto 代替结果"），以及用的是哪份模型。
    `config.device` 记的是**请求的**值（`auto` 也照记，它在 NOISE_EQUIV 里、不触发重建），这几个键记结果：
    worker 实际用上的 provider；det 的模型名（读自 ONNX 旁边的 inference.yml，只追溯）。"""
    meta["device_resolved"] = device
    meta["device_fallback"] = fallback
    meta["ort_server_args"] = ort_server_args(args, device)   # 服务实际收到的旋钮（CPU 规则、--ort-server-extra 的生效结果）
    meta["det_model"] = fast_det.spec.model_name
    meta["device_effective"] = {"det": f"ort:{det_cli.provider_for('det')}", "rec": f"ort:{ort_cli.provider_for('rec')}"}
    record_backend(args, meta, device, det_cli, ort_cli)
    print(f"[设备] --device {args.device} -> {device}：det {meta['device_effective']['det']}、"
          f"rec {meta['device_effective']['rec']}", flush=True)
    if device == "cpu":
        print(f"[设备] CPU 的调度规则（defaults §1.17 / §1.27 / §1.35）：det 批 {args.det_batch}、rec 在途 {rec_inflight(args, device)}、"
              "不按形状分 session、不预热", flush=True)
    # **用的是哪份模型文件**（内容，不是路径）：config 只记 `--rec-onnx` / `--det-onnx` 的字符串（而且它们在 NOISE_EQUIV 里），
    # 同名文件换了内容复用判据看不出来——这里记 sha256 追溯
    from flowocr import models as _models
    trt_src = {k: v["onnx"] for k, v in ((meta.get("backend") or {}).get("models") or {}).items() if v.get("backend") == "trt"}
    meta["models"] = {k: _models.meta(trt_src.get(k) or onnx_path(args, k, device)) for k in ("rec", "det")}


def record_backend(args, meta: dict, device: str, det_cli, ort_cli) -> None:
    """`_meta.backend`：请求的 `--backend` / `--trt-precision`、每个模型实际走 trt / cuda / cpu 和没走 TRT 的原因、TRT 版本、引擎缓存名和指纹
    （服务端 `ort_server.start_trt` 的说明，握手带回来）。跑完之后退回 CUDA EP 的次数在 `_meta.ort.<模型>.trt_oop / trt_err`。"""
    got = {"requested": args.backend, "trt_precision": args.trt_precision, "models": {}}
    plan = {"server_args": []}
    if device == "cpu":
        got["why"] = "--device cpu"
    else:
        plan = trt_plan(args)
        if plan["why"]:
            got["why"] = plan["why"]
    for k, cli in (("det", det_cli), ("rec", ort_cli)):
        info = getattr(cli, "trt", None) or {}
        m = (info.get("models") or {}).get(k)
        asked = f"{k}=" in " ".join(plan["server_args"])   # 这个模型要了 TRT（--trt-model k=...）
        if m is None and plan["server_args"] and not asked:     # 要了 TRT、这个模型没在里面：自带的 ONNX
            m = {"backend": "cuda", "fallback": "custom: 自带的 ONNX 走 CUDA EP"}
        elif m is None and asked and cli is not None:   # 要了 TRT，服务却没回说明：接的外部服务没带 TRT 参数，或服务端 GPU 不可用
            m = {"backend": "cuda", "fallback": "server: 推理服务没回 TensorRT 的说明"}
        got["models"][k] = m or {"backend": "cpu" if device == "cpu" else "cuda"}
        if info.get("version"):
            got["trt_version"] = info["version"]
    meta["backend"] = got
    print("[后端] " + "、".join(f"{k} {v['backend']}" + (f"（{v['fallback'][:120]}）" if v.get("fallback") else "")
                               for k, v in got["models"].items()) + (f"；{got['why']}" if got.get("why") else ""), flush=True)


def make_rec_pool(args, device: str, rec, ort_cli):
    """组批第二步：rec 搬到后台线程（recpool.py 文件头）。**推理引擎从此只被那一个线程碰**——
    回补的单框读也走它，不然主线程和它会同时调同一个 predictor。返回 `(旁路客户端, 池子或 None)`：
    `--rec-inflight N` 时第 2..N 个后端各接一条到**同一个** ORT 服务的旁路连接（decode-buffer），收尾要关。"""
    side_clis = [ort_cli.side_channel() for _ in range(rec_inflight(args, device) - 1)]
    side_recs = []
    if side_clis:
        from flowocr.extract import recort
        side_recs = [recort.OrtRec(model=rec.model, max_batch=rec.max_batch, grid=rec.grid, client=c, pre=args.rec_pre)
                     for c in side_clis]
        for r in side_recs:
            r.ladder = rec.ladder
    pool = (RecPool([rec] + side_recs, args.rec_bucket, args.rec_knee)
            if args.rec_async else None)
    return side_clis, pool


def make_reuse(args, rec, pool, src_fps: float):
    """复用判据第二版（`reuse_v2.ReuseV2`），回补的读接到池子上（有池子时）或直接调 rec。"""
    def rec_one(crop):
        # 回补用掉的 rec **不在这里记账**：commit_stage2 把这一批的次数当返回值交出去
        # （`settle_one` / `settle_window` 并进 n_rec），异步那条另由 `pool.stats().calls` 兜住。
        if pool is not None:
            # 回补**卡着落盘水位**（主线程正等它），所以插队；含它的那一组会先走
            rid = pool.new_ids(1)[0]
            pool.submit([(rid, crop)])
            got = pool.collect([rid], prio=True)[rid]
            return got if got is not None else ("", 0.0)
        r = rec.predict([crop], batch_size=1)[0]
        return (r["rec_text"], float(r["rec_score"])) if r is not None else ("", 0.0)

    def rec_many(crops):
        """回补的一层：一次全交给池子（它会按形状宽挑最大的一组发，插队）。"""
        ids = pool.new_ids(len(crops))
        pool.submit(list(zip(ids, [c.copy() for c in crops])))
        got = pool.collect(ids, prio=True)
        return [got[r] if got[r] is not None else ("", 0.0) for r in ids]

    v2 = reuse_v2.ReuseV2(args, corr, estimate_shift, rec_one)
    v2.src_fps = src_fps        # 它自己也写 obs 行，`frame` 认 pts 要用（framesource.pts_frame）
    if pool is not None:
        v2.rec_many = rec_many
        if args.recover_async:              # 回补异步（rec-reads 计划的 B）：二分交给池子推进，主线程不等
            v2.submit_task, v2.wait_tasks = pool.submit_task, pool.wait_tasks
    if args.trace:
        v2.trace = []          # 因果轨迹（`--trace`），跑完落盘；见 reuse_v2.trace 那段
    return v2


def load_dense(args) -> list[list[float]]:
    """`--exclude-regions`：predet 里选中的那几个区域的全包围盒（落在里面的框不读）。"""
    dense: list[list[float]] = []
    if not args.exclude_regions:
        return dense
    pd = json.loads(Path(args.predet).read_text(encoding="utf-8"))
    tot = sum(r["boxes"] for r in pd["regions"])
    for i in [int(x) for x in args.exclude_regions.split(",") if x.strip()]:
        r = pd["regions"][i]
        dense.append(r["rect_excl"])
        print(f"[predet] 排除区域 {i}：占预扫框 {r['boxes']/max(1,tot):.1%}，"
              f"排除框面积 {r['area_frac_excl']:.1%}，常驻度 {r['persistence']:.2f}", flush=True)
    return dense


def setup_edge(args, v2, meta: dict, grp, vp: VideoProbe, aux_grid):
    """两遍合一遍：辅助流（缩、灰度、全帧率）进帧环，reuse_v2 的链事件驱动在线回抠（flowocr.extract.edge_refine）。
    `--edge-proc`（默认）时回抠在自己的进程里、返回它的代理；否则进程内的 `EdgeRefiner`。挂到 `v2.edge` 上。"""
    src_fps = vp.src_fps
    edge_kw = dict(src_fps=src_fps, start_idx=vp.start_idx, width=vp.W, height=vp.H,
                   scale=args.refine_scale, grid=aux_grid.as_list(), thr=args.refine_thr,
                   noop=args.refine_aux == "noop",
                   ring_cap=edge_refine.ring_capacity(
                       vp.grid.max_gap, int(round(edge_refine.LEAD_US * src_fps / 1e6)),
                       int(round(edge_refine.END_TAIL_US * src_fps / 1e6)),
                       args.refine_ring, edge_refine.ring_in_flight(args), aux=aux_grid))
    if args.edge_proc:
        # 回抠放进自己的进程（edge_proc）：辅助流直接连过去，这边只发链事件、收证据。范围遮罩那边自己按规格建
        from flowocr.extract import edge_proc
        edge = edge_proc.EdgeProxy(edge_kwargs=edge_kw, start_idx=vp.start_idx, end_idx=vp.end_idx, src_fps=src_fps,
                                   width=vp.W, height=vp.H,
                                   regions_spec=(meta["regions"] if grp is not None else None))
    else:
        edge = edge_refine.EdgeRefiner(**edge_kw)
        if grp is not None:                 # 辅助流也只看得见范围里的像素（在线回抠的信号不许读进被遮掉的东西）
            edge.ring.premask = regions.aux_masker(grp, edge.Ws, edge.Hs, src_fps)
    v2.edge = edge
    # 延迟落盘要等"那一批开的段都停变了"（edge_refine.settled_by）；缓冲的采样点数不够长的话每次落盘都得等工作线程
    # 追平主线程，两个线程就串行了（--refresh-every 小的时候 horizon 只有二十几）
    v2.horizon = max(v2.horizon, edge_refine.MAX_OPEN_SAMPLES + 10)
    return edge


def settle_hwaccel(args, hw_probe, hw_pool, meta: dict) -> None:
    """**硬解开不开得了，先试解几格**（同 `--workers` 的老规矩：开不了就退回去、打印原因、meta 记生效值）。
    NVDEC 在有前摇的 av1 上少交付 1~3 帧；`gi-s2` 连 pts 0.000 都不交付，`--frame-select pts` 也选不出第 0 格。
    进了默认就不能让这类素材整段跑不了。判据是 FfmpegSource 自己那道硬解格子核对，不另写一份。
    试解在起动时就在后台跑了（`hw_probe`，和建模型并行），这里取结论；开不了就把 `args.hwaccel` 置空。"""
    if args.hwaccel:
        why = hw_probe.result()
        hw_pool.shutdown()
        if why:
            print(f"[硬解] --hwaccel {args.hwaccel} 在这段素材上开不了，退回软解：{why}", flush=True)
            args.hwaccel = ""
    meta["hwaccel_effective"] = args.hwaccel or ""
    if os.environ.get(HW_RETRY_ENV):
        meta["hwaccel_fallback"] = os.environ[HW_RETRY_ENV]     # 硬解跑到一半交付不对、整段改软解重跑的原因


def open_source(args, dec, edge, meta: dict, vp: VideoProbe, aux_grid):
    """取帧层：`--decode-shards` 的只解码子进程（硬解开得了才分片），或 framesource 的一路解码；辅助流跟着接到回抠上。
    返回 `(source, dec)`——不分片时 `dec` 已关、是 None。"""
    source_kw = dict(backend=args.decoder, grid=vp.grid, start_idx=vp.start_idx,
                     end_idx=vp.end_idx, src_fps=vp.src_fps, width=vp.W, height=vp.H,
                     prefetch=args.prefetch, pix=args.pipe_pix, hwaccel=args.hwaccel or None,
                     select_by=args.frame_select)
    lanes = None
    if dec is not None:
        # 硬解开得了才分片（试解不过 / 没 GPU 就走一路软解：owner 要每个操作都有能跑的 CPU 版，不求调度一样）
        lanes = ["hw"] * args.decode_shards if args.hwaccel else []
        if len(lanes) < 2:
            print("[解码分片] 不分片：硬解开不了，走一路软解", flush=True)
            dec.close()
            dec, lanes = None, None
            # `_meta.proc_split.decode` 记**生效**的路数（2026-09-24 审计：原来这里关了、meta 仍记请求的 3）
            meta["proc_split"]["decode"] = 0
            meta["proc_split"]["decode_off"] = "硬解开不了"
    if dec is not None:
        # 解码分片在只解码的子进程里（decode_proc + decode_shards）：采样帧经共享内存槽过来，det 在这边；
        # 辅助流按块序由那边直接转给回抠进程（它看到的和一路 ffmpeg 直连一样）
        dec.start({
            "video": args.video, "source_kw": source_kw, "W": vp.W, "H": vp.H,
            "slots": decode_proc.slots_for(args.det_batch, args.det_prefetch), "lanes": lanes, "block_sec": args.decode_block,
            "aux": ({"url": edge.url, "pts_addr": edge.pts_addr, "grid": aux_grid.as_list(), "scale": args.refine_scale}
                    if edge is not None and args.refine_aux in ("shared", "noop") else None)})
        meta["decode_shards"] = {"lanes": lanes, "block_sec": args.decode_block, "wait_start_sec": round(dec.wait_start_sec, 2)}
        return dec, dec
    source = framesource.make_source(
        args.video, **source_kw,
        aux=(({"remote": edge} if args.edge_proc else {"sink": edge.ring})
             | {"grid": aux_grid.as_list(), "scale": args.refine_scale}
             if edge is not None and args.refine_aux in ("shared", "noop") else None))
    return source, dec


@dataclass
class Counts:
    """主循环的计数（收尾时写进 `_meta`）。"""
    frames: int = 0         # 过了 stage1（逐帧那条路：写完）的采样帧
    boxes: int = 0
    rec: int = 0            # **送进 rec 的裁剪数**
    rec_calls: int = 0      # 模型真正被调用的次数：分桶 / 批处理之后和裁剪数脱钩，另记
    reused: int = 0         # 真走了复用分支的框
    dropped: int = 0        # 噪声区（`--dense`）里连 rec 都不送的框
    tiny: int = 0           # 太小送不进 rec 的框：既不是复用也不是识别
    lost: int = 0           # 后端少返回的框（报警，实测恒 0）
    settled: int = 0        # 窗口路径结算过的帧（收尾自检：必须等于 frames）


def crop_hashes(c) -> dict:
    """`--trace` 用的裁剪指纹：`h` 逐字节；`hq` 缩到 rec 输入高 48、每通道只留高 4 位——放过编码噪声的近似相同。"""
    h = hashlib.blake2b(c.tobytes() + repr(c.shape).encode(), digest_size=8).hexdigest()
    q = cv2.resize(c, (max(1, round(c.shape[1] * 48 / c.shape[0])), 48), interpolation=cv2.INTER_AREA) >> 4
    return {"h": h, "hq": hashlib.blake2b(q.tobytes() + repr(q.shape).encode(), digest_size=8).hexdigest()}


def make_rec_crop(args, W: int, H: int, frame, boxes, gray):
    """把"送进 rec 的裁剪"那套规则（外扩、按邻框截断、std 门）**绑到某一帧**上。

    2026-09-18 从循环体里提成工厂（当时是投机要给前方几帧造裁剪，而裁剪规则只能有一份）。
    投机那条路 09-19 删了，工厂留着——组批的窗口路径同样要"这一帧的裁剪规则"随帧走。
    """
    def rec_crop(i):
        b_ = boxes[i]
        h_ = max(1, b_[3] - b_[1])
        want = int(args.rec_pad_x * h_)
        # **只往空白处扩。** 固定倍数是错的：yuka（1080p、字幕行两边空）上
        # 外扩 0.8 倍行高让省略号多读出 50%，同样的设置在 bili（720p、
        # 字旁边还有字）上反而把邻字的笔画吃进来，省略号还少了 26%。
        # 所以外扩量再按"到同行最近邻框的距离的一半"截一刀——
        # 孤立的行能扩满，挤在一起的行几乎不扩。
        gl = gr = 10 ** 6
        for k, o_ in enumerate(boxes):
            if k == i:
                continue
            ov = min(b_[3], o_[3]) - max(b_[1], o_[1])
            if ov < 0.5 * min(h_, max(1, o_[3] - o_[1])):
                continue                      # 不在同一行，不挡路
            if o_[2] <= b_[0]:
                gl = min(gl, b_[0] - o_[2])
            elif o_[0] >= b_[2]:
                gr = min(gr, o_[0] - b_[2])
        px_l = min(want, gl // 2)
        px_r = min(want, gr // 2)
        # 再看一眼要扩进来的是什么。**外扩只在纯背景上安全**：
        # yuka 的对话框是暗底（扩入边 std 8–16），扩进去只有省略号；
        # bili 是番剧画面（std 37–39），扩进去的是背景图，rec 的 conf
        # 变低的比变高的多 2.3 倍。std 这个量在**素材层面**分得很开，
        # 所以拿它当自动门，不用给每部片子手工设开关。
        if args.rec_pad_max_std > 0 and (px_l or px_r):
            sd = []
            if px_l:
                sd.append(gray[b_[1]:b_[3], max(0, b_[0]-px_l):b_[0]])
            if px_r:
                sd.append(gray[b_[1]:b_[3], b_[2]:min(W, b_[2]+px_r)])
            sd = [x for x in sd if x.size]
            if sd and float(np.mean([x.std() for x in sd])) > args.rec_pad_max_std:
                px_l = px_r = 0
        py = int(args.rec_pad_y * h_)
        return frame[max(0, b_[1] - py):min(H, b_[3] + py),
                     max(0, b_[0] - px_l):min(W, b_[2] + px_r)]
    return rec_crop


class Loop:
    """单进程的主循环（2026-10-05 从 `main()` 拆出来：原来是一串闭包 + nonlocal 计数）。
    `run` 逐帧调 `step`；计数在 `n`（`Counts`），`--rec-window` 的窗口和结算（同步 `settle_window`、异步 `settle_one` / `drain`）
    也挂在这里；`finish` 是循环之后、产物文件关上之前的收尾。"""

    def __init__(self, args, vp: VideoProbe, *, grp, v2, pool, rec, dense: list, fh, t_start: float) -> None:
        self.args, self.vp, self.grp, self.v2, self.pool, self.rec, self.fh = args, vp, grp, v2, pool, rec, fh
        self.t_start = t_start
        self.n = Counts()
        self.dense = dense
        self.rg_cuts = regions.cuts_us(grp) if grp is not None else []   # 时间切点（µs）+ 那一刻变了的像素：提取层据此切链
        self.prev_t_us = -(1 << 62)
        self.prev: list[tuple[list[int], str, float, int]] = []   # box, text, conf, 已连续沿用几帧（`--no-reuse-v2` 的旧判据）
        self.prev_frame: np.ndarray | None = None
        # PTS 不合法（非有限、为负、不严格递增）不能只记一笔就往下跑：
        # 一条坏时间轴不会让任何下游报错，只会让所有时间都错一点。
        # 它**独立于覆盖率那道门**——坏 pts 出现在最后几帧时 `got/want` 照样过。
        self.bad_pts: str | None = None
        self.clock = ptsclock.PtsClock()
        self.idx = vp.start_idx                 # 最后一个采样帧的帧号（交付序号空间；收尾时回抠和辅助流核对要它）
        self.t_us = 0
        self.win: list[dict] = []               # `--rec-window`：已 stage1、等文本的帧（按帧序）
        self.win_q: list[tuple] = []            # 同步路径：排队的裁剪 (窗内第几帧, 框下标, 裁剪)
        self.win_ids: list[list[tuple]] = []    # 异步路径：逐帧的 [(请求号, 框下标)]（和 win 同下标）
        self.settle_lag: dict[int, int] = {}     # 异步路径：结算时它后面已判决几帧 -> 帧数（`_meta.rec_settle_lag`）

    def in_dense(self, b: list[int]) -> bool:
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        return any(r[0] <= cx <= r[2] and r[1] <= cy <= r[3] for r in self.dense)

    def run_rec(self, batch: list) -> tuple[list, int]:
        """把一批裁剪送进 rec，返回 (结果, predict 调用次数)。

        结果长度**恒等于** batch，缺的位置放 None——压缩会让后面的框配到前一个框的文本上，而且看不出来。
        """
        if not batch:
            return [], 0
        if self.args.rec_bucket:
            results, calls = recpack.predict_bucketed(self.rec, batch, self.args.rec_bucket)
        else:
            results = self.rec.predict(batch, batch_size=1)         # `--rec-bucket 0`：一个裁剪一次
            calls = len(batch)
        if len(results) < len(batch):
            results = list(results) + [None] * (len(batch) - len(results))
        return list(results), calls

    def settle_window(self) -> None:
        """窗口结算（组批第一步，inference-runtime 计划）：这一窗攒下的裁剪**一次分桶送 rec**，
        再按**帧序**逐帧 `commit_stage2`。

        分桶就是"有多少取多少"的贪心消费者退化到同步窗口的样子：`predict_bucketed` 按目标宽排序后
        贪心成组、每组封顶 `--rec-bucket`，**不会为了凑满一桶而等**。

        ⚠ 和逐帧那条路有一处**故意的差别**：后端少返回时（`n.lost`，是个报警、实测恒 0），
        逐帧那条把那个框整个丢出产物，窗口这条已经在 stage1 建过行了，于是留下一行空文本。
        丢一个框本来就是"看不出来"的错，留一行看得见的空文本反而更好归因。
        """
        win, win_q, n = self.win, self.win_q, self.n
        if not win:
            return
        t_r0 = time.perf_counter()
        results, calls = self.run_rec([c for _, _, c in win_q])
        n.rec_calls += calls
        stage("rec", t_r0)
        texts: list[dict] = [{} for _ in win]
        n_none = 0
        for (pos, i, _c), r in zip(win_q, results):
            if r is None:
                n_none += 1
                continue
            texts[pos][i] = (r["rec_text"], float(r["rec_score"]))
        if n_none:
            print(f"  ⚠ 窗口 帧 {win[0]['idx']}–{win[-1]['idx']}: 送进去 {len(win_q)} 个框，"
                  f"丢了 {n_none} 个", flush=True)
            n.lost += n_none
        t_st = time.perf_counter()
        for st_, tx in zip(win, texts):
            extra = self.v2.commit_stage2(st_, tx, self.fh)
            n.rec += extra
            n.rec_calls += extra
            n.settled += 1
        stage("commit", t_st)
        win.clear()
        win_q.clear()

    def settle_one(self, block: bool = True) -> bool:
        """异步路径：结算**最老的那一帧**。`block=True` 等它的读齐（并标成优先——它卡着落盘水位）；
        `block=False` 只在读已经全回来时结算，没齐就返回 False、什么都不动（**不标优先**：每帧都标的话
        挑批就退回"一来就发"，rec-batching 计划）。"""
        if not self.win:
            return False
        rids = [r for r, _ in self.win_ids[0]]
        if block:
            t_w = time.perf_counter()
            got = self.pool.collect(rids, block=True, prio=True)
            STAGE["rec"] += tl.span("rec_wait", t_w) - t_w   # 等 rec 的时间记在 rec 段（时间线上单列 rec_wait）
        else:
            got = self.pool.collect(rids, block=False, prio=False)
            if got is None:
                return False
        texts, n_none = {}, 0
        for rid, i in self.win_ids[0]:
            r = got[rid]
            if r is None:
                n_none += 1
                continue
            texts[i] = r
        st_ = self.win.pop(0)
        self.win_ids.pop(0)
        lag = self.n.frames - 1 - st_["seq"]     # 结算时它后面已经判决了几帧（rec-batching 计划：逐帧结算延迟）
        self.settle_lag[lag] = self.settle_lag.get(lag, 0) + 1
        if n_none:
            print(f"  ⚠ 帧 {st_['idx']}: rec 丢了 {n_none} 个框", flush=True)
            self.n.lost += n_none
        t_st = time.perf_counter()
        # 回补用掉的调用数由池子自己数（`stats().calls` 含它），这里只并裁剪数
        self.n.rec += self.v2.commit_stage2(st_, texts, self.fh)
        self.n.settled += 1
        stage("commit", t_st)
        return True

    def drain(self, keep: int) -> None:
        """异步路径：把未结算的帧压到 `keep` 帧以内（阻塞）——主线程领先结算的上限（`--rec-window` − 1），
        和收尾时的全部结算。平时的结算在 `settle_ready`。"""
        while len(self.win) > keep:
            self.settle_one()

    def settle_ready(self) -> None:
        """异步路径，每帧判决前：按帧序结算所有读数已全部回来的最老帧，遇到第一帧没齐就停（不阻塞、不跳帧）。
        文本待定的链只待定实际的 rec 延迟，而不是固定的整窗——严格门造成的多读跟着少（rec-batching 计划）。
        产物因此随 rec 的快慢变：2026-09-19 起的固定滞后结算就是为了防这个，owner 2026-10-11 定这种机器忙闲带来的抖动不防。"""
        while self.settle_one(block=False):
            pass

    def progress_v2(self) -> None:
        args, n = self.args, self.n
        if args.progress_every and n.frames % args.progress_every == 0:
            el = time.time() - self.t_start
            done = (self.idx - self.vp.start_idx) / max(1, self.vp.end_idx - self.vp.start_idx)
            print(f"[{done:5.1%}] 视频 {self.t_us/1e6/60:6.1f}min(pts)  已用 {el/60:5.1f}min  "
                  f"框 {n.boxes} rec {n.rec} 回补 {self.v2.stats['recover_reads']}", flush=True)

    def run(self, frames_in) -> None:
        for idx, t_sec, frame, polys, pre in frames_in:
            self.idx = idx
            if not self.step(idx, t_sec, frame, polys, pre):
                break

    def step(self, idx: int, t_sec: float, frame, polys, pre) -> bool:
        """一个采样帧。返回 False = 时间轴坏了、停下（`bad_pts`）。"""
        args, grp, v2, pool, n = self.args, self.grp, self.v2, self.pool, self.n
        W, H, src_fps = self.vp.W, self.vp.H, self.vp.src_fps
        tl.mark("frame", idx)
        # 真实 PTS。`cv2` 那条是 `read()` 之后的 `CAP_PROP_POS_MSEC`
        # （实测对齐 ffprobe 的 `pts_time`），ffmpeg 那条是 `showinfo` 报的
        # `pts_time`——两条都是解码器给的，不是帧号推的（docs/architecture/pipeline.md §4）。
        try:
            t_us = self.t_us = self.clock.push(t_sec, where=f"帧 {idx}")
        except ptsclock.BadPts as exc:
            self.bad_pts = str(exc)
            return False
        boxes = [poly_to_box(p) for p in polys]
        own_of: dict = {}
        if grp is not None:
            # 被遮罩完全分断的 det 框拆成子框、各自裁到有效部分（ocr-regions 计划）；
            # 子框外接矩形里有别的块时 own_of 给出本块的遮罩，rec 裁剪按它涂灰
            polys, boxes, own_of = regions.split_polys(polys, grp.mask_at(t_sec))
        if self.dense:                  # 噪声区里的框连 rec 都不用送
            keep_idx = [i for i, b in enumerate(boxes) if not self.in_dense(b)]
            n.dropped += len(boxes) - len(keep_idx)
            # 多边形 / 框 / 子框遮罩**一起**按保留的下标取（2026-09-23 Codex 审计：原来 own_of 没跟着重排，
            # 删掉前面的框后遮罩配到别的框上 -> IndexError / 涂错）
            polys, boxes, own_of = regions.take(polys, boxes, own_of, keep_idx)

        decided: list[tuple[int, str, float, int]] = []   # 沿用上一帧的
        todo: list[int] = []                             # 需要识别的
        corr_of: dict[int, float] = {}                   # 复用判据算过的框内相关系数（--record-corr 才写出）
        v2_assign: dict = {}
        if grp is not None and self.rg_cuts:
            # 自选范围的时间切点落在上一个采样点和这一帧之间：切到的位置不许续链（owner："时间关闭段不可续链"）。
            # 两次采样之间关了又开的也算——两个采样帧都看得见，但中间那段没被观察过
            hit = [a for tc, a in self.rg_cuts if self.prev_t_us < tc <= int(round(t_sec * 1e6)) and a is not None]
            if hit:
                area = np.logical_or.reduce(hit)
                if v2 is not None:
                    v2.sever(area)
                else:
                    self.prev = [p for p in self.prev if not regions.box_hits(p[0], area)]
            self.prev_t_us = int(round(t_sec * 1e6))
        prev = self.prev
        shift = ((0, 0) if args.no_reuse or not prev or v2 is not None
                 else estimate_shift([p[0] for p in prev], boxes))
        if pool is not None:
            self.settle_ready()                  # 判决前先收已经回来的：链的待定早一步清掉
        if v2 is not None:
            t_st = time.perf_counter()
            # 灰度整帧给复用判据（`reuse_v2` 要留 --reuse-mem + 2 帧）：**存 uint8、取裁剪时再转 float32**，
            # 和原来存 float32 整帧逐位相同，内存差四倍（1080p 8.3 -> 2.1 MB/帧，4K 上更要紧）
            # `--pregate` 时灰度和静态相关系数在 det 那一层已经算好了（值按位相同，见 pregate_of）
            gray_full = pre[0] if pre is not None else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            tr0 = len(v2.trace) if v2.trace is not None else None     # 这一帧的轨迹从哪一条起（decide 每个框写一条）
            decided, todo, v2_assign = v2.decide(n.frames, gray_full, boxes,
                                                 pre=(pre[1] if pre is not None else None))
            stage("decide", t_st)
        for i, b in enumerate(boxes):
            if v2 is not None:
                break
            if args.no_reuse or not prev:
                todo.append(i)
                continue
            # 静止假设与滚动假设各试一次，取匹配更好的那个
            b_back = [b[0] - shift[0], b[1] - shift[1], b[2] - shift[0], b[3] - shift[1]]
            best = max(prev, key=lambda p: max(iou(b, p[0]), iou(b_back, p[0])))
            if max(iou(b, best[0]), iou(b_back, best[0])) < args.reuse_iou:
                todo.append(i)
                continue
            moved = iou(b_back, best[0]) > iou(b, best[0])
            if args.refresh_every and best[3] + 1 >= args.refresh_every:
                todo.append(i)                      # 该刷新了，重新识别一次
                continue
            # 位置对上了，再看这块像素是不是真没变。
            # 滚动的框要拿上一帧**它当时所在的位置**去比，不是原地比。
            ref_box = best[0] if moved else b
            c = corr(gray_crop(frame, b), gray_crop(self.prev_frame, ref_box))
            corr_of[i] = c                          # --record-corr 用：只记本来就算了的，不多算、不改判决
            if c >= args.reuse_corr:
                decided.append((i, best[1], best[2], best[3] + 1))
            else:
                todo.append(i)

        gray = (cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                if args.rec_pad_x and args.rec_pad_max_std > 0 else None)
        n_reused_here = len(decided)       # 此刻 decided 里只有沿用上一帧的
        # 只把**送进 rec 的裁剪**外扩，obs 里记的仍是原始 det 框——
        # 复用判据、聚类、几何统计全都不动，这样外扩是个单变量改动。
        rec_crop = make_rec_crop(args, W, H, frame, boxes, gray)
        if own_of:
            # 绑到这一帧（同 make_rec_crop 的规矩：异步 / 窗口路径会晚一点才调它）
            rec_crop = (lambda i, _base=rec_crop, _f=frame, _b=boxes, _o=own_of:
                        regions.masked_crop(_f, _b[i], _o[i]) if i in _o else _base(i))
        t_rec0 = time.perf_counter()
        todo_r = list(todo)
        crops = [rec_crop(i) for i in todo_r]
        keep = [i for i, c in zip(todo_r, crops) if c.size and c.shape[0] > 3 and c.shape[1] > 3]
        crops = [c for c in crops if c.size and c.shape[0] > 3 and c.shape[1] > 3]
        n.tiny += len(todo_r) - len(keep)  # 太小送不进 rec 的：既不是复用也不是识别
        if v2 is not None and tr0 is not None:
            for i, c in zip(keep, crops):     # 真读的裁剪指纹（`--trace`，量"同一张图重复读"有多少）
                v2.trace[tr0 + i].update(idx=idx, **crop_hashes(c))
        if args.rec_window:
            # 组批（inference-runtime 计划）：这一帧**不发 rec**，裁剪排进队列；
            # 判决里和文本无关的那一半（stage1）照常做完，等文本的那一半（stage2）留到窗口结算。
            # 裁剪是整帧的切片，而帧马上就要放掉——**必须拷**（异步那条还要给后台线程用）。
            if pool is not None:
                ids = pool.new_ids(len(keep))
                pool.submit([(r, c.copy()) for r, c in zip(ids, crops)])
                self.win_ids.append(list(zip(ids, keep)))
            else:
                for i, c in zip(keep, crops):
                    self.win_q.append((len(self.win), i, c.copy()))
            n.rec += len(crops)
            stage("rec", t_rec0)
            n.reused += n_reused_here
            t_st = time.perf_counter()
            if args.record_corr:
                corr_of.update(v2.last_corr)
            st = v2.commit_stage1(n.frames, idx, t_us, boxes, polys,
                                  decided + [(i, "", 0.0, 0) for i in keep], set(keep),
                                  v2_assign, rec_crop, corr_of, args.record_corr)
            stage("commit", t_st)
            if tr0 is not None:
                for d in st["deferred"]:     # 真读最后落在哪条链（`chain` 字段只是匹配到的候选；被拦下的跨空档链接会另起新链）
                    v2.trace[tr0 + d["i"]]["own"] = (d.get("new_chain") or d["chain"]).uid
            self.win.append(st)
            n.boxes += len(decided) + len(keep)
            n.frames += 1
            if pool is not None:
                self.drain(args.rec_window - 1)   # 未结算的帧不超过 W（下一帧进来之前留一个位置）
            elif len(self.win) >= args.rec_window:
                self.settle_window()
            self.progress_v2()
            return True
        results, calls = self.run_rec(crops)    # 后端少返回时**不许错位**：缺的位置是 None
        n.rec_calls += calls
        stage("rec", t_rec0)
        # 长度对不上就别静默 zip——短的那一头会把框直接吞掉，输出里不留痕迹。
        # 分桶路径不会变短（它按下标回填），缺的位置是 None，两种都要数进 n.lost。
        n_none = sum(r is None for r in results)
        if len(results) != len(crops) or n_none:
            lost = len(crops) - len(results) + n_none
            print(f"  ⚠ 帧 {idx}: rec 返回 {len(results) - n_none} 条，"
                  f"送进去 {len(crops)} 个框，丢了 {lost} 个", flush=True)
            n.lost += lost
        for i, r in zip(keep, results):
            if r is None:
                continue
            decided.append((i, r["rec_text"], float(r["rec_score"]), 0))
        read_set = set(keep)
        n.rec += len(crops)
        # 复用数按**真走了复用分支的框**算。原来是 len(boxes) - len(crops)，
        # 把"太小被过滤掉的框"也算成复用，reuse_rate 虚高。
        n.reused += n_reused_here

        if v2 is not None:
            t_st = time.perf_counter()
            if args.record_corr:
                corr_of.update(v2.last_corr)   # v2 的相关系数在它自己那儿算（上面 corr_of 只有老路径会填）
            extra = v2.commit(n.frames, idx, t_us, boxes, polys, decided, read_set, v2_assign,
                              rec_crop, corr_of, args.record_corr, self.fh)
            stage("commit", t_st)
            n.rec += extra
            n.rec_calls += extra
            n.boxes += len(decided)
            n.frames += 1
            self.progress_v2()
            return True
        cur: list[tuple[list[int], str, float, int]] = []
        for i, text, score, age in sorted(decided):
            b = boxes[i]
            reused = i not in read_set
            self.fh.write(json.dumps({
                # **`frame` 认 pts，不认"这是第几个采样帧"**（2026-09-20，owner："是 bug 就直接修掉"）。
                # 09-09 把 `t_us` 改成了真实 pts，`frame` 当时没跟着改，于是有丢帧缺口的素材上
                # 它是"第几个采样帧 × 采样间隔 + 起点"而不是源帧号（f3 跨缺口窗实测差 309~330 帧，
                # 606 行里 335 行的 `frame/src_fps` 对不上 `t_us`）。下游只拿它当身份用、
                # 时间一律走 `t_us`，所以轨没被带偏；但"拿 frame 回视频找那一帧"会找错。
                # ⚠ **只在写出去的这一处换**：`idx` 本身必须留在交付序号那个空间里，
                # 因为辅助流的帧环是按交付序号编号的（framesource 的 yield 注释）。
                "frame": framesource.pts_frame(t_us, idx, src_fps),
                "t_us": t_us, "box": b,
                "poly": [[round(float(p[0])), round(float(p[1]))] for p in polys[i]],
                "text": text, "conf": round(score, 4),
                **({"reused": True} if reused else {}),
                **({"corr": round(corr_of[i], 4)} if args.record_corr and i in corr_of else {}),
            }, ensure_ascii=False) + "\n")
            n.boxes += 1
            cur.append((b, text, score, age))
        self.prev, self.prev_frame = cur, frame
        n.frames += 1
        # 长片必须能看到真进度。**别用输出文件的内容推进度**——
        # 开了噪声区排除之后，没检测到文字的帧根本不写行，
        # 文件里最后一条的时间戳会远远落后于实际处理到的位置。
        if args.progress_every and n.frames % args.progress_every == 0:
            el = time.time() - self.t_start
            done = (idx - self.vp.start_idx) / max(1, self.vp.end_idx - self.vp.start_idx)
            # 分子是真 pts、分母是帧号推算的——有丢帧缺口的素材上这两个口径
            # 差几秒（f3 尾端 9.5 s），所以两边都标出来，别看着像同一把尺
            print(f"[{done:5.1%}] 视频 {t_us/1e6/60:6.1f}min(pts) / "
                  f"{self.vp.end_idx/src_fps/60:.0f}min(帧号推算)  "
                  f"已用 {el/60:5.1f}min  预计还要 {el*(1-done)/max(done,1e-6)/60:5.1f}min  "
                  f"框 {n.boxes} rec {n.rec} 丢 {n.dropped}", flush=True)
        return True

    def finish(self, meta: dict, edge, aux, source, side_clis: list) -> None:
        """循环之后、产物文件关上之前：结算最后不满一窗的几帧（edge.finish 会排空队列，必须在它之前）、
        回抠收尾与辅助流的账、组批自检、复用判据剩下的行落盘。"""
        args, pool, v2 = self.args, self.pool, self.v2
        if pool is not None:
            self.drain(0)
            if v2 is not None:
                self.n.rec += v2.finish_recover()   # 回补任务要在池子关之前做完、扣住的回抠批次要在 edge.finish 之前送完
            pool.close()
            meta["rec_pool"] = pool.stats()
            meta["rec_settle_lag"] = {str(k): v for k, v in sorted(self.settle_lag.items())}
            self.n.rec_calls += pool.stats()["calls"]
            if side_clis:                          # 旁路连接的分因数据（主连接的在 `final_meta` 里记成 `ort.rec`）
                meta.setdefault("ort", {})["rec_side"] = [{**c.stats(), "provider": c.provider_for("rec")} for c in side_clis]
                close_clients(*side_clis)
        else:
            self.settle_window()
        if edge is not None:
            if self.bad_pts is None:
                meta["edge"] = edge.finish(self.idx)
            else:
                edge.abort()
            if aux is None:                             # shared：第二路读线程挂在采样流上
                aux = getattr(source, "inner", source).aux
            if aux is not None:
                aux.join()
                meta.setdefault("edge", {})["aux_sent"] = aux.sent
                meta["edge"]["aux_stopped_early"] = aux.stopped_early
                if hasattr(aux, "last_key"):
                    meta["edge"]["aux_last_key"] = aux.last_key
                if getattr(aux, "misaligned", ""):
                    meta["edge"]["aux_misaligned"] = aux.misaligned
                    print(f"[回抠] ⚠ 辅助流的 pts 和帧对不上、提前停了：{aux.misaligned}", flush=True)
                meta["edge"]["aux_cpu_sec"] = round(getattr(aux, "cpu_sec", 0.0), 2)
                meta["edge"]["aux_shift"] = getattr(aux, "shift", 0)
                meta["edge"]["aux_mode"] = args.refine_aux
                meta["edge"]["noop"] = edge.noop
            elif args.edge_proc and "edge" in meta:     # 辅助流收在回抠进程里，它的统计随 finish 一起回来
                meta["edge"]["aux_mode"] = args.refine_aux
                meta["edge"]["noop"] = edge.noop
        if args.rec_window and self.n.settled != self.n.frames:
            # **运行时自检**（2026-09-19 复审第 5 条）：接线守卫只看得见"函数还在不在"，
            # 看不见"接错了"。这一条走的是主循环本身：stage1 过几帧，stage2 就得结算几帧。
            raise SystemExit(f"组批自检不过：stage1 了 {self.n.frames} 帧，stage2 只结算了 {self.n.settled} 帧"
                             f"——窗口没排空，或者结算那一路被绕过了")
        if v2 is not None and v2.held:
            # 结束了、还有读在途的链等最后一次读结算完才丢缓存（`ReuseV2.held`）；全部结算之后还剩，就是有链的在途计数没归零
            raise SystemExit(f"回补缓存自检不过：全部结算之后还有 {len(v2.held)} 条结束的链握着缓存（在途计数没归零）")
        if v2 is not None:
            v2.flush_all(self.fh)


def close_source(meta: dict, source, dec) -> None:
    """关取帧层，记解码进程和采样格的账。"""
    source.close()
    if dec is not None:
        meta["decode_shards"].update(dec.shards or {})   # 各路领了几块、各花多久、预取峰值
        meta["decode_shards"]["wait_sec"] = round(dec.wait_sec, 2)   # 这边等解码进程交帧的时间
        meta["hw_preroll_ticks"] = dec.preroll_ticks   # 第一块才知道（开流时读到的是 0）
    # 采样格核对（framesource.grid_step）：**偏离格子**的帧 vs **没交付的格**分开记（复审 P1 第 2 条）。
    # 软解两样都只记——偏离格子只在 index 选帧遇到缺口时出现，缺格是素材自己的缺口；
    # 硬解那条路在来源里就判过了（偏离、或者源里有却没交付，都 raise HwaccelMismatch -> 整段改软解）
    off = getattr(source, "grid_off", {}) or {}
    skipped = getattr(source, "grid_skipped", 0) or 0
    if off:
        meta["grid_off"] = {str(k): v for k, v in sorted(off.items())}
    if skipped:
        meta["grid_skipped"] = skipped
    if off or skipped:
        print(f"[采样格] ⚠ 偏离格子 {sum(off.values())} 帧（偏移取值 {sorted(off)}）、没交付的格 {skipped} 个。"
              f"有丢帧缺口的素材上这是正常的；否则见 hw-decode-results 报告", flush=True)
    if getattr(source, "stderr_tail", None):
        print("[解码] ffmpeg 最后几行：\n  " + "\n  ".join(source.stderr_tail[-4:]),
              flush=True)


def final_meta(args, meta: dict, loop: Loop, source, edge, aux_grid, clis: dict) -> None:
    """一趟跑完的账：ORT 分因、完成判据、计数、各段耗时，并进 `meta`。"""
    n, vp, v2, clock = loop.n, loop.vp, loop.v2, loop.clock
    for _tag, _cli in clis.items():
        if _cli is not None:                       # ORT 那条路的分因数据（ortclient.stats 的说明）
            meta.setdefault("ort", {})[_tag] = _st = _cli.stats()
            _st["provider"] = _cli.provider_for(_tag)    # 默认拓扑下 det / rec 两个 client 都带着两个模型，按这个模型取
            for _k in ("trt_oop", "trt_err"):         # 服务端的累计数，几条连接里最后看到的最大（rec 的旁路连接也在回信里带）
                _v = max([_st.get(_k, 0)] + [x.get(_k, 0) for x in (meta["ort"].get(f"{_tag}_side") or [])])
                if _v:
                    _st[_k] = _v
            if _st.get("trt_oop") or _st.get("trt_err"):
                print(f"[后端] ⚠ {_tag} 有 {_st.get('trt_oop', 0)} 次形状超出 TensorRT 引擎的范围、{_st.get('trt_err', 0)} 次 TensorRT 报错，"
                      f"这些退回 CUDA EP 跑了" + (f"；第一次的错：{_st['trt_err_first'][:200]}" if _st.get("trt_err_first") else ""), flush=True)
    wall = time.time() - loop.t_start
    # 完成判据看**实际推进到哪一帧**，不看"文件里有没有东西"。
    # `total_frames` 在某些容器上不准，所以留 2% 余量，只有明显短缺才判未完成。
    want = max(1, vp.end_idx - vp.start_idx)
    # **推进了多少源帧由来源自己报**：`cv2` 数的是 grab/read 的次数，
    # ffmpeg 那条数的是"最后一个采样帧的下一格 − 起点"——两者口径一致，
    # 都能喂进同一个 `is_complete`。别再用 `loop.idx`：
    # 它是**最后一个采样帧的帧号**，天生比推进量少最多一个采样间隔。
    got = source.advanced
    # **`stopped_early` 不能直接当"没跑完"**（2026-09-08 修）：解码器读到文件真正的
    # 结尾也会置上它，而 `CAP_PROP_FRAME_COUNT` 在这批切片上比实际多报了 1%
    # （36,361 声称 vs 36,001 实际）。于是**九段切片每次跑完都自称未完成**、
    # 退出码 3，驱动脚本会永远重跑它们。当时我是把产物的 `complete` 手工回填了，
    # **改数据没改代码**，这次连代码一起改。
    # 覆盖率这道门本来就是为容器元数据不准留的余量；中途真断了（比如 40% 处
    # 解码失败）`got/want` 自然过不了这道门，`stopped_early` 那一项是多余且有害的。
    # 它仍然记进 `_meta`，因为"是读到头了还是被我们停了"是排查时要看的信息。
    # **坏 PTS 一票否决。** 覆盖率那道门管的是"推进到哪一帧"，管不着时间轴对不对。
    # **辅助流没交够也一票否决**（2026-09-24 审计）：回抠证据半路断掉的 obs 不能当成做完了、被一直复用
    aux_bad = (ocr_complete.aux_shortfall(meta.get("edge"), loop.idx if n.frames else None, aux_grid)
               if edge is not None and loop.bad_pts is None else None)
    if aux_bad:
        meta["aux_incomplete"] = aux_bad
        print(f"**[回抠] ⚠ {aux_bad}——产物记 complete=false**", flush=True)
    complete = ocr_complete.is_complete(got, want) and loop.bad_pts is None and aux_bad is None
    # `end_sec` 原来写的是 `total_frames / src_fps`——又一个平均帧率推算，
    # 在有缺口的素材上比真实时长短（f3 少 12.1 s）。改成**最后一个采样帧的真实 PTS**。
    # `start_sec` 仍是**请求的**窗口起点：`tools/ocr_complete.obs_reusable` 拿它和
    # `data/samples.json` 的 `start_s` 对账，那是"跑的是不是同一段"的判据，不是时间轴。
    # 实际覆盖到的 PTS 区间另记两个键，谁要判覆盖用它们。
    meta.update({"complete": complete, "frames_advanced": got, "frames_requested": want,
                 "stopped_early": source.stopped_early, "bad_pts": loop.bad_pts,
                 "sampled_frames": n.frames, "boxes": n.boxes,
                 "rec_calls": n.rec, "rec_predict_calls": n.rec_calls, "reused_boxes": n.reused,
                 "reuse_rate": round(n.reused / max(1, n.boxes), 4),
                 "dropped_dense": n.dropped, "dropped_tiny": n.tiny, "lost_rec": n.lost,
                 "wall_sec": round(wall, 2), "sec_per_frame": round(wall / max(n.frames, 1), 3),
                 "start_sec": args.start,
                 "end_sec": (args.end if args.end > 0 else
                             (clock.last_us / 1e6 if clock.last_us is not None else 0.0)),
                 **clock.meta(),
                 "realtime_x": round(clock.span_sec / wall, 2) if wall else None,
                 "stage_sec": {k: round(v, 2) for k, v in STAGE.items()},
                 **({"reuse_v2": v2.stats, "reuse_v2_sec": {k: round(x, 2) for k, x in v2.sec.items()}}
                    if v2 is not None else {}),
                 # 平均批直接记下来：**这次就是它把"窗口分支被误删"抓出来的**（5.01 -> 1.67），
                 # 而 rec_calls / rec_predict_calls 两个数要自己相除才看得出来
                 "rec_avg_batch": round(n.rec / max(1, n.rec_calls), 2),
                 })


def write_trace(path: str, v2, out_path: Path, args, frame_us: int) -> None:
    """`--trace`：复用判据的因果轨迹，带上**真实的张量宽**（重放要按它分桶；`recpack.rec_target_w` 和管线用同一份）。"""
    tp = Path(path)
    tp.parent.mkdir(parents=True, exist_ok=True)
    with tp.open("w", encoding="utf-8") as th:
        head = {"tag": out_path.stem, "frame_us": frame_us,
                "reuse_corr": args.reuse_corr, "reuse_corr_digit": args.reuse_corr_digit,
                "rec_bucket": args.rec_bucket}
        print(json.dumps({"_meta": head}, ensure_ascii=False), file=th)
        for r in v2.trace:
            b = r["box"]
            r["tw"] = recpack.rec_target_w(max(1, b[3] - b[1]), max(1, b[2] - b[0]))
            print(json.dumps(r, ensure_ascii=False), file=th)
    print(f"[轨迹] {len(v2.trace)} 条 -> {tp}", flush=True)


def rewrite_meta(out_path: Path, meta: dict) -> None:
    """产物第一行的 `_meta` 开头只写了起动时知道的那些，跑完换成全的。"""
    lines = out_path.read_text(encoding="utf-8").splitlines()
    lines[0] = json.dumps({"_meta": meta}, ensure_ascii=False)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def exit_status(meta: dict) -> int:
    """打一行汇总；时间轴坏了退 4，没跑完退 3（产物留着排查，`_meta.complete=false` 不让驱动当成已完成）。"""
    print(json.dumps({k: meta[k] for k in
                      ("complete", "sampled_frames", "boxes", "rec_calls", "reuse_rate",
                       "dropped_dense", "dropped_tiny", "lost_rec", "wall_sec",
                       "sec_per_frame", "realtime_x")},
                     ensure_ascii=False))
    if meta["bad_pts"]:
        print(f"**时间轴不可信**：{meta['bad_pts']}"
              f"\n  产物留着排查用，但 `_meta.complete=false`——不要拿它进正式成绩。",
              flush=True)
        return 4
    if not meta["complete"]:
        # 部分产物留着（可以拿来续跑/排查），但**退出码必须非零**，
        # 而且 `_meta.complete=false` 会让驱动脚本不把它当成已完成。
        want, got = meta["frames_requested"], meta["frames_advanced"]
        print(f"**没跑完**：请求 {want} 帧，只推进到 {got} 帧"
              f"（{got/want:.1%}）；stopped_early={meta['stopped_early']}。"
              f"\n  产物留着但**不算完成**——不要拿它进正式成绩。", flush=True)
        return 3
    return 0


def main() -> int:
    # 参数定义在共享层（`flowocr.extract.ocr_args`）：复用判据要拿**同一个解析器**
    # 算出"这一趟会用哪组生效值"，才能和 `_meta.config` 比
    args = ocr_args.parse_args()
    check_args(args)
    ffcheck.require()                               # ffmpeg / ffprobe / showinfo 缺了就在这里说清楚，别等取帧时 FileNotFoundError
    ffcheck.require_runtime()                       # onnxruntime 一个都没有 / 两个都在就退出；非 Windows 打警告
    config = ocr_args.product_config(args)          # 请求的生效值（拆进程关没关另记 `_meta.proc_split`）
    if args.timeline:
        if args.workers > 1:                        # 同 --trace：几个 worker 会写同一个文件
            raise SystemExit("--timeline 要 --workers 1（否则几个 worker 写同一份时间线，互相覆盖）")
        Path(args.timeline).parent.mkdir(parents=True, exist_ok=True)
        tl.start(args.timeline)                     # 子进程（回抠 / 解码）从环境变量认出来、各写各的
    tl.mark("boot", "args")                         # 起动 / 收尾各段的分界（timeline_report 的"起动"那张表）
    split_off = ocr_args.resolve_split(args)
    # `--decode-shards`：只解码的子进程第一件事就起（它不 import 推理库，起动约 1 s，和这边建模型并行；`--workers` 下每个 worker 自己起）
    # （`run_groups --share-decode` 时不起自己的，接上多组共用的广播解码进程，decode_proc.connect_decoder）
    dec = decode_proc.connect_decoder() if args.workers == 1 and args.decode_shards > 1 else None

    vp = probe_video(args)
    src_fps, W, H, grid = vp.src_fps, vp.W, vp.H, vp.grid
    total_frames, start_idx, end_idx = vp.total_frames, vp.start_idx, vp.end_idx

    aux_grid = resolve_aux_grid(args, src_fps)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    meta = base_meta(args, config, vp, aux_grid)
    grp = setup_regions(args, meta, W, H)

    if args.workers > 1:
        # **在建模型之前分叉**：父进程只切段、起子进程、拼结果，不占 GPU。
        # `--workers 3` 是默认（owner 2026-09-16），所以开不了的情况（窗口太短切不出段、有丢帧缺口的素材
        # 切点会漂）**不能再退出**，退回单进程、把原因打出来、meta 记 `workers_effective`。
        # `config.workers` 仍记**请求的**值——复用判据比的是同一段 + 同一条臂，退回是这段素材的确定性行为，
        # 同样的 argv 再跑一遍还是退回，产物可复现
        why = parallel_blocker(args, src_fps, grid, start_idx, end_idx, total_frames)
        if why is None:
            return run_parallel(args, meta, src_fps, grid, start_idx, end_idx, total_frames)
        print(f"[并行] --workers {args.workers} 开不了，退回单进程：{why}", flush=True)
        if not args.det_prefetch:
            print("[并行] 提示：`--det-prefetch` 被显式关掉了；它现在默认是 2，quick 六段上省 15%~25%"
                  "（产物逐字节相同，inference-runtime 计划）", flush=True)
    meta["workers_effective"] = 1
    if os.environ.get("FLOWOCR_RUN_GROUPS"):          # run_groups 的一组：这一趟怎么共用的（产物不变，不进 config）
        meta["run_groups"] = json.loads(os.environ["FLOWOCR_RUN_GROUPS"])
    meta["proc_split"] = {"edge": bool(args.edge_proc and args.refine_fused), "decode": args.decode_shards,
                          **({"decode_off": args.decode_shards_off} if getattr(args, "decode_shards_off", "") else {}),
                          **({"off": split_off} if split_off else {})}

    # 硬解试解（ffmpeg 解 4 格，约 0.65 s）和建模型无关：后台先跑着，用到的时候（建取帧层之前）再取结论
    hw_probe = hw_pool = None
    if args.hwaccel:
        import concurrent.futures as _cf
        hw_pool = _cf.ThreadPoolExecutor(1, thread_name_prefix="hw-probe")
        hw_probe = hw_pool.submit(
            framesource.hwaccel_blocker, args.video, grid=grid, start_idx=start_idx, src_fps=src_fps,
            width=W, height=H, hwaccel=args.hwaccel, select_by=args.frame_select)
    tl.mark("boot", "probed")
    try:
        device, fallback, (rec, fast_det, det_cli, ort_cli) = resolve_device(args)
    except BaseException:
        if dec is not None:
            dec.close()
        raise
    tl.mark("boot", "models")
    # `--det-batch auto` 等设备定了才能定（`--device auto` 可能退回 CPU）；之后帧环容量、解码槽、det 流都读这个整数
    args.det_batch = meta["det_batch"] = det_batch(args, device)
    record_devices(args, meta, device, fallback, fast_det, det_cli, ort_cli)
    fast_det = apply_region_crop(args, grp, fast_det, meta, W, H)
    side_clis, pool = make_rec_pool(args, device, rec, ort_cli)
    v2 = make_reuse(args, rec, pool, src_fps) if args.reuse_v2 else None
    dense = load_dense(args)
    t_start = time.time()

    aux = None
    edge = setup_edge(args, v2, meta, grp, vp, aux_grid) if args.refine_fused else None
    tl.mark("boot", "edge")
    settle_hwaccel(args, hw_probe, hw_pool, meta)
    tl.mark("boot", "hw_checked")
    source, dec = open_source(args, dec, edge, meta, vp, aux_grid)
    print(f"[解码] {source.describe()}", flush=True)   # 7aa8e1a 删 --front-proc 时连这两行一起删了（2026-09-24 审计找回）
    tl.mark("boot", "source")
    # 有前摇的 av1 从文件头起硬解时的平移修法（framesource.av1_preroll_ticks）：生效就记刻度，追溯用
    meta["hw_preroll_ticks"] = getattr(getattr(source, "inner", source), "preroll_ticks", 0)
    meta["seek_rev"] = framesource.SEEK_REV
    if meta["hw_preroll_ticks"]:
        print(f"[硬解] 有前摇的 av1：-ignore_editlist + setpts 平移 {meta['hw_preroll_ticks']} 刻度（试解已核对像素）",
              flush=True)
    if edge is not None and args.refine_aux in ("nvdec", "cuvid"):
        aux = framesource.FfmpegFullRate(args.video, start_idx=start_idx, end_idx=end_idx, src_fps=src_fps,
                                         width=W, height=H, step=aux_grid.step, scale=args.refine_scale,
                                         sink=edge.ring, hwaccel="cuda", cuvid=args.refine_aux == "cuvid").start()
    if edge is not None:
        print(f"[回抠] 在线：辅助流 {edge.Ws}x{edge.Hs} 灰度、{aux_grid.describe(src_fps)}，"
              f"帧环 {edge.ring.cap} 帧（{edge.ring.cap * edge.Ws * edge.Hs / 1e6:.0f} MB）", flush=True)

    with out_path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"_meta": meta}, ensure_ascii=False) + "\n")
        loop = Loop(args, vp, grp=grp, v2=v2, pool=pool, rec=rec, dense=dense, fh=fh, t_start=t_start)
        if start_idx:
            print(f"[窗口] 帧 {start_idx}–{end_idx}"
                  f"（{start_idx/src_fps/60:.1f}–{end_idx/src_fps/60:.1f} min，"
                  f"时间戳仍按原片绝对时间）", flush=True)
        # 自选范围：det 之前把范围外涂灰（取帧那一层包一下，其余属性照旧从 `source` 取）
        src_in = regions.MaskedSource(source, grp) if grp is not None else source
        frames_in = (det_stream_threaded(src_in, fast_det, args.det_batch, args.det_prefetch, args.pregate, args.det_lookahead)
                     if args.det_prefetch else det_stream(src_in, fast_det, args.det_batch, args.pregate, args.det_lookahead))
        loop.run(frames_in)
        loop.finish(meta, edge, aux, source, side_clis)
    tl.mark("boot", "loop_done")
    close_source(meta, source, dec)
    final_meta(args, meta, loop, source, edge, aux_grid, {"rec": ort_cli, "det": det_cli})
    if args.trace and v2 is not None and v2.trace is not None:
        write_trace(args.trace, v2, out_path, args, int(round(grid.interval_us(src_fps))))
    if args.timeline:
        tl.mark("boot", "end")
        print(f"[时间线] -> {tl.dump()}", flush=True)
    rewrite_meta(out_path, meta)
    return exit_status(meta)


def worker_main() -> int:
    """worker：真正干活的那一趟。硬解跑到一半交付不对 -> 写下原因、退出码 `EXIT_HW_MISMATCH`，
    由外层监督者在**本进程完全退出之后**起软解那一趟（`supervisor.supervise`）。
    ⚠ 软解那一趟自己再炸就照常抛——不许无限重试。"""
    try:
        return main()
    except framesource.HwaccelMismatch as e:
        if os.environ.get(HW_RETRY_ENV):
            raise
        print(f"[硬解] 交付不对：{e}——退出，交给外层改软解重跑", flush=True)
        orphan = os.environ.get(FAULT_ORPHAN_ENV)
        if orphan:
            q = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
            Path(orphan).write_text(str(q.pid), encoding="utf-8")
        rf = os.environ.get(HW_REASON_ENV)
        if rf:
            Path(rf).write_text(str(e), encoding="utf-8")
        return EXIT_HW_MISMATCH


if __name__ == "__main__":
    raise SystemExit(worker_main())
