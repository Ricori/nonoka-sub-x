"""TensorRT 引擎：profile、缓存名、建引擎（加锁、原子落盘）、建 session（TRT 后端计划）。

推理服务（`ort_server.py`）可能跑在**别的解释器**里、不 import flowocr，所以这个文件只用标准库 + onnxruntime + numpy，
目录全由调用方传进来；服务端按文件路径载入它（同 `recprep.py`）。装库、命令行在 `flowocr.trt`。
缓存名里的 ORT 版本取的是**跑这个文件的那个进程**的 onnxruntime——建引擎和读引擎都在推理服务那个解释器里做才对得上。
"""
from __future__ import annotations

import functools
import hashlib
import os
import shutil
import sys
import threading
import time
from pathlib import Path

PROFILES = {
    "det": ("x:1x3x32x32", "x:4x3x544x960", "x:4x3x960x960"),
    "rec": ("x:1x3x48x320", "x:4x3x48x320", "x:16x3x48x3200"),
}
"""每个模型的优化 profile（min / opt / max；TRT 后端计划第 1 步读代码定的）。
det：长边封 960、32 对齐、最小 32，16:9 横屏一律 544×960，批上限取 GPU 默认的 4：引擎按 profile 上限预留显存，上限 8 时一个 det session
建好就占约 1.8 GB、上限 4 时约 1.0 GB，4×544×960 的速度一样（2026-10-16，`tmp/meas/trt/detprof/`）；`--det-batch` 给到 5 以上的批按次退回 CUDA EP。
rec：高 48、宽 320..3200 十档、批 ≤16（批 × 宽 ≤ 20480 封顶，16×3200 实际不出现）。**只能一个 profile**：
ORT 1.29 的 TensorRT EP 建得出多 profile 的引擎，但跑的时候只用第一个，落进第二个的形状 enqueue 失败（`tmp/meas/trt/mprof/`）。"""
WORKSPACE = {"det": 2 << 30, "rec": 1 << 30, "lama": 2 << 30}
"""建引擎的 workspace 上限（lama 是阶段 5 的修补模型：输入形状固定、不要 profile，2 GB 照 inpainting 原型）：det fp32 在 profile 上限 8×960×960 时给 1 GB 建不出来（找不到实现）；上限 4 时 1 / 2 GB 都建得出、
session 显存和速度一样，留 2 GB 的余量；rec 1 GB 建得出（quick A/B 的临时钩子就是这档），
给大了引擎挑的实现会多占显存（rec fp32 4 GB 时一个 session 约 2.6 GB、1 GB 时约 1.8 GB，整卡读数，带噪声）。"""


class BuildError(RuntimeError):
    """建引擎这一步失败（不是建好之后跑不起来）：只有这种才值得补取 PTX 那份建引擎资源再试。"""


def setup_dlls(lib_dir: Path) -> None:
    """让 TensorRT EP 找得到 nvinfer 这几个库。要在建第一个 TRT session 之前调。
    Windows：建引擎资源是 nvinfer 运行时**按名字**载的，`add_dll_directory` 管不到，所以目录也放进 PATH（子进程继承）。
    Linux：`LD_LIBRARY_PATH` 只在进程启动时读，改了不管用——按完整路径、`RTLD_GLOBAL` 先把目录里的库都载进来，
    之后 TRT EP 按 soname 找 libnvinfer、nvinfer 按名字找建引擎资源，都认已载入的那份。"""
    d = str(lib_dir)
    if sys.platform != "win32":
        import ctypes
        libs = sorted(Path(lib_dir).glob("lib*.so*"), key=lambda p: (not p.name.startswith("libnvinfer.so"), p.name))
        for p in libs:                               # libnvinfer 先载：plugin / parser / 建引擎资源都依赖它
            ctypes.CDLL(str(p), mode=ctypes.RTLD_GLOBAL)
        return
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(d)
    if d not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


def profile_shapes(spec: str) -> list[tuple[int, ...]]:
    return [tuple(int(v) for v in item.split(":")[1].split("x")) for item in spec.split(",")]


