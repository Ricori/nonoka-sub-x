"""把一份字幕对回游戏原文剧本，量"剧本里的话有多少条被抓到了"。

为什么需要它（script-corpus 报告）：这批语料**没有人工定稿**，唯一权威的文本是
游戏剧本本身。所以"准不准"不能按 CER 算，只能问：**剧本里该上屏的那些行，
有多少条在字幕里出现了**。这正是 owner 定的准确性重心（漏条 / 幽灵 / 切分 / 时间）。

## 三件必须先分开的事，否则"漏条率"是假的

owner 2026-09-04：「几乎全部覆盖。可能有些游戏分支没走到，另外剧本里的一些条目
可能不是对话所以不会被 ocr。」——所以"剧本行没有对应字幕"有三个原因，只有第三个是缺陷：

1. **不是对话**。Naninovel 的 `# key` 直接给出类型：`_Narrative###`（旁白）、
   `_<角色名>###`（台词）是会上屏到字幕带的；`_Choice###`（选项）、`_Toast###`、
   `_Return###` 以及 `@choice` / `@toast` 这些命令条目走的是别的 UI。默认只统计前两类。
2. **分支没走到**。表现是**连续一整段**都没命中。孤立的一两条落在已命中的上下文里，
   才像真漏。所以报的时候按"未命中连续段的长度"分档，不合成一个数。
3. **真漏**。剩下的才是。

## 对齐怎么做

剧本是**有序**的，字幕也是有序的，所以不做全局最优匹配，而是走游标：
每条字幕在游标前方的窗口里找，找不到再全局找最近的一个（分支跳转）。
这与 `dev_tools/reference/match.py` 的锚点思路一致，但这里**只做统计、不改文本**。

用法：
    python -m flowocr.analyze.script_align --script data/corpus/mocai/script-raw/Scripts \
        --text data/corpus/mocai/script-jp/Scripts \
        --subs data/corpus/mocai/game_text/lower1_jp.ass ... \
        --out out/yuka/align-lower.json
"""
from __future__ import annotations

import argparse
import json
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from flowocr.artifacts import evalkit          # noqa: E402
from flowocr.artifacts import srtio            # noqa: E402

# 会上屏到字幕带的条目类型。`Choice`/`Toast`/`Return`/`Unknown` 走别的 UI，默认不计。
DIALOGUE_KINDS_DEFAULT = "Narrative,CHARACTER"
"""CHARACTER 是占位：凡是 key 后缀不在已知非对话表里的，都当成角色名（台词）。"""
NON_DIALOGUE_KINDS = {"Choice", "Toast", "Return", "Unknown"}
NON_DIALOGUE_CMDS = {"@choice", "@toast", "@printDebate", "@print", "@char"}
"""`@printDebate` 是被实测逼出来的：它有 1999 条，**命中率 0%**——不是漏，
是反驳（法庭辩论）那一屏走的是另一套 UI，根本不出现在字幕带里。
把它算进分母，字幕带的"漏条率"会凭空多 6 个百分点。

⚠ **用途 1（画面所有文字）要把这些加回来**：它们确实在屏幕上，只是不在字幕带。
这个开关就是两个用途口径不同的地方，`--include-cmds` 可以放回来。"""

KEY_RE = re.compile(r"^#\s*(.+?)\s*$")
CMD_RE = re.compile(r"^;\s*>\s*(@\w+)")
KIND_RE = re.compile(r"_([A-Za-z]+?)_?\d*$")


@dataclass
class Entry:
    key: str
    kind: str
    cmd: str
    text: str
    src: str
    matched: int = -1          # 对上的字幕下标，-1 = 没对上
    sim: float = 0.0
    status: str | None = None  # 游戏剧本行在这段视频里的处境（`gamescript.STATUSES`）；顺序剧本没有


ASS_TAG = re.compile(r"{[^}]*}")


def norm(t: str) -> str:
    r"""归一到可比形态。与 paddleocr 实验里的 second_opinion.py 同一个理由：
    括号字形、全半角这类装饰性差异不是"对不上"。

    **必须先剥掉 ASS 样式标签**：`pre_process.py` 把游戏的 `<color>` / `<b>` 转成了
    `{\c&Hff8e9c&}{\b1}…{\r}`，这些标签留在剧本文本里。老算法的 .ass 是把剧本原文
    整条抄过去的，标签也一起抄，所以它自己跟自己精确相等；我们的 OCR 读到的是屏幕上的
    纯文字，永远配不上。不剥标签，这个对比就是偏向基线的。"""
    t = ASS_TAG.sub("", t)
    t = unicodedata.normalize("NFKC", t)
    t = t.replace("\\N", "").replace("\\n", "")
    return "".join(t.split())


def load_script(raw_root: Path, text_root: Path) -> list[Entry]:
    """raw 提供 key/类型，text 提供已经去过标签的干净文本。

    两边**逐条对应**（raw 的 `# key` 条目数 == text 的非空行数），
    不对应就直接报错——静默错位会让后面所有数都错。
    """
    out: list[Entry] = []
    for f in sorted(raw_root.rglob("*.bytes")):
        keys, cmds = [], []
        cur_cmd = ""
        for line in f.read_text(encoding="utf-8").splitlines():
            m = KEY_RE.match(line)
            if m:
                keys.append(m.group(1))
                cmds.append("")
                cur_cmd = ""
                continue
            c = CMD_RE.match(line)
            if c and keys:
                cur_cmd = c.group(1)
                cmds[-1] = cur_cmd
        if not keys:
            continue
        g = text_root / f.relative_to(raw_root)
        if not g.exists():
            raise SystemExit(f"{f} 有 {len(keys)} 个条目，但 {g} 不存在")
        lines = [l for l in g.read_text(encoding="utf-8").splitlines() if l.strip()]
        if len(lines) != len(keys):
            raise SystemExit(f"逐条对应断了：{f.name} raw {len(keys)} 条 / text {len(lines)} 行")
        rel = str(f.relative_to(raw_root)).replace("\\", "/")
        for k, c, t in zip(keys, cmds, lines):
            m = KIND_RE.search(k)
            out.append(Entry(key=k, kind=m.group(1) if m else "?", cmd=c, text=t, src=rel))
    return out


