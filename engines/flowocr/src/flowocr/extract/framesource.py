"""采样帧从哪来：`cv2` 顺序解码 / ffmpeg 子进程只送采样帧 / 在两者之上叠预取。

hw-decode 计划的 B 和 C；实测在 hw-decode-results 报告，结论已写进 docs/architecture/defaults.md 的 1.2 / 1.7 / 1.8 节。

**判据和纯逻辑放在这里，是为了让 `tests/test_guards.py` 够得着**——
和 `flowocr.extract.ocr_complete`、`flowocr.extract.ptsclock` 同一个理由（audit-4 C8）。
`cv2` 只在类里按需 import，所以这个模块在没装 cv2 的解释器上也能被守卫导入。

## 三个来源

| | 谁在解码 | 采样格 | 什么时候用 |
| --- | --- | --- | --- |
| `FfmpegSource` | ffmpeg 子进程；**默认走 NVDEC**（`--hwaccel cuda`，2026-09-20），交付不齐时退回 `libdav1d` 等软解 | **默认按 pts**（`round(t*fps)`，2026-09-20）；`index` = `select='not(mod(n,步长))'` 回退（只配等距网格） | **默认**（2026-09-10）。av1 上端到端 3.4×；vp9/h264 打平。产物和 cv2 不逐字节相同，但差异是噪音级的（docs/architecture/defaults.md §1.2） |
| `Cv2Source` | OpenCV（软解） | 按帧号数着看在不在采样网格上，非采样帧 `grab()` | 旧默认，留给 A/B 和回退（`--decoder cv2`） |
| `PrefetchSource` | 包在上面任一个外面 | 不变 | C：解码与推理重叠 |

## FfmpegSource 的命令为什么长成这样

1. **`select` 而不是 `fps`**：`fps` 是补帧 / 丢帧滤镜（转恒定帧率），
   pts 缺口处会**复制帧填上**，f3/f4 上凭空造观测。`select` 只挑不造。
2. **`-fps_mode passthrough`**：否则 muxer 那层还会再补一次帧。
3. **nv12 而不是 bgr24**：管道带宽减半，而且 NV12→BGR 用 OpenCV 自己那套转换，
   和 `Cv2Source` 同源——实测 vp9 上像素差 max|Δ| 从 11 降到 3。
4. **pts 从 `showinfo` 的 stderr 逐帧读**：管道里是裸像素、没有时间戳，
   不读 stderr 就只能按帧号推，那又回到平均帧率那个坑
   （docs/dev-guide/pitfalls.md「容器平均帧率不是时间轴」）。
5. **默认走 `-hwaccel cuda`**（2026-09-20，owner 定；理由是 **CPU 不是墙钟**：wuwa-s2 5 分钟
   CPU 秒 319 -> 189（−41%），墙钟只 −3.2%~−4.6%，docs/architecture/defaults.md §1.8）。能翻是因为先修好了两件事：
   * **采样格按 pts 数**（`--frame-select pts`）：NVDEC 在**有前摇的 av1**（首包 pts 为负）上少交付 1~3 帧，
     按交付顺序数的格子会**整条静默平移**；按 pts 数只是某一格取不到（hw-decode-results）；
   * **辅助流跟着硬解一起走**：辅路在显存里 `scale_cuda` 缩完再 `hwdownload`，否则开硬解就退回两遍回抠。
   交付不齐的素材由 `hwaccel_blocker` 试解几格发现、**自动退回软解**。
   * **有前摇的 av1 从文件头起解**（`gi-s2` 连 pts 0.000 都不交付）2026-09-22 修了：`-ignore_editlist 1` +
     `setpts` 按整数刻度平移回原时间轴（`av1_preroll_ticks`），NVDEC 一帧不丢、逐字节同软解；
     试解多核一道像素（平移错了格子照样都在，只有像素看得出来）。只认 av1 且没有帧重排的流（hw-decode-results）。
   ⚠ 原来这里写的是"实测 NVDEC 的 av1 路径**在丢帧**"——2026-09-10 被 hw-decode-results 推翻：
   NVDEC 对 h264/hevc/vp8/vp9/av1（含 film grain）**全部 bit-exact**，问题只在启动那几帧没交付。
"""
from __future__ import annotations

import re
import socket
import subprocess
import threading
import time
from queue import Queue

from flowocr.extract import timeline as _tl   # --timeline（decode-buffer）；只有标准库
from flowocr.extract.framegrid import AuxGrid, TimeGrid

DEFAULT_DECODER = "ffmpeg"
"""**全局唯一的默认解码器**（owner 2026-09-10 定：不接受"有的用 cv2 有的用 ffmpeg"）。

`run_ocr2 --decoder` 的默认值、`ocr_complete.obs_reusable` 的复用判据都读这一个常量——
改默认只改这里，两处一起变。依据在 docs/architecture/defaults.md §1.2：有剧本的素材上命中集合差
+0/+0/+1、重复认领 0→0，差异是判决边界上的单字符抖动；av1 上端到端 3.4×。
"""

PTS_RE = re.compile(r"\bpts_time:(\d+(?:\.\d+)?)")
"""`showinfo` 那一行里的 `pts_time:`。"""

AUX_PTS_RE = re.compile(r"pts_time:(-?[0-9]+(?:[.][0-9]+)?)")
"""辅助流取首帧 pts 用（允许负号）。采样流那条仍用 PTS_RE：负 pts 交给 ptsclock 当坏时间轴拦。"""

QUEUE_SENTINEL = object()
"""预取队列的结束标记。**不能用 None**——None 是合法的"这一帧没解出来"。"""

SEEK_REV = 2
"""精确 seek 的版本，写进 `_meta.seek_rev`：2 = 退半帧（2026-09-26，毫秒量化 pts 的起点帧不再被丢）。
没有这个键的产物是修之前的；`ocr_complete.obs_reusable` 只把其中"窗口从片中起、首个采样点晚了一格、记了缺格"的作废。"""


def parse_pts_line(line: str) -> float | None:
    """从一行 ffmpeg stderr 里取 `pts_time`（秒）；不是那种行就返回 None。"""
    m = PTS_RE.search(line)
    return float(m.group(1)) if m else None


def grid_skew_of(t_sec: float, idx: int, src_fps: float) -> int:
    """这一帧的真实 pts 对**给定的帧号**差了几源帧。**纯函数，守卫直接测它。**
    真值是**容器的 pts**，不是"另一条解码路"（hw-decode-results）。"""
    return round(t_sec * src_fps) - idx


SHOWINFO_RE = re.compile(r"\[showinfo@(main|aux) @ [^]]*\] n: *(\d+) .*?\bpts_time:(\d+(?:\.\d+)?)")
r"""`showinfo@main` / `showinfo@aux` 那一行：哪一路 + 它自己的帧计数 `n` + `pts_time`。
滤镜图里两路各挂一个**命名实例**，stderr 的前缀就是实例名，所以同一个进程里两路的 pts 分得开（2026-09-20）。

⚠ **不锚行首**（2026-09-20 晚踩的）：ffmpeg 的进度行 `frame=… \r` **用回车结尾、不换行**，
紧跟着的 showinfo 行就粘在它后面。第一版写的是 `^\[showinfo@`，粘住的那行认不出、那个 pts 丢了，
于是之后**每一帧都拿到下一帧的 pts**、整体错开一格——5 分钟窗里丢几十行，回抠信号晚了 25 帧，
打字机真值上全字那一档从 7/8/16 塌到 0/1/4。命令里现在加了 `-nostats`（从源头关掉进度行）、
drain 按 `\r` 也切行，**再加上 `n` 逐帧核对**：以后不管为什么丢行，都会当场报错而不是静默错位。"""


def parse_showinfo(line: str) -> tuple[str, int, float] | None:
    """一行 stderr -> (`main` | `aux`, 帧计数 n, pts 秒)；不是那种行就 None。**纯函数，守卫直接测它。**"""
    m = SHOWINFO_RE.search(line)
    return (m.group(1), int(m.group(2)), float(m.group(3))) if m else None


def grid_step(prev_idx: int | None, idx: int, start_idx: int, grid: TimeGrid) -> tuple[int, list[int]]:
    """这一帧对采样格：`(off, skipped)`。**纯函数，守卫直接测它。**

    * `off`：偏离格子几帧（0 = 就在格上）。格子是采样网格 `grid`（`framegrid.TimeGrid`，锚在帧号 0 上），`start_idx` 在格上；
    * `skipped`：上一帧和这一帧之间**没交付的格**（帧号列表）。

    为什么要分开两件事（2026-09-20，Codex 复审 P1 第 2 条）：原来只有一个"交付序号算出来的帧号 vs pts"的差，
    **素材自己的缺口**会让它非零，于是硬解在合法缺口之后照样当场炸——f3/f4 这种有缺口的素材上，
    默认硬解会在预检之后、跑到缺口处**中途退出**。偏离格子（`off`）才是"交付错了帧"；
    缺格（`skipped`）要再问一句"源里到底有没有这一格"，见 `source_has_frames`。
    """
    off = idx - grid.floor(idx)
    base = start_idx if prev_idx is None else prev_idx + 1
    return off, grid.frames(base, idx - 1)


def probe_interval(t0: float, t1: float) -> str:
    """`ffprobe -read_intervals` 的区间串。**起点 ≤ 0 时写成"从头读到 t1"**（见 `source_has_frames`）。纯函数。"""
    return f"{t0:.6f}%{t1:.6f}" if t0 > 0 else f"%{t1:.6f}"


def source_has_frames(video: str, idxs: list[int], src_fps: float) -> list[int]:
    """`probe_source_frames` 的保守版：探不出来时**按"源里有"算**（硬解那条路用：宁可错退回软解，也不放过真吞帧）。"""
    got = probe_source_frames(video, idxs, src_fps)
    return list(idxs) if got is None else got


