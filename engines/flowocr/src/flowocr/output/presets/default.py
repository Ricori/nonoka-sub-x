"""内置预设 `default`（**生产默认**，owner 2026-09-24；`render` 不给 `--preset` 时就是它）：只留主轨的叠加字幕。

* tracks 产物：主轨（和阶段 2 的主轨 SRT 同一个行集，常驻 UI 按主轨自己的判定剔掉）+ 名牌轨；
* matched 产物：匹配器声明的默认可见组（`always` + `on` 档，填位按 `display.fill`；显示协议，`--opt show=` / `hide=` 增减）——
  认领上剧本的不论在不在主轨（`--feed nonoise` 多出来的正是主轨外的台词）；
* 样式：底板 `rows`、近黑、α 0x20；对齐按 `regions[].align`（matched 正文 / extra 按文本框画像的），效果检出即开。

写两份：字幕稿 `<tag>[-<lang>]-default.script.ass`（阶段 3，能在 Aegisub 里编辑）和最终的 `<tag>[-<lang>]-default.ass`（阶段 4）。
选项和实现见 `flowocr.output.presets._overlay`；方案见 script-fx 计划。
"""
from __future__ import annotations

from pathlib import Path

from flowocr.artifacts import matchedio, tracksio
from flowocr.output.presets import _overlay

ACCEPTS = (tracksio.SCHEMA, matchedio.SCHEMA)


def render(document: dict, output_dir, options: dict | None = None) -> list[Path]:
    return _overlay.render(document, output_dir, options, "default")
