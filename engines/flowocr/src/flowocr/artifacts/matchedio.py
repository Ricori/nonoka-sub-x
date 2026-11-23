"""`*-matched.json`（阶段 2 的匹配产物）的读与校验，**全仓库只有这一份**。

和 `tracksio` 对 `*-tracks.json` 的做法一样：版本按**整串**比，不等就拒，不做向后兼容（docs/architecture/README.md「产物契约」）。
2026-09-22 从 `scriptmatch` 搬到 `artifacts`（Codex 审计 P2）：阶段 3 的输出预设要读这份产物，
而 `output` 只许依赖 `artifacts`——判断留在 `analyze`，**产物的格式属于 artifacts**。
`scriptmatch` 顶层 re-export 这里的名字，老调用方（`SM.load_matched` / `SM.SCHEMA` / `SM.body`）不变。

一份 matched 里有什么：`cues`（一条 cue 一条记录，`ref` 是认领的剧本行或 None；剧本给得出说话人时 `ref` 带
`speaker` / `speaker_cn`，旁白是 None——内置的 `naninovel` 匹配器给，字段名同 `gamescript` 的剧本行）、`stats`、`provenance`
（含 `subs`：产它的那份 `*-tracks.json`），以及可选的 `overlay`（叠加层判定，`--no-overlay` 时没有）
和 `fills`（匹配器猜的占位的值，`{占位: {"value": 值或 None, "by_lang": {"jp": …, "cn": …, "en"?: …}, "how": 从哪来, …}}`；没有占位、或匹配器不猜时没有）。
`cues[]` 的每条记录和它的每条 `extra` 都带 `echo` / `echo_of`（回显判定，`flowocr.analyze.echo`；见 `ECHOES`），
`overlay.items` 的 `body` / `extra` 条目带 `claim`（它是哪次认领，`[记录下标, slot]`）和那次认领的 `echo` / `echo_of`
（SRT 投影按它们去回显：主那次认领有条目要出时才去，见 `script.srt_blocks`；叠加照画）。
"""
from __future__ import annotations

import json
from pathlib import Path

SCHEMA = "flowocr-matched/8"
"""`*-matched.json` 的版本。**写它的人必须有个读它的人**（2026-09-15 审计）：
`/1` → `/2` 升过一次而全仓库没有一处读这个字符串，等于没有版本控制——
旧产物喂给期待 `overlay` 的代码只会得到 `KeyError`，而不是"这是旧 schema"。
/3（2026-10-05）：剧本行的 `cn` 和新加的 `jp_tpl`（日文原文模板，有占位时才有）留着占位（`{NICKNAME}` 这类花括号），
顶层可选的 `fills` 是匹配器猜的占位的值（`flowocr.placeholders`）；`jp` 照旧是删了占位、匹配用的原文。
/4（2026-10-06）：`cues[]` 的记录和 `extra` 多了 `echo` / `echo_of`（`ECHOES`）。
/5（2026-10-07）：游戏剧本的认领（`ref` / `extra[]`）的 `kind` 只放剧本行的类型，这段视频里的处境另放 `status`
（`gamescript.STATUSES`；之前直接写进 `kind`）。顺序剧本的认领没有 `status`。
/6（2026-10-07）：显示协议——顶层 `display`、每次认领和每个叠加条目上的 `group` / `fill`、`overlay.fed`（`_check_display` 的说明）。
/6 之内加的**可选**字段（2026-10-23）：认领和叠加条目的 `jp_rich` / `cn_rich`，带样式的原文模板 / 译文（`flowocr.richtext` 的规范写法，
和 `jp_tpl or jp` / `cn` 只差标签，不同时才有）。读的一方缺了就是没有样式，所以没升版本。
/7（2026-11-05）：名牌逐块分组——挂在认上的台词上的留在 `name` 组，没挂上的归 `stray_name`（`NAME_GROUPS`）；
挂上的称号另成一层 `title`（`LAYERS`）。读 /6 的下游会把称号当未知的层、把 `stray_name` 当没声明的组，所以升版本。
/7 之内加的**可选**字段（2026-11-05）：名牌条目的 `ink`（判过字色：和这款游戏的名牌色对不对得上，`analyze.nameink`）、
`overlay.name_ink`（判过时各层的名牌色 `#rrggbb`）。读的一方缺了就是没判、照白字画，所以没升版本。
/8（2026-11-10，line-breaks 计划）：叠加按原文整块摆行——`overlay.textbox`（区域号 → 文本框画像）和 `body` / `extra` 条目的
`block` / `block_src` / `block_drawn_by`（`analyze.textbox`）。阶段 3 出叠加时缺了就报错（`output.script.need_textbox`），所以升版本；
/7 整个拒收（这里按整串比），提示重跑匹配。"""