ALL_ENTRIES = False
"""用途 1 开关：True 时所有剧本条目都算"该上屏"，见 --usage1。"""


def is_dialogue(e: Entry) -> bool:
    if ALL_ENTRIES:
        return True
    return e.kind not in NON_DIALOGUE_KINDS and e.cmd not in NON_DIALOGUE_CMDS


def read_subs(paths: list[Path], sort_by_time: bool = False) -> list[tuple[float, str, str]]:
    """按给定顺序读 .ass / .srt，返回 (起点秒, 文本, 来源文件)。

    默认**按命令行给的顺序首尾相接**，不重排：input1..5 是一次通关的先后，
    每个文件的时间戳都从 0 重新开始，重排会把游标逻辑毁掉。

    但用途 1 要把**同一段视频的多条轨**合起来看（字幕带 + 辩论屏 + UI…），
    那些文件共享同一条时间轴，必须 `sort_by_time=True` 按时间归并——
    否则一条轨读完再读下一条，游标会被拽回开头，对齐全乱。
    **这两种用法必须显式区分，猜错了不会报错、只会出一堆假数。**
    """
    out = []
    for p in paths:
        # 解析与"一条 = 一个 cue"的口径统一在 flowocr.artifacts.srtio。
        # 拼行用 `" "` 是本工具的历史口径——分隔符会改变文本相似度读数，不能随手改。
        for c in srtio.read_subs([p]):
            out.append((c.start, c.text(" "), p.name))
    if sort_by_time:
        out.sort(key=lambda x: x[0])
    return out


def align(script: list[Entry], subs: list[tuple[float, str, str]],
          window: int, fuzzy: float) -> dict:
    """游标对齐。返回统计，同时就地把 Entry.matched 填上。"""
    sn = [norm(e.text) for e in script]
    by_text: dict[str, list[int]] = {}
    for i, t in enumerate(sn):
        by_text.setdefault(t, []).append(i)
    sm = SequenceMatcher(None)

    cursor = 0
    n_exact = n_fuzzy = n_none = n_jump = 0
    sub_hit: list[int] = []
    for si, (_, raw_text, _) in enumerate(subs):
        t = norm(raw_text)
        if not t:
            sub_hit.append(-1)
            continue
        hit = -1
        cand = by_text.get(t)
        if cand:
            # 优先游标前方最近的一个；都在后方说明是分支跳转/回读
            ahead = [i for i in cand if i >= cursor]
            hit = ahead[0] if ahead else cand[-1]
            if not ahead or hit - cursor > window:
                n_jump += 1
            n_exact += 1
        else:
            # 窗口内模糊找。**只在窗口内做**：全局模糊搜 34k 条对 23k 条字幕
            # 是 8 亿次比较，而且会把无关的相似句配上，比不配还糟。
            # 剪枝**不改结果**（同 align_unordered）：两个 quick 比值都是 ratio 的上界，
            # 够不着当前最好分的不必真算（lower5 原始轨 37 s -> 8 s，JSON 逐字节相同）。
            # a=字幕、b=剧本的先后保持原样。
            lo, hi = cursor, min(len(script), cursor + window)
            best, best_s = -1, fuzzy
            sm.set_seq1(t)
            for i in range(lo, hi):
                sm.set_seq2(sn[i])
                if sm.real_quick_ratio() <= best_s or sm.quick_ratio() <= best_s:
                    continue
                s = sm.ratio()
                if s > best_s:
                    best, best_s = i, s
            if best >= 0:
                hit, n_fuzzy = best, n_fuzzy + 1
                script[best].sim = round(best_s, 3)
            else:
                n_none += 1
        sub_hit.append(hit)
        if hit >= 0:
            if script[hit].matched < 0:
                script[hit].matched = si
            cursor = hit + 1
    return {"subs": len(subs), "exact": n_exact, "fuzzy": n_fuzzy,
            "unmatched_subs": n_none, "jumps": n_jump, "sub_hit": sub_hit}


FILL_SIM, FILL_MIN = 0.5, 3
"""`text_support`：原始 OCR 的一条 cue 和剧本行**共有的内容字**占这一行的 `FILL_SIM`，或者不少于 `FILL_MIN` 字且占这条 OCR 的 `FILL_SIM`，
才算"OCR 读到了这一行"。0.5 取参照匹配器（`dev_tools/reference/match_ref.py`）不靠上下文时的门 `SIMILARITY_THRESHOLD`。
后一半是给**半截**的：打字机只读到句尾「ら 待っててよ。」、同一条 cue 里拼进了 `NO DATA` 这类垃圾——对整行的相似度都低，字却是这一行的；
`FILL_MIN` 挡住单字碰巧落在行里（`大` 对一句含「大」的台词），同 owner 的三档。"""


