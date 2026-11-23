"""阶段 5 的编码器选择和质量参数：按 owner 定的先后挑编码器，按目标 VMAF 在源视频上抽几段试压、搜出达标的最大质量值（文件最小）。

**编码器**（owner 2026-10-30）：有 H.265 硬件编码器就用它；否则 H.264 硬件编码器；都没有用 libx264。libx265 太慢不进自动选择，可以指定。
"质量"对软件编码器是 CRF，对硬件编码器是它的恒定质量参数（NVENC 的 `-rc vbr -cq <q> -b:v 0`）。
能不能用按真压几帧判：ffmpeg 列出了 nvenc 不等于有能用的 N 卡。QSV、AMF 的参数照 ffmpeg 的选项写，没在真卡上验过。

**质量值**（owner 2026-10-31："压出来的成品和输入的质量应该差不多……可能会稍大一点"）：目标 VMAF 95（`TARGET`）。
默认值按同一画质保守取（hevc / h264 硬件 26、libx264 14：5 个游戏实况片段里最差的也有 95.7），短视频不搜、直接用它；
不短于 `AUTO_MIN_S` 的默认搜（搜一次约 150 s，约占 inpainting default 档处理时间的 16%，换来的文件比默认值小 12–31%）。
QSV、AMF、libx265 的默认值没量过，照同类填的。

**搜法**（`Prober`）：拿源视频当输出的近似（修补只动底板那一小块，内容复杂度相近），不跑修补。在范围里等距取 n 段、每段 `SEG` 秒，
按输出的尺寸、帧率无损存一份作参照；每个候选质量值把各段压一遍、对参照算 VMAF（ffmpeg 自带的 libvmaf，默认模型 vmaf_v0.6.1），
全部抽样帧的均值达到目标就往大（省体积）走、不到就往小走：先试默认值，之后插值或二分。
"""
from __future__ import annotations

import functools
import json
import math
import os
import subprocess
import tempfile
import time
from fractions import Fraction
from pathlib import Path

import numpy as np

SEG = 3.0
"""每段秒数：60 fps 下 180 帧，够盖住一两个镜头，又不至于让一次试压太贵。"""
THREADS = os.cpu_count() or 4
HVC1 = ["-tag:v", "hvc1"]
"""H.265 在 mp4 里标 hvc1，苹果系播放器才认。"""


def _nv(name: str, tag: list[str]) -> dict:
    return {"args": lambda q: ["-c:v", name, "-preset", "p4", "-rc", "vbr", "-cq", str(q), "-b:v", "0", *tag]}


ENCODERS = {
    "hevc_nvenc": {**_nv("hevc_nvenc", HVC1), "q": 26, "range": (12, 40)},
    "hevc_qsv": {"args": lambda q: ["-c:v", "hevc_qsv", "-preset", "medium", "-global_quality", str(q), *HVC1], "q": 26, "range": (12, 40)},
    "hevc_amf": {"args": lambda q: ["-c:v", "hevc_amf", "-rc", "cqp", "-qp_i", str(q), "-qp_p", str(q), *HVC1], "q": 26, "range": (12, 40)},
    "h264_nvenc": {**_nv("h264_nvenc", []), "q": 26, "range": (10, 38)},
    "h264_qsv": {"args": lambda q: ["-c:v", "h264_qsv", "-preset", "medium", "-global_quality", str(q)], "q": 26, "range": (10, 38)},
    "h264_amf": {"args": lambda q: ["-c:v", "h264_amf", "-rc", "cqp", "-qp_i", str(q), "-qp_p", str(q), "-qp_b", str(q)], "q": 26, "range": (10, 38)},
    "libx264": {"args": lambda q: ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(q)], "q": 14, "range": (10, 38)},
    "libx265": {"args": lambda q: ["-c:v", "libx265", "-preset", "fast", "-crf", str(q), "-x265-params", "log-level=error", *HVC1],
                "q": 20, "range": (12, 40)},
}
"""名字 -> ffmpeg 参数（质量 q 进去）、默认质量、搜索的范围。"""
AUTO = ("hevc_nvenc", "hevc_qsv", "hevc_amf", "h264_nvenc", "h264_qsv", "h264_amf", "libx264")
"""自动选择的先后（owner 2026-10-30）。"""
AUTO_MIN_S = 1800
TARGET = 95.0