def probe_source_frames(video: str, idxs: list[int], src_fps: float) -> list[int] | None:
    """`idxs` 里哪些帧号**源文件里真的有**（按容器的包 pts，`round(pts * src_fps)` 同选帧判据）；**探不出来返回 None**，
    由调用方按自己的代价定怎么算（硬解：当"源里有"、退回软解；解码分片的软解路：放行 + 记一笔，2026-09-24 审核）。

    给硬解那条路判"没交付的格是解码器吞了、还是素材本来就缺"：源里有 = 解码器吞了（NVDEC 在前摇里
    少交付就是这个形状）；源里没有 = 合法缺口。只在真出现缺格时调一次，代价是一次很短的 ffprobe。
    ⚠ 探不出来（ffprobe 失败 / 超时 / 一个包都没读到）**不能**读成"源里没有 = 合法缺口"。"""
    if not idxs:
        return []
    t0 = min(idxs) / src_fps - 1.0
    t1 = max(idxs) / src_fps + 1.0
    # ⚠ **起点 ≤ 0 时要"从头读到 t1"**：首包 pts 为负的文件（gi-s2 −5.4 s）上 `-read_intervals 0%1`
    # **一个包都读不出来**（实测；`0.5%1.5` 却会退回前面的关键帧、照常读到）。第一版把 t0 截到 0，
    # 于是 gi-s2 那个"源里明明有第 0 帧"被答成"没有"——中途出这种事就会把真吞帧放过去
    iv = probe_interval(t0, t1)
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time",
             "-read_intervals", iv, "-of", "csv=p=0", video],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        # ⚠ 正常退出、但报错码非零（读不了文件之类）时 stdout 是空的——**不能**读成"源里没有 = 合法缺口"，
        # 那正好和这个函数的保守原则反着（第一版就这么写反了，守卫钉住）
        return None
    out = r.stdout
    have = set()
    for tok in out.split():
        try:
            have.add(round(float(tok.strip(",")) * src_fps))
        except ValueError:
            continue
    if not have:
        # 一个包都没读到 = 探不出来，不是"源里没有"：窗口比跳过的格前后各多 1 秒，
        # 一定盖得住上一个交付出来的帧，合法缺口不可能读出空集
        return None
    return [i for i in idxs if i in have]


def id_rate(nominal: float | None, avg: float) -> float:
    """**帧身份用的帧率**：容器的名义帧率（`r_frame_rate`），不合理时退回平均帧率。**纯函数，守卫直接测它。**

    为什么不能用平均帧率（2026-09-20，Codex 复审第三轮 P1，复现过）：`round(t × 平均帧率)` 只在平均帧率
    等于局部帧率时才唯一。有丢帧缺口的文件平均帧率偏低（f3/f4 是 59.978、实际帧间隔 1/60），
    每一帧的 `t × 59.978` 比真实帧号少 0.00036，小数部分每约 2780 帧跨一次 0.5——**相邻两帧取整到同一个号**，
    主路一个采样格选中两帧、帧环后一帧覆盖前一帧（60 fps 下约每 46 s 一次）。合成缺口视频更直接：
    平均帧率 8.83、名义 10，按平均帧率算辅助流 6 个重复键。
    名义帧率按定义是"能精确表示所有时间戳的最低帧率"，每一帧占自己一个格点，所以按它取整**天然唯一**，
    缺口处留洞、帧号就是真实的源帧位置。

    合理的范围：`[avg × 0.999, avg × 2]`——丢帧只会让平均帧率**低于**名义帧率；名义帧率比平均高出一倍以上
    （把时间基当帧率报的容器）就不信它。**不管用哪个，两路都还有碰撞守卫**：帧号必须严格递增，否则当场报错。"""
    if nominal and avg * 0.999 <= nominal <= avg * 2:
        return float(nominal)
    return float(avg)


FAULT_ENV = "FLOWOCR_FAULT_HWACCEL_AT"
"""**只给测试用**：硬解那条路在交付第 N 帧时假装交付不对（`HwaccelMismatch`），验证整段改软解重跑那条路。"""


class HwaccelMismatch(RuntimeError):
    """硬解交付的帧和容器 pts 对不上（**不是**素材自己的缺口）。`run_ocr2` 见到它就整段改软解重跑一次。"""


class SinkClosed(RuntimeError):
    """辅助流的 sink 拒收像素（块已失败 / 取消、分片已收摊）。`AuxReader` 把它记进 `refused`，不记成"pts 和帧对不上"。"""


def pts_frame(t_us: int, idx: int, src_fps: float) -> int:
    """写进 obs 的那个 `frame`：**这一帧的 pts 说它是第几帧**。**判据只有这一份。**

    09-09 把 `t_us` 改成真实 pts 时 `frame` **漏了没改**，有丢帧缺口的素材上差整个缺口
    （f3 跨缺口窗实测 309~330 帧），2026-09-20 补上（hw-decode-results）。
    **写 obs 的地方有两处**（`run_ocr2` 的主路 + `reuse_v2` 的延迟落盘），所以必须是一个函数。

    ⚠ 2026-09-20 晚（Codex 复审 P1）：**来源 yield 的 `idx` 本身现在就是 pts 帧号**
    （`FfmpegSource` / `Cv2Source` 都是），这一步因此是幂等的，留着当保险。
    之前那一版让 `idx` 留在"第几个采样帧 × stride + 起点"、只在写出去时换成 pts——
    **那是第三种编号**：它既不是 pts 帧号，也不是辅助流的交付序号（后者只有 index 选帧时才碰巧相等），
    于是 pts 选帧 + 缺口之后，回抠拿着同一个号从帧环里取到的是**另一张画面**（`aux_identity_check.py` 复现）。
    """
    return idx + grid_skew_of(t_us / 1e6, idx, src_fps)


PREROLL_PROBE_PACKETS = 64
"""`av1_preroll_ticks` 核"没有帧重排"时看前多少个包（pts == dts）。"""


def av1_preroll_ticks(video: str) -> int:
    """**有前摇的 av1 从文件头起解时，NVDEC 少交付 1~3 帧的修法**要平移的量（时间基的整数刻度）；不适用返回 0。

    病因（2026-09-22，decode-buffer / hw-decode-results）：YouTube 那批 av1 的 mp4 带 edit list，
    首包 pts 为负（−2.6 ~ −5.4 s 的前摇），ffmpeg 按 edit list 丢掉前摇帧时 NVDEC 少交付头几帧
    （`gi-s2` 连 pts 0.000 都没有）。`-ignore_editlist 1` 让前摇帧照常交付，这时 NVDEC 一帧不丢、逐字节同软解；
    再在滤镜图最前面 `setpts=PTS-刻度` 平移回原来的时间轴，负 pts 的前摇被 `select=gte(t,0)` 挡掉。
    4 条带前摇的 av1 × 1501 帧：时间戳和带 edit list 的软解逐帧相同、像素逐字节相同。

    **只认 av1、只认没有帧重排的流**：刻度 = 同一个首包带 / 不带 edit list 的 pts 之差，这只在 pts == dts 时等于
    edit list 的偏移。带 B 帧的 h264 / HEVC 上实测对不上（时间戳全对、画面全错），而它们本来也不少交付
    （自造的带前摇 h264 / HEVC 上当前硬解就是 0），所以不碰。刻度必须是整数：按秒换算会在 1/15360 这类时间基上差一刻。
    """
    def probe(*extra: str) -> list[tuple[int, int]]:
        cmd = (["ffprobe", "-v", "error", *extra, "-select_streams", "v:0", "-show_entries", "packet=pts,dts",
                "-read_intervals", f"%+#{PREROLL_PROBE_PACKETS}", "-of", "csv=p=0", video])
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
        except Exception:                                   # noqa: BLE001
            return []
        rows = []
        for line in out.splitlines():
            p = line.strip().split(",")
            if len(p) >= 2 and p[0].lstrip("-").isdigit() and p[1].lstrip("-").isdigit():
                rows.append((int(p[0]), int(p[1])))
        return rows

    try:
        codec = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                "stream=codec_name", "-of", "csv=p=0", video],
                               capture_output=True, text=True, timeout=60).stdout.strip()
    except Exception:                                       # noqa: BLE001
        return 0
    if codec != "av1":
        return 0
    edit = probe()
    if not edit or edit[0][0] >= 0 or any(p != d for p, d in edit):
        return 0
    raw = probe("-ignore_editlist", "1")
    if not raw:
        return 0
    return raw[0][0] - edit[0][0]


