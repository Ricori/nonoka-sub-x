"""拿 `flowocr.analyze.gamescript` 圈出来的剧本量一条轨：命中 / 三档漏条 / 重复认领 / 行首行尾缺字。

分桶与三档复用 `script_align.gap_report` 和 `evalkit.triviality`，**不重写第二份**。
和《魔裁》那套差三处，都是游戏直播的形态逼出来的：

1. **一条 cue 有几种读法**。原神 / 星铁的主轨 cue 是 `名牌 \\n 称号 \\n 正文…`
   粘在一起的（gamestream-first-look 报告），剧本行只有正文；
   末尾还常挂一个被吸进来的键位提示（`D`）。所以每条 cue 试"去掉开头 0–3 行 ×
   去不去掉最后一行"几种读法，取和剧本最像的那个。不靠名字表——库里原神的说话人
   只覆盖 4% 的节点，而且屏幕上的名牌和库里的 `speaker` 字段不是一回事。
2. **对齐按时间圈候选，不走游标**（`run` 的说明）。剧本行带 gamescript 从 obs 锚来的时刻，
   各臂共用；时间只圈范围，命中仍看文本。主认领之后还有**第二遍**（`claim_contained`）：
   一条 cue 常常装着两行剧本（选项按钮 + 正文、并带把两个说话人并成一条），整行落在 cue 里
   就算认领，但**不许和主认领占同一段字**——否则库里近似重复的另一行会被白送一个命中。
3. **分母里排掉 `gamescript.NON_BAND_KINDS` 的类型和有处境（`gamescript.STATUSES`）的行**（选项 / 黑屏 / 没走的分支 / 单元外 / 重演拷贝 /
   读到过但不在字幕带，判据见那边）。排掉的条目仍参与对齐、只是不计分母；它们若被主轨命中会单独报出来——
   那说明分支判错了，或者选项文本真的进了这条轨。重演拷贝例外：它连序列都不进。
   绝区零的变体组一组只算一个分母条目，任一种说法被认领都记在代表行上（`score`）。
4. **长 gap 照样算疑似真漏**（`gap_report(long_is_branch=False)`，audit-6）：《魔裁》那边
   长 gap 被解释成"分支没走到"，而这里分支和没播的一截已经排出分母，长 gap 没有分支解释。
   于是疑似真漏 = 全部未命中 = 分母 − 命中，逐档不经过 gap 分桶，分母各臂相同，三档可以跨臂比。

**这里量的是原始轨，不过匹配器。** 所以"对不上剧本"那一档是**幽灵条目的上限**，
而且比《魔裁》那边更松：库收什么因游戏而异（原神只有任务对话；星铁含全量台词；
绝区零含散条目），库外的文字——UI、路人、主播自己压的字幕——都落在这一档。
**重复认领**这里是原始轨口径：一条剧本行被几条 cue 认领，多出来的次数之和
（hit_delta 那个是匹配器输出口径，两者不可混比）。

用法：见 `CLI_DESCRIPTION`。
"""

from __future__ import annotations

import argparse
import copy
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from flowocr import textfold  # noqa: E402
from flowocr.artifacts import evalkit  # noqa: E402
from flowocr.analyze import gamescript as GS  # noqa: E402
from flowocr.analyze import script_align as SA  # noqa: E402
from flowocr.artifacts import srtio  # noqa: E402

CLI_DESCRIPTION = """拿 gamescript 圈出来的剧本量一条轨（用途 2 的尺）：命中 / 三档疑似真漏 / 重复认领 / 行首行尾缺字。

分母排掉选项、黑屏旁白、没走到的分支、没播的那截、重演拷贝和不在字幕带的行；量的是原始轨、不过匹配器，
所以"对不上剧本"那一档是幽灵条目的上限（UI、路人、主播自己的字幕都落在这里）。

    python -m flowocr.analyze.game_align out/gametext/gi-s2-ref.json --subs out/gs-gi-s2/gi-s2-main.srt
    # 两条臂比命中集合（按归一化文本的多重集）：
    python -m flowocr.analyze.game_align <ref.json> --subs <A 主轨> --vs <B 主轨>"""


@dataclass
class Ref:
    doc: dict
    script: list[SA.Entry]
    """对齐序列（gamescript 排好的 `sequence`：按 obs 时间排到行一级，重演拷贝不在里面）。"""
    windows: list[list]
    """每行可以被认领的时段——理由见 gamescript.sequence。"""
    role: dict[str, str]
    variant_of: dict[str, str]
    """绝区零变体行 -> 它那组的代表行（gamescript `fold_variants`）。一组只算一个分母条目。"""
    rows: dict[str, tuple[str, int]] = field(default_factory=dict)
    """剧本行 key -> (单元, 单元内行序)：行序是这一行在单元 `lines` 里的下标（重演拷贝占着位置、不进表）。夹缝认领（`claim_flanked`）看相邻行用；
    空表时那一遍不跑。"""


def load_ref(p: Path) -> Ref:
    doc = json.loads(p.read_text(encoding="utf-8"))
    if doc.get("schema") != GS.SCHEMA:
        raise SystemExit(f"{p} 的 schema 不是 {GS.SCHEMA}：{doc.get('schema')!r}（重跑 gamescript）")
    by_key, role, variant_of, rows = {}, {}, {}, {}
    for u in doc["units"]:
        for li, ln in enumerate(u["lines"]):
            if ln["status"] == "Duplicate":     # 同一个节点的第二份 key 相同，别覆盖原件
                continue
            rows[ln["key"]] = (u["uid"], li)
            by_key[ln["key"]] = SA.Entry(key=ln["key"], kind=ln["kind"], cmd="", text=ln["text"],
                                         src=u["uid"], status=ln["status"])
            role[ln["key"]] = ln["role"]
            if ln.get("variant_of"):
                variant_of[ln["key"]] = ln["variant_of"]
    return Ref(doc=doc, script=[by_key[k] for k, _, _ in doc["sequence"]],
               windows=[w for _, _, w in doc["sequence"]], role=role, variant_of=variant_of, rows=rows)


def fresh_script(ref: Ref) -> list[SA.Entry]:
    """对齐序列的一份干净拷贝（`matched` / `sim` 清零），给要自己跑一遍认领的地方用；处境（`status`）一起抄——
    漏了它，有处境的行会悄悄变成带内、进分母和认领。"""
    return [SA.Entry(key=e.key, kind=e.kind, cmd="", text=e.text, src=e.src, status=e.status) for e in ref.script]


OBS_READ, OBS_COMMON, OBS_NONE, OBS_SHORT = "obs 读到过", "只撞常用句", "obs 没读到", "太短判不了"
OBS_SIDES = (OBS_READ, OBS_COMMON, OBS_NONE, OBS_SHORT)


def obs_side(doc: dict) -> dict[str, str]:
    """每行剧本在 obs 里读到过没有（gamescript 记的锚点，任何区域、任何一帧）。

    这是**这一处**的判据，不是全局搜：锚点是 obs 里某个框的文字落在**这一行**里
    （同一单元内只锚最像的那行），而单元本身是按时间圈出来的。内容不到
    `qmin` 字的行从来不拿去检索，没有锚点不说明什么，单列。
    * 读到过而主轨没命中 = 丢在**轨这一层**（没进主轨 / cue 读法对不上）；
    * 只撞常用句 = 锚住它的查询**全都**撞了 >`max_units` 个单元（`言ってみるといい。` 这种到处都有的
      句子，别处的同句也会锚上来）——算不得"这一处读到过"，单列（audit-6）；
    * 没读到 = 丢在 **OCR 之前**（没出框、被遮挡、显示太短没采到……分不开，要摆帧）。"""
    qmin = doc["provenance"]["params"]["qmin"]
    out = {}
    for u in doc["units"]:
        for ln in u["lines"]:
            if ln["status"] == "Duplicate":     # 同 load_ref：同一节点的第二份别覆盖原件
                continue
            a = ln["anchor"]
            out[ln["key"]] = (OBS_READ if a and a["unique"] else OBS_COMMON if a else
                              OBS_NONE if len(GS.cnorm(ln["text"])) >= qmin else OBS_SHORT)
    return out


