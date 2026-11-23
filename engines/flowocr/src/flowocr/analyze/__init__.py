"""阶段 2：聚类与匹配（时间合并、默认空间聚类、UI / 名牌 / 噪音判断、可选的参考文本匹配）。

project-structure 计划。2026-09-22 第一刀把 `tools/` 里这些模块**原样**搬进来：
`build_tracks`（仍然连写 SRT 的部分一起，拆到 output 是下一步）、`uigate`、`nameplate`、`cluster_layers`、
`slot_lines` / `slot_modes` / `slot_pairs` / `slot_veto` / `slot_learned`（模型 `models/slot_pair.json` 随代码走）、
`pair_features` / `pair_model`、`game_patches`、`gamescript`、`game_align`、`script_align`、
`scriptmatch`（含 ASS 渲染，同样待拆）、`merge_nameplate`。`tools/` 原位留 shim。
"""
