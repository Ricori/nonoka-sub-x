"""取哪些帧：**时间网格**（2026-09-24，owner：三种间隔都不要求整除，"采样帧 ⊆ 高精帧环 ⊆ 辅助流"嵌套）。

帧号一律是 pts 帧号 `round(t × 名义帧率)`（`framesource.id_rate`），网格锚在帧号 0 上：

* `TimeGrid(p, q)`：帧率比 p/q（约分、≤ 1）的网格。第 k 格是帧 `ceil(k·q/p)`；第 i 帧在格上 ⇔ `floor(i·p/q)` 在这一帧变了。
  floor / ceil / 前后格 / 区间里的格都是整数闭式，不逐帧循环。**p = 1 时就是原来的"每 q 帧一帧"**（`uniform`），
  公式逐项退化成原来的整除算术——所以整除的情形和改之前逐字节相同。采样用它（`--fps`）。
* `AuxGrid(own, sample)`：辅助流 = 自己的网格（`--aux-max-fps`）∪ 采样网格（**嵌套**：采样帧一定在辅助流里）。
  回抠的边界锚在采样帧上（融合把首字夹在 [t_start − 一个间隔, t_start]），采样帧不在辅助流里边界就落不到它上面
  （09-16 那次：格子没对齐、首字系统性晚 1 帧、产物校验当场拒收）。两个都等距、而且采样步长是辅助步长的倍数时，
  并集就是自己那个网格（`uniform`），证据记录照旧只写 `step`。

ffmpeg 的 `select` 表达式和 Python 用同一组整数算 floor（`N·p/q` 的分子是精确整数、商离整数至少 1/q，双精度不会判错），
两边选出来的帧一致（守卫在 6 小时帧号内抽查）。**纯函数，守卫直接测。**
"""
from __future__ import annotations

from fractions import Fraction

FPS_DEN = 10_000
"""帧率换成有理数时分母的上限：59.94（60000/1001）、23.976（24000/1001）这类名义帧率都精确落得下。"""
CAP_TOL = 1e-3
"""辅助流上限落到整数步长的容差（相对）：60.0000x 这类名义帧率不算超（同时间网格之前的步长判据）。"""
SNAP_DEN = 100
SNAP_REL = 1e-5
"""两个帧率之比离一个**分母 ≤ `SNAP_DEN` 的简单分数**不到 `SNAP_REL`（相对）就吸附上去（2026-09-24 审计）。
为什么：用户敲的是 `29.97` / `59.94`，素材的名义帧率是 30000/1001 / 60000/1001——两边各自有理数化之后比值是 999999/2000000，
离 1/2 只差 1e-6，却被当成真不整除（不等距网格、`--fps 59.94` 直接报错、辅助流每 4.6 h 静默少一帧）。
1e-5 的相对误差 6 小时累计 0.2 s；真不整除的（59.94 上 2 fps 是 1001/30000，离 1/30 差 1e-3）不受影响。"""


