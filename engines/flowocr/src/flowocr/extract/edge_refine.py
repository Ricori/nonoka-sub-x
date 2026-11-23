"""两遍合一遍：OCR 那遍**在线**把首字 / 全字 / 消失的像素信号量出来，写进 obs 行的 `edge` 字段。

回抠（`flowocr.analyze.refine_boundaries`）原来是第二遍：建轨之后按 run 的首尾开解码窗、顺序解码整片。
它要的只是每条 run 首尾几十帧里那个框的裁剪，而 OCR 那遍的解码器本来就解了每一帧、`--reuse-v2` 的链在线知道
每个框何时**出现 / 变了 / 消失**。所以把两件事并起来（reuse-budget 计划）：

* 一路**全帧率、缩到 540p、只要亮度（gray）**的辅助流跟在采样流旁边（`framesource.FfmpegFullRate`），
  进一个有界的帧环 `FrameRing`。1080p 全帧率走 Windows 管道只有 660 MB/s、比 OCR 本身还慢，
  540p 之后只比只送采样帧多 4 s / 5 min（probe_fullrate）。
* `reuse_v2.commit` 每个采样点把"这一帧哪些框是新链 / 文本变了 / 没变、哪些链断了"交给 `EdgeRefiner.sample`；
  工作线程从环里抠**变化段**（链首或文本变化起，到文本停变后的那一个采样间隔为止）的裁剪，
  停变那一帧当模板，跑 `typewriter_fuse.signals`（和回抠**同一份**信号代码）；链断时抠结尾窗跑 `end_keyframes`
  + 整框相关系数的退回判据。结果只有十几个数，挂到链首行（`edge.on`）/ 末行（`edge.off`）上。
* 之后 `refine_boundaries --decoder obs` 不开视频：按 run 找到它首末行上的证据，重建 `measure()` 的输出，
  走原来的 `apply()`（OCR 硬区间、多信号融合、只防晚很多——一行不改）。

窗口口径照抄 refine_boundaries.make_job：打字机窗从首字前一个采样间隔再往前 LEAD（50 ms），到采样级全字之后
一个采样间隔（不越过末次观测帧）；结尾窗从 t_end 前 1.5 个间隔 + 50 ms 到 t_end 后 200 ms；退回判据在
[末次观测帧, +一个间隔] 上。

**时间：帧号换算，但锚在一个真实 pts 上**（2026-09-17 审计改的）。辅助流只有帧号，没有逐帧 pts，
所以窗内仍按 `Δ帧 × us_per_frame` 走；但每条证据记下它所属采样点的 `a_idx / a_us`（`a_us` 是 obs 行的
真实 pts），时间一律算成 `a_us + (i − a_idx) × us_per_frame`。**这样掉的是"帧号时钟和 pts 的累计偏差"**：
有丢帧缺口的素材上（yuka f4，`src_fps` 59.9870）`t_us − idx/fps` 在 20 分钟窗口里从 +2.3 ms 漂到 **−257.7 ms**，
f3 在 7.2 h 处是 9.5 s（docs/dev-guide/pitfalls.md「容器平均帧率不是时间轴」），而 run 的 t_start/t_end 是 pts；不锚就是两套时钟混用。
锚之后只剩窗内（几十帧）的漂，缺口不落在窗里就是 0。CFR 素材上 `t_us − idx/fps ≡ 0`，所以历史读数不变。
当时设想的彻底修法（辅助流逐帧带 pts）2026-09-17 量过不必要：剩下的漂来自平均帧率和窗内真实帧率之差，锚住窗口就够（reuse-budget 计划 §8 第 1 条）。

本模块**不 import cv2**（守卫在系统 Python 里跑；裁剪是 numpy 切片，辅助流已经是灰度）。
线程模型：OCR 主线程只往队列里放；工作线程等环里的帧、抠、算、写回行的 dict；`wait_batch` 让延迟落盘那一层
在写某个采样点的行之前先等它那一批处理完。环满时辅助流的读线程阻塞（背压），环的容量按窗口口径算，
工作线程最多落后主线程两个采样点，不会死锁（见 `ring_capacity`）。
"""
from __future__ import annotations

import threading
import time
from difflib import SequenceMatcher
from queue import Queue

import numpy as np

from flowocr.extract import timeline as _tl   # --timeline（decode-buffer）；只有标准库
from flowocr.extract.framegrid import AuxGrid, TimeGrid
from flowocr.extract import typewriter_fuse as TF

