"""在线回抠放进**自己的进程**：辅助流接收（`framesource.AuxReader`）+ 辅助帧环（`FrameRing`）+ `EdgeRefiner`（`--edge-proc`）。

为什么（decode-buffer 的 S2 第一刀）：同进程时回抠工作线程是 UI 密的素材上的瓶颈，而且是**被 GIL 饿着**的瓶颈——
wuwa-s2 上它忙 24.9 s、自己的 CPU 只有 11.5 s；环放大只把等待从"det 等帧"挪到"落盘等回抠"。它和辅助流收帧都是碰像素的活，
挪出去之后管线进程里少两个抢 GIL 的线程，它自己也有一把整的 GIL。

**算法一行不动**：`EdgeRefiner` 原样跑在这边，只把它和管线进程之间的三样东西换成消息：

* 链事件（`sample`）：行对象换成**行号**（管线进程里 `id(row)`，那边握着行、行活着号就不会被复用）；
* 证据（`_settle` / `_end` 写 `row["edge"]["on" / "off"]`）：这边的行是 `RowRef` 替身，写进去就发 `("ev", 行号, 键, 证据)` 回去，
  管线进程的收信线程挂到真的行上；
* 进度（`done_seq`）：`EdgeRefiner.on_done` 每批发 `("done", 批号)`。**证据先于进度发出、同一条连接按序到达**，
  所以管线进程看到"第 k 批做完"时，第 k 批及以前的证据都已经挂上了——延迟落盘（`wait_batch(settled_by)`）的规矩不用改。

辅助流：ffmpeg 的第二路输出直接 connect 到**这边** listen 的端口（像素不经过管线进程）；它的 pts 在 ffmpeg 的 stderr 里，
只能由管线进程读，所以逐行转发过来（每秒 60 条小消息）。⚠ **别攒批转发**：这边收一帧要先拿到它的 pts，攒着不发的话，
这边不读 TCP -> ffmpeg 写不出 -> 不再吐 stderr 行 -> 批永远攒不满，互等。

进程生命周期走 `childproc`（起子进程、口令、限时等连接、收尾，和只解码进程共用一份）。连接一断（管线进程死了）这边就退出。
"""
from __future__ import annotations

import sys
import threading
import time
import traceback
from queue import Queue

class _Slot:
    """`row["edge"]` 的替身：往里写证据就是发回管线进程。"""
    __slots__ = ("rid", "send")

    def __init__(self, rid: int, send) -> None:
        self.rid, self.send = rid, send

    def __setitem__(self, key: str, rec: dict) -> None:
        self.send(("ev", self.rid, key, rec))


class RowRef:
    """管线进程里一行的替身。`EdgeRefiner` 对行只做 `row.setdefault("edge", {})[键] = 证据` 这一件事（守卫核这一点）。"""
    __slots__ = ("rid", "_slot")

    def __init__(self, rid: int, send) -> None:
        self.rid, self._slot = rid, _Slot(rid, send)

    def setdefault(self, key: str, default=None):
        if key != "edge":
            raise KeyError(f"回抠只该往行里写 edge，碰了 {key!r}")
        return self._slot


