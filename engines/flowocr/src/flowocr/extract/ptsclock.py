"""采样帧时间戳的唯一判据：只认解码器给的真实 PTS，且必须严格递增。

**单独放一个文件是为了让守卫够得着**——和 `flowocr.extract.ocr_complete` 同一个理由
（audit-4 C8）：判据写在 `run_ocr2.py`（当时在 paddleocr 实验目录）里的话，那个文件
import cv2 和 paddleocr，共享层的自检够不着，够不着就等于测不了。
而这条判据恰好属于**错了不会报错**的一类：一条坏时间轴不会让任何下游抛异常，
只会让所有时间都错一点。

## 为什么不能再用 `idx / src_fps`

`src_fps` 是 `CAP_PROP_FPS`，即**容器的平均帧率**。直播录像有丢帧缺口时它低于
标称的 60，于是帧计数 × (1/59.978) 在缺口**之前**就已经比真实 pts 慢，
缺口之后还要再跳一次。实测（fps-and-rec-budget 报告）：

| 素材 | `src_fps` | pts 缺口 | 管线比真实 pts 晚 |
| --- | ---: | --- | --- |
| f1 / f2 / f5 | 60.000 | 无 | 0 |
| f3 | 59.978 | 两处（5.2 s + 7.0 s） | 7.2 h 处 **9.5 s** |
| f4 | 59.987 | 一处（6.0 s） | 6.4 h 处 **5.0 s** |

旧产物**不能**乘 `src_fps / round(src_fps)` 修回来：那只消掉伸缩，
缺口之后仍缺累计丢帧时长。

## `CAP_PROP_POS_MSEC` 的语义（2026-09-09 实测）

`read()` 之后 `cap.get(CAP_PROP_POS_MSEC)` 给的是**刚读到那一帧**的 pts，
逐帧对齐 ffprobe 的 `frame=pts_time`（从 0.0 起，不是下一帧）。
"""
from __future__ import annotations

import bisect
import json
import math


class BadPts(ValueError):
    """解码器给的时间戳不可信。**不要吞掉它**——产物必须判为未完成。"""


class PtsClock:
    """按顺序喂进每个采样帧的 pts（秒），换出 `t_us`（微秒整数）。

    三道门，任何一道不过就抛 `BadPts`：非有限、为负、不严格递增。

    严格递增而不是非递减：两个采样帧拿到同一个 `t_us`，下游会把两次观测
    叠成一个时刻——这既不会报错，也数不出来。
    """

    def __init__(self) -> None:
        self.first_us: int | None = None
        self.last_us: int | None = None
        self.n: int = 0

    def push(self, t_sec: float, *, where: str = "") -> int:
        tag = f"{where}：" if where else ""
        if not math.isfinite(t_sec) or t_sec < 0:
            raise BadPts(f"{tag}解码器给的 PTS 无效（{t_sec!r}）")
        t_us = int(round(t_sec * 1_000_000))
        if self.last_us is not None and t_us <= self.last_us:
            raise BadPts(f"{tag}PTS 没有严格递增"
                         f"（{self.last_us / 1e6:.6f}s -> {t_sec:.6f}s）")
        if self.first_us is None:
            self.first_us = t_us
        self.last_us = t_us
        self.n += 1
        return t_us

    @property
    def span_sec(self) -> float:
        """覆盖到的 pts 跨度（秒）。一帧都没有时是 0。"""
        if self.first_us is None or self.last_us is None:
            return 0.0
        return (self.last_us - self.first_us) / 1e6

    def meta(self) -> dict:
        """写进 `_meta` 的那几个键。`timebase` 是下游认时间轴的唯一凭据。"""
        return {"timebase": "pts",
                "pts_first_sec": self.first_us / 1e6 if self.first_us is not None else None,
                "pts_last_sec": self.last_us / 1e6 if self.last_us is not None else None}


