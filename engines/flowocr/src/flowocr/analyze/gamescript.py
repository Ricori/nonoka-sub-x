"""游戏文本包（`gtd-bundle/1`，`flowocr.analyze.gtdbundle`）-> 一段视频的「剧本」。

存在的理由：游戏直播那批素材（`data/index.md` F 节）一直**没有原文剧本**，用途 2 的尺
（命中 / 三档漏条 / 重复认领）在那批上一个都用不了（gamestream-first-look 报告）。
文本包（`../game-text-data` 的 `gametext <游戏> bundle` 出的，一款游戏一个）收了原神 / 星铁 / 绝区零三款的解包文本，但它是**整个游戏**
（原神 34.5 万个对话节点），不是"这段视频播了什么"。所以要先圈出这段视频打过的对话，
这就是本工具做的事；量轨在 `flowocr.analyze.game_align`。

## 圈对话单元：只用 obs，不用任何一条臂的轨

单元 = 一段连续对话（原神 talk / 星铁一个 script / 绝区零一个 scene；原神语音字幕一条链一个）；
另有两类库里没有结构的：星铁全量台词表里没人引用的台词按 id 百位块成组，绝区零散条目按 key 里的
事件号成组（字幕带上的 `CN` / `TL` 按连号成组，`band_loose_slot`）、否则一条一个。
拿 obs 里**全部**的 OCR 文本（每个框、每一帧，去重）去整库做 n-gram 检索 + 包含度校验，
锚住的单元就是这段视频打过的对话；对齐顺序按锚点时间排到**行一级**（`sequence`），
因为一个 talk 可以拖一个小时（玩家中途去做别的）。

**为什么用 obs 而不是主轨**：`build_tracks` 的所有 A/B 臂共用同一份 obs，
从 obs 圈出来的分母对各臂**一模一样**；从某条臂的主轨圈，分母就偏向那条臂。

**这一步有选择偏差，报数时要连它一起报**：一段对话若在**任何**区域、任何一帧都没被读到，
它就不进分母——这种"整段漏掉"量不出来。它和 `script_align --span-ref`（由参照字幕圈区间）
是同一类代价；好在圈的判据是**全部区域的 OCR**，而被量的是**主轨**，两者差一层。

## 分支：用图结构判"没走到"，别让 gap 分桶去猜

原神 1.4 万个分支点，子节点 99% 是玩家选项（`TALK_ROLE_PLAYER`）；星铁选项之后是
`wait TalkSentence_<选项>` 引出的一段。选项文本走**选项 UI**，不进字幕带（类比《魔裁》的
`@choice`），记成 `Choice`；而**没选的那一支的 NPC 回复**如果留在分母里，会以 1–3 条的
短 gap 出现，全被算成"疑似真漏"。`script_align` 的 gap 分桶（≥5 条才算分支）接不住它。

所以按图算：分支点的每个子节点取**独占可达集**（只从它能到、兄弟到不了的节点），
独占集里有 obs 锚点 = 走过；同组有兄弟走过而它没有 = `unwalked`；同组谁都没锚点 =
`ambiguous`（多半是回复都很短、锚不住）。两类都**排出分母、单独报数**。
这同样依赖 obs，所以同样是各臂共用的分母。绝区零上游没有剧情分支（它自己的 README
"上游边界"），两位主角的两种说法在库里是相邻两行，按相邻相似度连成**变体组**、一组只算一个
分母条目（`load_zzz` / `fold_variants`）；它的对话选项库里判不出来，靠下面的位置判据。

## 字幕带：用位置判库里没有结构的

用途 2 的分母是**字幕带**上的条目。库的结构能判的（选项、黑屏旁白、横幅）上面已经排掉；判不了的
（绝区零的选项按钮、居中旁白、头顶气泡——上游既没分支也没说话人）靠 obs 框的位置：先从锚住的行
估出这段视频的字幕带（`band_of`，众数窗口），读到过而框**全在带外**的行记 `OffBand`（`mark_offband`）。
同样只用 obs、各臂共用。2026-09-11 整场绝区零的未命中里 208 条是这一类。

短行（内容不到 `--qmin` 字）本来永远没有锚点、也就没有位置证据；**单元圈中之后候选只剩几十行**，
于是在本单元的时段内按逐字相等补一遍（`short_anchors`），整场绝区零又有 44 条选项按钮因此判出带外。

## 文本清洗（只影响匹配，不改语料）

`{M#…}{F#…}` 按视频的主角性别二选一（**从 obs 投票定**，两种写法各算一遍、看哪种读得上）；原神按性别取的称谓 / 句尾
`{PLAYERAVATAR#SEXPRO[甲（为男时）|乙（为女时）]}` / `{MATEAVATAR#SEXPRO[…]}` 读包时查称谓表改写成同样的写法（`sexpro`，包里没有表才留成占位）；
`<color>` 等标签删掉（译文和原文模板另存一份带样式的，`clean(rich=True)`，见下）；`{RUBY#[S]…}` 是注音（小字在正文上方），删掉（带样式的那份留着，见下）；剩下的花括号是占位（`{NICKNAME}` / `{REALNAME[…]}` 这类，
屏幕上是玩家起的名字或运行时的值，库里没有；包里没有称谓表时 `{PLAYERAVATAR#…}` 也在其中）：用来匹配的原文里删掉、跟着它落单的标点一起收拾
（`flowocr.placeholders.drop`），译文和原文模板里原样留着（`clean(keep=True)`，阶段 3 标出来、阶段 4 填值或删掉）；`\\n` 是条内换行，写成 `\\N`（和《魔裁》剧本同一口径，`script_align` 认它）。

样式：粗体 / 斜体 / 下划线 / 删除线 / 字色和 Ruby（`flowocr.richtext`）在译文和原文模板之外另存一份 `cn_rich` / `jp_rich`（和不带样式的那份不同时才有），
阶段 3 按预设的 `style` / `ruby` 选项投成字幕稿的 ASS 标签和 Ruby 标记（SRT 不带）；字号、对齐和别的标签不带。匹配只看不带样式的那几份。
三款游戏的 Ruby 写法各不同，`ruby_rich` 先换成 Unity 的 `<ruby="读音">正文</ruby>`。

用法：
    python -m flowocr.analyze.gamescript out/gamestream/gi-s2.jsonl --out out/gametext/gi-s2-ref.json
    # 游戏按文件名前缀猜（gi/hsr/zzz），猜不出就要 --game；包默认在 paths.gametext_root()，--gametext 可指
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from itertools import zip_longest
from pathlib import Path

import numpy as np

from flowocr import paths  # noqa: E402
from flowocr import placeholders as PH  # noqa: E402
from flowocr import richtext  # noqa: E402
from flowocr import textfold  # noqa: E402
from flowocr.analyze import gtdbundle  # noqa: E402

CODE_FP_FILES = ("src/flowocr/analyze/gamescript.py", "src/flowocr/analyze/gtdbundle.py", "src/flowocr/paths.py",
                 "src/flowocr/placeholders.py", "src/flowocr/richtext.py", "src/flowocr/textfold.py")
"""产剧本的代码（本文件 + 它 import 的本地模块，仓库相对路径；`paths` 是 shim，记真实现），
`build_tracks.code_fp` 按内容算指纹。"""
SCHEMA = "flowocr-gameref/7"
"""/2（2026-09-11，audit-6）：锚点带 `unique`、绝区零变体组折成 `Variant` + `variant_of`、
provenance 带 `integrity_warnings`。/3（同日）：锚点带框位置 `y` / `in_band`、新类型 `OffBand`、
`stats.band`。/4（2026-09-25，发布前）：库改成读文本包，provenance 的 `db` / `upstream` / `extractor` /
`integrity_warnings` 合成一个 `gametext`（带内容指纹，`gtdbundle.Bundle.provenance`）。
/5（2026-10-05）：行的 `cn` 里保留占位（`{NICKNAME}` 等花括号，阶段 3 标出来、阶段 4 填值或删掉，`flowocr.placeholders`），
原文里有占位的行另带 `jp_tpl`（留着占位的原文：日文输出用它，匹配器拿它从 OCR 原文里读名字）；`text` 照旧删掉占位。
/6（2026-10-07）：行的 `kind` 只放库里来的类型，这段视频里的处境（没播、没走的分支、变体、重演拷贝、不在字幕带）
另放 `status`（`STATUSES`；之前直接写进 `kind`、盖掉类型）；`stats.kinds` 按类型数，另加 `stats.statuses`。
旧剧本不做兼容，`game_align` 见到就让你重跑。
/7（2026-11-10，en-target 计划）：文本包有英文那份时，行另带 `en` / `en_rich` / `speaker_en`、顶层另带 `speakers_en`，
`terms` 的条目另带 `en`、`textjoins` 的候选另带 `en`；英文只是载荷，圈单元、认领、名牌投票照旧只看日文和中文。
/4 之内加的**可选**顶层 `terms`（2026-09-26，`term_table`）：文本包有短名词表时才有，读的一方缺了照常工作，所以没升版本。
/5 之内加的**可选**顶层 `textjoins`（2026-10-05，`used_textjoins`）：星铁剧本行用到的 `{TEXTJOIN#n}` 的候选，同理没升版本。"""

GAMES = {
    # 游戏 -> {loader 要读的表（文本包清单里的 `table`）: 认得的内容契约主版本}。语种按标准码取 `ja` / `zh-Hans`（有英文那份时另读 `en`），
    # 文件后缀由清单给。主版本对不上的包 `gtdbundle.Bundle.require` 拒绝：那是字段含义变了，这里的读法要先跟上。
    "genshin": {"quests": 1, "reminders": 1},
    "starrail": {"missions": 1, "dialogues": 1, "sentences": 1},
    "zzz": {"scenes": 1, "loose": 1},
}
KNOWN_VALUES = {
    # 据以分类的枚举字段 -> 这里认得的取值（抄自 game-text-data 那边契约声明在上面主版本时的取值）。
    # 上游加一种取值不改契约主版本（开放枚举），所以读库时把没见过的计数、打一行警告（`warn_unseen`）：
    # 新值照现有规则归类（style 不是 Normal 一律不进分母），要不要改归类人看过再定，定了加进这里。
    "genshin.quests.dialog[].role.type": frozenset({
        "TALK_ROLE_NPC", "TALK_ROLE_PLAYER", "TALK_ROLE_BLACK_SCREEN", "TALK_ROLE_NONE",
        "TALK_ROLE_NEED_CLICK_BLACK_SCREEN", "TALK_ROLE_MATE_AVATAR", "TALK_ROLE_GADGET",
        "TALK_ROLE_CONSEQUENT_NEED_CLICK_BLACK_SCREEN", "TALK_ROLE_CONSEQUENT_BLACK_SCREEN"}),
    "genshin.reminders.lines[].style": frozenset({
        "Normal", "Banner", "CombatChat", "MoonInfoReminder", "DialogueWithPortrait", "WhiteMessage",
        "SpecialReminderOnTop", "TpsGlassesChat", "PenumbraStory", "EventPromptDown", "AbyssWarReminder",
        "SpecialReminderOnBottom", "RoleCombatBanner", "BottomLine", "WxycReminder", "DrillBattleReminder",
        "AbyssVsLunarisOnTop", "NoText", "InteractionPromptUI", "InfoTextDialog", "InstituteHomeInfo",
        "WitchNicoleReminder", "TradeShowBonus", "SpecialReminderV2OnTop", "VarkaReminderBottom",
        "InstituteHomeWarning", "VarkaReminder", "HerculesBattle", "EscoffierCookingChat", "AbyssWhisper",
        "VaultDungeonRadio", "PenumbraMiniStory", "SpecialReminderV2OnBottom", "WeaponQuestSnezhnayaTop",
        "InLevelTowerOfHanoiReminder", "SpecialFocusAttackReminder", "AbyssVsLunarisOnBottom",
        "RobotAbyssalNarrate", "RobotAbyssalNarrateBroken", "CommonSpecialReminderNormal",
        "MarionetteTeaTimeReminder", "RankedMatchFeverReminder", "PenumbraTarget", "PenumbraInfo",
        "CommonSpecialReminderWarning", "RobotAbyssalWarning", "VarkaReminderLocked", "SneakLEReminder",
        "NarrationChat", "RedBanner", "CombatChatWithName", "ClueDecryptionChat",
        "CryoDragonReminder", "ClueDecryptionLocationDiscuss", "ClueDecryptionLocationConfirm",
        "ClueDecryptionLocationWrong", "RankedMatchLevelChallengeBanner"}),
    "starrail.events[].kind": frozenset({"talk", "option", "trigger", "wait"}),
    # 绝区零不在这里：散条目按 `head` 分类，`head` 在契约里是自由取值（种类上千）；`loose.reason` 这里不读
}
UNSEEN: Counter = Counter()
"""(字段, 取值) -> 这次读库见到几次；`KNOWN_VALUES` 里没有的才记。"""
GAMETEXT: str | None = None
"""`--gametext` 给的包 / 包目录；None 走 `paths.gametext_root()`。"""
_BUNDLES: dict[str, "gtdbundle.Bundle"] = {}
TAG_GAME = {"gi": "genshin", "hsr": "starrail", "zzz": "zzz"}
"""视频标签前缀 -> 游戏（`data/index.md` F 节的命名）。鸣潮没有库。"""

NON_BAND_KINDS = frozenset({"Choice", "BlackScreen", "Loose", "ReminderUI"})
"""本来就不在字幕带的**类型**（`kind`：库里来的，一行一个，不随视频变；**用途 2 = 字幕带**，前两类在屏幕上，是用途 1 的分母）：