def serve() -> int:
    from flowocr.extract import childproc, edge_refine, framesource
    from flowocr.extract import timeline as tl

    conn, send = childproc.connect()
    tl.start_from_env("edge")                        # --timeline：这个进程写 `<路径>.edge.jsonl`
    try:
        cfg = conn.recv()
    except (EOFError, OSError):
        return 0
    if isinstance(cfg, tuple):                        # ("x",)：管线进程起不来 / 不用了（同 decode_proc）
        return 0
    edge = edge_refine.EdgeRefiner(**cfg["edge"])
    if cfg.get("regions"):
        from flowocr.extract import regions
        g = regions.from_spec(cfg["regions"], cfg["W"], cfg["H"])
        if not g.unrestricted:
            edge.ring.premask = regions.aux_masker(g, edge.Ws, edge.Hs, cfg["src_fps"])
    pts_q: Queue = Queue()
    aux = framesource.AuxReader(start_idx=cfg["start_idx"], n_frames=cfg["aux_n"], step=edge.grid.step,
                                width=edge.Ws, height=edge.Hs, sink=edge.ring, pts_q=pts_q,
                                src_fps=cfg["src_fps"]).start()
    edge.on_done = lambda seq: send(("done", seq))
    progress_last = [time.monotonic()]

    def progress():
        # 只由实际完成的工作触发，不用存活心跳掩盖死锁。每秒最多一条。
        now = time.monotonic()
        if now - progress_last[0] >= 1.0:
            progress_last[0] = now
            send(("progress",))

    edge.on_progress = progress
    edge.on_error = lambda exc: send(("err", "".join(traceback.format_exception(exc))))   # 带 traceback：回抠进程里的错管线进程那边要看得见在哪
    # **pts 的第二个入口**（`--decode-shards`）：ffmpeg 跑在解码进程里时，stderr 也在那边读——让它直接连过来送 pts，
    # 不绕管线进程转一道。消息同主连接的 "p" / "pe"
    from multiprocessing.connection import Listener
    import secrets
    feed_key = secrets.token_bytes(16)
    feed = Listener(("127.0.0.1", 0), authkey=feed_key)

    def feeder() -> None:
        try:
            c = feed.accept()
            while True:
                m = c.recv()
                if m[0] == "p":
                    pts_q.put(m[1])
                else:
                    pts_q.put(framesource.QUEUE_SENTINEL)
                    return
        except (EOFError, OSError):
            pts_q.put(framesource.QUEUE_SENTINEL)     # 解码进程没了：辅助流就此停（AuxReader 记 stopped_early）

    threading.Thread(target=feeder, daemon=True, name="pts-feed").start()
    send(("hello", {"url": aux.url, "Ws": edge.Ws, "Hs": edge.Hs, "cap": edge.ring.cap,
                    "pts_addr": [feed.address[1], feed_key.hex()]}))
    refs: dict[int, RowRef] = {}

    def ref(rid: int) -> RowRef:
        r = refs.get(rid)
        if r is None:
            r = refs[rid] = RowRef(rid, send)
        return r

    try:
        while True:
            try:
                msg = conn.recv()
            except (EOFError, OSError):
                edge.abort()                     # 管线进程没了：不留孤儿
                return 1
            tag = msg[0]
            if tag == "p":
                pts_q.put(msg[1])
            elif tag == "pe":
                pts_q.put(framesource.QUEUE_SENTINEL)
            elif tag == "s":
                _, seq, idx, t_us, items, ended, stale, live = msg
                edge.sample(seq, idx, t_us, [(k, ref(rid), box, text, kind) for k, rid, box, text, kind in items],
                            [(k, ref(rid), box, text, li) for k, rid, box, text, li in ended], set(stale))
                if live is not None:             # 替身表跟着管线进程那边的行号表剪（它隔一阵发来仍可能被写的行号）
                    keep = set(live)
                    for rid in [r for r in refs if r not in keep]:
                        del refs[rid]
            elif tag == "f":
                stats = edge.finish(msg[1])
                aux.join()
                stats.update({"aux_sent": aux.sent, "aux_stopped_early": aux.stopped_early, "aux_last_key": aux.last_key,
                              "aux_cpu_sec": round(aux.cpu_sec, 2), "aux_shift": 0,
                              "ring_cap": edge.ring.cap, "proc": True})
                if aux.misaligned:
                    stats["aux_misaligned"] = aux.misaligned
                tl.dump()                                # 在回 fin 之前：B 收到就收摊了
                send(("fin", stats))
                return 0
            elif tag == "x":
                edge.abort()
                return 0
    finally:
        try:
            conn.close()
        except OSError:
            pass


