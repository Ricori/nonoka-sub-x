"""名牌的字色（owner 2026-11-05）：匹配器认上的名牌照这款游戏的名牌色画，长得不一样的特例（原神「グライアイの眼」的蓝底白字面板）不上色。

只看匹配器认上的真名牌——名牌层里组是 `name`（挂在认上的台词上，`scriptmatch.name_roles`）、有译名的块。同一种名牌（层 + 译名 + 位置，`same_spot`）一起判：
1. 在这种名牌的块里按时间均匀挑 `FIRST` 块，每块取中点那一帧，看原文是不是这款游戏这一层的名牌色（`INK`）写的（`ink_share`）；
2. 挑的几块都是或都不是，就按它定；不一致时再挑到 `MORE` 块（只量新挑的），是的占比不低于 `SHARE` 才上色。
定下来不上色的那种名牌，每块记 `ink: False`（字幕稿里另用 `PlainName` 样式，白字），上色的记 `ink: True`；没判的（别的组、没译名、
这款游戏这一层没有名牌色）不记。判过的产物另在 `overlay.name_ink` 记这款游戏各层的名牌色（`#rrggbb`），阶段 3 写进 `Name` / `Title` 样式。

只按游戏给名牌色（可以按游戏特化）：没有这款游戏的（只有 tracks、文本库里没有的游戏、naninovel）不上色；绝区零的名牌是白字，和不上色一样，也不判。
依据和代价见 docs/architecture/defaults.md 的名牌字色一节。
"""
from __future__ import annotations

import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from flowocr import provenance

INK: dict[str, dict[str, tuple[float, float, float]]] = {
    "genshin": {"name": (81.1, 2.5, 78.2), "title": (69.6, 0.4, 61.2)},
    "starrail": {"name": (75.3, 0.5, 18.1)},
}
"""游戏 → 名牌层（`name` / `title`）→ 名牌色（CIE Lab）。2026-11-05 在原神、星铁 5 段切片的名牌上量的中位数（取笔画最亮的核心，白顶帽）：
原神名字（金）32 条、称号（暗金，和名字差 ΔE 18–23，要单列）9 条，星铁名字（米金）12 条；单条离中位数最远 7.9 / 3.0 / 6.4。"""
PIX = 15.0
"""像素的颜色离名牌色不超过这么多（ΔE，Lab 里的欧氏距离）算"名牌色的笔画"。"""
MIN_SHARE = 0.01
"""名牌框里"名牌色的笔画"的像素占比，比框外一圈多出不到这么多，这一块就不算名牌色写的（`ink_share`）。读数见 docs/architecture/defaults.md 的名牌字色一节：
真名牌、称号除了整屏压暗的都在这以上，特例、名牌层里的称号（拿名字色比）、被误标成名牌的界面字大多在这以下——星铁和名牌同色的 `R2` 按钮、
任务提示判不出来，靠 `scriptmatch.name_roles` 不让它们进 `name` 组。"""
DIM = 0.5
"""框里最亮的笔画（L 的 99 分位）不到名牌色 L 的这么多倍，就是整屏压暗了（弹窗把画面盖暗，gi1「拉马奈」有两块：笔画 L 22–23，真名牌的块都在 67 以上），
这一块量不出字色、不算证据（`ink_share` 是 None）；不这样，块少的那种名牌碰上一块就整种不上色。"""
FIRST, MORE, SHARE = 3, 9, 0.8
"""每种名牌先量 `FIRST` 块；不一致时加到 `MORE` 块，是名牌色的占比 ≥ `SHARE` 才上色（owner 2026-11-05：不一致时多抽几块、一致率低于门槛就不上色）。"""
SPOT_DY = 2.0
"""同一个名字的两块名牌，上沿差不超过这么多个字高算同一种界面的名牌位置（`same_spot`）。"""
WORKERS = 4
"""同时开几个 ffmpeg 取帧（每帧一个进程、从关键帧解到那一刻，0.13–0.23 s）。"""


def to_lab(rgb: np.ndarray) -> np.ndarray:
    """sRGB uint8 (..., 3) → CIE Lab（D65）。用浮点公式，不用 OpenCV 的 8 位 Lab（名牌色就是这样量的）。"""
    c = rgb.astype(np.float64) / 255.0
    c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    m = np.array([[0.4124564, 0.3575761, 0.1804375], [0.2126729, 0.7151522, 0.0721750], [0.0193339, 0.1191920, 0.9503041]])
    xyz = c @ m.T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 216 / 24389, np.cbrt(xyz), (24389 / 27 * xyz + 16) / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1)