def venc(name: str, q: int) -> list[str]:
    """编码器 name 按质量 q 的 ffmpeg 参数（不含 `-pix_fmt`）。"""
    return ENCODERS[name]["args"](q)


@functools.cache
def usable(name: str) -> bool:
    """真压几帧小图试试这个编码器能不能用。"""
    r = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=256x256:r=30", "-frames:v", "5",
                        *venc(name, ENCODERS[name]["q"]), "-pix_fmt", "yuv420p", "-f", "null", "-"], capture_output=True)
    return r.returncode == 0


@functools.cache
def has_vmaf() -> bool:
    """ffmpeg 带没带 libvmaf（gyan.dev 的 full 构建带；没带就搜不了质量值）。"""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True)
    return " libvmaf " in r.stdout


def resolve(codec: str) -> str:
    """`auto` -> 按先后挑第一个能用的；指定的名字要在表里且能用。"""
    if codec == "auto":
        name = next((n for n in AUTO if usable(n)), None)
        if name is None:
            raise ValueError(f"这个 ffmpeg 一个能用的编码器都没有（试了 {'、'.join(AUTO)}）：换一个带 libx264 的 ffmpeg 构建")
        return name
    if codec not in ENCODERS:
        raise ValueError(f"不认识的编码器 {codec}，可选：auto、{'、'.join(ENCODERS)}")
    if not usable(codec):
        raise ValueError(f"编码器 {codec} 在这台机器上压不了")
    return codec


def n_samples(dur: float) -> int:
    """抽几段：每分钟一段，至少 4、至多 10（长视频一次试压约 30 s 素材）。"""
    return min(10, max(4, math.ceil(dur / 60)))


def src_fps(video: str) -> Fraction:
    """视频的帧率：平均帧率（avg_frame_rate）和标称帧率（r_frame_rate）相差不到 0.01% 时取标称的（yuka 的录像平均出来是
    1630078001/27167966，标称 60/1，差约 0.0003%），否则取平均的（真正的可变帧率；59.94 对 60 差 0.1%，也算不同）；容器只记了一个就用那个。"""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=avg_frame_rate,r_frame_rate",
                        "-of", "json", video], capture_output=True, text=True, check=True)
    st = json.loads(r.stdout)["streams"][0]
    got = {k: Fraction(st[k]) for k in ("avg_frame_rate", "r_frame_rate") if st.get(k, "0/0") not in ("0/0", "")}
    if not got:
        raise ValueError(f"读不出帧率：{video}")
    avg, nom = got.get("avg_frame_rate"), got.get("r_frame_rate")
    if avg is None or nom is not None and abs(avg - nom) <= nom / 10000:
        return nom
    return avg


def src_size(video: str) -> tuple[int, int]:
    """视频画面的（宽, 高）。"""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", video],
                       capture_output=True, text=True, check=True)
    st = json.loads(r.stdout)["streams"][0]
    return int(st["width"]), int(st["height"])


def _vmaf(dist: str, ref: str, cwd: Path, log: str) -> np.ndarray:
    """dist 对 ref 逐帧 VMAF。两边按帧序重排时间戳再配对（mkv 的毫秒时间戳在 60 fps 下会让 libvmaf 错帧配对、分数明显偏低）。"""
    subprocess.run(["ffmpeg", "-v", "error", "-i", dist, "-i", ref, "-lavfi",
                    f"[0:v]setpts=N/FRAME_RATE/TB[d];[1:v]setpts=N/FRAME_RATE/TB[r];"
                    f"[d][r]libvmaf=n_threads={THREADS}:log_fmt=json:log_path={log}", "-f", "null", "-"], cwd=cwd, check=True)
    js = json.loads((cwd / log).read_text(encoding="utf-8"))
    (cwd / log).unlink()
    return np.array([f["metrics"]["vmaf"] for f in js["frames"]])