class EdgeProxy:
    """管线进程这边：和 `EdgeRefiner` 同一组接口（`sample` / `settled_by` / `wait_batch` / `finish` / `abort`），活在另一个进程里。"""

    STALL_SEC = 60.0
    """回抠进程多久没前进算卡住：**看进度、不看总等待**，和解码分片（`decode_shards` 的 `STALL_SEC`）同一个判据。
    它落后管线很多批时一直在做（CPU 上、或收尾时积压一大截），等得再久也不算卡；批号和窗内工作进度都这么久不动才算。
    原来是"这一批总共等了 300 s"，积压够长的正常收尾也会被判卡住，真卡住又要白等 5 分钟。"""

    def __init__(self, *, edge_kwargs: dict, start_idx: int, end_idx: int, src_fps: float, width: int, height: int,
                 regions_spec: dict | None = None, start_timeout: float = 120.0, stall_sec: float = STALL_SEC) -> None:
        from types import SimpleNamespace

        from flowocr.extract import childproc

        self.child = childproc.Child("flowocr.extract.edge_proc", "回抠进程", timeout=start_timeout)
        self.proc = self.child.proc
        self.conn = self.child.conn                      # 限时等它连上（连不上就杀掉、抛）
        from flowocr.extract.framegrid import AuxGrid

        self.conn.send({"edge": edge_kwargs, "start_idx": start_idx, "src_fps": src_fps, "W": width, "H": height,
                        "regions": regions_spec, "aux_n": AuxGrid.from_list(edge_kwargs["grid"]).n_frames(start_idx, end_idx)})
        hello = self.conn.recv()
        if hello[0] != "hello":
            raise RuntimeError(f"回抠进程握手失败：{hello!r}")
        h = hello[1]
        self.url, self.Ws, self.Hs = h["url"], h["Ws"], h["Hs"]
        self.pts_addr = h["pts_addr"]
        """`[端口, 口令 hex]`：只解码进程（`--decode-shards`）直接把辅路 pts 送到这里（不经过管线进程）。"""
        self.ring = SimpleNamespace(cap=h["cap"])
        self.noop = bool(edge_kwargs.get("noop"))
        self.stall_sec = stall_sec
        self.rows: dict[int, tuple[dict, int]] = {}     # 行号 -> (行, 最近一次发出去时的批号)
        self.done_seq = -1
        self.work_progress = 0
        self.cv = threading.Condition()
        self.error: str | None = None
        self.stats: dict | None = None
        self.wait_sec = 0.0
        self.last_seq = -1
        self.th = threading.Thread(target=self._recv, daemon=True, name="edge-proxy")
        self.th.start()

    # ---------- 发 ----------
    def _send(self, msg) -> None:
        self.child.send(msg)

    def pts_put(self, item) -> None:
        """ffmpeg stderr 的辅路 pts（`framesource.FfmpegSource` 的 drain 线程逐行调）。"""
        from flowocr.extract.framesource import QUEUE_SENTINEL
        try:
            self._send(("pe",) if item is QUEUE_SENTINEL else ("p", item))
        except OSError:
            pass                                          # 那边已经退了：错误在 wait_batch / finish 里报

    def sample(self, seq: int, idx: int, t_us: int, items: list[tuple], ended: list[tuple],
               stale: set[int] | tuple = ()) -> None:
        its, ens = [], []
        for k, row, box, text, kind in items:
            self.rows[id(row)] = (row, seq)
            its.append((k, id(row), list(box), text, kind))
        for k, row, box, text, li in ended:
            if row is None:
                continue
            self.rows[id(row)] = (row, seq)
            ens.append((k, id(row), list(box), text, li))
        self.last_seq = seq
        live = None
        if seq % 64 == 0:
            # 行号表剪枝：证据最晚落在开段那一行上（段在 MAX_OPEN_SAMPLES 批内停变或放弃），再老的行不会再被写。
            # 按**那边已经做完的批号**剪（它可能落后这边若干批），不按这边刚发的批号
            from flowocr.extract.edge_refine import MAX_OPEN_SAMPLES
            floor = self.done_seq - MAX_OPEN_SAMPLES - 16
            for rid in [r for r, (_, s) in self.rows.items() if s < floor]:
                del self.rows[rid]
            live = list(self.rows)
        self._send(("s", seq, idx, t_us, its, ens, list(stale), live))

    # ---------- 收 ----------
    def _recv(self) -> None:
        try:
            while True:
                msg = self.conn.recv()
                tag = msg[0]
                if tag == "ev":
                    _, rid, key, rec = msg
                    hit = self.rows.get(rid)
                    if hit is None:
                        self.error = f"回抠进程发来的证据落在一行已经剪掉的行上（行号 {rid}、{key}）"
                    else:
                        hit[0].setdefault("edge", {})[key] = rec
                elif tag == "done":
                    with self.cv:
                        self.done_seq = msg[1]
                        self.cv.notify_all()
                elif tag == "progress":
                    with self.cv:
                        self.work_progress += 1
                        self.cv.notify_all()
                elif tag == "err":
                    self.error = f"回抠进程出错：{msg[1]}"
                elif tag == "fin":
                    self.stats = msg[1]
                if tag in ("err", "fin"):
                    with self.cv:
                        self.cv.notify_all()
                    if tag == "fin":
                        return
        except (EOFError, OSError) as exc:
            if self.stats is None and self.error is None:
                self.error = f"回抠进程断开：{exc!r}（退出码 {self.proc.poll()}）"
        finally:
            with self.cv:
                self.cv.notify_all()

    def _check(self) -> None:
        if self.error is not None:
            raise RuntimeError(self.error)

    # ---------- 和 EdgeRefiner 同名的几个 ----------
    @staticmethod
    def settled_by(seq: int) -> int:
        from flowocr.extract.edge_refine import MAX_OPEN_SAMPLES
        return seq + MAX_OPEN_SAMPLES + 1

    def _wait(self, done, what: str) -> None:
        """等到 `done()`；批号和窗内工作都 `stall_sec` 秒没推进才出错。出错 / 断开时 `_recv` 会叫醒这里。"""
        def position():
            return self.done_seq, self.work_progress
        last, t_last = position(), time.perf_counter()
        with self.cv:
            while not done() and self.error is None:
                self.cv.wait(0.5)
                # 完成/失败可能正是这次唤醒的原因；先认领结果，再判超时。
                # 特别是 fin 不改 done_seq，不能在成功到达后覆盖成超时错误。
                if done() or self.error is not None:
                    break
                now = time.perf_counter()
                if position() != last:
                    last, t_last = position(), now
                elif now - t_last > self.stall_sec:
                    self.error = f"{what}：回抠进程 {self.stall_sec:.0f} s 没前进（停在第 {self.done_seq} 批）"

    def ready(self, seq: int) -> bool:
        """同 `EdgeRefiner.ready`。"""
        return self.done_seq >= seq or self.stats is not None or self.error is not None

    def wait_batch(self, seq: int) -> None:
        t0 = time.perf_counter()
        self._wait(lambda: self.done_seq >= seq or self.stats is not None, f"等回抠进程做完第 {seq} 批")
        from flowocr.extract import timeline as tl
        self.wait_sec += tl.span("wait_batch", t0, a=seq) - t0
        self._check()

    FINISH_SEC = 300.0
    """批号追到头之后还要算结尾窗、汇总统计；汇总不保证持续发进度，仍给固定收尾宽限。"""

    def finish(self, last_idx: int) -> dict:
        self._send(("f", last_idx))
        # 两段：先按进度等积压的批做完（和 wait_batch 同一个判据），再给收尾一段固定宽限
        self._wait(lambda: self.done_seq >= self.last_seq or self.stats is not None or not self.th.is_alive(),
                   "等回抠进程做完积压的批")
        if self.error is None:
            self.th.join(self.FINISH_SEC)
            if self.th.is_alive() and self.stats is None:
                self.error = f"回抠进程收尾（结尾窗 / 汇总）超过 {self.FINISH_SEC:.0f} s"
        if self.error is not None or self.stats is None:
            self.child.close(wait=10)                     # 出错也收掉子进程，不留给监督者按进程树收
        self._check()
        if self.stats is None:
            raise RuntimeError("回抠进程没交回统计就断了")
        self.child.close(wait=30)                         # 它发完 fin 就自己退了：告别发不出去也无妨，限时等、不退就杀
        st = dict(self.stats)
        st["wait_batch_sec"] = round(self.wait_sec, 2)    # 管线进程这边等它的时间（那边的 wait_batch 从来没人调）
        return st

    def abort(self) -> None:
        self.child.close(wait=10)


if __name__ == "__main__":
    sys.exit(serve())
