"""回显判定：同一句剧本行被认领了几次时，定一次"主"，其余标出它是什么（matcher-next 计划第 1 节，owner 2026-09-17 / 10-06）。

匹配器之后、导出之前的一遍，内置两个匹配器（`gametext`、`naninovel`）共用 `mark`。**只读**认领记录、剧本锚点和 tracks 给的
逐 cue 属性（来源区域、在不在主轨、是不是面板样），**不改认领本身**：每条记录和它的每条 `extra` 只多两个字段
`echo`（`matchedio.ECHOES` 之一，主和只认领一次的行是 None）和 `echo_of`（主那次认领：`[记录下标, slot]`，slot −1 是那条记录的 `ref`、
≥0 是它的第几条 `extra`；同样主是 None）。主可能落在别的记录的 `extra` 上，下游（`matched_srt` 只出 `ref`）要分得出来。

* **聚成"出现"**：同一条剧本行（变体组并到代表行）的认领按时段聚，时段重叠或相隔 ≤ `NEAR` 秒的算同一次出现，不分区域。
* **定主**：离这一行的剧本锚点时刻最近的那次出现（没有锚点时取最早的那次）。⚠ 锚点是 `gamescript.sequence` 的排序时刻，
  即这一行在 obs 里**第一次被检索到**的时刻（全部 obs、不分区域），所以实际几乎总等于最早那次出现，只有最早那次没进 obs 检索时才不同；
  对话记录先于正文上屏时，主会落在对话记录上（10-06 量过四场只有零星几行、没有一行是对话记录）；匹配器认得出的**真记录面板**（`log_panel`，见下）
  不参与定主，这一类已经排掉，剩下的是认不出的面板。那次出现里多个认领时，在主轨上的 > 不是面板样的 > 分数高的 > 早的。
* **真记录面板**（`log_panel`，检测器三场修复计划 U3）：匹配器给的 cue 属性，面板样、而且里面还有别的台词的字（`scriptmatch.log_panels`）。
  一行有不是 `log_panel` 的认领时，只拿那些串出现、定主，`log_panel` 的认领一律判 `log`：夹在中间的记录面板不会把前后两次显示串成一次出现
  （后一次同一区域判 `replay`），记录面板也不会因为离台词框不到 `NEAR` 秒被判成同一次出现的 `same_time` / `other`、在 SRT 里多出一条。
  一行的认领全是 `log_panel` 的照下面的规则。`naninovel` 不给这个属性（恒为假），判定和原来逐项相同。
* **其余标形态**：主那次出现里另一区域的认领 → `same_time`（同一区域、和主相接的不算，落到 `other`）；别的出现里，按顺序：
  认领的 cue 面板样 → `log`；和主同一区域 → `replay`；另一区域、这一刻（起点 ± `LOG_WINDOW` 秒，有采样网格时按格号，见 `classify`）各区域一共认领了 ≥ `LOG_BATCH` 条别的**旧行**
  （那一行这次认领比它最早一次认领晚 > `LOG_OLD` 秒）→ `log`；剩下的 → `other`（报警留给人看）。
  "成批旧行"只判另一区域的（2026-10-06 定）：SRT 只去掉确定是回显的，宁可多一条重复也不删真台词——同区域的再现统一叫重播
  （原神 NPC 重复问候时选项菜单同刻重现、顺序剧本连续快速回看，都是同区域的真重演赶上了成批旧行）。

SRT 一类预设默认不出 `same_time` / `log`（`matchedio.SRT_DROP_ECHOES`），叠加照画。
"""
from __future__ import annotations

import bisect
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable

from flowocr.artifacts import matchedio
from flowocr.extract.framegrid import TimeGrid

NEAR = 2.0
"""同一次出现：同一行的两次认领时段重叠或相隔不超过这么多秒（同 `scriptmatch.MERGE_GAP`）。"""
LOG_WINDOW = 1.0
"""记录面板"这一刻"的半宽（秒）：别的认领的起点离这次认领的起点不超过这么多；有采样网格时折成格数、按格号比（`classify`）。"""
LOG_OLD = 10.0
"""旧行：那一行的这次认领比它最早的一次认领晚这么多秒以上。"""
LOG_BATCH = 2
"""这一刻至少认领了这么多条别的旧行，就是在翻对话记录（hsr 923 s、1461 s 一打开记录，十几句旧台词同时被认领一遍）。"""


@dataclass(frozen=True)
class Claim:
    """一次认领：记录 `rec` 的主认领（`slot == -1`）或它第 `slot` 条 `extra`。"""
    rec: int
    slot: int
    rep: str
    start: float
    end: float
    source: str
    main: bool
    panel: bool
    score: float
    log_panel: bool = False