PAD = 6
"""首字 / 全字的裁剪在框外各扩这么多像素（**全分辨率下**，同 refine_boundaries.PAD；按 scale 缩）。"""
LEAD_US = 50_000
"""打字机窗在"首字前一个采样间隔"之外再往前多取这么久（同 refine_boundaries.LEAD_US）。"""
END_LEAD_US = 50_000
"""结尾窗起点：t_end 前 1.5 个采样间隔再往前这么久。"""
END_TAIL_US = 200_000
"""结尾窗终点：t_end 之后这么久（淡出 / 交叉淡出的尾巴）。"""
MAX_OPEN_SAMPLES = 40
"""变化段最多跨这么多采样点还没停变就放弃（滚动的跑马灯 / 计时器不是打字机；也别让裁剪堆无限长）。"""
RECOVER_HOLD = 8
"""回补异步（`--recover-async`）时最多扣住几个回抠批次等回补任务做完（`reuse_v2.ReuseV2.hold_max`，还要小于落盘等回抠的距离）。
扣住的批也压着帧环，所以 `ring_in_flight` 跟着加这么多。"""
GROW_SIM = 0.6
"""文本变了：新文本的前缀和旧文本像到这个程度算**打字机在长**（旧文本没消失，不用给它开结尾窗）；
否则是**换句**（同一位置紧接着出下一句），旧文本的末行也要一份 `edge.off`——build_tracks 会在这里切 run。"""


def is_growth(old: str, new: str) -> bool:
    a, b = TF.chars(old), TF.chars(new)
    if not a or not b or len(b) <= len(a):
        return False
    return SequenceMatcher(None, a, b[:len(a)]).ratio() >= GROW_SIM


def is_jitter(old: str, new: str) -> bool:
    """同一段字被 rec 读得略有不同（字数没长）：不是变化，别重开窗。"""
    a, b = TF.chars(old), TF.chars(new)
    return bool(a and b) and not is_growth(old, new) and SequenceMatcher(None, a, b).ratio() >= GROW_SIM


def overlap_match(box, ref_box) -> float:
    """证据行的框配 run 的框：同一行（y 重叠过半）且 x 重叠 ≥ 较窄者的一半 -> 返回 x 重叠占较窄者的比例，否则 0。"""
    h = max(1.0, ref_box[3] - ref_box[1])
    if min(box[3], ref_box[3]) - max(box[1], ref_box[1]) < 0.5 * h:
        return 0.0
    w = max(1.0, min(box[2] - box[0], ref_box[2] - ref_box[0]))
    ov = (min(box[2], ref_box[2]) - max(box[0], ref_box[0])) / w
    return ov if ov >= 0.5 else 0.0


def line_match(box, ref_box) -> bool:
    """同 typewriter_fuse.ocr_points 的"同一行位置"：y 重叠过半、左沿差不到 1.5 个字高。"""
    x0, y0, _, y1 = ref_box
    h = max(1.0, y1 - y0)
    return min(box[3], y1) - max(box[1], y0) >= 0.5 * h and abs(box[0] - x0) <= 1.5 * h


def ring_in_flight(args) -> int:
    """解码头到回抠工作线程之间最多压着几个采样帧——帧环的容量按它给（`ring_capacity`；漏一项就环满卡解码）：
    取帧预取 + det 批 ×（1 + det 预取 + det 流水的前瞻）+ rec 窗口（主线程领先结算的上限）+ 扣着等回补的回抠批次（`--recover-async`）
    + `--decode-shards` 的共享内存槽。
    放在这里而不是 run_ocr2：守卫要能按行为测它（run_ocr2 顶层 import 推理库，守卫够不着），原来只能查源码里有没有那几个参数名。"""
    from flowocr.extract import decode_proc      # 模块顶层只有标准库
    ahead = args.det_lookahead if args.det_batch > 1 else 0          # `--det-lookahead`：辅助线程上正在推理的那一批
    hold = RECOVER_HOLD if args.recover_async and args.rec_async else 0      # 扣着等回补任务的回抠批次
    return (args.prefetch + args.det_batch * (1 + args.det_prefetch + ahead) + args.rec_window + hold
            + (decode_proc.slots_for(args.det_batch, args.det_prefetch) if args.decode_shards > 1 else 0))


