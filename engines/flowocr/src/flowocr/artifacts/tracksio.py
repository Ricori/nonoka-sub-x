"""**一级产物只有一份**：富信息 JSON 的读、写、校验。

形状与契约的正式文档：`docs/architecture/artifacts.md`（需求原文与四轮复审的过程在
`output-format-plan（已归档）`）。以前一条链上散着三份
产物——`-tracks.json`（没过滤的 run）、逐区域 `.srt`（过滤和切分发生在"写 SRT"那一步）、
`-provenance.json`（版本戳），**信息在往下走的每一步都在丢**，两个用途看的是两份不同的
东西，回抠还得把过滤再抄一遍（audit-4 C6）。

现在的契约：

* `build_tracks` 写**一份** JSON，它是唯一的事实来源；
* **切分（cue）、常驻 UI 的判定、并带的结果都已经写在 JSON 里**；
* 导出器（`flowocr.output.export`）只做**投影**：按 flag 取舍、按 cue 排版，不重新判断。

`srtio` 是同一个思路（"一条 = 一个 cue"只有一处口径），这里照抄它的做法：
**读的时候就校验**，对不上直接抛，不静默少算。

## 形状

```text
schema / tag / meta / frame_us / size / window_sec / n_windows
provenance : git_head, code_fp, argv, args, main_track, main_srt, main_cues, main_candidates
regions[]  : index, label, lang, primary_score, n_windows_present, features,
             align?（left / center / right，叠加导出锚哪条边，flowocr.analyze.align 判的）,
             align_how?（by = stats / vote / none、n、spread、votes：判定依据）
tracks[]   : id("r00" / "main"), kind, region|regions, label, srt, ui_lines[], cues[]
             cues[] : id, t_start, t_end, events[]（事件 id，**顺序就是导出时的行序**）
events[]   : id, region, t_start, t_full, t_end, box, text, conf, n_obs,
             n_independent, flags[], boxes[]?（只有 moving 的才落）, texts[]?（可选）,
             t_full_sampled?（采样级"全字出现"，build_tracks.sampled_full；t_full 只由回抠填）,
             t_full_how?（回抠填：{"first", "full"} 各是 agree / single / none，全字还可能是 capped，见 typewriter_fuse）,
             t_full_end?（回抠填：完全显示结束，淡出开始的前一帧；没淡出时 ≈ t_end）, t_end_how?（curve / ncc / open / moving：open = 窗里没看到消失，t_end 只是下界；moving = 末段在平移、结尾窗给的终点早于采样级，t_end 留采样级、不填 t_full_end）
text_effect? : 回抠写的整段素材的效果统计：samples, verdict?（instant / typewriter / likely-typewriter；
             样本 < 8 时不写）, ms_per_char_median?, iqr_over_median?, fade_out_*?
```

`align` / `align_how` 可选：旧产物没有，不读对齐的下游（SRT、匹配、体检）照常能读；
叠加预设读到没有 `align` 的产物当场报错、提示重跑 build_tracks（`flowocr.output.script`）。

两处**故意**的设计，改之前先读：

1. **过滤不删事件，只打标。** 用途 1 要留、用途 2 要剔，同一份数据。
   常驻 UI 的判定是**逐轨**的（同一行在区域轨里够比例、并进主轨后不一定够），
   所以权威清单是 `tracks[].ui_lines`；`events[].flags` 里的 `ui_filtered`
   是"在它自己的区域轨里被剔了"的方便标记，别拿它去投影主轨。
2. **cue 里的事件顺序就是 SRT 的行序**，不要在导出器里重排：建轨按 `row_order` 排（先分行、行按纵向位置、行内从左到右），
   重排一次就对不上原产物了。
"""
from __future__ import annotations

import bisect
import json
import math
from pathlib import Path

SCHEMA = "flowocr-tracks/1"

ALIGNS = ("left", "center", "right")
"""`regions[].align` 的取值。"""


def parse_viewport(s: str | None) -> tuple[float, float, float, float] | None:
    """游戏画面在录像里的位置 `x,y,w,h`（录像像素；建轨的 `--viewport`、匹配的 `--ctx viewport=`）→ 四个数；没给是 None。"""
    if not s:
        return None
    try:
        v = tuple(float(x) for x in s.split(","))
    except ValueError:
        v = ()
    if len(v) != 4 or not all(math.isfinite(x) for x in v) or v[2] <= 0 or v[3] <= 0:
        raise ValueError(f"viewport={s!r} 要是 x,y,w,h 四个数（录像像素，宽高为正）")
    return v


def check_viewport(v: tuple[float, float, float, float], W: float, H: float) -> None:
    """游戏画面要整个在录像画面里，不然报错。"""
    x, y, w, h = v
    if x < 0 or y < 0 or x + w > W or y + h > H:
        raise ValueError(f"viewport={x:g},{y:g},{w:g},{h:g} 出了录像画面（{W:g}×{H:g}）")


