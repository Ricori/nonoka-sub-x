"""打字机的**首字出现 / 全字出现**：多信号融合（`refine_boundaries` 调它；纯逻辑，不碰视频，守卫够得着）。

为什么不再用"整框相关系数过门"（2026-09-12，owner 看预览说前两个时间点不准）：
最后一两个字对整框相关系数的贡献太小，**还没打完就过门**，全字出现系统性偏早 4 帧以上；
首字出现拿"不像窗口首帧"判，背景一动、上一句淡出也触发。

几路互相独立的信号，各有各的死角：

| 信号 | 首字 | 全字 | 死角 |
| --- | --- | --- | --- |
| OCR 中途读数（`ocr_fit`）：打字中途读到的半截字数对时间做直线，外推到第 0.5 / N−0.5 个字 | ✓ | ✓ | 多数事件只有 1–2 个中途读数（只有 1 个时借同一段视频的典型速度） |
| 首字格"开始出现"（`rise_onset`）：首字格字形一致率从定稿帧往回走到单调上升的起点 | ✓ | | 立绘压在首字格上 |
| 逐字格定稿时刻 + Theil–Sen 直线（`cell_fit`）：截距 = 首字，末端 = 全字 | ✓ | ✓ | 半角字 / 淡入长的字体上截距偏晚 |
| 尾字格定稿（`glyph_onset`） | | ✓ | 淡入长时偏晚 |
| 整框不像窗口首帧（旧判据） | ✓ | | 背景一动就偏早 |

融合（`fuse`）：**OCR 证据给硬区间**（首字 ∈ [t_start − 一个采样间隔, t_start]；全字不早于首字、
也不早于任何一次"字还没读全"的读数），区间外的候选丢掉；剩下的按 `FUSE_TOL_US` 聚类取成员最多的一簇的中位数，
平票取列表里靠前的（顺序就是单信号可信度）。一个都不剩就退回采样级值、夹进区间，并记 `none`。
全字**只防晚很多**：不晚于 OCR 读全 + `LATE_CAP_US`；没有两路同意时不晚于最早候选 + `LATE_CAP_US`、记 `capped`
（owner：晚 10 帧以内都还好，晚很多是灾难，早很多无非一开始就全显示）。

常数只有像素 / 物理量（字形偏离 40、容差 30、一致率 0.6、同意 = 3 帧、"没读全" = 不到 80% 字数），
**没有 ms/char**（product-goals 第 7 条），也没有按游戏特化。

验证（五款游戏 + 两款 galgame / RPG，逐帧盲标，`dev_tools/typewriter_eval.py` + `data/gt/typewriter-*.json`）
见 text-effects 报告的"多信号融合"一节。
"""
from __future__ import annotations

from difflib import SequenceMatcher

import numpy as np

GLYPH_DIFF = 40
"""字形像素：和格内中位数的差至少这么多（灰度级）。"""
GLYPH_TOL = 30
"""一帧里的字形像素和模板差这么多以内算"已经是最终样子"。"""
GLYPH_THR = 0.6
"""字形像素里"已经是最终样子"的比例过这个门，这一格就算定稿。"""
EMPTY_THR = 0.90
"""旧判据：整框和窗口首帧的相关系数低于这个，算"有新东西了"。"""
RISE_EPS = 0.01
"""`rise_onset` 往回走时的去抖（分数是像素比例，这个量级是噪声）。"""
FUSE_TOL_US = 50_000
"""两个候选差这么多以内算"同意"（60 fps 下 3 帧）。"""
LATE_CAP_US = 166_667
"""全字最多允许比 OCR 读全 / 最早候选晚这么多（10 帧 @60fps）——owner 给的"晚了还能接受"的上限。"""
FUSE_PARTIAL = 0.8
"""OCR 读到的字数不到终稿这么多，算"那一刻还没打完"。"""
OCR_MIN_SIM = 0.6
"""中途读数和终稿前缀的相似度门。"""
CELL_MIN_PX = 8
"""一个字格里字形像素少于这么多就不给它定时（空格 / 标点缝）。"""


