"""内置匹配器：**游戏文本库**（原神 / 星铁 / 绝区零的文本包，`gtd-bundle/1`，由 `gamescript` 读成剧本；game-text-corpus 报告）。

两个入口（`flowocr.extensions`）：

* `cluster_patch(context, options)`：按游戏给默认聚类器（build_tracks）的参数覆盖。表就是 `PATCHES`
  （原 `tools/game_patches.py`，2026-09-22 搬进来；那个文件现在是把字典翻成 CLI 参数的薄适配）。
  `context` 给 `game`（`genshin` / `starrail` / `zzz`）或 `tag`（素材标签，前缀认游戏，同 `gamescript.TAG_GAME`）。
* `match(document, context, options)`：`document` 是 `*-tracks.json` 的 dict；`context["ref"]` 是 `gamescript` 产的剧本 JSON 路径，
  `context["tracks_path"]` 是那份 tracks 的路径（记进 provenance；`feed == "main"` 时从它取主轨 SRT）。
  返回 `*-matched.json` 的 dict（`scriptmatch.SCHEMA`）。判据和 CLI `scriptmatch.py` 是同一份 `run_match`。

`options` 的键和默认值同 `scriptmatch.py` 的旋钮：`feed`（nonoise）、`feed_panel`（keep）、`fuzzy`（0.75）、
`merge_gap`（`scriptmatch.MERGE_GAP`）、`overlay`（True）、`readables`（False，阅读面板长文本，要文本包有 readables 表）/ `obs`（None）。
"""
from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

from flowocr import placeholders as PH
from flowocr.analyze import game_align as GA
from flowocr.analyze import gamescript as GS
from flowocr.analyze import scriptmatch as SM

PATCHES: dict[str, dict] = {
    # 星铁：窗内并区的时间门。遗器故事的阅读面板和字幕带**从不同屏**，却因为空间上挨着被并查集
    # 串成一块（game-text-corpus 报告）；这道门要求"同屏过 or 时段交错 ≥2 次"才连边。
    # 整场 hsr +34 条命中，cue 1,645 → 1,450、对不上剧本 344 → 269、重复认领 811 → 654，两段切片 ±0。
    # **原神 / 绝区零不开**：那两款上是 −49 / −58 / −30，病在"主轨只能挑一个区域"接不住被分开的带。
    "starrail": {"region_time_gate": 2},
}
"""游戏 -> build_tracks 的参数覆盖。键是参数名（`region_time_gate`），不是 CLI 旗标。"""


def _flag(v) -> bool:
    """`options` 的值可能是命令行来的**字符串**（`--opt overlay=0`）——`bool("0")` 是 True，
    静默把"关掉"读成"开着"。这里按字面认（2026-09-22）。"""
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(v)


def game_of(context: dict) -> str | None:
    """`context["game"]` 优先；否则按素材标签前缀认（`gi-s1` → genshin），认不出就 None。"""
    game = context.get("game")
    if game:
        return str(game)
    tag = str(context.get("tag") or "")
    return GS.TAG_GAME.get(tag.split("-")[0].rstrip("0123456789")) if tag else None


def cluster_patch(context: dict, options: dict | None = None) -> dict:
    """这款游戏要给默认聚类器的参数覆盖；没有就是空字典（不改默认）。"""
    return dict(PATCHES.get(game_of(context) or "", {}))


def match(document: dict, context: dict, options: dict | None = None) -> dict:
    """见文件头。`document` 不动；返回新的 matched dict。"""
    o = dict(options or {})
    ref_path = context.get("ref")
    if not ref_path:
        raise ValueError("gametext 匹配器要 context['ref']（gamescript 产的剧本 JSON 路径）")
    ref = GA.load_ref(Path(ref_path))
    tracks_path = str(context.get("tracks_path") or "")
    params = {"feed": o.get("feed", "nonoise"), "feed_panel": o.get("feed_panel", "keep"), "readables": _flag(o.get("readables", False)),
              "fuzzy": float(o.get("fuzzy", 0.75)), "merge_gap": float(o.get("merge_gap", SM.MERGE_GAP))}
    res = SM.run_match(ref, tracks_path, params["feed"], params["feed_panel"], params["fuzzy"], params["merge_gap"],
                       overlay=_flag(o.get("overlay", True)), readables=params["readables"],
                       obs=o.get("obs"), doc=document, events=needs_events(ref), layout=context.get("layout"), viewport=context.get("viewport"))
    st = SM.match_stats(ref, res["recs"], res["suspect_raw"])
    return SM.matched_doc(ref, res["recs"], res["n_raw"], st, str(ref_path), res["subs_path"], res["tp"],
                          list(context.get("argv") or []), params, res["items"], res["ov_stats"], res["evs"], res["textbox"],
                          fills(ref, res["recs"], res["evs"]), res["name_ink"])