def to_hex(lab: tuple[float, float, float]) -> str:
    """CIE Lab → `#rrggbb`（`to_lab` 的反变换，出界的分量夹到 0–255）。"""
    fy = (lab[0] + 16) / 116
    f = np.array([fy + lab[1] / 500, fy, fy - lab[2] / 200])
    xyz = np.where(f ** 3 > 216 / 24389, f ** 3, (116 * f - 16) / (24389 / 27)) * np.array([0.95047, 1.0, 1.08883])
    m = np.array([[3.2404542, -1.5371385, -0.4985314], [-0.9692660, 1.8760108, 0.0415560], [0.0556434, -0.2040259, 1.0572252]])
    c = np.clip(m @ xyz, 0, 1)
    c = np.where(c <= 0.0031308, 12.92 * c, 1.055 * c ** (1 / 2.4) - 0.055)
    return "#%02x%02x%02x" % tuple(int(round(v * 255)) for v in c)


def ink_share(rgb: np.ndarray, box: list[float], target: tuple[float, float, float]) -> float | None:
    """一帧（sRGB，和 `box` 同一坐标）里框中的字是不是 `target` 这个颜色写的：框里离它不超过 `PIX` 的像素占比，减去框外一圈
    （外扩半个框高）的占比；画面整个压暗了（`DIM`）、框在画面外是 None（没有证据）。不量"字是什么颜色"、只问"有没有这个颜色的笔画"：
    名牌压在亮的、花的背景上时（粉色天空、草地），取笔画最亮核心的量法会量成背景（gi1 整场 40 种里 5 种真名牌这样判错）；
    减去四周是防背景本身就是这个颜色。"""
    H, W = rgb.shape[:2]
    x0, y0, x1, y1 = (int(v) for v in box)
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
    if y1 <= y0 or x1 <= x0:
        return None
    p = max(4, (y1 - y0) // 2)
    X0, Y0, X1, Y1 = max(0, x0 - p), max(0, y0 - p), min(W, x1 + p), min(H, y1 + p)
    lab = to_lab(rgb[Y0:Y1, X0:X1])
    inner = np.zeros(lab.shape[:2], bool)
    inner[y0 - Y0:y1 - Y0, x0 - X0:x1 - X0] = True
    if np.percentile(lab[..., 0][inner], 99) < DIM * target[0]:
        return None
    near = np.linalg.norm(lab - np.array(target), axis=-1) <= PIX
    return float(near[inner].mean() - (near[~inner].mean() if (~inner).any() else 0.0))


def grab(video: str, t_us: int, w: int, h: int) -> np.ndarray | None:
    """视频 `t_us` 那一刻的一帧，缩到 `w`×`h`（产物的坐标），sRGB uint8；取不到（视频结尾之后、解码失败）是 None。
    `-seek_timestamp`：`-ss` 按文件里的时间戳算，和阶段 4 底板取色同一口径。"""
    p = subprocess.run(["ffmpeg", "-v", "error", "-seek_timestamp", "1", "-ss", "%.3f" % (t_us / 1e6), "-i", video,
                        "-frames:v", "1", "-vf", f"scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                       capture_output=True)
    if p.returncode != 0 or len(p.stdout) != w * h * 3:
        return None
    return np.frombuffer(p.stdout, np.uint8).reshape(h, w, 3)


def spread(n: int, k: int) -> list[int]:
    """`n` 块里按时间均匀挑 `k` 块的下标（含首尾；`k` 是 1 时挑中间那块）；不到 `k` 块就全挑。"""
    if n <= k:
        return list(range(n))
    if k == 1:
        return [n // 2]
    return sorted({round(i * (n - 1) / (k - 1)) for i in range(k)})


def same_spot(a: list[float], b: list[float]) -> bool:
    """两个名牌框在同一种界面的名牌位置：中心横向差不超过一个字高、上沿差不超过 `SPOT_DY` 个字高（字高取小的那个）。
    比"同一块名牌"（`tracksio.same_plate`，上沿差半个字高）松：对话框里的名牌随台词行数上下挪 1.5 个字高左右（gi1「派蒙」、gi2「那维莱特」），
    还是同一种界面；要分开的是画面别处的界面（星铁「ヨク・タベル」在画面顶上的两块）。"""
    h = max(1.0, min(a[3] - a[1], b[3] - b[1]))
    return abs((a[0] + a[2]) - (b[0] + b[2])) / 2 <= h and abs(a[1] - b[1]) <= SPOT_DY * h


def check(items: list[dict], doc: dict, game: str | None) -> dict | None:
    """给 `items`（`overlay.items`）里认上的名牌判字色，就地记 `ink`；返回 `overlay.name_ink`（层 → `#rrggbb`）。
    `doc` 是这些条目的 tracks（画面尺寸、`provenance.video` 记的源视频）。这款游戏没有名牌色、一块要判的都没有、
    找不到源视频或 PATH 上没有 ffmpeg 时是 None，什么都不记（后两种打一行提示：名牌照白字画）。
    没有证据的块（没取到帧、画面压暗）不计进占比、算不一致（要加量）；一种名牌量到的块都没有证据时不上色。"""
    ink = INK.get(game or "")
    if not ink:
        return None
    v = (doc.get("provenance") or {}).get("video")
    video = str(provenance.local(v)) if v else ""
    if not video or not Path(video).is_file() or not shutil.which("ffmpeg"):
        why = f"找不到源视频（tracks 的 provenance.video = {v!r}）" if shutil.which("ffmpeg") else "PATH 上没有 ffmpeg、取不了帧"
        print(f"[名牌字色] {why}，{game} 的名牌不判字色、照白字画")
        return None
    kinds: dict[tuple[str, str, int], list[dict]] = {}       # (层, 译名, 第几处) -> 按起点排的块
    spots: dict[tuple[str, str], list[list[float]]] = {}     # (层, 译名) -> 每处第一块的框
    for it in sorted(items, key=lambda it: it["start_us"]):
        if it["layer"] in ink and it.get("group") == "name" and it.get("cn"):
            at = spots.setdefault((it["layer"], it["cn"]), [])
            k = next((i for i, b in enumerate(at) if same_spot(b, it["rows"][0])), None)
            if k is None:
                k = len(at)
                at.append(it["rows"][0])
            kinds.setdefault((it["layer"], it["cn"], k), []).append(it)
    if not kinds:
        return None
    w, h = doc["size"]
    got: dict[int, bool | None] = {}             # id(块) -> 是不是名牌色写的（None：没有证据）
    t0, n_frames, no_frame, dim = time.perf_counter(), 0, 0, 0

    def measure(it: dict) -> tuple[bool | None, str]:
        rgb = grab(video, (it["start_us"] + it["end_us"]) // 2, w, h)
        if rgb is None:
            return None, "no_frame"
        s = ink_share(rgb, it["rows"][0], ink[it["layer"]])
        return (None, "dim") if s is None else (s >= MIN_SHARE, "")

    def run(todo: list[dict]) -> None:
        nonlocal n_frames, no_frame, dim
        with ThreadPoolExecutor(WORKERS) as pool:
            for it, (ok, why) in zip(todo, pool.map(measure, todo)):
                got[id(it)] = ok
                no_frame += why == "no_frame"
                dim += why == "dim"
        n_frames += len(todo)

    run([b[i] for b in kinds.values() for i in spread(len(b), FIRST)])
    unsure = [k for k, b in kinds.items() if (seen := {got[id(x)] for x in b if id(x) in got}) != {True} and seen != {False}]
    more = []
    for k in unsure:
        b = kinds[k]
        more += [b[i] for i in spread(len(b), MORE) if id(b[i]) not in got]
    run(more)
    plain = []
    for k, blocks in kinds.items():
        seen = [got[id(x)] for x in blocks if id(x) in got and got[id(x)] is not None]
        ok = bool(seen) and sum(seen) >= SHARE * len(seen)
        for it in blocks:
            it["ink"] = ok
        if not ok:
            plain.append(k)

    def where(k: tuple[str, str, int]) -> str:
        layer, cn, i = k
        return f"{cn}（{layer}" + (f"、第 {i + 1} 处" if len(spots[layer, cn]) > 1 else "") + "）"
    print(f"[名牌字色] {game}：{len(kinds)} 种名牌（层 + 译名 + 位置）量了 {n_frames} 帧（{time.perf_counter() - t0:.1f} s，"
          f"不一致、加量的 {len(unsure)} 种"
          + (f"；没有证据的 {no_frame + dim} 块：没取到帧 {no_frame}、画面压暗（或框在画面外）{dim}" if no_frame + dim else "")
          + f"）；不上色（PlainName）的 {len(plain)} 种"
          + (f"：{'、'.join(where(k) for k in plain[:8])}" if plain else ""))
    return {layer: to_hex(lab) for layer, lab in ink.items()}