TEXT_KEYS = ("jp", "jp_tpl", "cn", "jp_rich", "cn_rich", "en", "en_rich")
"""剧本行带进认领和叠加条目的文本字段（`text_of`）。`en` / `en_rich` 是可选的（剧本带英文时才有，2026-11-10 en-target 计划），
读的一方缺了照常工作，所以没升版本；要出英文时 `need_lang` 核一遍产物里有没有。"""
LANGS = ("jp", "cn", "en")
"""输出能选的语种（`lang`）：日文原文、中文译文、英文译文。"""


def need_lang(doc: dict, lang: str) -> None:
    """`lang` 不认得、或产物里一条这个语种的文本都没有（`en`：匹配器 / 剧本不带英文，或是加英文之前跑的匹配）就报错，
    别让整份叠加悄悄退回 OCR 原文。认领的剧本行、叠加条目和阅读面板（`paras`，每段都有才算，同 `script` 的面板投影）都算。"""
    if lang not in LANGS:
        raise ValueError(f"lang 只能是 {' / '.join(LANGS)}：{lang!r}")
    if lang == "jp":
        return
    has = any(c.get("ref") and c["ref"].get(lang) for c in doc.get("cues") or ()) \
        or any(it.get(lang) or (it.get("paras") or {}).get(lang) and None not in it["paras"][lang]
               for it in (doc.get("overlay") or {}).get("items") or ())
    if not has:
        raise ValueError(f"这份匹配产物里没有 {lang} 文本：匹配器或剧本不带这个语种"
                         + ("；游戏文本包匹配器：剧本是加英文之前做的、或做剧本的文本包没有英文那份，用带英文的包重跑 `flowocr-gamescript`，"
                            "再从轨道产物重跑匹配（不用重做 OCR）" if lang == "en" else ""))


ECHOES = ("same_time", "log", "replay", "other")
"""回显判定的形态（`flowocr.analyze.echo`，matcher-next 计划第 1 节）：同一句剧本行被认领了几次时定一次主，其余的
`echo` 是下面之一、`echo_of` 是主那次认领 `[记录下标, slot]`（`cues[]` 的下标；slot −1 是那条记录的 `ref`，≥0 是它的第几条 `extra`）；
主和只认领一次的是 None。

* `same_time`：和主同一次出现（时段重叠或相隔 ≤ 2 s）、在另一个区域的同一句（同一区域的落到 `other`）；
* `log`：对话记录这类面板里的旧台词：匹配器认出的真记录面板（`scriptmatch.log_panels`，不论和主隔多久）；或别的时刻、cue 面板样；或在另一区域、这一刻成批认领了早就认过的别的行；
* `replay`：别的时刻、和主同一区域、不是面板样——真实的重演（剧情回放、选错后重来、快速回看），不当错；
* `other`：剩下的（另一区域、隔开、不成批；以及同一次出现里和主同区域相接、`merge_runs` 没并上的那一截），报警留给人看。"""
SRT_DROP_ECHOES = ("same_time", "log")
"""SRT 一类预设默认不出的形态（owner 2026-10-06）：用途 2 要一句一条。预设的 `echo=keep` 放回来；叠加预设照画。"""
ECHO_MODES = ("drop", "keep")