def recorded_viewport(doc: dict | None) -> str | None:
    """建轨时 `--viewport` 记在 `provenance.viewport` 里的游戏画面位置，写回 `x,y,w,h`；没记是 None。
    匹配时没给 `--ctx viewport=` 就用它对版式文件（`analyze.textbox.locate`）：游戏画面在哪是这段录像的事，建轨时给一次，以后每次重跑匹配都带上。"""
    v = ((doc or {}).get("provenance") or {}).get("viewport")
    return ",".join(f"{x:g}" for x in v) if v else None

ROW_OVERLAP = 0.5
"""两个框算同一行：纵向重叠至少是较矮那个字高的这么多（`same_row`）。"""
ROW_H_RATIO = 1.7
"""两个框算同一行：字高比不超过这么多。和建轨里"同一行字高"的门同一个值（`build_tracks.REGION_H_RATIO` / `GROW_H_RATIO`）——
一个装着两行的高框不和旁边单行的框串成一行。"""
SAMPLE_SLACK = 1.5
"""采样级时刻的容差是这么多个采样间隔：采样点的实际间隔有抖动，名牌比正文早出现一个采样点的（yuka-f3 上 17 条，早 0.5002–0.5007 s，
`frame_us` 0.50018 s）卡在"一个间隔"的边上，它们也不是"被拉早"。时间疑点检测（`timing_check`）和正文块起点的修正（`scriptmatch.body_start`）共用。"""


def same_row(a, b) -> bool:
    """两个框（`[x0, y0, x1, y1]`）在不在同一行。"""
    ha, hb = a[3] - a[1], b[3] - b[1]
    lo, hi = min(ha, hb), max(ha, hb)
    if lo <= 0 or hi > ROW_H_RATIO * lo:
        return False
    return min(a[3], b[3]) - max(a[1], b[1]) >= ROW_OVERLAP * lo


def boxes_cross(a, b) -> bool:
    """两个框（`[x0, y0, x1, y1]`）相交（两个方向都有重叠）。"""
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


NAME_GAP = 3.0
"""名牌下沿到下面那行台词上沿最多隔几个名牌字高（`name_over_body` 用）。2026-11-05 量：
原神、星铁的名牌离台词 ≤ 1.5 个字高，星铁对话记录面板里的字在 5 以上；绝区零 zzz-s2（安比那段）的名牌离台词 2.1–2.2 个字高（63–67 px，名牌 30 px），
原来的 2.0 下匹配器收不进这些名牌、tracks 里 40 块有 21 块挂不上（3.0 时 3 块）。
从 2.0 放到 3.0：匹配器 11 段（四场整场、《魔裁》f1–f5、gi-s2、zzz-s2）的记录、选项和名牌以外的条目逐条不变，默认画出的名牌多 29 块，
除一块绝区零聊天面板的「ラミル」都是真名牌（绝区零的铃、哲、比利）；多收进来的界面字归 `stray_name`、默认不画。"""


def name_over_body(nb: list[float], bb: list[float]) -> bool:
    """名牌（框 `nb`）挂在这行正文（框 `bb`）上：正文在名牌中线以下、上沿离名牌下沿不超过 `NAME_GAP` 个字高，
    左右有重叠或左边缘相距不超过两个字高（居中的名牌、左对齐的名牌都算）。
    只看几何、不看名字（不为单个病例加特判）；并排一行的角色列表、离得远的键位和菜单标题对不上这个形状。
    匹配器认开头行时比同一条 cue 里的事件、给名牌分组（`scriptmatch.name_roles`）时比叠加块的首行；阶段 3 给 tracks 的名牌分档（`output.script.mark_names`）时比主轨的句条目。"""
    h = max(1, nb[3] - nb[1])
    if not (bb[1] >= (nb[1] + nb[3]) / 2 and bb[1] - nb[3] <= NAME_GAP * h):
        return False
    return min(nb[2], bb[2]) - max(nb[0], bb[0]) > 0 or abs(nb[0] - bb[0]) <= 2 * h


def block_head(rows: list[list[float]], top: list[float] | None = None) -> list[float]:
    """一块台词给名牌比的那一行（`name_over_body` 的 `bb`）：上下取 `top`（默认首行），左右取整块所有行的范围。
    左右只看首行会漏：绝区零的台词左对齐、名牌居中，首行短（「こんばんは、リンちゃん。」「じゃあまた。」）时首行够不到名牌正下方，
    2026-11-05 整场 15 块真名牌这样没挂上；按整块比，四场整场、《魔裁》、gi-s2、zzz-s2 没有多挂上有译文的界面字。"""
    top = rows[0] if top is None else top
    return [min(r[0] for r in rows), top[1], max(r[2] for r in rows), top[3]]