def build_ffmpeg_cmd(video: str, *, grid: TimeGrid, start_sec: float,
                     n_out: int, pix: str = "nv12",
                     hwaccel: str | None = None, aux: dict | None = None,
                     select_by: str = "index", src_fps: float = 0.0,
                     preroll_ticks: int = 0, block: tuple[int, int, float | None] | None = None) -> list[str]:
    """只送采样帧的那条命令。**纯函数，守卫直接测它。**

    `grid`：采样网格（`framegrid.TimeGrid`）。`-ss` 放在 `-i` 前面（快速且精确）；`index` 选帧时 seek 之后 `select` 的 `n`
    从 0 重新数，而窗口起点已经对齐到网格，所以等距网格上 `n % 步长 == 0` 和整片跑落在同一批帧上
    （不等距的网格只配 `pts` 选帧：`n` 对不上绝对帧号）。

    `hwaccel`（**生产默认 `cuda`**，2026-09-20；见文件头 §5）：给了就走
    `-hwaccel X -hwaccel_output_format X`，并在滤镜链里插 `hwdownload,format=nv12`
    ——**丢弃发生在 hwdownload 之前**，这才是"跳过的帧不过 PCIe"。辅路在显存里 `scale_cuda` 缩完再下传。

    `select_by`：`index`（这个函数的默认 = 旧行为，探针直接调用时用）/ `pts`（**生产默认**，`ocr_args.resolve` 定）。

    `aux`（两遍合一遍，flowocr.extract.edge_refine）：`{"grid", "scale", "n_frames", "url"}` 给了就**同一次解码分两路**
    （`split`）：采样帧照旧走 stdout，辅助流（按 `grid` 取帧——`AuxGrid.as_list()`，时间网格 + 采样帧嵌套、缩到 scale、灰度）
    走 `url`（本机 TCP）。不等距的网格只配按 pts 选帧（按帧号 `n` 数的选法起点不在 0 时对不上绝对帧号）。
    解码只做一次——单独再起一个全帧率的 ffmpeg，vp9 上 3 个 worker 就是 6 路 1080p 软解一起抢 CPU。

    `preroll_ticks`（`av1_preroll_ticks` 给的）> 0：`-ignore_editlist 1` + 滤镜图最前面（辅路 `split` 之前）
    `setpts=PTS-刻度`，两路看到的都是原来的时间轴。只配"按 pts 选帧、从文件头起解"：`index` 选帧会把前摇帧也数进去，
    seek 到非零位置本来就不少交付（hw-decode-results），这两种组合直接拒绝。

    `block = (lo, hi, 关键帧秒或 None)`（解码分片的一块，`decode_shards`；只配 `select_by='pts'`）：两路都只取帧号 `[lo, hi)` 的帧、
    输入端 `-t` 读到块尾——块和块之间不重不漏靠的是帧号区间，不是 seek 落点。关键帧秒给了：`-ss <关键帧> -noaccurate_seek` 从那个关键帧起解（不多解一帧）；
    None（窗口的第一块）：照普通路径从 `start_sec` 精确 seek（文件头有前摇的 av1 照样平移，`-t` 多留 10 s 余量盖住前摇）。
    ⚠ 第一块**也要**带区间和 `-t`（2026-09-24 自检查出）：原来它走普通路径、辅助流按**数量**收口，缺口落在第一块里时越过块尾多读，
    读进第二块的帧号，转发给回抠进程时帧号倒退，回抠那边判"帧号不唯一"当场停掉辅助流（f3 26340–26400：3,377 帧只送出 1,009）。

    **`probe_ffmpeg_decode.py` 也用这一份**（2026-09-10）：它原来自带一份
    `build_cmd`，而那一份**没有 `-copyts`**——给探针加 `--start` 时会原样复活
    「窗口时间轴整体平移几个小时、没有任何一层报错」那个坑。同一条判据只留一份。
    """
    if n_out < 1:
        raise ValueError(f"要送出的帧数必须 ≥ 1，给的是 {n_out}")
    if pix not in ("nv12", "bgr24"):
        raise ValueError(f"不支持的像素格式 {pix!r}")
    # `-nostats`：进度行 `frame=… \r` 不换行，会把紧跟着的 showinfo 行粘住（见 SHOWINFO_RE 的说明）
    if preroll_ticks and (select_by != "pts" or start_sec > 0):
        raise ValueError("preroll_ticks 只配按 pts 选帧、从文件头起解（index 会把前摇帧数进去；seek 本来就不少交付）")
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info", "-nostdin"]
    if hwaccel:
        cmd += ["-hwaccel", hwaccel, "-hwaccel_output_format", hwaccel]
    if preroll_ticks:
        cmd += ["-ignore_editlist", "1"]
    # **精确 seek 落在起点帧前半帧**（2026-09-26）：起点帧号本身在采样格上，而容器 pts 可能比 `帧号 / 帧率` 略小——
    # 毫秒量化的直播录像（yuka input1–5：time_base 1/16000、pts 按 ms 取整）上帧号 ≡ 2 (mod 3) 的帧都早 0.33 ms，
    # `-ss 帧号/帧率` 就把这一帧当成 seek 点之前的丢掉：窗口第一格没交付，硬解那条路还因此判"吞帧"整段退回软解
    # （`--fps 2.2` 的 yuka-f5 14843 s 窗口实测；默认 2 fps 的格子帧号都是 30 的倍数、pts 精确，碰不到）。
    # 前后相邻帧离起点都是整一帧，退半帧不会多放进任何一帧，`index` 选帧的 n 也不变
    # 修之前的产物靠 `_meta.seek_rev`（`SEEK_REV`）认出来，复用判据按"首格被丢"的形状局部作废（`ocr_complete.obs_reusable`）
    seek = start_sec - 0.5 / src_fps if src_fps > 0 else start_sec
    rng = ""
    if block is not None:
        lo, hi, kf = block
        if select_by != "pts" or src_fps <= 0 or (kf is not None and preroll_ticks):
            raise ValueError("block 只配按 pts 选帧；从关键帧起解的块不带前摇平移（文件头那一块 kf=None 走普通 seek）")
        rng = rf"*between(round(t*{src_fps:.9f})\,{lo}\,{hi - 1})"
        # 输入端 `-t`：读到块尾再多半帧就停（素材缺口让数量凑不满时，别一路解到文件尾）
        if kf is not None:
            cmd += ["-ss", f"{kf:.6f}", "-noaccurate_seek", "-t", f"{max(0.0, (hi - 0.5) / src_fps - kf):.6f}", "-copyts"]
        else:
            dur = (f"{max(0.0, (hi - 0.5) / src_fps - (seek if start_sec > 0 else 0.0)) + (10.0 if preroll_ticks else 0.0):.6f}")
            cmd += (["-ss", f"{seek:.6f}", "-t", dur, "-copyts"] if start_sec > 0 else ["-t", dur])
    elif start_sec > 0:
        # **`-copyts` 不是可选的。** `-ss` 默认把输出时间戳**重新归零**，
        # 于是窗口跑出来的 `t_us` 从 0.001 起，而不是原片绝对时间——
        # 一整份产物的时间轴平移几个小时，而**没有任何一层会报错**
        # （obs 层对账第一次跑就抓到了：甲 14843.0 起、乙 0.001 起）。
        # `--start` 的契约是"时间戳仍按原片绝对时间写"，靠这个开关兑现。
        cmd += ["-ss", f"{seek:.6f}", "-copyts"]
    cmd += ["-i", video]
    if select_by == "pts":
        # **按时刻选，不按交付顺序选**（2026-09-20，hw-decode-results）。
        # `n` 数的是"进到 select 的第几帧"，解码器少交付几帧就整条格子平移；
        # `t` 是帧自己的 pts，少交付只会让某一格**取不到**，不会让后面所有格子错位。
        # `gte(t,0)` 挡掉 edit list 那段负 pts 的前摇（三段 av1 是 −2.7~−5.4 s）。
        if src_fps <= 0:
            raise ValueError("select_by='pts' 要 src_fps（格子按 round(t*src_fps) 数）")
        cond = grid.select(rf"round(t*{src_fps:.9f})")
        chain = [rf"select='gte(t\,0){rng}" + (f"*{cond}" if cond else "") + "'"]
    elif select_by == "index":
        if not grid.uniform:
            raise ValueError("不等距的采样网格只配按 pts 选帧（`n` 从 seek 点起数，对不上绝对帧号）")
        chain = [rf"select='not(mod(n\,{grid.q}))'"]
    else:
        raise ValueError(f"不认识的选帧方式 {select_by!r}（可选 index / pts）")
    if hwaccel:
        chain += ["hwdownload", "format=nv12"]
    if pix == "bgr24":
        chain.append("format=bgr24")
    chain.append("showinfo@main=checksum=0")   # 命名实例：两路的 pts 从 stderr 前缀分得开（parse_showinfo）
    if aux:
        ag = AuxGrid.from_list(aux["grid"])
        if ag.sample != grid or aux["n_frames"] < 1 or not (0 < aux["scale"] <= 1.0):
            raise ValueError(f"辅助流参数不对：{aux}")
        # **辅路和主路用同一套帧身份**（2026-09-20，复审 P1）：pts 选帧时辅路也按 pts 数、也挡掉负 pts 的前摇，
        # 而且自己挂一个 `showinfo@aux`——AuxReader 按**这一帧自己的 pts** 给它编号，不再按"交付的第几张"。
        # 等距的网格生成的表达式和加时间网格之前逐字相同（`AuxGrid.select`）
        if select_by == "pts":
            cond = ag.select(rf"round(t*{src_fps:.9f})")
            b = [rf"select='gte(t\,0){rng}" + (f"*{cond}" if cond else "") + "'"]
        elif ag.uniform:
            cond = ag.select("n")
            b = [f"select='{cond}'"] if cond else []
        else:
            raise ValueError("不等距的辅助流网格只配按 pts 选帧（`n` 从 seek 点起数，对不上绝对帧号）")
        if hwaccel:
            # **辅助流跟着硬解一起走**（2026-09-20）：在**显存里**缩到 540p 再下传，
            # 过 PCIe 的数据量比软解那条（1080p 下来再缩）还小。原来这里是直接拒绝组合，
            # 于是 `--hwaccel` 一开就自动退回两遍回抠——而那一遍 35–105 s，
            # 把硬解省下的全吃回去还倒欠（defaults.md §1.8 的总账）。
            # ⚠ `scale_cuda` 不吃 `trunc(iw*r/2)*2` 这种表达式，所以宽高由调用方算好传进来。
            if aux["scale"] < 1.0:
                if not (aux.get("w") and aux.get("h")):
                    raise ValueError("硬解的辅路要 aux['w'] / aux['h']：scale_cuda 不吃 trunc(iw*r/2)*2 这种表达式")
                # ⚠ 缩放器和 CPU 那条（swscale）不是同一套：`edge` 数值会差（wuwa-s2 30 s 上带 edge 的 320 行
                # 只有 128 行逐字段相同，多数差 1 帧、12 处差 10 帧以上）。试过 `interp_algo=bicubic` 对齐
                # swscale 的默认——**更糟**（93 行相同），所以留默认插值。等价与否要拿打字机真值判
                b.append(f"scale_cuda={aux['w']}:{aux['h']}")
            b += ["hwdownload", "format=nv12"]
        elif aux["scale"] < 1.0:
            b.append(f"scale=trunc(iw*{aux['scale']}/2)*2:trunc(ih*{aux['scale']}/2)*2")
        b.append("format=gray")
        b.append("showinfo@aux=checksum=0")
        head = f"setpts=PTS-{preroll_ticks}," if preroll_ticks else ""
        fc = f"[0:v]{head}split=2[a][b];[a]{','.join(chain)}[sa];[b]{','.join(b)}[sb]"
        return cmd + ["-filter_complex", fc,
                      "-map", "[sa]", "-frames:v", str(n_out), "-fps_mode", "passthrough",
                      "-f", "rawvideo", "-pix_fmt", pix, "pipe:1",
                      "-map", "[sb]", "-frames:v", str(aux["n_frames"]), "-fps_mode", "passthrough",
                      "-f", "rawvideo", "-pix_fmt", "gray", aux["url"]]
    if preroll_ticks:
        chain.insert(0, f"setpts=PTS-{preroll_ticks}")
    return cmd + ["-vf", ",".join(chain),
                  "-frames:v", str(n_out),
                  "-fps_mode", "passthrough",
                  "-f", "rawvideo", "-pix_fmt", pix, "-"]