def resolve(p: str) -> tuple[Path, dict]:
    """轨可以直接给 SRT，也可以给 `*-tracks.json`——后者从 provenance 取 `main_srt`
    （**不许 glob 猜**，字典序事故见 docs/dev-guide/verification.md「追溯」），并带回产它的 `git_head` / `code_fp`。"""
    path = Path(p)
    if not path.name.endswith("-tracks.json"):
        return path, {}
    from flowocr.artifacts import tracksio
    prov = tracksio.load(path)["provenance"]
    if not prov.get("main_srt"):
        raise SystemExit(f"{path} 没有主轨（provenance.main_srt 为空）")
    return path.parent / prov["main_srt"], {k: prov.get(k) for k in ("git_head", "code_fp")}


def code_same(a: dict, b: dict) -> bool | None:
    """两臂的轨是不是同一份 build_tracks 产的：比**代码指纹**不比 HEAD（audit-6——HEAD 移动、
    代码没变时 git_head 会不同；反过来 `+dirty` 相同也证明不了代码相同）。没指纹的算不知道。"""
    fa, fb = (a or {}).get("code_fp"), (b or {}).get("code_fp")
    return None if not (fa and fb) else fa == fb


def read_cues(p: Path) -> list[tuple[float, str, str]]:
    """(起点秒, 各行用换行拼起来的原文, 文件名)。**保留行结构**，读法要按行切。"""
    return [(c.start, "\n".join(c.lines), p.name) for c in srtio.read_subs([p])]


def read_ends(p: Path) -> list[float]:
    """和 `read_cues(p)` 一条对一条的终点秒（`run` / `score` 的 `ends`）。"""
    return [c.end for c in srtio.read_subs([p])]


HEAD_MIN_BODIES = 3
"""一行在整条轨里作为**非末行**、配过至少这么多种不同的末行，就是"开头行"（`head_lines`）。"""


def band_lines(script: list[SA.Entry]) -> frozenset[str]:
    """该在字幕带的剧本行（`band`）的 `SA.norm`：`head_lines` 的 `script_lines` 用它，几个调用点（匹配、叠加、疑似漏字幕判断器）同一份。"""
    return frozenset(SA.norm(e.text) for e in script if band(e))


def head_lines(subs, script_lines: frozenset[str] = frozenset()) -> frozenset[str]:
    """整条轨里的**开头行**（归一化后）：名牌、称号、常驻在正文上面的角标。

    判据只看这条轨自己：一行作为非末行出现、而且配过 ≥ `HEAD_MIN_BODIES` 种不同的末行（正文）。
    名牌天然如此——一个说话人要说很多句（gi1 `カチーナ` 配过 271 种正文，zzz `シーシイア` 217 种）；
    真台词作为非末行出现时，下面跟的几乎总是同一句的续行。不靠名字表（库里原神的说话人只覆盖 4% 的节点）。

    `script_lines`（该在字幕带的剧本行，`band_lines`）：**本身逐字就是一整条台词的不算开头行**。全量喂入时一句台词会和
    别的区域的字同刻并进同一条 cue、落在非末行上，配上十几种"正文"——hsr 整场「あなたが彼を食い止めたのですね。」
    这样被当成名牌剥掉，匹配前就没了，cue 和剧本逐字相同却记相似度 0（2026-10-05，匹配后漏行逐条追查）。
    `norm` 留着标点，所以喊名字的台词 `カチーナ！` 挡不住名牌 `カチーナ`。只看带内的行：文本包里的 UI 文案（绝区零
    `SKILL INFO`、`PM New Eridu`）常驻在变化的正文上面，正是开头行的形状——放进来，zzz 整场它们就被逐条认领，重复认领 232 → 265。

    为什么需要（2026-09-12，matcher 计划那 12 条"只由 `extra` 认领"的行逐条看出来的）：
    ①`カチーナ / はあ、はあ` 这种**正文很短**的 cue，去掉末行的那种读法只剩名牌，
    和剧本里恰好存在的喊名字的台词 `カチーナ！` 相似度 0.89，比短正文本身还高，于是主认领被名牌抢走、
    输出成"卡齐娜！"——认错，不只是丢；②第二遍包含认领把名牌 `シーシイア` 整行认成 `シーシィア…？`。"""
    from collections import defaultdict
    bodies: dict[str, set[str]] = defaultdict(set)
    span: dict[str, list[float]] = {}
    for t, raw, _ in subs:
        ls = [x for x in raw.split("\n") if x.strip()]
        for x in ls[:-1]:
            k = SA.norm(x)
            if k and len(k) <= HEAD_MAX_CHARS and k not in script_lines:
                bodies[k].add(SA.norm(ls[-1]))
                lo, hi = span.get(k, (t, t))
                span[k] = [min(lo, t), max(hi, t)]
    return frozenset(k for k, v in bodies.items()
                     if span[k][1] - span[k][0] >= HEAD_MIN_SPAN
                     and distinct_bodies(v, HEAD_MIN_BODIES) >= HEAD_MIN_BODIES)


HEAD_MAX_CHARS = 16
"""开头行最多这么多字（归一化后）。名牌、称号都短（最长见过 `「プラックウルフ」ロームル` 12 字）；
两行正文的第一行几乎总比这长——长度门是 `distinct_bodies` 之外的第二道保险。"""

HEAD_MIN_SPAN = 30.0
"""开头行在轨里首末两次出现至少隔这么多秒。**第三道保险**：名牌跟着说话人出现在一场又一场对话里；
正文的第一行只在它自己那几秒里出现。zzz-s2 `待った待った！刀を納めろ！ / ちゃんと言うから！` 这条
（11 字、第二行被打字机和 OCR 抖出几种互不相似的末行）前两道都拦不住，被当成开头行剥掉、命中 −1。"""


def distinct_bodies(bodies: set[str], enough: int) -> int:
    """数"真正不同"的末行有几种：**互为前缀、或相似度 ≥ 0.6 的算同一种**，数到 `enough` 就停。

    不这么数的话，两行正文的**第一行**会被当成开头行：打字机把第二行打成 `一緒に` / `一緒にテレ` /
    `一緒にテレビでも…` 好几种半截、OCR 再抖几个字，它就"配过 ≥3 种末行"了——剥掉之后整条 cue 对不上
    （2026-09-12 第一版剥开头行时实测：gi-s1 命中 30 → 23、zzz-s1 46 → 38，逐条看都是这个形状）。

    代表是贪心挑的，结果依赖遍历顺序，所以**等长的按字符串排**：只按长度排时等长的几条按 set 的迭代顺序走，
    而 str 的哈希每个进程随机加盐——同一份轨两次跑出的开头行集合不同（2026-09-26 gi-s1 名牌层 127 / 132 / 133 块）。"""
    reps: list[str] = []
    for b in sorted(bodies, key=lambda s: (-len(s), s)):
        if any(r.startswith(b) or SequenceMatcher(None, b, r, autojunk=False).ratio() >= 0.6 for r in reps):
            continue
        reps.append(b)
        if len(reps) >= enough:
            break
    return len(reps)