def echo_kept(x: dict, mode: str = "drop") -> bool:
    """只看形态：`mode="drop"`（默认）时 `SRT_DROP_ECHOES` 是 False，`keep` 全是 True；`mode` 取值不对就抛。
    要不要真去掉还得看主那次认领出不出（`record_kept`、`script.srt_blocks`）。"""
    if mode not in ECHO_MODES:
        raise ValueError(f"echo 只能是 {' / '.join(ECHO_MODES)}：{mode!r}")
    return mode == "keep" or x.get("echo") not in SRT_DROP_ECHOES


def record_kept(rec: dict, mode: str = "drop") -> bool:
    """拍平的 SRT（`matched_srt`：一条记录一条 cue，只出主认领 `ref`）出不出这条记录：同 `echo_kept`，但只在**主是某条记录的 `ref`** 时才去——
    主落在别的记录的 `extra` 上时，那一句在这份 SRT 里只能靠这条出，去掉就一次都没有了。"""
    of = rec.get("echo_of")
    return echo_kept(rec, mode) or not (of and of[1] == -1)


class MatchedSchemaError(Exception):
    """`*-matched.json` 不是当前版本 / 缺必需字段。"""


REQUIRED = ("schema", "provenance", "stats", "cues", "display")
"""每份 matched.json 都要有的顶层键。`overlay` 不在里头——`--no-overlay` 和非 tracks 输入都没有它，
要用它的调用方自己检查（`load(..., need_overlay=True)`）。"""

LAYERS = ("body", "extra", "name", "title", "choice", "offmain", "panel")
"""`overlay.items` 的层（owner 2026-09-14，product-goals 第 16 条）：判定（`scriptmatch.overlay_items`）写进 JSON。
层只管**怎么画**（盖在正文行上、独立的块、名牌、阅读面板）；画不画看条目的组（显示协议，`visible`）。属于产物契约，所以放这里——判断和投影两边都读它。

* `body`：主认领那条剧本行，盖在 cue 的正文行上（`body_of`）；
* `extra`：同一条 cue 装着的第二条剧本行，盖在**它自己的**那几行上——不并进正文、不拆 cue；
* `name`：名牌（`game_align.head_lines` 认出的开头行），译文先取剧本说话人的中文名（`speaker_names`），
  查不到再按字形查文本包的短名词表（`term_names`），再查不到的按名牌下面那几条记录的说话人投票、或借本段已译出且只差一个字的名牌（`fuzzy_names`）；
  条目的 `name_src` 记是哪一种（`speakers` / `terms` / `voted` / `fuzzy` / 空）；`overlay.stats.name_translated` 四种来源都算，`name_from_terms` / `name_from_voted` / `name_from_fuzzy` 单列；
  一个事件一块，在动的事件按位置切成几块（`event_segments`）；挂在认上的台词上的归 `name` 组，没挂上的归 `stray_name`（`scriptmatch.name_roles`）；
* `title`：名牌下面（或上面）字小一号的称号（原神的「傭兵」），和 `name` 同一种条目、同样来源，只是字幕稿里另用 `Title` 样式；
  只在挂上的名牌里分（`scriptmatch.name_roles`），没挂上的都留在 `name` 层；
* `choice`：主轨外的选项按钮（`choice_items` 里选项类的剧本行，`scriptmatch.choice_like`）；
* `offmain`：主轨外、逐字等于剧本行的其余文字（台词落到了别的区域、`Loose` 散条目…，`choice_layer`）；
* `panel`：阅读面板长文本（`readable_items`，选开，要文本包有 `readables` 表，`--readables` + `--obs`）。"""


