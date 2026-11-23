"""内置匹配器：**顺序剧本**——Naninovel 引擎导出的本地化脚本（《魔法少女ノ魔女裁判》那种目录剧本）。

和 `gametext` 的分工：那边是游戏文本库，按 obs 锚出的时段圈候选；这边只有一份**按文件顺序排的剧本**、没有时段，
所以走"游标 + 局部窗 + 惰性信任 + 全局跳转 + 上下文加权"（owner 2026-04 手写的匹配器 `match_ref` 那一套，它是这批素材上现有最好的），
在它上面改了这几处（每处的例子写在对应常量的说明里，量法与读数见 naninovel-matcher 报告）：

1. **剧本直接读 Naninovel 的原始脚本**（`# key` / `; > 命令` / `; 日文` / 中文），Unity 富文本（`<ruby=..>`、`<br>`、
   `<color>`、`<link>`、`<b>`、`<size>`）在读的时候剥掉，`<br>` 记成条内换行 `\\N`；粗体、字色和 Ruby 另存一份带样式的（`jp_rich` / `cn_rich`，
   `flowocr.richtext`，阶段 3 按 `style` / `ruby` 选项投成标签和 Ruby 标记）。匹配用的归一（`norm`）另剥 ASS 标签、
   做 NFKC，带标签的行不会因此配不上。
2. **上下文加分不再能把任意文字挂到有内容的行上**（`Matcher.eligible`、`COVER`）。原版的加权是 `0.6·max(本句, 后文) + 0.4·min(本句, 后文)`，
   后文都对得上时本句相似度为 0 也有 0.6，过了 0.5 的接受门——乱码 cue、证据选择画面、切入动画就这样被挂到下一句的前一行上
   （庭审"答错 → 回放"时那一行常是没走的分支）。现在剧本行有 ≥3 个内容字时，cue 要么本句就过了接受门、要么它的字大半按顺序落在
   这一行里（没读全），才轮得到上下文加分；纯标点 / 1–2 字的行照旧可以只靠上下文认（owner 2026-09-17：这类行靠上下文动量，不作强求）。
3. **上下文沿剧本单调地走**（`CTX_STEPS`）：下一条 cue 可以对下一行、同一行或隔一行。原版是对角线，主轨里同一行常被切成两三条，
   一错开后文全对不上，打字机中途的那条就挂到上一行去。
4. **惰性信任与全局跳转**：局部窗认上了、本句却不够像时也看一眼全局（`STRONG`），后文对得上的全局候选分高就跳，只凭本句的要高出
   `MARGIN`；局部窗里全部打分取最高（原版过 0.85 就停）。各分支后面跟着同一串台词时，后文分不出分支，只有本句分得出。
5. **全局搜索不再取"全库第一个过 0.85 的"**：用字的二元组倒排索引圈候选（`Index`），候选全部打分；同分时后文对得上的优先，
   再取离游标近的（同一个文件优先）。原版按文件顺序扫、过 0.85 就停，重复出现的台词总被认成全库最靠前的那一处；
   每条局部认不上的 cue 都要和三万行逐条比，非主轨喂入时慢到跑不动。
6. **说话人**：每条剧本行带 `speaker` / `speaker_cn`（字段名同 `gamescript` 的剧本行），由行上的 `; > Id: |#key|` 与
   `CharacterNames.bytes` 给出；旁白是 None。名牌隐藏成 `？？？` 的行（`Unknown`）若脚本里有 `; > ＠名前` 注记，
   或 key 的后缀是角色名，记**真正说话的那个人**，否则记 `？？？`。叠加层的名牌（主轨里认成开头行的事件）也按这张名字表出译文（`name_table`）。
7. **时间**：不从剧本合成（原版按 0.02 s/字往前挪起点、把 1 s 内的空档填平，这里都不做）；起点取 cue 里字最早出现的时刻
   （`main_cues`），连续认上同一行、间隔不超过 `MERGE_GAP` 的 cue 并成一条记录（`records`）。

产物（`flowocr-matched/8`）里每条记录：`start` / `end`（秒）、`ocr`（并进来的 cue 里最长的 OCR 原文）、`ref`（认上的剧本行：
`key` / `kind`（key 的后缀）/ `unit`（脚本文件）/ `jp` / `cn` / `score`（本句相似度）/ `speaker` / `speaker_cn`；没认上是 None）、
`how`（`exact` / `fuzzy` / `context`：本句不到门、靠后文认上的 / `none`）、`conf`、`via`（`lock` / `local` / `jump`）、
`cues`（由第几条 cue 组成，下标按 `main_cues` 的顺序）、`n_cues`、`extra`（恒为空：这一作一条 cue 只装一行）、
`echo` / `echo_of`（回显判定，和 `gametext` 共用 `flowocr.analyze.echo`；这里没有剧本锚点时刻，每行取最早那次出现当主）。

入口（`flowocr.extensions`）：
* `cluster_patch(context, options)`：按游戏给建轨的参数覆盖（表是 `PATCHES`，《魔裁》= `game=mocai` 打开按窗并带）。
* `match(document, context, options)`：`document` 是 `*-tracks.json` 的 dict，喂它的**主轨**；
  `context["script"]` 是剧本目录（含 `*.bytes` 的 `Scripts`），`context["names"]` 是 `CharacterNames.bytes`
  （不给就找剧本目录的上一级）。返回 `flowocr-matched/8`（`matchedio`）。
  `options`：`start` / `end`（秒，只匹配起点落在这段里的 cue）、`merge_gap`（秒，默认 `MERGE_GAP`）、
  `overlay`（默认开，判叠加层，同 `scriptmatch`）。

    python -m flowocr.analyze.match out/x/x-tracks.json --matcher naninovel \\
        --ctx script=data/corpus/mocai/script-raw/Scripts --out out/x/x-matched.json
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from flowocr import richtext, textfold
from flowocr.artifacts import evalkit, matchedio, srtio, tracksio

# ---------------------------------------------------------------- 剧本

NOT_BAND_CMDS = ("@choice", "@printDebate", "@toast")
"""不上字幕带的条目：选项、法庭辩论屏、Toast（同 `script_align.NON_DIALOGUE_CMDS` 的判断；`Return` 的行本身就是 `@choice`）。
和 `script_align` 不同的两处：名字被隐藏的 `Unknown` 行是有配音的台词、照样在字幕带里，匹配要认；`@print` / `@char` 引出的行也是字幕带里的字。
`script_align` 是量尺、分母口径不在这里改。"""

KIND_RE = re.compile(r"_([A-Za-z]+?)_?\d*$")
"""key 的后缀 = 行类型 / 说话人 id（`0101Adv02_Ema001` → `Ema`、`_Narrative001` → `Narrative`），同 `script_align.KIND_RE`。"""
SPEAKER_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]*):\s*\|#")
"""`; > Ema: |#0101Adv02_Ema001|`：这一行显示的名牌是谁（`CharacterNames.bytes` 的 id）。"""
HINT_RE = re.compile(r"^＠(?!備考)(\S+)$")
"""`; > ＠遠野ハンナ`：名牌隐藏成 `？？？` 时，脚本注记的真正说话人（`＠備考◆…` 是演出备注，不是名字）。"""
UNITY_TAG = re.compile(r"<[^<>]*>")
BR = re.compile(r"<br\s*/?>", re.I)
ASS_TAG = re.compile(r"{\\[^}]*}")
UNKNOWN = "Unknown"
NARRATIVE = "Narrative"


@dataclass
class Line:
    key: str
    kind: str                 # key 的后缀：`Narrative` / 角色 id / `Choice`…
    jp: str                   # 显示文本：剥了 Unity 富文本，`\N` 是条内换行
    cn: str | None
    unit: str                 # 出自哪个脚本文件（相对剧本目录，不带扩展名）
    band: bool                # 是不是字幕带里的字（`NOT_BAND_CMDS` 之外）
    speaker: str | None       # 说话人的日文名；旁白 None
    speaker_cn: str | None
    jp_rich: str | None = None    # 带样式（含 Ruby）的显示文本（`richtext` 的规范写法），和 `jp` / `cn` 不同时才有
    cn_rich: str | None = None


def strip_markup(t: str) -> str:
    """Unity 富文本 -> 屏幕上的字：`<br>` 记成 `\\N`，其余标签（`<ruby="よみ">字</ruby>` 留下 `字`）剥掉。"""
    return UNITY_TAG.sub("", BR.sub(r"\\N", t)).strip()


def rich_markup(t: str, shown: str | None) -> str | None:
    """同一句带样式的那份（`richtext.from_unity` 投回 `strip_markup` 的结果 `shown` 上，`<ruby="よみ">字</ruby>` 也在里面）；没有样式、投不上是 None。"""
    if not shown or "<" not in t:
        return None
    r = richtext.project(richtext.from_unity(BR.sub("\n", t)), shown)
    return r if r and r != shown else None


def load_names(path: Path | None) -> dict[str, tuple[str, str | None]]:
    """`CharacterNames.bytes`：`; 日文名` 下一行 `Id: 中文名` -> {id: (日文名, 中文名)}。没有文件就是空表（说话人只记 id）。"""
    out: dict[str, tuple[str, str | None]] = {}
    if path is None or not Path(path).is_file():
        return out
    jp = None
    for ln in Path(path).read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln.startswith(";"):
            jp = ln[1:].strip()
        elif ":" in ln and jp is not None:
            k, v = ln.split(":", 1)
            out[k.strip()] = (jp, v.strip() or None)
            jp = None
    return out


def name_table(names: dict[str, tuple[str, str | None]]) -> dict[str, str]:
    """名字表投成叠加层名牌的说话人表（`scriptmatch.overlay_items` 的 `speakers`）：日文名（`SA.norm` 归一，名牌事件按同一个归一查）-> 中文名。
    没有中文名的 id 不收。"""
    from flowocr.analyze import script_align as SA
    return {k: cn for jp, cn in names.values() if cn and (k := SA.norm(jp))}


def head_script_lines(lines: list[Line], names: dict[str, tuple[str, str | None]]) -> frozenset[str]:
    """开头行要排掉的剧本台词（`game_align.head_lines` 的 `script_lines`）：字幕带剧本行的 `SA.norm`（同 `GA.band_lines` 的口径，留标点），
    **角色名表里的名字除外**。叠加层（`match`）和疑似漏字幕判断器（`dev_tools/miss_detect.py`）用这同一份。

    为什么（2026-10-24）：不交这份时，短台词恰好落在两行 cue 的第一行、配过几种末行，就被认成名牌，`default` 叠加在台词上方
    画它的 OCR 原文（《魔裁》f1–f5 名牌层 226 块里 49 块是 `ええ。`、`ああ。`、`裁判の後。` 这类，defaults.md 1.69）。
    名字除外：剧本里有一整条台词只是喊了一声名字、恰好和名牌逐字相同时，排掉它就连真名牌一起排掉了。"""
    from flowocr.analyze import script_align as SA
    plates = {SA.norm(jp) for jp, _ in names.values()}
    return frozenset(k for ln in lines if ln.band and (k := SA.norm(ln.jp)) not in plates)


def load_script(root: Path, names: dict[str, tuple[str, str | None]] | None = None) -> list[Line]:
    """读剧本目录下全部 `*.bytes`（按路径排序 = 文件顺序；游玩顺序和它不全一样，靠全局跳转接上）。"""
    names = names or {}
    by_jp = {jp: cn for jp, cn in names.values()}
    out: list[Line] = []
    root = Path(root)
    for f in sorted(root.rglob("*.bytes")):
        unit = f.relative_to(root).with_suffix("").as_posix()
        blocks: list[list[str]] = []
        for raw in f.read_text(encoding="utf-8").splitlines():
            if raw.startswith("# "):
                blocks.append([raw])
            elif blocks:
                blocks[-1].append(raw)
        for b in blocks:
            key = b[0][2:].strip()
            cmds = [x[3:].strip() for x in b[1:] if x.startswith("; >")]
            jps = [x[1:].strip() for x in b[1:] if x.startswith(";") and not x.startswith("; >")]
            cn = "".join(x.strip() for x in b[1:] if x.strip() and not x.startswith(";"))
            if not jps:
                continue
            m = KIND_RE.search(key)
            kind = m.group(1) if m else "?"
            band = not any(c.split()[0] in NOT_BAND_CMDS for c in cmds if c.startswith("@"))
            shown = next((m.group(1) for c in cmds if (m := SPEAKER_RE.match(c))), None)
            hint = next((m.group(1) for c in cmds if (m := HINT_RE.match(c))), None)
            spk = speaker_of(kind, shown, hint, names, by_jp)
            jp, cn_shown = strip_markup(jps[0]), strip_markup(cn) or None
            out.append(Line(key=key, kind=kind, jp=jp, cn=cn_shown, unit=unit, band=band, speaker=spk[0], speaker_cn=spk[1],
                            jp_rich=rich_markup(jps[0], jp), cn_rich=rich_markup(cn, cn_shown)))
    return out


def speaker_of(kind: str, shown: str | None, hint: str | None, names: dict, by_jp: dict) -> tuple[str | None, str | None]:
    """一行的说话人（日文名, 中文名）。旁白 (None, None)。

    名牌隐藏成 `？？？`（`shown == "Unknown"`）的行：脚本注记了真名（`＠遠野ハンナ`）就记真名，
    key 的后缀本身是角色（`0104Adv13_Yuki001` 显示成 `？？？`）就记那个角色，都没有才记 `？？？`。
    名字表里没有的 id 记 id 本身。"""
    if kind == NARRATIVE and shown is None:
        return None, None
    if shown == UNKNOWN or (shown is None and kind == UNKNOWN):
        if hint:
            return hint, by_jp.get(hint)
        if kind != UNKNOWN and kind in names:
            return names[kind]
        return names.get(UNKNOWN, ("？？？", None))
    sid = shown or kind
    return names.get(sid, (sid, None))


# ---------------------------------------------------------------- 归一与相似度

NON_WORD = re.compile(r"[\W_]")


def norm(t: str) -> str:
    """匹配用的归一：剥 Unity / ASS 标签、`\\N`，NFKC，小书写假名折大，去掉标点与空白（原版的做法 + NFKC 与 ASS 标签）。
    标点不进相似度：OCR 常把 `……` 读成 `...` 或整串丢掉（script-corpus 报告），留着它短句就配不上。"""
    t = ASS_TAG.sub("", UNITY_TAG.sub("", t)).replace("\\N", "").replace("\\n", "")
    t = textfold.fold(t)
    return NON_WORD.sub("", t)


SHORT_LEN = 5
"""全局搜索时，cue 归一后不到这么多字要扣分（原版 `SHORT_PHRASE_LEN`）：短句在剧本里大量重复，跳过去要更强的证据。"""


def sim(a: str, b: str, penalize: bool, sm: SequenceMatcher | None = None) -> float:
    """cue（`a`）与剧本行（`b`）的相似度：`SequenceMatcher.ratio`；quick_ratio 不到 0.25 就直接用它（是上界，原版同样剪枝）。
    `penalize`：短 cue 扣 `(1 − 字数/SHORT_LEN) × 0.5`。"""
    if not a or not b:
        s = 0.0
    else:
        sm = sm or SequenceMatcher(None, autojunk=False)
        sm.set_seqs(a, b)
        s = sm.quick_ratio()
        if s >= 0.25:
            s = sm.ratio()
    if penalize and len(a) < SHORT_LEN:
        s = max(0.0, s - (1 - len(a) / SHORT_LEN) * 0.5)
    return s


# ---------------------------------------------------------------- 匹配

ACCEPT = 0.5
"""认上的门（原版 `SIMILARITY_THRESHOLD`）：加权后的分到这么多才认。"""
LAZY = 0.5
"""局部窗里最好的分到这么多就不跑全局（原版 `LAZY_TRUST_THRESHOLD`）——除非本句自己不够像（`STRONG`）。"""
JUMP = 0.7
"""全局搜到的分到这么多才跳过去（原版 `GLOB_JUMP_THRESHOLD`）：离开游标要更强的证据。"""
STRONG = 0.9
"""局部窗认上了、但本句相似度不到这么多、cue 又不短（≥ `SHORT_LEN` 字）时，照样跑一次全局，看有没有明显更像的行（`MARGIN`）。
原版的惰性信任只看加权分：`……いや、レイアちゃん。１つ見落としている証拠があるよ。` 在局部窗里对上
`何か……見落としていることがあるような。`（0.55）就停了，窗外一字不差的那一行根本没被看到。"""
MARGIN = 0.2
"""局部窗已经认上时，全局的候选若**后文一句都对不上**（只凭本句），要比局部的加权分高出这么多才跳过去：打字机中途的半截
（`きると思うよ！`）在局部有后文撑着（0.78），在别处又一字不差地是另一行短句（`できる……と思うよ。` 0.92）。
全局候选后文也对得上时，分高就跳：庭审里各分支后面跟着同一串台词（`ああん！？…` / `それはきっと…`），
后文分不出是哪个分支，只有本句分得出（`ノアちゃんはその時、足音を立てていないはず` 一字不差的那一行 vs 局部窗里 0.52 的另一个分支）。
全局搜索内部同理：最高分的候选只凭本句时，分差不到这么多、后文对得上的候选优先。"""
LOCAL_SPAN = 10
"""局部窗：游标所在行起往后这么多行（原版 `LOOKAHEAD_NORMAL + 5`）。窗从游标那一行开始，同一行被切成几条 cue 时接得上。
窗里全部打分、取最高的（原版过 0.85 就停：上一行靠后文也能过 0.85，换行瞬间 OCR 混着两行字时，下一行就轮不到）。"""
LOOKAHEAD = 5
"""上下文：往后看这么多条 cue，对得上（≥ `ACCEPT`）的计入（原版 `LOOKAHEAD_NORMAL`）。不跨文件。
原版是对角线（cue i+k ↔ 剧本行 j+k）；这里沿剧本**单调地走**（`CTX_STEPS`），见 `Matcher.score`。"""
CTX_STEPS = (1, 0, 2)
"""上下文往后走时，下一条 cue 可以对：下一行、还是这一行（同一行被切成了几条 cue：打字机中途 + 打完、对比度一变）、
隔一行（那一行没成条）。原版只认下一行：主轨里同一行常被切成两三条，对角线一错开，后文全对不上，
打字机中途的那条就只能挂到上一行去（后文对得上上一行的下一行），或被全局搜到别处同字的短句上去。"""
CTX_W = 0.4
"""加权：`(1 − CTX_W)·max(本句, 后文) + CTX_W·min(本句, 后文)`（原版 `context_sim_weight`）。"""
FLOOR_MIN_CONTENT = 3
"""剧本行有这么多内容字（同 `evalkit` 三档的"有内容"）时，上下文加分要先过 `Matcher.eligible` 那道门。"""
COVER = 0.6
"""本句相似度不到 `ACCEPT` 时，cue 的字至少要有这么多按顺序落在这一行里，才算"同一行没读全"、轮得到上下文加分：
打字机打到一半（`ならば` 对整句）、只读到后半截、读错几个字都过得了；乱码、证据画面、别的台词过不了。
按 cue 被这一行解释了多少算，不按相似度：短 cue 对长行的相似度天然低（3 字对 20 字是 0.26），拿相似度设门会把打字机中途全挡掉，
cue 于是被全局搜到别处同字的短句上去；反过来长 cue 对短行（`それは……今ここには、ないよ。` 对 `そ、それは……！`）相似度有 0.4，
可 cue 的大半不在这一行里。"""
GLOBAL_TOP = 64
"""全局搜索：按共有二元组数取前这么多个候选去打分。"""
GRAM_DF_MAX = 4000
"""出现在这么多行以上的二元组不进候选计数（`って`、`ちゃ` 这种几乎每行都有，只会拖慢、分不出谁）；cue 的二元组全是这种时照用。"""
TIE = 1e-9
CONF_HIGH, CONF_MED = 0.9, 0.75
"""置信度分档，按 cue 本句与剧本行的相似度（同 `scriptmatch` 的两道线）；只靠上下文认上的记 low。"""


def grams(s: str) -> set[str]:
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else ({s} if s else set())


class Index:
    """字的二元组 -> 剧本行下标（只收字幕带的行）。全局搜索按共有二元组数圈候选，不和三万行逐条比。"""

    def __init__(self, ref_norm: list[str]):
        self.post: dict[str, list[int]] = {}
        for j, t in enumerate(ref_norm):
            for g in grams(t):
                self.post.setdefault(g, []).append(j)

    def candidates(self, q: str, top: int = GLOBAL_TOP) -> list[int]:
        """并列的按下标排：`grams` 是集合，`most_common` 的并列顺序随进程的哈希盐变，并列卡在第 `top` 名上时候选集合会两次跑不同。"""
        gs = [g for g in grams(q) if g in self.post]
        rare = [g for g in gs if len(self.post[g]) <= GRAM_DF_MAX] or gs
        cnt: Counter = Counter()
        for g in rare:
            cnt.update(self.post[g])
        return [j for j, _ in sorted(cnt.items(), key=lambda x: (-x[1], x[0]))[:top]]


class Matcher:
    """一条 cue 流对一份剧本。`run()` 返回每条 cue 的 (剧本行下标或 -1, 加权分, 本句相似度, 怎么认上的)。"""

    def __init__(self, refs: list[Line], cue_text: list[str]):
        self.refs = refs
        self.rn = [norm(r.jp) for r in refs]
        self.rcontent = [evalkit.content_len(r.jp) for r in refs]
        self.unit = [r.unit for r in refs]
        self.cn = [norm(t) for t in cue_text]
        # 上下文只在**有字**的 cue 之间走（原版读 SRT 时就把归一后为空的条目丢了）
        self.live = [i for i, t in enumerate(self.cn) if t]
        self.pos = {i: k for k, i in enumerate(self.live)}
        self.index = Index(self.rn)
        self._sm = SequenceMatcher(None, autojunk=False)
        self._cache: dict[tuple[int, int, bool], float] = {}

    def s(self, i: int, j: int, penalize: bool) -> float:
        k = (i, j, penalize)
        v = self._cache.get(k)
        if v is None:
            v = self._cache[k] = sim(self.cn[i], self.rn[j], penalize, self._sm)
        return v

    def score(self, i: int, j: int, penalize: bool, base: float) -> tuple[float, bool]:
        """本句 `base` + 后文，再加权。后文：往后第 k 条 cue 对剧本上从 j 单调往后走到的那一行（`CTX_STEPS`：下一行 /
        同一行 / 隔一行，取最像的），对得上就按原版的方式累进平均（`acc += (ns − acc) / k`）、走到那一行；对不上不走。"""
        acc = base
        p = self.pos[i]
        q = j
        hits = 0
        for k in range(1, LOOKAHEAD + 1):
            if p + k >= len(self.live):
                break
            c = self.live[p + k]
            ns, nq = 0.0, q
            for d in CTX_STEPS:
                t = q + d
                if t >= len(self.refs) or self.unit[t] != self.unit[j]:
                    continue
                v = self.s(c, t, penalize)
                if v > ns:
                    ns, nq = v, t
            if ns >= ACCEPT:
                acc += (ns - acc) / k
                q = nq
                hits += 1
        return max(base, acc) * (1 - CTX_W) + min(base, acc) * CTX_W, hits > 0

    def eligible(self, i: int, j: int, base: float) -> bool:
        """上下文加分能不能用在 (cue i, 剧本行 j) 上（模块说明第 2 条）：剧本行是纯标点 / 1–2 字，或本句自己就过了 `ACCEPT`，
        或 cue 大半按顺序落在这一行里（`COVER`）。"""
        return self.rcontent[j] < FLOOR_MIN_CONTENT or base >= ACCEPT or self.covered(i, j)

    def covered(self, i: int, j: int) -> bool:
        """cue i 的字有 `COVER` 以上按顺序落在剧本行 j 里。"""
        a = self.cn[i]
        self._sm.set_seqs(a, self.rn[j])
        return sum(b.size for b in self._sm.get_matching_blocks()) >= COVER * len(a)

    def local(self, i: int, ptr: int) -> tuple[int, float, float]:
        best, rank = (-1, 0.0, 0.0), (0.0, False)
        for j in range(ptr, min(len(self.refs), ptr + LOCAL_SPAN)):
            base = self.s(i, j, False)
            if not self.eligible(i, j, base):
                continue
            sc, backed = self.score(i, j, False, base)
            # 同分时后文对得上的优先（窗里两支各有一句一模一样的台词，本句满分分不出），再取离游标近的
            if sc > rank[0] + TIE or (sc > rank[0] - TIE and backed and not rank[1]):
                best, rank = (j, sc, base), (sc, backed)
        return best

    def glob(self, i: int, ptr: int | None) -> tuple[int, float, float, bool]:
        q = self.cn[i]
        best: list[tuple[float, float, int, bool]] = []
        for j in self.index.candidates(q):
            base = self.s(i, j, True)
            # 原版：本句不到门就不算上下文，分就是本句——全局跳转 / 冷启动都够不着。
            # 这里另放进 cue 大半落在这一行里的（`covered`：打字机中途的半截，`……ちょっと待って。/就` 对整句只有 0.48）
            if base < ACCEPT and not (self.rcontent[j] >= FLOOR_MIN_CONTENT and self.covered(i, j)):
                continue
            sc, backed = self.score(i, j, True, base)
            best.append((sc, base, j, backed))
        if not best:
            return -1, 0.0, 0.0, False
        top = max(b[0] for b in best)
        pool = [b for b in best if b[0] >= top - TIE]
        if not any(b[3] for b in pool):
            # 最高分的候选后文一句都对不上、只凭本句：分差不到 `MARGIN` 的候选里有后文撑着的，取它
            # （打字机中途的半截 `……ちょっと待って。/就` 在另一章正好是一句 `……ちょっと待って。`，0.93；自己那一行 0.81、后文全对得上）
            backed = [b for b in best if b[3] and b[0] >= top - MARGIN]
            if backed:
                top2 = max(b[0] for b in backed)
                pool = [b for b in backed if b[0] >= top2 - TIE]
        # 同分（重复出现的台词）：后文对得上的优先（本句和后文都满分时分数分不出它们），再取离游标最近的
        # （同一个文件优先，再按行距）；没有游标（冷启动）取文件顺序最前的
        far = ((lambda j: (self.unit[j] != self.unit[ptr], abs(j - ptr))) if ptr is not None else (lambda j: (0, j)))
        sc, base, j, backed = min(pool, key=lambda b: (not b[3], *far(b[2])))
        return j, sc, base, backed

    def run(self) -> list[tuple[int, float, float, str]]:
        out: list[tuple[int, float, float, str]] = [(-1, 0.0, 0.0, "none")] * len(self.cn)
        ptr: int | None = None
        for i in self.live:
            loc = self.local(i, ptr) if ptr is not None else (-1, 0.0, 0.0)
            doubt = loc[1] < LAZY or (loc[2] < STRONG and len(self.cn[i]) >= SHORT_LEN)
            g = self.glob(i, ptr) if doubt else (-1, 0.0, 0.0, False)
            if ptr is None:
                pick, how = g, "lock"
            elif g[1] >= JUMP and g[0] != loc[0] and (loc[1] < LAZY or g[1] >= loc[1] + (TIE if g[3] else MARGIN)):
                pick, how = g, "jump"
            else:
                pick, how = loc, "local"
            if pick[0] >= 0 and pick[1] >= ACCEPT:
                out[i] = (pick[0], pick[1], pick[2], how)
                ptr = pick[0]
        return out


# ---------------------------------------------------------------- 产物

PATCHES: dict[str, dict] = {
    # 《魔裁》：主轨并带按时间窗再判（`--main-band-unit window`，owner 2026-10-06）。这个匹配器只喂主轨，
    # 跨窗缝合把某几个窗的字幕带缝进混合区域时那几句就喂不到；五部按窗判之后新增 1 / 11 / 12 / 0 / 7 行有内容台词、几乎不丢。
    # 全局默认不翻：gametext 不读主轨，代价（新进主轨的 UI）只落在不经匹配器的主轨 SRT 上（defaults.md 4.0.5）
    "mocai": {"main_band_unit": "window"},
}
"""游戏 -> build_tracks 的参数覆盖（`cluster_patch`）。键是参数名，不是 CLI 旗标；游戏由 `context["game"]` 给
（建轨时 `--matcher naninovel --matcher-ctx game=mocai`）——Naninovel 引擎的游戏不止一款，素材标签也认不出是哪款。"""


def cluster_patch(context: dict, options: dict | None = None) -> dict:
    """这款游戏要给默认聚类器的参数覆盖；没给游戏或不在表里就是空字典（不改默认）。"""
    return dict(PATCHES.get(str(context.get("game") or ""), {}))


PRESET, PRESET_OPTS = "matched_srt", {"lang": "cn"}
"""推荐的输出预设（`flowocr.extensions.recommendation`，owner 2026-10-06）：《魔裁》是字幕带一条，用途 2 要的是一份中文 SRT。"""

MERGE_GAP = 2.0
"""连续认上同一行的 cue 并成一条记录的间隔上限（秒），同 `scriptmatch.MERGE_GAP`；`options["merge_gap"]` 可改，0 = 不并。"""

CODE_FP_FILES = ("src/flowocr/analyze/matchers/naninovel.py", "src/flowocr/artifacts/matchedio.py",
                 "src/flowocr/artifacts/evalkit.py", "src/flowocr/artifacts/tracksio.py", "src/flowocr/artifacts/srtio.py",
                 "src/flowocr/textfold.py", "src/flowocr/richtext.py", "src/flowocr/analyze/echo.py",
                 "src/flowocr/extract/framegrid.py")
"""本文件 + 它 import 的本地模块（仓库相对路径）；叠加层的判定借 `scriptmatch`，它的那张表在 `match` 里并进来。"""


def main_cues(doc: dict, start: float | None = None, end: float | None = None
              ) -> tuple[list[srtio.Cue], list[list[dict]], list[bool]]:
    """主轨导出了的 cue（同主轨 SRT：剔完常驻 UI 为空的不要）和每条的事件；`start` / `end` 按起点截一段。

    cue 的起点取**它的字最早出现的时刻**（导出的事件里最早的 `t_start`，不早于 cue 自己的起点）：主轨的 cue 是首尾相接切的，
    上一条字消失到这一条字出现之间的空档（多半是一句没成条的短句，`あっ……！`、`へ？`——也是有配音的台词）算在这一条头上，
    实测 input1 庭审段有 2 s 的。这一刻的字是谁说的，正是说话人那一列要回答的，所以记录的时间不把那段空档算进来。
    第三个返回值是逐 cue 的"是不是面板样"（建轨打的 `panel_like`），回显判定用。"""
    tr = tracksio.main_track(doc)
    if tr is None:
        raise ValueError("tracks.json 没有主轨（provenance.main_track 为空）")
    cues, evs_of, panel_of = [], [], []
    for c in tr["cues"]:
        evs = tracksio.cue_lines(doc, tr, c)
        t0 = c["t_start"] / 1e6
        if not evs or (start is not None and t0 < start) or (end is not None and t0 >= end):
            continue
        t_text = max(t0, min(e["t_start"] for e in evs) / 1e6)
        cues.append(srtio.Cue(t_text, c["t_end"] / 1e6, tuple(e["text"] for e in evs), tr.get("srt") or ""))
        evs_of.append(evs)
        panel_of.append(bool(c.get("panel_like")))
    return cues, evs_of, panel_of


DISPLAY = {
    "groups": {
        "dialogue": {"tier": "always", "label": "台词"},
        **matchedio.NAME_GROUPS,
        "unmatched": {"tier": "on", "label": "主轨上没画成译文的文字"},
        "unfed": {"tier": "off", "label": "主轨以外的文字"}},
    "fill": {"tier": "on"}}
"""顺序剧本匹配器的显示声明（显示协议，`matchedio._check_display`；owner 2026-10-07）：剧本整本是台词，全归 `dialogue`；
只喂主轨，所以 `unmatched` 是主轨上没画成译文的（`matched_srt` 照旧出它的 OCR 原文）、`unfed` 是主轨以外的区域。
填位（多是靠上下文认上的纯标点、短句）默认照画，同之前的 `matched_srt`。"""


def ref_rec(r: Line, score: float) -> dict:
    return {"key": r.key, "kind": r.kind, "unit": r.unit, "jp": r.jp, "cn": r.cn, "score": round(score, 3),
            "speaker": r.speaker, "speaker_cn": r.speaker_cn,
            **{k: v for k, v in (("jp_rich", r.jp_rich), ("cn_rich", r.cn_rich)) if v}}


def records(refs: list[Line], cues: list[srtio.Cue], res: list[tuple[int, float, float, str]],
            cue_norm: list[str], merge_gap: float = MERGE_GAP) -> list[dict]:
    """一条 cue 一条记录；**连续认上同一行**的（中间只隔着没认上的）、间隔不超过 `merge_gap` 秒的并进前一条，时间取首尾。

    间隔上限的理由同 `scriptmatch.MERGE_GAP`，在这份剧本上还多一条：庭审里选错证据之后，同一句会在证据画面关掉后
    **再显示一次**（同一个 key，中间隔几十秒），并成一条就把证据画面那段也算成了这句的显示时间。"""
    out: list[dict] = []
    last = None                                   # out 里最近一条认上的记录
    for i, (c, (j, sc, base, how)) in enumerate(zip(cues, res)):
        raw = "\n".join(c.lines)
        if j < 0:
            out.append({"start": round(c.start, 3), "end": round(c.end, 3), "ocr": raw, "how": "none", "ref": None,
                        "extra": [], "cues": [i], "conf": "none", "n_cues": 1})
            continue
        if last is not None and out[last]["ref"]["key"] == refs[j].key and c.start - out[last]["end"] <= merge_gap:
            p = out[last]
            p["end"] = round(max(p["end"], c.end), 3)
            p["cues"].append(i)
            p["n_cues"] += 1
            if len(raw) > len(p["ocr"]):
                p["ocr"] = raw
            if base > p["_base"]:
                p["_base"], p["ref"]["score"] = base, round(base, 3)
                p["how"] = how_of(cue_norm[i], refs[j], base)
                p["conf"] = conf_of(base)
            continue
        out.append({"start": round(c.start, 3), "end": round(c.end, 3), "ocr": raw, "how": how_of(cue_norm[i], refs[j], base),
                    "ref": ref_rec(refs[j], base), "extra": [], "cues": [i], "conf": conf_of(base), "n_cues": 1,
                    "via": how, "_base": base})
        last = len(out) - 1
    for r in out:
        r.pop("_base", None)
    return out


def how_of(cue_norm: str, r: Line, base: float) -> str:
    """`exact`（归一后相同）/ `fuzzy`（本句过了接受门）/ `context`（本句不到门，靠后文认上的：纯标点、短句、没读全的行）。"""
    if cue_norm == norm(r.jp):
        return "exact"
    return "fuzzy" if base >= ACCEPT else "context"


def conf_of(base: float) -> str:
    return "high" if base >= CONF_HIGH else "med" if base >= CONF_MED else "low"


def stats_of(refs: list[Line], recs: list[dict], n_cues: int) -> dict:
    from flowocr.analyze import echo as EC
    keys = Counter(r["ref"]["key"] for r in recs if r["ref"])
    spk = Counter(("narration" if r["ref"]["speaker"] is None else "character") for r in recs if r["ref"])
    return {"cues": len(recs), "cues_raw": n_cues, "how": dict(Counter(r["how"] for r in recs)),
            "conf": dict(Counter(r["conf"] for r in recs)), "claimed": len(keys),
            # 重复认领：同一行被几条记录认领、多出来的次数（报警不是错误数：回想 / 重播也会让它涨）
            "repeat_groups": sum(1 for v in keys.values() if v > 1), "repeat_extra": sum(v - 1 for v in keys.values()),
            # 多出来的按回显形态拆（`flowocr.analyze.echo`），报警看 `other`；这一作全是台词，没有带内 / 带外之分
            "repeat_extra_echo": EC.counts(recs), "speaker": dict(spk)}


def _flag(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(v)


def match(document: dict, context: dict, options: dict | None = None) -> dict:
    """见文件头。`document` 不动；返回新的 matched dict。"""
    from flowocr import provenance as pv
    from flowocr.analyze import echo as EC
    from flowocr.analyze import scriptmatch as SM
    from flowocr.analyze import textbox as TB
    o = dict(options or {})
    script = context.get("script")
    if not script:
        raise ValueError("naninovel 匹配器要 context['script']（Naninovel 剧本目录，里面是 *.bytes）")
    script = Path(script)
    names_path = Path(context["names"]) if context.get("names") else script.parent / "CharacterNames.bytes"
    names = load_names(names_path)
    lines = load_script(script, names)
    if not lines:
        raise ValueError(f"{script} 下没有读到剧本行（要 Naninovel 导出的 *.bytes）")
    refs = [ln for ln in lines if ln.band]
    start = float(o["start"]) if o.get("start") not in (None, "") else None
    end = float(o["end"]) if o.get("end") not in (None, "") else None
    cues, evs_of, panel_of = main_cues(document, start, end)
    m = Matcher(refs, [c.text("") for c in cues])
    res = m.run()
    merge_gap = float(o.get("merge_gap", MERGE_GAP))
    recs = records(refs, cues, res, m.cn, merge_gap)
    # 回显判定（`flowocr.analyze.echo`，和 gametext 共用）：只喂主轨，全在主轨上、同一个来源；顺序剧本没有锚点时刻，每行取最早那次出现当主
    EC.mark(recs, [c.source for c in cues], [True] * len(cues), panel_of, None, grid=EC.grid_of(document))
    SM.mark_display(recs, cues, lambda c: "dialogue")
    items = ov_stats = textbox = None
    if _flag(o.get("overlay", True)):
        # 没有 `GA.Ref`：名牌的译名（名字表）和开头行要排掉的剧本台词（`head_script_lines`）由这里交进去
        items, ov_stats, textbox = SM.overlay_items(recs, document, evs_of, cues, speakers=name_table(names),
                                           script_lines=head_script_lines(lines, names),
                                           layout=TB.layout_for(context.get("layout"), context.get("game"),
                                                                         context.get("viewport") or tracksio.recorded_viewport(document)))
    tracks_path = str(context.get("tracks_path") or "")
    prov = document.get("provenance") or {}
    return {"schema": matchedio.SCHEMA,
            "provenance": {"tool": "matchers.naninovel", "version": pv.version(), "git_head": pv.git_head(),
                           "code_fp": pv.code_fp(tuple(sorted(set(CODE_FP_FILES) | set(SM.CODE_FP_FILES)))), "argv": [pv.portable(x) for x in context.get("argv") or ()],
                           "ref": pv.portable(script), "names": pv.portable(names_path) if names else None,
                           "subs": pv.portable(tracks_path), "tracks_code_fp": prov.get("code_fp"),
                           "params": {"start": start, "end": end, "merge_gap": merge_gap, "accept": ACCEPT, "jump": JUMP, "cover": COVER}},
            "stats": {**stats_of(refs, recs, len(cues)), "script_lines": len(lines), "band_lines": len(refs),
                      "speaker_names": len(names)},
            "cues": recs,
            "display": DISPLAY,
            **({"overlay": SM.overlay_doc(items, ov_stats, evs_of, textbox)} if items is not None else {})}