# ---------------------------------------------------------------- 占位的值（owner 2026-10-05）
# 共享层（`flowocr.placeholders`）只认花括号；这款匹配器猜两种：玩家起的名字 `{NICKNAME}`（三款游戏同一个写法），
# 和星铁玩家从候选里选（或自定义）的名字 `{TEXTJOIN#n}`（`textjoin_fills`）。别的占位（流浪者的名字、运行时取值）不猜，阶段 4 删掉。
# 按性别取的称谓不是占位：剧本阶段已按称谓表换成性别写法（`gamescript.sexpro`）。

def needs_events(ref: GA.Ref) -> bool:
    """猜玩家名要不要每条 cue 的事件：只有原神读名牌要（`--no-overlay` 时 `run_match` 据此决定读不读 tracks）。"""
    return (ref.doc.get("provenance") or {}).get("game") == "genshin"


def fills(ref: GA.Ref, recs: list[dict], evs: list[list[dict]] | None) -> dict:
    """匹配产物的 `fills`：认上的剧本行里有 `{NICKNAME}` 时才猜（`infer`），给 `{"{NICKNAME}": {"value", "how", 票数…}}`；没有就是空字典。
    两个来源，整部片一起投票：①认上的剧本行原文模板（`jp_tpl`）里有玩家名时，模板在它前后的字在 OCR 原文里找得到，夹在中间的就是名字（`guess`）；
    ②**只给原神**（owner 2026-10-05：按版式认名牌有过拟合之嫌，限在原神）：认上主角自己说的台词（剧本里角色是 PLAYER）时，
    那条 cue 里建轨打了 `nameplate` 标记的事件的文字就是玩家起的名字，算一票（gi1 整场带占位的台词只认上 1 句，主角台词的名牌事件 6 个候选、5 票有效）。"""
    lines = {ln["key"]: ln for u in ref.doc["units"] for ln in u["lines"]}
    got = [r for r in recs if r.get("ref") and r["ref"].get("key") in lines]
    if not any(PH.PLAYER in (lines[r["ref"]["key"]].get(k) or "") for r in got for k in ("cn", "en", "jp_tpl")):
        return textjoin_fills(ref, got, lines)
    pairs = [(r["ocr"], lines[r["ref"]["key"]]["jp_tpl"]) for r in got
             if PH.PLAYER in (lines[r["ref"]["key"]].get("jp_tpl") or "") and r.get("ocr")]
    plates = []
    if needs_events(ref) and evs:
        for r in got:
            if lines[r["ref"]["key"]].get("role") == "PLAYER":
                plates += list(dict.fromkeys(e["text"] for i in r["cues"] if i < len(evs) for e in evs[i]
                                             if "nameplate" in (e.get("flags") or [])))
    inf = infer(pairs, plates)
    inf["how"] = (f"从视频原文读出（{inf['votes']}/{inf['of']} 票）" if inf["value"] else
                  f"视频原文里读不出（{inf['of']} 票不够）")
    return {PH.PLAYER: inf, **textjoin_fills(ref, got, lines, inf["value"])}


