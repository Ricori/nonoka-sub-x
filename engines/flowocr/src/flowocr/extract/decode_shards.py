"""解码分片（`run_ocr2 --decode-shards`，decode-buffer）：几路 ffmpeg 交替解**相邻的时间块**，按帧号顺序交帧。

为什么：硬解之后每一帧（60 fps 的辅助流）都要 ffmpeg 自己的 CUDA 上下文跑一次 `scale_cuda` 核函数；只解码实测负载下一路慢 2 倍、
3 路 −55%，管线里 quick 六段 −4.9%。**病因是推测**（Windows 上不同进程的 CUDA 上下文分时、小核排在 det / rec 后面），
按上下文的 GPU 时间线没量过；分片有效是实测。

形状（原型 a1-mask-reuse 实验里的 `shard_proto.py` 量过）：

* **块**：窗口按约 `block_sec` 切，块边界对齐**关键帧**（`ffprobe` 只读包头，**边探边切**——整片 f4 探完要 41 s，起流不能等它），
  所以每块从关键帧起解、不多解一帧。第一块从窗口起点走普通路径（文件头有前摇的 av1 要 `-ignore_editlist` 平移，§8.6）；
  剩下不到半块时不再切（末块并进上一块，免得为几帧起一个 ffmpeg）。**尾部按路数均分**：剩下的不到"路数 × 块长"时，
  块长缩成"剩下的 / 路数"（每切一块重算一次，所以越切越短；不短于 `TAIL_MIN_SEC`），免得最后一块只有一路在解、其余路空着；窗口不到两块长时也照样切开。每块一个 `FfmpegSource(block_seek=…)`：
  身份核对（showinfo 的 `n` / pts 帧号 / 帧号唯一 / 硬解的格子核对）**原样逐块做**；块和块之间不重不漏靠帧号区间 `[lo, hi)`。
* **几路"谁空谁领下一块"**：每路各自选硬解（`hw`）或软解（`sw`，CPU 那一路）；快慢自动配平。
* **按序、边解边交**：同时在解 / 解完没交完的块最多 `ahead` 个（默认 = 路数 + 1：尾部均分后某一路先解完时不用干等在途名额，多出的那一块受字节预算管）；当前那一块**解出一帧就交一帧**。
  采样帧按块序从 `__iter__` 交出，辅助流由转发线程按块序写给回抠进程（它看到的和一路 ffmpeg 直连时一样：一条 TCP 流 + 逐帧 pts）。
* **内存按字节封顶**（2026-09-24 Codex 审计 P2：块数有界不等于内存有界——长 GOP 能切出 150 s 的块、4K 上一块 20 s 就是 2 GB）：
  **没有消费者在等**的块，各路往里放东西前先看总缓冲过没过 `budget_mb`，过了就停着（ffmpeg 跟着阻塞在管道上）。
  **有消费者在等的块永远放行**——于是不会两路互等：等着的那一路总有东西来，它拿走之后缓冲就降下去。
  代价：超预算之后预取变浅，几路并行的好处打折；上限约是预算 + 消费者等待时顺带解出来的那几帧。
* **每块收尾核一次这一块的尾巴**：块自己核了"块起点 -> 最后一帧"之间的缺格，块尾（最后一帧 -> 块终点）由这里核——
  整块一帧没交（Codex 审计 P1：首块空时原来整段放过去、`complete` 照样为真）也落在这一步。缺格问 `probe_source_frames`：
  源里有却没交付 = 解码出错（硬解那路抛 `HwaccelMismatch` -> 整段改软解重跑；软解那路抛 RuntimeError）；源里没有 = 素材缺口、放行；
  探不出来：硬解那路保守当吞帧，软解那路放行、记 `probe_unknown`。
  最后一块的块尾只核"源里有没有"、不计缺格（单路也不数窗口尾，那一截归覆盖率那道门）。
* **失败一处、全停**：块自己的错按块序抛（同单路）；辅助流转发自己的错（连不上 / 写不进 / pts 送不出）和规划失败进**共享的失败状态**，
  唤醒所有等待方、停掉在解的 ffmpeg，由 `__iter__` 抛出（原来转发的错被吞掉：块数 ≤ 路数时无声少了辅助流，多一块就永远卡住）。
  辅助流少交（半路断了 / 不被预算挡着却 60 s 没前进 / pts 对不上）也是这一块的错，不当成"读完了"。

没 GPU 时由调用方把各路都换成 `sw`（owner：每个操作都要有能跑的 CPU 版，不求调度一样）。
"""
from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Iterable, Iterator