def hwaccel_blocker(video: str, *, grid: TimeGrid, start_idx: int, src_fps: float,
                    width: int, height: int, hwaccel: str, select_by: str = "pts",
                    probe: int = 4) -> str | None:
    """硬解在这段素材上开不开得了：试解前几格，采样格对不上就返回原因（开得了返回 None）。

    为什么要试解（2026-09-20，`--hwaccel` 进默认时补的）：NVDEC 在**有前摇的 av1**
    （首包 pts 为负）上会少交付 1~3 帧，而 `gi-s2` 那种连 pts 0.000 都不交付的，
    `--frame-select pts` 也选不出第 0 格。进了默认就不能让这类素材**整段跑不了**——
    照 `--workers` 的老规矩：**开不了就退回去、把原因打出来、`_meta` 记生效值**。

    代价是一次很短的 ffmpeg 启动（只要 `probe` 格）。判据复用 `FfmpegSource` 自己那道
    硬解格子核对，不另写一份。
    """
    end_idx = grid.at(grid.index(start_idx) + probe)          # 起点之后第 probe 格（等距时就是 start + probe × 步长）
    src = FfmpegSource(video, grid=grid, start_idx=start_idx,
                       end_idx=end_idx, src_fps=src_fps,
                       width=width, height=height, pix="nv12", hwaccel=hwaccel,
                       select_by=select_by)
    got = []
    try:
        for idx, _t, frame in src:
            got.append((idx, frame))
    except RuntimeError as e:
        return str(e).split("。")[0]
    except Exception as e:                                        # noqa: BLE001
        return f"{type(e).__name__}: {e}"
    finally:
        src.close()
    if len(got) < probe:
        return f"试解只拿到 {len(got)}/{probe} 格（硬解没把采样帧交付齐）"
    if src.preroll_ticks:
        # **平移修法要多验一道像素**：刻度算错时格子照样都在（pts 对得上），只是**画面整体错位**——
        # 格子核对看不出来。NVDEC 对软解逐字节相同（hw-decode-results），所以这里要求逐字节相同
        ref = FfmpegSource(video, grid=grid, start_idx=start_idx,
                           end_idx=end_idx, src_fps=src_fps,
                           width=width, height=height, pix="nv12", hwaccel=None, select_by=select_by)
        want = []
        try:
            for idx, _t, frame in ref:
                want.append((idx, frame))
        finally:
            ref.close()
        import numpy as np
        bad = [a[0] for a, b in zip(got, want) if a[0] != b[0] or not np.array_equal(a[1], b[1])]
        if bad or len(want) != len(got):
            return (f"有前摇的 av1 平移修法（{src.preroll_ticks} 刻度）和软解对不上：{len(bad)}/{len(got)} 格画面不同"
                    f"（首个帧 {bad[0] if bad else '—'}）")
    return None


class Cv2Source:
    """现在管线的读法：非采样帧 `grab()`（推进但不解码），采样帧 `read()`。

    产出 `(idx, t_sec, frame)`，`t_sec` 是 `CAP_PROP_POS_MSEC` 给的真实 pts。
    """

    def __init__(self, video: str, *, grid: TimeGrid, start_idx: int, end_idx: int,
                 src_fps: float) -> None:
        import cv2

        self._cv2 = cv2
        self.cap = cv2.VideoCapture(video)
        if not self.cap.isOpened():
            raise SystemExit(f"无法打开 {video}")
        self.grid, self.start_idx, self.end_idx = grid, start_idx, end_idx
        self.src_fps = src_fps
        self.stopped_early = False
        self.advanced = 0
        self.grid_off: dict[int, int] = {}
        self.grid_skipped = 0

    def describe(self) -> str:
        return "cv2（OpenCV 顺序解码，非采样帧 grab）"

    def __iter__(self):
        cv2 = self._cv2
        if self.start_idx:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, self.start_idx)
        idx = self.start_idx
        prev_pidx: int | None = None
        while idx < self.end_idx:
            if not self.grid.member(idx):
                if not self.cap.grab():
                    self.stopped_early = True
                    break
                idx += 1
                self.advanced += 1
                continue
            ok, frame = self.cap.read()
            if not ok:
                self.stopped_early = True
                break
            t = self.cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
            # 同 FfmpegSource：**帧身份 = pts 帧号**；`idx` 这个计数器只管 grab/read 的节奏
            pidx = round(t * self.src_fps)
            off, skipped = grid_step(prev_pidx, pidx, self.start_idx, self.grid)
            if off:
                self.grid_off[off] = self.grid_off.get(off, 0) + 1
            self.grid_skipped += len(skipped)
            prev_pidx = pidx
            yield pidx, t, frame
            idx += 1
            self.advanced += 1

    def close(self) -> None:
        self.cap.release()


def merge_intervals(idx: list[int], gap: int = 0) -> list[tuple[int, int]]:
    """帧号集合 -> 排好序、不重叠的闭区间；相邻区间间隔 <= gap 的并起来（select 表达式短一点）。"""
    out: list[list[int]] = []
    for i in sorted(set(idx)):
        if out and i - out[-1][1] <= gap + 1:
            out[-1][1] = max(out[-1][1], i)
        else:
            out.append([i, i])
    return [(a, b) for a, b in out]


def window_frames(intervals: list[tuple[int, int]], step: int = 1, keep: list[int] | None = None) -> list[int]:
    """窗口模式实际送出的帧号（升序）：区间里每 step 帧取一帧（按绝对帧号对齐），keep 里的帧（模板帧）无论如何都要。"""
    want = {i for a, b in intervals for i in range(a, b + 1) if i % step == 0}
    want.update(keep or ())
    return sorted(want)


def build_ffmpeg_window_cmd(video: str, intervals: list[tuple[int, int]], *, src_fps: float,
                            pix: str = "nv12", step: int = 1, keep: list[int] | None = None,
                            scale: float = 1.0, base_ts: float | None = None) -> tuple[list[str], int, int]:
    """只送这些帧号闭区间里的帧（回抠用：各 run 的解码窗）。返回 (命令, 起始帧号, 要送出的帧数)。

    `-ss` 到第一个区间的起点（对齐到帧），`select` 里的 n 从 0 重新数，所以表达式按 n + 起始帧号写；
    `-copyts` 同 build_ffmpeg_cmd。区间在 ffmpeg 里逐帧求值，几百个 between 项没问题；
    几千个的话调用方先用 merge_intervals(gap) 并。
    `step` > 1：区间内只取绝对帧号能被 step 整除的帧（外加 `keep` 里的模板帧）；`scale` < 1：缩放后再送管道
    （1080p60 全帧率 nv12 走 Windows 管道只有约 660 MB/s，是解码的 8 倍慢——2026-09-16 probe_fullrate）。

    ⚠ **`base_ts` 必须给真实 pts**（2026-09-18 审计的 P1）：`select` 里的 `n` 数的是**seek 之后交付的第几帧**，
    所以"n + base = 绝对帧号"只在 seek 恰好落在帧 `base` 上时成立。不给 `base_ts` 时退回
    `base / src_fps`，而 `src_fps` 是**容器平均帧率**——有丢帧缺口的素材上 seek 落错位置，
    交付的帧整体偏移，却仍被标成我们要的那些帧号（像素和写回时刻错配，reuse-budget）。
    调用方用 `ptsclock.IndexClock`（obs 的 pts 锚点）把帧号换成真实时间再传进来。"""
    if not intervals:
        raise ValueError("没有要解码的区间")
    if pix not in ("nv12", "bgr24"):
        raise ValueError(f"不支持的像素格式 {pix!r}")
    if step < 1 or not (0 < scale <= 1.0):
        raise ValueError(f"step 必须 ≥ 1、scale 在 (0, 1]，给的是 {step} / {scale}")
    base = intervals[0][0]
    n_out = len(window_frames(intervals, step, keep))
    expr = "+".join(rf"between(n\,{a - base}\,{b - base})" for a, b in intervals)
    if step > 1:
        expr = rf"({expr})*not(mod(n+{base}\,{step}))"
        if keep:
            expr += "+" + "+".join(rf"eq(n\,{k - base})" for k in sorted(set(keep)) if k >= base)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "info", "-nostdin"]
    if base > 0:
        cmd += ["-ss", f"{(base / src_fps if base_ts is None else base_ts):.6f}", "-copyts"]
    cmd += ["-i", video]
    chain = [f"select='{expr}'"]
    if scale < 1.0:
        chain.append(f"scale=trunc(iw*{scale}/2)*2:trunc(ih*{scale}/2)*2")
    if pix == "bgr24":
        chain.append("format=bgr24")
    chain.append("showinfo")
    return (cmd + ["-vf", ",".join(chain), "-frames:v", str(n_out), "-fps_mode", "passthrough",
                   "-f", "rawvideo", "-pix_fmt", pix, "-"], base, n_out)


