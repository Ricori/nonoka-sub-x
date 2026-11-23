"""内置预设：阶段 3 的 SRT——一份轻量、符合直觉、默认对应主轨的 sidecar（owner 2026-10-05：阶段 3、4 的产物以 ASS 为主，
要 SRT 时用这个预设，不给叠加预设另加开关）。

条目和 `default` 的字幕稿是同一批（`script.build_items`）：一句一条、可见的几行按 SRT 的标准换行分行；名牌、常驻 UI、阅读面板、特殊上屏形式的台词与提示（`Special`）不进
（`script.srt_blocks`）。matched 的译文出剧本原句。阶段 2 写在 tracks.json 旁边的逐轨 SRT 是分析用的投影，和它不是一回事、也不同名。

options：
* `keep`：tracks 用，`main`（默认，同 `default`）/ `all`（同 `default_all` 的条目：全部区域轨）；
* `show` / `hide` / `lang` / `tracks`：matched 用，同叠加预设（显示协议：画哪些按匹配器声明的组，`show=all` 同 `default_all`；
  `keep` / `layers` 对匹配产物已去掉，传了报错）；matched 要对应的 tracks.json 在场（取事件和时刻，`matched_srt` 不用它）；
* `start`：cue 起点口径，`first`（默认，首字出现）/ `full`（全字出现）；
* `echo`：matched 用，`drop`（默认）不出同一句的同时回显和记录面板回显（`matchedio.SRT_DROP_ECHOES`，一句一条）/ `keep` 放回来；
* `name`：文件名（默认 `<tag>[-<lang>]-default.srt`，`keep=all`（matched 是 `show` 里有 `all`）时 `-default_all.srt`，和字幕稿同一个前缀）。
不查对齐判定（`regions[].align`）：SRT 用不到，较早的产物照样能出。
"""
from __future__ import annotations

from pathlib import Path

from flowocr import placeholders
from flowocr.artifacts import matchedio, srtio, tracksio
from flowocr.output import script

ACCEPTS = (tracksio.SCHEMA, matchedio.SCHEMA)
"""这个预设吃哪几种阶段 2 产物（`flowocr.output.render` 按 `document["schema"]` 核）。"""
OPTIONS = ("keep", "show", "hide", "lang", "layers", "tracks", "start", "echo", "name")
"""认的选项；`layers` 只为了交给 `build_items` 报错（已去掉，指向 `show` / `hide`）。"""
BUILD = ("keep", "show", "hide", "lang", "layers", "tracks")
"""交给 `script.build_items` 的那几个（取哪些条目）。"""
OUTPUTS = ("srt",)
"""只出 SRT（`scriptmatch --ass` 按它拒收：不能把 SRT 写进给 ASS 的文件名）。不声明的预设当作出 ASS。"""


def render(document: dict, output_dir, options: dict | None = None) -> list[Path]:
    o = dict(options or {})
    bad = sorted(set(o) - set(OPTIONS))
    if bad:
        raise ValueError(f"default_srt 不认识的选项：{bad}（认的：{list(OPTIONS)}）")
    start = str(o.get("start", "first"))
    if start not in ("first", "full"):
        raise ValueError(f"start 只能是 first / full：{start!r}")
    echo = str(o.get("echo", "drop"))
    matchedio.echo_kept({}, echo)                    # 先核取值，别等条目都建好了才报
    marks = placeholders.Marks(document.get("fills"), str(o.get("lang", "jp")))
    items, _doc, base, note, _src = script.build_items(document, {k: v for k, v in o.items() if k in BUILD},
                                                       align=False, marks=marks)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    wide = ("all" in matchedio.names(o.get("show")) if document.get("schema") == matchedio.SCHEMA
            else str(o.get("keep", "main")) == "all")
    p = out / str(o.get("name") or f"{base}-{'default_all' if wide else 'default'}.srt")
    blocks = script.srt_blocks(items, start=start, echo=echo)
    n = srtio.write_srt_blocks(p, blocks)
    print(f"-> {p}（{n} 条；{note}）")
    marks.say("SRT ", srt=True, texts=[ln for b in blocks for ln in b[2]])
    return [p]