INLINE_NAME = re.compile(r"^\s*([^:：]{1,16}?)\s*[:：]\s*(\S.*)$")
"""过场字幕的"名字: 台词"同一行格式（gi1 `カチーナ: ハッ!`、`パイモン: 気持ちいいな。…`）。"""


def strip_heads(raw: str, heads: frozenset[str]) -> tuple[list[str], bool]:
    """剥掉 cue 里的开头行，返回 (剩下的行, 剥掉过没有)。

    * 非末行里的开头行整行剥掉；末行是正文，不剥（`パイモン / カチーナ！` 的末行碰巧和名牌同字）；
    * **只剩一行、而它就是开头行**时整条不留：打字机起步那半秒只有名牌在屏上；
    * 任一行是 `开头行: 台词` 的同一行格式，剥掉前缀。"""
    ls = [x for x in raw.split("\n") if x.strip()]
    if not heads or not ls:
        return ls, False
    out, hit = [], False
    for k, x in enumerate(ls):
        m = INLINE_NAME.match(x)
        if m and SA.norm(m.group(1)) in heads:
            x, hit = m.group(2), True
        elif SA.norm(x) in heads and (k < len(ls) - 1 or len(ls) == 1):
            hit = True
            continue
        out.append(x)
    return out, hit


def variants(raw: str, heads: frozenset[str] = frozenset()) -> list[str]:
    """去掉开头 0–3 行（名牌 / 称号 / 偶尔吸进来的一个角标）× 去不去掉最后一行（键位提示）。

    **开头行（`head_lines`）先剥掉，不进任何读法**（`strip_heads`）：留着它，名牌就会和剧本里喊名字的台词
    配上——去掉末行只剩名牌（gi1 `カチーナ / はあ、はあ` → `カチーナ！`），或者名牌拼上打字机打出的半截
    （gi2 `ナヴィア / (…すご` → `ナヴィア…`，0.82）。两例都是 2026-09-12 摆帧抓到的。
    剥过名牌、末行又是有内容的正文（≥3 字）时，去掉末行后只剩 1–2 字碎片的读法也不要
    （gi1 `カチーナ / L / はあ、はあ` 里夹的 OCR 垃圾行 `L`）；没有名牌的 cue 不受这条影响。"""
    ls, stripped = strip_heads(raw, heads)
    content = evalkit.CLASSES[2]
    body_last = stripped and bool(ls) and evalkit.triviality(ls[-1]) == content
    out = []
    for i in range(min(4, len(ls))):
        for j in {len(ls), len(ls) - 1}:
            if j <= i or (j < len(ls) and body_last
                          and all(evalkit.triviality(x) != content for x in ls[i:j])):
                continue
            v = SA.norm(" ".join(ls[i:j]))
            if v and v not in out:
                out.append(v)
    return out


def band(e: SA.Entry) -> bool:
    return GS.in_band(e.kind, e.status)


ROLE_ROWS = 16
"""『按上游角色拆』那张表最多列几行（进分母的类总是全列）。"""

BIN = 10.0
"""按时段找候选时的分桶宽度（秒）。只影响速度，不影响结果。"""

CONTAIN_MIN, CONTAIN_MIN_CHARS = 0.85, 3
"""第二遍认领（`claim_contained`）：剧本行有这么多内容字**整行**落在 cue 剩下的字里才算被认领，
且这一行的内容不少于这么多字（1–2 字没有判别力，同 owner 的三档）。包含度的门和
`gamescript` 检索那道一样。"""


def rest_of(v: str, used: str) -> str:
    """cue 的一种读法 `v` 里，挖掉和 `used`（主认领那一行）对上的字之后剩下的。"""
    if not used:
        return v
    keep = [True] * len(v)
    for b in SequenceMatcher(None, v, used, autojunk=False).get_matching_blocks():
        for k in range(b.a, b.a + b.size):
            keep[k] = False
    return "".join(c for c, k in zip(v, keep) if k)


def claim_contained(script: list[SA.Entry], subs, cand, sub_hit: list[int],
                    heads: frozenset[str] = frozenset()) -> list[list[int]]:
    """第二遍：一条 cue 认领**整行落在它里面**的其他剧本行；返回每条 cue 多认领了哪些行。

    为什么需要（2026-09-11 数的）：主认领是一对一的，而屏幕上一条 cue 常常装着两行剧本——
    绝区零的选项按钮和正文同屏（`また後で来るね` + 对白）、星铁并带把两个说话人并进一条 cue；
    还有一类是 cue 里混进了 OCR 垃圾行（`ユーちゃん / キュン / 1`），几种读法都够不着模糊阈值，
    但那一行的字**整行都在**。这两种主轨都确实读到了，算漏是错的。

    **不许和主认领占同一段字**（`rest_of`）：不加这条，库里近似重复的另一行会被白送一个命中——
    绝区零两位主角的两种说法、原神的重演拷贝、星铁改过字的同一句，都只上屏了一种
    （实测：不挖掉主认领的字是 +57/+14/+9/+10，挖掉之后 +15/+7/+4/+8，差的那些逐条看全是这类）。
    **认领了一行就把它的字也挖掉**：同一条 cue 里认两行时，第二行不许再用第一行的字——
    否则库里同文本、不同编号的两行会被同一段字各认一次（zzz `いやああぁぁ————！`，2026-09-12）。
    **开头行（名牌）先剥掉再找**（`head_lines`）：名牌整行落在 cue 里，不等于有人喊了这个名字。"""
    out: list[list[int]] = [[] for _ in subs]
    cn = [GS.cnorm(e.text) for e in script]
    for si, (t, raw, _) in enumerate(subs):
        vs = [GS.cnorm(v) for v in variants(raw, heads)]
        if not vs:
            continue
        used = cn[sub_hit[si]] if sub_hit[si] >= 0 else ""
        rests = [rest_of(v, used) for v in vs]
        for i in cand(t):
            if script[i].matched >= 0 or len(cn[i]) < CONTAIN_MIN_CHARS:
                continue
            if max(GS.contained(cn[i], r) for r in rests) >= CONTAIN_MIN:
                script[i].matched = si
                out[si].append(i)
                rests = [rest_of(r, cn[i]) for r in rests]
    return out


SUPERSET_SIM = 0.9
"""`superset_hit`：以逐字相等那行开头的更长台词，对 cue 更长的读法要像到这个程度才改认它。"""


def superset_hit(hit: int, vs: list[str], cnd: list[int], sn: list[str], cn: list[str], sm: SequenceMatcher) -> int:
    """逐字相等的那行（`hit`）只对上了 cue 的**一部分**读法（去掉了末行 / 开头几行）、而窗里另一条**以它开头**的更长台词
    对更长的读法像到 ≥ `SUPERSET_SIM` 时，返回那条更长的（多条取最像的）；否则 -1。

    为什么（2026-10-05，匹配后漏行逐条追查）：zzz 0:18:20 屏上是一条两行台词「首席技術者？\\Nってことは、なんかを研究してる人なの？」，
    剧本里另有一条单独的「首席技術者？」——去掉末行的读法和它逐字相等，精确优先就认了短的，长的那条对整条 cue 0.96 却没人认
    （第二遍包含认领也认不上：主认领用掉了「首席技術者」那几个字）。只看比逐字相等那种读法**更长**的读法：
    单行 cue「ありがとう」不会改认「ありがとう！」。
    要求长台词**以**短行开头、不只是包含（owner 10-06）：只要求包含时 gi1 主轨口径上出过一次巧合——短行归一后只剩「たち」，
    恰好是一条长台词中间的子串。"""
    base = len(sn[hit])
    longer = [v for v in vs if len(v) > base]
    if not longer or not cn[hit]:
        return -1
    best, best_s = -1, SUPERSET_SIM
    for i in cnd:
        if i == hit or len(cn[i]) <= len(cn[hit]) or not cn[i].startswith(cn[hit]):
            continue
        sm.set_seq2(sn[i])
        for v in longer:
            sm.set_seq1(v)
            if sm.quick_ratio() >= best_s and (r := sm.ratio()) >= best_s:
                best, best_s = i, r
    return best