class FfmpegWindows:
    """按帧号区间从 ffmpeg 管道取帧（回抠用）。产出 (帧号, pts 秒, BGR 帧)，帧号按区间顺序回填——
    select 只挑不造、顺序不变，第 k 个送出的帧就是并集里第 k 小的帧号。"""

    def __init__(self, video: str, intervals: list[tuple[int, int]], *, src_fps: float,
                 width: int, height: int, pix: str = "nv12", step: int = 1,
                 keep: list[int] | None = None, scale: float = 1.0, ts_of=None) -> None:
        import cv2

        self._cv2 = cv2
        self.video, self.intervals = video, intervals
        self.step, self.keep, self.scale = step, keep, scale
        # 缩放后的尺寸要和 ffmpeg 的 scale 表达式一致（trunc(x*s/2)*2）
        width, height = (int(width * scale / 2) * 2, int(height * scale / 2) * 2) if scale < 1.0 else (width, height)
        self.src_fps, self.W, self.H, self.pix = src_fps, width, height, pix
        # 帧号 -> 真实时间（秒）。**给了它才谈得上"取到的是要的那一帧"**：seek 按它算，
        # 交付的每一帧再拿 ffmpeg 报的 pts 和它对一次（2026-09-18 审计的 P1，见 build_ffmpeg_window_cmd）
        self.ts_of = ts_of
        self.proc: subprocess.Popen | None = None
        self.stderr_tail: list[str] = []
        self.stopped_early = False
        self.pts_checked = 0

    def __iter__(self):
        cv2 = self._cv2
        base0 = self.intervals[0][0] if self.intervals else 0    # seek 的那一帧（同 build_ffmpeg_window_cmd）
        cmd, base, n_out = build_ffmpeg_window_cmd(
            self.video, self.intervals, src_fps=self.src_fps, pix=self.pix, step=self.step,
            keep=self.keep, scale=self.scale,
            base_ts=(self.ts_of(base0) if self.ts_of is not None else None))
        wanted = window_frames(self.intervals, self.step, self.keep)
        nbytes = self.W * self.H * 3 if self.pix == "bgr24" else self.W * self.H * 3 // 2
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=nbytes * 2)
        stderr = self.proc.stderr
        pts_q: Queue = Queue()

        def drain() -> None:
            for raw in stderr:                        # type: ignore[union-attr]
                line = raw.decode("utf-8", "replace")
                t = parse_pts_line(line)
                if t is not None:
                    pts_q.put(t)
                else:
                    self.stderr_tail.append(line.rstrip())
                    del self.stderr_tail[:-8]
            pts_q.put(QUEUE_SENTINEL)

        th = threading.Thread(target=drain, daemon=True)
        th.start()
        import numpy as np

        for k in range(n_out):
            buf = self.proc.stdout.read(nbytes)       # type: ignore[union-attr]
            if len(buf) < nbytes:
                self.stopped_early = True
                break
            t = pts_q.get()
            if t is QUEUE_SENTINEL:
                self.stopped_early = True
                break
            a = np.frombuffer(buf, np.uint8)
            frame = (a.reshape(self.H, self.W, 3).copy() if self.pix == "bgr24"
                     else cv2.cvtColor(a.reshape(self.H * 3 // 2, self.W), cv2.COLOR_YUV2BGR_NV12))
            if self.ts_of is not None:
                # **取到的是不是要的那一帧**：ffmpeg 报的 pts（-copyts，绝对时间）对时钟算出来的期望值。
                # 容差半个采样间隔——超了说明 seek 落错位置（有缺口的素材上 base/src_fps 就会这样），
                # 这时候"按位置回填帧号"会把错帧当成对的，**必须当场炸**，不能默默写回时刻。
                want_ts = self.ts_of(wanted[k])
                if abs(float(t) - want_ts) > 0.5 / max(1e-6, self.src_fps):
                    self.close()
                    raise ValueError(
                        f"窗口解码取到的帧对不上：第 {k} 帧要的是帧号 {wanted[k]}（期望 pts "
                        f"{want_ts:.6f} s），ffmpeg 报的是 {float(t):.6f} s——"
                        f"seek 落错位置了（丢帧缺口的素材上 base/src_fps 就会这样，reuse-budget）")
                self.pts_checked += 1
            yield wanted[k], float(t), frame
        self.close()
        th.join(timeout=5)
        if stderr is not None and not stderr.closed:
            stderr.close()

    def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            if proc.stdout and not proc.stdout.closed:
                proc.stdout.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


CUVID = {"av1": "av1_cuvid", "vp9": "vp9_cuvid", "h264": "h264_cuvid", "hevc": "hevc_cuvid", "vp8": "vp8_cuvid"}
"""ffprobe 的 codec_name -> cuvid 解码器。cuvid 解码器有 `-resize`：缩放在解码硬件里做，不起 CUDA 核函数。"""


def probe_codec(video: str) -> str:
    """视频流的 codec_name（ffprobe）；探不到给空串。"""
    try:
        return subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name",
                               "-of", "csv=p=0", video], capture_output=True, text=True, timeout=60).stdout.strip()
    except Exception:                                 # noqa: BLE001
        return ""


def build_ffmpeg_fullrate_cmd(video: str, *, start_sec: float, n_frames: int, step: int = 1,
                              scale: float = 0.5, hwaccel: str | None = None,
                              size: tuple[int, int] | None = None, cuvid: str | None = None) -> list[str]:
    """两遍合一遍的**辅助流**（flowocr.extract.edge_refine）：从同一个起点起、每 step 帧送一帧、缩到 scale、**只送亮度**
    （`-pix_fmt gray`，回抠的信号本来就在灰度上算）。`-ss`/`-copyts` 同 build_ffmpeg_cmd，所以 `select` 的 n
    和采样流数的是同一批帧：第 k 个送出的帧 = 起始帧号 + k × step。1080p 全帧率 nv12 走 Windows 管道
    只有 660 MB/s、比 OCR 那遍还慢；540p 灰度是它的 1/6（reuse-budget 计划）。

    `hwaccel="cuda"`（`--refine-aux nvdec`，2026-09-16 深夜）：**辅助流走 NVDEC**——同一个 ffmpeg 分两路时第二路每帧要多
    ≈0.9 ms CPU（不管第二路做什么，哪怕只取 Y 平面；`aux_bench.sh`），而 NVDEC + `scale_cuda` 到 540p 再 `hwdownload`
    五分钟只要 6.7 s CPU（软解采样流的 dav1d 是 102.8 s）。硬解开头会少交付 0–3 帧（hw-decode-results 报告），
    所以带 `showinfo`、帧号按首帧 pts 对齐；出的是 nv12（`size` 给缩后的宽高，Python 侧取 Y 平面）。"""
    if step < 1 or n_frames < 1 or not (0 < scale <= 1.0):
        raise ValueError(f"step ≥ 1、n_frames ≥ 1、scale 在 (0, 1]：给的是 {step} / {n_frames} / {scale}")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "info" if hwaccel else "error", "-nostdin"]
    if hwaccel:
        cmd += ["-hwaccel", hwaccel, "-hwaccel_output_format", hwaccel]
        if cuvid:                                     # 解码器私有选项，必须在 -i 前面
            if size is None:
                raise ValueError("cuvid 辅助流要给缩后的尺寸 size=(W, H)")
            cmd += ["-c:v", cuvid] + (["-resize", f"{size[0]}x{size[1]}"] if scale < 1.0 else [])
    if start_sec > 0:
        cmd += ["-ss", f"{start_sec:.6f}", "-copyts"]
    cmd += ["-i", video]
    chain = [rf"select='not(mod(n\,{step}))'"] if step > 1 else []
    if hwaccel:
        if size is None:
            raise ValueError("硬解辅助流要给缩后的尺寸 size=(W, H)")
        if scale < 1.0 and not cuvid:
            chain.append(f"scale_{hwaccel}={size[0]}:{size[1]}")
        chain += ["hwdownload", "format=nv12", "showinfo"]
        return cmd + ["-vf", ",".join(chain), "-frames:v", str(n_frames), "-fps_mode", "passthrough",
                      "-f", "rawvideo", "-pix_fmt", "nv12", "-"]
    if scale < 1.0:
        chain.append(f"scale=trunc(iw*{scale}/2)*2:trunc(ih*{scale}/2)*2")
    chain.append("format=gray")
    return cmd + ["-vf", ",".join(chain), "-frames:v", str(n_frames), "-fps_mode", "passthrough",
                  "-f", "rawvideo", "-pix_fmt", "gray", "-"]


class _Forward:
    """把辅路 pts 转发到别的进程（`EdgeProxy.pts_put`）：和本地 `Queue` 同一个 `put` 接口，drain 线程不用分两套写法。"""
    __slots__ = ("put",)

    def __init__(self, put) -> None:
        self.put = put


