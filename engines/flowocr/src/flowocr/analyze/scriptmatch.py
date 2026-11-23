"""通用匹配器：把主轨的每条 cue 对上剧本原文，产出用途 2 的**最终产物**。

方案与取舍写在 matcher 计划，这里只记实现上的判断：

* **匹配失败是一等公民**（owner，product-goals 用途 2）：对不上就原样留 OCR 文本、标 `how: "none"`，
  不硬塞一个最像的。导出字幕时这类条目照旧出现，只是没被替换。
* **时间一律取 cue 的**。原文替换掉 OCR 文本之后，时间轴是唯一保留下来的自有信息（product-goals 那张表），
  不从剧本合成，也不在这里做打字机延迟修正。
* **判据不另写一份**：cue 的读法（`game_align.variants`：去掉开头 0–3 行名牌 / 称号 × 去不去掉末行键位）、
  按时段圈候选、第二遍的整行包含认领，全部**直接调 `game_align`**。量尺和产品共用判据、不共用入口——
  拿匹配后的文本去量匹配器是循环论证（docs/dev-guide/verification.md「归因：问『这一处发生了什么』」）。
* **一条 cue 可以带第二条剧本行**（`extra`）：屏幕上一条 cue 装两行剧本是常事（选项按钮 + 正文、
  并带并进两个说话人）。产品侧**不拆 cue**——拆开要分时间，而时间是我们唯一的自有信息。
  **导出契约（owner 2026-09-14，product-goals 第 16 条）**：主产物是**叠加 ASS**，`extra` 是它独立的一层
  （`LAYERS`，层只管怎么画）；各层盖哪些块在 `overlay_items` 里判定、写进 JSON 的 `overlay.items`，每块带显示协议的组
  （`DISPLAY` / `group_of`），画不画由预设按组开关，导出器只投影。SRT 是拍平投影，只出主 `ref`（`body()`），所以"覆盖"（`ref` ∪ `extra`，
  和量尺同口径）**大于 SRT 里出现的剧本行数**，两个数都打出来，别混着读。

用法：见 `CLI_DESCRIPTION`。
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import NamedTuple

from flowocr import paths  # noqa: E402
from flowocr import placeholders as PH  # noqa: E402
from flowocr import textfold  # noqa: E402
from flowocr.artifacts import evalkit  # noqa: E402
from flowocr.artifacts import matchedio  # noqa: E402
from flowocr.artifacts.matchedio import LAYERS  # noqa: E402   层的名单：判定（这里）和投影（overlay）两边都用
from flowocr.analyze import echo as EC  # noqa: E402
from flowocr.analyze import game_align as GA  # noqa: E402
from flowocr.analyze import nameink as NI  # noqa: E402
from flowocr.analyze import script_align as SA  # noqa: E402
from flowocr.analyze import textbox as TB  # noqa: E402
from flowocr.artifacts import srtio  # noqa: E402
from flowocr.artifacts import tracksio  # noqa: E402

CODE_FP_FILES = ("src/flowocr/analyze/scriptmatch.py", "src/flowocr/artifacts/evalkit.py", "src/flowocr/analyze/game_align.py",
                 "src/flowocr/analyze/script_align.py", "src/flowocr/artifacts/srtio.py", "src/flowocr/artifacts/tracksio.py",
                 "src/flowocr/artifacts/matchedio.py", "src/flowocr/placeholders.py",
                 "src/flowocr/analyze/matchers/gametext.py", "src/flowocr/analyze/gamescript.py", "src/flowocr/textfold.py", "src/flowocr/richtext.py",
                 "src/flowocr/analyze/gtdbundle.py", "src/flowocr/paths.py", "src/flowocr/analyze/echo.py",
                 "src/flowocr/extract/framegrid.py", "src/flowocr/analyze/nameink.py", "src/flowocr/provenance.py",
                 "src/flowocr/analyze/textbox.py")
"""本文件 + 它 import 的本地模块（同 `build_tracks.CODE_FP_FILES` 的口径，仓库相对路径）。
再往上的两层各自有指纹、写在 provenance 里：剧本是 `ref_code_fp`（gamescript），轨是 `tracks_code_fp`。
`gamescript`（连同它 import 的本地模块）也列在这里：匹配时还调 `gamescript.cnorm`（检索归一，依赖 `textfold`），`ref_code_fp` 记的是产剧本那次的代码。"""
TR_EXTRA = tuple(k for k in matchedio.TEXT_KEYS if k not in ("jp", "cn"))
"""剧本行除 `jp` / `cn` 外带进认领、选项条目的文本字段（有才带）。"""
CLI_DESCRIPTION = """把轨的每条 cue 对上剧本原文（带评测口径的报表；通用入口是 flowocr-match）。

对不上的原样留 OCR 文本、标 how: "none"；时间一律取 cue 的。一条 cue 可以带第二条剧本行（extra），
SRT 只出主认领，所以报表里的"覆盖"会大于 SRT 里出现的剧本行数。叠加层的判定写进 --out 的 overlay.items。

    python -m flowocr.analyze.scriptmatch out/gametext/hsr-ref.json --subs out/gs-hsr/hsr-tracks.json \\
        --out out/gametext/hsr-matched.json --srt tmp/hsr-cn.srt --lang cn"""
# `*-matched.json` 的格式（版本、校验、正文取法）在 `flowocr.artifacts.matchedio`——阶段 3 的预设也要读它，
# 而 output 只许依赖 artifacts（2026-09-22 Codex 审计 P2）。这里 re-export，老调用方不变。
SCHEMA = matchedio.SCHEMA
MatchedSchemaError = matchedio.MatchedSchemaError
REQUIRED = matchedio.REQUIRED
load_matched = matchedio.load

CONF_HIGH, CONF_MED = 0.9, 0.75
"""改写的置信度分档（按 cue 读法与剧本行的相似度）。`contain` 一律算高——整行都在 cue 里。"""

SUSPECT = 0.85
""""可疑改写"的门，**必须严格高于 `--fuzzy`**。

接受判据本身就是"相似度 ≥ fuzzy"（`game_align.run`），而这里记的分是同一个量
（`sim_of` 和 `run` 用同一组 cue 读法、同一个 `SequenceMatcher.ratio`），`pick_variant` 又只在组里取最大——
所以拿 `--fuzzy` 当这条线，"低于门的改写"**结构上恒为 0**，量不了任何东西。
和 `unmatched_subs` 那个"幽灵条目上限"一个形状（docs/dev-guide/verification.md「报数口径」：指标的门不能等于接受判据的门）：
2026-09-11 初版把它写成 `CONF_MED`（= 默认 fuzzy 0.75），四场整片读出来全是 0，被当成"没有可疑改写"。
0.85 是拍的，只用来圈出**要摆帧的尾巴**，不是合格线。"""

NOT_BY_SIM = ("contain", "context")
"""相似度不是判据的两种认领，不进可疑改写和改写幅度分布：`contain` 整行都在 cue 里；`context` 是夹缝认领（`game_align.claim_flanked`），
凭上下两行的认领加"读法是这一行的一截"认上，`ref.score` 照旧记诚实的相似度（`でも` 对 `でも…` 0.57）。"""


def sim_of(raw: str, text: str, heads: frozenset[str] = frozenset()) -> float:
    """cue 的几种读法里和这条剧本行最像的那个的相似度。

    ⚠ **`heads` 必须和匹配时用的那一份相同**（2026-09-18 审计的 P2，已修）：
    `game_align.score` / `.run` 算候选读法时是 `variants(raw, heads)`——**开头行（名牌 / 称号）先剥掉**；
    而这里原来调的是 `variants(raw)`，**没剥**。于是同一条 cue「匹配时用的读法」和「报分时用的读法」不是一回事：
    名牌留在读法里，相似度被压低。审计给的两条实例：报 0.364 / 0.419，按真正用的读法是 **0.923 / 0.857**。
    影响面：`可疑改写 < 0.85` 这张清单、`conf` 的 high/med/low 分档、以及 `--feed-panel strict` 的那道门
    （它就是拿这个 score 判"弱认领"）。所以这不是"报表好看一点"的事，是**判决也跟着错**。
    """
    best = 0.0
    for v in GA.variants(raw, heads):
        best = max(best, SequenceMatcher(None, v, SA.norm(text), autojunk=False).ratio())
    return round(best, 3)


def pick_variant(groups: dict[str, list[int]], script: list[SA.Entry], i: int, raw: str,
                 heads: frozenset[str] = frozenset()) -> int:
    """认上的这一行若属于变体组（绝区零两位主角的两种说法），**输出屏幕上的那一种**：
    组里谁和这条 cue 最像就换成谁。

    量尺那边一组只算一个分母条目、认哪一种都算命中（`gamescript.fold_variants`）；
    产品这边不行——2026-09-11 整场绝区零实测，低分改写里大半是"屏幕上是アキラ的说法、
    输出成了リン的"。只在库里明确成组的行之间挑，挑不到别的台词上。"""
    group = groups.get(script[i].key)
    if not group:
        return i
    return max(group, key=lambda k: (sim_of(raw, script[k].text, heads), -k))


def variant_index(ref: GA.Ref, script: list[SA.Entry]) -> dict[str, list[int]]:
    """组里每一行的 key -> 这一组全部成员的下标（只有 ≥2 个成员的组才进）。"""
    idx = {e.key: k for k, e in enumerate(script)}
    members: dict[str, list[int]] = {}
    for k, rep in ref.variant_of.items():
        if k in idx:
            members.setdefault(rep, [idx[rep]] if rep in idx else []).append(idx[k])
    return {script[m].key: g for g in members.values() if len(g) > 1 for m in g}


PANEL_MIN_SCORE = 0.85
"""面板样 cue 的认领要多强才算（`--feed-panel strict`，对照臂，默认不开）。

当初的立论是"弱认领 + 面板形状"才是拿旧台词冒充此刻的特征（单看形状会误伤原神"台词 + 选项按钮"）；
2026-09-18 审计后撤回：定性过的错认分数 0.857–0.974，0.85 的门挡不住，hsr 上还误挡 3 条真正文。
不采纳的理由见 docs/architecture/defaults.md 3 节。`contain` 不受这条限制：整行原文就在 cue 里，相似度不是它的判据。"""


def match(ref: GA.Ref, cues: list[srtio.Cue], fuzzy: float,
          panel_of: list[bool] | None = None, panel_mode: str = "keep", main_of: list[bool] | None = None,
          step: float = 0.0) -> tuple[list[dict], dict]:
    """每条 cue 一条记录。返回 (记录, `game_align.score` 的统计)。`main_of`：每条 cue 在不在主轨上（`cue_main`），第四遍认领只给主轨上的 cue。
    `step`：tracks 的采样间隔（秒），夹缝认领判"同刻"用（`game_align.claim_flanked`）；0 = 不知道。

    `panel_mode="strict"` + `panel_of`：面板样 cue 的**弱认领**（score < `PANEL_MIN_SCORE`）退回"没认上"。
    `how`：`exact` / `fuzzy`（主认领，打字机半截和读到一大段也记 `fuzzy`）/ `contain`（只靠整行包含认上）/ `context`（夹缝认领）/ `none`。
    """
    script = GA.fresh_script(ref)
    subs = [(c.start, "\n".join(c.lines), "") for c in cues]
    st = GA.score(ref, script, subs, fuzzy, [c.end for c in cues], main_of, step)
    cn = {ln["key"]: {"cn": ln.get("cn"), **{k: ln[k] for k in TR_EXTRA if ln.get(k)}}
          for u in ref.doc["units"] for ln in u["lines"]}
    unit = {ln["key"]: u["uid"] for u in ref.doc["units"] for ln in u["lines"]}
    sn = [SA.norm(e.text) for e in script]
    heads = st.get("heads") or frozenset()     # **匹配用的那一份开头行**，报分要用同一份（见 sim_of）
    vgroups = variant_index(ref, script)
    ctx = set(st.get("context_subs", ()))
    out = []
    n_strict = [0]
    for i, c in enumerate(cues):
        raw = "\n".join(c.lines)
        h, extra = st["sub_hit"][i], st["sub_extra"][i]
        rec = {"start": round(c.start, 3), "end": round(c.end, 3), "ocr": raw,
               "how": "none", "ref": None, "extra": [], "cues": [i]}
        if h >= 0:
            h = pick_variant(vgroups, script, h, raw, heads)
            # `heads` 同样要带上（2026-09-19 自查补漏）：上一处把 `sim_of` 修了，**这一行漏了**——
            # 于是"名牌 + 正文"的 cue 剥开之后明明和剧本行逐字相同，却被记成 `fuzzy`。
            # 判据只有一份的意思是**这一份的每个调用点**都用同一种读法
            rec["how"] = "context" if i in ctx else "exact" if sn[h] in GA.variants(raw, heads) else "fuzzy"
            rec["ref"] = ref_rec(script[h], cn, unit, sim_of(raw, script[h].text, heads))
        elif extra:
            j = pick_variant(vgroups, script, extra[0], raw, heads)
            rec["how"] = "contain"
            rec["ref"] = ref_rec(script[j], cn, unit, sim_of(raw, script[j].text, heads))
        for j in extra[1:] if h < 0 else extra:
            rec["extra"].append(ref_rec(script[j], cn, unit, sim_of(raw, script[j].text, heads)))
        if (panel_mode == "strict" and panel_of and panel_of[i] and rec["ref"]
                and rec["how"] != "contain" and rec["ref"]["score"] < PANEL_MIN_SCORE):
            n_strict[0] += 1
            rec["how"], rec["ref"], rec["extra"] = "none", None, []
        r = rec["ref"]
        rec["conf"] = ("none" if not r else "high" if rec["how"] == "contain" or r["score"] >= CONF_HIGH
                       else "med" if r["score"] >= CONF_MED else "low")
        out.append(rec)
    if n_strict[0]:
        print(f"[喂入] --feed-panel strict：面板样 cue 的弱认领（score < {PANEL_MIN_SCORE}）"
              f"退回没认上 {n_strict[0]} 条")
    return out, st


MERGE_GAP = 2.0
"""相邻两条 cue 认的是同一条剧本行、且间隔不超过这么多秒，就并成一条（`--merge-gap 0` 关掉）。

**不是可选的美化**：主轨的 cue 按整条多行文本切，名牌一抖就是一条新 cue
（整场绝区零近一半的相邻 cue 正文相同，game-text-corpus 报告），
换成原文之后就是同一句话连打五遍。留间隔上限是为了不把**真实重播**（隔一阵又演一次）并掉。"""


LOG_PANEL_MIN = 8
"""真记录面板（`log_panels`）：别的台词的读数至少这么多内容字（`cnorm` 之后）。名牌、血条上的角色名（「カチーナ」「パイモン」）
恰好和某条只有名字的台词同字，4 字的门挡不住（检测器三场修复计划 U3 量的）。"""
LOG_PANEL_IN = 0.8
"""真记录面板：别的台词的读数有这么多字按顺序落在那条台词里（`gamescript.contained`）。"""


LOG_PANEL_OLD = EC.LOG_OLD
"""真记录面板：别的台词要比这条 cue 早这么多秒就上过屏（剧本锚点），同回显判定的"旧行"（`echo.LOG_OLD`）。"""
LOG_PANEL_NEAR = 4.0
"""真记录面板：别的台词的读数离本句正文的纵向距离不超过这么多个（正文的）行高——同一个列表里的上下几条。
四场整片上真记录面板最远 3.4 个行高；挂在台词框上方的任务提示横幅 17.3（hsr 1:23:23）。"""