def _content(t: str) -> str:
    """比内容用：`norm` 之后按 `gamescript.cnorm` 去掉标点空白、小书写假名并成大写（OCR 最常见的差异是省略号和 `はぁ` / `はあ`）。"""
    from flowocr.analyze.gamescript import cnorm     # 懒 import：gamescript 带 numpy，只有报填位的才要它
    return cnorm(norm(t))


def supports(ocr: str, line: str, partial: bool = True) -> bool:
    """这条 OCR 文本算不算读到了这一行，按这一行的内容字数（`_content`）分三档：
    * ≥ `FILL_MIN` 字：共有的内容字占这一行的 `FILL_SIM`，或 ≥ `FILL_MIN` 字且占这条 OCR 的 `FILL_SIM`（半截读数；`partial=False` 不认这一支，
      给在很宽的时间窗里搜读数的地方用——窗宽时长行总能撞上几个碎片，miss-detector 计划）；
    * 1–2 字：这几个字**全在** OCR 里（`「未着」……` 读成 `火花 / 「未着」`、`うん` 前面挂着名牌都算；`ふふ` 读成 `不不` 不算）；
    * 纯标点：连标点一起比（`norm`），共有的占这一行的 `FILL_SIM`（`…！！！` 读成 `ムアラニ / …!!!` 算）。"""
    lc, oc = _content(line), _content(ocr)
    def common(a: str, b: str) -> int:
        return sum(x.size for x in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks())
    if len(lc) >= FILL_MIN:
        m = common(oc, lc)
        return m >= FILL_SIM * len(lc) or (partial and m >= FILL_MIN and m >= FILL_SIM * len(oc))
    if lc:
        return common(oc, lc) == len(lc)
    ln = norm(line)
    return bool(ln) and common(norm(ocr), ln) >= FILL_SIM * len(ln)


def text_support(script: list[Entry], sub_hit: list[int], out_cues: list[tuple[float, float, int]],
                 ocr_cues: list[list[tuple[float, float, str]]]) -> set[int]:
    """**匹配器输出**上的命中里，哪些剧本行有 OCR 文本支撑：认领它的输出 cue 里至少有一条，时段内（正重叠）有一条原始 OCR cue
    和这一行共有足够多的内容字（`supports`）。返回这些行在 `script` 里的下标；命中但不在里面的就是**填位**——匹配器按上下文把剧本行填在了
    没读到这句的 cue 上（垃圾 cue、空档）。

    为什么要分开（2026-09-26 审计 → 2026-10-05 改口径）：匹配后的 `.ass` 文本已被换成剧本原文，游标对齐必然逐字命中；
    yuka f5 上看 x 那一刀记的 −15 条"命中"，多数是旧臂里匹配器把剧本行填在发饰误读的 `大` / `米` cue 上，
    挡掉垃圾 cue 填位就没了——不拆开，往主轨里灌垃圾 cue 的改动在这把尺上显得有收益。

    `sub_hit`：`align` 返回的每条输出 cue 认的剧本下标；`out_cues`：每条输出 cue 的 (起, 止, 文件序号)，和 `sub_hit` 一一对应；
    `ocr_cues`：按文件序号给的原始 OCR cue（起, 止, 文本），即喂给匹配器的那份。"""
    import bisect
    starts = [[c[0] for c in f] for f in ocr_cues]
    longest = [max((c[1] - c[0] for c in f), default=0.0) for f in ocr_cues]
    cache: dict[tuple[int, int, int], bool] = {}
    out: set[int] = set()
    for si, h in enumerate(sub_hit):
        if h < 0 or h in out:
            continue
        s, e, f = out_cues[si]
        k = bisect.bisect_left(starts[f], e)
        while k > 0:
            k -= 1
            a, b, t = ocr_cues[f][k]
            if a < s - longest[f]:
                break
            if b <= s:
                continue
            key = (f, k, h)
            if key not in cache:
                cache[key] = supports(t, script[h].text)
            if cache[key]:
                out.add(h)
                break
    return out


def read_timed(paths: list[Path]) -> list[tuple[float, float, int]]:
    """和 `read_subs(paths)` 一条对一条的 (起, 止, 文件序号)：`text_support` 的 `out_cues`。"""
    return [(c.start, c.end, f) for f, p in enumerate(paths) for c in srtio.read_subs([p])]


def read_ocr(paths: list[Path]) -> list[list[tuple[float, float, str]]]:
    """原始 OCR（喂给匹配器的那份）按文件读成 (起, 止, 文本)、按起点排：`text_support` 的 `ocr_cues`。"""
    return [sorted((c.start, c.end, c.text(" ")) for c in srtio.read_subs([p])) for p in paths]


