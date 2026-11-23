"""ONNX Runtime 常驻 worker 的**客户端**：起进程、管共享内存、一次调用一次往返。

服务端是同目录的 `ort_server.py`（独立进程；用哪个解释器起见 `server_python()`，为什么隔离见那份文件头）。
一个客户端可以带多个模型（det 和 rec 共用一个进程）。

用法：

    cli = OrtClient({"det": "models/PP-OCRv6_medium_det_onnx/inference.onnx",
                     "rec": "models/PP-OCRv6_medium_rec_onnx/inference_argmax2.onnx"})
    outs = cli.run("rec", x)          # x 是 float32 的 C 序数组；outs 是 list[np.ndarray]
    cli.close()

⚠ **形状要收敛**：服务端每个形状开一个 session（ORT 每次形状变化都重规划）。
调用方自己负责把形状取整到少数几个，否则显存会被 session 吃光。
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from multiprocessing import shared_memory
from pathlib import Path

import numpy as np

from flowocr import paths

SERVER = Path(__file__).resolve().parent / "ort_server.py"
"""worker 脚本和这个文件同目录（2026-09-22 起在包里）；venv / 模型仍跟着**产物根**走（`flowocr.paths` 那条）。"""



def _has_onnxruntime() -> bool:
    return importlib.util.find_spec("onnxruntime") is not None


def server_python() -> str:
    """起 ORT worker 用哪个解释器：`FLOWOCR_ORT_PYTHON` 设了就是它；否则**当前解释器**（要装了 onnxruntime——
    `[nvidia]` / `[cpu]` extras 都带）。原来还有第三条：退回 旧的 onnxrt 实验 venv（双 venv 时代的布局），
    发布前删了（release-plan F7：生产代码不依赖可整目录删的 `explore/`）；要用别的解释器就设 `FLOWOCR_ORT_PYTHON`。"""
    configured = os.environ.get("FLOWOCR_ORT_PYTHON", "").strip()
    if configured:                        # 显式指定（A/B 换 ORT 版本、或发行时把 worker 放在另一个解释器里）
        return configured
    if _has_onnxruntime():
        return sys.executable
    raise SystemExit(f"这个解释器（{sys.executable}）没装 onnxruntime：装 flowocr 的 [nvidia] 或 [cpu] extras"
                     f"（uv sync --extra nvidia|cpu），或用 FLOWOCR_ORT_PYTHON 指一个装了的解释器")


def read_ready(stream) -> dict:
    """读服务的 ready 行：之前的 `{"note": …}` 行（缺 TRT 引擎时现建，一两分钟）原样打给用户；不是 JSON 的行（原生库往 stdout 打的）跳过。
    读到头还没有 JSON 行就返回带最后一行 `line` 的字典（调用方按"起不来"报）。"""
    last = ""
    while True:
        line = stream.readline()
        if not line:
            return {"line": last[:300]} if last else {}
        if not line.strip():
            continue
        try:
            got = json.loads(line)
        except json.JSONDecodeError:
            last = line
            continue
        if isinstance(got, dict) and "note" in got and "ready" not in got:
            print(got["note"], flush=True)
            continue
        return got if isinstance(got, dict) else {"line": line[:300]}


def provider_of(reply: dict, models) -> str:
    """握手里这个客户端那几个模型实际用的 provider（不同就用 + 连起来，如 det 走 TRT、自带的 rec 走 CUDA EP 时）。"""
    provs = reply.get("providers") or {}
    return "+".join(dict.fromkeys(provs.get(m, "") for m in models if provs.get(m)))


def pick_addr(env: str, models) -> str:
    """`FLOWOCR_ORT_ADDR` 里挑出**这个 client 该连哪个服务**。

    两种写法：

    * `host:port` —— 不分引擎的老写法，连上去由握手核能力；
    * `rec=host:port,det=host:port` —— 按引擎写地址。`start_ort_services` 现在只起**一个**服务带 det 和 rec，
      两个地址是同一个端口（2026-10-05，resource 计划：两个服务各一份 CUDA 上下文和 arena，4 组时 rec 8.1 GB + det 6.4 GB）；
      det 和 rec 的 client 各连一条，照样并发（服务端普通 session 的推理不加锁）。

    要的模型分散在不同服务里就返回空串——这个 client 自己 spawn 一个带全套的 worker，
    正确性比共享重要（一个 client 一根管子一块共享内存，连不了两个服务）。
    """
    env = (env or "").strip()
    if not env:
        return ""
    if "=" not in env:
        return env
    table = dict(part.split("=", 1) for part in env.split(",") if "=" in part)
    hit = {table.get(m, "") for m in models}
    return hit.pop() if len(hit) == 1 and all(hit) else ""


class OrtClient:
    cublas_ws = ""
    trt = None
    providers: dict = {}
    server_id = ""
    """连的是哪个服务：接上现成服务时是它的地址，自己起的是 `pid:<服务进程号>`。几个进程的 `_meta` 合并时据此分辨服务端累计数是不是同一份（`ocr_parallel.merge_metas`）。"""

    def __init__(self, models: dict[str, str], in_mb: int = 64, out_mb: int = 32,
                 python: str | None = None, gpu_mem_mb: int = 6144, max_sessions: int = 12,
                 device: str = "gpu:0", extra_args: list[str] | None = None, attach: str = "") -> None:
        """`gpu_mem_mb` / `max_sessions`：arena 上限和 per-shape 池子的条数上限（资源预算：flowocr 自己显存 6.5 GB，owner 2026-10-05，架构说明「推理服务」）。
        CUDA EP 的 arena 上限 + per-shape 池子的条数上限——超了 ORT 当场报 OOM、池子按 LRU 淘汰，
        而不是把卡吃光（实测一条扫宽 × 批的探针同时活着 24 个 session、吃到 15 GB）。
        `python` 不给就按 `server_python()`；模型路径经 `paths.resolve_model`（相对当前目录找不到就到模型根找）。
        `device`：`gpu:N` / `cpu`（`gpu` 当 `gpu:0`），原样交给 worker（`ort_server --device`）；调用方按自己的 `--device` 传，
        **别让 worker 自己挑**（NVIDIA 包里 `--device cpu` 时 rec 曾经照样上 GPU，Codex 审计 P1；卡号曾经丢在半路，复审 P2）。
        实际用上的 provider 在 `self.provider`，`stats()` 带出去。
        `extra_args`：原样追加给 `ort_server` 的参数（比如 `--share-mem 1`；管线由 `run_ocr2.ort_server_args` 给）。
        `attach`：**只接**这个地址上的现成服务（`side_channel` 用）——接不上就抛，不自己起（那会多出一整份模型和显存）。"""
        from flowocr.extract.ocr_args import ort_device   # ort_server 顶层 import onnxruntime，别为一个字符串归一去拉它
        try:
            self.device = ort_device(device)
        except SystemExit as exc:
            raise ValueError(str(exc)) from None
        self.cap_in, self.mb = in_mb * 2 ** 20, (in_mb, out_mb)
        self.shm = shared_memory.SharedMemory(create=True, size=self.cap_in + out_mb * 2 ** 20)
        if attach:
            if not self._attach(attach, models):
                self.shm.close()
                self.shm.unlink()
                raise RuntimeError(f"接不上 ORT 服务 {attach}")
            return
        python = python or server_python()
        models = {n: paths.resolve_model(p) for n, p in models.items()}
        # **有现成的服务就接上去**（`FLOWOCR_ORT_ADDR=host:port`）：N 个 worker 共用一份模型
        # = **显存只有一份**。owner 2026-09-19 把 `--workers` 默认压回 1 的理由就是显存，
        # 共享是重新放宽的前提。自己的共享内存还是自己建——张量不走 socket。
        addr = pick_addr(os.environ.get("FLOWOCR_ORT_ADDR", ""), models)
        if addr and self._attach(addr, models):
            return
        self.shared = False
        args = [os.path.abspath(python), str(SERVER), "--shm", self.shm.name,
                "--in-bytes", str(self.cap_in), "--gpu-mem-mb", str(gpu_mem_mb),
                "--max-sessions", str(max_sessions), "--device", device]
        for name, path in models.items():
            args += ["--model", f"{name}={path}"]
        args += list(extra_args or ())
        # ⚠ Windows 的 CreateProcess 不吃"相对路径 + 正斜杠"的可执行文件（WinError 2，实测）
        # worker 的 stderr 落一个临时文件：它建 session 就死的时候（2026-09-22 nightly：ORT 1.26 的 CPU EP 打不开 fp16 模型），
        # 原来 DEVNULL 只剩客户端一行 JSONDecodeError，病因要另起进程才看得到
        import tempfile
        self._err = tempfile.NamedTemporaryFile(prefix="flowocr-ort-", suffix=".log", delete=False)
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self._err, text=True, encoding="utf-8",
                                     bufsize=1)
        hello = read_ready(self.proc.stdout)
        if "ready" not in hello:
            tail = self._stderr_tail()                # 先取，close() 会把那个临时文件删掉
            self.close()
            raise RuntimeError(f"ORT worker 起不来：{hello}；它的 stderr 尾部：{tail}")
        self.provider, self.ort_version = provider_of(hello, models), hello.get("ort_version", "")
        self.server_id = f"pid:{self.proc.pid}"
        self.providers = hello.get("providers") or {}
        """每个模型实际用的 provider（同一个服务里 det 走 TRT、rec 退回 CUDA EP 时两者不同）：`provider_for` 按模型取。"""
        self.trt = hello.get("trt")
        """TensorRT 那边的说明（服务没被要求用 TRT 时是 None）：版本、精度、每个模型 trt 还是退回了 cuda（`ort_server.start_trt`）。"""
        self.cublas_ws = hello.get("cublas_ws", "")     # 服务进程里生效的 CUBLAS_WORKSPACE_CONFIG（环境里原有的优先于 --cublas-ws）
        self.side_addr = f"127.0.0.1:{hello['port']}" if hello.get("port") else ""
        """服务开了旁路端口（`extra_args` 里有 `--side-listen`）时，`side_channel()` 从这里再接连接进去。"""
        self.pre = bool(hello.get("pre"))
        """服务端会做 rec 前处理（能收原样的 uint8 裁剪，`run_crops`）。"""
        self.models = list(models)
        self.t = {"send": 0.0, "wait": 0.0, "infer": 0.0, "pre": 0.0, "calls": 0}
        """分段计时（**别猜，量**）：调用方自己决定什么时候打出来。"""
        self.lock = threading.Lock()
        """⚠ **一个 client 同时只能有一个调用在飞**：一根管道 + 一块共享内存，
        两个线程各写各的请求会串线——回信认不出是谁的，输入区也会互相盖。
        管线里 det 在预取线程、rec 在主线程，**真会同时调**（2026-09-18 接 det 后端时差点栽）。
        锁只保证不坏；要并行就给两个引擎各起一个 worker（`run_ocr2` 就是这么做的）。"""
        self.shapes: dict = {}

    def _attach(self, addr: str, models: dict[str, str]) -> bool:
        """接上现成的共享服务。**接不上就返回 False，由调用方自己 spawn**。

    ⚠ `FLOWOCR_ORT_ADDR` 是**进程级**的，而一个 run_ocr2 进程里 det 和 rec 是**两个 client**
    （det 在预取线程、rec 在主线程，共用一个 worker 会串行——实测 det 段 24.8 → 26.1 s）。
    握手必须报出**自己要哪几个模型**（`want`）让服务端核：核不过就退回自己 spawn。不核的话，只加载了部分模型的服务
    （用户自己指的地址、旧版本按引擎分开的服务）会收到它没有的模型的请求、在服务端 `models[...]` 上炸（审计七）。
        """
        import socket
        try:
            host, _, port = addr.rpartition(":")
            sock = socket.create_connection((host or "127.0.0.1", int(port)), timeout=10)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)   # 关 Nagle，同 ort_server 那一侧（decode-buffer）
            f = sock.makefile("rwb")
            f.write((json.dumps({"hello": {"shm": self.shm.name, "in_bytes": self.cap_in,
                                           "want": sorted(models), "device": self.device}}) + "\n").encode())
            f.flush()
            ok = json.loads(f.readline() or "{}")
        except Exception as e:                       # 服务没了 / 连不上：自己起一个就是
            print(f"[ort] 接不上共享服务 {addr}（{e!r}），自己起一个 worker", flush=True)
            return False
        if not ok.get("ok"):
            f.close()
            sock.close()
            print(f"[ort] 共享服务 {addr} 不提供 {ok.get('missing')} / 设备是 {ok.get('device')}"
                  f"（它有 {ok.get('models')}，这个 client 要 {self.device}），自己起一个 worker", flush=True)
            return False
        sock.settimeout(None)
        self.provider, self.ort_version = provider_of(ok, models), ok.get("ort_version", "")
        self.providers = ok.get("providers") or {}
        self.trt = ok.get("trt")
        self.cublas_ws = ok.get("cublas_ws", "")
        self.side_addr = addr
        self.pre = bool(ok.get("pre"))
        self.sock, self.f, self.proc = sock, f, None
        self.models = list(models)
        self.t = {"send": 0.0, "wait": 0.0, "infer": 0.0, "pre": 0.0, "calls": 0}
        self.lock = threading.Lock()
        self.shapes = {}
        self.shared = True
        self.server_id = addr
        return True

    def provider_for(self, model: str) -> str:
        """这个模型实际用的 provider。默认拓扑下 det 走 rec 服务的旁路连接，两个 client 都带着 {rec, det}，
        `self.provider` 是两者连起来的，追溯设备要按模型取。"""
        return self.providers.get(model) or self.provider

    def side_channel(self) -> "OrtClient":
        """再接一条到**同一个服务进程**的连接（自己的共享内存、自己的锁），给同时在途的第 2..N 个请求用
        （`run_ocr2 --rec-inflight`，decode-buffer）：模型和显存仍只有一份。
        管道模式起服务时要带 `--side-listen`；接的是共享服务就连同一个地址。
        **接不上就抛**——这里不退回自己 spawn（那会多出一整份模型和显存）。"""
        if not self.side_addr:
            raise RuntimeError("这个 ORT 服务没开旁路端口（起它时要给 --side-listen）")
        return OrtClient(dict.fromkeys(self.models, ""), in_mb=self.mb[0], out_mb=self.mb[1],
                         device=self.device, attach=self.side_addr)

    def run(self, model: str, x: np.ndarray) -> list[np.ndarray]:
        if x.dtype != np.float32 or not x.flags["C_CONTIGUOUS"]:
            x = np.ascontiguousarray(x, dtype=np.float32)
        if x.nbytes > self.cap_in:
            raise ValueError(f"输入 {x.nbytes / 2**20:.0f} MB 超过共享内存的输入区 "
                             f"{self.cap_in / 2**20:.0f} MB")
        with self.lock:
            t0 = time.perf_counter()
            np.ndarray(x.shape, dtype=np.float32, buffer=self.shm.buf[: x.nbytes])[...] = x
            return self._roundtrip(model, {"model": model, "shape": list(x.shape)}, t0)

    def run_crops(self, model: str, crops: list[np.ndarray], grid: int, batch: int) -> list[np.ndarray]:
        """rec：送**原样的 uint8 裁剪**（依次紧排进共享内存），前处理在服务进程里做（`recprep.batch`，要 `self.pre`）。
        为什么（2026-09-23，decode-buffer）：前处理在管线进程里抢 GIL，6,400 个裁剪要 5~9 s（单独跑约 1 s）。"""
        crops = [np.ascontiguousarray(c, dtype=np.uint8) for c in crops]
        total = sum(c.nbytes for c in crops)
        if total > self.cap_in:
            raise ValueError(f"裁剪共 {total / 2**20:.0f} MB 超过共享内存的输入区 {self.cap_in / 2**20:.0f} MB")
        with self.lock:
            t0 = time.perf_counter()
            off = 0
            for c in crops:
                self.shm.buf[off: off + c.nbytes] = c.reshape(-1).data
                off += c.nbytes
            return self._roundtrip(model, {"model": model, "crops": [list(c.shape) for c in crops],
                                           "grid": grid, "batch": batch}, t0)

    def _roundtrip(self, model: str, req_d: dict, t0: float) -> list[np.ndarray]:
        """输入已经写进共享内存：发一行请求、等一行回信、按回信切输出。**两种请求共用这一份**。"""
        req = json.dumps(req_d) + "\n"
        if self.shared:                      # 共用一个服务进程：走 socket
            self.f.write(req.encode())
            self.f.flush()
            t1 = time.perf_counter()
            line = self.f.readline().decode()
        else:
            self.proc.stdin.write(req)
            self.proc.stdin.flush()
            t1 = time.perf_counter()
            line = self.proc.stdout.readline()
        t2 = time.perf_counter()
        if not line:
            raise RuntimeError("ORT worker 没了（连接断了）")
        rep = json.loads(line)
        if "outs" not in rep:
            raise RuntimeError(f"ORT worker 报错：{rep}")
        out, off = [], self.cap_in
        for dtype, shape in rep["outs"]:
            arr = np.ndarray(tuple(shape), dtype=np.dtype(dtype),
                             buffer=self.shm.buf[off: off + int(np.prod(shape)) * np.dtype(dtype).itemsize])
            out.append(arr.copy())
            off += arr.nbytes
        self.t["send"] += t1 - t0
        self.t["wait"] += t2 - t1
        self.t["infer"] += rep.get("infer_ms", 0.0) / 1000.0
        self.t["pre"] += rep.get("pre_ms", 0.0) / 1000.0     # 服务端做的前处理（只有 run_crops 有）
        for part in ("session", "output", "admit"):
            self.t[part] = self.t.get(part, 0.0) + rep.get(f"{part}_ms", 0.0) / 1000.0
        self.t["admit_max"] = max(self.t.get("admit_max", 0.0), rep.get("admit_ms", 0.0))
        self.t["built"] = rep.get("built", 0)          # 服务端自报的累计数（不是增量）
        self.t["evicted"] = rep.get("evicted", 0)
        self.t["warmed"] = rep.get("warmed", 0)        # 服务端起动后后台预热进池子的形状数（--warm）
        self.t["build"] = rep.get("build_ms", 0.0) / 1000.0
        if "trt_oop" in rep:                           # TRT 池子退回 CUDA EP 的次数（服务端累计）：超 profile / 运行中报错
            self.t["trt_oop"], self.t["trt_err"] = rep["trt_oop"], rep["trt_err"]
            if rep.get("trt_err_first"):
                self.t["trt_err_first"] = rep["trt_err_first"]
        self.t["calls"] += 1
        key = (model, tuple(rep.get("shape") or req_d.get("shape") or ()))
        self.shapes[key] = self.shapes.get(key, 0) + 1
        return out

    def stats(self) -> dict:
        """给 `_meta` 的分因数据（2026-09-19 两轮审计）。

        * `wait_sec − infer_sec` 是**客户端这一侧**看到的"推理之外的钱"：建 session + IPC + 输出拷贝。
          IPC 实测每次调用 0.12~0.33 ms，量级小，但**它确实混在里面**，别把这个差整个当成建 session。
          `overhead_sec` 还扣掉服务端前处理；新增的 `server_session_sec` / `server_output_sec` 是其中的子项，不能重复相加。
          session 子项包括获取/构建及计数锁等待，output 子项只计服务端写共享内存；都不是独立 CUDA 核函数时间。
          `server_admit_sec` 也是其中的子项：服务端准入（`ort_server.Admit`，多组共享服务时按激活大小封顶）挡着这个客户端的请求等了多久；
          `server_admit_max_ms` 是单次最长的一等——准入没有先来后到，大请求被别组的小请求一直挤着时看它。
          走 TRT 池子时 `server_session_sec` 还含等空闲 TRT session 的时间；`trt_oop` / `trt_err` 是这个模型退回 CUDA EP 的次数
          （超 profile / 运行中报错，服务端累计，`trt_err_first` 是第一次的错）。
        ⚠ **共享服务下 `built` / `evicted` / `build_sec` 是跨客户端的全局累计值**
          （服务端一份计数器、逐次回给每个客户端），所以多 worker 时**别把它当这一个 worker 的账**。
          那组证据是单进程量的、不受影响（审计七第三条）。

        * `built` / `evicted` / `build_sec` 是**服务端自报的**：建了几个 session、LRU 淘汰了几次、
          建它们花了多久。**只有 `build_sec` 分不开**"每个新形状一次性的规划"（整片上摊薄）和
          "反复淘汰重建"（整片上持续付）——而这两种对整片的预测相反。`evicted > 0` 就是后者在发生。
        """
        return {"calls": self.t["calls"], "send_sec": round(self.t["send"], 2),
                "wait_sec": round(self.t["wait"], 2), "infer_sec": round(self.t["infer"], 2),
                "overhead_sec": round(self.t["wait"] - self.t["infer"] - self.t["pre"], 2),
                "server_pre_sec": round(self.t["pre"], 2), "pre_on_server": self.pre,
                "server_session_sec": round(self.t.get("session", 0.0), 4),
                "server_output_sec": round(self.t.get("output", 0.0), 4),
                "server_admit_sec": round(self.t.get("admit", 0.0), 4),
                "server_admit_max_ms": round(self.t.get("admit_max", 0.0), 2),
                "built": self.t.get("built", 0), "evicted": self.t.get("evicted", 0), "warmed": self.t.get("warmed", 0),
                "build_sec": round(self.t.get("build", 0.0), 2),
                "shapes": len(self.shapes), "device": self.device, "provider": self.provider,
                "ort_version": self.ort_version, "cublas_ws": self.cublas_ws, "server": self.server_id,
                **{k: self.t[k] for k in ("trt_oop", "trt_err", "trt_err_first") if k in self.t}}

    def report(self, tag: str = "") -> None:
        if not self.t["calls"]:
            return
        print(f"[ort{tag}] {self.t['calls']} 次调用：送 {self.t['send']:.1f}s、等回信 "
              f"{self.t['wait']:.1f}s（worker 自报推理 {self.t['infer']:.1f}s）；"
              f"{len(self.shapes)} 种形状", flush=True)

    def close(self) -> None:
        if getattr(self, "shared", False):
            # 接上去的那条：**只关自己这一端**，服务进程是别人的
            try:
                self.f.write(b'{"bye":1}\n')
                self.f.flush()
            except Exception:
                pass
            finally:
                # A cancelled connection can fail while sending bye. Close each
                # handle independently, including the socket's buffered view.
                for handle in (getattr(self, "f", None), getattr(self, "sock", None)):
                    if handle is not None:
                        try:
                            handle.close()
                        except Exception:
                            pass
            if getattr(self, "shm", None):
                self.shm.close()
                try:
                    self.shm.unlink()
                except FileNotFoundError:
                    pass
            self.shm = None
            return
        try:
            if getattr(self, "proc", None) and self.proc.poll() is None:
                self.proc.stdin.write(json.dumps({"bye": 1}) + "\n")
                self.proc.stdin.flush()
                self.proc.wait(timeout=5)
        except Exception:
            if getattr(self, "proc", None):
                self.proc.kill()
        finally:
            if getattr(self, "shm", None):
                self.shm.close()
                try:
                    self.shm.unlink()
                except FileNotFoundError:
                    pass
            err = getattr(self, "_err", None)
            if err is not None:                       # worker 的 stderr 临时文件：正常退出就删掉
                self._err = None
                try:
                    err.close()
                    os.unlink(err.name)
                except OSError:
                    pass

    def _stderr_tail(self, n: int = 1500) -> str:
        err = getattr(self, "_err", None)
        if err is None:
            return ""
        try:
            err.flush()
            return Path(err.name).read_text(encoding="utf-8", errors="replace")[-n:].strip()
        except OSError:
            return ""

    def __del__(self) -> None:
        self.close()


def make_det_runner(client: OrtClient, model: str = "det"):
    """给 `FastDet` 换 runner：它只要一个 `runner([blob]) -> preds` 的可调用对象。

    所以 det 的**前处理（`FastDet.preprocess`）和后处理（`post_op`）一个字都不用动**，
    换掉的只是中间那一下推理。det 的输入是定尺（按 `--det-batch` 有一两种形状），
    没有 rec 那个动态宽的坑。

    ⚠ **只用 fp32 那份**：实测 fp16 的 det 会多出 10% 的框（IoU≥0.5 只有 94.4%、24 帧没有一帧
    多边形完全相同），fp32 则 21/24 帧完全相同、108 vs 107 框（一次性探针 `probe_ort_det.py`）。
    """
    def runner(blobs):
        x = blobs[0]
        return client.run(model, x)
    return runner