def log_panels(ref: GA.Ref, cues: list, evs_of: list[list[dict]], panel_of: list[bool], recs: list[dict],
               gap: float = MERGE_GAP) -> list[bool]:
    """每条 cue 是不是**真记录面板**（对话记录、回看面板这类一屏好几句旧台词的列表）：面板样（`panel_like`），认上了一句台词，
    而且有一个不是本句正文（`body_of`）的读数 `LOG_PANEL_IN` 落在**别的旧台词**里、就在本句正文上下几行（`LOG_PANEL_NEAR` 个行高以内）。
    "别的旧台词"：这一刻窗里的另一条带内台词，≥ `LOG_PANEL_MIN` 个内容字（`cnorm` 之后，读数和那一行都要够长），
    剧本锚点（`gamescript.sequence` 的排序时刻：第一次在 obs 里读到，变体组折到代表行）比 cue 起点早 `LOG_PANEL_OLD` 秒以上。

    为什么不直接用面板样（检测器三场修复计划 U3，四场整片上量的）：绝区零的真台词框里混进了 T 恤印字「QICAI JIE」也被标成面板样，
    拿它拆记录，真台词会被判成对话记录、SRT 里只剩尾巴。几道门各挡一类：
    * 带内、够长：名牌、血条上的角色名恰好和某条只有名字的台词同字（gi1 血条「カチーナ」、gi2 名牌「パイモン」）；
    * 是旧台词：台词框末尾跟着弹出的选项菜单（hsr 0:23:00，选项在文本包里也是台词，可它这时才第一次出现）；
    * 在上下几行：挂在台词框上方的任务提示横幅（hsr 1:23:23「探偵手帳を開いて…」，在文本包里算台词、12 分钟前就上过屏，离正文 17 个行高）。
    要求"至少两条别的台词"不行：hsr 0:15:22、1:53:51、1:56:50 的记录面板 cue 里只读到一条。

    **延续**：同一来源里紧接着一条真记录面板（间隔 ≤ `gap`，和 `merge_runs` 同一个 `--merge-gap`）、认的是同一句、这句的读数和面板里这句的读数**在同一处**（框相交）的 cue，
    也是记录面板——记录界面还开着，只是这一刻没被标成面板样（hsr 1:54:36–1:54:59「ただの直感ですが」，不延续的话拆出来判 `replay`、SRT 多出 23 s）。
    台词框里的重显和记录列表里那一行不在同一处（hsr 0:49:28：台词框 y≈856、记录行 y≈929），不延续。
    `recs` 是并条之前每条 cue 一条的记录（`match` 的输出）。"""
    out = [False] * len(cues)
    if not any(panel_of):
        return out
    heads = GA.head_lines([(c.start, "\n".join(c.lines), "") for c in cues], GA.band_lines(ref.script))
    cn = [GA.GS.cnorm(e.text) for e in ref.script]
    rep = lambda k: ref.variant_of.get(k, k)   # noqa: E731
    first = EC.anchors(ref.doc["sequence"], rep)

    def old_line(j: int, t: float, own: str) -> bool:
        k = ref.script[j].key
        return (GA.band(ref.script[j]) and rep(k) != own and len(cn[j]) >= LOG_PANEL_MIN
                and first.get(rep(k), t) < t - LOG_PANEL_OLD)

    def body(i: int) -> list[dict]:
        r = recs[i]
        return body_of(evs_of[i], heads, r["ref"]["jp"], r["ref"].get("jp_tpl") or "") if r["ref"] else []

    def near(e: dict, rows: list[dict]) -> bool:
        return any(max(0, b["box"][1] - e["box"][3], e["box"][1] - b["box"][3])
                   <= LOG_PANEL_NEAR * max(1, b["box"][3] - b["box"][1]) for b in rows)
    for i, c in enumerate(cues):
        if not panel_of[i] or not recs[i]["ref"]:
            continue
        mine = body(i)
        ids, t, own = {e["id"] for e in mine}, c.start, rep(recs[i]["ref"]["key"])
        reads = [q for e in evs_of[i] if e["id"] not in ids and len(q := GA.GS.cnorm(e["text"])) >= LOG_PANEL_MIN and near(e, mine)]
        if not reads:
            continue
        lines = [j for j, ws in enumerate(ref.windows) if any(lo <= t <= hi for lo, hi in ws) and old_line(j, t, own)]
        out[i] = any(GA.GS.contained(q, cn[j]) >= LOG_PANEL_IN for q in reads for j in lines)
    last: dict = {}                                    # (来源, key) -> 最近一条真记录面板（含延续）的 (cue 下标, 这句的读数)
    for i, c in enumerate(cues):
        r = recs[i]
        if not r["ref"]:
            continue
        k = (c.source, rep(r["ref"]["key"]))
        mine = body(i)
        if not out[i] and k in last:
            j, theirs = last[k]
            out[i] = (c.start - cues[j].end <= gap and c.start >= cues[j].start
                      and any(tracksio.boxes_cross(a["box"], b["box"]) for a in mine for b in theirs))
        if out[i]:
            last[k] = (i, mine)
    return out


def merge_runs(recs: list[dict], gap: float, src_of: list | None = None, log_panel_of: list[bool] | None = None) -> list[dict]:
    """相邻、同一条剧本行的记录并成一条：时间取首尾，OCR 文本留最长的那条（信息最全）。

    "相邻"按**来源区域**各自判（`src_of[cue 下标]` = 那条 cue 来自哪条区域轨；不给就是一个来源）：`--feed nonoise` 把各区域的 cue
    按时间交错着喂（`Cue.source` 是区域轨的 SRT 名），同一句话被一闪而过的杂项切成两条时，中间常夹着别的区域的 cue，按喂入序列判相邻就并不上——
    gi2 整场的重复认领 165 次里 122 次是这种间隔 ≤ 2 s 的（followups-evidence-0926 报告）。只并同一区域的，
    主对话和阅读面板里的同一句不会并成一个叠加块。`--feed main` 只有一个来源，结果和按序列判相同。

    ⚠ 并出来的 `ocr`（最长那条）和 `ref.score`（最像那次）可以来自不同的 cue；低分那条的可疑改写报警随合并消失，
    所以 `run_match` 另报合并前的条数（`suspect_raw`）。同一来源里的几条 cue 也不一定在同一处，
    叠加块按位置另切（`position_segments`）。

    **真记录面板**（`log_panel_of`，`log_panels`）和别的 cue 不并（检测器三场修复计划 U3）：玩家看完一句马上打开对话记录时，
    记录面板里同一句和台词框那条相接，并成一条的话面板读数进了正文成员——打字机拖到记录打开、终点挂到记录关掉（hsr 0:49:28、1:56:39）。
    "最近一条"仍按来源记，记录面板也算：隔着记录面板的前后两条不并。按（来源, 是不是记录面板）分开记的话，记录面板区域里两段没标成面板样的
    同一句读数会隔着面板并成一条（hsr 1:54:35–1:55:18，43 s），回显判定只看记录起点那一刻、判成 `other`，SRT 多出一条。"""
    if gap <= 0:
        return recs
    out: list[dict] = []
    last: dict = {}                                   # 来源 -> 这个来源在 out 里最近的一条
    lp = (lambda x: bool(log_panel_of[x["cues"][0]])) if log_panel_of else (lambda x: False)   # noqa: E731
    for r in recs:
        src = src_of[r["cues"][0]] if src_of is not None else None
        p = out[last[src]] if src in last else None
        same = (p and p["ref"] and r["ref"] and p["ref"]["key"] == r["ref"]["key"]
                and r["start"] - p["end"] <= gap and lp(p) == lp(r))
        if not same:
            out.append({**r, "n_cues": 1})
            last[src] = len(out) - 1
            continue
        p["end"] = r["end"]
        p["n_cues"] += 1
        p["cues"] = p["cues"] + r["cues"]
        if len(r["ocr"]) > len(p["ocr"]):
            p["ocr"] = r["ocr"]
        if r["ref"]["score"] > p["ref"]["score"]:     # 认的是同一行，留最像的那次的分
            p["ref"], p["how"], p["conf"] = r["ref"], r["how"], r["conf"]
        seen = {e["key"] for e in p["extra"]}
        p["extra"].extend(e for e in r["extra"] if e["key"] not in seen)
    return out


def ref_rec(e: SA.Entry, cn: dict, unit: dict, score: float) -> dict:
    """认上的剧本行：`jp` 是匹配用的原文（删了占位），`jp_tpl`（有占位时才有）留着占位、日文输出用它，`cn` 是留着占位的译文；
    `jp_rich` / `cn_rich`（有样式时才有）是带样式的那两份（`matchedio.TEXT_KEYS`）。"""
    return {"key": e.key, "kind": e.kind, "status": e.status, "unit": unit.get(e.key), "jp": e.text,
            **(cn.get(e.key) or {"cn": None}), "score": score}


body = matchedio.body
"""一条 cue 的正文取法（`flowocr.artifacts.matchedio.body`）：匹配上用剧本原文，否则原样留 OCR。"""


FEEDS = ("main", "nonoise", "all")
"""**喂给匹配器的是哪些文本**（owner 2026-09-17，product-goals 第 18 条）。

owner 把匹配器定为"特化的聚类规则 + 文本替换"，**"被匹配器吸收"本身就是一条分类判据**——
那就不该在它前面再插一道有损过滤。而 `main` 正是那道过滤：主轨 = "按分数挑一个区域 + 并带"的投影。

* `main`（默认，现状口径）：只喂主轨；
* `nonoise`：喂**除 `noise` 之外的全部区域轨**（= owner 说的"保留全部非噪音的 OCR 文本"）；
* `all`：全部区域轨，连 `noise` 一起。

量过的账在 game-text-corpus 报告：11 段没有一段变差，四个整场 +25~+69 命中，
新吸收的抽 8 条摆帧 8/8 是真吸收。**2026-09-20 owner 把默认从 `main` 翻成 `nonoise`**
（defaults.md §1.4；它顺带解掉了 `--slot-reassign` 的主要代价——主轨口径下丢的那批行，
`nonoise` 下 f2 看得见 64/66、f4 看得见 37/37）。⚠ 那次翻转的**代价侧当天补量是负的**：
gi2 整场上重复认领涨、待 owner 重新决定，数字见 defaults.md §1.4 那张表。
"""


def feed_from_doc(doc: dict, mode: str, panel: str = "keep") -> tuple[list, list[list[dict]], list[bool]]:
    """从一级产物**投影**出要喂的 cue 流 + 每条 cue 的事件（`mode != "main"` 时用）。

    **投影，不判断**（artifacts.md 第 1 条）：切分、常驻 UI 的剔除、并带都已经写在 JSON 里，
    这里只做三件事——挑轨、按 `cue_lines` 取该导出的事件、按时间排序。
    取舍和 `export.export_srt` 逐条对齐（剔完常驻 UI 为空的 cue 跳过）；**cue 的文字按可见的行拼**（`tracksio.rows_of_events`，
    同字幕稿）：`cue_lines` 是事件序，同一行被 det 切成的几段按框的上沿排，排出来是乱的——hsr「船の行き先は…境界 /
    「渡画泉隠」 / と呼ばれる場所よ。」三段在一行上，cue 读成「と呼ばれる場所よ。 / 「渡画泉隠」 / 船の行き先は…」，
    对这句只有 0.68（2026-10-05，匹配后漏行逐条追查）。返回的事件仍是 `cue_lines` 那一份。

    ⚠ **要排除的是"聚合轨"（`kind == "main"`），不是 `provenance.main_track` 指的那条**
    （2026-09-18 审计抓到的 P1）：`build_tracks` 只在**真的并了带**的时候才另建一条 `id="main"` 的聚合轨；
    没并带时 `main_track` 直接指向一条**真实区域**（`r00` 那种），按 id 排就把正文整条扔了——
    只有一条字幕区域的产物上 `nonoise` / `all` 会输出 0 条 cue。按 `kind` 排只扔掉真正重复的那份投影。

    `panel="drop"` 时把 `build_tracks` 标了 `panel_like` 的 cue 剔掉（对话记录面板那种：
    文本是真的、时间是错的，matcher 计划）。**判定是 build_tracks 做的，这里只按标取舍**
    （artifacts.md 第 1 条）；旧产物没有这个标，剔不掉，会打印一行提示。
    第三个返回值是逐 cue 的"是不是面板样"，给 `panel="strict"` 用（见 `PANEL_MIN_SCORE`）。
    """
    if mode not in FEEDS:
        raise SystemExit(f"--feed 只能取 {'/'.join(FEEDS)}：{mode}")
    rows: list[tuple[int, int, list[dict], str]] = []
    n_panel = 0
    n_agg = 0                              # 被白名单挡下的轨（聚合投影 / 名牌轨 / 以后别的）
    for tr in doc["tracks"]:
        # **白名单：只喂区域轨。** 别的轨都是同一批 run 的另一种投影，喂进来就是喂两遍：
        #   `kind="main"`      聚合投影（并带的那条）；
        #   `kind="nameplate"` 名牌轨（2026-09-20 加的，和区域轨**有意重叠**）——
        #                      它独立成条喂进匹配器的代价见 methodology-audit-3：
        #                      只有名字的 cue 会去抢含名字的剧本行，重复认领涨 2.2~7.0 倍。
        # ⚠ 原来这里是**黑名单**（只排 main），名牌轨一加就漏了。黑名单挡不住"以后又加一种轨"。
        if tr.get("kind") != "region":
            n_agg += 1
            continue
        if mode == "nonoise" and tr.get("label") == "noise":
            continue
        for cue in tr["cues"]:
            if panel == "drop" and cue.get("panel_like"):
                n_panel += 1
                continue
            evs = tracksio.cue_lines(doc, tr, cue)
            if evs:
                rows.append((cue["t_start"], cue["t_end"], evs, tr["srt"],
                         bool(cue.get("panel_like"))))
    rows.sort(key=lambda r: (r[0], r[1]))
    if panel == "drop":
        n_flag = sum(1 for tr in doc["tracks"] for c in tr["cues"] if c.get("panel_like"))
        print(f"[喂入] --feed-panel drop：剔掉 {n_panel} 条面板样 cue"
              + ("（这份产物里一个 `panel_like` 标都没有——是 build_tracks 打标之前产的？）"
                 if n_flag == 0 else ""))
    cues = [srtio.Cue(a / 1e6, b / 1e6, tuple(r["text"] for r in tracksio.rows_of_events(evs)), src)
            for a, b, evs, src, _ in rows]
    return cues, [evs for _, _, evs, _, _ in rows], [pl for *_, pl in rows]


def cue_events(doc: dict, cues: list) -> list[list[dict]]:
    """主轨每条**导出了的** cue 的事件，和 SRT 一条对一条（`export.export_srt` 跳过剔完常驻 UI 为空的 cue，这里照做）。
    条数或起点对不上就抛——叠加字幕要拿框，对错一条就整片错位。"""
    tr = tracksio.main_track(doc)
    if tr is None:
        raise SystemExit("tracks.json 没有主轨")
    pairs = [(cue, evs) for cue, evs in ((c, tracksio.cue_lines(doc, tr, c)) for c in tr["cues"]) if evs]
    if len(pairs) != len(cues):
        raise SystemExit(f"主轨 cue 对不上 SRT：tracks 里导出了 {len(pairs)} 条，SRT 有 {len(cues)} 条")
    bad = sum(1 for (cue, _), c in zip(pairs, cues) if abs(cue["t_start"] / 1e6 - c.start) > 0.002)
    if bad:
        raise SystemExit(f"主轨 cue 起点和 SRT 对不上 {bad} 条（SRT 不是这份 tracks.json 导出的？）")
    return [evs for _, evs in pairs]


BODY_IN_REF = 0.6
"""正文事件的字要有这么多落在剧本原文里才算正文（`body_of`）。"""


def body_of(evs: list[dict], heads: frozenset[str], ref_text: str = "", tpl: str = "") -> list[dict]:
    """一条 cue 里要被盖住的**正文**事件：内容 ≥3 字、不是开头行（名牌），并且字**落在剧本原文里**。

    最后这条是 2026-09-12 摆帧抓的：hsr 2:18 翻页提示 ▼ 被 OCR 读成 `un2ra` / `Y02R2`（5 个内容字），
    混进正文——框被撑大，打字机"全字出现"取到它最后一次出现的时刻，一句台词逐字显了 12 秒。
    同一行的"开头行：台词"格式（`アテア：コホンッ。`）先剥掉名字再比，同匹配时的读法（`GA.strip_heads`）：
    认领凭剥过的读法认上了，这里不剥，名字占了一半的字，正文就被筛光（疑似漏字幕判断器 2026-10-07 摆帧）。
    `tpl`：剧本行留着占位的原文（`jp_tpl`，玩家名等）。屏上填进了名字，读数比原文长（`旅人のねるめろ(本物)だよ` 对原文 `旅人のだよ。`），
    读数的字落在原文里的比例不够；这时读数要**合模板的形状**（`fits_template`）也算（同一场摆帧，选项被筛光）。"""
    rt = GA.GS.cnorm(ref_text)

    def is_body(e: dict) -> bool:
        if SA.norm(e["text"]) in heads:
            return False
        text = e["text"]
        if (m := GA.INLINE_NAME.match(text)) and SA.norm(m.group(1)) in heads:
            text = m.group(2)
        q = GA.GS.cnorm(text)
        if evalkit.content_len(text) >= 3:
            return not rt or GA.GS.contained(q, rt) >= BODY_IN_REF or bool(tpl and fits_template(q, rt, tpl))
        # 1–2 字的短台词（`「未着」……`，hsr 2:10 漏叠的那条）：字全在剧本原文里、而且占了原文内容的一半以上才算，
        # 挡住的是 `回` / `A` 这类碰巧落在长句里的单字垃圾
        return bool(q and rt) and GA.GS.contained(q, rt) >= 0.99 and len(q) >= 0.5 * len(rt)
    return [e for e in evs if is_body(e)]


def fits_template(q: str, rt: str, tpl: str) -> bool:
    """读数 `q`（归一后）合不合带占位那一行的形状：原文（归一后、删了占位的 `rt`）的字有 `BODY_IN_REF` 按顺序落在读数里，
    **而且**读数里没对上原文的字最多连成占位个数那么多段——多出来的只能是填进占位的名字。
    只看前一条，原文删掉占位后很短，一条长的无关读数也凑得够（`旅人のだよ` 对 `そうだよ、旅人の言う通りだ` 是 0.8，S4 评审）。"""
    if not q or not rt or GA.GS.contained(rt, q) < BODY_IN_REF:
        return False
    hit = [False] * len(q)
    for b in SequenceMatcher(None, q, rt, autojunk=False).get_matching_blocks():
        hit[b.a:b.a + b.size] = [True] * b.size
    runs = sum(1 for i, h in enumerate(hit) if not h and (i == 0 or hit[i - 1]))
    return runs <= len(PH.PLACEHOLDER_RE.findall(tpl))


def rows_of(body: list[dict]) -> list[list[float]]:
    """正文事件按 cy 分行，每行一个框（行内事件框的并集），从上到下（分行的判据在 `textbox.row_groups`，原文整块按同一份分）。"""
    return [box for box, _ in TB.row_groups(body)]