def load(path: Path, need_overlay: bool = False) -> dict:
    """读 `*-matched.json`，**校验版本和必需字段**，对不上就抛 `MatchedSchemaError`。

    `need_overlay=True` 时再要求有 `overlay.items`，并把"这份是不是 `--no-overlay` 产的"说清楚。"""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    validate(doc, need_overlay, str(path))
    return doc


def validate(doc: dict, need_overlay: bool = False, where: str = "<doc>") -> None:
    """同 `load` 的校验，给已经读进来的 dict 用（阶段 3 的预设拿到的就是 dict）。"""
    got = doc.get("schema")
    if got != SCHEMA:
        raise MatchedSchemaError(
            f"{where} 的 schema 是 {got!r}，当前是 {SCHEMA!r}——重跑匹配生成，不做兼容")
    missing = [k for k in REQUIRED if k not in doc]
    if missing:
        raise MatchedSchemaError(f"{where} 缺顶层键 {missing}")
    if need_overlay and (doc.get("overlay") or {}).get("items") is None:
        raise MatchedSchemaError(
            f"{where} 没有 overlay.items（`--no-overlay` 产的，或匹配时 --subs 不是 *-tracks.json）")
    _check_display(doc, where)


# ---- 显示协议（miss-detector 计划「显示协议」，owner 2026-10-06 / 10-07）----

TIERS = ("always", "on", "off")
"""组的档位：`always` 直接进 default、关不掉；`on` 默认进、`hide` 关得掉；`off` 默认不进、`show` 打得开。"""
RESERVED = ("unmatched", "unfed")
"""每个匹配器都要声明的保留组，内容**由定义推出、不写进产物**（`fed_rest`）：
* `unmatched`：喂给了匹配器、但没画成译文的文字——`cues[]` 里是 `ref` 为空的记录；叠加里是 `overlay.fed` 的事件中没被任何叠加条目引用的；
* `unfed`：没喂给匹配器的文字——叠加里是 tracks 中不在 `overlay.fed`、也没被任何叠加条目引用的事件；`cues[]` 里没有这一组。"""
STRAY_NAME = "stray_name"
"""没挂在认上的台词上的名牌块归的组（`scriptmatch.name_roles`），默认不画。"""
NAME_GROUPS = {
    "name": {"tier": "on", "label": "名牌（挂在认上的台词上，含称号）"},
    STRAY_NAME: {"tier": "off", "label": "没挂在认上的台词上的名牌样文字（对话记录面板、队伍栏、头顶名字…）"}}
"""名牌层的两个组（owner 2026-11-05），两个内置匹配器（`gametext`、`naninovel`）的显示声明都带。"""
SPECIAL = "special"
"""特殊上屏形式的台词与提示（横幅、战斗喊话、白字提示…，`scriptmatch.group_of`）归的组（owner 2026-11-07）：
阶段 3 的字幕稿用单独的样式 `Special`（`output.script.style_of`），两个 SRT 预设都不出。组仍由匹配器声明，没有这一组的匹配器照常。"""
KEYWORDS = ("all", "fill")
"""`show` / `hide` 的关键字，不能当组名：`all` 是全部组加填位，`fill` 是填位。"""



class Visible:
    """一次渲染的可见集合：`groups` 是可见的组名，`fill` 是填位画不画。`shows(x)` 判一次认领 / 一个叠加条目画不画；
    `unmatched` / `unfed` 两个保留组可不可见看 `groups`。"""

    def __init__(self, groups: frozenset[str], fill: bool):
        self.groups, self.fill = groups, fill

    def shows(self, x: dict) -> bool:
        return x["group"] in self.groups and (not x.get("fill") or self.fill)

    def __repr__(self) -> str:
        return f"Visible({sorted(self.groups)}, fill={self.fill})"


def names(value) -> list[str]:
    """`show` / `hide` 选项的值（逗号分隔的字符串，或已经拆好的列表）→ 名字列表。"""
    if not value:
        return []
    parts = value.split(",") if isinstance(value, str) else list(value)
    return [x.strip() for x in parts if x.strip()]