class AuxReader:
    """同一个 ffmpeg 的第二路输出（本机 TCP）-> 灰度帧 -> `sink.push(帧号, 帧)`（edge_refine.FrameRing，满了阻塞）。
    先 listen 再起 ffmpeg，ffmpeg 起来就 connect；读完或出错都 `sink.close()`。"""

    def __init__(self, *, start_idx: int, n_frames: int, step: int | None, width: int, height: int, sink,
                 pts_q: "Queue | None" = None, src_fps: float = 0.0) -> None:
        self.start_idx, self.n_frames, self.step, self.Ws, self.Hs, self.sink = start_idx, n_frames, step, width, height, sink
        # `pts_q` 给了 = 按**这一帧自己的 pts** 编号（`round(t * src_fps)`，和采样帧同一套身份）；
        # 没给 = 退回"交付的第几张 × step + 起点"，只有等距网格（`AuxGrid.step` 不是 None）编得出来
        if pts_q is None and not step:
            raise ValueError("不等距的辅助流网格要按 pts 编号（给 pts_q）")
        self.pts_q, self.src_fps = pts_q, src_fps
        self.misaligned = ""          # 非空 = 辅路 pts 和帧对不上、提前停了（原因写这里，run_ocr2 记进 _meta.edge）
        self.refused = ""             # 非空 = sink 拒收（`SinkClosed`：下游已失败 / 取消），提前停了
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.url = f"tcp://127.0.0.1:{self.srv.getsockname()[1]}"
        self.sent = 0
        self.last_key: int | None = None   # 最后推进 sink 的帧号：辅助流盖到哪儿（run_ocr2 拿它对采样流的最后一帧，判"辅助流半路断了"）
        self.cpu_sec = 0.0
        self.stopped_early = False
        self.th = threading.Thread(target=self._run, daemon=True, name="aux-reader")
        self._life = threading.Lock()
        self._cancelled = threading.Event()
        self._conn = None

    def start(self) -> "AuxReader":
        self.th.start()
        return self

    def _run(self) -> None:
        import numpy as np

        nbytes = self.Ws * self.Hs
        conn = f = None
        c0 = time.thread_time()
        try:
            self.srv.settimeout(0.2)
            deadline = time.monotonic() + 60
            while not self._cancelled.is_set():
                try:
                    conn, _ = self.srv.accept()
                    break
                except socket.timeout:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("辅助流 60 s 内未连接")
            with self._life:
                if self._cancelled.is_set():
                    return
                self._conn = conn
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
            f = conn.makefile("rb")
            prev_key = -1
            for k in range(self.n_frames):                # 一次读多帧试过（少抢几次 GIL），读数不变，删了（reuse-budget）
                t_rd = time.perf_counter()
                buf = f.read(nbytes)
                _tl.span("aux_read", t_rd, a=k)
                if len(buf) < nbytes:
                    self.stopped_early = True
                    break
                if self.pts_q is not None:
                    t_q = time.perf_counter()
                    got = self.pts_q.get()
                    _tl.span("aux_pts_wait", t_q, a=k)   # 像素到了、pts 还没到（pts 从管线进程的 stderr 线程转来，decode-buffer）
                    if got is QUEUE_SENTINEL:
                        self.stopped_early = True          # 像素来了、pts 没来：宁可停，也不编一个帧号
                        break
                    sn, t = got
                    if sn != k:
                        # 同采样流：丢了一行 pts，之后每一帧都会拿到下一帧的号、整体错开（2026-09-20 实测过）。
                        # 停下辅助流（回抠退回采样级）比拿错画面算边界强
                        self.stopped_early = True
                        self.misaligned = f"第 {k} 帧字节配到了 showinfo 的第 {sn} 行"
                        break
                    key = round(t * self.src_fps)
                    if key <= prev_key:
                        # 帧身份必须唯一（复审第三轮 P1）：重号会让帧环里后一帧覆盖前一帧、同号不同画面
                        self.stopped_early = True
                        self.misaligned = f"第 {k} 帧 pts {t:.6f} 取整到帧 {key}，不大于上一帧 {prev_key}（帧号不唯一）"
                        break
                    prev_key = key
                else:
                    key = self.start_idx + k * self.step
                self.sink.push(key, np.frombuffer(buf, np.uint8).reshape(self.Hs, self.Ws))
                self.sent += 1
                self.last_key = key
        except SinkClosed as exc:
            self.stopped_early = True
            self.refused = str(exc)
        except (OSError, RuntimeError) as exc:
            self.stopped_early = True
            if not self._cancelled.is_set() and isinstance(exc, RuntimeError):
                self.misaligned = repr(exc)
        finally:
            self.cpu_sec = time.thread_time() - c0
            self.sink.close()
            with self._life:
                self._conn = None
            if f is not None:
                f.close()
            for s in (conn, self.srv):
                try:
                    if s is not None:
                        s.close()
                except OSError:
                    pass

    def join(self, timeout: float = 30.0) -> None:
        self.th.join(timeout)

    def abort(self) -> None:
        """取消接入、像素、PTS 和 sink 等待；读线程负责最终关闭连接。"""
        with self._life:
            self._cancelled.set()
            if self._conn is not None:
                try:
                    self._conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        if self.pts_q is not None:
            self.pts_q.put(QUEUE_SENTINEL)
        self.sink.close()


class FfmpegFullRate:
    """辅助流**单独一个** ffmpeg 子进程 -> 灰度帧 -> `sink.push(帧号, 帧)`。给 `--refine-aux nvdec / cuvid`（硬解）用；
    纯软解再起一个进程的对照臂量过，比共享解码还慢一倍（+55–60%），命令行入口已删（reuse-budget）。
    默认路径是 `FfmpegSource(aux=...)`——同一次解码分两路。读完或出错都 `sink.close()`。"""

    def __init__(self, video: str, *, start_idx: int, end_idx: int, src_fps: float, width: int, height: int,
                 step: int, scale: float, sink, hwaccel: str | None = None, cuvid: bool = False) -> None:
        self.video, self.start_idx, self.step, self.scale, self.sink = video, start_idx, step, scale, sink
        self.hwaccel, self.src_fps = hwaccel, src_fps
        self.cuvid = CUVID.get(probe_codec(video)) if (cuvid and hwaccel) else None   # 认不出编码就退回 scale_cuda
        self.n_frames = max(1, -(-(end_idx - start_idx) // step))
        self.start_sec = start_idx / src_fps
        self.Ws, self.Hs = ((int(width * scale / 2) * 2, int(height * scale / 2) * 2) if scale < 1.0
                            else (width, height))
        self.stopped_early = False
        self.sent = 0
        self.shift = 0                      # 硬解开头少交付的帧数（首帧 pts 对出来的），软解恒 0
        self.cpu_sec = 0.0
        self.proc: subprocess.Popen | None = None
        self.stderr_tail: list[str] = []
        self.th = threading.Thread(target=self._run, daemon=True, name="aux-fullrate")

    def start(self) -> "FfmpegFullRate":
        self.th.start()
        return self

    def _run(self) -> None:
        import numpy as np

        hw = self.hwaccel
        nbytes = self.Ws * self.Hs * 3 // 2 if hw else self.Ws * self.Hs     # nv12 / gray
        cmd = build_ffmpeg_fullrate_cmd(self.video, start_sec=self.start_sec, n_frames=self.n_frames,
                                        step=self.step, scale=self.scale, hwaccel=hw, size=(self.Ws, self.Hs),
                                        cuvid=self.cuvid)
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE if hw else subprocess.DEVNULL, bufsize=nbytes * 4)
        pts_q: Queue = Queue()
        th = None
        if hw:                                            # 帧号按 pts 对：硬解开头会少交付 0–3 帧
            stderr = self.proc.stderr

            def drain() -> None:
                # 只要首帧的 pts（对齐硬解开头少交付的几帧）；之后的 showinfo 行只消费不解析——每帧一行、五分钟 18000 行，
                # 逐行正则要 7 s CPU
                first = True
                for raw in stderr:                        # type: ignore[union-attr]
                    if not first:
                        continue
                    line = raw.decode("utf-8", "replace")
                    m = AUX_PTS_RE.search(line)           # 认负数：av1_cuvid 从文件开头解时 pts 从 −5.4 s 起，
                    t = float(m.group(1)) if m else None   # 不认负数就永远等不到"首帧 pts"、和 ffmpeg 互相卡死（2026-09-17）
                    if t is not None:
                        pts_q.put(t)
                        first = False
                    else:
                        self.stderr_tail.append(line.rstrip())
                        del self.stderr_tail[:-8]
                pts_q.put(QUEUE_SENTINEL)
            th = threading.Thread(target=drain, daemon=True)
            th.start()
        c0 = time.thread_time()
        try:
            for k in range(self.n_frames):
                buf = self.proc.stdout.read(nbytes)       # type: ignore[union-attr]
                if len(buf) < nbytes:
                    self.stopped_early = True
                    break
                if hw:
                    if k == 0:
                        t = pts_q.get()
                        if t is QUEUE_SENTINEL:
                            self.stopped_early = True
                            break
                        # pts 为负 / 离起点太远 = 这个解码器的时间戳不可信（cuvid），不做对齐
                        sh = int(round(t * self.src_fps)) - self.start_idx
                        self.shift = sh if 0 <= sh <= 8 else 0
                    frame = np.frombuffer(buf, np.uint8, count=self.Ws * self.Hs).reshape(self.Hs, self.Ws)   # nv12 的 Y 平面
                else:
                    frame = np.frombuffer(buf, np.uint8).reshape(self.Hs, self.Ws)
                self.sink.push(self.start_idx + self.shift + k * self.step, frame)
                self.sent += 1
        finally:
            self.cpu_sec = time.thread_time() - c0
            self.sink.close()
            self.close()
            if th is not None:
                th.join(timeout=5)

    def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            if proc.stdout and not proc.stdout.closed:
                proc.stdout.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)

    def join(self, timeout: float = 30.0) -> None:
        self.th.join(timeout)
        self.close()


