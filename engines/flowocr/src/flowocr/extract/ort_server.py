"""ONNX Runtime 的**常驻 worker**：独立进程，靠共享内存和调用方交换张量。
用哪个解释器起它由 `ortclient.server_python()` 定（`FLOWOCR_ORT_PYTHON` > 当前解释器自己装了 onnxruntime > 旧的 onnxrt 实验 venv）。

一个进程可以同时服务**多个模型**（`--model 名字=路径` 可给多次）：管线里 det 和 rec 都走它，
省一份显存、也少一层 CUDA 上下文切换。

为什么推理在单独的进程里：最早是为了不和 Paddle 同进程（两个推理运行时各自加载 cuDNN / cuBLAS，版本互相踩）；
2026-10 运行时去掉 Paddle 之后照旧——管线进程的 GIL 不被推理和前处理抢，一个服务同时接 det 和几个 rec 在途。
代价是每次调用一次进程间往返，**实测 0.12~0.33 ms = 2%~3%**（一次性探针 `probe_ort_rec_ipc.py`）。

**每个形状一个 session**（`--per-shape`，默认开）：ORT 的 CUDA EP **每次形状变化**都要重新规划，
**不是只在第一次见到的时候**——同宽连打 4.6 ms、8 个宽轮转 **81 ms**，预热过也还是 80 ms。
每个形状各给一个 session 之后回到 5.2 ms。代价是每个 session 一份权重（rec fp32 73 MB）和一份自己的 arena，`--share-mem` 让它们共用（约 155 MB → 50 MB）。
所以调用方**必须**把形状收敛到少数几个（rec 那边是把张量宽取整到 320 的倍数 + 批取到 2 的幂）。

**TensorRT 后端**（`--trt-model 名字=路径`，TRT 后端计划）：给了的模型不建 CUDA EP 的 session，换成一个动态形状的 TRT 引擎、
一个 session 小池子（`--trt-sessions`，默认 det、rec 各 1 个；同一个 TRT session 上的并发推理是串行的，池子大于 1 时调用线程各领一个）。
引擎部分在同目录的 `trt_engine.py`（按文件路径载入，理由同 `load_recprep`）；缓存里没有就在**这个进程里**建（缓存名里的 ORT 版本得是建它的那个进程的），
建好之后每次起服务都拿入库的合成文字图对 CPU 参考跑一遍 min / opt / max 三个形状（`trt_engine.selfcheck`）。
ORT 版本对不上、建不出来、载不起来、自检不过，这个模型就退回 CUDA EP（`--model` 给的那份图），原因进握手的 `trt`；
跑的时候形状超出 profile、或 TRT 报错，那一次退回 CUDA EP 的按形状 session，分开计数（回信的 `trt_oop` / `trt_err`）。

协议（够用就好，不做通用 RPC）：
* 调用方建一块共享内存，输入张量原样写进前半段（float32、C 序）；
* 一行 JSON 进来：`{"model": 名字, "shape": [...]}`；或者 rec 送**原样的 uint8 裁剪**（依次紧排）：
  `{"model": 名字, "crops": [[h, w, 3], …], "grid": 320, "batch": k}`，前处理在这边做（`recprep.batch`，握手里 `pre` 为真才能这么送）；
* 结果按顺序写进后半段（`--in-bytes` 之后），回一行
  `{"outs": [[dtype, shape], …], "infer_ms": …}`——调用方按这个自己切；
* `{"bye": 1}` 退出。
"""
from __future__ import annotations

import argparse
import contextlib
import functools
import json
import sys
import threading
import time
from multiprocessing import resource_tracker, shared_memory

import numpy as np
import onnxruntime as ort


def attach_shm(name: str) -> shared_memory.SharedMemory:
    """挂接调用方建的共享内存段，挂上就撤掉本进程 `resource_tracker` 的登记（POSIX 上不撤，本进程一退就把段 unlink 掉）。
    同 `childproc.attach_shm`；这里另写一份是因为这个文件可能跑在别的解释器里、不 import flowocr。"""
    shm = shared_memory.SharedMemory(name=name)
    if sys.platform != "win32":
        resource_tracker.unregister(shm._name, "shared_memory")
    return shm


def norm_device(d: str) -> str:
    """`cpu` / `gpu` / `gpu:N` -> `cpu` / `gpu:N`（`gpu` 就是 0 号卡）。这个文件可能跑在别的解释器里、不 import flowocr，
    所以口径在这里自己写一份；和 `ocr_args.ort_device` 的输出是同一种字符串，握手按它逐字比。"""
    d = (d or "").strip().lower()
    if d == "cpu":
        return "cpu"
    if d == "gpu":
        return "gpu:0"
    if d.startswith("gpu:") and d[4:].isdigit():
        return f"gpu:{int(d[4:])}"
    raise SystemExit(f"--device 只认 cpu / gpu / gpu:N：{d!r}")