def aux_frames_in(aux: AuxGrid | None, span: int) -> int:
    """连续 `span` 个源帧里辅助流最多有几帧（帧环装的是辅助流的帧，容量按它数）。没给网格 = 每帧都要。
    等距步长 q：⌈span / q⌉ + 1；不等距（自己的网格 ∪ 采样网格）：两个网格各自的上界相加，宁多勿少。"""
    if aux is None:
        return span

    def upper(g: TimeGrid) -> int:
        return -(-span * g.p // g.q) + 1
    if aux.uniform:
        return min(span, upper(aux.own))
    return min(span, upper(aux.own) + upper(aux.sample))


def ring_capacity(gap: int, lead: int, tail: int, strides: int = 5, in_flight: int = 1, aux: AuxGrid | None = None) -> int:
    """帧环要装多少帧：`release` 之后环里留着工作线程处理的那个采样点之前 2 个间隔（+ lead + 3，回看窗），
    往前一直到**解码头**——中间压着 `in_flight` 个采样帧（取帧预取 + det 批 ×（1 + det 预取）+ rec 窗口
    + `--decode-shards` 的共享内存槽，调用方逐项加，`ring_in_flight`），外加工作线程最多落后的 2 个采样点（文件头）。
    所以下界是 **in_flight + 4 个采样间隔** + lead + tail（结尾窗）+ 8 个源帧的跨度。`strides`（`--refine-ring`）只是地板。
    `aux` 给了就把这段跨度换算成**辅助流的帧数**（`aux_frames_in`）：素材帧率高于 `--aux-max-fps` 时辅助流隔帧取，
    按源帧数给容量会多占一倍内存（120 fps 的片子默认只收 60 fps）；没给就按源帧数（每帧都进环）。

    装不下就不只是慢：一路解码时采样帧和辅助流出自**同一个 ffmpeg**，环一满整个 ffmpeg 就停。
    分片（`--decode-shards`，默认）时辅助流经转发线程按块序送来，环满先卡转发线程、再经字节预算压回各块的 ffmpeg；
    主流最多领先辅助流 `ahead` 块 + 预算，等待链不成环（2026-09-25 审计追过），但 `in_flight` 这条公式还是按一路的结构写的。
    * 2026-09-17：第一版没算批 det 的前瞻，`--det-batch 8` + 5 个间隔的环**互等到死**（主循环等采样帧、辅助流等环），
      100 秒没出一帧；于是加了 det 批那一项。
    * 2026-09-23（decode-buffer）：那一项只算了 det 批 ×（1 + 预取）+ 窗口（≈ 17 个间隔），漏了取帧预取和上面那 4 个，
      回抠换进程之后环满仍卡 8~14 s / 5 分钟。T4 放大到 28 / 40 个间隔：`--edge-proc` 打平、当时的 `--front-proc`（已删）每段 −1%~−3%，方向一致。
      现在的默认（预取 4、批 4、det 预取 2、窗口 4）是 24 个间隔，拆进程再加 16 个槽是 40——1080p 60 fps、0.35 灰度时约 180 / 305 MB 内存。"""
    return aux_frames_in(aux, max(5, strides, in_flight + 4) * gap + lead + tail + 8)


class FrameRing:
    """帧号 -> 灰度帧的有界环。生产者 `push` 满了就阻塞；消费者 `wait_for(idx)` 等到那一帧到了（或流结束）；
    `release(upto)` 丢掉更早的帧、并记住 `floor`——**之后再推进来的、低于 floor 的帧只记帧号不存**。
    第一版没有 floor，死锁过一次（yuka-f5 实跑，2026-09-16）：辅助流落后于 OCR 主循环时，工作线程按"这一批没有要抠的窗"
    先把低位帧号放掉了，随后辅助流才把这些已经放掉的旧帧推进来、把环填满、阻塞；而工作线程下一批等的是更高的帧号。"""

    def __init__(self, cap: int, stall_sec: float = 90.0) -> None:
        # `stall_sec`：推不进去又没人来取，卡这么久就**认定互等**并炸（2026-09-17）。
        # 容量算对了就永远不该触发；但"等某个一定会来的东西"的地方必须有出口——
        # 这已经是同一条教训的第三次（帧环死锁、cuvid 负 pts，都是等到天荒地老）
        self.cap = cap
        self.stall_sec = stall_sec
        self.stalled = False
        self.frames: dict[int, np.ndarray] = {}
        self.cv = threading.Condition()
        self.highest = -1
        self.floor = -1
        self.eof = False
        self.pushed = 0
        self.dropped = 0
        self.blocked_sec = 0.0
        self.premask = None
        """`(帧号, 帧) -> 帧`：进环之前按自选范围涂灰（`regions.aux_masker`，ocr-regions 计划）。None = 不受限。
        在锁外做（逐像素的活不该卡住消费者）。"""

    def push(self, idx: int, frame: np.ndarray) -> None:
        if self.premask is not None:
            frame = self.premask(idx, frame)
        with self.cv:
            if idx >= self.floor and len(self.frames) >= self.cap:
                t0 = time.perf_counter()
                while idx >= self.floor and len(self.frames) >= self.cap and not self.eof:
                    self.cv.wait(0.5)
                    if time.perf_counter() - t0 > self.stall_sec:
                        # 环满、没人取、流也没结束 = 互等（容量不够装下主循环的前瞻）。
                        # 标记 + 放行，让消费者那边抛出来，别静静地挂着
                        self.stalled = True
                        self.eof = True
                        self.cv.notify_all()
                        return
                self.blocked_sec += _tl.span("ring_blocked", t0, a=idx) - t0
            if self.eof:
                return
            if idx >= self.floor:
                self.frames[idx] = frame
            else:
                self.dropped += 1
            self.highest = max(self.highest, idx)
            self.pushed += 1
            self.cv.notify_all()

    def close(self) -> None:
        with self.cv:
            self.eof = True
            self.cv.notify_all()

    def wait_for(self, idx: int) -> bool:
        """等到帧号 ≥ idx 的帧进过环。流先结束了就 False；**互等**了就抛（见 `stall_sec`）。"""
        with self.cv:
            while self.highest < idx and not self.eof:
                self.cv.wait(0.5)
            if self.stalled:
                raise RuntimeError(
                    f"帧环互等：容量 {self.cap} 帧装不下主循环的前瞻（`--det-batch` × 采样间隔），"
                    f"辅助流推不进来、采样帧也就停了。把 `--refine-ring` 调大，或减小 `--det-batch`")
            return self.highest >= idx

    def take(self, lo: int, hi: int) -> list[tuple[int, np.ndarray]]:
        # 按窗口里的帧号逐个查，不扫整个环（环上千帧、窗口几十帧；回抠线程里原来约 5%，2026-10-16）
        with self.cv:
            fr = self.frames
            if hi - lo + 1 > len(fr):
                return sorted((k, f) for k, f in fr.items() if lo <= k <= hi)
            return [(k, fr[k]) for k in range(lo, hi + 1) if k in fr]

    def release(self, upto: int) -> None:
        with self.cv:
            self.floor = max(self.floor, upto)
            for k in [k for k in self.frames if k < upto]:
                del self.frames[k]
            self.cv.notify_all()


class Seg:
    """一条链的一个**变化段**：从链首 / 文本开始变，到停变后的那个采样点。裁剪按整行带（框的 y 范围外扩、整幅宽）存，
    停变时再按最终的框切——打字机在长字、框在变宽，段开始时不知道最终的框有多宽。"""
    __slots__ = ("key", "row", "text", "box", "y0", "y1", "lo", "idxs", "stacks", "opened_seq", "last_seq", "short",
                 "stale")

    def __init__(self, key: int, row: dict, text: str, box, y0: int, y1: int, lo: int, seq: int) -> None:
        self.key, self.row, self.text, self.box = key, row, text, box
        self.y0, self.y1, self.lo = y0, y1, lo
        self.idxs: list[int] = []
        self.stacks: list[np.ndarray] = []
        self.opened_seq = self.last_seq = seq
        self.short = False
        self.stale = False                       # 回补把这一段的变化点前移了 -> 窗口口径已经不对，证据作废


class EdgeRefiner:
    def __init__(self, *, src_fps: float, start_idx: int, width: int, height: int,
                 scale: float, grid, thr: float, ring_cap: int | None = None, noop: bool = False) -> None:
        # `grid` = 辅助流取哪些帧（`AuxGrid` 或它的 `as_list()`，跨进程传的是后者）；采样网格是它的 `sample`（嵌套在里面）。
        # 窗口里"上一个 / 下一个采样帧"都问采样网格（不等距时相邻间隔差一帧），不再按固定步长减
        self.grid = grid if isinstance(grid, AuxGrid) else AuxGrid.from_list(grid)
        self.sgrid = self.grid.sample
        if not 0 < scale <= 1.0:
            raise ValueError(f"scale 要在 (0, 1]，给的是 {scale}")
        self.start_idx = start_idx
        gap = self.sgrid.max_gap                 # 相邻两个采样帧最多隔几帧（等距就是步长）
        self.us = 1_000_000 / src_fps
        self.thr = thr
        self.W, self.H = width, height
        self.Ws, self.Hs = ((int(width * scale / 2) * 2, int(height * scale / 2) * 2) if scale < 1.0
                            else (width, height))
        self.sx, self.sy = self.Ws / width, self.Hs / height
        self.pad = max(2, int(round(PAD * self.sx)))
        self.lead = int(round(LEAD_US / self.us))
        self.end_lead = int(round(END_LEAD_US / self.us))
        self.tail = int(round(END_TAIL_US / self.us))
        self.ring = FrameRing(ring_cap or ring_capacity(gap, self.lead, self.tail, aux=self.grid))
        self.keep_back = 2 * gap + self.lead + self.end_lead + 3
        """放掉帧环里早于"当前采样帧 − 这么多"的帧：最早的窗口起点是上一个采样帧再往前 lead / 结尾窗的 1.5 个间隔 + end_lead。"""
        self.anchor = (start_idx, int(round(start_idx * self.us)))   # (帧号, 那一帧的真实 pts)，见文件头"时间"
        self.open: dict[int, Seg] = {}
        self.first_idx: dict[int, int] = {}
        self.last: dict[int, tuple] = {}             # 链 uid -> (行, 框, 文本) 上一个采样点的样子（换句时给旧句开结尾窗）
        self.q: Queue = Queue()
        self.done_seq = -1
        self.done_cv = threading.Condition()
        self.error: BaseException | None = None
        self.on_done = None
        """`(seq) -> None`：每处理完一批就调（在工作线程里）。独立进程版（`edge_proc`）靠它把进度发回管线进程。"""
        self.on_error = None
        """`(exc) -> None`：工作线程抛错时调（同上）。"""
        self.on_progress = None
        """完成一个窗内工作单元时调；慢 CPU 一批未完成仍可报告实际进展。"""
        # `worker_busy_sec` 是线程的墙钟（GIL 之下大半在等主线程），**`worker_cpu_sec` 才是它自己的算力**
        self.stats = {"segments": 0, "on": 0, "off": 0, "off_replaced": 0, "relinked": 0, "jitter": 0,
                      "short_windows": 0, "stale_windows": 0, "gave_up": 0,
                      "frames_pushed": 0, "ring_stalled": 0, "ring_blocked_sec": 0.0,
                      "worker_busy_sec": 0.0, "worker_cpu_sec": 0.0,
                      "worker_wait_sec": 0.0, "signals_sec": 0.0, "end_sec": 0.0, "wait_batch_sec": 0.0}
        # `--refine-aux noop`：空臂（辅助流照收进环、工作线程只放帧不算），量"辅助流那条路"单独多付多少（成本拆分用，不进 config）
        self.noop = noop
        self.th = threading.Thread(target=self._run, daemon=True, name="edge-refine")
        self.th.start()

    # ---------- OCR 主线程这边 ----------
    def sample(self, seq: int, idx: int, t_us: int, items: list[tuple], ended: list[tuple],
               stale: set[int] | tuple = ()) -> None:
        """一个采样点：items = [(链 uid, 行, 框, 文本, "new"|"changed"|"same")]（本帧仍在的框），
        ended = [(链 uid, 末行, 框, 文本, 末次观测帧号)]（本帧没接上的链）。
        `t_us` 是这一帧的**真实 pts**（时间锚，见文件头）；`stale` = 这一批里历史文本被回补改过的链 uid。"""
        self.q.put(("s", seq, idx, t_us, items, ended, set(stale)))

    def settled_by(self, seq: int) -> int:
        """写第 seq 批的行之前要等到哪一批处理完：段最晚在 MAX_OPEN_SAMPLES 批之后停变或放弃、结尾证据在链断那一批挂上，
        所以是 seq + MAX_OPEN_SAMPLES + 1。第一版只等第 seq 批本身，辅助流一落后（NVDEC 路，工作线程比主线程慢几十个采样点）
        行就先被写出去了、证据挂在已落盘的 dict 上——yuka-f5 的 on 行从 465 掉到 299（2026-09-16 深夜）。"""
        return seq + MAX_OPEN_SAMPLES + 1

    def ready(self, seq: int) -> bool:
        """第 seq 批处理完了没有（不等）；出错了也算，让随后的 `wait_batch` 抛出来。"""
        return self.done_seq >= seq or self.error is not None or not self.th.is_alive()

    def wait_batch(self, seq: int) -> None:
        """等到第 seq 批处理完（延迟落盘写那一批的行之前调；调用方给 `settled_by(批号)`）。"""
        t0 = time.perf_counter()
        with self.done_cv:
            while self.done_seq < seq and self.error is None and self.th.is_alive():
                self.done_cv.wait(0.5)
        self.stats["wait_batch_sec"] += _tl.span("wait_batch", t0, a=seq) - t0
        if self.error is not None:
            raise self.error

    def finish(self, last_idx: int) -> dict:
        """流结束：还开着的段按末次采样帧停变，等工作线程清空队列。返回统计。"""
        self.q.put(("f", last_idx))
        self.th.join()
        self.ring.close()
        if self.error is not None:
            raise self.error
        self.stats["frames_pushed"] = self.ring.pushed
        self.stats["ring_stalled"] = int(self.ring.stalled)
        self.stats["frames_below_floor"] = self.ring.dropped
        self.stats["ring_blocked_sec"] = round(self.ring.blocked_sec, 2)
        for k in ("worker_busy_sec", "worker_wait_sec", "signals_sec", "end_sec", "wait_batch_sec"):
            self.stats[k] = round(self.stats[k], 2)
        return self.stats

    def abort(self) -> None:
        self.ring.close()
        self.q.put(("x",))

    # ---------- 工作线程 ----------
    def _run(self) -> None:
        try:
            while True:
                t_idle = time.perf_counter()
                msg = self.q.get()
                t0 = _tl.span("edge_idle", t_idle)
                if msg[0] == "x":
                    return
                if msg[0] == "f":
                    for seg in list(self.open.values()):
                        self._settle(seg, msg[1], seg.box)
                    self.open.clear()
                    self.stats["worker_busy_sec"] += time.perf_counter() - t0
                    return
                _, seq, idx, t_us, items, ended, stale = msg
                c0 = time.thread_time()
                self.anchor = (idx, t_us)                # 时间锚：这一批的证据按这一帧的真实 pts 折算
                if self.noop:                            # 成本拆分用的空臂：辅助流照收、什么都不算
                    self.ring.release(idx - self.keep_back)
                else:
                    self._batch(seq, idx, items, ended, stale)
                self.stats["worker_cpu_sec"] += time.thread_time() - c0
                self.stats["worker_busy_sec"] += _tl.span("edge_batch", t0, a=idx) - t0
                with self.done_cv:
                    self.done_seq = seq
                    self.done_cv.notify_all()
                if self.on_done is not None:
                    self.on_done(seq)
        except BaseException as exc:                     # noqa: BLE001  主线程在 wait_batch / finish 里再抛
            self.error = exc
            self.ring.close()
            with self.done_cv:
                self.done_cv.notify_all()
            if self.on_error is not None:
                self.on_error(exc)

    def _t(self, i: int) -> int:
        """帧号 -> 时间：锚在这一批采样点的真实 pts 上（见文件头"时间"）。"""
        a_idx, a_us = self.anchor
        return int(round(a_us + (i - a_idx) * self.us))

    def _batch(self, seq: int, idx: int, items: list[tuple], ended: list[tuple],
               stale: set[int] = frozenset()) -> None:
        # 链断了、同一行紧接着起了新链（打字机长字让框变宽、IoU 掉到 0.7 以下；或 rec 抖动换了文本）：
        # 按文本判是**长 / 抖**（旧链的状态过继给新链，不开结尾窗、不重开打字机窗）还是**换句**（照常）
        kinds = {}
        left = []
        for key, row, box, text, last_idx in ended:
            heir = next((it for it in items if it[4] == "new" and it[0] not in kinds and line_match(it[2], box)
                         and (is_growth(text, it[3]) or is_jitter(text, it[3]))), None)
            if heir is None:
                left.append((key, row, box, text, last_idx))
                continue
            k2 = heir[0]
            for d in (self.open, self.first_idx, self.last):
                if key in d:
                    d[k2] = d.pop(key)
            self.last.setdefault(k2, (row, box, text))
            kinds[k2] = "changed" if is_growth(text, heir[3]) else "same"
            self.stats["relinked"] += 1
        ended = left
        for key, row, box, text, kind in items:
            kind = kinds.get(key, kind)
            seg = self.open.get(key)
            prev = self.last.get(key)
            self.last[key] = (row, box, text)
            if kind == "changed" and prev is not None and is_jitter(prev[2], text):
                self.stats["jitter"] += 1
                kind = "same"
            if kind == "changed" and prev is not None and not is_growth(prev[2], text):
                # 换句：同一条链上旧句直接被下一句顶掉。旧句的末行要结尾证据（build_tracks 在这里切 run），
                # 打字机在长（新文本以旧文本为前缀）时不用——旧字还在屏幕上
                self._end(key, prev[0], prev[1], prev[2], self.sgrid.prev(idx), key in stale)
                self.stats["off_replaced"] += 1
                if seg is not None:                  # 上一句还没停变就被顶掉：按末次观测帧停变
                    # 作废标记先并进来再停变：`_settle` 只看 `seg.stale`，原来这里漏了，回补改过的旧句照样写出 `on` 证据
                    seg.stale = seg.stale or key in stale
                    self._settle(seg, self.sgrid.prev(idx), prev[1])
                    del self.open[key]
                    seg = None
                self.first_idx[key] = idx
            if kind in ("new", "changed"):
                if kind == "new":
                    self.first_idx[key] = idx
                if seg is None:
                    seg = self._open(key, row, text, box, idx, seq)
                    seg.stale = key in stale          # 回补前移了变化点：这一段的窗起点已经不对（见 reuse_v2.commit）
                    self._capture(seg, seg.lo, idx)
                else:
                    seg.stale = seg.stale or key in stale
                    self._capture(seg, self.sgrid.prev(idx) + 1, idx)
                    seg.text, seg.box, seg.last_seq = text, box, seq
                    if seq - seg.opened_seq > MAX_OPEN_SAMPLES:
                        self.stats["gave_up"] += 1
                        del self.open[key]
            elif seg is not None:                        # 停变：这一个间隔也要，末帧当模板
                seg.stale = seg.stale or key in stale
                self._capture(seg, self.sgrid.prev(idx) + 1, idx)
                self._settle(seg, idx, box)
                del self.open[key]
        for key, row, box, text, last_idx in ended:
            seg = self.open.pop(key, None)
            if seg is not None:                          # 断的时候还在变：模板只能是末次观测帧
                seg.stale = seg.stale or key in stale
                self._settle(seg, last_idx, box)
            self._end(key, row, box, text, last_idx, key in stale)
            self.first_idx.pop(key, None)
            self.last.pop(key, None)
        self.ring.release(idx - self.keep_back)

    def _open(self, key: int, row: dict, text: str, box, idx: int, seq: int) -> Seg:
        y0 = max(0, int(box[1] * self.sy) - 2 * self.pad)
        y1 = min(self.Hs, int(box[3] * self.sy) + 2 * self.pad)
        lo = max(self.start_idx, self.sgrid.prev(idx) - self.lead)
        # 窗口起点对齐到辅助流的网格上（`AuxGrid.floor`）：窗口里的帧号就是网格上的帧、证据重建时（`sig_from_records`）
        # 按同一个网格数得回来；采样帧一定在网格上（嵌套），时间网格才盖住采样级的 t_start——
        # 2026-09-16 那次（step 2、起点 = 采样帧 − 33 是奇数、没对齐）网格全是奇数帧，首字吸附到网格上比 t_start 晚 1 帧，产物校验当场拒收
        lo = self.grid.floor(lo)
        seg = self.open[key] = Seg(key, row, text, box, y0, y1, lo, seq)
        self.stats["segments"] += 1
        return seg

    def _capture(self, seg: Seg, lo: int, hi: int) -> None:
        t0 = time.perf_counter()
        ok = self.ring.wait_for(hi)
        self.stats["worker_wait_sec"] += _tl.span("edge_wait_frame", t0) - t0
        if not ok:
            seg.short = True
        got = self.ring.take(lo, hi)
        if not got:
            seg.short = True
            return
        seg.idxs.extend(k for k, _ in got)
        seg.stacks.append(np.stack([f[seg.y0:seg.y1] for _, f in got]))
        if self.on_progress is not None:
            self.on_progress()

    def _inner(self, seg_y0: int, seg_y1: int, box) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]] | None:
        """最终的框（缩放后）在整行带里的裁剪范围 + 框在裁剪里的位置。"""
        bx0, by0 = max(0, int(box[0] * self.sx)), max(seg_y0, int(box[1] * self.sy))
        bx1, by1 = min(self.Ws, int(box[2] * self.sx)), min(seg_y1, int(box[3] * self.sy))
        if bx1 - bx0 < 4 or by1 - by0 < 4:
            return None
        x0, y0 = max(0, bx0 - self.pad), max(seg_y0, by0 - self.pad)
        x1, y1 = min(self.Ws, bx1 + self.pad), min(seg_y1, by1 + self.pad)
        return (x0, y0 - seg_y0, x1, y1 - seg_y0), (bx0 - x0, by0 - y0, bx1 - x0, by1 - y0)

    def _settle(self, seg: Seg, hi: int, box) -> None:
        """停变：拼起整段裁剪、按最终的框切、跑 signals，结果挂到段首行。"""
        a_idx, a_us = self.anchor
        rec = {"lo": seg.lo, "hi": hi, **self.grid.spec(), "a_idx": a_idx, "a_us": a_us,
               "n": len(TF.chars(seg.text)), "text": seg.text,
               "box": [int(v) for v in box], "first_old": None, "first_rise": None, "full_glyph": None, "cell": None}
        cut = self._inner(seg.y0, seg.y1, box)
        if seg.stale:
            self.stats["stale_windows"] += 1
            rec["stale"] = True
        if seg.short or not seg.stacks or cut is None or seg.idxs[-1] < self.grid.prev(hi):
            self.stats["short_windows"] += 1
            rec["short"] = True
        else:
            (x0, y0, x1, y1), inner = cut
            frames = np.concatenate(seg.stacks)[:, y0:y1, x0:x1]
            ts = [self._t(i) for i in seg.idxs]
            t0 = time.perf_counter()
            sig = TF.signals(frames, ts, inner, seg.text)
            self.stats["signals_sec"] += time.perf_counter() - t0
            if sig is not None:
                rec.update({k: sig[k] for k in ("first_old", "first_rise", "full_glyph")})
                rec["cell"] = [float(sig["cell"][0]), float(sig["cell"][1])] if sig["cell"] else None
        seg.row.setdefault("edge", {})["on"] = rec
        self.stats["on"] += 1
        if self.on_progress is not None:
            self.on_progress()

    def _end(self, key: int, row: dict, box, text: str, last_idx: int, stale: bool = False) -> None:
        """链断了：结尾窗（对比度曲线）+ 退回判据（整框相关系数、模板 = 末次观测帧）。"""
        t_end_idx = self.sgrid.next(last_idx)            # 下一个采样帧（run 的 t_end）
        lo = max(self.first_idx.get(key, last_idx), t_end_idx - int(round(1.5 * (t_end_idx - last_idx))) - self.end_lead)
        lo = self.grid.floor(lo)                          # 同 _open：对齐到辅助流的网格
        hi = self.grid.ceil(t_end_idx + self.tail)
        a_idx, a_us = self.anchor
        rec = {"lo": lo, "hi": hi, **self.grid.spec(), "a_idx": a_idx, "a_us": a_us,
               "n": len(TF.chars(text)), "text": text,
               "last_idx": last_idx, "box": [int(v) for v in box], "ncc_gone": None, "last_full": None, "gone": None}
        if stale:                                         # 回补改写了这一段的历史文本：末行的文本 / 时刻都不再是它量的那个
            self.stats["stale_windows"] += 1
            rec["stale"] = True
        t0 = time.perf_counter()
        full = self.ring.wait_for(hi)
        self.stats["worker_wait_sec"] += _tl.span("edge_wait_frame", t0) - t0
        t0 = time.perf_counter()
        got = self.ring.take(min(lo, last_idx), max(hi, t_end_idx))
        y0 = max(0, int(box[1] * self.sy) - 2 * self.pad)
        y1 = min(self.Hs, int(box[3] * self.sy) + 2 * self.pad)
        cut = self._inner(y0, y1, box)
        if not got or cut is None:
            self.stats["short_windows"] += 1
            rec["short"] = True
        else:
            (cx0, cy0, cx1, cy1), inner = cut
            by = {k: f[y0:y1][cy0:cy1, cx0:cx1] for k, f in got}
            ix0, iy0, ix1, iy1 = inner
            tpl = by.get(last_idx)
            if tpl is not None and tpl[iy0:iy1, ix0:ix1].size >= 64:
                like = TF.ncc_to(tpl[iy0:iy1, ix0:ix1])
                gone = None
                for k in reversed(self.grid.frames(last_idx, t_end_idx)):    # last_true：[末次观测帧, +一个间隔]
                    f = by.get(k)
                    if f is not None and like(f[iy0:iy1, ix0:ix1]) >= self.thr:
                        gone = k
                        break
                rec["ncc_gone"] = gone
            want = self.grid.frames(lo, hi)
            idxs = [k for k in want if k in by]
            if full and idxs and len(idxs) == len(want):     # 网格上的帧一帧不缺（lo / hi 都在网格上）
                frames = np.stack([by[k] for k in idxs])
                last_full, gone_c = TF.end_keyframes(frames, [self._t(k) for k in idxs], inner, text)
                rec["last_full"], rec["gone"] = last_full, gone_c
            else:
                self.stats["short_windows"] += 1
                rec["short"] = True
        row.setdefault("edge", {})["off"] = rec
        self.stats["off"] += 1
        self.stats["end_sec"] += time.perf_counter() - t0
        if self.on_progress is not None:
            self.on_progress()