class Prober:
    """一个范围、一个编码器：抽好参照段，按质量值试压打分（结果缓存）。用 `with`，退出时删临时目录。
    `size` / `fps` 是输出的尺寸和帧率（参照段照它缩放、抽帧）；`size` 和源相同时不缩放。"""

    def __init__(self, video: str, start: float, end: float, encoder: str, size: tuple[int, int], fps: Fraction,
                 n: int | None = None, seg: float = SEG):
        t0 = time.perf_counter()
        self.encoder, self.fps = encoder, fps
        video = os.path.abspath(video)                  # 下面的 ffmpeg 在临时目录里跑
        dur = end - start
        self.n = n or n_samples(dur)
        seg = min(seg, dur / self.n)
        self.starts = [round(start + (k + 0.5) * dur / self.n - seg / 2, 3) for k in range(self.n)]   # 每段居中在 n 等分的中点
        self.seg, self.tmp = seg, tempfile.TemporaryDirectory(prefix="flowocr-burn-q-")
        self.dir = Path(self.tmp.name)
        scale = "" if size == src_size(video) else f"scale={size[0]}:{size[1]},"
        for k, s in enumerate(self.starts):   # 参照：libx264 无损（yuv420p，qp 0）
            subprocess.run(["ffmpeg", "-v", "error", "-ss", "%.3f" % s, "-t", "%.3f" % seg, "-i", video, "-an",
                            "-vf", f"{scale}fps={fps}", "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0",
                            "-pix_fmt", "yuv420p", f"ref{k}.mp4"], cwd=self.dir, check=True)
        self.frames = [int(subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                                           "stream=nb_read_frames", "-of", "csv=p=0", f"ref{k}.mp4"], cwd=self.dir, capture_output=True,
                                          text=True, check=True).stdout) for k in range(self.n)]
        self.extract_s = round(time.perf_counter() - t0, 2)
        self.cache: dict[int, dict] = {}

    def __enter__(self) -> "Prober":
        return self

    def __exit__(self, *exc) -> None:
        self.tmp.cleanup()

    def probe(self, q: int) -> dict:
        """各段按 q 压一遍、打分：全部帧的 VMAF 均值、码率（kbps，压出的字节 ÷ 抽样时长）、耗时。"""
        if q in self.cache:
            return self.cache[q]
        t0, scores, size = time.perf_counter(), [], 0
        for k in range(self.n):
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", f"ref{k}.mp4", *venc(self.encoder, q), "-pix_fmt", "yuv420p",
                            f"d{k}.mp4"], cwd=self.dir, check=True)
            size += (self.dir / f"d{k}.mp4").stat().st_size
            scores.append(_vmaf(f"d{k}.mp4", f"ref{k}.mp4", self.dir, f"v{k}.json"))
        secs = sum(self.frames) / float(self.fps)
        r = {"q": q, "vmaf": round(float(np.concatenate(scores).mean()), 3), "kbps": round(size * 8 / secs / 1000),
             "s": round(time.perf_counter() - t0, 2)}
        self.cache[q] = r
        return r

    def search(self, target: float) -> dict:
        """达到 target 的最大整数质量值（假定分数随 q 单调降）。先试默认值，之后夹住"达标 lo、不达标 hi"往里收：
        两头都真试过就按分数线性插值猜下一个（分数随 q 近似线性，通常两三次收住），有一头是越界的虚端就二分。
        到下限还不达标就取下限（met=False）；到上限还达标就取上限。"""
        qmin, qmax = ENCODERS[self.encoder]["range"]
        d = min(max(ENCODERS[self.encoder]["q"], qmin), qmax)
        seen, sc = [], {}

        def ok(q):
            r = self.probe(q)
            seen.append(q)
            sc[q] = r["vmaf"]
            return sc[q] >= target
        lo, hi = (d, qmax + 1) if ok(d) else (qmin - 1, d)       # 越界的那头是虚的、没试过
        while hi - lo > 1:
            if lo in sc and hi in sc:
                m = min(max(lo + math.floor((sc[lo] - target) / (sc[lo] - sc[hi]) * (hi - lo)), lo + 1), hi - 1)
            else:
                m = (lo + hi) // 2
            lo, hi = (m, hi) if ok(m) else (lo, m)
        q = max(lo, qmin)
        r = self.probe(q)
        return {"target": target, "q": q, "met": r["vmaf"] >= target, "vmaf": r["vmaf"], "kbps": r["kbps"],
                "default_q": d, "default_kbps": self.probe(d)["kbps"], "tried": seen,
                "search_s": round(self.extract_s + sum(self.cache[x]["s"] for x in dict.fromkeys(seen)), 2)}


def search(video: str, start: float, end: float, encoder: str, target: float, size: tuple[int, int], fps: Fraction) -> dict:
    """一次性：抽参照、搜一个目标，返回选中的值和试压记录。"""
    with Prober(video, start, end, encoder, size, fps) as p:
        return {**p.search(target), "samples": p.n, "seg": p.seg}
