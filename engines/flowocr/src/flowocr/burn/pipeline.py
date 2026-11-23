"""阶段 5 的流水线：定输出规格（`Job`），按路线拼 ffmpeg 命令、跑、成功了再把 `.part` 改成正式文件名。

* 底板路线：一个 ffmpeg 进程，解码 → （缩放、换帧率）→ `subtitles` 滤镜烧叠加 ASS → 编码，音轨照拷。
* inpainting 路线：边解码边修补边编码，整片不进内存。解码、编码是两个 ffmpeg 进程（bgr24 原始帧走管道，按恒定帧率抽帧）；
  本进程里读解码管道、写编码管道各一个线程（管道读写放 GIL），主线程只修补，三者重叠，两头的队列各最多攒 `DEPTH` 帧。
  编码那头烧的是去掉底板行的叠加 ASS，音轨从源视频另开一路按同一范围取。
  YUV ↔ RGB 两头按源的色彩矩阵和范围转（`colors`），开精确舍入和全分辨率色度，输出帧标上同样的色彩信息
  （`setparams`：编码器按帧上的写，`-color_*` 输出选项会被帧上的"未指定"盖掉）。ffmpeg 默认的转换回到 YUV 时按 BT.601、舍入也粗，
  BT.709 的源整片每帧偏约 1–2 级（原神 4 帧上半幅均差 Y −1.2、V −1.7）；改了之后和不经 RGB 的底板路线一样（±0.03 级）。
* `subtitles` 滤镜参数里的盘符冒号、反斜杠、引号都要转义：把 ASS 拷进临时目录、改名 `burn.ass`，在那里跑、参数只写文件名。
* 烧一段（`start` / `end`）时解码从 `start` 起、时间戳从 0 算，烧字幕前加回 `start`（ASS 的时间是原片的），烧完再归零。
  前提是源的时间从 0 起（start_time = 0，现有素材都是）：不是的话整段烧（直接用源的时间戳）和烧一段会差 start_time 那么多，
  和阶段 1 的时间基准（带前摇的 av1，架构说明的帧身份一节）也没对过。
* 输出永远不落在源视频或 ASS 上（同名同目录时默认输出名就是源视频）：素材只读。
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

import numpy as np

from flowocr.burn import encode, plates
from flowocr.typeset import assfile, core, platecolor

MP4_AUDIO = {"aac", "mp3", "opus", "ac3", "eac3", "flac", "alac"}
"""mp4 容器收得下、照拷就行的音频编码；别的（vorbis、pcm…）重编成 AAC。"""
AAC = ["-c:a", "aac", "-b:a", "192k"]
DEPTH = 4
"""inpainting：读、写两个队列各最多攒几帧（1080p 一帧 6.2 MB）。"""
PROGRESS_S = 10.0
"""inpainting：每隔这么多秒报一次进度。"""
MATRIX = {"bt709": "bt709", "smpte170m": "smpte170m", "bt470bg": "bt470", "bt2020nc": "bt2020", "bt2020c": "bt2020",
          "smpte240m": "smpte240m", "fcc": "fcc"}
"""ffprobe 的 `color_space` -> `scale` 滤镜的 `in_color_matrix` / `out_color_matrix` 写法。"""
SWS = "bicubic+accurate_rnd+full_chroma_int+full_chroma_inp"
"""inpainting 两头 YUV ↔ RGB 的 swscale 选项：精确舍入、色度按全分辨率插值。"""


@dataclass
class Job:
    """一次烧录的输入和输出规格。`fps` 是 None 时不换帧率（按源）；`size` 和源相同时不缩放。"""
    ass: Path
    video: str
    out: Path
    start: float
    end: float
    size: tuple[int, int]
    fps: Fraction | None
    codec: str
    quality: int
    probe: dict | None = None
    notes: list[str] = field(default_factory=list)

    def note(self, s: str) -> None:
        """提示当场打出来（走 CPU、没有 libvmaf 这类事要在开跑前知道），也留在 `notes` 里。"""
        print(f"[阶段 5] {s}", flush=True)
        self.notes.append(s)


def video_of(ass: Path, given: str | None = None) -> str:
    """源视频：给了就用它，否则 ASS 的 `[Script Info]` 里阶段 3 记的 `FlowOCR Video`（阶段 4 取色读的同一个）。"""
    v = core.source_video(assfile.loads(ass.read_text(encoding="utf-8")).section("Script Info"), given)
    if not v or not Path(v).is_file():
        raise ValueError(f"找不到源视频{'：' + repr(v) if v else '：ASS 里没有 ' + core.VIDEO_KEY}，用 --video 指定")
    return v


def default_out(ass: Path, inpaint: bool) -> Path:
    """`x.ass` -> `x.mp4`（inpainting 是 `x.inpaint.mp4`），和 ASS 放在一起。"""
    stem = ass.name[:-4] if ass.name.lower().endswith(".ass") else ass.name
    return ass.with_name(stem + (".inpaint.mp4" if inpaint else ".mp4"))


def same_file(a: Path | str, b: Path | str) -> bool:
    a, b = Path(a), Path(b)
    if a.exists() and b.exists():
        return os.path.samefile(a, b)
    return os.path.normcase(a.resolve()) == os.path.normcase(b.resolve())


def plan(ass: Path, video: str | None, out: Path | None, inpaint: bool, start: float | None, end: float | None,
         size: tuple[int, int] | None, fps: Fraction | None, codec: str, quality: str | None, target: float) -> Job:
    """定规格：范围默认整段，尺寸、帧率默认按源；编码器按先后挑；质量值：给了数就用，`auto` 搜，
    不给时范围不短于 `encode.AUTO_MIN_S` 就搜、否则用编码器的保守默认值。"""
    v = video_of(ass, video)
    out = out or default_out(ass, inpaint)
    for what, p in (("源视频", v), ("叠加 ASS", ass)):
        if same_file(out, p):
            raise ValueError(f"输出 {out} 就是{what}，会把它覆盖掉：用 -o 另给一个输出")
    s = start or 0.0
    e = end if end is not None else platecolor.duration(v)
    if not s < e < float("inf"):
        raise ValueError(f"范围不对：{s}–{e} s（视频时长读不出来时用 --end 指定）")
    size = size or encode.src_size(v)
    job = Job(ass, v, out, s, e, size, fps, "", 0)
    if inpaint:                                     # 稿读不出底板就早报，别等搜完质量值
        pl, _ = plates.load(ass.read_text(encoding="utf-8-sig"), size)
        if not pl.evs:
            job.note("稿里没有打了标的底板，没有要抹的地方：输出和原样烧一样")
        elif pl.mode not in (None, "src"):
            job.note(f"底板是 plate={pl.mode} 出的，会连译文伸出原文的部分一起抹；只抹原文就给阶段 4 加 --opt plate=src 重出")
    name = encode.resolve(codec)
    want = quality == "auto" or quality is None and e - s >= encode.AUTO_MIN_S
    if want and not encode.has_vmaf():
        if quality == "auto":
            raise ValueError("--quality auto 要带 libvmaf 的 ffmpeg（`ffmpeg -filters` 里有 libvmaf）；或者用 --quality <n> 指定")
        job.note(f"ffmpeg 没带 libvmaf，搜不了质量值，用 {name} 的默认值 {encode.ENCODERS[name]['q']}")
        want = False
    if want:
        print(f"[阶段 5] 按目标 VMAF {target:g} 定 {name} 的质量值（在源视频上抽样试压）…", flush=True)
        job.probe = encode.search(v, s, e, name, target, size, fps or encode.src_fps(v))
        q = job.probe["q"]
    else:
        q = int(quality) if quality else encode.ENCODERS[name]["q"]
    job.codec, job.quality = name, q
    return job


def audio_args(video: str) -> tuple[list[str], str | None]:
    """源音轨怎么带：全部照拷；有 mp4 收不下的编码就全部重编成 AAC（返回一句提示）。没有音轨就不带。"""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_name", "-of", "json", video],
                       capture_output=True, text=True, check=True)
    codecs = [st.get("codec_name", "") for st in json.loads(r.stdout).get("streams", [])]
    if not codecs:
        return [], None
    if all(c in MP4_AUDIO for c in codecs):
        return ["-c:a", "copy"], None
    return AAC, f"源音轨是 {'、'.join(codecs)}，mp4 收不下，重编成 AAC 192k"


def colors(video: str) -> dict[str, str]:
    """源的色彩矩阵、范围、原色、传输特性（ffprobe 的写法）。没标的按播放器常见的猜法补：高 ≥ 720 当 BT.709、否则 BT.601，范围当有限（tv）。"""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=height,color_space,color_range,color_primaries,color_transfer", "-of", "json", video],
                       capture_output=True, text=True, check=True)
    st = json.loads(r.stdout)["streams"][0]
    guess = "bt709" if int(st.get("height") or 0) >= 720 else "smpte170m"
    known = lambda k: st.get(k) not in (None, "", "unknown", "unspecified", "reserved")       # noqa: E731
    return {"space": st["color_space"] if st.get("color_space") in MATRIX else guess,
            "range": st["color_range"] if st.get("color_range") in ("tv", "pc") else "tv",
            "primaries": st["color_primaries"] if known("color_primaries") else guess,
            "trc": st["color_transfer"] if known("color_transfer") else guess}


def seek_args(job: Job) -> list[str]:
    """解码的范围（整段时不加）。"""
    whole = job.start == 0 and job.end >= platecolor.duration(job.video)
    return [] if whole else ["-ss", "%.3f" % job.start, "-t", "%.3f" % (job.end - job.start)]


def burn_plate(job: Job) -> Path:
    """底板路线：一个 ffmpeg 进程把叠加 ASS 原样烧进去。返回输出文件。"""
    seek = seek_args(job)
    a, note = audio_args(job.video)
    if note:
        job.note(note)
    vf = []
    if job.size != encode.src_size(job.video):
        vf.append(f"scale={job.size[0]}:{job.size[1]}")
    if job.fps is not None:
        vf.append(f"fps={job.fps}")
    vf += sub_filter(job, seek)
    t0 = time.perf_counter()
    with staged(job, job.ass.read_text(encoding="utf-8-sig")) as (td, part):
        r = subprocess.run(["ffmpeg", "-v", "error", "-stats", "-y", *seek, "-i", os.path.abspath(job.video),
                            "-map", "0:v:0", *(["-map", "0:a?", *a] if a else []), "-vf", ",".join(vf),
                            *encode.venc(job.codec, job.quality), "-pix_fmt", "yuv420p", "-f", "mp4", str(part)], cwd=td)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg 失败（退出码 {r.returncode}），没有写 {job.out}")
    print(f"[阶段 5] -> {job.out}（{job.codec} 质量 {job.quality}，{time.perf_counter() - t0:.1f} s）", flush=True)
    return job.out


def burn_inpaint(job: Job, preset: str = "default", backend: str = "auto") -> Path:
    """inpainting 路线：逐帧按底板（跟着打字机展开）修补，再烧其余的行。返回输出文件。"""
    from flowocr.burn import inpaint
    pl, rest = plates.load(job.ass.read_text(encoding="utf-8-sig"), job.size)
    fps = job.fps or encode.src_fps(job.video)
    be, why = inpaint.backend_of(backend)
    if why:
        job.note(f"修补走 {be}：{why}")
    cfg = inpaint.PRESETS[preset]
    inp = inpaint.Inpainter(cfg, inpaint.session(cfg, be, "fp16" if be == "trt" else "fp32"), float(fps), be)
    seek = seek_args(job)
    a, note = audio_args(job.video)
    if note:
        job.note(note)
    W, H = job.size
    c = colors(job.video)
    m = MATRIX[c["space"]]
    total = job.end - job.start
    t0 = time.perf_counter()
    procs: list[subprocess.Popen] = []
    frames, wr = None, None
    with staged(job, "\n".join(rest) + "\n") as (td, part):
        try:
            dec = subprocess.Popen(["ffmpeg", "-v", "error", *seek, "-i", os.path.abspath(job.video), "-map", "0:v:0",
                                    "-vf", f"scale={W}:{H}:in_color_matrix={m}:in_range={c['range']}:flags={SWS},fps={fps}",
                                    "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                                   stdout=subprocess.PIPE)
            procs.append(dec)
            enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
                                    "-r", str(fps), "-i", "-", *([*seek, "-i", os.path.abspath(job.video)] if a else []),
                                    "-map", "0:v", *(["-map", "1:a?", *a] if a else []),
                                    "-vf", ",".join([*sub_filter(job, seek), f"scale=out_color_matrix={m}:out_range={c['range']}:flags={SWS}",
                                                     "format=yuv420p", f"setparams=colorspace={c['space']}:range={c['range']}"
                                                     f":color_primaries={c['primaries']}:color_trc={c['trc']}"]),
                                    *encode.venc(job.codec, job.quality), "-pix_fmt", "yuv420p", "-f", "mp4", str(part)],
                                   stdin=subprocess.PIPE, cwd=td)
            procs.append(enc)
            wr = Writer(enc.stdin, DEPTH)
            n, last = 0, t0
            frames = ahead(decoded(dec, W, H, job.start, fps), DEPTH)
            for t, img in frames:
                wr.put(inp(img, *pl.at(t)))
                n += 1
                if time.perf_counter() - last >= PROGRESS_S:
                    last = time.perf_counter()
                    done, rate = t - job.start, (t - job.start) / (last - t0)
                    print(f"[阶段 5] {done:.0f} / {total:.0f} s（{rate:.2f} 倍速，还要约 {(total - done) / max(rate, 1e-6) / 60:.1f} 分钟）",
                          flush=True)
            wr.close()
            if enc.wait() != 0:
                raise RuntimeError(f"编码失败（ffmpeg 退出码 {enc.returncode}），没有写 {job.out}")
        except BaseException as err:
            # 先停两个 ffmpeg，.part 才删得掉，卡在管道上的读写线程也才停得下。
            for pr in procs:
                _cleanup_after(err, f"停止 ffmpeg PID {pr.pid}", lambda pr=pr: stop_ffmpeg(pr))
            if frames is not None:
                _cleanup_after(err, "停止解码读取", frames.close)
            if wr is not None:
                _cleanup_after(err, "停止编码写入", wr.abort)
            raise
    st = inp.stats
    print(f"[阶段 5] -> {job.out}（{preset} 档，{be}；{job.codec} 质量 {job.quality}；{n} 帧，{time.perf_counter() - t0:.1f} s；"
          f"推理 {st['tiles']} 窗、{st['run_s']:.1f} s" + (f"，{st['nan_tiles']} 窗出 NaN" + ("、用 fp32 重算" if be == "trt" else "") if st["nan_tiles"] else "") + "）",
          flush=True)
    return job.out


def stop_ffmpeg(proc: subprocess.Popen) -> None:
    """中止 ffmpeg；Windows 上的启动器可能已退出，真正的 ffmpeg 仍在跑。"""
    if proc.poll() is None:
        proc.kill()
    proc.wait()
    if os.name != "nt":
        return

    # 先停启动器，它才不能在枚举之后再起 ffmpeg。Windows 保留子进程的原父 PID，
    # Popen 还握着进程句柄，那个 PID 不会被复用。等真 ffmpeg 退出才可删 ASS 和 .part。
    import psutil
    children = [p for p in psutil.process_iter(["ppid"]) if p.info["ppid"] == proc.pid]
    for child in children:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(children, timeout=10)
    if alive:
        raise RuntimeError(f"ffmpeg 启动器的子进程未退出：{[p.pid for p in alive]}")


def _cleanup_after(error: BaseException, what: str, action: Callable[[], None]) -> None:
    """已在处理原错误：记录收尾失败，继续清理其余资源。"""
    try:
        action()
    except Exception as cleanup_error:
        error.add_note(f"{what}失败：{cleanup_error}")


def sub_filter(job: Job, seek: list[str]) -> list[str]:
    """烧 `burn.ass` 的滤镜：只烧一段时时间戳先加回起点（ASS 的时间是原片的），烧完再归零。"""
    sub = "subtitles=burn.ass"
    return [f"setpts=PTS+{job.start}/TB", sub, "setpts=PTS-STARTPTS"] if seek else [sub]


def decoded(dec: subprocess.Popen, w: int, h: int, start: float, fps: Fraction):
    """从解码管道逐帧取：(原片里的时刻, BGR 图)。解码器出错退出时报错。"""
    size, n = w * h * 3, 0
    while len(buf := dec.stdout.read(size)) == size:
        yield start + float(n / Fraction(fps)), np.frombuffer(buf, np.uint8).reshape(h, w, 3)
        n += 1
    if dec.wait() != 0:
        raise RuntimeError(f"解码失败（ffmpeg 退出码 {dec.returncode}）")


_END = object()


def ahead(it, depth: int):
    """在线程里跑迭代器 it，最多攒 depth 项；迭代器里的异常转交给取的一方。
    取的一方不取了（出错、`close()`）就让线程停下：线程只在队列满时等，每 0.1 s 看一次停没停。
    卡在读解码管道上的线程要等管道关掉（调用方先结束解码进程）才停得下。"""
    q, stop = queue.Queue(depth), threading.Event()

    def put(x) -> bool:
        while not stop.is_set():
            try:
                q.put(x, timeout=0.1)
                return True
            except queue.Full:
                pass
        return False

    def run():
        try:
            for x in it:
                if not put(x):
                    return
        except BaseException as ex:         # noqa: BLE001 —— 原样转交
            put(ex)
            return
        put(_END)

    th = threading.Thread(target=run, daemon=True)
    th.start()
    try:
        while (x := q.get()) is not _END:
            if isinstance(x, BaseException):
                raise x
            yield x
    finally:
        stop.set()
        th.join(timeout=10)


class Writer(threading.Thread):
    """往编码管道写帧（在本线程里写，最多攒 depth 帧）。"""

    def __init__(self, pipe, depth: int):
        super().__init__(daemon=True)
        self.pipe, self.error = pipe, None
        self.q = queue.Queue(depth)
        self.start()

    def put(self, frame: np.ndarray) -> None:
        if self.error is not None:                          # 编码器中途退出：当场报，不把整片白修补完
            raise RuntimeError(f"编码器中途退出：{self.error}")
        self.q.put(frame)

    def run(self):
        while (f := self.q.get()) is not None:
            if self.error is None:                          # 写失败后照样取空队列，主线程不会卡在 put 上
                try:
                    self.pipe.write(f.data)
                except BaseException as ex:                 # noqa: BLE001 —— 交给主线程报
                    self.error = ex

    def abort(self) -> None:
        """出错时收摊：扔掉没写的帧、让线程退出（编码进程已经结束时写会失败，线程照样取到结束标记）。"""
        if not self.is_alive():
            return
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        self.q.put(None)
        self.join(timeout=10)

    def close(self) -> None:
        self.q.put(None)
        self.join()
        try:
            self.pipe.close()
        except OSError as ex:
            self.error = self.error or ex
        if self.error is not None:
            raise RuntimeError(f"编码器中途退出：{self.error}")


@contextmanager
def staged(job: Job, ass_text: str):
    """临时目录里写好 `burn.ass`，给出 (目录, `.part` 路径)；块里正常走完就把 `.part` 改成正式文件名，出错、Ctrl-C 都删掉 `.part`。"""
    out = job.out.resolve()
    part = out.with_name(out.name + ".part")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.TemporaryDirectory(prefix="flowocr-burn-")
    td = Path(tmp.name)
    ready = False
    try:
        (td / "burn.ass").write_text(ass_text, encoding="utf-8")
        ready = True
        yield td, part
        os.replace(part, out)                       # 改名失败（目标被占用、是目录）也算失败
    except BaseException as err:
        if ready:
            _cleanup_after(err, f"删除 {part}", lambda: part.unlink(missing_ok=True))
        _cleanup_after(err, f"删除 {td}", tmp.cleanup)
        raise
    else:
        tmp.cleanup()
