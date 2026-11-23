"""阶段 1 的 obs（`run_ocr2` 写的 jsonl：首行 `{"_meta": …}`，之后一行一个观测）整份读进来的入口。

**逐行解析、只留要的字段**（resource 计划第 2 步，2026-10-05）：原来 `read_text` + `splitlines` + 全量 `json.loads`，
整份文本、行列表、字典三份同时在内存里；`poly`、`edge`、`frame` 这几个键阶段 2 建 run 用不到，却占了字典的大头。
zzz 整片 523 k 条时读入就要 1.48 GB（文本 0.23 + 行 0.23 + 字典 1.0）。

只读部分时刻、或只要 `edge` 的读法（`refine_boundaries`、`gamescript.obs_texts`、`ptsclock`）本来就是自己流式读，不走这里。
"""
from __future__ import annotations

import json
from pathlib import Path

RUN_FIELDS = ("t_us", "box", "text", "conf", "reused")
"""`build_tracks.build_runs`（连同它调的 `rejoin_split_lines` / `estimate_shift`）读的字段（`reused` 只在为真时出现，缺了就是假）。
那边多读一个键要加在这里，不然投影把它丢掉、`.get` 静默拿到 None——守卫从源码扫这几个函数读的键，必须是它的子集。"""


def read_obs(path: str | Path, min_conf: float = 0.0,
             fields: tuple[str, ...] = RUN_FIELDS) -> tuple[dict, list[dict]]:
    """`(_meta, 观测列表)`：观测按文件顺序，只留 `fields` 里出现过的键，`conf < min_conf` 的不要。"""
    obs: list[dict] = []
    with open(path, encoding="utf-8") as f:
        meta = json.loads(f.readline())["_meta"]
        for line in f:
            if not line.strip():
                continue
            o = json.loads(line)
            if o["conf"] >= min_conf:
                obs.append({k: o[k] for k in fields if k in o})
    return meta, obs