class IndexClock:
    """**帧号 ↔ 真实时间**的双向映射（回抠这一级用）。

    为什么需要它（2026-09-17，reuse-budget 计划第 1 条）：回抠拿 run 的时间（**真实 pts**）
    换算成帧号去开解码窗，再把窗内的帧号换算回时间写进产物——两头都用 `us_per_frame = 1e6 / src_fps`，
    而 `src_fps` 是**容器的平均帧率**。于是在丢帧缺口的素材上，窗口位置和输出时刻都带着那份偏差：
    实测 yuka f4 一个 20 分钟窗口里**一处缺口都没有**，光是"平均帧率 59.9870 vs 窗内真实 60.000"这一项，
    从头到尾就漂 **−260 ms**（≈ 15 帧）；f3 在 7.2 h 处是 **9.5 s**（本文件开头那张表）。

    做法：拿 obs 里逐采样点的 `(帧号, t_us)` 当锚点做**分段线性**插值——采样点本身就是解码器给的真实 pts，
    不用再解一遍视频；锚点之间只剩局部帧率的抖动。锚点之外（窗口首尾探出去的几十帧）退回名义帧率。

    **不给锚点时它就是原来的常量换算**（`t_of(i) = i × us_per_frame`，逐位相同），所以 CFR 素材上产物不变。
    """

    __slots__ = ("us", "f", "t", "rate")

    def __init__(self, us_per_frame: float, pairs=()) -> None:
        self.us = float(us_per_frame)
        pairs = sorted(pairs)
        self.f = [int(p[0]) for p in pairs]
        self.t = [float(p[1]) for p in pairs]
        # 每段的局部帧率（µs/帧）；只有一个锚点时全程退回名义值
        self.rate = [((self.t[i + 1] - self.t[i]) / (self.f[i + 1] - self.f[i]))
                     for i in range(len(self.f) - 1)]

    def __len__(self) -> int:
        return len(self.f)

    def t_of(self, idx: float) -> float:
        """帧号 -> 时间（µs，**浮点**：调用方按原来的口径自己 `int(round(...))`）。"""
        if not self.f:
            return idx * self.us
        k = bisect.bisect_right(self.f, idx) - 1
        if k < 0:
            return self.t[0] - (self.f[0] - idx) * self.us
        if k >= len(self.f) - 1:
            return self.t[-1] + (idx - self.f[-1]) * self.us
        return self.t[k] + (idx - self.f[k]) * self.rate[k]

    def idx_of(self, t_us: float) -> float:
        """时间（µs）-> 帧号（浮点，同上）。和 `t_of` 严格互逆。"""
        if not self.t:
            return t_us / self.us
        k = bisect.bisect_right(self.t, t_us) - 1
        if k < 0:
            return self.f[0] - (self.t[0] - t_us) / self.us
        if k >= len(self.t) - 1:
            return self.f[-1] + (t_us - self.t[-1]) / self.us
        return self.f[k] + (t_us - self.t[k]) / self.rate[k]


def index_clock_from_obs(obs_path, us_per_frame: float) -> IndexClock:
    """从 obs 的逐采样点 `(frame, t_us)` 建 `IndexClock`。只流式扫一遍、按帧号去重；
    `timebase` 不是 pts 的旧产物一律退回常量换算（那种时间轴本身就不可信，拿来归因的工具一律只认 pts 时间轴）。"""
    pairs: dict[int, int] = {}
    with open(obs_path, encoding="utf-8") as fh:
        meta = json.loads(fh.readline()).get("_meta", {})
        if meta.get("timebase") != "pts":
            return IndexClock(us_per_frame)
        for line in fh:
            i = line.find('"frame": ')
            j = line.find('"t_us": ')
            if i < 0 or j < 0:
                continue
            f = int(line[i + 9:line.find(",", i)])
            if f not in pairs:
                pairs[f] = int(line[j + 8:line.find(",", j)])
    return IndexClock(us_per_frame, pairs.items())