class TimeGrid:
    __slots__ = ("p", "q")

    def __init__(self, p: int, q: int) -> None:
        if p < 1 or q < 1:
            raise ValueError(f"网格的帧率比要为正：{p}/{q}")
        f = min(Fraction(p, q), Fraction(1))
        self.p, self.q = f.numerator, f.denominator

    @classmethod
    def for_rate(cls, src_fps: float, fps: float) -> "TimeGrid":
        """素材 `src_fps` 上取 `fps` 帧 / 秒（比素材还高就是每帧都要）。"""
        if src_fps <= 0 or fps <= 0:
            raise ValueError(f"帧率要为正：素材 {src_fps} / 要的 {fps}")
        r = Fraction(fps).limit_denominator(FPS_DEN) / Fraction(src_fps).limit_denominator(FPS_DEN)
        s = r.limit_denominator(SNAP_DEN)
        if abs(s - r) <= SNAP_REL * r:
            r = s
        return cls(r.numerator, r.denominator)

    @classmethod
    def every(cls, step: int) -> "TimeGrid":
        """每 `step` 帧一帧（探针 / 旧产物的整数步长）。"""
        return cls(1, max(1, int(step)))

    @property
    def uniform(self) -> bool:
        return self.p == 1

    @property
    def step(self) -> int | None:
        """等距时的步长；不等距是 None。"""
        return self.q if self.p == 1 else None

    @property
    def max_gap(self) -> int:
        """相邻两格最多隔几帧（等距就是步长）。"""
        return -(-self.q // self.p)

    def fps(self, src_fps: float) -> float:
        return src_fps * self.p / self.q

    def interval_us(self, src_fps: float) -> float:
        """名义间隔（微秒）。不等距时实际间隔在它上下差不到一帧。"""
        return 1e6 * self.q / (self.p * src_fps)

    def as_list(self) -> list[int]:
        return [self.p, self.q]

    # ---------- 选帧（整数闭式）----------
    def at(self, k: int) -> int:
        """第 k 格的帧号 `ceil(k·q/p)`。"""
        return -(-k * self.q // self.p)

    def index(self, i: int) -> int:
        """不大于 i 的最近一格是第几格（`floor(i·p/q)`）。"""
        return i * self.p // self.q

    def member(self, i: int) -> bool:
        return self.at(self.index(i)) == i

    def floor(self, i: int) -> int:
        return self.at(self.index(i))

    def ceil(self, i: int) -> int:
        return self.at(self.index(i - 1) + 1)

    def prev(self, i: int) -> int:
        """严格小于 i 的最近一格。"""
        return self.floor(i - 1)

    def next(self, i: int) -> int:
        """严格大于 i 的最近一格。"""
        return self.at(self.index(i) + 1)

    def nearest(self, x: float) -> int:
        """离帧号 x（可以是小数）最近的一格（按**帧距**；一样近取前一格）——窗口起点对齐用。
        等距时就是原来的 `round(x / q) * q`（逐位保留，含 Python 的银行家舍入）。
        不等距时不能按格号四舍五入：60 fps 上 2.2 fps 的前两格是 0 / 28，x = 13.7 的格号 0.502 会舍到 28，而 0 更近（2026-09-24 Codex 审计）。"""
        if self.p == 1:
            return self.at(round(x / self.q))
        lo = self.floor(int(x // 1))
        hi = self.next(lo)
        return lo if x - lo <= hi - x else hi

    def frames(self, lo: int, hi: int) -> list[int]:
        """[lo, hi] 里的格（升序）。"""
        if hi < lo:
            return []
        if self.p == 1:
            q = self.q
            return list(range(lo + (-lo) % q, hi + 1, q))
        return [self.at(k) for k in range(self.index(lo - 1) + 1, self.index(hi) + 1)]

    def n_frames(self, lo: int, hi_excl: int) -> int:
        """[lo, hi_excl) 里最多交付几格（ffmpeg 的 `-frames:v`）。等距的照旧是 `ceil((hi − lo) / q)`（起点对齐时就是精确数）。"""
        if self.p == 1:
            return max(1, -(-(hi_excl - lo) // self.q))
        return max(1, self.index(hi_excl - 1) - self.index(lo - 1))

    def cond(self, n: str) -> str:
        """ffmpeg 表达式：帧号表达式 `n` 在格上（逗号已转义）。"""
        if self.p == 1:
            return rf"not(mod({n}\,{self.q}))"
        return rf"not(eq(floor({n}*{self.p}/{self.q})\,floor(({n}-1)*{self.p}/{self.q})))"

    def select(self, n: str) -> str:
        """`select` 的条件；空串 = 每帧都要。等距时和加时间网格之前逐字相同。"""
        return "" if self.p == 1 and self.q == 1 else self.cond(n)

    def describe(self, src_fps: float) -> str:
        if self.p == 1:
            return "每帧一帧" if self.q == 1 else f"每 {self.q} 帧一帧"
        return f"按时间网格 {self.fps(src_fps):.3f} fps（{self.p}/{self.q}）"

    def __eq__(self, other) -> bool:
        return isinstance(other, TimeGrid) and (self.p, self.q) == (other.p, other.q)

    def __hash__(self) -> int:
        return hash((self.p, self.q))

    def __repr__(self) -> str:
        return f"TimeGrid({self.p}/{self.q})"


def meta_stride(meta: dict) -> int:
    """obs `_meta` 里的**等距**采样步长。只认等距采样的探针（按"每 stride 帧一帧"数帧号的那几个）用它：
    时间网格上不等距的产物没有 `stride`，直接 `meta["stride"]` 是一个看不出原因的 KeyError（2026-09-24 审计）。"""
    s = meta.get("stride")
    if not s:
        raise ValueError(f"这份 obs 的采样不等距（`_meta.sample_grid` = {meta.get('sample_grid')}），这个探针只认等距采样"
                         f"（每 stride 帧一帧）；用整除素材帧率的 --fps 重跑一份")
    return int(s)


class AuxGrid:
    """辅助流：自己的网格 ∪ 采样网格（嵌套）。"""
    __slots__ = ("own", "sample")

    def __init__(self, own: TimeGrid, sample: TimeGrid) -> None:
        self.own, self.sample = own, sample

    @classmethod
    def for_video(cls, src_fps: float, aux_max_fps: float, sample: TimeGrid) -> "AuxGrid":
        """辅助流帧率 = min(上限, 素材帧率)。上限**不超过它 `CAP_TOL`** 就落得到某个整数步长（素材帧率 / k）时取那个步长：
        它是上限、不是目标值——119.88 fps 的素材给 60 就该隔帧（59.94 fps），不该为多出的 0.1% 走不等距网格（2026-09-24 Fable 复审）。"""
        k = max(1, round(src_fps / aux_max_fps))
        if src_fps / k <= aux_max_fps * (1 + CAP_TOL):
            return cls(TimeGrid.every(k), sample)
        return cls(TimeGrid.for_rate(src_fps, aux_max_fps), sample)

    @classmethod
    def from_list(cls, g: list[int]) -> "AuxGrid":
        """`as_list()` 的逆：`[p, q, 采样 p, 采样 q]`（跨进程 / 进 `_meta` / 证据记录的写法）。"""
        return cls(TimeGrid(g[0], g[1]), TimeGrid(g[2], g[3]))

    @classmethod
    def from_spec(cls, rec: dict) -> "AuxGrid":
        """证据记录里的写法 -> 网格：`grid` 或等距的 `step`（等距时并集就是自己的网格，采样网格取它自己）。"""
        if rec.get("grid"):
            return cls.from_list(rec["grid"])
        g = TimeGrid.every(rec.get("step") or 1)
        return cls(g, g)

    def as_list(self) -> list[int]:
        return self.own.as_list() + self.sample.as_list()

    @property
    def uniform(self) -> bool:
        """并集退化成自己那个等距网格：自己每帧都要（什么都盖得住），或两个都等距、采样步长是辅助步长的倍数。"""
        return self.own.p == 1 and (self.own.q == 1 or (self.sample.p == 1 and self.sample.q % self.own.q == 0))

    @property
    def step(self) -> int | None:
        return self.own.q if self.uniform else None

    def key(self) -> tuple:
        """复用判据比的规范形。等距的并集按步长比；不等距的按两个网格比——**帧集合相同、写法不同的偶尔会不相等**
        （如 2/3 ∪ 1/3 和 2/3 ∪ 2/3），后果只是多重建一次，不会把不同的帧集合判成相同（2026-09-24 审计把原来"帧集合相同就相等"改准）。"""
        return ("step", self.own.q) if self.uniform else tuple(self.as_list())

    def spec(self) -> dict:
        """写进证据记录的样子：等距的照旧只写 `step`（和旧产物逐字节相同），不等距的写 `grid`。"""
        return {"step": self.own.q} if self.uniform else {"grid": self.as_list()}

    def member(self, i: int) -> bool:
        return self.own.member(i) or self.sample.member(i)

    def floor(self, i: int) -> int:
        return self.own.floor(i) if self.uniform else max(self.own.floor(i), self.sample.floor(i))

    def ceil(self, i: int) -> int:
        return self.own.ceil(i) if self.uniform else min(self.own.ceil(i), self.sample.ceil(i))

    def prev(self, i: int) -> int:
        return self.floor(i - 1)

    def frames(self, lo: int, hi: int) -> list[int]:
        if self.uniform:
            return self.own.frames(lo, hi)
        return sorted(set(self.own.frames(lo, hi)) | set(self.sample.frames(lo, hi)))

    def n_frames(self, lo: int, hi_excl: int) -> int:
        """[lo, hi_excl) 里辅路最多交付几帧（`-frames:v`、`AuxReader` 读几帧）。"""
        if self.uniform:
            return self.own.n_frames(lo, hi_excl)
        return max(1, len(self.frames(lo, hi_excl - 1)))

    def cover_floor(self, last_idx: int) -> int:
        """辅助流交付到的最后一帧至少要到这里，才算盖住了采样流的最后一帧（判"辅助流半路断了"）。
        等距的照旧留 `q − 1` 帧余量；不等距的：采样帧一定在辅助流里（嵌套），要盖到不大于它的最近一个入选帧。"""
        if self.uniform:
            return last_idx - (self.own.q - 1)
        return self.floor(last_idx)

    def select(self, n: str) -> str:
        """ffmpeg `select` 的条件；空串 = 每帧都要。等距时和加时间网格之前逐字相同。"""
        if self.uniform:
            return self.own.select(n)
        return f"({self.sample.cond(n)}+{self.own.cond(n)})"

    def describe(self, src_fps: float) -> str:
        if self.uniform:
            return self.own.describe(src_fps)
        return f"按时间网格 {self.own.fps(src_fps):.3f} fps（{self.own.p}/{self.own.q}，含采样帧）"

    def __eq__(self, other) -> bool:
        return isinstance(other, AuxGrid) and self.key() == other.key()

    def __hash__(self) -> int:
        return hash(self.key())

    def __repr__(self) -> str:
        return f"AuxGrid({self.own} ∪ 采样 {self.sample})"
