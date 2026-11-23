"""内置预设 `default_all`（owner 2026-09-24）：同 `dev` 的保留范围，但成品的样子。

* tracks 产物：全部区域轨的事件，常驻 UI 也照常画（删不删由用户自己判断）；matched 产物：`show=all`，全部组加填位，`unmatched` / `unfed` 两组的事件
  （没画成译文的、没喂给匹配器的）照 tracks 画 OCR 原文；`--opt hide=` 关掉不要的组；
* 不标聚类编号、不改非主轨的字色；
* 底板 `rows`，近黑、α 0x20（更不透明，盖住原文）；对齐按 `regions[].align`（matched 正文 / extra 按文本框画像的），效果检出即开。

写两份：字幕稿 `…-default_all.script.ass` 和最终的 `…-default_all.ass`。选项和实现见 `flowocr.output.presets._overlay`；方案见 script-fx 计划。
"""
from __future__ import annotations

from pathlib import Path

from flowocr.artifacts import matchedio, tracksio
from flowocr.output.presets import _overlay

ACCEPTS = (tracksio.SCHEMA, matchedio.SCHEMA)


def render(document: dict, output_dir, options: dict | None = None) -> list[Path]:
    return _overlay.render(document, output_dir, options, "default_all")