def anchors(sequence: list, rep: Callable[[str], str]) -> dict[str, float]:
    """剧本的对齐序列（`gamescript` 的 `sequence`：`[key, 排序时刻, 时段]`，排序时刻是这一行从 obs 定的第一次上屏）-> 代表行的锚点时刻。
    变体组取代表行自己的；代表行不在序列里时取组里最早的。"""
    own: dict[str, float] = {}
    member: dict[str, float] = {}
    for k, t, *_ in sequence:
        r = rep(k)
        if k == r:
            own[r] = t
        else:
            member[r] = min(t, member.get(r, t))
    return {**member, **own}


def claims_of(recs: list[dict], source_of: list[str], main_of: list[bool], panel_of: list[bool],
              rep: Callable[[str], str], log_panel_of: list[bool] | None = None) -> list[Claim]:
    """记录里的全部认领。一条记录的几条 cue 来自同一区域（`merge_runs` 按区域并）；在主轨上 / 面板样 / 真记录面板按记录里任一条 cue 算
    （`merge_runs` 不把真记录面板和别的 cue 并在一起，所以真记录面板的记录里全是真记录面板）。"""
    out = []
    for i, r in enumerate(recs):
        cs = r["cues"]
        src, main, panel = source_of[cs[0]], any(main_of[c] for c in cs), any(panel_of[c] for c in cs)
        log_panel = bool(log_panel_of) and any(log_panel_of[c] for c in cs)
        for slot, x in ([(-1, r["ref"])] if r.get("ref") else []) + list(enumerate(r.get("extra") or ())):
            out.append(Claim(i, slot, rep(x["key"]), r["start"], r["end"], src, main, panel, x.get("score") or 0.0, log_panel))
    return out


def occurrences(cs: list[Claim]) -> list[list[Claim]]:
    """同一行的认领按时段聚成出现（重叠或相隔 ≤ `NEAR`），按时间排。"""
    out: list[list[Claim]] = []
    end = None
    for c in sorted(cs, key=lambda c: (c.start, c.end, c.rec, c.slot)):
        if out and c.start <= end + NEAR:
            out[-1].append(c)
            end = max(end, c.end)
        else:
            out.append([c])
            end = c.end
    return out


Grid = tuple[Callable[[float], int], int]
"""(起点秒 -> 它被采到的那一格的格号, `LOG_WINDOW` 折成几格)，`grid_of` 给，"这一刻"按格号比。"""


def grid_of(doc: dict) -> Grid | None:
    """tracks 的采样网格（`Grid`）：网格取 obs 带进 `meta` 的 `sample_grid`，旧产物退到等距的 `stride`，再退到 `src_fps` / `sample_fps`；
    都没有（只有 SRT 的、很旧的产物）返回 None，"这一刻"照原来按秒比。
    格号是起点所在帧往后取的最近一格：采样级的起点就在格上；回抠过的起点落在它被采到的那一格之前不到一个间隔（融合把首字夹在
    [采样起点 − 一个间隔, 采样起点]），往后取回到的正是那一格。
    两处会差一格：融合把首字恰好放在下界（前一格那一帧，gi1 事件 2680 从 673.5 s 回抠到 673.0 s）时还原成前一格——那是融合估计字在那一帧已经出现（估计，不是逐帧证实），按估计的出现时刻算前一格也说得通；
    时刻按名义帧率换成帧号（`round(t × src_fps)`，同网格的帧号约定），pts 和名义帧率差出半帧以上的素材上落在边界的起点也会差一格。
    要分毫不差得在产物里留回抠前的采样起点，没做：前一种按估计的出现时刻归格本身说得通，后一种要素材的时钟本来就偏。"""
    m = doc.get("meta") or {}
    fps = m.get("src_fps")
    if not fps:
        return None
    if m.get("sample_grid"):
        g = TimeGrid(*m["sample_grid"])
    elif m.get("stride"):
        g = TimeGrid.every(int(m["stride"]))
    elif m.get("sample_fps"):
        g = TimeGrid.for_rate(float(fps), float(m["sample_fps"]))
    else:
        return None
    fps = float(fps)
    return (lambda t: g.index(g.ceil(round(t * fps)))), round(LOG_WINDOW * 1e6 / g.interval_us(fps))


def in_window(keys: list, x, half) -> tuple[int, int]:
    """`keys`（升序）里落在 [x − half, x + half] 的下标区间 [lo, hi)。"""
    return bisect.bisect_left(keys, x - half), bisect.bisect_right(keys, x + half)