# ---------- 第二遍（refine_boundaries --decoder obs）这边：把行上的证据重建成 measure() 的输出 ----------

def rec_time(rec: dict, i: int, us_per_frame: float) -> int:
    """证据记录里的帧号 -> 时间：锚在它那一批采样点的真实 pts 上（`a_idx` / `a_us`，见文件头"时间"）。
    没有锚的旧产物退回纯帧号时钟（那是 2026-09-17 之前的口径）。"""
    a_idx, a_us = rec.get("a_idx"), rec.get("a_us")
    if a_idx is None or a_us is None:
        return int(round(i * us_per_frame))
    return int(round(a_us + (i - a_idx) * us_per_frame))


def usable(rec: dict | None) -> bool:
    """这条证据能不能用：窗口凑齐了（不 short），而且没被回补作废（不 stale，见 reuse_v2.commit）。"""
    return bool(rec) and not rec.get("short") and not rec.get("stale")


def sig_from_records(first: dict, last: dict, us_per_frame: float) -> dict | None:
    """段首行的 `edge.on`（首字那一侧）+ 采样级全字之前最后一段的 `edge.on`（全字那一侧）-> typewriter_fuse 的 sig。
    同一段时两者是同一个记录。任一侧 short / stale 就当没量出来。"""
    if not usable(first) or not usable(last):
        return None
    lo, hi = first["lo"], last["hi"]
    if hi < lo:
        return None
    grid = AuxGrid.from_spec(first)            # 等距的记 `step`、不等距的记 `grid`（`AuxGrid.spec`）
    cell = None
    if first.get("cell") and last.get("cell"):
        cell = (first["cell"][0], last["cell"][1])
    return {"ts": [rec_time(first, i, us_per_frame) for i in grid.frames(lo, hi)], "n": last["n"],
            "first_old": first["first_old"], "first_rise": first["first_rise"],
            "full_glyph": last["full_glyph"], "cell": cell}


