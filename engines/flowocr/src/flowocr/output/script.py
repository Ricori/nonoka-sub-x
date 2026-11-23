"""阶段 3：从阶段 2 产物投影出**字幕稿**（一份能在 Aegisub / mpv 里直接预览、编辑的 ASS），特效另由阶段 4（`flowocr.typeset`）生成。

方案：script-fx 计划（owner 2026-09-27）。这里**只投影**，不重新判断（artifacts.md 第 1 条）：

* 对齐方式是阶段 2 判好的 `regions[].align`（matched 的正文 / extra 用匹配器量的文本框画像 `overlay.textbox` 的 `align`）；matched 各层盖哪些块是 `scriptmatch.overlay_items` 判好的，
  画哪些按产物里匹配器声明的组和用户的 `show` / `hide` 算（显示协议，`matchedio.visible`）；
* tracks 取哪些事件、哪些是常驻 UI，按**轨**取（`tracksio.cue_lines`，UI 的权威清单 `ui_lines` 是逐轨的）；
* 效果（打字机 / 淡入淡出）的判定在这里做一次，写进字幕稿（淡入淡出是 `\\fad`，打字机是 Effect 里 `fo` 的 `tw`），阶段 4 不回读阶段 2。

条目（`Item`）= 一组同起同止的行。来源两个：

* `tracks_items`：**一条 cue 一条**（一句一条，cue-shape 计划的 a）：cue 里的行按 `tracksio.cue_rows` 拼好，行间 `\\N`；
  名牌、常驻 UI、在动的、挂在几条 cue 里的事件照旧一事件一条——挂在几条 cue 里的按句画会画好几遍（底板叠成不透明、打字机从头放好几次）。
  每个事件只画一次。一条里各行出现得不一样早时，阶段 3 写逐行时刻 `rowt`，阶段 4 按行显字（`row_times`）；
* `project_items`：一个匹配条目一条，译文照剧本换行分行（`wrap_lines`），正文按原文整块摆行（`layout.place_rows`）。

一条条目写成字幕稿里的一行（源行）：Style 按角色、Actor 是区域号、Effect 是阶段 4 要的其余参数 `fo:…`
（原文框、打字机时长、轨迹、`was`…，格式见 artifacts.md 的字幕稿一节）；正文是一个 tag 块（`\\an` `\\pos` `\\fs` `\\fad`）+ 纯文字，
剧本带样式（`style=on`，默认）时字中间另有剧本的粗体 / 斜体 / 下划线 / 删除线 / 字色标签（`flowocr.richtext`），
带 Ruby（`ruby=on`，默认）时另有 Ruby 的注释块 `{ruby:读音}正文{/ruby}`（渲染器不画；阶段 4 把它画成正文上方的小字，`flowocr.typeset.ruby`）。
"""
from __future__ import annotations

import bisect
import contextvars
import hashlib
import json
import subprocess
from collections import Counter
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from pathlib import Path

from flowocr import paths
from flowocr import placeholders
from flowocr import provenance
from flowocr import richtext
from flowocr import textfold
from flowocr.analyze import textbox as tbox     # 只用它的字对行（`align` / `ROW_IN_LINE`），和匹配器认整块的行同一把尺；analyze 不 import 阶段 3
from flowocr.artifacts import matchedio, tracksio
from flowocr.output import export, layout
from flowocr.typeset import assfile

FORMAT_VERSION = "1"
"""字幕稿格式的版本，写在 `[Script Info]` 的 `FlowOCR Script` 键里。"""

STYLES = ("Body", "Extra", "Name", "Title", "PlainName", "StrayName", "Choice", "Offmain", "Panel", "UI", "Special")
"""字幕稿的样式 = 角色（Style 表示"这是什么"，不表示"在哪"）。`Offmain` 是 matched 的"主轨外"那一层；
条目在不在主轨另由 `fo` 参数 `off` 记（dev 按它改字色）。`Title` 是名牌下面字小一号的称号（原神的「傭兵」），名字和称号分开，
在 Aegisub 里按样式改得了各自的字号、颜色（owner 2026-11-05）；`StrayName` 是长得像名牌、但不是挂在台词上又认出了译名的字
（匹配器的 `stray_name` 组打开时、退回画 OCR 原文的名牌、tracks 的低置信名牌 `mark_names`）。
匹配器判过名牌字色时（`overlay.name_ink`，`analyze.nameink`），`Name` / `Title` 的主色是这款游戏的名牌色（`style_lines`），
原文字色和名牌色对不上的那种名牌（原神「グライアイの眼」的蓝底白字面板）另用 `PlainName`（白字，名字和称号都用它）。
四种名牌样式阶段 4 都按名牌排（`NAME_STYLES`）。`Special` 是匹配器 `special` 组（`matchedio.SPECIAL`：横幅、战斗喊话、白字提示这类特殊上屏形式的台词与提示，owner 2026-11-07）的条目，
不论哪一层都用它，在 Aegisub 里能整批改或删；阶段 4 按正文行排，SRT 不出。"""
NAME_STYLES = ("Name", "Title", "PlainName", "StrayName")

PREVIEW_BOX = "&H20101010"
STYLE_FILL = "&H00FFFFFF"
"""字幕稿各样式默认的主色（白）。阶段 4 认它：样式的主色还是它，底板取色才自动改字色；不是它的（人改过的、写了名牌色的 `Name` / `Title`）照样式的颜色画。"""
"""预览底框的颜色（`BorderStyle=3` 用 OutlineColour 画框）：近黑、α 0x20。只供预览，阶段 4 的生成行不用它。"""

STYLE_MODES = ("on", "off")
"""`style` 选项：剧本里的行内样式（粗体 / 斜体 / 下划线 / 删除线 / 字色，`flowocr.richtext`）带不带进字幕稿和叠加 ASS。默认带；SRT 一律不带（owner 2026-10-23）。"""
RUBY_MODES = ("on", "off")
"""`ruby` 选项：剧本里的 Ruby（读音、语义注释）写不写进字幕稿（注释块 `{ruby:读音}正文{/ruby}`，`richtext.to_ass`）。默认写；
和 `style` 各管各的；SRT 一律不带（owner 2026-10-23）。"""
TYPEWRITER_MODES = ("off", "on", "dim")
"""打字机效果：`on` 检出了就逐字显出；`dim` 全字出现之前整句半透明（审计时刻用，阶段 4 的选项）；`off` 不做。"""

TW_MIN_FRAMES = 2
"""首字出现到全字出现不到这么多帧的不算打字机（回抠用原生帧、采样级用采样间隔）：
即时出现的字回抠也会给一个晚一两帧的全字时刻；采样级的全字判据会被"后面某帧多读两个字"骗出一个采样间隔。"""

FADE_IN_MAX_US = 300_000
"""复刻的淡入最长这么多（owner 2026-09-25：拿不准的时候，字能及时看清更重要）。回抠量到的"首字 → 全字"
不一定是淡入：整段判不清时它常常是打字过程（gi-s2 回抠产物上 0.4–1.2 s），画成那么长的淡入，字会一直发虚。
主轨上量到的真淡出多数 ≤ 2 帧、最长约 15 帧（text-effects 报告），这个上限盖得住。
⚠ owner 2026-09-25：有点绝对，待观察（followups 清单）。"""

ROW_END_TOL_US = 100_000
"""句条目里的行要**一起消失**：终点和这一句多数行的终点差得超过这么多的事件不进句条目、单独一条——ASS 的一条 Dialogue 只有一个终点，
混进一条晚消失的（水族馆那个图标 `福` 比台词多挂 0.25 s）会把整句一起拖长。同一句的几行在对话框关掉的同一帧消失，回抠后差不过几帧。
用绝对时长、不挂采样间隔：要比的是回抠后的终点（原生帧精度），挂在 0.5 s 的采样间隔上就放过了 `福` 这种；没回抠的产物上同一句各行
本来就落在同一个采样点，这个门碰不到。"""

INSTANT_MAX_SPREAD = 1.0
"""回抠的整段判定 `instant` 有两种来源（`refine_boundaries`）：每字生长的中位时长不到一帧（字确实没有生长），
或每字耗时的离散度 `iqr_over_median` 超过 1.0（判不清）。只有前一种才整段关掉逐字显出；
离散度超过这个数的按逐条的帧数门走——拿不准时跟着原文打字的节奏，不压成一段长淡入。"""


@dataclass
class Item:
    """一组同起同止的行。`events` 是效果（打字机 / 淡入淡出）取时刻用的正文事件，只有 `line` 才带；
    `boxes` 是在动的事件的轨迹；`translated`：叠的是剧本译文（matched），不是这个框里原有的那段字（tracks）——
    译文的行数可以和原文不同，锚轴取整组的（`layout.anchor_axis`）；正文译文（`body_text`）按原文整块摆行、字号跟原文的一字宽走
    （`layout.place_rows` / `layout.body_size`），选项、名牌的字号有保底（`layout.OVERLAY_MIN_SCALE`）；
    原文压回自己的框，锚轴取这一行自己的框，字号只按框定。"""
    start_us: int
    end_us: int
    rows: list[list[float]]
    lines: list[str]
    kind: str                                  # line / name / title / plain_name / stray_name / choice / offmain / block
    events: list[dict] = field(default_factory=list)
    region: int = -1
    main: bool = True
    ui: bool = False
    align: str = "center"
    translated: bool = False
    boxes: list | None = None
    cue: str = ""
    row_h: int = 0                             # block：原文行高（字号从它换算）
    layer: str = ""                            # matched 的层（body / extra / …）；tracks 的条目是空
    group: str = ""                            # matched 的组（显示协议）；tracks 的条目是空。只有 `matchedio.SPECIAL` 改样式（`style_of`）
    key: str | None = None                     # matched：剧本行的键（跨阶段 2 重跑仍然稳定）
    row_events: list[list[dict]] = field(default_factory=list)   # tracks 的句条目：每行的事件（`rows` 同序），取逐行时刻用
    row_leads: list[list[dict]] = field(default_factory=list)    # 每行的半截行（cue 的 `partial`）：只贡献首字时刻，不进 `events`
    source: str | None = None                  # matched 的译文条目：没折过行的剧本原句（SRT 出它，`srt_text`）
    spans: bool = False                        # tracks：跨句常驻（事件挂在这条轨的几条 cue 里）——字幕稿照画，SRT 不出（`srt_blocks`）
    echo: str | None = None                    # matched 的 body / extra：回显判定的形态（`matchedio.ECHOES`）——叠加照画，SRT 默认不出两种（`srt_blocks`）
    echo_of: list | None = None                # 同上：主那次认领 [记录, slot]
    claim: list | None = None                  # 同上：这个条目是哪次认领 [记录, slot]（slot −1 = ref）
    styled: str | None = None                  # matched 的译文条目：带样式 / Ruby 的剧本原句（`richtext` 的规范写法，`style` / `ruby` 关掉的那种已剥掉，占位已标），
                                               # 剩下没有标签是 None。
                                               # `lines` / `source` 照旧是纯文字（量字宽、断行都按它），写正文时才把样式投回去（`styled_lines`）
    block: list[list[float]] | None = None     # matched 的 body / extra：原文整块（匹配器的 `block`，按原文行号排、含补出的行）；None 就是 `rows`
    synth: list[int] = field(default_factory=list)   # 同上：原文整块里匹配器合成的行（`block_src` 是 `synth` 的），共用锚轴不算它们
    textbox: dict | None = None                # 同上：这个区域的文本框画像（匹配器的 `overlay.textbox[区域号]`：valign / pitch / em / chars / box_w）
    block_leads: list[list[dict]] = field(default_factory=list)  # 同上：原文整块每行的早出现证据（`block_leads`）——只给底板的 `grow`，不进 `events`、不碰打字机
    le: float | None = None                    # matched 的正文 / 面板、输出英文时：一个拉丁字平均占几个原文一字宽（画像的 `latin_em`，没有就 `LATIN_EM_DEFAULT`）。
                                               # 按输出语种给、不看内容：字号按拉丁字换算、面板在空格处折都只看它（`fo` 的 `le`）