def classify(claims: list[Claim], anchor: dict[str, float] | None,
             grid: Grid | None = None) -> dict[tuple[int, int], tuple[str, tuple[int, int]]]:
    """(记录, slot) -> (形态, 主的 (记录, slot))；只认领一次的行和主不在里面。`anchor` 是 None 时每行取最早那次出现当主。

    `grid`（`grid_of`）给了时"这一刻"按**采样格号**比：差不超过 `LOG_WINDOW` 折成的那几格。窗口量的是"同一次面板打开"，不该被起点精度左右：
    回抠把起点挪到帧级后，同一次打开的面板里各行差的不再是整数个间隔（hsr 6378.0 s 打开的记录面板，邻行的起点从 6377.0 s 挪到 6376.7 s，
    差 1.28 s，按秒比 ±1 s 数不到、整批旧行判成 other）；按秒放宽又分不开（25 fps 按 2 fps 采时间隔 12 / 13 帧交替，回抠后差两格的可以差 1.48 s，
    采样级差三格的最近也只差 1.48 s）。格号把两者都还原到采样网格上。没有 `grid` 时照原来按秒比（± `LOG_WINDOW`）。"""
    by_rep: dict[str, list[Claim]] = defaultdict(list)
    for c in claims:
        by_rep[c.rep].append(c)
    first = {k: min(c.start for c in cs) for k, cs in by_rep.items()}
    order = sorted(claims, key=lambda c: c.start)
    slot, half = grid if grid else ((lambda t: t), LOG_WINDOW)
    keys = [slot(c.start) for c in order]                 # 格号随起点不减，仍是升序

    def old_batch(c: Claim) -> int:
        lo, hi = in_window(keys, slot(c.start), half)
        return len({d.rep for d in order[lo:hi] if d.rep != c.rep and d.start - first[d.rep] > LOG_OLD})

    out: dict[tuple[int, int], tuple[str, tuple[int, int]]] = {}
    for k, cs in by_rep.items():
        if len(cs) < 2:
            continue
        logs = [c for c in cs if c.log_panel]
        if logs and len(logs) < len(cs):               # 真记录面板不参与串出现、定主，一律判 log（文件头）
            cs = [c for c in cs if not c.log_panel]
        else:
            logs = []
        occ = occurrences(cs)
        t = anchor.get(k) if anchor is not None else None
        if t is None:
            prim_occ = occ[0]
        else:
            prim_occ = min(occ, key=lambda o: (max(0.0, o[0].start - t, t - max(c.end for c in o)), o[0].start))
        prim = min(prim_occ, key=lambda c: (not c.main, c.panel, -c.score, c.start, c.rec, c.slot))
        for o in occ:
            for c in o:
                if c is prim:
                    continue
                if o is prim_occ:
                    # 同一次出现里**另一区域**的才是同时回显；同一区域、和主相接的（被一闪而过的别的 cue 隔开、`merge_runs` 没并上的同一次显示）
                    # 方案的四条都不沾（不是别的出现、也没隔开），落到 other：SRT 照出（去掉的话这一句的时段只剩主那一截），报警里看得见
                    form = "same_time" if c.source != prim.source else "other"
                elif c.panel:
                    form = "log"
                elif c.source == prim.source:
                    form = "replay"
                elif old_batch(c) >= LOG_BATCH:
                    form = "log"
                else:
                    form = "other"
                out[(c.rec, c.slot)] = (form, (prim.rec, prim.slot))
        for c in logs:
            out[(c.rec, c.slot)] = ("log", (prim.rec, prim.slot))
    return out


def mark(recs: list[dict], source_of: list[str], main_of: list[bool], panel_of: list[bool],
         anchor: dict[str, float] | None, rep: Callable[[str], str] = lambda k: k, log_panel_of: list[bool] | None = None,
         grid: Grid | None = None) -> None:
    """就地给每条记录和它的每条 `extra` 写 `echo` / `echo_of`（见文件头）。认领、时间、文本都不动。
    `source_of` / `main_of` / `panel_of` / `log_panel_of` 按喂入 cue 的下标（记录的 `cues`）：来源区域、在不在主轨上、是不是面板样、是不是真记录面板（不给就都不是）。
    `grid`：tracks 的采样网格（`grid_of`）；只有 SRT、不知道时 None（`classify`）。"""
    got = classify(claims_of(recs, source_of, main_of, panel_of, rep, log_panel_of), anchor, grid)
    for i, r in enumerate(recs):
        for slot, x in [(-1, r)] + list(enumerate(r.get("extra") or ())):
            form, of = got.get((i, slot), (None, None))
            x["echo"], x["echo_of"] = form, (list(of) if of else None)


def counts(recs: list[dict], keys: set[str] | None = None, rep: Callable[[str], str] = lambda k: k) -> dict[str, int]:
    """多出来的认领按形态数（`matchedio.ECHOES` 各一格，从产物的 `echo` 字段数）：每行除主之外的每次认领正好一个形态，
    所以几格之和 = 重复认领多出来的次数（同 `stats.repeat_extra` 的口径，按代表行）。`keys` 给了就只数代表行在里面的（带内）。"""
    n = dict.fromkeys(matchedio.ECHOES, 0)
    for r in recs:
        for x in ([r["ref"]] if r.get("ref") else []) + list(r.get("extra") or ()):
            e = r.get("echo") if x is r.get("ref") else x.get("echo")
            if e and (keys is None or rep(x["key"]) in keys):
                n[e] += 1
    return n