DISPLAY = {
    "groups": {
        "dialogue": {"tier": "always", "label": "台词"},
        **matchedio.NAME_GROUPS,
        "choice": {"tier": "on", "label": "选项按钮"},
        "offband": {"tier": "on", "label": "字幕带外的台词（含绝区零的选项按钮、居中旁白）"},
        "cutscene": {"tier": "on", "label": "过场字幕"},
        "narration": {"tier": "on", "label": "黑屏旁白"},
        matchedio.SPECIAL: {"tier": "on", "label": "特殊上屏形式的台词与提示（横幅、战斗喊话、白字提示…）"},
        "ui": {"tier": "off", "label": "界面文字（技能说明、帖子、成就…）"},
        "elsewhere": {"tier": "off", "label": "主轨外别处重现的台词（对话记录面板等）"},
        "panel": {"tier": "off", "label": "阅读面板长文本"},
        "unmatched": {"tier": "off", "label": "喂给了匹配器、没画成译文的文字"},
        "unfed": {"tier": "off", "label": "没交给匹配器的文字"}},
    "fill": {"tier": "on"}}
"""游戏文本包匹配器的显示声明（显示协议，`matchedio._check_display`；owner 2026-10-06 D1 / D4、10-07 确认）：
台词直接进 default；选项、带外的台词、过场字幕、黑屏旁白、特殊上屏形式的台词与提示（`LOOSE_SPECIAL` 那条的说明，owner 2026-11-07）、名牌默认进、可关；界面文字、别处重现的台词、没挂在台词上的名牌样文字（`name_roles`，owner 2026-11-05）、
阅读面板、没画成译文的文字默认不进、可开。
`elsewhere` 是选项层那条路（`choice_items`：主轨外、逐字等于剧本行、没被认领）找到的台词——2026-10-07 摆帧多是对话记录面板里的旧行，
也有短台词撞上界面里的同名字样（`カチーナ！` 对队伍列表的名字）；协议之前它们在 default 里也不画。
填位（认领了、但 OCR 没读到这句）默认照画。"""
LOOSE_CUTSCENE = ("Comic", "Cinema")
"""绝区零散条目里的过场字幕（`role` 是 `head`）：漫画过场、片中字幕。"""
LOOSE_SPECIAL = ("Tips", "MiddleTips")
"""绝区零散条目里游戏的提示 / 喊话系统出的字（`role` 是 `head`），归 `special`（owner 2026-11-07，special-lines 计划）：
`Tips` 是野外 / 战斗中的喊话、系统通知、操作教程，`MiddleTips` 是战斗提示横幅，都在画面上方——对应原神语音字幕里 style 不是 `Normal` 的那些
（`ReminderUI`：横幅、战斗喊话、白字提示、教程式提示，同样归 `special`）。上屏形式特殊，所以用单独的组、字幕稿里单独的样式，默认画、不进 SRT。
其余散条目（技能说明、帖子、成就、装载提示…）是界面文字。"""


def group_of(kind: str, status: str | None, role: str | None) -> str:
    """剧本行归哪个组（`DISPLAY`）：按**类型**判（处境不改组——没播段里的黑屏旁白仍是旁白），只有位置判在字幕带外的台词另归 `offband`。
    认领打组和检测器的适配层都调它，判据只这一份。"""
    if kind == "Choice":
        return "choice"
    if kind == "BlackScreen":
        return "narration"
    if kind == "ReminderUI" or (kind == "Loose" and role in LOOSE_SPECIAL):
        return matchedio.SPECIAL
    if kind == "Loose":
        return "cutscene" if role in LOOSE_CUTSCENE else "ui"
    if status == "OffBand":
        return "offband"
    return "dialogue"


def mark_display(recs: list[dict], cues: list, group_fn) -> None:
    """给每次认领打显示协议的 `group`（`group_fn(认领)`）和 `fill`：认领它的那几条 cue 里没有一条读到了这一行
    （`script_align.supports`，同 `hit_delta` 的口径）就是填位。两个内置匹配器共用。"""
    for r in recs:
        texts = ["\n".join(cues[i].lines) for i in r["cues"]]
        for c in ([r["ref"]] if r["ref"] else []) + list(r["extra"]):
            c["group"] = group_fn(c)
            c["fill"] = not any(SA.supports(t, c["jp"] or "") for t in texts)


def overlay_doc(items: list[dict], ov_stats: dict, evs_of: list[list[dict]], textbox: dict, name_ink: dict | None = None) -> dict:
    """产物的 `overlay`：条目、计数、喂给匹配器的事件（`fed`，显示协议的两个保留组由它推出）、文本框画像（`textbox`，`overlay_items` 给的）、
    判过名牌字色时各层的名牌色（`name_ink`，`nameink.check`）。两个内置匹配器共用。"""
    ov_stats["groups"] = dict(Counter(it["group"] for it in items))
    return {"stats": ov_stats, "items": items, "textbox": textbox,
            "fed": sorted({e["id"] for evs in evs_of for e in evs}), **({"name_ink": name_ink} if name_ink else {})}


def choice_like(kind: str, status: str | None) -> bool:
    """剧本行算不算"选项类"：库的结构判的选项（`Choice`，没有别的处境），或位置判在字幕带外的（`OffBand`，绝区零的选项按钮落在这里）。"""
    return status == "OffBand" or (status is None and kind == "Choice")


def choice_layer(choice: bool) -> str:
    """主轨外逐字等于剧本行的块归哪一层：选项类（`choice_like`）进 `choice`，其余进 `offmain`。
    2026-09-14 数过：原来统称 choice 的块里真选项只有 hsr 整场 87/340（`Talk` 217）、zzz 整场 178/498（`Loose` 241）。"""
    return "choice" if choice else "offmain"


def block_ok(ev: dict, choice: bool) -> bool:
    """主轨外逐字等于剧本行的事件能不能进叠加层：**动的块只收选项类**。

    静止的框盖不住在动的字——滚动的回看面板（hsr 6873 s 的ログ）上，事件带 `boxes[]` 轨迹，而叠加块只有一个框，
    于是译文错位压到别的行上（2026-09-14 摆帧）。hsr 整场 `offmain` 226 条里 131 条来源事件在动。
    滚动长文本归 `panel` 层（`readable_items` 逐帧读 obs）；选项按钮滑入时也会被记成 moving，所以选项类不拦。"""
    return choice or "boxes" not in ev


def block_long_enough(choice: bool, dur_us: int, frame_us: int) -> bool:
    """`offmain` 的块至少在屏上两个采样点：只读到一帧的是滚动途中 / 翻页瞬间的残影，盖上去就是闪一下
    （hsr 整场 `offmain` 去掉在动的之后 106 条里 32 条只有一帧，6873 s 回看面板上剩下的错位全是这种）。
    选项类不拦（按钮可能只停很短）。块的时长含末帧那一个采样间隔，所以一帧 = `frame_us`，留半帧余量。"""
    return choice or dur_us > frame_us + frame_us // 2


CHOICE_SPAN_PAD = 60.0
"""选项层的事件要落在它那一行所在单元的时段里，两头各放宽这么多秒。"""


def unit_spans(ref: GA.Ref) -> dict[str, list[float]]:
    """每个单元的时段：它各行锚点上屏时段的最早到最晚，两头放宽 `CHOICE_SPAN_PAD`；一个锚点都没有的单元不在里面。
    选项层（`choice_items`）和疑似漏字幕判断器（`dev_tools/miss_detect.py`）共用。"""
    out = {}
    for u in ref.doc["units"]:
        ts = [t for ln in u["lines"] for e in ((ln.get("anchor") or {}).get("episodes") or []) for t in e]
        if ts:
            out[u["uid"]] = [min(ts) - CHOICE_SPAN_PAD, max(ts) + CHOICE_SPAN_PAD]
    return out


def choice_items(doc: dict, main_regions: set[int], ref: GA.Ref, taken: set[int] = frozenset()) -> list[dict]:
    """**主轨之外**、逐字等于剧本行的事件（选项按钮这一类）：同一条剧本行、隔 ≤1 s 的并成一段。
    `taken`：已经被正文 / 第二条认领 / 译出了的名牌用掉的事件，不再拿来出块（同一个按钮否则画两遍：主认领认成选项行时它已经在 `body` 里）。

    2026-09-12 owner 看预览：hsr 0:21 的五个选项不在主轨里，没盖。剧本里选项是 `Choice` 行（有中文），
    所以只认**归一后逐字相等**（选项短，模糊匹配会乱认），并且事件时刻落在那一行所在单元的时段里（±60 s）。
    选项类的行另有一条**前缀**路（`ChoicePrefix`）：按钮后半截被挡住、淡入时只读到前半句。"""
    unit_span = unit_spans(ref)
    prefix = ChoicePrefix(ref, unit_span)
    by_text: dict[str, list[tuple]] = {}
    tpl = {ln["key"]: {k: ln[k] for k in TR_EXTRA if ln.get(k)} for u in ref.doc["units"] for ln in u["lines"]}
    for u in ref.doc["units"]:
        span = unit_span.get(u["uid"])
        for ln in u["lines"]:
            k = GA.GS.cnorm(ln["text"])
            if span and ln.get("cn") and len(k) >= 2:
                by_text.setdefault(k, []).append((ln["key"], ln["cn"], span, choice_like(ln["kind"], ln["status"]),
                                                  ln["text"], ln["kind"] == "Choice" and ln["status"] is None,
                                                  group_of(ln["kind"], ln["status"], ln.get("role")), u["uid"]))
    squash = lambda s: "".join(unicodedata.normalize("NFKC", s).split())   # noqa: E731
    hits: dict[str, list] = {}
    for e in doc["events"]:
        if tracksio.in_main(e, main_regions) or e["id"] in taken:
            continue
        t, q0 = e["t_start"] / 1e6, GA.GS.cnorm(e["text"])
        cands = []
        # 原样先试；对不上再把片假名后的 `一` 当长音 `ー` 试一次（剧本里 `カード一枚` 这种真汉字照原样认得上）
        for q in dict.fromkeys((q0, long_vowel(q0))):
            cands = [c for c in by_text.get(q, ()) if c[2][0] <= t <= c[2][1]]
            if not cands and (k := prefix.match(q, t)):
                cands = [c for c in by_text.get(k, ()) if c[3] and c[2][0] <= t <= c[2][1] and prefix.live(c[7], k, t, e["t_end"] / 1e6)]
                q = k
            if cands:
                break
        if not cands:
            continue
        # 归一化会去掉标点，按钮 `「未着」` 和台词 `「未着」……` 就成了同一个键；先认原文逐字相同的，再认 Choice 行——
        # 第一版取第一个，第五个选项被换成了台词的译文 `「未抵」……`，多出来的省略号把字挤小（hsr 0:24 摆帧）
        best = max(cands, key=lambda c: (squash(c[4]) == squash(e["text"]), c[5]))
        key, text = best[:2]
        if not block_ok(e, best[3]):
            continue
        hits.setdefault(key, []).append((e, text, q, best[4], best[3], best[6]))
    others = sorted((e for e in doc["events"] if not tracksio.in_main(e, main_regions) and e["id"] not in taken),
                    key=lambda e: e["t_start"])
    starts = [e["t_start"] for e in others]
    out = []
    for key, evs in hits.items():
        evs.sort(key=lambda x: x[0]["t_start"])
        groups: list[list] = []
        for e, text, jp, script_jp, choice, group in evs:
            # 还得是同一个位置：阅读面板里的小标题 `ブラーチ` 和选项按钮 `「ブラーチ」` 归一后逐字相等，
            # 只按时间并会把两处的框并成一个大框（第一版摆帧，一个「布拉琪」铺满半屏）。
            # 找的是**所有**开着的组；位置几乎不动（IoU ≥ 0.5）时隔 CHOICE_BRIDGE_US 以内也接上（惯性）
            cur = next((g for g in groups if box_iou(g[2], e["box"]) >= 0.3
                        and e["t_start"] - g[1] <= (CHOICE_BRIDGE_US if box_iou(g[2], e["box"]) >= 0.5
                                                    else 1_000_000)), None)
            if cur:
                cur[1] = max(cur[1], e["t_end"])
                cur[8].append(e["id"])
                cur[2] = [min(cur[2][0], e["box"][0]), min(cur[2][1], e["box"][1]),
                          max(cur[2][2], e["box"][2]), max(cur[2][3], e["box"][3])]
            else:
                cur = [e["t_start"], e["t_end"], list(e["box"]), text, jp, script_jp, key, choice, [e["id"]], group]
                groups.append(cur)
        # 惯性的另一半：组末尾之后 CHOICE_BRIDGE_US 以内、落在组框里、字是这条选项一部分的**残读**
        # （hsr 0:24 第二个选项被读成 `チ」` / `一チ`，逐字匹配认不出，按钮就断了）接进来
        for g in groups:
            i = bisect.bisect_left(starts, g[0])
            while i < len(others) and others[i]["t_start"] <= g[1] + CHOICE_BRIDGE_US:
                o = others[i]
                i += 1
                if o["t_end"] <= g[1] or box_inside(o["box"], g[2]) < 0.8:
                    continue
                q = GA.GS.cnorm(o["text"])
                # 残读也要过 `block_ok`（2026-09-15 审计）：它当不了种子，却能把块的结束时间往后推 2.5 s，
                # 于是**在动的**事件仍能让一个静止的 offmain 块多活一会儿——正是 matcher 计划那两道 offmain 过滤要挡的形状从另一个口子进来
                if q and GA.GS.contained(q, g[4]) >= 0.6 and block_ok(o, g[7]):
                    g[1] = max(g[1], o["t_end"])
                    g[8].append(o["id"])
        groups.sort(key=lambda g: g[0])
        for g in groups:
            prev = out[-1] if out and out[-1][4] == g[4] else None
            if prev and g[0] <= prev[1] and box_iou(prev[2], g[2]) >= 0.5:
                prev[1] = max(prev[1], g[1])
                prev[8] += g[8]          # 接上以后重叠的两段并掉，免得半透明底板叠两层
            else:
                out.append(g)
    # 这条路找到的台词不是认领，是别处重现的同一句（多是对话记录面板），归 `elsewhere`；选项、界面文字、过场字幕等照行的组。
    # 特殊上屏形式的提示 / 喊话（`special`）也照行的组、默认画：它们不进对话记录，在这条路上读到就是它本身上屏
    return [{"layer": choice_layer(x[7]), "group": "elsewhere" if x[9] == "dialogue" else x[9], "fill": False,
             "start_us": x[0], "end_us": x[1], "rows": [x[2]], "events": list(dict.fromkeys(x[8])),
             "key": x[6], "jp": x[5], "cn": x[3], **tpl.get(x[6], {})}
            for x in out if block_long_enough(x[7], x[1] - x[0], doc["frame_us"])]


CHOICE_BRIDGE_US = 2_500_000
"""选项按钮的惯性：同一位置隔这么久以内再读到（或读到残片）就算没断过。"""

KATA_ONE = re.compile(r"(?<=[\u30a1-\u30fa\u30fc])一")


def long_vowel(q: str) -> str:
    """片假名（或长音）后面的汉字 `一` 改成长音 `ー`：OCR 常把长音读成 `一`（hsr 选项 `また出た、ホ一ス先生！` 对 `ホース`，
    疑似漏字幕判断器 2026-10-07）。只给选项层逐字相等那条路兜底用，调用方先拿原样的去比。"""
    return KATA_ONE.sub("ー", q)


CHOICE_PREFIX_MIN = 6
"""前缀路（`ChoicePrefix`）的读数至少这么多个字（归一后）。"""
CHOICE_PREFIX_SHARE = 0.5
"""前缀路的读数至少占整行（归一后）这么多。"""
CHOICE_PREFIX_NEAR = 3.0
"""前缀路：同一单元有一行的锚点上屏时段离事件这么多秒以内，才算这一单元此刻在播。"""


