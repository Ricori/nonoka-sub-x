"""ffmpeg / ffprobe 在不在、够不够用，推理后端装对了没有，以及这一趟的运行时版本（release-plan F13 / F8）。

ffmpeg **不随包分发**（owner 2026-09-25：避开 ffmpeg 构建的再分发义务），用户自己装、放进 `PATH`。
这里只拦"缺了必挂"的：两个可执行文件、`showinfo`（逐帧 pts 从它的 stderr 读，`framesource` 文件头）、`select`、`scale`。
**硬解能力不在这里判**：NVDEC / `scale_cuda` 缺了由 `framesource.hwaccel_blocker` 试解不过、退回软解（原因写进 `_meta`），
照这台机器实际能跑的走，不按构建选项猜。
版本只警告、不拒绝（owner 2026-10-17 暂按建议执行）：8.0 / 8.1 上验证过；6.1（Ubuntu 24.04 的 apt 版）交出的时间戳在
有前摇的 av1 和 `--start` 窗口上会回绕 / 重复，取帧时报"帧号不唯一"退出（响亮失败、不出错产物）；7.x 没测。
"""
from __future__ import annotations

import functools
import importlib.metadata
import platform
import re
import shutil
import subprocess
import sys

REQUIRED_FILTERS = ("showinfo", "select", "scale")
VERIFIED_MAJOR = 8
"""验证过的最低 ffmpeg 主版本；更低的照跑、打一行警告。"""
_VERSION_RE = re.compile(r"^ffmpeg version n?(\d+)\.\d+")
DISTS = ("flowocr", "numpy", "opencv-contrib-python", "onnxruntime-gpu", "onnxruntime")
"""`_meta.runtime` 记哪些包的版本（按分发名查元数据、不 import）。"""


def _run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=30).stdout


@functools.lru_cache(maxsize=1)
def probe() -> dict:
    """`{"ffmpeg": 路径, "ffprobe": 路径, "version": 首行, "missing": [缺的东西]}`。一个进程只探一次。"""
    ff, fp = shutil.which("ffmpeg"), shutil.which("ffprobe")
    missing = [n for n, p in (("ffmpeg", ff), ("ffprobe", fp)) if p is None]
    version = ""
    if ff:
        try:
            version = (_run([ff, "-hide_banner", "-version"]).splitlines() or [""])[0].strip()
            names = {ln.split()[1] for ln in _run([ff, "-hide_banner", "-filters"]).splitlines()
                     if len(ln.split()) >= 3 and ln[:1] == " "}
            missing += [f"滤镜 {f}" for f in REQUIRED_FILTERS if f not in names]
        except (OSError, subprocess.SubprocessError) as exc:      # 在 PATH 上但起不来（损坏、权限、卡住）
            missing.append(f"能运行的 ffmpeg（{ff}：{type(exc).__name__}: {exc}）")
    return {"ffmpeg": ff, "ffprobe": fp, "version": version, "missing": missing}


def version_warning(version: str) -> str:
    """版本行低于 `VERIFIED_MAJOR` 时的警告；读不出版本号（git 快照构建等）或够新时返回空串。"""
    m = _VERSION_RE.match(version)
    if not m or int(m.group(1)) >= VERIFIED_MAJOR:
        return ""
    return (f"[ffmpeg] ⚠ 这是 {version.split()[2]}，flowocr 只在 ffmpeg {VERIFIED_MAJOR}.x 上验证过："
            f"6.1 在有前摇的 av1 文件和 --start 窗口上会报“帧号不唯一”退出。照跑，出错就换新版")


def require() -> dict:
    """缺了就带着说明退出；版本低于验证过的打一行警告；够用就返回 `probe()`。"""
    p = probe()
    if p["missing"]:
        raise SystemExit(f"ffmpeg 不够用：缺 {', '.join(p['missing'])}。flowocr 不带 ffmpeg，要自己装一个完整构建"
                         f"（如 gyan.dev / BtbN 的 Windows full build），把它的 bin 目录放进 PATH（ffmpeg 与 ffprobe 都要）")
    if warn := version_warning(p["version"]):
        print(warn, flush=True)
    return p


ORT_DISTS = ("onnxruntime-gpu", "onnxruntime")
"""推理后端的两个分发：装的是同一个 `onnxruntime` 顶层包，只能有一个（`[nvidia]` / `[cpu]` 两个 extra 各装一个）。"""


def require_runtime() -> None:
    """推理后端装对了没有、平台是不是验证过的。装成包时 extras 的互斥进不了元数据，所以起跑时查：
    一个都没有 -> 退出并说装哪个 extra；两个都在 -> 退出并说只留一个（后装的那个盖掉前一个，可能装成了不带 CUDA 的那份）；
    不是 Windows -> 打一行说明照跑（owner 2026-10-02：不挡想试的人）：Linux 只在 WSL2 的 Ubuntu 24.04 上验证过（2026-10-17），别的系统没验证过。"""
    have = []
    for d in ORT_DISTS:
        try:
            importlib.metadata.version(d)
            have.append(d)
        except importlib.metadata.PackageNotFoundError:
            pass
    if not have:
        raise SystemExit("没装推理后端（onnxruntime）。装的时候选一个 extra：有 NVIDIA 显卡 `flowocr[nvidia]`，没有 `flowocr[cpu]`"
                         "（源码装就是 `uv sync --frozen --extra nvidia` 或 `--extra cpu`）")
    if len(have) > 1:
        raise SystemExit("onnxruntime-gpu 和 onnxruntime 同时装着，两个会互相覆盖。只留一个：重装时 extra 只选 nvidia 或 cpu 之一"
                         "（源码装删掉 .venv 再 `uv sync`）")
    if sys.platform.startswith("linux"):
        print("[平台] Linux 只在 WSL2 的 Ubuntu 24.04 上验证过（CPU 与 NVIDIA GPU）：照跑，原生 Linux 上出了问题欢迎反馈", flush=True)
    elif sys.platform != "win32":
        print(f"[平台] ⚠ flowocr 只在 Windows 和 Linux（WSL2）上验证过，这台是 {platform.system()}：照跑，出了问题欢迎反馈", flush=True)


def runtime() -> dict:
    """这一趟的运行时：Python、各推理库的版本、ffmpeg 的版本行。**只追溯、不进复用判据**（同一组 config 可以出自不同运行时）。"""
    pkgs = {}
    for d in DISTS:
        try:
            pkgs[d] = importlib.metadata.version(d)
        except importlib.metadata.PackageNotFoundError:
            pass
    return {"python": sys.version.split()[0], "platform": platform.platform(), "packages": pkgs,
            "ffmpeg": probe()["version"]}