NO_START = "，。、！？；：,.!?;:)）」』】〕〉》…・·ー"
"""不放行首的（避头）：句读、右括号右引号、省略号、长音。"""
NO_END = "（(「『【〔〈《"
"""不放行尾的（避尾）：左括号左引号。"""
HOLD = 0xF0000
"""`wrap_lines` 里把占位的标记、Ruby 的正文暂换成的字（补充私用区，正文里不会有）。"""


cw = textfold.cw
"""一个字的宽度权重（全角 1、其余 0.5），和匹配器量原文行宽同一份（`textfold.cw`）。"""


def wrap_lines(text: str, chars: float | None = None, styled: str | None = None, latin: dict[str, float] | None = None) -> list[str]:
    """叠加的译文怎么分行（line-breaks 计划，owner 2026-11-09：照目标语言版游戏实际显示的样子）：

    1. 照剧本里写死的换行（`\\N`）切，去掉空行（剧本末尾多一个 `\\N` 的就是一行）；
    2. 匹配器给了这块文本框"一行能放几个全角字"（`chars`，文本框画像，有自动折行的证据才有）时，一行按 `cw` 权重量宽、超过就折：
       尽量往后断，避头（`NO_START` 挂在上一行末尾）、避尾（`NO_END` 挪到下一行），拉丁文为主的行（输出英文时是每一行）只在空格处断；
       断点按游戏的字数定，和叠加画多大字号无关。没有 `chars` 就不折。
    不按原文行框数凑行数：行数和原文对不上时由 `layout.place_rows` 摆。
    占位的标记（`` [`编号:值] ``，`flowocr.placeholders`）当一个字、不拆开，宽按值算（阶段 4 换成值以后的长度）。
    Ruby 的正文（`styled` 是 `text` 带标签的那份，`<ruby=…>` 包着的字，`flowocr.richtext`）同样当一个字（读音对着整段正文的中心画，拆开就对不上了），
    它里面的剧本换行去掉。
    `latin`：英文译文时每个拉丁字占几个全角字（`latin_em` × 字形的相对宽，`_project_one` 给），没有的字照 `cw`——英文按游戏英文版的字宽折（en-target 计划）。"""
    held: list[str] = []
    cwl = (lambda c: latin.get(c) or cw(c)) if latin else cw    # noqa: E731

    def hold(x: str) -> str:
        held.append(x)
        return chr(HOLD + len(held) - 1)

    def marks(x: str) -> str:
        return placeholders.MARK_RE.sub(lambda m: hold(m.group(0)), x)
    parts, at = [], 0
    for a, b in richtext.ruby_spans(styled) if styled and richtext.plain(styled) == text else ():
        parts += [marks(text[at:a]), hold(richtext.BREAKS.sub("", text[a:b]).replace("\n", ""))]
        at = b
    text = "".join(parts) + marks(text[at:])

    def back(x: str) -> str:
        return "".join(held[ord(c) - HOLD] if HOLD <= ord(c) < HOLD + len(held) else c for c in x) if held else x

    def weight(c: str) -> float:
        if HOLD <= ord(c) < HOLD + len(held):
            return sum(cwl(v) for v in placeholders.MARK_RE.sub(lambda m: m.group(2), held[ord(c) - HOLD]))
        return cwl(c)
    out = []
    for s in [x for x in text.replace("\\N", "\n").split("\n") if x.strip()] or [text]:
        out += [back(x) for x in (_fold(s, chars, weight, bool(latin)) if chars else [s])]
    return out


def _fold(s: str, chars: float, weight, words: bool = False) -> list[str]:
    """一行宽过 `chars`（全角字数，`weight` 给每个字的宽）就折，见 `wrap_lines`。`words`：输出英文，整行只在空格处断。"""
    pos = [0.0]                                       # pos[i]：第 i 个字之前的累计宽
    for c in s:
        pos.append(pos[-1] + weight(c))
    latin = words or sum(c.isascii() and c.isalpha() for c in s) > len(s) / 2
    out, start = [], 0
    while pos[-1] - pos[start] > chars:
        cand = [i for i in range(start + 1, len(s)) if not latin or s[i - 1] == " " or s[i] == " "]
        fit = [i for i in cand if pos[i] - pos[start] <= chars]
        if not cand:
            break
        best = max(fit) if fit else min(cand)
        while best < len(s) and s[best] in NO_START:         # 避头：标点挂在上一行末尾
            best += 1
        while best - 1 > start and s[best - 1] in NO_END:    # 避尾：左括号挪到下一行
            best -= 1
        if best >= len(s) or best <= start:
            break
        out.append(s[start:best].rstrip(" "))
        start = best
        while start < len(s) and s[start] == " ":            # 拉丁文断在空格处：空格不进下一行行首
            start += 1
    return out + [s[start:]] if start < len(s) else out


def region_align(doc: dict) -> dict[int, str]:
    return {r["index"]: r.get("align", "center") for r in doc.get("regions", [])}


def need_align(doc: dict, where: str) -> None:
    """叠加要阶段 2 判好的对齐方式；旧产物没有就当场报错，不静默退回居中。"""
    miss = [r["index"] for r in doc["regions"] if "align" not in r]
    if miss:
        raise ValueError(f"{where} 的区域没有对齐判定（regions[].align，缺 {len(miss)} 个）——"
                         "这份产物早于对齐判据，重跑 build_tracks（只是阶段 2，不重跑 OCR）")


def need_textbox(document: dict) -> None:
    """matched 叠加要匹配器写好的文本框画像（`overlay.textbox`）和正文条目的原文整块（`block` / `block_src`）；
    旧产物没有就当场报错，不静默退回按 OCR 行框摆（line-breaks 计划：那样缺首行的条目整体低一行画）。SRT 不查（`build_items` 的 `align=False`）。"""
    ov = document["overlay"]
    body = [it for it in ov["items"] if it["layer"] in ("body", "extra")]
    bad = [it for it in body if not it.get("block") or len(it.get("block_src") or ()) != len(it["block"])]
    if not isinstance(ov.get("textbox"), dict) or bad:
        raise ValueError(("匹配产物没有文本框画像（overlay.textbox）" if not isinstance(ov.get("textbox"), dict) else
                          f"匹配产物有 {len(bad)} 个正文条目缺原文整块（block / block_src）") +
                         "——这份产物早于按原文整块摆行，重跑匹配（只是匹配，不重跑阶段 2）")
    bad_va = sorted({str(p.get("valign")) for p in ov["textbox"].values() if p.get("valign") not in (None, *layout.VALIGNS)})
    if bad_va:
        raise ValueError(f"overlay.textbox 的 valign 只能是 {' / '.join(layout.VALIGNS)}：{', '.join(bad_va)}")


def tracks_items(doc: dict, keep: str, per_event: bool = False) -> list[Item]:
    """tracks → 条目：**一条 cue 一条**（一句一条），每个事件只画一次（cue-shape 计划的 a）。

    * `keep="main"`：主轨的 cue 过一遍 `cue_lines`（和阶段 2 的主轨 SRT 同一个行集）+ 名牌轨的事件；
    * `keep="all"`：全部区域轨，UI 事件也取（`drop_filtered=False`），按**那条区域轨自己的** `ui_lines`、
      全局的 `ui_footprint`、cue 上藏的杂行（`hide`）标成 UI；振り仮名（`ruby` 标）也取、按 UI 标。主轨和名牌轨都是区域轨的另一种投影，不再走。
    * 一条 cue 里的正文事件按 `tracksio.rows_of_events` 拼成行、合成一个条目；起止 = 这些事件和它们的半截行（cue 的 `partial`）的最早起点 / 最晚终点，
      夹在 cue 里。半截行只贡献所在行的首字时刻（`row_leads`），不进 `events`——挂进去会被 `typing_events` 当成更早的事件、把接上它的整行当碎片扔掉。
    * 照旧一事件一条的：名牌（`Name`）、UI、在动的（有轨迹）、在这条轨上挂在几条 cue 里的（按句画会画好几遍）。
    * `per_event=True`：全部一事件一条（`ocr_items` 用：matched 的认领按事件算，它按单个事件扣面板、补画 OCR 原文）。"""
    if keep == "main":
        main = tracksio.main_track(doc)
        if main is None:
            raise ValueError("这份产物没有主轨（provenance.main_track 为空），`default` 没有可叠的")
        trs, drop = [main, *(t for t in doc["tracks"] if t["kind"] == "nameplate")], True
    else:
        trs, drop = [t for t in doc["tracks"] if t["kind"] == "region"], False
    mains, aligns = tracksio.main_regions(doc), region_align(doc)
    # "挂在几条 cue 里 = 跨句常驻"只在切分是一句一条时成立：`--cue-join off`（或没记这个参数的旧产物）下，同一句被 ▼ 闪烁切成几条，
    # 那一行 run 也挂在几条 cue 里——标成 spans 会把真台词从 SRT 里整行丢掉（验收 peer 实测 off 臂少 105 条）
    pargs = (doc.get("provenance") or {}).get("args") or {}
    joined = pargs.get("cue_join") == "on" and pargs.get("srt_mode", "segment") == "segment"   # 同 build_tracks：合并只在 segment 切分下做
    done: set[int] = set()
    out: list[Item] = []

    def single(e: dict, ui: bool, cue_id: str, spans: bool = False) -> Item:
        moving = len(e.get("boxes") or []) >= 2
        return Item(e["t_start"], e["t_end"], [list(e["box"])], [e["text"]],
                    "name" if "nameplate" in (e.get("flags") or []) else "line",
                    events=[e], region=e["region"], main=tracksio.in_main(e, mains) if mains else True,
                    ui=ui, align="center" if moving else aligns.get(e["region"], "center"),
                    boxes=e["boxes"] if moving else None, cue=cue_id, spans=spans)

    for tr in trs:
        ui_text = set(tr.get("ui_lines") or ())
        hung = Counter(i for c in tr["cues"] for i in c["events"])
        for cue in tr["cues"]:
            hide = set(cue.get("hide") or ())
            group = []
            for e in tracksio.cue_lines(doc, tr, cue, drop_filtered=drop):
                if e["id"] in done:
                    continue
                flags = set(e.get("flags") or ())
                ui = not drop and (e["text"].strip() in ui_text or bool({"ui_footprint", "ruby"} & flags) or e["id"] in hide)
                if (per_event or ui or "nameplate" in flags or len(e.get("boxes") or []) >= 2 or hung[e["id"]] > 1
                        or tr["kind"] == "nameplate"):
                    out.append(single(e, ui, cue["id"], spans=joined and not per_event and hung[e["id"]] > 1))
                else:
                    group.append(e)
                done.add(e["id"])
            leads = [doc["events"][i] for i in cue.get("partial") or ()]
            if not group:
                for ld in leads:                  # 没有句条目可挂：半截行照原样单独画（它真在屏上显示过）
                    if ld["id"] not in done:
                        out.append(single(ld, False, cue["id"]))
                        done.add(ld["id"])
                continue
            ends = sorted(e["t_end"] for e in group)
            k = max(range(len(ends)), key=lambda i: (sum(1 for x in ends if ends[i] <= x <= ends[i] + ROW_END_TOL_US), ends[i]))
            lo = ends[k]
            for e in [e for e in group if not lo <= e["t_end"] <= lo + ROW_END_TOL_US]:      # 不和这一句一起消失的：单独一条
                out.append(single(e, False, cue["id"]))
            group = [e for e in group if lo <= e["t_end"] <= lo + ROW_END_TOL_US]
            rows = tracksio.rows_of_events(group)
            # 半截行只在它正好在这一行出现时消失（± 一个采样间隔）才算这一行的首字：隔了几秒才打出来的（wuwa 广告横幅），
            # 拿半截行的起点会把这一行的首字往前拉、把整句的打字机节奏拖长
            row_leads = [[ld for ld in leads if tracksio.same_row(ld["box"], r["box"])
                          and abs(min(e["t_start"] for e in r["events"]) - ld["t_end"]) <= doc["frame_us"]] for r in rows]
            # 没挂到任何一行上的半截行（回抠之后和后续之间隔开了：绝区零 244 s 打到一半黑场 0.8 s 再整句出来），
            # 它是真在屏上单独显示过的一段，照原样单独画，不藏
            used = {id(ld) for rl in row_leads for ld in rl}
            for ld in leads:
                if id(ld) not in used and ld["id"] not in done:
                    # 只画到同一处的那行出现之前：同文本的重复读数（长的被藏、短的当代表）后半段和代表重叠，画两遍底板会叠深
                    later = [min(e["t_start"] for e in r["events"]) for r in rows
                             if tracksio.same_row(ld["box"], r["box"]) and min(e["t_start"] for e in r["events"]) > ld["t_start"]]
                    it = single(ld, False, cue["id"])
                    if later:
                        it = replace(it, end_us=min(it.end_us, min(later)))
                    out.append(it)
                    done.add(ld["id"])
            s0 = min(e["t_start"] for e in group + [ld for rl in row_leads for ld in rl])
            e0 = max(e["t_end"] for e in group)
            s0, e0 = max(s0, cue.get("t_start", s0)), min(e0, cue.get("t_end", e0))
            region = rows[0]["events"][0]["region"]
            out.append(Item(s0, max(s0, e0), [r["box"] for r in rows], [r["text"] for r in rows], "line",
                            events=group, region=region, main=tracksio.in_main(rows[0]["events"][0], mains) if mains else True,
                            align=aligns.get(region, "center"), cue=cue["id"],
                            row_events=[r["events"] for r in rows], row_leads=row_leads))
    out.sort(key=lambda it: (it.start_us, min(e["id"] for e in it.events)))
    return out


