"""自选 OCR 范围与识别组（ocr-regions 计划）：范围的解析、某一时刻的生效遮罩、det 框按遮罩拆分。

**语义**（方案，owner 定）：

* 一个识别组 = 若干正矩形 / 负矩形。坐标是整幅画面宽高的**百分比**，可带原片时间段 `[t0, t1)`（秒，缺省 = 片头 / 片尾）。
  组 g 在时刻 t 的有效范围 `R_g(t) = ∪ 生效正矩形 − ∪ 生效负矩形`。
  组里**一个正矩形都没配** = 隐含全屏正矩形；**配了但此刻一个都不生效** = 此刻范围为空（不退回全屏）。
* 范围外的像素按 rec 的 padding 语义**填中灰**：rec 归一化 `(x/255 − 0.5)/0.5` 后补 0，对应原像素 127.5；
  uint8 只能取整，用 `FILL = 128`（张量里是 +0.0039，不宣称逐位相同，方案）。det 看到的是**同一种颜色**的像素，
  不是它张量里的 0（det 的逐通道归一化不同）。
* **det 框按遮罩拆**：框 B 里的有效像素 `B ∩ R_g(t)` 没有 → 丢；连通 → 一个框（有孔也不拆）；
  **被完全分断成几块 → 每块一个子框**，裁到那一块的最小外接矩形，外接矩形里**不属于它的有效像素也要涂灰**
  （包括同一父框的另一块）。连通性是**框内有效范围**的连通性，按 **4 邻接**（只在角上相碰不算连通）。
  没被分断的框也裁到有效部分的外接矩形：外面那圈本来就是灰的，留着只会让 rec 读一段灰。
* 时间：范围只在"配置切换"时刻变（`boundaries`）；那一刻前后的观测**不能被一个事件跨过**——
  由下游按 `_meta.regions` 里的切点断开（build_tracks），这里只负责给出切点。

像素坐标：百分比 × 宽高后**四舍五入**，矩形是半开区间 `[x0, x1) × [y0, y1)`，并截到画面内。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

FILL = 128
"""范围外像素的填充值（见文件头：rec padding 的 127.5 取整）。"""


@dataclass(frozen=True)
class Rect:
    x0: float
    y0: float
    x1: float
    y1: float
    neg: bool = False
    t0: float | None = None
    t1: float | None = None

    def active(self, t: float) -> bool:
        return (self.t0 is None or t >= self.t0) and (self.t1 is None or t < self.t1)

    def pixels(self, W: int, H: int) -> tuple[int, int, int, int]:
        c = lambda v, n: min(n, max(0, int(round(v / 100.0 * n))))   # noqa: E731
        return c(self.x0, W), c(self.y0, H), c(self.x1, W), c(self.y1, H)


def parse_rect(d: dict) -> Rect:
    box = d["box"]
    if len(box) != 4:
        raise ValueError(f"矩形要 [x0, y0, x1, y1]（百分比）：{d}")
    x0, y0, x1, y1 = (float(v) for v in box)
    if not (0 <= x0 < x1 <= 100 and 0 <= y0 < y1 <= 100):
        raise ValueError(f"矩形坐标要 0 ≤ x0 < x1 ≤ 100、0 ≤ y0 < y1 ≤ 100（百分比）：{d}")
    t = d.get("t") or [None, None]
    t0, t1 = (None if v is None else float(v) for v in t)
    if t0 is not None and t1 is not None and not t0 < t1:
        raise ValueError(f"时间段要 t0 < t1：{d}")
    return Rect(x0, y0, x1, y1, bool(d.get("neg", False)), t0, t1)


class RegionGroup:
    """一个识别组在一段视频上的范围。`mask_at(t)` 返回 None = 此刻不受限（全屏），否则 H×W 的 bool。"""

    def __init__(self, name: str, rects: list[Rect], W: int, H: int) -> None:
        self.name, self.rects, self.W, self.H = name, list(rects), W, H
        self.has_pos = any(not r.neg for r in self.rects)
        self._cache: dict[tuple, np.ndarray | None] = {}

    @property
    def unrestricted(self) -> bool:
        """整段都是全屏（默认组，或者只有空的配置）：调用方可以整个跳过遮罩。"""
        return not self.rects

    def key_at(self, t: float) -> tuple[int, ...]:
        return tuple(i for i, r in enumerate(self.rects) if r.active(t))

    def mask_for(self, key: tuple[int, ...]) -> np.ndarray | None:
        if key in self._cache:
            return self._cache[key]
        act = [self.rects[i] for i in key]
        pos = [r for r in act if not r.neg]
        neg = [r for r in act if r.neg]
        if not self.has_pos and not neg:
            m = None                                       # 没配正矩形、此刻也没负矩形：全屏
        else:
            m = np.zeros((self.H, self.W), bool)
            if self.has_pos:
                for r in pos:
                    x0, y0, x1, y1 = r.pixels(self.W, self.H)
                    m[y0:y1, x0:x1] = True
            else:
                m[:] = True                                # 没配正矩形 = 隐含全屏正矩形，仍扣负矩形
            for r in neg:
                x0, y0, x1, y1 = r.pixels(self.W, self.H)
                m[y0:y1, x0:x1] = False
            if m.all():
                m = None
        self._cache[key] = m
        return m

    def mask_at(self, t: float) -> np.ndarray | None:
        return self.mask_for(self.key_at(t))

    def boundaries(self) -> list[float]:
        """范围可能变化的时刻（秒，升序）。只列**真的变了遮罩**的：拆法不同的等价配置不产生切点。"""
        ts = sorted({v for r in self.rects for v in (r.t0, r.t1) if v is not None})
        out = []
        for t in ts:
            before, after = self.mask_for(self.key_at(t - 1e-6)), self.mask_for(self.key_at(t))
            if not _same(before, after):
                out.append(t)
        return out

    def observable_us(self, box, t0_us: int, t1_us: int) -> int:
        """[t0, t1) 里这个框位**看得见**的时长（µs）：按切点分段，**框里有有效像素**的段加起来（方案：
        常驻度 / UI 占比 / 时间支持的分母用"相关位置允许观察的时间"，关闭时段不算文字缺席）。

        2026-09-23（Codex 审计 P2）：原来只看框中心——带孔的连通框（有孔不拆）中心落在孔里、或区域的中位中心落在两块之间时，
        明明看得见却算成 0，调用方再 `max(1, ·)` 把占比放大成上百万。"""
        cuts = [int(round(b * 1e6)) for b in self.boundaries() if t0_us < b * 1e6 < t1_us]
        edges = [t0_us, *cuts, t1_us]
        tot = 0
        for a, b in zip(edges, edges[1:]):
            m = self.mask_at((a + b) / 2e6)
            if m is None or box_hits(box, m):
                tot += b - a
        return tot

    def union_bbox(self) -> tuple[int, int, int, int] | None:
        """整段里**任何时刻**有效像素的外接矩形 `(x0, y0, x1, y1)`（像素，右下开）；某时刻全屏 / 从来没有有效像素时返回 None（没得裁）。
        方案的"遮罩加单个最小外接矩形"：一个**静态**的框，盖住每一刻的有效范围——时变配置也对（框里被关掉的部分本来就是涂过的）。"""
        keys = {self.key_at(-1e18)} | {self.key_at(t) for t in self.boundaries()}
        rows = cols = None
        for key in keys:
            m = self.mask_for(key)
            if m is None:
                return None
            r, c = m.any(axis=1), m.any(axis=0)
            rows = r if rows is None else rows | r
            cols = c if cols is None else cols | c
        if rows is None or not rows.any():
            return None
        ys, xs = np.flatnonzero(rows), np.flatnonzero(cols)
        return int(xs[0]), int(ys[0]), int(xs[-1]) + 1, int(ys[-1]) + 1

    def changed_area(self, t: float) -> np.ndarray | None:
        """切点 t 前后遮罩不同的像素（bool H×W）；None = 没变。下游拿它判"哪些事件被这个切点切到"。"""
        before, after = self.mask_for(self.key_at(t - 1e-6)), self.mask_for(self.key_at(t))
        if _same(before, after):
            return None
        full = np.ones((self.H, self.W), bool)
        return (full if before is None else before) != (full if after is None else after)


def take(polys: list, boxes: list, own_of: dict, keep: list[int]) -> tuple[list, list, dict]:
    """按保留的下标 `keep` 同时取多边形、框和子框遮罩（`split_polys` 的三件套是平行的，**必须一起重排**）。"""
    return ([polys[i] for i in keep], [boxes[i] for i in keep],
            {j: own_of[i] for j, i in enumerate(keep) if i in own_of})


def box_hits(box, area: np.ndarray) -> bool:
    """框（至少 1 像素，截到画面内）里有没有 `area` 为真的像素。"""
    H, W = area.shape
    x0, y0 = min(W - 1, max(0, int(box[0]))), min(H - 1, max(0, int(box[1])))
    x1, y1 = max(x0 + 1, min(W, int(box[2]))), max(y0 + 1, min(H, int(box[3])))
    return bool(area[y0:y1, x0:x1].any())


def _same(a: np.ndarray | None, b: np.ndarray | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return bool(np.array_equal(a, b))


def fill_rects(mask: np.ndarray) -> list[tuple[int, int, int, int]]:
    """`~mask` 拆成矩形 `(y0, y1, x0, x1)`：先按"行完全相同"分横带，每条带里按列取连续段。**纯函数，守卫直接测。**
    遮罩都是矩形的并减矩形，拆出来只有几块；一个遮罩只拆一次（`apply_mask` 按对象缓存）。"""
    H = mask.shape[0]
    cuts = np.flatnonzero(np.any(mask[1:] != mask[:-1], axis=1)) + 1
    ys = [0, *cuts.tolist(), H]
    out = []
    for y0, y1 in zip(ys, ys[1:]):
        d = np.diff(np.concatenate(([0], (~mask[y0]).astype(np.int8), [0])))
        out += [(y0, y1, int(a), int(b)) for a, b in zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1))]
    return out


_RECTS: dict[int, tuple[np.ndarray, list]] = {}


def apply_mask(frame: np.ndarray, mask: np.ndarray | None) -> np.ndarray:
    """范围外涂 FILL（**原地**，返回同一个数组）。frame 是 H×W×C 或 H×W，和 mask 同尺寸。
    按矩形切片赋值、结果和 `frame[~mask] = FILL` 逐位相同——布尔索引在 1080p 上一帧 48 ms，
    自选范围的一组整体反而比全屏慢 59%（hsr-s2 35.7 对 22.4 s，2026-09-24）；切片赋值约 1 ms。"""
    if mask is None:
        return frame
    hit = _RECTS.get(id(mask))
    if hit is None or hit[0] is not mask:               # 遮罩对象是 RegionGroup / aux_masker 缓存着的，id 稳定
        hit = _RECTS[id(mask)] = (mask, fill_rects(mask))
    for y0, y1, x0, x1 in hit[1]:
        frame[y0:y1, x0:x1] = FILL
    return frame


def label4(m: np.ndarray) -> tuple[int, np.ndarray]:
    """4 邻接连通域标号（同 `cv2.connectedComponents(connectivity=4)` 的约定：0 = 背景，块从 1 起），**不依赖 cv2**（守卫在没装 cv2 的解释器里跑）。
    做法：按行 / 列的变化点把遮罩压成小网格——任意两条相邻变化点之间的格子内部必然一致——在网格上 BFS，再展开回像素。
    范围是轴对齐矩形拼出来的，网格只有几格到几十格。"""
    h, w = m.shape
    yb = np.concatenate(([0], np.flatnonzero(np.any(m[1:] != m[:-1], axis=1)) + 1, [h]))
    xb = np.concatenate(([0], np.flatnonzero(np.any(m[:, 1:] != m[:, :-1], axis=0)) + 1, [w]))
    cell = m[yb[:-1]][:, xb[:-1]]
    cl = np.zeros(cell.shape, np.int32)
    n = 0
    for i0, j0 in zip(*np.nonzero(cell)):
        if cl[i0, j0]:
            continue
        n += 1
        cl[i0, j0] = n
        stack = [(i0, j0)]
        while stack:
            i, j = stack.pop()
            for a, b in ((i - 1, j), (i + 1, j), (i, j - 1), (i, j + 1)):
                if 0 <= a < cell.shape[0] and 0 <= b < cell.shape[1] and cell[a, b] and not cl[a, b]:
                    cl[a, b] = n
                    stack.append((a, b))
    lab = np.repeat(np.repeat(cl, np.diff(yb), axis=0), np.diff(xb), axis=1)
    return n + 1, lab


def split_box(box, mask: np.ndarray | None) -> list[tuple[list[int], np.ndarray | None]]:
    """一个 det 框按遮罩拆（见文件头）。返回 [(子框, own)]：`own` 是子框大小的 bool，
    **子框里有不属于这一块的有效像素时**才给（rec / 复用取裁剪时要把 ~own 涂灰），否则 None。
    mask 为 None（此刻全屏）原样返回。"""
    bx0, by0, bx1, by1 = (int(v) for v in box)
    if mask is None:
        return [([bx0, by0, bx1, by1], None)]
    H, W = mask.shape
    # det 框可能略出画面：负坐标切片会从另一头绕回来，先截到画面里再切
    x0, y0, x1, y1 = max(0, bx0), max(0, by0), min(W, bx1), min(H, by1)
    m = mask[y0:y1, x0:x1]
    if m.size and m.all():
        return [([bx0, by0, bx1, by1], None)]              # 整框都在范围里：原样（不拿截过的框替换）
    if not m.any():
        return []
    n, lab = label4(m)
    out = []
    for k in range(1, n):
        ys, xs = np.nonzero(lab == k)
        sx0, sy0, sx1, sy1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
        sub = lab[sy0:sy1, sx0:sx1]
        own = sub == k
        foreign = (sub != k) & (sub != 0)
        out.append(([x0 + sx0, y0 + sy0, x0 + sx1, y0 + sy1], own if foreign.any() else None))
    out.sort(key=lambda s: (s[0][0], s[0][1]))
    return out


def masked_crop(frame: np.ndarray, box, own: np.ndarray | None) -> np.ndarray:
    """子框的裁剪：`own` 给了就把不属于这一块的像素涂灰（返回副本，不写坏源帧）。"""
    x0, y0, x1, y1 = box
    c = frame[y0:y1, x0:x1]
    if own is None:
        return c
    c = c.copy()
    c[~own] = FILL
    return c


# ---------- 接进管线用的几件（run_ocr2 / build_tracks） ----------

def group_spec(path: str | Path, name: str = "") -> dict:
    """配置文件里挑出**一个组**的规格（规范化的 dict，进 `_meta.config` 当复用判据用：同内容换路径算同一条臂，
    改了文件内容就对不上）。文件里只有一个组时 `name` 可以不给；多个组必须给——每组是一条独立 OCR 线（方案）。"""
    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    groups = spec.get("groups") or [{"name": "all", "rects": []}]          # 没有组 = 一个隐含的全屏组（方案表第一行）
    names = [g.get("name") for g in groups]
    if any(not n for n in names) or len(set(names)) != len(names):
        raise ValueError(f"{path}：每个组要有唯一的 name（产物按组分文件）：{names}")
    if not name:
        if len(groups) != 1:
            raise ValueError(f"{path} 里有 {len(groups)} 个组 {names}，要用 --region-group 挑一个")
        g = groups[0]
    else:
        hit = [g for g in groups if g.get("name") == name]
        if not hit:
            raise ValueError(f"{path} 里没有组 {name!r}（有 {names}）")
        g = hit[0]
    rects = [parse_rect(r) for r in g.get("rects") or ()]          # 早校验
    return {"name": g["name"], "rects": [{"box": [r.x0, r.y0, r.x1, r.y1], "neg": r.neg, "t": [r.t0, r.t1]}
                                         for r in rects]}


def from_spec(spec: dict, W: int, H: int) -> RegionGroup:
    """`group_spec` 的 dict -> RegionGroup（run_ocr2 起组、build_tracks 从 obs 的 `_meta.regions` 重建都走它）。"""
    return RegionGroup(spec["name"], [parse_rect(r) for r in spec.get("rects") or ()], W, H)


class MaskedSource:
    """包一层取帧来源：每帧按它的真实 pts 涂掉范围外的像素（**det 之前**，方案），其余属性原样转给里面那个。
    帧是来源每次新给的数组（run_ocr2 的 det_stream 注释），原地涂不会写坏别人的东西。"""

    def __init__(self, source, group: RegionGroup) -> None:
        self._src, self._g = source, group
        self._closed: dict[tuple, bool] = {}

    def detection_closed_at(self, t: float) -> bool:
        """此刻无有效像素，任何检测框都会被 split_polys 丢弃；None 遮罩表示全屏。"""
        key = self._g.key_at(t)
        if key not in self._closed:
            mask = self._g.mask_for(key)
            self._closed[key] = mask is not None and not mask.any()
        return self._closed[key]

    def __iter__(self):
        for idx, t_sec, frame in self._src:
            yield idx, t_sec, apply_mask(frame, self._g.mask_at(t_sec))

    def __getattr__(self, k):
        return getattr(self._src, k)


def aux_masker(group: RegionGroup, Ws: int, Hs: int, src_fps: float):
    """辅助流（缩小的灰度帧，edge_refine 的帧环）用的遮罩：同一个范围按比例缩到辅助流尺寸（最近邻），按帧号换算时间。
    ⚠ 辅助流只有帧号没有逐帧 pts（edge_refine 文件头），时间按 `帧号 / 名义帧率` 算——切点附近可能差一帧。
    辅助流的帧可能是只读的（`np.frombuffer`），要涂就先拷。"""
    ry = np.minimum((np.arange(Hs) * group.H) // Hs, group.H - 1)       # 最近邻取样（不依赖 cv2）
    rx = np.minimum((np.arange(Ws) * group.W) // Ws, group.W - 1)
    cache: dict[tuple, np.ndarray | None] = {}

    def pre(idx: int, frame: np.ndarray) -> np.ndarray:
        key = group.key_at(idx / src_fps)
        if key not in cache:
            m = group.mask_for(key)
            cache[key] = None if m is None else m[ry][:, rx]
        m = cache[key]
        if m is None:
            return frame
        return apply_mask(frame.copy(), m)
    return pre


class CroppedDet:
    """`--region-crop`（方案的对照臂）：det 只看组范围的静态外接矩形那一块（`RegionGroup.union_bbox`；范围外本来就涂过），
    多边形平移回原片坐标。和 `FastDet` 同接口（`predict` / `predict_batch`）。
    ⚠ **会改 det 的输入尺寸**（缩放策略按边长走，小块可能被放大）——框会变，所以它进复用判据、是一条要单独对质量和速度的臂，不是调度旋钮。"""

    def __init__(self, inner, bbox: tuple[int, int, int, int]) -> None:
        self.inner, self.bbox = inner, bbox
        self.off = np.array([bbox[0], bbox[1]])

    def _crop(self, img: np.ndarray) -> np.ndarray:
        x0, y0, x1, y1 = self.bbox
        return np.ascontiguousarray(img[y0:y1, x0:x1])

    def _shift(self, polys):
        return [np.asarray(p) + self.off for p in polys]

    def predict(self, img: np.ndarray):
        polys, scores = self.inner.predict(self._crop(img))
        return self._shift(polys), scores

    def predict_batch(self, imgs: list) -> list[tuple]:
        return [(self._shift(p), s) for p, s in self.inner.predict_batch([self._crop(im) for im in imgs])]

    # 流水的两半（`FastDet.infer_batch` / `post_batch`）：裁在前半、平移在后半
    def infer_batch(self, imgs: list) -> tuple:
        return self.inner.infer_batch([self._crop(im) for im in imgs])

    def post_batch(self, state: tuple) -> list[tuple]:
        return [(self._shift(p), s) for p, s in self.inner.post_batch(state)]


def split_polys(polys, mask: np.ndarray | None):
    """det 的多边形按遮罩拆（`split_box`）。返回 (新多边形表, 新框表, {新下标: own})。
    框没被遮到的原样保留多边形；裁过 / 拆过的换成子框的矩形（几何裁剪不是模型的新检测，方案第 2 条）。"""
    boxes_in = [[round(min(float(p[0]) for p in q)), round(min(float(p[1]) for p in q)),     # 口径同 run_ocr2.poly_to_box
                 round(max(float(p[0]) for p in q)), round(max(float(p[1]) for p in q))] for q in polys]
    if mask is None:
        return list(polys), boxes_in, {}
    out_p, out_b, own_of = [], [], {}
    for q, b in zip(polys, boxes_in):
        for sb, own in split_box(b, mask):
            out_p.append(q if sb == b else [[sb[0], sb[1]], [sb[2], sb[1]], [sb[2], sb[3]], [sb[0], sb[3]]])
            if own is not None:
                own_of[len(out_b)] = own
            out_b.append(sb)
    return out_p, out_b, own_of


def cuts_us(group: RegionGroup) -> list[tuple[int, np.ndarray]]:
    """时间关闭段的切点（µs）+ 那一刻遮罩变了的像素：下游判"哪些事件被这个切点切到"（build_tracks.build_runs）。"""
    return [(int(round(t * 1e6)), group.changed_area(t)) for t in group.boundaries()]