class ChoicePrefix:
    """选项层的前缀路：读数是一条选项类剧本行的前缀，四道门都过才认——够长（`match`）、只对得上一种说法（`match`）、
    行是选项类（调用方查）、这一单元此刻在播（`live`）。选项层和疑似漏字幕判断器（`dev_tools/miss_detect.py` 说没出块的原因）共用。

    疑似漏字幕判断器的摆帧（2026-10-07）：gi1 的选项按钮被主播立绘挡住后半截，OCR 只读到五到八成，逐字相等认不出，22 条选项没画；
    按钮一行放不下折成两行时也是这个形状。只认前缀：挡住、折行都从后面缺。"""

    def __init__(self, ref: GA.Ref, unit_span: dict[str, list[float]]):
        self.spans_of: dict[str, list[list[float]]] = defaultdict(list)
        self.unit_eps: dict[str, list[tuple[str, list[float]]]] = defaultdict(list)
        for u in ref.doc["units"]:
            span = unit_span.get(u["uid"])
            for ln in u["lines"]:
                k = GA.GS.cnorm(ln["text"])
                if span and len(k) >= 2:
                    self.spans_of[k].append(span)
                self.unit_eps[u["uid"]] += [(k, e) for e in ((ln.get("anchor") or {}).get("episodes") or [])]
        self.keys = sorted(self.spans_of)

    def match(self, q: str, t: float) -> str | None:
        """读数 `q`（归一后）是哪一条剧本行的前缀：够长（`CHOICE_PREFIX_MIN` 字、占整行 `CHOICE_PREFIX_SHARE`），
        并且时刻 `t` 所在单元时段里以它开头的剧本行**只有一种说法**（台词、没译文的行也算在里面，撞上就不认）。返回那一行的归一文本。"""
        if len(q) < CHOICE_PREFIX_MIN:
            return None
        i = bisect.bisect_left(self.keys, q)
        found = None
        while i < len(self.keys) and self.keys[i].startswith(q):
            k = self.keys[i]
            i += 1
            if not any(a <= t <= b for a, b in self.spans_of[k]):
                continue
            if found is not None or k == q:     # 读数本身就是一整行（没译文才走到这里），或以它开头的不止一种说法
                return None
            found = k
        return found if found and len(q) >= CHOICE_PREFIX_SHARE * len(found) else None

    def live(self, uid: str, k: str, t0: float, t1: float) -> bool:
        """前缀是弱证据，还要这一单元此刻在播：单元里**别的**行（归一文本不是 `k`）有锚点上屏时段离 [t0, t1] ≤ `CHOICE_PREFIX_NEAR`。
        gi2 冒险手帐页上的 `デイリー依頼報酬` 是凯瑟琳那段对话里选项 `「デイリー依頼」報酬受取` 的前缀，那段对话 20–40 s 后才开始；
        这一行自己的锚点不算：gamescript 正是凭这个读数给它锚上了 19678–19693 s。"""
        return any(x != k and a - CHOICE_PREFIX_NEAR <= t1 and t0 <= b + CHOICE_PREFIX_NEAR for x, (a, b) in self.unit_eps.get(uid, ()))


def box_inside(a: list[float], b: list[float]) -> float:
    """a 有多少面积落在 b 里。"""
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    area = (a[2] - a[0]) * (a[3] - a[1])
    return ix * iy / area if area > 0 else 0.0


READABLE_MIN_CHARS = 12
"""阅读面板这类长文本：屏幕上一行至少这么多内容字才去 TextMap 里找（短的撞车太多）。"""
READABLE_IN = 0.9
"""一行 OCR 的字有这么多落在某一段里，才算这一段。"""
READABLE_ONE_LINE = 0.5
"""一帧里某一段只认出**一行**时，这一行至少占那一段内容字的这么多，才算（`readable_items`）。

阅读面板一屏是好几行：同一段认出 ≥2 行就是面板；只认出一行的，要么是一句话的短段（道具说明），这一行就是整段，
要么是屏上一行短 UI 恰好是某段长文的子串——称号「「スチームバード新聞」編集長」撞上一本书（gi2 12 块）、
任务目标「ロマリタイムハーバーに行く」撞上一篇角色故事（gi1 14 块），这一行只占那一段的一小部分（2026-10-06）。"""


def paragraphs(t: str) -> list[str]:
    """TextMap 的一整条长文本拆成段：去掉 `<b>` / `<i>` 这类标签，按换行（字面的 `\\n` 或真换行）切，丢空段。"""
    t = re.sub(r"<[^>]*>", "", t).replace("\\n", "\n").replace("\xa0", " ")
    return [p.strip() for p in t.split("\n") if p.strip()]