def align_unordered(script: list[Entry], subs: list[tuple[float, str, str]],
                    lo: int, hi: int, fuzzy: float) -> dict:
    """不走游标，只问"区间内每条剧本，字幕里有没有人读到过"。

    **用途 1 必须用这个。** 游标对齐假设字幕流跟着剧本顺序走——对一条字幕带成立，
    对"画面全部文字"不成立：合并流里 UI、水印、时间戳、弹幕占了八成以上，
    它们不在剧本里，却会把游标甩得到处跳。实测同一份数据，
    游标口径把召回从 62%（只看字幕带）压到 39%，**加进更多正确的文字反而更低**，
    那不是管线的性质，是量法的错。

    代价：无序判据会高估——同一句话在别处出现也算命中。所以它只适合回答
    "画面上这处文字有没有被抓到"，不适合回答"时间对不对"。
    """
    idx = {}
    for si, (_, t, _) in enumerate(subs):
        n = norm(t)
        if n:
            idx.setdefault(n, si)
    by_len: dict[int, list[str]] = {}
    for n in idx:
        by_len.setdefault(len(n), []).append(n)
    n_exact = n_fuzzy = 0
    for e in script[lo:hi + 1]:
        n = norm(e.text)
        if not n:
            continue
        if n in idx:
            e.matched, n_exact = idx[n], n_exact + 1
            continue
        # 长度门按**比例**放宽，不是固定 ±3：我们的 cue 可能把说话人名和台词并在一起，
        # 或者 OCR 多吞/少读几个字，固定窗会把这些正当的匹配挡在外面。
        # 自检口径：无序判据永远不该低于游标判据（它是更松的问法）——
        # 固定 ±3 时实测 310 < 329，就是被这个门挡的。
        slack = max(4, int(0.4 * len(n)))
        best, best_s = -1, fuzzy
        # 剪枝**不改结果**：real_quick_ratio（只看长度）和 quick_ratio（字符多重集
        # 交集）都是 ratio 的上界，达不到当前最好分就没必要真跑 ratio。
        # 整片上候选是万级，少了这一步一次测量要跑一小时以上。
        # 两个序列的先后**保持原样**（a=剧本行、b=字幕行）：SequenceMatcher 的
        # ratio 不保证左右对称，换个方向可能量出别的数。
        sm = SequenceMatcher(None)
        sm.set_seq1(n)
        for L in range(max(1, len(n) - slack), len(n) + slack + 1):
            for c in by_len.get(L, ()):
                sm.set_seq2(c)
                if sm.real_quick_ratio() <= best_s or sm.quick_ratio() <= best_s:
                    continue
                r = sm.ratio()
                if r > best_s:
                    best, best_s = idx[c], r
        if best >= 0:
            e.matched, e.sim, n_fuzzy = best, round(best_s, 3), n_fuzzy + 1
    return {"subs": len(subs), "exact": n_exact, "fuzzy": n_fuzzy,
            "unmatched_subs": -1, "jumps": 0, "sub_hit": []}


def gap_runs(flags: list[bool]) -> list[tuple[int, int]]:
    """连续 False 段，返回 (起点下标, 长度)。"""
    out, i = [], 0
    while i < len(flags):
        if flags[i]:
            i += 1
            continue
        j = i
        while j < len(flags) and not flags[j]:
            j += 1
        out.append((i, j - i))
        i = j
    return out


def clen(t: str) -> int:
    """行长（原始字符数，不做 NFKC——见 script-corpus 报告『量行长不能用 NFKC』）。"""
    return len("".join(t.replace(chr(92) + "N", "").split()))


BANDS = [(1, 3), (3, 5), (5, 7), (7, 11), (11, 21), (21, 10 ** 6)]