def rows_below(rows: list[list[float]], top: list[float]) -> list[list[float]]:
    """`rows` 里从 `top`（`rows` 里的一行）往下连着排的几行，含 `top`：下一行的上沿离已收那几行的下沿不超过一个 `top` 的字高、
    左右和已收的范围有重叠。给还没成块的一堆行（匹配器认开头行时同一条 cue 的事件）凑出 `top` 所在的那块台词，再交 `block_head`——
    整条 cue 的范围会把同屏右下角的按钮、水印也算进台词块的左右。"""
    h = max(1.0, top[3] - top[1])
    got, x0, x1, y1 = [top], top[0], top[2], top[3]
    for r in sorted((r for r in rows if r is not top and r[1] > top[1]), key=lambda r: r[1]):
        if r[1] - y1 > h:
            break
        if r[1] >= y1 - 0.5 * h and min(x1, r[2]) - max(x0, r[0]) > 0:
            got.append(r)
            x0, x1, y1 = min(x0, r[0]), max(x1, r[2]), max(y1, r[3])
    return got


NAME_INERTIA_US = 2_000_000
"""同一块名牌前后两次出现隔不超过这么久（μs）就连成一串（`plate_runs`）：换下一句台词、OCR 漏读一两帧时名牌会断开一下。
2026-11-05 在 11 段匹配产物上量：2 s 把星铁 3 块（姬子、丹恒：下面的台词被认成对话记录面板里的旧行，或没认上）、绝区零 1 块
（名牌比台词早出现半秒）挂回来，没有多挂上有译文的界面字；放到 5 s 只再多 2 块半秒的。"""


def same_plate(a: list[float], b: list[float]) -> bool:
    """两个名牌框是同一处：中心横向差不超过一个字高、上沿差不超过半个字高（字高取小的那个）。"""
    h = max(1.0, min(a[3] - a[1], b[3] - b[1]))
    return abs((a[0] + a[2]) - (b[0] + b[2])) / 2 <= h and abs(a[1] - b[1]) <= 0.5 * h


def plate_runs(items: list[tuple[int, int, list[float], str]], gap_us: int = NAME_INERTIA_US) -> list[int]:
    """名牌块按"是不是同一块名牌的几次出现"连成串：`items[i]` = (起, 止, 框, 名字)，名字相同（有译名用译名，没有用归一后的读数）、
    同一处（`same_plate`，和串里上一块比）、和串里最晚的终点隔不超过 `gap_us` 的归一串。返回每块所在串的编号（串里最早那块的下标）。
    给名牌分档的时间惯性用（`spread`）：一串里有一块挂在台词上，整串都是那块名牌。"""
    rid = list(range(len(items)))
    by: dict[str, list[int]] = {}
    for i, it in enumerate(items):
        by.setdefault(it[3], []).append(i)
    for ids in by.values():
        open_: list[list] = []                       # [串编号, 上一块的框, 最晚的终点]
        for i in sorted(ids, key=lambda i: (items[i][0], i)):
            s, e, box, _ = items[i]
            open_ = [o for o in open_ if s - o[2] <= gap_us]          # 按起点扫：断开太久的串不会再接上
            run = next((o for o in open_ if same_plate(o[1], box)), None)
            if run is None:
                open_.append([i, box, e])
            else:
                rid[i] = run[0]
                run[1], run[2] = box, max(run[2], e)
    return rid


def spread(flags: list[bool], runs: list[int]) -> list[bool]:
    """一串（`plate_runs`）里有一块为真，整串都为真。"""
    good = {runs[i] for i, f in enumerate(flags) if f}
    return [f or runs[i] in good for i, f in enumerate(flags)]


STACK_GAP = 0.5
"""名牌和称号上下相邻的判据：两框竖直方向的空隙（叠了为负）在称号框高的 ±这么多倍以内（原神真名牌在 −0.22–0.36）。"""
STACK_SMALLER = 0.9
"""称号的框高不超过名字框高的这么多倍：称号的字比名字小一档（原神 0.42–0.86，一例 0.97 是名字框偏矮）。
被误标成名牌的 UI 上下两行多是同一字号（星铁菜单「固有名詞」「37/37」0.97、日志面板「選択肢」「姫子」0.91），靠这条挡住。"""


def stacked_on(b, c) -> bool:
    """框 `b` 是叠在框 `c` 上面或下面的小一号的字（称号对名字）：竖直上紧挨着（`STACK_GAP`）、字明显比 `c` 小（`STACK_SMALLER`）、
    两框的中心互相落在对方框里。不看上下：原神的称号在名字下面，判据不靠这个。"""
    h = b[3] - b[1]
    gap = max(b[1] - c[3], c[1] - b[3])
    a = (c[0] + c[2]) / 2
    return (h <= STACK_SMALLER * (c[3] - c[1]) and abs(gap) <= STACK_GAP * h
            and b[0] <= a <= b[2] and c[0] <= (b[0] + b[2]) / 2 <= c[2])