def box_iou(a: list[float], b: list[float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


READABLES_TABLE, READABLES_CONTRACT = "readables", 1
"""文本包里的阅读面板长文本表（game-text-data 的 `<游戏>.readables/1`：一行 `id` / `source` / `text`，各语种逐行 id 相同）。"""


def readable_paras(game: str) -> dict[str, tuple[str, str, str | None]] | None:
    """文本包的 `readables` 表 -> 段索引（归一键 -> (日文段, 中文段, 英文段或 None)），`readable_items` 用。包里没有这张表返回 None。

    一条长文本按 `paragraphs` 切段，日中段数不等的整条跳过（英文段数和日文不等的，这条的英文段记 None）；段的内容字少于 `READABLE_MIN_CHARS` 的不进（短段撞车太多）。
    表是阅读界面（书籍、信件、说明…）引用的长文本，已经在对话类表里的 game-text-data 那边去掉了，不和台词叠加抢字。
    建一次要读完整张表，结果按 包的内容指纹 + 本模块的代码指纹 缓存到 `out/_cache/readables/`（同 `gamescript.library`）。"""
    import hashlib
    import pickle
    from flowocr.analyze import build_tracks as bt
    GS = GA.GS
    bd = GS.bundle_of(game)
    if not bd.has([READABLES_TABLE]):
        return None
    bd.require({READABLES_TABLE: READABLES_CONTRACT})
    key = hashlib.sha256(json.dumps([game, bd.fingerprint, bt.code_fp(CODE_FP_FILES), READABLE_MIN_CHARS])
                         .encode()).hexdigest()[:16]
    cp = paths.data_root() / "out" / "_cache" / "readables" / f"{game}-{key}.pkl"
    if cp.exists():
        bd.verify_stored([READABLES_TABLE], ("ja", "zh-Hans", *((GS.EN,) if GS.has_en(game, READABLES_TABLE) else ())))
        with open(cp, "rb") as f:
            return pickle.load(f)
    paras: dict[str, tuple[str, str, str | None]] = {}
    for a, b, e in GS.paired(game, READABLES_TABLE):
        pj, pc = paragraphs(a["text"] or ""), paragraphs(b["text"] or "")
        if len(pj) != len(pc):
            continue
        pe = paragraphs(e["text"] or "") if e else []
        if len(pe) != len(pj):
            pe = [None] * len(pj)
        for x, y, z in zip(pj, pc, pe):
            k = GS.cnorm(x)
            if len(k) >= READABLE_MIN_CHARS:
                paras.setdefault(k, (x, y, z))
    # 不清理别的键：不同代码 / 不同包的进程共用这个目录，删"旧键"会删掉别的进程正在读的那份（二次验收 2026-10-06）。
    # 旧键只占盘，整目录删掉就等于没有缓存
    cp.parent.mkdir(parents=True, exist_ok=True)
    tmp = cp.with_suffix(f".{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        pickle.dump(paras, f, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(cp)
    return paras


def readables_of(ref: GA.Ref) -> tuple[dict[str, tuple[str, str, str | None]], dict]:
    """剧本是哪款游戏的，就读那款文本包的 `readables` 表；包里没有这张表就当场报错（选开了却什么都盖不上，别静默）。
    返回 (段索引, 来源)：来源是这次读的那份包（`Bundle.provenance`）——面板读的是**现在找得到的**包，
    不一定是产剧本那份（`provenance.gametext` 抄的是剧本的），所以单独记进 `overlay.stats.panel_gametext`。"""
    game = (ref.doc.get("provenance") or {}).get("game")
    paras = readable_paras(game) if game else None
    if paras is None:
        raise SystemExit(f"--readables：{game or '这份剧本'}的文本包里没有 `{READABLES_TABLE}` 表（game-text-data 新版才有）")
    return paras, GA.GS.bundle_of(game).provenance()


def readable_items(obs_path: Path, paras: dict[str, tuple[str, str, str | None]], frame_us: int,
                   taken: list[dict]) -> tuple[list[dict], int]:
    """阅读面板长文本（选开，2026-09-12 owner 要"试试看能不能盖"）：**逐帧**拿 obs 里的长文本行去找它属于哪一**段**
    （`paras`：归一键 -> (日文段, 中文段, 英文段或 None)，`readable_paras` 从文本包的 `readables` 表建），同一帧里认出来的段并成一块，
    盖上这几段的译文；相邻帧段集合相同、位置几乎不动就接着延长。返回 (items, 用掉的观测条数)。

    **不用 run / 事件**：阅读面板在滚，run 按静止框跟踪，同一个框位置上的文字一路在换，
    投票留下来的文本和并出来的框来自不同的滚动位置（第一版这么做，摆帧整块错位）。逐帧的观测没有这个问题。
    `taken` = 已经被台词叠加认领的正文事件：同一时刻、框重叠的观测跳过。

    **只读一遍 obs**（2026-10-06，原来两遍：先找命中的帧、再收那些帧的全部观测）：obs 按时间排，一帧的观测攒齐了再看——
    帧里有一行找到段，这一帧的观测全留下（块要长满整个面板那一栏）；没有就丢。"""
    from flowocr.analyze import gamescript as GS
    keys = list(paras)
    index = GS.Index(keys)
    by_sec: dict[int, list[dict]] = {}
    for e in taken:
        for s in range(e["t_start"] // 1_000_000, e["t_end"] // 1_000_000 + 1):
            by_sec.setdefault(s, []).append(e)
    found: dict[str, int | None] = {}
    frames: dict[int, dict[int, list[dict]]] = {}
    allobs: dict[int, list[dict]] = {}
    n_used = 0

    def in_taken(o: dict, t: int) -> bool:
        # 被台词认领的正文**框里**的观测跳过：打字机打到一半的那一帧框比整句窄，按 IoU 判就漏过去，
        # 半句台词被当成长文本又叠一层（hsr 0:47，摆帧看见的）
        return any(e["t_start"] <= t <= e["t_end"] and box_inside(o["box"], e["box"]) >= 0.8
                   for e in by_sec.get(t // 1_000_000, ()))

    def flush(t: int, buf: list[dict]) -> None:
        nonlocal n_used
        hit: dict[int, list[dict]] = {}
        for o in buf:
            q = GS.cnorm(o["text"])
            if len(q) < READABLE_MIN_CHARS:
                continue
            if q not in found:
                best, best_s = None, READABLE_IN
                for i in index.query(q, top=3):
                    sc = GS.contained(q, keys[i])
                    if sc >= best_s:
                        best, best_s = i, sc
                found[q] = best
            if found[q] is None or in_taken(o, t):
                continue
            hit.setdefault(found[q], []).append(o)
        hit = {p: os_ for p, os_ in hit.items()
               if len(os_) >= 2 or max(len(GS.cnorm(o["text"])) for o in os_) >= READABLE_ONE_LINE * len(keys[p])}
        if hit:
            frames[t] = hit
            allobs[t] = buf
            n_used += sum(len(v) for v in hit.values())

    cur_t, buf = None, []
    with open(obs_path, encoding="utf-8") as f:
        next(f)                                  # _meta
        for line in f:
            o = json.loads(line)
            t = o["t_us"]
            if t != cur_t:
                if cur_t is not None and t < cur_t:
                    raise SystemExit(f"{obs_path} 的观测没按时间排（{t} 在 {cur_t} 之后）：阅读面板层按帧只读一遍")
                if buf:
                    flush(cur_t, buf)
                cur_t, buf = t, []
            buf.append(o)
    if buf:
        flush(cur_t, buf)

    def grow_column(box: list[float], t: int, row_h: float) -> list[float]:
        """同一帧里 x 上落在块这一栏、上下挨着块（≤ 2.5 行高）的观测，一路并进块；台词正文框里的不并。"""
        cand = [o for o in allobs.get(t, ())
                if min(o["box"][2], box[2]) - max(o["box"][0], box[0]) >= 0.5 * max(1, o["box"][2] - o["box"][0])
                and not any(e["t_start"] <= t <= e["t_end"] and box_inside(o["box"], e["box"]) >= 0.8
                            for e in by_sec.get(t // 1_000_000, ()))]
        grew = True
        while grew:
            grew = False
            for o in cand:
                b = o["box"]
                if box[1] <= b[1] and b[3] <= box[3]:
                    continue
                if b[3] >= box[1] - 2.5 * row_h and b[1] <= box[3] + 2.5 * row_h:
                    box = [min(box[0], b[0]), min(box[1], b[1]), max(box[2], b[2]), max(box[3], b[3])]
                    grew = True
        return box

    # **一整块**（owner 2026-09-12：逐段各盖各的观感不好）：同一帧里所有认出来的段并成一块，
    # 框 = 这些行的并集再长满这一栏，译文按段顺序（屏幕上从上到下）排、在块里重新折行（`export_overlay` 的 block 分支）。
    # 相邻帧段集合相同、块几乎不动就延长
    items: list[list] = []
    cur = None
    for t in sorted(frames):
        paras_here = sorted(frames[t], key=lambda p: min(o["box"][1] for o in frames[t][p]))
        obs = [o for p in paras_here for o in frames[t][p]]
        box = [min(o["box"][0] for o in obs), min(o["box"][1] for o in obs),
               max(o["box"][2] for o in obs), max(o["box"][3] for o in obs)]
        row_h = sorted(o["box"][3] - o["box"][1] for o in obs)[len(obs) // 2]
        box = grow_column(box, t, row_h)
        sig = tuple(paras_here)
        # 采样间隔有 ±1 µs 的抖动，接续判据留半帧余量（docs/dev-guide/pitfalls.md「时间戳比较留半个间隔的余量」）
        if cur and cur[5] == sig and cur[1] + frame_us // 2 >= t and box_iou(cur[2][0], box) >= 0.8:
            cur[1] = t + frame_us
        else:
            en = [paras[keys[p]][2] for p in paras_here]
            cur = [t, t + frame_us, [box], {"jp": [paras[keys[p]][0] for p in paras_here],
                                            "cn": [paras[keys[p]][1] for p in paras_here],
                                            **({"en": en} if None not in en else {})}, row_h, sig]    # 有一段没有英文就整块不出英文
            items.append(cur)
    return [{"layer": "panel", "group": "panel", "fill": False, "start_us": s, "end_us": e, "rows": rows, "paras": ps, "row_h": row_h}
            for s, e, rows, ps, row_h, _ in items], n_used


def speaker_names(ref: GA.Ref, lang: str = "cn") -> dict[str, str]:
    """说话人：日文（`SA.norm` 归一）-> 中文（`lang="en"`：英文，剧本行的 `speaker_en` 和顶层 `speakers_en`）。名牌层的译文从这里取。

    先用剧本里 gamescript 写的**整库** `speakers` 表（这段视频圈中的剧本行常没有说话人，原神任务对话尤甚），
    表里没有的再从剧本行补（同一个名字取出现最多的写法）。库里没有说话人的（绝区零的 scene）就没有名字译文，
    名牌层照样判出框、只是 `cn` 为空、不叠。

    **旧剧本要拦**（2026-09-15 审计）：`speakers` / `speaker_cn` 是 09-14 才加进 `gamescript` 的。
    剧本早于那一版时这里会安安静静返回空表，名牌层于是**整层没有译文、`--lang cn` 把它全丢掉**，
    一行报错都没有——照文档的命令跑 gi-s2 得到 `名牌有中文名的 0/39`，而 matcher 计划 记的是 28。
    和"旧默认产的 obs 被静默复用"是同一个形状，所以这里明着喊，并把这件事写进产物
    （`overlay.stats.ref_has_speakers`），不让它只在终端上闪过。"""
    if "speakers" not in ref.doc and lang == "cn":
        print("⚠ 剧本里没有 `speakers` 表（早于 2026-09-14 的 gamescript）：名牌层将没有任何译文。"
              "重跑 `flowocr.analyze.gamescript` 生成剧本再来。")
    cnt: dict[str, Counter] = defaultdict(Counter)
    for u in ref.doc["units"]:
        for ln in u["lines"]:
            if ln.get("speaker") and ln.get(f"speaker_{lang}"):
                cnt[SA.norm(ln["speaker"])][ln[f"speaker_{lang}"]] += 1
    names = {k: c.most_common(1)[0][0] for k, c in cnt.items() if k}
    table = {SA.norm(k): v for k, v in (ref.doc.get("speakers" if lang == "cn" else f"speakers_{lang}") or {}).items()
             if SA.norm(k) and v}
    return {**names, **table}


def term_names(ref: GA.Ref, lang: str = "cn") -> dict[str, str]:
    """短名词表：日文（`SA.norm` 归一）-> 中文（`lang="en"`：英文）。说话人表（`speaker_names`）查不到的名牌再查它。

    表是 gamescript 从文本包的 `terms` 里挑的——只收这段 obs 里逐字出现过的键、消歧也在那边做完
    （`gamescript.term_table`）；旧包 / 旧剧本没有这一项，返回空表，名牌层照旧只查说话人。
    它按**字形**查到的是游戏自己的译文，但不保证那块字是人名（任务目标、称号也会查到），
    所以名牌条目上记 `name_src`，下游分得清"剧本说话人"和"按字形查到的"。"""
    return {SA.norm(k): v[lang] for k, v in (ref.doc.get("terms") or {}).items() if SA.norm(k) and v.get(lang)}


TERM_ON_DIALOGUE = 0.3
"""名牌层查短名词表的门（`on_dialogue`）：这一行作为开头行出现的 cue 里，至少这么大比例的主认领是带内台词，才拿字形去查。"""


def on_dialogue(heads: frozenset[str], cues: list, recs: list[dict]) -> dict[str, float]:
    """每个开头行"挂在台词上"的比例：它作为非末行出现的 cue 里，主认领是**带内台词**（`gamescript.in_band`）的占几成。

    为什么（2026-10-05，台账"名牌短名词表的余项"）：名牌判据（`head_lines` + `tracksio.name_over_body`）认的是**形状**——
    常驻在一块变化的字上面的短行；任务目标、奖励标题、装备名、技能名也是这个形状，短名词表又按字形查得到它们的译文，
    09-26 五段切片新译出的 54 块里 30 块是这类 UI。真名牌下面是台词，UI 下面是任务列表、属性、技能说明——
    剧本认领得出这个区别，不用名字表、不用按游戏写词表。四场整场 + 五段切片（agent 按字面判）：门 0.3 时
    切片留下的 24 块全是真名牌、去掉的 30 块全是 UI；整场查短名词表的 1631 → 512 块，去掉的 1119 块里认得出的真名字 4 种 15 块
    （gi2 `プレシア` / `シュテル` / `ユーフラシア`、zzz `怪しい参加者`：它们挂的那几条 cue 没被认领），留下的里认得出的 UI 2 种 12 块（hsr `後味`、`二相楽園`）。"""
    rec_of = {i: r for r in recs for i in r["cues"]}
    tot, dia = Counter(), Counter()
    for i, c in enumerate(cues):
        ls = [x for x in c.lines if x.strip()]
        for x in ls[:-1]:
            k = SA.norm(x)
            if k in heads:
                tot[k] += 1
                r = rec_of.get(i)
                # 顺序剧本（naninovel）的认领没有处境、类型也不在 NON_BAND_KINDS 里，按带内算：那里没有 UI 文案、选项这类带外行
                dia[k] += bool(r and r["ref"] and GA.GS.in_band(r["ref"].get("kind"), r["ref"].get("status")))
    return {k: dia[k] / tot[k] for k in tot}


def one_char_apart(a: str, b: str) -> bool:
    """两串只差一个字：换一个、漏一个或多一个。"""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    if len(a) > len(b):
        a, b = b, a
    return any(b[:i] + b[i + 1:] == a for i in range(len(b)))


FUZZY_NAME_MIN = 2
"""名牌容错查（`fuzzy_names`）两边都至少这么多内容字：单字名一字之差就是全换（hsr `冷` / `刃`）。"""


NAME_VOTES = 3
"""名牌投票（`fuzzy_names`）：一个读数下面至少这么多条记录的说话人是同一个人，才算"证据充分"。"""
NAME_VOTE_SHARE = 2 / 3
"""名牌投票：那个人还要占这个读数下面全部有说话人的记录的这么多。"""


def record_speakers(recs: list[dict], ref: GA.Ref | None = None) -> list[tuple[str, str] | None]:
    """每条记录主认领那一行的说话人（日文名, 中文名），名牌投票（`fuzzy_names`）的证据；没认上、或不知道是谁说的为 None。
    认领自带 `speaker` / `speaker_cn` 的（naninovel）直接用；gametext 的认领不带，按 key 查剧本行（库里有说话人的行不多，票常常不够）。"""
    by_key: dict[str, tuple[str, str]] = {}
    if ref is not None:
        for u in ref.doc["units"]:
            for ln in u["lines"]:
                if ln.get("speaker") and ln.get("speaker_cn"):
                    by_key.setdefault(ln["key"], (ln["speaker"], ln["speaker_cn"]))
    out: list[tuple[str, str] | None] = []
    for r in recs:
        f = r["ref"]
        sp = None if not f else (f.get("speaker"), f.get("speaker_cn")) if "speaker" in f else by_key.get(f["key"])
        out.append(sp if sp and sp[0] and sp[1] else None)
    return out


def fuzzy_names(name_items: list[dict], share: dict[str, float],
                votes: dict[str, dict[int, tuple[str, str]]] | None = None) -> dict[str, int]:
    """名牌容错（owner 2026-10-06 定做借已译出的；2026-10-24 owner 定加投票）：逐字查不到的名牌读数，按证据强弱两档认，
    证据越弱、字面要求越严。就地改 `name_items`，返回两档各改了几个事件 `{"voted": n, "fuzzy": n}`。两档都要读数 ≥ `FUZZY_NAME_MIN` 个内容字。

    1. **投票**（`name_src = "voted"`）：`votes[读数]` 是这个读数作为名牌挂着的每条记录的说话人（`record_speakers`，同一条记录只算一票）。
       票最多的那个人有 ≥ `NAME_VOTES` 票、占有说话人的票 ≥ `NAME_VOTE_SHARE`，就算证据充分；读数还要能是这个人名字的一截
       （`textfold.fits_within`：≤2 字的读数只许少读、不许读错，3 字起许错 1 处），才译成他。证据充分但字面对不上的（`ええ。` 下面总是同一个人说话）
       不按投票译；第 2 档也只在借来的正是这个人时才借。
       为什么要说话人：只看字形，2 字的读数一字之差就是一半换掉；名牌下面那句台词是谁说的，剧本已经认出来了。
       《魔裁》名牌常少读头一个字（`二階堂ヒロ` 读成 `階堂ヒロ`）、或读错一个字形（`階堂ヒ口`），逐字查和第 2 档都接不住。
    2. **借本段已译出的**（`name_src = "fuzzy"`）：没有充分的投票时，和**这段视频里已经译出的名牌**只差一个字（`one_char_apart`）、
       这样的名牌只有一个、读数也挂在台词上（`on_dialogue`，同短名词表），借它的译文；有充分投票而指向别人时不借。
       只吸附到已译出的：对整张短名词表做容错查，一字之差就撞到另一个条目的原神 42%、星铁 28%、绝区零 35%；
       同一段视频里已经出现过、译出来的名字少得多（hsr `文光` / `交光` → 爻光、zzz `Uン` → 铃，摆帧核过）。
    误译的样子是把一个角色的名牌画成另一个角色的名字——比不译显眼，所以条件都取严的。"""
    votes = votes or {}
    done: dict[str, dict] = {}
    for it in name_items:
        if it["cn"] and it["name_src"] in ("speakers", "terms"):
            done.setdefault(SA.norm(it["ocr"]), it)
    done = {k: it for k, it in done.items() if evalkit.content_len(it["ocr"]) >= FUZZY_NAME_MIN}
    fixed: dict[str, set[int]] = {"voted": set(), "fuzzy": set()}
    verdict: dict[str, tuple[str, str] | None] = {}       # 读数 -> 证据充分时票最多的那个人（日文名, 中文名）
    for k, by_rec in votes.items():
        c = Counter(by_rec.values())
        if c:
            who, v = c.most_common(1)[0]
            if v >= NAME_VOTES and v >= NAME_VOTE_SHARE * sum(c.values()):
                verdict[k] = who
    for it in name_items:
        if it["cn"]:
            continue
        k = SA.norm(it["ocr"])
        n = evalkit.content_len(it["ocr"])
        if n < FUZZY_NAME_MIN:
            continue
        who = verdict.get(k)
        if who is not None and textfold.fits_within(k, who[0], 0 if n <= 2 else 1):
            it.update(jp=who[0], cn=who[1], name_src="voted")
            fixed["voted"].add(it["events"][0])
            continue
        if share.get(k, 0.0) < TERM_ON_DIALOGUE:
            continue
        nb = [d for key, d in done.items() if one_char_apart(k, key)]
        if len(nb) == 1 and (who is None or who[1] == nb[0]["cn"]):
            it.update(jp=nb[0]["jp"], cn=nb[0]["cn"], name_src="fuzzy")
            fixed["fuzzy"].add(it["events"][0])
    return {k: len(v) for k, v in fixed.items()}


def name_roles(name_items: list[dict], line_items: list[dict]) -> dict[str, int]:
    """名牌块逐块定组和层（owner 2026-11-05：有匹配器时，只有挂在认上的台词上的才是真名牌，名字和称号分开）。就地改，返回各类块数。

    * **挂上**：这一块在屏的时候，正下方（`tracksio.name_over_body`，比台词块的首行、左右取整块 `tracksio.block_head`，间隔上限 `tracksio.NAME_GAP`）
      有一块认上的台词——正文 / extra 层、组是 `dialogue`、不是对话记录面板里的旧行（`echo` 为 `log`）。
      同一块名牌前后几次出现连成一串（`tracksio.plate_runs`：同名、同一处、隔不超过 `NAME_INERTIA_US`），一串里有一块挂上整串都算——
      换句台词、漏读一两帧时名牌会断开一下，断开那一小段下面可能正好没有台词块。挂上的留在 `name` 组，没挂上的归 `matchedio.STRAY_NAME`（默认不画）。
      为什么要逐块判：认开头行（`head_lines` + 同一条 cue 里有正文挂在下面）、查短名词表的门（`on_dialogue`）看的都是**这串字整场的形状**，
      同一个名字出现在队伍栏、好友列表、头顶、对话记录面板里也一样认、一样译。四场整场（2026-11-05 重跑匹配量，有译文的名牌）
      gi1 802 块里没挂上的 61、gi2 1048 / 36、hsr 480 / 135、zzz 234 / 26；抽帧看，绝大多数是上面那几种界面；
      落在常规名牌位置、真名牌没挂上的是下面那句台词没认上（gi1 3 块、hsr 2 块、zzz-s2 2 块）——这时台词本身也不画译文，名牌跟着不画。
      对话记录面板（hsr）里每条旧行上面也有一行字，读出来是 `二相楽園`、`後味` 这类地名、名词，所以 `log` 的台词不算。
    * **称号**：挂上的块里，叠在一块**有译文**的名牌下面（或上面）、字小一号的（`tracksio.stack_partners`）层改成 `title`，
      字幕稿里另用 `Title` 样式（原神的「傭兵」「燭明の守り人」）。离台词远的那块可能超出间隔上限
      （称号在下时是名字，在上时是称号），所以一对里有一块挂上，两块都算挂上。
      名字要有译文（认得出是谁）：绝区零没有称号，台词上面的角色名「ヴェリナ」、「大会ガイド」叠在一块大字招牌「QICAI JIE」下面，
      只看几何会把角色名当称号、把招牌当名字（2026-11-05 整场 14 块）。
    只改组和层，不改译文——选项层的去重、名牌投票在这之前就做完了，台词和选项的条目不受影响。"""
    lines = sorted((it for it in line_items if it["group"] == "dialogue" and it.get("echo") != "log"),
                   key=lambda it: it["start_us"])
    starts = [it["start_us"] for it in lines]
    span = max((it["end_us"] - it["start_us"] for it in lines), default=0)
    direct = []
    for n in name_items:
        lo, hi = bisect.bisect_left(starts, n["start_us"] - span), bisect.bisect_left(starts, n["end_us"])
        direct.append(any(lines[k]["end_us"] > n["start_us"] and tracksio.name_over_body(n["rows"][0], tracksio.block_head(lines[k]["rows"]))
                          for k in range(lo, hi)))
    runs = tracksio.plate_runs([(n["start_us"], n["end_us"], n["rows"][0], n["cn"] or SA.norm(n["ocr"])) for n in name_items])
    direct = tracksio.spread(direct, runs)
    partner = [j if j is not None and name_items[j]["cn"] else None
               for j in tracksio.stack_partners([(n["start_us"], n["end_us"], n["rows"][0]) for n in name_items])]
    hung = list(direct)
    for i, j in enumerate(partner):
        if j is not None and (direct[i] or direct[j]):
            hung[i] = hung[j] = True
    hung = tracksio.spread(hung, runs)
    cnt = Counter()
    for i, it in enumerate(name_items):
        it["group"] = "name" if hung[i] else matchedio.STRAY_NAME
        if hung[i] and partner[i] is not None and hung[partner[i]]:
            it["layer"] = "title"
        cnt[it["layer"] if hung[i] else matchedio.STRAY_NAME] += 1
    return dict(cnt)


SAME_PLACE = 0.5
"""一段叠加块里每条 cue 的正文都要和这一段**实际画的那个框**在同一处：较小那个外接框至少这么多面积落在另一个里（`position_segments`）。
打字机长字是小框落进大框（1.0），画面里跟着角色走的对白一挪就是 0。"""


def _ubox(evs: list[dict]) -> list[float]:
    return [min(e["box"][0] for e in evs), min(e["box"][1] for e in evs),
            max(e["box"][2] for e in evs), max(e["box"][3] for e in evs)]


def _same_place(a: list[float], b: list[float]) -> bool:
    return max(box_inside(a, b), box_inside(b, a)) >= SAME_PLACE


class Segment(NamedTuple):
    """`position_segments` 切出的一段叠加块。"""
    evs: list[dict]            # 段里全部 cue 的正文事件
    rows: list[list[float]]    # 画的那条 cue 的行框（条目的 `rows`）
    start: int
    end: int
    cue: int                   # 画的那条 cue 的下标
    shown: list[dict]          # 画的那条 cue 的正文事件（`rows` 就是它们的行框；原文整块按它们的字对剧本行，`textbox`）


def position_segments(parts: list[tuple[int, list[dict]]], cues: list, s_us: int, e_us: int) -> list[Segment]:
    """一条认领（`merge_runs` 可能并了好几条 cue）的正文按**位置**切成几段叠加块（`Segment`）。

    认领是"这几条 cue 是同一句剧本行"，渲染块是"译文画在哪、画多久"——两件事。区域可以包含跟着画面走的字，
    按区域并起来的几条 cue 不一定在同一处（2026-09-26 审计：hsr-s1 一句对白 2 s 里换了四个位置，
    并成一块后整段画在第一处）。一段画的是行数最多的那条 cue 的行框（同样多取先到的）；一条 cue 进这一段的条件是
    加进来之后**段里每条 cue** 都和那时要画的框在同一处（`SAME_PLACE`）——只和上一条比没有传递性，
    慢慢漂的字每一步都像"同一处"，几步之后就完全离开了画的那个框（Codex 复审的反例）；画的框因行数变多而换掉时，
    已在段里的也要重核。段内空档照旧补上；另起一段时两段之间的空档不画——那段时间字在动或不在，不能凭空画在旧位置。
    只有一段时起止就是记录的起止，和切段之前逐项相同。`parts` 是 (cue 下标, 这条 cue 的正文事件)，按时间排。"""
    groups: list[list[tuple[int, list[dict], list[float], int]]] = []

    def render_of(g):                                  # 行数最多的那条（同样多取先到的），和下面取 rows 的规则一致
        best = g[0]
        for m in g[1:]:
            if m[3] > best[3]:
                best = m
        return best

    for i, evs in parts:
        if not evs:
            continue
        cand = (i, evs, _ubox(evs), len(rows_of(evs)))
        if groups:
            trial = groups[-1] + [cand]
            rb = render_of(trial)[2]
            if all(_same_place(m[2], rb) for m in trial):
                groups[-1].append(cand)
                continue
        groups.append([cand])
    out = []
    for k, g in enumerate(groups):
        evs = [e for m in g for e in m[1]]
        shown = render_of(g)
        a = s_us if k == 0 else int(round(round(cues[g[0][0]].start, 3) * 1e6))
        b = e_us if k == len(groups) - 1 else int(round(round(cues[g[-1][0]].end, 3) * 1e6))
        out.append(Segment(evs, rows_of(shown[1]), a, b, shown[0], shown[1]))
    return out


def event_segments(e: dict) -> list[tuple[list[float], int, int]]:
    """一个事件按**位置**切成几段叠加块：(画的框, 起, 止)。名牌层一个事件一块（时间取事件自己的），事件在动时用的就是这个——
    和 `position_segments` 同一条规矩（`SAME_PLACE`，只和这一段画的框比、不和上一个采样点比，没有传递性）：
    沿着轨迹（`boxes`，只有在动的事件才有）每到一个和这一段画的框不在同一处的位置就另起一段，一段画它第一个采样点的框，
    到下一段的第一个采样点为止。只有一段（不动、或动了没离开过同一处）时就是事件的框和起止，和切段之前相同。
    不切的话在动的事件整段画在一个框上（事件的框取最终位置）：星铁对话记录里随列表滚动的 `ドロシー` 9 s 里挪了 322 px。"""
    path = e.get("boxes") or []
    if len(path) < 2:
        return [(list(e["box"]), e["t_start"], e["t_end"])]
    groups: list[list] = [[path[0]]]
    for t, b in path[1:]:
        if _same_place(b, groups[-1][0][1]):
            groups[-1].append((t, b))
        else:
            groups.append([(t, b)])
    if len(groups) == 1:
        return [(list(e["box"]), e["t_start"], e["t_end"])]
    return [(list(g[0][1]), e["t_start"] if k == 0 else g[0][0],
             e["t_end"] if k == len(groups) - 1 else groups[k + 1][0][0])
            for k, g in enumerate(groups)]


BODY_SHIFT_TOL = tracksio.SAMPLE_SLACK
"""正文块的起点只有要往后挪超过这么多个采样间隔才挪（`body_start`）：采样级精度以内的差不改，
名牌比正文早出现一个采样点（yuka 每集上千条，早 0.5 s）这种不动。"""
BODY_LOOKBACK_US = 120_000_000
"""找"正文出现之前就在屏的同一句读数"时往前看多远（yuka-f3「裁判の後。」的变体比记录起点早 47 s 就在屏上）；
`body_start` 用的事件索引（`tracksio.EventIndex`）按它建。"""


BODY_END_SLACK_US = 1000
"""`body_end`：回抠过的正文，终点比这句字最晚的消失晚不到这么多（记录起止的取整精度）不夹；采样级的不到 `tracksio.SAMPLE_SLACK` 个采样间隔不夹。"""
BODY_END_IN = 0.9
"""`body_end`：正文位置上的读数整个包住正文（正文的字有这么多按顺序落在读数里）也算这句字还在。"""


def body_end(b: int, a: int, evs: list[dict], mates: list[dict] = (), ref_text: str = "", slack: int = BODY_END_SLACK_US) -> int:
    """正文块的终点：`b`（记录或这一段 cue 的终点）夹到这句字最晚的消失，不早于起点 `a`。
    cue 的终点由它全部的事件定，同一条 cue 里比正文晚消失的名牌、按键提示会把终点拖晚 0.02–0.5 s，
    前一条译文压到下一句（hsr 2325.6 s：正文 2325.517 s 消失，名牌「サンデー」到 2326.017 s；检测器三场修复计划 U2）。`extra` 层一直是这么夹的。

    "这句字"除了正文事件，还算记录里**在正文位置上**（框和某个正文事件相交）、和正文或剧本原文是同一句（`textfold.same_reading`）
    或者整个包住正文的字（`BODY_END_IN`）的读数（`mates`，记录里全部 cue 的事件）：同一句的变体常常不在正文里——yuka-f3「裁判の後。」带句号的读数
    被当成开头行剥掉，只看正文会把终点夹早 78 s；zzz 3:07:41 后段读成「LT + ◆ で任務目標に向かう」，带着按键前缀，不算同一句的话夹早 9.5 s。
    成员里有开放边界（`t_end_how = open`，没看到它消失）的不夹：不知道它什么时候消失。
    差不到 `slack` 的不夹：回抠过的给 `BODY_END_SLACK_US`（记录的起止存成毫秒，和事件的微秒差出来的零点几毫秒只是取整），
    采样级的给 `tracksio.SAMPLE_SLACK` 个采样间隔（字最后一次被读到之后、下一个采样点之前什么时候消失不知道，采样点的间隔还有抖动；
    yuka-f3「待って……！」夹掉的 0.5 s 那时字还在）。

    这里认作"这句字还在"的读数**不**记进正文条目的 `events`（`body_start` 借的记）：它们都在正文事件之后，打字机按"全字出现"的最晚时刻判，
    记进去只会把打字机拖晚；"包住正文"的读数还可能是另一句（2026-10-24 摆帧：zzz 6160.5 s 技能面板上的另一行、《魔裁》f4 26606.8 s 被人物挡住的缺字读数，
    记进去都判出了假打字机，defaults.md 1.66）。"""
    if any(e.get("t_end_how") == "open" for e in evs):
        return max(a, b)
    ids, boxes = {e["id"] for e in evs}, [e["box"] for e in evs]
    texts = [e["text"] for e in evs] + ([ref_text] if ref_text else [])
    cores = [q for e in evs if len(q := GA.GS.cnorm(e["text"])) >= 3]
    last = max(e["t_end"] for e in evs)
    for m in mates:
        if (m["id"] not in ids and m["t_start"] < b and m["t_end"] > last and any(tracksio.boxes_cross(m["box"], x) for x in boxes)
                and (any(textfold.same_reading(m["text"], x) for x in texts)
                     or any(GA.GS.contained(q, GA.GS.cnorm(m["text"])) >= BODY_END_IN for q in cores))):
            last = m["t_end"]
    if b - last < slack:
        return max(a, b)
    return max(a, last)


def body_start(a: int, evs: list[dict], ref_text: str, index: tracksio.EventIndex, tol: int) -> tuple[int, list[dict]]:
    """正文块的起点：`a`（记录或这一段 cue 的起点）往后挪到"这句字最早出现的时刻"，只往后挪、不往前推，挪不到 `tol`（`BODY_SHIFT_TOL` 个采样间隔）就不挪。
    返回 (起点, 借来的读数)。
    回抠过的正文也用这个容差：正文事件的起点常晚于打字机打出的头一两个字（「こ」「ええ、キ」），收紧到几个原生帧的话，
    摆帧 20 条里 8 条把起点挪过了头（检测器三场修复计划 U2，撤回）。

    这句字最早出现的时刻 = 正文事件，以及**在正文的位置上**（框和某个正文事件相交）、正文出现之前就在屏、
    内容能是这句字开头的读数（`textfold.leads`：同一句的变体、打字刚出的几个字、只有标点）里最早的起点。
    这些读数常常不在正文里：多读一个句号的变体、被藏成半截行的打字中途读数，只看正文会把起点推得比字出现还晚。
    "这句字"既拿正文事件的读数比，也拿剧本原文 `ref_text` 比：正文事件本身读错时（zzz `SKILL INFD`），
    同一处更早、和原文一字不差的读数和它对不上，只比正文会把起点挪晚 66 s。

    为什么要挪：cue 的起点由它全部的事件定，名牌、计数器、按键提示、还没消失的上一句都算，gi1 有一句被同一条 cue 里先出来的名牌
    「カチーナ」拉早 24 s、一句被计数器「19000/24829」拉早 21.8 s，译文先浮在还没出字的地方（时间疑点检测器计划）。

    **借来的读数**（上面那些不在 `evs` 里、正文出现前就在屏的同一句读数）由调用方记进正文条目的 `events`（`overlay_items`）：
    块盖住了它们，不记的话选项路会拿它们在同一处再出一块、`unmatched` 组补画它们的 OCR 原文、打字机只看正文事件，
    把早就在屏的整句判成慢慢打出（2026-10-24，zzz `SKILL INFO` 判成 66.5 s，defaults.md 1.66）。"""
    first = min(e["t_start"] for e in evs)
    if first - a <= tol:
        return a, []
    ids, boxes = {e["id"] for e in evs}, [e["box"] for e in evs]
    texts = [e["text"] for e in evs] + ([ref_text] if ref_text else [])
    t, lean = first, []
    for e in index.window(a - BODY_LOOKBACK_US, first):
        if (e["id"] not in ids and e["t_end"] > a and any(tracksio.boxes_cross(e["box"], b) for b in boxes)
                and any(textfold.leads(e["text"], x) for x in texts)):
            t = min(t, e["t_start"])
            lean.append(e)
    return (t if t - a > tol else a), lean


def yield_to_body(name_items: list[dict], body_items: list[dict]) -> tuple[list[dict], int]:
    """一个事件同一时刻只画一块：任何正文块 `events` 里有的事件（正文事件本身，和定起点借来的读数，`body_start`），名牌层让出来——返回 (留下的名牌块, 让出的块数)。
    实际碰到的是借来的读数：被认成开头行、又是这句台词早出现的那次读数的，是台词被当成了名牌（《魔裁》f3 9:05:24 起的「裁判の後。」：
    名牌层在同一个框里另画一块 OCR 原文，盖在正文块上 63 s）。"""
    in_body = {i for it in body_items for i in it["events"]}
    kept = [it for it in name_items if it["events"][0] not in in_body]
    return kept, len(name_items) - len(kept)


NAME_ROW_TOL = 0.5
"""`yield_to_block`：名牌块的下沿离同一区域前后名牌下沿的中位超过这么多个正文行距，才当成台词的首行（暂定）。"""


def yield_to_block(items: list[dict], doc: dict, textbox: dict) -> tuple[list[dict], int]:
    """`name` 层的块（含归 stray_name 组的）被正文原文整块借成了一行（`block_drawn_by` 指着它）、位置又不在名牌该在的地方的，
    是台词的首行被认成了名牌：去掉这块，整块那一行改记没人画（阶段 3 当借来的事件、不另画）——返回 (留下的条目, 去掉的块数)。

    "名牌该在的地方"按前后文定：同一区域时间上前后各 `TB.INERTIA_K` 块名牌（没被整块借走的）的下沿中位，和惯性同一个思路——
    名牌跟着文本框，一款游戏里它的位置是定的。参照不到 `TB.INERTIA_MIN_REF` 块的不动。整块借这个事件时已经核过字（对上剧本那一行日文）
    和位置（落在按行距推出的那一行上，`TB.fill`），这里再要它离名牌的位置差 `NAME_ROW_TOL` 个行距以上，喊名字的台词（`カチーナ！`）
    恰好贴着名牌时不误去。实例是《魔裁》两行台词的短首行（`はい。`、`そう。`、`あら？`）：剧本里不是一整条台词、`head_lines` 排不掉，
    下沿约 909 px、名牌约 835 px，原来名牌层在台词首行上另画一块 OCR 原文、和正文译文叠两层。

    称号层（`title`）不参照也不去：它和名字叠着画、位置不同，混进中位会把名牌的位置拉偏；九场里也没有称号块被整块借走过。
    在 `name_roles` 之后跑：去掉的块原来若当过别的名牌的称号搭档、或帮一串名牌判成挂上，剩下的块不重算（九场里没出现）。"""
    events = doc["events"]
    uses: dict[int, int] = {}                                   # 名牌块下标 -> 借它的正文条目下标
    for k, it in enumerate(items):
        for by in it.get("block_drawn_by") or ():
            if by is not None and items[by]["layer"] == "name":
                uses.setdefault(by, k)
    if not uses:
        return items, 0
    by_reg: dict[int, list[int]] = defaultdict(list)
    for k, it in enumerate(items):
        if it["layer"] == "name":
            by_reg[events[it["events"][0]]["region"]].append(k)
    drop = set()
    for by, k in uses.items():
        order = sorted(by_reg[events[items[by]["events"][0]]["region"]], key=lambda i: items[i]["start_us"])
        n = order.index(by)
        refs = sorted(items[i]["rows"][0][3] for i in order[max(0, n - TB.INERTIA_K):n] + order[n + 1:n + 1 + TB.INERTIA_K] if i not in uses)
        pitch = (textbox.get(str(events[items[k]["events"][0]]["region"])) or {}).get("pitch")
        if len(refs) >= TB.INERTIA_MIN_REF and pitch and abs(items[by]["rows"][0][3] - refs[(len(refs) - 1) // 2]) > NAME_ROW_TOL * pitch:
            drop.add(by)
    if not drop:
        return items, 0
    at = {}
    out = []
    for k, it in enumerate(items):
        if k not in drop:
            at[k] = len(out)
            out.append(it)
    for it in out:
        if it.get("block_drawn_by"):
            it["block_drawn_by"] = [None if by is None or by in drop else at[by] for by in it["block_drawn_by"]]
    return out, len(drop)


def overlay_items(recs: list[dict], doc: dict, evs_of: list[list[dict]], cues: list,
                  ref: GA.Ref | None = None, readables: dict[str, tuple[str, str]] | None = None,
                  obs_path: Path | None = None, speakers: dict[str, str] | None = None,
                  script_lines: frozenset[str] | None = None, layout: dict | None = None) -> tuple[list[dict], dict, dict]:
    """**判定**：各层该盖哪些块，产出写进 `matched.json` 的 `overlay.items`（语言无关，jp / cn 都带）。
    导出器（`export_overlay`）只按层和语言投影，不再现场判断（artifacts.md 第 1 条）。
    返回 (条目, 各层计数, 文本框画像 `overlay.textbox`)；正文 / extra 条目带原文整块（`textbox.annotate`，计数里的 `textbox` 是它的统计）。

    `speakers`：名牌层的说话人表（日文名按 `SA.norm` 归一 -> 中文名）。不给就从 `ref` 取（`speaker_names`）；
    没有 `GA.Ref` 的匹配器（`naninovel`）用它交进自己的名字表——不交的话名牌层整层没有译文（2026-10-24，宣传片素材上的 `セナ` / `リオ`）。
    `script_lines`：开头行要排掉的剧本台词（`GA.head_lines` 的同名参数）。不给就从 `ref` 取 `GA.band_lines`；
    `naninovel` 交自己的（`naninovel.head_script_lines`）——不交的话落在两行 cue 首行的短台词（`ああ。`）被认成名牌、画出 OCR 原文。

    顺序 body → choice → panel → extra → name：前三层与 2026-09-14 之前 `export_overlay` 现场判出来的
    **顺序与内容相同**（开 body,choice,offmain 时叠加 ASS 与之前逐字节相同，带阅读面板（当时读上游 TextMap 的原型）再加 panel 也相同，
    matcher 计划的回归；那两道 offmain 过滤在那两段切片上不触发），新两层排在后面。
    ⚠ **保持关注**（product-goals 第 16 条）：选项 / 面板的规则是按预览摆帧逐条长出来的，别再按单帧补。"""
    # 和匹配用的同一份开头行（`GA.run`）：剧本在手就排掉本身是整条台词的
    if script_lines is None:
        script_lines = GA.band_lines(ref.script) if ref is not None else frozenset()
    heads = GA.head_lines([(c.start, "\n".join(c.lines), "") for c in cues], script_lines)
    body_items, extra_items = [], []
    # 和 body_items / extra_items 逐条对应：(画的那条 cue 的正文事件, 那条 cue 的全部事件)，建原文整块用
    shown_body: list[tuple[list[dict], list[dict]]] = []
    shown_extra: list[tuple[list[dict], list[dict]]] = []
    index = tracksio.EventIndex(doc["events"], BODY_LOOKBACK_US)
    parts_of = {ri: [(i, body_of(evs_of[i], heads, r["ref"].get("jp") or "", r["ref"].get("jp_tpl") or "")) for i in r["cues"]]
                for ri, r in enumerate(recs) if r["ref"]}
    own = {e["id"] for parts in parts_of.values() for _, b in parts for e in b}     # 各条认领自己的正文事件：别的正文块不借
    for ri, r in enumerate(recs):
        if not r["ref"]:
            continue
        s_us, e_us = int(round(r["start"] * 1e6)), int(round(r["end"] * 1e6))
        parts = parts_of[ri]
        body = [e for _, b in parts for e in b]
        mates = [e for i in r["cues"] for e in evs_of[i]]
        taken = {e["id"] for e in body}
        for seg in position_segments(parts, cues, s_us, e_us):
            evs, rows, a, b = seg.evs, seg.rows, seg.start, seg.end
            jp = r["ref"].get("jp") or ""
            a, lead = body_start(a, evs, jp, index, int(BODY_SHIFT_TOL * doc["frame_us"]))
            refined = "text_effect" in doc and any("t_full_how" in e for e in evs)      # 同 `script.refined_of`
            b = body_end(b, a, evs, mates, jp, BODY_END_SLACK_US if refined else int(tracksio.SAMPLE_SLACK * doc["frame_us"]))
            # 定起点时借来的同一句读数也记进 `events`、排在正文事件后面（区域、对齐仍取第一个正文事件的；`body_end` 认的不记，见它的说明）：
            # 块盖住了它们，一个事件同一时刻只画一块（选项路、`unmatched` 补画都不再用），打字机也看得见这句字早就在屏。
            # 别的认领自己的正文事件不借（对话记录滚动时上一格的同一句是另一条记录的正文，它有自己的块）
            lent = [e for e in lead if e["id"] not in taken and e["id"] not in own]
            taken |= {e["id"] for e in lent}
            body_items.append({"layer": "body", "group": r["ref"]["group"], "fill": r["ref"]["fill"],
                               "start_us": a, "end_us": b, "rows": rows,
                               "events": list(dict.fromkeys(e["id"] for e in evs + lent)), "key": r["ref"]["key"],
                               **{k: r["ref"][k] for k in matchedio.TEXT_KEYS if k in r["ref"]},
                               "claim": [ri, -1], "echo": r.get("echo"), "echo_of": r.get("echo_of")})
            shown_body.append((seg.shown, evs_of[seg.cue]))
        # extra 盖它自己的那几行：字落在这条剧本行里、而且不是主认领已经用掉的事件（含正文块定起点借来的读数）
        for xi, x in enumerate(r["extra"]):
            parts = [(i, [e for e in body_of(evs_of[i], heads, x.get("jp") or "", x.get("jp_tpl") or "") if e["id"] not in taken])
                     for i in r["cues"]]
            for seg in position_segments(parts, cues, s_us, e_us):
                evs, xrows, a, b = seg.evs, seg.rows, seg.start, seg.end
                taken |= {e["id"] for e in evs}
                # 时间取**它自己那几个事件**的（裁进这一段的范围）：选项按钮往往比正文晚出现好几秒，
                # 用记录的起止会让译文先浮在空处（2026-09-14 摆帧：zzz 18 条里 17 条事件时段不到记录的 80%）
                x_s = max(a, min(e["t_start"] for e in evs))
                x_e = min(b, max(e["t_end"] for e in evs))
                extra_items.append({"layer": "extra", "group": x["group"], "fill": x["fill"],
                                    "start_us": x_s, "end_us": max(x_s, x_e), "rows": xrows,
                                    "events": list(dict.fromkeys(e["id"] for e in evs)), "key": x["key"],
                                    **{k: x[k] for k in matchedio.TEXT_KEYS if k in x},
                                    "claim": [ri, xi], "echo": x.get("echo"), "echo_of": x.get("echo_of")})
                shown_extra.append((seg.shown, evs_of[seg.cue]))
    # 名牌：主轨里被认成开头行的**事件**（一个事件一块，时间取事件自己的——名牌跨好几条 cue 挂着）。
    # `名前：台词` 那种同一行格式框不出名字那半，不进这一层
    if speakers is not None:
        names, has_speakers = speakers, bool(speakers)
    else:
        names = speaker_names(ref) if ref is not None else {}
        has_speakers = ref is not None and "speakers" in ref.doc
    terms = term_names(ref) if ref is not None else {}
    # 短名词表按字形查，只给"挂在台词上"的开头行查（`on_dialogue`）：任务目标、装备名这类 UI 不当名牌译
    share = on_dialogue(heads, cues, recs) if terms or names else {}
    name_items, seen = [], set()
    n_terms_off = 0
    # 名牌投票的证据（`fuzzy_names`）：读数 -> {它挂着的记录: 那条记录的说话人}
    rec_of = {i: ri for ri, r in enumerate(recs) for i in r["cues"]}
    spk = record_speakers(recs, ref)
    votes: dict[str, dict[int, tuple[str, str]]] = defaultdict(dict)
    for i, evs in enumerate(evs_of):
        ri = rec_of.get(i)
        for e in evs:
            k = SA.norm(e["text"])
            if k not in heads:
                continue
            # owner 2026-09-14：名牌几乎总和正文一起出现——同一条 cue 里要有一行正文挂在它下面（`tracksio.name_over_body`），
            # 挡掉被 head_lines 认进来的键位 / UI（hsr 整场 `回` 108→21、`A` 84→12、`D` 32→0，matcher 计划；
            # 间隔上限 2026-11-05 从 2 个字高放到 `NAME_GAP` 的 3 个后是 21、21、2 块，收进来的这类再由 `name_roles` 归 `stray_name`）。
            # 同一个事件在别的 cue 里满足也算，所以只在收下时记 seen；投票每条 cue 都记
            # 左右按从这一行往下连着的那块台词比（`tracksio.rows_below` + `block_head`）：绝区零的台词左对齐、名牌居中，首行短时首行够不到名牌下面
            body = [b["box"] for b in evs if b is not e and SA.norm(b["text"]) not in heads and evalkit.content_len(b["text"]) >= 3]
            if not any(tracksio.name_over_body(e["box"], tracksio.block_head(tracksio.rows_below(body, b), b)) for b in body):
                continue
            if ri is not None and spk[ri]:
                votes[k][ri] = spk[ri]
            if e["id"] in seen:
                continue
            seen.add(e["id"])
            # 名字对上了说话人表（或短名词表）才算"有译文"：没对上的名牌这一层不出译文（jp 口径也不拿 OCR 读数冒充"原文"）；
            # 原文另存 ocr。`unmatched` 组可见时（`dev` / `default_all`）这类没有译文的条目退回画 OCR 原文（`output.script.build_items`）
            term = terms.get(k) if share.get(k, 0.0) >= TERM_ON_DIALOGUE else None
            n_terms_off += bool(terms.get(k) and not term and not names.get(k))
            cn, src = (names[k], "speakers") if names.get(k) else (term, "terms") if term else (None, None)
            # 在动的名牌按位置切段（`event_segments`，和正文 / extra 的 `position_segments` 同一条规矩）
            for box, a, b in event_segments(e):
                name_items.append({"layer": "name", "group": "name", "fill": False, "start_us": a, "end_us": b,
                                   "rows": [box], "events": [e["id"]],
                                   "jp": e["text"] if cn else None, "cn": cn, "name_src": src,
                                   "ocr": e["text"]})
    name_items, n_lent = yield_to_body(name_items, body_items)
    n_fz = fuzzy_names(name_items, share, votes)
    # 英文名只是跟着中文名带：认名（逐字查、投票、借）照旧只看中文，认出是谁以后按那个日文名查英文（说话人表优先，同中文）
    en_names = {**term_names(ref, "en"), **speaker_names(ref, "en")} if ref is not None else {}
    for it in name_items:
        if it["cn"] and (en := en_names.get(SA.norm(it["jp"]))):
            it["en"] = en
    name_items.sort(key=lambda it: it["start_us"])
    if n_lent:
        print(f"[名牌] 事件已记进正文块（定起点借来的读数）、名牌层让出的 {n_lent} 块")
    if n_fz["voted"]:
        print(f"[名牌] 逐字查不到、按下面台词的说话人投票认上的 {n_fz['voted']} 个名牌事件（name_src = voted）")
    if n_fz["fuzzy"]:
        print(f"[名牌] 读错一个字、借本段已译出的名牌认上的 {n_fz['fuzzy']} 个名牌事件（name_src = fuzzy）")
    if n_terms_off:
        print(f"[名牌] 短名词表查得到、但不挂在台词上（< {TERM_ON_DIALOGUE}，多是任务目标 / 装备 / 技能名）没译的 {n_terms_off} 个名牌事件")
    # 一个事件同一时刻只画一块：正文 / 第二条认领用掉的事件选项层不再用。名牌和选项层那条路撞在同一个事件上时，
    # 名牌对上了译名（说话人表 / 短名词表 / 容错查）就归名牌——选项层按归一后逐字相等认，名牌 `カチーナ` 会撞上台词 `カチーナ！`；
    # 没译出的名牌多是被认成开头行的按钮 / 界面字（hsr 的选项 `絵の世界について`、zzz 的 `SKILL INFO`，2026-10-07 四场整场），归选项层那条路，名牌条目去掉
    choices: list[dict] = []
    if ref is not None:
        choices = choice_items(doc, tracksio.main_regions(doc), ref,
                               {i for it in body_items + extra_items for i in it["events"]}
                               | {it["events"][0] for it in name_items if it["cn"]})
    panels, n_obs = [], 0
    if readables:
        # 台词先认领自己的正文事件，面板只拿剩下的：原型直接读上游 TextMap 时长条目里也有对话台词本身，
        # 面板先挑的话 3550 个事件里有台词的正文，打字机叠加从 479 条掉到 272 条（文本包的表已经去掉了台词，这道仍留着）
        taken_ev = [doc["events"][i] for it in body_items for i in it["events"]]
        panels, n_obs = readable_items(obs_path, readables, doc["frame_us"], taken_ev)
        # 面板块里的"选项"不是选项：面板小标题 `ブラーチ` / `羊飼い` 归一后和选项按钮逐字相等，块已经盖了它们
        blocks = [(p["start_us"], p["end_us"], p["rows"][0]) for p in panels]
        choices = [c for c in choices if not any(bs < c["end_us"] and c["start_us"] < be
                                                 and box_inside(c["rows"][0], bb) >= 0.8 for bs, be, bb in blocks)]
    in_choice = {i for it in choices for i in it["events"]}
    name_items = [it for it in name_items if it["cn"] or it["events"][0] not in in_choice]
    n_roles = name_roles(name_items, body_items + extra_items)
    print(f"[名牌] 挂在认上的台词上的名字 {n_roles.get('name', 0)} 块、称号 {n_roles.get('title', 0)} 块；"
          f"没挂上、归 {matchedio.STRAY_NAME}（默认不画）的 {n_roles.get(matchedio.STRAY_NAME, 0)} 块")
    items = body_items + choices + panels + extra_items + name_items
    # 原文整块与文本框画像（line-breaks 计划）：名牌分组、选项去重都做完之后才算，只写条目的新字段，不动 `rows`
    x_at = len(body_items) + len(choices) + len(panels)              # extra 条目在 items 里从这里起
    textbox, tb_stats = TB.annotate(items, doc, {**dict(enumerate(shown_body)), **{x_at + k: v for k, v in enumerate(shown_extra)}}, layout)
    # 名牌层在 items 末尾，去掉其中几块不挪正文 / extra 的下标
    items, n_head = yield_to_block(items, doc, textbox)
    kept = {id(it) for it in items}
    name_items = [it for it in name_items if id(it) in kept]
    tb_stats["names_yielded"] = n_head
    tb_stats["drawn_by"] = sum(1 for it in items for x in it.get("block_drawn_by") or () if x is not None)
    if n_head:
        print(f"[名牌] 被正文原文整块借成首行、位置不在名牌处的 {n_head} 块：是台词首行，名牌层去掉")
    for ln in TB.summary(textbox, tb_stats):
        print(ln)
    stats = {k: sum(1 for it in items if it["layer"] == k) for k in LAYERS}
    # 名牌层的 `name` / `title` 照层的惯例数块（在动的名牌切成几块，`event_segments`）；下面几个按**事件**数（去重、两层合起来），
    # 和 2026-10-05 之前的读数同口径（那时一个事件一块）
    stats["name_events"] = len({it["events"][0] for it in name_items})
    stats["name_translated"] = len({it["events"][0] for it in name_items if it["cn"]})
    stats["name_from_terms"] = len({it["events"][0] for it in name_items if it["name_src"] == "terms"})
    stats["name_from_fuzzy"] = len({it["events"][0] for it in name_items if it["name_src"] == "fuzzy"})
    stats["name_from_voted"] = len({it["events"][0] for it in name_items if it["name_src"] == "voted"})
    stats["name_terms_off"] = n_terms_off          # 短名词表查得到、但被 `on_dialogue` 门挡掉没译的名牌事件
    stats["panel_obs_used"] = n_obs
    # 剧本有没有说话人表（gametext 是剧本的 `speakers` 表，交了 `speakers` 的是那张表非空）：没有的话名牌层结构上一条译文都不会有
    # （`speaker_names` 的 ⚠），记进产物，免得下次看见 `name_translated 0` 去查判据
    stats["ref_has_speakers"] = has_speakers
    stats["textbox"] = tb_stats
    return items, stats, textbox


def run_match(ref: GA.Ref, subs: str, feed: str, feed_panel: str, fuzzy: float, merge_gap: float,
              overlay: bool = True, readables: bool = False, obs: str | None = None,
              doc: dict | None = None, events: bool = False, layout: str | None = None, viewport: str | None = None) -> dict:
    """匹配的**主体**：喂入 → 匹配 → 并相邻 → （有框时）判叠加层。**不打印统计、不写文件。**

    CLI（`main`）和内置匹配器（`flowocr.analyze.matchers.gametext.match`）都走这里，判据只有这一份
    （2026-09-22 从 main 里拆出来，正规化 project-structure）。`subs` 是 SRT 或 `*-tracks.json` 的路径；
    `doc` 给了就不再从 `subs` 读（匹配器 API 手上已经有 doc），`feed == "main"` 仍要 `subs` 指到文件。
    `events`：不判叠加层也要每条 cue 的事件（匹配器猜占位的值要用，比如原神读名牌）。
    `layout`：版式文件的路径（`--ctx layout=`，`none` 是不用）；没给按剧本的游戏找随代码走的那份（`textbox.layout_for`）。
    `viewport`：游戏画面在录像里的位置（`--ctx viewport=x,y,w,h`）；没给用建轨时记下的（`tracksio.recorded_viewport`），都没有按版式文件推算（`textbox.locate`）。
    返回 `recs` / `n_raw` / `suspect_raw` / `cues` / `items` / `ov_stats` / `textbox` / `doc_t` / `evs`（每条 cue 的事件，没读 tracks 是 None）/
    `subs_path` / `tp`。"""
    panel_of = None
    if feed == "main":
        subs_path, tp = GA.resolve(subs)
        cues = srtio.read_subs([subs_path])
        evs_of = None                      # 下面按主轨和 SRT 逐条对账之后再取
        if doc is None and subs.endswith("-tracks.json"):
            doc = tracksio.load(Path(subs))    # 给的是 tracks：采样间隔、采样网格和叠加层都从它取，CLI 和匹配器 API 同一判法
    else:
        if doc is None and not subs.endswith("-tracks.json"):
            raise SystemExit(f"--feed {feed} 要从一级产物投影，--subs 必须给 *-tracks.json")
        # **全量喂入不要求"有合格主轨"**（2026-09-18 审计）：`GA.resolve` 会在 `main_srt` 为空时直接退出，
        # 而"挑不出主轨、但屏幕上仍有可匹配文字"是合法产物——那正是全量喂入要覆盖的情形。
        # 所以这条路只从 tracks + provenance 取，不过 resolve。
        if doc is None:
            doc = tracksio.load(Path(subs))
        prov = doc["provenance"]
        subs_path = Path(subs) if subs else Path("")
        tp = {k: prov.get(k) for k in ("git_head", "code_fp")}
        cues, evs_of, panel_of = feed_from_doc(doc, feed, feed_panel)
        print(f"[喂入] --feed {feed}：{len(cues)} 条 cue（从 tracks.json 投影，"
              f"并带的那条聚合轨已排除；没并带时不排——那是真实区域）")
    main_of = cue_main(cues, evs_of, doc)
    # 帧级起点：夹缝认领的"同刻"容一个采样间隔（秒），回显判定的"这一刻"按采样格号比；只喂 SRT、手上没有 tracks 时都不知道，照原来的判法
    step, grid = (doc["frame_us"] / 1e6, EC.grid_of(doc)) if doc is not None else (0.0, None)
    recs, _score = match(ref, cues, fuzzy, panel_of, feed_panel, main_of, step)
    n_raw = len(recs)
    suspect_raw = sum(1 for r in recs if r["ref"] and r["how"] not in NOT_BY_SIM and r["ref"]["score"] < SUSPECT)
    log_panel_of = log_panels(ref, cues, evs_of, panel_of, recs, merge_gap) if panel_of is not None else None
    recs = merge_runs(recs, merge_gap, [c.source for c in cues], log_panel_of)
    mark_echoes(ref, recs, cues, main_of, panel_of, log_panel_of, grid)
    mark_display(recs, cues, lambda c: group_of(c["kind"], c.get("status"), ref.role.get(c["key"])))
    # 叠加层的判定（有框才判得了）：写进 JSON，--ass 只投影
    items = ov_stats = doc_t = evs = name_ink = textbox = None
    # tracks 只在要用时读：叠加层要框，匹配器猜占位的值可能要事件（`--no-overlay` 又不要事件时照旧不碰 tracks、不对账）
    if (doc is not None or subs.endswith("-tracks.json")) and (overlay or events):
        doc_t = doc if doc is not None else tracksio.load(Path(subs))
        evs = cue_events(doc_t, cues) if evs_of is None else evs_of
    if doc_t is not None and overlay:
        paras, panel_src = readables_of(ref) if readables else (None, None)
        items, ov_stats, textbox = overlay_items(recs, doc_t, evs,
                                        cues, ref=ref, readables=paras,
                                        obs_path=Path(obs) if obs else None,
                                        layout=TB.layout_for(layout, (ref.doc.get("provenance") or {}).get("game"),
                                                             viewport or tracksio.recorded_viewport(doc_t)))
        if panel_src:
            ov_stats["panel_gametext"] = panel_src
        # 名牌字色（owner 2026-11-05）：和匹配器同一阶段，只判认上的名牌；阶段 3 照判定写样式，不读视频
        name_ink = NI.check(items, doc_t, (ref.doc.get("provenance") or {}).get("game"))
        ov_stats["name_plain"] = sum(1 for it in items if it.get("ink") is False)
    return {"recs": recs, "n_raw": n_raw, "suspect_raw": suspect_raw, "cues": cues, "items": items, "ov_stats": ov_stats,
            "textbox": textbox, "name_ink": name_ink, "doc_t": doc_t, "evs": evs, "subs_path": subs_path, "tp": tp}


def cue_main(cues: list, evs_of: list[list[dict]] | None, doc: dict | None) -> list[bool]:
    """每条 cue 在不在主轨上：按 cue 的事件判（`tracksio.in_main`，任一个事件在主轨的区域就算）；只喂主轨（`--feed main`，没有事件）时全在主轨上。
    回显判定（定主）和第四遍认领（`game_align.claim_infix`）用的是同一份。"""
    if evs_of is not None and doc is not None:
        regs = tracksio.main_regions(doc)
        return [any(tracksio.in_main(e, regs) for e in evs) for evs in evs_of]
    return [True] * len(cues)


def mark_echoes(ref: GA.Ref, recs: list[dict], cues: list, main_of: list[bool],
                panel_of: list[bool] | None, log_panel_of: list[bool] | None = None,
                grid: EC.Grid | None = None) -> None:
    """回显判定（`flowocr.analyze.echo.mark`）：锚点是剧本对齐序列里这一行的排序时刻（gamescript 从 obs 定的第一次上屏），
    "在主轨上"是 `cue_main`。`grid`：tracks 的采样网格（`echo.grid_of`；只喂 SRT 时 None）。"""
    rep = lambda k: ref.variant_of.get(k, k)   # noqa: E731
    EC.mark(recs, [c.source for c in cues], main_of, panel_of or [False] * len(cues),
            EC.anchors(ref.doc["sequence"], rep), rep, log_panel_of, grid)


def match_stats(ref: GA.Ref, recs: list[dict], suspect_raw: int | None = None) -> dict:
    """产物 `stats` 和报表共用的那几个数（覆盖 / 重复认领 / 可疑改写的分布），只算一次。

    `suspect_raw`：`merge_runs` 之前的可疑改写条数（`run_match` 给）。合并留组内最高的分，低分那条的报警
    随合并消失、文字和时段却没改——所以合并前后两个数一起报，合并改动不能只看合并后的（2026-09-26 审计）。"""
    how = Counter(r["how"] for r in recs)
    conf = Counter(r["conf"] for r in recs)
    # 覆盖按**代表行**数，才和 game_align 的命中同口径：产品输出的是屏幕上那一种说法
    # （`pick_variant`），而分母里一个变体组只有代表行那一条
    claimed = {ref.variant_of.get(k, k) for k in
               ({r["ref"]["key"] for r in recs if r["ref"]} | {e["key"] for r in recs for e in r["extra"]})}
    band = [e for e in ref.script if GA.band(e)]
    n_band_claimed = sum(1 for e in band if e.key in claimed)
    # 导出契约：SRT 只出主 ref。分母内只由 `extra` 认领的行**不会出现在字幕里**，
    # 所以"覆盖"和"SRT 实际出的"是两个数（模块说明）。
    main_keys = {ref.variant_of.get(r["ref"]["key"], r["ref"]["key"]) for r in recs if r["ref"]}
    n_band_main = sum(1 for e in band if e.key in main_keys)
    n_extra_recs = sum(len(r["extra"]) for r in recs)
    # **按最终记录数，不按 `st`**（2026-09-18 审计）：`st` 是 `game_align.score` 的原始认领，
    # 而 `--feed-panel strict` 会**撤销**一部分认领、`merge_runs` 会并掉相邻的同一行——
    # 拿 `st` 算出来的"重复认领"于是和产物里真出现的次数不是一回事（strict 臂上它一点不动，看着像没效果）。
    keys = [r["ref"]["key"] for r in recs if r.get("ref")]
    keys += [x["key"] for r in recs for x in r.get("extra", ())]
    # 按**代表行**数（owner 2026-10-06）：同一句的两种说法（变体组）各被认领一次也算重复，和上面"覆盖"同口径；
    # 之前按记录的原始 key 数，zzz 整场 228 次，折叠后 236
    ci = Counter(ref.variant_of.get(k, k) for k in keys)
    groups, extra = sum(1 for v in ci.values() if v > 1), sum(v - 1 for v in ci.values())
    # 按带内 / 带外分开报（owner 2026-10-06，matcher-next 计划）：带外是文本包里的 UI 文案、散条目、选项，
    # 本来就反复上屏（zzz 整场多出来的认领 84% 是技能说明、按键提示、时钟），混在一个数里会把台词的报警淹掉。
    # 匹配器照认（用途 1 要它们的译文），只在报数里分开；报警只看带内
    # `ci` 的键已经是代表行：变体行的 kind 是 `Variant`、不算带内，按原始 key 判的话台词的另一种说法会记成带外
    band_keys = {e.key for e in band}
    extra_band = sum(v - 1 for k, v in ci.items() if k in band_keys)
    groups_band = sum(1 for k, v in ci.items() if v > 1 and k in band_keys)
    # 带内多出来的认领按回显形态拆（owner 10-06，matcher-next 计划第 1 节第 6 条）：报警看 `other`；带外仍只报合计
    echo_band = EC.counts(recs, band_keys, lambda k: ref.variant_of.get(k, k))
    scores = [r["ref"]["score"] for r in recs if r["ref"] and r["how"] not in NOT_BY_SIM]
    return {"how": how, "conf": conf, "band": band, "n_band_claimed": n_band_claimed,
            "n_band_main": n_band_main, "n_extra_recs": n_extra_recs, "groups": groups, "extra": extra,
            "groups_band": groups_band, "extra_band": extra_band, "echo_band": echo_band,
            "scores": scores, "suspect_raw": suspect_raw}


def report(st: dict, recs: list[dict], n_raw: int, merge_gap: float, fuzzy: float,
           ov_stats: dict | None, readables: bool) -> None:
    """CLI 的统计打印（只打印，什么都不算——数都在 `match_stats` 里）。"""
    how, conf, band, scores = st["how"], st["conf"], st["band"], st["scores"]
    n_band_claimed, n_band_main, n_extra_recs = st["n_band_claimed"], st["n_band_main"], st["n_extra_recs"]
    print(f"cue {n_raw} 条 -> 并掉同一条剧本行的相邻 cue 之后 {len(recs)} 条（--merge-gap {merge_gap}）：" + "、".join(f"{k} {how[k]}" for k in ("exact", "fuzzy", "contain", "context", "none"))
          + f"；置信度 " + "、".join(f"{k} {conf[k]}" for k in ("high", "med", "low", "none")))
    print(f"覆盖：该上屏的 {len(band)} 条里 {evalkit.denom(n_band_claimed, len(band))} 被认领"
          f"（和 game_align 的命中同口径）")
    print(f"  其中**拍平的 SRT 里出现的** {evalkit.denom(n_band_main, len(band))}："
          f"第二条剧本行（`extra`）共 {n_extra_recs} 条不进 SRT（其中分母内、没有别的 cue 认领的 "
          f"{n_band_claimed - n_band_main} 条）；"
          + (f"叠加 ASS 的 extra 层找到框的 {ov_stats['extra']} 条" if ov_stats else "给 tracks.json 才有叠加层"))
    if ov_stats:
        print("叠加层（判定写进 --out 的 overlay.items；画不画按显示协议的组，预设的 show / hide 开关）："
              + "  ".join(f"{k} {ov_stats[k]}" for k in LAYERS)
              + f"；名牌事件有中文名的 {ov_stats['name_translated']}/{ov_stats['name_events']}"
              + f"（其中查短名词表的 {ov_stats['name_from_terms']}）"
              + ("" if ov_stats["ref_has_speakers"] else "（**剧本没有说话人表**，这一档结构上就是 0）")
              + (f"；阅读面板用掉 {ov_stats['panel_obs_used']} 条观测" if readables else ""))
    eb = st["echo_band"]
    print(f"重复认领：{st['groups']} 条被 ≥2 条 cue 认领，多出 {st['extra']} 次（报警不是错误数）"
          f"——带内（该在字幕带的台词）{st['groups_band']} 条、多出 {st['extra_band']} 次："
          f"重播 {eb['replay']}、回显 {eb['same_time'] + eb['log']}（同时 {eb['same_time']}、记录面板 {eb['log']}）、"
          f"**其他 {eb['other']}（报警看这个）**；"
          f"带外（UI 文案、散条目、选项，本来就反复上屏）多出 {st['extra'] - st['extra_band']} 次，不进报警")
    if scores:
        if fuzzy >= SUSPECT:
            print(f"  ⚠ --fuzzy {fuzzy} ≥ 可疑改写的门 {SUSPECT}：下面那个数结构上恒为 0，别读（见 SUSPECT）")
        print("改写幅度（被换成原文的 cue 与剧本行的相似度；`contain`、`context` 不在分布里——整行都在 cue 里、"
              "凭上下两行的认领认上，相似度不是它们的判据）："
              + "  ".join(f"P{q*100:.0f} {evalkit.fmt_pctl(scores, q, unit='')}"
                          for q in (0.05, 0.25, 0.5))
              + f"；<{SUSPECT} 的 {sum(1 for s in scores if s < SUSPECT)} 条**是可疑改写，要摆帧**"
              + (f"（合并前 {st['suspect_raw']} 条）" if st.get("suspect_raw") is not None else "")
              + f"（`--fuzzy {fuzzy}` 已经把更低的挡在门外，所以这条线必须更高）")


def matched_doc(ref: GA.Ref, recs: list[dict], n_raw: int, st: dict, ref_path: str, subs_path: Path, tp: dict,
                argv: list[str], params: dict, items, ov_stats, evs, textbox: dict | None, fills: dict | None = None,
                name_ink: dict | None = None) -> dict:
    """`*-matched.json`（`SCHEMA`）。provenance 记这次是谁、用哪版代码、什么参数产的。
    `evs`：喂给匹配器的每条 cue 的事件（`run_match` 的 `evs`），叠加层的 `fed` 由它来（显示协议）。`textbox`：文本框画像（`run_match` 的 `textbox`）。
    `fills`：匹配器猜的占位的值（`{占位: {"value": 值或 None, "by_lang": {"jp": …, "cn": …, "en"?: …}, "how": 从哪来, …}}`，`flowocr.placeholders`）。
    `name_ink`：名牌字色判过时这款游戏各层的名牌色（`nameink.check`），记进 `overlay.name_ink`。"""
    from flowocr.analyze import build_tracks as bt
    pv = ref.doc["provenance"]
    scores = st["scores"]
    return {"schema": SCHEMA,
            "provenance": {"tool": "scriptmatch.py", "version": bt.version(), "git_head": bt.git_head(),
                           "code_fp": bt.code_fp(CODE_FP_FILES), "argv": [bt.portable(x) for x in argv],
                           "ref": bt.portable(ref_path), "ref_code_fp": pv.get("code_fp"),
                           "subs": bt.portable(subs_path), "tracks_code_fp": tp.get("code_fp"),
                           "game": pv.get("game"),
                           # 剧本出自哪份语料（gamescript 从文本包清单抄的）；通用剧本没有这一项
                           "gametext": {k: pv["gametext"].get(k) for k in ("fingerprint", "rev", "version", "source")}
                           if pv.get("gametext") else None,
                           "params": {"fuzzy": params["fuzzy"], "merge_gap": params["merge_gap"], "feed": params["feed"],
                                      "feed_panel": params["feed_panel"],
                                      "readables": bool(params.get("readables"))}},
            "stats": {"cues": len(recs), "cues_raw": n_raw, "how": dict(st["how"]), "conf": dict(st["conf"]),
                      "band_entries": len(st["band"]), "band_claimed": st["n_band_claimed"],
                      "band_claimed_exported": st["n_band_main"], "extra_records": st["n_extra_recs"],
                      "suspect_below": SUSPECT,
                      "suspect": sum(1 for s in scores if s < SUSPECT), "suspect_raw": st.get("suspect_raw"),
                      "repeat_groups": st["groups"], "repeat_extra": st["extra"],
                      "repeat_groups_band": st["groups_band"], "repeat_extra_band": st["extra_band"],
                      "repeat_extra_band_echo": st["echo_band"]},
            "cues": recs,
            **({"fills": fills} if fills else {}),
            "display": DISPLAY,
            **({"overlay": overlay_doc(items, ov_stats, evs, textbox, name_ink)} if items is not None else {})}



def main() -> int:
    # --help 只放用法；实现上的判断在模块文档串里，给读代码的人（发布前清理，Opus 审查 O6）
    ap = argparse.ArgumentParser(description=CLI_DESCRIPTION,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ref", help="gamescript.py 的剧本 JSON")
    ap.add_argument("--subs", required=True, help="主轨：SRT 或 `*-tracks.json`（取 provenance 的 main_srt）")
    # nonoise 是 2026-09-20 翻的默认，依据两条：①owner 09-17 定的一般形态是『保留全部非噪音文本 + 聚类 + 排序』，
    # 匹配器只是特化的聚类规则、它前面不该有有损过滤；②实测 hsr 整场覆盖 78.0% -> 89.2%，09-18 三段切片验收覆盖只增不减
    # （zzz-s1 +1、其余 ±0），摆帧 24 张确认全量喂入没有把 UI 涂满。它可能顺带解掉 --slot-reassign 的主要代价（推断、没实测）：
    # 主轨口径下丢的那批行，nonoise 的输入里 f2 64/66、f4 37/37 种文本还在——输入端的计数，不是命中（docs/architecture/defaults.md 1.4）
    ap.add_argument("--feed", default="nonoise", choices=FEEDS,
                    help="喂给匹配器的 cue 流：nonoise（默认，除 noise 的全部区域轨）/ "
                         "main（只喂主轨，回退用）/ all（连 noise）。nonoise / all 要 --subs 给 *-tracks.json。"
                         "注意用途 2 的尺（`script_align` / `game_align`）量的仍是主轨 SRT，和产物喂入的口径不同")
    ap.add_argument("--feed-panel", default="keep", choices=("keep", "drop", "strict"),
                    help="喂入时怎么处理 build_tracks 标了 `panel_like` 的 cue（对话记录面板那种："
                         "文本真、时间错）：keep（默认，现状）/ drop（整条不喂）/ "
                         "strict（喂，但只认强匹配，见 PANEL_MIN_SCORE）")
    ap.add_argument("--out", required=True, help="匹配结果 JSON（一条 cue 一条记录）")
    ap.add_argument("--srt", default=None,
                    help="顺便导出一份 SRT（同 matched_srt 的默认：同一句的同时回显 / 记录面板回显不出；"
                         "要放回，拿 --out 的产物跑 flowocr-render --preset matched_srt --opt echo=keep）")
    ap.add_argument("--lang", choices=matchedio.LANGS, default="jp", help="导出的正文取哪种语言")
    ap.add_argument("--fuzzy", type=float, default=0.75, help="同 game_align")
    ap.add_argument("--merge-gap", type=float, default=MERGE_GAP,
                    help="相邻 cue 认的是同一条剧本行、间隔 ≤ 这么多秒就并成一条（0 = 不并，见 merge_runs）")
    ap.add_argument("--ass", default=None,
                    help="顺便导出用途 2 的叠加 ASS：把 --lang 的原文压在原字幕的框上（--subs 必须给 tracks.json）；"
                         "旁边另有一份可编辑的字幕稿（.ass 换成 .script.ass）")
    ap.add_argument("--preset", default="default",
                    help="--ass 用哪个预设（同 render --preset：内置名 / 模块名 / .py 路径）。内置叠加预设："
                         "default（默认，body / extra / name 三层、底板 α 0x20）、default_all、"
                         "dev（全部层、非主轨浅蓝、聚类编号、底板 50%%）。见 flowocr.output.presets._overlay")
    ap.add_argument("--opt", action="append", default=[], metavar="K=V",
                    help="给预设的选项，可多次：plate / plate_alpha / typewriter / fade / show / hide（同 render --opt）")
    ap.add_argument("--readables", action="store_true",
                    help="把阅读面板（书籍、信件、说明）的长文本也盖上：文本包里要有 readables 表，要 --obs（见 readable_items）")
    ap.add_argument("--obs", default=None, help="--readables 要逐帧的观测（run_ocr2 的 jsonl）")
    ap.add_argument("--layout", default=None,
                    help="叠加的版式文件（同匹配器的 --ctx layout=；none 是不用，A/B 的对照臂）；不给按剧本的游戏找随代码走的那份")
    ap.add_argument("--viewport", default=None, metavar="X,Y,W,H",
                    help="游戏画面在录像里的位置（像素，同匹配器的 --ctx viewport=）；不给用建轨 --viewport 记下的，都没有按版式文件推算")
    ap.add_argument("--no-overlay", action="store_true",
                    help="不判叠加层、`--out` 里不写 `overlay`（只要 SRT / 只要认领统计时用）。"
                         "默认判——给了 `-tracks.json` 就判，`--ass` 只是投影。原神的剧本仍会读 tracks（从名牌读玩家名填 `{NICKNAME}`）")
    a = ap.parse_args()
    if a.readables and not a.obs:
        raise SystemExit("--readables 要逐帧观测，同时给 --obs")
    if (a.ass or a.readables) and not a.subs.endswith("-tracks.json"):
        raise SystemExit("--ass / --readables 要框，--subs 必须给 *-tracks.json")
    if a.ass and a.no_overlay:
        raise SystemExit("--ass 投影的就是叠加层，不能同时给 --no-overlay")
    opts = {}
    for kv in a.opt:
        if "=" not in kv:
            ap.error(f"--opt 要 K=V：{kv}")
        k, v = kv.split("=", 1)
        opts[k] = v
    ass_preset = None
    if a.ass:
        # 匹配跑之前就把预设加载好、核它收不收 matched、出不出 ASS：跑完几分钟才发现预设不对不划算
        from flowocr import extensions
        ass_preset = extensions.load(a.preset, "preset")
        accepts = getattr(ass_preset, "ACCEPTS", None)
        if accepts is not None and matchedio.SCHEMA not in accepts:
            ap.error(f"--preset {a.preset} 吃的是 {list(accepts)}，不收匹配产物 {matchedio.SCHEMA}")
        if "ass" not in getattr(ass_preset, "OUTPUTS", ("ass",)):
            ap.error(f"--preset {a.preset} 只出 {list(ass_preset.OUTPUTS)}、不出 ASS，不能给 --ass（要 SRT 用 --srt，或阶段 3 跑 default_srt）")

    from flowocr.analyze.matchers import gametext      # 猜占位的值是这款匹配器特化的（本文件是它的 CLI）
    ref = GA.load_ref(Path(a.ref))
    res = run_match(ref, a.subs, a.feed, a.feed_panel, a.fuzzy, a.merge_gap,
                    overlay=not a.no_overlay, readables=a.readables, obs=a.obs,
                    events=gametext.needs_events(ref), layout=a.layout, viewport=a.viewport)
    recs, items, ov_stats = res["recs"], res["items"], res["ov_stats"]
    st = match_stats(ref, recs, res["suspect_raw"])
    report(st, recs, res["n_raw"], a.merge_gap, a.fuzzy, ov_stats, a.readables)
    doc = matched_doc(ref, recs, res["n_raw"], st, a.ref, res["subs_path"], res["tp"], sys.argv[1:],
                      {"fuzzy": a.fuzzy, "merge_gap": a.merge_gap, "feed": a.feed, "feed_panel": a.feed_panel, "readables": a.readables},
                      items, ov_stats, res["evs"], res["textbox"], gametext.fills(ref, recs, res["evs"]), res["name_ink"])
    p = Path(a.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"-> {p}")
    # SRT / ASS 都是**阶段 3 的投影**，走内置预设那一份实现（2026-09-22 Codex 审计 P2：
    # 同一份产物换预设不该重跑匹配，于是投影只能有一处；这里只是把文件名交给它）
    from flowocr.output.presets import matched_srt as _srt
    if a.srt:
        out = Path(a.srt)
        for w in _srt.render(doc, out.parent, {"lang": a.lang, "name": out.name}):
            print(f"-> {w}（{srtio.count_cues(w)} 条）")   # 条数从写出的文件数（srtio 的唯一口径），不拿 len(recs) 代
    if a.ass:
        out = Path(a.ass)
        ass_preset.render(
            doc, out.parent, {**opts, "lang": a.lang, "tracks": str(res["subs_path"]), "name": out.name})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