from flowocr.extract import framesource
from flowocr.extract import timeline as _tl
from flowocr.extract.aux_transport import TcpAux
from flowocr.extract.framegrid import AuxGrid, TimeGrid

TAIL_MIN_SEC = 10.0      # 尾部均分时块长的下限：再短，每块起 ffmpeg + 从前一个关键帧预解的开销（约 0.3 s）就不划算

BUDGET_MB = 1024
"""纯预取（没有消费者在等的块）最多攒这么多字节。1080p60、采样 2 fps + 0.35 辅助流约 28 MB / 秒素材，即约 37 s。"""


class KeyframeStream:
    """`[t0, t1)` 里关键帧的 pts 秒，**边读边给**（ffprobe 只读包头，不解码）。只认递增的（解码序里偶有回头的不算）。
    `close()` 杀掉 ffprobe；读完后 `rc` 是它的退出码。"""

    def __init__(self, video: str, t0: float, t1: float) -> None:
        import tempfile

        self.video, self.t0, self.t1 = video, t0, t1
        self.rc: int | None = None
        self.err = tempfile.TemporaryFile()           # stderr 落临时文件（管道不读会满；DEVNULL 会把出错原因丢掉）
        self.proc = subprocess.Popen(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals", framesource.probe_interval(t0, t1),
             "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", video],
            stdout=subprocess.PIPE, stderr=self.err, text=True)

    def __iter__(self) -> Iterator[float]:
        last = None
        for ln in self.proc.stdout:                 # type: ignore[union-attr]
            parts = ln.strip().split(",")
            if len(parts) < 2 or "K" not in parts[1]:
                continue
            try:
                t = float(parts[0])
            except ValueError:
                continue
            if self.t0 <= t < self.t1 and (last is None or t > last):
                last = t
                yield t
        self.rc = self.proc.wait()

    def stderr_tail(self) -> str:
        self.err.seek(0)
        return self.err.read().decode("utf-8", "replace").strip()[-300:]

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def iter_blocks(kfs: Iterable[float], start_idx: int, end_idx: int, src_fps: float,
                block_sec: float, lanes: int = 1) -> Iterator[tuple[int, int, float | None]]:
    """`(lo, hi, 关键帧秒或 None)`，关键帧来一个判一个（**纯函数**，守卫直接测）：第一块从窗口起点起（None = 普通路径），
    之后在"上一块起点 + 块长"之后的第一个关键帧处切——**切完剩下的不到半块就不切**（末块并进上一块）。
    块长平时是 block_sec；剩下的不到 `lanes` 块时缩成"剩下的 / lanes"（每块重算，300 s 窗口两路是 60×4 + 30 + 16 + 14；
    不短于 `TAIL_MIN_SEC`，block_sec 不到它的两倍时不短于半块），先解完的那一路接着领下一小块；`lanes=1` 时就是 block_sec。
    关键帧秒加 0.5 ms 给 `-ss`：seek 找的是"不晚于目标"的关键帧，ffprobe 印出来的秒数往下舍入时会落到前一个关键帧上（多解一段，结果仍对）。"""
    span = block_sec * src_fps
    floor = min(TAIL_MIN_SEC * src_fps, span / 2)
    lo, seek = start_idx, None
    for kf in kfs:
        k = round(kf * src_fps)
        want = min(span, max(floor, (end_idx - lo) / lanes))
        if lo + want <= k and k + max(want / 2, floor) <= end_idx:
            yield lo, k, seek
            lo, seek = k, kf + 0.0005
    yield lo, end_idx, seek


