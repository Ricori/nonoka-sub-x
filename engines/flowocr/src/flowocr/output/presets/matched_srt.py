"""内置预设：把**匹配产物**（`*-matched.json`）拍平成一份 SRT——一条记录一条 cue，认领上的出剧本文本，其余按可见性留 OCR。

和 `scriptmatch --srt` 写的**逐字节相同**（它就调这个预设；同一份 `cues`、同一个 `matchedio.body`、同一个 `srtio` 写出）。
⚠ SRT 是**拍平投影，只出主 ref**：覆盖（ref ∪ extra）比这里出现的多，量覆盖看匹配产物本身（matcher 计划）。
剧本里的占位（`{NICKNAME}` 这类）标成 `` [`编号:值] ``、不自动替换（SRT 没有阶段 4）：打一段提示，用户搜 `` [` `` 逐处改。

出不出一条记录（显示协议，可见集合同叠加预设，`script.matched_visible`）：
* 有 `ref` 的：`ref` 可见（组可见，且不是填位、或填位可见）才出；在所选语种有文本就出剧本文本，没有的话只在 `unmatched` 组可见时出 OCR 原文；
* `ref` 为空的记录归 `unmatched`，这一组可见时出 OCR 原文；
* 特殊上屏形式的台词与提示（`ref` 的组是 `matchedio.SPECIAL`）一律不出，`show=special` / `show=all` 也不出，也不退回出 OCR 原文
  （SRT 是对白 sidecar，owner 2026-11-07；字幕稿和叠加照画）；判在可见性这一步，回显去重把它当"主没出"；
* 回显去重叠在可见性之后：主那次认领是某条记录的 `ref`、**而且那条记录这次出了**，才去掉这条回显（`matchedio.record_kept` 加上这一条）。

起止：终点是记录的；起点是这条记录正文叠加条目最早的起点（和字幕稿、叠加同一个时刻，被名牌等别的字拉早的已挪对），没有正文条目的用记录的。

options：
* `show` / `hide`：逗号分隔的组名、`fill`（`show` 另认 `all`），同叠加预设；`keep` / `layers` 对匹配产物已去掉，传了报错；
* `lang`：`jp`（默认）/ `cn` / `en`，正文取哪种语言；
* `echo`：`drop`（默认）不出同一句的同时回显和记录面板回显（记录的 `echo` 是 `matchedio.SRT_DROP_ECHOES` 之一，一句一条；
  主落在别的记录的 `extra` 上时照出，见 `matchedio.record_kept`）/ `keep` 放回来（屏上出现过的每一次）；
* `name`：文件名（默认 `<matched 的 stem 去掉 -matched>-<lang>.srt`）。
"""
from __future__ import annotations

from pathlib import Path

from flowocr import placeholders
from flowocr.artifacts import matchedio, srtio
from flowocr.output import script

ACCEPTS = (matchedio.SCHEMA,)
"""这个预设吃哪几种阶段 2 产物（`flowocr.output.render` 按 `document["schema"]` 核）。"""
OUTPUTS = ("srt",)
"""只出 SRT（见 `default_srt.OUTPUTS`）。"""


def render(document: dict, output_dir, options: dict | None = None) -> list[Path]:
    o = dict(options or {})
    matchedio.validate(document)
    vis = script.matched_visible(document, o)
    lang = str(o.get("lang", "jp"))
    matchedio.need_lang(document, lang)
    echo = str(o.get("echo", "drop"))
    matchedio.echo_kept({}, echo)
    raw = "unmatched" in vis.groups                  # OCR 原文（没认领的记录、认领了但没这个语种文本的）出不出

    def shown(r: dict) -> bool:
        ref = r.get("ref")
        if ref and ref.get("group") == matchedio.SPECIAL:
            return False
        return vis.shows(ref) and (raw or bool(matchedio.text_of(ref, lang))) if ref else raw
    cues = document["cues"]
    out = {ri for ri, r in enumerate(cues) if shown(r)}
    # 起止取这条记录正文叠加条目的：起点 `scriptmatch.body_start` 把被名牌、计数器等别的字拉早的挪到了这句字出现，
    # 终点夹到正文消失（同一条 cue 里晚消失的名牌不再拖晚，检测器三场修复计划 U2）；没有正文条目的照记录的。
    # 没有叠加层的产物（`scriptmatch --no-overlay`、naninovel 关掉 overlay，只要 SRT 时）整份照记录
    body: dict[int, list[int]] = {}
    for it in (document.get("overlay") or {}).get("items") or ():
        if it["layer"] == "body" and it["claim"][1] == -1:
            se = body.setdefault(it["claim"][0], [it["start_us"], it["end_us"]])
            se[0], se[1] = min(se[0], it["start_us"]), max(se[1], it["end_us"])
    marks = placeholders.Marks(document.get("fills"), lang)
    d = Path(output_dir)
    d.mkdir(parents=True, exist_ok=True)
    stem = Path(matchedio.tracks_path(document) or "matched").stem.removesuffix("-tracks")
    p = d / str(o.get("name") or f"{stem}-{lang}.srt")
    # record_kept 为 False 时 echo_of 一定是 [记录, -1]：主那条记录这次没出，这条回显就是这一句唯一出的一次，照出
    srtio.write_srt_blocks(p, ((*body.get(ri, (int(r["start"] * 1e6), int(r["end"] * 1e6))), matchedio.body(r, lang, marks))
                               for ri, r in enumerate(cues)
                               if ri in out and (matchedio.record_kept(r, echo) or r["echo_of"][0] not in out)))
    marks.say("SRT ", srt=True)
    return [p]