def visible(doc: dict, show=None, hide=None) -> Visible:
    """按产物的 `display` 和用户的 `show` / `hide` 算可见集合。`show=all` 打开全部组和填位；
    点了没声明的名字、`hide` 一个 `always` 组、`hide=all`、同一个名字既 show 又 hide，都抛 `ValueError`。"""
    disp = doc["display"]
    groups = disp["groups"]
    sh, hd = names(show), names(hide)
    known = set(groups) | set(KEYWORDS)
    bad = [x for x in sh + hd if x not in known]
    if bad:
        raise ValueError(f"show / hide 里有没声明的名字 {bad}；这份产物的组：{', '.join(groups)}（另有 fill、all）")
    if "all" in hd:
        raise ValueError("hide 不能是 all")
    both = set(sh) & set(hd)
    if both:
        raise ValueError(f"同一个名字既 show 又 hide：{sorted(both)}")
    fixed = [x for x in hd if x in groups and groups[x]["tier"] == "always"]
    if fixed:
        raise ValueError(f"组 {fixed} 是 always（直接进 default），关不掉")
    if "all" in sh:
        vis, fill = set(groups), True
    else:
        vis = {g for g, d in groups.items() if d["tier"] != "off"}
        fill = disp["fill"]["tier"] == "on"
        vis |= {x for x in sh if x in groups}
        fill = fill or "fill" in sh
    vis -= set(hd)
    fill = fill and "fill" not in hd
    return Visible(frozenset(vis), fill)


def fed_rest(doc: dict, tracks: dict) -> tuple[list[int], list[int]]:
    """两个保留组在叠加里的事件（升序事件 id）：`(unmatched, unfed)`。`tracks` 是这份产物对应的 `*-tracks.json`。
    检测器的"文本包外的字"清单和预设的补画用这同一个函数。"""
    used = {i for it in doc["overlay"]["items"] for i in it.get("events") or ()}
    fed = set(doc["overlay"]["fed"])
    ids = [e["id"] for e in tracks["events"]]
    return ([i for i in ids if i in fed and i not in used], [i for i in ids if i not in fed and i not in used])


def claims(doc: dict):
    """产物里的每次认领：`(记录下标, slot, 认领)`，slot −1 是 `ref`、≥0 是第几条 `extra`（同 `echo_of`）。"""
    for ri, r in enumerate(doc["cues"]):
        if r.get("ref"):
            yield ri, -1, r["ref"]
        for xi, x in enumerate(r.get("extra") or ()):
            yield ri, xi, x