def stack_partners(items: list[tuple[int, int, list[float]] | None]) -> list[int | None]:
    """名牌下面的称号认它的名字：`items[i]` = (起, 止, 框)，不参加的给 None。一条在时间上和它重叠、`stacked_on` 成立的条目里取重叠最久的
    （一样久取下标小的），返回它的下标；找不到、或不参加是 None。字大的那块（名字）自己没有伙伴。
    匹配器给名牌分名字 / 称号（`scriptmatch.name_roles`）、阶段 3 给 tracks 分称号（`output.script.mark_names`）、阶段 3 / 4 让称号跟名字共用锚轴（`output.layout.stacked_axes`）都用这一份。
    按起点扫、只和同时在屏的比，整场几千块名牌也是线性的量级。"""
    out: list[int | None] = [None] * len(items)
    best = [0] * len(items)
    active: list[int] = []
    for i in sorted((i for i, it in enumerate(items) if it is not None), key=lambda i: (items[i][0], i)):
        s, e, b = items[i]
        active = [j for j in active if items[j][1] > s]
        for j in active:
            ov = min(e, items[j][1]) - s
            for x, y in ((i, j), (j, i)):
                if (ov > best[x] or ov == best[x] and out[x] is not None and y < out[x]) and stacked_on(items[x][2], items[y][2]):
                    best[x], out[x] = ov, y
        active.append(i)
    return out


class EventIndex:
    """事件按起点排好，按时间窗取。比 `span_us` 还长的事件另存一份、每次都带上：它们的起点可能早在窗口之前，却一直挂过窗口。
    所以 `window(t - span_us, t)` 里 `t_end > t` 的就是 `t` 那一刻在屏的全部事件，不用扫整份。
    正文块起点的修正（`scriptmatch.body_start`）和时间疑点检测（`timing_check`）共用。"""

    def __init__(self, events: list[dict], span_us: int):
        self.span_us = span_us
        self.events = sorted(events, key=lambda e: e["t_start"])
        self.starts = [e["t_start"] for e in self.events]
        self.long = [e for e in self.events if e["t_end"] - e["t_start"] > span_us]

    def window(self, lo: int, hi: int) -> list[dict]:
        """起点在 `[lo, hi]` 里的事件，加上起点在 `lo` 之前的长事件。"""
        return [e for e in self.long if e["t_start"] < lo] + \
            self.events[bisect.bisect_left(self.starts, lo):bisect.bisect_right(self.starts, hi)]

    def on_screen(self, t: int, slack: int = 0) -> list[dict]:
        """`t` 那一刻在屏的事件（起点放宽 `slack`：起点在 `t + slack` 之前、终点在 `t` 之后）。"""
        return [e for e in self.window(t - self.span_us, t + slack) if e["t_end"] > t]


def row_index(boxes: list) -> list[int]:
    """每个框归到第几行：按纵向中心从上往下贪心归行，一个框要和这一行的**每个**成员都 `same_row` 才进这一行，
    不做传递闭包——一个字高介于两行之间的框（称号、略大的一行）同时压着上下两行时，并查集会把两行串成一行，
    拼行时横向间隙是负数、两行字被横着拼在一起。行按成员纵向中心的均值从上往下编号；结果只看几何，和输入顺序无关。"""
    n = len(boxes)
    groups: list[list[int]] = []
    for i in sorted(range(n), key=lambda i: ((boxes[i][1] + boxes[i][3]) / 2, boxes[i][0], i)):
        g = next((g for g in groups if all(same_row(boxes[i], boxes[j]) for j in g)), None)
        if g is None:
            groups.append([i])
        else:
            g.append(i)
    order = sorted(groups, key=lambda m: (sum((boxes[i][1] + boxes[i][3]) / 2 for i in m) / len(m), min(m)))
    out = [0] * n
    for k, m in enumerate(order):
        for i in m:
            out[i] = k
    return out


class SchemaError(ValueError):
    """产物和契约对不上。**抛出去，不要静默降级**——静默降级正是这个项目栽过的那类事故。"""


def dump(path: Path, doc: dict) -> dict:
    """写一份产物（写前先自校验：坏产物不许落盘）。"""
    doc = {"schema": SCHEMA, **doc}
    validate(doc)
    Path(path).write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return doc


def load(path: Path) -> dict:
    """读一份产物并校验。"""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    validate(doc)
    return doc


