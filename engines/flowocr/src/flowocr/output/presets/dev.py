"""内置预设 `dev`（开发默认，owner 2026-09-24）：**全部保留**的叠加字幕，看得出每条字属于哪个聚类。

* tracks 产物：全部区域轨的事件（含常驻 UI，黄字 #ffe033）；matched 产物：`show=all`（同 `default_all`），没画成译文的、
  没喂给匹配器的事件照 tracks 画 OCR 原文；
* 非主轨聚类的字用浅蓝（#73d7ff），每条字幕底板外接矩形内侧的右上角一个红字（#ff2d2d）区域号（不影响字幕本身的位置和对齐）；
* 底板 `rows`，近黑、α 0x80（50%，看得见底下的原文）；对齐按阶段 2 判的 `regions[].align`（matched 正文 / extra 按文本框画像的），效果检出即开；
* 字幕稿里每行的 `fo` 另带 `why`（效果判定的依据）。

写两份：字幕稿 `…-dev.script.ass` 和最终的 `…-dev.ass`。选项和实现见 `flowocr.output.presets._overlay`；方案见 script-fx 计划。
"""
from __future__ import annotations

from pathlib import Path

from flowocr.artifacts import matchedio, tracksio
from flowocr.output.presets import _overlay

ACCEPTS = (tracksio.SCHEMA, matchedio.SCHEMA)


def render(document: dict, output_dir, options: dict | None = None) -> list[Path]:
    return _overlay.render(document, output_dir, options, "dev")