def mark_names(items: list[Item]) -> list[Item]:
    """tracks 的名牌条目分档（没有匹配器时的高置信标签，owner 2026-11-05），就地改 `kind`、原样返回：
    * 同时正下方有一行主轨上的句子（`tracksio.name_over_body`，左右取整句 `tracksio.block_head`，间隔上限 `tracksio.NAME_GAP`）的是高置信的名牌，留 `name`（样式 `Name`）；
      同一块名牌前后几次出现（`tracksio.plate_runs`，读数相同（去掉标点后是空串的按原样）、同一处）有一次挂上就都算；
    * 没有的改 `stray_name`（样式 `StrayName`），和匹配器的 `stray_name` 组同一个意思：长得像名牌、没挂在台词上；
    * 高置信的名牌里叠在另一块下面（或上面）、字小一号的（`tracksio.stack_partners`，只在不在动、只有一个框的之间找）改 `title`；一对里一块挂上两块都算。
    和匹配器的分组（`scriptmatch.name_roles`）同一套几何，只是"台词"换成主轨上的句子。建轨按版式标的名牌里界面字不少，
    15 段切片的 tracks 量过（2026-11-05）：高置信那一档几乎全是角色名（绝区零 zzz-s2 40 块里 37 块），另一档多是界面字，
    也有台词不在主轨上（过场）、或名牌在队伍栏和对话记录里的真名字；称号不看这一档时分出 70 块、只有 7 块是真称号，看了之后剩 10 块：这 7 块和鸣潮 3 块 `Lv.` 字样。"""
    lines = sorted((it for it in items if it.kind == "line" and it.main and not it.ui), key=lambda it: it.start_us)
    starts = [it.start_us for it in lines]
    span = max((it.end_us - it.start_us for it in lines), default=0)

    def over_line(it: Item) -> bool:
        lo, hi = bisect.bisect_left(starts, it.start_us - span), bisect.bisect_left(starts, it.end_us)
        return any(lines[k].end_us > it.start_us and tracksio.name_over_body(it.rows[0], tracksio.block_head(lines[k].rows))
                   for k in range(lo, hi))
    names = [i for i, it in enumerate(items) if it.kind == "name" and not it.ui]
    runs = tracksio.plate_runs([(items[i].start_us, items[i].end_us, items[i].rows[0], textfold.bare(items[i].lines[0]) or items[i].lines[0].strip())
                                for i in names])
    hung = dict(zip(names, tracksio.spread([over_line(items[i]) for i in names], runs)))
    idx = [i for i in names if not items[i].boxes and len(items[i].rows) == 1]
    pair = {i: idx[j] for i, j in zip(idx, tracksio.stack_partners([(items[i].start_us, items[i].end_us, items[i].rows[0]) for i in idx]))
            if j is not None}
    for i, j in list(pair.items()):
        if hung[i] or hung[j]:
            hung[i] = hung[j] = True
    hung = dict(zip(names, tracksio.spread([hung[i] for i in names], runs)))
    for i in names:
        items[i].kind = ("title" if i in pair and hung[pair[i]] else "name") if hung[i] else "stray_name"
    return items


def project_items(items: list[dict], doc: dict, lang: str, vis: matchedio.Visible,
                  marks: placeholders.Marks | None = None, drawn: set[int] | None = None,
                  bare: set[int] | None = None, style: bool = True, ruby: bool = True,
                  textbox: dict | None = None, latin: dict[str, float] | None = None) -> list[Item]:
    """matched → 条目：**投影**，把 `overlay_items` 判好的条目摆成 `Item`，不判断任何东西。
    `textbox` 是匹配器的文本框画像（`overlay.textbox`：区域号字符串 → 画像），正文条目按它分行、摆行、定字号。
    画不画（显示协议）：`vis.shows(条目)`（组可见，且不是填位、或填位可见）**且**有 `lang` 的文本——没有就不叠（画面原样）。
    层（`layer`）只定**怎么画**（`Item` 的种类、样式），不定画不画。区域取条目第一个事件的；对齐取那个区域的，多行用公共轴。
    `drawn` 给了就记下画出来的条目用到的事件 id，连同正文条目原文整块借来的事件（`borrowed`：它们在这个条目的底板下，不再当没认领的 OCR 原文补画）；
    `bare` 给了就记下**可见、但没有 `lang` 文本**的条目的事件 id（`unmatched` 组可见时退回画它们的 OCR 原文，`build_items`）。
    译文里的占位由 `marks` 标成 `` [`编号:值] ``（阶段 4 换成值；`flowocr.placeholders`）。
    `style` / `ruby`：剧本带样式 / Ruby 的条目记下带标签的原句（`Item.styled`，关掉的那种剥掉）；阅读面板（`block`）不带。
    `latin`：拉丁字的相对宽（`layout.latin_rel`，英文译文时 `build_items` 量好给），正文按它折行；不给就按 `cw` 一律算半个全角字。"""
    marks = marks if marks is not None else placeholders.Marks()
    mains, aligns = tracksio.main_regions(doc), region_align(doc)
    leads = cue_leads(doc)
    out = []
    for it in items:
        if not vis.shows(it):
            continue
        item = _project_one(it, doc, lang, mains, aligns, marks, style, ruby, textbox, leads, latin)
        if item is None:
            if bare is not None:
                bare.update(it.get("events") or ())
            continue
        out.append(item)
        if drawn is not None:
            drawn.update(it.get("events") or ())
            drawn.update(borrowed(it))
    return out


def borrowed(it: dict) -> list[int]:
    """正文 / extra 条目的原文整块从 cue 里借来的事件 id（`block_src` 是 `event:<id>` 的行）：那一行在这个条目的底板下。
    被名牌条目画着的（`block_drawn_by` 非空）不算——那个事件归名牌条目，画不画看它。"""
    srcs = it.get("block_src") or []
    return [int(src[6:]) for src, by in zip(srcs, it.get("block_drawn_by") or [None] * len(srcs)) if src.startswith("event:") and by is None]


def cue_leads(doc: dict) -> dict[int, set[int]]:
    """主轨上每个事件所在 cue 的藏起来的读数（`hide`，契约上含半截行 `partial`）：事件 id → 这些事件的 id（`block_leads` 的候选）。"""
    main = tracksio.main_track(doc)
    out: dict[int, set[int]] = {}
    for c in (main or {}).get("cues") or ():
        extra = set(c.get("hide") or ())
        if extra:
            for i in c["events"]:
                out.setdefault(i, set()).update(extra)
    return out


def block_leads(it: dict, doc: dict, block: list[list[float]], leads: dict[int, set[int]]) -> list[list[dict]]:
    """matched 正文条目原文整块每一行的早出现证据，阶段 3 的底板 `grow` 取其中最早的首字（`grow_times`；只管底板，宁多勿露，打字机不碰）：

    * 主轨上条目所在 cue 的半截行、藏起来的读数（`cue_leads`）里，和这一行同一行（`tracksio.same_row`）、字对得上这一行的日文
      （`textbox.align` ≥ `textbox.ROW_IN_LINE`；整块的行数和日文行数相同时对这一行，否则对任一行；一两个字的残片几乎总对得上，`hide` 里的杂行
      也就可能混进来，后果只是这一行底板提前到条目起点，宁多勿露）的——建轨把同一处同一行字的早一次读数、
      打到一半的残片放进 `partial` / `hide`，匹配器只认领了后来那次，底板按它长会比日文晚（f1 6470 s：第 2 行的残片「さと」6470.0 s、整行 6471.0 s，
      认领的那次 6473.0 s）。对得上字的不受 `tracks_items` 那条"± 一个采样间隔"的限制：那条防的是隔了几秒才打出来的别的字。
    * 整块从 cue 里借来的行（`block_src` 是 `event:<id>`）：那个事件本身就是这一行的出现时刻。"""
    own = set(it.get("events") or ())
    lines = [x for x in (it.get("jp") or "").split("\\N") if x.strip()]
    cand = sorted({i for e in own for i in leads.get(e, ()) if i not in own})
    srcs = it.get("block_src") or [""] * len(block)
    out: list[list[dict]] = []
    for k, (b, src) in enumerate(zip(block, srcs)):
        texts = [lines[k]] if len(lines) == len(block) else lines
        row = [doc["events"][int(src[6:])]] if src.startswith("event:") else []
        row += [doc["events"][i] for i in cand if tracksio.same_row(doc["events"][i]["box"], b)
                and any(tbox.align(doc["events"][i]["text"], t)[0] >= tbox.ROW_IN_LINE for t in texts)]
        out.append(row)
    return out