def provider_options(kind: str, precision: str, prefix: str, cache: Path, device: int = 0) -> dict:
    """TensorRT EP 的选项（布尔值只认 'True' / 'False'，写错整个 EP 静默退回 CUDA EP——`make_session` 断言首选 provider 真是 TRT）。"""
    o = {"device_id": device, "trt_fp16_enable": str(precision == "fp16"), "trt_max_workspace_size": str(WORKSPACE[kind]),
         "trt_engine_cache_enable": "True", "trt_engine_cache_path": str(cache), "trt_engine_cache_prefix": prefix}
    if kind in PROFILES:                            # 没有 profile 的模型（lama）输入形状固定
        lo, opt, hi = PROFILES[kind]
        o.update(trt_profile_min_shapes=lo, trt_profile_opt_shapes=opt, trt_profile_max_shapes=hi)
    return o


def _sha256(p: Path) -> str:
    """文件 sha256；同一个进程里按 (路径, 大小, 修改时间) 记住（起服务时缓存名和参考文件名各要一次模型的哈希）。"""
    st = p.stat()
    return _sha256_of(str(p), st.st_size, st.st_mtime_ns)


@functools.lru_cache(maxsize=32)
def _sha256_of(path: str, _size: int, _mtime: int) -> str:
    p = Path(path)
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cache_prefix(kind: str, model: Path, precision: str, cap: tuple[int, int], trt_version: str) -> str:
    """引擎缓存的名字：模型文件 sha256 + TRT / ORT 版本 + 算力 + 精度 + 会改引擎的全部 EP 选项（去掉缓存路径和名字本身）。
    ORT 自己只按图结构算哈希，换了权重不换结构会读到旧引擎；以后加的 TRT 选项也自动进名字。只用字母数字下划线。"""
    import onnxruntime as ort
    opts = {k: v for k, v in provider_options(kind, precision, "", Path(), 0).items()
            if k not in ("trt_engine_cache_path", "trt_engine_cache_prefix", "device_id")}
    h = hashlib.sha256(repr(sorted(opts.items())).encode()).hexdigest()[:8]
    return (f"{kind}_{_sha256(model)[:16]}_t{trt_version.replace('.', '')}_o{ort.__version__.replace('.', '')}"
            f"_sm{cap[0]}{cap[1]}_{precision}_{h}")


def compute_cap(device: int = 0) -> tuple[int, int]:
    """这块卡的算力，问 CUDA 运行时（`onnxruntime.preload_dlls()` 之后才载得到 cudart；卡号和 ORT 的 `device_id` 一样看 `CUDA_VISIBLE_DEVICES`）。"""
    import ctypes
    rt = ctypes.CDLL("cudart64_13.dll" if sys.platform == "win32" else "libcudart.so.13")
    major, minor = ctypes.c_int(), ctypes.c_int()
    for attr, v in ((75, major), (76, minor)):           # cudaDevAttrComputeCapabilityMajor / Minor
        if rt.cudaDeviceGetAttribute(ctypes.byref(v), attr, device):
            raise RuntimeError(f"查不到第 {device} 块卡的算力（cudaDeviceGetAttribute）")
    return major.value, minor.value


def make_session(kind: str, model: Path, precision: str, prefix: str, cache: Path, device: int = 0, so=None, cuda: dict | None = None):
    """建 TRT session（CUDA EP 兜 TRT 吃不下的节点，选项 `cuda`），断言首选 provider 是 TRT；建的时候就关掉 ORT 出错时悄悄换 CPU 重建 / 重跑的回退
    （`enable_fallback=0`：不然建失败时它往 stdout 打 "EP Error" 再换 CPU）。"""
    import onnxruntime as ort
    so = so or ort.SessionOptions()
    s = ort.InferenceSession(str(model), sess_options=so, providers=[
        ("TensorrtExecutionProvider", provider_options(kind, precision, prefix, cache, device)),
        ("CUDAExecutionProvider", {**(cuda or {}), "device_id": device})], enable_fallback=0)
    if s.get_providers()[0] != "TensorrtExecutionProvider":
        raise RuntimeError(f"TensorRT EP 没生效：{s.get_providers()}")
    return s


def smoke(sess, kind: str) -> None:
    """每个 profile 的 min / opt / max 各跑一次（没有 profile 的按各输入的固定形状跑一次全零）：引擎能跑、浮点输出是有限数。"""
    import numpy as np
    ins = sess.get_inputs()
    feeds = ([{ins[0].name: np.zeros(shape, np.float32)} for spec in PROFILES[kind] for shape in profile_shapes(spec)]
             if kind in PROFILES else [{i.name: np.zeros(i.shape, np.float32) for i in ins}])
    for feed in feeds:
        outs = sess.run(None, feed)
        if not all(np.isfinite(o).all() for o in outs if o.dtype.kind == "f"):
            raise RuntimeError(f"{kind} {[f.shape for f in feed.values()]} 输出有 NaN / Inf")