def textjoin_fills(ref: GA.Ref, got: list[dict], lines: dict, nickname: str | None = None) -> dict:
    """星铁 `{TEXTJOIN#n}`：剧本的 `textjoins` 给了候选（gamescript 从文本包取）时，按视频认玩家选的是哪个（`pick_item`），整部片一起投票；
    得票最多的严格过半就采用（候选的票按 cue 算、1 票就够——`pick_item` 分不清时不投；自定义的名字按不同的剧本行算、要 ≥ `MIN_VOTES` 票），
    否则用默认项。候选各语种不同，记 `by_lang`；自定义的名字只有玩家打的那一串，记 `value`（中文输出也用它）。没有候选（旧包）的不猜。
    候选里嵌着玩家名（`{NICKNAME}`，n=22 的默认项）时换成 `nickname`，读不出就删掉；别的占位删掉。"""
    tjs = ref.doc.get("textjoins") or {}
    used = sorted({n for r in got for k in ("cn", "jp_tpl", "cn_rich", "jp_rich", "en", "en_rich")      # 带样式的那份：只出现在 Ruby 读音里的
                   for n in GS.TEXTJOIN_RE.findall(lines[r["ref"]["key"]].get(k) or "")}, key=int)
    out = {}
    for n in used:
        e = tjs.get(n)
        if not e:
            continue
        tag = f"{{TEXTJOIN#{n}}}"
        picks = {(r["ref"]["key"], i, v) for i, r in enumerate(got)
                 if r.get("ocr") and tag in (lines[r["ref"]["key"]].get("jp_tpl") or "")
                 if (v := pick_item(r["ocr"], lines[r["ref"]["key"]]["jp_tpl"], e["jp"], tag)) is not None}
        # 候选的票按 cue 算；自定义的名字按剧本行算（同一行出两条 cue、同一个错字读两遍不该凑够票）
        votes = Counter(v for _, _, v in picks if v[0] == "item")
        votes.update(v for _, v in {(k, v) for k, _, v in picks if v[0] == "custom"})
        of = sum(votes.values())
        top = votes.most_common(3)
        win = top[0] if top and top[0][1] > SHARE * of and (top[0][0][0] == "item" or top[0][1] >= MIN_VOTES) else None
        stat = {"votes": win[1] if win else 0, "of": of, "top": [[list(k), c] for k, c in top]}
        def item(i):
            return {k: _filled(e[k][i], nickname) for k in ("jp", "cn", "en") if k in e}
        if win and win[0][0] == "item":
            i = win[0][1]
            out[tag] = {"value": None, "by_lang": item(i),
                        "how": f"从视频原文认出候选「{e['jp'][i]}」（{win[1]}/{of} 票）", **stat}
        elif win:
            out[tag] = {"value": win[0][1], "how": f"从视频原文读出自定义的名字（{win[1]}/{of} 票）", **stat}
        elif e.get("default") is not None and any(item(e["default"]).values()):
            out[tag] = {"value": None, "by_lang": item(e["default"]),
                        "how": f"视频原文里没认出（{of} 票不够），用默认值", **stat}
        else:
            out[tag] = {"value": None, "how": f"视频原文里没认出（{of} 票不够），默认项没有文字，删掉", **stat}
    return out


def _filled(t: str | None, nickname: str | None) -> str | None:
    """候选文本里的占位：玩家名换成读出的名字（读不出就删），别的删掉。"""
    if not t:
        return None
    return PH.drop(t.replace(PH.PLAYER, nickname) if nickname else t) or None


_SPLIT = re.compile(r"[、，,・。！？!?…—\s]+")
"""截出的名字按这些分隔号切开再比（连着的占位之间、连喊几遍的名字之间是这些：`ペペ！ペペ`）；不按任意符号切：OCR 把一个字读成符号时会切出另一个候选（`次■プーマン` → `プーマン`）。"""
FUZZY = 0.75
"""截出的名字和候选的相似度（`SequenceMatcher.ratio`）到这么多才算这个候选（同匹配器 `--fuzzy` 的默认）：OCR 错一个字、截掉一个字也认得出。"""
FUZZY_GAP = 0.05
"""第一名比第二名高不到这么多就算分不清、不投票（`次元プーマン` 截掉首字成 `元プーマン`，和 `プーマン` 只差 0.02）：宁可少一票，不投错。"""
UNSURE = 0.5
"""和某个候选的相似度在 [`UNSURE`, `FUZZY`) 之间算分不清、不投票，离所有候选都远才算自定义的名字：
两三个字的候选读错一个字只有 0.5–0.67（`プー神` 读成 `プー袖`），当成自定义会把日文错字填进中文字幕。"""