def _check_display(doc: dict, where: str) -> None:
    """产物里的显示协议，`validate` 调它核：

    * 顶层 `display`：`{"groups": {组名: {"tier": TIERS 之一, "label": 给人看的名字}}, "fill": {"tier": "on" | "off"}}`；
      组名由匹配器起，`RESERVED` 两个必须在，`KEYWORDS` 不能用。
    * 每次认领（`cues[].ref`、`cues[].extra[]`）带 `group`（声明过、不是保留组）和 `fill`（bool：认领了、但 OCR 没读到这句）。
      `ref` 为空的记录不带，它归 `unmatched`。
    * 每个叠加条目（`overlay.items[]`）带 `group`（声明过、不是保留组）和 `fill`；`body` / `extra` 条目的这两个值与它 `claim` 指向的认领相同。
    * `overlay.fed`：喂给匹配器的 cue 用到的事件 id（升序），`unmatched` / `unfed` 由它推出。
    * 画不画（`Visible.shows`）：组可见，且不是填位、或填位可见。默认可见 = `always` + `on` 的组，填位按 `display.fill`。"""
    disp = doc.get("display")
    if not isinstance(disp, dict) or not isinstance(disp.get("groups"), dict) or not disp["groups"]:
        raise MatchedSchemaError(f"{where} 的 display.groups 不是非空的表")
    groups = disp["groups"]
    for g, d in groups.items():
        if g in KEYWORDS:
            raise MatchedSchemaError(f"{where} 的组名 {g!r} 是 show / hide 的关键字")
        if not isinstance(d, dict) or d.get("tier") not in TIERS or not isinstance(d.get("label"), str):
            raise MatchedSchemaError(f"{where} 的组 {g!r} 要有 tier（{' / '.join(TIERS)}）和 label：{d!r}")
    miss = [g for g in RESERVED if g not in groups]
    if miss:
        raise MatchedSchemaError(f"{where} 没声明保留组 {miss}")
    if (disp.get("fill") or {}).get("tier") not in ("on", "off"):
        raise MatchedSchemaError(f"{where} 的 display.fill.tier 只能是 on / off：{disp.get('fill')!r}")

    def own(x: dict, what: str) -> None:
        if x.get("group") not in groups or x["group"] in RESERVED:
            raise MatchedSchemaError(f"{where} 的{what}的 group {x.get('group')!r} 没声明或是保留组")
        if not isinstance(x.get("fill"), bool):
            raise MatchedSchemaError(f"{where} 的{what}没有布尔的 fill")
    claim = {}
    for ri, xi, c in claims(doc):
        own(c, f"认领 [{ri}, {xi}]")
        claim[(ri, xi)] = c
    ov = doc.get("overlay")
    if ov is None:
        return
    if not isinstance(ov.get("fed"), list):
        raise MatchedSchemaError(f"{where} 的 overlay 没有 fed（喂给匹配器的事件 id）")
    for n, it in enumerate(ov.get("items") or ()):
        own(it, f"叠加条目 {n}（{it.get('layer')}）")
        if it.get("layer") in ("body", "extra"):
            c = claim.get(tuple(it.get("claim") or ()))
            if c is None or (c["group"], c["fill"]) != (it["group"], it["fill"]):
                raise MatchedSchemaError(f"{where} 的叠加条目 {n} 的 group / fill 和它 claim 指向的认领对不上")


def text_of(x: dict, lang: str, rich: bool = False) -> str | None:
    """一条认领（`cues[].ref`）或叠加条目的 `lang` 正文：日文取留着占位的原文模板 `jp_tpl`（有占位时才有），没有就取 `jp`；中文取 `cn`。
    占位（`{NICKNAME}` 这类花括号）原样在里面，输出时由 `flowocr.placeholders.Marks` 标出来。
    `rich`：有带样式的那份（`<lang>_rich`）就取它（`flowocr.richtext` 的规范写法）。"""
    if rich and x.get(f"{lang}_rich"):
        return x[f"{lang}_rich"]
    return (x.get("jp_tpl") or x.get("jp")) if lang == "jp" else x.get(lang)


def body(rec: dict, lang: str, marks=None) -> list[str]:
    """导出字幕时这条 cue 的正文：匹配上、所选语种有文本就用剧本文本（`\\N` 是条内换行），否则原样留 OCR。
    出不出这条由调用方按显示协议先判（OCR 原文只在 `unmatched` 组可见时出，`matched_srt`）。
    `marks`（`flowocr.placeholders.Marks`）给了就把正文里的占位标成 `` [`编号:值] ``。SRT 是基础输出，不带剧本样式（owner 2026-10-23）。"""
    r = rec["ref"]
    t = text_of(r, lang) if r else None
    if t and marks is not None:
        t = marks.mark(t)
    return (t or rec["ocr"]).replace("\\N", "\n").split("\n")


def tracks_path(doc: dict) -> str:
    """产这份 matched 的 `*-tracks.json`（provenance 记的 `subs`）。叠加 ASS 要它的框和画面尺寸。"""
    return str((doc.get("provenance") or {}).get("subs") or "")