def _project_one(it: dict, doc: dict, lang: str, mains, aligns, marks: placeholders.Marks,
                 style: bool = True, ruby: bool = True, textbox: dict | None = None,
                 leads: dict[int, set[int]] | None = None, latin: dict[str, float] | None = None) -> Item | None:
    """一个 matched 条目 → `Item`；没有 `lang` 的文本就是 None（不叠）。
    body / extra 带上原文整块（`block`、`block_src` 里合成的行）和所在区域的文本框画像（`textbox[区域号]`，没有这个区域的是空的：
    对齐取 `top`、行距按原文整块现算、字号退回 `fit_line`、不折行），译文按画像的"一行几个全角字"折（`wrap_lines`），
    整块每行的早出现证据（`block_leads`，`leads` 是 `cue_leads`）给底板的 `grow`。"""
    evs = [doc["events"][i] for i in it.get("events") or []]
    reg = evs[0]["region"] if evs else -1
    kw = dict(region=reg, main=(tracksio.in_main(evs[0], mains) if evs else False) if mains else True, align=aligns.get(reg, "center"), translated=True,
              layer=it["layer"], group=it.get("group") or "", key=it.get("key"), echo=it.get("echo"), echo_of=it.get("echo_of"), claim=it.get("claim"))
    if it["layer"] == "panel":
        if not it["paras"].get(lang) or None in it["paras"][lang]:
            return None
        return Item(it["start_us"], it["end_us"], it["rows"], [marks.mark(p) for p in it["paras"][lang]],
                    "block", row_h=it["row_h"], le=layout.LATIN_EM_DEFAULT if latin else None, **kw)
    rich = (style or ruby) and bool(it.get(f"{lang}_rich"))   # 只有带样式的那份才按规范写法读；纯文字里字面的 `<b>` 照原样是字
    raw = matchedio.text_of(it, lang, rich)
    if rich:
        raw = richtext.select(raw, style, ruby)                # 关掉的那种先剥掉（Ruby 的读音里的占位不标、不进图例）
    styled = marks.mark(raw)                                   # 占位只标一次（`Marks` 记着各编号出现了几处），纯文字从它删标签得到
    if not styled:
        return None
    text = richtext.plain(styled) if rich else styled
    kw["styled"] = styled if styled != text else None
    if it["layer"] in ("body", "extra"):
        prof = (textbox or {}).get(str(reg)) or {}
        kw["align"] = prof.get("align") or kw["align"]         # 匹配器按整行读全的行打分判的对齐优先（`textbox.align_of`）
        le = (prof.get("latin_em") or layout.LATIN_EM_DEFAULT) if latin else None
        wide = {c: le * r for c, r in latin.items()} if latin else None
        return Item(it["start_us"], it["end_us"], it["rows"], wrap_lines(text, prof.get("chars"), kw["styled"], wide), "line", source=text, le=le,
                    events=evs, block=it.get("block"), synth=[i for i, s in enumerate(it.get("block_src") or ()) if s == "synth"],
                    textbox=prof or None, block_leads=block_leads(it, doc, it.get("block") or it["rows"], leads or {}), **kw)
    # 没挂在台词上的名牌（打开了这一组时）另用 StrayName 样式；判过字色、和这款游戏的名牌色对不上的用 PlainName
    kind = "stray_name" if it.get("group") == matchedio.STRAY_NAME else "plain_name" if it.get("ink") is False else it["layer"]
    return Item(it["start_us"], it["end_us"], it["rows"], [text], kind, **kw)   # choice / offmain / name / title：一块一行，不做打字机 / 淡入淡出


PANEL_COVER = 0.8
"""补画 OCR 原文时，框有这么多落在一块已画的阅读面板（`block`）里的事件，**面板在屏的那段时间**不画：面板条目不记它盖住的事件。"""


def ocr_items(doc: dict, ids: set[int], blocks: list[Item] = ()) -> list[Item]:
    """tracks 里 `ids` 这些事件照 tracks 的画法叠 OCR 原文（全部区域轨的视图，一事件一条，UI 照标）。
    matched 的 `unmatched` / `unfed` 组可见时用（`build_items`）：没画成译文的字还在画面上，看得出匹配漏了什么（owner 2026-09-25）。
    已画的阅读面板（`blocks`）底下的事件只扣掉面板在屏的时段（面板条目不记事件，按框和时间判），面板不画就不扣；
    面板前后照画；扣剩不到一个采样间隔的碎段不画——面板的起止只有采样级精度，那一截在它的误差里。
    不在任何区域轨 cue 里的事件这个视图本来就不画（`tracks_items`）。"""
    out = []
    for x in tracks_items(doc, "all", per_event=True):
        e = x.events[0]
        if e["id"] not in ids:
            continue
        b = e["box"]
        a = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
        cover = []
        for blk in blocks:
            r = blk.rows[0]
            if not (blk.start_us < x.end_us and x.start_us < blk.end_us):
                continue                       # 时间上不相交的面板和这条无关（同位置别的时段的面板不算）
            ix = max(0.0, min(b[2], r[2]) - max(b[0], r[0])) * max(0.0, min(b[3], r[3]) - max(b[1], r[1]))
            if a > 0 and ix >= PANEL_COVER * a:
                cover.append((blk.start_us, blk.end_us))
        if not cover:
            out.append(x)                      # 没被面板盖到的照原样（本身短于一个采样间隔的也画）
            continue
        pieces, s = [], x.start_us
        for cs, ce in sorted(cover):
            if cs > s:
                pieces.append((s, min(cs, x.end_us)))
            s = max(s, ce)
        pieces.append((s, x.end_us))
        out += [x if (ps, pe) == (x.start_us, x.end_us) else replace(x, start_us=ps, end_us=pe)
                for ps, pe in pieces if pe - ps >= doc["frame_us"]]
    return out


def native_frame_us(doc: dict) -> int:
    """原生帧长：从产物 `meta` 里 obs 带进来的源帧率算；没有就退到采样间隔（更保守）。"""
    fps = (doc.get("meta") or {}).get("src_fps")
    return int(round(1e6 / fps)) if fps else int(doc["frame_us"])


FRAGMENT_COVER = 0.9
"""框有这么多落在同一条目里**更早出现**的另一个事件框内的事件，算那一行的碎片（`typing_events`、`fade_events`）。"""


def _covered(f: dict, o: dict) -> bool:
    """f 的框至少有 `FRAGMENT_COVER` 落在 o 的框里。"""
    def area(b):
        return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])

    a = area(f["box"])
    ix = [max(f["box"][0], o["box"][0]), max(f["box"][1], o["box"][1]),
          min(f["box"][2], o["box"][2]), min(f["box"][3], o["box"][3])]
    return a > 0 and area(ix) >= FRAGMENT_COVER * a


def typing_events(events: list[dict]) -> list[dict]:
    """算显出时刻（打字机、淡入）用的事件：去掉**碎片**——框几乎整个落在同一条目里一个更早出现的事件框内的事件，
    不管那个事件这时还在不在屏。
    det 偶尔在一两个采样点把一行静止的字切成两框，切出来的那块另起一个事件、被一起认领进这句台词，
    它的起点晚（一行早就打完了），拿它的"全字出现"会把打字机拖到它身上（gi-s2 129.5 s：
    两行 1 秒打完，被 135.5 s 冒出的半截行拖成 6 秒）。
    不要求两者同时在屏：一句打完后变暗被重读、或早已整句在屏的字后来又读成一个新事件时，早先那次可能已经消失，
    后来的读数框落在它里面、全字出现晚，留着就判出假打字机（要求同时在屏量过：《魔裁》f2 7:16:13 打完变暗的一句判出 5.5 s，
    zzz 1:32:16 早已整屏在的技能说明判出 15 s）。淡出用的是另一套判据（`fade_events`）。"""
    return [f for f in events
            if not any(o is not f and o["t_start"] < f["t_start"] and _covered(f, o) for o in events)]


def fade_events(events: list[dict]) -> list[dict]:
    """算淡出用的事件：去掉碎片，但只去和它落进的那个更早事件**同时在屏**的（它出现时那个还没消失）。
    淡出看的是这句字最后什么时候开始消失：更早那次读数已经消失、后来的事件是同一句重新读出来的时候，
    按 `typing_events` 把后来的当碎片去掉，淡出就从早先那次消失时算起，字明明一直亮到条目结束却画成长淡出
    （hsr 1:46:16 那句在 6377.5 s 断了一下、6378.0 s 起重新读出一直亮到 6501.9 s，画成 124 s 的淡出）。
    det 切行切出来的碎片和原行同时在屏，照样去掉（gi-s2 129.5 s 那个半截行）。"""
    return [f for f in events
            if not any(o is not f and o["t_start"] < f["t_start"] < o["t_end"] and _covered(f, o) for o in events)]


TAIL_IN = 0.8
"""重读（`effect_events`）：它的字按顺序落在先出现那个事件的字里的比例至少这么多。"""


def _full_at(e: dict) -> int:
    return e.get("t_full") or e.get("t_full_sampled") or e["t_start"]


def _contained(q: str, c: str) -> float:
    """q 的字有多少按顺序落在 c 里（匹配块总长 / q 的字数），同 `gamescript.contained`。"""
    return sum(b.size for b in SequenceMatcher(None, q, c, autojunk=False).get_matching_blocks()) / max(1, len(q))


def is_reread(f: dict, o: dict, step: int) -> bool:
    """f 是 o 的**重读**：o 先出现、f 在 o 全字之后才出现、o 在 f 出现时已经消失（差不出一个采样间隔），
    且 f 的字几乎都按顺序落在 o 里（`TAIL_IN`）。台词框退场时整体下移，建轨的几何门接不上，最后一帧每行另起一个
    `n_obs` 1 的事件（zzz 7520.0 s：框下移 14–17 px、「ちょちょちょ」读成「ちょちょちよ」）；拿它的全字时刻会把打字机拖到句尾。
    要 o 已经消失：同时在屏的短第二行（gi1「てるの。」整行的字恰好都在第一行里）不是重读。去完标点为空的读数不算。
    o 的终点是末段在动的门留下的采样级（`t_end_how = moving`）时多容一个采样间隔：门把它的终点留在采样点上，比字真正离开晚到一个间隔
    （hsr 2982.0 s 对话记录合上时滑走的一行，终点从 2988.02 s 留到 2988.5 s，原样比就认不出 2987.9 s 起的重读、打字机拖成 6.1 s）。"""
    fb = textfold.bare(f["text"])
    slack = 2 * step if o.get("t_end_how") == "moving" else step
    return (bool(fb) and o["t_start"] < f["t_start"] and f["t_start"] >= _full_at(o)
            and o["t_end"] <= f["t_start"] + slack and _contained(fb, textfold.bare(o["text"])) >= TAIL_IN)


def effect_events(events: list[dict], step: int, pool: list[dict] | None = None) -> list[dict]:
    """算效果时刻用的事件：`typing_events`（去掉 det 切出来的碎片），再去掉**重读**（`is_reread`，对照 `pool`——整条的事件，
    默认就是 `events`；按行算时传整条，重读的原事件可以在别的行）。一行会被拿空时这一行不去重读（`LEVEL / LEVEL` 这类同字多行）。
    只给效果时刻（打字机、淡入淡出、逐行显出、底板跟长）和时间疑点的字数用；条目的成员、起止和文字不变。"""
    core = typing_events(events)
    pool = typing_events(pool) if pool is not None else core
    kept = [f for f in core if not any(o is not f and is_reread(f, o, step) for o in pool)]
    return kept or core