def pick_item(ocr: str, tpl: str, cands: list, tag: str) -> tuple | None:
    """一条：这句 OCR 原文里玩家选的是哪个候选。返回 `("item", 下标)` / `("custom", 名字)` / None（认不出或分不清，不投票）。

    * 先按模板锚点截出名字（`guess`）。截出的那段可能连着旁边的占位（`{TEXTJOIN#54}、{NICKNAME}、…` 截出 `プーマン、星`），
      所以按标点切开，每段（连同整段）和每个候选比相似度，取各候选的最高分：第一名 ≥ `FUZZY` 且比第二名高出 `FUZZY_GAP` 才算它；
      第一名够分但分不清是 None；离所有候选都远（相似度都不到 `UNSURE`、也不含任何候选）才是玩家自定义的名字，其余算分不清。
    * 截不出（锚点被 OCR 读错）就在整句里逐字找候选，**剧本固定文字里本来就有的候选不算**（`このプーマンは…` 里的 `プーマン`）；
      找到几个时，其余都是最长那个的一部分（`扑満` 在 `次元扑満` 里）才取最长的，否则分不清；命中的是更长候选的一截、
      而周围的字又像那个更长的候选（读掉一个字的 `元プーマン`）也算分不清。"""
    norm = [_strip(PH.PLACEHOLDER_RE.sub("", c)) if c else "" for c in cands]
    name = guess(ocr, tpl, tag)
    if name:
        parts = {_strip(p) for p in [name, *_SPLIT.split(name)] if _strip(p)}
        score = [max((SequenceMatcher(None, c, p, autojunk=False).ratio() for p in parts), default=0.0) if c else 0.0
                 for c in norm]
        order = sorted(range(len(norm)), key=lambda i: -score[i])
        if not order or score[order[0]] < FUZZY:
            whole = _strip(name)
            if (order and score[order[0]] >= UNSURE) or any(c and c in whole for c in norm):
                return None                              # 像某个候选又不够像（短候选读错一个字 `プー袖`）、或挤着候选（`1{44}{45}枚` 截出 `37`）：分不清
            return ("custom", name)
        if len(order) > 1 and score[order[0]] - score[order[1]] < FUZZY_GAP:
            return None
        return ("item", order[0])
    fixed = _strip(PH.PLACEHOLDER_RE.sub("", tpl))
    s = _strip(ocr)
    hit = [i for i, c in enumerate(norm) if c and c in s and c not in fixed]
    if not hit:
        return None
    best = max(hit, key=lambda i: len(norm[i]))
    if not all(norm[i] in norm[best] for i in hit):
        return None
    pos = s.find(norm[best])
    for j, c2 in enumerate(norm):                        # 命中的是更长候选的一截、周围又像那个更长的（`次元プーマン` 读掉首字成 `元プーマン`）：分不清
        if j != best and c2 and norm[best] in c2 and len(c2) > len(norm[best]):
            a = pos - c2.find(norm[best])
            if SequenceMatcher(None, c2, s[max(0, a):a + len(c2)], autojunk=False).ratio() >= FUZZY:
                return None
    return ("item", best)


NAME_MAX = 16
ANCHOR = 3
"""模板在占位前后各取这么多个字去 OCR 原文里找（不算标点空白）：太短会撞上别处，太长 OCR 错一个字就找不到。"""
MIN_VOTES = 2
SHARE = 0.5
SEPS = " 　\n、，,。！？!?…:："
"""名字首尾去掉的分隔号（括号不去：玩家起的名字里可以有）。"""
_PUNCT = re.compile(r"[\s\W_]+")


def _strip(s: str) -> str:
    return _PUNCT.sub("", s)