PREFIX_MIN, PREFIX_SIM, PREFIX_MAX_SPAN = 8, 0.9, 5.0
"""`claim_prefix`：读法至少这么多内容字；和剧本行同长的开头像到这个程度；同样的字在轨里**在屏**的首末（最早的起点到最晚的终点）不超过这么多秒。"""


def claim_prefix(script: list[SA.Entry], subs, cand, sub_hit: list[int], sub_extra: list[list[int]],
                 heads: frozenset[str], cn: list[str], ends: list[float] | None = None) -> int:
    """第三遍，**打字机半截**：前两遍都没认上任何行的 cue，某种保留末行的读法（内容归一后 ≥ `PREFIX_MIN` 字）
    是窗里**恰好一条**带内台词的开头（和那行同长的开头相似 ≥ `PREFIX_SIM`），而那行**到这时还没人认领**，就认它；
    同一行有几条这样的 cue 时给读到字最多的那条。就地改 `sub_hit` / `Entry.matched`，返回认了几行。

    为什么（2026-10-05，匹配后漏行逐条追查 + 摆帧）：gi2 2:15 前后玩家连点跳过对白，整句只在屏上停了不到半秒，2 fps 的采样只采到打字中途，
    「もし仕事の日の天気が毎日こんなに」对整句「…キュートだったら、最高なのに。」只有 0.68、「観察よ。スチームバード新聞に助」对 75 字的整句 0.34。
    第一版放在主认领里、对所有行开放，gi2 / zzz 新出的可疑改写一多半是认错的：任务追踪里的地名「エピクレシス歌劇場」、
    「メロピデ要塞の原始胎海噴出」恰好是某句台词的开头，论坛帖子列表的预览行是帖子正文的开头。所以收紧成三条：
    ①只认**别处都没认上**的带内台词（只补漏，不和正常认领抢）；②读法要**保留 cue 的末行**（打字中的是最后一行）；
    ③同样的字在整条轨里**在屏**的首末（最早的起点到最晚的终点，`ends` 给 cue 的终点）不超过 `PREFIX_MAX_SPAN`——打字中途的字一闪就变，常驻的地名、任务名一挂几十秒。
    ⚠ 只按各条 cue 的起点量（10-05 第一版）的话，一条挂 20 s 的常驻字只有一个起点、跨度是 0，照样放行：
    gi-s1 531 s「ガイドノート」菜单里静止的列表项「クルースニク晶鉱」就这样认成了一句台词（10-06 验收 peer 摆帧）。没给 `ends` 时退回只看起点。
    相似度照旧按整行记（`sim_of`），所以这类认领会进"可疑改写"——改写幅度确实大。"""
    def tails(raw: str) -> list[str]:
        ls, _ = strip_heads(raw, heads)
        return list(dict.fromkeys(c for c in (GS.cnorm(" ".join(ls[i:])) for i in range(min(4, len(ls))))
                                  if len(c) >= PREFIX_MIN))
    seen: dict[str, list[float]] = {}
    tl = []
    for si, (t, raw, _) in enumerate(subs):
        tl.append(tails(raw))
        te = max(t, ends[si]) if ends is not None else t
        for c in tl[-1]:
            lo, hi = seen.get(c, (t, te))
            seen[c] = [min(lo, t), max(hi, te)]
    sm = SequenceMatcher(None, autojunk=False)
    best: dict[int, tuple[int, int]] = {}                 # 行 -> (读到的字数, cue)
    for si, (t, _, _) in enumerate(subs):
        if sub_hit[si] >= 0 or sub_extra[si]:
            continue
        cs = [c for c in tl[si] if seen[c][1] - seen[c][0] <= PREFIX_MAX_SPAN]
        got: dict[int, int] = {}
        for c in cs:
            sm.set_seq1(c)
            for i in cand(t):
                if not band(script[i]) or len(cn[i]) <= len(c):
                    continue
                sm.set_seq2(cn[i][:len(c)])
                if sm.quick_ratio() >= PREFIX_SIM and sm.ratio() >= PREFIX_SIM:
                    got[i] = max(got.get(i, 0), len(c))
        if len(got) == 1:
            (i, n), = got.items()
            if script[i].matched < 0 and n > best.get(i, (0, -1))[0]:
                best[i] = (n, si)
    for i, (_, si) in best.items():
        sub_hit[si] = i
        script[i].matched = si
    return len(best)


INFIX_MIN, INFIX_IN, INFIX_COVER, INFIX_MAX_SPAN = 12, 0.9, 0.5, 30.0
"""`claim_infix`：读法至少这么多内容字（`cnorm` 之后）；读法的字按顺序落在剧本行里的比例；读法占剧本行的比例；同样的字在轨里在屏的首末不超过这么多秒。"""


def claim_infix(script: list[SA.Entry], subs, cand, sub_hit: list[int], sub_extra: list[list[int]],
                heads: frozenset[str], cn: list[str], ends: list[float] | None = None, main: list[bool] | None = None) -> int:
    """第四遍，**读到的是剧本行中间或后半的一大段**：前三遍都没认上任何行、在主轨上的 cue，某种保留末行的读法（内容归一后 ≥ `INFIX_MIN` 字）
    有 ≥ `INFIX_IN` 的字按顺序落在窗里**恰好一条**带内台词里、占那一行 ≥ `INFIX_COVER`，而那行**到这时还没人认领**，就认它；
    同一行有几条这样的 cue 时给读到字最多的那条。就地改 `sub_hit` / `Entry.matched`，返回认了几行。

    为什么（检测器三场修复计划 U5，owner 2026-10-10）：游戏版本和文本包不同的句子——gi1 0:52:27 屏上是「うん、何頭かいる中でも、トトは一番親切で優しい子なんだよ。」，
    文本包那一行前面多一句「ううん、この子たちは「クビナガライノ」っていうの。」——整行相似 0.74 差在 `fuzzy` 门下，整行包含方向反了，打字机半截要读法是开头，
    三遍都认不上，6 s 里只有名牌、没有正文。门和打字机半截那一遍同构（只补漏、恰好一条、保留末行），数"恰好一条"时已被认领的带内行也算进去，
    同一句重显的读数不会去认另一条没人认的行。挡常驻文字的门比打字机半截松（owner 10-10）：显完的整句要挂好几秒，在屏首末 ≤ `INFIX_MAX_SPAN`，
    再加 cue 在主轨上（`main`，按 cue 的来源，和回显判定同一份；没给的——`game_align` 命令行、`--feed main`——当在主轨上）。
    读法是那一行**开头**的（和同长的开头像到 `PREFIX_SIM`）不归这一遍：那是打字机半截的形状，它的在屏门更严（菜单里挂 20 s 的列表项恰好是某句台词的开头，gi-s1 531 s）；
    那一行照样算"恰好一条"的候选，一个读法既是 A 的开头又落在 B 里时不认（挡歧义）。"""
    def tails(raw: str) -> list[str]:
        ls, _ = strip_heads(raw, heads)
        return list(dict.fromkeys(c for c in (GS.cnorm(" ".join(ls[i:])) for i in range(min(4, len(ls))))
                                  if len(c) >= INFIX_MIN))
    seen: dict[str, list[float]] = {}
    tl = []
    for si, (t, raw, _) in enumerate(subs):
        tl.append(tails(raw))
        te = max(t, ends[si]) if ends is not None else t
        for c in tl[-1]:
            lo, hi = seen.get(c, (t, te))
            seen[c] = [min(lo, t), max(hi, te)]
    sm = SequenceMatcher(None, autojunk=False)
    best: dict[int, tuple[int, int]] = {}                 # 行 -> (读到的字数, cue)
    for si, (t, _, _) in enumerate(subs):
        if sub_hit[si] >= 0 or sub_extra[si] or (main is not None and not main[si]):
            continue
        cs = [c for c in tl[si] if seen[c][1] - seen[c][0] <= INFIX_MAX_SPAN]
        got: dict[int, int] = {}
        for c in cs:
            for i in cand(t):
                if not band(script[i]) or len(cn[i]) <= len(c) or len(c) < INFIX_COVER * len(cn[i]):
                    continue
                if GS.contained(c, cn[i]) < INFIX_IN:
                    continue
                sm.set_seqs(c, cn[i][:len(c)])
                # 开头的形状归打字机半截：照样算一个候选（挡歧义），只是这一遍不认它（-1）
                got[i] = -1 if sm.ratio() >= PREFIX_SIM or got.get(i) == -1 else max(got.get(i, 0), len(c))
        if len(got) == 1:
            (i, n), = got.items()
            if n > 0 and script[i].matched < 0 and n > best.get(i, (0, -1))[0]:
                best[i] = (n, si)
    for i, (_, si) in best.items():
        sub_hit[si] = i
        script[i].matched = si
    return len(best)