def effect_times(it: Item, doc: dict, frame_us: int, typewriter: bool = True, pool: list[dict] | None = None) -> tuple[int, int, int]:
    """(打字机时长, 淡入, 淡出)，单位 µs；不做的是 0。只有不动的 `line` 才做。

    * 显出的时刻（打字机、淡入）只从 `effect_events` 取（一行被 det 切出来的碎片、退场那一帧的重读不算；`pool` 是判重读用的整条事件，按行算时传）；
      淡出看的是这句字**最后**什么时候开始消失，从 `fade_events` 取（只去和原行同时在屏的碎片，不去重读）：对话记录滚动时同一行在新位置被重新读出、也满足重读的形状，
      排掉的话淡出从第一段读数的消失算起，hsr 1:51:29 记录面板上的一句拉成 36 s 的淡出；碎片的判据为什么和显出时刻不同见 `fade_events`。
    * 打字机：条目的事件被回抠过（带 `t_full_how`）就只认回抠的 `t_full`；整段素材判成 `instant`、而且是
      "字确实没有生长"那种（离散度 ≤ `INSTANT_MAX_SPREAD`）才不做，判不清的、没判定（样本 < 8）的按逐条的门走；没被回抠的（没回抠的产物、没认领到证据的条目、或开发探针按 `labels` / `regions` 只挑了部分区域）
      用采样级的 `t_full_sampled`。按事件判、不按整份产物判：只回抠了部分区域时，其余区域照样有采样级的效果。
      两种都要 ≥ `TW_MIN_FRAMES` 帧（回抠按原生帧、采样级和"全字没量出来"的回抠值按采样间隔）。
    * 淡出：`t_full_end`（回抠）到条目结束，不足一帧的不做——回抠会把没淡出的也填上，夹在结尾前一点。
      成员里有**开放边界**（`t_end_how = open`：没看到它消失）或末段在动（`moving`：终点留在采样级、不填 `t_full_end`）的整条不做——
      都没有消失的证据，不知道它什么时候、怎么消失，不能借别的行的 `t_full_end` 给整句（多行译文连底板）做淡出
      （hsr 6376.75 s 对话记录里一句随面板滚走，借了早先那次已消失读数的 `t_full_end`，画成 9 s 的淡出）。
    * 淡入：**实际没做**逐字显出时才复刻（首字出现 → 回抠的全字出现），不足 `TW_MIN_FRAMES` 个原生帧、
      或全字没量出来的不做，最长 `FADE_IN_MAX_US`。"""
    if it.kind != "line" or it.boxes or not it.events:
        return 0, 0, 0
    s, e = it.start_us, it.end_us
    refined = refined_of(it, doc)
    core = effect_events(it.events, int(doc["frame_us"]), pool)

    def last(key):
        v = [ev[key] for ev in core if ev.get(key) is not None]
        return min(max(v), e) if v else None
    full = last("t_full")
    # 回抠只量到首字、全字没量出来（`t_full_how.full == "none"`）时，t_full 是融合的回退值，精度是采样级的：
    # 打字机按采样间隔的门量，不然"多读两个字"骗出来的假打字机会从 2 个原生帧的门漏过去；也不拿它复刻淡入
    unmeasured = "none" in [(ev.get("t_full_how") or {}).get("full") for ev in core if ev.get("t_full") is not None]
    tw = 0
    if typewriter and refined:
        eff = doc["text_effect"]
        no_growth = eff.get("verdict") == "instant" and eff.get("iqr_over_median", 0.0) <= INSTANT_MAX_SPREAD
        step = doc["frame_us"] if unmeasured else frame_us
        if full is not None and not no_growth and full - s >= TW_MIN_FRAMES * step:
            tw = full - s
    elif typewriter:
        fs = last("t_full_sampled")
        if fs is not None and fs - s >= TW_MIN_FRAMES * doc["frame_us"]:
            tw = fs - s
    tail = fade_events(it.events)
    fend = (None if any(ev.get("t_end_how") in ("open", "moving") for ev in tail)
            else min(max(v), e) if (v := [ev["t_full_end"] for ev in tail if ev.get("t_full_end") is not None]) else None)
    fout = e - fend if fend is not None and e - fend >= frame_us else 0
    # 淡入也要 ≥ 2 个原生帧：即时出现的字回抠给的全字时刻晚一两帧，不该变成一个 1–2 帧的 \fad
    fin = full - s if tw == 0 and full is not None and not unmeasured and full - s >= TW_MIN_FRAMES * frame_us else 0
    return tw, min(fin, FADE_IN_MAX_US), fout