def validate(doc: dict) -> None:
    """结构自检。**只查"自己说的和自己有的对不对得上"**，不查数值好坏。"""
    if doc.get("schema") != SCHEMA:
        raise SchemaError(f"schema 不是 {SCHEMA}：{doc.get('schema')!r}"
                          "（旧产物不做兼容，重跑 build_tracks）")
    for key in ("meta", "frame_us", "size", "provenance", "regions", "tracks", "events"):
        if key not in doc:
            raise SchemaError(f"缺字段 {key}")
    n = len(doc["events"])
    for i, ev in enumerate(doc["events"]):
        if ev.get("id") != i:
            raise SchemaError(f"events[{i}].id={ev.get('id')}——id 必须就是下标")
        if ev["t_end"] < ev["t_start"]:
            raise SchemaError(f"events[{i}] 时间反了")
        if ev.get("t_full") is not None and not (ev["t_start"] <= ev["t_full"] <= ev["t_end"]):
            raise SchemaError(f"events[{i}].t_full 不在 [t_start, t_end] 里")
        if ev.get("t_full_sampled") is not None and not (ev["t_start"] <= ev["t_full_sampled"] <= ev["t_end"]):
            raise SchemaError(f"events[{i}].t_full_sampled 不在 [t_start, t_end] 里")
        if ev.get("t_full_end") is not None and not (ev["t_start"] <= ev["t_full_end"] <= ev["t_end"]):
            raise SchemaError(f"events[{i}].t_full_end 不在 [t_start, t_end] 里")
    regions = {r["index"] for r in doc["regions"]}
    for r in doc["regions"]:
        if "align" in r and (r["align"] not in ALIGNS or "align_how" not in r):
            raise SchemaError(f"regions[{r['index']}].align={r['align']!r}：只能取 {ALIGNS}，而且要带 align_how")
    for ev in doc["events"]:
        if ev["region"] not in regions:
            raise SchemaError(f"events[{ev['id']}].region={ev['region']} 没有对应的区域")
    seen: set[str] = set()
    # **非区域轨装的必须是区域轨里已有的事件**（2026-09-20 复审第 1 条）：
    # `kind="main"`（并带的聚合投影）和 `kind="nameplate"`（名牌轨）都是
    # **同一批 run 的另一种投影**——"打标不删"的核心不变量就是这个子集关系。
    # 放在 schema 校验里而不是守卫里：每次 `load` 都核一遍，覆盖面更广。
    in_region = {eid for tr in doc["tracks"] if tr.get("kind") == "region"
                 for cue in tr["cues"] for eid in cue["events"]}
    for tr in doc["tracks"]:
        if tr["id"] in seen:
            raise SchemaError(f"轨 id 重复：{tr['id']}")
        seen.add(tr["id"])
        if tr.get("kind") != "region":
            extra = {eid for cue in tr["cues"] for eid in cue["events"]} - in_region
            if extra:
                raise SchemaError(
                    f"{tr['id']}（kind={tr.get('kind')}）里有 {len(extra)} 个事件不在任何区域轨里"
                    f"（如 {sorted(extra)[:5]}）——非区域轨只能是区域轨的另一种投影")
        for cue in tr["cues"]:
            if not cue["events"]:
                raise SchemaError(f"{tr['id']} 的 cue {cue['id']} 是空的")
            if not set(cue.get("partial") or ()) <= set(cue.get("hide") or ()) <= set(cue["events"]):
                raise SchemaError(f"{tr['id']}/{cue['id']}：partial ⊂ hide ⊂ events 不成立")
            for eid in cue["events"]:
                if not 0 <= eid < n:
                    raise SchemaError(f"{tr['id']}/{cue['id']} 指向不存在的事件 {eid}")


def track(doc: dict, track_id: str) -> dict:
    for tr in doc["tracks"]:
        if tr["id"] == track_id:
            return tr
    raise SchemaError(f"没有这条轨：{track_id}（有的是 {[t['id'] for t in doc['tracks']]}）")


def main_track(doc: dict) -> dict | None:
    """provenance 指定的那条主轨。**不许 glob 猜文件名**（2026-09-07 的教训）。"""
    tid = doc["provenance"].get("main_track") or ""
    return track(doc, tid) if tid else None


def main_regions(doc: dict) -> set[int]:
    """**整块**并进主轨的区域（主轨是真实区域时就是它自己）；没有主轨是空集。"""
    main = main_track(doc)
    if not main:
        return set()
    return set(main.get("regions") or ([main["region"]] if "region" in main else []))


def in_main(ev: dict, regions: set[int]) -> bool:
    """一个事件在不在主轨上——**按事件判**，`regions` 给 `main_regions(doc)`（调用方算一次）。

    并带按窗补进来的事件（`build_tracks --main-band-unit window`）只有所在区域的一部分进了主轨，那块区域不在 `regions` 里，
    靠事件上的 `band_merged` 标认；只按区域判的话，它在叠加 / 字幕稿里被当成主轨外，在 `choice_items` 里又会被当成选项候选。"""
    return ev["region"] in regions or "band_merged" in (ev.get("flags") or ())


