"""TensorRT 后端要的库和引擎（TRT 后端计划；owner 2026-10-16 定做成正式后端，默认 TRT fp32、fp16 作开关）。

**库**：推理仍用 onnxruntime-gpu 自带的 TensorRT EP，它要 TensorRT 自己的几个 DLL。NVIDIA 只在自己的包索引上发
`tensorrt-cu13-libs` 的 wheel（PyPI 上同名包是个空壳 sdist），整包 1.9 GB——大头是按算力分的八份"建引擎资源"，
本机只用得上一份。所以不整包装：wheel 是 zip，服务器支持范围请求，先取文件尾的中央目录、再只取要的成员
（`RangeFile` 把 HTTP 范围请求包成可 seek 的文件，交给标准库 `zipfile` 读——本地 wheel（`--from`）走同一条路）。
要的成员：nvinfer / nvinfer_plugin / nvonnxparser 三个 DLL，本机算力那一份建引擎资源（`ARCHS`，没有专属文件的算力取 PTX 那份），
加许可证。每个成员的 sha256 钉在 `MEMBERS`，`zipfile` 读完一个成员顺带核 CRC32。**别从 wheel 的 `RECORD` 抄**：NVIDIA 生成 RECORD 之后
又给每个 DLL 补了 Authenticode 签名（大 10,352 字节），RECORD 里的大小和哈希全对不上；uv / pip 装的时候不核，所以装出来的也是签过名的那份。
不进 `pyproject.toml` 的依赖：`uv sync` 不碰它，项目 venv 不变大。

**放在哪**：`paths.trt_root()/<TRT 版本>/`（`FLOWOCR_TRT` > 数据根的 `trt/`），`lib/` 放 DLL，`engines/` 放引擎缓存。
先写 `.part` 再改名；`remove` 只删各版本目录下的这两个子目录（`FLOWOCR_TRT` 指错了也不会删到别的东西）。
引擎怎么建、缓存怎么起名在 `flowocr.extract.trt_engine`（推理服务按文件路径载入它，不 import flowocr）。

    flowocr-trt install [--from <wheel>] [--precision fp32|fp16|both] [--no-build]
    flowocr-trt status
    flowocr-trt remove

平台：Windows 和 Linux 的 x86-64（Linux 2026-10-17 补，WSL2 上验过）。Linux 用 manylinux 的 wheel，成员是 `.so`；
它的 sha256 取自 wheel 的 `RECORD`——Linux 的 `.so` 没有事后补签名，RECORD 和实际字节对得上（本机那几份 install 时逐个核过，
别的算力的那几份没下过、只信 RECORD）。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

from flowocr import paths
from flowocr.extract import trt_engine as E

TRT_VERSION = "10.16.1.11"
ORT_TRT = {"1.29": TRT_VERSION}
"""onnxruntime-gpu（CUDA 13）的 TensorRT EP 编译时对的 TensorRT 版本：ORT 升级时这里要跟着查（对不上就不装、不用）。"""
INDEX = os.environ.get("FLOWOCR_TRT_INDEX", "https://pypi.nvidia.com/tensorrt-cu13-libs/")
PLATFORM = "linux" if sys.platform.startswith("linux") else sys.platform
WHEELS = {"win32": f"tensorrt_cu13_libs-{TRT_VERSION}-py3-none-win_amd64.whl",
          "linux": f"tensorrt_cu13_libs-{TRT_VERSION}-py3-none-manylinux_2_28_x86_64.whl"}
"""各平台（都是 x86-64）的 wheel；`PLATFORM` 不在里面就是不支持。"""
WHEEL = WHEELS.get(PLATFORM, "")
LIB = "tensorrt_libs/"
NAMES = {"win32": ("{}_10.dll", "nvinfer_builder_resource_{}_10.dll"),
         "linux": ("lib{}.so.10", "libnvinfer_builder_resource_{}.so." + TRT_VERSION.rsplit(".", 1)[0])}
"""成员的文件名：（三个库、建引擎资源）。Linux 的建引擎资源带三段版本号。不支持的平台按 Windows 的名字算（`have` 一律是"缺"）。"""
LICENSE = f"tensorrt_cu13_libs-{TRT_VERSION}.dist-info/LICENSE.txt"
LICENSE_URL = "https://docs.nvidia.com/deeplearning/tensorrt/latest/reference/sla.html"
"""NVIDIA 官网的 TensorRT 许可（最新版本）：install 下载前打出来，用户下载前能读到（owner 2026-10-16：只提示、不拦）。"""
MEMBERS_BY = {"win32": {
    LICENSE: ("c86915fd95bbefbdda3135aace3e6ad6b4612846dcbdbc5d2f5a38b139dd88b4", 47141),
    LIB + "nvinfer_10.dll": ("73fd99ba7448ebe7b75f3a97bca5fd996f166fa75760f616c0b0eda5c2f7005b", 375812208),
    LIB + "nvinfer_plugin_10.dll": ("f0e70dc33c157c939465a76a328c7fec0c72be005b7cac6639134ec8502f7a42", 43440240),
    LIB + "nvonnxparser_10.dll": ("4474757aac9e6abe12b3086fa2ed95f97e93e7b26cc5e89dbada2670842f471e", 3268208),
    LIB + "nvinfer_builder_resource_ptx_10.dll": ("29fcebf04f2bf6ac6a99b805953d02d0442eeafe0f45fe9fa162e43537e4285b", 241134192),
    LIB + "nvinfer_builder_resource_sm75_10.dll": ("ac67f4219db18e5e24bb2022f746d6b61804cd4278b57f7908734317f0bb05d2", 116075120),
    LIB + "nvinfer_builder_resource_sm80_10.dll": ("edd5cea62a070fdabe6826aa4e51cbd123e045324758ee54f7254e2d63bd1afd", 186690160),
    LIB + "nvinfer_builder_resource_sm86_10.dll": ("35331ca0164b785b85a9f3d48bb7be1195af9d5ccb760033c0854e133f53b621", 176011888),
    LIB + "nvinfer_builder_resource_sm89_10.dll": ("5126cb3feb13ef2f5841fb30085cf304fa0dfacb7645078dd77c7fc8eeb125ad", 185069168),
    LIB + "nvinfer_builder_resource_sm90_10.dll": ("2fb60ef2998a876027fc4b99366af82d3c23fbfa5d4b5dbc8c82325d8ca66dc3", 453921904),
    LIB + "nvinfer_builder_resource_sm100_10.dll": ("c88d5abfccc601194b2a32c9e43956eb0c3435ee07114493d862e7c12640c17d", 282830960),
    LIB + "nvinfer_builder_resource_sm120_10.dll": ("3aa2d721ffa793cd5dc1088f4c1b98c29155aa64ad2fc1d5c12daa03f2a0e023", 262017136),
}, "linux": {
    LICENSE: ("64bd290f0251405f783ba1d2e155c500542be69795e51147a1d9f11a57bda8cc", 46961),
    LIB + "libnvinfer.so.10": ("6637a7f7117f80281c206ee79ebaf2a8b7f0f4d6b62a375856839d9260a4105a", 662824456),
    LIB + "libnvinfer_plugin.so.10": ("9400daa92803729f554b864c1f9291cf0778305460c9ab551480a332feaf13a3", 44554888),
    LIB + "libnvonnxparser.so.10": ("09cda8eddce7f53f039fbffc3c30a4d066dc7759464331ca8ccf3fa461b48cf7", 5058816),
    LIB + "libnvinfer_builder_resource_ptx.so.10.16.1": ("27877c08502c25d7fe5ed5397787d125084632392cf3d9110ea96a5ac39e185d", 239920952),
    LIB + "libnvinfer_builder_resource_sm75.so.10.16.1": ("e735551edd3b63342d76e1bbe0fc873496f6ee99878af33d4c8c798d38e05b97", 115939120),
    LIB + "libnvinfer_builder_resource_sm80.so.10.16.1": ("339c7a4979fa52410040977ead36be963e0843ca9197f1cd08d3df3d312d8d4c", 186554160),
    LIB + "libnvinfer_builder_resource_sm86.so.10.16.1": ("922796875978139aac04c625f06b53665faafa7caca88cf3b690f54e054564de", 175875888),
    LIB + "libnvinfer_builder_resource_sm89.so.10.16.1": ("00b9ec534cb2489d8046ccd439fbbbd512590adb3b6ca509cd8dac6c507a44d6", 184932144),
    LIB + "libnvinfer_builder_resource_sm90.so.10.16.1": ("b90ca522993573b4f88212978f6a9d6306ddea5c23664e6171226bb490f87296", 453793584),
    LIB + "libnvinfer_builder_resource_sm100.so.10.16.1": ("589277c3b8a525923905ace33599d88330317db017db7b3f61ac13e976af4c88", 282760992),
    LIB + "libnvinfer_builder_resource_sm120.so.10.16.1": ("37d5e476db50daa4088c25ebf6ede0e1eff14ab1aa8df03cf07407a07eeec07f", 261945120),
}}
MEMBERS = MEMBERS_BY.get(PLATFORM, {})
"""本平台的成员：wheel 里的路径 -> (sha256, 大小)。"""
_LIB_NAME, _RES_NAME = NAMES.get(PLATFORM, NAMES["win32"])
COMMON = tuple(LIB + _LIB_NAME.format(n) for n in ("nvinfer", "nvinfer_plugin", "nvonnxparser"))
ARCHS = {(7, 5): "sm75", (8, 0): "sm80", (8, 6): "sm86", (8, 9): "sm89", (9, 0): "sm90", (10, 0): "sm100", (12, 0): "sm120"}
"""算力 -> 专属的建引擎资源。wheel 里只有这几份；别的（8.7、10.3、12.1……）取 PTX 那份（`resource_for`）。
"只留本机那份能建引擎"只在 sm120 上验过，所以 install 一定跑一次建引擎冒烟，建不出来就补取 PTX 那份再试。"""
MIN_CAP = (7, 5)
"""TensorRT 10 支持的最低算力（和 ORT 的 CUDA EP 一样从 Turing 起）。"""


def root() -> Path:
    return paths.trt_root() / TRT_VERSION


def lib_dir() -> Path:
    return root() / "lib"


def engine_dir() -> Path:
    return root() / "engines"


def resource_for(cap: tuple[int, int]) -> str:
    """算力 -> 建引擎资源的成员名。低于 `MIN_CAP` 不支持（抛 `RuntimeError`）。"""
    if cap < MIN_CAP:
        raise RuntimeError(f"算力 {cap[0]}.{cap[1]} 低于 TensorRT 10 的下限 {MIN_CAP[0]}.{MIN_CAP[1]}，用 CUDA EP")
    return LIB + _RES_NAME.format(ARCHS.get(cap, "ptx"))


def members_for(cap: tuple[int, int], ptx: bool = False) -> list[str]:
    """本机要取的成员：三个 DLL + 本机那份建引擎资源（`ptx=True` 再加 PTX 那份）+ 许可证。"""
    out = [*COMMON, resource_for(cap), LICENSE]
    if ptx and LIB + _RES_NAME.format("ptx") not in out:
        out.insert(-1, LIB + _RES_NAME.format("ptx"))
    return out


def smi_index(device: int) -> str:
    """ORT 的 `device_id` 看 `CUDA_VISIBLE_DEVICES`、`nvidia-smi --id` 不看：设了且是数字列表时换成物理编号。"""
    vis = [v.strip() for v in os.environ.get("CUDA_VISIBLE_DEVICES", "").split(",") if v.strip()]
    if vis and device < len(vis) and vis[device].isdigit():
        return vis[device]
    return str(device)


def compute_cap(device: int = 0) -> tuple[int, int]:
    """GPU 的算力（`nvidia-smi --query-gpu=compute_cap`，驱动自带，不用 CUDA 运行时）。查不到抛 `RuntimeError`（话说清楚，不甩 traceback）。"""
    try:
        r = subprocess.run(["nvidia-smi", f"--id={smi_index(device)}", "--query-gpu=compute_cap", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"跑不了 nvidia-smi（{e}）：没装 N 卡驱动？") from e
    try:
        major, minor = r.stdout.strip().split(".")
        return int(major), int(minor)
    except ValueError:
        raise RuntimeError(f"nvidia-smi 查不到第 {device} 块卡的算力：{(r.stdout + r.stderr).strip()[:200]}") from None


def ort_compatible() -> str:
    """装着的 onnxruntime 对得上 `TRT_VERSION` 就返回空串，否则返回原因。"""
    try:
        import onnxruntime as ort
    except ImportError:
        return "没装 onnxruntime"
    mm = ".".join(ort.__version__.split(".")[:2])
    if ORT_TRT.get(mm) != TRT_VERSION:
        return f"onnxruntime {ort.__version__} 对的 TensorRT 版本没登记（ORT_TRT 里只有 {sorted(ORT_TRT)}）"
    if "TensorrtExecutionProvider" not in ort.get_available_providers():
        return f"onnxruntime {ort.__version__} 没带 TensorRT EP（要 onnxruntime-gpu）"
    return ""


class RangeFile(io.RawIOBase):
    """只读、可 seek 的文件，内容由 `fetch(start, end)`（含两端）按需取，每次至少取 `chunk` 字节并缓存这一段——
    `zipfile` 读成员时是顺序的小块读，不缓存的话一个 376 MB 的成员要发十万次请求。"""

    def __init__(self, fetch, size: int, chunk: int = 16 << 20) -> None:
        super().__init__()
        self._fetch, self._size, self._chunk = fetch, size, chunk
        self._pos, self._buf_at, self._buf = 0, 0, b""
        self.requests = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, off: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self._size}[whence]
        self._pos = max(0, base + off)
        return self._pos

    def readinto(self, b) -> int:
        n = min(len(b), self._size - self._pos)
        if n <= 0:
            return 0
        lo = self._pos - self._buf_at
        if not (0 <= lo and lo + n <= len(self._buf)):
            end = min(self._size, self._pos + max(n, self._chunk)) - 1
            self._buf, self._buf_at = self._fetch(self._pos, end), self._pos
            self.requests += 1
            if len(self._buf) != end - self._pos + 1:
                raise OSError(f"范围请求 {self._pos}-{end} 只回了 {len(self._buf)} 字节")
            lo = 0
        b[:n] = self._buf[lo:lo + n]
        self._pos += n
        return n


def _http_fetch(url: str, tries: int = 4):
    """一次范围请求；网络错或回少了重试（间隔 2 / 4 / 8 s），服务器不支持范围请求直接报。"""
    def fetch(start: int, end: int) -> bytes:
        import time
        for k in range(tries):
            req = urllib.request.Request(url, headers={"User-Agent": "flowocr", "Range": f"bytes={start}-{end}"})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    if r.status != 206:
                        raise RuntimeError(f"{url} 不支持范围请求（HTTP {r.status}）；整包下载后用 --from")
                    data = r.read()
                if len(data) == end - start + 1:
                    return data
                err: Exception = OSError(f"范围请求 {start}-{end} 只回了 {len(data)} 字节")
            except OSError as e:                    # urllib 的网络错都是 OSError
                err = e
            if k + 1 < tries:
                time.sleep(2 << k)
        raise err
    return fetch


def open_remote(url: str) -> RangeFile:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "flowocr"})
    with urllib.request.urlopen(req, timeout=60) as r:
        size = int(r.headers["Content-Length"])
    return RangeFile(_http_fetch(url), size)


def have(member: str, deep: bool = False) -> bool:
    """`lib/` 里已有这个成员：大小对上（`deep` 再核 sha256）。"""
    if member not in MEMBERS:
        return False
    want_sha, want_size = MEMBERS[member]
    p = lib_dir() / Path(member).name
    if not p.is_file() or p.stat().st_size != want_size:
        return False
    return not deep or E._sha256(p) == want_sha


def extract(zf: zipfile.ZipFile, member: str, dst_dir: Path, log=print) -> Path:
    """从 wheel 里取一个成员到 `dst_dir`：先写 `.part-<pid>`（两个 install 同时跑不互相覆盖），sha256 对上再改名；`zipfile` 读到成员尾顺带核 CRC32（不对抛 `BadZipFile`）。"""
    want_sha, want_size = MEMBERS[member]
    dst = dst_dir / Path(member).name
    tmp = dst.with_name(f"{dst.name}.part-{os.getpid()}")
    h, done, step = hashlib.sha256(), 0, max(1, want_size // 10)
    try:
        with zf.open(member) as src, open(tmp, "wb") as f:
            for chunk in iter(lambda: src.read(1 << 20), b""):
                f.write(chunk)
                h.update(chunk)
                if (done + len(chunk)) // step > done // step:
                    log(f"[trt] {dst.name} {100 * (done + len(chunk)) // want_size}%")
                done += len(chunk)
        if h.hexdigest() != want_sha or done != want_size:
            raise RuntimeError(f"{member} 的 sha256 / 大小是 {h.hexdigest()} / {done}，钉的是 {want_sha} / {want_size}——没装")
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
    return dst


def fetch_libs(members: list[str], wheel: str | None = None, log=print) -> list[Path]:
    """把缺的成员取进 `lib/`（`wheel` = 本地 wheel 路径；空 = 从包索引范围下载）。已有且大小对的跳过。"""
    lib_dir().mkdir(parents=True, exist_ok=True)
    todo = [m for m in members if not have(m)]
    if not todo:
        return []
    if wheel:
        f = open(wheel, "rb")
    else:
        log(f"[trt] 从 {INDEX}{WHEEL} 范围下载 {len(todo)} 个成员（共 {sum(MEMBERS[m][1] for m in todo) / 2**20:.0f} MB 解开后）")
        f = open_remote(INDEX + WHEEL)
    out = []
    with f, zipfile.ZipFile(f) as zf:
        for m in todo:
            out.append(extract(zf, m, lib_dir(), log))
    return out


def version_dirs() -> list[Path]:
    """`trt_root()` 下各 TensorRT 版本的目录（认得出的：名字是版本号、下面有 `lib/` 或 `engines/`）。升级 `TRT_VERSION` 后旧版本的也在里面。"""
    r = paths.trt_root()
    return sorted(d for d in r.iterdir() if d.is_dir() and re.fullmatch(r"\d+(\.\d+){1,3}", d.name)
                  and ((d / "lib").is_dir() or (d / "engines").is_dir())) if r.is_dir() else []


def ours(d: Path) -> bool:
    """`lib/` 里只有 TensorRT 的 DLL 和许可证、`engines/` 里只有引擎缓存的文件（含锁、参考输出、建到一半的临时目录）：
    `remove` 只删这样的目录，`FLOWOCR_TRT` 指错到别人的目录时认不出就不删。"""
    def known(f: Path) -> bool:
        if ".part-" in f.name or f.name.endswith(".part"):
            return True
        if d.name == "lib":
            return (f.name in {Path(m).name for ms in MEMBERS_BY.values() for m in ms}
                    or bool(re.fullmatch(r"(lib)?nv(infer|onnxparser)\w*(\.dll|\.so(\.\d+)*)", f.name)))
        return f.suffix in (".engine", ".profile", ".lock") or f.name.endswith(".ref.npz") or (f.is_dir() and f.name.startswith(".build-"))
    return all(known(f) for f in d.iterdir())


def stale_install() -> str:
    """只装着别的 TensorRT 版本的库时返回那个版本号（升级 flowocr 后配对的版本变了），否则空串。"""
    old = [d.name for d in version_dirs() if d != root() and (d / "lib").is_dir()]
    return old[-1] if old else ""


def remove(log=print) -> list[Path]:
    """删各版本目录下的 `lib/` 和 `engines/`，版本目录空了也删；`trt_root()` 下别的东西不碰。"""
    gone = []
    for v in version_dirs():
        for d in (v / "lib", v / "engines"):
            if d.exists():
                if not ours(d):
                    log(f"[trt] {d} 里有不认识的文件，不删（FLOWOCR_TRT 指对了吗？）")
                    continue
                try:
                    shutil.rmtree(d)
                except OSError as e:
                    log(f"[trt] 删不掉 {d}（{e.strerror}：多半有 flowocr 正在用它，关掉再删）")
                    continue
                gone.append(d)
                log(f"[trt] 删了 {d}")
        try:
            v.rmdir()
        except OSError:
            pass
    try:
        paths.trt_root().rmdir()
    except OSError:
        pass
    return gone


def build_defaults(precisions: list[str], device: int = 0, log=print) -> list[str]:
    """默认模型（det + 中文 rec）的引擎和启动自检的 CPU 参考，建在本进程里（install 用；推理服务缺引擎时在它自己的进程里建，缓存名里的 ORT 版本才对得上）。
    别的语言的 rec 用到时推理服务现建。"""
    import onnxruntime as ort
    from flowocr import models
    E.setup_dlls(lib_dir())
    if hasattr(ort, "preload_dlls"):
        ort.preload_dlls()
    cap = compute_cap(device)
    built = [E.build(kind, Path(models.path(kind)), prec, engine_dir(), cap, TRT_VERSION, device, log)
             for prec in precisions for kind in ("det", "rec")]
    for kind in ("det", "rec"):                     # 启动自检的 CPU 参考也在这里算好，管线起服务时只读
        E.ensure_ref(kind, Path(models.path(kind)), engine_dir(), log)
    return built


def ptx_may_help(e: E.BuildError) -> bool:
    """建引擎失败时补取 PTX 那份资源值不值得：显存不够、TRT EP 根本没生效这类原因换资源也没用，不白下 230 MB。"""
    msg = str(e).lower()
    return not any(k in msg for k in ("out of memory", "outofmemory", "cudaerrormemoryallocation", "没生效"))


def install(cap: tuple[int, int], precs: list[str], wheel: str | None, device: int = 0, build=None, fetch=None, log=print) -> None:
    """取库、建引擎；本机那份资源建不出来（`BuildError` 且 `ptx_may_help`）时补取 PTX 那份再建一次。`build` / `fetch` 只给守卫换掉。"""
    build = build or (lambda: build_defaults(precs, device, log))
    fetch = fetch or (lambda members: fetch_libs(members, wheel, log))
    for p in fetch(members_for(cap)):
        log(f"[trt] 装好 {p}")
    if not precs:
        return
    try:
        build()
    except E.BuildError as e:
        if resource_for(cap) == LIB + _RES_NAME.format("ptx") or not ptx_may_help(e):
            raise
        log(f"[trt] {e}；补取 PTX 那份建引擎资源再试")
        fetch(members_for(cap, ptx=True))
        build()


def status() -> list[str]:
    lines = [f"TensorRT 根：{root()}（TensorRT {TRT_VERSION}）"]
    why = ort_compatible()
    lines.append("onnxruntime：" + (why or "对得上"))
    try:
        cap = compute_cap()
        need = members_for(cap)
        lines.append(f"算力 {cap[0]}.{cap[1]} -> {Path(resource_for(cap)).name}")
    except RuntimeError as e:
        need = list(COMMON)
        lines.append(f"算力：查不到（{e}）")
    for m in need:
        lines.append(f"  {Path(m).name}：{'在' if have(m) else '缺'}")
    eng = sorted(engine_dir().glob("*.engine")) if engine_dir().is_dir() else []
    lines.append(f"引擎缓存：{len(eng)} 个，{sum(p.stat().st_size for p in eng) / 2**20:.0f} MB（换了模型 / ORT 版本后旧引擎不会自动删，`remove` 清）")
    junk = sorted(engine_dir().glob(".build-*")) if engine_dir().is_dir() else []
    if junk:
        lines.append(f"  建到一半留下的临时目录 {len(junk)} 个（下次建同名引擎时自动清，或 `remove`）")
    for v in version_dirs():
        if v != root():
            size = sum(f.stat().st_size for f in v.rglob("*") if f.is_file())
            lines.append(f"旧版本目录 {v}：{size / 2**20:.0f} MB（`remove` 清）")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="flowocr-trt", description="装 / 查 / 删 TensorRT 后端要的库和引擎缓存")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("install", help="取本机要的 TensorRT 库（范围下载，约 0.3~0.6 GB），再建默认模型的引擎")
    i.add_argument("--from", dest="wheel", default="", help=f"用本地的 {WHEEL}（网络受限时先在别处整包下好）")
    i.add_argument("--device", type=int, default=0, help="按哪块 GPU 的算力挑建引擎资源、在哪块卡上建引擎")
    i.add_argument("--precision", default="fp32", choices=("fp32", "fp16", "both"), help="建哪种精度的引擎（默认 fp32，和 --trt-precision 的默认一样）")
    i.add_argument("--no-build", action="store_true", help="只取库，不建引擎（管线第一次用时现建）")
    sub.add_parser("status", help="库、算力、引擎缓存的状态")
    sub.add_parser("remove", help="删库和引擎缓存")
    a = ap.parse_args(argv)
    if not WHEEL or platform.machine().lower() not in ("amd64", "x86_64"):
        ap.error("这一版只做了 Windows 和 Linux 的 x86-64")
    if a.cmd == "status":
        print("\n".join(status()))
        return 0
    if a.cmd == "remove":
        remove()
        return 0
    why = ort_compatible()
    if why:
        print(f"[trt] {why}，不装", file=sys.stderr)
        return 1
    try:
        cap = compute_cap(a.device)
    except RuntimeError as e:
        print(f"[trt] {e}", file=sys.stderr)
        return 1
    print(f"[trt] TensorRT 是 NVIDIA 的专有软件，下载和使用即表示接受 NVIDIA 的许可协议：{LICENSE_URL}"
          f"（随这个版本的全文装好后在 {lib_dir() / 'LICENSE.txt'}）", flush=True)
    precs = [] if a.no_build else ["fp32", "fp16"] if a.precision == "both" else [a.precision]
    install(cap, precs, a.wheel or None, a.device)
    print(f"[trt] 库在 {lib_dir()}" + (f"，引擎在 {engine_dir()}" if precs else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