def row_times(it: Item, doc: dict, frame_us: int, typewriter: bool = True) -> list[tuple[int, int]] | None:
    """句条目的逐行时刻 `(首字, 全字)`，毫秒、相对条目起点（阶段 4 按行显字，`fo` 的 `rowt`）。打不打字**按行判**：
    每行单独套 `effect_times` 的整套门（素材判"字没有生长"、回抠按原生帧 / 采样级按采样间隔、≥ `TW_MIN_FRAMES`），
    不打字的行写成全字 = 首字。首字取这一行的事件和半截行（`row_leads`）里最早的起点，用事件自己的时刻（不像 `grow` 往前放一个采样间隔）。
    只有一行、在动的、或各行首字都在条目起点一个采样间隔之内的不写（整条的 `tw` 就够了，和 matched 一样）。"""
    if it.kind != "line" or it.boxes or len(it.rows) < 2 or len(it.row_events) != len(it.rows):
        return None
    firsts = [min(e["t_start"] for e in evs + leads) for evs, leads in zip(it.row_events, it.row_leads or [[]] * len(it.rows))]
    if all(f - it.start_us < doc["frame_us"] for f in firsts):
        return None
    out = []
    for f, evs, row, line in zip(firsts, it.row_events, it.rows, it.lines):
        tw = effect_times(replace(it, start_us=f, rows=[row], lines=[line], events=evs, row_events=[], row_leads=[]),
                          doc, frame_us, typewriter, it.events)[0]
        a = max(0, (f - it.start_us) // 1000)
        out.append((a, min(a + tw // 1000, (it.end_us - it.start_us) // 1000)))
    return out


def effect_modes(o: dict) -> tuple[bool, bool]:
    """`typewriter`（off / on / dim，dim 在阶段 3 等于 on）、`fade`（off / on）两个选项 → (判打字机, 判淡入淡出)；写错了报错。
    `render_script` 和时间疑点检测（`timing_check`）共用，两边对选项的读法一致。"""
    typewriter, fade = str(o.get("typewriter", "on")), str(o.get("fade", "on"))
    if typewriter not in TYPEWRITER_MODES:
        raise ValueError(f"typewriter 只能取 {list(TYPEWRITER_MODES)}：{typewriter!r}")
    if fade not in ("off", "on"):
        raise ValueError(f"fade 只能是 off / on：{fade!r}")
    return typewriter != "off", fade == "on"


def item_effects(it: Item, doc: dict, frame_us: int, typewriter: bool = True, fade: bool = True
                 ) -> tuple[int, int, int, list[tuple[int, int]] | None]:
    """字幕稿里这一条实际写的效果：(打字机时长, 淡入, 淡出, 逐行时刻)，前三个是 µs，逐行时刻同 `row_times`。
    `export_script` 和时间疑点检测（`timing_check`）都走这里，两边看到的是同一组时刻。

    有逐行时刻时按行显字：整条的打字机时长是最后一行打完的时刻（一行都不打字就是 0）；淡入只在条目起点、只看第一行，有行在打字就不做。"""
    if it.kind == "block":
        return 0, 0, 0, None
    tw_us, fin, fout = effect_times(it, doc, frame_us, typewriter)
    rowt = row_times(it, doc, frame_us, typewriter)
    if rowt is not None:
        typed = [(a, b) for a, b in rowt if b > a]
        tw_us = max(b for _, b in typed) * 1000 if typed else 0
        fin = 0 if typed else effect_times(replace(it, rows=it.rows[:1], lines=it.lines[:1], events=it.row_events[0],
                                                   row_events=[], row_leads=[]), doc, frame_us, typewriter, it.events)[1]
    if not fade:
        fin = fout = 0
    return tw_us, fin, fout, rowt


def refined_of(it: Item, doc: dict) -> bool:
    """条目的事件被回抠过（带 `t_full_how`）：全字时刻只认回抠的 `t_full`，否则用采样级的 `t_full_sampled`。"""
    return "text_effect" in doc and any("t_full_how" in ev for ev in it.events)


def grow_times(it: Item, doc: dict, tw_us: int) -> list[tuple[int, int]] | None:
    """打字机条目每一行原文的（首字出现, 全字出现），毫秒、相对条目起点：阶段 4 让底板跟着原文长（`fo` 的 `grow`）。
    和 `effect_times` 同一批事件（`effect_events`）、同一个全字时刻的键（`refined_of`），夹到 `[0, tw]` 里——
    各行全字的最大值不超过整条的 `tw`，原文不会长得比打字机节奏还慢。事件按纵向重叠归到 `it.rows` 的行，每行取最早的首字、最晚的全字；
    一行没量到全字的按首字算（整行在首字时就盖上，宁多勿露）；一行没分到事件的，在前后两行之间按框宽比例补。没做打字机的是 None。
    **采样级的时刻往前放一个采样间隔**：没回抠的首字 / 全字只知道在这个采样点之前出现，真实时刻最多早一个间隔，按它画原文会露出来一截
    （gi2 摆帧：第二行原文比 `t_start` 早约 0.5 s 出现）；回抠过的（`t_full_how` 的 `first` / `full` 量到了）是原生帧精度，照用。"""
    if tw_us <= 0 or not it.rows:
        return None
    refined = refined_of(it, doc)
    key = "t_full" if refined else "t_full_sampled"
    tw, s0, step = tw_us // 1000, it.start_us, int(doc["frame_us"])
    n = len(it.rows)
    first: list[int | None] = [None] * n
    full: list[int | None] = [None] * n

    def clamp(us: int) -> int:
        return min(tw, max(0, (us - s0) // 1000))
    for ev in effect_events(it.events, step):
        b = ev["box"]
        ov = [min(b[3], r[3]) - max(b[1], r[1]) for r in it.rows]
        i = max(range(n), key=lambda k: (ov[k], -abs((b[1] + b[3]) - (it.rows[k][1] + it.rows[k][3]))))
        how = (ev.get("t_full_how") or {}) if refined else {}
        st = clamp(ev["t_start"] - (0 if how.get("first", "none") != "none" else step))
        first[i] = st if first[i] is None else min(first[i], st)
        if ev.get(key) is not None:
            f = clamp(ev[key] - (0 if how.get("full", "none") != "none" else step))
            full[i] = f if full[i] is None else max(full[i], f)
    for i, leads in enumerate(it.row_leads[:n]):          # 半截行：只贡献这一行的首字（cue 的 partial，见 tracks_items）
        for ld in leads:
            how = (ld.get("t_full_how") or {}) if refined else {}
            st = clamp(ld["t_start"] - (0 if how.get("first", "none") != "none" else step))
            first[i] = st if first[i] is None else min(first[i], st)
    segs: list[tuple[int, int] | None] = [None if a is None else (a, max(a, b if b is not None else a))
                                         for a, b in zip(first, full)]
    i = 0
    while i < n:
        if segs[i] is not None:
            i += 1
            continue
        j = i
        while j < n and segs[j] is None:
            j += 1
        a = segs[i - 1][1] if i > 0 else 0
        b = max(a, segs[j][0] if j < n else tw)
        ws = [max(0.0, r[2] - r[0]) for r in it.rows[i:j]]
        tot, acc = sum(ws) or 1.0, 0.0
        for k, w in zip(range(i, j), ws):
            segs[k] = (round(a + (b - a) * acc / tot), round(a + (b - a) * (acc + w) / tot))
            acc += w
        i = j
    return segs


def grow_param(segs: list[tuple[int, int]] | None) -> str | None:
    return "|".join(f"{s}+{f}" for s, f in segs) if segs else None


def effect_why(it: Item, doc: dict, tw: int, fin: int, fout: int) -> str | None:
    """效果判定的依据，一句话（只有 `why=on` 写进字幕稿，`dev` 审计用）。"""
    if it.kind != "line" or not it.events:
        return None
    if it.boxes:
        return "在动，不做效果"
    refined = "text_effect" in doc and any("t_full_how" in ev for ev in it.events)
    src = "回抠" if refined else "采样级"
    if refined and (doc["text_effect"] or {}).get("verdict"):
        src += f"，素材判 {doc['text_effect']['verdict']}"
    parts = [f"打字机 {tw / 1e6:.2f}s" if tw else "", f"淡入 {fin / 1e6:.2f}s" if fin else "",
             f"淡出 {fout / 1e6:.2f}s" if fout else ""]
    return "；".join(p for p in parts if p) + f"（{src}）" if any(parts) else f"无效果（{src}）"


# ---- 写字幕稿 ----

def style_of(it: Item) -> str:
    if it.kind == "block":
        return "Panel"
    if it.group == matchedio.SPECIAL:
        return "Special"
    if it.kind == "choice":
        return "Choice"
    if it.kind == "offmain":
        return "Offmain"
    if it.ui:
        return "UI"
    if it.kind == "name":
        return "Name"
    if it.kind == "title":
        return "Title"
    if it.kind == "plain_name":
        return "PlainName"
    if it.kind == "stray_name":
        return "StrayName"
    return "Extra" if it.layer == "extra" else "Body"


def num(v: float) -> str:
    """框和轨迹的坐标写进 `fo`：整数写整数，其余照 `repr` 写全，读回来逐位相同。"""
    return str(int(v)) if float(v).is_integer() else repr(float(v))


def rows_param(rows: list[list[float]]) -> str:
    """数值用空格分隔：参数在 Effect 字段里，逗号会切断 ASS 行的字段。"""
    return "|".join(" ".join(num(x) for x in r) for r in rows)


def path_param(boxes: list) -> str:
    return "|".join(f"{int(t)}:" + " ".join(num(x) for x in b) for t, b in boxes)


NOT_BODY_STYLES = ("Panel", "Choice", "Offmain", *NAME_STYLES)
"""不按正文译文排的样式：阶段 4 按样式推出的种类不是 `line` 的那些（`typeset.core.KIND_OF_STYLE`）。"""


def body_text(it: Item) -> bool:
    """正文译文：matched 的译文、样式不是面板 / 选项 / 名牌——按原文整块摆行（`layout.place_rows`）、字号走 `layout.body_size`。
    阶段 4 的判据是同一个（样式推出的种类是 `line`、`fit=tr`），所以按样式判、不按层判（`Special` 不论哪一层都按正文排）。"""
    return it.translated and it.kind != "block" and style_of(it) not in NOT_BODY_STYLES


def src_block(it: Item) -> list[list[float]]:
    """原文整块：匹配器给了就是它，否则是条目的行框。"""
    return it.block or it.rows


def view_rows(it: Item, H: int) -> list[list[float]]:
    """正文译文按原文整块摆出来的各行（`layout.place_rows`，阶段 4 同一函数、同一组参数——字幕稿的预览位置和字号按它算）；别的条目就是原文框。"""
    if not body_text(it):
        return it.rows
    tb = it.textbox or {}
    return layout.place_rows(src_block(it), len(it.lines), tb.get("valign") or "top", tb.get("pitch"), H)


def body_fo(it: Item) -> dict:
    """正文译文写进 `fo` 的摆行参数（阶段 4 只读字幕稿，要靠它们摆出和预览相同的行）：`va` 对齐、`pitch` 行距、`em` 原文一字宽、
    `bw` 文本框宽、`le` 英文一个拉丁字平均占几个原文一字宽（`Item.le`，只有输出英文时有）、`syn` 原文整块里合成的行（共用锚轴不算）。
    没有的不写（阶段 4 按缺省：`top`、按 `box` 现算、`fit_line`、画面宽、按全角字换算）。"""
    tb = it.textbox or {}
    return {"va": tb.get("valign") or "top", "pitch": num(tb["pitch"]) if tb.get("pitch") else None,
            "em": num(tb["em"]) if tb.get("em") else None, "bw": num(tb["box_w"]) if tb.get("box_w") else None,
            "le": num(it.le) if it.le else None, "syn": " ".join(map(str, it.synth)) or None}


def bord(size: int) -> str:
    """预览底框的边距跟着字号（`layout.plate_pad`）：样式的 `BorderStyle=3` 框外扩 `Outline`，这里逐行用 `\\bord` 覆盖。
    阶段 4 自己按字号重算底板，行首块里的 `\\bord` 由它管、不带进成品。"""
    return "\\bord%g" % round(layout.plate_pad(size), 1)


def draft_line(it: Item, size: int, W: int, H: int, widths: dict, tw_us: int, fin: int, fout: int, why: str | None,
               font: str = export.FONT_CN, grow: list[tuple[int, int]] | None = None,
               rowt: list[tuple[int, int]] | None = None, axis: float | None = None) -> str:
    """一个条目 → 字幕稿里的一行源行：正文是一个主流 tag 块 + 纯文字（行间 `\\N`），阶段 4 的参数 `fo:…` 放在 Effect 字段——
    正文里没有参数，改字的时候碰不到（owner 2026-09-27 看 Aegisub 里的字幕稿后定：正文尽量是纯文字、便于编辑；参数放 Effect）。"""
    t0, t1 = export.ass_time(it.start_us, True), export.ass_time(it.end_us, False)
    actor = f"r{it.region}" if it.region >= 0 else ""
    ev = list(dict.fromkeys(e["id"] for e in it.events if "id" in e))       # matched 条目的事件可能重复列出
    fo: dict = {"ev": " ".join(map(str, ev)) or None, "key": it.key, "box": rows_param(it.rows)}
    if it.kind == "block":
        size, wrapped = layout.block_layout(it.rows[0], it.lines, it.row_h, font, widths, latin=it.le is not None)
        pos = "%.0f,%.0f" % (it.rows[0][0], it.rows[0][1])
        fo.update({"wrap": it.row_h, "le": num(it.le) if it.le else None, "plate": "box", "off": not it.main,
                   "was": f"pos:{pos.replace(',', ' ')}|fs:{size}|an:7"})
        head = "{\\an7\\pos(%s)\\fs%d%s\\q2}" % (pos, size, bord(size))
        body = "\\N".join(export.ass_text(ln) for ln in wrapped)
        return f"Dialogue: 0,{t0},{t1},{style_of(it)},{actor},0,0,0,{assfile.encode_fo(fo)},{head}{body}"
    rows = view_rows(it, H)
    tw = [layout.line_width(font, ln, widths) * size for ln in it.lines]
    if body_text(it):
        axes = layout.block_axes(rows, src_block(it), it.synth, tw, it.align, W)
        fo.update({"box": rows_param(src_block(it)), **body_fo(it)})
    else:
        per_row = len(it.lines) == len(rows)
        axes = layout.axes_in_screen(rows, tw if per_row else [max(tw, default=0.0)] * len(rows),
                                     it.align, it.translated, W, axis)
    x = axes[0]
    y = (rows[0][1] + rows[-1][3]) / 2 - export.ink_dy(font) * size     # 墨迹中心对准各行的竖直中心（同阶段 4，`export.ink_dy`）
    if it.boxes:                                      # 预览：整条一个 \move 从首点到末点，精确轨迹在 path 里
        p0, p1 = tuple(it.boxes[0][1][:2]), tuple(it.boxes[-1][1][:2])
        base = (it.rows[0][0], it.rows[0][1])
        where = layout.pos_tag(x, y, (it.start_us, it.end_us, p0, p1), base)
    else:
        where = "\\pos(%.0f,%.0f)" % (x, y)
    owned = where[1:].replace("(", ":", 1).rstrip(")").replace(",", " ")  # "pos:x y" / "move:x0 y0 x1 y1 0 t"
    # 原文框本来装几行：OCR 原文带换行时一个框装着几行（tracks 事件本身带换行），比框数多；阶段 4 只把超出的行往外扩
    cap = sum(len([s for s in ln.split("\n") if s.strip()]) or 1 for ln in it.lines) if not it.translated else len(it.rows)
    fo.update({"fit": "tr" if it.translated else "src", "rows": True, "plate": True,
               "cap": cap if cap != len(it.rows) else None,
               "path": path_param(it.boxes) if it.boxes else None, "tw": tw_us // 1000 or None, "grow": grow_param(grow), "rowt": grow_param(rowt),
               "off": not it.main, "why": why, "was": f"{owned}|fs:{size}|an:{layout.ANCHOR[it.align]}"})
    fad = "\\fad(%d,%d)" % (fin // 1000, fout // 1000) if fin or fout else ""
    head = "{\\an%d%s\\fs%d%s%s}" % (layout.ANCHOR[it.align], where, size, bord(size), fad)
    styled = styled_lines(it, it.lines)
    body = richtext.to_ass("\n".join(styled), export.ass_text) if styled else "\\N".join(export.ass_text(ln) for ln in it.lines)
    return f"Dialogue: 0,{t0},{t1},{style_of(it)},{actor},0,0,0,{assfile.encode_fo(fo)},{head}{body}"


def styled_lines(it: Item, lines: list[str]) -> list[str] | None:
    """把条目带样式的原句投回 `lines`（纯文字的行，`wrap_lines` 分好的）：每行插进规范写法的标签，
    样式的标签跨行时各行不成对（整条一起转成 ASS），Ruby 不跨行（`wrap_lines` 不拆它）。没有样式、或行里的字和原句对不上（用户扩展改过行）是 None，照纯文字写。"""
    if not it.styled:
        return None
    got = richtext.project(it.styled, "\n".join(lines))
    return got.split("\n") if got is not None else None


def style_lines(font: str = export.FONT_CN, fills: dict[str, str] | None = None) -> list[str]:
    """字幕稿的样式：一个角色一个，参数相同。`BorderStyle=3` 出预览底框（各渲染器都认，框用 OutlineColour、外扩 `Outline`：样式里是 `PLATE_PAD_PX`，各行的 `\\bord` 按字号覆盖，`bord`）；
    次要色全透明、阴影 0：人要自己调某一行的打字机节奏时写 `\\k` 族 tag，预览就是逐字显出（`\\ko` 藏不住阴影，第 0 步探针量过）。
    `fills`：样式 → 主色（`&H00BBGGRR`），没给的是 `STYLE_FILL`（`name_fills` 给名牌色）。"""
    fills = fills or {}
    return [f"Style: {s},{font},48,{fills.get(s, STYLE_FILL)},&HFF000000,{PREVIEW_BOX},&H00000000,"
            f"0,0,0,0,100,100,0,0,3,{layout.PLATE_PAD_PX},0,5,0,0,0,1" for s in STYLES]


NAME_INK_STYLES = {"name": "Name", "title": "Title"}
"""`overlay.name_ink` 的层 → 写名牌色的样式。"""


def name_fills(document: dict) -> dict[str, str]:
    """匹配产物判过名牌字色时（`overlay.name_ink`：层 → `#rrggbb`）`Name` / `Title` 的主色（`&H00BBGGRR`）；tracks、没判过的是空的。"""
    ink = (document.get("overlay") or {}).get("name_ink") or {}
    return {NAME_INK_STYLES[k]: f"&H00{v[5:7]}{v[3:5]}{v[1:3]}".upper() for k, v in ink.items() if k in NAME_INK_STYLES}


def stacks(it: Item) -> bool:
    """参加名牌 / 称号共用锚轴（`layout.stacked_axes`）的条目：居中、不在动、只有一个原文框的名牌（三种名牌样式）。阶段 4 的 `Typesetter.stack_axes` 是同一判据。"""
    return style_of(it) in NAME_STYLES and it.align == "center" and not it.boxes and len(it.rows) == 1


def export_script(path: Path, items: list[Item], doc: dict, typewriter: bool = True, fade: bool = True,
                  why: bool = False, info: dict | None = None, fills: dict[str, str] | None = None) -> tuple[Counter, dict]:
    """把条目写成字幕稿。返回（计数：各种类、打字机 / 淡入淡出条数、对齐；量好的字宽——连跑阶段 4 时交给它，不用再量一遍）。

    * 字号：正文译文按 `layout.body_size`（一字宽跟原文、宽过文本框才缩），别的按 `layout.fit_line`（原文只按框，选项、名牌的译文有保底、不超画面）；
      同栏同时在屏的选项取中位数（`layout.same_size`）；面板按 `layout.block_layout` 折行；
    * 位置：单行就是阶段 4 的位置；多行是整组的锚轴和几行的竖直中心（阶段 4 逐行摆，正文译文按 `view_rows` 摆出的行，预览里行距是字体的）；
      在动的写一个首点到末点的 `\\move` 供预览，精确轨迹在 `fo` 的 `path`；
    * 效果见 `effect_times`，打字机记成 `fo` 的 `tw`（正文保持纯文字）、淡入淡出写成 `\\fad`；
    * `fills`：样式的主色（`name_fills`，匹配器判过的名牌色），没给的样式是白字。"""
    W, H = doc["size"]
    frame_us = native_frame_us(doc)
    font = export.FONT_CN
    widths = export.ink_widths([(font, k) for k in layout.EM_KEYS] + [(font, "\n".join(layout.width_keys(it.lines, it.kind == "block"))) for it in items])

    def size_of(it: Item) -> int:
        if it.kind == "block":
            return 0
        if body_text(it):
            tb = it.textbox or {}
            return layout.body_size(view_rows(it, H), it.lines, font, widths, tb.get("em"), tb.get("box_w"), W, it.le)
        return layout.fit_line(it.rows, it.lines, font, widths, it.translated, W)
    sizes = [size_of(it) for it in items]
    choice = [i for i, it in enumerate(items) if style_of(it) in ("Choice", "Offmain")]   # 按样式选：阶段 4 按样式推种类（`KIND_OF_STYLE`），两边一致
    for i, s in zip(choice, layout.same_size([(items[i].start_us, items[i].end_us, items[i].rows[0]) for i in choice],
                                             [sizes[i] for i in choice])):
        sizes[i] = s
    axes = layout.stacked_axes([(it.start_us, it.end_us, it.rows[0]) if stacks(it) else None for it in items],
                               [max((layout.line_width(font, ln, widths) * sz for ln in it.lines), default=0.0) if stacks(it) else 0.0
                                for it, sz in zip(items, sizes)], W)
    lines_out, n = [], Counter()
    for it, size, axis in zip(items, sizes, axes):
        n[it.kind] += 1
        got = "\n".join(styled_lines(it, it.lines) or ()) if it.kind != "block" else ""
        n["styled"] += richtext.select(got, ruby=False) != richtext.plain(got)
        n["ruby"] += len(richtext.ruby_spans(got))
        tw_us, fin, fout, rowt = item_effects(it, doc, frame_us, typewriter, fade)
        n["typewriter"] += tw_us > 0
        n["fade"] += bool(fin or fout)
        if it.kind != "block":
            n[f"align_{it.align}"] += 1
        n["rowt"] += rowt is not None
        # 有逐行时刻时，底板的原文那部分也逐行起盖：grow 按整条的时长夹（一行都不打字时 tw 是 0）
        # 正文译文的原文那部分按原文整块逐行长（`fo` 的 `box` 写的是整块，`grow` 和它一行对一行），每行的早出现证据是 `block_leads`
        grow = (grow_times(replace(it, rows=src_block(it), row_leads=it.block_leads) if body_text(it) else it, doc,
                           tw_us if rowt is None else max(1, it.end_us - it.start_us))
                if it.kind != "block" else None)
        n["stacked"] += axis is not None
        lines_out.append(draft_line(it, size, W, H, widths, tw_us, fin, fout,
                                    effect_why(it, doc, tw_us, fin, fout) if why else None, font, grow, rowt, axis))
    head = {"FlowOCR Script": FORMAT_VERSION, **(info or {})}
    Path(path).write_text(export.ass_doc(W, H, style_lines(font, fills), lines_out, info=head), encoding="utf-8")
    return n, widths


KEEPS = ("main", "all")
"""tracks 产物取哪些条目：`main` 主轨 + 名牌轨，`all` 全部区域轨。匹配产物没有这个选项，改按组选（`matched_visible`）。"""
MATCHED_GONE = ("keep", "layers")
"""匹配产物上去掉的选项（显示协议，owner 2026-10-07）：画哪些按匹配器声明的组和 `show` / `hide` 算，传了这两个就报错。"""


def matched_visible(document: dict, options: dict | None) -> matchedio.Visible:
    """匹配产物这次渲染的可见集合：`show` / `hide`（逗号分隔）交给 `matchedio.visible`，`MATCHED_GONE` 传了就报 `ValueError`。
    所有吃匹配产物的内置预设都过这一处（叠加、`script`、`default_srt` 经 `build_items`，`matched_srt` 直接调）。"""
    o = options or {}
    gone = [k for k in MATCHED_GONE if k in o]
    if gone:
        raise ValueError(f"匹配产物没有 {' / '.join(gone)} 选项了：画哪些改用 show / hide 按组选"
                         f"（这份产物的组：{', '.join(document['display']['groups'])}；另有 fill = 填位、all = 全部组加填位）")
    return matchedio.visible(document, o.get("show"), o.get("hide"))


def visible_note(document: dict, vis: matchedio.Visible) -> str:
    """可见集合写给人看的一句（字幕稿的 `FlowOCR Note`、渲染时的打印）：可见的组按产物声明的顺序，填位画不画。"""
    groups = [g for g in document["display"]["groups"] if g in vis.groups]
    return f"可见的组 {','.join(groups) or '（无）'}，填位{'画' if vis.fill else '不画'}"


def events_fingerprint(doc: dict) -> str:
    """tracks 的 `events[]` 的内容指纹（sha256 前 12 位）。字幕稿里的 `ev` 是 `events[]` 的下标，重跑阶段 2 会重新编号——
    以后要按 `ev` 回到产物（合并、回流），先核字幕稿记的这个指纹和手上的产物对不对得上。"""
    blob = json.dumps(doc["events"], sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


def build_items(document: dict, options: dict | None, align: bool = True,
                marks: placeholders.Marks | None = None) -> tuple[list[Item], dict, str, str, str]:
    """按预设的选项构造条目，不写文件：返回 `(条目, tracks 产物, 文件名前缀 base, 说明, 来源)`。
    matched 译文里的占位标成 `` [`编号:值] ``；要图例、要打提示就传一个 `placeholders.Marks(document.get("fills"), lang)` 进来，用完它记着编号。`render_script` 写字幕稿、
    `default_srt` 写 SRT 都走这里，所以两者一条对一条。`align=False` 不查 `regions[].align`、也不查 matched 的文本框画像（`need_textbox`）：SRT 用不到对齐和摆行，较早的产物照样能出。

    options（字符串，`render --opt` 原样给的）：
    * tracks：`keep`（`main` 默认 / `all`）；
    * matched：`show` / `hide`（逗号分隔的组名、`fill`，`show` 另认 `all`；显示协议，`matched_visible`）、`lang`（jp 默认 / cn / en）、
      `tracks`（产它的那份 `*-tracks.json`，默认按 `provenance.subs` 找）、`style`（`on` 默认 / `off`：剧本里的粗体 / 斜体 / 下划线 /
      删除线 / 字色带不带，`STYLE_MODES`）、`ruby`（`on` 默认 / `off`：剧本里的 Ruby 写不写成标记，`RUBY_MODES`）——tracks 上没有剧本，
      这两个取值照核、不起作用。`keep` / `layers` 传了就报错。

    matched 的条目：可见、有 `lang` 文本的叠加条目（`project_items`），再加 OCR 原文（`ocr_items`）——
    `unmatched` 组可见时画 `matchedio.fed_rest` 给的 unmatched 事件和**可见、但没有 `lang` 文本**的条目的事件（已被画出的条目用到的除外），
    `unfed` 组可见时画 unfed 事件；正文条目原文整块借来的事件（`borrowed`，在那个条目的底板下）两组都不画。退回画的名牌（名牌层的事件、带建轨名牌标的事件）一律是 `stray_name`。"""
    o = dict(options or {})
    style = str(o.get("style", "on"))
    if style not in STYLE_MODES:
        raise ValueError(f"style 只能是 {' / '.join(STYLE_MODES)}：{style!r}")
    ruby = str(o.get("ruby", "on"))
    if ruby not in RUBY_MODES:
        raise ValueError(f"ruby 只能是 {' / '.join(RUBY_MODES)}：{ruby!r}")
    if document.get("schema") == matchedio.SCHEMA:
        matchedio.validate(document, need_overlay=True)
        vis = matched_visible(document, o)
        lang = str(o.get("lang", "jp"))
        matchedio.need_lang(document, lang)
        tracks = str(o.get("tracks") or matchedio.tracks_path(document))
        if not tracks or not Path(tracks).is_file():
            raise FileNotFoundError(
                f"找不到这份匹配产物对应的 tracks（provenance.subs = {matchedio.tracks_path(document)!r}）；"
                f"叠加和 SRT 都要它的事件和时刻，用 options['tracks'] 指过去")
        doc = tracksio.load(Path(tracks))
        if align:
            need_align(doc, tracks)
            need_textbox(document)
        drawn: set[int] = set()
        bare: set[int] = set()
        marks = marks if marks is not None else placeholders.Marks(document.get("fills"), lang)
        latin = None
        if lang == "en":         # 英文按字形的真实宽折行：量一次可打印 ASCII 的步进宽（`layout.latin_rel`）
            latin = layout.latin_rel(export.FONT_CN, export.ink_widths([(export.FONT_CN, "\n".join([*layout.EM_KEYS, *layout.latin_keys()]))]))
            if not latin:        # 条目上的 `le`（按拉丁字定字号、在空格处折）跟着它有没有：量不出就别悄悄按中文画
                raise RuntimeError(f"量不出叠加字体 {export.FONT_CN} 的拉丁字宽，出不了英文")
        items = project_items(document["overlay"]["items"], doc, lang, vis, marks, drawn, bare, style == "on", ruby == "on",
                              document["overlay"].get("textbox"), latin)
        unmatched, unfed = matchedio.fed_rest(document, doc)
        # 正文原文整块借来的事件在 `drawn` 里（它们在那个条目的底板下），unmatched / unfed 都不再补画
        raw = {"unmatched": [i for i in unmatched if i not in drawn] + sorted(bare - drawn), "unfed": [i for i in unfed if i not in drawn]}
        ocr = {i for g, ids in raw.items() if g in vis.groups for i in ids}
        note = f"剧本译文 {lang}，{visible_note(document, vis)}"
        if ocr:
            # 退回画 OCR 原文的名牌一律 `stray_name`：`Name` / `Title` 只给挂在台词上、认出了译名的（匹配成功的真名牌，owner 2026-11-05）。
            # 名牌层的事件不看 tracks 的 kind——那是建轨的 `nameplate` 标，匹配器认的开头行多半没有，会以 `Body` 出现；
            # 挂上但没译出的也不算：《魔裁》里多是 `19:39`、`NO DATA`、`そう。` 这类（2026-11-05 量，f1–f5 默认叠加共 124 块，也有读错的真名字 `階堂匕口`）
            heads = {i for it in document["overlay"]["items"] if it["layer"] in ("name", "title") for i in it["events"]}
            for x in ocr_items(doc, ocr, [it for it in items if it.kind == "block"]):
                if x.kind == "name" or x.events[0]["id"] in heads:
                    x.kind = matchedio.STRAY_NAME
                items.append(x)
            note += "，另画 OCR 原文：" + "、".join(f"{g} {len(ids)} 个事件" for g, ids in raw.items() if g in vis.groups)
        tag = doc.get("tag") or Path(tracks).stem.removesuffix("-refined").removesuffix("-tracks")
        return items, doc, f"{tag}-{lang}", note, f"{tag} {matchedio.SCHEMA}"
    bad = [k for k in ("show", "hide", "layers") if k in o]
    if bad:
        raise ValueError(f"tracks 产物没有 {' / '.join(bad)} 选项（show / hide 按匹配器声明的组选，只对匹配产物）；取哪些用 keep=main|all")
    keep = str(o.get("keep", "main"))
    if keep not in KEEPS:
        raise ValueError(f"keep 只能取 {list(KEEPS)}：{keep!r}")
    doc = document
    if align:
        need_align(doc, str(doc.get("tag") or "这份 tracks"))
    base = str(doc.get("tag") or "tracks")
    note = f"OCR 原文，{'主轨 + 名牌' if keep == 'main' else '全部区域轨'}"
    return mark_names(tracks_items(doc, keep)), doc, base, note, f"{base} {tracksio.SCHEMA}"


OVERWRITE = contextvars.ContextVar("flowocr_script_overwrite", default=True)
"""已有的字幕稿能不能覆盖。库调用（驱动、守卫）默认能；`flowocr-render` 不给 `--force` 时设成不能：
字幕稿是人改的那一层，重跑第 3 步把改过的稿悄悄冲掉的代价太大（owner 2026-11-11）。"""


class ScriptExists(FileExistsError):
    """`OVERWRITE` 关着时要写的字幕稿已经在：一个文件都还没写。"""


def render_script(document: dict, output_dir, options: dict | None, suffix: str = ""
                  ) -> tuple[Path, Counter, list[Item], dict]:
    """阶段 3 的实现：吃 tracks（叠 OCR 原文）或 matched（叠剧本译文），写一份字幕稿。

    options（字符串，`render --opt` 原样给的）：
    * `keep`（tracks）/ `show` / `hide` / `lang` / `tracks` / `style` / `ruby`（matched）：取哪些条目、带不带剧本的样式和 Ruby，见 `build_items`；
    * `why`：`on` 把效果判定的依据写进每行的 `fo`（dev 审计用）；
    * `typewriter`（off / on / dim，dim 在这里等于 on）、`fade`（off / on）：off 就不判、不写；
    * `name`：文件名（默认 `<tag>[-<lang>]-<suffix>.script.ass`；叠加预设给的 `suffix` 是预设名，不给时 tracks 是 `keep`，
      matched 是 `all`（`show` 里有 `all`）或 `main`）。

    字幕稿已经在、`OVERWRITE` 又关着时报 `ScriptExists`，什么都不写（叠加预设先写字幕稿，所以最终 ASS 也不动）。"""
    o = dict(options or {})
    if not suffix:
        suffix = (("all" if "all" in matchedio.names(o.get("show")) else "main") if document.get("schema") == matchedio.SCHEMA
                  else str(o.get("keep", "main")))
    typewriter, fade = effect_modes(o)
    why = str(o.get("why", "off"))
    if why not in ("off", "on"):
        raise ValueError(f"why 只能是 off / on：{why!r}")
    marks = placeholders.Marks(document.get("fills"), str(o.get("lang", "jp")))
    items, doc, base, note, source = build_items(document, o, marks=marks)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    p = out / str(o.get("name") or f"{base}-{suffix}.script.ass")
    if p.exists() and not OVERWRITE.get():
        raise ScriptExists(f"{p} 已经在（可能是改过的字幕稿），不覆盖：要重出就加 --force（改过的先另存一份），或用 --outdir 写到别处")
    info = {"FlowOCR Source": f"{source} events-sha256:{events_fingerprint(doc)}",
            "FlowOCR Code": provenance.git_head(), "FlowOCR Note": note, **marks.legend()}
    if (doc.get("provenance") or {}).get("video"):
        info["FlowOCR Video"] = doc["provenance"]["video"]          # 阶段 4 的底板取色读它（已是 portable 的写法）
    n, widths = export_script(p, items, doc, typewriter, fade, why == "on", info, name_fills(document))
    kinds = "、".join(f"{k} {n[k]}" for k in ("line", "name", "title", "plain_name", "stray_name", "choice", "offmain", "block") if n[k])
    aligns = "、".join(f"{a} {n['align_' + a]}" for a in tracksio.ALIGNS if n["align_" + a])
    styled = (f"、剧本样式 {n['styled']} 条" if n["styled"] else "") + (f"、Ruby {n['ruby']} 处" if n["ruby"] else "") \
        + (f"、称号跟名字对齐 {n['stacked']} 条" if n["stacked"] else "")
    print(f"-> {p}（字幕稿，{len(items)} 条：{kinds}；对齐 {aligns}；打字机 {n['typewriter']} 条、淡入淡出 {n['fade']} 条{styled}；{note}）")
    from flowocr.output import timing_check             # 它 import 本模块，放这里免得循环
    sus = timing_check.check(items, doc, typewriter, fade)
    n["timing_suspects"] = len(sus)
    if sus:
        # 选项照这次实际生效的给（预设会替用户加 show=all / keep=all），照抄就是同一组条目
        opts = "".join(f" --opt {subprocess.list2cmdline([f'{k}={o[k]}'])}" for k in timing_check.OPTS if k in o)
        print(f"⚠ {timing_check.report(sus, timing_check.span_hours(items), show=0).splitlines()[0]}"
              f"——逐条看：{paths.python_cmd()} -m flowocr.output.timing_check <这份产物>{opts}")
    marks.say("字幕稿")
    return p, n, items, widths


# ---- 阶段 3 的 SRT（`default_srt`）----

SRT_STYLES = ("Body", "Extra", "Choice", "Offmain")
"""进 SRT 的条目（按字幕稿的样式）：正文。名牌、常驻 UI、阅读面板不进（owner 2026-10-05：SRT 是轻量、符合直觉的主轨 sidecar），
特殊上屏形式的台词与提示（`Special`）也不进（owner 2026-11-07），`show=all` 时也不进。
占位的编号（`placeholders.Marks`）按投影顺序分，不进 SRT 的条目也占号：这些条目里有占位时 SRT 的编号会跳号（名牌、面板同样）。
`special` 组的条目没有所选语种文本、又开着 `unmatched` 时，退回画的 OCR 原文（`build_items` 的 `bare`）是 tracks 的画法、没有组，样式是 `Body`、会进这里的 SRT；
现有文本包里这几类行日文、中文都齐（0 行缺），碰不到，所以只记在这里（`matched_srt` 那边判在可见性这一步，不退回）。"""


def srt_text(it: Item) -> list[str]:
    """一个条目在 SRT 里的正文，一行一行（SRT 的标准换行）：tracks 的条目是拼好的可见行（事件本身带的换行也拆开）；
    matched 的译文出剧本原句、按剧本自带的换行分行（叠加按文本框画像折过的行 `wrap_lines` 不用：SRT 不画在文本框里）。
    空行剔掉：SRT 按空行分块，正文里一个空行就把这条截断。SRT 是基础输出，不带剧本样式（`Item.styled` 不用，owner 2026-10-23）。"""
    raw = it.source if it.translated and it.source is not None else "\n".join(it.lines)
    return [ln.strip() for ln in raw.replace("\\N", "\n").replace("\\n", "\n").split("\n") if ln.strip()]


def srt_start_full(it: Item) -> int:
    """`start=full` 的起点：进条目的正文行（不含半截行）全字出现的最晚时刻，夹在条目里（同 `export.cue_start` 的理由：
    取最早的会退回首字出现）；没有全字时刻的照首字。"""
    fulls = [e["t_full"] for e in it.events if e.get("t_full") is not None]
    return max(it.start_us, min(max(fulls), it.end_us - 1)) if fulls else it.start_us


def srt_blocks(items: list[Item], text=srt_text, start: str = "first", echo: str = "drop") -> list[tuple[int, int, list[str]]]:
    """一批条目 → SRT 的块：一个正文条目（`SRT_STYLES`）一条 cue，起止是条目的起止（`start="full"` 换成全字出现）。
    matched 的同时回显 / 记录面板回显（`matchedio.SRT_DROP_ECHOES`）默认不出：用途 2 要一句一条（owner 2026-10-06）；`echo="keep"` 放回来，叠加照画。
    只在**主那次认领确实有条目出进这份 SRT** 时才去（同 `matchedio.record_kept` 的思路）：主可能是一条没有叠加条目的 `extra`
    （事件全被主认领占了），或它不可见（显示协议，`build_items`），这时去掉回显这一句就一次都不出——去回显叠在可见性之后。
    跨句常驻的（`spans`：事件挂在这条轨的几条 cue 里）不出——没打名牌标的名牌 / 称号、轮换的横幅、日志面板这类挂一整场的行，
    在 SRT 里会成一条叠在句子上的长 cue（21 臂里 48 条、最长 89.5 s）；只对同句合并过的产物标（`tracks_items` 的 `joined`）；判据只看结构（挂在几条 cue 里），不看文本长短，
    字幕稿和叠加照画（owner 2026-10-05）。`text` 是可换的规则（`Item -> 行`），用户预设可以换成拼成一行、加说话人前缀之类。
    其余时间重叠的条目就是重叠的 cue，不合并。"""
    matchedio.echo_kept({}, echo)                    # 核取值
    shown = [(it, lines) for it in sorted(items, key=lambda x: (x.start_us, x.end_us))
             if style_of(it) in SRT_STYLES and not it.spans and (lines := text(it))]
    present = {tuple(it.claim) for it, _ in shown if it.claim}
    return [(srt_start_full(it) if start == "full" else it.start_us, it.end_us, lines) for it, lines in shown
            if matchedio.echo_kept({"echo": it.echo}, echo) or not (it.echo_of and tuple(it.echo_of) in present)]
