"""底板贴合背景色（阶段 4 的 `plate_color`，plate-color 计划的 H3）：生成完所有行之后的一遍，读源视频，把每块近黑底板换成
取自画面的小格色块，正文按底板亮度改黑 / 白字。

做法（每块底板，每个采样时刻）：
1. 取样范围 = 底板外扩 `MARGIN_PX`；顶帽 / 黑顶帽找出笔画（底板外的也算：取样带里的 ▼、相邻角色的头发都抹掉），膨胀盖住阴影，
   同一时刻别的底板整块抹掉（里面是别的字）。取样带里没被抹的不到 `MARGIN_GROW` 就把取样带加倍（至多 `MARGIN_MAX`），
   同一块底板加宽过就不再退回（否则占比在门槛上下的小名牌逐帧换宽度、颜色跳）。
2. 在 1/`INP_SCALE` 分辨率上用 OpenCV inpaint（Telea）从掩码四周补出背景，裁回底板范围（1/4 分辨率）。
3. 底板切成 `CELL_W` px × 半行高起步的小格，格色沿时间 3 点中位滤波；相邻、在整行时间里离合并后的均色都不超过 `MERGE_TOL`
   的格并成一个矩形；每个矩形的颜色按折线简化（容差 `KEY_TOL`）写成 `\\t` 关键帧，格间重叠、轻羽化。
4. 打字机的 `\\clip` 链每格只留扫过它的那几步。底板用专门的样式 `P.fo-fx`（无描边、无阴影、左上对齐），省掉逐条重复的 tag。
5. 正文：底板中位色亮（L > 62）就写深色字，否则白字；描边换成底板色（等于看不见）。人在行首写的颜色 tag 在后面，照旧压过它；
   人改过主色的样式（调用方给 `keep`）整个不动：颜色是人定的，自动色不盖。

哪些行是底板、哪些是正文由调用方按元素种类给（`kinds`），不按长相认：扩展效果画的框、区域号也是绘图或字，不碰。

取样按 `FPS`、在每行头尾各 `EDGE_SKIP` 秒之外取；剩下的不到一个采样间隔（很短的行）就取中点那一帧（`window_of`）。
一个样本都没取到的底板（在视频结尾之后的、`window` 模式下窗口外的）保留原来的近黑底板。
底板的颜色只取决于视频和底板几何，和译文无关；颜色在 Lab 里算（OpenCV 8 位 Lab 换成真 Lab），ΔE 用欧氏距离。
依据和代价见 docs/architecture/defaults.md 的底板取色一节。
"""
from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
from dataclasses import dataclass, field, replace

import cv2
import numpy as np

from flowocr.typeset import assfile

VERSION = "h3"
"""算法版本，写进 `[Script Info]` 的 `FlowOCR Typeset`。"""

FPS = 5
EDGE_SKIP = 0.15
MARGIN_PX, MARGIN_GROW, MARGIN_MAX = 12, 0.1, 48
INP_SCALE = 3
INK_THR = 24
INK_DIL = 7
CELL_W = 24
MERGE_TOL = 4.0
KEY_TOL = 8.0
OVERLAP, BLUR = 3, 3
CLIP_PAD = 16
"""裁 `\\clip` 链时格子两边各多算这么多 px（`\\blur` 的光晕伸出格子）。"""
DARK_L = 62
"""底板中位色的 L 超过它就写深色字。"""
HWACCEL = "cuda"
"""解码先试 NVDEC：`fps` 在显存里丢帧、缩好再下传，ffmpeg 的 CPU 约省 7 倍（hsr 1080p60 h264 上 30 s：16.0 → 2.2 s）。
开不了（没有 N 卡、ffmpeg 没带、编码不支持）就整趟软解；试解过了、中途某段解码失败的照常报错（不写半成品）。NVDEC 下传后转成 yuv420p 再出 BGR（和软解同一条转色路径），
画面尺寸和字幕稿相同时两条路逐字节相同；直接从 nv12 出 BGR 会差约 1 级（hsr 上均值 0.9 / 255）。"""
PREFETCH = 4
"""解码在后台线程里预读这么多帧：ffmpeg 解码、转色和取样并行（不预读时 NVDEC 那条路两边几乎串行，墙钟约等于两边 CPU 之和）。"""
STYLE = "P"
"""底板样式名的词干（加上派生后缀 `.fo-fx`，`restore` 照删；和源样式的派生名撞了就加序号，由调用方挑）。"""