def cue_lines(doc: dict, tr: dict, cue: dict, drop_filtered: bool = True) -> list[dict]:
    """一条 cue 在这条轨上要导出的事件。**投影，不是判断**：

    `drop_filtered=True`（SRT、`default` 叠加用）剔常驻 UI；`False`（`dev` / `default_all` 叠加用）全给，
    由调用方按 flag 换外观。

    剔谁由**两条判据**合起来定（2026-09-20）：

    * `ui_lines` —— 文本那条，**逐轨**（同一行在区域轨里够比例、在主轨里不一定够）；
    * 事件上的 `ui_footprint` 标 —— 框位那条，**全局**（这个位置上的东西就是 UI，
      和它落在哪条轨无关）。框位判决**没法用"哪些文本"表达**——同一个文本在别的位置是正文。

    ⚠ 两者**作用域不同，别混成一个 flag**：混了之后导出器会把"区域轨里判的 UI"
    带进主轨，主轨 592 → 541 条、和 `build_tracks` 自己写的对不上（守卫当场抓到）。
    旧产物（只有 ui_lines、事件没标）照旧能投影。

    cue 上的 `hide`（同句合并留作成员的半截行和杂行，`build_tracks.seg_hide`）只在这条 cue 里不给：`drop_filtered=True` 全剔，
    `False` 只剔其中的半截行 `partial`（它是另一行的早先状态，不是另一段字），杂行照给、由调用方按 UI 换外观。

    事件上的 `ruby` 标（振り仮名，`build_tracks.ruby_runs`，全局、按几何）也只在 `drop_filtered=True` 时剔：
    它不是 UI，是正文的注音，SRT / 匹配器 / `default` 叠加不单独画它；可判据是几何的、会误标
    （任务 HUD 里的假名、面板小标题），所以全部文字的视图（`False`）照给，由调用方按标换外观——
    分类写进产物，藏不藏是输出的策略（2026-09-26 审计）。
    """
    ui = set(tr.get("ui_lines") or ())
    gone = set(cue.get("hide") or ()) if drop_filtered else set(cue.get("partial") or ())
    evs = [doc["events"][i] for i in cue["events"] if i not in gone]
    return [e for e in evs
            if not (drop_filtered and (e["text"].strip() in ui
                                       or {"ui_footprint", "ruby"} & set(e.get("flags") or ())))]


ROW_JOIN_GAP = 3.0
"""同一行里相邻两段拼成一行的横向间隙上限，单位是行高（`cue_rows`）：破折号断开的那一例间隙约两个字宽；
并带主轨里左右并排的两个对话框、表格的两栏远在这之外。"""


def _cjk(ch: str) -> bool:
    import unicodedata
    return unicodedata.east_asian_width(ch) in ("W", "F")


def join_row_text(a: str, b: str) -> str:
    """同一行的两段字拼起来：交界处有一边是全角字（中日韩）就直接相连，否则空一格。"""
    a, b = a.rstrip(), b.lstrip()
    if not a or not b:
        return a + b
    return a + b if _cjk(a[-1]) or _cjk(b[0]) else a + " " + b


def rows_of_events(evs: list[dict]) -> list[dict]:
    """一组事件按可见的行拼好：`row_index` 分行，行内从左到右，横向间隙 ≤ `ROW_JOIN_GAP` 倍行高的相邻两段拼成一行
    （框取并集、文字按 `join_row_text` 相接），隔得更远的同一行各自成行。返回 `[{"events", "box", "text"}]`，按行从上往下、行内从左往右。"""
    if not evs:
        return []
    row = row_index([e["box"] for e in evs])
    order = sorted(range(len(evs)), key=lambda i: (row[i], evs[i]["box"][0], i))
    out: list[dict] = []
    for i in order:
        e = evs[i]
        b = list(e["box"])
        last = out[-1] if out else None
        if last is not None and last["row"] == row[i]:
            h = max(b[3] - b[1], last["box"][3] - last["box"][1])
            if b[0] - last["box"][2] <= ROW_JOIN_GAP * h:
                last["events"].append(e)
                last["box"] = [min(last["box"][0], b[0]), min(last["box"][1], b[1]), max(last["box"][2], b[2]), max(last["box"][3], b[3])]
                last["text"] = join_row_text(last["text"], e["text"])
                continue
        out.append({"row": row[i], "events": [e], "box": b, "text": e["text"]})
    for r in out:
        del r["row"]
    return out


def cue_rows(doc: dict, tr: dict, cue: dict, drop_filtered: bool = True) -> list[dict]:
    """一条 cue 要导出的事件（`cue_lines`）按可见的行拼好（`rows_of_events`）：字幕稿的一句一条、阶段 3 的 SRT 都用它。"""
    return rows_of_events(cue_lines(doc, tr, cue, drop_filtered))


def ui_flagged(ev: dict) -> bool:
    """这个事件身上**有没有任何一种 UI 标**。

    ⚠⚠ **只给"作用域就是事件自己的区域轨"的地方用**——`flowocr.analyze.align` 按区域做统计时。
    叠加 ASS 按轨取事件、按那条轨的 `ui_lines` 标 UI，不用它。**`cue_lines` 绝对不能用它**：`ui_filtered` 是
    **逐轨**判决留在事件上的方便标记（它是在**区域轨**那一遍盖的），
    而 `ui_lines` 才是权威清单；拿它当全局判据，就会把"区域轨里判的 UI"带进主轨。

    这个坑 2026-09-20 一天之内踩了两次：第一次是把两种判决混进同一个 flag
    （主轨 592 → 541，守卫抓到）；第二次是我在审计七修 ASS 那条路时，
    顺手让 `cue_lines` 也走了这个函数——`e-yuka-f5-w8n5` 的并带主轨
    **build 写 457 条、重导只剩 329 条**（复审抓到；上一次的守卫没挡住，
    因为它跑的那段主轨就是一条区域轨，`ui_filtered` 和 `ui_lines` 恰好等价）。

    **判决的作用域写在这里，别再合并**：
    `ui_lines`（逐轨、文本）→ `cue_lines`；`ui_footprint`（全局、框位）→ 两边都算；
    `ui_filtered`（逐轨判决在事件上的影子）→ **只在没有轨的时候**当参考。
    """
    return bool({"ui_filtered", "ui_footprint"} & set(ev.get("flags") or ()))