class FfmpegSource:
    """ffmpeg 子进程只把采样帧送进管道；跳过的帧在子进程里解完就丢。

    **和推理天然并行**：解码在另一个进程里，Python 这边只等管道。
    """

    def __init__(self, video: str, *, grid: TimeGrid, start_idx: int, end_idx: int,
                 src_fps: float, width: int, height: int, pix: str = "nv12",
                 aux: dict | None = None, hwaccel: str | None = None,
                 select_by: str = "index", preroll_ticks: int | None = None,
                 block_seek: float | None = None, block: bool = False) -> None:
        import cv2

        self._cv2 = cv2
        self.block, self.block_seek = block or block_seek is not None, block_seek
        """解码分片的一块（`decode_shards`）：只取帧号 `[start_idx, end_idx)`，`block_seek` 给了就从那个关键帧秒起解
        （`build_ffmpeg_cmd` 的 `block`）；都没给 = 普通窗口（起点已对齐采样格）。"""
        self.hwaccel = hwaccel or None
        self.select_by = select_by
        # 有前摇的 av1 从文件头起硬解：平移修法（`av1_preroll_ticks`）。None = 按条件自动判；显式给 0 = 不修
        if preroll_ticks is None:
            preroll_ticks = (av1_preroll_ticks(video)
                             if self.hwaccel and start_idx == 0 and select_by == "pts" else 0)
        self.preroll_ticks = preroll_ticks
        self.video, self.grid = video, grid
        self.start_idx, self.end_idx = start_idx, end_idx
        self.grid0 = grid.ceil(start_idx)
        """块里第一个采样格（普通窗口起点已对齐采样网格，就是 start_idx）。"""
        self.src_fps, self.W, self.H, self.pix = src_fps, width, height, pix
        self.stopped_early = False
        self.advanced = 0
        self.proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._cancelled = False
        self.stderr_tail: list[str] = []
        # **采样格核对**（2026-09-20）。帧身份一律是 **pts 帧号**（`round(t * src_fps)`）；
        # 对着采样网格记两件事（`grid_step`）：
        #   `grid_off`     偏离格子的帧（{偏几帧: 几帧}）——index 选帧遇到缺口 / 解码器少交付时才有；
        #   `grid_skipped` 没交付的格数——pts 选帧遇到素材缺口时的正常形状
        # 硬解那条路：偏离格子就炸；缺格要问 `source_has_frames`，源里有才炸（复审 P1 第 2 条）。
        self.grid_off: dict[int, int] = {}
        self.grid_skipped = 0
        import os as _os
        self.fault_at = int(_os.environ.get(FAULT_ENV, "-1") or -1)
        # 两遍合一遍：`aux = {"sink", "grid", "scale"}`，同一次解码分出第二路（灰度、缩、按网格取帧）进 sink
        self.aux_cfg = aux
        self.aux: AuxReader | None = None

    def describe(self) -> str:
        s = f"ffmpeg 子进程（select + {self.pix} 管道，跳过的帧不进 Python）"
        if self.aux_cfg:
            s += (f" + 同一次解码分出辅助流（scale {self.aux_cfg['scale']}、"
                  f"{AuxGrid.from_list(self.aux_cfg['grid']).describe(self.src_fps)}、灰度、本机 TCP）")
        return s

    @property
    def n_out(self) -> int:
        """要送出多少帧：`[grid0, end_idx)` 里的格数。"""
        return self.grid.n_frames(self.grid0, self.end_idx)

    @property
    def frame_bytes(self) -> int:
        return self.W * self.H * 3 if self.pix == "bgr24" else self.W * self.H * 3 // 2

    def __iter__(self):
        cv2 = self._cv2
        # 起点用 `start_idx / src_fps`——这正是 `cap.set(POS_FRAMES, i)` 的语义
        # （实测：它本身就是按 `i / src_fps` 做的时间 seek），两条路落点一致。
        with self._proc_lock:
            if self._cancelled:
                return
            aux = None
            if self.aux_cfg:
                c = self.aux_cfg
                Ws, Hs = ((int(self.W * c["scale"] / 2) * 2, int(self.H * c["scale"] / 2) * 2) if c["scale"] < 1.0
                          else (self.W, self.H))
                ag = AuxGrid.from_list(c["grid"])
                n_aux = ag.n_frames(self.start_idx, self.end_idx)
                if c.get("remote") is not None:
                    # 辅助流收在**另一个进程**里（`--edge-proc`，edge_proc.EdgeProxy）：ffmpeg 直接 connect 过去，
                    # 这边只把 stderr 里的辅路 pts 逐行转发（`pts_put`）
                    url = c["remote"].url
                else:
                    aux_pts_q: Queue = Queue()
                    self.aux = AuxReader(start_idx=self.start_idx, n_frames=n_aux, step=ag.step, width=Ws, height=Hs,
                                         sink=c["sink"], pts_q=aux_pts_q, src_fps=self.src_fps).start()
                    url = self.aux.url
                # `w`/`h` 是给 `scale_cuda` 的（硬解那条路），表达式它不吃；软解那条仍按比例算，两边同一组数
                aux = {"grid": c["grid"], "scale": c["scale"], "n_frames": n_aux, "url": url,
                       "w": Ws, "h": Hs}
            cmd = build_ffmpeg_cmd(self.video, grid=self.grid,
                                   start_sec=self.start_idx / self.src_fps,
                                   n_out=self.n_out, pix=self.pix, aux=aux, hwaccel=self.hwaccel,
                                   select_by=self.select_by, src_fps=self.src_fps,
                                   preroll_ticks=self.preroll_ticks,
                                   block=(self.start_idx, self.end_idx, self.block_seek) if self.block else None)
            nbytes = self.frame_bytes
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, bufsize=nbytes * 2)
            proc = self.proc
            stderr = proc.stderr
        pts_q: Queue = Queue()

        aux_q = self.aux.pts_q if self.aux is not None else None
        if self.aux_cfg and self.aux_cfg.get("remote") is not None:
            aux_q = _Forward(self.aux_cfg["remote"].pts_put)

        drain_errors = []

        def drain() -> None:
            # **stderr 必须一直读**：不读满了管道就死锁，而 pts 也在里面。
            # 两路的 showinfo 是命名实例，按前缀分流：主路 -> pts_q、辅路 -> AuxReader 的队列
            try:
                for raw in stderr:                        # type: ignore[union-attr]
                    for line in raw.decode("utf-8", "replace").split("\r"):   # 进度行用 \r 结尾，别让它粘住下一行
                        got = parse_showinfo(line)
                        if got is not None:
                            (aux_q if got[0] == "aux" and aux_q is not None else pts_q).put((got[1], got[2]))
                        elif "pts_time" not in line and line.strip():
                            self.stderr_tail.append(line.rstrip())
                            del self.stderr_tail[:-8]
            except BaseException as exc:
                drain_errors.append(exc)
            finally:
                pts_q.put(QUEUE_SENTINEL)
                if aux_q is not None:
                    try:
                        aux_q.put(QUEUE_SENTINEL)
                    except BaseException as exc:
                        drain_errors.append(exc)

        th = threading.Thread(target=drain, daemon=True)
        th.start()

        import numpy as np

        n = 0
        prev_idx: int | None = None
        while n < self.n_out:
            t_rd = time.perf_counter()
            try:
                if self._cancelled:
                    break
                buf = proc.stdout.read(nbytes)       # type: ignore[union-attr]
            except (OSError, ValueError):
                if not self._cancelled:
                    raise
                break
            _tl.span("pipe_read", t_rd, a=n)
            if len(buf) < nbytes:
                self.stopped_early = True
                break
            t_q = time.perf_counter()
            got = pts_q.get()
            _tl.span("pts_wait", t_q, a=n)            # 像素到了、pts 还没到（stderr 线程跟不上时在这里等，decode-buffer）
            if got is QUEUE_SENTINEL:
                # 像素来了、pts 没来 = showinfo 没挂上或者被截断。
                # **宁可报错也不能编一个时间戳。**
                self.stopped_early = True
                break
            sn, t = got
            if sn != n:
                # showinfo 自己的帧计数和"这是第几帧字节"对不上 = 丢了（或多了）一行 pts，
                # 再往下每一帧都会拿到别人的时间戳。**当场炸，不静默错开**
                self.close()
                raise RuntimeError(f"采样流的 pts 行和帧对不上：第 {n} 帧字节配到了 showinfo 的第 {sn} 行"
                                   f"（stderr 丢行 / 多行），不能再往下配了")
            a = np.frombuffer(buf, np.uint8)
            t_cv = time.perf_counter()
            frame = (a.reshape(self.H, self.W, 3).copy() if self.pix == "bgr24"
                     else cv2.cvtColor(a.reshape(self.H * 3 // 2, self.W),
                                       cv2.COLOR_YUV2BGR_NV12))
            _tl.span("to_bgr", t_cv, a=n)
            # **帧身份 = pts 帧号**（2026-09-20，复审 P1）：辅助流的帧环用的也是它，所以同号必同画面
            idx = round(float(t) * self.src_fps)
            if prev_idx is not None and idx <= prev_idx:
                # **帧身份必须唯一**（复审第三轮 P1）：取整出重号 = 帧率和这份文件的时间戳对不上，
                # 再往下同号不同画面。当场炸，不静默覆盖
                self.close()
                raise RuntimeError(f"采样帧的帧号不唯一：pts {float(t):.6f} 取整到帧 {idx}，上一帧已经是 {prev_idx}"
                                   f"（帧率 {self.src_fps} 和这份文件的时间戳对不上，见 framesource.id_rate）")
            off, skipped = grid_step(prev_idx, idx, self.grid0, self.grid)
            if off:
                self.grid_off[off] = self.grid_off.get(off, 0) + 1
            if skipped:
                self.grid_skipped += len(skipped)
            if self.hwaccel and (off or skipped):
                # 硬解这条路必须交付**源里有的、格子上的**那一帧：
                #   偏离格子 = 交付错了帧（index 选帧 + NVDEC 在前摇里少交付，整条格子平移）；
                #   缺格且**源里有** = 解码器吞了帧（gi-s2 连 pts 0.000 都没交付）；
                #   缺格但源里没有 = 素材自己的缺口（f3/f4），**不是错**——原来这里会当场炸（复审 P1 第 2 条）
                eaten = source_has_frames(self.video, skipped, self.src_fps) if skipped else []
                if off or eaten:
                    self.close()
                    what = (f"偏离采样格 {off} 源帧（pts {float(t):.3f} = 帧 {idx}）" if off else
                            f"源里有、却没交付的格 {len(eaten)} 个（首个帧 {eaten[0]}，pts {eaten[0] / self.src_fps:.3f}）")
                    raise HwaccelMismatch(
                        f"--hwaccel {self.hwaccel} 交付不对：{what}。病因和量法见 hw-decode-results 报告")
            if idx >= self.end_idx:
                # **窗口按时刻收口，不按数量收口**（2026-09-20）。`-frames:v n_out` 是按**数量**
                # 收的，而 `--frame-select pts` 在丢帧缺口处那几格压根没有帧，于是 ffmpeg 会
                # **越过 `--end` 继续取**直到凑够数（f3 跨缺口的 60 s 窗实测多取约 5.2 s）。
                break
            n += 1
            prev_idx = idx
            if self.hwaccel and n == self.fault_at:
                self.close()
                raise HwaccelMismatch(f"（{FAULT_ENV} 注入的故障，第 {n} 帧）")
            # **推进量也认 pts**：缺口里那几格没有帧，按"送了几帧 × 间隔"数会把"跳过的时间"算成"没跑完"
            self.advanced = self.grid.next(idx) - self.start_idx
            yield idx, float(t), frame
        # 顺序不能反：drain 那个线程要读到 EOF 才结束，而 EOF 只有在 ffmpeg 退出
        # 之后才来，ffmpeg 又要等我们关掉 stdout。所以先 close 再 join。
        self.close()
        th.join(timeout=5)
        if stderr is not None and not stderr.closed:
            stderr.close()              # 线程已经退了，这时候关才安全
        if drain_errors and not self._cancelled:
            raise RuntimeError(f"解码 stderr / 辅助 PTS 转发失败：{drain_errors[0]!r}")

    def close(self) -> None:
        """**子进程一定要收掉。** 正常跑完是关管道让它自己退；

        但消费者中途 `break`（比如时间戳判据不过）时 ffmpeg 还在往管道里写，
        关了 stdout 它会拿到 EPIPE 自己退——万一没退就硬杀。
        漏一个 ffmpeg 在后台读着几个小时的素材，是长任务里最难查的那种问题。
        """
        # 句柄一直登记到 wait 完成：取消仍可 kill 唤醒并发的管道 close，
        # 后来的 close 等第一个回收者完成，不能把“被取走”当成“已退出”。
        with self._close_lock:
            with self._proc_lock:
                proc = self.proc
            if proc is None:
                return
            try:
                try:
                    if proc.stdout and not proc.stdout.closed:
                        proc.stdout.close()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
            finally:
                with self._proc_lock:
                    if self.proc is proc and proc.poll() is not None:
                        self.proc = None

    def cancel(self) -> None:
        """不可逆取消：阻止迟到启动，先杀子进程唤醒读，再回收管道。"""
        with self._proc_lock:
            self._cancelled = True
            proc = self.proc
            if proc is not None and proc.poll() is None:
                proc.kill()
            aux = self.aux
        if aux is not None:
            aux.abort()
        self.close()
        if aux is not None and aux.th is not threading.current_thread():
            aux.join(5)
            if aux.th.is_alive():
                raise RuntimeError("辅助流取消后读线程未退出")


class PrefetchSource:
    """C：把解码挪到后台线程，主线程只做 det / rec。

    **有界队列**（`depth × 帧大小`，1080p 每帧 6 MB），**严格保序**——
    单个生产者顺序放、单个消费者顺序取，复用判断依赖的"上一帧"仍然成立。
    """

    def __init__(self, inner, depth: int = 4) -> None:
        if depth < 1:
            raise ValueError(f"队列深度必须 ≥ 1，给的是 {depth}")
        self.inner, self.depth = inner, depth
        self.error: BaseException | None = None
        self._q: Queue = Queue(maxsize=depth)
        self._th: threading.Thread | None = None
        self._stop = False

    def describe(self) -> str:
        return f"{self.inner.describe()} + 预取队列（深度 {self.depth}）"

    @property
    def stopped_early(self) -> bool:
        return self.inner.stopped_early

    @property
    def advanced(self) -> int:
        return self.inner.advanced

    @property
    def grid_off(self) -> dict[int, int]:
        """采样格核对：偏离格子的帧（`FfmpegSource.grid_off`）。"""
        return getattr(self.inner, "grid_off", {})

    @property
    def grid_skipped(self) -> int:
        """采样格核对：没交付的格数（`FfmpegSource.grid_skipped`）。"""
        return getattr(self.inner, "grid_skipped", 0)

    def __iter__(self):
        # **每次迭代换一个新队列**：留着上一轮的残渣，第二次迭代会先吐出旧帧。
        # （现在只迭代一次，但这种"复用一次就坏"的东西不该留在共享层。）
        q = self._q = Queue(maxsize=self.depth)
        self._stop = False

        def produce() -> None:
            try:
                for item in self.inner:
                    if self._stop:      # close() 立的旗：别再往下解码了
                        break
                    t0 = time.perf_counter()
                    q.put(item)
                    _tl.span("prefetch_put", t0, a=item[0])
            except BaseException as exc:              # noqa: BLE001
                # **生产者的异常不能吞**：吞了就变成"提前没帧了"，
                # 而那和"正常读到头"在下游看起来一模一样。
                self.error = exc
            finally:
                q.put(QUEUE_SENTINEL)

        th = self._th = threading.Thread(target=produce, daemon=True)
        th.start()
        while True:
            item = q.get()
            if item is QUEUE_SENTINEL:
                break
            yield item
        th.join(timeout=10)
        if self.error is not None:
            raise self.error

    def close(self) -> None:
        """**先解开生产者、等它退干净，再关里层。**

        消费者中途 `break` 时（`run_ocr2` 的 `bad_pts` 就是这么跳出来的），
        生产者多半正卡在 `q.put`（队列满了）。要做三件事，顺序不能换：

        1. 排空队列——把卡在 `put` 上的生产者放出来；
        2. 立起停止旗，让它下一轮就退，不然它会一路解码到片尾；
        3. **join**——这一步 2026-09-10 补的。少了它，`inner.close()` 会和
           还活着的生产者抢同一个资源：`Cv2Source` 那条就是主线程
           `cap.release()` 撞上生产者线程 `cap.grab()`，OpenCV 上是未定义行为，
           可能直接把进程带走——于是 `bad_pts` 承诺的「退出码 4 + 留产物排查」
           反而拿不到。排空要和 join 交替做，因为生产者被放出来之后还会再放一个。
        """
        self._stop = True
        th = self._th
        deadline = time.monotonic() + 10.0
        while th is not None and th.is_alive() and time.monotonic() < deadline:
            self._drain()
            th.join(timeout=0.05)
        self._drain()
        self.inner.close()

    def _drain(self) -> None:
        """把队列腾空。队列有界，所以这个循环有上界。"""
        while True:
            try:
                self._q.get_nowait()
            except Exception:                         # noqa: BLE001  空了就算
                return


def make_source(video: str, *, backend: str, grid: TimeGrid, start_idx: int,
                end_idx: int, src_fps: float, width: int, height: int,
                prefetch: int = 0, pix: str = "nv12", aux: dict | None = None, hwaccel: str | None = None,
                select_by: str = "index"):
    """按名字造一个来源，需要的话再包一层预取。`aux` 只有 ffmpeg 后端认（两遍合一遍的第二路输出）。"""
    if backend == "cv2":
        if aux:
            raise ValueError("辅助流只有 ffmpeg 后端有（同一次解码分两路）")
        if select_by != "index":
            # cv2 是**自己按 idx 数着 grab/read**，选帧根本不经过 ffmpeg 的 select——
            # 静默忽略会让 A/B 的两条臂塌成一条（09-17 审计那个形状）。
            raise ValueError("--frame-select pts 只有 ffmpeg 后端有（cv2 是自己按帧号数的）")
        src = Cv2Source(video, grid=grid, start_idx=start_idx,
                        end_idx=end_idx, src_fps=src_fps)
    elif backend == "ffmpeg":
        src = FfmpegSource(video, grid=grid, start_idx=start_idx,
                           end_idx=end_idx, src_fps=src_fps,
                           width=width, height=height, pix=pix, aux=aux, hwaccel=hwaccel,
                           select_by=select_by)
    else:
        raise ValueError(f"不认识的解码后端 {backend!r}（可选 cv2 / ffmpeg）")
    return PrefetchSource(src, prefetch) if prefetch else src