FLANK_SHORT = 2
"""`claim_flanked` 的文字票：读法和剧本行短的那一方不超过这么多个内容字时只许少读、不许读错（0 处差），否则许错 1 处。同名牌投票（`scriptmatch.fuzzy_names`）的口径。"""


def flank_fits(r: str, c: str) -> bool:
    """夹缝认领的文字票：读法 `r` 是剧本行 `c`（两边都已 `GS.cnorm`）的一截（`textfold.fits_within`），
    错字不超过门（短的一方 ≤ `FLANK_SHORT` 字 0 处、否则 1 处），且占这一行过半。"""
    return 2 * len(r) > len(c) and textfold.fits_within(r, c, 0 if min(len(r), len(c)) <= FLANK_SHORT else 1)


def claim_flanked(script: list[SA.Entry], subs, cand, sub_hit: list[int], sub_extra: list[list[int]],
                  heads: frozenset[str], cn: list[str], rows: dict[str, tuple[str, int]],
                  variant_of: dict[str, str], step: float = 0.0) -> list[int]:
    """第五遍，**夹缝认领**：前四遍都没认上任何行的 cue X，夹在同一单元**相邻两行**的两次认领之间，就认中间那一行。
    就地改 `sub_hit` / `Entry.matched`，返回认上的 cue 下标（升序）。四条都要成立（shortline-context 计划的判据）：

    1. 单元里行 m 的上一行有一次认领起点 ≤ X 的起点、下一行有一次认领起点 ≥ X 的起点（同刻算两边：同屏的选项按钮）；
       "同刻"容不到一个采样间隔（`step`，秒）的差：和 X 的起点差不到一个间隔的认领两边都算。同一个菜单的按钮在采样级上起点相等，
       回抠到帧级后会差两三帧（hsr2 三选一菜单 48.90 / 48.90 / 48.94 s），中间那个按钮的邻行可能早它几帧；`step` 是 0（不知道）时就是原来的比法；
       取最近的那两次，两次之间（开区间）这个单元**没有别的行**被认领。"认领"是前四遍的主认领和包含认领，本遍新认的不当证据（不连锁）；
    2. m 是带内行、还没人认（变体组按整组算）、X 的起点在 m 的可认领时段里（`cand(t)`）；
    3. X 的某种读法（`variants`，开头行先剥）和 m 相容（`flank_fits`：是 m 的一截、错字不超过门、占过半）；
    4. 一个 X 只认一行：同时对上两行就不认；同一行有几条 X 时认给最早的那条（同一次显示切成几条 cue 时不多出重复认领）。

    为什么（owner 2026-10-24 定方向：短句靠匹配器按上下文和惯性推测）：2–4 个内容字、句尾拖一串标点的行（`でも…`、`そう…`），
    OCR 常读不出句尾，NFKC 后 `…` 展成 `...`，相似度过不了 `--fuzzy`、整行包含又要 ≥ 3 字，前四遍都够不着。
    相似度里去句尾标点分不开单独上屏的人名和"人名…？"这种台词（defaults.md 3.1）；这里靠**单元内的行序**挡：
    地图标记、记录面板说话人栏上的人名，要恰好落在"人名…？"上下两行的认领之间才会被认。"""
    from collections import defaultdict
    pos = {i: rows[e.key] for i, e in enumerate(script) if e.key in rows}
    rep = [variant_of.get(e.key, e.key) for e in script]
    taken = {rep[i] for i, e in enumerate(script) if e.matched >= 0}
    seen: dict[str, list[tuple[float, int]]] = defaultdict(list)     # 单元 -> [(认领起点, 行序)]，前四遍的
    for si, (t, _, _) in enumerate(subs):
        for i in ([sub_hit[si]] if sub_hit[si] >= 0 else []) + sub_extra[si]:
            if i in pos:
                seen[pos[i][0]].append((t, pos[i][1]))

    def flanked(u: str, li: int, t: float) -> bool:
        cl = seen.get(u, ())
        ta = max((x for x, r in cl if r == li - 1 and (x <= t or x < t + step)), default=None)
        tb = min((x for x, r in cl if r == li + 1 and (x >= t or x > t - step)), default=None)
        if ta is None or tb is None:
            return False
        lo, hi = min(ta, tb), max(ta, tb)               # 容差下两端可以倒序（下一行早 X 一两帧、上一行晚一两帧）
        return not any(lo < x < hi and r not in (li - 1, li + 1) for x, r in cl)
    best: dict[int, int] = {}                             # 行 -> 最早的 X
    for si, (t, raw, _) in enumerate(subs):
        if sub_hit[si] >= 0 or sub_extra[si]:
            continue
        rs = [r for r in dict.fromkeys(GS.cnorm(v) for v in variants(raw, heads)) if r]
        if not rs:
            continue
        hits = [i for i in cand(t)
                if i in pos and band(script[i]) and rep[i] not in taken
                and any(flank_fits(r, cn[i]) for r in rs) and flanked(*pos[i], t)]
        if len(hits) == 1 and (hits[0] not in best or (t, si) < (subs[best[hits[0]]][0], best[hits[0]])):
            best[hits[0]] = si
    for i, si in best.items():
        sub_hit[si] = i
        script[i].matched = si
    return sorted(best.values())