def events_of_region(doc: dict, index: int) -> list[dict]:
    return [e for e in doc["events"] if e["region"] == index]


def regions_with_runs(doc: dict) -> list[dict]:
    """"按区域看事件"的视图：每个区域一份 `runs`（就是它的事件，按 `t_start` 排）。

    **这不是兼容层**——旧的 `regions[].runs` 那份产物已经没了；
    只关心几何和文本的工具（画框、覆盖率、名牌判据…）用这个视图最省事，
    而且**拿到的是同一批事件对象**，改不改都指向 `doc["events"]` 里那一份。
    """
    by_region: dict[int, list[dict]] = {}
    for ev in doc["events"]:
        by_region.setdefault(ev["region"], []).append(ev)
    return [{**r, "runs": sorted(by_region.get(r["index"], []),
                                 key=lambda e: e["t_start"])} for r in doc["regions"]]


def event_times(doc: dict) -> dict[int, tuple[int, int]]:
    """回抠**之前**把每个事件的首尾抄一份。`refresh_cue_bounds` 要拿它配对边界。"""
    return {e["id"]: (e["t_start"], e["t_end"]) for e in doc["events"]}


def refresh_cue_bounds(doc: dict, before: dict[int, tuple[int, int]] | None = None) -> None:
    """事件的时间被改过（回抠）之后，把每条 cue 的首尾按成员重算。

    **只算时间，不重新切分**——切分是 `build_tracks` 那一步定下来的，
    回抠只更新时间信息（audit-4 C6 要的就是这个：回抠不该有第二套导出规则）。

    三条规矩，每条都是一次复审换来的：

    1. **一条 cue 的边界只认它自己的成员**（第三轮复审）。靠 `before`
       （回抠前的快照，`event_times()`）配对：当初落在这条边界上的成员，
       现在挪到哪儿了。起点取最早、终点取最晚——**cue 要盖住自己的成员**。
       ⚠ 只按"旧时间戳相同"去合并是错的：别的轨、别的 cue 里碰巧同一时刻
       开始/结束的事件会被当成同一个切点，于是**只回抠了 r00，r01 的 cue 也跟着变**；
       `raw` 档里两条各自回抠到 0.75 / 1.25 s，也会被一起压到 0.75 s。
    2. **真正共享切点的两段要一起挪**（第二、四轮复审）。判据有两半：同一条轨内
       `前一段的旧终点 == 后一段的旧起点`，**并且两段有共同成员**——也就是有一条
       run 跨过了这个切点，切点是"别的东西变了"切出来的。
       ⚠ 只看时间相接是错的：相接的两条**独立**字幕之间是真边界，
       回抠说前一条 1.8 s 消失、后一条 1.9 s 出现，中间 0.1 s 屏幕本来就是空的，
       绑成一个切点就会**在空白期提前显示**（第四轮复审的反例，
       它同时推翻了这里原先那句"两者都不会显示屏幕上没有的东西"）。
       两边给出的新值不一致时取**最早**那个证据；代价是跨过切点的那条被提前切掉一点。
    3. **切点必须单调**：每条边界各自配对，相邻两个完全可能一个往后挪、一个往前挪，
       不夹一道就会出负时长的 cue。

    没有 `before` 就一个边界都不动——那时无从知道是谁生成了这条边界。

    为什么不能按"成员生命周期的 min/max"重算（第一轮复审）：`segment` 档在
    **每个首尾边界**都切一刀，于是一条常驻行（`Auto` 挂 40 秒）同时是 20 条 2 秒 cue
    的成员，min/max 会把 **20 条字幕全部撑成 0–40 秒**——切分等于被丢掉了。
    """
    if not before:
        return
    for tr in doc["tracks"]:
        cues = tr["cues"]
        if not cues:
            continue
        order = sorted(range(len(cues)), key=lambda k: (cues[k]["t_start"], cues[k]["t_end"]))
        old = [(c["t_start"], c["t_end"]) for c in cues]
        lo: list[int | None] = [None] * len(cues)
        hi: list[int | None] = [None] * len(cues)
        for k, cue in enumerate(cues):
            evs = [doc["events"][i] for i in cue["events"] if doc["events"][i]["id"] in before]
            s_cand = [e["t_start"] for e in evs if before[e["id"]][0] == old[k][0]]
            e_cand = [e["t_end"] for e in evs if before[e["id"]][1] == old[k][1]]
            lo[k] = min(s_cand) if s_cand else None
            hi[k] = max(e_cand) if e_cand else None
        # 相邻且**共享切点**的，两边合成一个值；顺带保证它不早于前一段的起点
        members = [{doc["events"][i]["id"] for i in c["events"]} for c in cues]
        for u, v in zip(order, order[1:]):
            # 判据是**有成员跨过这个切点**，不是"旧时间戳首尾相接"（第四轮复审）：
            # 相接的两条**独立**字幕（A 0–2、B 2–4，没有共同成员）之间是真边界，
            # 回抠说 A 1.8 s 消失、B 1.9 s 出现，中间那 0.1 s 屏幕本来就是空的——
            # 硬把它们绑成一个切点，B 就会在空白期提前显示。
            # 反过来，A 挂 0–10 s、B 只有 0–2 s 时，切点在 A 的生命期**里面**，
            # 那才是同一个切点：不一起挪，A 会在交界处重复显示。
            if old[u][1] != old[v][0] or not (members[u] & members[v]):
                continue
            both = [x for x in (hi[u], lo[v]) if x is not None]
            if not both:
                continue
            shared = max(min(both), lo[u] if lo[u] is not None else old[u][0])
            hi[u] = lo[v] = shared
        for k in order:
            a = lo[k] if lo[k] is not None else old[k][0]
            b = hi[k] if hi[k] is not None else old[k][1]
            # 夹只夹**这一条自己**（首尾不许倒挂）。**不许跨 cue 夹**：
            # `raw` / `cue` 档的 cue 本来就可以时间重叠——两条字幕同屏是常态，
            # 跨 cue 夹单调会把后一条的起点硬推到前一条的终点上（第三轮复审的第二个反例）。
            cues[k]["t_start"], cues[k]["t_end"] = a, max(a, b)