POS = re.compile(r"\\pos\(([-\d.]+),([-\d.]+)\)")
MOVE = re.compile(r"\\move\(([-\d.]+),([-\d.]+),([-\d.]+),([-\d.]+)(?:,(\d+),(\d+))?\)")
DRAW = re.compile(r"\}m 0 0 l ([\d.]+) 0 ([\d.]+) ([\d.]+) 0 ([\d.]+)\{\\p0\}$")
BACK = re.compile(r"\\1c&H[0-9A-Fa-f]+&\\1a&H[0-9A-Fa-f]+&")
CLIP_STEP = re.compile(r"\\t\((\d+),(\d+),\\clip\((-?\d+),(-?\d+),(-?\d+),(-?\d+)\)\)")
CLIP_INIT = re.compile(r"\\clip\((-?\d+),(-?\d+),(-?\d+),(-?\d+)\)")
TIME = re.compile(r"(\d+):(\d\d):(\d\d)\.(\d\d)")


# ---- 行 ----

def t2s(s: str) -> float:
    h, m, sec, cs = map(int, TIME.match(s).groups())
    return h * 3600 + m * 60 + sec + cs / 100


@dataclass
class Ev:
    """一条生成行（`Dialogue:`，Effect `fo-fx`，底板是 `fo-fx|plate`），字段照 `core.dialogue` 的顺序。换小格时 Effect 原样带过去。"""
    fields: list[str]           # Layer … Effect, Text 共 10 个
    start: float
    end: float

    @property
    def text(self) -> str:
        return self.fields[9]

    def rect(self) -> tuple[float, float, float, float]:
        m = DRAW.search(self.text)
        w, h = float(m.group(1)), float(m.group(3))
        p = POS.search(self.text) or MOVE.search(self.text)
        x, y = float(p.group(1)), float(p.group(2))
        return (x, y, x + w, y + h)

    def rect_at(self, t: float) -> tuple[float, float, float, float]:
        """t 秒时的矩形：`\\move` 按它的时段线性插值。"""
        r = self.rect()
        mv = MOVE.search(self.text)
        if not mv:
            return r
        x1, y1, x2, y2 = (float(mv.group(i)) for i in range(1, 5))
        t1 = float(mv.group(5)) / 1000 if mv.group(5) else 0.0
        t2 = float(mv.group(6)) / 1000 if mv.group(6) else self.end - self.start
        u = min(1.0, max(0.0, (t - self.start - t1) / max(1e-3, t2 - t1)))
        dx, dy = x1 + (x2 - x1) * u - r[0], y1 + (y2 - y1) * u - r[1]
        return (r[0] + dx, r[1] + dy, r[2] + dx, r[3] + dy)

    def line(self, text: str | None = None, style: str | None = None) -> str:
        f = list(self.fields)
        if text is not None:
            f[9] = text
        if style is not None:
            f[3] = style
        return "Dialogue: " + ",".join(f)


def parse(line: str) -> Ev | None:
    if not line.startswith("Dialogue:"):
        return None
    f = line.split(": ", 1)[1].split(",", 9)
    if len(f) != 10:
        return None
    return Ev(f, t2s(f[1]), t2s(f[2]))


@dataclass
class Block:
    """一条源行生成的行：底板和正文（行号是在 `lines` 里的下标）。"""
    plates: list[tuple[int, Ev]]
    texts: list[tuple[int, Ev]]
    inner: dict = field(default_factory=dict)      # 底板序号 -> [(时刻, 各小格的 Lab，(ny, nx, 3))]
    band: dict = field(default_factory=dict)       # 底板序号 -> 当前取样带宽（加宽后不退回）


# ---- 颜色 ----

def bgr_to_lab(px: np.ndarray) -> np.ndarray:
    """uint8 BGR (N,3) -> 真 Lab float (N,3)。"""
    lab = cv2.cvtColor(px.reshape(-1, 1, 3), cv2.COLOR_BGR2LAB).reshape(-1, 3).astype(np.float32)
    lab[:, 0] *= 100 / 255
    lab[:, 1:] -= 128
    return lab