def plan_blocks(video: str, start_idx: int, end_idx: int, src_fps: float, block_sec: float,
                kfs: list[float] | None = None, lanes: int = 1) -> list[tuple[int, int, float | None]]:
    """整张块表（给了 `kfs` 就不探）。管线里用 `BlockPlan`（边探边给）。"""
    if kfs is None:
        ks = KeyframeStream(video, start_idx / src_fps, end_idx / src_fps)
        try:
            return list(iter_blocks(ks, start_idx, end_idx, src_fps, block_sec, lanes))
        finally:
            ks.close()
    return list(iter_blocks(kfs, start_idx, end_idx, src_fps, block_sec, lanes))


class BlockPlan:
    """管线用的块表：ffprobe 边探关键帧、边吐块（`ShardedSource` 的规划线程读它）。`close()` 停掉 ffprobe。"""

    def __init__(self, video: str, start_idx: int, end_idx: int, src_fps: float, block_sec: float, lanes: int = 1) -> None:
        self.ks = KeyframeStream(video, start_idx / src_fps, end_idx / src_fps)
        self.args = (start_idx, end_idx, src_fps, block_sec, lanes)
        self.error = ""                              # 非空 = ffprobe 出错、后面没切开的部分由一路整段解（进 `_meta.decode_shards.plan_error`）

    def __iter__(self) -> Iterator[tuple[int, int, float | None]]:
        yield from iter_blocks(self.ks, *self.args)
        if self.ks.rc:
            self.error = f"ffprobe 探关键帧退出码 {self.ks.rc}：{self.ks.stderr_tail()}"
            print(f"[解码分片] ⚠ {self.error}——后面没切开的部分由一路整段解", flush=True)

    def close(self) -> None:
        self.ks.close()


def _nbytes(item) -> int:
    return getattr(item[-1], "nbytes", 0)


class _Block:
    """一块的交付状态：两路各自按到达顺序追加，`done` 之后不再长；读的人按下标往后拿（`ShardedSource` 的条件变量通知）。"""

    def __init__(self, owner: "ShardedSource", k: int, kind: str, hi: int = 1 << 62) -> None:
        self.owner, self.k, self.kind, self.hi = owner, k, kind, hi
        self.main: list = []
        self.aux: list = []
        self.main_done = self.aux_done = False
        self.read_main = self.read_aux = False
        self.err: BaseException | None = None
        self.src = None
        self.nbytes = 0                 # 这一块还攒着没被拿走的字节
        self.waiting = 0                # 正在等这一块的消费者数（> 0 时各路往里放不受预算限制）
        self.last_main: int | None = None
        self.last_aux: int | None = None
        self.throttled = False          # 有一路正被字节预算挡着（它不前进是在等下游，不算卡住）

    # `AuxReader` 的 sink 接口：辅助流一帧一帧推进来
    def push(self, key, frame) -> None:
        o = self.owner
        with o._cv:
            o._admit(self, getattr(frame, "nbytes", 0), aux=True)
            self.aux.append((key, frame))
            self.last_aux = key
            o._cv.notify_all()

    def close(self) -> None:
        with self.owner._cv:
            self.aux_done = True
            self.owner._cv.notify_all()