if os.name == "nt":
    import msvcrt

    def _try_lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)      # 锁第一个字节（文件是空的也能锁）；被别人锁着抛 OSError

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class Lock:
    """引擎缓存名一把锁（`<名字>.lock`）：两个进程同时缺同一个引擎时只有一个在建，另一个每秒试一次、等到 `timeout`。
    用操作系统的文件锁（Windows `msvcrt.locking`、别处 `flock`），进程死了系统自动放锁。不用"刷修改时间的心跳 + 超时判陈锁"：
    ORT 建 session 时整段持有 GIL，TensorRT 又是在建 session 这一步建引擎（det 约 1 分钟），心跳线程跳不动，别人会把活着的锁当陈锁抢走。
    锁文件用完留在原处：删掉它，正在等的进程会锁到一个已经没有名字的文件上，和新建同名文件的进程同时进来。"""

    def __init__(self, path: Path, timeout: float = 1800.0, poll: float = 1.0) -> None:
        self.path, self.timeout, self.poll = path, timeout, poll
        self._fd: int | None = None

    def __enter__(self):
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT)
        t0 = time.monotonic()
        while True:
            try:
                _try_lock(fd)
                break
            except OSError:
                if time.monotonic() - t0 > self.timeout:
                    os.close(fd)
                    raise TimeoutError(f"等 {self.path} 超过 {self.timeout:.0f} s（另一个进程还在建？）") from None
                time.sleep(self.poll)
        self._fd = fd
        return self

    def __exit__(self, *exc) -> None:
        try:
            _unlock(self._fd)
        finally:
            os.close(self._fd)
            self._fd = None


def engine_ready(engine_dir: Path, prefix: str) -> bool:
    return any(engine_dir.glob(f"{prefix}*.engine"))


def engine_sha(engine_dir: Path, prefix: str) -> str:
    """引擎指纹（引擎文件 sha256 前 16 位，进 `_meta`）：建引擎按计时挑核，同一个缓存名重建出来的引擎可能换核、换读数。"""
    found = sorted(engine_dir.glob(f"{prefix}*.engine"))
    return _sha256(found[0])[:16] if found else ""


CHECK = Path(__file__).with_name("trt_check.npz")
CHECK_VERSION = 1
"""启动自检的输入：三行合成的英文字（48 高，OpenCV 自带的 Hershey 字体描边白字、深色底，uint8 `lines`，35 KB）。
不用随机输入：随机输入上 rec 各类别的分数挨得太近，argmax 本来就不稳。参考输出的名字带 profile 和这张图内容的短哈希（改了自动作废）；
`check_inputs` 的拼法改了要手动把 `CHECK_VERSION` 加一。"""
CHECK_TOL = {"float": (0.25, 0.005), "int": 0.02}
"""自检门限：浮点输出里和 CPU 参考差超过 0.25 的元素占比 ≤ 0.5%（det 概率图、rec 的置信度）；整数输出（rec 的类别）不一致的占比 ≤ 2%。
定得宽：要抓的是算错的引擎（TensorRT-RTX 那次的错图和参考差到 1.0），不是 TF32 / fp16 的抖动。本机读数（2026-10-16）：
TRT fp32 / fp16 对 CPU，det 概率图最大差 0.015 / 0.046；rec 类别全同，置信度在 4×320 那格有一个位置差 0.12 / 0.15（160 个里 1 个），别的 ≤ 0.02。"""