def lab_to_hex(lab: np.ndarray) -> str:
    """真 Lab -> ASS 的 BBGGRR。"""
    l8 = np.array([[[lab[0] * 255 / 100, lab[1] + 128, lab[2] + 128]]], np.float32).clip(0, 255).astype(np.uint8)
    b, g, r = cv2.cvtColor(l8, cv2.COLOR_LAB2BGR)[0, 0]
    return "%02X%02X%02X" % (b, g, r)


# ---- 取样 ----

def frames(video: str, s: float, e: float, w: int, h: int, hw: bool = False, quiet: bool = False):
    """[s, e) 里按 `FPS` 解码、缩到 `w`×`h`，逐帧给 (时刻, BGR)（后台预读 `PREFETCH` 帧）。`-seek_timestamp`：`-ss` 按文件里的时间戳算。
    `hw`：走 NVDEC（`HWACCEL`）；`quiet`：ffmpeg 的报错不打出来（试解用）。"""
    vf = f"fps={FPS},scale_cuda={w}:{h},hwdownload,format=nv12,format=yuv420p" if hw else f"fps={FPS},scale={w}:{h}"
    p = subprocess.Popen(["ffmpeg", "-v", "error", *(["-hwaccel", HWACCEL, "-hwaccel_output_format", HWACCEL] if hw else []),
                          "-seek_timestamp", "1", "-ss", "%.3f" % s, "-t", "%.3f" % (e - s), "-i", video,
                          "-vf", vf, "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL if quiet else None)
    n = w * h * 3
    q: queue.Queue = queue.Queue(PREFETCH)

    def read():
        while len(buf := p.stdout.read(n)) == n:
            q.put(buf)
        q.put(None)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    k = 0
    try:
        while (buf := q.get()) is not None:
            yield s + k / FPS, np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            k += 1
    finally:
        if reader.is_alive():                             # 调用方没读完就走（报错）：停掉 ffmpeg，读线程跟着结束
            p.kill()
            while reader.is_alive():
                try:
                    q.get(timeout=0.1)
                except queue.Empty:
                    pass
    if p.wait() != 0 or k < (e - s) * FPS * 0.9 - 1:      # 帧数允许差一帧左右的取整
        raise RuntimeError(f"底板取色的解码只得到 {k} 帧（应约 {(e - s) * FPS:.0f}）：{video} {s:.3f}-{e:.3f}")


def duration(video: str) -> float:
    """视频画面的时长（秒）。先看视频流（音轨比画面长时容器时长会算上音轨尾巴）：流的 duration，mkv 不记它就看流标签
    DURATION（`时:分:秒`）；都没有用容器的，再没有当成无限长。起点不为 0 的片子各容器口径不一（mkv 记的是结束时刻），没验。"""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=duration:stream_tags=DURATION:format=duration", "-of", "json", video],
                         capture_output=True, text=True).stdout
    try:
        info = json.loads(out)
    except ValueError:
        return float("inf")
    st = (info.get("streams") or [{}])[0]
    tag = (st.get("tags") or {}).get("DURATION", "").split(":")
    for dur in (lambda: st["duration"], lambda: sum(float(x) * 60 ** i for i, x in enumerate(reversed(tag))),
                lambda: info["format"]["duration"]):
        try:
            return float(dur())
        except (KeyError, TypeError, ValueError):
            continue
    return float("inf")


def hw_works(video: str, s: float, w: int, h: int) -> bool:
    """NVDEC 在这个视频上开不开得了：从 s 起试解两帧。"""
    try:
        return sum(1 for _ in frames(video, s, s + 2 / FPS, w, h, hw=True, quiet=True)) > 0
    except (RuntimeError, OSError):
        return False


def band_mask(img: np.ndarray, rect, others: list, m: int):
    """取样范围（底板外扩 m px）和它的掩码：笔画膨胀后，加上别的底板。返回 (左上角 X0, Y0, 范围内的画面, 掩码, 底板内部的布尔图)。"""
    H, W = img.shape[:2]
    x0, y0, x1, y1 = (int(round(v)) for v in rect)
    X0, Y0, X1, Y1 = max(0, x0 - m), max(0, y0 - m), min(W, x1 + m), min(H, y1 + m)
    reg = img[Y0:Y1, X0:X1]
    gray = cv2.cvtColor(reg, cv2.COLOR_BGR2GRAY)
    k = max(7, int(round((y1 - y0) * 0.6)) | 1)
    ker = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
    ink = ((cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, ker) > INK_THR)
           | (cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, ker) > INK_THR))
    inside = np.zeros_like(ink)
    inside[max(0, y0 - Y0):max(0, y1 - Y0), max(0, x0 - X0):max(0, x1 - X0)] = True
    mask = cv2.dilate(ink.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (INK_DIL, INK_DIL)))
    for o in others:
        ox0, oy0, ox1, oy1 = (int(round(v)) for v in o)
        mask[max(0, oy0 - Y0 - 3):max(0, oy1 - Y0 + 3), max(0, ox0 - X0 - 3):max(0, ox1 - X0 + 3)] = 1
    return X0, Y0, reg, mask, inside


