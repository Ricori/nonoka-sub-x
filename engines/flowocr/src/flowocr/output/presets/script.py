"""内置预设 `script`：只跑阶段 3，写一份**字幕稿**（`*.script.ass`），在 Aegisub / mpv 里预览、编辑，改完再跑阶段 4：

    python -m flowocr.output.render x-tracks.json --preset script --opt keep=all --outdir tmp/x
    python -m flowocr.typeset tmp/x/x-all.script.ass

options 见 `flowocr.output.script.render_script`（tracks 的 `keep` main / all；matched 的 `show` / `hide`、`lang`、`tracks`、`style`、`ruby`；
`why`、`typewriter`、`fade`、`name`）。默认文件名 `<tag>[-<lang>]-<main|all>.script.ass`（matched 的 `show` 里有 `all` 时是 `all`）。
字幕稿的格式见 artifacts.md 的字幕稿一节；方案见 script-fx 计划。
"""
from __future__ import annotations

from pathlib import Path

from flowocr.artifacts import matchedio, tracksio
from flowocr.output import script

ACCEPTS = (tracksio.SCHEMA, matchedio.SCHEMA)


def render(document: dict, output_dir, options: dict | None = None) -> list[Path]:
    return [script.render_script(document, output_dir, options)[0]]
