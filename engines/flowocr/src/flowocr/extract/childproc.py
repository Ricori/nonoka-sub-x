"""拆进程的共用胶水（2026-09-24 审计：原来 `EdgeProxy` / `FrontProxy` / `RemoteDet` 和两个 `serve` 各写一份）。

管线进程这边（`Child`）：本机端口上 `Listener` 等、`subprocess` 起 `python -m <模块> <端口>`（不用 multiprocessing 的 spawn——
那会在子进程里重新 import 管线主模块、把推理库拉起来）；口令走环境变量；**限时**等它连上（`accept` 没有超时参数，放线程里等）；
发送加锁；收尾一律"发告别 -> 限时等它退 -> 杀"。子进程那边（`connect`）按口令连回来，拿到连接和加了锁的 `send`。

各进程只写自己的消息协议（回抠：`edge_proc`；只解码：`decode_proc`）。子进程出错的约定：发 `("hw", 原因)`（硬解交付不对，
管线进程原样抛 `HwaccelMismatch` -> 外层监督者整段改软解）或 `("err", 回溯)`；`Child.recv_checked` 把这两种换成异常。
"""
from __future__ import annotations

import os
import sys
import threading
import time

AUTH_ENV = "FLOWOCR_CHILD_KEY"


class Child:
    """起一个子进程、等它连回来。`timeout` 是连上的时限（子进程 import 的时间在里面）。
    构造不等连接（起动可以和管线进程自己的起动并行）；第一次用 `conn` 时才等。"""

    def __init__(self, module: str, what: str, timeout: float = 120.0) -> None:
        import secrets
        import subprocess
        from multiprocessing.connection import Listener

        from flowocr.paths import child_env

        key = secrets.token_bytes(16)
        self.what, self.timeout = what, timeout
        self.lst = Listener(("127.0.0.1", 0), authkey=key)
        self.proc = subprocess.Popen([sys.executable, "-m", module, str(self.lst.address[1])],
                                     env=child_env(dict(os.environ, **{AUTH_ENV: key.hex()})))
        self._got: list = []
        self._accept = threading.Thread(target=lambda: self._got.append(self.lst.accept()), daemon=True,
                                        name=f"{module.rsplit('.', 1)[-1]}-accept")
        self._accept.start()
        self._conn = None
        self._lock = threading.Lock()
        self._closed = False

    @property
    def conn(self):
        if self._conn is None:
            # 分段等、每段看一眼子进程还在不在：import 阶段就崩了的，不必干等满 timeout 才报
            deadline = time.monotonic() + self.timeout
            while not self._got and self.proc.poll() is None and time.monotonic() < deadline:
                self._accept.join(0.5)
            if not self._got:
                self.proc.kill()
                rc = self.proc.poll()
                raise RuntimeError(f"{self.what} 没连上：" + (f"子进程已退出（退出码 {rc}）" if rc is not None
                                                             else f"{self.timeout:.0f} s 内没连上"))
            self._conn = self._got[0]
        return self._conn

    def send(self, msg) -> None:
        conn = self.conn
        with self._lock:
            conn.send(msg)

    def recv(self):
        return self.conn.recv()

    def recv_checked(self):
        """收一条；`("hw", …)` / `("err", …)` 换成异常。"""
        return check(self.recv(), self.what)

    def close(self, wait: float = 60.0) -> None:
        """收摊（连上前后都能调）：发 `("x",)` 让它退、限时等、不退就杀。连都没连上的直接杀。"""
        if self._closed:
            return
        self._closed = True
        if self._conn is None and self._got:
            self._conn = self._got[0]
        if self._conn is not None:
            try:
                with self._lock:
                    self._conn.send(("x",))
            except OSError:
                pass
        try:
            self.proc.wait(wait if self._conn is not None else 5)
        except Exception:                                  # noqa: BLE001
            self.proc.kill()
        for h in (self._conn, self.lst):
            try:
                if h is not None:
                    h.close()
            except OSError:
                pass


def check(msg, what: str):
    """子进程发来的一条消息：`("hw", 原因)` -> `HwaccelMismatch`（外层监督者整段改软解）、`("err", 回溯)` -> RuntimeError，其余原样返回。"""
    if msg[0] == "hw":
        from flowocr.extract.framesource import HwaccelMismatch
        raise HwaccelMismatch(msg[1])
    if msg[0] == "err":
        raise RuntimeError(f"{what}出错：\n{msg[1]}")
    return msg


def connect():
    """子进程那边：按 argv 的端口、环境变量的口令连回管线进程。返回 `(conn, send)`，`send` 加了锁（几个线程都会发）。"""
    from multiprocessing.connection import Client

    key = bytes.fromhex(os.environ.pop(AUTH_ENV))
    conn = Client(("127.0.0.1", int(sys.argv[1])), authkey=key)
    lock = threading.Lock()

    def send(msg) -> None:
        with lock:
            conn.send(msg)

    return conn, send


def attach_shm(name: str):
    """挂接**别的进程建的**共享内存段（建的那一方负责 unlink）。

    POSIX 上 CPython 3.12 的 `SharedMemory(name=…)` 挂接也会把段登记进本进程的 `resource_tracker`，本进程一退，
    tracker 就把段 unlink 掉、还打 "leaked shared_memory" 警告，建的那一方再 unlink 时找不到（2026-10-17 WSL 上的 Linux 试跑）。
    所以挂上就撤掉登记（3.13 起是 `track=False`；项目锁在 3.12）。Windows 不登记，原样挂接。"""
    from multiprocessing import resource_tracker, shared_memory

    shm = shared_memory.SharedMemory(name=name)
    if sys.platform != "win32":
        resource_tracker.unregister(shm._name, "shared_memory")
    return shm