def run(script: list[SA.Entry], windows: list[list], subs, fuzzy: float, ends: list[float] | None = None,
        main: list[bool] | None = None, rows: dict[str, tuple[str, int]] | None = None,
        variant_of: dict[str, str] | None = None, step: float = 0.0) -> dict:
    """**按时间圈候选**的对齐：cue 在 t 秒，只和"t 落在它可认领时段里"的剧本行比
    （时段由 gamescript 从 obs 定，见 `gamescript.sequence`）；精确相等优先，否则取相似度
    最高的（≥ fuzzy）；平手时先给还没被认领的行、再按序列先后。
    返回和 `script_align.align` 同形的统计，并就地填 `Entry.matched`（第一条认领它的 cue）。

    为什么不用 `script_align.align` 的游标：那套在一条字幕带 + 有序剧本上是对的，
    在这里会被短句带跑——一条 `うん` 的精确匹配把游标甩到很远的后面，之后读得几乎一样的
    cue 也够不着（gi1 实测 `カチ一ナちゃんを助けて…` 在主轨里只差一个 `ー`，被记成漏）。
    这里的剧本行本来就带 obs 时段，用它圈范围更稳；**时间只圈范围，命中仍看文本**。
    主认领之后四遍补漏（包含、打字机半截、读到一大段、夹缝）；`rows`（`Ref.rows`）不给时夹缝那一遍不跑。"""
    from collections import defaultdict
    for e in script:
        e.matched, e.sim = -1, 0.0
    bins: dict[int, list[int]] = defaultdict(list)
    for i, ws in enumerate(windows):
        for lo, hi in ws:
            for b in range(int(lo // BIN), int(hi // BIN) + 1):
                bins[b].append(i)

    def cand(t: float) -> list[int]:
        return sorted({i for i in bins.get(int(t // BIN), ())
                       if any(lo <= t <= hi for lo, hi in windows[i])})
    sn = [SA.norm(e.text) for e in script]
    cn = [GS.cnorm(e.text) for e in script]
    sm = SequenceMatcher(None)
    sm2 = SequenceMatcher(None, autojunk=False)            # 改认更长的行用（`superset_hit`）
    heads = head_lines(subs, band_lines(script))
    n_exact = n_fuzzy = n_none = n_super = 0
    sub_hit: list[int] = []
    for si, (t, raw, _) in enumerate(subs):
        vs = variants(raw, heads)
        if not vs:
            sub_hit.append(-1)
            continue
        cnd = cand(t)
        exact = [i for i in cnd if sn[i] in vs]
        # 逐字相等的全是带外行（选项按钮）时，把**只差标点**的带内行也拉进来一起挑：OCR 读漏了省略号，
        # 台词 `「未着」……` 就只剩按钮 `「未着」` 逐字相等；主轨是字幕带，认成选项就把正文换成了按钮的译文
        # （hsr 2:10，2026-09-12 owner 看预览指出）。只在这种情况触发，别的打平照旧
        if exact and not any(band(script[i]) for i in exact):
            cvs = {GS.cnorm(v) for v in vs}
            exact += [i for i in cnd if i not in exact and band(script[i]) and GS.cnorm(script[i].text) in cvs]
        if exact:
            # 先给还没被认领的：同文本两行同时在窗口里时，打字机拆出的后半条 cue 会去认领
            # 第二行、把一条漏抹平。这和 hit_delta 的多重集口径一致（同文本行按条数算），接受；
            # 再先给该在字幕带的行（见上）
            hit = min(exact, key=lambda i: (script[i].matched >= 0, not band(script[i]), i))
            sup = superset_hit(hit, vs, cnd, sn, cn, sm2)
            if sup >= 0:                   # 记成模糊：认的那行和任何读法都不逐字相等
                hit, n_super, n_fuzzy = sup, n_super + 1, n_fuzzy + 1
            else:
                n_exact += 1
        else:
            # 剪枝不改结果：两个 quick 比值都是 ratio 的上界（同 script_align）。
            # 这里剪的是 `< best_s`，script_align 是 `<= best_s`——**别"统一"**：那边平手不换，
            # 这边平手要让给还没被认领的行（下面那个 `s == best_s` 分支），剪掉等分的就换不成了
            hit, best_s = -1, fuzzy
            for i in cnd:
                sm.set_seq2(sn[i])
                s = 0.0
                for v in vs:
                    sm.set_seq1(v)
                    if sm.real_quick_ratio() < best_s or sm.quick_ratio() < best_s:
                        continue
                    s = max(s, sm.ratio())
                if s > best_s or (s == best_s and hit >= 0
                                  and script[hit].matched >= 0 > script[i].matched):
                    hit, best_s = i, s
            if hit >= 0:
                n_fuzzy += 1
                script[hit].sim = round(best_s, 3)
            else:
                n_none += 1
        sub_hit.append(hit)
        if hit >= 0 and script[hit].matched < 0:
            script[hit].matched = si
    sub_extra = claim_contained(script, subs, cand, sub_hit, heads)
    n_contain = sum(1 for h, ex in zip(sub_hit, sub_extra) if h < 0 and ex)
    n_prefix = claim_prefix(script, subs, cand, sub_hit, sub_extra, heads, cn, ends)
    n_infix = claim_infix(script, subs, cand, sub_hit, sub_extra, heads, cn, ends, main)
    ctx = claim_flanked(script, subs, cand, sub_hit, sub_extra, heads, cn, rows, variant_of or {}, step) if rows else []
    n_more = n_prefix + n_infix + len(ctx)
    n_fuzzy, n_none = n_fuzzy + n_more, n_none - n_more
    return {"subs": len(subs), "exact": n_exact, "fuzzy": n_fuzzy, "contain": n_contain,
            "superset": n_super, "prefix": n_prefix, "infix": n_infix, "context": len(ctx),   # 这四种都已算在 fuzzy 里
            "context_subs": ctx, "unmatched_subs": n_none - n_contain, "jumps": 0,
            "sub_hit": sub_hit, "sub_extra": sub_extra, "heads": heads}


def score(ref: Ref, script: list[SA.Entry], subs, fuzzy: float, ends: list[float] | None = None,
          main: list[bool] | None = None, step: float = 0.0) -> dict:
    """`run` + 把变体组折成一个条目：组里任何一种说法被认领，就记在代表行上
    （`via`：代表行 key -> 实际被认领的那一行）；`sub_hit` 里指向变体的也改指代表行，
    免得同一句话的两种说法各被一条 cue 认领时，重复认领数不出来。
    `script` 可以是 `ref.script` 的深拷贝（两臂各量各的，不互相覆盖 `matched`）。"""
    st = run(script, ref.windows, subs, fuzzy, ends, main, ref.rows, ref.variant_of, step)
    idx = {e.key: i for i, e in enumerate(script)}
    via: dict[str, SA.Entry] = {}
    for e in script:
        rep = ref.variant_of.get(e.key)
        if rep and e.matched >= 0 and script[idx[rep]].matched < 0:
            script[idx[rep]].matched = e.matched
            via[rep] = e
    def rep_of(h):     # 变体被认领时记在代表行上，重复认领才数得出来
        return idx[ref.variant_of[script[h].key]] if script[h].key in ref.variant_of else h
    st["sub_hit"] = [rep_of(h) if h >= 0 else h for h in st["sub_hit"]]
    st["sub_extra"] = [[rep_of(i) for i in ex] for ex in st["sub_extra"]]
    st["via"] = via
    return st


def claims(st: dict) -> list[int]:
    """每一次认领指向的剧本行：主认领 + 第二遍的包含认领（`claim_contained`）。"""
    return [h for h in st["sub_hit"] if h >= 0] + [i for ex in st["sub_extra"] for i in ex]


def repeat_claims(sub_hit: list[int]) -> tuple[int, int]:
    """(被 ≥2 条 cue 认领的剧本行数, 多出来的认领次数之和)。原始轨口径，见模块说明。"""
    c = Counter(h for h in sub_hit if h >= 0)
    return sum(1 for v in c.values() if v > 1), sum(v - 1 for v in c.values())


def head_tail(script: list[SA.Entry], subs, via: dict | None = None,
              skip: set[int] | None = None,
              heads: frozenset[str] = frozenset()) -> list[tuple[int, int, int]]:
    """配上的每一对：(剧本内容字数, 行首缺几个内容字, 行尾缺几个内容字)。

    用内容归一（去标点、小书写假名并大写）比，免得省略号读没了被算成"缺字"——
    那是另一件事（script-corpus 报告文本质量那节）。一条剧本行只取第一条认领它的 cue；
    变体组由另一种说法命中的，拿**被认领的那种说法**比（`score` 的 `via`）。

    判据的洞：取第一个匹配块的起点当"行首缺字"——cue 的读法带着没剥干净的名牌时，第一块
    可能对上剧本中段，行首缺字会被高估。gi-s2 那 61.8% 摆帧证实是立绘遮挡，那次没出错。
    **2026-09-18 起 `heads` 传进来了**（审计 P2 的同一族：报数用的读法要和匹配用的一致），
    名牌不再留在读法里——这个数因此比以前小一点，和以前的读数不能直接比。"""
    out = []
    for k, e in enumerate(script):
        # 包含认领的行按定义整行都在 cue 里，缺字恒为 0，算进去会把这个数压低
        if e.matched < 0 or not band(e) or k in (skip or ()):
            continue
        s = GS.cnorm((via or {}).get(e.key, e).text)
        if not s:
            continue
        best = None
        for v in variants(subs[e.matched][1], heads):
            v = GS.cnorm(v)
            sm = SequenceMatcher(None, v, s, autojunk=False)
            blocks = [b for b in sm.get_matching_blocks() if b.size]
            if not blocks:
                continue
            r = sm.ratio()
            if best is None or r > best[0]:
                best = (r, blocks[0].b, len(s) - (blocks[-1].b + blocks[-1].size))
        if best:
            out.append((len(s), best[1], best[2]))
    return out


def hitset(script: list[SA.Entry]) -> Counter:
    return Counter(SA.norm(e.text) for e in script if band(e) and e.matched >= 0)


def main() -> int:
    # --help 只放用法；和《魔裁》那套的三处差别写在模块文档串里，给读代码的人（发布前清理，Opus 审查 O6）
    ap = argparse.ArgumentParser(description=CLI_DESCRIPTION,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ref", help="gamescript.py 的剧本 JSON")
    ap.add_argument("--subs", required=True,
                    help="被量的轨：SRT，或 `*-tracks.json`（取 provenance 里的 main_srt）")
    ap.add_argument("--vs", default=None,
                    help="对比臂的轨（同上两种写法）：报命中集合差（三档）与重复认领的变化")
    ap.add_argument("--fuzzy", type=float, default=0.75, help="同 script_align")
    ap.add_argument("--branch-run", type=int, default=5,
                    help="连续这么多条未命中算『长 gap』。这里**只用来分开报**，长 gap 照样算疑似真漏"
                         "（分支已按对话图排出分母，见 script_align.gap_report 的 long_is_branch）")
    ap.add_argument("--show", type=int, default=8, help="列几条疑似真漏")
    ap.add_argument("--show-unmatched", type=int, default=0,
                    help="列几条对不上剧本的 cue（看幽灵上限里装的是什么）")
    ap.add_argument("--out", default=None, help="数和逐条清单写到这个 JSON")
    a = ap.parse_args()

    ref = load_ref(Path(a.ref))
    doc, script, role = ref.doc, ref.script, ref.role
    pv = doc["provenance"]
    gt = pv["gametext"]
    print(f"剧本：{pv['game']} {gt.get('version') or '?'}（文本包指纹 {gt['fingerprint'][7:19]}），"
          f"{len(doc['units'])} 个对话单元 / {len(script)} 行，由 {Path(pv['obs']).name} 圈出"
          f"（gamescript git_head {pv['git_head']}）")
    if not gt.get("integrity_checked"):
        print("  ⚠ 文本包：上游快照的完整性检查没过或没做")
    subs_path, tp = resolve(a.subs)
    subs = read_cues(subs_path)
    st = score(ref, script, subs, a.fuzzy, read_ends(subs_path))
    n_empty = st["subs"] - st["exact"] - st["fuzzy"] - st["contain"] - st["unmatched_subs"]
    print(f"轨 {subs_path.name}"
          + (f"（build_tracks git_head {tp['git_head']}，代码指纹 {tp['code_fp']}）" if tp else "")
          + f"：{st['subs']} 条 cue；精确 {st['exact']}、模糊 {st['fuzzy']}、"
          f"只靠包含认上 {st['contain']}、"
          f"**对不上剧本 {st['unmatched_subs']}（{st['unmatched_subs']/max(1,st['subs']):.1%}，"
          f"幽灵上限——库外的文字：UI / 路人 / 主播自己的字幕…都会落在这里）**"
          + (f"、归一化后为空 {n_empty}" if n_empty else ""))

    dial = [e for e in script if band(e)]
    # 排出分母的按"处境，没有就按类型"分栏（处境盖过类型：没播段里的黑屏旁白记在 OutOfSpan 栏）
    excl = Counter(e.status or e.kind for e in script if not band(e))
    excl["Duplicate"] = sum(1 for u in doc["units"] for ln in u["lines"] if ln["status"] == "Duplicate")
    excl = +excl
    # 变体被认领是预期内的（记在代表行上），不算"排出分母的却被命中"
    excl_hit = Counter(e.status or e.kind for e in script if not band(e) and e.matched >= 0 and e.status != "Variant")
    hit = [e for e in dial if e.matched >= 0]
    print()
    print(f"剧本侧：**该上屏的 {len(dial)} 条**（另有 "
          + "、".join(f"{k} {v}" for k, v in excl.most_common()) + " 不计），"
          f"命中 {len(hit)}（{len(hit)/max(1,len(dial)):.1%}）")
    if ref.variant_of:
        n_groups = len(set(ref.variant_of.values()))
        print(f"  变体组（同一句话的两种说法，一组算一条）：{n_groups} 组，其中 {len(st['via'])} 组"
              f"由代表行以外的那种说法命中")
    if excl_hit:
        print("  ⚠ 排出分母的条目被主轨命中了："
              + "、".join(f"{k} {v}/{excl[k]}" for k, v in excl_hit.most_common())
              + "——Unwalked 被命中 = 分支判错；Choice / OffBand 被命中 = 字幕带以外的字进了这条轨")
    gr = SA.gap_report(dial, a.branch_run, long_is_branch=False)
    side = obs_side(doc)
    C = evalkit.CLASSES[2]
    missed_all = [(k, "short" if k in gr["short_idx"] else "long") for k, e in enumerate(dial)
                  if e.matched < 0 and evalkit.triviality(e.text) == C]
    attr = Counter((g, side[dial[k].key]) for k, g in missed_all)
    print()
    print(f"未命中的『{C}』按 **obs 里读到过没有** 拆（gamescript 的锚点，只看这一行、这一段）：")
    print(f"  {'':<10}" + "".join(f"{x:>12}" for x in OBS_SIDES))
    for g, name in (("short", "短 gap"), ("long", "长 gap")):
        print(f"  {name:<10}" + "".join(f"{attr[(g, x)]:>14}" for x in OBS_SIDES))
    print(f"  读到过 = 丢在轨这一层；只撞常用句 = 别处的同句也能锚上来，算不得这一处读到过；"
          f"没读到 = 丢在 OCR 之前（没出框 / 遮挡 / 显示太短没采到，要摆帧）")

    print()
    print("按上游角色拆（**用来校准分母**：分母内命中率接近 0 说明它不走字幕带；"
          "『全部』连排出分母的也算，别拿它当分母占比读）：")
    print(f"  {'':<34}{'分母内 命中/条':>18}{'全部 命中/条':>18}")
    rb = Counter(role[e.key] for e in dial)
    rbh = Counter(role[e.key] for e in hit)
    rc = Counter(role[e.key] for e in script)
    rh = Counter(role[e.key] for e in script if e.matched >= 0)
    # 进分母的类全列；不进的只列前几类（绝区零散条目的 head 有上百种），其余并成一行
    shown = [r for r, _ in rc.most_common() if rb[r]]
    shown += [r for r, _ in rc.most_common() if not rb[r]][:max(0, ROLE_ROWS - len(shown))]
    for r in shown:
        print(f"  {r:<34}{evalkit.denom(rbh[r], rb[r]) if rb[r] else '—':>18}"
              f"{evalkit.denom(rh[r], rc[r]):>18}")
    rest = [r for r in rc if r not in shown]
    if rest:
        print(f"  {f'其余 {len(rest)} 类（都不进分母）':<30}{'—':>18}"
              f"{evalkit.denom(sum(rh[r] for r in rest), sum(rc[r] for r in rest)):>18}")

    groups, extra = repeat_claims(claims(st))
    n_extra_lines = sum(len(ex) for ex in st["sub_extra"])
    print(f"\n重复认领（原始轨口径）：{groups} 条剧本行被 ≥2 条 cue 认领，多出 {extra} 次"
          f"（**报警不是错误数**：真实重播 / 打字机拆成两条 cue 都会让它涨）")
    print(f"第二遍『整行落在 cue 里』多认领 {n_extra_lines} 行（一条 cue 装着两行剧本、"
          f"或 cue 混进垃圾行够不着模糊阈值；不许和主认领占同一段字，见 claim_contained）")

    ht = head_tail(script, subs, st["via"], {i for ex in st["sub_extra"] for i in ex},
                   st.get("heads") or frozenset())
    if ht:
        n = len(ht)
        h1 = sum(1 for _, h, _ in ht if h >= 1)
        h2 = sum(1 for _, h, _ in ht if h >= 2)
        t1 = sum(1 for _, _, t in ht if t >= 1)
        ex = sum(1 for _, h, t in ht if h == 0 and t == 0)
        print(f"行首 / 行尾缺字（配上的 {n} 对，按内容字比，不计标点）：首尾都不缺 {evalkit.denom(ex, n)}；"
              f"**行首缺 ≥1 字 {evalkit.denom(h1, n)}**（≥2 字 {evalkit.denom(h2, n)}）；"
              f"行尾缺 ≥1 字 {evalkit.denom(t1, n)}")

    if a.show_unmatched:
        un = [(t, raw) for (t, raw, _), h, ex in zip(subs, st["sub_hit"], st["sub_extra"])
              if h < 0 and not ex]
        print(f"\n对不上剧本的 cue（前 {a.show_unmatched} 条 / 共 {len(un)}）：")
        for t, raw in un[:a.show_unmatched]:
            print(f"  {t:8.1f}s  {raw.replace(chr(10), ' / ')[:80]}")

    short = gr["short"]
    if short and a.show:
        print(f"\n疑似真漏抽样（前 {a.show} 处）：")
        for i, k in short[:a.show]:
            for e in dial[i:i + k]:
                print(f"  [{role[e.key]:<10}] {e.src:<26} {e.text[:56]}")

    delta = None
    if a.vs:
        ha = hitset(script)
        vs_path, vp = resolve(a.vs)
        subs_b = read_cues(vs_path)
        script_b = copy.deepcopy(script)       # B 臂量自己那份，A 的 matched 原样留给下面写 JSON
        st_b = score(ref, script_b, subs_b, a.fuzzy, read_ends(vs_path))
        hb = hitset(script_b)
        gained, lost = hb - ha, ha - hb
        g = Counter(evalkit.triviality(t) for t in gained.elements())
        l = Counter(evalkit.triviality(t) for t in lost.elements())
        _, eb = repeat_claims(claims(st_b))
        net = sum(hb.values()) - sum(ha.values())
        print(f"\n命中集合差（归一化文本多重集）：**{a.subs} -> {a.vs}**")
        same = code_same(tp, vp)
        if same is False:
            print(f"  ⚠ 两臂的 build_tracks 代码指纹不同（{tp['code_fp']} / {vp['code_fp']}）——"
                  f"差里混着代码版本的变化")
        elif same is None and tp and vp:
            print("  ⚠ 有一臂的产物没有代码指纹（早于该字段），证明不了两臂是同一份 build_tracks")
        print(f"  命中 {sum(ha.values())} -> {sum(hb.values())}（净 {net:+d}：新增 +{sum(gained.values())}"
              f" / 丢失 -{sum(lost.values())}）；按三档："
              + "  ".join(f"{c} {g[c]-l[c]:+d}" for c in evalkit.CLASSES))
        print(f"  cue {st['subs']} -> {st_b['subs']}；对不上剧本 {st['unmatched_subs']} -> "
              f"{st_b['unmatched_subs']}；重复认领 {extra} -> {eb}")
        ex_l = [t for t in lost if evalkit.triviality(t) == C][:a.show]
        ex_g = [t for t in gained if evalkit.triviality(t) == C][:a.show]
        if ex_l:
            print(f"  丢失的『{C}』：" + " | ".join(t[:24] for t in ex_l))
        if ex_g:
            print(f"  新增的『{C}』：" + " | ".join(t[:24] for t in ex_g))
        delta = {"vs": a.vs, "vs_git_head": vp.get("git_head"), "vs_code_fp": vp.get("code_fp"),
                 "hit_a": sum(ha.values()), "hit_b": sum(hb.values()),
                 "by_class": {c: g[c] - l[c] for c in evalkit.CLASSES},
                 "repeat_extra_b": eb, "cues_b": st_b["subs"],
                 "unmatched_b": st_b["unmatched_subs"]}

    if a.out:
        p = Path(a.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "ref": a.ref, "subs": str(subs_path), "ref_git_head": pv["git_head"],
            "ref_code_fp": pv.get("code_fp"),
            "tracks_git_head": tp.get("git_head"), "tracks_code_fp": tp.get("code_fp"),
            "gametext_fingerprint": pv["gametext"]["fingerprint"],
            "gametext_integrity_checked": bool(pv["gametext"].get("integrity_checked")),
            "cues": st["subs"], "exact": st["exact"], "fuzzy": st["fuzzy"],
            "contain_cues": st["contain"], "contain_lines": n_extra_lines,
            "unmatched_cues": st["unmatched_subs"],
            "band_entries": len(dial), "band_hit": len(hit),
            "excluded": dict(excl), "excluded_hit": dict(excl_hit),
            "variant_groups": len(set(ref.variant_of.values())), "variant_via": len(st["via"]),
            "gap_long_entries": gr["n_long"], "gap_short_entries": gr["n_short"],
            "miss_by_class": gr["by_class"],
            "repeat_groups": groups, "repeat_extra": extra,
            "head_tail": {"pairs": len(ht), "head_ge1": sum(1 for _, h, _ in ht if h >= 1),
                          "head_ge2": sum(1 for _, h, _ in ht if h >= 2),
                          "tail_ge1": sum(1 for _, _, t in ht if t >= 1)},
            "delta": delta,
            "missed": [{"key": e.key, "role": role[e.key], "unit": e.src, "text": e.text,
                        "gap": "short" if k in gr["short_idx"] else "long"}
                       for k, e in enumerate(dial) if e.matched < 0],
            "missed_content": [{"key": dial[k].key, "gap": g, "obs": side[dial[k].key],
                                "role": role[dial[k].key], "unit": dial[k].src,
                                "text": dial[k].text} for k, g in missed_all],
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n-> {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
