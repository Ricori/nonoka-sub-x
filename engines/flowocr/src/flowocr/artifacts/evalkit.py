"""评测工具的统一自检。

存在的理由（methodology-audit 报告）：52 个 commit 里 22 个在推翻自己，
其中**判据/口径错 7 次**，是占比最大的一类。而当时项目规则里的 6 条 guardrail
只有 1 条被机制化——`compare_timing` 加了"配上 0 条非零退出 / 永远打印分母 /
样本不足不报分位"之后，**在那个工具上再没出过口径事故**；
但同类错误随即迁移到了没加固的 `script_align` 的归因判据上。

所以：把那一条真正管用的做法**抽出来，让所有评测工具都用**，
而不是等下一个工具被烧到再打一次补丁。

三条：

1. **永远打印分母。** "误差中位 0.1 s"没有意义，"配上 351/440 条，误差中位 0.1 s"才有。
   踩过：回抠前后配上 6 条 / 5 条，两行中位数根本不是同一批。
2. **样本不足不报分位。** 10 条里配上 6 条时，"P90" 只是第 5 大那个值。
3. **配上 0 条要非零退出。** 踩过：VideoSubFinder 的时间轴正文写的是时长不是台词，
   10 条参考配上 0 条，脚本只打一行汇总、退出码 0，假数就这么流进了文档。
"""
from __future__ import annotations

import sys

MIN_PCTL_SAMPLES = 20
"""样本少于这么多就不报分位数。"""


_PUNCT_CHARS = set("…。、！？!?-―ー.（）()「」『』ッっ・,，:：;；'\"’“”　")


def content_len(text: str) -> int:
    r"""**内容字数**：去掉 ASS 换行、空白和标点之后还剩几个字。

    和"字符数"不是一回事：`…………。` 有 6 个字符、**0 个内容字**。
    历史上的行长分档用的是字符数，所以 `……！` 落在"3-4 字"那一档里，
    看着像"短句更容易漏"，其实那一档根本没有内容字（methodology-audit-2 报告）。
    """
    s = text.replace(chr(92) + "N", "")
    return sum(1 for ch in s if ch not in _PUNCT_CHARS and not ch.isspace())


def is_punct_only(text: str) -> bool:
    """去掉标点/长音/促音之后一个内容字都不剩。"""
    return content_len(text) == 0


TRIVIAL_MAX = 2
"""内容字 <= 这么多就算 trivial（owner 2026-09-06：这一类要单独分出来）。"""

CLASSES = ("纯标点", "trivial(1-2字)", "有内容(>=3字)")


def triviality(text: str) -> str:
    """把一条剧本行分到三档。**分类不是装饰，是归因和取舍的分界线**：

    * **纯标点**（`……！` / `――` / `…………。`）——病因是 det 没出框；
      owner 2026-09-06 定：**不作强求**，有原文时靠上下文 momentum 对齐。
    * **trivial 1-2 字**（`はい。` / `え？`）——判别力同样接近于零，
      锚不锚得住基本取决于上下文，和长句不是一回事。
    * **有内容 >=3 字**——这一档才是"该抓到而没抓到"，五部实测只漏 0.7-1.4%，
      也是唯一值得盯着优化的一档。

    混在一个"漏条率"里报，会把结论带反（methodology-audit-2 报告）。
    """
    n = content_len(text)
    if n == 0:
        return CLASSES[0]
    return CLASSES[1] if n <= TRIVIAL_MAX else CLASSES[2]


def median(xs: list[float]) -> float:
    """偶数取**两个中间数的平均**。

    踩过：`sorted(x)[n//2]` 在 n=4 时取的是**偏大**的那一个，
    n=4 的头对头就这么报出了一个偏乐观的中位（methodology-audit-2 报告）。
    """
    if not xs:
        raise ValueError("空列表没有中位数")
    v = sorted(xs)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def denom(n: int, total: int, *, pct: bool = True) -> str:
    """`n/total = xx.x%`。**任何比例都必须带着分母出现。**"""
    if total <= 0:
        return f"{n}/0"
    return f"{n}/{total} = {n/total:.1%}" if pct else f"{n}/{total}"


def pctl(vals: list[float], q: float) -> float | None:
    """样本不足返回 None，让调用方没法不小心把它当成数印出去。"""
    if len(vals) < MIN_PCTL_SAMPLES:
        return None
    v = sorted(vals)
    return v[min(len(v) - 1, int(q * len(v)))]


def fmt_pctl(vals: list[float], q: float, *, unit: str = "s", nd: int = 3) -> str:
    v = pctl(vals, q)
    if v is None:
        return f"—（n={len(vals)}<{MIN_PCTL_SAMPLES}）"
    return f"{v:.{nd}f}{unit}"


def require_nonzero(n: int, what: str, hint: str = "") -> None:
    """配上 0 条不是"结果是 0"，是"判据用错了"。非零退出，别让它静默流下去。"""
    if n > 0:
        return
    msg = f"[判据自检] {what} 为 0 —— 这几乎一定是判据/输入不对，不是真的没有。"
    if hint:
        msg += f" {hint}"
    print(msg, file=sys.stderr, flush=True)
    raise SystemExit(2)


def warn_pair_shift(a_n: int, b_n: int, what: str = "配上的条目") -> None:
    """A/B 两侧配上的条目数不同时提醒：两行中位数不是同一批，不能直接比。"""
    if a_n == b_n:
        return
    print(f"[判据自检] 两侧{what}数不同（{a_n} vs {b_n}）——"
          f"两边的统计量**不是同一批条目**算出来的，直接横向比会得出假结论。"
          f"要可比就取交集（`--common-with`）。", file=sys.stderr, flush=True)