def guess(ocr: str, tpl: str, tag: str = PH.PLAYER) -> str | None:
    """一条：OCR 原文 + 带占位 `tag`（默认 `{NICKNAME}`）的原文模板 -> 夹在中间的名字（找不到是 None）。只看第一个；锚点只取到最近的另一个占位为止。
    占位后面的文字（去掉标点空白后的前 `ANCHOR` 个字）在 OCR 里的位置是名字的右界；占位前面有字时，它的后 `ANCHOR` 个字是左界，
    没有就从 OCR 开头算。名字去掉首尾的分隔号，长度 1..`NAME_MAX`。"""
    tpl = tpl.replace("\\N", "\n")                       # 剧本的条内换行是 `\\N`，OCR 原文是真换行：不换的话 `N` 会混进锚点
    pre, _, post = tpl.partition(tag)
    # 锚点只取到最近的另一个占位为止（同一个占位再出现也算）：那里是另一个名字，截进来会成 `星！星`、`モフモフ、星`；
    # 那一侧夹着的字不够 `ANCHOR` 个就不截（候选还能走整句查找）
    *_, pre_cut = PH.PLACEHOLDER_RE.split(pre)
    post_cut, *rest = PH.PLACEHOLDER_RE.split(post)
    if (pre_cut != pre and len(_strip(pre_cut)) < ANCHOR) or (rest and len(_strip(post_cut)) < ANCHOR):
        return None
    pre, post = pre_cut, post_cut
    pre_k, post_k = _strip(pre)[-ANCHOR:], _strip(post)[:ANCHOR]
    if not post_k and not pre_k:
        return None
    flat, idx = [], []                       # OCR 去掉标点空白后的字，和它们在原文里的位置
    for i, c in enumerate(ocr):
        if not _PUNCT.fullmatch(c):
            flat.append(c)
            idx.append(i)
    s = "".join(flat)
    lo = 0
    if pre_k:
        j = s.find(pre_k)
        if j < 0:
            return None
        lo = j + len(pre_k)
    hi = len(s)
    if post_k:
        j = s.find(post_k, lo)
        if j < 0:
            return None
        hi = j
    if hi <= lo:
        return None
    end = idx[hi] if hi < len(idx) else len(ocr)          # 截到后半段锚点开始处：名字里的括号（`ねるめろ(本物)`）不当标点去掉
    start = idx[lo]
    if not pre_k:                                          # 没有左锚：只在右锚那一行里找（前面几行是名牌，`派蒙\nねるめろ、…` 别切进名字）
        start = max(start, ocr.rfind("\n", 0, end) + 1)
    name = _unpaired(ocr[start:end].strip(SEPS))
    return name if 0 < len(name) <= NAME_MAX and "\n" not in name else None


PAIRS = {"」": "「", "』": "『", "）": "（", ")": "(", "】": "【"}


def _unpaired(name: str) -> str:
    """名字首尾没配对的括号去掉（`「{X}」、…` 截出 `ミミ」`）；配对的留着（`ねるめろ(本物)`）。"""
    while name and name[-1] in PAIRS and PAIRS[name[-1]] not in name:
        name = name[:-1].strip(SEPS)
    while name and name[0] in PAIRS.values() and {v: k for k, v in PAIRS.items()}[name[0]] not in name:
        name = name[1:].strip(SEPS)
    return name


def name_ok(name: str) -> str | None:
    """一个候选名字（名牌事件的文字）能不能算一票：去掉首尾分隔号后长度 1..`NAME_MAX`、不带换行、不只是数字 / 单个拉丁字母（键位提示）。"""
    name = (name or "").strip(SEPS)
    if not 0 < len(name) <= NAME_MAX or "\n" in name or re.fullmatch(r"[0-9]+|[A-Za-z]", name):
        return None
    return name


def infer(pairs: list[tuple[str, str]], plates: list[str] = ()) -> dict:
    """整部片投票：`pairs` 是（OCR 原文, 带 `{NICKNAME}` 的原文模板），`plates` 是认上主角台词时那条 cue 里名牌事件的文字（`name_ok` 过滤）。
    得票最多的名字票数 ≥ `MIN_VOTES`、**严格**超过全部读得出名字的 `SHARE` 才采用（平票不采用：不然取哪个由读到的先后决定）。
    返回 `{"value": 名字或 None, "votes": 得票, "of": 读得出名字的条数, "pairs": 模板条数, "plates": 主角台词条数, "top": 前几名}`。"""
    got = Counter(n for o, t in pairs if (n := guess(o, t)))
    got.update(n for t in plates if (n := name_ok(t)))
    top = got.most_common(5)
    of = sum(got.values())
    name = top[0][0] if top and top[0][1] >= MIN_VOTES and top[0][1] > SHARE * of else None
    return {"value": name, "votes": top[0][1] if top else 0, "of": of, "pairs": len(pairs),
            "plates": len(plates), "top": top}