NOT_KNOBS = ("--outdir", "--tag", "--viewport")
"""不属于"这一臂的旋钮"的建轨选项（同 `build_tracks.RUN_SPECIFIC`）：驱动（head2head.sh / gametext_eval.sh）自己填的输出位置和标签，
加上这段录像的游戏画面位置（`--viewport`，建轨不用、只记）。后者不剥的话，驱动的 BUILD_ARGS 没带它就会判成参数变了、重建，
重建出来的轨道丢了人给的位置；剥了的代价是旧轨道没记位置时驱动加 `--viewport` 不触发重建，要 `FORCE=1`。"""


def knob_args(argv: list[str], obs: str | None = None) -> list[str]:
    """命令行去掉不算旋钮的那几样（`NOT_KNOBS`，空格和 `=` 两种写法；给了 `obs` 再去掉第一个等于它的位置参数）。
    产物那一侧（`build_args`）和驱动这一趟要的 BUILD_ARGS 都过它再比：只剥一侧的话，BUILD_ARGS 带 `--viewport` 就次次判成参数变了。"""
    out, obs_seen, i = [], obs is None, 0
    while i < len(argv):
        x = argv[i]
        if x in NOT_KNOBS:
            i += 2
            continue
        if not obs_seen and x == obs:
            obs_seen = True
        elif not any(x.startswith(o + "=") for o in NOT_KNOBS):
            out.append(x)
        i += 1
    return out


def build_args(prov: dict) -> list[str]:
    """provenance 的 `argv` 去掉不算旋钮的那几样（obs 位置参数、`NOT_KNOBS`，`knob_args`），剩下的就是
    这一臂的 BUILD_ARGS，驱动拿它和这一趟要的（同样过 `knob_args`）比，对不上就重建。

    以前两个驱动各写 `argv[5:]`，依赖"恰好按 obs / --outdir / --tag 的顺序、用空格形式传"
    （audit-6）；这里按名剥，obs 按 provenance 里记的那个字符串认。
    ⚠ provenance 里的 `argv` 经过 `provenance.portable`（绝对路径记成相对数据根 / `~/…`）：驱动传的 BUILD_ARGS 若含绝对路径，
    要先过同一个函数再比，否则白重建一次（现有驱动都传相对路径，不受影响）。"""
    return knob_args(list(prov["argv"]), prov.get("obs"))


def load_regions(path: Path) -> tuple[dict, list[dict]]:
    """`load` + `regions_with_runs`，一步到位。"""
    doc = load(path)
    return doc, regions_with_runs(doc)


def _main(argv: list[str] | None = None) -> int:
    """给 shell 脚本用的取值口：

        python -m flowocr.artifacts.tracksio <tracks.json>            # 整个 provenance（JSON）
        python -m flowocr.artifacts.tracksio <tracks.json> main_srt   # 其中一个字段

    **驱动脚本别再自己 `json.load` 一份**——校验也就跟着丢了。
    产物坏了这里会抛，退出码非零，脚本当场停，而不是拿空字符串往下走。
    """
    import argparse

    ap = argparse.ArgumentParser(description="读 -tracks.json 里的 provenance")
    ap.add_argument("tracks")
    ap.add_argument("key", nargs="?", default="")
    a = ap.parse_args(argv)
    prov = load(Path(a.tracks))["provenance"]
    if not a.key:
        print(json.dumps(prov, ensure_ascii=False, indent=1))
    else:
        v = prov.get(a.key, "")
        print(json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