def measured_from_records(on_first: dict | None, on_last: dict | None, off: dict | None,
                          old_end_us: int, us_per_frame: float, frame_us: int, sgrid: TimeGrid) -> tuple | None:
    """-> refine_boundaries.measure 写进 job["measured"] 的那个五元组 (new_e, ok_gone, sig, last_full, end_how)。
    没有任何证据返回 None（该条原样退回，同 no_evidence）。口径同 measure：曲线给得出"完全消失"就用它，
    末次观测帧之后才算；`last_full` 不早于末次观测帧减一个采样间隔。
    **开放边界**（`end_how = "open"`，2026-09-26 owner 定）：退回判据一路像到链断那个采样点——查找范围
    `[末次观测帧, sgrid.next(末次观测帧)]`（`_end` 里的那个，`sgrid` 是这份 obs 的采样网格）的最后一帧字还在，
    窗里没看到它消失（det 把一行切碎、读数变了另起一条 run，字其实还在屏上）。t_end 照旧取"最后还像的一帧 + 1"，
    但那只是下界；完全显示结束也就不知道，不填。**按网格判、不拿 run 的 t_end 比**：run 的 t_end 是末次观测 + 名义间隔，
    不整除的采样（60 fps 上 2.2 / 2.1 fps）和实际的下一格差一帧，两个方向都会判反（2026-09-26 Codex 复审）。"""
    sig = sig_from_records(on_first, on_last, us_per_frame) if on_first and on_last else None
    new_e, ok_gone, last_full, end_how = old_end_us, False, None, None
    if usable(off):
        e_us = rec_time(off, off["last_idx"], us_per_frame)
        if off.get("ncc_gone") is not None:
            new_e, ok_gone = rec_time(off, off["ncc_gone"] + 1, us_per_frame), True
            end_how = "open" if off["ncc_gone"] >= sgrid.next(off["last_idx"]) else "ncc"
        if off.get("gone") is not None and off["gone"] > e_us:
            new_e, ok_gone, end_how = int(off["gone"]), True, "curve"
        last_full = off.get("last_full")
        if (last_full is not None and last_full < e_us - frame_us) or end_how == "open":
            last_full = None
    if sig is None and not ok_gone and last_full is None:
        return None
    return (new_e, ok_gone, sig, last_full, end_how)