* `Choice`：走选项 UI。原神里**玩家的非独白台词一律是选项按钮**，只有一个选项时也是
  （摆帧确认：gi1 148 s 的 `ナタ人って、見たことないしね。` 在右侧对话按钮上）；
  括号独白 `（…）` 才进字幕带。gi1 实测玩家行被主轨命中 10 条，9 条是括号独白；
  剩下那条 `カチーナ！ムアラニ！` 是例外（非独白却上了字幕带），这条判据会把它排出分母。
* `BlackScreen`：原神黑屏旁白（屏幕正中），不在字幕带。
* `Loose`：绝区零的散条目（`loose_*`：漫画过场、AutoEvent、教程、UI…）。上屏形态不知道
  （漫画气泡、弹窗都不是字幕带），所以只参与检索和对齐——cue 读到它就不算"对不上剧本"——
  不进分母。例外是摆帧确认在字幕带的 `CN` / `TL` 两类（`LOOSE_BAND_HEADS`），按连号成组、记 `Talk`、进分母。
* `ReminderUI`：原神语音字幕（`reminders_*`）里 style 不是 `Normal` 的（`Banner` / `WhiteMessage` /
  `CombatChat`…）。`Normal` 的记 `Reminder`、**进分母**：摆帧 gi1 6678 s 的
  `カチーナ / ありがとう、ムアラニちゃん。` 就在底部字幕带、带名牌，主轨命中 gi1 57/64、gi2 14/20；
  `Banner` 摆帧 gi1 7456 s 在画面上方横幅，gi1 / gi2 主轨命中 0/4、0/8（obs 全读到过），
  `WhiteMessage` 0/4。其余 style 没见过上屏，保守地不进分母。

画不画是另一回事（显示协议，`scriptmatch.group_of`）：`ReminderUI` 和绝区零散条目里的 `Tips` / `MiddleTips` 归 `special`（特殊上屏形式的台词与提示，默认画），
其余散条目除过场字幕外归 `ui`（界面文字，默认不画）。

其余类型（原神的上游角色 `NPC` / `PLAYER`…、`Reminder`、星铁 / 绝区零的 `Talk`）是台词。"""

STATUSES = ("OutOfSpan", "Unwalked", "Ambiguous", "Variant", "Duplicate", "OffBand")
"""行在**这段视频里的处境**（`status`；没有处境是 None）。有处境的行一律不进分母，类型照旧留在 `kind` 里
（没播段里的黑屏旁白仍是 `BlackScreen`）。按 `build` 里判的先后排列，单值：几种同时成立时**后判的覆盖先判的**——
`OutOfSpan` / 分支两种先判（选项不判），再折变体（`Variant`），再判重演拷贝（`Duplicate`，可以盖掉前面任一种），
最后判 `OffBand`（只判还没有处境、类型也在带内的行）。

* `OutOfSpan`：单元里没在这段视频里播的那几截——**第一条之前、最后一条之后**没被 obs
  锚住的行（切片从对话中间开始、玩家中途离开，同 `script_align --span-ref` 的道理），
  以及中间一长段零锚点、两头锚点隔了几个小时的（`unplayed`）。
* `Unwalked` / `Ambiguous`：分支没走到 / 判不了（见模块说明）。
* `Variant`：绝区零变体组里代表行以外的那种说法（`fold_variants`）。它参与对齐，被认领时
  **记在代表行上**——一组只算一个分母条目，任一种说法命中就算命中。
* `Duplicate`：库里同一段对话常有几份拷贝（换条件的重演版本，gi2 的 402513 / 402701
  共 59 行相同）。后一份里和前面单元重复的行，若 obs 里这段文字只上屏过一次，就不计，
  而且**连对齐序列都不进**（`sequence`）——只排出分母的话，对齐会去认领后面那份拷贝
  （gi1 实测 41/49 被命中），原件反被记成漏。