def load_recprep():
    """按**文件路径**加载同目录的 `recprep.py`（rec 的前处理），不经过 `flowocr.extract` 的包初始化
    （这个进程可能跑在别的解释器里，也可能没有 flowocr）。没有它或没有 cv2 就返回 None，
    客户端照旧自己做前处理、送张量（握手里的 `pre` 报这件事）。"""
    import importlib.util
    from pathlib import Path
    p = Path(__file__).with_name("recprep.py")
    try:
        import cv2  # noqa: F401
        spec = importlib.util.spec_from_file_location("_flowocr_recprep", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as exc:                     # noqa: BLE001
        print(json.dumps({"note": f"前处理不在服务端做：{exc!r}"}), file=sys.stderr, flush=True)
        return None


@functools.lru_cache(maxsize=1)
def load_trt_engine():
    """按文件路径加载同目录的 `trt_engine.py`（理由同 `load_recprep`），只载一次。载不了就抛，由调用方记成回退原因。"""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("_flowocr_trt_engine", Path(__file__).with_name("trt_engine.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TRT_SESSIONS = {"det": 1, "rec": 1}
"""TRT session 池子的默认大小（`--trt-sessions`）：det 1 个——单组同一时刻只有一次 det 推理，多组共用服务时 `--admit det` 也把 det 封在一批。
rec 1 个（owner 2026-10-16）：rec 是异步的、不在关键路径上，单个 session 忙的时间只占主循环的 45%~65%（quick 六段、一小时窗口），
两个 session 并发时又互相拖慢（单次 10.9 → 15.7 ms），所以 2 个对 1 个墙钟持平（单组、三组共用服务都量过），白多 1.0 GB 显存、约 1 GB 内存。
rec 的活比鸣潮菜单段再多一半以上、单个 session 忙到接近满的素材上 2 个才可能有用：`--ort-server-extra "--trt-sessions rec=2"`。"""


def parse_sizes(spec: str) -> dict:
    """`det=1,rec=2` -> {"det": 1, "rec": 2}，没写的模型按 `TRT_SESSIONS`、再没有就 1 个。"""
    got = dict(TRT_SESSIONS)
    for kv in filter(None, (spec or "").split(",")):
        k, _, v = kv.partition("=")
        got[k.strip()] = max(1, int(v))
    return got


def start_trt(a, models: dict, cuda_opts: dict, make_so, note) -> tuple[dict, dict]:
    """`--trt-model` 里的模型各建一个 TRT session 池（`trt_engine.Pool`）。返回 `(池子, 说明)`：说明进握手的 `trt`、客户端记进 `_meta.backend`——
    每个模型 `backend` 是 trt 还是 cuda，cuda 时 `fallback` 写"哪一步：原因"（ort_version / lib / build / ref / load / selfcheck）。"""
    from pathlib import Path
    want = {n: p for n, p in (m.split("=", 1) for m in a.trt_model) if n in models}
    info = {"version": a.trt_version, "precision": a.trt_precision, "models": {}}
    pools: dict = {}

    def fall(name: str, why: str) -> None:
        info["models"][name] = {"backend": "cuda", "fallback": why[:500]}
        note(f"[trt] {name} 退回 CUDA EP：{why[:300]}")

    mm = ".".join(ort.__version__.split(".")[:2])
    why = ""
    if a.trt_ort and mm not in a.trt_ort.split(","):
        why = f"ort_version: 推理服务的 onnxruntime 是 {ort.__version__}，TensorRT {a.trt_version} 对的是 {a.trt_ort}"
    elif "TensorrtExecutionProvider" not in ort.get_available_providers():
        why = f"ort_version: onnxruntime {ort.__version__} 没带 TensorRT EP"
    E = cap = None
    if not why:
        try:
            E = load_trt_engine()
            cap = E.compute_cap(cuda_opts["device_id"])
        except Exception as e:                   # noqa: BLE001
            why = f"lib: {type(e).__name__}: {e}"
    if why:
        for name in want:
            fall(name, why)
        return pools, info
    sizes, eng, dev = parse_sizes(a.trt_sessions), Path(a.trt_engines), cuda_opts["device_id"]
    for name, src in want.items():
        stage = "build"
        try:
            if name not in E.PROFILES:
                raise ValueError(f"没有 {name} 的 profile")
            prefix = E.build(name, Path(src), a.trt_precision, eng, cap, a.trt_version, dev, log=note)
            stage = "ref"
            ref = E.ensure_ref(name, Path(src), eng, log=note)
            stage = "load"
            sessions = [E.make_session(name, Path(src), a.trt_precision, prefix, eng, dev, so=make_so(), cuda=cuda_opts)
                        for _ in range(sizes.get(name, 1))]
            stage = "selfcheck"
            bad = E.selfcheck(sessions[0], name, ref)       # 同一份引擎文件、同样的核：查一个就够
            if bad:
                raise RuntimeError(bad)
        except Exception as e:                   # noqa: BLE001
            sessions = None                      # 自检不过的 TRT session 先放掉，别占着显存去建下一个模型的引擎
            fall(name, f"{stage}: {type(e).__name__}: {e}")
            continue
        pools[name] = E.Pool(name, sessions)
        info["models"][name] = {"backend": "trt", "onnx": src, "engine": prefix, "engine_sha256": E.engine_sha(eng, prefix),
                                "sessions": len(sessions)}
    return pools, info


def load_timeline():
    """`run_ocr2 --timeline` 开着时（环境变量 `FLOWOCR_TIMELINE`，起服务的客户端进程设的），这个进程也写一份 `<路径>.ort.jsonl`：
    每个请求在服务端的几段（取 / 建 session、前处理、推理、写输出）。同目录的 `timeline.py` 按文件路径加载（只有标准库，理由同
    `load_recprep`）。没开或加载不了就返回 None——关着时 `infer` 里只多几次 None 判断和两次 `perf_counter`。"""
    import importlib.util
    import os
    from pathlib import Path
    if not os.environ.get("FLOWOCR_TIMELINE"):
        return None
    try:
        spec = importlib.util.spec_from_file_location("_flowocr_timeline", Path(__file__).with_name("timeline.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.start_from_env("ort")
        return mod
    except Exception as exc:                     # noqa: BLE001
        print(json.dumps({"note": f"服务端不写时间线：{exc!r}"}), file=sys.stderr, flush=True)
        return None


class Admit:
    """按请求的大小发令牌的准入（`--admit`，2026-10-05，resource 计划）：同一个模型同时在算的令牌之和不超过 `cap`，
    激活显存从结构上有界——多组时各组的 det 批、各组的 rec 请求会同时压在一个服务上（4 组实测 rec 8.1 GB + det 6.4 GB）。
    令牌的单位（`unit`）：`img` = 批里几张图（det：长边缩到 960，一张图的激活大致一样大，竖屏、裁切也一样按张数算）；
    `bw` = 批 × 张量宽（rec：高固定 48，激活跟着 批 × 宽 走）。手上一个都没有时照放（单个超过 `cap` 的请求也能跑，不死锁）。
    没有先来后到：放行时谁装得下谁先走，大请求可能被别组的小请求一直挤着。所以每次等了多久回给调用方（`with … as 等待秒数`，
    回信的 `admit_ms`），客户端记进 `stats()` 的 `server_admit_sec` / `server_admit_max_ms`。"""

    def __init__(self, cap: int, unit: str = "bw") -> None:
        if unit not in ("img", "bw"):
            raise ValueError(f"--admit 的单位只认 img / bw：{unit}")
        self.cap, self.unit, self.used = cap, unit, 0
        self.cv = threading.Condition()

    def units(self, shape: tuple) -> int:
        return int(shape[0]) * (1 if self.unit == "img" else int(shape[-1]))

    @contextlib.contextmanager
    def __call__(self, units: int):
        t0 = time.perf_counter()
        with self.cv:
            while self.used and self.used + units > self.cap:
                self.cv.wait()
            self.used += units
        try:
            yield time.perf_counter() - t0
        finally:
            with self.cv:
                self.used -= units
                self.cv.notify_all()


def _borrowed_float32_ortvalue(ptr: int, dims: tuple, device_type: int, device_id: int):
    """借用调用方的 float32 缓冲；调用方须让数据活得比全部 session/OrtValue 久。

    DLPack 元数据单独用原生 malloc 分配，deleter 直接指向同一 CRT/libc 的 free。
    ORT 可在任意线程或解释器清理期间销毁它，不回调 Python，也不释放数据缓冲。
    mt 与 shape 在同一块里，均随最后一个 native OrtValue 释放，不依赖 Python keep 列表。
    capsule 仅在此函数内部创建并立即消费，不作为通用 DLPack producer 返回。
    """
    import ctypes

    class DLDevice(ctypes.Structure):
        _fields_ = [("device_type", ctypes.c_int32), ("device_id", ctypes.c_int32)]

    class DLDataType(ctypes.Structure):
        _fields_ = [("code", ctypes.c_uint8), ("bits", ctypes.c_uint8), ("lanes", ctypes.c_uint16)]

    class DLTensor(ctypes.Structure):
        _fields_ = [("data", ctypes.c_void_p), ("device", DLDevice), ("ndim", ctypes.c_int32),
                    ("dtype", DLDataType), ("shape", ctypes.POINTER(ctypes.c_int64)),
                    ("strides", ctypes.POINTER(ctypes.c_int64)), ("byte_offset", ctypes.c_uint64)]

    class DLManagedTensor(ctypes.Structure):
        _fields_ = [("dl_tensor", DLTensor), ("manager_ctx", ctypes.c_void_p), ("deleter", ctypes.c_void_p)]

    # Windows 不混用不同 CRT 的堆；POSIX 的默认进程符号提供同一对 malloc/free。
    runtime = ctypes.CDLL("ucrtbase.dll" if sys.platform == "win32" else None)
    runtime.malloc.argtypes, runtime.malloc.restype = [ctypes.c_size_t], ctypes.c_void_p
    runtime.free.argtypes, runtime.free.restype = [ctypes.c_void_p], None
    new_capsule = ctypes.pythonapi.PyCapsule_New
    new_capsule.restype, new_capsule.argtypes = ctypes.py_object, [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p]
    is_valid = ctypes.pythonapi.PyCapsule_IsValid
    is_valid.restype, is_valid.argtypes = ctypes.c_int, [ctypes.py_object, ctypes.c_char_p]
    size = ctypes.sizeof(DLManagedTensor) + len(dims) * ctypes.sizeof(ctypes.c_int64)
    storage = runtime.malloc(size)
    if not storage:
        raise MemoryError("DLPack 元数据分配失败")
    cap = None
    try:
        shape = (ctypes.c_int64 * len(dims)).from_address(storage + ctypes.sizeof(DLManagedTensor))
        shape[:] = dims
        mt = DLManagedTensor.from_address(storage)
        mt.dl_tensor = DLTensor(ptr, DLDevice(device_type, device_id), len(dims), DLDataType(2, 32, 1), shape, None, 0)
        mt.manager_ctx = None
        mt.deleter = ctypes.cast(runtime.free, ctypes.c_void_p).value
        # 不给 capsule Python 析构回调：转换失败由本函数释放，成功由 ORT 释放。
        cap = new_capsule(storage, b"dltensor", None)
        value = ort.capi._pybind_state.OrtValue.from_dlpack(cap, False)
    except BaseException:
        if cap is None or is_valid(cap, b"dltensor"):
            runtime.free(storage)
        raise
    return ort.OrtValue(value)


def gpu_initializers(path: str, device_id: int, keep: list) -> dict:
    """大权重（float32、≥1024 元素）各拷一份到用户持有的 CUDA 缓冲，供同模型各 session 共享。

    ORT 自己分配的 CUDA OrtValue 不能 add_initializer，所以走 cudart + DLPack。
    数据缓冲仍与进程同生共死；DLPack 元数据由 native OrtValue 单独拥有、正常释放。
    没有 onnx 包时不共享。keep 只需保留数据指针和 OrtValue，不再拥有 DLPack 元数据。
    """
    try:
        import onnx
        from onnx import numpy_helper
    except ImportError:
        print(json.dumps({"note": "没有 onnx 包，权重不共享"}), file=sys.stderr, flush=True)
        return {}
    import ctypes

    cudart = ctypes.CDLL("cudart64_13.dll" if sys.platform == "win32" else "libcudart.so.13")
    if cudart.cudaSetDevice(device_id):
        raise RuntimeError("cudaSetDevice 失败")
    out = {}
    for t in onnx.load(path).graph.initializer:
        arr = numpy_helper.to_array(t)
        if arr.dtype != np.float32 or arr.size < 1024:
            continue
        arr = np.ascontiguousarray(arr)
        ptr = ctypes.c_void_p()
        if cudart.cudaMalloc(ctypes.byref(ptr), ctypes.c_size_t(arr.nbytes)):
            raise RuntimeError(f"权重 {t.name} 分配不上显存")
        try:
            if cudart.cudaMemcpy(ptr, arr.ctypes.data_as(ctypes.c_void_p), ctypes.c_size_t(arr.nbytes), 1):
                raise RuntimeError(f"权重 {t.name} 拷不上显存")
            val = _borrowed_float32_ortvalue(ptr.value, arr.shape, 2, device_id)  # 2 = kDLCUDA
        except BaseException:
            cudart.cudaFree(ptr)
            raise
        keep += [ptr, val]
        out[t.name] = val
    return out


def serve_socket(a, hello: dict, infer, background: bool = False, on_close=None) -> int:
    """在 127.0.0.1 上等 N 个 worker 接进来，**共用一份模型和显存**。

    `background=True`（管道模式的 `--side-listen`）：accept 循环放后台线程、返回端口，由调用方写进自己的 ready 行；
    进程的生死仍由管道那条决定（stdin 读到 bye / EOF 主循环退出，后台线程是 daemon）。

    每个客户端一个线程；锁在 `infer` 里（2026-09-23 起）：只有池子的账一把，
    请求可以并发、同形状也行（原来一把全局锁，多 worker 的请求排成一队，decode-buffer）。客户端各带自己的共享内存（hello 里给名字和输入区大小），
    所以**张量不经过 socket**——socket 上只走一行 JSON。

    **握手要核能力**（2026-09-20 审计七）：客户端在 hello 里报 `want`（它要哪几个模型），
    服务端核对之后回 `{"ok":1,"models":[…]}`，少哪个就回 `{"ok":0,"missing":[…]}` 让它**自己去 spawn**。
    以前不核：`FLOWOCR_ORT_ADDR` 是进程级的，`OrtClient` 见到就接上去而不看自己要什么，
    于是 `--workers N` 时 det 的请求会打进只加载了 rec 的服务里。

    **父进程没了就退出**（`--exit-on-stdin-eof`）：这个进程握着显存，而
    `while True: accept()` 自己不会停——Ctrl-C 或驱动中途异常时会留一个吃着 GPU 的孤儿。

    ⚠ **判据是"stdin 读到 EOF"，不是"拿 pid 探活"**（2026-09-20 实测）：
    第一版写的是每 2 秒 `os.kill(parent_pid, 0)`，冒烟直接不过——
    Windows 上只要**还有谁握着那个进程的句柄**（父进程自己的 `Popen` 对象就算），
    进程都退出了 `OpenProcess` 照样成功、一个异常都不抛，于是看门狗永远不响。
    （顺带：signal 0 在 Windows 上确实只探活、不会误杀，这点验过。）
    stdin 那条没有这个问题：管道的写端跟着父进程的句柄表一起关，**EOF 是内核给的**。
    调用方要 `stdin=subprocess.PIPE` 并且一直不关它。
    """
    import os
    import socket
    import threading

    served = sorted(hello.get("models") or ())

    def watch_stdin() -> None:
        try:
            sys.stdin.buffer.read()      # 父进程活着就一直阻塞在这
        except Exception:
            pass
        os._exit(0)                      # 握着 CUDA 上下文，别走正常析构

    if a.exit_on_stdin_eof and not background:     # 旁路模式下 stdin 就是管道本身，由主循环读
        threading.Thread(target=watch_stdin, daemon=True).start()
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", max(a.listen, 0)))
    srv.listen(16)
    port = srv.getsockname()[1]

    def client(conn: "socket.socket") -> None:
        f = conn.makefile("rwb")
        shm = None
        try:
            h = json.loads(f.readline() or "{}").get("hello") or {}
            missing = [m for m in (h.get("want") or ()) if m not in served]
            try:                                      # 卡号也要对上：gpu:1 的客户端不能接到 0 号卡的服务上
                wrong_dev = bool(h.get("device")) and norm_device(h["device"]) != hello.get("device")
            except SystemExit:
                wrong_dev = True
            if missing or wrong_dev:
                # **不服务它**：客户端会退回自己 spawn 一个带全套模型、对的设备的 worker
                f.write((json.dumps({"ok": 0, "missing": missing, "models": served,
                                     "device": hello.get("device")}) + "\n").encode())
                f.flush()
                return
            shm = attach_shm(h["shm"])
            buf, in_bytes = shm.buf, int(h["in_bytes"])
            f.write((json.dumps({"ok": 1, "models": served, "providers": hello.get("providers"), "trt": hello.get("trt"),
                                 "pre": hello.get("pre"), "device": hello.get("device"), "ort_version": hello.get("ort_version"),
                                 "cublas_ws": hello.get("cublas_ws", "")}) + "\n").encode())
            f.flush()
            while True:
                line = f.readline()
                if not line:
                    break
                req = json.loads(line)
                if req.get("bye"):
                    break
                rep = infer(req, buf, in_bytes)  # 锁在 infer 里面：池子的账一把、每个 session 一把（不再一把全局锁）
                f.write((json.dumps(rep) + "\n").encode())
                f.flush()
        except Exception as e:                   # 一个客户端崩了不该带走服务
            try:
                f.write((json.dumps({"error": repr(e)}) + "\n").encode())
                f.flush()
            except Exception:
                pass
        finally:
            if shm is not None:
                shm.close()
            conn.close()
            if on_close is not None:
                on_close()

    def accept_loop() -> None:
        k = 0
        while True:
            conn, _ = srv.accept()
            k += 1
            # **关 Nagle**（2026-09-22，decode-buffer）：每次往返是"一行很短的 JSON 过去、一行很短的 JSON 回来"，
            # 正是 Nagle + 延迟确认最伤的形状（共享服务和管道模式的旁路连接都走这里）
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            threading.Thread(target=client, args=(conn,), daemon=True, name=f"conn-{k}").start()   # 线程名进时间线

    if background:
        threading.Thread(target=accept_loop, daemon=True, name="side-listen").start()
        return port
    print(json.dumps({**hello, "port": port}), flush=True)
    accept_loop()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", action="append", required=True, metavar="名字=路径")
    ap.add_argument("--shm", default="", help="共享内存的名字（调用方建、调用方销毁）；"
                                              "`--listen` 模式下由每个客户端在 hello 里给")
    ap.add_argument("--in-bytes", type=int, default=0, help="输入区字节数（输出从这之后开始）")
    ap.add_argument("--listen", type=int, default=-1, metavar="PORT",
                    help="**多客户端模式**：在 127.0.0.1 的这个端口上等（0 = 随便挑一个，"
                         "挑到的端口写在 ready 那行里）。N 个 worker 共用一个服务进程 = "
                         "**显存只有一份**——这是 `--workers` 能重新放宽的前提（owner 09-19）")
    ap.add_argument("--exit-on-stdin-eof", action="store_true",
                    help="`--listen` 模式下**读到 stdin 的 EOF 就自己退出**——"
                         "调用方要 `stdin=subprocess.PIPE` 并一直不关它，父进程一没，管道写端就关。"
                         "服务进程握着显存而 accept 循环自己不会停，不盯着就会留孤儿（审计七）。"
                         "⚠ **别改回拿 pid 探活**：Windows 上只要还有谁握着句柄，"
                         "进程退出了 `os.kill(pid, 0)` 照样成功——第一版就是这么挂的。"
                         "⚠ 也**别在 stdin 是 DEVNULL 时给这个参数**：那会立刻读到 EOF、当场退出")
    ap.add_argument("--device", default="gpu:0",
                    help="gpu:N = 必须用 N 号卡的 CUDA EP（这份 onnxruntime 没有 CUDA EP、或建 session 回落到 CPU，都退出码 3，不静默跑 CPU）；"
                         "`gpu` 等于 `gpu:0`；cpu = 只用 CPUExecutionProvider（NVIDIA 包里也是）。"
                         "由调用方按 run_ocr2 的 --device 传（Codex 审计 P1；卡号贯通是复审 P2：原来 gpu:1 到这里就成了 0 号卡）")
    ap.add_argument("--side-listen", action="store_true",
                    help="管道模式下**另开一个 127.0.0.1 端口**（写在 ready 行的 `port` 里），让同一个调用方再接几条连接进来、"
                         "同时有几个请求在途（`run_ocr2 --rec-inflight`，decode-buffer）。连接的握手和共享服务一样")
    ap.add_argument("--per-shape", type=int, default=1, help="1 = 每个形状一个 session（见文件头）")
    ap.add_argument("--share-mem", type=int, default=0,
                    help="1 = 同一个模型的 session 之间**共用显存**：①所有 session 共用一个进程级 CUDA arena"
                         "（`session.use_env_allocators`；默认每个 session 各自一份、各自涨到自己形状的激活峰值），"
                         "②权重在显存里只放一份（`gpu_initializers`）。15 个形状 4.8 GB → 1.5 GB、输出逐位相同（decode-buffer）。"
                         "`--gpu-mem-mb` 在这里才真是 arena 的总上限")
    ap.add_argument("--gpu-mem-mb", type=int, default=6144,
                    help="CUDA EP 的 arena 上限（MB，0 = 不限；`--share-mem 1` 时是共用 arena 的总上限）。资源预算是 flowocr 自己显存 6.5 GB"
                         "（owner 2026-10-05，架构说明「推理服务」）：句柄、cuBLAS 工作区、CUDA 上下文和 ffmpeg 硬解都在 arena 外，"
                         "所以它不是总量上限，是兜底——超了 ORT 当场报 OOM，而不是把卡吃光（探针一不小心就 24 个 session）。")
    ap.add_argument("--warm", default="",
                    help="起动后在后台预热的形状：`模型=批x宽,批x宽,…`（rec 的由管线按 recort.warm_shapes 生成）。每个新形状第一次要建 session 约 0.1 s + 首次 run 0.04~0.4 s，一趟 5 分钟段 18~26 个形状 = 4~6 s 压在 rec 的关键路径上；预热放在管线自己起动（建 det、探视频）的那几秒里做。同一个模型、同一份输入形状，不改读数")
    ap.add_argument("--threads", type=int, default=0,
                    help="每个 session 的 intra-op 线程数（0 = ORT 默认：物理核数）。CPU 路径上 det 和 rec 的 session 同时在跑、"
                         "各自一池物理核数的线程会互相挤（2026-09-25 量的）")
    ap.add_argument("--spin", type=int, default=1,
                    help="0 = 关掉 intra-op 线程池的自旋等待（`session.intra_op.allow_spinning`）。CPU 路径上几个 session 的线程池"
                         "轮空时自旋会把管线进程的 Python 线程挤饿（主线程提交段 4 → 16 s，2026-09-25）")
    ap.add_argument("--max-sessions", type=int, default=12,
                    help="per-shape 池子里最多留几个 session（0 = 不限）。超了**按最久没用的淘汰**："
                         "形状多的时候（探针扫宽 × 批）池子会线性吃显存，实测 24 个 session 吃到 15 GB。")
    ap.add_argument("--cudnn-conv-search", default="EXHAUSTIVE", choices=("EXHAUSTIVE", "HEURISTIC", "DEFAULT"),
                    help="CUDA EP 的 `cudnn_conv_algo_search`，默认同 ORT 自己的默认（EXHAUSTIVE）。⚠ **别拿 DEFAULT 当修法**"
                         "（2026-09-22 nightly 踩的）：ORT 1.26（CUDA 12 构建）+ cuDNN 9.9 在 sm_120（RTX 50 系）上，"
                         "EXHAUSTIVE / HEURISTIC 对 **fp16** 卷积报 `No valid engine configs for ConvFwd`；DEFAULT 能跑，"
                         "但它挑的是慢算法——rec 一批 360 ms，EXHAUSTIVE / HEURISTIC 是 15 ms，quick 六段墙钟 10 倍。"
                         "1.26 上 fp16 模型在这张卡上就是不可用，fp32 模型三种都正常；ORT 1.29（CUDA 13 构建）全都正常。")
    ap.add_argument("--arena-extend", default="pow2", choices=("pow2", "same"),
                    help="共用 arena 不够时怎么扩：pow2 = ORT 默认（按 2 的幂扩，一次最多 1 GB，扩出来的大块用不满也不还）；"
                         "same = 要多少扩多少（kSameAsRequested）。只影响 `--share-mem 1` 的共用 arena")
    ap.add_argument("--admit", default="",
                    help="准入：`模型=上限:单位,…`（单位 img = 批里几张图、bw = 批 × 张量宽），同一个模型同时在算的令牌之和不超过上限（`Admit`）。空 = 不限")
    ap.add_argument("--cublas-ws", default="",
                    help="设进程的 `CUBLAS_WORKSPACE_CONFIG`（如 `:4096:1`）：每个 (线程, session) 各有一套 cuBLAS 句柄和工作区，"
                         "默认工作区大，几十个形状 × 几根调用线程就是 1 GB 量级的显存（resource 计划）。空 = 不设；"
                         "环境里已经有这个变量时以环境的为准。生效的值在握手的 `cublas_ws`")
    ap.add_argument("--trt-model", action="append", default=[], metavar="名字=路径",
                    help="这个模型走 TensorRT（见文件头）：路径是建引擎用的 fp32 图（`--model` 给的那份是退回 CUDA EP 时用的，det 可以是半精度派生图）。"
                         "名字要在 `--model` 里，也是 profile 的名字（det / rec）。`--device cpu` 时不看")
    ap.add_argument("--trt-lib", default="", help="TensorRT 的 DLL 目录（`flowocr-trt install` 装的）")
    ap.add_argument("--trt-engines", default="", help="引擎缓存目录")
    ap.add_argument("--trt-version", default="", help="TensorRT 版本（进缓存名）")
    ap.add_argument("--trt-ort", default="", help="这版 TensorRT 对的 onnxruntime 主次版本（如 1.29，几个用逗号隔开）；推理服务的对不上就退回 CUDA EP")
    ap.add_argument("--trt-precision", default="fp32", choices=("fp32", "fp16"))
    ap.add_argument("--trt-sessions", default="", help="每个模型几个 TRT session：`det=1,rec=2`（没写的按 `TRT_SESSIONS`）")
    a = ap.parse_args()

    # 在 CUDA 库载入（preload_dlls、建 session）之前设：cuBLAS 建句柄时读它
    import os
    if a.cublas_ws and not os.environ.get("CUBLAS_WORKSPACE_CONFIG"):
        os.environ["CUBLAS_WORKSPACE_CONFIG"] = a.cublas_ws
    use_trt = bool(a.trt_model) and norm_device(a.device) != "cpu"
    if use_trt and a.trt_lib:
        # TensorRT 的 DLL 目录也在载 CUDA 库之前放进搜索路径（`trt_engine.setup_dlls`：建引擎资源是 nvinfer 按名字载的，要进 PATH）
        try:
            load_trt_engine().setup_dlls(a.trt_lib)
        except Exception as exc:                 # noqa: BLE001 —— 建 session 时再失败，按回退记
            print(json.dumps({"note": f"[trt] 设 DLL 目录失败：{exc!r}"}), file=sys.stderr, flush=True)
    if hasattr(ort, "preload_dlls"):
        ort.preload_dlls()                 # 不调它 CUDA EP 的 DLL 找不到、建 session 直接失败（CPU 包上是空操作）
        # 它在 Windows 上不载 NVRTC，cuDNN 每个形状挑算法时刷一行 "Could not locate nvrtc64_130_0.dll"（stderr 进临时文件）。
        # 故意不补：补上之后稳态耗时和输出都不变，每个形状首次 Run 多 1~1.7 s 运行时编译（defaults.md 没采纳的表）
    models = dict(m.split("=", 1) for m in a.model)
    # **设备由调用方说了算**（2026-09-22，Codex 审计 P1）：原来按"这份 onnxruntime 有没有 CUDA EP"自己挑，
    # 于是 NVIDIA 包里 `run_ocr2 --device cpu` 时 rec 照样上 GPU；CPU 包里要 GPU 却静默跑 CPU。
    # 现在：gpu 就必须用上 CUDA EP（没有 / 回落都退出码 3），cpu 就只给 CPU EP；hello 里报出实际用的
    a.device = norm_device(a.device)
    has_cuda = "CUDAExecutionProvider" in ort.get_available_providers()
    cuda_ok = a.device != "cpu"
    if cuda_ok and not has_cuda:
        print(json.dumps({"error": f"要 GPU，但这份 onnxruntime（{ort.__version__}）没有 CUDAExecutionProvider"
                                   f"（有的是 {ort.get_available_providers()}）；CPU 包请给 --device cpu"}), flush=True)
        return 3
    # 显存配额（owner 2026-09-19）：CUDA EP 的 arena 上限。给 0 就是老行为（不限）
    cuda_opts = {"cudnn_conv_algo_search": a.cudnn_conv_search}
    if cuda_ok:
        cuda_opts["device_id"] = int(a.device.split(":")[1])     # 卡号跟着 --device 走（多卡机器上别让 ORT 默认落 0 号卡）
    if a.gpu_mem_mb:
        cuda_opts["gpu_mem_limit"] = a.gpu_mem_mb * 1024 * 1024
    prov_opts = [("CUDAExecutionProvider", cuda_opts), "CPUExecutionProvider"] if cuda_ok else ["CPUExecutionProvider"]

    share = bool(a.share_mem) and cuda_ok
    if share:
        mi = ort.OrtMemoryInfo("Cuda", ort.OrtAllocatorType.ORT_ARENA_ALLOCATOR, cuda_opts["device_id"], ort.OrtMemType.DEFAULT)
        arena = ort.OrtArenaCfg({**({"max_mem": a.gpu_mem_mb * 1024 * 1024} if a.gpu_mem_mb else {}),
                                 **({"arena_extend_strategy": 1} if a.arena_extend == "same" else {})})
        ort.create_and_register_allocator_v2("CUDAExecutionProvider", mi,
                                             {k: str(v) for k, v in cuda_opts.items() if k != "gpu_mem_limit"}, arena)
    keep: list = []
    inits: dict = {}                             # 模型路径 -> 共享的显存权重
    opts: dict = {}
    opts_lock = threading.Lock()

    def options(path: str):
        """每个模型一份 SessionOptions（共享的权重按名字挂上去，不能串到别的模型）。加锁：走 TRT 的模型第一次用到它是在推理线程里
        （超 profile 退回 CUDA EP），两个线程同时进来会各拷一份共享权重。"""
        with opts_lock:
            return _options(path)

    def _options(path: str):
        if path not in opts:
            so = ort.SessionOptions()
            if a.threads > 0:
                so.intra_op_num_threads = a.threads
            if not a.spin:
                so.add_session_config_entry("session.intra_op.allow_spinning", "0")
            if share:
                so.add_session_config_entry("session.use_env_allocators", "1")
                if path not in inits:
                    inits[path] = gpu_initializers(path, cuda_opts["device_id"], keep)
                for name, val in inits[path].items():
                    so.add_initializer(name, val)
            opts[path] = so
        return opts[path]

    def trt_so():
        """TRT session 的 SessionOptions：线程 / 自旋跟 CUDA EP 的一样；不挂共用 arena 和共享权重（引擎自己管显存）。"""
        so = ort.SessionOptions()
        if a.threads > 0:
            so.intra_op_num_threads = a.threads
        if not a.spin:
            so.add_session_config_entry("session.intra_op.allow_spinning", "0")
        return so

    def note(msg: str) -> None:
        """起服务时的提示（建引擎要一两分钟）：ready 行之前打到 stdout，客户端逐行转给用户（`ortclient.read_ready`）。"""
        print(json.dumps({"note": msg}, ensure_ascii=False), flush=True)

    trt, trt_info = start_trt(a, models, cuda_opts, trt_so, note) if use_trt and cuda_ok else ({}, None)

    def make(path: str):
        """建一个 CUDA EP（或 CPU）session。ORT 的 python 封装在**建 session 失败和第一次 run 抛错时**都会打一行 "EP Error" 到 stdout、
        换成 CPU 重建再试（`_fallback_providers`）——stdout 是管道模式的回信通道，而且这和"有 CUDA EP 却没用上就退出码 3"是同一条规矩：
        错就报错，不静默换后端。所以 GPU 上建的时候就关掉（`enable_fallback=0`），建出来首选不是 CUDA 就抛。"""
        if not cuda_ok:
            return ort.InferenceSession(path, sess_options=options(path), providers=prov_opts)
        s = ort.InferenceSession(path, sess_options=options(path), providers=prov_opts, enable_fallback=0)
        if "CUDA" not in s.get_providers()[0]:
            raise RuntimeError(f"provider 回落到 {s.get_providers()[0]}（这份 onnxruntime 有 CUDA EP 却没用上）")
        return s

    base = {}
    providers = {}
    for name, path in models.items():
        if name in trt:                          # TRT 的池子就是基础 session（不另建一份 CUDA EP 的）
            base[name] = trt[name].sessions[0]
            providers[name] = "TensorrtExecutionProvider"
            continue
        try:
            s = make(path)
        except RuntimeError as e:
            if "provider 回落" not in str(e):
                raise
            print(json.dumps({"error": f"{name} 的 {e}"}, ensure_ascii=False), flush=True)
            return 3
        providers[name] = s.get_providers()[0]
        base[name] = s
    pool: dict = {}                              # (模型, 形状) -> session
    pool_lock = threading.Lock()
    n_built = n_evicted = 0
    build_sec = 0.0
    rp = load_recprep()
    tl = load_timeline()
    tl_lock = threading.Lock()

    def tl_dump() -> None:
        """时间线是累积的：每断开一个连接（和管道模式收尾时）整份重写一次，服务进程被杀时最多丢掉最后一个连接之后的那点。"""
        if tl is not None:
            with tl_lock:
                tl.dump()
    hello = {"ready": 1, "providers": providers, "trt": trt_info, "device": a.device, "ort_version": ort.__version__,
             "cublas_ws": os.environ.get("CUBLAS_WORKSPACE_CONFIG", ""),
             "models": {n: [i.name for i in s.get_inputs()] for n, s in base.items()}, "pre": rp is not None}

    n_warmed = 0

    def insert(key, made) -> tuple:
        """（持 pool_lock）放进池子：满了先按 LRU 淘汰一个——**淘汰在插入时做**，不在建之前（建在锁外，建的时候池子可能已经变了）。"""
        nonlocal n_evicted
        if a.max_sessions and len(pool) >= a.max_sessions:
            # dict 有序：最久没用的排在最前（正在别的线程里跑的那个由它自己握着引用，跑完放掉）
            pool.pop(next(iter(pool)))
            n_evicted += 1
        pool[key] = made
        return made

    def get_session(name: str, shape: tuple):
        """这个形状的 session：池子里有就用（挪到 LRU 末尾）；没有就**在锁外建**、锁内双查再放（2026-09-24：原来建在锁里，
        一个新形状的 0.1 s 会挡住另一个在途请求查池子）。两个线程同时建同一个形状时后到的那份丢掉。"""
        nonlocal n_built, build_sec
        key = (name, shape)
        with pool_lock:
            if key in pool:
                pool[key] = pool.pop(key)           # 用过就挪到末尾（LRU）
                return pool[key]
        t_b = time.perf_counter()
        made = make(models[name])
        with pool_lock:
            # **建了几个 / 淘汰了几次**（2026-09-19 复审第 2 条）：只有 `build_sec` 分不开
            # "每个新形状一次性的规划"（整片上摊薄）和"LRU 反复淘汰重建"（整片上持续付），
            # 而这两种对整片的预测是相反的。`evicted > 0` 就是后者在发生。
            build_sec += time.perf_counter() - t_b
            n_built += 1
            if key not in pool:
                return insert(key, made)
            pool[key] = pool.pop(key)
            return pool[key]

    def warm(spec: str) -> None:
        """后台预热（`--warm`）：逐个形状建 session + 用零输入跑一次（cuDNN 选算法在第一次 run 里），放进池子。
        池子里已经有的（真请求先到了）跳过；**只填空位、不淘汰**——预热不该把真在用的形状挤出去。出错只记一笔，不影响服务。"""
        nonlocal n_warmed
        try:
            name, _, shapes = spec.partition("=")
            if name not in models or name in trt:    # TRT 是一个动态形状的引擎，没有按形状的 session 要预热
                return
            h = int(base[name].get_inputs()[0].shape[2]) if isinstance(base[name].get_inputs()[0].shape[2], int) else 48
            for bw in shapes.split(","):
                b, w = (int(v) for v in bw.lower().split("x"))
                shape = (b, 3, h, w)
                key = (name, shape)
                with pool_lock:
                    if key in pool or (a.max_sessions and len(pool) >= a.max_sessions):
                        continue
                x = np.zeros(shape, np.float32)
                made = make(models[name])
                with admits[name](admits[name].units(shape)) if name in admits else contextlib.nullcontext():
                    made.run(None, {made.get_inputs()[0].name: x})
                with pool_lock:
                    if key not in pool and not (a.max_sessions and len(pool) >= a.max_sessions):
                        insert(key, made)
                        n_warmed += 1
        except Exception as exc:                     # noqa: BLE001
            print(f"[ort_server] 预热中断：{exc!r}", file=sys.stderr, flush=True)

    admits = {m: Admit(int(c.split(":")[0]), *(c.split(":")[1:2])) for m, c in (kv.split("=", 1) for kv in a.admit.split(",") if kv)}

    def infer(req: dict, buf, in_bytes: int) -> dict:
        """一次推理。**这段是两种模式共用的**——别抄第二份。"""
        name = req["model"]
        t_req = time.perf_counter()
        pre_ms, t_p = 0.0, t_req
        if "crops" in req:
            # **送的是原样的 uint8 裁剪**（2026-09-23，decode-buffer）：前处理在这个进程里做，不抢管线进程的 GIL
            t_p = time.perf_counter()
            crops, off = [], 0
            for h, w, c in req["crops"]:
                nb = h * w * c
                crops.append(np.ndarray((h, w, c), dtype=np.uint8, buffer=buf[off: off + nb]))
                off += nb
            x = rp.batch(crops, int(req["grid"]), int(req["batch"]))
            pre_ms = (time.perf_counter() - t_p) * 1000
        else:
            n = int(np.prod(req["shape"]))
            x = np.ndarray(tuple(req["shape"]), dtype=np.float32, buffer=buf[: n * 4])
        shape = tuple(x.shape)
        # **锁只罩着池子的账**（查 / 建 / LRU），推理本身不加锁（2026-09-23，decode-buffer）：
        # 原来共享服务一把全局锁罩住整次推理，三个 worker 的请求排成一队——wuwa-s2 上每段 rec 等 38~42 s、推理只有 11~14 s。
        # ORT 的 `InferenceSession.run` 本身允许多线程同时调，**同一个形状两个在途**也能叠起来（进程内实测批 1~16 吞吐 +22%~+38%，
        # 定文本的 argmax 逐位相同；`prob` 只在批 1 上差 ≤5e-4——批 1 的 kernel 串行两遍也会差，不是并发引起的）。
        t_session = time.perf_counter()
        tp = trt.get(name)
        if tp is not None:
            sess = None                             # 在 profile 里就从池子领，不在才建 CUDA EP 的按形状 session（`Pool.run` 的 fallback）
        elif a.per_shape:
            sess = get_session(name, shape)
        else:
            sess = base[name]
        session_ms = (time.perf_counter() - t_session) * 1000
        if tl is not None:
            if pre_ms:
                tl.span("ort_pre", t_p, t_p + pre_ms / 1000, name)
            tl.span("ort_sess", t_session, t_session + session_ms / 1000, name)
        t_wait = time.perf_counter()
        with admits[name](admits[name].units(shape)) if name in admits else contextlib.nullcontext(0.0) as admit_s:
            t0 = time.perf_counter()
            if tp is None:
                outs = sess.run(None, {sess.get_inputs()[0].name: x})
                waited = 0.0
            else:
                def cuda_run(x_):
                    s_ = get_session(name, shape)
                    return s_.run(None, {s_.get_inputs()[0].name: x_})
                outs, waited = tp.run(x, cuda_run)
            dt = time.perf_counter() - t0 - waited     # 等空闲 TRT session 的时间算"取 session"，不算推理
            session_ms += waited * 1000
        with pool_lock:
            counters = {"built": n_built, "evicted": n_evicted, "build_ms": build_sec * 1000, "warmed": n_warmed}
        if tp is not None:
            counters.update(trt_oop=tp.oop, trt_err=tp.err, **({"trt_err_first": tp.first_err} if tp.err else {}))
        if tl is not None and t0 - t_wait > 1e-4:
            tl.span("ort_wait", t_wait, t0, name)
        t_output = time.perf_counter()
        off = in_bytes
        meta = []
        for o in outs:
            o = np.ascontiguousarray(o)
            np.ndarray(o.shape, dtype=o.dtype, buffer=buf[off: off + o.nbytes])[...] = o
            meta.append([o.dtype.str, list(o.shape)])
            off += o.nbytes
        if tl is not None:
            tl.span("ort_run", t0 + waited, t0 + waited + dt, [name, list(shape)])
            tl.span("ort_out", t_output, None, name)
            tl.span("ort_req", t_req, None, name)
        return {"outs": meta, "infer_ms": dt * 1000, "shape": list(shape), "pre_ms": pre_ms, "admit_ms": admit_s * 1000,
                "session_ms": session_ms, "output_ms": (time.perf_counter() - t_output) * 1000, **counters}

    if a.warm and a.per_shape:
        for spec in a.warm.split(";"):
            threading.Thread(target=warm, args=(spec,), daemon=True, name="warm").start()
    if a.listen >= 0:
        return serve_socket(a, hello, infer, on_close=tl_dump)

    shm = attach_shm(a.shm)
    buf = shm.buf
    if a.side_listen:                            # 管道之外再开旁路连接（同一个调用方的第 2..N 个在途请求）
        hello = {**hello, "port": serve_socket(a, hello, infer, background=True, on_close=tl_dump)}
    print(json.dumps(hello), flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        if req.get("bye"):
            break
        try:
            rep_ = infer(req, buf, a.in_bytes)
        except Exception as e:                   # noqa: BLE001  报给调用方（它会 raise），别带着 traceback 死在 DEVNULL 的 stderr 里
            rep_ = {"error": repr(e)[:2000]}
        print(json.dumps(rep_), flush=True)
    shm.close()
    tl_dump()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