def check_inputs(kind: str) -> list:
    """profile 的 min / opt / max 三个形状各一份输入（float32、NCHW），批里第 i 张用第 `i % 3` 行字（`reference` 只算不同的那几张）。
    det：深色画布中间贴三行字（最近邻缩放到画布宽的 80%），按 ImageNet 均值方差归一；rec：三行字横着接起来铺满宽，按 0.5 / 0.5 归一。"""
    import numpy as np
    with np.load(CHECK) as z:
        lines = z["lines"]
    out = []
    for spec in PROFILES[kind]:
        n, _, h, w = profile_shapes(spec)[0]
        batch = []
        for i in range(n):
            k = i % len(lines)
            if kind == "rec":
                strip = np.concatenate([lines[(k + j) % len(lines)] for j in range(-(-w // lines.shape[2]))], axis=1)
                img = (strip[:h, :w].astype(np.float32) / 255 - 0.5) / 0.5
            else:
                page = np.concatenate(list(np.roll(lines, k, axis=0)), axis=0)
                s = min(0.8 * w / page.shape[1], 0.8 * h / page.shape[0])
                ph, pw = max(1, int(page.shape[0] * s)), max(1, int(page.shape[1] * s))
                small = page[np.arange(ph) * page.shape[0] // ph][:, np.arange(pw) * page.shape[1] // pw]
                img = np.full((h, w, 3), 40, np.uint8)
                y, x = (h - ph) // 2, (w - pw) // 2
                img[y: y + ph, x: x + pw] = small
                img = (img.astype(np.float32) / 255 - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225], np.float32)
            batch.append(img.transpose(2, 0, 1))
        out.append(np.ascontiguousarray(np.stack(batch), dtype=np.float32))
    return out


def ref_path(engine_dir: Path, kind: str, model: Path) -> Path:
    """CPU 参考输出的文件：只跟模型和自检输入（profile 的三个形状、自检图、拼法版本）有关，fp32 / fp16 两个引擎共用一份。"""
    h = hashlib.sha256(repr(PROFILES[kind]).encode() + CHECK.read_bytes()).hexdigest()[:8]
    return engine_dir / f"{kind}_{_sha256(model)[:16]}_c{CHECK_VERSION}_{h}.ref.npz"


def ensure_ref(kind: str, model: Path, engine_dir: Path, log=print) -> Path:
    """没有就在 CPU EP 上算一份参考输出（批里只算、只存不同的那几张，逐张算，最大形状也只占几百 MB 内存；本机 det 约 4 s）。
    先写 `.part-<pid>` 再改名：两个进程同时算出来的是同一份，谁后改名都一样，不用锁；Windows 上目标正被别人读着时改名失败，就用别人那份。"""
    import numpy as np
    import onnxruntime as ort
    p = ref_path(engine_dir, kind, model)
    if p.is_file():
        return p
    log(f"[trt] 算 {kind} 的 CPU 参考输出（启动自检用，只算一次）")
    so = ort.SessionOptions()
    so.log_severity_level = 3
    s = ort.InferenceSession(str(model), sess_options=so, providers=["CPUExecutionProvider"])
    name = s.get_inputs()[0].name
    got = {}
    for j, x in enumerate(check_inputs(kind)):
        uniq = [s.run(None, {name: x[i: i + 1]}) for i in range(min(len(x), 3))]
        for o in range(len(uniq[0])):
            got[f"s{j}_o{o}"] = np.concatenate([u[o] for u in uniq])
    tmp = p.with_name(f"{p.name}.part-{os.getpid()}")
    with open(tmp, "wb") as f:
        np.savez_compressed(f, **got)
    try:
        os.replace(tmp, p)
    except OSError:
        tmp.unlink(missing_ok=True)
        if not p.is_file():
            raise
    return p


def selfcheck(sess, kind: str, ref: Path) -> str:
    """用 TRT session 跑 `check_inputs` 的三个形状、对 CPU 参考（门限 `CHECK_TOL`）。过了返回空串，不过返回哪个形状哪个输出差多少。"""
    import numpy as np
    name = sess.get_inputs()[0].name
    with np.load(ref) as z:
        want = dict(z)
    (d_hi, frac_max), int_max = CHECK_TOL["float"], CHECK_TOL["int"]
    for j, x in enumerate(check_inputs(kind)):
        for o, got in enumerate(sess.run(None, {name: x})):
            w = want.get(f"s{j}_o{o}")
            if w is not None and len(w) == min(len(x), 3):
                w = w[np.arange(len(x)) % len(w)]            # 参考只存了不同的那几张（批里第 i 张 = 第 i % 3 张）
            if w is None or w.shape != got.shape:
                return f"{kind} {x.shape} 输出 {o} 的形状 {got.shape} 和参考 {None if w is None else w.shape} 对不上"
            if got.dtype.kind == "f":
                bad = float(np.mean(~(np.abs(got - w) <= d_hi)))     # NaN 也算差
                if bad > frac_max:
                    return f"{kind} {x.shape} 输出 {o}：{bad:.2%} 的元素和 CPU 参考差超过 {d_hi}（门限 {frac_max:.1%}）"
            else:
                bad = float(np.mean(got != w))
                if bad > int_max:
                    return f"{kind} {x.shape} 输出 {o}：{bad:.2%} 的类别和 CPU 参考不一致（门限 {int_max:.0%}）"
    return ""


class Pool:
    """一个模型的 TRT session 小池子（推理服务用）。同一个 TRT session 上的并发推理是串行的，所以调用线程各领一个，
    领不到就排队；池子多大由调用方定（默认 det、rec 各 1 个，`ort_server.TRT_SESSIONS`）。
    形状不在 profile 里（`fits`）、或 TRT 跑的时候抛错，就交给调用方给的 `fallback`（CUDA EP 的按形状 session）并分开计数：
    `oop` = 超 profile，`err` = 运行中报错（第一次的错误留在 `first_err`）。"""

    def __init__(self, kind: str, sessions: list) -> None:
        import queue
        self.kind, self.sessions = kind, list(sessions)
        self.lo, self.hi = profile_shapes(PROFILES[kind][0])[0], profile_shapes(PROFILES[kind][2])[0]
        self.input = sessions[0].get_inputs()[0].name
        self.free: queue.Queue = queue.Queue()
        for s in sessions:
            self.free.put(s)
        self.oop = self.err = 0
        self.first_err = ""
        self.lock = threading.Lock()

    def fits(self, shape: tuple) -> bool:
        return len(shape) == len(self.lo) and all(a <= int(s) <= b for a, s, b in zip(self.lo, shape, self.hi))

    def run(self, x, fallback) -> tuple[list, float]:
        """跑一次，返回 `(输出, 等空闲 session 的秒数)`。`fallback(x)` 返回输出。"""
        if not self.fits(x.shape):
            with self.lock:
                self.oop += 1
            return fallback(x), 0.0
        t0 = time.perf_counter()
        s = self.free.get()
        waited = time.perf_counter() - t0
        try:
            return s.run(None, {self.input: x}), waited
        except Exception as e:                   # noqa: BLE001 —— 退回 CUDA EP，计数进 `_meta`
            with self.lock:
                self.err += 1
                self.first_err = self.first_err or f"{type(e).__name__}: {e}"[:500]
        finally:
            self.free.put(s)
        return fallback(x), waited


def build(kind: str, model: Path, precision: str, engine_dir: Path, cap: tuple[int, int], trt_version: str,
          device: int = 0, log=print, make=make_session) -> str:
    """缓存里没有这个引擎就建：建到 `engines/.build-<pid>-<名字>/`、跑一遍冒烟，再逐个文件 `os.replace` 进 `engines/`，
    `.engine` 最后放（别的进程看见它就认为缓存齐了；ORT 读缓存还要旁边的 `.profile`）。建到一半被杀不留半个引擎；
    拿到锁之后先清掉这个名字的残留建引擎目录（锁在手里，它们只能是被杀的进程留下的）。返回缓存名。
    调用方要先 `setup_dlls()`、`onnxruntime.preload_dlls()`。`make` 只给守卫换掉。"""
    prefix = cache_prefix(kind, model, precision, cap, trt_version)
    engine_dir.mkdir(parents=True, exist_ok=True)
    if engine_ready(engine_dir, prefix):
        return prefix
    with Lock(engine_dir / f"{prefix}.lock"):
        if engine_ready(engine_dir, prefix):
            return prefix
        for old in engine_dir.glob(f".build-*-{prefix}"):
            shutil.rmtree(old, ignore_errors=True)
        tmp = engine_dir / f".build-{os.getpid()}-{prefix}"
        tmp.mkdir()
        try:
            log(f"[trt] 建 {kind} {precision} 引擎（{model.name}，约 0.5~1.5 分钟）")
            t0 = time.perf_counter()
            try:
                sess = make(kind, model, precision, prefix, tmp, device)
            except Exception as e:                  # noqa: BLE001 —— 建不出来单独报，install 据此补取 PTX 那份资源
                raise BuildError(f"{kind} {precision} 引擎建不出来：{type(e).__name__}: {e}") from e
            smoke(sess, kind)
            del sess
            made = sorted(tmp.iterdir())
            if not any(p.suffix == ".engine" for p in made):
                raise RuntimeError(f"建完没在 {tmp} 里找到 .engine（{[p.name for p in made]}）")
            for p in sorted(made, key=lambda p: p.suffix == ".engine"):
                os.replace(p, engine_dir / p.name)
            log(f"[trt] {kind} {precision} 引擎建好（{time.perf_counter() - t0:.0f} s）")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return prefix