class ShardedSource:
    """和 `FfmpegSource` 同接口的取帧层：`__iter__` 按帧号顺序吐 `(帧号, 时刻, BGR 帧)`，结束后 `stopped_early` / `advanced` /
    `grid_off` / `grid_skipped` / `stderr_tail` 有效。`aux_remote`（回抠进程的 `url` + `pts_put`）给了就按块序转发辅助流。
    `blocks`：块表（列表），或边探边给的可迭代对象（`BlockPlan`，有 `close` 的话收摊时调）。"""

    def __init__(self, video: str, *, lanes: list[str], blocks: Iterable[tuple[int, int, float | None]],
                 start_idx: int, end_idx: int, grid: TimeGrid, src_fps: float, width: int, height: int,
                 pix: str = "nv12", select_by: str = "pts", hwaccel: str | None = None, aux_remote=None,
                 aux_grid: list[int] | None = None, aux_scale: float = 0.35, ahead: int | None = None,
                 budget_mb: float = BUDGET_MB) -> None:
        if not lanes or any(k not in ("hw", "sw") for k in lanes):
            raise ValueError(f"解码分片的路只认 hw / sw：{lanes}")
        if "hw" in lanes and not hwaccel:
            raise ValueError("有 hw 路却没给 hwaccel（没 GPU 时调用方要把各路换成 sw）")
        if select_by != "pts":
            raise ValueError("解码分片只配按 pts 选帧（块和块之间按帧号区间接缝）")
        self.video, self.lanes, self.grid, self.src_fps = video, lanes, grid, src_fps
        self.W, self.H, self.pix, self.select_by, self.hw = width, height, pix, select_by, hwaccel
        # 辅助流取哪些帧（`AuxGrid.as_list()`；没给 = 每帧都要）
        self.aux_grid = AuxGrid.from_list(aux_grid) if aux_grid else AuxGrid(TimeGrid(1, 1), grid)
        self.aux_remote, self.aux_scale = aux_remote, aux_scale
        self.ahead = max(1, ahead or len(lanes) + 1)
        self.budget = int(budget_mb * (1 << 20))
        self.start_idx, self.end_idx = start_idx, end_idx
        self.hwaccel = hwaccel if "hw" in lanes else None
        self.preroll_ticks = 0
        self.aux = None                                  # 辅助流不挂在这边（转发给回抠进程）
        self.stopped_early = False
        self.advanced = 0
        self.grid_off: dict[int, int] = {}
        self.grid_skipped = 0
        self.stderr_tail: list[str] = []
        self.lane_blocks = [0] * len(lanes)
        self.lane_sec = [0.0] * len(lanes)
        self.peak_mb = 0.0                               # 缓冲的峰值（报表用）
        self.probe_unknown = 0                           # 软解路块尾缺格、源探不出来、放行了的次数
        self._cv = threading.Condition()
        self._close_lock = threading.Lock()
        if isinstance(blocks, list):
            self.blocks, self._plan, self._planned = list(blocks), None, True
        else:
            self.blocks, self._plan, self._planned = [], blocks, False
        self._blk: dict[int, _Block] = {}                # 块号 -> 交付状态（领到时建，两路都读完时删）
        self._next = 0
        self._sem = threading.Semaphore(self.ahead)
        self._closed = False
        self._fail: BaseException | None = None          # 共享的失败状态（转发 / 规划的错；块自己的错在 `_Block.err`）
        self._buffered = 0
        self._live: dict[int, framesource.FfmpegSource] = {}
        self._aux_writer = None                         # 登记在 open / 网络 IO 之前，取消可立即找到它
        self._threads: list[threading.Thread] = []
        self._cleanup_errors: list[str] = []

    @property
    def plan_error(self) -> str:
        return getattr(self._plan, "error", "")

    def describe(self) -> str:
        n = f"{len(self.blocks)} 块" if self._planned else "边探关键帧边切块"
        return (f"解码分片：{n}、{len(self.lanes)} 路（{'/'.join(self.lanes)}）、最多 {self.ahead} 块在途、"
                f"预取封顶 {self.budget >> 20} MB、边解边交" + ("、辅助流按块序转发给回抠进程" if self.aux_remote is not None else ""))

    # ---------- 共享状态（调用方持锁的标 `# 持锁`） ----------
    def _block_ready(self, k: int) -> bool:
        """块 k 规划出来了没有（持锁；等规划线程）。规划完了还没有 / 收摊 / 已失败 = False。"""
        while k >= len(self.blocks) and not self._planned:
            if self._closed or self._fail is not None:
                return False
            self._cv.wait(0.5)
        return k < len(self.blocks) and not self._closed and self._fail is None

    def _admit(self, b: _Block, n: int, aux: bool = False) -> None:
        """往块 b 里放 n 字节之前（持锁）：没人在等 b、而总缓冲会过预算，就等。
        **主流已经交到这块最后一个格**时不挡（2026-09-24 审核第三轮）：那之后 `FfmpegSource` 会自己收摊（关管道、等 10 s、不退就杀），
        ffmpeg 还没写完的辅助帧要是被预算挡在这里，它就卡在写上被杀掉、辅助流真的被截断。块尾这几帧（不到一个采样间隔）不是预取。"""
        def main_eof() -> bool:                     # 每轮重算：等的时候主流可能正好交到块尾（审计 2026-09-25，Codex P1）
            return b.last_main is not None and self.grid.next(b.last_main) >= b.hi
        def dead() -> bool:                         # 这一块已失败，或辅助读端已被取消：不再等预算，让写入方退出
            return b.err is not None or (aux and b.aux_done)
        while (self._buffered + n > self.budget and b.waiting == 0 and not main_eof()
               and not self._closed and self._fail is None and not dead()):
            b.throttled = True
            self._cv.wait(0.5)
        b.throttled = False
        if self._closed or self._fail is not None:
            raise framesource.SinkClosed("解码分片已收摊，不再接纳像素")
        if dead():
            raise framesource.SinkClosed(f"第 {b.k} 块已失败或取消，不再接纳像素")
        self._buffered += n
        b.nbytes += n
        self.peak_mb = max(self.peak_mb, self._buffered / (1 << 20))

    def _set_fail(self, exc: BaseException) -> None:
        with self._cv:
            if self._fail is None:
                self._fail = exc
            self._cv.notify_all()
        self._stop_live()

    def _abort_aux(self) -> None:
        with self._cv:
            writer = self._aux_writer
        target = writer if writer is not None else self.aux_remote
        if target is not None:
            target.abort()

    def _stop_live(self) -> None:
        try:
            self._abort_aux()
        except BaseException as exc:                    # 仍须取消所有源；错误由主路 / close 交回调用方
            self._remember_cleanup(f"辅助写端取消失败：{exc!r}")
        with self._cv:
            sources = list(self._live.values())
        for src in sources:
            try:
                src.cancel()
            except BaseException as exc:
                self._remember_cleanup(f"解码源取消失败：{exc!r}")

    def _remember_cleanup(self, message: str) -> None:
        """清理错误只登记、由 close() 交回调用方；不升格为共享失败，免得盖掉按块序该抛的根因（例如硬解失败）。"""
        with self._cv:
            if message not in self._cleanup_errors:
                self._cleanup_errors.append(message)
            self._cv.notify_all()

    # ---------- 规划 ----------
    def _planner(self) -> None:
        """把边探边给的块表搬进 `self.blocks`，顺带核"首尾相接、盖满窗口"（不满足就是规划错，不往下解）。"""
        try:
            prev_hi = self.start_idx
            for lo, hi, seek in self._plan:             # type: ignore[union-attr]
                if lo != prev_hi or hi <= lo:
                    raise RuntimeError(f"块表不连续：上一块到 {prev_hi}，这一块 [{lo}, {hi})")
                prev_hi = hi
                with self._cv:
                    if self._closed or self._fail is not None:
                        return
                    self.blocks.append((lo, hi, seek))
                    self._cv.notify_all()
            if prev_hi != self.end_idx:
                raise RuntimeError(f"块表只盖到 {prev_hi}，窗口到 {self.end_idx}")
        except BaseException as exc:                    # noqa: BLE001
            self._set_fail(RuntimeError(f"解码分片的块规划失败：{exc!r}"))
        finally:
            with self._cv:
                self._planned = True
                self._cv.notify_all()

    # ---------- 各路 ----------
    def _lane(self, i: int, kind: str) -> None:
        while True:
            self._sem.acquire()
            with self._cv:
                k = self._next
                self._next += 1                          # 先占号再等规划：几路不会领到同一块
                if not self._block_ready(k):
                    self._sem.release()
                    return
                lo, hi, seek = self.blocks[k]
                b = self._blk[k] = _Block(self, k, kind, hi)
                if self.aux_remote is None:
                    b.aux_done = b.read_aux = True
                self._cv.notify_all()
            t0 = time.perf_counter()
            try:
                src = framesource.FfmpegSource(
                    self.video, grid=self.grid, start_idx=lo, end_idx=hi, src_fps=self.src_fps,
                    width=self.W, height=self.H, pix=self.pix, select_by=self.select_by,
                    hwaccel=self.hw if kind == "hw" else None, block_seek=seek, block=True,
                    preroll_ticks=None if seek is None else 0,
                    aux=({"sink": b, "grid": self.aux_grid.as_list(), "scale": self.aux_scale}
                         if self.aux_remote is not None else None))
                b.src = src
                with self._cv:                           # 登记与取消共享锁；cancel 还阻止登记后才启动的 ffmpeg
                    cancelled = self._closed or self._fail is not None
                    if not cancelled:
                        self._live[i] = src
                if cancelled:
                    src.cancel()
                    raise RuntimeError(f"第 {k} 块没开解：已经收摊")
                for item in src:
                    with self._cv:
                        self._admit(b, _nbytes(item))
                        b.main.append(item)
                        b.last_main = item[0]
                        self._cv.notify_all()
                if self.aux_remote is not None and src.aux is not None:
                    self._join_aux(k, b, src.aux)
                    if src.aux.refused:
                        raise RuntimeError(f"第 {k} 块的辅助流写入被拒：{src.aux.refused}")
                    if src.aux.misaligned:
                        raise RuntimeError(f"第 {k} 块的辅助流 pts 对不上：{src.aux.misaligned}")
                    if b.last_main is not None and (b.last_aux is None or b.last_aux < self.aux_grid.cover_floor(b.last_main)):
                        # 同一个 ffmpeg 出两路：辅助流至少要盖到这一块最后一个采样帧（`AuxGrid.cover_floor`）
                        raise RuntimeError(f"第 {k} 块的辅助流只到帧 {b.last_aux}、采样流到了 {b.last_main}（辅助流半路断了）")
            except BaseException as exc:                # noqa: BLE001  交给读的人按块序抛
                with self._cv:                          # 叫醒被预算挡在 _admit 里的辅助读线程，下面的 cancel 才 join 得到它
                    b.err = exc
                    self._cv.notify_all()
            finally:
                try:
                    if b.err is not None and b.src is not None:
                        b.src.cancel()
                except BaseException as exc:
                    self._remember_cleanup(f"第 {k} 块失败后的清理失败：{exc!r}")
                finally:
                    with self._cv:
                        self._live.pop(i, None)
            dt = _tl.span("shard_block", t0, a=k) - t0
            with self._cv:
                self.lane_blocks[i] += 1
                self.lane_sec[i] += dt
                b.main_done = True
                if b.err is not None:
                    b.aux_done = True
                self._cv.notify_all()

    STALL_SEC = 60.0

    def _join_aux(self, k: int, b: _Block, aux) -> None:
        """等这一块的辅助流收完（ffmpeg 这时已经退了，只剩 socket 里的）。**不拿固定超时当正确性信号**（2026-09-24 审核）：
        被字节预算挡着是在等下游（回抠进程 / 管线慢，CPU 冒烟时一块能等几分钟），只有**不被挡、又 STALL_SEC 秒没前进**才算卡住。"""
        last, t_last = b.last_aux, time.perf_counter()
        while aux.th.is_alive():
            aux.join(1.0)
            if self._closed or self._fail is not None:
                return
            now = time.perf_counter()
            if b.last_aux != last or b.throttled:
                last, t_last = b.last_aux, now
            elif now - t_last > self.STALL_SEC:
                raise RuntimeError(f"第 {k} 块的辅助流 {self.STALL_SEC:.0f} s 没前进（ffmpeg 已退，停在帧 {b.last_aux}）")

    def _items(self, k: int, which: str):
        """块 k 的一路（`main` / `aux`）按到达顺序往外拿，拿过的位置置空（不留内存）；块出错 / 共享失败就抛。"""
        t0 = time.perf_counter()
        with self._cv:
            while k not in self._blk:
                if self._fail is not None:
                    raise self._fail
                if self._closed:
                    return
                self._cv.wait(0.5)
            b = self._blk[k]
        _tl.span("shard_wait", t0, a=k)
        seq = b.main if which == "main" else b.aux
        j = 0
        while True:
            with self._cv:
                while True:
                    if self._fail is not None:           # 先报根因：共享失败会停掉在解的 ffmpeg，那几块跟着出的错是次生的
                        raise self._fail
                    if self._closed:
                        return
                    if b.err is not None:
                        raise b.err
                    if j < len(seq):
                        item, seq[j] = seq[j], None
                        n = _nbytes(item)
                        self._buffered -= n
                        b.nbytes -= n
                        self._cv.notify_all()            # 缓冲降了：被预算挡着的那几路可以接着放
                        break
                    if b.main_done if which == "main" else b.aux_done:
                        return
                    b.waiting += 1
                    self._cv.notify_all()           # 被预算挡着的那一路：有人在等你了（不等它 0.5 s 轮询）
                    self._cv.wait(0.5)
                    b.waiting -= 1
            j += 1
            yield item

    def _release(self, k: int, which: str) -> None:
        """块 k 的一路读完了；两路都读完就把块删掉、放出一个名额给下一块。"""
        with self._cv:
            b = self._blk.get(k)
            if b is None:
                return
            if which == "main":
                b.read_main = True
            else:
                b.read_aux = True
            if b.read_main and b.read_aux:
                self._buffered -= b.nbytes
                del self._blk[k]
                self._sem.release()
                self._cv.notify_all()

    # ---------- 辅助流转发（按块序） ----------
    def _forward(self) -> None:
        n, k = 0, 0
        w = None
        drained = False
        try:
            try:
                w = self.aux_remote if hasattr(self.aux_remote, "open") else TcpAux(self.aux_remote)
                with self._cv:
                    self._aux_writer = w
                    cancelled = self._closed or self._fail is not None
                if cancelled:
                    return
                w.open()
            except OSError as exc:                       # 含连接中途被 abort（`OwnedSocket.connect` 转成 OSError）
                with self._cv:
                    cancelled = self._closed or self._fail is not None
                if not cancelled:
                    self._set_fail(RuntimeError(f"辅助流转发连不上回抠进程：{exc!r}"))
                return
            while True:
                with self._cv:
                    if not self._block_ready(k):
                        drained = not self._closed and self._fail is None and self._planned and k >= len(self.blocks)
                        return
                it = self._items(k, "aux")
                while True:
                    try:
                        key, frame = next(it)
                    except StopIteration:
                        break
                    except BaseException:               # noqa: BLE001  块自己的错 / 已经失败：主路按块序抛同一个
                        return
                    try:
                        w.send(frame, n, key / self.src_fps)
                    except BaseException as exc:        # noqa: BLE001  转发自己的错：主路不知道，必须进共享失败状态
                        self._set_fail(RuntimeError(f"辅助流转发失败（第 {k} 块、第 {n} 帧）：{exc!r}"))
                        return
                    n += 1
                self._release(k, "aux")
                k += 1
        finally:
            if w is not None:
                try:
                    if drained:
                        w.close()
                    else:
                        w.abort()
                except BaseException as exc:            # noqa: BLE001  正常尾标记失败也必须拒绝完成
                    self._set_fail(RuntimeError(f"辅助流结束交付失败：{exc!r}"))

    # ---------- 块尾核对 ----------
    def _check_tail(self, k: int, is_last: bool) -> None:
        """块 k 的"最后一帧 -> 块终点"之间（整块没交就是整块）有没有该交没交的格。块自己核过起点到最后一帧，这里不重数。"""
        lo, hi, _ = self.blocks[k]
        b = self._blk[k]
        frm = lo if b.last_main is None else b.last_main + 1
        tail = self.grid.frames(frm, hi - 1)
        if not tail:
            return
        eaten = framesource.probe_source_frames(self.video, tail, self.src_fps)
        if eaten is None:                                # 探不出来：硬解路保守（当吞帧、整段退软解），软解路放行 + 记一笔
            if b.kind == "hw":
                eaten = tail
            else:
                self.probe_unknown += 1
                eaten = []
        if eaten:
            what = (f"第 {k} 块（{b.kind}）源里有、却没交付的格 {len(eaten)} 个（首个帧 {eaten[0]}；"
                    f"这一块交到 {b.last_main}，块是 [{lo}, {hi})）")
            if b.kind == "hw":
                raise framesource.HwaccelMismatch(f"解码分片：{what}")
            raise RuntimeError(f"解码分片：{what}。ffmpeg 最后几行：{list(b.src.stderr_tail) if b.src else []}")
        if not is_last:                                  # 窗口尾那一截同单路：不数缺格，归覆盖率那道门
            self.grid_skipped += len(tail)

    # ---------- 消费者 ----------
    def __iter__(self):
        ths = [threading.Thread(target=self._lane, args=(i, k), daemon=True, name=f"shard-{i}-{k}")
               for i, k in enumerate(self.lanes)]
        if self._plan is not None:
            ths.append(threading.Thread(target=self._planner, daemon=True, name="shard-plan"))
        fwd = None
        if self.aux_remote is not None:
            fwd = threading.Thread(target=self._forward, daemon=True, name="shard-aux-fwd")
            ths.append(fwd)
        with self._cv:
            if self._closed:
                return
            self._threads = ths
            for th in ths:
                th.start()
        prev_idx: int | None = None
        try:
            k = 0
            while True:
                with self._cv:
                    if not self._block_ready(k):
                        break
                for idx, t, frame in self._items(k, "main"):
                    if prev_idx is not None and idx <= prev_idx:
                        raise RuntimeError(f"解码分片的帧号不单调：第 {k} 块的帧 {idx} 不大于上一帧 {prev_idx}（块边界重叠）")
                    prev_idx = idx
                    self.advanced = self.grid.next(idx) - self.start_idx
                    yield idx, t, frame
                with self._cv:
                    if self._fail is not None:
                        raise self._fail
                    if self._closed:
                        break
                    is_last = not self._block_ready(k + 1)
                    if self._fail is not None:
                        raise self._fail
                self._check_tail(k, is_last)
                src = self._blk[k].src
                for off, c in src.grid_off.items():
                    self.grid_off[off] = self.grid_off.get(off, 0) + c
                self.grid_skipped += src.grid_skipped
                if is_last:                                  # 同单路：读到头之前断了。中间块按 -t 收口、有缺口时凑不满数，不算
                    self.stopped_early = src.stopped_early
                self.stderr_tail = (self.stderr_tail + list(src.stderr_tail))[-8:]
                if k == 0:
                    self.preroll_ticks = src.preroll_ticks
                self._release(k, "main")
                k += 1
            with self._cv:
                if self._fail is not None:
                    raise self._fail
            if fwd is not None:
                # 辅助流转发完再收摊（回抠进程要读到 EOF 和 pts 的结束标记）；转发线程自己出错会进共享失败状态
                while fwd.is_alive():
                    fwd.join(1.0)
                if self._fail is not None:
                    raise self._fail
        except BaseException:
            # 已有异常在传播（含消费者提前 close 的 GeneratorExit）：只收摊，清理错误留给调用方之后的 close() 报，
            # 不能拿它替换原异常——硬解失败要原样交到 decode_proc，才会发 "hw" 走软解重跑
            with self._close_lock:
                self._close()
            raise
        self.close()

    def close(self) -> None:
        """收摊：不再领新块，正在解的那几块的 ffmpeg 收掉，边探的 ffprobe 停掉。"""
        # recv 与主路可以同时取消；后来的 close 必须等前一次清理登记完错误，
        # 不能仅凭源已从 _live 移除就宣告成功。工作线程不调用 close。
        with self._close_lock:
            self._close()
        if self._cleanup_errors:
            raise RuntimeError("解码分片清理失败：" + "；".join(self._cleanup_errors)) from self._fail

    def _close(self) -> None:
        with self._cv:
            self._closed = True
            for b in self._blk.values():
                b.main.clear()
                b.aux.clear()
                b.nbytes = 0
            self._buffered = 0
            self._cv.notify_all()
        self._stop_live()
        closer = getattr(self._plan, "close", None)
        if closer is not None:
            try:
                closer()
            except BaseException as exc:
                self._remember_cleanup(f"块规划器取消失败：{exc!r}")
        for _ in range(len(self.lanes) + 1):
            self._sem.release()
        deadline = time.monotonic() + 10
        for th in self._threads:
            if th is not threading.current_thread():
                th.join(max(0, deadline - time.monotonic()))
        if any(th.is_alive() and th is not threading.current_thread() for th in self._threads):
            self._remember_cleanup("解码分片取消后仍有工作线程未退出")