def gap_report(dial: list[Entry], branch_run: int, long_is_branch: bool = True) -> dict:
    """未命中拆成长 / 短 gap，疑似真漏按内容字数三档、按行长分档。打印并返回中间量。

    `dial` 是**已经定好的分母**（该上屏的那些条目，按剧本顺序），`matched` 已填。
    本工具的 `main` 和 `flowocr.analyze.game_align` 共用这一份，别再抄第二份。

    `long_is_branch`：长 gap 能不能解释成"分支没走到"。《魔裁》的剧本是线性文件，分支只能靠
    gap 长度猜，所以本工具是 True——长 gap 不计入疑似真漏。`game_align` 传 False：那边分支和
    没播的一截已经按对话图 / obs 锚点排出分母，**长 gap 里剩下的没有分支解释**（点得快、别的版式、
    圈选假阳性……），照样拆出来报，但三档表把它一起算进疑似真漏（audit-6）。

    返回的 `by_class`：档 -> [该档条目数, 其中疑似真漏, 疑似真漏里落在长 gap 的]，
    口径随 `long_is_branch`（True 时前两个数都不含长 gap、第三个恒为 0）。"""
    from collections import Counter
    hit = [e for e in dial if e.matched >= 0]
    flags = [e.matched >= 0 for e in dial]
    runs = gap_runs(flags)
    long_runs = [(i, n) for i, n in runs if n >= branch_run]
    short = [(i, n) for i, n in runs if n < branch_run]
    n_long = sum(n for _, n in long_runs)
    n_short = sum(n for _, n in short)
    print(f"未命中 {len(dial)-len(hit)} 条，拆开看：")
    if long_is_branch:
        print(f"  连续 ≥{branch_run} 条的段：{len(long_runs)} 段 / {n_long} 条"
              f"（{n_long/max(1,len(dial)):.1%}）—— **多半是分支没走到，不算漏**")
        print(f"  零散 <{branch_run} 条的：{len(short)} 处 / {n_short} 条"
              f"（{n_short/max(1,len(dial)):.1%}）—— **这才是疑似真漏**")
        # **这个数会"系统变差反而变好"**（methodology-audit-4 报告）：
        # 多漏几条把一段凑够 `--branch-run` 长度，整段就被挪进"多半是分支没走到"，
        # 短 gap 反而下降。反例：七条真实上屏的台词，A 命中 1/4/7 -> 短 gap 4 条；
        # B 再漏掉第 4 条 -> 五条连续未命中被判成长 gap -> 短 gap **0 条**。
        # 所以短 gap 只能和**命中**一起读，单独下降不等于漏得少。
        print(f"  ⚠ 短 gap **不能单独读**：再多漏几条、把一段凑到 ≥{branch_run} 长，"
              f"它会被移进长 gap，这个数反而下降。"
              f"\n    要看变好还是变坏，**必须连着命中一起看**（命中 {len(hit)}；"
              f"长 gap {n_long} 条里可能藏着真漏，那一侧没有逐条清单）。")
    else:
        print(f"  连续 ≥{branch_run} 条的段：{len(long_runs)} 段 / {n_long} 条"
              f"（{n_long/max(1,len(dial)):.1%}）—— 分支 / 没播的一截已经排出分母，"
              f"**这里没有分支解释**，一起算进疑似真漏（成段地漏，要摆帧看是什么）")
        print(f"  零散 <{branch_run} 条的：{len(short)} 处 / {n_short} 条"
              f"（{n_short/max(1,len(dial)):.1%}）")
        print("  疑似真漏 = 全部未命中 = 分母 − 命中（逐档都是命中的镜像，不经过 gap 分桶）。")

    c = Counter(n for _, n in runs)
    print("  未命中段长度分布：" + "  ".join(f"{k}条×{v}" for k, v in sorted(c.items())[:8]))

    # 按长度分档看漏条率。**这一档不是装饰**：第一次跑就看出未命中的中位长度只有 5 字，
    # 但"短行本来就多"和"短行更容易漏"是两回事，必须除以同档的总数才知道。
    short_idx = {i for i, n in short for i in range(i, i + n)}
    long_idx = {i for i, n in long_runs for i in range(i, i + n)}

    # **纯标点行必须单列**（methodology-audit-2 报告）：它占疑似真漏的 60-71%，
    # 病因是 **det 在字幕带里根本没出框**（抽帧逐条确认过），
    # 和有内容字的行（多半是匹配器没锚上）不是一回事。
    # 混在一个百分比里报，会得出"匹配器是杠杆"这种反的结论。
    # **按"内容字数"分三档报**（owner 2026-09-06：trivial 的那些要单独分类）。
    # 三档的病因和取舍完全不同，混在一个漏条率里报会把结论带反：
    #   纯标点        det 没出框；不作强求，有原文时靠上下文 momentum
    #   trivial 1-2 字 判别力接近零，锚不锚得住看上下文
    #   有内容 >=3 字  这一档才是真的"该抓到没抓到"
    # 口径随 long_is_branch：True 时长 gap 整段不进分母也不算漏；False 时全部未命中都算疑似真漏
    counted = (lambda k: k not in long_idx) if long_is_branch else (lambda k: True)
    missed_idx = short_idx if long_is_branch else short_idx | long_idx
    n_missed = len(missed_idx)
    print()
    print("疑似真漏按**内容字数**分类（内容字 = 去掉标点和空白之后还剩几个字）：")
    print(f"  {'类别':<16}{'剧本条目':>9}{'疑似真漏':>9}{'漏条率':>9}{'占疑似真漏':>11}"
          + ("" if long_is_branch else f"{'其中长 gap':>11}"))
    by_class = {}
    for cls in evalkit.CLASSES:
        tot = sum(1 for k, e in enumerate(dial) if evalkit.triviality(e.text) == cls and counted(k))
        m = sum(1 for k in missed_idx if evalkit.triviality(dial[k].text) == cls)
        ml = 0 if long_is_branch else sum(1 for k in long_idx
                                          if evalkit.triviality(dial[k].text) == cls)
        by_class[cls] = [tot, m, ml]
        if not tot and not m:
            continue
        print(f"  {cls:<16}{tot:>9}{m:>9}{m/max(1,tot):>9.1%}{m/max(1,n_missed):>11.1%}"
              + ("" if long_is_branch else f"{ml:>11}"))
    print("  归因请用 `dev_tools/probe_gap_locate.py`（问『这一处 det 出框了没有』），"
          "别用『全局模糊搜』那类判据（script_align --raw-ocr 那一刀）。")
    print()
    print("疑似真漏按行长分档（分母是该档全部对话条目"
          + ("，已排除『分支没走到』的长段）：" if long_is_branch else "）："))
    print(f"  {'行长':>8}{'剧本条目':>9}{'疑似真漏':>9}{'漏条率':>9}")
    for lo, hi in BANDS:
        tot = sum(1 for k, e in enumerate(dial) if lo <= clen(e.text) < hi and counted(k))
        m = sum(1 for k in missed_idx if lo <= clen(dial[k].text) < hi)
        tag = f"{lo}-{hi-1}" if hi < 10 ** 6 else f"{lo}+"
        if tot:
            print(f"  {tag:>8}{tot:>9}{m:>9}{m/tot:>9.2%}")

    return {"runs": runs, "long_runs": long_runs, "short": short, "n_long": n_long,
            "n_short": n_short, "short_idx": short_idx, "long_idx": long_idx,
            "by_class": by_class}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", required=True, help="script-raw 的 Scripts 目录（提供 key/类型）")
    ap.add_argument("--text", required=True, help="script-jp 的 Scripts 目录（提供干净文本）")
    ap.add_argument("--subs", nargs="+", required=True,
                    help="字幕文件，**按通关先后给**，不要让 shell 打乱顺序")
    ap.add_argument("--out", default=None, help="逐条结果写到这个 JSON")
    ap.add_argument("--window", type=int, default=400,
                    help="游标前方的搜索窗口（剧本条目数）")
    ap.add_argument("--fuzzy", type=float, default=0.75, help="窗口内模糊匹配的相似度下限")
    ap.add_argument("--branch-run", type=int, default=5,
                    help="未命中连续段达到这么长就算『分支没走到』，不计进疑似真漏")
    ap.add_argument("--raw-ocr", nargs="*", default=None,
                    help="OCR 的**原始**输出（未经匹配器）。给了就把疑似真漏再拆一刀："
                         "原始 OCR 里读到过的 = 匹配器丢的，没读到过的 = OCR 丢的。"
                         "两件事的修法完全不同，合成一个『漏条率』就没法定位")
    ap.add_argument("--fill-ocr", nargs="+", default=None,
                    help="喂给匹配器的那份 OCR（和 --subs 一个文件对一个，同顺序）。给了就把命中拆成"
                         "『有 OCR 文本支撑』和『填位』（匹配器按上下文填在没读到这句的 cue 上），"
                         "JSON 多出 dialogue_hit_text / dialogue_fill。量匹配器输出时要给（text_support）")
    ap.add_argument("--text-quality", action="store_true",
                    help="在配上的那些对上，报逐字相同率和省略号召回。"
                         "命中率只说『锚没锚上』，这两个数说『读得有多像』——"
                         "下游匹配器是相似度驱动的，短行差一个字就掉到阈值下面")
    ap.add_argument("--unordered", action="store_true",
                    help="不走游标，只问『区间内每条剧本有没有被读到过』。"
                         "**用途 1 必须开**（合并流里八成条目不在剧本里，游标会被甩飞）；"
                         "代价是会高估——同一句话在别处出现也算命中")
    ap.add_argument("--usage1", action="store_true",
                    help="用途 1 口径：分母 = 区间内**全部**剧本条目（对话 + 辩论屏 + 选项 "
                         "+ Toast），因为它们都在屏幕上。默认是用途 2 口径：只算字幕带。"
                         "两个用途的分母不同，这是它们唯一分岔的地方")
    ap.add_argument("--same-timeline", action="store_true",
                    help="多个字幕文件属于**同一段视频的不同轨**（用途 1：字幕带+辩论屏+UI），"
                         "按时间归并。默认是相反的假设：多个文件是一次通关的先后段落，首尾相接")
    ap.add_argument("--raw-sim", type=float, default=0.8,
                    help="判定『原始 OCR 读到过』的相似度下限。**别用完全相等**："
                         "`ぐっ……！？` 被读成 `ぐっ…....！?` 也算读到了，"
                         "那是匹配器的锅不是 OCR 的锅")
    ap.add_argument("--include-cmds", default="",
                    help="把这些命令类条目放回分母（逗号分隔，如 @printDebate）。"
                         "用途 1 要统计画面全部文字时用；字幕带口径保持默认")
    ap.add_argument("--time-range", nargs=2, type=float, default=None,
                    metavar=("S", "E"),
                    help="只取起点落在 [S, E) 秒内的字幕条目。跑窗口实验时两边都要加")
    ap.add_argument("--span-ref", nargs="+", default=None,
                    help="用这份字幕先圈出**剧本区间**，再把统计限定到该区间。"
                         "只跑一个时间窗时必须用它：否则分母是整部剧本，"
                         "命中率会被稀释成无意义的数。两边（基线/我们）用同一个 "
                         "--span-ref，分母才一样")
    ap.add_argument("--show", type=int, default=8, help="列几条疑似真漏")
    args = ap.parse_args()

    if args.usage1:
        global ALL_ENTRIES
        ALL_ENTRIES = True
        print("（用途 1 口径：分母含辩论屏/选项/Toast，即区间内全部剧本条目）")
    for c in (x.strip() for x in args.include_cmds.split(",") if x.strip()):
        NON_DIALOGUE_CMDS.discard(c)
        print(f"（{c} 已放回分母）")
    script = load_script(Path(args.script), Path(args.text))

    def load_subs(paths):
        xs = read_subs([Path(x) for x in paths], sort_by_time=args.same_timeline)
        if args.time_range:
            lo, hi = args.time_range
            xs = [x for x in xs if lo <= x[0] < hi]
        return xs

    subs = load_subs(args.subs)
    rng = (f"，限定 {args.time_range[0]/60:.0f}-{args.time_range[1]/60:.0f} min"
           if args.time_range else "")
    print(f"剧本 {len(script)} 条，字幕 {len(subs)} 条（{len(args.subs)} 个文件{rng}）")

    # 只跑一个时间窗时，分母必须是**该窗覆盖到的那段剧本**，不是整部剧本。
    # 用一份参照字幕圈出区间，两边共用同一个区间，命中率才可比。
    span = None
    if args.span_ref:
        ref = load_subs(args.span_ref)
        align(script, ref, args.window, args.fuzzy)
        got = [i for i, e in enumerate(script) if e.matched >= 0]
        if not got:
            raise SystemExit("--span-ref 一条都没对上剧本，圈不出区间")
        span = (min(got), max(got))
        print(f"区间由参照字幕（{len(ref)} 条）圈出：剧本 [{span[0]}, {span[1]}]"
              f"，共 {span[1]-span[0]+1} 条")
        for e in script:                      # 重置，别把参照的命中算进被测那一份
            e.matched, e.sim = -1, 0.0

    if args.unordered:
        if span is None:
            raise SystemExit("--unordered 必须配 --span-ref：无序判据得先有区间，否则分母是整部剧本")
        st = align_unordered(script, subs, span[0], span[1], args.fuzzy)
        print(f"字幕侧（无序口径）：{st['subs']} 条；"
              f"剧本被命中 精确 {st['exact']} + 模糊 {st['fuzzy']}"
              f"（**无序判据不报『对不上剧本』**：用途 1 的合并流里本来就大量是 UI、"
              f"水印、时间戳，不在剧本里不等于幽灵）")
    else:
        st = align(script, subs, args.window, args.fuzzy)
        n_empty = st["subs"] - st["exact"] - st["fuzzy"] - st["unmatched_subs"]
        print(f"字幕侧：精确 {st['exact']}（{st['exact']/max(1,st['subs']):.1%}）、"
              f"模糊 {st['fuzzy']}、**对不上剧本 {st['unmatched_subs']}"
              f"（{st['unmatched_subs']/max(1,st['subs']):.1%}，这批是幽灵条目的上限）**、"
              f"跨窗跳转 {st['jumps']}"
              + (f"、归一化后为空 {n_empty}（不进上面任何一档）" if n_empty else ""))
        # **在匹配后的 .ass 上，这个数结构上只能是 0。**
        # match_ref 把匹配不上的 cue 整条丢弃、留下的正文一律换成剧本原文，
        # 所以"对不上剧本"根本无从产生（methodology-audit-2 报告）。
        # 幽灵条目只能在**匹配前**的原始轨上量。
        if st["unmatched_subs"] == 0 and st["fuzzy"] == 0 and st["subs"] > 100:
            print("  ⚠ **对不上剧本 = 0 且模糊 = 0**：这几乎一定是"
                  "在**匹配器的输出**上量的（正文已被换成剧本原文），"
                  "这个 0 是结构性的，**不能当成『没有幽灵条目』的证据**。"
                  "要量幽灵请拿匹配前的原始轨。")

    lo_i, hi_i = span if span else (0, len(script) - 1)
    dial = [e for i, e in enumerate(script) if is_dialogue(e) and lo_i <= i <= hi_i]
    n_other = sum(1 for i, e in enumerate(script)
                  if not is_dialogue(e) and lo_i <= i <= hi_i)
    hit = [e for e in dial if e.matched >= 0]
    print()
    tail = ("（用途 1 口径：选项/Toast/命令**已算进分母**）" if args.usage1
            else f"（另有 {n_other} 条是选项/Toast/命令，不计）")
    print(f"剧本侧：**该上屏的对话类 {len(dial)} 条**{tail}，"
          f"命中 {len(hit)}（{len(hit)/max(1,len(dial)):.1%}）")
    hit_text = None
    if args.fill_ocr:
        if len(args.fill_ocr) != len(args.subs) or args.same_timeline or args.time_range or args.unordered:
            raise SystemExit("--fill-ocr 要和 --subs 一个文件对一个，且不和 --same-timeline / --time-range / --unordered 一起用")
        ok = text_support(script, st["sub_hit"], read_timed([Path(x) for x in args.subs]),
                          read_ocr([Path(x) for x in args.fill_ocr]))
        idx = {id(e): i for i, e in enumerate(script)}
        hit_text = [e for e in hit if idx[id(e)] in ok]
        print(f"  其中**有 OCR 文本支撑的** {len(hit_text)}（{len(hit_text)/max(1,len(dial)):.1%}），"
              f"填位 {len(hit) - len(hit_text)}（匹配器按上下文填在没读到这句的 cue 上，`text_support`）")

    gr = gap_report(dial, args.branch_run)
    runs, long_runs, short = gr["runs"], gr["long_runs"], gr["short"]
    n_long, n_short, short_idx = gr["n_long"], gr["n_short"], gr["short_idx"]

    if args.text_quality:
        # 配上之后，读得**有多像**。用途 2 里这一项直接决定下游匹配器锚不锚得住：
        # match.py 是相似度驱动的，短行差一个字就掉到阈值下面。
        BRK = chr(92) + "N"
        ELL = re.compile("…+")
        pairs = [(script[i].text.replace(BRK, ""), subs[script[i].matched][1])
                 for i in range(lo_i, hi_i + 1) if script[i].matched >= 0]
        if pairs:
            ex = sum(1 for x, y in pairs if norm(x) == norm(y))
            tail = re.compile(r"[.。!！?？、,)）」』　·・…]+$")
            ex_t = sum(1 for x, y in pairs if tail.sub("", norm(x)) == tail.sub("", norm(y)))
            ns = sum(len(ELL.findall(x)) for x, y in pairs)
            no = sum(len(ELL.findall(y)) for x, y in pairs)
            print()
            warn = ("  ⚠ 无序口径下多条剧本可能配到同一条字幕，这些数会被重复计入"
                    if args.unordered else "")
            print(f"文本质量（在配上的 {len(pairs)} 对上）：{warn}")
            print(f"  逐字相同 {ex}（{ex/len(pairs):.1%}）；"
                  f"剥掉两边尾部标点后 {ex_t}（{ex_t/len(pairs):.1%}）")
            print(f"  省略号串：剧本 {ns} 处，我们读出 {no} 处"
                  f"（**召回 {no/max(1,ns):.1%}**）")

    if args.usage1:
        # 用途 1 的重点不是总召回，是**各类上屏文字分别抓到多少**：
        # 字幕带是一回事，辩论屏、选项、Toast 走的是别的 UI、别的字号、别的位置。
        import collections as _c
        grp = _c.defaultdict(lambda: [0, 0])
        for e in dial:
            k = e.cmd or ("Choice" if e.kind == "Choice" else
                          "Toast" if e.kind == "Toast" else "对话/旁白")
            grp[k][0] += 1
            grp[k][1] += e.matched >= 0
        print()
        print("按条目类型拆开（用途 1 的重点）：")
        print(f"  {'类型':<16}{'剧本条目':>9}{'命中':>7}{'召回':>9}")
        for k, (tot, got) in sorted(grp.items(), key=lambda x: -x[1][0]):
            print(f"  {k:<16}{tot:>9}{got:>7}{got/max(1,tot):>9.1%}")

    if args.raw_ocr:
        # **不能用完全相等做归因。** 实测望言把 `ぐっ……！？` 读成 `ぐっ…....！?`
        # （省略号读成点），完全相等会把它算成"OCR 没读出来"，但那明明是读到了、
        # 只是匹配器没锚上。两件事的修法完全不同，归错了整条结论就偏。
        # 所以先按完全相等找，再在**同长度附近**做一次模糊找。
        raw_list = [norm(t) for _, t, _ in read_subs([Path(x) for x in args.raw_ocr])]
        seen = set(raw_list)
        by_len: dict[int, list[str]] = {}
        for t in seen:
            by_len.setdefault(len(t), []).append(t)

        def read_by_ocr(t: str) -> bool:
            if t in seen:
                return True
            for L in range(max(1, len(t) - 3), len(t) + 4):
                for c in by_len.get(L, ()):
                    if SequenceMatcher(None, t, c).ratio() >= args.raw_sim:
                        return True
            return False

        missed = [dial[k] for k in sorted(short_idx)]
        hit_raw = {id(e): read_by_ocr(norm(e.text)) for e in missed}
        n_in = sum(1 for e in missed if hit_raw[id(e)])
        print()
        print(f"再拆一刀（原始 OCR 去重 {len(seen)} 条）：")
        print(f"  {'行长':>7}{'疑似真漏':>9}{'OCR 读到过':>11}{'OCR 也没读出':>13}")
        for lo, hi in BANDS:
            sel = [e for e in missed if lo <= clen(e.text) < hi]
            if not sel:
                continue
            k = sum(1 for e in sel if hit_raw[id(e)])
            tag = f"{lo}-{hi-1}" if hi < 10 ** 6 else f"{lo}+"
            print(f"  {tag:>7}{len(sel):>9}{k:>11}{len(sel)-k:>13}")
        pu = [e for e in missed if evalkit.is_punct_only(e.text)]
        pu_in = sum(1 for e in pu if hit_raw[id(e)])
        print("  按内容字数分档看这一刀（**判据在 trivial 那两档上已经饱和**）：")
        for cls in evalkit.CLASSES:
            sel = [e for e in missed if evalkit.triviality(e.text) == cls]
            if not sel:
                continue
            k = sum(1 for e in sel if hit_raw[id(e)])
            print(f"    {cls:<16}{evalkit.denom(k, len(sel)):>18} 判为『匹配器丢的』")
        print(f"  → **{n_in/max(1,len(missed)):.1%} 是匹配器丢的**（OCR 明明读到过），"
              f"**{1-n_in/max(1,len(missed)):.1%} 是 OCR 就没读出来**")
        # **这一刀是全局搜，对纯标点行几乎恒真**（methodology-audit-2 报告）：
        # 整片里 `……！` 这类行有几千条，"别处出现过"必然成立，
        # 于是它们被一律记成"匹配器丢的"——而抽帧看到的是那一处 det 根本没出框。
        # docs/dev-guide/verification.md「归因：问『这一处发生了什么』」 早写过这条，这里又踩了一次。
        print(f"  ⚠ **上面这刀是全局模糊搜，不问『这一处发生了什么』。**"
              f"纯标点行 {evalkit.denom(pu_in, len(pu))} 被判成"
              f"『匹配器丢的』，而它们占疑似真漏的 "
              f"{evalkit.denom(len(pu), len(missed))}——"
              f"整片里这类行有几千条，判据在这一档上已经饱和。"
              f"\n    要归因请用 `dev_tools/probe_gap_locate.py`（位置判据："
              f"这一时刻字幕带里 det 出框了没有）。")

    if short and args.show:
        print(f"\n疑似真漏抽样（前 {args.show} 条）：")
        for i, n in short[:args.show]:
            for e in dial[i:i + n]:
                print(f"  [{e.kind:<10}] {e.src:<34} {e.text[:52]}")

    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "script_entries": len(script), "dialogue_entries": len(dial),
            "subs": st["subs"], "exact": st["exact"], "fuzzy": st["fuzzy"],
            "unmatched_subs": st["unmatched_subs"],
            "dialogue_hit": len(hit),
            **({"dialogue_hit_text": len(hit_text), "dialogue_fill": len(hit) - len(hit_text)}
               if hit_text is not None else {}),
            "gap_long_runs": len(long_runs), "gap_long_entries": n_long,
            "gap_short_places": len(short), "gap_short_entries": n_short,
            "branch_run": args.branch_run,
            # ⚠ `missed` **只有短 gap**（`--raw-ocr` 那套归因就是按它做的）。长 gap 另列一份：
            # 2026-09-18 审计发现有工具把 `missed` 当"全部未命中"用（`rec_engine_arm.sh` 的命中集合差），
            # 而长 gap 不为 0 时那就是静默少算。**要"全部未命中"的人自己把两份并起来**，
            # 并且拿 `gap_short_entries + gap_long_entries` 对一次总数。
            "missed": [{"key": e.key, "kind": e.kind, "src": e.src, "text": e.text}
                       for i, n in short for e in dial[i:i + n]],
            "missed_long": [{"key": e.key, "kind": e.kind, "src": e.src, "text": e.text}
                            for i, n in long_runs for e in dial[i:i + n]],
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n逐条结果 -> {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