def inner_bg(img: np.ndarray, rect, others: list, m0: int = 0) -> tuple[np.ndarray, int]:
    """底板范围的背景估计（1/4 分辨率 BGR）和这次用的取样带宽。m0：从这个宽度起步（加宽过就不退回）。"""
    x0, y0, x1, y1 = (int(round(v)) for v in rect)
    m = max(MARGIN_PX, m0)
    while True:
        X0, Y0, reg, mask, inside = band_mask(img, rect, others, m)
        share = float(((mask == 0) & ~inside).sum() / max(1, (~inside).sum()))
        if m >= MARGIN_MAX or share >= MARGIN_GROW:
            break
        m *= 2
    hw, hh = max(1, reg.shape[1] // INP_SCALE), max(1, reg.shape[0] // INP_SCALE)
    reg2 = cv2.resize(reg, (hw, hh), interpolation=cv2.INTER_AREA)
    mask2 = (cv2.resize(mask * 255, (hw, hh), interpolation=cv2.INTER_AREA) > 0).astype(np.uint8)
    inp = cv2.inpaint(reg2, mask2, max(2, round(10 / INP_SCALE)), cv2.INPAINT_TELEA)
    sx, sy = hw / reg.shape[1], hh / reg.shape[0]
    cy0, cx0 = max(0, int((y0 - Y0) * sy)), max(0, int((x0 - X0) * sx))     # 底板越出画面时只取画面里那部分
    crop = inp[cy0:max(cy0 + 1, int((y1 - Y0) * sy)), cx0:max(cx0 + 1, int((x1 - X0) * sx))]
    return cv2.resize(crop, (max(1, (x1 - x0) // 4), max(1, (y1 - y0) // 4)), interpolation=cv2.INTER_AREA), m


def same_rect(a, b) -> bool:
    return all(abs(u - v) <= 2 for u, v in zip(a, b))


def window_of(p: Ev, end: float = float("inf")) -> tuple[float, float] | None:
    """一块底板取样的时段 [a, z)（`end` 是视频时长）。头尾各去 `EDGE_SKIP`（行首行尾可能正在淡入淡出、换场）；
    剩下的不到一个采样间隔（很短的行）就取中点前后各半个间隔：正好落进一个取样帧，是离中点最近的那帧，最多差半个间隔
    （视频结尾前的退到最后一个取样帧，可能差近一个间隔），可能在行的显示区间外（取色前的近黑底板在很短的行上是一闪而过的黑块，差 0.1 s 的背景比它好）。
    伸到视频结尾之后的窗口往前挪到结尾之前；整行在结尾之后（没有画面可取）给 None。"""
    if p.start >= end:
        return None
    a, z = p.start + EDGE_SKIP, p.end - EDGE_SKIP
    if z - a >= 1 / FPS and z <= end:
        return a, z
    if z - a < 1 / FPS:
        m = (p.start + p.end) / 2
        a, z = m - 0.5 / FPS, m + 0.5 / FPS
    if z > end:
        a, z = min(a, end - 1 / FPS), end
    return a - 1e-6, z - 1e-6            # 两端一起挪：半开区间宽度不变，浮点误差不会让正好落在端点上的帧两头都算或都不算


def grid(p: Ev) -> tuple[int, int]:
    """底板切成几行几列小格：`CELL_W` px 宽，半行高起步（矮于 24 px 的不切行）。"""
    px0, py0, px1, py1 = p.rect()
    return max(1, round((py1 - py0) / max(12, (py1 - py0) / 2))), max(1, round((px1 - px0) / CELL_W))


class Sweep:
    """按时刻扫一串带区间 [a, z) 的东西：`at(t)` 给 t 时活着的（t 只增不减）。省掉每帧把全部底板扫一遍。"""
    def __init__(self, items: list[tuple[tuple[float, float], object]]):
        self.items = sorted(items, key=lambda x: x[0][0])
        self.j, self.on = 0, []

    def at(self, t: float) -> list:
        while self.j < len(self.items) and self.items[self.j][0][0] <= t:
            self.on.append(self.items[self.j])
            self.j += 1
        self.on = [x for x in self.on if x[0][1] > t]
        return [x[1] for x in self.on]


def sample(targets: list[tuple[Block, int, Ev, int]], pool: list[tuple[int, Ev]], video: str, s: float, e: float,
           w: int, h: int, hw: bool, end: float) -> None:
    """[s, e) 里逐帧给活着的底板取背景，当场缩成小格的 Lab 存下。targets：(块, 块里的底板序号, 底板, 源行号)；
    pool：(源行号, 底板)，同一时刻别的源行的底板从这里找（同一源行的相邻行不算：它们本来就挨着）。"""
    live = Sweep([(wd, x) for x in targets if (wd := window_of(x[2], end)) and wd[1] > s and wd[0] < e])
    near = Sweep([((q.start, q.end), (g, q)) for g, q in pool if q.end > s and q.start < e])
    for t, img in frames(video, s, e, w, h, hw):
        now, on = live.at(t), near.at(t)
        if not now:
            continue
        on = [(g, q.rect_at(t)) for g, q in on]
        for b, i, p, g in now:
            r = p.rect_at(t)
            others = [o for og, o in on if og != g and not same_rect(o, r)]
            bg, b.band[i] = inner_bg(img, r, others, b.band.get(i, 0))
            ny, nx = grid(p)
            cells = bgr_to_lab(cv2.resize(bg, (nx, ny), interpolation=cv2.INTER_AREA).reshape(-1, 3).copy())
            b.inner.setdefault(i, []).append((t, cells.reshape(ny, nx, 3)))


def spans_of(plates: list[Ev], end: float = float("inf"), gap: float = 2.0) -> list[tuple[float, float]]:
    """要解码的时段：每块底板的取样区间（`window_of`），按 `FPS` 的网格对齐，间隔不到 `gap` 秒的并成一段，夹到视频时长 `end`。"""
    iv = sorted(wd for wd in (window_of(p, end) for p in plates) if wd)
    out: list[list[float]] = []
    for a, z in iv:
        a = max(0.0, np.floor(a * FPS) / FPS)      # 起点不能是负的：`-ss` 负值时帧的时刻会整体错一格
        if out and a <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], z)
        else:
            out.append([a, z])
    return [(a, min(end, z + 1 / FPS)) for a, z in out]


# ---- 拟合与写出 ----

def smooth(series: np.ndarray) -> np.ndarray:
    """沿时间 3 点中位滤波，压掉粒子 / 闪烁。"""
    if len(series) < 3:
        return series
    pad = np.concatenate([series[:1], series, series[-1:]])
    return np.median(np.stack([pad[:-2], pad[1:-1], pad[2:]]), axis=0)


def rdp(ts: np.ndarray, xs: np.ndarray, tol: float) -> list[int]:
    """时间序列（Lab）的折线简化：留下偏离直线超过 tol 的拐点下标。"""
    keep = {0, len(ts) - 1}
    stack = [(0, len(ts) - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        u = (ts[a + 1:b] - ts[a]) / max(1e-6, ts[b] - ts[a])
        line = xs[a] + u[:, None] * (xs[b] - xs[a])
        d = np.linalg.norm(xs[a + 1:b] - line, axis=1)
        i = int(d.argmax())
        if d[i] > tol:
            k = a + 1 + i
            keep.add(k)
            stack += [(a, k), (k, b)]
    return sorted(keep)


def merge_cells(cells: np.ndarray, tol: float) -> list[tuple[int, int, int, int]]:
    """贪心合并小格（先向右、再向下长）：块里每格在每个时刻离块均色都不超过 tol（ΔE）。返回 (iy0, ix0, iy1, ix1)，右开。"""
    T, ny, nx, _ = cells.shape
    used = np.zeros((ny, nx), bool)

    def ok(y0, x0, y1, x1):
        blk = cells[:, y0:y1, x0:x1].reshape(T, -1, 3)
        return float(np.linalg.norm(blk - blk.mean(1, keepdims=True), axis=2).max()) <= tol

    rects = []
    for y in range(ny):
        for x in range(nx):
            if used[y, x]:
                continue
            x1 = x + 1
            while x1 < nx and not used[y, x1] and ok(y, x, y + 1, x1 + 1):
                x1 += 1
            y1 = y + 1
            while y1 < ny and not used[y1, x:x1].any() and ok(y, x, y1 + 1, x1):
                y1 += 1
            used[y:y1, x:x1] = True
            rects.append((y, x, y1, x1))
    return rects


def keys_of(ts: list, xs: np.ndarray) -> list[tuple[float, np.ndarray]]:
    ts = np.asarray(ts)
    xs = smooth(np.asarray(xs, np.float32))
    return [(float(ts[i]), xs[i]) for i in rdp(ts, xs, KEY_TOL)]


def color_tags(keys: list[tuple[float, np.ndarray]], ev_start: float, ev_end: float) -> str:
    """关键帧 -> `\\1c` 起始色 + 一串 `\\t` 线性过渡（时间相对事件起点，毫秒）。目标色和当前色相同的过渡不写（什么都不做），
    后面那段照样从它的时刻起。"""
    ts = np.array([k[0] for k in keys])
    xs = np.stack([k[1] for k in keys])
    cur = lab_to_hex(np.array([np.interp(ev_start, ts, xs[:, c]) for c in range(3)]))
    tags = "\\1c&H%s&" % cur
    prev = ev_start
    for t, x in keys:
        if t <= ev_start + 0.05 or prev >= ev_end:
            continue
        hx = lab_to_hex(x)
        if hx != cur:
            tags += "\\t(%d,%d,\\1c&H%s&)" % ((prev - ev_start) * 1000, (t - ev_start) * 1000, hx)
        cur, prev = hx, t
    return tags


def trim_clip(text: str, x0: float, x1: float) -> str:
    """打字机的 `\\clip` 链：起始 `\\clip(左,上,右,下)`（或全藏的 0,0,0,0），之后每个字一步 `\\t(t0,t1,\\clip(左,上,右,下))`，
    左、上、下不变，右边沿只增不减。裁到 [x0, x1] 这一格用得着的：右边沿落在格内的步和第一步盖满整格的；还没扫到这一格的步
    并进起始矩形，之后的步删掉。起始就盖满整格的，整条链都删。不是这种连续、单调的链就原样返回。"""
    steps = list(CLIP_STEP.finditer(text))
    init = CLIP_INIT.search(text)
    if not steps or not init or init.end() != steps[0].start():
        return text
    if text[steps[0].start():steps[-1].end()] != "".join(m.group(0) for m in steps):
        return text                           # 链中间夹着别的 tag
    end = steps[-1].end()
    rights = [int(m.group(5)) for m in steps]
    r0 = int(init.group(3))
    if any(b < a for a, b in zip([r0] + rights, rights)) or len({(m.group(3), m.group(4), m.group(6)) for m in steps}) > 1:
        return text
    hidden = init.group(0) == "\\clip(0,0,0,0)"
    if not hidden and (init.group(1), init.group(2), init.group(4)) != (steps[0].group(3), steps[0].group(4), steps[0].group(6)):
        return text
    keep, start = [], init.group(0)
    if not hidden and r0 >= x1:
        steps = []                            # 起始就盖满整格
    for m, r in zip(steps, rights):
        if r <= x0:
            # 还没扫到这一格：这一格照旧藏着。起始换成这一步的矩形，下一步若是一段过渡就从它插值（和原链一样）
            start = "\\clip(%s,%s,%s,%s)" % m.group(3, 4, 5, 6)
            continue
        keep.append(m.group(0))
        if r >= x1:
            break                             # 这一步之后整格可见，后面的步没作用
    return text[:init.start()] + start + "".join(keep) + text[end:]


def cell_text(p: Ev, x0: float, y0: float, x1: float, y1: float, color: str, own_style: bool = True) -> str:
    """一格：底板行换成新矩形和颜色，去掉行尾的 `{\\p0}`；`own_style`（小格用底板样式）时再去掉样式已有的 `\\an7` `\\bord0` `\\shad0`。
    `\\clip` 是屏幕坐标、原样保留（再按格裁剪），`\\move` 两端一起平移。"""
    r = p.rect()
    dx, dy = x0 - r[0], y0 - r[1]
    t = POS.sub("\\\\pos(%d,%d)" % (x0, y0), p.text, count=1)
    t = MOVE.sub(lambda m: "\\move(%d,%d,%d,%d%s)" % (float(m.group(1)) + dx, float(m.group(2)) + dy,
                                                     float(m.group(3)) + dx, float(m.group(4)) + dy,
                                                     ",%s,%s" % (m.group(5), m.group(6)) if m.group(5) else ""), t, count=1)
    t = BACK.sub(lambda m: color + "\\blur%g" % BLUR, t, count=1)
    t = DRAW.sub("}m 0 0 l %d 0 %d %d 0 %d" % (x1 - x0, x1 - x0, y1 - y0, y1 - y0), t)
    for tag in ("\\an7", "\\bord0", "\\shad0") if own_style else ():
        t = t.replace(tag, "", 1)
    if "\\move" not in t:
        t = trim_clip(t, x0 - CLIP_PAD, x1 + CLIP_PAD)
    return t


def cells_of(p: Ev, got: list, own_style: bool = True) -> tuple[list[str], np.ndarray]:
    """一块底板的取样 -> 各格的行文本，和整块的 Lab 中位色。"""
    ts = [g[0] for g in got]
    px0, py0, px1, py1 = p.rect()
    ny, nx = grid(p)
    cells = np.stack([g[1] for g in got])          # (T, ny, nx, 3)
    med = np.median(cells.reshape(-1, 3), axis=0)
    edge = max(2, OVERLAP // 2)
    cells = smooth(cells)
    out = []
    for iy0, ix0, iy1, ix1 in merge_cells(cells, MERGE_TOL):
        cx0, cx1 = px0 + (px1 - px0) * ix0 / nx, px0 + (px1 - px0) * ix1 / nx
        cy0, cy1 = py0 + (py1 - py0) * iy0 / ny, py0 + (py1 - py0) * iy1 / ny
        keys = keys_of(ts, cells[:, iy0:iy1, ix0:ix1].mean((1, 2)))
        out.append(cell_text(p, cx0 - (edge if ix0 == 0 else OVERLAP), cy0 - (edge if iy0 == 0 else OVERLAP),
                             cx1 + (edge if ix1 == nx else OVERLAP), cy1 + (edge if iy1 == ny else OVERLAP),
                             color_tags(keys, p.start, p.end), own_style))
    return out, med


def recolor_text(text: str, med: np.ndarray) -> str:
    """字色按底板亮度反选深 / 白，描边换成底板色。加在行首 tag 块的最前面：人写的颜色在后面，照旧压过它。
    行内回到样式色的空 `\\1c`（剧本样式的闭标签）改成这一行实际的字色；行内有值的颜色（剧本的、人写的）原样不动（owner 2026-10-23）。"""
    tags = "\\1c&H%s&\\3c&H%s&" % ("1A1A1A" if med[0] > DARK_L else "FFFFFF", lab_to_hex(med))
    i = text.find("{")
    text = text[:i + 1] + tags + text[i + 1:] if i == 0 else "{" + tags + "}" + text
    return assfile.recolor_inline(text, assfile.head_color(text))


def plate_style(r: assfile.Row, name: str) -> str:
    """底板样式 `name`：照一条源样式改名、去描边阴影、左上对齐、填充不透明（颜色由每格自己给）。"""
    d = replace(r, fields=list(r.fields))
    for k, v in (("Name", name), ("PrimaryColour", "&H00FFFFFF"), ("BorderStyle", "1"), ("Outline", "0"),
                 ("Shadow", "0"), ("Alignment", "7")):
        d.set(k, v)
    return str(d)


@dataclass
class Result:
    lines: list[str]
    blocks: int = 0                 # 有底板的源行
    plates: int = 0                 # 底板行
    sampled: int = 0                # 取到色、换成小格的底板行
    cells: int = 0                  # 写出的小格行
    grown: int = 0                  # 取样带加宽过的底板
    hw: bool = False                # 解码走了 NVDEC


def blocks_of(lines: list[str], kinds: list[str | None]) -> list[Block]:
    """`kinds[k]` 是 `lines[k]` 的种类：`"src"` 是一条处理过的源行，开一块；它后面的生成行里 `"plate"`（底板）、`"text"`（正文）
    收进这一块，别的（区域号、扩展效果的元素、不是生成行的 None）不碰。"""
    blocks: list[Block] = []
    cur: Block | None = None
    for k, (ln, kind) in enumerate(zip(lines, kinds)):
        if kind == "src":
            cur = Block([], [])
            blocks.append(cur)
        elif cur is not None and (kind == "text" or kind == "plate" and DRAW.search(ln)):     # 扩展效果自称底板、形状却不是矩形的不碰
            (cur.plates if kind == "plate" else cur.texts).append((k, parse(ln)))
    return blocks


def apply(lines: list[str], kinds: list[str | None], video: str, w: int, h: int,
          window: tuple[float, float] | None = None, hw: bool | None = None, style: str | None = STYLE + ".fo-fx",
          keep: set[str] | frozenset = frozenset()) -> Result:
    """`lines`：阶段 4 生成完的 `[Events]` 行，`kinds` 是逐行的种类（见 `blocks_of`）。返回换过底板和正文的行。
    `window`：只处理和 [s, e) 有交集的源行、只在 [s - 0.3, e + 0.3) 里取样（预览只出一段时用；原型对拍也用它）；
    不给就处理全部，按底板的时段解码。`hw`：解码走不走 NVDEC，None 是先试解、开得了就走。
    `style`：小格用的样式名（调用方避开已占用的名字）；None 是没有样式可派生，小格留在原样式、自带 `\\an7\\bord0\\shad0`。
    `keep`：这些样式的正文不改字色、描边（颜色是人定的）。"""
    blocks = blocks_of(lines, kinds)
    pool = [(g, p) for g, b in enumerate(blocks) for _, p in b.plates]
    if window:
        s, e = window
        for b in blocks:
            b.plates = [x for x in b.plates if x[1].end > s and x[1].start < e]
            b.texts = [x for x in b.texts if x[1].end > s and x[1].start < e]
        s, e = s - 0.3, e + 0.3
        pool = [(g, p) for g, p in pool if p.end > s and p.start < e]
    live = [(g, b) for g, b in enumerate(blocks) if b.plates]
    targets = [(b, i, p, g) for g, b in live for i, (_, p) in enumerate(b.plates)]
    end = duration(video) if targets else 0.0             # 解码夹到视频里：视频结尾之后的行没有画面可取
    spans = [(max(0.0, window[0] - 0.3), min(end, window[1] + 0.3))] if window else spans_of([x[2] for x in targets], end)
    spans = [(s, e) for s, e in spans if s < e]
    if hw is None:
        hw = bool(spans) and hw_works(video, spans[0][0], w, h)
    for s, e in spans:
        sample(targets, pool, video, s, e, w, h, hw, end)
    blocks = [b for _, b in live]
    res = Result(list(lines), blocks=len(blocks), plates=len(targets), hw=hw)
    for b in blocks:
        outs, meds = {}, []
        for i, (k, p) in enumerate(b.plates):
            got = b.inner.get(i)
            if not got:
                continue
            outs[k], med = cells_of(p, got, style is not None)
            meds.append(med)
            res.sampled += 1
            res.cells += len(outs[k])
            res.grown += b.band.get(i, 0) > MARGIN_PX
        for k, texts in outs.items():
            p = parse(res.lines[k])
            res.lines[k] = "\n".join(p.line(t, style) for t in texts)
        if meds:
            med = np.median(np.stack(meds), axis=0)
            for k, t in b.texts:
                if t.fields[3] not in keep:
                    res.lines[k] = t.line(recolor_text(t.text, med))
    res.lines = [x for ln in res.lines for x in ln.split("\n")]
    return res