def chars(s: str) -> str:
    return "".join(s.split())


def ncc(a: np.ndarray, b: np.ndarray) -> float:
    """归一化相关系数；常量图恒为 0（audit-4 C5 那条的来源，调用方要自己判"没找到"）。"""
    if a.shape != b.shape or a.size == 0:
        return 0.0
    a = a.astype(np.float32) - a.mean()
    b = b.astype(np.float32) - b.mean()
    d = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float((a * b).sum() / d) if d > 1e-6 else 0.0


# uint8 灰度上的中位数 / 95 分位用 256 格直方图算（2026-10-16，rec-workload 计划）：回抠线程里 `polarity` 原来占约 15%，
# 直方图版快约 10 倍；取哪两个次序统计量、怎么插值逐步照抄 numpy 2.x 的 median / percentile（linear），随机与偏态数组 2 万例逐位相同。
_Q95 = np.true_divide(95, 100)


def _order(c: np.ndarray, k: int) -> int:
    """累计计数 `c`（下标 = 值）里排序后第 k 个（0 起）元素的值。"""
    return int(np.searchsorted(c, k, side="right"))


def _u8_median_int(img: np.ndarray, c: np.ndarray | None = None) -> int:
    """`int(np.median(img))`，img 是 uint8。"""
    if c is None:
        c = np.cumsum(np.bincount(img.ravel(), minlength=256))
    n = img.size
    return _order(c, n // 2) if n % 2 else (_order(c, n // 2 - 1) + _order(c, n // 2)) // 2


def _pct95(h: np.ndarray):
    """计数 `h[d]`（值 d 的个数，按 int16 数组算）上的 `np.percentile(·, 95)`：同 numpy 的 linear 与 `_lerp`。"""
    c = np.cumsum(h)
    n = int(c[-1])
    v = (n - 1) * _Q95
    lo = int(np.floor(v))
    if v >= n - 1:
        return np.float64(np.int16(_order(c, n - 1)))
    g = v - np.float64(lo)
    a, b = np.int16(_order(c, lo)), np.int16(_order(c, lo + 1))
    d = b - a
    return b - d * (1 - g) if g >= 0.5 else a + d * g


def _u8_median_f32(u: np.ndarray) -> np.float32:
    """`np.median(u.astype(np.float32))`，u 是非空 uint8。"""
    c = np.cumsum(np.bincount(u.ravel(), minlength=256))
    n = u.size
    return np.float32(_order(c, n // 2)) if n % 2 else np.float32((_order(c, n // 2 - 1) + _order(c, n // 2)) / 2)


def _u8_rowmedian_f32(x: np.ndarray) -> np.ndarray:
    """`np.median(x.astype(np.float32), axis=1)`，x 是 (行, 列≥1) 的 uint8：每行一份直方图、一次 bincount。"""
    r, n = x.shape
    h = np.bincount((x + (np.arange(r, dtype=np.intp) * 256)[:, None]).ravel(), minlength=256 * r).reshape(r, 256)
    c = np.cumsum(h, axis=1)
    hi = (c > n // 2).argmax(axis=1)
    if n % 2:
        return hi.astype(np.float32)
    return ((c > n // 2 - 1).argmax(axis=1) + hi).astype(np.float32) / np.float32(2)


def _first_unlike_ref(frames: np.ndarray) -> int | None:
    """`_first_unlike` 的原式（离门限近时退回它）。"""
    stds = frames.std(axis=(1, 2))
    if not stds[0] >= 1.0:
        return None
    F = frames.astype(np.float32).astype(np.float64)
    A = F - F.mean(axis=(1, 2))[:, None, None]
    den = np.sqrt((A * A).sum(axis=(1, 2))) * np.sqrt((A[0] * A[0]).sum())
    num = (A * A[0]).sum(axis=(1, 2))
    cc = np.where(den > 1e-6, num / np.where(den > 1e-6, den, 1.0), 0.0)
    hit = np.nonzero((stds >= 1.0) & (cc < EMPTY_THR))[0]
    return int(hit[0]) if hit.size else None


def _first_unlike(frames: np.ndarray) -> int | None:
    """(帧, 行, 列) 的 uint8 叠里第一帧"有内容（std ≥ 1）且和首帧的相关系数 < `EMPTY_THR`"的下标；首帧 std < 1 就 None。
    用整数矩算：Σx、Σx²、Σx·x₀ 走 float64 求和 / 点积，每一步都是 < 2^53 的整数、精确；std 和相关系数由它们直接得出。
    有一帧离两道门限近到原式的浮点可能差最后一位（相对差 ≤ 1e-6）就整叠退回原式（`_first_unlike_ref`），所以结果逐位同原式。
    原式对整叠做 float64 去均值和逐元素乘，占 `signals` 一半多（2026-10-16，rec-workload 计划）。"""
    t = frames.reshape(len(frames), -1)
    n = t.shape[1]
    if n == 0 or len(frames) == 0 or frames.dtype != np.uint8 or n > 10_000_000:
        return _first_unlike_ref(frames)                 # 整数矩精确的前提：uint8；n²·65025 装得进 int64（n < 约 1.19e7）
    f = t.astype(np.float64)
    s = f.sum(axis=1).astype(np.int64)
    d = n * np.einsum("ij,ij->i", f, f).astype(np.int64) - s * s      # n² × 方差
    nn = n * n
    if (np.abs(d - nn) <= nn * 1e-6).any():
        return _first_unlike_ref(frames)
    big = d >= nn
    if not big[0]:
        return None
    cov = n * (f @ f[0]).astype(np.int64) - s * s[0]
    cc = np.where(d > 0, cov / (np.sqrt(np.maximum(d, 1).astype(np.float64)) * np.sqrt(float(d[0]))), 0.0)
    if (big & (np.abs(cc - EMPTY_THR) <= 1e-6)).any():
        return _first_unlike_ref(frames)
    hit = np.nonzero(big & (cc < EMPTY_THR))[0]
    return int(hit[0]) if hit.size else None


def _hits(frames: np.ndarray, ref: np.ndarray, ys: np.ndarray, xs: np.ndarray) -> np.ndarray:
    """(帧, 像素)：`ys, xs` 那些像素上和 `ref` 差 ≤ `GLYPH_TOL`。只碰字形像素、不对整叠做 int16 减法；
    uint8 上直接比上下界（同 `|帧 − ref| ≤ GLYPH_TOL`）。"""
    if frames.dtype != np.uint8 or not isinstance(GLYPH_TOL, int):     # 非整数容差时 uint8 上下界会截断
        return np.abs(frames[:, ys, xs].astype(np.int16) - ref[ys, xs].astype(np.int16)) <= GLYPH_TOL
    g = (frames.reshape(len(frames), -1)[:, ys * frames.shape[2] + xs] if frames.flags.c_contiguous
         else frames[:, ys, xs])
    r = ref[ys, xs].astype(np.int16)
    return (g >= np.maximum(r - GLYPH_TOL, 0).astype(np.uint8)) & (g <= np.minimum(r + GLYPH_TOL, 255).astype(np.uint8))


def ncc_to(tpl: np.ndarray):
    """`lambda a: ncc(a, tpl)`，模板那一侧（去均值、范数）只算一次：同一模板比很多帧时用，逐位同 `ncc`。"""
    if tpl.size == 0:
        return lambda a: 0.0
    b = tpl.astype(np.float32) - tpl.mean()
    nb = np.linalg.norm(b)

    def f(a: np.ndarray) -> float:
        if a.shape != b.shape:
            return 0.0
        a = a.astype(np.float32) - a.mean()
        d = float(np.linalg.norm(a) * nb)
        return float((a * b).sum() / d) if d > 1e-6 else 0.0
    return f


def polarity(img: np.ndarray) -> bool:
    """True = 亮字。看的是更极端的那一侧（偏离量的 95 分位）——一行里字占的像素最多。"""
    if img.dtype == np.uint8 and img.size:
        h = np.bincount(img.ravel(), minlength=256)
        med = _u8_median_int(img, np.cumsum(h))
        hb, hd = h[med + 1:], h[:med][::-1]              # 亮侧偏离 1..255-med、暗侧偏离 1..med（下标 0 补一个空格）
        pb = _pct95(np.concatenate(([0], hb))) if hb.sum() else 0
        pd = _pct95(np.concatenate(([0], hd))) if hd.sum() else 0
        return bool(pb >= pd)
    dev = img.astype(np.int16) - int(np.median(img))
    bright, dark = dev[dev > 0], -dev[dev < 0]
    return bool((np.percentile(bright, 95) if bright.size else 0) >= (np.percentile(dark, 95) if dark.size else 0))


def glyph_mask(tpl: np.ndarray, bright: bool | None = None) -> np.ndarray:
    """模板里算字形的像素：只取字那一侧的极性——两侧都算会把描边、底板边缘、背景纹理收进来。"""
    t = tpl.astype(np.int16)
    dev = t - (_u8_median_int(tpl) if tpl.dtype == np.uint8 and tpl.size else int(np.median(t)))
    if bright is None:
        bright = polarity(tpl)
    return dev >= GLYPH_DIFF if bright else dev <= -GLYPH_DIFF


def glyph_score(frames: np.ndarray, tpl: np.ndarray, bright: bool | None = None) -> np.ndarray:
    """逐帧：模板字形像素里，和模板差 ≤ `GLYPH_TOL` 的比例。字形像素太少（< 20）就全是 NaN。"""
    mask = glyph_mask(tpl, bright)
    m = int(mask.sum())
    if m < 20:
        return np.full(len(frames), np.nan)
    ys, xs = np.nonzero(mask)
    return np.count_nonzero(_hits(frames, tpl, ys, xs), axis=1) / m      # 布尔均值本来就是"计数 / 个数"，逐位同 .mean


def last_onset(ok) -> int | None:
    """最后一段一直持续到窗口末尾的"过门"的起点下标；末帧都不过门 = 没找到。"""
    ok = list(ok)
    if not ok or not ok[-1]:
        return None
    i = len(ok) - 1
    while i > 0 and ok[i - 1]:
        i -= 1
    return i


def rise_onset(scores) -> int | None:
    """首字格"开始出现"：从最后一段过门的起点往回走，分数还在单调下降就继续，停在局部最低点的下一帧。
    字一帧弹出（星铁 / 绝区零 / 像素 RPG）时和定稿同一帧；逐字淡入（魔裁）时退到淡入起点。"""
    s = [float(x) for x in scores]
    i1 = next((i for i in range(len(s) - 1, -1, -1) if not s[i] >= GLYPH_THR), None)
    if i1 is None or i1 == len(s) - 1:
        return None
    i1 += 1
    i = i1
    while i > 0 and s[i - 1] < s[i] - RISE_EPS:
        i -= 1
    return min(i + 1, i1) if i < i1 else i1


def theil_sen(xs, ys) -> tuple[float, float] | None:
    """稳健直线 y = a + b·x（b 夹到 ≥ 0：字不会倒着打）。被立绘挡住 / 背景污染的字格当离群值。"""
    if len(xs) < 2:
        return None
    x = np.asarray(xs, float)
    y = np.asarray(ys, float)
    i, j = np.triu_indices(len(x), 1)
    dx = x[j] - x[i]
    ok = dx != 0
    b = max(0.0, float(np.median((y[j] - y[i])[ok] / dx[ok]))) if ok.any() else 0.0
    return float(np.median(y - b * x)), b


def cell_fit(frames: np.ndarray, ts: list[int], x0: int, x1: int, n_chars: int) -> tuple[float, float] | None:
    """整行按字数等分成格，每格找"定稿"帧，Theil–Sen 拟合 → (首字, 全字)。"""
    if n_chars < 2:
        return None
    k = len(frames) - 1
    mask = glyph_mask(frames[k], polarity(frames[k]))
    px, py = np.nonzero(mask.T)                       # 字形像素按列排：每格是一段连续的切片
    close = _hits(frames, frames[k], py, px)
    cw = (x1 - x0) / n_chars
    xs, ys = [], []
    for c in range(n_chars):
        lo, hi, _ = slice(int(round(x0 + c * cw)), int(round(x0 + (c + 1) * cw))).indices(mask.shape[1])
        i0, i1 = np.searchsorted(px, (lo, hi))
        if i1 - i0 < CELL_MIN_PX:
            continue
        o = last_onset(np.count_nonzero(close[:, i0:i1], axis=1) / (i1 - i0) >= GLYPH_THR)
        if o is not None:
            xs.append(c)
            ys.append(ts[o])
    fit = theil_sen(xs, ys)
    if fit is None:
        return None
    a, b = fit
    return a, a + b * (n_chars - 1)


def ocr_points(rows, box, final: str) -> list[tuple[int, int]]:
    """窗口里的 OCR 观测 → [(t_us, 读到几个字)]：同一行位置（y 重叠过半、左沿差不到 1.5 个字高）、
    文本像终稿的前缀。`rows` = [(t_us, box, text)]，调用方先按时间圈好。"""
    x0, y0, _, y1 = box
    h = max(1.0, y1 - y0)
    N = len(final)
    best: dict[int, tuple[float, int]] = {}
    for t, bx, text in rows:
        if min(bx[3], y1) - max(bx[1], y0) < 0.5 * h or abs(bx[0] - x0) > 1.5 * h:
            continue
        q = chars(text)
        if not q:
            continue
        s = SequenceMatcher(None, q, final[:len(q)]).ratio()
        if s >= OCR_MIN_SIM and (t not in best or s > best[t][0]):
            best[t] = (s, min(len(q), N))
    return sorted((t, n) for t, (_, n) in best.items())


OCR_SLOPES_MIN = 5
"""全片 OCR 打字速度（`v_ocr`）至少要几条拟得出斜率的句子，不够就不给、退回逐字格斜率 `v_cell`（2026-09-24）。
正常素材要么一句都拟不出（魔裁：打字快，一句在一个采样间隔内打完），要么很多句（gi-s2 31、zzz-s1 9、hsr 364——都是过了下面单调性那道门之后的数，
和 `v_cell` 差 2%~4%）；只有两三句的时候那几句多半是框抖出来的假中途读数——yuka-f1 遮罩臂 3 句把全片定成 163 ms/字（真值约 17），
首字外推整体挪位（ocr-regions 末尾）。"""


def ocr_slope(pts, n_chars: int) -> float | None:
    """这条的打字速度（µs/字），要至少两个不同时刻的中途读数，而且**字数随时间严格增加**（打字只会长）：
    停在同一个字数上、或者读少了（`3, 12, 11, 11, 11` 这种，框抖 / 读半截），不是在打字，不拿来拟速度。"""
    part = sorted((t, n) for t, n in pts if 0 < n < n_chars - 0.5)
    if len({t for t, _ in part}) < 2 or len({n for _, n in part}) < 2:     # 字数没变拟合不出速度
        return None
    if any(n1 <= n0 for (_, n0), (_, n1) in zip(part, part[1:])):
        return None
    b, _ = np.polyfit([n for _, n in part], [t for t, _ in part], 1)
    return float(b) if b > 0 else None


def ocr_fit(pts, n_chars: int, t_start: int, t_full_sampled: int, span_us: int,
            v: float | None) -> tuple[float | None, float | None]:
    """中途读数外推 → (首字, 全字)，夹进 OCR 证据给的区间。只有一个读数时借 `v`（同一段视频的典型速度）。
    多个读数但字数**不随时间严格增加**（停住 / 读少了，`ocr_slope` 同一道门）时不拿它们拟直线，按只有第一个读数处理。"""
    part = sorted((t, n) for t, n in pts if 0 < n < n_chars - 0.5)
    typing = all(n1 > n0 for (_, n0), (_, n1) in zip(part, part[1:]))
    if len({t for t, _ in part}) >= 2 and len({n for _, n in part}) >= 2 and typing:
        b, a = np.polyfit([n for _, n in part], [t for t, _ in part], 1)
        first, full = a + b * 0.5, a + b * (n_chars - 0.5)
    elif part and v:
        t1, n1 = part[0]
        first = t1 - (n1 - 0.5) * v
        full = first + (n_chars - 1) * v
    else:
        return None, None
    first = min(max(first, t_start - span_us), t_start)
    lo = max([t for t, _ in part] + [first])
    return first, min(max(full, lo), t_full_sampled)


def fuse(cands, lo: float, hi: float, fallback: float) -> tuple[float, str]:
    """区间内的候选按 `FUSE_TOL_US` 聚类，取成员最多的一簇的中位数（平票取列表里靠前的）。
    返回 (值, "agree" 至少两路同意 / "single" 只剩一路 / "none" 一路都不剩、退回 fallback)。"""
    c = [v for v in cands if v is not None and lo - FUSE_TOL_US <= v <= hi + FUSE_TOL_US]
    if not c:
        return min(max(fallback, lo), hi), "none"
    best = None
    for i, v in enumerate(c):
        mem = [w for w in c if abs(w - v) <= FUSE_TOL_US]
        key = (len(mem), -i)
        if best is None or key > best[0]:
            best = (key, mem)
    mem = sorted(best[1])
    mid = len(mem) // 2
    val = mem[mid] if len(mem) % 2 else (mem[mid - 1] + mem[mid]) / 2
    return min(max(val, lo), hi), ("agree" if len(mem) >= 2 else "single")


def snap(ts: list[int], t: float | None) -> int | None:
    return None if t is None else min(ts, key=lambda x: abs(x - t))


def estimate(frames: np.ndarray, ts: list[int], inner: tuple[int, int, int, int], text: str,
             t_start: int, t_full_sampled: int, span_us: int, ocr_pts, v_ocr: float | None,
             v_cell: float | None = None) -> dict | None:
    """`signals` + `combine` 一步到位（整段速度不需要从别的条目借时用）。"""
    sig = signals(frames, ts, inner, text)
    return None if sig is None else combine(sig, t_start, t_full_sampled, span_us, ocr_pts, v_ocr, v_cell)


def signals(frames: np.ndarray, ts: list[int], inner: tuple[int, int, int, int], text: str) -> dict | None:
    """一条事件的像素信号（解码时算，不需要别的条目）。

    `frames`：从 t_start 前一个多采样间隔到 t_full_sampled 后一个采样间隔的**连续**帧，框外扩几像素的灰度裁剪；
    最后一帧当"最终样子"的模板。`inner` = 框在裁剪里的 (x0, y0, x1, y1)。时间都是 µs。"""
    final = chars(text)
    N = len(final)
    if N < 1 or len(frames) < 3:
        return None
    ts = list(ts)
    k = len(frames) - 1
    x0, y0, x1, y1 = inner
    cell = max(1, y1 - y0)                              # CJK 一个字宽 ≈ 字高
    # 旧判据：整框不像窗口首帧。常量图的相关系数恒为 0（C5），一律"不像"——那不是证据，不给候选。
    # 整叠一次算（原来逐帧调 ncc，占 signals 的 2/3；两遍合一遍之后它跑在 OCR 旁边的线程里，2026-09-16）
    i = _first_unlike(frames)
    first_old = ts[i] if i is not None else None
    # 首字格"开始出现"（极性按整行判：一格里立绘占得多时，格内判会判成立绘的暗边）
    head = frames[:, :, :x0 + cell]
    r = rise_onset(glyph_score(head, head[k], polarity(frames[k])))
    first_rise = ts[r] if r is not None else None
    # 尾字格定稿（极性按这一格自己判）
    tail = frames[:, :, max(0, x1 - cell):]
    g = last_onset(glyph_score(tail, tail[k]) >= GLYPH_THR)
    full_glyph = ts[g] if g is not None else None
    return {"ts": ts, "n": N, "first_old": first_old, "first_rise": first_rise, "full_glyph": full_glyph,
            "cell": cell_fit(frames, ts, x0, x1, N)}


def cell_speed(sig: dict | None) -> float | None:
    """逐字格直线的斜率（µs/字）；给"OCR 拟不出速度时借全片中位数"用。"""
    if not sig or not sig["cell"] or sig["n"] < 2:
        return None
    first, full = sig["cell"]
    v = (full - first) / (sig["n"] - 1)
    return v if v > 0 else None


def combine(sig: dict, t_start: int, t_full_sampled: int, span_us: int, ocr_pts,
            v_ocr: float | None, v_cell: float | None) -> dict:
    """像素信号 + OCR 中途读数 → 融合后的首字 / 全字。

    打字速度只在**首字**外推里退到 `v_cell`：魔裁打字快，每条只有一个中途读数，OCR 拟不出速度，
    借逐字格斜率的全片中位数后首字 4–10 帧从 7/28 降到 2/28；同一个速度借给全字外推却系统性偏晚（淡入长，
    像素定稿比 OCR 读全晚），所以全字只认 OCR 自己拟出来的速度。"""
    ts, N, cf = sig["ts"], sig["n"], sig["cell"]
    first_old, first_rise, full_glyph = sig["first_old"], sig["first_rise"], sig["full_glyph"]
    ocr_first, _ = ocr_fit(ocr_pts, N, t_start, t_full_sampled, span_us, v_ocr or v_cell)
    _, ocr_full = ocr_fit(ocr_pts, N, t_start, t_full_sampled, span_us, v_ocr)

    # 外推值**落到区间下界上**（速度说"比上一个采样点还早"）就不投票（2026-09-24，ocr-regions）：它是四个候选里唯一不看这一条像素的，
    # 平票时又排第一——框挪 1~4 px、或全片中位速度被别的条目带偏，它就压在下界上赢下平票，首字早整整一个采样间隔
    # （yuka-f5 裁剪臂 design 6 条早 10 帧以上、代价 230；去掉之后 31，默认产物三段真值逐项相同、gi-s2 首字 8 -> 7）。
    # 不在下界上的外推值照旧排第一：它把淡入偏晚的像素测量拉回来（完全不让它投票 f1 / f5 首字代价 18 / 15 -> 36 / 36）。
    # ⚠ **上界（t_start）不对称地保留**：夹在上界上的外推值按同一个道理也只是界，但让它也不投票 f5 design 首字 15 -> 18、其余不变
    # （a1-mask-reuse 实验里的 fuse_tiebreak.py 的 unpin2 臂）——别按对称性顺手改
    ex = snap(ts, ocr_first)
    if ex is not None and ex <= t_start - span_us + FUSE_TOL_US:
        ex = None
    first, first_how = fuse([ex, first_rise, snap(ts, cf[0] if cf else None), first_old],
                            t_start - span_us, t_start, t_start)
    partial = [t for t, n in ocr_pts if n < FUSE_PARTIAL * N]
    full_lo = max([t_start - span_us, first] + partial)
    full_cands = [full_glyph, snap(ts, cf[1] if cf else None), snap(ts, ocr_full)]
    # 只防"晚很多"（owner 2026-09-12：晚 10 帧以内都还好；晚很多是灾难——译文迟迟打不完——
    # 早很多无非一开始就全显示）。两道上限：OCR 读全之后 LATE_CAP_US 以内；没有两路同意时，
    # 不晚于区间内最早的候选 + LATE_CAP_US。10 帧以内的判断一律不动
    full, full_how = fuse(full_cands, full_lo, min(t_full_sampled + span_us, t_full_sampled + LATE_CAP_US),
                          t_full_sampled)
    if full_how == "single":
        inside = [v for v in full_cands if v is not None
                  and full_lo - FUSE_TOL_US <= v <= t_full_sampled + span_us + FUSE_TOL_US]
        if inside and full > min(inside) + LATE_CAP_US:
            full, full_how = max(full_lo, min(inside) + LATE_CAP_US), "capped"
    # 吸附之后首字仍**不晚于 t_start**（那一刻已经读到字了，是实测的 pts）：`ts` 是按锚点折算的估计，锚点的 pts 带容器时间基的舍入
    # （yuka 是毫秒；时间网格上的采样帧不在整毫秒处时差到 ±0.5 ms），吸附会越过 t_start 零点几毫秒、产物校验拒收（2026-09-24，--fps 2.2）
    first_s = min(snap(ts, first), t_start)
    return {"first": first_s, "full": max(snap(ts, full), first_s),
            "first_how": first_how, "full_how": full_how}


# ---------- 结尾：完全显示结束 / 完全消失（2026-09-12，淡出 / 交叉淡出） ----------
#
# 和开头的"首字 / 全字"平行：没有淡出时两个时刻重合（一帧切掉），有淡出时中间就是淡出时长。
# 整框相关系数对"整体均匀变淡"不敏感（亮度归一化掉了），原神的 3–4 帧淡出上旧终点晚 28 帧；
# 这里逐字格量**字形对比度**——(字形像素 − 本帧背景) / (模板里的字形 − 模板背景)，背景亮度随帧变也不怕。

FADE_HI = 0.85
"""对比度 ≥ 这个算"完全清楚"。"""
FADE_LO = 0.15
"""对比度 < 这个算"看不见"。"""


def contrast_curves(frames: np.ndarray, inner: tuple[int, int, int, int], n_chars: int) -> list[np.ndarray]:
    """逐字格的对比度曲线（模板 = 窗口首帧，要求那时字还完整）。字形像素或背景像素太少的格不给。"""
    x0, y0, x1, y1 = inner
    tpl = frames[0]
    bright = polarity(tpl)
    cw = (x1 - x0) / max(1, n_chars)
    out = []
    for c in range(n_chars):
        a, b = int(round(x0 + c * cw)), int(round(x0 + (c + 1) * cw))
        if b - a < 2:
            continue
        u = tpl[y0:y1, a:b]
        t = u.astype(np.float32)
        u8 = u.dtype == np.uint8                     # 整数值上的中位数走直方图（见 `polarity` 上面），逐位相同
        dev = t - (_u8_median_f32(u) if u8 and u.size else np.median(t))
        m = dev >= GLYPH_DIFF if bright else dev <= -GLYPH_DIFF
        if m.sum() < CELL_MIN_PX or (~m).sum() < CELL_MIN_PX:
            continue
        denom = t[m] - (_u8_median_f32(u[~m]) if u8 else np.median(t[~m]))
        useg = frames[:, y0:y1, a:b]
        seg = useg.astype(np.float32)
        bg = _u8_rowmedian_f32(useg[:, ~m]) if u8 else np.median(seg[:, ~m], axis=1)
        out.append(np.clip(np.median((seg[:, m] - bg[:, None]) / denom[None, :], axis=1), -0.5, 1.5))
    return out


def end_keyframes(frames: np.ndarray, ts: list[int], inner: tuple[int, int, int, int],
                  text: str) -> tuple[int | None, int | None]:
    """(完全显示结束, 完全消失)，µs。

    完全显示结束 = 从窗口开头起，10% 分位字格仍 ≥ FADE_HI 的最后一帧（窗口首帧就不完整 = 给不出）；
    完全消失 = 之后第一帧 75% 分位字格 < FADE_LO——**不要求持续**：同位置很快会出下一句。
    分位数而不是全体：个别格被立绘 / 描边污染不牵连整行。消失侧原来取 90% 分位，星铁一条字消失后
    有一两格被背景亮纹撑在 0.15–0.20，整行晚判 17 帧（2026-09-12）；75% 容得下四分之一的格被污染。"""
    n = len(chars(text))
    if n < 1 or len(frames) < 3:
        return None, None
    cs = contrast_curves(frames, inner, n)
    if len(cs) < 2:
        return None, None
    C = np.stack(cs)
    q_lo, q_hi = np.quantile(C, 0.1, axis=0), np.quantile(C, 0.75, axis=0)
    a = None
    if q_lo[0] >= FADE_HI:
        a = 0
        while a + 1 < len(ts) and q_lo[a + 1] >= FADE_HI:
            a += 1
    start = a + 1 if a is not None else 0
    b = next((i for i in range(start, len(ts)) if q_hi[i] < FADE_LO), None)
    return (ts[a] if a is not None else None), (ts[b] if b is not None else None)