* `OffBand`：类型在带内、没有别的处境、obs 也读到过（唯一查询），但锚住它的框**没有一个**落在字幕带里
  （`band_of` / `mark_offband`）。类型是库的结构判的，这一类是**位置**判的，判得出库里没有结构的：
  绝区零的选项按钮、居中旁白、头顶气泡（上游没有分支和说话人），原神 / 星铁结构判据漏掉的。
  拿已有的结构判据核过位置判据（2026-09-11 四场整片）：原神 / 星铁的 `Choice` 146/158、75/76、61/67
  落在带外，`BlackScreen` 21/29、6/6，`ReminderUI` 9/9、4/5；主轨命中的行被判出带外的 4 / 0 / 0 / 2 条
  （绝区零居中的选项、原神一条居中的独白，都进了主轨）。没锚点的行没有位置证据，照旧留在分母里。"""


def in_band(kind: str, status: str | None) -> bool:
    """这一行进不进用途 2 的分母（该在字幕带）：类型在带内，而且在这段视频里没有处境。"""
    return status is None and kind not in NON_BAND_KINDS


# ---------------------------------------------------------------- 读库

def note_value(field: str, value) -> None:
    if value is not None and value not in KNOWN_VALUES[field]:
        UNSEEN[(field, value)] += 1


def warn_unseen() -> None:
    """读完一款游戏的库：没见过的枚举取值各打一行（只在建库时读库，缓存命中时不重报——换了包缓存就不命中）。"""
    for (fld, v), n in sorted(UNSEEN.items()):
        print(f"  ⚠ 文本包：{fld} 出现没见过的取值 {v!r}（{n} 处），照现有规则归类；确认含义后加进 KNOWN_VALUES",
              file=sys.stderr, flush=True)
    UNSEEN.clear()


def bundle_of(game: str) -> "gtdbundle.Bundle":
    """这款游戏的文本包（进程内只找一次）：找到、核清单、确认 loader 要的表和语种都在。"""
    if game not in _BUNDLES:
        b = gtdbundle.locate(game, GAMETEXT)
        b.require(GAMES[game])
        _BUNDLES[game] = b
    return _BUNDLES[game]


EN = "en"
"""英文译文在文本包里的语种码（**可选**：包里这张表没有英文就不读，剧本行的 `en` 为 None）。"""
_NONE = ()


def has_en(game: str, stem: str) -> bool:
    return bundle_of(game).has([stem], (EN,))


def paired(game: str, stem: str):
    """同一张表的日文 / 中文 / 英文三份，**逐行 id 相同**（game-text-data 的不变量），这里再核一遍；包里没有英文那份时第三项一律 None。
    几份都读到底，`iter_rows` 读完各自核一遍哈希。"""
    bd = bundle_of(game)
    en = bd.iter_rows(stem, EN) if has_en(game, stem) else _NONE
    # 按最长的配：只核 id 相等核不出"一份少了尾巴"，裸 zip 会静默截断
    for a, b, e in zip_longest(bd.iter_rows(stem, "ja"), bd.iter_rows(stem, "zh-Hans"), en):
        if a is None or b is None or (en is not _NONE and e is None):
            short = "日文" if a is None else "中文" if b is None else "英文"
            raise SystemExit(f"{game}/{stem} 日中英几份行数不一样（{short}那份先完）")
        if a["id"] != b["id"] or (e is not None and e["id"] != a["id"]):
            raise SystemExit(f"{game}/{stem} 日中英几份逐行 id 对不上：{a['id']} / {b['id']} / {e and e['id']}")
        yield a, b, e


@dataclass
class Line:
    key: str
    kind: str                    # 类型（`NON_BAND_KINDS` 的说明）；这段视频里的处境在输出行的 `status` 上
    role: str                    # 上游的角色 / 载体（原样，用来校准分母）
    speaker: str | None
    raw: str                     # 上游原文（含占位 / 标签）
    cn: str | None
    memberships: list[tuple[str, str]] = field(default_factory=list)
    """独占所属的分支：(分支组, 选项)。嵌套分支会有多条。空 = 不在任何分支的独占集里。"""
    variant: str | None = None
    """绝区零变体组（同一句话按主角给的两种说法，`load_zzz`）。同组只算一个分母条目（`fold_variants`）。"""
    speaker_cn: str | None = None
    """说话人的中文名（上游中文那份的 `speaker`；原神任务对话多半为空、绝区零 scene 没有这个字段）。
    叠加 ASS 的名牌层译文从这里来（`scriptmatch.speaker_names`）。"""
    en: str | None = None
    """英文译文（包里有英文那份时；同 `cn`，只是载荷：判断照旧只看 `cn`）。"""
    speaker_en: str | None = None


@dataclass
class Unit:
    uid: str
    title: str | None
    lines: list[Line]
    solo: bool = False
    """一行被锚住就算这段视频有它（单条的散条目：没有同组的其他行可以凑够两个锚点）。"""


def is_monologue(raw: str | None) -> bool:
    """原神主角的心里话：整条用括号包着，进字幕带；其余玩家台词是选项按钮。"""
    t = clean(raw or "", "F")
    return t[:1] in ("（", "(")


def exclusive_members(nexts: dict[int, list[int]]) -> dict[int, list[tuple[int, int]]]:
    """对话图里每个分支点（出边 ≥2），每个子节点的**独占可达集**——只从它能到、
    兄弟到不了的那部分。返回 节点 -> [(分支点, 子节点)…]（嵌套分支会有多条）。
    可达性**不穿过分支点本身**：选项绕回分支点是原神的常态（"把几个选项都问一遍"），
    穿过去的话，分支点和它之后的兄弟分支都会被算进这一支。"""
    def reach(start: int, block: int) -> set[int]:
        seen, st = set(), [start]
        while st:
            x = st.pop()
            if x in seen or x == block or x not in nexts:
                continue
            seen.add(x)
            st.extend(nexts[x])
        return seen
    out: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for g, nx in nexts.items():
        if len(nx) < 2:
            continue
        rs = {n: reach(n, g) for n in nx}
        for n in nx:
            others = set().union(*(rs[m] for m in nx if m != n))
            for x in rs[n] - others:
                out[x].append((g, n))
    return out


def branch_kind(memberships: list[tuple[str, str]], walked: dict[str, set]) -> str | None:
    """这一行所属的分支走没走：同组有兄弟走过而它没有 -> Unwalked；同组谁都没走过 -> Ambiguous。"""
    st = ["unwalked" if walked.get(g) and o not in walked[g] else
          "ambiguous" if not walked.get(g) else "walked" for g, o in memberships]
    return "Unwalked" if "unwalked" in st else "Ambiguous" if "ambiguous" in st else None


def load_genshin() -> list[Unit]:
    units: dict[int, Unit] = {}
    pron = pronoun_table("genshin")
    sx = (lambda t, lang: sexpro(t, pron[lang]) if lang in pron else t) if pron else (lambda t, lang: t)   # noqa: E731
    for q, qc, qe in paired("genshin", "quests"):
        cn_of = {d["dialog_id"]: sx(d["text"], "zh-Hans") for d in qc["dialog"]}
        spk_of = {d["dialog_id"]: d.get("speaker") for d in qc["dialog"]}
        en_of = {d["dialog_id"]: (sx(d["text"], EN), d.get("speaker")) for d in (qe or {}).get("dialog") or ()}
        talks: dict[int, list[dict]] = defaultdict(list)
        for d in q["dialog"]:
            talks[d["talk_id"]].append(d)
        for tid, ds in talks.items():
            if tid in units:        # 同一个 talk 挂在几个任务下（上游 9,597 个节点如此），留第一个
                continue
            ds = sorted({d["dialog_id"]: d for d in ds}.values(), key=lambda d: d["dialog_id"])
            by = {d["dialog_id"]: d for d in ds}
            choice = set()
            for d in ds:
                note_value("genshin.quests.dialog[].role.type", (d.get("role") or {}).get("type"))
                nx = [n for n in (d.get("next") or []) if n in by]
                if len(nx) > 1:
                    choice.update(n for n in nx
                                  if (by[n].get("role") or {}).get("type") == "TALK_ROLE_PLAYER")
            lines = {}
            for d in ds:
                role = ((d.get("role") or {}).get("type") or "NONE").removeprefix("TALK_ROLE_")
                if d["dialog_id"] in choice or (role == "PLAYER" and not is_monologue(d["text"])):
                    kind = "Choice"
                elif "BLACK_SCREEN" in role:
                    kind = "BlackScreen"
                else:
                    kind = role
                en, spk_en = en_of.get(d["dialog_id"], (None, None))
                lines[d["dialog_id"]] = Line(key=f"gi:{d['dialog_id']}", kind=kind, role=role,
                                             speaker=d.get("speaker"), raw=sx(d["text"] or "", "ja"),
                                             cn=cn_of.get(d["dialog_id"]), speaker_cn=spk_of.get(d["dialog_id"]),
                                             en=en, speaker_en=spk_en)
            nexts = {i: [n for n in (d.get("next") or []) if n in by] for i, d in by.items()}
            for x, ms in exclusive_members(nexts).items():
                lines[x].memberships.extend((f"gi:{g}", str(o)) for g, o in ms)
            units[tid] = Unit(uid=f"gi:talk:{tid}", title=q.get("name"), lines=list(lines.values()))
    out = list(units.values())
    # 语音字幕（`reminders_*`：边玩边说的对白，不在任何 talk 里）：一条链一个单元，
    # 单句链一条锚点就算。role 记上游的 style；只有 `Normal` 在字幕带（见 NON_BAND_KINDS）
    for r, rc, re_ in paired("genshin", "reminders"):
        for l in r["lines"]:
            note_value("genshin.reminders.lines[].style", l["style"])
        lines = [Line(key=f"gi:rem:{l['reminder_id']}",
                      kind="Reminder" if l["style"] == "Normal" else "ReminderUI",
                      role=l["style"] or "?", speaker=l["speaker"], raw=sx(l["text"], "ja"), cn=sx(c["text"], "zh-Hans"),
                      speaker_cn=c.get("speaker"), en=sx(e.get("text"), EN), speaker_en=e.get("speaker"))
                 for l, c, e in zip(r["lines"], rc["lines"], re_["lines"] if re_ else [{}] * len(r["lines"]), strict=True)
                 if l["text"]]
        if lines:
            out.append(Unit(uid=f"gi:rem:{r['id']}", title=None, lines=lines, solo=len(lines) == 1))
    warn_unseen()
    return out


def _hsr_script(uid: str, title: str | None, ev: list[dict], evc: list[dict], eve: list[dict] | None = None) -> Unit:
    """星铁一个脚本：`talk`/`trigger` 是上屏的台词，`option` 是选项（它的 `reply` 是 NPC 的回答，
    上屏、属于这个选项的分支），`wait <选项的 trigger>` 之后到下一个 `wait` 之间是那个选项的分支段。

    两处第一版都做错过（2026-09-11 整场 hsr 反查出来的）：①没读 `reply`，选项对话里只剩玩家的选项、
    没有 NPC 的回答（上游 27,476 个选项带回答，game-text-data 那边第一版也这么丢过）；②分支段按
    `wait TalkSentence_<选项 id>` 认，而 wait 引用的多是选项的 `trigger`（= 回答那句，27,141 次对 2,054 次），
    九成以上的分支段没认出来、全当了主干。"""
    # wait 引用的是选项的 trigger；少数脚本 trigger 就是选项自己。一个 trigger 挂在几个选项上 = 不独占
    opts_of: dict[str, set] = defaultdict(set)
    group_of, g, prev_opt = {}, 0, False
    for e in ev:
        note_value("starrail.events[].kind", e["kind"])
        if e["kind"] == "option":
            if not prev_opt:
                g += 1
            group_of[e["sentence_id"]] = g
            prev_opt = True
            for t in {e.get("trigger"), f"TalkSentence_{e['sentence_id']}"} - {None}:
                opts_of[t].add(e["sentence_id"])
        else:
            prev_opt = False
    lines: dict[int, Line] = {}
    where: dict[int, set] = defaultdict(set)      # sentence -> {选项 id 或 None(主干)}

    def add(sid, kind, role, src, src_c, src_e):
        if sid not in lines:
            lines[sid] = Line(key=f"hsr:{sid}", kind=kind, role=role, speaker=src.get("speaker"),
                              raw=src["text"], cn=src_c.get("text"), speaker_cn=src_c.get("speaker"),
                              en=src_e.get("text"), speaker_en=src_e.get("speaker"))

    cur = None
    for e, c, en in zip(ev, evc, eve if eve is not None else [{}] * len(ev), strict=True):
        k = e["kind"]
        sid = e.get("sentence_id")
        if k == "wait":
            os_ = opts_of.get(e.get("custom_string") or "", set())
            cur = next(iter(os_)) if len(os_) == 1 else None
            continue
        if sid is None or not e.get("text"):
            continue
        add(sid, "Choice" if k == "option" else "Talk", e.get("carrier") or k, e, c, en)
        if k != "option":
            where[sid].add(cur)
            continue
        r = e.get("reply") or {}
        if r.get("sentence_id") is not None and r.get("text"):
            add(r["sentence_id"], "Talk", "OptionReply", r, c.get("reply") or {}, en.get("reply") or {})
            where[r["sentence_id"]].add(sid)
    for sid, ws in where.items():
        if len(ws) == 1 and None not in ws:
            (o,) = ws
            lines[sid].memberships.append((f"{uid}#g{group_of[o]}", str(o)))
    return Unit(uid=uid, title=title, lines=list(lines.values()))


def load_starrail() -> list[Unit]:
    out = []
    for m, mc, me in paired("starrail", "missions"):
        for s, sc, se in zip(m["scripts"], mc["scripts"], me["scripts"] if me else [None] * len(m["scripts"]), strict=True):
            out.append(_hsr_script(f"hsr:{s['source']}", m.get("name"), s["events"], sc["events"], se and se["events"]))
    for d, dc, de in paired("starrail", "dialogues"):
        out.append(_hsr_script(f"hsr:{d['id']}", d.get("interact_title") or d.get("location"),
                               d["events"], dc["events"], de and de["events"]))
    # 全量台词表里**没有脚本引用**的那些（过场时间轴里念的，`.playable` 不在上游仓库）。
    # 按 id 的百位块成组：同一脚本的台词 id 连号（game-text-data 的 docs/starrail），
    # 块内按 id 排。这是本工具的归组，库那边刻意不做。
    blocks: dict[int, list[Line]] = defaultdict(list)
    for s, sc, se in paired("starrail", "sentences"):
        if s["sources"] or not s["text"]:
            continue
        se = se or {}
        blocks[s["id"] // 100].append(Line(key=f"hsr:{s['id']}", kind="Talk", role="Timeline",
                                           speaker=s["speaker"], raw=s["text"], cn=sc["text"],
                                           speaker_cn=sc.get("speaker"), en=se.get("text"), speaker_en=se.get("speaker")))
    out.extend(Unit(uid=f"hsr:block:{b}", title=None, lines=ls) for b, ls in sorted(blocks.items()))
    warn_unseen()
    return out


VARIANT_SIM = 0.5
"""绝区零相邻两行的内容相似度达到这么多，就当成主角的两种说法（见 `load_zzz`）。"""


def variant_groups(texts: list[str], sim: float = VARIANT_SIM) -> list[list[int]]:
    """相邻且相似的行连成组（长度 ≥2 的才返回）。只看相邻：两种说法在库里总是挨着放的。"""
    groups, cur = [], [0] if texts else []
    for i in range(1, len(texts)):
        a, b = cnorm(texts[i - 1]), cnorm(texts[i])
        if len(a) >= 3 and len(b) >= 3 and SequenceMatcher(None, a, b, autojunk=False).ratio() >= sim:
            cur.append(i)
        else:
            if len(cur) > 1:
                groups.append(cur)
            cur = [i]
    if len(cur) > 1:
        groups.append(cur)
    return groups


def load_zzz() -> list[Unit]:
    """绝区零两位主角（リン / アキラ）的台词**在库里是相邻的两行**，玩家只会看到其中一行：

        _001 リンさん、ビリーさん、聞こえるかしら？…
        _002 アキラさん、ビリーさん、聞こえるかしら？…

    上游没有分支信息（它自己 README 的"上游边界"），所以按相邻相似度把它们连成一组（`variant`），
    同组**只算一个分母条目**、任一种说法被认领就算命中（`fold_variants`）。

    第一版把组交给 `branch_kind` 当选项判（锚住的那行走过、另一行 Unwalked），两处都错
    （audit-6，2026-09-11 数的）：①两行都没锚点就整组记 Ambiguous 排出分母，而整场 zzz
    这 47 行被主轨命中了 10 行；②obs 一个框的文字常常同时"包含于"两种说法，两行都被锚住、
    都留在分母里，于是必有一行记成漏。"""
    out = []
    for s, sc, se in paired("zzz", "scenes"):
        cn = {l["key"]: l["text"] for l in sc["lines"]}
        en = {l["key"]: l["text"] for l in (se or {}).get("lines") or ()}
        lines = [Line(key=f"zzz:{l['key']}", kind="Talk", role=s.get("kind") or "?",
                      speaker=l.get("speaker"), raw=l["text"] or "", cn=cn.get(l["key"]), en=en.get(l["key"]))
                 for l in s["lines"]]
        for g in variant_groups([clean(ln.raw, "F") for ln in lines]):
            for i in g:
                lines[i].variant = f"zzz:{s['id']}#v{g[0]}"
        out.append(Unit(uid=f"zzz:{s['id']}", title=s.get("title"), lines=lines))
    # 散条目：在字幕带上的两类（CN / TL）按连号成组进分母；其余 key 里读得出分组的
    # （AutoEvent<事件>_<序号>）成组按序号排，读不出的一条一个单元，都只参与检索与对齐
    groups: dict[str, list] = defaultdict(list)
    band: dict[str, list] = defaultdict(list)
    for r, rc, re_ in paired("zzz", "loose"):
        if not r["text"]:
            continue
        slot = band_loose_slot(r["head"], r["id"])
        ln = Line(key=f"zzz:{r['id']}", kind="Talk" if slot else "Loose", role=r["head"],
                  speaker=None, raw=r["text"], cn=rc["text"], en=(re_ or {}).get("text"))
        if slot:
            base, seq, pc = slot
            if pc:
                ln.variant = f"zzz:loose:{r['head']}:{base}#{seq}"
            band[f"{r['head']}:{base}"].append(((seq, pc), ln))
        elif r["group"]:
            groups[f"{r['head']}:{r['group']}"].append((r["seq"], ln))
        else:
            out.append(Unit(uid=f"zzz:loose:{r['id']}", title=None, lines=[ln], solo=True))
    for g, items in sorted(groups.items()) + sorted(band.items()):
        out.append(Unit(uid=f"zzz:loose:{g}", title=None,
                        lines=[ln for _, ln in sorted(items, key=lambda x: x[0])],
                        solo=len(items) == 1))
    return out


LOOSE_BAND_HEADS = ("CN", "TL")
"""绝区零散条目里**在字幕带上**的两类，进分母（audit-6，摆帧：`CN` 7,972 s 是带名牌 `リン` 的
对话框，`TL` 1,402 s 是过场字幕；整场 zzz 主轨命中 `CN` 6/8、`TL` 2/2）。同一批被命中的
`Comic`（漫画格里的气泡，位置随格子变）、技能说明、线索描述、按键提示不是字幕带，仍是 `Loose`。"""
CN_ID_RE = re.compile(r"^(?P<base>.*?_EP\d+)(?P<pc>[BG]?)_(?P<seq>\d+)$")
SEQ_ID_RE = re.compile(r"^(?P<base>.*)_(?P<seq>\d+)$")


def band_loose_slot(head: str, id_: str) -> tuple[str, int, str] | None:
    """字幕带散条目在它那段过场里的位置：(组, 序号, 主角写法 'B' / 'G' / '')；不进分母的给 None。

    库那边这两类**没有分组**（`group` 为空），每条一个单元的话，只有被 OCR 读到的行才进得了
    分母、漏量不出来，所以按连号成组（本工具的归组，同星铁的百位块）。`CN` 的 key 是
    `C280_EP020_010`：基底 `C280_EP020` 放两位主角共有的行，主角相关的几句在 `C280_EP020G_030` /
    `C280_EP020B_030` 两份里、**同一个序号各一种说法**（`私が…みんなのプロキシだよ！` /
    `僕が…みんなのプロキシだ！`）——并进基底那一组、同序号的两行记成变体组。
    名牌（`_Name_`）与读不出序号的（`_Lyric` 歌词）不进。"""
    if head not in LOOSE_BAND_HEADS or "_Name_" in id_:
        return None
    m = CN_ID_RE.match(id_) if head == "CN" else None
    if m:
        return m["base"], int(m["seq"]), m["pc"]
    m = SEQ_ID_RE.match(id_)
    return (m["base"], int(m["seq"]), "") if m else None


LOADERS = {"genshin": load_genshin, "starrail": load_starrail, "zzz": load_zzz}


# ---------------------------------------------------------------- 清洗与归一

GENDER_RE = re.compile(r"\{([MF])#([^{}]*)\}")
RUBY_RE = re.compile(r"\{RUBY#\[[^\]]*\][^{}]*\}|\{RUBY_[BE]#(?:[^{}]|\{[^{}]*\})*\}|<rt>[^<>]*</rt>")
"""注音：原神 `{RUBY#[S]注音}`，星铁 `{RUBY_B#注音}正文{RUBY_E#}`（删掉两个花括号正好剩正文），绝区零 `<ruby><rt>注音</rt>正文</ruby>`
（删掉 `<rt>…</rt>`，剩下的两个标签照别的标签删）。星铁的注音里可以嵌一个占位（`{RUBY_B#{TEXTJOIN#254}}`），注音整个删，所以允许嵌一层花括号。"""
GI_RUBY_RE = re.compile(r"\{RUBY#\[([DS])\]([^{}]*)\}")
"""原神的注音标的是读音的**水平中心**、不是范围（2026-10-23 查清，game-style 计划）：`[D]` 中心在标记所在的字缝，`[S]` 中心是标记前面那个字。
换成中心相同的最小范围：`X{RUBY#[D]读音}Y` → X、Y 两个字，`X{RUBY#[S]读音}` → X（`_gi_ruby`）。"""
SR_RUBY_RE = re.compile(r"\{RUBY_B#((?:[^{}]|\{[^{}]*\})*)\}(.*?)\{RUBY_E#\}", re.S)
ZZZ_RUBY_RE = re.compile(r"<ruby>\s*<rt>([^<>]*)</rt>(.*?)</ruby>", re.S)
HIDDEN_RE = re.compile(r"<[^<>]*>|\{(?:[^{}]|\{[^{}]*\})*\}")
"""不是看得见的字的：标签和花括号（占位、排版写法）。"""
LAYOUT_RE = re.compile(r"\{LAYOUT_([A-Z]+)#([^{}]*)\}")
PC_LAYOUTS = ("KEYBOARD", "PC")
"""绝区零教程提示按平台给几种写法；这批直播都是 PC，留键盘那一种（没有就留 FALLBACK）。"""
PLACEHOLDER_RE = PH.PLACEHOLDER_RE
"""性别写法 / 注音 / 平台写法 / 不断行空格 / 图标字先处理掉之后，剩下的花括号都是占位：多数是玩家相关的
（`{NICKNAME}` / `{REALNAME[…]}`；`{PLAYERAVATAR#SEXPRO[…]}` 只在包里没有称谓表时还在），少数是运行时取值：
原神 `{ABYSSWAR#…}`（21 处以内）、星铁 `{TEXTJOIN#…}`（142）、绝区零 `{NPC_…}`（25）。匹配用的原文里全删，译文和原文模板里留着。"""
TAG_RE = re.compile(r"<[^<>]*>")
NBSP_RE = re.compile(r"\{NON_BREAK_SPACE\}")
"""排版写法，不是占位：不断行的空格（原神日文 30 处，`近くて{NON_BREAK_SPACE}遠い`）。"""
ICON_RE = re.compile(r"\{REGEX#UNICODE\[[0-9A-Fa-f]+\]\}")
"""排版写法，不是占位：嵌在台词里的图标字（原神 7.1.0 中、日文各 21 处，`{REGEX#UNICODE[E001]}`），游戏字体里的私用区字形，字幕里画不出来，删掉。"""
def _layout(t: str) -> str:
    keep = next((p for p in PC_LAYOUTS if f"{{LAYOUT_{p}#" in t), "FALLBACK")
    return LAYOUT_RE.sub(lambda m: m.group(2) if m.group(1) == keep else "", t)


def _gi_ruby(t: str) -> str:
    """原神的注音（`GI_RUBY_RE`）换成 Unity 的 `<ruby="读音">正文</ruby>`：`[D]` 包住标记两边各一个字，`[S]` 包住前面那个字。
    两边的字要看得见：不是空白、不在花括号里（占位、排版写法），中间隔着的标签跳过（`<b>漢</b>{RUBY#[D]…}字` 照样是「漢字」，
    交叉的样式由 `from_unity` 拆开），已经归了前一处注音的不能再用。`[D]` 右边没有字（句尾）当 `[S]`、左边没有字只包右边那个，
    `[S]` 左边没有字（句首）这处注音丢掉。"""
    ms = list(GI_RUBY_RE.finditer(t))
    if not ms:
        return t
    s, at, pos = "", [], 0                                     # 先把标记全拿掉，记下每个标记在剩下的串里的位置
    for m in ms:
        s += t[pos:m.start()]
        at.append((len(s), m.group(1), m.group(2)))
        pos = m.end()
    s += t[pos:]
    tag = [0] * len(s)                                         # 1 = 在标签里（跳过），2 = 在花括号里（不是字）
    for m in HIDDEN_RE.finditer(s):
        tag[m.start():m.end()] = [1 if s[m.start()] == "<" else 2] * (m.end() - m.start())
    used: set[int] = set()

    def char(i: int, step: int) -> int | None:                 # 从 i 往 step 方向找第一个不在标签里的字；看不见、用过了是 None
        while 0 <= i < len(s) and tag[i] == 1:
            i += step
        return i if 0 <= i < len(s) and not tag[i] and not s[i].isspace() and i not in used else None
    opens: dict[int, list[str]] = defaultdict(list)
    closes: dict[int, list[str]] = defaultdict(list)
    for i, kind, reading in at:
        x, y = char(i - 1, -1), char(i, 1) if kind == "D" else None
        span = (x, y) if x is not None and y is not None else (x, x) if x is not None else (y, y) if kind == "D" and y is not None else None
        if span is None:
            continue
        used.update(range(span[0], span[1] + 1))
        opens[span[0]].append(richtext.unity_ruby(reading))
        closes[span[1]].append("</ruby>")
    return "".join("".join(opens.get(i, ())) + c + "".join(closes.get(i, ())) for i, c in enumerate(s))


def ruby_rich(t: str) -> str:
    """各游戏的注音 -> Unity 的 `<ruby="读音">正文</ruby>`（`richtext.unity_ruby`，`richtext.from_unity` 再收成规范写法）：
    原神按中心点换成最小范围（`_gi_ruby`），星铁 `{RUBY_B#读音}正文{RUBY_E#}`（读音可以是占位 `{RUBY_B#{TEXTJOIN#254}}`，留着），
    绝区零 `<ruby><rt>读音</rt>正文</ruby>`（绝区零另有 Unity 写法的 `<ruby=读音>`，本来就认得）。删掉标签以后和 `RUBY_RE` 删掉注音的结果相同。"""
    t = _gi_ruby(t)
    t = SR_RUBY_RE.sub(lambda m: richtext.unity_ruby(m.group(1)) + m.group(2) + "</ruby>", t)
    return ZZZ_RUBY_RE.sub(lambda m: richtext.unity_ruby(m.group(1)) + m.group(2) + "</ruby>", t)


def clean(raw: str, gender: str, keep: bool = False, rich: bool = False) -> str:
    """上游原文 -> 屏幕上会出现的文字（`\\N` 表示条内换行）。`keep`：占位原样留着（译文和原文模板用：阶段 3 标出来、
    阶段 4 填值或删掉，`flowocr.placeholders`）；用来匹配的原文不留。`rich`：同 `keep`，另把认得的样式和注音按规范写法留在里面
    （`ruby_rich`、`richtext.from_unity`，投回 `keep` 的结果上：两份只差标签）；投不上就是不带样式的那份。"""
    t = raw.lstrip("#")
    t = t.replace("\\n", "\n")
    t = GENDER_RE.sub(lambda m: m.group(2) if m.group(1) == gender else "", t)
    if rich:
        t = ruby_rich(t)
    t = RUBY_RE.sub("", t)
    t = _layout(t)
    t = NBSP_RE.sub(" ", t)
    t = ICON_RE.sub("", t)
    styled = t
    t = TAG_RE.sub("", t)                   # 先删标签：`<color>{NICKNAME}</color>，…` 删完标签占位才在行首
    if not (keep or rich):
        t = PH.drop(t)
    t = "\\N".join(x.strip() for x in t.split("\n") if x.strip())
    if rich and "<" in styled:
        return richtext.project(richtext.from_unity(styled), t) or t
    return t


NONWORD = re.compile(r"[\W_]+")


def cnorm(t: str) -> str:
    """检索用的**内容**归一：NFKC、小书写假名并成大写、去掉一切标点空白。
    OCR 最常见的差异是省略号 / 小书写假名（script-corpus 报告文本质量那节），
    检索这一步不该被它们挡住；量文本质量是 game_align 的事，不在这里。"""
    return NONWORD.sub("", textfold.fold(t.replace("\\N", "")))


# ---------------------------------------------------------------- 原神的称谓表（可选）

PRONOUNS_TABLE = "pronouns"
"""文本包里原神的称谓表（game-text-data 的 `pronouns_*`：`INFO_MALE_PRONOUN_HE` 这类键 -> 文本，日中逐行 id 相同）。
**可选**：旧包没有它照读，称谓占位就留成占位（阶段 4 删掉）。"""
PRONOUNS_CONTRACT = 1
SEXPRO_RE = re.compile(r"\{(PLAYERAVATAR|MATEAVATAR)#SEXPRO\[([^|\[\]{}]+)\|([^|\[\]{}]+)\]\}")
"""原神按性别取的称谓：`{PLAYERAVATAR#SEXPRO[甲|乙]}` 按旅行者的性别、`{MATEAVATAR#SEXPRO[…]}` 按另一位双子的性别选键——
**第一个键是那个角色为男时用的，第二个为女时**，和键名里的 MALE / FEMALE 无关（`我的{PLAYERAVATAR#SEXPRO[INFO_MALE_PRONOUN_SISTER|
INFO_FEMALE_PRONOUN_BROTHER]}` 是男主说「妹妹」；7.1.0 中文剧本里键对按位置读全部说得通）。再查称谓表（7.1.0 任务对话里中文 775 处、日文 982 处；日文另有按性别变的句尾 `INFO_*_PLAYER_END0x`，同样处理）。"""
_PRON: dict[str, dict | None] = {}


def pronoun_table(game: str) -> dict[str, dict[str, str]] | None:
    """`{语种: {键: 文本}}`（`ja` / `zh-Hans`，包里有英文那份时另有 `en`）；包里没有这张表是 None。"""
    if game not in _PRON:
        bd = bundle_of(game)
        if not bd.has([PRONOUNS_TABLE]):
            _PRON[game] = None
        else:
            bd.require({PRONOUNS_TABLE: PRONOUNS_CONTRACT})
            tabs: dict[str, dict] = {"ja": {}, "zh-Hans": {}, **({EN: {}} if has_en(game, PRONOUNS_TABLE) else {})}
            for row in paired(game, PRONOUNS_TABLE):
                for lang, r in zip(("ja", "zh-Hans", EN), row):
                    if r is not None and r["text"]:
                        tabs[lang][r["id"]] = r["text"]
            _PRON[game] = tabs
        if _PRON[game] is None:
            print(f"  称谓表：文本包里没有（旧包），按性别取的称谓留成占位、出字幕时删掉；换 game-text-data 带 {PRONOUNS_TABLE}_* 的包", flush=True)
    return _PRON[game]


def sexpro(t: str | None, table: dict[str, str]) -> str | None:
    """称谓占位改写成性别写法 `{M#…}{F#…}`（`M` / `F` 是旅行者的性别），之后和别的性别写法一起投票、二选一（`clean`）。
    第一个键归男、第二个归女（`SEXPRO_RE`）；`MATEAVATAR` 是另一位双子，旅行者是男的时它是女的，所以对调。查不到键的原样留着（当占位）。"""
    if not t or "SEXPRO" not in t:
        return t

    def one(m: re.Match) -> str:
        male, female = table.get(m.group(2)), table.get(m.group(3))
        if male is None or female is None:
            return m.group(0)
        if m.group(1) == "MATEAVATAR":
            male, female = female, male
        return f"{{M#{male}}}{{F#{female}}}"
    return SEXPRO_RE.sub(one, t)


# ---------------------------------------------------------------- 星铁玩家起名的占位（可选）

TEXTJOINS_TABLE = "textjoins"
"""文本包里星铁的 `{TEXTJOIN#n}` 候选表（game-text-data 的 `textjoins_*`：n -> 默认项下标 + 候选，日中逐行 id 相同）。
**可选**：旧包没有它照读，剧本里就没有 `textjoins`，这类占位没有候选、匹配器不猜（阶段 4 删掉）。"""
TEXTJOINS_CONTRACT = 1
TEXTJOIN_RE = re.compile(r"\{TEXTJOIN#(\d+)\}")


def textjoin_table(game: str, gender: str) -> dict[int, dict] | None:
    """`{n: {"default": 下标或 None, "jp": [候选…], "cn": [候选…], "en": [候选…]}}`（`en` 只在包里有英文那份时有；候选和台词一样 `clean`：性别写法按投出的性别选、去掉注音和富文本标记、
    占位留着——n=22 的默认项就是 `{NICKNAME}`，匹配器换成玩家名）；包里没有这张表是 None。"""
    bd = bundle_of(game)
    if not bd.has([TEXTJOINS_TABLE]):
        return None
    bd.require({TEXTJOINS_TABLE: TEXTJOINS_CONTRACT})
    out = {}
    def cands(r):
        return [clean(t, gender, keep=True) or None if t else None for t in r["items"]]
    for a, b, e in paired(game, TEXTJOINS_TABLE):
        out[a["id"]] = {"default": a["default"], "jp": cands(a), "cn": cands(b), **({EN: cands(e)} if e else {})}
    return out


def used_textjoins(game: str, out_units: list[dict], gender: str) -> dict[str, dict] | None:
    """圈出的剧本行（译文 / 原文模板）里用到的 `{TEXTJOIN#n}` 的候选，写进剧本的 `textjoins`（匹配器按视频认是哪个）；
    没用到或包里没有候选表是 None。"""
    used = sorted({int(n) for u in out_units for ln in u["lines"] for k in ("cn", EN, "jp_tpl")
                   for n in TEXTJOIN_RE.findall(ln.get(k) or "")})
    if not used:
        return None
    tab = textjoin_table(game, gender)
    if tab is None:
        print(f"  玩家起名的占位：剧本用到 {len(used)} 个 {{TEXTJOIN#n}}，文本包里没有候选表（旧包），留成占位、出字幕时删掉", flush=True)
        return None
    out = {str(n): tab[n] for n in used if n in tab}
    print(f"  玩家起名的占位：剧本用到 {len(used)} 个 {{TEXTJOIN#n}}，候选表里查到 {len(out)} 个", flush=True)
    return out or None


# ---------------------------------------------------------------- 短名词表（可选）

TERMS_TABLE = "terms"
"""文本包里的短名词对照表（game-text-data 的 `terms_*`：TextMap 里名词形的短串，一个 hash 一行，日中逐行 id 相同）。
**可选**：旧包没有它照读，剧本里就没有 `terms`，名牌层只查说话人表。"""
TERMS_CONTRACT = 1
"""短名词表认得的内容契约主版本（表在就要对上，见 `GAMES`）。"""
TERM_MIN_CONTENT = 2
"""查询键至少几个内容字（`cnorm` 之后）。名牌层会把噪音读数（`口`、`E`、`9`）当开头行，
一个字的键在 TextMap 里几乎总查得到，收进来就会把噪音"译"出来、在默认预设里画上板。"""
_KANA_HAN = re.compile(r"[぀-ヿ㐀-鿿]")


def term_key(t: str) -> str:
    """短名词表的查询键：NFKC、去空白——和 `script_align.norm` 对纯文本的结果相同，
    名牌层（`scriptmatch.term_names`）按那个归一查，两边键一致。"""
    return "".join(unicodedata.normalize("NFKC", t.replace("\\N", "")).split())


def term_table(game: str, obs_raw, gender: str) -> tuple[dict | None, dict]:
    """这段视频用得上的短名词：日文键（`term_key`）-> {`cn`, `n`（取中的中文对应几个 hash）, `of`（这个键共几个 hash）, `en`（同法取的英文，包里有英文那份时）}。

    * 只收 obs 里**逐字出现过**的键（归一后相等，内容 ≥`TERM_MIN_CONTENT` 字、含假名或汉字）：整张表二十多万条，
      名牌层只按屏幕上的字查，别的键写进剧本也用不上。
    * **消歧**：同一个日文键对几个中文时按 **hash 条数**取最多的那个（同一个中文串在 TextMap 里常有好几条，
      条数就是游戏里这种译法用得多不多）；打平取短的、再按字典序（确定性）。原神 7.0.0 短串的日文键里
      对多个中文的约 2.6%，抽样多是近义写法（`木製の橋` → 木质桥梁 13 / 木桥 6）。`n / of` 记下来，
      多数派占比低的那些下游可以另眼看。
    * 文本照剧本行一样 `clean`（性别写法、占位、标签）。
    返回 (表或 None——包里没有这张表, 统计)。"""
    if not bundle_of(game).has([TERMS_TABLE]):
        return None, {"table": False}
    bundle_of(game).require({TERMS_TABLE: TERMS_CONTRACT})
    want = term_wanted(obs_raw)
    out = pick_terms(paired(game, TERMS_TABLE), want, gender)
    amb = [v for v in out.values() if v["n"] < v["of"]]
    return out, {"table": True, "obs_keys": len(want), "keys": len(out), "ambiguous": len(amb),
                 "majority_below_60": sum(1 for v in amb if v["n"] < 0.6 * v["of"])}


def term_wanted(obs_raw) -> set[str]:
    """obs 文本里够格去查短名词表的键：内容 ≥`TERM_MIN_CONTENT` 字、含假名或汉字。"""
    want = set()
    for raw in obs_raw:
        k = term_key(raw)
        if len(cnorm(k)) >= TERM_MIN_CONTENT and _KANA_HAN.search(k):
            want.add(k)
    return want


def pick_terms(pairs, want: set[str], gender: str) -> dict[str, dict]:
    """(日文行, 中文行, 英文行或 None) 逐行 -> 键在 `want` 里的那些：按 hash 条数取最多的中文，打平取短的、再按字典序；
    英文只在中文取中的那些行里同法取（分开投会把另一个条目的英文配给这个中文），`n` / `of` 只记中文的。

    ⚠ 观察项（09-26 审计定级：中度，只在匹配器这一层）："短"的上限是各游戏说话人名的 P99（15 / 13 / 9），查询门、消歧规则都是手定；
    五段切片新译出的 54 块里真名牌 24、任务目标 / 奖励提示 30（game-text-corpus 报告 7.8）。看 `overlay.stats.name_from_terms` 和 `name_src = terms` 的块。"""
    cnt: dict[str, Counter] = defaultdict(Counter)
    cnt_en: dict[tuple[str, str], Counter] = defaultdict(Counter)    # (键, 中文) -> 那些行的英文
    for a, b, e in pairs:
        if not a["text"] or not b["text"]:
            continue
        k = term_key(clean(a["text"], gender))
        if k in want:
            cn = clean(b["text"], gender)
            if cn:
                cnt[k][cn] += 1
                if e and e["text"] and (en := clean(e["text"], gender)):
                    cnt_en[k, cn][en] += 1

    def pick(c: Counter) -> tuple[str, int]:
        return min(c.items(), key=lambda kv: (-kv[1], len(kv[0]), kv[0]))
    out = {}
    for k, c in sorted(cnt.items()):
        cn, n = pick(c)
        out[k] = {"cn": cn, "n": n, "of": sum(c.values()), **({EN: pick(cnt_en[k, cn])[0]} if cnt_en.get((k, cn)) else {})}
    return out


# ---------------------------------------------------------------- 检索

K = 5
"""n-gram 长度。日文 5 字的串在 30 万行里已经足够稀有；库侧隔一位取、查询侧逐位取，
对齐时至少有一半的查询 gram 落在库侧取过的位置上。"""
QMIN_FLOOR = K + 3
"""`--qmin` 的下限。查询 L 字有 L−K+1 个 gram、至少要中 2 个（`Index.query` 的 `need`），
而库侧只存隔一位的 gram：L = K+1 时两个 gram 相邻、库里至多存了一个，**永远命不中**；
L = K+2 时要看查询落在库行的奇偶位，一半的概率命不中。L ≥ K+3 才保证整行被包含时一定找得到。"""

_GRAM_BASE = np.uint64(0x9E3779B97F4A7C15)


def gram_hashes(s: str) -> np.ndarray:
    """s 里每个起点的 K-gram -> 64 位多项式哈希（按码位，uint64 自然回绕）。

    **跨进程稳定**：第一版用内置 `hash()`，它对 str 每个进程随机加盐——结果只在 gram 撞哈希时
    才依赖它，"重跑逐格相同"是实测出来的；换成确定的哈希，确定性就是可证的（audit-6）。"""
    c = np.frombuffer(s.encode("utf-32-le"), dtype=np.uint32).astype(np.uint64)
    n = len(c) - K + 1
    if n <= 0:
        return np.empty(0, dtype=np.uint64)
    h = np.zeros(n, dtype=np.uint64)
    for k in range(K):
        h = h * _GRAM_BASE + c[k:k + n]
    return h


class Index:
    def __init__(self, texts: list[str]):
        hs, ids = [], []
        for i, c in enumerate(texts):
            n = len(c)
            if n < K:
                continue
            pos = list(range(0, n - K + 1, 2))
            if pos[-1] != n - K:
                pos.append(n - K)
            hs.append(gram_hashes(c)[pos])
            ids.append(np.full(len(pos), i, dtype=np.int32))
        h = np.concatenate(hs) if hs else np.empty(0, dtype=np.uint64)
        order = np.argsort(h, kind="stable")
        self.h = h[order]
        self.ids = (np.concatenate(ids) if ids else np.empty(0, dtype=np.int32))[order]
        self.texts = texts

    def query(self, q: str, top: int = 6) -> list[int]:
        qh = gram_hashes(q)
        lo = np.searchsorted(self.h, qh, "left")
        hi = np.searchsorted(self.h, qh, "right")
        parts = [self.ids[a:b] for a, b in zip(lo, hi) if b > a]
        if not parts:
            return []
        # 同一个 gram 在同一行里出现多次只算一次，免得长重复行靠刷票上榜
        ids, cnt = np.unique(np.concatenate([np.unique(p) for p in parts]), return_counts=True)
        need = max(2, int(0.25 * len(qh)))
        ok = cnt >= need
        ids, cnt = ids[ok], cnt[ok]
        return [int(ids[j]) for j in np.argsort(-cnt, kind="stable")[:top]]


def contained(q: str, c: str) -> float:
    """q（一个 OCR 框 = 屏幕上的一行）有多少落在 c（库里一整条，可能跨两行）里。"""
    sm = SequenceMatcher(None, q, c, autojunk=False)
    return sum(b.size for b in sm.get_matching_blocks()) / max(1, len(q))


def obs_texts(obs: Path) -> dict[str, list[tuple[float, float]]]:
    """obs 里每条去重文本 -> 它出现过的全部 (时刻秒, 框中心 y / 画面高)。"""
    out: dict[str, list[tuple[float, float]]] = defaultdict(list)
    h = None
    with open(obs, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if "_meta" in r:
                h = r["_meta"].get("height")
                continue
            if not h:
                raise SystemExit(f"{obs} 的 _meta 没有画面高度（height），判不了字幕带（见 band_of）")
            b = r["box"]
            out[r["text"]].append((r["t_us"] / 1e6, (b[1] + b[3]) / 2 / h))
    return out


EPISODE_GAP = 3.0
"""相邻两次出现隔得不超过这么多秒，算同一次上屏（2 fps 采样，允许丢几帧）。"""


def episodes(times: list[float], gap: float = EPISODE_GAP) -> list[list[float]]:
    """一串时刻 -> 若干次上屏 [[起, 止]…]。同一行台词可能隔一个小时又出现一次（回想、
    重看），只记首末时刻会把两次当成一次。"""
    out: list[list[float]] = []
    for t in sorted(times):
        if out and t - out[-1][1] <= gap:
            out[-1][1] = t
        else:
            out.append([t, t])
    return out


BAND_WIN, BAND_PAD = 0.12, 0.03
"""字幕带的高（众数窗口）与两头的放宽，都按画面高算。原神 / 星铁两行字、绝区零四行字的对话框，
每行框中心都落在 0.12 以内（2026-09-11 四场整片：带内装下 822/1044、456/558、1667/1708、978/994 行）。"""
BAND_MIN_LINES = 5
"""锚住的行少于这么多就不估字幕带、不判 OffBand（众数窗口没有意义）。"""


def band_of(ys_per_line: list[list[float]]) -> tuple[float, float] | None:
    """这段视频的字幕带（画面高的比例，上 -> 下）：每行取锚点框 y 中心的中位数，
    找一个高 `BAND_WIN` 的窗口装下最多的行，两头各放宽 `BAND_PAD`。

    只用 obs，所以各臂共用；**不看任何一条臂的主轨在哪**（那样分母就偏向那条臂）。"""
    meds = sorted(float(np.median(ys)) for ys in ys_per_line if ys)
    if len(meds) < BAND_MIN_LINES:
        return None
    fit = [bisect_right(meds, m + BAND_WIN) - i for i, m in enumerate(meds)]
    i = fit.index(max(fit))
    return meds[i] - BAND_PAD, meds[i] + BAND_WIN + BAND_PAD


# ---------------------------------------------------------------- 主流程

def shown_twice(anchor: dict | None, span: float) -> bool:
    """obs 里这行确实上屏过两次：两次上屏之间隔了 ≥span 秒。只看首末时刻不行——
    一条挂在屏幕上一分多钟的台词会被当成"出现了两次"。"""
    eps = (anchor or {}).get("episodes") or []
    return any(b[0] - a[1] >= span for a, b in zip(eps, eps[1:]))


def mark_duplicates(out_units: list[dict], share: float, span: float) -> int:
    """后出现的单元若大半内容行和前面的单元重复，它多半是同一段对话的另一份拷贝
    （库里换条件的重演版本）——重复的行只在 obs 里确实出现过两次时才算。"""
    seen: set[str] = set()
    n = 0
    # 先按节点：原神同一个对话节点可以挂在两个 talk 下（gi1 的 500001 / 500017 共用 49 个
    # dialog_id），那就是同一行，后一份无条件记 Duplicate——否则 key 重复，后面按 key 查表会互相覆盖。
    node_keys: set[str] = set()
    for u in out_units:
        for ln in u["lines"]:
            if ln["key"] in node_keys:
                ln["status"] = "Duplicate"
                n += 1
            node_keys.add(ln["key"])
    for u in out_units:
        band = [ln for ln in u["lines"] if in_band(ln["kind"], ln["status"])]
        texts = [cnorm(ln["text"]) for ln in band]
        # "是不是拷贝"只按 ≥3 字的行判（`うん` 这种短句哪个单元都有，拿它投票会误判）；
        # 判定是拷贝之后，短行也照样按"前面出现过"标——同一份拷贝里的短句同样是重复的
        long = [k for k in texts if len(k) >= 3]
        if long and sum(k in seen for k in long) / len(long) >= share:
            for ln, k in zip(band, texts):
                if k in seen and not shown_twice(ln["anchor"], span):
                    ln["status"] = "Duplicate"
                    n += 1
        seen.update(k for k in texts if k)
    return n


def fold_variants(groups: list[list[dict]]) -> int:
    """每个变体组（输出行 dict 的列表，已去掉 Choice 和没播的）留**一个**代表行进分母，
    其余处境记 `Variant`、带 `variant_of`（代表行的 key）；返回记了几行。

    代表行挑"被唯一查询锚住的 > 被锚住的 > 组里第一个"——只影响报表里显示哪一种写法，
    不影响命中：`game_align` 那边任一种说法被认领都记在代表行上。"""
    n = 0
    for members in groups:
        if len(members) < 2:
            continue
        rep = min(members, key=lambda d: (not (d["anchor"] and d["anchor"]["unique"]),
                                          not d["anchor"], members.index(d)))
        for d in members:
            if d is not rep:
                d["status"], d["variant_of"] = "Variant", rep["key"]
                n += 1
    return n


SHORT_ANCHOR_PAD = 60.0
"""短行锚点：只认落在本单元锚点时段两头各放宽这么多秒之内的出现。"""


def short_anchors(units: list[Unit], picked: list[int], anchors: dict, qs_short: dict,
                  max_units: int, gender: str = "F", pad: float = SHORT_ANCHOR_PAD) -> int:
    """圈中单元里**没有锚点的短行**（内容不到 `--qmin` 字），在本单元的时段内按归一后**逐字相等**
    去 obs 里找；就地写进 `anchors`，返回找到几行。

    为什么这样才安全：短串在 30 万行的库里到处都是，所以它们不参与检索、不参与圈单元、
    也不参与估字幕带（`band_of` 在这之前就算完了）。但**单元一旦圈中，候选就只剩这几十行**，
    歧义没了——再排掉"同一串在 >`max_units` 个圈中单元里都有"的，剩下的是这一处的证据。

    收益（2026-09-11，整场绝区零）：现在记成漏的短行里 44 条能在**带外**找到（选项按钮：
    GalGame 居中、Chat 右侧），带内只有 3 条；这 44 条随后被 `mark_offband` 排出分母。
    没有这一步，它们只能以"太短判不了"挂在疑似真漏里（corpus）。"""
    where: dict[str, list[tuple[int, int]]] = defaultdict(list)
    span: dict[int, tuple[float, float]] = {}
    for ui in picked:
        eps = [e for li in range(len(units[ui].lines)) if (ui, li) in anchors
               for e in anchors[(ui, li)][0]]
        if not eps:
            continue
        span[ui] = (min(e[0] for e in eps) - pad, max(e[1] for e in eps) + pad)
        for li, ln in enumerate(units[ui].lines):
            if (ui, li) not in anchors:
                where[cnorm(clean(ln.raw, gender))].append((ui, li))
    n = 0
    for q, places in where.items():
        occ = qs_short.get(q)
        if not occ or len({ui for ui, _ in places}) > max_units:
            continue
        for ui, li in places:
            lo, hi = span[ui]
            got = [(t, y) for t, y in occ if lo <= t <= hi]
            if got:
                anchors[(ui, li)] = (episodes([t for t, _ in got]), 1, 1, [y for _, y in got])
                n += 1
    return n


def mark_offband(out_units: list[dict]) -> int:
    """还在分母里的行，若 obs 读到过它（唯一查询）而那些框**没有一个**落在字幕带里，记 `OffBand`；
    返回记了几行。变体组看整组：代表行没有位置证据时，拿同组另一种说法的。

    放在重演拷贝之后：拷贝的判定只看字幕带里的行，先判 OffBand 会让它少看几行、改了拷贝的结果。"""
    n = 0
    for u in out_units:
        members: dict[str, list[dict]] = defaultdict(list)
        for ln in u["lines"]:
            if ln.get("variant_of"):
                members[ln["variant_of"]].append(ln)
        for ln in u["lines"]:
            if not in_band(ln["kind"], ln["status"]):
                continue
            seen = [m["anchor"]["in_band"] for m in (ln, *members[ln["key"]])
                    if m["anchor"] and m["anchor"]["in_band"] is not None]
            if seen and not any(seen):
                ln["status"] = "OffBand"
                n += 1
    return n


SPAN_RUN = 5
SPAN_GAP_MIN, SPAN_PER_LINE = 300.0, 20.0


def unplayed(on: list[int], eps: dict[int, list], n: int) -> set[int]:
    """单元里**没在这段视频里播**的行号（记 OutOfSpan）：

    * 第一个锚点之前、最后一个之后（切片从对话中间开始 / 玩家中途离开）；
    * 中间一段：连续 ≥`SPAN_RUN` 行一个锚点都没有，而夹着它的两个锚点在时间上隔得比
      "这几行正常播完"远得多（> max(`SPAN_GAP_MIN`, `SPAN_PER_LINE` × 行数)），或者前后颠倒。

    第二条是 gi2 逼出来的：talk 400004 在 11,386 s 锚住开头几行，然后 58 行一个锚点都没有，
    下一个锚点在 16,479 s——中间 43 条内容行 OCR 一条都没读到过。一段真播过的对话，
    OCR 在任何区域都读不到其中任何一行，是不可能的；这是库里同一个 talk 的两截分别出现在
    视频里（或者其中一截是别处重复的句子锚上的），中间那截根本没播。
    只看 obs 锚点，所以各臂共用。`on` 是有锚点的行号（升序），`eps` 是它们的上屏时段。"""
    if not on:
        return set(range(n))
    out = set(range(on[0])) | set(range(on[-1] + 1, n))
    for i, j in zip(on, on[1:]):
        k = j - i - 1
        if k >= SPAN_RUN:
            # 两行各自可能上屏过几次：取"i 的某次结束 -> j 的某次开始"里最近的那一对
            fwd = [s - e for s, _ in eps[j] for _, e in eps[i] if s >= e]
            if not fwd or min(fwd) > max(SPAN_GAP_MIN, SPAN_PER_LINE * k):
                out.update(range(i + 1, j))
    return out


def sequence(out_units: list[dict], back: float, fwd: float) -> list[list]:
    """剧本行的**对齐序列**：[[key, 排序时刻, [[可认领起, 止]…]]…]，按 obs 时间排到行一级。

    * 有锚点的行：它的每一次上屏各外扩 `fwd` 秒，就是 cue 可以认领它的时段；
    * 没锚点的行（短句从来不拿去检索）：借本单元里**前面**最近那个有锚点的行的上屏时段，
      往后放宽 `back` 秒（它真正上屏只会更晚）；单元开头那几行借后面第一个，往前放宽。

    为什么不按单元排、也不只记一个时刻：一个 talk 可以拖很久（gi1 的 500010 锚点从 974 s
    一直到 3937 s，玩家中途去做别的），同一行也会隔很久再出现一次。这些时段给 `game_align`
    圈每条 cue 的候选（**只圈范围，命中与否仍看文本**）。只用 obs，所以各臂共用。
    重演拷贝（Duplicate）不进序列。"""
    items = []
    for ui, u in enumerate(out_units):
        eps = [ln["anchor"]["episodes"] if ln["anchor"] else None for ln in u["lines"]]
        on = [li for li, e in enumerate(eps) if e]
        for li, ln in enumerate(u["lines"]):
            if ln["status"] == "Duplicate":
                continue
            if eps[li]:
                src, win = eps[li], [[s - fwd, e + fwd] for s, e in eps[li]]
            elif on and on[0] < li:
                src = eps[max(j for j in on if j < li)]
                win = [[s, e + back] for s, e in src]
            elif on:
                src = eps[on[0]]
                win = [[s - back, e] for s, e in src]
            else:
                src, win = [[u["t0"], u["t0"]]], [[u["t0"] - back, u["t0"] + back]]
            items.append((src[0][0], ui, li, ln["key"], win))
    items.sort(key=lambda x: x[:3])
    return [[k, round(t, 2), [[round(a, 2), round(b, 2)] for a, b in w]] for t, _, _, k, w in items]


def library(game: str) -> tuple[list[Unit], list[tuple[int, int, str]], list[str], Index, str]:
    """读库 + 两种性别写法各归一一遍 + 建检索索引：(单元, flat, texts, 索引, 'cache hit|miss')。

    这三步只依赖**库文件和产剧本的代码**，不依赖 obs——原神一次 29 s 里占 ~28 s
    （2026-09-14 cProfile：读库 9.6 s、`clean`/`cnorm` ~6 s、`Index` 6.8 s），而同一次评测里库是常数。
    所以落盘到 `out/_cache/gamescript/`。键 = 游戏 + `code_fp(CODE_FP_FILES)` + 文本包的**内容指纹**：
    代码改了、库换了都换键；拷贝 / 解压包不改指纹，缓存照样命中。命中时照样核包里用到的文件的存储哈希
    （`verify_stored`，不解压，远比建库便宜）——否则清单完好、成员坏了的包在有缓存的机器上会静默通过。
    缓存只是加速，产物必须和不缓存时逐格相同（`--no-cache` 可对账）；整目录删掉就等于没有。"""
    import hashlib
    import os
    import pickle
    from flowocr.analyze import build_tracks as bt
    use_cache = USE_CACHE and game in GAMES      # 守卫里的合成库（`LOADERS["syn"]`）没有库文件，不缓存
    if not use_cache:
        return (*flatten(LOADERS[game]()), "no cache")
    b = bundle_of(game)
    key = hashlib.sha256(json.dumps([game, K, bt.code_fp(CODE_FP_FILES), b.fingerprint])
                         .encode()).hexdigest()[:16]
    cp = paths.data_root() / "out" / "_cache" / "gamescript" / f"{game}-{key}.pkl"
    if cp.exists():
        b.verify_stored(GAMES[game])
        if en := [t for t in GAMES[game] if b.has([t], (EN,))]:
            b.verify_stored(en, (EN,))
        with open(cp, "rb") as f:
            units, flat, texts, h, ids = pickle.load(f)
        idx = Index.__new__(Index)
        idx.h, idx.ids, idx.texts = h, ids, texts
        return units, flat, texts, idx, "cache hit"
    units, flat, texts, idx = flatten(LOADERS[game]())
    cp.parent.mkdir(parents=True, exist_ok=True)
    for old in cp.parent.glob(f"{game}-*.pkl"):     # 旧键永远不会再命中，留着只占盘
        old.unlink()
    tmp = cp.with_suffix(f".{os.getpid()}.tmp")     # 带 pid：两个进程同时建同一款游戏的缓存时不共用一个临时文件
    with open(tmp, "wb") as f:
        pickle.dump((units, flat, texts, idx.h, idx.ids), f, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(cp)
    return units, flat, texts, idx, "cache miss"


def flatten(units: list[Unit]) -> tuple[list[Unit], list[tuple[int, int, str]], list[str], Index]:
    """每行两种性别写法各归一一遍（写法不同才各占一格），再建检索索引。"""
    flat: list[tuple[int, int, str]] = []      # (unit 下标, line 下标, 性别)
    texts: list[str] = []
    for ui, u in enumerate(units):
        for li, ln in enumerate(u.lines):
            f, m = cnorm(clean(ln.raw, "F")), cnorm(clean(ln.raw, "M"))
            flat.append((ui, li, "F" if f != m else ""))
            texts.append(f)
            if f != m:
                flat.append((ui, li, "M"))
                texts.append(m)
    return units, flat, texts, Index(texts)


USE_CACHE = True
"""`library()` 用不用盘上缓存（`--no-cache` 关掉）。"""


def build(game: str, obs: Path, qmin: int, min_contain: float, max_units: int,
          min_anchors: int, solo_len: int, dup_share: float = 0.5, dup_span: float = 60.0,
          claim_back: float = 120.0, claim_fwd: float = 30.0, short_min: int = 4) -> dict:
    t0 = time.time()
    units, flat, texts, idx, cache = library(game)
    n_lines = sum(len(u.lines) for u in units)
    print(f"[{game}] 库：{len(units):,} 个对话单元 / {n_lines:,} 行；"
          f"索引 {len(idx.h):,} 个 {K}-gram（{cache}，{time.time()-t0:.0f}s）")

    ot = obs_texts(obs)
    qs: dict[str, list[tuple[float, float]]] = defaultdict(list)     # 归一后合并同一串
    qs_short: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for raw, occ in ot.items():
        q = cnorm(raw)
        if len(q) >= qmin:
            qs[q].extend(occ)
        elif len(q) >= short_min:
            qs_short[q].extend(occ)
    print(f"  obs：{len(ot):,} 条去重文本，其中内容 ≥{qmin} 字的 {len(qs):,} 条、"
          f"{short_min}–{qmin-1} 字的 {len(qs_short):,} 条（后者只给圈中单元里的短行当锚点）")

    hits: list[tuple[str, list[int], list]] = []   # (查询串, 命中的 flat 下标, 出现的 (时刻, y))
    n_hit = 0
    for q, tm in qs.items():
        sc = {j: contained(q, texts[j]) for j in idx.query(q)}
        ok = [j for j, c in sc.items() if c >= min_contain]
        if ok:
            n_hit += 1
            # 同一个单元里只留最像的那几行：一个框只是一行的一部分，公共部分会同时"包含于"
            # 两种说法（绝区零的リン / アキラ两行、原神的重演拷贝），都算锚点的话
            # 分支就判不出谁走过。
            best: dict[int, float] = {}
            for j in ok:
                best[flat[j][0]] = max(best.get(flat[j][0], 0.0), sc[j])
            ok = [j for j in ok if sc[j] >= best[flat[j][0]]]
            hits.append((q, ok, tm))
    print(f"  obs 文本在库里找得到的：{n_hit:,}/{len(qs):,}（{n_hit/max(1,len(qs)):.1%}）"
          f"（{time.time()-t0:.0f}s）")

    # 性别：只看两种写法读法不同的行，两种各算一次包含度，**高的那种**投一票、打平不投。
    # 第一版是"哪种写法进了候选就投哪种"，而性别差异通常只占一两个字，两种都过包含度门，
    # 于是两边都得票——gi1 投出 F 29 / M 18 这种没有意义的数。
    pair: dict[tuple[int, int], dict[str, int]] = defaultdict(dict)
    for j, (ui, li, g) in enumerate(flat):
        if g:
            pair[(ui, li)][g] = j
    votes = Counter()
    for q, ok, _ in hits:
        for key in {flat[j][:2] for j in ok if flat[j][2]}:
            cf, cm = (contained(q, texts[pair[key][g]]) for g in "FM")
            if cf != cm:
                votes["F" if cf > cm else "M"] += 1
    gender = "M" if votes["M"] > votes["F"] else "F"
    print(f"  主角性别写法投票：F {votes['F']} / M {votes['M']} -> 取 {gender}")

    # 锚点：每行记读到它的查询出现过的全部时刻与查询数；只挂在"唯一性够"的查询上的才用来圈单元。
    # 撞了 >max_units 个单元的常用句仍然记成锚点（时段、走没走分支、播没播都用它），
    # 但锚点上记下有没有唯一查询：只靠常用句锚住的行，"obs 读到过"说的是别处（audit-6）。
    # 框的位置（判字幕带）同理只记唯一查询的——常用句的框可能在画面任何地方
    anchors: dict[tuple[int, int], list] = {}
    unit_anchor_lines: dict[int, set] = defaultdict(set)
    unit_solo: set[int] = set()
    n_ambig = 0
    for q, ok, occ in hits:
        us = {flat[j][0] for j in ok}
        uniq = len(us) <= max_units
        n_ambig += not uniq
        for j in {flat[j][:2]: j for j in ok}.values():     # 两种性别写法算一行
            ui, li, _ = flat[j]
            v = anchors.setdefault((ui, li), [[], 0, 0, []])
            v[0].extend(t for t, _ in occ)
            v[1] += 1
            v[2] += uniq
            if uniq:
                v[3].extend(y for _, y in occ)
                unit_anchor_lines[ui].add(li)
                if len(q) >= solo_len:
                    unit_solo.add(ui)
    picked = [ui for ui, ls in unit_anchor_lines.items()
              if len(ls) >= min_anchors or ui in unit_solo or units[ui].solo]
    print(f"  圈中 {len(picked)} 个单元（≥{min_anchors} 行锚点，或一条 ≥{solo_len} 字的独占锚点）；"
          f"撞了 >{max_units} 个单元而不参与圈选的查询 {n_ambig} 条")

    anchors = {k: (episodes(ts), n, nu, ys) for k, (ts, n, nu, ys) in anchors.items()}
    # 字幕带只拿"本来就该在带里"的行估：库里已经知道不在带里的（选项、黑屏…）不投票
    band = band_of([anchors[(ui, li)][3] for ui in picked for li, ln in enumerate(units[ui].lines)
                    if (ui, li) in anchors and ln.kind not in NON_BAND_KINDS])
    print("  字幕带（obs 锚点框 y 中心的众数窗口，画面高的比例）："
          + (f"{band[0]:.3f}–{band[1]:.3f}" if band else f"锚住的行不到 {BAND_MIN_LINES} 条，不判"))
    n_short = short_anchors(units, picked, anchors, qs_short, max_units, gender)
    print(f"  圈中单元里的短行（{short_min}–{qmin-1} 字）：{n_short} 行在本单元的时段内按逐字相等找到了锚点"
          f"（不参与圈单元、也不参与估字幕带；作用是给它们位置证据与时段）")

    def first_t(ui):
        return min(anchors[(ui, li)][0][0][0] for li in range(len(units[ui].lines))
                   if (ui, li) in anchors)
    picked.sort(key=first_t)

    out_units = []
    n_variant = 0
    for ui in picked:
        u = units[ui]
        # 分支：独占集里有锚点的选项算走过
        walked: dict[str, set] = defaultdict(set)
        for li, ln in enumerate(u.lines):
            for g, o in ln.memberships:
                if ln.kind != "Choice" and (ui, li) in anchors:
                    walked[g].add(o)
        on = [li for li, ln in enumerate(u.lines) if ln.kind != "Choice" and (ui, li) in anchors]
        out_span = unplayed(on, {li: anchors[(ui, li)][0] for li in on}, len(u.lines))
        lines = []
        vgroups: dict[str, list[dict]] = defaultdict(list)
        for li, ln in enumerate(u.lines):
            status = None
            if ln.kind != "Choice" and li in out_span:
                status = "OutOfSpan"
            elif ln.kind != "Choice" and ln.memberships:
                status = branch_kind(ln.memberships, walked)
            text = clean(ln.raw, gender)
            if not text:
                continue
            a = anchors.get((ui, li))
            lines.append({"key": ln.key, "kind": ln.kind, "status": status, "role": ln.role, "speaker": ln.speaker,
                          "speaker_cn": ln.speaker_cn,
                          "text": text, "cn": (cn := clean(ln.cn or "", gender, keep=True) or None),
                          **({"jp_tpl": tpl} if (tpl := clean(ln.raw, gender, keep=True)) != text else {}),
                          **({"cn_rich": r} if cn and (r := clean(ln.cn, gender, rich=True)) != cn else {}),
                          **({EN: en} if (en := clean(ln.en or "", gender, keep=True) or None) else {}),
                          # 英文的注音不带：原神标在单个拉丁字母上（`rule-b{RUBY#[S]Varka}reaker`），按字算中心的写法放不对
                          **({"en_rich": r} if en and (r := clean(RUBY_RE.sub("", ln.en), gender, rich=True)) != en else {}),
                          **({"speaker_en": ln.speaker_en} if ln.speaker_en else {}),
                          **({"jp_rich": r} if (r := clean(ln.raw, gender, rich=True)) != tpl else {}),
                          "anchor": None if a is None else
                          {"t0": a[0][0][0], "t1": a[0][-1][1], "queries": a[1],
                           "unique": a[2] > 0,
                           "y": round(float(np.median(a[3])), 3) if a[3] else None,
                           "in_band": None if not (a[3] and band) else
                           any(band[0] <= y <= band[1] for y in a[3]),
                           "episodes": [[round(s, 2), round(e, 2)] for s, e in a[0]]}})
            if ln.variant and ln.kind != "Choice" and status != "OutOfSpan":
                vgroups[ln.variant].append(lines[-1])
        n_variant += fold_variants(list(vgroups.values()))
        out_units.append({"uid": u.uid, "title": u.title,
                          "t0": round(first_t(ui), 2), "lines": lines})
    n_dup = mark_duplicates(out_units, dup_share, dup_span)
    for u in out_units:
        # 代表行被判成重演拷贝时，同组的另一种说法也是拷贝——否则它指向一条不在对齐序列里的行
        dup = {ln["key"] for ln in u["lines"] if ln["status"] == "Duplicate"}
        for ln in u["lines"]:
            if ln.get("variant_of") in dup:
                ln["status"] = "Duplicate"
                n_dup += 1
    n_off = mark_offband(out_units)
    kinds = Counter(ln["kind"] for u in out_units for ln in u["lines"])
    statuses = Counter(ln["status"] for u in out_units for ln in u["lines"] if ln["status"])
    if n_variant:
        print(f"  变体组：{n_variant} 行记成 Variant（同一句话的另一种说法，一组只算一个分母条目）")
    print(f"  重演拷贝：{n_dup} 行记成 Duplicate（和前面单元重复的内容行占本单元 ≥{dup_share:.0%}，"
          f"且 obs 里只上屏过一次）")
    print(f"  不在字幕带：{n_off} 行记成 OffBand（锚住它的框没有一个落在字幕带里）")
    print("  圈出的剧本按类型：" + "  ".join(f"{k} {v}" for k, v in kinds.most_common()))
    print("  其中有处境的（不进分母）：" + "  ".join(f"{k} {v}" for k, v in statuses.most_common()))
    # 整库的说话人 日文 -> 中文（同名取出现最多的写法）：叠加 ASS 名牌层的译文从这里取。只看圈中的剧本行不够——
    # 原神任务对话的剧本行多半没有说话人，gi-s2 的 4 个名牌只看剧本行一个都翻不出，整库查得到 3 个（2026-09-14）
    spk: dict[str, Counter] = defaultdict(Counter)
    spk_en: dict[str, Counter] = defaultdict(Counter)
    for u in units:
        for ln in u.lines:
            if ln.speaker and ln.speaker_cn:
                spk[ln.speaker][ln.speaker_cn] += 1
            if ln.speaker and ln.speaker_en:
                spk_en[ln.speaker][ln.speaker_en] += 1
    # 说话人表查不到的名牌（原神隐藏名字的 NPC、星铁 / 绝区零没有说话人的场景）再查短名词表
    terms, tstat = term_table(game, ot, gender) if game in GAMES else (None, {"table": False})
    print("  短名词表：" + (f"obs 里 {tstat['obs_keys']:,} 个候选键、查到 {tstat['keys']:,} 个，"
                           f"其中对多个中文的 {tstat['ambiguous']}（多数派 <60% 的 {tstat['majority_below_60']}）"
                           if tstat["table"] else "文本包里没有（旧包），名牌层只查说话人表"))
    textjoins = used_textjoins(game, out_units, gender) if game in GAMES else None
    return {"units": out_units, "sequence": sequence(out_units, claim_back, claim_fwd),
            "speakers": {k: c.most_common(1)[0][0] for k, c in sorted(spk.items())},
            **({"speakers_en": {k: c.most_common(1)[0][0] for k, c in sorted(spk_en.items())}} if spk_en else {}),
            **({"terms": terms} if terms is not None else {}),
            **({"textjoins": textjoins} if textjoins else {}),
            "gender": gender, "gender_votes": dict(votes),
            "stats": {"db_units": len(units), "db_lines": n_lines,
                      "obs_texts": len(ot), "queries": len(qs), "queries_hit": n_hit,
                      "queries_ambiguous": n_ambig, "units_picked": len(picked),
                      "short_anchors": n_short, "terms": tstat,
                      "band": [round(band[0], 3), round(band[1], 3)] if band else None,
                      "kinds": dict(kinds), "statuses": dict(statuses)}}


def main() -> int:
    ap = argparse.ArgumentParser(prog="flowocr-gamescript", description="从游戏文本包里圈出这段视频打过的对话，写成剧本 JSON（匹配原文用）。"
                                             "设计与判据见本模块的文档串。")
    ap.add_argument("obs", help="第 1 步的观测 obs.jsonl（圈对话单元只用它）")
    ap.add_argument("--out", required=True, help="剧本 JSON")
    ap.add_argument("--game", choices=sorted(GAMES), default=None,
                    help="默认按 obs 文件名前缀猜（gi/hsr/zzz）")
    ap.add_argument("--qmin", type=int, default=8,
                    help="obs 文本内容字数达到这么多才拿去检索。短串在 30 万行里到处都是")
    ap.add_argument("--min-contain", type=float, default=0.85,
                    help="obs 文本有多大比例落在库里那一行里才算读到（一个框只是一行的一部分）")
    ap.add_argument("--max-units", type=int, default=3,
                    help="一条查询撞中超过这么多个单元，就不拿它圈单元（常用句）")
    ap.add_argument("--min-anchors", type=int, default=2,
                    help="一个单元至少有几行被锚住才算这段视频打过它")
    ap.add_argument("--solo-len", type=int, default=20,
                    help="或者：只有一行锚住，但那条查询内容 ≥ 这么多字且独占")
    ap.add_argument("--short-anchor-min", type=int, default=4,
                    help="圈中单元里的短行：内容达到这么多字才拿去和 obs 逐字比（见 short_anchors）")
    ap.add_argument("--claim-back", type=float, default=120.0,
                    help="没锚点的行借前一个锚点的上屏时段，往后放宽这么多秒（见 sequence）")
    ap.add_argument("--claim-fwd", type=float, default=30.0,
                    help="有锚点的行的每次上屏两头各外扩这么多秒（主轨 cue 可能早于 obs 锚点）")
    ap.add_argument("--no-cache", action="store_true",
                    help="读库和建索引不走 out/_cache/gamescript（对账用，见 library）")
    ap.add_argument("--gametext", default=None,
                    help="文本包：一个 .zip、带 manifest.json 的目录、或装着若干包的目录（默认 paths.gametext_root()，"
                         "即 FLOWOCR_GAMETEXT 或数据根的 gametext/）")
    a = ap.parse_args()
    global USE_CACHE, GAMETEXT
    USE_CACHE = not a.no_cache
    GAMETEXT = a.gametext
    if a.qmin < QMIN_FLOOR:
        ap.error(f"--qmin 不能小于 {QMIN_FLOOR}：更短的查询在检索这一步命不中或只命中一半（见 QMIN_FLOOR）")

    obs = Path(a.obs)
    game = a.game or TAG_GAME.get(obs.stem.split("-")[0].rstrip("0123456789"))
    if not game:
        raise SystemExit(f"从 {obs.name} 猜不出是哪款游戏，给 --game")
    doc = build(game, obs, a.qmin, a.min_contain, a.max_units, a.min_anchors, a.solo_len,
                claim_back=a.claim_back, claim_fwd=a.claim_fwd, short_min=a.short_anchor_min)
    from flowocr.analyze import build_tracks as bt
    gt = bundle_of(game).provenance()
    print(f"  文本包：{gt['source']}（{game} {gt['version']}，指纹 {gt['fingerprint'][7:19]}）")
    if not gt["integrity_checked"]:
        print("  ⚠ 文本包：上游快照的完整性检查没过或没做（工作区是否等于所记 commit，见包里 report.json 的 upstream.integrity_warnings）")
    doc = {"schema": SCHEMA,
           "provenance": {"tool": "gamescript.py", "version": bt.version(), "git_head": bt.git_head(),
                          "code_fp": bt.code_fp(CODE_FP_FILES),
                          "argv": [bt.portable(x) for x in sys.argv[1:]], "obs": bt.portable(obs),
                          "obs_mtime": obs.stat().st_mtime, "game": game,
                          "gametext": gt,
                          "params": {"qmin": a.qmin, "min_contain": a.min_contain,
                                     "max_units": a.max_units, "min_anchors": a.min_anchors,
                                     "solo_len": a.solo_len, "K": K, "episode_gap": EPISODE_GAP,
                                     "band_win": BAND_WIN, "band_pad": BAND_PAD,
                                     "short_anchor_min": a.short_anchor_min,
                                     "short_anchor_pad": SHORT_ANCHOR_PAD,
                                     "claim_back": a.claim_back, "claim_fwd": a.claim_fwd}},
           **doc}
    p = Path(a.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"-> {p}")
    return 0


if __name__ == "__main__":
    # **先按模块名导入自己再跑**：直接跑 main() 的话 `Unit` / `Line` 挂在 `__main__` 下，
    # `library()` pickle 进缓存的类名就是 `__main__.Unit`——之后别的工具 `import gamescript` 再读同一份缓存，
    # 按 `__main__` 找不到类，当场 AttributeError（2026-09-14 复查 try-list 时实测出来的）
    from flowocr.analyze import gamescript
    raise SystemExit(gamescript.main())
