"""字幕文件的**唯一**读写口径。

存在的理由（methodology-audit 报告）：在此之前，`build_tracks` /
`compare_timing` / `extra_entries` / `refine_boundaries` / `script_align`
**各写各的解析**，时间戳正则两种写法、拼行分隔符三种（`" / "` / `" "` / 换行），
而"一条 = 一个 SRT cue"这条口径**没有任何一处代码强制**——
于是它在 shell 里被 `grep -c '^[0-9]*$'` 破坏了 30 小时没人发现
（`*` 允许零个数字，空行分隔符全被算成一条，数出来约两倍）。

审计的结论是：**加固共享层，不要逐个工具打补丁**——
唯一被机制化过的那条 guardrail 在它那个工具上就真的止住了，
但同类错误迁移到了没加固的工具。这个模块就是那个共享层。

三条硬规矩：

1. **`count_cues()` 是"条数"的唯一定义**：SRT 数时间戳行，ASS 数 `Dialogue:` 行。
   任何别的数法（数序号行、数空行、`grep -c '^[0-9]*$'`）都不作数。
2. **解析结果与 `count_cues()` 必须一致**，不一致就抛——静默跳过畸形块
   正是上一次口径事故的形状。要宽容得显式传 `strict=False`。
3. **`Cue` 只存行，不存拼好的字符串**。拼行的分隔符由调用方指定，
   因为它会直接改变文本相似度的读数。默认 `" / "` 只是多数派，不是真理。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

SRT_TS = re.compile(r"(\d\d):(\d\d):(\d\d),(\d\d\d) --> (\d\d):(\d\d):(\d\d),(\d\d\d)")
SRT_TS_HEAD = re.compile(r"(\d\d):(\d\d):(\d\d),(\d\d\d) --> ")


class CueCountMismatch(RuntimeError):
    """解析出的条数与权威计数对不上——文件不是我们以为的样子，不要继续算。"""


@dataclass(frozen=True)
class Cue:
    start: float                       # 秒
    end: float
    lines: tuple[str, ...]
    source: str = ""

    def text(self, sep: str = " / ") -> str:
        """拼成一行。**分隔符必须由调用方决定**：它会改变文本相似度读数，
        不同工具历史上用过 `" / "` / `" "` / 换行，混用会让两份报告不可比。"""
        return sep.join(self.lines)

    @property
    def dur(self) -> float:
        return self.end - self.start


def count_cues(path: str | Path) -> int:
    """**"条数"的唯一权威定义。** SRT 数时间戳行，ASS 数 Dialogue 行。

    等价的 shell 写法（文档里报数就用这个）：
        grep -c -- '-->' foo.srt
        grep -c '^Dialogue:' foo.ass

    已知的边界：**正文里如果出现 `-->`，这里会多数一条**，于是 `_check()` 抛
    `CueCountMismatch`——对合法文件是误报。选择这么做是因为反过来更糟：
    静默少算正是上一次口径事故的形状。真遇到了就显式 `strict=False`，
    但**先确认那条 `-->` 是不是 OCR 把箭头图标读出来的**。
    """
    p = Path(path)
    txt = p.read_text(encoding="utf-8-sig", errors="replace")
    if p.suffix.lower() == ".ass":
        return sum(1 for ln in txt.splitlines() if ln.startswith("Dialogue:"))
    return sum(1 for ln in txt.splitlines() if "-->" in ln)


def _parse_ass_time(x: str) -> float:
    h, m, rest = x.split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


def read_ass(path: str | Path, *, strict: bool = True) -> list[Cue]:
    p = Path(path)
    out: list[Cue] = []
    for ln in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if not ln.startswith("Dialogue:"):
            continue
        f = ln.split(",", 9)
        if len(f) < 10:
            continue
        body = f[9].strip()
        # `\N` 是 ass 的换行，它就是"这条里有几行"；别在这里拼掉
        out.append(Cue(_parse_ass_time(f[1]), _parse_ass_time(f[2]),
                       tuple(body.split(r"\N")), p.name))
    _check(p, len(out), strict)
    return out


def read_srt(path: str | Path, *, sort: bool = False, strict: bool = True) -> list[Cue]:
    p = Path(path)
    out: list[Cue] = []
    for block in p.read_text(encoding="utf-8-sig", errors="replace").strip().split("\n\n"):
        m = SRT_TS.search(block)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        lines = block.splitlines()
        out.append(Cue(g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000,
                       g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000,
                       tuple(lines[2:]), p.name))
    _check(p, len(out), strict)
    return sorted(out, key=lambda c: (c.start, c.end)) if sort else out


def read_subs(paths, *, sort_by_time: bool = False, strict: bool = True) -> list[Cue]:
    """按给定顺序读多个 .srt/.ass，首尾相接。

    `sort_by_time` 的取舍是**语义的，不是风格的**：多个文件是"一次通关的先后"
    （各自从 0 计时）就不能重排；是"同一段视频的多条轨"（共享时间轴）就必须重排。
    猜错了不报错，只出一堆假数——所以这个参数没有安全默认值，调用方必须想清楚。
    """
    out: list[Cue] = []
    for p in [Path(x) for x in paths]:
        out += read_ass(p, strict=strict) if p.suffix.lower() == ".ass" \
            else read_srt(p, strict=strict)
    if sort_by_time:
        out.sort(key=lambda c: c.start)
    return out


def _check(p: Path, parsed: int, strict: bool) -> None:
    n = count_cues(p)
    if parsed == n:
        return
    msg = (f"{p.name}: 解析出 {parsed} 条，但权威计数是 {n} 条。"
           f"文件里有畸形块，或者它不是这个格式——不要拿这份数据继续算。"
           f"确实要宽容就显式传 strict=False。")
    if strict:
        raise CueCountMismatch(msg)
    print(f"[warn] {msg}", flush=True)


# ---------- 写 ----------

def fmt_ts_us(us: int) -> str:
    ms = us // 1000
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fmt_ts(seconds: float) -> str:
    return fmt_ts_us(int(round(seconds * 1_000_000)))


def write_srt_blocks(path: str | Path, blocks) -> int:
    """blocks: 可迭代的 (start_us, end_us, [行...])。返回**写了几条**。

    返回条数是故意的：调用方应当把它打印出来，
    这样文档里的数可以直接和 `count_cues()` 对上（boundary-refinement 报告）。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with p.open("w", encoding="utf-8") as fh:
        for n, (s, e, lines) in enumerate(blocks, 1):
            body = "\n".join(lines)
            fh.write(f"{n}\n{fmt_ts_us(s)} --> {fmt_ts_us(e)}\n{body}\n\n")
    return n
