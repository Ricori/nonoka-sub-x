"""时间疑点：只读阶段 2 的产物，逐条列出阶段 3 字幕稿里起止时间**疑似**错了的条目，带时刻、理由和证据，供人去摆帧确认。

    python -m flowocr.output.timing_check out/x/x-tracks.json [--opt keep=all] [--show 30] [--json 疑点.json]
    python -m flowocr.output.timing_check out/x/matched.json --opt lang=cn

查的就是字幕稿里写的那组时刻：条目走 `script.build_items`、效果走 `script.item_effects`，和 `render_script` 同一条路（`--opt` 也照传）。
不读视频、不读 obs，秒级。报的是线索，不是判决——不改字幕稿，也不改产物。

规则（`RULES`）分两类。A 类是产物内部对不上，没有阈值可调，但不等于屏上一定错；B 类是启发式，只是疑似：

* **A1 同位重叠**：同一区域、同一处（行重叠且横向相交）前后两条正文，后一条的起点早于前一条的终点超过容差
  （回抠过的 `A1_REFINED_FRAMES` 个原生帧，采样级的 `SAMPLE_TOL` 个采样间隔）。后一条起点早，或者前一条终点晚。
  同一区域里接连的重叠（相邻两次相隔不到 `BURST_GAP_US`）并成一段、只报一条：同一处的 OCR 噪声会一口气叠出上百次。
  重叠的只是两边各一行短字（`A1_SHORT_ROW` 字以内，多是一直挂着的名字）的照报，明细里标出来。
* **A2 起止超出成员**：条目的起点早于成员（事件加半截行）最早的 `t_start`，或终点晚于成员最晚的 `t_end`，超过 `SAMPLE_TOL` 个采样间隔。
  tracks 的条目按构造不会触发；matched 的正文条目起止取记录的，记录起点来自喂入 cue，cue 里名牌、计数器、上一句都算。
  证据两列：定下这个时刻的事件（同一条 cue 里起点对得上的非成员）；起点那一刻在屏、和正文是同一句（`textfold.same_reading`）却不在成员里的事件——
  有它就多半是误报：字其实早就在，只是成员没收全。
* **A3 起点晚于同句在屏**：条目起点之前超过 `SAMPLE_TOL` 个采样间隔，同一处（框和成员相交）就有和成员同一句（`textfold.same_reading`，
  或都不少于 `A3_NEAR_MIN` 字、只差个别字：读错一两个字）的非成员事件，一直挂到起点（断成几段、段间不超过容差的接起来算；起点前一个容差内刚结束、又已由别的条目画出的不算，那是字幕稿里连着交接的两条）。
  A2 的反面：起点晚了，字早就在。
  正文起点的源头修正（`scriptmatch.body_start`）只往后挪，挪过头靠它看见；喂入 cue 切开了同一句、记录起点本身晚的也是它。
  更早的那段字已经是别的条目的成员（同一句被拆成两条画）时照报，明细里标出那一条。
* **B1 起点被个别成员拉早**：最早的成员全字显出之后，要过 `B1_GAP_US` 才有不在同一行的成员出现。
* **B2 显出速度离群**：每字毫秒（打字机时长 / (字数 − 1)，字数按成员的原文数）和前后 `NEIGHBORS` 个同组条目的中位数比，
  慢 `B2_RATIO` 倍以上或快到 1/`B2_RATIO` 以下。同组：tracks 按区域、matched 按层。邻居不足 `MIN_NEIGHBORS` 个时比整份的中位数。
* **B3 停留时长离群**：全字之后的停留按每字折算，和邻居的中位数比超过 `B3_RATIO` 倍，且停留不少于 `B3_MIN_US`。

每条疑点另附把握标注（`notes`）：没回抠（采样级）、全字只有一路证据 / 没量到 / 被夹回、终点只是下界。它们说的是"没量准"，不单独成为疑点。

**回显条目单列**（owner 2026-10-10）：matched 的条目带回显判定（`echo`：对话记录、重演、同时另一处……，`matchedio.ECHOES`）。
回显条目照样过六条规则、照报（叠加层照画它们，时间错了画面上也错），但明细里注明是哪种回显（A1 两边任一是回显都注明），
汇总行每条规则另报"其中回显 N"，终端列表先列不沾回显的：对话记录界面一打开就是一批同时起、挂几十秒的条目，人可以先跳过。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from functools import cached_property
from pathlib import Path

from flowocr import textfold
from flowocr.artifacts import tracksio
from flowocr.output import script as S

RULES = {"A1": "同位重叠", "A2": "起止超出成员", "A3": "起点晚于同句在屏", "B1": "起点被个别成员拉早", "B2": "显出速度离群",
         "B3": "停留时长离群"}
OPTS = ("keep", "show", "hide", "lang", "tracks", "typewriter", "fade")
"""阶段 3 的选项里决定查哪些条目、按什么效果算时刻的那几个（`build_items`、`script.effect_modes`）；阶段 3 的提示按它拼出命令。"""

A1_REFINED_FRAMES = 3
"""A1 的容差：条目回抠过（原生帧精度）时这么多个原生帧；没回抠的用 `SAMPLE_TOL` 个采样间隔。"""
A1_SHORT_ROW = 4
"""A1 把"重叠的只是两边各一行这么多字以内的短行"标出来（见 `_same_place`）。"""
BURST_GAP_US = 10_000_000
"""A1 并段：同一区域里两次重叠的起点相隔不到这么久就算同一段（yuka-f4 7:20–7:40 同一处「Dummy」「Ms. 3」轮流读出，188 次）。"""
SAMPLE_TOL = tracksio.SAMPLE_SLACK
"""采样级的容差（A1 没回抠的条目、A2），这么多个采样间隔；为什么是 1.5 见 `tracksio.SAMPLE_SLACK`。"""
A3_NEAR_MIN, A3_NEAR_RATIO = 4, 0.8
"""A3 认"只差个别字"的同一句：两边去标点后都不少于这么多字，相似度（`difflib` 的 ratio）不低于这么多。
读错一个字（zzz 正文读成 `SKILL INFD`、同一处更早是 `SKILL INFO`）就配不上 `same_reading`。"""
SCREEN_SPAN_US = 120_000_000
"""查"某一刻在屏的事件"用的索引窗口（`tracksio.EventIndex`）：比它长的事件另存，所以只影响速度、不影响结果。"""
B1_GAP_US = 1_500_000
"""B1：最早的成员全字显出之后，下一行成员要隔这么久才出现才报。"""
ECHO_NAMES = {"log": "对话记录", "replay": "重演", "same_time": "同时另一处", "other": "其他"}
"""回显形态（`matchedio.ECHOES`）在明细里的写法。"""
B2_RATIO, B2_MIN_CHARS = 3.0, 8
"""B2：比邻居中位数慢这么多倍、或快到它的几分之一才报；只比不少于这么多字的（两三个字的碎片、注音跟着正文一起显出，折成每字毫秒天然偏大）。"""
B3_RATIO, B3_MIN_US, B3_MIN_CHARS = 4.0, 3_000_000, 8
"""B3：每字停留比邻居中位数长这么多倍，且停留至少这么久；只看不少于这么多字的（人名一挂就是几十秒，gi-s2 上 4 字的人名占了 B3 的全部 7 条）。"""
NEIGHBORS, MIN_NEIGHBORS, MIN_ITEMS = 5, 3, 5
"""B2 / B3 的邻居：同组前后各取这么多个；凑不够 `MIN_NEIGHBORS` 个就比整份的中位数；整份不到 `MIN_ITEMS` 个就不比。"""


@dataclass
class Suspect:
    rule: str
    start_us: int                  # 字幕稿里这一条的起止
    end_us: int
    text: str                      # 字幕稿里的文字（matched 是译文）
    off_us: int                    # 偏差量：A1 重叠多久、A2 超出多久、B1 隔了多久、B2 / B3 是本条的量（每字显出 / 每字停留，µs）
    detail: str
    ref: str                       # 回查用：tracks 的 cue id、matched 的剧本 key 或认领
    events: list[int]
    main: bool = True
    evidence: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    rows: list = field(default_factory=list)   # 这一条在画面上的行框（摆帧时裁这一块）
    span: list[int] = field(default_factory=list)   # 成员最早的 t_start、最晚的 t_end
    original: str = ""             # 成员的原文（matched 的 text 是译文）
    echo: str | None = None        # 这一条的回显形态（matched 的回显判定；主认领、只认领一次的和 tracks 的为空）
    echo_other: str | None = None  # A1 的前一条的回显形态（同上）；并段里别的几次重叠沾了回显的，也记在这里

    @property
    def echoish(self) -> bool:
        """沾回显：这一条或 A1 的另一边是回显条目。汇总单列、终端列表排后。"""
        return bool(self.echo or self.echo_other)


def suspect(rule: str, it: S.Item, off: int, detail: str, doc: dict, ref: str | None = None,
            events: list[int] | None = None, evidence: dict | None = None) -> Suspect:
    """一条疑点，条目上能取的都从条目取。"""
    ms = members(it)
    return Suspect(rule, it.start_us, it.end_us, " / ".join(it.lines), int(off), detail, ref if ref is not None else ref_of(it),
                   events if events is not None else [e["id"] for e in ms], it.main, evidence or {}, notes(it, doc),
                   [list(r) for r in it.rows], [min(e["t_start"] for e in ms), max(e["t_end"] for e in ms)], original(it), it.echo)


def members(it: S.Item) -> list[dict]:
    """条目的成员：事件加半截行（半截行只贡献首字时刻）。"""
    return list(it.events) + [ld for rl in it.row_leads for ld in rl]


def ref_of(it: S.Item) -> str:
    if it.key:
        return f"key {it.key}"
    if it.claim:
        return f"认领 {it.claim}"
    return f"cue {it.cue}" if it.cue else ""


def original(it: S.Item) -> str:
    """成员的原文（matched 的 `lines` 是译文，原文要从事件拿）。"""
    return " / ".join(r["text"] for r in tracksio.rows_of_events(list(it.events))) if it.events else ""


def echo_note(echo: str, who: str = "") -> str:
    return f"{who}回显·{ECHO_NAMES.get(echo, echo)}"


def notes(it: S.Item, doc: dict) -> list[str]:
    """把握标注：这条的时刻本来有多准；回显条目另注明是哪种回显。"""
    out = [echo_note(it.echo)] if it.echo else []
    if not S.refined_of(it, doc):
        out.append("采样级")
    how = Counter()
    for e in it.events:
        h = e.get("t_full_how") or {}
        for k in ("first", "full"):
            if h.get(k) in ("single", "none"):
                how[f"{'首字' if k == 'first' else '全字'}{'一路证据' if h[k] == 'single' else '没量到'}"] += 1
        if h.get("capped"):
            how["全字被夹回"] += 1
        if e.get("t_end_how") == "open":
            how["终点只是下界"] += 1
    return out + list(how)


def chars(evs: list[dict], step: int, pool: list[dict] | None = None) -> int:
    """成员原文的字数（去空白、去标点）；碎片和退场那一帧的重读不算（`script.effect_events`，和效果时刻同一批：重读的字是重复的，算进去会把每字毫秒算小）。
    B1 不用这一批，照旧只去几何碎片（`script.typing_events`）：重读口径会让 B1 漏掉对话记录界面那类真错。"""
    return sum(len(textfold.bare(e["text"])) for e in S.effect_events(evs, step, pool))


def _same_place(a: S.Item, b: S.Item) -> str | None:
    """两条在不在同一处：各取成员拼成的行，有一对行同一行、横向相交就在。不在返回 None；在的话返回 `"short"`（重叠的
    只是两边各一行 `A1_SHORT_ROW` 字以内的短行）或 `"long"`。短行照报、只是标出来：多是一直挂着的名字、标签
    （zzz / hsr 的 tracks 里名牌没单独成轨时，「リン」在连续几句里一直挂着；复核抽样的没错 14 条里 13 条是这种），
    但排除掉会丢真错（OCR 噪声被画出来的「1階 2階」「5」之类，调参抽样里 4 条），疑点工具宁可多报（owner 2026-10-07）。"""
    rows_a, rows_b = tracksio.rows_of_events(list(a.events)), tracksio.rows_of_events(list(b.events))
    pairs = [(ra, rb) for ra in rows_a for rb in rows_b
             if tracksio.same_row(ra["box"], rb["box"]) and min(ra["box"][2], rb["box"][2]) > max(ra["box"][0], rb["box"][0])]
    if not pairs:
        return None
    return "short" if all(max(len(textfold.bare(ra["text"])), len(textfold.bare(rb["text"]))) <= A1_SHORT_ROW
                          for ra, rb in pairs) else "long"


def _tol(it: S.Item, doc: dict) -> int:
    return A1_REFINED_FRAMES * S.native_frame_us(doc) if S.refined_of(it, doc) else int(SAMPLE_TOL * doc["frame_us"])


def _lines(items: list[S.Item]) -> list[S.Item]:
    """参与检查的条目：不动的正文行（名牌、选项、面板、UI、跨句常驻、在动的不查）。"""
    return [it for it in items if it.kind == "line" and not it.ui and not it.spans and not it.boxes and it.events]


class Context:
    """查证据用的索引：事件 → 它所在的区域轨 cue；按起点排好的事件。用到时才建。"""

    def __init__(self, doc: dict):
        self.doc = doc

    @cached_property
    def cues_of(self) -> dict[int, list[dict]]:
        out: dict[int, list[dict]] = defaultdict(list)
        for tr in self.doc["tracks"]:
            if tr.get("kind") == "region":
                for cue in tr["cues"]:
                    for i in cue["events"]:
                        out[i].append(cue)
        return out

    @cached_property
    def index(self) -> tracksio.EventIndex:
        return tracksio.EventIndex(self.doc["events"], SCREEN_SPAN_US)

    def mates(self, it: S.Item) -> list[dict]:
        """和成员同在一条区域轨 cue 里的事件（成员自己除外）。"""
        own = {e["id"] for e in members(it)}
        ids = {i for e in members(it) for c in self.cues_of.get(e["id"], ()) for i in c["events"]} - own
        return [self.doc["events"][i] for i in sorted(ids)]

    def on_screen(self, t: int, tol: int) -> list[dict]:
        """`t` 那一刻在屏的事件（起点放宽 `tol`）。"""
        return self.index.on_screen(t, tol)


def _box_hit(e: dict, boxes: list[list[float]]) -> bool:
    return any(tracksio.boxes_cross(e["box"], r) for r in boxes)


def rule_a1(items: list[S.Item], doc: dict, ctx: Context) -> list[Suspect]:
    out = []
    by_reg: dict[int, list[S.Item]] = defaultdict(list)
    for it in _lines(items):
        by_reg[it.region].append(it)
    for xs in by_reg.values():
        xs.sort(key=lambda it: (it.start_us, it.end_us))
        active: list[S.Item] = []
        hits: list[Suspect] = []
        for b in xs:
            active = [a for a in active if a.end_us > b.start_us]
            for a in active:
                ov = min(a.end_us, b.end_us) - b.start_us
                place = _same_place(a, b) if ov > max(_tol(a, doc), _tol(b, doc)) else None
                if place:
                    hits.append(h := suspect("A1", b, ov,
                                        f"和前一条 {fmt_t(a.start_us)}–{fmt_t(a.end_us)}「{' / '.join(a.lines)[:20]}」同处重叠 {ov / 1e6:.2f} s："
                                        "这一条起点早，或前一条终点晚"
                                        + ("（重叠的只是两边各一行短字，多半是一直挂着的名字）" if place == "short" else ""),
                                        doc, f"{ref_of(b)} ↔ {ref_of(a)}",
                                        [e["id"] for e in members(b)] + [e["id"] for e in members(a)],
                                        {"other": {"start_us": a.start_us, "end_us": a.end_us, "text": " / ".join(a.lines),
                                                   "original": original(a), "echo": a.echo}, "short_rows": place == "short"}))
                    if a.echo:
                        h.echo_other = a.echo
                        h.notes.append(echo_note(a.echo, "前一条是"))
            active.append(b)
        out += _bursts(hits)
    return out


def _bursts(hits: list[Suspect]) -> list[Suspect]:
    """同一区域按起点排好的 A1 命中并段：相邻两次相隔不到 `BURST_GAP_US` 的并进前一段，报第一次，另记次数和段尾。"""
    out: list[Suspect] = []
    last = None
    for h in hits:
        if out and h.start_us - last.start_us < BURST_GAP_US:
            head = out[-1]
            head.evidence["burst"] = head.evidence.get("burst", 1) + 1
            head.evidence["burst_end_us"] = max(head.evidence.get("burst_end_us", head.end_us), h.end_us)
            if h.echoish and not head.echoish:          # 并进来的那次沾回显、报出的第一次不沾：照样单列，免得混在正常条目里
                head.echo_other = h.echo or h.echo_other
                head.notes.append(echo_note(head.echo_other, "并段里有"))
        else:
            out.append(h)
        last = h
    for h in out:
        if h.evidence.get("burst", 1) > 1:
            h.detail += f"；同一处接连重叠 {h.evidence['burst']} 次，到 {fmt_t(h.evidence['burst_end_us'])}"
    return out


def rule_a2(items: list[S.Item], doc: dict, ctx: Context) -> list[Suspect]:
    out = []
    tol = int(SAMPLE_TOL * doc["frame_us"])
    for it in _lines(items):
        ms = members(it)
        lo, hi = min(e["t_start"] for e in ms), max(e["t_end"] for e in ms)
        texts = [e["text"] for e in ms]
        for side, off, t in (("起点", lo - it.start_us, it.start_us), ("终点", it.end_us - hi, it.end_us)):
            if off <= tol:
                continue
            key = "t_start" if side == "起点" else "t_end"
            setter = sorted((e for e in ctx.mates(it) if abs(e[key] - t) <= tol), key=lambda e: abs(e[key] - t))
            boxes = [e["box"] for e in ms]
            own = {e["id"] for e in ms}
            same = [e for e in ctx.on_screen(t, tol) if e["id"] not in own and _box_hit(e, boxes)
                    and any(textfold.same_reading(e["text"], x) for x in texts)] if side == "起点" else []
            ev = {"setter": [{"id": e["id"], "text": e["text"], key: e[key], "flags": e.get("flags") or [],
                              "same_reading": any(textfold.same_reading(e["text"], x) for x in texts)} for e in setter[:3]],
                  "same_on_screen": [{"id": e["id"], "text": e["text"], "t_start": e["t_start"]} for e in same[:3]],
                  "members": [lo, hi], "side": "start" if side == "起点" else "end"}
            who = (f"由事件 {setter[0]['id']}「{setter[0]['text'][:16]}」定" if setter else "没找到定下它的事件")
            alt = (f"；但同一句字（事件 {same[0]['id']}「{same[0]['text'][:16]}」）那一刻已在屏，多半是成员没收全" if same else "")
            word = "早于" if side == "起点" else "晚于"
            out.append(suspect("A2", it, off, f"{side}{word}成员 {off / 1e6:.2f} s，{who}{alt}；原文「{original(it)[:30]}」",
                               doc, evidence=ev))
    return out


def _near_reading(a: str, b: str) -> bool:
    """同一句字（`same_reading`），或者两边都不少于 `A3_NEAR_MIN` 字、只差个别字（读错一两个字）。"""
    if textfold.same_reading(a, b):
        return True
    x, y = textfold.bare(a), textfold.bare(b)
    return min(len(x), len(y)) >= A3_NEAR_MIN and SequenceMatcher(None, x, y).ratio() >= A3_NEAR_RATIO


def rule_a3(items: list[S.Item], doc: dict, ctx: Context) -> list[Suspect]:
    out = []
    tol = int(SAMPLE_TOL * doc["frame_us"])
    drawn: dict[int, S.Item] = {}
    for it in items:
        for e in members(it):
            drawn.setdefault(e["id"], it)
    for it in _lines(items):
        ms = members(it)
        own, boxes, texts = {e["id"] for e in ms}, [e["box"] for e in ms], [e["text"] for e in ms]

        def same_before(t: int) -> list[dict]:
            """`t` 之前一个容差以内还在屏、起点早于 `t` 超过容差的同一处同一句（采样级的读数常断成几段，段间隔一个采样间隔）。"""
            return [e for e in ctx.on_screen(t - tol, 0) if e["id"] not in own and t - e["t_start"] > tol
                    and _box_hit(e, boxes) and any(_near_reading(e["text"], x) for x in texts)]
        # 挨着起点的那段：挂过起点的都算；起点前一个容差内刚结束的只算没被别的条目画出的——被前一条画着、刚好在这里交接的
        # 是字幕稿里连着的两条（同一句换了一条），屏上没有空档，不是起点晚
        near = [e for e in same_before(it.start_us) if e["t_end"] > it.start_us or drawn.get(e["id"]) in (None, it)]
        if not near:
            continue
        chain = [min(near, key=lambda e: e["t_start"])]
        while prev := [e for e in same_before(chain[-1]["t_start"]) if e["t_start"] < chain[-1]["t_start"]]:
            chain.append(min(prev, key=lambda e: e["t_start"]))       # 顺着断开的同一句往前接，接到最早那段
        e = chain[-1]
        off = it.start_us - e["t_start"]
        by = next((drawn[x["id"]] for x in near if drawn.get(x["id"]) not in (None, it)), None)
        gap = all(x["t_end"] <= it.start_us for x in near)
        split = (f"（挨着起点的那段字已由 {fmt_t(by.start_us)}–{fmt_t(by.end_us)}「{' / '.join(by.lines)[:16]}」那一条画出，多半是同一句被拆成两条）"
                 if by is not None else
                 "（起点前刚断开：多是衣服上的字、名牌、按键提示这类一直挂着的字读得断断续续）" if gap else
                 "（挂过起点，那一刻没有字幕盖着这句字）")
        out.append(suspect("A3", it, off,
                           f"起点之前 {off / 1e6:.2f} s 同一处已有同一句字：事件 {e['id']}「{e['text'][:16]}」{fmt_t(e['t_start'])} 起在屏，"
                           f"{f'断成 {len(chain)} 段' if len(chain) > 1 else '一段'}挂到起点{split}；原文「{original(it)[:30]}」", doc,
                           evidence={"earlier": [{"id": x["id"], "text": x["text"], "t_start": x["t_start"], "t_end": x["t_end"]}
                                                 for x in reversed(chain)][:3],
                                     "segments": len(chain), "gap": gap,
                                     "drawn_by": None if by is None else {"start_us": by.start_us, "end_us": by.end_us,
                                                                          "text": " / ".join(by.lines)}}))
    return out


def rule_b1(items: list[S.Item], doc: dict, ctx: Context) -> list[Suspect]:
    out = []
    for it in _lines(items):
        ms = sorted(S.typing_events(members(it)), key=lambda e: e["t_start"])
        if len(ms) < 2:
            continue
        first = ms[0]
        rest = [e for e in ms[1:] if not tracksio.same_row(e["box"], first["box"])]
        if not rest:
            continue
        full = first.get("t_full") or first.get("t_full_sampled") or first["t_start"]
        full = max(full, it.start_us)
        gap = rest[0]["t_start"] - full
        if gap >= B1_GAP_US:
            out.append(suspect("B1", it, gap,
                               f"事件 {first['id']}「{first['text'][:16]}」（{len(textfold.bare(first['text']))} 字，n_obs {first.get('n_obs')}"
                               f"{'，UI' if {'ui_footprint', 'ruby'} & set(first.get('flags') or ()) else ''}）显出后 {gap / 1e6:.2f} s "
                               f"才出现下一行「{rest[0]['text'][:16]}」", doc,
                               evidence={"first": [first["t_start"], full], "next": rest[0]["t_start"]}))
    return out


def _group(it: S.Item) -> tuple:
    return ("layer", it.layer) if it.translated else ("region", it.region)


def _outliers(vals: list[tuple[tuple, int, float, S.Item, object]], ratio: float, both: bool
              ) -> list[tuple[float, float, S.Item, object]]:
    """(组, 起点, 量, 条目, 附带) → 和邻居中位数比离群的 (量, 中位数, 条目, 附带)。"""
    if len(vals) < MIN_ITEMS:
        return []
    overall = statistics.median(v[2] for v in vals)
    groups: dict[tuple, list] = defaultdict(list)
    for v in vals:
        groups[v[0]].append(v)
    out = []
    for g in groups.values():
        g.sort(key=lambda v: v[1])
        for k, (_, _, x, it, what) in enumerate(g):
            near = [v[2] for v in g[max(0, k - NEIGHBORS):k] + g[k + 1:k + 1 + NEIGHBORS]]
            med = statistics.median(near) if len(near) >= MIN_NEIGHBORS else overall
            if med > 0 and (x > ratio * med or (both and x < med / ratio)):
                out.append((x, med, it, what))
    return out


def rule_b2(items: list[S.Item], doc: dict, ctx: Context, skip: set[int] = frozenset(),
            typewriter: bool = True, fade: bool = True) -> list[Suspect]:
    frame_us = S.native_frame_us(doc)
    vals = []
    for it in _lines(items):
        if id(it) in skip:
            continue
        tw, _, _, rowt = S.item_effects(it, doc, frame_us, typewriter, fade)
        if rowt is not None:
            for k, (a, b) in enumerate(rowt):
                n = chars(it.row_events[k], doc["frame_us"], it.events)
                if b > a and n >= B2_MIN_CHARS:
                    vals.append((_group(it), it.start_us + a * 1000, (b - a) * 1000 / (n - 1), it,
                                 (f"第 {k + 1} 行", [it.start_us + a * 1000, it.start_us + b * 1000])))
        elif tw > 0:
            n = chars(it.events, doc["frame_us"])
            if n >= B2_MIN_CHARS:
                vals.append((_group(it), it.start_us, tw / (n - 1), it, ("", [it.start_us, it.start_us + tw])))
    return [suspect("B2", it, x,
                    f"{row}打字机 {x / 1000:.0f} ms/字，邻近中位数 {med / 1000:.0f} ms/字（{'慢' if x > med else '快'} {max(x, med) / min(x, med):.1f} 倍）",
                    doc, evidence={"side": "slow" if x > med else "fast", "median": med, "reveal": reveal})
            for x, med, it, (row, reveal) in _outliers(vals, B2_RATIO, both=True)]


def rule_b3(items: list[S.Item], doc: dict, ctx: Context, typewriter: bool = True, fade: bool = True) -> list[Suspect]:
    frame_us = S.native_frame_us(doc)
    vals = []
    for it in _lines(items):
        n = chars(it.events, doc["frame_us"])
        if n < B3_MIN_CHARS:
            continue
        tw, fin, _, _ = S.item_effects(it, doc, frame_us, typewriter, fade)
        dwell = it.end_us - (it.start_us + max(tw, fin))
        vals.append((_group(it), it.start_us, dwell / n, it, it.start_us + max(tw, fin)))
    out = []
    for x, med, it, shown in _outliers(vals, B3_RATIO, both=False):
        dwell = it.end_us - shown
        if dwell >= B3_MIN_US:
            out.append(suspect("B3", it, x, f"全字后停留 {dwell / 1e6:.1f} s，每字 {x / 1e6:.2f} s，邻近中位数 {med / 1e6:.2f} s/字", doc,
                               evidence={"median": med, "shown": shown}))
    return out


def check(items: list[S.Item], doc: dict, typewriter: bool = True, fade: bool = True) -> list[Suspect]:
    """全部规则，按时刻排。B2 跳过 A2 报了起点的条目（首字就是那个偏早的起点，必然显得慢）。"""
    ctx = Context(doc)
    early = {id(it) for it in _lines(items) if min(e["t_start"] for e in members(it)) - it.start_us > SAMPLE_TOL * doc["frame_us"]}
    out = rule_a1(items, doc, ctx) + rule_a2(items, doc, ctx) + rule_a3(items, doc, ctx) + rule_b1(items, doc, ctx) + \
        rule_b2(items, doc, ctx, early, typewriter, fade) + rule_b3(items, doc, ctx, typewriter, fade)
    return sorted(out, key=lambda s: (s.start_us, s.rule))


def fmt_t(us: int) -> str:
    """字幕稿的时刻写法 `H:MM:SS.cc`（Aegisub 里能直接跳过去）。"""
    cs = int(round(us / 1e4))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def span_hours(items: list[S.Item]) -> float:
    if not items:
        return 0.0
    return (max(it.end_us for it in items) - min(it.start_us for it in items)) / 3.6e9


def summary(sus: list[Suspect], hours: float) -> str:
    n = Counter(s.rule for s in sus)
    m = Counter(s.rule for s in sus if s.main)
    e = Counter(s.rule for s in sus if s.echoish)
    per = (lambda k: f"，每小时 {m[k] / hours:.1f}") if hours > 0 else (lambda k: "")
    echo = lambda k: f"，其中回显 {e[k]}" if e[k] else ""
    return "、".join(f"{k} {RULES[k]} {n[k]}（主轨 {m[k]}{per(k)}{echo(k)}）" for k in RULES if n[k]) or "无"


def report(sus: list[Suspect], hours: float, show: int = 30) -> str:
    """汇总一行 + 逐条（不沾回显的在前，各自按时刻）。"""
    lines = [f"时间疑点 {len(sus)} 条：{summary(sus, hours)}"]
    for s in sorted(sus, key=lambda s: s.echoish)[:show]:
        tag = ("" if s.main else "（主轨外）") + ("（回显）" if s.echoish else "")
        lines.append(f"{fmt_t(s.start_us)}–{fmt_t(s.end_us)}  {s.rule} {RULES[s.rule]}{tag}  「{s.text[:24]}」  {s.detail}"
                     + (f"  [{s.ref}]" if s.ref else "") + (f"  把握：{'、'.join(s.notes)}" if s.notes else ""))
    if len(sus) > show:
        lines.append(f"……另有 {len(sus) - show} 条（--show 调，--json 存全量）")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from flowocr.output.render import load_doc
    ap = argparse.ArgumentParser(description="列出阶段 3 字幕稿里起止时间疑似错的条目（只读产物）")
    ap.add_argument("product", metavar="产物", help="阶段 2 的产物：*-tracks.json 或匹配产物")
    ap.add_argument("--opt", action="append", default=[], metavar="K=V", help=f"阶段 3 的选项（{' / '.join(OPTS)}），照阶段 3 那行提示给的抄")
    ap.add_argument("--show", type=int, default=30, help="终端最多列多少条（默认 30）")
    ap.add_argument("--json", type=Path, help="完整清单存到这里")
    a = ap.parse_args(argv)
    opts = {}
    for kv in a.opt:
        if "=" not in kv:
            ap.error(f"--opt 要 K=V：{kv}")
        k, v = kv.split("=", 1)
        opts[k] = v
    try:
        modes = S.effect_modes(opts)                # 读产物之前就校验，拼错了马上报
    except ValueError as e:
        ap.error(str(e))
    items, doc, *_ = S.build_items(load_doc(Path(a.product)), opts, align=False)
    sus = check(items, doc, *modes)
    hours = span_hours(items)
    print(report(sus, hours, a.show))
    if a.json:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(json.dumps({"product": str(a.product), "opts": opts, "hours": hours,
                                      "suspects": [asdict(s) for s in sus]}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"-> {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
